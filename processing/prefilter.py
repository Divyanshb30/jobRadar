"""Layer 2 — pre-filter. Hard rules, zero LLM, zero tokens.

Drops obvious junk fast so the scoring layer only pays for plausible listings.
Order matters: cheap title/geo checks first, visa hard-drop and salary floor
next, staleness last. Every step logs how many it removed so tuning the regexes
is a matter of reading one run's logs.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from common import config
from processing.models import Pipeline, RawJob

log = logging.getLogger("jobradar")


class PreFilter:
    def __init__(self):
        pl = config.pipelines()
        self.keep_re = re.compile(pl["title_keywords_regex"], re.IGNORECASE)
        self.excl_re = re.compile(pl["title_exclusion_regex"], re.IGNORECASE)
        self.junk_res = [re.compile(p, re.IGNORECASE)
                         for p in pl.get("junk_title_patterns", [])]
        self.phd_res = [re.compile(p, re.IGNORECASE)
                        for p in pl.get("phd_required_patterns", [])]
        _soft = pl.get("phd_softener_regex")
        self.phd_softener_re = re.compile(_soft, re.IGNORECASE) if _soft else None
        self.visa_no_res = [re.compile(p, re.IGNORECASE)
                            for p in pl.get("visa_no_patterns", [])]
        self.india_geos = [g.lower() for g in pl["pipelines"]["india"]["geographies"]]
        self.intl_geos = [g.lower() for g in pl["pipelines"]["international"]["geographies"]]
        self.staleness_hours = int(pl.get("staleness_hours", 48))
        self.min_ctc_lpa = float(pl["pipelines"]["india"].get("min_ctc_lpa", 25))

    # ---- public entry point ----

    def run(self, jobs: list[RawJob]) -> list[RawJob]:
        log.info("[prefilter] input: %d", len(jobs))
        jobs = self._title_match(jobs)
        jobs = self._phd_drop(jobs)
        jobs = self._route_locations(jobs)
        jobs = self._visa_hard_drop(jobs)
        jobs = self._salary_floor(jobs)
        jobs = self._staleness(jobs)
        log.info("[prefilter] output: %d", len(jobs))
        return jobs

    # ---- individual rules ----

    def _title_match(self, jobs: list[RawJob]) -> list[RawJob]:
        kept = []
        for j in jobs:
            t = j.title
            if not self.keep_re.search(t) or self.excl_re.search(t):
                continue
            if any(rx.search(t) for rx in self.junk_res):
                continue   # aggregator / search-index page, not a real listing
            kept.append(j)
        log.info("[prefilter] title_match: %d -> %d", len(jobs), len(kept))
        return kept

    def _phd_drop(self, jobs: list[RawJob]) -> list[RawJob]:
        """Hard-drop roles that mandate a PhD/doctorate (title OR description).

        Conservative by design — only explicit requirements match, so
        "PhD preferred / a plus / or equivalent" survive to the LLM, which makes
        the nuanced call via its ``phd_required`` field.
        """
        if not self.phd_res:
            return jobs
        kept, dropped = [], 0
        for j in jobs:
            text = f"{j.title}\n{j.description}"
            if self._mandates_phd(text):
                dropped += 1
                continue
            kept.append(j)
        log.info("[prefilter] phd_required: removed %d", dropped)
        return kept

    def _mandates_phd(self, text: str) -> bool:
        """True only if a PhD is *mandatory*. A softener phrase within ~80 chars
        of the match (e.g. 'preferred', 'or equivalent', 'or Master's') vetoes
        the drop, so optional-PhD roles survive to the LLM."""
        for rx in self.phd_res:
            m = rx.search(text)
            if not m:
                continue
            if self.phd_softener_re is None:
                return True
            window = text[max(0, m.start() - 80): m.end() + 80]
            if not self.phd_softener_re.search(window):
                return True
        return False

    def _route_locations(self, jobs: list[RawJob]) -> list[RawJob]:
        kept = []
        for j in jobs:
            pipeline = self._classify(j)
            if pipeline is None:
                continue
            j.pipeline = pipeline
            kept.append(j)
        log.info("[prefilter] location_route: %d -> %d", len(jobs), len(kept))
        return kept

    def _classify(self, j: RawJob) -> Pipeline | None:
        """India geos win first; then International; then remote -> Intl."""
        hay = f"{j.location} {j.title}".lower()
        if not j.location:
            hay += " " + j.description[:200].lower()
        if any(g in hay for g in self.india_geos):
            return Pipeline.INDIA
        if any(g in hay for g in self.intl_geos):
            return Pipeline.INTERNATIONAL
        if "remote" in hay or j.source in ("RemoteOK",):
            return Pipeline.INTERNATIONAL       # priority pipeline
        return None                              # geography mismatch -> drop

    def _visa_hard_drop(self, jobs: list[RawJob]) -> list[RawJob]:
        kept, dropped = [], 0
        for j in jobs:
            if j.pipeline == Pipeline.INTERNATIONAL:
                text = f"{j.title} {j.description}"
                if any(rx.search(text) for rx in self.visa_no_res):
                    dropped += 1
                    continue
            kept.append(j)
        log.info("[prefilter] visa_hard_drop: removed %d", dropped)
        return kept

    def _salary_floor(self, jobs: list[RawJob]) -> list[RawJob]:
        """India only: drop when a disclosed salary is clearly below floor.

        No salary disclosed -> keep (the LLM estimates in Layer 3). Only a
        confidently-parsed INR figure under 25 LPA is dropped here.
        """
        floor_inr = self.min_ctc_lpa * 100_000
        kept, dropped = [], 0
        for j in jobs:
            if j.pipeline == Pipeline.INDIA:
                top = j.salary_max or j.salary_min
                is_inr = (j.currency or "").upper() == "INR"
                if top and is_inr and top < floor_inr:
                    dropped += 1
                    continue
            kept.append(j)
        log.info("[prefilter] salary_floor(India): removed %d", dropped)
        return kept

    def _staleness(self, jobs: list[RawJob]) -> list[RawJob]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.staleness_hours)
        kept, dropped = [], 0
        for j in jobs:
            if j.posted_at is not None and j.posted_at < cutoff:
                dropped += 1
                continue
            kept.append(j)
        log.info("[prefilter] staleness(>%dh): removed %d",
                 self.staleness_hours, dropped)
        return kept
