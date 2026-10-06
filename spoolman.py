"""
Spoolman integration: filament database and spool deduction.
"""

import asyncio
import base64
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import config
import state as _state
from persistence import FILAMENT_DENSITY
from push import load_notif_settings, send_push_all
from state import broadcast_to_browsers

SPOOLMAN_AUTO_CREATE = os.getenv("SPOOLMAN_AUTO_CREATE", "true").lower() in (
    "1", "true", "yes", "on",
)
try:
    SPOOLMAN_DEFAULT_SPOOL_WEIGHT = float(
        os.getenv("SPOOLMAN_DEFAULT_SPOOL_WEIGHT", "1000")
    )
except ValueError:
    SPOOLMAN_DEFAULT_SPOOL_WEIGHT = 1000.0
if SPOOLMAN_DEFAULT_SPOOL_WEIGHT <= 0:
    SPOOLMAN_DEFAULT_SPOOL_WEIGHT = 1000.0

_auto_create_lock = threading.Lock()

_MATERIAL_DENSITIES = {
    "ABS": 1.04,
    "ASA": 1.07,
    "PA": 1.14,
    "PC": 1.20,
    "PETG": 1.27,
    "PLA": FILAMENT_DENSITY,
    "PVA": 1.23,
    "TPU": 1.21,
}


def parse_cc2_filename(filename: str) -> dict:
    """Parse the configured ECC2 output filename format.

    Preferred format uses explicit, order-independent KEY=value fields:
    ECC2@VENDOR=...@NAME=...@MATERIAL=...@COLOR=...@NOZZLE=...@MODEL=...@TIME=...

    The legacy literal-asterisk variant is accepted as well. For the underscore
    format, input names may contain underscores; filament profile names should
    not contain underscores so the boundary remains unambiguous.
    """
    name = str(filename or "").rsplit("/", 1)[-1]
    if name.lower().endswith(".gcode"):
        name = name[:-6]
    if name.startswith("ECC2@") and "=" in name:
        fields = {}
        bare_parts = []
        for part in name.split("@")[1:]:
            if "=" not in part:
                if part.strip():
                    bare_parts.append(part.strip())
                continue
            key, value = part.split("=", 1)
            fields[key.strip().upper()] = value.strip()
        color = fields.get("COLOR", "").lstrip("#").upper()[:6]
        material = fields.get("MATERIAL", "").upper()
        profile_name = fields.get("PROFILE", "") or (
            bare_parts[0] if bare_parts else ""
        )
        identity = split_filament_profile(profile_name, material)
        vendor_name = fields.get("VENDOR", "") or identity["vendor_name"]
        display_name = fields.get("NAME", "") or identity["display_name"]
        if (
            not re.fullmatch(r"[0-9A-F]{6}", color)
            or not material
            or not vendor_name
        ):
            return {}
        return {
            "nozzle_diameter": fields.get("NOZZLE", ""),
            "input_filename_base": fields.get("MODEL", ""),
            "filament_name": profile_name or display_name,
            "vendor_name": vendor_name,
            "display_name": display_name,
            "material": material,
            "color_hex": color,
            "print_time": fields.get("TIME", ""),
        }
    elif name.startswith("ECC2@"):
        parts = name.split("@")
        if len(parts) != 7:
            return {}
        _, nozzle, input_name, filament_name, material, raw_color, print_time = parts
        material = material.strip().upper()
    elif "*" in name:
        parts = name.split("*")
        if len(parts) != 5 or not parts[0].startswith("ECC2_"):
            return {}
        nozzle = parts[0][len("ECC2_"):].strip()
        input_name = parts[1].strip()
        filament_name = parts[2].strip()
        raw_color = parts[3]
        print_time = parts[4].strip()
        material = ""
    else:
        if not name.startswith("ECC2_"):
            return {}
        try:
            nozzle, remainder = name[len("ECC2_"):].split("_", 1)
            before_color, raw_color, print_time = remainder.rsplit("_", 2)
            input_name, filament_name = before_color.rsplit("_", 1)
        except ValueError:
            return {}
        nozzle = nozzle.strip()
        input_name = input_name.strip()
        filament_name = filament_name.strip()
        print_time = print_time.strip()
        material = ""

    color = raw_color.strip().lstrip("#").upper()[:6]
    if not re.fullmatch(r"[0-9A-F]{6}", color):
        return {}
    identity = split_filament_profile(filament_name, material)
    return {
        "nozzle_diameter": nozzle,
        "input_filename_base": input_name,
        "filament_name": filament_name,
        "vendor_name": identity["vendor_name"],
        "display_name": identity["display_name"],
        "material": identity["material"],
        "color_hex": color,
        "print_time": print_time,
    }


