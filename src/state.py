"""State management module for crawler checkpointing.

Tracks processed cafe slugs and provides atomic, batched persistence to disk
via temporary file replacement to survive process interrupts.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Optional

try:
    from loguru import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)


class StateManager:
    """Manages persistent checkpointing of processed cafe slugs."""

    def __init__(
        self,
        state_path: str | Path = "crawler_state.json",
        flush_every: int = 10,
    ) -> None:
        """Initialize the state manager and load any existing checkpoints.

        Args:
            state_path: Path to the JSON state file.
            flush_every: Number of additions before triggering a disk flush.
        """
        self.state_path = Path(state_path)
        self.flush_every = max(1, flush_every)
        self.processed: set[str] = set()
        self._dirty: set[str] = set()
        self._lock = asyncio.Lock()

        self._load_state()

    def _load_state(self) -> None:
        """Load state from JSON file or initialize an empty state gracefully."""
        if not self.state_path.exists():
            logger.debug(
                f"No existing state file at {self.state_path}, starting fresh."
            )
            return

        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if (
                    isinstance(data, dict)
                    and "processed_slugs" in data
                    and isinstance(data["processed_slugs"], list)
                ):
                    self.processed = set(data["processed_slugs"])
                    logger.info(
                        f"Loaded {len(self.processed)} processed slugs from "
                        f"{self.state_path}"
                    )
                else:
                    logger.warning(
                        f"Malformed state structure in {self.state_path}. "
                        "Starting with an empty state."
                    )
        except (json.JSONDecodeError, OSError) as err:
            logger.warning(
                f"Failed to read state file at {self.state_path} ({err}). "
                "Starting with an empty state."
            )

    def is_processed(self, slug: str) -> bool:
        """Check whether a cafe slug has already been processed.

        Args:
            slug: Unique slug identifier of the cafe.

        Returns:
            True if slug is recorded as processed, else False.
        """
        return slug in self.processed

    async def mark_processed(self, slug: str) -> None:
        """Record a slug as processed, flushing to disk if threshold reached.

        Args:
            slug: Cafe slug to mark as completed.
        """
        async with self._lock:
            if slug not in self.processed:
                self.processed.add(slug)
                self._dirty.add(slug)

            if len(self._dirty) >= self.flush_every:
                self._flush_locked()

    async def flush(self) -> None:
        """Force flush all in-memory changes to disk immediately."""
        async with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        """Write current processed state to disk atomically using os.replace."""
        if self.state_path.parent:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)

        tmp_file = self.state_path.with_name(
            f"{self.state_path.name}.tmp.{os.getpid()}"
        )
        payload = {"processed_slugs": sorted(self.processed)}

        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())

            os.replace(tmp_file, self.state_path)
            self._dirty.clear()
            logger.debug(
                f"Flushed {len(self.processed)} slugs to {self.state_path}"
            )
        except OSError as err:
            logger.error(f"Failed to flush state to {self.state_path}: {err}")
            if tmp_file.exists():
                try:
                    tmp_file.unlink()
                except OSError:
                    pass
            raise
