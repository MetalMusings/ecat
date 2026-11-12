"""Tests of "Still to connect" (wiring.py) and of writing what it connects,
on a made-up configuration like QtPlasmaC's simulator.

Run with: python3 -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["ECATSETUP_DEVICES"] = os.path.join(os.path.dirname(__file__), "no-such-dir")
os.environ["ECATSETUP_SETTINGS"] = os.path.join(os.path.dirname(__file__), "no-such-dir", "settings.yaml")
# state (test lock, drive limits): never the user's own
if "ECATSETUP_TEST_STATE" not in os.environ:
    os.environ["ECATSETUP_TEST_STATE"] = os.environ["XDG_STATE_HOME"] = \
        __import__("tempfile").mkdtemp(prefix="ecatsetup-test-state-")
sys.path.insert(0, os.path.dirname(__file__))

from ecatsetup import integrate, wiring  # noqa: E402
from test_integrate import cfg as base_cfg  # noqa: E402

INI = """\
[EMCMOT]
SERVO_PERIOD = 1000000

[KINS]
JOINTS = 1
KINEMATICS = trivkins coordinates=X

[HAL]
HALFILE = sim.hal
POSTGUI_HALFILE = sim_postgui.hal

[JOINT_0]
TYPE = LINEAR
HOME_SEARCH_VEL = 1.0
"""

SIM = """\
loadrt plasmac
loadrt dbounce names=db_arc-ok
loadrt sim_home_switch names=sim-joint.0.home-switch
loadrt or2 names=estop_or
loadrt not names=estop_not
net sim:homesw  sim-joint.0.home-switch.home-sw => joint.0.home-sw-in
net plasmac:arc-ok  db_arc-ok.out => plasmac.arc-ok-in
net sim:estop-raw estop_or.out  => estop_not.in
net sim:estop-out estop_not.out => iocontrol.0.emc-enable-in
net plasmac:feed-hold plasmac.feed-hold => motion.feed-hold
net plasmac:torch-on  plasmac.torch-on
"""

POSTGUI = """\
loadusr -Wn qtplasmac_sim qtvcp qtplasmac_sim.ui
loadusr -Wn sim-torch sim-torch
net sim:arc-ok      qtplasmac_sim.arc_ok  =>  db_arc-ok.in
net plasmac:torch-on   =>  qtplasmac_sim.torch_on sim-torch.start
"""


def cfg():
    c = base_cfg()
    io = c["slaves"][1]["process-data"]
    io["inputs"] = [{"pdo": "0x1A00", "sm": 3, "entries": [
        {"object": "0x6000:01", "bits": 1, "type": "BOOL", "pin": "in-1", "hal-type": "bit"},
        {"object": "0x6000:02", "bits": 1, "type": "BOOL", "pin": "in-2", "hal-type": "bit"}]}]
    return c


class TestWiring(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        lib = tempfile.TemporaryDirectory()
        self.addCleanup(lib.cleanup)
        p = mock.patch.object(integrate, "HALLIB", lib.name)
        p.start()
        self.addCleanup(p.stop)
        for name, text in (("machine.ini", INI), ("sim.hal", SIM), ("sim_postgui.hal", POSTGUI)):
            self.write(name, text)
        self.ini = os.path.join(self.dir.name, "machine.ini")

    def write(self, name, text):
        with open(os.path.join(self.dir.name, name), "w") as f:
            f.write(text)

    def read(self, name):
        with open(os.path.join(self.dir.name, name)) as f:
            return f.read()

    def rows(self, c=None):
        return {r.pin: r for r in wiring.rows(self.ini, c or cfg())}

    def test_what_the_simulation_drives(self):
        rows = self.rows()
        # through the debounce: the slave's pin goes where the simulation's did
        r = rows["plasmac.arc-ok-in"]
        self.assertEqual((r.status, r.target), ("simulated", "db_arc-ok.in"))
        self.assertEqual(r.where, "sim_postgui.hal:3")
        self.assertEqual(rows["joint.0.home-sw-in"].status, "simulated")
        # an output only the simulation reads
        r = rows["plasmac.torch-on"]
        self.assertEqual((r.way, r.status, r.target), ("out", "simulated", "plasmac.torch-on"))
        # not through a not or an or2: the E-stop is connected where LinuxCNC reads it
        r = rows["iocontrol.0.emc-enable-in"]
        self.assertEqual((r.status, r.target), ("open", "iocontrol.0.emc-enable-in"))
        self.assertIn("the inputs of estop_or", r.now)
        # LinuxCNC's own logic is not listed
        self.assertNotIn("motion.feed-hold", rows)

    def test_a_home_switch_only_when_the_joint_homes_to_one(self):
        self.write("machine.ini", INI.replace("HOME_SEARCH_VEL = 1.0", "HOME_SEARCH_VEL = 0"))
        self.write("sim.hal", SIM.replace("net sim:homesw", "#"))
        self.assertNotIn("joint.0.home-sw-in", self.rows())
        self.write("machine.ini", INI)
        self.assertEqual(self.rows()["joint.0.home-sw-in"].status, "open")

    def test_connect_and_write(self):
        c = cfg()
        rows = self.rows(c)
        wiring.connect(c, rows["plasmac.arc-ok-in"], "io.in-1")
        wiring.connect(c, rows["plasmac.torch-on"], "io.out-1")
        rows = self.rows(c)
        self.assertEqual((rows["plasmac.arc-ok-in"].status, rows["plasmac.arc-ok-in"].slave),
                         ("connected", "io.in-1"))
        self.assertEqual(rows["plasmac.torch-on"].slave, "io.out-1")
        p = integrate.plan(self.ini, c)
        self.assertEqual(p.errors, [])
        main = next(ch.new for ch in p.changes if ch.path.endswith(integrate.MAIN_FILE))
        # the user's signal names, the debounce kept, and no GUI pin before the GUI
        self.assertIn("net sim:arc-ok io.in-1 => db_arc-ok.in", main)
        self.assertIn("net plasmac:torch-on plasmac.torch-on => io.out-1", main)
        self.assertNotIn("qtplasmac_sim", main)
        self.assertIn(("sim_postgui.hal", 3, "net sim:arc-ok      qtplasmac_sim.arc_ok  =>  db_arc-ok.in"),
                      p.commented)
        self.assertTrue(any("qtplasmac_sim.arc_ok no longer connected" in n for n in p.notes), p.notes)
        integrate.apply(p)
        self.assertIn(integrate.COMMENT + "net sim:arc-ok", self.read("sim_postgui.hal"))
        # the simulation's torch still listens to the signal, after the GUI
        self.assertIn("net plasmac:torch-on   =>  qtplasmac_sim.torch_on", self.read("sim_postgui.hal"))

    def test_the_old_writer_goes_by_the_arrows(self):
        # a writer whose name says nothing (not .out): the arrow says it writes
        self.write("sim.hal", SIM.replace("sim-joint.0.home-switch.home-sw =>", "sim-sw.state =>")
                   + "loadrt sim_switch names=sim-sw\n")
        c = cfg()
        wiring.connect(c, self.rows(c)["joint.0.home-sw-in"], "io.in-2")
        p = integrate.plan(self.ini, c)
        self.assertEqual(p.errors, [])
        main = next(ch.new for ch in p.changes if ch.path.endswith(integrate.MAIN_FILE))
        self.assertIn("net sim:homesw io.in-2 => joint.0.home-sw-in", main)
        self.assertNotIn("sim-sw.state", main)

    def test_a_gui_pin_reading_the_signal_is_not_moved_before_the_gui(self):
        self.write("sim_postgui.hal", POSTGUI.replace("=>  db_arc-ok.in", "=>  db_arc-ok.in qtplasmac_sim.led"))
        c = cfg()
        wiring.connect(c, self.rows(c)["plasmac.arc-ok-in"], "io.in-1")
        p = integrate.plan(self.ini, c)
        self.assertEqual(p.errors, [])
        main = next(ch.new for ch in p.changes if ch.path.endswith(integrate.MAIN_FILE))
        self.assertNotIn("qtplasmac_sim", main)
        # the GUI's LED reads the signal after the GUI is up
        post = next(ch.new for ch in p.changes if ch.path.endswith("sim_postgui.hal"))
        self.assertIn("net sim:arc-ok => qtplasmac_sim.led" + integrate.ADDED, post)
        self.assertTrue(any("qtplasmac_sim.arc_ok no longer connected" in n for n in p.notes))

    def test_another_slave_pin_takes_the_place(self):
        c = cfg()
        r = self.rows(c)["plasmac.arc-ok-in"]
        wiring.connect(c, r, "io.in-1")
        wiring.connect(c, r, "io.in-2")
        self.assertEqual(integrate.nets(c), [{"signal": "io-in-2", "pins": ["io.in-2", "db_arc-ok.in"]}])
        wiring.connect(c, r, "")
        self.assertEqual(integrate.nets(c), [])

    def test_a_gui_pin_cannot_be_connected_before_the_gui(self):
        c = cfg()
        integrate.set_net(c, "io.in-1", ["qtplasmac_sim.arc_ok"])
        p = integrate.plan(self.ini, c)
        self.assertTrue(any("POSTGUI" in e and "qtplasmac_sim.arc_ok" in e for e in p.errors), p.errors)

    def test_simulation_names(self):
        g = wiring.graph(self.ini, cfg())
        for pin, sim in (("sim-torch.start", True), ("qtplasmac_sim.arc_ok", True),
                         ("sim-joint.0.home-switch.home-sw", True), ("plasmac.torch-on", False),
                         ("db_arc-ok.out", False), ("estop_not.out", False)):
            self.assertEqual(wiring.is_sim(g, pin), sim, pin)


if __name__ == "__main__":
    unittest.main()
