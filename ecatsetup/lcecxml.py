"""Read an lcec XML configuration (ethercat-conf.xml), to compare with or
import into ecat."""

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field


def _hex(text):
    text = (text or "0").strip()
    return int(text, 16)


def _float(text):
    return float(text) if text not in (None, "") else None


@dataclass
class LcecBits:
    bitlen: int
    pin: str
    hal_type: str
    scale: float = None
    offset: float = None


@dataclass
class LcecEntry:
    index: int
    subindex: int
    bitlen: int
    pin: str
    hal_type: str
    scale: float = None
    offset: float = None
    bits: list = field(default_factory=list)   # complexEntry: [LcecBits]


@dataclass
class LcecPdo:
    sm: int
    direction: str          # "out" or "in"
    index: int
    entries: list = field(default_factory=list)


@dataclass
class LcecSlave:
    idx: int
    name: str
    type: str
    vendor_id: int
    product_code: int
    config_pdos: bool = False
    assign_activate: int = None
    sync0_cycle: str = None
    sync0_shift: int = None
    sdos: list = field(default_factory=list)      # (index, subindex or "complete", bytes)
    pdo_list: list = field(default_factory=list)  # [LcecPdo], in file order
    unsupported: list = field(default_factory=list)  # element names not imported

    @property
    def pdos(self):
        """(sm, pdo index) -> [LcecEntry]."""
        return {(p.sm, p.index): p.entries for p in self.pdo_list}


@dataclass
class LcecConfig:
    app_time_period: int = None
    ref_clock_sync_cycles: int = None
    sync_to_ref_clock: bool = None
    global_shift_time: int = None
    interface: str = None
    transport: str = None
    slaves: list = field(default_factory=list)


def _entry(e):
    entry = LcecEntry(_hex(e.get("idx")), _hex(e.get("subIdx")), int(e.get("bitLen", "0")),
                      e.get("halPin", ""), e.get("halType", ""),
                      _float(e.get("scale")), _float(e.get("offset")))
    for c in e.findall("complexEntry"):
        entry.bits.append(LcecBits(int(c.get("bitLen")), c.get("halPin", ""), c.get("halType", ""),
                                   _float(c.get("scale")), _float(c.get("offset"))))
    return entry


def load(path):
    root = ET.parse(path).getroot()
    cfg = LcecConfig()
    master = root.find("master")
    if master is None:
        return cfg
    if master.get("appTimePeriod"):
        cfg.app_time_period = int(master.get("appTimePeriod"))
    if master.get("refClockSyncCycles"):
        cfg.ref_clock_sync_cycles = int(master.get("refClockSyncCycles"))
    if master.get("syncToRefClock"):
        cfg.sync_to_ref_clock = master.get("syncToRefClock").lower() == "true"
    if master.get("globalShiftTime"):
        shift = master.get("globalShiftTime")
        cfg.global_shift_time = "auto" if shift.lower() == "auto" else int(shift)
    cfg.interface = master.get("interface")
    cfg.transport = master.get("transport")
    for s in master.findall("slave"):
        slave = LcecSlave(int(s.get("idx")), s.get("name", ""), s.get("type", ""),
                          _hex(s.get("vid")), _hex(s.get("pid")),
                          (s.get("configPdos") or "").lower() == "true")
        dc = s.find("dcConf")
        if dc is not None:
            slave.assign_activate = _hex(dc.get("assignActivate"))
            slave.sync0_cycle = dc.get("sync0Cycle")
            slave.sync0_shift = int(dc.get("sync0Shift", "0"))
        for sdo in s.findall("sdoConfig"):
            raw = sdo.find("sdoDataRaw")
            data = bytes.fromhex(raw.get("data")) if raw is not None else b""
            sub = sdo.get("subIdx", "0")
            slave.sdos.append((_hex(sdo.get("idx")),
                               "complete" if sub.lower() == "complete" else _hex(sub), data))
        for sm in s.findall("syncManager"):
            for pdo in sm.findall("pdo"):
                slave.pdo_list.append(LcecPdo(int(sm.get("idx")), sm.get("dir", ""),
                                              _hex(pdo.get("idx")),
                                              [_entry(e) for e in pdo.findall("pdoEntry")]))
        for child in s:
            if child.tag in ("initCmds", "modParam", "idnConfig", "watchdog"):
                slave.unsupported.append(child.tag)
        cfg.slaves.append(slave)
    return cfg
