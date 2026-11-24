"""Editing a slave's process data in the configuration: add an object to a PDO,
remove an entry, add a whole PDO (docs/development.md "Own PDO mapping", step 3).

All of it works on the slave's dict (as in the file) and the ESI description of
the device, and refuses with an EditError that says why: a fixed PDO cannot be
changed, an open PDO takes only so many entries, an object that may be mapped
only as an output cannot go into an input PDO, two PDOs that exclude each other
are not both used. Nothing here needs Qt or the bus.
"""

from . import config, pdoreq, propose


class EditError(ValueError):
    pass


def _writable(slave):
    if slave.get("configure-pdos", True) is False:
        raise EditError("the PDOs are not written to this slave: its own mapping is used (chosen in "
                        "the PDO boxes), so a change here does nothing on the slave. Choose a PDO in "
                        "the PDO boxes to write the mapping again")


def _pdos(slave, direction):
    return (slave.setdefault("process-data", {}).setdefault(
        "outputs" if direction == "rx" else "inputs", []))


def _index(pdo):
    return int(str(pdo.get("pdo")), 16)


def _esi_pdo(esi_dev, index):
    return next((p for p in esi_dev.pdos if p.index == index), None) if esi_dev else None


def _used_pins(slave):
    return {e.get("pin") for d in (slave.get("process-data") or {}).values() for p in d
            for e in p.get("entries") or [] if e.get("pin")}


def _unique(slave, pin):
    used, name, n = _used_pins(slave), pin, 2
    while name in used:
        name, n = "%s-%d" % (pin, n), n + 1
    return name


def _describe(index, sub, dictionary, esi_dev, axes):
    """(name, bits, ESI type) of an object, from the dictionary, an ESI PDO, or the
    CiA 402 table; raises EditError when none knows it."""
    e = dictionary.entries.get((index, sub)) if dictionary is not None else None
    if e is not None and propose.COE_TYPES.get(e.data_type):
        return e.name, e.bitlen, propose.COE_TYPES[e.data_type]
    for p in esi_dev.pdos if esi_dev else []:
        for x in p.entries:
            if (x.index, x.subindex) == (index, sub) and index:
                return x.name, x.bitlen, x.data_type
    spec = pdoreq.load("cia402")["objects"]
    for a in range(axes):
        base = spec.get(index - a * 0x800)
        if base is not None and sub == 0:
            return base["name"], base["bits"], base["type"]
    raise EditError("%s is not in the device's dictionary or ESI file: its size is not known"
                    % pdoreq.object_name(index))


def direction_of(index, esi_dev, axes=1):
    """"rx" (an output), "tx" (an input), "rt" (either) or None (not known)."""
    flag = (esi_dev.mapping.get(index) if esi_dev else None) or ""
    if flag:
        return {"R": "rx", "T": "tx", "RT": "rt"}.get(flag.upper())
    spec = pdoreq.load("cia402")["objects"]
    for a in range(axes):
        base = spec.get(index - a * 0x800)
        if base is not None:
            return base["dir"]
    return None


def entry_for(slave, index, sub, bits, data_type, name, esi_dev=None):
    """The file's entry for an object: the profile's own objects get no pin."""
    e = {"object": config.obj(index, sub), "bits": bits}
    if index == 0:
        e["use"] = "gap"
        return e
    e["type"] = data_type
    axes = len((slave.get("cia402") or {}).get("axes") or [1])
    axis = next((a for a in range(axes) if index - a * 0x800 in propose.CIA402_PROFILE), None)
    if slave.get("profile") == "cia402" and axis is not None:
        e["use"] = "profile"
        return e
    pin = None
    axis = next((a for a in range(axes) if index - a * 0x800 in propose.CIA402_PINS), None)
    if slave.get("profile") == "cia402" and axis is not None:
        pin, e["hal-type"] = propose.CIA402_PINS[index - axis * 0x800]
        if axes > 1:
            pin = "%s-%d" % (pin, axis + 1)
    else:
        pin = propose.kebab(name.split("__")[-1]) or "0x%04x-%02x" % (index, sub)
        e["hal-type"] = "bool" if bits == 1 else propose.HAL_TYPES.get(data_type, "uint")
    e["pin"] = _unique(slave, pin)
    return e


def candidates(slave, esi_dev, dictionary):
    """Objects that may be added: [(index, subindex, name, direction)] from the ESI
    file's mapping flags (R/T), not in the process data already."""
    have = {config.parse_obj(e["object"]) for d in (slave.get("process-data") or {}).values()
            for p in d for e in p.get("entries") or [] if "object" in e}
    out = []
    for index, flag in sorted((esi_dev.mapping if esi_dev else {}).items()):
        if (index, 0) in have or index < 0x2000 or index >= 0xA000:
            continue
        name = (dictionary.name(index, 0) if dictionary is not None else "") or pdoreq.NAMES.get(index, "")
        out.append((index, 0, name, {"R": "rx", "T": "tx"}.get(flag.upper(), "rt")))
    return out


