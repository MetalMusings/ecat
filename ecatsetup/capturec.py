"""Fake slaves for the component's C tests from a capture (capture.py).

header() writes a C header for component/tests: each slave's identity
and its process data as the slave has it assigned (`pdos`), as the
fake_entry_t lists fake_slave() takes, and, when the capture has frames
of a bus in SAFEOP or OP, the process data of each cycle as the frames
carried it. The frames are mapped to the slaves' entries through the
FMMU and sync manager registers in the capture (0x0600, 0x0800): the
logical address an FMMU maps, the sync manager it maps, and the entries
of the PDOs assigned to that sync manager in order.

`ecatsetup --capture-c CAPTURE [--c-name NAME]` prints the header.
"""

import json
import re
import struct

from . import capture

_SM = re.compile(r"^SM(\d+): PhysAddr 0x([0-9a-f]+)", re.I)
_PDO = re.compile(r"^\s+(Rx|Tx)PDO 0x([0-9a-f]+)", re.I)
_ENTRY = re.compile(r"^\s+PDO entry 0x([0-9a-f]{4}):([0-9a-f]{2}),\s*(\d+) bit, \"(.*)\"", re.I)

LOGICAL_CMDS = {10, 11, 12}   # LRD, LWR, LRW
ETHERTYPE = 0x88A4


def parse_pdos(text):
    """The PDO assignment from "pdos": [(sm, direction "out"/"in" or None,
    [(index, subindex, bits, name)])], entries in process data order
    (index 0 is a gap)."""
    sms = []
    for line in text.splitlines():
        m = _SM.match(line)
        if m:
            sms.append([int(m.group(1)), None, []])
            continue
        m = _PDO.match(line)
        if m and sms:
            sms[-1][1] = "out" if m.group(1).lower() == "rx" else "in"
            continue
        m = _ENTRY.match(line)
        if m and sms:
            sms[-1][2].append((int(m.group(1), 16), int(m.group(2), 16), int(m.group(3)),
                               m.group(4).strip()))
    return [tuple(s) for s in sms if s[2]]


def parse_regs(text):
    """reg_read output ("0x98 0x01 ...") as bytes."""
    return bytes(int(x, 16) for x in text.split())


def fmmus(regs):
    """Active FMMUs from the registers from 0x0000: [(logical start, length,
    physical start, type)], type 1 read (inputs), 2 write (outputs)."""
    out = []
    for n in range(16):
        a = 0x600 + 16 * n
        if len(regs) < a + 16:
            break
        log, length, _sb, _eb, phys, _pb, typ, act = struct.unpack_from("<IHBBHBBB", regs, a)
        if act & 1 and length:
            out.append((log, length, phys, typ & 3))
    return out


def sync_managers(regs):
    """Sync managers from the registers: {number: (physical start, length)}."""
    out = {}
    for n in range(16):
        a = 0x800 + 8 * n
        if len(regs) < a + 8:
            break
        phys, length = struct.unpack_from("<HH", regs, a)
        if length:
            out[n] = (phys, length)
    return out


def frames(pcap):
    """EtherCAT frames in a pcap file: [(returned, [(cmd, address, data, wkc)])].
    returned: the frame came back through the slaves (they set the locally
    administered bit of the source address)."""
    if len(pcap) < 24:
        return []
    magic = pcap[:4]
    if magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"):
        end = "<"
    elif magic in (b"\xa1\xb2\xc3\xd4", b"\xa1\xb2\x3c\x4d"):
        end = ">"
    else:
        return []
    out = []
    pos = 24
    while pos + 16 <= len(pcap):
        _s, _u, incl, _orig = struct.unpack_from(end + "IIII", pcap, pos)
        pkt = pcap[pos + 16:pos + 16 + incl]
        pos += 16 + incl
        if len(pkt) < 16:
            continue
        etype = struct.unpack_from(">H", pkt, 12)[0]
        off = 14
        if etype == 0x8100 and len(pkt) >= 18:
            etype = struct.unpack_from(">H", pkt, 16)[0]
            off = 18
        if etype != ETHERTYPE:
            continue
        hdr = struct.unpack_from("<H", pkt, off)[0]
        if hdr >> 12 != 1:          # not datagrams
            continue
        off += 2
        dgs = []
        while off + 10 <= len(pkt):
            cmd, _idx, addr, lenf, _irq = struct.unpack_from("<BBIHH", pkt, off)
            n = lenf & 0x7FF
            data = pkt[off + 10:off + 10 + n]
            if len(data) < n or off + 12 + n > len(pkt):
                break
            wkc = struct.unpack_from("<H", pkt, off + 10 + n)[0]
            dgs.append((cmd, addr, data, wkc))
            off += 12 + n
            if not lenf & 0x8000:
                break
        out.append((bool(pkt[6] & 0x02), dgs))
    return out


def entry_places(sms, sm_regs, fmmu_list):
    """Where each entry is in the logical process image:
    [(direction, index, subindex, bits, logical bit address)]."""
    places = []
    for sm, direction, entries in sms:
        if sm not in sm_regs:
            continue
        phys, _length = sm_regs[sm]
        want = 1 if direction == "in" else 2
        f = next((f for f in fmmu_list if f[3] & want and f[2] <= phys < f[2] + f[1]), None)
        if f is None:
            continue
        base = (f[0] + phys - f[2]) * 8
        bit = 0
        for index, sub, bits, _name in entries:
            if index:
                places.append((direction, index, sub, bits, base + bit))
            bit += bits
    return places


