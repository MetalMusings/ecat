import os
import tempfile
import unittest

from ecatsetup import coe, esi, guidance, quirks
from ecatsetup.session import Session

DATA = os.path.join(os.path.dirname(__file__), "data")
ESI = os.path.join(DATA, "dict-esi.xml")


def device():
    return esi.load_file(ESI)[0]


class TestEsiDictionary(unittest.TestCase):
    def setUp(self):
        self.d = esi.dictionary(device())

    def test_a_compound_object_gives_its_sub_items(self):
        e = self.d.entries[(0x8000, 1)]
        self.assertEqual((e.name, e.data_type, e.bitlen), ("Current", "uint16", 16))
        self.assertEqual((e.minimum, e.maximum, e.default), (0, 8000, 5000))
        self.assertEqual(self.d.objects[0x8000], "Motor settings")

    def test_default_data_is_read_as_a_signed_number(self):
        self.assertEqual(self.d.entries[(0x8000, 2)].default, -1)
        self.assertEqual(self.d.entries[(0x8000, 0)].default, 3)

    def test_access_follows_the_restrictions(self):
        self.assertEqual(self.d.entries[(0x8000, 1)].access, "rwrwrw")
        self.assertEqual(self.d.entries[(0x8000, 3)].access, "r-r-r-")
        self.assertEqual(self.d.entries[(0x9000, 0)].access, "rwr-r-")
        self.assertTrue(self.d.entries[(0x9000, 0)].writable_preop)
        self.assertFalse(self.d.entries[(0x8000, 3)].writable_preop)

    def test_simple_objects_arrays_and_strings(self):
        self.assertEqual(self.d.entries[(0x9000, 0)].default, 2)
        self.assertEqual(self.d.entries[(0x9001, 0)].data_type, "string")
        self.assertEqual([self.d.entries[(0x8001, i)].data_type for i in (0, 1, 2)],
                         ["uint8", "uint16", "uint16"])

    def test_a_file_without_the_device_gives_nothing(self):
        other = esi.EsiDevice(ESI, 2, 0x9999, 0, "x", "x")
        self.assertFalse(esi.dictionary(other).entries)
        self.assertFalse(esi.dictionary(esi.EsiDevice("/nonexistent.xml", 2, 1, 0, "x", "x")).entries)


class TestEnriched(unittest.TestCase):
    TEXT = ('SDO 0x8000, "Motor settings"\n'
            '  0x8000:01, rwrwrw, uint16, 16 bit, "Current"\n')

    def test_the_slaves_entries_get_the_esis_ranges(self):
        mine = coe.Dictionary(self.TEXT)
        d = mine.enriched(esi.dictionary(device()))
        e = d.entries[(0x8000, 1)]
        self.assertEqual((e.minimum, e.maximum, e.default), (0, 8000, 5000))
        self.assertEqual(d.source, "slave and ESI")
        self.assertNotIn((0x9000, 0), d.entries)       # the slave's own list decides

    def test_without_entries_the_esi_is_used(self):
        d = coe.Dictionary().enriched(esi.dictionary(device()))
        self.assertEqual(d.source, "ESI")
        self.assertIn((0x9000, 0), d.entries)


class TestCheckSetting(unittest.TestCase):
    def setUp(self):
        self.d = esi.dictionary(device())
        os.environ["ECATSETUP_DEVICES"] = tempfile.mkdtemp()
        quirks.load(reload=True)
        self.addCleanup(quirks.load, True)
        self.slave = {"position": 0, "name": "s", "identity": {"vendor": "0x2", "product": "0x1234"}}

    def check(self, **st):
        st.setdefault("type", "UINT")
        st.setdefault("value", 100)
        return guidance.check_setting(self.slave, st, self.d)

    def test_a_fine_setting_has_no_problem(self):
        self.assertEqual(self.check(object="0x8000:01"), [])

    def test_an_object_that_is_not_there_is_a_warning(self):
        (level, text), = self.check(object="0x8123:00")
        self.assertEqual(level, "warning")
        self.assertIn("not in the object dictionary", text)

    def test_read_only_is_not_writable(self):
        found = self.check(object="0x8000:03", type="UDINT")
        self.assertTrue(any("not writable in PREOP" in t for _, t in found))

    def test_write_only_in_other_states_is_not_writable_in_preop(self):
        e = coe.CoeEntry(0x9000, 0, "r-rwrw", "uint8", 8, "Mode")
        d = coe.Dictionary()
        d.entries[(0x9000, 0)] = e
        found = guidance.check_setting(self.slave, {"object": "0x9000:00", "type": "USINT",
                                                    "value": 1}, d)
        self.assertEqual(found[0][0], "error")     # the slave's own list: certain

    def test_value_range_of_the_type_and_of_the_esi(self):
        found = self.check(object="0x8000:01", value=70000)
        self.assertTrue(any(lv == "error" and "does not fit UINT" in t for lv, t in found))
        found = self.check(object="0x8000:01", value=9000)
        self.assertEqual([lv for lv, _ in found], ["warning"])
        self.assertIn("0 to 8000", found[0][1])

    def test_another_type_than_the_dictionarys(self):
        found = self.check(object="0x8000:01", type="UDINT")
        self.assertTrue(any("the dictionary says UINT" in t for _, t in found))

    def test_a_quirk_command_is_an_error_and_a_name_a_suspicion(self):
        d = os.environ["ECATSETUP_DEVICES"]
        with open(os.path.join(d, "t.yaml"), "w") as f:
            f.write("name: t\nmatch: {vendor: '0x2', product: '0x1234'}\n"
                    "commands: {'0x9000': Mode is a command here}\n")
        quirks.load(reload=True)
        (level, text), = self.check(object="0x9000:00", type="USINT", value=1)
        self.assertEqual((level, text), ("error", "Mode is a command here"))
        found = self.check(object="0x8000:02", type="DINT", value=0)    # "Zero position"
        self.assertEqual(found[0][0], "warning")
        self.assertIn("sounds like a command", found[0][1])

    def test_store_parameters_is_suspected(self):
        self.assertEqual(guidance.command_reason(self.slave, 0x1010)[0], "warning")


