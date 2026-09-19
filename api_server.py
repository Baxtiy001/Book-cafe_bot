"""aiohttp REST API for the Book Cafe Mini App.

Auth model
----------
Every request carries ``Authorization: tma <initData>``. The initData is
HMAC-verified (services/auth.py) and resolved into a role. Admin-only routes
require the ``X-Branch-Id`` header or a ``branch_id`` param where relevant.

Endpoint map
------------
GET  /api/health                            -> liveness
GET  /api/bootstrap                         -> branding, catalog, rules, me
GET  /api/media/{key}                       -> uploaded branding/dish images
GET  /api/catalog?branch_id=                -> categories+dishes tree
GET  /api/orders/my                         -> client order history
POST /api/orders                            -> place order (client)
GET  /api/orders/{id}                       -> order detail (owner/courier/admin)
POST /api/orders/{id}/cancel                -> client cancel while new/accepted
GET  /api/courier/board                     -> courier assigned+available orders
POST /api/courier/status                    -> online/offline toggle
POST /api/courier/claim/{order_id}          -> race-safe claim from the app
POST /api/courier/deliver/{order_id}        -> mark delivered
POST /api/courier/pin                       -> bind telegram account by PIN code
GET  /api/admin/stats?branch_id=            -> KPIs, revenue by day, top dishes
CRUD /api/admin/branches|categories|dishes|delivery-rules|branding|settings
CRUD /api/admin/couriers|integrations
GET  /api/admin/integrations/{id}/export    -> on-demand JSON export for 1C
POST /api/integration/import                -> inbound 1C/1UZ import (token auth)
GET  /api/integration/orders-feed           -> polling fallback for accounting
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from aiohttp import web
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

import app_keys
from config import settings
from database import Database
from services.auth import AuthError, extract_auth_header, parse_init_data, resolve_role
from services.delivery import calculate_delivery
from services.dispatch import DispatchService, claim_and_notify
from services.integrations import ImportError_, apply_import
from services.notifications import build_export_document, fan_out_order_event

log = logging.getLogger("api")

MAX_UPLOAD = 12 * 1024 * 1024  # 12 MB
ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp", "image/gif"}
ACTIVE_STATUSES = ["new", "accepted", "cooking", "ready",
                   "courier_assigned", "delivering", "delivered"]


# --------------------------------------------------------------------- utils --
def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(
        data, status=status,
        dumps=lambda d: json.dumps(d, ensure_ascii=False, default=str),
    )


def _err(message: str, status: int = 400, **extra: Any) -> web.Response:
    return _json({"error": message, **extra}, status)


def _ok(data: Any = None) -> web.Response:
    return _json(data if data is not None else {"ok": True})


async def actor(request: web.Request) -> dict[str, Any]:
    """Async actor resolution (the real implementation used by handlers)."""
    if "actor" in request:
        return request["actor"]
    try:
        tg_user = parse_init_data(
            extract_auth_header(request.headers.get("Authorization"))
        )
    except AuthError as exc:
        raise web.HTTPUnauthorized(text=str(exc)) from exc
    db: Database = request.app[app_keys.DB_KEY]
    user = await db.get_user(tg_user.id)
    if user is None:
        # First API contact: persist the verified Telegram identity.
        user = await db.upsert_user(
            tg_user.id,
            first_name=tg_user.first_name,
            last_name=tg_user.last_name,
            username=tg_user.username,
            language=tg_user.language_code or settings.DEFAULT_LANGUAGE,
        )
    role = resolve_role(tg_user.id, user)
    request["actor"] = {
        "tg": tg_user,
        "user": user,
        "role": role,
        "is_admin": role == "admin",
        "is_courier": role == "courier",
    }
    return request["actor"]


async def require_admin(request: web.Request) -> dict[str, Any]:
    a = await actor(request)
    if not a["is_admin"]:
        raise web.HTTPForbidden(text="admin only")
    return a


async def require_courier(request: web.Request) -> dict[str, Any]:
    a = await actor(request)
    if not a["is_courier"] and not a["is_admin"]:
        raise web.HTTPForbidden(text="courier role required")
    return a


def _branch_scoped(request: web.Request) -> int:
    raw = request.headers.get("X-Branch-Id") or request.query.get("branch_id", "")
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="branch_id required (header X-Branch-Id)") from None


# ------------------------------------------------------------------- helpers --
async def load_catalog(db: Database, branch_id: int) -> dict[str, Any]:
    cats = await db.list_categories(branch_id)
    dishes = await db.list_dishes(branch_id)
    tree: dict[int, list[dict[str, Any]]] = {}
    for c in cats:
        tree.setdefault(int(c["parent_id"] or 0), []).append(c)
    return {
        "categories": cats,
        "dishes": dishes,
        "tree": {str(k): v for k, v in tree.items()},
    }


async def order_view(db: Database, order: dict[str, Any],
                     include_items: bool = True) -> dict[str, Any]:
    view = dict(order)
    if not include_items:
        view.pop("items", None)
    branch = await db.get_branch(order["branch_id"])
    view["branch"] = {
        "id": branch["id"], "name": branch["name"],
        "address": branch["address"], "phone": branch["phone"],
        "lat": branch["lat"], "lon": branch["lon"],
    } if branch else None
    courier = await db.get_courier(order["courier_id"]) if order["courier_id"] else None
    if courier:
        u = await db.get_user(courier["user_id"])
        view["courier"] = {
            "id": courier["id"], "name": (u or {}).get("first_name", "Kuryer"),
            "phone": (u or {}).get("phone", ""), "rating": courier["rating"],
        }
    else:
        view["courier"] = None
    return view


def media_url(key: str | None) -> str | None:
    return f"/api/media/{key}" if key else None


# --------------------------------------------------------------- middleware --
@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception:
        log.exception("Unhandled error on %s %s", request.method, request.path)
        return _err("internal error", 500)


@web.middleware
async def integration_auth_middleware(request: web.Request, handler):
    if request.path.startswith("/api/integration/"):
        token = request.headers.get("X-Integration-Token", "")
        if not settings.INTEGRATION_TOKEN or token != settings.INTEGRATION_TOKEN:
            return _err("invalid integration token", 401)
    return await handler(request)


# ------------------------------------------------------------ basic endpoints --
async def health(request: web.Request) -> web.Response:
    return _ok({"status": "ok", "time": int(time.time()), "version": 1})


async def bootstrap(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    a = await actor(request)
    branches = await db.list_branches()
    if not branches:
        return _err("no branches configured — run seed.py", 503)

    requested = request.query.get("branch_id", "")
    branch_id = int(requested) if requested.isdigit() else branches[0]["id"]
    branch = await db.get_branch(branch_id) or branches[0]
    branding = await db.get_branding(branch["id"])
    catalog = await load_catalog(db, branch["id"])
    rules = await db.list_delivery_rules(branch["id"])
    upsell_cfg = await db.get_setting("upsell", branch["id"])
    my_orders: list[dict[str, Any]] = []
    if a["role"] == "client":
        rows = await db.list_orders(user_id=a["tg"].id, limit=20)
        my_orders = [await order_view(db, o, include_items=False) for o in rows]
    return _ok({
        "me": {
            "id": a["tg"].id, "name": a["tg"].full_name, "role": a["role"],
            "phone": (a["user"] or {}).get("phone", ""),
        },
        "branches": branches,
        "branch": branch,
        "branding": {
            "logo_url": media_url(branding["logo_key"]),
            "banner_url": media_url(branding["banner_key"]),
            "theme": branding["theme_parsed"],
        },
        "catalog": catalog,
        "delivery_rules": rules,
        "upsell": upsell_cfg,
        "my_orders": my_orders,
    })


async def media(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    key = request.match_info["key"]
    meta = await db.get_media(key)
    if meta is None:
        raise web.HTTPNotFound(text="media not found")
    path = Path(settings.MEDIA_DIR) / meta["filename"]
    if not path.exists():
        raise web.HTTPNotFound(text="media file missing")
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


async def catalog(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    branch_id = int(request.query.get("branch_id", 0) or 0)
    if not branch_id:
        branches = await db.list_branches()
        branch_id = branches[0]["id"] if branches else 0
    return _ok(await load_catalog(db, branch_id))


# ------------------------------------------------------------------ orders --
async def create_order(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    bot: Bot = request.app[app_keys.BOT_KEY]
    dispatch: DispatchService = request.app[app_keys.DISPATCH_KEY]
    a = await actor(request)
    try:
        body = await request.json()
    except Exception:
        return _err("invalid JSON body")

    branch = await db.get_branch(int(body.get("branch_id") or 0))
    if branch is None:
        return _err("branch not found", 404)
    items = body.get("items") or []
    if not items:
        return _err("cart is empty")

    # Re-price against the live catalog — never trust client prices.
    priced: list[dict[str, Any]] = []
    items_total = 0
    for it in items:
        dish = await db.get_dish(int(it.get("id") or 0))
        if dish is None or not dish["is_active"] or not dish["is_available"]:
            continue
        qty = max(1, min(50, int(it.get("qty") or 1)))
        priced.append({
            "dish_id": dish["id"], "title": dish["title"],
            "price": dish["price"], "qty": qty,
        })
        items_total += dish["price"] * qty
    if not priced:
        return _err("all items unavailable", 409)

    kind = body.get("kind", "delivery")
    if kind not in {"delivery", "pickup", "dinein"}:
        kind = "delivery"
    lat = body.get("lat")
    lon = body.get("lon")
    address = (body.get("address") or "").strip()
    phone = (body.get("phone") or "").strip()
    if kind == "delivery" and not address:
        return _err("address required for delivery")

    delivery_fee = 0
    if kind == "delivery":
        delivery_fee = await calculate_delivery(db, branch, items_total, lat, lon)
    total = items_total + delivery_fee

    user = await db.get_user(a["tg"].id)
    if phone and user and user.get("phone") != phone:
        await db.update_user(a["tg"].id, {"phone": phone})
    if kind == "delivery" and address:
        known = await db.list_addresses(a["tg"].id)
        if not any(ad["address"] == address for ad in known):
            await db.add_address(a["tg"].id, title="So'nggi", address=address,
                                 lat=lat, lon=lon)

    order = await db.create_order({
        "user_id": a["tg"].id,
        "branch_id": branch["id"],
        "kind": kind,
        "items": priced,
        "items_total": items_total,
        "delivery_fee": delivery_fee,
        "total": total,
        "address": address,
        "lat": lat, "lon": lon,
        "phone": phone,
        "comment": (body.get("comment") or "")[:500],
        "payment": body.get("payment", "cash"),
    })

    courier_count = 0
    if kind == "delivery":
        courier_count = await dispatch.broadcast_order(bot, order)

    asyncio.create_task(fan_out_order_event(db, "order.created", order))
    await db.audit(a["tg"].id, "order.create", "order", order["id"])

    # Notify admins about the new order.
    summary_lines = [f"📦 <b>Yangi buyurtma #{order['public_code']}</b>"
                     f" ({branch['name']})"]
    for item in priced:
        summary_lines.append(f"• {item['title']} × {item['qty']}")
    summary_lines.append(f"💳 Jami: {total:,} so'm".replace(",", " "))
    for admin_id in settings.ADMIN_IDS:
        try:
            await bot.send_message(admin_id, "\n".join(summary_lines))
        except (TelegramForbiddenError, TelegramBadRequest):
            pass

    return _ok({"order": await order_view(db, order),
                "couriers_notified": courier_count})


async def my_orders(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    a = await actor(request)
    rows = await db.list_orders(user_id=a["tg"].id, limit=50)
    return _ok({"orders": [await order_view(db, o, include_items=False) for o in rows]})


async def order_detail(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    a = await actor(request)
    order = await db.get_order(int(request.match_info["order_id"]))
    if order is None:
        return _err("order not found", 404)
    if not a["is_admin"] and order["user_id"] != a["tg"].id:
        courier = await db.get_courier_by_user(a["tg"].id)
        if not courier or order["courier_id"] != courier["id"]:
            return _err("forbidden", 403)
    return _ok({"order": await order_view(db, order)})


async def cancel_order(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    bot: Bot = request.app[app_keys.BOT_KEY]
    a = await actor(request)
    order = await db.get_order(int(request.match_info["order_id"]))
    if order is None:
        return _err("order not found", 404)
    if order["user_id"] != a["tg"].id and not a["is_admin"]:
        return _err("forbidden", 403)
    if order["status"] not in {"new", "accepted"}:
        return _err(f"cannot cancel in status {order['status']}", 409)
    await db.update_order(order["id"], {
        "status": "cancelled", "cancelled_at": time.time(),
        "cancel_reason": "client" if order["user_id"] == a["tg"].id else "admin",
    })
    updated = await db.get_order(order["id"])
    assert updated is not None
    asyncio.create_task(fan_out_order_event(db, "order.cancelled", updated))
    if order["courier_id"]:
        courier = await db.get_courier(order["courier_id"])
        if courier:
            await db.set_courier_status(courier["id"], "online")
            try:
                await bot.send_message(
                    courier["user_id"],
                    f"⚠️ Buyurtma #{order['public_code']} bekor qilindi.",
                )
            except (TelegramForbiddenError, TelegramBadRequest):
                pass
    return _ok({"order": await order_view(db, updated)})


# ----------------------------------------------------------------- courier --
async def courier_board(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    a = await require_courier(request)
    courier = await db.get_courier_by_user(a["tg"].id)
    if courier is None:
        return _ok({"registered": False})
    active = await db.list_orders(courier_id=courier["id"],
                                  statuses=["courier_assigned", "delivering"])
    history = await db.list_orders(courier_id=courier["id"],
                                   statuses=["delivered"], limit=15)
    open_orders = await db.list_orders(statuses=["new", "accepted", "cooking", "ready"],
                                       limit=30)
    available = [o for o in open_orders
                 if o["kind"] == "delivery" and o["courier_id"] is None]
    return _ok({
        "registered": True,
        "profile": courier,
        "active_orders": [await order_view(db, o) for o in active],
        "available_orders": [await order_view(db, o) for o in available],
        "history": [await order_view(db, o, include_items=False) for o in history],
    })


async def courier_status(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    a = await require_courier(request)
    courier = await db.get_courier_by_user(a["tg"].id)
    if courier is None:
        return _err("not a registered courier", 403)
    try:
        body = await request.json()
    except Exception:
        return _err("invalid JSON body")
    status = body.get("status")
    if status not in {"online", "offline"}:
        return _err("status must be online|offline")
    await db.set_courier_status(courier["id"], status)
    return _ok({"status": status})


async def courier_claim(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    bot: Bot = request.app[app_keys.BOT_KEY]
    dispatch: DispatchService = request.app[app_keys.DISPATCH_KEY]
    a = await require_courier(request)
    order_id = int(request.match_info["order_id"])
    ok, message = await claim_and_notify(db, bot, dispatch, order_id, a["tg"].id)
    return _json({"ok": ok, "message": message}, status=200 if ok else 409)


async def courier_deliver(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    a = await require_courier(request)
    order = await db.get_order(int(request.match_info["order_id"]))
    courier = await db.get_courier_by_user(a["tg"].id)
    if order is None or courier is None:
        return _err("not found", 404)
    if order["courier_id"] != courier["id"] and not a["is_admin"]:
        return _err("forbidden", 403)
    # Kitchen stage (cooking/ready) may coexist with an assigned courier;
    # completion is valid whenever this courier owns a live order.
    if order["status"] not in {"courier_assigned", "delivering", "ready"}:
        return _err(f"cannot complete from status {order['status']}", 409)
    await db.update_order(order["id"],
                          {"status": "delivered", "delivered_at": time.time()})
    await db.update_courier(courier["id"], {"status": "online"})
    await db.bump_courier_stats(courier["id"])
    updated = await db.get_order(order["id"])
    assert updated is not None
    asyncio.create_task(fan_out_order_event(db, "order.delivered", updated))
    client = await db.get_user(int(order["user_id"]))
    if client:
        bot: Bot = request.app[app_keys.BOT_KEY]
        try:
            await bot.send_message(
                client["id"],
                f"✅ Buyurtma #{order['public_code']} yetkazildi! Yoqimli ishtaha!",
            )
        except (TelegramForbiddenError, TelegramBadRequest):
            pass
    return _ok({"order": await order_view(db, updated)})


async def courier_pin(request: web.Request) -> web.Response:
    """A client pastes the PIN an admin shared — becomes that courier."""
    db: Database = request.app[app_keys.DB_KEY]
    a = await actor(request)
    try:
        body = await request.json()
    except Exception:
        return _err("invalid JSON body")
    pin = str(body.get("pin") or "").strip().upper()
    if not pin:
        return _err("pin required")
    for c in await db.list_couriers():
        if (c.get("pin_code") or "").upper() == pin:
            await db.update_user(a["tg"].id, {"role": "courier"})
            await db.update_courier(c["id"], {"user_id": a["tg"].id})
            await db.audit(a["tg"].id, "courier.pin_bind", "courier", c["id"])
            return _ok({"registered": True, "courier_id": c["id"]})
    return _err("invalid pin", 403)


# ------------------------------------------------------------------- admin --
async def admin_stats(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    a = await require_admin(request)
    branch_id = _branch_scoped(request)
    branch = await db.get_branch(branch_id)
    if branch is None:
        return _err("branch not found", 404)
    now = time.time()
    stats = {
        "branch": branch,
        "today": {
            "revenue": await db.revenue_since(branch_id, now - 86400),
            "orders": await db.count_orders(branch_id, statuses=ACTIVE_STATUSES),
        },
        "week": {
            "revenue": await db.revenue_since(branch_id, now - 7 * 86400),
            "orders": await db.count_orders(branch_id, statuses=ACTIVE_STATUSES),
        },
        "month": {
            "revenue": await db.revenue_since(branch_id, now - 30 * 86400),
            "orders": await db.count_orders(branch_id, statuses=ACTIVE_STATUSES),
        },
        "by_day": await db.orders_by_day(branch_id, days=14),
        "top_dishes": await db.top_dishes(branch_id, limit=5),
        "couriers": await db.list_couriers(branch_id),
        "audit": await db.list_audit(15),
    }
    return _ok(stats)


async def admin_branches_list(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    await require_admin(request)
    return _ok({"branches": await db.list_branches(only_active=False),
                "brands": await db.list_brands(only_active=False)})


async def admin_branch_create(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    a = await require_admin(request)
    try:
        body = await request.json()
    except Exception:
        return _err("invalid JSON body")
    name = (body.get("name") or "").strip()
    if not name:
        return _err("name required")
    brand_id = int(body.get("brand_id") or 0)
    brands = await db.list_brands()
    if brand_id and not any(b["id"] == brand_id for b in brands):
        return _err("brand not found", 404)
    if not brand_id:
        brand = brands[0] if brands else await db.create_brand(name)
        brand_id = brand["id"]
    branch = await db.create_branch(
        brand_id=brand_id, name=name, city=body.get("city", ""),
        address=body.get("address", ""), phone=body.get("phone", ""),
        lat=body.get("lat"), lon=body.get("lon"),
        work_from=body.get("work_from", "09:00"),
        work_to=body.get("work_to", "22:00"),
    )
    if not await db.list_delivery_rules(branch["id"]):
        await db.create_delivery_rule({
            "branch_id": branch["id"], "rule_type": "flat",
            "amount": int(body.get("default_delivery_fee", 15000)),
            "free_over": int(body.get("free_over", 100000)),
        })
    await db.audit(a["tg"].id, "branch.create", "branch", branch["id"])
    return _ok({"branch": branch})


async def admin_branch_update(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    a = await require_admin(request)
    branch_id = int(request.match_info["branch_id"])
    body = await request.json()
    await db.update_branch(branch_id, body)
    await db.audit(a["tg"].id, "branch.update", "branch", branch_id)
    return _ok({"branch": await db.get_branch(branch_id)})


async def admin_branch_delete(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    a = await require_admin(request)
    branch_id = int(request.match_info["branch_id"])
    await db.delete_branch(branch_id)
    await db.audit(a["tg"].id, "branch.delete", "branch", branch_id)
    return _ok()


async def admin_category_create(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    a = await require_admin(request)
    body = await request.json()
    branch_id = int(body.get("branch_id") or 0)
    if not await db.get_branch(branch_id):
        return _err("branch not found", 404)
    title = (body.get("title") or "").strip()
    if not title:
        return _err("title required")
    cat = await db.create_category(
        branch_id, title,
        parent_id=body.get("parent_id"),
        emoji=body.get("emoji", ""), position=int(body.get("position", 0)),
    )
    await db.audit(a["tg"].id, "category.create", "category", cat["id"])
    return _ok({"category": cat})


async def admin_category_update(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    a = await require_admin(request)
    cat_id = int(request.match_info["category_id"])
    body = await request.json()
    await db.update_category(cat_id, body)
    await db.audit(a["tg"].id, "category.update", "category", cat_id)
    return _ok({"category": await db.get_category(cat_id)})


async def admin_category_delete(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    a = await require_admin(request)
    cat_id = int(request.match_info["category_id"])
    await db.delete_category(cat_id)
    await db.audit(a["tg"].id, "category.delete", "category", cat_id)
    return _ok()


async def admin_dish_create(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    a = await require_admin(request)
    body = await request.json()
    branch_id = int(body.get("branch_id") or 0)
    if not await db.get_branch(branch_id):
        return _err("branch not found", 404)
    title = (body.get("title") or "").strip()
    if not title:
        return _err("title required")
    category_id = int(body.get("category_id") or 0)
    if not await db.get_category(category_id):
        return _err("category not found", 404)
    dish = await db.create_dish({
        "branch_id": branch_id, "category_id": category_id,
        "title": title,
        "description": body.get("description", ""),
        "price": int(body.get("price", 0)),
        "old_price": body.get("old_price"),
        "image_key": body.get("image_key"),
        "is_available": body.get("is_available", True),
        "is_hit": body.get("is_hit", False),
        "is_vegan": body.get("is_vegan", False),
        "upsell_group": body.get("upsell_group", ""),
        "position": int(body.get("position", 0)),
    })
    await db.audit(a["tg"].id, "dish.create", "dish", dish["id"])
    return _ok({"dish": dish})


async def admin_dish_update(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    a = await require_admin(request)
    dish_id = int(request.match_info["dish_id"])
    body = await request.json()
    await db.update_dish(dish_id, body)
    await db.audit(a["tg"].id, "dish.update", "dish", dish_id)
    return _ok({"dish": await db.get_dish(dish_id)})


async def admin_dish_delete(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    a = await require_admin(request)
    dish_id = int(request.match_info["dish_id"])
    await db.delete_dish(dish_id)
    await db.audit(a["tg"].id, "dish.delete", "dish", dish_id)
    return _ok()


async def admin_delivery_rules_list(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    await require_admin(request)
    branch_id = _branch_scoped(request)
    return _ok({"rules": await db.list_delivery_rules(branch_id, only_active=False)})


async def admin_delivery_rule_create(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    a = await require_admin(request)
    body = await request.json()
    if body.get("rule_type") not in {"free_over", "flat", "per_km", "percent"}:
        return _err("rule_type must be free_over|flat|per_km|percent")
    body["branch_id"] = _branch_scoped(request)
    rule_id = await db.create_delivery_rule(body)
    await db.audit(a["tg"].id, "delivery_rule.create", "rule", rule_id)
    return _ok({"rule_id": rule_id})


async def admin_delivery_rule_update(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    a = await require_admin(request)
    rule_id = int(request.match_info["rule_id"])
    body = await request.json()
    await db.update_delivery_rule(rule_id, body)
    await db.audit(a["tg"].id, "delivery_rule.update", "rule", rule_id)
    return _ok()


async def admin_delivery_rule_delete(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    a = await require_admin(request)
    rule_id = int(request.match_info["rule_id"])
    await db.delete_delivery_rule(rule_id)
    await db.audit(a["tg"].id, "delivery_rule.delete", "rule", rule_id)
    return _ok()


async def admin_branding_get(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    await require_admin(request)
    branch_id = _branch_scoped(request)
    b = await db.get_branding(branch_id)
    return _ok({
        "logo_url": media_url(b["logo_key"]),
        "banner_url": media_url(b["banner_key"]),
        "theme": b["theme_parsed"],
    })


async def admin_branding_update(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    a = await require_admin(request)
    branch_id = _branch_scoped(request)
    body = await request.json()
    theme = body.get("theme")
    if theme is not None and not isinstance(theme, dict):
        return _err("theme must be an object")
    await db.update_branding(
        branch_id, theme=theme,
        logo_key=body.get("logo_key"), banner_key=body.get("banner_key"),
    )
    await db.audit(a["tg"].id, "branding.update", "branch", branch_id)
    b = await db.get_branding(branch_id)
    return _ok({
        "logo_url": media_url(b["logo_key"]),
        "banner_url": media_url(b["banner_key"]),
        "theme": b["theme_parsed"],
    })


async def admin_media_upload(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    a = await require_admin(request)
    branch_raw = request.headers.get("X-Branch-Id") or request.query.get("branch_id", "0")
    try:
        branch_id = int(branch_raw or 0)
    except ValueError:
        branch_id = 0
    if not request.content_type.startswith("multipart/"):
        return _err("multipart/form-data required", 415)
    reader = await request.multipart()
    file_part = None
    kind = "image"
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "file":
            file_part = part
        elif part.name == "kind":
            kind = (await part.text()).strip() or "image"
    if file_part is None:
        return _err("multipart 'file' part required")
    mime = (file_part.headers.get("Content-Type") or "").split(";")[0].strip()
    if mime not in ALLOWED_MIME:
        return _err("only jpeg/png/webp/gif allowed", 415)
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await file_part.read_chunk(64 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > MAX_UPLOAD:
            return _err("file too large", 413)
        chunks.append(chunk)
    raw = b"".join(chunks)
    key = uuid.uuid4().hex
    ext = {"image/jpeg": ".jpg", "image/png": ".png",
           "image/webp": ".webp", "image/gif": ".gif"}.get(mime, ".jpg")
    filename = f"{key}{ext}"
    media_dir = Path(settings.MEDIA_DIR)
    media_dir.mkdir(parents=True, exist_ok=True)
    (media_dir / filename).write_bytes(raw)
    await db.save_media(key, branch_id or None, kind, filename, mime, size,
                        a["tg"].id)
    await db.audit(a["tg"].id, "media.upload", "media", None,
                   {"kind": kind, "size": size})
    return _ok({"key": key, "url": f"/api/media/{key}", "kind": kind})


async def admin_settings_get(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    await require_admin(request)
    branch_id = _branch_scoped(request)
    return _ok({"settings": await db.list_settings(branch_id)})


async def admin_settings_update(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    a = await require_admin(request)
    body = await request.json()
    branch_id = int(body.get("branch_id") or 0)
    key = (body.get("key") or "").strip()
    if not key:
        return _err("key required")
    value = body.get("value")
    if not isinstance(value, dict):
        value = {"value": value}
    await db.set_setting(key, value, branch_id)
    await db.audit(a["tg"].id, "settings.update", "setting", None, {"key": key})
    return _ok()


async def admin_couriers_list(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    await require_admin(request)
    branch_raw = request.query.get("branch_id", "")
    branch_id = int(branch_raw) if branch_raw.isdigit() else None
    return _ok({"couriers": await db.list_couriers(branch_id)})


async def admin_courier_create(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    a = await require_admin(request)
    body = await request.json()
    tg_id = int(body.get("tg_id") or 0)
    if not tg_id:
        return _err("tg_id required (courier's Telegram numeric id)")
    courier = await db.register_courier(
        tg_id, transport=body.get("transport", "foot"),
        branch_id=body.get("branch_id"),
    )
    await db.audit(a["tg"].id, "courier.create", "courier", courier["id"])
    bot: Bot = request.app[app_keys.BOT_KEY]
    try:
        await bot.send_message(
            tg_id,
            f"🛵 Siz kuryer sifatida qo'shildingiz!\n"
            f"PIN: <code>{courier['pin_code']}</code>\n"
            "Navbatga chiqish: /online",
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        pass
    return _ok({"courier": courier})


async def admin_courier_update(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    a = await require_admin(request)
    courier_id = int(request.match_info["courier_id"])
    body = await request.json()
    fields = {k: v for k, v in body.items()
              if k in {"status", "transport", "branch_id", "rating", "pin_code"}}
    await db.update_courier(courier_id, fields)
    await db.audit(a["tg"].id, "courier.update", "courier", courier_id)
    return _ok({"courier": await db.get_courier(courier_id)})


async def admin_orders_list(request: web.Request) -> web.Response:
    """Live kitchen board: active orders for a branch."""
    db: Database = request.app[app_keys.DB_KEY]
    await require_admin(request)
    branch_id = _branch_scoped(request)
    rows = await db.list_orders(branch_id=branch_id,
                                statuses=ACTIVE_STATUSES[:-1], limit=60)
    return _ok({"orders": [await order_view(db, o) for o in rows]})


async def admin_order_status(request: web.Request) -> web.Response:
    """Admin/kitchen moves an order along: accepted -> cooking -> ready."""
    db: Database = request.app[app_keys.DB_KEY]
    bot: Bot = request.app[app_keys.BOT_KEY]
    a = await require_admin(request)
    order = await db.get_order(int(request.match_info["order_id"]))
    if order is None:
        return _err("order not found", 404)
    try:
        body = await request.json()
    except Exception:
        return _err("invalid JSON body")
    status = body.get("status")
    if status not in {"accepted", "cooking", "ready", "cancelled"}:
        return _err("status must be accepted|cooking|ready|cancelled")
    if order["status"] in {"delivered", "cancelled"}:
        return _err(f"order already {order['status']}", 409)
    fields: dict[str, Any] = {"status": status}
    if status == "cancelled":
        fields["cancelled_at"] = time.time()
        fields["cancel_reason"] = "admin"
        if order["courier_id"]:
            courier = await db.get_courier(order["courier_id"])
            if courier:
                await db.set_courier_status(courier["id"], "online")
    if status == "accepted":
        fields["accepted_at"] = time.time()
    await db.update_order(order["id"], fields)
    updated = await db.get_order(order["id"])
    assert updated is not None
    asyncio.create_task(fan_out_order_event(db, f"order.{status}", updated))
    client = await db.get_user(int(order["user_id"]))
    if client and status in {"accepted", "cooking", "ready", "cancelled"}:
        labels = {"accepted": "👩\u200d🍳 Qabul qilindi", "cooking": "🔥 Tayyorlanmoqda",
                  "ready": "🛎 Tayyor!", "cancelled": "❌ Bekor qilindi"}
        try:
            await bot.send_message(client["id"],
                                   f"{labels[status]} \u2014 buyurtma #{order['public_code']}")
        except (TelegramForbiddenError, TelegramBadRequest):
            pass
    return _ok({"order": await order_view(db, updated)})


async def admin_integrations_list(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    await require_admin(request)
    branch_raw = request.query.get("branch_id", "")
    branch_id = int(branch_raw) if branch_raw.isdigit() else None
    return _ok({"integrations": await db.list_integrations(branch_id)})


async def admin_integration_create(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    a = await require_admin(request)
    body = await request.json()
    if not body.get("system") or not body.get("direction"):
        return _err("system and direction required")
    if body["direction"] not in {"export", "import", "both"}:
        return _err("direction must be export|import|both")
    body.setdefault("branch_id", _branch_scoped(request))
    body.setdefault("secret", uuid.uuid4().hex)
    if not isinstance(body.get("mapping", {}), dict):
        return _err("mapping must be an object")
    iid = await db.create_integration(body)
    await db.audit(a["tg"].id, "integration.create", "integration", iid)
    return _ok({"integration": await db.get_integration(iid)})


async def admin_integration_update(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    a = await require_admin(request)
    iid = int(request.match_info["integration_id"])
    body = await request.json()
    await db.update_integration(iid, body)
    await db.audit(a["tg"].id, "integration.update", "integration", iid)
    return _ok({"integration": await db.get_integration(iid)})


async def admin_integration_delete(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    a = await require_admin(request)
    iid = int(request.match_info["integration_id"])
    await db.delete_integration(iid)
    await db.audit(a["tg"].id, "integration.delete", "integration", iid)
    return _ok()


async def admin_integration_logs(request: web.Request) -> web.Response:
    db: Database = request.app[app_keys.DB_KEY]
    await require_admin(request)
    iid = int(request.match_info["integration_id"])
    return _ok({"logs": await db.list_integration_logs(iid)})


async def admin_integration_export(request: web.Request) -> web.Response:
    """On-demand JSON export shaped by the integration's mapping (for 1C pull)."""
    db: Database = request.app[app_keys.DB_KEY]
    await require_admin(request)
    iid = int(request.match_info["integration_id"])
    integ = await db.get_integration(iid)
    if integ is None:
        return _err("integration not found", 404)
    branch_raw = request.query.get("branch_id", "")
    branch_id = int(branch_raw) if branch_raw.isdigit() else integ.get("branch_id")
    orders = await db.list_orders(branch_id=branch_id, limit=100)
    mapping = json.loads(integ["mapping"] or "{}")
    docs = []
    for o in orders:
        branch = await db.get_branch(o["branch_id"])
        user = await db.get_user(int(o["user_id"]))
        docs.append(build_export_document(o, mapping, branch, user))
    return _ok({"integration": integ["name"], "count": len(docs),
                "documents": docs})


