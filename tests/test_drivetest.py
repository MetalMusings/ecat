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
# quirk files: only those that come with the program
os.environ["ECATSETUP_DEVICES"] = os.path.join(os.path.dirname(__file__), "no-such-dir")
sys.path.insert(0, os.path.dirname(__file__))

from ecatsetup import drivetest, testmode, tool  # noqa: E402
from test_testmode import PINS, TestTestMode, cfg  # noqa: E402

ECT60 = {"name": "x", "position": 0, "profile": "cia402", "cia402": {"mode": "csp"},
         "identity": {"vendor": "0x00000A88", "product": "0x0A880002"}}
OTHER = dict(ECT60, identity={"vendor": "0x00000002", "product": "0x00001234"})


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

    def test_second_axis(self):
        s = dict(drivetest.DEFAULTS, scale=10.0)
        other = dict(OTHER, cia402={"mode": "csp", "axes": ["a", "b"]})
        has = {(0x687D, 1), (0x687D, 2), (0x6865, 0), (0x6872, 0), (0x2022, 0)}
        limits, _ = drivetest.plan_limits(other, lambda i, sub: (i, sub) in has, 10.0, 0, s, axis=1)
        got = {lim.name: (lim.index, lim.subindex) for lim in limits}
        self.assertEqual(got, {"travel-min": (0x687D, 1), "travel-max": (0x687D, 2),
                               "following-error": (0x6865, 0), "max-torque": (0x6872, 0)})
        self.assertIn("0x6865", next(lim.text for lim in limits if lim.name == "following-error"))

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

    def test_only_cia402_drives_in_a_mode_it_can_move(self):
        run = self.started()
        with self.assertRaises(testmode.TestError):
            drivetest.DriveTest(run, dict(ECT60, name="io", profile="generic"))
        with self.assertRaises(testmode.TestError):
            drivetest.DriveTest(run, dict(ECT60, cia402={"mode": "csv"}))
        # CSV with homing can be homed, not moved
        dt = drivetest.DriveTest(run, dict(ECT60, cia402={"mode": "csv", "modes": ["hm"]}))
        self.assertFalse(dt.moves)
        self.assertTrue(dt.homes)

    def test_pp_moves_once_enabled(self):
        run = self.started()
        pp = dict(OTHER, cia402={"mode": "pp"})
        c = self.coe()
        c.entries = dict(c.entries); c.entries[(0x607F, 0)] = 1
        self.drive.sdos[(0x607F, 0)] = 900000
        dt = drivetest.DriveTest(run, pp, c, {"scale": 100.0})
        dt.prepare()
        # travel and the maximum profile velocity in the drive
        self.assertEqual(self.drive.sdos[(0x607D, 1)], 5000 - 2000)
        self.assertEqual(self.drive.sdos[(0x607F, 0)], 500)
        log = self.setp_log()
        for line in ("x.velo-scale 100.0", "x.pp-start FALSE", "x.pp-velocity 5.0",
                     "x.pp-relative FALSE"):
            self.assertIn(line, log)
        self.assertFalse(any(x.startswith("ecatjog") for x in log))
        dt.enable(True)
        self.assertEqual(dt.jog(1.0), 51.0)
        # not yet enabled: the edge would be lost, so it waits
        self.assertIsNone(dt.tick(False))
        self.assertNotIn("x.pp-start TRUE", self.setp_log())
        self.assertEqual(dt.tick(True), "move")
        self.assertEqual(self.setp_log()[-2:], ["x.pp-target 51.0", "x.pp-start TRUE"])
        self.assertIsNone(dt.tick(True))         # once
        # from where the drive is, never past the travel
        self.set_fake_pin("x.pos-fb", "69.5")
        self.assertEqual(dt.jog(5.0), 70.0)
        dt.enable(False)
        self.assertEqual(self.setp_log()[-2:], ["x.pp-start FALSE", "x.enable FALSE"])
        self.assertIsNone(dt.tick(True))         # released before enabled: nothing pending
        with self.assertRaises(testmode.TestError):
            dt.run_velocity(1)
        self.assertEqual(dt.restore(), [])
        self.assertEqual(self.drive.sdos[(0x607F, 0)], 900000)

    def test_pv_runs_while_held(self):
        run = self.started()
        pv = dict(OTHER, cia402={"mode": "pv"})
        dt = drivetest.DriveTest(run, pv, self.coe(), {"scale": 10.0, "max-velocity": 2.0})
        dt.prepare()
        self.assertNotIn((0x607D, 1), [w[:2] for w in self.drive.writes])   # no travel in PV
        self.assertIn("x.velocity-cmd 0", self.setp_log())
        dt.enable(True)
        dt.run_velocity(-1)
        self.assertEqual(dt.tick(True), "run")
        self.assertEqual(self.setp_log()[-1], "x.velocity-cmd -2.0")
        dt.enable(False)
        self.assertEqual(self.setp_log()[-2:], ["x.velocity-cmd 0", "x.enable FALSE"])
        with self.assertRaises(testmode.TestError):
            dt.jog(1.0)

    def test_home(self):
        run = self.started()
        dt = drivetest.DriveTest(run, dict(ECT60, cia402={"mode": "csp", "modes": ["hm"]}),
                                 self.coe(), {"scale": 100.0})
        dt.prepare()
        self.assertIn("x.home FALSE", self.setp_log())
        dt.enable(True)
        dt.home()
        self.assertEqual(dt.tick(True), "home")
        self.assertEqual(self.setp_log()[-1], "x.home TRUE")
        dt.enable(False)
        self.assertEqual(self.setp_log()[-2:], ["x.home FALSE", "x.enable FALSE"])
        no_hm = drivetest.DriveTest(run, ECT60, self.coe(), {"scale": 100.0})
        no_hm.prepare()
        with self.assertRaises(testmode.TestError):
            no_hm.home()


try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5.QtWidgets import QApplication
    from ecatsetup import gui, session
except ImportError:     # pragma: no cover
    gui = None


@unittest.skipIf(gui is None, "no PyQt5")
class TestMoveCard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        # the fake HAL of TestTestMode, without running its tests again
        self.helper = TestTestMode("test_says_so_when_ecat_does_not_load")
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.started = self.helper.started

    def test_buttons_by_mode(self):
        s = session.Session()
        w = gui.MainWindow(s)
        self.addCleanup(w.close)
        w.test = self.started()
        card = w.test_page.move
        shown = lambda b: not b.isHidden()  # noqa: E731
        card.show_drive(dict(OTHER, cia402={"mode": "pp", "modes": ["hm"]}))
        self.assertTrue(shown(card.setup))
        self.assertEqual((shown(card.jog_row), shown(card.run_row), shown(card.home_button)),
                         (True, False, True))
        self.assertIn("plans the step itself", card.jog_plus.toolTip())
        card.show_drive(dict(OTHER, cia402={"mode": "pv"}))
        self.assertEqual((shown(card.jog_row), shown(card.run_row), shown(card.home_button)),
                         (False, True, False))
        two = dict(OTHER, cia402={"mode": "csp", "axes": ["a", "b"]})
        card.show_drive(two)
        self.assertTrue(shown(card.axis))
        self.assertEqual(card.axis_name(), "a")
        card.axis.setCurrentIndex(1)
        self.assertEqual(card.axis_name(), "b")
        card.show_drive(dict(OTHER, cia402={"mode": "csv"}))
        self.assertFalse(shown(card.axis))
        self.assertFalse(shown(card.setup))
        self.assertIn("Only cia402 drives", card.unsupported.text())
        w.test = None


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
