"""Platform for fishing assistant sensor integration."""
from __future__ import annotations

import datetime
from datetime import timedelta
from zoneinfo import ZoneInfo

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .score import get_fish_score_forecast

# Fetch a fresh forecast at most once per hour, only during these hours,
# to match the "auto-refreshes 4x per day" behaviour described in the README.
UPDATE_HOURS = (0, 6, 12, 18)

# HA polls every 30 minutes; UPDATE_HOURS above decides whether that poll
# actually triggers a new API call, so we don't hammer Open-Meteo needlessly.
SCAN_INTERVAL = timedelta(minutes=30)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities,
):
    """Set up fishing assistant sensors from a config entry."""
    # Options set via the OptionsFlow are stored in config_entry.options,
    # not config_entry.data - merge them in (options win) so changes made
    # through the options dialog (e.g. the fish list) actually take effect
    # instead of silently being ignored on the next reload/restart.
    data = {**config_entry.data, **config_entry.options}

    name = data["name"]
    lat = data["latitude"]
    lon = data["longitude"]
    fish_list = data["fish"]
    body_type = data["body_type"]
    timezone = data["timezone"]
    elevation = data["elevation"]

    sensors = [
        FishScoreSensor(
            name=name,
            fish=fish,
            lat=lat,
            lon=lon,
            timezone=timezone,
            body_type=body_type,
            elevation=elevation,
            config_entry_id=config_entry.entry_id,
        )
        for fish in fish_list
    ]

    async_add_entities(sensors)


def _parse_window(window: str | None) -> tuple[int, int] | None:
    """Turn 'HH:MM - HH:MM' or 'HH:MM – HH:MM' into (start, end) as HHMM ints.

    Handles both a plain hyphen and an en dash, since the forecast text
    uses the latter (see README example: '04:00 – 06:00').
    Returns None if the string is missing or unparsable - callers should
    treat that as "no data", not fall back to a made-up time.
    """
    if not window:
        return None

    separator = "–" if "–" in window else "-"
    if separator not in window:
        return None

    try:
        start_str, end_str = (part.strip() for part in window.split(separator, 1))
        start_num = int(start_str.replace(":", ""))
        end_num = int(end_str.replace(":", ""))
    except (ValueError, AttributeError):
        return None

    return start_num, end_num


class FishScoreSensor(SensorEntity):
    """Representation of a Fishing Assistant Sensor."""

    should_poll = True

    def __init__(self, name, fish, lat, lon, body_type, timezone, elevation, config_entry_id):
        """Initialize the sensor."""
        self._last_update_hour = None
        self._config_entry_id = config_entry_id
        self._device_identifier = f"{name}_{lat}_{lon}"
        self._name = f"{name.lower().replace(' ', '_')}_{fish}_score"
        self._friendly_name = f"{name} ({fish.title()}) Fishing Score"
        self._state = None
        self._forecast: dict = {}
        self._attrs = {
            "fish": fish,
            "location": name,
            "lat": lat,
            "lon": lon,
            "body_type": body_type,
            "timezone": timezone,
            "elevation": elevation,
        }

    def _local_now(self) -> datetime.datetime:
        """Return 'now' in this location's own timezone.

        Using dt_util.now() (the Home Assistant instance's configured
        timezone) is wrong here if the fishing spot is in a different
        timezone than the HA server - around midnight, that mismatch
        can shift 'today' by a whole day and mislabel best_window as
        'Tomorrow' when it's actually today's window (or vice versa).
        """
        try:
            tz = ZoneInfo(self._attrs["timezone"])
        except Exception:
            return dt_util.now()
        return dt_util.now(tz)

    @property
    def name(self):
        """Return the friendly name of the sensor."""
        return self._friendly_name

    @property
    def unique_id(self):
        """Return a unique ID for this sensor."""
        return self._name

    @property
    def state_class(self):
        """Return the state class of the sensor for continuous history graphs."""
        return SensorStateClass.MEASUREMENT

    @property
    def icon(self):
        """Return the icon to use in the frontend."""
        return "mdi:fish"

    @property
    def native_value(self):
        """Return the state of the sensor as an integer."""
        return int(self._state) if self._state is not None else None

    @property
    def extra_state_attributes(self):
        """Report the forecast plus a live 'next top window' derived from it."""
        res_attrs = dict(self._attrs)
        res_attrs["forecast"] = self._forecast
        res_attrs["next_top_window"] = "No data"
        res_attrs["window_active"] = False

        if not self._forecast:
            return res_attrs

        now = self._local_now()
        today_str = now.strftime("%Y-%m-%d")
        tomorrow_str = (now + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        now_num = int(now.strftime("%H%M"))

        today_window = self._forecast.get(today_str, {}).get("best_window")
        tomorrow_window = self._forecast.get(tomorrow_str, {}).get("best_window")

        today_parsed = _parse_window(today_window)
        if today_parsed:
            start_num, end_num = today_parsed
            if now_num <= end_num:
                res_attrs["next_top_window"] = f"Today: {today_window}"
                res_attrs["window_active"] = start_num <= now_num <= end_num
                return res_attrs

        if tomorrow_window:
            res_attrs["next_top_window"] = f"Tomorrow: {tomorrow_window}"

        return res_attrs

    @property
    def device_info(self):
        """Return device information about this fishing entity."""
        return {
            "identifiers": {(DOMAIN, self._device_identifier)},
            "name": self._attrs["location"],
            "manufacturer": "Fishing Assistant",
            "model": "Fish Score Sensor",
            "entry_type": "service",
        }

    async def async_update(self):
        """Fetch the 7-day forecast (at most once per hour, on the hour marks)."""
        now = self._local_now()

        if self._last_update_hour is not None:
            if now.hour not in UPDATE_HOURS or self._last_update_hour == now.hour:
                return

        forecast = await get_fish_score_forecast(
            hass=self.hass,
            fish=self._attrs["fish"],
            lat=self._attrs["lat"],
            lon=self._attrs["lon"],
            timezone=self._attrs["timezone"],
            elevation=self._attrs["elevation"],
            body_type=self._attrs["body_type"],
        )

        today_str = now.strftime("%Y-%m-%d")
        today_data = forecast.get(today_str, {})

        self._state = today_data.get("score", 0)
        self._forecast = forecast
        self._last_update_hour = now.hour

    async def async_added_to_hass(self):
        """Run when entity about to be added to hass."""
        await self.async_update()

        # extra_state_attributes (window_active / next_top_window) is only
        # recomputed when HA writes the entity's state, which for a polling
        # entity normally happens once per SCAN_INTERVAL (30 min). That's
        # fine for fetching new forecast data, but it means a window's
        # start/end can lag up to ~30 min behind real time. Re-write the
        # state every minute - cheap, no API call - so those two attributes
        # stay accurate without needing to poll (and re-fetch) more often.
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_refresh_state, datetime.timedelta(minutes=1)
            )
        )

    @callback
    def _async_refresh_state(self, now=None):
        """Re-write state so window_active/next_top_window reflect the current time."""
        self.async_write_ha_state()
