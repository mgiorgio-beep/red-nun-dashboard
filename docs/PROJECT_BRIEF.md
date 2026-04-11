# Red Nun — Full Project Brief
**Last updated:** March 16, 2026 (added sports_bp integration, streaming fixes, Peacock typo normalization)

This document covers the two Flask applications that power Red Nun's sports guide, staff tools, and TV control system. Use this as reference context for any future Claude sessions.

---

## Architecture Overview

There are **two separate Flask apps** running on a Beelink SER5 mini PC at the Chatham location:

| App | Path | Port | Service | User | Purpose |
|-----|------|------|---------|------|---------|
| **Dashboard** | `/opt/rednun/` | 8080 | `rednun.service` | `rednun` | Main dashboard: analytics, sports guide, staff PWA |
| **TV Control** | `/opt/tv_control/` | 5000 | `rednun-tv.service` | `root` | Local TV proxy: DirecTV/Roku control, specials board, network scanner |

Both use gunicorn (2 workers), systemd, and Python venvs. The Dashboard is exposed publicly via Cloudflare tunnel at `dashboard.rednun.com`. The TV Control app is local-network only (accessed from devices on the `10.1.10.x` Chatham subnet).

---

## APP 1: Dashboard (`/opt/rednun/`, port 8080)

### Server Entry Point — `server.py`
Main Flask app. Registers ~15 blueprints including:
- `sports_bp` (from `sports_guide/`) — sports guide routes
- `staff_bp` (from `staff/`) — staff PWA routes
- Also: auth, invoices, catalog, inventory, specials, food cost, vendor, voice recipe, pmix, product costing, order guide, storage

**Scheduled Jobs** (APScheduler):
- FANZO scrape: daily at 5:00 AM ET (`scrape_fanzo_guide()`)
- Odds fetch: every 2 hours at 5,7,9,...,23 ET (`fetch_all_odds()`)

### Sports Guide Module — `/opt/rednun/sports_guide/`

#### `fanzo_scraper.py` — Core Scraper
- Scrapes sports TV schedule from `guide.thedailyrail.com` (FANZO/DailyRail platform)
- **Auto-auth**: Uses Gmail IMAP to fetch magic-link emails from FANZO, extracts session cookie automatically — no manual cookie management
- Parses HTML guide into structured JSON with sections (by sport), games, channels, times
- Merges streaming services (ESPN+, Peacock, Apple TV+, Prime, Max, Paramount+) into sport sections
- **Typo normalization**: `_stream_badge()` corrects FANZO source typos (e.g. "Peaccok" → "Peacock") before lookup
- Highlights favorite teams (Bruins, Celtics, Red Sox, Patriots, Harvard, Dartmouth, BC, UMass)
- **`start_ts` field**: Each game object now includes a Unix timestamp (Eastern time) for machine-readable scheduling
- Saves to `/opt/rednun/data/sports_guide.json`

#### `fanzo_config.py` — Configuration
```python
FANZO_CONFIG_ID = '239743'
FAVORITE_TEAMS = ['Bruins', 'Celtics', 'Patriots', 'Red Sox', 'Harvard', 'Dartmouth', 'Boston College', 'BC ', 'UMass']
STREAMING_SERVICES = {
    'ESPNplus'/'ESPN+': 'ESPN+',       color '#3B82F6'
    'Peacock SP'/'Peacock': 'Peacock',  color '#8B6CEF'
    'Apple TV+': 'Apple TV+',          color '#333333'
    'Amazon Prime'/'Prime Video': 'Prime Video', color '#00A8E1'
    'Max': 'Max',                      color '#002BE7'
    'Paramount+': 'Paramount+',        color '#0064FF'
}
# Both fanzo_config.py and fanzo_scraper.py have this dict (scraper uses lowercase keys)
# _stream_badge() normalizes typos like "Peaccok" → "Peacock" before lookup
```

#### `espn_odds_fetcher.py` — Betting Odds
- Fetches spread + O/U from ESPN's free public API (DraftKings source)
- Covers: NFL, NBA, NHL, MLB, college football, men's/women's college basketball, MLS
- Falls back to The Odds API (paid, `ODDS_API_KEY`) for UFC only
- Saves to `/opt/rednun/data/odds.json`

