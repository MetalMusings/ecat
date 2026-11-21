"""The ecat configuration file (ethercat.yaml): make it from proposals, write
it, read it back and check it.

The format is described in docs/component.md. The file is plain YAML
that the setup program writes; numbers that are EtherCAT identifiers
(vendor, product, object indexes) are kept as "0x..." strings so that they
read as they do in vendor documentation.
"""

import ipaddress
import json
import re
import subprocess

import yaml

from . import quirks
from .propose import CIA402_PROFILE, CIA402_MODES as CIA402_NEEDS, ENCODER_INPUTS, ENCODER_OUTPUTS, PP_LIMITS, \
    PP_REQUIRED

FORMAT_VERSION = 1

HEADER = """\
# ecat configuration, written by EtherCAT Setup.
# Read by the ecat HAL component: loadrt ecat config=<this file>
# Format: https://github.com/MetalMusings/ecat/blob/main/docs/component.md#the-configuration-file
# The program rewrites this file; comments added by hand are not kept.
"""

PROFILES = {402: "cia402", 5001: "mdp"}
DATA_TYPES = {"BOOL", "SINT", "INT", "DINT", "LINT", "USINT", "UINT", "UDINT",
              "ULINT", "REAL", "LREAL"}
# LinuxCNC 2.10's pin types. A file written for 2.9 spells them bit, s32, u32,
# float: they are read as the same types and written with the new names.
HAL_TYPES = {"bool", "sint", "uint", "real"}
HAL_ALIASES = {"bit": "bool", "s32": "sint", "u32": "uint", "float": "real"}
# 64-bit integer entries (LINT, ULINT): an integer pin of the same sign has
# all 64 bits; a real pin carries them up to 2^53
HAL_64 = {"LINT": "sint", "ULINT": "uint"}
# a drive with several axes (cia402.axes): axis n's objects at index + n * 0x800
AXIS_OFFSET = 0x800
MAX_AXES = 4
CIA402_MODES = {"csp", "csv", "pv", "pp"}
# other modes a cia402 drive may switch to (cia402.modes); they need the
# mode objects to change modes
CIA402_OTHER_MODES = {"hm", "csp", "csv", "pv", "pp"}
CIA402_MODE_OBJECTS = {0x6060, 0x6061}
TRANSPORTS = {"raw", "xdp-native", "xdp-skb"}

_OBJECT = re.compile(r"^0x([0-9a-fA-F]{4}):([0-9a-fA-F]{2})$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*(\.[A-Za-z0-9][A-Za-z0-9_-]*)*$")
INTEGER_TYPES = DATA_TYPES - {"BOOL", "REAL", "LREAL"}


def hex32(v):
    return "0x%08X" % v


def obj(index, subindex):
    return "0x%04X:%02X" % (index, subindex)


# the encoder function's pins and parameters, after "<slave>.<channel>."
ENCODER_PINS = ("reset", "counts", "raw-count", "pos", "velocity", "ok", "index-enable",
                "latch-enable-pos", "latch-enable-neg", "latch-pos")

# the serial function (component/devices/ecat_serial.c): pins after
# "<slave>.<channel>.", the baud rates and data frames of the terminals'
# CoE 0x8000:11 and :15, the most channels per slave
SERIAL_PINS = ("state", "tx-bytes", "rx-bytes", "parity-errors", "framing-errors", "overrun-errors",
               "buffer-full", "baud", "burst-timeouts", "settings-error", "port-open")
# (Beckhoff's manual EL600x, EL602x: 300-1200 on the EL6002/EL6022, 12000
# and 14400 on the EL6001/EL6021 from firmware 07)
SERIAL_BAUDS = {300: 1, 600: 2, 1200: 3, 2400: 4, 4800: 5, 9600: 6, 12000: 14, 14400: 15,
                19200: 7, 38400: 8, 57600: 9, 115200: 10}
SERIAL_FRAMES = {"7E1": 1, "7O1": 2, "8N1": 3, "8E1": 4, "8O1": 5, "7N2": 8, "7E2": 9, "7O2": 10,
                 "8N2": 11, "8E2": 12, "8O2": 13}
SERIAL_MAX_CHANNELS = 8
SERIAL_MAX_DATA = 22
SERIAL_SUB_BAUD, SERIAL_SUB_FRAME, SERIAL_SUB_CONTINUOUS = 0x11, 0x15, 0x04


def parse_obj(text):
    """"0x6040:00" -> (0x6040, 0)."""
    m = _OBJECT.match(str(text))
    if not m:
        raise ValueError("not an object reference: %r" % text)
    return int(m.group(1), 16), int(m.group(2), 16)


# the master's state change timeouts (ETG.2000, master/fsm_change.c), ms: a slave's
# state-timeouts-ms gives longer ones where its ESI file asks for them
STATE_TIMEOUTS = {"preop": 3000, "safeop-op": 10000, "back-to-init": 5000, "back-to-safeop": 200}


# the time the master gives a slave to answer an SDO (master/sdo_request.h), ms
SDO_TIMEOUT = 1000


def longer_timeouts(esi_timeouts):
    """The ESI file's state timeouts that are longer than the master's defaults."""
    return {k: v for k, v in (esi_timeouts or {}).items()
            if k in STATE_TIMEOUTS and isinstance(v, int) and v > STATE_TIMEOUTS[k]}


# the cia402 modes' numbers in 0x6060 (propose.CIA402_MODE_NUMBERS)
MODE_NUMBERS = {"pp": 1, "pv": 3, "hm": 6, "csp": 8, "csv": 9}


def mode_setting(slave, axis=0):
    """The mode a drive is put in at start (0x6060:00 for an axis): a setting's value, else
    an init-cmds row's (from: profile is cia402.mode's number); None without either."""
    want = (0x6060 + axis * AXIS_OFFSET, 0)
    for st in slave.get("settings") or []:
        try:
            if parse_obj(st.get("object")) == want and "value" in st:
                return int(st["value"])
        except (ValueError, TypeError):
            pass
    for r in slave.get("init-cmds") or []:
        try:
            if not isinstance(r, dict) or r.get("disabled") or parse_obj(r.get("object")) != want:
                continue
        except ValueError:
            continue
        if r.get("from") == "profile":
            return MODE_NUMBERS.get((slave.get("cia402") or {}).get("mode"))
        if "value" in r:
            try:
                return int(r["value"])
            except (ValueError, TypeError):
                return None
    return None


INIT_AT = ("IP", "PS", "IP,PS")
INIT_FROM = ("profile", "cycle-time")


