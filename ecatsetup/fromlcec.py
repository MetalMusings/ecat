"""Moving a LinuxCNC configuration from lcec to ecat.

The bus is read and proposed as for any machine; what the old configuration
adds is read from its HAL files, and from the lcec XML file they load when it
is there:

- the cia402 component (hal-cia402) between a joint and a generic CiA 402
  slave (the "glue"): an instance whose slave has ecat's cia402 profile now is
  replaced by it. Its joint and pos-scale are proposed on the Joints card,
  the nets between it and its lcec slave are taken out, its other pins
  become the drive's pins of the same name, and its loadrt and addf lines
  go;
- every other old pin the HAL files use (lcec's, and a glue instance's that
  has no counterpart) is a row of a list: the user chooses the ecat pin that
  takes its place, or that it is taken out. The choices are kept in the
  configuration (linuxcnc.old-pins). A suggestion comes from the XML file
  (the same object of the slave at the same position), the master's and the
  slaves' state pins, or the same pin name;
- the settings lcec wrote at every start (sdoConfig, and modParams through
  the quirk files) are offered as suggestions, never added unasked.

integrate.plan() comments every line with an old pin out, as it does lcec's
own lines, and puts the line with the new pins after it (marked added): the
HAL files keep their order, comments and signal names.
"""

import os
import re
from dataclasses import dataclass, field

from . import config, integrate, lcecxml

COMPONENT = "cia402"
# the glue component's pins that only pass the drive's objects to and from lcec
GLUE = {"statusword", "opmode-display", "drv-actual-position", "drv-actual-velocity",
        "controlword", "opmode", "drv-target-position", "drv-target-velocity"}
# a glue pin whose ecat counterpart has another name, or none (None)
RENAME = {"csp-mode": None, "home": "index-enable"}
# objects the cia402 profile owns -> the axis pin that shows them
PROFILE_OBJECTS = {0x6040: "controlword", 0x6041: "statusword", 0x6060: "opmode",
                   0x6061: "opmode-display", 0x6064: "drv-actual-position",
                   0x606C: "drv-actual-velocity", 0x607A: "drv-target-position",
                   0x60FF: "drv-target-velocity"}
# the master's pins in lcec and ecat
MASTER_PINS = {"slaves-responding", "state-init", "state-preop", "state-safeop", "state-op",
               "link-up", "all-op"}
SLAVE_PINS = {"slave-online", "slave-oper", "slave-state-init", "slave-state-preop",
              "slave-state-safeop", "slave-state-op"}
_LCEC = re.compile(r"lcec\.\S+")


@dataclass
class Glue:
    instance: str               # cia402.X
    slave: str                  # the lcec slave's pin prefix: lcec.0.X
    joint: int = None           # the joint whose motor-pos-cmd it takes
    scale: str = None           # its pos-scale as the HAL files set it


@dataclass
class Found:
    """What the HAL files say about lcec."""
    xml_path: str = None
    xml: object = None          # lcecxml.LcecConfig, or None
    problems: list = field(default_factory=list)    # nothing is written while there are any
    notes: list = field(default_factory=list)
    glue: list = field(default_factory=list)        # [Glue]
    signals: dict = field(default_factory=dict)     # signal -> set of pins
    setp: dict = field(default_factory=dict)        # pin -> value (INI references resolved)
    uses: dict = field(default_factory=dict)        # old pin -> [(file, line)]
    instances: list = field(default_factory=list)   # every instance of the component


@dataclass
class Row:
    """An old pin and what takes its place."""
    pin: str
    uses: list                  # [(file, line)]
    decided: bool = False       # a choice was made (or the pin is replaced by itself)
    new: str = None             # the new pin; None with decided: taken out
    auto: bool = False          # decided by the glue's replacement
    suggestion: str = None
    why: str = ""               # where the suggestion comes from, or why there is none


def split(pin):
    """(master, slave, rest) of an lcec pin: slave None for the master's own
    (lcec.0.link-up), master None for lcec's global ones (lcec.link-up); None
    for anything else."""
    parts = pin.split(".", 3)
    if parts[0] != "lcec" or len(parts) < 2:
        return None
    if len(parts) == 2:
        return None, None, parts[1]
    if not parts[1].isdigit():
        return None
    if len(parts) == 3:
        return parts[1], None, parts[2]
    return parts[1], parts[2], parts[3]


