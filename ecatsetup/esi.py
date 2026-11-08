"""Read ESI files (EtherCAT Slave Information, the vendor's XML) and find the
device description that matches a slave."""

import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field


def number(text):
    """ESI numbers are decimal or "#x" hexadecimal."""
    if text is None:
        return None
    text = text.strip()
    if text.lower().startswith("#x"):
        return int(text[2:], 16)
    return int(text, 0) if text.lower().startswith("0x") else int(text)


@dataclass
class EsiEntry:
    index: int
    subindex: int
    bitlen: int
    name: str
    data_type: str


@dataclass
class EsiPdo:
    index: int
    direction: str       # "rx" or "tx"
    sm: int              # assigned by default to this SM, or None
    fixed: bool
    mandatory: bool
    name: str
    entries: list = field(default_factory=list)
    excludes: list = field(default_factory=list)


@dataclass
class EsiDcMode:
    name: str
    desc: str
    assign_activate: int
    sync0_factor: int
    shift0: int


@dataclass
class EsiDevice:
    path: str
    vendor_id: int
    product_code: int
    revision: int
    type_name: str
    name: str
    profile: int = None
    pdos: list = field(default_factory=list)
    dc_modes: list = field(default_factory=list)
    init_cmds: list = field(default_factory=list)   # (transition, index, subindex, data hex)


def _text(el, path):
    found = el.find(path)
    return found.text.strip() if found is not None and found.text else ""


def _device(path, vendor_id, dev):
    t = dev.find("Type")
    d = EsiDevice(path, vendor_id, number(t.get("ProductCode")),
                  number(t.get("RevisionNo") or "0"), (t.text or "").strip(),
                  _text(dev, "Name"))
    prof = dev.find("Profile/ProfileNo")
    if prof is not None and prof.text:
        d.profile = number(prof.text)
    for tag, direction in (("RxPdo", "rx"), ("TxPdo", "tx")):
        for p in dev.findall(tag):
            pdo = EsiPdo(number(_text(p, "Index")), direction,
                         number(p.get("Sm")) if p.get("Sm") else None,
                         p.get("Fixed") in ("1", "true"),
                         p.get("Mandatory") in ("1", "true"), _text(p, "Name"))
            for e in p.findall("Entry"):
                pdo.entries.append(EsiEntry(number(_text(e, "Index")),
                                            number(_text(e, "SubIndex") or "0"),
                                            number(_text(e, "BitLen")),
                                            _text(e, "Name"), _text(e, "DataType")))
            pdo.excludes = [number(x.text) for x in p.findall("Exclude") if x.text]
            d.pdos.append(pdo)
    for m in dev.findall("Dc/OpMode"):
        c0 = m.find("CycleTimeSync0")
        d.dc_modes.append(EsiDcMode(
            _text(m, "Name"), _text(m, "Desc"),
            number(_text(m, "AssignActivate") or "0"),
            number(c0.get("Factor")) if c0 is not None and c0.get("Factor") else 1,
            number(_text(m, "ShiftTimeSync0") or "0")))
    for c in dev.findall("Mailbox/CoE/InitCmd"):
        d.init_cmds.append((",".join(t.text for t in c.findall("Transition")),
                            number(_text(c, "Index")), number(_text(c, "SubIndex") or "0"),
                            _text(c, "Data")))
    return d


def load_file(path):
    """All device descriptions in one ESI file. Broken files give none."""
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return []
    if root.tag != "EtherCATInfo":
        return []
    vendor = number(_text(root, "Vendor/Id") or "0")
    devices = []
    for dev in root.findall("Descriptions/Devices/Device"):
        try:
            devices.append(_device(path, vendor, dev))
        except (TypeError, ValueError, AttributeError):
            continue
    return devices


def load_dirs(paths):
    """All device descriptions in the given files and folders (recursively)."""
    devices = []
    for p in paths:
        if os.path.isdir(p):
            for dirpath, _dirs, files in os.walk(p):
                for f in files:
                    if f.lower().endswith(".xml"):
                        devices.extend(load_file(os.path.join(dirpath, f)))
        else:
            devices.extend(load_file(p))
    return devices


def find(devices, vendor_id, product_code, revision):
    """The best description for a slave: exact revision, else the same product.

    Returns (device or None, exact match).
    """
    same = [d for d in devices if d.vendor_id == vendor_id and d.product_code == product_code]
    for d in same:
        if d.revision == revision:
            return d, True
    if same:
        return max(same, key=lambda d: d.revision), False
    return None, False
