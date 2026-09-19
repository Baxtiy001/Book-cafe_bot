"""Outbound webhook service powering 1C/1UZ accounting exports.

Every configured integration with direction ``export`` or ``both`` receives a
signed JSON webhook on order lifecycle events. Mappings configured from the
admin panel transform the internal order schema into whatever the accounting
system expects — no server-side code changes required.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any

import aiohttp

from config import settings
from database import Database

log = logging.getLogger("integrations")


def map_order_payload(order: dict[str, Any], mapping: dict[str, str],
                      branch: dict[str, Any] | None = None,
                      user: dict[str, Any] | None = None) -> dict[str, Any]:
    """Transform an internal order into the accounting system's shape.

    ``mapping`` maps target-field -> source-field using dotted paths, e.g.
    ``{"DocNumber": "public_code", "Customer": "user.phone"}``.
    Source paths: any order field, ``user.*``, ``branch.*``,
    ``items`` (verbatim array) and ``meta.now``.
    """
    src: dict[str, Any] = {
        **{k: v for k, v in order.items() if k != "items"},
        "user": user or {},
        "branch": branch or {},
        "meta": {"now": int(time.time())},
    }

    def lookup(path: str) -> Any:
        node: Any = src
        for part in path.split("."):
            if isinstance(node, dict):
                node = node.get(part)
            else:
                return None
        return node

    if not mapping:
        return src
    return {target: lookup(path) for target, path in mapping.items()}


def sign_payload(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def fan_out_order_event(db: Database, event: str,
                              order: dict[str, Any]) -> None:
    """Deliver an order event to all active export integrations."""
    branch = await db.get_branch(order["branch_id"]) or {}
    user = await db.get_user(int(order["user_id"])) or {}
    integrations = await db.list_integrations(branch_id=order["branch_id"],
                                              only_active=True)
    global_rows = await db.list_integrations(branch_id=0, only_active=True)
    for integ in [*global_rows, *integrations]:
        if integ["direction"] not in {"export", "both"}:
            continue
        if not integ["webhook_url"]:
            continue
        payload = map_order_payload(
            order, json.loads(integ["mapping"] or "{}"), branch, user
        )
        payload["event"] = event
        body = json.dumps(payload, ensure_ascii=False, default=str).encode()
        signature = sign_payload(integ["secret"] or "", body)
        status = "ok"
        response_text = ""
        try:
            timeout = aiohttp.ClientTimeout(total=settings.INTEGRATION_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    integ["webhook_url"],
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-BookCafe-Signature": signature,
                        "X-BookCafe-Event": event,
                    },
                ) as resp:
                    response_text = (await resp.text())[:1000]
                    if resp.status >= 400:
                        status = "error"
        except Exception as exc:  # network errors must never break the order flow
            status = "error"
            response_text = str(exc)[:500]
            log.warning("Webhook %s failed: %s", integ["webhook_url"], exc)
        await db.log_integration(integ["id"], "export", status,
                                 body.decode()[:8000], response_text)


def build_export_document(order: dict[str, Any], mapping: dict[str, str] | None = None,
                          branch: dict[str, Any] | None = None,
                          user: dict[str, Any] | None = None) -> dict[str, Any]:
    """Synchronous mapper used by the on-demand export endpoint."""
    if mapping:
        mapped = map_order_payload(order, mapping, branch, user)
        mapped["event"] = "order.export.manual"
        return mapped
    return map_order_payload(order, {}, branch, user)
