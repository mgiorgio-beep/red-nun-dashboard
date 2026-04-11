#!/usr/bin/env python3
"""
RED NUN DASHBOARD — PRE-BUILD CODE AUDIT
=========================================
Run this on the server before starting inventory build sessions.
Identifies bugs, redundancies, missing tables, and potential issues
that could trip up Claude Code.

Usage:
  cd /opt/rednun && source venv/bin/activate
  python3 audit_dashboard.py

Or paste into Claude Code as the first task:
  "Run this audit script and fix everything it flags before starting inventory work."
"""

import os
import sys
import re
import sqlite3
import importlib
import glob
from datetime import datetime

REDNUN_DIR = '/opt/rednun'
RESULTS = []
FIXES = []

def header(text):
    print(f"\n{'='*70}")
    print(f"  {text}")
    print(f"{'='*70}")

def ok(msg):
    print(f"  ✅ {msg}")
    RESULTS.append(('OK', msg))

def warn(msg):
    print(f"  ⚠️  {msg}")
    RESULTS.append(('WARN', msg))

def fail(msg, fix=None):
    print(f"  ❌ {msg}")
    RESULTS.append(('FAIL', msg))
    if fix:
        print(f"     FIX: {fix}")
        FIXES.append((msg, fix))

def info(msg):
    print(f"  ℹ️  {msg}")

# ──────────────────────────────────────────────────────────────────────
# 1. FILE STRUCTURE & PYTHON FILES
# ──────────────────────────────────────────────────────────────────────
header("1. FILE STRUCTURE")

# Find all Python files
py_files = []
for f in glob.glob(os.path.join(REDNUN_DIR, '*.py')):
    if '/venv/' not in f:
        py_files.append(f)
        
info(f"Found {len(py_files)} Python files in {REDNUN_DIR}")

# Check for main app file
main_app = None
for f in py_files:
    try:
        with open(f) as fh:
            content = fh.read()
            if 'Flask(__name__)' in content or 'Flask( __name__)' in content:
                main_app = f
                info(f"Main Flask app: {f}")
    except:
        pass

if not main_app:
    fail("Cannot find main Flask app file (no Flask(__name__) found)",
         "Search manually: grep -rl 'Flask(' /opt/rednun/*.py")
else:
    ok(f"Main app found: {main_app}")

# Check key files exist
key_files = {
    'invoice_processor.py': 'Invoice OCR pipeline',
    'invoice_routes.py': 'Invoice API routes',
    'batch_ocr.py': 'Batch OCR processor',
    'drive_invoice_watcher.py': 'Google Drive watcher',
    'analytics.py': 'Sales analytics',
    'bottle_weights_seed.py': 'Bottle weights seeder',
    'session_journal.json': 'Session state tracker',
}

for filename, desc in key_files.items():
    path = os.path.join(REDNUN_DIR, filename)
    if os.path.exists(path):
        ok(f"{filename} exists ({desc})")
    else:
        warn(f"{filename} MISSING ({desc})")

# Check static files
static_dir = os.path.join(REDNUN_DIR, 'static')
static_files = {
    'invoices.html': 'Invoice scanner UI',
    'sidebar.js': 'Dynamic sidebar builder',
    'plan.html': 'Project plan page',
    'manage.html': 'Product management page',
}
for filename, desc in static_files.items():
    path = os.path.join(static_dir, filename)
    if os.path.exists(path):
        ok(f"static/{filename} exists ({desc})")
    else:
        warn(f"static/{filename} MISSING ({desc})")


# ──────────────────────────────────────────────────────────────────────
# 2. DATABASE HEALTH
# ──────────────────────────────────────────────────────────────────────
header("2. DATABASE HEALTH")

# Find active database
db_candidates = ['toast_data.db', 'rednun.db']
active_db = None

for db_name in db_candidates:
    db_path = os.path.join(REDNUN_DIR, db_name)
    if os.path.exists(db_path):
        size_mb = os.path.getsize(db_path) / (1024 * 1024)
        info(f"{db_name}: {size_mb:.1f} MB")
        if size_mb > 5:  # Likely the main DB
            active_db = db_path

if not active_db:
    # Fall back to first found
    for db_name in db_candidates:
        db_path = os.path.join(REDNUN_DIR, db_name)
        if os.path.exists(db_path):
            active_db = db_path
            break

if not active_db:
    fail("No database file found!", "Check: ls /opt/rednun/*.db")
