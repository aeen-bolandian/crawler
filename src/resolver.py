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

_RE_PAGE_URL_COORDS = re.compile(r"/@(-?\d+\.\d+),(-?\d+\.\d+)")
_RE_HREF_COORDS = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")

PERSIAN_TO_LATIN: dict[str, str] = {
    "آ": "a",
    "ا": "a",
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
    "ع": "a",
    "غ": "gh",
    "ف": "f",
    "ق": "gh",
    "ک": "k",
    "گ": "g",
    "ل": "l",
    "م": "m",
    "ن": "n",
    "و": "v",
    "ه": "h",
    "ی": "y",
    "ء": "",
    "ئ": "y",
    "أ": "a",
    "إ": "e",
    "ؤ": "o",
    "ة": "h",
}

SPECIAL_CASES: dict[str, set[str]] = {
    "عمارت": {"mansion", "house", "emarat"},
    "mansion": {"عمارت"},
    "house": {"عمارت", "خانه", "سرای"},
    "emarat": {"عمارت"},
    "باقلوا": {"baklava", "baqlava"},
    "baklava": {"باقلوا"},
    "baqlava": {"باقلوا"},
    "لانژ": {"lounge"},
    "lounge": {"لانژ"},
    "کافه": {"cafe", "coffee"},
    "cafe": {"کافه"},
    "coffee": {"کافه", "قهوه"},
    "رستوران": {"restaurant"},
    "restaurant": {"رستوران"},
    "نیلی": {"nili"},
    "باغ": {"garden", "bagh"},
    "خانه": {"house", "khaneh"},
    "سرای": {"saray", "house"},
    "مجموعه": {"complex"},
    "ایتی": {"eighty"},
    "ایت": {"eight"},
    "eighty": {"ایتی"},
    "eight": {"ایت"},
}

GENERIC_NAME_STOP_WORDS: frozenset[str] = frozenset(
    {
        "کافه",
        "cafe",
        "coffee",
        "kafe",
        "رستوران",
        "restaurant",
        "tehran",
        "تهران",
        "قهوه",
    }
)


def _clean_persian_name(text: str) -> str:
    """Normalize Arabic/Persian Unicode variants and strip diacritics."""
    text = text.replace("ي", "ی").replace("ك", "ک").replace("ة", "ه")
    text = text.replace("\u200c", " ")
    text = re.sub(r"[\u064b-\u0652\u0670]", "", text)
    return text


def normalize_venue_name(name: str) -> str:
    """Normalize venue name by lowercasing, removing punctuation and stop words."""
    if not name:
        return ""
    text = _clean_persian_name(name.lower())
    text = re.sub(r"[^\w\s]", " ", text)
    words = [w for w in text.split() if w not in GENERIC_NAME_STOP_WORDS]
    return " ".join(words)


def transliterate_persian_word(word: str) -> set[str]:
    """Transliterate a Persian word to Latin with phonetic vowel variants."""
    clean = _clean_persian_name(word)
    base = "".join(PERSIAN_TO_LATIN.get(c, c) for c in clean)
    if not base:
        return set()
    variants = {base}

    # Initial 'ای' -> 'i' / 'e'
    if clean.startswith("ای"):
        rest = "".join(PERSIAN_TO_LATIN.get(c, c) for c in clean[2:])
        variants.add("i" + rest)
        variants.add("e" + rest)
    if clean.startswith("او"):
        rest = "".join(PERSIAN_TO_LATIN.get(c, c) for c in clean[2:])
        variants.add("u" + rest)
        variants.add("o" + rest)

    # Vowel replacements for 'و' -> u/o and 'ی' -> i/e
    if len(base) > 1:
        for v in ("o", "u"):
            variants.add(base[0] + base[1:].replace("v", v))
        for y in ("i", "e"):
            variants.add(base.replace("y", y))

    # Combined vowel mappings (e.g. 'bvrsa' -> 'bursa', 'hyrvkam' -> 'herokam')
    c1 = base
    if c1.startswith("ay"):
        c1 = "i" + c1[2:]
    variants.add(c1.replace("y", "i").replace("v", "u"))
    variants.add(c1.replace("y", "e").replace("v", "o"))

    # Word ending 'ه' -> 'e' / 'a'
    if clean.endswith("ه") and len(base) > 2:
        variants.add(base[:-1] + "e")
        variants.add(base[:-1] + "a")

    # Add collapsed repeated characters
    collapsed = {re.sub(r"(.)\1+", r"\1", v) for v in variants}
    variants.update(collapsed)
    return variants


def phonetic_normalize(s: str) -> str:
    """Normalize English spelling variations (soft-c, w->v, y->i, etc.)."""
    s = s.lower()
    s = re.sub(r"c([eiy])", r"s\1", s)
    s = s.replace("c", "k")
    s = s.replace("w", "v")
    s = s.replace("y", "i")
    s = s.replace("ee", "i")
    s = s.replace("oo", "u")
    s = s.replace("ou", "u")
    s = s.replace("ck", "k")
    s = s.replace("q", "gh")
    s = re.sub(r"(.)\1+", r"\1", s)
    return s


def consonant_skeleton(word: str) -> str:
    """Extract phonetic consonant skeleton for cross-vowel comparison."""
    w = phonetic_normalize(word)
    if any(c in PERSIAN_TO_LATIN for c in w):
        trans_candidates = transliterate_persian_word(w)
        w = next(iter(trans_candidates))
    w = phonetic_normalize(w)
    res = []
    for i, ch in enumerate(w):
        if ch in "aeiou":
            continue
        if (
            ch == "h"
            and i == len(w) - 1
            and (i == 0 or w[i - 1] not in "sckgz")
        ):
            continue
        if ch.isalpha():
            res.append(ch)
    skeleton = "".join(res)
    return re.sub(r"(.)\1+", r"\1", skeleton)


