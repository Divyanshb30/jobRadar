"""Contact discovery — free-first.

Given a company + target personas, find 1-2 real people (name, title, LinkedIn)
via Serper (Google) people search, resolve the company email domain, and guess a
work email from common patterns. If ``HUNTER_API_KEY`` is set, emails are found
and verified via Hunter instead of guessed. Everything fails soft — a company we
can't research just yields fewer/again no contacts, never an exception.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import requests

from common import config
from outreach.models import Contact

log = logging.getLogger("jobradar")

_UA = "JobRadar/1.0 personal job-search outreach helper"
# Domains that are never a company's own site.
_AGG = ("linkedin.", "glassdoor.", "indeed.", "wikipedia.", "facebook.",
        "twitter.", "x.com", "youtube.", "crunchbase.", "bloomberg.",
        "ambitionbox.", "naukri.", "wellfound.", "ycombinator.", "github.")


class Discovery:
    def __init__(self):
        self.ocfg = config.outreach().get("discovery", {})
        scfg = config.sources()["free_apis"]["serper"]
        self.serper_url = scfg["base_url"]
        self.serper_key = config.env(scfg["api_key_env"])
        self.hunter_key = config.env(self.ocfg.get("hunter_api_key_env",
                                                   "HUNTER_API_KEY"))
        self.n_results = int(self.ocfg.get("serper_results", 5))
        self.patterns = self.ocfg.get("email_patterns",
                                      ["{first}.{last}", "{first}"])

    @property
    def available(self) -> bool:
        return bool(self.serper_key)

    # ---- Serper helper ----

    def _serper(self, query: str) -> list[dict]:
        if not self.serper_key:
            return []
        try:
            r = requests.post(self.serper_url, json={"q": query},
                              headers={"X-API-KEY": self.serper_key,
                                       "Content-Type": "application/json",
                                       "User-Agent": _UA}, timeout=30)
            if r.status_code != 200:
                log.warning("[outreach.discovery] serper HTTP %s", r.status_code)
                return []
            return r.json().get("organic", []) or []
        except (requests.RequestException, ValueError) as exc:
            log.warning("[outreach.discovery] serper failed: %s", exc)
            return []

    # ---- public API ----

    def find_contacts(self, company: str, personas: list[str],
                      n: int) -> list[Contact]:
        if not company or not self.available:
            return []
        persona_q = " OR ".join(f'"{p}"' for p in personas[:6])
        query = f'site:linkedin.com/in ({persona_q}) "{company}"'
        contacts: list[Contact] = []
        seen: set[str] = set()
        for r in self._serper(query)[:self.n_results]:
            c = self._parse_person(r, company)
            if c and c.linkedin_url not in seen:
                seen.add(c.linkedin_url)
                contacts.append(c)
            if len(contacts) >= n:
                break
        # Resolve emails once we know the domain.
        domain = self.company_domain(company)
        for c in contacts:
            if domain:
                c.email, c.email_confidence = self._resolve_email(c, domain)
        return contacts

    def company_domain(self, company: str) -> str:
        for r in self._serper(f"{company} official website"):
            link = r.get("link", "")
            host = urlparse(link).netloc.lower().lstrip("www.")
            if host and not any(a in host for a in _AGG):
                return host
        return ""

    # ---- parsing / email ----

    @staticmethod
    def _parse_person(result: dict, company: str) -> Contact | None:
        title = result.get("title", "")
        link = result.get("link", "")
        if "linkedin.com/in" not in link:
            return None
        # "Name - Title - Company | LinkedIn"
        head = re.split(r"\s[|–—]\s|\sLinkedIn", title)[0]
        parts = [p.strip() for p in head.split(" - ") if p.strip()]
        if not parts:
            return None
        name = parts[0]
        role = parts[1] if len(parts) > 1 else ""
        # Guard against non-person results.
        if len(name.split()) > 4 or not name:
            return None
        return Contact(name=name, title=role, company=company,
                       linkedin_url=link.split("?")[0], source="serper")

    def _resolve_email(self, c: Contact, domain: str) -> tuple[str, str]:
        if self.hunter_key:
            email = self._hunter(c, domain)
            if email:
                return email, "verified"
        # Guess from the first pattern.
        parts = c.name.lower().split()
        if len(parts) < 2:
            return "", "none"
        first, last = re.sub(r"[^a-z]", "", parts[0]), re.sub(r"[^a-z]", "", parts[-1])
        if not (first and last):
            return "", "none"
        pat = self.patterns[0]
        local = pat.format(first=first, last=last, f=first[:1], l=last[:1])
        return f"{local}@{domain}", "guessed"

    def _hunter(self, c: Contact, domain: str) -> str:
        try:
            parts = c.name.split()
            r = requests.get("https://api.hunter.io/v2/email-finder",
                             params={"domain": domain, "first_name": parts[0],
                                     "last_name": parts[-1],
                                     "api_key": self.hunter_key}, timeout=30)
            if r.status_code == 200:
                return (r.json().get("data") or {}).get("email", "") or ""
        except (requests.RequestException, ValueError, KeyError, IndexError):
            pass
        return ""
