"""The HAL check: the configuration's HAL files against ethercat.yaml.

Users edit their HAL files by hand, and those drift from the
configuration: a slave renamed, a PDO entry removed, a pin connected in
two places. This reads every HAL file of the INI file (with
ethercat-load.hal and ethercat.hal, as LinuxCNC would) and finds what HAL
would refuse, or what does not do what it seems to, before LinuxCNC
starts. It reads only.
"""

import difflib
import hashlib
import os
from dataclasses import dataclass

from . import halpins, integrate

CHECKSUM = "# ecatsetup-checksum: "


@dataclass
class Finding:
    severity: str       # "error" (HAL would refuse, or it cannot work) or "warn"
    file: str           # file name
    line: int           # 1-based, 0 if none
    text: str

    def __str__(self):
        where = "%s:%d" % (self.file, self.line) if self.line else self.file
        return "%s: %s: %s" % ("error" if self.severity == "error" else "warning", where, self.text)


@dataclass
class Use:
    file: str
    line: int
    signal: str = None  # None: setp


def checksum(text):
    """The checksum ecatsetup writes into its HAL files' headers: over the
    text without the checksum line."""
    body = "".join(x for x in text.splitlines(keepends=True) if not x.startswith(CHECKSUM))
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def with_checksum(text):
    """A HAL file's text with its checksum line after the first comment block."""
    lines = text.splitlines(keepends=True)
    i = 0
    while i < len(lines) and lines[i].startswith("#"):
        i += 1
    lines.insert(i, "%s%s\n" % (CHECKSUM, checksum(text)))
    return "".join(lines)


def edited(text):
    """True if a file ecatsetup wrote has been changed since; None if it has
    no checksum (not written by this version)."""
    line = next((x for x in text.splitlines() if x.startswith(CHECKSUM)), None)
    if line is None:
        return None
    return line[len(CHECKSUM):].strip() != checksum(text)


def hal_files(ini_path):
    """[(path, postgui)] in the order LinuxCNC reads them."""
    d = os.path.dirname(os.path.abspath(ini_path))
    e = integrate.read_ini(ini_path)
    out = []
    for key, post in (("HALFILE", False), ("POSTGUI_HALFILE", True)):
        for v in integrate.ini_all(e, "HAL", key):
            name = v.split()[0] if v.split() else ""
            if name:
                out.append((integrate.find_hal_file(d, name), post))
    return out


def uses(ini_path):
    """{pin: [Use]} over all HAL files, and {signal: [pins]}."""
    pins, sigs = {}, {}
    for path, _ in hal_files(ini_path):
        if not path.endswith(".hal"):
            continue
        try:
            text = integrate.read_text(path)
        except OSError:
            continue
        name = os.path.basename(path)
        for a, _, t in integrate.statements(text):
            if t[0] == "net" and len(t) > 1:
                sig, ps = integrate.net_pins(t)
                sigs.setdefault(sig, [])
                for p in ps:
                    pins.setdefault(p, []).append(Use(name, a + 1, sig))
                    sigs[sig].append(p)
            elif t[0] in ("linkps", "linksp") and len(t) >= 3:
                p, sig = (t[1], t[2]) if t[0] == "linkps" else (t[2], t[1])
                pins.setdefault(p, []).append(Use(name, a + 1, sig))
                sigs.setdefault(sig, []).append(p)
            elif t[0] == "setp" and len(t) >= 3:
                pins.setdefault(t[1], []).append(Use(name, a + 1))
            elif len(t) == 3 and t[1] == "=" and "." in t[0]:
                pins.setdefault(t[0], []).append(Use(name, a + 1))
    return pins, sigs


def check(ini_path, cfg):
    """[Finding] for the INI file's HAL files against the configuration."""
    out = []
    if not os.path.exists(ini_path):
        return [Finding("error", os.path.basename(ini_path), 0, "does not exist")]
    have = halpins.pins(cfg)
    prefixes = tuple(integrate.own_prefixes(cfg)) + ("ecat.",)
    # the first part of the slaves' names too ("lcec." for lcec.0.X): a pin of
    # a slave renamed or gone still looks like one of ours
    roots = tuple(sorted({x.split(".", 1)[0] + "." for x in prefixes if "." in x[:-1]}))
    prefixes += roots
    pins, sigs = uses(ini_path)

    for path, _ in hal_files(ini_path):
        if path.endswith(".hal") and not os.path.exists(path):
            out.append(Finding("error", os.path.basename(path), 0,
                               "is in the INI file but does not exist"))

    # ecatsetup's own files changed by hand
    d = os.path.dirname(os.path.abspath(ini_path))
    for name in (integrate.LOAD_FILE, integrate.MAIN_FILE):
        path = os.path.join(d, name)
        if os.path.exists(path) and edited(integrate.read_text(path)):
            out.append(Finding("warn", name, 0, "was changed by hand since EtherCAT Setup wrote "
                               "it; writing again replaces it: move the change into a HAL file "
                               "of your own"))

    def known(p):
        return have.get(p) or integrate.known_pin(p)

    for pin, us in sorted(pins.items()):
        first = us[0]
        if pin.startswith(prefixes) and pin not in have:
            close = difflib.get_close_matches(pin, list(have), n=1, cutoff=0.75)
            if not close:
                # renamed: the same pin under another slave name
                tail = pin.rsplit(".", 1)[-1]
                same = sorted(p for p in have if p.rsplit(".", 1)[-1] == tail)
                close = same[:1] if len(same) == 1 else []
            out.append(Finding("error", first.file, first.line,
                               "%s is not a pin of the configuration%s" % (
                                   pin, "; did you mean %s?" % close[0] if close else
                                   " (renamed or removed?)")))
            continue
        on = sorted({u.signal for u in us if u.signal})
        if len(on) > 1:
            u = next(u for u in us if u.signal == on[1])
            out.append(Finding("error", u.file, u.line, "%s is on two signals, %s; HAL refuses "
                               "the second" % (pin, " and ".join(on))))
        info = have.get(pin)
        for u in us:
            if u.signal is None and on:
                out.append(Finding("error", u.file, u.line, "setp of %s, which is on signal %s; "
                                   "HAL refuses to set a connected pin" % (pin, on[0])))
            if u.signal is None and info and info[1] == "out":
                out.append(Finding("error", u.file, u.line,
                                   "setp of %s, an output of the slave" % pin))

    for sig, ps in sorted(sigs.items()):
        ps = list(dict.fromkeys(ps))
        writers = [p for p in ps if (known(p) or (None, None))[1] == "out"]
        if len(writers) > 1:
            u = pins[writers[1]][0]
            out.append(Finding("error", u.file, u.line, "signal %s has two writers, %s"
                               % (sig, " and ".join(writers))))
        ours_in = [p for p in ps if p in have and have[p][1] == "in"]
        # a signal with only the slave's pins may be driven at run time (a GUI's
        # Python code, such as QtPlasmaC's): only one with other pins, all known
        if ours_in and not writers and len(ps) > len(ours_in) and all(known(p) for p in ps):
            u = pins[ours_in[0]][0]
            out.append(Finding("warn", u.file, u.line, "nothing writes signal %s, so %s stays "
                               "at its default" % (sig, ", ".join(ours_in))))
    return out


def where(ini_path, pin):
    """Where a pin is used: [Use]."""
    return uses(ini_path)[0].get(pin, [])
