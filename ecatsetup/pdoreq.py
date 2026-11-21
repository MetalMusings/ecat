"""What a profile needs in the process data, and whether a device can give it
(docs/development.md "Own PDO mapping", the ladder: profile -> choices ->
requirement -> verdict -> ways).

The requirements are data (ecatsetup/pdoreq/*.yaml, the user's
~/.config/ecatsetup/pdoreq/); this module reads them and judges a device's
ESI description against them. Verdicts:

    ready       a way exists that needs no change of any PDO's entries
    build       an open PDO can be filled with what is needed
    impossible  something needed cannot be mapped (or the drive lacks the mode)
    unknown     not enough is known (no ESI file)

Ways to meet a requirement: "own" (the slave's mapping as it is, nothing
written; not judged here, the slave's content is not known without the bus),
"ready" (fixed PDOs / a predefined set / the default assignment, as the ESI
gives them) and "build" (own mapping in open PDOs).
"""

import dataclasses
import itertools
import os
from dataclasses import dataclass, field

import yaml

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pdoreq")
USER = os.path.expanduser("~/.config/ecatsetup/pdoreq")

READY, BUILD, IMPOSSIBLE, UNKNOWN = "ready", "build", "impossible", "unknown"

NAMES = {0x6040: "Controlword", 0x6041: "Statusword", 0x6060: "Modes of operation",
         0x6061: "Modes of operation display", 0x607A: "Target position",
         0x6064: "Position actual value", 0x60FF: "Target velocity",
         0x606C: "Velocity actual value", 0x603F: "Error code",
         0x6502: "Supported drive modes"}


def object_name(index):
    return "0x%04X %s" % (index, NAMES[index]) if index in NAMES else "0x%04X" % index


def load(profile):
    """The requirement data for a profile name ("cia402"); None if there is none."""
    folders = [os.environ["ECATSETUP_PDOREQ"]] if os.environ.get("ECATSETUP_PDOREQ") else [USER, HERE]
    for folder in folders:
        path = os.path.join(folder, profile + ".yaml")
        if os.path.exists(path):
            with open(path) as f:
                return yaml.safe_load(f)
    return None


@dataclass
class Requirement:
    rx: set = field(default_factory=set)       # needed, (index, subindex)
    tx: set = field(default_factory=set)
    good_rx: set = field(default_factory=set)  # good to have, never blocking
    good_tx: set = field(default_factory=set)
    notes: list = field(default_factory=list)


def cia402(mode, modes=(), axes=1, data=None):
    """The process data a CiA 402 drive in `mode` (and `modes`, the ones it may be
    switched to) needs."""
    data = data or load("cia402")
    chosen = [mode] + [m for m in modes if m != mode]
    r = Requirement()
    rx, tx = set(data["common"]["rx"]), set(data["common"]["tx"])
    for m in chosen:
        spec = data["modes"].get(m)
        if spec is None:
            r.notes.append("unknown mode %s" % m)
            continue
        rx |= set(spec["rx"])
        tx |= set(spec["tx"])
    if len(chosen) > 1:
        rx |= set(data["several"]["rx"])
        tx |= set(data["several"]["tx"])
    good_tx = set(data["good"]["tx"]) - tx
    shift = data["axis-offset"]
    for a in range(axes):
        r.rx |= {(i + a * shift, 0) for i in rx}
        r.tx |= {(i + a * shift, 0) for i in tx}
        r.good_tx |= {(i + a * shift, 0) for i in good_tx}
        r.good_rx |= {(i + a * shift, 0) for i in set(data["good"]["rx"]) - rx}
    return r


@dataclass
class Way:
    kind: str                # "ready" or "build"
    title: str
    rx: list = field(default_factory=list)   # PDO indexes (ready) / objects to map (build)
    tx: list = field(default_factory=list)
    sms: dict = None         # ready ways from a predefined set: sm -> PDO indexes
    lacks_good: list = field(default_factory=list)   # (index, subindex) good to have, not in it


