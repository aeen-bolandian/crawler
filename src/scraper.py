"""Google Maps scraper module for cafes and customer reviews.

Provides CDP tab pool recycling, page type detection, coordinate/name validation,
reviews tab navigation, infinite scroll reviews extraction, and rate limit detection.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
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

from src.exporter import ReviewRecord
from src.resolver import (
    MAX_MATCH_DISTANCE_LOOSE_M,
    MAX_MATCH_DISTANCE_STRICT_M,
    ResolvedTarget,
    extract_coords_from_href,
    extract_coords_from_url,
    haversine_m,
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
    PAGE_LOAD_TIMEOUT_MS: int = 15_000
    POST_GOTO_SETTLE_MS: int = 5_000
    POST_TAB_CLICK_SETTLE_MS: int = 2_000
    POST_MORE_EXPAND_SETTLE_MS: int = 300
    PLACE_PAGE_WAIT_MS: int = 10_000
    REVIEW_CARDS_WAIT_MS: int = 20_000

    # Scrolling parameters
    SCROLL_STEP_PX: int = 3_000
    SCROLL_MAX_ITERATIONS: int = 25
    SCROLL_WAIT_FOR_NEW_CARD_MS: int = 2_500
    SCROLL_MAX_STAGNANT: int = 3

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


GENERIC_STOP_WORDS: frozenset[str] = frozenset(
    {"cafe", "coffee", "kafe", "کافه", "قهوه", "tehran", "تهران"}
)

_TRANSLATION_MAP: dict[str, list[str]] = {
    "عمارت": ["mansion", "emarat"],
    "نیلی": ["nili"],
    "باغ": ["garden", "bagh"],
    "خانه": ["house", "khaneh"],
    "سرای": ["saray", "house"],
    "مجموعه": ["complex"],
    "رستوران": ["restaurant"],
}

_PERSIAN_TO_LATIN: dict[str, str] = {
    "ا": "a",
    "آ": "a",
    "ب": "b",
    "پ": "p",
    "ت": "t",
    "ث": "s",
    "ج": "j",
    "چ": "ch",
    "ح": "h",
    "خ": "kh",
    "د": "d",
    "ذ": "z",
    "ر": "r",
    "ز": "z",
    "ژ": "zh",
    "س": "s",
    "ش": "sh",
    "ص": "s",
    "ض": "z",
    "ط": "t",
    "ظ": "z",
    "ع": "e",
    "غ": "gh",
    "ف": "f",
    "ق": "gh",
    "ک": "k",
    "گ": "g",
    "ل": "l",
    "م": "m",
    "ن": "n",
    "و": "o",
    "ه": "h",
    "ی": "i",
    "ئ": "e",
}


def _normalize_name(name: str) -> str:
    """Normalize Persian and English cafe names for comparison."""
    if not name:
        return ""
    text = name.lower()
    text = text.replace("ي", "ی").replace("ك", "ک").replace("ة", "ه")
    text = text.replace("\u200c", " ")
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def _transliterate_word(word: str) -> str:
    """Transliterate a single Persian word phonetically to Latin."""
    return "".join(_PERSIAN_TO_LATIN.get(c, c) for c in word)


def _latinize_words(text: str) -> set[str]:
    """Convert text words into a set of Latin translations and transliterations."""
    norm = _normalize_name(text)
    words = [w for w in norm.split() if w not in GENERIC_STOP_WORDS]
    results: set[str] = set()
    for w in words:
        if w in _TRANSLATION_MAP:
            results.update(_TRANSLATION_MAP[w])
        trans = _transliterate_word(w)
        if trans:
            results.add(trans)
        results.add(w)
    return results


def _ngram_match(s1: str, s2: str, n: int = 3) -> float:
    """Calculate character n-gram Jaccard similarity between two strings."""
    if not s1 or not s2:
        return 0.0
    if s1 == s2:
        return 1.0
    if len(s1) < n or len(s2) < n:
        return 1.0 if s1 == s2 else 0.0

    ngrams1 = {s1[i : i + n] for i in range(len(s1) - n + 1)}
    ngrams2 = {s2[i : i + n] for i in range(len(s2) - n + 1)}
    intersection = len(ngrams1 & ngrams2)
    union = len(ngrams1 | ngrams2)
    return intersection / union if union > 0 else 0.0


def names_look_same(name1: str, name2: str) -> bool:
    """Determine whether two cafe names refer to the same venue.

    Handles Persian Unicode variations, translation of architectural terms
    (e.g., 'عمارت' -> 'Mansion'), phonetic transliteration, and short-word
    rejection.
    """
    norm1 = _normalize_name(name1)
    norm2 = _normalize_name(name2)
    if not norm1 or not norm2:
        return False

    # Reject short words or names consisting only of generic words
    meaningful1 = [
        w for w in norm1.split() if w not in GENERIC_STOP_WORDS and len(w) >= 3
    ]
    meaningful2 = [
        w for w in norm2.split() if w not in GENERIC_STOP_WORDS and len(w) >= 3
    ]
    if not meaningful1 or not meaningful2:
        return False

    if norm1 == norm2:
        return True

    lat1 = {
        t for t in _latinize_words(name1) if t not in GENERIC_STOP_WORDS and len(t) >= 3
    }
    lat2 = {
        t for t in _latinize_words(name2) if t not in GENERIC_STOP_WORDS and len(t) >= 3
    }

    common = lat1 & lat2
    return bool(common)


class ScraperWorker:
    """Scrapes Google Maps reviews using an assigned or claimed browser tab."""

    def __init__(
        self,
        context: BrowserContext,
        worker_id: int,
        page_pool: asyncio.Queue,
        max_reviews: int = 150,
        config: Optional[ScraperConfig] = None,
    ) -> None:
        """Initialize the scraper worker.

        Args:
            context: Playwright BrowserContext.
            worker_id: Numeric worker identifier.
            page_pool: Queue holding reusable browser tabs.
            max_reviews: Maximum review records to extract per venue.
            config: Scraper configuration parameters and selectors.
        """
        self.context = context
        self.worker_id = worker_id
        self.page_pool = page_pool
        self.max_reviews = max_reviews
        self.config = config or ScraperConfig()
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

    async def _dismiss_consent(self, page: Page) -> None:
        """Dismiss Google consent dialogs if present."""
        for selector in self.config.CONSENT_SELECTORS:
            try:
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=500):
                    await btn.click()
                    break
            except Exception:
                pass

    async def _click_reviews_tab(
        self, page: Page, target: Optional[ResolvedTarget] = None
    ) -> bool:
        """Attempt clicking the reviews tab with timeout and fallback selectors."""
        cafe_id = target.cafe.id if target and target.cafe else ""
        logger.debug(f"[w{self.worker_id}][{cafe_id}] waiting for Reviews tab")

        tab_el = None
        try:
            tab_el = await page.wait_for_selector(
                self.config.SEL_REVIEWS_TAB,
                timeout=10_000,
                state="visible",
            )
        except Exception:
            try:
                tab_el = await page.wait_for_selector(
                    self.config.SEL_REVIEWS_TAB_FALLBACK,
                    timeout=3_000,
                    state="visible",
                )
            except Exception:
                tab_el = None

        if tab_el is None:
            logger.warning(
                f"[w{self.worker_id}][{cafe_id}] Reviews tab not found within 10s"
            )
            return False

        try:
            is_selected = await tab_el.get_attribute("aria-selected") == "true"
            if is_selected:
                logger.debug(
                    f"[w{self.worker_id}][{cafe_id}] Reviews tab already selected"
                )
                return True
        except Exception:
            pass

        logger.debug(
            f"[w{self.worker_id}][{cafe_id}] Reviews tab found, clicking"
        )
        try:
            await tab_el.click(timeout=5_000)
            logger.debug(
                f"[w{self.worker_id}][{cafe_id}] Reviews tab clicked"
            )
            return True
        except Exception as err:
            logger.warning(
                f"[w{self.worker_id}][{cafe_id}] Reviews tab click failed: {err}"
            )
            return False

    async def _wait_for_review_cards(
        self, page: Page, target: Optional[ResolvedTarget] = None
    ) -> bool:
        """Wait for review card elements to be attached in DOM."""
        cafe_id = target.cafe.id if target and target.cafe else ""
        logger.debug(f"[w{self.worker_id}][{cafe_id}] waiting for review cards")
        try:
            await page.wait_for_selector(
                self.config.SEL_REVIEW_ITEM,
                timeout=self.config.REVIEW_CARDS_WAIT_MS,
                state="attached",
            )
            count = await page.locator(self.config.SEL_REVIEW_ITEM).count()
            logger.debug(
                f"[w{self.worker_id}][{cafe_id}] review cards appeared: "
                f"{count} cards"
            )
            return True
        except Exception:
            try:
                await page.wait_for_selector(
                    self.config.SEL_REVIEW_ITEM_FALLBACK,
                    timeout=3_000,
                    state="attached",
                )
                count = await page.locator(
                    self.config.SEL_REVIEW_ITEM_FALLBACK
                ).count()
                logger.debug(
                    f"[w{self.worker_id}][{cafe_id}] review cards appeared: "
                    f"{count} cards"
                )
                return True
            except Exception:
                logger.warning(
                    f"[w{self.worker_id}][{cafe_id}] review cards timeout after "
                    f"{self.config.REVIEW_CARDS_WAIT_MS}ms"
                )
                return False

    async def scrape(self, target: ResolvedTarget) -> list[ReviewRecord]:
        """Scrape reviews for a resolved cafe target.

        Args:
            target: ResolvedTarget with cafe metadata and query URLs.

        Returns:
            List of extracted ReviewRecord items. Empty list if skipped or failed.
        """
        self._last_skip_reason = None
        try:
            page = await self.ensure_page()
        except Exception as err:
            logger.error(f"Worker {self.worker_id}: failed acquiring page: {err}")
            self._last_skip_reason = SkipReason.EXCEPTION
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

    async def _scrape_with_page(
        self, page: Page, target: ResolvedTarget
    ) -> list[ReviewRecord]:
        """Internal scraping routine after obtaining page."""
        try:
            logger.debug(
                f"[w{self.worker_id}][{target.cafe.id}] goto START: {target.url}"
            )
            await page.goto(
                target.url,
                timeout=self.config.PAGE_LOAD_TIMEOUT_MS,
                wait_until="commit",
            )
            logger.debug(
                f"[w{self.worker_id}][{target.cafe.id}] goto DONE: {page.url}"
            )
        except Exception as err:
            logger.warning(
                f"Worker {self.worker_id}: nav failed for {target.url}: {err}"
            )
            self._last_skip_reason = SkipReason.PAGE_NAV_FAILED
            return []

        if "google.com/sorry" in page.url:
            logger.warning(
                f"Worker {self.worker_id}: rate limited on google.com/sorry"
            )
            self._last_skip_reason = SkipReason.PAGE_SIGNAL_NONE
            return []

        await self._dismiss_consent(page)
        logger.debug(
            f"[w{self.worker_id}][{target.cafe.id}] consent dismissed"
        )
        await page.wait_for_timeout(self.config.POST_GOTO_SETTLE_MS)
        logger.debug(
            f"[w{self.worker_id}][{target.cafe.id}] post-goto settle done"
        )

        # Detect whether we are on a single place page or search results list
        is_place = False
        logger.debug(f"[w{self.worker_id}][{target.cafe.id}] checking h1")
        try:
            h1_el = page.locator(self.config.SEL_PLACE_H1).first
            if await h1_el.is_visible(timeout=1000):
                is_place = True
        except Exception:
            pass
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
            self._last_skip_reason = SkipReason.PAGE_SIGNAL_NONE
            return []

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
                        matched_link = link
                        break
                else:
                    text = await link.inner_text()
                    if names_look_same(target.cafe.name, text):
                        matched_link = link
                        break

            if matched_link is None:
                self._last_skip_reason = SkipReason.NO_RESULT_MATCHED
                return []

            try:
                await matched_link.click()
                await page.wait_for_timeout(self.config.POST_GOTO_SETTLE_MS)
            except Exception as err:
                logger.warning(f"Worker {self.worker_id}: click link failed: {err}")
                self._last_skip_reason = SkipReason.PAGE_NAV_FAILED
                return []

        if is_place:
            logger.debug(
                f"[w{self.worker_id}][{target.cafe.id}] validating place match"
            )

        # Validate venue on single place view
        page_coords = extract_coords_from_url(page.url)
        if (
            page_coords
            and target.cafe.has_coordinates
            and target.cafe.latitude is not None
            and target.cafe.longitude is not None
        ):
            dist = haversine_m(
                target.cafe.latitude,
                target.cafe.longitude,
                page_coords[0],
                page_coords[1],
            )
            if dist > MAX_MATCH_DISTANCE_STRICT_M:
                logger.warning(
                    f"Coordinate mismatch for {target.cafe.slug}: "
                    f"{dist:.1f}m > {MAX_MATCH_DISTANCE_STRICT_M}m"
                )
                self._last_skip_reason = SkipReason.PLACE_COORDS_MISMATCH
                return []
        else:
            try:
                h1_text = await page.locator(
                    self.config.SEL_PLACE_H1
                ).first.inner_text(timeout=2000)
                if not names_look_same(target.cafe.name, h1_text):
                    logger.warning(
                        f"Name mismatch for {target.cafe.slug}: "
                        f"'{target.cafe.name}' vs '{h1_text}'"
                    )
                    self._last_skip_reason = SkipReason.PLACE_NAME_MISMATCH
                    return []
            except Exception:
                pass

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
            self._last_skip_reason = SkipReason.REVIEWS_TAB_FAILED
            return []

        await page.wait_for_timeout(self.config.POST_TAB_CLICK_SETTLE_MS)

        # Wait for review cards to appear
        try:
            cards_ready = await asyncio.wait_for(
                self._wait_for_review_cards(page, target), timeout=25.0
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

        cards_loc = page.locator(self.config.SEL_REVIEW_ITEM)
        if await cards_loc.count() == 0:
            cards_loc = page.locator(self.config.SEL_REVIEW_ITEM_FALLBACK)

        # Scroll reviews container
        feed = page.locator(self.config.SEL_FEED).first
        stagnant = 0
        last_count = await cards_loc.count()

        for _ in range(self.config.SCROLL_MAX_ITERATIONS):
            if last_count >= self.max_reviews:
                break
            try:
                await feed.evaluate(
                    "(el, px) => el.scrollTop += px", self.config.SCROLL_STEP_PX
                )
            except Exception:
                try:
                    await page.mouse.wheel(0, self.config.SCROLL_STEP_PX)
                except Exception:
                    pass
            await page.wait_for_timeout(self.config.SCROLL_WAIT_FOR_NEW_CARD_MS)
            new_count = await cards_loc.count()
            if new_count == last_count:
                stagnant += 1
                if stagnant >= self.config.SCROLL_MAX_STAGNANT:
                    break
            else:
                stagnant = 0
                last_count = new_count

        # Expand "More" buttons
        try:
            more_buttons = await page.locator(self.config.SEL_MORE_BUTTON).all()
            for btn in more_buttons:
                try:
                    if await btn.is_visible():
                        await btn.click()
                        await page.wait_for_timeout(
                            self.config.POST_MORE_EXPAND_SETTLE_MS
                        )
                except Exception:
                    pass
        except Exception:
            pass

        # Extract reviews
        cards = await cards_loc.all()
        reviews: list[ReviewRecord] = []
        for card in cards[: self.max_reviews]:
            author = ""
            try:
                author = await card.locator(self.config.SEL_AUTHOR).first.inner_text()
            except Exception:
                pass

            rating = ""
            try:
                r_el = card.locator(self.config.SEL_RATING).first
                rating = await r_el.get_attribute("aria-label") or ""
            except Exception:
                pass

            date = ""
            try:
                date = await card.locator(self.config.SEL_DATE).first.inner_text()
            except Exception:
                pass

            text = ""
            try:
                text = await card.locator(self.config.SEL_TEXT).first.inner_text()
            except Exception:
                pass

            if author or text or rating:
                reviews.append(
                    ReviewRecord(
                        cafe_slug=target.cafe.slug,
                        cafe_name=target.cafe.name,
                        city=target.cafe.city,
                        author=author.strip(),
                        rating=rating.strip(),
                        date=date.strip(),
                        text=text.strip(),
                    )
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
