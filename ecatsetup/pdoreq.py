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
