"""Writing an ecat configuration into a LinuxCNC configuration: the HAL
files, the INI changes, and the joints connected to drives.

Two HAL files are written next to the INI file:

- ethercat-load.hal, the first HALFILE, loads the component, so that the
  slaves' pins exist for the user's own HAL files;
- ethercat.hal, the last HALFILE, adds the component's functions to the
  servo thread (which exists only once motmod is loaded, in the user's
  files) and connects the joints to their drives.

The user's own HAL files are changed only where they would clash: lines
that load lcec or ecat, add their functions, connect a pin that
ethercat.hal now connects, or set a connected drive's pos-scale are
commented out with COMMENT in front. The signal names of those lines are
kept in ethercat.hal, so that other lines using them still work. Every
changed file is kept as .bak.

Nothing here uses Qt; plan() shows what apply() would write.
"""

import os
import re
import shutil
from dataclasses import dataclass, field

from . import config

LOAD_FILE = "ethercat-load.hal"
MAIN_FILE = "ethercat.hal"
COMMENT = "#ecatsetup: "
HEADER = ("# Written by EtherCAT Setup; it is rewritten when the program writes the\n"
          "# configuration again. Change the joints and drives in the program.\n")
ARROWS = {"=>", "<=", "<=>"}

# a joint's pin, the drive's pin, and which way the value goes
CONNECTIONS = [
    ("motor-pos-cmd", "pos-cmd", "=>", "pos-cmd"),
    ("motor-pos-fb", "pos-fb", "<=", "pos-fb"),
    ("amp-enable-out", "enable", "=>", "enable"),
    ("amp-fault-in", "drv-fault", "<=", "amp-fault"),
]
HOMING = ("index-enable", "index-enable", "<=>", "index-enable")


# -- the INI file

EXPANDED = ".expanded"


def include_path(ini_path, name):
    """The file an #INCLUDE line names, as the linuxcnc script finds it:
    ~ and $VARIABLES expanded, relative to the INI file's folder."""
    name = os.path.expandvars(os.path.expanduser(name))
    return os.path.join(os.path.dirname(os.path.abspath(ini_path)), name)


def ini_lines(path):
    """[(file, line)] of an INI file with its #INCLUDEs in place, as the
    linuxcnc script expands them before anything reads the file: only the
    main file's "#INCLUDE <file>" lines count (an included file is copied
    as it is), and a file that cannot be read is left out. The #INCLUDE
    line itself stays, as a comment of the main file."""
    out = []
    for line in read_text(path).splitlines(keepends=True):
        out.append((path, line))
        f = line.split()
        if len(f) >= 2 and f[0] == "#INCLUDE":
            inc = include_path(path, line.strip()[len("#INCLUDE"):].strip())
            try:
                text = read_text(inc)
            except OSError:
                continue
            if text and not text.endswith("\n"):
                text += "\n"
            out += [(inc, x) for x in text.splitlines(keepends=True)]
    return out


def ini_includes(path):
    """The files the INI file's #INCLUDE lines name that exist."""
    return list(dict.fromkeys(f for f, _ in ini_lines(path) if f != path))


def expanded_source(path):
    """For the file LinuxCNC writes from an INI file with #INCLUDEs
    (<name>.ini.expanded, written again at every start), the INI file it is
    made from; None for any other file."""
    if path.endswith(EXPANDED):
        src = path[:-len(EXPANDED)]
        return src if os.path.exists(src) else None
    return None


def _sections(lines):
    """Each line's section, and whether it is in the first section of that
    name: LinuxCNC's INI reader looks a key up in the first [SECTION] only."""
    out, current, first, seen = [], None, False, set()
    for line in lines:
        t = line.strip()
        if t.startswith("[") and "]" in t:
            current = t[1:t.index("]")].strip()
            first = current not in seen
            seen.add(current)
        out.append((current, first))
    return out


def read_ini(path):
    """[(section, key, value)] in file order, as LinuxCNC reads them:
    #INCLUDEs expanded, and only the first section of each name."""
    out = []
    lines = [x for _, x in ini_lines(path)]
    for line, (section, first) in zip(lines, _sections(lines)):
        s = line.strip()
        if section and first and "=" in s and not s.startswith(("#", ";", "[")):
            k, v = s.split("=", 1)
            out.append((section, k.strip(), v.split("#")[0].strip()))
    return out


def ini_get(entries, section, key, default=None):
    for s, k, v in entries:
        if s == section and k == key:
            return v
    return default


def ini_all(entries, section, key):
    return [v for s, k, v in entries if s == section and k == key]


def ini_joints(ini_path):
    """[(joint number, axis letter or "", joint type)] from the INI file."""
    e = read_ini(ini_path)
    try:
        n = int(ini_get(e, "KINS", "JOINTS") or 0)
    except ValueError:
        n = 0
    letters = ""
    kins = ini_get(e, "KINS", "KINEMATICS") or ""
    m = re.search(r"coordinates=([A-Za-z]+)", kins)
    if m:
        letters = m.group(1).upper()
    else:
        letters = (ini_get(e, "KINS", "COORDINATES") or ini_get(e, "TRAJ", "COORDINATES")
                   or "").replace(" ", "").upper()
    return [(j, letters[j] if len(letters) == n else "",
             ini_get(e, "JOINT_%d" % j, "TYPE", "LINEAR")) for j in range(n)]


HALLIB = os.environ.get("HALLIB_DIR") or "/usr/share/linuxcnc/hallib"


def find_hal_file(d, name):
    """Where LinuxCNC finds a HALFILE: the configuration folder, then its
    library (HALLIB_PATH is .:HALLIB_DIR); LIB: names only the library."""
    if name.startswith("LIB:"):
        return os.path.join(HALLIB, name[4:])
    path = os.path.join(d, name)
    if os.path.exists(path):
        return path
    lib = os.path.join(HALLIB, name)
    return lib if os.path.exists(lib) else path


def user_hal_files(ini_path, postgui=None):
    """The INI's HAL files other than ecat's own, as paths. postgui True
    gives only the POSTGUI_HALFILEs, False only the HALFILEs."""
    d = os.path.dirname(os.path.abspath(ini_path))
    e = read_ini(ini_path)
    files = []
    for key in ("HALFILE", "POSTGUI_HALFILE"):
        if postgui is not None and postgui != (key == "POSTGUI_HALFILE"):
            continue
        for v in ini_all(e, "HAL", key):
            name = v.split()[0] if v.split() else ""
            if name in (LOAD_FILE, MAIN_FILE) or not name:
                continue
            files.append(find_hal_file(d, name))
    return files


