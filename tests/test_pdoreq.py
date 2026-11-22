"""pdoreq: what a profile needs in the process data, judged against an ESI file."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ecatsetup import coe, config, esi, pdoreq, propose, sii


def entry(index, bits, name="x"):
    return ("<Entry><Index>#x%04X</Index><SubIndex>0</SubIndex><BitLen>%d</BitLen>"
            "<Name>%s</Name><DataType>%s</DataType></Entry>"
            % (index, bits, name, {8: "SINT", 32: "DINT"}.get(bits, "UINT")))


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


def make(objects, pdos, pdo_config=True, modes=0x180, pdo_assign=True):
    xml = ('<?xml version="1.0"?><EtherCATInfo><Vendor><Id>1</Id></Vendor><Descriptions><Devices>'
           '<Device><Type ProductCode="#x1" RevisionNo="#x1">T</Type><Name>T</Name>'
           '<Profile><ProfileNo>402</ProfileNo><Dictionary><DataTypes/><Objects>%s</Objects></Dictionary></Profile>'
           '%s<Mailbox><CoE PdoAssign="%d" PdoConfig="%d"/></Mailbox></Device>'
           '</Devices></Descriptions></EtherCATInfo>'
           % ("".join(objects) + obj(0x6502, None, modes, "UDINT"), "".join(pdos), int(pdo_assign), int(pdo_config)))
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

    def test_max_value_is_decimal_unless_hex(self):
        dev = make(MAPPABLE + ['<Object><Index>#x1600</Index><Name>o</Name><Type>DT1600</Type><BitSize>336</BitSize>'
                               '<Info><SubItem><Name>SubIndex 000</Name><Info><MaxValue>10</MaxValue></Info>'
                               '</SubItem></Info></Object>',
                               '<Object><Index>#x1A00</Index><Name>o</Name><Type>DT1A00</Type><BitSize>336</BitSize>'
                               '<Info><SubItem><Name>SubIndex 000</Name><Info><MaxValue>#x10</MaxValue></Info>'
                               '</SubItem></Info></Object>'], [])
        self.assertEqual(dev.max_entries, {0x1600: 10, 0x1A00: 16})

    def test_mapping_facts_read_from_the_esi(self):
        dev = make(MAPPABLE, [])
        self.assertEqual(dev.mapping[0x6040], "R")
        self.assertEqual(dev.drive_modes, 0x180)
        self.assertTrue(dev.pdo_config)


def fslave(outputs=(), inputs=(), profile="cia402", mode="csp", configure=True, **kw):
    def pdos(items):
        return [{"pdo": "0x%04X" % i, "sm": sm,
                 "entries": [{"object": "0x%04X:%02X" % (o, 0), "bits": b} for o, b in es]}
                for i, sm, es in items]
    s = {"position": 1, "name": "x", "profile": profile, "configure-pdos": configure,
         "process-data": {"outputs": pdos(outputs), "inputs": pdos(inputs)}}
    if profile == "cia402":
        s["cia402"] = {"mode": mode}
    s.update(kw)
    return s


ESI_PDOS = [pdo("RxPdo", 0x1600, [(0x6040, 16), (0x607A, 32)], sm=2),
            pdo("RxPdo", 0x1701, [(0x6040, 16), (0x607A, 32)], fixed=True, exclude=[0x1702]),
            pdo("RxPdo", 0x1702, [(0x6040, 16), (0x60FF, 32)], fixed=True, exclude=[0x1701]),
            pdo("TxPdo", 0x1A00, [(0x6041, 16), (0x6064, 32)], sm=3)]
TX = [(0x1A00, 3, [(0x6041, 16), (0x6064, 32)])]


class TestCheckSlave(unittest.TestCase):
    def setUp(self):
        self.dev = make(MAPPABLE, ESI_PDOS)
        self.dev.max_entries[0x1600] = 3

    def texts(self, slave, dev=None, exact=True):
        return pdoreq.check_slave(slave, dev or self.dev, exact)

    def test_a_file_like_the_esi_has_no_problems(self):
        s = fslave([(0x1600, 2, [(0x6040, 16), (0x607A, 32)])], TX)
        self.assertEqual([x for x in self.texts(s) if x[0] != "note"], [])

    def test_no_esi_no_check(self):
        self.assertEqual(pdoreq.check_slave(fslave([(0x1600, 2, [(0x6040, 16)])]), None), [])

    def test_slave_that_is_not_written_is_not_checked(self):
        s = fslave([(0x1701, 2, [(0x6040, 16)])], TX, configure=False)
        self.assertEqual([x for x in self.texts(s) if x[0] != "note"], [])

    def test_changed_fixed_pdo(self):
        s = fslave([(0x1701, 2, [(0x6040, 16), (0x607A, 32), (0x60FF, 32)])], TX)
        found = self.texts(s)
        self.assertEqual(found[0][0], "error")
        self.assertIn("0x1701 is fixed in the ESI file", found[0][1])

    def test_another_revision_only_warns(self):
        s = fslave([(0x1701, 2, [(0x6040, 16)])], TX)
        self.assertEqual(self.texts(s, exact=False)[0][0], "warning")

    def test_exclude(self):
        s = fslave([(0x1701, 2, [(0x6040, 16), (0x607A, 32)]), (0x1702, 2, [(0x6040, 16), (0x60FF, 32)])], TX)
        self.assertTrue(any("exclude each other" in t for _l, t in self.texts(s)))

    def test_wrong_direction(self):
        s = fslave(TX, [(0x1600, 3, [(0x6040, 16), (0x607A, 32)])])
        found = self.texts(s)
        self.assertTrue(any("is a RX PDO in the ESI file, but is listed under inputs" in t for _l, t in found))

    def test_too_many_entries(self):
        s = fslave([(0x1600, 2, [(0x6040, 16), (0x607A, 32), (0x60FF, 32), (0x6060, 8)])], TX)
        self.assertTrue(any("takes at most 3 entries, the file has 4" in t for _l, t in self.texts(s)))

    def test_object_that_cannot_be_mapped_that_way(self):
        s = fslave([(0x1600, 2, [(0x6040, 16), (0x6064, 32)])], TX)     # 0x6064 is a TX object
        self.assertTrue(any("0x6064 Position actual value can only be mapped as TX" in t
                            for _l, t in self.texts(s)))

    def test_pdo_config_off(self):
        dev = make(MAPPABLE, ESI_PDOS, pdo_config=False)
        s = fslave([(0x1600, 2, [(0x6040, 16)])], TX)
        self.assertTrue(any("PdoConfig is off" in t for _l, t in self.texts(s, dev)))

    def test_pdo_assign_off(self):
        dev = make(MAPPABLE, ESI_PDOS, pdo_assign=False)
        s = fslave([(0x1701, 2, [(0x6040, 16), (0x607A, 32)])], TX)
        self.assertTrue(any("PdoAssign is off" in t for _l, t in self.texts(s, dev)))
        s = fslave([(0x1600, 2, [(0x6040, 16), (0x607A, 32)])], TX)
        self.assertFalse(any("PdoAssign" in t for _l, t in self.texts(s, dev)))

    def test_session_names_the_slave(self):
        from ecatsetup import session
        sess = session.Session([])
        sess.esi_devices = [self.dev]
        s = fslave([(0x1701, 2, [(0x6040, 16)])], TX,
                   identity={"vendor": "0x00000001", "product": "0x00000001", "revision": "0x00000001"})
        sess.cfg = {"slaves": [s]}
        found = sess.pdo_problems()
        self.assertEqual(found[0][0], "error")
        self.assertTrue(found[0][1].startswith("slave 1 (x): PDO 0x1701 is fixed"))

    def test_exclude_named_only_by_the_higher_pdo(self):
        dev = make(MAPPABLE, [pdo("RxPdo", 0x1701, [(0x6040, 16), (0x607A, 32)], fixed=True),
                              pdo("RxPdo", 0x1702, [(0x6040, 16), (0x60FF, 32)], fixed=True, exclude=[0x1701])])
        s = fslave([(0x1701, 2, [(0x6040, 16), (0x607A, 32)]), (0x1702, 2, [(0x6040, 16), (0x60FF, 32)])], [])
        found = [t for _l, t in pdoreq.check_slave(s, dev) if "exclude" in t]
        self.assertEqual(found, ["PDO 0x1701 and PDO 0x1702 exclude each other in the ESI file"])

    def test_a_drive_without_a_mode_does_not_crash_the_check(self):
        s = fslave([(0x1600, 2, [(0x6040, 16)])], TX)
        s["cia402"] = {}
        self.assertIsInstance(self.texts(s), list)
        self.assertEqual(pdoreq.cia402_status(None, [], 1, set()), [])
        self.assertEqual(pdoreq.cia402_status("bad", ["hm"], 1, set()), [])

    def test_unknown_pdo_is_a_warning(self):
        s = fslave([(0x1999, 2, [(0x6040, 16)])], TX)
        self.assertEqual(self.texts(s)[0][0], "warning")

    def test_single_mode_without_mode_objects_is_a_note(self):
        s = fslave([(0x1600, 2, [(0x6040, 16), (0x607A, 32)])], TX)
        s["settings"] = [{"object": "0x6060:00", "type": "SINT", "value": 8}]
        notes = [t for l, t in self.texts(s) if l == "note"]
        self.assertEqual(len(notes), 2)
        self.assertIn("the mode is written at every start (0x6060:00 = 8)", notes[0])
        self.assertIn("0x6061", notes[1])

    def test_generic_on_a_drive_that_would_do_for_cia402(self):
        s = fslave([(0x1600, 2, [(0x6040, 16), (0x607A, 32)])], TX, profile="generic")
        self.assertTrue(any("runs this CiA 402 drive as generic" in t for _l, t in self.texts(s)))
        s = fslave([(0x1600, 2, [(0x6040, 16)])], TX, profile="generic")
        self.assertFalse(any("generic" in t for _l, t in self.texts(s)))


class TestStatus(unittest.TestCase):
    def test_enough(self):
        have = {(0x6040, 0), (0x607A, 0), (0x6041, 0), (0x6064, 0), (0x6060, 0), (0x6061, 0)}
        self.assertEqual([x.level for x in pdoreq.cia402_status("csp", [], 1, have)], ["ok"])

    def test_missing_objects_are_an_error_with_names(self):
        st = pdoreq.cia402_status("csp", [], 1, {(0x6041, 0), (0x6064, 0), (0x6060, 0), (0x6061, 0)})
        self.assertEqual(st[0].level, "error")
        self.assertIn("cannot run as CiA 402 in CSP: 0x6040 Controlword, 0x607A Target position missing",
                      st[0].text)
        self.assertEqual(st[0].add, [(0x6040, "rx"), (0x607A, "rx")])

    def test_single_mode_without_mode_objects(self):
        base = {(0x6040, 0), (0x607A, 0), (0x6041, 0), (0x6064, 0)}
        # neither set nor seen: an error, fixed by the setting
        st = pdoreq.cia402_status("csp", [], 1, base)
        self.assertEqual([x.level for x in st], ["error", "info"])
        self.assertIn("neither set nor seen: add the setting 0x6060:00 = 8", st[0].text)
        self.assertEqual(st[0].add, [(0x6060, "setting")])
        # the mode as a setting: information
        st = pdoreq.cia402_status("csp", [], 1, base, mode_settings={0: 8})
        self.assertEqual([x.level for x in st], ["info", "info"])
        self.assertIn("the mode is written at every start (0x6060:00 = 8)", st[0].text)
        # seen but not set: the drive must already be in the mode
        st = pdoreq.cia402_status("pv", [], 1, {(0x6040, 0), (0x60FF, 0), (0x6041, 0), (0x606C, 0), (0x6061, 0)})
        self.assertEqual([x.level for x in st], ["info"])
        self.assertIn("must already be in PV", st[0].text)

    def test_several_modes_without_mode_objects_is_an_error(self):
        st = pdoreq.cia402_status("csp", ["hm"], 1, {(0x6040, 0), (0x607A, 0), (0x6041, 0), (0x6064, 0)})
        self.assertEqual([x.level for x in st], ["error"])
        self.assertIn("0x6060", st[0].text)


def drive_sii():
    return sii.Sii(vendor_id=1, product_code=1, revision=1, serial=0, mailbox_protocols=0x04, name="T")


def proposal(dev, mode="csp", extra=(), **kw):
    return propose.propose({"position": 1, "dc": False}, drive_sii(), coe.Dictionary(),
                           device_type=0x20192, drive_modes=0x1AD, esi_dev=dev, esi_exact=True,
                           mode=mode, extra_modes=extra, **kw)


class TestProposal(unittest.TestCase):
    def test_assignment_that_is_enough_is_kept(self):
        dev = make(MAPPABLE, [pdo("RxPdo", 0x1600, [(0x6040, 16), (0x607A, 32)], sm=2),
                              pdo("TxPdo", 0x1A00, [(0x6041, 16), (0x6064, 32)], sm=3)])
        p = proposal(dev)
        self.assertEqual(p.pdo_way, "")
        self.assertEqual([c.index for c in p.pdos], [0x1600, 0x1A00])
        self.assertEqual([s.level for s in p.status], ["info", "info"])   # the mode as a setting
        self.assertEqual(p.init_cmds, [{"at": "PS", "object": "0x6060:00", "from": "profile"}])
        self.assertEqual(config.check(config.from_proposals([p])), [])

    def test_own_mapping_is_built_in_the_open_pdos(self):
        dev = make(MAPPABLE, [pdo("RxPdo", 0x1600, [(0x6040, 16)], sm=2),
                              pdo("TxPdo", 0x1A00, [(0x6041, 16)], sm=3)])
        p = proposal(dev, way="recommended")
        self.assertEqual(p.pdo_way, "build")
        rx = next(c for c in p.pdos if c.direction == "rx")
        tx = next(c for c in p.pdos if c.direction == "tx")
        self.assertEqual((rx.sm, [e.index for e in rx.entries]), (2, [0x6040, 0x607A]))
        self.assertEqual([e.index for e in tx.entries][:2], [0x6041, 0x6064])
        self.assertTrue(all(e.role == "profile" for e in rx.entries))
        self.assertFalse(any(s.level == "error" for s in p.status))
        yaml_slave = config.slave_from_proposal(p)
        self.assertEqual([e["object"] for e in yaml_slave["process-data"]["outputs"][0]["entries"]],
                         ["0x6040:00", "0x607A:00"])

    def test_only_the_direction_that_lacks_something_is_rebuilt(self):
        dev = make(MAPPABLE, [pdo("RxPdo", 0x1701, [(0x6040, 16), (0x607A, 32)], fixed=True, sm=2),
                              pdo("TxPdo", 0x1A00, [(0x6041, 16)], sm=3)])
        p = proposal(dev, way="recommended")
        self.assertEqual(p.pdo_way, "build")
        self.assertEqual([c.index for c in p.pdos if c.direction == "rx"], [0x1701])
        tx = next(c for c in p.pdos if c.direction == "tx")
        self.assertEqual([e.index for e in tx.entries][:2], [0x6041, 0x6064])

    def test_a_covered_output_is_kept_when_only_the_input_is_built(self):
        dev = make(MAPPABLE, [pdo("RxPdo", 0x1600, [(0x6040, 16)], sm=2),
                              pdo("TxPdo", 0x1B01, [(0x6041, 16), (0x6064, 32)], fixed=True, sm=3)])
        p = proposal(dev, way="recommended")
        self.assertEqual(p.pdo_way, "build")
        self.assertEqual([c.index for c in p.pdos if c.direction == "tx"], [0x1B01])
        rx = next(c for c in p.pdos if c.direction == "rx")
        self.assertEqual([e.index for e in rx.entries], [0x6040, 0x607A])

    def test_ready_way_with_fixed_pdos_for_several_modes(self):
        pdos = [pdo("RxPdo", 0x1701, [(0x6040, 16), (0x607A, 32)], fixed=True, sm=2, exclude=[0x1702]),
                pdo("RxPdo", 0x1702, [(0x6040, 16), (0x607A, 32), (0x6060, 8)], fixed=True, exclude=[0x1701]),
                pdo("TxPdo", 0x1B01, [(0x6041, 16), (0x6064, 32)], fixed=True, sm=3, exclude=[0x1B02]),
                pdo("TxPdo", 0x1B02, [(0x6041, 16), (0x6064, 32), (0x6061, 8)], fixed=True, exclude=[0x1B01])]
        p = proposal(make(MAPPABLE, pdos, pdo_config=False), extra=["hm"], way="recommended")
        self.assertEqual(p.pdo_way, "ready")
        self.assertEqual([(c.index, c.sm) for c in p.pdos], [(0x1702, 2), (0x1B02, 3)])
        self.assertEqual([s.level for s in p.status], ["ok"])

    def test_a_better_way_is_only_recommended(self):
        dev = make(MAPPABLE, [pdo("RxPdo", 0x1600, [(0x6040, 16)], sm=2),
                              pdo("TxPdo", 0x1A00, [(0x6041, 16)], sm=3)])
        p = proposal(dev)
        self.assertEqual(p.pdo_way, "")
        self.assertEqual([[e.index for e in c.entries] for c in p.pdos], [[0x6040], [0x6041]])
        self.assertEqual(p.recommendation["way"], "own mapping in the open PDOs")
        self.assertIn("lacks what CSP needs", p.recommendation["why"])
        self.assertTrue(any(n.startswith("recommended PDO mapping") for n in p.notes))
        self.assertEqual(p.status[0].level, "error")

    def test_a_way_can_be_chosen_by_its_title(self):
        pdos = [pdo("RxPdo", 0x1701, [(0x6040, 16), (0x607A, 32), (0x6060, 8)], fixed=True, exclude=[0x1702]),
                pdo("RxPdo", 0x1702, [(0x6040, 16), (0x607A, 32), (0x6060, 8), (0x60FF, 32)], fixed=True,
                    exclude=[0x1701]),
                pdo("RxPdo", 0x1600, [(0x6040, 16)], sm=2),
                pdo("TxPdo", 0x1B01, [(0x6041, 16), (0x6064, 32), (0x6061, 8)], fixed=True, sm=3)]
        dev = make(MAPPABLE, pdos)
        p = proposal(dev, extra=["hm"])
        self.assertEqual(p.recommendation["way"], "fixed PDOs 0x1701, 0x1B01")
        p = proposal(dev, extra=["hm"], way="fixed PDOs 0x1702, 0x1B01")
        self.assertEqual([c.index for c in p.pdos], [0x1702, 0x1B01])
        p = proposal(dev, extra=["hm"], way="own mapping in the open PDOs")
        self.assertEqual(p.pdo_way, "build")
        self.assertEqual([c.index for c in p.pdos if c.direction == "rx"], [0x1600])

    def test_impossible_is_an_error_line_and_a_note(self):
        pdos = [pdo("RxPdo", 0x1600, [(0x6040, 16)], sm=2), pdo("TxPdo", 0x1A00, [(0x6041, 16)], sm=3)]
        p = proposal(make(MAPPABLE, pdos, pdo_config=False))
        self.assertEqual(p.pdo_way, "")
        self.assertEqual(p.status[0].level, "error")
        self.assertTrue(any("PdoConfig" in n for n in p.notes))
        self.assertTrue(any("cannot run as CiA 402" in n for n in p.notes))

    def test_an_assignment_asked_for_is_not_overridden(self):
        pdos = [pdo("RxPdo", 0x1600, [(0x6040, 16)], sm=2), pdo("TxPdo", 0x1A00, [(0x6041, 16)], sm=3)]
        p = proposal(make(MAPPABLE, pdos), pdo_set="nothing of that name")
        self.assertEqual(p.pdo_way, "")
        self.assertEqual(p.status[0].level, "error")

    def test_limit_of_the_open_pdo_never_cuts_what_is_needed(self):
        dev = make(MAPPABLE, [pdo("RxPdo", 0x1600, [(0x6040, 16), (0x60B8, 16), (0x60FE, 32)], sm=2),
                              pdo("TxPdo", 0x1A00, [(0x6041, 16)], sm=3)])
        dev.max_entries[0x1600] = 2
        p = proposal(dev, way="recommended")
        rx = next(c for c in p.pdos if c.direction == "rx")
        self.assertEqual([e.index for e in rx.entries], [0x6040, 0x607A])


class TestWaysFor(unittest.TestCase):
    def test_none_when_the_mapping_is_enough(self):
        dev = make(MAPPABLE, ESI_PDOS)
        s = fslave([(0x1600, 2, [(0x6040, 16), (0x607A, 32)])], TX)
        self.assertEqual(pdoreq.ways_for(s, dev), [])
        self.assertEqual(pdoreq.ways_for(s, None), [])

    def test_ready_ways_first_then_build(self):
        dev = make(MAPPABLE, ESI_PDOS)
        s = fslave([(0x1600, 2, [(0x6040, 16)])], [(0x1A00, 3, [(0x6041, 16)])])
        ways = pdoreq.ways_for(s, dev)
        self.assertEqual([(w.kind, w.title) for w in ways],
                         [("ready", "the default assignment"), ("build", "own mapping in the open PDOs")])
        self.assertEqual(ways[0].why, "has what CSP needs; not 0x603F Error code, 0x6061 Modes of "
                                      "operation display (good to have)")
        self.assertIn("open PDOs", ways[-1].why)

    def test_the_other_modes_count(self):
        dev = make(MAPPABLE, ESI_PDOS)
        s = fslave([(0x1600, 2, [(0x6040, 16), (0x607A, 32)])], TX)
        s["cia402"]["modes"] = ["csv"]
        self.assertIn("CSP, CSV", pdoreq.ways_for(s, dev)[0].why)
        s["cia402"]["modes"] = ["hm"]          # the device's 0x6502 has no homing: no way
        self.assertEqual(pdoreq.ways_for(s, dev), [])

    def test_no_mode_no_ways(self):
        s = fslave([(0x1600, 2, [(0x6040, 16)])], TX)
        s["cia402"] = {}
        self.assertEqual(pdoreq.ways_for(s, make(MAPPABLE, ESI_PDOS)), [])


# `ethercat pdos -p4` on the A6 simulation, as the master read it (2026-11-22)
PDOS_TEXT = """SM0: PhysAddr 0x1000, DefaultSize  256, ControlRegister 0x26, Enable 1
SM1: PhysAddr 0x1400, DefaultSize  256, ControlRegister 0x22, Enable 1
SM2: PhysAddr 0x1800, DefaultSize   12, ControlRegister 0x64, Enable 1
  RxPDO 0x1600 ""
    PDO entry 0x6040:00, 16 bit, ""
    PDO entry 0x607a:00, 32 bit, ""
    PDO entry 0x6060:00,  8 bit, ""
