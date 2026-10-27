"""Tests for CSV review exporter module."""

from __future__ import annotations

import asyncio
import csv
from pathlib import Path

import pytest

from src.exporter import CSVExporter, ReviewRecord


@pytest.fixture
def sample_review() -> ReviewRecord:
    """Fixture providing a standard ReviewRecord instance."""
    return ReviewRecord(
        cafe_slug="cafe-fereshteh",
        cafe_name="کافه فرشته",
        city="تهران",
        author="مریم احمدی",
        rating="5 stars",
        date="2 weeks ago",
        text="فضای بسیار آرام و قهوه‌های تخصصی با کیفیت عالی.",
    )


@pytest.mark.asyncio
async def test_creates_header_on_first_write(
    tmp_path: Path, sample_review: ReviewRecord
) -> None:
    """Verify that CSV header is created on the initial write."""
    output_file = tmp_path / "reviews.csv"
    exporter = CSVExporter(output_file)

    count = await exporter.append_many([sample_review])
    assert count == 1
    assert output_file.exists()

    with open(output_file, mode="r", encoding="utf-8-sig") as f:
        reader = list(csv.reader(f))
        assert len(reader) == 2
        assert reader[0] == CSVExporter.COLUMNS
        assert reader[1] == sample_review.to_csv_row()


@pytest.mark.asyncio
async def test_appends_without_duplicate_header(
    tmp_path: Path, sample_review: ReviewRecord
) -> None:
    """Verify subsequent appends do not insert duplicate header rows."""
    output_file = tmp_path / "reviews.csv"
    exporter = CSVExporter(output_file)

    await exporter.append_many([sample_review])
    await exporter.append_many([sample_review])

    with open(output_file, mode="r", encoding="utf-8-sig") as f:
        reader = list(csv.reader(f))
        assert len(reader) == 3
        assert reader[0] == CSVExporter.COLUMNS
        assert reader[1] == sample_review.to_csv_row()
        assert reader[2] == sample_review.to_csv_row()


@pytest.mark.asyncio
async def test_appends_multiple_reviews(tmp_path: Path) -> None:
    """Verify writing multiple reviews yields header plus review rows."""
    output_file = tmp_path / "reviews.csv"
    exporter = CSVExporter(output_file)

    reviews = [
        ReviewRecord(
            cafe_slug=f"cafe-{i}",
            cafe_name=f"Cafe {i}",
            city="Tehran",
            author=f"Author {i}",
            rating="4 stars",
            date="1 month ago",
            text=f"Review text {i}",
        )
        for i in range(3)
    ]

    count = await exporter.append_many(reviews)
    assert count == 3

    with open(output_file, mode="r", encoding="utf-8-sig") as f:
        reader = list(csv.reader(f))
        assert len(reader) == 4
        assert reader[0] == CSVExporter.COLUMNS


@pytest.mark.asyncio
async def test_empty_reviews_returns_zero(tmp_path: Path) -> None:
    """Verify that passing empty review list returns 0 without creating file."""
    output_file = tmp_path / "reviews.csv"
    exporter = CSVExporter(output_file)

    count = await exporter.append_many([])
    assert count == 0
    assert not output_file.exists()


@pytest.mark.asyncio
async def test_unicode_persian(
    tmp_path: Path, sample_review: ReviewRecord
) -> None:
    """Verify that Persian text and UTF-8 BOM are preserved in the file."""
    output_file = tmp_path / "reviews.csv"
    exporter = CSVExporter(output_file)

    await exporter.append_many([sample_review])

    raw_bytes = output_file.read_bytes()
    assert raw_bytes.startswith(b"\xef\xbb\xbf")

    with open(output_file, mode="r", encoding="utf-8-sig") as f:
        reader = list(csv.reader(f))
        row = reader[1]
        assert row[1] == "کافه فرشته"
        assert row[3] == "مریم احمدی"
        assert row[6] == "فضای بسیار آرام و قهوه‌های تخصصی با کیفیت عالی."


def test_text_newlines_collapsed() -> None:
    """Verify that newlines within review text are collapsed to single spaces."""
    review = ReviewRecord(
        cafe_slug="cafe-test",
        cafe_name="Cafe Test",
        city="Tehran",
        author="User",
        rating="5 stars",
        date="yesterday",
        text="line1\nline2\r\nline3",
    )
    row = review.to_csv_row()
    assert row[-1] == "line1 line2 line3"


def test_rating_string_preserved() -> None:
    """Verify that rating string format is preserved as-is."""
    review = ReviewRecord(
        cafe_slug="cafe-test",
        cafe_name="Cafe Test",
        city="Tehran",
        author="User",
        rating="5 stars",
        date="yesterday",
        text="Good",
    )
    assert review.to_csv_row()[4] == "5 stars"


@pytest.mark.asyncio
async def test_concurrent_append(tmp_path: Path) -> None:
    """Verify that concurrent appends do not collide or corrupt CSV output."""
    output_file = tmp_path / "concurrent_reviews.csv"
    exporter = CSVExporter(output_file)

    async def worker(worker_id: int) -> int:
        records = [
            ReviewRecord(
                cafe_slug=f"slug-{worker_id}-{i}",
                cafe_name=f"Name {worker_id}",
                city="Tehran",
                author=f"Author {worker_id}",
                rating="4 stars",
                date="today",
                text=f"Text {worker_id}-{i}",
            )
            for i in range(5)
        ]
        return await exporter.append_many(records)

    results = await asyncio.gather(*(worker(i) for i in range(10)))
    assert sum(results) == 50

    with open(output_file, mode="r", encoding="utf-8-sig") as f:
        reader = list(csv.reader(f))
        # 1 header + 50 reviews = 51 lines
        assert len(reader) == 51
        assert reader[0] == CSVExporter.COLUMNS
