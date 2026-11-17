"""Tests of the real-time check's rule catalogue (rtrules.py and
ecatsetup/rtrules/*.yaml).

Every rule has evidence that makes it apply and evidence that does not, in
its own `tests:`; they are applied here to "a good computer" built the way a
real run is (a fake /proc and /sys tree, a measured run). The names a rule
uses must be names the evidence has, so that a typo cannot make a rule
silently never apply.

Run with: python3 -m unittest discover -s tests
"""

import ast
import copy
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("ECATSETUP_LINUXCNC_SERIES", "2.10")
os.environ["ECATSETUP_SETTINGS"] = os.path.join(os.path.dirname(__file__), "no-such-dir", "settings.yaml")
if "ECATSETUP_TEST_STATE" not in os.environ:
    os.environ["ECATSETUP_TEST_STATE"] = os.environ["XDG_STATE_HOME"] = os.environ["TMPDIR"] = \
        __import__("tempfile").tempdir = __import__("tempfile").mkdtemp(prefix="ecatsetup-test-state-")
    __import__("atexit").register(__import__("shutil").rmtree, os.environ["ECATSETUP_TEST_STATE"], True)

from ecatsetup import rtcheck, rtrules  # noqa: E402
from test_rtcheck import fake_tree, write  # noqa: E402

SHIPPED = [rtrules.HERE]


def good_run(inv, late_us=8, poll=None, gpe_rates=None, correlations=None):
    """A measured 1 ms servo thread, late at most late_us."""
    run = rtcheck.Run("threads", "stress", 60, inv)
    s = rtcheck.CycleStats("servo-thread", 1000000)
    s.feed("1000000\n" * 200 + "%d\n%d\n" % (1000000 + late_us * 1000, 1000000 - late_us * 1000) +
           "1000000\n" * 200)
    run.stats = [s]
    run.rt_cpus = [3]
    run.times = {"servo-thread": {"period": 1000000, "tmax_us": 20.0,
                                  "funcs": {"ecat.read-all": 9.0, "motion-command-handler": 4.0}}}
    run.poll = poll or {"counters": {}, "values": {}}
    run.gpe_rates = gpe_rates or {}
    run.correlations = correlations or {}
    return run


class Base:
    """The evidence of a good computer: no rule applies."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = cls.tmp.name
        roots = fake_tree(root)
        os.remove(os.path.join(roots.proc, "100", "comm"))           # no irqbalance
        for c in range(4):
            write(roots.sys, "devices/system/cpu/cpu%d/cpufreq/scaling_governor" % c, "performance\n")
        write(roots.sys, "module/pcie_aspm/parameters/policy", "default [performance] powersave\n")
        cls.roots = roots
        cls.inv = rtcheck.inventory(roots)
        cls.rules = rtrules.load(SHIPPED)
        cls.base = rtrules.evidence(cls.inv, good_run(cls.inv))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()


class TestCatalogue(Base, unittest.TestCase):
    def test_a_good_computer_gives_nothing(self):
        found = rtrules.analyse(self.base, self.rules)
        self.assertEqual([f.id for f in found], [])
        self.assertIn("No rule applies", rtrules.report(found, self.base))

    def test_every_rule_has_its_own_tests(self):
        self.assertGreaterEqual(len(self.rules), 30)
        for r in self.rules.values():
            t = r["tests"] or {}
            self.assertIn("present", t, r.id)
            self.assertIn("absent", t, r.id)
            self.assertTrue(r["source"], r.id)
            self.assertIn("seen-here", r.d, r.id)

    def ctx(self, overrides):
        c = copy.deepcopy(self.base)
        c.update(copy.deepcopy(overrides))
        return c

    def test_each_rule_applies_on_its_evidence_and_not_without(self):
        for r in self.rules.values():
            with self.subTest(rule=r.id):
                present = rtrules.analyse(self.ctx(r["tests"]["present"]), {r.id: r})
                self.assertEqual([f.id for f in present], [r.id], "present")
                absent = rtrules.analyse(self.ctx(r["tests"]["absent"]), {r.id: r})
                self.assertEqual([f.id for f in absent if f.id == r.id], [], "absent")

    def test_texts_have_every_name_filled(self):
        for r in self.rules.values():
            with self.subTest(rule=r.id):
                f, = [x for x in rtrules.analyse(self.ctx(r["tests"]["present"]), {r.id: r})]
                texts = [f.title, f.evidence, f.cause, f.action, f.effect, f.check, f.undo, f.kernel_param]
                texts += f.commands + f.undo_commands
                for t in texts:
                    if t:
                        self.assertNotIn("?", t.replace("?)", ""), t)
                if f.kind == "runtime" and f.commands:
                    self.assertTrue(f.undo_commands)

    def test_names_are_the_evidence_names(self):
        """A rule's names, and its item fields, exist in real evidence."""
        known = set(self.base) | {"item", "True", "False", "None"}
        # the item lists from a tree that has items in each
        items = {k: v for k, v in self.base.items() if isinstance(v, list) and v and isinstance(v[0], dict)}
        items.setdefault("funcs", self.base["funcs"])
        for r in self.rules.values():
            tree = ast.parse(str(r["when"]), mode="eval")
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id not in rtrules.FUNCTIONS:
                    self.assertIn(node.id, known, "%s: %s" % (r.id, node.id))
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                        and node.value.id == "item":
                    self.assertTrue(items.get(r.each), "%s: no items of %s to check" % (r.id, r.each))
                    self.assertIn(node.attr, items[r.each][0], "%s: item.%s" % (r.id, node.attr))
            for t in [r["title"], r["evidence"]] + list((r["action"] or {}).get("commands") or []):
                for m in rtrules._FIELD.finditer(str(t or "")):
                    head = m.group(1).split(".")[0]
                    self.assertIn(head, known, "%s: {%s}" % (r.id, m.group(1)))
                    if head == "item" and "." in m.group(1):
                        self.assertIn(m.group(1).split(".")[1], items[r.each][0], "%s: {%s}" % (r.id, m.group(1)))