#### `team_logos.py` — Logo Maps
- ESPN CDN URLs for NFL, NBA, NHL, MLB, NCAA team logos
- League section header icons

#### `sports.py` — Routes
| Route | Purpose |
|-------|---------|
| `GET /sports` | Staff-facing guide (has refresh button, nav) |
| `GET /sports/public` | Customer-facing guide (no nav) |
| `GET /guide` | Alias for public guide |
| `GET /sports/embed` | iframe-embeddable version for rednun.com |
| `POST /sports/refresh` | Trigger manual re-scrape |
| `GET /sports/api/data` | JSONP wrapper for cross-domain embedding |
| `GET /sports/api/odds` | Cached odds JSON |
| `GET/POST /sports/api/section-order` | Drag-and-drop section order persistence |

#### Templates
- `sports_guide.html` (~900 lines) — Full guide with live ESPN scores (45s polling), team logos, odds (desktop), dark mode (7pm-6am), section drag-and-drop
- `sports_embed.html` — Lightweight embeddable version

#### Features
- Live ESPN scores: polls every 45s, shows live badges, greys out final games
- Team logos: ESPN CDN for all major leagues + NCAA
- Betting odds: DraftKings spreads + O/U on desktop
- Favorite team highlighting with gold star
- Section drag-and-drop reorder (persists to `section_order.json`)
- Dark mode: auto 7pm-6am on mobile
- PWA: manifest, service worker, iOS home screen icons

---

### Staff Module — `/opt/rednun/staff/`

#### `staff.py` — Routes & TV Control Backend
| Route | Method | Purpose |
|-------|--------|---------|
| `/staff` | GET | Serve staff PWA shell |
| `/staff/api/tvs` | GET | Get TV config (6 TVs with dtv_ip, roku_ip) |
| `/staff/api/tvs` | POST | Save TV config to `tvs.json` |
| `/staff/api/dtv/tune` | POST | Tune DirecTV receiver via SHEF protocol |
| `/staff/api/roku/command` | POST | Send Roku ECP command (power, hdmi, launch app) |
| `/staff/api/roku/apps` | GET | Return Roku app ID mapping |
| `/staff/api/lights` | GET | Get light zone states |
| `/staff/api/lights` | POST | Save light zone states |

#### TV Data Model (`/opt/rednun/data/tvs.json`)
```json
[
  {"id": "bar-left", "name": "Bar Left", "dtv_ip": "", "roku_ip": "", "channel": ""},
  {"id": "bar-middle", "name": "Bar Middle", "dtv_ip": "", "roku_ip": "", "channel": ""},
  {"id": "bar-right", "name": "Bar Right", "dtv_ip": "", "roku_ip": "", "channel": ""},
  {"id": "dr-left", "name": "DR Left", "dtv_ip": "", "roku_ip": "", "channel": ""},
  {"id": "dr-middle", "name": "DR Middle", "dtv_ip": "", "roku_ip": "", "channel": ""},
  {"id": "dr-right", "name": "DR Right", "dtv_ip": "", "roku_ip": "", "channel": ""}
]
```
- `dtv_ip`: DirecTV receiver IP (all 6 TVs have a receiver)
- `roku_ip`: Roku TV IP (empty = no Roku; 4 of 6 have Roku now)
- `channel`: Last-tuned channel display text

#### TV Inventory at Chatham
| Location | DirecTV Box | Roku TV | Notes |
|----------|-------------|---------|-------|
| Bar Left | Yes | Yes | |
| Bar Middle | Yes | **No** | Regular TV, planned Roku upgrade |
| Bar Right | Yes | Yes | |
| DR Left | Yes | **No** | Regular TV, planned Roku upgrade |
| DR Middle | Yes | Yes | Paired in tv_control: roku 10.1.10.154, dtv 10.1.10.93 |
| DR Right | Yes | Yes | |

#### DirecTV SHEF Protocol
- HTTP on port 8080 (receivers expose this natively)
- Tune: `GET http://{ip}:8080/tv/tune?major={channel}`
- Get current: `GET http://{ip}:8080/tv/getTuned`
- Remote key: `GET http://{ip}:8080/remote/processKey?key={key}&hold=keyPress`

