# 📚 Book Cafe — Multi-Branch Telegram Mini App Ecosystem

Production-ready restaurant/cafe ecosystem driven entirely by a **Telegram Mini
App** with a **Python backend (aiogram 3 + aiohttp + SQLite)**.

**Zero hardcoding:** every brand, branch, category, subcategory, dish, price,
theme color, logo, delivery rule, courier and 1C integration is a database row
managed from the admin panel inside the Mini App itself.

---

## ✨ What's inside

| Area | Capability |
|---|---|
| 🏪 Multi-branch / multi-brand | Create brands → branches (Tashkent, Jizzakh…) with per-branch menus |
| 📋 Dynamic menu engine | Categories, subcategories, dishes, prices, HIT/vegan badges, upsell groups |
| 🎨 White-label branding | Logo + banner upload, live color theme presets, per-branch themes |
| 🛵 Smart delivery pricing | Rule engine: `free_over`, `flat`, `per_km`, `percent` (first match wins) |
| 🚨 Courier dispatch | Broadcast to online couriers, Accept/Reject race, first-claim-wins, auto keyboard freeze |
| 📦 Order lifecycle | new → accepted → cooking → ready → courier_assigned → delivering → delivered / cancelled |
| 🔄 1C / 1UZ ready | Signed webhook export, mapping editor, JSON import endpoint, polling feed |
| 🔐 Secure auth | Telegram `initData` HMAC validation, role resolution (admin/courier/client) |
| 📊 Statistics | Revenue by day/week/month, 14-day chart, top dishes, courier leaderboards, audit log |

## 🗂 Project structure

```
bot.py                  # entrypoint: aiogram + aiohttp in one loop
config.py               # all settings from .env (zero hardcoding)
database.py             # relational SQLite schema + async facade
api_server.py           # aiohttp REST API
seed.py                 # demo data seeder
app_keys.py             # aiohttp AppKey constants
handlers/start.py       # aiogram router (/start, courier flows, callbacks)
services/
  auth.py               # initData HMAC validation + roles
  delivery.py           # delivery fee rule engine
  dispatch.py           # courier broadcast + race-safe claim
  notifications.py      # outbound webhooks (export)
  integrations.py       # inbound import (1C dishes/prices/stock)
webapp/
  index.html            # Mini App shell (styles + markup)
  app.js                # Mini App logic (client + courier + admin)
env.template.txt        # .env template (see step 2)
```

---

## 🚀 Run locally — step by step

### Step 1 — Install dependencies

```bash
python3 -m venv .venv && source .venv/bin/activate   # optional but recommended
pip install -r requirements.txt
```

### Step 2 — Create `.env`

Copy `env.template.txt` to `.env` and fill in:

```ini
BOT_TOKEN=...          # from @BotFather
BOT_USERNAME=my_bot    # without @
ADMIN_IDS=123456789    # your Telegram numeric id (comma separated for several)
BASE_URL=https://<your-subdomain>.ngrok-free.app
WEBAPP_URL=https://<your-subdomain>.ngrok-free.app/webapp/
USE_WEBHOOK=true
WEBHOOK_SECRET=some-long-random-string
INTEGRATION_TOKEN=some-integration-token
```

### Step 3 — Create the bot in Telegram

1. Open **@BotFather** → `/newbot` → follow prompts.
2. Copy the token into `.env` (`BOT_TOKEN`).
3. `/setdomain` — set your ngrok/domain URL (required for Mini Apps).
4. `/setmenubutton` → choose your bot → paste `WEBAPP_URL` → text: `Book Cafe`.

### Step 4 — Seed the database

```bash
python seed.py
```

Creates brand “Book Cafe”, 2 branches (Toshkent + Jizzax), full menu,
delivery rules (free over 100 000 UZS), themes and settings.

### Step 5 — Expose with ngrok

```bash
ngrok http 8080
```

Copy the `https://...ngrok-free.app` URL into `.env` (`BASE_URL`,
`WEBAPP_URL`), then restart the bot. Telegram requires **HTTPS** for Mini Apps.

### Step 6 — Run

```bash
python bot.py
```

- API + Mini App: `https://<ngrok>/webapp/`
- Bot webhook: `https://<ngrok>/telegram-webhook/<SECRET_PATH>`
- Health check: `curl https://<ngrok>/api/health` → `{"status": "ok"}`

For pure local testing without Telegram you can set `USE_WEBHOOK=false`
and use `python bot.py` with long polling — the API still serves `/webapp/`.

### Step 7 — Test in Telegram

1. Open your bot → send `/start` → a clean greeting with one **full-screen
   Mini App button** (also available via the chat menu button 🍔).
2. **Client flow:** pick dishes → cart → upsell offers → checkout (delivery /
   pickup, GPS, payment) → live order tracking.
3. **Courier flow:** send `/courier` to the bot → get PIN → Mini App → “Kuryer”
   tab → toggle online → receive “New Order!” broadcasts → race to Accept.
4. **Admin flow:** add your id to `ADMIN_IDS` → reopen the Mini App → “⚙️ Admin”
   tab: branches, menu builder, delivery rules, branding, couriers, 1C.

### Step 8 — Wire up accounting (1C / 1UZ)

In the admin panel → **🔄 1C / Integratsiya**:

- **Export:** create integration with webhook URL + field mapping
  (`{"DocNumber":"public_code","Amount":"total"}`); orders are POSTed signed
  with `X-BookCafe-Signature` (HMAC of the body with the integration secret).
- **Pull export:** `GET /api/admin/integrations/{id}/export?branch_id=1`
  returns mapped JSON documents.
- **Import:** 1C pushes dishes/prices/stock:
  ```bash
  curl -X POST https://<host>/api/integration/import?integration_id=1 \
    -H "X-Integration-Token: <INTEGRATION_TOKEN>" \
    -H "Content-Type: application/json" \
    -d '{"type":"dishes","items":[{"code":"A-001","title":"Osh","price":45000,"available":true}]}'
  ```
- **Polling feed:** `GET /api/integration/orders-feed?since_ts=<ts>`
  with the `X-Integration-Token` header.

---

## 🔐 Security model

- Every API call carries `Authorization: tma <initData>` — validated by
  HMAC-SHA256 exactly per Telegram spec, expired data (>24 h) rejected.
- Roles: `ADMIN_IDS` (env) → DB `users.role` (`courier`) → `client`.
- Admin routes gated server-side; courier claiming is atomic in SQL.
- Media uploads restricted to jpeg/png/webp/gif ≤ 12 MB.
- Integration endpoints require `X-Integration-Token`; outbound webhooks are
  signed so 1C can verify authenticity.

## 🧯 Troubleshooting

| Symptom | Fix |
|---|---|
| Blank Mini App | Check `BASE_URL`/`WEBAPP_URL` are https and reachable; open `/api/health` |
| `initData invalid` | Bot token changed — restart bot; phone date correct |
| No courier broadcast | Courier must toggle **online**; only `delivery` orders broadcast |
| Webhook not updating | Re-run ngrok, update `BASE_URL`, restart `python bot.py` |
| Buttons don't close for other couriers | Check `dispatch` in logs; Telegram edit limits apply per 24 h |

## 🧹 Reset

```bash
rm -rf data/
python seed.py
python bot.py
```