def infer_material_from_name(filament_name: str) -> str:
    """Extract a common material token from a slicer filament profile name."""
    words = re.findall(r"[A-Za-z0-9+-]+", str(filament_name or "").upper())
    known = set(_MATERIAL_DENSITIES) | {"HIPS", "PEEK", "PEI", "PET", "PP"}
    return next((word for word in words if word in known), "")


def split_filament_profile(profile_name: str, material: str = "") -> dict:
    """Split 'Bambu Lab PETG Basic Gray' into vendor/material/display name."""
    profile = str(profile_name or "").strip()
    wanted_material = str(material or "").strip().upper()
    words = profile.split()
    material_index = next(
        (
            index for index, word in enumerate(words)
            if word.upper() == wanted_material
        ),
        None,
    ) if wanted_material else None

    if material_index is None:
        inferred = infer_material_from_name(profile)
        if inferred:
            wanted_material = inferred
            material_index = next(
                index for index, word in enumerate(words)
                if word.upper() == inferred
            )

    if material_index is None:
        return {
            "vendor_name": "",
            "material": wanted_material,
            "display_name": profile,
        }

    vendor_name = " ".join(words[:material_index]).strip()
    vendor_name = {
        "elegoo": "ELEGOO",
        "bambu lab": "Bambu Lab",
    }.get(vendor_name.casefold(), vendor_name)
    product_words = words[material_index + 1:]
    if product_words and product_words[0].casefold() == "basic":
        product_words = product_words[1:]
    display_name = " ".join(product_words).strip()
    return {
        "vendor_name": vendor_name,
        "material": wanted_material,
        "display_name": display_name or profile,
    }

def spoolman_find_by_material_color(
    material: str,
    color_hex: str,
    printer_id: str | None = None,
    filament_name: str = "",
    vendor_name: str = "",
) -> dict | None:
    """
    Find exactly one active Spoolman spool by filament material + color.

    If several spools match, prefer exactly one already located at this
    printer. Otherwise return None because the physical spool is ambiguous.
    """

    def normalize_color(value: str) -> str:
        return str(value or "").strip().lstrip("#").upper()[:6]

    wanted_material = str(material or "").strip().upper()
    wanted_color = normalize_color(color_hex)
    wanted_name = str(filament_name or "").strip().casefold()
    wanted_vendor = str(vendor_name or "").strip().casefold()

    if not wanted_material or len(wanted_color) != 6:
        print(
            f"[Spoolman] Auto-match skipped: invalid material/color "
            f"({material!r}, {color_hex!r})"
        )
        return None

    try:
        base = get_spoolman_url()
        url = f"{base}/api/v1/spool?allow_archived=false"
        with urllib.request.urlopen(url, timeout=5) as resp:
            spools = json.loads(resp.read())

        matches = []
        for spool in spools:
            if spool.get("archived"):
                continue

            filament = spool.get("filament") or {}
            spool_material = str(filament.get("material") or "").strip().upper()
            spool_color = normalize_color(filament.get("color_hex") or "")
            if spool_material != wanted_material or spool_color != wanted_color:
                continue
            if wanted_name and str(filament.get("name") or "").strip().casefold() != wanted_name:
                continue
            spool_vendor = filament.get("vendor") or {}
            if wanted_vendor and str(spool_vendor.get("name") or "").strip().casefold() != wanted_vendor:
                continue

            remaining = spool.get("remaining_weight")
            if remaining is not None:
                try:
                    if float(remaining) <= 0:
                        continue
                except (TypeError, ValueError):
                    pass
            matches.append(spool)

        if not matches:
            print(
                f"[Spoolman] Auto-match: no spool found for "
                f"{wanted_material} #{wanted_color}"
            )
            return None

        if len(matches) == 1:
            spool = matches[0]
            filament = spool.get("filament") or {}
            print(
                f"[Spoolman] Auto-match: {wanted_material} #{wanted_color} → "
                f"spool {spool['id']} ({filament.get('name', 'unknown')})"
            )
            return spool

        if printer_id:
            location = _printer_location(printer_id)
            located = [
                spool for spool in matches
                if (spool.get("location") or "") == location
            ]
            if len(located) == 1:
                spool = located[0]
                print(
                    f"[Spoolman] Auto-match: {wanted_material} #{wanted_color} → "
                    f"spool {spool['id']} (preferred by location {location})"
                )
                return spool

        ids = [spool.get("id") for spool in matches]
        print(
            f"[Spoolman] Auto-match ambiguous: {wanted_material} "
            f"#{wanted_color}, matching spools: {ids}"
        )
        return None
    except Exception as e:
        print(f"[Spoolman] Auto-match failed: {e}")
        return None


