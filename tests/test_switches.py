"""Home and limit switches: 0x60FD read, the switch test's verdicts, and the
search for a drive's input settings (ecatsetup/switches.py)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
if not os.environ.get("ECATSETUP_DEVICES"):
    os.environ["ECATSETUP_DEVICES"] = os.path.join(os.path.dirname(__file__), "no-such-dir")

from ecatsetup import coe, homing, switches  # noqa: E402

ECT60 = {"name": "x", "profile": "cia402", "identity": {"vendor": "0x00000A88", "product": "0x0A880002"},
         "cia402": {"mode": "csp", "modes": ["hm"]},
         "process-data": {"inputs": [{"pdo": "0x1A00", "entries": [
             {"object": "0x6041:00", "use": "profile"},
             {"object": "0x60FD:00", "bits": 32, "type": "UDINT", "pin": "digital-inputs"}]}]}}
OTHER = dict(ECT60, identity={"vendor": "0x00000001", "product": "0x00000001"})

# the three makers' ways (ECT60, A6, EC1), and settings that are not about inputs
SDOS = """\
SDO 0x2000, "Peak Current"
  0x2000:00, rwrwrw, uint16, 16 bit, "Peak Current"
SDO 0x2004, "I/O Parameters"
  0x2004:00, r-r-r-, uint8, 8 bit, "SubIndex 000"
  0x2004:01, rwrwrw, uint16, 16 bit, "DI1 function selection"
  0x2004:02, rwrwrw, uint16, 16 bit, "DI1 logic selection"
  0x2004:03, rwrwrw, uint16, 16 bit, "DI1 filter time"
SDO 0x2005, "Outputs Function"
  0x2005:00, r-r-r-, uint8, 8 bit, "SubIndex 000"
  0x2005:01, rwrwrw, uint16, 16 bit, "Output 1 Function"
SDO 0x2007, "Inputs Function"
  0x2007:00, r-r-r-, uint8, 8 bit, "SubIndex 000"
  0x2007:05, rwrwrw, uint16, 16 bit, "Input 5 Function"
SDO 0x2008, "Inputs Polarity"
  0x2008:00, rwrwrw, uint16, 16 bit, "Inputs Polarity"
SDO 0x2009, "Filter Time"
  0x2009:00, rwrwrw, uint16, 16 bit, "Filter Time"
SDO 0x2701, "Input IO polarity configuration"
  0x2701:00, rwrwrw, uint16, 16 bit, "Input IO polarity configuration"
SDO 0x2702, "Input IO function selection"
  0x2702:00, r-r-r-, uint8, 8 bit, "SubIndex 000"
  0x2702:03, rwrwrw, uint16, 16 bit, "X2 function"
SDO 0x6098, "Homing method"
  0x6098:00, rwrwrw, int8, 8 bit, "Homing method"
"""


class TestRead(unittest.TestCase):
    def test_pin_and_state(self):
        self.assertEqual(switches.input_pin(ECT60), "digital-inputs")
        self.assertIsNone(switches.input_pin(dict(ECT60, **{"process-data": {}})))
        # the plasma cutter's X with its home switch on IN5 pressed (2026-11-24)
        st = switches.state(1245188, ECT60)
        self.assertEqual((st["home"], st["negative"], st["positive"]), (True, False, False))
        # named by the ECT60's quirk file; a drive without one gets the bits
        self.assertEqual(st["inputs"], ["IN1", "IN2", "IN5"])
        self.assertEqual(switches.state(1245188, OTHER)["inputs"],
                         ["input bit 16", "input bit 17", "input bit 20"])

    def test_needed_by_the_method(self):
        s = dict(ECT60)
        self.assertEqual(switches.needed(s), [])
        homing.apply(s, 0, {"method": 21, "fast": 3000})
        self.assertEqual(switches.needed(s), ["home"])
        homing.apply(s, 0, {"method": 35})
        self.assertEqual(switches.needed(s), [])


class TestVerdict(unittest.TestCase):
    def check(self, released, pressed, level, text, kind="home", slave=ECT60):
        got = switches.verdict(kind, released, pressed, slave)
        self.assertEqual(got[0], level, got)
        self.assertIn(text, got[1])

    def test_verdicts(self):
        self.check(0x30000, 0x130004, "ok", "Set up right")
        self.check(0x30004, 0x130000, "warning", "the other way round")
        self.check(0x30000, 0x130000, "warning", "IN5 changes")
        self.check(0x30000, 0x130000, "warning", "input bit 20 changes", slave=OTHER)
        self.check(0x30000, 0x30001, "warning", "works as the drive's negative limit switch")
        self.check(0x30000, 0x30000, "error", "Nothing changed")
        self.check(0x0, 0x1, "ok", "negative limit switch", kind="negative")


class TestInputSettings(unittest.TestCase):
    def test_found_by_name(self):
        found = [(i, s) for i, s, _ in switches.input_settings(ECT60, coe.Dictionary(SDOS))]
        self.assertEqual(found, [(0x2004, 1), (0x2004, 2), (0x2007, 5), (0x2008, 0),
                                 (0x2701, 0), (0x2702, 3)])

    def test_nothing_without_a_dictionary(self):
        self.assertEqual(switches.input_settings(ECT60, None), [])


if __name__ == "__main__":
    unittest.main()
