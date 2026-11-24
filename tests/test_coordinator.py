"""Tests for the Energy Manager coordinator / FSM logic."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.ha_energy_manager.const import (
    CONF_BATTERY_SOC_SENSOR,
    CONF_BATTERY_CHARGE_SWITCH,
    CONF_BATTERIES,
    CONF_CHARGE_SWITCH,
    CONF_CUSTOM_LOAD_POWER_NUMBER,
    CONF_DISCHARGE_SWITCH,
    CONF_MAX_CHARGE_POWER_NUMBER,
    CONF_POWER_SUPPLY_MODE_SELECT,
    OPT_DEVICE_SETTINGS,
    OPT_DEADBAND,
    OPT_FEED_IN_DEADBAND,
    OPT_MAX_GRID_IMPORT_SOLAR_CHARGE,
    DEFAULT_EV_START_DELAY_CYCLES,
    DEFAULT_EV_STOP_DELAY_CYCLES,
    DEFAULT_EV_VOLTAGE,
    DEFAULT_LOG_BUFFER_SIZE,
    DEFAULT_PROPORTIONAL_DAMPING,
    MODE_AUTOMATIC,
    MODE_FORCED_CHARGE,
    MODE_HOLD,
    MODE_SOLAR,
    CONF_POWERSTREAM_PAIRED,
    OPT_EV_CHARGER_PHASES,
    PS_MODE_PRIORITIZE_STORAGE,
    PS_MODE_PRIORITIZE_SUPPLY,
    STATE_CHARGE,
    STATE_DISCHARGE,
    STATE_HOLD,
)
from tests.conftest import EV_OPTIONS, make_state


# ── Battery SOC stale-value protection ────────────────────────────────


class TestBatterySOCProtection:
    """Test that integration restarts reporting 0% / unavailable don't break the FSM."""

    def test_unavailable_uses_last_known_value(self, coordinator, mock_hass):
        """When sensor is unavailable, return last known good SOC."""
        coordinator._last_valid_battery_soc = 65.0
        mock_hass._sensor_states["sensor.battery_soc"] = make_state("unavailable")
        assert coordinator._read_battery_soc() == 65.0

    def test_unknown_uses_last_known_value(self, coordinator, mock_hass):
        """When sensor is unknown, return last known good SOC."""
        coordinator._last_valid_battery_soc = 42.0
        mock_hass._sensor_states["sensor.battery_soc"] = make_state("unknown")
        assert coordinator._read_battery_soc() == 42.0

    def test_missing_entity_uses_last_known_value(self, coordinator, mock_hass):
        """When sensor entity doesn't exist, return last known good SOC."""
        coordinator._last_valid_battery_soc = 80.0
        mock_hass._sensor_states.pop("sensor.battery_soc", None)
        assert coordinator._read_battery_soc() == 80.0

    def test_zero_drop_from_high_soc_is_rejected(self, coordinator, mock_hass):
        """A sudden drop from >5% to 0% is treated as a sensor glitch."""
        coordinator._last_valid_battery_soc = 50.0
        mock_hass._sensor_states["sensor.battery_soc"] = make_state(0)
        assert coordinator._read_battery_soc() == 50.0

    def test_zero_accepted_when_soc_already_low(self, coordinator, mock_hass):
        """0% is accepted when the previous reading was already near 0%."""
        coordinator._last_valid_battery_soc = 3.0
        mock_hass._sensor_states["sensor.battery_soc"] = make_state(0)
        soc = coordinator._read_battery_soc()
        assert soc == 0.0

    def test_zero_accepted_on_first_boot(self, coordinator, mock_hass):
        """0% is accepted when there is no previous reading (first boot)."""
        assert coordinator._last_valid_battery_soc is None
        mock_hass._sensor_states["sensor.battery_soc"] = make_state(0)
        assert coordinator._read_battery_soc() == 0.0

    def test_unavailable_with_no_previous_returns_zero(self, coordinator, mock_hass):
        """Unavailable with no previous reading returns 0.0."""
        assert coordinator._last_valid_battery_soc is None
        mock_hass._sensor_states["sensor.battery_soc"] = make_state("unavailable")
        assert coordinator._read_battery_soc() == 0.0

    def test_multi_battery_soc_uses_highest_available(self, coordinator, mock_hass):
        """The shared FSM can discharge when at least one battery is usable."""
        coordinator._batteries = [
            {CONF_BATTERY_SOC_SENSOR: "sensor.battery_a"},
            {CONF_BATTERY_SOC_SENSOR: "sensor.battery_b"},
        ]
        mock_hass._sensor_states.update(
            {"sensor.battery_a": make_state(15), "sensor.battery_b": make_state(70)}
        )

        assert coordinator._read_battery_soc() == 70

    def test_valid_reading_updates_last_known(self, coordinator, mock_hass):
        """A valid SOC reading updates the cached value."""
        mock_hass._sensor_states["sensor.battery_soc"] = make_state(72)
        assert coordinator._read_battery_soc() == 72.0
        assert coordinator._last_valid_battery_soc == 72.0

    def test_normal_soc_decline_accepted(self, coordinator, mock_hass):
        """Normal gradual SOC decline (e.g. 50 → 48) is accepted."""
        coordinator._last_valid_battery_soc = 50.0
        mock_hass._sensor_states["sensor.battery_soc"] = make_state(48)
        assert coordinator._read_battery_soc() == 48.0
        assert coordinator._last_valid_battery_soc == 48.0

    @pytest.mark.asyncio
    async def test_discharge_continues_during_restart(self, coordinator, mock_hass):
        """Discharge state is maintained when SOC sensor goes unavailable mid-discharge."""
        coordinator._active_mode = MODE_AUTOMATIC
        coordinator._fsm_state = STATE_DISCHARGE
        coordinator._fsm_state_entered_at = time.monotonic() - 120
        coordinator._last_valid_battery_soc = 50.0
        coordinator._current_feed_in_power = 400
        coordinator._last_feed_in_power = 400

        # Simulate EcoFlow integration restart: sensor becomes unavailable
        mock_hass._sensor_states["sensor.battery_soc"] = make_state("unavailable")

        data = await coordinator._async_update_data()
        # Should remain in DISCHARGE, not drop to HOLD
        assert coordinator._fsm_state == STATE_DISCHARGE
        assert data.battery_soc == 50.0

    @pytest.mark.asyncio
    async def test_control_trace_records_grid_filter_and_device_charge_targets(
        self, coordinator, mock_hass, mock_config_entry
    ):
        coordinator._is_enabled = False
        coordinator._batteries = [
            {
                CONF_BATTERY_SOC_SENSOR: "sensor.battery_a",
                CONF_BATTERY_CHARGE_SWITCH: "switch.charge_a",
                CONF_MAX_CHARGE_POWER_NUMBER: "number.charge_a",
            },
            {
                CONF_BATTERY_SOC_SENSOR: "sensor.battery_b",
                CONF_BATTERY_CHARGE_SWITCH: "switch.charge_b",
                CONF_MAX_CHARGE_POWER_NUMBER: "number.charge_b",
            },
        ]
        coordinator._last_device_charge = {
            "sensor.battery_a": 250,
            "sensor.battery_b": 0,
        }
        mock_config_entry.options[OPT_DEVICE_SETTINGS] = {
            "sensor.battery_a": {"min_charge": 200},
            "sensor.battery_b": {"min_charge": 200},
        }
        coordinator._config_entry = mock_config_entry
        mock_hass._sensor_states.update(
            {
                "sensor.battery_a": make_state(45),
                "sensor.battery_b": make_state(61),
                "number.charge_a": make_state(250),
                "number.charge_b": make_state(0),
            }
        )

        await coordinator._async_update_data()

        trace = coordinator.control_trace[-1]
        assert trace["grid_raw_w"] == 100
        assert trace["grid_control_w"] == 100
        assert trace["batteries"]["sensor.battery_a"] == {
            "soc": 45.0,
            "charge_target": 250,
            "charge_control_state": "250",
        }
        assert trace["batteries"]["sensor.battery_b"]["charge_target"] == 0

    @pytest.mark.asyncio
    async def test_discharge_control_uses_live_grid_sample(
        self, coordinator, mock_hass
    ):
        """Feed-in control should not react to a stale median after a grid change."""
        coordinator._active_mode = MODE_AUTOMATIC
        coordinator._fsm_state = STATE_DISCHARGE
        coordinator._grid_power_history.extend([-500, -500])
        mock_hass._sensor_states["sensor.grid_power"] = make_state(400)
        run_automatic = AsyncMock()
        coordinator._run_automatic = run_automatic

        await coordinator._async_update_data()

        assert run_automatic.await_args.args[0] == 400
        assert coordinator.control_trace[-1]["grid_filtered_w"] == -500


# ── Snap to step ─────────────────────────────────────────────────────


class TestSnapToStep:
    """Test the _snap_to_step helper."""

    def test_snap_100_step(self, coordinator):
        assert coordinator._snap_to_step(150) == 200
        assert coordinator._snap_to_step(149) == 100
        assert coordinator._snap_to_step(250) == 200
        assert coordinator._snap_to_step(251) == 300
        assert coordinator._snap_to_step(0) == 0

    def test_snap_50_step(self, coordinator):
        assert coordinator._snap_to_step(30, step=50) == 50
        assert coordinator._snap_to_step(24, step=50) == 0
        assert coordinator._snap_to_step(75, step=50) == 100
        assert coordinator._snap_to_step(276, step=50) == 300

    def test_grid_control_filter_rejects_an_isolated_spike(self, coordinator):
        assert coordinator._filter_grid_power(100) == 100
        assert coordinator._filter_grid_power(1200) == 1200
        assert coordinator._filter_grid_power(100) == 100
        assert coordinator._filter_grid_power(100) == 100


# ── Proportional adjustment ─────────────────────────────────────────


