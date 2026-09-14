"""
webapp.py

Entry point for the Mountain House dashboard. Wires together the route
blueprints, starts the generator automation background job, and serves
the app. See db.py, generator_control.py, and the routes_*.py files for
the actual logic - this file just assembles them.

Run with:
    python webapp.py

Then, from a phone/browser on the same WiFi (or over Tailscale), visit:
    http://<this laptop's IP>:5000
(find the laptop's local IP with `ipconfig` in PowerShell, look for
IPv4 Address under your WiFi adapter)
"""

from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler

import db
import generator_control
from config import AUTOMATION_POLL_INTERVAL_S
from routes_dashboard import dashboard_bp
from routes_generator import generator_bp
from routes_ac import ac_bp
from routes_power import power_bp
from routes_sensors import sensors_bp

app = Flask(__name__)
app.register_blueprint(dashboard_bp)
app.register_blueprint(generator_bp)
app.register_blueprint(ac_bp)
app.register_blueprint(power_bp)
app.register_blueprint(sensors_bp)


if __name__ == "__main__":
    db.init_table()

    scheduler = BackgroundScheduler()
    scheduler.add_job(generator_control.automation_tick, "interval", seconds=AUTOMATION_POLL_INTERVAL_S)
    scheduler.start()
    print(f"Generator automation loop running in the background (checks every {AUTOMATION_POLL_INTERVAL_S}s, starts in Manual mode).")

    from waitress import serve
    print("Dashboard running in the background. On your phone (same WiFi or Tailscale), visit http://<this laptop's IP>:5000")
    serve(app, host="0.0.0.0", port=5000, threads=8)
