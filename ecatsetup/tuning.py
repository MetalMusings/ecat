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

Nothing is written here. A setting is tried by writing it in RAM (the
Test page's SDO card) and measuring again; one that helps belongs in the
configuration's settings, which ecat writes at every start.
"""

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
