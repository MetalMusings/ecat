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

No Qt here: the Test page's Switches card and the homing card use it.
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
         "input. See the drive's manual under digital inputs. Then try the switch on the Test "
         "page (Switches card, during the I/O test): it tells whether the drive sees it.")


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
