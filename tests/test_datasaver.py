"""Veri tasarrufu (Faz 1+2) testleri: cimri prob politikası, dış IP önbelleği, kota.

Gerçek komut çalıştırmaz (engine._ns/public_ip sahtelenir); usage.json gerçek
kullanıcı dizinine dokunmaz (usage._path geçici dosyaya yönlendirilir).
"""
import os
import tempfile
import time
import unittest
from unittest import mock

from tether_isolator import usage
from tether_isolator.config import Profile, Settings
from tether_isolator.manager import Manager
from tether_isolator.system import RunResult


def _prof(**kw):
    kw.setdefault("auto_reconnect", False)   # testte watchdog thread'i istemeyiz
    return Profile(**kw)


class TestWatchdogInterval(unittest.TestCase):
    def test_no_active_profile_uses_base(self):
        m = Manager(Settings(watchdog_interval=4), dry_run=True)
        self.assertEqual(m._effective_watchdog_interval(), 4)

    def test_disabled_uses_base(self):
        m = Manager(Settings(watchdog_interval=4), dry_run=True)
        m._active_profile = _prof(name="d")
        self.assertEqual(m._effective_watchdog_interval(), 4)

    def test_enabled_balanced_widens_interval(self):
        m = Manager(Settings(watchdog_interval=4), dry_run=True)
        prof = _prof(name="d")
        prof.data_saver.enabled = True
        prof.data_saver.level = "balanced"
        m._active_profile = prof
        self.assertEqual(m._effective_watchdog_interval(), 10)

    def test_enabled_strict_widens_more(self):
        m = Manager(Settings(watchdog_interval=4), dry_run=True)
        prof = _prof(name="d")
        prof.data_saver.enabled = True
        prof.data_saver.level = "strict"
        m._active_profile = prof
        self.assertEqual(m._effective_watchdog_interval(), 20)

    def test_enabled_light_keeps_base(self):
        m = Manager(Settings(watchdog_interval=4), dry_run=True)
        prof = _prof(name="d")
        prof.data_saver.enabled = True
        prof.data_saver.level = "light"
        m._active_profile = prof
        self.assertEqual(m._effective_watchdog_interval(), 4)

    def test_enabled_but_frugal_probes_off_uses_base(self):
        m = Manager(Settings(watchdog_interval=4), dry_run=True)
        prof = _prof(name="d")
        prof.data_saver.enabled = True
        prof.data_saver.level = "strict"
        prof.data_saver.frugal_probes = False
        m._active_profile = prof
        self.assertEqual(m._effective_watchdog_interval(), 4)

    def test_base_larger_than_widened_wins(self):
        m = Manager(Settings(watchdog_interval=30), dry_run=True)
        prof = _prof(name="d")
        prof.data_saver.enabled = True
        prof.data_saver.level = "balanced"
        m._active_profile = prof
        self.assertEqual(m._effective_watchdog_interval(), 30)


class TestPublicIpCache(unittest.TestCase):
    def test_disabled_always_refetches(self):
        m = Manager(Settings(), dry_run=False)
        m._active_profile = _prof(name="d")
        calls = {"n": 0}

        def fake_public_ip():
            calls["n"] += 1
            return f"1.2.3.{calls['n']}"
        m.engine.public_ip = fake_public_ip
        self.assertEqual(m._get_public_ip_cached(), "1.2.3.1")
        self.assertEqual(m._get_public_ip_cached(), "1.2.3.2")

    def test_enabled_caches_within_ttl(self):
        m = Manager(Settings(), dry_run=False)
        prof = _prof(name="d")
        prof.data_saver.enabled = True
        prof.data_saver.level = "balanced"   # TTL 300s
        m._active_profile = prof
        calls = {"n": 0}

        def fake_public_ip():
            calls["n"] += 1
            return f"1.2.3.{calls['n']}"
        m.engine.public_ip = fake_public_ip
        first = m._get_public_ip_cached()
        second = m._get_public_ip_cached()
        self.assertEqual(first, second)
        self.assertEqual(calls["n"], 1)

    def test_enabled_refetches_after_ttl_expires(self):
        m = Manager(Settings(), dry_run=False)
        prof = _prof(name="d")
        prof.data_saver.enabled = True
        prof.data_saver.level = "light"   # TTL 120s
        m._active_profile = prof
        m.engine.public_ip = lambda: "9.9.9.9"
        m._get_public_ip_cached()
        m._public_ip_cache_ts = time.time() - 121   # TTL'i geçmiş say
        m.engine.public_ip = lambda: "8.8.4.4"
        second = m._get_public_ip_cached()
        self.assertEqual(second, "8.8.4.4")


