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
