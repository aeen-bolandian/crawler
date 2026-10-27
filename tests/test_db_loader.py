"""Tests for SQLite cafe database loader module."""

from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Generator

import pytest

from src.db_loader import CafeRecord, DatabaseLoader


@pytest.fixture
def tmp_db_with_optional(tmp_path: Path) -> Generator[Path, None, None]:
    """Create a temporary SQLite database containing optional search columns."""
    db_file = tmp_path / "cafes_with_opt.db"
    conn = sqlite3.connect(db_file)
    conn.execute(
        """
        CREATE TABLE cafes_cafe (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            address TEXT NOT NULL,
            city TEXT NOT NULL,
            neighborhood TEXT NOT NULL,
            latitude REAL,
            longitude REAL,
            phone TEXT,
            approval_status TEXT NOT NULL,
            search_address TEXT,
            search_name TEXT,
            search_city TEXT
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO cafes_cafe (
            slug, name, address, city, neighborhood, latitude, longitude,
            phone, approval_status, search_address, search_name, search_city
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "cafe-tehran",
                "Cafe Tehran",
                "Valiasr St",
                "Tehran",
                "Valiasr",
                35.7001,
                51.4001,
                "+982188888888",
                "approved",
                "Valiasr St, Tehran",
                "Cafe Tehran",
                "Tehran",
            ),
            (
                "cafe-shiraz",
                "Cafe Shiraz",
                "Zand Blvd",
                "Shiraz",
                "Zand",
                29.6100,
                52.5400,
                "+987133333333",
                "pending",
                "Zand Blvd, Shiraz",
                "Cafe Shiraz",
                "Shiraz",
            ),
            (
                "cafe-isfahan",
                "Cafe Isfahan",
                "Chaharbagh St",
                "Isfahan",
                "Chaharbagh",
                32.6500,
                51.6600,
                "+983132222222",
                "approved",
                "Chaharbagh St, Isfahan",
                "Cafe Isfahan",
                "Isfahan",
            ),
        ],
    )
    conn.commit()
    conn.close()
    yield db_file


@pytest.fixture
def tmp_db_without_optional(tmp_path: Path) -> Generator[Path, None, None]:
    """Create a temporary SQLite database without optional search columns."""
    db_file = tmp_path / "cafes_without_opt.db"
    conn = sqlite3.connect(db_file)
    conn.execute(
        """
        CREATE TABLE cafes_cafe (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            address TEXT NOT NULL,
            city TEXT NOT NULL,
            neighborhood TEXT NOT NULL,
            latitude REAL,
            longitude REAL,
            phone TEXT,
            approval_status TEXT NOT NULL
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO cafes_cafe (
            slug, name, address, city, neighborhood, latitude, longitude,
            phone, approval_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "cafe-tabriz",
                "Cafe Tabriz",
                "Saat Sq",
                "Tabriz",
                "Saat",
                38.0700,
                46.2900,
                "+984135555555",
                "approved",
            ),
            (
                "cafe-yazd",
                "Cafe Yazd",
                "Amir Chakhmaq",
                "Yazd",
                "Center",
                None,
                None,
                None,
                "rejected",
            ),
        ],
    )
    conn.commit()
    conn.close()
    yield db_file


def test_load_approved_only(tmp_db_with_optional: Path) -> None:
    """Verify that only approval_status='approved' records are yielded by default."""
    with DatabaseLoader(tmp_db_with_optional) as loader:
        cafes = list(loader.iter_cafes(approved_only=True))
        assert len(cafes) == 2
        assert [c.slug for c in cafes] == ["cafe-tehran", "cafe-isfahan"]
        for cafe in cafes:
            assert isinstance(cafe, CafeRecord)


def test_load_all_cafes(tmp_db_with_optional: Path) -> None:
    """Verify that when approved_only=False, all cafe records are yielded."""
    with DatabaseLoader(tmp_db_with_optional) as loader:
        cafes = list(loader.iter_cafes(approved_only=False))
        assert len(cafes) == 3
        assert [c.slug for c in cafes] == [
            "cafe-tehran",
            "cafe-shiraz",
            "cafe-isfahan",
        ]


def test_optional_columns_present(tmp_db_with_optional: Path) -> None:
    """Verify search_address and other optional columns are populated when present."""
    with DatabaseLoader(tmp_db_with_optional) as loader:
        cafe = loader.get_by_slug("cafe-tehran")
        assert cafe is not None
        assert cafe.search_address == "Valiasr St, Tehran"
        assert cafe.search_name == "Cafe Tehran"
        assert cafe.search_city == "Tehran"


def test_optional_columns_absent(tmp_db_without_optional: Path) -> None:
    """Verify search_address defaults to empty string when columns are absent."""
    with DatabaseLoader(tmp_db_without_optional) as loader:
        cafe = loader.get_by_slug("cafe-tabriz")
        assert cafe is not None
        assert cafe.search_address == ""
        assert cafe.search_name == ""
        assert cafe.search_city == ""


@pytest.mark.parametrize(
    ("lat", "lng", "expected"),
    [
        (35.6892, 51.3890, True),
        (-90.0, 180.0, True),
        (90.0, -180.0, True),
        (0.0, 0.0, True),
        (None, 51.3890, False),
        (35.6892, None, False),
        (None, None, False),
        (90.1, 51.0, False),
        (-91.0, 51.0, False),
        (35.0, 181.0, False),
        (35.0, -181.0, False),
        (float("nan"), 51.0, False),
    ],
)
def test_has_coordinates(
    lat: float | None, lng: float | None, expected: bool
) -> None:
    """Test CafeRecord.has_coordinates property across valid and invalid values."""
    record = CafeRecord(
        slug="test-slug",
        name="Test Cafe",
        address="Test Address",
        city="Test City",
        neighborhood="Test Neighborhood",
        latitude=lat,
        longitude=lng,
        phone=None,
    )
    assert record.has_coordinates is expected


def test_get_by_slug(tmp_db_with_optional: Path) -> None:
    """Test get_by_slug with existing and non-existing slugs."""
    with DatabaseLoader(tmp_db_with_optional) as loader:
        found = loader.get_by_slug("cafe-isfahan")
        assert found is not None
        assert found.name == "Cafe Isfahan"
        assert found.city == "Isfahan"

        not_found = loader.get_by_slug("non-existent-slug")
        assert not_found is None


def test_missing_required_columns(tmp_path: Path) -> None:
    """Test that schema validation raises RuntimeError if required columns missing."""
    db_file = tmp_path / "broken.db"
    conn = sqlite3.connect(db_file)
    conn.execute(
        """
        CREATE TABLE cafes_cafe (
            id INTEGER PRIMARY KEY,
            slug TEXT NOT NULL,
            name TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="Missing required columns"):
        with DatabaseLoader(db_file):
            pass


def test_readonly(tmp_db_with_optional: Path) -> None:
    """Verify that attempting to write to the database raises OperationalError."""
    with DatabaseLoader(tmp_db_with_optional) as loader:
        assert loader._conn is not None
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            loader._conn.execute(
                "UPDATE cafes_cafe SET name = 'Hacked' WHERE slug = 'cafe-tehran'"
            )
