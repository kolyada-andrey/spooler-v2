"""
CC2 printer connection via MQTT (Klipper-based firmware).
"""

import asyncio
import json
import math
import secrets
import time
import uuid
from pathlib import Path

import state
from persistence import save_tray_map
from printers.base import PrinterConnection
from spoolman import (
    infer_material_from_name,
    parse_cc2_filename,
    spoolman_find_or_create_by_material_color,
    spoolman_set_location,
    spoolman_assign,
)
from printers.protocol import (
    CMD_LIGHT, CMD_PAUSE, CMD_RESUME, CMD_STOP,
    deep_merge,
)

try:
    import aiomqtt
    AIOMQTT_AVAILABLE = True
except ImportError:
    AIOMQTT_AVAILABLE = False


def _serial_cache_path(printer_id: str) -> Path:
    from persistence import DATA_DIR
    return DATA_DIR / f"cc2_serial_{printer_id}.txt"

def _load_cached_serial(printer_id: str) -> str | None:
    try:
        v = _serial_cache_path(printer_id).read_text().strip()
        return v or None
    except FileNotFoundError:
        return None

def _save_cached_serial(printer_id: str, serial: str) -> None:
    try:
        _serial_cache_path(printer_id).write_text(serial)
    except Exception:
        pass

# Official CC2 method codes (elegooofficial/CentauriCarbon2 method.h)
_CC2_METHODS = {
    CMD_PAUSE:  1021,
    CMD_STOP:   1022,
    CMD_RESUME: 1023,
    CMD_LIGHT:  1029,
}

_CC2_STATE_KEYS = {
    "machine_status", "print_status", "extruder",
    "heater_bed", "ztemperature_sensor", "gcode_move", "led",
    "external_device", "tool_head", "fans",
    # Canvas / filament
    "canvas", "canvas_info", "channel_info", "channels",
    "filament", "filament_info", "extruder_filament",
    "mmu", "ams",
}


