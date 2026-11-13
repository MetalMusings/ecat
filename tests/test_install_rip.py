"""packaging/install-rip: the installer of the archive for a run-in-place LinuxCNC,
against a made-up archive and tree."""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = os.path.join(os.path.dirname(__file__), "..")
HEADERS = {"hal.h": "int hal;\n", "rtapi.h": "int rtapi;\n"}


class TestInstallRip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = self.tmp.name
        self.arch = os.path.join(t, "arch")
        self.rip = os.path.join(t, "rip")
        self.prefix = os.path.join(t, "prefix")
        self.settings = os.path.join(t, "settings.yaml")
        os.makedirs(self.arch)
        os.makedirs(os.path.join(self.rip, "scripts"))
        os.makedirs(os.path.join(self.rip, "include"))
        os.makedirs(os.path.join(self.rip, "rtlib"))
        open(os.path.join(self.rip, "scripts", "rip-environment"), "w").close()
        for name, text in HEADERS.items():
            with open(os.path.join(self.rip, "include", name), "w") as f:
                f.write(text)
        with open(os.path.join(self.arch, "ecat-abi.sha256"), "w") as f:
            for name, text in HEADERS.items():
                f.write("%s  %s\n" % (hashlib.sha256(text.encode()).hexdigest(), name))
        with open(os.path.join(self.arch, "ecat.so"), "w") as f:
            f.write("not a library\n")
        with open(os.path.join(self.arch, "BUILD-INFO"), "w") as f:
            f.write("ecat 0.14 (ecat-v0.14, abc1234)\nlinuxcnc v2.10.0-pre2 deadbeef\ndistro debian13\n")
        os.makedirs(os.path.join(self.arch, "ecatsetup"))
        open(os.path.join(self.arch, "ecatsetup", "__init__.py"), "w").close()
        shutil.copy(os.path.join(ROOT, "packaging", "install-rip"), os.path.join(self.arch, "install"))
        self.env = dict(os.environ, HOME=t, ECATSETUP_SETTINGS=self.settings, XDG_DATA_HOME=os.path.join(t, "share"))
        self.env.pop("EMC2_HOME", None)

    def run_install(self, *args):
        return subprocess.run(["bash", os.path.join(self.arch, "install"), "--rip", self.rip,
                               "--prefix", self.prefix] + list(args),
                              env=self.env, capture_output=True, text=True)

    def test_installs_when_the_headers_are_the_same(self):
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.rip, "rtlib", "ecat.so")))
        self.assertTrue(os.access(os.path.join(self.prefix, "bin", "ecatsetup"), os.X_OK))
        self.assertTrue(os.path.isdir(os.path.join(self.prefix, "lib", "ecat-rip", "ecatsetup")))
        with open(self.settings) as f:
            self.assertEqual(yaml.safe_load(f)["linuxcnc"], os.path.realpath(self.rip))

    def test_refuses_when_a_header_differs(self):
        with open(os.path.join(self.rip, "include", "hal.h"), "w") as f:
            f.write("int hal_changed;\n")
        r = self.run_install()
        self.assertEqual(r.returncode, 1)
        self.assertFalse(os.path.exists(os.path.join(self.rip, "rtlib", "ecat.so")))
        self.assertFalse(os.path.exists(os.path.join(self.prefix, "bin", "ecatsetup")))
        self.assertIn("hal.h", r.stderr)
        self.assertIn("v2.10.0-pre2", r.stderr)                 # which LinuxCNC it was built for
        self.assertIn("make -C component install", r.stderr)    # and how to build for this one
        self.assertFalse(os.path.exists(self.settings))

    def test_refuses_when_a_header_is_missing(self):
        os.remove(os.path.join(self.rip, "include", "rtapi.h"))
        r = self.run_install()
        self.assertEqual(r.returncode, 1)
        self.assertIn("rtapi.h", r.stderr)

    def test_not_a_tree(self):
        os.remove(os.path.join(self.rip, "scripts", "rip-environment"))
        r = self.run_install()
        self.assertEqual(r.returncode, 1)
        self.assertIn("rip-environment", r.stderr)

    def test_uninstall_takes_away_what_it_put(self):
        self.assertEqual(self.run_install().returncode, 0)
        r = self.run_install("--uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.rip, "rtlib", "ecat.so")))
        self.assertFalse(os.path.exists(os.path.join(self.prefix, "bin", "ecatsetup")))
        self.assertFalse(os.path.exists(os.path.join(self.prefix, "lib", "ecat-rip")))
        with open(self.settings) as f:
            self.assertNotIn("linuxcnc", yaml.safe_load(f) or {})
        self.assertTrue(os.path.isdir(os.path.join(self.rip, "rtlib")))

    def test_uninstall_without_install(self):
        r = self.run_install("--uninstall")
        self.assertEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main()
