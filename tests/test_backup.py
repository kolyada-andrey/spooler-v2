"""backup.py — zip creation, validation, restore, and auto-backup scheduling."""

import json
import time
import zipfile

import pytest

import backup
import features
import persistence


def _write(name: str, content):
    path = persistence.DATA_DIR / name
    path.write_text(json.dumps(content) if not isinstance(content, str) else content)


# ── create_backup_zip ────────────────────────────────────────────────────────

def test_create_backup_zip_includes_only_existing_files():
    _write("printers.json", [])
    # history.json intentionally not created
    manifest = backup.create_backup_zip(persistence.DATA_DIR / "b.zip", include_secrets=True)
    names = {f["name"] for f in manifest["files"]}
    assert names == {"printers.json"}


def test_create_backup_zip_checksums_match_content():
    _write("printers.json", [{"id": "p1"}])
    zip_path = persistence.DATA_DIR / "b.zip"
    manifest = backup.create_backup_zip(zip_path, include_secrets=True)
    with zipfile.ZipFile(zip_path) as zf:
        raw = zf.read("printers.json")
    entry = next(f for f in manifest["files"] if f["name"] == "printers.json")
    assert entry["sha256"] == backup._sha256(raw)


def test_create_backup_zip_redacts_access_code_by_default():
    _write("printers.json", [{"id": "p1", "access_code": "secret"}])
    zip_path = persistence.DATA_DIR / "b.zip"
    backup.create_backup_zip(zip_path, include_secrets=False)
    with zipfile.ZipFile(zip_path) as zf:
        data = json.loads(zf.read("printers.json"))
    assert data[0]["access_code"] == ""
    assert data[0]["id"] == "p1"  # rest of the entry is untouched


def test_create_backup_zip_keeps_access_code_when_requested():
    _write("printers.json", [{"id": "p1", "access_code": "secret"}])
    zip_path = persistence.DATA_DIR / "b.zip"
    backup.create_backup_zip(zip_path, include_secrets=True)
    with zipfile.ZipFile(zip_path) as zf:
        data = json.loads(zf.read("printers.json"))
    assert data[0]["access_code"] == "secret"


# ── validate_backup_zip ──────────────────────────────────────────────────────

def _make_manifest_zip(path, manifest, extra_members=None):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        for name, content in (extra_members or {}).items():
            zf.writestr(name, content)


def test_validate_accepts_well_formed_backup():
    _write("printers.json", [{"id": "p1"}])
    zip_path = persistence.DATA_DIR / "b.zip"
    backup.create_backup_zip(zip_path, include_secrets=True)
    manifest = backup.validate_backup_zip(zip_path)
    assert manifest["files"]


def test_validate_rejects_not_a_zip():
    bad = persistence.DATA_DIR / "notazip.zip"
    bad.write_text("this is not a zip file")
    with pytest.raises(backup.RestoreError, match="valid zip"):
        backup.validate_backup_zip(bad)


def test_validate_rejects_missing_manifest():
    path = persistence.DATA_DIR / "b.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("printers.json", "[]")
    with pytest.raises(backup.RestoreError, match="manifest"):
        backup.validate_backup_zip(path)


def test_validate_rejects_unrecognised_declared_filename():
    path = persistence.DATA_DIR / "b.zip"
    manifest = {"spooler_version": "1.0.0", "created_at": "x", "includes_secrets": False,
                "files": [{"name": "../../etc/passwd", "sha256": "x"}]}
    _make_manifest_zip(path, manifest)
    with pytest.raises(backup.RestoreError, match="Unrecognised"):
        backup.validate_backup_zip(path)


def test_validate_rejects_extra_undeclared_member():
    """Zip-slip style attack: a member not listed in the manifest at all --
    must be rejected rather than silently ignored or extracted."""
    path = persistence.DATA_DIR / "b.zip"
    manifest = {"spooler_version": "1.0.0", "created_at": "x", "includes_secrets": False, "files": []}
    _make_manifest_zip(path, manifest, extra_members={"../../etc/cron.d/evil": "* * * * * root rm -rf /"})
    with pytest.raises(backup.RestoreError, match="Unexpected extra content"):
        backup.validate_backup_zip(path)


