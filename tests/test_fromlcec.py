"""Tests of moving an lcec configuration to ecat (fromlcec.py, and how
integrate.plan and wiring use it), on a made-up configuration folder shaped
like a real one: lcec with generic slaves, the cia402 component between the
joints and the drives, and an I/O slave with bit pins.

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
if "ECATSETUP_TEST_STATE" not in os.environ:
    os.environ["ECATSETUP_TEST_STATE"] = os.environ["XDG_STATE_HOME"] = \
        tempfile.mkdtemp(prefix="ecatsetup-test-state-")
sys.path.insert(0, os.path.dirname(__file__))

from ecatsetup import config, fromlcec, halcheck, integrate, lcecxml, wiring  # noqa: E402

INI = """\
[EMCMOT]
SERVO_PERIOD = 1000000

[KINS]
JOINTS = 2
KINEMATICS = trivkins coordinates=XY

[HAL]
HALFILE = machine.hal
POSTGUI_HALFILE = postgui.hal

[JOINT_0]
TYPE = LINEAR
SCALE = 100

[JOINT_1]
TYPE = LINEAR
"""

MACHINE = """\
loadrt [KINS]KINEMATICS
loadrt [EMCMOT]EMCMOT servo_period_nsec=[EMCMOT]SERVO_PERIOD num_joints=[KINS]JOINTS
loadusr -W lcec_conf ethercat-conf.xml
loadrt lcec
loadrt cia402 names=cia402.X
addf lcec.read-all servo-thread
addf cia402.X.read-all servo-thread
addf motion-command-handler servo-thread

# the I/O slave
net home-x <= lcec.0.IO.in-1
net home-x => joint.0.home-sw-in
net estop-ext lcec.0.IO.in-2 => iocontrol.0.emc-enable-in
setp lcec.0.IO.level 2.5
net io-ok lcec.0.all-op
net io-online lcec.0.IO.slave-online
net raw lcec.0.IO.raw => sampler.0.pin.0   # gone in ecat

# X through the cia402 component
net x-pos-cmd <= joint.0.motor-pos-cmd
net x-pos-fb  <= joint.0.motor-pos-fb
net x-enable  <= joint.0.amp-enable-out
net x-pos-cmd => cia402.X.pos-cmd
net x-pos-fb  => cia402.X.pos-fb
net x-enable  => cia402.X.enable
setp cia402.X.csp-mode 1
setp cia402.X.pos-scale [JOINT_0]SCALE
net x-statusword lcec.0.X.cia-statusword => cia402.X.statusword
net x-drv-act-pos lcec.0.X.actual-position => cia402.X.drv-actual-position \\
    ddt.0.in
net x-controlword cia402.X.controlword => lcec.0.X.cia-controlword
net x-drv-target-pos cia402.X.drv-target-position => lcec.0.X.target-position
net x-fault cia402.X.drv-fault => joint.0.amp-fault-in

