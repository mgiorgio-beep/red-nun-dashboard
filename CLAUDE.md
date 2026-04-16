# Red Nun Dashboard — Claude Code Guide

## ⚠️ REPO & WORKFLOW — READ THIS FIRST

### The one repo that matters
```
GitHub:  https://github.com/mgiorgio-beep/red-nun-dashboard
Local:   C:\Users\giorg\red-nun-dashboard          (Beelink)
Server:  /opt/red-nun-dashboard                    (DigitalOcean)
Live at: https://dashboard.rednun.com
```

### SSH to server
```
ssh -p 2222 rednun@ssh.rednun.com
```
Key auth via `C:\Users\giorg\.ssh\id_ed25519` (already configured).

### Deploy workflow
```
1. Edit files in C:\Users\giorg\red-nun-dashboard\
2. git add / git commit / git push
3. On server: cd /opt/red-nun-dashboard && git pull && systemctl restart rednun
```
Or one-liner from server:
```
cd /opt/red-nun-dashboard && git pull && systemctl restart rednun
```

### DO NOT edit or push from these old folders — they are stale/outdated:
- `C:\Users\giorg\Downloads\toast-analytics\`  (old copy, wrong repo)
- `C:\Users\giorg\dashboard\`                  (points to red-nun-analytics, NOT this repo)

---

## What This Is
Custom restaurant management dashboard replacing MarginEdge ($363/mo).
Two locations: Dennis Port & Chatham, Cape Cod, MA.

## Tech Stack
- **Server:** DigitalOcean Ubuntu 24 (IP: 159.65.180.102)
- **SSH:** `ssh -p 2222 rednun@ssh.rednun.com`
- **Service:** `systemctl restart rednun` (runs as root via sudo)
- **Process:** gunicorn → `web/server.py` on port 8080, nginx proxies 443→8080
- **Backend:** Python / Flask / Gunicorn
- **Database:** SQLite WAL → `toast_data.db` (in repo root on server)
- **Frontend:** Vanilla HTML/JS/CSS, dark theme
- **Auth:** Email-based login with invite system (see `routes/auth_routes.py`)

## Key Files
```
web/server.py                  — Flask app, route registrations
web/static/manage.html         — Main dashboard SPA (~7,600 lines)
web/static/sidebar.js          — Shared sidebar nav (injected on every page)
web/static/payments.html       — Vendor payments / AP page
routes/billpay_routes.py       — Bill pay API (AP invoices, checks, payroll)
routes/inventory_routes.py     — Inventory management
routes/invoice_routes.py       — Invoice scanning (Claude Vision OCR)
routes/vendor_routes.py        — Vendor CRUD
routes/auth_routes.py          — Login, invite, roles
data/                          — Runtime JSON (specials, odds, tvs) — not edited manually
```

## Service Management
```bash
systemctl restart rednun        # restart app
systemctl status rednun         # check status
journalctl -u rednun -f         # live logs
journalctl -u rednun -n 50      # last 50 log lines
nginx -t && systemctl restart nginx   # restart nginx
```

## Database
- Path on server: `/opt/red-nun-dashboard/toast_data.db`
- SQLite WAL mode — supports concurrent reads
- Never commit `toast_data.db` to git

## CRITICAL — DO NOT BREAK
1. **Auth middleware** on all routes — `@login_required` decorator from `auth_routes.py`
2. **Timezone logic** in toast integrations — uses 4AM ET business day boundary
3. **WAL mode** on SQLite — keep it
4. **gunicorn workers=2** — needed for concurrent requests

## Two Locations
- Dennis Port: `location = 'dennis'`
- Chatham: `location = 'chatham'`
All data queries accept optional `?location=` param.

## Design System
- Background: `#020617` (slate-950)
- Cards: `#0f172a` (slate-900)
- Borders: `#1e293b` (slate-800)
- Text: `#e2e8f0` (slate-200)
- Green (positive): `#22c55e`
- Red (alert): `#ef4444`
- Amber (warning): `#f59e0b`
- Blue (info): `#38bdf8`