class TestProportionalAdjustment:
    """Test the _calc_proportional_adjustment helper."""

    def test_within_deadband_returns_zero(self, coordinator):
        """No adjustment when grid is within deadband."""
        assert coordinator._calc_proportional_adjustment(30, 50, 100) == 0
        assert coordinator._calc_proportional_adjustment(-30, 50, 100) == 0
        assert coordinator._calc_proportional_adjustment(0, 50, 100) == 0

    def test_small_error_uses_minimum_step(self, coordinator):
        """Small error (just outside deadband) uses step as minimum."""
        # grid=70, deadband=50, step=100 → error=20, raw=max(100,20*0.8)=100
        assert coordinator._calc_proportional_adjustment(70, 50, 100) == -100
        # Negative direction
        assert coordinator._calc_proportional_adjustment(-70, 50, 100) == 100

    def test_large_error_scales_proportionally(self, coordinator):
        """Large error produces proportional adjustment."""
        # grid=700, deadband=50, step=100 → error=650, raw=max(100,650*0.8)=520→snap 500
        assert coordinator._calc_proportional_adjustment(700, 50, 100) == -500
        # Negative direction
        assert coordinator._calc_proportional_adjustment(-700, 50, 100) == 500

    def test_large_error_with_50w_step(self, coordinator):
        """Large error with 50W step size (feed-in)."""
        # grid=500, deadband=50, step=50 → error=450, raw=max(50,450*0.8)=360→snap 350
        assert coordinator._calc_proportional_adjustment(500, 50, 50) == -350

    def test_sign_convention(self, coordinator):
        """Positive grid (import) → negative adj (reduce charge)."""
        adj = coordinator._calc_proportional_adjustment(200, 50, 100)
        assert adj < 0
        # Negative grid (export) → positive adj (increase charge)
        adj = coordinator._calc_proportional_adjustment(-200, 50, 100)
        assert adj > 0

    def test_exact_deadband_returns_zero(self, coordinator):
        """Grid exactly at deadband boundary returns zero."""
        assert coordinator._calc_proportional_adjustment(50, 50, 100) == 0
        assert coordinator._calc_proportional_adjustment(-50, 50, 100) == 0


# ── Charge power setter ─────────────────────────────────────────────


class TestSetChargePower:
    """Test _async_set_charge_power."""

    @pytest.mark.asyncio
    async def test_set_charge_power_positive(self, coordinator, mock_hass):
        """Setting positive charge power turns on switch, sets PS mode to storage, and sends number.set_value."""
        await coordinator._async_set_charge_power(400, reason="test")
        mock_hass.services.async_call.assert_any_call(
            "switch", "turn_on", {"entity_id": "switch.charge"},
        )
        mock_hass.services.async_call.assert_any_call(
            "select",
            "select_option",
            {"entity_id": "select.ps_mode", "option": PS_MODE_PRIORITIZE_STORAGE},
        )
        mock_hass.services.async_call.assert_any_call(
            "number", "set_value", {"entity_id": "number.charge_power", "value": 400}
        )
        assert coordinator._last_charge_power == 400
        assert coordinator._current_charge_power == 400

    @pytest.mark.asyncio
    async def test_charge_allocation_uses_each_batterys_charge_switch(
        self, coordinator, mock_hass, mock_config_entry
    ):
        """Only the battery assigned a positive target has its switch enabled."""
        coordinator._batteries = [
            {
                CONF_BATTERY_SOC_SENSOR: "sensor.battery_low",
                CONF_BATTERY_CHARGE_SWITCH: "switch.charge_low",
                CONF_MAX_CHARGE_POWER_NUMBER: "number.charge_low",
            },
            {
                CONF_BATTERY_SOC_SENSOR: "sensor.battery_high",
                CONF_BATTERY_CHARGE_SWITCH: "switch.charge_high",
                CONF_MAX_CHARGE_POWER_NUMBER: "number.charge_high",
            },
        ]
        coordinator._config_entry = mock_config_entry
        mock_hass._sensor_states.update(
            {
                "sensor.battery_low": make_state(20),
                "sensor.battery_high": make_state(80),
                "switch.charge_low": make_state("off"),
                "switch.charge_high": make_state("off"),
            }
        )

        await coordinator._async_set_charge_power(300)

        mock_hass.services.async_call.assert_any_call(
            "switch", "turn_on", {"entity_id": "switch.charge_low"}
        )
        mock_hass.services.async_call.assert_any_call(
            "switch", "turn_off", {"entity_id": "switch.charge_high"}
        )
        mock_hass.services.async_call.assert_any_call(
            "number", "set_value", {"entity_id": "number.charge_low", "value": 300}
        )
        mock_hass.services.async_call.assert_any_call(
            "number", "set_value", {"entity_id": "number.charge_high", "value": 0}
        )

    @pytest.mark.asyncio
    async def test_starting_charge_stops_feed_in_first(self, coordinator, mock_hass):
        """The discharge relay is opened before the charge relay is enabled."""
        coordinator._last_feed_in_power = 300
        coordinator._current_feed_in_power = 300
        coordinator._last_discharge_switch = False  # stale cache; relay is externally on
        mock_hass._sensor_states["switch.discharge"] = make_state("on")

        await coordinator._async_set_charge_power(400)

        calls = mock_hass.services.async_call.call_args_list
        discharge_off = next(
            i for i, call in enumerate(calls)
            if call.args[:2] == ("switch", "turn_off")
            and call.args[2]["entity_id"] == "switch.discharge"
        )
        charge_on = next(
            i for i, call in enumerate(calls)
            if call.args[:2] == ("switch", "turn_on")
            and call.args[2]["entity_id"] == "switch.charge"
        )
        assert discharge_off < charge_on
        feed_in_zero = next(
            i for i, call in enumerate(calls)
            if call.args[:2] == ("number", "set_value")
            and call.args[2] == {"entity_id": "number.custom_load", "value": 0}
        )
        assert feed_in_zero < charge_on

    @pytest.mark.asyncio
    async def test_set_charge_power_zero_stops_charging(self, coordinator, mock_hass):
        """Setting charge power to 0 turns off charge switch."""
        await coordinator._async_set_charge_power(0, reason="test")
        mock_hass.services.async_call.assert_any_call(
            "switch", "turn_off", {"entity_id": "switch.charge"},
        )
        assert coordinator._last_charge_power == 0
        assert coordinator._current_charge_power == 0

    @pytest.mark.asyncio
    async def test_set_charge_power_negative_stops_charging(self, coordinator, mock_hass):
        """Negative value also turns off charge switch."""
        await coordinator._async_set_charge_power(-100, reason="test")
        mock_hass.services.async_call.assert_any_call(
            "switch", "turn_off", {"entity_id": "switch.charge"},
        )
        assert coordinator._last_charge_power == 0
        assert coordinator._current_charge_power == 0

    @pytest.mark.asyncio
    async def test_set_charge_power_snaps_to_step(self, coordinator, mock_hass):
        """Value is snapped to 100W step."""
        await coordinator._async_set_charge_power(350, reason="test")
        # 350 snaps to 400 (min 200, max 1200)
        mock_hass.services.async_call.assert_any_call(
            "number", "set_value", {"entity_id": "number.charge_power", "value": 400}
        )

    @pytest.mark.asyncio
    async def test_set_charge_power_clamps_to_min(self, coordinator, mock_hass):
        """Value below min_power is clamped to min_power."""
        await coordinator._async_set_charge_power(50, reason="test")
        # 50 is below min 200, clamped to 200
        mock_hass.services.async_call.assert_any_call(
            "number", "set_value", {"entity_id": "number.charge_power", "value": 200}
        )

    @pytest.mark.asyncio
    async def test_set_charge_power_clamps_to_max(self, coordinator, mock_hass):
        """Value above max_power is clamped to max_power."""
        await coordinator._async_set_charge_power(5000, reason="test")
        mock_hass.services.async_call.assert_any_call(
            "number", "set_value", {"entity_id": "number.charge_power", "value": 1200}
        )

    @pytest.mark.asyncio
    async def test_set_charge_power_skips_redundant(self, coordinator, mock_hass):
        """Skips service call if value unchanged."""
        await coordinator._async_set_charge_power(400, reason="first")
        mock_hass.services.async_call.reset_mock()

        await coordinator._async_set_charge_power(400, reason="second")
        # Only charge switch call, no number.set_value
        # Actually for positive values, charge switch is not called from _async_set_charge_power
        # It's only turned off when value <= 0
        # The skip happens because _last_charge_power == snapped
        assert not any(
            call[0] == ("number", "set_value")
            for call in mock_hass.services.async_call.call_args_list
        )

    @pytest.mark.asyncio
    async def test_multi_battery_charge_splits_and_applies_device_caps(
        self, coordinator, mock_hass, mock_config_entry
    ):
        """Charge target is split, then capped by each battery's own maximum."""
        coordinator._batteries = [
            {
                CONF_BATTERY_SOC_SENSOR: "sensor.battery_a",
                CONF_BATTERY_CHARGE_SWITCH: "switch.charge_a",
                CONF_MAX_CHARGE_POWER_NUMBER: "number.charge_a",
                CONF_POWER_SUPPLY_MODE_SELECT: "select.mode_a",
            },
            {
                CONF_BATTERY_SOC_SENSOR: "sensor.battery_b",
                CONF_BATTERY_CHARGE_SWITCH: "switch.charge_b",
                CONF_MAX_CHARGE_POWER_NUMBER: "number.charge_b",
                CONF_POWER_SUPPLY_MODE_SELECT: "select.mode_b",
            },
        ]
        mock_config_entry.options[OPT_DEVICE_SETTINGS] = {
            "sensor.battery_a": {"max_charge": 400, "min_charge": 100},
            "sensor.battery_b": {"max_charge": 800, "min_charge": 100},
        }
        coordinator._config_entry = mock_config_entry

        await coordinator._async_set_charge_power(1000)

        calls = [
            call.args[2]
            for call in mock_hass.services.async_call.call_args_list
            if call.args[0:2] == ("number", "set_value")
        ]
        assert calls == [
            {"entity_id": "number.charge_a", "value": 400},
            {"entity_id": "number.charge_b", "value": 600},
        ]
        assert coordinator._current_charge_power == 1000

    @pytest.mark.asyncio
    async def test_multi_battery_charge_skips_full_battery_and_uses_remaining_capacity(
        self, coordinator, mock_hass, mock_config_entry
    ):
        """A full battery receives no charge and its available share goes to others."""
        coordinator._batteries = [
            {
                CONF_BATTERY_SOC_SENSOR: "sensor.full",
                CONF_MAX_CHARGE_POWER_NUMBER: "number.charge_full",
            },
            {
                CONF_BATTERY_SOC_SENSOR: "sensor.available",
                CONF_MAX_CHARGE_POWER_NUMBER: "number.charge_available",
            },
        ]
        mock_config_entry.options[OPT_DEVICE_SETTINGS] = {
            "sensor.full": {"max_charge": 1200, "min_charge": 200},
            "sensor.available": {"max_charge": 800, "min_charge": 200},
        }
        coordinator._config_entry = mock_config_entry
        mock_hass._sensor_states["sensor.full"] = make_state(100)
        mock_hass._sensor_states["sensor.available"] = make_state(40)

        await coordinator._async_set_charge_power(500)

        calls = [
            call.args[2]
            for call in mock_hass.services.async_call.call_args_list
            if call.args[0:2] == ("number", "set_value")
        ]
        assert calls == [
            {"entity_id": "number.charge_full", "value": 0},
            {"entity_id": "number.charge_available", "value": 500},
        ]


