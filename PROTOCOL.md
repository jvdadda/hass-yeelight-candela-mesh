# Yeelight Candela protocol notes

> Audience: future contributors and LLMs working on this integration.
> This is the "how it actually works under the hood" doc — the README
> stays product-focused.

This document captures what we learned by reverse engineering the official
Yeelight Android APK (`yeelight-3-5-4.apk`, package `com.yeelight.cherry`)
and validating it empirically on `YLFW01YL` Candelas. Everything below was
either read straight from decompiled Java (`com.telink.bluetooth.light.*`)
or confirmed live on hardware with throwaway Python POCs.

---

## 1. Hardware

| Component | Detail |
|---|---|
| Lamp model | Yeelight Candela `YLFW01YL` |
| MCU/Radio | Telink **TLSR8253** (BLE 5.0 + Telink proprietary 2.4 GHz mode) |
| Stack | Telink "Light Mesh" — a **proprietary mesh** that pre-dates SIG Mesh |
| Default mesh name | `yeelight_ms` (factory) |
| Default mesh password | `YLu2M80aE` (factory) |
| Default LTK | `0x01,0x02,0x03,0x04,0x05,0x06,0x07,0x08,0x09,0x0a,0x0b,0x0c,0x0d,0x0e,0x0f,0x10` |

**Important:** "Telink mesh" ≠ Bluetooth SIG Mesh. The wire format,
provisioning, addressing, and crypto are all Telink-specific. Generic
SIG-Mesh tooling (e.g. `mesh-cfg-test`, `bluez-meshd`) will **not** talk
to these lamps.

---

## 2. Two-layer model

```
┌──────────────┐  GATT (BLE pair, encrypted)   ┌────────────┐  proprietary 2.4 GHz radio   ┌──────────┐
│  Home        │ ←───────────────────────────→ │  Gateway   │ ←──────────────────────────→ │  Peer    │
│  Assistant   │       1 connection             │  Candela   │     (NOT standard BLE Adv)    │  Candela │
│  (Pi 5)      │                                │  (any one) │                              │  (Nx)    │
└──────────────┘                                └────────────┘                              └──────────┘
```

Two completely separate transports:

- **GATT layer (Pi ↔ gateway)** — standard BLE, characteristics on the
  Telink Light service. We talk to *one* lamp here (the "gateway").
- **Mesh layer (gateway ↔ peers)** — *not* standard BLE Advertising.
  Empirical observation: a passive scanner on the Pi's bcm43455c0 chip
  sees zero traffic between lamps even when one is transmitting commands
  (validated with `bluetoothctl` and `btmon`). The Telink chip almost
  certainly switches to its **2.4 GHz proprietary mode** (Telink
  "TLSR8x mesh") for inter-node propagation, which a stock BLE radio
  cannot capture.

**Operational consequence:** you *must* pair with at least one lamp via
GATT. There is no "TX a packet from the Pi and let the lamps relay it"
shortcut without specialised hardware (e.g. an nRF52840 reflashed with
custom firmware that speaks Telink's 2.4 GHz proto — out of scope).

---

## 3. GATT service / characteristics

Telink Light service and its 5 characteristics (note the unusual
non-Bluetooth-SIG UUID base):

| Role | UUID | Notes |
|---|---|---|
| Service | `00010203-0405-0607-0809-0a0b0c0d1910` | The Telink Light service |
| **Pair** | `00010203-0405-0607-0809-0a0b0c0d1914` | Write to start handshake, read to get response |
| **Command** | `00010203-0405-0607-0809-0a0b0c0d1912` | Write encrypted command frames here |
| **Notify** | `00010203-0405-0607-0809-0a0b0c0d1911` | Subscribe for status notifications + use for keepalive read |
| OTA | `00010203-0405-0607-0809-0a0b0c0d1913` | Firmware updates (unused by this integration) |

The lamp also advertises an extra service UUID `0xfe87` in its scan
response — useful for filtering during discovery (see `config_flow.py`).

---

## 4. Pair handshake

The Pi proves it knows `mesh_name` + `mesh_password` without sending
them in the clear. Both sides then derive a per-session AES key.

### Step 1 — client → lamp

Pi picks 8 random bytes `Sc` (session_random_client), then writes 17
bytes to the **pair** characteristic:

```
[0x0c] [Sc[0..7]] [E[0..7]]
```

where `E = AES-ECB(key=LTK, plaintext=(name XOR password) padded to 16) [0..7]`
with the **Telink byte-reverse convention** applied:

- Reverse the key bytes before passing to AES
- Reverse the plaintext bytes before passing to AES
- Reverse the 16-byte output before truncating to 8

The LTK is the default `01..10` for factory-paired lamps (see Hardware
table above). If the lamp was ever paired in the Mi Home / Yeelight app
the LTK will have been rotated and you must extract it from the cloud
blob (out of scope).

### Step 2 — lamp → client

Pi reads the **pair** characteristic, expecting 17 bytes:

```
[0x0d] [Ss[0..7]] [Es[0..7]]
```

- First byte `0x0d` = success. Anything else (commonly `0x0e`) = auth
  failure (wrong mesh name/password).
- `Ss` = 8 random bytes from the lamp.
- `Es` = lamp's encryption proof (we don't need to verify it for control
  to work, but a defensive impl could).

