"""ESI InitCmds as settings, and the cia402 mode as a setting where 0x6060 is not mapped."""

import os
import sys
import tempfile
import unittest
from unittest import mock

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
        self.rows = {r["object"]: r for r in self.found.cmds}

    def test_fixed_ones_with_their_types_and_transitions(self):
        self.assertEqual(self.rows["0x2001:00"], {"at": "PS", "object": "0x2001:00", "name": "Brake delay",
                                                 "type": "INT", "value": -10})
        self.assertEqual(self.rows["0x8000:06"], {"at": "PS", "object": "0x8000:06", "type": "USINT", "value": 1})

    def test_complete_access_and_the_cycle_time(self):
        self.assertEqual(self.rows["0x8090:01"], {"at": "PS", "object": "0x8090:01", "complete": True,
                                                 "data": "00 00 00 03 03 00"})
        self.assertEqual(self.rows["0x1C32:02"], {"at": "PS", "object": "0x1C32:02", "from": "cycle-time"})
        self.assertTrue(any("complete_sdo_from" in n for n in self.found.notes))

    def test_tool_functions_and_other_transitions_are_skipped(self):
        skipped = dict(self.found.skipped)
        self.assertIn("revision", skipped["0xF081:01"])
        self.assertIn("0x10F3:05", skipped)
        self.assertIn("PI", skipped["0x2002:00"])
        self.assertNotIn("0xF081:01", self.rows)

    def test_the_profiles_object(self):
        self.assertEqual(self.rows["0x6060:00"], {"at": "PS", "object": "0x6060:00", "from": "profile"})
        generic = {r["object"]: r for r in initcmds.from_esi(self.dev, esi.dictionary(self.dev), None).cmds}
        # this dictionary says 16 bits, the InitCmd has one byte: the byte as it is
        self.assertEqual(generic["0x6060:00"], {"at": "PS", "object": "0x6060:00", "name": "o", "data": "08"})

    def test_raw_bytes_where_the_type_is_not_known(self):
        self.assertEqual(initcmds.value_of(0x8001, 1, "010203"), {"data": "01 02 03"})


class TestProposal(unittest.TestCase):
    def rows(self, p):
        return config.slave_from_proposal(p).get("init-cmds") or []

    def test_init_cmds_apart_from_the_settings(self):
        p = proposal(device(CMDS, NO_MODE), mode="pv")
        s = config.slave_from_proposal(p)
        self.assertEqual(s["settings"], [])
        self.assertEqual([r["object"] for r in s["init-cmds"]],
                         ["0x6060:00", "0x2001:00", "0x8000:06", "0x1C32:02", "0x8090:01"])
        self.assertTrue(any("the mode (3, PV) is written at every start" in n for n in p.notes))

    def test_no_mode_row_where_0x6060_is_mapped(self):
        self.assertFalse(any(r["object"] == "0x6060:00" for r in self.rows(proposal(device(CMDS, WITH_MODE)))))

    def test_a_mode_row_also_for_a_drive_with_one_mode(self):
        # it may start in mode 0; the check wants the mode set or seen (the A6 simulation: CSP only)
        self.assertTrue(any(r["object"] == "0x6060:00" for r in self.rows(proposal(device(CMDS, NO_MODE, modes=0x80)))))
        self.assertTrue(any(r["object"] == "0x6060:00"
                            for r in self.rows(proposal(device(CMDS, NO_MODE), drive_modes=0x80))))
        cfg = config.from_proposals([proposal(device(CMDS, NO_MODE), drive_modes=0x80)])
        self.assertFalse(any("neither set nor seen" in e for e in config.check(cfg)))

    def test_a_mode_row_without_an_initcmd(self):
        self.assertEqual(self.rows(proposal(device([], NO_MODE))),
                         [{"at": "PS", "object": "0x6060:00", "from": "profile"}])

    def test_a_proposed_file_passes_the_check(self):
        for pdos in (NO_MODE, WITH_MODE):
            cfg = config.from_proposals([proposal(device(CMDS, pdos))])
            self.assertEqual(config.check(cfg), [])

    def test_check(self):
        cfg = config.from_proposals([proposal(device(CMDS, NO_MODE))])
        rows = cfg["slaves"][0]["init-cmds"]
        rows.append({"at": "SO", "object": "0x8000:07", "type": "USINT", "value": 1})
        rows.append({"at": "PS", "object": "0x8000:08", "from": "nothing"})
        rows.append({"at": "PS", "object": "0x8000:09", "complete": True, "data": "01"})
        rows[-1]["object"] = "0x8000:02"
        found = config.check(cfg)
        self.assertTrue(any("at SO is not supported yet" in e for e in found))
        self.assertTrue(any("from must be profile or cycle-time" in e for e in found))
        self.assertTrue(any("complete access starts at subindex 0 or 1" in e for e in found))
        rows[-3]["disabled"] = True                    # a row turned off is not checked further
        self.assertFalse(any("at SO" in e for e in config.check(cfg)))

    def test_the_mode_row_counts_as_set(self):
        cfg = config.from_proposals([proposal(device([], NO_MODE))])
        self.assertEqual(config.check(cfg), [])
        cfg["slaves"][0]["init-cmds"][0]["disabled"] = True
        self.assertTrue(any("neither set nor seen" in e for e in config.check(cfg)))

    def test_no_initcmds_only_the_mode(self):
        self.assertEqual(proposal(device([], WITH_MODE)).init_cmds, [])