# ── Feed-in power setter ────────────────────────────────────────────


class TestSetFeedInPower:
    """Test _async_set_feed_in_power."""

    @pytest.mark.asyncio
    async def test_set_feed_in_positive_sets_supply_mode(self, coordinator, mock_hass):
        """Positive feed-in turns on discharge switch, sets PS mode to supply, and sends custom load power."""
        await coordinator._async_set_feed_in_power(300, reason="test")
        mock_hass.services.async_call.assert_any_call(
            "switch", "turn_on", {"entity_id": "switch.discharge"},
        )
        mock_hass.services.async_call.assert_any_call(
            "select",
            "select_option",
            {"entity_id": "select.ps_mode", "option": PS_MODE_PRIORITIZE_SUPPLY},
        )
        mock_hass.services.async_call.assert_any_call(
            "number", "set_value", {"entity_id": "number.custom_load", "value": 300}
        )

    @pytest.mark.asyncio
    async def test_starting_feed_in_stops_charge_first(self, coordinator, mock_hass):
        """The charge relay is opened before the discharge relay is enabled."""
        coordinator._last_charge_power = 300
        coordinator._current_charge_power = 300
        coordinator._last_charge_switch = False  # stale cache; relay is externally on
        mock_hass._sensor_states["switch.charge"] = make_state("on")

        await coordinator._async_set_feed_in_power(300)

        calls = mock_hass.services.async_call.call_args_list
        charge_off = next(
            i for i, call in enumerate(calls)
            if call.args[:2] == ("switch", "turn_off")
            and call.args[2]["entity_id"] == "switch.charge"
        )
        charge_zero = next(
            i for i, call in enumerate(calls)
            if call.args[:2] == ("number", "set_value")
            and call.args[2] == {"entity_id": "number.charge_power", "value": 0}
        )
        discharge_on = next(
            i for i, call in enumerate(calls)
            if call.args[:2] == ("switch", "turn_on")
            and call.args[2]["entity_id"] == "switch.discharge"
        )
        assert charge_off < discharge_on
        assert charge_zero < discharge_on

    @pytest.mark.asyncio
    async def test_starting_feed_in_clears_stale_per_battery_charge_targets(
        self, coordinator, mock_hass, mock_config_entry
    ):
        """Restarted charge setpoints are zeroed before either PowerStream feeds in."""
        coordinator._batteries = [
            {
                CONF_BATTERY_SOC_SENSOR: "sensor.battery_a",
                CONF_BATTERY_CHARGE_SWITCH: "switch.charge_a",
                CONF_MAX_CHARGE_POWER_NUMBER: "number.charge_a",
                CONF_CUSTOM_LOAD_POWER_NUMBER: "number.load_a",
                CONF_POWER_SUPPLY_MODE_SELECT: "select.mode_a",
                CONF_POWERSTREAM_PAIRED: True,
            },
            {
                CONF_BATTERY_SOC_SENSOR: "sensor.battery_b",
                CONF_BATTERY_CHARGE_SWITCH: "switch.charge_b",
                CONF_MAX_CHARGE_POWER_NUMBER: "number.charge_b",
                CONF_CUSTOM_LOAD_POWER_NUMBER: "number.load_b",
                CONF_POWER_SUPPLY_MODE_SELECT: "select.mode_b",
                CONF_POWERSTREAM_PAIRED: True,
            },
        ]
        coordinator._config_entry = mock_config_entry
        coordinator._last_charge_switch = True
        mock_hass._sensor_states.update(
            {
                "sensor.battery_a": make_state(80),
                "sensor.battery_b": make_state(80),
                "number.charge_a": make_state(200),
                "number.charge_b": make_state(200),
                "switch.charge_a": make_state("on"),
                "switch.charge_b": make_state("on"),
            }
        )

        await coordinator._async_set_feed_in_power(300)

        calls = mock_hass.services.async_call.call_args_list
        discharge_on = next(
            i for i, call in enumerate(calls)
            if call.args[:2] == ("switch", "turn_on")
            and call.args[2]["entity_id"] == "switch.discharge"
        )
        for entity_id in ("number.charge_a", "number.charge_b"):
            cleared = next(
                i for i, call in enumerate(calls)
                if call.args[:2] == ("number", "set_value")
                and call.args[2] == {"entity_id": entity_id, "value": 0}
            )
            assert cleared < discharge_on
        for entity_id in ("switch.charge_a", "switch.charge_b"):
            switch_off = next(
                i for i, call in enumerate(calls)
                if call.args[:2] == ("switch", "turn_off")
                and call.args[2]["entity_id"] == entity_id
            )
            assert switch_off < discharge_on

    @pytest.mark.asyncio
    async def test_feed_in_fails_closed_if_charge_relay_cannot_be_stopped(
        self, coordinator, mock_hass
    ):
        """Never enable feed-in when turning off charging fails."""
        coordinator._last_charge_switch = None

        async def fail_charge_off(domain, service, data):
            if domain == "switch" and service == "turn_off" and data["entity_id"] == "switch.charge":
                raise RuntimeError("relay unavailable")

        mock_hass.services.async_call.side_effect = fail_charge_off
        success = await coordinator._async_set_feed_in_power(300)

        assert success is False
        assert not any(
            call.args[:2] == ("switch", "turn_on")
            and call.args[2]["entity_id"] == "switch.discharge"
            for call in mock_hass.services.async_call.call_args_list
        )

    @pytest.mark.asyncio
    async def test_feed_in_fails_closed_if_battery_charge_target_cannot_be_cleared(
        self, coordinator, mock_hass, mock_config_entry
    ):
        """Never start feed-in if a battery's AC charge target remains nonzero."""
        coordinator._batteries = [
            {
                CONF_BATTERY_SOC_SENSOR: "sensor.battery_a",
                CONF_MAX_CHARGE_POWER_NUMBER: "number.charge_a",
                CONF_CUSTOM_LOAD_POWER_NUMBER: "number.load_a",
                CONF_POWERSTREAM_PAIRED: True,
            },
        ]
        coordinator._config_entry = mock_config_entry
        mock_hass._sensor_states.update(
            {"sensor.battery_a": make_state(80), "number.charge_a": make_state(200)}
        )

        async def fail_charge_target(domain, service, data):
            if domain == "number" and service == "set_value" and data["entity_id"] == "number.charge_a":
                raise RuntimeError("charge control unavailable")

        mock_hass.services.async_call.side_effect = fail_charge_target
        success = await coordinator._async_set_feed_in_power(300)

        assert success is False
        assert not any(
            call.args[:2] == ("switch", "turn_on")
            and call.args[2]["entity_id"] == "switch.discharge"
            for call in mock_hass.services.async_call.call_args_list
        )

    @pytest.mark.asyncio
    async def test_set_feed_in_zero_turns_off_switch(self, coordinator, mock_hass):
        """Zero feed-in turns off discharge switch."""
        # First set a positive value
        await coordinator._async_set_feed_in_power(300, reason="initial")
        mock_hass.services.async_call.reset_mock()

        await coordinator._async_set_feed_in_power(0, reason="stop")
        mock_hass.services.async_call.assert_any_call(
            "switch", "turn_off", {"entity_id": "switch.discharge"},
        )
        assert coordinator._last_feed_in_power == 0

    @pytest.mark.asyncio
    async def test_set_feed_in_snaps_to_10_step(self, coordinator, mock_hass):
        """Feed-in is snapped to 10W steps."""
        await coordinator._async_set_feed_in_power(276, reason="test")
        mock_hass.services.async_call.assert_any_call(
            "number", "set_value", {"entity_id": "number.custom_load", "value": 280}
        )

    @pytest.mark.asyncio
    async def test_set_feed_in_clamps_to_max(self, coordinator, mock_hass):
        """Feed-in clamped to max_grid_feed_in_power."""
        await coordinator._async_set_feed_in_power(2000, reason="test")
        # Default max is 800
        mock_hass.services.async_call.assert_any_call(
            "number", "set_value", {"entity_id": "number.custom_load", "value": 800}
        )

    @pytest.mark.asyncio
    async def test_multi_battery_soc_floor_and_feed_in_cap(
        self, coordinator, mock_hass, mock_config_entry
    ):
        """Low-SOC batteries are set to zero while eligible batteries obey their cap."""
        coordinator._batteries = [
            {
                CONF_BATTERY_SOC_SENSOR: "sensor.battery_a",
                CONF_CUSTOM_LOAD_POWER_NUMBER: "number.load_a",
                CONF_POWER_SUPPLY_MODE_SELECT: "select.mode_a",
                CONF_POWERSTREAM_PAIRED: True,
            },
            {
                CONF_BATTERY_SOC_SENSOR: "sensor.battery_b",
                CONF_CUSTOM_LOAD_POWER_NUMBER: "number.load_b",
                CONF_POWER_SUPPLY_MODE_SELECT: "select.mode_b",
                CONF_POWERSTREAM_PAIRED: True,
            },
        ]
        mock_config_entry.options[OPT_DEVICE_SETTINGS] = {
            "sensor.battery_a": {"min_soc": 20, "max_feed_in": 600},
            "sensor.battery_b": {"min_soc": 10, "max_feed_in": 400},
        }
        coordinator._config_entry = mock_config_entry
        mock_hass._sensor_states.update(
            {
                "sensor.battery_a": make_state(15),
                "sensor.battery_b": make_state(80),
                "number.load_a": make_state(100),
            }
        )

        await coordinator._async_set_feed_in_power(700)

        mock_hass.services.async_call.assert_any_call(
            "number", "set_value", {"entity_id": "number.load_a", "value": 0}
        )
        mock_hass.services.async_call.assert_any_call(
            "number", "set_value", {"entity_id": "number.load_b", "value": 400}
        )
        assert coordinator._current_feed_in_power == 400

    @pytest.mark.parametrize(
        ("target", "expected"),
        [
            (600, {"number.load_a": 190, "number.load_b": 410}),
            (1000, {"number.load_a": 320, "number.load_b": 680}),
        ],
    )
    @pytest.mark.asyncio
    async def test_multi_powerstream_feed_in_balances_usable_energy(
        self, coordinator, mock_hass, mock_config_entry, target, expected
    ):
        """Split feed-in according to capacity-weighted SOC above reserve."""
        coordinator._batteries = [
            {
                CONF_BATTERY_SOC_SENSOR: "sensor.battery_low",
                CONF_CUSTOM_LOAD_POWER_NUMBER: "number.load_a",
                CONF_POWER_SUPPLY_MODE_SELECT: "select.mode_a",
                CONF_POWERSTREAM_PAIRED: True,
            },
            {
                CONF_BATTERY_SOC_SENSOR: "sensor.battery_high",
                CONF_CUSTOM_LOAD_POWER_NUMBER: "number.load_b",
                CONF_POWER_SUPPLY_MODE_SELECT: "select.mode_b",
                CONF_POWERSTREAM_PAIRED: True,
            },
        ]
        mock_config_entry.options[OPT_DEVICE_SETTINGS] = {
            "sensor.battery_low": {"min_soc": 10, "max_feed_in": 800},
            "sensor.battery_high": {"min_soc": 10, "max_feed_in": 800},
        }
        coordinator._config_entry = mock_config_entry
        mock_hass._sensor_states.update(
            {
                "sensor.battery_low": make_state(47),
                "sensor.battery_high": make_state(89),
            }
        )

        await coordinator._async_set_feed_in_power(target)

        calls = {
            call.args[2]["entity_id"]: call.args[2]["value"]
            for call in mock_hass.services.async_call.call_args_list
            if call.args[0:2] == ("number", "set_value")
        }
        assert calls == expected
        assert coordinator._current_feed_in_power == target

    @pytest.mark.asyncio
    async def test_multi_powerstream_feed_in_accounts_for_different_capacities(
        self, coordinator, mock_hass, mock_config_entry
    ):
        """A larger battery with more usable Wh carries more of the discharge."""
        coordinator._batteries = [
            {
                CONF_BATTERY_SOC_SENSOR: "sensor.battery_small",
                CONF_CUSTOM_LOAD_POWER_NUMBER: "number.load_small",
                CONF_POWERSTREAM_PAIRED: True,
            },
            {
                CONF_BATTERY_SOC_SENSOR: "sensor.battery_large",
                CONF_CUSTOM_LOAD_POWER_NUMBER: "number.load_large",
                CONF_POWERSTREAM_PAIRED: True,
            },
        ]
        mock_config_entry.options[OPT_DEVICE_SETTINGS] = {
            "sensor.battery_small": {"min_soc": 10, "max_feed_in": 800, "capacity_wh": 1000},
            "sensor.battery_large": {"min_soc": 10, "max_feed_in": 800, "capacity_wh": 5000},
        }
        coordinator._config_entry = mock_config_entry
        mock_hass._sensor_states.update(
            {"sensor.battery_small": make_state(80), "sensor.battery_large": make_state(80)}
        )

        await coordinator._async_set_feed_in_power(600)

        calls = {
            call.args[2]["entity_id"]: call.args[2]["value"]
            for call in mock_hass.services.async_call.call_args_list
            if call.args[0:2] == ("number", "set_value")
        }
        assert calls == {"number.load_small": 100, "number.load_large": 500}

    @pytest.mark.asyncio
    async def test_shared_feed_control_is_written_once_and_respects_group_soc_floor(
        self, coordinator, mock_hass, mock_config_entry
    ):
        """A shared PowerStream is controlled once and stopped if any linked battery is low."""
        coordinator._batteries = [
            {
                CONF_BATTERY_SOC_SENSOR: "sensor.battery_a",
                CONF_CUSTOM_LOAD_POWER_NUMBER: "number.shared_load",
                CONF_POWER_SUPPLY_MODE_SELECT: "select.shared_mode",
                CONF_POWERSTREAM_PAIRED: True,
            },
            {
                CONF_BATTERY_SOC_SENSOR: "sensor.battery_b",
                CONF_CUSTOM_LOAD_POWER_NUMBER: "number.shared_load",
                CONF_POWER_SUPPLY_MODE_SELECT: "select.shared_mode",
                CONF_POWERSTREAM_PAIRED: True,
            },
        ]
        mock_config_entry.options[OPT_DEVICE_SETTINGS] = {
            "sensor.battery_a": {"min_soc": 20, "max_feed_in": 300},
            "sensor.battery_b": {"min_soc": 10, "max_feed_in": 400},
        }
        coordinator._config_entry = mock_config_entry
        mock_hass._sensor_states.update(
            {
                "sensor.battery_a": make_state(80),
                "sensor.battery_b": make_state(80),
            }
        )

        await coordinator._async_set_feed_in_power(700)
        mock_hass.services.async_call.assert_any_call(
            "number", "set_value", {"entity_id": "number.shared_load", "value": 700}
        )
        assert sum(
            call.args[2].get("entity_id") == "number.shared_load"
            for call in mock_hass.services.async_call.call_args_list
            if call.args[0:2] == ("number", "set_value")
        ) == 1

        mock_hass.services.async_call.reset_mock()
        mock_hass._sensor_states["sensor.battery_a"] = make_state(15)
        mock_hass._sensor_states["number.shared_load"] = make_state(700)
        await coordinator._async_set_feed_in_power(700)

        mock_hass.services.async_call.assert_any_call(
            "number", "set_value", {"entity_id": "number.shared_load", "value": 0}
        )
        mock_hass.services.async_call.assert_any_call(
            "switch", "turn_off", {"entity_id": "switch.discharge"}
        )

    @pytest.mark.asyncio
    async def test_multi_battery_feed_in_requires_explicit_pairing(
        self, coordinator, mock_hass
    ):
        """Legacy guessed associations cannot drive a multi-battery feed control."""
        coordinator._batteries = [
            {
                CONF_BATTERY_SOC_SENSOR: "sensor.battery_a",
                CONF_CUSTOM_LOAD_POWER_NUMBER: "number.shared_load",
                CONF_POWER_SUPPLY_MODE_SELECT: "select.shared_mode",
            },
            {
                CONF_BATTERY_SOC_SENSOR: "sensor.battery_b",
                CONF_CUSTOM_LOAD_POWER_NUMBER: "number.shared_load",
                CONF_POWER_SUPPLY_MODE_SELECT: "select.shared_mode",
            },
        ]

        await coordinator._async_set_feed_in_power(500)

        mock_hass.services.async_call.assert_any_call(
            "switch", "turn_off", {"entity_id": "switch.discharge"}
        )
        assert not any(
            call.args[0:2] == ("number", "set_value")
            and call.args[2].get("value", 0) > 0
            for call in mock_hass.services.async_call.call_args_list
        )


