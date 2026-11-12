"""Quirks: what EtherCAT Setup knows about particular devices beyond their
ESI file, SII and dictionary, read from quirk files (docs/ecatsetup.md).

Quirk files are YAML, one per device or family: those that come with
EtherCAT Setup (ecatsetup/devices/*.yaml) and the user's own
(~/.config/ecatsetup/devices/*.yaml, or $ECATSETUP_DEVICES). A user's file
for the same vendor and product replaces the one that came with the
program. Quirks act only through what EtherCAT Setup writes (ethercat.yaml,
ethercat.hal) or does in its test mode; the component never reads them,
so that the written files stay complete and predictable.
"""

import glob
import os

import yaml

SHIPPED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "devices")
KEYS = {"name", "match", "commands", "limits", "pins", "settings", "hal", "lcec", "homing"}


def user_dir():
    return os.environ.get("ECATSETUP_DEVICES") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
        "ecatsetup", "devices")


class QuirkError(ValueError):
    pass


def _hex(text, what):
    try:
        return int(str(text), 16)
    except ValueError:
        raise QuirkError("%s %r is not a hex number" % (what, text))


def _obj(text):
    """"0x2022:00" -> (0x2022, 0); "0x2010" -> (0x2010, None)."""
    index, _, sub = str(text).partition(":")
    return _hex(index, "object"), (_hex(sub, "subindex") if sub else None)


def parse(data, path="<quirk>"):
    """Check a quirk file's content; returns it with objects parsed."""
    if not isinstance(data, dict):
        raise QuirkError("not a YAML mapping")
    unknown = set(data) - KEYS
    if unknown:
        raise QuirkError("unknown keys %s" % ", ".join(sorted(unknown)))
    q = {"name": str(data.get("name") or os.path.basename(path)), "path": path}
    m = data.get("match")
    if m is not None:
        if not isinstance(m, dict) or "vendor" not in m or "product" not in m:
            raise QuirkError("match needs vendor and product")
        q["match"] = (_hex(m["vendor"], "vendor"), _hex(m["product"], "product"))
    q["commands"], q["command-runs"] = {}, {}
    for k, v in (data.get("commands") or {}).items():
        index, sub = _obj(k)
        if isinstance(v, dict):
            # note, and optionally what "Run command" writes: value, type, confirm
            if "note" not in v:
                raise QuirkError("commands.%s needs a note" % k)
            q["commands"][index] = str(v["note"])
            q["command-runs"][(index, sub or 0)] = {
                "value": v.get("value"), "type": v.get("type"),
                "confirm": str(v["confirm"]) if v.get("confirm") else None}
        else:
            q["commands"][index] = str(v)
    limits = {}
    for k, v in (data.get("limits") or {}).items():
        if isinstance(v, str):
            limits[k] = v
        elif isinstance(v, dict) and "object" in v and "type" in v:
            index, sub = _obj(v["object"])
            limits[k] = (index, sub or 0, str(v["type"]))
        else:
            raise QuirkError("limits.%s needs object and type, or a text" % k)
    q["limits"] = limits
    pins = {}
    for k, v in (data.get("pins") or {}).items():
        index, sub = _obj(k)
        if not isinstance(v, dict) or not v.get("pin"):
            raise QuirkError("pins.%s needs a pin" % k)
        pins[(index, sub or 0)] = dict(v)
    q["pins"] = pins
    settings = []
    for v in data.get("settings") or []:
        if not isinstance(v, dict) or "object" not in v or "type" not in v or "value" not in v:
            raise QuirkError("settings need object, type and value")
        index, sub = _obj(v["object"])
        st = {"object": "0x%04X:%02X" % (index, sub or 0), "type": str(v["type"]),
              "value": v["value"]}
        if v.get("name"):
            st["name"] = str(v["name"])
        settings.append(st)
    q["settings"] = settings
    hal = data.get("hal")
    if hal is not None:
        if not isinstance(hal, dict) or not isinstance(hal.get("lines", []), list):
            raise QuirkError("hal needs lines (a list)")
        q["hal"] = {"load": {str(k): [str(x) for x in v]
                             for k, v in (hal.get("load") or {}).items()},
                    "addf": [str(x) for x in hal.get("addf") or []],
                    "lines": [str(x) for x in hal.get("lines") or []]}
    homing = data.get("homing")
    if homing is not None:
        q["homing"] = _homing(homing)
    lcec = data.get("lcec") or {}
    q["lcec"] = {"types": [str(t) for t in lcec.get("types") or []],
                 "sdo": {}, "modparams": {}, "settings": []}
    for k, v in (lcec.get("sdo") or {}).items():
        index, sub = _obj(k)
        q["lcec"]["sdo"][(index, sub or 0)] = (v.get("replace"), str(v.get("note", "")))
    for k, v in (lcec.get("modparams") or {}).items():
        index, sub = _obj(v.get("object"))
        q["lcec"]["modparams"][k] = (index, sub or 0, str(v.get("type")), v.get("convert"),
                                     v.get("values"))
    for v in lcec.get("settings") or []:
        index, sub = _obj(v.get("object"))
        q["lcec"]["settings"].append((index, sub or 0, str(v.get("type")), int(v.get("value")),
                                      str(v.get("note", ""))))
    return q


