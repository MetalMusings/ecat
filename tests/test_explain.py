"""What the event log means (ecatsetup/explain.py and ecatsetup/explain/*.yaml):
each symptom's and each cause's own tests, the context from a log, the masters,
the computer and the port counters, and the text."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
if "ECATSETUP_TEST_STATE" not in os.environ:
    os.environ["ECATSETUP_TEST_STATE"] = os.environ["XDG_STATE_HOME"] = os.environ["TMPDIR"] = \
        __import__("tempfile").tempdir = __import__("tempfile").mkdtemp(prefix="ecatsetup-test-state-")
    __import__("atexit").register(__import__("shutil").rmtree, os.environ["ECATSETUP_TEST_STATE"], True)

from ecatsetup import diagnose, events, explain, rtcheck  # noqa: E402

STATE_KEYS = {"likely": "likely", "possible": "possible", "ruled-out": "ruled out",
              "not checked": "not checked"}


def ev(i, type_, slave=-1, a=0, b=0):
    return events.Event(i, 1000.0 + i, type_, slave, a, b)


def log(evs, texts=()):
    return events.Log("/x", 1000.0, 1000000, 1, "/c/ethercat.yaml", len(evs), list(evs),
                      [events.Text(i, 1000.0 + i, 4, t) for i, t in enumerate(texts)], len(texts))


# a run: link up, the slaves come up (working counter zero then incomplete is
# normal there), all in OP, then the trouble while running
START = [ev(0, events.START, a=3), ev(1, events.LINK, a=1), ev(2, events.MASTER_AL, a=2, b=0),
         ev(3, events.WC, a=0, b=1), ev(4, events.WC, a=1, b=0), ev(5, events.MASTER_AL, a=8, b=2),
         ev(6, events.WC, a=2, b=1)]


class TestCatalogue(unittest.TestCase):
    def test_each_symptom_and_cause_does_what_its_tests_say(self):
        symptoms = explain.load([explain.HERE])
        self.assertTrue(symptoms)
        for s in symptoms:
            tests = s["tests"] or {}
            self.assertIn("present", tests, s.id)
            self.assertIn("absent", tests, s.id)
            self.assertTrue(explain.findings(tests["present"], [s]), "%s: present" % s.id)
            self.assertFalse(explain.findings(tests["absent"], [s]), "%s: absent" % s.id)
            for c in s.causes:
                ct = c.d.get("tests") or {}
                self.assertTrue(len(ct) >= 2, "%s/%s needs tests for two states" % (s.id, c.id))
                for key, ctx in ct.items():
                    self.assertEqual(c.judge(ctx or {})[0], STATE_KEYS[key], "%s/%s %s" % (s.id, c.id, key))

    def test_bad_files_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            for text, what in (("- {id: a, title: t, when: 'x', meaning: m, causes: [{id: c, title: t}], "
                                "colour: red}", "unknown keys colour"),
                               ("- {id: a, title: t, when: 'x.__class__', meaning: m, "
                                "causes: [{id: c, title: t}]}", "not allowed"),
                               ("- {id: a, title: t, when: 'x', meaning: m, causes: "
                                "[{id: c, title: t}, {id: c, title: u}]}", "two causes"),
                               ("- {id: a, title: t, when: 'x', causes: [{id: c, title: t}]}", "no meaning")):
                with open(os.path.join(d, "a.yaml"), "w") as f:
                    f.write(text)
                with self.assertRaisesRegex(Exception, what):
                    explain.load([d])

    def test_a_users_file_replaces_a_shipped_symptom(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "mine.yaml"), "w") as f:
                f.write("- {id: frames-lost, title: Mine, when: 'log', meaning: m, causes: [{id: c, title: t}]}\n")
            found = explain.load([explain.HERE, d])
        self.assertEqual([s["title"] for s in found if s.id == "frames-lost"], ["Mine"])


class TestFromLog(unittest.TestCase):
    def test_the_start_is_not_counted(self):
        c = explain.from_log(log(START))
        self.assertEqual((c["wc_zero"], c["wc_incomplete"], c["lost"]), (0, 0, 0))
        self.assertFalse(explain.findings(explain.context(log(START))))

    def test_frames_lost_while_running(self):
        """Mücke's log: working counter zero, skipped and unmatched datagrams,
        the drives shut down for invalid process data."""
        evs = START + [ev(7, events.WC, a=0, b=2), ev(8, events.DRIVE_BLOCK, slave=0, a=2),
                       ev(9, events.WC, a=2, b=0), ev(10, events.LATE, a=2400, b=2),
                       ev(11, events.SHIFT, a=145000, b=3), ev(12, events.WC, a=0, b=2)]
        texts = ["EtherCAT WARNING 0: Datagram 0x5581 (domain0-0-main) was SKIPPED 2 times.",
                 "EtherCAT WARNING 0: 4 datagrams UNMATCHED!",
                 "EtherCAT WARNING 0: 1 datagram TIMED OUT!",
                 "EtherCAT WARNING 0: Datagram 0x5582 (domain0-0-main) was SKIPPED."]
        c = explain.from_log(log(evs, texts))
        self.assertEqual((c["wc_zero"], c["skipped"], c["unmatched"], c["timed_out"], c["lost"]),
                         (2, 3, 4, 1, 8))
        self.assertEqual((c["drives_shut"], c["late"], c["turnaround_3"]), (1, 3, 1))
        found = explain.findings(explain.context(log(evs, texts)))
        self.assertEqual([f.id for f in found], ["frames-lost"])
        f = found[0]
        self.assertIn("working counter zero 2 times while running", f.evidence)
        self.assertIn("3 skipped, 4 unmatched, 1 timed-out", f.evidence)
        # nothing else known: only the late thread is likely, the rest not checked
        states = {c["id"]: c["state"] for c in f.causes}
        self.assertEqual(states["late-thread"], "likely")
        self.assertEqual(f.causes[0]["id"], "late-thread")
        self.assertEqual({v for k, v in states.items() if k != "late-thread"}, {"not checked"})


class TestJudged(unittest.TestCase):
    """The whole chain on one computer: a kernel master loaded, a Realtek card
    with EEE, the slaves examined with clean counters."""

    def computer(self):
        inv = rtcheck.Inventory()
        inv.add("preempt_rt", "PREEMPT_RT", "yes", "x", data=True)
        inv.add("cpus", "CPUs", "0-3", "x", data=[0, 1, 2, 3])
        inv.add("isolated", "Isolated", "3", "x", data=[3])
        inv.add("nics", "Network cards", "...", "x", data=[
            {"name": "enp1s0", "driver": "r8169", "operstate": "up", "eee": "enabled - active", "aspm": {}},
            {"name": "eno1", "driver": "e1000e", "operstate": "up", "eee": "disabled", "aspm": {}}])
        inv.add("default_route", "Default route", "eno1", "x", data="eno1")
        return inv

    def test_judged(self):
        masters = {"device": False, "modules": ["ec_master", "ec_r8169"], "enabled": "enabled",
                   "active": "active", "free": [], "rtapi": False, "linuxcnc": [], "kernel_master": True}
        port = diagnose.Port(1, True, {"rx": 0, "phy": 0, "fwd": 0, "lost": 0},
                             {"rx": 0, "phy": 0, "fwd": 0, "lost": 0})
        reports = [diagnose.Report(0, "x", False, 8, False, 0, ports=[port])]
        evs = START + [ev(7, events.WC, a=0, b=2)]
        ctx = explain.context(log(evs), masters=masters, inv=self.computer(), interface="enp1s0",
                              reports=reports)
        f = explain.findings(ctx)[0]
        states = {c["id"]: c["state"] for c in f.causes}
        self.assertEqual(states, {
            "kernel-master": "likely", "kernel-master-at-boot": "not checked", "late-thread": "not checked",
            "not-preempt-rt": "ruled out", "no-isolated-cpu": "ruled out", "nic-eee": "likely",
            "nic-carries-lan": "ruled out", "nic-irq-cpu": "not checked", "nic-realtek": "possible",
            "cable": "ruled out"})
        kernel = next(c for c in f.causes if c["id"] == "kernel-master")
        self.assertEqual(kernel["evidence"], "modules ec_master, ec_r8169; ethercat.service is active")
        eee = next(c for c in f.causes if c["id"] == "nic-eee")
        self.assertIn("ethtool --set-eee enp1s0 eee off", eee["fix"])
        # likely first, ruled out last
        order = [c["state"] for c in f.causes]
        self.assertEqual(order, sorted(order, key=["likely", "possible", "not checked", "ruled out"].index))
        text = "\n".join(explain.text_lines([f]))
        self.assertIn("[x]  The IgH kernel master is loaded beside the userspace master: modules ec_master", text)
        self.assertIn("What to do: Two masters on one card", text)
        self.assertIn("[ok] A cable, a connector or a slave's power", text)
        self.assertIn("How to check: The Timing tab", text)
        html = explain.html([f])
        self.assertIn("The IgH kernel master is loaded", html)
        self.assertNotIn("<script", html)

    def test_cable_evidence_says_only_what_was_seen(self):
        evs = START + [ev(7, events.WC, a=0, b=2), ev(8, events.SLAVE_ONLINE, slave=3, a=0)]
        f = explain.findings(explain.context(log(evs)))[0]
        cable = next(c for c in f.causes if c["id"] == "cable")
        self.assertEqual((cable["state"], cable["evidence"]), ("likely", "slaves offline 1 times while running"))
        self.assertNotIn("\n", f.evidence)

    def test_port_errors(self):
        port = diagnose.Port(1, True, {"rx": 4, "phy": 0, "fwd": 2, "lost": 1},
                             {"rx": 0, "phy": 0, "fwd": 0, "lost": 0})
        c = explain.from_reports([diagnose.Report(2, "x", False, 8, False, 0, ports=[port])])
        self.assertEqual(c["port_errors_text"], "slave 2 port 1: lost 1, rx 4")


if __name__ == "__main__":
    unittest.main()
