"""Home and limit switches of a CiA 402 drive, as the standard has them.

0x60FD (Digital inputs, ETG.6010 / CiA 402): bit 0 the negative limit switch,
bit 1 the positive, bit 2 the home switch. That much is the same on every
drive, and it is all the program knows of switches. Which physical input is
which switch, and its polarity, every maker sets up in objects of its own:
the drive's manual says which, the user sets them under Settings and watches
them on the Workbench (the SDO card's watch list, the pins).

No Qt here: the Workbench's Switches card and the homing card use it.
"""

from . import config, homing

# 0x60FD bits of the standard, and the homing catalogue's names for them
FUNCTIONS = (("home", 2, "home switch"), ("negative", 0, "negative limit switch"),
             ("positive", 1, "positive limit switch"))
BIT = {kind: bit for kind, bit, _ in FUNCTIONS}
TEXT = {kind: text for kind, _, text in FUNCTIONS}
NEEDS = {text: kind for kind, _, text in FUNCTIONS}


def guide(need):
    """What the homing card says about a method's switches (need: ["home", ...])."""
    what = " and the ".join(TEXT[k] for k in need)
    bits = ", ".join("%s bit %d" % (TEXT[k], BIT[k]) for k in need)
    return ("Switches: this method needs the %s. The drive must have it as 0x60FD %s: "
            "which input is the %s, and whether it is normally open or closed, is set in "
            "the maker's own objects (the drive's manual, under digital inputs; set them "
            "under Settings). On the Workbench, the Switches card's box lights up while "
            "the drive sees the switch pressed." % (what, bits, what))


def entry(slave):
    for pdo in ((slave or {}).get("process-data") or {}).get("inputs") or []:
        for e in pdo.get("entries") or []:
            try:
                index, sub = config.parse_obj(e.get("object"))
            except ValueError:
                continue
            if (index, sub) == (0x60FD, 0):
                return e
    return None


def input_pin(slave):
    """The pin of 0x60FD in the slave's process data (as the file names it),
    or None when it is not mapped or has no pin of its own."""
    return (entry(slave) or {}).get("pin")


def sources(slave):
    """Where each switch is read, {kind: (pin, bit)}: the bit of 0x60FD's own
    pin, or (pin, None) for a bit field's pin of that bit. A switch whose bit has
    no pin is left out; {} when 0x60FD is not mapped."""
    e = entry(slave)
    if e is None:
        return {}
    if e.get("pin"):
        return {kind: (e["pin"], bit) for kind, bit, _ in FUNCTIONS}
    split = e.get("split") or []
    return {kind: (split[bit], None) for kind, bit, _ in FUNCTIONS
            if bit < len(split) and split[bit]}


def read(src, v):
    """Each switch as v (a pin's value as text, or None) shows it: {kind: bool};
    a switch whose pin gives nothing is left out."""
    out = {}
    for kind, (pin, bit) in src.items():
        raw = v(pin)
        if raw is None:
            continue
        raw = raw.strip()
        if bit is None:
            out[kind] = raw.upper() in ("TRUE", "1")
            continue
        try:
            out[kind] = bool(int(raw, 0) >> bit & 1)
        except ValueError:
            pass
    return out


def needed(slave, axis=0):
    """The switches the chosen homing method needs: ["home", "negative", ...]."""
    chosen = homing.choice(slave, axis)
    if not chosen:
        return []
    m = homing.method(chosen["method"], slave)
    return [NEEDS[n] for n in m.needs if n in NEEDS]


def state(value):
    """What 0x60FD says now: {"home": bool, "negative": ..., "positive": ...}."""
    return {kind: bool(value >> bit & 1) for kind, bit, _ in FUNCTIONS}
