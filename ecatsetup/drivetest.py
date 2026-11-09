"""The CiA 402 drive test: enable one drive in CSP and jog it in small
steps, within limits written to the drive itself, while the I/O test
(testmode.TestRun) runs.

Safety, in layers:

- Nothing moves without a deliberate act: the window enables the drive
  only while a button is held (hold-to-enable), and every jog is a held
  button too.
- The heartbeat: the drive's watchdog pin must keep changing
  (testmode.WATCHDOG_MS); if this program hangs or dies, the servo thread
  shuts the drive down by itself.
- Limits in the drive, where it has the objects: travel (0x607D) around
  the position at the start, a tight following-error window (0x6065, or
  the vendor's object), and the maximum torque (0x6072). The drive's own
  values are read first and written back when the test ends; until then
  they are also kept in a file (RESTORE_FILE), so that they can be put
  back after a crash.
- Limits in HAL, in the servo thread: the limit3 in front of pos-cmd
  keeps velocity, acceleration and travel low, whatever the drive has.

Only CSP drives can be moved here; the drive's other modes come later.
"""

import json
import os
from dataclasses import asdict, dataclass

from . import testmode, tool

RESTORE_FILE = os.path.join(os.environ.get("XDG_STATE_HOME") or
                            os.path.expanduser("~/.local/state"), "ecatsetup", "drive-limits.json")

# Defaults, in machine units (mm) and seconds.
DEFAULTS = {"scale": 1.0, "max-velocity": 5.0, "max-acceleration": 50.0, "travel": 20.0,
            "following-error": 2.0, "max-torque": 30.0}

# Where a drive keeps a limit, if not in the CiA 402 object: (vendor,
# product) -> {limit: (index, subindex, tool type)}.
LIMIT_QUIRKS = {
    # Rtelligent ECT60: "Position trace error Limit", in pulses (drive
    # error 0x8611 when exceeded)
    (0x00000A88, 0x0A880002): {"following-error": (0x2022, 0, "uint32")},
}
STANDARD_LIMITS = {
    "following-error": (0x6065, 0, "uint32"),
    "max-torque": (0x6072, 0, "uint16"),
}


@dataclass
class Limit:
    name: str           # travel-min, travel-max, following-error, max-torque
    index: int
    subindex: int
    dtype: str          # the ethercat tool's type
    value: int
    text: str           # what it means, for the window
    original: int = None


def identity(slave):
    ident = slave.get("identity") or {}
    try:
        return int(str(ident.get("vendor")), 16), int(str(ident.get("product")), 16)
    except ValueError:
        return None


def plan_limits(slave, has_object, scale, start_counts, settings):
    """The limits to write to a drive, and notes on those it cannot take.

    has_object(index, subindex): whether the drive's dictionary has it;
    scale: counts per machine unit; start_counts: the drive's position."""
    limits, notes = [], []
    travel = int(round(abs(settings["travel"] * scale)))
    if has_object(0x607D, 1) and has_object(0x607D, 2):
        limits += [Limit("travel-min", 0x607D, 1, "int32", start_counts - travel,
                         "travel %+g mm from start (0x607D:01)" % -settings["travel"]),
                   Limit("travel-max", 0x607D, 2, "int32", start_counts + travel,
                         "travel %+g mm from start (0x607D:02)" % settings["travel"])]
    else:
        notes.append("no software position limits (0x607D) in the drive: travel is limited "
                     "in HAL only")
    quirks = LIMIT_QUIRKS.get(identity(slave), {})
    for name, value, text in (
            ("following-error", int(round(abs(settings["following-error"] * scale))),
             "following error %g mm" % settings["following-error"]),
            ("max-torque", int(round(settings["max-torque"] * 10)),
             "max torque %g %%" % settings["max-torque"])):
        where = quirks.get(name) or STANDARD_LIMITS[name]
        if has_object(where[0], where[1]):
            limits.append(Limit(name, where[0], where[1], where[2], value,
                                "%s (0x%04X:%02X)" % (text, where[0], where[1])))
        else:
            notes.append("no %s object in the drive: %s" % (
                name.replace("-", " "),
                "limited in HAL by the velocity and acceleration only" if name == "max-torque"
                else "the drive keeps its own window"))
    notes.append("velocity %g mm/s and acceleration %g mm/s2 are limited in HAL (limit3)"
                 % (settings["max-velocity"], settings["max-acceleration"]))
    return limits, notes


