#!/usr/bin/env python3
"""
Sunset Cruise Detector — Vashon Island
Checks Open-Meteo weather for calm, clear sunset cruise windows
(low wind + good visibility) and posts alerts to Discord via webhook.
"""

import os
import requests
from datetime import datetime, timedelta, timezone
import pytz

# ── Config ────────────────────────────────────────────────────────────────────

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK")
if not DISCORD_WEBHOOK:
    raise RuntimeError("DISCORD_WEBHOOK environment variable is not set")

# Vashon Island
LAT = 47.44
LON = -122.47
TZ = pytz.timezone("America/Los_Angeles")

# Thresholds — what makes a good sunset cruise:
MAX_WIND_MPH = 10          # low wind  (~8.7 knots)
MIN_VISIBILITY_MILES = 6   # good, haze-free visibility
MAX_PRECIP_CHANCE = 20     # percent — rain rules it out
MAX_CLOUD_COVER = 85       # percent — clear or partly cloudy is fine; reject only
                           # near-total overcast (no sunset to see). Raise to ~100
                           # to stop gating on clouds entirely.

# How many days ahead to check
DAYS_AHEAD = 7

METERS_PER_MILE = 1609.34

# ── Open-Meteo Weather + Sunset ───────────────────────────────────────────────

def get_weather_and_sunset(date: datetime.date) -> dict:
    """Return sunset time plus hourly weather for the given date."""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": LAT,
        "longitude": LON,
        "daily": "sunset,sunrise",
        "hourly": (
            "cloud_cover,visibility,wind_speed_10m,"
            "precipitation_probability,weather_code"
        ),
        "wind_speed_unit": "mph",
        "timezone": "America/Los_Angeles",
        "start_date": date.isoformat(),
        "end_date": date.isoformat(),
    }
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def parse_sunset(data: dict) -> datetime:
    """Extract sunset as a timezone-aware datetime."""
    sunset_str = data["daily"]["sunset"][0]  # e.g. "2025-05-10T20:47"
    dt = datetime.fromisoformat(sunset_str)
    return TZ.localize(dt)


