"""Telink Mesh client for Yeelight Candela.

Reverse-engineered from yeelight-3-5-4.apk (com.telink.bluetooth.light.*) and
cross-validated against the open-source BleMeshLib SDK (zhourenjun/BleMeshLib).

Architecture: 1 GATT connection to a "gateway" lamp + broadcast to mesh address
0xFFFF reaches all lamps in the same mesh network simultaneously (~10ms latency).

The Telink firmware drops the GATT connection after ~30s of inactivity.
We auto-reconnect in background. UX impact: ~10s delay on the first command
after a drop, instant otherwise.
"""
from __future__ import annotations

import asyncio
import logging
import struct
from os import urandom

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from Crypto.Cipher import AES

from .const import (
    PAIR_CHAR_UUID,
    COMMAND_CHAR_UUID,
    NOTIFY_CHAR_UUID,
    ADDR_BROADCAST,
    OP_POWER,
    OP_BRIGHTNESS,
    OP_STATUS_QUERY,
    VENDOR_ID,
    PAIR_TIMEOUT_S,
    KEEPALIVE_INTERVAL_S,
    COMMAND_THROTTLE_MS,
)

_LOGGER = logging.getLogger(__name__)


# === Telink Mesh crypto primitives (port of Awox/BleMeshLib packetutils) ===

def _aes_ecb(key: bytes, value: bytes) -> bytearray:
    """AES-128-ECB with key+value byte-reverse (Telink convention).

    Both key and value are reversed before encryption (Telink stores everything
    little-endian explicitly). The result is also reversed back.
    """
    assert len(key) == 16, f"key must be 16 bytes, got {len(key)}"
    k = bytearray(key)
    val = bytearray(value.ljust(16, b"\x00"))
    k.reverse()
    val.reverse()
    cipher = AES.new(bytes(k), AES.MODE_ECB)
    out = bytearray(cipher.encrypt(bytes(val)))
    out.reverse()
    return out


def _make_checksum(key: bytes, nonce: bytes, payload: bytes) -> bytearray:
    """CCM-style MIC: AES-CBC over (nonce | len | payload chunks)."""
    base = (bytes(nonce) + bytes([len(payload)])).ljust(16, b"\x00")
    check = _aes_ecb(key, base)
    for i in range(0, len(payload), 16):
        chunk = bytearray(payload[i : i + 16].ljust(16, b"\x00"))
        check = bytearray(a ^ b for a, b in zip(check, chunk))
        check = _aes_ecb(key, bytes(check))
    return check


def _crypt_payload(key: bytes, nonce: bytes, payload: bytes) -> bytearray:
    """AES-CTR-style XOR (encryption == decryption)."""
    base = bytearray(b"\x00" + nonce).ljust(16, b"\x00")
    result = bytearray()
    for i in range(0, len(payload), 16):
        enc_base = _aes_ecb(key, bytes(base))
        chunk = bytearray(payload[i : i + 16])
        result += bytearray(a ^ b for a, b in zip(enc_base, chunk))
        base[0] = (base[0] + 1) & 0xFF
    return result


def _make_pair_packet(mesh_name: bytes, mesh_password: bytes, session_random: bytes) -> bytes:
    """Build the 17-byte pair handshake packet (header 0x0c + 8B random + 8B encrypted check)."""
    m_n = bytearray(mesh_name.ljust(16, b"\x00"))
    m_p = bytearray(mesh_password.ljust(16, b"\x00"))
    s_r = bytes(session_random).ljust(16, b"\x00")
    name_pass = bytearray(a ^ b for a, b in zip(m_n, m_p))
    enc = _aes_ecb(s_r, bytes(name_pass))
    return bytes(b"\x0c" + bytes(session_random) + bytes(enc[:8]))


