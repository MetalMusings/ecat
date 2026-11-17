"""Tests of the real-time check (rtcheck.py) on fake /proc and /sys trees
and a fake halcmd/halsampler.

Run with: python3 -m unittest discover -s tests
"""

import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("ECATSETUP_LINUXCNC_SERIES", "2.10")
os.environ["ECATSETUP_SETTINGS"] = os.path.join(os.path.dirname(__file__), "no-such-dir", "settings.yaml")
if "ECATSETUP_TEST_STATE" not in os.environ:
    os.environ["ECATSETUP_TEST_STATE"] = os.environ["XDG_STATE_HOME"] = os.environ["TMPDIR"] = \
        __import__("tempfile").tempdir = __import__("tempfile").mkdtemp(prefix="ecatsetup-test-state-")
    __import__("atexit").register(__import__("shutil").rmtree, os.environ["ECATSETUP_TEST_STATE"], True)

from ecatsetup import rtcheck  # noqa: E402

INTERRUPTS = """\
            CPU0       CPU1       CPU2       CPU3
   0:         10          0          0          0  IR-IO-APIC    2-edge      timer
   9:          0          4          0          0  IR-IO-APIC    9-fasteoi   acpi
 125:          0          0          0      91234  IR-PCI-MSIX-0000:02:00.0    0-edge      enp2s0
 126:         12          0          0          0  IR-PCI-MSIX-0000:02:00.0    1-edge      enp2s0-rx-0
 127:          0        300          0          0  IR-PCI-MSI-0000:00:14.0    0-edge      xhci_hcd
 128:          5          0          0          0  IR-PCI-MSI-0000:00:17.0    0-edge      ahci[0000:00:17.0], snd_hda_intel:card0
NMI:          0          0          0          0   Non-maskable interrupts
LOC:     123456     234567     345678       9999   Local timer interrupts
ERR:          0
MIS:          0
"""

THREADS_210 = """\
Realtime Threads:
     Period  FP     Name               (     Time [s], Max-Time [s] )
    1000000  YES          servo-thread (  0.000000144,  0.000000670 )
                  1 ecatsetup-rt-td-servo-thread
                  2 ecat.read-all
                  3 motion-command-handler
                  4 sampler.1
      25000  YES           base-thread (  0.000002485,  0.000002485 )
                  1 ecatsetup-rt-td-base-thread
                  2 sampler.0
"""

PINS_210 = """\
     8  real  OUT              1.44e-07  servo-thread.time
     8  real  I/O               6.7e-07  servo-thread.tmax
    11  real  I/O               4.2e-05  ecat.read-all.tmax
    11  real  I/O               2.0e-06  motion-command-handler.tmax
     7  real  I/O               5.0e-06  base-thread.tmax
"""

