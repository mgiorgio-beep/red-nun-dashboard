# Red Nun Dashboard

Restaurant operations dashboard for The Red Nun.
Consolidates POS, labor, accounting, vendor invoices, and TV displays into a single Flask app.

## What it does

- **Toast POS sync** — pulls orders, payments, and menu data into a local SQLite (`toast_data.db`).
- **7shifts labor** — pulls schedules and actual labor for cost analysis.
- **QuickBooks Online** — payroll journal entries, AP push, reconciliation.
- **Invoice OCR** — Drive/email/local watchers feed PDFs into Claude vision; auto-categorizes against the product catalog.
- **Inventory AI** — voice + photo capture for shelf counts; reconciles to POS depletion.
- **Specials & Sports TV** — Fire TV displays for the bar and dining room (specials board, schedule, scores, odds).
- **Sonos** — venue music tab in the staff app (play/pause, volume, browse Spotify/Pandora).
- **Telegram bot** — operator queries against the dashboard via Anthropic SDK.
- **Reports** — P&L, food cost, pour cost, audit dashboard, forecast.

## Layout

```
web/                Flask app entry (server.py), templates, static
routes/             Flask blueprints (auth, invoices, billpay, catalog, products, ...)
bot/                Telegram bot (bot.py)
ai/                 Inventory AI (audio, vision, reconcile) + pmix matcher
reports/            analytics, audit_dashboard, forecast, invoice_anomaly, pour_cost
staff/              Staff app (specials editor, TV power, Sonos, watchdog)
monitoring/         Server health checks, ddns updater
scraping/           Sports guide (Fanzo, ESPN, odds fetchers)
integrations/
  toast/            toast_client, sync, data_store
  sevenshifts/      sevenshifts_client
  quickbooks/       qb_*.py, payroll, JE push, check printing assets
  invoices/         processor + watchers/{drive, local, email_invoice, email}
  google/           gmail_auth, auth_drive
  recipes/          recipe_costing, recipe_autopopulate
  vendors/          vendor_item_matcher
  thermostat/       thermostat, thermostat_fetch
  sonos/            (Sonos integration via SoCo)
scripts/archive/    One-off historical scripts (fix_, patch_, deploy_, migrate_, etc.)
deploy/             deploy.sh, deploy_sports.sh, deploy_invoices.sh
docs/               CLAUDE.md, PROJECT_BRIEF.md, session summaries, briefs
data/               schema_v2.sql (rest of data/ is gitignored runtime state)
tests/              test_ai_inventory
```

## Runtime

- Production host: Beelink (`rednun` user, port 8080).
- Service: `rednun.service` runs `gunicorn -w 2 -b 0.0.0.0:8080 web.server:app`.
- Bot service: `rednun-agent.service` runs `python bot/bot.py`.
- Toast DB lives outside the repo at `/var/lib/rednun/toast_data.db` (path set via `TOAST_DB_PATH` in `.env`).
- Sonos Amp at `10.1.10.242` (Dining Room), controlled via `soco`.
- Two Fire TVs (bar + dining) load specials from `http://10.1.10.83:8080/staff/specials/tv`.

## Setup

```bash
git clone https://github.com/mgiorgio-beep/red-nun-dashboard.git /opt/red-nun-dashboard
cd /opt/red-nun-dashboard
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env  # then fill in secrets
sudo systemctl restart rednun rednun-agent
```

## Standing rules

- **No silent API spend.** All Anthropic API calls must be triggered by an explicit user action (button click, form submit). No background pollers, no scheduled API calls.
- **Backup before schema changes.** `cp toast_data.db /opt/backups/toast_data_$(date +%Y%m%d_%H%M).db`
- **Restart after Python or template changes.** Gunicorn caches templates: `sudo systemctl restart rednun`. Verify with `curl` before relaunching TVs.
- **Two-worker shared state must use files or DB.** In-process dicts will diverge across workers. Watchdog uses a file lock; Sonos linking uses a JSON sidecar.

## License

Private. Not for redistribution.
