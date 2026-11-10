"""The CiA 402 homing methods (0x6098): what each one does, which of them a
drive supports, and the settings a chosen method needs.

The catalog is the same for every drive. What a drive supports comes from the
drive itself (0x60E3, if it has that object) or from its quirk file
(`homing: methods`); the quirk file wins, because some drives do not report
it right. A drive that needs more than the standard objects for a method
(the ECT60's torque homing) gives `homing: extras` for it. See
docs/quirks.md.
"""

from . import config, quirks, tool

# What a method needs from the machine
LIMIT_NEG, LIMIT_POS, HOME_SWITCH, INDEX, STOP = \
    "negative limit switch", "positive limit switch", "home switch", "index pulse", "mechanical stop"


class Method:
    def __init__(self, number, text, needs=(), moves=True, uses_index=False):
        self.number = number
        self.text = text
        self.needs = tuple(needs)
        self.moves = moves
        self.uses_index = uses_index

    def __repr__(self):
        return "Method(%d)" % self.number


def _build():
    m = {}

    def add(n, text, needs=(), moves=True, index=False):
        m[n] = Method(n, text, needs, moves, index)

    add(1, "Negative limit switch, then the index pulse", (LIMIT_NEG, INDEX), index=True)
    add(2, "Positive limit switch, then the index pulse", (LIMIT_POS, INDEX), index=True)
    for n in (3, 4):
        add(n, "Home switch (positive side), then the index pulse", (HOME_SWITCH, INDEX), index=True)
    for n in (5, 6):
        add(n, "Home switch (negative side), then the index pulse", (HOME_SWITCH, INDEX), index=True)
    for n in range(7, 11):
        add(n, "Home switch, starting positive, reversing at the positive limit switch, "
               "then the index pulse", (HOME_SWITCH, LIMIT_POS, INDEX), index=True)
    for n in range(11, 15):
        add(n, "Home switch, starting negative, reversing at the negative limit switch, "
               "then the index pulse", (HOME_SWITCH, LIMIT_NEG, INDEX), index=True)
    add(17, "Negative limit switch", (LIMIT_NEG,))
    add(18, "Positive limit switch", (LIMIT_POS,))
    for n in (19, 20):
        add(n, "Home switch (positive side)", (HOME_SWITCH,))
    for n in (21, 22):
        add(n, "Home switch (negative side)", (HOME_SWITCH,))
    for n in range(23, 27):
        add(n, "Home switch, starting positive, reversing at the positive limit switch",
            (HOME_SWITCH, LIMIT_POS))
    for n in range(27, 31):
        add(n, "Home switch, starting negative, reversing at the negative limit switch",
            (HOME_SWITCH, LIMIT_NEG))
    add(33, "Index pulse, negative direction", (INDEX,), index=True)
    add(34, "Index pulse, positive direction", (INDEX,), index=True)
    add(35, "The current position is home; the axis does not move", moves=False)
    add(36, "Manufacturer specific (the drive's manual says what it does)")
    add(37, "Manufacturer specific, or in newer drives the current position "
            "(the drive's manual says which)")
    return m


CATALOG = _build()
# The numbers below 0 are manufacturer specific, and 15, 16, 31, 32 are reserved.


def method(number, slave=None):
    """A catalog entry, with the drive's own text and needs from its quirk file
    laid over it. A number that is in neither gives a manufacturer-specific entry."""
    base = CATALOG.get(number)
    over = (quirks.of(slave).get("homing") or {}).get("describe", {}).get(number) if slave else None
    if base is None:
        base = Method(number, "Manufacturer specific (the drive's manual says what it does)")
    if over:
        base = Method(number, over.get("text", base.text),
                      over.get("needs", base.needs), over.get("moves", base.moves),
                      over.get("index", base.uses_index))
    return base


def supported(slave, axis=0, read=None):
    """(methods, where) for a slave: a sorted list of numbers and "quirk file" or
    "drive (0x60E3)"; or (None, None) when neither knows, and every method in
    the catalog may be tried. `read(index, sub, type)` reads an SDO (None if
    the drive refuses); tool.upload by default."""
    listed = (quirks.of(slave).get("homing") or {}).get("methods")
    if listed:
        return sorted(listed), "quirk file"
    read = read or (lambda i, s, t: tool.upload(slave["position"], i, s, t))
    base = 0x60E3 + axis * config.AXIS_OFFSET
    count = read(base, 0, "uint8")
    if not count:
        return None, None
    # Each sub-index of 0x60E3 holds a supported method number (CiA 402).
    # Not tried on a drive: the ECT60 has no 0x60E3.
    found = set()
    for sub in range(1, count + 1):
        v = read(base, sub, "int16")
        if v:
            found.add(v)
    return (sorted(found), "drive (0x60E3)") if found else (None, None)


def choices(slave, axis=0, read=None):
    """The list for the window: [(Method, supported)] for every method of the
    catalog plus those a quirk file adds; supported is True, False, or None
    when nothing says."""
    nums, _ = supported(slave, axis, read)
    every = sorted(set(CATALOG) | set(nums or ()))
    return [(method(n, slave), None if nums is None else n in nums) for n in every]


# Parameters of a method (drive units), and the objects they go to
PARAMS = (("fast", 0x6099, 1, "uint32", "speed towards the switch"),
          ("slow", 0x6099, 2, "uint32", "speed from the switch to the index or the edge"),
          ("accel", 0x609A, 0, "int32", "acceleration"),
          ("offset", 0x607C, 0, "int32", "home offset"))


def parameters(number, slave=None):
    """Which of fast, slow, accel, offset a method uses."""
    m = method(number, slave)
    if not m.moves:
        return ["offset"]
    return ["fast", "slow", "accel", "offset"]


def plan(slave, number, params=None, axis=0):
    """The settings that make the drive home by a method: [{"object", "type",
    "value", "name"}] for the slave's `settings`. params: {"fast": ...} for
    those the method uses; the others are left out. The drive's own extras for
    the method (its quirk file) come last."""
    params = params or {}
    off = axis * config.AXIS_OFFSET
    out = [{"object": "0x%04X:%02X" % (0x6098 + off, 0), "type": "SINT", "value": number,
            "name": "Homing method"}]
    used = parameters(number, slave)
    for key, index, sub, dtype, text in PARAMS:
        if key in used and params.get(key) is not None:
            out.append({"object": "0x%04X:%02X" % (index + off, sub),
                        "type": {"uint32": "UDINT", "int32": "DINT"}[dtype],
                        "value": int(params[key]), "name": text})
    extras = ((quirks.of(slave).get("homing") or {}).get("extras") or {}).get(number, [])
    for e in extras:
        index, _, sub = e["object"].partition(":")
        out.append(dict(e, object="0x%04X:%s" % (int(index, 16) + off, sub)))
    return out
