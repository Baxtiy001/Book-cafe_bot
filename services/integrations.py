"""Import-side integration engine (1C/1UZ -> Book Cafe).

Accounting systems POST structured JSON to the import endpoint; the mapping
document defines where each field lives, so a 1C integration specialist can
connect their system from the admin panel with zero code changes.
"""
from __future__ import annotations

import json
from typing import Any

from database import Database


class ImportError_(Exception):
    pass


def _dig(data: dict[str, Any], dotted: str) -> Any:
    node: Any = data
    for part in dotted.split("."):
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return node


async def apply_import(db: Database, integration: dict[str, Any],
                       document: dict[str, Any]) -> dict[str, Any]:
    """Apply an inbound document according to the integration mapping.

    Supported documents (chosen by ``document["type"]`` or mapping ``doctype``):

    * ``dishes``  — upsert dishes by external code (title/price/category/stock)
    * ``prices``  — update prices only
    * ``stock``   — toggle availability (is_available)
    """
    mapping = json.loads(integration["mapping"] or "{}")
    doctype = document.get("type") or mapping.get("doctype") or "dishes"
    branch_id = integration.get("branch_id") or 0

    if branch_id == 0:
        raise ImportError_("Integration has no branch bound — bind one in the admin panel")

    items = _dig(document, mapping.get("items_path", "items")) or []
    if not isinstance(items, list):
        raise ImportError_("items is not a list")

    applied = 0
    errors: list[str] = []

    if doctype in {"dishes", "prices", "stock"}:
        category_id = mapping.get("category_id")
        for idx, raw in enumerate(items):
            try:
                title = str(_dig(raw, mapping.get("title_path", "title")) or "").strip()
                if not title:
                    raise ImportError_("missing title")
                price = _dig(raw, mapping.get("price_path", "price"))
                available = _dig(raw, mapping.get("available_path", "available"))
                ext_code = str(_dig(raw, mapping.get("code_path", "code")) or f"ext-{idx}")

                existing = await _find_by_ext(db, branch_id, ext_code)
                if doctype == "stock":
                    if existing:
                        await db.update_dish(existing["id"], {
                            "is_available": 1 if available in (1, True, "1", "true") else 0,
                        })
                        applied += 1
                    continue

                if existing:
                    fields: dict[str, Any] = {}
                    if price is not None:
                        fields["price"] = int(price)
                    if doctype == "dishes" and title:
                        fields["title"] = title
                    if available is not None:
                        fields["is_available"] = 1 if available in (1, True, "1", "true") else 0
                    await db.update_dish(existing["id"], fields)
                else:
                    if doctype == "prices":
                        continue
                    cat = category_id or await _ensure_import_category(db, branch_id)
                    created = await db.create_dish({
                        "branch_id": branch_id,
                        "category_id": cat,
                        "title": title,
                        "price": int(price or 0),
                        "is_available": 1 if available in (1, True, "1", "true") else 1,
                    })
                    await set_external_link(db, branch_id, ext_code, int(created["id"]))
                applied += 1
            except Exception as exc:
                errors.append(f"item[{idx}]: {exc}")

    return {"type": doctype, "applied": applied, "errors": errors}


async def _find_by_ext(db: Database, branch_id: int, ext_code: str) -> dict[str, Any] | None:
    """Find dish by external code via settings ledger (no schema change)."""
    setting = await db.get_setting(f"ext_code:{branch_id}:{ext_code}")
    dish_id = setting.get("dish_id")
    if not dish_id:
        return None
    return await db.get_dish(int(dish_id))


async def _ensure_import_category(db: Database, branch_id: int) -> int:
    cats = await db.list_categories(branch_id)
    for c in cats:
        if c["title"].lower() in {"import", "1c", "external"}:
            return int(c["id"])
    created = await db.create_category(branch_id, "Import", emoji="📥")
    return int(created["id"])


async def set_external_link(db: Database, branch_id: int, ext_code: str,
                            dish_id: int) -> None:
    """Persist external-code -> dish mapping inside the settings ledger."""
    await db.set_setting(f"ext_code:{branch_id}:{ext_code}",
                         {"dish_id": dish_id}, 0)
