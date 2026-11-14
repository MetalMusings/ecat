"""The flight recorder of the ecat component: reading its file.

The component keeps time-stamped events of the bus in a ring buffer in a
memory-mapped file (component/ecat_events.h), /dev/shm/ecat-events, and
the previous run's as /dev/shm/ecat-events.prev. They can be read while
LinuxCNC runs and after it has stopped. The layout here must match
ecat_events.h (VERSION).

From version 2 the file also keeps the master's own messages (its log,
such as "EtherCAT WARNING 0-2: Slave seems not to support distributed
clocks!"), up to level info, in a second ring after the events.
"""

import os
import re
import struct
import time
from dataclasses import dataclass

from . import diagnose

PATH = os.environ.get("ECAT_EVENTS") or "/dev/shm/ecat-events"
MAGIC = 0x56454345
VERSION = 2
VERSIONS = (1, 2)       # version 1 has no messages
HEADER = struct.Struct("<IIIIQQII256s")
RECORD = struct.Struct("<QQHhIII")
TEXT_HEADER = struct.Struct("<QII")      # after the events: next, count, record size
TEXT_LEN = 216
TEXT = struct.Struct("<QQiI%ds" % TEXT_LEN)
# syslog levels of the master's messages
LEVELS = {0: "error", 1: "error", 2: "error", 3: "error", 4: "warn"}

START, ACTIVATED, STOP = 1, 2, 3
LINK, MASTER_AL, RESPONDING, WC = 10, 11, 12, 13
SLAVE_AL, SLAVE_ONLINE, SLAVE_OP, DC_SYNC = 20, 21, 22, 23
DRIVE_FAULT, DRIVE_BLOCK, DRIVE_CLEAR = 30, 31, 32
GUARD, LATE, SHIFT = 40, 41, 42

# how serious an event is, for the window: "info", "warn" or "error"
WC_STATES = {0: "zero", 1: "incomplete", 2: "complete"}
GUARD_STATES = {0: "the servo thread runs the bus again", 1: "the servo thread stopped; "
                "taking the slaves to PREOP", 2: "done"}
FAULT_REASONS = {
    1: "drive fault", 2: "no valid process data (working counter, or the slave not in OP)",
    3: "\"Operation enabled\" not reached in time", 4: "the drive left \"Operation enabled\"",
    5: "watchdog (the heartbeat stopped)", 6: "mode not reported, not changed or not stopped in time",
    7: "a command that is not a number (NaN, infinite) or a mode not in the file",
    8: "homing error", 9: "homing not done in time", 10: "PP move not done in time",
    11: "the master's DC sync check timed out"}


class EventsError(Exception):
    pass


@dataclass
class Event:
    index: int
    time: float         # seconds since the epoch
    type: int
    slave: int          # position; -1 for the master
    a: int
    b: int


# "EtherCAT ERROR 0-2: ...", "EtherCAT WARNING 0-main-2: ...", "EtherCAT 0-2: ..."
_SLAVE_TEXT = re.compile(r"EtherCAT(?: [A-Z]+)? \d+-(?:[A-Za-z]+-)?(\d+): ")


@dataclass
class Text:
    """One of the master's messages."""
    index: int
    time: float         # seconds since the epoch
    level: int          # syslog level: 3 error, 4 warning, 6 info
    text: str

    @property
    def slave(self):
        """The position of the slave it is about, or None."""
        m = _SLAVE_TEXT.match(self.text)
        return int(m.group(1)) if m else None

    @property
    def severity(self):
        return LEVELS.get(self.level, "info")


@dataclass
class Log:
    path: str
    start: float
    period_ns: int
    pid: int
    config: str
    total: int          # events written since the start
    events: list        # [Event], oldest first; the oldest may have been overwritten
    texts: list = None  # [Text], the master's messages, oldest first (None: version 1)
    texts_total: int = 0

    @property
    def lost(self):
        return self.total - len(self.events)

    @property
    def running(self):
        """Whether the process that writes it still runs."""
        try:
            with open("/proc/%d/cmdline" % self.pid, "rb") as f:
                return b"rtapi_app" in f.read()
        except OSError:
            return False


