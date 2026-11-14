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
    # one folder for the whole run: the state, and every temporary file of the
    # tests and of what they start (TMPDIR); removed when the run ends
    os.environ["ECATSETUP_TEST_STATE"] = os.environ["XDG_STATE_HOME"] = os.environ["TMPDIR"] = \
        __import__("tempfile").tempdir = __import__("tempfile").mkdtemp(prefix="ecatsetup-test-state-")
    __import__("atexit").register(__import__("shutil").rmtree, os.environ["ECATSETUP_TEST_STATE"], True)
sys.path.insert(0, os.path.dirname(__file__))

from ecatsetup import config, integrate, wiring  # noqa: E402
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
        {"object": "0x6000:01", "bits": 1, "type": "BOOL", "pin": "in-1", "hal-type": "bool"},
        {"object": "0x6000:02", "bits": 1, "type": "BOOL", "pin": "in-2", "hal-type": "bool"}]}]
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
        self.assertTrue(any("simulation panel" in n for n in p.notes), p.notes)
        integrate.apply(p)
        post = self.read("sim_postgui.hal")
        self.assertIn(integrate.COMMENT + "net sim:arc-ok", post)
        # the panel is gone: its loading line and every line with its pins are commented out;
        # the simulated torch keeps listening to the real signal
        self.assertIn(integrate.COMMENT + "loadusr -Wn qtplasmac_sim", post)
        self.assertIn(integrate.COMMENT + "net plasmac:torch-on   =>  qtplasmac_sim.torch_on", post)
        self.assertIn("net plasmac:torch-on => sim-torch.start" + integrate.ADDED, post)
        self.assertEqual([l for l in post.splitlines()
                          if "qtplasmac_sim" in l and not l.startswith(integrate.COMMENT)], [])

    def test_the_estop_is_an_ordinary_signal(self):
        # no special case: the real input takes the signal, the simulated writer (the
        # estop_not of the simulated chain) gives way, and the chain's other lines stay
        c = cfg()
        wiring.connect(c, self.rows(c)["iocontrol.0.emc-enable-in"], "io.in-1")
        p = integrate.plan(self.ini, c)
        self.assertEqual(p.errors, [])
        main = next(ch.new for ch in p.changes if ch.path.endswith(integrate.MAIN_FILE))
        self.assertIn("net sim:estop-out io.in-1 => iocontrol.0.emc-enable-in", main)
        self.assertNotIn("estop_not", main)
        sim = next(ch.new for ch in p.changes if ch.path.endswith("sim.hal"))
        self.assertIn(integrate.COMMENT + "net sim:estop-out estop_not.out => iocontrol.0.emc-enable-in", sim)
        self.assertIn("net sim:estop-raw estop_or.out  => estop_not.in", sim)
        self.assertNotIn(integrate.COMMENT + "net sim:estop-raw", sim)

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

    def test_the_simulation_panel_is_taken_out_with_its_leds(self):
        self.write("sim_postgui.hal", POSTGUI.replace("=>  db_arc-ok.in", "=>  db_arc-ok.in qtplasmac_sim.led"))
        c = cfg()
        wiring.connect(c, self.rows(c)["plasmac.arc-ok-in"], "io.in-1")
        p = integrate.plan(self.ini, c)
        self.assertEqual(p.errors, [])
        main = next(ch.new for ch in p.changes if ch.path.endswith(integrate.MAIN_FILE))
        self.assertNotIn("qtplasmac_sim", main)
        # the panel is taken out, its LED with it
        post = next(ch.new for ch in p.changes if ch.path.endswith("sim_postgui.hal"))
        self.assertNotIn("qtplasmac_sim.led" + integrate.ADDED, post)
        self.assertEqual([l for l in post.splitlines()
                          if "qtplasmac_sim" in l and not l.startswith(integrate.COMMENT)], [])

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


