"""Tests for ecatsetup.capturec: C fake slaves from a capture."""

import io
import json
import os
import shutil
import struct
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ecatsetup import capture, capturec  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
PLASMA = os.path.join(ROOT, "tests", "data", "capture-ect60-easercat.tar.gz")
PLASMA_OP = os.path.join(ROOT, "tests", "data", "capture-plasma-op.tar.gz")

PDOS = """SM0: PhysAddr 0x1000, DefaultSize  128, ControlRegister 0x26, Enable 1
SM1: PhysAddr 0x1080, DefaultSize  128, ControlRegister 0x22, Enable 1
SM2: PhysAddr 0x1100, DefaultSize    0, ControlRegister 0x64, Enable 1
  RxPDO 0x1600 "Outputs"
    PDO entry 0x7000:01,  1 bit, "Out 1"
    PDO entry 0x0000:00,  7 bit, "Gap"
    PDO entry 0x7010:00, 16 bit, "Word"
SM3: PhysAddr 0x1180, DefaultSize    0, ControlRegister 0x20, Enable 1
  TxPDO 0x1a00 "Inputs"
    PDO entry 0x6000:01,  1 bit, "In 1"
    PDO entry 0x6000:02,  1 bit, "In 2"
    PDO entry 0x0000:00,  6 bit, "Gap"
    PDO entry 0x6010:00, 32 bit, "Counter"
"""


def regs():
    """ESC registers: SM2 0x1100 3 bytes, SM3 0x1180 5 bytes; FMMU 0 maps
    the outputs to logical 0x100, FMMU 1 the inputs to 0x103."""
    r = bytearray(capture.REG_END)
    struct.pack_into("<HHBBBB", r, 0x800 + 2 * 8, 0x1100, 3, 0x64, 0, 1, 0)
    struct.pack_into("<HHBBBB", r, 0x800 + 3 * 8, 0x1180, 5, 0x20, 0, 1, 0)
    struct.pack_into("<IHBBHBBB", r, 0x600, 0x100, 3, 0, 7, 0x1100, 0, 2, 1)
    struct.pack_into("<IHBBHBBB", r, 0x610, 0x103, 5, 0, 7, 0x1180, 0, 1, 1)
    return bytes(r)


def frame(src0, data, addr=0x100, wkc=3):
    dg = struct.pack("<BBIHH", 12, 0, addr, len(data), 0) + data + struct.pack("<H", wkc)
    eth = b"\xff" * 6 + bytes([src0, 0x62, 0x37, 0x06, 0x66, 0x54]) + struct.pack(">H", 0x88A4)
    return eth + struct.pack("<H", len(dg) | 0x1000) + dg


def pcap(frames):
    out = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
    for f in frames:
        out += struct.pack("<IIII", 0, 0, len(f), len(f)) + f
    return out


def image(out1, word, in1, in2, counter):
    return bytes([out1]) + struct.pack("<H", word) + bytes([in1 | in2 << 1]) + struct.pack("<I", counter)


def write_capture(path, frames_bytes):
    regs_hex = regs()
    calls = [{"args": ["pdos", "-p", "0"], "out": PDOS}]
    for a in range(0, capture.REG_END, capture.REG_CHUNK):
        calls.append({"args": ["reg_read", "-p", "0", "0x%04x" % a, str(capture.REG_CHUNK)],
                      "out": " ".join("0x%02x" % b for b in regs_hex[a:a + capture.REG_CHUNK])})
    manifest = {"format": 1, "time": "t", "slaves": [
        {"position": 0, "vendor_id": 2, "product_code": 0x1234, "revision": 1,
         "name": 'Test "IO"', "state": "OP"}]}
    files = {"capture.json": json.dumps(manifest).encode(), "tool.json": json.dumps(calls).encode()}
    if frames_bytes is not None:
        files["frames.pcap"] = frames_bytes
    with tarfile.open(path, "w:gz") as tar:
        for name, data in files.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))


