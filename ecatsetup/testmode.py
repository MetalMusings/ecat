"""Test mode: run the ecat component in halrun with a servo thread, without
LinuxCNC, so that inputs can be read and outputs set from EtherCAT Setup.

The configuration being edited is written to a folder of its own and
loaded from there, so unsaved changes can be tried. Pins are read and set
with halcmd. (A component of LinuxCNC's Python hal module in the setup
program would be killed by halrun's "unload all" when the test stops.)

The pins of CiA 402 drives and the master's pins are read-only in the
I/O test's pin tables. A drive is enabled only through drivetest.py, and
its safety does not depend on this program: every cia402 drive's
watchdog-ms is set in the HAL file, and the drive is shut down in the
servo thread if its watchdog pin, which this program counts up
(tick_watchdogs()), stops changing.

A CSP drive's pos-cmd comes from a limit3, so that it moves only at the
test's velocity and acceleration and within its travel. While the drive
is not in "Operation enabled", the limit3 is loaded with pos-fb, so that
enabling it never makes a jump; once enabled, it follows the target that
drivetest.py sets (the mux2's in1).

Before the test stops, every drive is disabled, pins set during the test
go back to 0, and the slaves are taken to PREOP (ecat.take-down).

The free-running master (ethercat-uspace start) hands the bus over to the
component by itself; it does not come back when the test stops.
"""

import os
import subprocess
import tempfile
import time

from . import config

HALRUN = os.environ.get("ECAT_HALRUN", "halrun")
HALCMD = os.environ.get("ECAT_HALCMD", "halcmd")
KERNEL_MASTER = "/dev/EtherCAT0"

# how long a drive keeps running without a new heartbeat
WATCHDOG_MS = 500


def jog_names(i):
    """The HAL instances that make CSP drive i's pos-cmd."""
    return "ecatjog%d" % i, "ecatsel%d" % i, "ecatnot%d" % i


def csp_drives(cfg):
    """Names of the cia402 slaves in CSP, which the drive test can move."""
    return [s["name"] for s in cfg.get("slaves") or []
            if s.get("profile") == "cia402" and (s.get("cia402") or {}).get("mode") == "csp"]


def hal_file(cfg, cfg_path, period):
    """The test's HAL file."""
    lines = ["# Written by EtherCAT Setup for its test mode.",
             "loadrt ecat config=%s" % cfg_path,
             "loadrt threads name1=servo-thread period1=%d" % period]
    drives = csp_drives(cfg)
    if drives:
        jogs, sels, nots = zip(*(jog_names(i) for i in range(len(drives))))
        lines += ["loadrt limit3 names=%s" % ",".join(jogs),
                  "loadrt mux2 names=%s" % ",".join(sels),
                  "loadrt not names=%s" % ",".join(nots)]
    lines.append("addf ecat.read-all servo-thread")
    for i, name in enumerate(drives):
        jog, sel, not_ = jog_names(i)
        lines += ["addf %s servo-thread" % f for f in (sel, not_, jog)]
        lines += [
            "net %s-fb %s.pos-fb => %s.in0" % (jog, name, sel),
            "net %s-enabled %s.stat-op-enabled => %s.sel %s.in" % (jog, name, sel, not_),
            "net %s-load %s.out => %s.load" % (jog, not_, jog),
            "net %s-target %s.out => %s.in" % (jog, sel, jog),
            "net %s-cmd %s.out => %s.pos-cmd" % (jog, jog, name),
            # until the drive test sets it: no motion at all
            "setp %s.maxv 0" % jog]
    for s in cfg.get("slaves") or []:
        if s.get("profile") == "cia402":
            lines.append("setp %s.watchdog-ms %d" % (s["name"], WATCHDOG_MS))
    lines += ["addf ecat.write-all servo-thread", "start", ""]
    return "\n".join(lines)


class TestError(Exception):
    pass


class Pin:
    __slots__ = ("name", "type", "dir", "value")

    def __init__(self, name, type_, dir_, value):
        self.name, self.type, self.dir, self.value = name, type_, dir_, value

    def __repr__(self):
        return "Pin(%s %s %s %s)" % (self.name, self.type, self.dir, self.value)


