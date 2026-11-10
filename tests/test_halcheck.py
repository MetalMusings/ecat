"""Tests of the HAL check (halcheck.py), the component's pin list
(halpins.py), hand edits of ethercat.hal and renames into the user's HAL
files.

Run with: python3 -m unittest discover -s tests
"""

import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
os.environ["ECATSETUP_DEVICES"] = os.path.join(os.path.dirname(__file__), "no-such-dir")

from ecatsetup import __main__ as cli, config, halcheck, halpins, integrate  # noqa: E402
from test_integrate import Folder, cfg  # noqa: E402

COMPONENT = os.path.join(os.path.dirname(__file__), "..", "component")
HALTYPE = {"HAL_BIT": "bit", "HAL_FLOAT": "float", "HAL_S32": "s32", "HAL_U32": "u32"}
HALDIR = {"HAL_IN": "in", "HAL_OUT": "out", "HAL_IO": "io", "HAL_RW": "rw", "HAL_RO": "ro"}


def c_tables(path):
    """{table name: [(name, type, dir)]} of a C file's pin and parameter tables."""
    with open(os.path.join(COMPONENT, path)) as f:
        text = f.read()
    out = {}
    for m in re.finditer(r"static const lcec_(?:pin|param)desc_t (\w+)\[\] = \{(.*?)\n\};", text, re.S):
        out[m.group(1)] = [(name.split(".")[-1], HALTYPE[t], HALDIR[d]) for t, d, name in re.findall(
            r"\{(HAL_\w+), (HAL_\w+), [^\"]*\"([^\"]+)\"\}", m.group(2))]
    return out


class TestPinList(unittest.TestCase):
    def test_mirrors_the_c_tables(self):
        m = c_tables("lcec_main.c")
        self.assertEqual(halpins.MASTER_GLOBAL, m["master_global_pins"])
        self.assertEqual(halpins.MASTER, m["master_pins"] + m["master_params"])
        self.assertEqual(halpins.SLAVE, m["slave_pins"])
        c = c_tables("devices/ecat_cia402.c")
        self.assertEqual(halpins.CIA402, c["pins"] + c["params"])
        self.assertEqual(halpins.CIA402_HOMING, c["homing_pins"] + c["homing_params"])
        self.assertEqual(halpins.CIA402_PV, c["pv_pins"])
        self.assertEqual(halpins.CIA402_PP, c["pp_pins"] + c["pp_params"])
        self.assertEqual(halpins.CIA402_PP_VELOCITY, c["pp_velocity_pins"])
        self.assertEqual(halpins.CIA402_STOP, c["stop_params"])
        self.assertEqual(halpins.CIA402_ZERO, c["zero_pins"])
        self.assertEqual(halpins.CIA402_MODE, c["mode_pins"])
        e = c_tables("devices/ecat_encoder.c")
        self.assertEqual(halpins.ENCODER, e["pins"] + e["params"])
        self.assertEqual(halpins.ENCODER_INDEX, e["index_pins"])
        self.assertEqual(halpins.ENCODER_LATCH, e["latch_pins"])

    def test_pins_of_a_configuration(self):
        c = cfg()
        p = halpins.pins(c)
        self.assertEqual(p["io.out-1"], ("bit", "in"))
        self.assertEqual(p["x.pos-fb"], ("float", "out"))
        self.assertEqual(p["x.pos-scale"], ("float", "rw"))
        self.assertEqual(p["ecat.0.all-op"], ("bit", "out"))
        self.assertEqual(p["ecat.take-down"], ("bit", "in"))
        self.assertNotIn("x.index-enable", p)
        c["slaves"][0]["cia402"]["modes"] = ["hm"]
        self.assertIn("x.index-enable", halpins.pins(c))
        self.assertNotIn("x.opmode-cmd", halpins.pins(c))
        c["slaves"][0]["cia402"]["modes"] = ["hm", "pv"]
        self.assertIn("x.opmode-cmd", halpins.pins(c))
        self.assertIn("x.at-speed", halpins.pins(c))


