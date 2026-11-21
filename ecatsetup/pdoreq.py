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


_loaded = {}


def load(profile):
    """The requirement data for a profile name ("cia402"); None if there is none.
    Read once per file (and again when the file changes)."""
    folders = [os.environ["ECATSETUP_PDOREQ"]] if os.environ.get("ECATSETUP_PDOREQ") else [USER, HERE]
    for folder in folders:
        path = os.path.join(folder, profile + ".yaml")
        try:
            stamp = os.stat(path).st_mtime_ns
        except OSError:
            continue
        if _loaded.get(path, (None,))[0] != stamp:
            with open(path) as f:
                _loaded[path] = (stamp, yaml.safe_load(f))
        return _loaded[path][1]
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
    set_name: str = None     # the predefined set's name (a ready way from one)
    lacks_good: list = field(default_factory=list)   # (index, subindex) good to have, not in it
    why: str = ""            # what it gives, for the window


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
    return Way(READY, title, [p.index for p in rx], [p.index for p in tx], sms=sms, lacks_good=lacks)


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
        candidates.append(("predefined set \"%s\"" % s.name, members, s.sms, s.name))
    defaults = [p for p in pdos if p.sm is not None]
    if defaults:
        candidates.append(("the default assignment", defaults, None, None))
    for title, members, sms, name in candidates:
        rx = [p for p in members if p.direction == "rx"]
        tx = [p for p in members if p.direction == "tx"]
        if req.rx <= _have(rx) and req.tx <= _have(tx):
            v.ways.append(_way(title, rx, tx, req, sms))
            v.ways[-1].set_name = name
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


MODE_NUMBERS = {"pp": 1, "pv": 3, "hm": 6, "csp": 8, "csv": 9}


def initcmd_mode(esi_dev):
    """The value an ESI file's InitCmd writes to 0x6060 at start, or None."""
    for cmd in getattr(esi_dev, "init_cmds", None) or []:
        if cmd[1] == 0x6060 and cmd[2] == 0 and cmd[3]:
            try:
                return int.from_bytes(bytes.fromhex(cmd[3].replace(" ", "")), "little", signed=True)
            except ValueError:
                return None
    return None


def cia402_status(mode, modes, axes, have, data=None, mode_settings=None, esi_mode=None):
    """The lines under a drive's PDOs: what its mode set lacks (error), and what is
    only a loss (info). `have` is the set of (index, subindex) mapped, both directions;
    `mode_settings` {axis: value of its setting 0x6060:00}; `esi_mode` the value its
    ESI file's InitCmd writes to 0x6060 (initcmd_mode), if it has one."""
    data = data or load("cia402")
    if mode not in data["modes"]:
        return []            # no or an unknown mode: config.check says so
    modes = [m for m in modes or [] if m in data["modes"]]
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
        # one mode: set every cycle, or once as a setting; seen, or not
        shift = data["axis-offset"]
        settings = mode_settings or {}
        for a in range(axes):
            mo, md = 0x6060 + a * shift, 0x6061 + a * shift
            if (mo, 0) in have:
                continue
            value = settings.get(a)
            if value is not None:
                out.append(Status("info", "0x%04X is not mapped: the mode is written at every start "
                                  "(0x%04X:00 = %d)" % (mo, mo, value)))
            elif (md, 0) in have:
                out.append(Status("info", "0x%04X is not mapped and not a setting: the drive must already "
                                  "be in %s (the profile enables it only when 0x%04X shows that); add the "
                                  "setting to put it there" % (mo, mode.upper(), md), [(mo, "setting")]))
            elif esi_mode is not None and a == 0:
                out.append(Status("error", "the drive's mode is neither set nor seen: its ESI file writes "
                                  "0x6060 = %d at start (an InitCmd), which this file does not have yet; "
                                  "\"Add what the device needs\" (Settings) adds it, as 0x6060:00 = %d for %s, "
                                  "or map 0x%04X or 0x%04X" % (esi_mode, MODE_NUMBERS[mode], mode.upper(), mo, md),
                                  [(mo, "setting")]))
            else:
                out.append(Status("error", "the drive's mode is neither set nor seen: add the setting "
                                  "0x%04X:00 = %d, or map 0x%04X or 0x%04X" % (mo, MODE_NUMBERS[mode], mo, md),
                                  [(mo, "setting")]))
            if (md, 0) not in have:
                out.append(Status("info", "%s is not mapped: the mode is not read back"
                                  % object_name(md) if a == 0 else "0x%04X is not mapped" % md))
    if not out:
        out.append(Status("ok", "enough for CiA 402 in %s" % ", ".join(m.upper() for m in chosen)))
    return out