def pending_restores():
    """Limits written by a drive test that were not put back (a crash)."""
    try:
        with open(RESTORE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _save_restores(entries):
    os.makedirs(os.path.dirname(RESTORE_FILE), exist_ok=True)
    tmp = RESTORE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(entries, f, indent=1)
    os.replace(tmp, RESTORE_FILE)


def restore_pending():
    """Put back limits a crashed test left in drives. Returns (done, failed)
    as readable strings; failed entries stay in the file."""
    done, failed, keep = [], [], []
    for e in pending_restores():
        try:
            tool.download(e["position"], e["index"], e["subindex"], str(e["original"]), e["dtype"])
            done.append("slave %d 0x%04X:%02X = %d" % (e["position"], e["index"], e["subindex"],
                                                       e["original"]))
        except tool.ToolError as x:
            failed.append("slave %d 0x%04X:%02X: %s" % (e["position"], e["index"], e["subindex"], x))
            keep.append(e)
    if keep:
        _save_restores(keep)
    elif os.path.exists(RESTORE_FILE):
        os.remove(RESTORE_FILE)
    return done, failed


class DriveTest:
    """One drive, prepared for moving: limits written, HAL set up."""

    def __init__(self, run, slave, coe=None, settings=None):
        if slave["name"] not in run.drives:
            raise testmode.TestError("%s is not a CSP drive: only CSP drives can be moved in the "
                                     "test" % slave["name"])
        self.run, self.slave, self.coe = run, slave, coe
        self.settings = dict(DEFAULTS, **(settings or {}))
        self.name = slave["name"]
        self.position = slave["position"]
        self.limit3, self.sel, _ = testmode.jog_names(run.drives.index(self.name))
        self.limits, self.notes = [], []
        self.start = None       # machine units, at prepare()
        self.target = None
        self.prepared = False

    def _has(self, index, subindex):
        if self.coe is not None and self.coe.objects:
            return (index, subindex) in self.coe.entries
        return tool.upload(self.position, index, subindex, "uint8") is not None

    def pin(self, name):
        p = self.run.pins("%s.%s" % (self.name, name)).get("%s.%s" % (self.name, name))
        if p is None:
            raise testmode.TestError("%s.%s: no such pin" % (self.name, name))
        return p.value

    def prepare(self):
        """Write the limits to the drive and to HAL. The drive stays disabled."""
        s = self.settings
        if s["scale"] == 0:
            raise testmode.TestError("the scale (counts per mm) cannot be 0")
        self.run.setp_many({"%s.enable" % self.name: "FALSE",
                            "%s.pos-scale" % self.name: repr(float(s["scale"]))})
        counts = int(self.pin("drv-actual-position"))
        self.limits, self.notes = plan_limits(self.slave, self._has, s["scale"], counts, s)
        # the drive's own values first, kept in a file until put back
        for lim in self.limits:
            lim.original = tool.upload(self.position, lim.index, lim.subindex, lim.dtype)
            if lim.original is None:
                raise testmode.TestError("cannot read 0x%04X:%02X from slave %d"
                                         % (lim.index, lim.subindex, self.position))
        # each original is in the file before its limit is written, so that
        # a crash at any point leaves nothing in the drive without a way back
        tried = []
        try:
            for lim in self.limits:
                _save_restores(pending_restores() + [
                    dict(asdict(lim), position=self.position, slave=self.name)])
                tried.append(lim)
                tool.download(self.position, lim.index, lim.subindex, str(lim.value), lim.dtype)
        except tool.ToolError as e:
            self.limits = tried
            self.restore()
            raise testmode.TestError("the drive refused 0x%04X:%02X = %d: %s"
                                     % (lim.index, lim.subindex, lim.value, e))
        self.start = float(self.pin("pos-fb"))
        self.target = self.start
        travel = abs(s["travel"])
        self.run.setp_many({
            "%s.maxv" % self.limit3: repr(float(abs(s["max-velocity"]))),
            "%s.maxa" % self.limit3: repr(float(abs(s["max-acceleration"]))),
            "%s.min" % self.limit3: repr(self.start - travel),
            "%s.max" % self.limit3: repr(self.start + travel),
            "%s.in1" % self.sel: repr(self.start)})
        self.prepared = True

    def enable(self, on):
        """Hold-to-enable: on while the button is held. The target starts
        where the drive is, so that enabling does not move it."""
        if not self.prepared:
            raise testmode.TestError("prepare the drive first")
        if on:
            self.target = float(self.pin("pos-fb"))
            self.run.setp_many({"%s.in1" % self.sel: repr(self.target)})
        self.run.setp_many({"%s.enable" % self.name: "TRUE" if on else "FALSE"})

    def jog(self, delta):
        """Move the target by delta (machine units), within the travel."""
        if not self.prepared or self.target is None:
            raise testmode.TestError("prepare the drive first")
        travel = abs(self.settings["travel"])
        self.target = min(max(self.target + delta, self.start - travel), self.start + travel)
        self.run.setp_many({"%s.in1" % self.sel: repr(self.target)})
        return self.target

    def fault_reset(self, on):
        self.run.setp_many({"%s.fault-reset" % self.name: "TRUE" if on else "FALSE"})

    def restore(self):
        """Disable the drive and put its own limits back. Returns the
        failures as readable strings."""
        failed = []
        try:
            self.run.setp_many({"%s.enable" % self.name: "FALSE", "%s.maxv" % self.limit3: "0"})
        except (testmode.TestError, OSError):
            pass
        entries = pending_restores()
        for lim in self.limits:
            try:
                tool.download(self.position, lim.index, lim.subindex, str(lim.original), lim.dtype)
                entries = [e for e in entries if not (
                    e.get("position") == self.position and e.get("index") == lim.index
                    and e.get("subindex") == lim.subindex)]
            except tool.ToolError as e:
                failed.append("0x%04X:%02X back to %d: %s" % (lim.index, lim.subindex,
                                                             lim.original, e))
        if entries:
            _save_restores(entries)
        elif os.path.exists(RESTORE_FILE):
            os.remove(RESTORE_FILE)
        self.prepared = False
        return failed


def hal_scale(hal_dir, name):
    """<name>.pos-scale as set in a configuration's HAL files, or None."""
    try:
        files = [os.path.join(hal_dir, f) for f in os.listdir(hal_dir) if f.endswith(".hal")]
    except OSError:
        return None
    for path in files:
        try:
            with open(path, errors="replace") as f:
                for line in f:
                    p = line.split("#")[0].split()
                    if len(p) == 3 and p[0] == "setp" and p[1] == "%s.pos-scale" % name:
                        try:
                            return float(p[2])
                        except ValueError:
                            pass
        except OSError:
            pass
    return None


