"""Local one-time helper — generate the Gmail read-only OAuth token.

The Gmail-alerts scraper needs a user OAuth token (not a service account, since
it reads *your* inbox). Run this once on your machine:

  1. In Google Cloud Console, create an OAuth client (type: Desktop app) for the
     same project, download the JSON, save it as ``gmail_credentials.json`` in
     the jobRadar/ root.
  2. Run:  python scripts/gmail_authorize.py
  3. A browser opens; approve Gmail access (read alerts + create drafts).
  4. ``gmail_token.json`` is written. For CI, paste its contents (one line) into
     the GMAIL_OAUTH_TOKEN GitHub secret.

Scopes: ``gmail.readonly`` (the LinkedIn-alert scraper) + ``gmail.compose`` (the
outreach module, to create draft emails — never to send). Re-run this after an
upgrade so the token carries the compose scope.

This source is optional — everything else works without it.
"""

from __future__ import annotations

import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly",
          "https://www.googleapis.com/auth/gmail.compose"]
ROOT = Path(__file__).resolve().parent.parent
CRED = ROOT / "gmail_credentials.json"
TOKEN = ROOT / "gmail_token.json"


def main() -> None:
    if not CRED.exists():
        sys.exit(f"Missing {CRED}. Download an OAuth Desktop-app client JSON "
                 f"and save it there first.")
    flow = InstalledAppFlow.from_client_secrets_file(str(CRED), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN.write_text(creds.to_json(), encoding="utf-8")
    print(f"Wrote {TOKEN}")
    print("For CI, copy its contents into the GMAIL_OAUTH_TOKEN secret.")


if __name__ == "__main__":
    main()
