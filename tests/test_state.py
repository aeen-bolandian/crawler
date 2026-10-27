"""Tests for crawler state manager module."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.state import StateManager


def test_load_missing_file(tmp_path: Path) -> None:
    """Verify that a missing state file initializes an empty set."""
    state_file = tmp_path / "state.json"
    mgr = StateManager(state_file)
    assert mgr.processed == set()
    assert not state_file.exists()


def test_load_corrupt_file(tmp_path: Path) -> None:
    """Verify that a corrupt state file defaults to an empty set with warning."""
    state_file = tmp_path / "corrupt_state.json"
    state_file.write_text("invalid json content {{{", encoding="utf-8")

    mgr = StateManager(state_file)
    assert mgr.processed == set()


@pytest.mark.asyncio
async def test_mark_processed_persists(tmp_path: Path) -> None:
    """Verify that marking a slug as processed persists after flush."""
    state_file = tmp_path / "state.json"
    mgr = StateManager(state_file, flush_every=10)

    await mgr.mark_processed("cafe-alpha")
    assert mgr.is_processed("cafe-alpha")
    await mgr.flush()

    reloaded = StateManager(state_file)
    assert "cafe-alpha" in reloaded.processed


@pytest.mark.asyncio
async def test_batch_flush(tmp_path: Path) -> None:
    """Verify automatic disk flush when batch threshold is reached."""
    state_file = tmp_path / "state.json"
    mgr = StateManager(state_file, flush_every=10)

    # First 9 items: should not have flushed to disk yet
    for i in range(9):
        await mgr.mark_processed(f"slug-{i}")

    assert not state_file.exists()

    # 10th item triggers batch flush
    await mgr.mark_processed("slug-9")
    assert state_file.exists()

    # Add 5 more (total 15), flush explicitly
    for i in range(10, 15):
        await mgr.mark_processed(f"slug-{i}")
    await mgr.flush()

    reloaded = StateManager(state_file)
    assert len(reloaded.processed) == 15
    for i in range(15):
        assert reloaded.is_processed(f"slug-{i}")


def test_is_processed(tmp_path: Path) -> None:
    """Verify is_processed returns True for present slugs and False otherwise."""
    state_file = tmp_path / "state.json"
    mgr = StateManager(state_file)
    mgr.processed.add("cafe-existing")

    assert mgr.is_processed("cafe-existing") is True
    assert mgr.is_processed("cafe-missing") is False


@pytest.mark.asyncio
async def test_duplicate_mark(tmp_path: Path) -> None:
    """Verify marking the same slug repeatedly does not duplicate or corrupt."""
    state_file = tmp_path / "state.json"
    mgr = StateManager(state_file, flush_every=5)

    await mgr.mark_processed("cafe-dup")
    await mgr.mark_processed("cafe-dup")
    await mgr.flush()

    with open(state_file, "r", encoding="utf-8") as f:
        data = json.load(f)
        assert data["processed_slugs"] == ["cafe-dup"]


@pytest.mark.asyncio
async def test_atomic_write(tmp_path: Path) -> None:
    """Verify that temporary files are cleaned up and no .tmp files linger."""
    state_file = tmp_path / "state.json"
    mgr = StateManager(state_file)

    await mgr.mark_processed("cafe-test")
    await mgr.flush()

    assert state_file.exists()
    tmp_files = list(tmp_path.glob("*.tmp*"))
    assert len(tmp_files) == 0


@pytest.mark.asyncio
async def test_force_flush(tmp_path: Path) -> None:
    """Verify force flush writes to disk even with fewer items than flush_every."""
    state_file = tmp_path / "state.json"
    mgr = StateManager(state_file, flush_every=100)

    await mgr.mark_processed("slug-1")
    assert not state_file.exists()

    await mgr.flush()
    assert state_file.exists()

    reloaded = StateManager(state_file)
    assert reloaded.is_processed("slug-1")
