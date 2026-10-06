"""persistence.py — corrupt-file recovery and concurrent-write safety."""

import concurrent.futures

import persistence


def test_load_history_returns_empty_list_when_file_missing():
    assert persistence.load_history() == []


def test_load_history_returns_empty_list_on_corrupt_json():
    persistence.HISTORY_FILE.write_text("{not valid json")
    assert persistence.load_history() == []


def test_load_printers_returns_empty_list_on_corrupt_json():
    persistence.PRINTERS_FILE.write_text("not json at all")
    assert persistence.load_printers() == []


def test_load_tray_map_returns_empty_dict_on_corrupt_json():
    persistence.TRAY_MAP_FILE.write_text("{broken")
    assert persistence.load_tray_map() == {}


def test_append_history_roundtrips_a_single_entry():
    persistence.append_history({"filename": "a.gcode"})
    history = persistence.load_history()
    assert len(history) == 1
    assert history[0]["filename"] == "a.gcode"


def test_append_history_trims_to_max_entries(monkeypatch):
    monkeypatch.setattr(persistence, "HISTORY_MAX_ENTRIES", 5)
    for i in range(8):
        persistence.append_history({"i": i})
    history = persistence.load_history()
    assert len(history) == 5
    # Oldest entries are dropped, newest kept, in order.
    assert [e["i"] for e in history] == [3, 4, 5, 6, 7]


def test_append_history_loses_no_entries_under_concurrent_writers():
    n = 200
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as ex:
        list(ex.map(lambda i: persistence.append_history({"i": i}), range(n)))
    history = persistence.load_history()
    assert sorted(e["i"] for e in history) == list(range(n))


def test_migrate_history_ids_backfills_missing_ids():
    import json
    persistence.HISTORY_FILE.write_text(json.dumps([
        {"filename": "old1.gcode"},
        {"filename": "old2.gcode", "id": "already-has-one"},
    ]))
    persistence.migrate_history_ids()
    history = persistence.load_history()
    assert history[0]["id"]  # backfilled, non-empty
    assert history[1]["id"] == "already-has-one"  # untouched


def test_migrate_history_ids_is_a_noop_when_nothing_missing():
    persistence.append_history({"filename": "a.gcode", "id": "existing-id"})
    before = persistence.load_history()
    persistence.migrate_history_ids()
    after = persistence.load_history()
    assert before == after


def test_migrate_history_ids_does_not_deadlock_with_append_history():
    # migrate_history_ids() must not be called from inside append_history's
    # lock — this just exercises the real call sequence a server startup
    # would use, with a timeout-free assertion that it simply returns.
    persistence.append_history({"filename": "a.gcode"})
    persistence.migrate_history_ids()
    assert len(persistence.load_history()) == 1
