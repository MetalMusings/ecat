"""Tests of the ecat setup prototype, without a bus.

Run with: python3 -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ecatsetup import update, diagnose, coe, config, esi, importxml, lcecxml, propose, sii  # noqa: E402

DATA = os.path.join(os.path.dirname(__file__), "data")


def easercat():
    with open(os.path.join(DATA, "easercat7000.sii"), "rb") as f:
        s = sii.parse(f.read())
    with open(os.path.join(DATA, "easercat7000-sdos.txt")) as f:
        d = coe.Dictionary(f.read())
    return s, d


class TestSii(unittest.TestCase):
    def test_identity_and_names(self):
        s, _ = easercat()
        self.assertEqual((s.vendor_id, s.product_code, s.revision), (0xD0BED1D0, 0x7000, 2))
        self.assertEqual(s.name, "MetalMusings EaserCAT 7000")
        self.assertTrue(s.has_coe)

    def test_sync_managers_and_pdos(self):
        s, _ = easercat()
        self.assertEqual([m.type for m in s.sync_managers],
                         ["mailbox out", "mailbox in", "outputs", "inputs"])
        pdos = {p.index: p for p in s.pdos}
        self.assertEqual(pdos[0x1A01].sm, 3)
        self.assertEqual([e.name for e in pdos[0x1A01].entries][:2], ["RawData", "CalculatedVoltage"])
        self.assertEqual(pdos[0x1A01].entries[1].data_type, "REAL")
        # This slave's SII gives subindex 0 for every entry of a record.
        self.assertEqual({e.subindex for e in pdos[0x1A01].entries}, {0})


class TestCoe(unittest.TestCase):
    def test_dictionary(self):
        _, d = easercat()
        self.assertEqual(d.objects[0x2000], "Settings_Unit1")
        self.assertEqual([e.subindex for e in d.subentries(0x6001)], [1, 2, 3, 4, 5])
        self.assertEqual(d.name(0x6001, 2), "Out_Unit1: CalculatedVoltage")


class TestPropose(unittest.TestCase):
    def test_mdp_slave(self):
        s, d = easercat()
        p = propose.propose({"position": 3, "dc": False}, s, d, device_type=5001)
        self.assertEqual(p.profile, 5001)
        entries = {(e.index, e.subindex): e for pd in p.pdos for e in pd.entries}
        # Subindexes corrected from the CoE dictionary.
        self.assertIn((0x6001, 2), entries)
        self.assertEqual(entries[(0x6001, 2)].pin, "out-unit1.calculated-voltage")
        self.assertEqual(entries[(0x6001, 2)].hal_type, "float")
        self.assertEqual(entries[(0x7000, 0)].pin, "output4")
        self.assertTrue(any("subindexes taken from the CoE dictionary" in n for n in p.notes))
        self.assertIsNone(p.dc_assign)

    def test_optional_dc_is_not_used(self):
        # An I/O terminal such as the EL5101 lists free run first, DC as an option.
        s, d = easercat()
        s.dc_modes = [sii.DcMode("Synchron", 0, 0, 0, 0), sii.DcMode("DC", 0x320, 0, 0, 1)]
        p = propose.propose({"position": 3, "dc": True}, s, d, device_type=5001)
        self.assertIsNone(p.dc_assign)
        self.assertTrue(any("DC is optional" in n for n in p.notes))
        self.assertEqual(len(p.settings), 10)

    def cia402_sii(self, tx_entries):
        s = sii.Sii(0xA88, 0xA880002, 0x202, 0, 0x04, name="Drive")
        s.pdos = [
            sii.Pdo(0x1600, "rx", 2, "Receive PDO 1", False, False, [
                sii.PdoEntry(0x6040, 0, "Control Word", "UINT", 16),
                sii.PdoEntry(0x6060, 0, "Modes of Operation", "SINT", 8),
                sii.PdoEntry(0x607A, 0, "Target Position", "DINT", 32)]),
            sii.Pdo(0x1A00, "tx", 3, "Transmit PDO 1", False, False, tx_entries),
        ]
        s.dc_modes = [sii.DcMode("DC", 0x300, 0, 0, 1), sii.DcMode("Synchron", 0, 0, 0, 1)]
        return s

    def test_cia402_drive(self):
        s = self.cia402_sii([sii.PdoEntry(0x6041, 0, "Status Word", "UINT", 16),
                             sii.PdoEntry(0x6064, 0, "Position Actual Value", "DINT", 32)])
        p = propose.propose({"position": 0, "dc": True}, s, coe.Dictionary(),
                            device_type=0x00040192, drive_modes=0x1AD)
        self.assertEqual(p.profile, 402)
        self.assertEqual(p.drive_modes, ["PP", "PV", "TQ", "HM", "CSP", "CSV"])
        roles = {e.index: (e.role, e.pin) for pd in p.pdos for e in pd.entries}
        self.assertEqual(roles[0x6040], ("profile", ""))
        self.assertEqual(roles[0x607A], ("profile", ""))
        self.assertEqual(roles[0x6064], ("profile", ""))
        self.assertEqual((p.dc_assign, p.dc_sync0_factor), (0x300, 1))
        self.assertEqual(p.notes, [])

    def test_cia402_missing_entries(self):
        s = self.cia402_sii([sii.PdoEntry(0x6041, 0, "Status Word", "UINT", 16)])
        p = propose.propose({"position": 0, "dc": True}, s, coe.Dictionary(), device_type=0x192)
        self.assertTrue(any("lack 0x6064" in n for n in p.notes))

    def test_kebab(self):
        self.assertEqual(propose.kebab("LowpassFilteredVoltage"), "lowpass-filtered-voltage")
        self.assertEqual(propose.kebab("Out_Unit1"), "out-unit1")
        self.assertEqual(propose.kebab("Target Position"), "target-position")


ESI = """<?xml version="1.0"?>
<EtherCATInfo>
  <Vendor><Id>#x0A88</Id></Vendor>
  <Descriptions><Devices>
    <Device>
      <Type ProductCode="#x0A880002" RevisionNo="#x00000201">ECT60</Type>
      <Name>ECT60</Name>
      <Profile><ProfileNo>402</ProfileNo></Profile>
      <RxPdo Sm="2" Fixed="1"><Index>#x1600</Index><Name>Outputs</Name>
        <Entry><Index>#x6040</Index><SubIndex>0</SubIndex><BitLen>16</BitLen>
          <Name>Control Word</Name><DataType>UINT</DataType></Entry>
      </RxPdo>
      <Dc><OpMode><Name>DC</Name><Desc>DC sync</Desc><AssignActivate>#x0300</AssignActivate>
        <CycleTimeSync0 Factor="1">0</CycleTimeSync0><ShiftTimeSync0>0</ShiftTimeSync0></OpMode></Dc>
    </Device>
  </Devices></Descriptions>
