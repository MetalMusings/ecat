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

def read_ini(path):
    """[(section, key, value)] in file order, as LinuxCNC reads them."""
    out, section = [], None
    with open(path, errors="replace") as f:
        for line in f:
            s = line.strip()
            if s.startswith("[") and "]" in s:
                section = s[1:s.index("]")].strip()
            elif section and "=" in s and not s.startswith(("#", ";")):
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


def ini_text(ini_path, yaml_name):
    """The INI file's text with [ETHERCAT]CONFIG and the two HALFILEs."""
    with open(ini_path, errors="replace") as f:
        lines = f.read().splitlines(keepends=True)
    nl = "\n"

    def section_of(i):
        s = None
        for line in lines[:i + 1]:
            t = line.strip()
            if t.startswith("[") and "]" in t:
                s = t[1:t.index("]")].strip()
        return s

    def key_of(line):
        t = line.strip()
        if "=" not in t or t.startswith(("#", ";", "[")):
            return None
        return t.split("=", 1)[0].strip()

    # [HAL]: ours out, then in again where they belong
    out = []
    for i, line in enumerate(lines):
        if section_of(i) == "HAL" and key_of(line) == "HALFILE" and \
                line.split("=", 1)[1].split("#")[0].strip() in (LOAD_FILE, MAIN_FILE):
            continue
        out.append(line)
    lines = out
    hal = [i for i, line in enumerate(lines) if section_of(i) == "HAL"]
    if not hal:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += nl
        lines += [nl, "[HAL]" + nl]
        hal = [len(lines) - 1]
    halfiles = [i for i in hal if key_of(lines[i]) == "HALFILE"]
    first = halfiles[0] if halfiles else hal[0] + 1
    last = (halfiles[-1] + 1) if halfiles else hal[0] + 1
    lines.insert(last, "HALFILE = %s%s" % (MAIN_FILE, nl))
    lines.insert(first, "HALFILE = %s%s" % (LOAD_FILE, nl))

    # [ETHERCAT]CONFIG
    eth = [i for i in range(len(lines)) if section_of(i) == "ETHERCAT"]
    cfg_line = "CONFIG = %s%s" % (yaml_name, nl)
    keys = [i for i in eth if key_of(lines[i]) == "CONFIG"]
    if keys:
        lines[keys[0]] = cfg_line
    elif eth:
        lines.insert(eth[0] + 1, cfg_line)
    else:
        # before [HAL], where a reader looks for it
        at = next(i for i in range(len(lines)) if lines[i].strip().startswith("[HAL]"))
        lines[at:at] = ["[ETHERCAT]" + nl, cfg_line, nl]
    return "".join(lines)


# -- the user's HAL files

def read_text(path):
    with open(path, errors="replace") as f:
        return f.read()


def original(text):
    """A HAL file's text as the user wrote it: lines commented out by an
    earlier plan are back, so that every plan starts from the same text."""
    return "".join(line[len(COMMENT):] if line.startswith(COMMENT) else line
                   for line in text.splitlines(keepends=True))

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


def signals(paths):
    """signal -> set of pins, over HAL files."""
    sig = {}
    for path in paths:
        try:
            text = original(read_text(path))
        except OSError:
            continue
        for _, _, t in statements(text):
            if t[0] == "net" and len(t) > 1:
                name, pins = net_pins(t)
                sig.setdefault(name, set()).update(pins)
    return sig


def setp_values(paths):
    """pin or parameter -> the value it is set to, over HAL files."""
    out = {}
    for path in paths:
        try:
            text = original(read_text(path))
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
    """{name: slave} of the drives a joint can be connected to."""
    out = {}
    for s in (cfg or {}).get("slaves") or []:
        if s.get("profile") == "cia402" and (s.get("cia402") or {}).get("mode") == "csp":
            out[s.get("name")] = s
    return out


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
    for j in joints(cfg):
        if j.get("slave") == old:
            j["slave"] = new
    for n in nets(cfg):
        n["pins"] = [new + p[len(old):] if p.startswith(old + ".") else p
                     for p in n.get("pins") or []]