def _homing(data):
    """homing: methods (numbers), describe (text, needs, moves per method),
    extras (settings per method)."""
    if not isinstance(data, dict) or set(data) - {"methods", "describe", "extras"}:
        raise QuirkError("homing takes methods, describe and extras")
    out = {"methods": [], "describe": {}, "extras": {}}
    for n in data.get("methods") or []:
        out["methods"].append(int(n))
    for n, v in (data.get("describe") or {}).items():
        if not isinstance(v, dict):
            raise QuirkError("homing.describe.%s needs text (and needs, moves)" % n)
        out["describe"][int(n)] = dict(v)
    for n, items in (data.get("extras") or {}).items():
        lst = []
        for v in items or []:
            if not isinstance(v, dict) or "object" not in v or "type" not in v or "value" not in v:
                raise QuirkError("homing.extras.%s: settings need object, type and value" % n)
            index, sub = _obj(v["object"])
            st = {"object": "0x%04X:%02X" % (index, sub or 0), "type": str(v["type"]),
                  "value": v["value"]}
            if v.get("name"):
                st["name"] = str(v["name"])
            lst.append(st)
        out["extras"][int(n)] = lst
    return out


_cache = None
problems = []   # files that could not be read, as text


def load(reload=False):
    """All quirks: the user's first, then those that came with the program."""
    global _cache
    if _cache is not None and not reload:
        return _cache
    del problems[:]
    out = []
    for d in (user_dir(), SHIPPED):
        for path in sorted(glob.glob(os.path.join(d, "*.yaml"))):
            try:
                with open(path) as f:
                    out.append(parse(yaml.safe_load(f), path))
            except (OSError, yaml.YAMLError, TypeError, ValueError, AttributeError) as e:
                problems.append("%s: %s" % (path, e))
    _cache = out
    return out


def identity(slave):
    """(vendor, product) of a configuration's slave, or None."""
    ident = (slave or {}).get("identity") or {}
    try:
        return int(str(ident.get("vendor")), 16), int(str(ident.get("product")), 16)
    except ValueError:
        return None


def of(key):
    """The quirk of a (vendor, product), or of a configuration's slave; {}
    if there is none. The user's file wins."""
    if isinstance(key, dict):
        key = identity(key)
    return next((q for q in load() if q.get("match") == key), {}) if key else {}


def command(slave, index):
    """Why an object of this slave is a command, not a setting; or None."""
    return of(slave).get("commands", {}).get(index)


def command_run(slave, index, subindex):
    """What "Run command" writes for a quirk file's command: {"value", "type",
    "confirm"} (each may be None), or None if the file does not say."""
    return of(slave).get("command-runs", {}).get((index, subindex))


def limits(slave):
    """{limit: (index, subindex, tool type), or why the drive's object is
    not used} for the drive test."""
    return of(slave).get("limits", {})


def pins(key):
    """{(index, subindex): {"pin", "hal-type", "scale", "offset"}}."""
    return of(key).get("pins", {})


def settings(key):
    """[{"object", "type", "value", "name"}]: settings a new slave gets."""
    return of(key).get("settings", [])


def lcec_sdo(key):
    """{(index, subindex): (replacement, note)} for an lcec sdoConfig."""
    return of(key).get("lcec", {}).get("sdo", {})


def for_lcec_type(lcec_type):
    return next((q for q in load() if lcec_type in q["lcec"]["types"]), {})


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


def lcec_modparams(lcec_type, modparams):
    """lcec modParams of a slave as settings: ([(index, subindex, type,
    value, name)], notes)."""
    q = for_lcec_type(lcec_type)
    table = q.get("lcec", {}).get("modparams", {})
    out, notes = [], []
    for index, sub, dtype, value, why in q.get("lcec", {}).get("settings", []):
        out.append((index, sub, dtype, value, None))
        notes.append("0x%04X:%02X = %d added: %s" % (index, sub, value, why))
    for name, text in modparams:
        if name not in table:
            notes.append("modParam %s=%s not imported%s" % (
                name, text, "" if table else " (no quirk file translates lcec type %s)"
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


def from_slave(slave):
    """A quirk file's content (a dict to write as YAML) from a configured
    slave: its identity, the pins it was given and its settings, so that the
    next proposal for the same device starts from them."""
    ident = identity(slave)
    if not ident:
        raise QuirkError("the slave has no vendor and product")
    out = {"name": str(slave.get("device") or slave.get("name") or "device"),
           "match": {"vendor": "0x%08X" % ident[0], "product": "0x%08X" % ident[1]}}
    pins = {}
    for kind in ("outputs", "inputs"):
        for pdo in (slave.get("process-data") or {}).get(kind) or []:
            for e in pdo.get("entries") or []:
                if not e.get("pin"):
                    continue
                d = {"pin": e["pin"]}
                for k in ("hal-type", "scale", "offset"):
                    if e.get(k) is not None:
                        d[k] = e[k]
                pins[str(e.get("object"))] = d
    if pins:
        out["pins"] = pins
    sts = [{k: st[k] for k in ("object", "name", "type", "value") if k in st}
           for st in slave.get("settings") or []]
    if sts:
        out["settings"] = sts
    return out


def file_name(slave):
    """<vendor>-<product>.yaml for a slave's quirk file."""
    ident = identity(slave) or (0, 0)
    return "%08x-%08x.yaml" % ident


def save(slave, path=None):
    """Write a slave's quirk file into the user's folder (or path); returns
    the path. Reloads the quirks."""
    data = from_slave(slave)
    path = path or os.path.join(user_dir(), file_name(slave))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        f.write("# Quirk file for %s, saved by EtherCAT Setup from a configured slave.\n"
                "# See docs/ecatsetup.md. Edit freely.\n" % data["name"])
        yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False, allow_unicode=True)
    load(reload=True)
    return path
