"""
digest.py

Scheduled digest: checks every numeric sensor column across every table
in home_data.db for data-quality issues (stale/flatlined readings, real
collection gaps), then asks Claude to turn the raw findings into a short
plain-English summary. Meant to run once a day via Windows Task
Scheduler, same pattern as this project's own MountainHouseWebapp task
and off_grid_house_control's House_Polling task — a daily trigger
pointed at this venv's python.exe with this script as the argument.

Lives in mountain_house_control (not the separate charting-tutorial
project it started in) because that's what it actually is now: a
producer for this dashboard's Daily Digest card, not a standalone
exploration script. quality_checks.py alongside it is a trimmed extract
of that other project's house.py/schema.py — just the flatline/gap
detection and schema-introspection logic this file calls, without
dragging in a bunch of unrelated matplotlib chart functions.

Deliberately NOT built as a tool-calling agent like agent.py. There's no
decision to make here — it's the same fixed checks every run — so this
calls quality_checks.py's functions directly and only hands Claude the
results to phrase, rather than letting it choose what to check. Simpler,
cheaper, and nothing for it to skip or get wrong about which check to run.

Output goes to the console (for a manual run), a timestamped file under
digests/ (because a Task Scheduler run — especially a hidden one, like
your other house tasks — has no console for you to see), AND home_data.db
(daily_digest_log) — that last one is what lets this dashboard show the
latest digest via routes_sensors.py's /api/digest/latest, reading the
same shared DB every other card here already reads from.

Setup: pip install anthropic (in mountain_house_control's own venv,
alongside Flask/waitress/etc.), ANTHROPIC_API_KEY set as an environment
variable. Point the existing daily Task Scheduler task at this venv's
python.exe and this file's new path once it's moved over.

Run manually:
    python digest.py
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

import anthropic
import pandas as pd

from load_data import DB_PATH
from quality_checks import describe_database, find_flatlines, find_gaps, get_generator_runtime_by_day

MODEL = "claude-sonnet-4-5"  # check docs.claude.com/en/docs/about-claude/models

# How many of the preceding completed days to average together as the
# "normal" baseline a day's generator runtime gets compared against.
GENERATOR_HISTORY_DAYS = 14

# If a day's runtime differs from that baseline average by at least this
# fraction, it's worth Claude calling out explicitly rather than just
# stating the number - could mean the automation logic misfired, Gary
# left it in Manual and forgot about it, it ran out of fuel early, an
# unusually cloudy stretch needed more generator charging than normal, etc.
GENERATOR_DEVIATION_THRESHOLD = 0.5

# Tables to skip entirely. weather_log is the closest weather-API source
# Gary could get (down in the valley, not the mountain site itself) —
# it's only there for comparison against mountain_temp_log, the real
# on-site sensor, so it's not something worth data-quality-checking on
# its own.
SKIP_TABLES = {"weather_log"}

# Columns that exist in home_data.db but aren't real sensor readings, or
# are EXPECTED to sit unchanged for long stretches (an on/off flag, a
# thermostat setpoint) — flagging those as "stale" would just be noise.
# You'll likely need to extend this after your first real run once you
# see what else gets flagged that shouldn't (e.g. an AC's target
# temperature column, if ac_status_log has one — I don't have that
# table's full schema in front of me to guess at it correctly).
SKIP_COLUMNS = {"id", "timestamp", "is_on"}

# (table, column) pairs where a flatline AT EXACTLY ZERO is expected and
# normal, not a data-quality issue — solar panels (both power AND
# voltage - two separate arrays in different spots, so they don't
# necessarily drop to 0 at exactly the same moment in the morning/
# evening, but each one legitimately reads 0 overnight) and battery
# discharge reads 0 whenever the generator is supplying the house
# directly instead of the battery. A flatline at any OTHER value (e.g.
# p_pv_1 stuck at 45W at 2pm, or v_pv_2 still showing voltage at 2am) is
# still flagged — only the known-normal zero case is excluded.
#
# Tradeoff worth knowing: this can't distinguish "legitimately not
# discharging" from "discharge sensor stuck reporting 0 while the
# battery is actually discharging" — both look identical to this check.
# If that distinction ever matters, it'd need cross-checking against a
# related column (e.g. SOC actually dropping) rather than looking at
# p_discharge alone.
ZERO_IS_NORMAL_COLUMNS = {
    ("inverter_log", "p_pv_1"),
    ("inverter_log", "p_pv_2"),
    ("inverter_log", "v_pv_1"),
    ("inverter_log", "v_pv_2"),
    ("inverter_log", "p_discharge"),
}

# Cumulative "since local midnight" running totals (e_pv_day, e_chg_day,
# e_dischg_day, e_eps_day, and anything else following the same naming
# convention) are fundamentally different from a live sensor reading:
# holding perfectly flat is the expected DEFAULT state any time there's
# no incremental activity to add, not a sign of anything stuck. Gary's
# own example: once SOC hits 100% and the battery stops taking a charge
# overnight, e_chg_day legitimately holds at the same value for hours -
# 3-5 right now, and it'll stretch to the full sunset-to-sunrise window
# once it's cool enough that the AC load isn't pulling it back down
# below 100% partway through the night. Matched by name suffix rather
# than a hardcoded list so a future *_day column (e.g. if per-string PV
# totals get split out) is automatically covered without another edit
# here.
#
# This only exempts these columns from the STALE/FLATLINE check, not
# from find_gaps below - a real collection failure (the inverter itself
# going unreachable for hours) still shows up as missing rows regardless
# of which column it happens to be, so that failure mode stays caught.
def _is_daily_accumulator_column(column: str) -> bool:
    return column.endswith("_day")


# Per-(table, column) overrides for stale_minutes, when the generic
# STALE_MULTIPLIER-derived threshold (sized off the table's overall
# reading cadence) is too tight for how a SPECIFIC column actually
# behaves. v_bat: whenever solar production and house load happen to
# closely match (net current into/out of the battery near zero), the
# reading can legitimately hold steady for a while even though nothing's
# wrong - Gary's data showed this 64 separate times, topping out at ~16
# minutes. 45 minutes gives comfortable headroom above that normal
# variation while still catching a genuinely stuck voltage sensor (which
# would hold for hours, not tens of minutes).
STALE_MINUTES_OVERRIDES = {
    ("inverter_log", "v_bat"): 45,
}

# How many multiples of a table's own median reading interval count as
# "stuck" vs. "a real gap" — same idea as the thresholds you picked by
# hand in house.py, just computed relative to each table's ACTUAL
# cadence (from schema.py) instead of one number guessed per table.
STALE_MULTIPLIER = 6  # e.g. weather_log's ~10 min cadence -> 60 min stale threshold
GAP_MULTIPLIER = 12   # e.g. weather_log's ~10 min cadence -> 2 hour gap threshold


def run_quality_checks(since_days: float = 1) -> list[dict]:
    """
    Check every numeric column in every table for stale runs and real
    gaps, sizing each table's thresholds off its own median reading
    interval instead of a hand-picked number.

    Returns one finding dict per (table, column) that actually has
    something worth flagging — a clean sensor doesn't appear at all, so
    this list is short (often empty) on an ordinary day.
    """
    db = describe_database()
    findings = []

    for table, info in db.items():
        if table in SKIP_TABLES:
            continue

        interval_sec = info["median_interval_seconds"]
        if interval_sec is None or interval_sec <= 0:
            continue  # no timestamp column, or not enough rows to tell

        stale_minutes = max(1, (interval_sec * STALE_MULTIPLIER) / 60)
        gap_hours = max(0.1, (interval_sec * GAP_MULTIPLIER) / 3600)

        numeric_columns = [
            col for col, dtype in info["columns"].items()
            if col not in SKIP_COLUMNS and ("float" in dtype or "int" in dtype)
        ]

        for column in numeric_columns:
            effective_stale_minutes = STALE_MINUTES_OVERRIDES.get((table, column), stale_minutes)

            if _is_daily_accumulator_column(column):
                # A cumulative daily total holding flat is the normal
                # resting state whenever there's no incremental activity,
                # not a data-quality issue - see the comment above
                # _is_daily_accumulator_column. Skip the flatline check
                # entirely for these; find_gaps below still runs
                # normally, so an actual collection failure is still caught.
                stale_runs = pd.DataFrame(columns=["value", "start", "end", "count", "duration_min"])
            else:
                runs = find_flatlines(table, column, effective_stale_minutes, since_days)
                stale_runs = runs[runs["duration_min"] >= effective_stale_minutes]

                if (table, column) in ZERO_IS_NORMAL_COLUMNS:
                    # a flatline at 0 is expected here (night, or generator running) —
                    # only a flatline at a nonzero value is actually suspicious
                    stale_runs = stale_runs[stale_runs["value"].abs() > 1e-9]

            gaps = find_gaps(table, column, gap_hours, since_days)

            if len(stale_runs) == 0 and len(gaps) == 0:
                continue

            # The value it was actually stuck at, for the longest stale run -
            # without this, Claude can't tell "flatlined at 0" (usually fine,
            # though ZERO_IS_NORMAL_COLUMNS already screens most of those out
            # before we even get here) apart from "flatlined at some nonzero
            # reading that shouldn't be constant" (the actually interesting
            # case, e.g. a solar voltage sensor stuck reporting power
            # overnight). Only computed when there IS a stale run - a
            # (table, column) can also land here purely on a gap.
            longest_stale_value = None
            if len(stale_runs):
                longest_stale_value = round(float(stale_runs.loc[stale_runs["duration_min"].idxmax(), "value"]), 2)

            findings.append({
                "table": table,
                "column": column,
                "stale_minutes_threshold": round(effective_stale_minutes, 1),
                "gap_hours_threshold": round(gap_hours, 2),
                "stale_run_count": len(stale_runs),
                "longest_stale_minutes": round(stale_runs["duration_min"].max(), 1) if len(stale_runs) else 0,
                "longest_stale_value": longest_stale_value,
                "gap_count": len(gaps),
                "longest_gap_hours": round(gaps["duration_hours"].max(), 1) if len(gaps) else 0,
            })

    return findings


def check_generator_runtime(db_path: str = DB_PATH) -> dict:
    """
    Generator runtime for the most recently completed local calendar day
    ("yesterday" - not "today", since today may still be partial
    depending on when this script happens to run) vs. the average of the
    GENERATOR_HISTORY_DAYS days before that.

    This isn't a stale/flatline/gap check like run_quality_checks - the
    generator's own on/off event log doesn't have that failure mode, and
    Gary wants this reported every single day regardless of whether
    anything looks off, not just when something's flagged (see
    write_digest, which always includes it). notably_different just
    marks whether it's worth Claude calling out explicitly.
    """
    history = get_generator_runtime_by_day(db_path, num_days=GENERATOR_HISTORY_DAYS + 1)
    yesterday = history[-1]
    previous_days = history[:-1]

    previous_hours = [d["hours"] for d in previous_days]
    avg_previous = round(sum(previous_hours) / len(previous_hours), 2) if previous_hours else None

    notably_different = False
    if avg_previous:  # nonzero and not None
        notably_different = abs(yesterday["hours"] - avg_previous) / avg_previous >= GENERATOR_DEVIATION_THRESHOLD
    elif avg_previous == 0 and yesterday["hours"] > 0:
        # hasn't run at all in recent history, but ran yesterday - worth a mention
        notably_different = True

    return {
        "date": yesterday["date"],
        "hours": yesterday["hours"],
        "avg_previous_hours": avg_previous,
        "days_compared": len(previous_days),
        "notably_different": notably_different,
    }


def save_digest_to_db(summary: str, finding_count: int) -> None:
    """Writes the digest into home_data.db (daily_digest_log) using the
    same DB_PATH load_data.py already resolves (real path or the
    HOME_DATA_DB env var override) — so this always points at the same
    database as every other check in this file, and the
    mountain_house_control dashboard can read the latest digest with a
    plain query instead of reaching across the filesystem into this
    project's own digests/ folder.

    CREATE TABLE IF NOT EXISTS here (rather than assuming
    mountain_house_control created it first) means this script doesn't
    care which side runs first on a fresh setup - whichever runs first
    creates the table, the other just uses it."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_digest_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            summary TEXT NOT NULL,
            finding_count INTEGER
        )
    """)
    conn.execute(
        "INSERT INTO daily_digest_log (timestamp, summary, finding_count) VALUES (?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), summary, finding_count),
    )
    conn.commit()
    conn.close()


def write_digest(since_days: float = 1) -> str:
    """Run the checks, get a plain-English summary from Claude, print it, save it to a file, and log it to the DB."""
    findings = run_quality_checks(since_days)
    generator = check_generator_runtime()
    client = anthropic.Anthropic()

    generator_fact = f"Generator ran {generator['hours']:.1f} hour(s) on {generator['date']}."
    if generator["avg_previous_hours"] is not None:
        generator_fact += (
            f" That compares to an average of {generator['avg_previous_hours']:.1f} hour(s)/day "
            f"over the {generator['days_compared']} days before that"
            + (
                " — notably different from that recent pattern, worth calling out explicitly."
                if generator["notably_different"]
                else " — in line with recent history."
            )
        )
    else:
        generator_fact += " No prior days' history yet to compare it against."

    # Report generator runtime every day regardless of whether anything else
    # is flagged - this is a fact Gary wants included in every digest, not
    # just something surfaced when it looks wrong.
    if not findings:
        prompt = (
            f"Every sensor in home_data.db looked normal over the last {since_days:g} day(s) — "
            "no stale/flatlined readings and no real collection gaps beyond each table's own "
            f"normal reading cadence.\n\n{generator_fact}\n\n"
            "Write a short, casual summary (a couple of sentences) that covers both things: that "
            "the sensors look fine, and the generator runtime fact above - mention plainly if it's "
            "flagged as notably different from recent days, otherwise just note the runtime in passing."
        )
    else:
        prompt = (
            f"Here are data-quality findings from the last {since_days:g} day(s) of Gary's "
            "off-grid house sensor data (home_data.db). Each entry is a table/column where "
            "something was flagged — stale/flatlined readings and/or real collection gaps — using "
            "a threshold set relative to that table's own normal reading interval, not a fixed "
            "number. `longest_stale_value` is what the reading was actually stuck at during its "
            "longest stale run - a solar power/voltage column stuck at 0 is usually just nighttime "
            "(already filtered out where we know that's normal), but stuck at a nonzero value for "
            "hours (e.g. a voltage reading that shouldn't hold steady overnight) is the genuinely "
            "suspicious case and should be called out specifically with that value, not just "
            "described generically as 'flatlined'.\n\n"
            f"{generator_fact}\n\n"
            "Write a short, plain-English summary (a few sentences, not a bulleted report) someone "
            "would actually want to read over coffee: which sensors need a look and why (referencing "
            "the actual stuck value where it's the interesting part), roughly how bad it is, the "
            "generator runtime fact above, and skip anything genuinely minor.\n\n"
            f"{json.dumps(findings, indent=2)}"
        )

    response = client.messages.create(model=MODEL, max_tokens=400, messages=[{"role": "user", "content": prompt}])
    summary = "".join(block.text for block in response.content if block.type == "text")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs("digests", exist_ok=True)
    digest_path = f"digests/digest_{stamp}.txt"
    with open(digest_path, "w", encoding="utf-8") as f:
        f.write(f"House data digest — {datetime.now():%Y-%m-%d %I:%M %p}\n\n{summary}\n")

    finding_count = len(findings) + (1 if generator["notably_different"] else 0)
    save_digest_to_db(summary, finding_count)

    print(summary)
    print(f"\nSaved {digest_path}")
    return summary


if __name__ == "__main__":
    write_digest(since_days=1)
