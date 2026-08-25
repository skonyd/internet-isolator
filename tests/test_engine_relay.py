"""engine.py + relay.py testleri: dry davranışı ve gerçek komut kurulumu."""
import unittest
from unittest import mock

from tether_isolator import engine as eng
from tether_isolator import relay as rly
from tether_isolator import system
from tether_isolator.config import Settings, RelayPolicy
from tether_isolator.system import RunResult
from tests._util import FakeRun


class TestEngineDry(unittest.TestCase):
    def setUp(self):
        self.e = eng.Engine(Settings(), dry_run=True)

    def test_connectivity_true_in_dry(self):
        self.assertTrue(self.e.connectivity())

    def test_wait_wifi_associated_true_in_dry(self):
        self.assertTrue(self.e.wait_wifi_associated("wlan0"))

    def test_uplink_ip_returns_pair(self):
        ip, gw = self.e.uplink_ip("wlan0")
        self.assertTrue(ip and gw)

    def test_move_wifi_phy_in_noop_in_dry(self):
        self.e.move_wifi_phy_in("wlan0")   # hata fırlatmamalı

    def test_probe_true_in_dry(self):
        self.assertTrue(self.e.probe(["10.150.0.5"]))


class TestEngineCommands(unittest.TestCase):
    """Gerçek modda üretilen komutları FakeRun ile yakala (komut çalışmaz)."""

    def test_move_uplink_in_builds_commands(self):
        e = eng.Engine(Settings(), dry_run=False)
        fake = FakeRun()
        with mock.patch.object(eng, "run", fake), \
             mock.patch.object(system, "interface_in_namespace", return_value=False):
            e.move_uplink_in("usb0")
        self.assertTrue(fake.ran("link", "set", "usb0", "netns", "tether_zone"))
        self.assertTrue(fake.ran("netns", "exec", "tether_zone", "up"))

    def test_wifi_phy_in_uses_netns_name(self):
        e = eng.Engine(Settings(), dry_run=False)
        fake = FakeRun()
        with mock.patch.object(eng, "run", fake), \
             mock.patch.object(system, "detect_wifi_phy", return_value="phy0"), \
             mock.patch.object(system, "have", return_value=True):
            e.move_wifi_phy_in("wlan0")
        # ölü-PID değil, isimle namespace kullanılmalı
        self.assertTrue(fake.ran("iw", "phy", "phy0", "set", "netns", "name", "tether_zone"))

    def test_probe_any_reachable(self):
        e = eng.Engine(Settings(), dry_run=False)
        fake = FakeRun(RunResult(1, "", ""))          # varsayılan: ulaşılamaz
        fake.add_rule(lambda c: "10.150.0.6" in c, RunResult(0, "", ""))
        with mock.patch.object(eng, "run", fake):
            self.assertTrue(e.probe(["10.150.0.5", "10.150.0.6"]))

    def test_probe_none_reachable(self):
        e = eng.Engine(Settings(), dry_run=False)
        fake = FakeRun(RunResult(1, "", ""))
        with mock.patch.object(eng, "run", fake):
            self.assertFalse(e.probe(["10.150.0.5", "10.150.0.6"]))

    def test_wifi_associate_waits_no_dhcp_here(self):
        e = eng.Engine(Settings(), dry_run=False)
        fake = FakeRun()
        with mock.patch.object(eng, "run", fake), \
             mock.patch("builtins.open", mock.mock_open()), \
             mock.patch("os.makedirs"), mock.patch("os.chmod"):
            e.wifi_associate("wlan0", "SSID", "parola")
        self.assertTrue(fake.ran("wpa_supplicant", "-D", "nl80211", "-i", "wlan0"))