def init_cmd_errors(slave):
    """What is wrong with a slave's init-cmds (as ecat_config.c checks them)."""
    rows = slave.get("init-cmds")
    if rows is None:
        return []
    if not isinstance(rows, list):
        return ["init-cmds must be a list"]
    out = []
    for r in rows:
        if not isinstance(r, dict):
            out.append("init-cmds: each row is a mapping (at, object, ...)")
            continue
        o = r.get("object")
        try:
            index, sub = parse_obj(o)
        except ValueError:
            out.append("init-cmds: %r is not an object such as 0x8000:01" % (o,))
            continue
        if r.get("disabled") is True:
            continue
        if r.get("at") not in INIT_AT:
            out.append("init-cmds %s: at %s is not supported yet (ecat writes them in PREOP, before SAFEOP: "
                       "IP and PS)" % (o, r.get("at") or "(missing)"))
        f = r.get("from")
        if f is not None:
            if f not in INIT_FROM:
                out.append("init-cmds %s: from must be profile or cycle-time" % o)
            elif f == "profile" and not (slave.get("profile") == "cia402" and sub == 0
                                         and (index - 0x6060) % AXIS_OFFSET == 0 and 0x6060 <= index < 0x8000):
                out.append("init-cmds %s: from: profile is only for a cia402 drive's 0x6060:00" % o)
        elif "value" in r:
            if r.get("type") not in DATA_TYPES:
                out.append("init-cmds %s: unknown type %r" % (o, r.get("type")))
        elif "data" in r:
            try:
                bytes.fromhex(str(r["data"]))
            except ValueError:
                out.append("init-cmds %s: data %r is not hex bytes" % (o, r["data"]))
        else:
            out.append("init-cmds %s: give value and type, data, or from" % o)
        if r.get("complete") and sub > 1:
            out.append("init-cmds %s: complete access starts at subindex 0 or 1" % o)
    return out


def sync_mode_setting(slave):
    """After a change of cia402.mode: the setting 0x6060:00 of each axis, where there is
    one, says the new mode."""
    c = slave.get("cia402") or {}
    if slave.get("profile") != "cia402" or c.get("mode") not in MODE_NUMBERS:
        return
    axes = len(c.get("axes") or [1])
    for st in slave.get("settings") or []:
        try:
            index, sub = parse_obj(st.get("object"))
        except ValueError:
            continue
        if sub == 0 and any(index == 0x6060 + a * AXIS_OFFSET for a in range(axes)):
            st["value"] = MODE_NUMBERS[c["mode"]]
            st["type"] = st.get("type") or "SINT"


def default_name(p):
    kind = {"cia402": "drive", "mdp": "io"}.get(PROFILES.get(p.profile), "slave")
    return "%s-%d" % (kind, p.position)


def _entry(e):
    d = {"object": obj(e.index, e.subindex), "bits": e.bitlen}
    if e.role == "gap":
        d["use"] = "gap"
        return d
    d["type"] = e.data_type
    if e.role in ("profile", "encoder", "serial"):
        d["use"] = "profile"
    elif getattr(e, "split", None):
        d["split"] = list(e.split)
    elif e.pin:
        d["pin"] = e.pin
        d["hal-type"] = e.hal_type
        if getattr(e, "scale", None) is not None:
            d["scale"] = e.scale
        if getattr(e, "offset", None):
            d["offset"] = e.offset
        if getattr(e, "extend", False):
            d["extend"] = True
        if getattr(e, "raw", False):
            d["raw"] = True
    return d


def slave_from_proposal(p, name=None):
    s = {
        "position": p.position,
        "name": name or default_name(p),
        "device": p.name,
        "identity": {"vendor": hex32(p.vendor_id), "product": hex32(p.product_code),
                     "revision": hex32(p.revision)},
        "profile": PROFILES.get(p.profile, "generic"),
    }
    if getattr(p, "modules", None) is not None:
        s["modules"] = [hex32(m) for m in p.modules]
    if p.profile == 402:
        s["cia402"] = {"mode": p.mode}
        if getattr(p, "axes", 1) > 1:
            s["cia402"]["axes"] = ["%s-%d" % (s["name"], n + 1) for n in range(p.axes)]
    if p.encoders:
        s["encoders"] = [{"name": n, "index": "0x%04X" % i} for n, i in p.encoders]
    if getattr(p, "serial_channels", None):
        # a port per channel: /dev/ttyECAT<position>, -<n> with several
        s["serial"] = [{"name": n, "output-pdo": "0x%04X" % o, "input-pdo": "0x%04X" % i,
                        "port": "/dev/ttyECAT%d" % p.position if len(p.serial_channels) == 1
                        else "/dev/ttyECAT%d-%d" % (p.position, k + 1),
                        "baud": 9600, "frame": "8N1"}
                       for k, (n, o, i) in enumerate(p.serial_channels)]
    pd = {"outputs": [], "inputs": []}
    for c in p.pdos:
        pd["outputs" if c.direction == "rx" else "inputs"].append(
            {"pdo": "0x%04X" % c.index, "sm": c.sm, "entries": [_entry(e) for e in c.entries]})
    s["configure-pdos"] = True
    s["process-data"] = {k: v for k, v in pd.items() if v}
    if p.dc_assign is not None:
        s["dc"] = {"assign-activate": "0x%04X" % p.dc_assign,
                   "sync0-cycles": p.dc_sync0_factor, "sync0-shift-ns": p.dc_shift}
        if getattr(p, "dc_sync1_cycles", 0):
            s["dc"]["sync1-cycles"] = p.dc_sync1_cycles
            s["dc"]["sync1-shift-ns"] = p.dc_sync1_shift
    s["settings"] = [{"object": obj(i, sub), "type": t, "value": p.pp_limits[(i, sub)]}
                     for i, sub, t in PP_LIMITS if (i, sub) in p.pp_limits]
    if getattr(p, "state_timeouts", None):
        s["state-timeouts-ms"] = dict(p.state_timeouts)
    if getattr(p, "sdo_timeout", None):
        s["sdo-timeout-ms"] = p.sdo_timeout
    # what the device needs written at start (ESI InitCmds, the drive's mode)
    # what the device's description writes at start (its InitCmds, 0xF030, the drive's mode)
    if getattr(p, "init_cmds", None):
        s["init-cmds"] = [dict(r) for r in p.init_cmds]
    # a quirk file's settings for this device (one saved from an earlier slave)
    have = {parse_obj(st["object"]) for st in s["settings"]}
    for st in quirks.settings((p.vendor_id, p.product_code)):
        if parse_obj(st["object"]) not in have:
            s["settings"].append(dict(st))
    return s


def from_proposals(proposals, interface=None, transport="raw", servo_period=None):
    master = {}
    if interface:
        master["interface"] = interface
    master["transport"] = transport
    slaves = [slave_from_proposal(p) for p in proposals]
    if any("dc" in s for s in slaves):
        # Without these the DC slaves' clocks are not kept in step with the
        # servo thread. The thread follows the reference clock, and the
        # component places SYNC0 where the turn-around is the same in every
        # cycle (sync0-shift-ns: auto).
        master["dc"] = {"ref-clock-sync-cycles": 1000, "servo-thread-follows-ref-clock": True,
                        "sync0-shift-ns": "auto"}
    return {"ecat": FORMAT_VERSION, "master": master, "slaves": slaves}


# master.dc.ref-clock-sync-cycles with servo-thread-follows-ref-clock: false:
# at most this, and what the window sets when false is chosen. Measured
# 2026-11-14 (docs/development.md "DC clock variants"): at 1 or 100 cycles
# the slaves' clock stayed within about a microsecond of the servo thread,
# at 1000 it wandered by microseconds within two minutes.
REF_SYNC_MAX = 100


def ref_clock(cfg):
    """The slave (its dict) that the component makes the DC reference clock:
    after ETG.1020 (21.1), "the first device that should be synchronized",
    the used slave with dc: (assign-activate not 0x0000) that comes first on
    the bus. None when no slave has DC (the master's own choice then)."""
    best = None
    for s in (cfg or {}).get("slaves") or []:
        dc = s.get("dc")
        if s.get("unused") or not isinstance(dc, dict) or not isinstance(s.get("position"), int):
            continue
        if _assign_value(dc.get("assign-activate")) == 0:
            continue
        if best is None or s["position"] < best["position"]:
            best = s
    return best


