"""Adzuna REST API scraper (free tier).

One request per (country, keyword) page. Adzuna's country coverage does not
include every geography we target (e.g. UAE is unsupported) — unsupported
countries simply error and are skipped per-country, so the rest still run.
Docs: https://developer.adzuna.com/
"""

from __future__ import annotations

import logging

import requests

from common import config
from processing.models import RawJob
from scrapers.base import BaseScraper

log = logging.getLogger("jobradar")


class AdzunaScraper(BaseScraper):
    name = "Adzuna"

    def __init__(self):
        self.cfg = config.sources()["free_apis"]["adzuna"]
        self.app_id = config.env(self.cfg["app_id_env"])
        self.app_key = config.env(self.cfg["app_key_env"])

    @property
    def available(self) -> bool:
        return bool(self.app_id and self.app_key)

    def _fetch(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        keywords = config.pipelines()["search_keywords"][:4]
        per_page = int(self.cfg.get("max_results_per_query", 50))
        max_days = int(self.cfg.get("max_days_old", 2))

        for country in self.cfg.get("countries", ["gb", "in"]):
            for kw in keywords:
                url = f"{self.cfg['base_url']}/{country}/search/1"
                params = {
                    "app_id": self.app_id,
                    "app_key": self.app_key,
                    "results_per_page": per_page,
                    "what": kw,
                    "max_days_old": max_days,
                    "content-type": "application/json",
                }
                try:
                    resp = requests.get(url, params=params, timeout=30)
                    if resp.status_code != 200:
                        log.warning("[Adzuna] %s '%s' -> HTTP %s",
                                    country, kw, resp.status_code)
                        continue
                    for r in resp.json().get("results", []):
                        job = self._map(r, country)
                        if job:
                            jobs.append(job)
                except requests.RequestException as exc:
                    log.warning("[Adzuna] %s '%s' failed: %s", country, kw, exc)
        return jobs

    def _map(self, r: dict, country: str) -> RawJob | None:
        title = r.get("title")
        if not title:
            return None
        loc = (r.get("location") or {}).get("display_name", "")
        return RawJob(
            source=self.name,
            title=title,
            company=(r.get("company") or {}).get("display_name", ""),
            location=loc or country.upper(),
            description=r.get("description", ""),
            url=r.get("redirect_url", ""),
            salary_min=r.get("salary_min"),
            salary_max=r.get("salary_max"),
            currency={"gb": "GBP", "in": "INR", "sg": "SGD",
                      "de": "EUR", "nl": "EUR"}.get(country),
            posted_at=self._parse_dt(r.get("created")),
            raw=r,
        )
