"""printers/error_codes.py — verified CC2 error_code catalog."""

from printers.error_codes import lookup


def test_lookup_known_code():
    entry = lookup(704)
    assert entry["category"] == "leveling"
    assert entry["message"] == "Leveling failed. Please try again."


def test_lookup_unknown_code_returns_none():
    assert lookup(999999) is None
