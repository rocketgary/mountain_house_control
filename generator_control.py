"""
generator_control.py

Talks to the ESP32 generator controller over the house WiFi, and runs
the automatic start/stop decision logic. The phone-facing routes in
routes_generator.py never talk to the ESP32 directly - they go through
the functions here, which is also what the background automation_tick
job (scheduled from webapp.py) calls on a timer.
"""

import threading
import time

import requests

from config import (
    GENERATOR_IP,
    GENERATOR_API_KEY,
    GENERATOR_REQUEST_TIMEOUT_S,
    GENERATOR_START_SOC,
    GENERATOR_MIN_PLAUSIBLE_SOC,
    GENERATOR_STOP_WATTS_THRESHOLD,
    GENERATOR_STOP_CONFIRM_S,
    GENERATOR_STOP_SOC_FLOOR,
    GENERATOR_MIN_RUNTIME_S,
    GENERATOR_COOLDOWN_AFTER_STOP_S,
    GENERATOR_MAX_RUNTIME_S,
)
from db import get_latest_battery_metrics

# ---- Automatic mode state ----
# All of this lives only in this process's memory, on purpose: every time
# the app (re)starts - a normal restart, a crash, a Task Scheduler
# relaunch - the generator comes back up in MANUAL mode. Automation never
# silently re-arms itself after an unexpected restart; you have to
# deliberately switch it back to Automatic each time, same as you would
# after physically checking on things.
automation_lock = threading.Lock()
generator_mode = "manual"        # "manual" or "automatic"
auto_last_start_time = None      # time.time() of the last auto-triggered start
auto_last_stop_time = None       # time.time() of the last auto-triggered stop
auto_zero_watts_since = None     # time.time() of when Generator/Grid Charge
                                   # first read at/below the threshold during
                                   # the current run; reset to None any time
                                   # it climbs back above the threshold (e.g.
                                   # an AC kicking on mid-balance) or when a
                                   # run starts/stops
auto_last_stop_incomplete = False   # True if the most recent auto-stop
                                      # happened without SOC ever reaching
                                      # GENERATOR_STOP_SOC_FLOOR during that
                                      # run - a sign something cut the
                                      # charge short (out of fuel, tripped
                                      # breaker, etc.) rather than the
                                      # battery actually finishing. Shown on
                                      # the dashboard until the next
                                      # successful auto-start.


def fetch_generator_status():
    """GETs the ESP32's /status. Returns the parsed dict on success, or
    None if the controller couldn't be reached - callers (both the phone's
    status route and the automation loop) decide what None means for them."""
    try:
        r = requests.get(f"http://{GENERATOR_IP}/status", timeout=GENERATOR_REQUEST_TIMEOUT_S)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException:
        return None


def send_generator_command(action):
    """POSTs a start/stop/reset command through to the ESP32. Returns
    (ok, error_message)."""
    try:
        r = requests.get(
            f"http://{GENERATOR_IP}/{action}",
            params={"key": GENERATOR_API_KEY},
            timeout=GENERATOR_REQUEST_TIMEOUT_S,
        )
        if r.status_code == 401:
            return False, "Generator controller rejected the key - check GENERATOR_API_KEY matches the ESP32 sketch"
        return True, None
    except requests.exceptions.RequestException as e:
        return False, f"Could not reach generator controller: {e}"


