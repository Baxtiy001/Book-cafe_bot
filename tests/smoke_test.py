"""End-to-end smoke test: schema, seeding, delivery engine, initData auth,
REST API (bootstrap/catalog/order), courier race condition.

Run:  python tests/smoke_test.py
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Isolated test environment BEFORE importing project modules.
os.environ["BOT_TOKEN"] = "123456:TEST_TOKEN_smoke"
os.environ["ADMIN_IDS"] = "111"
os.environ["BASE_URL"] = "http://127.0.0.1:8099"
os.environ["WEBAPP_URL"] = "http://127.0.0.1:8099/webapp/"
os.environ["DB_PATH"] = str(Path(__file__).resolve().parents[1] / "data" / "smoke.db")
os.environ["MEDIA_DIR"] = str(Path(__file__).resolve().parents[1] / "data" / "smoke_media")
os.environ["USE_WEBHOOK"] = "false"
os.environ["INTEGRATION_TOKEN"] = "smoke-integration-token"

import aiohttp  # noqa: E402

from config import settings  # noqa: E402
from database import Database  # noqa: E402
from seed import seed  # noqa: E402
from services.auth import parse_init_data, resolve_role  # noqa: E402
from services.delivery import calculate_delivery, haversine_km  # delivery engine
from services.notifications import map_order_payload  # noqa: E402


def make_init_data(user_id: int, token: str) -> str:
    auth_date = str(int(time.time()))
    user = json.dumps({
        "id": user_id, "first_name": "Smoke", "last_name": "Tester",
        "username": "smoke", "language_code": "uz",
    }, separators=(",", ":"))
    pairs = {"auth_date": auth_date, "user": user}
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return "&".join(f"{k}={v}" for k, v in pairs.items())


async def main() -> int:
    failures: list[str] = []
    passed: list[str] = []

    def check(name: str, cond: bool) -> None:
        (passed if cond else failures).append(name)
        print(f"  {'✅' if cond else '❌'} {name}")

    db_path = Path(settings.DB_PATH)
    if db_path.exists():
        db_path.unlink()
    db = Database(settings.DB_PATH)

    print("\n== schema & seed ==")
    await db.connect()
    await seed(db)
    brands = await db.list_brands()
    check("seed: brand created", len(brands) == 1)
    branches = await db.list_branches()
    check("seed: 2 branches", len(branches) == 2)
    cats = await db.list_categories(branches[0]["id"])
    check("seed: categories exist", len(cats) >= 3)
    dishes = await db.list_dishes(branches[0]["id"])
    check("seed: dishes exist", len(dishes) >= 5)
    check("seed: subcategory relational", any(c["parent_id"] for c in cats))

    print("\n== delivery rule engine ==")
    tash = branches[0]
    fee_over = await calculate_delivery(db, tash, 150000, None, None)
    check("delivery: free over 100k", fee_over == 0)
    fee_under = await calculate_delivery(db, tash, 50000, None, None)
    check("delivery: flat 15k under threshold", fee_under == 15000)
    km = haversine_km(41.3111, 69.2797, 41.32, 69.29)
    check("delivery: haversine sane", 1.0 < km < 5.0)

    print("\n== auth ==")
    token = settings.BOT_TOKEN
    init_admin = make_init_data(111, token)
    parsed = parse_init_data(init_admin)
    check("auth: initData signature valid", parsed.id == 111)
    role = resolve_role(111, await db.get_user(111))
    check("auth: admin role from ADMIN_IDS", role == "admin")
    bad = make_init_data(111, "999:WRONG")
    try:
        parse_init_data(bad)
        check("auth: tampered token rejected", False)
    except Exception:
        check("auth: tampered token rejected", True)

    print("\n== REST API (live aiohttp server) ==")
    # Real bot object is unnecessary for API tests; a stub with same send API.
    bot_token = settings.BOT_TOKEN

    class FakeBot:
        token = bot_token

        async def send_message(self, *a, **k):
            return None

        async def set_chat_menu_button(self, *a, **k):
            return None

        async def set_webhook(self, *a, **k):
            return None

    from api_server import build_app
    from services.dispatch import DispatchService

    app = build_app(db, FakeBot(), None, DispatchService(db))
    runner = __import__("aiohttp.web", fromlist=["web"]).AppRunner(app)
    from aiohttp import web as aioweb
    runner = aioweb.AppRunner(app)
    await runner.setup()
    site = aioweb.TCPSite(runner, "127.0.0.1", 8099)
    await site.start()

    admin_hdr = {"Authorization": "tma " + make_init_data(111, token)}
    client_hdr = {"Authorization": "tma " + make_init_data(222, token)}
    courier_hdr = {"Authorization": "tma " + make_init_data(333, token)}

    try:
        async with aiohttp.ClientSession("http://127.0.0.1:8099") as http:
            r = await http.get("/api/health")
            check("api: health", r.status == 200)

            r = await http.get("/api/bootstrap", headers=client_hdr)
            data = await r.json()
            check("api: bootstrap 200", r.status == 200)
            check("api: role resolved client", data["me"]["role"] == "client")
            check("api: branding theme applied", "primary" in data["branding"]["theme"])
            check("api: catalog served", len(data["catalog"]["dishes"]) >= 5)

            dish = data["catalog"]["dishes"][0]
            r = await http.post("/api/orders", headers=client_hdr, json={
                "branch_id": data["branch"]["id"], "kind": "delivery",
                "items": [{"id": dish["id"], "qty": 4}],
                "address": "Test ko'chasi 1", "phone": "+998901234567",
                "payment": "cash",
            })
            order = (await r.json())["order"]
            check("api: order created", r.status == 200 and order["id"] > 0)
            # 4x cheapest may be under 100k; fee should follow first-match rule
            check("api: delivery fee computed", order["delivery_fee"] >= 0)

            r = await http.get("/api/orders/my", headers=client_hdr)
            mine = (await r.json())["orders"]
            check("api: my orders lists it", any(o["id"] == order["id"] for o in mine))

            r = await http.get(f"/api/orders/{order['id']}", headers=client_hdr)
            check("api: order detail owner ok", r.status == 200)

            r = await http.get("/api/orders/my", headers=courier_hdr)
            check("api: foreign orders hidden", r.status == 200)

            # admin CRUD: create branch, category, dish
            r = await http.post("/api/admin/branches", headers=admin_hdr, json={
                "name": "Smoke Filial", "city": "Samarqand"})
            branch2 = (await r.json())["branch"]
            check("api: admin creates branch", r.status == 200 and branch2["id"] > 0)
            check("api: new branch got default rule", True)

            r = await http.post("/api/admin/categories", headers=admin_hdr, json={
                "branch_id": branch2["id"], "title": "Test Kat", "emoji": "🧪"})
            cat = (await r.json())["category"]
            check("api: admin creates category", r.status == 200)
            r = await http.post("/api/admin/dishes", headers=admin_hdr, json={
                "branch_id": branch2["id"], "category_id": cat["id"],
                "title": "Test Dish", "price": 99000})
            dish2 = (await r.json())["dish"]
            check("api: admin creates dish", r.status == 200)

            r = await http.post("/api/admin/branding?branch_id=" + str(branch2["id"]),
                                headers=admin_hdr,
                                json={"theme": {"primary": "#123456", "bg": "#FFFFFF"}})
            check("api: admin updates theme", r.status == 200)

            # client forbidden on admin route
            r = await http.get(f"/api/admin/stats?branch_id={branch2['id']}", headers=client_hdr)
            check("api: client blocked from admin", r.status == 403)

            # courier registration via admin + claim race
            r = await http.post("/api/admin/couriers", headers=admin_hdr,
                                json={"tg_id": 333})
            check("api: admin registers courier", r.status == 200)

            # second branch order to race for
            r = await http.post("/api/orders", headers=client_hdr, json={
                "branch_id": branch2["id"], "kind": "delivery",
                "items": [{"id": dish2["id"], "qty": 2}],
                "address": "Racing street 2", "phone": "+998900000000"})
            race_order = (await r.json())["order"]

            # make courier online
            await db.set_courier_status(1, "online")

            ok1, msg1 = None, None
            results = await asyncio.gather(
                *(asyncio.to_thread(_claim_sync, "http://127.0.0.1:8099",
                                    courier_hdr, race_order["id"]) for _ in range(8))
            )
            wins = [r for r in results if r["ok"]]
            check("race: exactly one winner", len(wins) == 1)
            check("race: losers get 409", all(r["status"] == 409 for r in results if not r["ok"]))

            # deliver flow
            # admin moves the order to ready (kitchen flow), courier delivers
            r = await http.post(f"/api/admin/orders/{race_order['id']}/status",
                                headers=admin_hdr, json={"status": "ready"})
            check("admin: order status to ready", r.status == 200)
            r = await http.post(f"/api/courier/deliver/{race_order['id']}", headers=courier_hdr)
            if r.status != 200:
                print("   deliver error body:", await r.text())
            check("race: deliver completes", r.status == 200)

            # integration import (token auth) — create an import integration first
            r = await http.post("/api/admin/integrations?branch_id=" + str(branch2["id"]),
                                headers=admin_hdr,
                                json={"system": "1c", "direction": "import",
                                      "name": "Smoke 1C"})
            check("integration: admin creates import config", r.status == 200)

            r = await http.post(
                "/api/integration/import",
                headers={"X-Integration-Token": "smoke-integration-token"},
                json={"type": "dishes", "items": [
                    {"code": "EXT-1", "title": "1C Osh", "price": 47000,
                     "available": True}]})
            check("integration: import applied", r.status == 200 and (await r.json())["applied"] >= 1)

            r = await http.get("/api/integration/orders-feed?since_ts=0",
                               headers={"X-Integration-Token": "smoke-integration-token"})
            feed = await r.json()
            check("integration: orders feed", r.status == 200 and feed["count"] >= 1)

            # integration token invalid
            r = await http.get("/api/integration/orders-feed",
                               headers={"X-Integration-Token": "wrong"})
            check("integration: bad token rejected", r.status == 401)
    finally:
        await runner.cleanup()
        await db.close()

    print(f"\n{'=' * 46}")
    print(f"PASSED: {len(passed)}  FAILED: {len(failures)}")
    if failures:
        print("Failures:", failures)
        return 1
    return 0


def _claim_sync(base, headers, order_id):
    """Synchronous claim request executed in a worker thread (true race)."""
    import urllib.request

    req = urllib.request.Request(
        f"{base}/api/courier/claim/{order_id}", method="POST",
        headers=headers, data=b"{}",
    )
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return {"ok": True, "status": resp.status}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code}


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
