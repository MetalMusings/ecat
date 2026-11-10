"""Tests of the I/O test mode against a fake halrun and halcmd.

Run with: python3 -m unittest discover -s tests
"""

import json
import os
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ecatsetup import testmode  # noqa: E402

# The fake HAL keeps its pins in a JSON file: name -> [type, dir, value].
FAKE_HALRUN = """\
#!/usr/bin/python3
import json, os, sys
state = os.environ["FAKE_HAL_STATE"]
if os.environ.get("FAKE_HAL_FAIL"):
    # as halrun -I: the error in the file is reported, and it goes on reading commands
    print("ecat: slave 1 is EL2008 (vendor 0x00000002, product 0x07D83052); the configuration "
          "expects d there", flush=True)
    print("ecat: the bus does not match the configuration, so ecat does not start", flush=True)
    print("ecat: failure, clearing config", flush=True)
    print("ecat: exiting", flush=True)
    print("%s:2: insmod for ecat failed, returned -1" % sys.argv[-1], flush=True)
    for line in sys.stdin:
        pass
    sys.exit(0)
json.dump(json.loads(os.environ["FAKE_HAL_PINS"]), open(state, "w"))
for line in sys.stdin:
    with open(state + ".cmds", "a") as f:
        f.write(line)
    if line.strip() == "exit":
        break
os.unlink(state)
"""
FAKE_HALCMD = """\
#!/usr/bin/python3
import json, os, sys
state = os.environ["FAKE_HAL_STATE"]
pins = json.load(open(state)) if os.path.exists(state) else {}
a = sys.argv[1:]
if a[:2] == ["-k", "-f"]:
    for line in sys.stdin:
        f = line.split()
        if len(f) == 3 and f[0] == "setp":
            pins.setdefault(f[1], ["param", "IN", ""])[2] = f[2]
            with open(state + ".setp", "a") as log:
                log.write("%s %s\\n" % (f[1], f[2]))
    json.dump(pins, open(state, "w"))
    sys.exit(0)
if a[:3] == ["-s", "show", "comp"]:
    print("    2  User  halcmd%d   %d ready" % (os.getpid(), os.getpid()))
    if pins:
        print("    4  RT    ecat   ready")
elif a[:3] == ["-s", "show", "pin"]:
    for n in sorted(pins):
        if len(a) < 4 or n.startswith(a[3]):
            t, d, v = pins[n]
            print("ecat %s %s %s %s" % (t, d, v, n))
elif a[0] == "setp":
    if a[1] not in pins or pins[a[1]][1] != "IN":
        print("pin '%s' is not writable" % a[1], file=sys.stderr)
        sys.exit(1)
    pins[a[1]][2] = a[2]
    if a[1] == "ecat.take-down" and a[2] == "TRUE":
        pins["ecat.state-op"][2] = "FALSE"
        pins["ecat.state-preop"][2] = "TRUE"
    json.dump(pins, open(state, "w"))
    with open(state + ".setp", "a") as f:
        f.write("%s %s\\n" % (a[1], a[2]))
"""

PINS = {
    "ecat.0.all-op": ["bit", "OUT", "TRUE"],
    "ecat.0.slaves-responding": ["u32", "OUT", "0x00000002"],
    "ecat.take-down": ["bit", "IN", "FALSE"],
    "ecat.state-op": ["bit", "OUT", "TRUE"],
    "ecat.state-safeop": ["bit", "OUT", "FALSE"],
    "ecat.state-preop": ["bit", "OUT", "FALSE"],
    "io.out-1": ["bit", "IN", "FALSE"],
    "io.level": ["float", "IN", "0"],
    "io.in-1": ["bit", "OUT", "TRUE"],
    "x.enable": ["bit", "IN", "FALSE"],
}


