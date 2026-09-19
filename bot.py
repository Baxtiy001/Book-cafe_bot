"""Single asyncio loop hosting aiogram (polling or webhook) + aiohttp API.

Run:  python bot.py
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (
    MenuButtonWebApp,
    WebAppInfo,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from api_server import build_app
from config import settings
from database import db as database
from handlers.start import router as start_router
from services.dispatch import DispatchService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logging.getLogger("aiogram.event").setLevel(logging.WARNING)
log = logging.getLogger("bot")


async def on_startup(bot: Bot, database_ref, dispatch: DispatchService) -> None:
    await database_ref.connect()
    # Keep the chat clutter-free: the menu button opens the Mini App directly.
    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(
            text="📱 Book Cafe",
            web_app=WebAppInfo(url=settings.WEBAPP_URL),
        )
    )
    if settings.USE_WEBHOOK:
        await bot.set_webhook(
            f"{settings.BASE_URL}/telegram-webhook/{settings.SECRET_PATH}",
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query"],
        )
        log.info("Webhook set: %s/telegram-webhook/%s", settings.BASE_URL,
                 settings.SECRET_PATH)
    else:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("Webhook deleted — long polling mode")


async def on_shutdown(bot: Bot, database_ref) -> None:
    if settings.USE_WEBHOOK:
        with contextlib.suppress(Exception):
            await bot.delete_webhook(drop_pending_updates=False)
    await database_ref.close()


async def main() -> int:
    settings.validate()
    await database.connect()
    dispatch = DispatchService(database)

    bot = Bot(token=settings.BOT_TOKEN,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(start_router)
    dp["db"] = database          # workflow data -> injected into handlers
    dp["dispatch"] = dispatch

    app = build_app(database, bot, dp, dispatch)

    # Register ALL routes BEFORE the runner freezes the router.
    if settings.USE_WEBHOOK:
        webhook_handler = SimpleRequestHandler(
            dispatcher=dp, bot=bot, secret_token=settings.WEBHOOK_SECRET,
        )
        webhook_handler.register(app, path=f"/telegram-webhook/{settings.SECRET_PATH}")
        setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=settings.HOST, port=settings.PORT)
    await site.start()
    log.info("API listening on %s:%s", settings.HOST, settings.PORT)

    try:
        await on_startup(bot, database, dispatch)
        if settings.USE_WEBHOOK:
            await asyncio.Event().wait()  # serve forever
        else:
            await dp.start_polling(bot, handle_signals=True)
    finally:
        await on_shutdown(bot, database)
        with contextlib.suppress(Exception):
            await runner.cleanup()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except (KeyboardInterrupt, SystemExit):
        pass