@dataclass
class Verdict:
    status: str
    ways: list = field(default_factory=list)
    problems: list = field(default_factory=list)    # texts: why impossible / what is missing


def _have(pdos):
    return {(e.index, e.subindex) for p in pdos for e in p.entries if e.index}


def _combos(pdos, need, direction):
    """Smallest sets of fixed PDOs (no two excluding each other) that hold `need`."""
    pool = [p for p in pdos if p.direction == direction and p.fixed and (_have([p]) & need)]
    out = []
    for n in range(1, 4):
        for combo in itertools.combinations(pool, n):
            if any(a.index in b.excludes or b.index in a.excludes
                   for a, b in itertools.combinations(combo, 2)):
                continue
            if need <= _have(combo):
                out.append(combo)
        if out:
            break
    return out


def _way(title, rx, tx, req, sms=None):
    lacks = sorted((req.good_rx | req.good_tx) - (_have(rx) | _have(tx)))
    return Way(READY, title, [p.index for p in rx], [p.index for p in tx], sms, lacks)


def judge(req, esi_dev, supported=None, mode_bits=()):
    """Judge a requirement against an ESI device description.

    supported: the drive's 0x6502 (None: the ESI's default); mode_bits: (bit,
    name) of the modes chosen, each checked against it.
    """
    v = Verdict(UNKNOWN)
    if esi_dev is None:
        v.problems.append("no ESI file: what the device can map is not known")
        return v
    if not req.rx and not req.tx:
        v.status = READY
        return v
    sup = supported if supported is not None else esi_dev.drive_modes
    for bit, name in mode_bits:
        if sup is not None and not sup & (1 << bit):
            v.status = IMPOSSIBLE
            v.problems.append("the drive does not report %s in 0x6502" % name)
    if v.status == IMPOSSIBLE:
        return v

    pdos = esi_dev.pdos
    by_index = {p.index: p for p in pdos}
    # ready: the vendor's predefined sets and the default assignment, as they are
    candidates = []
    for s in esi_dev.pdo_sets:
        members = [by_index[i] for sm, ids in sorted(s.sms.items()) for i in ids if i in by_index]
        candidates.append(("predefined set \"%s\"" % s.name, members, s.sms))
    defaults = [p for p in pdos if p.sm is not None]
    if defaults:
        candidates.append(("the default assignment", defaults, None))
    for title, members, sms in candidates:
        rx = [p for p in members if p.direction == "rx"]
        tx = [p for p in members if p.direction == "tx"]
        if req.rx <= _have(rx) and req.tx <= _have(tx):
            v.ways.append(_way(title, rx, tx, req, sms))
    # ready: fixed PDOs, picked per direction
    rxs = _combos(pdos, req.rx, "rx") if req.rx else [()]
    txs = _combos(pdos, req.tx, "tx") if req.tx else [()]
    seen = {(tuple(w.rx), tuple(w.tx)) for w in v.ways}
    for rx, tx in itertools.islice(itertools.product(rxs, txs), 6):
        key = (tuple(p.index for p in rx), tuple(p.index for p in tx))
        if key in seen:
            continue
        seen.add(key)
        v.ways.append(_way("fixed PDOs " + ", ".join("0x%04X" % i for i in key[0] + key[1]),
                           rx, tx, req))
    if v.ways:
        v.status = READY
        v.ways.sort(key=lambda w: len(w.lacks_good))
    # build: an open PDO per direction, the objects mappable the right way
    problems = []
    if not esi_dev.pdo_config:
        problems.append("the device's PDOs cannot be changed (PdoConfig is %s in its ESI file)"
                        % ("off" if esi_dev.pdo_config is not None else "missing"))
    else:
        for direction, need, flag in (("rx", req.rx, "R"), ("tx", req.tx, "T")):
            if not need:
                continue
            open_pdos = [p for p in pdos if p.direction == direction and not p.fixed]
            if not open_pdos:
                problems.append("no open %s PDO to build in" % direction.upper())
                continue
            limit = esi_dev.max_entries.get(open_pdos[0].index)
            if limit is not None and len(need) > limit:
                problems.append("0x%04X takes %d entries, %d are needed"
                                % (open_pdos[0].index, limit, len(need)))
            for index, sub in sorted(need):
                got = esi_dev.mapping.get(index)
                if got is None:
                    problems.append(("%s is not in the device's dictionary" if esi_dev.mapping
                                     else "the ESI file does not say whether %s can be mapped")
                                    % object_name(index))
                elif flag not in got:
                    problems.append("%s cannot be mapped as %s data" % (object_name(index), direction.upper()))
    if not problems:
        v.ways.append(Way(BUILD, "own mapping in the open PDOs", sorted(req.rx), sorted(req.tx)))
        if v.status == UNKNOWN:
            v.status = BUILD
    elif v.status != READY:
        v.status = UNKNOWN if all("does not say" in t for t in problems) else IMPOSSIBLE
        v.problems += problems
    return v


