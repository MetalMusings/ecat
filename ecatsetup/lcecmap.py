"""What lcec's per-product C drivers did, as data: for moving an lcec
configuration to ecat (From lcec, --import-xml) only.

lcec had a driver per product; its XML file gave them named `modParam`s
(maxCurrent="1.5") that the driver turned into SDO writes, and some drivers
wrote settings of their own that the XML file never showed. ecat has no
product drivers, so each such driver is a file here (ecatsetup/lcec/*.yaml):

    name: lcec el7041
    types: [EL7041, EP7041]            # lcec type names (type= in the XML)
    modparams:                         # per modParam: the setting it becomes
      maxCurrent: {object: '0x8010:01', type: UINT, convert: milli}
      maxSpeed: {object: '0x8012:05', type: UINT, values: {1000: 0, 2000: 1}}
    settings:                          # always written by the driver
    - {object: '0x8012:01', type: USINT, value: 3, note: why}
    sdo:                               # an sdoConfig ecat does differently
      '0x2010:00': {replace: cia402.zero-at-start, note: why}

This is knowledge of lcec's code, not of the devices: the device files
(quirks.py) do not hold it. Only the program's own files are read.
"""

import glob
import os

import yaml

SHIPPED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lcec")
KEYS = {"name", "types", "modparams", "settings", "sdo"}

problems = []
_cache = None


class LcecMapError(ValueError):
    pass


def _obj(text):
    from .quirks import _obj
    return _obj(text)


def parse(data, path="<lcec>"):
    if not isinstance(data, dict):
        raise LcecMapError("not a YAML mapping")
    unknown = set(data) - KEYS
    if unknown:
        raise LcecMapError("unknown keys %s" % ", ".join(sorted(unknown)))
    out = {"name": str(data.get("name") or os.path.basename(path)), "path": path,
           "types": [str(t) for t in data.get("types") or []],
           "sdo": {}, "modparams": {}, "settings": []}
    if not out["types"]:
        raise LcecMapError("types: the lcec type names it is for")
    for k, v in (data.get("sdo") or {}).items():
        index, sub = _obj(k)
        out["sdo"][(index, sub or 0)] = (v.get("replace"), str(v.get("note", "")))
    for k, v in (data.get("modparams") or {}).items():
        index, sub = _obj(v.get("object"))
        out["modparams"][k] = (index, sub or 0, str(v.get("type")), v.get("convert"),
                               v.get("values"))
    for v in data.get("settings") or []:
        index, sub = _obj(v.get("object"))
        out["settings"].append((index, sub or 0, str(v.get("type")), int(v.get("value")),
                                str(v.get("note", ""))))
    return out


def load(reload=False):
    global _cache
    if _cache is not None and not reload:
        return _cache
    del problems[:]
    out = []
    for path in sorted(glob.glob(os.path.join(SHIPPED, "*.yaml"))):
        try:
            with open(path) as f:
                out.append(parse(yaml.safe_load(f), path))
        except (OSError, yaml.YAMLError, ValueError) as e:
            problems.append("%s: %s" % (path, e))
    _cache = out
    return out


def for_type(lcec_type):
    """The translation of an lcec type, or {}."""
    return next((m for m in load() if lcec_type in m["types"]), {})


def sdo(lcec_type):
    """{(index, subindex): (replacement, note)} for this type's sdoConfigs."""
    return for_type(lcec_type).get("sdo", {})


def _convert(conv, values, text):
    if values:
        v = int(float(text))
        if v not in values:
            raise ValueError("%s is not one of %s" % (text, ", ".join(str(k) for k in values)))
        return int(values[v])
    if conv == "milli":
        return int(round(float(text) * 1000))
    if conv == "flag":
        return 1 if str(text).strip().lower() in ("1", "true", "yes", "on") else 0
    if conv in (None, "int"):
        return int(float(text))
    raise ValueError("unknown conversion %r" % conv)


def modparams(lcec_type, params):
    """lcec modParams of a slave as settings: ([(index, subindex, type,
    value, name)], notes)."""
    m = for_type(lcec_type)
    table = m.get("modparams", {})
    out, notes = [], []
    for index, sub, dtype, value, why in m.get("settings", []):
        out.append((index, sub, dtype, value, None))
        notes.append("0x%04X:%02X = %d added: %s" % (index, sub, value, why))
    for name, text in params:
        if name not in table:
            notes.append("modParam %s=%s not imported%s" % (
                name, text, "" if table else " (ecat has no translation of lcec type %s)"
                % lcec_type))
            continue
        index, sub, dtype, conv, values = table[name]
        try:
            value = _convert(conv, values, text)
        except (TypeError, ValueError) as e:
            notes.append("modParam %s=%s not imported: %s" % (name, text, e))
            continue
        out = [o for o in out if (o[0], o[1]) != (index, sub)]
        out.append((index, sub, dtype, value, name))
        notes.append("modParam %s=%s imported as setting 0x%04X:%02X = %d"
                     % (name, text, index, sub, value))
    return out, notes