# Objects that are commands, not settings, which the test mode does not
# write: (vendor, product) -> {index: why}.
SDO_COMMANDS = {
    (0x00000A88, 0x0A880002): {0x2010: "Zero Position on the ECT60: a command, not a setting"},
}
# a data type of the configuration file -> the ethercat tool's
TOOL_TYPES = {"BOOL": "bool", "SINT": "int8", "INT": "int16", "DINT": "int32", "LINT": "int64",
              "USINT": "uint8", "UINT": "uint16", "UDINT": "uint32", "ULINT": "uint64",
              "REAL": "float", "LREAL": "double"}


def sdo_write_refused(slave, index, subindex):
    """Why test mode does not write this object to the slave, or None."""
    ident = slave.get("identity") or {}
    try:
        kind = (int(str(ident.get("vendor")), 16), int(str(ident.get("product")), 16))
    except ValueError:
        kind = None
    why = SDO_COMMANDS.get(kind, {}).get(index)
    if why:
        return why
    for direction in ("outputs", "inputs"):
        for pdo in (slave.get("process-data") or {}).get(direction) or []:
            for e in pdo.get("entries") or []:
                try:
                    if config.parse_obj(e.get("object")) == (index, subindex):
                        return "0x%04X:%02X is process data: set its pin instead" % (index, subindex)
                except ValueError:
                    pass
    if slave.get("profile") == "cia402" and 0x6040 <= index <= 0x67FF:
        return "CiA 402 objects of a drive are written only in the CiA 402 test mode"
    return None


