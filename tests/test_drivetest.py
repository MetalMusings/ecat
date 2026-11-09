"""Tests of the drive test (drivetest.py) against the fake halrun and
halcmd of test_testmode and a fake drive's SDOs.

Run with: python3 -m unittest discover -s tests
"""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from ecatsetup import drivetest, testmode, tool  # noqa: E402
from test_testmode import PINS, TestTestMode, cfg  # noqa: E402

ECT60 = {"name": "x", "position": 0, "profile": "cia402",
         "identity": {"vendor": "0x00000A88", "product": "0x0A880002"}}


class FakeDrive:
    """SDOs of a drive: (index, subindex) -> value."""

    def __init__(self, sdos):
        self.sdos = dict(sdos)
        self.writes = []
        self.refuse = set()

    def upload(self, pos, index, sub, dtype):
        return self.sdos.get((index, sub))

    def download(self, pos, index, sub, value, dtype=None):
        if (index, sub) in self.refuse:
            raise tool.ToolError("SDO abort")
        self.writes.append((index, sub, int(value)))
        self.sdos[(index, sub)] = int(value)


class TestPlan(unittest.TestCase):
    def test_ect60(self):
        s = dict(drivetest.DEFAULTS, scale=100.0)
        limits, notes = drivetest.plan_limits(ECT60, lambda i, sub: (i, sub) in {
            (0x607D, 1), (0x607D, 2), (0x2022, 0)}, 100.0, 5000, s)
        got = {lim.name: (lim.index, lim.subindex, lim.value) for lim in limits}
        # the ECT60 keeps its following error in 0x2022, and ignores 0x607D
        # in CSP, so its travel is limited in HAL only
        self.assertEqual(got, {"following-error": (0x2022, 0, 200)})
        self.assertTrue(any("ignores its software position limits" in n for n in notes))
        self.assertTrue(any("max torque" in n for n in notes))
        self.assertTrue(any("limit3" in n for n in notes))

    def test_travel_in_the_drive(self):
        other = dict(ECT60, identity={"vendor": "0x00000002", "product": "0x00001234"})
        s = dict(drivetest.DEFAULTS, scale=100.0)
        limits, _ = drivetest.plan_limits(other, lambda i, sub: (i, sub) in {
            (0x607D, 1), (0x607D, 2)}, 100.0, 5000, s)
        got = {lim.name: lim.value for lim in limits}
        self.assertEqual(got, {"travel-min": 5000 - 2000, "travel-max": 5000 + 2000})

    def test_standard_drive(self):
        other = dict(ECT60, identity={"vendor": "0x00000002", "product": "0x00001234"})
        s = dict(drivetest.DEFAULTS, scale=10.0)
        limits, _ = drivetest.plan_limits(other, lambda i, sub: True, 10.0, 0, s)
        got = {lim.name: (lim.index, lim.value) for lim in limits}
        self.assertEqual(got["following-error"], (0x6065, 20))
        self.assertEqual(got["max-torque"], (0x6072, 300))

    def test_no_limits_in_drive(self):
        _, notes = drivetest.plan_limits(ECT60, lambda i, sub: False, 1.0, 0, drivetest.DEFAULTS)
        self.assertTrue(any("0x607D" in n for n in notes))


