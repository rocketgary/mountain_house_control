"""
routes_sensors.py

Routes for the raw sensor data: the ESP32 outdoor temperature sensor
(posts readings directly to this app, unlike everything else here
which reads from the shared DB the poller writes to), the EG4
inverter/solar numbers, and the sensor-vs-weather-API comparison.
"""

from datetime import datetime, timezone

from flask import Blueprint, request, jsonify

import db
from config import API_KEY

sensors_bp = Blueprint("sensors", __name__, url_prefix="/api")


@sensors_bp.route("/sensor/mountain-temp", methods=["POST"])
def receive_reading():
    if request.headers.get("X-API-Key") != API_KEY:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True)
    if not data or "temp_f" not in data or "humidity" not in data:
        return jsonify({"error": "expected JSON with temp_f and humidity"}), 400

    conn = db.get_connection()
    conn.execute(
        "INSERT INTO mountain_temp_log (timestamp, temp_f, humidity, hif) VALUES (?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), data["temp_f"], data["humidity"], data["hif"])
    )
    conn.commit()
    conn.close()

    print(f"[mountain-temp] {data['temp_f']}F, {data['humidity']}% humidity, hif={data['hif']}")
    return jsonify({"status": "ok"}), 200


@sensors_bp.route("/sensor/mountain-temp/latest")
def latest_reading():
    conn = db.get_connection()
    row = conn.execute(
        "SELECT timestamp, temp_f, humidity, hif FROM mountain_temp_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()

    if row is None:
        return jsonify({"available": False})

    timestamp, temp_f, humidity, hif = row
    return jsonify({
        "available": True,
        "timestamp": timestamp,
        "temp_f": temp_f,
        "humidity": humidity,
        "hif": hif,
    })


@sensors_bp.route("/solar/latest")
def latest_inverter():
    conn = db.get_connection()
    row = conn.execute(
        """SELECT timestamp, p_pv_1, p_pv_2, soc, v_bat, e_pv_day,
                  p_discharge, e_dischg_day, p_rec, p_import
           FROM inverter_log ORDER BY timestamp DESC LIMIT 1"""
    ).fetchone()
    conn.close()

    if row is None:
        return jsonify({"available": False})

    (timestamp, p_pv_1, p_pv_2, soc, v_bat, e_pv_day,
     p_discharge, e_dischg_day, p_rec, p_import) = row
    return jsonify({
        "available": True,
        "timestamp": timestamp,
        "solar_watts": (p_pv_1 or 0) + (p_pv_2 or 0),
        "soc": soc,
        "v_bat": v_bat,
        "e_pv_day": e_pv_day,
        "discharge_watts": p_discharge or 0,
        "e_dischg_day": e_dischg_day or 0,
        "gen_grid_watts": p_rec or 0,
        # Total power being imported from the generator/grid right now -
        # gen_grid_watts (p_rec) above is only the slice of that charging
        # the battery; this also includes whatever's powering the house
        # directly. Only meaningful while the generator's actually
        # running - reads ~0 otherwise.
        "gen_import_watts": p_import or 0,
    })


@sensors_bp.route("/comparison/latest")
def comparison_latest():
    conn = db.get_connection()
    weather_row = conn.execute(
        "SELECT timestamp, temp_f FROM weather_log ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    sensor_row = conn.execute(
        "SELECT timestamp, temp_f FROM mountain_temp_log ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    conn.close()

    if weather_row is None or sensor_row is None:
        return jsonify({"available": False})

    weather_ts, weather_temp = weather_row
    sensor_ts, sensor_temp = sensor_row
    delta = round(sensor_temp - weather_temp, 1)

    return jsonify({
        "available": True,
        "weather_temp_f": weather_temp,
        "weather_timestamp": weather_ts,
        "sensor_temp_f": sensor_temp,
        "sensor_timestamp": sensor_ts,
        "delta": delta,
    })
