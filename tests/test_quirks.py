"""Tests of quirk files (quirks.py, ecatsetup/devices/*.yaml) and the HAL
lines they give (integrate.quirk_hal).

Run with: python3 -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
# quirk files: only those that come with the program, unless a test says so
os.environ["ECATSETUP_DEVICES"] = os.path.join(os.path.dirname(__file__), "no-such-dir")

from ecatsetup import config, integrate, quirks  # noqa: E402
from test_integrate import Folder  # noqa: E402

EL7041 = {"vendor": "0x00000002", "product": "0x1B813052"}


def stepper(name="x-stm"):
    def e(obj, pin, bits=1, t="BOOL", **kw):
        return dict({"object": obj, "bits": bits, "type": t, "pin": pin,
                     "hal-type": "bit"}, **kw)
    return {"position": 4, "name": name, "device": "EL7041", "identity": dict(EL7041),
            "profile": "generic", "configure-pdos": True, "process-data": {
                "outputs": [{"pdo": "0x1602", "sm": 2, "entries": [
                    e("0x7010:01", "enable"), e("0x7010:02", "reset")]},
                    {"pdo": "0x1604", "sm": 2, "entries": [
                        e("0x7010:21", "velocity", 16, "INT", scale=32767.0,
                          **{"hal-type": "float"})]}],
                "inputs": [{"pdo": "0x1A03", "sm": 3, "entries": [e("0x6010:04", "error")]}]}}


class TestFiles(unittest.TestCase):
    def tearDown(self):
        quirks.load(reload=True)

    def test_shipped_files_read(self):
        qs = quirks.load(reload=True)
        self.assertEqual(quirks.problems, [])
        self.assertEqual(len(qs), 3)
        ect = quirks.of((0xA88, 0xA880002))
        self.assertEqual(ect["commands"], {0x2010: "Zero Position on the ECT60, a command, not a setting"})
        self.assertEqual(ect["limits"]["following-error"], (0x2022, 0, "uint32"))
        self.assertIn("ignores", ect["limits"]["travel"])
        self.assertEqual(quirks.lcec_sdo((0xA88, 0xA880002))[(0x2010, 0)][0], "cia402.zero-at-start")
        self.assertEqual(quirks.pins((2, 0x1B813052))[(0x7010, 0x21)]["scale"], 32767)
        self.assertEqual(quirks.for_lcec_type("EL7031")["name"],
                         "Beckhoff EL70x1, position interface (lcec el70x1)")
        self.assertEqual(quirks.of((1, 2)), {})

    def test_user_file_wins_and_bad_files_are_reported(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "mine.yaml"), "w") as f:
                f.write("name: my ECT60\nmatch: {vendor: '0xA88', product: '0x0A880002'}\n"
                        "commands: {'0x2011': mine}\n")
            with open(os.path.join(d, "bad.yaml"), "w") as f:
                f.write("name: x\nmatsh: {}\n")
            os.environ["ECATSETUP_DEVICES"] = d
            try:
                quirks.load(reload=True)
                self.assertEqual(quirks.of((0xA88, 0xA880002))["name"], "my ECT60")
                self.assertEqual(quirks.command({"identity": {"vendor": "0xA88",
                                                              "product": "0x0A880002"}}, 0x2011),
                                 "mine")
                self.assertEqual(len(quirks.problems), 1)
                self.assertIn("bad.yaml: unknown keys matsh", quirks.problems[0])
            finally:
                os.environ["ECATSETUP_DEVICES"] = os.path.join(os.path.dirname(__file__),
                                                               "no-such-dir")

    def test_parse_errors(self):
        for data, msg in (({"match": {"vendor": "0x2"}}, "match needs vendor and product"),
                          ({"pins": {"0x7010:01": {}}}, "pins.0x7010:01 needs a pin"),
                          ({"limits": {"travel": 3}}, "limits.travel needs object and type"),
                          ({"commands": {"x": "y"}}, "object 'x' is not a hex number")):
            with self.assertRaisesRegex(quirks.QuirkError, msg):
                quirks.parse(data)


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