def weather_at_window(data: dict, sunset: datetime, window_minutes: int = 90) -> dict:
    """Return conditions in the window around sunset."""
    times = data["hourly"]["time"]
    clouds = data["hourly"]["cloud_cover"]
    visibility = data["hourly"]["visibility"]  # meters
    winds = data["hourly"]["wind_speed_10m"]
    precip = data["hourly"]["precipitation_probability"]

    window_start = sunset - timedelta(minutes=window_minutes // 2)
    window_end = sunset + timedelta(minutes=window_minutes // 2)

    rel_cloud, rel_vis, rel_wind, rel_precip = [], [], [], []
    for i, t_str in enumerate(times):
        t = TZ.localize(datetime.fromisoformat(t_str))
        if window_start <= t <= window_end:
            rel_cloud.append(clouds[i])
            rel_vis.append(visibility[i])
            rel_wind.append(winds[i])
            rel_precip.append(precip[i])

    if not rel_cloud:
        return {}

    avg = lambda xs: sum(xs) / len(xs)

    return {
        "avg_cloud": round(avg(rel_cloud)),
        "min_visibility_mi": round(min(rel_vis) / METERS_PER_MILE, 1),
        "avg_wind": round(avg(rel_wind), 1),
        "max_precip": max(rel_precip),
    }


# ── Condition Checker ─────────────────────────────────────────────────────────

def check_day(date: datetime.date) -> dict | None:
    """Returns a result dict if it's a good cruise window, else None."""
    # Weather + sunset
    wx_data = get_weather_and_sunset(date)
    sunset = parse_sunset(wx_data)

    # Skip if sunset has already passed
    if sunset < datetime.now(TZ):
        return None

    # Weather check
    wx = weather_at_window(wx_data, sunset)
    if not wx:
        return None

    if wx["avg_wind"] > MAX_WIND_MPH:
        return None
    if wx["min_visibility_mi"] < MIN_VISIBILITY_MILES:
        return None
    if wx["max_precip"] > MAX_PRECIP_CHANCE:
        return None
    if wx["avg_cloud"] > MAX_CLOUD_COVER:
        return None

    return {
        "date": date,
        "sunset": sunset,
        "weather": wx,
    }


# ── Discord Alert ─────────────────────────────────────────────────────────────

def post_to_discord(windows: list[dict]):
    if not windows:
        return

    embeds = []
    for w in windows:
        date_str = w["date"].strftime("%A, %B %-d")
        sunset_str = w["sunset"].strftime("%-I:%M %p")

        wx = w["weather"]

        fields = [
            {"name": "🌇 Sunset", "value": sunset_str, "inline": True},
            {"name": "💨 Wind", "value": f"{wx['avg_wind']} mph avg", "inline": True},
            {"name": "👁️ Visibility", "value": f"{wx['min_visibility_mi']} mi", "inline": True},
            {"name": "☁️ Cloud Cover", "value": f"{wx['avg_cloud']}%", "inline": True},
            {"name": "🌧️ Precip Chance", "value": f"{wx['max_precip']}%", "inline": True},
        ]

        embeds.append({
            "title": f"🌅 Golden Cruise Window — {date_str}",
            "color": 0xFF8C00,  # deep orange
            "fields": fields,
            "footer": {"text": "Vashon Sunset Cruise Bot · Open-Meteo"},
        })

    payload = {
        "content": "**El Mistico conditions check** — upcoming golden windows:",
        "embeds": embeds,
    }

    r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
    r.raise_for_status()
    print(f"✅ Posted {len(windows)} window(s) to Discord.")


# ── Discord No-Windows Summary ────────────────────────────────────────────────

def post_no_windows_summary(days_checked: int):
    """Post a weekly 'bot is alive but nothing looks good' message."""
    payload = {
        "embeds": [{
            "title": "🌧️ No Golden Windows This Week",
            "description": (
                f"Checked the next **{days_checked} days** — "
                "conditions aren't lining up for a sunset cruise. "
                "Wind, haze, or rain are working against us. Stay tuned."
            ),
            "color": 0x778899,  # slate grey
            "footer": {"text": "Vashon Sunset Cruise Bot · Open-Meteo"},
        }]
    }
    r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
    r.raise_for_status()
    print("📭 Posted no-windows summary to Discord.")


# ── Heartbeat ────────────────────────────────────────────────────────────────

def post_heartbeat(days_checked: int, windows_found: int):
    """Always post a short status so we know the bot ran."""
    now = datetime.now(TZ).strftime("%-I:%M %p %Z")
    if windows_found:
        msg = f"✅ Bot ran at {now} — checked {days_checked} days, posted {windows_found} golden window(s)."
    else:
        msg = f"✅ Bot ran at {now} — checked {days_checked} days, no golden windows right now."

    payload = {"content": msg}
    r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
    r.raise_for_status()
    print(f"💓 Heartbeat posted.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    today = datetime.now(TZ).date()
    is_sunday = today.weekday() == 6
    golden_windows = []

    for i in range(DAYS_AHEAD):
        date = today + timedelta(days=i)
        print(f"Checking {date}...")
        try:
            result = check_day(date)
            if result:
                golden_windows.append(result)
                print(f"  ✨ Golden window found!")
            else:
                print(f"  ✗ Conditions not met.")
        except Exception as e:
            print(f"  ⚠️ Error: {e}")

    if golden_windows:
        post_to_discord(golden_windows)
    elif is_sunday:
        post_no_windows_summary(DAYS_AHEAD)

    # Always post a heartbeat so we know the bot is alive
    post_heartbeat(DAYS_AHEAD, len(golden_windows))


if __name__ == "__main__":
    main()