class CC2Connection(PrinterConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, printer_type="cc2", **kwargs)
        self._mqtt_client      = None
        self._mqtt_serial: str | None = None
        self._mqtt_client_id: str | None = None
        self._mqtt_request_id: str | None = None
        self._mqtt_registered  = False
        self._mqtt_registration_ready = False
        self._cc2_state: dict  = {}
        self._filament_mm_max  = 0.0
        self._prev_state_str   = ""
        self._extruder_offset  = 0.0
        self._last_extruder    = 0.0
        self._awaiting_file_list  = False
        self._current_filename    = ""
        self._mqtt_serial         = _load_cached_serial(self.id)
        self._prev_active_tray_id = -2  # sentinel: not yet seen
        self._pending_thumb_fut: asyncio.Future | None = None
        self._pending_meta_fut:  asyncio.Future | None = None
        self._pending_thumb_filename = ""
        self._pending_meta_filename  = ""
        self._file_info_lock = asyncio.Lock()
        self._expected_filament_g    = 0.0  # from method 1046 at print start
        self._expected_print_time_s  = 0    # from method 1046 at print start
        self._last_active_filament_mm = 0.0 # snapshot for cancelled prints
        self._tracked_print_filename = ""
        self._current_print_metadata: dict | None = None
        self._print_metadata_task: asyncio.Task | None = None
        self._print_metadata_event: asyncio.Event | None = None

    async def connect(self) -> None:
        self._prev_active_tray_id = -2
        if not AIOMQTT_AVAILABLE:
            print(f"[Printer {self.name}] aiomqtt not installed — CC2 unavailable")
            await self._broadcast_state()
            return
        try:
            print(f"[Printer {self.name}] Connecting via MQTT to {self.ip}:1883 …")
            ts_hex  = format(int(time.time() * 1000), "x")[-5:]
            rnd_hex = format(secrets.randbelow(4096), "x")
            self._mqtt_client_id  = f"0cli{ts_hex}{rnd_hex}"[:10]
            self._mqtt_request_id = uuid.uuid4().hex[:16]
            self._mqtt_registered = False
            self._mqtt_registration_ready = False
            # Keep the cached serial as a hint only.  Every MQTT session must
            # observe a fresh status message before registering; otherwise the
            # printer may acknowledge the client without routing later API
            # responses (notably method 1046) to it.

            async with aiomqtt.Client(
                hostname=self.ip,
                port=1883,
                username="elegoo",
                password=self.access_code,
            ) as client:
                self._mqtt_client = client
                self.camera_url = f"http://{self.ip}:8080/mjpeg"

                # A cached serial must not bypass the printer's session startup
                # sequence.  Discover/confirm it from a live status message on
                # every connection, exactly as on a cold container start.
                await client.subscribe("elegoo/+/api_status")
                if self._mqtt_serial:
                    print(
                        f"[Printer {self.name}] MQTT open — waiting for status "
                        f"before full registration (cached SN {self._mqtt_serial})…"
                    )
                else:
                    print(f"[Printer {self.name}] MQTT open — listening for serial (cold start)…")
                poll_task = asyncio.create_task(self._mqtt_status_poller())
                try:
                    async for message in client.messages:
                        await self._handle_mqtt_message(message)
                finally:
                    poll_task.cancel()
                    try:
                        await poll_task
                    except asyncio.CancelledError:
                        pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[Printer {self.name}] MQTT failed: {e}")
        finally:
            self._mqtt_client     = None
            self._mqtt_registered = False
            self._mqtt_registration_ready = False
            self.connected        = False
            self.camera_url       = f"http://{self.ip}:8080/mjpeg"
            await self._broadcast_state()

    async def send_cmd(self, cmd: int, data: dict | None = None) -> bool:
        if not self._mqtt_client or not self._mqtt_serial:
            return False
        if isinstance(cmd, int) and cmd > 1000:
            method = cmd  # already a CC2 method code
        else:
            method = _CC2_METHODS.get(cmd)
            if not method:
                return False
        if not self._mqtt_registered and method != 1003:
            if state.DEBUG:
                print(f"[Printer {self.name}] CC2 not registered yet, dropping method {method}")
            return False
        topic   = f"elegoo/{self._mqtt_serial}/{self._mqtt_client_id}/api_request"
        payload = {"id": uuid.uuid4().int & 0xFFFF, "method": method}
        if cmd == CMD_LIGHT and data:
            light_on = data.get("LightStatus", {}).get("SecondLight", False)
            payload["params"] = {"brightness": 255 if light_on else 0, "power": 1 if light_on else 0}
        elif method in (1020, 1031, 1044, 1045, 1046, 1047) and data:
            payload["params"] = data
        try:
            await self._mqtt_client.publish(topic, json.dumps(payload))
            return True
        except Exception as e:
            print(f"[Printer {self.name}] MQTT send error: {e}")
            return False

    async def _send_registration(self) -> bool:
        """Publish the CC2 registration handshake for the current MQTT session."""
        if (
            not self._mqtt_client
            or not self._mqtt_serial
            or not self._mqtt_client_id
            or not self._mqtt_request_id
        ):
            return False
        try:
            await self._mqtt_client.publish(
                f"elegoo/{self._mqtt_serial}/api_register",
                json.dumps({
                    "client_id": self._mqtt_client_id,
                    "request_id": self._mqtt_request_id,
                }),
            )
            return True
        except Exception as e:
            print(f"[Printer {self.name}] MQTT registration send error: {e}")
            return False

    async def _prepare_registration(self, serial: str) -> bool:
        """Finish subscriptions and register after this session's first status."""
        if self._mqtt_registration_ready or not self._mqtt_client:
            return False

        serial_changed = serial != self._mqtt_serial
        self._mqtt_serial = serial
        if serial_changed:
            _save_cached_serial(self.id, serial)
            print(f"[Printer {self.name}] SN discovered: {serial} (saved to cache)")
        else:
            print(f"[Printer {self.name}] SN confirmed: {serial}")

        # Subscribe to every session-specific reply before publishing the
        # registration.  Keep the exact status subscription before removing
        # discovery so there is no gap in status delivery.
        await self._mqtt_client.subscribe(f"elegoo/{serial}/api_status")
        await self._mqtt_client.subscribe(
            f"elegoo/{serial}/{self._mqtt_request_id}/register_response"
        )
        await self._mqtt_client.subscribe(
            f"elegoo/{serial}/{self._mqtt_client_id}/api_response"
        )
        await self._mqtt_client.unsubscribe("elegoo/+/api_status")
        self._mqtt_registration_ready = True

        sent = await self._send_registration()
        if sent:
            print(f"[Printer {self.name}] CC2 full registration sent")
        return sent

    async def _mqtt_status_poller(self) -> None:
        tick = 0
        while True:
            await asyncio.sleep(5)
            if not self._mqtt_registered:
                if self._mqtt_registration_ready and await self._send_registration():
                    print(f"[Printer {self.name}] CC2 registration retry sent")
                continue
            await self.send_cmd(1003)   # machine_status
            if tick % 2 == 0:
                await self.send_cmd(2005)  # canvas channel info
            tick += 1

    async def _handle_mqtt_message(self, message) -> None:
        topic = str(message.topic)

        # Registration is deliberately gated on a live status message from the
        # current MQTT session.  This applies to both cached and newly
        # discovered serial numbers.
        parts = topic.split("/")
        if (
            not self._mqtt_registration_ready
            and len(parts) == 3
            and parts[0] == "elegoo"
            and parts[1]
            and parts[2] == "api_status"
        ):
            await self._prepare_registration(parts[1])

        expected_register_topic = (
            f"elegoo/{self._mqtt_serial}/{self._mqtt_request_id}/register_response"
            if self._mqtt_serial and self._mqtt_request_id else ""
        )
        if topic == expected_register_topic:
            try:
                p = json.loads(message.payload.decode())
                if p.get("error") == "ok":
                    if self._mqtt_registered:
                        return
                    self._mqtt_registered = True
                    self.connected = True
                    print(f"[Printer {self.name}] CC2 registered OK — ready")
                    await self._broadcast_state()
                    await self.send_cmd(1002)  # full state
                    await self.send_cmd(1003)  # machine_status
                    await self.send_cmd(1042)  # camera URL
                    await self.send_cmd(2005)  # canvas channel info
                    await self.send_cmd(1056)  # extruder filament info
                    if self._current_filename and self._print_is_active():
                        # Status pushes may arrive before the printer is ready
                        # to acknowledge registration. Restart the metadata
                        # lookup now that authenticated commands can succeed.
                        self._begin_print_tracking(
                            self._current_filename, force=True
                        )
                    loop = asyncio.get_running_loop()
                    loop.run_in_executor(None, self._sync_spoolman_locations)
                else:
                    print(f"[Printer {self.name}] CC2 registration failed: {p}")
            except Exception:
                pass
            return

        try:
            payload = json.loads(message.payload.decode())
        except Exception:
            return

        if "api_response" in topic:
            inner  = payload.get("result")

            if self._awaiting_file_list and isinstance(inner, dict) and "file_list" in inner:
                self._awaiting_file_list = False
                raw_list = inner.get("file_list") or []
                if state.DEBUG and raw_list:
                    print(f"[CC2 file_list] first item keys: {list(raw_list[0].keys())}")
                    print(f"[CC2 file_list] first item: {json.dumps(raw_list[0])[:400]}")
                from printers.cc1 import _parse_print_time
                files = [
                    {
                        "name":       f.get("filename", ""),
                        "path":       f.get("filename", ""),
                        "size":       f.get("size", 0),
                        "is_dir":     f.get("type") == "dir",
                        "print_time": f.get("print_time") or _parse_print_time(f.get("filename", "")),
                        "layers":     f.get("layer"),
                        "filament_g": f.get("total_filament_used"),
                        "color_map":  f.get("color_map"),
                        "printed":    f.get("total_print_times", 0),
                    }
                    for f in raw_list
                    if isinstance(f, dict)
                ]
                await state.broadcast_to_browsers({
                    "type": "file_list", "printer_id": self.id, "files": files,
                })
                return

            # Camera URL response (method 1042 GET_MONITOR_VIDENO_URL)
            _method = payload.get("method")
            if _method == 1042 and isinstance(inner, dict):
                url = inner.get("url") or inner.get("video_url") or inner.get("mjpeg_url")
                if url:
                    self.camera_url = url
                    await self._broadcast_state()
                return

            # Thumbnail response (method 1045)
            if _method == 1045 and isinstance(inner, dict):
                if self._pending_thumb_fut and not self._pending_thumb_fut.done():
                    response_filename = str(inner.get("filename") or "")
                    if (not response_filename or self._same_filename(
                            response_filename, self._pending_thumb_filename)):
                        b64 = inner.get("thumbnail") or inner.get("data") or inner.get("image")
                        self._pending_thumb_fut.set_result(b64 if isinstance(b64, str) else None)
                return

            # File metadata response (method 1046)
            if _method == 1046 and isinstance(inner, dict):
                response_filename = str(inner.get("filename") or "")
                if (self._pending_meta_fut and not self._pending_meta_fut.done()
                        and self._same_filename(
                            response_filename, self._pending_meta_filename)):
                    self._pending_meta_fut.set_result(inner)
                print(f"[Printer {self.name}] 1046 full response: {json.dumps(inner)[:600]}")
                if self._same_filename(response_filename, self._current_filename):
                    # Only metadata for the current job can influence its
                    # progress, spool selection, and deduction.
                    self._current_print_metadata = inner
                    fila_g = inner.get("total_filament_used")
                    if fila_g is not None and float(fila_g or 0) > 0:
                        self._expected_filament_g = float(fila_g)
                    pt = inner.get("print_time")
                    if pt is not None and int(pt or 0) > 0:
                        self._expected_print_time_s = int(pt)
                    if self._expected_filament_g or self._expected_print_time_s:
                        print(f"[Printer {self.name}] File metadata: "
                              f"{self._expected_filament_g}g / {self._expected_print_time_s}s")
                    if self._print_metadata_event:
                        self._print_metadata_event.set()
                    asyncio.create_task(self._auto_link_spools_from_metadata(inner))
                return

            source = inner if isinstance(inner, dict) else payload
            if state.DEBUG:
                _method = payload.get("method")
                if _method in (2005, 1056, 1044):
                    print(f"[CC2 probe] method={_method} full response: "
                          f"{json.dumps(payload)[:800]}")
                else:
                    unknown = {k for k in source if k not in _CC2_STATE_KEYS
                               and k not in ("error_code",) and isinstance(source[k], (dict, list))}
                    if unknown:
                        print(f"[CC2] method={_method} unknown keys: {unknown} — "
                              f"raw: {json.dumps({k: source[k] for k in unknown})[:600]}")
            updates = {k: v for k, v in source.items()
                       if k in _CC2_STATE_KEYS and isinstance(v, dict)}
            # Strip stale filament_used from the 1002 full-state snapshot
            if inner is not None and isinstance(updates.get("print_status"), dict):
                updates["print_status"].pop("filament_used", None)
            if updates:
                deep_merge(self._cc2_state, updates)
                self._apply_cc2_status()
                # Polling replies carry the same state as api_status pushes.
                # Account for their transitions too, otherwise an ended print
                # can be visible in the UI but never reach history/deduction.
                await self._check_print_transition()
                await self._broadcast_state()
            return

        result = payload.get("result", {})
        if not isinstance(result, dict) or not result:
            return

        deep_merge(self._cc2_state, result)
        self._apply_cc2_status()
        await self._check_print_transition()
        await self._broadcast_state()

    async def request_file_list(self) -> bool:
        if not self._mqtt_registered:
            msg = ("Printer MQTT not ready yet — wait a moment and try again."
                   if self.connected else "Printer not connected.")
            await state.broadcast_to_browsers({
                "type": "file_list", "printer_id": self.id, "files": [],
                "error": msg,
            })
            return False
        self._awaiting_file_list = True
        ok = await self.send_cmd(1044, {"storage_media": "local", "offset": 0, "limit": 50})
        if not ok:
            self._awaiting_file_list = False
            await state.broadcast_to_browsers({
                "type": "file_list", "printer_id": self.id, "files": [],
                "error": "Failed to send file list request.",
            })
        else:
            asyncio.create_task(self._file_list_timeout())
        return ok

    async def fetch_file_info(self, filename: str) -> None:
        """Fetch thumbnail (1045) + metadata (1046) via MQTT and broadcast file_info."""
        if not self._mqtt_registered:
            return
        # MQTT responses do not expose a request id reliably, so do not allow
        # two browser lookups to overwrite each other's single response futures.
        async with self._file_info_lock:
            loop = asyncio.get_running_loop()
            self._pending_thumb_fut = loop.create_future()
            self._pending_meta_fut  = loop.create_future()
            self._pending_thumb_filename = filename
            self._pending_meta_filename = filename
            await self.send_cmd(1045, {"storage_media": "local", "filename": filename})
            await self.send_cmd(1046, {"storage_media": "local", "filename": filename})
            try:
                results = await asyncio.wait_for(
                    asyncio.gather(self._pending_thumb_fut, self._pending_meta_fut,
                                   return_exceptions=True),
                    timeout=10,
                )
            except asyncio.TimeoutError:
                results = [None, {}]
            finally:
                self._pending_thumb_fut = None
                self._pending_meta_fut  = None
                self._pending_thumb_filename = ""
                self._pending_meta_filename = ""

        thumb_b64 = results[0] if isinstance(results[0], str) else None
        meta      = results[1] if isinstance(results[1], dict) else {}
        await state.broadcast_to_browsers({
            "type":         "file_info",
            "printer_id":   self.id,
            "filename":     filename,
            "thumbnail_b64": thumb_b64,
            "print_time":   meta.get("print_time"),
            "layers":       meta.get("layer"),
            "filament_g":   meta.get("total_filament_used"),
        })

    def _sync_spoolman_locations(self) -> None:
        """On connect, push all tray-linked spool locations to Spoolman."""
        for spool_id in (state.tray_map.get(self.id) or {}).values():
            if spool_id is not None:
                spoolman_set_location(spool_id, self.id)

    async def _file_list_timeout(self) -> None:
        await asyncio.sleep(10)
        if self._awaiting_file_list:
            self._awaiting_file_list = False
            await state.broadcast_to_browsers({
                "type": "file_list", "printer_id": self.id, "files": [],
                "error": "File list request timed out.",
            })

    @staticmethod
    def _same_filename(left: str, right: str) -> bool:
        """Compare printer paths without treating an empty response as a match."""
        return bool(left and right and Path(left).name == Path(right).name)

    def _print_is_active(self) -> bool:
        """Return whether either raw or normalized CC2 state shows an active job."""
        print_state = self._cc2_state.get("print_status", {}).get("state", "")
        normalized_status = self._decoded_printinfo().get("Status")
        return (
            print_state in ("printing", "paused")
            or normalized_status in (2, 3, 4, 5, 6, 7, 13)
        )

    def _begin_print_tracking(self, filename: str, *, force: bool = False) -> None:
        """Reset one CC2 job and fetch its 1046 metadata with bounded retries."""
        if not filename:
            return
        same_job = self._same_filename(filename, self._tracked_print_filename)
        fetch_in_progress = (
            self._print_metadata_task is not None
            and not self._print_metadata_task.done()
        )
        if not force and same_job and (fetch_in_progress or self._current_print_metadata):
            return
        if self._print_metadata_task and not self._print_metadata_task.done():
            self._print_metadata_task.cancel()
        self._tracked_print_filename = filename
        self._current_print_metadata = None
        self._filament_mm_max = 0.0
        self._extruder_offset = 0.0
        self._last_extruder = 0.0
        self._expected_filament_g = 0.0
        self._expected_print_time_s = 0
        self._last_active_filament_mm = 0.0
        self._print_metadata_task = asyncio.create_task(
            self._fetch_print_metadata(filename)
        )

    async def _fetch_print_metadata(self, filename: str) -> None:
        """Request 1046 until it arrives, the job changes, or retries are exhausted."""
        event: asyncio.Event | None = None
        try:
            for attempt in range(1, 4):
                if not self._same_filename(filename, self._current_filename):
                    return
                event = asyncio.Event()
                self._print_metadata_event = event
                sent = await self.send_cmd(
                    1046, {"storage_media": "local", "filename": filename}
                )
                if sent:
                    try:
                        await asyncio.wait_for(event.wait(), timeout=4)
                        return
                    except asyncio.TimeoutError:
                        pass
                if attempt < 3:
                    await asyncio.sleep(2)
            if self._mqtt_registered:
                print(f"[Printer {self.name}] 1046 unavailable for {filename!r} after 3 attempts")
        finally:
            if self._print_metadata_event is event:
                self._print_metadata_event = None

    def _start_auto_link_for_current_metadata(self) -> None:
        if self._current_print_metadata:
            asyncio.create_task(
                self._auto_link_spools_from_metadata(self._current_print_metadata)
            )

    async def _auto_link_spools_from_metadata(self, meta: dict) -> None:
        """Link CC2 trays to exact material/color matches in Spoolman."""
        color_map = meta.get("color_map")
        meta_filename = str(meta.get("filename") or "")
        current_filename = str(self._current_filename or "")
        filename_meta = parse_cc2_filename(meta_filename or current_filename)
        if not isinstance(color_map, list):
            color_map = []
        if not color_map and filename_meta:
            filament_name = filename_meta.get("filament_name", "")
            material = (
                filename_meta.get("material")
                or infer_material_from_name(filament_name)
            )
            if material:
                color_map = [{
                    "name": material,
                    "color": filename_meta.get("color_hex"),
                    "t": 0,
                }]
        if not color_map:
            return

        # Newer firmware may omit print_status.state and expose the active
        # phase only through machine_status.sub_status.  _apply_cc2_status()
        # has already normalized that value into PrintInfo.Status.
        if not self._print_is_active():
            return

        if (
            meta_filename
            and current_filename
            and Path(meta_filename).name != Path(current_filename).name
        ):
            print(
                f"[Printer {self.name}] Auto-match ignored: metadata file "
                f"{meta_filename!r} != current file {current_filename!r}"
            )
            return

        loop = asyncio.get_running_loop()
        matched_spools = []
        mapping_changed = False

        for item in color_map:
            if not isinstance(item, dict):
                continue

            material = str(
                filename_meta.get("material") or item.get("name") or ""
            ).strip()
            # The slicer's color_map can contain its generic preview color
            # (#000000 in particular). The configured output filename carries
            # default_filament_colour and is authoritative when present.
            color = str(
                filename_meta.get("color_hex") or item.get("color") or ""
            ).strip()
            filament_name = str(
                filename_meta.get("display_name")
                or filename_meta.get("filament_name")
                or ""
            ).strip()
            vendor_name = str(filename_meta.get("vendor_name") or "").strip()
            try:
                tray_id = int(item.get("t", 0))
            except (TypeError, ValueError):
                tray_id = 0

            if not material or not color:
                continue

            spool = await loop.run_in_executor(
                None,
                spoolman_find_or_create_by_material_color,
                material,
                color,
                self.id,
                filament_name,
                vendor_name,
            )
            if not spool:
                continue

            spool_id = int(spool["id"])
            printer_trays = state.tray_map.setdefault(self.id, {})
            tray_key = str(tray_id)
            if printer_trays.get(tray_key) != spool_id:
                printer_trays[tray_key] = spool_id
                mapping_changed = True

            matched_spools.append(spool_id)
            await loop.run_in_executor(
                None, spoolman_set_location, spool_id, self.id
            )

            density = (spool.get("filament") or {}).get("density")
            if density:
                try:
                    self.filament_density = float(density)
                except (TypeError, ValueError):
                    pass

            print(
                f"[Printer {self.name}] Auto-linked Slot {tray_id + 1} → "
                f"Spool {spool_id} ({material} {color})"
            )

        if mapping_changed:
            await loop.run_in_executor(None, save_tray_map, state.tray_map)
            await state.broadcast_to_browsers({
                "type": "tray_map",
                "tray_map": state.tray_map,
            })

        if len(matched_spools) == 1:
            self._current_print_spool = matched_spools[0]
            print(
                f"[Printer {self.name}] Current print spool → "
                f"{matched_spools[0]}"
            )

    async def start_print_file(self, filename: str, print_opts: dict | None = None) -> bool:
        self._current_filename = filename
        opts = print_opts or {}
        ok = await self.send_cmd(1020, {
            "filename":      filename,
            "storage_media": "local",
            "config": {
                "delay_video":   bool(opts.get("timelapse")),
                "bedlevel_force": bool(opts.get("leveling")),
                "print_layout":  "B" if opts.get("smooth_plate") else "A",
            },
        })
        if ok:
            # Do not rely on a subsequent state push: some firmware revisions
            # report only a sub_status while starting a job.
            self._begin_print_tracking(filename, force=True)
        return ok

    def _apply_cc2_status(self) -> None:
        s     = self._cc2_state
        ps    = s.get("print_status", {})
        gm    = s.get("gcode_move", {})
        ext   = s.get("extruder", {})
        bed   = s.get("heater_bed", {})
        ztemp = s.get("ztemperature_sensor", {})
        ms    = s.get("machine_status", {})

        print_duration = ps.get("print_duration", 0) or 0
        remaining      = ps.get("remaining_time_sec", 0) or 0
        state_str      = ps.get("state", "")
        filename_from_ps = (ps.get("filename") or ps.get("task_name") or
                            ps.get("file_name") or ps.get("file", {}).get("filename", ""))
        if filename_from_ps:
            self._current_filename = filename_from_ps
        sub_status     = ms.get("sub_status", 0)

        _SUB_TRANSIENT = {2501: 5, 2503: 7}
        _SUB_STABLE    = {
            1045: 15, 1096: 15, 1405: 15,
            2075: 3,  2401: 3,  2402: 3,
            2077: 9,
            2502: 6,  2505: 6,
            2504: 8,
        }
        _STATE_STR = {
            "printing":  3,
            "paused":    6,
            "complete":  9,
            "cancelled": 8,
            "error":     14,
            "standby":   0,
        }

        if sub_status in _SUB_TRANSIENT:
            status_code = _SUB_TRANSIENT[sub_status]
        elif state_str in _STATE_STR:
            status_code = _STATE_STR[state_str]
        elif sub_status in _SUB_STABLE:
            status_code = _SUB_STABLE[sub_status]
        elif remaining > 0:
            status_code = 3
        else:
            status_code = 0

        if status_code == 0:
            print_duration = 0
            remaining      = 0

        # CC2 doesn't report remaining_time_sec — estimate from file's expected duration
        if remaining == 0 and status_code == 3 and self._expected_print_time_s > 0:
            remaining = max(0, self._expected_print_time_s - print_duration)

        total    = print_duration + remaining
        progress = min(100, round(print_duration / total * 100)) if total > 0 else 0

        # Some firmware versions omit print_status.state and expose the active
        # phase only through sub_status.  Keep a normalized state edge for
        # metadata fetching and per-print cleanup in that case.
        tracking_state = state_str
        if not tracking_state:
            if status_code == 3:
                tracking_state = "printing"
            elif status_code == 9:
                tracking_state = "complete"
            elif status_code == 8:
                tracking_state = "cancelled"
            elif status_code == 14:
                tracking_state = "error"
            elif status_code == 0 and self._prev_state_str in ("printing", "paused"):
                tracking_state = "standby"

        prev_state_str = self._prev_state_str
        print_ending = (
            tracking_state in ("complete", "standby", "cancelled", "error")
            and prev_state_str in ("printing", "paused")
        )
        new_print = (
            tracking_state == "printing"
            and prev_state_str not in ("printing", "paused")
        )
        if new_print:
            fname = self._current_filename
            if fname:
                self._begin_print_tracking(fname)
                self._start_auto_link_for_current_metadata()

        # CC2 MQTT never sends filament_used or reliable gcode_move.extruder.
        # Use time-progress × expected grams (from method 1046) instead.
        if self._expected_filament_g > 0:
            density = self.filament_density or 1.24
            expected_mm = self._expected_filament_g * 10.0 / (math.pi * 0.0875 ** 2 * density)
            just_finished = (
                tracking_state in ("complete", "standby")
                and prev_state_str in ("printing", "paused")
            )
            if tracking_state == "printing" and (print_duration + remaining) > 0:
                filament_mm = print_duration / (print_duration + remaining) * expected_mm
                self._last_active_filament_mm = filament_mm
            elif just_finished:
                filament_mm = expected_mm
            else:
                filament_mm = self._last_active_filament_mm
        else:
            # Fallback: legacy live tracking (usually 0 for CC2 but kept for safety)
            filament_from_push = ps.get("filament_used") or 0
            if filament_from_push > self._filament_mm_max:
                self._filament_mm_max = filament_from_push
            raw_ext = gm.get("extruder") or 0
            if raw_ext < self._last_extruder * 0.5 and self._last_extruder > 1.0:
                self._extruder_offset += self._last_extruder
            self._last_extruder = raw_ext
            filament_mm = max(self._filament_mm_max, self._extruder_offset + raw_ext)

        # CC2 reports active_tray_id=-1 for ordinary single-material prints.
        # If the base tracker lost the asynchronously auto-selected spool, recover
        # the exact spool most recently linked to Slot 1 instead of falling back
        # to the first Spoolman item sharing the printer location.
        if print_ending and self._current_print_spool is None:
            slot_one_spool = (state.tray_map.get(self.id) or {}).get("0")
            if slot_one_spool is not None:
                self._current_print_spool = int(slot_one_spool)
                print(
                    f"[Printer {self.name}] Restored current print spool from "
                    f"Slot 1 → {slot_one_spool}"
                )

        if print_ending:
            self._tracked_print_filename = ""
            if self._print_metadata_task and not self._print_metadata_task.done():
                self._print_metadata_task.cancel()

        if tracking_state:
            self._prev_state_str = tracking_state

        led    = s.get("led", {})
        led_on = 1 if (led.get("status", 0) or 0) > 0 else 0

        sf = gm.get("speed_factor")
        speed_factor = sf if sf is not None else 1.0

        self.status = {
            "PrintInfo": {
                "Status":         status_code,
                "CurrentLayer":   ps.get("current_layer", 0),
                "TotalLayer":     ps.get("total_layer", 0),
                "CurrentTicks":   progress,
                "TotalTicks":     100,
                "PrintTime":      print_duration,
                "RemainTime":     remaining,
                "TotalExtrusion": filament_mm,
                "Filename":       self._current_filename,
            },
            "TempOfNozzle":     ext.get("temperature", 0),
            "TempTargetNozzle": ext.get("target", 0),
            "TempOfHotbed":     bed.get("temperature", 0),
            "TempTargetHotbed": bed.get("target", 0),
            "TempOfBox":        ztemp.get("temperature", 0),
            "LightStatus":      {"SecondLight": led_on},
            "SpeedFactor":      round(speed_factor * 100),
        }

        # Expose canvas tray info so the browser can render filament slots
        ci = s.get("canvas_info", {})
        if ci:
            self.status["canvas_info"] = ci
            active_tray = ci.get("active_tray_id", -1)
            if active_tray != self._prev_active_tray_id:
                self._prev_active_tray_id = active_tray
                spool_id = (state.tray_map.get(self.id) or {}).get(str(active_tray)) if active_tray >= 0 else None
                loop = asyncio.get_running_loop()
                if active_tray >= 0:
                    loop.run_in_executor(None, spoolman_assign, self.id, spool_id)
                loop.run_in_executor(None, self._update_filament_density)
                self._on_tray_change(spool_id)
                print(f"[Printer {self.name}] Active tray changed → {active_tray}, spool {spool_id}")
