"""vpn.py testleri: komut kurulumu, dry davranışı, tun tespiti."""
import unittest
from unittest import mock

from tether_isolator import vpn as vpnmod
from tether_isolator import engine as eng
from tether_isolator.config import Settings
from tether_isolator.system import RunResult
from tests._util import FakeRun


class TestVPNDry(unittest.TestCase):
    def setUp(self):
        self.v = vpnmod.VPN(Settings(), dry_run=True)

    def test_connect_noop_in_dry(self):
        self.v.connect("/tmp/x.ovpn")           # hata fırlatmamalı

    def test_wait_connected_true_in_dry(self):
        self.assertTrue(self.v.wait_connected())

    def test_is_active_false_in_dry(self):
        self.assertFalse(self.v.is_active())


class TestVPNCommands(unittest.TestCase):
    def test_connect_builds_netns_openvpn_command(self):
        v = vpnmod.VPN(Settings(), dry_run=False)
        fake = FakeRun()
        with mock.patch.object(vpnmod, "run", fake), \
             mock.patch.object(vpnmod.system, "have", return_value=True), \
             mock.patch("os.path.isfile", return_value=True), \
             mock.patch("os.makedirs"), mock.patch("os.remove"), \
             mock.patch.object(v, "disconnect"), \
             mock.patch.object(v, "_dco_supported", return_value=True), \
             mock.patch.object(v, "wait_connected", return_value=True), \
             mock.patch.object(v, "status",
                               return_value={"active": True, "iface": "tun0", "ip": "10.8.0.2/24"}):
            v.connect("/home/u/garageliman.ovpn")
        self.assertTrue(fake.ran("ip", "netns", "exec", "tether_zone", "openvpn"))
        self.assertTrue(fake.ran("--config", "/home/u/garageliman.ovpn"))
        self.assertTrue(fake.ran("--daemon", "tisor-vpn"))
        self.assertTrue(fake.ran("--disable-dco"))   # netns için DCO kapalı
        # eski sunucular (garageliman) için legacy CBC de kabul edilsin
        self.assertTrue(fake.ran("--data-ciphers", vpnmod.DATA_CIPHERS))
        self.assertIn("AES-128-CBC", vpnmod.DATA_CIPHERS)

    def test_connect_missing_binary_raises(self):
        v = vpnmod.VPN(Settings(), dry_run=False)
        with mock.patch.object(vpnmod.system, "have", return_value=False):
            with self.assertRaises(eng.EngineError):
                v.connect("/x.ovpn")

    def test_connect_missing_file_raises(self):
        v = vpnmod.VPN(Settings(), dry_run=False)
        with mock.patch.object(vpnmod.system, "have", return_value=True), \
             mock.patch("os.path.isfile", return_value=False), \
             mock.patch("os.makedirs"):
            with self.assertRaises(eng.EngineError):
                v.connect("/nope.ovpn")

    def test_tun_iface_detection(self):
        v = vpnmod.VPN(Settings(), dry_run=False)
        out = ("1: lo: <LOOPBACK>\n"
               "2: enx0: <BROADCAST>\n"
               "5: tun0: <POINTOPOINT,UP>\n")
        fake = FakeRun(RunResult(0, out, ""))
        with mock.patch.object(vpnmod, "run", fake):
            self.assertEqual(v.tun_iface(), "tun0")


if __name__ == "__main__":
    unittest.main()
