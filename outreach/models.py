"""Small dataclasses shared across the outreach steps."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Contact:
    """A person to reach out to at a target company."""
    name: str = ""
    title: str = ""
    company: str = ""
    linkedin_url: str = ""
    twitter_handle: str = ""
    email: str = ""
    email_confidence: str = "none"     # verified | guessed | none
    source: str = ""                    # where we found them

    @property
    def first_name(self) -> str:
        return (self.name.split() or [""])[0]


@dataclass
class OutreachItem:
    """One job + its discovered contacts + the personalised copy per channel."""
    company: str
    role: str
    location: str = ""
    job_url: str = ""
    score: int = 0
    jd_snippet: str = ""                # first chunk of the job description
    company_hook: str = ""              # a researched, company-specific angle
    contacts: list[Contact] = field(default_factory=list)
    # Personalised copy (filled by the personaliser).
    subject: str = ""
    email_body: str = ""
    linkedin_note: str = ""
    twitter_dm: str = ""
    error: Optional[str] = None