def _assign_value(v):
    """assign-activate as a number, read as the component reads it (strtoll
    with base 0: 0x hex, a leading 0 octal); 0 when missing, None when
    unreadable."""
    if v is None:
        return 0
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    t = str(v).strip()
    try:
        if t.lower().startswith(("0x", "-0x", "+0x")):
            return int(t, 16)
        if len(t.lstrip("+-")) > 1 and t.lstrip("+-").startswith("0"):
            return int(t, 8)
        return int(t, 10)
    except ValueError:
        return None


def dc_warnings(cfg):
    """Texts about DC settings that work but are not advised."""
    out = []
    for s in (cfg or {}).get("slaves") or []:
        dc = s.get("dc") if isinstance(s, dict) else None
        if isinstance(dc, dict) and _assign_value(dc.get("assign-activate")) & 0x0400 and not dc.get("sync1-cycles"):
            out.append("slave %s (%s): assign-activate %s turns SYNC1 on, but no sync1-cycles is given: SYNC1 "
                       "then has no cycle of its own and the slave may refuse SAFEOP (AL 0x0030); give sync1-cycles "
                       "(SYNC1 every n SYNC0 cycles, from the DC mode in the ESI file)"
                       % (s.get("position"), s.get("name"), dc.get("assign-activate")))
    return out + _master_dc_warnings(cfg)


def _master_dc_warnings(cfg):
    mdc = ((cfg or {}).get("master") or {}).get("dc")
    if not isinstance(mdc, dict) or mdc.get("servo-thread-follows-ref-clock") is True:
        return []
    n = mdc.get("ref-clock-sync-cycles")
    if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= REF_SYNC_MAX:
        return []
    return ["master.dc: with servo-thread-follows-ref-clock: false the reference clock should be "
            "synchronised every 1 to %d cycles (ref-clock-sync-cycles is %s): less often, the "
            "slaves' clock wanders against the servo thread. Or use "
            "servo-thread-follows-ref-clock: true" % (REF_SYNC_MAX, "not set" if n is None else n)]


EOE_KEYS = ("ip", "mask", "gateway", "dns", "hostname", "mac", "host-ip")


FEEDFORWARD_KEYS = {"velocity": 0x60B1, "torque": 0x60B2, "torque-max": None}


