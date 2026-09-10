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


# --- years-of-experience detection ----------------------------------------
# Pulls the MINIMUM required YoE out of a job's text so roles that demand more
# than the candidate has (0-2 YoE) are dropped deterministically at pre-filter,
# before any LLM cost. The LLM's own years_required gate misses these two common
# cases: (a) no LLM key -> rule-based fallback never sets years_required, and
# (b) the requirement sits past the description snippet the scorer is fed. This
# scans the FULL description, so it catches both.
_YEARS_MENTION = re.compile(
    r"(?P<lo>\d{1,2})\s*"
    r"(?:\+|\s*(?:-|–|—|to)\s*\d{1,2})?\s*"   # 5+, 5-7, 5 to 7
    r"(?:\+\s*)?"
    r"(?:years?|yrs?)\b",
    re.IGNORECASE,
)
# Context that marks a years-mention as a real experience REQUIREMENT.
_EXP_CTX = re.compile(
    r"experien|\bexp\b|professional|indust(?:ry|rial)|relevant|hands[\s-]?on|"
    r"\bof work\b|expertise|seniority|post[\s-]?qualif|in\s+(?:ai|ml|machine|"
    r"deep|data|software|nlp|python|programming|engineering|develop)",
    re.IGNORECASE,
)
_REQ_CTX = re.compile(
    r"requir|minimum|min\.|at least|must have|must possess|need|expect|"
    r"seeking|looking for|should have|proven|demonstrated|with over|with\s+\d",
    re.IGNORECASE,
)
# Phrases that VETO the drop (the years are optional, a ceiling, or company
# boilerplate rather than a per-candidate minimum).
_YEARS_SOFTENER = re.compile(
    r"prefer|plus|nice[\s-]?to[\s-]?have|desirable|desired|\bideal|bonus|"
    r"advantage|would be|good to have|welcome|not required|is a plus",
    re.IGNORECASE,
)
_YEARS_CEILING = re.compile(
    r"(?:up\s?to|upto|less than|under|fewer than|no more than|maximum|max\.|"
    r"at most|within)\s*$",
    re.IGNORECASE,
)
# Company-tenure / aggregate-experience phrasings — NOT a per-candidate minimum.
# Deliberately narrow: "we are looking for N years" is a requirement, so only
# tenure claims ("we have N years", "our team has N years") veto the drop.
_YEARS_BOILERPLATE = re.compile(
    r"we(?:'ve| have| bring)|our (?:team|company|firm|clients?)|"
    r"team (?:has|have|brings|of|with)|combined|"
    r"founded|established|for (?:over|more than)|in business|company (?:with|has)|"
    r"serving|history of|track record",
    re.IGNORECASE,
)
_YEARS_COMBINED = re.compile(r"\bcombined\b", re.IGNORECASE)


def _required_years(text: str, window: int = 45) -> int | None:
    """Highest non-softened minimum-YoE requirement in ``text``, or None.

    For each "N years" mention we take N as the lower bound (of "N+", "N-M",
    "N to M"), confirm it reads as a requirement (an experience/requirement
    phrase nearby, or the compact "N+ years" form), and skip it when a softener,
    ceiling, or company-boilerplate phrase sits alongside it. Returns the max
    surviving lower bound so a "2 yrs Python / 6 yrs ML" ad is judged on the 6.
    """
    best: int | None = None
    for m in _YEARS_MENTION.finditer(text or ""):
        lo = int(m.group("lo"))
        had_plus = "+" in m.group(0)
        pre = text[max(0, m.start() - window): m.start()]
        post = text[m.end(): m.end() + window]
        win = pre + " " + post
        if _YEARS_CEILING.search(pre):          # "up to 5 years"
            continue
        if _YEARS_SOFTENER.search(win):         # "5 years preferred / a plus"
            continue
        if _YEARS_BOILERPLATE.search(pre):      # "we have 10 years ..."
            continue
        if _YEARS_COMBINED.search(post):        # "30+ years combined experience"
            continue
        if not (had_plus or _EXP_CTX.search(win) or _REQ_CTX.search(pre)):
            continue
        if best is None or lo > best:
            best = lo
    return best


