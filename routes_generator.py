"""
routes_generator.py

Phone-facing generator routes. These proxy through to
generator_control.py (which in turn talks to the ESP32) rather than
reaching the ESP32 directly - the phone never talks to the ESP32,
only to this webapp.
"""

from flask import Blueprint, request, jsonify

import generator_control

generator_bp = Blueprint("generator", __name__, url_prefix="/api/generator")


@generator_bp.route("/status")
def generator_status():
    status, code = generator_control.get_full_status()
    return jsonify(status), code


@generator_bp.route("/mode")
def generator_get_mode():
    return jsonify({"mode": generator_control.get_mode()})


@generator_bp.route("/mode", methods=["POST"])
def generator_set_mode():
    new_mode = (request.get_json(silent=True) or {}).get("mode") or request.form.get("mode")
    ok, err = generator_control.set_mode(new_mode)
    if not ok:
        return jsonify({"ok": False, "error": err}), 400
    return jsonify({"ok": True, "mode": generator_control.get_mode()})


@generator_bp.route("/start", methods=["POST"])
def generator_start():
    generator_control.manual_override()
    ok, err = generator_control.send_generator_command("start")
    return jsonify({"ok": ok, "error": err}) if not ok else jsonify({"ok": True})


@generator_bp.route("/stop", methods=["POST"])
def generator_stop():
    generator_control.manual_override()
    ok, err = generator_control.send_generator_command("stop")
    return jsonify({"ok": ok, "error": err}) if not ok else jsonify({"ok": True})


@generator_bp.route("/reset", methods=["POST"])
def generator_reset():
    generator_control.manual_override()
    ok, err = generator_control.send_generator_command("reset")
    return jsonify({"ok": ok, "error": err}) if not ok else jsonify({"ok": True})
