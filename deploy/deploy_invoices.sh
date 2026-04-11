#!/bin/bash
# Deploy Invoice Scanner to Red Nun Analytics server
# Run from /opt/rednun with venv activated

set -e
echo "═══════════════════════════════════════════"
echo "  Deploying Invoice Scanner"
echo "═══════════════════════════════════════════"

# 1. Backup
echo "1. Creating backup..."
TS=$(date +%Y%m%d_%H%M%S)
cp server.py server.py.bak.$TS

# 2. Create invoice images directory
echo "2. Creating upload directory..."
mkdir -p invoice_images

# 3. Check for ANTHROPIC_API_KEY in .env
if grep -q "ANTHROPIC_API_KEY" .env; then
    echo "3. ANTHROPIC_API_KEY found in .env ✓"
else
    echo "3. Adding ANTHROPIC_API_KEY placeholder to .env..."
    echo "" >> .env
    echo "# Anthropic API key for invoice scanning (Claude Vision)" >> .env
    echo "ANTHROPIC_API_KEY=" >> .env
    echo "   ⚠ You need to add your API key to .env!"
fi

# 4. Install requests if needed (should already be there)
echo "4. Checking dependencies..."
pip install requests --break-system-packages -q 2>/dev/null || pip install requests -q

# 5. Initialize tables
echo "5. Initializing invoice tables..."
python3 -c "from invoice_processor import init_invoice_tables; init_invoice_tables()"

# 6. Patch server.py to include invoice routes
echo "6. Patching server.py..."
python3 << 'PATCH'
code = open('server.py').read()

if 'invoice_bp' in code:
    print("   server.py already has invoice routes ✓")
else:
    # Add import after existing imports
    old_import = "from export import generate_weekly_excel"
    new_import = """from export import generate_weekly_excel
from invoice_routes import invoice_bp
from invoice_processor import init_invoice_tables"""
    code = code.replace(old_import, new_import)

    # Register blueprint after CORS(app)
    old_cors = "CORS(app)"
    new_cors = """CORS(app)
app.register_blueprint(invoice_bp)"""
    code = code.replace(old_cors, new_cors)

    # Add table init after ME table init
    old_init = 'if ME_AVAILABLE:'
    new_init = """# Initialize invoice scanner tables
try:
    init_invoice_tables()
    logger.info("Invoice scanner tables initialized")
except Exception as e:
    logger.warning(f"Invoice table init failed: {e}")

if ME_AVAILABLE:"""
    # Only replace the first occurrence
    code = code.replace(old_init, new_init, 1)

    open('server.py', 'w').write(code)
    print("   server.py patched ✓")

# Verify syntax
try:
    compile(open('server.py').read(), 'server.py', 'exec')
    print("   Syntax check: OK ✓")
except SyntaxError as e:
    print(f"   ⚠ SYNTAX ERROR: {e}")
    print("   Restoring backup...")
    import shutil
    shutil.copy(f'server.py.bak.{__import__("os").popen("ls -t server.py.bak.* | head -1").read().strip().split(".")[-1]}', 'server.py')
    exit(1)
PATCH

# 7. Restart service
echo "7. Restarting service..."
systemctl restart rednun
sleep 2

# 8. Verify
echo "8. Verifying..."
if systemctl is-active --quiet rednun; then
    echo "   Service: RUNNING ✓"
else
    echo "   ⚠ SERVICE FAILED"
    journalctl -u rednun -n 10 --no-pager
    exit 1
fi

HTTP=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8080/)
echo "   Dashboard HTTP: $HTTP"

INV=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8080/invoices)
echo "   Invoice page HTTP: $INV"

echo ""
echo "═══════════════════════════════════════════"
echo "  Invoice Scanner Deployed! ✓"
echo ""
echo "  Access: https://dashboard.rednun.com/invoices"
echo ""
echo "  ⚠ Remember to add your Anthropic API key:"
echo "    nano /opt/rednun/.env"
echo "    ANTHROPIC_API_KEY=sk-ant-..."
echo "═══════════════════════════════════════════"
