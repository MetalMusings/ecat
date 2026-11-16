"""Tests of the search for a drive's settings (tuning.py) and the quirk
keys `tuning:` and `measure:`.

Run with: python3 -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# quirk files: only those that come with the program
os.environ["ECATSETUP_DEVICES"] = os.path.join(os.path.dirname(__file__), "no-such-dir")
os.environ["ECATSETUP_SETTINGS"] = os.path.join(os.path.dirname(__file__), "no-such-dir", "settings.yaml")

from ecatsetup import coe, quirks, response, tuning  # noqa: E402

ECT60 = {"name": "x", "position": 0, "profile": "cia402", "cia402": {"mode": "csp"},
         "identity": {"vendor": "0x00000A88", "product": "0x0A880002"},
         "process-data": {"inputs": [{"pdo": "0x1A00", "entries": [
             {"object": "0x6041:00", "bits": 16, "type": "UINT", "use": "profile"},
             {"object": "0x6077:00", "bits": 16, "type": "INT", "pin": "actual-torque",
              "hal-type": "sint"},
             {"object": "0x60F4:00", "bits": 32, "type": "DINT", "pin": "following-error",
              "hal-type": "sint"}]}]}}
OTHER = dict(ECT60, identity={"vendor": "0x00000099", "product": "0x00000001"})

# part of the ECT60's dictionary as `ethercat sdos` lists it
SDOS = """\
SDO 0x2009, "Filter Time"
  0x2009:00, rwrwrw, uint16, 16 bit, "Filter Time"
SDO 0x2010, "Zero Position"
  0x2010:00, rwrwrw, uint16, 16 bit, "Zero Position"
SDO 0x2011, "Control mode"
  0x2011:00, rwrwrw, uint16, 16 bit, "Control mode"
SDO 0x2023, "Position loop parameters"
  0x2023:00, r-r-r-, uint8, 8 bit, "SubIndex 000"
  0x2023:01, rwrwrw, uint16, 16 bit, "PosLoop_Kp"
  0x2023:02, rwrwrw, uint16, 16 bit, "PosLoop_Ki"
  0x2023:04, rwrwrw, uint16, 16 bit, "PosLoop_Kvff"
SDO 0x2026, "Servo mode2 parameters"
  0x2026:00, r-r-r-, uint8, 8 bit, "SubIndex 000"
  0x2026:01, rwrwrw, uint16, 16 bit, "PVIA_Kp"
SDO 0x200b, "Current loop parameters"
  0x200b:00, r-r-r-, uint8, 8 bit, "SubIndex 000"
  0x200b:02, rwrwrw, uint16, 16 bit, "Iloop_Kp"
SDO 0x204b, "INPort filter"
  0x204b:00, rwrwrw, uint16, 16 bit, "INPort filter"
SDO 0x2060, "Amplitude of First Anti-Vibration"
  0x2060:00, rwrwrw, uint16, 16 bit, "Amplitude of First Anti-Vibration"
SDO 0x2070, "Smoothing time"
  0x2070:00, r-r-r-, uint16, 16 bit, "Smoothing time"
SDO 0x2080, "Command filter"
  0x2080:00, rwrwrw, uint16, 16 bit, "Command filter"
SDO 0x6065, "Following error window"
  0x6065:00, rwrwrw, uint32, 32 bit, "Following error window"
