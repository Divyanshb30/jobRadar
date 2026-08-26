"""Company-watchlist scanner — pulls jobs straight from employers' ATS boards.

Greenhouse, Lever and Ashby all expose free, no-auth JSON job-board APIs. Given a
curated watchlist (``config/watchlist.yaml``) this is the highest-signal,
zero-noise, zero-Apify source: full descriptions, always fresh, and only the
companies we actually want. Titles are pre-filtered to AI/ML before emitting so
a 800-role board contributes only its relevant openings.

Endpoints:
  greenhouse -> https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
  lever      -> https://api.lever.co/v0/postings/{token}?mode=json
  ashby      -> https://api.ashbyhq.com/posting-api/job-board/{token}
"""

from __future__ import annotations

import logging
import re
from html import unescape

import requests

from common import config
from processing.models import RawJob
from scrapers.base import BaseScraper

log = logging.getLogger("jobradar")

_UA = "JobRadar/1.0 (+https://github.com/) personal job aggregator"


def _strip_html(text: str) -> str:
    return unescape(re.sub(r"<[^>]+>", " ", text or "")).strip()


class ATSBoardsScraper(BaseScraper):
    name = "Careers"

    def __init__(self):
        self.companies = config.watchlist().get("companies", []) or []
        self.keep_re = re.compile(config.pipelines()["title_keywords_regex"],
                                  re.IGNORECASE)

    @property
    def available(self) -> bool:
        return bool(self.companies)

    def _fetch(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        sess = requests.Session()
        sess.headers.update({"User-Agent": _UA})
        handlers = {"greenhouse": self._greenhouse, "lever": self._lever,
                    "ashby": self._ashby}
        for co in self.companies:
            ats = str(co.get("ats", "")).lower()
            token = co.get("token")
            fn = handlers.get(ats)
            if not (fn and token):
                log.warning("[ATS] bad watchlist entry: %s", co)
                continue
            try:
                found = fn(sess, token, co.get("name", token))
                jobs.extend(found)
            except (requests.RequestException, ValueError, KeyError) as exc:
                log.warning("[ATS] %s (%s) failed: %s", co.get("name"), ats, exc)
        return jobs

    def _keep(self, title: str) -> bool:
        return bool(title and self.keep_re.search(title))

    # ---- per-platform mappers ----

    def _greenhouse(self, sess, token, name) -> list[RawJob]:
        url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
        r = sess.get(url, params={"content": "true"}, timeout=30)
        if r.status_code != 200:
            log.warning("[ATS] greenhouse:%s -> HTTP %s", token, r.status_code)
            return []
        out = []
        for j in r.json().get("jobs", []):
            title = j.get("title", "")
            if not self._keep(title):
                continue
            loc = (j.get("location") or {}).get("name", "")
            out.append(RawJob(
                source=name, title=title, company=name, location=loc,
                description=_strip_html(j.get("content", "")),
                url=j.get("absolute_url", ""),
                posted_at=self._parse_dt(j.get("updated_at")
                                         or j.get("first_published")),
                raw=j))
        return out

    def _lever(self, sess, token, name) -> list[RawJob]:
        url = f"https://api.lever.co/v0/postings/{token}"
        r = sess.get(url, params={"mode": "json"}, timeout=30)
        if r.status_code != 200:
            log.warning("[ATS] lever:%s -> HTTP %s", token, r.status_code)
            return []
        out = []
        for j in r.json():
            title = j.get("text", "")
            if not self._keep(title):
                continue
            cats = j.get("categories") or {}
            desc = j.get("descriptionPlain") or _strip_html(j.get("description", ""))
            out.append(RawJob(
                source=name, title=title, company=name,
                location=cats.get("location", ""),
                description=desc, url=j.get("hostedUrl", ""),
                posted_at=self._parse_dt(j.get("createdAt")), raw=j))
        return out

    def _ashby(self, sess, token, name) -> list[RawJob]:
        url = f"https://api.ashbyhq.com/posting-api/job-board/{token}"
        r = sess.get(url, timeout=30)
        if r.status_code != 200:
            log.warning("[ATS] ashby:%s -> HTTP %s", token, r.status_code)
            return []
        out = []
        for j in r.json().get("jobs", []):
            title = j.get("title", "")
            if not self._keep(title):
                continue
            loc = j.get("location", "")
            if j.get("isRemote") and "remote" not in loc.lower():
                loc = f"Remote ({loc})" if loc else "Remote"
            desc = j.get("descriptionPlain") or _strip_html(j.get("descriptionHtml", ""))
            out.append(RawJob(
                source=name, title=title, company=name, location=loc,
                description=desc, url=j.get("jobUrl") or j.get("applyUrl", ""),
                posted_at=self._parse_dt(j.get("publishedAt")), raw=j))
        return out
