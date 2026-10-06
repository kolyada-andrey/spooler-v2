"""config.py — integration config store: priority, validation, masking, locking."""

import json

import pytest

import config
from config import ConfigError, clear, get, on_change, set as config_set


def test_default_value_when_nothing_set():
    assert get("spoolman.url") == "http://localhost:7912"
    assert config.source_of("spoolman.url") == "default"


def test_env_var_used_when_no_stored_override(monkeypatch):
    monkeypatch.setenv("SPOOLMAN_URL", "http://192.168.1.50:7912")
    assert get("spoolman.url") == "http://192.168.1.50:7912"
    assert config.source_of("spoolman.url") == "env"


def test_ui_value_wins_over_env(monkeypatch):
    monkeypatch.setenv("SPOOLMAN_URL", "http://192.168.1.50:7912")
    config_set("spoolman.url", "http://10.0.0.5:7912")
    assert get("spoolman.url") == "http://10.0.0.5:7912"
    assert config.source_of("spoolman.url") == "ui"


def test_set_strips_trailing_slash():
    config_set("spoolman.url", "http://10.0.0.5:7912/")
    assert get("spoolman.url") == "http://10.0.0.5:7912"


def test_set_rejects_disallowed_scheme():
    with pytest.raises(ConfigError, match="http or https"):
        config_set("spoolman.url", "file:///etc/passwd")


def test_set_rejects_url_without_host():
    with pytest.raises(ConfigError):
        config_set("spoolman.url", "http://")


def test_set_rejects_unknown_key():
    with pytest.raises(ConfigError):
        config_set("not.a.real.key", "x")


def test_bool_field_coerces_env_string(monkeypatch):
    monkeypatch.setenv("PROXY_SPOOLMAN", "false")
    assert get("spoolman.proxy") is False
    monkeypatch.setenv("PROXY_SPOOLMAN", "true")
    assert get("spoolman.proxy") is True


def test_bool_field_set_accepts_native_bool():
    config_set("spoolman.proxy", False)
    assert get("spoolman.proxy") is False


def test_int_field_default_and_env(monkeypatch):
    assert get("backup.interval_days") == 1
    monkeypatch.setenv("SPOOLER_BACKUP_INTERVAL_DAYS", "3")
    assert get("backup.interval_days") == 3


def test_int_field_rejects_negative():
    with pytest.raises(ConfigError, match="cannot be negative"):
        config_set("backup.interval_days", -1)


def test_int_field_rejects_non_numeric():
    with pytest.raises(ConfigError, match="must be a number"):
        config_set("backup.interval_days", "not-a-number")


def test_int_field_zero_is_allowed():
    config_set("backup.interval_days", 0)
    assert get("backup.interval_days") == 0


def test_values_persist_to_disk():
    config_set("spoolman.url", "http://10.0.0.5:7912")
    assert json.loads(config.INTEGRATIONS_FILE.read_text())["spoolman.url"] == "http://10.0.0.5:7912"


# ── Secrets ───────────────────────────────────────────────────────────────────

def test_secret_never_shown_in_describe_all():
    config_set("spoolman.auth_pass", "hunter2")
    entry = next(e for e in config.describe_all() if e["key"] == "spoolman.auth_pass")
    assert "value" not in entry
    assert entry["set"] is True


def test_secret_not_set_reports_set_false():
    entry = next(e for e in config.describe_all() if e["key"] == "spoolman.auth_pass")
    assert entry["set"] is False


def test_clear_removes_stored_secret():
    config_set("spoolman.auth_pass", "hunter2")
    clear("spoolman.auth_pass")
    assert get("spoolman.auth_pass") == ""
    entry = next(e for e in config.describe_all() if e["key"] == "spoolman.auth_pass")
    assert entry["set"] is False


# ── SPOOLER_LOCK_CONFIG ───────────────────────────────────────────────────────

def test_lock_config_forces_env_over_stored_ui_value(monkeypatch):
    config_set("spoolman.url", "http://10.0.0.5:7912")  # stored before lock engages
    monkeypatch.setenv("SPOOLMAN_URL", "http://192.168.1.50:7912")
    monkeypatch.setattr(config, "LOCK_CONFIG", True)
    assert get("spoolman.url") == "http://192.168.1.50:7912"
    assert config.source_of("spoolman.url") == "env"


def test_lock_config_rejects_writes(monkeypatch):
    monkeypatch.setattr(config, "LOCK_CONFIG", True)
    with pytest.raises(ConfigError, match="locked"):
        config_set("spoolman.url", "http://evil.example.com")


def test_lock_config_without_env_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(config, "LOCK_CONFIG", True)
    assert get("spoolman.url") == "http://localhost:7912"
    assert config.source_of("spoolman.url") == "default"


def test_describe_all_reports_locked(monkeypatch):
    monkeypatch.setattr(config, "LOCK_CONFIG", True)
    entry = next(e for e in config.describe_all() if e["key"] == "spoolman.url")
    assert entry["locked"] is True


# ── on_change ─────────────────────────────────────────────────────────────────

def test_on_change_fires_for_matching_prefix():
    seen = []
    on_change("spoolman.", lambda k, v: seen.append((k, v)))
    config_set("spoolman.url", "http://10.0.0.5:7912")
    assert seen == [("spoolman.url", "http://10.0.0.5:7912")]


def test_on_change_does_not_fire_for_other_prefix():
    seen = []
    on_change("slicer.", lambda k, v: seen.append((k, v)))
    config_set("spoolman.url", "http://10.0.0.5:7912")
    assert seen == []


def test_on_change_fires_on_clear_too():
    config_set("spoolman.auth_pass", "hunter2")
    seen = []
    on_change("spoolman.", lambda k, v: seen.append((k, v)))
    clear("spoolman.auth_pass")
    assert seen == [("spoolman.auth_pass", "")]


# ── Test-connection result tracking ──────────────────────────────────────────

def test_record_and_read_test_result():
    assert config.last_test_result("spoolman") is None
    config.record_test_result("spoolman", True, "Connected in 10 ms")
    result = config.last_test_result("spoolman")
    assert result["ok"] is True
    assert result["message"] == "Connected in 10 ms"
    assert "at" in result