PARAMS_29 = """\
     6  s32   RW            689  servo-thread.tmax
     9  s32   RW          42000  ecat.read-all.tmax
     9  s32   RW           2000  motion-command-handler.tmax
     5  s32   RW           5000  base-thread.tmax
"""


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def fake_tree(root, isolated="3", smt=False, vm=False):
    P, S = os.path.join(root, "proc"), os.path.join(root, "sys")
    write(P, "sys/kernel/osrelease", "6.12.69+deb13-rt-amd64\n")
    write(P, "version", "Linux version 6.12.69 #1 SMP PREEMPT_RT\n")
    write(P, "cmdline", "BOOT_IMAGE=/vmlinuz ro quiet isolcpus=managed_irq,domain,3 nohz_full=3 "
          "rcu_nocbs=3 irqaffinity=0-2 intel_idle.max_cstate=1\n")
    write(P, "cpuinfo", "processor\t: 0\nmodel name\t: Intel(R) N100\nflags\t\t: fpu vme%s\n"
          % (" hypervisor" if vm else ""))
    write(P, "interrupts", INTERRUPTS)
    for irq, aff in (("0", "0"), ("9", "1"), ("125", "3"), ("126", "0"), ("127", "1"), ("128", "0")):
        write(P, "irq/%s/effective_affinity_list" % irq, aff + "\n")
    write(S, "kernel/realtime", "1\n")
    cpu = "devices/system/cpu"
    write(S, cpu + "/online", "0-3\n")
    write(S, cpu + "/isolated", isolated + "\n")
    write(S, cpu + "/nohz_full", "3\n")
    write(S, cpu + "/intel_pstate/no_turbo", "0\n")
    write(S, cpu + "/cpuidle/current_driver", "intel_idle\n")
    for c in range(4):
        sib = {0: "0,2", 2: "0,2", 1: "1,3", 3: "1,3"}[c] if smt else str(c)
        write(S, cpu + "/cpu%d/topology/thread_siblings_list" % c, sib + "\n")
        write(S, cpu + "/cpu%d/cpufreq/scaling_governor" % c, "performance\n" if c == 3 else "powersave\n")
        write(S, cpu + "/cpu%d/thermal_throttle/core_throttle_count" % c, "2\n" if c == 1 else "0\n")
        for i, (name, lat, dis) in enumerate((("POLL", 0, 0), ("C1E", 2, 0), ("C6", 170, 1 if c == 3 else 0))):
            base = cpu + "/cpu%d/cpuidle/state%d/" % (c, i)
            write(S, base + "name", name + "\n")
            write(S, base + "latency", "%d\n" % lat)
            write(S, base + "disable", "%d\n" % dis)
            write(S, base + "usage", "100\n")
            write(S, base + "time", "1000\n")
    # a network card with its driver, and lo
    os.makedirs(os.path.join(S, "bus/pci/drivers/igc"), exist_ok=True)
    os.makedirs(os.path.join(S, "devices/pci0000:00/0000:02:00.0/link"), exist_ok=True)
    write(S, "devices/pci0000:00/0000:02:00.0/link/l1_aspm", "1\n")
    os.symlink(os.path.join(S, "bus/pci/drivers/igc"),
               os.path.join(S, "devices/pci0000:00/0000:02:00.0/driver"))
    os.makedirs(os.path.join(S, "class/net/enp2s0"), exist_ok=True)
    os.symlink(os.path.join(S, "devices/pci0000:00/0000:02:00.0"), os.path.join(S, "class/net/enp2s0/device"))
    write(S, "class/net/enp2s0/operstate", "up\n")
    write(S, "class/net/enp2s0/speed", "100\n")
    os.makedirs(os.path.join(S, "class/net/lo"), exist_ok=True)
    write(S, "module/pcie_aspm/parameters/policy", "[default] performance powersave powersupersave\n")
    os.makedirs(os.path.join(S, "bus/pci/drivers/i915"), exist_ok=True)
    os.makedirs(os.path.join(S, "devices/pci0000:00/0000:00:02.0"), exist_ok=True)
    os.symlink(os.path.join(S, "bus/pci/drivers/i915"), os.path.join(S, "devices/pci0000:00/0000:00:02.0/driver"))
    os.makedirs(os.path.join(S, "class/drm/card0"), exist_ok=True)
    os.symlink(os.path.join(S, "devices/pci0000:00/0000:00:02.0"), os.path.join(S, "class/drm/card0/device"))
    os.makedirs(os.path.join(S, "class/drm/card0-HDMI-A-1"), exist_ok=True)
    write(S, "class/dmi/id/sys_vendor", "QEMU\n" if vm else "\n")
    write(S, "class/dmi/id/board_vendor", "ACME\n")
    write(S, "class/dmi/id/board_name", "N100 board\n")
    write(S, "class/dmi/id/bios_vendor", "American Megatrends\n")
    write(S, "class/dmi/id/bios_version", "5.26\n")
    write(S, "class/power_supply/AC/type", "Mains\n")
    write(S, "class/power_supply/AC/online", "1\n")
    # processes: irqbalance, and rtapi_app with a FIFO thread on CPU 3
    write(P, "100/comm", "irqbalance\n")
    write(P, "200/comm", "rtapi_app\n")
    rest = " ".join(["0"] * 35)

    def stat_line(tid, comm, cpu_, prio, pol):
        return "%d (%s) S %s %d %d %d 0 0\n" % (tid, comm, rest, cpu_, prio, pol)
    write(P, "200/task/200/stat", stat_line(200, "rtapi_app", 2, 0, 0))
    write(P, "200/task/201/stat", stat_line(201, "rtapi_app:T#0", 3, 98, 1))
    return rtcheck.Roots(P, S, run=fake_run)


