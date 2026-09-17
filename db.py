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

    # Which AC unit(s) load_shedding.py has currently turned off to protect
    # the generator from tripping its own overload cutoff - a row present
    # means that unit is shed BY AUTOMATION specifically, not just "off"
    # (an AC turned off by hand for an unrelated reason never gets a row
    # here, so restoring it isn't this app's business). shed_order is what
    # lets get_load_shed_units() report "most recently shed" so units come
    # back on in reverse order rather than all at once. Persisted here
    # (rather than kept only in memory, like the generator's own Manual/
    # Automatic toggle) specifically so a webapp restart mid-shed doesn't
    # strand an AC off with no way to know it should come back on later.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS load_shed_state (
            ac_key TEXT PRIMARY KEY,
            shed_at TEXT NOT NULL,
            shed_order INTEGER NOT NULL
        )
    """)

    # Single-row table (id is always 1) tracking fuel_tracking.py's running
    # Wh-since-refuel total - persisted (not just in memory) so a webapp
    # restart mid-accumulation resumes from the same total and the same
    # last-counted reading instead of losing progress or double-counting
    # the gap across the restart. last_ts/last_watts are the previous
    # inverter_log reading already folded into wh_since_refuel, needed for
    # the next trapezoidal step; fuel_type gates whether new readings
    # accumulate at all (see fuel_tracking.py); alert_sent guards against
    # re-sending the "time to refuel" notice on every poll once past
    # threshold.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS generator_fuel_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            wh_since_refuel REAL NOT NULL DEFAULT 0,
            last_ts TEXT,
            last_watts REAL,
            fuel_type TEXT NOT NULL DEFAULT 'gasoline',
            alert_sent INTEGER NOT NULL DEFAULT 0,
            last_refuel_at TEXT
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


def get_latest_import_reading():
    """Returns (timestamp, gen_import_watts) from the most recent
    inverter_log row, or None if there's no data yet. load_shedding.py
    uses the timestamp (not just the wattage) to tell whether a check has
    already seen this exact reading before - so its debounce counts
    actual distinct poller readings, not how many times its own scheduler
    happens to check in between."""
    conn = get_connection()
    row = conn.execute(
        "SELECT timestamp, p_import FROM inverter_log ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if row is None:
        return None
    timestamp, p_import = row
    return timestamp, (p_import or 0)


def get_load_shed_units():
    """Returns the ac_key values currently shed by load_shedding.py,
    oldest-shed first - so units[-1] is the most recently shed one,
    which is what gets restored first once import drops."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT ac_key FROM load_shed_state ORDER BY shed_order ASC"
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def record_load_shed(ac_key):
    """Marks ac_key as currently shed by automation. INSERT OR REPLACE so
    calling this again for a unit that's somehow already marked (shouldn't
    normally happen - load_shedding.py checks first) just refreshes its
    shed_at rather than erroring or creating a duplicate."""
    conn = get_connection()
    max_order = conn.execute("SELECT MAX(shed_order) FROM load_shed_state").fetchone()[0] or 0
    conn.execute(
        "INSERT OR REPLACE INTO load_shed_state (ac_key, shed_at, shed_order) VALUES (?, ?, ?)",
        (ac_key, datetime.now(timezone.utc).isoformat(), max_order + 1),
    )
    conn.commit()
    conn.close()


def clear_load_shed(ac_key):
    """Marks ac_key as no longer shed by automation (it's been restored).
    A no-op if it wasn't marked in the first place."""
    conn = get_connection()
    conn.execute("DELETE FROM load_shed_state WHERE ac_key = ?", (ac_key,))
    conn.commit()
    conn.close()


def get_fuel_state():
    """Returns fuel_tracking.py's current state as a dict - creating the
    default row (0 Wh so far, on gasoline, no reading counted yet) the
    very first time this is called on a fresh setup, so callers never
    have to special-case "no row yet" themselves."""
    conn = get_connection()
    row = conn.execute(
        "SELECT wh_since_refuel, last_ts, last_watts, fuel_type, alert_sent, last_refuel_at FROM generator_fuel_state WHERE id = 1"
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO generator_fuel_state (id, wh_since_refuel, fuel_type, alert_sent) VALUES (1, 0, 'gasoline', 0)"
        )
        conn.commit()
        row = (0, None, None, "gasoline", 0, None)
    conn.close()
    return {
        "wh_since_refuel": row[0],
        "last_ts": row[1],
        "last_watts": row[2],
        "fuel_type": row[3],
        "alert_sent": bool(row[4]),
        "last_refuel_at": row[5],
    }


def update_fuel_progress(wh_since_refuel, last_ts, last_watts, alert_sent):
    """Called once per genuinely new inverter_log reading by
    fuel_tracking.py's check_fuel_tracking() - advances the running total
    and the bookkeeping needed for the next reading's trapezoidal step."""
    conn = get_connection()
    conn.execute(
        "UPDATE generator_fuel_state SET wh_since_refuel = ?, last_ts = ?, last_watts = ?, alert_sent = ? WHERE id = 1",
        (wh_since_refuel, last_ts, last_watts, int(alert_sent)),
    )
    conn.commit()
    conn.close()


def set_fuel_type(fuel_type):
    """Switches which fuel the generator is currently reported as running
    on. Doesn't touch wh_since_refuel either way - switching TO propane
    just means fuel_tracking.py stops adding to the gasoline-tank total
    from here on (see its check_fuel_tracking()); switching back to
    gasoline resumes adding to that same total right where it left off,
    since the gasoline tank's actual level didn't change while running on
    propane."""
    if fuel_type not in ("gasoline", "propane"):
        raise ValueError("fuel_type must be 'gasoline' or 'propane'")
    get_fuel_state()  # ensure the row exists first
    conn = get_connection()
    conn.execute("UPDATE generator_fuel_state SET fuel_type = ? WHERE id = 1", (fuel_type,))
    conn.commit()
    conn.close()


def record_refuel():
    """Call when Gary presses the dashboard's Refuel button after actually
    topping off the tank: zeroes the Wh-since-refuel counter and clears
    the alert flag so a future threshold-crossing can alert again.
    last_ts/last_watts are deliberately left alone - they're just the
    most recent inverter reading already seen, needed so the very next
    poller reading continues the trapezoidal integration correctly
    instead of restarting cold with no prior point to integrate from."""
    get_fuel_state()  # ensure the row exists first
    conn = get_connection()
    conn.execute(
        "UPDATE generator_fuel_state SET wh_since_refuel = 0, alert_sent = 0, last_refuel_at = ? WHERE id = 1",
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.commit()
    conn.close()