def test_validate_rejects_checksum_mismatch():
    path = persistence.DATA_DIR / "b.zip"
    manifest = {"spooler_version": "1.0.0", "created_at": "x", "includes_secrets": False,
                "files": [{"name": "printers.json", "sha256": "0" * 64}]}
    _make_manifest_zip(path, manifest, extra_members={"printers.json": "[]"})
    with pytest.raises(backup.RestoreError, match="Checksum mismatch"):
        backup.validate_backup_zip(path)


def test_validate_rejects_newer_spooler_version():
    path = persistence.DATA_DIR / "b.zip"
    manifest = {"spooler_version": "999.0.0", "created_at": "x", "includes_secrets": False, "files": []}
    _make_manifest_zip(path, manifest)
    with pytest.raises(backup.RestoreError, match="newer Spooler version"):
        backup.validate_backup_zip(path)


def test_validate_accepts_older_spooler_version():
    path = persistence.DATA_DIR / "b.zip"
    manifest = {"spooler_version": "0.0.1", "created_at": "x", "includes_secrets": False, "files": []}
    _make_manifest_zip(path, manifest)
    backup.validate_backup_zip(path)  # must not raise


def test_validate_rejects_missing_declared_file():
    path = persistence.DATA_DIR / "b.zip"
    manifest = {"spooler_version": "1.0.0", "created_at": "x", "includes_secrets": False,
                "files": [{"name": "printers.json", "sha256": "0" * 64}]}
    _make_manifest_zip(path, manifest)  # declared but never actually written
    with pytest.raises(backup.RestoreError, match="missing declared file"):
        backup.validate_backup_zip(path)


# ── restore_from_zip ─────────────────────────────────────────────────────────

def test_restore_round_trip_produces_identical_content():
    _write("printers.json", [{"id": "p1", "name": "Printer A"}])
    _write("history.json", [{"filename": "a.gcode", "id": "h1"}])
    zip_path = persistence.DATA_DIR / "b.zip"
    backup.create_backup_zip(zip_path, include_secrets=True)

    (persistence.DATA_DIR / "printers.json").unlink()
    (persistence.DATA_DIR / "history.json").unlink()

    backup.restore_from_zip(zip_path)

    assert json.loads((persistence.DATA_DIR / "printers.json").read_text()) == [
        {"id": "p1", "name": "Printer A"}
    ]
    assert json.loads((persistence.DATA_DIR / "history.json").read_text()) == [
        {"filename": "a.gcode", "id": "h1"}
    ]


def test_restore_takes_a_safety_backup_first():
    _write("printers.json", [{"id": "old"}])
    zip_path = persistence.DATA_DIR / "b.zip"
    backup.create_backup_zip(zip_path, include_secrets=True)

    _write("printers.json", [{"id": "current-before-restore"}])
    backup.restore_from_zip(zip_path)

    pre_restore = list(backup.BACKUP_DIR.glob("pre-restore-*.zip"))
    assert len(pre_restore) == 1
    with zipfile.ZipFile(pre_restore[0]) as zf:
        snapshotted = json.loads(zf.read("printers.json"))
    assert snapshotted == [{"id": "current-before-restore"}]


def test_restore_rejects_invalid_backup_and_writes_nothing():
    _write("printers.json", [{"id": "untouched"}])
    bad = persistence.DATA_DIR / "bad.zip"
    bad.write_text("not a zip")
    with pytest.raises(backup.RestoreError):
        backup.restore_from_zip(bad)
    assert json.loads((persistence.DATA_DIR / "printers.json").read_text()) == [{"id": "untouched"}]


# ── Automatic backups: retention, version-change, daily ─────────────────────

