"""
routes_power.py

Power-plug routes: per-plug status (the Power Plugs card), the
whole-house running total (the Power Draw card), and today's estimated
energy use per plug (the "Today" column on both the AC and Power
Plugs tables).
"""

from datetime import datetime

from flask import Blueprint, jsonify

import db
from config import PLUG_UNITS, HOUSE_TIMEZONE

power_bp = Blueprint("power", __name__, url_prefix="/api")


@power_bp.route("/plug/<key>/latest")
def latest_plug_unit(key):
    unit = next((u for u in PLUG_UNITS if u["key"] == key), None)
    if unit is None:
        return jsonify({"available": False, "error": "unknown plug"}), 404

    reading = db.get_latest_plug_readings().get(unit["plug_name"])
    if reading is None:
        return jsonify({"available": False})

    return jsonify({
        "available": True,
        "timestamp": reading["timestamp"],
        "power_w": reading["power_w"],
        "is_on": reading["is_on"],
    })


@power_bp.route("/power/total")
def power_total():
    """Whole-house total: sums the latest reading across every plug in
    plug_power_log, AC plugs and standalone plugs alike - not just
    AC_UNITS/PLUG_UNITS, so a plug someone forgets to add to either list
    still counts toward the total (it just won't get its own row on a
    card until it's added there)."""
    readings = db.get_latest_plug_readings()
    if not readings:
        return jsonify({"available": False})

    total_watts = sum(r["power_w"] or 0 for r in readings.values())
    newest_timestamp = max(r["timestamp"] for r in readings.values())

    return jsonify({
        "available": True,
        "total_watts": total_watts,
        "timestamp": newest_timestamp,
        "plug_count": len(readings),
    })


@power_bp.route("/power/daily")
def power_daily():
    by_plug_kwh = db.get_daily_energy_by_plug()
    return jsonify({
        "available": bool(by_plug_kwh),
        "date": datetime.now(HOUSE_TIMEZONE).date().isoformat(),
        "by_plug_kwh": by_plug_kwh,
    })
