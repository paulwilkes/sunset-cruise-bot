#!/usr/bin/env python3
"""
Sunset Cruise Detector — Vashon Island
Checks NOAA tides + Open-Meteo weather for golden sunset cruise windows
and posts alerts to Discord via webhook.
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

# NOAA station — Point Defiance/Tacoma (close to south end)
# Alternative: Seattle = 9447130
NOAA_STATION = "9446484"

# Thresholds
MAX_WIND_MPH = 10          # ~8.7 knots
MAX_CLOUD_COVER = 35       # percent
TIDE_WINDOW_MINUTES = 120  # high tide must be within this many mins of sunset

# How many days ahead to check
DAYS_AHEAD = 3

# ── NOAA Tides ────────────────────────────────────────────────────────────────

def get_tide_highs(date: datetime.date) -> list[dict]:
    """Return list of high tide events for a given date."""
    date_str = date.strftime("%Y%m%d")
    url = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
    params = {
        "begin_date": date_str,
        "end_date": date_str,
        "station": NOAA_STATION,
        "product": "predictions",
        "datum": "MLLW",
        "time_zone": "lst_ldt",
        "interval": "hilo",
        "units": "english",
        "application": "vashon_sunset_bot",
        "format": "json",
    }
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    predictions = data.get("predictions", [])
    highs = [p for p in predictions if p.get("type") == "H"]
    return highs


# ── Open-Meteo Weather + Sunset ───────────────────────────────────────────────

def get_weather_and_sunset(date: datetime.date) -> dict:
    """Return sunset time plus hourly weather for the given date."""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": LAT,
        "longitude": LON,
        "daily": "sunset,sunrise",
        "hourly": "cloud_cover,wind_speed_10m,precipitation_probability,weather_code",
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
    """Return averaged conditions in the window around sunset."""
    times = data["hourly"]["time"]
    clouds = data["hourly"]["cloud_cover"]
    winds = data["hourly"]["wind_speed_10m"]
    precip = data["hourly"]["precipitation_probability"]

    window_start = sunset - timedelta(minutes=window_minutes // 2)
    window_end = sunset + timedelta(minutes=window_minutes // 2)

    relevant_clouds, relevant_winds, relevant_precip = [], [], []
    for i, t_str in enumerate(times):
        t = TZ.localize(datetime.fromisoformat(t_str))
        if window_start <= t <= window_end:
            relevant_clouds.append(clouds[i])
            relevant_winds.append(winds[i])
            relevant_precip.append(precip[i])

    if not relevant_clouds:
        return {}

    return {
        "avg_cloud": round(sum(relevant_clouds) / len(relevant_clouds)),
        "avg_wind": round(sum(relevant_winds) / len(relevant_winds), 1),
        "max_precip": max(relevant_precip),
    }


# ── Condition Checker ─────────────────────────────────────────────────────────

def check_day(date: datetime.date) -> dict | None:
    """
    Returns a result dict if conditions are golden, else None.
    """
    # Tides
    highs = get_tide_highs(date)
    if not highs:
        return None

    # Weather + sunset
    wx_data = get_weather_and_sunset(date)
    sunset = parse_sunset(wx_data)

    # Find high tides near sunset
    matching_highs = []
    for h in highs:
        tide_time = TZ.localize(datetime.strptime(h["t"], "%Y-%m-%d %H:%M"))
        delta = abs((tide_time - sunset).total_seconds() / 60)
        if delta <= TIDE_WINDOW_MINUTES:
            matching_highs.append({
                "time": tide_time,
                "height_ft": float(h["v"]),
                "minutes_from_sunset": round((tide_time - sunset).total_seconds() / 60),
            })

    if not matching_highs:
        return None

    # Best high tide (closest to sunset)
    best_tide = min(matching_highs, key=lambda x: abs(x["minutes_from_sunset"]))

    # Weather check
    wx = weather_at_window(wx_data, sunset)
    if not wx:
        return None

    if wx["avg_cloud"] > MAX_CLOUD_COVER:
        return None
    if wx["avg_wind"] > MAX_WIND_MPH:
        return None
    if wx["max_precip"] > 20:
        return None

    return {
        "date": date,
        "sunset": sunset,
        "tide": best_tide,
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

        tide = w["tide"]
        tide_str = tide["time"].strftime("%-I:%M %p")
        tide_delta = tide["minutes_from_sunset"]
        tide_rel = (
            f"{abs(tide_delta)} min before sunset"
            if tide_delta < 0
            else f"right at sunset"
            if tide_delta == 0
            else f"{tide_delta} min after sunset"
        )

        wx = w["weather"]

        embeds.append({
            "title": f"🌅 Golden Cruise Window — {date_str}",
            "color": 0xFF8C00,  # deep orange
            "fields": [
                {"name": "🌇 Sunset", "value": sunset_str, "inline": True},
                {"name": "🌊 High Tide", "value": f"{tide_str} ({tide_rel})\n{tide['height_ft']:.1f} ft", "inline": True},
                {"name": "💨 Wind", "value": f"{wx['avg_wind']} mph avg", "inline": True},
                {"name": "☁️ Cloud Cover", "value": f"{wx['avg_cloud']}%", "inline": True},
                {"name": "🌧️ Precip Chance", "value": f"{wx['max_precip']}%", "inline": True},
            ],
            "footer": {"text": "Vashon Sunset Cruise Bot · NOAA + Open-Meteo"},
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
                "Wind, clouds, or tides are working against us. Stay tuned."
            ),
            "color": 0x778899,  # slate grey
            "footer": {"text": "Vashon Sunset Cruise Bot · NOAA + Open-Meteo"},
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