else:
    ok(f"Active database: {active_db}")
    
    conn = sqlite3.connect(active_db)
    conn.row_factory = sqlite3.Row
    
    # List all tables
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
    info(f"Tables found: {len(tables)}")
    
    # Check critical tables
    critical_tables = {
        'orders': 'Toast POS orders',
        'product_inventory_settings': 'Product catalog (431+ items)',
        'me_invoices': 'MarginEdge invoices (107+)',
        'me_invoice_items': 'MarginEdge line items (936+)',
    }
    
    for table, desc in critical_tables.items():
        if table in tables:
            count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            ok(f"{table}: {count} rows ({desc})")
        else:
            fail(f"{table} MISSING ({desc})")
    
    # Check tables that may or may not exist
    optional_tables = {
        'scanned_invoices': ('Invoice scanner results', 
            "Run: python3 -c \"from invoice_processor import init_invoice_tables; init_invoice_tables()\""),
        'scanned_invoice_items': ('Scanned invoice line items',
            "Created by init_invoice_tables()"),
        'bottle_weights': ('Liquor bottle tare weights',
            "Run: python3 bottle_weights_seed.py"),
        'inventory_sessions': ('Inventory count sessions — NEW',
            "Will be created in Session 1 of inventory build"),
        'inventory_items': ('Inventory count items — NEW',
            "Will be created in Session 1 of inventory build"),
        'inventory_history': ('Inventory history — NEW',
            "Will be created in Session 1 of inventory build"),
    }
    
    for table, (desc, fix) in optional_tables.items():
        if table in tables:
            count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            ok(f"{table}: {count} rows ({desc})")
        else:
            warn(f"{table} not created yet ({desc}) — FIX: {fix}")
    
    # Check DB size and suggest cleanup
    db_size_mb = os.path.getsize(active_db) / (1024 * 1024)
    if db_size_mb > 500:
        warn(f"Database is {db_size_mb:.0f}MB — consider VACUUM or data cleanup")
    
    # Check for table size breakdown
    info("Table row counts:")
    for table in sorted(tables):
        try:
            count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            if count > 0:
                info(f"  {table}: {count:,} rows")
        except:
            info(f"  {table}: (error reading)")
    
    conn.close()


# ──────────────────────────────────────────────────────────────────────
# 3. manage.html — KNOWN BUGS
# ──────────────────────────────────────────────────────────────────────
header("3. manage.html — KNOWN BUGS")

manage_path = os.path.join(REDNUN_DIR, 'static', 'manage.html')
if os.path.exists(manage_path):
    with open(manage_path, 'r') as f:
        manage_lines = f.readlines()
    
    manage_content = ''.join(manage_lines)
    
    # BUG 1: JS code after </script></body></html>
    found_end = False
    code_after_end = False
    end_line = None
    for i, line in enumerate(manage_lines):
        if '</html>' in line:
            found_end = True
            end_line = i + 1
        elif found_end and line.strip() and not line.strip().startswith('<!--'):
            code_after_end = True
            break
    
    if code_after_end:
        fail(f"JS code found AFTER </html> tag at line {end_line}. "
             f"Settings and Order Guide functions render as raw text on page.",
             f"Move all JS code from after line {end_line} to BEFORE the </script> tag")
    else:
        ok("No code found after </html> tag")
    
    # BUG 2: Backtick syntax errors (confirm` and fetch`)
    backtick_issues = []
    for i, line in enumerate(manage_lines):
        for func in ['confirm`', 'fetch`', 'alert`', 'prompt`']:
            if func in line:
                backtick_issues.append((i+1, line.strip()[:100]))
    
    if backtick_issues:
        for lineno, snippet in backtick_issues:
            fail(f"Backtick syntax error at line {lineno}: {snippet}",
                 f"Replace backtick template literal with parentheses + string concatenation")
    else:
        ok("No backtick syntax errors found")
    
    # BUG 3: Duplicate lines (from previous partial fixes)
    for i in range(len(manage_lines) - 1):
        if (manage_lines[i].strip() and 
            manage_lines[i].strip() == manage_lines[i+1].strip() and
            'const response' in manage_lines[i]):
            fail(f"Duplicate line at {i+1}-{i+2}: {manage_lines[i].strip()[:80]}",
                 "Remove the duplicate line")
    
    # BUG 4: Missing try { blocks around async operations
    for i, line in enumerate(manage_lines):
        if 'await fetch(' in line:
            # Check if there's a try block within the previous 5 lines
            context = ''.join(manage_lines[max(0,i-5):i])
            if 'try' not in context and 'catch' not in context:
                # Check within the function scope
                for j in range(i-1, max(0, i-15), -1):
                    if 'try' in manage_lines[j] or 'function' in manage_lines[j]:
                        break
                else:
                    warn(f"Line {i+1}: await fetch() without try/catch nearby")
    
    info(f"manage.html: {len(manage_lines)} lines total")