def build_auto_note(soc, gen_grid_watts, status):
    """A one-line human-readable explanation of what automatic mode is
    currently doing/waiting for - shown on the phone dashboard so 'why did
    it (not) do something' is never a mystery during the week you're
    watching this closely."""
    if soc is None:
        return "Automatic: no battery data yet - not making changes until it's available."
    if status is None:
        return "Automatic: can't reach the generator controller right now."

    engine_awake = status.get("battery", False)
    now = time.time()

    if not engine_awake:
        base = f"Automatic: battery at {soc:.0f}% - will start once it drops to {GENERATOR_START_SOC}% or below."
        if auto_last_stop_incomplete:
            base += f" Heads up: the last run stopped without reaching {GENERATOR_STOP_SOC_FLOOR}% SOC - worth checking the generator (fuel, breaker, etc.) before assuming it finished."
        return base

    started_at = auto_last_start_time or now
    runtime_min = (now - started_at) / 60
    watts_txt = f"{gen_grid_watts:.0f}W" if gen_grid_watts is not None else "unknown"

    if auto_zero_watts_since is not None:
        confirming_min = (now - auto_zero_watts_since) / 60
        return (
            f"Automatic: generator/grid charge near 0W ({watts_txt}) for "
            f"{confirming_min:.0f} min - will stop once that's held for "
            f"{GENERATOR_STOP_CONFIRM_S // 60} min and minimum runtime is met "
            f"({runtime_min:.0f} of {GENERATOR_MIN_RUNTIME_S // 60} min so far)."
        )
    return (
        f"Automatic: charging, running {runtime_min:.0f} min so far, battery "
        f"at {soc:.0f}%, generator/grid charge {watts_txt} - will stop once "
        f"that drops to 0."
    )


def get_full_status():
    """Combines the ESP32's live status with the mode and human-readable
    auto-note, in the shape the /api/generator/status route returns
    directly. Returns (status_dict, http_status_code)."""
    status = fetch_generator_status()
    soc, gen_grid_watts = get_latest_battery_metrics()

    if status is None:
        return {
            "available": False,
            "error": "Could not reach generator controller",
            "mode": generator_mode,
            "auto_note": build_auto_note(soc, gen_grid_watts, None) if generator_mode == "automatic" else None,
        }, 503

    status["mode"] = generator_mode
    status["auto_note"] = build_auto_note(soc, gen_grid_watts, status) if generator_mode == "automatic" else None
    return status, 200


def get_mode():
    return generator_mode


def set_mode(new_mode):
    """Switches between manual and automatic. Returns (ok, error_message).
    Starts clean each time Automatic is turned on - doesn't carry over
    stale state from a previous automatic session."""
    global generator_mode, auto_last_start_time, auto_zero_watts_since, auto_last_stop_incomplete

    if new_mode not in ("manual", "automatic"):
        return False, "mode must be 'manual' or 'automatic'"

    with automation_lock:
        generator_mode = new_mode
        if new_mode == "automatic":
            auto_last_start_time = None
            auto_zero_watts_since = None
            auto_last_stop_incomplete = False
        print(f"[generator] Mode switched to {new_mode}.")

    return True, None


def manual_override():
    """Called at the top of every manual start/stop/reset action. If
    automatic mode is on, a manual button press means the user is taking
    control right now - switch to Manual so automation doesn't fight
    them or resume on its own a minute later."""
    global generator_mode
    with automation_lock:
        if generator_mode == "automatic":
            generator_mode = "manual"
            print("[generator] Manual action taken - switched out of Automatic mode.")


