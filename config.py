"""
config.py

Centralizes environment variables, constants, and the AC/plug unit
definitions used across the app. Load this first (it calls
load_dotenv() at import time) so every other module can just import
the values it needs instead of reading the environment itself.
"""

import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Loads variables from a .env file (in the same folder as this script)
# into the process environment. Without this call, python-dotenv being
# installed does nothing on its own — os.environ.get() below would only
# ever see real OS/session environment variables, never .env file contents.
#
# override=True makes the .env file win if a same-named variable already
# exists elsewhere (a leftover Windows System/User environment variable,
# or a stale PowerShell session variable) — without it, load_dotenv()
# silently skips any variable that's already set, which is a common
# source of "my .env value isn't being used" confusion.
load_dotenv(override=True)

# The house's local timezone, used to figure out where "today" starts for
# the daily energy totals - plug_power_log timestamps are stored in
# UTC, so a plain UTC midnight boundary would attribute a few hours of
# each evening to the wrong day. Requires the `tzdata` package on Windows
# (pip install tzdata) since Windows has no built-in IANA timezone
# database for zoneinfo to read - without it this raises
# ZoneInfoNotFoundError at import time.
HOUSE_TIMEZONE = ZoneInfo("America/Chicago")

# Reuse the same SQLite database as off_grid_house_control's collectors
# (lives in that project's folder, not this one)
DB_PATH = Path(r"C:\AI\Projects\off_grid_house_control\home_data.db")

# Simple shared-secret check on the sensor POST endpoint, loaded from an
# environment variable so it never ends up in git history. Set it either
# via a .env file (same folder as this script, containing a line like
# MOUNTAIN_HOUSE_API_KEY=something-only-you-know — make sure .env is in
# .gitignore, which it already is) or as a real Windows environment
# variable for a permanent setup (Task Scheduler picks those up
# automatically). Put the same value in the ESP32 sketch.
API_KEY = os.environ.get("MOUNTAIN_HOUSE_API_KEY")
if not API_KEY:
    raise RuntimeError(
        "MOUNTAIN_HOUSE_API_KEY environment variable is not set. "
        "Set it before running webapp.py — see the comment above this check."
    )

# ---- Generator remote start ----
# This webapp is the bridge for controlling the generator from outside the
# house: your phone reaches THIS app over Tailscale, and this app in turn
# talks to the ESP32 controller over the regular house WiFi (the ESP32
# itself never joins Tailscale). GENERATOR_IP is the ESP32's current
# house-network IP - since the Starlink router doesn't support DHCP
# reservations, if the generator controls stop responding, check the
# ESP32's Serial Monitor (over USB) for its current IP and update this.
GENERATOR_IP = os.environ.get("GENERATOR_IP", "192.168.1.234")

# Must exactly match API_KEY in the ESP32's generator sketch. Same .env
# pattern as MOUNTAIN_HOUSE_API_KEY above - add a line to your .env file:
# GENERATOR_API_KEY=something-only-you-know
GENERATOR_API_KEY = os.environ.get("GENERATOR_API_KEY")
if not GENERATOR_API_KEY:
    raise RuntimeError(
        "GENERATOR_API_KEY environment variable is not set. "
        "Set it before running webapp.py, matching the API_KEY value in "
        "07_generator_remote_start_prototype.ino."
    )

GENERATOR_REQUEST_TIMEOUT_S = 5  # how long to wait on the ESP32 before
                                  # giving up and reporting it unreachable

# ---- Generator automatic mode thresholds ----
# Plain constants rather than env vars since they're not secrets - edit
# them directly and restart the app if you want to tune them.
GENERATOR_START_SOC = 50       # auto-start once SOC drops to this or below
GENERATOR_MIN_PLAUSIBLE_SOC = 15   # a reading below this is treated as a
                                     # bad/glitched sample rather than a
                                     # real value, and ignored - the
                                     # inverter shuts everything off around
                                     # 20% SOC, so a genuine reading well
                                     # below that basically never happens.
                                     # Added after a bogus 0% reading
                                     # showed up twice in the log and
                                     # auto-started the generator when it
                                     # shouldn't have.
GENERATOR_STOP_WATTS_THRESHOLD = 0   # stop trigger is the Generator/Grid
                                       # Charge reading (p_rec) dropping to
                                       # this or below - not a SOC threshold.
                                       # During equalization/balancing the
                                       # charge controller can hold SOC at
                                       # 99-100% for 30-40 minutes while
                                       # still actively pulling power from
                                       # the generator; cutting the
                                       # generator while that's happening
                                       # causes a power blip as the
                                       # controller switches over (WiFi
                                       # drop, sometimes an AC powering off)
                                       # - waiting for the draw to actually
                                       # hit zero avoids that
