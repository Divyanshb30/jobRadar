"""JobRadar orchestrator — scrape -> pre-filter -> dedup -> score -> write -> email.

Run daily by GitHub Actions (see .github/workflows/daily_scan.yml) or manually:

    python main.py                 # full run
    python main.py --dry-run       # scrape+filter+score, print, no write/email
    python main.py --no-email      # write to Sheet but don't send the digest
    python main.py --limit 40      # cap listings into the scorer (cheap testing)

Every phase is defensive: a dead source, a failed API, or missing credentials
degrades the run rather than crashing it. Exit code is non-zero only if nothing
at all could be produced, so CI surfaces a truly broken pipeline.
"""

from __future__ import annotations

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from common import logging_setup
from output.digest_builder import build_html
from output.gmail_sender import GmailSender
from output.sheets_writer import SheetsWriter
from processing.dedup import remove_duplicates
from processing.models import Pipeline, RawJob, ScoredJob
from processing.prefilter import PreFilter
from processing.scorer import Scorer
from scrapers.adzuna import AdzunaScraper
from scrapers.apify_scraper import ApifyScraper
from scrapers.base import BaseScraper
from scrapers.gmail_alerts import GmailAlertsScraper
from scrapers.remoteok import RemoteOKScraper
from scrapers.reed import ReedScraper
from scrapers.serper_google_jobs import SerperGoogleJobsScraper

log = logging.getLogger("jobradar")


def build_scrapers() -> list[BaseScraper]:
    """Assemble every source. Apify actors are split per (actor, pipeline)."""
    scrapers: list[BaseScraper] = [
        ApifyScraper("indeed", "india"),
        ApifyScraper("indeed", "international"),
        ApifyScraper("linkedin", "india"),
        ApifyScraper("linkedin", "international"),
        ApifyScraper("naukri", "india"),
        ApifyScraper("bayt", "international"),
        ApifyScraper("glassdoor", "international"),
        AdzunaScraper(),
        ReedScraper(),
        RemoteOKScraper(),
        SerperGoogleJobsScraper(),
        GmailAlertsScraper(),
    ]
    return scrapers


def scrape_all(scrapers: list[BaseScraper], workers: int = 6) -> list[RawJob]:
    """Run scrapers concurrently — most of the wall-clock time is network I/O."""
    active = [s for s in scrapers if s.available]
    skipped = [s.name for s in scrapers if not s.available]
    if skipped:
        log.info("Skipping unavailable sources: %s", ", ".join(sorted(set(skipped))))

    all_jobs: list[RawJob] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(s.scrape): s for s in active}
        for fut in as_completed(futures):
            all_jobs.extend(fut.result())
    log.info("SCRAPE complete: %d raw listings from %d active sources",
             len(all_jobs), len(active))
    return all_jobs


def run(args: argparse.Namespace) -> int:
    logging_setup.setup()
    log.info("=== JobRadar run starting ===")

    # 1. SCRAPE
    raw_jobs = scrape_all(build_scrapers())
    if not raw_jobs:
        log.error("No raw jobs scraped — every source failed or is unconfigured.")
        return 1

    # 2. PRE-FILTER
    filtered = PreFilter().run(raw_jobs)

    # 3. DEDUP (against the existing tracker where possible)
    writer = SheetsWriter()
    existing_hashes: set[str] = set()
    if writer.available and not args.no_sheets:
        try:
            existing_hashes = writer.read_existing_hashes()
            log.info("Loaded %d existing tracker hashes for dedup",
                     len(existing_hashes))
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not read tracker for dedup: %s", exc)
    filtered = remove_duplicates(filtered, existing_hashes)

    if args.limit:
        filtered = filtered[:args.limit]
        log.info("Limited to %d listings for scoring (--limit)", len(filtered))

    # 4. SCORE
    scored: list[ScoredJob] = Scorer().score(filtered)
    _log_summary(scored)

    if args.dry_run:
        log.info("--dry-run: skipping write + email")
        _print_top(scored)
        return 0

    # 5. WRITE
    sheet_url = ""
    if args.no_sheets:
        log.info("--no-sheets: skipping Google Sheets write")
    elif not writer.available:
        log.warning("Google Sheets not configured; skipping write")
    else:
        try:
            writer.bulk_append(scored)
            sheet_url = writer.sheet_url()
        except Exception as exc:  # noqa: BLE001
            log.error("Sheets write failed: %s", exc, exc_info=True)

    # 6. NOTIFY
    if args.no_email:
        log.info("--no-email: skipping digest send")
    else:
        subject, html = build_html(scored, sheet_url)
        GmailSender().send(subject, html)

    log.info("=== JobRadar run complete: %d jobs delivered ===", len(scored))
    return 0


def _log_summary(scored: list[ScoredJob]) -> None:
    intl = sum(1 for j in scored if j.pipeline == Pipeline.INTERNATIONAL)
    india = sum(1 for j in scored if j.pipeline == Pipeline.INDIA)
    strong = sum(1 for j in scored if j.composite_score >= 60)
    log.info("SCORED: %d total (%d international, %d India), %d strong (60+)",
             len(scored), intl, india, strong)


def _print_top(scored: list[ScoredJob], n: int = 15) -> None:
    print("\n--- Top scored jobs (dry run) ---")
    for j in sorted(scored, key=lambda s: s.composite_score, reverse=True)[:n]:
        pl = j.pipeline.value if j.pipeline else "?"
        print(f"[{j.composite_score:3d}] ({pl:13s}) {j.title[:45]:45s} "
              f"| {j.company[:20]:20s} | visa={j.visa_status.value}")
    print("---------------------------------\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="JobRadar daily scan")
    parser.add_argument("--dry-run", action="store_true",
                        help="scrape/filter/score only; no write or email")
    parser.add_argument("--no-email", action="store_true")
    parser.add_argument("--no-sheets", action="store_true")
    parser.add_argument("--limit", type=int, default=0,
                        help="cap listings sent to the scorer (testing)")
    args = parser.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
