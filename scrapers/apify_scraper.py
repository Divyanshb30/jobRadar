"""Generic Apify actor runner — Indeed, LinkedIn, Naukri, Bayt, Glassdoor.

Each actor has its OWN input schema (verified against the live Apify schemas),
so a single generic input does not work — Bayt even sets
``additionalProperties: false`` and rejects unknown keys. This module therefore
builds a correct, per-actor input dict, and runs one actor run per
(search title x location/country) combination.

Cost/coverage knobs live in ``config/sources.yaml``:
  * ``apify.queries_per_run``  — how many search titles per actor+pipeline
  * ``apify.actors.<name>.max_items`` — result cap per run

Output field names still vary per actor, so ``_map_item`` stays tolerant.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Optional

from common import config
from processing.models import RawJob
from scrapers.base import BaseScraper

log = logging.getLogger("jobradar")

try:
    from apify_client import ApifyClient
except Exception:  # pragma: no cover
    ApifyClient = None  # type: ignore

# Locations / countries per pipeline, per actor convention.
# UAE ('ae') leads the international lists — it is the top-priority geography.
# Singapore ('sg') intentionally removed from scope.
INDEED_COUNTRIES = {"india": ["in"],
                    "international": ["ae", "uk", "de", "nl", "ie"]}  # ISO-ish
LINKEDIN_LOCATIONS = {"india": ["India"],
                      "international": ["United Arab Emirates", "United Kingdom",
                                        "Germany", "Netherlands", "Ireland"]}
GLASSDOOR_LOCATIONS = LINKEDIN_LOCATIONS
BAYT_COUNTRIES = {"international": ["United Arab Emirates"]}          # full names


def _first(d: dict[str, Any], *keys: str, default: Any = "") -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _loc_str(value: Any) -> str:
    """Normalize a location that may arrive as a plain string or a dict.

    Different actors return different shapes:
      * LinkedIn/Glassdoor: {"name": "London, England", ...}
      * Indeed geo object:   {"city": "", "admin1Code": "KA",
                              "countryName": "India", ...}
    We prefer a ready-made display name; otherwise assemble city + country.
    """
    if not isinstance(value, dict):
        return str(value or "")
    for k in ("displayName", "name", "formattedLocation", "label", "text"):
        if value.get(k):
            return str(value[k])
    parts = [value.get("city"),
             value.get("countryName") or value.get("country")]
    parts = [str(p) for p in parts if p]
    if parts:
        return ", ".join(parts)
    # Last resort: any country hint, never the raw dict repr.
    return str(value.get("countryName") or value.get("countryCode") or "")


def _map_item(item: dict[str, Any], source_name: str) -> Optional[RawJob]:
    """Map one dataset item onto RawJob, tolerant of field-name variation."""
    title = _first(item, "title", "jobTitle", "positionName", "position", "name")
    if not title:
        return None

    company = _first(item, "company", "companyName", "employer", "company_name",
                     "organization")
    if isinstance(company, dict):
        company = _first(company, "name", "displayName", "title",
                         default=str(company))
    location = _loc_str(_first(item, "location", "jobLocation", "place", "city",
                               "formattedLocation"))
    description = _first(item, "description", "descriptionText", "jobDescription",
                         "descriptionHtml", "snippet", "jobDescriptionText")
    url = _first(item, "url", "jobUrl", "link", "applyUrl", "externalApplyLink",
                 "detailsUrl", "jobUrl")
    salary = _first(item, "salary", "salaryText", "salaryInfo", "compensation",
                    "salaryRange")
    if isinstance(salary, dict):
        salary = _first(salary, "text", "raw", default=str(salary))
    posted = _first(item, "postedAt", "postingDateParsed", "date", "postedDate",
                    "publishedAt", "datePosted", "postedTime", default=None)

    return RawJob(
        source=source_name,
        title=str(title),
        company=str(company),
        location=str(location),
        description=str(description),
        url=str(url),
        salary_raw=str(salary),
        posted_at=BaseScraper._parse_dt(posted),
        easy_apply=bool(_first(item, "easyApply", "isEasyApply", default=False)),
        raw=item,
    )


class ApifyScraper(BaseScraper):
    """One instance = one actor for one pipeline (e.g. Indeed / international)."""

    def __init__(self, actor_key: str, pipeline: str):
        self.actor_key = actor_key
        self.pipeline = pipeline
        self.cfg = config.sources()["apify"]
        self.actor_cfg = self.cfg["actors"][actor_key]
        self.token = config.env(self.cfg.get("token_env"))
        self.name = actor_key.capitalize()

    @property
    def available(self) -> bool:
        if ApifyClient is None:
            log.warning("apify-client not installed; skipping %s", self.actor_key)
            return False
        if not self.token:
            return False
        return self.pipeline in self.actor_cfg.get("pipelines", [])

    def _queries(self) -> list[str]:
        n = int(self.cfg.get("queries_per_run", 2))
        return config.pipelines()["search_keywords"][:n]

    @property
    def _limit(self) -> int:
        return int(self.actor_cfg.get("max_items", 40))

    # --- per-actor input assembly (one dict per actor run) ---

    def _run_specs(self) -> list[dict[str, Any]]:
        q = self._queries()
        lim = self._limit
        specs: list[dict[str, Any]] = []

        if self.actor_key == "indeed":
            for country in INDEED_COUNTRIES[self.pipeline]:
                for title in q:
                    specs.append({"country": country, "title": title,
                                  "location": "", "limit": lim,
                                  "datePosted": "3"})   # last 3 days

        elif self.actor_key == "linkedin":
            for loc in LINKEDIN_LOCATIONS[self.pipeline]:
                for title in q:
                    specs.append({"title": title, "location": loc,
                                  "datePosted": "r604800",   # last 7 days
                                  "limit": lim,
                                  "experienceLevel": ["1", "2", "3"]})

        elif self.actor_key == "glassdoor":
            for loc in GLASSDOOR_LOCATIONS[self.pipeline]:
                for title in q:
                    specs.append({"keywords": title, "location": loc,
                                  "daysOld": 3, "limit": lim})

        elif self.actor_key == "naukri":
            for title in q:
                specs.append({"keywords": title, "location": "India",
                              "experience": 1, "jobAge": "3", "limit": lim})

        elif self.actor_key == "bayt":
            for country in BAYT_COUNTRIES[self.pipeline]:
                for title in q:
                    specs.append({"max_results": lim, "keyword": title,
                                  "country": country, "job_type": "all"})

        elif self.actor_key == "wellfound":
            # blackfalcondata/wellfound-scraper: full descriptions inline,
            # experienceLevel="entry" (0-2 YoE) filters seniors at the source.
            # India -> India-located; International -> remote startup roles with
            # visaSponsorship=false (drops explicit no-sponsorship, keeps
            # YES + UNKNOWN — matching our visa policy).
            for title in q:
                # NB: no jobType filter — the actor's "fulltime" substring match
                # drops Wellfound's "Full-time" values and returns nothing.
                spec: dict[str, Any] = {"query": title, "experienceLevel": "entry",
                                        "maxResults": lim,
                                        "descriptionMaxLength": 1200}
                if self.pipeline == "india":
                    spec["location"] = ["India"]
                else:
                    spec["remote"] = True
                    spec["visaSponsorship"] = False
                specs.append(spec)

        else:
            log.warning("[apify] no input builder for actor '%s'", self.actor_key)
        return specs

    def _fetch(self) -> list[RawJob]:
        client = ApifyClient(self.token)
        actor_id = self.actor_cfg["id"]
        timeout = int(self.cfg.get("timeout_secs", 240))
        specs = self._run_specs()
        log.info("[%s/%s] %d actor run(s) on %s",
                 self.actor_key, self.pipeline, len(specs), actor_id)

        jobs: list[RawJob] = []
        for spec in specs:
            try:
                run = client.actor(actor_id).call(
                    run_input=spec,
                    run_timeout=timedelta(seconds=timeout),
                    max_items=self._limit,
                )
            except Exception as exc:  # noqa: BLE001 - one bad run must not kill others
                log.warning("[%s] run failed (input=%s): %s",
                            self.actor_key, spec, exc)
                continue
            # apify-client 3.x returns a pydantic Run object, not a dict.
            dataset_id = getattr(run, "default_dataset_id", None)
            if not dataset_id:
                continue
            for item in client.dataset(dataset_id).iterate_items():
                mapped = _map_item(item, self.name)
                if mapped:
                    jobs.append(mapped)
        return jobs
