"""An ESI file's InitCmds as a slave's init-cmds (docs/development.md "ESI InitCmds").

An InitCmd is an SDO write the vendor's file asks the configuring tool to do at a state
change, such as StepperOnline's 0x6060 = 8 at PREOP->SAFEOP. ecat does not read ESI files
at run time, so EtherCAT Setup copies them into the slave's `init-cmds`, which the
component writes before the user's `settings`, in PREOP whenever the master configures
the slave. They stay apart from the settings: the vendor's, with their transition, complete
access, and templates the component fills in (the cycle time); a row can be turned off
(`disabled: true`). The rules are data: pdoreq/initcmds.yaml.
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
    cmds: list = field(default_factory=list)       # init-cmds rows for the file
    notes: list = field(default_factory=list)      # what the user should know
    skipped: list = field(default_factory=list)    # (object text, why)


def _rules():
    return pdoreq.load("initcmds")


def _obj(index, sub):
    return "0x%04X:%02X" % (index, sub)


def value_of(index, sub, data, dictionary=None):
    """The value part of a row from an InitCmd's data: a typed value where the type is
    known (the dictionary, else the data's length) and its size fits, else the raw bytes."""
    raw = bytes.fromhex((data or "").replace(" ", ""))
    entry = dictionary.entries.get((index, sub)) if dictionary is not None else None
    ctype = guidance.config_type(entry) or _BY_LENGTH.get(len(raw))
    out = {}
    if entry is not None and dictionary.name(index, sub):
        out["name"] = dictionary.name(index, sub)
    if ctype in ("REAL", "LREAL") or ctype is None or _SIZE.get(ctype) != len(raw):
        # as the file has it, byte by byte: also where the dictionary's type has another size
        out["data"] = raw.hex(" ")
        return out
    out["type"] = ctype
    out["value"] = int.from_bytes(raw, "little", signed=ctype in _SIGNED)
    return out


def from_esi(esi_dev, dictionary=None, profile=None, axes=1):
    """The InitCmds of a device description (and its modules', moved to their slots), as
    init-cmds rows, notes and what is skipped. `profile` ("cia402", ...): an object it owns
    becomes a row with `from: profile`, also on a later axis (+ 0x800 each, `axes` of them)."""
    out = Found()
    if esi_dev is None:
        return out
    rules = _rules()
    owned = set(rules.get("profile-owned", {}).get(profile) or [])
    shift = pdoreq.load("cia402")["axis-offset"]          # a drive's later axes
    templates = {pdoreq_obj(k): v for k, v in (rules.get("templates") or {}).items()}
    seen = set()
    for cmd in esi_dev.init_cmds:
        transitions, index, sub, data = cmd[:4]
        complete = len(cmd) > 4 and bool(cmd[4])
        when = [t.strip().upper() for t in (transitions or "").split(",") if t.strip()]
        text = _obj(index, sub)
        on_time = [t for t in rules["transitions"]["on-time"] if t in when]   # in ecat's order: IP,PS
        if not on_time:
            out.skipped.append((text, "at %s, a state change ecat does not write at yet" % ",".join(when)))
            continue
        row = {"at": ",".join(on_time), "object": text}
        if (index, sub) in templates:
            row["from"] = templates[(index, sub)]
        elif any(index == o + a * shift for o in owned for a in range(max(1, axes))):
            row["from"] = "profile"
        else:
            why = next((r["why"] for r in rules["skip"] if r["from"] <= index <= r["to"]), None)
            if why is not None or not any(lo <= index <= hi for lo, hi in rules["take"]):
                out.skipped.append((text, why or "outside the objects taken"))
                continue
            if complete:
                if sub > 1:
                    out.skipped.append((text, "complete access from subindex %d" % sub))
                    continue
                row["complete"] = True
                row["data"] = bytes.fromhex((data or "").replace(" ", "")).hex(" ")
            else:
                row.update(value_of(index, sub, data, dictionary))
        if (row["object"], row.get("complete", False)) in seen:
            continue
        seen.add((row["object"], row.get("complete", False)))
        out.cmds.append(row)
    if any("complete" in r for r in out.cmds):
        out.notes.append("complete-access InitCmds need a master with ecrt_slave_config_complete_sdo_from "
                         "(the master package 1.6 or later) where they start at subindex 1")
    return out


def pdoreq_obj(text):
    """(index, subindex) of "0x1C32:02"."""
    index, sub = str(text).split(":")
    return int(index, 16), int(sub, 16)
