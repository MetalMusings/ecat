"""Tests of quirk files (quirks.py, ecatsetup/devices/*.yaml) and the HAL
lines they give (integrate.quirk_hal).

Run with: python3 -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# the tests are written for 2.10 unless they say otherwise (lcnc_env.series)
os.environ.setdefault("ECATSETUP_LINUXCNC_SERIES", "2.10")
sys.path.insert(0, os.path.dirname(__file__))
# quirk files: only those that come with the program, unless a test says so
os.environ["ECATSETUP_DEVICES"] = os.path.join(os.path.dirname(__file__), "no-such-dir")
os.environ["ECATSETUP_SETTINGS"] = os.path.join(os.path.dirname(__file__), "no-such-dir", "settings.yaml")
# state (test lock, drive limits): never the user's own
if "ECATSETUP_TEST_STATE" not in os.environ:
    # one folder for the whole run: the state, and every temporary file of the
    # tests and of what they start (TMPDIR); removed when the run ends
    os.environ["ECATSETUP_TEST_STATE"] = os.environ["XDG_STATE_HOME"] = os.environ["TMPDIR"] = \
        __import__("tempfile").tempdir = __import__("tempfile").mkdtemp(prefix="ecatsetup-test-state-")
    __import__("atexit").register(__import__("shutil").rmtree, os.environ["ECATSETUP_TEST_STATE"], True)

from ecatsetup import config, integrate, lcecmap, quirks  # noqa: E402
from ecatsetup import propose  # noqa: E402
from test_integrate import Folder  # noqa: E402
from test_prototype import easercat  # noqa: E402

EL7041 = {"vendor": "0x00000002", "product": "0x1B813052"}


def stepper(name="x-stm"):
    def e(obj, pin, bits=1, t="BOOL", **kw):
        return dict({"object": obj, "bits": bits, "type": t, "pin": pin,
                     "hal-type": "bool"}, **kw)
    return {"position": 4, "name": name, "device": "EL7041", "identity": dict(EL7041),
            "profile": "generic", "configure-pdos": True, "process-data": {
                "outputs": [{"pdo": "0x1602", "sm": 2, "entries": [
                    e("0x7010:01", "enable"), e("0x7010:02", "reset")]},
                    {"pdo": "0x1604", "sm": 2, "entries": [
                        e("0x7010:21", "velocity", 16, "INT", scale=32767.0,
                          **{"hal-type": "real"})]}],
                "inputs": [{"pdo": "0x1A03", "sm": 3, "entries": [e("0x6010:04", "error")]}]}}


class TestFiles(unittest.TestCase):
    def tearDown(self):
        quirks.load(reload=True)

    def test_shipped_files_read(self):
        qs = quirks.load(reload=True)
        self.assertEqual(quirks.problems, [])
        self.assertEqual(len(qs), 4)
        # a family's file matches each of its products
        self.assertEqual(quirks.of((0xAB5, 0x980))["name"], "GeRui EC1 series")
        self.assertEqual(quirks.of((0xAB5, 0x1030))["name"], "GeRui EC1 series")
        self.assertEqual(quirks.of((0xAB5, 0x69673537)), {})
        # the shipped files hold notes, pins and HAL lines only (2026-11-28)
        for q in qs:
            self.assertFalse(set(q["raw"]) & quirks.RETIRED, q["path"])
        self.assertEqual(quirks.pins((2, 0x1B813052))[(0x7010, 0x21)]["scale"], 32767)
        self.assertEqual(quirks.of((1, 2)), {})

    def test_user_file_wins_and_bad_files_are_reported(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "mine.yaml"), "w") as f:
                f.write("name: my ECT60\nmatch: {vendor: '0xA88', product: '0x0A880002'}\n"
                        "notes: Mine.\ncommands: {'0x2011': mine}\n")
            with open(os.path.join(d, "bad.yaml"), "w") as f:
                f.write("name: x\nmatsh: {}\n")
            os.environ["ECATSETUP_DEVICES"] = d
            try:
                quirks.load(reload=True)
                self.assertEqual(quirks.of((0xA88, 0xA880002))["name"], "my ECT60")
                # laid over the shipped file: its notes stay, a retired key is read without
                ect = quirks.of((0xA88, 0xA880002))
                self.assertEqual([t for _, t in ect["notes"]][1:], ["Mine."])
                self.assertNotIn("commands", ect)
                self.assertTrue(ect["layers"][0].endswith("rtelligent-ect60.yaml"))
                self.assertEqual(len(quirks.problems), 1)
                self.assertIn("bad.yaml: unknown keys matsh", quirks.problems[0])
            finally:
                os.environ["ECATSETUP_DEVICES"] = os.path.join(os.path.dirname(__file__),
                                                               "no-such-dir")

    def test_a_users_file_gives_no_hal_lines(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "el7041.yaml"), "w") as f:
                f.write("name: mine\nmatch: {vendor: '0x2', product: '0x1B813052'}\n"
                        "pins: {'0x7010:01': {pin: power}}\nhal: {lines: ['net a b']}\n")
            with open(os.path.join(d, "other.yaml"), "w") as f:
                f.write("name: other\nmatch: {vendor: '0x2', product: '0x1'}\n"
                        "hal: {lines: ['net a b']}\n")
            os.environ["ECATSETUP_DEVICES"] = d
            try:
                quirks.load(reload=True)
                q = quirks.of((2, 0x1B813052))
                self.assertEqual(q["pins"][(0x7010, 1)]["pin"], "power")        # pins: yes
                self.assertNotIn("net a b", q["hal"]["lines"])               # the shipped lines stay
                self.assertIn("net {signal:0x6010:04} => {name}-auto-reset.in1", q["hal"]["lines"])
                self.assertNotIn("hal", quirks.of((2, 1)))
                self.assertEqual(len(quirks.problems), 2)
                self.assertIn("hal left out: only the program's files give it", quirks.problems[0])
            finally:
                os.environ["ECATSETUP_DEVICES"] = os.path.join(os.path.dirname(__file__),
                                                               "no-such-dir")
                quirks.load(reload=True)

    def test_parse_errors(self):
        for data, msg in (({"match": {"vendor": "0x2"}}, "match needs vendor and product"),
                          ({"pins": {"0x7010:01": {}}}, "pins.0x7010:01 needs a pin"),
                          ({"pins": {"x": {"pin": "a"}}}, "object 'x' is not a hex number"),
                          ({"settings": [{"object": "0x2000:01"}]},
                           "settings need object, type and value"),
                          ({"colour": "red"}, "unknown keys colour")):
            with self.assertRaisesRegex(quirks.QuirkError, msg):
                quirks.parse(data)


class TestNotes(unittest.TestCase):
    """The notes on a device (a text) and the sentences that name an object."""

    def test_the_shipped_drives_have_notes(self):
        quirks.load(reload=True)
        ect = (0xA88, 0xA880002)
        self.assertEqual(len(quirks.notes(ect)), 1)
        tip = quirks.object_tip(ect, 0x2010, 0)
        self.assertIn("Never write 0x2010", tip)
        for key in ((0x400000, 0x715), (0xAB5, 0x980), (2, 0x1B813052)):
            self.assertTrue(quirks.notes(key), key)
        self.assertEqual(quirks.notes((1, 2)), [])

    def test_sentences_and_objects(self):
        q = quirks.parse({"name": "t", "match": {"vendor": "0x2", "product": "0x1"}, "notes":
                          "Inputs: 0x2007:03-06 set the function. Polarity\nis 0x2008, bits 2-5.\n\n"
                          "Error 0x8611 (0x603F) is a following error. 0x2023:01 is Kp."})
        self.assertEqual(quirks.sentences(q["notes"][0][1]),
                         ["Inputs: 0x2007:03-06 set the function.", "Polarity is 0x2008, bits 2-5.",
                          "Error 0x8611 (0x603F) is a following error.", "0x2023:01 is Kp."])
        orig = quirks._cache
        quirks._cache = [q]
        try:
            n = quirks.object_notes
            self.assertEqual(n((2, 1), 0x2007, 5), ["Inputs: 0x2007:03-06 set the function."])
            self.assertEqual(n((2, 1), 0x2007, 7), [])
            self.assertEqual(n((2, 1), 0x2007), ["Inputs: 0x2007:03-06 set the function."])
            self.assertEqual(n((2, 1), 0x2008, 0), ["Polarity is 0x2008, bits 2-5."])   # any subindex
            self.assertEqual(n((2, 1), 0x2023, 2), [])
            self.assertEqual(n((2, 1), 0x603F, 0), ["Error 0x8611 (0x603F) is a following error."])
            self.assertEqual(quirks.object_tip((2, 1), 0x9999, 0), "")
        finally:
            quirks._cache = orig

    def test_yours_comes_after_the_programs(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "mine.yaml"), "w") as f:
                f.write("name: mine\nmatch: {vendor: '0xA88', product: '0x0A880002'}\n"
                        "notes: My X uses 0x2010 never.\n")
            os.environ["ECATSETUP_DEVICES"] = d
            try:
                quirks.load(reload=True)
                got = quirks.notes((0xA88, 0xA880002))
                self.assertEqual([os.path.basename(p) for p, _ in got], ["rtelligent-ect60.yaml", "mine.yaml"])
                self.assertEqual(len(quirks.object_notes((0xA88, 0xA880002), 0x2010, 0)), 2)
            finally:
                os.environ["ECATSETUP_DEVICES"] = os.path.join(os.path.dirname(__file__), "no-such-dir")
                quirks.load(reload=True)

    def test_notes_must_be_text(self):
        with self.assertRaises(quirks.QuirkError):
            quirks.parse({"notes": ["a"]})


class TestLcecMap(unittest.TestCase):
    """lcec's drivers' translations (lcecmap.py, ecatsetup/lcec), apart from the devices."""

    def test_the_shipped_files(self):
        ms = lcecmap.load(reload=True)
        self.assertEqual(lcecmap.problems, [])
        self.assertEqual(len(ms), 2)
        types = [t for m in ms for t in m["types"]]
        self.assertEqual(len(types), len(set(types)))
        self.assertIn("el70x1", lcecmap.for_type("EL7031")["name"])
        self.assertEqual(lcecmap.for_type("EL7041")["modparams"]["maxCurrent"][:2], (0x8010, 1))
        self.assertEqual(lcecmap.for_type("generic"), {})
        # no device file holds lcec's translations any more
        self.assertFalse(any(q["raw"].get("lcec") for q in quirks.load(reload=True)))

    def test_an_old_device_file_with_lcec_is_read_without_it(self):
        q = quirks.parse({"name": "old", "match": {"vendor": "0x2", "product": "0x1"},
                          "lcec": {"types": ["X"], "modparams": {"a": {"object": "0x8010:01"}}}})
        self.assertNotIn("lcec", q)

    def test_an_sdo_row_of_the_lcec_type_comes_first(self):
        orig = lcecmap._cache
        lcecmap._cache = [lcecmap.parse({"types": ["T"], "sdo": {
            "0x2010:00": {"replace": "cia402.zero-at-start", "note": "why"}}})]
        try:
            self.assertEqual(quirks.lcec_replacement("T", 0x2010, 0), ("cia402.zero-at-start", "why"))
            self.assertIsNone(quirks.lcec_replacement("U", 0x2010, 0))
            self.assertIsNone(quirks.lcec_replacement(None, 0x2010, 0))
        finally:
            lcecmap._cache = orig

    def test_parse_errors(self):
        for data, msg in (({"types": ["T"], "match": {}}, "unknown keys match"),
                          ({"name": "x"}, "types")):
            with self.assertRaises(lcecmap.LcecMapError) as e:
                lcecmap.parse(data)
            self.assertIn(msg, str(e.exception))


class TestSplitPins(unittest.TestCase):
    def test_a_bit_field_is_read_and_checked(self):
        q = quirks.parse({"name": "t", "pins": {"0x6000:00": {"split": ["a", None, "c"]}}})
        self.assertEqual(q["pins"][(0x6000, 0)]["split"], ["a", None, "c"])
        for bad in ({"split": "abc"}, {"split": [1]}, {}):
            with self.assertRaises(quirks.QuirkError):
                quirks.parse({"pins": {"0x6000:00": bad}})

    def test_saving_a_slave_keeps_its_bit_pins(self):
        slave = {"identity": dict(EL7041), "name": "n", "process-data": {"inputs": [
            {"entries": [{"object": "0x6000:00", "bits": 32, "split": ["x", None, "z"]}]}]}}
        self.assertEqual(quirks.from_slave(slave)["pins"]["0x6000:00"], {"split": ["x", None, "z"]})


class TestSaved(unittest.TestCase):
    """Save as quirk file: a configured slave's pins and settings become
    the next proposal's."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ["ECATSETUP_DEVICES"] = self.dir.name
        quirks.load(reload=True)

    def tearDown(self):
        os.environ["ECATSETUP_DEVICES"] = os.path.join(os.path.dirname(__file__), "no-such-dir")
        quirks.load(reload=True)
        self.dir.cleanup()

    def proposal(self):
        s, d = easercat()
        return propose.propose({"position": 3, "dc": False}, s, d, device_type=5001)

    def test_round_trip(self):
        s = config.slave_from_proposal(self.proposal(), "io")
        self.assertEqual(s["settings"], [])
        e = next(e for pd in s["process-data"]["inputs"] for e in pd["entries"]
                 if e["object"] == "0x6001:02")
        e["pin"], e["scale"] = "voltage", 2.0
        s["settings"].append({"object": "0x2000:02", "name": "I2C_address", "type": "USINT",
                              "value": 72})
        data = quirks.from_slave(s)
        self.assertEqual(data["match"], {"vendor": s["identity"]["vendor"],
                                         "product": s["identity"]["product"]})
        self.assertEqual(data["pins"]["0x6001:02"], {"pin": "voltage", "hal-type": "real",
                                                     "scale": 2.0})
        path = quirks.save(s)
        self.assertEqual(os.path.dirname(path), self.dir.name)
        self.assertEqual(quirks.problems, [])
        self.assertEqual(quirks.of(s)["path"], path)

        p = self.proposal()
        again = config.slave_from_proposal(p, "io")
        e = next(e for pd in again["process-data"]["inputs"] for e in pd["entries"]
                 if e["object"] == "0x6001:02")
        self.assertEqual((e["pin"], e["scale"]), ("voltage", 2.0))
        self.assertEqual(again["settings"], [{"object": "0x2000:02", "type": "USINT",
                                              "value": 72, "name": "I2C_address"}])
        self.assertTrue(any("settings 0x2000:02 from the quirk file" in n for n in p.notes))

    def test_no_identity(self):
        with self.assertRaises(quirks.QuirkError):
            quirks.from_slave({"name": "x"})


