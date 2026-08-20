"""Layer 4 — Google Sheets writer (gspread + service account).

Writes into ONE spreadsheet ("JobRadar Tracker") with TWO worksheets/tabs:
"India" and "International". Each ScoredJob is routed to the tab matching its
pipeline. Dedup reads hashes from both tabs so the same posting is never
re-appended regardless of which tab it lives in.

Credentials come from ``GOOGLE_SHEETS_CREDENTIALS`` as either the raw
service-account JSON (one line) or a path to a JSON file (resolved relative to
the current dir or the project root). The spreadsheet is identified by
``JOBRADAR_SHEET_ID`` (preferred) or by title.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from common import config
from processing.models import Pipeline, ScoredJob

log = logging.getLogger("jobradar")

try:
    import gspread
    from google.oauth2.service_account import Credentials
    _GSPREAD_OK = True
except Exception:  # pragma: no cover
    _GSPREAD_OK = False

SPREADSHEET_TITLE = "JobRadar Tracker"
# One tab per pipeline.
WORKSHEETS: dict[Pipeline, str] = {
    Pipeline.INDIA: "India",
    Pipeline.INTERNATIONAL: "International",
}
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
    # A filesystem path? Try it as given, then relative to the project root
    # (so it works no matter which directory main.py is launched from).
    if len(raw) < 500:
        for cand in (Path(raw), Path(config.ROOT) / raw):
            if cand.exists():
                raw = cand.read_text(encoding="utf-8")
                break
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.error("[sheets] GOOGLE_SHEETS_CREDENTIALS is neither valid JSON "
                  "nor a readable file path (looked in cwd and %s)", config.ROOT)
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


def open_spreadsheet(create: bool = False):
    """Open the JobRadar spreadsheet (by id, else by title, else create)."""
    gc = client()
    sheet_id = config.env("JOBRADAR_SHEET_ID")
    if sheet_id:
        return gc.open_by_key(sheet_id)
    try:
        return gc.open(SPREADSHEET_TITLE)
    except gspread.SpreadsheetNotFound:
        if not create:
            raise
        try:
            sh = gc.create(SPREADSHEET_TITLE)
        except gspread.exceptions.APIError as exc:
            if "quota" in str(exc).lower():
                raise RuntimeError(_QUOTA_HELP.format(
                    email=service_account_email() or "<service-account-email>")
                ) from exc
            raise
        log.info("[sheets] created spreadsheet %s (id=%s)", SPREADSHEET_TITLE, sh.id)
        return sh


def open_worksheet(sh, title: str, create: bool = False):
    """Open one worksheet/tab by title, creating it if asked."""
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        if not create:
            raise
        return sh.add_worksheet(title, rows=2000, cols=len(HEADERS))


class SheetsWriter:
    def __init__(self):
        self._sh = None
        self._ws: dict[Pipeline, object] = {}

    @property
    def available(self) -> bool:
        return _GSPREAD_OK and load_service_account_info() is not None

    @property
    def spreadsheet(self):
        if self._sh is None:
            self._sh = open_spreadsheet(create=False)
        return self._sh

    def worksheet(self, pipeline: Pipeline):
        if pipeline not in self._ws:
            self._ws[pipeline] = open_worksheet(
                self.spreadsheet, WORKSHEETS[pipeline], create=True)
        return self._ws[pipeline]

    def read_existing_hashes(self) -> set[str]:
        """Union of dedup hashes across both tabs."""
        hashes: set[str] = set()
        for pipeline in WORKSHEETS:
            try:
                col = self.worksheet(pipeline).col_values(DEDUP_COL)
                hashes.update(h for h in col[1:] if h)
            except Exception as exc:  # noqa: BLE001
                log.warning("[sheets] could not read hashes from %s tab: %s",
                            WORKSHEETS[pipeline], exc)
        return hashes

    def bulk_append(self, jobs: list[ScoredJob]) -> int:
        """Group jobs by pipeline and append each group to its own tab."""
        if not jobs:
            log.info("[sheets] nothing to append")
            return 0
        total = 0
        for pipeline, tab in WORKSHEETS.items():
            group = [j for j in jobs if j.pipeline == pipeline]
            if not group:
                continue
            rows = [j.to_tracker_row() for j in group]
            self.worksheet(pipeline).append_rows(
                rows, value_input_option="USER_ENTERED")
            log.info("[sheets] appended %d rows to '%s' tab", len(rows), tab)
            total += len(rows)
        return total

    def sheet_url(self) -> str:
        try:
            return self.spreadsheet.url
        except Exception:  # noqa: BLE001
            return ""