def feedforward_errors(c, other_modes, owned, n_axes):
    """What is wrong with cia402.feedforward (as ecat_config.c checks it)."""
    ff = c.get("feedforward")
    if ff is None:
        return []
    if not isinstance(ff, dict):
        return ["cia402.feedforward needs velocity, torque and/or torque-max"]
    out = ["cia402.feedforward: unknown key %r (velocity, torque, torque-max)" % k
           for k in ff if k not in FEEDFORWARD_KEYS]
    for k, index in FEEDFORWARD_KEYS.items():
        if k not in ff:
            continue
        v = ff[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            out.append("cia402.feedforward.%s: %r is not a number" % (k, v))
            continue
        if k == "torque-max" and not 0 <= v <= 300:
            out.append("cia402.feedforward.torque-max: 0 to 300 (% of rated torque)")
        if index is not None:
            for a in range(n_axes):
                if index + a * AXIS_OFFSET not in owned:
                    out.append("cia402.feedforward.%s needs 0x%04X:00 (%s) with use: profile in the "
                               "outputs" % (k, index + a * AXIS_OFFSET,
                                            "velocity offset" if k == "velocity" else "torque offset"))
    if (set(ff) & {"velocity", "torque"}) and "csp" not in [c.get("mode")] + list(other_modes):
        out.append("cia402.feedforward works in CSP only: csp must be the mode or one of the modes")
    return out


def eoe_errors(eoe):
    """Errors in a slave's eoe: block (the same rules as component/ecat_config.c)."""
    if eoe is None:
        return []
    if not isinstance(eoe, dict):
        return ["eoe: must be a mapping (ip, mask, gateway, dns, hostname, mac, host-ip)"]
    out = ["eoe: unknown key %r (ip, mask, gateway, dns, hostname, mac, host-ip)" % k for k in eoe if k not in EOE_KEYS]

    def addr(key, text, prefix=False):
        try:
            if prefix and "/" in str(text):
                return ipaddress.IPv4Interface(str(text))
            return ipaddress.IPv4Address(str(text))
        except ValueError:
            out.append("eoe.%s: %r is not an IPv4 address%s" % (key, text, " (such as 192.168.10.21/24)" if prefix else ""))
        return None

    if "ip" not in eoe:
        out.append("eoe: needs ip (such as 192.168.10.21/24)")
    else:
        ip = addr("ip", eoe["ip"], True)
        if ip is not None and "mask" in eoe and "/" in str(eoe["ip"]):
            out.append("eoe: give the subnet as ip/prefix or as mask, not both")
    if "mask" in eoe:
        m = addr("mask", eoe["mask"])
        if m is not None:
            v = int(m)
            if v == 0 or (~v & 0xFFFFFFFF) & ((~v & 0xFFFFFFFF) + 1):
                out.append("eoe.mask: %r is not a subnet mask" % (eoe["mask"],))
    for k in ("gateway", "dns"):
        if k in eoe:
            addr(k, eoe[k])
    if "hostname" in eoe and not (isinstance(eoe["hostname"], str) and 0 < len(eoe["hostname"]) < 32):
        out.append("eoe.hostname: %r is empty or longer than 31 characters" % (eoe["hostname"],))
    if "mac" in eoe and isinstance(eoe["mac"], int):
        # YAML reads 12:00:00:00:10:21 without quotes as one number
        out.append("eoe.mac: write the MAC address in quotes (\"02:00:00:00:10:21\")")
    elif "mac" in eoe and not re.fullmatch(r"[0-9A-Fa-f]{1,2}(:[0-9A-Fa-f]{1,2}){5}", str(eoe["mac"])):
        out.append("eoe.mac: %r is not a MAC address such as 02:00:00:00:10:21" % (eoe["mac"],))
    if "host-ip" in eoe:
        before = len(out)
        host = addr("host-ip", eoe["host-ip"])
        net = eoe_network(eoe) if before == len(out) else None
        if host is not None and net is not None:
            if "/" not in str(eoe.get("ip")) and "mask" not in eoe:
                out.append("eoe.host-ip: needs the subnet: give ip with a prefix length (such as 192.168.10.21/24) or mask")
            elif net.network.prefixlen in (0, 32):
                out.append("eoe.host-ip: the subnet has room for one address only")
            elif host == net.ip:
                out.append("eoe.host-ip: %r is the slave's own address" % (eoe["host-ip"],))
            elif host not in net.network:
                out.append("eoe.host-ip: %r is not in the slave's subnet" % (eoe["host-ip"],))
    return out


def eoe_network(eoe):
    """The slave's subnet from an eoe: block, an IPv4Interface, or None."""
    try:
        ip = str(eoe["ip"])
        if "/" not in ip and eoe.get("mask"):
            return ipaddress.IPv4Interface("%s/%s" % (ip, eoe["mask"]))
        return ipaddress.IPv4Interface(ip)
    except (KeyError, ValueError, TypeError):
        return None


def eoe_device(slave):
    """The name of the network device the master makes for the slave."""
    return "eoe0s%s" % slave.get("position")


def eoe_suggestion(cfg, slave, networks=()):
    """An eoe: block to start from: the slave's address and the computer's
    in the first 192.168.N.0/24 (N from 10) that no other slave's EoE and
    none of the computer's own networks (`networks`, from host_networks())
    is in."""
    taken = [n for _, n in networks]
    for s in (cfg or {}).get("slaves") or []:
        net = eoe_network(s["eoe"]) if s is not slave and isinstance(s.get("eoe"), dict) else None
        if net is not None:
            taken.append(net.network)
    for n in list(range(10, 255)) + list(range(0, 10)):
        net = ipaddress.IPv4Network("192.168.%d.0/24" % n)
        if not any(net.overlaps(t) for t in taken):
            return {"ip": "192.168.%d.2/24" % n, "host-ip": "192.168.%d.1" % n}
    return {"ip": "192.168.10.2/24", "host-ip": "192.168.10.1"}


def host_networks(run=subprocess.run):
    """This computer's own IPv4 networks, [(device, IPv4Network)], without
    the master's EoE devices (eoe...) and the loopback. [] if `ip` fails."""
    try:
        text = run(["ip", "-j", "-4", "addr"], capture_output=True, text=True, timeout=5).stdout
        devices = json.loads(text)
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    out = []
    for d in devices if isinstance(devices, list) else []:
        name = str(d.get("ifname", ""))
        if name == "lo" or name.startswith("eoe"):
            continue
        for a in d.get("addr_info") or []:
            try:
                out.append((name, ipaddress.IPv4Interface("%s/%s" % (a["local"], a["prefixlen"])).network))
            except (KeyError, ValueError):
                pass
    return out


def eoe_warnings(cfg, networks=()):
    """Texts about EoE settings that are valid but probably wrong: the same
    address twice, a gateway outside the slave's subnet, and for host-ip:
    two network devices in one subnet, a subnet the computer already has
    (`networks`, from host_networks())."""
    out, seen, hosts = [], {}, []
    for s in (cfg or {}).get("slaves") or []:
        eoe = s.get("eoe")
        if not isinstance(eoe, dict) or eoe_errors(eoe):
            continue
        who = "slave %s (%s)" % (s.get("position"), s.get("name"))
        net = eoe_network(eoe)
        if net is None:
            continue
        if net.ip in seen:
            out.append("%s: eoe ip %s is also %s's" % (who, net.ip, seen[net.ip]))
        seen[net.ip] = who
        if "gateway" in eoe and ipaddress.IPv4Address(str(eoe["gateway"])) not in net.network and net.network.prefixlen < 32:
            out.append("%s: eoe gateway %s is not in the slave's subnet %s" % (who, eoe["gateway"], net.network))
        if "host-ip" in eoe:
            for other, other_net in hosts:
                if net.network.overlaps(other_net):
                    out.append("%s: eoe host-ip is in %s, as %s's: each slave has its own network device on the "
                               "computer, and two in one subnet reach only one of the slaves. Give each slave "
                               "its own subnet" % (who, net.network, other))
            hosts.append((who, net.network))
            for device, own in networks:
                if net.network.overlaps(own):
                    out.append("%s: eoe subnet %s is also the computer's own on %s (%s): with host-ip the "
                               "computer would have two ways into it, and its network may stop working. "
                               "Use a subnet of its own for the slave" % (who, net.network, device, own))
    for s in (cfg or {}).get("slaves") or []:
        eoe = s.get("eoe")
        if isinstance(eoe, dict) and not eoe_errors(eoe) and "host-ip" in eoe:
            host = ipaddress.IPv4Address(str(eoe["host-ip"]))
            if host in seen and seen[host] != "slave %s (%s)" % (s.get("position"), s.get("name")):
                out.append("slave %s (%s): eoe host-ip %s is %s's address" % (s.get("position"), s.get("name"), host, seen[host]))
    return out


# RS422/RS485 terminals (Beckhoff EL6021, EL6022): for two-wire RS485 they
# need "Enable half duplex", or they hear what they send themselves
SERIAL_RS485 = {(0x2, 0x17853052), (0x2, 0x17863052)}


def serial_notes(cfg):
    """Notes about serial channels that may be right, but often are not."""
    out = []
    for s in (cfg or {}).get("slaves") or []:
        ident = s.get("identity") or {}
        try:
            key = (int(str(ident.get("vendor")), 16), int(str(ident.get("product")), 16))
        except ValueError:
            continue
        if key not in SERIAL_RS485:
            continue
        set_objects = set()
        for st in s.get("settings") or []:
            try:
                if st.get("value"):
                    set_objects.add(parse_obj(st.get("object")))
            except ValueError:
                pass
        for k, ch in enumerate(s.get("serial") or []):
            try:
                index = int(str(ch.get("settings-index", "0x%04X" % (0x8000 + 0x10 * k))), 16)
            except ValueError:
                continue
            if (index, 0x06) not in set_objects:
                out.append("slave %s (%s): serial channel %s has no half duplex (0x%04X:06): right "
                           "for RS422; for two-wire RS485 tick it, or the terminal hears what it "
                           "sends itself" % (s.get("position"), s.get("name"), ch.get("name"), index))
    return out


class _Dumper(yaml.SafeDumper):
    pass


def _flow_entries(dumper, data):
    # One line per PDO entry or setting: short dicts in flow style.
    flow = set(data) <= {"object", "bits", "type", "use", "pin", "hal-type", "split",
                         "invert", "scale", "offset", "name", "value", "data",
                         "vendor", "product", "revision", "joint", "slave"}
    return dumper.represent_mapping("tag:yaml.org,2002:map", data, flow_style=flow)


_Dumper.add_representer(dict, _flow_entries)


def dumps(cfg):
    return HEADER + yaml.dump(cfg, Dumper=_Dumper, sort_keys=False, width=100,
                              default_flow_style=False)


def write(cfg, path):
    errors = check(cfg)
    if errors:
        raise ValueError("not written, the configuration has errors:\n  " + "\n  ".join(errors))
    with open(path, "w") as f:
        f.write(dumps(cfg))


def canonical_hal_types(cfg):
    """Spell every hal-type with 2.10's names (in place); returns cfg."""
    for s in (cfg or {}).get("slaves") or []:
        for kind in ("outputs", "inputs"):
            for pdo in (s.get("process-data") or {}).get(kind) or []:
                for e in pdo.get("entries") or []:
                    if e.get("hal-type") in HAL_ALIASES:
                        e["hal-type"] = HAL_ALIASES[e["hal-type"]]
    return cfg


def load(path):
    with open(path) as f:
        return canonical_hal_types(yaml.safe_load(f))


def axes(slave):
    """The pin prefixes of a cia402 slave's axes: its cia402.axes, or its
    own name for a drive with one axis; [] for other slaves."""
    if (slave or {}).get("profile") != "cia402":
        return []
    names = ((slave.get("cia402") or {}).get("axes"))
    return list(names) if isinstance(names, list) and names else [slave.get("name")]


def axis_of(cfg, prefix):
    """(slave, axis number) of a cia402 axis's pin prefix, or (None, None)."""
    for s in (cfg or {}).get("slaves") or []:
        for n, a in enumerate(axes(s)):
            if a == prefix:
                return s, n
    return None, None


def check(cfg, series=None):
    """Errors in a configuration, as readable strings; empty if it is fine.
    series: the LinuxCNC series it is checked for, (2, 9) or (2, 10); the one
    in use when None (lcnc_env.series)."""
    from . import lcnc_env
    series = series or lcnc_env.series()
    wide = lcnc_env.wide_pins(series)
    series_text = "%d.%d" % series
    errors = []
    err = errors.append
    if not isinstance(cfg, dict):
        return ["the file is not a YAML mapping"]
    if cfg.get("ecat") != FORMAT_VERSION:
        err("ecat: format version %r, expected %d" % (cfg.get("ecat"), FORMAT_VERSION))
    master = cfg.get("master") or {}
    if master.get("transport", "raw") not in TRANSPORTS:
        err("master: unknown transport %r" % master.get("transport"))
    if not isinstance(master.get("start-check", True), bool):
        err("master.start-check: must be true or false")
    for key in ("irq-cpu", "cpu"):
        v = master.get(key, -1)
        if isinstance(v, bool) or not isinstance(v, int) or not -1 <= v <= 1023:
            err("master.%s: %r is not a CPU number (0 and up, or -1 to leave it)" % (key, v))
    mdc = master.get("dc") or {}
    if "sync-to-ref-clock" in mdc:
        err("master.dc.sync-to-ref-clock is now called servo-thread-follows-ref-clock")
    shift = mdc.get("sync0-shift-ns")
    if shift == "auto":
        # the component moves the servo thread against SYNC0
        if mdc.get("servo-thread-follows-ref-clock") is not True:
            err("master.dc.sync0-shift-ns: auto needs master.dc.servo-thread-follows-ref-clock: true")
    elif shift is not None and (not isinstance(shift, int) or isinstance(shift, bool)):
        err("master.dc.sync0-shift-ns: %r is not a number or auto" % shift)
    slaves = cfg.get("slaves")
    if not isinstance(slaves, list) or not slaves:
        return errors + ["slaves: none"]

    positions, names, pins, axis_names, ports = set(), set(), set(), set(), set()
    for i, s in enumerate(slaves):
        where = "slave %s" % s.get("position", "#%d" % i)
        pos, name = s.get("position"), s.get("name")
        if not isinstance(pos, int) or pos < 0:
            err("%s: position must be a number" % where)
        elif pos in positions:
            err("%s: position used twice" % where)
        positions.add(pos)
        if not isinstance(name, str) or not _NAME.match(name):
            err("%s: name %r must be letters, digits, '-', '_' and '.'" % (where, name))
        elif name in names or name in axis_names:
            err("%s: name %r used twice" % (where, name))
        names.add(name)
        ident = s.get("identity") or {}
        for k in ("vendor", "product"):
            try:
                int(str(ident.get(k)), 16)
            except ValueError:
                err("%s: identity %s %r is not a number" % (where, k, ident.get(k)))
        profile = s.get("profile")
        if profile not in ("cia402", "mdp", "generic"):
            err("%s: unknown profile %r" % (where, profile))
        # unused: true: on the bus, in the file for its identity (compared at
        # start), but not configured and left alone by the component, which
        # reads nothing else of the entry (it stays for a later unused: false)
        if "unused" in s and not isinstance(s["unused"], bool):
            err("%s: unused must be true or false" % where)
        if s.get("unused") is True:
            if s.get("eoe") is not None:
                err("%s: unused: true and eoe: a slave with a network address is used (the master configures it)"
                    % where)
            continue
        if profile == "cia402" and (s.get("cia402") or {}).get("mode") not in CIA402_MODES:
            err("%s: cia402 mode must be one of %s" % (where, ", ".join(sorted(CIA402_MODES))))
        other_modes = (s.get("cia402") or {}).get("modes") or [] if profile == "cia402" else []
        if not isinstance(other_modes, list):
            err("%s: cia402 modes must be a list, such as [hm]" % where)
            other_modes = []
        for m in other_modes:
            if m not in CIA402_OTHER_MODES:
                err("%s: cia402 mode %r is not supported in modes (%s)"
                    % (where, m, ", ".join(sorted(CIA402_OTHER_MODES))))
        if profile == "cia402" and not isinstance((s.get("cia402") or {}).get("zero-at-start", False), bool):
            err("%s: cia402 zero-at-start must be true or false" % where)
        # several axes: axis n has the objects at index + n * 0x800
        n_axes = 1
        if profile == "cia402" and "axes" in (s.get("cia402") or {}):
            axes = s["cia402"]["axes"]
            if not isinstance(axes, list) or not 1 <= len(axes) <= MAX_AXES:
                err("%s: cia402 axes must be a list of 1 to %d names" % (where, MAX_AXES))
            else:
                n_axes = len(axes)
                for a in axes:
                    if not isinstance(a, str) or not _NAME.match(a):
                        err("%s: axis name %r must be letters, digits, '-', '_' and '.'" % (where, a))
                    elif a in names or a in axis_names:
                        err("%s: axis name %r is used for another slave or axis" % (where, a))
                    axis_names.add(a)

        mods = s.get("modules")
        if mods is not None:
            if not isinstance(mods, list):
                err("%s: modules must be a list of module idents" % where)
            else:
                for m in mods:
                    try:
                        int(str(m), 16)
                    except ValueError:
                        err("%s: module %r is not a hex number" % (where, m))

        # encoder channels: status index -> name
        encoders = {}
        enc = s.get("encoders") or []
        if not isinstance(enc, list):
            err("%s: encoders must be a list" % where)
            enc = []
        for ch in enc:
            ch = ch if isinstance(ch, dict) else {}
            cname = ch.get("name")
            try:
                index = int(str(ch.get("index")), 16)
            except ValueError:
                index = -1
            if not isinstance(cname, str) or not _NAME.match(cname):
                err("%s: encoder name %r is not valid" % (where, cname))
                continue
            if not (0x6000 <= index <= 0x6FF0 and index & 0xF == 0):
                err("%s: encoder %s: index must be the channel's status index, 0x6000 to 0x6FF0 "
                    "in steps of 0x10" % (where, cname))
                continue
            if index in encoders or cname in encoders.values():
                err("%s: encoder %s: index or name used twice" % (where, cname))
            encoders[index] = cname
            for pin in ENCODER_PINS:
                pins.add("%s.%s.%s" % (name, cname, pin))

        # serial channels: output PDO, input PDO -> name; bits seen per PDO
        serial = []  # (name, out, in, settings index)
        ser = s.get("serial") or []
        if not isinstance(ser, list):
            err("%s: serial must be a list" % where)
            ser = []
        if len(ser) > SERIAL_MAX_CHANNELS:
            err("%s: more than %d serial channels" % (where, SERIAL_MAX_CHANNELS))
        for i, ch in enumerate(ser):
            ch = ch if isinstance(ch, dict) else {}
            cname = ch.get("name")
            if not isinstance(cname, str) or not _NAME.match(cname):
                err("%s: serial channel name %r is not valid" % (where, cname))
                continue
            try:
                out, inp = int(str(ch.get("output-pdo")), 16), int(str(ch.get("input-pdo")), 16)
            except ValueError:
                out = inp = -1
            if not (0x1600 <= out <= 0x17FF and 0x1A00 <= inp <= 0x1BFF):
                err("%s: serial channel %s: output-pdo (0x1600-0x17FF) and input-pdo "
                    "(0x1A00-0x1BFF) are needed" % (where, cname))
                continue
            port = ch.get("port")
            if not isinstance(port, str) or not port.startswith("/") or len(port) >= 48:
                err("%s: serial channel %s: port must be a full path shorter than 48 characters, "
                    "such as /dev/ttyECAT0" % (where, cname))
            elif port in ports:
                err("%s: serial channel %s: port %s is another channel's" % (where, cname, port))
            else:
                ports.add(port)
            try:
                settings_index = int(str(ch.get("settings-index", "0x%04X" % (0x8000 + 0x10 * i))), 16)
            except ValueError:
                settings_index = -1
            if not 0x8000 <= settings_index <= 0x8FF0:
                err("%s: serial channel %s: settings-index must be 0x8000 to 0x8FF0" % (where, cname))
            if ch.get("baud", 9600) not in SERIAL_BAUDS:
                err("%s: serial channel %s: baud %r is not one of %s"
                    % (where, cname, ch.get("baud"), ", ".join(str(b) for b in sorted(SERIAL_BAUDS))))
            if str(ch.get("frame", "8N1")) not in SERIAL_FRAMES:
                err("%s: serial channel %s: frame %r is not one of %s"
                    % (where, cname, ch.get("frame"), ", ".join(SERIAL_FRAMES)))
            if ch.get("send-continuous", False) not in (True, False):
                err("%s: serial channel %s: send-continuous is true or false" % (where, cname))
            for other in serial:
                if other[0] == cname or other[1] == out or other[2] == inp or other[3] == settings_index:
                    err("%s: serial channel %s: its name, PDOs or settings-index are another channel's"
                        % (where, cname))
            serial.append((cname, out, inp, settings_index))
            for pin in SERIAL_PINS:
                pins.add("%s.%s.%s" % (name, cname, pin))
        serial_bits = {}  # PDO -> bits

        def encoder_of(o):
            if o[0] in encoders and o[1] in ENCODER_INPUTS:
                return encoders[o[0]]
            if o[0] - 0x1000 in encoders and o[1] in ENCODER_OUTPUTS:
                return encoders[o[0] - 0x1000]
            return None

        seen = set()
        owned = set()
        sm_dir = {}
        for direction in ("outputs", "inputs"):
            for pdo in (s.get("process-data") or {}).get(direction, []):
                sm = pdo.get("sm")
                if not isinstance(sm, int) or not 0 <= sm <= 255:
                    err("%s: PDO %s: sm must be 0 to 255" % (where, pdo.get("pdo")))
                elif sm_dir.setdefault(sm, direction) != direction:
                    # the component would leave the second direction's PDOs out
                    err("%s: sm %d is used for outputs and inputs" % (where, sm))
                    sm_dir[sm] = "both"
                try:
                    pdo_index = int(str(pdo.get("pdo")), 16)
                except ValueError:
                    pdo_index = None
                ser_ch = next((c for c in serial if pdo_index in (c[1], c[2])), None)
                if ser_ch is not None and pdo_index != (ser_ch[1] if direction == "outputs" else ser_ch[2]):
                    err("%s: serial channel %s: PDO 0x%04X is in the %s" % (where, ser_ch[0], pdo_index, direction))
                    ser_ch = None
                if ser_ch is not None:
                    # all of it is the serial function's
                    serial_bits[pdo_index] = 0
                    for e in pdo.get("entries", []):
                        bits = e.get("bits")
                        serial_bits[pdo_index] += bits if isinstance(bits, int) else 0
                        if e.get("use") == "gap":
                            continue
                        if e.get("use") != "profile" or "pin" in e or "split" in e:
                            err("%s: %s: serial channel %s handles this PDO; give its entries use: profile"
                                % (where, e.get("object"), ser_ch[0]))
                        try:
                            seen.add(parse_obj(e.get("object")))
                        except ValueError as x:
                            err("%s: %s" % (where, x))
                    continue
                for e in pdo.get("entries", []):
                    try:
                        o = parse_obj(e.get("object"))
                    except ValueError as x:
                        err("%s: %s" % (where, x))
                        continue
                    if e.get("use") == "gap":
                        continue
                    base = next((o[0] - a * AXIS_OFFSET for a in range(n_axes)
                                 if o[0] - a * AXIS_OFFSET in CIA402_PROFILE), None)
                    if profile == "cia402" and o[1] == 0 and base is not None:
                        if e.get("use") != "profile":
                            err("%s: %s is handled by the cia402 profile; give it use: profile"
                                % (where, e["object"]))
                        owned.add(o[0])
                    elif encoder_of(o):
                        if e.get("use") != "profile":
                            err("%s: %s is handled by encoder %s; give it use: profile"
                                % (where, e["object"], encoder_of(o)))
                        owned.add(o)
                    elif e.get("use") == "profile":
                        err("%s: %s: use: profile is only for objects of the cia402 profile, an "
                            "encoder channel or a serial channel's PDO" % (where, e["object"]))
                    if o in seen:
                        err("%s: %s mapped twice" % (where, e["object"]))
                    seen.add(o)
                    if e.get("type") not in DATA_TYPES:
                        err("%s: %s has unknown type %r" % (where, e["object"], e.get("type")))
                    bits = e.get("bits")
                    if not isinstance(bits, int) or not 0 < bits <= 64:
                        err("%s: %s bits must be 1..64" % (where, e["object"]))
                    own = []
                    if "pin" in e:
                        own = [e["pin"]]
                        if e.get("hal-type") not in HAL_TYPES:
                            err("%s: %s has unknown hal-type %r" % (where, e["object"], e.get("hal-type")))
                        elif e.get("type") in HAL_64 and not wide and e.get("hal-type") != "real":
                            err("%s: %s: LinuxCNC %s has no 64-bit pins: a %s entry takes hal-type real"
                                % (where, e["object"], series_text, e["type"]))
                        elif e.get("type") in HAL_64 and e.get("hal-type") not in ("real", HAL_64[e["type"]]):
                            err("%s: %s: a %s entry takes hal-type %s (or real)"
                                % (where, e["object"], e["type"], HAL_64[e["type"]]))
                        if ("scale" in e or "offset" in e) and not (
                                e.get("hal-type") == "real" and e.get("type") in INTEGER_TYPES):
                            err("%s: %s: scale and offset need an integer type and hal-type real"
                                % (where, e["object"]))
                        if e.get("raw") is not None and (
                                e.get("raw") is not True and e.get("raw") is not False):
                            err("%s: %s: raw is true or false" % (where, e["object"]))
                        elif e.get("raw") and (e.get("hal-type") != "uint" or not isinstance(bits, int)
                                               or bits > 32):
                            err("%s: %s: raw is for an entry of up to 32 bits with hal-type uint"
                                % (where, e["object"]))
                        if e.get("extend") is not None and (
                                e.get("extend") is not True and e.get("extend") is not False):
                            err("%s: %s: extend is true or false" % (where, e["object"]))
                        elif e.get("extend") and (direction != "inputs" or e.get("hal-type") != "sint"
                                                  or not isinstance(bits, int) or not 8 <= bits <= 32):
                            err("%s: %s: extend is for an input entry of 8 to 32 bits with "
                                "hal-type sint" % (where, e["object"]))
                        elif e.get("extend") and not wide and bits == 32:
                            err("%s: %s: LinuxCNC %s's pins have 32 bits: extend is for an entry of "
                                "8 to 16 bits there" % (where, e["object"], series_text))
                    if "invert" in e:
                        inv = e["invert"]
                        if "pin" in e:
                            if not isinstance(inv, bool) or e.get("hal-type") != "bool":
                                err("%s: %s: invert is true or false, for a bit pin"
                                    % (where, e["object"]))
                        elif not isinstance(inv, list) or "split" not in e:
                            err("%s: %s: invert is true or false for a bit pin, a list of "
                                "names for a split" % (where, e["object"]))
                        else:
                            for n in inv:
                                if n not in (e.get("split") or []):
                                    err("%s: %s: invert lists %s, which is not in split"
                                        % (where, e["object"], n))
                    if "split" in e:
                        split = e["split"]
                        if not isinstance(split, list) or len(split) > (bits or 0):
                            err("%s: %s split has more pins than bits" % (where, e["object"]))
                        else:
                            own += [p for p in split if p]
                    for p in own:
                        full = "%s.%s" % (name, p)
                        if not isinstance(p, str) or not _NAME.match(p):
                            err("%s: pin name %r is not valid" % (where, p))
                        elif full in pins:
                            err("%s: pin %s used twice" % (where, full))
                        pins.add(full)
        for cname, out, inp, settings_index in serial:
            for pdo_index, direction in ((out, "outputs"), (inp, "inputs")):
                bits = serial_bits.get(pdo_index)
                if bits is None:
                    err("%s: serial channel %s: PDO 0x%04X is not in the %s" % (where, cname, pdo_index, direction))
                elif bits % 8 or not 24 <= bits <= 16 + 8 * SERIAL_MAX_DATA:
                    err("%s: serial channel %s: PDO 0x%04X has %d bits; it needs a control (status) byte, "
                        "a length byte and 1 to %d data bytes" % (where, cname, pdo_index, bits, SERIAL_MAX_DATA))
            if out in serial_bits and inp in serial_bits and serial_bits[out] != serial_bits[inp]:
                err("%s: serial channel %s: the output and input PDOs need the same size" % (where, cname))
        for index, cname in encoders.items():
            if (index, 0x11) not in owned:
                err("%s: encoder %s needs 0x%04X:11 (counter value) in the process data"
                    % (where, cname, index))
        if profile == "cia402":
            rx, tx = set(), set()
            for m in [(s.get("cia402") or {}).get("mode")] + other_modes:
                r, t = CIA402_NEEDS.get(m, (set(), set()))
                rx, tx = rx | r, tx | t
            for a in range(n_axes):
                off = a * AXIS_OFFSET
                for i in sorted((rx | tx) - {x - off for x in owned}):
                    err("%s: the cia402 profile needs 0x%04X:00 in the process data" % (where, i + off))
                if other_modes:
                    for i in sorted(CIA402_MODE_OBJECTS - {x - off for x in owned} - rx - tx):
                        err("%s: changing modes needs 0x%04X:00 in the process data"
                            % (where, i + off))
            for e in feedforward_errors(s.get("cia402") or {}, other_modes, owned, n_axes):
                err("%s: %s" % (where, e))
        dc = s.get("dc")
        if dc is not None:
            aa = dc.get("assign-activate")
            if aa is None or str(aa).strip() == "":
                err("%s: DC sync needs an assign-activate value, such as 0x0300 (from the ESI file)"
                    % where)
            else:
                try:
                    int(str(aa), 16)
                except ValueError:
                    err("%s: dc assign-activate %r is not a hex number, such as 0x0300" % (where, aa))
            for key, low in (("sync1-cycles", 0), ("sync1-shift-ns", None)):
                v = dc.get(key)
                if v is None:
                    continue
                if isinstance(v, bool) or not isinstance(v, int) or (low is not None and v < low):
                    err("%s: dc %s must be a whole number%s" % (where, key, " of 0 or more" if low == 0 else ""))
            if dc.get("sync1-shift-ns") is not None and not dc.get("sync1-cycles"):
                err("%s: dc sync1-shift-ns needs sync1-cycles (SYNC1 every n SYNC0 cycles)" % where)
        for e in eoe_errors(s.get("eoe")):
            err("%s: %s" % (where, e))
        for e in init_cmd_errors(s):
            err("%s: %s" % (where, e))
        v = s.get("sdo-timeout-ms")
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 600000):
            err("%s: sdo-timeout-ms must be 1 to 600000 (ms)" % where)
        st = s.get("state-timeouts-ms")
        if st is not None:
            if not isinstance(st, dict):
                err("%s: state-timeouts-ms needs preop, safeop-op, back-to-init and/or back-to-safeop" % where)
            else:
                for k, v in st.items():
                    if k not in STATE_TIMEOUTS:
                        err("%s: state-timeouts-ms: unknown key %r (%s)" % (where, k, ", ".join(STATE_TIMEOUTS)))
                    elif isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 600000:
                        err("%s: state-timeouts-ms.%s must be 1 to 600000 (ms)" % (where, k))
        set_objects = set()
        for st in s.get("settings") or []:
            try:
                set_objects.add(parse_obj(st.get("object")))
                for cname, _, _, settings_index in serial:
                    if parse_obj(st.get("object")) in ((settings_index, SERIAL_SUB_BAUD),
                                                       (settings_index, SERIAL_SUB_FRAME),
                                                       (settings_index, SERIAL_SUB_CONTINUOUS)):
                        err("%s: setting %s: the baud rate, data frame and send continuous of serial "
                            "channel %s are its baud, frame and send-continuous"
                            % (where, st.get("object"), cname))
                why = quirks.command(s, parse_obj(st.get("object"))[0])
                if why:
                    err("%s: setting %s is a command, not a setting: %s"
                        % (where, st.get("object"), why))
            except ValueError as x:
                err("%s: setting: %s" % (where, x))
            if "value" in st:
                if st.get("type") not in DATA_TYPES:
                    err("%s: setting %s has unknown type %r" % (where, st.get("object"), st.get("type")))
            elif "data" in st:
                try:
                    bytes.fromhex(str(st["data"]))
                except ValueError:
                    err("%s: setting %s data %r is not hex bytes" % (where, st.get("object"), st["data"]))
            else:
                err("%s: setting %s has neither value nor data" % (where, st.get("object")))
        if profile == "cia402" and (s.get("cia402") or {}).get("mode") in CIA402_MODES:
            # the mode: set every cycle (0x6060 mapped), once as a setting, or at least seen
            # (0x6061: the profile enables only in the mode it shows); a setting says the
            # mode the profile runs in (ecat_config.c checks the same)
            number = MODE_NUMBERS[s["cia402"]["mode"]]
            for a in range(n_axes):
                o = a * AXIS_OFFSET
                value = mode_setting(s, a)
                if value is not None and not other_modes and value != number:
                    err("%s: the setting %s is %s, but cia402.mode is %d: make them the same (the setting "
                        "is the mode the drive is put in at start)" % (where, obj(0x6060 + o, 0), value, number))
                if not other_modes and 0x6060 + o not in owned and 0x6061 + o not in owned and value is None:
                    err("%s: the drive's mode is neither set nor seen: add the setting %s = %d (type SINT, "
                        "the mode at start), or map %s or %s"
                        % (where, obj(0x6060 + o, 0), number, obj(0x6060 + o, 0), obj(0x6061 + o, 0)))
        if profile == "cia402" and "pp" in [(s.get("cia402") or {}).get("mode")] + other_modes:
            # the drive plans PP moves itself: it must know its travel (0x607F,
            # where the drive has it: session.setting_problems)
            for a in range(n_axes):
                for i, sub, _ in PP_REQUIRED:
                    if (i + a * AXIS_OFFSET, sub) not in set_objects:
                        err("%s: cia402 mode pp needs the setting %s: add it to the settings, type DINT, "
                            "with the %s position the drive may move to, in counts (the drive plans PP "
                            "moves within its software position limits; its present value, read in "
                            "the SDO card, is a start)"
                            % (where, obj(i + a * AXIS_OFFSET, sub), "lowest" if sub == 1 else "highest"))
    errors += check_name_lengths(cfg, series)
    from . import fromlcec
    return errors + check_joints(cfg) + check_nets(cfg) + check_params(cfg) + \
        fromlcec.check(cfg)


