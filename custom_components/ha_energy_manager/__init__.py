"""The Energy Manager integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    CONF_CHARGE_SWITCH,
    CONF_BATTERIES,
    CONF_BATTERY_SOC_SENSOR,
    CONF_CUSTOM_LOAD_POWER_NUMBER,
    CONF_DISCHARGE_SWITCH,
    CONF_MAX_CHARGE_POWER_NUMBER,
    CONF_POWER_SUPPLY_MODE_SELECT,
    CONF_POWERSTREAM_PAIRED,
    DOMAIN,
    OPT_DEVICE_SETTINGS,
    OPT_MIN_BATTERY_SOC,
    OPT_MAX_GRID_FEED_IN_POWER,
    OPT_MAX_CHARGE_POWER,
    OPT_MIN_CHARGE_POWER,
    DEFAULT_MIN_BATTERY_SOC,
    DEFAULT_MAX_GRID_FEED_IN_POWER,
    DEFAULT_MAX_CHARGE_POWER,
    DEFAULT_MIN_CHARGE_POWER,
    PLATFORMS,
)
from .coordinator import EnergyManagerCoordinator
from .discovery import async_discover_control_entities

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Energy Manager from a config entry."""
    # Merge configured entities with auto-discovered control entities
    entity_ids = dict(entry.data)

    # Auto-discover control entities if not already in config data
    needs_discovery = CONF_BATTERIES not in entity_ids or (
        not isinstance(entity_ids.get(CONF_BATTERY_SOC_SENSOR), list)
        and any(key not in entity_ids for key in (
            CONF_MAX_CHARGE_POWER_NUMBER,
            CONF_CUSTOM_LOAD_POWER_NUMBER,
            CONF_POWER_SUPPLY_MODE_SELECT,
        ))
    ) or any(
        not battery.get(CONF_MAX_CHARGE_POWER_NUMBER)
        for battery in entity_ids.get(CONF_BATTERIES, [])
    )

    if needs_discovery:
        discovered = await async_discover_control_entities(hass, entity_ids)
        entity_ids.update(discovered)

        # Persist the discovered battery/control associations for the per-device
        # options form. Keep the legacy top-level keys for older installations.
        if CONF_BATTERIES in entity_ids:
            hass.config_entries.async_update_entry(entry, data=entity_ids)

        # Verify all required entities are available
        missing = []
        if (CONF_MAX_CHARGE_POWER_NUMBER not in entity_ids
                and not entity_ids.get(CONF_BATTERIES)):
            missing.append("Max Charge Power (number with 'ac_charging_power' in name)")
        if (CONF_CUSTOM_LOAD_POWER_NUMBER not in entity_ids
                and not entity_ids.get(CONF_BATTERIES)):
            missing.append("Custom Load Power (number with 'custom_load_power' in name)")
        if (CONF_POWER_SUPPLY_MODE_SELECT not in entity_ids
                and not entity_ids.get(CONF_BATTERIES)):
            missing.append("Power Supply Mode (select with 'power_supply_mode' in name)")

        if missing:
            raise ConfigEntryNotReady(
                f"Could not auto-discover EcoFlow control entities: {', '.join(missing)}. "
                f"Make sure the EcoFlow integration is loaded and entities are available."
            )


    incomplete = [
        battery.get(CONF_BATTERY_SOC_SENSOR, "unknown")
        for battery in entity_ids.get(CONF_BATTERIES, [])
        if not battery.get(CONF_MAX_CHARGE_POWER_NUMBER)
    ]
    if incomplete:
        raise ConfigEntryNotReady(
            "Could not find a charge power number on the same device as: "
            + ", ".join(incomplete)
        )

    unpaired = [
        battery.get(CONF_BATTERY_SOC_SENSOR, "unknown")
        for battery in entity_ids.get(CONF_BATTERIES, [])
        if len(entity_ids.get(CONF_BATTERIES, [])) > 1
        and not battery.get(CONF_POWERSTREAM_PAIRED)
    ]
    if unpaired:
        _LOGGER.warning(
            "PowerStream associations are not confirmed for %s. Use Reconfigure "
            "to select the feed-in and mode controls for each battery; feed-in "
            "is disabled for unpaired batteries.",
            ", ".join(unpaired),
        )

    if entity_ids.get(CONF_BATTERIES) and not entry.options.get(OPT_DEVICE_SETTINGS):
        legacy_settings = {
            battery[CONF_BATTERY_SOC_SENSOR]: {
                "min_soc": entry.options.get(OPT_MIN_BATTERY_SOC, DEFAULT_MIN_BATTERY_SOC),
                "max_feed_in": entry.options.get(OPT_MAX_GRID_FEED_IN_POWER, DEFAULT_MAX_GRID_FEED_IN_POWER),
                "max_charge": entry.options.get(OPT_MAX_CHARGE_POWER, DEFAULT_MAX_CHARGE_POWER),
                "min_charge": entry.options.get(OPT_MIN_CHARGE_POWER, DEFAULT_MIN_CHARGE_POWER),
            }
            for battery in entity_ids[CONF_BATTERIES]
        }
        options = {**entry.options, OPT_DEVICE_SETTINGS: legacy_settings}
        hass.config_entries.async_update_entry(entry, options=options)

    # Verify switch entities are configured
    missing_switches = []
    if CONF_CHARGE_SWITCH not in entity_ids:
        missing_switches.append("Charge Switch (Shelly relay for charger)")
    if CONF_DISCHARGE_SWITCH not in entity_ids:
        missing_switches.append("Discharge Switch (Shelly relay for PowerStream)")
    if missing_switches:
        raise ConfigEntryNotReady(
            f"Missing switch entities: {', '.join(missing_switches)}. "
            f"Please reconfigure the integration and select the Shelly switch entities."
        )

    if needs_discovery:
        _LOGGER.info(
            "Auto-discovered control entities: charge_power=%s, custom_load=%s, ps_mode=%s",
            entity_ids.get(CONF_MAX_CHARGE_POWER_NUMBER),
            entity_ids.get(CONF_CUSTOM_LOAD_POWER_NUMBER),
            entity_ids.get(CONF_POWER_SUPPLY_MODE_SELECT),
        )

    coordinator = EnergyManagerCoordinator(hass, entry, entity_ids)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator: EnergyManagerCoordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_shutdown()

    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    coordinator: EnergyManagerCoordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.update_options()