else:
    warn("manage.html not found")


# ──────────────────────────────────────────────────────────────────────
# 4. analytics.py — VOIDED/DELETED FILTER CHECK
# ──────────────────────────────────────────────────────────────────────
header("4. analytics.py — VOIDED/DELETED ORDER FILTER")

analytics_path = os.path.join(REDNUN_DIR, 'analytics.py')
if os.path.exists(analytics_path):
    with open(analytics_path, 'r') as f:
        analytics_lines = f.readlines()
    
    analytics_content = ''.join(analytics_lines)
    
    # Find all FROM orders queries
    from_orders_lines = []
    for i, line in enumerate(analytics_lines):
        if 'FROM orders' in line:
            from_orders_lines.append(i + 1)
    
    info(f"Found {len(from_orders_lines)} 'FROM orders' queries at lines: {from_orders_lines}")
    
    # Find all where_clauses initializations
    where_lines = []
    for i, line in enumerate(analytics_lines):
        if 'where_clauses = ' in line or 'where_clauses=' in line:
            where_lines.append((i + 1, line.strip()))
    
    # Check which ones include the voided/deleted filter
    unfiltered = []
    for lineno, content in where_lines:
        if 'where_clauses = []' in content:
            # Check if voided/deleted filter is added shortly after
            context = ''.join(analytics_lines[lineno-1:lineno+5])
            if 'deleted' not in context and 'voided' not in context:
                unfiltered.append(lineno)
    
    filtered_count = len(where_lines) - len(unfiltered)
    
    if unfiltered:
        fail(f"{len(unfiltered)} queries MISSING voided/deleted filter at lines: {unfiltered}",
             "Add to each: where_clauses = [\"json_extract(raw_json, '$.deleted') != 1\", "
             "\"json_extract(raw_json, '$.voided') != 1\"]")
    else:
        ok(f"All {len(where_lines)} where_clauses have voided/deleted filters")
    
    info(f"where_clauses found at: {[(l, c[:60]) for l, c in where_lines]}")
else:
    warn("analytics.py not found")


# ──────────────────────────────────────────────────────────────────────
# 5. BLUEPRINT REGISTRATION
# ──────────────────────────────────────────────────────────────────────
header("5. BLUEPRINT REGISTRATION")

if main_app:
    with open(main_app, 'r') as f:
        app_content = f.read()
    
    # Check which blueprints are registered
    bp_pattern = re.findall(r'register_blueprint\((\w+)', app_content)
    if bp_pattern:
        for bp in bp_pattern:
            ok(f"Blueprint registered: {bp}")
    else:
        warn("No blueprints found registered in main app")
    
    # Check if invoice_bp is registered
    if 'invoice_bp' in app_content or 'invoice_routes' in app_content:
        ok("invoice_bp appears to be registered")
    else:
        warn("invoice_bp may not be registered — check manually")
    
    # Check for inventory_bp (shouldn't exist yet)
    if 'inventory_bp' in app_content or 'inventory_routes' in app_content:
        info("inventory_bp already registered (unexpected at this stage)")
    else:
        info("inventory_bp not yet registered (expected — will add in Session 5)")


# ──────────────────────────────────────────────────────────────────────
# 6. API & AI CONFIGURATION
# ──────────────────────────────────────────────────────────────────────
header("6. API & AI CONFIGURATION")

invoice_proc_path = os.path.join(REDNUN_DIR, 'invoice_processor.py')
if os.path.exists(invoice_proc_path):
    with open(invoice_proc_path, 'r') as f:
        ip_content = f.read()
    
    # Detect which AI API is used
    if 'anthropic' in ip_content.lower() or 'claude' in ip_content.lower():
        ok("Invoice processor uses Anthropic/Claude API")
        info("Inventory system should use the SAME Anthropic API")
    if 'openai' in ip_content.lower():
        ok("Invoice processor uses OpenAI API")
        info("Inventory system should use the SAME OpenAI API")
    
    # Check how API key is loaded
    if 'os.environ' in ip_content or 'os.getenv' in ip_content:
        ok("API key loaded from environment variable")
    elif 'ANTHROPIC_API_KEY' in ip_content or 'OPENAI_API_KEY' in ip_content:
        ok("API key constant found")
    else:
        warn("Could not determine how API key is loaded — check invoice_processor.py manually")
    
    # Check for Vision API usage
    if 'vision' in ip_content.lower() or 'image' in ip_content.lower():
        ok("Vision API usage found in invoice_processor.py")
    else:
        warn("No Vision API usage detected in invoice_processor.py")
