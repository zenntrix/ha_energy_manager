"""Tests for Energy Manager config flow helpers."""

from unittest.mock import MagicMock

from custom_components.ha_energy_manager.config_flow import (
    _battery_name,
    _battery_pairing_schema,
    _make_battery_association,
    _updated_battery_config,
)
from custom_components.ha_energy_manager.const import (
    CONF_BATTERY_SOC_SENSOR,
    CONF_BATTERY_CHARGE_SWITCH,
    CONF_BATTERIES,
    CONF_CUSTOM_LOAD_POWER_NUMBER,
    CONF_MAX_CHARGE_POWER_NUMBER,
    CONF_POWER_SUPPLY_MODE_SELECT,
    CONF_POWERSTREAM_PAIRED,
    OPT_DEVICE_SETTINGS,
    OPT_MAX_CHARGE_POWER,
    OPT_MAX_GRID_FEED_IN_POWER,
    OPT_MIN_BATTERY_SOC,
    OPT_MIN_CHARGE_POWER,
)


def test_battery_pairing_form_exposes_a_charge_switch_per_battery():
    """Each battery's pairing step includes its own charge relay selector."""
    schema = _battery_pairing_schema()
    fields = {marker.schema for marker in schema.schema}

    assert CONF_BATTERY_CHARGE_SWITCH in fields


def test_reconfigure_preserves_retained_limits_and_defaults_new_batteries():
    """Adding a battery keeps existing limits and seeds new limits from legacy options."""
    entry_data = {
        "grid_power_sensor": "sensor.grid",
        CONF_BATTERY_SOC_SENSOR: ["sensor.battery_a", "sensor.battery_b"],
        CONF_BATTERIES: [{CONF_BATTERY_SOC_SENSOR: "sensor.battery_a"}],
    }
    entry_options = {
        OPT_DEVICE_SETTINGS: {
            "sensor.battery_a": {
                "min_soc": 25,
                "max_feed_in": 550,
                "max_charge": 900,
                "min_charge": 300,
                "capacity_wh": 2048,
            },
            "sensor.battery_b": {
                "min_soc": 15,
                "max_feed_in": 650,
                "max_charge": 1000,
                "min_charge": 200,
                "capacity_wh": 5120,
            },
        },
        OPT_MIN_BATTERY_SOC: 20,
        OPT_MAX_GRID_FEED_IN_POWER: 700,
        OPT_MAX_CHARGE_POWER: 1100,
        OPT_MIN_CHARGE_POWER: 250,
        "ev_charger_switch": "switch.ev",
    }

    data, options = _updated_battery_config(
        entry_data, entry_options, ["sensor.battery_a", "sensor.battery_c"]
    )

    assert data[CONF_BATTERY_SOC_SENSOR] == ["sensor.battery_a", "sensor.battery_c"]
    assert CONF_BATTERIES not in data
    assert data["grid_power_sensor"] == "sensor.grid"
    assert options[OPT_DEVICE_SETTINGS] == {
        "sensor.battery_a": {
            "min_soc": 25,
            "max_feed_in": 550,
            "max_charge": 900,
            "min_charge": 300,
            "capacity_wh": 2048,
        },
        "sensor.battery_c": {
            "min_soc": 20,
            "max_feed_in": 700,
            "max_charge": 1100,
            "min_charge": 250,
            "capacity_wh": 1000,
        },
    }
    assert options["ev_charger_switch"] == "switch.ev"


def test_battery_association_records_selected_powerstream_controls():
    """Each battery mapping stores the exact feed-in and mode entities selected for it."""
    association = _make_battery_association(
        "sensor.battery_a",
        {
            CONF_BATTERY_CHARGE_SWITCH: "switch.battery_a_charge",
            CONF_CUSTOM_LOAD_POWER_NUMBER: "number.powerstream_a_feed_in",
            CONF_POWER_SUPPLY_MODE_SELECT: "select.powerstream_a_mode",
        },
        "number.battery_a_charge_power",
    )

    assert association == {
        CONF_BATTERY_SOC_SENSOR: "sensor.battery_a",
        CONF_BATTERY_CHARGE_SWITCH: "switch.battery_a_charge",
        CONF_CUSTOM_LOAD_POWER_NUMBER: "number.powerstream_a_feed_in",
        CONF_POWER_SUPPLY_MODE_SELECT: "select.powerstream_a_mode",
        CONF_POWERSTREAM_PAIRED: True,
        CONF_MAX_CHARGE_POWER_NUMBER: "number.battery_a_charge_power",
    }


def test_battery_name_prefers_friendly_name_and_falls_back_to_entity_id():
    hass = MagicMock()
    named_state = MagicMock()
    named_state.attributes = {"friendly_name": "Delta Alpha"}
    hass.states.get.side_effect = lambda entity_id: (
        named_state if entity_id == "sensor.delta_alpha_soc" else None
    )

    assert _battery_name(hass, "sensor.delta_alpha_soc") == "Delta Alpha"
    assert _battery_name(hass, "sensor.missing_soc") == "sensor.missing_soc"
