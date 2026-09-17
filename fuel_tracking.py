"""
fuel_tracking.py

Reminds Gary to refuel the generator's gas tank - not on a timer (draw
varies too much for that to mean anything: roughly 1600-1800W baseline +
whatever the house is using with no solar coming in, 700-800W + house
load once solar picks back up), but by tracking total energy (Wh) the
generator has actually supplied since the last refuel. Wattage pulled
correlates with fuel burned regardless of how that draw happened to be
made up moment to moment, so a running Wh total is a much better proxy
for "how much of the tank is left" than any fixed runtime would be.

Runs as its own APScheduler job (see webapp.py), completely independent
of generator_control.py's automation_tick and load_shedding.py: fuel
gets burned whether the generator was started by hand or by automation,
so this watches actual measured import load regardless of how or why the
generator happens to be running - same reasoning load_shedding.py
already uses for its own independent job.

Accumulates trapezoidally off db.get_latest_import_reading() (p_import -
the SAME total-import reading load_shedding.py watches for overload
protection) each time a genuinely new poller reading arrives, the same
integration approach already used elsewhere in this app (see
db.py:get_daily_energy_by_plug's docstring). Only accumulates while the
currently-selected fuel type is gasoline (see db.set_fuel_type) -
switching to propane pauses the running total rather than resetting it,
since propane draws from a separate, much larger source Gary manages
himself; the gasoline-tank clock just holds wherever it was and picks
back up once he switches back to gasoline.

State persists to generator_fuel_state (db.py) - a webapp restart
mid-accumulation resumes from the same running total and the same
last-counted reading rather than losing progress or double-counting the
gap across the restart, same reasoning as load_shed_state.
"""
from datetime import datetime

import alerts
import db
from config import REFUEL_ALERT_WATT_HOURS


def _hours_between(ts_a: str, ts_b: str) -> float:
    return (datetime.fromisoformat(ts_b) - datetime.fromisoformat(ts_a)).total_seconds() / 3600


def check_fuel_tracking():
    """Call on a short interval (see webapp.py's scheduler). Only acts on a
    genuinely NEW inverter reading, same debounce reasoning as
    load_shedding.py's check_load_shedding - otherwise a scheduler
    checking more often than the poller updates would let the trapezoidal
    step see a zero elapsed time (harmless) or, worse, double-count a
    reading it already folded in."""
    latest = db.get_latest_import_reading()
    if latest is None:
        return
    timestamp, import_watts = latest

    state = db.get_fuel_state()
    if state["last_ts"] == timestamp:
        return  # no new poller data since the last check

    wh_total = state["wh_since_refuel"]
    if state["last_ts"] is not None and state["fuel_type"] == "gasoline":
        elapsed_hours = _hours_between(state["last_ts"], timestamp)
        if elapsed_hours > 0:
            # Trapezoidal: average the two readings' wattage over the
            # elapsed time rather than just multiplying the latest
            # reading by the gap - smooths over a draw that changed
            # partway through (e.g. an AC kicking on) instead of crediting
            # (or blaming) the whole interval to whichever reading came
            # last.
            avg_watts = (state["last_watts"] + import_watts) / 2
            wh_total += avg_watts * elapsed_hours
    # else: first reading ever (no prior point to integrate from yet), or
    # currently on propane - either way, nothing gets added this step,
    # but the bookkeeping below still advances so the next reading has a
    # correct starting point once gasoline tracking is live again.

    alert_sent = state["alert_sent"]
    if not alert_sent and state["fuel_type"] == "gasoline" and wh_total >= REFUEL_ALERT_WATT_HOURS:
        print(f"[fuel_tracking] {wh_total:.0f}Wh supplied since the last refuel (threshold {REFUEL_ALERT_WATT_HOURS}Wh) - sending refuel reminder.")
        alerts.send_alert(
            "Generator needs fuel soon",
            f"The generator has supplied about {wh_total:.0f}Wh since the last refuel "
            f"(alert threshold: {REFUEL_ALERT_WATT_HOURS:.0f}Wh) - worth topping off the tank soon. "
            "Press Refuel on the dashboard once you have.",
            priority=0,
        )
        alert_sent = True

    db.update_fuel_progress(wh_total, timestamp, import_watts, alert_sent)
