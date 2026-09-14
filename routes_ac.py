"""
routes_ac.py

Status and control routes for the three LG ThinQ air conditioners.
Reads climate status and plug power from the DB (populated by the
poller) and proxies control commands through to ac_control.py, which
talks to the LG ThinQ API directly.
"""

from flask import Blueprint, request, jsonify

import ac_control
import db
from config import AC_UNITS

ac_bp = Blueprint("ac", __name__, url_prefix="/api/ac")


def _find_ac_unit(key):
    return next((u for u in AC_UNITS if u["key"] == key), None)


def _ac_command(action_name, fn, *args):
    """Runs one LG ThinQ control call, converting any exception (API
    down/unreachable, bad token, device offline, an invalid mode/fan/temp
    value) into an (ok, error_message) pair, so a bad button press or a
    flaky API call surfaces as a message on the dashboard instead of a
    500."""
    try:
        fn(*args)
        return True, None
    except Exception as e:
        return False, f"Could not {action_name}: {e}"


@ac_bp.route("/<key>/latest")
def latest_ac_unit(key):
    unit = _find_ac_unit(key)
    if unit is None:
        return jsonify({"available": False, "error": "unknown AC unit"}), 404

    conn = db.get_connection()
    row = conn.execute(
        """SELECT timestamp, current_temp_f, target_temp_f, mode, fan_speed, power_status
           FROM ac_status_log
           WHERE TRIM(alias) = TRIM(?) COLLATE NOCASE
           ORDER BY timestamp DESC LIMIT 1""",
        (unit["ac_alias"],),
    ).fetchone()
    conn.close()

    plug_reading = db.get_latest_plug_readings().get(unit["plug_name"])
    power_w = plug_reading["power_w"] if plug_reading else None

    if row is None:
        # No climate data, but there might still be a power reading for
        # this AC's plug - include it rather than leaving the Power
        # column blank just because ac_status_log came up empty.
        return jsonify({"available": False, "power_w": power_w})

    timestamp, current_temp_f, target_temp_f, mode, fan_speed, power_status = row

    # "mode" is returned as the raw LG API value (COOL/FAN/AIR_DRY/
    # ENERGY_SAVING) since the control panel's mode dropdown needs that
    # exact value - the friendlier "ECO" label is applied client-side
    # just for display, same idea as the fan/power labels.
    return jsonify({
        "available": True,
        "timestamp": timestamp,
        "current_temp_f": current_temp_f,
        "target_temp_f": target_temp_f,
        "mode": mode,
        "fan_speed": fan_speed,
        "power_status": power_status,
        "power_w": power_w,
    })


@ac_bp.route("/<key>/power", methods=["POST"])
def ac_power(key):
    unit = _find_ac_unit(key)
    if unit is None:
        return jsonify({"ok": False, "error": "unknown AC unit"}), 404
    turn_on = bool((request.get_json(silent=True) or {}).get("on"))
    fn = ac_control.power_on if turn_on else ac_control.power_off
    ok, err = _ac_command("change power state", fn, unit["device_id"])
    return jsonify({"ok": ok, "error": err}) if not ok else jsonify({"ok": True})


@ac_bp.route("/<key>/temp", methods=["POST"])
def ac_temp(key):
    unit = _find_ac_unit(key)
    if unit is None:
        return jsonify({"ok": False, "error": "unknown AC unit"}), 404
    data = request.get_json(silent=True) or {}
    try:
        temp_f = float(data.get("temp_f"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "temp_f must be a number"}), 400
    ok, err = _ac_command("set temperature", ac_control.set_temperature_f, unit["device_id"], temp_f)
    return jsonify({"ok": ok, "error": err}) if not ok else jsonify({"ok": True})


@ac_bp.route("/<key>/mode", methods=["POST"])
def ac_mode(key):
    unit = _find_ac_unit(key)
    if unit is None:
        return jsonify({"ok": False, "error": "unknown AC unit"}), 404
    mode = (request.get_json(silent=True) or {}).get("mode", "")
    ok, err = _ac_command("set mode", ac_control.set_mode, unit["device_id"], mode)
    return jsonify({"ok": ok, "error": err}) if not ok else jsonify({"ok": True})


@ac_bp.route("/<key>/fan", methods=["POST"])
def ac_fan(key):
    unit = _find_ac_unit(key)
    if unit is None:
        return jsonify({"ok": False, "error": "unknown AC unit"}), 404
    speed = (request.get_json(silent=True) or {}).get("speed", "")
    ok, err = _ac_command("set fan speed", ac_control.set_fan_speed, unit["device_id"], speed)
    return jsonify({"ok": ok, "error": err}) if not ok else jsonify({"ok": True})