addf cia402.X.write-all servo-thread
addf lcec.write-all servo-thread
"""

POSTGUI = "net x-enable => pyvcp.x-led\nnet io-ok => pyvcp.bus-led\n"

XML = """\
<masters>
  <master idx="0" appTimePeriod="1000000" refClockSyncCycles="-1">
    <slave idx="0" name="X" type="generic" vid="00000a88" pid="0a880002" configPdos="true">
      <sdoConfig idx="2000" subIdx="0"><sdoDataRaw data="a0 0f"/></sdoConfig>
      <sdoConfig idx="2003" subIdx="0"><sdoDataRaw data="64 00"/></sdoConfig>
      <syncManager idx="2" dir="out">
        <pdo idx="1600">
          <pdoEntry idx="6040" subIdx="00" bitLen="16" halPin="cia-controlword" halType="u32"/>
          <pdoEntry idx="607A" subIdx="00" bitLen="32" halPin="target-position" halType="s32"/>
        </pdo>
      </syncManager>
      <syncManager idx="3" dir="in">
        <pdo idx="1a00">
          <pdoEntry idx="6041" subIdx="00" bitLen="16" halPin="cia-statusword" halType="u32"/>
          <pdoEntry idx="6064" subIdx="00" bitLen="32" halPin="actual-position" halType="s32"/>
        </pdo>
      </syncManager>
    </slave>
    <slave idx="1" name="IO" type="generic" vid="00000002" pid="03f03052" configPdos="true">
      <syncManager idx="2" dir="out">
        <pdo idx="1600">
          <pdoEntry idx="7010" subIdx="01" bitLen="16" halPin="level" halType="float"/>
        </pdo>
      </syncManager>
      <syncManager idx="3" dir="in">
        <pdo idx="1a00">
          <pdoEntry idx="6000" subIdx="00" bitLen="8" halType="complex">
            <complexEntry bitLen="1" halPin="in-1" halType="bit"/>
            <complexEntry bitLen="1" halPin="in-2" halType="bit"/>
            <complexEntry bitLen="6"/>
          </pdoEntry>
          <pdoEntry idx="6001" subIdx="01" bitLen="16" halPin="raw" halType="s32"/>
        </pdo>
      </syncManager>
    </slave>
  </master>
</masters>
"""


def cfg():
    """The configuration as proposed from the bus: other names than lcec's."""
    drive = {"position": 0, "name": "drive0", "device": "ECT60",
             "identity": {"vendor": "0x00000A88", "product": "0x0A880002"},
             "profile": "cia402", "cia402": {"mode": "csp"}, "configure-pdos": True,
             "process-data": {
                 "outputs": [{"pdo": "0x1600", "sm": 2, "entries": [
                     {"object": "0x6040:00", "bits": 16, "type": "UINT", "use": "profile"},
                     {"object": "0x607A:00", "bits": 32, "type": "DINT", "use": "profile"}]}],
                 "inputs": [{"pdo": "0x1A00", "sm": 3, "entries": [
                     {"object": "0x6041:00", "bits": 16, "type": "UINT", "use": "profile"},
                     {"object": "0x6064:00", "bits": 32, "type": "DINT", "use": "profile"}]}]},
             "settings": [{"object": "0x2003:00", "type": "UINT", "value": 100}]}
    io = {"position": 1, "name": "io1", "device": "IO",
          "identity": {"vendor": "0x00000002", "product": "0x03F03052"},
          "profile": "generic", "configure-pdos": True,
          "process-data": {
              "outputs": [{"pdo": "0x1600", "sm": 2, "entries": [
                  {"object": "0x7010:01", "bits": 16, "type": "INT", "pin": "out-level",
                   "hal-type": "real"}]}],
              "inputs": [{"pdo": "0x1A00", "sm": 3, "entries": [
                  {"object": "0x6000:00", "bits": 8, "type": "USINT", "hal-type": "bits",
                   "split": ["din-0", "din-1"]},
                  {"object": "0x6001:01", "bits": 16, "type": "INT", "pin": "value",
                   "hal-type": "sint"}]}]}}
    return {"ecat": 1, "master": {"interface": "enp1s0", "transport": "raw"},
            "slaves": [drive, io]}


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
        self.write("ethercat-conf.xml", XML)
        self.ini = os.path.join(self.dir.name, "machine.ini")

    def write(self, name, text):
        with open(os.path.join(self.dir.name, name), "w") as f:
            f.write(text)

    def read(self, name):
        with open(os.path.join(self.dir.name, name)) as f:
            return f.read()

    def ready(self):
        """The configuration after the user's steps in the window: lcec's names,
        the suggestions, the joints."""
        c = cfg()
        f = fromlcec.find(self.ini)
        fromlcec.take_names(f, c)
        for r in fromlcec.rows(f, c):
            if not r.decided and r.suggestion:
                fromlcec.set_choice(c, r.pin, r.suggestion)
        fromlcec.set_choice(c, "lcec.0.IO.raw", None)
        for j, (name, scale) in integrate.propose_joints(self.ini, c).items():
            integrate.set_joint(c, j, name, scale)
        return c


