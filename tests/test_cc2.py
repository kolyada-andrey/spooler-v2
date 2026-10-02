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
    assert hint["category"] == "unknown"  # never guessed
    assert hint["raw"] == {"error_code": 42, "sub_status": 2501}


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