def cia402_verdicts(esi_dev, mode, modes=(), axes=1, supported=None, data=None):
    """Judge the chosen mode set and every mode on its own:
    (Verdict, {mode: Verdict})."""
    data = data or load("cia402")

    def bits(ms):
        return [(data["modes"][m]["bit"], m.upper()) for m in ms if m in data["modes"]]
    chosen = [mode] + [m for m in modes if m != mode]
    whole = judge(cia402(mode, modes, axes, data), esi_dev, supported, bits(chosen))
    each = {m: judge(cia402(m, (), axes, data), esi_dev, supported, bits([m]))
            for m in data["modes"] if not data["modes"][m].get("only-in-modes")}
    return whole, each


@dataclass
class Status:
    level: str               # "ok", "info" or "error"
    text: str
    add: list = field(default_factory=list)   # [(index, "rx" or "tx")] that would fix it


def _objects(data, index, axes):
    """The entry description of an object, also on a later axis."""
    shift = data["axis-offset"]
    for a in range(axes):
        base = index - a * shift
        if base in data["objects"]:
            return data["objects"][base]
    return None


def cia402_status(mode, modes, axes, have, data=None):
    """The lines under a drive's PDOs: what its mode set lacks (error), and what is
    only a loss (info). `have` is the set of (index, subindex) mapped, both directions."""
    data = data or load("cia402")
    req = cia402(mode, modes, axes, data)
    chosen = [mode] + [m for m in modes if m != mode]
    out = []
    missing = [(i, "rx") for i, s in sorted(req.rx) if (i, s) not in have]
    missing += [(i, "tx") for i, s in sorted(req.tx) if (i, s) not in have]
    if missing:
        out.append(Status("error", "cannot run as CiA 402 in %s: %s missing"
                          % (", ".join(m.upper() for m in chosen),
                             ", ".join(object_name(i) for i, _ in missing)), missing))
    if len(chosen) == 1:
        for index, text in ((0x6060, "the mode is set by SDO at start"),
                            (0x6061, "the mode is not read back")):
            if (index, 0) not in have:
                out.append(Status("info", "%s is not mapped: %s" % (object_name(index), text),
                                  [(index, "rx" if index == 0x6060 else "tx")]))
    if not out:
        out.append(Status("ok", "enough for CiA 402 in %s" % ", ".join(m.upper() for m in chosen)))
    return out


@dataclass
class Plan:
    pdos: list                # EsiPdo (with sm) for the proposal
    way: Way = None           # what was done, None when the assignment was enough
    verdict: Verdict = None