def read(path=PATH):
    """The log in a recorder file."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        raise EventsError("no event log at %s (%s): ecat has not run since the computer "
                          "started, or is older than this program" % (path, e.strerror))
    if len(data) < HEADER.size:
        raise EventsError("%s is too short" % path)
    magic, version, n, size, start, nxt, period, pid, config = HEADER.unpack_from(data)
    if magic != MAGIC or version not in VERSIONS or size != RECORD.size:
        raise EventsError("%s is not an event log of this version (%d)" % (path, VERSION))
    if len(data) < HEADER.size + n * size:
        raise EventsError("%s is too short" % path)
    events = []
    for i in range(max(0, nxt - n), nxt):
        seq, t, typ, slave, a, b, _ = RECORD.unpack_from(data, HEADER.size + (i % n) * size)
        if seq == i + 1:
            events.append(Event(i, t / 1e9, typ, slave, a, b))
    log = Log(path, start / 1e9, period, pid, config.split(b"\0", 1)[0].decode(errors="replace"),
              nxt, events)
    if version >= 2:
        log.texts, log.texts_total = _texts(data, HEADER.size + n * size, path)
    return log


def _texts(data, at, path):
    """The master's messages of a version 2 file, from offset at."""
    if len(data) < at + TEXT_HEADER.size:
        raise EventsError("%s is too short" % path)
    nxt, n, size = TEXT_HEADER.unpack_from(data, at)
    at += TEXT_HEADER.size
    if size != TEXT.size or len(data) < at + n * size:
        raise EventsError("%s is not an event log of this version (%d)" % (path, VERSION))
    out = []
    for i in range(max(0, nxt - n), nxt):
        seq, t, level, _, raw = TEXT.unpack_from(data, at + (i % n) * size)
        if seq == i + 1:
            out.append(Text(i, t / 1e9, level, raw.split(b"\0", 1)[0].decode(errors="replace")))
    return out, nxt


def folded(texts):
    """[(Text, times)]: a message repeated right after itself is shown once."""
    out = []
    for t in texts:
        if out and out[-1][0].text == t.text and out[-1][0].level == t.level:
            out[-1][1] += 1
        else:
            out.append([t, 1])
    return [(t, n) for t, n in out]


def text_line(t, times=1):
    """A master's message as shown: the text, and how often it came."""
    return "master: %s%s" % (t.text, " (%d times)" % times if times > 1 else "")


def slave_texts(log, position):
    """The master's messages about one slave, oldest first, folded."""
    return folded([t for t in (log.texts or []) if t.slave == position])


def slave_messages(position, paths=None):
    """The master's messages about a slave from the latest run that has any:
    (path, [(Text, times)]); (None, []) if no run has any."""
    for path in paths or (PATH, PATH + ".prev"):
        try:
            found = slave_texts(read(path), position)
        except EventsError:
            continue
        if found:
            return path, found
    return None, []


def merged(log, events=None, texts=None):
    """Events and the master's messages in time order: [("event", Event) or
    ("text", (Text, times))]."""
    evs = log.events if events is None else events
    tx = folded(log.texts or []) if texts is None else texts
    rows = [(e.time, 0, e.index, ("event", e)) for e in evs]
    rows += [(t.time, 1, t.index, ("text", (t, n))) for t, n in tx]
    return [r[3] for r in sorted(rows, key=lambda r: r[:3])]


def _al(state):
    s = diagnose.AL_STATES.get(state & 0x0F, "0x%X" % (state & 0x0F))
    return s + (" + ERROR" if state & 0x10 else "")


def _states(bits):
    names = [n for b, n in ((1, "INIT"), (2, "PREOP"), (4, "SAFEOP"), (8, "OP")) if bits & b]
    return "+".join(names) or "none"


