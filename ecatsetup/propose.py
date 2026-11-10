"""Propose a configuration for one slave from what can be read: the slave's
SII, its CoE dictionary, a few CoE objects, and an ESI file if one matches.

This is the core of the prototype: the question it answers is whether a
slave can be configured without a hand-written mapping.
"""

import dataclasses
import os
import re
from dataclasses import dataclass, field

from . import quirks

# CiA 402 objects that the drive profile handles itself (no HAL pin); the
# profile gives them its own pins (component/devices/ecat_cia402.c).
# a drive with several axes: axis n's objects at index + n * 0x800
AXIS_OFFSET = 0x800
CIA402_PROFILE = {0x603F, 0x6040, 0x6041, 0x6060, 0x6061,
                  0x6064, 0x606C, 0x607A, 0x6081, 0x60FF}
# Other CiA 402 process data, with fixed pin names and types.
CIA402_PINS = {
    0x6071: ("torque-cmd", "s32"), 0x6077: ("torque-fb", "s32"),
    0x60F4: ("following-error", "s32"), 0x60FD: ("digital-inputs", "u32"),
    0x60FE: ("digital-outputs", "u32"), 0x6063: ("encoder-fb", "s32"),
}
# What each CiA 402 cyclic mode needs in the process data.
CIA402_MODES = {
    "csp": ({0x6040, 0x607A}, {0x6041, 0x6064}),
    "csv": ({0x6040, 0x60FF}, {0x6041, 0x606C}),
    "pv": ({0x6040, 0x60FF}, {0x6041, 0x606C}),
    "pp": ({0x6040, 0x607A}, {0x6041, 0x6064}),
}
# The settings a PP drive must have, since it plans its moves itself: the
# software position limits and the maximum profile velocity.
PP_LIMITS = [(0x607D, 1, "DINT"), (0x607D, 2, "DINT"), (0x607F, 0, "UDINT")]
# 0x6502 "supported drive modes", bit -> name.
CIA402_MODE_BITS = {0: "PP", 1: "VL", 2: "PV", 3: "TQ", 5: "HM", 6: "IP",
                    7: "CSP", 8: "CSV", 9: "CST"}
PROFILE_NAMES = {402: "CiA 402 drive", 5001: "Modular device (ETG.5001)"}

HAL_TYPES = {"BOOL": "bit", "SINT": "s32", "INT": "s32", "DINT": "s32",
             "USINT": "u32", "UINT": "u32", "UDINT": "u32", "REAL": "float",
             "LINT": "float", "ULINT": "float", "LREAL": "float"}  # no 64-bit HAL pins
# The ethercat tool's CoE type names, in ESI/SII terms.
# Unsigned 32-bit entries that are counts which wrap, such as encoder
# counters and latched positions: proposed as s32 pins.
WRAPPING = re.compile(r"counter|latch|position", re.I)

COE_TYPES = {"bool": "BOOL", "int8": "SINT", "int16": "INT", "int32": "DINT",
             "uint8": "USINT", "uint16": "UINT", "uint32": "UDINT",
             "float": "REAL", "double": "LREAL", "int64": "LINT", "uint64": "ULINT"}


@dataclass
class Entry:
    index: int
    subindex: int
    bitlen: int
    name: str
    data_type: str
    role: str            # "profile" (cia402), "encoder", "pin" or "gap"
    pin: str = ""
    hal_type: str = ""
    scale: float = None  # float pins of integer entries: read raw * scale + offset,
    offset: float = None  # written as (value + offset) * scale


@dataclass
class PdoChoice:
    sm: int
    index: int
    direction: str
    name: str
    entries: list = field(default_factory=list)


@dataclass
class Proposal:
    position: int
    vendor_id: int
    product_code: int
    revision: int
    serial: int
    name: str
    profile: int = None
    profile_source: str = ""
    esi_revision: int = None    # the revision of the ESI file's device, when there is one
    esi_exact: bool = False     # the ESI file is for the slave's revision
    drive_modes: list = field(default_factory=list)
    mode: str = ""
    pdos: list = field(default_factory=list)
    pdo_source: str = ""
    pdo_sets: list = field(default_factory=list)   # names of the ESI's predefined assignments
    dc_assign: int = None
    dc_sync0_factor: int = None
    dc_shift: int = None
    dc_source: str = ""
    esi_path: str = ""
    settings: list = field(default_factory=list)   # writable vendor objects
    encoders: list = field(default_factory=list)   # (name, status index) of encoder channels
    axes: int = 1        # a CiA 402 drive's axes: the status word at 0x6041 + n * 0x800
    pp_limits: dict = field(default_factory=dict)  # PP: (index, subindex) -> the drive's value
    modules: list = None  # a modular device's detected modules (0xF050), or None
    max_velocity: dict = field(default_factory=dict)  # CiA 402: axis -> 0x607F (+ 0x800 n), counts/s
    notes: list = field(default_factory=list)


