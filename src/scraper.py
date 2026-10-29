"""Google Maps scraper module for cafes and customer reviews.

Provides CDP tab pool recycling, page type detection, coordinate/name validation,
reviews tab navigation, infinite scroll reviews extraction, and rate limit detection.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import gc
import re
import time
from typing import Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    async_playwright,
)

try:
    from loguru import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)

from src.db_loader import CafeRecord
from src.exporter import ReviewRecord
from src.resolver import (
    MAX_MATCH_DISTANCE_LOOSE_M,
    MAX_MATCH_DISTANCE_STRICT_M,
    ResolvedTarget,
    extract_coords_from_href,
    extract_coords_from_url,
    haversine_m,
    names_look_same,
    normalize_venue_name as _normalize_name,
    transliterate_persian_word as _transliterate_word,
)


@dataclass(frozen=True, slots=True)
class ScraperConfig:
    """Selectors, timeouts, and scrolling limits for Google Maps scraper."""

    # Selectors
    SEL_REVIEW_ITEM: str = "div.jftiEf, div[data-review-id]"
    SEL_REVIEW_ITEM_FALLBACK: str = "div[jsaction*='review']"
    SEL_AUTHOR: str = ".d4r55"
    SEL_RATING: str = (
        "span[role='img'][aria-label*='star'], "
        "span[role='img'][aria-label*='ستاره']"
    )
    SEL_DATE: str = "span.rsqaWe"
    SEL_TEXT: str = "span.wiI7pd"
    SEL_MORE_BUTTON: str = "button.w8nwRe.kyuRq"
    SEL_PLACE_H1: str = "h1"
    SEL_PLACE_TAB: str = "button[role='tab']"
    SEL_REVIEWS_TAB: str = (
        "button[role='tab']:has-text('Reviews'), "
        "button[role='tab']:has-text('نظرات')"
    )
    SEL_REVIEWS_TAB_FALLBACK: str = "button[role='tab'][aria-label*='review' i]"
    SEL_FEED: str = (
        "div.m6QErb.DsmM0c, div.m6QErb[aria-label*='Reviews'], div[role='feed']"
    )
    SEL_RESULT_LINK: str = "a[href*='/maps/place/']"

    # Timeouts in milliseconds
    PAGE_LOAD_TIMEOUT_MS: int = 20_000
    PAGE_SIGNAL_TIMEOUT_S: float = 10.0
    RATE_LIMIT_PAUSE_SECONDS: float = 60.0
    POST_GOTO_SETTLE_MS: int = 3_000
    POST_TAB_CLICK_SETTLE_MS: int = 2_000
    POST_MORE_EXPAND_SETTLE_MS: int = 300
    PLACE_PAGE_WAIT_MS: int = 10_000
    REVIEW_CARDS_WAIT_MS: int = 20_000

    # Scrolling parameters
    SCROLL_STEP_PX: int = 3_000
    SCROLL_MAX_ITERATIONS: int = 10
    SCROLL_WAIT_FOR_NEW_CARD_MS: int = 1_500
    SCROLL_MAX_STAGNANT: int = 2

    # Tab rotation
    MAX_CAFES_PER_TAB: int = 10

    # Results to inspect
    MAX_RESULTS_TO_TRY: int = 20
    CONSENT_SELECTORS: tuple[str, ...] = (
        "form[action*='consent.google'] button",
        "button[aria-label*='Accept all']",
        "button[aria-label*='Reject all']",
    )


class SkipReason:
    """String constants explaining why a cafe scraping attempt was skipped."""

    PAGE_SIGNAL_NONE = "no_page_signal"
    NO_RESULT_MATCHED = "no_result_matched_within_threshold"
    PLACE_COORDS_MISMATCH = "place_coords_mismatch"
    PLACE_NAME_MISMATCH = "place_name_mismatch"
    REVIEWS_TAB_FAILED = "reviews_tab_could_not_be_clicked"
    REVIEW_CARDS_MISSING = "review_cards_never_appeared"
    PAGE_NAV_FAILED = "navigation_to_result_url_failed"
    EXCEPTION = "exception"
    NO_REVIEWS = "zero_reviews"


def _is_crash_error(err: BaseException) -> bool:
    """Check if exception indicates a browser tab or context crash."""
    err_str = str(err).lower()
    return any(
        k in err_str
        for k in (
            "crash",
            "target closed",
            "target page, context or browser has been closed",
            "session closed",
            "connection closed",
        )
    )


def _is_network_error(err: Exception) -> bool:
    """Return True if the error is a network/VPN/SSL failure."""
    msg = str(err).lower()
    return (
        "handshake failed" in msg
        or "net_error -100" in msg
        or "net_error -2" in msg
        or "connection reset" in msg
    )


class ScraperWorker:
    """Scrapes Google Maps reviews using an assigned or claimed browser tab."""

    def __init__(
        self,
        context: BrowserContext,
        worker_id: int,
        page_pool: asyncio.Queue,
        max_reviews: int = 150,
        config: Optional[ScraperConfig] = None,
        max_cafes_per_tab: Optional[int] = None,
        rate_limit_pause: Optional[asyncio.Event] = None,
    ) -> None:
        """Initialize the scraper worker.

        Args:
            context: Playwright BrowserContext.
            worker_id: Numeric worker identifier.
            page_pool: Queue holding reusable browser tabs.
            max_reviews: Maximum review records to extract per venue.
            config: Scraper configuration parameters and selectors.
            max_cafes_per_tab: Maximum cafes to process before rotating tab.
            rate_limit_pause: Shared Event to pause workers during rate limiting.
        """
        self.context = context
        self.worker_id = worker_id
        self.page_pool = page_pool
        self.max_reviews = max_reviews
        self.config = config or ScraperConfig()
        self.max_cafes_per_tab = (
            max_cafes_per_tab
            if max_cafes_per_tab is not None
            else self.config.MAX_CAFES_PER_TAB
        )
        self.rate_limit_pause = rate_limit_pause
        self.cafes_processed_on_this_tab: int = 0
        self._needs_new_page: bool = False
        self._page: Optional[Page] = None
        self._owns_page: bool = False
        self._last_skip_reason: Optional[str] = None

    @property
    def last_skip_reason(self) -> Optional[str]:
        """Return the reason code for the most recent skipped scrape, if any."""
        return self._last_skip_reason

    async def ensure_page(self) -> Page:
        """Claim a page from the pool or create a fallback page if empty.

        Returns:
            The acquired Playwright Page instance.
        """
        if self._page is not None and not self._page.is_closed():
            return self._page

        if not self.page_pool.empty():
            self._page = await self.page_pool.get()
            self._owns_page = False
            logger.debug(f"Worker {self.worker_id}: claimed page from pool.")
        else:
            logger.warning(
                f"Worker {self.worker_id}: page pool empty, creating new page."
            )
            self._page = await self.context.new_page()
            self._owns_page = True

        return self._page

    async def close(self) -> None:
        """Return claimed page to the pool or close if worker-owned."""
        if self._page is not None:
            if self._owns_page:
                try:
                    await self._page.close()
                except Exception as err:
                    logger.debug(
                        f"Worker {self.worker_id}: error closing page: {err}"
                    )
            else:
                await self.page_pool.put(self._page)
            self._page = None
            gc.collect()

    async def _rotate_tab(self, page: Optional[Page] = None) -> Page:
        """Close current page, create a fresh page, and navigate to Google Maps.

        Args:
            page: Optional Page instance to close. Defaults to self._page.

        Returns:
            Newly created and navigated Page instance.
        """
        target_page = page if page is not None else self._page
        if target_page is not None:
            try:
                if not target_page.is_closed():
                    await target_page.close()
            except Exception as err:
                logger.debug(
                    f"[w{self.worker_id}] error closing page during rotate: {err}"
                )
        gc.collect()

        new_page = await self.context.new_page()
        try:
            await new_page.goto(
                "https://www.google.com/maps",
                timeout=self.config.PAGE_LOAD_TIMEOUT_MS,
                wait_until="commit",
            )
            await new_page.wait_for_timeout(3_000)
        except Exception as err:
            if _is_crash_error(err):
                raise
            if _is_network_error(err):
                logger.warning(
                    f"[w{self.worker_id}] [network] SSL handshake failed — check VPN"
                )
            else:
                logger.warning(
                    f"[w{self.worker_id}] error navigating new tab: {err}"
                )

        self.cafes_processed_on_this_tab = 0
        self._needs_new_page = False
        self._page = new_page
        self._owns_page = True
        return new_page

    async def _dismiss_consent(self, page: Page) -> None:
        """Dismiss Google consent dialogs if present."""
        for selector in self.config.CONSENT_SELECTORS:
            try:
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=500):
                    await btn.click()
                    break
            except Exception as err:
                if _is_crash_error(err):
                    raise

    async def _click_reviews_tab(
        self,
        page: Page,
        target: Optional[ResolvedTarget | CafeRecord] = None,
    ) -> bool:
        """Attempt clicking the reviews tab with fallback and logging."""
        cafe_id = ""
        if isinstance(target, ResolvedTarget) and target.cafe:
            cafe_id = target.cafe.id
        elif isinstance(target, CafeRecord):
            cafe_id = target.id

        tab = page.locator(self.config.SEL_REVIEWS_TAB).first
        if await tab.count() == 0:
            tab = page.get_by_role(
                "tab", name=re.compile(r"reviews|نظرات", re.I)
            ).first

        if await tab.count() == 0:
            all_tabs = page.locator('button[role="tab"]')
            n = await all_tabs.count()
            labels: list[str] = []
            for i in range(min(n, 10)):
                try:
                    t = all_tabs.nth(i)
                    aria = await t.get_attribute("aria-label") or ""
                    text = (await t.inner_text(timeout=1000)).strip()
                    labels.append(f"{text}|{aria}")
                except Exception as err:
                    if _is_crash_error(err):
                        raise
            logger.warning(
                f"[w{self.worker_id}][{cafe_id}] Reviews tab not found. "
                f"Found {n} tabs: {labels}"
            )
            return False

        try:
            selected = await tab.get_attribute("aria-selected")
        except Exception as err:
            if _is_crash_error(err):
                raise
            selected = None

        if selected == "true":
            return True

        try:
            if not await tab.is_visible():
                await page.wait_for_timeout(2000)
                if not await tab.is_visible():
                    return False
        except Exception as err:
            if _is_crash_error(err):
                raise
            return False

        try:
            await tab.click(timeout=10_000)
            await page.wait_for_timeout(3000)
            return True
        except Exception as err:
            if _is_crash_error(err):
                raise
            logger.debug(
                f"[w{self.worker_id}][{cafe_id}] click failed: {err}"
            )
            return False

    async def _wait_for_review_cards(
        self,
        page: Page,
        target: Optional[ResolvedTarget | CafeRecord] = None,
    ) -> bool:
        """Wait for review card elements using multiple fallback selectors."""
        cafe_id = ""
        if isinstance(target, ResolvedTarget) and target.cafe:
            cafe_id = target.cafe.id
        elif isinstance(target, CafeRecord):
            cafe_id = target.id

        selectors = [
            "div.jftiEf",
            "div[data-review-id]",
            "div[jsaction*='review']",
        ]
        for sel in selectors:
            try:
                await page.wait_for_selector(
                    sel, timeout=30_000, state="attached"
                )
                count = await page.locator(sel).count()
                if count > 0:
                    logger.debug(
                        f"[w{self.worker_id}][{cafe_id}] cards via {sel}: {count}"
                    )
                    return True
            except Exception as err:
                if _is_crash_error(err):
                    raise
                continue
        logger.warning(
            f"[w{self.worker_id}][{cafe_id}] Review cards never appeared "
            f"after 30s on any selector."
        )
        return False

    async def _log_no_signal_diagnostics(
        self, page: Page, cafe_id: int | str, message: str
    ) -> None:
        """Log diagnostic details when no page signal is found."""
        try:
            h1_c = await page.locator(self.config.SEL_PLACE_H1).count()
        except Exception:
            h1_c = -1
        try:
            link_c = await page.locator(self.config.SEL_RESULT_LINK).count()
        except Exception:
            link_c = -1
        try:
            main_c = await page.locator("[role='main']").count()
        except Exception:
            main_c = -1
        try:
            results_div_c = await page.locator(
                "div[aria-label*='Results' i], div[aria-label*='نتایج' i]"
            ).count()
        except Exception:
            results_div_c = -1

        logger.warning(
            f"[w{self.worker_id}][{cafe_id}] {message} | "
            f"url='{page.url}' | h1_count={h1_c} | result_links={link_c} | "
            f"role_main={main_c} | results_div={results_div_c}"
        )

    async def _get_h1_text(self, page: Page) -> str:
        """Extract and return text from the main place h1, or empty string.

        Args:
            page: Playwright Page to inspect.

        Returns:
            Stripped inner text of h1 if found, else empty string.
        """
        try:
            h1 = page.locator(self.config.SEL_PLACE_H1)
            count = await h1.count()
            if isinstance(count, int) and count > 0:
                val = await h1.first.inner_text(timeout=2000)
                return val.strip() if isinstance(val, str) else ""
        except Exception as exc:
            if _is_crash_error(exc):
                raise
            logger.debug(f"[w{self.worker_id}] failed to get h1 text: {exc}")
        return ""

    async def _retry_with_bare_name(
        self, page: Page, cafe: CafeRecord
    ) -> bool:
        """Retry search with just name + city when coord mismatch occurs.

        Returns True if a place within MAX_MATCH_DISTANCE_STRICT_M is found.
        """
        from urllib.parse import quote_plus

        bare_query = f"{cafe.name} {cafe.city}".strip()
        encoded = quote_plus(bare_query)
        if (
            cafe.has_coordinates
            and cafe.latitude is not None
            and cafe.longitude is not None
        ):
            bare_url = (
                f"https://www.google.com/maps/search/{encoded}"
                f"/@{cafe.latitude},{cafe.longitude},17z?hl=en"
            )
        else:
            bare_url = (
                f"https://www.google.com/maps/search/?api=1"
                f"&query={encoded}&hl=en"
            )
        logger.debug(
            f"[w{self.worker_id}][{cafe.id}] retry with bare query: "
            f"{bare_query!r}"
        )
        try:
            await page.goto(
                bare_url,
                timeout=self.config.PAGE_LOAD_TIMEOUT_MS,
                wait_until="commit",
            )
            await page.wait_for_timeout(self.config.POST_GOTO_SETTLE_MS)
        except Exception as err:
            if _is_crash_error(err):
                raise
            if _is_network_error(err):
                logger.warning(
                    f"[w{self.worker_id}][{cafe.id}] "
                    "[network] SSL handshake failed — check VPN"
                )
                self._last_skip_reason = SkipReason.PAGE_NAV_FAILED
            else:
                logger.debug(
                    f"[w{self.worker_id}][{cafe.id}] bare retry nav failed: {err}"
                )
            return False

        # Check if we landed on a place page directly
        page_coords = extract_coords_from_url(page.url)
        if (
            page_coords
            and cafe.has_coordinates
            and cafe.latitude
            and cafe.longitude
        ):
            dist = haversine_m(cafe.latitude, cafe.longitude, *page_coords)
            if dist <= MAX_MATCH_DISTANCE_STRICT_M:
                page_name = await self._get_h1_text(page)
                if page_name and names_look_same(cafe.name, page_name):
                    logger.info(
                        f"[w{self.worker_id}][{cafe.id}] bare retry matched "
                        f"(dist={dist:.1f}m)"
                    )
                    return True
                logger.debug(
                    f"[w{self.worker_id}][{cafe.id}] bare retry: coords match "
                    f"but name mismatch: db={cafe.name!r} page={page_name!r}"
                )
                return False

        # Otherwise, try the first result link on a search results page
        links = page.locator(self.config.SEL_RESULT_LINK)
        count = await links.count()
        if count == 0:
            return False
        try:
            href = await links.first.get_attribute("href") or ""
            coords = extract_coords_from_href(href)
            if (
                coords
                and cafe.has_coordinates
                and cafe.latitude
                and cafe.longitude
            ):
                dist = haversine_m(cafe.latitude, cafe.longitude, *coords)
                if dist <= MAX_MATCH_DISTANCE_STRICT_M:
                    link_text = (await links.first.inner_text()).strip()
                    if names_look_same(cafe.name, link_text):
                        logger.info(
                            f"[w{self.worker_id}][{cafe.id}] bare retry matched "
                            f"via first result (dist={dist:.1f}m)"
                        )
                        try:
                            await page.goto(
                                href,
                                timeout=self.config.PAGE_LOAD_TIMEOUT_MS,
                                wait_until="commit",
                            )
                            await page.wait_for_timeout(
                                self.config.POST_GOTO_SETTLE_MS
                            )
                            return True
                        except Exception as err:
                            if _is_crash_error(err):
                                raise
                            if _is_network_error(err):
                                logger.warning(
                                    f"[w{self.worker_id}][{cafe.id}] "
                                    "[network] SSL handshake failed — check VPN"
                                )
                                self._last_skip_reason = (
                                    SkipReason.PAGE_NAV_FAILED
                                )
                            else:
                                logger.debug(
                                    f"[w{self.worker_id}][{cafe.id}] "
                                    f"bare retry first link nav failed: {err}"
                                )
                            return False
                    logger.debug(
                        f"[w{self.worker_id}][{cafe.id}] bare retry first link "
                        f"coords match but name mismatch: "
                        f"db={cafe.name!r} link={link_text!r}"
                    )
        except Exception as err:
            logger.debug(
                f"[w{self.worker_id}][{cafe.id}] bare retry link failed: {err}"
            )
        return False

    async def _validate_place_match(
        self, page: Page, cafe: CafeRecord
    ) -> bool:
        """Validate that the current page matches the target cafe.

        Requires BOTH coordinate match (within MAX_MATCH_DISTANCE_STRICT_M)
        and name match (via names_look_same). If coordinates diverge,
        retries with bare name+city.

        Args:
            page: Current Playwright page.
            cafe: CafeRecord being validated.

        Returns:
            True if place matches both coordinate and name, False otherwise.
        """
        page_coords = extract_coords_from_url(page.url)
        if (
            page_coords
            and cafe.has_coordinates
            and cafe.latitude is not None
            and cafe.longitude is not None
        ):
            dist = haversine_m(
                cafe.latitude,
                cafe.longitude,
                page_coords[0],
                page_coords[1],
            )
            if dist <= MAX_MATCH_DISTANCE_STRICT_M:
                page_name = await self._get_h1_text(page)
                if page_name and names_look_same(cafe.name, page_name):
                    return True
                logger.warning(
                    f"[w{self.worker_id}][{cafe.id}] coords match (dist={dist:.1f}m) "
                    f"but name mismatch: db={cafe.name!r} page={page_name!r}"
                )
                self._last_skip_reason = SkipReason.PLACE_NAME_MISMATCH
                return False
            else:
                logger.warning(
                    f"Coordinate mismatch for {cafe.slug}: "
                    f"{dist:.1f}m > {MAX_MATCH_DISTANCE_STRICT_M}m. "
                    f"Retrying with bare name+city..."
                )
                if await self._retry_with_bare_name(page, cafe):
                    return True
                self._last_skip_reason = SkipReason.PLACE_COORDS_MISMATCH
                return False
        else:
            page_name = await self._get_h1_text(page)
            if page_name.lower() in ("results", "نتایج"):
                await self._log_no_signal_diagnostics(
                    page,
                    cafe.id,
                    f"place check remained on search results ('{page_name}')",
                )
                self._last_skip_reason = SkipReason.PAGE_SIGNAL_NONE
                return False
            if not page_name or not names_look_same(cafe.name, page_name):
                logger.warning(
                    f"Name mismatch for {cafe.slug}: "
                    f"'{cafe.name}' vs '{page_name}'"
                )
                self._last_skip_reason = SkipReason.PLACE_NAME_MISMATCH
                return False
            return True

    def _trigger_rate_limit(self, cafe_slug: str) -> None:
        """Trigger shared rate limit pause event and log warning.

        Args:
            cafe_slug: Slug of the cafe encountering rate-limiting.
        """
        logger.warning(
            f"[rate-limit] detected by worker {self.worker_id} on cafe "
            f"{cafe_slug} — pausing all workers for 60s"
        )
        if self.rate_limit_pause is not None:
            self.rate_limit_pause.set()

    async def scrape(self, target: ResolvedTarget) -> list[ReviewRecord]:
        """Scrape reviews for a resolved cafe target.

        Args:
            target: ResolvedTarget with cafe metadata and query URLs.

        Returns:
            List of extracted ReviewRecord items. Empty list if skipped or failed.
        """
        self._last_skip_reason = None

        if self.rate_limit_pause is not None and self.rate_limit_pause.is_set():
            pause_duration = getattr(
                self.config, "RATE_LIMIT_PAUSE_SECONDS", 60.0
            )
            logger.info(
                f"[w{self.worker_id}] rate_limit_pause is set, "
                f"pausing for {pause_duration}s"
            )
            await asyncio.sleep(pause_duration)
            self.rate_limit_pause.clear()

        if self._needs_new_page:
            try:
                await self._rotate_tab(self._page)
            except Exception as err:
                logger.error(
                    f"Worker {self.worker_id}: failed rotating tab: {err}"
                )
            self._needs_new_page = False

        try:
            page = await self.ensure_page()
        except Exception as err:
            logger.error(f"Worker {self.worker_id}: failed acquiring page: {err}")
            self._last_skip_reason = SkipReason.EXCEPTION
            self.cafes_processed_on_this_tab += 1
            return []

        t0 = time.monotonic()
        try:
            return await self._scrape_with_page(page, target)
        except Exception as err:
            logger.error(
                f"Worker {self.worker_id}: error scraping {target.cafe.slug}: {err}"
            )
            self._last_skip_reason = SkipReason.EXCEPTION
            return []
        finally:
            logger.debug(
                f"[w{self.worker_id}][{target.cafe.id}] scrape() took "
                f"{time.monotonic() - t0:.1f}s"
            )
            self.cafes_processed_on_this_tab += 1
            if self.cafes_processed_on_this_tab >= self.max_cafes_per_tab:
                logger.info(
                    f"[w{self.worker_id}] rotating tab after "
                    f"{self.cafes_processed_on_this_tab} cafes"
                )
                try:
                    await self._rotate_tab(page)
                except Exception as err:
                    logger.warning(
                        f"[w{self.worker_id}] error during tab rotation: {err}"
                    )

    async def _scrape_with_page(
        self, page: Page, target: ResolvedTarget
    ) -> list[ReviewRecord]:
        """Internal scraping routine after obtaining page with crash recovery."""
        try:
            return await self._scrape_with_page_impl(page, target)
        except Exception as err:
            if _is_crash_error(err):
                logger.warning(
                    f"[w{self.worker_id}][{target.cafe.id}] "
                    f"page crashed, will recreate: {err}"
                )
                self._last_skip_reason = SkipReason.EXCEPTION
                self._needs_new_page = True
                return []
            raise

    async def _scrape_with_page_impl(
        self, page: Page, target: ResolvedTarget
    ) -> list[ReviewRecord]:
        """Core scraping routine after obtaining page."""
        max_attempts: int = 3
        goto_success: bool = False
        consecutive_timeouts: int = 0
        for attempt in range(1, max_attempts + 1):
            try:
                logger.debug(
                    f"[w{self.worker_id}][{target.cafe.id}] "
                    f"goto START (attempt {attempt}/{max_attempts}): {target.url}"
                )
                await page.goto(
                    target.url,
                    timeout=self.config.PAGE_LOAD_TIMEOUT_MS,
                    wait_until="commit",
                )
                logger.debug(
                    f"[w{self.worker_id}][{target.cafe.id}] goto DONE: {page.url}"
                )
                goto_success = True
                break
            except Exception as err:
                if _is_crash_error(err):
                    raise
                if _is_network_error(err):
                    logger.warning(
                        f"[w{self.worker_id}][{target.cafe.id}] "
                        "[network] SSL handshake failed — check VPN"
                    )
                    self._last_skip_reason = SkipReason.PAGE_NAV_FAILED
                    return []
                consecutive_timeouts += 1
                logger.warning(
                    f"Worker {self.worker_id}: nav attempt {attempt}/{max_attempts} "
                    f"failed for {target.url}: {err}"
                )
                if attempt < max_attempts:
                    await page.wait_for_timeout(3_000)

        if not goto_success:
            if consecutive_timeouts >= max_attempts:
                self._trigger_rate_limit(target.cafe.slug)
            self._last_skip_reason = SkipReason.PAGE_NAV_FAILED
            return []

        if "google.com/sorry" in page.url:
            self._trigger_rate_limit(target.cafe.slug)
            await self._log_no_signal_diagnostics(
                page, target.cafe.id, "rate limited on google.com/sorry"
            )
            self._last_skip_reason = SkipReason.PAGE_SIGNAL_NONE
            return []

        await self._dismiss_consent(page)
        logger.debug(
            f"[w{self.worker_id}][{target.cafe.id}] consent dismissed"
        )
        logger.debug(
            f"[w{self.worker_id}][{target.cafe.id}] waiting for page signal"
        )
        wait_timeout = getattr(self.config, "PAGE_SIGNAL_TIMEOUT_S", 10.0)
        wait_deadline = asyncio.get_event_loop().time() + wait_timeout
        signal_found = False
        while asyncio.get_event_loop().time() < wait_deadline:
            try:
                h1_count = await page.locator(self.config.SEL_PLACE_H1).count()
                link_count = await page.locator(
                    self.config.SEL_RESULT_LINK
                ).count()
                if h1_count > 0 or link_count > 0:
                    signal_found = True
                    break
            except Exception as err:
                if _is_crash_error(err):
                    raise
            await page.wait_for_timeout(500)

        if not signal_found:
            await self._log_no_signal_diagnostics(
                page, target.cafe.id, "no signal after 10s wait"
            )
            self._last_skip_reason = SkipReason.PAGE_SIGNAL_NONE
            return []

        logger.debug(
            f"[w{self.worker_id}][{target.cafe.id}] page signal detected"
        )
        await page.wait_for_timeout(self.config.POST_GOTO_SETTLE_MS)
        logger.debug(
            f"[w{self.worker_id}][{target.cafe.id}] post-goto settle done"
        )

        # Detect whether we are on a single place page or search results list
        is_place = False
        h1_text = ""
        logger.debug(f"[w{self.worker_id}][{target.cafe.id}] checking h1")
        try:
            h1_el = page.locator(self.config.SEL_PLACE_H1).first
            if await h1_el.is_visible(timeout=1000):
                val = await h1_el.inner_text(timeout=1000)
                h1_text = val.strip() if isinstance(val, str) else ""
                is_place = True
        except Exception as err:
            if _is_crash_error(err):
                raise

        if h1_text.lower() in ("results", "نتایج"):
            is_place = False

        logger.debug(
            f"[w{self.worker_id}][{target.cafe.id}] is_place={is_place}"
        )

        result_links = page.locator(self.config.SEL_RESULT_LINK)
        logger.debug(
            f"[w{self.worker_id}][{target.cafe.id}] counting result links"
        )
        link_count = await result_links.count()
        logger.debug(
            f"[w{self.worker_id}][{target.cafe.id}] result links: {link_count}"
        )

        if not is_place and link_count == 0:
            await self._log_no_signal_diagnostics(
                page, target.cafe.id, "no page signal (neither place nor links)"
            )
            self._last_skip_reason = SkipReason.PAGE_SIGNAL_NONE
            return []

        matched_via_bare_retry = False
        if not is_place and link_count > 0:
            # Multi-result search feed: find matching venue link
            matched_link = None
            for idx in range(min(link_count, self.config.MAX_RESULTS_TO_TRY)):
                link = result_links.nth(idx)
                href = await link.get_attribute("href") or ""
                coords = extract_coords_from_href(href)
                if (
                    coords
                    and target.cafe.has_coordinates
                    and target.cafe.latitude is not None
                    and target.cafe.longitude is not None
                ):
                    dist = haversine_m(
                        target.cafe.latitude,
                        target.cafe.longitude,
                        coords[0],
                        coords[1],
                    )
                    if dist <= MAX_MATCH_DISTANCE_LOOSE_M:
                        # Check name too
                        link_text = (await link.inner_text()).strip()
                        if names_look_same(target.cafe.name, link_text):
                            matched_link = link
                            break
                        else:
                            logger.debug(
                                f"[w{self.worker_id}][{target.cafe.id}] "
                                f"result #{idx} coords match (dist={dist:.1f}m) "
                                f"but name mismatch: db={target.cafe.name!r} "
                                f"link={link_text!r}"
                            )
                else:
                    text = (await link.inner_text()).strip()
                    if names_look_same(target.cafe.name, text):
                        matched_link = link
                        break

            if matched_link is None:
                logger.warning(
                    f"No matching link within distance for {target.cafe.slug}. "
                    f"Retrying with bare name+city..."
                )
                if await self._retry_with_bare_name(page, target.cafe):
                    matched_via_bare_retry = True
                else:
                    self._last_skip_reason = SkipReason.NO_RESULT_MATCHED
                    return []
            else:
                try:
                    await matched_link.click()
                    await page.wait_for_timeout(self.config.POST_GOTO_SETTLE_MS)
                except Exception as err:
                    if _is_crash_error(err):
                        raise
                    logger.warning(
                        f"Worker {self.worker_id}: click link failed: {err}"
                    )
                    self._last_skip_reason = SkipReason.PAGE_NAV_FAILED
                    return []

        if not matched_via_bare_retry:
            if is_place:
                logger.debug(
                    f"[w{self.worker_id}][{target.cafe.id}] validating place match"
                )
            if not await self._validate_place_match(page, target.cafe):
                return []

        # Click reviews tab
        try:
            clicked = await asyncio.wait_for(
                self._click_reviews_tab(page, target), timeout=15.0
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"[w{self.worker_id}][{target.cafe.id}] "
                "click_reviews_tab hard timeout"
            )
            clicked = False

        if not clicked:
            logger.debug(
                f"[w{self.worker_id}][{target.cafe.id}] retrying place page load"
            )
            try:
                await page.reload(wait_until="commit", timeout=15_000)
                await page.wait_for_timeout(5_000)
                clicked = await self._click_reviews_tab(page, target)
            except Exception as err:
                if _is_crash_error(err):
                    raise
                clicked = False

        if not clicked:
            self._last_skip_reason = SkipReason.REVIEWS_TAB_FAILED
            return []

        await page.wait_for_timeout(self.config.POST_TAB_CLICK_SETTLE_MS)

        # Wait for review cards to appear
        try:
            cards_ready = await asyncio.wait_for(
                self._wait_for_review_cards(page, target), timeout=40.0
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"[w{self.worker_id}][{target.cafe.id}] "
                "wait_for_review_cards hard timeout"
            )
            cards_ready = False

        if not cards_ready:
            self._last_skip_reason = SkipReason.REVIEW_CARDS_MISSING
            return []

        # === JS-BASED SCROLL ===
        logger.debug(
            f"[w{self.worker_id}][{target.cafe.id}] starting JS scroll"
        )
        scroll_js = """
            (maxIter) => {
                const cards = document.querySelectorAll(
                    'div.jftiEf, div[data-review-id]'
                );
                if (cards.length === 0) return {count: 0};
                let el = cards[0].parentElement;
                while (el && el.scrollHeight <= el.clientHeight + 4) {
                    el = el.parentElement;
                }
                if (!el) return {count: cards.length};
                el.scrollTop = el.scrollHeight;
                return {count: cards.length};
            }
        """
        last_count = 0
        stagnant = 0
        for i in range(self.config.SCROLL_MAX_ITERATIONS):
            try:
                result = await page.evaluate(
                    scroll_js, self.config.SCROLL_MAX_ITERATIONS
                )
            except Exception as err:
                if _is_crash_error(err):
                    raise
                logger.debug(
                    f"[w{self.worker_id}][{target.cafe.id}] "
                    f"scroll eval failed: {err}"
                )
                break
            await page.wait_for_timeout(self.config.SCROLL_WAIT_FOR_NEW_CARD_MS)
            new_count = (
                result.get("count", 0) if isinstance(result, dict) else 0
            )
            logger.debug(
                f"[w{self.worker_id}][{target.cafe.id}] "
                f"scroll iter {i}: {new_count} cards"
            )
            if new_count >= self.max_reviews:
                break
            if new_count == last_count:
                stagnant += 1
                if stagnant >= self.config.SCROLL_MAX_STAGNANT:
                    break
            else:
                stagnant = 0
                last_count = new_count

        # === JS-BASED EXPAND "MORE" ===
        logger.debug(
            f"[w{self.worker_id}][{target.cafe.id}] expanding More buttons"
        )
        expand_js = """
            () => {
                const btns = document.querySelectorAll(
                    'button.w8nwRe.kyuRq, button[aria-label*="More" i], ' +
                    'button[aria-label*="بیشتر" i]'
                );
                let n = 0;
                for (const b of btns) {
                    try { b.click(); n++; } catch(e) {}
                }
                return n;
            }
        """
        try:
            expanded = await page.evaluate(expand_js)
            logger.debug(
                f"[w{self.worker_id}][{target.cafe.id}] "
                f"expanded {expanded} More buttons"
            )
        except Exception as err:
            if _is_crash_error(err):
                raise
            logger.debug(
                f"[w{self.worker_id}][{target.cafe.id}] expand failed: {err}"
            )
        await page.wait_for_timeout(self.config.POST_MORE_EXPAND_SETTLE_MS)

        # === JS-BASED EXTRACTION ===
        logger.debug(
            f"[w{self.worker_id}][{target.cafe.id}] extracting reviews via JS"
        )
        extract_js = """
            (max) => {
                const cards = document.querySelectorAll(
                    'div.jftiEf, div[data-review-id]'
                );
                const out = [];
                for (const card of cards) {
                    if (out.length >= max) break;
                    const author = (
                        card.querySelector('.d4r55')?.innerText || ''
                    ).trim();
                    const ratingEl = card.querySelector(
                        'span[role="img"][aria-label*="star" i], ' +
                        'span[role="img"][aria-label*="ستاره" i]'
                    );
                    const rating = ratingEl?.getAttribute('aria-label') || '';
                    const date = (
                        card.querySelector('span.rsqaWe')?.innerText || ''
                    ).trim();
                    const text = (
                        card.querySelector('span.wiI7pd')?.innerText || ''
                    ).trim();
                    out.push({author, rating, date, text});
                }
                return out;
            }
        """
        try:
            raw = await page.evaluate(extract_js, self.max_reviews)
        except Exception as err:
            if _is_crash_error(err):
                raise
            logger.warning(
                f"[w{self.worker_id}][{target.cafe.id}] extraction failed: {err}"
            )
            raw = []

        reviews: list[ReviewRecord] = []
        seen: set[tuple[str, str, str, str]] = set()
        for item in raw:
            author = (item.get("author") or "").strip() or "Anonymous"
            rating = (item.get("rating") or "").strip()
            date = (item.get("date") or "").strip()
            text = " ".join((item.get("text") or "").splitlines()).strip()
            key = (author, date, text, rating)
            if key in seen:
                continue
            seen.add(key)
            if author or text or rating:
                reviews.append(
                    ReviewRecord(
                        cafe_slug=target.cafe.slug,
                        cafe_name=target.cafe.name,
                        city=target.cafe.city,
                        author=author,
                        rating=rating,
                        date=date,
                        text=text,
                    )
                )

        logger.debug(
            f"[w{self.worker_id}][{target.cafe.id}] "
            f"extracted {len(reviews)} reviews"
        )

        if not reviews:
            self._last_skip_reason = SkipReason.NO_REVIEWS

        return reviews


class GoogleMapsScraper:
    """Manages CDP connection, Playwright lifecycle, and tab pooling."""

    def __init__(
        self,
        headless: bool = True,
        max_reviews: int = 150,
        proxy: Optional[str] = None,
        cdp_url: str = "http://localhost:9222",
    ) -> None:
        """Initialize scraper configuration.

        Args:
            headless: Whether to run headless if Chromium is launched.
            max_reviews: Default reviews limit per cafe.
            proxy: Optional HTTP/SOCKS proxy URL.
            cdp_url: Chrome DevTools Protocol endpoint.
        """
        self.headless = headless
        self.max_reviews = max_reviews
        self.proxy = proxy
        self.cdp_url = cdp_url
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._owns_browser: bool = False
        self._page_pool: asyncio.Queue = asyncio.Queue()

    @property
    def context(self) -> BrowserContext:
        """Return the active BrowserContext."""
        if self._context is None:
            raise RuntimeError("Scraper context is not initialized.")
        return self._context

    @property
    def page_pool(self) -> asyncio.Queue:
        """Return the tab pool queue."""
        return self._page_pool

    async def __aenter__(self) -> GoogleMapsScraper:
        """Connect to Chrome via CDP or fall back to launching Chromium."""
        self._playwright = await async_playwright().start()

        try:
            browser = await self._playwright.chromium.connect_over_cdp(self.cdp_url)
            contexts = browser.contexts
            if contexts:
                self._context = contexts[0]
            else:
                self._context = await browser.new_context()
            self._owns_browser = False
            logger.info(f"Connected to Chrome via CDP at {self.cdp_url}")
        except Exception as err:
            logger.warning(
                f"CDP connection to {self.cdp_url} failed ({err}). "
                "Launching local Chromium fallback."
            )
            proxy_dict = {"server": self.proxy} if self.proxy else None
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless, proxy=proxy_dict
            )
            self._context = await self._browser.new_context()
            self._owns_browser = True

        for page in self._context.pages:
            if not page.is_closed():
                await self._page_pool.put(page)

        logger.info(
            f"Initialized page pool with {self._page_pool.qsize()} existing tabs"
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object | None,
    ) -> None:
        """Close browser if owned and stop Playwright."""
        if self._owns_browser and self._browser is not None:
            try:
                await self._browser.close()
            except Exception as err:
                logger.debug(f"Error closing browser: {err}")

        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as err:
                logger.debug(f"Error stopping playwright: {err}")