</EtherCATInfo>
"""


ENCODER_ESI = """<?xml version="1.0"?>
<EtherCATInfo><Vendor><Id>2</Id></Vendor><Descriptions><Devices>
<Device><Type ProductCode="#x13ed3052" RevisionNo="#x03ff0000">EL5101</Type><Name>Encoder</Name>
 <Info><VendorSpecific><TwinCAT>
  <AlternativeSmMapping><Name>Legacy</Name><Sm No="3"><Pdo>#x1a00</Pdo></Sm></AlternativeSmMapping>
  <AlternativeSmMapping Default="1"><Name>Standard 16 Bit</Name><Sm No="3"><Pdo>#x1a03</Pdo></Sm></AlternativeSmMapping>
  <AlternativeSmMapping><Name>Standard 32 Bit</Name><Sm No="3"><Pdo>#x1a04</Pdo></Sm></AlternativeSmMapping>
 </TwinCAT></VendorSpecific></Info>
 <TxPdo Sm="3" Fixed="1"><Index>#x1a00</Index><Name>Inputs</Name>
  <Entry><Index>#x6000</Index><SubIndex>2</SubIndex><BitLen>16</BitLen><Name>Value</Name><DataType>UINT</DataType></Entry></TxPdo>
 <TxPdo Fixed="1"><Index>#x1a03</Index><Name>ENC Status compact</Name>
  <Entry><Index>#x6010</Index><SubIndex>17</SubIndex><BitLen>16</BitLen><Name>Counter value</Name><DataType>UINT</DataType></Entry></TxPdo>
 <TxPdo Fixed="1"><Index>#x1a04</Index><Name>ENC Status</Name>
  <Entry><Index>#x6010</Index><SubIndex>1</SubIndex><BitLen>1</BitLen><Name>Status__Latch C valid</Name><DataType>BOOL</DataType></Entry>
  <Entry><Index>#x6010</Index><SubIndex>17</SubIndex><BitLen>32</BitLen><Name>Counter value</Name><DataType>UDINT</DataType></Entry></TxPdo>
