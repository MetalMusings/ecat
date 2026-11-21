"""An ESI file's InitCmds as settings (docs/development.md "ESI InitCmds").

An InitCmd is an SDO write the vendor's file asks the configuring tool to do at a
state change, such as StepperOnline's 0x6060 = 8 at PREOP->SAFEOP. TwinCAT does
them; ecat does not read ESI files at run time, so EtherCAT Setup puts the ones
that are fixed settings into the slave's `settings` (written in PREOP whenever the
master configures the slave). The rules are data: pdoreq/initcmds.yaml.
"""

from dataclasses import dataclass, field

from . import guidance, pdoreq

# the types of a value by its length, when the dictionary does not say
_BY_LENGTH = {1: "USINT", 2: "UINT", 4: "UDINT", 8: "ULINT"}
_SIGNED = {"SINT", "INT", "DINT", "LINT"}
_SIZE = {"BOOL": 1, "SINT": 1, "USINT": 1, "INT": 2, "UINT": 2, "DINT": 4, "UDINT": 4, "REAL": 4,
         "LINT": 8, "ULINT": 8, "LREAL": 8}


@dataclass
class Found:
    settings: list = field(default_factory=list)   # setting dicts for the file
    notes: list = field(default_factory=list)      # what the user should know
    skipped: list = field(default_factory=list)    # (object text, why)
    owned: list = field(default_factory=list)      # (index, subindex, value) a profile decides


def _rules():
    return pdoreq.load("initcmds")


def _obj(index, sub):
    return "0x%04X:%02X" % (index, sub)


def setting(index, sub, data, dictionary=None):
    """The setting an InitCmd's data gives: a typed value where the type is known
    (the dictionary, else the data's length), else the raw bytes."""
    raw = bytes.fromhex((data or "").replace(" ", ""))
    entry = dictionary.entries.get((index, sub)) if dictionary is not None else None
    ctype = guidance.config_type(entry) or _BY_LENGTH.get(len(raw))
    st = {"object": _obj(index, sub)}
    if entry is not None and dictionary.name(index, sub):
        st["name"] = dictionary.name(index, sub)
    if ctype in ("REAL", "LREAL") or ctype is None or _SIZE.get(ctype) != len(raw):
        # as the file has it, byte by byte: also where the dictionary's type has another size
        st["data"] = raw.hex(" ")
        return st
    st["type"] = ctype
    st["value"] = int.from_bytes(raw, "little", signed=ctype in _SIGNED)
    return st


def from_esi(esi_dev, dictionary=None, profile=None):
    """The InitCmds of a device description, sorted into settings, notes and what is
    skipped. `profile` ("cia402", ...): objects it owns are returned in `owned`."""
    out = Found()
    if esi_dev is None:
        return out
    rules = _rules()
    owned = set(rules.get("profile-owned", {}).get(profile) or [])
    seen = set()
    for cmd in esi_dev.init_cmds:
        transitions, index, sub, data = cmd[:4]
        complete = len(cmd) > 4 and cmd[4]
        when = {t.strip().upper() for t in (transitions or "").split(",") if t.strip()}
        text = _obj(index, sub)
        if complete:
            out.skipped.append((text, "written with complete access (the whole object at once), which "
                                      "ecat's settings do not do yet"))
            if any(lo <= index <= hi for lo, hi in rules["take"]):
                out.notes.append("%s: the ESI file writes the whole object at start (complete access); "
                                 "ecat cannot yet: set its subindexes as settings if the device needs it" % text)
            continue
        if not when & set(rules["transitions"]["on-time"] + rules["transitions"]["early"]):
            out.skipped.append((text, "at %s, a state change ecat does not configure at" % ",".join(sorted(when))))
            continue
        why = next((r["why"] for r in rules["skip"] if r["from"] <= index <= r["to"]), None)
        if why is not None or not any(lo <= index <= hi for lo, hi in rules["take"]):
            out.skipped.append((text, why or "outside the objects taken as settings"))
            continue
        if (index, sub) in seen:
            continue
        seen.add((index, sub))
        st = setting(index, sub, data, dictionary)
        if index in owned:
            raw = bytes.fromhex((data or "").replace(" ", ""))
            out.owned.append((index, sub, st.get("value", int.from_bytes(raw, "little", signed=True))))
            continue
        out.settings.append(st)
        if not when & set(rules["transitions"]["on-time"]):
            out.notes.append("%s: the ESI file writes it at %s; ecat writes settings in PREOP, before "
                             "that" % (text, ",".join(sorted(when))))
    return out
