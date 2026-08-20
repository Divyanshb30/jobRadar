"""RemoteOK JSON feed scraper (free — International/Remote pipeline).

The feed is a single JSON array; element 0 is a legal/metadata notice which we
skip. No auth. RemoteOK asks for a descriptive User-Agent.
Feed: https://remoteok.com/api
"""

from __future__ import annotations

import logging

import requests

from common import config
from processing.models import RawJob
from scrapers.base import BaseScraper

log = logging.getLogger("jobradar")

_UA = "JobRadar/1.0 (+https://github.com/) personal job aggregator"


class RemoteOKScraper(BaseScraper):
    name = "RemoteOK"

    def __init__(self):
        self.cfg = config.sources()["free_apis"]["remoteok"]
        self.tags = {t.lower() for t in self.cfg.get("tags", [])}

    @property
    def available(self) -> bool:
        return True  # public feed, no key

    def _fetch(self) -> list[RawJob]:
        resp = requests.get(self.cfg["url"], headers={"User-Agent": _UA}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            return []

        jobs: list[RawJob] = []
        for r in data:
            if not isinstance(r, dict) or "position" not in r:
                continue  # skips the metadata element at index 0
            tags = {str(t).lower() for t in (r.get("tags") or [])}
            # Keep if it carries at least one tag we care about, or the position
            # text mentions ML/AI (RemoteOK tagging is inconsistent).
            pos = str(r.get("position", "")).lower()
            if not (tags & self.tags) and not any(
                k in pos for k in ("machine learning", "ml ", "ai ", "nlp", "data scien")
            ):
                continue
            jobs.append(self._map(r))
        return jobs

    def _map(self, r: dict) -> RawJob:
        loc = r.get("location") or "Remote"
        return RawJob(
            source=self.name,
            title=r.get("position", ""),
            company=r.get("company", ""),
            location=f"Remote ({loc})" if loc.lower() != "remote" else "Remote",
            description=r.get("description", ""),
            url=r.get("url", r.get("apply_url", "")),
            salary_min=r.get("salary_min"),
            salary_max=r.get("salary_max"),
            currency="USD",
            posted_at=self._parse_dt(r.get("date") or r.get("epoch")),
            raw=r,
        )