def fake_run(argv):
    if argv[:2] == ["ethtool", "-c"]:
        return "Coalesce parameters for %s:\nrx-usecs:\t3\nrx-frames:\tn/a\n" % argv[2]
    if argv[:2] == ["ethtool", "--show-eee"]:
        return "EEE settings for %s:\n\tEEE status: enabled - active\n" % argv[2]
    return None


class TestParsing(unittest.TestCase):
    def test_cpu_lists(self):
        self.assertEqual(rtcheck.cpu_list("0-2,5\n"), [0, 1, 2, 5])
        self.assertEqual(rtcheck.cpu_list(""), [])
        self.assertEqual(rtcheck.cpu_list(None), [])
        self.assertEqual(rtcheck.list_text([5, 0, 1, 2]), "0-2,5")
        self.assertEqual(rtcheck.list_text([3]), "3")

    def test_cmdline(self):
        p = rtcheck.cmdline_params("ro quiet isolcpus=managed_irq,3 idle=poll")
        self.assertIsNone(p["ro"])
        self.assertEqual(p["isolcpus"], "managed_irq,3")
        self.assertEqual(p["idle"], "poll")

    def test_interrupts(self):
        cpus, rows = rtcheck.parse_interrupts(INTERRUPTS)
        self.assertEqual(cpus, [0, 1, 2, 3])
        by = {r["irq"]: r for r in rows}
        self.assertEqual(by["125"]["counts"], [0, 0, 0, 91234])
        self.assertEqual(by["125"]["name"], "enp2s0")
        self.assertEqual(by["125"]["chip"], "IR-PCI-MSIX-0000:02:00.0")
        self.assertEqual(by["128"]["name"], "ahci[0000:00:17.0], snd_hda_intel:card0")
        self.assertEqual(by["LOC"]["counts"], [123456, 234567, 345678, 9999])
        self.assertEqual(by["LOC"]["name"], "Local timer interrupts")
        self.assertEqual(by["ERR"]["counts"], [0])

    def test_task_stat(self):
        rest = " ".join(["0"] * 35)
        self.assertEqual(rtcheck.task_stat("7 (rtapi_app:T#0) S %s 3 98 1 0 0" % rest),
                         ("rtapi_app:T#0", 3, 98, 1))
        # a name with spaces and parentheses
        self.assertEqual(rtcheck.task_stat("7 (a (b) c) S %s 1 0 0 0 0" % rest)[0], "a (b) c")
        self.assertIsNone(rtcheck.task_stat(""))


