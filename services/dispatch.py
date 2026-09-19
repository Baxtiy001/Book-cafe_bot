"""Courier dispatch service — race-condition-safe broadcast + claim.

Flow
----
1. Client order reaches status ``new``.
2. ``broadcast_order`` fans out the "New Order!" message with inline
   Accept/Reject buttons to every courier whose status is ``online`` (and to
   courier chat ids listed in settings if any).
3. The first courier to press ``Accept`` claims the order. The database layer
   guarantees atomicity; this module then closes the keyboard for EVERY other
   courier by editing the broadcast messages to "Order claimed 🎉".
4. If nobody accepts within ``auto_close_seconds`` the keyboard expires on its
   own (button callback just answers "offer expired"), and the order can be
   re-broadcast manually by an admin.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import settings
from database import Database

log = logging.getLogger("dispatch")

AUTO_CLOSE_SECONDS = 90


class CourierCB(CallbackData, prefix="cour"):
    action: str          # accept | reject
    order_id: int


def _order_summary(order: dict[str, Any]) -> str:
    lines = [f"📦 <b>Yangi buyurtma #{order['public_code']}</b>"]
    lines.append("— — —")
    for item in order.get("items", []):
        qty = int(item.get("qty", 1))
        lines.append(f"• {item.get('title', '?')} × {qty}")
    lines.append("— — —")
    kind = order.get("kind", "delivery")
    kind_label = {"delivery": "🛵 Yetkazish", "pickup": "🏪 Olib ketish",
                  "dinein": "🍽 Zalda"}.get(kind, kind)
    lines.append(f"{kind_label}: {order.get('address') or '-'}")
    lines.append(f"📞 {order.get('phone') or '-'}")
    if order.get("comment"):
        lines.append(f"📝 {order['comment']}")
    lines.append(f"💰 Yetkazish: {order.get('delivery_fee', 0):,} so'm".replace(",", " "))
    lines.append(f"💳 Jami: <b>{order.get('total', 0):,} so'm</b>".replace(",", " "))
    return "\n".join(lines)


def courier_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Olib boraman", callback_data=CourierCB(action="accept", order_id=order_id).pack()
        ),
        InlineKeyboardButton(
            text="❌ Rad etish", callback_data=CourierCB(action="reject", order_id=order_id).pack()
        ),
    ]])


class DispatchService:
    """Owns broadcast fan-out and post-race keyboard cleanup."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._sent: dict[int, list[tuple[int, int]]] = {}

    async def broadcast_order(self, bot: Bot, order: dict[str, Any]) -> int:
        couriers = await self.db.list_couriers()
        online_ids = [
            c for c in couriers
            if c.get("status") == "online"
        ]
        text = _order_summary(order)
        sent: list[tuple[int, int]] = []
        for courier in online_ids:
            chat_id = int(courier["user_id"])
            try:
                msg = await bot.send_message(
                    chat_id, text, reply_markup=courier_keyboard(order["id"])
                )
                sent.append((chat_id, msg.message_id))
            except (TelegramForbiddenError, TelegramBadRequest) as exc:
                log.warning("Courier %s unreachable: %s", chat_id, exc)
        self._sent[order["id"]] = sent

        if sent:
            asyncio.create_task(self._expire_later(bot, order["id"]))
        return len(sent)

    async def _expire_later(self, bot: Bot, order_id: int) -> None:
        await asyncio.sleep(AUTO_CLOSE_SECONDS)
        order = await self.db.get_order(order_id)
        if order is None or order["courier_id"] is not None:
            return
        await self.close_broadcast(bot, order_id,
                                   text="⌛️ Taklif muddati tugadi — buyurtma shoshilinch emas.")

    async def close_broadcast(self, bot: Bot, order_id: int, text: str) -> None:
        """Freeze every courier's buttons after a winner is decided."""
        sent = self._sent.pop(order_id, [])
        new_text = text
        for chat_id, message_id in sent:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id,
                    text=new_text, reply_markup=None,
                )
            except TelegramBadRequest:
                pass  # already edited / identical content

    def winner_text(self, order: dict[str, Any], courier_name: str) -> str:
        body = _order_summary(order)
        return f"🏁 <b>{courier_name}</b> buyurtmani qabul qildi!\n\n{body}"


async def claim_and_notify(
    db: Database, bot: Bot, dispatch: DispatchService,
    order_id: int, courier_tg_id: int,
) -> tuple[bool, str]:
    courier = await db.get_courier_by_user(courier_tg_id)
    if courier is None:
        return False, "Siz kuryer sifatida ro'yxatdan o'tmagansiz."

    ok, order = await db.claim_order(order_id, courier["id"])
    if order is None:
        return False, "Buyurtma topilmadi."
    if not ok:
        return False, "⌛️ Kechikdingiz — bu buyurtmani boshqa kuryer oldi."

    user = await db.get_user(int(order["user_id"]))
    courier_name = courier.get("first_name") or "Kuryer"
    branch = await db.get_branch(order["branch_id"]) or {}

    # Notify every losing courier + freeze their keyboards.
    await dispatch.close_broadcast(
        bot, order_id,
        text=dispatch.winner_text(order, courier_name),
    )
    # Notify the client.
    if user:
        try:
            await bot.send_message(
                user["id"],
                f"🛵 <b>{courier_name}</b> buyurtmangizni olib boradi!"
                f"\n📦 #{order['public_code']}",
            )
        except TelegramForbiddenError:
            pass
    # Notify admins.
    for admin_id in settings.ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"🛵 <b>{courier_name}</b> buyurtma #{order['public_code']} ni oldi"
                f" ({branch.get('name', '')})",
            )
        except (TelegramForbiddenError, TelegramBadRequest):
            pass
    return True, "✅ Buyurtma sizga biriktirildi!"
