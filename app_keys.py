"""Shared aiohttp app-singleton keys and helpers.

Because aiogram and aiohttp run inside one asyncio loop, they share state via
the aiohttp ``AppKey`` pattern to stay fully typed and avoid global mutable
module state where possible.
"""
from __future__ import annotations

from aiohttp.web import AppKey

from database import Database
from services.dispatch import DispatchService

DB_KEY = AppKey("db", Database)
BOT_KEY = AppKey("bot", object)
DISPATCHER_KEY = AppKey("dispatcher", object)
DISPATCH_KEY = AppKey("dispatch", DispatchService)
CONFIG_KEY = AppKey("config", object)