# Encoder channels laid out as in ETG.5001 MDP 511: inputs at a status
# index S, outputs at S + 0x1000. The ecat encoder function handles these.
ENCODER_INPUTS = {0x01, 0x02, 0x03, 0x11, 0x12}
ENCODER_OUTPUTS = {0x01, 0x02, 0x03, 0x04, 0x11}


def _short(name):
    return (name or "").split("__")[-1].strip().lower()


def encoder_channels(entries):
    """Status indexes of the encoder channels among the mapped entries: a
    16 or 32-bit "Counter value" at S:11 and "Latch C valid" at S:01."""
    by = {(e.index, e.subindex): e for e in entries}
    found = []
    for (index, sub), e in sorted(by.items()):
        if (sub == 0x11 and 0x6000 <= index <= 0x6FF0 and index & 0xF == 0
                and e.bitlen in (16, 32) and _short(e.name) == "counter value"
                and _short(getattr(by.get((index, 0x01)), "name", "")) == "latch c valid"):
            found.append(index)
    return found


def encoder_owned(index, entries):
    """The entries of the channel at status index `index` that it handles."""
    return {(e.index, e.subindex) for e in entries
            if (e.index == index and e.subindex in ENCODER_INPUTS)
            or (e.index == index + 0x1000 and e.subindex in ENCODER_OUTPUTS)}


# Analog channels laid out as in ETG.5001 (Beckhoff EL3xxx/EL4xxx): a
# 16-bit value at 0x6nn0:11 (inputs) or 0x7nn0:11 (outputs), whose full
# scale 0x7FFF is the top of the measuring range.
ANALOG_FULL = 0x7FFF
_NUM = r"(\d+(?:[.,]\d+)?)"
_UNIT = r"\s*(mV|V|mA|uA|µA)\b"


def analog_range(text):
    """The measuring range in a device's name, such as "EL3102 2Ch. Ana.
    Input +/-10V" or "4..20 mA": (low, high, unit), or None."""
    t = (text or "").replace("±", "+/-")
    m = re.search(r"\+/-\s*" + _NUM + _UNIT, t)
    if m:
        v = float(m.group(1).replace(",", "."))
        return -v, v, m.group(2).replace("µ", "u")
    m = re.search(_NUM + r"\s*(?:-|\.\.|…|to)\s*" + _NUM + _UNIT, t)
    if m:
        lo, hi = (float(x.replace(",", ".")) for x in m.group(1, 2))
        if hi > lo:
            return lo, hi, m.group(3).replace("µ", "u")
    return None


def analog_scaling(rng, output):
    """scale and offset of a float pin for a range: raw 0 is the low end of
    a range that starts at 0 or above (0..10 V, 4..20 mA) and 0 of one
    around 0 (+/-10 V); raw 0x7FFF is the high end."""
    lo, hi, _ = rng
    zero = lo if lo >= 0 else 0.0
    if output:
        return ANALOG_FULL / (hi - zero), -zero
    return (hi - zero) / ANALOG_FULL, zero


def analog_value(e, direction):
    """Whether an entry is an analog channel's value."""
    base = 0x6000 if direction == "tx" else 0x7000
    return (e.subindex == 0x11 and base <= e.index <= base + 0xFF0 and e.index & 0xF == 0
            and e.bitlen == 16 and e.data_type in ("INT", "UINT")
            and re.search(r"value|output", _short(e.name)) is not None)