else:
    fail("invoice_processor.py not found!")


# ──────────────────────────────────────────────────────────────────────
# 7. DUPLICATE / UNUSED FILES
# ──────────────────────────────────────────────────────────────────────
header("7. DUPLICATE & UNUSED FILES")

# Check for backup files in main directory
bak_files = glob.glob(os.path.join(REDNUN_DIR, '*.bak'))
bak_files += glob.glob(os.path.join(REDNUN_DIR, '*.old'))
bak_files += glob.glob(os.path.join(REDNUN_DIR, '*.backup'))
bak_files += glob.glob(os.path.join(REDNUN_DIR, '*_backup*'))
bak_files += glob.glob(os.path.join(REDNUN_DIR, '*_old*'))
bak_files += glob.glob(os.path.join(REDNUN_DIR, '*_copy*'))

if bak_files:
    warn(f"Found {len(bak_files)} backup/old files in main directory:")
    for f in bak_files[:10]:
        info(f"  {os.path.basename(f)}")
    if len(bak_files) > 10:
        info(f"  ... and {len(bak_files) - 10} more")
else:
    ok("No stale backup files in main directory")

# Check /opt/rednun/backups/ size
backups_dir = os.path.join(REDNUN_DIR, 'backups')
if os.path.exists(backups_dir):
    total_backup_size = 0
    backup_count = 0
    for f in os.listdir(backups_dir):
        fp = os.path.join(backups_dir, f)
        if os.path.isfile(fp):
            total_backup_size += os.path.getsize(fp)
            backup_count += 1
    size_gb = total_backup_size / (1024**3)
    info(f"Backups directory: {backup_count} files, {size_gb:.1f} GB")
    if size_gb > 2:
        warn(f"Backups taking {size_gb:.1f}GB — consider cleaning old ones")
else:
    info("No backups directory found")

# Check for multiple similar Python files (possible duplicates)
py_basenames = [os.path.basename(f) for f in py_files]
for name in py_basenames:
    base = name.replace('.py', '')
    variants = [n for n in py_basenames if base in n and n != name]
    if variants:
        warn(f"Possible duplicates of {name}: {variants}")

# Check disk usage
try:
    import shutil
    total, used, free = shutil.disk_usage('/')
    free_gb = free / (1024**3)
    use_pct = (used / total) * 100
    if free_gb < 3:
        fail(f"Low disk space: {free_gb:.1f}GB free ({use_pct:.0f}% used)",
             "Clean old backups and run VACUUM on database")
    elif free_gb < 5:
        warn(f"Disk space getting tight: {free_gb:.1f}GB free ({use_pct:.0f}% used)")
    else:
        ok(f"Disk space OK: {free_gb:.1f}GB free ({use_pct:.0f}% used)")
except:
    pass


# ──────────────────────────────────────────────────────────────────────
# 8. SERVICE & RUNTIME
# ──────────────────────────────────────────────────────────────────────
header("8. SERVICE & RUNTIME")

# Check gunicorn
try:
    import subprocess
    result = subprocess.run(['pgrep', '-a', 'gunicorn'], capture_output=True, text=True)
    if result.stdout.strip():
        ok("gunicorn is running")
        for line in result.stdout.strip().split('\n')[:3]:
            info(f"  {line.strip()[:100]}")
    else:
        warn("gunicorn is NOT running — systemctl restart rednun")
except:
    warn("Could not check gunicorn status")

# Check port 8080
try:
    result = subprocess.run(['ss', '-tlnp'], capture_output=True, text=True)
    if '8080' in result.stdout:
        ok("Port 8080 is listening")
    else:
        warn("Port 8080 not listening — service may be down")
except:
    pass

# Check cron jobs
try:
    result = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
    if result.stdout.strip():
        info("Cron jobs:")
        for line in result.stdout.strip().split('\n'):
            if line.strip() and not line.startswith('#'):
                info(f"  {line.strip()}")
    else:
        info("No cron jobs configured")
except:
    pass

