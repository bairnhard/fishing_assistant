from homeassistant.core import HomeAssistant
import datetime
from typing import Dict
import aiohttp
import pandas as pd
import logging


from .fish_profiles import FISH_PROFILES
from .helpers.astro import calculate_astronomy_forecast

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
_LOGGER = logging.getLogger(__name__)


def scale_score(score):
    # `score` is a weight-normalised value in [0, 1] (weights sum to 1.0).
    # A typical decent day sits around 0.70-0.80, so map the realistic
    # 0.50-0.85 band onto 0-10 instead of the old 0.50-0.90 band, which
    # combined with the best-3h-window daily score pinned almost every day at 10.
    stretched = (score - 0.5) / (0.85 - 0.5) * 10
    return max(0, min(10, round(stretched)))


def get_profile_weights(body_type: str) -> dict:
    if body_type not in ["lake", "river", "pond", "reservoir"]:
        _LOGGER.warning(f"Unknown body_type '{body_type}', defaulting to 'lake'.")
        body_type = "lake"

    weights = {
        "temp": 0.25,
        "cloud": 0.1,
        "pressure": 0.15,
        "wind": 0.1,
        "precip": 0.1,
        "twilight": 0.15,
        "solunar": 0.1,
        "moon": 0.05,
    }

    if body_type == "river":
        weights.update({
            "pressure": 0.05,
            "solunar": 0.05,
            "precip": 0.2,
        })
    elif body_type == "pond":
        weights.update({
            "temp": 0.3,
            "precip": 0.2,
            "pressure": 0.2,
        })
    elif body_type == "reservoir":
        weights.update({
            "pressure": 0.1,
            "solunar": 0.08,
            "moon": 0.07,
        })

    # Normalise so the weights always sum to 1.0. Without this the per-body
    # overrides above add to the base weights (e.g. "pond" summed to 1.2),
    # inflating the raw score and pushing it past the top of the scale.
    total = sum(weights.values())
    weights = {k: v / total for k, v in weights.items()}

    return weights


async def get_fish_score_forecast(
    hass: HomeAssistant,
    fish: str,
    lat: float,
    lon: float,
    timezone: str,
    elevation: float,
    body_type: str,
) -> Dict[str, Dict[str, str | float]]:
    fish_profile = FISH_PROFILES.get(fish)
    if not fish_profile:
        _LOGGER.warning(f"No fish profile found for '{fish}'")
        return {}

    today = datetime.date.today()
    end_date = today + datetime.timedelta(days=6)

    # Get moon + sun event timings from Skyfield
    astro_data = await calculate_astronomy_forecast(hass, lat, lon, timezone, days=7)

    if not astro_data:
        return {}

    try:
        # Log the exact values being sent to the API
        _LOGGER.debug(f"Making Open-Meteo API request with lat={lat} ({type(lat)}), lon={lon} ({type(lon)})")
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m,cloudcover,pressure_msl,precipitation,windspeed_10m",
            "daily": "sunrise,sunset",
            "timezone": timezone,
            "elevation": elevation,
            "start_date": str(today),
            "end_date": str(end_date)
        }
        _LOGGER.debug(f"Full API parameters: {params}")

        async with aiohttp.ClientSession() as session:
            async with session.get(
                OPEN_METEO_URL,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "hourly": "temperature_2m,cloudcover,pressure_msl,precipitation,windspeed_10m",
                    "daily": "sunrise,sunset",
                    "timezone": timezone,
                    "elevation": elevation,
                    "start_date": str(today),
                    "end_date": str(end_date)
                },
                timeout=aiohttp.ClientTimeout(total=15)
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    _LOGGER.error(f"Open-Meteo API error: Status {response.status}, Response: {error_text}")
                    return {}

                data = await response.json()
                _LOGGER.debug(f"Open-Meteo response: {data}")
                if "hourly" not in data or "daily" not in data:
                    _LOGGER.warning(f"Fishing forecast fetch failed for {fish} at {lat}, {lon}: {data}")
                    return {}
    except Exception as e:
        _LOGGER.error(f"Exception while fetching Open-Meteo data: {e}")
        return {}

    # Units: temp °C, cloud %, pressure hPa, wind km/h, precip mm
    hourly = pd.DataFrame({
        "datetime": pd.to_datetime(data["hourly"]["time"]),
        "temp": data["hourly"]["temperature_2m"],
        "cloud": data["hourly"]["cloudcover"],
        "pressure": data["hourly"]["pressure_msl"],
        "precip": data["hourly"]["precipitation"],
        "wind": data["hourly"]["windspeed_10m"]
    })

    hourly["date"] = hourly["datetime"].dt.date
    hourly["hour"] = hourly["datetime"].dt.hour
    hourly["pressure_trend"] = hourly["pressure"].diff()

    forecast = {}
    weights = get_profile_weights(body_type)


    for date, group in hourly.groupby("date"):
        date_str = str(date)
        scores = []
        astro = astro_data.get(date_str, {})

        for _, row in group.iterrows():
            score = _score_hour(row=row, profile=fish_profile, astro=astro, weights=weights)
            scores.append((row["hour"], score))

        # Best 3-hour rolling window
        best_avg = 0
        best_window = ("--:--", "--:--")
        for i in range(len(scores) - 2):
            avg = (scores[i][1] + scores[i+1][1] + scores[i+2][1]) / 3
            if avg > best_avg:
                best_avg = avg
                best_window = (f"{scores[i][0]:02}:00", f"{scores[i+2][0]:02}:00")

        # Daily score is the average of all hourly scores, not the best 3-hour
        # window. The best window is still reported separately as "best_window".
        # Using the best window as the daily score pinned nearly every day at
        # the maximum, because dawn/dusk hours almost always score ~1.0.
        day_mean = sum(s for _, s in scores) / len(scores) if scores else 0

        forecast[date_str] = {
            "score": scale_score(day_mean),
            "best_window": f"{best_window[0]} – {best_window[1]}"
        }
        
    _LOGGER.debug(f"Forecast for {fish} on {lat},{lon}: {forecast}")

    return forecast


