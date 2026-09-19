"""Fully relational SQLite schema for the multi-branch Book Cafe ecosystem.

Design principles
-----------------
* SQLite runs in WAL mode; every write passes through a process-wide asyncio
  lock so concurrent aiohttp/aiogram coroutines never interleave mid-transaction.
* Every domain concept (brands, branches, menu, branding, delivery rules,
  couriers, orders, integrations, audits) lives in a relational table. The
  admin dashboard mutates rows — never source code.
* Money is stored as integer sums (UZS tiyin-free), per Uzbekistan convention.
* Soft deletes (`is_active`) keep historical orders and 1C exports consistent.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Sequence

import aiosqlite

from config import settings

SCHEMA_VERSION = 3

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

-- ---------------------------------------------------------------- brands --
-- A brand is a white-label sub-ecosystem (e.g. "Book Cafe", "Coffee Lab").
CREATE TABLE IF NOT EXISTS brands (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    slug            TEXT    NOT NULL UNIQUE,
    description     TEXT    NOT NULL DEFAULT '',
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      REAL    NOT NULL,
    updated_at      REAL    NOT NULL
);

-- -------------------------------------------------------------- branches --
CREATE TABLE IF NOT EXISTS branches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id        INTEGER NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
    name            TEXT    NOT NULL,
    city            TEXT    NOT NULL DEFAULT '',
    address         TEXT    NOT NULL DEFAULT '',
    phone           TEXT    NOT NULL DEFAULT '',
    lat             REAL,
    lon             REAL,
    work_from       TEXT    NOT NULL DEFAULT '09:00',
    work_to         TEXT    NOT NULL DEFAULT '22:00',
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      REAL    NOT NULL,
    updated_at      REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_branches_brand ON branches(brand_id);

-- ------------------------------------------------------------ categories --
CREATE TABLE IF NOT EXISTS categories (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_id       INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
    parent_id       INTEGER REFERENCES categories(id) ON DELETE CASCADE,
    title           TEXT    NOT NULL,
    emoji           TEXT    NOT NULL DEFAULT '',
    position        INTEGER NOT NULL DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      REAL    NOT NULL,
    updated_at      REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_categories_branch ON categories(branch_id);
CREATE INDEX IF NOT EXISTS idx_categories_parent ON categories(parent_id);

-- ---------------------------------------------------------------- dishes --
CREATE TABLE IF NOT EXISTS dishes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_id       INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
    category_id     INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    title           TEXT    NOT NULL,
    description     TEXT    NOT NULL DEFAULT '',
    price           INTEGER NOT NULL DEFAULT 0,
    old_price       INTEGER,
    image_key       TEXT,
    is_available    INTEGER NOT NULL DEFAULT 1,
    is_hit          INTEGER NOT NULL DEFAULT 0,
    is_vegan        INTEGER NOT NULL DEFAULT 0,
    upsell_group    TEXT    NOT NULL DEFAULT '',
    position        INTEGER NOT NULL DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      REAL    NOT NULL,
    updated_at      REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dishes_branch  ON dishes(branch_id);
CREATE INDEX IF NOT EXISTS idx_dishes_category ON dishes(category_id);

-- ------------------------------------------------- branding / white-label --
-- One row per branch; JSON payload holds the whole theme token set so the
-- frontend can restyle itself without a redeploy.
CREATE TABLE IF NOT EXISTS branding (
    branch_id       INTEGER PRIMARY KEY REFERENCES branches(id) ON DELETE CASCADE,
    logo_key        TEXT,
    banner_key      TEXT,
    theme           TEXT    NOT NULL DEFAULT '{}',
    updated_at      REAL    NOT NULL
);

-- ------------------------------------------------------- delivery pricing --
-- Rule engine rows evaluated top-down; first matching rule wins.
-- rule_type: free_over | flat | per_km | percent
CREATE TABLE IF NOT EXISTS delivery_rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_id       INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
    rule_type       TEXT    NOT NULL,
    min_total       INTEGER NOT NULL DEFAULT 0,
    max_km          REAL,
    amount          INTEGER NOT NULL DEFAULT 0,
    percent         REAL    NOT NULL DEFAULT 0,
    free_over       INTEGER,
    position        INTEGER NOT NULL DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      REAL    NOT NULL,
    updated_at      REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_delivery_branch ON delivery_rules(branch_id);

-- ----------------------------------------------------------------- users --
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY,          -- telegram user id
    first_name      TEXT    NOT NULL DEFAULT '',
    last_name       TEXT    NOT NULL DEFAULT '',
    username        TEXT    NOT NULL DEFAULT '',
    phone           TEXT    NOT NULL DEFAULT '',
    language        TEXT    NOT NULL DEFAULT 'uz',
    role            TEXT    NOT NULL DEFAULT 'client',  -- client|courier|admin
    branch_id       INTEGER REFERENCES branches(id) ON DELETE SET NULL,
    is_blocked      INTEGER NOT NULL DEFAULT 0,
    created_at      REAL    NOT NULL,
    updated_at      REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);

-- User addresses for fast repeat delivery checkout.
CREATE TABLE IF NOT EXISTS addresses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title           TEXT    NOT NULL DEFAULT '',
    address         TEXT    NOT NULL,
    lat             REAL,
    lon             REAL,
    created_at      REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_addresses_user ON addresses(user_id);

-- --------------------------------------------------------------- couriers --
CREATE TABLE IF NOT EXISTS couriers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    branch_id       INTEGER REFERENCES branches(id) ON DELETE SET NULL,
    transport       TEXT    NOT NULL DEFAULT 'foot',  -- foot|bike|car
    status          TEXT    NOT NULL DEFAULT 'offline',
        -- offline | online | assigned | delivering
    pin_code        TEXT    NOT NULL DEFAULT '',
    rating          REAL    NOT NULL DEFAULT 5.0,
    total_orders    INTEGER NOT NULL DEFAULT 0,
    balance         INTEGER NOT NULL DEFAULT 0,
    created_at      REAL    NOT NULL,
    updated_at      REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_couriers_status ON couriers(status);

-- ---------------------------------------------------------------- orders --
CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    public_code     TEXT    NOT NULL UNIQUE,      -- human friendly e.g. A1B2C3
    user_id         INTEGER NOT NULL REFERENCES users(id),
    branch_id       INTEGER NOT NULL REFERENCES branches(id),
    kind            TEXT    NOT NULL DEFAULT 'delivery',  -- delivery|pickup|dinein
    status          TEXT    NOT NULL DEFAULT 'new',
        -- new | accepted | cooking | ready | courier_assigned | delivering
        -- | delivered | cancelled
    courier_id      INTEGER REFERENCES couriers(id) ON DELETE SET NULL,
    items           TEXT    NOT NULL DEFAULT '[]',  -- JSON snapshot
    items_total     INTEGER NOT NULL DEFAULT 0,
    delivery_fee    INTEGER NOT NULL DEFAULT 0,
    total           INTEGER NOT NULL DEFAULT 0,
    address         TEXT    NOT NULL DEFAULT '',
    lat             REAL,
    lon             REAL,
    phone           TEXT    NOT NULL DEFAULT '',
    comment         TEXT    NOT NULL DEFAULT '',
    payment         TEXT    NOT NULL DEFAULT 'cash', -- cash|card|online
    eta_minutes     INTEGER,
    accepted_at     REAL,
    delivered_at    REAL,
    cancelled_at    REAL,
    cancel_reason   TEXT    NOT NULL DEFAULT '',
    created_at      REAL    NOT NULL,
    updated_at      REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_user    ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_branch  ON orders(branch_id);
CREATE INDEX IF NOT EXISTS idx_orders_status  ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_courier ON orders(courier_id);

-- Atomic claim ledger powering the courier "first click wins" race.
CREATE TABLE IF NOT EXISTS order_claims (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    courier_id      INTEGER NOT NULL REFERENCES couriers(id) ON DELETE CASCADE,
    outcome         TEXT    NOT NULL,              -- claimed | rejected | lost
    created_at      REAL    NOT NULL,
    UNIQUE(order_id, courier_id)
);
CREATE INDEX IF NOT EXISTS idx_claims_order ON order_claims(order_id);

-- -------------------------------------------------------------- settings --
-- Free-form per-branch JSON settings (upsell text, languages, toggles...).
CREATE TABLE IF NOT EXISTS settings (
    key             TEXT    NOT NULL,
    branch_id       INTEGER NOT NULL DEFAULT 0,
    value           TEXT    NOT NULL DEFAULT '{}',
    updated_at      REAL    NOT NULL,
    PRIMARY KEY (key, branch_id)
);

-- ------------------------------------------------------------ media assets --
CREATE TABLE IF NOT EXISTS media_assets (
    key             TEXT PRIMARY KEY,             -- opaque storage key
    branch_id       INTEGER REFERENCES branches(id) ON DELETE SET NULL,
    kind            TEXT    NOT NULL DEFAULT 'image',  -- image|logo|banner|dish
    filename        TEXT    NOT NULL,
    mime            TEXT    NOT NULL DEFAULT 'image/jpeg',
    size            INTEGER NOT NULL DEFAULT 0,
    uploaded_by     INTEGER,
    created_at      REAL    NOT NULL
);

-- ------------------------------------------------- integrations (1C/1UZ) --
-- Mapping + sync jobs configured 100% from the Mini App admin panel.
CREATE TABLE IF NOT EXISTS integrations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_id       INTEGER REFERENCES branches(id) ON DELETE CASCADE,
    system          TEXT    NOT NULL,             -- 1c | 1uz | custom
    name            TEXT    NOT NULL DEFAULT '',
    direction       TEXT    NOT NULL DEFAULT 'export',  -- export|import|both
    webhook_url     TEXT    NOT NULL DEFAULT '',
    secret          TEXT    NOT NULL DEFAULT '',
    mapping         TEXT    NOT NULL DEFAULT '{}', -- JSON field map
    is_active       INTEGER NOT NULL DEFAULT 1,
    last_sync_at    REAL,
    last_status     TEXT    NOT NULL DEFAULT '',
    created_at      REAL    NOT NULL,
    updated_at      REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS integration_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    integration_id  INTEGER NOT NULL REFERENCES integrations(id) ON DELETE CASCADE,
    direction       TEXT    NOT NULL,
    status          TEXT    NOT NULL,             -- ok | error
    payload         TEXT    NOT NULL DEFAULT '',
    response        TEXT    NOT NULL DEFAULT '',
    created_at      REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ilogs_integration ON integration_logs(integration_id);

-- ------------------------------------------------------------ admin audit --
CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id        INTEGER NOT NULL,
    action          TEXT    NOT NULL,
    entity          TEXT    NOT NULL DEFAULT '',
    entity_id       INTEGER,
    details         TEXT    NOT NULL DEFAULT '{}',
    created_at      REAL    NOT NULL
);
"""


