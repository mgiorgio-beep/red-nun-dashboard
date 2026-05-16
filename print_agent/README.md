# Red Nun Print Agent — Windows Setup

A small Python service that runs on the home Windows desktop, polls the
dashboard for queued check PDFs, and prints them silently to the Brother
HL-L6210DW on the same home LAN. Talks to the dashboard over the existing
WireGuard tunnel (or over the public internet if WG is down).

## One-time install

### 1. Install Python 3 (if not already)

Download Python 3.11+ from https://www.python.org/downloads/ and **check
"Add Python to PATH"** during install.

Verify in PowerShell:

```powershell
python --version
```

### 2. Install Sumatra PDF

Used for silent PDF printing. Download from
https://www.sumatrapdfreader.org/ and install with defaults.

Verify:

```powershell
& "C:\Program Files\SumatraPDF\SumatraPDF.exe" -h
```

### 3. Install the agent

```powershell
# Pick any directory. C:\Tools\rednun-print-agent\ is fine.
mkdir C:\Tools\rednun-print-agent
cd C:\Tools\rednun-print-agent

# Copy print_agent.py from the repo (download or via git clone).
# Then:
python -m pip install --upgrade pip
python -m pip install requests python-dotenv
```

### 4. Get your API key from the server

SSH to the Beelink:

```bash
ssh -p 2222 rednun@ssh.rednun.com
sudo bash -c 'KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(40))"); \
              echo "PRINT_AGENT_API_KEY=$KEY" >> /opt/red-nun-dashboard/.env; \
              echo "Generated key:"; echo $KEY'
sudo systemctl restart rednun
```

Copy the printed key — you'll paste it into the agent config below.

### 5. Configure the agent

Create `C:\Tools\rednun-print-agent\.env`:

```
RNPA_BASE_URL=https://dashboard.rednun.com
RNPA_API_KEY=<paste the key from step 4>
RNPA_PRINTER_NAME=Brother HL-L6210DW series
RNPA_POLL_INTERVAL=15

# Tray 2 holds check stock. Tray 1 stays loaded with plain paper so the
# printer can still print warnings, status pages, etc. 'noscale' is
# critical: any scaling shifts the MICR line and the bank may reject the
# check. Override only if your driver names the tray differently.
RNPA_PRINT_SETTINGS=bin=Tray2,monochrome,noscale
```

If WireGuard is up 24/7 and you prefer the LAN-style path, use:

```
RNPA_BASE_URL=http://10.10.10.1:8080
```

Make a tiny launcher batch file `C:\Tools\rednun-print-agent\run_agent.bat`:

```bat
@echo off
cd /d %~dp0
for /f "usebackq tokens=*" %%i in (`type .env`) do set %%i
python print_agent.py
```

### 6. Test in a terminal first

```powershell
cd C:\Tools\rednun-print-agent
.\run_agent.bat
```

You should see:

```
INFO Red Nun Print Agent starting — agent_id=<your-pc-name>
INFO Server:   https://dashboard.rednun.com
INFO Printer:  Brother HL-L6210DW series
INFO Sumatra:  C:\Program Files\SumatraPDF\SumatraPDF.exe
INFO Server health OK: {'ok': True, ...}
```

If health fails, the API key is wrong or the server is unreachable.

### 7. Print a test job

On the Beelink:

```bash
sqlite3 /var/lib/rednun/toast_data.db "
  INSERT INTO print_jobs (kind, pdf_path, status, location)
  VALUES ('check',
          (SELECT pdf_path FROM print_jobs WHERE status='printed' ORDER BY id DESC LIMIT 1),
          'pending', 'chatham');
"
```

Within ~15 seconds the agent should pick it up and the check should print
(it'll be a duplicate of the most recent check — fine for testing on plain
paper; switch to check stock for the next real one).

### 8. Install as a Windows service (NSSM)

Download NSSM from https://nssm.cc/download, extract anywhere, and:

```powershell
# Open an *Administrator* PowerShell

# Install the service
C:\path\to\nssm.exe install RedNunPrintAgent ^
  "C:\Tools\rednun-print-agent\run_agent.bat"

# Set working directory
C:\path\to\nssm.exe set RedNunPrintAgent AppDirectory C:\Tools\rednun-print-agent

# Set service display name and description
C:\path\to\nssm.exe set RedNunPrintAgent DisplayName "Red Nun Print Agent"
C:\path\to\nssm.exe set RedNunPrintAgent Description "Polls the dashboard for queued check PDFs and prints them to the Brother HL-L6210DW."

# Auto-restart on crash, wait 5s then retry
C:\path\to\nssm.exe set RedNunPrintAgent AppExit Default Restart
C:\path\to\nssm.exe set RedNunPrintAgent AppRestartDelay 5000

# Send service stdout/stderr to log files
C:\path\to\nssm.exe set RedNunPrintAgent AppStdout C:\ProgramData\RedNunPrintAgent\service.log
C:\path\to\nssm.exe set RedNunPrintAgent AppStderr C:\ProgramData\RedNunPrintAgent\service.err

# Start it
C:\path\to\nssm.exe start RedNunPrintAgent
```

Confirm it's running:

```powershell
Get-Service RedNunPrintAgent
```

Logs live at `C:\ProgramData\RedNunPrintAgent\print_agent.log`. Tail with:

```powershell
Get-Content C:\ProgramData\RedNunPrintAgent\print_agent.log -Tail 50 -Wait
```

## Day-to-day

- **Tray 2 = check stock. Tray 1 = plain paper** (for printer warnings /
  status pages). The agent always pulls from Tray 2 via the `bin=Tray2`
  setting. If you see a check come out of Tray 1, the driver is using a
  different tray name — confirm in Windows printer properties and update
  `RNPA_PRINT_SETTINGS` in `.env` (e.g. `bin=Lower` or `bin=2`).
- A queued check shows up in **Bill Pay → Print Checks** in the dashboard
  too, in case you ever want to print one yourself from a browser.
- The Brother prints checks on a single page; no duplex needed for
  DocuGard Top-Check stock.
- An evening summary email lands in your inbox each day (6 PM) with what
  was paid, what got skipped (and why), and any print jobs stuck in error.

## Troubleshooting

| Symptom | What to check |
|---------|---------------|
| Agent service stopped | `Get-Service RedNunPrintAgent` then start it; check `service.err` |
| "unauthorized" in logs | `.env` `RNPA_API_KEY` doesn't match the server's `.env` |
| Health check works but no jobs picked up | `sqlite3` on the Beelink: `SELECT * FROM print_jobs WHERE status='pending'` — if empty, auto_pay isn't producing jobs |
| Sumatra exit code != 0 | Printer name doesn't match Windows printer name exactly. Open Settings → Printers, copy the name verbatim into `.env` |
| Prints from wrong tray | Brother driver uses a different tray name than `Tray2`. Open Devices & Printers → right-click Brother → Printer properties → Device Settings tab. Note the exact name (e.g. "Tray 2" with a space, or "Tray2", or "Lower Tray"). Update `RNPA_PRINT_SETTINGS` in `.env` and restart the service: `Restart-Service RedNunPrintAgent` |
| Prints on wrong paper | Tray 1 must always have plain paper loaded, Tray 2 must have check stock. The agent forces Tray 2 via `bin=Tray2` |
| Stuck job after fault | The server retries up to 5 times then marks `status='error'`. See the daily email; reset with `UPDATE print_jobs SET status='pending', attempts=0 WHERE id=...` |