def cfg():
    def slave(pos, name, profile):
        s = {"position": pos, "name": name, "device": "d", "profile": profile,
             "identity": {"vendor": "0x00000A88", "product": "0x0A880002"},
             "configure-pdos": False, "process-data": {"outputs": [
                 {"pdo": "0x1600", "sm": 2, "entries": [
                     {"object": "0x7000:01", "bits": 1, "type": "BOOL", "pin": "out-1",
                      "hal-type": "bit"}]}]}}
        if profile == "cia402":
            s["cia402"] = {"mode": "csp"}
            s["process-data"] = {"outputs": [{"pdo": "0x1600", "sm": 2, "entries": [
                {"object": "0x6040:00", "bits": 16, "type": "UINT", "use": "profile"},
                {"object": "0x607A:00", "bits": 32, "type": "DINT", "use": "profile"}]}],
                "inputs": [{"pdo": "0x1A00", "sm": 3, "entries": [
                    {"object": "0x6041:00", "bits": 16, "type": "UINT", "use": "profile"},
                    {"object": "0x6064:00", "bits": 32, "type": "DINT", "use": "profile"}]}]}
        return s
    return {"ecat": 1, "master": {"interface": "enp1s0", "transport": "raw"},
            "slaves": [slave(0, "x", "cia402"), slave(1, "io", "generic")]}