def hal_entries(ini_path):
    """[(INI value, path, postgui)] of the INI's HAL files other than ecat's."""
    d = os.path.dirname(os.path.abspath(ini_path))
    out = []
    for key in ("HALFILE", "POSTGUI_HALFILE"):
        for v in ini_all(read_ini(ini_path), "HAL", key):
            name = v.split()[0] if v.split() else ""
            if name in (LOAD_FILE, MAIN_FILE) or not name:
                continue
            out.append((v, find_hal_file(d, name), key == "POSTGUI_HALFILE"))
    return out


# LinuxCNC's simulated configurations (configs/sim): files that simulate
SIM_FILES = {
    "simulated_home.hal": "home switches", "simulated_home_lathe.hal": "home switches",
    "simulated-gantry-home.hal": "home switches", "simulated-xyyz-home.hal": "home switches",
    "simulated_limits.hal": "limit switches", "sim_spindle_encoder.hal": "the spindle",
    "spindle_sim.hal": "the spindle", "gantrysim.hal": "a gantry's joints",
}
_SWITCH = re.compile(r"joint\.\d+\.(home-sw-in|pos-lim-sw-in|neg-lim-sw-in)$")
BASIC_SIM = "basic_sim.tcl"

# outputs of components, by HAL's naming: a pin that writes its signal
_WRITER = re.compile(r"\.(out|out-not|out-invert|home-sw|position-fb|velocity-fb)$")


def basic_sim_file(ini_path):
    """The HAL file LIB:basic_sim.tcl saved when the configuration last ran
    (<ini name>_cmds.hal in its folder), or None."""
    d = os.path.dirname(os.path.abspath(ini_path))
    path = os.path.join(d, os.path.splitext(os.path.basename(ini_path))[0] + "_cmds.hal")
    return path if os.path.exists(path) else None


def ini_texts(ini_path, yaml_name, replace=None):
    """{file: new text} of the INI file and the files it includes, with
    [ETHERCAT]CONFIG and the two HALFILEs; replace: {HALFILE value: new
    value} (a file copied into the folder). The HALFILEs go into the first
    [HAL] section, in the file that holds it (an #INCLUDEd one too), since
    LinuxCNC reads only that one; a new [ETHERCAT] goes just before it."""
    nl = "\n"
    tagged = [list(x) for x in ini_lines(ini_path)]   # [file, line]
    for x in tagged:
        t = x[1].strip()
        if replace and "=" in t and t.split("=", 1)[0].strip() in ("HALFILE", "POSTGUI_HALFILE"):
            v = t.split("=", 1)[1].split("#")[0].strip()
            if v in replace:
                x[1] = "%s = %s%s" % (t.split("=", 1)[0].strip(), replace[v], nl)

    def key_of(line):
        t = line.strip()
        if "=" not in t or t.startswith(("#", ";", "[")):
            return None
        return t.split("=", 1)[0].strip()

    def in_first(name):
        secs = _sections([x[1] for x in tagged])
        return [i for i, (s, first) in enumerate(secs) if s == name and first]

    # [HAL]: ours out (from every [HAL]), then in again where they belong
    secs = _sections([x[1] for x in tagged])
    tagged = [x for x, (s, _) in zip(tagged, secs)
              if not (s == "HAL" and key_of(x[1]) == "HALFILE"
                      and x[1].split("=", 1)[1].split("#")[0].strip() in (LOAD_FILE, MAIN_FILE))]
    hal = in_first("HAL")
    if not hal:
        mine = [x for x in tagged if x[0] == ini_path]
        if mine and not mine[-1][1].endswith("\n"):
            mine[-1][1] += nl
        tagged += [[ini_path, nl], [ini_path, "[HAL]" + nl]]
        hal = [len(tagged) - 1]
    hal_file = tagged[hal[0]][0]
    halfiles = [i for i in hal if key_of(tagged[i][1]) == "HALFILE"]
    first = halfiles[0] if halfiles else hal[0] + 1
    last = (halfiles[-1] + 1) if halfiles else hal[0] + 1
    tagged.insert(last, [hal_file, "HALFILE = %s%s" % (MAIN_FILE, nl)])
    tagged.insert(first, [hal_file, "HALFILE = %s%s" % (LOAD_FILE, nl)])

    # [ETHERCAT]CONFIG, in the first [ETHERCAT], or a new one before [HAL]
    eth = in_first("ETHERCAT")
    cfg_line = "CONFIG = %s%s" % (yaml_name, nl)
    keys = [i for i in eth if key_of(tagged[i][1]) == "CONFIG"]
    if keys:
        tagged[keys[0]][1] = cfg_line
    elif eth:
        tagged.insert(eth[0] + 1, [tagged[eth[0]][0], cfg_line])
    else:
        at = in_first("HAL")[0]
        tagged[at:at] = [[hal_file, "[ETHERCAT]" + nl], [hal_file, cfg_line], [hal_file, nl]]

    out = {}
    for f, line in tagged:
        out[f] = out.get(f, "") + line
    return out


def ini_text(ini_path, yaml_name, replace=None):
    """The main INI file's new text (see ini_texts)."""
    return ini_texts(ini_path, yaml_name, replace).get(ini_path, "")


# -- the user's HAL files

def read_text(path):
    with open(path, errors="replace") as f:
        return f.read()


def is_tcl(path):
    return path.endswith(".tcl")


def read_hal(path, ini_path=None):
    """A HAL file's text; a .tcl one as the plain HAL it gives with the INI
    file's values (haltcl.py). Raises OSError."""
    if is_tcl(path):
        from . import haltcl
        return haltcl.expand(path, ini_path).text
    return read_text(path)


ADDED = "  " + COMMENT + "added"    # the end of a line an earlier plan put in


def original(text):
    """A HAL file's text as the user wrote it: lines commented out by an
    earlier plan are back, and lines it added are gone, so that every plan
    starts from the same text."""
    return "".join(line[len(COMMENT):] if line.startswith(COMMENT) else line
                   for line in text.splitlines(keepends=True)
                   if not line.rstrip("\n").endswith(ADDED))

def statements(text):
    """(first line index, last line index, tokens) of each HAL statement,
    with comments left out and backslash continuations joined."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        start, parts = i, []
        while True:
            body = lines[i].split("#", 1)[0].rstrip()
            if body.endswith("\\"):
                parts.append(body[:-1])
                if i + 1 < len(lines):
                    i += 1
                    continue
            else:
                parts.append(body)
            break
        tokens = " ".join(parts).split()
        if tokens:
            yield start, i, tokens
        i += 1


def net_pins(tokens):
    """A net statement's signal and pins."""
    rest = [t for t in tokens[2:] if t not in ARROWS]
    return tokens[1], rest


def signals(paths, ini_path=None):
    """signal -> set of pins, over HAL files (.tcl ones with ini_path's
    values)."""
    sig = {}
    for path in paths:
        try:
            text = original(read_hal(path, ini_path))
        except OSError:
            continue
        for _, _, t in statements(text):
            if t[0] == "net" and len(t) > 1:
                name, pins = net_pins(t)
                sig.setdefault(name, set()).update(pins)
    return sig