def add_modules(p, detected, configured):
    """A modular device (ETG.5001 0xF050/0xF030): best effort. The process
    data is taken as the slave maps it now; the modules are recorded, so
    that a change is noticed."""
    if detected is None:
        return
    p.modules = list(detected)
    ids = ", ".join("0x%08X" % m for m in detected) or "none"
    p.notes.append("modular device: %d module%s detected (0xF050: %s); the process data is "
                   "taken as the slave maps it now" % (len(detected), "" if len(detected) == 1
                                                       else "s", ids))
    if configured is not None and configured and configured != detected:
        p.notes.append("modules configured in 0xF030 (%s) differ from those detected: the slave "
                       "may refuse OP until 0xF030 matches (see docs/component.md)"
                       % (", ".join("0x%08X" % m for m in configured)))


def kebab(name):
    """"Target Position", "LowpassFilteredVoltage", "Out_Unit1" -> kebab-case."""
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", name.strip())
    return re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()


def _coe_type(coe, index, subindex):
    e = coe.entries.get((index, subindex)) if coe else None
    return COE_TYPES.get(e.data_type) if e else None


def _fix_subindexes(pdo_entries, coe, notes, pdo_index):
    """Some SII contents give subindex 0 for every entry of a record object.
    Take the subindexes from the CoE dictionary, matched by order and name."""
    by_index = {}
    for e in pdo_entries:
        by_index.setdefault(e.index, []).append(e)
    for index, entries in by_index.items():
        if index == 0 or len(entries) < 2 or any(e.subindex for e in entries):  # 0: gaps
            continue
        subs = coe.subentries(index) if coe else []
        if len(subs) < len(entries):
            notes.append("PDO 0x%04X: entries of 0x%04X all have subindex 0 in the SII "
                         "and the CoE dictionary cannot correct it" % (pdo_index, index))
            continue
        for e, s in zip(entries, subs):
            e.subindex = s.subindex
        notes.append("PDO 0x%04X: the SII gives subindex 0 for all entries of 0x%04X; "
                     "subindexes taken from the CoE dictionary" % (pdo_index, index))