class TestDriveTest(TestTestMode):
    """Runs on TestTestMode's fake HAL; its own tests run there too."""

    def setUp(self):
        super().setUp()
        pins = dict(PINS, **{"x.pos-fb": ["float", "OUT", "50"],
                             "x.drv-actual-position": ["s32", "OUT", "5000"]})
        p = mock.patch.dict(os.environ, {"FAKE_HAL_PINS": json.dumps(pins)})
        p.start()
        self.addCleanup(p.stop)
        self.drive = FakeDrive({(0x607D, 1): -1, (0x607D, 2): -1, (0x2022, 0): 4000})
        for name in ("upload", "download"):
            p = mock.patch.object(tool, name, getattr(self.drive, name))
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(drivetest, "RESTORE_FILE", os.path.join(self.dir.name, "restore.json"))
        p.start()
        self.addCleanup(p.stop)

    def coe(self):
        c = mock.Mock()
        c.objects = {0x607D: "", 0x2022: ""}
        c.entries = {(0x607D, 1): 1, (0x607D, 2): 1, (0x2022, 0): 1}
        return c

    def set_fake_pin(self, name, value):
        with open(self.state) as f:
            pins = json.load(f)
        pins[name][2] = value
        with open(self.state, "w") as f:
            json.dump(pins, f)

    def setp_log(self):
        with open(self.state + ".setp") as f:
            return f.read().splitlines()

    def test_prepare_jog_restore(self):
        run = self.started()
        dt = drivetest.DriveTest(run, ECT60, self.coe(), {"scale": 100.0})
        dt.prepare()
        # the drive's own values are kept in a file until they are back
        self.assertEqual({(e["index"], e["subindex"], e["original"])
                          for e in drivetest.pending_restores()},
                         {(0x2022, 0, 4000)})
        self.assertEqual(self.drive.sdos[(0x607D, 1)], -1)     # not written to an ECT60
        self.assertEqual(self.drive.sdos[(0x2022, 0)], 200)
        log = self.setp_log()
        self.assertIn("x.enable FALSE", log)
        self.assertIn("ecatjog0.maxv 5.0", log)
        self.assertIn("ecatjog0.min 30.0", log)    # pos-fb 50 - 20 mm
        self.assertIn("ecatjog0.max 70.0", log)
        self.assertEqual(dt.start, 50.0)

        # the drive has settled a little since: enabling starts from there,
        # so that it does not move
        self.set_fake_pin("x.pos-fb", "50.5")
        dt.enable(True)
        self.assertEqual(self.setp_log()[-2:], ["ecatsel0.in1 50.5", "x.enable TRUE"])
        self.assertEqual(dt.jog(1.0), 51.5)
        self.assertEqual(dt.jog(100.0), 70.0)      # never past the travel
        dt.enable(False)
        self.assertEqual(self.setp_log()[-1], "x.enable FALSE")

        self.assertEqual(dt.restore(), [])
        self.assertEqual(self.drive.sdos[(0x2022, 0)], 4000)
        self.assertEqual(drivetest.pending_restores(), [])
        self.assertIn("ecatjog0.maxv 0", self.setp_log())

    def test_nothing_moves_before_prepare(self):
        run = self.started()
        dt = drivetest.DriveTest(run, ECT60, self.coe())
        with self.assertRaises(testmode.TestError):
            dt.enable(True)
        with self.assertRaises(testmode.TestError):
            dt.jog(1.0)

    def test_refused_limit_puts_the_others_back(self):
        run = self.started()
        other = dict(ECT60, identity={"vendor": "0x00000002", "product": "0x00001234"})
        self.drive.sdos[(0x6065, 0)] = 1000
        self.drive.refuse.add((0x6065, 0))
        c = self.coe()
        c.entries = dict(c.entries); c.entries[(0x6065, 0)] = 1
        dt = drivetest.DriveTest(run, other, c, {"scale": 100.0})
        with self.assertRaises(testmode.TestError):
            dt.prepare()
        # 0x607D was written before the refusal, and is back
        self.assertEqual(self.drive.sdos[(0x607D, 1)], -1)
        self.assertEqual(self.drive.sdos[(0x607D, 2)], -1)
        self.assertFalse(dt.prepared)

    def test_restore_after_a_crash(self):
        run = self.started()
        dt = drivetest.DriveTest(run, ECT60, self.coe(), {"scale": 100.0})
        dt.prepare()
        # the program dies here; the next one finds the file
        done, failed = drivetest.restore_pending()
        self.assertEqual((len(done), failed), (1, []))
        self.assertEqual(self.drive.sdos[(0x2022, 0)], 4000)
        self.assertFalse(os.path.exists(drivetest.RESTORE_FILE))

    def test_only_csp_drives(self):
        run = self.started()
        with self.assertRaises(testmode.TestError):
            drivetest.DriveTest(run, dict(ECT60, name="io"))


class TestHalScale(unittest.TestCase):
    def test_from_hal_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "m.hal"), "w") as f:
                f.write("setp lcec.0.X.pos-scale 100.0  # counts/mm\nsetp lcec.0.Y.pos-scale x\n")
            self.assertEqual(drivetest.hal_scale(d, "lcec.0.X"), 100.0)
            self.assertIsNone(drivetest.hal_scale(d, "lcec.0.Y"))
            self.assertIsNone(drivetest.hal_scale(d, "lcec.0.Z"))


if __name__ == "__main__":
    unittest.main()