class TestInventory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_facts(self):
        inv = rtcheck.inventory(fake_tree(self.root))
        d = inv.data
        self.assertTrue(d["preempt_rt"])
        self.assertEqual(d["isolated"], [3])
        self.assertEqual(d["isolcpus_flags"], ["managed_irq", "domain"])
        self.assertEqual(d["nohz_full"], [3])
        self.assertEqual(d["rcu_nocbs"], [3])
        self.assertEqual(d["irqaffinity"], [0, 1, 2])
        self.assertEqual(inv.fact("smt").value, "off")
        self.assertEqual(inv.fact("cpu_model").value, "Intel(R) N100")
        self.assertEqual(d["governor"]["3"], "performance")
        self.assertIn("cpu3 performance", inv.fact("governor").value)
        self.assertTrue(d["turbo"])
        # CPU 3 has C6 disabled: its deepest is C1E; the others reach C6
        self.assertIn("C6 (170 us)", inv.fact("cpuidle").value)
        self.assertIn("C1E (2 us)", inv.fact("cpuidle").value)
        self.assertEqual(d["cpuidle"]["max_cstate"], {"intel_idle.max_cstate": "1"})
        self.assertEqual(inv.fact("throttle").value, "2")
        irqs = {r["irq"]: r for r in d["interrupts"]["rows"]}
        self.assertEqual(irqs["125"]["affinity"], [3])
        self.assertTrue(d["irqbalance"])
        self.assertFalse(d["networkmanager"])
        nic, = d["nics"]
        self.assertEqual((nic["name"], nic["driver"], nic["speed"]), ("enp2s0", "igc", "100"))
        self.assertEqual(nic["irqs"], ["125", "126"])
        self.assertEqual(nic["rx_usecs"], "3")
        self.assertEqual(nic["eee"], "enabled - active")
        self.assertEqual(nic["aspm"], {"l1_aspm": True})
        self.assertIn("ASPM l1", inv.fact("nics").value)
        self.assertEqual(inv.fact("aspm_policy").value, "default")
        self.assertEqual(d["graphics"], ["i915"])
        self.assertEqual(inv.fact("board").value, "ACME N100 board")
        self.assertIn("5.26", inv.fact("bios").value)
        self.assertFalse(d["vm"])
        self.assertFalse(d["battery"])
        self.assertEqual(d["rt_cpu"], [3])
        self.assertIn("rtapi_app:T#0 on CPU 3", inv.fact("rt_cpu").value)
        # every fact says where it was read
        self.assertTrue(all(f.source for f in inv.facts))
        text = rtcheck.inventory_text(inv)
        self.assertIn("[/sys/devices/system/cpu/isolated]", text)

    def test_smt_and_vm(self):
        inv = rtcheck.inventory(fake_tree(self.root, isolated="", smt=True, vm=True))
        self.assertEqual(inv.data["isolated"], [])
        self.assertEqual(inv.fact("isolated").value, "none")
        self.assertEqual(inv.fact("smt").value, "on: [0,2]  [1,3]")
        self.assertTrue(inv.data["vm"])

    def test_missing_tree(self):
        # an empty tree: nothing crashes, unknowns say so
        r = rtcheck.Roots(os.path.join(self.root, "p"), os.path.join(self.root, "s"), run=lambda a: None)
        inv = rtcheck.inventory(r)
        self.assertEqual(inv.fact("kernel").value, "?")
        self.assertEqual(inv.fact("turbo").value, "not known")
        self.assertEqual(inv.fact("rt_cpu").value, "LinuxCNC is not running")

    def test_ini(self):
        d = os.path.join(self.root, "cfg")
        write(d, "m.ini", "[EMCMOT]\nBASE_PERIOD = 25000\nSERVO_PERIOD = 1000000\n"
              "[HAL]\nHALFILE = m.hal\nHALFILE = ethercat.hal\n")
        write(d, "m.hal", "loadrt [KINS]KINEMATICS\nloadrt hm2_eth board_ip=10.10.10.10\n"
              "loadrt hostmot2\n")
        inv = rtcheck.inventory(rtcheck.Roots(os.path.join(self.root, "p"), os.path.join(self.root, "s"),
                                              run=lambda a: None), ini=os.path.join(d, "m.ini"))
        self.assertEqual(inv.data["threads"], [("base-thread", 25000), ("servo-thread", 1000000)])
        self.assertEqual(inv.fact("threads").value, "base-thread 25 us, servo-thread 1 ms")
        self.assertEqual(inv.data["io"], ["Mesa Ethernet (hm2_eth)"])

    def test_json(self):
        inv = rtcheck.inventory(fake_tree(self.root))
        back = rtcheck.Inventory.from_json(json.loads(json.dumps(inv.to_json())))
        self.assertEqual(back.fact("isolated").value, "3")
        self.assertEqual(back.data["isolated"], [3])


