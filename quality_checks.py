"""
quality_checks.py

Just the data-quality-detection logic digest.py needs: flatline runs,
real collection gaps, and a schema map of home_data.db to size each
table's thresholds off its own actual reading cadence.

This is a trimmed extract of house.py + schema.py from the separate
charting-tutorial project (a learning/exploration folder full of
matplotlib chart functions that have nothing to do with the daily
digest). Rather than pull that whole file - and its plotting
dependencies - into this repo, only the three functions digest.py
actually calls (and what they depend on) live here: find_flatlines,
find_gaps, and describe_database. Also has get_generator_runtime_by_day,
a from-scratch addition (not from house.py/schema.py) that reconstructs
daily generator runtime from generator_run_log the same way
mountain_house_control's own db.py does for its "Runtime today" card,
generalized across multiple days so digest.py can compare a day's
runtime against recent history.

Deliberately NOT importing HOUSE_TIMEZONE from this project's own
config.py, even though the value is identical (America/Chicago) - this
file only converts timestamps to local time for display, and importing
config.py would also run its required-env-var checks for things this
script has nothing to do with (the LG ThinQ / generator API keys). This
way digest.py's Task Scheduler run can't fail over an unrelated env var
being unset.
"""
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from load_data import load_table

# Requires `pip install tzdata` on Windows, same as mountain_house_control
# and the original charting-tutorial project both already needed.
HOUSE_TZ = ZoneInfo("America/Chicago")


def load_local(table: str, db_path: str = None, since_days: float | None = None) -> pd.DataFrame:
    """load_table(), with `timestamp` converted from UTC to HOUSE_TZ for display."""
    kwargs = {"since_days": since_days}
    if db_path is not None:
        kwargs["db_path"] = db_path
    df = load_table(table, **kwargs)
    df["timestamp"] = df["timestamp"].dt.tz_convert(HOUSE_TZ)
    return df


def find_flatlines(
    table: str,
    column: str,
    stale_minutes: float = 60,
    since_days: float | None = 30,
) -> pd.DataFrame:
    """
    Group `column` in `table` into runs of consecutive identical values
    and return one row per run: value, start, end, count, duration_min.

    Returns EVERY run, not just the stale ones - filter to
    duration_min >= stale_minutes yourself for just the flagged
    stretches (this is what digest.py's run_quality_checks does).
    """
    df = load_local(table, since_days=since_days)
    series = df.set_index("timestamp")[column]

    same_as_prev = series.eq(series.shift())
    run_id = (~same_as_prev).cumsum()

    runs = pd.DataFrame({"timestamp": series.index, "value": series.values, "run_id": run_id.values})
    runs = runs.groupby("run_id").agg(
        value=("value", "first"),
        start=("timestamp", "first"),
        end=("timestamp", "last"),
        count=("value", "size"),
    )
    runs["duration_min"] = (runs["end"] - runs["start"]).dt.total_seconds() / 60
    return runs.reset_index(drop=True)


def find_gaps(
    table: str,
    column: str,
    gap_hours: float = 2,
    since_days: float | None = 30,
) -> pd.DataFrame:
    """
    Detect real collection gaps in `column`: stretches of `gap_hours`+
    with no readings at all - the failure mode find_flatlines can't see,
    since there's no repeated value to flag, just missing rows.

    Returns one row per gap: gap_start, gap_end, duration_hours
    """
    df = load_local(table, since_days=since_days)
    series = df.set_index("timestamp")[column]

    deltas = series.index.to_series().diff()
    post_gap_points = series.index[deltas > pd.Timedelta(hours=gap_hours)]

    gaps = []
    for gap_end in post_gap_points:
        gap_start = series.index[series.index.get_loc(gap_end) - 1]
        duration_hours = (gap_end - gap_start).total_seconds() / 3600
        gaps.append({"gap_start": gap_start, "gap_end": gap_end, "duration_hours": duration_hours})
    return pd.DataFrame(gaps, columns=["gap_start", "gap_end", "duration_hours"])


