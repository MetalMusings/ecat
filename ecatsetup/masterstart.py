"""Starting the free-running userspace master with sudo, after the user has
confirmed. Everything about that lives here, so that a polkit helper can
replace it later. Nothing here runs by itself: callers show `text(plan)`,
ask, and only then call `run(plan, ...)`. The window never runs as root.

The command is `sudo [-n] ethercat-uspace start [-- -i NIC -t TRANSPORT]`.
NIC and transport come from the file's `master:` section when it names
them, otherwise from ethercat-uspace.conf (the tool reads it). If the
kernel master is loaded (/dev/EtherCAT0) the plan also has
`sudo systemctl stop ethercat`, visibly: it may be in use on purpose.
"""

import os
import shlex
import shutil
import subprocess

from . import config, tool

SYS_NET = "/sys/class/net"
KERNEL_MASTER_DEVICE = "/dev/EtherCAT0"
# processes that mean LinuxCNC owns the bus: no master is to be started
LINUXCNC_PROCESSES = {"linuxcncsvr", "milltask", "linuxcnc"}


class Plan:
    """The commands to run, in order, and what the user should know first."""

    def __init__(self, commands, notes):
        self.commands = commands    # list of argv lists, without "sudo"
        self.notes = notes          # list of text lines


def linuxcnc_running(proc="/proc"):
    """True if a LinuxCNC process runs (it takes the NIC itself)."""
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return False
    for pid in pids:
        try:
            with open(os.path.join(proc, pid, "comm")) as f:
                if f.read().strip() in LINUXCNC_PROCESSES:
                    return True
        except OSError:
            continue
    return False


def plan(cfg=None, sys_net=SYS_NET, kernel_device=KERNEL_MASTER_DEVICE):
    """What to run to get a master. `cfg` is the loaded file (or None).
    Interface and transport are passed only when the file gives them and
    they are valid (an existing NIC, a known transport); nothing else is
    ever passed on."""
    master = (cfg or {}).get("master") or {}
    start = [os.path.abspath(shutil.which(tool.TOOL) or tool.TOOL), "start"]
    notes, extra = [], []
    nic, transport = master.get("interface"), master.get("transport")
    if nic and os.path.isdir(os.path.join(sys_net, str(nic))) and "/" not in str(nic):
        extra += ["-i", str(nic)]
        if transport in config.TRANSPORTS:
            extra += ["-t", transport]
    elif nic:
        notes.append("The file names the interface %s, which does not exist here; "
                     "the master's own settings are used." % nic)
    if extra:
        start += ["--"] + extra
    commands = [start]
    if os.path.exists(kernel_device):
        commands.insert(0, ["systemctl", "stop", "ethercat"])
        notes.append("The kernel master is loaded (%s). It is stopped first: it shares "
                     "the network card, and may be in use on purpose." % kernel_device)
    return Plan(commands, notes)


def argv(command, noninteractive):
    return ["sudo"] + (["-n"] if noninteractive else []) + command


def text(p):
    """The commands as the user would type them, one per line."""
    return "\n".join(" ".join(shlex.quote(a) for a in argv(c, False)) for c in p.commands)


def run(p, noninteractive=True, runner=subprocess.run):
    """Run the plan. With `noninteractive` (the window) sudo never asks for a
    password: if one is needed the answer says so. Without it (a terminal)
    sudo asks by itself. Returns (ok, message)."""
    for command in p.commands:
        try:
            if noninteractive:
                proc = runner(argv(command, True), capture_output=True, text=True)
            else:
                proc = runner(argv(command, False), stderr=subprocess.PIPE, text=True)
        except OSError as e:
            return False, "Could not run sudo: %s" % e
        if proc.returncode != 0:
            err = (proc.stderr or "").strip()
            if "password is required" in err or "a terminal is required" in err:
                return False, ("sudo needs a password here. Run this in a terminal:\n"
                               + text(Plan([command], [])))
            return False, "%s: %s" % (" ".join(command[-3:]), err or "failed (%d)" % proc.returncode)
    return True, "The master is running."


def confirm_terminal(p, input_fn=input, out=print):
    """Ask on a terminal; True on yes. Only call this when stdin is a tty."""
    out("No master is running. To start one:")
    for line in p.notes:
        out("  " + line)
    for line in text(p).splitlines():
        out("  " + line)
    try:
        return input_fn("Run it now? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def offer_terminal(cfg=None, stdin=None, stderr=None, input_fn=input, out=print, runner=subprocess.run):
    """For the command line: when no master answers, offer to start one.
    Returns True if one was started. Nothing is offered without a terminal
    (a script must not hang), nor while LinuxCNC runs (it has the bus)."""
    import sys
    stdin, stderr = stdin or sys.stdin, stderr or sys.stderr
    if not (stdin.isatty() and stderr.isatty()) or linuxcnc_running():
        return False
    p = plan(cfg)
    if not confirm_terminal(p, input_fn, out):
        return False
    ok, msg = run(p, noninteractive=False, runner=runner)
    out(msg)
    return ok
