"""Skip reporter module for logging skipped or un-scraped cafes.

Records skipped cafes and reasons to a UTF-8 BOM CSV file for post-analysis
and enables re-loading of skipped slugs for retry runs.
"""

from __future__ import annotations

import asyncio
import csv
from pathlib import Path

try:
    from loguru import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)

from src.db_loader import CafeRecord


class SkipReporter:
    """Records skipped cafes to an append-only CSV file with UTF-8 BOM."""

    COLUMNS: list[str] = ["cafe_id", "cafe_slug", "cafe_name", "reason"]

    def __init__(self, path: str | Path = "data/skipped_cafes.csv") -> None:
        """Initialize the skip reporter with an output file path.

        Creates the parent directory if it does not exist.

        Args:
            path: Destination path for the skipped cafes CSV.
        """
        self.path = Path(path)
        if self.path.parent:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    async def record(self, cafe: CafeRecord, reason: str) -> None:
        """Record a skipped cafe with the given reason string.

        Args:
            cafe: CafeRecord instance that was skipped.
            reason: Text reason or SkipReason code explaining the skip.
        """
        async with self._lock:
            file_exists = self.path.exists()
            is_empty = file_exists and self.path.stat().st_size == 0
            write_header = (not file_exists) or is_empty

            row = [
                str(cafe.id) if cafe.id is not None else "",
                cafe.slug,
                cafe.name,
                reason,
            ]

            with open(self.path, mode="a", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                if write_header:
                    writer.writerow(self.COLUMNS)
                writer.writerow(row)

        logger.debug(f"Recorded skip for '{cafe.slug}': {reason}")

    def load_skipped_slugs(self) -> set[str]:
        """Read all previously recorded skipped slugs from the CSV file.

        Returns:
            Set of unique cafe slugs recorded in the skip file.
        """
        if not self.path.exists():
            return set()

        skipped: set[str] = set()
        try:
            with open(self.path, mode="r", encoding="utf-8-sig") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                if not header:
                    return set()

                slug_idx = 1
                if "cafe_slug" in header:
                    slug_idx = header.index("cafe_slug")

                for row in reader:
                    if len(row) > slug_idx and row[slug_idx].strip():
                        skipped.add(row[slug_idx].strip())
        except OSError as err:
            logger.warning(
                f"Error reading skipped cafes file {self.path}: {err}"
            )

        return skipped
