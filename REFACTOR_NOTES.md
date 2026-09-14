# What changed

`webapp.py` went from one ~750-line file (plus a large inline HTML/JS
string) to nine focused files. Behavior is unchanged — every route,
every automation threshold, every comment explaining a "why" decision
carried over exactly. This was verified by importing the refactored
app with real dependencies installed and confirming all 18 original
routes are registered identically, plus rendering the dashboard
through Flask's test client to confirm the extracted template works.

| File | What's in it |
|---|---|
| `config.py` | Env vars, constants, `AC_UNITS`/`PLUG_UNITS` |
| `db.py` | All direct SQLite access |
| `generator_control.py` | ESP32 communication + the automatic start/stop decision logic |
| `ac_control.py` | LG ThinQ API calls (unchanged from what you had) |
| `routes_ac.py` | AC status/control endpoints |
| `routes_generator.py` | Generator status/mode/start/stop/reset endpoints |
| `routes_power.py` | Power plug + total + daily energy endpoints |
| `routes_sensors.py` | Outdoor temp sensor, solar/inverter, weather comparison |
| `routes_dashboard.py` | The `/` route |
| `templates/dashboard.html` | The dashboard's HTML/CSS/JS, pulled out of the Python string |
| `webapp.py` | Entry point — creates the Flask app, registers blueprints, starts the automation scheduler |

## To integrate this into your real project

1. **Back up first.** Copy your current `C:\AI\Projects\mountain_house_control` folder somewhere safe before overwriting anything.
2. Drop these files into that folder, **replacing** `webapp.py` and adding the new ones. Keep your existing `.env`, `requirements.txt`, and `restart_webapp.bat` as they are — nothing here changes what they need to contain.
3. Add a `templates` folder (if it doesn't exist) with `dashboard.html` inside it — Flask's `render_template()` expects that exact folder name sitting next to `webapp.py`.
4. Run it: `python webapp.py`, then check the dashboard loads and the generator mode toggle, AC controls, and readings all still work as expected.

## One open decision

`config.py` still has your three real LG ThinQ `device_id` values hardcoded in `AC_UNITS`, same as before. They're not usable without your `.env` credentials, but they are specific to your real ACs. Leave as-is, or move them to a gitignored file if you'd rather the public repo have zero house-specific identifiers — your call, not required.
