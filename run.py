"""CLI orchestrator for concurrent Google Maps review scraping.

Coordinates SQLite database loading, Playwright CDP tab pooling, address
resolving, review scraping, CSV streaming export, and batched atomic
checkpointing.
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import hashlib
from pathlib import Path
import random
import sys
from typing import Optional

from tqdm import tqdm

try:
    from loguru import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)

from src.db_loader import CafeRecord, DatabaseLoader
from src.exporter import CSVExporter
from src.resolver import LocationResolver
from src.scraper import GoogleMapsScraper, ScraperWorker, SkipReason
from src.skip_reporter import SkipReporter
from src.state import StateManager


def _configure_logging(level: str = "INFO") -> None:
    """Configure loguru logging with stderr output and rotating file handler.

    Args:
        level: Minimum log level string (e.g. DEBUG, INFO, WARNING, ERROR).
    """
    try:
        from loguru import logger as _loguru_logger

        _loguru_logger.remove()
        fmt = (
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
        )
        _loguru_logger.add(sys.stderr, level=level.upper(), format=fmt)

        Path("logs").mkdir(exist_ok=True)
        file_fmt = (
            "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | "
            "{name}:{line} - {message}"
        )
        _loguru_logger.add(
            "logs/crawler_{time}.log",
            level=level.upper(),
            rotation="10 MB",
            retention="14 days",
            format=file_fmt,
        )
    except ImportError:
        import logging

        logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO))


def shard_of(slug: str, num_shards: int) -> int:
    """Compute the deterministic shard index for a cafe slug using MD5.

    Args:
        slug: Unique cafe identifier slug.
        num_shards: Total number of shards.

    Returns:
        Shard index in the range [0, num_shards - 1].
    """
    if num_shards < 1:
        raise ValueError("num_shards must be at least 1")
    return int(hashlib.md5(slug.encode("utf-8")).hexdigest(), 16) % num_shards


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse command line arguments for the crawler orchestrator.

    Args:
        argv: List of command line arguments (defaults to sys.argv[1:]).

    Returns:
        Parsed argparse.Namespace.
    """
    parser = argparse.ArgumentParser(
        description="Google Maps cafe reviews scraper."
    )
    parser.add_argument(
        "--db", default="cafes_cafe.db", help="SQLite database path"
    )
    parser.add_argument(
        "--output",
        default="data/google_reviews.csv",
        help="Destination CSV path",
    )
    parser.add_argument(
        "--state",
        default="crawler_state.json",
        help="Checkpoint state JSON path",
    )
    parser.add_argument(
        "--skipped",
        default="data/skipped_cafes.csv",
        help="Skipped cafes CSV path",
    )
    parser.add_argument(
        "--skipped-input",
        default=None,
        help="Input CSV path for --retry-skipped mode",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Max cafes to process"
    )
    parser.add_argument(
        "--workers", type=int, default=4, help="Concurrent workers count"
    )
    parser.add_argument(
        "--cdp-url",
        default="http://localhost:9222",
        help="Chrome CDP debugging endpoint",
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Use headful browser if CDP fails",
    )
    parser.add_argument(
        "--retry-skipped",
        action="store_true",
        help="Only retry cafes present in --skipped-input",
    )
    parser.add_argument(
        "--max-cafes-per-tab",
        type=int,
        default=10,
        help="Rotate browser tab after N cafes",
    )
    parser.add_argument(
        "--shard-id",
        type=int,
        default=0,
        help="Zero-based index of this shard",
    )
    parser.add_argument(
        "--num-shards",
        type=int,
        default=1,
        help="Total number of shards for distributed crawling",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity level",
    )

    args = parser.parse_args(argv)
    if args.workers < 1:
        raise ValueError("--workers must be at least 1")
    if args.num_shards < 1:
        raise ValueError("--num-shards must be at least 1")
    if args.shard_id < 0 or args.shard_id >= args.num_shards:
        raise ValueError(
            f"--shard-id must be between 0 and {args.num_shards - 1} "
            f"(got {args.shard_id})"
        )
    return args


