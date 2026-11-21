"""ESI InitCmds as settings, and the cia402 mode as a setting where 0x6060 is not mapped."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ecatsetup import coe, config, esi, initcmds, propose, sii  # noqa: E402
from test_pdoreq import MAPPABLE, pdo  # noqa: E402


_DIR = tempfile.TemporaryDirectory()


def init(transition, index, sub, data, complete=False):
    return ("<InitCmd%s><Transition>%s</Transition><Index>#x%04X</Index><SubIndex>%d</SubIndex>"
            "<Data>%s</Data></InitCmd>" % (' CompleteAccess="1"' if complete else "", transition, index, sub, data))


def device(cmds, pdos, profile=402, modes=0x180, objects=MAPPABLE, info=""):
    xml = ('<?xml version="1.0"?><EtherCATInfo><Vendor><Id>1</Id></Vendor><Descriptions><Devices>'
           '<Device><Type ProductCode="#x1" RevisionNo="#x1">T</Type><Name>T</Name>' + info +
           '<Profile><ProfileNo>%d</ProfileNo><Dictionary><DataTypes/><Objects>%s'
           '<Object><Index>#x6502</Index><Name>m</Name><Type>UDINT</Type><BitSize>32</BitSize>'
           '<Info><DefaultValue>#x%X</DefaultValue></Info><Flags><Access>ro</Access></Flags></Object>'
           '<Object><Index>#x2001</Index><Name>Brake delay</Name><Type>INT</Type><BitSize>16</BitSize>'
           '<Flags><Access>rw</Access></Flags></Object>'
           '</Objects></Dictionary></Profile>%s<Mailbox><CoE PdoAssign="1" PdoConfig="1">%s</CoE></Mailbox>'
           '</Device></Devices></Descriptions></EtherCATInfo>'
           % (profile, "".join(objects), modes, "".join(pdos), "".join(cmds)))
    # the file stays while the tests run: the dictionary is read from it when it is needed
    fd, path = tempfile.mkstemp(suffix=".xml", dir=_DIR.name)
    os.write(fd, xml.encode())
    os.close(fd)
    return esi.load_file(path)[0]


CMDS = [init("PS", 0x6060, 0, "08"), init("PS", 0x2001, 0, "f6ff"), init("PS", 0x8000, 6, "01"),
        init("PS", 0x1C32, 2, "00000000"), init("IP", 0xF081, 1, "00001000"), init("IP", 0x10F3, 5, "0000"),
        init("PS", 0x8090, 1, "000000030300", complete=True), init("PI", 0x2002, 0, "01")]
NO_MODE = [pdo("RxPdo", 0x1600, [(0x6040, 16), (0x607A, 32)], sm=2),
           pdo("TxPdo", 0x1A00, [(0x6041, 16), (0x6064, 32)], sm=3)]
WITH_MODE = [pdo("RxPdo", 0x1600, [(0x6040, 16), (0x6060, 8), (0x607A, 32)], sm=2),
             pdo("TxPdo", 0x1A00, [(0x6041, 16), (0x6061, 8), (0x6064, 32)], sm=3)]


def proposal(dev, mode="csp", **kw):
    s = sii.Sii(vendor_id=1, product_code=1, revision=1, serial=0, mailbox_protocols=0x04, name="T")
    return propose.propose({"position": 1, "dc": False}, s, coe.Dictionary(), device_type=0x20192,
                           drive_modes=kw.pop("drive_modes", None), esi_dev=dev, esi_exact=True, mode=mode, **kw)


class TestSorting(unittest.TestCase):
    def setUp(self):
        self.dev = device(CMDS, NO_MODE)
        self.found = initcmds.from_esi(self.dev, esi.dictionary(self.dev), "cia402")

    def test_fixed_settings_are_taken_with_their_types(self):
        self.assertEqual(self.found.settings, [
            {"object": "0x2001:00", "name": "Brake delay", "type": "INT", "value": -10},
            {"object": "0x8000:06", "type": "USINT", "value": 1}])

    def test_templates_and_tool_functions_are_skipped(self):
        skipped = dict(self.found.skipped)
        self.assertIn("cycle time", skipped["0x1C32:02"])
        self.assertIn("revision", skipped["0xF081:01"])
        self.assertIn("0x10F3:05", skipped)
        self.assertIn("complete access", skipped["0x8090:01"])
        self.assertIn("PI", skipped["0x2002:00"])
        self.assertTrue(any("0x8090:01" in n and "complete access" in n for n in self.found.notes))

    def test_the_profiles_object_is_not_a_setting(self):
        self.assertEqual(self.found.owned, [(0x6060, 0, 8)])
        generic = initcmds.from_esi(self.dev, esi.dictionary(self.dev), None)
        # this dictionary says 16 bits, the InitCmd has one byte: the byte as it is
        self.assertIn({"object": "0x6060:00", "name": "o", "data": "08"}, generic.settings)

    def test_raw_bytes_where_the_type_is_not_known(self):
        self.assertEqual(initcmds.setting(0x8001, 1, "010203"), {"object": "0x8001:01", "data": "01 02 03"})


class TestProposal(unittest.TestCase):
    def settings(self, p):
        return config.slave_from_proposal(p)["settings"]

    def test_the_mode_is_a_setting_where_0x6060_is_not_mapped(self):
        p = proposal(device(CMDS, NO_MODE), mode="pv")
        st = self.settings(p)
        self.assertIn({"object": "0x6060:00", "name": "Modes of operation", "type": "SINT", "value": 3}, st)
        self.assertIn({"object": "0x2001:00", "name": "Brake delay", "type": "INT", "value": -10}, st)
        self.assertTrue(any("the mode is the setting 0x6060:00 = 3 (PV)" in n for n in p.notes))

    def test_no_mode_setting_where_0x6060_is_mapped(self):
        st = self.settings(proposal(device(CMDS, WITH_MODE)))
        self.assertFalse(any(x["object"] == "0x6060:00" for x in st))

    def test_no_mode_setting_for_a_drive_with_one_mode(self):
        st = self.settings(proposal(device(CMDS, NO_MODE, modes=0x80)))
        self.assertFalse(any(x["object"] == "0x6060:00" for x in st))
        st = self.settings(proposal(device(CMDS, NO_MODE), drive_modes=0x80))    # read from the drive
        self.assertFalse(any(x["object"] == "0x6060:00" for x in st))

    def test_a_proposed_file_passes_the_check(self):
        for pdos in (NO_MODE, WITH_MODE):
            cfg = config.from_proposals([proposal(device(CMDS, pdos))])
            self.assertEqual(config.check(cfg), [])

    def test_no_initcmds_no_settings(self):
        p = proposal(device([], WITH_MODE))
        self.assertEqual(p.init_settings, [])


TIMEOUTS = ("<Info><StateMachine><Timeout><PreopTimeout>6000</PreopTimeout><SafeopOpTimeout>9000"
            "</SafeopOpTimeout><BackToInitTimeout>5000</BackToInitTimeout><BackToSafeopTimeout>1000"
            "</BackToSafeopTimeout></Timeout></StateMachine><Mailbox><Timeout><RequestTimeout>100"
            "</RequestTimeout><ResponseTimeout>2000</ResponseTimeout></Timeout></Mailbox></Info>")


class TestStateTimeouts(unittest.TestCase):
    def test_read_from_the_esi(self):
        dev = device([], WITH_MODE, info=TIMEOUTS)
        self.assertEqual(dev.state_timeouts, {"preop": 6000, "safeop-op": 9000, "back-to-init": 5000,
                                              "back-to-safeop": 1000})

    def test_only_the_longer_ones_are_proposed(self):
        p = proposal(device([], WITH_MODE, info=TIMEOUTS))
        self.assertEqual(config.slave_from_proposal(p)["state-timeouts-ms"], {"preop": 6000, "back-to-safeop": 1000})
        self.assertNotIn("state-timeouts-ms", config.slave_from_proposal(proposal(device([], WITH_MODE))))

    def test_sdo_timeout(self):
        dev = device([], WITH_MODE, info=TIMEOUTS)
        self.assertEqual(dev.sdo_timeout, 2000)
        s = config.slave_from_proposal(proposal(dev))
        self.assertEqual(s["sdo-timeout-ms"], 2000)
        short = device([], WITH_MODE, info=TIMEOUTS.replace(">2000<", ">800<"))
        self.assertNotIn("sdo-timeout-ms", config.slave_from_proposal(proposal(short)))
        cfg = config.from_proposals([proposal(dev)])
        cfg["slaves"][0]["sdo-timeout-ms"] = 0
        self.assertTrue(any("sdo-timeout-ms must be 1 to 600000" in e for e in config.check(cfg)))

    def test_check(self):
        cfg = config.from_proposals([proposal(device([], WITH_MODE, info=TIMEOUTS))])
        self.assertEqual(config.check(cfg), [])
        st = cfg["slaves"][0]["state-timeouts-ms"]
        st["preop"] = 0
        st["boot"] = 100
        found = config.check(cfg)
        self.assertTrue(any("state-timeouts-ms.preop must be 1 to 600000" in e for e in found))
        self.assertTrue(any("unknown key 'boot'" in e for e in found))


class TestRecommendedOnUpdate(unittest.TestCase):
    """An existing file gets the device's settings only when the user adds them."""

    def setUp(self):
        from ecatsetup import session
        self.p = proposal(device(CMDS, NO_MODE, info=TIMEOUTS))
        self.sess = session.Session([])
        info = {"position": 1, "vendor_id": 1, "product_code": 1, "revision": 1}
        self.sess.bus = {1: session.BusSlave(info, coe.Dictionary(), self.p)}
        self.sess.cfg = config.from_proposals([self.p])
        self.slave = self.sess.cfg["slaves"][0]
        self.slave["settings"] = []          # a file made before InitCmds were read
        del self.slave["state-timeouts-ms"]
        del self.slave["sdo-timeout-ms"]

    def test_missing_settings_are_named(self):
        missing = self.sess.missing_init_settings(1)
        self.assertEqual([st["object"] for st in missing], ["0x2001:00", "0x8000:06", "0x6060:00"])

    def test_the_mode_follows_the_files_mode(self):
        self.slave["cia402"]["mode"] = "pv"
        self.assertIn({"object": "0x6060:00", "name": "Modes of operation", "type": "SINT", "value": 3},
                      self.sess.missing_init_settings(1))

    def test_no_mode_setting_where_the_file_maps_0x6060(self):
        self.slave["process-data"]["outputs"][0]["entries"].append(
            {"object": "0x6060:00", "bits": 8, "type": "SINT", "use": "profile"})
        self.assertNotIn("0x6060:00", [st["object"] for st in self.sess.missing_init_settings(1)])

    def test_update_only_recommends(self):
        notes = self.sess.update_from_bus()
        self.assertEqual(self.sess.slave(1)["settings"], [])
        self.assertTrue(any("recommended settings" in n and "0x2001:00 = -10" in n for n in notes), notes)

    def test_timeouts_recommended_and_added(self):
        self.assertEqual(self.sess.missing_state_timeouts(1), {"preop": 6000, "back-to-safeop": 1000, "sdo": 2000})
        notes = self.sess.update_from_bus()
        self.assertNotIn("state-timeouts-ms", self.sess.slave(1))
        self.assertTrue(any("recommended timeouts" in n and "preop 6000 ms" in n for n in notes))

    def test_added_when_asked(self):
        added = self.sess.add_init_settings(1)
        self.assertEqual(len(added), 6)
        self.assertEqual(self.sess.slave(1)["state-timeouts-ms"], {"preop": 6000, "back-to-safeop": 1000})
        self.assertEqual(self.sess.slave(1)["sdo-timeout-ms"], 2000)
        self.assertEqual(self.sess.missing_state_timeouts(1), {})
        self.assertEqual(self.sess.missing_init_settings(1), [])
        self.assertEqual(config.check(self.sess.cfg), [])


if __name__ == "__main__":
    unittest.main()