class TestCommandQuirk(unittest.TestCase):
    def setUp(self):
        os.environ["ECATSETUP_DEVICES"] = self.dir = tempfile.mkdtemp()
        self.addCleanup(quirks.load, True)

    def write(self, text):
        with open(os.path.join(self.dir, "t.yaml"), "w") as f:
            f.write("name: t\nmatch: {vendor: '0x2', product: '0x1'}\n" + text)
        quirks.load(reload=True)

    slave = {"identity": {"vendor": "0x2", "product": "0x1"}}

    def test_the_plain_form_has_no_run_values(self):
        self.write("commands: {'0x2010': Zero position}\n")
        self.assertEqual(quirks.command(self.slave, 0x2010), "Zero position")
        self.assertIsNone(quirks.command_run(self.slave, 0x2010, 0))

    def test_the_rich_form_gives_what_run_command_writes(self):
        self.write("commands:\n  '0x1010:01': {note: Store, value: 1702257011, type: uint32, "
                   "confirm: Keeps all settings}\n")
        self.assertEqual(quirks.command(self.slave, 0x1010), "Store")
        self.assertEqual(quirks.command_run(self.slave, 0x1010, 1),
                         {"value": 1702257011, "type": "uint32", "confirm": "Keeps all settings"})
        self.assertEqual(quirks.problems, [])

    def test_a_command_without_a_note_is_a_problem(self):
        self.write("commands:\n  '0x1010': {value: 1}\n")
        self.assertTrue(quirks.problems)

    def test_drives_enabled_reads_the_statusword(self):
        from ecatsetup import testmode
        cfg = {"slaves": [{"position": 1, "name": "x", "profile": "cia402"},
                          {"position": 2, "name": "io", "profile": "generic"}]}
        words = {(1, 0x6041): 0x0237}
        up = lambda pos, i, sub, t: words.get((pos, i))   # noqa: E731
        self.assertEqual(testmode.drives_enabled(cfg, up), ["x"])
        words[(1, 0x6041)] = 0x0231                       # switched on, not enabled
        self.assertEqual(testmode.drives_enabled(cfg, up), [])


class TestSessionDictionary(unittest.TestCase):
    def test_settings_are_checked_against_the_esi_without_a_bus(self):
        s = Session([ESI])
        s.cfg = {"slaves": [{
            "position": 0, "name": "s",
            "identity": {"vendor": "0x00000002", "product": "0x00001234", "revision": "0x00010000"},
            "settings": [{"object": "0x8000:01", "type": "UINT", "value": 9000},
                         {"object": "0x8000:01", "type": "UINT", "value": 10}]}]}
        found = s.setting_problems()
        self.assertEqual(len(found), 1)
        self.assertIn("slave 0 (s): setting 0x8000:01", found[0][1])

    def test_config_check_refuses_a_quirk_command_as_a_setting(self):
        from ecatsetup import config, propose  # noqa: F401
        os.environ.pop("ECATSETUP_DEVICES", None)
        quirks.load(reload=True)
        cfg = config.from_proposals([], None, "raw", None)
        cfg["slaves"] = [{"position": 0, "name": "d", "profile": "generic",
                          "identity": {"vendor": "0x00000A88", "product": "0x0A880002"},
                          "settings": [{"object": "0x2010:00", "type": "UINT", "value": 1}]}]
        self.assertTrue(any("is a command, not a setting" in e for e in config.check(cfg)))


if __name__ == "__main__":
    unittest.main()
