"""Diagnose the slaves on the bus: why a slave is not in OP, which cable
or connector has errors, what the slave says about itself, and what a
CiA 402 drive is missing to run.

Everything here only reads (ESC registers, SDO uploads), so it can run
against a free-running master or against the master inside a running
LinuxCNC.
"""

import time
from dataclasses import dataclass, field

from . import tool

AL_STATES = {1: "INIT", 2: "PREOP", 3: "BOOT", 4: "SAFEOP", 8: "OP"}

# AL status codes (ETG.1000.6), with what usually lies behind them.
AL_CODES = {
    0x0000: ("No error", ""),
    0x0001: ("Unspecified error", ""),
    0x0002: ("No memory", ""),
    0x0011: ("Invalid requested state change",
             "often a follow-up of an earlier error; restart the master"),
    0x0012: ("Unknown requested state", ""),
    0x0013: ("Bootstrap not supported", ""),
    0x0014: ("No valid firmware", "the slave's firmware must be loaded again"),
    0x0015: ("Invalid mailbox configuration (BOOT)", ""),
    0x0016: ("Invalid mailbox configuration", "the SII's mailbox settings are wrong"),
    0x0017: ("Invalid sync manager configuration",
             "the process data does not fit the slave: check the mapping, or use "
             "configure-pdos: false"),
    0x0018: ("No valid inputs available", ""),
    0x0019: ("No valid outputs", ""),
    0x001A: ("Synchronization error", "check the slave's DC settings"),
    0x001B: ("Sync manager watchdog",
             "the process data stopped coming: LinuxCNC stopped, or cycles were missed"),
    0x001C: ("Invalid sync manager types", ""),
    0x001D: ("Invalid output configuration", "the output mapping is not accepted"),
    0x001E: ("Invalid input configuration", "the input mapping is not accepted"),
    0x001F: ("Invalid watchdog configuration", ""),
    0x0020: ("Slave needs cold start", "switch the slave's power off and on"),
    0x0021: ("Slave needs INIT", ""),
    0x0022: ("Slave needs PREOP", ""),
    0x0023: ("Slave needs SAFEOP", ""),
    0x0024: ("Invalid input mapping", "the input mapping is not accepted"),
    0x0025: ("Invalid output mapping", "the output mapping is not accepted"),
    0x0026: ("Inconsistent settings", "a setting (SDO) does not fit the others"),
    0x0027: ("Freerun not supported", "the slave needs distributed clocks: give it dc"),
    0x0028: ("Synchronization not supported", "the slave cannot run with this dc setting"),
    0x0029: ("Freerun needs 3 buffer mode", ""),
    0x002A: ("Background watchdog", ""),
    0x002B: ("No valid inputs and outputs", ""),
    0x002C: ("Fatal sync error", "SYNC0 stopped or came at the wrong time; check DC"),
    0x002D: ("No sync error", "SYNC0 does not come: check dc assign-activate"),
    0x0030: ("Invalid DC SYNC configuration",
             "the slave does not accept the dc settings: assign-activate, sync0 cycle"),
    0x0031: ("Invalid DC latch configuration", ""),
    0x0032: ("PLL error", "the slave's clock does not follow; check the DC settings"),
    0x0033: ("DC sync IO error", ""),
    0x0034: ("DC sync timeout error", ""),
    0x0035: ("DC invalid sync cycle time", "the slave does not support this cycle time"),
    0x0036: ("DC sync0 cycle time", "the slave does not support this SYNC0 cycle"),
    0x0037: ("DC sync1 cycle time", ""),
    0x0041: ("Mailbox AoE", ""),
    0x0042: ("Mailbox EoE", ""),
    0x0043: ("Mailbox CoE", "a start-up SDO failed: check the settings"),
    0x0044: ("Mailbox FoE", ""),
    0x0045: ("Mailbox SoE", ""),
    0x004F: ("Mailbox VoE", ""),
    0x0050: ("EEPROM no access", ""),
    0x0051: ("EEPROM error", "the slave's SII is damaged"),
    0x0060: ("Slave restarted locally", "the slave lost power or reset itself"),
}

# 0x603F error code classes (CiA 301/402).
ERROR_CLASSES = [
    (0x2000, "current"), (0x3000, "voltage"), (0x4000, "temperature"),
    (0x5000, "device hardware"), (0x6000, "device software"), (0x7000, "additional modules"),
    (0x8000, "monitoring"), (0x9000, "external error"), (0xF000, "additional functions"),
    (0xFF00, "device specific"),
]
ERROR_CODES = {
    0x2310: "continuous over-current", 0x2320: "short circuit / earth leakage",
    0x3110: "mains over-voltage", 0x3120: "mains under-voltage", 0x3210: "DC link over-voltage",
    0x3220: "DC link under-voltage", 0x4210: "excess temperature, device",
    0x4310: "excess temperature, drive", 0x5112: "supply low voltage",
    0x7121: "motor blocked", 0x7300: "sensor (encoder)", 0x7305: "incremental sensor 1 fault",
    0x7500: "communication", 0x8110: "CAN overrun", 0x8400: "velocity speed controller",
    0x8611: "following error", 0x8612: "reference limit", 0x8700: "sync controller",
}

