"""pdoreq: what a profile needs in the process data, judged against an ESI file."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ecatsetup import esi, pdoreq


def entry(index, bits, name="x"):
    return ("<Entry><Index>#x%04X</Index><SubIndex>0</SubIndex><BitLen>%d</BitLen>"
            "<Name>%s</Name><DataType>UINT</DataType></Entry>" % (index, bits, name))


def pdo(kind, index, entries, fixed=False, sm=None, exclude=()):
    return ("<%s%s%s><Index>#x%04X</Index><Name>p</Name>%s%s</%s>"
            % (kind, ' Fixed="1"' if fixed else "", ' Sm="%d"' % sm if sm is not None else "",
               index, "".join("<Exclude>#x%04X</Exclude>" % e for e in exclude),
               "".join(entry(i, b) for i, b in entries), kind))


def obj(index, flag=None, default=None, typ="UINT"):
    return ("<Object><Index>#x%04X</Index><Name>o</Name><Type>%s</Type><BitSize>16</BitSize>"
            "%s<Flags><Access>rw</Access>%s</Flags></Object>"
            % (index, typ, "<Info><DefaultValue>#x%X</DefaultValue></Info>" % default if default is not None else "",
               "<PdoMapping>%s</PdoMapping>" % flag if flag else ""))


def make(objects, pdos, pdo_config=True, modes=0x180):
    xml = ('<?xml version="1.0"?><EtherCATInfo><Vendor><Id>1</Id></Vendor><Descriptions><Devices>'
           '<Device><Type ProductCode="#x1" RevisionNo="#x1">T</Type><Name>T</Name>'
           '<Profile><ProfileNo>402</ProfileNo><Dictionary><DataTypes/><Objects>%s</Objects></Dictionary></Profile>'
           '%s<Mailbox><CoE PdoAssign="1" PdoConfig="%d"/></Mailbox></Device>'
           '</Devices></Descriptions></EtherCATInfo>'
           % ("".join(objects) + obj(0x6502, None, modes, "UDINT"), "".join(pdos), int(pdo_config)))
    fd, path = tempfile.mkstemp(suffix=".xml")
    os.write(fd, xml.encode())
    os.close(fd)
    try:
        return esi.load_file(path)[0]
    finally:
        os.unlink(path)


MAPPABLE = [obj(0x6040, "R"), obj(0x6041, "T"), obj(0x607A, "R"), obj(0x6064, "T"),
            obj(0x60FF, "R"), obj(0x606C, "T"), obj(0x6060, "R"), obj(0x6061, "T")]


class TestRequirement(unittest.TestCase):
    def test_modes(self):
        r = pdoreq.cia402("csp")
        self.assertEqual(r.rx, {(0x6040, 0), (0x607A, 0)})
        self.assertEqual(r.tx, {(0x6041, 0), (0x6064, 0)})
        self.assertEqual(r.good_tx, {(0x603F, 0), (0x6061, 0)})

    def test_several_modes_need_the_mode_objects(self):
        r = pdoreq.cia402("csp", ["pv"])
        self.assertIn((0x6060, 0), r.rx)
        self.assertIn((0x6061, 0), r.tx)
        self.assertIn((0x60FF, 0), r.rx)
        self.assertNotIn((0x6061, 0), r.good_tx)

    def test_homing_alone_adds_nothing_but_the_mode_objects(self):
        r = pdoreq.cia402("csp", ["hm"])
        self.assertEqual(r.rx, {(0x6040, 0), (0x607A, 0), (0x6060, 0)})

    def test_axes_shift(self):
        r = pdoreq.cia402("csp", axes=2)
        self.assertIn((0x6040 + 0x800, 0), r.rx)
        self.assertIn((0x6064 + 0x800, 0), r.tx)


class TestJudge(unittest.TestCase):
    def test_unknown_without_esi(self):
        self.assertEqual(pdoreq.judge(pdoreq.cia402("csp"), None).status, pdoreq.UNKNOWN)

    def test_ready_by_default_assignment(self):
        dev = make(MAPPABLE, [pdo("RxPdo", 0x1600, [(0x6040, 16), (0x607A, 32)], sm=2),
                              pdo("TxPdo", 0x1A00, [(0x6041, 16), (0x6064, 32)], sm=3)])
        v = pdoreq.judge(pdoreq.cia402("csp"), dev)
        self.assertEqual(v.status, pdoreq.READY)
        self.assertEqual(v.ways[0].title, "the default assignment")
        self.assertEqual(v.ways[0].lacks_good, [(0x603F, 0), (0x6061, 0)])

    def test_ready_by_fixed_pdos_respecting_exclude(self):
        pdos = [pdo("RxPdo", 0x1701, [(0x6040, 16)], fixed=True, exclude=[0x1702]),
                pdo("RxPdo", 0x1702, [(0x607A, 32)], fixed=True, exclude=[0x1701]),
                pdo("RxPdo", 0x1703, [(0x6040, 16), (0x607A, 32)], fixed=True),
                pdo("TxPdo", 0x1B01, [(0x6041, 16), (0x6064, 32)], fixed=True)]
        dev = make(MAPPABLE, pdos, pdo_config=False)
        v = pdoreq.judge(pdoreq.cia402("csp"), dev)
        self.assertEqual(v.status, pdoreq.READY)
        self.assertEqual([(w.rx, w.tx) for w in v.ways], [([0x1703], [0x1B01])])

    def test_excluding_fixed_pdos_are_never_combined(self):
        pdos = [pdo("RxPdo", 0x1701, [(0x6040, 16)], fixed=True, exclude=[0x1702]),
                pdo("RxPdo", 0x1702, [(0x607A, 32)], fixed=True, exclude=[0x1701]),
                pdo("TxPdo", 0x1B01, [(0x6041, 16), (0x6064, 32)], fixed=True)]
        v = pdoreq.judge(pdoreq.cia402("csp"), make(MAPPABLE, pdos, pdo_config=False))
        self.assertEqual(v.status, pdoreq.IMPOSSIBLE)
        self.assertEqual(v.ways, [])

    def test_build_when_only_an_open_pdo_can_do_it(self):
        pdos = [pdo("RxPdo", 0x1600, [(0x6040, 16)]), pdo("TxPdo", 0x1A00, [(0x6041, 16)])]
        v = pdoreq.judge(pdoreq.cia402("csp"), make(MAPPABLE, pdos))
        self.assertEqual(v.status, pdoreq.BUILD)
        self.assertEqual(v.ways[0].kind, pdoreq.BUILD)
        self.assertEqual(v.ways[0].rx, [(0x6040, 0), (0x607A, 0)])

    def test_impossible_when_not_mappable(self):
        objs = [o for o in MAPPABLE if "x607A" not in o] + [obj(0x607A)]   # no PdoMapping
        pdos = [pdo("RxPdo", 0x1600, [(0x6040, 16)]), pdo("TxPdo", 0x1A00, [(0x6041, 16)])]
        v = pdoreq.judge(pdoreq.cia402("csp"), make(objs, pdos))
        self.assertEqual(v.status, pdoreq.IMPOSSIBLE)
        self.assertTrue(any("0x607A" in t for t in v.problems))

    def test_impossible_when_direction_is_wrong(self):
        objs = [o for o in MAPPABLE if "x607A" not in o] + [obj(0x607A, "T")]
        pdos = [pdo("RxPdo", 0x1600, [(0x6040, 16)]), pdo("TxPdo", 0x1A00, [(0x6041, 16)])]
        v = pdoreq.judge(pdoreq.cia402("csp"), make(objs, pdos))
        self.assertEqual(v.status, pdoreq.IMPOSSIBLE)
        self.assertTrue(any("cannot be mapped as RX" in t for t in v.problems))

    def test_impossible_when_pdos_cannot_be_changed_and_nothing_is_ready(self):
        pdos = [pdo("RxPdo", 0x1600, [(0x6040, 16)], sm=2), pdo("TxPdo", 0x1A00, [(0x6041, 16)], sm=3)]
        v = pdoreq.judge(pdoreq.cia402("csp"), make(MAPPABLE, pdos, pdo_config=False))
        self.assertEqual(v.status, pdoreq.IMPOSSIBLE)
        self.assertTrue(any("PdoConfig" in t for t in v.problems))

    def test_unknown_without_a_dictionary(self):
        dev = make([], [pdo("RxPdo", 0x1600, [(0x6040, 16)]), pdo("TxPdo", 0x1A00, [(0x6041, 16)])])
        self.assertEqual(pdoreq.judge(pdoreq.cia402("csp"), dev).status, pdoreq.UNKNOWN)

    def test_too_many_entries(self):
        objs = MAPPABLE + [obj(0x1600, None, None)]
        pdos = [pdo("RxPdo", 0x1600, [(0x6040, 16)]), pdo("TxPdo", 0x1A00, [(0x6041, 16)])]
        dev = make(objs, pdos)
        dev.max_entries[0x1600] = 1
        v = pdoreq.judge(pdoreq.cia402("csp"), dev)
        self.assertEqual(v.status, pdoreq.IMPOSSIBLE)
        self.assertTrue(any("takes 1 entries" in t for t in v.problems))

    def test_drive_without_the_mode(self):
        pdos = [pdo("RxPdo", 0x1600, [(0x6040, 16)]), pdo("TxPdo", 0x1A00, [(0x6041, 16)])]
        whole, each = pdoreq.cia402_verdicts(make(MAPPABLE, pdos, modes=0x100), "csp")
        self.assertEqual(whole.status, pdoreq.IMPOSSIBLE)     # 0x100 = csv only
        self.assertIn("CSP in 0x6502", whole.problems[0])
        self.assertEqual(each["csv"].status, pdoreq.BUILD)
        self.assertEqual(each["csp"].status, pdoreq.IMPOSSIBLE)

    def test_supported_from_the_slave_wins_over_the_esi(self):
        pdos = [pdo("RxPdo", 0x1600, [(0x6040, 16)]), pdo("TxPdo", 0x1A00, [(0x6041, 16)])]
        whole, _ = pdoreq.cia402_verdicts(make(MAPPABLE, pdos, modes=0x100), "csp", supported=0x80)
        self.assertEqual(whole.status, pdoreq.BUILD)

    def test_mapping_facts_read_from_the_esi(self):
        dev = make(MAPPABLE, [])
        self.assertEqual(dev.mapping[0x6040], "R")
        self.assertEqual(dev.drive_modes, 0x180)
        self.assertTrue(dev.pdo_config)


@unittest.skipUnless(os.path.exists(os.path.expanduser("~/esi/stepperonline")), "no A6 ESI file")
class TestA6(unittest.TestCase):
    def setUp(self):
        folder = os.path.expanduser("~/esi/stepperonline")
        files = [os.path.join(folder, f) for f in os.listdir(folder) if f.endswith(".xml")]
        self.dev = esi.load_file(files[0])[0]

    def test_csp_is_ready_with_the_default_assignment(self):
        v, _ = pdoreq.cia402_verdicts(self.dev, "csp")
        self.assertEqual(v.status, pdoreq.READY)
        default = next(w for w in v.ways if w.title == "the default assignment")
        self.assertEqual((default.rx, default.tx), ([0x1701], [0x1B01]))

    def test_several_modes_need_fixed_pdos_with_the_mode_objects(self):
        v, _ = pdoreq.cia402_verdicts(self.dev, "csp", ["hm"])
        self.assertEqual({tuple(w.rx) for w in v.ways if w.kind == "ready"},
                         {(0x1702,), (0x1703,)})
        self.assertEqual(v.ways[-1].kind, pdoreq.BUILD)


if __name__ == "__main__":
    unittest.main()