def _make_session_key(
    mesh_name: bytes, mesh_password: bytes, session_random: bytes, response_random: bytes
) -> bytes:
    """Derive the AES session key from the pair handshake exchange."""
    rnd = bytes(session_random) + bytes(response_random)
    m_n = bytearray(mesh_name.ljust(16, b"\x00"))
    m_p = bytearray(mesh_password.ljust(16, b"\x00"))
    name_pass = bytearray(a ^ b for a, b in zip(m_n, m_p))
    return bytes(_aes_ecb(bytes(name_pass), rnd))


def _make_command_packet(
    session_key: bytes, gateway_mac: str, dest_mesh_addr: int, opcode: int, params: bytes
) -> bytes:
    """Build a 20-byte encrypted Telink Mesh command packet.

    Wire format:
        [seq:3 clear] + [MIC:2 clear] + [encrypted: dest:2 + opcode:1 + vendor_id_BE:2 + params:10]

    Vendor bytes: live tests confirm `0x64 0x01` (Awox-style, low-high) works on
    Yeelight Candela. The BleMeshLib SDK uses `vendor_id BE` (high-low = 0x01 0x64
    for Yeelight CID 0x0164). We keep the validated 0x64 0x01 for now; switch to
    0x01 0x64 in a future release if confirmed needed for other Yeelight models.
    """
    seq = urandom(3)

    # Build nonce: gateway MAC reversed (4 bytes) + 0x01 + seq (3 bytes)
    mac_bytes = bytearray.fromhex(gateway_mac.replace(":", ""))
    mac_bytes.reverse()
    nonce = bytes(mac_bytes[:4]) + b"\x01" + bytes(seq)

    # Plaintext payload (15 bytes, padded with 0x00):
    # bytes 0-1: dest mesh address (LE)
    # byte 2:   opcode | 0xC0 (vendor opcode flag, per SIG-Mesh)
    # bytes 3-4: vendor bytes 0x64 0x01 (live-validated; see docstring)
    # bytes 5-14: params (up to 10 bytes)
    dest_le = struct.pack("<H", dest_mesh_addr)
    op_byte = bytes([opcode | 0xC0])
    vendor_bytes = b"\x64\x01"  # validated live; see docstring for SDK 0x01 0x64 alternative
    plaintext = (dest_le + op_byte + vendor_bytes + bytes(params)).ljust(15, b"\x00")

    mic = _make_checksum(session_key, nonce, plaintext)
    encrypted = _crypt_payload(session_key, nonce, plaintext)

    return bytes(seq) + bytes(mic[:2]) + bytes(encrypted)


# === High-level mesh client ===