class CrawlerPipeline:
    """Orchestrates concurrent cafe scraping, exporting, and checkpointing."""

    def __init__(
        self,
        loader: DatabaseLoader,
        scraper: GoogleMapsScraper,
        exporter: CSVExporter,
        state: StateManager,
        skip_reporter: SkipReporter,
        resolver: Optional[LocationResolver] = None,
        workers: int = 4,
        retry_skipped: bool = False,
        retry_slugs: Optional[set[str]] = None,
        max_cafes_per_tab: int = 10,
        shard_id: int = 0,
        num_shards: int = 1,
        rate_limit_pause: Optional[asyncio.Event] = None,
    ) -> None:
        """Initialize the crawler pipeline with dependencies.

        Args:
            loader: DatabaseLoader for retrieving cafe records.
            scraper: GoogleMapsScraper providing browser context and tab pool.
            exporter: CSVExporter for persisting reviews.
            state: StateManager for checkpointing completed slugs.
            skip_reporter: SkipReporter for un-scraped cafe logging.
            resolver: Optional LocationResolver instance.
            workers: Concurrency worker count.
            retry_skipped: Whether to operate in retry-skipped mode.
            retry_slugs: Slugs to retry if in retry_skipped mode.
            max_cafes_per_tab: Max cafes to process before rotating tab.
            shard_id: Shard index (0-based) for distributed crawling.
            num_shards: Total number of shards (1 = disabled).
            rate_limit_pause: Shared Event to pause workers during rate limiting.
        """
        self.loader = loader
        self.scraper = scraper
        self.exporter = exporter
        self.state = state
        self.skip_reporter = skip_reporter
        self.resolver = resolver or LocationResolver()
        self.workers = max(1, workers)
        self.retry_skipped = retry_skipped
        self.retry_slugs = retry_slugs or set()
        self.max_cafes_per_tab = max(1, max_cafes_per_tab)
        self.shard_id = shard_id
        self.num_shards = num_shards
        self.rate_limit_pause = (
            rate_limit_pause
            if rate_limit_pause is not None
            else asyncio.Event()
        )

    async def _process_one(
        self, worker: ScraperWorker, cafe: CafeRecord
    ) -> None:
        """Process a single cafe: resolve, scrape, and export or record skip.

        Args:
            worker: Assigned ScraperWorker.
            cafe: CafeRecord to process.
        """
        await asyncio.sleep(random.uniform(1.0, 3.0))
        target = self.resolver.resolve(cafe)
        reviews = await worker.scrape(target)

        if len(reviews) >= 1:
            # Mark FIRST so a crash won't re-process this cafe
            await self.state.mark_processed(cafe.slug)
            await self.exporter.append_many(reviews)
            logger.info(
                f"Cafe '{cafe.slug}': scraped and exported {len(reviews)} reviews."
            )
        else:
            reason = worker.last_skip_reason or SkipReason.NO_REVIEWS
            await self.skip_reporter.record(cafe, reason)
            logger.warning(
                f"Cafe '{cafe.slug}': 0 reviews obtained (reason: {reason})."
            )

    async def run(self, limit: Optional[int] = None) -> None:
        """Execute concurrent crawling across eligible cafe records.

        Args:
            limit: Optional maximum number of cafes to process.
        """
        all_cafes = list(self.loader.iter_cafes(approved_only=True))

        if self.retry_skipped:
            pending = [c for c in all_cafes if c.slug in self.retry_slugs]
            logger.info(
                f"Retry mode: {len(pending)} cafes match retry list."
            )
        else:
            pending = [
                c for c in all_cafes if not self.state.is_processed(c.slug)
            ]
            logger.info(
                f"Standard mode: {len(pending)} pending cafes out of {len(all_cafes)}."
            )

        if self.num_shards > 1:
            pending = [
                c
                for c in pending
                if shard_of(c.slug, self.num_shards) == self.shard_id
            ]
            logger.info(
                f"Shard {self.shard_id}/{self.num_shards}: "
                f"{len(pending)} cafes assigned."
            )
            if len(pending) == 0:
                logger.error(
                    f"ERROR: Shard {self.shard_id}/{self.num_shards} assigned 0 cafes. "
                    f"Possible causes:"
                    f"\n  1. The database file does not match the shard you passed."
                    f"\n  2. All cafes in this shard were already processed."
                )
                raise SystemExit(1)

        if limit is not None and limit > 0:
            pending = pending[:limit]
            logger.info(f"Applied limit: processing {len(pending)} cafes.")

        if not pending:
            logger.info("No pending cafes to scrape.")
            return

        queue: asyncio.Queue[Optional[CafeRecord]] = asyncio.Queue()
        for cafe in pending:
            await queue.put(cafe)

        # Enqueue stop sentinels
        for _ in range(self.workers):
            await queue.put(None)

        pbar = tqdm(total=len(pending), desc="Scraping Cafes", unit="cafe")

        async def worker_loop(worker_id: int) -> None:
            worker = ScraperWorker(
                context=self.scraper.context,
                worker_id=worker_id,
                page_pool=self.scraper.page_pool,
                max_cafes_per_tab=self.max_cafes_per_tab,
                rate_limit_pause=self.rate_limit_pause,
            )
            try:
                while True:
                    cafe = await queue.get()
                    if cafe is None:
                        queue.task_done()
                        break
                    try:
                        await self._process_one(worker, cafe)
                    finally:
                        queue.task_done()
                        pbar.update(1)
            finally:
                await worker.close()

        tasks = [
            asyncio.create_task(worker_loop(i + 1)) for i in range(self.workers)
        ]

        try:
            await asyncio.gather(*tasks)
        except (asyncio.CancelledError, KeyboardInterrupt):
            logger.warning("Pipeline interrupted. Cancelling active tasks...")
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            pbar.close()
            await self.state.flush()
            logger.info(
                f"State saved. Completed cafes: {len(self.state.processed)}"
            )


