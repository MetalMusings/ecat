"""Import an lcec XML configuration as an ecat configuration.

Generic slaves are carried over as they are: their process data, pins,
bit splits, scaling, distributed clocks and SDOs. Slaves with an lcec
product driver are not: their behaviour is in lcec's C code. If the bus can
be read, they are configured from the slave's own data instead.

Every import gives a list of notes saying what was changed or left out.
"""

import re

from . import config, propose

# lcec hal types -> (hal-type, data type for a given bit length or None)
_SIGNED = {8: "SINT", 16: "INT", 32: "DINT", 64: "LINT"}
_UNSIGNED = {8: "USINT", 16: "UINT", 32: "UDINT", 64: "ULINT"}


def _name(text, fallback):
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", text or "").strip("-.")
    return name or fallback


def _types(hal_type, bitlen, coe_type):
    """(hal-type, data type) for an lcec halType, or (None, None)."""
    if hal_type == "bit":
        return "bit", coe_type or ("BOOL" if bitlen == 1 else _UNSIGNED.get(bitlen))
    if hal_type == "s32":
        return "s32", coe_type or _SIGNED.get(bitlen)
    if hal_type == "u32":
        return "u32", coe_type or _UNSIGNED.get(bitlen)
    if hal_type == "float":
        return "float", coe_type or _SIGNED.get(bitlen)
    if hal_type == "float-unsigned":
        return "float", coe_type or _UNSIGNED.get(bitlen)
    if hal_type == "float-ieee":
        return "float", "REAL"
    if hal_type == "float-double-ieee":
        return "float", "LREAL"
    return None, None


def _entry(x, coe, where, notes):
    obj = config.obj(x.index, x.subindex)
    coe_type = propose._coe_type(coe, x.index, x.subindex) if coe else None
    if x.index == 0:
        return {"object": obj, "bits": x.bitlen, "use": "gap"}
    coe_entry = coe.entries.get((x.index, x.subindex)) if coe else None
    if coe_entry is not None and coe_entry.bitlen != x.bitlen:
        notes.append("%s: %s is mapped with %d bits in the XML file, but the slave's CoE "
                     "dictionary has %d (%s %s); imported as in the XML file, check it"
                     % (where, obj, x.bitlen, coe_entry.bitlen, coe_entry.data_type,
                        coe.name(x.index, x.subindex)))
        coe_type = None
    if x.hal_type == "complex" or x.bits:
        e = {"object": obj, "bits": x.bitlen,
             "type": coe_type or _UNSIGNED.get(x.bitlen, "UDINT")}
        split, bit = [], 0
        for b in x.bits:
            if b.bitlen == 1:
                split.append(b.pin or None)
            else:
                if b.pin:
                    notes.append("%s: %s bits %d-%d (pin %s, %s) not imported: only single "
                                 "bits can be split into pins" % (where, obj, bit, bit + b.bitlen - 1,
                                                                 b.pin, b.hal_type))
                split.extend([None] * b.bitlen)
            bit += b.bitlen
        while split and split[-1] is None:
            split.pop()
        if split:
            e["split"] = split
        return e
    hal, dtype = _types(x.hal_type, x.bitlen, coe_type)
    e = {"object": obj, "bits": x.bitlen, "type": dtype or _UNSIGNED.get(x.bitlen, "UDINT")}
    if x.pin:
        if hal is None:
            notes.append("%s: %s halType %r unknown, imported as u32" % (where, obj, x.hal_type))
            hal = "u32"
        e["pin"] = x.pin
        e["hal-type"] = hal
        if x.scale is not None:
            e["scale"] = x.scale
        if x.offset is not None:
            e["offset"] = x.offset
    return e


def _setting(index, sub, data, coe):
    s = {"object": config.obj(index, sub)}
    entry = coe.entries.get((index, sub)) if coe else None
    if entry is not None:
        s["name"] = coe.name(index, sub)
        dtype = propose.COE_TYPES.get(entry.data_type)
        if dtype in config.INTEGER_TYPES and len(data) * 8 == entry.bitlen:
            s["type"] = dtype
            s["value"] = int.from_bytes(data, "little", signed=entry.data_type.startswith("int"))
            return s
    s["data"] = data.hex(" ")
    return s