def fit_cia402(esi_dev, chosen, mode, modes=(), axes=1, supported=None, data=None):
    """The PDOs for a CiA 402 drive: `chosen` (the default assignment) when it holds
    what the mode set needs, else the best ready way, else an own mapping in the
    open PDO of each direction that lacks something, else `chosen` as it is."""
    data = data or load("cia402")
    req = cia402(mode, modes, axes, data)
    have = _have(chosen)
    if (req.rx | req.tx) <= have:
        return Plan(chosen)
    chosen_modes = [mode] + [m for m in modes if m != mode]
    v = judge(req, esi_dev, supported,
              [(data["modes"][m]["bit"], m.upper()) for m in chosen_modes if m in data["modes"]])
    by_index = {p.index: p for p in esi_dev.pdos}

    def sm_of(direction, wanted=None):
        sms = [p.sm for p in chosen + esi_dev.pdos if p.direction == direction and p.sm is not None]
        return wanted if wanted is not None else (sms[0] if sms else (2 if direction == "rx" else 3))

    ready = [w for w in v.ways if w.kind == READY]
    if ready:
        w = ready[0]
        pdos = []
        if w.sms:
            for sm, ids in sorted(w.sms.items()):
                pdos += [dataclasses.replace(by_index[i], sm=sm) for i in ids if i in by_index]
        else:
            pdos = [dataclasses.replace(by_index[i], sm=sm_of(by_index[i].direction))
                    for i in w.rx + w.tx]
        return Plan(pdos, w, v)
    # an own mapping is needed only in the direction that lacks something
    partial = Requirement(
        rx=req.rx if not req.rx <= _have([p for p in chosen if p.direction == "rx"]) else set(),
        tx=req.tx if not req.tx <= _have([p for p in chosen if p.direction == "tx"]) else set(),
        good_rx=req.good_rx, good_tx=req.good_tx)
    vp = judge(partial, esi_dev, supported, [])
    build = [w for w in vp.ways if w.kind == BUILD]
    if not build:
        return Plan(chosen, None, v if v.problems else vp)
    pdos = list(chosen)
    for direction, need, good in (("rx", req.rx, req.good_rx), ("tx", req.tx, req.good_tx)):
        mapped = _have([p for p in pdos if p.direction == direction])
        if need <= mapped:
            continue
        target = next(p for p in esi_dev.pdos if p.direction == direction and not p.fixed)
        # what the open PDO holds by default stays if it is needed, then what is
        # missing, then what is only good to have; the PDO's limit cuts the last
        base = next((p for p in chosen if p.index == target.index), target)
        entries = [e for e in base.entries if (e.index, e.subindex) in need]
        have_now = {(e.index, e.subindex) for e in entries}
        entries += [e for e in base.entries if (e.index, e.subindex) not in need and e.index]
        for index, sub in sorted(need | good):
            if (index, sub) in {(e.index, e.subindex) for e in entries}:
                continue
            spec = _objects(data, index, axes)
            if spec is None:
                continue
            flag = "R" if direction == "rx" else "T"
            if esi_dev.mapping and flag not in esi_dev.mapping.get(index, ""):
                continue
            entries.append(_new_entry(index, sub, spec))
        # the needed ones first, so that the limit never cuts them
        entries.sort(key=lambda e: (e.index, e.subindex) not in need)
        limit = esi_dev.max_entries.get(target.index)
        if limit is not None:
            entries = entries[:limit]
        pdos = [p for p in pdos if p.direction != direction]
        pdos.append(dataclasses.replace(target, sm=sm_of(direction), entries=entries))
    return Plan(pdos, build[0], v)


def _new_entry(index, sub, spec):
    from .esi import EsiEntry
    return EsiEntry(index, sub, spec["bits"], spec["name"], spec["type"])


# --------------------------------------------------------------------------
# the file against the ESI

def _parse(text):
    import re
    m = re.match(r"\s*(?:0x)?([0-9A-Fa-f]+)\s*:\s*(?:0x)?([0-9A-Fa-f]+)\s*$", str(text))
    return (int(m.group(1), 16), int(m.group(2), 16)) if m else None


def _file_pdos(slave):
    """[(direction, pdo index, sm, [(index, subindex, bits)])] of a slave's process data."""
    out = []
    pd = slave.get("process-data") or {}
    for key, direction in (("outputs", "rx"), ("inputs", "tx")):
        for p in pd.get(key) or []:
            try:
                index = int(str(p.get("pdo")), 16)
            except ValueError:
                continue
            entries = []
            for e in p.get("entries") or []:
                o = _parse(e.get("object")) if isinstance(e, dict) else None
                if o is not None:
                    entries.append((o[0], o[1], e.get("bits")))
            out.append((direction, index, p.get("sm"), entries))
    return out