def hal_name_len(series):
    """The longest pin or parameter name HAL takes (HAL_NAME_LEN): 47
    characters on LinuxCNC 2.9, 55 on 2.10."""
    return 55 if series >= (2, 10) else 47


def check_name_lengths(cfg, series):
    """Pins and parameters whose names are too long for the series' HAL: the
    component refuses to load ("length N too long for name")."""
    from . import halpins
    try:
        names = halpins.pins(cfg)
    except Exception:                   # a broken file: its own errors say why
        return []
    limit = hal_name_len(series)
    return ["pin %s: %d characters, LinuxCNC %d.%d takes at most %d in a HAL name: give it a shorter "
            "name" % (n, len(n), series[0], series[1], limit)
            for n in sorted(names) if len(n) > limit]


def check_joints(cfg):
    """Errors in the linuxcnc section: joints connected to drives."""
    errors = []
    lc = cfg.get("linuxcnc")
    if lc is None:
        return errors
    if not isinstance(lc, dict):
        return ["linuxcnc: must be a mapping"]
    if not isinstance(lc.get("check-hal-at-start", False), bool):
        errors.append("linuxcnc.check-hal-at-start: must be true or false")
    js = lc.get("joints") or []
    if not isinstance(js, list):
        return ["linuxcnc.joints: must be a list"]
    csp = {a for s in cfg.get("slaves") or []
           if s.get("profile") == "cia402" and (s.get("cia402") or {}).get("mode") == "csp"
           for a in axes(s)}
    numbers, slaves = set(), set()
    for j in js:
        n = j.get("joint") if isinstance(j, dict) else None
        where = "linuxcnc.joints: joint %r" % (n,)
        if not isinstance(n, int) or isinstance(n, bool) or n < 0:
            errors.append("%s: joint must be a number" % where)
        elif n in numbers:
            errors.append("%s: listed twice" % where)
        numbers.add(n)
        if not isinstance(j, dict):
            continue
        if j.get("slave") not in csp:
            errors.append("%s: %r is not a cia402 drive in mode csp" % (where, j.get("slave")))
        elif j["slave"] in slaves:
            errors.append("%s: %s is connected to another joint too" % (where, j["slave"]))
        slaves.add(j.get("slave"))
        scale = j.get("scale", 1.0)
        if not isinstance(scale, (int, float)) or isinstance(scale, bool) or scale == 0:
            errors.append("%s: scale must be a number other than 0" % where)
    return errors


