"""A modular device's modules in its slots (ETG.2000 Slots/Modules): indexes moved per
slot and slot group, the proposal's PDOs and InitCmds, 0xF030."""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ecatsetup import coe, config, esi, propose, sii, slots  # noqa: E402

_DIR = tempfile.TemporaryDirectory()


def entry(index, sub, bits, name, dtype="UINT", slot=True):
    return ("<Entry><Index%s>#x%04X</Index><SubIndex>%d</SubIndex><BitLen>%d</BitLen><Name>%s</Name>"
            "<DataType>%s</DataType></Entry>" % (' DependOnSlot="1"' if slot else "", index, sub, bits, name, dtype))


MODULES = """<Modules>
  <Module><Type ModuleIdent="#x00000010">DI4</Type><Name>DI4</Name>
    <TxPdo Sm="3"><Index DependOnSlot="1">#x1A00</Index><Name>DI4 Inputs</Name>%s</TxPdo>
    <Mailbox><CoE><InitCmd><Transition>PS</Transition><Index DependOnSlot="1">#x8000</Index>
      <SubIndex>1</SubIndex><Data>01</Data></InitCmd></CoE></Mailbox></Module>
  <Module><Type ModuleIdent="#x00000020">DO4</Type><Name>DO4</Name>
    <RxPdo><Index DependOnSlot="1">#x1600</Index><Name>DO4 Outputs</Name>%s</RxPdo>
    <TxPdo><Index DependOnSlot="1">#x1A10</Index><Name>DO4 Status</Name></TxPdo></Module>
  <Module><Type ModuleIdent="#x00000000">Empty</Type><Name>Empty</Name></Module>
</Modules>""" % (entry(0x6000, 1, 16, "Input", "UINT") + entry(0xF100, 1, 16, "Coupler status", "UINT", slot=False),
                  entry(0x7000, 1, 16, "Output", "UINT"))


def device(slots_xml, download=True):
    xml = ('<?xml version="1.0"?><EtherCATInfo><Vendor><Id>9</Id></Vendor><Descriptions><Devices>'
           '<Device><Type ProductCode="#x1" RevisionNo="#x1">C</Type><Name>Coupler</Name>'
           '<Profile><ProfileNo>5001</ProfileNo></Profile>'
           '<Slots DownloadModuleIdentList="%d" SlotPdoIncrement="1" SlotIndexIncrement="#x10">%s</Slots>'
           '<Mailbox><CoE PdoAssign="1" PdoConfig="0"/></Mailbox></Device></Devices>%s'
           '</Descriptions></EtherCATInfo>' % (int(download), slots_xml, MODULES))
    fd, path = tempfile.mkstemp(suffix=".xml", dir=_DIR.name)
    os.write(fd, xml.encode())
    os.close(fd)
    return esi.load_file(path)[0]


FOUR = ('<Slot MinInstances="1" MaxInstances="4"><Name>IO slot</Name>'
        '<ModuleIdent Default="1">#x0</ModuleIdent><ModuleIdent>#x10</ModuleIdent>'
        '<ModuleIdent>#x20</ModuleIdent></Slot>')


class TestRead(unittest.TestCase):
    def test_slots_and_modules(self):
        d = device(FOUR)
        self.assertEqual(len(d.slots.slots), 1)
        sl = d.slots.slots[0]
        self.assertEqual((sl.name, sl.idents, sl.default, sl.min, sl.max), ("IO slot", [0, 0x10, 0x20], 0, 1, 4))
        self.assertEqual((d.slots.pdo_increment, d.slots.index_increment, d.slots.download_ident_list), (1, 0x10, True))
        self.assertEqual(sorted(d.slots.modules), [0, 0x10, 0x20])
        self.assertEqual(d.slots.modules[0x10].pdo_moves, {0: (True, False)})