async def _main_async(args: argparse.Namespace) -> int:
    """Run the asynchronous scraper CLI orchestration pipeline.

    Args:
        args: Parsed command line arguments.

    Returns:
        Process exit code (0 on success, non-zero on failure).
    """
    if args.workers < 1:
        logger.error("--workers must be at least 1")
        return 1

    _configure_logging(args.log_level)

    retry_slugs: Optional[set[str]] = None
    if args.retry_skipped:
        if not args.skipped_input:
            logger.error("--retry-skipped flag requires --skipped-input <path>")
            return 1
        input_reporter = SkipReporter(args.skipped_input)
        retry_slugs = input_reporter.load_skipped_slugs()
        logger.info(
            f"Loaded {len(retry_slugs)} slugs to retry from {args.skipped_input}"
        )

    state = StateManager(args.state)
    skip_reporter = SkipReporter(args.skipped)
    exporter = CSVExporter(args.output)
    resolver = LocationResolver()

    def _emergency_flush() -> None:
        try:
            import json
            import os

            tmp = state.state_path.with_suffix(".tmp")
            payload = {"processed_slugs": sorted(state.processed)}
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, state.state_path)
        except Exception:
            pass

    atexit.register(_emergency_flush)

    try:
        with DatabaseLoader(args.db) as loader:
            async with GoogleMapsScraper(
                headless=not args.headful,
                cdp_url=args.cdp_url,
            ) as scraper:
                pipeline = CrawlerPipeline(
                    loader=loader,
                    scraper=scraper,
                    exporter=exporter,
                    state=state,
                    skip_reporter=skip_reporter,
                    resolver=resolver,
                    workers=args.workers,
                    retry_skipped=args.retry_skipped,
                    retry_slugs=retry_slugs,
                    max_cafes_per_tab=args.max_cafes_per_tab,
                    shard_id=args.shard_id,
                    num_shards=args.num_shards,
                )
                await pipeline.run(limit=args.limit)
    except KeyboardInterrupt:
        logger.warning("Interrupted — state preserved")
        await state.flush()
    except Exception as err:
        logger.error(f"Fatal error during crawler execution: {err}")
        await state.flush()
        return 1
    finally:
        await state.flush()

    return 0


def main() -> None:
    """CLI orchestrator entrypoint."""
    try:
        args = parse_args()
    except ValueError as err:
        logger.error(f"Invalid CLI arguments: {err}")
        sys.exit(2)
    exit_code = asyncio.run(_main_async(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
