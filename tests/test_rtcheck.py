"""Tests of the real-time check (rtcheck.py) on fake /proc and /sys trees
and a fake halcmd/halsampler.

Run with: python3 -m unittest discover -s tests
"""

import io
import json
import os
import stat
import struct
import sys
import tempfile
import time
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
        self.roots = fake_tree(self.root)
        inv = rtcheck.inventory(self.roots)
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
        # the EtherCAT master's own real-time thread on another CPU (as on the
        # plasma cutter without irq-cpu): not a real-time threads' CPU
        rest = " ".join(["0"] * 35)
        write(self.roots.proc, "200/task/202/stat", "202 (irq_work) S %s 2 98 1 0 0\n" % rest)
        inv = rtcheck.inventory(self.roots)
        self.assertEqual(inv.data["rt_cpu"], [3])
        self.assertIn("other real-time threads of rtapi_app: irq_work on CPU 2", inv.fact("rt_cpu").value)
        self.assertEqual(rtcheck.hal_threads([(1, "irq_work", 2, 98), (2, "rtapi_app", 3, 98)]),
                         [(2, "rtapi_app", 3, 98)])
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


class TestTimeline(unittest.TestCase):
    def test_lateness(self):
        s = rtcheck.CycleStats("servo-thread", 1000000)
        # on time, then one cycle 80 us late (the next comes 80 us early), on time again
        s.feed("1000000\n" * 100 + "1080000\n920000\n" + "1000000\n" * 100)
        self.assertEqual(s.late_max, 80000)
        self.assertEqual(s.summary()["late_max_us"], 80.0)
        self.assertEqual(s.summary()["max_abs_us"], 80.0)
        # two late cycles in a row: the second moved 0 from the first, but is still late
        t = rtcheck.CycleStats("servo-thread", 1000000)
        t.feed("1000000\n" * 100 + "1050000\n1000000\n950000\n" + "1000000\n" * 10)
        self.assertEqual(t.late_max, 50000)
        late = [e for e in t.events if e[2] >= 50000]
        self.assertEqual(len(late), 2)
        # times: the sum of the deltas since the first sample
        self.assertEqual(late[0][0], 99 * 1000000 + 1050000)
        self.assertEqual(late[1][0], late[0][0] + 1000000)

    def test_slow_drift_is_not_lateness(self):
        # a PLL that lengthens every period by 20 ns: no lateness worth the name
        s = rtcheck.CycleStats("servo-thread", 1000000)
        s.feed("1000020\n" * 5000)
        self.assertLess(s.late_max, 1100)

    def test_lost_samples_end_the_time_line(self):
        s = rtcheck.CycleStats("servo-thread", 1000000)
        s.feed("1000000\n" * 10 + "overrun\n" + "1000000\n1060000\n")
        self.assertEqual(s.overruns, 1)
        self.assertIsNone(s.events[-1][0])
        st = rtcheck.CycleStats("x", 1000000)
        st.anchor = (0, 0)
        st.events = [(None, 60000, 60000)]
        self.assertEqual(rtcheck.spikes(st, 10000), [])

    def test_json_keeps_the_time_line(self):
        s = rtcheck.CycleStats("servo-thread", 1000000)
        s.anchor = (5000000000, 300000)
        s.feed("1000000\n" * 10 + "1070000\n930000\n")
        back = rtcheck.CycleStats.from_json(json.loads(json.dumps(s.to_json())))
        self.assertEqual(back.anchor, s.anchor)
        self.assertEqual(back.events, s.events)
        self.assertEqual(back.summary(), s.summary())


POLL = """\
# rtpoll 1 10000000 3
K 0 irq:LOC LOC: Local timer interrupts
K 1 smi SMIs (system management interrupts)
K 2 freq:3 CPU 3 frequency (kHz)
1000000000 0:1 2=800000
"""


def poll_text(n=1000, smi_at=(), loc_every=1):
    lines = [POLL.rstrip("\n")]
    for i in range(1, n):
        t = 1000000000 + i * 10000000
        parts = []
        if i % loc_every == 0:
            parts.append("0:1")
        if i in smi_at:
            parts.append("1:1")
        lines.append(" ".join([str(t)] + parts))
    return "\n".join(lines) + "\nE SMI count not read: test\n"


