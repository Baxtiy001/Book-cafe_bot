"""aiogram 3.x router: /start + secure WebApp entry + courier claim callbacks.

The chat interface is deliberately minimal: a clean greeting and one prominent
full-screen WebApp button. Everything else happens inside the Mini App.

Dependencies (``db``, ``bot``, ``dispatch``) are injected by aiogram from
workflow data set in ``bot.py``.
"""
from __future__ import annotations

import json
import time

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from config import settings
from database import Database
from services.dispatch import CourierCB, DispatchService, claim_and_notify

router = Router(name="start")


def _webapp_button(text: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text, web_app=WebAppInfo(url=settings.WEBAPP_URL))
    ]])


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def cmd_start(message: Message, command: CommandObject | None, db: Database,
                    bot: Bot, dispatch: DispatchService) -> None:
    from_user = message.from_user
    assert from_user is not None

    await db.upsert_user(
        tg_id=from_user.id,
        first_name=from_user.first_name or "",
        last_name=from_user.last_name or "",
        username=from_user.username or "",
        language=from_user.language_code or settings.DEFAULT_LANGUAGE,
    )

    payload = command.args if command else None
    if payload and payload.startswith("cour_"):
        await _start_courier_flow(message, db, bot)
        return

    if from_user.id in set(settings.ADMIN_IDS):
        text = (
            "👋 <b>Assalomu alaykum, admin!</b>\n\n"
            "Bu chat faqat kirish nuqtasi. Filiallar, menyu, brending, statistika "
            "va kuryerlar — hammasi Mini App ichida boshqariladi."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Admin panelni ochish",
                                  web_app=WebAppInfo(url=settings.WEBAPP_URL))],
        ])
        await message.answer(text, reply_markup=kb)
        return

    text = (
        "👋 <b>Assalomu alaykum!</b>\n\n"
        "📖 <b>Book Cafe</b> — kitob, qahva va mazali taomlar.\n\n"
        "Pastdagi tugma orqali to'liq ekranli ilovamizni oching:"
    )
    await message.answer(text, reply_markup=_webapp_button("📱 Menyuni ochish"))


@router.message(Command("help"), F.chat.type == ChatType.PRIVATE)
async def cmd_help(message: Message, db: Database) -> None:
    user = await db.get_user(message.from_user.id)  # type: ignore[union-attr]
    role = user["role"] if user else "client"
    lines = [
        "ℹ️ <b>Book Cafe Mini App</b>",
        "",
        "• /start — ilovani ochish",
        "• /app — to'liq ekranli ilova",
        "• /myid — Telegram ID raqamingiz",
    ]
    if role == "courier":
        lines += ["• /online — navbatga chiqish", "• /offline — navbatdan chiqish"]
    if role == "admin":
        lines += ["• /stats — statistika", "• /couriers — kuryerlar holati"]
    await message.answer("\n".join(lines))


@router.message(Command("app"), F.chat.type == ChatType.PRIVATE)
async def cmd_app(message: Message) -> None:
    await message.answer(
        "📱 <b>Book Cafe</b> ilovasini oching:",
        reply_markup=_webapp_button("Ochish"),
    )


@router.message(Command("myid"), F.chat.type == ChatType.PRIVATE)
async def cmd_myid(message: Message) -> None:
    await message.answer(
        f"🆔 Sizning Telegram ID: <code>{message.from_user.id}</code>"  # type: ignore[union-attr]
    )


@router.message(Command("stats"), F.chat.type == ChatType.PRIVATE)
async def cmd_stats(message: Message, db: Database) -> None:
    if message.from_user is None or message.from_user.id not in set(settings.ADMIN_IDS):
        await message.answer("⛔️ Bu buyruq faqat adminlar uchun.")
        return
    revenue = await db.revenue_since(None, time.time() - 86400)
    orders = await db.count_orders(statuses=[
        "new", "accepted", "cooking", "ready",
        "courier_assigned", "delivering", "delivered",
    ])
    await message.answer(
        f"📊 <b>Oxirgi 24 soat</b>\n\n"
        f"💰 Tushum: <b>{revenue:,} so'm</b>".replace(",", " ") +
        f"\n📦 Buyurtmalar: <b>{orders}</b>",
        reply_markup=_webapp_button("📊 To'liq statistika"),
    )


