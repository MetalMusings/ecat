"""Bringing a drive up: what is known, missing, or shows only when tried, for the
goal "to OP and moving" (docs/development.md "Device facts: properties, advice,
and bringing up a new drive", item 3). No Qt: the slave page's card shows the rows.

The rows are worked out again each time from the configuration's slave, what was
read from the bus, the quirk files and measured results, and what the workbench saw
in this window's session (`Seen`). What the workbench saw is not stored: it belongs
to the session and the machine. Only properties go to a quirk file, and only when
the user chooses so on the card that owns them.
"""

import os
import time
from dataclasses import dataclass, field

from . import config, diagnose, measured, quirks

GOAL = "To OP and moving"
HOMING = "Homing and switches"
GOALS = (GOAL, HOMING)
OK, MISSING, TRY, BAD = "ok", "missing", "try", "bad"
MARKS = {OK: "✓", MISSING: "!", TRY: "?", BAD: "✗"}

# a drive whose position at the start of a run is within this many counts of where
# the run before ended kept it; one that starts within it of 0 did not
SAME_COUNTS = 2


@dataclass
class Row:
    key: str
    state: str          # OK, MISSING, TRY or BAD
    what: str           # the row's name
    text: str           # what is known, or how the gap is filled
    card: str = None    # devicedataview.CARDS: where it is filled
    # a fact the row can keep for the device type: (path, value, what, note, source kind);
    # note not None: the user types the value (what an error code means)
    remember: tuple = None


@dataclass
class Seen:
    """What the workbench saw of one slave in this session."""
    op: str = None              # the time it was first seen in OP
    not_op: str = None          # why not, while the workbench ran
    enabled: str = None         # the time "Operation enabled" was first seen
    stuck: str = None           # held in "Switch on disabled" while enable was asked
    fault: int = None           # 0x603F at a fault
    moved: float = None         # mm the drive followed a move
    runs: list = field(default_factory=list)    # [first, last, run] drv-actual-position per run
    homed: str = None           # the Move card's last homing: "homed" or "error"
    follows_after: bool = None  # whether the drive followed its target after it
    since: dict = field(default_factory=dict)   # how long a state has lasted (monotonic)
    fault_at: float = None      # the move's distance when the fault came: a new move clears it
    last_moved: float = 0.0     # the move's distance last seen

    WAIT = 10           # s on the workbench without OP before it counts as not reaching it
    HELD = 3            # s in "Switch on disabled" with enable on

    def now(self):
        return time.strftime("%H:%M")

    def key(self):
        """What the card shows of it: when this changes, the card is worked out again."""
        return (self.op, self.not_op, self.enabled, self.stuck, self.fault, bool(self.moved),
                tuple((r[0], r[2]) for r in self.runs), tuple(r[1] for r in self.runs[:-1]),
                self.homed, self.follows_after)

    def _lasted(self, what, on, seconds, clock):
        if not on:
            self.since.pop(what, None)
            return False
        start = self.since.setdefault(what, clock)
        return clock - start >= seconds

    # what the workbench reports, cycle by cycle (gui.TestPage.update_values)
    def saw(self, run, op, state=None, error=None, position=None, enable_asked=False,
            moved=None, clock=None):
        clock = time.monotonic() if clock is None else clock
        if op and self.op is None:
            self.op = self.now()
        if op:
            self.not_op = None
        if self._lasted(("op", run), not op, self.WAIT, clock) and self.op is None:
            self.not_op = "not in OP after %d s on the workbench" % self.WAIT
        if state == 4 and self.enabled is None:
            self.enabled = self.now()
            self.stuck = None
        if self._lasted("held", state == 1 and enable_asked, self.HELD, clock) and self.enabled is None:
            self.stuck = self.now()
        if moved is not None:
            self.last_moved = moved
        if state in (6, 7) and error:
            if self.fault != error or self.fault_at is None:
                self.fault_at = self.last_moved
            self.fault = error
        if moved is not None and abs(moved) > 0.01 and state == 4:
            self.moved = max(abs(moved), self.moved or 0)
            # enabled again and moved since the fault: it follows again
            if self.fault and self.enabled and abs(moved - (self.fault_at or 0.0)) > 0.01:
                self.fault = self.fault_at = None
        if op and position is not None:
            if not self.runs or self.runs[-1][2] != run:
                self.runs.append([position, position, run])
            else:
                self.runs[-1][1] = position