CIA402_STATES = [
    (0x4F, 0x00, "Not ready to switch on", "the drive is starting or testing itself"),
    (0x4F, 0x40, "Switch on disabled",
     "waiting for Shutdown (control word 0x06): enable is not set, or a fault was just reset"),
    (0x6F, 0x21, "Ready to switch on", "waiting for Switch on (control word 0x07): enable is not set"),
    (0x6F, 0x23, "Switched on", "waiting for Enable operation (0x0F), which the profile sends "
                                "only in the configured mode"),
    (0x6F, 0x27, "Operation enabled", ""),
    (0x6F, 0x07, "Quick stop active", "a quick stop was requested"),
    (0x4F, 0x0F, "Fault reaction active", "the drive is stopping after a fault"),
    (0x4F, 0x08, "Fault", "see the error code; reset with fault-reset"),
]
MODES = {0: "none", 1: "PP", 3: "PV", 4: "TQ", 6: "HM", 7: "IP", 8: "CSP", 9: "CSV", 10: "CST"}


def reg_read(pos, address, length):
    out = tool.run("reg_read", "-p", pos, "0x%04x" % address, length)
    return bytes(int(b, 16) for b in out.split())


def counters(pos):
    """Error counters 0x0300-0x0313: per port (rx, phy, forwarded, lost link)."""
    r = reg_read(pos, 0x0300, 0x14)
    return [{"rx": r[2 * p], "phy": r[2 * p + 1], "fwd": r[0x08 + p], "lost": r[0x10 + p]}
            for p in range(4)]


def dl_status(pos):
    """Link and communication per port, from 0x0110."""
    v = int.from_bytes(reg_read(pos, 0x0110, 2), "little")
    return [{"link": bool(v >> (4 + p) & 1), "comm": bool(v >> (9 + 2 * p) & 1)} for p in range(4)]


def al_status(pos):
    st = reg_read(pos, 0x0130, 2)[0]
    code = int.from_bytes(reg_read(pos, 0x0134, 2), "little")
    return st & 0x0F, bool(st & 0x10), code


def cia402_state(sw):
    for mask, value, name, hint in CIA402_STATES:
        if sw & mask == value:
            return name, hint
    return "unknown", ""


def error_text(code):
    if code == 0:
        return "no error"
    name = ERROR_CODES.get(code)
    cls = next((n for base, n in reversed(ERROR_CLASSES) if code >= base), "")
    return "0x%04X %s" % (code, name or ("(%s)" % cls if cls else ""))


@dataclass
class Port:
    number: int
    link: bool
    now: dict           # counters since power-on: rx, phy, fwd, lost
    delta: dict         # how much they grew while watched

    @property
    def grew(self):
        """Counters that grew, other than forwarded errors (which only say
        that the fault is further back)."""
        return [k for k in ("rx", "phy", "lost") if self.delta[k]]

    @property
    def shown(self):
        return self.link or any(self.now.values())


@dataclass
class Cia402:
    statusword: int = None
    state: str = ""
    hint: str = ""
    mode: object = None             # 0x6061
    requested: object = None        # 0x6060
    error_code: int = None          # 0x603F


@dataclass
class Report:
    """What was found on one slave."""
    position: int
    name: str
    flagged: bool                   # the master's error flag in the slave list
    state: int                      # AL state: 1, 2, 4, 8
    error: bool                     # AL error indication
    code: int                       # AL status code
    last_code: int = None           # kept by the master when the slave's was cleared
    ports: list = field(default_factory=list)
    seconds: float = 0
    history: list = field(default_factory=list)   # the slave's diagnosis history (0x10F3)
    error_register: int = None      # 0x1001
    cia402: Cia402 = None

    @property
    def state_name(self):
        return AL_STATES.get(self.state, "0x%X" % self.state)


def examine(positions, seconds=2.0):
    """Read the slaves and return a Report for each."""
    slaves = tool.slave_list()
    first = {pos: counters(pos) for pos in positions}
    if seconds > 0:
        time.sleep(seconds)
    reports = []
    for pos in positions:
        name, flagged = slaves.get(pos, ("", False))
        state, err, code = al_status(pos)
        r = Report(pos, name, flagged, state, err, code, seconds=seconds)
        if flagged and not err and state != 8:
            r.last_code = tool.slave_info(pos).get("al_status_code")
        dl, now, then = dl_status(pos), counters(pos), first[pos]
        for p in range(4):
            r.ports.append(Port(p, dl[p]["link"], now[p],
                                {k: (now[p][k] - then[p][k]) % 256 for k in now[p]}))
        diag = _try(lambda: tool.run("diag", "-p", pos))
        if diag and "does not provide" not in diag:
            r.history = diag.strip().splitlines()
        r.error_register = tool.upload(pos, 0x1001, 0, "uint8")
        dt = tool.upload(pos, 0x1000, 0, "uint32")
        if dt is not None and dt & 0xFFFF == 402:
            c = r.cia402 = Cia402()
            c.statusword = tool.upload(pos, 0x6041, 0, "uint16")
            if c.statusword is not None:
                c.state, c.hint = cia402_state(c.statusword)
                c.mode = tool.upload(pos, 0x6061, 0, "int8")
                c.requested = tool.upload(pos, 0x6060, 0, "int8")
                c.error_code = tool.upload(pos, 0x603F, 0, "uint16")
        reports.append(r)
    return reports


