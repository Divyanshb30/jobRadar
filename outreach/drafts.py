"""Output — assemble messages and write drafts (Gmail + local files).

Email: one draft per email-bearing contact, created in Gmail if a compose-scoped
OAuth token is available, otherwise written to ``out_dir`` as a fallback. LinkedIn
and X copy is always written to files for you to paste and send manually. A
consolidated Markdown summary is always written. Nothing is ever sent.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from datetime import date
from email.mime.text import MIMEText
from pathlib import Path

from common import config
from outreach.models import OutreachItem

log = logging.getLogger("jobradar")

try:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    _GMAIL_OK = True
except Exception:  # pragma: no cover
    _GMAIL_OK = False

_COMPOSE_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly",
                   "https://www.googleapis.com/auth/gmail.compose"]


def _profile_field(profile: str, label: str, default: str = "") -> str:
    m = re.search(rf"\*\*{label}:\*\*\s*(.+)", profile)
    return m.group(1).strip() if m else default


class DraftWriter:
    def __init__(self):
        self.cfg = config.outreach()
        ocfg = self.cfg.get("output", {})
        self.want_gmail = bool(ocfg.get("gmail_drafts", True))
        self.out_dir = Path(ocfg.get("out_dir", "output/outreach"))
        self.from_addr = config.env(ocfg.get("from_env", "GMAIL_SENDER")) or ""
        self.email_cap = int(ocfg.get("daily_email_cap", 12))
        prof = ""
        try:
            prof = Path(self.cfg.get("profile_path",
                                     "config/outreach_profile.md")).read_text(
                encoding="utf-8")
        except OSError:
            pass
        self.my_name = _profile_field(prof, "Name", "")
        self.my_linkedin = _profile_field(prof, "LinkedIn", "")
        self._service = None
        self._gmail_tried = False

    # ---- public ----

    def write(self, items: list[OutreachItem]) -> dict:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        emails_made = 0
        summary_lines = [f"# Outreach — {date.today().isoformat()}", ""]
        for item in items:
            summary_lines += self._item_summary(item)
            emails_made += self._write_item(item, emails_made)
        summary = "\n".join(summary_lines)
        (self.out_dir / f"outreach_{date.today().isoformat()}.md").write_text(
            summary, encoding="utf-8")
        log.info("[outreach] wrote %d item file(s), %d email draft(s); dir=%s",
                 len(items), emails_made, self.out_dir)
        return {"items": len(items), "email_drafts": emails_made,
                "out_dir": str(self.out_dir)}

    # ---- per-item ----

    def _assemble_email(self, item: OutreachItem, first_name: str) -> str:
        greeting = f"Hi {first_name}," if first_name else "Hi there,"
        sig = "\n".join(x for x in ("Best,", self.my_name, self.my_linkedin) if x)
        return f"{greeting}\n\n{item.email_body}\n\n{sig}"

    def _write_item(self, item: OutreachItem, made_so_far: int) -> int:
        made = 0
        slug = re.sub(r"[^a-z0-9]+", "-", item.company.lower()).strip("-") or "job"
        lines = [f"# {item.role} @ {item.company}", "",
                 f"- Location: {item.location}", f"- Score: {item.score}",
                 f"- Job: {item.job_url}", f"- Hook: {item.company_hook}", ""]
        if item.contacts:
            lines.append("## Contacts")
            for c in item.contacts:
                lines.append(f"- **{c.name}** — {c.title} | {c.linkedin_url} | "
                             f"{c.email or '(no email)'} ({c.email_confidence})")
            lines.append("")
        # Email drafts (one per contact with an email).
        if self.cfg.get("channels", {}).get("email", True):
            for c in item.contacts:
                body = self._assemble_email(item, c.first_name)
                if c.email and (made_so_far + made) < self.email_cap:
                    if self._create_gmail_draft(c.email, item.subject, body):
                        made += 1
                lines += ["## Email draft"
                          f" → {c.email or '(no address found)'}",
                          f"**Subject:** {item.subject}", "", "```", body,
                          "```", ""]
                break   # one email draft per job (primary contact)
        if self.cfg.get("channels", {}).get("linkedin") and item.linkedin_note:
            target = item.contacts[0].linkedin_url if item.contacts else ""
            lines += [f"## LinkedIn note → {target}",
                      f"({len(item.linkedin_note)} chars)", "```",
                      item.linkedin_note, "```", ""]
        if self.cfg.get("channels", {}).get("twitter") and item.twitter_dm:
            lines += ["## X/Twitter DM", "```", item.twitter_dm, "```", ""]
        (self.out_dir / f"{slug}_{item.score}.md").write_text(
            "\n".join(lines), encoding="utf-8")
        return made

    def _item_summary(self, item: OutreachItem) -> list[str]:
        c = item.contacts[0] if item.contacts else None
        who = f"{c.name} ({c.email or 'no email'})" if c else "no contact found"
        return [f"## [{item.score}] {item.role} @ {item.company}",
                f"- {item.location} · {who}", ""]

    # ---- Gmail ----

    def _gmail_service(self):
        if self._gmail_tried:
            return self._service
        self._gmail_tried = True
        if not (self.want_gmail and _GMAIL_OK):
            return None
        gcfg = config.sources()["free_apis"]["gmail_alerts"]
        raw = config.env(gcfg.get("token_env"))
        info = None
        if raw:
            try:
                info = json.loads(raw)
            except json.JSONDecodeError:
                pass
        else:
            tp = Path(config.CONFIG_DIR).parent / gcfg.get("token_path",
                                                           "gmail_token.json")
            if tp.exists():
                info = json.loads(tp.read_text(encoding="utf-8"))
        if not info:
            log.info("[outreach] no Gmail token — email drafts go to files only")
            return None
        try:
            creds = Credentials.from_authorized_user_info(info, _COMPOSE_SCOPES)
            self._service = build("gmail", "v1", credentials=creds,
                                  cache_discovery=False)
        except Exception as exc:  # noqa: BLE001
            log.warning("[outreach] Gmail service unavailable: %s", exc)
            self._service = None
        return self._service

    def _create_gmail_draft(self, to_addr: str, subject: str, body: str) -> bool:
        service = self._gmail_service()
        if service is None:
            return False
        try:
            msg = MIMEText(body)
            msg["to"] = to_addr
            if self.from_addr:
                msg["from"] = self.from_addr
            msg["subject"] = subject
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            service.users().drafts().create(
                userId="me", body={"message": {"raw": raw}}).execute()
            return True
        except Exception as exc:  # noqa: BLE001 - compose scope missing, etc.
            log.warning("[outreach] Gmail draft failed (%s) — using files. "
                        "Re-run scripts/gmail_authorize.py for compose scope.",
                        exc)
            self._service = None       # stop retrying this run
            self.want_gmail = False
            return False