class TestParts(unittest.TestCase):
    def test_parse_pdos(self):
        sms = capturec.parse_pdos(PDOS)
        self.assertEqual([(s[0], s[1], len(s[2])) for s in sms], [(2, "out", 3), (3, "in", 4)])
        self.assertEqual(sms[1][2][3], (0x6010, 0, 32, "Counter"))

    def test_registers(self):
        r = regs()
        self.assertEqual(capturec.fmmus(r), [(0x100, 3, 0x1100, 2), (0x103, 5, 0x1180, 1)])
        self.assertEqual(capturec.sync_managers(r), {2: (0x1100, 3), 3: (0x1180, 5)})
        self.assertEqual(capturec.parse_regs("0x01 0xff"), b"\x01\xff")

    def test_places_skip_gaps(self):
        r = regs()
        p = capturec.entry_places(capturec.parse_pdos(PDOS), capturec.sync_managers(r),
                                  capturec.fmmus(r))
        self.assertEqual([(x[1], x[2], x[4]) for x in p], [
            (0x7000, 1, 0x800), (0x7010, 0, 0x808),
            (0x6000, 1, 0x818), (0x6000, 2, 0x819), (0x6010, 0, 0x820)])

    def test_fmmu_of_the_right_direction(self):
        """A reading FMMU on the outputs' sync manager (before the writing
        one) does not place the outputs."""
        r = bytearray(capture.REG_END)
        r[:] = regs()
        r[0x620:0x640] = r[0x600:0x620]
        struct.pack_into("<IHBBHBBB", r, 0x600, 0x200, 3, 0, 7, 0x1100, 0, 1, 1)
        struct.pack_into("<IHBBHBBB", r, 0x610, 0x300, 5, 0, 7, 0x1180, 0, 2, 1)
        r = bytes(r)
        p = capturec.entry_places(capturec.parse_pdos(PDOS), capturec.sync_managers(r),
                                  capturec.fmmus(r))
        self.assertEqual([x[4] for x in p], [0x800, 0x808, 0x818, 0x819, 0x820])

    def test_frames_and_cycles(self):
        sent = frame(0xC4, image(1, 0x1234, 0, 0, 0))
        back1 = frame(0xC6, image(1, 0x1234, 1, 0, 0xDEADBEEF))
        back2 = frame(0xC6, image(0, 0x4321, 0, 1, 7))
        nowkc = frame(0xC6, image(0, 0, 1, 1, 9), wkc=0)
        fl = capturec.frames(pcap([sent, back1, nowkc, back2]))
        self.assertEqual([f[0] for f in fl], [False, True, True, True])
        r = regs()
        p = capturec.entry_places(capturec.parse_pdos(PDOS), capturec.sync_managers(r),
                                  capturec.fmmus(r))
        self.assertEqual(capturec.cycles(p, fl), [[1, 0x1234, 1, 0, 0xDEADBEEF],
                                                  [0, 0x4321, 0, 1, 7]])
        self.assertEqual(len(capturec.cycles(p, fl, limit=1)), 1)

    def test_not_pcap(self):
        self.assertEqual(capturec.frames(b"nothing at all, not a pcap file"), [])


class TestHeader(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ecatsetup-test-capturec-")
        self.addCleanup(shutil.rmtree, self.dir)
        self.path = os.path.join(self.dir, "c.tar.gz")

    def test_with_cycles(self):
        write_capture(self.path, pcap([frame(0xC6, image(1, 2, 1, 1, 5))]))
        h = capturec.header(self.path, "my-bus")
        self.assertIn("static const fake_entry_t my_bus_0_entries[] = {", h)
        self.assertIn("    {0x7010, 0, 16},  // out Word", h)
        self.assertIn("    {0x6010, 0, 32},  // in Counter", h)
        self.assertNotIn("Gap", h)
        self.assertIn("    0x1, 0x2, 0x1, 0x1, 0x5,", h)
        self.assertIn('{0, 0x00000002, 0x00001234, 0x00000001, "Test \\"IO\\"", my_bus_0_entries, '
                      '5, 2, 1, my_bus_0_values},', h)
        self.assertIn("#define MY_BUS_N_SLAVES 1", h)

    def test_without_frames(self):
        write_capture(self.path, None)
        h = capturec.header(self.path, "x")
        self.assertIn("x_0_entries, 5, 2, 0, NULL}", h)
        self.assertNotIn("_values", h)

    def test_checked_in_headers_are_current(self):
        for cap, name, cycles in (("capture-ect60-easercat", "plasma", 1000),
                                  ("capture-plasma-op", "plasma_op", 20)):
            h = os.path.join(ROOT, "component", "tests", "captured_%s.h" % name)
            with open(h) as f:
                self.assertEqual(f.read(), capturec.header(
                    os.path.join(ROOT, "tests", "data", cap + ".tar.gz"), name, cycles),
                    "regenerate: python3 -m ecatsetup --capture-c tests/data/%s.tar.gz "
                    "--c-name %s --c-cycles %d > %s" % (cap, name, cycles, h))

    def test_op_capture_has_cycles(self):
        _m, sl = capturec.slaves(capture.read(PLASMA_OP), limit=20)
        self.assertEqual([len(s["cycles"]) for s in sl], [20, 20, 20, 20])
        x = sl[0]
        row = dict(zip([(e[0], e[1]) for e in x["entries"]], x["cycles"][0]))
        # held at the actual position, CSP, Shutdown
        self.assertEqual(row[(0x607A, 0)], row[(0x6064, 0)])
        self.assertEqual((row[(0x6040, 0)], row[(0x6060, 0)], row[(0x6061, 0)]), (6, 8, 8))


if __name__ == "__main__":
    unittest.main()