def setp_values(paths, ini_path=None):
    """pin or parameter -> the value it is set to, over HAL files."""
    out = {}
    for path in paths:
        try:
            text = original(read_hal(path, ini_path))
        except OSError:
            continue
        for _, _, t in statements(text):
            if t[0] == "setp" and len(t) == 3:
                out[t[1]] = t[2]
            elif len(t) == 3 and t[1] == "=" and "." in t[0]:
                out[t[0]] = t[2]
    return out


def resolve(value, ini_entries):
    """A HAL value, with [SECTION]KEY taken from the INI file."""
    m = re.fullmatch(r"\[([^\]]+)\](\S+)", value or "")
    if m:
        return ini_get(ini_entries, m.group(1), m.group(2))
    return value


def drives(cfg):
    """{name: slave} of the drives a joint can be connected to: the pin
    prefix of each CSP axis (the slave's name, or its cia402.axes)."""
    out = {}
    for s in (cfg or {}).get("slaves") or []:
        if s.get("profile") == "cia402" and (s.get("cia402") or {}).get("mode") == "csp":
            for a in config.axes(s):
                out[a] = s
    return out


def scale_from(counts_per_rev, units_per_rev, gear=1.0):
    """A joint's pos-scale (counts per machine unit) from the drive's counts
    per motor revolution, the machine units per revolution of the screw or
    pulley (its lead), and the gear between motor and screw (motor turns
    per screw turn)."""
    if not counts_per_rev or not units_per_rev or not gear:
        raise ValueError("counts per revolution, units per revolution and the gear must not be 0")
    return float(counts_per_rev) * float(gear) / float(units_per_rev)


def joint_checks(ini_path, cfg, max_counts=None):
    """Notes on the INI file's joints against their drives: limits that
    are missing or more than a drive can do. max_counts: {drive: its
    maximum velocity in counts per second (0x607F)}, where read."""
    out = []
    e = read_ini(ini_path)
    if ini_get(e, "EMCMOT", "SERVO_PERIOD") is None:
        out.append("[EMCMOT]SERVO_PERIOD is not set: the servo thread runs at LinuxCNC's "
                   "default, 1 ms, which is also the EtherCAT cycle")
    for j in joints(cfg):
        sec = "JOINT_%d" % j["joint"]
        for key in ("FERROR", "MIN_FERROR"):
            if ini_get(e, sec, key) is None:
                out.append("[%s]%s is not set: LinuxCNC's default is used; set it for the "
                           "machine (in machine units)" % (sec, key))
        try:
            v = float(resolve(ini_get(e, sec, "MAX_VELOCITY"), e))
        except (TypeError, ValueError):
            continue
        limit = (max_counts or {}).get(j.get("slave"))
        counts = abs(v * float(j.get("scale") or 1.0))
        if limit and counts > limit:
            out.append("[%s]MAX_VELOCITY %g units/s is %d counts/s, more than %s's maximum "
                       "profile velocity (0x607F) of %d counts/s" % (sec, v, counts,
                                                                     j.get("slave"), limit))
    return out


def check_at_start(cfg):
    """linuxcnc.check-hal-at-start: run the HAL check when LinuxCNC starts."""
    return ((cfg or {}).get("linuxcnc") or {}).get("check-hal-at-start") is True


def joints(cfg):
    """The configuration's joint list: [{"joint", "slave", "scale"}]."""
    return ((cfg or {}).get("linuxcnc") or {}).get("joints") or []


def set_joint(cfg, joint, slave=None, scale=None):
    """Connect a joint to a drive, or (slave None) to nothing."""
    lc = cfg.setdefault("linuxcnc", {})
    js = [j for j in lc.get("joints") or [] if j.get("joint") != joint]
    if slave:
        js.append({"joint": joint, "slave": slave, "scale": float(scale or 1.0)})
    js.sort(key=lambda j: j["joint"])
    if js:
        lc["joints"] = js
    else:
        lc.pop("joints", None)
        if not lc:
            cfg.pop("linuxcnc", None)


def rename_slave(cfg, old, new):
    """A slave renamed from old to new: its joints and nets follow, and so
    do its axes named after it (old-1, old-2 become new-1, new-2)."""
    renamed = {old: new}
    for s in (cfg or {}).get("slaves") or []:
        ax = (s.get("cia402") or {}).get("axes") if s.get("name") == new else None
        if isinstance(ax, list):
            for i, a in enumerate(ax):
                if isinstance(a, str) and a.startswith(old + "-"):
                    ax[i] = renamed[a] = new + a[len(old):]
    for j in joints(cfg):
        if j.get("slave") in renamed:
            j["slave"] = renamed[j["slave"]]
    for n in nets(cfg):
        out = []
        for p in n.get("pins") or []:
            prefix = next((o for o in renamed if p.startswith(o + ".")), None)
            out.append(renamed[prefix] + p[len(prefix):] if prefix else p)
        n["pins"] = out
    return renamed


def rename_in_hal(ini_path, renamed):
    """What renaming pin prefixes ({old: new}, from rename_slave) changes in
    the user's HAL files in the configuration folder: a Plan of FileChanges
    and how many pins each file has. Comments are left alone, and so are
    ecatsetup's own files (written again) and LinuxCNC's hallib."""
    p = Plan()
    d = os.path.dirname(os.path.abspath(ini_path))
    pats = [(re.compile(r"(?<!\S)" + re.escape(old + ".")), new + ".")
            for old, new in sorted(renamed.items(), key=lambda kv: -len(kv[0])) if old != new]
    if not pats:
        return p
    for path in user_hal_files(ini_path):
        if not path.endswith(".hal") or os.path.dirname(os.path.abspath(path)) != d:
            continue
        try:
            text = read_text(path)
        except OSError:
            continue
        count, lines = 0, []
        for line in text.splitlines(keepends=True):
            body, hash_, comment = line.partition("#")
            for pat, new in pats:
                body, n = pat.subn(new, body)
                count += n
            lines.append(body + hash_ + comment)
        new_text = "".join(lines)
        if new_text != text:
            p.changes.append(FileChange(path, "change", text, new_text,
                                        "%d pin%s renamed" % (count, "" if count == 1 else "s")))
    return p


# -- inputs and outputs: nets between the slaves' pins and LinuxCNC's

