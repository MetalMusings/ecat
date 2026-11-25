"""masterlog.py: the free master's messages from the system journal."""

import json
import os
import tempfile
import unittest

from ecatsetup import masterlog


def entry(text, level=4, usec=1_700_000_000_000_000, pid=42):
    return json.dumps({"MESSAGE": text, "PRIORITY": str(level), "__REALTIME_TIMESTAMP": str(usec),
                       "_PID": str(pid), "SYSLOG_IDENTIFIER": "ec_master"}) + "\n"


class FakeProcess:
    """journalctl: prints its lines, then ends (the test joins the reader)."""
    started = []

    def __init__(self, argv, lines, **kwargs):
        self.argv = argv
        self.stdout = iter(lines)
        self.terminated = False
        FakeProcess.started.append(self)

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


def popen_with(lines):
    return lambda argv, **kwargs: FakeProcess(argv, lines, **kwargs)


class TestFind(unittest.TestCase):
    def test_free_master_pid(self):
        with tempfile.TemporaryDirectory() as proc:
            for pid, comm in (("17", "bash"), ("230", "ec_master"), ("self", "x")):
                os.makedirs(os.path.join(proc, pid))
                with open(os.path.join(proc, pid, "comm"), "w") as f:
                    f.write(comm + "\n")
            self.assertEqual(masterlog.free_master_pid(proc), 230)
            os.remove(os.path.join(proc, "230", "comm"))
            self.assertIsNone(masterlog.free_master_pid(proc))
        self.assertIsNone(masterlog.free_master_pid("/no/such/proc"))


class TestAccess(unittest.TestCase):
    def test_root_or_a_journal_group_reads_it_directly(self):
        self.assertEqual(masterlog.journal_access(euid=0, groups=[], sudo_works=lambda: False), ([], None))
        self.assertEqual(masterlog.journal_access(euid=1000, groups=["systemd-journal"],
                                                  sudo_works=lambda: False), ([], None))
        self.assertEqual(masterlog.journal_access(euid=1000, groups=["adm"], sudo_works=lambda: False),
                         ([], None))

    def test_else_sudo_without_a_password(self):
        self.assertEqual(masterlog.journal_access(euid=1000, groups=["users"], sudo_works=lambda: True),
                         (["sudo", "-n"], None))

    def test_else_says_what_to_do(self):
        prefix, why = masterlog.journal_access(euid=1000, groups=["users"], sudo_works=lambda: False)
        self.assertIsNone(prefix)
        self.assertIn("usermod -aG systemd-journal", why)

    def test_command(self):
        argv = masterlog.journal_command(230, ["sudo", "-n"])
        self.assertEqual(argv[:3], ["sudo", "-n", "journalctl"])
        for a in ("--output=json", "--follow", "--lines=all", "--priority=info", "_PID=230"):
            self.assertIn(a, argv)


class TestParse(unittest.TestCase):
    def test_entry(self):
        t = masterlog.parse(entry("EtherCAT ERROR 0-4: Failed to set SAFEOP state.", 3), 5)
        self.assertEqual((t.index, t.level, t.severity, t.slave, t.source),
                         (5, 3, "error", 4, "free master"))
        self.assertEqual(t.time, 1_700_000_000.0)
        self.assertEqual(t.text, "EtherCAT ERROR 0-4: Failed to set SAFEOP state.")

    def test_binary_message_and_junk(self):
        line = json.dumps({"MESSAGE": list(b"EtherCAT 0-1: \xff"), "PRIORITY": "6",
                           "__REALTIME_TIMESTAMP": "1"})
        self.assertTrue(masterlog.parse(line, 0).text.startswith("EtherCAT 0-1: "))
        self.assertIsNone(masterlog.parse("not json", 0))
        self.assertIsNone(masterlog.parse(json.dumps({"PRIORITY": "6"}), 0))


class TestMasterLog(unittest.TestCase):
    def setUp(self):
        FakeProcess.started = []

    def follow(self, log):
        if log.follower is not None:
            log.follower.thread.join(5)

    def test_follows_each_master_and_keeps_the_messages(self):
        pids = [None, 230, 230, None, 231]
        log = masterlog.MasterLog(find=lambda: pids.pop(0), access=lambda: (["sudo", "-n"], None),
                                  popen=popen_with([entry("EtherCAT WARNING 0-4: a"), "junk\n",
                                                    entry("EtherCAT 0-4: b", 6)]))
        log.poll()
        self.assertEqual((log.texts(), log.status()), ([], ""))
        log.poll()
        self.follow(log)
        self.assertEqual([t.text for t in log.texts()], ["EtherCAT WARNING 0-4: a", "EtherCAT 0-4: b"])
        self.assertIn("pid 230", log.status())
        self.assertIn("_PID=230", FakeProcess.started[0].argv)
        log.poll()                      # the same master: no second journalctl
        self.assertEqual(len(FakeProcess.started), 1)
        log.poll()                      # it stopped: its messages stay
        self.assertTrue(FakeProcess.started[0].terminated)
        self.assertEqual(len(log.texts()), 2)
        self.assertEqual(log.status(), "")
        log.poll()                      # another one
        self.follow(log)
        self.assertEqual([t.index for t in log.texts()], [0, 1, 2, 3])
        log.close()
        self.assertTrue(FakeProcess.started[1].terminated)

    def test_journal_not_readable(self):
        asked = []

        def access():
            asked.append(1)
            return None, "the free master's messages are in the system journal, which ..."
        pids = [230, None, 231]
        log = masterlog.MasterLog(find=lambda: pids.pop(0), access=access, popen=popen_with([]))
        log.poll()
        self.assertEqual(FakeProcess.started, [])
        self.assertIn("system journal", log.note)
        self.assertIn("system journal", log.status())
        log.poll()
        log.poll()
        self.assertEqual(len(asked), 1)     # decided once per window


if __name__ == "__main__":
    unittest.main()
