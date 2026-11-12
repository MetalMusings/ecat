"""Tests of EtherCAT Setup's own settings (settings.py).

Run with: python3 -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ecatsetup import settings  # noqa: E402


class TestSettings(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.old = os.environ.get("ECATSETUP_SETTINGS")
        os.environ["ECATSETUP_SETTINGS"] = os.path.join(self.dir.name, "sub", "settings.yaml")
# state (test lock, drive limits): never the user's own
if "ECATSETUP_TEST_STATE" not in os.environ:
    os.environ["ECATSETUP_TEST_STATE"] = os.environ["XDG_STATE_HOME"] = \
        __import__("tempfile").mkdtemp(prefix="ecatsetup-test-state-")

    def tearDown(self):
        if self.old is None:
            del os.environ["ECATSETUP_SETTINGS"]
        else:
            os.environ["ECATSETUP_SETTINGS"] = self.old
        self.dir.cleanup()

    def test_esi_folders(self):
        self.assertEqual(settings.load(), {})
        self.assertEqual(settings.esi_folders(), [])
        a, b = os.path.join(self.dir.name, "a"), os.path.join(self.dir.name, "b")
        os.mkdir(a)
        os.mkdir(b)
        settings.add_esi_folder(a)
        settings.add_esi_folder(b)
        settings.add_esi_folder(a)          # moved last, not twice
        self.assertEqual(settings.esi_folders(), [b, a])
        os.rmdir(b)                         # gone: left out
        self.assertEqual(settings.esi_folders(), [a])

    def test_bad_file(self):
        os.makedirs(os.path.dirname(settings.path()))
        with open(settings.path(), "w") as f:
            f.write("[not: a mapping")
        self.assertEqual(settings.load(), {})
        settings.add_esi_folder(self.dir.name)
        self.assertEqual(settings.esi_folders(), [self.dir.name])


if __name__ == "__main__":
    unittest.main()