# LinuxCNC pins a slave's pin is often connected to: (pattern, HAL type,
# direction seen from HAL: "in" is read by LinuxCNC, "out" written by it)
KNOWN_PINS = [
    (r"iocontrol\.0\.emc-enable-in", "bool", "in"),
    (r"iocontrol\.0\.user-enable-out", "bool", "out"),
    (r"iocontrol\.0\.user-request-enable", "bool", "out"),
    (r"iocontrol\.0\.(coolant-flood|coolant-mist|lube)", "bool", "out"),
    (r"iocontrol\.0\.lube_level", "bool", "in"),
    (r"motion\.digital-in-\d\d", "bool", "in"),
    (r"motion\.digital-out-\d\d", "bool", "out"),
    (r"motion\.analog-in-\d\d", "real", "in"),
    (r"motion\.analog-out-\d\d", "real", "out"),
    (r"motion\.probe-input", "bool", "in"),
    (r"motion\.feed-hold", "bool", "in"),
    (r"motion\.enable", "bool", "in"),
    (r"joint\.\d+\.(home-sw-in|pos-lim-sw-in|neg-lim-sw-in|amp-fault-in)", "bool", "in"),
    (r"joint\.\d+\.amp-enable-out", "bool", "out"),
    (r"joint\.\d+\.motor-pos-cmd", "real", "out"),
    (r"joint\.\d+\.motor-pos-fb", "real", "in"),
    (r"spindle\.\d+\.(on|forward|reverse|brake)", "bool", "out"),
    (r"spindle\.\d+\.(speed-out|speed-out-abs|speed-out-rps|speed-out-rps-abs)",
     "real", "out"),
    (r"spindle\.\d+\.(at-speed|is-oriented|orient-fault-in)", "bool", "in"),
    (r"spindle\.\d+\.(speed-in)", "real", "in"),
    (r"halui\.(machine|estop|program|mist|flood|lube)\.(on|off|activate|reset|run|pause|"
     r"resume|step|stop)", "bool", "in"),
    (r"halui\.machine\.is-on", "bool", "out"),
    (r"halui\.estop\.is-activated", "bool", "out"),
    (r"halui\.program\.is-(running|paused|idle)", "bool", "out"),
    # plasmac (QtPlasmaC's component): the torch and its sensors
    (r"plasmac\.(arc-ok-in|float-switch|ohmic-probe|breakaway|move-up|move-down)", "bool", "in"),
    (r"plasmac\.arc-voltage-in", "real", "in"),
    (r"plasmac\.(torch-on|ohmic-enable|scribe-arm|scribe-on)", "bool", "out"),
]

# suggestions offered in the window, by HAL type and direction there
SUGGESTIONS = [
    "iocontrol.0.emc-enable-in", "iocontrol.0.user-enable-out",
    "iocontrol.0.user-request-enable", "iocontrol.0.coolant-flood",
    "iocontrol.0.coolant-mist", "motion.probe-input", "motion.feed-hold",
] + ["motion.digital-in-%02d" % i for i in range(4)] + \
    ["motion.digital-out-%02d" % i for i in range(4)] + \
    ["motion.analog-in-%02d" % i for i in range(4)] + \
    ["motion.analog-out-%02d" % i for i in range(4)] + [
    "spindle.0.on", "spindle.0.forward", "spindle.0.reverse", "spindle.0.speed-out",
    "spindle.0.speed-out-abs", "spindle.0.at-speed", "spindle.0.speed-in",
    "halui.machine.on", "halui.machine.off", "halui.estop.activate", "halui.estop.reset",
    "halui.program.run", "halui.program.pause", "halui.program.resume",
    "halui.program.stop", "halui.machine.is-on", "halui.estop.is-activated",
]

# HAL takes any name without blanks; '#' would start a comment in a HAL file
SIGNAL_RE = re.compile(r"[^\s#<=>][^\s#]{0,46}")
PIN_RE = SIGNAL_RE


def known_pin(pin):
    """(HAL type, direction) of a LinuxCNC pin in KNOWN_PINS, or None."""
    for pattern, t, d in KNOWN_PINS:
        if re.fullmatch(pattern, pin):
            return t, d
    return None


def suggestions(ini_path=None):
    """LinuxCNC pins to offer, with the INI file's joints' switches."""
    out = list(SUGGESTIONS)
    if ini_path:
        try:
            for j, _, _ in ini_joints(ini_path):
                out += ["joint.%d.%s" % (j, x) for x in ("home-sw-in", "pos-lim-sw-in",
                                                         "neg-lim-sw-in")]
        except OSError:
            pass
    return out


def slave_pins(cfg):
    """{pin: (HAL type, direction)} of the slaves' process data pins;
    direction as seen from HAL: an input of the slave is an "out" pin."""
    out = {}
    for s in (cfg or {}).get("slaves") or []:
        name = s.get("name")
        for kind in ("outputs", "inputs"):
            way = "in" if kind == "outputs" else "out"
            for pdo in (s.get("process-data") or {}).get(kind) or []:
                for e in pdo.get("entries") or []:
                    if "pin" in e:
                        out["%s.%s" % (name, e["pin"])] = (e.get("hal-type"), way)
                    for p in e.get("split") or []:
                        if p:
                            out["%s.%s" % (name, p)] = ("bool", way)
    return out


def own_prefixes(cfg):
    """The pin prefixes of the configuration's slaves and axes: "<name>."."""
    out = []
    for s in (cfg or {}).get("slaves") or []:
        out += [x + "." for x in [s.get("name")] + config.axes(s) if x]
    return list(dict.fromkeys(out))


def is_own(cfg, pin):
    return any(pin.startswith(p) for p in own_prefixes(cfg))


def nets(cfg):
    """The configuration's nets: [{"signal", "pins"}]."""
    return ((cfg or {}).get("linuxcnc") or {}).get("nets") or []


def net_of(cfg, pin):
    return next((n for n in nets(cfg) if pin in (n.get("pins") or [])), None)


def default_signal(pin):
    return pin.replace(".", "-")


def set_net(cfg, pin, others, signal=None):
    """Connect a slave's pin to other pins (others empty: to nothing). Its
    net keeps its signal name unless one is given."""
    others = [p for p in dict.fromkeys(others or []) if p and p != pin]
    lc = cfg.setdefault("linuxcnc", {})
    ns = [n for n in lc.get("nets") or []]
    old = next((n for n in ns if pin in (n.get("pins") or [])), None)
    if old is not None:
        ns.remove(old)
    if others:
        ns.append({"signal": signal or (old or {}).get("signal") or default_signal(pin),
                   "pins": [pin] + others})
    ns.sort(key=lambda n: n.get("signal") or "")
    if ns:
        lc["nets"] = ns
    else:
        lc.pop("nets", None)
        if not lc:
            cfg.pop("linuxcnc", None)


def joint_pins(cfg):
    """{pin: joint} of the pins a joint's connection takes."""
    out = {}
    for n, _, _, conns in _managed(cfg):
        for c in conns:
            out[c[0]] = out[c[1]] = n
    return out