def _objects(slave, direction):
    out = set()
    for pdo in ((slave.get("process-data") or {}).get(direction) or []):
        for e in pdo.get("entries") or []:
            try:
                out.add(config.parse_obj(e.get("object")))
            except (ValueError, TypeError):
                pass
    return out


def _start_position(slave, seen):
    """The start position row: the quirk file's property first, then what two runs of
    the workbench showed (offered as the property, source observed)."""
    c = slave.get("cia402") or {}
    known = quirks.keeps_position(slave)
    ticked = bool(c.get("zero-at-start"))
    runs = [r for r in (seen.runs if seen else []) if r]
    kept = None
    before = after = None
    if len(runs) >= 2:
        before, after = runs[-2][1], runs[-1][0]
        if abs(after - before) <= SAME_COUNTS and abs(after) > SAME_COUNTS:
            kept = True
        elif abs(after) <= SAME_COUNTS and abs(before) > SAME_COUNTS:
            kept = False
    keep = None
    if known is None and kept is not None:
        keep = (("position", "kept"), kept, "The drive %s its position over a restart of the bus"
                % ("keeps" if kept else "does not keep"), None, "observed")
    if ticked:
        return Row("start", OK, "Start position", "counts from 0 at start (ticked)", "operation",
                   remember=keep)
    if known is True or kept is True:
        where = "the device's quirk file says so" if known is True else \
            "%d counts at the end of one run, %d at the start of the next" % (before, after)
        return Row("start", OK, "Start position",
                   "the drive keeps its position over a restart of the bus (%s). Nothing is "
                   "needed: LinuxCNC takes the position as it is, and the profile holds it while "
                   "the drive enables. Tick Start position: Position counts from 0 at start only "
                   "if the machine should read 0 at start" % where, "operation", remember=keep)
    if known is False:
        return Row("start", OK, "Start position", "the drive starts from 0 by itself (the device's "
                   "quirk file)")
    if kept is False:
        return Row("start", OK, "Start position",
                   "the drive starts from 0 by itself (it ended a run at %d counts and started "
                   "the next at %d)" % (before, after), remember=keep)
    return Row("start", OK, "Start position",
               "not known whether the drive keeps its position; either works. It shows over two "
               "runs of the workbench with the drive's power on and the drive moved in the first",
               "workbench")