class TestFind(Folder):
    def test_not_lcec(self):
        self.write("machine.hal", "net a joint.0.motor-pos-cmd => x.pos-cmd\n")
        self.assertIsNone(fromlcec.find(self.ini))

    def test_found(self):
        f = fromlcec.find(self.ini)
        self.assertEqual(os.path.basename(f.xml_path), "ethercat-conf.xml")
        self.assertEqual([x.name for x in f.xml.slaves], ["X", "IO"])
        self.assertEqual(f.glue, [fromlcec.Glue("cia402.X", "lcec.0.X", 0, "100")])
        self.assertEqual(f.problems, [])
        # the lines of lcec and the component themselves are not uses
        self.assertNotIn("lcec.read-all", f.uses)
        self.assertNotIn("cia402.X.read-all", f.uses)
        self.assertEqual(f.uses["lcec.0.IO.in-1"], [("machine.hal", 11)])
        self.assertEqual(f.uses["lcec.0.all-op"], [("machine.hal", 15)])

    def test_without_the_xml_file(self):
        os.remove(os.path.join(self.dir.name, "ethercat-conf.xml"))
        f = fromlcec.find(self.ini)
        self.assertIsNone(f.xml)
        self.assertTrue(any("does not exist" in n for n in f.notes))
        # the slaves are found by name then; drive0 is not X: the glue stays
        c = cfg()
        self.assertEqual(fromlcec.replaced(f, c), {})
        c["slaves"][0]["name"] = "X"
        self.assertEqual(fromlcec.replaced(f, c), {"cia402.X": "X"})

    def test_two_masters_are_refused(self):
        self.write("ethercat-conf.xml", XML.replace("</masters>", '<master idx="1"/></masters>'))
        with self.assertRaises(ValueError):
            lcecxml.load(os.path.join(self.dir.name, "ethercat-conf.xml"))
        f = fromlcec.find(self.ini)
        self.assertTrue(any("2 masters" in x for x in f.problems))
        p = integrate.plan(self.ini, self.ready())
        self.assertTrue(any("2 masters" in e for e in p.errors))


