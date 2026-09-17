"""
routes_fuel.py

Phone-facing routes for fuel_tracking.py's refuel reminder: a status
route the dashboard polls, a Refuel button (zeroes the Wh-since-refuel
counter), and a fuel-type toggle (gasoline/propane).
"""
from flask import Blueprint, jsonify, request

import db
from config import REFUEL_ALERT_WATT_HOURS

fuel_bp = Blueprint("fuel", __name__, url_prefix="/api/fuel")


@fuel_bp.route("/status")
def fuel_status():
    state = db.get_fuel_state()
    return jsonify({
        "wh_since_refuel": round(state["wh_since_refuel"], 0),
        "alert_threshold_wh": REFUEL_ALERT_WATT_HOURS,
        "fuel_type": state["fuel_type"],
        "last_refuel_at": state["last_refuel_at"],
    })


@fuel_bp.route("/refuel", methods=["POST"])
def fuel_refuel():
    db.record_refuel()
    return jsonify({"ok": True})


@fuel_bp.route("/type", methods=["POST"])
def fuel_set_type():
    fuel_type = (request.get_json(silent=True) or {}).get("fuel_type") or request.form.get("fuel_type")
    try:
        db.set_fuel_type(fuel_type)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "fuel_type": fuel_type})
