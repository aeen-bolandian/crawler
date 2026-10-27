"""SQLite database loader module for cafe records.

Provides read-only access to cafes stored in SQLite database with schema validation,
coordinate validity checks, and optional column fallback.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Iterable, Optional

try:
    from loguru import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CafeRecord:
    """Represents a cafe entity loaded from SQLite database."""

    slug: str
    name: str
    address: str
    city: str
    neighborhood: str
    latitude: Optional[float]
    longitude: Optional[float]
    phone: Optional[str]
    search_address: str = ""
    search_name: str = ""
    search_city: str = ""
    id: Optional[int] = None

    @property
    def has_coordinates(self) -> bool:
        """Return True if both latitude and longitude are valid numeric coordinates.

        Coordinates are considered valid if neither is None, neither is NaN,
        latitude is within [-90.0, 90.0], and longitude is within [-180.0, 180.0].
        """
        if self.latitude is None or self.longitude is None:
            return False
        try:
            lat = float(self.latitude)
            lng = float(self.longitude)
            if math.isnan(lat) or math.isnan(lng):
                return False
            return -90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0
        except (ValueError, TypeError):
            return False


class DatabaseLoader:
    """Context manager for loading cafe records from SQLite in read-only mode."""

    REQUIRED_COLUMNS: frozenset[str] = frozenset(
        {
            "id",
            "slug",
            "name",
            "address",
            "city",
            "neighborhood",
            "latitude",
            "longitude",
            "phone",
        }
    )

    OPTIONAL_COLUMNS: tuple[str, ...] = (
        "search_address",
        "search_name",
        "search_city",
    )

    def __init__(self, db_path: str | Path) -> None:
        """Initialize the database loader with a file path.

        Args:
            db_path: Path to the SQLite database file.
        """
        self.db_path = Path(db_path)
        self._conn: Optional[sqlite3.Connection] = None
        self._available_columns: set[str] = set()

    def __enter__(self) -> DatabaseLoader:
        """Open the database connection in read-only mode and validate schema.

        Returns:
            The initialized DatabaseLoader instance.

        Raises:
            RuntimeError: If file is missing, integrity check fails, or schema invalid.
        """
        if not self.db_path.exists():
            raise RuntimeError(f"Database file does not exist: {self.db_path}")

        resolved_path = self.db_path.resolve().as_posix()
        uri = f"file:{resolved_path}?mode=ro"

        try:
            self._conn = sqlite3.connect(uri, uri=True)
            self._conn.row_factory = sqlite3.Row
        except sqlite3.Error as err:
            raise RuntimeError(
                f"Failed to open database at {self.db_path}: {err}"
            ) from err

        self._validate_integrity()
        self._validate_schema()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object | None,
    ) -> None:
        """Close the database connection safely."""
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error as err:
                logger.warning(f"Error while closing database connection: {err}")
            finally:
                self._conn = None

    def _validate_integrity(self) -> None:
        """Run PRAGMA quick_check to verify SQLite database integrity.

        Raises:
            RuntimeError: If database integrity check fails.
        """
        if self._conn is None:
            raise RuntimeError("Database connection is not open.")

        try:
            cursor = self._conn.execute("PRAGMA quick_check")
            result = cursor.fetchone()
            if not result or result[0] != "ok":
                raise RuntimeError(f"Database integrity check failed: {result}")
        except sqlite3.Error as err:
            raise RuntimeError(f"Integrity check execution failed: {err}") from err

    def _validate_schema(self) -> None:
        """Validate presence of table cafes_cafe and required columns.

        Raises:
            RuntimeError: If table or required columns are missing.
        """
        if self._conn is None:
            raise RuntimeError("Database connection is not open.")

        cursor = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='cafes_cafe'"
        )
        if not cursor.fetchone():
            raise RuntimeError("Table 'cafes_cafe' not found in database.")

        cursor = self._conn.execute("PRAGMA table_info(cafes_cafe)")
        self._available_columns = {row["name"] for row in cursor.fetchall()}

        missing = self.REQUIRED_COLUMNS - self._available_columns
        if missing:
            raise RuntimeError(
                f"Missing required columns in 'cafes_cafe': {sorted(missing)}"
            )

    def _build_select_query(
        self, where_condition: Optional[str] = None
    ) -> tuple[str, list[str]]:
        """Construct SELECT query with required and present optional columns.

        Args:
            where_condition: Optional WHERE clause without the 'WHERE' keyword.

        Returns:
            A tuple of (query_string, selected_columns_list).
        """
        cols = list(self.REQUIRED_COLUMNS)
        cols.sort()
        for opt in self.OPTIONAL_COLUMNS:
            if opt in self._available_columns:
                cols.append(opt)

        query = f"SELECT {', '.join(cols)} FROM cafes_cafe"
        if where_condition:
            query += f" WHERE {where_condition}"
        return query, cols

    def _row_to_record(self, row: sqlite3.Row) -> CafeRecord:
        """Convert a sqlite3.Row into a CafeRecord dataclass instance.

        Args:
            row: SQLite row containing cafe data.

        Returns:
            A populated CafeRecord instance.
        """
        search_addr = (
            str(row["search_address"])
            if "search_address" in self._available_columns
            and row["search_address"] is not None
            else ""
        )
        search_nm = (
            str(row["search_name"])
            if "search_name" in self._available_columns
            and row["search_name"] is not None
            else ""
        )
        search_ct = (
            str(row["search_city"])
            if "search_city" in self._available_columns
            and row["search_city"] is not None
            else ""
        )

        return CafeRecord(
            id=int(row["id"]) if row["id"] is not None else None,
            slug=str(row["slug"]),
            name=str(row["name"]),
            address=str(row["address"]),
            city=str(row["city"]),
            neighborhood=str(row["neighborhood"]),
            latitude=float(row["latitude"]) if row["latitude"] is not None else None,
            longitude=float(row["longitude"]) if row["longitude"] is not None else None,
            phone=str(row["phone"]) if row["phone"] is not None else None,
            search_address=search_addr,
            search_name=search_nm,
            search_city=search_ct,
        )

    def iter_cafes(self, approved_only: bool = True) -> Iterable[CafeRecord]:
        """Yield cafe records ordered by id.

        Args:
            approved_only: Whether to filter only approved cafes.

        Yields:
            CafeRecord instances ordered by id.

        Raises:
            RuntimeError: If connection is closed or query fails.
        """
        if self._conn is None:
            raise RuntimeError("Database connection is not open. Use with-statement.")

        where_cond: Optional[str] = None
        if approved_only and "approval_status" in self._available_columns:
            where_cond = "approval_status = 'approved'"

        query, _ = self._build_select_query(where_condition=where_cond)
        query += " ORDER BY id ASC"

        try:
            cursor = self._conn.execute(query)
            for row in cursor:
                yield self._row_to_record(row)
        except sqlite3.Error as err:
            raise RuntimeError(f"Failed querying cafes: {err}") from err

    def get_by_slug(self, slug: str) -> Optional[CafeRecord]:
        """Fetch a single cafe record by its unique slug.

        Args:
            slug: Unique slug identifier for the cafe.

        Returns:
            The matching CafeRecord if found, otherwise None.

        Raises:
            RuntimeError: If connection is closed or query fails.
        """
        if self._conn is None:
            raise RuntimeError("Database connection is not open. Use with-statement.")

        query, _ = self._build_select_query(where_condition="slug = ?")
        try:
            cursor = self._conn.execute(query, (slug,))
            row = cursor.fetchone()
            if row is None:
                return None
            return self._row_to_record(row)
        except sqlite3.Error as err:
            raise RuntimeError(
                f"Failed fetching cafe with slug '{slug}': {err}"
            ) from err
