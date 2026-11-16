"""The drive's settings that may explain a response measurement
(response.py): those its quirk file names (`tuning:`), and the others found
by name in its object dictionary, with their present values.

This is the step after the diagnosis: response.diagnose() says what kind of
setting the following error points at (response.KINDS: a command filter,
the position loop, feedforward, ...); candidates() lists the drive's
settings of those kinds first. A drive nobody has seen here yet is searched
by the names in its own dictionary ("Filter Time", "PosLoop_Kp", ...), and a
value at the end of its range is flagged: that is how the ECT60's command
filter, 0x2009 at its maximum of 25600 us, was found (2026-11-16).

A setting is tried by writing it in RAM (try_value), the drive's own value
kept first in TRIALS_FILE, and measuring again: response.compare() says what
changed. One that helps is kept as a setting of the slave (keep), which ecat
writes at every start; one that does not is put back (undo). Values still
being tried are put back when the test stops, and offered back after a
crash (undo_all). Nothing is stored in the drive's EEPROM.
"""

import json
import os
import re
from dataclasses import dataclass

from . import config, quirks

# the name of a dictionary entry -> its kind (response.KINDS); the first match wins
KEYWORDS = [
    ("gain-high", re.compile(r"notch|resonan|vibration", re.I)),
    ("feedforward", re.compile(r"feed.?forward|vff|\bff\b|speed compensation", re.I)),
    ("torque", re.compile(r"inertia", re.I)),
    ("command-filter", re.compile(r"filter|smooth|average|interpolat|jerk|s.?curve", re.I)),
    ("position-loop", re.compile(r"kp|ki\b|_ki|kd\b|_kd|gain|stiff|rigid|pos\w* loop", re.I)),
]
# names that are not about following a position: inputs, the current loop, alarms ...
NOT_MOTION = re.compile(r"\bin(put)?s?\b|\bin ?port|\bport\b|\bdi\b|\bdo\b|outputs?\b|current|iloop|"
                        r"alarm|error|home|homing|baud|address|encoder resolution", re.I)
VENDOR = range(0x2000, 0x6000)  # where drives keep their own settings
MAX_READ = 60                   # entries read at most (each is an SDO upload)


@dataclass
class Candidate:
    index: int
    subindex: int
    name: str
    kind: str
    data_type: str          # the ethercat tool's
    value: int = None       # now, None if not read
    minimum: int = None
    maximum: int = None
    factory: int = None
    unit: str = ""
    note: str = ""          # from the quirk file
    tried: str = ""         # from the quirk file: what was found on a machine
    applies: bool = True    # False: the quirk's `when` does not hold (another mode)
    from_quirk: bool = False

    @property
    def edge(self):
        """"max" or "min" when the value is at the end of its range, else None."""
        if self.value is None:
            return None
        if self.maximum is not None and self.value >= self.maximum:
            return "max"
        if self.minimum is not None and self.value <= self.minimum and self.maximum is not None:
            return "min"
        return None

    @property
    def obj(self):
        return config.obj(self.index, self.subindex)

    def why(self):
        """What to know about it, for the window."""
        parts = []
        if self.edge:
            parts.append("at the %s of its range" % ("maximum" if self.edge == "max" else "minimum"))
        if self.factory is not None and self.value is not None and self.value != self.factory:
            parts.append("factory %d" % self.factory)
        if not self.applies:
            parts.append("not used in the drive's present mode")
        if self.note:
            parts.append(self.note)
        if self.tried:
            parts.append("tried: " + self.tried)
        return "; ".join(parts)


TOOL_TYPES = {"BOOL": "bool", "SINT": "int8", "INT": "int16", "DINT": "int32",
              "USINT": "uint8", "UINT": "uint16", "UDINT": "uint32"}


def kind_of(name):
    """The kind of a dictionary entry's name, or None."""
    if not name or NOT_MOTION.search(name):
        return None
    for kind, pattern in KEYWORDS:
        if pattern.search(name):
            return kind
    return None


