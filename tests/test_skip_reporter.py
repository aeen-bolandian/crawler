"""Tests for skip reporter module."""

from __future__ import annotations

import asyncio
import csv
from pathlib import Path

import pytest

from src.db_loader import CafeRecord
from src.skip_reporter import SkipReporter


@pytest.fixture
def sample_cafe() -> CafeRecord:
    """Fixture providing a sample CafeRecord for skip logging."""
    return CafeRecord(
        slug="cafe-tarikh",
        name="کافه تاریخ",
        address="خیابان سی تیر",
        city="تهران",
        neighborhood="سی تیر",
        latitude=35.6900,
        longitude=51.4150,
        phone=None,
        id=42,
    )


@pytest.mark.asyncio
async def test_creates_header(tmp_path: Path, sample_cafe: CafeRecord) -> None:
    """Verify that skip CSV file is created with correct header on initial write."""
    skip_file = tmp_path / "skipped.csv"
    reporter = SkipReporter(skip_file)

    await reporter.record(sample_cafe, "no_reviews")
    assert skip_file.exists()

    with open(skip_file, mode="r", encoding="utf-8-sig") as f:
        reader = list(csv.reader(f))
        assert len(reader) == 2
        assert reader[0] == SkipReporter.COLUMNS
        assert reader[1] == ["42", "cafe-tarikh", "کافه تاریخ", "no_reviews"]


@pytest.mark.asyncio
async def test_appends_without_duplicate_header(
    tmp_path: Path, sample_cafe: CafeRecord
) -> None:
    """Verify multiple records do not duplicate the header row."""
    skip_file = tmp_path / "skipped.csv"
    reporter = SkipReporter(skip_file)

    await reporter.record(sample_cafe, "reason_1")
    await reporter.record(sample_cafe, "reason_2")

    with open(skip_file, mode="r", encoding="utf-8-sig") as f:
        reader = list(csv.reader(f))
        assert len(reader) == 3
        assert reader[0] == SkipReporter.COLUMNS
        assert reader[1][3] == "reason_1"
        assert reader[2][3] == "reason_2"


@pytest.mark.asyncio
async def test_records_multiple(tmp_path: Path) -> None:
    """Verify writing 3 records results in exactly 4 lines (1 header + 3 data)."""
    skip_file = tmp_path / "skipped.csv"
    reporter = SkipReporter(skip_file)

    for i in range(3):
        cafe = CafeRecord(
            slug=f"cafe-{i}",
            name=f"Cafe {i}",
            address="Addr",
            city="City",
            neighborhood="Neigh",
            latitude=None,
            longitude=None,
            phone=None,
            id=i,
        )
        await reporter.record(cafe, f"reason-{i}")

    with open(skip_file, mode="r", encoding="utf-8-sig") as f:
        reader = list(csv.reader(f))
        assert len(reader) == 4
        assert reader[0] == SkipReporter.COLUMNS


@pytest.mark.asyncio
async def test_unicode_persian(tmp_path: Path, sample_cafe: CafeRecord) -> None:
    """Verify UTF-8 BOM encoding and Persian text integrity."""
    skip_file = tmp_path / "skipped.csv"
    reporter = SkipReporter(skip_file)

    persian_reason = "عدم انطباق مختصات جغرافیایی"
    await reporter.record(sample_cafe, persian_reason)

    raw_bytes = skip_file.read_bytes()
    assert raw_bytes.startswith(b"\xef\xbb\xbf")

    with open(skip_file, mode="r", encoding="utf-8-sig") as f:
        reader = list(csv.reader(f))
        assert reader[1][2] == "کافه تاریخ"
        assert reader[1][3] == persian_reason


@pytest.mark.asyncio
async def test_load_skipped_slugs(
    tmp_path: Path, sample_cafe: CafeRecord
) -> None:
    """Verify reading existing skipped slugs returns correct set."""
    skip_file = tmp_path / "skipped.csv"
    reporter = SkipReporter(skip_file)

    await reporter.record(sample_cafe, "reason_1")
    other_cafe = CafeRecord(
        slug="cafe-other",
        name="Other Cafe",
        address="Addr",
        city="City",
        neighborhood="Neigh",
        latitude=None,
        longitude=None,
        phone=None,
        id=99,
    )
    await reporter.record(other_cafe, "reason_2")

    loaded_slugs = reporter.load_skipped_slugs()
    assert loaded_slugs == {"cafe-tarikh", "cafe-other"}


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    """Verify loading from a non-existent skip file returns an empty set."""
    skip_file = tmp_path / "non_existent.csv"
    reporter = SkipReporter(skip_file)
    assert reporter.load_skipped_slugs() == set()


@pytest.mark.asyncio
async def test_concurrent_record(tmp_path: Path) -> None:
    """Verify concurrent record calls write all rows cleanly without data loss."""
    skip_file = tmp_path / "concurrent_skips.csv"
    reporter = SkipReporter(skip_file)

    async def log_skip(idx: int) -> None:
        cafe = CafeRecord(
            slug=f"slug-{idx}",
            name=f"Cafe {idx}",
            address="Addr",
            city="City",
            neighborhood="Neigh",
            latitude=None,
            longitude=None,
            phone=None,
            id=idx,
        )
        await reporter.record(cafe, f"reason-{idx}")

    await asyncio.gather(*(log_skip(i) for i in range(10)))

    slugs = reporter.load_skipped_slugs()
    assert len(slugs) == 10
    assert slugs == {f"slug-{i}" for i in range(10)}

    with open(skip_file, mode="r", encoding="utf-8-sig") as f:
        reader = list(csv.reader(f))
        assert len(reader) == 11
        assert reader[0] == SkipReporter.COLUMNS
