"""Pydantic data models — the contract shared across every layer.

RawJob     -> emitted by scrapers (Layer 1), consumed by pre-filter (Layer 2).
ScoredJob  -> emitted by the LLM scorer (Layer 3), consumed by write/notify.

Keeping these two models small and strict means a broken scraper can never
inject a malformed record deep into the pipeline; validation fails at the edge.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class Pipeline(str, Enum):
    INDIA = "India"
    INTERNATIONAL = "International"


class VisaStatus(str, Enum):
    YES = "YES"
    NO = "NO"
    UNKNOWN = "UNKNOWN"
    NA = "N/A"


class Verdict(str, Enum):
    STRONG = "Strong match"
    REVIEW = "Review"
    DROP = "Drop"


def _norm(value: Optional[str]) -> str:
    """Lowercase + collapse whitespace, for stable hashing/matching."""
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip().lower()


class RawJob(BaseModel):
    """A single listing as pulled from a source, before scoring.

    Every scraper is responsible for mapping its native payload onto these
    fields. `raw` retains the untouched source dict for debugging.
    """

    source: str                      # "Indeed", "LinkedIn", "Reed", ...
    title: str
    company: str = ""
    location: str = ""
    description: str = ""
    url: str = ""

    # Salary — parsed where the source exposes structured numbers.
    salary_raw: str = ""
    salary_min: Optional[float] = None       # annual, source currency
    salary_max: Optional[float] = None
    currency: Optional[str] = None

    posted_at: Optional[datetime] = None     # None when the source omits it
    easy_apply: bool = False

    # Assigned during pre-filter, not by the scraper.
    pipeline: Optional[Pipeline] = None

    raw: dict[str, Any] = Field(default_factory=dict, repr=False)

    @field_validator("title", "company", "location", "description", "url",
                      "salary_raw", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        if v is None:
            return ""
        return str(v).strip()

    @property
    def dedup_hash(self) -> str:
        """Stable identity: company + title + location, normalized.

        Used to dedup within a batch and against the existing tracker so the
        same posting scraped from two sources collapses to one row.
        """
        key = f"{_norm(self.company)}|{_norm(self.title)}|{_norm(self.location)}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]

    @property
    def description_snippet(self) -> str:
        """First chunk of the description — what the scorer actually sees."""
        return _norm(self.description)[:1200]


class ScoredJob(BaseModel):
    """A RawJob enriched with the LLM's judgement and a composite score."""

    raw: RawJob

    experience_fit: int = 0          # 0-100
    years_required: int = 0          # min YoE the job requires (0 = entry/unstated)
    role_fit: int = 0                # 0-100
    tech_stack_match: int = 0        # 0-100
    visa_status: VisaStatus = VisaStatus.NA
    salary_estimate: Optional[str] = None    # e.g. "18-25 LPA", India only
    phd_required: bool = False               # LLM backstop for the PhD gate
    red_flags: Optional[str] = None

    composite_score: int = 0         # 0-100 weighted (incl. priority boost)
    priority_bonus: int = 0          # resume-alignment boost folded into composite
    verdict: Verdict = Verdict.DROP

    @field_validator("years_required", mode="before")
    @classmethod
    def _years(cls, v: Any) -> int:
        try:
            return max(0, min(50, int(round(float(v)))))
        except (TypeError, ValueError):
            return 0

    @field_validator("experience_fit", "role_fit", "tech_stack_match",
                      "composite_score", mode="before")
    @classmethod
    def _clamp(cls, v: Any) -> int:
        try:
            return max(0, min(100, int(round(float(v)))))
        except (TypeError, ValueError):
            return 0

    # Convenience passthroughs so downstream code reads scored.title etc.
    @property
    def title(self) -> str:
        return self.raw.title

    @property
    def company(self) -> str:
        return self.raw.company

    @property
    def location(self) -> str:
        return self.raw.location

    @property
    def url(self) -> str:
        return self.raw.url

    @property
    def source(self) -> str:
        return self.raw.source

    @property
    def pipeline(self) -> Optional[Pipeline]:
        return self.raw.pipeline

    def to_tracker_row(self) -> list[str]:
        """Flatten to the Google Sheets column order (A..Q). See schema §4."""
        pipeline = self.pipeline.value if self.pipeline else ""
        return [
            datetime.utcnow().strftime("%Y-%m-%d"),      # A Date Found
            pipeline,                                     # B Pipeline
            self.source,                                  # C Source
            self.title,                                   # D Job Title
            self.company,                                 # E Company
            self.location,                                # F Location
            str(self.composite_score),                    # G Score
            str(self.experience_fit),                     # H Experience Fit
            str(self.role_fit),                           # I Role Fit
            str(self.tech_stack_match),                   # J Tech Match
            self.visa_status.value,                       # K Visa Status
            self.salary_estimate or self.raw.salary_raw,  # L Salary Range
            self.url,                                      # M Job URL
            "Yes" if self.raw.easy_apply else "No",       # N Easy Apply
            "New",                                         # O Status
            self.red_flags or "",                          # P Notes
            self.raw.dedup_hash,                            # Q Dedup Hash
        ]