def _words_match(w1: str, w2: str) -> bool:
    """Check if two words match via exact, substring (min 4), or prefix/suffix (3)."""
    if w1 == w2:
        return True
    c1 = re.sub(r"(.)\1+", r"\1", w1)
    c2 = re.sub(r"(.)\1+", r"\1", w2)
    if c1 == c2:
        return True
    if len(w1) >= 4 and len(w2) >= 4 and (w1 in w2 or w2 in w1):
        return True
    if len(c1) >= 4 and len(c2) >= 4 and (c1 in c2 or c2 in c1):
        return True
    if len(w1) >= 3 and len(w2) >= 3:
        if (
            w2.startswith(w1)
            or w2.endswith(w1)
            or w1.startswith(w2)
            or w1.endswith(w2)
        ):
            return True
    if len(c1) >= 3 and len(c2) >= 3:
        if (
            c2.startswith(c1)
            or c2.endswith(c1)
            or c1.startswith(c2)
            or c1.endswith(c2)
        ):
            return True
    return False


def _has_substr_overlap(s1: str, s2: str, min_len: int = 4) -> bool:
    """Check if s1 and s2 share a common substring of at least min_len characters."""
    if len(s1) < min_len or len(s2) < min_len:
        return False
    if s1 in s2 or s2 in s1:
        return True
    c1 = re.sub(r"(.)\1+", r"\1", s1)
    c2 = re.sub(r"(.)\1+", r"\1", s2)
    if c1 in c2 or c2 in c1:
        return True
    for length in range(min(len(s1), len(s2)), min_len - 1, -1):
        for start in range(len(s1) - length + 1):
            sub = s1[start : start + length]
            if sub in s2:
                return True
    return False


def names_look_same(name1: str, name2: str) -> bool:
    """Determine whether two cafe names refer to the same venue.

    Handles Persian Unicode normalization, transliteration (character and
    phonetic vowel variants), special architectural/culinary terms,
    consonant skeletons, and multi-word compounds.
    """
    norm1 = normalize_venue_name(name1)
    norm2 = normalize_venue_name(name2)
    if not norm1 or not norm2:
        return False

    if len(norm1) < 3 or len(norm2) < 3:
        return False

    # Level 1: Exact normalized match
    if norm1 == norm2:
        return True

    words1 = [w for w in norm1.split() if len(w) >= 2]
    words2 = [w for w in norm2.split() if len(w) >= 2]
    if not words1 or not words2:
        return False

    # Level 2: Special-case match
    for w1 in words1:
        if w1 in SPECIAL_CASES:
            for w2 in words2:
                if w2 in SPECIAL_CASES[w1]:
                    return True
    for w2 in words2:
        if w2 in SPECIAL_CASES:
            for w1 in words1:
                if w1 in SPECIAL_CASES[w2]:
                    return True

    # Level 3: Substring match with min 4 chars
    for w1 in words1:
        for w2 in words2:
            if len(w1) >= 4 and len(w2) >= 4 and (w1 in w2 or w2 in w1):
                return True

    # Level 4: Word-level transliteration overlap
    trans_sets1 = [transliterate_persian_word(w) for w in words1]
    trans_sets2 = [transliterate_persian_word(w) for w in words2]

    for t_set in trans_sets1:
        for t1 in t_set:
            for w2 in words2:
                if _words_match(t1, w2):
                    return True
            for p2 in [phonetic_normalize(w) for w in words2]:
                if _words_match(phonetic_normalize(t1), p2):
                    return True

    for t_set in trans_sets2:
        for t2 in t_set:
            for w1 in words1:
                if _words_match(t2, w1):
                    return True
            for p1 in [phonetic_normalize(w) for w in words1]:
                if _words_match(phonetic_normalize(t2), p1):
                    return True

    for t_set1 in trans_sets1:
        for t1 in t_set1:
            for t_set2 in trans_sets2:
                for t2 in t_set2:
                    if _words_match(t1, t2):
                        return True

    # Level 5: Consonant skeletons
    skel1 = [consonant_skeleton(w) for w in words1]
    skel2 = [consonant_skeleton(w) for w in words2]

    for s1 in skel1:
        if len(s1) >= 3:
            for s2 in skel2:
                if len(s2) >= 3 and _words_match(s1, s2):
                    return True

    joined1_skel = consonant_skeleton("".join(words1))
    joined2_skel = consonant_skeleton("".join(words2))
    if len(joined1_skel) >= 3 and len(joined2_skel) >= 3:
        if _words_match(joined1_skel, joined2_skel):
            return True

    joined_trans1 = transliterate_persian_word("".join(words1))
    joined_trans2 = transliterate_persian_word("".join(words2))
    for jt1 in joined_trans1:
        for jt2 in joined_trans2:
            if _has_substr_overlap(jt1, jt2, min_len=4):
                return True
        for w2 in words2:
            if _has_substr_overlap(jt1, w2, min_len=4):
                return True
    for jt2 in joined_trans2:
        for w1 in words1:
            if _has_substr_overlap(jt2, w1, min_len=4):
                return True

    return False


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


# Aliases for scraper compatibility
haversine_m = haversine_distance
extract_coords_from_url = extract_coordinates_from_url
extract_coords_from_href = extract_coordinates_from_href


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