#### Roku ECP (External Control Protocol)
- HTTP on port 8060 (Roku TVs expose this natively)
- Keypress: `POST http://{ip}:8060/keypress/{key}` (PowerOn, PowerOff, Home, InputHDMI1, etc.)
- Launch app: `POST http://{ip}:8060/launch/{app_id}`
- Device info: `GET http://{ip}:8060/query/device-info` (XML)
- Installed apps: `GET http://{ip}:8060/query/apps` (XML)

#### Roku App IDs (Streaming Services)
```
ESPN+/ESPN App: 34376    Peacock: 593099      Apple TV+: 551012
Prime Video: 13          Netflix: 12          YouTube TV: 195316
Hulu: 2285               YouTube: 837         MLB.TV: 49235
NFL+: 696059             Paramount+: 291097
```

#### `staff.html` (~72KB) — Staff PWA Template
iOS-style tabbed interface with three tabs:
1. **TVs Tab**: Sports guide display (same FANZO data), tap game → TV picker drawer (6 TVs), "Manage TVs" setup screen with editable IP fields. Streaming-aware: greys out non-Roku TVs for streaming-only games, shows purple Roku dot on Roku TVs.
2. **Specials Tab**: CRUD form for daily specials (soup, appetizer, specials), publishes to `/api/specials`
3. **Venue Tab**: Lights UI (scene buttons + zone sliders), thermostat display

#### `tuneTV()` Logic (Frontend)
When staff taps a game then picks a TV:
1. DTV channel + has dtv_ip → tune DirecTV box (+ switch Roku to HDMI1 if roku_ip exists)
2. Streaming game + has roku_ip → launch streaming app on Roku
3. Streaming game + no roku_ip → toast "This TV doesn't have a Roku"
4. No channel info → toast "No channel info"
5. Has channel but no DTV IP → toast "Set up TV IP in Manage TVs"

---

## APP 2: TV Control (`/opt/tv_control/`, port 5000)

Local-network Flask app on the Beelink. Serves as an HTTP proxy to solve:
1. Safari mixed-content blocking (HTTPS dashboard page → HTTP local devices)
2. Roku CORS restrictions (ECP has no Access-Control-Allow-Origin headers)

### Sports Guide Integration (Cross-App Import)
The TV Control app imports the full sports guide blueprint from the Dashboard codebase at `/opt/rednun/sports_guide/`. This is done via `sys.path` in `app.py`:
```python
sys.path.insert(0, '/opt/rednun/venv/lib/python3.12/site-packages')  # bs4, pytz
sys.path.insert(0, '/opt/rednun')
from sports_guide.sports import sports_bp
# ...
app.register_blueprint(sports_bp)
```
- The sports_guide package lives at `/opt/rednun/sports_guide/` (single source of truth, not copied)
- Dependencies (`beautifulsoup4`, `pytz`) are borrowed from the rednun venv's site-packages
- All data paths in the package resolve to `/opt/rednun/data/` via `os.path.abspath(__file__)`
- The FANZO scraper cron still runs from `/opt/rednun/` — TV Control reads the same `sports_guide.json`
- This gives the TV Control app the full rendered guide at `/guide`, `/sports`, `/sports/embed` etc.
- The existing `/api/guide` route (raw JSON) is separate and still works alongside the blueprint routes

### `app.py` — Main App
| Route | Purpose |
|-------|---------|
| `GET /` | TV control PWA (control.html) |
| `GET /settings` | TV pairing settings page |
| `GET /health` | Health check |
| `GET /api/guide` | Sports guide data (local cache → fallback to dashboard.rednun.com) |
| `GET /api/dtv/autotune` | DirecTV auto-tune (accepts ip, ch, time params) |
| `GET /guide` | Public sports guide (HTML) — via sports_bp |
| `GET /sports` | Staff sports guide with refresh button — via sports_bp |
| `GET /sports/public` | Customer-facing guide — via sports_bp |
| `GET /sports/embed` | iframe embed version — via sports_bp |
| `POST /sports/refresh` | Trigger FANZO re-scrape — via sports_bp |
| `GET /sports/api/data` | JSONP sports data endpoint — via sports_bp |
| `GET /sports/api/odds` | Cached odds JSON — via sports_bp |
| `GET/POST /sports/api/section-order` | Section order persistence — via sports_bp |
| `GET /specials` | Full-screen chalkboard display for portrait TV |
| `GET /specials/edit` | Manager editor for specials (phone UI) |
| `GET/POST /api/specials` | Specials CRUD |