def check_slave(slave, esi_dev, exact=True):
    """The slave's PDOs in the file against what its ESI description allows:
    [(level, text)], level "error" ("warning" when the ESI file is for another
    revision of the device), "warning" or "note". Nothing when the PDOs are not
    written to the slave (configure-pdos: false) or there is no ESI file."""
    if esi_dev is None or not isinstance(slave, dict):
        return []
    out = []
    bad = "error" if exact else "warning"
    mine = _file_pdos(slave)
    if slave.get("configure-pdos", True) and mine:
        by_index = {p.index: p for p in esi_dev.pdos}
        names = {i for _d, i, _s, _e in mine}
        for direction, index, sm, entries in mine:
            esi_pdo = by_index.get(index)
            label = "PDO 0x%04X" % index
            if esi_pdo is None:
                out.append(("warning", "%s is not in the ESI file's PDOs" % label))
                continue
            if esi_pdo.direction != direction:
                out.append((bad, "%s is a %s PDO in the ESI file, but is listed under %s"
                            % (label, esi_pdo.direction.upper(),
                               "outputs" if direction == "rx" else "inputs")))
                continue
            theirs = [(e.index, e.subindex, e.bitlen) for e in esi_pdo.entries]
            same = [(i, s, b) for i, s, b in entries] == theirs
            if not same:
                if esi_pdo.fixed:
                    out.append((bad, "%s is fixed in the ESI file: its entries cannot be changed "
                                "(it holds %s)" % (label, _list(theirs))))
                elif esi_dev.pdo_config is False:
                    out.append((bad, "the ESI file says the device's PDOs cannot be changed "
                                "(PdoConfig is off), but %s differs from the device's own" % label))
                else:
                    limit = esi_dev.max_entries.get(index)
                    if limit is not None and len([e for e in entries if e[0]]) > limit:
                        out.append((bad, "%s takes at most %d entries, the file has %d"
                                    % (label, limit, len([e for e in entries if e[0]]))))
                    flag = "R" if direction == "rx" else "T"
                    for i, s, _b in entries:
                        got = esi_dev.mapping.get(i)
                        if i and got in ("R", "T") and got != flag:
                            out.append((bad, "%s: %s can only be mapped as %s data in the ESI file"
                                        % (label, object_name(i), "RX" if got == "R" else "TX")))
            for other in esi_pdo.excludes:
                if other in names and other != index and index < other:
                    out.append((bad, "%s and PDO 0x%04X exclude each other in the ESI file"
                                % (label, other)))
        if esi_dev.pdo_assign is False:
            theirs = {(p.sm, p.index) for p in esi_dev.pdos if p.sm is not None}
            ours = {(sm, i) for _d, i, sm, _e in mine}
            if ours != theirs:
                out.append((bad, "the ESI file says the device's PDO assignment cannot be changed "
                            "(PdoAssign is off), but the file assigns other PDOs than its default"))
    # the profile against what the device is
    if slave.get("profile") == "cia402":
        c = slave.get("cia402") or {}
        have = {(i, s) for _d, _p, _s, es in mine for i, s, _b in es}
        for st in cia402_status(c.get("mode"), [m for m in c.get("modes") or [] if m != c.get("mode")],
                                len(c.get("axes") or [1]), have):
            if st.level == "info":
                out.append(("note", st.text))
    elif slave.get("profile") == "generic" and esi_dev.profile == 402 and mine:
        have = {(i, s) for _d, _p, _s, es in mine for i, s, _b in es}
        if not [t for t in cia402_status("csp", [], 1, have) if t.level == "error"]:
            out.append(("note", "the file runs this CiA 402 drive as generic: the drive profile's "
                        "protections (enable, fault handling, mode) do not apply; its PDOs would "
                        "be enough for CiA 402 in CSP"))
    return out


def _list(entries):
    return ", ".join("0x%04X:%02X" % (i, s) for i, s, _b in entries if i) or "nothing"
