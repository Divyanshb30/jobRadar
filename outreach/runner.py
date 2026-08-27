"""Outreach orchestrator — select jobs, discover contacts, personalise, draft.

Takes the scored jobs from the pipeline, picks the strongest few (one per
company), researches contacts, writes personalised copy, and emits drafts. All
network/LLM work fails soft per item so one bad company can't sink the batch.
"""

from __future__ import annotations

import logging

from common import config
from outreach.discovery import Discovery
from outreach.drafts import DraftWriter
from outreach.models import OutreachItem
from outreach.personalize import Personalizer
from processing.models import ScoredJob

log = logging.getLogger("jobradar")


class OutreachRunner:
    def __init__(self):
        self.cfg = config.outreach()
        self.sel = self.cfg.get("selection", {})
        self.personas = self.cfg.get("personas", [])
        self.n_contacts = int(self.cfg.get("discovery", {}).get(
            "contacts_per_job", 2))

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled"))

    def run(self, scored: list[ScoredJob], gmail: bool = True) -> dict | None:
        if not self.enabled:
            log.info("[outreach] disabled (config/outreach.yaml enabled: false)")
            return None
        items = self._select(scored)
        if not items:
            log.info("[outreach] no jobs met the selection bar (min_score=%s)",
                     self.sel.get("min_score"))
            return None
        log.info("[outreach] researching %d target(s)", len(items))

        discovery = Discovery()
        if not discovery.available:
            log.warning("[outreach] no SERPER_API_KEY — contacts can't be "
                        "discovered; copy will still be drafted.")
        personalizer = Personalizer()
        for item in items:
            try:
                item.contacts = discovery.find_contacts(
                    item.company, self.personas, self.n_contacts)
                personalizer.personalise(item)
                log.info("[outreach] %s: %d contact(s)", item.company,
                         len(item.contacts))
            except Exception as exc:  # noqa: BLE001 - never sink the batch
                item.error = str(exc)
                log.warning("[outreach] %s failed: %s", item.company, exc)

        writer = DraftWriter()
        if not gmail:
            writer.want_gmail = False   # dry-run: files only, no Gmail drafts
        return writer.write(items)

    def _select(self, scored: list[ScoredJob]) -> list[OutreachItem]:
        pipelines = set(self.sel.get("pipelines", ["International", "India"]))
        min_score = int(self.sel.get("min_score", 70))
        max_per = int(self.sel.get("max_per_run", 8))
        require_company = bool(self.sel.get("require_company", True))

        picks: list[OutreachItem] = []
        seen_companies: set[str] = set()
        for sj in sorted(scored, key=lambda s: s.composite_score, reverse=True):
            pl = sj.pipeline.value if sj.pipeline else ""
            if pl not in pipelines or sj.composite_score < min_score:
                continue
            if require_company and not sj.company:
                continue
            key = sj.company.strip().lower()
            if key in seen_companies:
                continue        # one outreach per company per run
            seen_companies.add(key)
            picks.append(OutreachItem(
                company=sj.company, role=sj.title, location=sj.location or "",
                job_url=sj.url, score=sj.composite_score,
                jd_snippet=sj.raw.description[:900]))
            if len(picks) >= max_per:
                break
        return picks