### `proxy.py` — DirecTV & Roku Proxy Routes (Blueprint, prefix `/api`)
| Route | Method | Purpose |
|-------|--------|---------|
| `/api/dtv/tune?ip=&ch=` | GET | Tune DirecTV channel |
| `/api/dtv/key?ip=&key=` | GET | Send remote key to DirecTV |
| `/api/dtv/info?ip=` | GET | Get current tuning info |
| `/api/roku/keypress?ip=&key=` | POST | Send Roku keypress |
| `/api/roku/launch?ip=&app=` | POST | Launch Roku app (name or ID) |
| `/api/roku/hdmi?ip=&port=` | POST | Switch HDMI input |
| `/api/roku/query?ip=&path=` | GET | Query Roku device info (XML) |
| `/api/roku/apps?ip=` | GET | Get installed apps (parsed XML→JSON) |

### `scanner.py` — Network Scanner (Blueprint, prefix `/api`)
| Route | Purpose |
|-------|---------|
| `GET /api/scan?subnet=10.1.10&start=1&end=254` | Full subnet scan for DirecTV + Roku devices |
| `GET /api/probe?ip=10.1.10.88` | Probe single IP for device type |

- Uses ThreadPoolExecutor with 50 workers for parallel scanning
- Probes port 8080 (DirecTV SHEF) and port 8060 (Roku ECP) on each IP
- Returns device info: type, IP, channel/callsign (DTV) or name/model/serial (Roku)

### `config_manager.py` — TV Config CRUD (Blueprint, prefix `/api`)
| Route | Method | Purpose |
|-------|--------|---------|
| `/api/config` | GET | Get full TV config |
| `/api/config` | POST | Save full TV config |
| `/api/config/tv` | POST | Add/update single TV pairing |
| `/api/config/tv/<name>` | DELETE | Delete TV pairing |
| `/api/config/test` | GET | Test connectivity to all paired TVs |

**Config model** (`/opt/tv_control/data/tv_config.json`):
```json
{
  "location": "Chatham",
  "subnet": "10.1.10",
  "tvs": [
    {
      "name": "DR Middle",
      "roku_ip": "10.1.10.154",
      "dtv_ip": "10.1.10.93",
      "hdmi_port": 1,
      "enabled": true
    }
  ]
}
```
Currently only DR Middle is paired. Other TVs need IPs entered.

### Templates
- **`control.html`** — TV control PWA: shows TV strip at top, sports guide with one-touch tune, virtual DirecTV remote panel, streaming app launcher
- **`settings.html`** — Network scan UI, TV pairing interface, connectivity testing
- **`specials.html`** — Full-screen chalkboard-style daily specials display (for portrait TV behind bar)
- **`specials_edit.html`** — Phone-friendly editor for updating specials

### Static Assets
- PWA manifest, apple-touch-icon, 192/512 icons

---

## Data Files

### `/opt/rednun/data/`
| File | Purpose | Updated By |
|------|---------|------------|
| `sports_guide.json` | Current scraped guide (authoritative source) | FANZO scraper (daily 5AM) |
| `odds.json` | Cached betting odds | ESPN odds fetcher (every 2hrs) |
| `section_order.json` | User's section display order | Drag-and-drop in guide UI |
| `tvs.json` | Staff app TV config (6 TVs, IPs) | Staff Manage TVs form |
| `lights.json` | Light zone states | Staff Venue tab |

