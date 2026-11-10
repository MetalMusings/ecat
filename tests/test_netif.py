"""Tests of netif: the master's interface and whether it can do xdp-native."""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# quirk files: only those that come with the program
os.environ["ECATSETUP_DEVICES"] = os.path.join(os.path.dirname(__file__), "no-such-dir")
os.environ["ECATSETUP_SETTINGS"] = os.path.join(os.path.dirname(__file__), "no-such-dir", "settings.yaml")

from ecatsetup import netif, tool  # noqa: E402


class TestNetif(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        for name, mac in (("lo", "00:00:00:00:00:00"), ("enp1s0", "e8:ea:6a:09:88:2b"),
                          ("enp2s0", "00:dd:8c:e0:65:a5")):
            os.makedirs(os.path.join(self.dir.name, name))
            with open(os.path.join(self.dir.name, name, "address"), "w") as f:
                f.write(mac + "\n")
        p = mock.patch.object(netif, "SYS_NET", self.dir.name)
        p.start()
        self.addCleanup(p.stop)

    def test_interface_by_mac(self):
        self.assertEqual(netif.interfaces(), ["enp1s0", "enp2s0"])
        self.assertEqual(netif.by_mac("E8:EA:6A:09:88:2B"), "enp1s0")
        self.assertIsNone(netif.by_mac("00:00:00:00:00:00"))  # not lo
        self.assertIsNone(netif.by_mac(None))
        with mock.patch.object(tool, "master_mac", lambda: "00:dd:8c:e0:65:a5"):
            self.assertEqual(netif.master_interface(), "enp2s0")

        def no_master():
            raise tool.ToolError("no master")
        with mock.patch.object(tool, "master_mac", no_master):
            self.assertIsNone(netif.master_interface())

    def test_fill_master(self):
        with mock.patch.object(netif, "xdp_native", lambda name: name == "enp1s0"):
            cfg = {"master": {"transport": "raw"}}
            self.assertEqual(len(netif.fill_master(cfg, "enp1s0")), 1)
            self.assertEqual(cfg["master"], {"interface": "enp1s0", "transport": "xdp-native"})
            cfg = {"master": {"transport": "raw"}}
            netif.fill_master(cfg, "enp2s0")
            self.assertEqual(cfg["master"], {"interface": "enp2s0", "transport": "raw"})
            cfg = {"master": {"transport": "raw"}}
            netif.fill_master(cfg, "enp1s0", keep_transport=True)
            self.assertEqual(cfg["master"]["transport"], "raw")
            cfg = {"master": {"transport": "xdp-skb"}}
            netif.fill_master(cfg, "enp1s0")
            self.assertEqual(cfg["master"]["transport"], "xdp-skb")
            cfg = {"master": {"interface": "eth0", "transport": "raw"}}
            self.assertEqual(netif.fill_master(cfg, "enp1s0"), [])
            self.assertEqual(cfg["master"], {"interface": "eth0", "transport": "raw"})
            cfg = {"master": {"transport": "raw"}}
            self.assertEqual(netif.fill_master(cfg, None), [])
            self.assertEqual(netif.default_transport("enp1s0"), "xdp-native")
            self.assertEqual(netif.default_transport("enp2s0"), "raw")
            self.assertEqual(netif.default_transport(None), "raw")


    def test_xdp_native_from_the_feature_bits(self):
        for bits, native in ((0xb, True), (0x3, True), (0x0, False), (0x2, False)):  # igc, igb, r8169
            with mock.patch.object(netif, "xdp_features", lambda name: bits):
                self.assertIs(netif.xdp_native("enp1s0"), native)

        def old_kernel(name):
            raise KeyError(netif.NETDEV_A_DEV_XDP_FEATURES)
        with mock.patch.object(netif, "xdp_features", old_kernel):
            self.assertIsNone(netif.xdp_native("enp1s0"))


class TestKernel(unittest.TestCase):
    """The real netlink question, on this computer's interfaces."""

    def test_xdp_native_answers_without_root(self):
        self.assertIn(netif.xdp_native("lo"), (False, None))
        self.assertIsNone(netif.xdp_native("no-such-if0"))
        for name in netif.interfaces():
            self.assertIn(netif.xdp_native(name), (True, False, None))


if __name__ == "__main__":
    unittest.main()
