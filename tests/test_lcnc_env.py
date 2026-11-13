"""lcnc_env.py: which LinuxCNC the program works with."""

import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ecatsetup import lcnc_env, settings  # noqa: E402


def fake_tree(d):
    os.makedirs(os.path.join(d, "scripts"))
    os.makedirs(os.path.join(d, "bin"))
    os.makedirs(os.path.join(d, "lib", "hallib"))
    with open(os.path.join(d, "scripts", "rip-environment"), "w") as f:
        f.write('export EMC2_HOME="%s"\nexport FAKE_RIP=yes\nexport PATH="%s/bin:$PATH"\n' % (d, d))
    with open(os.path.join(d, "bin", "halrun"), "w") as f:
        f.write("#!/bin/sh\n")
    os.chmod(os.path.join(d, "bin", "halrun"), 0o755)


class TestLcncEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(os.path.join(self.tmp.name, "rip"))
        fake_tree(self.root)
        self.settings = os.path.join(self.tmp.name, "settings.yaml")
        env = {k: v for k, v in os.environ.items() if k not in ("EMC2_HOME", "ECATSETUP_LINUXCNC")}
        env["ECATSETUP_SETTINGS"] = self.settings
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        lcnc_env._cache.clear()

    def test_none_without_a_tree(self):
        self.assertIsNone(lcnc_env.tree())
        self.assertIsNone(lcnc_env.env())
        self.assertEqual(lcnc_env.program("halrun"), "halrun")
        self.assertEqual(lcnc_env.hallib(), "/usr/share/linuxcnc/hallib")
        self.assertEqual(lcnc_env.describe(), "installed LinuxCNC")

    def test_the_tree_from_the_settings(self):
        settings.save({"linuxcnc": self.root})
        self.assertEqual(lcnc_env.tree(), self.root)
        self.assertEqual(lcnc_env.program("halrun"), os.path.join(self.root, "bin", "halrun"))
        self.assertEqual(lcnc_env.hallib(), os.path.join(self.root, "lib", "hallib"))
        self.assertIn(self.root, lcnc_env.describe())

    def test_rip_environment_is_read_for_the_programs(self):
        settings.save({"linuxcnc": self.root})
        env = lcnc_env.env()
        self.assertEqual(env.get("FAKE_RIP"), "yes")
        self.assertTrue(env["PATH"].startswith(os.path.join(self.root, "bin")))
        self.assertNotIn("FAKE_RIP", os.environ)

    def test_a_shell_already_in_the_tree_needs_nothing(self):
        with mock.patch.dict(os.environ, {"EMC2_HOME": self.root}):
            self.assertEqual(lcnc_env.tree(), self.root)
            self.assertIsNone(lcnc_env.env())

    def test_a_folder_that_is_no_tree_is_ignored(self):
        settings.save({"linuxcnc": self.tmp.name})
        self.assertIsNone(lcnc_env.tree())

    def test_the_variable_wins_over_the_settings(self):
        other = os.path.realpath(os.path.join(self.tmp.name, "other"))
        fake_tree(other)
        settings.save({"linuxcnc": self.root})
        with mock.patch.dict(os.environ, {"ECATSETUP_LINUXCNC": other}):
            self.assertEqual(lcnc_env.tree(), other)


if __name__ == "__main__":
    unittest.main()
