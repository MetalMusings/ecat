"""Tests for ecatsetup.capture: recording the tool's calls, replaying them,
and a recorded bus (three ECT60 drives and an EaserCAT 7000) as fake slaves."""

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ecatsetup import capture, coe, diagnose, session, tool  # noqa: E402

DATA = os.path.join(os.path.dirname(__file__), "data")
PLASMA = os.path.join(DATA, "capture-ect60-easercat.tar.gz")


def easercat_readable():
    """The EaserCAT 7000's entries readable in PREOP."""
    with open(os.path.join(DATA, "easercat7000-sdos.txt")) as f:
        return capture.readable_entries(coe.Dictionary(f.read()), "PREOP")


def fake_run_factory():
    """The ethercat tool for one EaserCAT 7000 at position 0, as tool.run."""
    with open(os.path.join(DATA, "easercat7000.sii"), "rb") as f:
        sii = f.read()
    with open(os.path.join(DATA, "easercat7000-sdos.txt")) as f:
        sdos = f.read()
    calls = []

    def run(*args, binary=False):
        args = [str(a) for a in args]
        calls.append(args)
        cmd = args[0]
        if cmd == "slaves" and "-v" in args:
            out = ("=== Master 0, Slave 0 ===\nState: PREOP\nVendor Id:       0xd0bed1d0\n"
                   "Product code:    0x00007000\nRevision number: 0x00000002\n"
                   "Serial number:   0x00000000\nDistributed clocks: no\n"
                   "Supported protocols: EoE, CoE\n")
        elif cmd == "slaves":
            out = "0  0:0  PREOP  +  MetalMusings EaserCAT 7000\n"
        elif cmd == "master":
            out = "Master0\n  Main: 00:11:22:33:44:55 (attached)\n"
        elif cmd == "sii_read":
            return sii if binary else sii.decode(errors="replace")
        elif cmd == "sdos":
            out = sdos
        elif cmd == "upload":
            if args[-2] == "0x1000":
                out = "0x00001389 5001\n"
            elif args[-2] == "0x6001":
                raise tool.ToolError("SDO upload aborted")
            else:
                out = "0x0001 1\n"
        elif cmd in ("reg_read", "pdos", "xml", "graph", "version", "config", "domains", "diag"):
            out = "%s output\n" % cmd
        else:
            raise tool.ToolError("unexpected: %s" % args)
        return out.encode() if binary else out
    return run, calls