class TestHal(Folder):
    def cfg(self, hal=""):
        self.write("machine.hal", "net x-enable joint.0.amp-enable-out => x-stm.enable\n" + hal)
        c = {"ecat": 1, "master": {"interface": "x", "transport": "raw"}, "slaves": [stepper()]}
        self.assertEqual(config.check(c), [])
        return c

    def main(self, p):
        return next(ch.new for ch in p.changes if ch.name == "ethercat.hal")

    def test_auto_reset_lines(self):
        p = integrate.plan(self.ini, self.cfg())
        self.assertEqual(p.errors, [])
        main = self.main(p).splitlines()
        self.assertIn("loadrt and2 names=x-stm-auto-reset", main)
        self.assertIn("loadrt edge names=x-stm-enable-edge", main)
        # the functions between ecat.read-all and ecat.write-all
        i = main.index("addf ecat.read-all  servo-thread 1")
        self.assertEqual(main[i + 1:i + 4], ["addf x-stm-enable-edge servo-thread",
                                             "addf x-stm-auto-reset servo-thread",
                                             "addf ecat.write-all servo-thread"])
        for line in ("setp x-stm-enable-edge.in-edge FALSE",
                     "net x-enable => x-stm-enable-edge.in",          # the user's signal
                     "net x-stm-error x-stm.error",                   # made for an output
                     "net x-stm-error => x-stm-auto-reset.in1",
                     "net x-stm-reset x-stm-auto-reset.out => x-stm.reset"):
            self.assertIn(line, main)
        self.assertTrue(any("HAL lines from the quirk file beckhoff-el7041.yaml" in n
                            for n in p.notes))

    def test_left_out(self):
        # enable not driven
        self.write("machine.hal", "")
        c = {"ecat": 1, "master": {"interface": "x", "transport": "raw"}, "slaves": [stepper()]}
        p = integrate.plan(self.ini, c)
        self.assertTrue(any("left out: nothing drives x-stm.enable" in n for n in p.notes), p.notes)
        self.assertNotIn("edge", self.main(p))
        # reset is the user's
        p = integrate.plan(self.ini, self.cfg("net my-reset halui.machine.is-on => x-stm.reset\n"))
        self.assertTrue(any("x-stm.reset is connected in your HAL files" in n for n in p.notes))
        # edge loaded by the user: HAL loads a component once
        p = integrate.plan(self.ini, self.cfg("loadrt edge names=mine\n"))
        self.assertTrue(any("your HAL files load edge" in n for n in p.notes))
        # no pin for the error bit
        c = self.cfg()
        c["slaves"][0]["process-data"]["inputs"] = []
        p = integrate.plan(self.ini, c)
        self.assertTrue(any("0x6010:04 has no pin" in n for n in p.notes))


if __name__ == "__main__":
    unittest.main()