class TestRows(Folder):
    def test_suggestions(self):
        c = cfg()
        f = fromlcec.find(self.ini)
        got = {r.pin: r for r in fromlcec.rows(f, c)}
        # the same object (a bit of it) of the slave at the same position
        self.assertEqual(got["lcec.0.IO.in-1"].suggestion, "io1.din-0")
        self.assertEqual(got["lcec.0.IO.in-2"].suggestion, "io1.din-1")
        self.assertIn("0x6000:00 bit 1", got["lcec.0.IO.in-2"].why)
        self.assertEqual(got["lcec.0.IO.level"].suggestion, "io1.out-level")
        self.assertEqual(got["lcec.0.IO.raw"].suggestion, "io1.value")
        # the master's and the slave's state pins
        self.assertEqual(got["lcec.0.all-op"].suggestion, "ecat.0.all-op")
        self.assertEqual(got["lcec.0.IO.slave-online"].suggestion, "io1.slave-online")
        self.assertFalse(any(r.decided for p, r in got.items() if p.startswith("lcec.0.IO")))

    def test_the_glue_is_decided_by_the_profile(self):
        c = cfg()
        got = {r.pin: r for r in fromlcec.rows(fromlcec.find(self.ini), c)}
        for pin, new in (("cia402.X.pos-cmd", "drive0.pos-cmd"), ("cia402.X.enable", "drive0.enable"),
                         ("cia402.X.pos-scale", "drive0.pos-scale"),
                         ("cia402.X.drv-fault", "drive0.drv-fault"),
                         ("cia402.X.csp-mode", None), ("cia402.X.statusword", None),
                         ("lcec.0.X.cia-statusword", None), ("lcec.0.X.cia-controlword", None)):
            self.assertTrue(got[pin].auto and got[pin].decided, pin)
            self.assertEqual(got[pin].new, new, pin)
        # the actual position goes to ddt too: the profile's pin of that object
        # writes the signal now, the component's pin goes
        r = got["lcec.0.X.actual-position"]
        self.assertTrue(r.auto)
        self.assertEqual(r.new, "drive0.drv-actual-position")
        self.assertTrue(got["cia402.X.drv-actual-position"].auto)
        self.assertIsNone(got["cia402.X.drv-actual-position"].new)

    def test_a_drive_without_the_profile_keeps_the_component(self):
        c = cfg()
        c["slaves"][0]["profile"] = "generic"
        del c["slaves"][0]["cia402"]
        f = fromlcec.find(self.ini)
        self.assertEqual(fromlcec.replaced(f, c), {})
        self.assertFalse(any(r.auto for r in fromlcec.rows(f, c)))
        self.assertIn("has no cia402 profile", fromlcec.glue_notes(f, c)[0])

    def test_a_slave_named_as_lcec_needs_no_row(self):
        c = cfg()
        c["slaves"][1]["name"] = "lcec.0.IO"
        c["slaves"][1]["process-data"]["inputs"][0]["entries"][0]["split"] = ["in-1", "in-2"]
        pins = [r.pin for r in fromlcec.rows(fromlcec.find(self.ini), c)]
        self.assertNotIn("lcec.0.IO.in-1", pins)
        self.assertIn("lcec.0.IO.raw", pins)

    def test_names_and_rename(self):
        c = cfg()
        f = fromlcec.find(self.ini)
        fromlcec.set_choice(c, "lcec.0.IO.in-1", "io1.din-0")
        self.assertEqual(fromlcec.names(f, c), {"drive0": "X", "io1": "IO"})
        self.assertEqual(fromlcec.take_names(f, c), {"drive0": "X", "io1": "IO"})
        self.assertEqual([s["name"] for s in c["slaves"]], ["X", "IO"])
        # the choices follow the new names
        self.assertEqual(fromlcec.choices(c), {"lcec.0.IO.in-1": "IO.din-0"})
        self.assertEqual(fromlcec.names(f, c), {})

    def test_choices_and_check(self):
        c = cfg()
        fromlcec.set_choice(c, "lcec.0.IO.in-1", "io1.din-0")
        fromlcec.set_choice(c, "lcec.0.IO.raw", None)
        self.assertEqual(c["linuxcnc"]["old-pins"], [{"old": "lcec.0.IO.in-1", "new": "io1.din-0"},
                                                     {"old": "lcec.0.IO.raw"}])
        self.assertEqual(config.check(c), [])
        fromlcec.set_choice(c, "lcec.0.IO.in-2", "io1.no-such-pin")
        self.assertTrue(any("not a pin or parameter" in e for e in config.check(c)))
        fromlcec.set_choice(c, "lcec.0.IO.in-2", None, decided=False)
        fromlcec.set_choice(c, "lcec.0.IO.in-1", None, decided=False)
        fromlcec.set_choice(c, "lcec.0.IO.raw", None, decided=False)
        self.assertNotIn("linuxcnc", c)