def check_nets(cfg):
    """Errors in linuxcnc.nets: the slaves' pins connected to LinuxCNC's."""
    from . import integrate
    lc = cfg.get("linuxcnc")
    if not isinstance(lc, dict) or lc.get("nets") is None:
        return []
    ns = lc["nets"]
    if not isinstance(ns, list):
        return ["linuxcnc.nets: must be a list"]
    errors = []
    own = integrate.slave_pins(cfg)
    names = {s.get("name"): s for s in cfg.get("slaves") or []}
    taken = integrate.joint_pins(cfg)
    signals, seen = set(), {}
    for n in ns:
        if not isinstance(n, dict):
            errors.append("linuxcnc.nets: each net must be a mapping")
            continue
        sig = n.get("signal")
        where = "linuxcnc.nets: %s" % (sig,)
        if not isinstance(sig, str) or not integrate.SIGNAL_RE.fullmatch(sig):
            errors.append("%s: signal must be a HAL name (no blanks or #, at most 47 characters)"
                          % where)
        elif sig in signals:
            errors.append("%s: listed twice" % where)
        signals.add(sig)
        pins = n.get("pins")
        if not isinstance(pins, list) or len(pins) < 2 or \
                not all(isinstance(x, str) for x in pins):
            errors.append("%s: pins must be a list of at least two pin names" % where)
            continue
        mine = False
        types, writers = set(), []
        for x in pins:
            if not integrate.PIN_RE.fullmatch(x):
                errors.append("%s: %r is not a HAL pin name" % (where, x))
                continue
            if x in seen:
                errors.append("%s: %s is in signal %s too" % (where, x, seen[x]))
            seen[x] = sig
            if x in taken:
                errors.append("%s: %s is connected by joint %d" % (where, x, taken[x]))
            slave = next((s for name, s in names.items()
                          if name and x.startswith(name + ".")), None) or \
                axis_of(cfg, x.rsplit(".", 1)[0])[0]
            if slave is not None:
                mine = True
                if x not in own and slave.get("profile") in (None, "generic") and \
                        not slave.get("encoders"):
                    errors.append("%s: %s is not a pin of slave %s" % (where, x, slave["name"]))
            info = own.get(x) or integrate.known_pin(x)
            if info:
                types.add(info[0])
                if info[1] == "out":
                    writers.append(x)
        if not mine:
            errors.append("%s: none of its pins is a slave's pin" % where)
        if len(types) > 1:
            errors.append("%s: pins of different HAL types (%s)" % (where, ", ".join(sorted(types))))
        if len(writers) > 1:
            errors.append("%s: more than one pin writes it (%s)" % (where, ", ".join(writers)))
    return errors