class TestTestMode(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        d = self.dir.name
        for name, text in (("halrun", FAKE_HALRUN), ("halcmd", FAKE_HALCMD)):
            path = os.path.join(d, name)
            with open(path, "w") as f:
                f.write(text)
            os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        self.state = os.path.join(d, "hal.json")
        for p in (mock.patch.object(testmode, "HALRUN", os.path.join(d, "halrun")),
                  mock.patch.object(testmode, "HALCMD", os.path.join(d, "halcmd")),
                  mock.patch.object(testmode, "KERNEL_MASTER", os.path.join(d, "none")),
                  mock.patch.object(testmode.netif, "interfaces", lambda: ["enp1s0", "enp2s0"]),
                  mock.patch.object(testmode.netif, "xdp_native", lambda name: name == "enp1s0"),
                  mock.patch.object(testmode.netif, "driver", {"enp1s0": "igb", "enp2s0": "r8169"}.get),
                  mock.patch.dict(os.environ, {"FAKE_HAL_STATE": self.state,
                                               "FAKE_HAL_PINS": json.dumps(PINS)})):
            p.start()
            self.addCleanup(p.stop)

    def started(self):
        run = testmode.TestRun(cfg(), 1000000)
        run.start(timeout=10)
        self.addCleanup(run.stop)
        return run

    def test_start_read_set_and_stop(self):
        run = self.started()
        self.assertTrue(run.running)
        with open(os.path.join(run.dir, "test.hal")) as f:
            hal = f.read()
        self.assertIn("loadrt ecat config=%s/ethercat.yaml" % run.dir, hal)
        self.assertIn("period1=1000000", hal)
        pins = run.pins("io.")
        self.assertEqual(sorted(pins), ["io.in-1", "io.level", "io.out-1"])
        run.set_pin(pins["io.out-1"], True)
        run.set_pin(pins["io.level"], "2.5")
        self.assertEqual(run.pins("io.")["io.out-1"].value, "TRUE")
        run.stop()
        self.assertFalse(run.running)
        with open(self.state + ".setp") as f:
            setp = f.read().splitlines()
        # set during the test, then back to 0 before the thread stops
        # first every drive disabled, then the slaves to PREOP while process
        # data still flows
        self.assertEqual(setp, ["io.out-1 TRUE", "io.level 2.5", "x.enable FALSE",
                                "io.out-1 FALSE", "io.level 0", "ecat.take-down TRUE"])
        with open(self.state + ".cmds") as f:
            self.assertEqual(f.read(), "stop\nexit\n")

    def test_drives_master_and_inputs_are_not_set(self):
        run = self.started()
        pins = run.pins()
        for name in ("x.enable", "ecat.0.all-op", "io.in-1"):
            self.assertFalse(run.writable(pins[name]), name)
            with self.assertRaises(testmode.TestError):
                run.set_pin(pins[name], True)
        self.assertTrue(run.writable(pins["io.out-1"]))

    def test_refuses_with_hal_running(self):
        with open(self.state, "w") as f:
            json.dump(PINS, f)
        with self.assertRaisesRegex(testmode.TestError, "HAL runs already"):
            testmode.TestRun(cfg()).start(timeout=1)

    def test_refuses_with_the_kernel_master(self):
        with mock.patch.object(testmode, "KERNEL_MASTER", self.dir.name):
            with self.assertRaisesRegex(testmode.TestError, "systemctl stop ethercat"):
                testmode.TestRun(cfg()).start(timeout=1)

    def test_refuses_an_interface_that_cannot_work(self):
        for master, text in (({"transport": "raw"}, "no network interface"),
                             ({"interface": "eth9", "transport": "raw"}, "eth9 does not exist"),
                             ({"interface": "enp2s0", "transport": "xdp-native"},
                              r"enp2s0 \(r8169\) cannot do xdp-native")):
            c = cfg()
            c["master"] = master
            with self.assertRaisesRegex(testmode.TestError, text):
                testmode.TestRun(c).start(timeout=1)
        for master in ({"interface": "enp2s0", "transport": "raw"},
                       {"interface": "enp1s0", "transport": "xdp-native"}):
            c = cfg()
            c["master"] = master
            self.assertIsNone(testmode.interface_problem(c))

    def test_says_so_when_ecat_does_not_load(self):
        # halrun -I goes on after the file failed: the test must not wait and run "anyway"
        with mock.patch.dict(os.environ, {"FAKE_HAL_FAIL": "1"}):
            run = testmode.TestRun(cfg())
            t0 = time.monotonic()
            with self.assertRaises(testmode.TestError) as e:
                run.start(timeout=10)
            # the component's own reasons, not halrun's or its farewell
            self.assertEqual(str(e.exception), "ecat could not be started:\n"
                             "ecat: slave 1 is EL2008 (vendor 0x00000002, product 0x07D83052); "
                             "the configuration expects d there\n"
                             "ecat: the bus does not match the configuration, so ecat does not start")
            self.assertLess(time.monotonic() - t0, 5)
            self.assertFalse(run.running)

    def test_refuses_a_configuration_with_errors(self):
        c = cfg()
        c["slaves"][1]["name"] = "x"
        with self.assertRaisesRegex(testmode.TestError, "used twice"):
            testmode.TestRun(c)

    def test_sdo_writes_refused(self):
        x, io = cfg()["slaves"]
        self.assertIn("Zero Position", testmode.sdo_write_refused(x, 0x2010, 0))
        self.assertIn("process data", testmode.sdo_write_refused(io, 0x7000, 1))
        self.assertIn("CiA 402", testmode.sdo_write_refused(x, 0x6081, 0))
        self.assertIsNone(testmode.sdo_write_refused(x, 0x2000, 1))

    def test_hal_file(self):
        hal = testmode.hal_file(cfg(), "/t/ethercat.yaml", 500000)
        lines = hal.splitlines()
        self.assertIn("loadrt limit3 names=ecatjog0", lines)
        # pos-cmd comes from the limit3, which follows pos-fb until the
        # drive is in "Operation enabled"
        self.assertIn("net ecatjog0-cmd ecatjog0.out => x.pos-cmd", lines)
        self.assertIn("net ecatjog0-fb x.pos-fb => ecatsel0.in0", lines)
        self.assertIn("net ecatjog0-enabled x.stat-op-enabled => ecatsel0.sel ecatnot0.in", lines)
        self.assertIn("net ecatjog0-load ecatnot0.out => ecatjog0.load", lines)
        self.assertIn("setp ecatjog0.maxv 0", lines)
        self.assertIn("setp x.watchdog-ms %d" % testmode.WATCHDOG_MS, lines)
        # the jog runs between reading and writing the bus
        order = [l for l in lines if l.startswith("addf")]
        self.assertEqual(order, ["addf ecat.read-all servo-thread", "addf ecatsel0 servo-thread",
                                 "addf ecatnot0 servo-thread", "addf ecatjog0 servo-thread",
                                 "addf ecat.write-all servo-thread"])

    def test_heartbeat(self):
        run = self.started()
        run.tick_watchdogs()
        run.tick_watchdogs()
        with open(self.state + ".setp") as f:
            self.assertEqual(f.read().splitlines(), ["x.watchdog 1", "x.watchdog 2"])

    def test_parse_pins(self):
        pins = testmode.parse_pins("ecat float IN 0 lcec.0.E7000.Level-1\n\n")
        p = pins["lcec.0.E7000.Level-1"]
        self.assertEqual((p.type, p.dir, p.value), ("float", "IN", "0"))


if __name__ == "__main__":
    unittest.main()
