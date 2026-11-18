"""Save report: one text file to attach when asking for help (a forum
post), with what is needed to understand an EtherCAT problem in
LinuxCNC: versions, the configuration, the relevant INI lines and HAL
file, a diagnosis of the bus if a master can be reached, and the flight
recorder's events of this run and the one before. Also what decides
whether frames get through: other EtherCAT masters on the computer (the
IgH kernel master beside the userspace one shares the card), the
transport and the master's frame counters, and the computer as the
real-time check reads it.

Personal details the problem does not need are left out: disk UUIDs on
the kernel command line, MAC addresses (a card is named by its interface
name instead) and the home folder's name (written ~).

It reads only. Nothing is sent anywhere; the user decides where the file
goes.
"""

import os
import platform
import re
import subprocess
import time

from . import config, diagnose, events, integrate, masterstart, netif, quirks, tool

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


# kernel parameters naming a disk, a partition or an encrypted volume
PRIVATE_PARAM = re.compile(r"\b(root|resume|cryptdevice|cryptroot|rd\.luks\.uuid|rd\.luks\.name|"
                           r"rd\.lvm\.lv|rd\.md\.uuid|boot|systemd\.gpt_auto)=\S+")
MAC = re.compile(r"\b[0-9a-f]{2}(?::[0-9a-f]{2}){5}\b", re.I)
SYS_NET = "/sys/class/net"
KERNEL_MASTER_DEVICE = masterstart.KERNEL_MASTER_DEVICE
FREE_MASTER_PROCESSES = {"ec_master", "ethercat-uspace"}


def mac_names(sys_net=SYS_NET):
    """MAC address (lower case) -> interface name."""
    out = {}
    for name in sorted(os.listdir(sys_net)) if os.path.isdir(sys_net) else []:
        try:
            with open(os.path.join(sys_net, name, "address")) as f:
                out.setdefault(f.read().strip().lower(), name)
        except OSError:
            continue
    return out


def redact(text, sys_net=SYS_NET, home=None):
    """The text without disk UUIDs on a kernel command line, with each MAC
    address replaced by its interface's name (or "(MAC)") and the home
    folder written as ~ (it holds the user's name)."""
    text = PRIVATE_PARAM.sub(lambda m: m.group(1) + "=(left out)", text)
    home = home or os.path.expanduser("~")
    if home and home != "/":
        text = re.sub(re.escape(home.rstrip("/")) + r"(?=/|\b)", "~", text)
    names = mac_names(sys_net)
    return MAC.sub(lambda m: "(MAC of %s)" % names[m.group(0).lower()]
                   if m.group(0).lower() in names else "(MAC)", text)


def ecat_build(here=None):
    """Where ecatsetup runs from and which build it is: a run-in-place archive
    says so in BUILD-INFO beside the package, a checkout by git, a package
    by dpkg (in versions)."""
    pkg = here or os.path.dirname(os.path.abspath(__file__))
    top = os.path.dirname(pkg)
    try:
        with open(os.path.join(top, "BUILD-INFO")) as f:
            first = f.readline().strip()
        if first:
            return "%s (run-in-place archive: %s)" % (pkg, first)
    except OSError:
        pass
    if os.path.exists(os.path.join(top, ".git")):
        rev = _run("git", "-C", top, "describe", "--tags", "--always", "--dirty")
        if rev:
            return "%s (checkout %s)" % (pkg, rev)
    return pkg


