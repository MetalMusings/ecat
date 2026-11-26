"""The free-running master's messages, from the system journal.

While the `ecat` component is the master (the workbench, LinuxCNC), the
master's messages are in the component's flight recorder (events.py). The
free-running master that `ethercat-uspace start` starts (`ec_master`, for
reading the bus, SDOs, the SII) logs to syslog instead, that is the system
journal. This module follows that journal while such a master runs, so that
the window can show its messages beside the recorder's.

ec_master runs as root, so its entries are in the system journal. A user
reads that in the groups systemd-journal or adm; otherwise `sudo -n
journalctl` is used if sudo needs no password (the window starts the master
with `sudo -n` the same way); otherwise the messages cannot be shown and
`MasterLog.note` says why and what to do. One journalctl process follows one
master for as long as it runs; nothing is polled with sudo.
"""

import grp
import json
import os
import subprocess
import threading

from .events import Text

PROCESS = "ec_master"
JOURNAL_GROUPS = ("systemd-journal", "adm", "wheel")
SOURCE = "free master"
KEEP = 2000     # messages kept per window session


def free_master_pid(proc="/proc"):
    """The pid of a running free master (ec_master), or None."""
    try:
        pids = sorted((p for p in os.listdir(proc) if p.isdigit()), key=int)
    except OSError:
        return None
    for pid in pids:
        try:
            with open(os.path.join(proc, pid, "comm")) as f:
                if f.read().strip() == PROCESS:
                    return int(pid)
        except OSError:
            continue
    return None


def _group_names():
    names = set()
    for gid in os.getgroups():
        try:
            names.add(grp.getgrgid(gid).gr_name)
        except KeyError:
            continue
    return names


def _sudo_works():
    try:
        return subprocess.run(["sudo", "-n", "true"], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def journal_access(euid=None, groups=None, sudo_works=None):
    """How the system journal can be read: (command prefix, None), or
    (None, why not) when it cannot."""
    if (os.geteuid() if euid is None else euid) == 0:
        return [], None
    if set(JOURNAL_GROUPS) & (_group_names() if groups is None else set(groups)):
        return [], None
    if (_sudo_works() if sudo_works is None else sudo_works()):
        return ["sudo", "-n"], None
    return None, ("the free master's messages are in the system journal, which this user "
                  "cannot read: add the user to the group systemd-journal "
                  "(sudo usermod -aG systemd-journal $USER, then log in again)")


def journal_command(pid, prefix=()):
    """The command that prints the master's messages (up to level info) since
    it started, and then each new one, as JSON lines."""
    return list(prefix) + ["journalctl", "--quiet", "--output=json", "--follow",
                           "--lines=all", "--priority=info", "_PID=%d" % pid]


def parse(line, index):
    """A journal JSON line as a Text, or None."""
    try:
        entry = json.loads(line)
        msg = entry["MESSAGE"]
        if isinstance(msg, list):       # journald gives non-UTF-8 text as bytes
            msg = bytes(msg).decode(errors="replace")
        level = int(entry.get("PRIORITY", 6))
        when = int(entry["__REALTIME_TIMESTAMP"]) / 1e6
    except (ValueError, KeyError, TypeError):
        return None
    return Text(index, when, level, msg.rstrip("\n"), SOURCE)


class Follower:
    """One journalctl process following one master, read in a thread."""

    def __init__(self, pid, prefix, popen=subprocess.Popen, first_index=0):
        self.pid = pid
        self.texts = []
        self.lock = threading.Lock()
        self.next_index = first_index
        self.proc = popen(journal_command(pid, prefix), stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        for line in self.proc.stdout:
            with self.lock:
                t = parse(line, self.next_index)
                if t is not None:
                    self.next_index += 1
                    self.texts.append(t)
                    del self.texts[:-KEEP]

    def snapshot(self):
        with self.lock:
            return list(self.texts)

    def stop(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                self.proc.kill()
            except OSError:
                pass
        self.thread.join(timeout=5)


class MasterLog:
    """The free master's messages of this window session: follows the
    journal of each free master that runs while the window is open; the
    messages of one that stopped are kept."""

    def __init__(self, find=None, access=None, popen=None):
        # looked up when called, so that tests can replace the module's functions
        self.find = find or (lambda: free_master_pid())
        self.access = access or (lambda: journal_access())
        self.popen = popen or (lambda *a, **k: subprocess.Popen(*a, **k))
        self.pid = None             # the free master followed now, or None
        self.follower = None
        self.kept = []              # messages of masters that stopped
        self.note = ""              # why the messages cannot be shown, or ""
        self.prefix = None          # how the journal is read (None: it cannot be)
        self.decided = False        # prefix and note are decided once per window

    def poll(self):
        """Follow the free master that runs now (call it every few seconds)."""
        pid = self.find()
        if pid == self.pid:
            return
        self._stop()
        self.pid = pid
        if pid is None:
            return
        if not self.decided:
            self.prefix, why = self.access()
            self.note = why or ""
            self.decided = True
        if self.prefix is None:
            return
        try:
            self.follower = Follower(pid, self.prefix, self.popen,
                                     first_index=self.kept[-1].index + 1 if self.kept else 0)
        except OSError as e:
            self.note = "the system journal cannot be read: %s" % e

    def _stop(self):
        if self.follower is not None:
            self.follower.stop()
            self.kept = (self.kept + self.follower.snapshot())[-KEEP:]
            self.follower = None

    def texts(self):
        """[Text], oldest first."""
        return self.kept + (self.follower.snapshot() if self.follower is not None else [])

    def status(self):
        """One line on the free master, or "" when none ran."""
        if self.pid is None:
            return ""
        if self.follower is None:
            return "the free master (ec_master) runs; " + self.note if self.note else ""
        return "the free master (ec_master, pid %d) runs; its messages come from the system " \
               "journal" % self.pid

    def close(self):
        self._stop()
        self.pid = None
