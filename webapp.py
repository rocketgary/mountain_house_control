"""
webapp.py

Step 1 of the home automation app: receives temperature/humidity readings
from the ESP32 and serves a simple mobile-friendly page showing the
latest reading. Runs locally for now — reachable from your phone only
while on the same WiFi network as the laptop. Tailscale (step 3 of the
roadmap) will make this reachable from anywhere without any changes here.

Endpoints:
    POST /api/sensor/mountain-temp   -- ESP32 posts readings here
    GET  /api/sensor/mountain-temp/latest -- JSON, used by the page's JS
    GET  /                            -- the dashboard page

Run with:
    python webapp.py

Then, from a phone/browser on the same WiFi, visit:
    http://<laptop's local IP>:5000
(find the laptop's local IP with `ipconfig` in PowerShell, look for
IPv4 Address under your WiFi adapter)
"""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, request, jsonify, render_template_string

# Reuse the same SQLite database as main.py's collectors
DB_PATH = Path(__file__).parent / "house_control.db"  # adjust if your db lives elsewhere

# Simple shared-secret check on the POST endpoint, loaded from an
# environment variable so it never ends up in git history. Set it before
# running the app, e.g. in PowerShell:
#   $env:MOUNTAIN_HOUSE_API_KEY = "something-only-you-know"
# and put the same value in the ESP32 sketch. For a permanent setup (Task
# Scheduler, etc.), set it as a Windows environment variable instead so it
# persists across sessions without needing to be typed each time.
API_KEY = os.environ.get("MOUNTAIN_HOUSE_API_KEY")
if not API_KEY:
    raise RuntimeError(
        "MOUNTAIN_HOUSE_API_KEY environment variable is not set. "
        "Set it before running webapp.py — see the comment above this check."
    )

app = Flask(__name__)


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    return conn


def init_table():
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mountain_temp_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            temp_f REAL,
            humidity REAL
        )
    """)
    conn.commit()
    conn.close()


@app.route("/api/sensor/mountain-temp", methods=["POST"])
def receive_reading():
    if request.headers.get("X-API-Key") != API_KEY:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True)
    if not data or "temp_f" not in data or "humidity" not in data:
        return jsonify({"error": "expected JSON with temp_f and humidity"}), 400

    conn = get_connection()
    conn.execute(
        "INSERT INTO mountain_temp_log (timestamp, temp_f, humidity) VALUES (?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), data["temp_f"], data["humidity"])
    )
    conn.commit()
    conn.close()

    print(f"[mountain-temp] {data['temp_f']}F, {data['humidity']}% humidity")
    return jsonify({"status": "ok"}), 200


@app.route("/api/sensor/mountain-temp/latest")
def latest_reading():
    conn = get_connection()
    row = conn.execute(
        "SELECT timestamp, temp_f, humidity FROM mountain_temp_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()

    if row is None:
        return jsonify({"available": False})

    timestamp, temp_f, humidity = row
    return jsonify({
        "available": True,
        "timestamp": timestamp,
        "temp_f": temp_f,
        "humidity": humidity,
    })


DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Mountain House</title>
    <style>
        body {
            font-family: -apple-system, sans-serif;
            background: #0f172a;
            color: #f1f5f9;
            margin: 0;
            padding: 24px 16px;
            text-align: center;
        }
        h1 { font-size: 1.1rem; color: #94a3b8; font-weight: 500; margin-bottom: 32px; }
        .card {
            background: #1e293b;
            border-radius: 16px;
            padding: 24px;
            margin: 0 auto 16px;
            max-width: 360px;
        }
        .temp { font-size: 3.5rem; font-weight: 700; margin: 8px 0; }
        .humidity { font-size: 1.5rem; color: #94a3b8; }
        .label { font-size: 0.85rem; color: #64748b; text-transform: uppercase; letter-spacing: 0.05em; }
        .updated { font-size: 0.8rem; color: #64748b; margin-top: 16px; }
        .stale { color: #f87171; }
    </style>
</head>
<body>
    <h1>Mountain House Dashboard</h1>
    <div class="card">
        <div class="label">Outdoor Temperature</div>
        <div class="temp" id="temp">--</div>
        <div class="humidity" id="humidity">--</div>
        <div class="updated" id="updated">Loading...</div>
    </div>

    <script>
        async function refresh() {
            try {
                const res = await fetch('/api/sensor/mountain-temp/latest');
                const data = await res.json();
                if (!data.available) {
                    document.getElementById('temp').textContent = 'No data yet';
                    return;
                }
                document.getElementById('temp').textContent = data.temp_f.toFixed(1) + '°F';
                document.getElementById('humidity').textContent = data.humidity.toFixed(0) + '% humidity';

                const ts = new Date(data.timestamp);
                const ageMinutes = (Date.now() - ts) / 60000;
                const updatedEl = document.getElementById('updated');
                updatedEl.textContent = 'Updated ' + ts.toLocaleTimeString();
                updatedEl.className = ageMinutes > 15 ? 'updated stale' : 'updated';
                if (ageMinutes > 15) {
                    updatedEl.textContent += ' (stale — check sensor)';
                }
            } catch (e) {
                document.getElementById('updated').textContent = 'Connection error';
            }
        }
        refresh();
        setInterval(refresh, 30000);
    </script>
</body>
</html>
"""


@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)


if __name__ == "__main__":
    init_table()
    print("Dashboard running. On your phone (same WiFi), visit http://<this laptop's IP>:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)