def list_tables(db_path: str) -> list[str]:
    """All table names in home_data.db, sqlite's own bookkeeping tables excluded."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def describe_table(table: str, db_path: str) -> dict:
    """
    One table's shape: columns (with dtype), row count, time range, and
    the median gap between consecutive readings - the "normal cadence"
    digest.py sizes its stale/gap thresholds relative to, instead of a
    fixed number per table. A table with no `timestamp` column returns
    those three fields as None rather than raising.
    """
    df = load_local(table, db_path=db_path)

    info = {
        "table": table,
        "row_count": len(df),
        "columns": {col: str(dtype) for col, dtype in df.dtypes.items()},
        "time_start": None,
        "time_end": None,
        "median_interval_seconds": None,
    }

    if "timestamp" in df.columns and len(df) > 1:
        ts = df["timestamp"].sort_values()
        info["median_interval_seconds"] = ts.diff().dt.total_seconds().median()
        info["time_start"] = ts.iloc[0]
        info["time_end"] = ts.iloc[-1]

    return info


def describe_database(db_path: str = None) -> dict:
    """
    The full map: every table in home_data.db, described - what
    digest.py's run_quality_checks() uses to pick each table's
    stale/gap thresholds relative to its own actual reading cadence.
    """
    from load_data import DB_PATH as DEFAULT_DB_PATH
    path = db_path or DEFAULT_DB_PATH
    return {table: describe_table(table, path) for table in list_tables(path)}


def get_generator_runtime_by_day(db_path: str = None, num_days: int = 15) -> list[dict]:
    """
    Reconstructs total generator runtime (hours) for each of the last
    `num_days` COMPLETED local calendar days (America/Chicago) - not
    including today, since today may still be partial depending on when
    digest.py happens to run. Same event-replay approach as
    mountain_house_control's own db.py:get_generator_runtime_today() -
    walking generator_run_log's start/stop rows and totaling the time
    between them, including a run already in progress when the window
    opened - just generalized across multiple days instead of one, and
    splitting any interval that happens to straddle a local midnight
    across both days instead of crediting it all to one.

    Returns a list of {"date": "YYYY-MM-DD", "hours": float}, OLDEST
    FIRST, with exactly num_days entries - a day the generator never ran
    still gets an entry with hours: 0.0, so a quiet day isn't just
    missing from the list (which matters for averaging: a missing entry
    would silently drop out of the count instead of counting as a real
    zero).
    """
    if db_path is None:
        from load_data import DB_PATH as db_path

    now_local = datetime.now(HOUSE_TZ)
    today_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    window_start_local = today_start_local - timedelta(days=num_days)
    window_start_utc = window_start_local.astimezone(timezone.utc)
    today_start_utc = today_start_local.astimezone(timezone.utc)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS generator_run_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                action TEXT NOT NULL
            )
        """)
        last_before = conn.execute(
            """SELECT action, timestamp FROM generator_run_log
               WHERE timestamp < ? ORDER BY timestamp DESC LIMIT 1""",
            (window_start_utc.isoformat(),),
        ).fetchone()
        rows = conn.execute(
            """SELECT action, timestamp FROM generator_run_log
               WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp ASC""",
            (window_start_utc.isoformat(), today_start_utc.isoformat()),
        ).fetchall()
    finally:
        conn.close()

    # Reconstruct (start, end) intervals within the window - a run already
    # in progress when the window opened starts counting from window_start;
    # a run still going when today started is capped at today_start (we
    # only report on completed days, so anything spilling into today is
    # today's business, not this window's).
    intervals = []
    running_since = window_start_utc if (last_before and last_before[0] == "start") else None
    for action, ts_str in rows:
        ts = datetime.fromisoformat(ts_str)
        if action == "start":
            running_since = ts
        elif action == "stop" and running_since is not None:
            intervals.append((running_since, ts))
            running_since = None
    if running_since is not None:
        intervals.append((running_since, today_start_utc))

    # Split each interval across local-midnight boundaries and accumulate
    # seconds per calendar day, so a run that happens to cross midnight is
    # credited proportionally to both days instead of entirely to one.
    day_seconds: dict[str, float] = {}
    for start, end in intervals:
        cur = start
        while cur < end:
            local_day_start = cur.astimezone(HOUSE_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
            next_day_start_utc = (local_day_start + timedelta(days=1)).astimezone(timezone.utc)
            chunk_end = min(end, next_day_start_utc)
            date_key = local_day_start.strftime("%Y-%m-%d")
            day_seconds[date_key] = day_seconds.get(date_key, 0.0) + (chunk_end - cur).total_seconds()
            cur = chunk_end

    results = []
    for i in range(num_days, 0, -1):
        day_local = today_start_local - timedelta(days=i)
        date_key = day_local.strftime("%Y-%m-%d")
        results.append({"date": date_key, "hours": round(day_seconds.get(date_key, 0.0) / 3600, 2)})
    return results
