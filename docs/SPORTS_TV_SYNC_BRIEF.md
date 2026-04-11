# Red Nun Sports Guide & TV Control — Sync Brief
**Date:** March 16, 2026

---

## WHAT'S BUILT (Working in Production)

### Sports Guide (`/opt/rednun/sports_guide/`)
| Feature | Status | Notes |
|---------|--------|-------|
| FANZO Scraper | DONE | Auto-auth via Gmail IMAP, no manual cookies |
| Daily auto-scrape | DONE | 5:00 AM ET via APScheduler |
| Public guide (`/sports/public`, `/guide`) | DONE | Customer-facing, no nav |
| Staff guide (`/sports`) | DONE | Has refresh button |
| Embed view (`/sports/embed`) | DONE | For iframe on rednun.com |
| Live ESPN scores | DONE | Polls every 45s, live badges, final greying |
| Team logos (NFL/NBA/NHL/MLB/NCAA) | DONE | ESPN CDN |
| League section logos | DONE | Header icons per sport |
| Betting odds (desktop) | DONE | ESPN DraftKings spreads + O/U |
| Odds fetcher | DONE | `espn_odds_fetcher.py`, runs every 2 hours |
| Streaming service merge | DONE | ESPN+/Peacock/AppleTV+/Prime routed to sport sections |
| Favorite team highlighting | DONE | Bruins, Celtics, Red Sox, Patriots, Harvard, Dartmouth, BC, UMass |
| Section drag & drop reorder | DONE | Persists to `section_order.json` |
| Dark mode (mobile) | DONE | Auto 7pm-6am |
| PWA (home screen) | DONE | Manifest, service worker, icons |

### Staff App (`/opt/rednun/staff/`)
| Feature | Status | Notes |
|---------|--------|-------|
| Staff PWA shell (`/staff`) | DONE | iOS-style tabs: TVs, Specials, Venue |
| TVs tab — sports guide display | DONE | Same FANZO data, logos, live scores |
| TVs tab — tap game → TV drawer | DONE | Shows 4 TVs (Bar Left/Center/Right, Dining) |
| Specials tab — CRUD form | DONE | Soup, appetizer, daily specials |
| Specials tab — publish to board | DONE | Posts to `/api/specials` |
| Venue tab — lights UI | DONE | Scene buttons + zone sliders |
| Venue tab — thermostat display | DONE | Reads from `/api/thermostats` (Chatham) |
| Dark mode (TVs panel) | DONE | Auto 7pm-6am |

---

## WHAT'S STUBBED (UI Exists, No Backend)

| Feature | Where | What's Missing |
|---------|-------|----------------|
| **DirecTV channel control** | `staff.html:677` | `tuneTV()` calls `/api/dtv/tune?ip=...&ch=...` but **no backend endpoint exists** |
| **TV IP addresses** | `staff.py:53-56` | All 4 TVs have `"ip": ""` — need real DirecTV receiver IPs |
| **Board power toggle** | `staff.html:1162` | `// TODO: POST to /api/tv-power when ADB endpoint is built` |
| **Lutron Caseta lights** | `staff.py:82` | `// TODO: implement Lutron Caseta telnet proxy to Smart Bridge on Beelink` |

---

## WHAT'S NOT STARTED

| Feature | Planned In | Description |
|---------|-----------|-------------|
| **DirecTV HTTP API** | plan.html | IP-based channel control for DTV receivers |
| **Roku local API** | plan.html | ECP (port 8060) for Roku TVs — app launch, input switching |
| **DirecTV channel database** | plan.html | Lookup: network name → DTV channel number |
| **TV IP management UI** | — | Admin screen to configure TV IPs |
| **Sonos/music control** | staff.html | "Coming soon" placeholder in Venue tab |
| **Dennis Port thermostat** | staff.html | "Coming soon" — only Chatham wired |
| **IR blaster fallback** | — | For non-networked legacy TVs |

---

## KNOWN BUGS / ISSUES

1. **`fetch_all_odds` import missing in `server.py`** — Scheduler job references it but import may fail if not loaded. Should add: `from sports_guide.espn_odds_fetcher import fetch_all_odds`
2. **`staff.pyy` duplicate file** — Old backup with IP check code, can be deleted
3. **`sports_guide.html:351`** — Misplaced `</div><!-- sections-container -->` inside the channels loop (inside `{% for ch in game.channels %}`)

---

## FILE MAP

```
/opt/rednun/
├── server.py                    # Main Flask app — registers sports_bp + staff_bp
├── sports_guide/
│   ├── __init__.py              # Exports sports_bp, scrape_fanzo_guide, load_sports_data
│   ├── sports.py                # Routes: /sports, /guide, /sports/embed, /sports/api/*
│   ├── fanzo_scraper.py         # FANZO scraper + Gmail auto-auth
│   ├── fanzo_config.py          # Favorite teams, streaming services config
│   ├── espn_odds_fetcher.py     # ESPN odds (primary, free)
│   ├── odds_fetcher.py          # The Odds API (backup, paid)
│   ├── team_logos.py            # Team/league logo maps
│   ├── templates/
│   │   ├── sports_guide.html    # Main guide template (907 lines)
│   │   ├── sports_embed.html    # Embeddable version
│   │   └── sports_guide_backup.html
│   └── static/                  # PWA manifest, icons, service worker
├── staff/
│   ├── staff.py                 # Routes: /staff, /staff/api/tvs, /staff/api/lights
│   ├── templates/
│   │   └── staff.html           # Staff PWA (72KB — TVs/Specials/Venue tabs)
│   └── static/                  # PWA manifest, icons
├── data/
│   ├── sports_guide.json        # Current scraped guide (auto-updated daily)
│   ├── odds.json                # Cached betting odds
│   ├── section_order.json       # User's section order preference
│   ├── tvs.json                 # (NOT YET CREATED) TV config with IPs
│   └── lights.json              # (NOT YET CREATED) Light zone states
└── deploy_sports.sh             # Original deploy script (archive)
```

---

## SCHEDULED JOBS

| Job | Schedule | Function |
|-----|----------|----------|
| FANZO scrape | Daily 5:00 AM ET | `scrape_fanzo_guide()` |
| Odds fetch | Every 2hrs (5,7,9...23) | `fetch_all_odds()` |

---

## ENV VARS NEEDED

```
GMAIL_ADDRESS=...                    # For FANZO auto-auth
GMAIL_APP_PASSWORD=...               # Gmail app password
FANZO_SESSION_COOKIE=...             # Auto-cached by scraper
ODDS_API_KEY=...                     # The Odds API (optional, UFC only)
```

---

## TO COMPLETE TV CONTROL (Priority Order)

1. **Get DirecTV receiver IPs** — Scan network or check router for DTV boxes
2. **Build `/api/dtv/tune` endpoint** — DirecTV receivers accept HTTP commands on port 8080
3. **Populate `tvs.json`** — Map TV names to IPs
4. **Wire up `tuneTV()` in staff.html** — Already calls the endpoint, just needs real data
5. **Optional: Roku ECP integration** — HTTP commands on port 8060
6. **Optional: Lutron Caseta proxy** — Telnet to Smart Bridge for real light control

---

## CLAUDE.AI vs CLAUDE CODE GAP

Work was split across sessions:
- **Claude.ai** — Likely did early planning, template design, deploy scripts
- **Claude Code** — Built the scraper rewrite (Feb 16), odds fetcher, staff shell

Key risk: If features were discussed/designed in Claude.ai but not committed to code, they exist only in chat history. This brief captures everything that's actually in the codebase as of today.
