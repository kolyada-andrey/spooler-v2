"""
Shared test fixtures.

Tests must never touch the real DATA_DIR, Spoolman, or the network. Every test
gets its persistence files redirected into a fresh tmp_path automatically.
"""

import pytest

import backup
import persistence


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Point persistence's (and backup's) file constants at a throwaway
    directory per test.

    persistence.py resolves DATA_DIR once at import time, so later os.environ
    changes don't retarget it — patch the already-bound Path objects directly
    instead of relying on the env var. backup.py imports its own DATA_DIR/
    BACKUP_DIR reference from persistence at import time too, so it needs the
    same treatment or it would keep pointing at the real one.
    """
    monkeypatch.setattr(persistence, "DATA_DIR", tmp_path)
    monkeypatch.setattr(persistence, "PRINTERS_FILE", tmp_path / "printers.json")
    monkeypatch.setattr(persistence, "HISTORY_FILE", tmp_path / "history.json")
    monkeypatch.setattr(persistence, "TRAY_MAP_FILE", tmp_path / "tray_map.json")
    monkeypatch.setattr(backup, "DATA_DIR", tmp_path)
    monkeypatch.setattr(backup, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(backup, "_VERSION_MARKER", tmp_path / ".last_version")
    monkeypatch.setattr(backup, "_DAILY_MARKER", tmp_path / ".last_daily_backup")
    yield tmp_path