def automation_tick():
    """Runs on a background schedule (see the scheduler set up in
    webapp.py) every AUTOMATION_POLL_INTERVAL_S seconds. No-ops entirely
    unless generator_mode is "automatic". The stop trigger watches the
    Generator/Grid Charge reading (p_rec) rather than SOC: during
    equalization/balancing the charge controller can hold SOC at 99-100%
    for 30-40 minutes while still actively pulling from the generator,
    and cutting power mid-balance causes a blip as the controller
    switches over (WiFi drop, sometimes an AC powering off). Waiting for
    that draw to actually reach zero - and stay there for
    GENERATOR_STOP_CONFIRM_S - avoids that. A confirmed zero-watts
    reading ALWAYS stops the generator, regardless of SOC - if the draw
    genuinely dropped to zero, there's no reason to keep the engine
    running whether that's because charging finished or because
    something else stopped it (ran out of fuel, a tripped breaker, etc.).
    GENERATOR_STOP_SOC_FLOOR only decides whether that stop gets
    logged/flagged as a normal finish or an incomplete one - it never
    blocks the stop itself, so this can't get stuck running for hours
    after the generator has already gone quiet for a reason automation
    can't detect."""
    global auto_last_start_time, auto_last_stop_time, auto_zero_watts_since, auto_last_stop_incomplete

    with automation_lock:
        if generator_mode != "automatic":
            return

        soc, gen_grid_watts = get_latest_battery_metrics()
        if soc is None:
            print("[automation] No battery data available, skipping this check.")
            return

        if soc < GENERATOR_MIN_PLAUSIBLE_SOC:
            print(f"[automation] SOC reading of {soc:.0f}% looks like a glitch (below the {GENERATOR_MIN_PLAUSIBLE_SOC}% plausibility floor) - ignoring it and skipping this check.")
            return

        status = fetch_generator_status()
        if status is None:
            print("[automation] Generator controller unreachable, skipping this check.")
            return

        if status.get("busy"):
            return   # a start/stop sequence is already mid-flight, don't pile on

        engine_awake = status.get("battery", False)
        now = time.time()

        if not engine_awake:
            if soc > GENERATOR_START_SOC:
                return
            if auto_last_stop_time and (now - auto_last_stop_time) < GENERATOR_COOLDOWN_AFTER_STOP_S:
                remaining = GENERATOR_COOLDOWN_AFTER_STOP_S - (now - auto_last_stop_time)
                print(f"[automation] SOC at {soc:.0f}%, but still in cooldown ({remaining/60:.0f} min left) after the last auto-stop.")
                return

            print(f"[automation] SOC at {soc:.0f}% (<= {GENERATOR_START_SOC}%) - auto-starting generator.")
            ok, err = send_generator_command("start")
            if ok:
                auto_last_start_time = now
                auto_zero_watts_since = None
                auto_last_stop_incomplete = False
            else:
                print(f"[automation] Auto-start failed: {err}")
            return

        # Engine is awake - we're either mid-charge or waiting out the
        # minimum runtime / confirmation window before stopping.
        if auto_last_start_time is None:
            # We're in automatic mode and the engine is already running,
            # but we didn't start it ourselves this session (e.g. it was
            # started manually right before switching to Automatic, or
            # this process just restarted). Start the runtime clock now
            # rather than assuming we know how long it's actually been
            # running.
            auto_last_start_time = now

        if gen_grid_watts is not None and gen_grid_watts <= GENERATOR_STOP_WATTS_THRESHOLD:
            if auto_zero_watts_since is None:
                auto_zero_watts_since = now
        else:
            # Draw climbed back above the threshold (e.g. an AC kicked on
            # mid-balance) - the confirmation window resets, so a brief
            # dip to zero can't trigger a stop on its own.
            auto_zero_watts_since = None

        runtime = now - auto_last_start_time
        if runtime < GENERATOR_MIN_RUNTIME_S:
            return

        zero_watts_confirmed = (
            auto_zero_watts_since is not None
            and (now - auto_zero_watts_since) >= GENERATOR_STOP_CONFIRM_S
        )

        stop_is_incomplete = soc < GENERATOR_STOP_SOC_FLOOR

        if zero_watts_confirmed:
            held_min = (now - auto_zero_watts_since) / 60
            if stop_is_incomplete:
                print(f"[automation] Generator/grid charge has read ~0W for {held_min:.0f} min but SOC is only {soc:.0f}% (below the {GENERATOR_STOP_SOC_FLOOR}% floor) - stopping, but this doesn't look like a normal finish. Check the generator (fuel, breaker, etc.).")
            else:
                print(f"[automation] Generator/grid charge has read ~0W for {held_min:.0f} min and SOC is {soc:.0f}% - charge controller appears done, auto-stopping.")
        elif runtime >= GENERATOR_MAX_RUNTIME_S:
            print(f"[automation] Max runtime cap ({GENERATOR_MAX_RUNTIME_S/3600:.0f}h) reached without the charge controller reporting done - auto-stopping as a safety net.")
        else:
            return

        ok, err = send_generator_command("stop")
        if ok:
            auto_last_stop_time = now
            auto_zero_watts_since = None
            auto_last_start_time = None
            auto_last_stop_incomplete = stop_is_incomplete
        else:
            print(f"[automation] Auto-stop failed: {err}")
