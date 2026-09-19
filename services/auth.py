"""Telegram WebApp ``initData`` validation + role resolution.

The frontend always calls the API with an ``Authorization: tma <initData>``
header. This module verifies the HMAC-SHA256 signature exactly as documented
(https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app)
and then resolves the Telegram user into a database user with role resolution:

* ADMIN_IDS from .env are always admins.
* Users mapped to the ``couriers`` table are couriers.
* Everyone else is a client.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

from config import settings


class AuthError(Exception):
    """Raised when initData is missing, tampered or expired."""


@dataclass(slots=True)
class TgUser:
    id: int
    first_name: str = ""
    last_name: str = ""
    username: str = ""
    language_code: str = ""
    photo_url: str = ""
    auth_date: int = 0

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip() or f"User {self.id}"


def parse_init_data(raw_init_data: str) -> TgUser:
    """Validate signature & freshness, return the embedded Telegram user."""
    if not raw_init_data:
        raise AuthError("initData is required")
    try:
        pairs = sorted(
            chunk.split("=", 1) for chunk in raw_init_data.split("&") if "=" in chunk
        )
        data: dict[str, str] = dict(pairs)
    except Exception as exc:  # malformed query string
        raise AuthError("initData is malformed") from exc

    received_hash = data.pop("hash", "")
    if not received_hash:
        raise AuthError("initData has no hash")

    # 1) secret = HMAC(secret_key, "WebAppData")
    secret_key = hmac.new(b"WebAppData", settings.BOT_TOKEN.encode(), hashlib.sha256).digest()
    # 2) hash = HMAC(secret, data_check_string)
    data_check_string = "\n".join(f"{k}={v}" for k, v in data.items())
    calculated = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calculated, received_hash):
        raise AuthError("initData signature mismatch")

    auth_date = int(data.get("auth_date", "0"))
    if auth_date and time.time() - auth_date > 86400:
        raise AuthError("initData expired (older than 24h)")

    raw_user = data.get("user")
    if not raw_user:
        raise AuthError("initData contains no user")
    payload: dict[str, Any] = json.loads(raw_user)
    return TgUser(
        id=int(payload["id"]),
        first_name=payload.get("first_name", ""),
        last_name=payload.get("last_name", ""),
        username=payload.get("username", ""),
        language_code=payload.get("language_code", settings.DEFAULT_LANGUAGE),
        photo_url=payload.get("photo_url", ""),
        auth_date=auth_date,
    )


def resolve_role(tg_id: int, user_row: dict[str, Any] | None) -> str:
    """Env ADMIN_IDS outrank any DB role; courier row wins over 'client'."""
    if tg_id in set(settings.ADMIN_IDS):
        return "admin"
    if user_row and user_row.get("role") == "courier":
        return "courier"
    return "client"


def extract_auth_header(header: str | None) -> str:
    if not header:
        raise AuthError("Authorization header missing")
    if header.lower().startswith("tma "):
        return header[4:].strip()
    return header.strip()