def propose_nets(ini_path, cfg):
    """{signal: [pins]} as the user's HAL files (not the POSTGUI ones)
    connect the slaves' pins, for signals the configuration does not have
    and that no joint uses."""
    files = user_hal_files(ini_path, postgui=False)
    taken = set(joint_pins(cfg))
    # a CSP drive's joint pins belong to the Joints, connected or not
    taken |= {"%s.%s" % (name, c[1]) for name in drives(cfg) for c in CONNECTIONS + [HOMING]}
    have = {p for n in nets(cfg) for p in n.get("pins") or []}
    have_sig = {n.get("signal") for n in nets(cfg)}
    # the pins from files in the configuration folder, which can change;
    # lines elsewhere (LinuxCNC's hallib) keep using the signal's name
    d = os.path.dirname(os.path.abspath(ini_path))
    sig = signals([f for f in files if os.path.dirname(os.path.abspath(f)) == d], ini_path)
    found = {name: pins for name, pins in sig.items() if any(is_own(cfg, x) for x in pins)}
    out = {}
    for name, pins in found.items():
        pins = sorted(pins, key=lambda x: (not is_own(cfg, x), x))
        if name in have_sig or not any(is_own(cfg, p) for p in pins) or len(pins) < 2:
            continue
        if taken & set(pins) or have & set(pins) or any(p.startswith("joint.") and
                                                         "-sw-in" not in p for p in pins):
            continue
        out[name] = pins
    return out


def propose_joints(ini_path, cfg):
    """{joint: (slave name, scale)} as the user's HAL files connect them,
    for joints the configuration does not connect yet."""
    files = user_hal_files(ini_path)
    sig = signals(files, ini_path)
    setp = setp_values(files, ini_path)
    e = read_ini(ini_path)
    have = {j["joint"] for j in joints(cfg)}
    out = {}
    for j, _, _ in ini_joints(ini_path):
        if j in have:
            continue
        pin = "joint.%d.motor-pos-cmd" % j
        for pins in sig.values():
            if pin not in pins:
                continue
            for name in drives(cfg):
                if "%s.pos-cmd" % name in pins:
                    try:
                        scale = float(resolve(setp.get("%s.pos-scale" % name), e) or 1.0)
                    except ValueError:
                        scale = 1.0
                    out[j] = (name, scale)
    return out


# -- HAL lines from quirk files

_PLACE = re.compile(r"\{(name|pin|signal)(?::([0-9A-Fa-fx]+:[0-9A-Fa-fx]+))?\}")


def _slave_objects(slave):
    """{(index, subindex): (pin, HAL direction)} of a slave's pins."""
    out = {}
    for kind in ("outputs", "inputs"):
        for pdo in (slave.get("process-data") or {}).get(kind) or []:
            for e in pdo.get("entries") or []:
                if "pin" in e:
                    try:
                        out[config.parse_obj(e.get("object"))] = (
                            "%s.%s" % (slave["name"], e["pin"]), "in" if kind == "outputs" else "out")
                    except ValueError:
                        pass
    return out


def quirk_hal(cfg, pin_sig, user_loaded, p):
    """The HAL lines quirk files give the configuration's slaves:
    ({component: [instances]}, [functions], [lines]). pin_sig: pin ->
    signal, as the files will connect them. Notes and refusals go to the
    plan p."""
    from . import quirks
    loads, addf, lines = {}, [], []
    for s in (cfg or {}).get("slaves") or []:
        q = quirks.of(s)
        hal = q.get("hal")
        if not hal:
            continue
        name, objs = s.get("name"), _slave_objects(s)
        made, why = [], None

        def fill(text):
            def one(m):
                nonlocal why
                kind, o = m.group(1), m.group(2)
                if kind == "name":
                    return name
                obj = config.parse_obj(o)
                if obj not in objs:
                    why = why or "%s has no pin" % o
                    return m.group(0)
                pin, way = objs[obj]
                if kind == "pin":
                    if pin in pin_sig:
                        why = why or "%s is connected in your HAL files" % pin
                    return pin
                if pin in pin_sig:
                    return pin_sig[pin]
                if way == "in":
                    why = why or "nothing drives %s" % pin
                    return m.group(0)
                sig = default_signal(pin)
                if sig not in {x for x in pin_sig.values()}:
                    made.append("net %s %s" % (sig, pin))
                pin_sig[pin] = sig
                return sig
            return _PLACE.sub(one, text)

        text = [fill(x) for x in hal["lines"]]
        funcs = [fill(x) for x in hal["addf"]]
        insts = {c: [fill(x) for x in v] for c, v in hal["load"].items()}
        clash = sorted(set(insts) & user_loaded)
        if clash:
            why = why or "your HAL files load %s, which HAL loads only once" % ", ".join(clash)
        if why:
            p.notes.append("%s: the HAL lines of the quirk file %s are left out: %s"
                           % (name, os.path.basename(q["path"]), why))
            continue
        for c, v in insts.items():
            loads.setdefault(c, []).extend(v)
        addf += funcs
        lines += ["# %s: from the quirk file %s" % (name, os.path.basename(q["path"]))] + \
            made + text
        p.notes.append("%s: HAL lines from the quirk file %s" % (name, os.path.basename(q["path"])))
    return loads, addf, lines


# -- the plan

@dataclass
class FileChange:
    path: str
    kind: str           # "new" or "change"
    old: str
    new: str
    what: str = ""      # a short description

    @property
    def name(self):
        return os.path.basename(self.path)


@dataclass
class Plan:
    changes: list = field(default_factory=list)     # [FileChange]
    commented: list = field(default_factory=list)   # [(file name, line number, text)]
    errors: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    edited: list = field(default_factory=list)      # ecatsetup's files changed by hand


def _managed(cfg):
    """[(joint, letter, drive name, scale, [(joint pin, drive pin, arrow, suffix)])]."""
    out = []
    ds = drives(cfg)
    for j in joints(cfg):
        name = j.get("slave")
        conns = list(CONNECTIONS)
        if "hm" in (((ds.get(name) or {}).get("cia402") or {}).get("modes") or []):
            conns.append(HOMING)
        out.append((j["joint"], name, j.get("scale", 1.0),
                    [("joint.%d.%s" % (j["joint"], a), "%s.%s" % (name, b), arrow, suffix)
                     for a, b, arrow, suffix in conns]))
    return out


# LinuxCNC's own pins, there before any HAL file's components
CORE_PREFIXES = {"motion", "iocontrol", "joint", "axis", "spindle"}


def _arrow_writers(t):
    """The pins a net statement writes its signal with, by its arrows."""
    rest = t[2:]
    for i, x in enumerate(rest):
        if x == "=>":
            return rest[:i]
        if x == "<=":
            return [y for y in rest[i + 1:] if y not in ARROWS]
    return []


def _loaded_names(t):
    from . import wiring
    return wiring._loaded(t)


