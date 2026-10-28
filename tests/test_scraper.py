"""Tests for Google Maps scraper module."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.db_loader import CafeRecord
from src.resolver import ResolverStrategy, ResolvedTarget
from src.scraper import (
    GoogleMapsScraper,
    ScraperConfig,
    ScraperWorker,
    SkipReason,
    _normalize_name,
    _transliterate_word,
    names_look_same,
)


@pytest.fixture
def sample_cafe() -> CafeRecord:
    """Fixture providing a standard CafeRecord with coordinates."""
    return CafeRecord(
        slug="cafe-nili",
        name="عمارت نیلی",
        address="خیابان ولیعصر",
        city="تهران",
        neighborhood="ولیعصر",
        latitude=35.7592,
        longitude=51.4089,
        phone="+982188000000",
        search_address="تهران، خیابان ولیعصر",
        search_name="عمارت نیلی",
        search_city="تهران",
        id=1,
    )


@pytest.fixture
def sample_target(sample_cafe: CafeRecord) -> ResolvedTarget:
    """Fixture providing a ResolvedTarget instance."""
    return ResolvedTarget(
        cafe=sample_cafe,
        url="https://www.google.com/maps/search/?api=1&query=test",
        strategy=ResolverStrategy.TEXT_SEARCH,
    )


# 1. Helper tests
def test_names_look_same_persian_identical() -> None:
    """Verify identical Persian names match."""
    assert names_look_same("کافه بهار", "کافه بهار") is True
    assert names_look_same("کافه   بهار", "کافه بهار") is True


def test_names_look_same_with_transliteration() -> None:
    """Verify architectural translation and transliteration match."""
    assert names_look_same("عمارت نیلی", "Nili Mansion") is True
    assert names_look_same("کافه عمارت نیلی", "Nili Mansion Cafe") is True


def test_names_look_same_different_names() -> None:
    """Verify distinct cafe names do not match even with generic words."""
    assert names_look_same("کافه نادری", "کافه گلها") is False
    assert names_look_same("کافه بهار", "کافه نیلوفر") is False


def test_names_look_same_short_words_rejected() -> None:
    """Verify that short words or generic-only words are rejected."""
    assert names_look_same("A", "A") is False
    assert names_look_same("ab", "ab") is False
    assert names_look_same("کافه", "کافه") is False
    assert names_look_same("", "") is False


# 2. ScraperWorker tests with mocked Page
@pytest.mark.asyncio
async def test_worker_ensures_page_from_pool() -> None:
    """Verify ensure_page acquires an existing page from the pool."""
    pool: asyncio.Queue = asyncio.Queue()
    mock_page = MagicMock()
    mock_page.is_closed = MagicMock(return_value=False)
    await pool.put(mock_page)

    mock_context = MagicMock()
    worker = ScraperWorker(context=mock_context, worker_id=1, page_pool=pool)

    page = await worker.ensure_page()
    assert page == mock_page
    assert pool.empty() is True

    await worker.close()
    assert pool.qsize() == 1


@pytest.mark.asyncio
async def test_worker_creates_page_when_pool_empty() -> None:
    """Verify fallback to context.new_page() when the pool is exhausted."""
    pool: asyncio.Queue = asyncio.Queue()
    mock_new_page = AsyncMock()
    mock_new_page.is_closed = MagicMock(return_value=False)

    mock_context = MagicMock()
    mock_context.new_page = AsyncMock(return_value=mock_new_page)

    worker = ScraperWorker(context=mock_context, worker_id=2, page_pool=pool)
    page = await worker.ensure_page()

    assert page == mock_new_page
    assert mock_context.new_page.called

    await worker.close()
    assert mock_new_page.close.called
    assert pool.empty() is True


@pytest.mark.asyncio
async def test_worker_scrape_no_place_match(
    sample_target: ResolvedTarget,
) -> None:
    """Verify worker sets PLACE_COORDS_MISMATCH when coordinates diverge."""
    pool: asyncio.Queue = asyncio.Queue()
    mock_page = AsyncMock()
    mock_page.is_closed = MagicMock(return_value=False)
    # URL coordinate far from target (35.7592, 51.4089)
    mock_page.url = "https://www.google.com/maps/@36.9000,52.9000,17z"
    mock_page.goto = AsyncMock()
    mock_page.wait_for_timeout = AsyncMock()

    h1_loc = AsyncMock()
    h1_loc.is_visible = AsyncMock(return_value=True)
    h1_loc.inner_text = AsyncMock(return_value="Different Place")

    links_loc = AsyncMock()
    links_loc.count = AsyncMock(return_value=0)

    def mock_locator(sel: str) -> MagicMock:
        loc = MagicMock()
        if sel == "h1":
            loc.first = h1_loc
            loc.count = AsyncMock(return_value=1)
        elif "maps/place" in sel:
            return links_loc
        else:
            loc.first = AsyncMock(is_visible=AsyncMock(return_value=False))
            loc.count = AsyncMock(return_value=0)
        return loc

    mock_page.locator = MagicMock(side_effect=mock_locator)
    await pool.put(mock_page)

    mock_context = MagicMock()
    worker = ScraperWorker(context=mock_context, worker_id=3, page_pool=pool)

    reviews = await worker.scrape(sample_target)
    assert reviews == []
    assert worker.last_skip_reason == SkipReason.PLACE_COORDS_MISMATCH


@pytest.mark.asyncio
async def test_worker_handles_results_h1_as_search_results_page(
    sample_target: ResolvedTarget,
) -> None:
    """Verify that h1='Results' treats page as search results list, not single place."""
    pool: asyncio.Queue = asyncio.Queue()
    mock_page = AsyncMock()
    mock_page.is_closed = MagicMock(return_value=False)
    mock_page.url = "https://www.google.com/maps/search/test"
    mock_page.goto = AsyncMock()
    mock_page.wait_for_timeout = AsyncMock()

    h1_loc = AsyncMock()
    h1_loc.is_visible = AsyncMock(return_value=True)
    h1_loc.inner_text = AsyncMock(return_value="Different Place")
    h1_loc.inner_text = AsyncMock(return_value="Results")

    matched_link = AsyncMock()
    matched_link.get_attribute = AsyncMock(
        return_value="/maps/place/Cafe/data=!4m2!3m1!1s0x0:0x0!3d35.7592!4d51.4089"
    )
    matched_link.click = AsyncMock()

    links_loc = MagicMock()
    links_loc.count = AsyncMock(return_value=1)
    links_loc.nth = MagicMock(return_value=matched_link)

    def mock_locator(sel: str) -> MagicMock:
        loc = MagicMock()
        if sel == "h1":
            loc.first = h1_loc
            loc.count = AsyncMock(return_value=1)
        elif "maps/place" in sel:
            return links_loc
        else:
            loc.first = AsyncMock(is_visible=AsyncMock(return_value=False))
            loc.count = AsyncMock(return_value=0)
        return loc

    mock_page.locator = MagicMock(side_effect=mock_locator)
    await pool.put(mock_page)

    mock_context = MagicMock()
    worker = ScraperWorker(context=mock_context, worker_id=5, page_pool=pool)
    worker._click_reviews_tab = AsyncMock(return_value=False)

    await worker.scrape(sample_target)
    assert matched_link.click.called


@pytest.mark.asyncio
async def test_worker_scrape_success(sample_target: ResolvedTarget) -> None:
    """Verify successful scrape extracts reviews and returns ReviewRecord list."""
    pool: asyncio.Queue = asyncio.Queue()
    mock_page = AsyncMock()
    mock_page.is_closed = MagicMock(return_value=False)
    # Matching coordinate
    mock_page.url = "https://www.google.com/maps/@35.7592,51.4089,17z"
    mock_page.goto = AsyncMock()
    mock_page.wait_for_timeout = AsyncMock()
    mock_page.wait_for_selector = AsyncMock()

    h1_loc = AsyncMock()
    h1_loc.is_visible = AsyncMock(return_value=True)
    h1_loc.inner_text = AsyncMock(return_value="عمارت نیلی")

    reviews_tab = AsyncMock()
    reviews_tab.is_visible = AsyncMock(return_value=True)
    reviews_tab.click = AsyncMock()

    # Review card mock
    card = MagicMock()
    author_loc = AsyncMock(inner_text=AsyncMock(return_value="Maryam"))
    rating_loc = AsyncMock(
        get_attribute=AsyncMock(return_value="5 stars")
    )
    date_loc = AsyncMock(inner_text=AsyncMock(return_value="yesterday"))
    text_loc = AsyncMock(inner_text=AsyncMock(return_value="Amazing coffee!"))

    def card_locator(sel: str) -> MagicMock:
        loc = MagicMock()
        if "d4r55" in sel:
            loc.first = author_loc
        elif "img" in sel:
            loc.first = rating_loc
        elif "rsqaWe" in sel:
            loc.first = date_loc
        elif "wiI7pd" in sel:
            loc.first = text_loc
        return loc

    card.locator = MagicMock(side_effect=card_locator)

    cards_loc = MagicMock()
    cards_loc.count = AsyncMock(return_value=1)
    cards_loc.all = AsyncMock(return_value=[card])

    feed_loc = AsyncMock()
    feed_loc.evaluate = AsyncMock()

    async def mock_evaluate(js: str, *args: object) -> object:
        if "extract_js" in js or "const out = [];" in js:
            return [
                {
                    "author": "Maryam",
                    "rating": "5 stars",
                    "date": "yesterday",
                    "text": "Amazing coffee!",
                }
            ]
        elif "maxIter" in js:
            return {"count": 1}
        elif "btns" in js:
            return 0
        return 0

    mock_page.evaluate = AsyncMock(side_effect=mock_evaluate)

    def page_locator(sel: str) -> MagicMock:
        loc = MagicMock()
        if sel == "h1":
            loc.first = h1_loc
            loc.count = AsyncMock(return_value=1)
        elif "button[role='tab']" in sel:
            loc.first = reviews_tab
            loc.count = AsyncMock(return_value=1)
        elif "jftiEf" in sel:
            return cards_loc
        elif "m6QErb" in sel or "feed" in sel:
            loc.first = feed_loc
            loc.count = AsyncMock(return_value=1)
        elif "button.w8nwRe" in sel:
            loc.all = AsyncMock(return_value=[])
            loc.count = AsyncMock(return_value=0)
        elif "maps/place" in sel:
            loc.count = AsyncMock(return_value=0)
        else:
            loc.first = AsyncMock(is_visible=AsyncMock(return_value=False))
            loc.count = AsyncMock(return_value=0)
        return loc

    mock_page.locator = MagicMock(side_effect=page_locator)
    await pool.put(mock_page)

    mock_context = MagicMock()
    worker = ScraperWorker(context=mock_context, worker_id=4, page_pool=pool)

    reviews = await worker.scrape(sample_target)
    assert len(reviews) == 1
    assert reviews[0].author == "Maryam"
    assert reviews[0].rating == "5 stars"
    assert reviews[0].text == "Amazing coffee!"
    assert worker.last_skip_reason is None


def test_worker_last_skip_reason() -> None:
    """Verify last_skip_reason property tracks skip reasons accurately."""
    mock_context = MagicMock()
    pool: asyncio.Queue = asyncio.Queue()
    worker = ScraperWorker(context=mock_context, worker_id=5, page_pool=pool)
    assert worker.last_skip_reason is None

    worker._last_skip_reason = SkipReason.REVIEWS_TAB_FAILED
    assert worker.last_skip_reason == SkipReason.REVIEWS_TAB_FAILED


# 3. GoogleMapsScraper tests
@pytest.mark.asyncio
async def test_scraper_context_manager_with_cdp_mock() -> None:
    """Verify CDP connection reuses existing browser contexts."""
    mock_browser = MagicMock()
    mock_context = MagicMock()
    mock_context.pages = []
    mock_browser.contexts = [mock_context]

    mock_pw = MagicMock()
    mock_pw.chromium.connect_over_cdp = AsyncMock(return_value=mock_browser)
    mock_pw.stop = AsyncMock()

    with patch(
        "src.scraper.async_playwright",
        return_value=MagicMock(start=AsyncMock(return_value=mock_pw)),
    ):
        async with GoogleMapsScraper(cdp_url="http://localhost:9222") as scraper:
            assert scraper.context == mock_context
            assert scraper.page_pool.qsize() == 0


@pytest.mark.asyncio
async def test_scraper_falls_back_to_launch() -> None:
    """Verify GoogleMapsScraper falls back to launching Chromium on CDP failure."""
    mock_browser = MagicMock()
    mock_browser.close = AsyncMock()
    mock_context = MagicMock()
    mock_context.pages = []
    mock_browser.new_context = AsyncMock(return_value=mock_context)

    mock_pw = MagicMock()
    mock_pw.chromium.connect_over_cdp = AsyncMock(
        side_effect=Exception("CDP connection refused")
    )
    mock_pw.chromium.launch = AsyncMock(return_value=mock_browser)
    mock_pw.stop = AsyncMock()

    with patch(
        "src.scraper.async_playwright",
        return_value=MagicMock(start=AsyncMock(return_value=mock_pw)),
    ):
        async with GoogleMapsScraper(headless=True) as scraper:
            assert scraper.context == mock_context
            assert mock_pw.chromium.launch.called

    assert mock_browser.close.called
    assert mock_pw.stop.called


# 4. Tab rotation & crash recovery tests
@pytest.mark.asyncio
async def test_worker_rotate_tab() -> None:
    """Verify _rotate_tab closes old page and initializes fresh page."""
    pool: asyncio.Queue = asyncio.Queue()
    old_page = MagicMock()
    old_page.is_closed = MagicMock(return_value=False)
    old_page.close = AsyncMock()

    new_page = MagicMock()
    new_page.goto = AsyncMock()
    new_page.wait_for_timeout = AsyncMock()

    mock_context = MagicMock()
    mock_context.new_page = AsyncMock(return_value=new_page)

    worker = ScraperWorker(
        context=mock_context,
        worker_id=1,
        page_pool=pool,
        max_cafes_per_tab=5,
    )
    worker._page = old_page
    worker.cafes_processed_on_this_tab = 5
    worker._needs_new_page = True

    result_page = await worker._rotate_tab(old_page)

    assert result_page == new_page
    assert worker._page == new_page
    assert worker.cafes_processed_on_this_tab == 0
    assert worker._needs_new_page is False
    assert worker._owns_page is True
    assert old_page.close.called
    new_page.goto.assert_awaited_once_with(
        "https://www.google.com/maps",
        timeout=worker.config.PAGE_LOAD_TIMEOUT_MS,
        wait_until="commit",
    )


@pytest.mark.asyncio
async def test_worker_auto_rotates_after_max_cafes(
    sample_target: ResolvedTarget,
) -> None:
    """Verify worker rotates tab when cafes_processed reaches max_cafes_per_tab."""
    pool: asyncio.Queue = asyncio.Queue()
    mock_page = MagicMock()
    mock_page.is_closed = MagicMock(return_value=False)
    await pool.put(mock_page)

    mock_context = MagicMock()
    worker = ScraperWorker(
        context=mock_context,
        worker_id=1,
        page_pool=pool,
        max_cafes_per_tab=1,
    )
    worker._scrape_with_page = AsyncMock(return_value=[])
    worker._rotate_tab = AsyncMock()

    await worker.scrape(sample_target)

    assert worker.cafes_processed_on_this_tab == 1
    worker._rotate_tab.assert_awaited_once_with(mock_page)


@pytest.mark.asyncio
async def test_worker_recovers_from_crash(
    sample_target: ResolvedTarget,
) -> None:
    """Verify crash marks _needs_new_page and rotates tab on next scrape."""
    pool: asyncio.Queue = asyncio.Queue()
    crashed_page = MagicMock()
    crashed_page.is_closed = MagicMock(return_value=False)
    crashed_page.url = "https://www.google.com/maps/search/test"
    crashed_page.goto = AsyncMock(
        side_effect=Exception("Page.goto: Page crashed")
    )
    await pool.put(crashed_page)

    mock_context = MagicMock()
    worker = ScraperWorker(
        context=mock_context,
        worker_id=2,
        page_pool=pool,
        max_cafes_per_tab=10,
    )
    worker._rotate_tab = AsyncMock()

    # First scrape fails with crash
    reviews = await worker.scrape(sample_target)
    assert reviews == []
    assert worker.last_skip_reason == SkipReason.EXCEPTION
    assert worker._needs_new_page is True
    assert worker.cafes_processed_on_this_tab == 1

    # Second scrape triggers _rotate_tab due to _needs_new_page
    worker._scrape_with_page = AsyncMock(return_value=[])
    await worker.scrape(sample_target)
    assert worker._rotate_tab.called
    assert worker._needs_new_page is False


@pytest.mark.asyncio
async def test_worker_page_signal_timeout(
    sample_target: ResolvedTarget,
) -> None:
    """Verify worker sets PAGE_SIGNAL_NONE when signal doesn't appear."""
    pool: asyncio.Queue = asyncio.Queue()
    mock_page = AsyncMock()
    mock_page.is_closed = MagicMock(return_value=False)
    mock_page.url = "https://www.google.com/maps/search/test"
    mock_page.goto = AsyncMock()
    mock_page.wait_for_timeout = AsyncMock()

    loc_zero = MagicMock()
    loc_zero.count = AsyncMock(return_value=0)
    loc_zero.first = AsyncMock(is_visible=AsyncMock(return_value=False))
    mock_page.locator = MagicMock(return_value=loc_zero)
    await pool.put(mock_page)

    mock_context = MagicMock()
    cfg = ScraperConfig(PAGE_SIGNAL_TIMEOUT_S=0.05, POST_GOTO_SETTLE_MS=0)
    worker = ScraperWorker(
        context=mock_context,
        worker_id=1,
        page_pool=pool,
        config=cfg,
    )

    reviews = await worker.scrape(sample_target)
    assert reviews == []
    assert worker.last_skip_reason == SkipReason.PAGE_SIGNAL_NONE