class TestCorrelation(unittest.TestCase):
    def test_parse(self):
        p = rtcheck.PollData.parse(poll_text(100, smi_at=(10, 20)))
        self.assertEqual(p.interval, 10000000)
        self.assertEqual(p.cpus, [3])
        self.assertEqual(len(p.times), 100)
        self.assertEqual(p.labels["smi"], "SMIs (system management interrupts)")
        self.assertEqual([d for _, d in p.changes["smi"]], [1, 1])
        self.assertEqual(p.values["freq:3"], [(1000000000, 800000)])
        self.assertEqual(p.errors, ["SMI count not read: test"])
        tot = p.totals()
        self.assertEqual(tot["smi"][1], 2)
        self.assertAlmostEqual(tot["irq:LOC"][2], 100 / 0.99, places=0)

    def stats_with_spikes(self, times_ms):
        """A 1 ms thread whose first sample is at 1.0 s, late by 80 us at times_ms (after it)."""
        s = rtcheck.CycleStats("servo-thread", 1000000)
        s.anchor = (1000000000, 50000)
        deltas = []
        for i in range(10000):
            deltas.append(1080000 if i in times_ms else 920000 if i - 1 in times_ms else 1000000)
        s.feed("\n".join(str(d) for d in deltas) + "\n")
        return s

    def test_spikes_line_up_with_smis(self):
        spikes_at = (1005, 3005, 5005, 7005)
        s = self.stats_with_spikes(spikes_at)
        # an SMI in the poll right after each spike (poll i covers ((i-1)*10, i*10] ms after 1 s)
        p = rtcheck.PollData.parse(poll_text(1000, smi_at=(101, 301, 501, 701, 900)))
        c = rtcheck.correlate(s, p)
        self.assertEqual(c["spikes"], 4)
        by = {x["key"]: x for x in c["counters"]}
        self.assertEqual(by["smi"]["with"], 4)
        self.assertTrue(rtcheck.strong(by["smi"]))
        # the timer interrupt grows in every poll: with every spike, but by chance
        self.assertEqual(by["irq:LOC"]["with"], 4)
        self.assertFalse(rtcheck.strong(by["irq:LOC"]))
        text = "\n".join(rtcheck.correlation_text(c))
        self.assertIn("4 of 4 with SMIs", text)
        self.assertIn("<- suspect", text)

    def test_smis_elsewhere_are_not_suspects(self):
        s = self.stats_with_spikes((1005, 3005, 5005, 7005))
        p = rtcheck.PollData.parse(poll_text(1000, smi_at=(150, 350, 550, 750)))
        c = rtcheck.correlate(s, p)
        self.assertNotIn("smi", [x["key"] for x in c["counters"]])
        # nor 20-30 ms after them: the window is one poll after the spike, and the anchor's uncertainty
        p = rtcheck.PollData.parse(poll_text(1000, smi_at=(103, 303, 503, 703)))
        c = rtcheck.correlate(s, p)
        self.assertNotIn("smi", [x["key"] for x in c["counters"]])
        self.assertAlmostEqual(c["window_ms"], 10.1, places=1)

    def test_bursts_are_one_spike(self):
        s = rtcheck.CycleStats("servo-thread", 1000000)
        # late by 80 us for three cycles, then on time; again 10 ms later
        block = "1000000\n" * 10 + "1080000\n1000000\n1000000\n920000\n"
        s.feed(block * 2 + "1000000\n" * 10)
        sp = rtcheck.spikes(s, 10000)
        self.assertEqual(len(sp), 2)
        self.assertEqual(sp[0][1], 80000)

    def test_no_rtla_beside_linuxcnc(self):
        run = rtcheck.Run("attach", "as-used", 10, rtcheck.Inventory(), root=True)
        run.rt_cpus = [3]
        with mock.patch.object(rtcheck.subprocess, "Popen") as popen, \
                mock.patch.object(rtcheck.shutil, "which", return_value="/usr/bin/rtla"):
            self.assertIsNone(rtcheck.start_rtla(run, 10, 50))
            self.assertFalse(popen.called)
            self.assertIn("rtla timerlat was not run beside LinuxCNC", run.notes[-1])
            run = rtcheck.Run("threads", "rest", 10, rtcheck.Inventory(), root=True)
            run.rt_cpus = [3]
            rtcheck.start_rtla(run, 10, 50)
            self.assertTrue(popen.called)

    def test_rtla(self):
        d = os.path.join(os.path.dirname(__file__), "data", "rt")
        with open(os.path.join(d, "rtla-hit.txt")) as f:
            hit = rtcheck.parse_rtla(f.read())
        self.assertTrue(hit["hit"])
        # the spike it stopped at (12.03 us) is larger than the table's 9
        self.assertEqual(hit["cpus"]["3"]["thread_max_us"], 13)
        self.assertIn("Blocking thread", hit["analysis"])
        with open(os.path.join(d, "rtla-nohit.txt")) as f:
            no = rtcheck.parse_rtla(f.read())
        self.assertFalse(no["hit"])
        self.assertEqual(no["cpus"]["3"], {"count": 2998, "irq_max_us": 5, "thread_max_us": 18})
        hit["threshold_us"] = 12
        self.assertIn("first wake-up over 12 us", "\n".join(rtcheck.rtla_text(hit)))