def _is_ecat_line(t):
    """A statement that loads lcec or ecat, or adds their functions."""
    if t[0] == "loadusr" and any("lcec_conf" in x for x in t[1:]):
        return True
    if t[0] == "loadrt" and len(t) > 1 and t[1] in ("lcec", "lcec_uspace", "ecat"):
        return True
    if t[0] == "addf" and len(t) > 1 and t[1].split(".")[0] in ("lcec", "ecat"):
        return True
    return False


def plan(ini_path, cfg, yaml_name="ethercat.yaml", max_counts=None):
    """What writing the configuration into the INI file's configuration
    would change. Nothing is written. max_counts: see joint_checks()."""
    p = Plan()
    d = os.path.dirname(os.path.abspath(ini_path))
    if not os.path.exists(ini_path):
        p.errors.append("%s does not exist" % ini_path)
        return p
    if ini_path.endswith(EXPANDED):
        src = expanded_source(ini_path)
        p.errors.append("%s is the file LinuxCNC writes from %s at every start, with its "
                        "#INCLUDEs in place: what is written here is lost. Choose %s instead"
                        % (os.path.basename(ini_path),
                           os.path.basename(src) if src else "the INI file",
                           os.path.basename(src) if src else "that INI file"))
        return p
    ini_e = read_ini(ini_path)
    ds = drives(cfg)
    njoints = len(ini_joints(ini_path))
    seen = set()
    for j in joints(cfg):
        n = j.get("joint")
        if not isinstance(n, int) or not 0 <= n < njoints:
            p.errors.append("joint %r: the INI file has %d joints" % (n, njoints))
        if j.get("slave") not in ds:
            p.errors.append("joint %s: %r is not a CSP drive in the configuration"
                            % (n, j.get("slave")))
        if j.get("slave") in seen:
            p.errors.append("%s is connected to two joints" % j.get("slave"))
        seen.add(j.get("slave"))
    managed = _managed(cfg)
    managed_pins = {pin for _, _, _, conns in managed for c in conns for pin in c[:2]}
    io = [n for n in nets(cfg) if isinstance(n, dict) and len(n.get("pins") or []) >= 2]
    managed_pins |= {pin for n in io for pin in n["pins"]}
    postgui = set(user_hal_files(ini_path, postgui=True))
    scale_pins = {"%s.pos-scale" % name for _, name, _, _ in managed}

    # LIB:basic_sim.tcl builds the simulation when LinuxCNC starts; the file it
    # saved then is static HAL, and takes its place
    ini_replace = {}
    files = []
    for value, path, _ in hal_entries(ini_path):
        if os.path.basename(path) == BASIC_SIM:
            saved = basic_sim_file(ini_path)
            if saved is None:
                p.errors.append("%s builds the simulation when LinuxCNC starts, and saves it as "
                                "%s_cmds.hal: start this configuration once in the simulator, "
                                "then write again" % (value, os.path.splitext(
                                    os.path.basename(ini_path))[0]))
                continue
            ini_replace[value] = os.path.basename(saved)
            p.notes.append("%s is replaced by %s, the HAL it built when the configuration ran in "
                           "the simulator" % (value, os.path.basename(saved)))
            files.append(saved)
        else:
            files.append(path)
    for path in files:
        if not path.endswith(".hal") and not is_tcl(path):
            p.notes.append("%s is not a HAL file; it is not checked for lcec or ecat lines"
                           % os.path.basename(path))
    hal_files = [f for f in files if f.endswith(".hal") and os.path.exists(f)]
    tcl_files = [f for f in files if is_tcl(f) and os.path.exists(f)]
    for f in files:
        if (f.endswith(".hal") or is_tcl(f)) and not os.path.exists(f):
            p.errors.append("%s is in the INI file but does not exist" % os.path.basename(f))

    # a .tcl file is read as the HAL it gives, but never changed: a line
    # that would be commented out in a .hal file is an error there
    for path in tcl_files:
        from . import haltcl
        try:
            x = haltcl.expand(path, ini_path)
        except OSError as e:
            p.notes.append("%s could not be read as HAL (%s); it is not checked" % (
                os.path.basename(path), e))
            continue
        p.notes += x.problems
        clash = []          # (Tcl line, what)
        for a, _, t in statements(x.text):
            pins = []
            if _is_ecat_line(t):
                pins = [" ".join(t)]
            elif t[0] == "setp" and len(t) == 3 and t[1] in scale_pins:
                pins = [t[1]]
            elif t[0] == "net" and len(t) > 1:
                pins = [pin for pin in net_pins(t)[1] if pin in managed_pins]
            if pins:
                clash.append((x.lines[a] if a < len(x.lines) else 0, pins))
        if clash:
            lines = sorted({n for n, _ in clash})
            what = sorted({pin for _, ps in clash for pin in ps})
            p.errors.append("%s (line%s %s) connects %s, which ethercat.hal connects now. EtherCAT "
                            "Setup does not change .tcl files: take those lines out there, or the "
                            "file out of the INI file" % (
                                os.path.basename(path), "" if len(lines) == 1 else "s",
                                ", ".join(str(n) for n in lines), ", ".join(what)))

    # what the user's files connect, before any change
    old_sig = signals(hal_files + tcl_files, ini_path)
    # lines to comment out, and the signals they used
    sig_of_pin = {}          # managed pin -> the signal the user's file put it on
    kept = {}                # signal -> pins of commented lines that are not managed
    line_writers = set()     # pins that wrote their signal in a commented line (arrows)
    # what exists before the GUI: what the HALFILEs load, LinuxCNC's own pins
    early = set()
    for path in hal_files + tcl_files:
        if path in postgui:
            continue
        try:
            text = read_hal(path, ini_path)
        except OSError:
            continue
        for _, _, t in statements(original(text)):
            early.update(inst for inst, _ in _loaded_names(t))

    def exists_early(pin):
        return (is_own(cfg, pin) or pin.split(".", 1)[0] in CORE_PREFIXES
                or any(pin.startswith(inst + ".") for inst in early))

    for path in hal_files:
        current = read_text(path)
        text = original(current)
        lines = text.splitlines(keepends=True)
        hit = set()
        mine = {}            # this file's kept pins
        after = {}           # last line of a statement -> the line that replaces it
        dropped = []         # writers of a POSTGUI line that a real pin replaces
        for a, b, t in statements(text):
            why = None
            if _is_ecat_line(t):
                why = True
            elif t[0] == "setp" and len(t) == 3 and t[1] in scale_pins:
                why = True
            elif len(t) == 3 and t[1] == "=" and t[0] in scale_pins:
                why = True
            elif t[0] == "net" and len(t) > 1:
                name, pins = net_pins(t)
                if managed_pins & set(pins):
                    why = True
                    wrote = _arrow_writers(t)
                    line_writers.update(wrote)
                    for pin in pins:
                        if pin in managed_pins:
                            sig_of_pin.setdefault(pin, name)
                        elif path not in postgui:
                            mine.setdefault(name, []).append(pin)
                    if path in postgui:
                        # the GUI's own readers stay on the signal, after the GUI
                        # is up; a writer gives way to the real pin
                        rest = [x for x in pins if x not in managed_pins and x not in wrote]
                        dropped += [x for x in wrote if x not in managed_pins]
                        if rest:
                            # the signal ethercat.hal connects the pin to
                            ours = [x for x in pins if x in managed_pins]
                            sig = sig_of_pin.get(ours[0], name) if ours else name
                            after[b] = "net %s => %s%s\n" % (sig, " ".join(rest), ADDED)
            if why:
                hit.update(range(a, b + 1))
        if hit and path in postgui:
            moved = sorted({pin for a, b, t in statements(text) if a in hit and t[0] == "net"
                            for pin in net_pins(t)[1] if pin in managed_pins})
            late = [x for x in moved if not exists_early(x)]
            if late:
                p.errors.append("%s (a POSTGUI file, read after ethercat.hal) connects %s: "
                                "connect the signal there, not the pin, or connect it by hand"
                                % (os.path.basename(path), ", ".join(late)))
                continue
            if dropped:
                p.notes.append("%s: %s no longer connected (a real input or output takes "
                               "its place)" % (os.path.basename(path), ", ".join(dropped)))
        for name, pins in mine.items():
            kept.setdefault(name, []).extend(pins)
        copy_to = None
        if hit and os.path.dirname(os.path.abspath(path)) != d and text == current:
            # LinuxCNC's own file (hallib) is never changed: a copy in the folder
            # takes its place (HALLIB_PATH finds the folder's first)
            copy_to = os.path.join(d, os.path.basename(path))
            if os.path.exists(copy_to):
                p.errors.append("%s is in the folder and in %s: rename one" % (
                    os.path.basename(path), os.path.dirname(path)))
                continue
            for value, epath, _ in hal_entries(ini_path):
                if epath == path and value.startswith("LIB:"):
                    ini_replace[value] = value[4:]
        new = []
        for i, line in enumerate(lines):
            if i in hit:
                p.commented.append((os.path.basename(path), i + 1, line.rstrip("\n")))
                new.append(COMMENT + line)
                if i in after:
                    new.append(after[i])
            else:
                new.append(line)
        new = "".join(new)
        if copy_to is not None:
            p.changes.append(FileChange(copy_to, "new", "", new, "copied from LinuxCNC's %s, "
                                        "%d line%s commented out" % (
                                            os.path.dirname(path), len(hit),
                                            "" if len(hit) == 1 else "s")))
            p.notes.append("%s is copied into the configuration folder and changed there; "
                           "LinuxCNC's own file is not" % os.path.basename(path))
            continue
        if new != current:
            before = {i for i, x in enumerate(current.splitlines()) if x.startswith(COMMENT)}
            back = len(before - hit)
            parts = []
            if hit:
                parts.append("%d line%s commented out" % (len(hit), "" if len(hit) == 1 else "s"))
            if back:
                parts.append("%d back as you wrote %s" % (back, "it" if back == 1 else "them"))
            p.changes.append(FileChange(path, "change", current, new, ", ".join(parts)))

    # signal names: the user's where there was one
    letters = {j: letter for j, letter, _ in ini_joints(ini_path)}
    count = {}
    for letter in letters.values():
        count[letter] = count.get(letter, 0) + 1
    used = set(old_sig)
    driven = set()           # signals ethercat.hal gives a writer
    body = ["addf ecat.read-all  servo-thread 1", "addf ecat.write-all servo-thread", ""]
    for n, name, scale, conns in managed:
        letter = letters.get(n, "")
        base = letter.lower() if letter and count.get(letter) == 1 else \
            ("%s%d" % (letter.lower(), n) if letter else "j%d" % n)
        body.append("# joint %d%s: %s" % (n, " (%s)" % letter if letter else "", name))
        body.append("setp %s.pos-scale %r" % (name, float(scale)))
        for jpin, dpin, arrow, suffix in conns:
            a, b = sig_of_pin.get(jpin), sig_of_pin.get(dpin)
            cmd_sig = sig_of_pin.get("joint.%d.motor-pos-cmd" % n)
            if suffix == "pos-fb" and a is not None and a == cmd_sig:
                # a simulation looped the command back as feedback (core_sim.hal):
                # the feedback is the drive's now, on a signal of its own
                p.notes.append("joint %d: the simulation looped motor-pos-cmd back to "
                               "motor-pos-fb (signal %s); the feedback comes from %s now"
                               % (n, a, name))
                a = None
            if a and b and a != b:
                p.errors.append("%s and %s are on different signals in your HAL files (%s, %s): "
                                "something sits between the joint and the drive; connect them "
                                "by hand" % (jpin, dpin, a, b))
            sig = a or b
            if sig is None:
                sig = "%s-%s" % (base, suffix)
                if sig in used:
                    sig = "ecat-" + sig
            used.add(sig)
            driven.add(sig)
            if arrow == "<=":
                body.append("net %s %s => %s" % (sig, dpin, jpin))
            elif arrow == "<=>":
                body.append("net %s %s <=> %s" % (sig, jpin, dpin))
            else:
                body.append("net %s %s => %s" % (sig, jpin, dpin))
        body.append("")
    if io:
        body.append("# inputs and outputs")
    own = slave_pins(cfg)
    for n in io:
        pins = list(dict.fromkeys(n["pins"]))
        theirs = [sig_of_pin[x] for x in pins if x in sig_of_pin]
        sig = n.get("signal") or default_signal(pins[0])
        if theirs:
            if len(set(theirs)) > 1:
                p.errors.append("%s are on different signals in your HAL files (%s): connect "
                                "them by hand" % (", ".join(x for x in pins if x in sig_of_pin),
                                                  ", ".join(sorted(set(theirs)))))
            if theirs[0] != sig:
                p.notes.append("signal %s is called %s in your HAL files; that name is kept"
                               % (sig, theirs[0]))
            sig = theirs[0]
        elif sig in used:
            p.errors.append("signal %s is used in your HAL files already: choose another name "
                            "or connect it there" % sig)
        used.add(sig)

        def way(x):
            return (own.get(x) or known_pin(x) or (None, None))[1]
        writers = [x for x in pins if way(x) == "out"]
        if writers:
            driven.add(sig)
        if len(writers) == 1:
            rest = [x for x in pins if x != writers[0]]
            body.append("net %s %s => %s" % (sig, writers[0], " ".join(rest)))
        else:
            body.append("net %s %s" % (sig, " ".join(pins)))
    if io:
        body.append("")
    # a signal ethercat.hal drives now loses the writers of the lines commented
    # out: a simulated home switch, the simulation's E-stop loop-back
    for s_name in list(kept):
        if s_name in driven:
            gone = [x for x in kept[s_name] if (known_pin(x) or (None, None))[1] == "out"
                    or _WRITER.search(x) or x in line_writers]
            if gone:
                kept[s_name] = [x for x in kept[s_name] if x not in gone]
                p.notes.append("signal %s: %s no longer writes it (a real input does now)"
                               % (s_name, ", ".join(dict.fromkeys(gone))))
                # a simulated switch shared by joints (X and Z on one or2) is
                # not the other joint's real switch: each is connected on its own
                shared = [x for x in kept[s_name] if _SWITCH.match(x)]
                if shared:
                    kept[s_name] = [x for x in kept[s_name] if x not in shared]
                    p.notes.append("signal %s: %s left unconnected; the simulation shared the "
                                   "switch, a machine has one each: connect its own input"
                                   % (s_name, ", ".join(shared)))
    extra = [(s, pins) for s, pins in kept.items() if pins]
    if extra:
        body.append("# other pins of lines commented out in your HAL files")
        for s, pins in extra:
            body.append("net %s %s" % (s, " ".join(dict.fromkeys(pins))))
        body.append("")

    # HAL lines from quirk files, with the signals as they are now
    pin_sig = {}
    for sig_name, sig_pins in old_sig.items():
        for pin in sig_pins:
            pin_sig.setdefault(pin, sig_name)
    for line in body:
        t = line.split("#", 1)[0].split()
        if t and t[0] == "net" and len(t) > 2:
            for pin in net_pins(t)[1]:
                pin_sig[pin] = t[1]
    user_loaded = set()
    for path in hal_files:
        for _, _, t in statements(original(read_text(path))):
            if t[0] == "loadrt" and len(t) > 1:
                user_loaded.add(t[1])
    q_loads, q_addf, q_lines = quirk_hal(cfg, pin_sig, user_loaded, p)
    if q_addf:
        # after ecat.read-all, before ecat.write-all
        body[1:1] = ["addf %s servo-thread" % f for f in q_addf]
    if q_lines:
        body += q_lines + [""]
    if q_loads:
        body[0:0] = ["loadrt %s names=%s" % (comp, ",".join(names))
                     for comp, names in sorted(q_loads.items())]

    load = HEADER + "\n# Loads the component before the other HAL files, so that the slaves'\n" \
        "# pins exist for them.\n"
    if check_at_start(cfg):
        load += "\n# linuxcnc.check-hal-at-start: the HAL files against the configuration, before\n" \
            "# anything is loaded (ecatsetup --check-hal reads INI_FILE_NAME)\n" \
            "loadusr -w ecatsetup --check-hal\n"
    load += "loadrt ecat config=[ETHERCAT]CONFIG\n"
    main = HEADER + "\n# The last HALFILE: the servo thread exists now. ecat.read-all goes first\n" \
        "# in the thread, ecat.write-all last.\n" + "\n".join(body).rstrip() + "\n"
    from . import halcheck
    for fname, text, what in ((LOAD_FILE, load, "loads ecat"),
                              (MAIN_FILE, main, "functions and joints")):
        text = halcheck.with_checksum(text)
        path = os.path.join(d, fname)
        old = read_text(path) if os.path.exists(path) else ""
        if old and halcheck.edited(old):
            p.edited.append(fname)
        if old != text:
            p.changes.append(FileChange(path, "change" if old else "new", old, text, what))
    for path, new_ini in ini_texts(ini_path, yaml_name, ini_replace).items():
        old_ini = read_text(path)
        if new_ini != old_ini:
            p.changes.append(FileChange(path, "change", old_ini, new_ini,
                                        "[ETHERCAT]CONFIG and the HAL files" if path == ini_path
                                        else "[ETHERCAT]CONFIG and the HAL files (#INCLUDEd by %s)"
                                        % os.path.basename(ini_path)))
    p.notes += joint_checks(ini_path, cfg, max_counts)
    for path in hal_files:
        what = SIM_FILES.get(os.path.basename(path))
        if what:
            p.notes.append("%s still simulates %s: connect the real ones (Joints, Inputs and "
                           "outputs), then take it out of the INI file" % (
                               os.path.basename(path), what))
    if not managed and not io:
        p.notes.append("no joint or pin is connected here: ethercat.hal only adds the "
                       "component's functions; your HAL files keep their connections")
    for j, _, _ in ini_joints(ini_path) if managed else []:
        if j not in {m[0] for m in managed}:
            p.notes.append("joint %d is not connected here (it is connected in your HAL files, "
                           "or not on EtherCAT)" % j)
    for v in ini_all(ini_e, "HAL", "POSTGUI_HALFILE"):
        path = find_hal_file(d, v.split()[0]) if v.split() else ""
        try:
            late = [t[1] for _, _, t in statements(original(read_text(path)))
                    if t[0] == "addf" and len(t) > 2 and t[2] == "servo-thread"]
        except OSError:
            late = []
        if late:
            p.notes.append("%s adds %s to the servo thread after ecat.write-all: their outputs "
                           "reach the slaves a cycle later" % (os.path.basename(path),
                                                               ", ".join(late)))
    return p