def _now() -> float:
    return time.time()


def _slugify(name: str) -> str:
    keep = [c.lower() if c.isalnum() else "-" for c in name.strip()]
    slug = "".join(keep).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or f"brand-{uuid.uuid4().hex[:6]}"


class Database:
    """Thin async facade over aiosqlite with a serialized write path."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ lifecycle --
    async def connect(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)"
        )
        await self._db.execute(
            "INSERT OR IGNORE INTO meta(k, v) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def raw(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database.connect() was not awaited")
        return self._db

    async def _write(self, sql: str, params: Sequence[Any]) -> aiosqlite.Cursor:
        async with self._lock:
            cur = await self.raw.execute(sql, params)
            await self.raw.commit()
            return cur

    # --------------------------------------------------------------- brands --
    async def create_brand(self, name: str, description: str = "") -> dict[str, Any]:
        ts = _now()
        slug = _slugify(name)
        base = slug
        n = 1
        while await self.get_brand_by_slug(slug):
            n += 1
            slug = f"{base}-{n}"
        cur = await self._write(
            "INSERT INTO brands(name, slug, description, created_at, updated_at)"
            " VALUES(?,?,?,?,?)",
            (name, slug, description, ts, ts),
        )
        return await self.get_brand(cur.lastrowid or 0)

    async def get_brand(self, brand_id: int) -> dict[str, Any] | None:
        async with self.raw.execute("SELECT * FROM brands WHERE id=?", (brand_id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def get_brand_by_slug(self, slug: str) -> dict[str, Any] | None:
        async with self.raw.execute("SELECT * FROM brands WHERE slug=?", (slug,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_brands(self, only_active: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM brands"
        if only_active:
            sql += " WHERE is_active=1"
        sql += " ORDER BY id"
        async with self.raw.execute(sql) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def update_brand(self, brand_id: int, fields: dict[str, Any]) -> None:
        allowed = {"name", "slug", "description", "is_active"}
        sets, params = self._build_update(fields, allowed)
        if not sets:
            return
        await self._write(
            f"UPDATE brands SET {sets}, updated_at=? WHERE id=?",
            (*params, _now(), brand_id),
        )

    async def delete_brand(self, brand_id: int) -> None:
        await self._write("DELETE FROM brands WHERE id=?", (brand_id,))

    # ------------------------------------------------------------- branches --
    async def create_branch(
        self,
        brand_id: int,
        name: str,
        city: str = "",
        address: str = "",
        phone: str = "",
        lat: float | None = None,
        lon: float | None = None,
        work_from: str = "09:00",
        work_to: str = "22:00",
    ) -> dict[str, Any]:
        ts = _now()
        cur = await self._write(
            "INSERT INTO branches(brand_id,name,city,address,phone,lat,lon,"
            "work_from,work_to,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (brand_id, name, city, address, phone, lat, lon, work_from, work_to, ts, ts),
        )
        branch = await self.get_branch(cur.lastrowid or 0)
        await self.ensure_branding(branch["id"])  # default theme row
        return branch

    async def get_branch(self, branch_id: int) -> dict[str, Any] | None:
        async with self.raw.execute("SELECT * FROM branches WHERE id=?", (branch_id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_branches(self, brand_id: int | None = None, only_active: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM branches"
        conds: list[str] = []
        params: list[Any] = []
        if brand_id is not None:
            conds.append("brand_id=?")
            params.append(brand_id)
        if only_active:
            conds.append("is_active=1")
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY brand_id, id"
        async with self.raw.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def update_branch(self, branch_id: int, fields: dict[str, Any]) -> None:
        allowed = {
            "name", "city", "address", "phone", "lat", "lon",
            "work_from", "work_to", "is_active",
        }
        sets, params = self._build_update(fields, allowed)
        if not sets:
            return
        await self._write(
            f"UPDATE branches SET {sets}, updated_at=? WHERE id=?",
            (*params, _now(), branch_id),
        )

    async def delete_branch(self, branch_id: int) -> None:
        await self._write("DELETE FROM branches WHERE id=?", (branch_id,))

    # ----------------------------------------------------------- categories --
    async def create_category(
        self, branch_id: int, title: str, parent_id: int | None = None,
        emoji: str = "", position: int = 0,
    ) -> dict[str, Any]:
        ts = _now()
        cur = await self._write(
            "INSERT INTO categories(branch_id,parent_id,title,emoji,position,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (branch_id, parent_id, title, emoji, position, ts, ts),
        )
        row = cur.lastrowid or 0
        return {"id": row, "branch_id": branch_id, "parent_id": parent_id,
                "title": title, "emoji": emoji, "position": position,
                "is_active": 1}

    async def get_category(self, category_id: int) -> dict[str, Any] | None:
        async with self.raw.execute(
            "SELECT * FROM categories WHERE id=?", (category_id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_categories(self, branch_id: int, only_active: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM categories WHERE branch_id=?"
        if only_active:
            sql += " AND is_active=1"
        sql += " ORDER BY position, id"
        async with self.raw.execute(sql, (branch_id,)) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def update_category(self, category_id: int, fields: dict[str, Any]) -> None:
        allowed = {"title", "emoji", "position", "parent_id", "is_active"}
        sets, params = self._build_update(fields, allowed)
        if not sets:
            return
        await self._write(
            f"UPDATE categories SET {sets}, updated_at=? WHERE id=?",
            (*params, _now(), category_id),
        )

    async def delete_category(self, category_id: int) -> None:
        await self._write("DELETE FROM categories WHERE id=?", (category_id,))

    # --------------------------------------------------------------- dishes --
    async def create_dish(self, data: dict[str, Any]) -> dict[str, Any]:
        ts = _now()
        cur = await self._write(
            "INSERT INTO dishes(branch_id,category_id,title,description,price,"
            "old_price,image_key,is_available,is_hit,is_vegan,upsell_group,"
            "position,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                data["branch_id"], data["category_id"], data["title"],
                data.get("description", ""), int(data.get("price", 0)),
                data.get("old_price"), data.get("image_key"),
                int(bool(data.get("is_available", True))),
                int(bool(data.get("is_hit", False))),
                int(bool(data.get("is_vegan", False))),
                data.get("upsell_group", ""), int(data.get("position", 0)),
                ts, ts,
            ),
        )
        dish = await self.get_dish(cur.lastrowid or 0)
        assert dish is not None
        return dish

    async def get_dish(self, dish_id: int) -> dict[str, Any] | None:
        async with self.raw.execute("SELECT * FROM dishes WHERE id=?", (dish_id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_dishes(
        self,
        branch_id: int,
        category_id: int | None = None,
        only_active: bool = True,
        search: str = "",
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM dishes WHERE branch_id=?"
        params: list[Any] = [branch_id]
        if category_id is not None:
            sql += " AND category_id=?"
            params.append(category_id)
        if only_active:
            sql += " AND is_active=1"
        if search:
            sql += " AND (title LIKE ? OR description LIKE ?)"
            like = f"%{search}%"
            params.extend([like, like])
        sql += " ORDER BY position, id"
        async with self.raw.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def update_dish(self, dish_id: int, fields: dict[str, Any]) -> None:
        allowed = {
            "title", "description", "price", "old_price", "image_key",
            "is_available", "is_hit", "is_vegan", "upsell_group", "position",
            "category_id", "is_active",
        }
        sets, params = self._build_update(fields, allowed)
        if not sets:
            return
        await self._write(
            f"UPDATE dishes SET {sets}, updated_at=? WHERE id=?",
            (*params, _now(), dish_id),
        )

    async def delete_dish(self, dish_id: int) -> None:
        await self._write("DELETE FROM dishes WHERE id=?", (dish_id,))

    # ------------------------------------------------------------- branding --
    async def ensure_branding(self, branch_id: int) -> dict[str, Any]:
        async with self.raw.execute(
            "SELECT * FROM branding WHERE branch_id=?", (branch_id,)
        ) as cur:
            row = await cur.fetchone()
        if row:
            return dict(row)
        await self._write(
            "INSERT INTO branding(branch_id,theme,updated_at) VALUES(?,?,?)",
            (branch_id, "{}", _now()),
        )
        return {"branch_id": branch_id, "logo_key": None, "banner_key": None,
                "theme": "{}", "updated_at": _now()}

    async def get_branding(self, branch_id: int) -> dict[str, Any]:
        await self.ensure_branding(branch_id)
        async with self.raw.execute(
            "SELECT * FROM branding WHERE branch_id=?", (branch_id,)
        ) as cur:
            row = await cur.fetchone()
        data = dict(row) if row else {}
        data["theme_parsed"] = json.loads(data.get("theme") or "{}")
        return data

    async def update_branding(
        self, branch_id: int, theme: dict[str, Any] | None = None,
        logo_key: str | None = None, banner_key: str | None = None,
    ) -> None:
        await self.ensure_branding(branch_id)
        ts = _now()
        if theme is not None:
            await self._write(
                "UPDATE branding SET theme=?, updated_at=? WHERE branch_id=?",
                (json.dumps(theme, ensure_ascii=False), ts, branch_id),
            )
        if logo_key is not None:
            await self._write(
                "UPDATE branding SET logo_key=?, updated_at=? WHERE branch_id=?",
                (logo_key, ts, branch_id),
            )
        if banner_key is not None:
            await self._write(
                "UPDATE branding SET banner_key=?, updated_at=? WHERE branch_id=?",
                (banner_key, ts, branch_id),
            )

    # -------------------------------------------------------- delivery rules --
    async def list_delivery_rules(self, branch_id: int, only_active: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM delivery_rules WHERE branch_id=?"
        if only_active:
            sql += " AND is_active=1"
        sql += " ORDER BY position, id"
        async with self.raw.execute(sql, (branch_id,)) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def create_delivery_rule(self, data: dict[str, Any]) -> int:
        ts = _now()
        cur = await self._write(
            "INSERT INTO delivery_rules(branch_id,rule_type,min_total,max_km,"
            "amount,percent,free_over,position,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                data["branch_id"], data["rule_type"], int(data.get("min_total", 0)),
                data.get("max_km"), int(data.get("amount", 0)),
                float(data.get("percent", 0)), data.get("free_over"),
                int(data.get("position", 0)), ts, ts,
            ),
        )
        return cur.lastrowid or 0

    async def update_delivery_rule(self, rule_id: int, fields: dict[str, Any]) -> None:
        allowed = {
            "rule_type", "min_total", "max_km", "amount", "percent",
            "free_over", "position", "is_active",
        }
        sets, params = self._build_update(fields, allowed)
        if not sets:
            return
        await self._write(
            f"UPDATE delivery_rules SET {sets}, updated_at=? WHERE id=?",
            (*params, _now(), rule_id),
        )

    async def delete_delivery_rule(self, rule_id: int) -> None:
        await self._write("DELETE FROM delivery_rules WHERE id=?", (rule_id,))

    # ----------------------------------------------------------------- users --
    async def upsert_user(
        self, tg_id: int, first_name: str = "", last_name: str = "",
        username: str = "", language: str | None = None,
    ) -> dict[str, Any]:
        ts = _now()
        await self._write(
            "INSERT INTO users(id, first_name, last_name, username, language,"
            " created_at, updated_at) VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET first_name=excluded.first_name,"
            " last_name=excluded.last_name, username=excluded.username,"
            " updated_at=excluded.updated_at",
            (tg_id, first_name, last_name, username,
             language or "uz", ts, ts),
        )
        user = await self.get_user(tg_id)
        assert user is not None
        return user

    async def get_user(self, tg_id: int) -> dict[str, Any] | None:
        async with self.raw.execute("SELECT * FROM users WHERE id=?", (tg_id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def update_user(self, tg_id: int, fields: dict[str, Any]) -> None:
        allowed = {
            "first_name", "last_name", "username", "phone", "language",
            "role", "branch_id", "is_blocked",
        }
        sets, params = self._build_update(fields, allowed)
        if not sets:
            return
        await self._write(
            f"UPDATE users SET {sets}, updated_at=? WHERE id=?",
            (*params, _now(), tg_id),
        )

    async def set_user_role(self, tg_id: int, role: str, branch_id: int | None = None) -> None:
        fields: dict[str, Any] = {"role": role}
        if branch_id is not None:
            fields["branch_id"] = branch_id
        await self.update_user(tg_id, fields)

    async def add_address(self, user_id: int, title: str, address: str,
                          lat: float | None = None, lon: float | None = None) -> int:
        cur = await self._write(
            "INSERT INTO addresses(user_id,title,address,lat,lon,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (user_id, title, address, lat, lon, _now()),
        )
        return cur.lastrowid or 0

    async def list_addresses(self, user_id: int) -> list[dict[str, Any]]:
        async with self.raw.execute(
            "SELECT * FROM addresses WHERE user_id=? ORDER BY id DESC", (user_id,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # ------------------------------------------------------------- couriers --
    async def register_courier(self, tg_id: int, transport: str = "foot",
                               branch_id: int | None = None) -> dict[str, Any]:
        ts = _now()
        await self.upsert_user(tg_id)
        await self.set_user_role(tg_id, "courier", branch_id)
        await self._write(
            "INSERT OR IGNORE INTO couriers(user_id,branch_id,transport,pin_code,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (tg_id, branch_id, transport, uuid.uuid4().hex[:6].upper(), ts, ts),
        )
        courier = await self.get_courier_by_user(tg_id)
        assert courier is not None
        return courier

    async def get_courier_by_user(self, tg_id: int) -> dict[str, Any] | None:
        async with self.raw.execute(
            "SELECT c.*, u.first_name, u.username FROM couriers c"
            " JOIN users u ON u.id=c.user_id WHERE c.user_id=?",
            (tg_id,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def get_courier(self, courier_id: int) -> dict[str, Any] | None:
        async with self.raw.execute(
            "SELECT * FROM couriers WHERE id=?", (courier_id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_couriers(self, branch_id: int | None = None) -> list[dict[str, Any]]:
        sql = ("SELECT c.*, u.first_name, u.username, u.phone FROM couriers c"
               " JOIN users u ON u.id=c.user_id")
        params: list[Any] = []
        if branch_id is not None:
            sql += " WHERE c.branch_id=?"
            params.append(branch_id)
        sql += " ORDER BY c.status, c.id"
        async with self.raw.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def set_courier_status(self, courier_id: int, status: str) -> None:
        await self._write(
            "UPDATE couriers SET status=?, updated_at=? WHERE id=?",
            (status, _now(), courier_id),
        )

    async def update_courier(self, courier_id: int, fields: dict[str, Any]) -> None:
        allowed = {"transport", "status", "branch_id", "rating",
                   "total_orders", "balance", "pin_code"}
        sets, params = self._build_update(fields, allowed)
        if not sets:
            return
        await self._write(
            f"UPDATE couriers SET {sets}, updated_at=? WHERE id=?",
            (*params, _now(), courier_id),
        )

    async def bump_courier_stats(self, courier_id: int) -> None:
        await self._write(
            "UPDATE couriers SET total_orders=total_orders+1,"
            " balance=balance+?, status='online', updated_at=? WHERE id=?",
            (self.courier_bonus, _now(), courier_id),
        )

    courier_bonus: int = 15000  # overridable via settings table

    # --------------------------------------------------------------- orders --
    async def create_order(self, data: dict[str, Any]) -> dict[str, Any]:
        ts = _now()
        code = data.get("public_code") or uuid.uuid4().hex[:6].upper()
        while await self._order_code_taken(code):
            code = uuid.uuid4().hex[:6].upper()
        cur = await self._write(
            "INSERT INTO orders(public_code,user_id,branch_id,kind,status,items,"
            "items_total,delivery_fee,total,address,lat,lon,phone,comment,"
            "payment,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                code, data["user_id"], data["branch_id"],
                data.get("kind", "delivery"), "new",
                json.dumps(data.get("items", []), ensure_ascii=False),
                int(data.get("items_total", 0)), int(data.get("delivery_fee", 0)),
                int(data.get("total", 0)), data.get("address", ""),
                data.get("lat"), data.get("lon"), data.get("phone", ""),
                data.get("comment", ""), data.get("payment", "cash"), ts, ts,
            ),
        )
        order = await self.get_order(cur.lastrowid or 0)
        assert order is not None
        return order

    async def _order_code_taken(self, code: str) -> bool:
        async with self.raw.execute(
            "SELECT 1 FROM orders WHERE public_code=?", (code,)
        ) as cur:
            return await cur.fetchone() is not None

    async def get_order(self, order_id: int) -> dict[str, Any] | None:
        async with self.raw.execute("SELECT * FROM orders WHERE id=?", (order_id,)) as cur:
            row = await cur.fetchone()
        return self._hydrate_order(row)

    async def get_order_by_code(self, code: str) -> dict[str, Any] | None:
        async with self.raw.execute(
            "SELECT * FROM orders WHERE public_code=?", (code,)
        ) as cur:
            row = await cur.fetchone()
        return self._hydrate_order(row)

    async def list_orders(
        self,
        user_id: int | None = None,
        branch_id: int | None = None,
        courier_id: int | None = None,
        status: str | None = None,
        statuses: Iterable[str] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM orders"
        conds: list[str] = []
        params: list[Any] = []
        if user_id is not None:
            conds.append("user_id=?"); params.append(user_id)
        if branch_id is not None:
            conds.append("branch_id=?"); params.append(branch_id)
        if courier_id is not None:
            conds.append("courier_id=?"); params.append(courier_id)
        if status is not None:
            conds.append("status=?"); params.append(status)
        if statuses is not None:
            stats = list(statuses)
            if stats:
                conds.append(f"status IN ({','.join('?' * len(stats))})")
                params.extend(stats)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        async with self.raw.execute(sql, params) as cur:
            return [o for r in await cur.fetchall() if (o := self._hydrate_order(r))]

    async def _set_order(self, order_id: int, fields: dict[str, Any]) -> None:
        allowed = {
            "status", "courier_id", "delivery_fee", "total", "eta_minutes",
            "accepted_at", "delivered_at", "cancelled_at", "cancel_reason",
        }
        sets, params = self._build_update(fields, allowed)
        if not sets:
            return
        await self._write(
            f"UPDATE orders SET {sets}, updated_at=? WHERE id=?",
            (*params, _now(), order_id),
        )

    async def update_order(self, order_id: int, fields: dict[str, Any]) -> None:
        await self._set_order(order_id, fields)

    async def count_orders(self, branch_id: int | None = None,
                           statuses: Iterable[str] | None = None) -> int:
        sql = "SELECT COUNT(*) c FROM orders"
        conds: list[str] = []
        params: list[Any] = []
        if branch_id is not None:
            conds.append("branch_id=?"); params.append(branch_id)
        if statuses:
            stats = list(statuses)
            conds.append(f"status IN ({','.join('?' * len(stats))})")
            params.extend(stats)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        async with self.raw.execute(sql, params) as cur:
            row = await cur.fetchone()
        return int(row["c"]) if row else 0

    async def revenue_since(self, branch_id: int | None, since_ts: float) -> int:
        sql = ("SELECT COALESCE(SUM(total),0) s FROM orders"
               " WHERE status NOT IN ('cancelled')")
        params: list[Any] = []
        if branch_id is not None:
            sql += " AND branch_id=?"
            params.append(branch_id)
        sql += " AND created_at>=?"
        params.append(since_ts)
        async with self.raw.execute(sql, params) as cur:
            row = await cur.fetchone()
        return int(row["s"]) if row else 0

    async def orders_by_day(self, branch_id: int, days: int = 7) -> list[dict[str, Any]]:
        since = _now() - days * 86400
        async with self.raw.execute(
            "SELECT DATE(created_at,'unixepoch') day, COUNT(*) orders,"
            " COALESCE(SUM(total),0) revenue FROM orders"
            " WHERE branch_id=? AND created_at>=? AND status!='cancelled'"
            " GROUP BY day ORDER BY day",
            (branch_id, since),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def top_dishes(self, branch_id: int, limit: int = 5) -> list[dict[str, Any]]:
        async with self.raw.execute(
            "SELECT * FROM orders WHERE branch_id=? AND status!='cancelled'"
            " ORDER BY id DESC LIMIT 200", (branch_id,)
        ) as cur:
            rows = await cur.fetchall()
        counter: dict[str, int] = {}
        for r in rows:
            for item in json.loads(r["items"]):
                counter[item.get("title", "?")] = (
                    counter.get(item.get("title", "?"), 0) + int(item.get("qty", 1))
                )
        ranked = sorted(counter.items(), key=lambda kv: kv[1], reverse=True)[:limit]
        return [{"title": t, "qty": q} for t, q in ranked]

    # --------------------------------------------------- courier claim race --
    async def claim_order(self, order_id: int, courier_id: int) -> tuple[bool, dict[str, Any] | None]:
        """Atomically claim an order.

        The UNIQUE(order_id, courier_id) ledger plus a conditional UPDATE on
        `courier_id IS NULL AND status IN ('new','accepted','cooking','ready')`
        guarantees exactly one courier wins; all concurrent losers receive
        False without any race window.
        """
        ts = _now()
        async with self._lock:
            async with self.raw.execute(
                "SELECT * FROM orders WHERE id=?", (order_id,)
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                return False, None
            if row["courier_id"] is not None or row["status"] not in {
                "new", "accepted", "cooking", "ready",
            }:
                await self.raw.execute(
                    "INSERT OR IGNORE INTO order_claims(order_id,courier_id,"
                    "outcome,created_at) VALUES(?,?,?,?)",
                    (order_id, courier_id, "lost", ts),
                )
                await self.raw.commit()
                return False, dict(row)

            await self.raw.execute(
                "INSERT OR IGNORE INTO order_claims(order_id,courier_id,outcome,"
                "created_at) VALUES(?,?,?,?)",
                (order_id, courier_id, "claimed", ts),
            )
            await self.raw.execute(
                "UPDATE orders SET courier_id=?, status='courier_assigned',"
                " accepted_at=COALESCE(accepted_at,?), updated_at=? WHERE id=?"
                " AND courier_id IS NULL",
                (courier_id, ts, ts, order_id),
            )
            claimed = self.raw.total_changes > 0
            await self.raw.commit()
        if not claimed:
            return False, await self.get_order(order_id)
        await self.set_courier_status(courier_id, "delivering")
        return True, await self.get_order(order_id)

    async def reject_order(self, order_id: int, courier_id: int) -> None:
        await self._write(
            "INSERT OR IGNORE INTO order_claims(order_id,courier_id,outcome,"
            "created_at) VALUES(?,?,?,?)",
            (order_id, courier_id, "rejected", _now()),
        )

    async def order_claims_for(self, order_id: int) -> list[dict[str, Any]]:
        async with self.raw.execute(
            "SELECT * FROM order_claims WHERE order_id=?", (order_id,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # -------------------------------------------------------------- settings --
    async def get_setting(self, key: str, branch_id: int = 0) -> dict[str, Any]:
        async with self.raw.execute(
            "SELECT value FROM settings WHERE key=? AND branch_id=?",
            (key, branch_id),
        ) as cur:
            row = await cur.fetchone()
        return json.loads(row["value"]) if row else {}

    async def set_setting(self, key: str, value: dict[str, Any], branch_id: int = 0) -> None:
        await self._write(
            "INSERT INTO settings(key,branch_id,value,updated_at) VALUES(?,?,?,?)"
            " ON CONFLICT(key,branch_id) DO UPDATE SET value=excluded.value,"
            " updated_at=excluded.updated_at",
            (key, branch_id, json.dumps(value, ensure_ascii=False), _now()),
        )

    async def list_settings(self, branch_id: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM settings"
        params: list[Any] = []
        if branch_id is not None:
            sql += " WHERE branch_id=?"
            params.append(branch_id)
        async with self.raw.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # ---------------------------------------------------------- media assets --
    async def save_media(self, key: str, branch_id: int | None, kind: str,
                         filename: str, mime: str, size: int,
                         uploaded_by: int | None) -> None:
        await self._write(
            "INSERT OR REPLACE INTO media_assets(key,branch_id,kind,filename,"
            "mime,size,uploaded_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (key, branch_id, kind, filename, mime, size, uploaded_by, _now()),
        )

    async def get_media(self, key: str) -> dict[str, Any] | None:
        async with self.raw.execute(
            "SELECT * FROM media_assets WHERE key=?", (key,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_media(self, branch_id: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM media_assets"
        params: list[Any] = []
        if branch_id is not None:
            sql += " WHERE branch_id=?"
            params.append(branch_id)
        sql += " ORDER BY created_at DESC"
        async with self.raw.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # ------------------------------------------------------------ audit log --
    async def audit(self, actor_id: int, action: str, entity: str = "",
                    entity_id: int | None = None, details: dict[str, Any] | None = None) -> None:
        await self._write(
            "INSERT INTO audit_log(actor_id,action,entity,entity_id,details,"
            "created_at) VALUES(?,?,?,?,?,?)",
            (actor_id, action, entity, entity_id,
             json.dumps(details or {}, ensure_ascii=False), _now()),
        )

    async def list_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        async with self.raw.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # ------------------------------------------------------------ integrations --
    async def create_integration(self, data: dict[str, Any]) -> int:
        ts = _now()
        cur = await self._write(
            "INSERT INTO integrations(branch_id,system,name,direction,"
            "webhook_url,secret,mapping,is_active,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                data.get("branch_id"), data["system"], data.get("name", ""),
                data.get("direction", "export"), data.get("webhook_url", ""),
                data.get("secret", uuid.uuid4().hex),
                json.dumps(data.get("mapping", {}), ensure_ascii=False),
                int(data.get("is_active", True)), ts, ts,
            ),
        )
        return cur.lastrowid or 0

    async def list_integrations(self, branch_id: int | None = None,
                                only_active: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM integrations"
        conds: list[str] = []
        params: list[Any] = []
        if branch_id is not None:
            conds.append("branch_id=?"); params.append(branch_id)
        if only_active:
            conds.append("is_active=1")
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id"
        async with self.raw.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_integration(self, integration_id: int) -> dict[str, Any] | None:
        async with self.raw.execute(
            "SELECT * FROM integrations WHERE id=?", (integration_id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def update_integration(self, integration_id: int, fields: dict[str, Any]) -> None:
        allowed = {
            "system", "name", "direction", "webhook_url", "secret",
            "mapping", "is_active",
        }
        sets, params = self._build_update(fields, allowed)
        if "mapping" in fields and isinstance(fields["mapping"], dict):
            sets, params = self._build_update(
                {**{k: v for k, v in fields.items() if k != "mapping"},
                 "mapping": json.dumps(fields["mapping"], ensure_ascii=False)},
                allowed,
            )
        if not sets:
            return
        await self._write(
            f"UPDATE integrations SET {sets}, updated_at=? WHERE id=?",
            (*params, _now(), integration_id),
        )

    async def delete_integration(self, integration_id: int) -> None:
        await self._write("DELETE FROM integrations WHERE id=?", (integration_id,))

    async def log_integration(self, integration_id: int, direction: str,
                              status: str, payload: str, response: str) -> None:
        await self._write(
            "INSERT INTO integration_logs(integration_id,direction,status,"
            "payload,response,created_at) VALUES(?,?,?,?,?,?)",
            (integration_id, direction, status, payload[:8000],
             response[:8000], _now()),
        )
        await self._write(
            "UPDATE integrations SET last_sync_at=?, last_status=? WHERE id=?",
            (_now(), status, integration_id),
        )

    async def list_integration_logs(self, integration_id: int, limit: int = 20) -> list[dict[str, Any]]:
        async with self.raw.execute(
            "SELECT * FROM integration_logs WHERE integration_id=?"
            " ORDER BY id DESC LIMIT ?", (integration_id, limit)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # -------------------------------------------------------------- helpers --
    @staticmethod
    def _build_update(fields: dict[str, Any], allowed: set[str]) -> tuple[str, list[Any]]:
        sets: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            sets.append(f"{key}=?")
            params.append(value)
        return ", ".join(sets), params

    @staticmethod
    def _hydrate_order(row: aiosqlite.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        data["items"] = json.loads(data.get("items") or "[]")
        return data


# Module-level singleton shared by aiogram handlers and aiohttp API.
db = Database(settings.DB_PATH)
