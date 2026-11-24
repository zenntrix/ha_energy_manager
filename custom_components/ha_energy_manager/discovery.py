"""Auto-discovery of EcoFlow control entities."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_BATTERY_SOC_SENSOR,
    CONF_BATTERY_CHARGE_SWITCH,
    CONF_BATTERIES,
    CONF_CUSTOM_LOAD_POWER_NUMBER,
    CONF_GRID_POWER_SENSOR,
    CONF_MAX_CHARGE_POWER_NUMBER,
    CONF_POWER_SUPPLY_MODE_SELECT,
    CONF_POWERSTREAM_PAIRED,
    CONF_SOLAR_POWER_SENSOR,
)

_LOGGER = logging.getLogger(__name__)

# Patterns to match entity IDs (substrings, case-insensitive)
_CHARGE_POWER_PATTERNS = ["ac_charging_power", "charge_power"]
_CUSTOM_LOAD_PATTERNS = ["custom_load_power"]
_PS_MODE_PATTERNS = ["power_supply_mode", "supply_priority"]


def _match_entity(entity_id: str, patterns: list[str]) -> bool:
    """Check if an entity_id matches any of the given patterns."""
    lower = entity_id.lower()
    return any(p in lower for p in patterns)


def _find_matching(
    candidates: list[str],
    patterns: list[str],
) -> str | None:
    """Find the first entity matching any pattern."""
    for entity_id in candidates:
        if _match_entity(entity_id, patterns):
            return entity_id
    return None


async def async_discover_control_entities(
    hass: HomeAssistant,
    config_data: dict[str, Any],
) -> dict[str, Any]:
    """Discover EcoFlow control entities.

    Strategy:
    1. Find devices from configured sensor entities
    2. Preserve the user-selected charge switch and PowerStream controls
    3. Discover battery charge-power controls from that battery's device

    Returns discovered legacy IDs and per-battery associations.
    """
    registry = er.async_get(hass)

    # Collect device IDs from the configured entities
    configured_entity_ids = [
        entity_id
        for key in (
            CONF_GRID_POWER_SENSOR,
            CONF_SOLAR_POWER_SENSOR,
            CONF_BATTERY_SOC_SENSOR,
        )
        if key in config_data
        for entity_id in (
            config_data[key]
            if isinstance(config_data[key], list)
            else [config_data[key]]
        )
    ]

    device_ids: set[str] = set()
    for entity_id in configured_entity_ids:
        entry = registry.async_get(entity_id)
        if entry and entry.device_id:
            device_ids.add(entry.device_id)

    _LOGGER.debug(
        "Auto-discovery: found %d devices from configured entities: %s",
        len(device_ids),
        device_ids,
    )

    # Collect candidates from device siblings
    device_numbers: list[str] = []
    device_selects: list[str] = []
    # Also collect ALL entities as fallback
    all_numbers: list[str] = []
    all_selects: list[str] = []

    for entry in registry.entities.values():
        if entry.domain == "number":
            all_numbers.append(entry.entity_id)
            if entry.device_id in device_ids:
                device_numbers.append(entry.entity_id)
        elif entry.domain == "select":
            all_selects.append(entry.entity_id)
            if entry.device_id in device_ids:
                device_selects.append(entry.entity_id)

    batteries: list[dict[str, Any]] = []
    solar_id = config_data.get(CONF_SOLAR_POWER_SENSOR)
    solar_entry = registry.async_get(solar_id) if solar_id else None
    soc_entities = config_data.get(CONF_BATTERY_SOC_SENSOR, [])
    if isinstance(soc_entities, str):
        soc_entities = [soc_entities]
    configured_batteries = {
        battery.get(CONF_BATTERY_SOC_SENSOR): battery
        for battery in config_data.get(CONF_BATTERIES, [])
    }
    for soc_entity_id in soc_entities:
        soc_entry = registry.async_get(soc_entity_id)
        battery_device = soc_entry.device_id if soc_entry else None
        sibling_numbers = [
            item.entity_id for item in registry.entities.values()
            if battery_device and item.domain == "number" and item.device_id == battery_device
        ]
        sibling_selects = [
            item.entity_id for item in registry.entities.values()
            if battery_device and item.domain == "select" and item.device_id == battery_device
        ]
        battery = {CONF_BATTERY_SOC_SENSOR: soc_entity_id}
        charge = _find_matching(sibling_numbers, _CHARGE_POWER_PATTERNS)
        load = _find_matching(sibling_numbers, _CUSTOM_LOAD_PATTERNS)
        mode = _find_matching(sibling_selects, _PS_MODE_PATTERNS)
        if len(soc_entities) == 1 and solar_entry and solar_entry.device_id:
            ps_numbers = [
                item.entity_id for item in registry.entities.values()
                if item.domain == "number" and item.device_id == solar_entry.device_id
            ]
            ps_selects = [
                item.entity_id for item in registry.entities.values()
                if item.domain == "select" and item.device_id == solar_entry.device_id
            ]
            load = load or _find_matching(ps_numbers, _CUSTOM_LOAD_PATTERNS)
            mode = mode or _find_matching(ps_selects, _PS_MODE_PATTERNS)
        if charge:
            battery[CONF_MAX_CHARGE_POWER_NUMBER] = charge
        if load:
            battery[CONF_CUSTOM_LOAD_POWER_NUMBER] = load
        if mode:
            battery[CONF_POWER_SUPPLY_MODE_SELECT] = mode
        configured = configured_batteries.get(soc_entity_id, {})
        # Preserve the explicit PowerStream-to-battery association entered in
        # setup or Reconfigure. Discovery fills missing battery-side controls.
        battery.update(
            {
                key: configured[key]
                for key in (
                    CONF_MAX_CHARGE_POWER_NUMBER,
                    CONF_BATTERY_CHARGE_SWITCH,
                    CONF_CUSTOM_LOAD_POWER_NUMBER,
                    CONF_POWER_SUPPLY_MODE_SELECT,
                    CONF_POWERSTREAM_PAIRED,
                )
                if configured.get(key)
            }
        )
        batteries.append(battery)

    _LOGGER.debug(
        "Auto-discovery candidates: device(%d numbers, %d selects), "
        "all(%d numbers, %d selects)",
        len(device_numbers),
        len(device_selects),
        len(all_numbers),
        len(all_selects),
    )

    # Prefer entities on configured devices. Only use the global fallback when
    # none of the configured entities has a registry device (legacy/unpaired
    # entities). Falling back whenever a sibling is missing can silently bind
    # this entry to a different EcoFlow battery when several are installed.
    discovered: dict[str, Any] = {}
    if batteries:
        discovered[CONF_BATTERIES] = batteries  # type: ignore[assignment]

    fallback_numbers = all_numbers if not device_ids else []
    fallback_selects = all_selects if not device_ids else []

    # Max charge power number
    match = _find_matching(device_numbers, _CHARGE_POWER_PATTERNS)
    if not match:
        match = _find_matching(fallback_numbers, _CHARGE_POWER_PATTERNS)
        if match:
            _LOGGER.info("Auto-discovered charge power (global search): %s", match)
    else:
        _LOGGER.info("Auto-discovered charge power (device): %s", match)
    if match:
        discovered[CONF_MAX_CHARGE_POWER_NUMBER] = match

    # Custom load power number
    match = _find_matching(device_numbers, _CUSTOM_LOAD_PATTERNS)
    if not match:
        match = _find_matching(fallback_numbers, _CUSTOM_LOAD_PATTERNS)
        if match:
            _LOGGER.info("Auto-discovered custom load power (global search): %s", match)
    else:
        _LOGGER.info("Auto-discovered custom load power (device): %s", match)
    if match:
        discovered[CONF_CUSTOM_LOAD_POWER_NUMBER] = match

    # Power supply mode select
    match = _find_matching(device_selects, _PS_MODE_PATTERNS)
    if not match:
        match = _find_matching(fallback_selects, _PS_MODE_PATTERNS)
        if match:
            _LOGGER.info("Auto-discovered power supply mode (global search): %s", match)
    else:
        _LOGGER.info("Auto-discovered power supply mode (device): %s", match)
    if match:
        discovered[CONF_POWER_SUPPLY_MODE_SELECT] = match

    # Charge controls remain battery-specific; never share one battery's
    # charging number with another selected battery.
    if len(batteries) == 1:
        if CONF_MAX_CHARGE_POWER_NUMBER not in batteries[0] and CONF_MAX_CHARGE_POWER_NUMBER in discovered:
            batteries[0][CONF_MAX_CHARGE_POWER_NUMBER] = discovered[CONF_MAX_CHARGE_POWER_NUMBER]

    # Log what was not found
    missing = []
    if CONF_MAX_CHARGE_POWER_NUMBER not in discovered:
        missing.append("max_charge_power_number (patterns: %s)" % _CHARGE_POWER_PATTERNS)
    if CONF_CUSTOM_LOAD_POWER_NUMBER not in discovered:
        missing.append("custom_load_power_number (patterns: %s)" % _CUSTOM_LOAD_PATTERNS)
    if CONF_POWER_SUPPLY_MODE_SELECT not in discovered:
        missing.append("power_supply_mode_select (patterns: %s)" % _PS_MODE_PATTERNS)

    if missing:
        _LOGGER.warning(
            "Auto-discovery could not find: %s. "
            "Available number entities: %s. Available select entities: %s",
            missing,
            all_numbers,
            all_selects,
        )

    return discovered
