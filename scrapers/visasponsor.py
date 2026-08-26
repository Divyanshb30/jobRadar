"""visasponsor.jobs scraper (International pipeline — visa-sponsorship jobs).

The site has no JSON API: ``/api/jobs?country=…&page=…`` returns server-rendered
HTML, so we parse the job cards (``div.job`` inside an ``a[href^="/api/jobs/"]``).
Every listing here is a visa-sponsorship role, so visa sponsorship is implied.

The list page carries no description, so for the few AI/ML-titled survivors we
fetch the detail page to get one (``fetch_description``). Pages are 0-indexed.
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
_BASE = "https://visasponsor.jobs"


class VisaSponsorScraper(BaseScraper):
    name = "VisaSponsor"

    def __init__(self):
        self.cfg = config.sources()["free_apis"]["visasponsor"]
        self.countries = self.cfg.get("countries", ["United-Kingdom"])
        self.keywords = self.cfg.get("keywords", ["AI Engineer"])
        self.max_pages = int(self.cfg.get("max_pages", 2))
        self.fetch_desc = bool(self.cfg.get("fetch_description", True))
        self.max_desc = int(self.cfg.get("max_descriptions", 40))
        self.keep_re = re.compile(config.pipelines()["title_keywords_regex"],
                                  re.IGNORECASE)

    @property
    def available(self) -> bool:
        return True  # public site, no key

    def _fetch(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        seen: set[str] = set()          # a job can match multiple keywords
        enriched = 0
        sess = requests.Session()
        sess.headers.update({"User-Agent": _UA})
        for country in self.countries:
            for kw in self.keywords:
                for page in range(self.max_pages):
                    try:
                        resp = sess.get(self.cfg["base_url"],
                                        params={"country": country, "keyword": kw,
                                                "page": page}, timeout=30)
                        if resp.status_code != 200:
                            log.warning("[VisaSponsor] %s/%s p%d -> HTTP %s",
                                        country, kw, page, resp.status_code)
                            break
                        cards = self._parse_cards(resp.text, country)
                        if not cards:
                            break
                        for job in cards:
                            if job.url in seen:
                                continue
                            seen.add(job.url)
                            if self.fetch_desc and enriched < self.max_desc:
                                self._enrich(sess, job)
                                enriched += 1
                            jobs.append(job)
                    except requests.RequestException as exc:
                        log.warning("[VisaSponsor] %s/%s p%d failed: %s",
                                    country, kw, page, exc)
                        break
        return jobs

    def _parse_cards(self, html: str, country: str) -> list[RawJob]:
        soup = BeautifulSoup(html, "html.parser")
        out: list[RawJob] = []
        anchors = [a for a in soup.select("a[href^='/api/jobs/']")
                   if a.get("href") != "/api/jobs"]
        for a in anchors:
            card = a.find(class_="job") or a
            title_el = card.select_one(".fs-5")
            title = title_el.get_text(strip=True) if title_el else ""
            if not title or not self.keep_re.search(title):
                continue   # cheap title gate before we keep the card
            company_el = card.select_one(".employer-name")
            company = company_el.get_text(strip=True) if company_el else ""
            loc_el = card.select_one(".col-11")
            location = (" ".join(loc_el.get_text(" ", strip=True).split())
                        if loc_el else country.replace("-", " "))
            visa = ", ".join(t.get_text(strip=True)
                             for t in card.select("#classificationTags .tag"))
            industry = ", ".join(c.get_text(strip=True)
                                 for c in card.select(".job-classification span"))
            url = _BASE + a["href"]
            desc = (f"Visa sponsorship: {visa or 'yes'}. "
                    f"Industry: {industry or 'n/a'}. Location: {location}.")
            out.append(RawJob(source=self.name, title=title, company=company,
                              location=location, description=desc, url=url,
                              raw={"country": country, "visa": visa,
                                   "industry": industry}))
        return out

    def _enrich(self, sess: requests.Session, job: RawJob) -> None:
        """Fetch the detail page and replace the synthetic description."""
        try:
            resp = sess.get(job.url, timeout=30)
            if resp.status_code != 200:
                return
            soup = BeautifulSoup(resp.text, "html.parser")
            main = soup.find("main") or soup.find(class_=re.compile("descr", re.I))
            text = (main or soup).get_text(" ", strip=True)
            if text:
                job.description = (job.description + " " + text)[:4000]
        except requests.RequestException:
            pass