def add_object(slave, esi_dev, dictionary, index, sub=0, direction=None, pdo=None):
    """Add an object to an open PDO of the right direction; returns the entry.
    `direction` ("rx"/"tx") is needed only for an object that may be mapped both
    ways, `pdo` (an index) to choose which PDO."""
    _writable(slave)
    axes = len((slave.get("cia402") or {}).get("axes") or [1])
    way = direction_of(index, esi_dev, axes)
    if way in ("rx", "tx"):
        if direction not in (None, way):
            raise EditError("%s can only be mapped as %s data"
                            % (pdoreq.object_name(index), "RX (output)" if way == "rx" else "TX (input)"))
        direction = way
    elif direction is None:
        raise EditError("say whether %s goes into an output or an input PDO" % pdoreq.object_name(index))
    name, bits, dtype = _describe(index, sub, dictionary, esi_dev, axes)
    target = None
    for p in _pdos(slave, direction):
        if pdo is not None and _index(p) != pdo:
            continue
        ep = _esi_pdo(esi_dev, _index(p))
        if ep is not None and ep.fixed:
            if pdo is not None:
                raise EditError("PDO 0x%04X is fixed in the ESI file: its entries cannot be changed"
                                % _index(p))
            continue
        target = p
        break
    if target is None and not _pdos(slave, direction):
        # nothing of this direction in the file: the device's first open PDO, when it has one
        open_pdo = next((p for p in addable_pdos(slave, esi_dev)
                         if p.direction == direction and not p.fixed), None)
        if open_pdo is not None:
            target = add_pdo(slave, esi_dev, open_pdo.index)
    if target is None and pdo is not None:
        raise EditError("PDO 0x%04X is not among the %s PDOs in the file"
                        % (pdo, "output" if direction == "rx" else "input"))
    if target is None:
        fixed = [_index(p) for p in _pdos(slave, direction)]
        if fixed:
            raise EditError("the %s PDOs %s are fixed in the ESI file: choose the open PDO "
                            "(or another) in the %s PDO box, or add an open PDO"
                            % ("output" if direction == "rx" else "input",
                               ", ".join("0x%04X" % i for i in fixed),
                               "Output" if direction == "rx" else "Input"))
        raise EditError("no %s PDO in the file to add to: use Add PDO" % ("output" if direction == "rx" else "input"))
    limit = esi_dev.max_entries.get(_index(target)) if esi_dev else None
    if limit is not None and len(target.get("entries") or []) >= limit:
        raise EditError("PDO %s takes at most %d entries" % (target.get("pdo"), limit))
    entry = entry_for(slave, index, sub, bits, dtype, name, esi_dev)
    target.setdefault("entries", []).append(entry)
    return entry


def remove_entry(slave, esi_dev, direction, pdo_pos, entry_pos):
    """Remove an entry (direction "outputs"/"inputs", the PDO's and the entry's
    position in the file); an empty PDO goes with it."""
    _writable(slave)
    pdos = (slave.get("process-data") or {}).get(direction) or []
    pdo = pdos[pdo_pos]
    ep = _esi_pdo(esi_dev, _index(pdo))
    if ep is not None and ep.fixed:
        raise EditError("PDO %s is fixed in the ESI file: its entries cannot be changed" % pdo.get("pdo"))
    gone = pdo["entries"].pop(entry_pos)
    if not pdo["entries"]:
        pdos.pop(pdo_pos)
    return gone


def addable_pdos(slave, esi_dev):
    """The ESI file's PDOs that may be added: not in the file, not excluded by one
    that is, and only where the device lets its assignment change."""
    if esi_dev is None or esi_dev.pdo_assign is False:
        return []
    mine = {_index(p) for d in ("outputs", "inputs") for p in (slave.get("process-data") or {}).get(d) or []}
    excluded = {x for i in mine for x in (_esi_pdo(esi_dev, i).excludes if _esi_pdo(esi_dev, i) else [])}
    return [p for p in esi_dev.pdos if p.index not in mine and p.index not in excluded
            and not (set(p.excludes) & mine)]


def add_pdo(slave, esi_dev, index):
    """Add one of the ESI file's PDOs with its entries; returns the PDO's dict."""
    _writable(slave)
    if esi_dev is None:
        raise EditError("no ESI file for this device: a PDO's entries are not known")
    ep = _esi_pdo(esi_dev, index)
    if ep is None:
        raise EditError("PDO 0x%04X is not in the ESI file" % index)
    if ep not in addable_pdos(slave, esi_dev):
        raise EditError("PDO 0x%04X is already used, excludes (or is excluded by) a PDO in the "
                        "file, or the device's assignment cannot be changed" % index)
    sms = [p.sm for p in esi_dev.pdos if p.direction == ep.direction and p.sm is not None]
    sm = ep.sm if ep.sm is not None else (sms[0] if sms else (2 if ep.direction == "rx" else 3))
    entries = [entry_for(slave, e.index, e.subindex, e.bitlen, e.data_type, e.name, esi_dev)
               for e in ep.entries]
    pdo = {"pdo": "0x%04X" % index, "sm": sm, "entries": entries}
    _pdos(slave, ep.direction).append(pdo)
    return pdo


def add_missing(slave, esi_dev, dictionary, adds):
    """Add what pdoreq.cia402_status says is missing: adds is [(index, "rx"/"tx")].
    Returns (entries added, texts of what could not be added)."""
    added, failed = [], []
    if any(d == "setting" for _i, d in adds):
        # the mode as a setting (not a PDO edit: also where the PDOs are not written)
        mode = (slave.get("cia402") or {}).get("mode")
        for index, d in adds:
            if d != "setting" or mode not in config.MODE_NUMBERS:
                continue
            row = {"at": "PS", "object": config.obj(index, 0), "from": "profile"}
            slave.setdefault("init-cmds", []).append(row)
            added.append(row)
        adds = [(i, d) for i, d in adds if d != "setting"]
        if not adds:
            return added, failed
    _writable(slave)
    for index, direction in adds:
        try:
            added.append(add_object(slave, esi_dev, dictionary, index, 0, direction))
        except EditError as e:
            failed.append(str(e))
    return added, sorted(set(failed))