def _process_data(slave):
    out = set()
    for direction in ("outputs", "inputs"):
        for pdo in (slave.get("process-data") or {}).get(direction) or []:
            for e in pdo.get("entries") or []:
                try:
                    out.add(config.parse_obj(e.get("object")))
                except ValueError:
                    pass
    return out


def candidates(slave, dictionary, read=None, kinds=()):
    """The drive's settings worth a look, those of `kinds` (in that order)
    first, then the quirk file's before those found by name, then those at the
    end of their range. read(index, subindex, tool type) -> int or None gives
    the present values (None: not read)."""
    out, seen = [], set()
    commands = quirks.of(slave).get("commands", {})
    for t in quirks.tuning(slave):
        e = dictionary.entries.get((t["index"], t["subindex"])) if dictionary else None
        c = Candidate(t["index"], t["subindex"],
                      dictionary.name(t["index"], t["subindex"]) if dictionary else "",
                      t["kind"], TOOL_TYPES.get(t["type"].upper(), t["type"]),
                      minimum=t.get("min", e.minimum if e else None),
                      maximum=t.get("max", e.maximum if e else None),
                      factory=t.get("factory", e.default if e else None),
                      unit=t.get("unit", ""), note=t.get("note", ""), tried=t.get("tried", ""),
                      from_quirk=True)
        c.when = t.get("when")
        out.append(c)
        seen.add((c.index, c.subindex))
    pdo = _process_data(slave)
    for (index, sub), e in sorted((dictionary.entries if dictionary else {}).items()):
        if (index, sub) in seen or index not in VENDOR or (index, sub) in pdo or index in commands:
            continue
        if not e.writable or e.data_type not in TOOL_TYPES.values():
            continue
        if sub == 0 and dictionary.subentries(index):
            continue        # the count of an object's entries
        kind = kind_of(dictionary.name(index, sub))
        if kind is None:
            continue
        c = Candidate(index, sub, dictionary.name(index, sub), kind, e.data_type,
                      minimum=e.minimum, maximum=e.maximum, factory=e.default)
        c.when = None
        out.append(c)
    if read is not None:
        for c in out[:MAX_READ]:
            c.value = read(c.index, c.subindex, c.data_type)
            if c.when:
                wi, ws, values, wtype = c.when
                mode = read(wi, ws, wtype)
                c.applies = mode is None or mode in values
    order = list(kinds)

    def rank(c):
        return (order.index(c.kind) if c.kind in order else len(order),
                not c.applies, not c.from_quirk, c.edge is None, c.index, c.subindex)
    return sorted(out, key=rank)


# -- trying a setting: written in RAM, the original kept until it is kept or undone

TRIALS_FILE = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
                           "ecatsetup", "tuning-trials.json")
# the ethercat tool's type -> the configuration's
CONFIG_TYPES = {v: k for k, v in TOOL_TYPES.items()}


class TrialError(Exception):
    pass