class TestSubstitute(unittest.TestCase):
    def sub(self, line, m, gone=()):
        t = fromlcec.substitute(line.split(), m, set(gone))
        return None if t is None else " ".join(t)

    def test_net(self):
        m = {"lcec.0.a": "io.a", "lcec.0.b": None}
        self.assertEqual(self.sub("net s lcec.0.a => x.in", m), "net s io.a => x.in")
        # a pin taken out, and an arrow with nothing after it
        self.assertEqual(self.sub("net s x.out => lcec.0.b", m), "net s x.out")
        self.assertEqual(self.sub("net s => lcec.0.b y.in", m), "net s => y.in")
        self.assertEqual(self.sub("net s <= lcec.0.b", m), None)
        self.assertEqual(self.sub("net s lcec.0.b => x.in <= lcec.0.a", m), "net s => x.in <= io.a")

    def test_unchanged_is_the_same_list(self):
        t = "net s x.out => y.in".split()
        self.assertIs(fromlcec.substitute(t, {"lcec.0.a": "io.a"}, set()), t)

    def test_setp_and_others(self):
        m = {"lcec.0.a": "io.a", "lcec.0.b": None}
        self.assertEqual(self.sub("setp lcec.0.a 2.5", m), "setp io.a 2.5")
        self.assertIsNone(self.sub("setp lcec.0.b 1", m))
        self.assertEqual(self.sub("lcec.0.a = 3", m), "io.a = 3")
        self.assertEqual(self.sub("linkps lcec.0.a sig", m), "linkps io.a sig")
        self.assertIsNone(self.sub("linkps lcec.0.b sig", m))

    def test_the_component_lines(self):
        gone = {"cia402.X", "cia402.Y"}
        self.assertIsNone(self.sub("loadrt cia402 names=cia402.X,cia402.Y", {}, gone))
        self.assertEqual(self.sub("loadrt cia402 names=cia402.X,cia402.Z", {}, gone),
                         "loadrt cia402 names=cia402.Z")
        self.assertIsNone(self.sub("addf cia402.X.read-all servo-thread", {}, gone))
        self.assertEqual(self.sub("addf cia402.Z.read-all servo-thread", {}, gone),
                         "addf cia402.Z.read-all servo-thread")
        # count=: the instances cannot be taken out one by one
        self.assertIsNone(self.sub("loadrt cia402 count=2", {}, {"cia402.0", "cia402.1"}))
        self.assertEqual(self.sub("loadrt cia402 count=2", {}, {"cia402.0"}),
                         "loadrt cia402 count=2")


class TestPlan(Folder):
    def test_undecided_pins_are_an_error(self):
        c = cfg()
        p = integrate.plan(self.ini, c)
        e = next(e for e in p.errors if "no new pin chosen" in e)
        self.assertIn("lcec.0.IO.in-1 (machine.hal:11)", e)
        with self.assertRaises(ValueError):
            integrate.apply(p)

    def test_plan_and_apply(self):
        c = self.ready()
        self.assertEqual(integrate.joints(c), [{"joint": 0, "slave": "X", "scale": 100.0}])
        self.assertEqual(config.check(c), [])
        p = integrate.plan(self.ini, c)
        self.assertEqual(p.errors, [])
        self.assertTrue(any("cia402.X is replaced by the cia402 profile of X (joint 0)" in n
                            for n in p.notes))
        integrate.apply(p)
        hal = self.read("machine.hal")
        for line in ("loadusr -W lcec_conf ethercat-conf.xml", "loadrt lcec",
                     "loadrt cia402 names=cia402.X", "addf cia402.X.read-all servo-thread",
                     "net x-pos-cmd => cia402.X.pos-cmd", "setp cia402.X.csp-mode 1",
                     "setp cia402.X.pos-scale [JOINT_0]SCALE",
                     "net x-statusword lcec.0.X.cia-statusword => cia402.X.statusword",
                     "net raw lcec.0.IO.raw => sampler.0.pin.0   # gone in ecat",
                     "addf lcec.write-all servo-thread"):
            self.assertIn(integrate.COMMENT + line + "\n", hal)
        # put back with the new pins, after the line commented out
        self.assertIn(integrate.COMMENT + "net home-x <= lcec.0.IO.in-1\n"
                      "net home-x <= IO.din-0" + integrate.ADDED + "\n", hal)
        for line in ("net estop-ext IO.din-1 => iocontrol.0.emc-enable-in", "setp IO.out-level 2.5",
                     "net io-ok ecat.0.all-op", "net io-online IO.slave-online",
                     "net x-drv-act-pos X.drv-actual-position => ddt.0.in",
                     # a pin taken out: the others stay on the signal
                     "net raw => sampler.0.pin.0"):
            self.assertIn(line + integrate.ADDED + "\n", hal)
        # the joint's lines are in ethercat.hal, with the user's signal names
        main = self.read("ethercat.hal")
        for line in ("setp X.pos-scale 100.0", "net x-pos-cmd joint.0.motor-pos-cmd => X.pos-cmd",
                     "net x-pos-fb X.pos-fb => joint.0.motor-pos-fb",
                     "net x-enable joint.0.amp-enable-out => X.enable",
                     "net x-fault X.drv-fault => joint.0.amp-fault-in"):
            self.assertIn(line + "\n", main)
        # the POSTGUI file is left as it is: its signals still exist
        self.assertEqual(self.read("postgui.hal"), POSTGUI)
        # written: nothing more to change
        self.assertEqual(integrate.plan(self.ini, c).changes, [])
        # the HAL check finds nothing to complain about in what was written
        found = [x for x in halcheck.check(self.ini, c) if x.severity == "error"]
        self.assertEqual(found, [], [x.text for x in found])

    def test_still_to_connect_sees_the_new_pins(self):
        c = self.ready()
        rows = {r.pin: r for r in wiring.rows(self.ini, c)}
        self.assertEqual(rows["joint.0.home-sw-in"].status, "connected")
        self.assertEqual(rows["joint.0.home-sw-in"].slave, "IO.din-0")
        self.assertEqual(rows["iocontrol.0.emc-enable-in"].slave, "IO.din-1")

    def test_a_tcl_file_with_old_pins_is_turned_into_hal(self):
        self.write("io.tcl", "net home-y <= lcec.0.IO.in-2\n")
        self.write("machine.ini", INI.replace("POSTGUI_HALFILE", "HALFILE = io.tcl\n"
                                                                 "POSTGUI_HALFILE"))
        p = integrate.plan(self.ini, self.ready())
        self.assertEqual(p.errors, [])
        new = next(x for x in p.changes if x.name == "io.hal")
        self.assertIn("net home-y <= IO.din-1" + integrate.ADDED, new.new)
        self.assertTrue(any("io.tcl is turned into io.hal" in n for n in p.notes))