class TestRecordReplay(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ecatsetup-test-capture-")
        self.addCleanup(shutil.rmtree, self.dir)
        self.path = os.path.join(self.dir, "c.tar.gz")
        self.real_run = tool.run
        self.addCleanup(setattr, tool, "run", self.real_run)

    def record(self, **kw):
        run, calls = fake_run_factory()
        tool.run = run
        with mock.patch.object(capture.report, "build", return_value="# report\n"), \
                mock.patch.object(capture.netif, "master_interface", return_value="eth9"):
            notes = capture.record(self.path, progress=lambda t: None,
                                   pcap_fn=kw.pop("pcap_fn", lambda i, s: (None, "none here")),
                                   **kw)
        tool.run = self.real_run
        return notes, calls

    def test_record_puts_the_tool_back(self):
        self.record()
        self.assertIs(tool.run, self.real_run)

    def test_put_back_after_a_failure(self):
        def broken(*a, **k):
            raise tool.ToolError("no master")
        tool.run = broken
        with self.assertRaises(tool.ToolError):
            capture.record(self.path, progress=lambda t: None)
        self.assertIs(tool.run, broken)

    def test_contents(self):
        notes, calls = self.record()
        with tarfile.open(self.path) as tar:
            names = set(tar.getnames())
        self.assertEqual(names, {"capture.json", "tool.json", "report.md"})
        files = capture.read(self.path)
        m = json.loads(files["capture.json"])
        self.assertEqual(m["format"], capture.FORMAT)
        self.assertEqual(m["interface"], "eth9")
        s = m["slaves"][0]
        self.assertEqual((s["position"], s["vendor_id"], s["product_code"]), (0, 0xD0BED1D0, 0x7000))
        self.assertIn("no frames: none here", notes)
        self.assertTrue(any("no ESI" in n for n in notes))
        # registers up to 0x1000 in chunks, never process RAM
        regs = [c for c in calls if c[0] == "reg_read"]
        self.assertEqual([c[3] for c in regs], ["0x0000", "0x0400", "0x0800", "0x0c00"])

    def test_every_readable_value_is_uploaded(self):
        _notes, calls = self.record()
        with open(os.path.join(DATA, "easercat7000-sdos.txt")) as f:
            d = coe.Dictionary(f.read())
        want = {("0x%04x" % e.index, str(e.subindex)) for e in capture.readable_entries(d, "PREOP")}
        got = {(c[3], c[4]) for c in calls if c[0] == "upload" and "-t" not in c}
        self.assertEqual(got, want)
        self.assertTrue(want)
        m = json.loads(capture.read(self.path)["capture.json"])
        refused = sum(1 for e in capture.readable_entries(d, "PREOP") if e.index == 0x6001)
        self.assertTrue(refused)
        self.assertEqual(m["slaves"][0]["values_refused"], refused)

    def test_no_values(self):
        _notes, calls = self.record(values=False)
        self.assertFalse([c for c in calls if c[0] == "upload" and "-t" not in c])

    def test_replay_gives_what_was_recorded(self):
        self.record()
        r = capture.Replay(self.path)
        run, _ = fake_run_factory()
        self.assertEqual(r.run("sii_read", "-p", 0, binary=True), run("sii_read", "-p", 0, binary=True))
        self.assertEqual(r.run("sdos", "-p", 0), run("sdos", "-p", 0))
        # recorded without -t, asked with it
        e = easercat_readable()[-1]
        self.assertNotEqual(e.index, 0x6001)
        self.assertEqual(r.run("upload", "-p", 0, "-t", "uint16", "0x%04x" % e.index, e.subindex),
                         "0x0001 1\n")
        with self.assertRaises(tool.ToolError) as e:
            r.run("upload", "-p", 0, "0x6001", [x.subindex for x in easercat_readable() if x.index == 0x6001][0])
        self.assertIn("aborted", str(e.exception))
        with self.assertRaises(tool.ToolError):
            r.run("sdos", "-p", 7)
        self.assertEqual(r.missing, ["sdos -p 7"])
        with self.assertRaises(tool.ToolError):
            r.run("download", "-p", 0, "--", "0x8000", 1, 5)

    def test_repeated_calls_in_turn(self):
        tool.run = capture.Recorder(lambda *a, **k: None).run  # not used: hand-made file
        calls = [{"args": ["reg_read", "-p", "0", "0x0300", "8"], "out": "a"},
                 {"args": ["reg_read", "-p", "0", "0x0300", "8"], "out": "b"}]
        with tarfile.open(self.path, "w:gz") as tar:
            for name, data in (("capture.json", b'{"format": 1}'),
                               ("tool.json", json.dumps(calls).encode())):
                ti = tarfile.TarInfo(name)
                ti.size = len(data)
                tar.addfile(ti, __import__("io").BytesIO(data))
        r = capture.Replay(self.path)
        self.assertEqual([r.run("reg_read", "-p", 0, "0x0300", 8) for _ in range(3)], ["a", "b", "b"])

    def test_not_a_capture(self):
        p = os.path.join(self.dir, "x.tar.gz")
        with open(p, "wb") as f:
            f.write(b"nothing")
        with self.assertRaises(capture.CaptureError):
            capture.Replay(p)

    def test_newer_format_refused(self):
        with tarfile.open(self.path, "w:gz") as tar:
            for name, data in (("capture.json", b'{"format": 99}'), ("tool.json", b"[]")):
                ti = tarfile.TarInfo(name)
                ti.size = len(data)
                tar.addfile(ti, __import__("io").BytesIO(data))
        with self.assertRaises(capture.CaptureError) as e:
            capture.read(self.path)
        self.assertIn("newer", str(e.exception))


class TestParts(unittest.TestCase):
    def test_readable_entries_by_state(self):
        d = coe.Dictionary('SDO 0x2000, "x"\n'
                           '  0x2000:01, rwr---, uint16, 16 bit, "a"\n'
                           '  0x2000:02, ----rw, uint16, 16 bit, "b"\n')
        self.assertEqual([e.subindex for e in capture.readable_entries(d, "PREOP")], [1])
        self.assertEqual([e.subindex for e in capture.readable_entries(d, "SAFEOP")], [1])
        self.assertEqual([e.subindex for e in capture.readable_entries(d, "OP")], [2])
        self.assertEqual(capture.readable_entries(d, "INIT"), [])

    def test_esi_extract_keeps_only_the_device(self):
        d = tempfile.mkdtemp(prefix="ecatsetup-test-capture-")
        self.addCleanup(shutil.rmtree, d)
        p = os.path.join(d, "two.xml")
        with open(p, "w") as f:
            f.write('<?xml version="1.0"?><EtherCATInfo><Vendor><Id>#x2</Id></Vendor>'
                    '<Descriptions><Devices>'
                    '<Device><Type ProductCode="#x1" RevisionNo="#x1">ONE</Type><Name>One</Name></Device>'
                    '<Device><Type ProductCode="#x2" RevisionNo="#x1">TWO</Type><Name>Two</Name></Device>'
                    '</Devices></Descriptions></EtherCATInfo>')
        devs = capture.esi.load_file(p)
        out = capture.esi_extract([x for x in devs if x.product_code == 2])
        self.assertEqual(list(out), ["two.xml"])
        q = os.path.join(d, "out.xml")
        with open(q, "wb") as f:
            f.write(out["two.xml"])
        got = capture.esi.load_file(q)
        self.assertEqual([(x.vendor_id, x.product_code, x.type_name) for x in got], [(2, 2, "TWO")])

    def test_pcap(self):
        def runner(stdout, rc=124, stderr=b""):
            return lambda cmd, **kw: subprocess.CompletedProcess(cmd, rc, stdout, stderr)
        with mock.patch.object(capture.shutil, "which", return_value="/usr/bin/tcpdump"):
            data, why = capture.pcap("eth0", 1, runner=runner(b"x" * 100))
            self.assertEqual((len(data), why), (100, None))
            data, why = capture.pcap("eth0", 1, runner=runner(b"x" * 24))
            self.assertIsNone(data)
            self.assertIn("XDP", why)
            self.assertIsNone(capture.pcap(None, 1)[0])
        with mock.patch.object(capture.shutil, "which", return_value=None):
            self.assertIn("not installed", capture.pcap("eth0", 1)[1])


class TestRecordedBus(unittest.TestCase):
    """A bus recorded on the plasma cutter (2026-11-14) as fake slaves."""

    def setUp(self):
        self.real_run = tool.run
        self.addCleanup(setattr, tool, "run", self.real_run)
        capture.Replay(PLASMA).install()

    def test_describe(self):
        text = "\n".join(capture.describe(PLASMA))
        self.assertIn("slave 0: ECT60V202(COE) 0x00000A88:0x0A880002", text)
        self.assertIn("slave 3: MetalMusings EaserCAT 7000", text)

    def test_proposal(self):
        s = session.Session()
        s.read_bus()
        self.assertEqual(sorted(s.bus), [0, 1, 2, 3])
        self.assertEqual([s.bus[p].proposal.profile for p in range(3)], [402, 402, 402])
        self.assertIn(0x2000, s.bus[0].coe.objects)

    def test_diagnosis(self):
        lines = []
        diagnose.diagnose(tool.slave_positions(), 0, out=lines.append)
        text = "\n".join(lines)
        self.assertIn("Switch on disabled", text)
        self.assertIn("Slave 3: MetalMusings EaserCAT 7000", text)


if __name__ == "__main__":
    unittest.main()