### Step 3 — derive session key

```
session_key = AES-ECB(
    key   = LTK,
    plain = Sc[0..7] || Ss[0..7]    // 16 bytes, with byte-reverse convention
)
```

Both sides now hold the same 16-byte `session_key`. All subsequent
command frames are encrypted with it.

---

## 5. Command frame (20 bytes)

Every write to the **command** characteristic is exactly 20 bytes:

```
offset  bytes  field
------  -----  -----
 0..1   2      sequence number (LE, increment per command, wraps)
 2      1      source MAC byte 0   (we use 0x00 — Pi has no mesh address)
 3      1      source MAC byte 1   (we use 0x00)
 4..5   2      MIC placeholder     (overwritten with checksum after encryption)
 6..7   2      destination mesh address (LE)
 8..9   2      vendor bytes         (see "vendor mystery" below)
 10     1      opcode               (see opcode table below)
 11..19 9      opcode parameters (zero-padded if shorter)
```

### Encryption

The `[6..19]` slice is XOR-encrypted with an AES-CTR-style stream:

```python
# AES-CTR-like keystream (Telink custom)
nonce = gateway_mac[3..0] || sequence_LE || 0x00 || 0x00 || 0x00 || 0x00
        # = 8 bytes nonce, then a counter byte that's incremented
ciphertext = plaintext ^ AES-ECB(session_key, nonce_with_counter_block)
```

(See `mesh.py:_crypt_payload` for the exact byte ordering — Telink
deviates from textbook CTR in subtle ways: starting counter, padding,
and the omnipresent byte-reverse on the AES key.)

### MIC (bytes 4..5)

After encrypting the payload, compute a 2-byte CCM-like MIC over
`(nonce || ciphertext)` keyed on `session_key`, and store it at offset
`[4..5]` (overwriting the placeholder). The lamp recomputes it and drops
the frame on mismatch.

### Decryption (notify path)

When subscribing to the **notify** characteristic, incoming 20-byte
frames have the same structure but with the source = lamp mesh address.
Decrypt with the same `session_key`, verify MIC, parse opcode.

---

## 6. Mesh addressing

| Address | Meaning |
|---|---|
| `0x0000` | Unicast to the GATT-connected lamp itself ("self") |
| `0x0001..0x00FF` | Per-device unicast (each provisioned lamp has one — discoverable via opcode `0xDD`) |
| `0x8001..0x80FF` | Group addresses (a lamp can be member of multiple) |
| `0xFFFF` | **Broadcast** to every lamp on the mesh — what we use for sync control |

