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
import fuel_tracking
import generator_control
import load_shedding
from config import AUTOMATION_POLL_INTERVAL_S
from routes_dashboard import dashboard_bp
from routes_generator import generator_bp
from routes_ac import ac_bp
from routes_power import power_bp
from routes_sensors import sensors_bp
from routes_loadshed import loadshed_bp
from routes_fuel import fuel_bp

app = Flask(__name__)
app.register_blueprint(dashboard_bp)
app.register_blueprint(generator_bp)
app.register_blueprint(ac_bp)
app.register_blueprint(power_bp)
app.register_blueprint(sensors_bp)
app.register_blueprint(loadshed_bp)
app.register_blueprint(fuel_bp)


# How often to check for an overload condition - independent of (and much
# shorter than) AUTOMATION_POLL_INTERVAL_S above, since this is a safety
# check rather than a convenience automation. The interval itself doesn't
# affect how many "consecutive readings" load_shedding.py sees, though -
# it dedupes on the inverter reading's own timestamp, so checking this
# often just means it reacts within seconds of a new poller reading
# landing, not that it double-counts the same reading.
LOAD_SHED_POLL_INTERVAL_S = 15

# Same idea as LOAD_SHED_POLL_INTERVAL_S - fuel_tracking.py dedupes on the
# inverter reading's own timestamp, so checking this often just means it
# picks up a new poller reading (roughly every 45s) within seconds rather
# than double-counting anything.
FUEL_POLL_INTERVAL_S = 30

if __name__ == "__main__":
    db.init_table()

    scheduler = BackgroundScheduler()
    scheduler.add_job(generator_control.automation_tick, "interval", seconds=AUTOMATION_POLL_INTERVAL_S)
    scheduler.add_job(load_shedding.check_load_shedding, "interval", seconds=LOAD_SHED_POLL_INTERVAL_S)
    scheduler.add_job(fuel_tracking.check_fuel_tracking, "interval", seconds=FUEL_POLL_INTERVAL_S)
    scheduler.start()
    print(f"Generator automation loop running in the background (checks every {AUTOMATION_POLL_INTERVAL_S}s, starts in Manual mode).")
    print(f"Generator overload protection running in the background (checks every {LOAD_SHED_POLL_INTERVAL_S}s).")
    print(f"Generator refuel tracking running in the background (checks every {FUEL_POLL_INTERVAL_S}s).")

    from waitress import serve
    print("Dashboard running in the background. On your phone (same WiFi or Tailscale), visit http://<this laptop's IP>:5000")
    serve(app, host="0.0.0.0", port=5000, threads=8)
