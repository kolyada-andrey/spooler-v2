"""
printers/base.py — print-start/print-end transition handling.

These exercise PrinterConnection directly (not a protocol subclass): connect()
and send_cmd() are never called, only the status-transition machinery, so the
abstract methods raising NotImplementedError never matters here.
"""

import pytest

import persistence
import printers.base as base_mod
from printers.base import PrinterConnection, classify_print_transition


# ── classify_print_transition (pure function) ───────────────────────────────

@pytest.mark.parametrize("prev,cur,expected", [
    (None, 0, None),            # unknown -> idle: nothing happened
    (None, 2, "start"),         # unknown -> printing: start
    (0, 2, "start"),            # idle -> printing: start
    (2, 2, None),                # printing -> printing: no re-trigger
    (2, 9, "end"),               # printing -> complete: end
    (2, 8, "end"),               # printing -> cancelled: end
    (2, 14, "end"),              # printing -> error: end
    (2, 0, "end"),                # printing -> idle (CC2-style completion): end
    (9, 2, "start"),             # complete -> printing directly: must still be "start"
                                  # (regression test for the ACTIVE-set bug: 9 used to be
                                  # considered "active", so this transition was missed)
    (9, 9, None),                 # sitting at complete: nothing happens repeatedly
    (6, 9, "end"),                 # paused -> complete: end
])
def test_classify_print_transition(prev, cur, expected):
    assert classify_print_transition(prev, cur) == expected


# ── _check_print_transition (integration of the above into PrinterConnection) ─

@pytest.fixture
def printer(monkeypatch):
    p = PrinterConnection("pid1", "10.0.0.5", "Test Printer")
    monkeypatch.setattr(base_mod, "get_spool_density", lambda printer_id: 1.24)
    monkeypatch.setattr(base_mod, "spoolman_deduct", lambda *a, **kw: None)
    monkeypatch.setattr(base_mod, "spoolman_deduct_spool", lambda *a, **kw: None)
    return p


def _set_status(printer, status_code, **printinfo_overrides):
    pi = {"Status": status_code, **printinfo_overrides}
    printer.status = {"PrintInfo": pi}


@pytest.mark.asyncio
async def test_print_start_initializes_extrusion_tracking(printer):
    printer._extrusion_snapshot = 999.0
    printer._spool_extrusion = {1: 50.0}
    _set_status(printer, 2, TotalExtrusion=0)
    await printer._check_print_transition()
    assert printer._extrusion_snapshot == 0.0
    assert printer._spool_extrusion == {}
    assert printer._print_start_time is not None


@pytest.mark.asyncio
async def test_complete_to_printing_resets_snapshot_regression(printer):
    """Regression test for the ACTIVE-set bug: complete (9) directly to
    printing (2), with no idle poll observed in between, must still reset
    per-print extrusion tracking — this was the actual production bug."""
    _set_status(printer, 9, TotalExtrusion=500)
    await printer._check_print_transition()  # printer sits at "complete"
    printer._extrusion_snapshot = 500.0        # simulate stale leftover snapshot
    printer._spool_extrusion = {1: 500.0}

    _set_status(printer, 2, TotalExtrusion=0)  # next print starts directly from 9
    await printer._check_print_transition()

    assert printer._extrusion_snapshot == 0.0
    assert printer._spool_extrusion == {}


@pytest.mark.asyncio
async def test_print_complete_records_history_entry(printer):
    _set_status(printer, 2, TotalExtrusion=0, Filename="a.gcode")
    await printer._check_print_transition()

    _set_status(printer, 9, TotalExtrusion=1000, Filename="a.gcode", PrintTime=600)
    await printer._check_print_transition()

    history = persistence.load_history()
    assert len(history) == 1
    assert history[0]["filename"] == "a.gcode"
    assert history[0]["completed"] is True
    assert history[0]["print_time_s"] == 600


@pytest.mark.asyncio
async def test_print_cancelled_records_history_with_completed_false(printer):
    _set_status(printer, 2, TotalExtrusion=0, Filename="b.gcode")
    await printer._check_print_transition()

    _set_status(printer, 8, TotalExtrusion=300, Filename="b.gcode", PrintTime=120)
    await printer._check_print_transition()

    history = persistence.load_history()
    assert len(history) == 1
    assert history[0]["completed"] is False


@pytest.mark.asyncio
async def test_no_history_entry_when_no_filament_and_no_filename(printer):
    _set_status(printer, 2, TotalExtrusion=0)
    await printer._check_print_transition()

    _set_status(printer, 9, TotalExtrusion=0)  # nothing was ever printed
    await printer._check_print_transition()

    assert persistence.load_history() == []


@pytest.mark.asyncio
async def test_idle_to_idle_does_not_append_history(printer):
    _set_status(printer, 0, TotalExtrusion=0)
    await printer._check_print_transition()
    _set_status(printer, 0, TotalExtrusion=0)
    await printer._check_print_transition()
    assert persistence.load_history() == []
