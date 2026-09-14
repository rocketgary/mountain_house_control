# mountain_house_control

Flask dashboard and automation layer for an off-grid solar/battery/generator system. Reads live data from a companion polling service ([`off_grid_house_control`](../off_grid_house_control)) and turns it into one dashboard with full manual and automatic control — replacing what used to be several separate manufacturer apps and a lot of manual monitoring.

## What it does

- **Dashboard** — a single view of solar, battery SOC, generator status, AC status and power draw, and total house power consumption
- **Automatic generator control** — starts the generator when SOC drops to ≤ 50%, using a precisely-timed sequence sent to an ESP32-based relay controller; stops it automatically once generator output holds at 0 watts for 3 minutes, with safeguards for sensor glitches, equalization/balancing charge holds, minimum runtime, cooldown, and a maximum-runtime safety net
- **Manual overrides** — direct control of the generator, plus the three house air conditioners via the LG ThinQ API
- **Remote access** — start/stop from anywhere, replacing a 100 ft-range physical remote and no longer requiring a neighbor to drive over and do it by hand

## Why an ESP32 relay controller

The generator has no remote API of its own, so control happens by physically simulating button presses through relays wired to an ESP32. The start sequence wakes the generator, waits, then presses start twice with specific timing tuned to how the machine actually behaves; the stop sequence reverses the process. Full timings and reasoning are in the [case study](../off-grid-automation-case-study.md).

## Project structure

The app is split by responsibility rather than living in one file:

| File | Responsibility |
|---|---|
| `webapp.py` | Entry point — creates the Flask app, registers routes, starts the automation scheduler |
| `config.py` | Environment variables, thresholds, AC/plug unit definitions |
| `db.py` | All SQLite access (reads from the shared `home_data` database) |
| `generator_control.py` | ESP32 communication and the automatic start/stop decision logic |
| `ac_control.py` | LG ThinQ API calls |
| `routes_ac.py`, `routes_generator.py`, `routes_power.py`, `routes_sensors.py`, `routes_dashboard.py` | Flask route groups by feature area |
| `templates/dashboard.html` | The dashboard page itself |

## Stack

- Python, Flask, APScheduler
- LG ThinQ API — AC status and control
- tinytuya — real power draw from AC and freezer smart plugs
- Reads from a shared SQLite database (`home_data`) populated by `off_grid_house_control`

## Setup

1. Copy `.env.example` to `.env` (if present) or create `.env` with: `MOUNTAIN_HOUSE_API_KEY`, `GENERATOR_API_KEY`, `GENERATOR_IP`, `PAT_TOKEN`, `X_COUNTRY`, `X_CLIENT_ID`, `X_API_KEY`. **Never commit `.env`.**
2. `pip install -r requirements.txt`
3. `python webapp.py`
4. From a phone/browser on the same WiFi (or over Tailscale), visit `http://<this machine's IP>:5000`

## Status

Running a real off-grid property day to day — generator automation, AC control, and power monitoring all live. Next up: automatic generator runtime tracking, a refuel reminder, and two agents — one that charts historical data, one that produces a 24-hour system summary.
