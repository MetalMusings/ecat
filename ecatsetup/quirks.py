"""Quirks: what EtherCAT Setup knows about particular devices beyond their
ESI file, SII and dictionary, read from quirk files (docs/ecatsetup.md).

Quirk files are YAML, one per device or family: those that come with
EtherCAT Setup (ecatsetup/devices/*.yaml) and the user's own
(~/.config/ecatsetup/devices/*.yaml, or $ECATSETUP_DEVICES). A user's file
for the same vendor and product is laid over the one that came with the
program, fact by fact (devicedata.merge): where both say something, the
user's is used; the rest of the shipped file stays. Quirks act only through what EtherCAT Setup writes (ethercat.yaml,
ethercat.hal) or does in its test mode; the component never reads them,
so that the written files stay complete and predictable.
"""

import glob
import os

import yaml

from . import config

SHIPPED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "devices")
KEYS = {"name", "match", "commands", "limits", "pins", "settings", "hal", "lcec", "homing",
        "tuning", "measure", "format", "sources", "position", "errors"}
# what a command does, when the program can act on it (commands' rich form, `purpose`):
# a drive's "zero position" command is what cia402.zero-at-start replaces
COMMAND_PURPOSES = {"zero-position", "store-parameters", "clear-fault", "other"}
# the newest file format this program reads (devicedata.FORMAT); a file of a newer
# format may hold keys this one does not know: they are skipped, not refused
FORMAT = 1
# what a tuning entry is about (response.KINDS)
TUNING_KINDS = {"command-filter", "position-loop", "feedforward", "torque", "gain-high", "other"}


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
    fmt = data.get("format")
    newer = isinstance(fmt, int) and fmt > FORMAT
    if unknown and not newer:
        raise QuirkError("unknown keys %s" % ", ".join(sorted(unknown)))
    if data.get("sources") is not None and not isinstance(data["sources"], dict):
        raise QuirkError("sources: a mapping of facts to where they come from")
    q = {"name": str(data.get("name") or os.path.basename(path)), "path": path, "raw": data,
         "sources": dict(data.get("sources") or {})}
    m = data.get("match")
    if m is not None:
        if not isinstance(m, dict) or "vendor" not in m or "product" not in m:
            raise QuirkError("match needs vendor and product")
        # product: one code, or a list for a family with the same objects
        products = m["product"] if isinstance(m["product"], list) else [m["product"]]
        if not products:
            raise QuirkError("match needs vendor and product")
        vendor = _hex(m["vendor"], "vendor")
        q["matches"] = [(vendor, _hex(p, "product")) for p in products]
        q["match"] = q["matches"][0]
    q["commands"], q["command-runs"], q["command-purposes"] = {}, {}, {}
    for k, v in (data.get("commands") or {}).items():
        index, sub = _obj(k)
        if isinstance(v, dict):
            # note, and optionally what "Run command" writes: value, type, confirm; and
            # what it is for (purpose), when the program can act on it
            if "note" not in v:
                raise QuirkError("commands.%s needs a note" % k)
            if v.get("purpose") is not None:
                if v["purpose"] not in COMMAND_PURPOSES:
                    raise QuirkError("commands.%s: purpose is one of %s"
                                     % (k, ", ".join(sorted(COMMAND_PURPOSES))))
                q["command-purposes"][index] = v["purpose"]
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
        if not isinstance(v, dict) or not (v.get("pin") or v.get("split")):
            raise QuirkError("pins.%s needs a pin (or split, a list of bit pins)" % k)
        if "split" in v:
            # one name per bit, from bit 0; null for a bit that has no pin
            sp = v["split"]
            if not isinstance(sp, list) or any(x is not None and not isinstance(x, str) for x in sp):
                raise QuirkError("pins.%s: split must be a list of names (null for unused bits)" % k)
            v = dict(v, split=[x or None for x in sp])
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
    tuning = []
    for k, v in (data.get("tuning") or {}).items():
        index, sub = _obj(k)
        if not isinstance(v, dict) or v.get("kind") not in TUNING_KINDS or "type" not in v:
            raise QuirkError("tuning.%s needs a kind (%s) and a type"
                             % (k, ", ".join(sorted(TUNING_KINDS))))
        t = {"index": index, "subindex": sub or 0, "kind": v["kind"], "type": str(v["type"])}
        for key in ("unit", "note", "tried"):
            if v.get(key) is not None:
                t[key] = str(v[key])
        for key in ("min", "max", "factory"):
            if v.get(key) is not None:
                t[key] = int(v[key])
        when = v.get("when")
        if when is not None:
            if not isinstance(when, dict) or "object" not in when or "values" not in when:
                raise QuirkError("tuning.%s: when needs object and values" % k)
            wi, ws = _obj(when["object"])
            t["when"] = (wi, ws or 0, [int(x) for x in when["values"]], str(when.get("type", "uint16")))
        tuning.append(t)
    q["tuning"] = tuning
    measure = data.get("measure") or {}
    if not isinstance(measure, dict):
        raise QuirkError("measure needs a mapping (ignore: {object: why})")
    q["measure-ignore"] = {_obj(k)[0]: str(v) for k, v in (measure.get("ignore") or {}).items()}
    # the drive's position counter at start: kept from an earlier run while it has power
    # (a property; the advice, cia402.zero-at-start, is the window's)
    position = data.get("position")
    q["position"] = {}
    if position is not None:
        if not isinstance(position, dict) or not isinstance(position.get("kept"), bool):
            raise QuirkError("position needs kept: true or false")
        q["position"] = {"kept": position["kept"]}
    # what the drive means by its error codes (0x603F)
    errors = data.get("errors") or {}
    if not isinstance(errors, dict):
        raise QuirkError("errors needs a mapping of codes (0x603F) to what they mean")
    q["errors"] = {}
    for k, v in errors.items():
        try:
            code = _hex(k, "error code") if isinstance(k, str) else int(k)
        except (QuirkError, ValueError, TypeError):
            raise QuirkError("errors: %r is not a code" % (k,))
        if not 0 <= code <= 0xFFFF or not isinstance(v, str) or not v.strip():
            raise QuirkError("errors.%s: a code of 16 bits and a text" % k)
        q["errors"][code] = v.strip()
    # lcec: (lcec's drivers' translations, until 2026-11-28) is read from ecatsetup/lcec
    # (lcecmap.py) now; a file that has it is taken without it
    return q


