"""Propose a configuration for one slave from what can be read: the slave's
SII, its CoE dictionary, a few CoE objects, and an ESI file if one matches.

This is the core of the prototype: the question it answers is whether a
slave can be configured without a hand-written mapping.
"""

import dataclasses
import re
from dataclasses import dataclass, field

# CiA 402 objects that the drive profile handles itself (no HAL pin); the
# profile gives them its own pins (component/devices/ecat_cia402.c).
CIA402_PROFILE = {0x603F, 0x6040, 0x6041, 0x6060, 0x6061,
                  0x6064, 0x606C, 0x607A, 0x60FF}
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
}
# 0x6502 "supported drive modes", bit -> name.
CIA402_MODE_BITS = {0: "PP", 1: "VL", 2: "PV", 3: "TQ", 5: "HM", 6: "IP",
                    7: "CSP", 8: "CSV", 9: "CST"}
PROFILE_NAMES = {402: "CiA 402 drive", 5001: "Modular device (ETG.5001)"}

HAL_TYPES = {"BOOL": "bit", "SINT": "s32", "INT": "s32", "DINT": "s32",
             "USINT": "u32", "UINT": "u32", "UDINT": "u32", "REAL": "float",
             "LINT": "s64", "ULINT": "u64", "LREAL": "float"}
# The ethercat tool's CoE type names, in ESI/SII terms.
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
    role: str            # "profile", "pin" or "gap"
    pin: str = ""
    hal_type: str = ""


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
    notes: list = field(default_factory=list)


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
        missing = (need_rx | need_tx) - have
        if missing:
            p.notes.append("the default PDOs lack %s for %s; the mapping must be changed"
                           % (", ".join("0x%04X" % i for i in sorted(missing)), mode.upper()))

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
            if p.profile == 402 and e.index in CIA402_PROFILE:
                e.role = "profile"
            elif p.profile == 402 and e.index in CIA402_PINS:
                e.pin, e.hal_type = CIA402_PINS[e.index]
            else:
                # Beckhoff names a record's members "Status__Latch C valid":
                # the part before "__" repeats the record's name.
                base = kebab(e.name.split("__")[-1]) or "0x%04x-%02x" % (e.index, e.subindex)
                e.pin = "%s.%s" % (kebab(pd.name), base) if counts[e.name] > 1 else base
                e.hal_type = "bit" if e.bitlen == 1 else HAL_TYPES.get(e.data_type, "u32")
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
        p.settings = [e for (i, s), e in sorted(coe.entries.items())
                      if 0x2000 <= i < 0x6000 and e.writable and s >= 0
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
