"""Tests for Google Maps location resolver module."""

from __future__ import annotations

import urllib.parse

import pytest

from src.db_loader import CafeRecord
from src.resolver import (
    MAX_MATCH_DISTANCE_LOOSE_M,
    MAX_MATCH_DISTANCE_STRICT_M,
    LocationResolver,
    ResolverStrategy,
    _short_address,
    extract_coordinates_from_href,
    extract_coordinates_from_url,
    haversine_distance,
)


@pytest.fixture
def sample_cafe_with_coords() -> CafeRecord:
    """Fixture providing CafeRecord with valid coordinates and search fields."""
    return CafeRecord(
        slug="cafe-neda",
        name="کافه ندا",
        address="خیابان ولیعصر، بالاتر از میدان ونک، کوچه نگار",
        city="تهران",
        neighborhood="ونک",
        latitude=35.7592,
        longitude=51.4089,
        phone="+982188776655",
        search_address="ایران، استان تهران، شهر تهران، خیابان ولیعصر، کوچه نگار",
        search_name="کافه ندا ونک",
        search_city="تهران",
        id=10,
    )


@pytest.fixture
def sample_cafe_without_coords() -> CafeRecord:
    """Fixture providing CafeRecord with no coordinates and empty search fields."""
    return CafeRecord(
        slug="cafe-bahar",
        name="کافه بهار",
        address="خیابان آزادی، خیابان حبیب‌الهی",
        city="تهران",
        neighborhood="طرشت",
        latitude=None,
        longitude=None,
        phone=None,
        search_address="",
        search_name="",
        search_city="",
        id=20,
    )


def test_short_address_strips_noise() -> None:
    """Verify removal of administrative noise tokens like ایران, تهران, and شهر."""
    addr = "ایران، استان تهران، شهر تهران، خیابان ولیعصر"
    result = _short_address(addr)
    assert result == "خیابان ولیعصر"
    assert "ایران" not in result
    assert "استان تهران" not in result
    assert "شهر تهران" not in result


def test_short_address_strips_محله() -> None:
    """Verify that 'محله X' collapses to 'X'."""
    addr = "محله زعفرانیه، خیابان آصف"
    result = _short_address(addr)
    assert result == "زعفرانیه خیابان آصف"


def test_short_address_handles_glued_city() -> None:
    """Verify that glued 'تهران' at the end of address parts is safely removed."""
    addr = "بلوار هوانیروزتهران، خیابان گلها"
    result = _short_address(addr)
    assert result == "بلوار هوانیروز خیابان گلها"


def test_short_address_empty() -> None:
    """Verify that empty or whitespace-only addresses return an empty string."""
    assert _short_address("") == ""
    assert _short_address("   ") == ""
    assert _short_address("،،،") == ""


def test_short_address_keeps_tail() -> None:
    """Verify that only the last `keep_tail` meaningful parts are retained."""
    addr = "بخش یک، بخش دو، بخش سه، بخش چهار، بخش پنج"
    result = _short_address(addr, keep_tail=3)
    assert result == "بخش سه بخش چهار بخش پنج"


def test_resolve_with_search_address(sample_cafe_with_coords: CafeRecord) -> None:
    """Verify that primary query uses search_name and shortened search_address."""
    resolver = LocationResolver()
    target = resolver.resolve(sample_cafe_with_coords)

    assert target.cafe == sample_cafe_with_coords
    assert target.strategy == ResolverStrategy.TEXT_SEARCH
    encoded_name = urllib.parse.quote_plus("کافه ندا ونک")
    assert f"query={encoded_name}" in target.url
    assert urllib.parse.quote_plus("کوچه نگار") in target.url
    assert "کوچه نگار" in urllib.parse.unquote_plus(target.url)


def test_resolve_without_search_address(
    sample_cafe_without_coords: CafeRecord,
) -> None:
    """Verify resolver falls back to name+neighborhood+city without search_address."""
    resolver = LocationResolver()
    target = resolver.resolve(sample_cafe_without_coords)

    unquoted = urllib.parse.unquote_plus(target.url)
    assert "کافه بهار طرشت تهران" in unquoted


