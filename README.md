# Mountain House Control

A home-automation project for an off-grid mountain-top house — data collection,
a phone-viewable dashboard, and (eventually) remote control, built piece by
piece.

> Rename this file's title / the repo itself to whatever you land on —
> `mountain-house-control` and `offgrid-house-dashboard` were the two
> frontrunners.

## What's here

- **`main.py`** — background collector. Polls weather, AC status, plug
  power, and inverter data on independent schedules (via APScheduler) and
  logs everything to SQLite. Runs continuously via Windows Task Scheduler
  (starts at boot, restarts on failure).
- **`webapp.py`** — Flask app that receives readings from an ESP32 +
  DHT22 temperature/humidity sensor placed outside, stores them, and
  serves a simple mobile-friendly dashboard page.
- **`query_tools.py`** (in `use_control/`) — checks data freshness across
  all logged tables and prints recent rows for a quick sanity check.

## Status / roadmap

- [x] Background collector running unattended (weather, AC, plugs, inverter)
- [x] ESP32 + DHT22 outdoor temperature sensor wired up and posting readings
- [x] Local dashboard viewable on phone over WiFi
- [ ] Make the dashboard installable as a PWA (add-to-home-screen, app-like feel)
- [ ] Remote access from anywhere via Tailscale (private VPN, no port forwarding)
- [ ] Additional sensors added to the dashboard, one at a time
- [ ] Remote **control** (e.g. relay/AC toggling) — deliberately last, once
      the read-only foundation is solid and access is locked down

## Setup

1. Clone the repo and create a virtual environment:
   ```
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```
   (if there's no `requirements.txt` yet: `pip install apscheduler flask` at minimum)

2. Set the API key used to authenticate the ESP32's posts to `webapp.py`.
   This is loaded from an environment variable, never committed to the repo:
   ```powershell
   $env:MOUNTAIN_HOUSE_API_KEY = "something-only-you-know"
   ```
   For a permanent setup, add this as a proper Windows environment variable
   instead so Task Scheduler picks it up automatically.

3. Run the collector and the dashboard (as separate processes for now):
   ```
   python main.py
   python webapp.py
   ```

4. On your phone, connected to the same WiFi as the machine running this,
   visit `http://<that machine's local IP>:5000`.

## Notes

- Database files (`*.db`) and anything holding secrets (`.env`, `config.py`)
  are excluded via `.gitignore` — never commit real credentials or logged
  house data.
- The ESP32 sketch (Arduino, not in this repo yet) reads the DHT22 sensor
  and POSTs JSON readings to `webapp.py`'s `/api/sensor/mountain-temp`
  endpoint with an `X-API-Key` header matching `MOUNTAIN_HOUSE_API_KEY`.