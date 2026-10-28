"""CSV exporter module for Google Maps cafe reviews.

Provides an async-safe, append-only CSV streaming exporter using utf-8-sig
encoding to ensure compatibility with Microsoft Excel and proper rendering
of Persian characters.
"""

from __future__ import annotations

import asyncio
import csv
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable

try:
    from loguru import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReviewRecord:
    """Represents an extracted customer review for a cafe."""

    cafe_slug: str
    cafe_name: str
    city: str
    author: str
    rating: str
    date: str
    text: str

    def to_csv_row(self) -> list[str]:
        """Convert record fields into a CSV row with collapsed newlines.

        Returns:
            List of string values corresponding to the CSV columns.
        """
        clean_text = re.sub(r"\s*[\r\n]+\s*", " ", self.text).strip()
        return [
            self.cafe_slug,
            self.cafe_name,
            self.city,
            self.author,
            self.rating,
            self.date,
            clean_text,
        ]


class CSVExporter:
    """Async-safe, append-only CSV exporter with UTF-8 BOM encoding."""

    COLUMNS: list[str] = [
        "cafe_slug",
        "cafe_name",
        "city",
        "author",
        "rating",
        "date",
        "text",
    ]

    def __init__(self, output_path: str | Path) -> None:
        """Initialize the CSV exporter with a destination file path.

        Creates the parent directory if it does not already exist.

        Args:
            output_path: Path to the target CSV file.
        """
        self.output_path = Path(output_path)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._seen: set[tuple[str, str, str, str]] = set()

    async def append_many(self, reviews: Iterable[ReviewRecord]) -> int:
        """Append review records to the CSV file safely under an asyncio lock.

        Only writes the CSV header when the file is freshly created or empty.

        Args:
            reviews: Iterable of ReviewRecord instances.

        Returns:
            Number of review rows successfully written.
        """
        review_list = list(reviews)
        if not review_list:
            return 0

        async with self._lock:
            file_exists = self.output_path.exists()
            is_empty = file_exists and self.output_path.stat().st_size == 0
            write_header = (not file_exists) or is_empty

            with open(
                self.output_path, mode="a", encoding="utf-8-sig", newline=""
            ) as f:
                writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
                if write_header:
                    writer.writerow(self.COLUMNS)
                written = 0
                for review in review_list:
                    key = (
                        review.cafe_slug,
                        review.author,
                        review.date,
                        review.text,
                    )
                    if key in self._seen:
                        continue
                    self._seen.add(key)
                    writer.writerow(review.to_csv_row())
                    written += 1

        logger.debug(
            f"Wrote {written}/{len(review_list)} new rows to {self.output_path}"
        )
        return len(review_list)
