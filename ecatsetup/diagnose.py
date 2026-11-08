"""Diagnose the slaves on the bus: why a slave is not in OP, which cable
or connector has errors, what the slave says about itself, and what a
CiA 402 drive is missing to run.

Everything here only reads (ESC registers, SDO uploads), so it can run
against a free-running master or against the master inside a running
LinuxCNC.
"""

import time

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


def diagnose(positions, seconds=2.0, out=print):
    """Report on the slaves; return the number of problems found."""
    problems = 0
    slaves = tool.slave_list()
    first = {pos: counters(pos) for pos in positions}
    if seconds > 0:
        time.sleep(seconds)

    for pos in positions:
        name, flagged = slaves.get(pos, ("", False))
        out("Slave %d: %s" % (pos, name))

        # application layer state
        state, err, code = al_status(pos)
        text, hint = AL_CODES.get(code, ("unknown code", ""))
        line = "  State: %s" % AL_STATES.get(state, "0x%X" % state)
        if err or state != 8:
            problems += 1
            line += ", error 0x%04X: %s" % (code, text) if err else " (not OP)"
        out(line)
        if err and hint:
            out("    -> %s" % hint)
        elif flagged and not err and state != 8:
            last = tool.slave_info(pos).get("al_status_code")
            if last:
                text, hint = AL_CODES.get(last, ("unknown code", ""))
                out("    -> refused with 0x%04X: %s (kept by the master; the slave's own code was "
                    "cleared when the master acknowledged the error)" % (last, text))
                if hint:
                    out("    -> %s" % hint)
            else:
                out("    -> the slave refused a state change and the master stopped it; acknowledging"
                    " the error cleared the slave's code, so it is only in the master's log"
                    " (\"AL status message 0x....\")")

        # links and error counters
        dl, now, then = dl_status(pos), counters(pos), first[pos]
        for p in range(4):
            if not dl[p]["link"] and not any(now[p].values()):
                continue
            delta = {k: (now[p][k] - then[p][k]) % 256 for k in now[p]}
            # forwarded errors only say that the fault is further back
            total = ", ".join("%s %s" % (k, _count(now[p][k])) for k in ("rx", "phy", "lost")
                              if now[p][k])
            line = "  Port %d: %s%s" % (p, "link" if dl[p]["link"] else "no link",
                                         "; since power-on: " + total if total else "")
            out(line)
            grew = [k for k in ("rx", "phy", "lost") if delta[k]]
            if grew:
                problems += 1
                out("    -> %s increased by %s in %g s: the cable or connector at this port, "
                    "or the device on its other end"
                    % (" and ".join(grew), "/".join(str(delta[k]) for k in grew), seconds))
            elif delta["fwd"]:
                out("    -> forwarded errors increased: the fault is before this slave")

        # the slave's own messages
        diag = _try(lambda: tool.run("diag", "-p", pos))
        if diag and "does not provide" not in diag:
            out("  Diagnosis history (0x10F3):")
            for line in diag.strip().splitlines()[-10:]:
                out("    " + line)
        er = tool.upload(pos, 0x1001, 0, "uint8")
        if er:
            problems += 1
            out("  Error register 0x1001: 0x%02X" % er)

        # CiA 402
        dt = tool.upload(pos, 0x1000, 0, "uint32")
        if dt is not None and dt & 0xFFFF == 402:
            problems += _cia402(pos, out)
        out("")
    return problems


def _cia402(pos, out):
    sw = tool.upload(pos, 0x6041, 0, "uint16")
    if sw is None:
        out("  CiA 402: the status word 0x6041 cannot be read")
        return 1
    name, hint = cia402_state(sw)
    md = tool.upload(pos, 0x6061, 0, "int8")
    want = tool.upload(pos, 0x6060, 0, "int8")
    ec = tool.upload(pos, 0x603F, 0, "uint16")
    out("  CiA 402: %s (status word 0x%04X), mode %s%s" % (
        name, sw, MODES.get(md, md), "" if want in (None, md) else ", requested %s" % MODES.get(want, want)))
    problems = 0
    if hint:
        out("    -> %s" % hint)
    if ec:
        problems += 1
        out("  Error code 0x603F: %s" % error_text(ec))
    if not sw & (1 << 4) and name in ("Switched on", "Operation enabled"):
        problems += 1
        out("    -> no voltage on the power stage (status word bit 4): check the main power")
    if not sw & (1 << 9):
        problems += 1
        out("    -> not remote (bit 9): the drive is set to local control, the bus cannot run it")
    if sw & (1 << 7):
        out("    -> warning bit set (bit 7)")
    if name == "Fault":
        problems += 1
    return problems


def _count(v):
    return "255+" if v == 255 else str(v)


def _try(f):
    try:
        return f()
    except tool.ToolError:
        return None
