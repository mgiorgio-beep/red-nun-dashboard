#!/usr/bin/env bash
# resolve_claude_md.sh
# Clears the three ⚠️ VERIFY flags in CLAUDE.md by detecting what's actually
# true on this Beelink, then patching the file in place.
#
# Run from the repo root, e.g.:
#   cd /opt/red-nun-dashboard && bash resolve_claude_md.sh
# or
#   cd /opt/rednun && bash resolve_claude_md.sh
#
# It will:
#   1) Figure out the real repo path (where this script is run from)
#   2) Figure out the real venv path by reading the systemd unit
#   3) Count lines in web/static/manage.html
#   4) Detect how deploys actually happen (git remote + service ExecStart)
#   5) Rewrite the three VERIFY blocks in CLAUDE.md
#
# Safe to re-run. Makes a timestamped backup of CLAUDE.md before editing.

set -euo pipefail

CLAUDE_MD="CLAUDE.md"

if [[ ! -f "$CLAUDE_MD" ]]; then
  echo "ERROR: $CLAUDE_MD not found in $(pwd)"
  echo "Run this from the repo root (where CLAUDE.md lives)."
  exit 1
fi

echo "═══════════════════════════════════════════"
echo "  Resolving VERIFY flags in CLAUDE.md"
echo "═══════════════════════════════════════════"
echo ""

# ─── 1. Repo path ──────────────────────────────────────────────────────────
REPO_PATH="$(pwd)"
echo "1. Repo path:       $REPO_PATH"

# ─── 2. Venv path (read from systemd unit) ─────────────────────────────────
UNIT_FILE="/etc/systemd/system/rednun.service"
if [[ ! -f "$UNIT_FILE" ]]; then
  UNIT_FILE="$(systemctl show -p FragmentPath rednun 2>/dev/null | cut -d= -f2 || true)"
fi

VENV_PATH="UNKNOWN"
EXEC_START=""
if [[ -n "$UNIT_FILE" && -f "$UNIT_FILE" ]]; then
  EXEC_START="$(grep -E '^ExecStart=' "$UNIT_FILE" | head -1 | sed 's/^ExecStart=//')"
  # Pull the first path-looking token (the interpreter/gunicorn binary)
  BIN_PATH="$(echo "$EXEC_START" | awk '{print $1}')"
  if [[ "$BIN_PATH" == */bin/* ]]; then
    VENV_PATH="${BIN_PATH%/bin/*}"
  fi
fi
echo "2. Venv path:       $VENV_PATH"
echo "   (from: $UNIT_FILE)"

# ─── 3. manage.html line count ─────────────────────────────────────────────
MANAGE_HTML="web/static/manage.html"
if [[ -f "$MANAGE_HTML" ]]; then
  MANAGE_LINES="$(wc -l < "$MANAGE_HTML" | tr -d ' ')"
else
  MANAGE_LINES="UNKNOWN (web/static/manage.html not found)"
fi
echo "3. manage.html:     $MANAGE_LINES lines"

# ─── 4. Deploy flow detection ──────────────────────────────────────────────
GIT_REMOTE="$(git remote get-url origin 2>/dev/null || echo 'none')"
echo "4. Git remote:      $GIT_REMOTE"

# If there's a git remote and the repo has recent pulls, assume push/pull flow.
if [[ "$GIT_REMOTE" == *github.com* ]]; then
  DEPLOY_STYLE="git-push-pull"
else
  DEPLOY_STYLE="direct-edit"
fi
echo "   Deploy style:    $DEPLOY_STYLE"
echo ""

# ─── 5. Backup + patch ─────────────────────────────────────────────────────
BACKUP="CLAUDE.md.bak.$(date +%Y%m%d_%H%M%S)"
cp "$CLAUDE_MD" "$BACKUP"
echo "Backed up to:       $BACKUP"
echo ""

python3 - "$CLAUDE_MD" "$REPO_PATH" "$VENV_PATH" "$MANAGE_LINES" "$DEPLOY_STYLE" <<'PYEOF'
import re, sys
path, repo, venv, lines, deploy = sys.argv[1:6]

with open(path, 'r', encoding='utf-8') as f:
    text = f.read()

# ── Flag 1: Server repo path block ────────────────────────────────────────
# Replace the whole "VERIFY ON NEXT SESSION" blockquote about the repo path
pat1 = re.compile(
    r"> ⚠️ \*\*VERIFY ON NEXT SESSION:\*\* The repo path.*?possible to fix it\.\n?",
    re.DOTALL,
)
# Fallback matcher (tolerant of wording drift)
pat1b = re.compile(
    r"> ⚠️ \*\*VERIFY ON NEXT SESSION:\*\*.*?\n\n",
    re.DOTALL,
)

replacement1 = (
    f"Repo lives at `{repo}`. Venv at `{venv}/bin/python3`.\n\n"
)

new_text, n1 = pat1.subn(replacement1, text)
if n1 == 0:
    new_text, n1 = pat1b.subn(replacement1, text)

# Also fix the "Server:" line in the code block at the top if it's wrong
new_text = re.sub(
    r"Server:\s+/opt/red-nun-dashboard\s+\(Beelink SER5, Chatham\)",
    f"Server:  {repo}        (Beelink SER5, Chatham)",
    new_text,
)

# ── Flag 2: Deploy workflow VERIFY ────────────────────────────────────────
if deploy == "git-push-pull":
    deploy_block = (
        "### Deploy workflow\n"
        "```\n"
        "1. Edit locally\n"
        "2. git add / git commit / git push\n"
        f"3. On server: cd {repo} && git pull && sudo systemctl restart rednun\n"
        "```\n"
    )
else:
    deploy_block = (
        "### Deploy workflow\n"
        "Edit directly on the Beelink, then:\n"
        "```\n"
        "sudo systemctl restart rednun\n"
        "```\n"
        "(No separate push/pull step — this is the working copy.)\n"
    )

pat2 = re.compile(
    r"### Deploy workflow\n"
    r"> ⚠️ \*\*VERIFY:\*\*.*?(?=\n---|\n## )",
    re.DOTALL,
)
new_text, n2 = pat2.subn(deploy_block, new_text)

# ── Flag 3: manage.html line count ────────────────────────────────────────
pat3 = re.compile(
    r"`web/static/manage\.html` — Main dashboard SPA \(product management, [^)]*\)"
)
replacement3 = f"`web/static/manage.html` — Main dashboard SPA (product management, ~{lines} lines)"
new_text, n3 = pat3.subn(replacement3, new_text)

# ── Fix venv path line further down ───────────────────────────────────────
new_text = re.sub(
    r"Python venv: `/opt/rednun/venv/bin/python3` \*\(verify path[^)]*\)\*",
    f"Python venv: `{venv}/bin/python3`",
    new_text,
)

with open(path, 'w', encoding='utf-8') as f:
    f.write(new_text)

print(f"  Flag 1 (repo/venv paths):  {'patched' if n1 else 'NOT FOUND — check manually'}")
print(f"  Flag 2 (deploy workflow):  {'patched' if n2 else 'NOT FOUND — check manually'}")
print(f"  Flag 3 (manage.html lines):{'patched' if n3 else 'NOT FOUND — check manually'}")
PYEOF

echo ""
echo "═══════════════════════════════════════════"
echo "  Done. Review with:"
echo "    diff $BACKUP $CLAUDE_MD"
echo "  Then commit:"
echo "    git add $CLAUDE_MD"
echo "    git commit -m 'Resolve VERIFY flags in CLAUDE.md'"
echo "═══════════════════════════════════════════"