def cia402_findings(c):
    """(problems, lines) about a drive."""
    if c.statusword is None:
        return 1, ["CiA 402: the status word 0x6041 cannot be read"]
    sw, lines, problems = c.statusword, [], 0
    lines.append("CiA 402: %s (status word 0x%04X), mode %s%s" % (
        c.state, sw, MODES.get(c.mode, c.mode),
        "" if c.requested in (None, c.mode) else ", requested %s" % MODES.get(c.requested, c.requested)))
    if c.hint:
        lines.append("  -> %s" % c.hint)
    if c.error_code:
        problems += 1
        lines.append("Error code 0x603F: %s" % error_text(c.error_code))
    if not sw & (1 << 4) and c.state in ("Switched on", "Operation enabled"):
        problems += 1
        lines.append("  -> no voltage on the power stage (status word bit 4): check the main power")
    if not sw & (1 << 9):
        problems += 1
        lines.append("  -> not remote (bit 9): the drive is set to local control, the bus cannot run it")
    if sw & (1 << 7):
        lines.append("  -> warning bit set (bit 7)")
    if c.state == "Fault":
        problems += 1
    return problems, lines


def diagnose(positions, seconds=2.0, out=print):
    """Report on the slaves; return the number of problems found."""
    problems = 0
    reports = examine(positions, seconds)
    for r in reports:
        out("Slave %d: %s" % (r.position, r.name))

        # application layer state
        text, hint = AL_CODES.get(r.code, ("unknown code", ""))
        line = "  State: %s" % r.state_name
        if r.error or r.state != 8:
            problems += 1
            line += ", error 0x%04X: %s" % (r.code, text) if r.error else " (not OP)"
        out(line)
        if r.error and hint:
            out("    -> %s" % hint)
        elif r.flagged and not r.error and r.state != 8:
            if r.last_code:
                text, hint = AL_CODES.get(r.last_code, ("unknown code", ""))
                out("    -> refused with 0x%04X: %s (kept by the master; the slave's own code was "
                    "cleared when the master acknowledged the error)" % (r.last_code, text))
                if hint:
                    out("    -> %s" % hint)
            else:
                out("    -> the slave refused a state change and the master stopped it; acknowledging"
                    " the error cleared the slave's code, so it is only in the master's log"
                    " (\"AL status message 0x....\")")

        # links and error counters
        for port in r.ports:
            if not port.shown:
                continue
            total = ", ".join("%s %s" % (k, _count(port.now[k])) for k in ("rx", "phy", "lost")
                              if port.now[k])
            out("  Port %d: %s%s" % (port.number, "link" if port.link else "no link",
                                     "; since power-on: " + total if total else ""))
            if port.grew:
                problems += 1
                out("    -> %s increased by %s in %g s: the cable or connector at this port, "
                    "or the device on its other end"
                    % (" and ".join(port.grew), "/".join(str(port.delta[k]) for k in port.grew),
                       seconds))
            elif port.delta["fwd"]:
                out("    -> forwarded errors increased: the fault is before this slave")

        # the slave's own messages
        if r.history:
            out("  Diagnosis history (0x10F3):")
            for line in r.history[-10:]:
                out("    " + line)
        if r.error_register:
            problems += 1
            out("  Error register 0x1001: 0x%02X" % r.error_register)

        if r.cia402 is not None:
            n, lines = cia402_findings(r.cia402)
            problems += n
            for line in lines:
                out("  " + line)
        out("")
    for line in lost_link_notes(reports):
        out(line)
    return problems


def lost_link_notes(reports):
    """Notes on cables that lost their link since power-on: the same count of
    lost links (not a saturated one) on the ports at both ends of a cable.
    Not problems by themselves: the counters are never cleared by the master."""
    notes = []
    by_pos = {r.position: r for r in reports}
    for r in reports:
        nxt = by_pos.get(r.position + 1)
        if nxt is None:
            continue
        for a in r.ports:
            for b in nxt.ports:
                if 0 < a.now["lost"] < 255 and 0 < b.now["lost"] < 255 and a.now["lost"] == b.now["lost"] \
                        and a.number != 0 and b.number == 0:
                    notes.append("Note: the link was lost %d time%s since power-on between slave %d port %d "
                                 "and slave %d port %d: that cable was disconnected, loose or the slave "
                                 "behind it lost power (the counters are not cleared; a cable "
                                 "pulled on purpose counts too)"
                                 % (a.now["lost"], "" if a.now["lost"] == 1 else "s", r.position, a.number,
                                    nxt.position, b.number))
    return notes


def _count(v):
    return "255+" if v == 255 else str(v)


def _try(f):
    try:
        return f()
    except tool.ToolError:
        return None