# ── Power supply mode ────────────────────────────────────────────────


class TestPowerSupplyMode:
    """Test PS mode setter."""

    @pytest.mark.asyncio
    async def test_ps_mode_skips_redundant(self, coordinator, mock_hass):
        """PS mode setter skips if both tracked and actual state match."""
        await coordinator._async_set_power_supply_mode(PS_MODE_PRIORITIZE_SUPPLY)
        # Simulate that HA entity now reflects the new mode
        mock_hass._sensor_states["select.ps_mode"] = make_state(PS_MODE_PRIORITIZE_SUPPLY)
        mock_hass.services.async_call.reset_mock()
        await coordinator._async_set_power_supply_mode(PS_MODE_PRIORITIZE_SUPPLY)
        mock_hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_ps_mode_reapplied_if_externally_changed(self, coordinator, mock_hass):
        """PS mode is re-sent if entity state was changed externally (e.g. EcoFlow app)."""
        await coordinator._async_set_power_supply_mode(PS_MODE_PRIORITIZE_SUPPLY)
        # Simulate external change: EcoFlow switched back to storage
        mock_hass._sensor_states["select.ps_mode"] = make_state(PS_MODE_PRIORITIZE_STORAGE)
        mock_hass.services.async_call.reset_mock()
        # Even though _last_ps_mode is SUPPLY, entity state differs → re-send
        await coordinator._async_set_power_supply_mode(PS_MODE_PRIORITIZE_SUPPLY)
        mock_hass.services.async_call.assert_any_call(
            "select",
            "select_option",
            {"entity_id": "select.ps_mode", "option": PS_MODE_PRIORITIZE_SUPPLY},
        )


# ── Switch setters ──────────────────────────────────────────────────