def import_lcec(xml_cfg, bus=None, servo_period=None):
    """Make an ecat configuration from an lcecxml.LcecConfig.

    bus: {position: (info, coe.Dictionary, propose.Proposal)} read from the
    bus, or None. Returns (configuration, notes).
    """
    notes = []
    master = {}
    if xml_cfg.interface:
        master["interface"] = xml_cfg.interface
    master["transport"] = xml_cfg.transport or "raw"
    if not xml_cfg.transport:
        notes.append("master: no transport in the XML file (lcec_uspace may have taken it from "
                     "LCEC_ETHERCAT_TRANSPORT); imported as raw")
    dc = {}
    if xml_cfg.ref_clock_sync_cycles is not None:
        dc["ref-clock-sync-cycles"] = abs(xml_cfg.ref_clock_sync_cycles)
        legacy = xml_cfg.ref_clock_sync_cycles < 0
        if xml_cfg.sync_to_ref_clock is not None or legacy:
            dc["servo-thread-follows-ref-clock"] = bool(xml_cfg.sync_to_ref_clock) or (
                xml_cfg.sync_to_ref_clock is None and legacy)
        if legacy and xml_cfg.sync_to_ref_clock is None:
            notes.append("master: negative refClockSyncCycles means syncToRefClock in lcec; "
                         "imported as servo-thread-follows-ref-clock: true")
    if xml_cfg.global_shift_time:
        dc["sync0-shift-ns"] = xml_cfg.global_shift_time
    if dc:
        master["dc"] = dc
    if not xml_cfg.interface:
        notes.append("master: no interface in the XML file; give it before use")
    if servo_period and xml_cfg.app_time_period and xml_cfg.app_time_period != servo_period:
        notes.append("master: appTimePeriod %d ns differs from the servo period %d ns; ecat uses "
                     "the servo period" % (xml_cfg.app_time_period, servo_period))

    slaves = []
    for x in xml_cfg.slaves:
        where = "slave %d" % x.idx
        info, coe, prop = (bus or {}).get(x.idx, (None, None, None))
        name = _name(x.name, "slave-%d" % x.idx)
        if prop and (prop.vendor_id, prop.product_code) != (x.vendor_id, x.product_code):
            notes.append("%s: the bus has 0x%08X/0x%08X, the XML file 0x%08X/0x%08X; "
                         "imported as in the XML file" % (where, prop.vendor_id, prop.product_code,
                                                         x.vendor_id, x.product_code))
            coe, prop = None, None

        if x.type != "generic":
            if prop:
                s = config.slave_from_proposal(prop, name)
                notes.append("%s: lcec type %s replaced by a configuration from the slave's own "
                             "data (profile %s); its pins differ from lcec's" % (where, x.type, s["profile"]))
            else:
                s = {"position": x.idx, "name": name, "device": x.type,
                     "identity": {"vendor": config.hex32(x.vendor_id),
                                  "product": config.hex32(x.product_code)},
                     "profile": "generic", "configure-pdos": False, "process-data": {}}
                notes.append("%s: lcec type %s has its own driver and is not imported; read the "
                             "slave from the bus to configure it" % (where, x.type))
        else:
            s = {"position": x.idx, "name": name,
                 "device": prop.name if prop else "generic",
                 "identity": {"vendor": config.hex32(x.vendor_id),
                              "product": config.hex32(x.product_code)},
                 "profile": "generic", "configure-pdos": x.config_pdos}
            if prop:
                s["identity"]["revision"] = config.hex32(prop.revision)
                if prop.profile == 402:
                    notes.append("%s: the slave is a CiA 402 drive; imported as generic so that HAL "
                                 "files that drive its control word keep working" % where)
            pd = {"outputs": [], "inputs": []}
            for p in x.pdo_list:
                direction = "outputs" if p.direction == "out" else "inputs"
                pd[direction].append({"pdo": "0x%04X" % p.index, "sm": p.sm,
                                      "entries": [_entry(e, coe, where, notes) for e in p.entries]})
            s["process-data"] = {k: v for k, v in pd.items() if v}

        if x.assign_activate is not None:
            cycles, cyc = 1, (x.sync0_cycle or "*1").strip()
            if cyc.startswith("*"):
                cycles = int(cyc[1:] or 1)
            elif servo_period and int(cyc) % servo_period == 0:
                cycles = int(cyc) // servo_period
            else:
                notes.append("%s: sync0Cycle %s ns imported as 1 servo period" % (where, cyc))
            s["dc"] = {"assign-activate": "0x%04X" % x.assign_activate,
                       "sync0-cycles": cycles, "sync0-shift-ns": x.sync0_shift or 0}
        elif x.type != "generic" and prop and "dc" in s:
            pass   # from the slave's own data
        settings = []
        for index, sub, data in x.sdos:
            if sub == "complete":
                notes.append("%s: SDO 0x%04X complete access not imported" % (where, index))
                continue
            settings.append(_setting(index, sub, data, coe))
        s["settings"] = settings
        for tag in x.unsupported:
            notes.append("%s: <%s> not imported" % (where, tag))
        slaves.append(s)
    return {"ecat": config.FORMAT_VERSION, "master": master, "slaves": slaves}, notes
