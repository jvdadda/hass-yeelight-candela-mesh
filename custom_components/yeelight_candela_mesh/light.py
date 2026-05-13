"""Light platform for Yeelight Candela Mesh.

Exposes a SINGLE LightEntity (`light.candela_mesh_<name>`) that broadcasts to
all Candelas on the same mesh network. One command → all lamps in sync.

Why a single entity (vs one per lamp like hass-yeelightbt)?
  - The mesh is the natural unit. The Candelas were designed to act as a group.
  - 1 broadcast = 3 lamps in ~10ms. Pre-existing HA `light_group` would send 3
    sequential commands = wasteful + visually unsync.
  - Per-lamp control IS technically possible via mesh address, but requires
    discovering each lamp's mesh_id (opcode 0xDD). Future enhancement.
"""
from __future__ import annotations

import logging

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, CONF_GATEWAY_MAC
from .mesh import TelinkMeshClient

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the light entity for a config entry."""
    client: TelinkMeshClient = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([CandelaMeshLight(client, entry)])


class CandelaMeshLight(LightEntity):
    """Single broadcast light entity for the whole Candela mesh."""

    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_should_poll = False  # event-driven via NOTIFY (future); state pushed on cmd success
    _attr_assumed_state = True  # we don't read NOTIFY frames yet — UI shows the "assumed" indicator
    _attr_has_entity_name = True
    _attr_name = None  # this is the device's only/main entity — display the device name as-is

    def __init__(self, client: TelinkMeshClient, entry: ConfigEntry):
        self._client = client
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_mesh"
        # Start with state unknown — we won't claim on/off until we either
        # successfully send a command or (later) parse a NOTIFY frame.
        self._attr_is_on = None
        self._attr_brightness = None

    async def async_added_to_hass(self) -> None:
        """Hook the client's connection events so the UI flips to 'unknown'
        the moment we lose the GATT session, instead of showing a stale
        last-commanded state forever."""
        self._client.add_connection_listener(self._on_connection_change)
        # Initial paint: if the client somehow already disconnected before
        # we got here, reflect that.
        self._on_connection_change()

    def _on_connection_change(self) -> None:
        if not self._client.is_connected:
            self._attr_is_on = None
            self._attr_brightness = None
        if self.hass is not None:
            self.async_write_ha_state()

    @property
    def device_info(self):
        """Group all entities under one device."""
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": self._entry.title,
            "manufacturer": "Yeelight",
            "model": "YLFW01YL (mesh group)",
            "connections": {("bluetooth", self._entry.data[CONF_GATEWAY_MAC])},
        }

    async def async_turn_on(self, **kwargs) -> None:
        """Turn on. Optional brightness (HA 0-255 scale).

        Smart-toggle behaviour: a turn_on call issued while we already
        believe the lamps to be on (and no new brightness is supplied)
        is treated as a turn_off. Lets the same physical/voice button
        toggle the lamps without needing a separate 'toggle' service.

        Note: this slightly bends HA's standard light.turn_on contract.
        Automations that rely on turn_on being a strict 'set to on'
        should call light.toggle explicitly to avoid the off-flip, or
        always supply a brightness (which disables the smart-toggle and
        falls through to the normal set-brightness path).
        """
        if self._attr_is_on and ATTR_BRIGHTNESS not in kwargs:
            return await self.async_turn_off()
        try:
            # Always send power-on first (lamps may be off and ignore brightness alone)
            await self._client.send_power(True)
            if ATTR_BRIGHTNESS in kwargs:
                ha_brightness = int(kwargs[ATTR_BRIGHTNESS])
                # Map HA 0-255 → Telink 1-100 (avoid 0 which the Candela treats as no-op)
                telink_b = max(1, round(ha_brightness * 100 / 255))
                await self._client.send_brightness(telink_b)
                self._attr_brightness = ha_brightness
            elif not self._attr_brightness:
                # First on with no remembered brightness → default to mid.
                self._attr_brightness = 128
                await self._client.send_brightness(50)
            self._attr_is_on = True
            self.async_write_ha_state()
        except Exception as e:  # noqa: BLE001
            _LOGGER.error("turn_on failed: %s", e)
            raise

    async def async_turn_off(self, **kwargs) -> None:
        """Turn off all lamps in the mesh."""
        try:
            await self._client.send_power(False)
            self._attr_is_on = False
            self.async_write_ha_state()
        except Exception as e:  # noqa: BLE001
            _LOGGER.error("turn_off failed: %s", e)
            raise