class TelinkMeshClient:
    """Manages a single GATT connection to one lamp + broadcast to the whole mesh.

    Lifecycle:
        await client.connect()       # GATT connect + pair handshake
        await client.send_power(True)
        await client.send_brightness(80)
        await client.disconnect()

    The client auto-reconnects on drop. Commands queued during a reconnect are
    sent once the new session is established. Throttles same-opcode commands
    at COMMAND_THROTTLE_MS to avoid overflowing the BLE FIFO.
    """

    def __init__(self, ble_device: BLEDevice, gateway_mac: str, mesh_name: str, mesh_password: str):
        self._ble_device = ble_device
        self._gateway_mac = gateway_mac
        self._mesh_name = mesh_name.encode()
        self._mesh_password = mesh_password.encode()

        self._client: BleakClient | None = None
        self._session_key: bytes | None = None
        self._lock = asyncio.Lock()
        self._last_cmd_at: dict[int, float] = {}  # opcode -> last send timestamp
        self._keepalive_task: asyncio.Task | None = None
        self._notify_callbacks: list = []

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected and self._session_key is not None

    async def connect(self) -> None:
        """Open GATT connection, pair handshake, derive session key. ~10s on Candela."""
        async with self._lock:
            if self.is_connected:
                return
            _LOGGER.info("Connecting to %s (mesh=%s)", self._gateway_mac, self._mesh_name.decode())
            self._client = BleakClient(self._ble_device, timeout=PAIR_TIMEOUT_S)
            await self._client.connect()

            session_random = urandom(8)
            pair_pkt = _make_pair_packet(self._mesh_name, self._mesh_password, session_random)
            await self._client.write_gatt_char(PAIR_CHAR_UUID, pair_pkt, response=True)
            await asyncio.sleep(0.5)
            reply = await self._client.read_gatt_char(PAIR_CHAR_UUID)
            if not reply or reply[0] != 0x0D:
                code = f"0x{reply[0]:02x}" if reply else "no-reply"
                await self._client.disconnect()
                self._client = None
                raise RuntimeError(
                    f"Pair handshake failed (expected 0x0d, got {code}). "
                    f"Check mesh_name and mesh_password."
                )
            response_random = bytes(reply[1:9])
            self._session_key = _make_session_key(
                self._mesh_name, self._mesh_password, session_random, response_random
            )
            _LOGGER.info("Paired. Session key derived.")

            # Start keepalive loop (the lamp drops after ~30s, our throttle helps but eventually drops)
            if self._keepalive_task is None or self._keepalive_task.done():
                self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def disconnect(self) -> None:
        """Close the connection cleanly."""
        async with self._lock:
            if self._keepalive_task and not self._keepalive_task.done():
                self._keepalive_task.cancel()
            if self._client is not None:
                try:
                    await self._client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            self._client = None
            self._session_key = None

    async def _keepalive_loop(self) -> None:
        """Send a status_query every KEEPALIVE_INTERVAL_S to extend connection lifetime."""
        try:
            while True:
                await asyncio.sleep(KEEPALIVE_INTERVAL_S)
                if not self.is_connected:
                    return
                try:
                    await self._send_raw(OP_STATUS_QUERY, ADDR_BROADCAST, [16], throttle=False)
                except Exception as e:  # noqa: BLE001
                    _LOGGER.debug("Keepalive failed: %s — connection probably dropped", e)
                    return
        except asyncio.CancelledError:
            pass

    async def _ensure_connected(self) -> None:
        """Reconnect if the session is dead. Used lazily before each command."""
        if not self.is_connected:
            try:
                await self.connect()
            except Exception:  # noqa: BLE001
                self._client = None
                self._session_key = None
                raise

    async def _send_raw(
        self, opcode: int, dest_addr: int, params: list[int] | bytes, throttle: bool = True
    ) -> None:
        """Send an encrypted Telink Mesh command. Auto-throttle per opcode."""
        if throttle:
            now = asyncio.get_event_loop().time() * 1000
            last = self._last_cmd_at.get(opcode, 0)
            wait = COMMAND_THROTTLE_MS - (now - last)
            if wait > 0:
                await asyncio.sleep(wait / 1000)
            self._last_cmd_at[opcode] = asyncio.get_event_loop().time() * 1000

        await self._ensure_connected()
        pkt = _make_command_packet(self._session_key, self._gateway_mac, dest_addr, opcode, bytes(params))
        try:
            await self._client.write_gatt_char(COMMAND_CHAR_UUID, pkt, response=True)
        except (BleakError, EOFError) as e:
            _LOGGER.warning("Write failed (%s), invalidating session", e)
            self._client = None
            self._session_key = None
            raise

    # === Public commands (broadcast to whole mesh by default) ===

    async def send_power(self, on: bool, dest: int = ADDR_BROADCAST) -> None:
        """Turn lamps on (True) or off (False)."""
        await self._send_raw(OP_POWER, dest, [1 if on else 0, 0, 0])

    async def send_brightness(self, brightness_0_100: int, dest: int = ADDR_BROADCAST) -> None:
        """Set brightness on a 0-100 scale (Telink native unit)."""
        b = max(0, min(100, int(brightness_0_100)))
        await self._send_raw(OP_BRIGHTNESS, dest, [b, 0, 0])

    async def query_status(self, dest: int = ADDR_BROADCAST) -> None:
        """Request status notifications from all lamps in the mesh."""
        await self._send_raw(OP_STATUS_QUERY, dest, [16], throttle=False)