def propose(info, sii, coe, device_type=None, drive_modes=None, esi_dev=None,
            esi_exact=False, mode="csp", pdo_set=None):
    """Make a Proposal for one slave.

    info: tool.slave_info(); sii: sii.Sii; coe: coe.Dictionary (may be empty);
    device_type: CoE 0x1000; drive_modes: CoE 0x6502; esi_dev: esi.EsiDevice.
    """
    p = Proposal(info["position"], sii.vendor_id, sii.product_code, sii.revision,
                 sii.serial, sii.name or sii.order)

    # Profile: the slave's own device type first, then the ESI file.
    if device_type is not None and (device_type & 0xFFFF) in PROFILE_NAMES:
        p.profile, p.profile_source = device_type & 0xFFFF, "CoE 0x1000"
    elif esi_dev and esi_dev.profile in PROFILE_NAMES:
        p.profile, p.profile_source = esi_dev.profile, "ESI"
    if p.profile == 402:
        p.mode = mode
        if drive_modes is not None:
            p.drive_modes = [n for b, n in sorted(CIA402_MODE_BITS.items()) if drive_modes & (1 << b)]
            if mode.upper() not in p.drive_modes:
                p.notes.append("the drive does not report %s in 0x6502" % mode.upper())

    if esi_dev:
        p.esi_path = esi_dev.path
        p.esi_revision, p.esi_exact = esi_dev.revision, bool(esi_exact)
        if not esi_exact:
            p.notes.append("ESI file is for revision 0x%08X, the slave is 0x%08X"
                           % (esi_dev.revision, sii.revision))

    # Process data: a predefined assignment from the ESI file (the one asked
    # for, else its default), else the ESI's default assignment, else the SII's.
    chosen_set = None
    if esi_dev and esi_dev.pdo_sets:
        p.pdo_sets = [s.name for s in esi_dev.pdo_sets]
        if pdo_set:
            chosen_set = next((s for s in esi_dev.pdo_sets if s.name.lower() == pdo_set.lower()), None)
            if chosen_set is None:
                p.notes.append("no predefined PDO assignment \"%s\"; the ESI has: %s"
                               % (pdo_set, ", ".join(p.pdo_sets)))
        if chosen_set is None:
            chosen_set = next((s for s in esi_dev.pdo_sets if s.default), None)
    elif pdo_set:
        p.notes.append("no ESI file with predefined PDO assignments; \"%s\" ignored" % pdo_set)
    if chosen_set is not None:
        by_index = {d.index: d for d in esi_dev.pdos}
        source = "ESI predefined assignment \"%s\"" % chosen_set.name
        candidates = [dataclasses.replace(by_index[i], sm=sm)
                      for sm, pdos in sorted(chosen_set.sms.items()) for i in pdos if i in by_index]
    elif esi_dev and any(d.sm is not None for d in esi_dev.pdos):
        source, candidates = "ESI default assignment", esi_dev.pdos
    else:
        source, candidates = "slave's SII default assignment", sii.pdos
    p.pdo_source = source
    chosen = [c for c in candidates if c.sm is not None]
    if not chosen and not candidates:
        p.notes.append("no process data (such as a coupler)")
    elif not chosen:
        p.notes.append("no PDOs assigned by default; the mapping must be chosen")

    pdos = []
    for c in chosen:
        entries = [Entry(e.index, e.subindex, e.bitlen, e.name, e.data_type, "pin")
                   for e in c.entries]
        _fix_subindexes(entries, coe, p.notes, c.index)
        pdos.append(PdoChoice(c.sm, c.index, c.direction, c.name, entries))

    if p.profile == 402:
        need_rx, need_tx = CIA402_MODES.get(mode, (set(), set()))
        have = {e.index for pd in pdos for e in pd.entries}
        # several axes (ETG.6010: axis n at index + n * 0x800)
        while p.axes < 4 and 0x6041 + p.axes * AXIS_OFFSET in have:
            p.axes += 1
        if p.axes > 1:
            p.notes.append("%d axes: each gets its own pins (cia402.axes)" % p.axes)
        missing = set()
        for a in range(p.axes):
            missing |= {i + a * AXIS_OFFSET for i in need_rx | need_tx} - have
        if missing:
            p.notes.append("the default PDOs lack %s for %s; the mapping must be changed"
                           % (", ".join("0x%04X" % i for i in sorted(missing)), mode.upper()))

    # Encoder channels: the encoder function handles their counter and latches.
    mapped = [e for pd in pdos for e in pd.entries]
    enc_owned = set()
    channels = encoder_channels(mapped)
    for n, index in enumerate(channels, 1):
        name = "enc" if len(channels) == 1 else "enc-%d" % n
        p.encoders.append((name, index))
        enc_owned |= encoder_owned(index, mapped)
        bits = next(e.bitlen for e in mapped if (e.index, e.subindex) == (index, 0x11))
        p.notes.append("encoder %s: channel 0x%04X, %d-bit counter; position, velocity, index "
                       "and latch pins from the encoder function" % (name, index, bits))

    # Names that occur in more than one PDO get the PDO's name as a prefix.
    counts = {}
    for pd in pdos:
        for e in pd.entries:
            counts[e.name] = counts.get(e.name, 0) + 1

    for pd in pdos:
        for e in pd.entries:
            if e.index == 0 and e.subindex == 0:
                e.role = "gap"
                continue
            coe_t = _coe_type(coe, e.index, e.subindex)
            if coe_t and coe_t != e.data_type:
                p.notes.append("0x%04X:%02X: %s says %s, the CoE dictionary %s; using %s"
                               % (e.index, e.subindex, "ESI" if esi_dev else "SII",
                                  e.data_type, coe_t, coe_t))
                e.data_type = coe_t
            if p.profile == 402 and any(e.index - a * AXIS_OFFSET in CIA402_PROFILE
                                        for a in range(p.axes)):
                e.role = "profile"
            elif (e.index, e.subindex) in enc_owned:
                e.role = "encoder"
            elif p.profile == 402 and any(e.index - a * AXIS_OFFSET in CIA402_PINS
                                          for a in range(p.axes)):
                a = next(a for a in range(p.axes) if e.index - a * AXIS_OFFSET in CIA402_PINS)
                e.pin, e.hal_type = CIA402_PINS[e.index - a * AXIS_OFFSET]
                if p.axes > 1:
                    e.pin = "%s-%d" % (e.pin, a + 1)
            else:
                # Beckhoff names a record's members "Status__Latch C valid":
                # the part before "__" repeats the record's name.
                base = kebab(e.name.split("__")[-1]) or "0x%04x-%02x" % (e.index, e.subindex)
                e.pin = "%s.%s" % (kebab(pd.name), base) if counts[e.name] > 1 else base
                e.hal_type = "bit" if e.bitlen == 1 else HAL_TYPES.get(e.data_type, "u32")
                if e.hal_type == "u32" and e.bitlen == 32 and WRAPPING.search(e.name):
                    # an encoder counter or latch counts down past 0;
                    # LinuxCNC's encoder and joint pins take it signed
                    e.hal_type = "s32"
                elif pd.direction == "tx" and e.bitlen == 16 and re.search(r"counter", e.name, re.I):
                    p.notes.append("%s: a 16-bit counter, which wraps at 65536%s"
                                   % (e.pin, "; a 32-bit set is offered with --pdo-set"
                                      if esi_dev and len(esi_dev.pdo_sets) > 1 else ""))
                elif p.profile == 5001 and analog_value(e, pd.direction):
                    rng = analog_range(p.name) or analog_range(getattr(esi_dev, "name", ""))
                    if rng:
                        e.hal_type = "float"
                        e.scale, e.offset = analog_scaling(rng, pd.direction == "rx")
                        p.notes.append("%s: analog %s, %g..%g %s from the device's name; the pin "
                                       "is in %s" % (e.pin, "output" if pd.direction == "rx"
                                                     else "input", rng[0], rng[1], rng[2], rng[2]))
                    else:
                        p.notes.append("%s: analog %s in raw counts (0x7FFF is the top of the "
                                       "range); no range found in the device's name"
                                       % (e.pin, "output" if pd.direction == "rx" else "input"))
    # A quirk file's pins for this device win over the names above.
    qpins = quirks.pins((p.vendor_id, p.product_code))
    used = []
    for pd in pdos:
        for e in pd.entries:
            qp = qpins.get((e.index, e.subindex))
            if qp and e.role == "pin":
                e.pin = str(qp["pin"])
                e.hal_type = str(qp.get("hal-type") or e.hal_type)
                e.scale, e.offset = qp.get("scale"), qp.get("offset")
                used.append(e.pin)
    if used:
        p.notes.append("pins %s from the quirk file %s" % (
            ", ".join(used), os.path.basename(quirks.of((p.vendor_id, p.product_code))["path"])))
    qset = quirks.settings((p.vendor_id, p.product_code))
    if qset:
        p.notes.append("settings %s from the quirk file %s" % (
            ", ".join(st["object"] for st in qset),
            os.path.basename(quirks.of((p.vendor_id, p.product_code))["path"])))
    p.pdos = pdos

    # Distributed clocks: an ESI operation mode, else the SII's.
    modes = []
    if esi_dev and esi_dev.dc_modes:
        modes = [(m.name, m.assign_activate, m.sync0_factor, m.shift0, "ESI") for m in esi_dev.dc_modes]
    elif sii.dc_modes:
        modes = [(m.name, m.assign_activate, m.sync0_cycle_factor, m.shift_time0, "SII")
                 for m in sii.dc_modes]
    # The first mode is the slave's default: a drive lists DC first, an I/O
    # terminal free run ("Synchron"), with DC as an option.
    if modes and modes[0][1]:
        name, p.dc_assign, p.dc_sync0_factor, p.dc_shift, src = modes[0]
        p.dc_source = "%s mode \"%s\"" % (src, name)
    elif any(m[1] for m in modes):
        p.notes.append("DC is optional (%s modes: %s); not used by default"
                       % (modes[0][4], ", ".join(m[0] for m in modes)))
    elif info.get("dc") and p.profile == 402:
        p.notes.append("the drive supports DC but describes no DC mode; it may need one")

    # Settings: writable vendor objects, for the user to choose.
    if coe:
        # vendor objects, and a modular device's channel settings (0x8nn0,
        # such as an analog terminal's filter and user scaling)
        p.settings = [e for (i, s), e in sorted(coe.entries.items())
                      if (0x2000 <= i < 0x6000 or (p.profile == 5001 and 0x8000 <= i < 0x9000))
                      and e.writable and s >= 0
                      and not (s == 0 and coe.subentries(i))]
    return p


def decode(data, coe_entry):
    """An SDO value from lcec's raw bytes, readable by its CoE type."""
    if coe_entry is None:
        return data.hex(" ")
    t = coe_entry.data_type
    if t.startswith(("uint", "int")):
        return str(int.from_bytes(data, "little", signed=t.startswith("int")))
    return data.hex(" ")
