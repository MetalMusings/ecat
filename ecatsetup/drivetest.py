"""The CiA 402 drive test: enable one drive and move it in small steps,
within limits written to the drive itself, while the I/O test
(testmode.TestRun) runs. A drive in CSP is jogged through a limit3 in
HAL; a drive in PP moves by steps to a target the drive plans itself; a
drive in PV runs at a low velocity while a button is held; a drive with
`hm` in its modes homes by its own method.

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
  keeps velocity, acceleration and travel low, whatever the drive has
  (CSP). In PP and PV the drive plans the motion itself: this program
  keeps the target within the travel and the velocity within the
  maximum, and writes the maximum profile velocity (0x607F) to the drive
  where it has it.
- The profile only acts on a rising edge of pp-start or home once the
  drive is in "Operation enabled": a move or homing asked for while the
  drive is still enabling waits (pending) until tick() sees it enabled.
"""

import json
import os
from dataclasses import asdict, dataclass

from . import config, testmode, tool
from . import quirks as q

RESTORE_FILE = os.path.join(os.environ.get("XDG_STATE_HOME") or
                            os.path.expanduser("~/.local/state"), "ecatsetup", "drive-limits.json")

# Defaults, in machine units (mm) and seconds.
DEFAULTS = {"scale": 1.0, "max-velocity": 5.0, "max-acceleration": 50.0, "travel": 20.0,
            "following-error": 2.0, "max-torque": 30.0}

STANDARD_LIMITS = {
    "following-error": (0x6065, 0, "uint32"),
    "max-torque": (0x6072, 0, "uint16"),
    "max-velocity": (0x607F, 0, "uint32"),
}

# the modes a drive can be moved in here
MOVABLE = ("csp", "pp", "pv")


def mode_of(slave):
    return ((slave or {}).get("cia402") or {}).get("mode")


def can_home(slave):
    return "hm" in (((slave or {}).get("cia402") or {}).get("modes") or [])


def testable(slave):
    """Whether the drive test can move or home this slave."""
    return (slave or {}).get("profile") == "cia402" and (mode_of(slave) in MOVABLE
                                                         or can_home(slave))


@dataclass
class Limit:
    name: str           # travel-min, travel-max, following-error, max-torque
    index: int
    subindex: int
    dtype: str          # the ethercat tool's type
    value: int
    text: str           # what it means, for the window
    original: int = None


identity = q.identity