def _statements(ini_path):
    """(file name, line, tokens) of every statement of the user's HAL files,
    as written (an earlier plan's changes undone)."""
    out = []
    for path in integrate.user_hal_files(ini_path):
        try:
            text = integrate.original(integrate.read_hal(path, ini_path))
        except OSError:
            continue
        for a, _, t in integrate.statements(text):
            out.append((os.path.basename(path), a + 1, t))
    return out


def _is_lcec_line(t):
    return integrate._is_ecat_line(t) and not (len(t) > 1 and t[1] == "ecat")


def _xml_path(t, ini_path):
    """The XML file a loadusr of lcec_conf names, or None."""
    if t[0] != "loadusr":
        return None
    for i, x in enumerate(t):
        if os.path.basename(x) == "lcec_conf" and i + 1 < len(t):
            name = integrate.resolve(t[i + 1], integrate.read_ini(ini_path))
            return os.path.join(os.path.dirname(os.path.abspath(ini_path)), name)
    return None


def find(ini_path):
    """A Found for a configuration whose HAL files use lcec, or None."""
    from . import wiring
    stmts = _statements(ini_path)
    if not any(_is_lcec_line(t) or any(_LCEC.fullmatch(x) for x in t) for _, _, t in stmts):
        return None
    f = Found()
    e = integrate.read_ini(ini_path)
    for name, line, t in stmts:
        path = _xml_path(t, ini_path)
        if path and f.xml_path is None:
            f.xml_path = path
        if t[0] == "loadrt" and len(t) > 1 and t[1] == COMPONENT:
            f.instances += [i for i, _ in wiring._loaded(t) if i != COMPONENT]
        if t[0] == "net" and len(t) > 1:
            sig, pins = integrate.net_pins(t)
            f.signals.setdefault(sig, set()).update(pins)
        elif t[0] == "setp" and len(t) == 3:
            f.setp[t[1]] = integrate.resolve(t[2], e)
    if f.xml_path is None:
        f.notes.append("no lcec XML file is loaded (loadusr lcec_conf): the old pins get no "
                       "suggestions from it, and lcec's settings are not offered")
    elif not os.path.exists(f.xml_path):
        f.notes.append("%s (loaded by lcec_conf) does not exist: the old pins get no suggestions "
                       "from it, and lcec's settings are not offered" % os.path.basename(f.xml_path))
    else:
        try:
            f.xml = lcecxml.load(f.xml_path)
        except ValueError as err:
            f.problems.append(str(err))
        except Exception as err:      # a broken XML file: no suggestions
            f.notes.append("%s cannot be read (%s): the old pins get no suggestions from it"
                           % (os.path.basename(f.xml_path), err))
    _find_glue(f)
    glue = {g.instance for g in f.glue}
    for name, line, t in stmts:
        if _is_lcec_line(t) or _glue_line(t, glue):
            continue
        for x in t[1:]:
            if _LCEC.fullmatch(x) or any(x.startswith(i + ".") for i in glue):
                f.uses.setdefault(x, []).append((name, line))
    return f


def _glue_line(t, instances):
    """The component's loadrt, or an addf of one of these instances."""
    if t[0] == "loadrt" and len(t) > 1 and t[1] == COMPONENT:
        return True
    return t[0] == "addf" and len(t) > 1 and any(t[1].startswith(i + ".") for i in instances)


def _find_glue(f):
    for inst in f.instances:
        slaves = set()
        for pins in f.signals.values():
            if any(p == "%s.%s" % (inst, g) for g in GLUE for p in pins):
                for p in pins:
                    k = split(p)
                    if k and k[0] is not None and k[1] is not None:
                        slaves.add("lcec.%s.%s" % (k[0], k[1]))
        if not slaves:
            continue
        if len(slaves) > 1:
            f.notes.append("%s is connected to more than one lcec slave (%s): it is not replaced; "
                           "choose the new pins of its lines by hand"
                           % (inst, ", ".join(sorted(slaves))))
            continue
        g = Glue(inst, slaves.pop())
        for pins in f.signals.values():
            if "%s.pos-cmd" % inst in pins:
                for p in pins:
                    m = re.fullmatch(r"joint\.(\d+)\.motor-pos-cmd", p)
                    if m:
                        g.joint = int(m.group(1))
        g.scale = f.setp.get("%s.pos-scale" % inst)
        f.glue.append(g)


# -- the slaves

