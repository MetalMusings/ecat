"""Home and limit switches of a CiA 402 drive: reading them, testing them, and
finding where a drive sets them up.

What a switch does is read from the drive's digital inputs, 0x60FD (ETG.6010
/ CiA 402): bit 0 the negative limit switch, bit 1 the positive, bit 2 the
home switch, bits 16 and up the maker's own (most drives: the physical
inputs). That part is the same on every drive, so the test works on drives
we do not know. Which input is which switch, and its polarity, every maker
sets up in objects of its own (ECT60: 0x2007, 0x2008; EC1: 0x2702, 0x2701;
A6: 0x2004): input_settings() looks for them by name in the drive's
dictionary. A drive's quirk file may name the physical input bits
(`homing: input-bits`, such as IN5: 20).

No Qt here: the Workbench's Switches card and the homing card use it.
"""

import re

from . import config, homing, quirks

# 0x60FD bits of the standard, and the homing catalogue's names for them
FUNCTIONS = (("home", 2, "home switch"), ("negative", 0, "negative limit switch"),
             ("positive", 1, "positive limit switch"))
BIT = {kind: bit for kind, bit, _ in FUNCTIONS}
TEXT = {kind: text for kind, _, text in FUNCTIONS}
NEEDS = {text: kind for kind, _, text in FUNCTIONS}

GUIDE = ("The drive must know which of its inputs is the home switch (and the limit "
         "switches), and whether the switch is normally open or closed. Most drives set this "
         "per input in objects of the maker's own (0x2000 to 0x5FFF): a function for each input "
         "(home switch, limits, probe ...) and a polarity, for each input or as one bit per "
         "input. See the drive's manual under digital inputs. Then try the switch on the "
         "Workbench (Switches card, while the workbench runs): it tells whether the drive sees it.")


def input_pin(slave):
    """The pin of 0x60FD in the slave's process data (as the file names it),
    or None when it is not mapped or has no pin."""
    for pdo in ((slave or {}).get("process-data") or {}).get("inputs") or []:
        for e in pdo.get("entries") or []:
            try:
                index, sub = config.parse_obj(e.get("object"))
            except ValueError:
                continue
            if (index, sub) == (0x60FD, 0):
                return e.get("pin")
    return None


def input_names(slave):
    """{bit: name} of the drive's physical inputs in 0x60FD, from its quirk file."""
    bits = (quirks.of(slave).get("homing") or {}).get("input-bits") or {}
    return {int(b): str(n) for n, b in bits.items()}


def bit_name(bit, slave):
    for kind, b, text in FUNCTIONS:
        if b == bit:
            return text
    return input_names(slave).get(bit, "input bit %d" % bit)


def needed(slave, axis=0):
    """The switches the chosen homing method needs: ["home", "negative", ...]."""
    chosen = homing.choice(slave, axis)
    if not chosen:
        return []
    m = homing.method(chosen["method"], slave)
    return [NEEDS[n] for n in m.needs if n in NEEDS]


def state(value, slave):
    """What 0x60FD says now: {"home": bool, "negative": ..., "positive": ...,
    "inputs": [names of the physical inputs that are on (bits 16 and up)]}."""
    out = {kind: bool(value >> bit & 1) for kind, bit, _ in FUNCTIONS}
    out["inputs"] = [bit_name(b, slave) for b in range(16, 32) if value >> b & 1]
    return out


def verdict(kind, released, pressed, slave):
    """What a switch test found, from 0x60FD with the switch released and
    pressed: (level, text), level "ok", "warning" or "error"."""
    bit = BIT[kind]
    what = TEXT[kind]
    changed = (released ^ pressed) & 0xFFFFFFFF
    on_released = bool(released >> bit & 1)
    on_pressed = bool(pressed >> bit & 1)
    if not changed:
        return ("error", "Nothing changed in the drive's inputs (0x60FD): the switch does not reach "
                "the drive. Check the wiring, the inputs' supply (such as +24 V on their common), "
                "and that the switch goes to an input of this drive.")
    if on_pressed and not on_released:
        return ("ok", "Set up right: the drive sees the %s when it is pressed (0x60FD bit %d)."
                % (what, bit))
    if on_released and not on_pressed:
        return ("warning", "The %s is on while the switch is released and goes off when it is "
                "pressed: the polarity is the other way round. Set the input normally closed "
                "instead of open (or the other way), or use a switch of the other kind."
                % what)
    others = [b for b in range(32) if changed >> b & 1]
    functions = [TEXT[k] for k, b, _ in FUNCTIONS if b in others]
    physical = [bit_name(b, slave) for b in others if b >= 16]
    if functions:
        return ("warning", "The switch works as the drive's %s, not as its %s: that input has "
                "another function. Set it to %s in the drive's input settings."
                % (" and ".join(functions), what, what))
    if physical:
        return ("warning", "The switch reaches the drive (%s changes), but that input is not "
                "the %s: set the input's function to %s in the drive's input settings "
                "(see the homing card on the drive's page)."
                % (", ".join(physical), what, what))
    return ("warning", "Something changed in 0x60FD (bit %s), but not the %s."
            % (", ".join(str(b) for b in others), what))


# names of settings that set up inputs, as makers write them
_INPUT = re.compile(r"\b(input|inputs|DI\s*\d*|IN\s*\d+|X\s*\d+|digital)\b", re.I)
_ROLE = re.compile(r"(function|polarity|logic|level|select|assign|config|definition|"
                   r"home|origin|limit|switch)", re.I)


