"""gov.uk Find a Job (DWP) scraper — DISABLED by default (International/UK).

Find a Job has no public API. As of 2026 the site was rebranded to a
JS-rendered "Work Hub" single-page app, so its server HTML no longer contains
job results — a plain HTTP GET returns the app shell, not listings. A working
scraper would need a headless browser, which is out of scope for this pipeline.

This adapter is therefore **disabled** via ``free_apis.findajob_uk.enabled:
false`` in ``config/sources.yaml``. It ships a best-effort parser for the
classic server-rendered layout so that, if the site ever reverts, flipping the
flag revives it. UK visa-sponsorship coverage is handled instead by the UK
sponsor register (``processing/sponsor_register.py``), the visasponsor.jobs
scraper, and Reed.
"""

from __future__ import annotations

import logging
import re

import requests
from bs4 import BeautifulSoup

from common import config
from processing.models import RawJob
from scrapers.base import BaseScraper

log = logging.getLogger("jobradar")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) JobRadar/1.0 "
       "personal job aggregator")
_BASE = "https://findajob.dwp.gov.uk"


class FindAJobUKScraper(BaseScraper):
    name = "FindAJob"

    def __init__(self):
        self.cfg = config.sources()["free_apis"]["findajob_uk"]
        self.max_pages = int(self.cfg.get("max_pages", 2))
        self.queries = config.pipelines()["search_keywords"][:3]
        self.keep_re = re.compile(config.pipelines()["title_keywords_regex"],
                                  re.IGNORECASE)

    @property
    def available(self) -> bool:
        # Off unless explicitly re-enabled (the live site is a JS SPA).
        return bool(self.cfg.get("enabled", False))

    def _fetch(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        sess = requests.Session()
        sess.headers.update({"User-Agent": _UA})
        for q in self.queries:
            for page in range(1, self.max_pages + 1):
                try:
                    resp = sess.get(self.cfg["base_url"],
                                    params={"q": q, "p": page}, timeout=30)
                    if resp.status_code != 200:
                        break
                    cards = self._parse(resp.text)
                    if not cards:
                        break
                    jobs.extend(cards)
                except requests.RequestException as exc:
                    log.warning("[FindAJob] '%s' p%d failed: %s", q, page, exc)
                    break
        if not jobs:
            log.info("[FindAJob] no server-rendered results (site is JS-only?)")
        return jobs

    def _parse(self, html: str) -> list[RawJob]:
        soup = BeautifulSoup(html, "html.parser")
        out: list[RawJob] = []
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            if not re.search(r"/details/\d+", href):
                continue
            title = a.get_text(strip=True)
            if not title or not self.keep_re.search(title):
                continue
            out.append(RawJob(
                source=self.name, title=title,
                location="United Kingdom",
                url=href if href.startswith("http") else _BASE + href,
                raw={"href": href}))
        return out
