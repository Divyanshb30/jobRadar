"""BaseScraper — the contract every source adapter implements.

A scraper's one job: turn a source's native payload into ``List[RawJob]``.
Two rules make the orchestrator robust:

1. ``scrape()`` never raises. Network hiccups, missing keys, malformed JSON —
   all are caught and logged, and the scraper returns whatever it managed to
   collect (often an empty list). One dead source must not sink the run.
2. ``available`` reports whether required secrets are present, so main.py can
   skip a source cleanly instead of discovering it mid-run.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Optional

from processing.models import RawJob

log = logging.getLogger("jobradar")


class BaseScraper(ABC):
    #: Human-readable source label, written to the tracker's "Source" column.
    name: str = "base"

    @property
    def available(self) -> bool:
        """Whether this scraper has everything it needs to run. Override when
        the source requires secrets; default assumes it does."""
        return True

    @abstractmethod
    def _fetch(self) -> list[RawJob]:
        """Do the real work. May raise — ``scrape`` wraps it."""
        raise NotImplementedError

    def scrape(self) -> list[RawJob]:
        if not self.available:
            log.warning("[%s] skipped — missing credentials/config", self.name)
            return []
        try:
            jobs = self._fetch()
            log.info("[%s] fetched %d raw listings", self.name, len(jobs))
            return jobs
        except Exception as exc:  # noqa: BLE001 - deliberate: never sink the run
            log.error("[%s] failed: %s", self.name, exc, exc_info=True)
            return []

    # ---- shared helpers for subclasses ----

    @staticmethod
    def _parse_dt(value: Any) -> Optional[datetime]:
        """Best-effort ISO/epoch date parsing. Returns None on failure."""
        if value in (None, ""):
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(float(value), tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        s = str(value).strip()
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ",
                    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
                    "%d/%m/%Y", "%m/%d/%Y"):
            try:
                dt = datetime.strptime(s.replace("Z", "+0000")
                                       if fmt.endswith("%z") else s, fmt)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        # ISO with fractional seconds / offset colon.
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
