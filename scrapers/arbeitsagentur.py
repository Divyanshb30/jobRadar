"""Bundesagentur für Arbeit Jobsuche API (Germany — International pipeline).

Germany's federal employment-agency job search, v6 JSON. Free: the ``client_id``
is a public constant sent as the ``X-API-Key`` header (not a secret). The list
response has no description, so we set a compact synthetic one and link to the
public job-detail page; the LLM scores on title + role + location.

Only ``ARBEIT`` (regular employment) listings are kept — the feed also carries
self-employment (``SELBSTAENDIGKEIT``) and apprenticeships (``AUSBILDUNG``).
Docs: https://github.com/bundesAPI/jobsuche-api
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
_DETAIL = "https://www.arbeitsagentur.de/jobsuche/jobdetail/"


class ArbeitsagenturScraper(BaseScraper):
    name = "Arbeitsagentur"

    def __init__(self):
        self.cfg = config.sources()["free_apis"]["arbeitsagentur"]
        self.cities = self.cfg.get("cities", ["Berlin"])
        self.size = int(self.cfg.get("size", 25))
        self.radius = int(self.cfg.get("radius_km", 50))
        self.client_id = self.cfg.get("client_id", "jobboerse-jobsuche")
        # First few resume titles are the query terms (English AI/ML roles).
        self.queries = config.pipelines()["search_keywords"][:3]
        self.keep_re = re.compile(config.pipelines()["title_keywords_regex"],
                                  re.IGNORECASE)

    @property
    def available(self) -> bool:
        return True  # public API, keyless (constant client id)

    def _fetch(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        headers = {"User-Agent": _UA, "X-API-Key": self.client_id,
                   "Accept": "application/json"}
        for city in self.cities:
            for was in self.queries:
                params = {"was": was, "wo": city, "umkreis": self.radius,
                          "size": self.size, "page": 1}
                try:
                    resp = requests.get(self.cfg["base_url"], params=params,
                                        headers=headers, timeout=30)
                    if resp.status_code != 200:
                        log.warning("[Arbeitsagentur] %s/%s -> HTTP %s",
                                    city, was, resp.status_code)
                        continue
                    for r in resp.json().get("ergebnisliste", []):
                        job = self._map(r)
                        if job:
                            jobs.append(job)
                except (requests.RequestException, ValueError) as exc:
                    log.warning("[Arbeitsagentur] %s/%s failed: %s",
                                city, was, exc)
        return jobs

    def _map(self, r: dict) -> RawJob | None:
        if r.get("stellenangebotsart") != "ARBEIT":
            return None
        title = r.get("stellenangebotsTitel")
        if not title or not self.keep_re.search(title):
            return None
        locs = r.get("stellenlokationen") or []
        adr = (locs[0].get("adresse") if locs else {}) or {}
        city = adr.get("ort", "")
        location = ", ".join(p for p in (city, "Germany") if p) or "Germany"
        refnr = r.get("referenznummer") or r.get("refnr") or ""
        company = r.get("firma", "")
        remote = " (remote possible)" if r.get("homeofficemoeglich") else ""
        beruf = r.get("hauptberuf") or ""
        desc = (f"{title}. {beruf}. Employer: {company}. "
                f"Location: {location}{remote}.")
        return RawJob(
            source=self.name,
            title=title,
            company=company,
            location=location,
            description=desc,
            url=(_DETAIL + refnr) if refnr else "",
            posted_at=self._parse_dt(r.get("aenderungsdatum")
                                     or r.get("datumErsteVeroeffentlichung")),
            raw=r,
        )
