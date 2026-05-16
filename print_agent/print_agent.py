"""
Red Nun Print Agent — sits on the home Windows desktop, polls the dashboard
for queued check PDFs, and prints them silently to the Brother HL-L6210DW via
Sumatra PDF.

Designed to run under NSSM as a Windows service. Single file, stdlib + requests.

Configuration: via environment variables (recommended for NSSM) or a YAML-like
config file at the path passed in --config.

Required env vars:
    RNPA_BASE_URL          e.g. https://dashboard.rednun.com  OR  http://10.10.10.1:8080
    RNPA_API_KEY           must match PRINT_AGENT_API_KEY in server .env
    RNPA_PRINTER_NAME      e.g. "Brother HL-L6210DW series"

Optional:
    RNPA_SUMATRA_PATH      default: search PATH then common install dirs
    RNPA_POLL_INTERVAL     seconds between checkouts, default 15
    RNPA_LOG_FILE          default: %ProgramData%\\RedNunPrintAgent\\print_agent.log
    RNPA_AGENT_ID          identifier sent with checkouts, default = hostname
    RNPA_PRINT_SETTINGS    SumatraPDF -print-settings string, comma-separated.
                            Default: "bin=Tray2,monochrome,noscale"
                            - bin=Tray2  -> pull check stock from Tray 2.
                              Tray 1 stays loaded with plain paper for
                              printer warnings / error pages.
                            - monochrome -> ink/toner efficient.
                            - noscale    -> DO NOT scale; MICR line position
                              is bank-critical, scaling breaks it.
                            Override only if your printer driver uses
                            different tray names (e.g. "bin=Lower" or
                            "bin=2"). See SumatraPDF docs for syntax.
"""

import os
import sys
import time
import socket
import logging
import logging.handlers
import subprocess
import tempfile
from pathlib import Path

try:
    import requests
except ImportError:  # pragma: no cover
    print("ERROR: 'requests' package not installed. Run: pip install requests", file=sys.stderr)
    sys.exit(2)


# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────

DEFAULT_SUMATRA_CANDIDATES = [
    r"C:\Program Files\SumatraPDF\SumatraPDF.exe",
    r"C:\Program Files (x86)\SumatraPDF\SumatraPDF.exe",
    str(Path.home() / "AppData" / "Local" / "SumatraPDF" / "SumatraPDF.exe"),
]


def _env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        print(f"ERROR: env var {name} is required", file=sys.stderr)
        sys.exit(2)
    return v


def _resolve_sumatra():
    explicit = os.environ.get("RNPA_SUMATRA_PATH")
    if explicit and os.path.isfile(explicit):
        return explicit
    from shutil import which
    p = which("SumatraPDF") or which("SumatraPDF.exe")
    if p:
        return p
    for c in DEFAULT_SUMATRA_CANDIDATES:
        if os.path.isfile(c):
            return c
    return None


def _setup_logging():
    log_file = _env("RNPA_LOG_FILE")
    if not log_file:
        prog_data = os.environ.get("ProgramData", r"C:\ProgramData")
        log_dir = os.path.join(prog_data, "RedNunPrintAgent")
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, "print_agent.log")
    handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s"
    ))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    root.addHandler(console)
    return log_file


# ──────────────────────────────────────────────────────────────────────────────
# Server client
# ──────────────────────────────────────────────────────────────────────────────

class ServerClient:
    def __init__(self, base_url, api_key, agent_id, timeout=20):
        self.base = base_url.rstrip("/")
        self.headers = {"X-API-Key": api_key}
        self.agent_id = agent_id
        self.timeout = timeout

    def health(self):
        r = requests.get(f"{self.base}/api/print-agent/health",
                         headers=self.headers, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def checkout(self):
        r = requests.post(f"{self.base}/api/print-agent/checkout",
                          headers=self.headers,
                          json={"agent_id": self.agent_id},
                          timeout=self.timeout)
        if r.status_code == 204:
            return None
        r.raise_for_status()
        return r.json()

    def download_pdf(self, job_id, dest_path):
        r = requests.get(f"{self.base}/api/print-agent/jobs/{job_id}/pdf",
                         headers=self.headers, timeout=self.timeout, stream=True)
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(64 * 1024):
                f.write(chunk)
        return dest_path

    def ack(self, job_id, status, error_text=""):
        r = requests.post(f"{self.base}/api/print-agent/jobs/{job_id}/ack",
                          headers=self.headers,
                          json={"status": status, "error": error_text},
                          timeout=self.timeout)
        r.raise_for_status()
        return r.json()


# ──────────────────────────────────────────────────────────────────────────────
# Printing
# ──────────────────────────────────────────────────────────────────────────────

class Printer:
    def __init__(self, sumatra_path, printer_name, print_settings=""):
        self.sumatra = sumatra_path
        self.printer_name = printer_name
        # Comma-separated SumatraPDF print-settings, e.g.
        #   "bin=Tray2,monochrome,noscale"
        self.print_settings = (print_settings or "").strip()

    def print_pdf(self, pdf_path):
        """Send pdf_path to the configured Windows printer via SumatraPDF.

        SumatraPDF args:
            -print-to "<name>"        print to the named Windows printer
            -silent                   no UI, no error dialogs
            -print-settings "<opts>"  comma-separated. Used to pin checks
                                       to Tray 2 ('bin=Tray2') and disable
                                       scaling ('noscale') -- MICR-critical.
        """
        cmd = [self.sumatra, "-print-to", self.printer_name, "-silent"]
        if self.print_settings:
            cmd += ["-print-settings", self.print_settings]
        cmd.append(pdf_path)
        logging.info(f"Spawning: {' '.join(repr(c) for c in cmd)}")
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"SumatraPDF exit {result.returncode}: "
                f"stderr={result.stderr.decode('utf-8', errors='replace').strip()}"
            )


