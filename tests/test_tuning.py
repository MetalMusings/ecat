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


class FakeDrive:
    def __init__(self, values, refuse=(), ignore=()):
        self.values = dict(values)
        self.refuse, self.ignore = set(refuse), set(ignore)
        self.writes = []
        self.seen_in_file = []      # what the trials file held at each write

    def read(self, index, sub, _type):
        return self.values.get((index, sub))

    def write(self, index, sub, value, _type):
        self.seen_in_file.append([(e["index"], e["original"]) for e in tuning.pending()])
        if (index, sub) in self.refuse:
            raise RuntimeError("SDO abort 0x06090030")
        self.writes.append((index, sub, int(value)))
        if (index, sub) not in self.ignore:
            self.values[(index, sub)] = int(value)


class TestTrials(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        p = mock.patch.object(tuning, "TRIALS_FILE", os.path.join(d.name, "trials.json"))
        p.start()
        self.addCleanup(p.stop)
        self.drive = FakeDrive({(0x2009, 0): 25600, (0x6041, 0): 0x0231, (0x2080, 0): 3})
        self.filt = tuning.Candidate(0x2009, 0, "Filter Time", "command-filter", "uint16",
                                     maximum=25600, minimum=0)

    def tried(self, c, value):
        return tuning.try_value(ECT60, c, value, self.drive.read, self.drive.write)

    def test_try_keep(self):
        e = self.tried(self.filt, "6400")
        self.assertEqual((e["original"], e["value"]), (25600, 6400))
        # the drive's own value was in the file before anything was written
        self.assertEqual(self.drive.seen_in_file[0], [(0x2009, 25600)])
        self.assertEqual(self.drive.values[(0x2009, 0)], 6400)
        # a second try keeps the drive's own value, not the first try
        self.tried(self.filt, 2000)
        self.assertEqual([(x["original"], x["value"]) for x in tuning.pending()], [(25600, 2000)])
        slave = {"name": "x", "position": 0, "settings": [
            {"object": "0x2000:00", "type": "UINT", "value": 4000}]}
        st = tuning.keep(tuning.pending()[0], slave)
        self.assertEqual(st, {"object": "0x2009:00", "name": "Filter Time", "type": "UINT",
                              "value": 2000})
        self.assertEqual(len(slave["settings"]), 2)
        self.assertEqual(tuning.pending(), [])
        self.assertFalse(os.path.exists(tuning.TRIALS_FILE))
        # kept again: the setting is replaced, not doubled
        self.tried(self.filt, 3000)
        tuning.keep(tuning.pending()[0], slave)
        self.assertEqual([x["value"] for x in slave["settings"]], [4000, 3000])

    def test_undo(self):
        e = self.tried(self.filt, 2000)
        tuning.undo(e, self.drive.write)
        self.assertEqual(self.drive.values[(0x2009, 0)], 25600)
        self.assertEqual(tuning.pending(), [])

    def test_refused(self):
        with self.assertRaisesRegex(tuning.TrialError, "takes 0..25600"):
            self.tried(self.filt, 30000)
        with self.assertRaisesRegex(tuning.TrialError, "whole number"):
            self.tried(self.filt, "2.5")
        self.drive.values[(0x6041, 0)] = 0x0237      # Operation enabled
        with self.assertRaisesRegex(tuning.TrialError, "enabled"):
            self.tried(self.filt, 2000)
        self.assertEqual(self.drive.writes, [])
        self.drive.values[(0x6041, 0)] = 0x0231
        # the drive refuses it: its own value is written back, nothing is left pending
        self.drive.refuse = {(0x2009, 0)}
        with self.assertRaisesRegex(tuning.TrialError, "refused"):
            self.tried(self.filt, 2000)
        self.assertEqual(tuning.pending(), [])
        self.assertEqual(self.drive.writes, [])
        # refused after an earlier try: that one is still in the drive, and in the file
        self.drive.refuse = set()
        self.tried(self.filt, 6400)
        self.drive.refuse = {(0x2009, 0)}
        with self.assertRaises(tuning.TrialError):
            self.tried(self.filt, 2000)
        self.assertEqual([(e["original"], e["value"]) for e in tuning.pending()], [(25600, 6400)])

    def test_a_drive_that_does_not_take_it(self):
        self.drive.ignore = {(0x2009, 0)}
        with self.assertRaisesRegex(tuning.TrialError, "did not take it"):
            self.tried(self.filt, 2000)
        self.assertEqual(self.drive.writes[-1], (0x2009, 0, 25600))
        self.assertEqual(tuning.pending(), [])

    def test_undo_all(self):
        other = tuning.Candidate(0x2080, 0, "Command filter", "command-filter", "uint16")
        self.tried(self.filt, 2000)
        self.tried(other, 5)
        self.drive.refuse = {(0x2080, 0)}
        done, failed = tuning.undo_all(lambda pos: self.drive.write)
        self.assertEqual(done, ["slave 0 0x2009:00 back to 25600"])
        self.assertIn("0x2080:00 back to 3", failed[0])
        # what could not be written back stays in the file
        self.assertEqual([(e["index"], e["original"]) for e in tuning.pending()], [(0x2080, 3)])