def rows(slave, proposal=None, problems=(), seen=None, period_us=None, dictionary=None):
    """The rows for the goal, for a CiA 402 drive ([] for another slave).
    problems: [(level, text)] the configuration check and the PDO check give for this
    slave; seen: Seen or None; period_us: the servo period; dictionary: the slave's
    object dictionary (coe.Dictionary) or None."""
    if not isinstance(slave, dict) or slave.get("profile") != "cia402" or slave.get("unused"):
        return []
    out = []
    p = proposal
    # the ESI file: PDO sets, DC modes, names
    if p is None:
        out.append(Row("esi", TRY, "ESI file", "the slave was not read from the bus: Rescan"))
    elif not getattr(p, "esi_path", None):
        out.append(Row("esi", MISSING, "ESI file",
                       "none: the PDO choice and DC modes come from one (Choose ESI files...)", "source"))
    elif not getattr(p, "esi_exact", True):
        out.append(Row("esi", MISSING, "ESI file",
                       "for revision 0x%08X, the slave is 0x%08X: check what it gave"
                       % (p.esi_revision or 0, p.revision or 0), "source"))
    else:
        out.append(Row("esi", OK, "ESI file", os.path.basename(p.esi_path)))

    # process data and the mode
    errors = [t for level, t in problems if level == "error"]
    warnings = [t for level, t in problems if level == "warning"]
    # an error about a setting the file lacks (such as PP's 0x607D) is the settings', not
    # the process data's
    settings = [t for t in errors if "needs the setting" in t]
    pd = [t for t in errors if ("PDO" in t or "process" in t or "mode" in t) and t not in settings]
    if pd:
        out.append(Row("pdo", BAD, "Process data", pd[0], "process-data"))
    elif warnings and any("PDO" in t for t in warnings):
        out.append(Row("pdo", MISSING, "Process data", next(t for t in warnings if "PDO" in t),
                       "process-data"))
    else:
        out.append(Row("pdo", OK, "Process data",
                       "what the profile needs is mapped" if p is not None and getattr(p, "esi_path", None)
                       else "as proposed (not checked against an ESI file)", "process-data"))
    if settings:
        out.append(Row("settings", BAD, "Settings", settings[0].split(": ", 1)[-1] + (
            "; Add what the device needs on the Settings card takes the drive's present values"
            if "0x607D" in settings[0] else ""), "settings"))
    outputs, inputs = _objects(slave, "outputs"), _objects(slave, "inputs")
    mode = (slave.get("cia402") or {}).get("mode", "csp")
    if (0x6060, 0) in outputs:
        out.append(Row("mode", OK, "Mode", "%s, set every cycle (0x6060 mapped)" % mode.upper(),
                       "operation"))
    elif config.mode_setting(slave) is not None:
        out.append(Row("mode", OK, "Mode", "%s, set once at start (0x6060:00)" % mode.upper(),
                       "operation"))
    elif (0x6061, 0) in inputs:
        out.append(Row("mode", OK, "Mode", "%s, seen in 0x6061: the drive must be in it already" % mode.upper(),
                       "operation"))
    else:
        out.append(Row("mode", BAD, "Mode", "neither set nor seen: map 0x6060 or 0x6061, or set "
                       "0x6060:00", "process-data"))

    # sync
    dc = slave.get("dc") or {}
    if dc.get("assign-activate"):
        res = measured.results(slave)
        ok = [r for r in res if r.get("result") == "ok"]
        refused = sorted(r["period-us"] for r in res if r.get("result") == "refused")
        if period_us and any(period_us <= x for x in refused):
            out.append(Row("sync", BAD, "Sync", "DC; the device refused SYNC0 at %g us, and your "
                           "servo period is %g us: choose a longer one" % (max(refused), period_us),
                           "sync"))
        elif ok:
            shortest = min(r["period-us"] for r in ok)
            text = "measured: runs at %g us%s" % (shortest, " (refused %s us)" % ", ".join(
                "%g" % x for x in refused) if refused else "")
            if period_us and period_us < shortest:
                out.append(Row("sync", MISSING, "Sync", "DC; your period %g us is shorter than any "
                               "measured on this device (%s)" % (period_us, text), "sync"))
            else:
                out.append(Row("sync", OK, "Sync", "DC, SYNC0; " + text, "operation"))
        else:
            out.append(Row("sync", TRY, "Sync", "DC, SYNC0; the shortest period the device takes is not "
                           "known: shows when the workbench runs (Remember this period)", "sync"))
    else:
        out.append(Row("sync", MISSING, "Sync", "free run: a CSP drive usually needs DC (SYNC0); "
                       "choose its DC mode", "operation"))

    # PP and PV: the drive plans its own moves, so its velocity limit belongs in the settings
    c = slave.get("cia402") or {}
    if {"pp", "pv"} & set([mode] + list(c.get("modes") or [])):
        if dictionary is None or not dictionary.objects:
            out.append(Row("velocity", TRY, "Velocity limit", "whether the drive has a velocity limit "
                           "(0x607F) shows with its dictionary", "settings"))
        else:
            lacking = config.velocity_limits_missing(slave, lambda i, sub: i in dictionary.objects)
            if lacking:
                out.append(Row("velocity", MISSING, "Velocity limit",
                               "%s is not in the settings: in PP and PV the drive plans its own moves "
                               "with it; Add what the device needs takes its present value"
                               % ", ".join("0x%04X:%02X" % (i, sub) for _, i, sub, _ in lacking),
                               "settings"))
            else:
                out.append(Row("velocity", OK, "Velocity limit", "in the settings, or the drive has "
                               "no such object", "settings"))

    # what the workbench saw
    s = seen or Seen()
    if s.op:
        out.append(Row("op", OK, "Reaches OP", "seen at %s" % s.op, "workbench"))
    elif s.not_op:
        out.append(Row("op", BAD, "Reaches OP", s.not_op + ": Diagnose shows its AL status code "
                       "and what it means%s" % (
                           "; when it is 0x0036 (SYNC0 cycle time), the device refused this servo "
                           "period: remember that on the Live tab's Synchronisation card, so "
                           "that it is known for the device" if dc.get("assign-activate") else ""),
                       "diagnose"))
    else:
        out.append(Row("op", TRY, "Reaches OP", "shows when the workbench runs", "workbench"))
    def fault():
        """(text, remember) of the fault seen: in words, or offered to be remembered."""
        if quirks.error_meaning(slave, s.fault):
            return "fault %s" % diagnose.error_text(s.fault, slave), None
        code = "0x%04X" % s.fault
        return ("fault %s: what the drive means by it is in its manual; remember it for the "
                "device type, so that it is said in words next time" % diagnose.error_text(s.fault),
                (("errors", code), None, "Error code %s: what it means" % code, "", "manual"))
    # a fault before "Operation enabled" was seen is the enable's; one after it, the move's
    if s.enabled:
        out.append(Row("enable", OK, "Enables", "\"Operation enabled\" seen at %s" % s.enabled, "move"))
    elif s.fault:
        text, keep = fault()
        out.append(Row("enable", BAD, "Enables", text, "diagnose", remember=keep))
    elif s.stuck:
        out.append(Row("enable", BAD, "Enables",
                       "stays in \"Switch on disabled\" while enable is held: usually the hardware "
                       "(STO, an enable input, the power stage), not the configuration", "move"))
    else:
        out.append(Row("enable", TRY, "Enables", "shows when you write limits and hold Enable "
                       "on the Move card", "move"))
    if s.enabled and s.fault:
        text, keep = fault()
        out.append(Row("follows", BAD, "Follows", "%s%s: Reset fault on the Move card and jog "
                       "again" % (text, " after a move of %.2f mm" % s.moved if s.moved else ""),
                       "move", remember=keep))
    elif s.moved:
        out.append(Row("follows", OK, "Follows", "followed a move of %.2f mm" % s.moved, "move"))
    else:
        out.append(Row("follows", TRY, "Follows", "shows when you jog it on the Move card", "move"))
    out.append(_start_position(slave, s))
    return out


