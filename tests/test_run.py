"""Tests for CLI runner and crawler pipeline orchestration."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from run import CrawlerPipeline, parse_args
from src.db_loader import CafeRecord
from src.exporter import ReviewRecord
from src.scraper import ScraperWorker, SkipReason


@pytest.fixture
def sample_cafes() -> list[CafeRecord]:
    """Fixture providing a list of 3 sample cafe records."""
    return [
        CafeRecord(
            slug=f"cafe-{i}",
            name=f"Cafe {i}",
            address=f"Address {i}",
            city="Tehran",
            neighborhood="Center",
            latitude=35.7000 + i * 0.01,
            longitude=51.4000 + i * 0.01,
            phone=None,
            id=i,
        )
        for i in range(1, 4)
    ]


@pytest.fixture
def sample_review() -> ReviewRecord:
    """Fixture providing a sample ReviewRecord."""
    return ReviewRecord(
        cafe_slug="cafe-1",
        cafe_name="Cafe 1",
        city="Tehran",
        author="User",
        rating="5 stars",
        date="yesterday",
        text="Great",
    )


def test_parse_args_defaults() -> None:
    """Verify CLI argument parser defaults."""
    args = parse_args([])
    assert args.db == "cafes_cafe.db"
    assert args.output == "data/google_reviews.csv"
    assert args.state == "crawler_state.json"
    assert args.skipped == "data/skipped_cafes.csv"
    assert args.skipped_input is None
    assert args.limit is None
    assert args.workers == 4
    assert args.cdp_url == "http://localhost:9222"
    assert args.headful is False
    assert args.retry_skipped is False
    assert args.max_cafes_per_tab == 10
    assert args.log_level == "INFO"

    custom_args = parse_args(["--max-cafes-per-tab", "5"])
    assert custom_args.max_cafes_per_tab == 5


def test_parse_args_invalid_workers() -> None:
    """Verify that --workers 0 raises an error."""
    with pytest.raises((ValueError, SystemExit)):
        parse_args(["--workers", "0"])


@pytest.mark.asyncio
async def test_pipeline_filters_processed(
    sample_cafes: list[CafeRecord], sample_review: ReviewRecord
) -> None:
    """Verify only unprocessed cafes are queued and scraped."""
    loader = MagicMock()
    loader.iter_cafes = MagicMock(return_value=iter(sample_cafes))

    state = MagicMock()
    # cafe-1 is already processed, cafe-2 and cafe-3 are not
    state.is_processed = MagicMock(
        side_effect=lambda slug: slug == "cafe-1"
    )
    state.mark_processed = AsyncMock()
    state.flush = AsyncMock()
    state.processed = {"cafe-1"}

    exporter = MagicMock()
    exporter.append_many = AsyncMock(return_value=1)

    skip_reporter = MagicMock()
    skip_reporter.record = AsyncMock()

    scraper = MagicMock()
    scraper.context = MagicMock()
    scraper.page_pool = asyncio.Queue()

    pipeline = CrawlerPipeline(
        loader=loader,
        scraper=scraper,
        exporter=exporter,
        state=state,
        skip_reporter=skip_reporter,
        workers=2,
    )

    processed_slugs: list[str] = []

    async def mock_process_one(worker: MagicMock, cafe: CafeRecord) -> None:
        processed_slugs.append(cafe.slug)

    pipeline._process_one = mock_process_one  # type: ignore[assignment]
    await pipeline.run()

    assert "cafe-1" not in processed_slugs
    assert "cafe-2" in processed_slugs
    assert "cafe-3" in processed_slugs


@pytest.mark.asyncio
async def test_pipeline_retry_skipped_mode(
    sample_cafes: list[CafeRecord],
) -> None:
    """Verify retry-skipped mode queues only the specified slugs."""
    loader = MagicMock()
    loader.iter_cafes = MagicMock(return_value=iter(sample_cafes))

    state = MagicMock()
    state.is_processed = MagicMock(return_value=False)
    state.flush = AsyncMock()
    state.processed = set()

    scraper = MagicMock()
    scraper.context = MagicMock()
    scraper.page_pool = asyncio.Queue()

    pipeline = CrawlerPipeline(
        loader=loader,
        scraper=scraper,
        exporter=MagicMock(),
        state=state,
        skip_reporter=MagicMock(),
        workers=1,
        retry_skipped=True,
        retry_slugs={"cafe-2"},
    )

    processed_slugs: list[str] = []

    async def mock_process_one(worker: MagicMock, cafe: CafeRecord) -> None:
        processed_slugs.append(cafe.slug)

    pipeline._process_one = mock_process_one  # type: ignore[assignment]
    await pipeline.run()

    assert processed_slugs == ["cafe-2"]


@pytest.mark.asyncio
async def test_pipeline_applies_limit(sample_cafes: list[CafeRecord]) -> None:
    """Verify limit parameter caps the number of processed cafes."""
    loader = MagicMock()
    loader.iter_cafes = MagicMock(return_value=iter(sample_cafes))

    state = MagicMock()
    state.is_processed = MagicMock(return_value=False)
    state.flush = AsyncMock()
    state.processed = set()

    scraper = MagicMock()
    scraper.context = MagicMock()
    scraper.page_pool = asyncio.Queue()

    pipeline = CrawlerPipeline(
        loader=loader,
        scraper=scraper,
        exporter=MagicMock(),
        state=state,
        skip_reporter=MagicMock(),
        workers=1,
    )

    processed_slugs: list[str] = []

    async def mock_process_one(worker: MagicMock, cafe: CafeRecord) -> None:
        processed_slugs.append(cafe.slug)

    pipeline._process_one = mock_process_one  # type: ignore[assignment]
    await pipeline.run(limit=1)

    assert len(processed_slugs) == 1
    assert processed_slugs == ["cafe-1"]


@pytest.mark.asyncio
async def test_pipeline_skips_zero_reviews(
    sample_cafes: list[CafeRecord],
) -> None:
    """Verify that cafes yielding zero reviews are recorded in skip report."""
    state = MagicMock()
    state.mark_processed = AsyncMock()

    exporter = MagicMock()
    exporter.append_many = AsyncMock()

    skip_reporter = MagicMock()
    skip_reporter.record = AsyncMock()

    pipeline = CrawlerPipeline(
        loader=MagicMock(),
        scraper=MagicMock(),
        exporter=exporter,
        state=state,
        skip_reporter=skip_reporter,
    )

    worker = MagicMock()
    worker.scrape = AsyncMock(return_value=[])
    worker.last_skip_reason = SkipReason.REVIEWS_TAB_FAILED

    await pipeline._process_one(worker, sample_cafes[0])

    skip_reporter.record.assert_called_once_with(
        sample_cafes[0], SkipReason.REVIEWS_TAB_FAILED
    )
    exporter.append_many.assert_not_called()
    state.mark_processed.assert_not_called()


@pytest.mark.asyncio
async def test_pipeline_checkpoints_success(
    sample_cafes: list[CafeRecord], sample_review: ReviewRecord
) -> None:
    """Verify that cafes with >=1 review are exported and marked in state."""
    state = MagicMock()
    state.mark_processed = AsyncMock()

    exporter = MagicMock()
    exporter.append_many = AsyncMock(return_value=1)

    skip_reporter = MagicMock()
    skip_reporter.record = AsyncMock()

    pipeline = CrawlerPipeline(
        loader=MagicMock(),
        scraper=MagicMock(),
        exporter=exporter,
        state=state,
        skip_reporter=skip_reporter,
    )

    worker = MagicMock()
    worker.scrape = AsyncMock(return_value=[sample_review])
    worker.last_skip_reason = None

    await pipeline._process_one(worker, sample_cafes[0])

    exporter.append_many.assert_called_once_with([sample_review])
    state.mark_processed.assert_called_once_with(sample_cafes[0].slug)
    skip_reporter.record.assert_not_called()


@pytest.mark.asyncio
async def test_pipeline_handles_cancellation(
    sample_cafes: list[CafeRecord],
) -> None:
    """Verify cancellation properly calls state.flush()."""
    loader = MagicMock()
    loader.iter_cafes = MagicMock(return_value=iter(sample_cafes))

    state = MagicMock()
    state.is_processed = MagicMock(return_value=False)
    state.flush = AsyncMock()
    state.processed = set()

    scraper = MagicMock()
    scraper.context = MagicMock()
    scraper.page_pool = asyncio.Queue()

    pipeline = CrawlerPipeline(
        loader=loader,
        scraper=scraper,
        exporter=MagicMock(),
        state=state,
        skip_reporter=MagicMock(),
        workers=1,
    )

    async def mock_process_one(worker: MagicMock, cafe: CafeRecord) -> None:
        raise asyncio.CancelledError()

    pipeline._process_one = mock_process_one  # type: ignore[assignment]

    with pytest.raises(asyncio.CancelledError):
        await pipeline.run()

    assert state.flush.called


@pytest.mark.asyncio
async def test_pipeline_mock_full_run(
    sample_cafes: list[CafeRecord], sample_review: ReviewRecord
) -> None:
    """Verify end-to-end pipeline execution with mixed success and skip."""
    loader = MagicMock()
    # 2 cafes
    loader.iter_cafes = MagicMock(return_value=iter(sample_cafes[:2]))

    state = MagicMock()
    state.is_processed = MagicMock(return_value=False)
    state.mark_processed = AsyncMock()
    state.flush = AsyncMock()
    state.processed = set()

    exporter = MagicMock()
    exporter.append_many = AsyncMock(return_value=1)

    skip_reporter = MagicMock()
    skip_reporter.record = AsyncMock()

    scraper = MagicMock()
    scraper.context = MagicMock()
    page_pool: asyncio.Queue = asyncio.Queue()
    mock_page = MagicMock(is_closed=MagicMock(return_value=False))
    await page_pool.put(mock_page)
    scraper.page_pool = page_pool

    pipeline = CrawlerPipeline(
        loader=loader,
        scraper=scraper,
        exporter=exporter,
        state=state,
        skip_reporter=skip_reporter,
        workers=1,
    )

    call_count = 0

    async def mock_scrape(target: MagicMock) -> list[ReviewRecord]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [sample_review]
        return []

    with patch.object(ScraperWorker, "scrape", side_effect=mock_scrape):
        await pipeline.run()

    # cafe-1 succeeded -> exporter called, state marked
    assert exporter.append_many.call_count == 1
    state.mark_processed.assert_called_once_with("cafe-1")

    # cafe-2 had 0 reviews -> skip_reporter called
    assert skip_reporter.record.call_count == 1
    assert state.flush.called
