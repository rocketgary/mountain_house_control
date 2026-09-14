"""
routes_dashboard.py

Serves the dashboard page itself. The actual HTML/CSS/JS lives in
templates/dashboard.html - this just renders it with the AC/plug unit
lists so the page can build its cards and controls dynamically.
"""

from flask import Blueprint, render_template

from config import AC_UNITS, PLUG_UNITS

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/")
def dashboard():
    return render_template("dashboard.html", ac_units=AC_UNITS, plug_units=PLUG_UNITS)
