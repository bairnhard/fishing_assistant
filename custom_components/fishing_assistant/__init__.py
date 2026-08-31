import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import config_validation as cv
from homeassistant.core import HomeAssistant
from .const import DOMAIN
from .helpers.location import resolve_location_metadata_sync
_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["sensor"]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up Fishing Assistant from YAML (not used)."""
    return True
async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Fishing Assistant from a config entry."""
    _LOGGER.debug("Setting up entry: %s", entry.entry_id)
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = entry.data
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_update_listener))
    return True
async def async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry whenever its options are updated (e.g. via the options flow)."""
    await hass.config_entries.async_reload(entry.entry_id)
async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old config entries to the latest version."""
    _LOGGER.debug("Migrating entry %s from version %s", entry.entry_id, entry.version)
    if entry.version == 1:
        lat = entry.data.get("latitude")
        lon = entry.data.get("longitude")
        if lat is None or lon is None:
            _LOGGER.warning(
                "Skipping timezone/elevation migration for entry %s due to missing coordinates",
                entry.entry_id,
            )
            hass.config_entries.async_update_entry(entry, version=2)
            return True
        metadata = await hass.async_add_executor_job(resolve_location_metadata_sync, lat, lon)
        new_data = dict(entry.data)
        new_data["timezone"] = metadata.get("timezone", entry.data.get("timezone"))
        new_data["elevation"] = metadata.get("elevation", entry.data.get("elevation"))
        hass.config_entries.async_update_entry(entry, data=new_data, version=2)
        _LOGGER.info("Successfully migrated entry %s to version 2", entry.entry_id)
    return True
async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug("Unloading entry: %s", entry.entry_id)
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