@dataclass
class Plan:
    pdos: list                # EsiPdo (with sm) for the proposal
    way: Way = None           # what was done, None when the assignment was enough
    verdict: Verdict = None


def fit_cia402(esi_dev, chosen, mode, modes=(), axes=1, supported=None, data=None, choose=None):
    """The PDOs for a CiA 402 drive: `chosen` (the default assignment) when it holds
    what the mode set needs, else the best ready way, else an own mapping in the
    open PDO of each direction that lacks something, else `chosen` as it is.
    `choose`: the title of the way to take instead of the best one."""
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

    ready = [w for w in v.ways if w.kind == READY and choose in (None, w.title)]
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
    build = [w for w in vp.ways if w.kind == BUILD and choose in (None, w.title)]
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
            for other in sorted(names):
                o = by_index.get(other)
                if other > index and (other in esi_pdo.excludes or (o is not None and index in o.excludes)):
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
                                len(c.get("axes") or [1]), have, mode_settings=mode_settings(slave)):
            if st.level == "info":
                out.append(("note", st.text))
    elif slave.get("profile") == "generic" and esi_dev.profile == 402 and mine:
        have = {(i, s) for _d, _p, _s, es in mine for i, s, _b in es}
        req = cia402("csp")
        if (req.rx | req.tx) <= have:
            out.append(("note", "the file runs this CiA 402 drive as generic: the drive profile's "
                        "protections (enable, fault handling, mode) do not apply; its PDOs would "
                        "be enough for CiA 402 in CSP"))
    out += encoder_status(slave, esi_dev)
    out += serial_status(slave, esi_dev)
    return out


def mode_settings(slave):
    """{axis: value} of a drive's settings 0x6060:00."""
    from . import config
    axes = len((slave.get("cia402") or {}).get("axes") or [1])
    return {a: v for a in range(axes) for v in [config.mode_setting(slave, a)] if v is not None}


def _list(entries):
    return ", ".join("0x%04X:%02X" % (i, s) for i, s, _b in entries if i) or "nothing"


# --------------------------------------------------------------------------
# MDP: an incremental encoder channel (ETG.5001 511)

def mdp_encoder(base, uses=("position",), data=None):
    """The process data an encoder channel at status index `base` needs for its uses."""
    data = data or load("mdp-encoder")
    r = Requirement()
    for use in uses:
        spec = data["uses"][use]
        r.tx |= {(base, s) for s in spec.get("in", [])}
        r.rx |= {(base + data["output-offset"], s) for s in spec.get("out", [])}
    return r


def encoder_status(slave, esi_dev=None, data=None):
    """Notes on each encoder channel of a slave: which uses the mapping does not give
    (the pins that are then missing), and which PDO assignment of the device does."""
    data = data or load("mdp-encoder")
    have = {(i, s) for _d, _p, _s, es in _file_pdos(slave) for i, s, _b in es}
    out = []
    for ch in slave.get("encoders") or []:
        try:
            base = int(str(ch.get("index")), 16)
        except ValueError:
            continue
        for use in ("index", "latch"):
            req = mdp_encoder(base, (use,), data)
            lacks = sorted((req.rx | req.tx) - have)
            if not lacks:
                continue
            text = "encoder %s: no %s (%s): %s not mapped" % (
                ch.get("name"), data["uses"][use]["title"], data["uses"][use]["pins"],
                ", ".join("0x%04X:%02X" % x for x in lacks))
            v = judge(req, esi_dev) if esi_dev is not None else None
            sets = [w.title for w in v.ways if w.kind == READY] if v else []
            if not sets:
                continue        # the device has no way to give it (an EL5151 has no external latch)
            text += "; the device's %s has it" % ", ".join(sets[:2])
            out.append(("note", text))
    return out


# --------------------------------------------------------------------------
# MDP: a serial channel (ETG.5001 600)