class TestSettings(Folder):
    def test_hints(self):
        c = cfg()
        f = fromlcec.find(self.ini)
        hints = fromlcec.setting_hints(f, c)
        # 0x2003 has the same value (the bytes read as the configuration's type)
        self.assertEqual([(h.slave, h.setting["object"], h.now) for h in hints],
                         [("drive0", "0x2000:00", None)])
        self.assertEqual(hints[0].setting, {"object": "0x2000:00", "data": "a0 0f"})
        fromlcec.add_setting(c, hints[0])
        self.assertIn({"object": "0x2000:00", "data": "a0 0f"}, c["slaves"][0]["settings"])
        self.assertEqual(fromlcec.setting_hints(f, c), [])
        self.assertEqual(config.check(c), [])

    def test_another_value_is_offered(self):
        c = cfg()
        c["slaves"][0]["settings"] = [{"object": "0x2003:00", "type": "UINT", "value": 50},
                                      {"object": "0x2000:00", "type": "UINT", "value": 4000}]
        hints = fromlcec.setting_hints(fromlcec.find(self.ini), c)
        self.assertEqual([(h.setting, h.now) for h in hints],
                         [({"object": "0x2003:00", "type": "UINT", "value": 100}, 50)])
        fromlcec.add_setting(c, hints[0])
        self.assertEqual(c["slaves"][0]["settings"],
                         [{"object": "0x2000:00", "type": "UINT", "value": 4000},
                          {"object": "0x2003:00", "type": "UINT", "value": 100}])

    def test_zero_position_becomes_zero_at_start(self):
        # the ECT60's quirk file: lcec's 0x2010 is a command, ecat's zero-at-start
        self.write("ethercat-conf.xml", XML.replace(
            '<sdoConfig idx="2000"', '<sdoConfig idx="2010" subIdx="0"><sdoDataRaw data="01 00"/>'
            '</sdoConfig><sdoConfig idx="2000"'))
        c = cfg()
        hints = fromlcec.setting_hints(fromlcec.find(self.ini), c)
        z = [h for h in hints if h.zero_at_start]
        self.assertEqual(len(z), 1)
        self.assertNotIn("0x2010:00", [h.setting["object"] for h in hints if h.setting])
        fromlcec.add_setting(c, z[0])
        self.assertTrue(c["slaves"][0]["cia402"]["zero-at-start"])


