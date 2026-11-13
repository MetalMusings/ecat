"""Tests of the ecat setup prototype, without a bus.

Run with: python3 -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# quirk files: only those that come with the program
os.environ["ECATSETUP_DEVICES"] = os.path.join(os.path.dirname(__file__), "no-such-dir")
os.environ["ECATSETUP_SETTINGS"] = os.path.join(os.path.dirname(__file__), "no-such-dir", "settings.yaml")
# state (test lock, drive limits): never the user's own
if "ECATSETUP_TEST_STATE" not in os.environ:
    os.environ["ECATSETUP_TEST_STATE"] = os.environ["XDG_STATE_HOME"] = \
        __import__("tempfile").mkdtemp(prefix="ecatsetup-test-state-")

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
    def test_a_legacy_encoder_without_esi_says_what_is_missing(self):
        from ecatsetup import sii
        pdos = [sii.Pdo(0x1600, "rx", 2, "Outputs", False, False, [
                    sii.PdoEntry(0x7000, 1, "Ctrl", "USINT", 8), sii.PdoEntry(0x7000, 2, "Value", "UINT", 16)]),
                sii.Pdo(0x1A00, "tx", 3, "Inputs", False, False, [
                    sii.PdoEntry(0x6000, 1, "Status", "USINT", 8), sii.PdoEntry(0x6000, 2, "Value", "UINT", 16),
                    sii.PdoEntry(0x6000, 3, "Latch", "UINT", 16)])]
        s = sii.Sii(0x2, 0x13ED3052, 0x3F90000, 0, 0x04, name="EL5101", pdos=pdos)
        p = propose.propose({"position": 3, "dc": False}, s, coe.Dictionary(), device_type=5001)
        self.assertEqual(p.encoders, [])
        self.assertTrue(any("no ESI file matches it" in n and "Standard 32 Bit (MDP 511)" in n
                            for n in p.notes), p.notes)

    def test_mdp_slave(self):
        s, d = easercat()
        p = propose.propose({"position": 3, "dc": False}, s, d, device_type=5001)
        self.assertEqual(p.profile, 5001)
        entries = {(e.index, e.subindex): e for pd in p.pdos for e in pd.entries}
        # Subindexes corrected from the CoE dictionary.
        self.assertIn((0x6001, 2), entries)
        self.assertEqual(entries[(0x6001, 2)].pin, "out-unit1.calculated-voltage")
        self.assertEqual(entries[(0x6001, 2)].hal_type, "real")
        # bit names from the shipped quirk file for the EaserCAT 7000
        self.assertEqual(entries[(0x7000, 0)].split, ["out-1", "out-2", "activate-relay", "out-4"])
        self.assertEqual(entries[(0x7000, 0)].pin, "")
        self.assertEqual(len(entries[(0x6000, 0)].split), 12)
        self.assertTrue(any("from the quirk file metalmusings-easercat-7000.yaml" in n for n in p.notes))
        cfg_entry = config._entry(entries[(0x7000, 0)])
        self.assertEqual(cfg_entry["split"][2], "activate-relay")
        self.assertNotIn("pin", cfg_entry)
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

    def analog_sii(self, name):
        s = sii.Sii(0x2, 0x0C1E3052, 0x00140000, 0, 0x04, name=name)
        s.pdos = [
            sii.Pdo(0x1A00, "tx", 3, "AI Standard Channel 1", False, False, [
                sii.PdoEntry(0x6000, 1, "Status__Underrange", "BOOL", 1),
                sii.PdoEntry(0x6000, 7, "Status__Error", "BOOL", 1),
                sii.PdoEntry(0x6000, 0x11, "Value", "INT", 16)]),
            sii.Pdo(0x1600, "rx", 2, "AO Outputs Channel 1", False, False, [
                sii.PdoEntry(0x7000, 0x11, "Analog output", "INT", 16)]),
        ]
        return s

    def test_stepper_terminal(self):
        s = sii.Sii(0x2, 0x1B813052, 0x00100000, 0, 0x04, name="EL7041 1Ch. Stepper motor output stage (50V, 5A)")
        s.pdos = [
            sii.Pdo(0x1602, "rx", 2, "STM RxPDO-Map Control", False, False, [
                sii.PdoEntry(0x7010, 0x01, "Enable", "BOOL", 1),
                sii.PdoEntry(0x7010, 0x02, "Reset", "BOOL", 1),
                sii.PdoEntry(0x7010, 0x03, "Reduce torque", "BOOL", 1),
                sii.PdoEntry(0, 0, "", "", 13)]),
            sii.Pdo(0x1604, "rx", 2, "STM RxPDO-Map Velocity", False, False, [
                sii.PdoEntry(0x7010, 0x21, "Velocity", "INT", 16)]),
            sii.Pdo(0x1A03, "tx", 3, "STM TxPDO-Map Status", False, False, [
                sii.PdoEntry(0x6010, 0x01, "Status__Ready to enable", "BOOL", 1),
                sii.PdoEntry(0x6010, 0x04, "Status__Error", "BOOL", 1)]),
        ]
        p = propose.propose({"position": 1, "dc": False}, s, coe.Dictionary(), device_type=5001)
        e = {(x.index, x.subindex): x for pd in p.pdos for x in pd.entries}
        self.assertEqual([(e[(0x7010, i)].pin, e[(0x7010, i)].hal_type) for i in (1, 2, 3)],
                         [("enable", "bool"), ("reset", "bool"), ("reduce-torque", "bool")])
        v = e[(0x7010, 0x21)]
        self.assertEqual((v.pin, v.hal_type, v.scale), ("velocity", "real", 32767.0))
        self.assertEqual(e[(0x6010, 4)].pin, "error")
        self.assertTrue(any("from the quirk file beckhoff-el7041.yaml" in n for n in p.notes), p.notes)
        slave = config.slave_from_proposal(p)
        cfg = {"ecat": 1, "master": {"interface": "x", "transport": "raw"}, "slaves": [slave]}
        self.assertEqual(config.check(cfg), [])

    def test_read_modules(self):
        from unittest import mock
        from ecatsetup import session, tool
        c = coe.Dictionary()
        c.objects = {0xF050: "Detected Module Ident List"}
        vals = {(0xF050, 0): 2, (0xF050, 1): 0x101, (0xF050, 2): 0x202}
        with mock.patch.object(tool, "upload", lambda pos, i, sub, t: vals.get((i, sub))):
            self.assertEqual(session.read_modules(3, c, 0xF050), [0x101, 0x202])
            self.assertIsNone(session.read_modules(3, c, 0xF030))     # not in the dictionary
            del vals[(0xF050, 2)]
            self.assertIsNone(session.read_modules(3, c, 0xF050))     # a sub could not be read

    def test_modular_device(self):
        p = propose.Proposal(1, 2, 3, 0, 0, "Coupler")
        propose.add_modules(p, [0x101, 0x202], [0x101])
        self.assertEqual(p.modules, [0x101, 0x202])
        self.assertIn("modular device: 2 modules detected (0xF050: 0x00000101, 0x00000202)",
                      p.notes[0])
        self.assertIn("differ from those detected", p.notes[1])
        self.assertEqual(config.slave_from_proposal(p)["modules"], ["0x00000101", "0x00000202"])
        q = propose.Proposal(1, 2, 3, 0, 0, "Terminal")
        propose.add_modules(q, None, None)
        self.assertIsNone(q.modules)
        self.assertNotIn("modules", config.slave_from_proposal(q))

    def test_analog_range(self):
        self.assertEqual(propose.analog_range("EL3102 2K. Ana. Eingang +/-10V, DIFF"),
                         (-10.0, 10.0, "V"))
        self.assertEqual(propose.analog_range("EL3052 2Ch. Ana. Input 4-20mA"), (4.0, 20.0, "mA"))
        self.assertEqual(propose.analog_range("EL4002 0..10 V"), (0.0, 10.0, "V"))
        self.assertEqual(propose.analog_range("±75 mV"), (-75.0, 75.0, "mV"))
        self.assertIsNone(propose.analog_range("EL3202 PT100 (RTD)"))
        self.assertIsNone(propose.analog_range("EL1008 8Ch. Dig. Input 24V, 3ms"))
        # raw 0x7FFF is the top; 0 the bottom of 4..20 mA, or 0 of +/-10 V
        sc, off = propose.analog_scaling((4.0, 20.0, "mA"), output=False)
        self.assertAlmostEqual(0x7FFF * sc + off, 20.0)
        self.assertAlmostEqual(0 * sc + off, 4.0)
        sc, off = propose.analog_scaling((4.0, 20.0, "mA"), output=True)
        self.assertAlmostEqual((12.0 + off) * sc, 0x7FFF / 2)
        sc, off = propose.analog_scaling((-10.0, 10.0, "V"), output=False)
        self.assertAlmostEqual(-0x7FFF * sc + off, -10.0)

    def test_analog_terminal(self):
        p = propose.propose({"position": 2, "dc": False},
                            self.analog_sii("EL3102 2Ch. Ana. Input +/-10V, Diff."),
                            coe.Dictionary(), device_type=5001)
        e = {(x.index, x.subindex): x for pd in p.pdos for x in pd.entries}
        v, out = e[(0x6000, 0x11)], e[(0x7000, 0x11)]
        self.assertEqual((v.pin, v.hal_type), ("value", "real"))
        self.assertAlmostEqual(v.scale, 10.0 / 0x7FFF)
        self.assertEqual(out.hal_type, "real")
        self.assertAlmostEqual(out.scale, 0x7FFF / 10.0)
        self.assertEqual(e[(0x6000, 7)].pin, "error")
        self.assertTrue(any("value: analog input, -10..10 V" in n for n in p.notes))
        s = config.slave_from_proposal(p)
        entry = s["process-data"]["inputs"][0]["entries"][2]
        self.assertEqual((entry["hal-type"], round(entry["scale"], 9)), ("real", round(10 / 0x7FFF, 9)))
        self.assertNotIn("offset", entry)
        cfg = {"ecat": 1, "master": {"interface": "x", "transport": "raw"}, "slaves": [s]}
        self.assertEqual(config.check(cfg), [])
        # no range in the name: raw counts, with a note
        p = propose.propose({"position": 2, "dc": False}, self.analog_sii("Analog box"),
                            coe.Dictionary(), device_type=5001)
        v = next(x for pd in p.pdos for x in pd.entries if x.subindex == 0x11)
        self.assertEqual(v.hal_type, "sint")
        self.assertTrue(any("raw counts" in n for n in p.notes))

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

    def test_digital_outputs_are_a_bit_pattern(self):
        # the digital outputs are bits, not a number: a value too wide is cut, not held at the top
        s = sii.Sii(0xA88, 0xA880010, 1, 0, 0x04, name="DRV")
        s.pdos = [
            sii.Pdo(0x1600, "rx", 2, "RxPDO", False, False, [
                sii.PdoEntry(0x6040, 0, "Control Word", "UINT", 16),
                sii.PdoEntry(0x607A, 0, "Target Position", "DINT", 32),
                sii.PdoEntry(0x60FE, 1, "Digital Outputs", "UDINT", 32)]),
            sii.Pdo(0x1A00, "tx", 3, "TxPDO", False, False, [
                sii.PdoEntry(0x6041, 0, "Status Word", "UINT", 16),
                sii.PdoEntry(0x6064, 0, "Position Actual Value", "DINT", 32),
                sii.PdoEntry(0x60FD, 0, "Digital Inputs", "UDINT", 32)])]
        p = propose.propose({"position": 0, "dc": True}, s, coe.Dictionary(), device_type=0x192)
        slave = config.slave_from_proposal(p, "d")
        entries = {e["object"]: e for k in ("outputs", "inputs")
                   for pdo in slave["process-data"][k] for e in pdo["entries"]}
        out = entries["0x60FE:01"]
        self.assertEqual((out["pin"], out["hal-type"], out.get("raw")), ("digital-outputs", "uint", True))
        self.assertNotIn("raw", entries["0x60FD:00"])
        self.assertEqual(config.check({"ecat": 1, "master": {}, "slaves": [slave]}), [])

    def test_two_axes(self):
        s = sii.Sii(0xA88, 0xA880010, 1, 0, 0x04, name="ECT60x2")
        s.pdos = [
            sii.Pdo(0x1600, "rx", 2, "Receive PDO 1", False, False, [
                sii.PdoEntry(0x6040, 0, "Control Word", "UINT", 16),
                sii.PdoEntry(0x607A, 0, "Target Position", "DINT", 32),
                sii.PdoEntry(0x6840, 0, "Control Word", "UINT", 16),
                sii.PdoEntry(0x687A, 0, "Target Position", "DINT", 32)]),
            sii.Pdo(0x1A00, "tx", 3, "Transmit PDO 1", False, False, [
                sii.PdoEntry(0x6041, 0, "Status Word", "UINT", 16),
                sii.PdoEntry(0x6064, 0, "Position Actual Value", "DINT", 32),
                sii.PdoEntry(0x60FD, 0, "Digital Inputs", "UDINT", 32),
                sii.PdoEntry(0x6841, 0, "Status Word", "UINT", 16),
                sii.PdoEntry(0x6864, 0, "Position Actual Value", "DINT", 32),
                sii.PdoEntry(0x68FD, 0, "Digital Inputs", "UDINT", 32)]),
        ]
        p = propose.propose({"position": 0, "dc": True}, s, coe.Dictionary(), device_type=0x192)
        self.assertEqual(p.axes, 2)
        roles = {e.index: (e.role, e.pin) for pd in p.pdos for e in pd.entries}
        self.assertEqual(roles[0x6840], ("profile", ""))
        self.assertEqual(roles[0x6864], ("profile", ""))
        self.assertEqual(roles[0x60FD], ("pin", "digital-inputs-1"))
        self.assertEqual(roles[0x68FD], ("pin", "digital-inputs-2"))
        slave = config.slave_from_proposal(p, "xy")
        self.assertEqual(slave["cia402"], {"mode": "csp", "axes": ["xy-1", "xy-2"]})
        cfg = {"ecat": 1, "master": {"interface": "x", "transport": "raw"}, "slaves": [slave]}
        self.assertEqual(config.check(cfg), [])
        self.assertEqual(config.axes(slave), ["xy-1", "xy-2"])
        # each axis needs its objects; names must not clash
        out = slave["process-data"]["inputs"][0]["entries"]
        del out[3]                                 # 0x6841
        self.assertIn("slave 0: the cia402 profile needs 0x6841:00 in the process data",
                      config.check(cfg))
        slave["cia402"]["axes"] = ["xy", "xy-2"]
        self.assertTrue(any("axis name 'xy' is used" in e for e in config.check(cfg)))
        slave["cia402"]["axes"] = ["a", "b", "c", "d", "e"]
        self.assertTrue(any("axes must be a list of 1 to 4" in e for e in config.check(cfg)))

    def test_cia402_missing_entries(self):
        s = self.cia402_sii([sii.PdoEntry(0x6041, 0, "Status Word", "UINT", 16)])
        p = propose.propose({"position": 0, "dc": True}, s, coe.Dictionary(), device_type=0x192)
        self.assertTrue(any("lack 0x6064" in n for n in p.notes))

    def test_cia402_modes(self):
        def drive(cia402, with_mode_objects=True):
            def e(obj, bits, t):
                return {"object": obj, "bits": bits, "type": t, "use": "profile"}
            out = [e("0x6040:00", 16, "UINT"), e("0x607A:00", 32, "DINT")]
            inp = [e("0x6041:00", 16, "UINT"), e("0x6064:00", 32, "DINT")]
            if with_mode_objects:
                out.append(e("0x6060:00", 8, "SINT"))
                inp.append(e("0x6061:00", 8, "SINT"))
            return {"ecat": 1, "master": {}, "slaves": [{
                "position": 0, "name": "x", "identity": {"vendor": "0xa88", "product": "0xa880002"},
                "profile": "cia402", "cia402": cia402, "process-data": {
                    "outputs": [{"pdo": "0x1600", "sm": 2, "entries": out}],
                    "inputs": [{"pdo": "0x1A00", "sm": 3, "entries": inp}]}}]}
        self.assertEqual(config.check(drive({"mode": "csp", "modes": ["hm"]})), [])
        self.assertEqual(config.check(drive({"mode": "csp"}, False)), [])
        errors = config.check(drive({"mode": "csp", "modes": ["hm"]}, False))
        self.assertEqual(len(errors), 2)
        self.assertTrue(all("changing modes needs 0x606" in e for e in errors))
        self.assertTrue(any("'cst' is not supported" in e for e in config.check(drive({"mode": "csp", "modes": ["cst"]}))))
        # other modes need their objects, and PP its limits
        self.assertTrue(any("needs 0x60FF" in e for e in config.check(drive({"mode": "csp", "modes": ["pv"]}))))
        self.assertTrue(any("needs the setting 0x607D:01" in e
                            for e in config.check(drive({"mode": "csp", "modes": ["pp"]}))))
        self.assertTrue(any("must be a list" in e for e in config.check(drive({"mode": "csp", "modes": "hm"}))))
        self.assertEqual(config.check(drive({"mode": "csp", "zero-at-start": True})), [])
        self.assertTrue(any("zero-at-start must be true or false" in e
                            for e in config.check(drive({"mode": "csp", "zero-at-start": "yes"}))))
        # PV needs the velocity objects, not the position ones
        pv = drive({"mode": "pv"})
        self.assertTrue(any("needs 0x60FF" in e for e in config.check(pv)))
        for pdo, obj in (("outputs", "0x60FF:00"), ("inputs", "0x606C:00")):
            pv["slaves"][0]["process-data"][pdo][0]["entries"].append(
                {"object": obj, "bits": 32, "type": "DINT", "use": "profile"})
        self.assertEqual(config.check(pv), [])
        # PP needs the positions and the drive's limits as settings
        pp = drive({"mode": "pp"})
        self.assertEqual(sorted(e.split(": ", 1)[1] for e in config.check(pp)), [
            "cia402 mode pp needs the setting 0x607D:01", "cia402 mode pp needs the setting 0x607D:02",
            "cia402 mode pp needs the setting 0x607F:00"])
        p = propose.Proposal(0, 0xa88, 0xa880002, 0, 0, "ECT60", profile=402, mode="pp")
        p.pp_limits = {(0x607D, 1): -1000, (0x607D, 2): 1000, (0x607F, 0): 500}
        pp["slaves"][0]["settings"] = config.slave_from_proposal(p)["settings"]
        self.assertEqual(config.check(pp), [])
        self.assertEqual(pp["slaves"][0]["settings"][0], {"object": "0x607D:01", "type": "DINT", "value": -1000})

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
        # a counter that wraps at its width goes on counting in 64 bits
        counters = [e for pd in p.pdos for e in pd.entries if e.role == "pin" and e.extend]
        self.assertEqual([(e.bitlen, e.hal_type) for e in counters], [(16, "sint")])
        slave16 = config.slave_from_proposal(p)
        ext = [e for pdo in slave16["process-data"]["inputs"] for e in pdo["entries"] if e.get("extend")]
        self.assertEqual(len(ext), 1)
        self.assertEqual(config.check({"ecat": 1, "master": {}, "slaves": [slave16]}), [])
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
        pinned = dict(counter, pin="count", **{"hal-type": "sint"})
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
                    {"object": obj, "bits": 16, "type": "UINT", "pin": pin, "hal-type": "uint"}]}
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

    def test_hal_types_in_2_10_names_and_the_old_ones_read(self):
        # a file written for 2.9 spells the types bit, s32, u32, float: read as the same types
        text = ("ecat: 1\nmaster: {}\nslaves:\n- position: 0\n  name: s\n"
                "  identity: {vendor: '0x2', product: '0x1'}\n  profile: generic\n"
                "  process-data:\n    inputs:\n    - pdo: '0x1A00'\n      sm: 3\n      entries:\n"
                "      - {object: '0x6000:01', bits: 1, type: BOOL, pin: a, hal-type: bit}\n"
                "      - {object: '0x6000:02', bits: 16, type: INT, pin: b, hal-type: s32}\n"
                "      - {object: '0x6000:03', bits: 16, type: UINT, pin: c, hal-type: u32}\n"
                "      - {object: '0x6000:04', bits: 16, type: INT, pin: d, hal-type: float, scale: 2}\n")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "e.yaml")
            with open(path, "w") as f:
                f.write(text)
            cfg = config.load(path)
        types = [e["hal-type"] for e in cfg["slaves"][0]["process-data"]["inputs"][0]["entries"]]
        self.assertEqual(types, ["bool", "sint", "uint", "real"])
        self.assertEqual(config.check(cfg), [])
        # written with the new names
        self.assertIn("hal-type: sint", config.dumps(cfg))
        self.assertNotIn("s32", config.dumps(cfg))

    def test_extend_and_raw_rules(self):
        def check(entry, kind="inputs"):
            return config.check({"ecat": 1, "master": {}, "slaves": [{
                "position": 0, "name": "s", "identity": {"vendor": "0x2", "product": "0x1"},
                "profile": "generic",
                "process-data": {kind: [{"pdo": "0x1A00", "sm": 3, "entries": [dict(
                    {"object": "0x6000:01", "bits": 32, "type": "UDINT", "pin": "c",
                     "hal-type": "sint"}, **entry)]}]}}]})
        self.assertEqual(check({"extend": True}), [])
        self.assertEqual(check({"bits": 16, "type": "UINT", "extend": True}), [])
        self.assertEqual(check({"extend": False}), [])
        self.assertTrue(any("extend is for an input entry of 8 to 32 bits with hal-type sint" in e
                            for e in check({"extend": True}, "outputs")))
        self.assertTrue(any("extend is for an input" in e for e in check({"hal-type": "uint", "extend": True})))
        self.assertTrue(any("extend is for an input" in e for e in check(
            {"bits": 64, "type": "LINT", "extend": True})))
        self.assertTrue(any("extend is true or false" in e for e in check({"extend": "yes"})))
        # raw: a bit pattern, an unsigned pin of up to 32 bits
        self.assertEqual(check({"hal-type": "uint", "raw": True}, "outputs"), [])
        self.assertTrue(any("raw is for an entry of up to 32 bits with hal-type uint" in e
                            for e in check({"raw": True})))
        self.assertTrue(any("raw is for an entry of up to 32 bits" in e for e in check(
            {"hal-type": "uint", "bits": 64, "type": "ULINT", "raw": True})))
        self.assertTrue(any("raw is true or false" in e for e in check({"raw": 1})))

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
                                               "servo-thread-follows-ref-clock": True,
                                               "sync0-shift-ns": "auto"})
        self.assertEqual(config.check(cfg), [])

    def test_check_assign_activate(self):
        cfg = self.easercat_config()
        slave = cfg["slaves"][0]
        for value in ("", None):
            slave["dc"] = {"assign-activate": value, "sync0-cycles": 1}
            self.assertEqual(config.check(cfg), ["slave 3: DC sync needs an assign-activate value, such as "
                                                 "0x0300 (from the ESI file)"])
        slave["dc"]["assign-activate"] = "SYNC0"
        self.assertEqual(config.check(cfg), ["slave 3: dc assign-activate 'SYNC0' is not a hex number, "
                                             "such as 0x0300"])
        slave["dc"]["assign-activate"] = "0x0300"
        self.assertEqual(config.check(cfg), [])

    def test_check_master_shift(self):
        cfg = self.easercat_config()
        cfg["master"]["dc"] = {"sync0-shift-ns": "auto"}
        self.assertTrue(any("auto needs master.dc.servo-thread-follows-ref-clock" in e for e in config.check(cfg)))
        cfg["master"]["dc"] = {"servo-thread-follows-ref-clock": True, "sync0-shift-ns": "often"}
        self.assertTrue(any("'often' is not a number or auto" in e for e in config.check(cfg)))
        cfg["master"]["dc"] = {"servo-thread-follows-ref-clock": False, "sync0-shift-ns": 300000}
        self.assertEqual(config.check(cfg), [])
        cfg["master"]["dc"] = {"sync-to-ref-clock": True}
        self.assertTrue(any("now called servo-thread-follows-ref-clock" in e for e in config.check(cfg)))

    def test_check_finds_errors(self):
        cfg = self.easercat_config()
        s = cfg["slaves"][0]
        cfg["slaves"].append(dict(s))
        entries = s["process-data"]["inputs"][0]["entries"]
        entries[0]["split"] = ["a"] * 40
        s["process-data"]["inputs"][1]["entries"][0]["scale"] = 2.0
        s["settings"] = [{"object": "0x2000:01"}]
        errors = "\n".join(config.check(cfg))
        self.assertIn("position used twice", errors)
        self.assertIn("name 'io-3' used twice", errors)
        self.assertIn("split has more pins than bits", errors)
        self.assertIn("scale and offset need", errors)
        self.assertIn("neither value nor data", errors)

    def test_invert_is_checked(self):
        cfg = self.easercat_config()
        entries = cfg["slaves"][0]["process-data"]["inputs"][0]["entries"]
        entries[0]["invert"] = ["in-4", "in-7"]
        self.assertFalse(config.check(cfg))
        entries[0]["invert"] = ["in-4", "nope"]
        self.assertTrue(any("invert lists nope, which is not in split" in x
                            for x in config.check(cfg)))
        entries[0]["invert"] = True
        self.assertTrue(any("a list of names for a split" in x for x in config.check(cfg)))
        del entries[0]["invert"]
        other = cfg["slaves"][0]["process-data"]["inputs"][1]["entries"][0]   # a s32 pin
        other["invert"] = True
        self.assertTrue(any("invert is true or false, for a bit pin" in x for x in config.check(cfg)))
        other.update({"hal-type": "bool", "invert": True})
        self.assertFalse(any("invert" in x for x in config.check(cfg)))
        self.assertIn("invert: true", config.dumps(cfg))

    def test_64_bit_entries_are_integer_pins(self):
        # LinuxCNC 2.10 has 64-bit integer pins; real (a float) still carries them to 2^53
        self.assertEqual(propose.HAL_TYPES["LINT"], "sint")
        self.assertEqual(propose.HAL_TYPES["ULINT"], "uint")
        cfg = self.easercat_config()
        e = cfg["slaves"][0]["process-data"]["inputs"][0]["entries"][0]
        e.pop("split", None)
        e.update(bits=64, type="LINT", pin="big", **{"hal-type": "sint"})
        self.assertFalse(any("LINT" in x or "hal-type" in x for x in config.check(cfg)))
        e["hal-type"] = "uint"
        self.assertTrue(any("a LINT entry takes hal-type sint" in x for x in config.check(cfg)))
        e["type"] = "ULINT"
        self.assertFalse(any("LINT" in x or "hal-type" in x for x in config.check(cfg)))
        e["hal-type"] = "sint"
        self.assertTrue(any("a ULINT entry takes hal-type uint" in x for x in config.check(cfg)))
        e["hal-type"] = "s64"
        self.assertTrue(any("unknown hal-type 's64'" in x for x in config.check(cfg)))
        e["hal-type"] = "real"
        self.assertFalse(any("LINT" in x or "hal-type" in x for x in config.check(cfg)))
        cfg["master"]["start-check"] = "yes"
        self.assertIn("master.start-check: must be true or false", config.check(cfg))


class TestImport(unittest.TestCase):
    def load(self, xml, bus=None):
        with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False) as f:
            f.write(xml)
        try:
            return importxml.import_lcec(lcecxml.load(f.name), bus, 1000000)
        finally:
            os.unlink(f.name)

    def test_stepper_modparams(self):
        xml = LCEC.replace('type="generic"', 'type="EL7041"').replace(
            '<dcConf', '<modParam name="maxCurrent" value="2.5"/>\n'
                       '      <modParam name="maxSpeed" value="4000"/>\n'
                       '      <modParam name="microsteps" value="3"/>\n'
                       '      <modParam name="kp" value="1"/>\n'
                       '      <dcConf')
        cfg, notes = self.load(xml)
        rows = {x["object"]: (x.get("type"), x.get("value")) for x in cfg["slaves"][0]["settings"]}
        self.assertEqual(rows["0x8010:01"], ("UINT", 2500))
        self.assertEqual(rows["0x8012:05"], ("UINT", 2))
        self.assertNotIn("0x8012:45", rows)
        self.assertTrue(any("microsteps=3 not imported: 3 is not one of 1, 2, 4" in n for n in notes))
        self.assertTrue(any("kp=1 not imported" in n for n in notes))
        self.assertFalse(any("<modParam> not imported" in n for n in notes))
        # el70x1: raw values, and the position interface
        cfg, notes = self.load(LCEC.replace('type="generic"', 'type="EL7041-0052"').replace(
            '<dcConf', '<modParam name="maxCurrent" value="1500"/>\n      <dcConf'))
        rows = {x["object"]: x.get("value") for x in cfg["slaves"][0]["settings"]}
        self.assertEqual((rows["0x8010:01"], rows["0x8012:01"]), (1500, 3))

    def test_ect60_zero_position_becomes_zero_at_start(self):
        zero = '<sdoConfig idx="2010" subIdx="0"><sdoDataRaw data ="01 00"/></sdoConfig>'
        xml = LCEC.replace('<dcConf', zero + '\n      <dcConf')
        # generic: kept, with a note
        cfg, notes = self.load(xml)
        self.assertIn("0x2010:00", [x["object"] for x in cfg["slaves"][0]["settings"]])
        self.assertTrue(any("would be replaced by cia402.zero-at-start" in n for n in notes))
        # with the cia402 profile from the bus: replaced
        xml = xml.replace('type="generic"', 'type="DeASDA"')
        p = propose.Proposal(0, 0xa88, 0xa880002, 0, 0, "ECT60", profile=402, mode="csp")
        cfg, notes = self.load(xml, {0: (None, None, p)})
        s = cfg["slaves"][0]
        self.assertEqual(s["profile"], "cia402")
        self.assertIs(s["cia402"]["zero-at-start"], True)
        self.assertNotIn("0x2010:00", [x["object"] for x in s["settings"]])
        self.assertIn("0x2000:00", [x["object"] for x in s["settings"]])
        self.assertTrue(any("replaced by cia402.zero-at-start" in n for n in notes))

    def test_auto_shift(self):
        xml = LCEC.replace("<master idx", '<master globalShiftTime="auto" idx')
        with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False) as f:
            f.write(xml)
        try:
            cfg, notes = importxml.import_lcec(lcecxml.load(f.name), None, 1000000)
        finally:
            os.unlink(f.name)
        self.assertEqual(cfg["master"]["dc"]["sync0-shift-ns"], "auto")

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
        self.assertTrue(m["dc"]["servo-thread-follows-ref-clock"])          # negative refClockSyncCycles
        s = cfg["slaves"][0]
        self.assertTrue(s["configure-pdos"])
        self.assertEqual(s["dc"], {"assign-activate": "0x0300", "sync0-cycles": 1, "sync0-shift-ns": 0})
        self.assertEqual(s["settings"], [{"object": "0x2000:00", "data": "a0 0f"}])
        inputs = s["process-data"]["inputs"][0]["entries"]
        self.assertEqual(inputs[0]["split"], ["in-1", None, "in-3"])
        self.assertEqual((inputs[1]["type"], inputs[1]["hal-type"], inputs[1]["scale"]),
                         ("INT", "real", 0.5))
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
        fresh["master"]["dc"] = {"ref-clock-sync-cycles": 1000, "servo-thread-follows-ref-clock": True}
        cfg, notes = update.merge(self.old, fresh)
        self.assertEqual(cfg["slaves"][0]["identity"]["revision"], "0x00000002")
        self.assertEqual(cfg["master"]["interface"], "enp2s0")
        self.assertIn("dc", cfg["master"])
        self.assertEqual(len(notes), 2)

    def test_linuxcnc_connections_kept_and_pruned(self):
        self.old["linuxcnc"] = {
            "joints": [{"joint": 0, "slave": "x-drive", "scale": 100.0}],
            "nets": [{"signal": "home", "pins": ["io.in-1", "joint.0.home-sw-in"]},
                     {"signal": "both", "pins": ["io.in-2", "x-drive.out", "motion.digital-in-00"]}],
            "check-hal-at-start": True}
        # all there: everything kept (before, an update lost the linuxcnc section)
        cfg, _ = update.merge(self.old, self.fresh(
            slave(0, "d0", "0x1", "0xA"), slave(1, "d1", "0x1", "0xA"), slave(2, "i2", "0x2", "0xB")))
        self.assertEqual(cfg["linuxcnc"], self.old["linuxcnc"])
        # io gone: its net goes, the other loses io's pin; the joint stays
        cfg, notes = update.merge(self.old, self.fresh(
            slave(0, "d0", "0x1", "0xA"), slave(1, "d1", "0x1", "0xA")))
        self.assertEqual(cfg["linuxcnc"]["nets"], [
            {"signal": "both", "pins": ["x-drive.out", "motion.digital-in-00"]}])
        self.assertEqual(cfg["linuxcnc"]["joints"], self.old["linuxcnc"]["joints"])
        self.assertIn("signal home: pins of slaves not on the bus removed", notes)
        # one drive left: matched at its position, so y-drive is gone, and its joint
        self.old["linuxcnc"]["joints"].append({"joint": 1, "slave": "y-drive", "scale": 1.0})
        cfg, notes = update.merge(self.old, self.fresh(slave(0, "d0", "0x1", "0xA"),
                                                       slave(1, "i1", "0x2", "0xB")))
        self.assertEqual(cfg["linuxcnc"]["joints"], [{"joint": 0, "slave": "x-drive", "scale": 100.0}])
        self.assertTrue(any("joint 1: its drive y-drive is not on the bus" in n for n in notes))

    def test_modules(self):
        from ecatsetup import session
        self.old["slaves"][2]["modules"] = ["0x00000101", "0x00000202"]
        same = slave(2, "i2", "0x2", "0xB")
        same["modules"] = ["0x00000101", "0x00000202"]
        fresh = self.fresh(slave(0, "d0", "0x1", "0xA"), slave(1, "d1", "0x1", "0xA"), same)
        self.assertFalse(update.compare(self.old, fresh).any)
        other = dict(same, modules=["0x00000101"])
        fresh = self.fresh(slave(0, "d0", "0x1", "0xA"), slave(1, "d1", "0x1", "0xA"), other)
        ch = update.compare(self.old, fresh)
        self.assertEqual(ch.modules, [("io", 2)])
        cfg, notes = update.merge(self.old, fresh)
        self.assertTrue(any("its modules changed (0x00000101, the file had 0x00000101, "
                            "0x00000202); propose it again" in n for n in notes), notes)
        self.assertEqual(cfg["slaves"][2]["modules"], self.old["slaves"][2]["modules"])
        # recorded the first time the bus has them
        del self.old["slaves"][2]["modules"]
        cfg, _ = update.merge(self.old, fresh)
        self.assertEqual(cfg["slaves"][2]["modules"], ["0x00000101"])
        self.assertTrue(session.modules_match(None, [1]))
        self.assertTrue(session.modules_match(["0x1", "0x2"], [1, 2]))
        self.assertFalse(session.modules_match(["0x1"], [1, 2]))
        cfg["slaves"][2]["modules"] = ["x"]
        self.assertTrue(any("module 'x' is not a hex number" in e for e in config.check(cfg)))

    def test_compare_and_consequences(self):
        fresh = self.fresh(slave(0, "n0", "0x3", "0xC"), slave(1, "d1", "0x1", "0xA", "0x2"),
                           slave(2, "d2", "0x1", "0xA"))
        ch = update.compare(self.old, fresh)
        self.assertEqual(ch.moved, [("x-drive", "dev 0xA", 0, 1), ("y-drive", "dev 0xA", 1, 2)])
        self.assertEqual(ch.new, [(0, "dev 0xC")])
        self.assertEqual(ch.gone, [("io", "dev 0xB", 2)])
        self.assertEqual(ch.revision, [("x-drive", 1, "0x00000001", "0x2")])
        self.assertEqual(ch.summary(), "2 moved, 1 new, 1 missing, 1 of another revision")
        self.assertFalse(update.compare(self.old, self.old).any)
        self.old["linuxcnc"] = {"nets": [{"signal": "home", "pins": ["io.in-1", "joint.0.home-sw-in"]}]}
        with tempfile.TemporaryDirectory() as d:
            ini = os.path.join(d, "m.ini")
            with open(ini, "w") as f:
                f.write("[HAL]\nHALFILE = m.hal\n")
            with open(os.path.join(d, "m.hal"), "w") as f:
                f.write("net led io.out-3 => pyvcp.led\n")
            then = update.consequences(self.old, ch, ini)
        self.assertEqual(then, ["signal home connects io.in-1, which will not exist",
                                "m.hal:1 uses io.out-3, which will not exist"])



class FakeBus:
    """Registers and SDOs of one slave, as the ethercat tool would give them."""

    def __init__(self, regs, sdos, later=None, slave_line="0  0:0  OP  +  Some Drive"):
        self.regs, self.sdos, self.later, self.reads = regs, sdos, later or {}, 0
        self.slave_line = slave_line
        self.verbose = "State: PREOP\nFlag: E\n"
        self.listing = ""       # the slave's `ethercat sdos` output
        self.asked = []         # (index, "upload" or "diag"): what was asked of the slave

    def run(self, *args, binary=False):
        if args[0] == "slaves" and "-v" in args:
            return self.verbose
        if args[0] == "slaves":
            return self.slave_line + "\n"
        if args[0] == "sdos":
            return self.listing
        if args[0] == "diag":
            self.asked.append((0x10F3, "diag"))
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
        self.asked.append((index, "upload"))
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
        # an old count at its limit is not a problem, but it hides whether it still grows
        self.assertIn("Note: phy at its limit (255+)", text)

    def test_objects_the_slave_does_not_list_are_not_asked_for(self):
        # the master logs an abort for each: only ask for what the dictionary has
        regs = {0x0130: 0x08, 0x0110: 0x10}
        bus = FakeBus(regs, {0x1001: 0})
        bus.listing = 'SDO 0x1000, "Device Type"\n  0x1000:00, r-r-r-, uint32, 32 bit, "Device Type"\n'
        self.run_diagnose(bus)
        self.assertEqual(bus.asked, [(0x1000, "upload")])

    def test_listed_objects_are_asked_for(self):
        regs = {0x0130: 0x08, 0x0110: 0x10}
        bus = FakeBus(regs, {0x1001: 0})
        bus.listing = "".join('SDO 0x%04x, "x"\n  0x%04x:00, r-r-r-, uint32, 32 bit, "x"\n' % (i, i)
                              for i in (0x1000, 0x1001, 0x10F3))
        self.run_diagnose(bus)
        self.assertEqual(sorted(bus.asked), [(0x1000, "upload"), (0x1001, "upload"),
                                             (0x10F3, "diag")])

    def test_an_empty_listing_asks_as_before(self):
        bus = FakeBus({0x0130: 0x08, 0x0110: 0x10}, {})
        self.run_diagnose(bus)
        self.assertEqual(sorted(bus.asked), [(0x1000, "upload"), (0x1001, "upload"),
                                             (0x10F3, "diag")])

    def test_a_count_that_is_not_at_its_limit_has_no_note(self):
        regs = {0x0130: 0x08, 0x0110: 0x10, 0x0301: 254}
        found, text = self.run_diagnose(FakeBus(regs, {}))
        self.assertEqual(found, 0)
        self.assertNotIn("at its limit", text)

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

