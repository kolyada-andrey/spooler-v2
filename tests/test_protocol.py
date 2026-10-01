"""
printers/protocol.py — decode_printinfo and deep_merge.

Fixture note: the hex-encoded-key format below ('54 6F 74 61 6C...') is
reconstructed from the decoding rule documented in decode_printinfo's own
docstring, not captured from a real printer. It is a synthetic example built
to match the documented/verified encoding, not a hardware capture.
"""

from printers.protocol import decode_printinfo, deep_merge


def test_decode_printinfo_decodes_hex_encoded_key():
    # "TotalExtrusion" as space-separated hex byte pairs, per the function's docstring.
    hex_key = "54 6F 74 61 6C 45 78 74 72 75 73 69 6F 6E"
    pi = {hex_key: 123.4, "Status": 3}
    result = decode_printinfo(pi)
    assert result["TotalExtrusion"] == 123.4
    assert result["Status"] == 3
    assert hex_key not in result


def test_decode_printinfo_leaves_plain_keys_untouched():
    pi = {"Status": 3, "CurrentLayer": 10, "Filename": "test.gcode"}
    assert decode_printinfo(pi) == pi


def test_decode_printinfo_ignores_single_hex_like_token():
    # A lone two-char token isn't treated as an encoded key — needs multiple
    # space-separated hex pairs to be decoded, per the ">1 part" check.
    pi = {"4F": "something"}
    assert decode_printinfo(pi) == {"4F": "something"}


def test_decode_printinfo_falls_back_on_invalid_hex():
    # Looks hex-shaped per-token but decodes to invalid UTF-8 / fails decode.
    pi = {"FF FF": 1}
    result = decode_printinfo(pi)
    assert result == {"FF FF": 1}


def test_deep_merge_merges_nested_dicts():
    base = {"a": {"x": 1, "y": 2}, "b": 5}
    deep_merge(base, {"a": {"y": 99, "z": 3}, "c": 7})
    assert base == {"a": {"x": 1, "y": 99, "z": 3}, "b": 5, "c": 7}


def test_deep_merge_replaces_non_dict_values():
    base = {"a": 1}
    deep_merge(base, {"a": {"nested": True}})
    assert base == {"a": {"nested": True}}