def halcmd(*args, check=True):
    proc = subprocess.run([HALCMD] + [str(a) for a in args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise TestError((proc.stderr or proc.stdout).strip() or "halcmd %s failed" % args[0])
    return proc.stdout


def parse_pins(text):
    """name -> Pin, from "halcmd -s show pin"."""
    pins = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 5:
            pins[f[4]] = Pin(f[4], f[1], f[2], f[3])
    return pins


def hal_running():
    """True if a HAL session (LinuxCNC or halrun) runs already."""
    try:
        text = halcmd("-s", "show", "comp", check=False)
    except OSError:
        return False
    # halcmd lists itself, as halcmd<pid>, even when nothing else runs
    return any(len(f) >= 3 and not f[2].startswith("halcmd")
               for f in (line.split() for line in text.splitlines()))


def read_only_prefixes(cfg):
    """Pin prefixes that test mode does not set: CiA 402 drives (moved only
    in the CiA 402 test mode) and the master."""
    return tuple(["ecat."] + ["%s." % s["name"] for s in cfg.get("slaves") or []
                              if s.get("profile") == "cia402"])


class TestRun:
    def __init__(self, cfg, period=None):
        errors = config.check(cfg)
        if errors:
            raise TestError("the configuration has errors:\n  " + "\n  ".join(errors))
        self.cfg = cfg
        self.period = period or 1000000
        self.read_only = read_only_prefixes(cfg)
        self.drives = csp_drives(cfg)
        self.cia402 = [s["name"] for s in cfg.get("slaves") or [] if s.get("profile") == "cia402"]
        self.heartbeat = 0
        self.proc = None
        self.dir = None
        self.log_path = None
        self.written = {}       # pin -> type, of pins set during the test

    # -- start and stop

    def start(self, progress=lambda text: None, timeout=40.0):
        if os.path.exists(KERNEL_MASTER):
            raise TestError("The kernel EtherCAT master is loaded and may use the network card.\n"
                            "Stop it first: sudo systemctl stop ethercat")
        if hal_running():
            raise TestError("HAL runs already (LinuxCNC or halrun): stop it first")
        self.dir = tempfile.mkdtemp(prefix="ecatsetup-test-")
        cfg_path = os.path.join(self.dir, "ethercat.yaml")
        config.write(self.cfg, cfg_path)
        hal_path = os.path.join(self.dir, "test.hal")
        with open(hal_path, "w") as f:
            f.write(hal_file(self.cfg, cfg_path, self.period))
        self.log_path = os.path.join(self.dir, "halrun.log")
        progress("Starting ecat in halrun (log: %s)" % self.log_path)
        self.log = open(self.log_path, "w")
        self.proc = subprocess.Popen([HALRUN, "-I", "-f", hal_path], cwd=self.dir,
                                     stdin=subprocess.PIPE, stdout=self.log,
                                     stderr=subprocess.STDOUT, text=True)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                self._closed()
                raise TestError("halrun stopped:\n" + self.log_tail())
            pins = self._try_pins("ecat.0.")
            if pins.get("ecat.0.all-op") and pins["ecat.0.all-op"].value == "TRUE":
                progress("All slaves in OP")
                return
            time.sleep(0.25)
        progress("Not all slaves reached OP in %g s; the test runs anyway" % timeout)

    def _try_pins(self, prefix=""):
        try:
            return self.pins(prefix)
        except (TestError, OSError):
            return {}

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self, timeout=20.0):
        """Put set pins back to 0, stop the thread, unload, end halrun."""
        if self.proc is None:
            return
        if self.running:
            self.disable_drives()
            for name, type_ in list(self.written.items()):
                try:
                    halcmd("setp", name, "0" if type_ != "bit" else "FALSE")
                except (TestError, OSError):
                    pass
            # let a few cycles carry the zeros out before the thread stops
            time.sleep(max(0.05, 5 * self.period / 1e9))
            self.take_down()
            try:
                # the thread stops before the master is released (lcec
                # uses the master's memory from the thread)
                self.proc.stdin.write("stop\nexit\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                self.proc.wait(5)
        self._closed()

    def take_down(self, timeout=2.0):
        """Take the slaves to PREOP while process data still flows, as
        ecat-take-down does at a LinuxCNC shutdown: otherwise they lose it
        in OP when the thread stops, and stop with an error. Returns True
        once no slave is above PREOP."""
        try:
            halcmd("setp", "ecat.take-down", "TRUE")
        except (TestError, OSError):
            return False
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            pins = self._try_pins("ecat.state-")
            if pins and all(pins.get("ecat.state-" + s) and pins["ecat.state-" + s].value == "FALSE"
                            for s in ("safeop", "op")):
                return True
            time.sleep(0.02)
        return False

    def disable_drives(self):
        """Drop every drive's enable."""
        try:
            self.setp_many({"%s.enable" % n: "FALSE" for n in self.cia402})
        except (TestError, OSError):
            pass

    def tick_watchdogs(self):
        """Count up the drives' watchdog pins: the heartbeat. Without it a
        drive is shut down within WATCHDOG_MS."""
        if not self.cia402 or not self.running:
            return
        self.heartbeat = (self.heartbeat + 1) & 0xFFFFFFFF
        self.setp_many({"%s.watchdog" % n: self.heartbeat for n in self.cia402})

    def setp_many(self, values):
        """Set several pins or parameters with one halcmd."""
        if not values:
            return
        text = "".join("setp %s %s\n" % (k, v) for k, v in values.items())
        proc = subprocess.run([HALCMD, "-k", "-f"], input=text, capture_output=True, text=True)
        if proc.returncode != 0:
            raise TestError((proc.stderr or proc.stdout).strip() or "halcmd setp failed")

    def _closed(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        self.log.close()
        self.proc = None
        self.written = {}

    def log_tail(self, lines=15):
        try:
            with open(self.log_path, errors="replace") as f:
                return "".join(f.readlines()[-lines:])
        except (OSError, TypeError):
            return ""

    # -- pins

    def pins(self, prefix=""):
        args = ["-s", "show", "pin"] + ([prefix] if prefix else [])
        return parse_pins(halcmd(*args))

    def writable(self, pin):
        return pin.dir == "IN" and not pin.name.startswith(self.read_only)

    def set_pin(self, pin, value):
        """Set an input pin of the component (an output to a slave)."""
        if not self.running:
            raise TestError("the test does not run")
        if not self.writable(pin):
            raise TestError("%s cannot be set in the I/O test" % pin.name)
        if pin.type == "bit":
            text = "TRUE" if value in (True, 1, "1", "TRUE", "true") else "FALSE"
        elif pin.type == "float":
            text = repr(float(value))
        else:
            text = str(int(value, 0) if isinstance(value, str) else int(value))
        halcmd("setp", pin.name, text)
        self.written[pin.name] = pin.type
