# Beelink Server — System Map
**Server:** Beelink SER5, Chatham MA  
**SSH:** `ssh -p 2222 rednun@ssh.rednun.com`  
**Server IP:** 174.180.119.126

---

## Services (systemd)

| Service | Description | Port | Code Dir | Env File |
|---|---|---|---|---|
| `rednun.service` | Red Nun Dashboard + Bot | 8080 | `/opt/red-nun-dashboard` | `/opt/red-nun-dashboard/.env` |
| `rednun-agent.service` | Red Nun Telegram Bot | — | `/opt/red-nun-dashboard` | `/opt/red-nun-dashboard/.env` |
| `ticketsnap.service` | TicketSnap Invoice Scanner | 8081 | `/opt/ticketsnap` | `/opt/ticketsnap/.env` |
| `wheelhouse.service` | Wheelhouse Fishing Intel | 8090 | `/opt/wheelhouse` | `/opt/wheelhouse/.env` |
| `wheelhouse-bot.service` | Wheelhouse Telegram Bot | — | `/opt/wheelhouse-bot` | `/opt/wheelhouse-bot/.env` |
| `northfla.service` | North Florida Wheelhouse | 8091 | `/opt/northfla` | `/opt/northfla/.env` |
| `pm2-rednun.service` | Skywatch Node app | 3000 | `/home/rednun/skywatch` | — |
| `xvfb.service` | Virtual display for headless browser scrapers | — | — | — |

---

## Nginx → Service Routing

| Domain | → Port | Service |
|---|---|---|
| `dashboard.rednun.com` | → 8080 | rednun.service |
| `dashboard.rednun.com` (invoice scanner paths) | → 8081 | ticketsnap.service |
| `wheelhouse.rednun.com` | → 8090 | wheelhouse.service |
| `northfla.rednun.com` | → 8091 | northfla.service |
| `skywatch.rednun.com` | → 3000 | pm2-rednun.service (Node) |

---

## DNS Records (Cloudflare)

| Record | Type | Points To | Proxied | Notes |
|---|---|---|---|---|
| `dashboard.rednun.com` | A | 174.180.119.126 | YES | DDNS auto-updates this |
| `wheelhouse.rednun.com` | A | 174.180.119.126 | YES | |
| `northfla.rednun.com` | A | 174.180.119.126 | YES | |
| `skywatch.rednun.com` | A | 174.180.119.126 | YES | |
| `ssh.rednun.com` | A | 174.180.119.126 | NO | SSH can't go through Cloudflare |
| `rednun.com` | A | 162.120.94.90 | **NO** | ⛔ Register.com web host — DO NOT TOUCH |
| `www.rednun.com` | CNAME | sites.toasttab.com | **NO** | ⛔ NEVER PROXY — breaks Toast ordering immediately |
| `_acme-challenge.rednun.com` | CNAME | rednun.com.cec5188867cef154.dcv.cloudflare.com | **NO** | ⛔ NEVER PROXY — breaks Toast SSL cert renewal (ordering fails within days) |
| `mail.rednun.com` | CNAME | webmail01.register.com | NO | |
| MX records | — | Google Workspace | NO | |

---

## Databases

| File | Size | Used By | Notes |
|---|---|---|---|
| `/var/lib/rednun/toast_data.db` | ~1.3 GB | Red Nun Dashboard (via `DB_PATH` in .env) | LIVE — WAL mode |
| `/opt/wheelhouse/wheelhouse.db` | ~192 KB | Wheelhouse | LIVE |
| `/opt/red-nun-dashboard/data/toast_data.db` | 0 bytes | Nothing | Empty stub — ignore |
| `/opt/rednun.retired-20260412/toast_data.db` | ~1.3 GB | Nothing | Retired copy from Phase 2H migration |

---

## Cron Jobs

| Schedule | Script | Purpose |
|---|---|---|
| Every 5 min | `/opt/red-nun-dashboard/monitoring/ddns.py` | Update `dashboard.rednun.com` DNS in Cloudflare |
| Every 5 min | `/opt/red-nun-dashboard/integrations/invoices/watchers/email_invoice_poller.py` | Poll Gmail for invoice emails |
| Every 30 min | `/opt/red-nun-dashboard/monitoring/server_down_check.py` | Alert if server is unreachable |
| Daily 6am, 12pm, 6pm | `/opt/wheelhouse/logger.py` | Wheelhouse data logger |
| Daily 7am | `/home/rednun/vendor-scrapers/run_all.sh` | Scrape vendor invoices |
| Daily 10am | `scraping.sports_guide.fanzo_scraper.scrape_fanzo_guide()` | Fanzo sports guide scrape |
| Mon 8am | `/opt/red-nun-dashboard/monitoring/server_health_report.py` | Weekly server health email |
| Daily 3pm | `adb 10.1.10.20:5555` brightness 200 | Dennis Fire TV brighten for evening |
| Daily 11pm | `adb 10.1.10.20:5555` brightness 30 | Dennis Fire TV dim for close |

---

## /opt Directory

| Directory | Purpose | Git Repo |
|---|---|---|
| `/opt/red-nun-dashboard` | Red Nun Dashboard (current) | github.com/mgiorgio-beep/red-nun-dashboard |
| `/opt/wheelhouse` | Wheelhouse Cape Cod | github.com/mgiorgio-beep/wheelwatch |
| `/opt/wheelhouse-bot` | Wheelhouse Telegram bot | — |
| `/opt/northfla` | Wheelhouse North Florida | — |
| `/opt/ticketsnap` | TicketSnap invoice scanner | — |
| `/opt/backups` | Server backups | — |
| `/opt/rednun.retired-20260412` | OLD repo before Phase 2H migration — safe to delete | — |

---

## Data Directories (outside repos)

| Path | Contents |
|---|---|
| `/var/lib/rednun/` | Live SQLite DB + WAL files |
| `/opt/red-nun-dashboard/data/` | odds.json, specials.json, tv_power.json, schema |
| `/home/rednun/vendor-scrapers/` | Vendor invoice scraper scripts |
| `/home/rednun/skywatch/` | Skywatch Node app |
| `/opt/red-nun-dashboard/monitoring/` | Log files for cron jobs |

---

## Known Gotchas

- **`www.rednun.com` must NEVER be proxied through Cloudflare** — breaks Toast online ordering immediately (cost $1k+ in lost orders Apr 12 2026)
- **`_acme-challenge.rednun.com` must NEVER be proxied through Cloudflare** — breaks Toast SSL cert renewal; ordering fails on a delay (days), harder to catch (Apr 11-15 2026 outage)
- **`rednun.com` A record must never be changed** — points to Register.com host, not this server
- **Do not open Toast-related DNS records in Cloudflare's edit UI** — Cloudflare defaults proxy to ON when saving; even a no-op save can flip it
- **Real DB is `/var/lib/rednun/toast_data.db`** — set via `DB_PATH` in `.env`. The file at `/opt/red-nun-dashboard/data/toast_data.db` is an empty stub.
- **DDNS only updates `dashboard.rednun.com`** — all other A records point to the same IP but need manual update if the server IP ever changes
- **`/opt/rednun.retired-20260412`** — old repo left from Phase 2H cutover, can be deleted once confident everything is working
