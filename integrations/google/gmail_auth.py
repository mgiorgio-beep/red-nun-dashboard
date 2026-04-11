#!/usr/bin/env python3
"""
Gmail OAuth2 Authorization Helper

Run this ONCE on a machine with a browser to authorize Gmail access
for email_invoice_poller.py, then copy gmail_token.pickle to the Beelink.

Headless two-step flow:
  Step 1 (on server):   python gmail_auth.py --url
                        Copy the printed URL and open it in a browser.
  Step 2 (on server):   python gmail_auth.py --code <paste_code_here>

Interactive flow (with browser on same machine):
  python gmail_auth.py

Usage: cd /opt/rednun && source venv/bin/activate && python gmail_auth.py
"""
import os
import sys
import pickle

sys.path.insert(0, '/opt/rednun')
os.chdir('/opt/rednun')

from google_auth_oauthlib.flow import InstalledAppFlow

CREDENTIALS_FILE = '/opt/rednun/google_credentials.json'
TOKEN_FILE       = '/opt/rednun/gmail_token.pickle'

SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.modify',
]


def check_existing():
    """Return True if token already exists and is valid."""
    if not os.path.exists(TOKEN_FILE):
        return False
    try:
        with open(TOKEN_FILE, 'rb') as f:
            creds = pickle.load(f)
        if creds.valid:
            print(f"Token already exists and is valid.")
            print(f"Scopes: {creds.scopes}")
            return True
        if creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
            with open(TOKEN_FILE, 'wb') as f:
                pickle.dump(creds, f)
            print("Token refreshed.")
            print(f"Scopes: {creds.scopes}")
            return True
    except Exception as e:
        print(f"Existing token error: {e}")
    return False


def _make_flow():
    """Create an InstalledAppFlow configured for OOB (headless) use."""
    return InstalledAppFlow.from_client_secrets_file(
        CREDENTIALS_FILE, SCOPES,
        redirect_uri='urn:ietf:wg:oauth:2.0:oob'
    )


def step1_get_url():
    """Generate and print the authorization URL."""
    flow = _make_flow()
    auth_url, _ = flow.authorization_url(
        access_type='offline',
        prompt='consent',
    )
    print("\n" + "=" * 60)
    print("STEP 1: Open this URL in a browser and authorize:")
    print("=" * 60)
    print(auth_url)
    print("=" * 60)
    print("\nAfter clicking Allow, Google will show a short code.")
    print("Run step 2:")
    print("  python gmail_auth.py --code <paste_code_here>")
    print()


def step2_exchange_code(code):
    """Exchange authorization code for credentials and save token."""
    flow = _make_flow()
    flow.fetch_token(code=code.strip())
    creds = flow.credentials

    with open(TOKEN_FILE, 'wb') as f:
        pickle.dump(creds, f)

    print(f"\nToken saved to {TOKEN_FILE}")
    print(f"Scopes: {creds.scopes}")
    print("\nGmail OAuth complete. email_invoice_poller.py is ready to use.")


def interactive_flow():
    """Run console-based interactive OAuth flow."""
    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
    print("\nStarting console OAuth flow...")
    print("You will be given a URL to open in a browser.\n")
    creds = flow.run_console()
    with open(TOKEN_FILE, 'wb') as f:
        pickle.dump(creds, f)
    print(f"\nToken saved to {TOKEN_FILE}")
    print(f"Scopes: {creds.scopes}")


if __name__ == '__main__':
    args = sys.argv[1:]

    if check_existing():
        sys.exit(0)

    if '--url' in args:
        step1_get_url()
    elif '--code' in args:
        idx = args.index('--code')
        if idx + 1 >= len(args):
            print("Usage: python gmail_auth.py --code <authorization_code>")
            sys.exit(1)
        step2_exchange_code(args[idx + 1])
    else:
        interactive_flow()
