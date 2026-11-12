"""Tests of the homing method catalog (homing.py) and its quirk keys.

Run with: python3 -m unittest discover -s tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["ECATSETUP_DEVICES"] = os.path.join(os.path.dirname(__file__), "no-such-dir")
os.environ["ECATSETUP_SETTINGS"] = os.path.join(os.path.dirname(__file__), "no-such-dir", "settings.yaml")
# state (test lock, drive limits): never the user's own
if "ECATSETUP_TEST_STATE" not in os.environ:
    os.environ["ECATSETUP_TEST_STATE"] = os.environ["XDG_STATE_HOME"] = \
        __import__("tempfile").mkdtemp(prefix="ecatsetup-test-state-")

from ecatsetup import homing, quirks  # noqa: E402

ECT = {"position": 0, "identity": {"vendor": "0x00000A88", "product": "0x0A880002"}}
OTHER = {"position": 1, "identity": {"vendor": "0x00000001", "product": "0x00000001"}}


def reader(table):
    return lambda i, s, t: table.get((i, s))


class TestHoming(unittest.TestCase):
    def test_catalog(self):
        self.assertTrue(homing.CATALOG[17].moves)
        self.assertFalse(homing.CATALOG[35].moves)
        self.assertIn(homing.INDEX, homing.CATALOG[1].needs)
        self.assertNotIn(homing.INDEX, homing.CATALOG[17].needs)
        self.assertNotIn(15, homing.CATALOG)

    def test_quirk_file_decides_support(self):
        nums, where = homing.supported(ECT, read=reader({}))
        self.assertEqual(nums, [17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 35, 36, 37])
        self.assertEqual(where, "quirk file")

    def test_drive_object_when_no_quirk(self):
        nums, where = homing.supported(OTHER, read=reader(
            {(0x60E3, 0): 3, (0x60E3, 1): 1, (0x60E3, 2): 17, (0x60E3, 3): 35}))
        self.assertEqual((nums, where), ([1, 17, 35], "drive (0x60E3)"))

    def test_unknown_support(self):
        self.assertEqual(homing.supported(OTHER, read=reader({})), (None, None))
        self.assertTrue(all(ok is None for _, ok in homing.choices(OTHER, read=reader({}))))

    def test_choices_mark_unsupported(self):
        got = {m.number: ok for m, ok in homing.choices(ECT)}
        self.assertTrue(got[36])
        self.assertFalse(got[1])
        self.assertIn("Torque homing", homing.method(36, ECT).text)
        self.assertIn("Manufacturer", homing.method(36, OTHER).text)

    def test_plan(self):
        p = homing.plan(OTHER, 17, {"fast": 3000, "slow": 1000, "accel": 5000, "offset": 0})
        self.assertEqual([(s["object"], s["value"]) for s in p],
                         [("0x6098:00", 17), ("0x6099:01", 3000), ("0x6099:02", 1000),
                          ("0x609A:00", 5000), ("0x607C:00", 0)])
        self.assertEqual(p[0]["type"], "SINT")

    def test_plan_stay_has_only_the_offset(self):
        p = homing.plan(OTHER, 35, {"fast": 3000, "offset": 7})
        self.assertEqual([s["object"] for s in p], ["0x6098:00", "0x607C:00"])

    def test_plan_adds_the_drive_extras_and_moves_to_the_axis(self):
        p = homing.plan(ECT, 36, {"fast": 3000}, axis=1)
        objs = [s["object"] for s in p]
        self.assertEqual(objs[:2], ["0x6898:00", "0x6899:01"])
        self.assertIn("0x2030:01", objs)
        self.assertEqual(len(p), 2 + 7)
        self.assertEqual(len(homing.plan(ECT, 17, {"fast": 1})), 2)

    def test_bad_quirk(self):
        with self.assertRaises(quirks.QuirkError):
            quirks.parse({"homing": {"nope": 1}})
        with self.assertRaises(quirks.QuirkError):
            quirks.parse({"homing": {"extras": {36: [{"object": "0x2030:01"}]}}})


if __name__ == "__main__":
    unittest.main()


class TestChoice(unittest.TestCase):
    def slave(self, **kw):
        s = {"position": 0, "name": "x", "identity": dict(OTHER["identity"]),
             "settings": [{"object": "0x2000:01", "type": "UINT", "value": 5}]}
        s.update(kw)
        return s

    def test_apply_and_change(self):
        s = self.slave()
        homing.apply(s, 0, {"method": 17, "fast": 3000, "slow": 1000})
        self.assertEqual(s["homing"], {"method": 17, "fast": 3000, "slow": 1000})
        self.assertEqual([x["object"] for x in s["settings"]],
                         ["0x2000:01", "0x6098:00", "0x6099:01", "0x6099:02"])
        homing.apply(s, 0, {"method": 35})
        self.assertEqual([x["object"] for x in s["settings"]], ["0x2000:01", "0x6098:00"])
        homing.apply(s, 0, None)
        self.assertNotIn("homing", s)
        self.assertEqual(s["settings"], [{"object": "0x2000:01", "type": "UINT", "value": 5}])

    def test_extras_go_with_the_method(self):
        s = self.slave(identity=dict(ECT["identity"]), settings=[])
        homing.apply(s, 0, {"method": 36, "fast": 3000})
        self.assertIn("0x2030:01", [x["object"] for x in s["settings"]])
        homing.apply(s, 0, {"method": 17, "fast": 3000})
        self.assertNotIn("0x2030:01", [x["object"] for x in s["settings"]])

    def test_choice_reads_both_forms(self):
        self.assertEqual(homing.choice({"homing": {"method": 17}}), {"method": 17})
        self.assertIsNone(homing.choice({"homing": {"method": 17}}, 1))
        self.assertEqual(homing.choice({"homing": [None, {"method": 18}]}, 1), {"method": 18})
        self.assertIsNone(homing.choice({}))


class TestIniHelp(unittest.TestCase):
    def test_none_without_a_choice(self):
        self.assertIsNone(homing.ini_help({}, {"name": "x"}))

    def test_lines_for_the_joint_of_the_drive(self):
        cfg = {"linuxcnc": {"joints": [{"joint": 2, "slave": "z", "scale": 1.0}]}}
        s = {"name": "z", "homing": {"method": 17}}
        lines = homing.ini_help(cfg, s)
        text = "\n".join(t for t, _ in lines)
        self.assertIn("[JOINT_2]", text)
        self.assertIn("HOME_SEARCH_VEL = 0", text)
        self.assertIn("HOME_USE_INDEX = YES", text)
        self.assertIn("joint.2.index-enable <=> z.index-enable", text)
        self.assertIn("MOVES", text)
        self.assertIn("does not move", "\n".join(
            t for t, _ in homing.ini_help(cfg, {"name": "z", "homing": {"method": 35}})))
        self.assertIn("[JOINT_N]", homing.ini_help({}, s)[0][0])
