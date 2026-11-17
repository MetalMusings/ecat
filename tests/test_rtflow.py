"""Tests of the real-time check's guided work (rtflow.py): the steps, the
change log, sudo, the undo script and the report, against a fake sudo.

Run with: python3 -m unittest discover -s tests
"""

import os
import subprocess
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

from ecatsetup import rtcheck, rtflow, rtrules  # noqa: E402
from test_rtcheck import fake_tree, write  # noqa: E402
from test_rtrules import good_run  # noqa: E402


class FakeSudo:
    """subprocess.run for `sudo -n sh -c CMD`: records, fails on demand."""

    def __init__(self, fail=(), password=False):
        self.calls = []
        self.fail = fail
        self.password = password

    def __call__(self, argv, **kw):
        assert argv[:4] == ["sudo", "-n", "sh", "-c"], argv
        cmd = argv[4]
        self.calls.append(cmd)
        if self.password:
            return subprocess.CompletedProcess(argv, 1, "", "sudo: a password is required\n")
        if any(f in cmd for f in self.fail):
            return subprocess.CompletedProcess(argv, 1, "", "sh: 1: cannot create: Input/output error\n")
        return subprocess.CompletedProcess(argv, 0, "", "")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": os.path.join(self.tmp.name, "state")})
        self.env.start()
        self.roots = fake_tree(os.path.join(self.tmp.name, "tree"))
        self.rules = rtrules.load([rtrules.HERE])

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def finding(self, rule_id, inv=None, run=None):
        inv = inv or rtcheck.inventory(self.roots)
        found = rtrules.analyse(rtrules.evidence(inv, run), self.rules)
        return next(f for f in found if f.id == rule_id)

    def irq_finding(self):
        """IRQ 125 (enp2s0) on the real-time CPU 3: a run-time move."""
        inv = rtcheck.inventory(self.roots)
        run = good_run(inv, poll={"counters": {"irq:125": {"label": "IRQ 125", "total": 9000, "per_s": 150}},
                                  "values": {}})
        return self.finding("irq-on-rt-cpu", inv, run)


class TestChanges(Base):
    def test_try_and_undo(self):
        f = self.irq_finding()
        log = rtflow.ChangeLog()
        sudo = FakeSudo()
        c = rtflow.try_change(log, f, "/runs/a.json", runner=sudo)
        self.assertEqual(sudo.calls, ["echo 0-2 > /proc/irq/125/smp_affinity_list"])
        self.assertEqual((c.state, c.rule, c.item, c.run_before), ("tried", "irq-on-rt-cpu", "125", "/runs/a.json"))
        # the log is a file a user can read, and it is read back
        with open(rtflow.changes_path()) as fh:
            text = fh.read()
        self.assertIn("smp_affinity_list", text)
        self.assertIn("Undo by hand", text)
        back = rtflow.ChangeLog()
        self.assertEqual(back.changes[0].undo_commands, ["echo 3 > /proc/irq/125/smp_affinity_list"])
        # not twice
        with self.assertRaisesRegex(rtflow.SudoError, "in force already"):
            rtflow.try_change(log, f, runner=sudo)
        # the undo script has it, and nothing once it is undone
        with open(rtflow.undo_script_path()) as fh:
            script = fh.read()
        self.assertTrue(script.startswith("#!/bin/sh"))
        self.assertIn("echo 3 > /proc/irq/125/smp_affinity_list", script)
        rtflow.undo_change(log, c, runner=sudo)
        self.assertEqual(sudo.calls[-1], "echo 3 > /proc/irq/125/smp_affinity_list")
        self.assertEqual(c.state, "undone")
        with open(rtflow.undo_script_path()) as fh:
            self.assertIn("nothing is in force", fh.read())

    def test_undo_script_newest_first(self):
        log = rtflow.ChangeLog()
        sudo = FakeSudo()
        rtflow.try_change(log, self.irq_finding(), runner=sudo)
        inv = rtcheck.inventory(self.roots)
        gov = self.finding("turbo-on", inv, good_run(inv, late_us=60))
        rtflow.try_change(log, gov, runner=sudo)
        with open(rtflow.undo_script_path()) as fh:
            script = fh.read()
        self.assertLess(script.index("no_turbo"), script.index("smp_affinity_list"))

    def test_a_failed_command_changes_nothing(self):
        f = self.irq_finding()
        f.commands = ["echo a > /x", "echo b > /y"]
        f.undo_commands = ["echo A > /x", "echo B > /y"]
        log = rtflow.ChangeLog()
        sudo = FakeSudo(fail=("/y",))
        with self.assertRaisesRegex(rtflow.SudoError, "Input/output"):
            rtflow.try_change(log, f, runner=sudo)
        self.assertEqual(sudo.calls, ["echo a > /x", "echo b > /y", "echo A > /x"])
        self.assertEqual(log.changes, [])

    def test_password(self):
        f = self.irq_finding()
        log = rtflow.ChangeLog()
        with self.assertRaises(rtflow.SudoError) as e:
            rtflow.try_change(log, f, runner=FakeSudo(password=True))
        self.assertTrue(e.exception.needs_password)
        self.assertEqual(log.changes, [])
        c = rtflow.record_by_hand(log, f)
        self.assertTrue(c.by_hand)
        self.assertEqual(rtflow.shell_line(f.commands[0]),
                         "sudo sh -c 'echo 0-2 > /proc/irq/125/smp_affinity_list'")

    def test_only_run_time_changes(self):
        inv = rtcheck.inventory(self.roots)
        f = self.finding("i915-display-power", inv, good_run(inv, late_us=200))
        self.assertEqual(f.kind, "permanent")
        with self.assertRaises(rtflow.SudoError):
            rtflow.try_change(rtflow.ChangeLog(), f, runner=FakeSudo())