</Device></Devices></Descriptions></EtherCATInfo>
"""


class TestEsi(unittest.TestCase):
    def test_predefined_pdo_assignments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "enc.xml")
            with open(path, "w") as f:
                f.write(ENCODER_ESI)
            dev = esi.load_file(path)[0]
        self.assertEqual([(s.name, s.default) for s in dev.pdo_sets],
                         [("Legacy", False), ("Standard 16 Bit", True), ("Standard 32 Bit", False)])
        s = sii.Sii(2, 0x13ED3052, 0x03FF0000, 0, 0, name="EL5101")
        info = {"position": 3, "dc": False}
        p = propose.propose(info, s, coe.Dictionary(), esi_dev=dev, esi_exact=True)
        self.assertEqual([pd.index for pd in p.pdos], [0x1A03])          # the ESI's default
        self.assertEqual(len([n for n in p.notes if "16-bit counter" in n]), 1)
        p = propose.propose(info, s, coe.Dictionary(), esi_dev=dev, esi_exact=True,
                            pdo_set="standard 32 bit")
        self.assertEqual([(pd.sm, pd.index) for pd in p.pdos], [(3, 0x1A04)])
        # an MDP 511 encoder channel: the encoder function handles it
        self.assertEqual(p.encoders, [("enc", 0x6010)])
        self.assertEqual([e.role for e in p.pdos[0].entries], ["encoder", "encoder"])
        s = config.slave_from_proposal(p)
        self.assertEqual(s["encoders"], [{"name": "enc", "index": "0x6010"}])
        self.assertEqual(config.check({"ecat": 1, "master": {}, "slaves": [s]}), [])

    def test_encoder_rules(self):
        def slave(entries, encoders=({"name": "enc", "index": "0x6000"},)):
            return {"ecat": 1, "master": {}, "slaves": [{
                "position": 0, "name": "e", "identity": {"vendor": "0x2", "product": "0x13ed3052"},
                "profile": "mdp", "encoders": list(encoders),
                "process-data": {"inputs": [{"pdo": "0x1A04", "sm": 3, "entries": entries}]}}]}
        counter = {"object": "0x6000:11", "bits": 32, "type": "UDINT", "use": "profile"}
        self.assertEqual(config.check(slave([counter])), [])
        pinned = dict(counter, pin="count", **{"hal-type": "s32"})
        del pinned["use"]
        self.assertTrue(any("handled by encoder enc" in e for e in config.check(slave([pinned]))))
        valid = {"object": "0x6000:01", "bits": 1, "type": "BOOL", "use": "profile"}
        self.assertTrue(any("needs 0x6000:11" in e for e in config.check(slave([valid]))))
        self.assertTrue(any("status index" in e for e in config.check(
            slave([counter], [{"name": "enc", "index": "0x6001"}]))))
        self.assertTrue(any("only for objects" in e for e in config.check(slave([counter], []))))

    def test_sync_manager_rules(self):
        def slave(out_sm, in_sm):
            def pdo(index, sm, obj, pin):
                return {"pdo": index, "sm": sm, "entries": [
                    {"object": obj, "bits": 16, "type": "UINT", "pin": pin, "hal-type": "u32"}]}
            return {"ecat": 1, "master": {}, "slaves": [{
                "position": 0, "name": "s", "identity": {"vendor": "0x2", "product": "0x1"},
                "profile": "generic", "process-data": {
                    "outputs": [pdo("0x1600", out_sm, "0x7000:01", "a"), pdo("0x1601", out_sm, "0x7010:01", "b")],
                    "inputs": [pdo("0x1A00", in_sm, "0x6000:01", "c")]}}]}
        self.assertEqual(config.check(slave(2, 3)), [])
        # as the component: the inputs on sm 2 would be left out
        errors = config.check(slave(2, 2))
        self.assertEqual([e for e in errors if "sm 2 is used for outputs and inputs" in e], [errors[0]])
        self.assertEqual(len(errors), 1)
        self.assertTrue(any("sm must be 0 to 255" in e for e in config.check(slave(2, 256))))
        self.assertTrue(any("sm must be 0 to 255" in e for e in config.check(slave(None, 3))))

    def test_a_pulse_counter_is_not_an_encoder(self):
        # EL2521: a "Counter value" without "Latch C valid"
        e = [propose.Entry(0x6020, 0x11, 32, "Counter value", "UDINT", "pin"),
             propose.Entry(0x6020, 0x03, 1, "Set counter done", "BOOL", "pin")]
        self.assertEqual(propose.encoder_channels(e), [])

    def test_parse_and_find(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ect.xml")
            with open(path, "w") as f:
                f.write(ESI)
            with open(os.path.join(tmp, "broken.xml"), "w") as f:
                f.write("<EtherCATInfo><Vendor>")
            devices = esi.load_dirs([tmp])
        self.assertEqual(len(devices), 1)
        d = devices[0]
        self.assertEqual((d.vendor_id, d.product_code, d.profile), (0xA88, 0xA880002, 402))
        self.assertEqual(d.pdos[0].entries[0].index, 0x6040)
        self.assertEqual(d.dc_modes[0].assign_activate, 0x300)
        found, exact = esi.find(devices, 0xA88, 0xA880002, 0x202)
        self.assertIs(found, d)
        self.assertFalse(exact)   # revision 0x201 against 0x202


LCEC = """<masters>
  <master idx="0" appTimePeriod="1000000" refClockSyncCycles="-1000">
    <slave idx="0" type="generic" vid="00000a88" pid="0a880002" configPdos="true">
      <dcConf assignActivate="300" sync0Cycle="*1" sync0Shift="0"/>
      <sdoConfig idx="2000" subIdx="0"><sdoDataRaw data ="a0 0f"/></sdoConfig>
      <syncManager idx="2" dir="out">
        <pdo idx="1600">
          <pdoEntry idx="607A" subIdx="00" bitLen="32" halPin="target-position" halType="s32"/>
          #<pdoEntry idx="60FF" subIdx="00" bitLen="32" halPin="target-velocity" halType="s32"/>
        </pdo>
      </syncManager>
    </slave>
  </master>