def check_params(cfg):
    """Errors in linuxcnc.params: parameters of the slaves given a value."""
    from . import halpins
    lc = cfg.get("linuxcnc")
    if not isinstance(lc, dict) or lc.get("params") is None:
        return []
    ps = lc["params"]
    if not isinstance(ps, list):
        return ["linuxcnc.params: must be a list"]
    known = halpins.parameters(cfg)
    errors, seen = [], set()
    for e in ps:
        if not isinstance(e, dict):
            errors.append("linuxcnc.params: each entry must be a mapping with pin and value")
            continue
        pin, v = e.get("pin"), e.get("value")
        where = "linuxcnc.params: %s" % (pin,)
        if pin not in known:
            errors.append("%s: not a parameter of a slave (the master's and a joint's "
                          "pos-scale are not set here)" % where)
            continue
        if pin in seen:
            errors.append("%s: listed twice" % where)
        seen.add(pin)
        htype = known[pin][0]
        if htype == "bool":
            if not isinstance(v, bool):
                errors.append("%s: must be true or false" % where)
        elif htype == "real":
            if not isinstance(v, (int, float)) or isinstance(v, bool) or v != v \
                    or abs(v) == float("inf"):
                errors.append("%s: must be a number" % where)
            elif pin.endswith(".pos-scale") and v == 0:
                errors.append("%s: the scale must not be 0" % where)
        elif not isinstance(v, int) or isinstance(v, bool) or (htype == "uint" and v < 0):
            errors.append("%s: must be a%s whole number" % (where, " positive" if htype == "uint" else ""))
    return errors
