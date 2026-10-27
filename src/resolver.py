"""Google Maps location resolver module.

Constructs prioritized Google Maps search URLs from CafeRecord data,
normalizes and cleans Persian address tokens, and provides coordinate
validation utilities.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import re
import urllib.parse

try:
    from loguru import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)

from src.db_loader import CafeRecord

# Coordinate match distance thresholds (in meters)
MAX_MATCH_DISTANCE_STRICT_M: float = 500.0
MAX_MATCH_DISTANCE_LOOSE_M: float = 800.0

# Regex patterns for coordinate extraction
_RE_PAGE_URL_COORDS = re.compile(r"/@(-?\d+\.\d+),(-?\d+\.\d+)")
_RE_HREF_COORDS = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")


class ResolverStrategy(str, Enum):
    """Strategy used to resolve a Google Maps search target."""

    DIRECT_SEARCH = "direct_search"
    PHONE_SEARCH = "phone_search"
    TEXT_SEARCH = "text_search"


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    """Target query and generated URLs for Google Maps scraping."""

    cafe: CafeRecord
    url: str
    strategy: ResolverStrategy
    fallback_urls: tuple[str, ...] = ()


def haversine_distance(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Calculate the great-circle distance between two coordinates in meters.

    Args:
        lat1: Latitude of the first point.
        lon1: Longitude of the first point.
        lat2: Latitude of the second point.
        lon2: Longitude of the second point.

    Returns:
        Distance in meters.
    """
    earth_radius_m = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return earth_radius_m * c


def extract_coordinates_from_url(url: str) -> tuple[float, float] | None:
    """Extract (lat, lng) from a Google Maps page URL matching /@lat,lng.

    Args:
        url: Full URL string.

    Returns:
        (latitude, longitude) as float tuple if matched, else None.
    """
    match = _RE_PAGE_URL_COORDS.search(url)
    if not match:
        return None
    try:
        return float(match.group(1)), float(match.group(2))
    except (ValueError, TypeError):
        return None


def extract_coordinates_from_href(href: str) -> tuple[float, float] | None:
    """Extract (lat, lng) from place href data parameters (!3dlat!4dlng).

    Args:
        href: Element href attribute.

    Returns:
        (latitude, longitude) as float tuple if matched, else None.
    """
    match = _RE_HREF_COORDS.search(href)
    if not match:
        return None
    try:
        return float(match.group(1)), float(match.group(2))
    except (ValueError, TypeError):
        return None


def _short_address(search_address: str, keep_tail: int = 3) -> str:
    """Normalize and shorten a Persian address to its most meaningful components.

    Splits on both Persian (،) and Arabic (,) commas, removes administrative
    noise tokens such as 'ایران', 'تهران', 'شهر تهران', 'استان تهران', strips
    glued city suffixes, removes district labels like 'منطقه N شهر تهران', and
    collapses 'محله X' to 'X'.

    Args:
        search_address: The raw address string.
        keep_tail: Maximum number of trailing parts to retain.

    Returns:
        Shortened address parts joined by space.
    """
    if not search_address or not search_address.strip():
        return ""

    # Normalize Persian and ASCII/Arabic commas
    normalized = search_address.replace("،", ",")
    raw_parts = [p.strip() for p in normalized.split(",") if p.strip()]

    noise_exact = {"ایران", "تهران", "شهر تهران", "استان تهران", "شهر", "استان"}
    meaningful_parts: list[str] = []

    for part in raw_parts:
        if part in noise_exact:
            continue

        # Remove 'شهر X' / 'استان X' if X contains 'تهران'
        if re.match(r"^(شهر|استان)\s+.*تهران.*$", part):
            continue

        # Collapse 'منطقه N شهر تهران' or 'منطقه N'
        if re.match(r"^منطقه\s+[\d\u06f0-\u06f9]+(\s+شهر\s+تهران|\s+تهران)?$", part):
            continue

        # Strip 'محله X' -> keeps 'X'
        part = re.sub(r"^محله\s+", "", part).strip()

        # Strip glued 'تهران' at the end of word (e.g., 'بلوار هوانیروزتهران')
        if part.endswith("تهران") and part != "تهران":
            part = re.sub(r"\s*تهران$", "", part).strip()

        if not part or part in noise_exact:
            continue

        meaningful_parts.append(part)

    if not meaningful_parts:
        return ""

    selected = meaningful_parts[-keep_tail:]
    return " ".join(selected)


class LocationResolver:
    """Builds and resolves Google Maps search URLs from cafe records."""

    def __init__(self, zoom: int = 17, lang: str = "en") -> None:
        """Initialize the resolver with map display parameters.

        Args:
            zoom: Google Maps zoom level (default 17).
            lang: Google Maps UI language code (default 'en').
        """
        self.zoom = zoom
        self.lang = lang

    def _build_url(self, query: str, cafe: CafeRecord) -> str:
        """Construct a Google Maps web search URL with optional center/zoom.

        Args:
            query: The unencoded search query string.
            cafe: CafeRecord containing optional coordinates.

        Returns:
            Fully-formed URL string.
        """
        base_url = "https://www.google.com/maps/search/?api=1"
        encoded_query = urllib.parse.quote_plus(query)
        url = f"{base_url}&query={encoded_query}&hl={self.lang}"

        if (
            cafe.has_coordinates
            and cafe.latitude is not None
            and cafe.longitude is not None
        ):
            url += f"&center={cafe.latitude},{cafe.longitude}&zoom={self.zoom}"

        return url

    def resolve(self, cafe: CafeRecord) -> ResolvedTarget:
        """Resolve a CafeRecord into a primary search URL and fallbacks.

        Args:
            cafe: The cafe entity to resolve.

        Returns:
            ResolvedTarget containing the primary query URL and fallback URLs.
        """
        search_name = (cafe.search_name or "").strip()
        cafe_name = search_name or cafe.name.strip()
        short_addr = _short_address(cafe.search_address)

        fallback_urls: list[str] = []

        if short_addr:
            primary_query = f"{cafe_name} {short_addr}".strip()
            # Fallback A: name + neighborhood + city
            fb_a_query = (
                f"{cafe.name.strip()} {cafe.neighborhood.strip()} "
                f"{cafe.city.strip()}"
            ).strip()
            fallback_urls.append(self._build_url(fb_a_query, cafe))
        else:
            primary_query = (
                f"{cafe.name.strip()} {cafe.neighborhood.strip()} "
                f"{cafe.city.strip()}"
            ).strip()

        # Fallback B: bare name only
        fb_b_query = cafe_name
        fallback_urls.append(self._build_url(fb_b_query, cafe))

        primary_url = self._build_url(primary_query, cafe)

        logger.debug(
            f"Resolved cafe '{cafe.slug}': primary='{primary_query}', "
            f"fallbacks={len(fallback_urls)}"
        )

        return ResolvedTarget(
            cafe=cafe,
            url=primary_url,
            strategy=ResolverStrategy.TEXT_SEARCH,
            fallback_urls=tuple(fallback_urls),
        )
