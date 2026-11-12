"""Tests of writing a configuration into a LinuxCNC configuration
(integrate.py), on a made-up configuration folder.

Run with: python3 -m unittest discover -s tests
"""

import copy
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# quirk files: only those that come with the program
os.environ["ECATSETUP_DEVICES"] = os.path.join(os.path.dirname(__file__), "no-such-dir")
os.environ["ECATSETUP_SETTINGS"] = os.path.join(os.path.dirname(__file__), "no-such-dir", "settings.yaml")
# state (test lock, drive limits): never the user's own
if "ECATSETUP_TEST_STATE" not in os.environ:
    os.environ["ECATSETUP_TEST_STATE"] = os.environ["XDG_STATE_HOME"] = \
        __import__("tempfile").mkdtemp(prefix="ecatsetup-test-state-")
sys.path.insert(0, os.path.dirname(__file__))

from ecatsetup import config, integrate  # noqa: E402
from test_testmode import cfg as base_cfg  # noqa: E402

INI = """\
[EMCMOT]
SERVO_PERIOD = 1000000

[KINS]
JOINTS = 3
KINEMATICS = trivkins coordinates=XYZ

[HAL]
TWOPASS = true
HALUI = halui
HALFILE = machine.hal
HALFILE = LIB:basic.hal
POSTGUI_HALFILE = postgui.hal

[JOINT_0]
TYPE = LINEAR
SCALE = 100
"""

MACHINE = """\
loadrt [KINS]KINEMATICS
loadrt [EMCMOT]EMCMOT servo_period_nsec=[EMCMOT]SERVO_PERIOD num_joints=[KINS]JOINTS
loadusr -W lcec_conf ethercat-conf.xml
loadrt lcec
addf lcec.read-all servo-thread
addf motion-command-handler servo-thread
net x-pos-cmd    <= joint.0.motor-pos-cmd
net x-pos-cmd    => x.pos-cmd
net x-pos-fb     x.pos-fb => joint.0.motor-pos-fb ddt.0.in
net x-enable     joint.0.amp-enable-out \\
                 => x.enable
setp x.pos-scale [JOINT_0]SCALE
net x-fault      x.drv-fault => joint.0.amp-fault-in
net io-out       io.out-1 <= motion.digital-out-00
addf lcec.write-all servo-thread
"""

POSTGUI = "net x-enable => pyvcp.x-led\n"


def cfg():
    c = base_cfg()
    y = copy.deepcopy(c["slaves"][0])
    y.update(position=2, name="y")
    c["slaves"].append(y)
    return c