# ------------------------------------------------------------- integration --
async def integration_import(request: web.Request) -> web.Response:
    """Inbound 1C/1UZ push: dishes/prices/stock documents."""
    db: Database = request.app[app_keys.DB_KEY]
    try:
        document = await request.json()
    except Exception:
        return _err("invalid JSON body")
    integration_id = int(request.query.get("integration_id", 0) or 0)
    integration = None
    if integration_id:
        integration = await db.get_integration(integration_id)
    if integration is None:
        actives = await db.list_integrations(only_active=True)
        imports = [i for i in actives if i["direction"] in {"import", "both"}]
        integration = imports[0] if imports else None
    if integration is None:
        return _err("no active import integration configured", 404)
    try:
        result = await apply_import(db, integration, document)
    except ImportError_ as exc:
        await db.log_integration(integration["id"], "import", "error",
                                 json.dumps(document)[:8000], str(exc))
        return _err(str(exc), 422)
    except Exception as exc:
        await db.log_integration(integration["id"], "import", "error",
                                 json.dumps(document)[:8000], str(exc))
        return _err(f"import failed: {exc}", 500)
    await db.log_integration(integration["id"], "import", "ok",
                             json.dumps(document, ensure_ascii=False)[:8000],
                             json.dumps(result, ensure_ascii=False)[:8000])
    return _ok(result)