def xml_slave(f, lcec_slave):
    """The XML file's slave of an lcec pin prefix (lcec.0.X), or None."""
    if f.xml is None:
        return None
    name = lcec_slave.split(".", 2)[2]
    return next((x for x in f.xml.slaves if (x.name or str(x.idx)) == name), None)


def new_slave(f, cfg, lcec_slave):
    """The configuration's slave that takes an lcec slave's place: the one at its
    position in the XML file, or (without it) the one of the same name."""
    slaves = (cfg or {}).get("slaves") or []
    x = xml_slave(f, lcec_slave)
    if x is not None:
        return next((s for s in slaves if s.get("position") == x.idx), None)
    name = lcec_slave.split(".", 2)[2]
    return next((s for s in slaves if s.get("name") in (name, lcec_slave)), None)


def _axis(slave):
    a = config.axes(slave)
    return a[0] if a else None


def replaced(f, cfg):
    """{glue instance: the axis that replaces it} for those whose slave has the
    cia402 profile now."""
    out = {}
    for g in f.glue:
        axis = _axis(new_slave(f, cfg, g.slave))
        if axis:
            out[g.instance] = axis
    return out


def glue_notes(f, cfg):
    """What becomes of each instance of the component."""
    out = []
    rep = replaced(f, cfg)
    for g in f.glue:
        if g.instance in rep:
            out.append("The cia402 component's %s is replaced by the cia402 profile of %s%s"
                       % (g.instance, rep[g.instance],
                          "" if g.joint is None else " (joint %d)" % g.joint))
        else:
            s = new_slave(f, cfg, g.slave)
            out.append("The cia402 component's %s stays: %s" % (
                g.instance, "no slave of the configuration takes %s's place" % g.slave
                if s is None else "%s has no cia402 profile (choose it on the Bus page to "
                "replace the component)" % s.get("name")))
    return out


# -- old pins and what takes their place

def _xml_object(x, rest):
    """((index, subindex), bit or None) of the XML entry an lcec pin of slave x is."""
    for p in x.pdo_list:
        for e in p.entries:
            if e.pin == rest and not e.bits:
                return (e.index, e.subindex), None
            bit = 0
            for b in e.bits:
                if b.pin == rest and b.bitlen == 1:
                    return (e.index, e.subindex), bit
                bit += b.bitlen
    return None, None


def _entry_pin(s, obj, bit):
    """The pin of a configuration slave's entry for an object (one bit of it), or None."""
    for kind in ("outputs", "inputs"):
        for pdo in (s.get("process-data") or {}).get(kind) or []:
            for e in pdo.get("entries") or []:
                try:
                    if config.parse_obj(e.get("object")) != obj:
                        continue
                except ValueError:
                    continue
                if bit is None:
                    if "pin" in e:
                        return "%s.%s" % (s["name"], e["pin"])
                    if e.get("use") == "profile" and s.get("profile") == "cia402":
                        index = obj[0]
                        n = (index - 0x6000) // config.AXIS_OFFSET if index >= 0x6000 else 0
                        base = index - n * config.AXIS_OFFSET
                        axes = config.axes(s)
                        if base in PROFILE_OBJECTS and n < len(axes):
                            return "%s.%s" % (axes[n], PROFILE_OBJECTS[base])
                    return None
                split_ = e.get("split") or []
                if bit < len(split_) and split_[bit]:
                    return "%s.%s" % (s["name"], split_[bit])
                if e.get("bits") == 1 and "pin" in e and bit == 0:
                    return "%s.%s" % (s["name"], e["pin"])
                return None
    return None