The Candelas were designed to act as a synchronised group ("rotate one,
all match"), so broadcasting `0xFFFF` is both natural and bandwidth-cheap:
1 BLE write → all N lamps react within ~10 ms.

Per-lamp control is *technically* possible by:

1. Sending `0xDD` (group/address query) and parsing the `0xDC` notify
   replies to learn each lamp's mesh address.
2. Writing commands with `dst = <that address>` instead of `0xFFFF`.

Not yet implemented in this integration.

---

## 7. Opcode table

Extracted in clear from `com.telink.bluetooth.light.Opcode` in the
decompiled APK. Only the ones relevant to Candelas are listed; the
class defines many more for other Yeelight devices.

| Opcode | Name | Params | Direction |
|---|---|---|---|
| `0xD0` | Power | `[0x01]` on, `[0x00]` off | TX |
| `0xD2` | Brightness | `[1..100]` (0 = no-op, treated as ignore) | TX |
| `0xE2` | Color RGB | `[R, G, B]` (Candelas accept it but render warm-white only) | TX |
| `0xF0` | Color temperature | `[CCT_low, CCT_high]` (1700–6500 K) | TX |
| `0xD7` | Group add/remove | `[group_addr_lo, group_addr_hi, mode]` | TX |
| `0xDA` | Status query | `[0x10]` for "all status" | TX |
| `0xDB` | Status response | `[on, brightness, ...]` | RX (notify) |
| `0xDC` | Online status | `[mesh_addr_lo, mesh_addr_hi, ...]` | RX (notify) |
| `0xDD` | Group/address query | — | TX → triggers `0xDC` |
| `0xEA` | User-all (combined state) | varies | TX/RX |
| `0xEE` | Scene op (CRUD) | `[scene_id, ...]` | TX |
| `0xEF` | Scene load | `[scene_id]` | TX |

Opcodes `0xC0..0xCF` and `0xF1..0xFF` exist but are device-specific
to non-Candela Yeelight products.

---

## 8. The vendor-bytes mystery

Bytes `[8..9]` of the command frame are the **vendor identifier**. Two
candidates exist in the wild:

| Bytes | Origin | Behaviour on Candela |
|---|---|---|
| `0x01 0x64` | Yeelight SDK-official (vendor_id `356` BE, matches the published Telink Mesh vendor allocation) | **Did NOT trigger any lamp action** in our tests |
| `0x64 0x01` | Awox / Eqiva / generic Telink-Light SDK (vendor_id `356` LE — same number, opposite endianness) | **Worked first try** — lamps reacted to power and brightness |

This integration uses `0x64 0x01` because it was empirically validated
on hardware. Why the SDK-official ordering doesn't work is unresolved —
possible hypotheses:

- The Candela firmware has an off-by-one / endianness bug in vendor
  parsing that Yeelight worked around in their app
- Different firmware revisions accept different orderings
- The Candela was forked from a generic Telink reference design before
  Yeelight standardised the byte order

If a future contributor sees lamps not responding while pair handshake
succeeds, this is the first thing to flip and re-test.

---

## 9. Connection lifecycle

The Candela firmware closes the GATT link **~30 s after the last
write**, regardless of:

- BLE link-layer keepalive (LL_PING)
- ATT keepalives (e.g. periodic notify reads)
- Phy params (interval, latency)

We tried all of the above empirically. The drop is firmware-side,
hard-coded. So our strategy is:

1. **Keepalive read** every 5 s on the notify characteristic — keeps
   the link warm against *transient* idle (the firmware does sometimes
   accept this as activity for short periods).
2. **Auto-reconnect** when the next command fails: re-pair (~10 s) then
   retry. From the user's perspective, the *first* command after a long
   idle costs ~10 s; subsequent commands are fast (~50 ms).

Throttling: the Candela rejects bursts of identical-opcode commands
under ~250 ms apart. We throttle to **320 ms** between same-opcode
writes.

---

## 10. Useful pointers

- **Decompile the APK yourself**:
  ```
  brew install jadx apktool
  jadx -d ./decompiled yeelight-X.Y.Z.apk
  ```
  Look in:
  - `com.telink.bluetooth.light.Opcode` — opcode constants in clear
  - `com.telink.bluetooth.light.Command` — frame builders
  - `com.telink.bluetooth.light.Mesh` — pair handshake, key derivation
  - `com.yeelight.cherry.network.MeshConfigManager` — the *rotation
    algorithm* and the cloud API endpoints used to push/fetch mesh
    credentials. **Important:** this gives you the *logic*, not the
    credentials of any specific lamp. The actual rotated credentials
    live in the Yeelight cloud (linked to the user's Yeelight account)
    and in the app's local cache
    (`/data/data/com.yeelight.cherry/shared_prefs/...`, root required).
    To recover credentials of an already-paired lamp you need the
    cache, the cloud account, or a factory reset — decompiling alone
    is insufficient.
- **Open-source Java reference**:
  [`zhourenjun/BleMeshLib`](https://github.com/zhourenjun/BleMeshLib) —
  a clean port of the Telink Light SDK that confirmed the opcode table
  and frame structure when we cross-checked the decompiled code.
- **Existing HA integration (different approach)**:
  [`hcoohb/hass-yeelightbt`](https://github.com/hcoohb/hass-yeelightbt) —
  uses one GATT connection per lamp, no mesh broadcast. Useful as a
  reference for the pair handshake details (it also does a
  byte-reverse-aware AES).

---

## 11. NOTIFY parsing — what we tried, why it doesn't work (yet)

State sync would let HA know the *real* lamp state (when someone turns
the dial physically, uses the Yeelight app, or another integration
acts on the same mesh). The Telink protocol exposes status via two
NOTIFY-only opcodes: `0xDB` (status response, sent in reply to a
`0xDA` query) and `0xDC` (online status report, broadcast by lamps
when their state changes).

We attempted to subscribe to GATT NOTIFY on the Yeelight Candela
firmware (YLFW01YL, late-2024 build) using bleak from inside HA core
on Raspberry Pi 5 / HAOS / bluez. **Every CCCD subscribe attempt
failed**, leaving us without a way to receive these frames. Summary:

| Approach | Result |
|---|---|
| `start_notify(NOTIFY_CHAR_UUID = 1911)` after pair | `[org.bluez.Error.Failed] ATT 0x0e (Unlikely Error)` |
| `start_notify(YEELIGHT_NOTIFY_CHAR_UUID = 8f65073d)` (Yeelight 0xfe87 service) after pair | same ATT 0x0e |
| Same, but BEFORE pair handshake | `[org.bluez.Error.NotPermitted] Notify acquired` |
| Same, with defensive `stop_notify` first | still NotPermitted / ATT 0x0e |
| **Polling** `read_gatt_char` on the notify chars after sending `0xDA` | both chars return a 1-byte constant (`01` / `00`), not the 20-byte status frame |
| Polling read on `COMMAND_CHAR_UUID = 1912` and other readable chars | 16-byte zeros / static bytes, never the status payload |

Hypotheses for why CCCD subscribe is blocked:

1. **HA's bluetooth integration acquires the notify chars itself** for
   passive scanning purposes, and bluez refuses a second consumer.
   `NotPermitted: Notify acquired` is the smoking gun.
2. **The Yeelight firmware uses a non-standard CCCD encoding** (e.g.,
   needs an authenticated/encrypted link before subscribe), which
   bluez's stock CCCD-write fails to satisfy. ATT 0x0e is consistent
   with this.
3. **Bluez has stale acquisition state** that survives across HA
   restarts and only clears on a host reboot. Confirmed: after enough
   failed attempts, even basic GATT writes start failing and only a
   `ha host reboot` recovers.

What we know works fine *without* NOTIFY:

- Pair handshake (read on `1914` after write — not via NOTIFY)
- All command writes (`OP_POWER`, `OP_BRIGHTNESS`, etc.)
- Multi-lamp broadcast on `0xFFFF`

Approaches tried, with results:

- **HA `bluetooth.async_register_callback`** for advertisement data
  (active scanning) — *tested, dead end*. The Yeelight Candelas only
  beacon their identity in BLE Adv: vendor `0x0164` + a 27-byte
  scan-response payload `64 01 d6 ae c0 41 01 00 00 d6 00 + ASCII
  "yl_candela" + zero-padding`. **Constant** across hours of
  observation, regardless of physical state changes. Status is not
  exposed via BLE Adv on this firmware.
- **`start_notify` retry on a freshly-rebooted Pi** — *tested, still
  hangs*. After a clean `ha host reboot` the bluez stack is fresh,
  but `await client.start_notify(...)` hangs (~30 s) before HA's
  setup-retry timer kicks in. Consistent across both candidate
  notify chars (Telink `1911` and Yeelight `8f65073d`). The hang
  likely reflects HA's bluetooth integration having acquired the
  char for its own use, then refusing a second consumer at the
  bluez level (NotPermitted: Notify acquired).

Approaches still worth trying for a future contributor:

- **Manual CCCD descriptor write** via `client.write_gatt_descriptor`
  with the `0x2902` CCCD descriptor + value `0x0001`, bypassing
  bleak's `start_notify` abstraction entirely. Hard rate-limit
  required (one attempt per HA restart cycle, no retry loops, or
  bluez state corrupts and only `ha host reboot` recovers).
- **A separate bleak process** outside HA's bluetooth integration's
  acquisition (would require coordinating BT chip access — not
  recommended for a HACS integration but may work for a one-shot
  proof-of-concept).
- **A different BLE adapter** (USB dongle) dedicated to this
  integration, leaving the on-board chip to HA's general scanner.
- **ESPHome BLE proxy** — a cheap ESP32 flashed with the bluetooth
  proxy firmware, advertised to HA. The ESP's BLE stack is not bluez;
  it doesn't have the acquisition issue. Same one-time hardware cost
  as a USB dongle (~5 €).

### Independent confirmation (`hcoohb/hass-yeelight-bt`)

The pre-existing alternative integration `hass-yeelight-bt` (which
uses the *Yeelight per-lamp* protocol on chars `aa7d3f34` /
`8f65073d`, not the Telink mesh protocol we use) hit **the exact
same bug** and explicitly skips `start_notify` for Candela on bluez:

```python
if self._model == MODEL_CANDELA and self._is_client_bluez:
    # It may be that on bluez the notification request is not
    # sent properly. Not sure on esp... so only apply to bluez
```

They write commands blindly and rely on optimistic state, exactly
like we do. This is a strong signal that the limitation is genuinely
fundamental to the **Candela firmware × bluez** combination, not
something we're missing in our implementation. ESP32 proxy is the
known-good workaround per their comment.

The official Yeelight Android app uses the standard Android
`setCharacteristicNotification` + `writeDescriptor(ENABLE_NOTIFICATION_VALUE)`
path (decompiled APK: `com.yeelight.yeelib.device.connections.ConnectionBase`)
— so there's nothing exotic on the lamp side. The breakage is
between bluez and the Candela firmware's CCCD handling.

---

Until any of those land, the integration uses **optimistic state**
with `_attr_assumed_state = True` (state shown in HA reflects the
last command we sent, not necessarily what the lamps physically
display) and flips to `state = unknown` whenever the GATT session
drops, so the user knows when the displayed state is unreliable.

The crypto + decode helper `_decrypt_notify()` in `mesh.py` is kept
in tree as it'll be needed the moment any of the above paths starts
returning bytes.

---

## 12. Things that are NOT possible (without new hardware)

- **Sending mesh frames without GATT pair** — would require TX'ing on
  Telink's 2.4 GHz proprietary mode, which standard BLE chips can't do.
  Candidate hardware: **nRF52840 dongle** (~30 €) flashed with custom
  firmware speaking the Telink mesh PHY. Significant effort, out of
  scope for this integration.
- **Sniffing inter-lamp mesh propagation** — same reason. Standard BLE
  sniffers see nothing on the air between lamps even when commands are
  flying. Confirmed empirically with `btmon` on a Pi 5 and an RFXtrx433
  general-purpose RF receiver (the latter is 433 MHz, not 2.4 GHz, so
  expected — but listed because the question came up).
- **Per-lamp control with the current integration** — possible but
  requires implementing the `0xDD` discovery + per-address write path.
  Patches welcome.

---

## 13. Glossary

- **LTK** — Long Term Key. The static 16-byte AES key derived from the
  factory mesh credentials. Rotated by the official app on first pair.
- **Session key** — Per-connection 16-byte AES key derived from LTK +
  random nonces from both sides during the pair handshake.
- **MIC** — Message Integrity Code. 2 bytes appended to each command
  frame (here stored at offset 4..5, not at the end as in textbook CCM).
- **Telink byte-reverse convention** — Telink's AES wrapper reverses
  key bytes and value bytes before/after the AES black box. Easy to
  miss; accounts for ~80 % of debugging time on a fresh impl.
- **Mesh broadcast (`0xFFFF`)** — The dst address that every node on
  the mesh accepts. The whole point of this integration.