# --- US (non-target) geography detection ----------------------------------
# US roles kept leaking into the International tab two ways: the old substring
# geo match ("uk" inside "Milwaukee"), and the remote catch-all routing US
# remote jobs to International. These detect a US location so _classify can drop
# it. Country names are case-insensitive; the short "US"/state codes are
# case-SENSITIVE so the lowercase pronoun "us" ("join us") and words never trip
# it (locations are properly cased). DE (Germany), IN (India), OR ("or") are
# omitted from the state list — they collide with target country codes / words.
_US_PHRASE = re.compile(r"\bunited states\b|\bu\.?s\.?a\.?\b", re.IGNORECASE)
_US_STATES = ("AL|AK|AZ|AR|CA|CO|CT|FL|GA|HI|ID|IA|KS|KY|LA|ME|MD|MA|MI|MN|"
              "MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|PA|RI|SC|SD|TN|TX|UT|"
              "VT|VA|WA|WV|WI|WY")
_US_CODE = re.compile(
    r"\bUS\b|\(US\)|[-–—/]\s*US\b|"
    r"\bUS[\s\-](?:based|only|remote|citizen|work|resident)\b|"
    r",\s*(?:" + _US_STATES + r")\b",
)
# Unambiguous target-country tokens that OVERRIDE a US signal, resolving city
# collisions (Cambridge/Birmingham/Manchester/London/Dublin/Berlin all exist in
# the US too). City-level target tokens intentionally do NOT override US.
_INTL_STRONG_TOKENS = ["uae", "united arab emirates", "abu dhabi", "dubai",
                       "united kingdom", "uk", "germany", "netherlands",
                       "ireland", "europe"]


def _is_us(raw_hay: str) -> bool:
    return bool(_US_PHRASE.search(raw_hay) or _US_CODE.search(raw_hay))


def _geo_regex(geos: list[str]) -> re.Pattern:
    """Word-boundary alternation over geo tokens (so 'uk' no longer matches
    'Milwaukee', 'ie' inside words, etc.)."""
    return re.compile(r"\b(?:" + "|".join(re.escape(g) for g in geos) + r")\b",
                      re.IGNORECASE)


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
        # Word-boundary matchers (substring matching mis-routed US cities).
        self.india_re = _geo_regex(self.india_geos)
        self.intl_re = _geo_regex(self.intl_geos)
        self.intl_strong_re = _geo_regex(_INTL_STRONG_TOKENS)
        self.staleness_hours = int(pl.get("staleness_hours", 48))
        self.min_ctc_lpa = float(pl["pipelines"]["india"].get("min_ctc_lpa", 25))
        # Hard experience gate (0-2 YoE target). 0/None required = kept.
        self.max_years = int(pl.get("experience_gate", {}).get("max_years", 2))

    # ---- public entry point ----

    def run(self, jobs: list[RawJob]) -> list[RawJob]:
        log.info("[prefilter] input: %d", len(jobs))
        jobs = self._title_match(jobs)
        jobs = self._experience_drop(jobs)
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

    def _experience_drop(self, jobs: list[RawJob]) -> list[RawJob]:
        """Hard-drop roles that require MORE than ``max_years`` YoE.

        Deterministic, LLM-free gate over the full title + description. The
        title regex only catches "N+ years" in the title; most YoE requirements
        live in the body ("minimum 5 years of experience"), which this catches.
        Conservative: softened / optional / ceiling / boilerplate mentions are
        left for the LLM, so only a clear over-threshold minimum is dropped.
        """
        if self.max_years is None:
            return jobs
        kept, dropped = [], 0
        for j in jobs:
            yrs = _required_years(f"{j.title}\n{j.description}")
            if yrs is not None and yrs > self.max_years:
                dropped += 1
                continue
            kept.append(j)
        log.info("[prefilter] experience(>%dyr): removed %d",
                 self.max_years, dropped)
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
        """India geos win first; then drop US (out-of-scope); then target
        International geos; then remote -> Intl.

        US roles are dropped even when they carry a target city name that also
        exists in the US (Cambridge MA, Dublin CA, Manchester NH, ...) — a US
        signal only yields to an unambiguous target-COUNTRY token. A remote job
        with a US signal is dropped too, so US-only remote roles stop landing in
        the International tab.
        """
        raw = f"{j.location} {j.title}"
        if not j.location:
            raw += " " + j.description[:200]
        hay = raw.lower()

        if self.india_re.search(hay):
            return Pipeline.INDIA

        is_remote = "remote" in hay or j.source in ("RemoteOK",)
        us = _is_us(raw)
        # US (and not an unambiguous target country) -> out of scope, drop.
        if us and not self.intl_strong_re.search(hay):
            return None
        if self.intl_re.search(hay):
            return Pipeline.INTERNATIONAL
        if is_remote and not us:
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
