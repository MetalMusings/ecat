"""Save report: one text file to attach when asking for help (a forum
post), with what is needed to understand an EtherCAT problem in
LinuxCNC: versions, the configuration, the relevant INI lines and HAL
file, a diagnosis of the bus if a master can be reached, and the flight
recorder's events of this run and the one before.

It reads only. Nothing is sent anywhere; the user decides where the file
goes.
"""

import os
import platform
import subprocess
import time

from . import config, diagnose, events, integrate, netif, quirks, tool

PACKAGES = ["linuxcnc-ecat", "linuxcnc-ethercat-uspace", "linuxcnc-uspace", "linuxcnc"]
INI_KEYS = {
    "EMCMOT": ["SERVO_PERIOD", "BASE_PERIOD"],
    "ETHERCAT": None,
    "HAL": ["HALFILE", "POSTGUI_HALFILE", "SHUTDOWN", "HALUI"],
    "KINS": None,
    "TRAJ": ["COORDINATES", "LINEAR_UNITS", "MAX_LINEAR_VELOCITY"],
}
JOINT_KEYS = ["TYPE", "MAX_VELOCITY", "MAX_ACCELERATION", "FERROR", "MIN_FERROR", "SCALE",
              "HOME_SEQUENCE", "HOME_USE_INDEX", "HOME_OFFSET"]


def _run(*cmd):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return p.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def versions():
    out = []
    for pkg in PACKAGES:
        v = _run("dpkg-query", "-W", "-f", "${Version}", pkg)
        if v:
            out.append("%s %s" % (pkg, v))
    out.append("kernel %s%s" % (platform.release(),
                                " (PREEMPT_RT)" if "PREEMPT_RT" in platform.version() else ""))
    try:
        with open("/etc/os-release") as f:
            name = next((x.split("=", 1)[1].strip().strip('"') for x in f
                         if x.startswith("PRETTY_NAME=")), "")
        out.append("system %s" % name)
    except OSError:
        pass
    out.append("ecatsetup %s" % os.path.dirname(os.path.abspath(__file__)))
    return out


def ini_lines(ini_path):
    """The INI lines that matter for EtherCAT, in file order."""
    out = []
    for s, k, v in integrate.read_ini(ini_path):
        keys = INI_KEYS.get(s, [])
        if (s in INI_KEYS and (keys is None or k in keys)) or (
                s.startswith("JOINT_") and k in JOINT_KEYS):
            out.append("[%s]%s = %s" % (s, k, v))
    return out


def _section(out, title):
    out.append("")
    out.append("## " + title)
    out.append("")


def _file(out, path):
    try:
        with open(path, errors="replace") as f:
            text = f.read()
    except OSError as e:
        out.append("(cannot read %s: %s)" % (path, e.strerror))
        return
    out.append("```")
    out.append(text.rstrip())
    out.append("```")


def build(cfg_path=None, ini_path=None, bus=True, seconds=2.0):
    """The report as text. cfg_path: ethercat.yaml; ini_path: the machine's
    INI file; bus: diagnose the slaves if a master can be reached."""
    out = ["# EtherCAT report, %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
           "",
           "Written by EtherCAT Setup (ecatsetup --report). It holds the configuration, "
           "the INI lines about EtherCAT and the joints, a diagnosis of the bus and the "
           "ecat component's event log. Look it through before posting it."]

    _section(out, "Versions")
    out += ["- " + v for v in versions()]

    cfg = None
    if cfg_path is None and ini_path:
        e = integrate.read_ini(ini_path)
        name = integrate.ini_get(e, "ETHERCAT", "CONFIG")
        if name:
            cfg_path = os.path.join(os.path.dirname(os.path.abspath(ini_path)), name)
    _section(out, "Configuration %s" % (cfg_path or "(none given)"))
    if cfg_path:
        _file(out, cfg_path)
        try:
            cfg = config.load(cfg_path)
            errs = config.check(cfg)
            out.append("")
            out.append("Check: " + ("no errors" if not errs else "%d error(s)" % len(errs)))
            out += ["- " + x for x in errs]
            iface = (cfg.get("master") or {}).get("interface")
            if iface:
                out.append("Interface %s: driver %s, xdp-native %s" % (
                    iface, netif.driver(iface) or "?", "yes" if netif.xdp_native(iface) else "no"))
        except Exception as x:     # a report must not stop at a bad file
            out.append("(cannot read it as a configuration: %s)" % x)
    quirks.load()
    if quirks.problems:
        out.append("Quirk files not read: " + "; ".join(quirks.problems))

    if ini_path:
        _section(out, "INI %s" % ini_path)
        try:
            out.append("```")
            out += ini_lines(ini_path)
            out.append("```")
        except OSError as e:
            out.append("(cannot read it: %s)" % e.strerror)
        d = os.path.dirname(os.path.abspath(ini_path))
        for name in (integrate.LOAD_FILE, integrate.MAIN_FILE):
            path = os.path.join(d, name)
            if os.path.exists(path):
                _section(out, name)
                _file(out, path)

    _section(out, "Bus")
    if bus:
        try:
            positions = tool.slave_positions()
            out.append("```")
            found = diagnose.diagnose(positions, seconds, out=out.append)
            out.append("```")
            out.append("%d problem(s) found" % found if found else "No problems found")
        except tool.ToolError as e:
            out.append("No master could be reached (%s): LinuxCNC with ecat, or "
                       "`sudo ethercat-uspace start`, must run for this part." % str(e).strip())
    else:
        out.append("(not diagnosed)")

    names = events.names_of(cfg)
    for title, path in (("Event log, this run (or the last)", events.PATH),
                        ("Event log, the run before", events.PATH + ".prev")):
        _section(out, title)
        try:
            log = events.read(path)
            out.append("```")
            events.report(log, names, out=out.append)
            out.append("```")
        except events.EventsError as e:
            out.append("(%s)" % e)
    return "\n".join(out) + "\n"


def default_name():
    return "ethercat-report-%s.md" % time.strftime("%Y%m%d-%H%M%S")
