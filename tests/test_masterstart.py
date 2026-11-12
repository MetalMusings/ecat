"""Starting the master with sudo after the user confirmed: the plan, the
run, the terminal offer."""

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ecatsetup import masterstart  # noqa: E402


class Tty:
    def __init__(self, tty):
        self.tty = tty

    def isatty(self):
        return self.tty


class PlanTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.net = d.name
        os.mkdir(os.path.join(d.name, "enp2s0"))
        os.makedirs(os.path.join(d.name, "sub", "dev"))     # exists, but is not a NIC name
        self.nokernel = os.path.join(d.name, "no-device")

    def plan(self, cfg=None, kernel=None):
        return masterstart.plan(cfg, sys_net=self.net, kernel_device=kernel or self.nokernel)

    def test_plain_start_without_a_file(self):
        p = self.plan()
        self.assertEqual(len(p.commands), 1)
        self.assertEqual(p.commands[0][1:], ["start"])
        self.assertTrue(masterstart.text(p).startswith("sudo "))

    def test_nic_and_transport_from_the_file(self):
        p = self.plan({"master": {"interface": "enp2s0", "transport": "xdp-native"}})
        self.assertEqual(p.commands[0][1:], ["start", "--", "-i", "enp2s0", "-t", "xdp-native"])

    def test_unknown_nic_is_not_passed_on(self):
        p = self.plan({"master": {"interface": "nope0", "transport": "raw"}})
        self.assertEqual(p.commands[0][1:], ["start"])
        self.assertTrue(any("nope0" in n for n in p.notes))

    def test_odd_names_are_never_passed_on(self):
        for nic in ("../enp2s0", "enp2s0 --evil", "sub/dev"):
            p = self.plan({"master": {"interface": nic}})
            self.assertEqual(p.commands[0][1:], ["start"], nic)

    def test_unknown_transport_is_not_passed_on(self):
        p = self.plan({"master": {"interface": "enp2s0", "transport": "x; rm"}})
        self.assertEqual(p.commands[0][1:], ["start", "--", "-i", "enp2s0"])

    def test_kernel_master_is_stopped_first_and_visibly(self):
        p = self.plan(kernel=self.net)      # any path that exists
        self.assertEqual(p.commands[0], ["systemctl", "stop", "ethercat"])
        self.assertEqual(len(p.commands), 2)
        self.assertIn("sudo systemctl stop ethercat", masterstart.text(p))
        self.assertTrue(p.notes)


class RunTest(unittest.TestCase):
    def proc(self, code=0, err=""):
        return subprocess.CompletedProcess([], code, "", err)

    def test_window_uses_sudo_n_and_stops_at_the_first_failure(self):
        calls = []

        def runner(cmd, **kw):
            calls.append(cmd)
            return self.proc(1, "sudo: a password is required")
        p = masterstart.Plan([["systemctl", "stop", "ethercat"], ["ethercat-uspace", "start"]], [])
        ok, msg = masterstart.run(p, True, runner)
        self.assertFalse(ok)
        self.assertEqual(calls, [["sudo", "-n", "systemctl", "stop", "ethercat"]])
        self.assertIn("terminal", msg)
        self.assertIn("sudo systemctl stop ethercat", msg)

    def test_terminal_uses_plain_sudo(self):
        calls = []
        ok, _ = masterstart.run(masterstart.Plan([["x", "start"]], []), False,
                                lambda cmd, **kw: calls.append(cmd) or self.proc())
        self.assertTrue(ok)
        self.assertEqual(calls, [["sudo", "x", "start"]])

    def test_other_failure_shows_the_tools_message(self):
        ok, msg = masterstart.run(masterstart.Plan([["x", "start"]], []), True,
                                  lambda cmd, **kw: self.proc(1, "x is in use by pid 5"))
        self.assertFalse(ok)
        self.assertIn("in use by pid 5", msg)


class OfferTest(unittest.TestCase):
    def offer(self, answer, tty=True, linuxcnc=False):
        ran, said = [], []
        with mock.patch.object(masterstart, "linuxcnc_running", lambda: linuxcnc):
            started = masterstart.offer_terminal(
                stdin=Tty(tty), stderr=Tty(tty), input_fn=lambda prompt: answer, out=said.append,
                runner=lambda cmd, **kw: ran.append(cmd) or subprocess.CompletedProcess([], 0, "", ""))
        return started, ran, said

    def test_yes_runs_the_command(self):
        started, ran, said = self.offer("y")
        self.assertTrue(started)
        self.assertEqual(ran[-1][:1], ["sudo"])
        self.assertTrue(any("sudo" in s for s in said))     # shown before it ran

    def test_default_is_no(self):
        started, ran, _ = self.offer("")
        self.assertFalse(started)
        self.assertEqual(ran, [])

    def test_nothing_without_a_terminal(self):
        started, ran, said = self.offer("y", tty=False)
        self.assertEqual((started, ran, said), (False, [], []))

    def test_nothing_while_linuxcnc_runs(self):
        started, ran, said = self.offer("y", linuxcnc=True)
        self.assertEqual((started, ran, said), (False, [], []))


class LinuxcncTest(unittest.TestCase):
    def test_process_names(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(masterstart.linuxcnc_running(d))
            for pid, comm in (("10", "bash"), ("11", "milltask")):
                os.mkdir(os.path.join(d, pid))
                open(os.path.join(d, pid, "comm"), "w").write(comm + "\n")
            self.assertTrue(masterstart.linuxcnc_running(d))


class ToolEnvTest(unittest.TestCase):
    def test_tool_never_prompts_when_ecatsetup_runs_it(self):
        from ecatsetup import tool
        seen = {}

        def fake(cmd, **kw):
            seen.update(kw)
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        with mock.patch.object(subprocess, "run", fake):
            tool.run("slaves")
        self.assertEqual(seen["env"]["ETHERCAT_USPACE_NO_PROMPT"], "1")


if __name__ == "__main__":
    unittest.main()
