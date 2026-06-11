# Invoice & TV Reliability Plan — June 2026

Goal: Mike stops double-checking invoices and stops babysitting TVs. Based on the
2026-06-11 audits (Cowork scraper-repo audit + on-box audit by Claude Code).

## The diagnosis in one paragraph

The invoice pipeline has **zero end-to-end reconciliation** — nothing ever compares
"what the vendor portal showed" to "what's in the dashboard," so manual double-checking
is currently the only completeness check that exists. Eight separate paths can lose an
invoice silently (zero-result scrapes that are really page failures, OCR timeouts with
no retry, dedup state desync, parse errors). The seven scrapers are copy-paste forks, so
every portal change breaks one and fixes don't propagate. TV control turned out not to be
a separate app at all — it lives in the Staff App with its self-healing watchdog
**commented out**, which is why it needs manual minding.

## Phase 0 — Stop the bleeding (DONE or in flight, 2026-06-11)

- Auth gate on invoice API (verified 401 from outside) — done
- Auto-deploy pipeline: Cowork pushes → Chatham timer pulls; no more copy-paste — done
- run_all batch stalled since Jun 9 → diagnose + catch-up run — assigned to on-box Claude
- QBO email scraper failing every 15 min (no Gmail token) → disable + document — assigned
- tv_power.py: add adb timeout, fix lock handling, re-enable watchdog — assigned
- Tiger Exchange casing dup in vendors — assigned

## Phase 1 — The trust fix: completeness ledger (build next)

Every scraper already sees the portal's invoice list before downloading. Capture it.

1. Each scraper writes a **manifest** of every invoice it saw (vendor, invoice #, date,
   total) — regardless of whether it downloads, dedups, or skips it.
2. A nightly reconciler compares manifests vs `scanned_invoices` and produces one verdict:
   - "All N invoices seen in portals are in the dashboard ✓"
   - or "MISSING: SG Dennis #12345 $432.10 (seen Jun 10, never imported)"
3. Delivery via the existing Telegram bot + a dashboard badge.
4. Per-vendor **cadence alarms**: each vendor has an expected rhythm (Caron weekly,
   US Foods ~2×/wk). Silence beyond the rhythm = alert, even if no scrape "failed."
5. Orphan sweep: any downloaded file not matched to a DB row within 24h = alert (catches
   the OCR-timeout black hole).

This phase replaces Mike's manual double-checking. Build it before touching anything else.

## Phase 2 — Shrink the scraping surface: EDI (plan already 80% written)

`docs/EDI_SETUP.md` has the full design; parser + watcher + tests already exist as
untracked files. US Foods + PFG ≈ half of invoice volume becomes vendor-pushed SFTP
files — no portal, no Playwright, no OCR, no API spend.

1. Commit the EDI parser/watcher/tests (currently untracked on the server)
2. Stand up the SFTP endpoint per EDI_SETUP.md (on-box Claude, ~1 session)
3. Send the two drafted vendor emails (US Foods: redirect existing TPID ECREDNME;
   PFG: re-sign waiver per location, request credit-memo mode)
4. Cutover per the test plan; scrapers stay as backstop during overlap

## Phase 3 — Harden what remains (liquor + linen vendors)

1. Extract one shared scraper base (login/session/retry/manifest); each vendor becomes a
   thin config + page-logic module instead of a fork
2. Real zero-result detection: distinguish "no new invoices" from "page didn't render"
   (assert on a known page element, not an empty list)
3. L. Knife auth is degrading (works on residual cookies, warns on every run) — fix the
   ADFS popup login before it hard-fails
4. VTInfo/Colonial: portal vendor-picker changed; needs a live portal session to re-map
   selectors (dedicated on-box session)
5. Daily 7am triage: headless `claude -p` reads run_all.log after the batch, classifies
   failures, attempts known fixes, Telegrams one plain-English line

## TV control (fix in place — no rewrite)

The Staff App's TV code is salvageable as-is. After Phase 0's safety fixes + watchdog
re-enable, remaining items, in order of payoff:

1. Replace the fixed 12–24s sleep chains in the specials launch with readiness polling
2. Add 2–3 retries with backoff to Roku/DirecTV commands; surface failures in the staff UI
3. Update CLAUDE.md to reflect reality (no /opt/tv_control on Chatham)

A rewrite is NOT recommended: the hardware integrations (ADB quirks, Roku ECP, DirecTV
SHEF) encode hard-won knowledge, and the failure modes are all fixable in place.

## What we are explicitly NOT doing

- Not rewriting the seven scrapers from scratch (portals change; the fragility is managed
  by Phase 1 visibility + Phase 2 volume reduction, not by prettier code)
- Not putting AI in the critical import path (deterministic code imports invoices; AI
  triages failures)
- Not touching the manual-payment workflow (portal payment scraper stays dead per May
  decision)

## Sequencing

| Order | Work | Where |
|-------|------|-------|
| now | Phase 0 items | on-box Claude (assigned 6/11) |
| next | Phase 1 ledger + reconciler | Cowork builds, pipeline deploys, on-box Claude wires cron |
| then | Phase 2 EDI standup + vendor emails | on-box Claude + Mike sends emails |
| then | Phase 3 hardening, one vendor at a time | either Claude |
| ongoing | TV polish items | Cowork via pipeline |