def text(e, names=None):
    """(severity, text) of an event; names: {position: slave name}."""
    who = "master" if e.slave < 0 else "slave %d%s" % (
        e.slave, " (%s)" % names[e.slave] if names and e.slave in names else "")
    t = e.type
    if t == START:
        return "info", "ecat loaded, %d slave%s in the file" % (e.a, "" if e.a == 1 else "s")
    if t == ACTIVATED:
        return "info", "master activated, servo period %d us" % (e.a // 1000)
    if t == STOP:
        return "info", "ecat unloading"
    if t == LINK:
        return ("info", "link up") if e.a else ("error", "link DOWN: cable, or the first slave's power")
    if t == MASTER_AL:
        return ("info" if e.a in (0x08, 0x02) or not e.b else "warn",
                "slaves' states: %s (was %s)" % (_states(e.a), _states(e.b)))
    if t == RESPONDING:
        return ("warn" if e.a < e.b else "info",
                "%d slaves respond (was %d)" % (e.a, e.b))
    if t == WC:
        # from zero, on the way up at the start, incomplete is normal
        return ("info" if e.a == 2 or (e.a == 1 and e.b == 0) else "error",
                "working counter %s (was %s)%s" % (
                    WC_STATES.get(e.a, e.a), WC_STATES.get(e.b, e.b),
                    "" if e.a == 2 or e.b == 0 else ": process data not exchanged with every slave"))
    if t == SLAVE_AL:
        return ("warn" if e.a & 0x10 or (e.b & 0x0F) == 8 else "info",
                "%s: %s (was %s)" % (who, _al(e.a), _al(e.b) if e.b else "unknown"))
    if t == SLAVE_ONLINE:
        return ("info", "%s online" % who) if e.a else ("error", "%s OFFLINE" % who)
    if t == SLAVE_OP:
        return ("info", "%s operational" % who) if e.a else ("warn", "%s not operational" % who)
    if t == DC_SYNC:
        return ("error", "%s: DC sync check timed out (clock not within 10 us)" % who) if e.a \
            else ("info", "%s: DC sync check passed" % who)
    if t == DRIVE_FAULT:
        return "error", "%s: drive fault %s (status word 0x%04X)" % (
            who, diagnose.error_text(e.a), e.b)
    if t == DRIVE_CLEAR:
        return "info", "%s: drive fault cleared%s" % (who, " (axis %d)" % e.b if e.b else "")
    if t == DRIVE_BLOCK:
        return "error", "%s: the profile shut the drive down%s: fault-reason %d, %s" % (
            who, " (axis %d)" % e.b if e.b else "", e.a, FAULT_REASONS.get(e.a, "?"))
    if t == GUARD:
        if e.a == 2:
            if e.b & 0x0C:
                return "error", "stop guard: gave up, slaves still in %s (outputs zero)" % _states(e.b)
            return "info", "stop guard: done, outputs zero, slaves in %s" % _states(e.b)
        return ("info" if e.a == 0 else "warn", "stop guard: %s" % GUARD_STATES.get(e.a, e.a))
    if t == LATE:
        return "warn", "servo thread late: %d us since its last write%s" % (
            e.a, ", %d more within the second" % e.b if e.b else "")
    if t == SHIFT:
        return ("info" if e.b == 2 else "warn",
                "SYNC0 shift %d us, turn-around %d cycles" % (e.a // 1000, e.b))
    return "info", "event %d (%s, %d, %d)" % (t, who, e.a, e.b)


def annotate(evs, names=None):
    """{event index: (severity, text)} for events in order, with what the events
    before them say: the master reading the bus again, or the slaves still
    coming up, are not faults. An event whose context is not in the list (the
    log was overwritten) gets its own text."""
    out = {}
    responding = master_al = link = None
    for e in evs:
        sev, msg = text(e, names)
        if e.type == LINK:
            link = e.a
        elif e.type == RESPONDING:
            responding = e.a
        elif e.type == MASTER_AL:
            master_al = e.a
        elif e.type == SLAVE_ONLINE and not e.a and link and responding is not None \
                and 0 <= e.slave < responding:
            # after a cable break the master reads the bus again and every slave is
            # offline for a while, also those that answer: only those beyond it are
            sev, msg = "info", "%s offline while the master reads the bus again " \
                "(it responds)" % ("slave %d" % e.slave if not names or e.slave not in names
                                   else "slave %d (%s)" % (e.slave, names[e.slave]))
        elif e.type == WC and e.a != 2 and e.b == 2 and link and master_al not in (None, 0, 8):
            sev, msg = "info", "working counter %s (was %s): the slaves are still coming up" % (
                WC_STATES.get(e.a, e.a), WC_STATES.get(e.b, e.b))
        elif e.type == WC and e.a == 0 and e.b == 1 and link and master_al not in (None, 0, 8):
            sev, msg = "info", "working counter zero (was incomplete): the slaves are still " \
                "coming up"
        out[e.index] = (sev, msg)
    return out


def stamp(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + ".%03d" % (int(t * 1000) % 1000)


def report(log, names=None, out=print, last=None):
    """A log as text, oldest first."""
    out("Event log %s: ecat %s at %s, %s, servo period %s us" % (
        log.path, "running" if log.running else "stopped", stamp(log.start), log.config or "?",
        log.period_ns // 1000 if log.period_ns else "?"))
    if log.lost > 0:
        out("(%d older events were overwritten)" % log.lost)
    if log.texts is not None and log.texts_total > len(log.texts):
        out("(%d older messages of the master were overwritten)"
            % (log.texts_total - len(log.texts)))
    rows = merged(log)
    rows = rows[-last:] if last else rows
    texts = annotate(log.events, names)
    marks = {"info": "", "warn": "WARN", "error": "ERROR"}
    for kind, x in rows:
        if kind == "event":
            sev, msg = texts[x.index]
            out("%s %-5s %s" % (stamp(x.time), marks[sev], msg))
        else:
            t, n = x
            out("%s %-5s %s" % (stamp(t.time), marks[t.severity], text_line(t, n)))
    if not rows:
        out("(no events)")


def names_of(cfg):
    return {s.get("position"): s.get("name") for s in (cfg or {}).get("slaves") or []}