class Folder(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.lib = tempfile.TemporaryDirectory()
        self.addCleanup(self.lib.cleanup)
        p = mock.patch.object(integrate, "HALLIB", self.lib.name)
        p.start()
        self.addCleanup(p.stop)
        self.write("machine.ini", INI)
        self.write("machine.hal", MACHINE)
        self.write("postgui.hal", POSTGUI)
        with open(os.path.join(self.lib.name, "basic.hal"), "w") as f:
            f.write("net estop iocontrol.0.user-enable-out => iocontrol.0.emc-enable-in\n")
        self.ini = os.path.join(self.dir.name, "machine.ini")

    def write(self, name, text):
        with open(os.path.join(self.dir.name, name), "w") as f:
            f.write(text)

    def read(self, name):
        with open(os.path.join(self.dir.name, name)) as f:
            return f.read()


class TestIni(Folder):
    def test_joints(self):
        self.assertEqual(integrate.ini_joints(self.ini),
                         [(0, "X", "LINEAR"), (1, "Y", "LINEAR"), (2, "Z", "LINEAR")])

    def test_hal_files_from_the_folder_and_the_library(self):
        self.assertEqual(integrate.user_hal_files(self.ini), [
            os.path.join(self.dir.name, "machine.hal"), os.path.join(self.lib.name, "basic.hal"),
            os.path.join(self.dir.name, "postgui.hal")])

    def test_ini_text(self):
        text = integrate.ini_text(self.ini, "ethercat.yaml")
        hal = text[text.index("[HAL]"):text.index("[JOINT_0]")].split("\n")
        # ethercat-load.hal first, ethercat.hal after the last HALFILE,
        # before POSTGUI_HALFILE
        self.assertEqual([x for x in hal if x.startswith("HALFILE") or x.startswith("POSTGUI")], [
            "HALFILE = ethercat-load.hal", "HALFILE = machine.hal", "HALFILE = LIB:basic.hal",
            "HALFILE = ethercat.hal", "POSTGUI_HALFILE = postgui.hal"])
        self.assertIn("[ETHERCAT]\nCONFIG = ethercat.yaml\n\n[HAL]", text)
        # once is enough
        self.write("machine.ini", text)
        self.assertEqual(integrate.ini_text(self.ini, "ethercat.yaml"), text)
        # the file name changes
        self.assertIn("CONFIG = plasma.yaml", integrate.ini_text(self.ini, "plasma.yaml"))


class TestIniInclude(Folder):
    """An INI file that #INCLUDEs the rest, as QtPlasmaC's do: the main file
    has [DISPLAY] and the #INCLUDE line, the included file everything else."""

    def setUp(self):
        super().setUp()
        self.write("machine.inc", INI)
        self.main = "[DISPLAY]\nDISPLAY = axis\n#INCLUDE machine.inc\n"
        self.write("machine.ini", self.main)

    def test_read_through_the_include(self):
        self.assertEqual(integrate.ini_joints(self.ini),
                         [(0, "X", "LINEAR"), (1, "Y", "LINEAR"), (2, "Z", "LINEAR")])
        self.assertEqual(integrate.user_hal_files(self.ini), [
            os.path.join(self.dir.name, "machine.hal"), os.path.join(self.lib.name, "basic.hal"),
            os.path.join(self.dir.name, "postgui.hal")])
        from ecatsetup.session import servo_period
        self.assertEqual(servo_period(self.ini), 1000000)
        self.assertEqual(integrate.ini_includes(self.ini), [os.path.join(self.dir.name, "machine.inc")])

    def test_include_path_with_home_and_variables(self):
        with mock.patch.dict(os.environ, {"MYCFG": self.dir.name}):
            self.write("machine.ini", "[DISPLAY]\n#INCLUDE $MYCFG/machine.inc\n")
            self.assertEqual(len(integrate.ini_joints(self.ini)), 3)

    def test_a_missing_include_is_left_out(self):
        self.write("machine.ini", self.main.replace("machine.inc", "gone.inc"))
        self.assertEqual(integrate.ini_joints(self.ini), [])

    def test_only_the_main_files_includes_count(self):
        # LinuxCNC copies an included file as it is
        self.write("other.inc", "[KINS]\nJOINTS = 5\n")
        self.write("machine.inc", "#INCLUDE other.inc\n" + INI)
        self.assertEqual(len(integrate.ini_joints(self.ini)), 3)

    def test_only_the_first_section_of_a_name_counts(self):
        self.write("machine.ini", self.main + "\n[HAL]\nHALFILE = extra.hal\n")
        self.assertNotIn(os.path.join(self.dir.name, "extra.hal"), integrate.user_hal_files(self.ini))

    def test_the_halfiles_go_into_the_included_file(self):
        texts = integrate.ini_texts(self.ini, "ethercat.yaml")
        inc = os.path.join(self.dir.name, "machine.inc")
        # the main file has no [HAL]: it stays as it was
        self.assertEqual(texts[self.ini], self.main)
        text = texts[inc]
        hal = text[text.index("[HAL]"):text.index("[JOINT_0]")].split("\n")
        self.assertEqual([x for x in hal if x.startswith("HALFILE") or x.startswith("POSTGUI")], [
            "HALFILE = ethercat-load.hal", "HALFILE = machine.hal", "HALFILE = LIB:basic.hal",
            "HALFILE = ethercat.hal", "POSTGUI_HALFILE = postgui.hal"])
        self.assertIn("[ETHERCAT]\nCONFIG = ethercat.yaml\n\n[HAL]", text)
        # once is enough
        self.write("machine.inc", text)
        self.assertEqual(integrate.ini_texts(self.ini, "ethercat.yaml")[inc], text)

    def test_ethercat_section_in_the_main_file_is_used(self):
        self.write("machine.ini", "[ETHERCAT]\nCONFIG = old.yaml\n\n" + self.main)
        texts = integrate.ini_texts(self.ini, "ethercat.yaml")
        self.assertIn("[ETHERCAT]\nCONFIG = ethercat.yaml\n", texts[self.ini])
        self.assertNotIn("[ETHERCAT]", texts[os.path.join(self.dir.name, "machine.inc")])

    def test_plan_and_apply_change_the_included_file(self):
        p = integrate.plan(self.ini, cfg())
        self.assertEqual(p.errors, [])
        inc = os.path.join(self.dir.name, "machine.inc")
        self.assertIn(inc, [c.path for c in p.changes])
        self.assertNotIn(self.ini, [c.path for c in p.changes])
        integrate.apply(p)
        self.assertIn("HALFILE = ethercat-load.hal", self.read("machine.inc"))
        self.assertEqual(self.read("machine.ini"), self.main)

    def test_the_expanded_file_is_refused(self):
        exp = self.ini + ".expanded"
        self.write("machine.ini.expanded", INI)
        self.assertEqual(integrate.expanded_source(exp), self.ini)
        self.assertIsNone(integrate.expanded_source(self.ini))
        p = integrate.plan(exp, cfg())
        self.assertTrue(any("machine.ini instead" in e for e in p.errors), p.errors)
        self.assertEqual(p.changes, [])


class TestPlan(Folder):
    def test_proposed_from_the_hal_files(self):
        c = cfg()
        self.assertEqual(integrate.propose_joints(self.ini, c), {0: ("x", 100.0)})
        integrate.set_joint(c, 0, "x", 100.0)
        self.assertEqual(integrate.propose_joints(self.ini, c), {})

    def test_plan_and_apply(self):
        c = cfg()
        integrate.set_joint(c, 0, "x", 100.0)
        integrate.set_joint(c, 1, "y", 50.0)
        self.assertEqual(config.check(c), [])
        p = integrate.plan(self.ini, c)
        self.assertEqual(p.errors, [])
        self.assertEqual([(ch.name, ch.kind) for ch in p.changes], [
            ("machine.hal", "change"), ("ethercat-load.hal", "new"), ("ethercat.hal", "new"),
            ("machine.ini", "change")])
        integrate.apply(p)
        machine = self.read("machine.hal").splitlines()
        commented = [x[len(integrate.COMMENT):] for x in machine
                     if x.startswith(integrate.COMMENT)]
        # lcec's lines and the joint's connections, both lines of a continued
        # statement, and the scale; not motion's lines or other nets
        self.assertEqual(commented, [
            "loadusr -W lcec_conf ethercat-conf.xml", "loadrt lcec",
            "addf lcec.read-all servo-thread",
            "net x-pos-cmd    <= joint.0.motor-pos-cmd", "net x-pos-cmd    => x.pos-cmd",
            "net x-pos-fb     x.pos-fb => joint.0.motor-pos-fb ddt.0.in",
            "net x-enable     joint.0.amp-enable-out \\", "                 => x.enable",
            "setp x.pos-scale [JOINT_0]SCALE",
            "net x-fault      x.drv-fault => joint.0.amp-fault-in",
            "addf lcec.write-all servo-thread"])
        self.assertIn("net io-out       io.out-1 <= motion.digital-out-00", machine)
        main = self.read("ethercat.hal")
        # the user's signal names are kept, so that postgui.hal's x-enable
        # and ddt.0.in still work
        for line in ("addf ecat.read-all  servo-thread 1", "addf ecat.write-all servo-thread",
                     "setp x.pos-scale 100.0",
                     "net x-pos-cmd joint.0.motor-pos-cmd => x.pos-cmd",
                     "net x-pos-fb x.pos-fb => joint.0.motor-pos-fb",
                     "net x-enable joint.0.amp-enable-out => x.enable",
                     "net x-fault x.drv-fault => joint.0.amp-fault-in",
                     "net x-pos-fb ddt.0.in",
                     # joint 1 had no connections: new names
                     "net y-pos-cmd joint.1.motor-pos-cmd => y.pos-cmd",
                     "net y-amp-fault y.drv-fault => joint.1.amp-fault-in"):
            self.assertIn(line + "\n", main)
        self.assertIn("loadrt ecat config=[ETHERCAT]CONFIG", self.read("ethercat-load.hal"))
        self.assertEqual(self.read("machine.hal.bak"), MACHINE)
        self.assertEqual(self.read("postgui.hal"), POSTGUI)
        # done once: nothing more to change, the signal names stay
        again = integrate.plan(self.ini, c)
        self.assertEqual((again.changes, again.errors), ([], []))

    def test_joint_taken_back(self):
        c = cfg()
        integrate.set_joint(c, 0, "x", 100.0)
        integrate.apply(integrate.plan(self.ini, c))
        integrate.set_joint(c, 0, None)
        p = integrate.plan(self.ini, c)
        what = [ch.what for ch in p.changes if ch.name == "machine.hal"]
        self.assertEqual(what, ["4 lines commented out, 7 back as you wrote them"])
        integrate.apply(p)
        machine = self.read("machine.hal")
        # the joint's lines are back; lcec's stay out
        self.assertIn("\nnet x-pos-cmd    <= joint.0.motor-pos-cmd\n", machine)
        self.assertIn("\nsetp x.pos-scale [JOINT_0]SCALE\n", machine)
        self.assertIn(integrate.COMMENT + "loadrt lcec\n", machine)
        self.assertNotIn("joint.0", self.read("ethercat.hal"))

    def test_without_joints(self):
        p = integrate.plan(self.ini, cfg())
        self.assertEqual(p.errors, [])
        integrate.apply(p)
        self.assertNotIn("net ", self.read("ethercat.hal"))
        # the joints' connections in the user's file stay as they were
        self.assertIn("\nnet x-pos-cmd    <= joint.0.motor-pos-cmd\n", self.read("machine.hal"))

    def test_a_component_between_joint_and_drive(self):
        self.write("machine.hal", "net a joint.0.motor-pos-cmd => filter.in\n"
                                  "net b filter.out => x.pos-cmd\n")
        c = cfg()
        integrate.set_joint(c, 0, "x", 1.0)
        p = integrate.plan(self.ini, c)
        self.assertTrue(any("different signals" in e for e in p.errors))
        with self.assertRaises(ValueError):
            integrate.apply(p)

    def test_library_file_is_copied_not_changed(self):
        lib = os.path.join(self.lib.name, "basic.hal")
        with open(lib, "w") as f:
            f.write("net x-fault x.drv-fault => joint.0.amp-fault-in\n")
        c = cfg()
        integrate.set_joint(c, 0, "x", 1.0)
        p = integrate.plan(self.ini, c)
        self.assertEqual(p.errors, [])
        copy = next(ch for ch in p.changes if ch.name == "basic.hal")
        self.assertEqual((copy.kind, os.path.dirname(copy.path)), ("new", self.dir.name))
        self.assertIn(integrate.COMMENT + "net x-fault", copy.new)
        integrate.apply(p)
        # LIB:basic.hal now names the copy; LinuxCNC's file is as it was
        self.assertIn("HALFILE = basic.hal\n", self.read("machine.ini"))
        self.assertNotIn("LIB:basic.hal", self.read("machine.ini"))
        with open(lib) as f:
            self.assertEqual(f.read(), "net x-fault x.drv-fault => joint.0.amp-fault-in\n")
        again = integrate.plan(self.ini, c)
        self.assertEqual((again.changes, again.errors), ([], []))

    def test_missing_hal_file(self):
        self.write("machine.ini", INI.replace("HALFILE = machine.hal", "HALFILE = gone.hal"))
        self.assertTrue(any("gone.hal" in e for e in integrate.plan(self.ini, cfg()).errors))

    def test_joint_checks(self):
        c = cfg()
        integrate.set_joint(c, 0, "io", 1.0)
        self.assertTrue(any("not a cia402 drive" in e for e in config.check(c)))
        c = cfg()
        c["linuxcnc"] = {"joints": [{"joint": 0, "slave": "x"}, {"joint": 1, "slave": "x"}]}
        self.assertTrue(any("another joint" in e for e in config.check(c)))
        c["linuxcnc"] = {"joints": [{"joint": 7, "slave": "x", "scale": 1.0}]}
        self.assertEqual(config.check(c), [])
        self.assertTrue(any("3 joints" in e for e in integrate.plan(self.ini, c).errors))

    def test_rename(self):
        c = cfg()
        integrate.set_joint(c, 0, "x", 2.0)
        integrate.rename_slave(c, "x", "lcec.0.X")
        self.assertEqual(integrate.joints(c), [{"joint": 0, "slave": "lcec.0.X", "scale": 2.0}])
        integrate.set_joint(c, 0, None)
        self.assertNotIn("linuxcnc", c)

    def test_yaml_round_trip(self):
        c = cfg()
        integrate.set_joint(c, 0, "x", 100.0)
        self.assertIn("  - {joint: 0, slave: x, scale: 100.0}\n", config.dumps(c))


if __name__ == "__main__":
    unittest.main()


try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5.QtWidgets import QApplication, QMessageBox
    from ecatsetup import gui, session
except ImportError:     # pragma: no cover
    gui = None


@unittest.skipIf(gui is None, "no PyQt5")
class TestHalPage(Folder):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_take_over_and_write(self):
        s = session.Session(ini_path=self.ini)
        s.cfg, s.path = cfg(), os.path.join(self.dir.name, "ethercat.yaml")
        w = gui.MainWindow(s)
        self.addCleanup(lambda: (s.__setattr__("saved", s.cfg), w.close()))
        w.tabs.setCurrentWidget(w.hal_page)
        page = w.hal_page
        page.refresh()
        self.assertEqual([r.joint for r in page.rows], [0, 1, 2])
        self.assertFalse(page.take_over.isHidden())
        page.do_take_over()
        self.assertEqual(integrate.joints(s.cfg), [{"joint": 0, "slave": "x", "scale": 100.0}])
        self.assertEqual(page.rows[0].drive.currentData(), "x")
        # a drive chosen in the window
        row = page.rows[1]
        row.drive.setCurrentIndex(row.drive.findData("y"))
        self.assertEqual(integrate.joints(s.cfg)[1]["slave"], "y")
        self.assertTrue(page.write_button.isEnabled())
        self.assertIn("net x-pos-cmd joint.0.motor-pos-cmd => x.pos-cmd",
                      page.code.toPlainText())
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            page.write()
        self.assertEqual(config.load(s.path)["linuxcnc"]["joints"][1]["slave"], "y")
        self.assertTrue(os.path.exists(os.path.join(self.dir.name, "ethercat.hal")))
        self.assertIn("HALFILE = ethercat-load.hal", self.read("machine.ini"))
        self.assertFalse(s.dirty)
        # written: nothing more to change
        self.assertEqual(page.plan.changes, [])

    def test_scale_calculator(self):
        s = session.Session(ini_path=self.ini)
        s.cfg, s.path = cfg(), os.path.join(self.dir.name, "ethercat.yaml")
        integrate.set_joint(s.cfg, 0, "x", 1.0)
        w = gui.MainWindow(s)
        self.addCleanup(lambda: (s.__setattr__("saved", s.cfg), w.close()))
        w.tabs.setCurrentWidget(w.hal_page)
        w.hal_page.refresh()
        row = w.hal_page.rows[0]
        # the defaults: 10000 counts per revolution, 5 units per revolution
        with mock.patch.object(gui.QDialog, "exec_", return_value=gui.QDialog.Accepted):
            row.calculate()
        self.assertEqual(integrate.joints(s.cfg)[0]["scale"], 2000.0)

    def test_connect_pins(self):
        s = session.Session(ini_path=self.ini)
        s.cfg, s.path = cfg(), os.path.join(self.dir.name, "ethercat.yaml")
        w = gui.MainWindow(s)
        self.addCleanup(lambda: (s.__setattr__("saved", s.cfg), w.close()))
        w.tabs.setCurrentWidget(w.hal_page)
        page = w.hal_page
        page.refresh()
        self.assertFalse(page.take_over_io.isHidden())
        self.assertIn("io-out (io.out-1)", page.take_over_io_text.text())
        page.do_take_over_io()
        self.assertEqual(integrate.nets(s.cfg), [
            {"signal": "io-out", "pins": ["io.out-1", "motion.digital-out-00"]}])
        self.assertTrue(page.take_over_io.isHidden())
        r = next(r for r in range(page.pins.rowCount()) if page.pins.item(r, 2).text() == "io.out-1")
        box = page.pins.cellWidget(r, 5)
        self.assertEqual(box.currentText(), "motion.digital-out-00")
        # only bit pins LinuxCNC writes are offered for a slave's output
        offered = [box.itemText(i) for i in range(box.count())]
        self.assertIn("iocontrol.0.user-enable-out", offered)
        self.assertNotIn("motion.digital-in-00", offered)
        self.assertNotIn("motion.analog-out-00", offered)
        box.setEditText("spindle.0.on")
        page.pin_edited("io.out-1", box)
        self.app.processEvents()
        self.assertEqual(integrate.nets(s.cfg), [
            {"signal": "io-out", "pins": ["io.out-1", "spindle.0.on"]}])
        self.assertIn("net io-out spindle.0.on => io.out-1", page.code.toPlainText())
        self.assertTrue(s.dirty)


class TestNets(Folder):
    def test_proposed_from_the_hal_files(self):
        c = cfg()
        # io.out-1 in machine.hal; basic.hal's estop has no slave pin
        self.assertEqual(integrate.propose_nets(self.ini, c),
                         {"io-out": ["io.out-1", "motion.digital-out-00"]})
        integrate.set_net(c, "io.out-1", ["motion.digital-out-00"], "io-out")
        self.assertEqual(integrate.propose_nets(self.ini, c), {})

    def test_not_proposed_when_a_joint_has_it(self):
        self.write("machine.hal", "net x-enable joint.0.amp-enable-out => x.enable io.out-1\n")
        c = cfg()
        integrate.set_joint(c, 0, "x", 1.0)
        self.assertEqual(integrate.propose_nets(self.ini, c), {})

    def test_set_net(self):
        c = cfg()
        integrate.set_net(c, "io.out-1", ["motion.digital-out-00", "motion.digital-out-00"])
        self.assertEqual(integrate.nets(c), [
            {"signal": "io-out-1", "pins": ["io.out-1", "motion.digital-out-00"]}])
        # another pin keeps the signal name
        integrate.set_net(c, "io.out-1", ["iocontrol.0.user-enable-out"])
        self.assertEqual(integrate.nets(c), [
            {"signal": "io-out-1", "pins": ["io.out-1", "iocontrol.0.user-enable-out"]}])
        integrate.set_net(c, "io.out-1", [])
        self.assertNotIn("linuxcnc", c)

    def test_rename_follows(self):
        c = cfg()
        integrate.set_net(c, "io.out-1", ["motion.digital-out-00"])
        integrate.rename_slave(c, "io", "board")
        self.assertEqual(integrate.nets(c)[0]["pins"], ["board.out-1", "motion.digital-out-00"])

    def test_plan_keeps_the_users_signal(self):
        c = cfg()
        integrate.set_net(c, "io.out-1", ["motion.digital-out-00"], "my-name")
        self.assertEqual(config.check(c), [])
        p = integrate.plan(self.ini, c)
        self.assertEqual(p.errors, [])
        self.assertIn("signal my-name is called io-out in your HAL files; that name is kept",
                      p.notes)
        integrate.apply(p)
        self.assertIn("net io-out motion.digital-out-00 => io.out-1\n", self.read("ethercat.hal"))
        self.assertIn(integrate.COMMENT + "net io-out       io.out-1 <= motion.digital-out-00",
                      self.read("machine.hal"))
        again = integrate.plan(self.ini, c)
        self.assertEqual((again.changes, again.errors), ([], []))
        # taken out again: the user's line comes back
        integrate.set_net(c, "io.out-1", [])
        integrate.apply(integrate.plan(self.ini, c))
        self.assertIn("\nnet io-out       io.out-1 <= motion.digital-out-00\n",
                      self.read("machine.hal"))
        self.assertNotIn("io.out-1", self.read("ethercat.hal"))

    def test_new_signal_name_in_use(self):
        self.write("machine.hal", "net x-enable joint.0.amp-enable-out => x.enable\n")
        c = cfg()
        integrate.set_net(c, "io.out-1", ["motion.digital-out-01"], "x-enable")
        p = integrate.plan(self.ini, c)
        self.assertIn("signal x-enable is used in your HAL files already: choose another name "
                      "or connect it there", p.errors)

    def test_a_postgui_reader_stays_on_the_signal(self):
        # the slave's pin exists before the GUI: ethercat.hal connects it, and
        # the GUI's LED goes on reading the signal after the GUI is up
        self.write("machine.hal", MACHINE.replace("net io-out       io.out-1 <= motion.digital-out-00\n", ""))
        self.write("postgui.hal", "net led io.out-1 => pyvcp.led\n")
        c = cfg()
        integrate.set_net(c, "io.out-1", ["motion.digital-out-01"], "led")
        p = integrate.plan(self.ini, c)
        self.assertEqual(p.errors, [])
        new = next(ch.new for ch in p.changes if ch.path.endswith("postgui.hal"))
        self.assertEqual(new, integrate.COMMENT + "net led io.out-1 => pyvcp.led\n"
                         "net led => pyvcp.led" + integrate.ADDED + "\n")
        main = next(ch.new for ch in p.changes if ch.path.endswith(integrate.MAIN_FILE))
        self.assertIn("net led motion.digital-out-01 => io.out-1", main)
        self.assertNotIn("pyvcp", main)
        # once is enough
        integrate.apply(p)
        self.assertNotIn(os.path.join(self.dir.name, "postgui.hal"),
                         [ch.path for ch in integrate.plan(self.ini, c).changes])

    def test_postgui_pin_is_refused(self):
        # a pin only the GUI has cannot be connected before it
        c = cfg()
        integrate.set_net(c, "io.out-1", ["pyvcp.led"])
        self.write("postgui.hal", "net led pyvcp.led => pyvcp.other\n")
        p = integrate.plan(self.ini, c)
        self.assertTrue(any("postgui.hal (a POSTGUI file" in e for e in p.errors), p.errors)

    def test_check(self):
        c = cfg()
        lc = c.setdefault("linuxcnc", {})
        lc["nets"] = [{"signal": "a b", "pins": ["io.out-1"]}]
        self.assertEqual(config.check(c), [
            "linuxcnc.nets: a b: signal must be a HAL name (no blanks or #, at most 47 characters)",
            "linuxcnc.nets: a b: pins must be a list of at least two pin names"])
        lc["nets"] = [{"signal": "a", "pins": ["io.out-2", "motion.digital-in-00"]}]
        self.assertEqual(config.check(c), ["linuxcnc.nets: a: io.out-2 is not a pin of slave io"])
        lc["nets"] = [{"signal": "a", "pins": ["motion.digital-out-00", "halui.machine.on"]}]
        self.assertEqual(config.check(c), ["linuxcnc.nets: a: none of its pins is a slave's pin"])
        lc["nets"] = [{"signal": "a", "pins": ["io.out-1", "motion.analog-out-00"]}]
        self.assertEqual(config.check(c), ["linuxcnc.nets: a: pins of different HAL types "
                                           "(bool, real)"])
        lc["nets"] = [{"signal": "a", "pins": ["io.out-1", "motion.digital-out-00",
                                               "halui.machine.is-on"]}]
        self.assertEqual(config.check(c), ["linuxcnc.nets: a: more than one pin writes it "
                                           "(motion.digital-out-00, halui.machine.is-on)"])
        lc["nets"] = [{"signal": "a", "pins": ["io.out-1", "motion.digital-out-00"]},
                      {"signal": "b", "pins": ["io.out-1", "motion.digital-out-01"]}]
        self.assertEqual(config.check(c), ["linuxcnc.nets: b: io.out-1 is in signal a too"])
        # a profile's pins are not known here; a joint's are taken
        integrate.set_joint(c, 0, "x", 1.0)
        lc["nets"] = [{"signal": "a", "pins": ["x.enable", "motion.digital-out-00"]},
                      {"signal": "b", "pins": ["x.drv-din-0", "motion.digital-in-00"]}]
        self.assertEqual(config.check(c), ["linuxcnc.nets: a: x.enable is connected by joint 0"])


class TestAxes(Folder):
    def cfg(self):
        c = cfg()
        x = c["slaves"][0]
        x["cia402"]["axes"] = ["x-1", "x-2"]
        for kind, objs in (("outputs", ("0x6840:00", "0x687A:00")), ("inputs", ("0x6841:00", "0x6864:00"))):
            x["process-data"][kind][0]["entries"] += [
                {"object": o, "bits": 16 if o.endswith("40:00") or o.endswith("41:00") else 32,
                 "type": "UINT" if o.endswith("40:00") or o.endswith("41:00") else "DINT",
                 "use": "profile"} for o in objs]
        return c

    def test_joints_on_axes(self):
        c = self.cfg()
        self.assertEqual(config.check(c), [])
        self.assertEqual(sorted(integrate.drives(c)), ["x-1", "x-2", "y"])
        integrate.set_joint(c, 0, "x-1", 100.0)
        integrate.set_joint(c, 1, "x-2", 50.0)
        self.assertEqual(config.check(c), [])
        p = integrate.plan(self.ini, c)
        self.assertEqual(p.errors, [])
        main = next(ch.new for ch in p.changes if ch.name == "ethercat.hal")
        self.assertIn("net y-pos-cmd joint.1.motor-pos-cmd => x-2.pos-cmd", main)
        self.assertIn("setp x-1.pos-scale 100.0", main)
        # the slave renamed: its axes and their joints follow
        c["slaves"][0]["name"] = "gantry"
        integrate.rename_slave(c, "x", "gantry")
        self.assertEqual(c["slaves"][0]["cia402"]["axes"], ["gantry-1", "gantry-2"])
        self.assertEqual([j["slave"] for j in integrate.joints(c)], ["gantry-1", "gantry-2"])
        self.assertEqual(config.check(c), [])


CORE_SIM = """\
loadrt [KINS]KINEMATICS
loadrt [EMCMOT]EMCMOT servo_period_nsec=[EMCMOT]SERVO_PERIOD num_joints=[KINS]JOINTS
addf motion-command-handler servo-thread
addf motion-controller servo-thread
net Xpos joint.0.motor-pos-cmd => joint.0.motor-pos-fb ddt_x.in
net estop-loop iocontrol.0.user-enable-out iocontrol.0.emc-enable-in
"""
SIM_HOME = """\
loadrt or2 names=or2_0
net Xhomesw => or2_0.in0
net Zhomesw => or2_0.in1
net XZhomesw or2_0.out => joint.0.home-sw-in joint.2.home-sw-in
"""


class TestFromSimulation(Folder):
    def setUp(self):
        super().setUp()
        for name, text in (("core_sim.hal", CORE_SIM), ("simulated_home.hal", SIM_HOME)):
            with open(os.path.join(self.lib.name, name), "w") as f:
                f.write(text)
        self.write("machine.ini", INI.replace("HALFILE = machine.hal\n", "HALFILE = core_sim.hal\n"
                                              "HALFILE = simulated_home.hal\n")
                   .replace("HALFILE = LIB:basic.hal\n", "").replace("POSTGUI_HALFILE = postgui.hal\n", ""))

    def cfg(self):
        c = cfg()
        c["slaves"][1]["process-data"]["inputs"] = [{"pdo": "0x1A00", "sm": 3, "entries": [
            {"object": "0x6000:01", "bits": 1, "type": "BOOL", "pin": "in-1", "hal-type": "bool"},
            {"object": "0x6000:02", "bits": 1, "type": "BOOL", "pin": "in-2", "hal-type": "bool"}]}]
        integrate.set_joint(c, 0, "x", 100.0)
        integrate.set_net(c, "io.in-1", ["joint.0.home-sw-in"])
        integrate.set_net(c, "io.in-2", ["iocontrol.0.emc-enable-in"])
        self.assertEqual(config.check(c), [])
        return c

    def test_simulation_replaced(self):
        p = integrate.plan(self.ini, self.cfg())
        self.assertEqual(p.errors, [])
        integrate.apply(p)
        main = self.read("ethercat.hal")
        # the loop-back's command signal keeps ddt; the feedback is the drive's
        self.assertIn("net Xpos joint.0.motor-pos-cmd => x.pos-cmd\n", main)
        self.assertIn("net x-pos-fb x.pos-fb => joint.0.motor-pos-fb\n", main)
        self.assertIn("net Xpos ddt_x.in\n", main)
        # real inputs instead of the simulation's writers, and Z's switch left alone
        self.assertIn("net estop-loop io.in-2 => iocontrol.0.emc-enable-in\n", main)
        self.assertIn("net XZhomesw io.in-1 => joint.0.home-sw-in\n", main)
        self.assertNotIn("user-enable-out", main)
        self.assertNotIn("or2_0.out", main)
        self.assertNotIn("joint.2.home-sw-in", main)
        notes = "\n".join(p.notes)
        self.assertIn("joint 0: the simulation looped motor-pos-cmd back", notes)
        self.assertIn("joint.2.home-sw-in left unconnected", notes)
        self.assertIn("simulated_home.hal still simulates home switches", notes)
        # copies in the folder; LinuxCNC's files unchanged
        self.assertIn(integrate.COMMENT + "net Xpos", self.read("core_sim.hal"))
        with open(os.path.join(self.lib.name, "core_sim.hal")) as f:
            self.assertEqual(f.read(), CORE_SIM)
        again = integrate.plan(self.ini, self.cfg())
        self.assertEqual((again.changes, again.errors), ([], []))

    def test_basic_sim(self):
        self.write("machine.ini", INI.replace("HALFILE = machine.hal\n",
                                              "HALFILE = LIB:basic_sim.tcl -no_sim_spindle\n"))
        p = integrate.plan(self.ini, self.cfg())
        self.assertTrue(any("start this configuration once in the simulator" in e
                            and "machine_cmds.hal" in e for e in p.errors), p.errors)
        # what basic_sim.tcl saved when it ran
        self.write("machine_cmds.hal", "net J0:pos-fb joint.0.motor-pos-cmd => joint.0.motor-pos-fb "
                                       "J0_switch.cur-pos\nnet J0:homesw J0_switch.home-sw => "
                                       "joint.0.home-sw-in\n")
        p = integrate.plan(self.ini, self.cfg())
        self.assertEqual(p.errors, [])
        integrate.apply(p)
        self.assertIn("HALFILE = machine_cmds.hal\n", self.read("machine.ini"))
        main = self.read("ethercat.hal")
        self.assertIn("net J0:pos-fb joint.0.motor-pos-cmd => x.pos-cmd\n", main)
        self.assertIn("net J0:homesw io.in-1 => joint.0.home-sw-in\n", main)
        self.assertNotIn("J0_switch.home-sw", main)
        self.assertIn("net J0:pos-fb J0_switch.cur-pos\n", main)


class TestJointChecks(Folder):
    def test_scale_from(self):
        self.assertAlmostEqual(integrate.scale_from(10000, 5), 2000.0)
        self.assertAlmostEqual(integrate.scale_from(10000, 10, gear=3), 3000.0)
        with self.assertRaises(ValueError):
            integrate.scale_from(10000, 0)

    def test_joint_checks(self):
        c = cfg()
        integrate.set_joint(c, 0, "x", 100.0)
        self.write("machine.ini", INI + "MAX_VELOCITY = 50\nFERROR = 1\n")
        notes = integrate.joint_checks(self.ini, c)
        self.assertEqual(notes, ["[JOINT_0]MIN_FERROR is not set: LinuxCNC's default is used; set "
                                 "it for the machine (in machine units)"])
        # 50 mm/s at 100 counts/mm is 5000 counts/s
        notes = integrate.joint_checks(self.ini, c, {"x": 4000})
        self.assertIn("[JOINT_0]MAX_VELOCITY 50 units/s is 5000 counts/s, more than x's maximum "
                      "profile velocity (0x607F) of 4000 counts/s", notes)
        self.assertEqual(len(integrate.joint_checks(self.ini, c, {"x": 6000})), 1)
        self.write("machine.ini", INI.replace("SERVO_PERIOD = 1000000\n", ""))
        self.assertTrue(any("SERVO_PERIOD is not set" in n
                            for n in integrate.joint_checks(self.ini, c)))
