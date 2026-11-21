"""Config flow for Energy Manager integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_BATTERY_SOC_SENSOR,
    CONF_BATTERIES,
    CONF_CHARGE_SWITCH,
    CONF_CUSTOM_LOAD_POWER_NUMBER,
    CONF_DISCHARGE_SWITCH,
    CONF_GRID_POWER_SENSOR,
    CONF_MAX_CHARGE_POWER_NUMBER,
    CONF_POWER_SUPPLY_MODE_SELECT,
    CONF_POWERSTREAM_PAIRED,
    CONF_SOLAR_POWER_SENSOR,
    DEFAULT_CHARGE_POWER_STEP,
    DEFAULT_DEADBAND,
    DEFAULT_EV_CHARGER_PHASES,
    DEFAULT_EV_MAX_CHARGING_CURRENT,
    DEFAULT_EV_MIN_CHARGING_CURRENT,
    DEFAULT_EV_MIN_BATTERY_SOC,
    DEFAULT_EV_MIN_EXCESS_POWER,
    DEFAULT_FEED_IN_MODE,
    DEFAULT_FEED_IN_DEADBAND,
    DEFAULT_FEED_IN_POWER_STEP,
    DEFAULT_FEED_IN_STATIC_POWER,
    DEFAULT_MAX_CHARGE_POWER,
    DEFAULT_BATTERY_CAPACITY_WH,
    DEFAULT_MAX_GRID_FEED_IN_POWER,
    DEFAULT_MAX_GRID_IMPORT_SOLAR_CHARGE,
    DEFAULT_MIN_BATTERY_SOC,
    DEFAULT_MIN_CHARGE_POWER,
    DEFAULT_MIN_DWELL_TIME,
    DEFAULT_GRID_POWER_TOLERANCE_DISCHARGE,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    FEED_IN_DYNAMIC,
    FEED_IN_STATIC,
    OPT_CHARGE_POWER_STEP,
    OPT_DEADBAND,
    OPT_EV_CHARGER_CURRENT_NUMBER,
    OPT_EV_CHARGER_PHASES,
    OPT_EV_CHARGER_SWITCH,
    OPT_EV_MIN_BATTERY_SOC,
    OPT_EV_MAX_CHARGING_CURRENT,
    OPT_EV_MIN_CHARGING_CURRENT,
    OPT_EV_MIN_EXCESS_POWER,
    OPT_FEED_IN_MODE,
    OPT_FEED_IN_DEADBAND,
    OPT_FEED_IN_POWER_STEP,
    OPT_FEED_IN_STATIC_POWER,
    OPT_GRID_POWER_TOLERANCE_DISCHARGE,
    OPT_MAX_CHARGE_POWER,
    OPT_MAX_GRID_FEED_IN_POWER,
    OPT_MAX_GRID_IMPORT_SOLAR_CHARGE,
    OPT_MIN_BATTERY_SOC,
    OPT_MIN_CHARGE_POWER,
    OPT_MIN_DWELL_TIME,
    OPT_DEVICE_SETTINGS,
    OPT_BATTERY_CAPACITY,
    OPT_UPDATE_INTERVAL,
)

ENTITY_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_GRID_POWER_SENSOR): EntitySelector(
            EntitySelectorConfig(domain="sensor")
        ),
        vol.Required(CONF_SOLAR_POWER_SENSOR): EntitySelector(
            EntitySelectorConfig(domain="sensor")
        ),
        vol.Required(CONF_BATTERY_SOC_SENSOR): EntitySelector(
            EntitySelectorConfig(domain="sensor", multiple=True)
        ),
        vol.Required(CONF_CHARGE_SWITCH): EntitySelector(
            EntitySelectorConfig(domain="switch")
        ),
        vol.Required(CONF_DISCHARGE_SWITCH): EntitySelector(
            EntitySelectorConfig(domain="switch")
        ),
        vol.Optional(OPT_EV_CHARGER_SWITCH): EntitySelector(
            EntitySelectorConfig(domain="switch")
        ),
        vol.Optional(OPT_EV_CHARGER_CURRENT_NUMBER): EntitySelector(
            EntitySelectorConfig(domain="number")
        ),
    }
)

# Keys that belong in options, not data (when submitted via setup flow)
_EV_ENTITY_KEYS = {OPT_EV_CHARGER_SWITCH, OPT_EV_CHARGER_CURRENT_NUMBER}


def _options_schema(
    options: dict[str, Any] | None = None,
    batteries: list[dict[str, Any]] | None = None,
) -> vol.Schema:
    """Build the options schema with current values as defaults."""
    if options is None:
        options = {}
    fields: dict[Any, Any] = {
            vol.Required(
                OPT_FEED_IN_MODE,
                default=options.get(OPT_FEED_IN_MODE, DEFAULT_FEED_IN_MODE),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        {"label": "Dynamic", "value": FEED_IN_DYNAMIC},
                        {"label": "Static", "value": FEED_IN_STATIC},
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                OPT_FEED_IN_STATIC_POWER,
                default=options.get(
                    OPT_FEED_IN_STATIC_POWER, DEFAULT_FEED_IN_STATIC_POWER
                ),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=800,
                    step=50,
                    unit_of_measurement="W",
                    mode=NumberSelectorMode.SLIDER,
                )
            ),
            vol.Required(
                OPT_MIN_BATTERY_SOC,
                default=options.get(OPT_MIN_BATTERY_SOC, DEFAULT_MIN_BATTERY_SOC),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=100,
                    step=5,
                    unit_of_measurement="%",
                    mode=NumberSelectorMode.SLIDER,
                )
            ),
            vol.Required(
                OPT_MAX_GRID_FEED_IN_POWER,
                default=options.get(
                    OPT_MAX_GRID_FEED_IN_POWER, DEFAULT_MAX_GRID_FEED_IN_POWER
                ),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=800,
                    step=50,
                    unit_of_measurement="W",
                    mode=NumberSelectorMode.SLIDER,
                )
            ),
            vol.Required(
                OPT_GRID_POWER_TOLERANCE_DISCHARGE,
                default=options.get(
                    OPT_GRID_POWER_TOLERANCE_DISCHARGE,
                    DEFAULT_GRID_POWER_TOLERANCE_DISCHARGE,
                ),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=1000,
                    step=10,
                    unit_of_measurement="W",
                    mode=NumberSelectorMode.SLIDER,
                )
            ),
            vol.Required(
                OPT_MAX_GRID_IMPORT_SOLAR_CHARGE,
                default=options.get(
                    OPT_MAX_GRID_IMPORT_SOLAR_CHARGE,
                    DEFAULT_MAX_GRID_IMPORT_SOLAR_CHARGE,
                ),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=500,
                    step=10,
                    unit_of_measurement="W",
                    mode=NumberSelectorMode.SLIDER,
                )
            ),
            vol.Required(
                OPT_MAX_CHARGE_POWER,
                default=options.get(OPT_MAX_CHARGE_POWER, DEFAULT_MAX_CHARGE_POWER),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=100,
                    max=2400,
                    step=100,
                    unit_of_measurement="W",
                    mode=NumberSelectorMode.SLIDER,
                )
            ),
            vol.Required(
                OPT_MIN_CHARGE_POWER,
                default=options.get(OPT_MIN_CHARGE_POWER, DEFAULT_MIN_CHARGE_POWER),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=100,
                    max=1200,
                    step=100,
                    unit_of_measurement="W",
                    mode=NumberSelectorMode.SLIDER,
                )
            ),
            vol.Required(
                OPT_UPDATE_INTERVAL,
                default=options.get(OPT_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=1,
                    max=120,
                    step=1,
                    unit_of_measurement="s",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                OPT_DEADBAND,
                default=options.get(OPT_DEADBAND, DEFAULT_DEADBAND),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=10,
                    max=200,
                    step=10,
                    unit_of_measurement="W",
                    mode=NumberSelectorMode.SLIDER,
                )
            ),
            vol.Required(
                OPT_FEED_IN_DEADBAND,
                default=options.get(OPT_FEED_IN_DEADBAND, DEFAULT_FEED_IN_DEADBAND),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=200,
                    step=1,
                    unit_of_measurement="W",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                OPT_CHARGE_POWER_STEP,
                default=options.get(
                    OPT_CHARGE_POWER_STEP, DEFAULT_CHARGE_POWER_STEP
                ),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=1,
                    max=200,
                    step=1,
                    unit_of_measurement="W",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                OPT_FEED_IN_POWER_STEP,
                default=options.get(
                    OPT_FEED_IN_POWER_STEP, DEFAULT_FEED_IN_POWER_STEP
                ),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=10,
                    max=200,
                    step=10,
                    unit_of_measurement="W",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                OPT_MIN_DWELL_TIME,
                default=options.get(OPT_MIN_DWELL_TIME, DEFAULT_MIN_DWELL_TIME),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=300,
                    step=10,
                    unit_of_measurement="s",
                    mode=NumberSelectorMode.BOX,
                )
            ),
        }
    if batteries:
        hidden_keys = (
            OPT_MIN_BATTERY_SOC,
            OPT_MAX_GRID_FEED_IN_POWER,
            OPT_MAX_CHARGE_POWER,
            OPT_MIN_CHARGE_POWER,
            OPT_BATTERY_CAPACITY,
        )
        for field in list(fields):
            if getattr(field, "schema", field) in hidden_keys:
                fields.pop(field)
    return vol.Schema(fields)


def _device_options_schema(settings: dict[str, Any]) -> vol.Schema:
    """Build per-battery capacity and power limit controls."""
    return vol.Schema(
        {
            vol.Required(OPT_MIN_BATTERY_SOC, default=settings.get("min_soc", DEFAULT_MIN_BATTERY_SOC)): NumberSelector(
                NumberSelectorConfig(min=0, max=100, step=5, unit_of_measurement="%", mode=NumberSelectorMode.SLIDER)
            ),
            vol.Required(OPT_MAX_GRID_FEED_IN_POWER, default=settings.get("max_feed_in", DEFAULT_MAX_GRID_FEED_IN_POWER)): NumberSelector(
                NumberSelectorConfig(min=0, max=800, step=50, unit_of_measurement="W", mode=NumberSelectorMode.SLIDER)
            ),
            vol.Required(OPT_MAX_CHARGE_POWER, default=settings.get("max_charge", DEFAULT_MAX_CHARGE_POWER)): NumberSelector(
                NumberSelectorConfig(min=100, max=2400, step=100, unit_of_measurement="W", mode=NumberSelectorMode.SLIDER)
            ),
            vol.Required(OPT_MIN_CHARGE_POWER, default=settings.get("min_charge", DEFAULT_MIN_CHARGE_POWER)): NumberSelector(
                NumberSelectorConfig(min=100, max=1200, step=100, unit_of_measurement="W", mode=NumberSelectorMode.SLIDER)
            ),
            vol.Required(OPT_BATTERY_CAPACITY, default=settings.get("capacity_wh", DEFAULT_BATTERY_CAPACITY_WH)): NumberSelector(
                NumberSelectorConfig(min=100, max=30000, step=100, unit_of_measurement="Wh", mode=NumberSelectorMode.BOX)
            ),
        }
    )


def _updated_battery_config(
    entry_data: dict[str, Any],
    entry_options: dict[str, Any],
    selected_sensors: list[str] | str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Update battery selection while preserving settings for retained batteries."""
    if isinstance(selected_sensors, str):
        selected_sensors = [selected_sensors]
    selected_sensors = list(dict.fromkeys(selected_sensors))
    if not selected_sensors:
        raise ValueError("At least one battery SOC sensor must be selected")

    data = dict(entry_data)
    data[CONF_BATTERY_SOC_SENSOR] = selected_sensors
    data.pop(CONF_BATTERIES, None)  # force control rediscovery after reload

    current_settings = entry_options.get(OPT_DEVICE_SETTINGS, {})
    defaults = {
        "min_soc": entry_options.get(OPT_MIN_BATTERY_SOC, DEFAULT_MIN_BATTERY_SOC),
        "max_feed_in": entry_options.get(
            OPT_MAX_GRID_FEED_IN_POWER, DEFAULT_MAX_GRID_FEED_IN_POWER
        ),
        "max_charge": entry_options.get(OPT_MAX_CHARGE_POWER, DEFAULT_MAX_CHARGE_POWER),
        "min_charge": entry_options.get(OPT_MIN_CHARGE_POWER, DEFAULT_MIN_CHARGE_POWER),
        "capacity_wh": DEFAULT_BATTERY_CAPACITY_WH,
    }
    options = dict(entry_options)
    options[OPT_DEVICE_SETTINGS] = {
        sensor: dict(current_settings.get(sensor, defaults))
        for sensor in selected_sensors
    }
    return data, options