class TestPoller(unittest.TestCase):
    def test_on_a_fake_tree(self):
        from ecatsetup import rtpoll
        with tempfile.TemporaryDirectory() as root:
            roots = fake_tree(root)
            write(roots.sys, "firmware/acpi/interrupts/gpe_all", "  100\n")
            p = rtpoll.Poller([3], proc=roots.proc, sys_=roots.sys, dev=os.path.join(root, "dev"), smi=True)
            self.assertTrue(any("SMI count not read" in x for x in p.out))
            p.step(1000)
            write(roots.proc, "interrupts", INTERRUPTS.replace("91234", "91334"))
            write(roots.sys, "firmware/acpi/interrupts/gpe_all", "  103\n")
            write(roots.sys, "devices/system/cpu/cpu3/cpuidle/state1/usage", "107\n")
            p.step(2000)
            data = rtcheck.PollData.parse("# rtpoll 1 10000000 3\n" + "\n".join(p.out) + "\n")
        self.assertEqual(data.changes["irq:125"], [(2000, 100)])
        self.assertEqual(data.labels["irq:125"], "IRQ 125 (enp2s0)")
        self.assertEqual(data.changes["gpe:all"], [(2000, 3)])
        self.assertEqual(data.changes["idle:3:C1E"], [(2000, 7)])
        # CPU 0's counts are not this poller's business
        self.assertNotIn("irq:126", data.changes)


class FakeProbe:
    """ProbeReader's interface over lists: runs are added with run()."""
    slots = 64 + 32

    def __init__(self):
        self.names = ["ecatsetup-rt-servo-thread"]
        self.ring = [0] * self.slots
        self.n = 0

    def run(self, t):
        self.ring[self.n % self.slots] = t
        self.n += 1

    def count(self, i):
        return self.n

    def times(self, i, start, end):
        return [self.ring[k % self.slots] for k in range(start, end)]

    def close(self):
        pass