SM3: PhysAddr 0x1c00, DefaultSize   28, ControlRegister 0x20, Enable 1
  TxPDO 0x1a00 "TXPDO 1 Mapping Parameter "
    PDO entry 0x6041:00, 16 bit, "Status Word"
    PDO entry 0x6064:00, 32 bit, ""
    PDO entry 0x0000:00,  8 bit, ""
    PDO entry 0x6061:00,  8 bit, ""
"""


class TestOwnMapping(unittest.TestCase):
    """Way A: the slave's own mapping (configure-pdos false), as the master read it."""

    def setUp(self):
        from ecatsetup import tool
        self.own = tool.parse_pdos(PDOS_TEXT)
        self.dev = make(MAPPABLE + [obj(0x6060, "R"), obj(0x6061, "T")], ESI_PDOS)

    def test_parse(self):
        self.assertEqual([(sm, d, x, n) for sm, d, x, n, _e in self.own],
                         [(2, "rx", 0x1600, ""), (3, "tx", 0x1A00, "TXPDO 1 Mapping Parameter ")])
        self.assertEqual(self.own[1][4], [(0x6041, 0, 16, "Status Word"), (0x6064, 0, 32, ""), (0, 0, 8, ""),
                                          (0x6061, 0, 8, "")])
        from ecatsetup import tool
        self.assertEqual(tool.parse_pdos(""), [])

    def test_judged_against_the_modes(self):
        s = fslave([(0x1600, 2, [(0x6040, 16)])], TX)
        level, text = pdoreq.own_row(s, self.dev, self.own)
        self.assertEqual(level, "ok")
        self.assertIn("the slave's own: 0x1600, 0x1A00; has what CSP needs", text)
        self.assertIn("lost at power-off", text)         # not the ESI file's default
        s["cia402"]["modes"] = ["csv"]
        level, text = pdoreq.own_row(s, self.dev, self.own)
        self.assertEqual(level, "warning")
        self.assertIn("lacks 0x606C Velocity actual value, 0x60FF Target velocity", text)
        self.assertIsNone(pdoreq.own_row(s, self.dev, None))
        self.assertIsNone(pdoreq.own_row(s, self.dev, []))

    def test_the_esi_default_is_said_so(self):
        from ecatsetup import tool
        own = tool.parse_pdos("SM2: x\n  RxPDO 0x1600 \"\"\n    PDO entry 0x6040:00, 16 bit, \"\"\n"
                              "    PDO entry 0x607a:00, 32 bit, \"\"\n"
                              "SM3: x\n  TxPDO 0x1a00 \"\"\n    PDO entry 0x6041:00, 16 bit, \"\"\n"
                              "    PDO entry 0x6064:00, 32 bit, \"\"\n")
        _level, text = pdoreq.own_row(fslave(), self.dev, own)
        self.assertIn("the ESI file's default", text)

    def test_a_proposal_from_it(self):
        own = pdoreq.own_pdos(self.own, self.dev)
        self.assertEqual([(p.index, p.sm, p.direction) for p in own], [(0x1600, 2, "rx"), (0x1A00, 3, "tx")])
        self.assertEqual(own[1].entries[0].name, "Status Word")
        self.assertEqual(own[0].entries[1].data_type, "DINT")       # 0x607A from the ESI file's PDOs
        self.assertEqual(own[1].entries[2].data_type, "USINT")      # a gap: by its size
        p = proposal(self.dev, own=own)
        self.assertEqual(p.pdo_way, "own")
        self.assertIsNone(p.recommendation)
        s = config.slave_from_proposal(p, name="a6")
        self.assertIs(s["configure-pdos"], False)
        self.assertEqual([x["pdo"] for d in s["process-data"].values() for x in d], ["0x1600", "0x1A00"])
        self.assertIsNone(pdoreq.own_differs(s, self.own))

    def test_the_file_against_the_slave(self):
        s = fslave([(0x1701, 2, [(0x6040, 16), (0x607A, 32)])], [(0x1A00, 3, [(0x6041, 16)])])
        text = pdoreq.own_differs(s, self.own)
        self.assertIn("on SM2 the slave has 0x1600 where the file has 0x1701", text)
        s = fslave([(0x1600, 2, [(0x6040, 16), (0x607A, 32), (0x6060, 8)])],
                   [(0x1A00, 3, [(0x6041, 16), (0x6064, 32), (0, 8), (0x6061, 8)])])
        self.assertIsNone(pdoreq.own_differs(s, self.own))
        s["process-data"]["outputs"][0]["entries"].pop()        # one entry less: differs
        self.assertIn("SM2", pdoreq.own_differs(s, self.own))