SPINDLE_INI = """\
[EMCMOT]
SERVO_PERIOD = 1000000

[KINS]
JOINTS = 1
KINEMATICS = trivkins coordinates=X

[HAL]
HALFILE = spindle_sim.hal

[JOINT_0]
TYPE = LINEAR
"""

# as LinuxCNC's spindle_sim.hal: a simulated spindle encoder on the spindle's pins
SPINDLE_SIM = """\
loadrt sim_spindle names=spindle_sim
net spindle-index-enable spindle.0.index-enable <=> spindle_sim.index-enable
net spindle-revs spindle_sim.position-fb => spindle.0.revs
net spindle-speed spindle.0.speed-out => spindle_sim.velocity-cmd
"""


def encoder_cfg():
    """A terminal with an encoder channel (MDP 511), latch C mapped."""
    c = base_cfg()
    io = c["slaves"][1]
    io["profile"] = "mdp"
    io["encoders"] = [{"name": "enc", "index": "0x6010"}]
    io["process-data"] = {
        "outputs": [{"pdo": "0x1603", "sm": 2, "entries": [
            {"object": "0x7010:01", "bits": 1, "type": "BOOL", "use": "profile"},
            {"object": "0x7010:11", "bits": 32, "type": "UDINT", "use": "profile"}]}],
        "inputs": [{"pdo": "0x1A04", "sm": 3, "entries": [
            {"object": "0x6010:01", "bits": 1, "type": "BOOL", "use": "profile"},
            {"object": "0x6010:11", "bits": 32, "type": "UDINT", "use": "profile"},
            {"object": "0x6010:12", "bits": 32, "type": "UDINT", "use": "profile"}]}]}
    return c


