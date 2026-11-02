from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Dict
from skyfield.api import load, wgs84
from skyfield import almanac
import os
from homeassistant.core import HomeAssistant
import logging


async def calculate_astronomy_forecast(hass: HomeAssistant, lat: float, lon: float, tz_name: str = "UTC", days: int = 7) -> Dict[str, dict]:
    ts = load.timescale()

    # Check if ephemeris file exists, if not create the directory
    data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
    os.makedirs(data_dir, exist_ok=True)

    eph_path = os.path.join(data_dir, "de421.bsp")

    # Download if not exists
    if not os.path.exists(eph_path):
        _LOGGER = logging.getLogger(__name__)
        _LOGGER.info("Downloading skyfield ephemeris data...")
        # Use executor to download without blocking
        def download_eph():
            import urllib.request
            url = "https://naif.jpl.nasa.gov/pub/naif/generic_kernels/spk/planets/de421.bsp"
            urllib.request.urlretrieve(url, eph_path)
            return load(eph_path)

        eph = await hass.async_add_executor_job(download_eph)
    else:
        # Load existing file
        eph = await hass.async_add_executor_job(lambda: load(eph_path))
    location = wgs84.latlon(lat, lon)

    # All event times below are formatted in this local timezone so that they
    # line up with the local-time hourly weather data used for scoring. The
    # skyfield events themselves are computed in UTC; we convert on output.
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc

    def local(t):
        """skyfield Time -> local tz-aware datetime."""
        return t.utc_datetime().astimezone(tz)

    start_date = datetime.now(tz).date()
    end_date = start_date + timedelta(days=days)

    # Search a slightly wider UTC window so events that fall on the first/last
    # local day near midnight (up to a timezone offset away from UTC) are caught.
    search_start = start_date - timedelta(days=1)
    search_end = end_date + timedelta(days=1)
    t0 = ts.utc(search_start.year, search_start.month, search_start.day)
    t1 = ts.utc(search_end.year, search_end.month, search_end.day)

    # Astronomy events
    moon_phases = almanac.moon_phases(eph)
    moon_rise_set = almanac.risings_and_settings(eph, eph['Moon'], location)
    moon_transits = almanac.meridian_transits(eph, eph['Moon'], location)
    sun_rise_set = almanac.sunrise_sunset(eph, location)

    # Init empty containers
    events = {
        "moon_phase": {},
        "moonrise": {},
        "moonset": {},
        "moon_transit": {},
        "moon_underfoot": {},
        "sunrise": {},
        "sunset": {}
    }

    # Moon phase per day
    times, phases = almanac.find_discrete(t0, t1, moon_phases)
    for t, p in zip(times, phases):
        date_str = str(local(t).date())
        events["moon_phase"][date_str] = float(round(p % 1, 3))

    # Moonrise / moonset
    times, events_raw = almanac.find_discrete(t0, t1, moon_rise_set)
    for t, ev in zip(times, events_raw):
        lt = local(t)
        date_str = str(lt.date())
        key = "moonrise" if ev == 1 else "moonset"
        events[key][date_str] = lt.strftime("%H:%M")

    # Transit / underfoot
    times, events_raw = almanac.find_discrete(t0, t1, moon_transits)
    for t, ev in zip(times, events_raw):
        lt = local(t)
        date_str = str(lt.date())
        key = "moon_transit" if ev == 1 else "moon_underfoot"
        if key not in events:
            events[key] = {}
        events[key][date_str] = lt.strftime("%H:%M")

    # Sunrise / sunset
    times, events_raw = almanac.find_discrete(t0, t1, sun_rise_set)
    for t, ev in zip(times, events_raw):
        lt = local(t)
        date_str = str(lt.date())
        key = "sunrise" if ev == 1 else "sunset"
        events[key][date_str] = lt.strftime("%H:%M")

    # Final forecast
    forecast = {}
    for i in range(days):
        d = start_date + timedelta(days=i)
        ds = str(d)
        forecast[ds] = {
            "moon_phase": events["moon_phase"].get(ds),
            "moonrise": events["moonrise"].get(ds),
            "moonset": events["moonset"].get(ds),
            "moon_transit": events["moon_transit"].get(ds),
            "moon_underfoot": events["moon_underfoot"].get(ds),
            "sunrise": events["sunrise"].get(ds),
            "sunset": events["sunset"].get(ds),
        }

    return forecast