def homing_rows(slave, seen=None, support=(None, None), axis=0):
    """The rows for the goal "homing and switches", for a CiA 402 drive ([] for another
    slave). support: (methods, where) as homing.supported gives them (the window reads
    0x60E3); seen: Seen or None."""
    from . import homing, switches
    if not isinstance(slave, dict) or slave.get("profile") != "cia402" or slave.get("unused"):
        return []
    out = []
    c = slave.get("cia402") or {}
    if "hm" not in (c.get("modes") or []):
        return [Row("hm", MISSING, "Homing by the drive",
                    "not among the drive's modes: tick Homing (hm) under Operation and sync for the "
                    "homing card", "operation")]
    out.append(Row("hm", OK, "Homing by the drive", "among the drive's modes"))
    nums, where = support or (None, None)
    if nums:
        out.append(Row("methods", OK, "Methods", "%s (from the %s)" % (_numbers(nums), where), "homing"))
    else:
        out.append(Row("methods", TRY, "Methods",
                       "not known: every method of the standard may be tried. The manual lists them: "
                       "Remember the drive's methods... on the homing card", "homing"))
    chosen = homing.choice(slave, axis)
    if not chosen:
        out.append(Row("method", MISSING, "Method", "none chosen: choose one on the homing card", "homing"))
        return out
    n = chosen["method"]
    m = homing.method(n, slave)
    own = n not in homing.CATALOG or n in (36, 37)
    described = n in ((quirks.of(slave).get("homing") or {}).get("describe") or {})
    if own and not described:
        out.append(Row("method", MISSING, "Method",
                       "%d: the standard leaves its meaning to the drive. What it does is in the "
                       "drive's manual: Remember the drive's methods... says it for the next one" % n,
                       "homing"))
    else:
        out.append(Row("method", OK, "Method", "%d: %s" % (n, m.text), "homing"))
    if own:
        extras = ((quirks.of(slave).get("homing") or {}).get("extras") or {}).get(n)
        if extras:
            out.append(Row("extras", OK, "Settings it needs", "%d from the quirk file (%s)" % (
                len(extras), ", ".join(st["object"] for st in extras)), "settings"))
        else:
            out.append(Row("extras", TRY, "Settings it needs",
                           "a method of the drive's own may need settings of the drive's own (such as "
                           "torque limits for torque homing): see its manual, and add them under "
                           "Settings", "settings"))
    need = switches.needed(slave, axis)
    if not need:
        out.append(Row("switches", OK, "Switches", "the method needs none"))
    else:
        what = " and the ".join(switches.TEXT[k] for k in need)
        if (seen or Seen()).homed == "homed":
            out.append(Row("switches", OK, "Switches", "the method needs the %s: homing found it" % what))
        else:
            out.append(Row("switches", TRY, "Switches",
                           "the method needs the %s: on the workbench its box on the Switches card "
                           "lights while the drive sees it pressed. Which input it is, and its "
                           "polarity, the drive's manual says (set under Settings)" % what, "switches"))
    if m.uses_index:
        out.append(Row("index", TRY, "Index pulse", "the method needs the encoder's index pulse: "
                       "shows when it homes", "move"))
    s = seen or Seen()
    if s.homed == "homed":
        out.append(Row("homed", OK, "Homes", "homed on the workbench"))
    elif s.homed:
        out.append(Row("homed", BAD, "Homes", "homing failed on the workbench: Diagnose shows the "
                       "drive's error code", "diagnose"))
    else:
        out.append(Row("homed", TRY, "Homes", "Home (hold) on the Move card, with the emergency stop "
                       "within reach", "move"))
    if s.follows_after is True:
        out.append(Row("after", OK, "Follows after homing", "it followed its target after homing"))
    elif s.follows_after is False:
        out.append(Row("after", BAD, "Follows after homing",
                       "it did not follow its target after homing: tick Enable the drive again "
                       "after homing on the homing card, save and home again", "homing"))
    else:
        out.append(Row("after", TRY, "Follows after homing", "the Move card checks it after a homing",
                       "move"))
    return out


def _numbers(nums):
    """17, 18, 19 ... as 17-19."""
    nums = sorted(nums)
    out, start = [], None
    for i, n in enumerate(nums):
        if start is None:
            start = n
        if i + 1 == len(nums) or nums[i + 1] != n + 1:
            out.append(str(start) if start == n else "%d-%d" % (start, n))
            start = None
    return ", ".join(out)


def summary(found):
    """One line: how far the goal is."""
    if not found:
        return ""
    bad = sum(r.state == BAD for r in found)
    missing = sum(r.state == MISSING for r in found)
    tries = sum(r.state == TRY for r in found)
    if not (bad or missing or tries):
        return "Ready: everything for this goal is known."
    bits = []
    if bad:
        bits.append("%d to put right" % bad)
    if missing:
        bits.append("%d missing" % missing)
    if tries:
        bits.append("%d shown only when tried" % tries)
    return ", ".join(bits) + "."