class TestSessionWays(unittest.TestCase):
    """Session.apply_pdos / change_profile: only the PDO part of the entry changes."""

    def setUp(self):
        from unittest import mock
        from ecatsetup import session
        self.dev = make(MAPPABLE + [obj(0x60F4, "T")],
                        [pdo("RxPdo", 0x1600, [(0x6040, 16)], sm=2),
                         pdo("TxPdo", 0x1A00, [(0x6041, 16), (0x60F4, 32)], sm=3)])
        self.calls = []

        def read_slave(pos, devices, mode, pdo_set=None, progress=None, extra_modes=(), way=None,
                       profile=None):
            self.calls.append((mode, list(extra_modes), way, profile))
            return {}, coe.Dictionary(), proposal(self.dev, mode, extra_modes, way=way, profile=profile)
        patch = mock.patch.object(session, "read_slave", read_slave)
        patch.start()
        self.addCleanup(patch.stop)
        self.sess = session.Session([])
        self.sess.esi_devices = [self.dev]
        first = proposal(self.dev)
        self.sess.cfg = config.from_proposals([first])
        self.slave = self.sess.cfg["slaves"][0]
        self.slave["settings"] = [{"object": "0x2000:01", "type": "UINT", "value": 3}]
        tx = self.slave["process-data"]["inputs"][0]["entries"]
        next(e for e in tx if e["object"] == "0x60F4:00")["pin"] = "my-ferror"

    def test_apply_the_recommended_way(self):
        self.sess.apply_pdos(1, way="recommended")
        self.assertEqual([e["object"] for e in self.slave["process-data"]["outputs"][0]["entries"]],
                         ["0x6040:00", "0x607A:00"])
        self.assertEqual(self.slave["settings"], [{"object": "0x2000:01", "type": "UINT", "value": 3}])
        pin = next(e for e in self.slave["process-data"]["inputs"][0]["entries"] if e["object"] == "0x60F4:00")
        self.assertEqual(pin["pin"], "my-ferror")         # the user's pin name kept
        self.assertEqual(self.calls[-1][2], "recommended")

    def test_the_other_modes_go_along(self):
        self.slave["cia402"]["modes"] = ["hm"]
        self.sess.apply_pdos(1, way="recommended")
        self.assertEqual(self.calls[-1][:2], ("csp", ["hm"]))
        self.assertIn("0x6060:00", [e["object"] for e in self.slave["process-data"]["outputs"][0]["entries"]])

    def test_assign_the_ticked_pdos(self):
        from unittest import mock
        from ecatsetup import session

        def read_slave(pos, devices, mode, pdo_set=None, progress=None, extra_modes=(), way=None,
                       profile=None, assignment=None):
            return {}, coe.Dictionary(), proposal(self.dev, mode, extra_modes, assignment=assignment)
        self.slave["process-data"]["outputs"][0]["entries"].append(
            {"object": "0x2000:01", "bits": 16, "pin": "mine"})      # the user's own entry
        with mock.patch.object(session, "read_slave", read_slave), \
                mock.patch.object(self.sess, "esi_device", return_value=self.dev):
            self.sess.assign_pdos(1, [0x1600, 0x1A00])
            # what was read is kept: the next choice needs no bus
            self.sess.bus[1].info = {"position": 1, "dc": False}
            self.sess.bus[1].proposal.reads = {"sii": drive_sii(), "device_type": 0x20192,
                                               "drive_modes": 0x1AD, "dev": self.dev, "exact": True,
                                               "detected": None, "configured": None}
            with mock.patch.object(session, "read_slave", side_effect=AssertionError("bus read")):
                self.sess.assign_pdos(1, [0x1600, 0x1A00])
        # an open PDO the file has keeps its entries (the status lines say what is missing)
        self.assertEqual([e["object"] for e in self.slave["process-data"]["outputs"][0]["entries"]],
                         ["0x6040:00", "0x2000:01"])
        pin = next(e for e in self.slave["process-data"]["inputs"][0]["entries"] if e["object"] == "0x60F4:00")
        self.assertEqual(pin["pin"], "my-ferror")
        self.assertEqual(self.slave["settings"][0]["value"], 3)

    def test_use_the_slaves_own_mapping(self):
        from ecatsetup import session, tool
        self.sess.bus[1] = session.BusSlave({"position": 1, "dc": False, "vendor_id": 1, "product_code": 1,
                                             "revision": 1},
                                            coe.Dictionary(), proposal(self.dev))
        own = tool.parse_pdos("SM2: x\n  RxPDO 0x1600 \"\"\n    PDO entry 0x6040:00, 16 bit, \"\"\n"
                              "    PDO entry 0x607a:00, 32 bit, \"\"\n"
                              "SM3: x\n  TxPDO 0x1a00 \"\"\n    PDO entry 0x6041:00, 16 bit, \"\"\n"
                              "    PDO entry 0x6064:00, 32 bit, \"\"\n")
        self.sess.bus[1].proposal.reads = {"sii": drive_sii(), "device_type": 0x20192, "drive_modes": 0x1AD,
                                           "dev": self.dev, "exact": True, "detected": None,
                                           "configured": None, "own": own}
        from unittest import mock
        with mock.patch.object(self.sess, "esi_device", return_value=self.dev):
            self.assertEqual(self.sess.own_mapping(1), own)
            self.sess.use_own_pdos(1)
            self.assertIs(self.slave["configure-pdos"], False)
            self.assertEqual([e["object"] for e in self.slave["process-data"]["outputs"][0]["entries"]],
                             ["0x6040:00", "0x607A:00"])
            self.assertEqual(self.slave["settings"][0]["value"], 3)       # the rest stays
            self.assertFalse(any("own PDO mapping" in t for t in self.sess.check()[1]))
            # the slave changed since: the check says so
            self.sess.bus[1].proposal.reads["own"] = own[:1]
            self.assertTrue(any("runs on its own PDO mapping" in t and "SM3" in t
                                for t in self.sess.check()[1]), self.sess.check()[1])
        self.sess.bus[1].proposal.reads["own"] = None
        with self.assertRaises(ValueError):
            self.sess.use_own_pdos(1)

    def test_no_wait_for_a_dictionary_the_slave_does_not_give(self):
        from ecatsetup import session
        sii = drive_sii()
        sii.coe_details = 0x0D          # SDO, PDO assign/config; no SDO information (the A6)
        self.assertFalse(session.sdo_info(sii))
        sii.coe_details = 0x2F
        self.assertTrue(session.sdo_info(sii))
        sii.coe_details = 0             # not filled in: look as before
        self.assertTrue(session.sdo_info(sii))

    def test_change_to_generic_and_back(self):
        self.sess.change_profile(1, "generic")
        self.assertEqual(self.slave["profile"], "generic")
        self.assertNotIn("cia402", self.slave)
        e = self.slave["process-data"]["outputs"][0]["entries"][0]
        self.assertNotEqual(e.get("use"), "profile")      # a pin now
        self.assertEqual(self.slave["settings"][0]["value"], 3)
        self.sess.change_profile(1, "cia402")
        self.assertEqual((self.slave["profile"], self.slave["cia402"]["mode"]), ("cia402", "csp"))
        self.assertEqual(self.slave["process-data"]["outputs"][0]["entries"][0]["use"], "profile")


