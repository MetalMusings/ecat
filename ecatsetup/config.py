"""The ecat configuration file (ethercat.yaml): make it from proposals, write
it, read it back and check it.

The format is described in docs/config-format.md. The file is plain YAML
that the setup program writes; numbers that are EtherCAT identifiers
(vendor, product, object indexes) are kept as "0x..." strings so that they
read as they do in vendor documentation.
"""

import re

import yaml

from .propose import CIA402_PROFILE, CIA402_MODES as CIA402_NEEDS, ENCODER_INPUTS, ENCODER_OUTPUTS, PP_LIMITS

FORMAT_VERSION = 1

HEADER = """\
# ecat configuration, written by EtherCAT Setup.
# Read by the ecat HAL component: loadrt ecat config=<this file>
# Format: https://github.com/MetalMusings/ecat/blob/main/docs/config-format.md
# The program rewrites this file; comments added by hand are not kept.
"""

PROFILES = {402: "cia402", 5001: "mdp"}
DATA_TYPES = {"BOOL", "SINT", "INT", "DINT", "LINT", "USINT", "UINT", "UDINT",
              "ULINT", "REAL", "LREAL"}
HAL_TYPES = {"bit", "s32", "u32", "s64", "u64", "float"}
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


def parse_obj(text):
    """"0x6040:00" -> (0x6040, 0)."""
    m = _OBJECT.match(str(text))
    if not m:
        raise ValueError("not an object reference: %r" % text)
    return int(m.group(1), 16), int(m.group(2), 16)


def default_name(p):
    kind = {"cia402": "drive", "mdp": "io"}.get(PROFILES.get(p.profile), "slave")
    return "%s-%d" % (kind, p.position)


def _entry(e):
    d = {"object": obj(e.index, e.subindex), "bits": e.bitlen}
    if e.role == "gap":
        d["use"] = "gap"
        return d
    d["type"] = e.data_type
    if e.role in ("profile", "encoder"):
        d["use"] = "profile"
    elif e.pin:
        d["pin"] = e.pin
        d["hal-type"] = e.hal_type
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
    if p.profile == 402:
        s["cia402"] = {"mode": p.mode}
    if p.encoders:
        s["encoders"] = [{"name": n, "index": "0x%04X" % i} for n, i in p.encoders]
    pd = {"outputs": [], "inputs": []}
    for c in p.pdos:
        pd["outputs" if c.direction == "rx" else "inputs"].append(
            {"pdo": "0x%04X" % c.index, "sm": c.sm, "entries": [_entry(e) for e in c.entries]})
    s["configure-pdos"] = True
    s["process-data"] = {k: v for k, v in pd.items() if v}
    if p.dc_assign is not None:
        s["dc"] = {"assign-activate": "0x%04X" % p.dc_assign,
                   "sync0-cycles": p.dc_sync0_factor, "sync0-shift-ns": p.dc_shift}
    s["settings"] = [{"object": obj(i, sub), "type": t, "value": p.pp_limits[(i, sub)]}
                     for i, sub, t in PP_LIMITS if (i, sub) in p.pp_limits]
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


class _Dumper(yaml.SafeDumper):
    pass


def _flow_entries(dumper, data):
    # One line per PDO entry or setting: short dicts in flow style.
    flow = set(data) <= {"object", "bits", "type", "use", "pin", "hal-type", "split",
                         "scale", "offset", "name", "value", "data",
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


def load(path):
    with open(path) as f:
        return yaml.safe_load(f)


def check(cfg):
    """Errors in a configuration, as readable strings; empty if it is fine."""
    errors = []
    err = errors.append
    if not isinstance(cfg, dict):
        return ["the file is not a YAML mapping"]
    if cfg.get("ecat") != FORMAT_VERSION:
        err("ecat: format version %r, expected %d" % (cfg.get("ecat"), FORMAT_VERSION))
    master = cfg.get("master") or {}
    if master.get("transport", "raw") not in TRANSPORTS:
        err("master: unknown transport %r" % master.get("transport"))
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

    positions, names, pins = set(), set(), set()
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
        elif name in names:
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
                for e in pdo.get("entries", []):
                    try:
                        o = parse_obj(e.get("object"))
                    except ValueError as x:
                        err("%s: %s" % (where, x))
                        continue
                    if e.get("use") == "gap":
                        continue
                    if profile == "cia402" and o[1] == 0 and o[0] in CIA402_PROFILE:
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
                        err("%s: %s: use: profile is only for objects of the cia402 profile or an "
                            "encoder channel" % (where, e["object"]))
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
                        if ("scale" in e or "offset" in e) and not (
                                e.get("hal-type") == "float" and e.get("type") in INTEGER_TYPES):
                            err("%s: %s: scale and offset need an integer type and hal-type float"
                                % (where, e["object"]))
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
        for index, cname in encoders.items():
            if (index, 0x11) not in owned:
                err("%s: encoder %s needs 0x%04X:11 (counter value) in the process data"
                    % (where, cname, index))
        if profile == "cia402":
            rx, tx = set(), set()
            for m in [(s.get("cia402") or {}).get("mode")] + other_modes:
                r, t = CIA402_NEEDS.get(m, (set(), set()))
                rx, tx = rx | r, tx | t
            for i in sorted((rx | tx) - owned):
                err("%s: the cia402 profile needs 0x%04X:00 in the process data" % (where, i))
            if other_modes:
                for i in sorted(CIA402_MODE_OBJECTS - owned - rx - tx):
                    err("%s: changing modes needs 0x%04X:00 in the process data" % (where, i))
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
        set_objects = set()
        for st in s.get("settings") or []:
            try:
                set_objects.add(parse_obj(st.get("object")))
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
        if profile == "cia402" and "pp" in [(s.get("cia402") or {}).get("mode")] + other_modes:
            # the drive plans PP moves itself: it must know its limits
            for i, sub, _ in PP_LIMITS:
                if (i, sub) not in set_objects:
                    err("%s: cia402 mode pp needs the setting %s" % (where, obj(i, sub)))
    return errors + check_joints(cfg)


def check_joints(cfg):
    """Errors in the linuxcnc section: joints connected to drives."""
    errors = []
    lc = cfg.get("linuxcnc")
    if lc is None:
        return errors
    if not isinstance(lc, dict):
        return ["linuxcnc: must be a mapping"]
    js = lc.get("joints") or []
    if not isinstance(js, list):
        return ["linuxcnc.joints: must be a list"]
    csp = {s.get("name") for s in cfg.get("slaves") or []
           if s.get("profile") == "cia402" and (s.get("cia402") or {}).get("mode") == "csp"}
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