def _reconfigure_schema(entry_data: dict[str, Any]) -> vol.Schema:
    """Build the battery selector with the current selection suggested."""
    sensors = entry_data.get(CONF_BATTERY_SOC_SENSOR, [])
    if isinstance(sensors, str):
        sensors = [sensors]
    return vol.Schema(
        {
            vol.Required(
                CONF_BATTERY_SOC_SENSOR,
                default=sensors,
            ): EntitySelector(
                EntitySelectorConfig(domain="sensor", multiple=True)
            )
        }
    )


def _battery_pairing_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Build selectors that explicitly associate one PowerStream to a battery."""
    defaults = defaults or {}
    fields: dict[Any, Any] = {}
    for key, domain in (
        (CONF_CUSTOM_LOAD_POWER_NUMBER, "number"),
        (CONF_POWER_SUPPLY_MODE_SELECT, "select"),
    ):
        default = defaults.get(key)
        marker = vol.Required(key, default=default) if default else vol.Required(key)
        fields[marker] = EntitySelector(EntitySelectorConfig(domain=domain))
    return vol.Schema(fields)


def _make_battery_association(
    sensor: str,
    user_input: dict[str, Any],
    charge_power_entity: str | None = None,
) -> dict[str, Any]:
    """Build an explicit association between an SOC sensor and its PowerStream."""
    battery = {
        CONF_BATTERY_SOC_SENSOR: sensor,
        CONF_CUSTOM_LOAD_POWER_NUMBER: user_input[CONF_CUSTOM_LOAD_POWER_NUMBER],
        CONF_POWER_SUPPLY_MODE_SELECT: user_input[CONF_POWER_SUPPLY_MODE_SELECT],
        CONF_POWERSTREAM_PAIRED: True,
    }
    if charge_power_entity:
        battery[CONF_MAX_CHARGE_POWER_NUMBER] = charge_power_entity
    return battery


def _battery_name(hass: Any, sensor: str) -> str:
    """Return a readable name for a battery SOC entity."""
    state = hass.states.get(sensor)
    attributes = getattr(state, "attributes", {}) if state else {}
    name = attributes.get("friendly_name") if hasattr(attributes, "get") else None
    if isinstance(name, str) and name:
        return name
    return sensor


class EnergyManagerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Energy Manager."""

    VERSION = 1

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Change the battery sensors without recreating the config entry."""
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            selected_sensors = user_input[CONF_BATTERY_SOC_SENSOR]
            if not selected_sensors:
                return self.async_show_form(
                    step_id="reconfigure",
                    data_schema=_reconfigure_schema(entry.data),
                    errors={"base": "no_battery_selected"},
                )
            data, options = _updated_battery_config(
                dict(entry.data),
                dict(entry.options),
                selected_sensors,
            )
            self._reconfigure_entry = entry
            self._reconfigure_data = data
            self._reconfigure_options = options
            existing = {
                battery[CONF_BATTERY_SOC_SENSOR]: battery
                for battery in entry.data.get(CONF_BATTERIES, [])
            }
            self._pairing_sensors = data[CONF_BATTERY_SOC_SENSOR]
            self._pairing_index = 0
            self._battery_pairs: list[dict[str, Any]] = []
            self._existing_battery_pairs = existing
            return await self.async_step_reconfigure_pairing()

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_reconfigure_schema(entry.data),
        )

    async def async_step_reconfigure_pairing(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Explicitly pair each selected battery with its PowerStream controls."""
        if user_input is not None:
            sensor = self._pairing_sensors[self._pairing_index]
            previous = self._existing_battery_pairs.get(sensor, {})
            self._battery_pairs.append(
                _make_battery_association(
                    sensor,
                    user_input,
                    previous.get(CONF_MAX_CHARGE_POWER_NUMBER),
                )
            )
            self._pairing_index += 1
            if self._pairing_index >= len(self._pairing_sensors):
                self._reconfigure_data[CONF_BATTERIES] = self._battery_pairs
                return self.async_update_reload_and_abort(
                    self._reconfigure_entry,
                    data=self._reconfigure_data,
                    options=self._reconfigure_options,
                )

        sensor = self._pairing_sensors[self._pairing_index]
        defaults = self._existing_battery_pairs.get(sensor, {})
        battery_name = _battery_name(self.hass, sensor)
        self.context.setdefault("title_placeholders", {})["battery"] = battery_name
        return self.async_show_form(
            step_id="reconfigure_pairing",
            data_schema=_battery_pairing_schema(defaults),
            description_placeholders={"battery": battery_name},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the entity selection step."""
        if user_input is not None:
            # Extract optional EV entity IDs → store in options (not data)
            self._setup_ev_options: dict[str, Any] = {}
            for key in _EV_ENTITY_KEYS:
                if key in user_input:
                    self._setup_ev_options[key] = user_input.pop(key)

            soc_sensors = user_input.get(CONF_BATTERY_SOC_SENSOR, [])
            if isinstance(soc_sensors, str):
                soc_sensors = [soc_sensors]
            if not soc_sensors:
                return self.async_show_form(
                    step_id="user",
                    data_schema=ENTITY_SCHEMA,
                    errors={"base": "no_battery_selected"},
                )
            self._setup_data = user_input
            self._pairing_sensors = list(dict.fromkeys(soc_sensors))
            self._pairing_index = 0
            self._battery_pairs = []
            return await self.async_step_battery_pairing()

        return self.async_show_form(
            step_id="user",
            data_schema=ENTITY_SCHEMA,
        )

    async def async_step_battery_pairing(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Pair each new battery with the exact PowerStream controls it uses."""
        if user_input is not None:
            self._battery_pairs.append(
                _make_battery_association(
                    self._pairing_sensors[self._pairing_index], user_input
                )
            )
            self._pairing_index += 1
            if self._pairing_index < len(self._pairing_sensors):
                return await self.async_step_battery_pairing()

            self._setup_data[CONF_BATTERY_SOC_SENSOR] = self._pairing_sensors
            self._setup_data[CONF_BATTERIES] = self._battery_pairs
            return self.async_create_entry(
                title="Energy Manager",
                data=self._setup_data,
                options={
                    OPT_FEED_IN_MODE: DEFAULT_FEED_IN_MODE,
                    OPT_FEED_IN_STATIC_POWER: DEFAULT_FEED_IN_STATIC_POWER,
                    OPT_MIN_BATTERY_SOC: DEFAULT_MIN_BATTERY_SOC,
                    OPT_MAX_GRID_FEED_IN_POWER: DEFAULT_MAX_GRID_FEED_IN_POWER,
                    OPT_GRID_POWER_TOLERANCE_DISCHARGE: DEFAULT_GRID_POWER_TOLERANCE_DISCHARGE,
                    OPT_MAX_GRID_IMPORT_SOLAR_CHARGE: DEFAULT_MAX_GRID_IMPORT_SOLAR_CHARGE,
                    OPT_MAX_CHARGE_POWER: DEFAULT_MAX_CHARGE_POWER,
                    OPT_MIN_CHARGE_POWER: DEFAULT_MIN_CHARGE_POWER,
                    OPT_UPDATE_INTERVAL: DEFAULT_UPDATE_INTERVAL,
                    OPT_DEADBAND: DEFAULT_DEADBAND,
                    OPT_CHARGE_POWER_STEP: DEFAULT_CHARGE_POWER_STEP,
                    OPT_FEED_IN_POWER_STEP: DEFAULT_FEED_IN_POWER_STEP,
                    OPT_MIN_DWELL_TIME: DEFAULT_MIN_DWELL_TIME,
                    **self._setup_ev_options,
                },
            )

        sensor = self._pairing_sensors[self._pairing_index]
        battery_name = _battery_name(self.hass, sensor)
        self.context.setdefault("title_placeholders", {})["battery"] = battery_name
        return self.async_show_form(
            step_id="battery_pairing",
            data_schema=_battery_pairing_schema(),
            description_placeholders={"battery": battery_name},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow handler."""
        return EnergyManagerOptionsFlow(config_entry)


def _ev_options_schema(options: dict[str, Any] | None = None) -> vol.Schema:
    """Build the EV options schema with current values as defaults."""
    if options is None:
        options = {}
    return vol.Schema(
        {
            vol.Optional(
                OPT_EV_CHARGER_SWITCH,
                description={
                    "suggested_value": options.get(OPT_EV_CHARGER_SWITCH),
                },
            ): EntitySelector(
                EntitySelectorConfig(domain="switch")
            ),
            vol.Optional(
                OPT_EV_CHARGER_CURRENT_NUMBER,
                description={
                    "suggested_value": options.get(OPT_EV_CHARGER_CURRENT_NUMBER),
                },
            ): EntitySelector(
                EntitySelectorConfig(domain="number")
            ),
            vol.Required(
                OPT_EV_MIN_BATTERY_SOC,
                default=options.get(
                    OPT_EV_MIN_BATTERY_SOC, DEFAULT_EV_MIN_BATTERY_SOC
                ),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=100,
                    step=5,
                    unit_of_measurement="%",
                    mode=NumberSelectorMode.SLIDER,
                )
            ),
            vol.Required(
                OPT_EV_MIN_EXCESS_POWER,
                default=options.get(
                    OPT_EV_MIN_EXCESS_POWER, DEFAULT_EV_MIN_EXCESS_POWER
                ),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=500,
                    max=5000,
                    step=100,
                    unit_of_measurement="W",
                    mode=NumberSelectorMode.SLIDER,
                )
            ),
            vol.Required(
                OPT_EV_MIN_CHARGING_CURRENT,
                default=options.get(
                    OPT_EV_MIN_CHARGING_CURRENT, DEFAULT_EV_MIN_CHARGING_CURRENT
                ),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=5,
                    max=16,
                    step=1,
                    unit_of_measurement="A",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                OPT_EV_MAX_CHARGING_CURRENT,
                default=options.get(
                    OPT_EV_MAX_CHARGING_CURRENT, DEFAULT_EV_MAX_CHARGING_CURRENT
                ),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=5,
                    max=32,
                    step=1,
                    unit_of_measurement="A",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                OPT_EV_CHARGER_PHASES,
                default=str(options.get(
                    OPT_EV_CHARGER_PHASES, DEFAULT_EV_CHARGER_PHASES
                )),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        {"label": "1 Phase", "value": "1"},
                        {"label": "3 Phases", "value": "3"},
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


class EnergyManagerOptionsFlow(OptionsFlow):
    """Handle options flow for Energy Manager."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry
        self._battery_options: dict[str, Any] = {}
        self._device_index = 0
        self._device_settings: dict[str, dict[str, Any]] = dict(
            config_entry.options.get(OPT_DEVICE_SETTINGS, {})
        )
        for battery in config_entry.data.get(CONF_BATTERIES, []):
            sensor = battery[CONF_BATTERY_SOC_SENSOR]
            self._device_settings.setdefault(
                sensor,
                {
                    "min_soc": config_entry.options.get(OPT_MIN_BATTERY_SOC, DEFAULT_MIN_BATTERY_SOC),
                    "max_feed_in": config_entry.options.get(OPT_MAX_GRID_FEED_IN_POWER, DEFAULT_MAX_GRID_FEED_IN_POWER),
                    "max_charge": config_entry.options.get(OPT_MAX_CHARGE_POWER, DEFAULT_MAX_CHARGE_POWER),
                    "min_charge": config_entry.options.get(OPT_MIN_CHARGE_POWER, DEFAULT_MIN_CHARGE_POWER),
                    "capacity_wh": DEFAULT_BATTERY_CAPACITY_WH,
                },
            )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the battery options step."""
        if user_input is not None:
            batteries = self._config_entry.data.get(CONF_BATTERIES, [])
            self._battery_list = batteries
            self._battery_options = user_input
            if batteries:
                sensor = batteries[0][CONF_BATTERY_SOC_SENSOR]
                battery_name = _battery_name(self.hass, sensor)
                self.context.setdefault("title_placeholders", {})["battery"] = battery_name
                return self.async_show_form(
                    step_id="device",
                    data_schema=_device_options_schema(
                        self._device_settings.get(sensor, {})
                    ),
                    description_placeholders={"battery": battery_name},
                )
            return await self.async_step_ev()

        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(self._config_entry.options, self._config_entry.data.get(CONF_BATTERIES, [])),
        )

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Collect limits for each selected battery in sequence."""
        batteries = getattr(self, "_battery_list", self._config_entry.data.get(CONF_BATTERIES, []))
        if user_input is not None:
            sensor = batteries[self._device_index][CONF_BATTERY_SOC_SENSOR]
            self._device_settings[sensor] = {
                "min_soc": user_input[OPT_MIN_BATTERY_SOC],
                "max_feed_in": user_input[OPT_MAX_GRID_FEED_IN_POWER],
                "max_charge": user_input[OPT_MAX_CHARGE_POWER],
                "min_charge": user_input[OPT_MIN_CHARGE_POWER],
                "capacity_wh": user_input[OPT_BATTERY_CAPACITY],
            }
            self._device_index += 1
            if self._device_index >= len(batteries):
                self._battery_options[OPT_DEVICE_SETTINGS] = self._device_settings
                return await self.async_step_ev()

        sensor = batteries[self._device_index][CONF_BATTERY_SOC_SENSOR]
        battery_name = _battery_name(self.hass, sensor)
        self.context.setdefault("title_placeholders", {})["battery"] = battery_name
        return self.async_show_form(
            step_id="device",
            data_schema=_device_options_schema(self._device_settings.get(sensor, {})),
            description_placeholders={"battery": battery_name},
        )

    async def async_step_ev(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the EV charging options step."""
        if user_input is not None:
            merged = {**self._battery_options, **user_input}
            return self.async_create_entry(title="", data=merged)

        return self.async_show_form(
            step_id="ev",
            data_schema=_ev_options_schema(self._config_entry.options),
        )