def test_cleanup_old_backups_keeps_newest_by_mtime():
    backup.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(5):
        p = backup.BACKUP_DIR / f"daily-{i}.zip"
        p.write_text("x")
        os_time = time.time() + i  # ensure strictly increasing mtimes
        import os
        os.utime(p, (os_time, os_time))
        paths.append(p)
    backup.cleanup_old_backups(keep=2)
    remaining = {p.name for p in backup.BACKUP_DIR.glob("*.zip")}
    assert remaining == {"daily-3.zip", "daily-4.zip"}


def test_make_automatic_backup_creates_a_file():
    _write("printers.json", [])
    path = backup.make_automatic_backup("daily")
    assert path is not None
    assert path.exists()
    assert path.parent == backup.BACKUP_DIR


def test_check_startup_backup_skips_on_fresh_install():
    backup.check_startup_backup()
    assert list(backup.BACKUP_DIR.glob("pre-upgrade-*.zip")) == []
    assert backup._VERSION_MARKER.read_text().strip() == persistence.current_version()


def test_check_startup_backup_backs_up_on_version_change():
    backup._VERSION_MARKER.write_text("0.0.1")
    _write("printers.json", [])
    backup.check_startup_backup()
    assert len(list(backup.BACKUP_DIR.glob("pre-upgrade-*.zip"))) == 1
    assert backup._VERSION_MARKER.read_text().strip() == persistence.current_version()


def test_check_startup_backup_no_backup_when_version_unchanged():
    backup._VERSION_MARKER.write_text(persistence.current_version())
    backup.check_startup_backup()
    assert list(backup.BACKUP_DIR.glob("pre-upgrade-*.zip")) == []


def test_maybe_daily_backup_runs_when_no_marker(monkeypatch):
    monkeypatch.setattr(backup, "AUTO_BACKUP_DAILY", True)
    backup.maybe_daily_backup()
    assert len(list(backup.BACKUP_DIR.glob("daily-*.zip"))) == 1


def test_maybe_daily_backup_skips_when_recent(monkeypatch):
    monkeypatch.setattr(backup, "AUTO_BACKUP_DAILY", True)
    backup._DAILY_MARKER.write_text(str(time.time()))
    backup.maybe_daily_backup()
    assert list(backup.BACKUP_DIR.glob("daily-*.zip")) == []


def test_maybe_daily_backup_respects_disabled_flag(monkeypatch):
    monkeypatch.setattr(backup, "AUTO_BACKUP_DAILY", False)
    backup.maybe_daily_backup()
    assert list(backup.BACKUP_DIR.glob("daily-*.zip")) == []


def test_list_auto_backups_reports_name_size_modified():
    backup.make_automatic_backup("daily")
    entries = backup.list_auto_backups()
    assert len(entries) == 1
    assert set(entries[0]) == {"name", "size", "modified"}
    assert entries[0]["size"] > 0


# ── Gated by the "backup" feature flag ───────────────────────────────────────

def test_backup_feature_defaults_on():
    assert features.is_enabled("backup") is True


def test_check_startup_backup_skipped_when_feature_off():
    features.set_enabled("backup", False)
    backup._VERSION_MARKER.write_text("0.0.1")  # version changed -- would normally back up
    _write("printers.json", [])
    backup.check_startup_backup()
    assert list(backup.BACKUP_DIR.glob("pre-upgrade-*.zip")) == []
    # Marker deliberately NOT updated while the feature is off, so turning it
    # back on still catches the version change it missed this time.
    assert backup._VERSION_MARKER.read_text().strip() == "0.0.1"


def test_maybe_daily_backup_skipped_when_feature_off(monkeypatch):
    monkeypatch.setattr(backup, "AUTO_BACKUP_DAILY", True)
    features.set_enabled("backup", False)
    backup.maybe_daily_backup()
    assert list(backup.BACKUP_DIR.glob("daily-*.zip")) == []