def plan_limits(slave, has_object, scale, start_counts, settings, axis=0):
    """The limits to write to a drive, and notes on those it cannot take.

    has_object(index, subindex): whether the drive's dictionary has it;
    scale: counts per machine unit; start_counts: the drive's position;
    axis: the axis of a drive with several, whose CiA 402 objects are at
    index + axis * 0x800 (vendor objects of quirks are not moved)."""
    off = axis * config.AXIS_OFFSET
    if off:
        # planned as axis 0, then moved to the axis's objects
        limits, notes = plan_limits(
            slave, lambda i, sub: has_object(i + off if 0x6000 <= i < 0x6800 else i, sub),
            scale, start_counts, settings)
        for lim in limits:
            if 0x6000 <= lim.index < 0x6800:
                lim.text = lim.text.replace("0x%04X" % lim.index, "0x%04X" % (lim.index + off))
                lim.index += off
        return limits, notes
    limits, notes = [], []
    mode = mode_of(slave) or "csp"
    travel = int(round(abs(settings["travel"] * scale)))
    # where the drive keeps a limit, if not in the CiA 402 object (quirk files)
    quirks = q.limits(slave)
    if mode == "pv":
        notes.append("PV runs while a button is held: there is no travel limit")
    elif isinstance(quirks.get("travel"), str) and mode == "csp":
        notes.append("%s: travel is limited in HAL only" % quirks["travel"])
    elif has_object(0x607D, 1) and has_object(0x607D, 2):
        limits += [Limit("travel-min", 0x607D, 1, "int32", start_counts - travel,
                         "travel %+g mm from start (0x607D:01)" % -settings["travel"]),
                   Limit("travel-max", 0x607D, 2, "int32", start_counts + travel,
                         "travel %+g mm from start (0x607D:02)" % settings["travel"])]
    else:
        notes.append("no software position limits (0x607D) in the drive: travel is limited "
                     "in %s only" % ("HAL" if mode == "csp" else "this program's targets"))
    if mode in ("pp", "pv"):
        where = quirks.get("max-velocity") or STANDARD_LIMITS["max-velocity"]
        if has_object(where[0], where[1]):
            limits.append(Limit("max-velocity", where[0], where[1], where[2],
                                int(round(abs(settings["max-velocity"] * scale))),
                                "max profile velocity %g mm/s (0x%04X:%02X)"
                                % (settings["max-velocity"], where[0], where[1])))
        else:
            notes.append("no max profile velocity (0x607F) in the drive: the velocity is "
                         "limited by this program's commands only")
    if mode == "pp" and has_object(0x6081, 0):
        # the profile velocity of the moves: also here, for a drive whose 0x6081 is not in
        # the process data (no pp-velocity pin)
        limits.append(Limit("profile-velocity", 0x6081, 0, "uint32",
                            int(round(abs(settings["max-velocity"] * scale))),
                            "profile velocity %g mm/s (0x6081)" % settings["max-velocity"]))
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
    if mode == "csp":
        notes.append("velocity %g mm/s and acceleration %g mm/s2 are limited in HAL (limit3)"
                     % (settings["max-velocity"], settings["max-acceleration"]))
    else:
        notes.append("the drive ramps with its own acceleration (0x6083/0x6084)")
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

    def __init__(self, run, slave, coe=None, settings=None, axis=0):
        if not testable(slave):
            raise testmode.TestError("%s cannot be moved in the test: only cia402 drives in "
                                     "CSP, PP or PV, or with hm in their modes" % slave["name"])
        self.run, self.slave, self.coe = run, slave, coe
        self.settings = dict(DEFAULTS, **(settings or {}))
        names = config.axes(slave)
        if not 0 <= axis < len(names):
            raise testmode.TestError("%s has no axis %d" % (slave["name"], axis))
        self.axis = axis
        self.name = names[axis]         # the axis's pin prefix
        self.position = slave["position"]
        self.mode = mode_of(slave)
        self.homes = can_home(slave)
        if self.mode == "csp":
            self.limit3, self.sel, _ = testmode.jog_names(run.drives.index(self.name))
        else:
            self.limit3 = self.sel = None
        self.limits, self.notes = [], []
        self.start = None       # machine units, at prepare()
        self.target = None
        self.pending = None     # ("move", target), ("run", velocity) or ("home",)
        self.prepared = False

    @property
    def moves(self):
        """Whether the drive can be moved (not only homed)."""
        return self.mode in MOVABLE

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
        first = {"%s.enable" % self.name: "FALSE",
                 "%s.pos-scale" % self.name: repr(float(s["scale"]))}
        if self.mode in ("pp", "pv"):
            # the drive's velocity units taken as counts per second
            first["%s.velo-scale" % self.name] = repr(float(s["scale"]))
        if self.mode == "pv":
            first["%s.velocity-cmd" % self.name] = "0"
        if self.mode == "pp":
            first["%s.pp-start" % self.name] = "FALSE"
        if self.homes:
            first["%s.home" % self.name] = "FALSE"
        self.run.setp_many(first)
        counts = int(self.pin("drv-actual-position"))
        self.limits, self.notes = plan_limits(self.slave, self._has, s["scale"], counts, s,
                                              self.axis)
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
        if self.mode == "csp":
            self.run.setp_many({
                "%s.maxv" % self.limit3: repr(float(abs(s["max-velocity"]))),
                "%s.maxa" % self.limit3: repr(float(abs(s["max-acceleration"]))),
                "%s.min" % self.limit3: repr(self.start - travel),
                "%s.max" % self.limit3: repr(self.start + travel),
                "%s.in1" % self.sel: repr(self.start)})
        elif self.mode == "pp":
            values = {"%s.pp-relative" % self.name: "FALSE"}
            # the pin is there only when 0x6081 is in the process data; else the drive's
            # own 0x6081, written above, sets the velocity
            if "%s.pp-velocity" % self.name in self.run.pins("%s.pp-velocity" % self.name):
                values["%s.pp-velocity" % self.name] = repr(float(abs(s["max-velocity"])))
            self.run.setp_many(values)
        self.prepared = True

    def enable(self, on):
        """Hold-to-enable: on while the button is held. The target starts
        where the drive is, so that enabling does not move it. Off also
        drops a pending move, a PV velocity and the start edges."""
        if not self.prepared:
            raise testmode.TestError("prepare the drive first")
        values = {}
        if on:
            if self.mode == "csp":
                self.target = float(self.pin("pos-fb"))
                values["%s.in1" % self.sel] = repr(self.target)
        else:
            self.pending = None
            if self.mode == "pv":
                values["%s.velocity-cmd" % self.name] = "0"
            if self.mode == "pp":
                values["%s.pp-start" % self.name] = "FALSE"
            if self.homes:
                values["%s.home" % self.name] = "FALSE"
        values["%s.enable" % self.name] = "TRUE" if on else "FALSE"
        self.run.setp_many(values)

    def _clamp(self, target):
        travel = abs(self.settings["travel"])
        return min(max(target, self.start - travel), self.start + travel)

    def jog(self, delta):
        """Move the target by delta (machine units), within the travel. In
        CSP the limit3 follows at once; in PP the move starts once the
        drive is enabled (tick)."""
        if not self.prepared or self.target is None:
            raise testmode.TestError("prepare the drive first")
        if self.mode == "csp":
            self.target = self._clamp(self.target + delta)
            self.run.setp_many({"%s.in1" % self.sel: repr(self.target)})
        elif self.mode == "pp":
            # from where the drive is: a step never adds up beyond the travel
            self.target = self._clamp(float(self.pin("pos-fb")) + delta)
            self.pending = ("move", self.target)
        else:
            raise testmode.TestError("%s is in %s: it is run, not jogged" % (self.name, self.mode))
        return self.target

    def run_velocity(self, sign):
        """PV: run at the maximum velocity, sign +1 or -1, while held."""
        if not self.prepared:
            raise testmode.TestError("prepare the drive first")
        if self.mode != "pv":
            raise testmode.TestError("%s is not in PV" % self.name)
        self.pending = ("run", sign * abs(self.settings["max-velocity"]))

    def home(self):
        """Home by the drive's method, once it is enabled (tick)."""
        if not self.prepared:
            raise testmode.TestError("prepare the drive first")
        if not self.homes:
            raise testmode.TestError("%s has no hm in its modes" % self.name)
        self.pending = ("home",)

    def tick(self, enabled):
        """Called with the drive's stat-op-enabled: starts a pending move,
        run or homing once the drive is enabled. Returns what it started."""
        if self.pending is None or not enabled:
            return None
        what, self.pending = self.pending, None
        if what[0] == "move":
            self.run.setp_many({"%s.pp-target" % self.name: repr(what[1]),
                                "%s.pp-start" % self.name: "TRUE"})
        elif what[0] == "run":
            self.run.setp_many({"%s.velocity-cmd" % self.name: repr(what[1])})
        elif what[0] == "home":
            self.run.setp_many({"%s.home" % self.name: "TRUE"})
        return what[0]

    def fault_reset(self, on):
        self.run.setp_many({"%s.fault-reset" % self.name: "TRUE" if on else "FALSE"})

    def restore(self):
        """Disable the drive and put its own limits back. Returns the
        failures as readable strings."""
        failed = []
        try:
            values = {"%s.enable" % self.name: "FALSE"}
            if self.limit3:
                values["%s.maxv" % self.limit3] = "0"
            if self.mode == "pv":
                values["%s.velocity-cmd" % self.name] = "0"
            self.pending = None
            self.run.setp_many(values)
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


