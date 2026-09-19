"""One-off check: /webapp/ and /webapp/app.js are served correctly."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["BOT_TOKEN"] = "123456:TEST_TOKEN_smoke"
os.environ["ADMIN_IDS"] = "111"
os.environ["DB_PATH"] = str(Path(__file__).resolve().parents[1] / "data" / "smoke.db")
os.environ["USE_WEBHOOK"] = "false"
os.environ["INTEGRATION_TOKEN"] = "tok"

import aiohttp  # noqa: E402
from aiohttp import web as aioweb  # noqa: E402

from api_server import build_app  # noqa: E402
from database import Database  # noqa: E402
from services.dispatch import DispatchService  # noqa: E402


async def main() -> int:
    db = Database(os.environ["DB_PATH"])
    await db.connect()

    class FakeBot:
        token = "x"

        async def send_message(self, *a, **k):
            return None

    app = build_app(db, FakeBot(), None, DispatchService(db))
    runner = aioweb.AppRunner(app)
    await runner.setup()
    site = aioweb.TCPSite(runner, "127.0.0.1", 8098)
    await site.start()
    ok = True
    try:
        async with aiohttp.ClientSession("http://127.0.0.1:8098") as http:
            r = await http.get("/webapp/")
            html = await r.text()
            ok &= r.status == 200 and "telegram-web-app.js" in html and "app.js" in html
            print(f"{'✅' if ok else '❌'} /webapp/ -> {r.status}, html {len(html)} bytes")

            r = await http.get("/webapp/app.js")
            js = await r.text()
            js_ok = r.status == 200 and "renderMenu" in js and "API.post" in js
            ok &= js_ok
            print(f"{'✅' if js_ok else '❌'} /webapp/app.js -> {r.status}, js {len(js)} bytes")

            r = await http.get("/webapp/secret.txt")
            blocked = r.status == 404
            ok &= blocked
            print(f"{'✅' if blocked else '❌'} unknown asset blocked -> {r.status}")
    finally:
        await runner.cleanup()
        await db.close()
    Path(os.environ["DB_PATH"]).unlink(missing_ok=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