class TestExpressions(unittest.TestCase):
    def ev(self, text, **ctx):
        return rtrules.evaluate(rtrules.compile_expr(text), ctx)

    def test_values(self):
        self.assertTrue(self.ev("a and b > 2", a=True, b=3))
        self.assertTrue(self.ev("'x' in l", l=["x"]))
        self.assertTrue(self.ev("'x' not in l", l=None))
        self.assertFalse(self.ev("missing > 2"))                # None compared: false
        self.assertEqual(self.ev("item.rate * 2", item={"rate": 3}), 6)
        self.assertIsNone(self.ev("item.nope", item={}))
        self.assertEqual(self.ev("cmd['i915.enable_dc']", cmd={"i915.enable_dc": "0"}), "0")
        self.assertTrue(self.ev("re(n, 'xhci|ehci')", n="xhci_hcd"))
        self.assertEqual(self.ev("set(a) & set(b)", a=[1, 2], b=[2]), {2})
        self.assertEqual(self.ev("len(x)", x=None), 0)
        self.assertIsNone(self.ev("a / b", a=1, b=0))

    def test_refused(self):
        for text in ("item.__class__", "open('x')", "(lambda: 1)()", "[x for x in y]",
                     "a.b()", "__import__('os')"):
            with self.subTest(text=text):
                with self.assertRaises(rtrules.RuleError):
                    rtrules.compile_expr(text)

    def test_fill(self):
        c = {"a": 3.14159, "item": {"irq": "40", "aff": [0, 1, 2]}, "l": ["x", "y"]}
        self.assertEqual(rtrules.fill("{a:.1f} {item.irq} {item.aff} {l}", c), "3.1 40 0-2 x, y")
        self.assertEqual(rtrules.fill("{nope} {item.__class__}", c), "? ?")


RULE = """
- id: %(id)s
  title: T
  when: "%(when)s"
  gain: low
  cause: c
  action: {kind: %(kind)s, text: t%(cmds)s}
  source: [{text: s}]
  seen-here: null
  tests: {present: {}, absent: {}}
"""