class TestSwitchSetters:
    """Test Shelly switch setters."""

    @pytest.mark.asyncio
    async def test_charge_switch_on(self, coordinator, mock_hass):
        """Charge switch turns on."""
        await coordinator._async_set_charge_switch(True)
        mock_hass.services.async_call.assert_any_call(
            "switch", "turn_on", {"entity_id": "switch.charge"},
        )
        assert coordinator._last_charge_switch is True

    @pytest.mark.asyncio
    async def test_charge_switch_off(self, coordinator, mock_hass):
        """Charge switch turns off."""
        await coordinator._async_set_charge_switch(False)
        mock_hass.services.async_call.assert_any_call(
            "switch", "turn_off", {"entity_id": "switch.charge"},
        )
        assert coordinator._last_charge_switch is False

    @pytest.mark.asyncio
    async def test_charge_switch_skips_redundant(self, coordinator, mock_hass):
        """Charge switch skips redundant call."""
        await coordinator._async_set_charge_switch(True)
        mock_hass._sensor_states["switch.charge"] = make_state("on")
        mock_hass.services.async_call.reset_mock()
        await coordinator._async_set_charge_switch(True)
        mock_hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_discharge_switch_on(self, coordinator, mock_hass):
        """Discharge switch turns on."""
        await coordinator._async_set_discharge_switch(True)
        mock_hass.services.async_call.assert_any_call(
            "switch", "turn_on", {"entity_id": "switch.discharge"},
        )
        assert coordinator._last_discharge_switch is True

    @pytest.mark.asyncio
    async def test_discharge_switch_off(self, coordinator, mock_hass):
        """Discharge switch turns off."""
        await coordinator._async_set_discharge_switch(False)
        mock_hass.services.async_call.assert_any_call(
            "switch", "turn_off", {"entity_id": "switch.discharge"},
        )
        assert coordinator._last_discharge_switch is False

    @pytest.mark.asyncio
    async def test_discharge_switch_skips_redundant(self, coordinator, mock_hass):
        """Discharge switch skips redundant call."""
        await coordinator._async_set_discharge_switch(True)
        mock_hass._sensor_states["switch.discharge"] = make_state("on")
        mock_hass.services.async_call.reset_mock()
        await coordinator._async_set_discharge_switch(True)
        mock_hass.services.async_call.assert_not_called()


# ── FSM state transitions ───────────────────────────────────────────


class TestFSMStateTransitions:
    """Test FSM state transition logic."""

    def test_set_fsm_state_resets_tracking(self, coordinator):
        """_set_fsm_state resets all last-sent values."""
        coordinator._last_charge_power = 400
        coordinator._last_feed_in_power = 200
        coordinator._last_ps_mode = PS_MODE_PRIORITIZE_SUPPLY
        coordinator._last_charge_switch = True
        coordinator._last_discharge_switch = True

        coordinator._set_fsm_state(STATE_DISCHARGE, "test")

        assert coordinator._last_charge_power is None
        assert coordinator._last_feed_in_power is None
        assert coordinator._last_ps_mode is None
        assert coordinator._last_charge_switch is None
        assert coordinator._last_discharge_switch is None

    def test_set_fsm_state_no_change(self, coordinator):
        """Same state transition is a no-op."""
        coordinator._fsm_state = STATE_HOLD
        old_time = coordinator._fsm_state_entered_at

        coordinator._set_fsm_state(STATE_HOLD, "same")
        assert coordinator._fsm_state_entered_at == old_time

    def test_mode_change_resets_to_hold(self, coordinator):
        """Changing mode resets FSM to HOLD."""
        coordinator._fsm_state = STATE_DISCHARGE
        coordinator.active_mode = MODE_AUTOMATIC
        assert coordinator._fsm_state == STATE_HOLD


# ── Forced charge mode ───────────────────────────────────────────────


class TestForcedChargeMode:
    """Test forced charge mode behavior."""

    @pytest.mark.asyncio
    async def test_forced_charge_sets_max_power(self, coordinator, mock_hass):
        """Forced charge turns on charge switch, sets PS mode to storage, and sets max power."""
        coordinator._active_mode = MODE_FORCED_CHARGE
        await coordinator._run_forced_charge()

        mock_hass.services.async_call.assert_any_call(
            "switch", "turn_on", {"entity_id": "switch.charge"},
        )
        mock_hass.services.async_call.assert_any_call(
            "select",
            "select_option",
            {"entity_id": "select.ps_mode", "option": PS_MODE_PRIORITIZE_STORAGE},
        )
        assert coordinator._fsm_state == STATE_CHARGE


# ── Hold mode ────────────────────────────────────────────────────────


class TestHoldMode:
    """Test hold mode behavior."""

    @pytest.mark.asyncio
    async def test_hold_disables_everything(self, coordinator, mock_hass):
        """Hold mode stops charging and feeding in, turns off both switches."""
        await coordinator._run_hold()

        # Both Shelly switches should be turned off
        mock_hass.services.async_call.assert_any_call(
            "switch", "turn_off", {"entity_id": "switch.charge"},
        )
        mock_hass.services.async_call.assert_any_call(
            "switch", "turn_off", {"entity_id": "switch.discharge"},
        )
        assert coordinator._fsm_state == STATE_HOLD
        assert coordinator._current_charge_power == 0
        assert coordinator._current_feed_in_power == 0


# ── Automatic mode transitions ───────────────────────────────────────


class TestAutomaticMode:
    """Test automatic mode FSM transitions."""

    @pytest.mark.asyncio
    async def test_hold_to_charge_on_solar_surplus(self, coordinator, mock_hass):
        """HOLD → CHARGE when solar surplus detected."""
        coordinator._active_mode = MODE_AUTOMATIC
        coordinator._fsm_state = STATE_HOLD
        coordinator._fsm_state_entered_at = time.monotonic() - 120  # dwell exceeded

        # grid export (negative = exporting) means surplus
        await coordinator._run_automatic(
            grid_power=-200, solar_power=800, battery_soc=50
        )
        assert coordinator._fsm_state == STATE_CHARGE

    @pytest.mark.asyncio
    async def test_hold_to_discharge_on_high_consumption(self, coordinator, mock_hass):
        """HOLD → DISCHARGE when grid consumption > max_feed_in and SOC > min."""
        coordinator._active_mode = MODE_AUTOMATIC
        coordinator._fsm_state = STATE_HOLD
        coordinator._fsm_state_entered_at = time.monotonic() - 120

        # Default max_feed_in is 800, min_soc is 10
        await coordinator._run_automatic(
            grid_power=1000, solar_power=0, battery_soc=50
        )
        assert coordinator._fsm_state == STATE_DISCHARGE

    @pytest.mark.asyncio
    async def test_hold_stays_hold_low_consumption(self, coordinator, mock_hass):
        """HOLD stays HOLD when no surplus and grid within tolerance (dynamic mode)."""
        coordinator._active_mode = MODE_AUTOMATIC
        coordinator._fsm_state = STATE_HOLD
        coordinator._fsm_state_entered_at = time.monotonic() - 120
        coordinator._config_entry.options["grid_power_tolerance_discharge"] = 50

        # grid=30 is within tolerance (50), so no discharge trigger
        await coordinator._run_automatic(
            grid_power=30, solar_power=0, battery_soc=50
        )
        assert coordinator._fsm_state == STATE_HOLD

    @pytest.mark.asyncio
    async def test_hold_respects_custom_grid_tolerance(self, coordinator, mock_config_entry, mock_hass):
        """HOLD stays HOLD when grid import is below a custom tolerance (e.g. 150W)."""
        coordinator._active_mode = MODE_AUTOMATIC
        coordinator._fsm_state = STATE_HOLD
        coordinator._fsm_state_entered_at = time.monotonic() - 120

        # Set custom tolerance to 150W
        mock_config_entry.options["grid_power_tolerance_discharge"] = 150

        # grid=100 < tolerance 150 → should NOT discharge
        await coordinator._run_automatic(
            grid_power=100, solar_power=0, battery_soc=50
        )
        assert coordinator._fsm_state == STATE_HOLD

        # grid=200 > tolerance 150 → SHOULD discharge
        coordinator._fsm_state_entered_at = time.monotonic() - 120
        await coordinator._run_automatic(
            grid_power=200, solar_power=0, battery_soc=50
        )
        assert coordinator._fsm_state == STATE_DISCHARGE

    @pytest.mark.asyncio
    async def test_charge_to_hold_no_surplus_low_soc(self, coordinator, mock_hass):
        """CHARGE → HOLD when no solar surplus and SOC <= min (can't discharge)."""
        coordinator._active_mode = MODE_AUTOMATIC
        coordinator._fsm_state = STATE_CHARGE
        coordinator._fsm_state_entered_at = time.monotonic() - 120

        # High grid import, no solar, low SOC → can't discharge → HOLD
        await coordinator._run_automatic(
            grid_power=500, solar_power=0, battery_soc=10
        )
        assert coordinator._fsm_state == STATE_HOLD

    @pytest.mark.asyncio
    async def test_charge_stays_charge_with_solar_surplus_and_grid_import(self, coordinator, mock_hass):
        """CHARGE stays CHARGE when solar surplus exists even if grid import is high.

        This prevents oscillation: when charging from solar, the charge power
        itself causes grid import. The correct response is to reduce charge
        power (gradual ramping), NOT to switch to DISCHARGE.
        """
        coordinator._active_mode = MODE_AUTOMATIC
        coordinator._fsm_state = STATE_CHARGE
        coordinator._fsm_state_entered_at = time.monotonic() - 120
        # Simulate active charging at 300W which causes the 200W grid import
        coordinator._current_charge_power = 300
        coordinator._last_charge_power = 300

        # Solar surplus (700W solar, grid_import=200W from charging at 300W)
        # grid_natural = 200 - 300 + 0 = -100 → solar surplus detected
        await coordinator._run_automatic(
            grid_power=200, solar_power=700, battery_soc=50
        )
        # Should stay in CHARGE, not switch to DISCHARGE
        assert coordinator._fsm_state == STATE_CHARGE

    @pytest.mark.asyncio
    async def test_charge_to_discharge_no_solar(self, coordinator, mock_hass):
        """CHARGE → DISCHARGE when no solar surplus and SOC > min."""
        coordinator._active_mode = MODE_AUTOMATIC
        coordinator._fsm_state = STATE_CHARGE
        coordinator._fsm_state_entered_at = time.monotonic() - 120

        # No solar, sufficient SOC → DISCHARGE
        await coordinator._run_automatic(
            grid_power=500, solar_power=0, battery_soc=50
        )
        assert coordinator._fsm_state == STATE_DISCHARGE

    @pytest.mark.asyncio
    async def test_charge_to_discharge_high_consumption(self, coordinator, mock_hass):
        """CHARGE → DISCHARGE when high grid consumption and SOC > min."""
        coordinator._active_mode = MODE_AUTOMATIC
        coordinator._fsm_state = STATE_CHARGE
        coordinator._fsm_state_entered_at = time.monotonic() - 120

        await coordinator._run_automatic(
            grid_power=1000, solar_power=0, battery_soc=50
        )
        assert coordinator._fsm_state == STATE_DISCHARGE

    @pytest.mark.asyncio
    async def test_discharge_to_hold_low_soc(self, coordinator, mock_hass):
        """DISCHARGE → HOLD when battery SOC <= min."""
        coordinator._active_mode = MODE_AUTOMATIC
        coordinator._fsm_state = STATE_DISCHARGE
        coordinator._fsm_state_entered_at = time.monotonic() - 120

        # SOC at min (10%)
        await coordinator._run_automatic(
            grid_power=500, solar_power=0, battery_soc=10
        )
        assert coordinator._fsm_state == STATE_HOLD

    @pytest.mark.asyncio
    async def test_discharge_to_charge_solar_surplus(self, coordinator, mock_hass):
        """DISCHARGE → CHARGE when solar surplus detected."""
        coordinator._active_mode = MODE_AUTOMATIC
        coordinator._fsm_state = STATE_DISCHARGE
        coordinator._fsm_state_entered_at = time.monotonic() - 120
        # Set current solar power (normally set by _async_update_data)
        coordinator._current_solar_power = 1000

        await coordinator._run_automatic(
            grid_power=-200, solar_power=1000, battery_soc=50
        )
        assert coordinator._fsm_state == STATE_CHARGE

    @pytest.mark.asyncio
    async def test_dwell_time_prevents_transition(self, coordinator, mock_hass):
        """Transition is blocked if dwell time not exceeded."""
        coordinator._active_mode = MODE_AUTOMATIC
        coordinator._fsm_state = STATE_HOLD
        # Set entered_at to now → dwell NOT exceeded
        coordinator._fsm_state_entered_at = time.monotonic()

        await coordinator._run_automatic(
            grid_power=-100, solar_power=800, battery_soc=50
        )
        # Should stay in HOLD despite solar surplus
        assert coordinator._fsm_state == STATE_HOLD