@pytest.mark.asyncio
async def test_worker_page_signal_detected(
    sample_target: ResolvedTarget,
) -> None:
    """Verify worker proceeds when signal appears after initial delay."""
    pool: asyncio.Queue = asyncio.Queue()
    mock_page = AsyncMock()
    mock_page.is_closed = MagicMock(return_value=False)
    mock_page.url = "https://www.google.com/maps/@35.7592,51.4089,17z"
    mock_page.goto = AsyncMock()
    mock_page.wait_for_timeout = AsyncMock()

    counts = [0, 1]

    async def mock_count() -> int:
        return counts.pop(0) if counts else 1

    h1_loc = AsyncMock()
    h1_loc.is_visible = AsyncMock(return_value=True)
    h1_loc.inner_text = AsyncMock(return_value="عمارت نیلی")

    def page_locator(sel: str) -> MagicMock:
        loc = MagicMock()
        if sel == "h1":
            loc.first = h1_loc
            loc.count = AsyncMock(side_effect=mock_count)
        else:
            loc.first = AsyncMock(is_visible=AsyncMock(return_value=False))
            loc.count = AsyncMock(return_value=0)
        return loc

    mock_page.locator = MagicMock(side_effect=page_locator)
    await pool.put(mock_page)

    mock_context = MagicMock()
    cfg = ScraperConfig(PAGE_SIGNAL_TIMEOUT_S=1.0, POST_GOTO_SETTLE_MS=0)
    worker = ScraperWorker(
        context=mock_context,
        worker_id=1,
        page_pool=pool,
        config=cfg,
    )
    worker._click_reviews_tab = AsyncMock(return_value=False)

    reviews = await worker.scrape(sample_target)
    assert reviews == []
    assert worker.last_skip_reason == SkipReason.REVIEWS_TAB_FAILED


