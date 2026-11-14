"""What a slave can do with distributed clocks (DC), from every source there
is, and what to warn about in a configuration.

The sources, most certain first:

- the slave's EtherCAT chip, as the master reports it ("ethercat slaves
  -v": "Distributed clocks: yes, 64 bit", "yes, delay measurement only",
  "no"). A chip without a system time cannot take SYNC0 at all; one with it
  can, but that does not say the device uses it (an EL1008 has a 64-bit
  chip and no DC mode);
- the ESI file of the slave's revision: its operation modes (Dc/OpMode,
  AssignActivate);
- the slave's SII (EEPROM), category DC: the same modes, from the slave
  itself (optional: a missing category is a weaker "no");
- CoE 0x1C32/0x1C33 (ETG.1020), for slaves with CoE: :04 the
  synchronisation types supported, :05 the minimum cycle time.

The bus must have been read: without it nothing is known and nothing is
said. Nothing here forbids a setting; the window and --check warn.
"""

import os
from dataclasses import dataclass, field

from . import tool

# 0x1C32/0x1C33:04 (ETG.1020): bit 0 free run, bit 1 synchronous with the SM
# event, bits 2..4 the DC modes (SYNC0, SYNC1, subordinated)
SYNC_FREE_RUN = 0x0001
SYNC_SM = 0x0002
SYNC_DC = 0x001C
# a minimum cycle time above this is not believed (an ECT60 says 3.9 s)
PLAUSIBLE_MIN_CYCLE_NS = 100_000_000
CLOCK_TEXT = {"64": "DC clock, 64 bit", "32": "DC clock, 32 bit",
              "delay": "delay measurement only: no system time, no SYNC0",
              "none": "no DC", None: "not reported"}


@dataclass
class Mode:
    """An operation mode: free run when assign is 0, else DC."""
    name: str
    desc: str
    assign: int
    sync0_factor: int
    shift: int
    source: str          # "ESI" or "SII"

    @property
    def dc(self):
        return bool(self.assign)

    def label(self):
        text = self.name or "unnamed"
        if self.desc and self.desc.lower() != text.lower():
            text += " (%s)" % self.desc
        return "%s 0x%04X" % (text, self.assign) if self.assign else text


@dataclass
class SyncParams:
    """0x1C32 (outputs) or 0x1C33 (inputs) as read from the slave."""
    index: int
    supported: int = None    # :04
    min_cycle: int = None    # :05, ns


@dataclass
class DcInfo:
    clock: str = None             # tool.dc_clock(): "64", "32", "delay", "none" or None
    sii_modes: list = field(default_factory=list)
    esi_modes: list = None        # None: no ESI file for the slave
    esi_exact: bool = False       # the ESI file is for the slave's revision
    esi_file: str = ""
    sync: list = field(default_factory=list)   # [SyncParams]

    # the modes

    def modes(self):
        """The slave's modes from the best source: an ESI file of its
        revision, else its SII, else an ESI file of another revision."""
        if self.esi_modes is not None and self.esi_exact:
            return list(self.esi_modes)
        if self.sii_modes:
            return list(self.sii_modes)
        return list(self.esi_modes or [])

    def source(self):
        """Where modes() come from, as text."""
        if self.esi_modes is not None and self.esi_exact:
            return "ESI file %s" % self.esi_file
        if self.sii_modes:
            return "the slave's SII"
        if self.esi_modes:
            return "ESI file %s, for another revision" % self.esi_file
        return "the slave's SII" + (" and the ESI file %s" % self.esi_file
                                    if self.esi_modes is not None else "")

    def dc_modes(self):
        """The DC modes (AssignActivate not 0), each assign once."""
        out, seen = [], set()
        for m in self.modes():
            if m.dc and (m.name, m.assign) not in seen:
                seen.add((m.name, m.assign))
                out.append(m)
        return out

    # what the slave says

    @property
    def can_sync(self):
        """False if the chip has no system time (it cannot take SYNC0); True
        if it has one; None if not known."""
        if self.clock in ("none", "delay"):
            return False
        return True if self.clock in ("32", "64") else None

    def coe_supported(self):
        """0x1C32/0x1C33:04 as read, or None."""
        vals = [s.supported for s in self.sync if s.supported is not None]
        return vals[0] if vals else None

    def coe_dc(self):
        """Whether CoE 0x1C3x:04 lists a DC mode, or None."""
        v = self.coe_supported()
        return None if v is None else bool(v & SYNC_DC)

    def free_run_ok(self):
        """False when the slave lists only DC modes, or its CoE lists neither
        free run nor SM synchronous; True or None (not known) otherwise."""
        v = self.coe_supported()
        if v is not None and v & SYNC_DC and not v & (SYNC_FREE_RUN | SYNC_SM):
            return False
        modes = self.modes()
        if modes:
            return any(not m.dc for m in modes)
        return None

    def min_cycle(self):
        """(ns, plausible): the largest minimum cycle time the slave gives, or
        (None, None)."""
        vals = [s.min_cycle for s in self.sync if s.min_cycle]
        if not vals:
            return None, None
        v = max(vals)
        return v, v <= PLAUSIBLE_MIN_CYCLE_NS

    def lines(self):
        """What each source says, for showing."""
        out = ["Chip: %s" % CLOCK_TEXT.get(self.clock, self.clock)]
        out.append("SII: %s" % (_modes_text(self.sii_modes) if self.sii_modes else "no DC category"))
        if self.esi_modes is None:
            out.append("ESI: no file for this slave")
        else:
            out.append("ESI %s%s: %s" % (self.esi_file, "" if self.esi_exact else
                                         " (another revision)", _modes_text(self.esi_modes)
                                         if self.esi_modes else "no DC modes"))
        v = self.coe_supported()
        if v is not None:
            kinds = [n for bit, n in ((SYNC_FREE_RUN, "free run"), (SYNC_SM, "SM synchronous"),
                                      (SYNC_DC, "DC")) if v & bit]
            text = "CoE 0x1C3x:04 = 0x%04X: %s" % (v, ", ".join(kinds) or "none")
            ns, ok = self.min_cycle()
            if ns:
                text += "; minimum cycle %s%s" % (_time(ns), "" if ok else
                                                  " (not plausible, ignored)")
            out.append(text)
        return out


