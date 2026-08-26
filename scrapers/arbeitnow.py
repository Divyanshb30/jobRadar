"""Arbeitnow free job-board API (Germany/EU + remote — International pipeline).

Free, keyless public JSON feed. One array of jobs per page with Laravel-style
``?page=N`` pagination. We keep only AI/ML-relevant titles (the feed spans every
industry) so downstream layers aren't flooded.
Feed: https://www.arbeitnow.com/api/job-board-api
"""

from __future__ import annotations

import logging
import re

import requests

from common import config
from processing.models import RawJob
from scrapers.base import BaseScraper

log = logging.getLogger("jobradar")

_UA = "JobRadar/1.0 (+https://github.com/) personal job aggregator"


class ArbeitnowScraper(BaseScraper):
    name = "Arbeitnow"

    def __init__(self):
        self.cfg = config.sources()["free_apis"]["arbeitnow"]
        self.max_pages = int(self.cfg.get("max_pages", 3))
        self.keep_re = re.compile(config.pipelines()["title_keywords_regex"],
                                  re.IGNORECASE)

    @property
    def available(self) -> bool:
        return True  # public feed, no key

    def _fetch(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        for page in range(1, self.max_pages + 1):
            try:
                resp = requests.get(self.cfg["url"], params={"page": page},
                                    headers={"User-Agent": _UA}, timeout=30)
                if resp.status_code != 200:
                    log.warning("[Arbeitnow] page %d -> HTTP %s", page,
                                resp.status_code)
                    break
                data = resp.json().get("data") or []
                if not data:
                    break
                for r in data:
                    job = self._map(r)
                    if job:
                        jobs.append(job)
            except (requests.RequestException, ValueError) as exc:
                log.warning("[Arbeitnow] page %d failed: %s", page, exc)
                break
        return jobs

    def _map(self, r: dict) -> RawJob | None:
        title = r.get("title")
        if not title or not self.keep_re.search(title):
            return None
        loc = r.get("location") or ("Remote" if r.get("remote") else "Germany")
        return RawJob(
            source=self.name,
            title=title,
            company=r.get("company_name", ""),
            location=loc,
            description=r.get("description", ""),
            url=r.get("url", ""),
            posted_at=self._parse_dt(r.get("created_at")),
            raw=r,
        )
