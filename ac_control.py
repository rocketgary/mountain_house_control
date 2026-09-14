#Talks to the LG ThinQ Connect API (api-aic.lgthinq.com) to control the AC
#units - ported from the existing tkinter control program. The GUI-specific
#"import gui" is dropped since this runs inside webapp.py instead, and every
#function here still just takes a device_id plus whatever value it's
#setting, same as before.

import os
import uuid
import base64
import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://api-aic.lgthinq.com"

# Credentials - from .env (same PAT_TOKEN/X_COUNTRY/X_CLIENT_ID/X_API_KEY
# values the tkinter program uses). Never logged or returned to the client -
# webapp.py's routes only pass back generic ok/error results.
PAT_TOKEN = os.getenv("PAT_TOKEN")
X_COUNTRY = os.getenv("X_COUNTRY")
X_CLIENT_ID = os.getenv("X_CLIENT_ID")
X_API_KEY = os.getenv("X_API_KEY")


def generate_message_id():
    """Generate url-safe base64 no-padding UUID4 (22 chars) as required by LG API"""
    uid = uuid.uuid4()
    return base64.urlsafe_b64encode(uid.bytes).rstrip(b'=').decode('utf-8')


def control_device(device_id, payload):
    url = f"{BASE_URL}/devices/{device_id}/control"

    headers = {
        "Authorization": f"Bearer {PAT_TOKEN}",
        "x-message-id": generate_message_id(),
        "x-country": X_COUNTRY,
        "x-client-id": X_CLIENT_ID,
        "x-api-key": X_API_KEY,
        "x-conditional-control": "false",  # Only control if device is in controllable state
        "Content-Type": "application/json",
    }

    response = requests.post(url, headers=headers, json=payload, timeout=10)
    response.raise_for_status()
    return response.json()


def power_on(device_id):
    payload = {"operation": {"airConOperationMode": "POWER_ON"}}
    return control_device(device_id, payload)


def power_off(device_id):
    payload = {"operation": {"airConOperationMode": "POWER_OFF"}}
    return control_device(device_id, payload)


def set_mode(device_id, mode):
    """mode: COOL, FAN, AIR_DRY, ENERGY_SAVING"""
    valid_modes = ["COOL", "FAN", "AIR_DRY", "ENERGY_SAVING"]
    if mode not in valid_modes:
        raise ValueError(f"Invalid mode. Choose from: {valid_modes}")
    payload = {"airConJobMode": {"currentJobMode": mode}}
    return control_device(device_id, payload)


def set_temperature_f(device_id, temp_f):
    """Convert F to C since the API only accepts Celsius"""
    temp_c = round((temp_f - 32) * 5 / 9 * 2) / 2  # Round to nearest 0.5
    if not 16 <= temp_c <= 30:
        raise ValueError(f"Temperature {temp_f}°F = {temp_c}°C is out of range (16-30°C)")

    payload = {"temperature": {"unit": "C", "targetTemperature": temp_c}}
    return control_device(device_id, payload)


def set_temperature_c(device_id, temp_c):
    """Temperature in Celsius: 16 - 30, step 0.5"""
    if not 16 <= temp_c <= 30:
        raise ValueError("Temperature must be between 16 and 30°C")
    payload = {"temperature": {"targetTemperature": temp_c, "unit": "C"}}
    return control_device(device_id, payload)


def set_fan_speed(device_id, speed):
    """speed: LOW, MID, HIGH"""
    valid_speeds = ["LOW", "MID", "HIGH"]
    if speed not in valid_speeds:
        raise ValueError(f"Invalid speed. Choose from: {valid_speeds}")
    payload = {"airFlow": {"windStrength": speed}}
    return control_device(device_id, payload)