class TestRelay(unittest.TestCase):
    def _fake_with_routes(self, table):
        """veth-present + host rotalarını döndüren FakeRun kur."""
        fake = FakeRun()
        # _ns_veth_present() true dönsün diye 'show tisor-ns' çıktısı ver
        fake.add_rule(lambda c: "show" in c and rly.NS_VETH in c,
                      RunResult(0, rly.NS_VETH, ""))
        # 'ip -4 route show' host rotalarını döndürsün
        fake.add_rule(lambda c: c[:3] == ["ip", "-4", "route"],
                      RunResult(0, table, ""))
        return fake

    _TABLE = (
        "default via 192.168.1.1 dev wlan0\n"
        "10.0.15.0/24 dev eth0 proto kernel scope link src 10.0.15.42\n"
        "172.16.5.0/24 via 10.0.15.1 dev eth0\n"
        "10.77.0.0/30 dev tisor-host proto kernel scope link src 10.77.0.1\n"
    )

    def test_enable_mirrors_lan_routes_and_masquerades(self):
        r = rly.Relay(Settings(), dry_run=False)
        fake = self._fake_with_routes(self._TABLE)
        with mock.patch.object(rly, "run", fake), \
             mock.patch.object(system, "have", side_effect=lambda t: t == "nft"):
            routes = r.enable(RelayPolicy())
        # veth kuruldu
        self.assertTrue(fake.ran("link", "add", rly.HOST_VETH, "type", "veth"))
        self.assertTrue(fake.ran("link", "set", rly.NS_VETH, "netns", "tether_zone"))
        # host'un ulaştığı ağlar izole alana aynalandı
        self.assertTrue(fake.ran("route", "replace", "10.0.15.0/24"))
        self.assertTrue(fake.ran("route", "replace", "172.16.5.0/24"))
        # varsayılan rota DEĞİŞTİRİLMEZ → internet tether'de kalır
        self.assertFalse(fake.ran("route", "replace", "default"))
        # NAT + FORWARD izni
        self.assertTrue(fake.ran("masquerade"))
        self.assertIn("10.0.15.0/24", routes)
        self.assertIn("172.16.5.0/24", routes)

    def test_enable_masquerade_iptables_fallback(self):
        r = rly.Relay(Settings(), dry_run=False)
        fake = self._fake_with_routes(self._TABLE)
        with mock.patch.object(rly, "run", fake), \
             mock.patch.object(system, "have",
                               side_effect=lambda t: t == "iptables"):
            r.enable(RelayPolicy())
        self.assertTrue(fake.ran("iptables", "-I", "FORWARD"))     # DROP'u aşan izin
        self.assertTrue(fake.ran("MASQUERADE"))

    def test_enable_routes_extra_targets_via_host(self):
        r = rly.Relay(Settings(), dry_run=False)
        fake = self._fake_with_routes(self._TABLE)
        with mock.patch.object(rly, "run", fake), \
             mock.patch.object(system, "have", side_effect=lambda t: t == "nft"), \
             mock.patch("socket.getaddrinfo",
                        return_value=[(2, 1, 6, "", ("195.214.160.160", 0))]):
            routes = r.enable(RelayPolicy(extra_targets=["mail.havelsan.com.tr",
                                                         "203.0.113.5"]))
        # alan adı çözülüp /32 rota olarak host üzerinden geçirildi
        self.assertTrue(fake.ran("route", "replace", "195.214.160.160/32"))
        self.assertTrue(fake.ran("route", "replace", "203.0.113.5/32"))
        self.assertIn("195.214.160.160/32", routes)

    def test_resolve_extra_ip_cidr_and_bad(self):
        r = rly.Relay(Settings(), dry_run=False)
        with mock.patch("socket.getaddrinfo", side_effect=OSError("nxdomain")):
            got = r._resolve_extra(["10.0.15.5", "10.9.0.0/24", "", "yok.invalid"])
        self.assertEqual(got, ["10.0.15.5/32", "10.9.0.0/24"])   # çözülemeyen atlandı

    def test_enable_raises_if_veth_absent(self):
        r = rly.Relay(Settings(), dry_run=False)
        fake = FakeRun()   # _ns_veth_present false (kural yok → boş çıktı)
        with mock.patch.object(rly, "run", fake):
            with self.assertRaises(eng.EngineError):
                r.enable(RelayPolicy())

    def test_host_lan_routes_skips_default_and_veth(self):
        r = rly.Relay(Settings(), dry_run=False)
        fake = FakeRun(RunResult(0, self._TABLE, ""))
        with mock.patch.object(rly, "run", fake):
            got = r._host_lan_routes()
        self.assertIn("10.0.15.0/24", got)
        self.assertIn("172.16.5.0/24", got)
        self.assertNotIn("10.77.0.0/30", got)   # relay veth atlanır
        self.assertNotIn("0.0.0.0/0", got)       # varsayılan (internet) atlanır

    def test_is_active_dry_false(self):
        r = rly.Relay(Settings(), dry_run=True)
        # dry'da run boş çıktı döner → HOST_VETH yok → aktif değil
        self.assertFalse(r.is_active())


class TestResolvConf(unittest.TestCase):
    """İzole alanın resolv.conf'u — telefon hotspot'undaki yavaş DNS düzeltmesi.

    glibc A ve AAAA sorgularını aynı porttan paralel gönderir; basit NAT'lar
    (iPhone hotspot vb.) ikinci yanıtı düşürünce resolver tam timeout bekler ve
    HER isim çözümlemesi ~5 sn sürer. Ölçüm (5 taze alan adı, aynı hat):
    varsayılan 26,8 sn — single-request-reopen 0,87 sn.
    """

    def setUp(self):
        self.e = eng.Engine(Settings(), dry_run=False)

    def _write(self, dns):
        """resolv.conf'a YAZILAN içeriği döndürür (gerçek dosya sistemine dokunmadan)."""
        opened = mock.mock_open()
        with mock.patch("builtins.open", opened), \
             mock.patch.object(eng.os, "makedirs"):
            self.e._write_resolv(dns)
        handle = opened()
        return "".join(c.args[0] for c in handle.write.call_args_list)

    def test_nameservers_written_in_order(self):
        out = self._write(["8.8.8.8", "1.1.1.1"])
        self.assertIn("nameserver 8.8.8.8", out)
        self.assertIn("nameserver 1.1.1.1", out)
        self.assertLess(out.index("8.8.8.8"), out.index("1.1.1.1"))

    def test_single_request_reopen_option_present(self):
        """ASIL REGRESYON KORUMASI: bu seçenek düşerse DNS 30 kat yavaşlar."""
        out = self._write(["8.8.8.8"])
        self.assertIn("single-request-reopen", out)

    def test_timeout_bounded(self):
        """Paket yine de kaybolursa 5 sn yerine kısa beklensin (emniyet ağı)."""
        out = self._write(["8.8.8.8"])
        self.assertIn("timeout:2", out)

    def test_options_line_comes_after_nameservers(self):
        out = self._write(["8.8.8.8", "1.1.1.1"])
        self.assertLess(out.index("nameserver 1.1.1.1"), out.index("options "))

    def test_single_options_line_only(self):
        out = self._write(["8.8.8.8", "1.1.1.1"])
        self.assertEqual(out.count("options "), 1)


if __name__ == "__main__":
    unittest.main()
