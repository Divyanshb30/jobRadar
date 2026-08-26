"""Deterministic visa-sponsorship verification via official sponsor registers.

The UK (Home Office) and Netherlands (IND) both publish free, authoritative lists
of every organisation licensed to sponsor a work visa. Looking a company up in
the register is far more reliable than asking the LLM to *guess* whether a UK/NL
role offers sponsorship — a hit means visa == YES with confidence.

Sources (see ``config/sources.yaml`` -> ``sponsor_registers``):
  * UK: gov.uk "Register of licensed sponsors: workers" — a CSV whose media-id
    changes each update, so we scrape the current ``.csv`` link off the
    publication page. ~143k orgs, column "Organisation Name".
  * NL: IND "Public register recognised sponsors (work)" — a server-rendered
    HTML table, ~13k orgs, column "Organisation".

Everything fails soft: a failed download / parse yields an empty set and logs,
never raising. The registers are cached to disk (daily) so re-runs don't
re-download the ~11 MB UK CSV.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import time
from pathlib import Path

import requests

from common import config

log = logging.getLogger("jobradar")

_UA = "JobRadar/1.0 (+https://github.com/) personal job aggregator"

# Legal-form suffixes / noise stripped during name normalisation.
_SUFFIX_RE = re.compile(
    r"\b(ltd|limited|plc|llp|llc|inc|incorporated|corp|corporation|co|company|"
    r"gmbh|ag|bv|b\.v|nv|n\.v|holding|holdings|group|uk|international|global|"
    r"services|solutions|technologies|technology|labs)\b", re.IGNORECASE)
_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")
_WS_RE = re.compile(r"\s+")

# Location keyword -> register country.
_UK_HINTS = ("united kingdom", "uk", "england", "scotland", "wales",
             "northern ireland", "london", "manchester", "cambridge",
             "edinburgh", "oxford", "bristol", "leeds", "birmingham", "glasgow")
_NL_HINTS = ("netherlands", "holland", "amsterdam", "rotterdam", "the hague",
             "eindhoven", "utrecht", "nl")


def _normalise(name: str) -> str:
    n = (name or "").lower().strip()
    n = _PUNCT_RE.sub(" ", n)
    n = _SUFFIX_RE.sub(" ", n)
    return _WS_RE.sub(" ", n).strip()


def country_of(location: str) -> str | None:
    """Map a free-text location to 'UK', 'NL', or None."""
    hay = (location or "").lower()
    if any(h in hay for h in _NL_HINTS):
        return "NL"
    if any(h in hay for h in _UK_HINTS):
        return "UK"
    return None


class SponsorRegister:
    """Loads + caches the UK/NL registers and answers membership queries."""

    def __init__(self):
        self.cfg = config.sources().get("sponsor_registers", {}) or {}
        self.enabled = bool(self.cfg.get("enabled", False))
        self.cache_dir = Path(self.cfg.get("cache_dir", "output/.sponsor_cache"))
        self.ttl = int(self.cfg.get("cache_ttl_hours", 24)) * 3600
        self._sets: dict[str, set[str]] = {}
        self._loaded = False

    # ---- public API ----

    def is_sponsor(self, company: str, location: str) -> bool:
        """True iff ``company`` is on the register for the job's country."""
        if not self.enabled or not company:
            return False
        country = country_of(location)
        if country is None:
            return False
        self._ensure_loaded()
        names = self._sets.get(country)
        if not names:
            return False
        # Exact match on the normalised name (legal suffixes stripped on both
        # sides), so "Revolut Ltd" and "Revolut" collapse to the same key.
        # Conservative by design — no fuzzy/substring matching, which would risk
        # false positives across 143k entries.
        key = _normalise(company)
        return bool(key) and key in names

    # ---- loading ----

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        if self.cfg.get("uk"):
            self._sets["UK"] = self._load_uk()
        if self.cfg.get("nl"):
            self._sets["NL"] = self._load_nl()
        log.info("[sponsor] loaded registers: UK=%d, NL=%d",
                 len(self._sets.get("UK", ())), len(self._sets.get("NL", ())))

    def _cache_read(self, tag: str) -> str | None:
        path = self.cache_dir / f"{tag}.cache"
        try:
            if path.exists() and (time.time() - path.stat().st_mtime) < self.ttl:
                return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
        return None

    def _cache_write(self, tag: str, text: str) -> None:
        try:
            (self.cache_dir / f"{tag}.cache").write_text(text, encoding="utf-8")
        except OSError:
            pass

    def _load_uk(self) -> set[str]:
        try:
            text = self._cache_read("uk")
            if text is None:
                pub = self.cfg["uk"]["publication_url"]
                page = requests.get(pub, headers={"User-Agent": _UA}, timeout=60)
                m = re.search(r'https://[^\s"\'<>]+\.csv', page.text)
                if not m:
                    log.warning("[sponsor] UK: no CSV link on publication page")
                    return set()
                csv_resp = requests.get(m.group(0), headers={"User-Agent": _UA},
                                        timeout=90)
                csv_resp.raise_for_status()
                text = csv_resp.text
                self._cache_write("uk", text)
            col = self.cfg["uk"].get("name_column", "Organisation Name")
            return self._parse_csv(text, col)
        except (requests.RequestException, ValueError, KeyError) as exc:
            log.warning("[sponsor] UK register load failed: %s", exc)
            return set()

    def _load_nl(self) -> set[str]:
        try:
            text = self._cache_read("nl")
            if text is None:
                resp = requests.get(self.cfg["nl"]["url"],
                                    headers={"User-Agent": _UA}, timeout=60)
                resp.raise_for_status()
                text = resp.text
                self._cache_write("nl", text)
            return self._parse_nl_table(text,
                                        self.cfg["nl"].get("name_column",
                                                           "Organisation"))
        except (requests.RequestException, ValueError, KeyError) as exc:
            log.warning("[sponsor] NL register load failed: %s", exc)
            return set()

    @staticmethod
    def _parse_csv(text: str, name_col: str) -> set[str]:
        reader = csv.reader(io.StringIO(text))
        rows = iter(reader)
        header = next(rows, [])
        try:
            idx = header.index(name_col)
        except ValueError:
            idx = 0
        out = {_normalise(r[idx]) for r in rows if r and len(r) > idx and r[idx].strip()}
        out.discard("")
        return out

    @staticmethod
    def _parse_nl_table(html: str, name_col: str) -> set[str]:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table")
        if not table:
            return set()
        rows = table.find_all("tr")
        if not rows:
            return set()
        headers = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])]
        idx = headers.index(name_col) if name_col in headers else 0
        out = set()
        for tr in rows[1:]:
            cells = tr.find_all(["td", "th"])
            if len(cells) > idx:
                out.add(_normalise(cells[idx].get_text(strip=True)))
        out.discard("")
        return out
