"""
Shared test fixtures.

Tests must never touch the real DATA_DIR, Spoolman, or the network. Every test
gets its persistence files redirected into a fresh tmp_path automatically.
"""

import pytest

import persistence


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Point persistence's file constants at a throwaway directory per test.

    persistence.py resolves DATA_DIR once at import time, so later os.environ
    changes don't retarget it — patch the already-bound Path objects directly
    instead of relying on the env var.
    """
    monkeypatch.setattr(persistence, "DATA_DIR", tmp_path)
    monkeypatch.setattr(persistence, "PRINTERS_FILE", tmp_path / "printers.json")
    monkeypatch.setattr(persistence, "HISTORY_FILE", tmp_path / "history.json")
    monkeypatch.setattr(persistence, "TRAY_MAP_FILE", tmp_path / "tray_map.json")
    yield tmp_path