def input_settings(slave, dictionary):
    """The drive's settings that look like setting up its inputs, by name:
    [(index, subindex, name)], in the maker's range 0x2000 to 0x5FFF. A guess
    for drives without a quirk file: nothing is written."""
    out = []
    if dictionary is None:
        return out
    for (index, sub), e in sorted(dictionary.entries.items()):
        if not 0x2000 <= index < 0x6000 or not getattr(e, "writable", True):
            continue
        if sub == 0 and dictionary.subentries(index):
            continue
        name = dictionary.name(index, sub) or ""
        # an object's name tells what its entries are for ("Inputs Function: Input 3")
        full = "%s %s" % (dictionary.name(index, 0) or "", name) if sub else name
        if _INPUT.search(full) and _ROLE.search(full):
            out.append((index, sub, name))
    return out


# -- setting up the inputs, for a drive whose quirk file says how (homing: inputs)

class SetupError(Exception):
    pass


def setup(slave):
    """The quirk file's homing: inputs (names, function, polarity), or None."""
    return (quirks.of(slave).get("homing") or {}).get("inputs")


def _setting(slave, obj):
    want = config.parse_obj(obj)
    for st in (slave or {}).get("settings") or []:
        try:
            if config.parse_obj(st["object"]) == want:
                return st
        except (KeyError, ValueError):
            continue
    return None


def _value(slave, obj, typ, factory, read):
    """An object's value as the drive will have it: the file's setting, else
    what the drive has now (read), else the factory value; None if unknown."""
    st = _setting(slave, obj)
    if st is not None:
        return int(st["value"])
    if read is not None:
        index, sub = config.parse_obj(obj)
        v = read(index, sub, typ)
        if v is not None:
            return int(v)
    return factory


def _put(slave, obj, typ, value, name):
    st = _setting(slave, obj)
    if st is not None:
        st["value"] = value
        return
    slave.setdefault("settings", []).append({"object": obj, "type": typ, "value": value, "name": name})


def current(slave, kind, read=None):
    """(input, polarity choice) the drive will have for a switch: the input
    whose function is that switch (None if none), and the name of its
    polarity choice (None if unknown or there is no polarity)."""
    s = setup(slave)
    if not s:
        return None, None
    f = s["function"]
    code = f["codes"].get(kind)
    found = None
    for n in s["names"]:
        if code is not None and _value(slave, f["objects"][n], f["type"], f["factory"].get(n), read) == code:
            found = n
            break
    return found, (polarity(slave, found, read) if found else None)


def polarity(slave, name, read=None):
    """The name of an input's polarity choice, or None."""
    p = (setup(slave) or {}).get("polarity")
    if not p:
        return None
    if "bits" in p:
        if name not in p["bits"]:
            return None
        whole = _value(slave, p["object"], p["type"], p.get("factory"), read)
        if whole is None:
            return None
        v = whole >> p["bits"][name] & 1
    else:
        if name not in p["objects"]:
            return None
        v = _value(slave, p["objects"][name], p["type"], p["factory"].get(name), read)
    return next((c for c, x in p["choices"].items() if x == v), None)


def assign(slave, kind, name, choice=None, read=None):
    """Make input `name` the drive's switch `kind` (None: no input has it),
    with polarity `choice` (a name of the quirk's choices; None: as it is).
    Another input that had that function gets none (two inputs with one
    function is an error on some drives). Written as settings of the slave,
    only for the inputs that change; nothing is written if any of it cannot
    be (SetupError, such as a polarity bit whose object's other bits are not
    known: read the bus first). Returns notes on what else changed."""
    s = setup(slave)
    if not s:
        raise SetupError("the drive's quirk file does not say how its inputs are set up")
    f = s["function"]
    code, none = f["codes"].get(kind), f["codes"]["none"]
    if code is None:
        raise SetupError("this drive has no function code for the %s" % TEXT[kind])
    if name is not None and name not in s["names"]:
        raise SetupError("%s is not an input of this drive (%s)" % (name, ", ".join(s["names"])))
    by_code = {v: k for k, v in f["codes"].items()}
    writes, notes = [], []
    for n in s["names"]:
        now = _value(slave, f["objects"][n], f["type"], f["factory"].get(n), read)
        if n == name and now != code:
            writes.append((f["objects"][n], f["type"], code, "%s function: %s" % (n, TEXT[kind])))
            if now is not None and now != none:
                notes.append("%s was %s; it is the %s now" % (
                    n, "the " + TEXT[by_code[now]] if by_code.get(now) in TEXT else
                    "function %d" % now, TEXT[kind]))
        elif n != name and now == code:
            writes.append((f["objects"][n], f["type"], none, "%s function: none" % n))
            notes.append("%s is no longer the %s" % (n, TEXT[kind]))
    p = s.get("polarity")
    if name is not None and choice is not None and p:
        if choice not in p["choices"]:
            raise SetupError("polarity %r: one of %s" % (choice, ", ".join(p["choices"])))
        value = p["choices"][choice]
        if "bits" in p and name in p["bits"]:
            whole = _value(slave, p["object"], p["type"], p.get("factory"), read)
            if whole is None:
                raise SetupError("the other inputs' polarity in %s is not known: read the bus first"
                                 % p["object"])
            bit = p["bits"][name]
            new = (whole | 1 << bit) if value else (whole & ~(1 << bit))
            if new != whole or _setting(slave, p["object"]) is not None:
                writes.append((p["object"], p["type"], new, "Inputs polarity"))
        elif "objects" in p and name in p["objects"]:
            if _value(slave, p["objects"][name], p["type"], p["factory"].get(name), read) != value:
                writes.append((p["objects"][name], p["type"], value, "%s polarity: %s" % (name, choice)))
    for w in writes:
        _put(slave, *w)
    return notes
