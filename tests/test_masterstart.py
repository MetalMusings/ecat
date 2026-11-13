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


MASTER_TEXT = """Master0
  Phase: Idle
  Active: no
  Slaves: 6
  Ethernet devices:
    Main: e8:ea:6a:09:88:2b (attached)
      Link: UP
"""


class StaleTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.net = d.name
        for nic, mac in (("enp1s0", "e8:ea:6a:09:88:2b"), ("enp2s0", "aa:bb:cc:dd:ee:ff")):
            os.mkdir(os.path.join(d.name, nic))
            with open(os.path.join(d.name, nic, "address"), "w") as f:
                f.write(mac + "\n")

    def reason(self, text=MASTER_TEXT, nic="enp1s0"):
        cfg = {"master": {"interface": nic}} if nic else None
        return masterstart.stale_reason(masterstart.master_status(text), cfg, self.net)

    def test_status(self):
        self.assertEqual(masterstart.master_status(MASTER_TEXT), {
            "mac": "e8:ea:6a:09:88:2b", "link": True, "slaves": 6, "active": False})

    def test_a_good_master_is_not_stale(self):
        self.assertEqual(self.reason(), "")
        self.assertEqual(self.reason(nic=None), "")

    def test_no_link_no_slaves_or_another_card(self):
        self.assertIn("no link", self.reason(MASTER_TEXT.replace("Link: UP", "Link: DOWN")))
        self.assertIn("no slaves", self.reason(MASTER_TEXT.replace("Slaves: 6", "Slaves: 0")))
        why = self.reason(nic="enp2s0")
        self.assertIn("another network card", why)
        self.assertIn("enp2s0", why)

    def test_a_master_in_use_is_never_stale(self):
        text = MASTER_TEXT.replace("Active: no", "Active: yes").replace("Slaves: 6", "Slaves: 0")
        self.assertEqual(self.reason(text, nic="enp2s0"), "")

    def test_a_card_that_does_not_exist_here_says_nothing(self):
        self.assertEqual(self.reason(nic="nope0"), "")

    def test_restart_stops_first_and_starts_on_the_files_card(self):
        p = masterstart.restart_plan({"master": {"interface": "enp1s0"}}, sys_net=self.net,
                                     kernel_device=os.path.join(self.net, "none"))
        self.assertEqual([c[1] for c in p.commands], ["stop", "start"])
        self.assertEqual(p.commands[1][2:], ["--", "-i", "enp1s0"])


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

    def conf(self, text):
        path = os.path.join(self.net, "ethercat-uspace.conf")
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_a_card_is_needed_without_a_file_or_the_conf(self):
        conf = self.conf("# INTERFACE=enp2s0\n")
        p = masterstart.plan(None, sys_net=self.net, kernel_device=self.nokernel, conf=conf)
        self.assertTrue(p.needs_interface)
        conf = self.conf("INTERFACE=\"enp2s0\"  # the bus\n")
        self.assertEqual(masterstart.conf_interface(conf), "enp2s0")
        p = masterstart.plan(None, sys_net=self.net, kernel_device=self.nokernel, conf=conf)
        self.assertFalse(p.needs_interface)
        p = masterstart.plan({"master": {"interface": "enp2s0"}}, sys_net=self.net,
                             kernel_device=self.nokernel, conf=self.conf(""))
        self.assertFalse(p.needs_interface)
        self.assertEqual(p.interface, "enp2s0")

    def test_the_chosen_card(self):
        p = masterstart.plan(None, sys_net=self.net, kernel_device=self.nokernel,
                             interface="enp2s0", conf=self.conf(""))
        self.assertEqual(p.commands[0][1:], ["start", "--", "-i", "enp2s0"])
        self.assertFalse(p.needs_interface)
        # the file's transport goes with the file's card only
        cfg = {"master": {"interface": "enp2s0", "transport": "xdp-native"}}
        p = masterstart.plan(cfg, sys_net=self.net, kernel_device=self.nokernel, interface="enp2s0")
        self.assertEqual(p.commands[0][1:], ["start", "--", "-i", "enp2s0", "-t", "xdp-native"])
        cfg = {"master": {"interface": "enp1s0", "transport": "xdp-native"}}
        p = masterstart.plan(cfg, sys_net=self.net, kernel_device=self.nokernel, interface="enp2s0")
        self.assertEqual(p.commands[0][1:], ["start", "--", "-i", "enp2s0"])
        # a chosen card that does not exist is not passed on either
        p = masterstart.plan(None, sys_net=self.net, kernel_device=self.nokernel,
                             interface="nope0", conf=self.conf(""))
        self.assertEqual(p.commands[0][1:], ["start"])

    def test_wired_cards(self):
        for nic, up in (("enp3s0", "0"), ("wlan0", "1")):
            os.makedirs(os.path.join(self.net, nic, "device"))
            with open(os.path.join(self.net, nic, "carrier"), "w") as f:
                f.write(up + "\n")
        os.makedirs(os.path.join(self.net, "wlan0", "wireless"))
        os.makedirs(os.path.join(self.net, "enp2s0", "device"))
        with open(os.path.join(self.net, "enp2s0", "carrier"), "w") as f:
            f.write("1\n")
        # with a link first; no wireless, nothing without a device (enp1s0 here, lo, bridges)
        self.assertEqual(masterstart.wired(self.net), [("enp2s0", True), ("enp3s0", False)])

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
    def offer(self, answer, tty=True, linuxcnc=False, conf_nic="enp1s0", net=None):
        ran, said = [], []
        answers = list(answer) if isinstance(answer, list) else None
        with mock.patch.object(masterstart, "linuxcnc_running", lambda: linuxcnc), \
                mock.patch.object(masterstart, "conf_interface", lambda path=None: conf_nic):
            started = masterstart.offer_terminal(
                stdin=Tty(tty), stderr=Tty(tty),
                input_fn=lambda prompt: answers.pop(0) if answers is not None else answer,
                out=said.append, sys_net=net or masterstart.SYS_NET,
                runner=lambda cmd, **kw: ran.append(cmd) or subprocess.CompletedProcess([], 0, "", ""))
        return started, ran, said

    def test_the_card_is_asked_for_when_nothing_names_it(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        for nic, up in (("enp1s0", "0"), ("enp2s0", "1")):
            os.makedirs(os.path.join(d.name, nic, "device"))
            with open(os.path.join(d.name, nic, "carrier"), "w") as f:
                f.write(up)
        # Enter takes the card with a link
        started, ran, said = self.offer(["", "y"], conf_nic=None, net=d.name)
        self.assertTrue(started)
        self.assertEqual(ran[-1][-3:], ["--", "-i", "enp2s0"])
        self.assertTrue(any("enp2s0 (link), enp1s0 (no link)" in x for x in said))
        started, ran, _ = self.offer(["enp1s0", "y"], conf_nic=None, net=d.name)
        self.assertEqual(ran[-1][-2:], ["-i", "enp1s0"])
        started, ran, said = self.offer(["wlan9"], conf_nic=None, net=d.name)
        self.assertEqual((started, ran), (False, []))
        self.assertIn("wlan9 is not one of them.", said)

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
