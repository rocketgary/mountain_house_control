"""
db.py

All direct SQLite access for the dashboard: opening connections,
ensuring the mountain_temp_log table exists, and the read queries used
across the various /api routes. Reads from the same home_data.db that
off_grid_house_control's poller writes to.
"""

import sqlite3
from datetime import datetime, timezone

from config import DB_PATH, HOUSE_TIMEZONE


def get_connection():
    return sqlite3.connect(DB_PATH, timeout=10)


def init_table():
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mountain_temp_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            temp_f REAL,
            humidity REAL,
            hif REAL
        )
    """)

    # Lightweight migration: add any columns that don't exist yet on an
    # older copy of this table (SQLite has no "ADD COLUMN IF NOT EXISTS").
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(mountain_temp_log)")}
    if "hif" not in existing_cols:
        conn.execute("ALTER TABLE mountain_temp_log ADD COLUMN hif REAL")

    # One row per generator start/stop (see log_generator_event below) -
    # lets get_generator_runtime_today() reconstruct how long the
    # generator actually ran today, covering both Manual button presses
    # and Automatic mode, rather than tracking runtime only in memory
    # (which resets on every app restart and only ever covered auto-runs).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS generator_run_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            action TEXT NOT NULL
        )
    """)

    # Written by the separate charting-tutorial project's digest.py (a
    # daily Task Scheduler job, different folder/venv entirely) - not by
    # anything in this app. Created here too, defensively, so this app
    # doesn't 500 on a fresh setup if it happens to start up before
    # digest.py has ever run once - whichever side runs first wins, the
    # other just finds it already there.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_digest_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            summary TEXT NOT NULL,
            finding_count INTEGER
        )
    """)

    conn.commit()
    conn.close()


def get_latest_battery_metrics():
    """Returns (soc, gen_grid_watts) from the most recent inverter_log row,
    or (None, None) if there's no data yet. gen_grid_watts is p_rec - the
    same number shown on the Solar Input card as "Generator/Grid Charge" -
    and is what automation actually watches to decide the generator is
    done: it's the real signal that the charge controller has stopped
    pulling from the generator, unlike SOC which can sit at 99-100% for
    30-40 minutes during equalization/balancing while the generator is
    still running."""
    conn = get_connection()
    row = conn.execute(
        "SELECT soc, p_rec FROM inverter_log ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if row is None:
        return None, None
    soc, gen_grid_watts = row
    return soc, (gen_grid_watts if gen_grid_watts is not None else 0)


def log_generator_event(action):
    """Records one generator start/stop event. generator_control.py's
    send_generator_command() is the single place that calls this - both
    manual button presses (via routes_generator.py) and automation_tick's
    own start/stop decisions go through that one function, so logging
    there (rather than at every call site) captures the generator's real
    runtime regardless of which mode caused it to run."""
    conn = get_connection()
    conn.execute(
        "INSERT INTO generator_run_log (timestamp, action) VALUES (?, ?)",
        (datetime.now(timezone.utc).isoformat(), action),
    )
    conn.commit()
    conn.close()


def get_generator_runtime_today():
    """Returns total seconds the generator has run today (the current
    America/Chicago calendar day), reconstructed from generator_run_log's
    start/stop events rather than tracked live - so it's correct even
    across app restarts and covers Manual-mode runs, not just Automatic.

    Handles a run that was already in progress when today started (looks
    at the last event before midnight to see if it was a "start" with no
    matching "stop" yet) and a run still in progress right now (counts up
    to this moment rather than waiting for a "stop" event that hasn't
    happened yet)."""
    now_local = datetime.now(HOUSE_TIMEZONE)
    start_of_day_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_day_utc = start_of_day_local.astimezone(timezone.utc)

    conn = get_connection()
    last_before_today = conn.execute(
        """SELECT action, timestamp FROM generator_run_log
           WHERE timestamp < ? ORDER BY timestamp DESC LIMIT 1""",
        (start_of_day_utc.isoformat(),),
    ).fetchone()
    rows = conn.execute(
        """SELECT action, timestamp FROM generator_run_log
           WHERE timestamp >= ? ORDER BY timestamp ASC""",
        (start_of_day_utc.isoformat(),),
    ).fetchall()
    conn.close()

    total_seconds = 0.0
    running_since = start_of_day_utc if (last_before_today and last_before_today[0] == "start") else None

    for action, ts_str in rows:
        ts = datetime.fromisoformat(ts_str)
        if action == "start":
            running_since = ts
        elif action == "stop" and running_since is not None:
            total_seconds += (ts - running_since).total_seconds()
            running_since = None

    if running_since is not None:
        # Still running right now - count up to this moment rather than
        # waiting for a "stop" event that hasn't happened yet.
        total_seconds += (datetime.now(timezone.utc) - running_since).total_seconds()

    return total_seconds


def get_latest_plug_readings():
    """Returns {plug_name: {"power_w", "is_on", "timestamp"}} for the most
    recent reading of every distinct plug in plug_power_log - one query
    that covers the Power Plugs card, the AC card's Power column, and the
    Power Draw card's whole-house total, regardless of how many plugs
    exist now or get added later. Grouping by plug_name (rather than just
    taking the single latest timestamp overall) means it still works
    correctly even if different plugs' readings land at slightly
    different times instead of one shared batch timestamp."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT p.plug_name, p.power_w, p.is_on, p.timestamp
        FROM plug_power_log p
        INNER JOIN (
            SELECT plug_name, MAX(timestamp) AS max_ts
            FROM plug_power_log
            GROUP BY plug_name
        ) latest ON p.plug_name = latest.plug_name AND p.timestamp = latest.max_ts
    """).fetchall()
    conn.close()
    return {
        plug_name: {"power_w": power_w, "is_on": bool(is_on), "timestamp": timestamp}
        for plug_name, power_w, is_on, timestamp in rows
    }


def get_daily_energy_by_plug():
    """Estimates each plug's energy used so far today (the current
    America/Chicago calendar day) by numerically integrating the power_w
    readings already sitting in plug_power_log - trapezoidal rule: for
    each pair of consecutive readings, treat power as ramping linearly
    between them and multiply the average by the elapsed time. This is
    an ESTIMATE, not a meter reading - its accuracy depends on how often
    the poller runs (currently roughly every 45s based on observed
    timestamps) - but it needs no changes to the poller or the database.

    We looked at using the plug's own add_ele (DP 17) counter instead,
    since that's presumably closer to what SmartLife shows, but its
    readings didn't scale consistently against SmartLife's displayed
    daily totals across different plugs (ratios of 1.5x-2.4x, not a
    single conversion factor) - consistent with add_ele being a delta
    that's partly drained by whatever polls it, Tuya's own cloud service
    included, rather than a stable "since midnight" total we can read
    once locally and trust. Integrating our own already-logged readings
    sidesteps that entirely.

    Returns {plug_name: kwh_today}. A plug with no readings yet today
    (or only one - integration needs at least two points) is simply
    absent from the dict rather than reported as zero."""
    now_local = datetime.now(HOUSE_TIMEZONE)
    start_of_day_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_day_utc = start_of_day_local.astimezone(timezone.utc)

    conn = get_connection()
    rows = conn.execute(
        """SELECT plug_name, timestamp, power_w FROM plug_power_log
           WHERE timestamp >= ? ORDER BY plug_name, timestamp ASC""",
        (start_of_day_utc.isoformat(),),
    ).fetchall()
    conn.close()

    energy_wh_by_plug = {}
    prev_by_plug = {}   # plug_name -> (timestamp, power_w) of the previous row

    for plug_name, ts_str, power_w in rows:
        ts = datetime.fromisoformat(ts_str)
        power_w = power_w or 0

        if plug_name in prev_by_plug:
            prev_ts, prev_power = prev_by_plug[plug_name]
            elapsed_hours = (ts - prev_ts).total_seconds() / 3600
            if elapsed_hours > 0:
                avg_power = (prev_power + power_w) / 2
                energy_wh_by_plug[plug_name] = energy_wh_by_plug.get(plug_name, 0) + avg_power * elapsed_hours

        prev_by_plug[plug_name] = (ts, power_w)

    return {plug_name: wh / 1000 for plug_name, wh in energy_wh_by_plug.items()}