# -- inputs and outputs: nets between the slaves' pins and LinuxCNC's

# LinuxCNC pins a slave's pin is often connected to: (pattern, HAL type,
# direction seen from HAL: "in" is read by LinuxCNC, "out" written by it)
KNOWN_PINS = [
    (r"iocontrol\.0\.emc-enable-in", "bit", "in"),
    (r"iocontrol\.0\.user-enable-out", "bit", "out"),
    (r"iocontrol\.0\.user-request-enable", "bit", "out"),
    (r"iocontrol\.0\.(coolant-flood|coolant-mist|lube)", "bit", "out"),
    (r"iocontrol\.0\.lube_level", "bit", "in"),
    (r"motion\.digital-in-\d\d", "bit", "in"),
    (r"motion\.digital-out-\d\d", "bit", "out"),
    (r"motion\.analog-in-\d\d", "float", "in"),
    (r"motion\.analog-out-\d\d", "float", "out"),
    (r"motion\.probe-input", "bit", "in"),
    (r"motion\.feed-hold", "bit", "in"),
    (r"motion\.enable", "bit", "in"),
    (r"joint\.\d+\.(home-sw-in|pos-lim-sw-in|neg-lim-sw-in)", "bit", "in"),
    (r"spindle\.\d+\.(on|forward|reverse|brake)", "bit", "out"),
    (r"spindle\.\d+\.(speed-out|speed-out-abs|speed-out-rps|speed-out-rps-abs)",
     "float", "out"),
    (r"spindle\.\d+\.(at-speed|is-oriented|orient-fault-in)", "bit", "in"),
    (r"spindle\.\d+\.(speed-in)", "float", "in"),
    (r"halui\.(machine|estop|program|mist|flood|lube)\.(on|off|activate|reset|run|pause|"
     r"resume|step|stop)", "bit", "in"),
    (r"halui\.machine\.is-on", "bit", "out"),
    (r"halui\.estop\.is-activated", "bit", "out"),
    (r"halui\.program\.is-(running|paused|idle)", "bit", "out"),
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
                            out["%s.%s" % (name, p)] = ("bit", way)
    return out


def own_prefixes(cfg):
    """The pin prefixes of the configuration's slaves: "<name>."."""
    return [s.get("name") + "." for s in (cfg or {}).get("slaves") or [] if s.get("name")]


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
    sig = signals([f for f in files if os.path.dirname(os.path.abspath(f)) == d])
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
    sig = signals(files)
    setp = setp_values(files)
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


def _is_ecat_line(t):
    """A statement that loads lcec or ecat, or adds their functions."""
    if t[0] == "loadusr" and any("lcec_conf" in x for x in t[1:]):
        return True
    if t[0] == "loadrt" and len(t) > 1 and t[1] in ("lcec", "lcec_uspace", "ecat"):
        return True
    if t[0] == "addf" and len(t) > 1 and t[1].split(".")[0] in ("lcec", "ecat"):
        return True
    return False


def plan(ini_path, cfg, yaml_name="ethercat.yaml"):
    """What writing the configuration into the INI file's configuration
    would change. Nothing is written."""
    p = Plan()
    d = os.path.dirname(os.path.abspath(ini_path))
    if not os.path.exists(ini_path):
        p.errors.append("%s does not exist" % ini_path)
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

    files = user_hal_files(ini_path)
    for path in files:
        if not path.endswith(".hal"):
            p.notes.append("%s is not a HAL file; it is not checked for lcec or ecat lines"
                           % os.path.basename(path))
    hal_files = [f for f in files if f.endswith(".hal") and os.path.exists(f)]
    for f in files:
        if f.endswith(".hal") and not os.path.exists(f):
            p.errors.append("%s is in the INI file but does not exist" % os.path.basename(f))

    # what the user's files connect, before any change
    old_sig = signals(hal_files)
    # lines to comment out, and the signals they used
    sig_of_pin = {}          # managed pin -> the signal the user's file put it on
    kept = {}                # signal -> pins of commented lines that are not managed
    for path in hal_files:
        current = read_text(path)
        text = original(current)
        lines = text.splitlines(keepends=True)
        hit = set()
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
                    for pin in pins:
                        if pin in managed_pins:
                            sig_of_pin.setdefault(pin, name)
                        else:
                            kept.setdefault(name, []).append(pin)
            if why:
                hit.update(range(a, b + 1))
        if hit and path in postgui:
            moved = sorted({pin for a, b, t in statements(text) if a in hit and t[0] == "net"
                            for pin in net_pins(t)[1] if pin in managed_pins})
            if moved:
                p.errors.append("%s (a POSTGUI file, read after ethercat.hal) connects %s: "
                                "connect the signal there, not the pin, or connect it by hand"
                                % (os.path.basename(path), ", ".join(moved)))
                continue
        if hit and os.path.dirname(os.path.abspath(path)) != d and text == current:
            p.errors.append("%s, outside the configuration folder, loads or connects what "
                            "ethercat.hal would; it is not changed here: copy it into the "
                            "configuration folder" % path)
            continue
        new = []
        for i, line in enumerate(lines):
            if i in hit:
                p.commented.append((os.path.basename(path), i + 1, line.rstrip("\n")))
                new.append(COMMENT + line)
            else:
                new.append(line)
        new = "".join(new)
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
    body = ["addf ecat.read-all  servo-thread 1", "addf ecat.write-all servo-thread", ""]
    for n, name, scale, conns in managed:
        letter = letters.get(n, "")
        base = letter.lower() if letter and count.get(letter) == 1 else \
            ("%s%d" % (letter.lower(), n) if letter else "j%d" % n)
        body.append("# joint %d%s: %s" % (n, " (%s)" % letter if letter else "", name))
        body.append("setp %s.pos-scale %r" % (name, float(scale)))
        for jpin, dpin, arrow, suffix in conns:
            a, b = sig_of_pin.get(jpin), sig_of_pin.get(dpin)
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
        if len(writers) == 1:
            rest = [x for x in pins if x != writers[0]]
            body.append("net %s %s => %s" % (sig, writers[0], " ".join(rest)))
        else:
            body.append("net %s %s" % (sig, " ".join(pins)))
    if io:
        body.append("")
    extra = [(s, pins) for s, pins in kept.items() if pins]
    if extra:
        body.append("# other pins of lines commented out in your HAL files")
        for s, pins in extra:
            body.append("net %s %s" % (s, " ".join(dict.fromkeys(pins))))
        body.append("")

    load = HEADER + "\n# Loads the component before the other HAL files, so that the slaves'\n" \
        "# pins exist for them.\nloadrt ecat config=[ETHERCAT]CONFIG\n"
    main = HEADER + "\n# The last HALFILE: the servo thread exists now. ecat.read-all goes first\n" \
        "# in the thread, ecat.write-all last.\n" + "\n".join(body).rstrip() + "\n"
    for fname, text, what in ((LOAD_FILE, load, "loads ecat"),
                              (MAIN_FILE, main, "functions and joints")):
        path = os.path.join(d, fname)
        old = read_text(path) if os.path.exists(path) else ""
        if old != text:
            p.changes.append(FileChange(path, "change" if old else "new", old, text, what))
    old_ini = read_text(ini_path)
    new_ini = ini_text(ini_path, yaml_name)
    if new_ini != old_ini:
        p.changes.append(FileChange(ini_path, "change", old_ini, new_ini,
                                    "[ETHERCAT]CONFIG and the HAL files"))
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


def apply(p):
    """Write a plan's files; each existing file is kept as .bak first.
    Returns the paths written."""
    if p.errors:
        raise ValueError("not written:\n  " + "\n  ".join(p.errors))
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
    for e in p.errors:
        out("error: %s" % e)