TIMEOUTS = ("<Info><StateMachine><Timeout><PreopTimeout>6000</PreopTimeout><SafeopOpTimeout>9000"
            "</SafeopOpTimeout><BackToInitTimeout>5000</BackToInitTimeout><BackToSafeopTimeout>1000"
            "</BackToSafeopTimeout></Timeout></StateMachine><Mailbox><Timeout><RequestTimeout>100"
            "</RequestTimeout><ResponseTimeout>2000</ResponseTimeout></Timeout></Mailbox></Info>")


class TestStatusText(unittest.TestCase):
    def test_the_line_names_the_initcmd(self):
        from ecatsetup import pdoreq
        dev = device(CMDS, NO_MODE)
        self.assertEqual(pdoreq.initcmd_mode(dev), 8)
        self.assertIsNone(pdoreq.initcmd_mode(device([], NO_MODE)))
        have = {(0x6040, 0), (0x607A, 0), (0x60FF, 0), (0x6041, 0), (0x6064, 0), (0x606C, 0)}
        st = pdoreq.cia402_status("pv", [], 1, have, esi_mode=8)
        self.assertEqual(st[0].level, "error")
        self.assertIn("its ESI file writes 0x6060 = 8 at start (an InitCmd)", st[0].text)
        self.assertIn("adds it, as 0x6060:00 = 3 for PV", st[0].text)
        self.assertIn("add the setting 0x6060:00 = 3", pdoreq.cia402_status("pv", [], 1, have)[0].text)


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
    """An existing file gets the device's init-cmds only when the user adds them."""

    def setUp(self):
        from ecatsetup import session
        self.dev = device(CMDS, NO_MODE, info=TIMEOUTS)
        self.p = proposal(self.dev)
        self.sess = session.Session([])
        info = {"position": 1, "vendor_id": 1, "product_code": 1, "revision": 1}
        self.sess.bus = {1: session.BusSlave(info, coe.Dictionary(), self.p)}
        self.sess.cfg = config.from_proposals([self.p])
        self.slave = self.sess.cfg["slaves"][0]
        del self.slave["init-cmds"]          # a file made before InitCmds were read
        del self.slave["state-timeouts-ms"]
        del self.slave["sdo-timeout-ms"]

    def test_missing_rows_are_named(self):
        self.assertEqual([r["object"] for r in self.sess.missing_init_cmds(1)],
                         ["0x6060:00", "0x2001:00", "0x8000:06", "0x1C32:02", "0x8090:01"])

    def test_no_mode_row_where_the_file_maps_0x6060_or_sets_it(self):
        self.slave["process-data"]["outputs"][0]["entries"].append(
            {"object": "0x6060:00", "bits": 8, "type": "SINT", "use": "profile"})
        self.assertNotIn("0x6060:00", [r["object"] for r in self.sess.missing_init_cmds(1)])
        self.slave["process-data"]["outputs"][0]["entries"].pop()
        self.slave["settings"] = [{"object": "0x6060:00", "type": "SINT", "value": 8}]
        self.assertNotIn("0x6060:00", [r["object"] for r in self.sess.missing_init_cmds(1)])

    def test_the_mode_row_follows_the_file_as_it_is_now(self):
        # read with one set of modes and PDOs, changed in the window since: the file decides
        self.p.init_cmds = [r for r in self.p.init_cmds if r.get("from") != "profile"]
        self.assertIn("0x6060:00", [r["object"] for r in self.sess.missing_init_cmds(1)])
        self.slave["cia402"]["modes"] = ["hm"]          # 0x6060 is then set every cycle
        self.assertNotIn("0x6060:00", [r["object"] for r in self.sess.missing_init_cmds(1)])
        del self.slave["cia402"]["modes"]
        self.slave["init-cmds"] = [{"at": "PS", "object": "0x6060:00", "from": "profile", "disabled": True}]
        self.assertNotIn("0x6060:00", [r["object"] for r in self.sess.missing_init_cmds(1)])

    def test_every_command_is_shown_with_what_happens_to_it(self):
        with mock.patch.object(self.sess, "esi_device", return_value=self.dev):
            view = {v["row"]["object"]: v for v in self.sess.init_cmd_view(1)}
        self.assertEqual(view["0x6060:00"]["state"], "needed")
        self.assertEqual(view["0x2001:00"]["state"], "offered")
        self.assertEqual(view["0xF081:01"]["state"], "skipped")
        self.assertIn("not written by ecat", view["0x10F3:05"]["text"])
        self.assertEqual(view["0x2002:00"]["state"], "skipped")         # at PI
        # 0x6060 mapped: the ESI's mode command is shown as not needed
        self.slave["process-data"]["outputs"][0]["entries"].append(
            {"object": "0x6060:00", "bits": 8, "type": "SINT", "use": "profile"})
        self.slave["init-cmds"] = [{"at": "PS", "object": "0x2001:00", "type": "INT", "value": -10,
                                    "disabled": True}]
        with mock.patch.object(self.sess, "esi_device", return_value=self.dev):
            view = {v["row"]["object"]: v for v in self.sess.init_cmd_view(1)}
        self.assertEqual(view["0x6060:00"]["state"], "not-needed")
        self.assertIn("in the PDOs", view["0x6060:00"]["text"])
        self.assertEqual((view["0x2001:00"]["state"], view["0x2001:00"]["file_index"]), ("off", 0))
        # a mode row the file has while 0x6060 is mapped: written, and said to be not needed now
        self.slave["init-cmds"].append({"at": "PS", "object": "0x6060:00", "from": "profile"})
        with mock.patch.object(self.sess, "esi_device", return_value=self.dev):
            view = {v["row"]["object"]: v for v in self.sess.init_cmd_view(1)}
        self.assertEqual(view["0x6060:00"]["state"], "in-file")
        self.assertIn("not needed now", view["0x6060:00"]["text"])

    def test_a_row_turned_off_is_not_brought_back(self):
        self.slave["init-cmds"] = [{"at": "PS", "object": "0x8000:06", "type": "USINT", "value": 1, "disabled": True}]
        self.assertNotIn("0x8000:06", [r["object"] for r in self.sess.missing_init_cmds(1)])

    def test_update_only_recommends(self):
        notes = self.sess.update_from_bus()
        self.assertNotIn("init-cmds", self.sess.slave(1))
        self.assertTrue(any("writes at start what the file does not have yet" in n and "0x2001:00 = -10" in n
                            and "0x1C32:02 = the cycle time" in n for n in notes), notes)

    def test_timeouts_recommended_and_added(self):
        self.assertEqual(self.sess.missing_state_timeouts(1), {"preop": 6000, "back-to-safeop": 1000, "sdo": 2000})
        notes = self.sess.update_from_bus()
        self.assertNotIn("state-timeouts-ms", self.sess.slave(1))
        self.assertTrue(any("recommended timeouts" in n and "preop 6000 ms" in n for n in notes))

    def test_added_when_asked(self):
        added = self.sess.add_device_needs(1)
        self.assertEqual(len(added), 8)
        self.assertEqual(len(self.sess.slave(1)["init-cmds"]), 5)
        self.assertEqual(self.sess.slave(1)["state-timeouts-ms"], {"preop": 6000, "back-to-safeop": 1000})
        self.assertEqual(self.sess.slave(1)["sdo-timeout-ms"], 2000)
        self.assertEqual(self.sess.missing_state_timeouts(1), {})
        self.assertEqual(self.sess.missing_init_cmds(1), [])
        self.assertEqual(config.check(self.sess.cfg), [])


if __name__ == "__main__":
    unittest.main()