</masters>
"""


class TestLcecXml(unittest.TestCase):
    def test_load(self):
        with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False) as f:
            f.write(LCEC)
        try:
            cfg = lcecxml.load(f.name)
        finally:
            os.unlink(f.name)
        s = cfg.slaves[0]
        self.assertEqual((s.vendor_id, s.product_code, s.assign_activate), (0xA88, 0xA880002, 0x300))
        self.assertEqual(s.sdos, [(0x2000, 0, b"\xa0\x0f")])
        # "#" is not an XML comment: lcec maps the entry, and so does the reader.
        self.assertEqual([e.index for e in s.pdos[(2, 0x1600)]], [0x607A, 0x60FF])


class TestConfig(unittest.TestCase):
    def easercat_config(self):
        s, d = easercat()
        p = propose.propose({"position": 3, "dc": False}, s, d, device_type=5001)
        return config.from_proposals([p], "enp2s0", "xdp-native")

    def test_round_trip(self):
        cfg = self.easercat_config()
        self.assertEqual(config.check(cfg), [])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ethercat.yaml")
            config.write(cfg, path)
            self.assertEqual(config.load(path), cfg)
        self.assertEqual(cfg["slaves"][0]["name"], "io-3")
        self.assertEqual(cfg["slaves"][0]["profile"], "mdp")

    def test_master_dc_with_dc_slaves(self):
        self.assertNotIn("dc", self.easercat_config()["master"])
        s, d = easercat()
        p = propose.propose({"position": 3, "dc": False}, s, d, device_type=5001)
        p.dc_assign, p.dc_sync0_factor, p.dc_shift = 0x300, 1, 0
        cfg = config.from_proposals([p], "enp2s0", "xdp-native", 1000000)
        self.assertEqual(cfg["master"]["dc"], {"ref-clock-sync-cycles": 1000,
                                               "sync-to-ref-clock": True,
                                               "sync0-shift-ns": 300000})

    def test_check_finds_errors(self):
        cfg = self.easercat_config()
        s = cfg["slaves"][0]
        cfg["slaves"].append(dict(s))
        entries = s["process-data"]["inputs"][0]["entries"]
        entries[0]["split"] = ["a"] * 40
        entries[0]["scale"] = 2.0
        s["settings"] = [{"object": "0x2000:01"}]
        errors = "\n".join(config.check(cfg))
        self.assertIn("position used twice", errors)
        self.assertIn("name 'io-3' used twice", errors)
        self.assertIn("split has more pins than bits", errors)
        self.assertIn("scale and offset need", errors)
        self.assertIn("neither value nor data", errors)


class TestImport(unittest.TestCase):
    def test_generic_slave(self):
        xml = LCEC.replace("<master idx", '<master interface="enp2s0" globalShiftTime="300000" idx')
        xml = xml.replace("</syncManager>", """</syncManager>
      <syncManager idx="3" dir="in">
        <pdo idx="1a00">
          <pdoEntry idx="6000" subIdx="00" bitLen="8" halType="complex">
            <complexEntry bitLen="1" halPin="in-1" halType="bit"/>
            <complexEntry bitLen="1"/>
            <complexEntry bitLen="1" halPin="in-3" halType="bit"/>
            <complexEntry bitLen="5"/>
          </pdoEntry>
          <pdoEntry idx="6001" subIdx="01" bitLen="16" halPin="analog" halType="float" scale="0.5"/>
        </pdo>
      </syncManager>
      <initCmds filename="x.xml"/>""")
        with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False) as f:
            f.write(xml)
        try:
            cfg, notes = importxml.import_lcec(lcecxml.load(f.name), None, 1000000)
        finally:
            os.unlink(f.name)
        self.assertEqual(config.check(cfg), [])
        m = cfg["master"]
        self.assertEqual((m["interface"], m["dc"]["sync0-shift-ns"]), ("enp2s0", 300000))
        self.assertTrue(m["dc"]["sync-to-ref-clock"])          # negative refClockSyncCycles
        s = cfg["slaves"][0]
        self.assertTrue(s["configure-pdos"])
        self.assertEqual(s["dc"], {"assign-activate": "0x0300", "sync0-cycles": 1, "sync0-shift-ns": 0})
        self.assertEqual(s["settings"], [{"object": "0x2000:00", "data": "a0 0f"}])
        inputs = s["process-data"]["inputs"][0]["entries"]
        self.assertEqual(inputs[0]["split"], ["in-1", None, "in-3"])
        self.assertEqual((inputs[1]["type"], inputs[1]["hal-type"], inputs[1]["scale"]),
                         ("INT", "float", 0.5))
        self.assertTrue(any("<initCmds> not imported" in n for n in notes))
        self.assertTrue(any("no transport" in n for n in notes))


class TestIni(unittest.TestCase):
    def test_servo_period(self):
        from ecatsetup.__main__ import servo_period
        with tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False) as f:
            f.write("1# text before the first section\n[EMCMOT]\nSERVO_PERIOD = 1000000 # ns\n")
        try:
            self.assertEqual(servo_period(f.name), 1000000)
        finally:
            os.unlink(f.name)



def slave(pos, name, vendor, product, revision="0x00000001"):
    return {"position": pos, "name": name, "device": "dev %s" % product,
            "identity": {"vendor": vendor, "product": product, "revision": revision},
            "profile": "generic", "configure-pdos": False, "process-data": {},
            "settings": []}


class TestUpdate(unittest.TestCase):
    def setUp(self):
        # the file: the user's names and a setting
        self.old = {"ecat": 1, "master": {"interface": "enp2s0", "transport": "raw"},
                    "slaves": [slave(0, "x-drive", "0x1", "0xA"), slave(1, "y-drive", "0x1", "0xA"),
                               slave(2, "io", "0x2", "0xB")]}
        self.old["slaves"][0]["settings"] = [{"object": "0x2000:00", "type": "UINT", "value": 4000}]

    def fresh(self, *slaves):
        return {"ecat": 1, "master": {"transport": "raw"}, "slaves": list(slaves)}

    def test_unchanged_bus_keeps_the_file(self):
        cfg, notes = update.merge(self.old, self.fresh(
            slave(0, "drive-0", "0x1", "0xA"), slave(1, "drive-1", "0x1", "0xA"),
            slave(2, "io-2", "0x2", "0xB")))
        self.assertEqual(cfg, self.old)
        self.assertEqual(notes, [])

    def test_new_moved_and_missing_slaves(self):
        # a new I/O slave at 0 pushes the drives down; "io" is gone
        cfg, notes = update.merge(self.old, self.fresh(
            slave(0, "io-0", "0x3", "0xC"), slave(1, "drive-1", "0x1", "0xA"),
            slave(2, "drive-2", "0x1", "0xA")))
        self.assertEqual([(s["position"], s["name"]) for s in cfg["slaves"]],
                         [(0, "io-0"), (1, "x-drive"), (2, "y-drive")])
        self.assertEqual(cfg["slaves"][1]["settings"], self.old["slaves"][0]["settings"])
        text = "\n".join(notes)
        self.assertIn("x-drive (dev 0xA): moved from position 0 to 1", text)
        self.assertIn("slave 0: new", text)
        self.assertIn("io (dev 0xB) at position 2: not on the bus", text)

    def test_inserted_slave_moves_nothing_else(self):
        # file: X at 0, Z at 2 (Y was left out); bus: three drives
        del self.old["slaves"][1]
        self.old["slaves"][1]["position"] = 2  # "io"
        self.old["slaves"].insert(1, slave(1, "z-drive", "0x1", "0xA"))
        self.old["slaves"][1]["position"], self.old["slaves"][2]["position"] = 2, 3
        cfg, notes = update.merge(self.old, self.fresh(
            slave(0, "d0", "0x1", "0xA"), slave(1, "d1", "0x1", "0xA"),
            slave(2, "d2", "0x1", "0xA"), slave(3, "i3", "0x2", "0xB")))
        self.assertEqual([s["name"] for s in cfg["slaves"]], ["x-drive", "d1", "z-drive", "io"])
        self.assertEqual(len(notes), 1)

    def test_new_name_does_not_clash(self):
        self.old["slaves"][2]["name"] = "io-3"
        cfg, _ = update.merge(self.old, self.fresh(
            slave(0, "drive-0", "0x1", "0xA"), slave(1, "drive-1", "0x1", "0xA"),
            slave(2, "io-2", "0x2", "0xB"), slave(3, "io-3", "0x3", "0xC")))
        self.assertEqual(cfg["slaves"][3]["name"], "io-3-2")

    def test_revision_and_master_dc(self):
        fresh = self.fresh(slave(0, "drive-0", "0x1", "0xA", "0x00000002"),
                           slave(1, "drive-1", "0x1", "0xA"), slave(2, "io-2", "0x2", "0xB"))
        fresh["master"]["dc"] = {"ref-clock-sync-cycles": 1000, "sync-to-ref-clock": True}
        cfg, notes = update.merge(self.old, fresh)
        self.assertEqual(cfg["slaves"][0]["identity"]["revision"], "0x00000002")
        self.assertEqual(cfg["master"]["interface"], "enp2s0")
        self.assertIn("dc", cfg["master"])
        self.assertEqual(len(notes), 2)



class FakeBus:
    """Registers and SDOs of one slave, as the ethercat tool would give them."""

    def __init__(self, regs, sdos, later=None, slave_line="0  0:0  OP  +  Some Drive"):
        self.regs, self.sdos, self.later, self.reads = regs, sdos, later or {}, 0
        self.slave_line = slave_line
        self.verbose = "State: PREOP\nFlag: E\n"

    def run(self, *args, binary=False):
        if args[0] == "slaves" and "-v" in args:
            return self.verbose
        if args[0] == "slaves":
            return self.slave_line + "\n"
        if args[0] == "diag":
            return "Slave does not provide a diagnosis history (0x10F3).\n"
        if args[0] == "reg_read":
            address, length = int(args[3], 16), int(args[4])
            if address == 0x0300:
                self.reads += 1
            regs = dict(self.regs)
            if address == 0x0300 and self.reads > 1:
                regs.update(self.later)
            return " ".join("0x%02x" % regs.get(address + i, 0) for i in range(length))
        raise AssertionError(args)

    def upload(self, pos, index, subindex, dtype):
        return self.sdos.get(index)


class TestDiagnose(unittest.TestCase):
    def run_diagnose(self, bus):
        lines = []
        with mock.patch.object(diagnose.tool, "run", bus.run), \
                mock.patch.object(diagnose.tool, "upload", bus.upload):
            found = diagnose.diagnose([0], seconds=0, out=lines.append)
        return found, "\n".join(lines)

    def test_state_error_cable_and_drive_fault(self):
        regs = {0x0130: 0x14, 0x0134: 0x1D,          # SAFEOP + error, invalid output configuration
                0x0110: 0x30, 0x0303: 3}             # link on ports 0 and 1; port 1 phy errors
        later = {0x0303: 9}                          # ... which grow
        sdos = {0x1000: 0x00020192, 0x6041: 0x0218, 0x603F: 0x7305, 0x6061: 8, 0x6060: 8}
        found, text = self.run_diagnose(FakeBus(regs, sdos, later))
        self.assertIn("State: SAFEOP, error 0x001D: Invalid output configuration", text)
        self.assertIn("Port 1: link; since power-on: phy 9", text)
        self.assertIn("phy increased by 6", text)
        self.assertIn("CiA 402: Fault (status word 0x0218), mode CSP", text)
        self.assertIn("0x7305 incremental sensor 1 fault", text)
        self.assertEqual(found, 4)  # state, cable, error code, fault

    def test_healthy_slave(self):
        regs = {0x0130: 0x08, 0x0110: 0x10, 0x0301: 255}   # OP; old saturated phy count
        found, text = self.run_diagnose(FakeBus(regs, {}))
        self.assertEqual(found, 0)
        self.assertIn("phy 255+", text)
        self.assertNotIn("->", text)

    def test_error_acknowledged_by_the_master(self):
        regs = {0x0130: 0x02, 0x0110: 0x10}                # PREOP, no error flag, no code
        found, text = self.run_diagnose(FakeBus(regs, {}, slave_line="0  0:0  PREOP  E  Some IO"))
        self.assertEqual(found, 1)
        self.assertIn("only in the master's log", text)

    def test_error_code_kept_by_the_master(self):
        regs = {0x0130: 0x02, 0x0110: 0x10}
        bus = FakeBus(regs, {}, slave_line="0  0:0  PREOP  E  Some IO")
        bus.verbose = "State: PREOP\nFlag: E\nLast AL status code: 0x001d\n"
        found, text = self.run_diagnose(bus)
        self.assertIn("refused with 0x001D: Invalid output configuration", text)
        self.assertIn("the output mapping is not accepted", text)


if __name__ == "__main__":
    unittest.main()