@pytest.mark.asyncio
async def test_worker_goto_retries_and_triggers_rate_limit(
    sample_target: ResolvedTarget,
) -> None:
    """Verify goto retries 3 times on timeout, sets PAGE_NAV_FAILED, and pauses."""
    pool: asyncio.Queue = asyncio.Queue()
    mock_page = AsyncMock()
    mock_page.is_closed = MagicMock(return_value=False)
    mock_page.goto = AsyncMock(
        side_effect=Exception("Page.goto: Timeout 20000ms exceeded")
    )
    mock_page.wait_for_timeout = AsyncMock()
    await pool.put(mock_page)

    rate_limit_pause = asyncio.Event()
    mock_context = MagicMock()
    worker = ScraperWorker(
        context=mock_context,
        worker_id=1,
        page_pool=pool,
        rate_limit_pause=rate_limit_pause,
    )

    reviews = await worker.scrape(sample_target)
    assert reviews == []
    assert worker.last_skip_reason == SkipReason.PAGE_NAV_FAILED
    assert mock_page.goto.call_count == 3
    assert rate_limit_pause.is_set()


@pytest.mark.asyncio
async def test_worker_sorry_page_triggers_rate_limit(
    sample_target: ResolvedTarget,
) -> None:
    """Verify encountering google.com/sorry triggers rate_limit_pause."""
    pool: asyncio.Queue = asyncio.Queue()
    mock_page = AsyncMock()
    mock_page.is_closed = MagicMock(return_value=False)
    mock_page.url = "https://www.google.com/sorry/index?continue=..."
    mock_page.goto = AsyncMock()
    mock_page.wait_for_timeout = AsyncMock()
    loc = MagicMock()
    loc.count = AsyncMock(return_value=0)
    mock_page.locator = MagicMock(return_value=loc)
    await pool.put(mock_page)

    rate_limit_pause = asyncio.Event()
    mock_context = MagicMock()
    worker = ScraperWorker(
        context=mock_context,
        worker_id=2,
        page_pool=pool,
        rate_limit_pause=rate_limit_pause,
    )

    reviews = await worker.scrape(sample_target)
    assert reviews == []
    assert worker.last_skip_reason == SkipReason.PAGE_SIGNAL_NONE
    assert rate_limit_pause.is_set()


