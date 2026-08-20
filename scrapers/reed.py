"""Reed.co.uk REST API scraper (free, UK — International pipeline).

Auth is HTTP Basic with the API key as the username and an empty password.
Docs: https://www.reed.co.uk/developers/jobseeker
"""

from __future__ import annotations

import logging

import requests

from common import config
from processing.models import RawJob
from scrapers.base import BaseScraper

log = logging.getLogger("jobradar")


class ReedScraper(BaseScraper):
    name = "Reed"

    def __init__(self):
        self.cfg = config.sources()["free_apis"]["reed"]
        self.api_key = config.env(self.cfg["api_key_env"])

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _fetch(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        keywords = config.pipelines()["search_keywords"][:5]
        take = int(self.cfg.get("results_to_take", 50))

        for kw in keywords:
            params = {
                "keywords": kw,
                "resultsToTake": take,
                "postedByRecruitmentAgency": "true",
            }
            try:
                resp = requests.get(
                    self.cfg["base_url"],
                    params=params,
                    auth=(self.api_key, ""),   # key as username, blank password
                    timeout=30,
                )
                if resp.status_code != 200:
                    log.warning("[Reed] '%s' -> HTTP %s", kw, resp.status_code)
                    continue
                for r in resp.json().get("results", []):
                    job = self._map(r)
                    if job:
                        jobs.append(job)
            except requests.RequestException as exc:
                log.warning("[Reed] '%s' failed: %s", kw, exc)
        return jobs

    def _map(self, r: dict) -> RawJob | None:
        title = r.get("jobTitle")
        if not title:
            return None
        return RawJob(
            source=self.name,
            title=title,
            company=r.get("employerName", ""),
            location=r.get("locationName", "United Kingdom"),
            description=r.get("jobDescription", ""),
            url=r.get("jobUrl", ""),
            salary_min=r.get("minimumSalary"),
            salary_max=r.get("maximumSalary"),
            currency="GBP",
            posted_at=self._parse_dt(r.get("date")),  # dd/mm/yyyy handled below
            raw=r,
        )
