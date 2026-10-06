import json
import unittest
from unittest.mock import patch

from printers.cc2 import CC2Connection


class _Message:
    def __init__(self, topic: str, payload: dict):
        self.topic = topic
        self.payload = json.dumps(payload).encode()


class _MqttClient:
    def __init__(self):
        self.calls = []

    async def subscribe(self, topic):
        self.calls.append(("subscribe", topic))

    async def unsubscribe(self, topic):
        self.calls.append(("unsubscribe", topic))

    async def publish(self, topic, payload):
        self.calls.append(("publish", topic, json.loads(payload)))


class CC2RegistrationTests(unittest.IsolatedAsyncioTestCase):
    def _connection(self):
        connection = CC2Connection(
            printer_id="printer-1",
            ip="192.168.10.36",
            name="Centauri Carbon 2",
            access_code="12345",
        )
        connection._mqtt_serial = "F01CACHED"
        connection._mqtt_client_id = "client-1"
        connection._mqtt_request_id = "request-1"
        connection._mqtt_client = _MqttClient()

        async def _noop():
            return None

        connection._broadcast_state = _noop
        connection._check_print_transition = _noop
        return connection

    async def test_cached_serial_waits_for_live_status_before_full_registration(self):
        connection = self._connection()
        client = connection._mqtt_client

        self.assertFalse(connection._mqtt_registration_ready)
        self.assertEqual(client.calls, [])

        await connection._handle_mqtt_message(_Message(
            "elegoo/F01CACHED/api_status",
            {"result": {"machine_status": {"sub_status": 0}}},
        ))

        self.assertTrue(connection._mqtt_registration_ready)
        self.assertEqual(client.calls, [
            ("subscribe", "elegoo/F01CACHED/api_status"),
            ("subscribe", "elegoo/F01CACHED/request-1/register_response"),
            ("subscribe", "elegoo/F01CACHED/client-1/api_response"),
            ("unsubscribe", "elegoo/+/api_status"),
            (
                "publish",
                "elegoo/F01CACHED/api_register",
                {"client_id": "client-1", "request_id": "request-1"},
            ),
        ])

    async def test_live_status_replaces_stale_cached_serial(self):
        connection = self._connection()

        with patch("printers.cc2._save_cached_serial") as save_serial:
            await connection._handle_mqtt_message(_Message(
                "elegoo/F01LIVE/api_status",
                {"result": {"machine_status": {"sub_status": 0}}},
            ))

        self.assertEqual(connection._mqtt_serial, "F01LIVE")
        save_serial.assert_called_once_with("printer-1", "F01LIVE")
        publish_call = connection._mqtt_client.calls[-1]
        self.assertEqual(publish_call[1], "elegoo/F01LIVE/api_register")

    async def test_1046_response_is_handled_after_session_registration(self):
        connection = self._connection()
        await connection._handle_mqtt_message(_Message(
            "elegoo/F01CACHED/api_status",
            {"result": {"machine_status": {"sub_status": 0}}},
        ))
        await connection._handle_mqtt_message(_Message(
            "elegoo/F01CACHED/request-1/register_response",
            {"error": "ok"},
        ))

        connection._current_filename = "job.gcode"
        await connection._handle_mqtt_message(_Message(
            "elegoo/F01CACHED/client-1/api_response",
            {
                "method": 1046,
                "result": {
                    "filename": "job.gcode",
                    "total_filament_used": 12.5,
                    "print_time": 900,
                },
            },
        ))

        self.assertTrue(connection._mqtt_registered)
        self.assertEqual(connection._expected_filament_g, 12.5)
        self.assertEqual(connection._expected_print_time_s, 900)
        self.assertEqual(connection._current_print_metadata["filename"], "job.gcode")


if __name__ == "__main__":
    unittest.main()
