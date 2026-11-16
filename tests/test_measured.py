import os
import tempfile
import unittest
from datetime import date

from ecatsetup import measured, quirks

ECT = {"name": "x", "device": "ECT60", "identity": {"vendor": "0x00000A88", "product": "0x0A880002"}}
OTHER = {"name": "y", "identity": {"vendor": "0x00000002", "product": "0x03F03052"}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("ECATSETUP_MEASURED")
        self.old_dev = os.environ.get("ECATSETUP_DEVICES")
        os.environ["ECATSETUP_MEASURED"] = self.tmp.name
        os.environ["ECATSETUP_DEVICES"] = os.path.join(self.tmp.name, "devices")

    def tearDown(self):
        if self.old is None:
            os.environ.pop("ECATSETUP_MEASURED", None)
        else:
            os.environ["ECATSETUP_MEASURED"] = self.old
        if self.old_dev is None:
            os.environ.pop("ECATSETUP_DEVICES", None)
        else:
            os.environ["ECATSETUP_DEVICES"] = self.old_dev
        self.tmp.cleanup()
        # report() loads the quirk files of this test's folder into the cache: the
        # next module must not get them (an ECT60 file here hid the shipped one's lcec
        # translations from test_prototype when all ran in one process)
        quirks.load(reload=True)


class TestMeasured(Base):
    def test_shipped_results(self):
        r = measured.results(ECT)
        self.assertEqual([x["period-us"] for x in r], [100, 125, 150, 175, 200, 250])
        self.assertEqual(r[0]["result"], "refused")
        self.assertEqual(r[0]["al-status"], "0x0036")
        self.assertEqual(measured.results(OTHER), [])
        self.assertEqual(measured.results({"name": "z"}), [])
        self.assertEqual(measured.results((0xA88, 0x0A880002)), r)       # a (vendor, product) too

    def test_revisions(self):
        mine = dict(ECT, identity=dict(ECT["identity"], revision="0x00000202"))
        newer = dict(ECT, identity=dict(ECT["identity"], revision="0x00000300"))
        self.assertEqual(measured.other_revisions(mine, measured.results(mine)), [])
        self.assertEqual(measured.other_revisions(newer, measured.results(newer)), [0x202])
        self.assertEqual(measured.other_revisions(ECT, measured.results(ECT)), [])   # unknown: no claim
        self.assertNotIn("check it again", measured.line(mine, 250000))
        self.assertIn("revision 0x00000202, this one is 0x00000300: check it again",
                      measured.line(newer, 250000))
        self.assertTrue(any("may differ" in t for lv, t in measured.problems(newer, 250000)))
        self.assertEqual(measured.problems(mine, 250000), [])
        # a result recorded now says which revision it was measured on
        path = measured.record(newer, 140000, "ok", today=date(2026, 12, 1))
        self.assertEqual(measured._read(path)[0]["results"][0]["revision"], "0x00000300")
        self.assertNotIn("revision", measured._read(measured.record(
            OTHER, 140000, "ok", today=date(2026, 12, 1)))[1]["results"][0])

    def test_report(self):
        text = measured.report(today=date(2026, 12, 1))
        self.assertIn("Rtelligent ECT60: 250 us ok", text)
        self.assertIn("Nothing to look at.", text)
        text = measured.report(today=date(2028, 1, 1))
        self.assertIn("measured 411 days ago, check it again", text)
        # a result of yours that contradicts a shipped one, and one without a revision or date
        with open(measured.user_file(), "w") as f:
            f.write("- name: Rtelligent ECT60\n  match: {vendor: '0x00000A88', product: '0x0A880002'}\n"
                    "  results:\n  - {period-us: 100, result: ok, date: 2026-12-01}\n"
                    "  - {period-us: 90, result: refused}\n")
        os.makedirs(os.environ["ECATSETUP_DEVICES"])
        with open(os.path.join(os.environ["ECATSETUP_DEVICES"], "mine.yaml"), "w") as f:
            f.write("name: mine\nmatch: {vendor: '0x00000A88', product: '0x0A880002'}\n")
        text = measured.report(today=date(2026, 12, 2))
        self.assertIn("your mine.yaml replaces the shipped rtelligent-ect60.yaml", text)
        self.assertIn("100 us: you found ok, the shipped result is refused", text)
        self.assertIn("90 us: no valid date", text)

    def test_verdict(self):
        r = measured.results(ECT)
        v = lambda us: measured.verdict(r, us * 1000)
        self.assertEqual(v(80)[0], "bad")
        self.assertEqual(v(100)[0], "bad")                 # at the period that was refused
        self.assertEqual(v(110)[0], "unknown")             # between: not claimed
        self.assertIn("between 100 and 125", v(110)[1])
        self.assertEqual(v(125), ("ok", "tried ok at this period"))
        self.assertEqual(v(150)[0], "ok")
        self.assertEqual(v(1000)[0], "ok")
        self.assertIsNone(measured.verdict([], 125000))
        self.assertIsNone(measured.verdict(r, 0))
        # only a refusal: nothing is known to work
        only = [{"period-us": 100.0, "result": "refused"}]
        self.assertEqual(measured.verdict(only, 90000)[0], "bad")
        self.assertEqual(measured.verdict(only, 200000)[0], "unknown")

    def test_describe(self):
        text = measured.describe(measured.results(ECT))
        self.assertEqual(text, "250 us ok, 200 us ok, 175 us ok, 150 us ok, 125 us ok, "
                               "100 us refused (AL 0x0036); measured 2026-11-16")
        self.assertEqual(measured.describe([]), "")

    def test_record_and_merge(self):
        path = measured.record(ECT, 110000, "ok", note="halrun", today=date(2026, 12, 1))
        self.assertTrue(path.startswith(self.tmp.name))
        r = measured.results(ECT)
        self.assertEqual([x["period-us"] for x in r], [100, 110, 125, 150, 175, 200, 250])
        self.assertEqual(measured.verdict(r, 110000)[0], "ok")
        self.assertEqual(measured.verdict(r, 105000)[0], "unknown")
        # the same period again: the user's result replaces, and the shipped one is kept under it
        measured.record(ECT, 125000, "refused", al_status=0x36, today=date(2026, 12, 2))
        r = {x["period-us"]: x for x in measured.results(ECT)}
        self.assertEqual(r[125]["result"], "refused")
        self.assertEqual(r[125]["al-status"], "0x0036")
        self.assertEqual(r[125]["date"], "2026-12-02")
        self.assertEqual(r[150]["result"], "ok")
        # a second device is a second entry; the file still reads
        measured.record(OTHER, 250000, "ok", today=date(2026, 12, 3))
        self.assertEqual([x["period-us"] for x in measured.results(OTHER)], [250])
        self.assertEqual(measured.results(OTHER)[0]["date"], "2026-12-03")
        # recording twice the same period does not duplicate it
        measured.record(OTHER, 250000, "ok", today=date(2026, 12, 4))
        self.assertEqual(len(measured.results(OTHER)), 1)

    def test_record_errors(self):
        with self.assertRaises(ValueError):
            measured.record({"name": "n"}, 125000, "ok")
        with self.assertRaises(ValueError):
            measured.record(ECT, 125000, "maybe")

    def test_a_broken_user_file_is_ignored(self):
        with open(measured.user_file(), "w") as f:
            f.write("not: [a list")
        self.assertEqual(len(measured.results(ECT)), 6)


if __name__ == "__main__":
    unittest.main()