def pending():
    """Settings being tried: [{"position", "slave", "index", "subindex",
    "dtype", "original", "value", "name"}], oldest first."""
    try:
        with open(TRIALS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _save(entries):
    os.makedirs(os.path.dirname(TRIALS_FILE), exist_ok=True)
    if not entries:
        if os.path.exists(TRIALS_FILE):
            os.remove(TRIALS_FILE)
        return
    tmp = TRIALS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(entries, f, indent=1)
    os.replace(tmp, TRIALS_FILE)


def _same(e, position, index, subindex):
    return e["position"] == position and e["index"] == index and e["subindex"] == subindex


def drive_enabled(slave, read):
    """Whether one of the slave's axes is in "Operation enabled" (0x6041 bit 2)."""
    for n in range(len(config.axes(slave)) or 1):
        sw = read(0x6041 + n * config.AXIS_OFFSET, 0, "uint16")
        if sw is not None and int(sw) & 4:
            return True
    return False


def try_value(slave, c, value, read, write):
    """Write value to the candidate's object in the drive (RAM), keeping the
    drive's own value first. A second try of the same object keeps the first
    original. read(index, subindex, type) -> int or None; write(index,
    subindex, value text, type). Refused while the drive is enabled, outside
    a known range, or when the drive does not take it. Returns the entry."""
    pos = slave["position"]
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise TrialError("give a whole number for %s" % c.obj)
    if (c.minimum is not None and value < c.minimum) or (c.maximum is not None and value > c.maximum):
        raise TrialError("%s takes %s..%s" % (c.obj, c.minimum, c.maximum))
    if drive_enabled(slave, read):
        raise TrialError("%s is enabled: release the buttons first; a setting is changed only "
                         "while the drive stands disabled" % slave.get("name"))
    entries = pending()
    entry = next((e for e in entries if _same(e, pos, c.index, c.subindex)), None)
    before = entry["value"] if entry is not None else None    # an earlier try, still in the drive
    if entry is None:
        original = read(c.index, c.subindex, c.data_type)
        if original is None:
            raise TrialError("cannot read %s from slave %d" % (c.obj, pos))
        entry = {"position": pos, "slave": slave.get("name"), "index": c.index,
                 "subindex": c.subindex, "dtype": c.data_type, "original": int(original),
                 "value": None, "name": c.name}
        entries.append(entry)
    entry["value"] = value
    # the original is in the file before the value is written
    _save(entries)
    try:
        write(c.index, c.subindex, str(value), c.data_type)
    except Exception as e:      # noqa: BLE001 - the drive's refusal, shown as it is
        # nothing changed in the drive: it keeps its own value, or the earlier try
        rest = [x for x in pending() if not _same(x, pos, c.index, c.subindex)]
        if before is not None:
            rest.append(dict(entry, value=before))
        _save(rest)
        raise TrialError("the drive refused %s = %d: %s" % (c.obj, value, e))
    back = read(c.index, c.subindex, c.data_type)
    if back is not None and int(back) != value:
        undo(entry, write)
        raise TrialError("%s reads %s after writing %d: the drive did not take it; put back"
                         % (c.obj, back, value))
    return entry


def undo(entry, write):
    """Put the drive's own value back; the entry leaves the file."""
    write(entry["index"], entry["subindex"], str(entry["original"]), entry["dtype"])
    _save([e for e in pending() if not _same(e, entry["position"], entry["index"], entry["subindex"])])


def keep(entry, slave):
    """Make a tried value a setting of the slave (ecat writes it at every
    start; the drive keeps it in RAM meanwhile); the entry leaves the file.
    Returns the setting."""
    obj = config.obj(entry["index"], entry["subindex"])
    st = {"object": obj, "name": entry.get("name") or obj,
          "type": CONFIG_TYPES.get(entry["dtype"], "UDINT"), "value": entry["value"]}
    sts = slave.setdefault("settings", [])
    for i, x in enumerate(sts):
        if str(x.get("object")).upper() == obj.upper():
            sts[i] = dict(x, value=entry["value"])
            st = sts[i]
            break
    else:
        sts.append(st)
    _save([e for e in pending() if not _same(e, entry["position"], entry["index"], entry["subindex"])])
    return st


def undo_all(write_for):
    """Put back every value being tried (the test stops, or what a crash left).
    write_for(position) -> write(index, subindex, value text, type). Returns
    (done, failed) as readable strings; failed entries stay in the file."""
    done, failed = [], []
    for e in list(pending()):
        text = "slave %d %s back to %d" % (e["position"], config.obj(e["index"], e["subindex"]),
                                          e["original"])
        try:
            undo(e, write_for(e["position"]))
            done.append(text)
        except Exception as x:      # noqa: BLE001 - kept in the file, said to the user
            failed.append("%s: %s" % (text, x))
    return done, failed
