"""Gmail LinkedIn-alert parser (free — both pipelines).

Reads recent "LinkedIn Job Alert" emails and extracts the job cards inside.
Auth uses an OAuth *user* token (read-only Gmail scope), supplied either as a
local ``gmail_token.json`` or, in CI, as the ``GMAIL_OAUTH_TOKEN`` secret (the
full authorized-user JSON on one line).

LinkedIn alert HTML is not a stable contract, so parsing is best-effort regex
over ``/jobs/view/`` anchors. If auth is missing or parsing yields nothing, the
scraper returns [] — the Sheet and other sources still work (see Risk Register).
"""

from __future__ import annotations

import base64
import json
import logging
import re
from html import unescape

from common import config
from processing.models import RawJob
from scrapers.base import BaseScraper

log = logging.getLogger("jobradar")

try:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    _GMAIL_OK = True
except Exception:  # pragma: no cover
    _GMAIL_OK = False

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Anchor whose href points at a specific LinkedIn job view; capture its text.
_JOB_LINK_RE = re.compile(
    r'href="(https?://[^"]*linkedin\.com[^"]*/jobs/view/[^"]+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")


class GmailAlertsScraper(BaseScraper):
    name = "LinkedIn Alert"

    def __init__(self):
        self.cfg = config.sources()["free_apis"]["gmail_alerts"]

    # --- credential loading ---

    def _credentials(self):
        raw = config.env(self.cfg.get("token_env"))
        info = None
        if raw:
            try:
                info = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("[Gmail] GMAIL_OAUTH_TOKEN is not valid JSON")
        else:
            from pathlib import Path

            token_path = Path(config.CONFIG_DIR).parent / self.cfg.get(
                "token_path", "gmail_token.json")
            if token_path.exists():
                info = json.loads(token_path.read_text(encoding="utf-8"))
        if not info:
            return None
        try:
            return Credentials.from_authorized_user_info(info, SCOPES)
        except Exception as exc:  # noqa: BLE001
            log.warning("[Gmail] could not build credentials: %s", exc)
            return None

    @property
    def available(self) -> bool:
        if not _GMAIL_OK:
            return False
        return self._credentials() is not None

    # --- fetch + parse ---

    def _fetch(self) -> list[RawJob]:
        creds = self._credentials()
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        sender = self.cfg.get("sender_filter", "jobalerts-noreply@linkedin.com")
        lookback = int(self.cfg.get("hours_lookback", 48))
        days = max(1, round(lookback / 24))
        query = f"from:{sender} newer_than:{days}d"

        resp = service.users().messages().list(
            userId="me", q=query, maxResults=25).execute()
        message_ids = [m["id"] for m in resp.get("messages", [])]
        log.info("[Gmail] %d alert emails matched", len(message_ids))

        jobs: list[RawJob] = []
        for mid in message_ids:
            msg = service.users().messages().get(
                userId="me", id=mid, format="full").execute()
            html = self._extract_html(msg.get("payload", {}))
            jobs.extend(self._parse_html(html))
        # Dedup within alerts by url (LinkedIn repeats cards across emails).
        seen, unique = set(), []
        for j in jobs:
            if j.url in seen:
                continue
            seen.add(j.url)
            unique.append(j)
        return unique

    @staticmethod
    def _extract_html(payload: dict) -> str:
        """Walk the MIME tree and return the first text/html body found."""
        stack = [payload]
        while stack:
            part = stack.pop()
            mime = part.get("mimeType", "")
            body = part.get("body", {})
            data = body.get("data")
            if mime == "text/html" and data:
                return base64.urlsafe_b64decode(data).decode("utf-8", "ignore")
            stack.extend(part.get("parts", []) or [])
        # Fallback to any body data.
        data = payload.get("body", {}).get("data")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", "ignore")
        return ""

    def _parse_html(self, html: str) -> list[RawJob]:
        jobs: list[RawJob] = []
        for url, inner in _JOB_LINK_RE.findall(html):
            text = unescape(_TAG_RE.sub(" ", inner))
            text = re.sub(r"\s+", " ", text).strip()
            if not text or len(text) < 3:
                continue
            # Clean tracking params off the LinkedIn URL.
            clean_url = url.split("?")[0]
            jobs.append(RawJob(
                source=self.name,
                title=text,
                company="",           # LinkedIn alert titles bundle role text
                location="",
                description=text,
                url=clean_url,
                raw={"alert_text": text, "url": clean_url},
            ))
        return jobs