def apply(p, overwrite_edited=False):
    """Write a plan's files; each existing file is kept as .bak first.
    Returns the paths written. A file of ecatsetup's own that was changed
    by hand is replaced only with overwrite_edited."""
    if p.errors:
        raise ValueError("not written:\n  " + "\n  ".join(p.errors))
    if p.edited and not overwrite_edited:
        raise ValueError("not written: %s changed by hand since EtherCAT Setup wrote it; move "
                         "the change into a HAL file of your own, or write anyway"
                         % ", ".join(p.edited))
    written = []
    for c in p.changes:
        if os.path.exists(c.path):
            shutil.copyfile(c.path, c.path + ".bak")
        tmp = c.path + ".tmp"
        with open(tmp, "w") as f:
            f.write(c.new)
        shutil.copymode(c.path, tmp) if os.path.exists(c.path) else None
        os.replace(tmp, c.path)
        written.append(c.path)
    return written


def describe(p, out=print):
    """A plan as text: the files, then each change as a diff."""
    import difflib
    for c in p.changes:
        out("%s %s (%s)" % ("new   " if c.kind == "new" else "change", c.path, c.what))
    for c in p.changes:
        out("")
        if c.kind == "new":
            out("--- %s (new)" % c.name)
            out(c.new.rstrip())
        else:
            out("".join(difflib.unified_diff(c.old.splitlines(keepends=True),
                                             c.new.splitlines(keepends=True),
                                             c.name + ".bak", c.name, n=1)).rstrip())
    for n in p.notes:
        out("note: %s" % n)
    for f in p.edited:
        out("warning: %s was changed by hand since EtherCAT Setup wrote it; writing replaces "
            "the change (the diff above shows it as removed)" % f)
    for e in p.errors:
        out("error: %s" % e)