def spoolman_find_or_create_by_material_color(
    material: str,
    color_hex: str,
    printer_id: str,
    filament_name: str = "",
    vendor_name: str = "",
) -> dict | None:
    """Find an active spool, or create one and its filament when absent."""
    with _auto_create_lock:
        spool = spoolman_find_by_material_color(
            material,
            color_hex,
            printer_id,
            filament_name,
            vendor_name,
        )
        if spool or not SPOOLMAN_AUTO_CREATE:
            return spool

        wanted_material = str(material or "").strip().upper()
        wanted_color = str(color_hex or "").strip().lstrip("#").upper()[:6]
        if not wanted_material or not re.fullmatch(r"[0-9A-F]{6}", wanted_color):
            return None

        try:
            base = get_spoolman_url()
            # A None result can also mean multiple matching physical spools.
            # Never create another one in that case.
            with urllib.request.urlopen(
                f"{base}/api/v1/spool?allow_archived=false", timeout=5
            ) as resp:
                active_spools = json.loads(resp.read())
            existing = []
            for item in active_spools:
                filament = item.get("filament") or {}
                item_material = str(
                    filament.get("material") or ""
                ).strip().upper()
                item_color = str(
                    filament.get("color_hex") or ""
                ).strip().lstrip("#").upper()[:6]
                if item_material != wanted_material or item_color != wanted_color:
                    continue
                if filament_name and str(
                    filament.get("name") or ""
                ).strip().casefold() != str(filament_name).strip().casefold():
                    continue
                item_vendor = filament.get("vendor") or {}
                if vendor_name and str(
                    item_vendor.get("name") or ""
                ).strip().casefold() != str(vendor_name).strip().casefold():
                    continue
                try:
                    if float(item.get("remaining_weight")) <= 0:
                        continue
                except (TypeError, ValueError):
                    pass
                existing.append(item)
            if existing:
                print(
                    f"[Spoolman] Auto-create skipped: {len(existing)} matching "
                    f"spool(s) already exist"
                )
                return None

            with urllib.request.urlopen(
                f"{base}/api/v1/filament?limit=9999", timeout=5
            ) as resp:
                filaments = json.loads(resp.read())

            wanted_name = str(filament_name or "").strip()
            wanted_vendor = str(vendor_name or "").strip()
            matching = [
                item for item in filaments
                if str(item.get("material") or "").strip().upper() == wanted_material
                and str(item.get("color_hex") or "").strip().lstrip("#").upper()[:6]
                == wanted_color
                and (
                    not wanted_vendor
                    or str(
                        ((item.get("vendor") or {}).get("name") or "")
                    ).strip().casefold() == wanted_vendor.casefold()
                )
            ]
            named = [
                item for item in matching
                if wanted_name
                and str(item.get("name") or "").strip().casefold()
                == wanted_name.casefold()
                and (
                    not wanted_vendor
                    or str(
                        ((item.get("vendor") or {}).get("name") or "")
                    ).strip().casefold() == wanted_vendor.casefold()
                )
            ]

            if len(named) == 1:
                filament = named[0]
            elif len(matching) == 1 and not wanted_name:
                filament = matching[0]
            elif len(matching) > 1:
                print(
                    f"[Spoolman] Auto-create skipped: ambiguous filament types "
                    f"for {wanted_material} #{wanted_color}"
                )
                return None
            else:
                vendor_id = None
                wanted_vendor = str(vendor_name or "").strip()
                if wanted_vendor:
                    with urllib.request.urlopen(
                        f"{base}/api/v1/vendor?limit=9999", timeout=5
                    ) as resp:
                        vendors = json.loads(resp.read())
                    vendor = next(
                        (
                            item for item in vendors
                            if str(item.get("name") or "").strip().casefold()
                            == wanted_vendor.casefold()
                        ),
                        None,
                    )
                    if vendor is None:
                        req = urllib.request.Request(
                            f"{base}/api/v1/vendor",
                            data=json.dumps({"name": wanted_vendor}).encode(),
                            headers={"Content-Type": "application/json"},
                            method="POST",
                        )
                        with urllib.request.urlopen(req, timeout=5) as resp:
                            vendor = json.loads(resp.read())
                        print(
                            f"[Spoolman] Auto-created vendor {vendor['id']}: "
                            f"{wanted_vendor}"
                        )
                    vendor_id = int(vendor["id"])

                filament_payload = {
                    "name": wanted_name or f"Auto {wanted_material} #{wanted_color}",
                    "material": wanted_material,
                    "color_hex": wanted_color,
                    "density": _MATERIAL_DENSITIES.get(
                        wanted_material, FILAMENT_DENSITY
                    ),
                    "diameter": 1.75,
                    "weight": SPOOLMAN_DEFAULT_SPOOL_WEIGHT,
                    "comment": "Automatically created from CC2 print metadata",
                }
                if vendor_id is not None:
                    filament_payload["vendor_id"] = vendor_id
                req = urllib.request.Request(
                    f"{base}/api/v1/filament",
                    data=json.dumps(filament_payload).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    filament = json.loads(resp.read())
                print(
                    f"[Spoolman] Auto-created filament {filament['id']}: "
                    f"{filament_payload['name']}"
                )

            spool_payload = {
                "filament_id": int(filament["id"]),
                "remaining_weight": SPOOLMAN_DEFAULT_SPOOL_WEIGHT,
                "location": _printer_location(printer_id),
                "comment": "Automatically created at CC2 print start",
            }
            req = urllib.request.Request(
                f"{base}/api/v1/spool",
                data=json.dumps(spool_payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                spool = json.loads(resp.read())
            print(
                f"[Spoolman] Auto-created spool {spool['id']} for "
                f"{wanted_material} #{wanted_color} "
                f"({SPOOLMAN_DEFAULT_SPOOL_WEIGHT:g}g)"
            )
            return spool
        except Exception as e:
            print(f"[Spoolman] Auto-create failed: {e}")
            return None

def _printer_location(printer_id: str) -> str:
    """Return the Spoolman location string for a printer — its name, not its UUID."""
    p = _state.printers.get(printer_id)
    return p.name if p else printer_id

SPOOLMAN_DB_URL = "https://donkie.github.io/SpoolmanDB/filaments.json"


def get_spoolman_url() -> str:
    # Read live every call (not a module constant) so a change made in
    # Settings -> Integrations takes effect immediately, no restart needed --
    # every one of this module's Spoolman calls already funnels through here.
    return config.get("spoolman.url")


def spoolman_auth_header() -> dict:
    """Basic-auth header for Spoolman, if configured (e.g. Spoolman sitting
    behind a reverse proxy that requires it) -- empty dict otherwise."""
    user = config.get("spoolman.auth_user")
    if not user:
        return {}
    pw = config.get("spoolman.auth_pass")
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _spoolman_request(url: str, method: str = "GET", body: bytes | None = None) -> urllib.request.Request:
    """Build a Request with the configured basic-auth header attached (a
    no-op header-wise when none is configured) -- every direct Spoolman call
    in this module goes through this so reverse-proxy basic auth, once set,
    covers all of them uniformly, not just the browser-facing UI proxy."""
    headers = spoolman_auth_header()
    if body is not None:
        headers["Content-Type"] = "application/json"
    return urllib.request.Request(url, data=body, headers=headers, method=method)


def test_spoolman_connection() -> dict:
    """GET /api/v1/health -- verified directly against a real running Spoolman
    instance (not guessed), returns {"status": "healthy"}. Lightest possible
    check that still proves the URL, network path, and basic auth (if any)
    all actually work."""
    url = get_spoolman_url()
    start = time.time()
    try:
        req = _spoolman_request(f"{url}/api/v1/health")
        with urllib.request.urlopen(req, timeout=5):
            pass
        elapsed_ms = round((time.time() - start) * 1000)
        return {"ok": True, "message": f"Connected in {elapsed_ms} ms"}
    except urllib.error.HTTPError as e:
        return {"ok": False, "message": f"HTTP {e.code} from {url}"}
    except Exception as e:
        return {"ok": False, "message": f"Could not reach {url}: {e}"}


SPOOLMAN_DB_TTL  = 3600  # re-fetch at most once per hour

_spoolman_db: list | None = None
_spoolman_db_fetched: float = 0.0


def get_spoolman_db() -> list:
    global _spoolman_db, _spoolman_db_fetched
    if _spoolman_db is not None and time.time() - _spoolman_db_fetched < SPOOLMAN_DB_TTL:
        return _spoolman_db
    try:
        with urllib.request.urlopen(SPOOLMAN_DB_URL, timeout=10) as resp:
            _spoolman_db = json.loads(resp.read())
            _spoolman_db_fetched = time.time()
            print(f"[SpoolmanDB] Loaded {len(_spoolman_db)} filaments")
    except Exception as e:
        print(f"[SpoolmanDB] Fetch failed: {e}")
        if _spoolman_db is None:
            _spoolman_db = []
    return _spoolman_db


def get_spool_density(printer_id: str) -> float:
    """Return the filament density (g/cm³) for the spool assigned to this printer.

    Falls back to the PLA default if Spoolman is unreachable or no spool is assigned.
    Designed to run in a thread pool executor.
    """
    loc = _printer_location(printer_id)
    try:
        base = get_spoolman_url()
        url = f"{base}/api/v1/spool?location={urllib.parse.quote(loc)}"
        with urllib.request.urlopen(_spoolman_request(url), timeout=3) as resp:
            data = json.loads(resp.read())
        if data:
            density = data[0].get("filament", {}).get("density")
            if density and float(density) > 0:
                return float(density)
    except Exception:
        pass
    return FILAMENT_DENSITY


def spoolman_set_location(spool_id: int, printer_id: str) -> None:
    """Set location on a single spool without touching any other spools."""
    loc = _printer_location(printer_id)
    try:
        base = get_spoolman_url()
        req = _spoolman_request(
            f"{base}/api/v1/spool/{spool_id}",
            method="PATCH", body=json.dumps({"location": loc}).encode(),
        )
        urllib.request.urlopen(req, timeout=3).close()
        print(f"[Spoolman] Spool {spool_id} location → {loc}")
    except Exception as e:
        print(f"[Spoolman] Set location skipped ({e})")


def spoolman_assign(printer_id: str, spool_id: int | None) -> None:
    """Assign a spool to a printer in Spoolman (blocking — run in executor).

    Clears the location on any spool currently assigned to this printer, then
    sets location=printer_name on the new spool (if given).
    """
    loc = _printer_location(printer_id)
    try:
        base = get_spoolman_url()
        # Find currently assigned spool and clear it
        url = f"{base}/api/v1/spool?location={urllib.parse.quote(loc)}"
        with urllib.request.urlopen(_spoolman_request(url), timeout=3) as resp:
            current = json.loads(resp.read())
        for s in current:
            if spool_id is None or s["id"] != spool_id:
                req = _spoolman_request(
                    f"{base}/api/v1/spool/{s['id']}",
                    method="PATCH", body=json.dumps({"location": ""}).encode(),
                )
                urllib.request.urlopen(req, timeout=3).close()
        # Assign the new spool
        if spool_id is not None:
            req = _spoolman_request(
                f"{base}/api/v1/spool/{spool_id}",
                method="PATCH", body=json.dumps({"location": loc}).encode(),
            )
            urllib.request.urlopen(req, timeout=3).close()
            print(f"[Spoolman] Spool {spool_id} → {loc}")
    except Exception as e:
        print(f"[Spoolman] Assign skipped ({e})")


def _notify_spool_level(
    result: dict,
    printer_id: str,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Broadcast low/empty spool warnings from a thread-pool executor."""
    remaining = result.get("remaining_weight", 0)
    total     = result.get("initial_weight", 0)
    name      = result.get("filament", {}).get("name") or f"Spool {result.get('id', '?')}"

    if remaining <= 0:
        msg: dict | None = {"type": "spool_empty", "spool": result, "printer_id": printer_id}
    elif total > 0 and (remaining / total) < 0.1:
        msg = {"type": "spool_low", "spool": result, "printer_id": printer_id}
    else:
        msg = None
    if msg:
        asyncio.run_coroutine_threadsafe(broadcast_to_browsers(msg), loop)

    notif = load_notif_settings()
    spool_low_cfg = notif.get("spool_low", {})
    if spool_low_cfg.get("enabled") and remaining > 0:
        threshold = float(spool_low_cfg.get("threshold", 100))
        if remaining <= threshold:
            send_push_all(
                f"Spool almost empty — {name}",
                f"{round(remaining)}g remaining on {printer_id}.",
            )


def spoolman_deduct_spool(
    spool_id: int,
    amount_g: float,
    printer_id: str,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Deduct filament from a specific spool by ID. Runs in a thread-pool executor."""
    try:
        base = get_spoolman_url()
        body = json.dumps({"use_weight": round(amount_g, 1)}).encode()
        req = _spoolman_request(f"{base}/api/v1/spool/{spool_id}/use", method="PUT", body=body)
        with urllib.request.urlopen(req, timeout=3) as resp:
            result = json.loads(resp.read())
        name = result.get("filament", {}).get("name") or f"Spool {spool_id}"
        remaining = result.get("remaining_weight", 0)
        print(f"[Spoolman] {amount_g}g deducted from '{name}' → {remaining}g left")
        _notify_spool_level(result, printer_id, loop)
    except Exception as e:
        print(f"[Spoolman] Deduct skipped for spool {spool_id} ({e})")


def spoolman_deduct(printer_id: str, amount_g: float, loop: asyncio.AbstractEventLoop) -> None:
    """Deduct filament from the spool assigned to this printer's location in Spoolman.

    Fallback for single-colour prints where no per-tray tracking is available.
    Runs in a thread-pool executor.
    """
    loc = _printer_location(printer_id)
    try:
        base = get_spoolman_url()
        url = f"{base}/api/v1/spool?location={urllib.parse.quote(loc)}"
        with urllib.request.urlopen(_spoolman_request(url), timeout=3) as resp:
            data = json.loads(resp.read())
        if not data:
            return
        spool_id = data[0]["id"]
        spoolman_deduct_spool(spool_id, amount_g, printer_id, loop)
    except Exception as e:
        print(f"[Spoolman] Deduct skipped ({e})")
