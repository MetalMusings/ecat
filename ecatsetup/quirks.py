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
import re

import yaml

from . import config

SHIPPED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "devices")
# what a quirk file holds (decided 2026-11-28): a device's notes as text, and the pins,
# settings and HAL lines a proposal takes. The facts that were keys of their own until
# then (commands, limits, homing, tuning, measure, position, errors) are notes now, and
# lcec's translations are in ecatsetup/lcec: a file that has them is read without them.
KEYS = {"name", "match", "notes", "pins", "settings", "hal", "format", "sources"}
RETIRED = {"commands", "limits", "homing", "tuning", "measure", "position", "errors", "lcec"}
# the newest file format this program reads (devicedata.FORMAT); a file of a newer
# format may hold keys this one does not know: they are skipped, not refused
FORMAT = 1


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
    unknown = set(data) - KEYS - RETIRED
    fmt = data.get("format")
    newer = isinstance(fmt, int) and fmt > FORMAT
    if unknown and not newer:
        raise QuirkError("unknown keys %s" % ", ".join(sorted(unknown)))
    if data.get("sources") is not None and not isinstance(data["sources"], dict):
        raise QuirkError("sources: a mapping of facts to where they come from")
    q = {"name": str(data.get("name") or os.path.basename(path)), "path": path, "raw": data,
         "sources": dict(data.get("sources") or {})}
    notes = data.get("notes")
    if notes is not None and not isinstance(notes, str):
        raise QuirkError("notes: a text")
    # what is worth knowing about the device, as text (decided 2026-11-28): one note per
    # file, the program's and the user's apart (load)
    q["notes"] = [(path, notes.strip())] if notes and notes.strip() else []
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
    return q


_cache = None
problems = []   # files that could not be read, as text


# keys only the program's own files give (decided 2026-11-28): HAL lines are a recipe the
# program carries and tests, as lcec's translations are; a user's file that has them is
# read without them
SHIPPED_ONLY = ("hal",)


def _read(d, own=True):
    out = []
    for path in sorted(glob.glob(os.path.join(d, "*.yaml"))):
        try:
            with open(path) as f:
                data = yaml.safe_load(f)
            if not own and isinstance(data, dict) and set(SHIPPED_ONLY) & set(data):
                problems.append("%s: %s left out: only the program's files give it"
                                % (path, ", ".join(k for k in SHIPPED_ONLY if k in data)))
                data = {k: v for k, v in data.items() if k not in SHIPPED_ONLY}
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
    for q, data in _read(user_dir(), own=False):
        mine = set(q.get("matches", [q.get("match")]))
        under = next(((sq, sd) for sq, sd in shipped
                      if mine & set(sq.get("matches", [sq.get("match")]))), None)
        if under:
            try:
                mine_notes = q["notes"]
                q = parse(devicedata.merge(under[1], data), q["path"])
                q["layers"] = [under[0]["path"]]
                # both notes: the program's first, then the user's
                q["notes"] = under[0]["notes"] + mine_notes
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


def notes(slave):
    """[(path, text)]: the device's notes, the program's first, then the user's."""
    return list(of(slave).get("notes") or [])


# 0x2010, 0x2007:03, or a range of subindexes 0x2007:03-06
_OBJECT = re.compile(r"\b0x([0-9A-Fa-f]{4})(?::([0-9A-Fa-f]{1,2})(?:-([0-9A-Fa-f]{1,2}))?)?\b")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(\"'])")


def sentences(text):
    """The text's sentences, each on one line (a paragraph's line breaks joined)."""
    out = []
    for para in re.split(r"\n\s*\n", text or ""):
        para = " ".join(para.split())
        out += [x for x in _SENTENCE.split(para) if x]
    return out


def object_notes(slave, index, subindex=None):
    """The notes' sentences that name this object (0x2010, or 0x2010:01 for that
    subindex only), in order; [] when none does."""
    out = []
    for _path, text in notes(slave):
        for x in sentences(text):
            for m in _OBJECT.finditer(x):
                if int(m.group(1), 16) != index:
                    continue
                if m.group(2) is not None and subindex is not None:
                    lo = int(m.group(2), 16)
                    hi = int(m.group(3), 16) if m.group(3) else lo
                    if not lo <= subindex <= hi:
                        continue
                out.append(x)
                break
    return out


def object_tip(slave, index, subindex=None):
    """A tooltip's text for an object named in the device's notes, or ""."""
    found = object_notes(slave, index, subindex)
    return ("From the notes on this device:\n" + "\n".join("- " + x for x in found)) if found else ""


def pins(key):
    """{(index, subindex): {"pin", "hal-type", "scale", "offset"}}."""
    return of(key).get("pins", {})


def settings(key):
    """[{"object", "type", "value", "name"}]: settings a new slave gets."""
    return of(key).get("settings", [])


ZERO_AT_START = "cia402.zero-at-start"


def lcec_replacement(lcec_type, index, subindex):
    """What an lcec sdoConfig of this object becomes, from the lcec type's
    translation (lcecmap): (what, why) with what ZERO_AT_START; None for an
    ordinary setting. A command lcec wrote as a setting is not known here any
    more: the device's notes say it (they show with the row on From lcec)."""
    from . import lcecmap
    return lcecmap.sdo(lcec_type).get((index, subindex)) or None


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