GENERATOR_STOP_CONFIRM_S = 3 * 60    # how long the watts reading has to
                                       # stay at/below the threshold before
                                       # it's trusted enough to stop on -
                                       # guards against a single noisy or
                                       # glitched reading
GENERATOR_STOP_SOC_FLOOR = 90        # NOT a gate on stopping - a confirmed
                                       # zero-watts reading always stops the
                                       # generator (see generator_control.py
                                       # for why). This just labels the stop
                                       # as "complete" (SOC reached this) or
                                       # "incomplete" (it didn't) so you can
                                       # tell a normal finish apart from
                                       # something like running out of fuel
                                       # mid-charge
GENERATOR_MIN_RUNTIME_S = 20 * 60     # don't auto-stop before the generator's
                                        # been running at least this long,
                                        # even if it looks done already
GENERATOR_COOLDOWN_AFTER_STOP_S = 15 * 60   # don't auto-start again this
                                              # soon after an auto-stop
GENERATOR_MAX_RUNTIME_S = 6 * 60 * 60   # safety net: auto-stop no matter
                                          # what after this long, in case the
                                          # watts reading never confirms
                                          # "done" (e.g. a sensor hiccup) -
                                          # better a generator that stops on
                                          # its own after 6 hours than one
                                          # that runs unattended indefinitely
AUTOMATION_POLL_INTERVAL_S = 60

# ---- AC units (the AC card) ----
# One shared config drives both the AC routes AND the dashboard's
# table/JS - add, rename, or reorder a unit here and it shows up
# everywhere without touching a table row or writing a new route for it.
#
# "ac_alias" should match the ac_status_log.alias column's meaningful text
# - the actual lookup trims whitespace and ignores case on both sides, so
# exact spacing/capitalization no longer has to match. If a row still
# shows "No data" forever after that, the alias text itself is wrong (or
# stale - a rename means old rows use a different alias than new ones do),
# not just its whitespace/casing - run this to see every alias actually in
# use and when it was last written:
#   SELECT alias, COUNT(*), MAX(timestamp) FROM ac_status_log GROUP BY alias ORDER BY MAX(timestamp) DESC;
#
# "plug_name" matches plug_power_log.plug_name - confirmed for all three
# from a live query, this is what fills in each row's Power column. The
# freezer isn't an AC (see PLUG_UNITS below) so it's not listed here even
# though it shares the same plug_power_log table.
# "device_id" is the LG ThinQ device id (from ac_status_log.ac_id, which
# collect_ac.py already logs alongside each reading) - it's what
# ac_control.py needs to send a command to the right physical unit.
# Confirmed via: SELECT DISTINCT ac_id, alias FROM ac_status_log;
#
# NOTE: these device_id values are specific to your real ACs. They're
# not usable on their own without PAT_TOKEN/X_CLIENT_ID/X_API_KEY (which
# stay in .env, never here) - but if you'd rather not have even these in
# a public repo, move this whole list to a gitignored local_config.py or
# a JSON file instead, same pattern as .env for the credentials.
AC_UNITS = [
    {"key": "main", "label": "Main", "ac_alias": "Main Room Air Conditioner", "plug_name": "Main AC",
     "device_id": "3a6786f582bf76ee9c182160cbd249b4c3c6a2f1ac68bcc175518f04d576a1e0"},
    {"key": "bed",  "label": "Bed",  "ac_alias": "Bedroom",                   "plug_name": "Bedroom AC",
     "device_id": "822bfe73f41876e1bfdcbb32fe851c7dfabec8bdc8927703f665da98ab111080"},
    {"key": "root", "label": "Root", "ac_alias": "Root cellar",               "plug_name": "Root AC",
     "device_id": "d461d7b30bf081374f2ac2a66c16c6bfcc4786353391b45f4024813e6786d34a"},
]

# ---- Standalone power plugs (the Power Plugs card) ----
# Same pattern as AC_UNITS - add a plug here (matching plug_power_log's
# plug_name exactly) and it shows up in the Power Plugs card and counts
# toward the Power Draw card's total automatically, no other changes
# needed. Plugs that power an AC unit belong in AC_UNITS above instead,
# not here, so they aren't listed (and counted) twice.
PLUG_UNITS = [
    {"key": "freezer", "label": "Large Freezer", "plug_name": "Large Freezer"},
]
