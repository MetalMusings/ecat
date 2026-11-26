"""kernelmaster.py: the IgH kernel master only when it has ecat's card
(the same rule as the component's start check, ecat_kernel_master_uses)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from ecatsetup import kernelmaster  # noqa: E402


class TestKernelMaster(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.d = d.name
        self.device = os.path.join(self.d, "EtherCAT0")
        self.net = os.path.join(self.d, "net")
        os.makedirs(os.path.join(self.net, "enp2s0"))
        with open(os.path.join(self.net, "enp2s0", "address"), "w") as f:
            f.write("a8:b8:e0:01:02:03\n")

    def conf(self, text):
        path = os.path.join(self.d, "ethercat.conf")
        with open(path, "w") as f:
            f.write(text)
        return (path,)

    def loaded(self):
        open(self.device, "w").close()

    def test_uses(self):
        u = kernelmaster.uses
        self.assertEqual(u('MASTER0_DEVICE="enp2s0"\n', "enp2s0"), "MASTER0_DEVICE=enp2s0")
        self.assertEqual(u("MASTER0_DEVICE=A8:B8:E0:01:02:03\n", "enp2s0", "a8:b8:e0:01:02:03"),
                         "MASTER0_DEVICE=A8:B8:E0:01:02:03")
        self.assertEqual(u("MASTER1_BACKUP=ff:ff:ff:ff:ff:ff\n", "enp2s0"),
                         "MASTER1_BACKUP=ff:ff:ff:ff:ff:ff")
        self.assertIsNone(u("MASTER0_DEVICE=enp1s0\n", "enp2s0", "a8:b8:e0:01:02:03"))
        self.assertIsNone(u('#MASTER0_DEVICE="enp2s0"\nDEVICE_MODULES="generic"\n', "enp2s0"))
        self.assertIsNone(u('MASTER0_DEVICE=""\n', "enp2s0"))

    def test_not_loaded_is_never_in_the_way(self):
        self.assertIsNone(kernelmaster.conflict("enp2s0", self.device, self.conf("MASTER0_DEVICE=enp2s0"),
                                                self.net))

    def test_loaded_on_this_card(self):
        self.loaded()
        why = kernelmaster.conflict("enp2s0", self.device, self.conf('MASTER0_DEVICE="enp2s0"\n'), self.net)
        self.assertIn("uses enp2s0 (MASTER0_DEVICE=enp2s0 in", why)
        why = kernelmaster.conflict("enp2s0", self.device, self.conf("MASTER0_DEVICE=a8:b8:e0:01:02:03\n"),
                                    self.net)
        self.assertIn("uses enp2s0", why)

    def test_loaded_on_another_card_is_not_named(self):
        self.loaded()
        self.assertIsNone(kernelmaster.conflict("enp2s0", self.device, self.conf("MASTER0_DEVICE=enp1s0\n"),
                                                self.net))

    def test_loaded_and_unknown(self):
        self.loaded()
        why = kernelmaster.conflict("enp2s0", self.device, (os.path.join(self.d, "none"),), self.net)
        self.assertIn("cannot be read, so it may use enp2s0", why)
        why = kernelmaster.conflict(None, self.device, self.conf("MASTER0_DEVICE=enp1s0\n"), self.net)
        self.assertIn("is not known", why)

    def test_at_boot(self):
        conf = self.conf("MASTER0_DEVICE=enp2s0\n")
        self.assertIn("starts with the computer", kernelmaster.at_boot("enp2s0", True, conf, self.net))
        self.assertIsNone(kernelmaster.at_boot("enp2s0", False, conf, self.net))
        self.assertIsNone(kernelmaster.at_boot("enp2s0", True, self.conf("MASTER0_DEVICE=enp1s0\n"),
                                               self.net))


if __name__ == "__main__":
    unittest.main()
