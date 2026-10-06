"""printers/cc2.py — error_code capture and reason surfacing."""

import json

import pytest

from printers.cc2 import CC2Connection


class _FakePayload:
    def __init__(self, data: dict):
        self._raw = json.dumps(data).encode()

    def decode(self):
        return self._raw.decode()


class _FakeMessage:
    def __init__(self, topic: str, data: dict):
        self.topic = topic
        self.payload = _FakePayload(data)


@pytest.fixture
def printer():
    p = CC2Connection("pid1", "10.0.0.5", "Test CC2")
    p.connected = True
    p._mqtt_serial = "SN123"
    p._mqtt_client_id = "cli1"
    p._mqtt_registered = True
    return p


def test_protocol_reason_hint_none_without_error_code(printer):
    assert printer._protocol_reason_hint() is None


def test_protocol_reason_hint_none_when_error_code_is_zero(printer):
    printer._cc2_state["error_code"] = 0
    assert printer._protocol_reason_hint() is None


def test_protocol_reason_hint_surfaces_raw_code_only(printer):
    printer._cc2_state["error_code"] = 42
    printer._cc2_state["machine_status"] = {"sub_status": 2501}
    hint = printer._protocol_reason_hint()
    assert hint["code"] == 42
    assert hint["category"] == "unknown"  # never guessed -- not in error_codes.py
    assert hint["message"] == ""
    assert hint["raw"] == {"error_code": 42, "sub_status": 2501}


def test_protocol_reason_hint_resolves_known_error_code(printer):
    printer._cc2_state["error_code"] = 704
    hint = printer._protocol_reason_hint()
    assert hint["code"] == 704
    assert hint["category"] == "leveling"
    assert hint["message"] == "Leveling failed. Please try again."


def test_apply_cc2_status_maps_bed_preheating_sub_status_1906(printer):
    # Verified against Elegoo's own elegoo-link SDK -- 1906 was missing from
    # our sub_status table even though 1405/1096 (also preheating) were there.
    printer._cc2_state["machine_status"] = {"sub_status": 1906}
    printer._apply_cc2_status()
    assert printer.status["PrintInfo"]["Status"] == 15


@pytest.mark.parametrize("sub_status,expected_code", [
    (2801, 1),   # homing
    (2802, 1),   # homing
    (2901, 20),  # auto-leveling
    (2902, 20),  # auto-leveling
])
def test_apply_cc2_status_maps_homing_and_leveling_sub_statuses(printer, sub_status, expected_code):
    printer._cc2_state["machine_status"] = {"sub_status": sub_status}
    printer._apply_cc2_status()
    assert printer.status["PrintInfo"]["Status"] == expected_code


@pytest.mark.asyncio
async def test_error_code_captured_from_api_response_poll_path(printer):
    # Simulates the 5s status poller's method 1003 response, which goes
    # through _CC2_STATE_KEYS filtering that would otherwise drop a scalar
    # like error_code entirely.
    msg = _FakeMessage(
        "elegoo/SN123/cli1/api_response",
        {"method": 1003, "result": {"machine_status": {"sub_status": 0}, "error_code": 7}},
    )
    await printer._handle_mqtt_message(msg)
    assert printer._cc2_state.get("error_code") == 7


@pytest.mark.asyncio
async def test_error_code_captured_from_api_status_push_path(printer):
    msg = _FakeMessage(
        "elegoo/SN123/api_status",
        {"result": {"print_status": {"state": "printing"}, "error_code": 13}},
    )
    await printer._handle_mqtt_message(msg)
    assert printer._cc2_state.get("error_code") == 13


# ── external_device.camera ───────────────────────────────────────────────────

def test_camera_connected_defaults_to_unknown(printer):
    assert printer.camera_connected is None


def test_camera_connected_true_when_reported(printer):
    printer._cc2_state["external_device"] = {"camera": True}
    printer._apply_cc2_status()
    assert printer.camera_connected is True


def test_camera_connected_false_when_reported(printer):
    printer._cc2_state["external_device"] = {"camera": False}
    printer._apply_cc2_status()
    assert printer.camera_connected is False


def test_camera_connected_stays_unknown_without_external_device(printer):
    printer._apply_cc2_status()
    assert printer.camera_connected is None


# ── Device attributes (method 1001) ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_device_attributes_captured_from_api_response(printer):
    msg = _FakeMessage(
        "elegoo/SN123/cli1/api_response",
        {"method": 1001, "result": {
            "machine_model": "Centauri Carbon 2",
            "software_version": {"mcu_version": "00.00.00.00", "ota_version": "02.01.00.00", "soc_version": ""},
            "sn": "F013B3B8WZZ9K11",
            "hostname": "CC_2",
        }},
    )
    await printer._handle_mqtt_message(msg)
    assert printer.attrs == {
        "Model":           "Centauri Carbon 2",
        "FirmwareVersion": "02.01.00.00",
        "MainboardID":     "F013B3B8WZZ9K11",
        "Hostname":        "CC_2",
    }


def test_attrs_untouched_without_device_attribute_fields(printer):
    printer._cc2_state["machine_status"] = {"sub_status": 0}
    printer._apply_cc2_status()
    assert printer.attrs == {}
