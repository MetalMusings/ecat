"""Guidance for a slave's settings (SDOs written at every start): what the
object dictionary says about an object, and what is wrong with a setting
given it. No Qt: the window, --check and the tests use the same functions.

A dictionary is a coe.Dictionary: the slave's own, an ESI file's, or both
(Session.dictionary).
"""

import re

from . import config, propose, quirks

# the type names a configuration uses, from the tool's
CONFIG_TYPES = dict(propose.COE_TYPES)
RANGES = {"BOOL": (0, 1), "SINT": (-2 ** 7, 2 ** 7 - 1), "USINT": (0, 2 ** 8 - 1),
          "INT": (-2 ** 15, 2 ** 15 - 1), "UINT": (0, 2 ** 16 - 1),
          "DINT": (-2 ** 31, 2 ** 31 - 1), "UDINT": (0, 2 ** 32 - 1),
          "LINT": (-2 ** 63, 2 ** 63 - 1), "ULINT": (0, 2 ** 64 - 1)}
# names that suggest an object does something when written, instead of
# being a setting (a suspicion, never a block)
_COMMANDISH = re.compile(r"zero|reset|save|store|restore|clear|clean|default|trigger|command|"
                         r"calibrat|learn", re.I)
# CoE: store and restore parameters; writing the ASCII signature runs them
_STORE_OBJECTS = (0x1010, 0x1011)


def config_type(entry):
    """The configuration's type name of a dictionary entry, or None."""
    return CONFIG_TYPES.get(entry.data_type) if entry else None


def command_reason(slave, index, entry=None):
    """(level, why) when an object is a command, not a setting: "error"
    for a quirk file's commands, "warning" for a suspicion from the
    dictionary; None if it looks like a setting."""
    why = quirks.command(slave, index)
    if why:
        return "error", why
    if index in _STORE_OBJECTS:
        return "warning", "store/restore parameters run when written: a command, not a setting"
    if entry is not None:
        if "w" in entry.access and "r" not in entry.access:
            return "warning", "the object can only be written: it may be a command, not a setting"
        if _COMMANDISH.search(entry.name or ""):
            return "warning", "%r sounds like a command, not a setting" % entry.name
    return None


def is_command(slave, index, dictionary=None):
    entry = dictionary.entries.get((index, 0)) if dictionary is not None else None
    return command_reason(slave, index, entry)


def check_setting(slave, st, dictionary):
    """[(level, text)] for one setting: "error" or "warning"."""
    out = []
    try:
        index, sub = config.parse_obj(st.get("object"))
    except ValueError as e:
        return [("error", str(e))]
    entry = dictionary.entries.get((index, sub)) if dictionary is not None else None
    cmd = command_reason(slave, index, entry)
    if cmd:
        out.append(cmd)
    if dictionary is None or not dictionary.entries:
        return out
    if entry is None:
        out.append(("warning", "not in the object dictionary (%s); a dictionary may be "
                    "incomplete" % dictionary.source))
        return out
    strict = "slave" in dictionary.source
    if not entry.writable_preop:
        out.append(("error" if strict else "warning",
                    "not writable in PREOP, where settings are written (access %s)" % entry.access))
    want = config_type(entry)
    if want and st.get("type") and want != st["type"] and "value" in st:
        out.append(("warning", "the dictionary says %s, not %s" % (want, st["type"])))
    value = st.get("value")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        lo, hi = RANGES.get(st.get("type"), (None, None))
        if lo is not None and not lo <= value <= hi:
            out.append(("error", "%s does not fit %s (%d to %d)" % (value, st["type"], lo, hi)))
        elif entry.minimum is not None and value < entry.minimum or \
                entry.maximum is not None and value > entry.maximum:
            out.append(("warning", "outside the range the ESI gives (%s to %s)" % (
                entry.minimum if entry.minimum is not None else "-", entry.maximum
                if entry.maximum is not None else "-")))
    return out


def check_slave(slave, dictionary):
    """{row: [(level, text)]} for the rows of a slave's settings that have a problem."""
    out = {}
    for row, st in enumerate(slave.get("settings") or []):
        found = check_setting(slave, st, dictionary)
        if found:
            out[row] = found
    return out


def describe_range(entry):
    """"0 to 8000, default 5000" from a dictionary entry, or empty."""
    parts = []
    if entry.minimum is not None or entry.maximum is not None:
        parts.append("%s to %s" % ("" if entry.minimum is None else entry.minimum,
                                   "" if entry.maximum is None else entry.maximum))
    if entry.default is not None:
        parts.append("default %s" % entry.default)
    return ", ".join(parts)


def describe(entry):
    """A dictionary entry as a line: type, size, access, range, default."""
    if entry is None:
        return ""
    return ", ".join(["%s, %d bit, access %s" % (entry.data_type, entry.bitlen, entry.access)]
                     + ([describe_range(entry)] if describe_range(entry) else []))


def tool_type(config_type_name):
    """The ethercat tool's type name for a configuration's, or None."""
    return next((t for t, c in CONFIG_TYPES.items() if c == config_type_name), None)