class TestLayout(unittest.TestCase):
    def test_indexes_move_with_the_slot(self):
        L = slots.layout(device(FOUR).slots, [0x10, 0x20, 0x10])
        self.assertEqual(L.problems, [])
        self.assertEqual([(p.index, p.direction, p.sm, [e.index for e in p.entries]) for p in L.pdos],
                         [(0x1A00, "tx", 3, [0x6000, 0xF100]), (0x1601, "rx", 2, [0x7010]),
                          (0x1A02, "tx", 3, [0x6020, 0xF100])])      # 0xF100 does not move; DO4 Status is empty
        # the same module twice: its PDOs named apart
        self.assertEqual([p.name for p in L.pdos], ["DI4 Inputs 0", "DO4 Outputs", "DI4 Inputs 2"])
        self.assertEqual([(i, s) for _t, i, s, _d, _c in L.init_cmds], [(0x8000, 1), (0x8020, 1)])

    def test_the_configured_list(self):
        L = slots.layout(device(FOUR).slots, [0x10, 0x20])
        self.assertEqual(L.settings, [{"object": "0xF030:00", "type": "USINT", "value": 0},
                                      {"object": "0xF030:01", "type": "UDINT", "value": 0x10},
                                      {"object": "0xF030:02", "type": "UDINT", "value": 0x20},
                                      {"object": "0xF030:00", "type": "USINT", "value": 2}])
        self.assertEqual(slots.layout(device(FOUR, download=False).slots, [0x10]).settings, [])

    def test_a_slots_own_increment_and_the_group(self):
        xml = ('<Slot MinInstances="1" MaxInstances="1" SlotGroup="2" SlotPdoIncrement="4" '
               'SlotGroupIndexIncrement="#x800"><Name>A</Name><ModuleIdent>#x10</ModuleIdent></Slot>')
        d = device(xml)
        # the group moves only what says DependOnSlotGroup: nothing in these modules
        L = slots.layout(d.slots, [0x10])
        self.assertEqual([p.index for p in L.pdos], [0x1A00])
        self.assertEqual(d.slots.slots[0].group, 2)
        # a slot's own increment over the device's (1), on slot 1
        xml = ('<Slot MinInstances="1" MaxInstances="1"><Name>A</Name><ModuleIdent>#x20</ModuleIdent></Slot>'
               '<Slot MinInstances="1" MaxInstances="1" SlotPdoIncrement="4"><Name>B</Name>'
               '<ModuleIdent>#x10</ModuleIdent></Slot>')
        L = slots.layout(device(xml).slots, [0x20, 0x10])
        self.assertEqual([p.index for p in L.pdos], [0x1600, 0x1A04])

    def test_modules_that_fit_no_slot(self):
        xml = ('<Slot MinInstances="1" MaxInstances="1"><Name>A</Name><ModuleIdent>#x10</ModuleIdent></Slot>'
               '<Slot MinInstances="0" MaxInstances="1"><Name>B</Name><ModuleIdent>#x20</ModuleIdent></Slot>')
        L = slots.layout(device(xml).slots, [0x20, 0x10])
        self.assertTrue(any("needs a module, but 0x00000020" in t for t in L.problems), L.problems)
        self.assertTrue(any("fits no slot" in t for t in L.problems), L.problems)

    def test_unknown_module(self):
        L = slots.layout(device(FOUR).slots.__class__(slots=device(FOUR).slots.slots + [], modules={},
                                                       pdo_increment=1, index_increment=0x10), [0x10])
        self.assertTrue(any("not in the ESI file's Modules" in t for t in L.problems))

    def test_empty_modules_map_nothing(self):
        self.assertEqual(slots.layout(device(FOUR).slots, [0, 0]).pdos, [])


def proposal(dev, modules):
    s = sii.Sii(vendor_id=9, product_code=1, revision=1, serial=0, mailbox_protocols=0x04, name="Coupler")
    return propose.propose({"position": 0, "dc": False}, s, coe.Dictionary(), device_type=5001,
                           esi_dev=dev, esi_exact=True, modules=modules)


class TestProposal(unittest.TestCase):
    def test_the_modules_pdos_settings_and_list(self):
        p = proposal(device(FOUR), [0x10, 0x20])
        self.assertEqual([c.index for c in p.pdos], [0x1A00, 0x1601])
        s = config.slave_from_proposal(p)
        objs = [st["object"] for st in s["settings"]]
        self.assertEqual(objs, ["0x8000:01", "0xF030:00", "0xF030:01", "0xF030:02", "0xF030:00"])
        self.assertEqual(config.check(config.from_proposals([p])), [])
        self.assertTrue(any("slot 1 (IO slot): module 0x00000020 DO4" in n for n in p.notes))

    def test_without_the_module_list(self):
        p = proposal(device(FOUR), None)
        self.assertEqual(p.pdos, [])
        self.assertTrue(any("module list (0xF050) was not read" in n for n in p.notes))


class TestSession(unittest.TestCase):
    def test_modules_are_read_before_the_proposal(self):
        from ecatsetup import session, sii as sii_mod, tool
        dev = device(FOUR)
        the_sii = sii_mod.Sii(vendor_id=9, product_code=1, revision=1, serial=0, mailbox_protocols=0x04)
        values = {(0x1000, 0): 5001, (0xF050, 0): 2, (0xF050, 1): 0x10, (0xF050, 2): 0x20}
        info = {"position": 0, "dc": False, "vendor_id": 9, "product_code": 1, "revision": 1, "serial": 0,
                "state": "PREOP", "al_status_code": None}
        with mock.patch.object(tool, "slave_info", lambda pos: info), \
                mock.patch.object(tool, "sii", lambda pos: b""), \
                mock.patch.object(sii_mod, "parse", lambda data: the_sii), \
                mock.patch.object(tool, "sdos_settled", lambda pos, **kw: ""), \
                mock.patch.object(tool, "upload", lambda pos, index, sub, dtype: values.get((index, sub))):
            _info, _coe, p = session.read_slave(0, [dev], "csp")
        self.assertEqual(p.modules, [0x10, 0x20])
        self.assertEqual([c.index for c in p.pdos], [0x1A00, 0x1601])


if __name__ == "__main__":
    unittest.main()
