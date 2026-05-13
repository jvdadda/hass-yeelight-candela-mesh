"""Yeelight Candela Mesh integration."""
from __future__ import annotations

import logging

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    DOMAIN,
    CONF_GATEWAY_MAC,
    CONF_MESH_NAME,
    CONF_MESH_PASSWORD,
)
from .mesh import TelinkMeshClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.LIGHT]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Yeelight Candela Mesh from a config entry."""
    gateway_mac = entry.data[CONF_GATEWAY_MAC]
    mesh_name = entry.data[CONF_MESH_NAME]
    mesh_password = entry.data[CONF_MESH_PASSWORD]

    def get_device():
        """Resolve a fresh BLEDevice for the gateway MAC.

        Re-resolved on every reconnect because HA's bluetooth integration
        ages out devices that haven't been seen by any scanner recently
        (~60s window). Holding the BLEDevice object captured at setup
        time would yield a stale reference and bleak would fail with
        'No backend with an available connection slot that can reach
        address X' as soon as the lamp drifted out of the cache.
        """
        return bluetooth.async_ble_device_from_address(hass, gateway_mac, connectable=True)

    if get_device() is None:
        raise ConfigEntryNotReady(
            f"Yeelight Candela gateway lamp {gateway_mac} not visible. "
            f"Power on at least one Candela on the same mesh network."
        )

    client = TelinkMeshClient(get_device, gateway_mac, mesh_name, mesh_password)
    try:
        await client.connect()
    except Exception as e:
        raise ConfigEntryNotReady(f"Pairing failed: {e}") from e

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = client
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        client: TelinkMeshClient = hass.data[DOMAIN].pop(entry.entry_id)
        await client.disconnect()
    return unload_ok