def _homing(data):
    """homing: methods (numbers), describe (text, needs, moves per method),
    extras (settings per method), enable-again (methods after which the
    drive follows its target only once enabled again). input-bits and inputs
    (how the inputs got their function, until 2026-11-27) are no longer read:
    a file that has them is taken without them."""
    if not isinstance(data, dict) or set(data) - {"methods", "describe", "extras", "enable-again",
                                                  "input-bits", "inputs"}:
        raise QuirkError("homing takes methods, describe, extras and enable-again")
    out = {"methods": [], "describe": {}, "extras": {}, "enable-again": []}
    for n in data.get("methods") or []:
        out["methods"].append(int(n))
    for n in data.get("enable-again") or []:
        out["enable-again"].append(int(n))
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


def _read(d):
    out = []
    for path in sorted(glob.glob(os.path.join(d, "*.yaml"))):
        try:
            with open(path) as f:
                data = yaml.safe_load(f)
            out.append((parse(data, path), data))
        except (OSError, yaml.YAMLError, TypeError, ValueError, AttributeError) as e:
            problems.append("%s: %s" % (path, e))
    return out


def load(reload=False):
    """All quirks: the user's first, each laid over the shipped file for the
    same device (q["layers"] names it), then those that came with the program."""
    from . import devicedata
    global _cache
    if _cache is not None and not reload:
        return _cache
    del problems[:]
    shipped = _read(SHIPPED)
    out = []
    for q, data in _read(user_dir()):
        mine = set(q.get("matches", [q.get("match")]))
        under = next(((sq, sd) for sq, sd in shipped
                      if mine & set(sq.get("matches", [sq.get("match")]))), None)
        if under:
            try:
                q = parse(devicedata.merge(under[1], data), q["path"])
                q["layers"] = [under[0]["path"]]
            except (TypeError, ValueError, AttributeError) as e:
                problems.append("%s over %s: %s" % (q["path"], under[0]["path"], e))
        out.append(q)
    out += [sq for sq, _ in shipped]
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
    if there is none. The user's file (laid over the shipped one) comes first."""
    if isinstance(key, dict):
        key = identity(key)
    return next((q for q in load() if key in q.get("matches", [q.get("match")])), {}) if key else {}