class TestStatistics(unittest.TestCase):
    def test_poisson(self):
        self.assertAlmostEqual(rtcheck.poisson_upper(0), 2.996, places=2)
        self.assertAlmostEqual(rtcheck.poisson_upper(1), 4.744, places=2)
        self.assertAlmostEqual(rtcheck.poisson_upper(10), 17.0, delta=0.1)
        # the design's example: none in 15 minutes -> fewer than about 12 per hour
        self.assertAlmostEqual(rtcheck.rate_bound(0, 900), 11.98, places=1)
        self.assertAlmostEqual(rtcheck.seconds_needed(12), 899, delta=2)

    def test_cycle_stats(self):
        s = rtcheck.CycleStats("servo-thread", 1000000)
        # the first sample of a fresh timedelta is 0: skipped
        lines = ["0"] + ["1000000"] * 9990 + ["1000500"] * 5 + ["1025000", "975000", "1120000", "overrun"]
        s.feed("\n".join(lines) + "\n")
        self.assertEqual(s.n, 9998)
        self.assertEqual(s.overruns, 1)
        m = s.summary()
        self.assertEqual(m["max_us"], 120.0)
        self.assertEqual(m["min_us"], -25.0)
        self.assertEqual(m["max_abs_us"], 120.0)
        self.assertEqual(m["p99_us"], 1)
        self.assertEqual(m["over"]["20"], 3)
        self.assertEqual(m["over"]["50"], 1)
        self.assertEqual(m["over"]["100"], 1)
        self.assertEqual(m["over"]["200"], 0)
        self.assertEqual(s.worst[0][1], 120000)
        # halsampler -t: the sample number first
        t = rtcheck.CycleStats("x", 1000000)
        t.feed("0 1000000\n1 1003000\n")
        self.assertEqual(t.n, 2)
        self.assertEqual(t.max, 3000)
        # "over" means more than: exactly 20 us is not counted
        t.feed("1020000\n980000\n")
        self.assertEqual(t.over[20], 0)
        self.assertEqual(t.over[10], 2)
        back = rtcheck.CycleStats.from_json(json.loads(json.dumps(s.to_json())))
        self.assertEqual(back.summary(), m)

    def test_claims(self):
        s = rtcheck.CycleStats("servo-thread", 1000000)
        s.feed("1000000\n" * 900000)              # 15 minutes at 1 ms
        self.assertIn("fewer than 12 per hour", rtcheck.claim_text(s, 50))
        s.feed("1060000\n")
        self.assertIn("1 over 50 us in 15 min", rtcheck.claim_text(s, 50))

    def test_compare_with_noise(self):
        a = {"p99_us": 4, "p9999_us": 14, "max_abs_us": 21.0}
        b = {"p99_us": 4, "p9999_us": 9, "max_abs_us": 8.7}
        n = rtcheck.noise(a, b)
        self.assertEqual(n["p9999_us"], 5)
        after = {"p99_us": 3, "p9999_us": 6, "max_abs_us": 40.0}
        c = rtcheck.compare(a, after, n)
        self.assertEqual(c["p99_us"][2], "no change")      # 1 us: the histogram's resolution
        self.assertEqual(c["p9999_us"][2], "better")       # 8 us < -5
        self.assertEqual(c["max_abs_us"][2], "worse")      # +19 us > 12.3
        self.assertEqual(rtcheck.compare(a, b)["p9999_us"][2], "better")

    def test_rates(self):
        self.assertFalse(rtcheck.rate_differs(1, 600, 2, 600))
        self.assertTrue(rtcheck.rate_differs(40, 600, 0, 600))


