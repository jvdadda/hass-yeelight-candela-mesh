"""Config flow for Yeelight Candela Mesh."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS

from .const import (
    DOMAIN,
    DEFAULT_MESH_NAME,
    DEFAULT_MESH_PASSWORD,
    CONF_MESH_NAME,
    CONF_MESH_PASSWORD,
    CONF_GATEWAY_MAC,
    YEELIGHT_AD_SERVICE_UUID,
)

_LOGGER = logging.getLogger(__name__)

# Yeelight BLE name patterns observed in the wild
NAME_PATTERNS = ("yeelight_ms", "yl_candela", "candela")


class YeelightCandelaMeshConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle config flow for Yeelight Candela Mesh."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered_addresses: list[tuple[str, str, int]] = []  # (address, name, rssi)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: scan and pick a gateway lamp + enter mesh credentials."""
        # Scan BLE for Yeelight Candelas
        self._discovered_addresses = []
        for service_info in bluetooth.async_discovered_service_info(self.hass, connectable=True):
            name_lower = (service_info.name or "").lower()
            if not any(pat in name_lower for pat in NAME_PATTERNS):
                continue
            self._discovered_addresses.append(
                (service_info.address, service_info.name or "<unnamed>", service_info.rssi)
            )

        if not self._discovered_addresses:
            return self.async_abort(reason="no_devices_found")

        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_GATEWAY_MAC])
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=f"Candela Mesh ({user_input[CONF_MESH_NAME]})",
                data={
                    CONF_GATEWAY_MAC: user_input[CONF_GATEWAY_MAC],
                    CONF_MESH_NAME: user_input[CONF_MESH_NAME],
                    CONF_MESH_PASSWORD: user_input[CONF_MESH_PASSWORD],
                },
            )

        # Sort discovered by RSSI desc (closest first as default)
        self._discovered_addresses.sort(key=lambda x: x[2] or -200, reverse=True)
        choices = {
            addr: f"{name} ({addr}, RSSI={rssi})"
            for addr, name, rssi in self._discovered_addresses
        }

        schema = vol.Schema(
            {
                vol.Required(CONF_GATEWAY_MAC, default=self._discovered_addresses[0][0]): vol.In(choices),
                vol.Required(CONF_MESH_NAME, default=DEFAULT_MESH_NAME): str,
                vol.Required(CONF_MESH_PASSWORD, default=DEFAULT_MESH_PASSWORD): str,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            description_placeholders={
                "default_name": DEFAULT_MESH_NAME,
                "default_password": DEFAULT_MESH_PASSWORD,
            },
        )

    async def async_step_bluetooth(
        self, discovery_info: bluetooth.BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Auto-discovery via the bluetooth integration (when a Candela is found)."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        # Stash for confirm step
        self.context["title_placeholders"] = {"name": discovery_info.name or "Yeelight Candela"}
        self._discovered_addresses = [
            (discovery_info.address, discovery_info.name or "<unnamed>", discovery_info.rssi)
        ]
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm bluetooth-discovered device + ask for mesh credentials."""
        if user_input is not None:
            address = self._discovered_addresses[0][0]
            return self.async_create_entry(
                title=f"Candela Mesh ({user_input[CONF_MESH_NAME]})",
                data={
                    CONF_GATEWAY_MAC: address,
                    CONF_MESH_NAME: user_input[CONF_MESH_NAME],
                    CONF_MESH_PASSWORD: user_input[CONF_MESH_PASSWORD],
                },
            )
        schema = vol.Schema(
            {
                vol.Required(CONF_MESH_NAME, default=DEFAULT_MESH_NAME): str,
                vol.Required(CONF_MESH_PASSWORD, default=DEFAULT_MESH_PASSWORD): str,
            }
        )
        return self.async_show_form(step_id="bluetooth_confirm", data_schema=schema)
