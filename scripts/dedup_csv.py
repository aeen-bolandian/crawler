#!/usr/bin/env python3
"""Remove duplicate rows from review CSVs."""

from __future__ import annotations

import csv
from pathlib import Path
import sys


def dedup_file(path: Path) -> tuple[int, int]:
    """Deduplicate rows in a review CSV file in-place preserving order.

    Args:
        path: Path to the target CSV file.

    Returns:
        Tuple of (row_count_before, row_count_after).
    """
    with path.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        columns = reader.fieldnames or []
    seen: set[tuple[str, str, str, str]] = set()
    unique: list[dict[str, str]] = []
    for row in rows:
        key = (
            row.get("cafe_slug", ""),
            row.get("author", ""),
            row.get("date", ""),
            row.get("text", ""),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    if len(unique) < len(rows):
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(
                f, fieldnames=columns, quoting=csv.QUOTE_MINIMAL
            )
            writer.writeheader()
            writer.writerows(unique)
    return len(rows), len(unique)


def main() -> None:
    """CLI entrypoint for CSV deduplication."""
    for p in sys.argv[1:]:
        path = Path(p)
        if not path.exists():
            print(f"missing: {path}")
            continue
        before, after = dedup_file(path)
        tag = "clean" if before == after else "fixed"
        print(f"{tag} {path}: {before} → {after}")


if __name__ == "__main__":
    main()