@unittest.skipUnless(os.path.exists(os.path.expanduser("~/esi/stepperonline")), "no A6 ESI file")
class TestA6(unittest.TestCase):
    def setUp(self):
        folder = os.path.expanduser("~/esi/stepperonline")
        files = [os.path.join(folder, f) for f in os.listdir(folder) if f.endswith(".xml")]
        self.dev = esi.load_file(files[0])[0]

    def test_the_open_pdo_takes_ten_entries(self):
        self.assertEqual(self.dev.max_entries[0x1600], 10)     # 336 bits = 16 + 10 x 32

    def test_csp_is_ready_with_the_default_assignment(self):
        v, _ = pdoreq.cia402_verdicts(self.dev, "csp")
        self.assertEqual(v.status, pdoreq.READY)
        default = next(w for w in v.ways if w.title == "the default assignment")
        self.assertEqual((default.rx, default.tx), ([0x1701], [0x1B01]))

    def test_proposal_for_csp_and_homing_fills_the_open_pdos(self):
        # the open PDOs with just what is needed come before fixed PDOs (the user, 2026-11-22)
        p = proposal(self.dev, extra=["hm"], way="recommended")
        self.assertEqual(p.pdo_way, "build")
        self.assertEqual([c.index for c in p.pdos], [0x1600, 0x1A00])
        rx, tx = ([(e.index, e.subindex) for e in c.entries] for c in p.pdos)
        self.assertTrue({(0x6040, 0), (0x607A, 0), (0x6060, 0)} <= set(rx), rx)
        self.assertTrue({(0x6041, 0), (0x6064, 0), (0x6061, 0)} <= set(tx), tx)
        self.assertEqual([s.level for s in p.status], ["ok"])

    def test_the_fixed_pdos_can_still_be_chosen(self):
        p = proposal(self.dev, extra=["hm"], way="fixed PDOs 0x1702, 0x1B02")
        self.assertEqual([c.index for c in p.pdos], [0x1702, 0x1B02])
        self.assertEqual([s.level for s in p.status], ["ok"])

    def test_proposal_for_csp_and_homing_only_recommends(self):
        p = proposal(self.dev, extra=["hm"])
        self.assertEqual(p.pdo_way, "")
        self.assertEqual([c.index for c in p.pdos], [0x1701, 0x1B01])
        self.assertEqual(p.recommendation["way"], "own mapping in the open PDOs")

    def test_several_modes_need_fixed_pdos_with_the_mode_objects(self):
        v, _ = pdoreq.cia402_verdicts(self.dev, "csp", ["hm"])
        self.assertEqual({tuple(w.rx) for w in v.ways if w.kind == "ready"},
                         {(0x1702,), (0x1703,)})
        self.assertEqual(v.ways[0].kind, pdoreq.BUILD)

    def test_every_pdo_is_a_row_with_what_it_gives(self):
        s = fslave([(0x1701, 2, [(0x6040, 16)])], [(0x1B01, 3, [(0x6041, 16)])])
        s["cia402"]["modes"] = ["hm"]
        rows = {r.index: r for r in pdoreq.assignment_rows(s, self.dev)}
        self.assertEqual(sorted(rows), sorted(p.index for p in self.dev.pdos))
        self.assertEqual([i for i, r in sorted(rows.items()) if r.in_file], [0x1701, 0x1B01])
        self.assertEqual(rows[0x1701].level, "warning")
        self.assertIn("lacks 0x6060", rows[0x1701].text)
        self.assertEqual((rows[0x1702].level, rows[0x1B02].level), ("ok", "ok"))
        self.assertEqual(rows[0x1600].level, "ok")
        self.assertIn("open: filled with what CSP, HM needs", rows[0x1600].text)
        self.assertIn(0x1702, rows[0x1701].excludes)

    def test_the_chosen_pdos_are_taken_and_the_open_one_filled(self):
        p = proposal(self.dev, extra=["hm"], assignment=[0x1600, 0x1B02])
        self.assertEqual(p.pdo_way, "chosen")
        self.assertEqual([(c.index, c.sm) for c in p.pdos], [(0x1600, 2), (0x1B02, 3)])
        rx = [e.index for e in p.pdos[0].entries]
        self.assertIn(0x6060, rx)
        self.assertEqual([s.level for s in p.status], ["ok"])
        self.assertIsNone(p.recommendation)
        # what the user ticked is taken as it is, also when it lacks something
        p = proposal(self.dev, extra=["hm"], assignment=[0x1701, 0x1B01])
        self.assertEqual([c.index for c in p.pdos], [0x1701, 0x1B01])
        self.assertIsNone(p.recommendation)
        self.assertEqual(p.status[0].level, "error")


if __name__ == "__main__":
    unittest.main()
