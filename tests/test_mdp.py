"""MDP (ETG.5001) encoder channels: requirement per use, recognition by the ESI's sub-profile 511,
the predefined set that has a channel, and the notes on what a mapping does not give."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ecatsetup import coe, config, esi, pdoreq, propose, sii  # noqa: E402


def entry(index, sub, bits, name, dtype="UINT"):
    return ("<Entry><Index>#x%04X</Index><SubIndex>%d</SubIndex><BitLen>%d</BitLen><Name>%s</Name>"
            "<DataType>%s</DataType></Entry>" % (index, sub, bits, name, dtype))


def pdo(kind, index, entries, sm=None):
    return '<%s Fixed="1"%s><Index>#x%04X</Index><Name>p</Name>%s</%s>' % (
        kind, ' Sm="%d"' % sm if sm is not None else "", index, "".join(entries), kind)


LEGACY_IN = pdo("TxPdo", 0x1A00, [entry(0x6000, 1, 1, "Latch C valid", "BOOL"),
                                  entry(0x6000, 2, 16, "Value")])
LEGACY_OUT = pdo("RxPdo", 0x1600, [entry(0x7000, 1, 1, "Enable latch C", "BOOL")])
POSITION_IN = pdo("TxPdo", 0x1A03, [entry(0x6010, 0x11, 16, "Counter value")])
FULL_IN = pdo("TxPdo", 0x1A04, [entry(0x6010, 1, 1, "Latch C valid", "BOOL"),
                                entry(0x6010, 2, 1, "Latch extern valid", "BOOL"),
                                entry(0x6010, 0x11, 32, "Counter value", "UDINT"),
                                entry(0x6010, 0x12, 32, "Latch value", "UDINT")])
FULL_OUT = pdo("RxPdo", 0x1603, [entry(0x7010, 1, 1, "Enable latch C", "BOOL"),
                                 entry(0x7010, 2, 1, "Enable latch extern on positive edge", "BOOL")])
SETS = {"Legacy (MDP 510)": ({2: [0x1600], 3: [0x1A00]}, True),
        "Position only (MDP 511)": ({3: [0x1A03]}, False),
        "Full (MDP 511)": ({2: [0x1603], 3: [0x1A04]}, False)}


def device(channels=((5001, 510), (5001, 511)), default="Legacy (MDP 510)"):
    sets = "".join('<TwinCAT><AlternativeSmMapping%s><Name>%s</Name>%s</AlternativeSmMapping></TwinCAT>'
                   % (' Default="1"' if name == default else "", name,
                      "".join('<Sm No="%d">%s</Sm>' % (sm, "".join("<Pdo>#x%04X</Pdo>" % i for i in ids))
                              for sm, ids in sms.items()))
                   for name, (sms, _d) in SETS.items())
    chan = "".join("<ChannelInfo><ProfileNo>%d</ProfileNo><AddInfo>%d</AddInfo></ChannelInfo>" % c
                   for c in channels)
    xml = ('<?xml version="1.0"?><EtherCATInfo><Vendor><Id>2</Id></Vendor><Descriptions><Devices>'
           '<Device><Type ProductCode="#x1" RevisionNo="#x1">T</Type><Name>EL5101</Name>'
           '<Profile>%s<Dictionary><DataTypes/><Objects/></Dictionary></Profile>%s%s</Device>'
           '</Devices></Descriptions></EtherCATInfo>'
           % (chan, LEGACY_OUT + FULL_OUT + LEGACY_IN + POSITION_IN + FULL_IN,
              "<Info><VendorSpecific>%s</VendorSpecific></Info>" % sets))
    fd, path = tempfile.mkstemp(suffix=".xml")
    os.write(fd, xml.encode())
    os.close(fd)
    try:
        return esi.load_file(path)[0]
    finally:
        os.unlink(path)


def proposal(dev, **kw):
    s = sii.Sii(vendor_id=2, product_code=1, revision=1, serial=0, mailbox_protocols=0x04, name="EL5101")
    return propose.propose({"position": 1, "dc": False}, s, coe.Dictionary(), device_type=5001,
                           esi_dev=dev, esi_exact=True, **kw)


class TestEsi(unittest.TestCase):
    def test_sub_profiles_are_read(self):
        d = device()
        self.assertEqual(d.channels, [(5001, 510), (5001, 511)])
        self.assertEqual(d.sub_profiles, {510, 511})
        self.assertEqual(device(channels=()).sub_profiles, set())


class TestRequirement(unittest.TestCase):
    def test_per_use(self):
        r = pdoreq.mdp_encoder(0x6010, ["position"])
        self.assertEqual((r.rx, r.tx), (set(), {(0x6010, 0x11)}))
        r = pdoreq.mdp_encoder(0x6010, ["position", "index"])
        self.assertEqual(r.tx, {(0x6010, 0x11), (0x6010, 1), (0x6010, 0x12)})
        self.assertEqual(r.rx, {(0x7010, 1)})
        r = pdoreq.mdp_encoder(0x6020, ["latch"])
        self.assertEqual((r.rx, r.tx), ({(0x7020, 2)}, {(0x6020, 2), (0x6020, 0x12)}))


class TestRecognition(unittest.TestCase):
    def test_a_channel_without_latch_c_needs_the_sub_profile(self):
        self.assertEqual(proposal(device(), pdo_set="Position only (MDP 511)").encoders, [("enc", 0x6010)])
        self.assertEqual(proposal(device(channels=()), pdo_set="Position only (MDP 511)").encoders, [])

    def test_the_default_without_a_channel_is_replaced_by_a_set_that_has_one(self):
        p = proposal(device())
        self.assertEqual(p.encoders, [("enc", 0x6010)])
        self.assertEqual(p.pdo_way, "ready")
        self.assertIn("Position only", p.pdo_source)
        self.assertTrue(any("no encoder channel" in n for n in p.notes))

    def test_an_assignment_asked_for_is_kept(self):
        p = proposal(device(), pdo_set="Legacy (MDP 510)")
        self.assertEqual((p.encoders, p.pdo_way), ([], ""))

    def test_nothing_changes_when_the_default_has_a_channel(self):
        p = proposal(device(default="Full (MDP 511)"))
        self.assertEqual(p.pdo_way, "")
        self.assertIn("Full", p.pdo_source)

    def test_no_sub_profile_no_replacement(self):
        p = proposal(device(channels=()))
        self.assertEqual((p.encoders, p.pdo_way), ([], ""))


def slave_of(p):
    s = config.slave_from_proposal(p)
    return s


class TestNotes(unittest.TestCase):
    def test_what_a_mapping_does_not_give(self):
        dev = device()
        s = slave_of(proposal(dev, pdo_set="Position only (MDP 511)"))
        notes = [t for lv, t in pdoreq.check_slave(s, dev) if lv == "note"]
        self.assertEqual(len(notes), 2)
        self.assertIn("encoder enc: no index (index-enable): 0x6010:01, 0x6010:12, 0x7010:01 not mapped", notes[0])
        self.assertIn("the device's predefined set \"Full (MDP 511)\" has it", notes[0])
        self.assertIn("no external latch", notes[1])

    def test_a_full_mapping_has_no_notes(self):
        dev = device()
        s = slave_of(proposal(dev, pdo_set="Full (MDP 511)"))
        self.assertEqual([x for x in pdoreq.check_slave(s, dev) if x[0] == "note"], [])

    def test_the_file_is_still_without_errors(self):
        dev = device()
        s = slave_of(proposal(dev, pdo_set="Full (MDP 511)"))
        self.assertEqual([x for x in pdoreq.check_slave(s, dev) if x[0] != "note"], [])


if __name__ == "__main__":
    unittest.main()
