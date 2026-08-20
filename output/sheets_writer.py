"""Layer 4 — Google Sheets writer (gspread + service account).

One bulk ``append_rows`` per run, no per-row calls. Reads existing dedup hashes
from column Q first so a job already in the tracker is never re-appended.

Credentials come from ``GOOGLE_SHEETS_CREDENTIALS`` as either the raw
service-account JSON (one line) or a path to a JSON file. The target sheet is
identified by ``JOBRADAR_SHEET_ID`` (preferred) or by title.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from common import config
from processing.models import ScoredJob

log = logging.getLogger("jobradar")

try:
    import gspread
    from google.oauth2.service_account import Credentials
    _GSPREAD_OK = True
except Exception:  # pragma: no cover
    _GSPREAD_OK = False

SPREADSHEET_TITLE = "JobRadar Tracker"
WORKSHEET_TITLE = "Tracker"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive"]
DEDUP_COL = 17  # column Q

HEADERS = ["Date Found", "Pipeline", "Source", "Job Title", "Company",
           "Location", "Score", "Experience Fit", "Role Fit", "Tech Match",
           "Visa Status", "Salary Range", "Job URL", "Easy Apply", "Status",
           "Notes", "Dedup Hash"]


def load_service_account_info() -> dict | None:
    raw = config.env("GOOGLE_SHEETS_CREDENTIALS")
    if not raw:
        return None
    # A filesystem path?
    p = Path(raw)
    if len(raw) < 500 and p.exists():
        raw = p.read_text(encoding="utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.error("[sheets] GOOGLE_SHEETS_CREDENTIALS is neither valid JSON "
                  "nor a readable file path")
        return None


def client():
    if not _GSPREAD_OK:
        raise RuntimeError("gspread/google-auth not installed")
    info = load_service_account_info()
    if not info:
        raise RuntimeError("GOOGLE_SHEETS_CREDENTIALS not set")
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def service_account_email() -> str | None:
    info = load_service_account_info()
    return info.get("client_email") if info else None


_QUOTA_HELP = (
    "\nGoogle service accounts have NO Drive storage of their own, so they "
    "cannot CREATE a spreadsheet — only write to one you already own.\n\n"
    "Fix (one time):\n"
    "  1. In your own Google Drive, create a blank Google Sheet.\n"
    "  2. Share it (Editor) with the service account:\n"
    "         {email}\n"
    "  3. Copy the sheet id from its URL:\n"
    "         https://docs.google.com/spreadsheets/d/<THIS_IS_THE_ID>/edit\n"
    "  4. Set it:  JOBRADAR_SHEET_ID=<THIS_IS_THE_ID>   (in .env / secrets)\n"
    "  5. Re-run:  python setup_tracker.py\n"
)


def open_worksheet(create: bool = False):
    gc = client()
    sheet_id = config.env("JOBRADAR_SHEET_ID")
    if sheet_id:
        sh = gc.open_by_key(sheet_id)
    else:
        try:
            sh = gc.open(SPREADSHEET_TITLE)
        except gspread.SpreadsheetNotFound:
            if not create:
                raise
            try:
                sh = gc.create(SPREADSHEET_TITLE)
            except gspread.exceptions.APIError as exc:
                if "quota" in str(exc).lower():
                    raise RuntimeError(
                        _QUOTA_HELP.format(email=service_account_email()
                                           or "<service-account-email>")) from exc
                raise
            log.info("[sheets] created spreadsheet %s (id=%s)",
                     SPREADSHEET_TITLE, sh.id)
    try:
        return sh.worksheet(WORKSHEET_TITLE)
    except gspread.WorksheetNotFound:
        if not create:
            raise
        return sh.add_worksheet(WORKSHEET_TITLE, rows=2000, cols=len(HEADERS))


class SheetsWriter:
    def __init__(self):
        self._ws = None

    @property
    def available(self) -> bool:
        return _GSPREAD_OK and load_service_account_info() is not None

    @property
    def worksheet(self):
        if self._ws is None:
            self._ws = open_worksheet(create=False)
        return self._ws

    def read_existing_hashes(self) -> set[str]:
        try:
            col = self.worksheet.col_values(DEDUP_COL)
        except Exception as exc:  # noqa: BLE001
            log.warning("[sheets] could not read existing hashes: %s", exc)
            return set()
        # Drop the header cell.
        return {h for h in col[1:] if h}

    def bulk_append(self, jobs: list[ScoredJob]) -> int:
        if not jobs:
            log.info("[sheets] nothing to append")
            return 0
        rows = [j.to_tracker_row() for j in jobs]
        self.worksheet.append_rows(rows, value_input_option="USER_ENTERED")
        log.info("[sheets] appended %d rows", len(rows))
        return len(rows)

    def sheet_url(self) -> str:
        try:
            return self.worksheet.spreadsheet.url
        except Exception:  # noqa: BLE001
            return ""