def command(slave, index):
    """Why an object of this slave is a command, not a setting; or None."""
    return of(slave).get("commands", {}).get(index)


def command_purpose(slave, index):
    """What a command is for (COMMAND_PURPOSES), or None."""
    return of(slave).get("command-purposes", {}).get(index)


def keeps_position(slave):
    """True, False, or None when the quirk file does not say."""
    return (of(slave).get("position") or {}).get("kept")


def error_meaning(slave, code):
    """What the drive means by an error code (0x603F), or None."""
    return (of(slave).get("errors") or {}).get(code)


def command_run(slave, index, subindex):
    """What "Run command" writes for a quirk file's command: {"value", "type",
    "confirm"} (each may be None), or None if the file does not say."""
    return of(slave).get("command-runs", {}).get((index, subindex))


def limits(slave):
    """{limit: (index, subindex, tool type), or why the drive's object is
    not used} for the drive test."""
    return of(slave).get("limits", {})


def tuning(slave):
    """[{"index", "subindex", "kind", "type", "unit", "min", "max", "factory",
    "note", "tried", "when"}]: the drive's settings that tuning looks at."""
    return of(slave).get("tuning", [])


def measure_ignore(slave):
    """{object index: why}: objects the response measurement does not record."""
    return of(slave).get("measure-ignore", {})


def pins(key):
    """{(index, subindex): {"pin", "hal-type", "scale", "offset"}}."""
    return of(key).get("pins", {})


def settings(key):
    """[{"object", "type", "value", "name"}]: settings a new slave gets."""
    return of(key).get("settings", [])


ZERO_AT_START = "cia402.zero-at-start"


def lcec_replacement(key, index, subindex, lcec_type=None):
    """What an lcec sdoConfig of this object becomes: (what, why), what being
    ZERO_AT_START or "command"; None for an ordinary setting. The lcec type's
    translation (lcecmap) first; otherwise it follows from the object's properties: a
    command whose purpose is zero-position is what cia402.zero-at-start does
    without writing to the drive, and any other command is not a setting."""
    from . import lcecmap
    given = lcecmap.sdo(lcec_type).get((index, subindex)) if lcec_type else None
    if given:
        return given
    why = command(key, index)
    if not why:
        return None
    if command_purpose(key, index) == "zero-position":
        return ZERO_AT_START, ("0x%04X:%02X (%s) zeroes the drive's position, a command: not "
                               "needed with ecat, which holds the drive's position while it "
                               "enables. cia402.zero-at-start (Start position) gives the same 0 "
                               "at start without writing to the drive, if the machine should "
                               "read 0 at start" % (index, subindex, why))
    return "command", ("0x%04X:%02X is a command, says the device's quirk file (%s): as a "
                       "setting it would run at every start" % (index, subindex, why))


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
                if e.get("split"):
                    pins[str(e.get("object"))] = {"split": list(e["split"])}
                    continue
                if not e.get("pin"):
                    continue
                d = {"pin": e["pin"]}
                for k in ("hal-type", "scale", "offset"):
                    if e.get(k) is not None:
                        d[k] = e[k]
                if "hal-type" in d:
                    d["hal-type"] = config.HAL_ALIASES.get(d["hal-type"], d["hal-type"])
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
    from . import devicedata
    data = from_slave(slave)
    path = path or os.path.join(user_dir(), file_name(slave))
    try:
        with open(path) as f:
            old = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        old = None
    if isinstance(old, dict):
        # what was remembered before (commands, sources) stays
        data = devicedata.merge(old, data)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        f.write("# Quirk file for %s, saved by EtherCAT Setup from a configured slave.\n"
                "# See docs/ecatsetup.md. Edit freely.\n" % data["name"])
        yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False, allow_unicode=True)
    load(reload=True)
    return path