def _time(ns):
    if ns >= 1_000_000_000:
        return "%.3g s" % (ns / 1e9)
    if ns >= 1000:
        return "%g us" % (ns / 1000)
    return "%d ns" % ns


def _modes_text(modes):
    return "; ".join(m.label() for m in modes)


def build(info, sii=None, esi_dev=None, esi_exact=False, sync=()):
    """A DcInfo from what was read: info from tool.slave_info(), sii an
    sii.Sii, esi_dev an esi.EsiDevice (or None)."""
    d = DcInfo(clock=info.get("dc_clock") if info.get("dc_clock") is not None else
               ("64" if info.get("dc") else None))
    if sii is not None:
        d.sii_modes = [Mode(m.name, "", m.assign_activate, m.sync0_cycle_factor, m.shift_time0, "SII")
                       for m in sii.dc_modes]
    if esi_dev is not None:
        d.esi_modes = [Mode(m.name, m.desc, m.assign_activate, m.sync0_factor, m.shift0, "ESI")
                       for m in esi_dev.dc_modes]
        d.esi_exact = bool(esi_exact)
        d.esi_file = os.path.basename(esi_dev.path or "")
    d.sync = list(sync)
    return d


def read_sync(pos, coe):
    """0x1C32/0x1C33 :04 and :05 of a slave with CoE, for the objects its
    dictionary lists (others would make the master log an abort)."""
    out = []
    if not coe:
        return out
    for index in (0x1C32, 0x1C33):
        if index not in coe.objects:
            continue
        s = SyncParams(index)
        if (index, 4) in coe.entries:
            s.supported = tool.upload(pos, index, 4, "uint16")
        if (index, 5) in coe.entries:
            s.min_cycle = tool.upload(pos, index, 5, "uint32")
        out.append(s)
    return out


def parse_assign(value):
    """assign-activate from the file as an int, or None."""
    try:
        return int(str(value).strip(), 16)
    except ValueError:
        return None


def problems(s, d, period_ns=None):
    """[(level, text)] about slave s's DC setting (its dict in the file)
    against what the slave can do (DcInfo d): level "warning" or "note".
    Nothing is forbidden."""
    out = []
    if d is None:
        return out
    dc = s.get("dc")
    if dc is not None:
        assign = parse_assign(dc.get("assign-activate", ""))
        if assign == 0:
            pass        # 0x0000: free run, the same as no DC
        elif d.can_sync is False:
            out.append(("warning", "DC SYNC0 is on, but the slave's EtherCAT chip has %s: it "
                        "will not reach OP with DC (the master says \"Slave seems not to support "
                        "distributed clocks\")" % ("no DC" if d.clock == "none" else
                                                   "no DC clock, only delay measurement")))
        elif not d.dc_modes() and d.coe_dc() is not True:
            out.append(("warning", "DC SYNC0 is on, but the slave describes no DC mode (%s): it "
                        "may refuse OP or run without SYNC0; free run is its mode" % d.source()))
        elif d.dc_modes() and assign is not None and assign not in {m.assign for m in d.dc_modes()}:
            out.append(("warning", "assign-activate 0x%04X is not one of the slave's DC modes: "
                        "%s (%s)" % (assign, _modes_text(d.dc_modes()), d.source())))
        elif d.coe_dc() is False:
            out.append(("warning", "DC SYNC0 is on, but the slave's CoE 0x1C32/0x1C33:04 lists "
                        "no DC mode"))
    elif d.free_run_ok() is False:
        out.append(("warning", "DC is off, but the slave lists only DC modes (%s): it may refuse "
                    "OP without DC (AL 0x0027 \"Freerun not supported\")"
                    % (_modes_text(d.dc_modes()) or "CoE 0x1C3x:04")))
    ns, ok = d.min_cycle()
    if period_ns and ns and ok and period_ns < ns:
        out.append(("warning", "the slave's minimum cycle time is %s (0x1C32/0x1C33:05); the "
                    "servo thread runs every %s: it may refuse OP (AL 0x0035) or miss cycles"
                    % (_time(ns), _time(period_ns))))
    if (d.esi_modes is not None and d.esi_exact and d.sii_modes
            and {m.assign for m in d.esi_modes if m.dc} != {m.assign for m in d.sii_modes if m.dc}):
        out.append(("note", "the slave's SII and its ESI file differ in their DC modes (SII: %s; "
                    "ESI: %s); the ESI file's are offered" % (_modes_text(d.sii_modes),
                                                             _modes_text(d.esi_modes) or "none")))
    return out


def read(pos, esi_devices):
    """A DcInfo of a slave on the bus, for checking a file against it:
    the chip, the SII, the ESI file and CoE 0x1C32/0x1C33."""
    from . import coe as coe_mod, esi, sii as sii_mod
    info = tool.slave_info(pos)
    sii = sii_mod.parse(tool.sii(pos))
    dev, exact = esi.find(esi_devices, sii.vendor_id, sii.product_code, sii.revision)
    coe = coe_mod.Dictionary(tool.sdos(pos)) if sii.has_coe else coe_mod.Dictionary()
    return build(info, sii, dev, exact, read_sync(pos, coe))
