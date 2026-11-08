"""Read an lcec XML configuration (ethercat-conf.xml), to compare with or
import into ecat."""

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field


def _hex(text):
    text = (text or "0").strip()
    return int(text, 16)


@dataclass
class LcecEntry:
    index: int
    subindex: int
    bitlen: int
    pin: str
    hal_type: str
    bits: list = field(default_factory=list)   # complexEntry: (bitlen, pin, type)


@dataclass
class LcecSlave:
    idx: int
    name: str
    type: str
    vendor_id: int
    product_code: int
    assign_activate: int = None
    sync0_cycle: str = None
    sync0_shift: int = None
    sdos: list = field(default_factory=list)      # (index, subindex, bytes)
    pdos: dict = field(default_factory=dict)      # (sm, pdo index) -> [LcecEntry]


@dataclass
class LcecConfig:
    app_time_period: int = None
    slaves: list = field(default_factory=list)


def load(path):
    root = ET.parse(path).getroot()
    cfg = LcecConfig()
    master = root.find("master")
    if master is None:
        return cfg
    if master.get("appTimePeriod"):
        cfg.app_time_period = int(master.get("appTimePeriod"))
    for s in master.findall("slave"):
        slave = LcecSlave(int(s.get("idx")), s.get("name", ""), s.get("type", ""),
                          _hex(s.get("vid")), _hex(s.get("pid")))
        dc = s.find("dcConf")
        if dc is not None:
            slave.assign_activate = _hex(dc.get("assignActivate"))
            slave.sync0_cycle = dc.get("sync0Cycle")
            slave.sync0_shift = int(dc.get("sync0Shift", "0"))
        for sdo in s.findall("sdoConfig"):
            raw = sdo.find("sdoDataRaw")
            data = bytes.fromhex(raw.get("data")) if raw is not None else b""
            slave.sdos.append((_hex(sdo.get("idx")), _hex(sdo.get("subIdx")), data))
        for sm in s.findall("syncManager"):
            for pdo in sm.findall("pdo"):
                entries = []
                for e in pdo.findall("pdoEntry"):
                    entry = LcecEntry(_hex(e.get("idx")), _hex(e.get("subIdx")),
                                      int(e.get("bitLen", "0")), e.get("halPin", ""),
                                      e.get("halType", ""))
                    for c in e.findall("complexEntry"):
                        entry.bits.append((int(c.get("bitLen")), c.get("halPin", ""),
                                           c.get("halType", "")))
                    entries.append(entry)
                slave.pdos[(int(sm.get("idx")), _hex(pdo.get("idx")))] = entries
        cfg.slaves.append(slave)
    return cfg