try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5.QtWidgets import QApplication
    from ecatsetup import gui, session
except ImportError:
    gui = None


@unittest.skipIf(gui is None, "no PyQt5")
class TestCard(Folder):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_the_card(self):
        s = session.Session(ini_path=self.ini)
        s.cfg, s.path = cfg(), os.path.join(self.dir.name, "ethercat.yaml")
        w = gui.MainWindow(s)
        self.addCleanup(lambda: (s.__setattr__("saved", s.cfg), w.close()))
        w.tabs.setCurrentWidget(w.hal_page)
        page = w.hal_page
        page.refresh()
        self.assertFalse(page.lcec_card.isHidden())
        self.assertIn("still need a choice", page.lcec_text.text())
        self.assertIn("no new pin chosen", page.problems.text())
        # the names, then the suggestions
        self.assertFalse(page.lcec_names.isHidden())
        page.do_lcec_names()
        self.assertEqual([x["name"] for x in s.cfg["slaves"]], ["X", "IO"])
        self.assertTrue(page.lcec_names.isHidden())
        # each suggestion is taken on its own, with the row's Use button
        r = next(i for i, x in enumerate(page.lcec_rows) if x.pin == "lcec.0.IO.in-1")
        page.lcec_table.cellWidget(r, 3).click()
        self.app.processEvents()
        self.assertEqual(fromlcec.choices(s.cfg), {"lcec.0.IO.in-1": "IO.din-0"})
        r = next(i for i, x in enumerate(page.lcec_rows) if x.pin == "lcec.0.IO.in-1")
        self.assertIsNone(page.lcec_table.cellWidget(r, 3))      # decided: no button
        # the rest at once
        self.assertFalse(page.lcec_suggest.isHidden())
        page.do_lcec_suggestions()
        self.assertTrue(page.lcec_suggest.isHidden())
        # a choice in the table: taken out
        r = next(i for i, x in enumerate(page.lcec_rows) if x.pin == "lcec.0.IO.raw")
        box = page.lcec_table.cellWidget(r, 2)
        self.assertEqual(box.currentText(), "IO.value")
        box.setEditText(page.TAKE_OUT)
        page.lcec_edited(page.lcec_rows[r], box)
        self.app.processEvents()
        self.assertIsNone(fromlcec.choices(s.cfg)["lcec.0.IO.raw"])
        # a pin that does not exist is not taken
        r = next(i for i, x in enumerate(page.lcec_rows) if x.pin == "lcec.0.IO.in-1")
        box = page.lcec_table.cellWidget(r, 2)
        box.setEditText("IO.nothing")
        page.lcec_edited(page.lcec_rows[r], box)
        self.app.processEvents()
        self.assertEqual(fromlcec.choices(s.cfg)["lcec.0.IO.in-1"], "IO.din-0")
        # the joint, from the component
        self.assertIn("joint.0 to X", page.take_over_text.text())
        page.do_take_over()
        self.assertEqual(integrate.joints(s.cfg), [{"joint": 0, "slave": "X", "scale": 100.0}])
        # lcec's setting, added
        self.assertEqual([h.setting["object"] for h in page.lcec_hints], ["0x2000:00"])
        page.lcec_settings.cellWidget(0, 5).click()
        self.assertEqual(page.lcec_hints, [])
        self.assertEqual(page.problems.text(), "")
        self.assertTrue(page.write_button.isEnabled())
        self.assertIn("net x-pos-cmd joint.0.motor-pos-cmd => X.pos-cmd", page.code.toPlainText())


if __name__ == "__main__":
    unittest.main()
