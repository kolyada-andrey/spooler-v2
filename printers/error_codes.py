"""
Known CC2 error_code meanings.

Every entry here has been verified against the printer's own touchscreen (or
another authoritative source noted in the entry) -- never guessed from the
numeric value alone. A code missing from this table is surfaced raw with
category "unknown" and no message, per printers/cc2.py's
_protocol_reason_hint(); that is the correct, honest behavior for anything
not yet verified, not a gap to "fill in" with a plausible-sounding guess.
"""

CC2_ERROR_CODES = {
    # Verified 2026-10-02 against a real CC2's touchscreen (Tharje).
    704: {"category": "leveling", "message": "Leveling failed. Please try again."},
}


def lookup(error_code) -> dict | None:
    return CC2_ERROR_CODES.get(error_code)