@pytest.mark.asyncio
async def test_worker_pauses_when_rate_limit_set(
    sample_target: ResolvedTarget,
) -> None:
    """Verify worker waits when rate_limit_pause is active and clears it."""
    pool: asyncio.Queue = asyncio.Queue()
    mock_page = AsyncMock()
    mock_page.is_closed = MagicMock(return_value=False)
    mock_page.url = "https://www.google.com/maps/@35.7592,51.4089,17z"
    mock_page.goto = AsyncMock()
    mock_page.wait_for_timeout = AsyncMock()
    await pool.put(mock_page)

    rate_limit_pause = asyncio.Event()
    rate_limit_pause.set()

    mock_context = MagicMock()
    cfg = ScraperConfig(RATE_LIMIT_PAUSE_SECONDS=0.01)
    worker = ScraperWorker(
        context=mock_context,
        worker_id=3,
        page_pool=pool,
        config=cfg,
        rate_limit_pause=rate_limit_pause,
    )
    worker._scrape_with_page = AsyncMock(return_value=[])

    await worker.scrape(sample_target)
    assert not rate_limit_pause.is_set()


# 5. Integration test (skipped by default)
@pytest.mark.integration
@pytest.mark.asyncio
async def test_scrape_real_google_maps() -> None:
    """Run manually with Chrome debug on port 9222."""
    pytest.skip("Integration test: run manually with Chrome debug on port 9222")