def suggest(f, cfg, pin, pins=None):
    """(new pin or None, where it comes from or why there is none) for an old pin."""
    from . import halpins
    pins = pins if pins is not None else halpins.pins(cfg)
    rep = replaced(f, cfg)
    for inst, axis in rep.items():
        if pin.startswith(inst + "."):
            name = pin[len(inst) + 1:]
            new = RENAME.get(name, name)
            if new is None:
                return None, "ecat has no %s: the mode is cia402.mode in the configuration" % name
            target = "%s.%s" % (axis, new)
            if target in pins:
                return target, "the cia402 profile's pin"
            if new == "index-enable":
                return None, "%s has no index-enable: give the drive the hm mode (homing by the " \
                    "drive) on the Bus page" % axis
            return None, "%s has no pin %s" % (axis, new)
    k = split(pin)
    if k is None:
        return None, ""
    master, slave, rest = k
    if slave is None:
        if rest in MASTER_PINS:
            return ("ecat.0.%s" % rest if master is not None else "ecat.%s" % rest), \
                "the master's pin"
        return None, "ecat's master has no %s" % rest
    s = new_slave(f, cfg, "lcec.%s.%s" % (master, slave))
    if s is None:
        x = xml_slave(f, "lcec.%s.%s" % (master, slave))
        return None, ("no slave at position %d in the configuration" % x.idx if x is not None
                      else "no slave of the configuration is %s" % slave)
    if rest in SLAVE_PINS:
        return "%s.%s" % (s["name"], rest), "the slave's state pin"
    x = xml_slave(f, "lcec.%s.%s" % (master, slave))
    if x is not None:
        obj, bit = _xml_object(x, rest)
        if obj is not None:
            new = _entry_pin(s, obj, bit)
            where = "0x%04X:%02X%s" % (obj[0], obj[1], "" if bit is None else " bit %d" % bit)
            if new and new in pins:
                return new, "the same object (%s) in the XML file" % where
            return None, "%s (%s in the XML file) has no pin in the configuration: give its " \
                "entry a pin on the Bus page" % (s["name"], where)
    same = "%s.%s" % (s["name"], rest)
    if same in pins:
        return same, "the same name"
    return None, "nothing found in %s" % s["name"]


def choices(cfg):
    """{old pin: new pin, or None (taken out)} chosen (linuxcnc.old-pins)."""
    out = {}
    for e in ((cfg or {}).get("linuxcnc") or {}).get("old-pins") or []:
        if isinstance(e, dict) and isinstance(e.get("old"), str):
            out[e["old"]] = e.get("new")
    return out


def set_choice(cfg, old, new, decided=True):
    """Choose what takes an old pin's place (new None: it is taken out);
    decided False forgets the choice."""
    lc = cfg.setdefault("linuxcnc", {})
    es = [e for e in lc.get("old-pins") or [] if e.get("old") != old]
    if decided:
        es.append({"old": old, "new": new} if new else {"old": old})
    es.sort(key=lambda e: e["old"])
    if es:
        lc["old-pins"] = es
    else:
        lc.pop("old-pins", None)
        if not lc:
            cfg.pop("linuxcnc", None)


def auto(f, cfg):
    """{old pin: new pin or None} the glue's replacement decides."""
    from . import halpins
    out = {}
    rep = replaced(f, cfg)
    pins = halpins.pins(cfg)
    for g in f.glue:
        if g.instance not in rep:
            continue
        # the nets between the component and its slave go; where other pins
        # are on one too (a ddt reading the position), the slave's pin is the
        # profile's pin of that object now, and the component's pin goes
        for sig_pins in f.signals.values():
            ins = {p for p in sig_pins if p.startswith(g.instance + ".")
                   and p[len(g.instance) + 1:] in GLUE}
            lc = {p for p in sig_pins if p.startswith(g.slave + ".")}
            if not (ins and lc):
                continue
            if sig_pins <= ins | lc:
                out.update({p: None for p in sig_pins})
                continue
            out.update({p: None for p in ins})
            for p in lc:
                new, _ = suggest(f, cfg, p, pins)
                if new:
                    out[p] = new
        for p in f.uses:
            if p.startswith(g.instance + ".") and p not in out:
                name = p[len(g.instance) + 1:]
                new = RENAME.get(name, name)
                target = "%s.%s" % (rep[g.instance], new) if new else None
                if new is None or target in pins:
                    out[p] = target
    return out


def rows(f, cfg):
    """[Row] of the old pins the HAL files use, in the order of their first use."""
    from . import halpins
    pins = halpins.pins(cfg)
    a, chosen = auto(f, cfg), choices(cfg)
    out = []
    for pin, uses in f.uses.items():
        if pin in pins:         # the slave was given lcec's name: the pin is the same
            continue
        r = Row(pin, uses)
        r.suggestion, r.why = suggest(f, cfg, pin, pins)
        if pin in chosen:
            r.decided, r.new = True, chosen[pin]
        elif pin in a:
            r.decided, r.new, r.auto = True, a[pin], True
        out.append(r)
    return out


def mapping(f, cfg):
    """{old pin: new pin or None} for every decided old pin."""
    return {r.pin: r.new for r in rows(f, cfg) if r.decided}