def test_resolve_generates_fallbacks(sample_cafe_with_coords: CafeRecord) -> None:
    """Verify that resolver generates fallback queries including bare name."""
    resolver = LocationResolver()
    target = resolver.resolve(sample_cafe_with_coords)

    assert len(target.fallback_urls) == 2
    # Fallback A: name + neighborhood + city
    unquoted_fb_a = urllib.parse.unquote_plus(target.fallback_urls[0])
    assert "کافه ندا ونک تهران" in unquoted_fb_a

    # Fallback B: bare name only
    unquoted_fb_b = urllib.parse.unquote_plus(target.fallback_urls[1])
    assert "کافه ندا ونک" in unquoted_fb_b


def test_resolve_appends_coordinates(sample_cafe_with_coords: CafeRecord) -> None:
    """Verify that center and zoom params are appended when coordinates present."""
    resolver = LocationResolver(zoom=16)
    target = resolver.resolve(sample_cafe_with_coords)

    assert "&center=35.7592,51.4089&zoom=16" in target.url


def test_resolve_omits_coordinates(sample_cafe_without_coords: CafeRecord) -> None:
    """Verify that center and zoom params are omitted when coordinates are absent."""
    resolver = LocationResolver()
    target = resolver.resolve(sample_cafe_without_coords)

    assert "center=" not in target.url
    assert "zoom=" not in target.url


def test_resolve_encodes_persian(sample_cafe_with_coords: CafeRecord) -> None:
    """Verify that Persian text in queries is properly percent-encoded for HTTP."""
    resolver = LocationResolver()
    target = resolver.resolve(sample_cafe_with_coords)

    assert "%" in target.url
    assert "hl=en" in target.url
    assert target.url.startswith("https://www.google.com/maps/search/?api=1")


def test_haversine_distance() -> None:
    """Test haversine formula with known coordinates."""
    # Distance between Azadi Tower and Milad Tower in Tehran is ~6.1 km
    lat_azadi, lon_azadi = 35.6997, 51.3380
    lat_milad, lon_milad = 35.7448, 51.3753
    dist = haversine_distance(lat_azadi, lon_azadi, lat_milad, lon_milad)
    assert 6000 <= dist <= 6300

    # Same location should be 0 distance
    assert haversine_distance(35.0, 51.0, 35.0, 51.0) == 0.0


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://www.google.com/maps/@35.7592,51.4089,17z?entry=ttu",
            (35.7592, 51.4089),
        ),
        (
            "https://www.google.com/maps/place/Cafe/@-33.8688,151.2093,15z",
            (-33.8688, 151.2093),
        ),
        ("https://www.google.com/maps/search/cafe?hl=en", None),
    ],
)
def test_extract_coordinates_from_url(
    url: str, expected: tuple[float, float] | None
) -> None:
    """Test regex extraction of coordinates from Google Maps page URLs."""
    assert extract_coordinates_from_url(url) == expected


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        (
            "/maps/place/Cafe/data=!4m2!3m1!1s0x0:0x0!3d35.7592!4d51.4089",
            (35.7592, 51.4089),
        ),
        (
            "/maps/place/Sydney/data=!3d-33.8688!4d151.2093?hl=en",
            (-33.8688, 151.2093),
        ),
        ("/maps/place/Cafe/data=!4m2!3m1", None),
    ],
)
def test_extract_coordinates_from_href(
    href: str, expected: tuple[float, float] | None
) -> None:
    """Test regex extraction of coordinates from Google Maps result hrefs."""
    assert extract_coordinates_from_href(href) == expected


def test_distance_constants() -> None:
    """Verify strict and loose match distance thresholds."""
    assert MAX_MATCH_DISTANCE_STRICT_M == 500.0
    assert MAX_MATCH_DISTANCE_LOOSE_M == 800.0
