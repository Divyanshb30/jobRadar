"""Personalisation — turn a job + profile into channel-specific copy.

One LLM call per job returns a small JSON object: a company hook, an email
subject + body (paragraphs only; greeting and signature are added per-contact by
the drafts step), a <=300-char LinkedIn note, and a short X DM. The config
templates supply the tone/structure guidance, so editing them changes the voice.
Falls back to a plain template if the LLM is unavailable.
"""

from __future__ import annotations

import logging
from pathlib import Path

from common import config, llm
from outreach.models import OutreachItem

log = logging.getLogger("jobradar")


class Personalizer:
    def __init__(self):
        self.cfg = config.outreach()
        self.channels = self.cfg.get("channels", {})
        self.templates = self.cfg.get("templates", {})
        self.profile = self._load_profile()

    def _load_profile(self) -> str:
        path = Path(self.cfg.get("profile_path", "config/outreach_profile.md"))
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            log.warning("[outreach] profile file missing: %s", path)
            return ""

    def personalise(self, item: OutreachItem) -> None:
        data = llm.complete_json(self._prompt(item), temperature=0.5,
                                 max_tokens=900)
        if not data:
            self._fallback(item)
            return
        item.company_hook = str(data.get("company_hook", "")).strip()
        item.subject = str(data.get("subject", f"AI Engineer interested in {item.company}")).strip()
        item.email_body = str(data.get("email_body", "")).strip()
        item.linkedin_note = str(data.get("linkedin_note", "")).strip()[:300]
        item.twitter_dm = str(data.get("twitter_dm", "")).strip()[:280]
        if not item.email_body:
            self._fallback(item)

    def _prompt(self, item: OutreachItem) -> str:
        want = []
        if self.channels.get("email", True):
            want.append('"subject": str, "email_body": str (2-3 short paragraphs, '
                        'NO greeting line and NO sign-off — those are added later)')
        if self.channels.get("linkedin"):
            want.append('"linkedin_note": str (MAX 300 characters, no links)')
        if self.channels.get("twitter"):
            want.append('"twitter_dm": str (under 280 characters, casual)')
        fields = ",\n  ".join(['"company_hook": str (one specific angle about the company/role)'] + want)
        g = self.templates
        guide = "\n".join(
            f"{ch} guidance:\n{g.get(ch, {}).get('guidance', '').strip()}"
            for ch in ("email", "linkedin", "twitter")
            if self.channels.get(ch) and g.get(ch, {}).get("guidance"))
        return f"""You write concise, specific, non-generic cold outreach for a job seeker.
Use ONE concrete proof point from the CANDIDATE PROFILE that best matches the JOB.
Never invent facts. No buzzword soup, no "I am writing to express interest".

CANDIDATE PROFILE:
{self.profile}

JOB:
- Role: {item.role}
- Company: {item.company}
- Location: {item.location}
- Description: {item.jd_snippet[:900] or '(not available)'}

{guide}

Return ONLY a JSON object with these fields:
{{
  {fields}
}}"""

    def _fallback(self, item: OutreachItem) -> None:
        """Deterministic minimal copy if the LLM is unavailable."""
        item.company_hook = item.company_hook or f"AI/LLM work at {item.company}"
        item.subject = item.subject or f"AI Engineer keen on the {item.role} role"
        item.email_body = (
            f"I came across the {item.role} role at {item.company} and it lines up "
            f"closely with what I do: building production LLM and agentic systems "
            f"(multi-agent orchestration, RAG, Azure OpenAI). I recently shipped a "
            f"five-agent platform approved for production after a 40-stakeholder "
            f"review, automating 40+ hours/week of manual work.\n\n"
            f"Would you be open to a quick chat, or could you point me to the right "
            f"person on the team?")
        item.linkedin_note = (item.linkedin_note or
            f"Hi — I build production LLM/agentic systems and the {item.role} role "
            f"at {item.company} is a strong fit. Would love to connect.")[:300]