# ── Dynamic feed-in calculation ──────────────────────────────────────


class TestDynamicFeedIn:
    """Test dynamic feed-in power calculation in discharge state.

    Dynamic feed-in uses PROPORTIONAL adjustment (scaled to grid error,
    damped and capped to reduce overshoot from sensor and device delay).
    The configured step size acts as the minimum adjustment granularity.
    """

    @pytest.mark.asyncio
    async def test_dynamic_feed_in_ramps_up_proportional(self, coordinator, mock_hass):
        """Dynamic feed-in increases proportionally when grid import > tolerance."""
        coordinator._active_mode = MODE_AUTOMATIC
        coordinator._fsm_state = STATE_DISCHARGE
        coordinator._fsm_state_entered_at = time.monotonic() - 120
        coordinator._current_feed_in_power = 0  # starting from 0

        # grid=500, tolerance=50 → error=450; per-cycle cap gives 50W.
        await coordinator._run_automatic(
            grid_power=500, solar_power=0, battery_soc=50
        )
        assert coordinator._current_feed_in_power == 50

    @pytest.mark.asyncio
    async def test_dynamic_feed_in_ramps_up_from_existing(self, coordinator, mock_hass):
        """Dynamic feed-in increases proportionally from current value."""
        coordinator._active_mode = MODE_AUTOMATIC
        coordinator._fsm_state = STATE_DISCHARGE
        coordinator._fsm_state_entered_at = time.monotonic() - 120
        coordinator._current_feed_in_power = 300
        coordinator._last_feed_in_power = 300

        # Large error is capped at a 50W increase per cycle.
        await coordinator._run_automatic(
            grid_power=500, solar_power=0, battery_soc=50
        )
        assert coordinator._current_feed_in_power == 350

    @pytest.mark.asyncio
    async def test_dynamic_feed_in_clamped_to_max(self, coordinator, mock_hass):
        """Dynamic feed-in ramp-up is clamped to max_grid_feed_in_power."""
        coordinator._active_mode = MODE_AUTOMATIC
        coordinator._fsm_state = STATE_DISCHARGE
        coordinator._fsm_state_entered_at = time.monotonic() - 120
        coordinator._current_feed_in_power = 800  # already at max
        coordinator._last_feed_in_power = 800

        # grid=2000, tolerance=50 → wants to ramp up but already at max 800
        await coordinator._run_automatic(
            grid_power=2000, solar_power=0, battery_soc=50
        )
        assert coordinator._current_feed_in_power == 800

    @pytest.mark.asyncio
    async def test_dynamic_feed_in_caps_large_single_cycle_change(
        self, coordinator, mock_hass
    ):
        """A single extreme grid reading cannot request a large feed-in jump."""
        coordinator._active_mode = MODE_AUTOMATIC
        coordinator._fsm_state = STATE_DISCHARGE
        coordinator._fsm_state_entered_at = time.monotonic() - 120

        await coordinator._run_automatic(
            grid_power=2000, solar_power=0, battery_soc=50
        )

        assert coordinator._current_feed_in_power == 50

    @pytest.mark.asyncio
    async def test_dynamic_feed_in_ramps_down_on_export(self, coordinator, mock_hass):
        """Grid export means we're far below target → proportional ramp down."""
        coordinator._active_mode = MODE_AUTOMATIC
        coordinator._fsm_state = STATE_DISCHARGE
        coordinator._fsm_state_entered_at = time.monotonic() - 120
        coordinator._current_feed_in_power = 400
        coordinator._last_feed_in_power = 400

        # grid=-100, tol=0 → damped adjustment rounds up to a 30W increment.
        await coordinator._run_automatic(
            grid_power=-100, solar_power=0, battery_soc=50
        )
        assert coordinator._current_feed_in_power == 370

    @pytest.mark.asyncio
    async def test_dynamic_feed_in_reduces_on_small_export(self, coordinator, mock_hass):
        """Small grid export triggers a small correction toward zero."""
        coordinator._active_mode = MODE_AUTOMATIC
        coordinator._fsm_state = STATE_DISCHARGE
        coordinator._fsm_state_entered_at = time.monotonic() - 120
        coordinator._current_feed_in_power = 800
        coordinator._last_feed_in_power = 800

        # grid=-30, tol=0 → 10W correction toward zero.
        await coordinator._run_automatic(
            grid_power=-30, solar_power=0, battery_soc=50
        )
        assert coordinator._current_feed_in_power == 790

    @pytest.mark.asyncio
    async def test_dynamic_feed_in_ramps_down_below_tolerance(
        self, coordinator, mock_config_entry, mock_hass
    ):
        """Grid import below target tolerance → ramp down to reduce feed-in."""
        coordinator._active_mode = MODE_AUTOMATIC
        coordinator._fsm_state = STATE_DISCHARGE
        coordinator._fsm_state_entered_at = time.monotonic() - 120
        coordinator._current_feed_in_power = 400
        coordinator._last_feed_in_power = 400

        # Set tolerance=150 (target grid import)
        mock_config_entry.options["grid_power_tolerance_discharge"] = 150
        mock_config_entry.options[OPT_FEED_IN_DEADBAND] = 20

        # grid=0, tol=150 → error=-150, beyond the stability deadband.
        await coordinator._run_automatic(
            grid_power=0, solar_power=0, battery_soc=50
        )
        assert coordinator._current_feed_in_power == 360  # 400 - 40

    @pytest.mark.asyncio
    async def test_dynamic_feed_in_ramps_down_large_gap(
        self, coordinator, mock_config_entry, mock_hass
    ):
        """Battery feeding in too much with grid well below target → large ramp down."""
        coordinator._active_mode = MODE_AUTOMATIC
        coordinator._fsm_state = STATE_DISCHARGE
        coordinator._fsm_state_entered_at = time.monotonic() - 120
        coordinator._current_feed_in_power = 600
        coordinator._last_feed_in_power = 600

        # Scenario: 668W consumption, 600W feed-in → 68W grid import
        # tolerance=200 (target), error=68-200=-132; adjustment rounds to 40W.
        mock_config_entry.options["grid_power_tolerance_discharge"] = 200
        await coordinator._run_automatic(
            grid_power=68, solar_power=0, battery_soc=50
        )
        assert coordinator._current_feed_in_power == 560

    @pytest.mark.asyncio
    async def test_dynamic_feed_in_holds_within_tolerance(self, coordinator, mock_hass):
        """Dynamic feed-in holds steady when grid within tolerance band."""
        coordinator._active_mode = MODE_AUTOMATIC
        coordinator._fsm_state = STATE_DISCHARGE
        coordinator._fsm_state_entered_at = time.monotonic() - 120
        coordinator._current_feed_in_power = 300
        coordinator._last_feed_in_power = 300

        # Grid is within the ±10W feed-in deadband around zero.
        await coordinator._run_automatic(
            grid_power=10, solar_power=0, battery_soc=50
        )
        assert coordinator._current_feed_in_power == 300

    @pytest.mark.asyncio
    async def test_dynamic_feed_in_zero_when_already_zero_and_low_grid(self, coordinator, mock_hass):
        """Dynamic feed-in stays 0 when grid within tolerance and starting at 0."""
        coordinator._active_mode = MODE_AUTOMATIC
        coordinator._fsm_state = STATE_DISCHARGE
        coordinator._fsm_state_entered_at = time.monotonic() - 120
        coordinator._current_feed_in_power = 0

        # Grid is within the ±10W feed-in deadband around zero.
        await coordinator._run_automatic(
            grid_power=10, solar_power=0, battery_soc=50
        )
        assert coordinator._current_feed_in_power == 0

    @pytest.mark.asyncio
    async def test_dynamic_feed_in_limits_each_increase(self, coordinator, mock_hass):
        """A persistent import ramps feed-in gradually instead of overshooting."""
        coordinator._active_mode = MODE_AUTOMATIC
        coordinator._fsm_state = STATE_DISCHARGE
        coordinator._fsm_state_entered_at = time.monotonic() - 120
        coordinator._current_feed_in_power = 0

        # Simulate 3 cycles with consistent high grid import.
        for i in range(3):
            await coordinator._run_automatic(
                grid_power=500, solar_power=0, battery_soc=50
            )
        assert coordinator._current_feed_in_power == 150

    @pytest.mark.asyncio
    async def test_dynamic_feed_in_holds_on_small_import_error(self, coordinator, mock_hass):
        """Small import error below the configured deadband holds steady."""
        coordinator._active_mode = MODE_AUTOMATIC
        coordinator._fsm_state = STATE_DISCHARGE
        coordinator._fsm_state_entered_at = time.monotonic() - 120
        coordinator._current_feed_in_power = 200
        coordinator._last_feed_in_power = 200

        # Grid is within the ±10W feed-in deadband around zero.
        await coordinator._run_automatic(
            grid_power=10, solar_power=0, battery_soc=50
        )
        assert coordinator._current_feed_in_power == 200  # hold, error too small

    @pytest.mark.asyncio
    async def test_dynamic_feed_in_uses_configured_deadband(
        self, coordinator, mock_config_entry, mock_hass
    ):
        """Configured deadband prevents adjustments for smaller grid errors."""
        coordinator._active_mode = MODE_AUTOMATIC
        coordinator._fsm_state = STATE_DISCHARGE
        coordinator._fsm_state_entered_at = time.monotonic() - 120
        coordinator._current_feed_in_power = 400
        coordinator._last_feed_in_power = 400
        mock_config_entry.options[OPT_FEED_IN_DEADBAND] = 200
        coordinator._config_entry = mock_config_entry

        # Error is below the configured feed-in deadband.
        await coordinator._run_automatic(
            grid_power=100, solar_power=0, battery_soc=50
        )

        assert coordinator._current_feed_in_power == 400


