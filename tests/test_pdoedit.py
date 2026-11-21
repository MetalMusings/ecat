"""pdoedit: adding objects and PDOs to a slave's process data, removing entries."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ecatsetup import coe, pdoedit, pdoreq
from test_pdoreq import MAPPABLE, fslave, make, obj, pdo, ESI_PDOS


def entries(slave, direction="outputs", pos=0):
    return [e["object"] for e in slave["process-data"][direction][pos]["entries"]]


class TestAddObject(unittest.TestCase):
    def setUp(self):
        self.dev = make(MAPPABLE + [obj(0x60F4, "T"), obj(0x2000, "RT")], ESI_PDOS)
        self.dev.max_entries[0x1600] = 3
        self.d = coe.Dictionary()
        self.d.entries[(0x60F4, 0)] = coe.CoeEntry(0x60F4, 0, "r-r-r-", "int32", 32, "Following error actual value")

    def slave(self, **kw):
        return fslave([(0x1600, 2, [(0x6040, 16)])], [(0x1A00, 3, [(0x6041, 16)])], **kw)

    def test_a_profile_object_gets_no_pin(self):
        s = self.slave()
        e = pdoedit.add_object(s, self.dev, self.d, 0x607A)
        self.assertEqual(e, {"object": "0x607A:00", "bits": 32, "type": "DINT", "use": "profile"})
        self.assertEqual(entries(s), ["0x6040:00", "0x607A:00"])

    def test_the_direction_comes_from_the_mapping_flag(self):
        s = self.slave()
        pdoedit.add_object(s, self.dev, self.d, 0x6064)
        self.assertEqual(entries(s, "inputs"), ["0x6041:00", "0x6064:00"])

    def test_known_pin_names_for_other_drive_objects(self):
        s = self.slave()
        e = pdoedit.add_object(s, self.dev, self.d, 0x60F4)
        self.assertEqual((e["pin"], e["hal-type"]), ("following-error", "sint"))

    def test_object_both_ways_needs_a_direction(self):
        with self.assertRaises(pdoedit.EditError) as cm:
            pdoedit.add_object(self.slave(), self.dev, self.d, 0x2000)
        self.assertIn("output or an input", str(cm.exception))

    def test_object_that_cannot_be_mapped_that_way(self):
        with self.assertRaises(pdoedit.EditError) as cm:
            pdoedit.add_object(self.slave(), self.dev, self.d, 0x6064, direction="rx")
        self.assertIn("only be mapped as TX", str(cm.exception))

    def test_dictionary_gives_the_size_of_an_unlisted_object(self):
        d = coe.Dictionary()
        d.entries[(0x2000, 0)] = coe.CoeEntry(0x2000, 0, "rwrwrw", "uint16", 16, "User value")
        s = self.slave()
        e = pdoedit.add_object(s, self.dev, d, 0x2000, direction="rx")
        self.assertEqual((e["bits"], e["type"], e["pin"]), (16, "UINT", "user-value"))

    def test_unknown_size_is_refused(self):
        dev = make(MAPPABLE + [obj(0x2001, "R")], ESI_PDOS)
        with self.assertRaises(pdoedit.EditError) as cm:
            pdoedit.add_object(self.slave(), dev, self.d, 0x2001)
        self.assertIn("size is not known", str(cm.exception))

    def test_fixed_pdo_is_refused_with_the_way_out(self):
        s = fslave([(0x1701, 2, [(0x6040, 16)])], [(0x1A00, 3, [(0x6041, 16)])])
        with self.assertRaises(pdoedit.EditError) as cm:
            pdoedit.add_object(s, self.dev, self.d, 0x607A)
        self.assertIn("0x1701 are fixed", str(cm.exception))
        self.assertEqual(entries(s), ["0x6040:00"])

    def test_the_entry_limit(self):
        s = fslave([(0x1600, 2, [(0x6040, 16), (0x607A, 32), (0x60FF, 32)])], [])
        with self.assertRaises(pdoedit.EditError) as cm:
            pdoedit.add_object(s, self.dev, self.d, 0x6060)
        self.assertIn("at most 3", str(cm.exception))

    def test_a_direction_with_no_pdo_gets_the_devices_open_one(self):
        s = fslave([], [(0x1A00, 3, [(0x6041, 16)])])
        pdoedit.add_object(s, self.dev, self.d, 0x6060)
        self.assertEqual([p["pdo"] for p in s["process-data"]["outputs"]], ["0x1600"])
        self.assertIn("0x6060:00", entries(s))

    def test_pin_names_stay_unique(self):
        s = self.slave()
        pdoedit.add_object(s, self.dev, self.d, 0x60F4)
        s["process-data"]["inputs"][0]["entries"].pop()
        s["process-data"]["inputs"][0]["entries"].append({"object": "0x9999:00", "bits": 16, "pin": "following-error"})
        e = pdoedit.add_object(s, self.dev, self.d, 0x60F4)
        self.assertEqual(e["pin"], "following-error-2")

    def test_candidates_are_the_mappable_objects_not_yet_in_the_file(self):
        found = {(i, d) for i, _s, _n, d in pdoedit.candidates(self.slave(), self.dev, self.d)}
        self.assertIn((0x607A, "rx"), found)
        self.assertIn((0x6064, "tx"), found)
        self.assertIn((0x2000, "rt"), found)
        self.assertNotIn((0x6040, "rx"), found)


class TestRemoveAndAddPdo(unittest.TestCase):
    def setUp(self):
        self.dev = make(MAPPABLE, ESI_PDOS)

    def test_remove(self):
        s = fslave([(0x1600, 2, [(0x6040, 16), (0x607A, 32)])], [])
        gone = pdoedit.remove_entry(s, self.dev, "outputs", 0, 1)
        self.assertEqual(gone["object"], "0x607A:00")
        self.assertEqual(entries(s), ["0x6040:00"])

    def test_the_last_entry_takes_the_pdo_with_it(self):
        s = fslave([(0x1600, 2, [(0x6040, 16)])], [])
        pdoedit.remove_entry(s, self.dev, "outputs", 0, 0)
        self.assertEqual(s["process-data"]["outputs"], [])

    def test_fixed_pdo_cannot_be_edited(self):
        s = fslave([(0x1701, 2, [(0x6040, 16), (0x607A, 32)])], [])
        with self.assertRaises(pdoedit.EditError):
            pdoedit.remove_entry(s, self.dev, "outputs", 0, 0)
        self.assertEqual(len(entries(s)), 2)

    def test_add_pdo_with_its_entries(self):
        s = fslave([], [(0x1A00, 3, [(0x6041, 16)])])
        p = pdoedit.add_pdo(s, self.dev, 0x1701)
        self.assertEqual((p["pdo"], p["sm"]), ("0x1701", 2))
        self.assertEqual([e["object"] for e in p["entries"]], ["0x6040:00", "0x607A:00"])
        self.assertTrue(all(e["use"] == "profile" for e in p["entries"]))

    def test_addable_pdos_leave_out_used_and_excluded(self):
        s = fslave([(0x1701, 2, [(0x6040, 16), (0x607A, 32)])], [])
        self.assertEqual(sorted(p.index for p in pdoedit.addable_pdos(s, self.dev)), [0x1600, 0x1A00])
        with self.assertRaises(pdoedit.EditError):
            pdoedit.add_pdo(s, self.dev, 0x1702)        # excluded by 0x1701

    def test_exclude_given_by_one_side_only_counts(self):
        dev = make(MAPPABLE, [pdo("RxPdo", 0x1701, [(0x6040, 16)], fixed=True, exclude=[0x1702]),
                              pdo("RxPdo", 0x1702, [(0x6040, 16), (0x607A, 32)], fixed=True)])
        s = fslave([(0x1701, 2, [(0x6040, 16)])], [])
        self.assertEqual(pdoedit.addable_pdos(s, dev), [])
        s = fslave([(0x1702, 2, [(0x6040, 16), (0x607A, 32)])], [])
        self.assertEqual(pdoedit.addable_pdos(s, dev), [])

    def test_assignment_that_cannot_change(self):
        dev = make(MAPPABLE, ESI_PDOS, pdo_assign=False)
        self.assertEqual(pdoedit.addable_pdos(fslave([], []), dev), [])

    def test_add_missing_reports_what_could_not_be_added(self):
        dev = make(MAPPABLE, ESI_PDOS)
        s = fslave([(0x1701, 2, [(0x6040, 16), (0x607A, 32)])], [(0x1A00, 3, [(0x6041, 16)])])
        added, failed = pdoedit.add_missing(s, dev, coe.Dictionary(),
                                            [(0x6064, "tx"), (0x6060, "rx")])
        self.assertEqual([e["object"] for e in added], ["0x6064:00"])
        self.assertEqual(len(failed), 1)
        self.assertIn("fixed", failed[0])


if __name__ == "__main__":
    unittest.main()