### `/opt/tv_control/data/`
| File | Purpose | Updated By |
|------|---------|------------|
| `tv_config.json` | TV pairing config (name + roku_ip + dtv_ip + hdmi) | Settings page |
| `sports_guide.json` | **Stale copy** — fetched from dashboard, not directly scraped | `/api/guide` endpoint |
| `specials.json` | Daily specials content | Specials editor |

**Important**: The authoritative sports guide data lives at `/opt/rednun/data/sports_guide.json`. The copy at `/opt/tv_control/data/sports_guide.json` is a cache that gets refreshed when `/api/guide` is called (falls back to fetching from `dashboard.rednun.com`).

---

## Environment Variables

### Dashboard (`/opt/rednun/`)
```
GMAIL_ADDRESS=...              # For FANZO auto-auth (Gmail IMAP)
GMAIL_APP_PASSWORD=...         # Gmail app password
FANZO_SESSION_COOKIE=...       # Auto-cached by scraper
ODDS_API_KEY=...               # The Odds API (optional, UFC only)
```

### TV Control (`/opt/tv_control/`)
```
TV_CONTROL_PORT=5000           # Set in systemd unit
DASHBOARD_URL=https://dashboard.rednun.com  # For guide data fallback
SECRET_KEY=rednun-tv-control-2026
```

---

## Network

- **Subnet**: `10.1.10.x` (Comcast modem at Chatham)
- All TV device IPs are **DHCP-reserved** in the Comcast modem (static assignments)
- DirecTV receivers: port 8080 (SHEF, GET-based)
- Roku TVs: port 8060 (ECP, POST-based for commands, GET for queries)
- Beelink SER5 runs both Flask apps and is on the same subnet

---

## Known Issues & TODOs

### Bugs
1. **`fetch_all_odds` import** — `server.py` scheduler job references it but import may fail if not loaded. Should verify: `from sports_guide.espn_odds_fetcher import fetch_all_odds`
2. **`staff.pyy` duplicate file** — Old backup at `/opt/rednun/staff/staff.pyy`, can be deleted
3. **`sports_guide.html:351`** — Misplaced `</div>` inside channels loop

### Incomplete / Stubbed
1. **TV IP addresses** — Only DR Middle is fully paired. Other 5 TVs need IPs entered via Manage TVs or Settings page
2. **Lutron Caseta lights** — `staff.py` has `# TODO: implement Lutron Caseta telnet proxy to Smart Bridge on Beelink`. Currently saves state to JSON but doesn't control physical lights
3. **Board power toggle** — `staff.html` has `// TODO: POST to /api/tv-power when ADB endpoint is built`
4. **Stale guide data on TV Control `/api/guide`** — `/opt/tv_control/data/sports_guide.json` is a cached copy used only by the `/api/guide` JSON endpoint, can go stale. The sports_bp blueprint routes (`/guide`, `/sports`, etc.) read directly from `/opt/rednun/data/sports_guide.json` and are always current
5. **sys.path dependency** — TV Control app depends on `/opt/rednun/venv/lib/python3.12/site-packages` for `bs4` and `pytz`. If the rednun venv Python version changes, the path in `app.py` line 17 must be updated

### Not Started
- **Sonos/music control** — "Coming soon" placeholder in staff Venue tab
- **Dennis Port thermostat** — Only Chatham wired currently
- **IR blaster fallback** — For non-networked legacy TVs
- **DirecTV channel database** — Lookup: network name → DTV channel number (so staff can tap "ESPN" and it maps to channel 206)

---

## Deployment

```bash
# Dashboard
sudo systemctl restart rednun
# or: sudo systemctl status rednun

# TV Control
sudo systemctl restart rednun-tv
# or: sudo systemctl status rednun-tv
```

**Note**: `/opt/tv_control/` is owned by root. Edits require `sudo`. The dashboard at `/opt/rednun/` is owned by user `rednun`.

---

## File Map