class TestHal(unittest.TestCase):
    def test_threads_hal(self):
        text = rtcheck.threads_hal([("base-thread", 25000), ("servo-thread", 1000000)])
        lines = text.splitlines()
        self.assertEqual(lines[0], "loadrt threads name1=base-thread period1=25000 "
                                   "name2=servo-thread period2=1000000")
        self.assertIn("loadrt timedelta names=ecatsetup-rt-td-base-thread,ecatsetup-rt-td-servo-thread",
                      lines)
        self.assertIn("loadrt sampler depth=16384,16384 cfg=s,s", lines)
        # the timedelta first in its thread, the sampler channel by index
        self.assertIn("addf ecatsetup-rt-td-servo-thread servo-thread 1", lines)
        self.assertIn("net ecatsetup-rt-1 ecatsetup-rt-td-servo-thread.out sampler.1.pin.0", lines)
        self.assertEqual(lines[-1], "start")

    def test_detach_reverses_attach(self):
        a = rtcheck.attach_commands(["servo-thread"])
        d = rtcheck.detach_commands(["servo-thread"])
        for cmd in a:
            f = cmd.split()
            if f[0] == "addf":
                self.assertIn("delf %s %s" % (f[1], f[2]), d)
            if f[0] == "net":
                self.assertIn("delsig %s" % f[1], d)
        # functions out before the components go
        self.assertLess(max(i for i, c in enumerate(d) if c.startswith("delf")),
                        min(i for i, c in enumerate(d) if c.startswith("unloadrt")))

    def test_times_210_and_29(self):
        vals = rtcheck.parse_values(PINS_210)
        t = rtcheck.thread_times(THREADS_210, vals)
        self.assertAlmostEqual(t["servo-thread"]["tmax_us"], 0.67)
        self.assertEqual(t["servo-thread"]["period"], 1000000)
        self.assertEqual(sorted(t["servo-thread"]["funcs"]),
                         ["ecat.read-all", "motion-command-handler", "sampler.1"])
        self.assertAlmostEqual(t["servo-thread"]["funcs"]["ecat.read-all"], 42.0)
        self.assertIsNone(t["servo-thread"]["funcs"]["sampler.1"])
        t29 = rtcheck.thread_times(THREADS_210, rtcheck.parse_values(PARAMS_29))
        self.assertAlmostEqual(t29["servo-thread"]["tmax_us"], 0.689)
        self.assertAlmostEqual(t29["servo-thread"]["funcs"]["ecat.read-all"], 42.0)
        self.assertAlmostEqual(t29["base-thread"]["tmax_us"], 5.0)

    def test_budget(self):
        s = rtcheck.CycleStats("servo-thread", 1000000)
        s.feed("1000000\n1030000\n970000\n")
        times = {"servo-thread": {"tmax_us": 80.0, "funcs": {"ecat.read-all": 42.0, "motion": 20.0,
                                                           "hm2_7i76e.0.read": 10.0}}}
        b = rtcheck.budget(s, times)
        self.assertEqual(b.left_us, 1000 - 30 - 80)
        self.assertEqual(b.verdict(), "plenty")
        self.assertEqual(b.io_funcs, ["ecat.read-all", "hm2_7i76e.0.read"])
        self.assertEqual(b.io_us, 52.0)
        self.assertIn("I/O at most 52.0 us", b.text())
        self.assertEqual(rtcheck.Budget("t", 125000, 40, 70, None, []).verdict(), "tight")
        self.assertEqual(rtcheck.Budget("t", 125000, 40, 100, None, []).verdict(),
                         "too little: cycles run into the next one")
        self.assertEqual(rtcheck.Budget("t", 125000, 20, 60, None, []).verdict(), "enough")
        self.assertIn("not measured", rtcheck.Budget("t", 1000000, 20, None, None, []).text())


FAKE_HALCMD = """\
#!/usr/bin/python3
import json, os, sys
state = os.environ["FAKE_RT_STATE"]
st = json.load(open(state))
args = sys.argv[1:]
if args and args[0] == "-s":
    args = args[1:]
with open(state + ".cmds", "a") as f:
    f.write(" ".join(args) + "\\n")
if args[:2] == ["show", "comp"]:
    for c in st["comps"]:
        print("    9  RT    %s   ready" % c)
elif args[:2] == ["show", "thread"]:
    print(st["threads"])
elif args[:2] == ["show", "pin"]:
    print(st["pins"])
elif args[:2] == ["show", "param"]:
    pass
elif args and args[0] == "loadrt":
    if args[1] in st["comps"]:
        print("already loaded", file=sys.stderr)
        sys.exit(1)
    st["comps"].append(args[1])
elif args and args[0] == "unloadrt":
    st["comps"].remove(args[1])
json.dump(st, open(state, "w"))
"""

