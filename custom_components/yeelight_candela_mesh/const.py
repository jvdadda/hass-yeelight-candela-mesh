"""Constants for the Yeelight Candela Mesh integration."""

DOMAIN = "yeelight_candela_mesh"

# Default Telink Mesh credentials hardcoded in Yeelight app (MeshNetWork.java)
# Used when the user hasn't re-paired the lamps under a custom mesh name via Mi Home/Yeelight app.
DEFAULT_MESH_NAME = "yeelight_ms"
DEFAULT_MESH_PASSWORD = "YLu2M80aE"

# Telink GATT service & characteristics (extracted from yeelight-3-5-4.apk UuidInformation.java)
TELINK_SERVICE_UUID = "00010203-0405-0607-0809-0a0b0c0d1910"
PAIR_CHAR_UUID = "00010203-0405-0607-0809-0a0b0c0d1914"
COMMAND_CHAR_UUID = "00010203-0405-0607-0809-0a0b0c0d1912"
NOTIFY_CHAR_UUID = "00010203-0405-0607-0809-0a0b0c0d1911"
OTA_CHAR_UUID = "00010203-0405-0607-0809-0a0b0c0d1913"

# Yeelight advertising service (presence beacon)
YEELIGHT_AD_SERVICE_UUID = "0000fe87-0000-1000-8000-00805f9b34fb"

# Telink Mesh opcodes (Opcode.java enum from Yeelight APK + BleMeshLib reference)
OP_POWER = 0xD0           # params [1] = on, [0] = off
OP_BRIGHTNESS = 0xD2      # params [bright_0_100, 0, 0]
OP_COLOR_RGB = 0xE2       # params [r, g, b] (Candela = white only, ignored)
OP_GROUP_ADD_RM = 0xD7    # add/remove from group
OP_STATUS_QUERY = 0xDA    # request status
OP_STATUS_RESPONSE = 0xDB # status reply
OP_ONLINE_STATUS = 0xDC   # online status report (notify char)
OP_GROUP_QUERY = 0xDD     # group_id query
OP_USER_ALL = 0xEA        # broadcast to all
OP_SCENE_OP = 0xEE        # scene operation
OP_SCENE_LOAD = 0xEF      # load scene

# Mesh addresses
ADDR_BROADCAST = 0xFFFF   # all nodes in mesh
ADDR_UNICAST_SELF = 0x0000 # the GATT-connected lamp only

# Telink Light vendor ID (Yeelight Bluetooth SIG company ID)
# Big-endian per BleMeshLib SDK: bytes [01, 64]
VENDOR_ID = 0x0164

# Throttle between same-opcode commands (BleMeshLib AdvanceStrategy default)
COMMAND_THROTTLE_MS = 320

# Connection management
PAIR_TIMEOUT_S = 15
RECONNECT_INTERVAL_S = 30
KEEPALIVE_INTERVAL_S = 5

# Config flow keys
CONF_MESH_NAME = "mesh_name"
CONF_MESH_PASSWORD = "mesh_password"
CONF_GATEWAY_MAC = "gateway_mac"