def _score_hour(row, profile, astro, weights: dict) -> float:
    hour = row["hour"]

    temp_score = _score_temp(row["temp"], profile["temp_range"])
    cloud_score = 1 - abs(row["cloud"] - profile["ideal_cloud"]) / 100
    press_score = _score_pressure_trend(row["pressure_trend"])
    wind_score = _score_wind(row["wind"])
    precip_score = _score_precip(row["precip"])

    # Astro events
    sunrise = _parse_time(astro.get("sunrise"))
    sunset = _parse_time(astro.get("sunset"))
    moon_phase = astro.get("moon_phase", 0.5)
    transit = _parse_time(astro.get("moon_transit", None))
    underfoot = _parse_time(astro.get("moon_underfoot"))
    moonrise = _parse_time(astro.get("moonrise"))
    moonset = _parse_time(astro.get("moonset"))

    twilight_score = _score_twilight(hour, sunrise, sunset)
    moon_score = _score_moon_phase(moon_phase)
    solunar_score = _score_solunar(hour, transit, underfoot, moonrise, moonset)

    return round((
        temp_score * weights["temp"] +
        cloud_score * weights["cloud"] +
        press_score * weights["pressure"] +
        wind_score * weights["wind"] +
        precip_score * weights["precip"] +
        twilight_score * weights["twilight"] +
        solunar_score * weights["solunar"] +
        moon_score * weights["moon"]
    ), 2)


# ----------------------------
# Individual scoring functions
# ----------------------------

def _score_temp(temp: float, ideal_range: tuple[float, float]) -> float:
    # Note: temp is air temp in °C, used as proxy for water temp.
    low, high = ideal_range
    if temp < low:
        return max(0, (temp - (low - 10)) / 10)
    elif temp > high:
        return max(0, (high + 10 - temp) / 10)
    return 1.0

def _score_pressure_trend(trend: float) -> float:
    if pd.isna(trend):
        return 0.7
    if trend < -2:
        return 1.0
    elif trend > 2:
        return 0.4
    return 0.7

def _score_wind(speed: float) -> float:
    # Assumes km/h
    if speed < 2:
        return 0.8
    elif speed < 6:
        return 1.0
    elif speed < 10:
        return 0.6
    return 0.2

def _score_precip(amount: float) -> float:
    # Assumes mm/h
    if amount == 0:
        return 0.7
    elif amount < 1:
        return 1.0
    elif amount < 5:
        return 0.5
    return 0.2

def _score_twilight(hour: int, sunrise, sunset) -> float:
    if not sunrise or not sunset:
        return 0.7
    if abs(hour - sunrise.hour) <= 1 or abs(hour - sunset.hour) <= 1:
        return 1.0
    return 0.7

def _score_moon_phase(phase: float) -> float:
    # 0.0 = New, 0.5 = Full, 1.0 = New
    if phase is None:
        return 0.7  # Default score when moon phase data is missing
    if phase < 0.1 or phase > 0.9:
        return 1.0
    return 0.7

def _score_solunar(hour: int, transit, underfoot, moonrise, moonset) -> float:
    boost = 0
    for event in [transit, underfoot]:
        if event and abs(hour - event.hour) <= 1:
            boost += 0.5
    for event in [moonrise, moonset]:
        if event and abs(hour - event.hour) <= 1:
            boost += 0.25
    return min(1.0, 0.6 + boost)


def _parse_time(time_str: str):
    if not time_str:
        return None
    try:
        return datetime.datetime.strptime(time_str, "%H:%M").time()
    except Exception:
        return None