class TestProbe(unittest.TestCase):
    def test_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "probe")
            write_probe(path, [("ecatsetup-rt-base-thread", 25000, [1, 2]),
                               ("ecatsetup-rt-servo-thread", 1000000, list(range(70000)))])
            r = rtcheck.ProbeReader(path)
            self.assertEqual(r.names, ["ecatsetup-rt-base-thread", "ecatsetup-rt-servo-thread"])
            self.assertEqual((r.count(0), r.count(1), r.period(1), r.slots), (2, 70000, 1000000, 65536))
            self.assertEqual(r.times(0, 0, 2), [1, 2])
            # across the end of the ring
            self.assertEqual(r.times(1, 65530, 65540), list(range(65530, 65540)))
            r.close()
            with open(path, "r+b") as f:
                f.write(b"NOTAPROB")
            with self.assertRaises(rtcheck.RtError):
                rtcheck.ProbeReader(path)

    def recorder(self, probe):
        rec = rtcheck.Recorder([("servo-thread", 1000000)])
        rec.reader, rec.index, rec.next, rec.last = probe, [0], [probe.n], [None]
        return rec

    def test_the_runs_become_cycles(self):
        p = FakeProbe()
        for k in range(5):
            p.run(10 ** 9 + k * 1000000)          # before the start: not counted
        rec = self.recorder(p)
        st = rec.stats[0]
        for k in range(5, 15):
            p.run(10 ** 9 + k * 1000000 + (40000 if k == 9 else 0))
        rec.read()
        # 10 runs read: 9 cycles; the anchor is the second run's time
        self.assertEqual(st.n, 9)
        self.assertEqual(st.anchor, (10 ** 9 + 6 * 1000000, 0))
        self.assertEqual((st.max, st.min), (40000, -40000))
        self.assertEqual(st.events[0][0], 3 * 1000000 + 40000)     # the late run's time since the anchor
        self.assertEqual(st.late_max, 40000)

    def test_start_skips_what_came_before(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"ECAT_RTPROBE_FILE": os.path.join(d, "probe")}):
            write_probe(os.path.join(d, "probe"), [("ecatsetup-rt-servo-thread", 1000000,
                                                    [k * 1000000 for k in range(500)])])
            rec = rtcheck.Recorder([("servo-thread", 1000000), ("base-thread", 25000)])
            rec.start()
            rec.stop()
            self.assertEqual([st.n for st in rec.stats], [0, 0])
            self.assertEqual(rec.errors, ["base-thread: the probe has no function for this thread"])

    def test_runs_lost_when_read_too_late(self):
        p = FakeProbe()
        rec = self.recorder(p)
        st = rec.stats[0]
        for k in range(10):
            p.run(k * 1000000)
        rec.read()
        for k in range(10, 10 + p.slots + 5):        # the ring went round before it was read
            p.run(k * 1000000)
        rec.read()
        self.assertEqual(st.overruns, 1)
        # no cycle of the gap counts; the time line goes on from the probe's own times
        self.assertEqual((st.max, st.min), (0, 0))
        self.assertEqual(st.t, (10 + p.slots + 4) * 1000000 - 1000000)


