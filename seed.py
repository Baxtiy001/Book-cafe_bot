"""Seed the database with a realistic multi-branch demo: 1 brand, 2 branches,
delivery rules, categories/subcategories, dishes and settings.

Run:  python seed.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from database import Database, db as default_db
from config import settings


async def seed(db: Database | None = None) -> None:
    db = db or default_db
    await db.connect()

    if await db.list_brands(only_active=False):
        print("Database already seeded — skipping (delete the file to reseed).")
        await db.close()
        return

    print("Seeding Book Cafe demo data ...")
    brand = await db.create_brand("Book Cafe", "Kitob, qahva va milliy taomlar")

    tashkent = await db.create_branch(
        brand_id=brand["id"], name="Book Cafe Toshkent",
        city="Toshkent", address="Amir Temur shoh ko'chasi 108",
        phone="+998 71 200 70 07", lat=41.3111, lon=69.2797,
        work_from="08:00", work_to="23:00",
    )
    jizzakh = await db.create_branch(
        brand_id=brand["id"], name="Book Cafe Jizzax",
        city="Jizzax", address="Sharof Rashidov ko'chasi 25",
        phone="+998 72 222 33 44", lat=40.1158, lon=67.8422,
        work_from="09:00", work_to="22:00",
    )

    # ---- delivery pricing rules (admin-editable in the Mini App) ----
    await db.create_delivery_rule({
        "branch_id": tashkent["id"], "rule_type": "free_over",
        "min_total": 0, "amount": 15000, "free_over": 100000, "position": 0,
    })
    await db.create_delivery_rule({
        "branch_id": tashkent["id"], "rule_type": "flat",
        "min_total": 0, "amount": 15000, "position": 1,
    })
    await db.create_delivery_rule({
        "branch_id": jizzakh["id"], "rule_type": "flat",
        "min_total": 0, "amount": 10000, "position": 0,
    })

    # ---- white-label theme (admin-editable) ----
    await db.update_branding(tashkent["id"], theme={
        "primary": "#B4552D", "accent": "#2D6A4F", "bg": "#FAF6F0",
        "card": "#FFFFFF", "text": "#2B2118", "radius": 16,
        "font": "Georgia, 'Times New Roman', serif",
    })
    await db.update_branding(jizzakh["id"], theme={
        "primary": "#7C3AED", "accent": "#0EA5E9", "bg": "#F8F7FF",
        "card": "#FFFFFF", "text": "#1E1B2E", "radius": 14,
        "font": "system-ui, sans-serif",
    })

    menu: dict[str, dict[str, list[tuple[str, int, str, bool]]]] = {
        tashkent["id"]: {
            "☕️ Issiq ichimliklar": [
                ("Latte", 28000, "Mayin sutli kofe", False),
                ("Cappuccino", 26000, "Klassik italyancha", False),
                ("Kitob choyi", 15000, "Yashil choy + limon + asal", False),
                ("Raf kofe", 32000, "Kremli vanilli raf", True),
            ],
            "🍕 Fast-food": [
                ("Pepperoni Pitsa", 75000, "Katta 30sm, achchiq kolbasa", True),
                ("Cheeseburger", 35000, "Mol go'shti, cheder pishloq", False),
                ("Hot-dog", 24000, "Xrustaki bulochka", False),
                ("Fri kartoshka", 18000, "Ketchup bilan", False),
            ],
            "🍰 Shirinliklar": [
                ("Cheesecake", 38000, "New York uslubi", True),
                ("Tiramisu", 42000, "Italyan deserti", False),
                ("Medovik", 30000, "Asal tort", False),
            ],
            "📚 Kitoblar": [
                ("O'tkan kunlar", 55000, "Abdulla Qodiriy romani", False),
                ("1984", 68000, "J. Orwell", False),
                ("Kichik Shahzoda", 45000, "A. de Sent-Ekzyuperi", True),
            ],
        },
        jizzakh["id"]: {
            "☕️ Issiq ichimliklar": [
                ("Latte", 25000, "Mayin sutli kofe", False),
                ("Amerikano", 20000, "Kuchli qora kofe", False),
            ],
            "🍢 Milliy taomlar": [
                ("Osh", 42000, "Toshkent uslubidagi palov", True),
                ("Lagman", 38000, "Qo'lda cho'zilgan", False),
                ("Manti (5 ta)", 35000, "Go'shtli manti", False),
            ],
            "🍰 Shirinliklar": [
                ("Napoleon", 32000, "Sloyli tort", False),
            ],
        },
    }
    upsell_groups = {"Pitsa": "_drink", "Cheeseburger": "_drink", "Osh": "_drink"}
    for branch_id, categories in menu.items():
        for cat_pos, (cat_title, dishes) in enumerate(categories.items()):
            cat = await db.create_category(branch_id, cat_title, position=cat_pos)
            for pos, (title, price, desc, hit) in enumerate(dishes):
                await db.create_dish({
                    "branch_id": branch_id, "category_id": cat["id"],
                    "title": title, "description": desc, "price": price,
                    "is_hit": hit, "position": pos,
                    "upsell_group": upsell_groups.get(title, ""),
                })

    # Subcategory demo (parent category "Shirinliklar" -> "Tortlar") for Tashkent
    cats = await db.list_categories(tashkent["id"])
    desserts = next((c for c in cats if "Shirinlik" in c["title"]), None)
    if desserts:
        sub = await db.create_category(tashkent["id"], "🎂 Tortlar",
                                       parent_id=desserts["id"], position=99)
        await db.create_dish({
            "branch_id": tashkent["id"], "category_id": sub["id"],
            "title": "Medovik bo'lagi", "description": "Asal tort",
            "price": 30000, "position": 0,
        })

    await db.set_setting("upsell", {
        "enabled": True,
        "title": "Birga buyurtma qilasizmi?",
        "groups": {
            "_drink": {"title": " ichimlik qo'shamizmi?", "discount_percent": 10},
        },
    }, tashkent["id"])
    await db.set_setting("upsell", {"enabled": True}, jizzakh["id"])

    await db.set_setting("receipt", {
        "footer": "Rahmat! Kitob o'qing, qahva iching ☕️",
    }, 0)

    await db.audit(0, "seed.run", "database")
    print(f"✔ Brand: {brand['name']}")
    print(f"✔ Branches: {tashkent['name']}, {jizzakh['name']}")
    print("✔ Categories, dishes, delivery rules, themes seeded.")


if __name__ == "__main__":
    asyncio.run(seed())