# Check ffmpeg (needed for inventory)
try:
    result = subprocess.run(['which', 'ffmpeg'], capture_output=True, text=True)
    if result.stdout.strip():
        ok(f"ffmpeg installed: {result.stdout.strip()}")
    else:
        warn("ffmpeg NOT installed — needed for inventory video processing",)
except:
    pass

# Check Whisper
try:
    result = subprocess.run(
        ['pip', 'show', 'openai-whisper'], 
        capture_output=True, text=True,
        cwd=REDNUN_DIR
    )
    if 'Name: openai-whisper' in result.stdout:
        ok("openai-whisper is installed")
    else:
        info("openai-whisper not yet installed (needed for inventory audio)")
except:
    pass

# Check rapidfuzz
try:
    result = subprocess.run(
        ['pip', 'show', 'rapidfuzz'], 
        capture_output=True, text=True,
        cwd=REDNUN_DIR
    )
    if 'Name: rapidfuzz' in result.stdout:
        ok("rapidfuzz is installed")
    else:
        info("rapidfuzz not yet installed (needed for inventory product matching)")
except:
    pass


# ──────────────────────────────────────────────────────────────────────
# 9. PYTHON IMPORT / SYNTAX CHECKS
# ──────────────────────────────────────────────────────────────────────
header("9. PYTHON SYNTAX CHECKS")

for f in py_files:
    basename = os.path.basename(f)
    try:
        result = subprocess.run(
            [sys.executable, '-c', f'import py_compile; py_compile.compile("{f}", doraise=True)'],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            ok(f"{basename} — syntax OK")
        else:
            fail(f"{basename} — SYNTAX ERROR: {result.stderr[:200]}")
    except Exception as e:
        warn(f"{basename} — could not check: {e}")


# ──────────────────────────────────────────────────────────────────────
# 10. SIDEBAR.JS CHECK
# ──────────────────────────────────────────────────────────────────────
header("10. SIDEBAR CONFIGURATION")

sidebar_path = os.path.join(REDNUN_DIR, 'static', 'sidebar.js')
if os.path.exists(sidebar_path):
    with open(sidebar_path, 'r') as f:
        sidebar_content = f.read()
    
    # Check what sections exist
    sections = re.findall(r'name:\s*["\']([^"\']+)["\']', sidebar_content)
    info(f"Sidebar sections: {sections}")
    
    if 'Inventory' in sections:
        info("Inventory already in sidebar")
    else:
        info("Inventory NOT in sidebar yet (will add in Session 5)")
    
    if 'Invoices' in sections:
        ok("Invoices section present in sidebar")
else:
    warn("sidebar.js not found")


# ──────────────────────────────────────────────────────────────────────
# 11. INTAKE FOLDER CHECK
# ──────────────────────────────────────────────────────────────────────
header("11. INTAKE FOLDERS")

folders = {
    'invoice_images': 'Invoice scans intake',
    'invoice_images_archive': 'Processed invoice archive',
    'inventory_intake': 'Inventory video/audio intake (NEW)',
}

for folder, desc in folders.items():
    path = os.path.join(REDNUN_DIR, folder)
    if os.path.exists(path):
        count = len(os.listdir(path))
        ok(f"{folder}/: exists, {count} files ({desc})")
    else:
        info(f"{folder}/: does not exist yet ({desc})")


# ──────────────────────────────────────────────────────────────────────
# SUMMARY
# ──────────────────────────────────────────────────────────────────────
header("AUDIT SUMMARY")

fails = [r for r in RESULTS if r[0] == 'FAIL']
warns = [r for r in RESULTS if r[0] == 'WARN']
oks = [r for r in RESULTS if r[0] == 'OK']

print(f"\n  ✅ {len(oks)} passed")
print(f"  ⚠️  {len(warns)} warnings")
print(f"  ❌ {len(fails)} failures\n")

if FIXES:
    print("  FIXES NEEDED BEFORE STARTING INVENTORY BUILD:")
    print("  " + "─" * 50)
    for i, (issue, fix) in enumerate(FIXES, 1):
        print(f"  {i}. {issue}")
        print(f"     → {fix}")
        print()

if not fails:
    print("  🎉 No critical issues found — ready for inventory build!")
elif len(fails) <= 3:
    print(f"  ⚡ {len(fails)} issues to fix — should be quick, then ready to go.")
else:
    print(f"  🔧 {len(fails)} issues need attention before starting inventory build.")

print(f"\n{'='*70}")
print(f"  Audit completed at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print(f"{'='*70}\n")