class TestLoading(unittest.TestCase):
    def write(self, d, name, **kw):
        kw.setdefault("cmds", "")
        kw.setdefault("kind", "advice")
        with open(os.path.join(d, name), "w") as f:
            f.write(RULE % kw)

    def test_user_rule_replaces_shipped(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            self.write(a, "x.yaml", id="r1", when="vm")
            self.write(b, "y.yaml", id="r1", when="battery")
            with mock.patch.dict(os.environ, {"ECATSETUP_RTRULES": a + os.pathsep + b}):
                r = rtrules.load()
            self.assertEqual(r["r1"]["when"], "battery")

    def test_bad_rules_are_refused(self):
        with tempfile.TemporaryDirectory() as a:
            self.write(a, "x.yaml", id="r1", when="vm", kind="runtime", cmds=", commands: [x]")
            with self.assertRaisesRegex(rtrules.RuleError, "undo commands"):
                rtrules.load([a])
        with tempfile.TemporaryDirectory() as a:
            self.write(a, "x.yaml", id="r1", when="item.__dict__")
            with self.assertRaises(rtrules.RuleError):
                rtrules.load([a])
        with tempfile.TemporaryDirectory() as a:
            self.write(a, "x.yaml", id="r1", when="vm", kind="soon")
            with self.assertRaisesRegex(rtrules.RuleError, "kind"):
                rtrules.load([a])


class TestAnalysis(Base, unittest.TestCase):
    def test_fallback_only_for_items_nobody_explains(self):
        c = copy.deepcopy(self.base)
        c["irqs"] = [{"irq": "125", "name": "xhci_hcd", "on_rt": True, "managed": False, "rate_rt": 50,
                      "affinity_text": "3", "io_nic": False},
                     {"irq": "40", "name": "foo", "on_rt": True, "managed": False, "rate_rt": 50,
                      "affinity_text": "3", "io_nic": False}]
        found = rtrules.analyse(c, self.rules)
        ids = sorted((f.id, f.item["irq"]) for f in found)
        self.assertEqual(ids, [("irq-on-rt-cpu", "40"), ("usb-irq-on-rt-cpu", "125")])
        usb = [f for f in found if f.id == "usb-irq-on-rt-cpu"][0]
        self.assertEqual(usb.commands, ["echo 0-2 > /proc/irq/125/smp_affinity_list"])
        self.assertEqual(usb.undo_commands, ["echo 3 > /proc/irq/125/smp_affinity_list"])

    def test_unexplained_suspects(self):
        c = copy.deepcopy(self.base)
        sus = [{"key": "irq:RES", "label": "RES: Rescheduling interrupts", "with": 15, "of": 23,
                "expected": 0.2, "lift": 3.3},
               {"key": "idle:3:C6", "label": "CPU 3 enters idle state C6", "with": 9, "of": 10,
                "expected": 0.1, "lift": 9}]
        c.update(suspect_items=sus, suspects=[x["key"] for x in sus], idle_suspect=True,
                 deep_states=[{"cpu": 3, "state": "state2", "name": "C6", "latency_us": 170, "path": "/p"}],
                 deepest_idle_us=170, deep_states_text="C6 (170 us)", disable_deep_cmds="echo 1 > /p",
                 enable_deep_cmds="echo 0 > /p")
        found = rtrules.analyse(c, self.rules)
        ids = [f.id for f in found]
        self.assertIn("deep-idle-states", ids)          # explains idle:3:C6 (its key "idle:")
        un = [f for f in found if f.id == "unexplained"]
        self.assertEqual([f.keys for f in un], [["irq:RES"]])
        self.assertIn("15 of 23", un[0].text())
        # runtime and permanent changes first, advice last
        self.assertLess(ids.index("deep-idle-states"), ids.index("unexplained"))


class TestEndToEnd(unittest.TestCase):
    """From a fake tree and a fake run to the finding, as a user's run goes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.rules = rtrules.load(SHIPPED)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_gpe_storm(self):
        roots = fake_tree(self.root)
        write(roots.sys, "firmware/acpi/interrupts/gpe6F", "  450123  EN  enabled  unmasked\n")
        write(roots.sys, "firmware/acpi/interrupts/gpe6D", "      36  EN  enabled  unmasked\n")
        inv = rtcheck.inventory(roots)
        self.assertEqual(inv.data["gpes"], {"gpe6F": 450123, "gpe6D": 36})
        corr = {"servo-thread": {"threshold_us": 40, "spikes": 12, "window_ms": 11, "counters": [
            {"key": "gpe:all", "label": "ACPI GPEs (all)", "with": 11, "of": 12, "expected": 0.05, "lift": 18}]}}
        run = good_run(inv, late_us=310, gpe_rates={"gpe6F": 41.5}, correlations=corr,
                       poll={"counters": {"gpe:all": {"label": "ACPI GPEs (all)", "total": 2490, "per_s": 41.5}},
                             "values": {}})
        found = rtrules.analyse(rtrules.evidence(inv, run), self.rules)
        gpe = [f for f in found if f.id == "acpi-gpe-storm"]
        self.assertEqual(len(gpe), 1)
        self.assertEqual(gpe[0].commands, ["echo disable > /sys/firmware/acpi/interrupts/gpe6F"])
        self.assertEqual(gpe[0].kernel_param, "acpi_mask_gpe=0x6F")
        self.assertNotIn("unexplained", [f.id for f in found])     # the GPE rule explains gpe:all

    def test_ethercat_without_irq_cpu(self):
        """The training case here: irq-cpu taken away, the card's interrupts on CPUs 0-2."""
        roots = fake_tree(self.root)
        for irq in ("125", "126"):
            write(roots.proc, "irq/%s/effective_affinity_list" % irq, "0-2\n")
        cfg = os.path.join(self.root, "cfg")
        write(cfg, "m.ini", "[EMCMOT]\nSERVO_PERIOD = 250000\n[HAL]\nHALFILE = m.hal\n")
        write(cfg, "m.hal", "loadrt ecat config=ethercat.yaml\n")
        write(cfg, "ethercat.yaml", "master:\n  interface: enp2s0\nslaves: []\n")
        inv = rtcheck.inventory(roots, ini=os.path.join(cfg, "m.ini"))
        self.assertEqual(inv.data["io_nics"], ["enp2s0"])
        ctx = rtrules.evidence(inv, None)
        found = [f for f in rtrules.analyse(ctx, self.rules) if f.id == "ethercat-nic-irq-cpu"]
        self.assertEqual(sorted(f.item["irq"] for f in found), ["125", "126"])
        self.assertIn("irq-cpu: 3", found[0].action)
        # with irq-cpu set (on the real-time CPU), nothing to say, and the
        # general "move it off" rule leaves EtherCAT's card alone
        for irq in ("125", "126"):
            write(roots.proc, "irq/%s/effective_affinity_list" % irq, "3\n")
        inv = rtcheck.inventory(roots, ini=os.path.join(cfg, "m.ini"))
        ids = [f.id for f in rtrules.analyse(rtrules.evidence(inv, good_run(inv, poll={"counters": {
            "irq:125": {"label": "IRQ 125 (enp2s0)", "total": 1000, "per_s": 500}}, "values": {}})), self.rules)]
        self.assertNotIn("ethercat-nic-irq-cpu", ids)
        self.assertNotIn("irq-on-rt-cpu", ids)

    def test_mesa_card_found_by_its_address(self):
        roots = fake_tree(self.root)
        # 10.10.10.0/24 on enp2s0 (little-endian hex, as the kernel writes it)
        write(roots.proc, "net/route", "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
              "wlan0\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0\n"
              "enp2s0\t000A0A0A\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n")
        cfg = os.path.join(self.root, "cfg")
        write(cfg, "m.ini", "[EMCMOT]\nSERVO_PERIOD = 1000000\n[HAL]\nHALFILE = m.hal\n")
        write(cfg, "m.hal", 'loadrt hostmot2\nloadrt hm2_eth board_ip="10.10.10.10"\n')
        inv = rtcheck.inventory(roots, ini=os.path.join(cfg, "m.ini"))
        self.assertEqual(inv.data["io_nics"], ["enp2s0"])
        self.assertEqual(inv.data["default_route"], "wlan0")
        ids = {f.id: f for f in rtrules.analyse(rtrules.evidence(inv, None), self.rules)}
        self.assertEqual(ids["mesa-nic-coalescing"].commands, ["ethtool -C enp2s0 rx-usecs 0"])
        self.assertEqual(ids["mesa-nic-coalescing"].undo_commands, ["ethtool -C enp2s0 rx-usecs 3"])
        self.assertIn("io-nic-eee", ids)
        self.assertNotIn("io-nic-carries-lan", ids)

    def test_run_from_json(self):
        """A saved run gives the same findings (a user's file becomes a test)."""
        roots = fake_tree(self.root)
        inv = rtcheck.inventory(roots)
        run = good_run(inv, late_us=60)
        run.stats[0].anchor = (0, 0)
        back = rtcheck.Run.from_json(json.loads(json.dumps(run.to_json())))
        a = [f.to_json() for f in rtrules.analyse(rtrules.evidence(inv, run), self.rules)]
        b = [f.to_json() for f in rtrules.analyse(rtrules.evidence(back.inventory, back), self.rules)]
        self.assertEqual(a, b)
        self.assertTrue(a)


class TestRecordedRun(unittest.TestCase):
    """A real run (here, 2026-11-17: threads only, 30 s under stress-ng, as root):
    its findings come out the same from the file."""

    def test_findings(self):
        path = os.path.join(os.path.dirname(__file__), "data", "rt", "run-n100-stress-root.json")
        run = rtcheck.Run.load_file(path)
        self.assertTrue(run.root and run.smi_measured)
        self.assertEqual(run.rt_cpus, [3])
        ctx = rtrules.evidence(run.inventory, run)
        self.assertGreater(ctx["late_max_us"], 20)
        self.assertEqual(ctx["smi_count"], 0)
        found = rtrules.analyse(ctx, rtrules.load(SHIPPED))
        ids = [f.id for f in found]
        for rule in ("governor-not-performance", "turbo-on", "i915-display-power"):
            self.assertIn(rule, ids)
        # EtherCAT's card on the real-time CPU is right: no rule moves it
        self.assertNotIn("irq-on-rt-cpu", ids)
        self.assertNotIn("isolcpus-without-managed-irq", ids)
        un = [f for f in found if f.id == "unexplained"]
        self.assertEqual([f.keys for f in un], [["irq:RES"]])


if __name__ == "__main__":
    unittest.main()