"""

VALUES = {(0x2009, 0): 25600, (0x2011, 0): 1, (0x2023, 1): 4000, (0x2023, 2): 0,
          (0x2023, 4): 30, (0x2026, 1): 2000, (0x2060, 0): 0, (0x2080, 0): 7}


def read(index, sub, _type):
    return VALUES.get((index, sub))


class TestKinds(unittest.TestCase):
    def test_names(self):
        self.assertEqual([tuning.kind_of(n) for n in (
            "Filter Time", "Position loop parameters: PosLoop_Kvff", "PosLoop_Kp",
            "Speed feedforward gain", "Load inertia ratio", "Notch filter 1 frequency",
            "INPort filter", "IN Port filter", "Input filter", "Current loop parameters: Iloop_Kp",
            "Alarm Code", "Peak Current")],
            ["command-filter", "feedforward", "position-loop", "feedforward", "torque",
             "gain-high", None, None, None, None, None, None])


class TestCandidates(unittest.TestCase):
    def test_the_ect60(self):
        found = tuning.candidates(ECT60, coe.Dictionary(SDOS), read,
                                  kinds=["command-filter", "feedforward"])
        objs = [c.obj for c in found]
        # what the result points at first; the quirk file's before those found by name
        self.assertEqual(objs[:2], ["0x2009:00", "0x2080:00"])
        f = found[0]
        self.assertTrue(f.from_quirk)
        self.assertEqual((f.value, f.maximum, f.factory, f.edge), (25600, 25600, 6400, "max"))
        self.assertIn("at the maximum of its range", f.why())
        self.assertIn("tried: 25600 -> 2000", f.why())
        self.assertEqual(found[2].obj, "0x2023:04")     # feedforward next
        # a mode's settings: the other mode's are kept, marked, after
        kp2 = next(c for c in found if c.obj == "0x2026:01")
        self.assertFalse(kp2.applies)
        self.assertIn("not used in the drive's present mode", kp2.why())
        self.assertTrue(next(c for c in found if c.obj == "0x2023:01").applies)
        # not: commands, input filters, the current loop, read-only, CiA 402, the count
        for obj in ("0x2010:00", "0x204B:00", "0x200B:02", "0x2070:00", "0x6065:00", "0x2023:00"):
            self.assertNotIn(obj, objs)
        self.assertIn("0x2060:00", objs)

    def test_a_drive_without_a_quirk_file(self):
        d = coe.Dictionary(SDOS)
        # an ESI range makes a value at its end stand out
        e = d.entries[(0x2080, 0)]
        d.entries[(0x2080, 0)] = coe.CoeEntry(e.index, e.subindex, e.access, e.data_type,
                                              e.bitlen, e.name, 0, 7, 2)
        found = tuning.candidates(OTHER, d, read, kinds=["command-filter"])
        self.assertEqual([c.obj for c in found][:2], ["0x2080:00", "0x2009:00"])
        self.assertEqual(found[0].edge, "max")
        self.assertFalse(any(c.from_quirk for c in found))
        # the ECT60's 0x2010 is a command only by its quirk file
        self.assertNotIn("0x2010:00", [c.obj for c in found])

    def test_the_quirk_file_first(self):
        # an entry found by name at the end of its range comes after the quirk file's
        d = coe.Dictionary(SDOS)
        e = d.entries[(0x2080, 0)]
        d.entries[(0x2080, 0)] = coe.CoeEntry(e.index, e.subindex, e.access, e.data_type,
                                              e.bitlen, e.name, 0, 7, 2)
        with mock.patch.dict(VALUES, {(0x2009, 0): 2000}):
            found = tuning.candidates(ECT60, d, read, kinds=["command-filter"])
        self.assertEqual([(c.obj, c.edge) for c in found][:2],
                         [("0x2009:00", None), ("0x2080:00", "max")])

    def test_not_read(self):
        found = tuning.candidates(ECT60, coe.Dictionary(SDOS))
        self.assertTrue(all(c.value is None and c.edge is None for c in found))

    def test_process_data_is_left_out(self):
        s = dict(ECT60, **{"process-data": {"inputs": [{"pdo": "0x1A00", "entries": [
            {"object": "0x2080:00", "bits": 16, "type": "UINT", "pin": "f", "hal-type": "uint"}]}]}})
        self.assertNotIn("0x2080:00", [c.obj for c in tuning.candidates(s, coe.Dictionary(SDOS))])


class TestQuirkKeys(unittest.TestCase):
    def test_parse(self):
        q = quirks.parse({"tuning": {"0x2009": {"kind": "command-filter", "type": "UINT",
                                                "max": 25600, "when": {"object": "0x2011:00",
                                                                        "values": [1]}}},
                          "measure": {"ignore": {"0x6077": "no torque"}}})
        self.assertEqual(q["tuning"][0]["index"], 0x2009)
        self.assertEqual(q["tuning"][0]["when"], (0x2011, 0, [1], "uint16"))
        self.assertEqual(q["measure-ignore"], {0x6077: "no torque"})
        for bad in ({"tuning": {"0x2009": {"kind": "nonsense", "type": "UINT"}}},
                    {"tuning": {"0x2009": {"kind": "command-filter"}}},
                    {"tuning": {"0x2009": {"kind": "command-filter", "type": "UINT",
                                           "when": {"object": "0x2011"}}}},
                    {"measure": ["0x6077"]}):
            with self.assertRaises(quirks.QuirkError):
                quirks.parse(bad)

    def test_ect60_torque_is_not_recorded(self):
        # its 0x6077 reads a constant: not a column, not offered to add
        self.assertEqual(sorted(response.measure_entries(ECT60)), ["drive-error"])
        self.assertEqual(sorted(response.measure_entries(OTHER)), ["drive-error", "torque"])
        d = coe.Dictionary(SDOS + 'SDO 0x6077, "Torque"\n  0x6077:00, r-r-r-, int16, 16 bit, "T"\n')
        bare = dict(ECT60, **{"process-data": {"inputs": [{"pdo": "0x1A00", "entries": []}]}})
        self.assertEqual(response.missing_objects(bare, d), [])
        self.assertEqual([m[0] for m in response.missing_objects(dict(bare, identity=OTHER["identity"]), d)],
                         [0x6077])


if __name__ == "__main__":
    unittest.main()
