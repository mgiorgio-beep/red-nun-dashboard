#!/bin/bash
# deploy.sh — Apply bug fixes + deploy upgraded dashboard
# Usage: Copy these files to the server, then run: bash deploy.sh
#
# Files needed alongside this script:
#   - index.html (new dashboard)
#   - apply_fixes.sh (server.py patches)

set -e
cd /opt/rednun
source venv/bin/activate

echo "═══════════════════════════════════════════"
echo "  Red Nun Analytics — Deploy"
echo "═══════════════════════════════════════════"

# 1. Backup
BACKUP="backups/$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP"
cp server.py "$BACKUP/"
cp static/index.html "$BACKUP/"
echo "✓ Backed up to $BACKUP/"

# 2. Apply server.py bug fixes
echo ""
echo "--- Applying server.py fixes ---"
bash apply_fixes.sh

# 3. Deploy new dashboard
echo ""
echo "--- Deploying new dashboard ---"
cp index.html static/index.html
echo "✓ Dashboard updated"

# 4. Restart
echo ""
echo "--- Restarting service ---"
systemctl restart rednun
sleep 2

# 5. Verify
STATUS=$(systemctl is-active rednun)
if [ "$STATUS" = "active" ]; then
    echo "✓ Service is running"
    # Quick health check
    HTTP=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8080/)
    echo "✓ HTTP status: $HTTP"
else
    echo "✗ Service failed! Check: journalctl -u rednun -n 30"
    exit 1
fi

echo ""
echo "═══════════════════════════════════════════"
echo "  Deploy complete!"
echo "  Dashboard: https://dashboard.rednun.com"
echo "═══════════════════════════════════════════"