class TestHal(unittest.TestCase):
    def test_threads_hal(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("ECAT_RTPROBE_FILE", None)
            text = rtcheck.threads_hal([("base-thread", 25000), ("servo-thread", 1000000)])
        lines = text.splitlines()
        self.assertEqual(lines[0], "loadrt threads name1=base-thread period1=25000 "
                                   "name2=servo-thread period2=1000000")
        # one probe function per thread, first in it; nothing of sampler or timedelta
        self.assertIn("loadrt ecat_rtprobe names=ecatsetup-rt-base-thread,ecatsetup-rt-servo-thread", lines)
        self.assertIn("addf ecatsetup-rt-servo-thread servo-thread 1", lines)
        self.assertIn("addf ecatsetup-rt-base-thread base-thread 1", lines)
        self.assertFalse([l for l in lines if "sampler" in l or "timedelta" in l])
        self.assertEqual(lines[-1], "start")
        with mock.patch.dict(os.environ, {"ECAT_RTPROBE_FILE": "/x/p"}):
            self.assertIn("file=/x/p", rtcheck.attach_commands(["t"])[0])

    def test_probe_error(self):
        self.assertIn("not installed", rtcheck.probe_error("dlopen: ecat_rtprobe.so: cannot open shared object"))
        self.assertIn("did not load", rtcheck.probe_error("rtapi_app_main: -22"))

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
if args and args[0] == "-kf":
    for line in sys.stdin:
        with open(state + ".cmds", "a") as f:
            f.write(line)
        if line.startswith("getp"):
            print("TRUE", flush=True)
    sys.exit(0)
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
    if args[1] == "ecat_rtprobe" and st.get("no_probe"):
        print("dlopen: ecat_rtprobe.so: cannot open shared object file", file=sys.stderr)
        sys.exit(1)
    st["comps"].append(args[1])
    if args[1] == "ecat_rtprobe":
        kw = dict(a.split("=", 1) for a in args[2:])
        import subprocess
        subprocess.Popen([sys.executable, os.environ["FAKE_PROBE_WRITER"], kw["file"], kw["names"]],
                         start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        for _ in range(100):
            if os.path.exists(kw["file"]):
                break
            import time
            time.sleep(0.02)
elif args and args[0] == "unloadrt":
    st["comps"].remove(args[1])
    if args[1] == "ecat_rtprobe" and os.path.exists(os.environ["ECAT_RTPROBE_FILE"]):
        os.remove(os.environ["ECAT_RTPROBE_FILE"])
json.dump(st, open(state, "w"))
"""

def write_probe(path, insts):
    """An ecat_rtprobe file (component/ecat_rtprobe_core.h; its C test checks
    the same offsets): insts = [(name, period, [times])]."""
    slots = 65536
    data = bytearray(64 + 128 * len(insts) + 8 * slots * len(insts))
    struct.pack_into("<8sIIIIq", data, 0, b"ECATRTP\0", 1, len(insts), slots, 128, 5)
    for i, (name, period, times) in enumerate(insts):
        o = 64 + 128 * i
        data[o:o + len(name)] = name.encode()
        struct.pack_into("<qQ", data, o + 64, period, len(times))
        base = 64 + 128 * len(insts) + i * slots * 8
        for k, t in enumerate(times):
            struct.pack_into("<q", data, base + (k % slots) * 8, t)
    with open(path, "wb") as f:
        f.write(data)


# What ecat_rtprobe does in a running HAL, for the fake halcmd: the file is
# made at loadrt and written as time goes on (base 25 us, servo 1 ms, every
# 20th servo run 60 us late), until unloadrt removes it.
FAKE_PROBE_WRITER = """\
import mmap, os, struct, sys, time
path, names = sys.argv[1], sys.argv[2].split(",")
periods = [25000 if "base" in n else 1000000 for n in names]
slots = 65536
size = 64 + 128 * len(names) + 8 * slots * len(names)
with open(path + ".tmp", "wb") as f:
    f.truncate(size)
os.rename(path + ".tmp", path)
f = open(path, "r+b")
m = mmap.mmap(f.fileno(), size)
struct.pack_into("<8sIIIIq", m, 0, b"ECATRTP\\0", 1, len(names), slots, 128, 5)
for i, n in enumerate(names):
    m[64 + 128 * i:64 + 128 * i + len(n)] = n.encode()
    struct.pack_into("<q", m, 64 + 128 * i + 64, periods[i])
t0 = time.monotonic_ns()
counts = [0] * len(names)
end = time.monotonic() + 30
while os.path.exists(path) and time.monotonic() < end:
    now = time.monotonic_ns() - t0
    for i, p in enumerate(periods):
        base = 64 + 128 * len(names) + i * slots * 8
        while counts[i] * p <= now:
            k = counts[i]
            t = t0 + k * p + (60000 if p == 1000000 and k % 20 == 7 else 0)
            struct.pack_into("<q", m, base + (k % slots) * 8, t)
            counts[i] += 1
        struct.pack_into("<Q", m, 64 + 128 * i + 72, counts[i])
    time.sleep(0.005)
"""

# the poller: a file of its format, written at once; then it waits to be stopped
FAKE_RTPOLL = """\
#!/usr/bin/python3
import os, sys, time
out = sys.argv[sys.argv.index("--out") + 1]
with open(out, "w") as f:
    f.write(open(os.environ["FAKE_RTPOLL_DATA"]).read())
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
        for name, text in (("halcmd", FAKE_HALCMD), ("probe_writer.py", FAKE_PROBE_WRITER),
                           ("rtpoll", FAKE_RTPOLL)):
            p = os.path.join(d, name)
            with open(p, "w") as f:
                f.write(text)
            os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        self.env = mock.patch.dict(os.environ, {
            "ECAT_HALCMD": os.path.join(d, "halcmd"), "FAKE_PROBE_WRITER": os.path.join(d, "probe_writer.py"),
            "ECAT_RTPROBE_FILE": os.path.join(d, "probe"),
            "FAKE_RT_STATE": self.state, "ECAT_RTPOLL": os.path.join(d, "rtpoll"),
            "FAKE_RTPOLL_DATA": os.path.join(d, "poll.txt")})
        with open(os.path.join(d, "poll.txt"), "w") as f:
            f.write("# rtpoll 1 10000000 3\nK 0 irq:LOC LOC: Local timer interrupts\n"
                    "100000000\n110000000 0:12\n")
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
        # about 0.5 s of runs, read as they came (the ones before the start are not counted)
        self.assertGreater(servo.n, 200)
        self.assertLess(servo.n, 3000)
        self.assertGreater(base.n, 40 * servo.n - 2000)
        self.assertEqual(servo.max, 60000)
        self.assertEqual(servo.min, -60000)
        self.assertGreaterEqual(servo.over[50], 2)          # each late run, and the short one after it
        self.assertGreater(servo.late_max, 59000)
        cmds = self.cmds()
        self.assertIn("addf ecatsetup-rt-servo-thread servo-thread 1", cmds)
        self.assertIn("addf ecatsetup-rt-base-thread base-thread 1", cmds)
        # the anchor is the probe's own time of the first cycle: exact, on the poller's clock
        self.assertEqual(servo.anchor[1], 0)
        self.assertLess(abs(servo.anchor[0] - time.monotonic_ns()), 10e9)
        self.assertTrue(all(e[0] is not None for e in servo.events))
        # the poller's file was read
        self.assertEqual(run.poll["counters"]["irq:LOC"]["total"], 12)
        # everything the check added is gone again
        self.assertIn("delf ecatsetup-rt-servo-thread servo-thread", cmds)
        self.assertEqual(cmds[-1], "unloadrt ecat_rtprobe")
        self.assertFalse(os.path.exists(os.environ["ECAT_RTPROBE_FILE"]))
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
        self.assertIn("servo-thread (1 ms): %d cycles" % servo.n, out.getvalue())

    def test_beside_a_sampler_of_its_own(self):
        # the configuration's own sampler and timedelta are no obstacle any more
        self.hal(["halcmd123", "motmod", "sampler", "timedelta"])
        run = rtcheck.measure_attach(0.3, inventory=rtcheck.Inventory(), progress=lambda t: None)
        self.assertGreater(run.stats[1].n, 100)
        with open(self.state) as f:
            self.assertEqual(json.load(f)["comps"], ["halcmd123", "motmod", "sampler", "timedelta"])

    def test_refuses_when_the_probe_is_loaded(self):
        self.hal(["halcmd123", "motmod", "ecat_rtprobe"])
        with self.assertRaises(rtcheck.RtError) as e:
            rtcheck.measure_attach(0.5, inventory=rtcheck.Inventory(), progress=lambda t: None)
        self.assertIn("ecat_rtprobe is loaded already", str(e.exception))
        self.assertFalse(any(c.startswith(("loadrt", "addf")) for c in self.cmds()))

    def test_probe_not_installed(self):
        self.hal(["halcmd123", "motmod"])
        with open(self.state) as f:
            st = json.load(f)
        st["no_probe"] = True
        with open(self.state, "w") as f:
            json.dump(st, f)
        with self.assertRaises(rtcheck.RtError) as e:
            rtcheck.measure_attach(0.5, inventory=rtcheck.Inventory(), progress=lambda t: None)
        self.assertIn("not installed", str(e.exception))
        self.assertFalse(any(c.startswith("addf") for c in self.cmds()))

    def test_no_hal(self):
        self.hal(["halcmd123"])
        with self.assertRaises(rtcheck.RtError):
            rtcheck.measure_attach(0.5, inventory=rtcheck.Inventory(), progress=lambda t: None)


if __name__ == "__main__":
    unittest.main()