# ── Solar surplus detection ──────────────────────────────────────────


class TestSolarSurplus:
    """Test has_solar_surplus logic."""

    @pytest.mark.asyncio
    async def test_surplus_when_grid_export(self, coordinator, mock_hass):
        """Grid export (negative) means surplus."""
        coordinator._active_mode = MODE_AUTOMATIC
        coordinator._fsm_state = STATE_HOLD
        coordinator._fsm_state_entered_at = time.monotonic() - 120

        await coordinator._run_automatic(
            grid_power=-200, solar_power=300, battery_soc=50
        )
        assert coordinator._fsm_state == STATE_CHARGE

    @pytest.mark.asyncio
    async def test_no_surplus_high_grid_import(self, coordinator, mock_hass):
        """High grid import with some solar = no surplus."""
        coordinator._active_mode = MODE_AUTOMATIC
        coordinator._fsm_state = STATE_HOLD
        coordinator._fsm_state_entered_at = time.monotonic() - 120

        # grid_power=1100 > deadband=50, solar=753 > min_power=200
        # but grid_power(1100) >= deadband(50) so has_solar_surplus is False
        await coordinator._run_automatic(
            grid_power=1100, solar_power=753, battery_soc=50
        )
        # Should go to DISCHARGE (high consumption) not CHARGE
        assert coordinator._fsm_state == STATE_DISCHARGE

    @pytest.mark.parametrize(
        ("surplus", "expected"),
        [
            (200, {"number.charge_a": 0, "number.charge_b": 200}),
            (300, {"number.charge_a": 0, "number.charge_b": 300}),
            (400, {"number.charge_a": 200, "number.charge_b": 200}),
            (500, {"number.charge_a": 250, "number.charge_b": 250}),
        ],
    )
    @pytest.mark.asyncio
    async def test_automatic_solar_surplus_distributes_by_soc_and_minimum(
        self, coordinator, mock_hass, mock_config_entry, surplus, expected
    ):
        """Surplus charges the lowest SOC battery first, then shares evenly."""
        coordinator._active_mode = MODE_AUTOMATIC
        coordinator._fsm_state = STATE_CHARGE
        coordinator._batteries = [
            {
                CONF_BATTERY_SOC_SENSOR: "sensor.battery_a",
                CONF_MAX_CHARGE_POWER_NUMBER: "number.charge_a",
            },
            {
                CONF_BATTERY_SOC_SENSOR: "sensor.battery_b",
                CONF_MAX_CHARGE_POWER_NUMBER: "number.charge_b",
            },
        ]
        mock_config_entry.options[OPT_DEVICE_SETTINGS] = {
            "sensor.battery_a": {"min_charge": 200, "max_charge": 1200},
            "sensor.battery_b": {"min_charge": 200, "max_charge": 1200},
        }
        coordinator._config_entry = mock_config_entry
        mock_hass._sensor_states["sensor.battery_a"] = make_state(60)
        mock_hass._sensor_states["sensor.battery_b"] = make_state(30)

        await coordinator._run_automatic(
            grid_power=-surplus, solar_power=800, battery_soc=50
        )

        calls = {
            call.args[2]["entity_id"]: call.args[2]["value"]
            for call in mock_hass.services.async_call.call_args_list
            if call.args[0:2] == ("number", "set_value")
        }
        assert calls == expected
        assert coordinator._current_charge_power == surplus

    @pytest.mark.asyncio
    async def test_automatic_solar_charge_uses_configured_import_allowance(
        self, coordinator, mock_hass, mock_config_entry
    ):
        """Configured grid-import allowance can cover a shortfall below min charge."""
        coordinator._active_mode = MODE_AUTOMATIC
        coordinator._fsm_state = STATE_CHARGE
        coordinator._batteries = [
            {
                CONF_BATTERY_SOC_SENSOR: "sensor.battery_a",
                CONF_MAX_CHARGE_POWER_NUMBER: "number.charge_a",
            },
            {
                CONF_BATTERY_SOC_SENSOR: "sensor.battery_b",
                CONF_MAX_CHARGE_POWER_NUMBER: "number.charge_b",
            },
        ]
        mock_config_entry.options[OPT_DEVICE_SETTINGS] = {
            "sensor.battery_a": {"min_charge": 200, "max_charge": 1200},
            "sensor.battery_b": {"min_charge": 200, "max_charge": 1200},
        }
        mock_config_entry.options[OPT_MAX_GRID_IMPORT_SOLAR_CHARGE] = 200
        coordinator._config_entry = mock_config_entry

        await coordinator._run_automatic(
            grid_power=0, solar_power=800, battery_soc=50
        )

        assert coordinator._current_charge_power == 200

    @pytest.mark.asyncio
    async def test_solar_mode_allocates_surplus_to_lowest_soc_battery(
        self, coordinator, mock_hass, mock_config_entry
    ):
        """Solar-only mode uses net export and charges the lowest SOC first."""
        coordinator._batteries = [
            {
                CONF_BATTERY_SOC_SENSOR: "sensor.battery_a",
                CONF_MAX_CHARGE_POWER_NUMBER: "number.charge_a",
            },
            {
                CONF_BATTERY_SOC_SENSOR: "sensor.battery_b",
                CONF_MAX_CHARGE_POWER_NUMBER: "number.charge_b",
            },
        ]
        mock_config_entry.options[OPT_DEVICE_SETTINGS] = {
            "sensor.battery_a": {"min_charge": 200},
            "sensor.battery_b": {"min_charge": 200},
        }
        coordinator._config_entry = mock_config_entry
        mock_hass._sensor_states["sensor.battery_a"] = make_state(60)
        mock_hass._sensor_states["sensor.battery_b"] = make_state(30)

        await coordinator._run_solar(grid_power=-300, solar_power=800)

        calls = {
            call.args[2]["entity_id"]: call.args[2]["value"]
            for call in mock_hass.services.async_call.call_args_list
            if call.args[0:2] == ("number", "set_value")
        }
        assert calls == {"number.charge_a": 0, "number.charge_b": 300}
        assert coordinator._current_charge_power == 300

    @pytest.mark.asyncio
    async def test_hold_enters_charge_when_net_export_covers_one_battery_minimum(
        self, coordinator, mock_config_entry
    ):
        """One battery can charge when the surplus covers its own minimum."""
        coordinator._active_mode = MODE_AUTOMATIC
        coordinator._fsm_state = STATE_HOLD
        coordinator._fsm_state_entered_at = time.monotonic() - 120
        coordinator._current_solar_power = 800
        coordinator._batteries = [
            {CONF_BATTERY_SOC_SENSOR: "sensor.battery_a", CONF_MAX_CHARGE_POWER_NUMBER: "number.charge_a"},
            {CONF_BATTERY_SOC_SENSOR: "sensor.battery_b", CONF_MAX_CHARGE_POWER_NUMBER: "number.charge_b"},
        ]
        mock_config_entry.options[OPT_DEVICE_SETTINGS] = {
            "sensor.battery_a": {"min_charge": 200},
            "sensor.battery_b": {"min_charge": 200},
        }
        coordinator._config_entry = mock_config_entry

        await coordinator._run_automatic(
            grid_power=-200, solar_power=800, battery_soc=50
        )

        assert coordinator._fsm_state == STATE_CHARGE


# ── Logging ──────────────────────────────────────────────────────────


