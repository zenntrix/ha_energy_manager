"""Tests for integration diagnostics exports."""

from __future__ import annotations

import pytest

from custom_components.ha_energy_manager.const import DOMAIN
from custom_components.ha_energy_manager.diagnostics import (
    async_get_config_entry_diagnostics,
)


@pytest.mark.asyncio
async def test_diagnostics_includes_control_trace(
    coordinator, mock_hass, mock_config_entry
):
    """Per-cycle controller data is available in downloaded diagnostics."""
    mock_hass.data = {DOMAIN: {mock_config_entry.entry_id: coordinator}}
    coordinator.data = None
    trace_entry = {"grid_raw_w": -300, "charge_target_w": 300}
    coordinator._control_trace.append(trace_entry)

    diagnostics = await async_get_config_entry_diagnostics(
        mock_hass, mock_config_entry
    )

    assert diagnostics["control_trace"] == [trace_entry]