class TestTrafficAndQuota(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        path = os.path.join(self._tmp.name, "usage.json")
        self._patch = mock.patch.object(usage, "_path", return_value=path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _mk_manager(self, **ds_kwargs):
        m = Manager(Settings(), dry_run=False)
        prof = _prof(name="d", uplink="usb0")
        for k, v in ds_kwargs.items():
            setattr(prof.data_saver, k, v)
        m._active_profile = prof
        m._active_iface = "usb0"
        m.state.phase = "online"
        return m

    @staticmethod
    def _fake_ns(rx, tx):
        out = (f"3: usb0: <UP> mtu 1500 RX: bytes {rx} packets 1 errors 0 dropped 0 "
               f"TX: bytes {tx} packets 1 errors 0 dropped 0")
        return lambda *a, **kw: RunResult(0, out, "")

    def test_update_traffic_stats_accumulates_and_persists(self):
        m = self._mk_manager()
        m.engine._ns = self._fake_ns(1000, 500)
        m._update_traffic_stats("usb0")   # ilk örnek: yalnızca baseline, delta yok
        self.assertEqual(m.state.traffic_rx, 0)
        m.engine._ns = self._fake_ns(1500, 700)
        m._update_traffic_stats("usb0")   # ikinci örnek: delta 500/200
        self.assertEqual(m.state.traffic_rx, 500)
        self.assertEqual(m.state.traffic_tx, 200)
        rx_today, tx_today = usage.today_bytes()
        self.assertEqual((rx_today, tx_today), (500, 200))

    def test_quota_warn_at_80_percent(self):
        m = self._mk_manager(enabled=True, quota_mb=1, quota_action="warn")
        m.engine._ns = self._fake_ns(1000, 0)
        m._update_traffic_stats("usb0")
        m.engine._ns = self._fake_ns(1000 + 850_000, 0)   # ~%85 kota
        m._update_traffic_stats("usb0")
        self.assertTrue(m.state.quota_warned)
        self.assertFalse(m.state.quota_hit)
        self.assertFalse(any(e["level"] == "error" for e in m.state.events))

    def test_quota_hit_triggers_killswitch(self):
        m = self._mk_manager(enabled=True, quota_mb=1, quota_action="killswitch")
        m.engine._ns = self._fake_ns(1000, 0)
        m._update_traffic_stats("usb0")
        m.engine._ns = self._fake_ns(1000 + 1_200_000, 0)   # %120 kota
        with mock.patch.object(m, "_apply_kill_switch") as ks:
            m._update_traffic_stats("usb0")
            ks.assert_called_once()
        self.assertTrue(m.state.quota_hit)

    def test_quota_disabled_by_default(self):
        m = self._mk_manager()   # quota_mb=0
        m.engine._ns = self._fake_ns(1000, 0)
        m._update_traffic_stats("usb0")
        m.engine._ns = self._fake_ns(50_000_000, 0)
        m._update_traffic_stats("usb0")
        self.assertFalse(m.state.quota_warned)
        self.assertFalse(m.state.quota_hit)


if __name__ == "__main__":
    unittest.main()