class TestCheck(Folder):
    def check(self, c=None):
        return [(f.severity, f.file, f.line, f.text) for f in halcheck.check(self.ini, c or cfg())]

    def test_clean(self):
        # the fixture's files: x and io pins that exist
        self.assertEqual(self.check(), [])

    def test_findings(self):
        self.write("machine.hal", "net a io.out1 <= motion.digital-out-00\n"
                                  "net b x.pos-fb => joint.0.motor-pos-fb\n"
                                  "net c x.pos-fb => halui.machine.on\n"
                                  "setp x.pos-fb 3\n"
                                  "net d motion.digital-out-01 halui.machine.is-on => io.out-1\n"
                                  "net e io.out-9 joint.0.home-sw-in\n"
                                  "net f joint.0.amp-fault-in => io.out-1\n")
        found = self.check()
        texts = [t for _, _, _, t in found]
        self.assertIn("io.out1 is not a pin of the configuration; did you mean io.out-1?", texts)
        self.assertIn("x.pos-fb is on two signals, b and c; HAL refuses the second", texts)
        self.assertIn("setp of x.pos-fb, which is on signal b; HAL refuses to set a connected pin",
                      texts)
        self.assertIn("setp of x.pos-fb, an output of the slave", texts)
        self.assertIn("signal d has two writers, motion.digital-out-01 and halui.machine.is-on",
                      texts)
        self.assertIn("io.out-1 is on two signals, d and f; HAL refuses the second", texts)
        self.assertTrue(any(t.startswith("io.out-9 is not a pin") for t in texts))
        self.assertIn(("error", "machine.hal", 1, texts[texts.index(
            "io.out1 is not a pin of the configuration; did you mean io.out-1?")]), found)

    def test_unlinkp_and_delsig(self):
        # as LinuxCNC's axis_manualtoolchange.hal: unlink, then connect anew
        self.write("machine.hal", "net loop io.in-x x.enable\n"
                                  "net a motion.digital-out-00 => io.out-1\n"
                                  "unlinkp io.out-1\n"
                                  "net b motion.digital-out-01 => io.out-1\n"
                                  "net c motion.digital-out-02 => x.fault-reset\n"
                                  "delsig c\n"
                                  "net d motion.digital-out-03 => x.fault-reset\n")
        texts = [t for _, _, _, t in self.check()]
        self.assertFalse(any("two signals" in t for t in texts), texts)

    def test_nothing_writes(self):
        self.write("machine.hal", "net g joint.0.home-sw-in io.out-1\n"
                                  "net h => io.out-1-not-there\n"
                                  "net plasmac:laser-on => io.out-1\n")
        texts = [t for _, _, _, t in self.check()]
        self.assertNotIn("nothing writes signal plasmac:laser-on", " ".join(texts))
        # g: two readers and no writer; but io.out-1 is on g and plasmac:laser-on
        self.assertIn("nothing writes signal g, so io.out-1 stays at its default", texts)

    def test_renamed_slave(self):
        c = cfg()
        for s in c["slaves"]:
            s["name"] = "lcec.0.%s" % s["name"]
        self.write("machine.hal", "net a lcec.0.io.out-1 <= motion.digital-out-00\n")
        self.assertEqual(self.check(c), [])
        c["slaves"][1]["name"] = "lcec.0.board"
        texts = [t for _, _, _, t in self.check(c)]
        self.assertEqual(texts, ["lcec.0.io.out-1 is not a pin of the configuration; did you "
                                 "mean lcec.0.board.out-1?"])


class TestHandEdits(Folder):
    def test_checksum_and_overwrite(self):
        c = cfg()
        integrate.set_joint(c, 0, "x", 100.0)
        integrate.apply(integrate.plan(self.ini, c))
        main = self.read("ethercat.hal")
        self.assertIn(halcheck.CHECKSUM, main)
        self.assertFalse(halcheck.edited(main))
        self.assertEqual(halcheck.check(self.ini, c), [])
        self.write("ethercat.hal", main + "net mine x.stat-fault => halui.machine.off\n")
        found = halcheck.check(self.ini, c)
        self.assertEqual([(f.severity, f.file) for f in found], [("warn", "ethercat.hal")])
        integrate.set_joint(c, 0, "x", 50.0)
        p = integrate.plan(self.ini, c)
        self.assertEqual(p.edited, ["ethercat.hal"])
        with self.assertRaisesRegex(ValueError, "changed by hand"):
            integrate.apply(p)
        integrate.apply(p, overwrite_edited=True)
        self.assertNotIn("net mine", self.read("ethercat.hal"))
        self.assertEqual(integrate.plan(self.ini, c).edited, [])

    def test_check_at_start(self):
        c = cfg()
        c["linuxcnc"] = {"check-hal-at-start": True}
        self.assertEqual(config.check(c), [])
        integrate.apply(integrate.plan(self.ini, c))
        load = self.read("ethercat-load.hal")
        self.assertLess(load.index("loadusr -w ecatsetup --check-hal"), load.index("loadrt ecat"))
        c["linuxcnc"] = {"check-hal-at-start": "yes"}
        self.assertIn("linuxcnc.check-hal-at-start: must be true or false", config.check(c))


