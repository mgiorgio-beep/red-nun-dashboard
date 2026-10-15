"""
Update the two Drive plan files for the 2026-10-12 .. 10-15 work. DRY RUN unless --apply.

The files live in the Drive folder, which rclone bisyncs with ~/cowork/red-nun-dashboard
on Chatham, so editing them there updates Drive.

  UI_NAV_REVIEW_AND_PLAN_2026-10-08.md  append docs/plan_updates/UI_PLAN_2026-10-15.md
  MASTER_PLAN.md                        US Foods Capt Parker's/Lanyard row: ACTIVE -> DONE
                                        (pickup orders billed to Red Nun, not someone else's);
                                        DONE row for UI plan Phases 0-5; D14 (Sprague /
                                        UniFirst account numbers); changelog line

Each change is skipped if it is already there, so a second run does nothing.
--apply writes a .bak next to each file first.

  cd /opt/red-nun-dashboard && venv/bin/python3 scripts/plan_update_2026_10_15.py           # show
  venv/bin/python3 scripts/plan_update_2026_10_15.py --apply                                # do it
"""
import argparse
import os
import shutil
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI_TEXT = os.path.join(REPO, "docs", "plan_updates", "UI_PLAN_2026-10-15.md")
UI_MARKER = "## Done 2026-10-12: Phase 4, batch 2"

USF_ROW_START = "| 🔴 **Other restaurants' US Foods invoices in Red Nun's books**"
DONE_HEADER = "## ✅ DONE"
DONE_ROWS = [
    ("**US Foods Capt Parker's / Lanyard invoices are OURS** — the 12 invoices ($1,791.81) ship-to "
     "CAPT PARKERS PUB / LANYARD were pickup orders billed to Red Nun (dropped at a nearby restaurant "
     "when we can't make the delivery minimum). Nothing to remove. Bill-to is now captured on intake; "
     "`scripts/foreign_shipto_invoices.py` lists ship-to-elsewhere invoices for a look", "10/14"),
    ("**Dashboard UI plan Phases 0–5 done** — standard page header, phone cards, one location picker, "
     "one vendor screen (vendor list merged 10/15), Bev/Food Cost rebuilt, login on open data routes. "
     "See `UI_NAV_REVIEW_AND_PLAN_2026-10-08.md`", "10/15"),
]
DEC_HEADER = "## ❓ DECISIONS"
DEC_ROW = ("| D14 | Account numbers that disagree between the old vendor lists — **Sprague** 72009969 vs "
           "9534366007, **UniFirst** 1500286 vs 1231141. Which is right? (fix in Setup → Vendors) | 10/15 |")
CHANGELOG = ("- 2026-10-15 — Dashboard UI plan Phases 4–5 finished (page header, phone cards, one location "
             "picker, one vendor screen; vendor list merged on Chatham, backup "
             "`/opt/backups/toast_data_20261015_0954.db`). Bev/Food Cost rebuilt, open data routes now need a "
             "login. US Foods Capt Parker's/Lanyard item closed (pickup orders, ours). D14 added.")


def find_file(folder, name):
    for root, _, files in os.walk(folder):
        if name in files:
            return os.path.join(root, name)
    raise SystemExit(f"{name} not found under {folder}")


def table_end(lines, header_prefix):
    """Index just past the last row of the first table after the header."""
    i = next((k for k, l in enumerate(lines) if l.startswith(header_prefix)), None)
    if i is None:
        raise SystemExit(f"section '{header_prefix}' not found")
    while i < len(lines) and not lines[i].startswith("|"):
        i += 1
    while i < len(lines) and lines[i].startswith("|"):
        i += 1
    return i


def update_master(text):
    lines, notes = text.split("\n"), []
    usf = [k for k, l in enumerate(lines) if l.startswith(USF_ROW_START)]
    if usf:
        del lines[usf[0]]
        notes.append("remove US Foods Capt Parker's/Lanyard row from ACTIVE")
    for item, date in DONE_ROWS:
        if item[:40] in text:
            continue
        i = table_end(lines, DONE_HEADER)
        lines.insert(i, f"| {item} | {date} |")
        notes.append(f"add DONE row: {item[:60]}…")
    if "| D14 |" not in text:
        lines.insert(table_end(lines, DEC_HEADER), DEC_ROW)
        notes.append("add D14 (Sprague / UniFirst account numbers)")
    if not any(l.startswith("- 2026-10-15 — Dashboard UI plan") for l in lines):
        while lines and not lines[-1].strip():
            lines.pop()
        lines += [CHANGELOG, ""]
        notes.append("add 2026-10-15 changelog line")
    return "\n".join(lines), notes


def main():
    ap = argparse.ArgumentParser(description="Update the Drive plan files (dry run unless --apply).")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--folder", default=os.path.expanduser("~/cowork/red-nun-dashboard"))
    args = ap.parse_args()

    ui_path = find_file(args.folder, "UI_NAV_REVIEW_AND_PLAN_2026-10-08.md")
    mp_path = find_file(args.folder, "MASTER_PLAN.md")
    with open(UI_TEXT, encoding="utf-8") as fh:
        ui_add = fh.read()

    changes = {}
    with open(ui_path, encoding="utf-8") as fh:
        ui = fh.read()
    print(ui_path)
    if UI_MARKER in ui:
        print("   already has the 10/12–10/15 update")
    else:
        changes[ui_path] = ui.rstrip("\n") + "\n" + ui_add
        print(f" * append {ui_add.count(chr(10) + '## ')} sections")

    with open(mp_path, encoding="utf-8") as fh:
        mp = fh.read()
    new_mp, notes = update_master(mp)
    print(mp_path)
    for n in notes or ["   nothing to change"]:
        print((" * " if notes else "") + n)
    if notes:
        changes[mp_path] = new_mp

    if not changes:
        print("\nNothing to do.")
        return
    if not args.apply:
        print("\nDry run — nothing changed. Re-run with --apply.")
        return
    for path, text in changes.items():
        shutil.copy2(path, path + ".bak")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"wrote {path} (backup {path}.bak)")
    print("Drive picks these up on the next rclone bisync.")


if __name__ == "__main__":
    sys.exit(main())
