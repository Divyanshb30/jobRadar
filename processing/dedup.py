"""Hash-based deduplication — within the current batch and against the tracker.

The dedup key (company + title + location, normalized) lives on RawJob as
``dedup_hash``. This module removes:
  1. intra-batch duplicates (same posting from two sources), and
  2. anything already written to the Google Sheet on a previous run.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from processing.models import RawJob

log = logging.getLogger("jobradar")


def remove_duplicates(jobs: list[RawJob],
                      existing_hashes: Iterable[str] | None = None) -> list[RawJob]:
    existing = set(existing_hashes or [])
    seen: set[str] = set()
    kept: list[RawJob] = []
    intra, against_tracker = 0, 0

    for j in jobs:
        h = j.dedup_hash
        if h in existing:
            against_tracker += 1
            continue
        if h in seen:
            intra += 1
            continue
        seen.add(h)
        kept.append(j)

    log.info("[dedup] %d -> %d (intra-batch: %d, already-in-tracker: %d)",
             len(jobs), len(kept), intra, against_tracker)
    return kept