class TestRename(Folder):
    def test_rename_in_hal(self):
        before = ("net io-out io.out-1 <= motion.digital-out-00  # io.out-1\n"
                  "setp x.pos-scale 100\nnet z xio.out-1 => radio.x\n")
        self.write("machine.hal", before)
        c = cfg()
        renamed = integrate.rename_slave(c, "io", "board")
        p = integrate.rename_in_hal(self.ini, renamed)
        self.assertEqual([(ch.name, ch.what) for ch in p.changes], [("machine.hal", "1 pin renamed")])
        integrate.apply(p)
        text = self.read("machine.hal")
        # whole pin names only; comments left alone
        self.assertIn("net io-out board.out-1 <= motion.digital-out-00  # io.out-1\n", text)
        self.assertIn("net z xio.out-1 => radio.x\n", text)
        self.assertEqual(self.read("machine.hal.bak"), before)
        c["slaves"][1]["name"] = "board"
        self.assertEqual([f for f in halcheck.check(self.ini, c) if f.severity == "error"], [])


class TestCommand(Folder):
    def test_check_hal(self):
        c = cfg()
        config.write(c, os.path.join(self.dir.name, "ethercat.yaml"))
        with open(self.ini) as f:
            ini = f.read().replace("[HAL]", "[ETHERCAT]\nCONFIG = ethercat.yaml\n\n[HAL]")
        self.write("machine.ini", ini)
        out = []
        with mock.patch.dict(os.environ, {"INI_FILE_NAME": self.ini}), \
                mock.patch("builtins.print", out.append):
            self.assertEqual(cli.check_hal(cli.argparse.Namespace(check_hal="", ini=None)), 0)
        self.assertEqual(out, ["ecatsetup: the HAL files match ethercat.yaml"])
        self.write("machine.hal", "net a io.out-2 <= motion.digital-out-00\n")
        out = []
        with mock.patch.dict(os.environ, {"INI_FILE_NAME": self.ini}), \
                mock.patch("builtins.print", out.append):
            self.assertEqual(cli.check_hal(cli.argparse.Namespace(check_hal="", ini=None)), 1)
        self.assertIn("1 error", out[-1])


if __name__ == "__main__":
    unittest.main()


try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5.QtWidgets import QApplication, QMessageBox
    from ecatsetup import gui, session
except ImportError:     # pragma: no cover
    gui = None


@unittest.skipIf(gui is None, "no PyQt5")
class TestWindow(Folder):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def window(self):
        s = session.Session(ini_path=self.ini)
        s.cfg, s.path = cfg(), os.path.join(self.dir.name, "ethercat.yaml")
        w = gui.MainWindow(s)
        self.addCleanup(lambda: (s.__setattr__("saved", s.cfg), w.close()))
        return s, w

    def test_check_card_and_start_option(self):
        self.write("machine.hal", "net a io.out1 <= motion.digital-out-00\n")
        s, w = self.window()
        w.tabs.setCurrentWidget(w.hal_page)
        page = w.hal_page
        page.refresh()
        self.assertEqual(page.check_table.rowCount(), 1)
        self.assertEqual(page.check_table.item(0, 1).text(), "machine.hal:1")
        self.assertIn("did you mean io.out-1", page.check_table.item(0, 2).text())
        page.check_at_start.setChecked(True)
        self.assertIs(s.cfg["linuxcnc"]["check-hal-at-start"], True)
        page.check_at_start.setChecked(False)
        self.assertNotIn("linuxcnc", s.cfg)

    def test_rename_offered_for_hal_files(self):
        self.write("machine.hal", "net io-out io.out-1 <= motion.digital-out-00\n")
        s, w = self.window()
        w.select_slave(1)
        w.slave_page.name.setText("board")
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.Yes) as q:
            w.slave_page.general_edited()
        self.assertIn("machine.hal: 1 pin renamed", q.call_args[0][2])
        self.assertIn("board.out-1", self.read("machine.hal"))
