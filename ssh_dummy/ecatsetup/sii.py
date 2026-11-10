"""Parse a slave's SII (EEPROM) contents, as read with "ethercat sii_read".

Layout from ETG.2010: a fixed header of 64 words, then categories, each a
type word, a size word (in words) and the data.
"""

import struct
from dataclasses import dataclass, field

CAT_STRINGS = 10
CAT_GENERAL = 30
CAT_FMMU = 40
CAT_SYNCM = 41
CAT_TXPDO = 50
CAT_RXPDO = 51
CAT_DC = 60
CAT_END = 0xFFFF

# SyncM types in the SII.
SM_TYPES = {0: "unused", 1: "mailbox out", 2: "mailbox in", 3: "outputs", 4: "inputs"}

# Data type codes (CoE base types) used in SII PDO entries.
DATA_TYPES = {
    0x01: "BOOL", 0x02: "SINT", 0x03: "INT", 0x04: "DINT", 0x05: "USINT",
    0x06: "UINT", 0x07: "UDINT", 0x08: "REAL", 0x09: "VISIBLE_STRING",
    0x0A: "OCTET_STRING", 0x11: "LREAL", 0x15: "LINT", 0x1B: "ULINT",
}


@dataclass
class SyncManager:
    index: int
    start: int
    length: int
    control: int
    enable: int
    type: str


@dataclass
class PdoEntry:
    index: int
    subindex: int
    name: str
    data_type: str
    bitlen: int


@dataclass
class Pdo:
    index: int
    direction: str          # "rx" (outputs) or "tx" (inputs)
    sm: int                 # sync manager, or None if not assigned
    name: str
    fixed: bool
    mandatory: bool
    entries: list = field(default_factory=list)


@dataclass
class DcMode:
    name: str
    assign_activate: int
    cycle_time0: int
    shift_time0: int
    sync0_cycle_factor: int


@dataclass
class Sii:
    vendor_id: int
    product_code: int
    revision: int
    serial: int
    mailbox_protocols: int
    name: str = ""
    order: str = ""
    group: str = ""
    coe_details: int = 0
    sync_managers: list = field(default_factory=list)
    pdos: list = field(default_factory=list)
    dc_modes: list = field(default_factory=list)

    @property
    def has_coe(self):
        return bool(self.mailbox_protocols & 0x04)


def _string(strings, idx):
    """SII strings are numbered from 1; 0 means none."""
    return strings[idx - 1] if 0 < idx <= len(strings) else ""


def parse(data):
    """Parse SII contents (bytes) into an Sii object."""
    if len(data) < 128:
        raise ValueError("SII too short: %d bytes" % len(data))
    words = struct.unpack_from("<64H", data)
    sii = Sii(
        vendor_id=words[0x08] | words[0x09] << 16,
        product_code=words[0x0A] | words[0x0B] << 16,
        revision=words[0x0C] | words[0x0D] << 16,
        serial=words[0x0E] | words[0x0F] << 16,
        mailbox_protocols=words[0x1C],
    )

    # First pass: collect the categories; strings are needed by the others.
    cats = []
    off = 128
    while off + 4 <= len(data):
        ctype, size = struct.unpack_from("<HH", data, off)
        if ctype == CAT_END:
            break
        cats.append((ctype, data[off + 4:off + 4 + size * 2]))
        off += 4 + size * 2

    strings = []
    for ctype, body in cats:
        if ctype == CAT_STRINGS and body:
            pos = 1
            for _ in range(body[0]):
                n = body[pos]
                strings.append(body[pos + 1:pos + 1 + n].decode("latin-1"))
                pos += 1 + n

    for ctype, body in cats:
        if ctype == CAT_GENERAL and len(body) >= 6:
            sii.group = _string(strings, body[0])
            sii.order = _string(strings, body[2])
            sii.name = _string(strings, body[3])
            sii.coe_details = body[5]
        elif ctype == CAT_SYNCM:
            for i in range(len(body) // 8):
                start, length, control, _status, enable, smtype = \
                    struct.unpack_from("<HHBBBB", body, i * 8)
                sii.sync_managers.append(SyncManager(
                    i, start, length, control, enable,
                    SM_TYPES.get(smtype, "type %d" % smtype)))
        elif ctype in (CAT_TXPDO, CAT_RXPDO):
            pos = 0
            while pos + 8 <= len(body):
                index, n, sm, _sync, name_idx, flags = \
                    struct.unpack_from("<HBBBBH", body, pos)
                pos += 8
                pdo = Pdo(index, "tx" if ctype == CAT_TXPDO else "rx",
                          None if sm == 0xFF else sm, _string(strings, name_idx),
                          fixed=bool(flags & 0x0010),
                          mandatory=bool(flags & 0x0001))
                for _ in range(n):
                    e_index, e_sub, e_name, e_type, e_bits, _f = \
                        struct.unpack_from("<HBBBBH", body, pos)
                    pos += 8
                    pdo.entries.append(PdoEntry(
                        e_index, e_sub, _string(strings, e_name),
                        DATA_TYPES.get(e_type, "type 0x%02x" % e_type), e_bits))
                sii.pdos.append(pdo)
        elif ctype == CAT_DC:
            for i in range(len(body) // 24):
                (cycle0, shift0, _shift1, _s1f, assign, s0f, name_idx,
                 _desc) = struct.unpack_from("<IIIhHhBB", body, i * 24)
                sii.dc_modes.append(DcMode(_string(strings, name_idx), assign,
                                           cycle0, shift0, s0f))
    return sii
