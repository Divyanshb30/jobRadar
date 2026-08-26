"""Google Jobs via Serper.dev (free tier: 2,500 searches/month).

Serper mirrors Google's SERP. Depending on the query, jobs can surface either
in a dedicated ``jobs`` block or as ``organic`` results, so we parse both and
fall back gracefully. Each query = one search credit, so we budget the number
of (keyword x geo) combinations against ``queries_per_day``.
Docs: https://serper.dev/
"""

from __future__ import annotations

import logging

import requests

from common import config
from processing.models import RawJob
from scrapers.base import BaseScraper

log = logging.getLogger("jobradar")


class SerperGoogleJobsScraper(BaseScraper):
    name = "Google Jobs"

    def __init__(self):
        self.cfg = config.sources()["free_apis"]["serper"]
        self.api_key = config.env(self.cfg["api_key_env"])

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _queries(self) -> list[str]:
        roles = ["AI Engineer", "GenAI Engineer", "Applied AI Engineer",
                 "Forward Deployed Engineer", "Machine Learning Engineer",
                 "ML Engineer", "NLP Engineer", "Data Scientist"]
        # Dubai / UAE leads — top-priority geography. Singapore removed from scope.
        geos = ["Dubai UAE", "India", "United Kingdom",
                "Germany", "Netherlands", "Ireland", "Remote"]
        budget = int(self.cfg.get("queries_per_day", 80))
        queries = [f'"{r}" jobs {g}' for g in geos for r in roles]
        return queries[:budget]

    def _fetch(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        headers = {"X-API-KEY": self.api_key, "Content-Type": "application/json"}
        for q in self._queries():
            try:
                resp = requests.post(
                    self.cfg["base_url"],
                    json={"q": q},
                    headers=headers,
                    timeout=30,
                )
                if resp.status_code != 200:
                    log.warning("[Serper] '%s' -> HTTP %s: %s",
                                q, resp.status_code, resp.text[:200])
                    continue
                jobs.extend(self._parse(resp.json()))
            except requests.RequestException as exc:
                log.warning("[Serper] '%s' failed: %s", q, exc)
        return jobs

    def _parse(self, data: dict) -> list[RawJob]:
        out: list[RawJob] = []
        # Preferred: a structured jobs block.
        for j in data.get("jobs", []) or []:
            title = j.get("title")
            if not title:
                continue
            out.append(RawJob(
                source=self.name,
                title=title,
                company=j.get("company", ""),
                location=j.get("location", ""),
                description=j.get("description", j.get("snippet", "")),
                url=j.get("link", j.get("url", "")),
                salary_raw=j.get("salary", ""),
                posted_at=self._parse_dt(j.get("postedAt")),
                raw=j,
            ))
        # Fallback: organic results that look like job pages.
        if not out:
            for o in data.get("organic", []) or []:
                title = o.get("title", "")
                if not title:
                    continue
                out.append(RawJob(
                    source=self.name,
                    title=title,
                    company="",
                    location="",
                    description=o.get("snippet", ""),
                    url=o.get("link", ""),
                    raw=o,
                ))
        return out