class TestLogging:
    """Test decision logging."""

    def test_log_buffer_records_entries(self, coordinator):
        """Log entries are added to the ring buffer."""
        coordinator._log_decision("test_event", "test reason")
        assert len(coordinator.log_entries) == 1
        assert coordinator.log_entries[0]["event"] == "test_event"
        assert coordinator.log_entries[0]["reason"] == "test reason"

    def test_log_buffer_max_size(self, coordinator):
        """Log buffer respects max size."""
        for i in range(150):
            coordinator._log_decision("event", f"reason {i}")
        assert len(coordinator.log_entries) == DEFAULT_LOG_BUFFER_SIZE

    def test_mode_change_logs(self, coordinator):
        """Mode change creates a log entry."""
        coordinator.active_mode = MODE_AUTOMATIC
        entries = coordinator.log_entries
        assert any("Mode changed" in e["reason"] for e in entries)

    def test_enabled_change_logs(self, coordinator):
        """Enabling/disabling creates a log entry."""
        coordinator.is_enabled = False
        entries = coordinator.log_entries
        assert any("disabled" in e["reason"] for e in entries)


# ── Enabled/disabled ─────────────────────────────────────────────────


class TestEnabled:
    """Test enable/disable behavior."""

    @pytest.mark.asyncio
    async def test_disabled_skips_control(self, coordinator, mock_hass):
        """When disabled, no mode logic runs."""
        coordinator._is_enabled = False
        coordinator._active_mode = MODE_FORCED_CHARGE

        data = await coordinator._async_update_data()
        # No service calls should be made
        mock_hass.services.async_call.assert_not_called()

    def test_disable_resets_tracking(self, coordinator):
        """Disabling resets all last-sent values."""
        coordinator._last_charge_power = 400
        coordinator._last_ps_mode = PS_MODE_PRIORITIZE_SUPPLY
        coordinator._last_charge_switch = True
        coordinator._last_discharge_switch = True

        coordinator.is_enabled = False

        assert coordinator._last_charge_power is None
        assert coordinator._last_ps_mode is None
        assert coordinator._last_charge_switch is None
        assert coordinator._last_discharge_switch is None


# ── EV Surplus Charging ──────────────────────────────────────────────


class TestEVCharging:
    """Test EV surplus charging overlay logic."""

    @pytest.mark.asyncio
    async def test_ev_not_configured_is_noop(self, coordinator, mock_hass):
        """Without EV entities configured, EV charging does nothing."""
        coordinator._active_mode = MODE_AUTOMATIC
        mock_hass._sensor_states["sensor.battery_soc"] = make_state(100)

        data = await coordinator._async_update_data()
        assert data.ev_charging_active is False
        assert data.ev_charging_current == 0.0
        assert data.ev_charging_power == 0.0

    @pytest.mark.asyncio
    async def test_ev_starts_after_delay_cycles(self, ev_coordinator, mock_hass):
        """EV charging starts after DEFAULT_EV_START_DELAY_CYCLES of sufficient excess."""
        ev_coordinator._active_mode = MODE_AUTOMATIC
        ev_coordinator._fsm_state = STATE_HOLD

        # SOC 100%, large grid export (surplus)
        mock_hass._sensor_states["sensor.battery_soc"] = make_state(100)
        mock_hass._sensor_states["sensor.grid_power"] = make_state(-2000)

        # Run cycles: should NOT start before delay
        for i in range(DEFAULT_EV_START_DELAY_CYCLES - 1):
            data = await ev_coordinator._async_update_data()
            assert data.ev_charging_active is False, f"Should not be active at cycle {i+1}"

        # One more cycle: should start now
        data = await ev_coordinator._async_update_data()
        assert data.ev_charging_active is True
        assert data.ev_charging_current > 0

    @pytest.mark.asyncio
    async def test_ev_does_not_start_below_soc_threshold(self, ev_coordinator, mock_hass):
        """EV charging does not start when SOC < 100%."""
        ev_coordinator._active_mode = MODE_AUTOMATIC
        ev_coordinator._fsm_state = STATE_HOLD

        mock_hass._sensor_states["sensor.battery_soc"] = make_state(95)
        mock_hass._sensor_states["sensor.grid_power"] = make_state(-2000)

        for _ in range(DEFAULT_EV_START_DELAY_CYCLES + 2):
            data = await ev_coordinator._async_update_data()
            assert data.ev_charging_active is False

    @pytest.mark.asyncio
    async def test_ev_stops_on_soc_drop(self, ev_coordinator, mock_hass):
        """EV charging stops immediately when SOC drops below 100%."""
        ev_coordinator._active_mode = MODE_AUTOMATIC
        ev_coordinator._fsm_state = STATE_HOLD

        mock_hass._sensor_states["sensor.battery_soc"] = make_state(100)
        mock_hass._sensor_states["sensor.grid_power"] = make_state(-2000)

        # Start EV charging
        for _ in range(DEFAULT_EV_START_DELAY_CYCLES - 1):
            await ev_coordinator._async_update_data()
        data = await ev_coordinator._async_update_data()
        assert data.ev_charging_active is True

        # SOC drops
        mock_hass._sensor_states["sensor.battery_soc"] = make_state(98)
        data = await ev_coordinator._async_update_data()
        assert data.ev_charging_active is False

    @pytest.mark.asyncio
    async def test_ev_stops_after_deficit_delay(self, ev_coordinator, mock_hass):
        """EV charging stops after DEFAULT_EV_STOP_DELAY_CYCLES of insufficient excess."""
        ev_coordinator._active_mode = MODE_AUTOMATIC
        ev_coordinator._fsm_state = STATE_HOLD

        mock_hass._sensor_states["sensor.battery_soc"] = make_state(100)
        mock_hass._sensor_states["sensor.grid_power"] = make_state(-2000)

        # Start EV charging (3 cycles for delay)
        for _ in range(DEFAULT_EV_START_DELAY_CYCLES - 1):
            await ev_coordinator._async_update_data()
        data = await ev_coordinator._async_update_data()
        assert data.ev_charging_active is True

        # Now set high grid import to create deficit even after accounting
        # for EV own consumption (8A * 230V = 1840W add-back)
        mock_hass._sensor_states["sensor.grid_power"] = make_state(3000)

        # Should still be active during deficit delay
        for i in range(DEFAULT_EV_STOP_DELAY_CYCLES - 1):
            data = await ev_coordinator._async_update_data()
            assert data.ev_charging_active is True, f"Should still be active at deficit cycle {i+1}"

        # One more cycle: should stop
        data = await ev_coordinator._async_update_data()
        assert data.ev_charging_active is False

    @pytest.mark.asyncio
    async def test_ev_adjusts_current_to_match_excess(self, ev_coordinator, mock_hass):
        """EV charging current adjusts proportionally to available excess."""
        ev_coordinator._active_mode = MODE_AUTOMATIC
        ev_coordinator._fsm_state = STATE_HOLD

        mock_hass._sensor_states["sensor.battery_soc"] = make_state(100)
        mock_hass._sensor_states["sensor.grid_power"] = make_state(-3000)

        # Start EV charging (start happens on the last cycle)
        for _ in range(DEFAULT_EV_START_DELAY_CYCLES - 1):
            await ev_coordinator._async_update_data()
        data = await ev_coordinator._async_update_data()
        assert data.ev_charging_active is True
        # 3000W / 230V / 1 phase = 13.04A → 13A
        assert data.ev_charging_current == 13

        # Reduce excess: account for current EV consumption
        # EV currently uses 13A * 230V = 2990W
        # New grid = -1500W → available = 1500 + 2990 = 4490W → 19.5A → clamped to 16A (max)
        mock_hass._sensor_states["sensor.grid_power"] = make_state(-1500)
        data = await ev_coordinator._async_update_data()
        assert data.ev_charging_current == 16  # clamped to max

    @pytest.mark.asyncio
    async def test_ev_accounts_for_own_consumption(self, ev_coordinator, mock_hass):
        """Available excess calculation accounts for EV's own power draw."""
        ev_coordinator._active_mode = MODE_AUTOMATIC
        ev_coordinator._fsm_state = STATE_HOLD

        mock_hass._sensor_states["sensor.battery_soc"] = make_state(100)
        mock_hass._sensor_states["sensor.grid_power"] = make_state(-2000)

        # Start charging (start happens on last cycle)
        for _ in range(DEFAULT_EV_START_DELAY_CYCLES - 1):
            await ev_coordinator._async_update_data()
        data = await ev_coordinator._async_update_data()
        assert data.ev_charging_active is True
        initial_current = data.ev_charging_current
        # 2000W / 230V = 8.7A → 8A
        assert initial_current == 8

        # Now grid shows 0W (because EV is consuming exactly the surplus)
        # EV power = 8A * 230V = 1840W
        # available = -0 + 1840 = 1840W → 1840/230 = 8A
        mock_hass._sensor_states["sensor.grid_power"] = make_state(0)
        data = await ev_coordinator._async_update_data()
        assert data.ev_charging_active is True
        assert data.ev_charging_current == 8  # Stays at 8A

    @pytest.mark.asyncio
    async def test_ev_three_phase_calculation(self, ev_coordinator, mock_config_entry, mock_hass):
        """3-phase EV charger: power = amps * 230V * 3 = 690W per amp."""
        mock_config_entry.options[OPT_EV_CHARGER_PHASES] = 3
        ev_coordinator._active_mode = MODE_AUTOMATIC
        ev_coordinator._fsm_state = STATE_HOLD

        mock_hass._sensor_states["sensor.battery_soc"] = make_state(100)
        mock_hass._sensor_states["sensor.grid_power"] = make_state(-5000)

        # Start charging (start happens on last cycle)
        for _ in range(DEFAULT_EV_START_DELAY_CYCLES - 1):
            await ev_coordinator._async_update_data()
        data = await ev_coordinator._async_update_data()
        assert data.ev_charging_active is True
        # 5000W / (230V * 3) = 7.25A → 7A
        assert data.ev_charging_current == 7
        # Power = 7A * 230V * 3 = 4830W
        assert data.ev_charging_power == 7 * DEFAULT_EV_VOLTAGE * 3
