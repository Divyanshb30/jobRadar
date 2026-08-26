"""Himalayas free remote-jobs API (International/Remote pipeline).

Free, keyless public JSON API. ``jobs`` array with offset/limit pagination
(``totalCount``/``nextCursor`` also returned). Remote roles worldwide; we keep
AI/ML-relevant titles only.
API: https://himalayas.app/jobs/api
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


class HimalayasScraper(BaseScraper):
    name = "Himalayas"

    def __init__(self):
        self.cfg = config.sources()["free_apis"]["himalayas"]
        self.limit = int(self.cfg.get("limit", 20))
        self.max_pages = int(self.cfg.get("max_pages", 3))
        self.keep_re = re.compile(config.pipelines()["title_keywords_regex"],
                                  re.IGNORECASE)

    @property
    def available(self) -> bool:
        return True  # public API, no key

    def _fetch(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        for page in range(self.max_pages):
            offset = page * self.limit
            try:
                resp = requests.get(self.cfg["url"],
                                    params={"limit": self.limit, "offset": offset},
                                    headers={"User-Agent": _UA}, timeout=30)
                if resp.status_code != 200:
                    log.warning("[Himalayas] offset %d -> HTTP %s", offset,
                                resp.status_code)
                    break
                payload = resp.json()
                batch = payload.get("jobs") or []
                if not batch:
                    break
                for r in batch:
                    job = self._map(r)
                    if job:
                        jobs.append(job)
                if offset + self.limit >= int(payload.get("totalCount", 0)):
                    break
            except (requests.RequestException, ValueError) as exc:
                log.warning("[Himalayas] offset %d failed: %s", offset, exc)
                break
        return jobs

    def _map(self, r: dict) -> RawJob | None:
        title = r.get("title")
        if not title or not self.keep_re.search(title):
            return None
        restrictions = r.get("locationRestrictions") or []
        loc = "Remote"
        if restrictions:
            loc = "Remote (" + ", ".join(str(x) for x in restrictions[:3]) + ")"
        return RawJob(
            source=self.name,
            title=title,
            company=r.get("companyName", ""),
            location=loc,
            description=r.get("description", r.get("excerpt", "")),
            url=r.get("applicationLink", ""),
            salary_min=r.get("minSalary"),
            salary_max=r.get("maxSalary"),
            currency=r.get("currency"),
            posted_at=self._parse_dt(r.get("pubDate")),
            raw=r,
        )