class TestEncoderChannelPins(unittest.TestCase):
    """The encoder channel's pins can be connected, the handshake pin (index-enable, both
    ways) too, and a simulated spindle encoder gives way."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        lib = tempfile.TemporaryDirectory()
        self.addCleanup(lib.cleanup)
        p = mock.patch.object(integrate, "HALLIB", lib.name)
        p.start()
        self.addCleanup(p.stop)
        for name, text in (("machine.ini", SPINDLE_INI), ("spindle_sim.hal", SPINDLE_SIM)):
            with open(os.path.join(self.dir.name, name), "w") as f:
                f.write(text)
        self.ini = os.path.join(self.dir.name, "machine.ini")

    def rows(self, c):
        return {r.pin: r for r in wiring.rows(self.ini, c)}

    def test_the_channels_pins_are_pins_to_connect(self):
        own = integrate.slave_pins(encoder_cfg())
        self.assertEqual(own["io.enc.pos"], ("real", "out"))
        self.assertEqual(own["io.enc.index-enable"], ("bool", "io"))
        self.assertEqual(own["io.enc.reset"], ("bool", "in"))
        self.assertNotIn("io.enc.pos-scale", own)                 # a parameter
        self.assertEqual(integrate.known_pin("spindle.0.index-enable"), ("bool", "io"))
        self.assertEqual(integrate.known_pin("spindle.0.revs"), ("real", "in"))

    def test_the_spindle_rows(self):
        rows = self.rows(encoder_cfg())
        r = rows["spindle.0.index-enable"]
        self.assertEqual((r.way, r.status), ("io", "simulated"))
        self.assertIn("spindle_sim.index-enable", r.now)
        r = rows["spindle.0.revs"]
        self.assertEqual((r.way, r.status), ("in", "simulated"))

    def test_connect_and_write(self):
        c = encoder_cfg()
        rows = self.rows(c)
        wiring.connect(c, rows["spindle.0.index-enable"], "io.enc.index-enable")
        wiring.connect(c, rows["spindle.0.revs"], "io.enc.pos")
        self.assertEqual(config.check(c), [])
        rows = self.rows(c)
        self.assertEqual((rows["spindle.0.index-enable"].status, rows["spindle.0.index-enable"].slave),
                         ("connected", "io.enc.index-enable"))
        p = integrate.plan(self.ini, c)
        self.assertEqual(p.errors, [])
        main = next(ch.new for ch in p.changes if ch.path.endswith(integrate.MAIN_FILE))
        self.assertIn("net spindle-index-enable io.enc.index-enable <=> spindle.0.index-enable\n", main)
        self.assertIn("net spindle-revs io.enc.pos => spindle.0.revs\n", main)
        # nothing of the simulated encoder is left on the signals
        self.assertNotIn("spindle_sim.index-enable", main)
        self.assertNotIn("spindle_sim.position-fb", main)
        self.assertTrue(any("signal spindle-index-enable: spindle_sim.index-enable no longer on it"
                            in n for n in p.notes), p.notes)
        sim = next(ch.new for ch in p.changes if ch.name == "spindle_sim.hal")
        self.assertIn(integrate.COMMENT + "net spindle-index-enable", sim)
        self.assertIn("net spindle-speed spindle.0.speed-out => spindle_sim.velocity-cmd\n", sim)


class TestSignalNames(TestEncoderChannelPins):
    """A slave's pin is connected by a signal's name: the signal's other pins,
    the simulation's writer giving way."""

    def graph(self, c):
        return wiring.graph(self.ini, c)

    def test_a_signal_the_simulation_writes(self):
        c = encoder_cfg()
        g = self.graph(c)
        self.assertIn("spindle-revs", wiring.signals(g))
        # the slave writes the position: the readers of the signal are connected, the
        # simulated writer gives way
        pins, sig, dropped, conflicts = wiring.expand_signals(g, "out", ["spindle-revs"])
        self.assertEqual((pins, sig, conflicts), (["spindle.0.revs"], "spindle-revs", []))
        self.assertEqual(len(dropped), 1)
        self.assertIn("spindle_sim", dropped[0])
        # a pin is a pin, not looked up
        pins, sig, _, _ = wiring.expand_signals(g, "out", ["spindle.0.revs"])
        self.assertEqual((pins, sig), (["spindle.0.revs"], None))
        # a word that is neither stays as typed (the check says what is wrong with it)
        self.assertEqual(wiring.expand_signals(g, "out", ["no-such"])[0], ["no-such"])

    def test_a_real_writer_is_reported_as_a_conflict(self):
        c = encoder_cfg()
        with open(os.path.join(self.dir.name, "extra.hal"), "w") as f:
            f.write("loadrt scale names=real_rev\nnet real-revs real_rev.out => spindle.0.revs\n")
        with open(self.ini) as f:
            ini = f.read()
        with open(self.ini, "w") as f:
            f.write(ini.replace("HALFILE = spindle_sim.hal", "HALFILE = spindle_sim.hal\nHALFILE = extra.hal"))
        g = self.graph(c)
        pins, sig, dropped, conflicts = wiring.expand_signals(g, "out", ["real-revs"])
        self.assertEqual((pins, conflicts, dropped), (["spindle.0.revs"], ["real_rev.out"], []))
        # a slave's pin that reads takes the signal's real writer
        pins, _, _, conflicts = wiring.expand_signals(g, "in", ["real-revs"])
        self.assertEqual((pins, conflicts), (["real_rev.out"], []))

    def test_the_connection_by_signal_writes_the_same_net(self):
        c = encoder_cfg()
        g = self.graph(c)
        pins, sig, _, _ = wiring.expand_signals(g, "out", ["spindle-revs"])
        integrate.set_net(c, "io.enc.pos", pins, sig)
        p = integrate.plan(self.ini, c)
        self.assertEqual(p.errors, [])
        main = next(ch.new for ch in p.changes if ch.path.endswith(integrate.MAIN_FILE))
        self.assertIn("net spindle-revs io.enc.pos => spindle.0.revs\n", main)
        self.assertNotIn("spindle_sim.position-fb", main)
