# Yeelight Candela Mesh — Home Assistant integration

Custom HACS integration for **Yeelight Candela** lamps (model `YLFW01YL`) that pilots the
whole mesh through a single GATT connection to one "gateway" lamp, instead of opening N
GATT connections (one per lamp) like the existing `hass-yeelightbt`.

> Status: **alpha** (0.1.0). Validated live on 2 Candelas (Pi 5 / HAOS).

## Why this exists

The Candela uses a proprietary **Telink Mesh** protocol (pre-SIG, vendored from Telink
TLSR8253 chip). Existing community plugins (`hcoohb/hass-yeelightbt` + forks) fall back
to "1 BLE connection per lamp", which:

- saturates the Pi's BT chip when you have ≥3 Candelas
- means lamps are NOT in sync (3 sequential commands ≠ 1 broadcast)
- multiplies the ~10 s pair latency by N

This integration:

- pairs **once** with one Candela (the "gateway")
- sends commands as **mesh broadcast** (`dst = 0xFFFF`) → all lamps on the same mesh
  network react in sync, in a single BLE write
- exposes a **single** `light.candela_mesh` entity covering the whole mesh

Reverse engineered from the official Yeelight Android APK (`com.telink.bluetooth.light.*`)
and validated empirically on hardware. See [PROTOCOL.md](PROTOCOL.md) for the
full write-up of the wire protocol, crypto, and BLE quirks — useful if you want
to extend this integration or build something similar from scratch.

## Hardware tested

- Raspberry Pi 5 8 GB / Home Assistant OS 2025.x
- 3× Yeelight Candela `YLFW01YL` (firmware 1.4.x, factory mesh credentials)
- Built-in Pi 5 Bluetooth controller (no external dongle needed)

## Installation (HACS, custom repository)

1. HACS → Integrations → ⋮ → *Custom repositories*
2. URL: `https://github.com/jvdadda/hass-yeelight-candela-mesh`, category: *Integration*
3. Install **Yeelight Candela Mesh** then restart Home Assistant
4. Settings → Devices & Services → *Add Integration* → "Yeelight Candela Mesh"

## Configuration

The integration auto-discovers Candelas via the `bluetooth` integration. You can also
add one manually (it scans BLE for names matching `yeelight_ms*` / `yl_candela*`).

You will be asked for:

| Field | Default | Notes |
|---|---|---|
| Gateway lamp | (closest by RSSI) | Any one Candela on the mesh — the others will follow |
| Mesh name | `yeelight_ms` | Factory default for never-paired-in-app lamps |
| Mesh password | `YLu2M80aE` | Factory default |

> If your lamps were ever paired in the **Mi Home** or **Yeelight** app, the app
> rotated the mesh credentials. See *Recovering custom mesh credentials* below.

## What works

- `light.turn_on` / `light.turn_off` (broadcast, all lamps in sync)
- `brightness` (HA 0–255 mapped to Telink 1–100)
- Auto-reconnect when the firmware drops the GATT link (~30 s timeout — known firmware
  behaviour, not a bug)
- 5 s keepalive read on the notify characteristic to keep the link warm
- Bluetooth auto-discovery in the config flow

## What does not work yet

- **Per-lamp control** — the mesh address of each individual Candela is discoverable
  via opcode `0xDD` but not yet implemented. Only group broadcast for now.
- **Color temperature** (opcode `0xF0`) — Candelas are warm-white only (~1800 K), but
  there's a CCT register; not exposed yet.
- **Scenes** (opcodes `0xEE` / `0xEF`) — wired in const but not surfaced.
- **State feedback from lamps** — the integration optimistically updates state; no
  NOTIFY parsing yet.

## Recovering custom mesh credentials

If the factory defaults don't pair, your mesh was rotated by the Yeelight / Mi Home
app the first time you used it. Realistic options:

1. **Factory reset** the lamps (5× quick on/off cycles, ~1 s each, until a long blink),
   then pair fresh with this integration *before* re-opening the Yeelight app on those
   lamps. **This is the path that actually works for most users.**
2. Recover the rotated credentials from the Yeelight app's local cache (requires a
   rooted Android device + extracting `SharedPreferences` / sqlite from
   `/data/data/com.yeelight.cherry/`), or by replaying the Yeelight cloud API call
   the app makes at startup (requires bypassing cert pinning + having the original
   Yeelight account that paired the lamps). Decompiling the APK alone is **not**
   enough — it gives you the rotation logic but not the credentials of any specific
   lamp. Out of scope for this README.

## Known limitations

- The Candela firmware closes the GATT link after ~30 s of inactivity regardless of
  keepalive, so the **first command after a drop costs ~10 s** (re-pair). After that,
  commands are fast (~50 ms).
- Mesh propagation between lamps does NOT use standard BLE Advertising — it likely
  uses a 2.4 GHz proprietary radio mode of the Telink chip. This means a "second
  gateway" approach (sniff + replay frames from a Pi without any pair) is **not
  feasible** without specialised hardware (e.g. nRF52840 dongle).
- One config entry = one mesh network. If you have multiple disjoint mesh networks
  (different `mesh_name`), add one config entry per network.

## Credits

This work stands on the shoulders of:

- [`hcoohb/hass-yeelightbt`](https://github.com/hcoohb/hass-yeelightbt) and the
  [`stast1`](https://github.com/stast1/hass-yeelightbt) fork — the GATT-per-lamp
  reference, where I borrowed the BLE handshake structure
- [`zhourenjun/BleMeshLib`](https://github.com/zhourenjun/BleMeshLib) — open-source
  Java port of the Telink Light SDK that confirmed the opcode table
- The Yeelight Android app itself, which decompiled cleanly with `jadx` and exposed
  `com.telink.bluetooth.light.Opcode` in the clear

## License

MIT — see [LICENSE](LICENSE).