@router.message(Command("couriers"), F.chat.type == ChatType.PRIVATE)
async def cmd_couriers(message: Message, db: Database) -> None:
    if message.from_user is None or message.from_user.id not in set(settings.ADMIN_IDS):
        await message.answer("⛔️ Faqat adminlar uchun.")
        return
    couriers = await db.list_couriers()
    if not couriers:
        await message.answer("Kuryerlar ro'yxati bo'sh. Kuryerlar /courier buyrug'i bilan ulanadi.")
        return
    lines = ["🛵 <b>Kuryerlar</b>\n"]
    for c in couriers:
        emoji = {"online": "🟢", "delivering": "🛵", "assigned": "⏳",
                 "offline": "⚫️"}.get(c["status"], "⚫️")
        lines.append(
            f"{emoji} {c.get('first_name') or c['user_id']} — {c['status']}"
            f" | 📦 {c['total_orders']} | ⭐️ {c['rating']:.1f}"
        )
    await message.answer("\n".join(lines), reply_markup=_webapp_button("🛵 Kuryerlar paneli"))


@router.message(Command("courier"), F.chat.type == ChatType.PRIVATE)
async def cmd_courier(message: Message, db: Database, bot: Bot) -> None:
    await _start_courier_flow(message, db, bot)


async def _start_courier_flow(message: Message, db: Database, bot: Bot) -> None:
    from_user = message.from_user
    assert from_user is not None
    await db.upsert_user(
        tg_id=from_user.id,
        first_name=from_user.first_name or "",
        last_name=from_user.last_name or "",
        username=from_user.username or "",
    )
    existing = await db.get_courier_by_user(from_user.id)
    if existing:
        await message.answer(
            f"🛵 Siz kuryersiz (PIN: <code>{existing['pin_code']}</code>).\n"
            "Mini App ichida 'Kuryer' bo'limiga o'ting:",
            reply_markup=_webapp_button("🛵 Kuryer paneli"),
        )
        return
    courier = await db.register_courier(from_user.id)
    await message.answer(
        "🛵 <b>Kuryer sifatida ro'yxatdan o'tdingiz!</b>\n"
        f"PIN kodingiz: <code>{courier['pin_code']}</code>\n\n"
        "Navbatga chiqish: /online",
        reply_markup=_webapp_button("🛵 Kuryer paneli"),
    )
    for admin_id in settings.ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"🛵 Yangi kuryer qo'shildi: {from_user.first_name or from_user.id}"
                f" (ID: <code>{from_user.id}</code>)",
            )
        except Exception:
            pass


@router.message(Command("online"), F.chat.type == ChatType.PRIVATE)
async def cmd_online(message: Message, db: Database) -> None:
    courier = await db.get_courier_by_user(message.from_user.id)  # type: ignore[union-attr]
    if courier is None:
        await message.answer("⛔️ Siz kuryer emassiz. /courier buyrug'i bilan ulaning.")
        return
    await db.set_courier_status(courier["id"], "online")
    await message.answer("✅ Siz navbatdasiz. Yangi buyurtmalar shu chatga tushadi.")


@router.message(Command("offline"), F.chat.type == ChatType.PRIVATE)
async def cmd_offline(message: Message, db: Database) -> None:
    courier = await db.get_courier_by_user(message.from_user.id)  # type: ignore[union-attr]
    if courier is None:
        await message.answer("⛔️ Siz kuryer emassiz.")
        return
    await db.set_courier_status(courier["id"], "offline")
    await message.answer("👋 Navbatdan chiqdingiz.")


@router.message(F.web_app_data)
async def on_web_app_data(message: Message, db: Database, bot: Bot) -> None:
    """Legacy path: cart sent via sendData — acknowledged, API flow preferred."""
    data = message.web_app_data
    if data is None:
        return
    user = await db.get_user(message.from_user.id)  # type: ignore[union-attr]
    try:
        payload = json.loads(data.data)
    except json.JSONDecodeError:
        await message.answer("⚠️ Buyurtma formati noto'g'ri.")
        return
    await message.answer(
        "✅ Buyurtma qabul qilindi! Holatini Mini Appda kuzatib boring:",
        reply_markup=_webapp_button("📦 Buyurtmalarim"),
    )
    for admin_id in settings.ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"📦 WebApp buyurtma: {user['first_name'] if user else message.from_user.id}"
                f"\n{json.dumps(payload, ensure_ascii=False)[:500]}",
            )
        except Exception:
            pass


@router.callback_query(CourierCB.filter(F.action == "accept"))
async def cb_accept(query: CallbackQuery, callback_data: CourierCB,
                    db: Database, bot: Bot, dispatch: DispatchService) -> None:
    ok, text = await claim_and_notify(db, bot, dispatch,
                                      callback_data.order_id, query.from_user.id)
    await query.answer(text, show_alert=not ok)


@router.callback_query(CourierCB.filter(F.action == "reject"))
async def cb_reject(query: CallbackQuery, callback_data: CourierCB, db: Database) -> None:
    await db.reject_order(callback_data.order_id, query.from_user.id)
    await query.answer("👋 Rad etildi")
