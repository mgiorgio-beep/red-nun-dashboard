#!/usr/bin/env python3
"""Populate session_journal.json with historical sessions 0-28."""
import json, os

journal_path = '/opt/red-nun-dashboard/session_journal.json'

# Load existing journal
try:
    with open(journal_path) as f:
        journal = json.load(f)
except:
    journal = {"sessions": []}

if "sessions" not in journal:
    journal["sessions"] = []

# Build historical sessions from project summaries
historical = [
    {
        "session": 0,
        "date": "2026-02-22",
        "title": "Audit Cleanup",
        "summary": "Removed 10 .bak files, fixed port collision docs. Full server audit: 102 passed, 17 warnings, 2 failures. analytics.py line 331 false positive confirmed safe.",
        "files_changed": ["audit_dashboard.py", "CLAUDE.md"]
    },
    {
        "session": 1,
        "date": "2026-02-22",
        "title": "AI Inventory DB Tables",
        "summary": "Created ai_inventory_sessions, ai_inventory_items, ai_inventory_history tables. Created inventory_intake/ folder. Documented existing inventory_routes.py (1,045 lines).",
        "files_changed": ["inventory_ai_routes.py", "data_store.py"]
    },
    {
        "session": 2,
        "date": "2026-02-22",
        "title": "Audio Engine",
        "summary": "Built Whisper transcription + Claude Haiku parsing + rapidfuzz product matching. 7/7 self-test items matched.",
        "files_changed": ["inventory_ai_audio.py"]
    },
    {
        "session": 3,
        "date": "2026-02-22",
        "title": "Vision Engine",
        "summary": "Claude Sonnet Vision frame extraction, 40-frame cap, deduplication, scale reading with bottle_weights lookup.",
        "files_changed": ["inventory_ai_vision.py"]
    },
    {
        "session": 4,
        "date": "2026-02-22",
        "title": "Reconciliation Engine",
        "summary": "Merges audio+vision streams, confidence scoring, cross-references 30-day history, flags conflicts.",
        "files_changed": ["inventory_ai_reconcile.py"]
    },
    {
        "session": 5,
        "date": "2026-02-22",
        "title": "Routes + UI",
        "summary": "7 API endpoints, mobile upload zone, processing overlay, item review cards grouped by location, history tab.",
        "files_changed": ["inventory_ai_routes.py", "static/ai_inventory.html"]
    },
    {
        "session": 6,
        "date": "2026-02-22",
        "title": "Hardening",
        "summary": "DELETE endpoint, double-confirm guard, timeout handling, ffmpeg error handling, 500MB upload limit. 83/83 tests pass.",
        "files_changed": ["inventory_ai_routes.py"]
    },
    {
        "session": "Polish-1",
        "date": "2026-02-22",
        "title": "Sidebar Reorganization",
        "summary": "Products section split out, nav fallback fixes, dashboard thermometer row repositioned inside view-dashboard. Default view fixed to dashboard.",
        "files_changed": ["static/sidebar.js", "static/manage.html"]
    },
    {
        "session": "Polish-2",
        "date": "2026-02-22",
        "title": "Analytics Cleanup",
        "summary": "Revenue + Servers tabs removed. Recipe Analysis and Recipe Viewer Coming Soon divs added. Menu Analysis blank page fixed.",
        "files_changed": ["static/index.html", "static/sidebar.js", "static/manage.html"]
    },
    {
        "session": "Polish-3",
        "date": "2026-02-22",
        "title": "Sidebar Active States",
        "summary": "Fixed pending/history key mismatch, cross-page nav to /invoices lost tab context, showView wrapper was path-unaware. Cleaned 40+ .bak files.",
        "files_changed": ["static/sidebar.js"]
    },
    {
        "session": "Polish-4",
        "date": "2026-02-22",
        "title": "Labor Fix + Price Movers",
        "summary": "Labor blank page fixed — raw_json column conflict in time_entries queries. Price Movers card added to dashboard using UNION of scanned + ME data. Real movers: Squash +71%, Cucumber +67%, Mozzarella -50%.",
        "files_changed": ["analytics.py", "static/manage.html"]
    },
    {
        "session": 7,
        "date": "2026-02-22",
        "title": "Local File Watcher + Invoice Confirmation",
        "summary": "Built local_invoice_watcher.py watching invoice_images/ folder. Auto-processes new files via manifest. Confirmed 11 pending invoices.",
        "files_changed": ["local_invoice_watcher.py"]
    },
    {
        "session": 8,
        "date": "2026-02-22",
        "title": "Email Invoice Intake",
        "summary": "Built email_invoice_poller.py — Gmail API OAuth poller. Saves attachments to invoice_images/. Dashboard badge shows pending invoice count.",
        "files_changed": ["email_invoice_poller.py"]
    },
    {
        "session": 9,
        "date": "2026-02-22",
        "title": "Payment Tracking + Order Guide",
        "summary": "mark_invoice_paid(), outstanding invoices endpoint, payment filter pills, color-coded invoice list, Outstanding tab. Order Guide: /order-guide page grouping by vendor, highlights cheapest. Test: mushrooms Reinhart $23.51.",
        "files_changed": ["invoice_routes.py", "static/invoices.html", "order_guide_routes.py"]
    },
    {
        "session": 10,
        "date": "2026-02-22",
        "title": "Beelink Migration Prep + Chalkboard Specials",
        "summary": "DDNS script for Cloudflare API. Migration runbook (15 steps). Digital chalkboard: specials_admin.html + chalkboard_specials_portrait.html (Fredericka font, auto-refresh, drag-sort).",
        "files_changed": ["ddns_update.py", "migration_runbook.txt", "static/specials_admin.html", "static/chalkboard_specials_portrait.html"]
    },
    {
        "session": 11,
        "date": "2026-02-22",
        "title": "Price Alerts + Product Name Normalization",
        "summary": "product_name_mapper.py with rapidfuzz WRatio, abbreviation expansion, shares_key_token guard. 20 auto-mapped food items. Price alerts working: Onion +79.2%, Lettuce +139.4%. Invoice image lightbox.",
        "files_changed": ["product_name_mapper.py", "invoice_routes.py", "static/invoices.html"]
    },
    {
        "session": 12,
        "date": "2026-02-22",
        "title": "Invoice Location Filter + Page Refresh Bug",
        "summary": "Invoice list filters by location (Chatham 42 / Dennis Port 77). Pending Review no longer blank after F5.",
        "files_changed": ["static/invoices.html", "invoice_routes.py"]
    },
    {
        "session": 13,
        "date": "2026-02-22",
        "title": "Auto-Populate Product Setup from Invoices",
        "summary": "pack_size column added to invoice items. OCR extracts pack size. populate_product_setup_from_items() fills products on confirm. Backfill: 428 price updates, 13 pack sizes, 1574/1619 products priced.",
        "files_changed": ["invoice_processor.py", "invoice_routes.py", "backfill_product_setup.py"]
    },
    {
        "session": "Hotfix-1",
        "date": "2026-02-23",
        "title": "Product Setup Rewire",
        "summary": "Product Setup rewired to write to products table (not product_inventory_settings). Products now visible after invoice confirm.",
        "files_changed": ["invoice_routes.py"]
    },
    {
        "session": "Hotfix-2",
        "date": "2026-02-23",
        "title": "OCR Self-Validation + Auto-Confirm",
        "summary": "Claude Vision extracts item count and totals, validates against invoice, auto-confirms if match. Auto-confirmed badge shown in invoice list.",
        "files_changed": ["invoice_processor.py", "invoice_routes.py"]
    },
    {
        "session": "Hotfix-3",
        "date": "2026-02-23",
        "title": "New Categories + Thumbnails",
        "summary": "3 new categories: TOGO_SUPPLIES, DR_SUPPLIES, KITCHEN_SUPPLIES. Google Drive auto-delete after download. Invoice thumbnails via pdf2image — 46 generated.",
        "files_changed": ["invoice_processor.py", "invoice_routes.py"]
    },
    {
        "session": 14,
        "date": "2026-02-23",
        "title": "Clean Slate Data Reset",
        "summary": "All stale ME/invoice/product data wiped. Schemas preserved. DB vacuumed. 20 invoice images archived. Backup: toast_data_pre_reset_20260223_1658.db.",
        "files_changed": ["toast_data.db"]
    },
    {
        "session": 15,
        "date": "2026-02-23",
        "title": "Auto-Detect Invoice Location from OCR",
        "summary": "ship_to_address added to OCR prompt. detect_location_from_address() — ZIP/street/town matching, 9/9 tests pass. Location auto-selects in review UI with auto-detected badge.",
        "files_changed": ["invoice_processor.py", "invoice_routes.py", "static/invoices.html"]
    },
    {
        "session": 16,
        "date": "2026-02-26",
        "title": "Seamless Inventory Upload",
        "summary": "upload_token column on ai_inventory_sessions. 6 new API endpoints for session management. ai_inventory.html rebuilt with 3 states (start/waiting/review). iOS Shortcut integration. Disk freed 2.7GB.",
        "files_changed": ["inventory_ai_routes.py", "static/ai_inventory.html"]
    },
    {
        "session": "Token-Leak-Audit",
        "date": "2026-02-27",
        "title": "Token Leak Audit + Fix",
        "summary": "~$200 burned by local_invoice_watcher.py retrying 15 invoices every 5min (4320 calls/day). All invoice watcher cron jobs disabled. Invoice processing now upload-only via dashboard UI.",
        "files_changed": ["crontab"]
    },
    {
        "session": 15,
        "date": "2026-02-28",
        "title": "Product Costing Clean Rebuild",
        "summary": "NEW TABLE: product_costing (case_price / units_per_case formula). 243 products backfilled. product_costing_routes.py with 4 endpoints. Product Setup UI rebuilt. Recipe editor updated. Invoice confirm wired to update prices. 15/15 acceptance tests passed.",
        "files_changed": ["product_costing_routes.py", "static/manage.html", "invoice_routes.py"]
    },
    {
        "session": "Product-Mapping-Rebuild",
        "date": "2026-03-01",
        "title": "Product Mapping Rebuild",
        "summary": "NEW TABLE: vendor_item_links. canonical_product_name column added to scanned_invoice_items. Rebuilt /product-mapping with ME-style UI (Suggested/Unlinked/Linked tabs). 5-rule fuzzy matching: 8 clean auto-links, 0 bad matches. 62 canonical products seeded from Red Nun menu. is_canonical flag added.",
        "files_changed": ["mapping_routes.py", "static/manage.html", "data_store.py"]
    },
    {
        "session": 28,
        "date": "2026-03-02",
        "title": "First Real Inventory Walk + Beelink Migration",
        "summary": "Beelink SER5 arrived at Chatham. Flask app migrated from DigitalOcean (destroyed). New IP: 174.180.119.126, local: 10.1.10.83. Let's Encrypt SSL. Cloudflare Full strict mode. Gunicorn timeout 30s->300s, workers 1->2. Glasses recorded 50-second bar walk. Upload blocked by Cloudflare 100MB limit on 108MB video.",
        "files_changed": ["server.py", "/etc/nginx/sites-enabled/rednun", "/etc/systemd/system/rednun.service"]
    },
    {
        "session": "30A",
        "date": "2026-03-04",
        "title": "WireGuard VPN + Samba Share",
        "summary": "WireGuard VPN installed on Beelink (wg0, UDP 51820, 10.8.0.0/24). Client config generated for home Windows machine. Samba share configured: //10.1.10.83/invoices → /opt/red-nun-dashboard/invoice_images. ScanSnap can scan directly to share over LAN.",
        "files_changed": ["/etc/wireguard/wg0.conf", "/etc/samba/smb.conf"]
    },
    {
        "session": "30B",
        "date": "2026-03-04",
        "title": "Gmail Poller Re-enabled + Auto-Scan",
        "summary": "Gmail email poller re-enabled after token leak audit confirmed safety. Auto-scan feature added: poller calls /api/invoices/scan after saving each file. Log path fixed. Manifest cleared. Thumbnail display added to invoice history cards and review screen.",
        "files_changed": ["email_invoice_poller.py", "invoice_routes.py", "static/invoices.html"]
    },
    {
        "session": "30C",
        "date": "2026-03-05",
        "title": "PDF Multi-Page Split Fix",
        "summary": "poppler-utils installed. PDF page split added to invoice_routes.py (convert_from_bytes with poppler_path). Email poller got page split code. mime_type detection moved before auto_rotate. Result: 32/33 items from US Foods 2-page PDF.",
        "files_changed": ["invoice_routes.py", "email_invoice_poller.py", "invoice_processor.py"]
    },
    {
        "session": "30D",
        "date": "2026-03-05",
        "title": "ZIP + Landscape + Auto-Orient Fix",
        "summary": "Gmail reports ScanSnap ZIPs as application/octet-stream with garbled filenames — added filename-based fallback detection. Multi-page JPEG scan from ZIP sent as one scan request. Auto-orient for upside-down pages. Landscape rotation issue fixed (was rotating portrait incorrectly). Both PDF and JPEG paths now get 32/33 items. PDF path: $13 gap. Poppler path confirmed working.",
        "files_changed": ["email_invoice_poller.py", "invoice_routes.py"]
    },
    {
        "session": "30E",
        "date": "2026-03-05",
        "title": "IIF Parser Investigation (current)",
        "summary": "Discovered US Foods emails IIF (QuickBooks interchange) files with perfect structured data — no OCR needed. Testing if email poller can parse IIF directly for 100% accurate invoice import.",
        "files_changed": []
    }
]

# Check which sessions already exist
existing_sessions = {str(s.get('session', '')) for s in journal['sessions']}
added = 0
for s in historical:
    key = str(s['session'])
    if key not in existing_sessions:
        journal['sessions'].append(s)
        added += 1
        print(f"Added session {s['session']}: {s['title']}")
    else:
        print(f"Skipped session {s['session']} (already exists)")

# Sort by date then session
journal['sessions'].sort(key=lambda x: (x.get('date',''), str(x.get('session',''))))

with open(journal_path, 'w') as f:
    json.dump(journal, f, indent=2)

print(f"\nDone — added {added} sessions. Total: {len(journal['sessions'])}")