def substitute(t, m, gone):
    """A statement with the old pins replaced: new tokens, None (it goes), or t
    itself when it has none. m: mapping(); gone: the glue instances replaced."""
    from . import wiring
    if t[0] == "loadrt" and len(t) > 1 and t[1] == COMPONENT and gone:
        made = [i for i, _ in wiring._loaded(t) if i != COMPONENT]
        keep = [i for i in made if i not in gone]
        if len(keep) == len(made):
            return t
        if not keep:
            return None
        names = [x for x in t[2:] if x.startswith("names=")]
        if not names:
            return t            # count=: the instances cannot be taken out one by one
        return [x if not x.startswith("names=") else "names=" + ",".join(keep) for x in t]
    if t[0] == "addf" and len(t) > 1 and any(t[1].startswith(i + ".") for i in gone):
        return None
    if not any(x in m for x in t):
        return t
    if t[0] == "net" and len(t) > 1:
        groups, arrows, cur = [], [], []
        for x in t[2:]:
            if x in integrate.ARROWS:
                groups.append(cur)
                arrows.append(x)
                cur = []
            elif x in m:
                if m[x] is not None:
                    cur.append(m[x])
            else:
                cur.append(x)
        groups.append(cur)
        if not any(groups):
            return None
        out = ["net", t[1]] + groups[0]
        for arrow, g in zip(arrows, groups[1:]):
            if g:
                out += [arrow] + g
        return out
    if t[0] in ("setp", "sets") and len(t) >= 2:
        if t[1] in m:
            return None if m[t[1]] is None else [t[0], m[t[1]]] + t[2:]
        return t
    if len(t) == 3 and t[1] == "=" and t[0] in m:
        return None if m[t[0]] is None else [m[t[0]]] + t[1:]
    if any(x in m and m[x] is None for x in t):
        return None
    return [m.get(x, x) for x in t]


def substituter(ini_path, cfg):
    """A function that gives a statement of the HAL files with the old pins
    replaced (see substitute); one that changes nothing when lcec is not used."""
    try:
        f = find(ini_path) if ini_path and cfg is not None else None
    except OSError:
        f = None
    if f is None:
        return lambda t: t
    m, gone = mapping(f, cfg), set(replaced(f, cfg))
    return lambda t: substitute(t, m, gone)


def propose_joints(f, cfg):
    """{joint: (axis, scale)} of the glue instances that are replaced."""
    out = {}
    rep = replaced(f, cfg)
    for g in f.glue:
        if g.joint is None or g.instance not in rep:
            continue
        try:
            scale = float(g.scale) if g.scale is not None else 1.0
        except ValueError:
            scale = 1.0
        out[g.joint] = (rep[g.instance], scale)
    return out


# -- the slaves' names and settings

def names(f, cfg):
    """{configuration slave name: the XML file's name} where they differ and
    the XML name can be taken."""
    if f.xml is None:
        return {}
    taken = {s.get("name") for s in cfg.get("slaves") or []}
    taken |= {a for s in cfg.get("slaves") or [] for a in config.axes(s)}
    out = {}
    for x in f.xml.slaves:
        s = next((s for s in cfg.get("slaves") or [] if s.get("position") == x.idx), None)
        want = x.name
        if s is None or not want or s.get("name") == want:
            continue
        if not config._NAME.match(want) or want in taken or want in out.values():
            continue
        out[s["name"]] = want
    return out


def take_names(f, cfg):
    """Name the slaves as the XML file does; returns {old: new} of the pin
    prefixes renamed."""
    renamed = {}
    for old, new in names(f, cfg).items():
        s = next(s for s in cfg["slaves"] if s.get("name") == old)
        s["name"] = new
        renamed.update(integrate.rename_slave(cfg, old, new))
    return renamed


@dataclass
class SettingHint:
    position: int
    slave: str                  # the configuration's slave name
    setting: dict = None        # as in the slave's settings; None for zero-at-start
    now: object = None          # the configuration's value of that object, or None
    what: str = ""              # where it comes from
    zero_at_start: bool = False


_SIZES = {"SINT": 1, "USINT": 1, "INT": 2, "UINT": 2, "DINT": 4, "UDINT": 4, "LINT": 8,
          "ULINT": 8}


def _value(st):
    return st.get("value") if "value" in st else st.get("data")


