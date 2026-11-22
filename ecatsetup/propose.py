"""Propose a configuration for one slave from what can be read: the slave's
SII, its CoE dictionary, a few CoE objects, and an ESI file if one matches.

This is the core of the prototype: the question it answers is whether a
slave can be configured without a hand-written mapping.
"""

import dataclasses
import os
import re
from dataclasses import dataclass, field

from . import dcinfo, pdoreq, quirks

# CiA 402 objects that the drive profile handles itself (no HAL pin); the
# profile gives them its own pins (component/devices/ecat_cia402.c).
# a drive with several axes: axis n's objects at index + n * 0x800
AXIS_OFFSET = 0x800
CIA402_PROFILE = {0x603F, 0x6040, 0x6041, 0x6060, 0x6061,
                  0x6064, 0x606C, 0x607A, 0x6081, 0x60FF,
                  0x60B1, 0x60B2}   # velocity and torque offset: feedforward in CSP
# Other CiA 402 process data, with fixed pin names and types.
CIA402_PINS = {
    0x6071: ("torque-cmd", "sint"), 0x6077: ("torque-fb", "sint"),
    0x60F4: ("following-error", "sint"), 0x60FD: ("digital-inputs", "uint"),
    0x60FE: ("digital-outputs", "uint"), 0x6063: ("encoder-fb", "sint"),
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
# Of those, what every PP drive must have as settings. 0x607F only where the
# drive has it (the Rtelligent ECT60 has not): a warning with the dictionary.
PP_REQUIRED = PP_LIMITS[:2]
PP_MAX_VELOCITY = PP_LIMITS[2]
# The modes' numbers in 0x6060/0x6061.
CIA402_MODE_NUMBERS = {"pp": 1, "pv": 3, "hm": 6, "csp": 8, "csv": 9}
# 0x6502 "supported drive modes", bit -> name.
CIA402_MODE_BITS = {0: "PP", 1: "VL", 2: "PV", 3: "TQ", 5: "HM", 6: "IP",
                    7: "CSP", 8: "CSV", 9: "CST"}
PROFILE_NAMES = {402: "CiA 402 drive", 5001: "Modular device (ETG.5001)"}

HAL_TYPES = {"BOOL": "bool", "SINT": "sint", "INT": "sint", "DINT": "sint",
             "USINT": "uint", "UINT": "uint", "UDINT": "uint", "REAL": "real",
             "LINT": "sint", "ULINT": "uint", "LREAL": "real"}  # 64-bit integer pins in 2.10
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
    role: str            # "profile" (cia402), "encoder", "serial", "pin" or "gap"
    pin: str = ""
    hal_type: str = ""
    scale: float = None  # float pins of integer entries: read raw * scale + offset,
    offset: float = None  # written as (value + offset) * scale
    split: list = None    # a bit field: one pin name per bit (None: no pin), instead of pin
    extend: bool = False  # an input counter of 16 or 32 bits: the pin keeps counting past the wrap (64 bits)
    raw: bool = False     # a bit pattern (digital outputs): cut to the entry's width, not held at its top


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
    pdo_set_pdos: dict = field(default_factory=dict)   # name -> sorted [(sm, pdo index)]
    dc_assign: int = None
    dc_sync0_factor: int = None
    dc_sync1_cycles: int = 0     # SYNC1 every n SYNC0 cycles (0: none)
    dc_sync1_shift: int = 0
    dc_shift: int = None
    dc_source: str = ""
    dc_info: object = None      # dcinfo.DcInfo: what the slave can do with DC
    has_eoe: bool = False       # the SII lists the EoE mailbox protocol
    esi_path: str = ""
    settings: list = field(default_factory=list)   # writable vendor objects
    encoders: list = field(default_factory=list)   # (name, status index) of encoder channels
    serial_channels: list = field(default_factory=list)  # (name, output PDO, input PDO)
    axes: int = 1        # a CiA 402 drive's axes: the status word at 0x6041 + n * 0x800
    pp_limits: dict = field(default_factory=dict)  # PP: (index, subindex) -> the drive's value
    modules: list = None  # a modular device's detected modules (0xF050), or None
    max_velocity: dict = field(default_factory=dict)  # CiA 402: axis -> 0x607F (+ 0x800 n), counts/s
    pdo_way: str = ""     # "" (the assignment as it was), or the way that was applied (the user asked)
    recommendation: dict = None   # a better PDO assignment, for the user to approve:
    #                               {"way": title or None, "pdo-set": name or None, "why": text}
    recognised: int = None        # the profile the slave or its ESI file says (402, 5001, None)
    status: list = field(default_factory=list)    # pdoreq.Status lines under the PDOs
    state_timeouts: dict = field(default_factory=dict)  # the ESI's state timeouts longer than the master's
    sdo_timeout: int = None   # the ESI's SDO response timeout, where longer than the master's
    init_cmds: list = field(default_factory=list)  # init-cmds: the ESI's InitCmds (and its modules'),
    #                                                0xF030, the cia402 mode where 0x6060 is not mapped
    notes: list = field(default_factory=list)


# Encoder channels laid out as in ETG.5001 MDP 511: inputs at a status
# index S, outputs at S + 0x1000. The ecat encoder function handles these.
ENCODER_INPUTS = {0x01, 0x02, 0x03, 0x11, 0x12}
ENCODER_OUTPUTS = {0x01, 0x02, 0x03, 0x04, 0x11}


def _short(name):
    return (name or "").split("__")[-1].strip().lower()


def encoder_channels(entries, sub_profile_511=False):
    """Status indexes of the encoder channels among the mapped entries: a
    16 or 32-bit "Counter value" at S:11 and "Latch C valid" at S:01. A device
    whose ESI file lists the sub-profile 511 (incremental encoder) needs no
    latch C: a channel mapped for its position alone is one too."""
    by = {(e.index, e.subindex): e for e in entries}
    found = []
    for (index, sub), e in sorted(by.items()):
        if (sub == 0x11 and 0x6000 <= index <= 0x6FF0 and index & 0xF == 0
                and e.bitlen in (16, 32) and _short(e.name) == "counter value"
                and (sub_profile_511
                     or _short(getattr(by.get((index, 0x01)), "name", "")) == "latch c valid")):
            found.append(index)
    return found


def encoder_owned(index, entries):
    """The entries of the channel at status index `index` that it handles."""
    return {(e.index, e.subindex) for e in entries
            if (e.index == index and e.subindex in ENCODER_INPUTS)
            or (e.index == index + 0x1000 and e.subindex in ENCODER_OUTPUTS)}


# Serial channels (Beckhoff EL6001, EL6002, EL6021, EL6022): an output PDO
# that starts with the control word ("Ctrl", or its bits "Ctrl__Transmit
# request" ...) followed by "Data Out 0" ..., and an input PDO with the
# status word and "Data In 0" .... The serial function handles both.
SERIAL_MAX_DATA = 22


def _serial_pdo(pd, word, data):
    names = [_short(e.name) for e in pd.entries]
    bits = sum(e.bitlen for e in pd.entries)
    first = (pd.entries[0].name or "").split("__")[0].strip().lower() if pd.entries else ""
    return (first == word and "%s 0" % data in names and bits % 8 == 0
            and 24 <= bits <= 16 + 8 * SERIAL_MAX_DATA)


def serial_channels(pdos):
    """(output PDO, input PDO) pairs of serial channels, in order: the n-th
    output PDO of the kind with the n-th input PDO of the same size."""
    outs = [pd for pd in pdos if pd.direction == "rx" and _serial_pdo(pd, "ctrl", "data out")]
    ins = [pd for pd in pdos if pd.direction == "tx" and _serial_pdo(pd, "status", "data in")]
    return [(o, i) for o, i in zip(outs, ins)
            if sum(e.bitlen for e in o.entries) == sum(e.bitlen for e in i.entries)]


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


def _channels(sub, pdos):
    """The channels of the kind a sub-profile is for among PDOs (EsiPdo or PdoChoice)."""
    if sub == 511:
        return encoder_channels([e for c in pdos for e in c.entries], True)
    return serial_channels(pdos)


def _set_with_channel(esi_dev, chosen, sub):
    """(predefined set, its PDOs with their sync managers) when the device has sub-profile
    `sub`, `chosen` has no channel of its kind and a set has: for serial the one with the most
    data bytes, else the first. Otherwise None."""
    if sub not in esi_dev.sub_profiles or _channels(sub, chosen):
        return None
    by_index = {d.index: d for d in esi_dev.pdos}
    found = []
    for st in esi_dev.pdo_sets:
        members = [dataclasses.replace(by_index[i], sm=sm) for sm, ids in sorted(st.sms.items())
                   for i in ids if i in by_index]
        channels = _channels(sub, members)
        if channels:
            size = sum(e.bitlen for e in channels[0][0].entries) if sub == 600 else 0
            found.append(((size, st.name.startswith("Standard")), st, members))
    if not found:
        return None
    best = max(found, key=lambda f: f[0])      # max keeps the first of equal sizes
    return best[1], best[2]


def propose(info, sii, coe, device_type=None, drive_modes=None, esi_dev=None,
            esi_exact=False, mode="csp", pdo_set=None, extra_modes=(), way=None, profile=None,
            modules=None, assignment=None):
    """Make a Proposal for one slave.

    info: tool.slave_info(); sii: sii.Sii; coe: coe.Dictionary (may be empty);
    device_type: CoE 0x1000; drive_modes: CoE 0x6502; esi_dev: esi.EsiDevice.
    extra_modes: the drive's other modes (cia402.modes); way: apply a PDO way the user chose
    ("recommended", or the title of one of pdoreq's ways), else a better way is only
    recommended (Proposal.recommendation); profile: "cia402", "mdp" or "generic", the user's
    choice over the one recognised. modules: the module idents the slave detects (0xF050);
    with Slots in its ESI file the modules' PDOs, InitCmds and 0xF030 come from there.
    assignment: the PDO indexes the user chose (the window's PDO assignment list), taken as
    they are; an open one among them is filled with what the profile needs.
    """
    from . import lcnc_env
    wide = lcnc_env.wide_pins()  # 64-bit integer pins (LinuxCNC 2.10)
    p = Proposal(info["position"], sii.vendor_id, sii.product_code, sii.revision,
                 sii.serial, sii.name or sii.order)

    # Profile: the slave's own device type first, then the ESI file.
    if device_type is not None and (device_type & 0xFFFF) in PROFILE_NAMES:
        p.profile, p.profile_source = device_type & 0xFFFF, "CoE 0x1000"
    elif esi_dev and esi_dev.profile in PROFILE_NAMES:
        p.profile, p.profile_source = esi_dev.profile, "ESI"
    p.recognised = p.profile
    if profile is not None:
        wanted = {"cia402": 402, "mdp": 5001, "generic": None}[profile]
        if wanted != p.profile:
            p.profile, p.profile_source = wanted, "chosen"
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
        p.pdo_set_pdos = {s.name: sorted((sm, i) for sm, pdos in s.sms.items() for i in pdos)
                          for s in esi_dev.pdo_sets}
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
    layout = None
    if esi_dev is not None and getattr(esi_dev, "slots", None) is not None and modules is not None:
        from . import slots as slots_mod
        sms = {c.direction: c.sm for c in esi_dev.pdos if c.sm is not None}
        layout = slots_mod.layout(esi_dev.slots, list(modules), sms.get("rx", 2), sms.get("tx", 3))
        chosen = chosen + layout.pdos
        p.pdo_source = "%s and the modules' PDOs (ESI Slots, %d module%s from 0xF050)" % (
            source, len(modules), "" if len(modules) == 1 else "s")
        p.notes += layout.notes + layout.problems
    elif esi_dev is not None and getattr(esi_dev, "slots", None) is not None:
        p.notes.append("a modular device (Slots in its ESI file), but its module list (0xF050) was not "
                       "read: its modules' PDOs are not in the proposal")
    if not chosen and not candidates:
        p.notes.append("no process data (such as a coupler)")
    elif not chosen:
        p.notes.append("no PDOs assigned by default; the mapping must be chosen")

    modes_text = ", ".join(m.upper() for m in [mode, *extra_modes])
    if assignment is not None and esi_dev:
        wants = {"profile": "cia402", "cia402": {"mode": mode, "modes": list(extra_modes)}} \
            if p.profile == 402 else None
        chosen = pdoreq.assigned(esi_dev, set(assignment), wants) + (layout.pdos if layout else [])
        p.pdo_way = "chosen"
        p.pdo_source = "PDOs chosen: %s" % ", ".join("0x%04X" % c.index for c in chosen)
        p.notes.append("PDO mapping: %s" % p.pdo_source)
    if p.profile == 402 and esi_dev and not pdo_set and assignment is None:
        # (an assignment the user asked for is kept as it is)
        # the mode set needs objects the assignment may lack: another ready way, else an own
        # mapping in the open PDOs (docs/development.md "Own PDO mapping"); recommended, and
        # applied only when the user says so
        have = {e.index for c in chosen for e in c.entries}
        axes = 1
        while axes < 4 and 0x6041 + axes * AXIS_OFFSET in have:
            axes += 1
        plan = pdoreq.fit_cia402(esi_dev, chosen, mode, extra_modes, axes, drive_modes,
                                 choose=None if way in (None, "recommended") else way)
        if plan.way is not None and way is not None:
            chosen = plan.pdos
            p.pdo_way = plan.way.kind
            p.pdo_source = "%s (%s)" % (plan.way.title, "ready" if plan.way.kind == "ready"
                                        else "built for %s" % modes_text)
            p.notes.append("PDO mapping: %s, as chosen" % p.pdo_source)
        elif plan.way is not None:
            p.recommendation = {"way": plan.way.title, "pdo-set": plan.way.set_name,
                                "why": "the assignment lacks what %s needs" % modes_text}
            p.notes.append("recommended PDO mapping: %s (%s); not applied until you choose it"
                           % (plan.way.title, p.recommendation["why"]))
        elif plan.verdict is not None:
            p.notes += ["the PDOs cannot give %s: %s" % (mode.upper(), t) for t in plan.verdict.problems[:3]]

    if p.profile == 5001 and esi_dev and not pdo_set and assignment is None:
        # a terminal whose default assignment lacks the channel its sub-profile is for (an
        # incremental encoder's legacy layout, a serial terminal's 50-word set): the
        # predefined set that has one, recommended
        for sub, kind in ((511, "encoder"), (600, "serial")):
            found = _set_with_channel(esi_dev, chosen, sub)
            if found is None:
                continue
            set_, members = found
            if way is not None:
                chosen = members
                p.pdo_way = "ready"
                p.pdo_source = "ESI predefined assignment \"%s\"" % set_.name
                p.notes.append("PDO mapping: %s, as chosen" % p.pdo_source)
            else:
                p.recommendation = {"way": "predefined set \"%s\"" % set_.name, "pdo-set": set_.name,
                                    "why": "the assignment has no %s channel (sub-profile %d)" % (kind, sub)}
                p.notes.append("recommended PDO assignment: \"%s\" (%s); not applied until you "
                               "choose it" % (set_.name, p.recommendation["why"]))
            break

    pdos = []
    for c in chosen:
        entries = [Entry(e.index, e.subindex, e.bitlen, e.name, e.data_type, "pin")
                   for e in c.entries]
        _fix_subindexes(entries, coe, p.notes, c.index)
        pdos.append(PdoChoice(c.sm, c.index, c.direction, c.name, entries))

    if p.profile == 402:
        have = {e.index for pd in pdos for e in pd.entries}
        # several axes (ETG.6010: axis n at index + n * 0x800)
        while p.axes < 4 and 0x6041 + p.axes * AXIS_OFFSET in have:
            p.axes += 1
        if p.axes > 1:
            p.notes.append("%d axes: each gets its own pins (cia402.axes)" % p.axes)

    # Encoder channels: the encoder function handles their counter and latches.
    mapped = [e for pd in pdos for e in pd.entries]
    enc_owned = set()
    channels = encoder_channels(mapped, 511 in getattr(esi_dev, "sub_profiles", ()))
    for n, index in enumerate(channels, 1):
        name = "enc" if len(channels) == 1 else "enc-%d" % n
        p.encoders.append((name, index))
        enc_owned |= encoder_owned(index, mapped)
        bits = next(e.bitlen for e in mapped if (e.index, e.subindex) == (index, 0x11))
        p.notes.append("encoder %s: channel 0x%04X, %d-bit counter; position, velocity, index "
                       "and latch pins from the encoder function" % (name, index, bits))

    # Serial channels: the serial function handles their PDOs.
    serial_pdos = set()
    channels = serial_channels(pdos)
    for n, (o, i) in enumerate(channels, 1):
        name = "port" if len(channels) == 1 else "port-%d" % n
        p.serial_channels.append((name, o.index, i.index))
        serial_pdos |= {o.index, i.index}
        p.notes.append("serial %s: PDOs 0x%04X/0x%04X, %d data bytes; a serial port on the "
                       "computer from the serial function (set its baud rate and frame to the "
                       "device's; RS485 needs half duplex, 0x%04X:06; Modbus RTU above 38400 baud "
                       "needs send-continuous: true, so that a frame goes out without a pause)"
                       % (name, o.index, i.index, sum(e.bitlen for e in o.entries) // 8 - 2,
                          0x8000 + 0x10 * (n - 1)))

    # A modular-device terminal read without its ESI file is in the layout the terminal
    # itself starts with, often the legacy one (an EL5101: 0x6000 Status, Value, Latch):
    # the encoder function needs the ETG.5001 layout, which the ESI file's predefined
    # assignments ("Standard 32 Bit (MDP 511)") give
    if (p.profile == 5001 and esi_dev is None and not p.encoders
            and any(re.search(r"latch|counter", e.name or "", re.I) for pd in pdos for e in pd.entries)):
        p.notes.append("this terminal has a counter or latch, but no ESI file matches it: its "
                       "process data is the terminal's own default, which may be a legacy layout "
                       "without an encoder channel (position, velocity, index). Add the ESI "
                       "files (Beckhoff's XML) and read the bus again: the ESI file's predefined "
                       "assignments, such as \"Standard 32 Bit (MDP 511)\", give the encoder "
                       "function")

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
            elif pd.index in serial_pdos:
                e.role = "serial"
            elif p.profile == 402 and any(e.index - a * AXIS_OFFSET in CIA402_PINS
                                          for a in range(p.axes)):
                a = next(a for a in range(p.axes) if e.index - a * AXIS_OFFSET in CIA402_PINS)
                e.pin, e.hal_type = CIA402_PINS[e.index - a * AXIS_OFFSET]
                e.raw = e.pin == "digital-outputs"
                if p.axes > 1:
                    e.pin = "%s-%d" % (e.pin, a + 1)
            else:
                # Beckhoff names a record's members "Status__Latch C valid":
                # the part before "__" repeats the record's name.
                base = kebab(e.name.split("__")[-1]) or "0x%04x-%02x" % (e.index, e.subindex)
                e.pin = "%s.%s" % (kebab(pd.name), base) if counts[e.name] > 1 else base
                e.hal_type = "bool" if e.bitlen == 1 else HAL_TYPES.get(e.data_type, "uint")
                if e.data_type in ("LINT", "ULINT") and not wide:
                    e.hal_type = "real"  # LinuxCNC 2.9 has no 64-bit pins: exact up to 2^53
                if e.hal_type == "uint" and e.bitlen == 32 and WRAPPING.search(e.name):
                    # an encoder counter or latch counts down past 0;
                    # LinuxCNC's encoder and joint pins take it signed
                    e.hal_type = "sint"
                if (pd.direction == "tx" and e.hal_type in ("sint", "uint")
                        and ((e.bitlen == 32 and WRAPPING.search(e.name) and wide)
                             or (e.bitlen == 16 and re.search(r"counter", e.name, re.I)))):
                    # the entry wraps; the pin counts on in 64 bits (a counter or a latched
                    # position that goes past 0 is negative, as the encoder's own pins).
                    # LinuxCNC 2.9's pins have 32 bits: a 16-bit counter is taken on to
                    # 32 bits there, a 32-bit one wraps as the pin does.
                    e.hal_type, e.extend = "sint", True
                    p.notes.append("%s: a %d-bit counter, extended to %d bits (extend)"
                                   % (e.pin, e.bitlen, 64 if wide else 32))
                elif p.profile == 5001 and analog_value(e, pd.direction):
                    rng = analog_range(p.name) or analog_range(getattr(esi_dev, "name", ""))
                    if rng:
                        e.hal_type = "real"
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
            if qp and e.role == "pin" and qp.get("split"):
                e.split, e.pin, e.hal_type = list(qp["split"]), "", "bool"
                used.extend(x for x in e.split if x)
            elif qp and e.role == "pin":
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

    # The ESI file's state change timeouts, where they are longer than the master's
    if esi_dev is not None and getattr(esi_dev, "state_timeouts", None):
        from . import config as config_mod
        p.state_timeouts = config_mod.longer_timeouts(esi_dev.state_timeouts)
        if p.state_timeouts:
            p.notes.append("state change timeouts from the ESI file, longer than the master's: %s"
                           % ", ".join("%s %d ms" % kv for kv in sorted(p.state_timeouts.items())))
    if esi_dev is not None and (getattr(esi_dev, "sdo_timeout", None) or 0) > 1000:
        p.sdo_timeout = esi_dev.sdo_timeout
        p.notes.append("SDO answers may take %d ms (the ESI file's ResponseTimeout; the master's own is "
                       "1000 ms)" % p.sdo_timeout)

    # Settings the device needs written at start: its ESI file's InitCmds, and for a
    # drive whose 0x6060 is not in the process data, the mode (unless it has only one)
    module_cmds = layout.init_cmds if layout is not None else []
    if esi_dev is not None and (esi_dev.init_cmds or module_cmds):
        from . import esi as esi_mod, initcmds
        dictionary = coe if coe is not None and coe.entries else esi_mod.dictionary(esi_dev)
        cmds = dataclasses.replace(esi_dev, init_cmds=list(esi_dev.init_cmds) + module_cmds)
        found = initcmds.from_esi(cmds, dictionary, "cia402" if p.profile == 402 else None)
        p.init_cmds += found.cmds
        p.notes += found.notes
        if found.cmds:
            p.notes.append("init-cmds %s from the ESI file's InitCmds" % ", ".join(r["object"] for r in found.cmds))
    if layout is not None:
        # the configured module list, written at the transition the ESI file names (IP)
        p.init_cmds += [dict(st, at="IP") for st in layout.settings]
    if p.profile == 402:
        mapped = {e.index for pd in pdos for e in pd.entries}
        # also a drive that reports one mode only: it may start in mode 0 (no mode), and the
        # profile and the check need the mode set or seen (2026-11-22)
        for a in range(p.axes):
            index = 0x6060 + a * AXIS_OFFSET
            obj_text = "0x%04X:00" % index
            if index in mapped or extra_modes:
                # the mode is set every cycle: no row for it
                p.init_cmds = [r for r in p.init_cmds if r["object"] != obj_text]
                continue
            if not any(r["object"] == obj_text for r in p.init_cmds):
                p.init_cmds.append({"at": "PS", "object": obj_text, "from": "profile"})
            p.notes.append("0x%04X is not in the process data: the mode (%d, %s) is written at every start "
                           "(init-cmds, from: profile)" % (index, CIA402_MODE_NUMBERS[mode], mode.upper()))
        from . import config
        mine = {"cia402": {"mode": mode, "axes": [None] * p.axes}, "init-cmds": p.init_cmds}
        p.status = pdoreq.cia402_status(mode, extra_modes, p.axes,
                                        {(e.index, e.subindex) for pd in pdos for e in pd.entries},
                                        mode_settings=pdoreq.mode_settings(mine))
        for st in p.status:
            if st.level == "error":
                p.notes.append(st.text)

    # Distributed clocks: the modes of an ESI file of the slave's revision,
    # else the SII's, else an ESI file's of another revision (dcinfo.py).
    p.has_eoe = sii.has_eoe
    if sii.has_eoe:
        p.notes.append("This slave supports EoE (Ethernet over EtherCAT): an eoe: block in its entry gives it an "
                       "IP address (docs/component.md)")
    p.dc_info = d = dcinfo.build(info, sii, esi_dev, esi_exact)
    modes = d.modes()
    # The first mode is the slave's default: a drive lists DC first, an I/O
    # terminal free run ("Synchron"), with DC as an option.
    if modes and modes[0].dc and d.can_sync is not False:
        m = modes[0]
        p.dc_assign, p.dc_sync0_factor, p.dc_shift = m.assign, m.sync0_factor, m.shift
        if m.assign & 0x0400:      # SYNC1 is on (ESI files give a factor for every mode)
            p.dc_sync1_cycles, p.dc_sync1_shift = m.sync1_cycles, m.sync1_shift
        if m.assign & 0x0400 and not m.sync1_cycles:
            p.notes.append("the DC mode \"%s\" uses SYNC1 but gives no SYNC1 cycle%s: set sync1-cycles"
                           % (m.name, " (only a fixed %d ns)" % m.sync1_ns if m.sync1_ns else ""))
        p.dc_source = "%s mode \"%s\"" % (m.source, m.name)
    elif modes and modes[0].dc:
        p.notes.append("the slave lists DC modes, but its EtherCAT chip has no DC clock; DC "
                       "not used")
    elif d.dc_modes():
        p.notes.append("DC is optional (%s modes: %s); not used by default"
                       % (modes[0].source, ", ".join(m.name for m in modes)))
    elif d.can_sync and p.profile == 402:
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