def run_file(inv, late_us, path):
    r = good_run(inv, late_us=late_us)
    r.way, r.load, r.seconds = "threads", "stress", 60
    r.save(path)
    return rtcheck.Run.load_file(path), path


class TestFlow(Base):
    def test_the_steps(self):
        sudo = FakeSudo()
        flow = rtflow.Flow()
        self.assertEqual(flow.step()[0], 0)
        inv = flow.take_inventory(roots=self.roots)
        self.assertEqual(flow.step()[0], 1)
        d = self.tmp.name
        # baseline: two runs the same way give the noise (late 600 us of 1 ms: not ample)
        flow.add_run(*run_file(inv, 600, os.path.join(d, "b1.json")))
        self.assertIn("once more", flow.step()[1])
        rows, word = flow.add_run(*run_file(inv, 610, os.path.join(d, "b2.json")))
        self.assertEqual(len(flow.baseline), 2)
        self.assertAlmostEqual(flow.noise["servo-thread"]["late_max_us"], 10.0)
        # findings, and one change tried
        self.assertEqual(flow.step()[0], 2)
        found, _ = flow.findings()
        tryable = [f for f in found if flow.tryable(f)]
        self.assertTrue(tryable)
        f = next(x for x in tryable if x.id == "turbo-on")
        ch = rtflow.try_change(flow.log, f, flow.reference, runner=sudo)
        flow.tried(ch)
        self.assertEqual(flow.step()[0], 4)
        self.assertFalse(any(flow.tryable(x) for x in found))        # one at a time
        # measured again: better by more than the noise
        rows, word = flow.add_run(*run_file(inv, 300, os.path.join(d, "a1.json")))
        self.assertEqual(word, "better")
        self.assertEqual(flow.log.find(ch.id).verdict, "better")
        self.assertIn("Keep it or undo it", flow.step()[1])
        flow.keep()
        self.assertEqual(flow.log.find(ch.id).state, "kept")
        self.assertEqual(flow.reference, os.path.join(d, "a1.json"))
        self.assertIsNone(flow.pending)
        # the state survives the window being closed
        again = rtflow.Flow()
        self.assertEqual(again.baseline, flow.baseline)
        self.assertEqual(again.reference, flow.reference)
        # a change that made it worse, undone
        f2 = next(x for x in flow.findings()[0] if flow.tryable(x))
        ch2 = rtflow.try_change(flow.log, f2, flow.reference, runner=sudo)
        flow.tried(ch2)
        rows, word = flow.add_run(*run_file(inv, 700, os.path.join(d, "a2.json")))
        self.assertEqual(word, "worse")
        flow.undo(runner=sudo)
        self.assertEqual(flow.log.find(ch2.id).state, "undone")
        self.assertEqual(sudo.calls[-1], ch2.undo_commands[-1])
        self.assertEqual(flow.reference, os.path.join(d, "a1.json"))
        # the report has it all
        text = rtflow.report(flow)
        for part in ("## The computer", "b1.json (baseline)", "## The noise", "## Changes", "kept",
                     "undone", "## Findings now", "## What was done, in order", "## Where the check stands"):
            self.assertIn(part, text)

    def test_noise_decides(self):
        flow = rtflow.Flow()
        inv = flow.take_inventory(roots=self.roots)
        d = self.tmp.name
        flow.add_run(*run_file(inv, 600, os.path.join(d, "b1.json")))
        flow.add_run(*run_file(inv, 610, os.path.join(d, "b2.json")))     # noise 10 us
        f = next(x for x in flow.findings()[0] if flow.tryable(x))
        flow.tried(rtflow.try_change(flow.log, f, flow.reference, runner=FakeSudo()))
        rows, word = flow.add_run(*run_file(inv, 604, os.path.join(d, "a1.json")))
        self.assertEqual(word, "no change")                  # 6 us better, within the noise

    def test_a_different_way_starts_the_baseline_again(self):
        flow = rtflow.Flow()
        inv = flow.take_inventory(roots=self.roots)
        d = self.tmp.name
        flow.add_run(*run_file(inv, 40, os.path.join(d, "b1.json")))
        r, p = run_file(inv, 40, os.path.join(d, "b2.json"))
        r.load = "rest"
        flow.add_run(r, p)
        self.assertEqual(flow.baseline, [p])

    def test_done(self):
        flow = rtflow.Flow()
        # a computer the rules have nothing to say about: governor performance, no turbo, no irqbalance
        os.remove(os.path.join(self.roots.proc, "100", "comm"))
        for c in range(4):
            write(self.roots.sys, "devices/system/cpu/cpu%d/cpufreq/scaling_governor" % c, "performance\n")
        write(self.roots.sys, "devices/system/cpu/intel_pstate/no_turbo", "1\n")
        inv = flow.take_inventory(roots=self.roots)
        d = self.tmp.name
        flow.add_run(*run_file(inv, 8, os.path.join(d, "b1.json")))
        flow.add_run(*run_file(inv, 9, os.path.join(d, "b2.json")))
        self.assertEqual(flow.step()[0], 5)
        self.assertIn("no more changes are needed", flow.step()[1])


if __name__ == "__main__":
    unittest.main()