def serial_sets(esi_dev):
    """The device's predefined sets that give a serial channel: [(set name, data bytes of its
    first channel)], the largest first."""
    from . import propose
    if esi_dev is None:
        return []
    by_index = {p.index: p for p in esi_dev.pdos}
    out = []
    for st in esi_dev.pdo_sets:
        members = [by_index[i] for _sm, ids in sorted(st.sms.items()) for i in ids if i in by_index]
        found = propose.serial_channels(members)
        if found:
            out.append((st.name, sum(e.bitlen for e in found[0][0].entries) // 8 - 2))
    return sorted(out, key=lambda x: (-x[1], not x[0].startswith("Standard")))


def serial_status(slave, esi_dev, data=None):
    """Notes on a slave's serial channels: no channel at all in the mapping of a device that
    has a serial profile, or fewer data bytes per cycle than the device's best set gives."""
    data = data or load("mdp-serial")
    if esi_dev is None or data["sub-profile"] not in esi_dev.sub_profiles:
        return []
    sets = serial_sets(esi_dev)
    if not sets:
        return []
    best_name, best = sets[0]
    mine = {p: es for _d, p, _s, es in _file_pdos(slave)}
    channels = slave.get("serial") or []
    if not channels:
        return [("note", "no serial port: the file's PDOs have no serial channel (the serial function "
                 "handles up to %d data bytes per cycle); the device's %s has one"
                 % (data["max-data-bytes"], "set \"%s\"" % best_name))]
    out = []
    for ch in channels:
        try:
            have = sum(b or 0 for _i, _s, b in mine.get(int(str(ch.get("input-pdo")), 16), [])) // 8 - 2
        except ValueError:
            continue
        if 0 < have < best:
            out.append(("note", "serial %s: %d data bytes per cycle; the device's set \"%s\" gives %d, "
                        "so a frame goes out in fewer cycles" % (ch.get("name"), have, best_name, best)))
    return out


# --------------------------------------------------------------------------
# the ways to show for a slave as it is in the file (the window's "Ways to get this")

def _sets_with(esi_dev, has):
    """The device's predefined sets whose PDOs `has` accepts: [(name, members)]."""
    by_index = {p.index: p for p in esi_dev.pdos}
    out = []
    for st in esi_dev.pdo_sets:
        members = [by_index[i] for _sm, ids in sorted(st.sms.items()) for i in ids if i in by_index]
        if has(members):
            out.append((st.name, members))
    return out


def ways_for(slave, esi_dev, supported=None):
    """What the user can do about the slave's PDOs as the file has them: [Way], the
    recommended one first, each with `why` set. Nothing when they already give what the
    profile and its choices need, or when the ESI file says too little."""
    if esi_dev is None or not isinstance(slave, dict):
        return []
    from . import propose
    have = {(i, s) for _d, _p, _s, es in _file_pdos(slave) for i, s, _b in es}
    out = []
    if slave.get("profile") == "cia402":
        c = slave.get("cia402") or {}
        data = load("cia402")
        mode = c.get("mode")
        if mode not in data["modes"]:
            return []
        modes = [m for m in c.get("modes") or [] if m != mode and m in data["modes"]]
        axes = len(c.get("axes") or [1])
        req = cia402(mode, modes, axes, data)
        if (req.rx | req.tx) <= have:
            return []
        chosen = [mode] + modes
        v = judge(req, esi_dev, supported,
                  [(data["modes"][m]["bit"], m.upper()) for m in chosen])
        for w in v.ways:
            lacks = ", ".join(object_name(i) for i, _s in w.lacks_good)
            w.why = "has what %s needs%s" % (", ".join(m.upper() for m in chosen),
                                             "; not %s (good to have)" % lacks if lacks else "")
            if w.kind == BUILD:
                w.why = "maps what %s needs in the device's open PDOs" % ", ".join(m.upper() for m in chosen)
            out.append(w)
        return out
    enc = load("mdp-encoder")
    if enc["sub-profile"] in esi_dev.sub_profiles:
        channels = slave.get("encoders") or []
        if not channels:
            for name, _m in _sets_with(esi_dev, lambda ms: propose.encoder_channels(
                    [e for p in ms for e in p.entries], True)):
                out.append(Way(READY, "predefined set \"%s\"" % name, set_name=name))
                out[-1].why = "has an encoder channel"
        for ch in channels:
            try:
                base = int(str(ch.get("index")), 16)
            except ValueError:
                continue
            for use in ("index", "latch"):
                req = mdp_encoder(base, (use,), enc)
                if (req.rx | req.tx) <= have:
                    continue
                for w in judge(req, esi_dev).ways:
                    if w.kind == READY and w.set_name and w.set_name not in [x.set_name for x in out]:
                        w.why = "gives encoder %s its %s" % (ch.get("name"), enc["uses"][use]["title"])
                        out.append(w)
    ser = load("mdp-serial")
    if ser["sub-profile"] in esi_dev.sub_profiles:
        best = serial_sets(esi_dev)
        notes = serial_status(slave, esi_dev, ser)
        if best and notes:
            w = Way(READY, "predefined set \"%s\"" % best[0][0], set_name=best[0][0])
            w.why = "a serial channel with %d data bytes per cycle" % best[0][1]
            out.append(w)
    return out
