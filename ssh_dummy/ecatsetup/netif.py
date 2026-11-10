"""Network interfaces: which one the master uses, and whether it can do
xdp-native.

Both are read without root: the master's MAC from the ethercat tool, the
interface with that MAC from /sys/class/net, and the driver's XDP features
from the kernel's netdev netlink family (Linux 6.3 and later).
"""

import os
import socket
import struct

from . import tool

SYS_NET = "/sys/class/net"

# generic netlink
NETLINK_GENERIC = 16
NLM_F_REQUEST = 1
NLMSG_ERROR = 2
GENL_ID_CTRL = 0x10
CTRL_CMD_GETFAMILY = 3
CTRL_ATTR_FAMILY_ID = 1
CTRL_ATTR_FAMILY_NAME = 2
NETDEV_CMD_DEV_GET = 1
NETDEV_A_DEV_IFINDEX = 1
NETDEV_A_DEV_XDP_FEATURES = 3
NETDEV_XDP_ACT_BASIC = 1     # XDP in the driver: what xdp-native needs


def interfaces():
    try:
        return sorted(n for n in os.listdir(SYS_NET) if n != "lo")
    except OSError:
        return []


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def by_mac(mac):
    """The interface with this MAC address, or None."""
    mac = (mac or "").lower()
    for name in interfaces():
        if mac and _read(os.path.join(SYS_NET, name, "address")) == mac:
            return name
    return None


def driver(name):
    """The interface's driver, such as igb or r8169, or None."""
    link = os.path.join(SYS_NET, name, "device", "driver")
    return os.path.basename(os.path.realpath(link)) if os.path.exists(link) else None


def master_interface():
    """The interface the running master uses, or None (no master, or not found)."""
    try:
        return by_mac(tool.master_mac())
    except tool.ToolError:
        return None


def _attr(kind, data):
    n = 4 + len(data)
    return struct.pack("HH", n, kind) + data + b"\0" * ((4 - n % 4) % 4)


def _attrs(buf):
    out = {}
    while len(buf) >= 4:
        n, kind = struct.unpack_from("HH", buf)
        if n < 4:
            break
        out[kind & 0x3fff] = buf[4:n]
        buf = buf[(n + 3) & ~3:]
    return out


def _request(sock, family, cmd, payload):
    body = struct.pack("BBH", cmd, 1, 0) + payload
    sock.send(struct.pack("IHHII", 16 + len(body), family, NLM_F_REQUEST, 1, 0) + body)
    data = sock.recv(65536)
    n, kind = struct.unpack_from("IH", data)
    if kind == NLMSG_ERROR:
        err = struct.unpack_from("i", data, 16)[0]
        if err:
            raise OSError(-err, os.strerror(-err))
    return _attrs(data[20:n])


def xdp_features(name):
    """The driver's XDP feature bits for an interface. Raises OSError or
    KeyError when the kernel cannot tell."""
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_GENERIC) as s:
        s.settimeout(1.0)
        s.bind((0, 0))
        family = _request(s, GENL_ID_CTRL, CTRL_CMD_GETFAMILY,
                          _attr(CTRL_ATTR_FAMILY_NAME, b"netdev\0"))
        fid = struct.unpack("H", family[CTRL_ATTR_FAMILY_ID][:2])[0]
        dev = _request(s, fid, NETDEV_CMD_DEV_GET,
                       _attr(NETDEV_A_DEV_IFINDEX, struct.pack("I", socket.if_nametoindex(name))))
        return struct.unpack("Q", dev[NETDEV_A_DEV_XDP_FEATURES][:8])[0]


def xdp_native(name):
    """True or False: whether the interface's driver does XDP itself, as
    xdp-native needs. None when it cannot be told (no such interface, or an
    older kernel)."""
    try:
        return bool(xdp_features(name) & NETDEV_XDP_ACT_BASIC)
    except (OSError, KeyError, struct.error, ValueError):
        return None


def default_transport(name):
    return "xdp-native" if name and xdp_native(name) else "raw"


def fill_master(cfg, interface, keep_transport=False):
    """With nothing else to go on, use the interface the bus was read
    through, and xdp-native if it can do it. Only a master without an
    interface is changed, and its transport only if it is raw and not
    keep_transport (given on purpose). Returns notes."""
    master = cfg.setdefault("master", {})
    if master.get("interface") or not interface:
        return []
    master["interface"] = interface
    note = "master: interface %s, as the bus was read through it" % interface
    if not keep_transport and master.get("transport", "raw") == "raw" and xdp_native(interface):
        master["transport"] = "xdp-native"
        note += "; transport xdp-native, as %s can do it" % (driver(interface) or interface)
    return [note]