def _typed(st, cur):
    """A setting given as raw bytes, as a number of the type the configuration
    has for that object (when the sizes agree), so that the two compare."""
    if "data" not in st or cur is None or _SIZES.get(cur.get("type")) is None:
        return st
    try:
        data = bytes.fromhex(st["data"])
    except ValueError:
        return st
    if len(data) != _SIZES[cur["type"]]:
        return st
    out = {"object": st["object"]}
    if cur.get("name"):
        out["name"] = cur["name"]
    out["type"] = cur["type"]
    out["value"] = int.from_bytes(data, "little", signed=not cur["type"].startswith("U"))
    return out


def setting_hints(f, cfg, dictionaries=None):
    """[SettingHint] of the settings lcec wrote at every start (sdoConfig, and its
    modParams as the quirk files translate them) that the configuration does not
    have, or has with another value. dictionaries: {position: coe.Dictionary}."""
    from . import importxml, quirks
    if f.xml is None:
        return []
    out = []
    for x in f.xml.slaves:
        s = next((s for s in cfg.get("slaves") or [] if s.get("position") == x.idx), None)
        if s is None:
            continue
        ident = quirks.identity(s)
        if ident is not None and ident != (x.vendor_id, x.product_code):
            continue
        coe = (dictionaries or {}).get(x.idx)
        have = {}
        for st in s.get("settings") or []:
            try:
                have[config.parse_obj(st.get("object"))] = st
            except ValueError:
                pass
        replace = quirks.lcec_sdo((x.vendor_id, x.product_code))
        rows_ = []
        for index, sub, data in x.sdos:
            if sub == "complete":
                continue
            if (index, sub) in replace:
                what, why = replace[(index, sub)]
                if what == "cia402.zero-at-start" and s.get("profile") == "cia402" \
                        and not (s.get("cia402") or {}).get("zero-at-start"):
                    out.append(SettingHint(x.idx, s["name"], what="lcec's sdoConfig 0x%04X:%02X: "
                                           "%s" % (index, sub, why), zero_at_start=True))
                continue
            if quirks.command(s, index):
                continue
            rows_.append((importxml._setting(index, sub, data, coe), "lcec's sdoConfig"))
        if x.modparams:
            params, _ = quirks.lcec_modparams(x.type, x.modparams)
            for index, sub, dtype, value, name in params:
                st = {"object": config.obj(index, sub), "type": dtype, "value": value}
                if coe is not None and (index, sub) in coe.entries:
                    st["name"] = coe.name(index, sub)
                rows_.append((st, "lcec's modParam %s" % name if name else "lcec's driver"))
        for st, what in rows_:
            obj = config.parse_obj(st["object"])
            cur = have.get(obj)
            st = _typed(st, cur)
            if cur is not None and _value(cur) == _value(st):
                continue
            out.append(SettingHint(x.idx, s["name"], st, None if cur is None else _value(cur),
                                   what))
    return out


def add_setting(cfg, hint):
    """Put a setting hint into the configuration."""
    s = next(s for s in cfg["slaves"] if s.get("position") == hint.position)
    if hint.zero_at_start:
        s.setdefault("cia402", {})["zero-at-start"] = True
        return
    obj = config.parse_obj(hint.setting["object"])
    settings = [st for st in s.get("settings") or []
                if not _same_obj(st.get("object"), obj)]
    settings.append(dict(hint.setting))
    s["settings"] = settings


def _same_obj(text, obj):
    try:
        return config.parse_obj(text) == obj
    except ValueError:
        return False


def check(cfg):
    """Errors in linuxcnc.old-pins."""
    from . import halpins
    lc = cfg.get("linuxcnc")
    if not isinstance(lc, dict) or lc.get("old-pins") is None:
        return []
    es = lc["old-pins"]
    if not isinstance(es, list):
        return ["linuxcnc.old-pins: must be a list"]
    errors, seen = [], set()
    pins = halpins.pins(cfg)
    for e in es:
        if not isinstance(e, dict) or not isinstance(e.get("old"), str) or \
                set(e) - {"old", "new"}:
            errors.append("linuxcnc.old-pins: each entry must be a mapping with old and, "
                          "unless the pin is taken out, new")
            continue
        where = "linuxcnc.old-pins: %s" % e["old"]
        if e["old"] in seen:
            errors.append("%s: listed twice" % where)
        seen.add(e["old"])
        new = e.get("new")
        if new is not None and (not isinstance(new, str) or new not in pins):
            errors.append("%s: %r is not a pin or parameter of the configuration" % (where, new))
    return errors
