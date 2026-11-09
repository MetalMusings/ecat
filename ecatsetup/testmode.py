"""Test mode: run the ecat component in halrun with a servo thread, without
LinuxCNC, so that inputs can be read and outputs set from EtherCAT Setup.

The configuration being edited is written to a folder of its own and
loaded from there, so unsaved changes can be tried. Pins are read and set
with halcmd. (A component of LinuxCNC's Python hal module in the setup
program would be killed by halrun's "unload all" when the test stops.)

Part 2 of the GUI tests I/O only: the pins of CiA 402 drives and the
master's pins are read-only here, so nothing enables a drive. Pins set
during the test are put back to 0 before the test stops.

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

HAL_FILE = """\
# Written by EtherCAT Setup for its test mode.
loadrt ecat config={config}
loadrt threads name1=servo-thread period1={period}
addf ecat.read-all servo-thread
addf ecat.write-all servo-thread
start
"""


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
            f.write(HAL_FILE.format(config=cfg_path, period=self.period))
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
            for name, type_ in list(self.written.items()):
                try:
                    halcmd("setp", name, "0" if type_ != "bit" else "FALSE")
                except (TestError, OSError):
                    pass
            # let a few cycles carry the zeros out before the thread stops
            time.sleep(max(0.05, 5 * self.period / 1e9))
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