```
/opt/rednun/                           # Dashboard (port 8080, user: rednun)
├── server.py                          # Main Flask app, registers all blueprints
├── sports_guide/
│   ├── __init__.py                    # Exports sports_bp, scrape_fanzo_guide, load_sports_data
│   ├── sports.py                      # Routes: /sports, /guide, /sports/embed, /sports/api/*
│   ├── fanzo_scraper.py               # FANZO scraper + Gmail auto-auth + start_ts
│   ├── fanzo_config.py                # Favorite teams, streaming services config
│   ├── espn_odds_fetcher.py           # ESPN odds (primary, free)
│   ├── odds_fetcher.py                # The Odds API (backup, paid)
│   ├── team_logos.py                  # Team/league logo URL maps
│   ├── templates/
│   │   ├── sports_guide.html          # Main guide template (~900 lines)
│   │   ├── sports_embed.html          # Embeddable version
│   │   └── sports_guide_backup.html
│   └── static/                        # PWA manifest, icons, service worker
├── staff/
│   ├── staff.py                       # Routes: /staff, TV config, DTV/Roku control, lights
│   ├── templates/
│   │   └── staff.html                 # Staff PWA (~72KB, TVs/Specials/Venue tabs)
│   └── static/                        # PWA manifest, icons
├── data/
│   ├── sports_guide.json              # Authoritative scraped guide data
│   ├── odds.json                      # Cached betting odds
│   ├── section_order.json             # Section display order
│   ├── tvs.json                       # Staff TV config (6 TVs with IPs)
│   └── lights.json                    # Light zone states
└── venv/                              # Python virtual environment

/opt/tv_control/                       # TV Control (port 5000, user: root)
├── app.py                             # Main Flask app, registers blueprints
├── proxy.py                           # DirecTV SHEF + Roku ECP proxy routes
├── scanner.py                         # Network scanner (ThreadPoolExecutor, 50 workers)
├── config_manager.py                  # TV pairing config CRUD + connectivity test
├── templates/
│   ├── control.html                   # TV control PWA (remote, guide, one-touch tune)
│   ├── settings.html                  # Network scan + TV pairing UI
│   ├── specials.html                  # Full-screen chalkboard specials display
│   └── specials_edit.html             # Phone-friendly specials editor
├── static/                            # PWA manifest, icons
├── data/
│   ├── tv_config.json                 # TV pairing config (currently 1 TV paired)
│   ├── sports_guide.json              # Stale cache of guide data
│   └── specials.json                  # Daily specials content
└── venv/                              # Python virtual environment
```

---

## How the Two Apps Relate

1. **Dashboard** is the authoritative source for sports guide data (FANZO scraper runs here)
2. **TV Control imports the sports_guide blueprint** directly from `/opt/rednun/sports_guide/` via `sys.path` — so both apps serve the same rendered guide HTML from the same code and same data file
3. **TV Control also has `/api/guide`** (raw JSON) which caches locally and falls back to `dashboard.rednun.com` — this is a separate, older endpoint
4. **Both apps** can control TVs — Dashboard's staff module has its own DTV/Roku proxy endpoints, while TV Control has a more full-featured proxy with network scanning and a virtual remote
5. **Staff PWA** (`/staff` on Dashboard) is the primary interface for day-to-day "tap game → tune TV" workflow
6. **TV Control PWA** (`/` on port 5000) is the more technical interface with full remote control, network scanning, and device pairing
7. **Specials** exist in both: Dashboard has `specials_routes.py` (database-backed), TV Control has `specials.json` (file-backed chalkboard display)

### Data Flow: Game → TV
```
FANZO website
  → fanzo_scraper.py (Gmail auto-auth, daily 5AM, runs from /opt/rednun/)
  → /opt/rednun/data/sports_guide.json (single source of truth)
  → Dashboard reads it directly (staff PWA /staff, guide /sports, /guide)
  → TV Control reads it directly (sports_bp imported via sys.path — /guide, /sports on port 5000)
  → TV Control /api/guide also reads it as JSON (with stale-cache fallback to dashboard.rednun.com)
  → Staff taps game → picks TV
  → POST /staff/api/dtv/tune (or GET /api/dtv/tune on port 5000)
  → GET http://{dtv_ip}:8080/tv/tune?major={channel}
  → DirecTV receiver changes channel

For streaming games:
  → POST /staff/api/roku/command {command: "launch_app", app_id: "34376"}
  → POST http://{roku_ip}:8060/launch/34376
  → Roku TV opens ESPN+ app
```