async def integration_orders_feed(request: web.Request) -> web.Response:
    """Polling fallback for accounting systems that cannot receive webhooks."""
    db: Database = request.app[app_keys.DB_KEY]
    since = float(request.query.get("since_ts", 0) or 0)
    branch_raw = request.query.get("branch_id", "")
    branch_id = int(branch_raw) if branch_raw.isdigit() else None
    orders = await db.list_orders(branch_id=branch_id, limit=200)
    fresh = [o for o in orders if o["created_at"] >= since]
    return _json({"count": len(fresh), "orders": fresh})


# ------------------------------------------------------------- app factory --
def build_app(db: Database, bot: Bot, dp: Any, dispatch: DispatchService) -> web.Application:
    app = web.Application(middlewares=[error_middleware, integration_auth_middleware],
                          client_max_size=MAX_UPLOAD + 1024 * 1024)
    app[app_keys.DB_KEY] = db
    app[app_keys.BOT_KEY] = bot
    app[app_keys.DISPATCHER_KEY] = dp
    app[app_keys.DISPATCH_KEY] = dispatch

    r = app.router
    # public / shared
    r.add_get("/api/health", health)
    r.add_get("/api/bootstrap", bootstrap)
    r.add_get("/api/media/{key}", media)
    r.add_get("/api/catalog", catalog)
    # client orders
    r.add_get("/api/orders/my", my_orders)
    r.add_post("/api/orders", create_order)
    r.add_get("/api/orders/{order_id}", order_detail)
    r.add_post("/api/orders/{order_id}/cancel", cancel_order)
    # courier
    r.add_get("/api/courier/board", courier_board)
    r.add_post("/api/courier/status", courier_status)
    r.add_post("/api/courier/claim/{order_id}", courier_claim)
    r.add_post("/api/courier/deliver/{order_id}", courier_deliver)
    r.add_post("/api/courier/pin", courier_pin)
    # admin
    r.add_get("/api/admin/stats", admin_stats)
    r.add_get("/api/admin/orders", admin_orders_list)
    r.add_post("/api/admin/orders/{order_id}/status", admin_order_status)
    r.add_get("/api/admin/branches", admin_branches_list)
    r.add_post("/api/admin/branches", admin_branch_create)
    r.add_post("/api/admin/branches/{branch_id}", admin_branch_update)
    r.add_post("/api/admin/branches/{branch_id}/delete", admin_branch_delete)
    r.add_post("/api/admin/categories", admin_category_create)
    r.add_post("/api/admin/categories/{category_id}", admin_category_update)
    r.add_post("/api/admin/categories/{category_id}/delete", admin_category_delete)
    r.add_post("/api/admin/dishes", admin_dish_create)
    r.add_post("/api/admin/dishes/{dish_id}", admin_dish_update)
    r.add_post("/api/admin/dishes/{dish_id}/delete", admin_dish_delete)
    r.add_get("/api/admin/delivery-rules", admin_delivery_rules_list)
    r.add_post("/api/admin/delivery-rules", admin_delivery_rule_create)
    r.add_post("/api/admin/delivery-rules/{rule_id}", admin_delivery_rule_update)
    r.add_post("/api/admin/delivery-rules/{rule_id}/delete", admin_delivery_rule_delete)
    r.add_get("/api/admin/branding", admin_branding_get)
    r.add_post("/api/admin/branding", admin_branding_update)
    r.add_post("/api/admin/media", admin_media_upload)
    r.add_get("/api/admin/settings", admin_settings_get)
    r.add_post("/api/admin/settings", admin_settings_update)
    r.add_get("/api/admin/couriers", admin_couriers_list)
    r.add_post("/api/admin/couriers", admin_courier_create)
    r.add_post("/api/admin/couriers/{courier_id}", admin_courier_update)
    r.add_get("/api/admin/integrations", admin_integrations_list)
    r.add_post("/api/admin/integrations", admin_integration_create)
    r.add_post("/api/admin/integrations/{integration_id}", admin_integration_update)
    r.add_post("/api/admin/integrations/{integration_id}/delete", admin_integration_delete)
    r.add_get("/api/admin/integrations/{integration_id}/logs", admin_integration_logs)
    r.add_get("/api/admin/integrations/{integration_id}/export", admin_integration_export)
    # machine-to-machine
    r.add_post("/api/integration/import", integration_import)
    r.add_get("/api/integration/orders-feed", integration_orders_feed)

    # Mini App static files (index.html + app.js) — explicit allowlist only.
    webapp_dir = Path(__file__).resolve().parent / "webapp"
    if webapp_dir.exists():
        async def webapp_index(_request: web.Request) -> web.Response:
            return web.FileResponse(webapp_dir / "index.html",
                                    headers={"Cache-Control": "no-cache"})

        async def webapp_js(request: web.Request) -> web.Response:
            asset = request.match_info.get("asset", "")
            if asset != "app.js":
                raise web.HTTPNotFound(text="asset not found")
            return web.FileResponse(webapp_dir / "app.js",
                                    headers={"Cache-Control": "no-cache"})

        r.add_get("/webapp", webapp_index)
        r.add_get("/webapp/", webapp_index)
        r.add_get("/webapp/{asset}", webapp_js)

    return app