# ──────────────────────────────────────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────────────────────────────────────

def run_one_iteration(client, printer, tmpdir):
    """Returns True if a job was processed, False if queue empty."""
    job = client.checkout()
    if not job:
        return False
    job_id = job["id"]
    logging.info(
        f"Claimed job #{job_id}: check #{job.get('check_number')} "
        f"location={job.get('location')} attempts={job.get('attempts')}"
    )
    pdf_path = os.path.join(tmpdir, f"job_{job_id}.pdf")
    try:
        client.download_pdf(job_id, pdf_path)
        size = os.path.getsize(pdf_path)
        logging.info(f"Downloaded PDF to {pdf_path} ({size:,} bytes)")
        printer.print_pdf(pdf_path)
        client.ack(job_id, "printed")
        logging.info(f"Job #{job_id} printed and acked.")
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        logging.exception(f"Job #{job_id} failed: {msg}")
        try:
            client.ack(job_id, "error", error_text=msg)
        except Exception as ack_err:
            logging.error(f"Could not ack failure for job #{job_id}: {ack_err}")
    finally:
        try:
            os.remove(pdf_path)
        except OSError:
            pass
    return True


def main():
    log_file = _setup_logging()
    base_url = _env("RNPA_BASE_URL", required=True)
    api_key = _env("RNPA_API_KEY", required=True)
    printer_name = _env("RNPA_PRINTER_NAME", required=True)
    poll_interval = int(_env("RNPA_POLL_INTERVAL", "15"))
    agent_id = _env("RNPA_AGENT_ID") or socket.gethostname()
    # Tray 2 = check stock by default. Tray 1 stays plain for printer
    # warnings / error pages. 'noscale' is non-negotiable for MICR alignment.
    print_settings = _env("RNPA_PRINT_SETTINGS", "bin=Tray2,monochrome,noscale")

    sumatra = _resolve_sumatra()
    if not sumatra:
        logging.error(
            "SumatraPDF.exe not found. Install from https://www.sumatrapdfreader.org/ "
            "or set RNPA_SUMATRA_PATH explicitly."
        )
        sys.exit(2)

    logging.info(f"Red Nun Print Agent starting -- agent_id={agent_id}")
    logging.info(f"Server:   {base_url}")
    logging.info(f"Printer:  {printer_name}")
    logging.info(f"Sumatra:  {sumatra}")
    logging.info(f"Settings: {print_settings or '(none)'}")
    logging.info(f"Log file: {log_file}")

    client = ServerClient(base_url, api_key, agent_id)
    printer = Printer(sumatra, printer_name, print_settings=print_settings)

    try:
        h = client.health()
        logging.info(f"Server health OK: {h}")
    except Exception as e:
        logging.error(f"Server health check failed: {e}")

    tmpdir = tempfile.mkdtemp(prefix="rednun_print_")
    logging.info(f"Tempdir: {tmpdir}")

    backoff = 1.0
    while True:
        try:
            processed = run_one_iteration(client, printer, tmpdir)
            backoff = 1.0
            if not processed:
                time.sleep(poll_interval)
        except requests.exceptions.RequestException as net_err:
            logging.warning(f"Network error: {net_err}. Backing off {backoff:.0f}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
        except KeyboardInterrupt:
            logging.info("Shutdown requested. Exiting.")
            return 0
        except Exception:
            logging.exception("Unexpected loop error")
            time.sleep(min(poll_interval * 2, 60))


if __name__ == "__main__":
    sys.exit(main() or 0)
