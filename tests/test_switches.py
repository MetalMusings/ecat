"""Home and limit switches as the standard has them: 0x60FD bits 0 to 2, and the
switches a homing method needs (ecatsetup/switches.py)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
if not os.environ.get("ECATSETUP_DEVICES"):
    os.environ["ECATSETUP_DEVICES"] = os.path.join(os.path.dirname(__file__), "no-such-dir")

from ecatsetup import homing, quirks, switches  # noqa: E402

ECT60 = {"name": "x", "profile": "cia402", "identity": {"vendor": "0x00000A88", "product": "0x0A880002"},
         "cia402": {"mode": "csp", "modes": ["hm"]},
         "process-data": {"inputs": [{"pdo": "0x1A00", "entries": [
             {"object": "0x6041:00", "use": "profile"},
             {"object": "0x60FD:00", "bits": 32, "type": "UDINT", "pin": "digital-inputs"}]}]}}


class TestRead(unittest.TestCase):
    def test_pin_and_state(self):
        self.assertEqual(switches.input_pin(ECT60), "digital-inputs")
        self.assertIsNone(switches.input_pin(dict(ECT60, **{"process-data": {}})))
        # the plasma cutter's X with its home switch on IN5 pressed (2026-11-24)
        self.assertEqual(switches.state(1245188), {"home": True, "negative": False, "positive": False})
        self.assertEqual(switches.state(0b011), {"home": False, "negative": True, "positive": True})
        # the physical inputs (bits 16 and up) are not switches to the program
        self.assertEqual(switches.state(0xFFFF0000), {"home": False, "negative": False, "positive": False})

    def test_a_bit_field(self):
        # 0x60FD as a bit field (the user, 2026-11-28): each switch from its bit's own pin
        e = {"object": "0x60FD:00", "bits": 32, "type": "UDINT",
             "split": ["neg", "", "home"] + [""] * 17 + ["in5"]}
        s = dict(ECT60, **{"process-data": {"inputs": [{"pdo": "0x1A00", "entries": [e]}]}})
        self.assertIsNone(switches.input_pin(s))
        self.assertEqual(switches.sources(s), {"home": ("home", None), "negative": ("neg", None)})
        pins = {"home": "TRUE", "neg": "FALSE"}
        self.assertEqual(switches.read(switches.sources(s), pins.get), {"home": True, "negative": False})
        self.assertEqual(switches.read(switches.sources(s), {}.get), {})
        # the whole object's pin: the bits of its value
        src = switches.sources(ECT60)
        self.assertEqual(src["home"], ("digital-inputs", 2))
        self.assertEqual(switches.read(src, {"digital-inputs": "0x00100004"}.get),
                         {"home": True, "negative": False, "positive": False})
        self.assertEqual(switches.sources(dict(ECT60, **{"process-data": {}})), {})

    def test_needed_by_the_method(self):
        s = dict(ECT60)
        self.assertEqual(switches.needed(s), [])
        homing.apply(s, 0, {"method": 21, "fast": 3000})
        self.assertEqual(switches.needed(s), ["home"])
        homing.apply(s, 0, {"method": 35})
        self.assertEqual(switches.needed(s), [])

    def test_the_guide_names_the_bit_and_sends_to_the_manual(self):
        text = switches.guide(["home"])
        self.assertIn("0x60FD home switch bit 2", text)
        self.assertIn("manual", text)
        self.assertIn("negative limit switch bit 0", switches.guide(["negative"]))


class TestQuirks(unittest.TestCase):
    def test_old_input_keys_are_taken_without_them(self):
        q = quirks.parse({"name": "x", "match": {"vendor": "0x1", "product": "0x1"},
                          "homing": {"methods": [21], "input-bits": {"IN5": 20},
                                     "inputs": {"function": {}}}}, "x.yaml")
        self.assertNotIn("homing", q)        # retired with the other fact keys (2026-11-28)


if __name__ == "__main__":
    unittest.main()