def _processes(proc="/proc"):
    """(pid, comm, command line) of every process."""
    out = []
    try:
        pids = sorted((p for p in os.listdir(proc) if p.isdigit()), key=int)
    except OSError:
        return out
    for pid in pids:
        try:
            with open(os.path.join(proc, pid, "comm")) as f:
                comm = f.read().strip()
            with open(os.path.join(proc, pid, "cmdline"), "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            continue
        out.append((pid, comm, cmd))
    return out


def master_facts(proc="/proc", kernel_device=KERNEL_MASTER_DEVICE, run=None):
    """The EtherCAT masters of this computer as data: the IgH kernel master
    (device, ec_* modules, ethercat.service), free userspace masters,
    LinuxCNC and rtapi_app. `kernel_master` is True when the kernel master
    is loaded: it must not share a network card with the userspace master."""
    run = run or _run
    mods = []
    try:
        with open(os.path.join(proc, "modules")) as f:
            mods = sorted(x.split()[0] for x in f if x.startswith("ec_"))
    except OSError:
        pass
    procs = _processes(proc)
    f = {"device": os.path.exists(kernel_device), "modules": mods,
         "enabled": run("systemctl", "is-enabled", "ethercat.service"),
         "active": run("systemctl", "is-active", "ethercat.service"),
         "free": [(p, c) for p, comm, c in procs if comm in FREE_MASTER_PROCESSES],
         "rtapi": any(comm == "rtapi_app" for _, comm, _ in procs),
         "linuxcnc": sorted({comm for _, comm, _ in procs if comm in masterstart.LINUXCNC_PROCESSES})}
    f["kernel_master"] = bool(f["device"] or mods or f["active"] == "active")
    return f


def masters(proc="/proc", kernel_device=KERNEL_MASTER_DEVICE, run=None, facts=None):
    """Lines on the EtherCAT masters of this computer (master_facts)."""
    f = facts or master_facts(proc, kernel_device, run)
    out = ["IgH kernel master: %s; modules %s; ethercat.service %s" % (
        "loaded (%s)" % kernel_device if f["device"] else "not loaded",
        ", ".join(f["modules"]) if f["modules"] else "none",
        "%s, %s" % (f["enabled"], f["active"]) if f["enabled"] else "not installed")]
    for pid, cmd in f["free"]:
        out.append("Free userspace master: pid %s: %s" % (pid, cmd))
    if not f["free"]:
        out.append("Free userspace master: none running")
    out.append("LinuxCNC: %s; rtapi_app (HAL, with ecat's master when loaded): %s" % (
        "running (%s)" % ", ".join(f["linuxcnc"]) if f["linuxcnc"] else "not running",
        "running" if f["rtapi"] else "not running"))
    if f["kernel_master"]:
        out.append("WARNING: the IgH kernel master is loaded. It must not use the network card "
                   "of the userspace master: `sudo systemctl stop ethercat` (and `sudo systemctl "
                   "disable ethercat` so that it does not come back at the next start).")
    elif f["enabled"] == "enabled":
        out.append("Note: ethercat.service is enabled: the kernel master starts at the next boot.")
    return out


def master_status():
    """The running master's own status (`ethercat-uspace master`): link,
    frames sent and received, lost frames. None when no master answers."""
    try:
        return tool.run("master").strip()
    except (tool.ToolError, OSError):
        return None


def computer(ini_path=None):
    """The computer as the real-time check reads it (rtcheck.Inventory)."""
    from . import rtcheck
    return rtcheck.inventory(ini=ini_path)


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
    out.append("ecatsetup %s" % ecat_build())
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
           "the INI lines about EtherCAT and the joints, the EtherCAT masters on this "
           "computer, the computer as the real-time check reads it, a diagnosis of the bus "
           "and the ecat component's event log. Disk UUIDs, MAC addresses and the home "
           "folder's name are left out. Look it through before posting it."]

    _section(out, "Versions")
    out += ["- " + v for v in versions()]
    explain_at = len(out)       # what the event log means goes here, at the end

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
            master = cfg.get("master") or {}
            iface = master.get("interface")
            if iface:
                out.append("Interface %s: driver %s, xdp-native %s; transport %s" % (
                    iface, netif.driver(iface) or "?", "yes" if netif.xdp_native(iface) else "no",
                    master.get("transport") or "raw (the default)"))
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

    facts = master_facts()
    _section(out, "EtherCAT masters")
    out += ["- " + x for x in masters(facts=facts)]
    if bus:
        status = master_status()
        if status:
            out.append("")
            out.append("The running master (`ethercat-uspace master`):")
            out.append("```")
            out.append(status)
            out.append("```")

    _section(out, "Computer")
    out.append("As the real-time check (Timing tab) reads it; the source of each line in [].")
    inv = None
    try:
        from . import rtcheck
        inv = computer(ini_path)
        out.append("```")
        out += rtcheck.inventory_text(inv).splitlines()
        out.append("```")
    except Exception as x:     # a report must not stop at an odd computer
        out.append("(cannot read it: %s)" % x)

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
    this_run = None
    for title, path in (("Event log, this run (or the last)", events.PATH),
                        ("Event log, the run before", events.PATH + ".prev")):
        _section(out, title)
        try:
            log = events.read(path)
            this_run = this_run or (log if path == events.PATH else None)
            out.append("```")
            events.report(log, names, out=out.append)
            out.append("```")
        except events.EventsError as e:
            out.append("(%s)" % e)

    # first in the report: what this run's event log means on this computer
    if this_run is not None:
        try:
            from . import explain
            found = explain.findings(explain.context(
                this_run, names, masters=facts, inv=inv,
                interface=((cfg or {}).get("master") or {}).get("interface")))
        except Exception as x:     # a report must not stop at a bad symptom file
            found, x_text = [], str(x)
        else:
            x_text = None
        if found or x_text:
            part = []
            _section(part, "What the event log means")
            part.append("From this run's event log, judged against this computer.")
            part.append("")
            part += explain.text_lines(found) if found else ["(cannot judge it: %s)" % x_text]
            out[explain_at:explain_at] = part
    return redact("\n".join(out) + "\n")


def default_name():
    return "ethercat-report-%s.md" % time.strftime("%Y%m%d-%H%M%S")