FAKE_HALSAMPLER = """\
#!/usr/bin/python3
import sys, time
ch = sys.argv[sys.argv.index("-c") + 1]
period = 25000 if ch == "0" else 1000000
for i in range(2000):
    print(period + (60000 if (ch == "1" and i == 7) else 0))
sys.stdout.flush()
time.sleep(60)
"""

THREADS_USER = """\
Realtime Threads:
     Period  FP     Name               (     Time [s], Max-Time [s] )
    1000000  YES          servo-thread (  0.000000144,  0.000000670 )
                  1 motion-command-handler
      25000  YES           base-thread (  0.000002485,  0.000002485 )
                  1 stepgen.make-pulses
"""


class TestAttach(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.state = os.path.join(d, "hal.json")
        for name, text in (("halcmd", FAKE_HALCMD), ("halsampler", FAKE_HALSAMPLER)):
            p = os.path.join(d, name)
            with open(p, "w") as f:
                f.write(text)
            os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        self.env = mock.patch.dict(os.environ, {
            "ECAT_HALCMD": os.path.join(d, "halcmd"), "ECAT_HALSAMPLER": os.path.join(d, "halsampler"),
            "FAKE_RT_STATE": self.state})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def hal(self, comps):
        with open(self.state, "w") as f:
            json.dump({"comps": comps, "threads": THREADS_USER, "pins": PINS_210}, f)

    def cmds(self):
        with open(self.state + ".cmds") as f:
            return [l.strip() for l in f]

    def test_attach_measures_and_takes_out_again(self):
        self.hal(["halcmd123", "motmod", "stepgen"])
        inv = rtcheck.Inventory()
        run = rtcheck.measure_attach(0.5, inventory=inv, progress=lambda t: None)
        base, servo = run.stats
        self.assertEqual((base.thread, base.period), ("base-thread", 25000))
        self.assertEqual((servo.thread, servo.period), ("servo-thread", 1000000))
        self.assertEqual(servo.n, 2000)
        self.assertEqual(servo.max, 60000)
        self.assertEqual(servo.over[50], 1)
        cmds = self.cmds()
        self.assertIn("addf ecatsetup-rt-td-servo-thread servo-thread 1", cmds)
        self.assertIn("addf sampler.0 base-thread", cmds)
        # everything the check added is gone again
        self.assertIn("delf ecatsetup-rt-td-servo-thread servo-thread", cmds)
        self.assertEqual(cmds[-2:], ["unloadrt sampler", "unloadrt timedelta"])
        with open(self.state) as f:
            self.assertEqual(json.load(f)["comps"], ["halcmd123", "motmod", "stepgen"])
        self.assertTrue(any("since LinuxCNC started" in n for n in run.notes))
        b = {x.thread: x for x in run.budgets()}
        self.assertAlmostEqual(b["servo-thread"].thread_us, 0.67)
        # saved and read back
        path = run.save(os.path.join(self.tmp.name, "run.json"))
        back = rtcheck.Run.load_file(path)
        self.assertEqual(back.text(), run.text())
        out = io.StringIO()
        from ecatsetup import __main__ as cli
        with redirect_stdout(out):
            self.assertEqual(cli.from_run(path), 0)
        self.assertIn("servo-thread (1 ms): 2000 cycles", out.getvalue())

    def test_refuses_when_sampler_is_loaded(self):
        self.hal(["halcmd123", "motmod", "sampler"])
        with self.assertRaises(rtcheck.RtError) as e:
            rtcheck.measure_attach(0.5, inventory=rtcheck.Inventory(), progress=lambda t: None)
        self.assertIn("sampler loaded already", str(e.exception))
        self.assertFalse(any(c.startswith(("loadrt", "addf")) for c in self.cmds()))

    def test_no_hal(self):
        self.hal(["halcmd123"])
        with self.assertRaises(rtcheck.RtError):
            rtcheck.measure_attach(0.5, inventory=rtcheck.Inventory(), progress=lambda t: None)


if __name__ == "__main__":
    unittest.main()
