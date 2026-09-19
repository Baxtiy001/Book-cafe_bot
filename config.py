"""Central, zero-hardcoding configuration.

Every runtime knob is read from the environment (.env) so the same codebase can
run dev, staging and production instances without touching source code.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

def _env_str(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)).strip())
    except (TypeError, ValueError):
        return default

def _env_bool(key: str, default: bool = False) -> bool:
    return os.getenv(key, str(default)).strip().lower() in {"1", "true", "yes", "on"}

def _env_list_ids(key: str) -> list[int]:
    raw = os.getenv(key, "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [int(x) for x in parsed]
    except json.JSONDecodeError:
        pass
    out: list[int] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            out.append(int(chunk))
    return out


class Config:
    """Immutable-ish settings container."""

    BOT_TOKEN: str = _env_str("BOT_TOKEN")
    BOT_USERNAME: str = _env_str("BOT_USERNAME")

    # Comma separated telegram ids, e.g. "6123456789, 555444333".
    ADMIN_IDS: list[int] = _env_list_ids("ADMIN_IDS")

    # Telegram profile ids allowed to open the courier board.
    COURIER_PIN: str = _env_str("COURIER_PIN", "0000")

    WEBHOOK_SECRET: str = _env_str("WEBHOOK_SECRET", "bookcafe-webhook")
    BASE_URL: str = _env_str("BASE_URL", "http://127.0.0.1:8080").rstrip("/")
    WEBAPP_URL: str = _env_str("WEBAPP_URL", f"{BASE_URL.rstrip('/')}/webapp/").rstrip("/")
    if not WEBAPP_URL:
        WEBAPP_URL = f"{BASE_URL}/webapp"

    HOST: str = _env_str("API_HOST", "0.0.0.0")
    PORT: int = _env_int("API_PORT", 8080)

    DB_PATH: str = _env_str("DB_PATH", str(BASE_DIR / "data" / "bookcafe.db"))
    MEDIA_DIR: str = _env_str("MEDIA_DIR", str(BASE_DIR / "data" / "media"))

    USE_WEBHOOK: bool = _env_bool("USE_WEBHOOK", True)
    SECRET_PATH: str = _env_str("SECRET_PATH", "telegram-webhook")

    # Integration backends (1C / 1UZ) push into these endpoints.
    INTEGRATION_TOKEN: str = _env_str("INTEGRATION_TOKEN", "")
    INTEGRATION_TIMEOUT: int = _env_int("INTEGRATION_TIMEOUT", 15)

    DEFAULT_LANGUAGE: str = _env_str("DEFAULT_LANG", "uz")
    SUPPORTED_LANGS: tuple[str, ...] = ("uz", "ru", "en")

    SUPPORT_URL: str = _env_str("SUPPORT_URL", "https://t.me/bookcafe_support")

    def validate(self) -> None:
        if not self.BOT_TOKEN:
            raise RuntimeError("BOT_TOKEN is missing — put it into .env (see .env.example)")

    def public_base_url(self) -> str:
        return self.BASE_URL

    def as_dict(self) -> dict[str, Any]:
        return {
            "base_url": self.BASE_URL,
            "webapp_url": self.WEBAPP_URL,
            "admin_count": len(self.ADMIN_IDS),
            "use_webhook": self.USE_WEBHOOK,
            "db_path": self.DB_PATH,
            "media_dir": self.MEDIA_DIR,
        }


settings = Config()
MEDIA_ROOT = Path(settings.MEDIA_DIR)
DB_FILE = Path(settings.DB_PATH)