def _value(data, start, bit, bits):
    """An entry's value (at most 32 bits kept) from a datagram's data."""
    rel = bit - start * 8
    nbytes = (rel % 8 + bits + 7) // 8
    lo = rel // 8
    if lo < 0 or lo + nbytes > len(data):
        return None
    v = int.from_bytes(data[lo:lo + nbytes], "little") >> (rel % 8)
    return v & ((1 << min(bits, 32)) - 1)


def cycles(places, frame_list, limit=1000):
    """Per returned frame that holds all the entries: their values, in the
    order of places. At most limit cycles."""
    out = []
    for returned, dgs in frame_list:
        if not returned:
            continue
        row = []
        for _d, _i, _s, bits, bit in places:
            v = None
            for cmd, addr, data, wkc in dgs:
                if cmd in LOGICAL_CMDS and wkc and addr * 8 <= bit and \
                        bit + bits <= (addr + len(data)) * 8:
                    v = _value(data, addr, bit, bits)
                    break
            if v is None:
                break
            row.append(v)
        if places and len(row) == len(places):
            out.append(row)
            if len(out) >= limit:
                break
    return out


def _calls(files):
    calls = {}
    for c in json.loads(files["tool.json"]):
        calls.setdefault(" ".join(c["args"]), c)
    return calls


def slaves(files, limit=1000):
    """What a header holds, per slave: dicts with position, ids, name,
    entries (outputs first, gaps left out), n_outputs, cycles."""
    manifest = json.loads(files["capture.json"])
    calls = _calls(files)
    frame_list = frames(files.get("frames.pcap", b""))
    out = []
    for s in manifest.get("slaves", []):
        pos = s["position"]
        c = calls.get("pdos -p %d" % pos)
        sms = parse_pdos(c.get("out", "")) if c else []
        regs = b""
        for a in range(0, capture.REG_END, capture.REG_CHUNK):
            r = calls.get("reg_read -p %d 0x%04x %d" % (pos, a, capture.REG_CHUNK))
            if not r or "out" not in r:
                break
            regs += parse_regs(r["out"])
        ordered = [e for d in ("out", "in") for sm, dd, es in sms if dd == d
                   for e in es if e[0]]
        n_out = sum(1 for sm, dd, es in sms if dd == "out" for e in es if e[0])
        places = entry_places(sms, sync_managers(regs), fmmus(regs))
        # cycle values in the order of the entries
        rows = cycles(places, frame_list, limit) if frame_list else []
        if rows:
            at = {(p[1], p[2]): i for i, p in enumerate(places)}
            if all((e[0], e[1]) in at for e in ordered):
                rows = [[r[at[(e[0], e[1])]] for e in ordered] for r in rows]
            else:
                rows = []
        out.append({"position": pos, "vendor_id": s.get("vendor_id") or 0,
                    "product_code": s.get("product_code") or 0, "revision": s.get("revision") or 0,
                    "name": s.get("name") or "", "state": s.get("state"),
                    "entries": ordered, "n_outputs": n_out, "cycles": rows})
    return manifest, out


def _ident(text):
    t = re.sub(r"[^0-9A-Za-z_]", "_", text).strip("_").lower()
    return ("c_" + t) if not t or t[0].isdigit() else t


def _cstr(text):
    return '"%s"' % text.replace("\\", "\\\\").replace('"', '\\"')


def header(path, name="capture", limit=1000):
    """The C header text for a capture file."""
    files = capture.read(path)
    manifest, sl = slaves(files, limit)
    n = _ident(name)
    guard = "_ECAT_TEST_CAPTURED_%s_H_" % n.upper()
    out = ["//",
           "//    Fake slaves from a capture: generated by",
           "//    `ecatsetup --capture-c %s --c-name %s`; do not edit." % (path.split("/")[-1], name),
           "//    Captured %s; slaves in %s." % (manifest.get("time", "?"), ", ".join(
               sorted({s["state"] or "?" for s in sl})) or "-"),
           "//",
           "",
           "#ifndef " + guard,
           "#define " + guard,
           "",
           '#include "fake.h"',
           ""]
    for s in sl:
        p = "%s_%d" % (n, s["position"])
        out.append("// slave %d: %s, vendor 0x%08X product 0x%08X revision 0x%08X" % (
            s["position"], s["name"], s["vendor_id"], s["product_code"], s["revision"]))
        out.append("static const fake_entry_t %s_entries[] = {" % p)
        for i, (index, sub, bits, ename) in enumerate(s["entries"]):
            out.append("    {0x%04X, %d, %d},  // %s %s" % (
                index, sub, bits, "out" if i < s["n_outputs"] else "in", ename))
        out.append("    FAKE_END,")
        out.append("};")
        if s["cycles"]:
            out.append("static const uint32_t %s_values[] = {" % p)
            for row in s["cycles"]:
                out.append("    " + ", ".join("0x%X" % v for v in row) + ",")
            out.append("};")
        out.append("")
    out.append("static const fake_capture_slave_t %s_slaves[] = {" % n)
    for s in sl:
        p = "%s_%d" % (n, s["position"])
        out.append("    {%d, 0x%08X, 0x%08X, 0x%08X, %s, %s_entries, %d, %d, %d, %s}," % (
            s["position"], s["vendor_id"], s["product_code"], s["revision"], _cstr(s["name"]), p,
            len(s["entries"]), s["n_outputs"], len(s["cycles"]),
            "%s_values" % p if s["cycles"] else "NULL"))
    out.append("};")
    out.append("#define %s_N_SLAVES %d" % (n.upper(), len(sl)))
    out.append("")
    out.append("#endif")
    return "\n".join(out) + "\n"
