"""Tests of reading HAL files written in Tcl (haltcl.py) and their use in
the plan and the HAL check. Skipped without tclsh.

Run with: python3 -m unittest discover -s tests
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["ECATSETUP_DEVICES"] = os.path.join(os.path.dirname(__file__), "no-such-dir")
os.environ["ECATSETUP_SETTINGS"] = os.path.join(os.path.dirname(__file__), "no-such-dir", "settings.yaml")
# state (test lock, drive limits): never the user's own
if "ECATSETUP_TEST_STATE" not in os.environ:
    os.environ["ECATSETUP_TEST_STATE"] = os.environ["XDG_STATE_HOME"] = \
        __import__("tempfile").mkdtemp(prefix="ecatsetup-test-state-")
sys.path.insert(0, os.path.dirname(__file__))

from ecatsetup import halcheck, haltcl, integrate  # noqa: E402
from test_integrate import cfg  # noqa: E402

MAIN = "[DISPLAY]\nDISPLAY = axis\n#INCLUDE machine.inc\n"

INC = """\
[EMCMOT]
EMCMOT = motmod
SERVO_PERIOD = 1000000

[KINS]
JOINTS = 3
KINEMATICS = trivkins coordinates=XYZ

[HAL]
HALFILE = sim.tcl
HALFILE = machine.hal
"""

# like QtPlasmaC's sim_no_stepgen.tcl
SIM = """\
# a comment
set numJoints $::KINS(JOINTS)
loadrt $::KINS(KINEMATICS)
loadrt $::EMCMOT(EMCMOT) servo_period_nsec=$::EMCMOT(SERVO_PERIOD) num_joints=$numJoints
for {set j 0} {$j < $numJoints} {incr j} {
    loadrt pid names=sim-joint.${j}.pid
    setp sim-joint.${j}.pid.FF0 0.5*2
    net sim:joint${j}.pos-cmd joint.${j}.motor-pos-cmd => sim-joint.${j}.pid.command
}
net sim:estop-out estop_not.out => iocontrol.0.emc-enable-in
"""


@unittest.skipIf(shutil.which(haltcl.TCLSH) is None, "tclsh is not installed")
class TestHalTcl(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        haltcl._cache.clear()
        for name, text in (("machine.ini", MAIN), ("machine.inc", INC), ("sim.tcl", SIM),
                           ("machine.hal", "net estop-in io.in-0 => estop_or.in0\n")):
            self.write(name, text)
        self.ini = self.path("machine.ini")
        self.tcl = self.path("sim.tcl")

    def path(self, name):
        return os.path.join(self.dir.name, name)

    def write(self, name, text):
        with open(self.path(name), "w") as f:
            f.write(text)

    def test_the_hal_it_gives(self):
        x = haltcl.expand(self.tcl, self.ini)
        lines = x.text.splitlines()
        self.assertEqual(x.problems, [])
        # INI values (through the #INCLUDE), without parse_ini's list braces
        self.assertEqual(lines[0], "loadrt trivkins coordinates=XYZ")
        self.assertEqual(lines[1], "loadrt motmod servo_period_nsec=1000000 num_joints=3")
        # the loop, setp's expression, and the Tcl file's line of each
        self.assertEqual(lines[2:5], ["loadrt pid names=sim-joint.0.pid",
                                      "setp sim-joint.0.pid.FF0 1.0",
                                      "net sim:joint0.pos-cmd joint.0.motor-pos-cmd => "
                                      "sim-joint.0.pid.command"])
        self.assertEqual(x.lines[:5], [3, 4, 6, 7, 8])
        self.assertEqual(len(lines), 2 + 3 * 3 + 1)
        self.assertEqual(x.lines[-1], 10)

    def test_queries_and_errors_are_named(self):
        self.write("sim.tcl", "set v [getp motion.enable]\nloadrt pid\nerror boom\nloadrt ddt\n")
        x = haltcl.expand(self.tcl, self.ini)
        self.assertEqual(x.text, "getp motion.enable\nloadrt pid\n")
        self.assertTrue(any("sim.tcl:1 asks HAL (getp" in p for p in x.problems), x.problems)
        self.assertTrue(any("Tcl error" in p and "boom" in p for p in x.problems), x.problems)

    def test_a_changed_file_is_read_again(self):
        haltcl.expand(self.tcl, self.ini)
        self.write("machine.inc", INC.replace("JOINTS = 3", "JOINTS = 1"))
        os.utime(self.path("machine.inc"), ns=(1, 1))
        self.assertEqual(haltcl.expand(self.tcl, self.ini).text.count("loadrt pid"), 1)

    def test_signals_and_the_hal_check_read_it(self):
        sig = integrate.signals(integrate.user_hal_files(self.ini), self.ini)
        self.assertEqual(sig["sim:joint1.pos-cmd"], {"joint.1.motor-pos-cmd", "sim-joint.1.pid.command"})
        pins, _ = halcheck.uses(self.ini)
        self.assertEqual(pins["joint.2.motor-pos-cmd"], [halcheck.Use("sim.tcl", 8, "sim:joint2.pos-cmd")])

    def test_the_plan_refuses_a_tcl_line_it_cannot_comment_out(self):
        c = cfg()
        c.setdefault("linuxcnc", {})["joints"] = [{"joint": 0, "slave": "x", "scale": 100.0}]
        p = integrate.plan(self.ini, c)
        e = [x for x in p.errors if x.startswith("sim.tcl")]
        self.assertEqual(len(e), 1, p.errors)
        self.assertIn("(line 8) connects joint.0.motor-pos-cmd", e[0])
        self.assertNotIn(self.tcl, [ch.path for ch in p.changes])
        # nothing connected here: nothing to refuse
        c["linuxcnc"]["joints"] = []
        self.assertFalse([x for x in integrate.plan(self.ini, c).errors if x.startswith("sim.tcl")])


if __name__ == "__main__":
    unittest.main()
