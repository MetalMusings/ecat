"""Read ESI files (EtherCAT Slave Information, the vendor's XML) and find the
device description that matches a slave."""

import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from . import coe


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
class EsiPdoSet:
    """A predefined PDO assignment (AlternativeSmMapping), as TwinCAT offers."""
    name: str
    default: bool
    sms: dict            # sync manager -> PDO indexes


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
    pdo_sets: list = field(default_factory=list)


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
    for a in dev.findall("Info/VendorSpecific/TwinCAT/AlternativeSmMapping"):
        sms = {}
        for sm in a.findall("Sm"):
            sms[number(sm.get("No"))] = [number(x.text) for x in sm.findall("Pdo") if x.text]
        d.pdo_sets.append(EsiPdoSet(_text(a, "Name"), a.get("Default") in ("1", "true"), sms))
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


# ESI data type names as the ethercat tool names them (coe.py, propose.COE_TYPES)
_TOOL_TYPES = {"BOOL": "bool", "BIT": "bool", "SINT": "int8", "INT": "int16", "DINT": "int32",
               "LINT": "int64", "USINT": "uint8", "BYTE": "uint8", "UINT": "uint16",
               "WORD": "uint16", "UDINT": "uint32", "DWORD": "uint32", "ULINT": "uint64",
               "REAL": "float", "LREAL": "double"}
_SIGNED = {"int8", "int16", "int32", "int64"}


def _tool_type(name):
    name = (name or "").strip()
    if name.upper().startswith("STRING"):
        return "string"
    if name.upper().startswith("BIT") and name[3:].isdigit():
        return "uint8"
    return _TOOL_TYPES.get(name.upper(), name.lower())


def _access(el):
    """The six-character access string of the tool ("rwrwrw": read and
    write in PREOP, SAFEOP and OP) from an ESI Access element. A
    restriction names the states where it is allowed."""
    if el is None or not (el.text or "").strip():
        return "r-r-r-"
    word = el.text.strip().lower()
    states = ("preop", "safeop", "op")

    def allowed(attr):
        listed = (el.get(attr) or "").lower().replace(",", " ").split()
        return [(not listed) or s in listed for s in states]
    read, write = allowed("ReadRestrictions"), allowed("WriteRestrictions")
    out = ""
    for i in range(3):
        out += "r" if "r" in word and read[i] else "-"
        out += "w" if (word in ("rw", "wo")) and write[i] else "-"
    if word == "wo":
        out = out.replace("r", "-")
    return out


def _value(text):
    try:
        return number(text)
    except (TypeError, ValueError):
        return None


def _default(info, data_type):
    """DefaultValue, else DefaultData (little endian bytes) as a number."""
    if info is None:
        return None, None, None
    d = _value(_text(info, "DefaultValue"))
    if d is None and data_type in _TOOL_TYPES.values() and data_type not in ("float", "double"):
        raw = _text(info, "DefaultData")
        try:
            if raw:
                d = int.from_bytes(bytes.fromhex(raw), "little", signed=data_type in _SIGNED)
        except ValueError:
            d = None
    return _value(_text(info, "MinValue")), _value(_text(info, "MaxValue")), d


_dictionaries = {}


def dictionary(dev):
    """The object dictionary an ESI file gives a device, as a coe.Dictionary
    (with minimum, maximum and default values where the file has them). Empty
    if the file has none or cannot be read."""
    key = (dev.path, dev.product_code, dev.revision)
    if key not in _dictionaries:     # parsing a Beckhoff file takes about a second
        _dictionaries[key] = _read_dictionary(dev)
    return _dictionaries[key]


def _read_dictionary(dev):
    out = coe.Dictionary()
    out.source = "ESI"
    try:
        root = ET.parse(dev.path).getroot()
    except (ET.ParseError, OSError):
        return out
    for el in root.findall("Descriptions/Devices/Device"):
        t = el.find("Type")
        if t is not None and number(t.get("ProductCode")) == dev.product_code and \
                number(t.get("RevisionNo") or "0") == dev.revision:
            break
    else:
        return out
    d = el.find("Profile/Dictionary")
    if d is None:
        return out
    types = {_text(x, "Name"): x for x in d.findall("DataTypes/DataType")}
    for o in d.findall("Objects/Object"):
        try:
            _object(out, o, types)
        except (TypeError, ValueError):
            continue
    return out


def _object(out, o, types):
    index = number(_text(o, "Index"))
    out.objects[index] = _text(o, "Name")
    dt = types.get(_text(o, "Type"))
    infos = o.findall("Info/SubItem")
    object_access = o.find("Flags/Access")
    subs = dt.findall("SubItem") if dt is not None else []
    if dt is not None and dt.find("ArrayInfo") is not None and not subs:
        # an array: sub 0 is the count, then the elements
        base = _tool_type(_text(dt, "BaseType"))
        n = number(_text(dt, "ArrayInfo/Elements") or "0")
        size = number(_text(dt, "BitSize") or "0") // max(n, 1)
        out.entries[(index, 0)] = coe.CoeEntry(index, 0, "r-r-r-", "uint8", 8, "SubIndex 000")
        for i in range(1, n + 1):
            out.entries[(index, i)] = coe.CoeEntry(
                index, i, _access(object_access), base, size, "%s %d" % (_text(o, "Name"), i))
        return
    if not subs:
        t = _tool_type(_text(o, "Type"))
        lo, hi, default = _default(o.find("Info"), t)
        out.entries[(index, 0)] = coe.CoeEntry(
            index, 0, _access(object_access), t, number(_text(o, "BitSize") or "0"),
            _text(o, "Name"), lo, hi, default)
        return
    by_name = {}
    for x in infos:
        by_name.setdefault(_text(x, "Name"), x)
    for i, sub in enumerate(subs):
        name = _text(sub, "Name")
        t = _tool_type(_text(sub, "Type"))
        info = infos[i] if len(infos) == len(subs) else by_name.get(name)
        lo, hi, default = _default(info.find("Info") if info is not None else None, t)
        access = _access(sub.find("Flags/Access"))
        out.entries[(index, number(_text(sub, "SubIdx") or "0"))] = coe.CoeEntry(
            index, number(_text(sub, "SubIdx") or "0"), access, t,
            number(_text(sub, "BitSize") or "0"), name, lo, hi, default)
