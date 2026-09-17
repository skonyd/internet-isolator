"""manager.py entegrasyon testleri (dry-run ağırlıklı, gerçek komut çalışmaz)."""
import os
import unittest
from unittest import mock

from tether_isolator import system
from tether_isolator.config import Profile, Settings
from tether_isolator.engine import EngineError
from tether_isolator.manager import Manager
from tether_isolator.state import AppProcess, RuntimeState
from tests._util import IsolatedPaths


def _prof(**kw):
    kw.setdefault("auto_reconnect", False)   # testte watchdog thread'i istemeyiz
    return Profile(**kw)


class TestManagerLifecycle(unittest.TestCase):
    def test_start_online_then_stop(self):
        m = Manager(Settings(), dry_run=True)
        m.start_session(_prof(name="d", uplink="usb0", uplink_kind="usb",
                              apps=["terminator"]), "usb0")
        st = m.snapshot()
        self.assertEqual(st["phase"], "online")
        self.assertEqual(len([a for a in st["apps"] if a["running"]]), 1)
        m.stop_session()
        self.assertEqual(m.snapshot()["phase"], "idle")

    def test_double_start_rejected(self):
        m = Manager(Settings(), dry_run=True)
        m.start_session(_prof(name="d", uplink="usb0", apps=[]), "usb0")
        with self.assertRaises(EngineError):
            m.start_session(_prof(name="d", uplink="usb0", apps=[]), "usb0")

    def test_wifi_start_dry(self):
        m = Manager(Settings(), dry_run=True)
        m.start_session(_prof(name="w", uplink="wlan0", uplink_kind="wifi",
                              wifi_ssid="Ev", wifi_password="parola", apps=[]), "wlan0")
        self.assertEqual(m.snapshot()["phase"], "online")

    def test_wifi_without_creds_raises_and_resets(self):
        m = Manager(Settings(), dry_run=True)
        p = _prof(name="w", uplink="wlan0", uplink_kind="wifi", apps=[])
        with self.assertRaises(EngineError):
            m.start_session(p, "wlan0")
        # yarım kalan durum temizlendi → tekrar başlatmayı engellemez
        self.assertEqual(m.snapshot()["phase"], "idle")


class TestManagerContinuity(unittest.TestCase):
    def test_switch_uplink_keeps_apps(self):
        m = Manager(Settings(), dry_run=True)
        m.start_session(_prof(name="d", uplink="usb0", uplink_kind="usb",
                              apps=["terminator"]), "usb0")
        m.switch_uplink("wlan0", uplink_kind="wifi",
                        wifi_ssid="Ev", wifi_password="p")
        st = m.snapshot()
        self.assertEqual(st["uplink"], "wlan0")
        self.assertEqual(st["reconnect_count"], 1)
        self.assertEqual(len([a for a in st["apps"] if a["running"]]), 1)  # korundu

    def test_switch_same_uplink_rejected(self):
        m = Manager(Settings(), dry_run=True)
        m.start_session(_prof(name="d", uplink="usb0", apps=[]), "usb0")
        with self.assertRaises(EngineError):
            m.switch_uplink("usb0")

    def test_switch_without_session_rejected(self):
        m = Manager(Settings(), dry_run=True)
        with self.assertRaises(EngineError):
            m.switch_uplink("wlan0")


class TestManagerResilience(unittest.TestCase):
    def test_start_failure_cleans_up_and_allows_retry(self):
        m = Manager(Settings(), dry_run=True)
        p = _prof(name="d", uplink="usb0", apps=[])
        with mock.patch.object(m, "_bring_uplink_into_ns",
                               side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                m.start_session(p, "usb0")
        self.assertEqual(m.snapshot()["phase"], "idle")
        # phase 'starting'te takılı kalmadığından tekrar başlatılabilir
        m.start_session(p, "usb0")
        self.assertEqual(m.snapshot()["phase"], "online")

    def test_app_launch_failure_does_not_kill_session(self):
        m = Manager(Settings(), dry_run=True)
        with mock.patch("tether_isolator.apps.launch",
                        side_effect=RuntimeError("app patladı")):
            m.start_session(_prof(name="d", uplink="usb0", apps=["opera"]), "usb0")
        # uygulama patlasa da oturum ayakta
        self.assertEqual(m.snapshot()["phase"], "online")

    def test_stop_session_terminates_apps_no_orphans(self):
        """'Durdur' orphan bırakmasın diye uygulamaları sonlandırmalı."""
        m = Manager(Settings(), dry_run=True)
        m.start_session(_prof(name="d", uplink="usb0", apps=["terminator"]), "usb0")
        with mock.patch.object(m, "_terminate_apps") as term:
            m.stop_session()
        term.assert_called_once()

    def test_terminate_apps_actually_kills_process_group(self):
        import subprocess
        import time as _t
        m = Manager(Settings(), dry_run=False)
        proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
        m.state.apps = [AppProcess("sleep", proc.pid)]
        m._terminate_apps()
        for _ in range(40):
            if proc.poll() is not None:
                break
            _t.sleep(0.05)
        self.assertIsNotNone(proc.poll())   # süreç sonlandırıldı (orphan kalmadı)


class TestManagerAdoption(unittest.TestCase):
    def test_adopt_resumes_existing_session(self):
        with IsolatedPaths():
            prev = RuntimeState(phase="online", profile="default", uplink="wlan0",
                                namespace="tether_zone", reconnect_count=2)
            prev.apps = [AppProcess("terminator", os.getpid())]  # yaşayan pid
            prev.persist()

            m = Manager(Settings(), dry_run=False)
            m.s.profiles["default"] = Profile(name="default", auto_reconnect=False)
            with mock.patch.object(system, "namespace_exists", return_value=True), \
                 mock.patch.object(system, "interface_in_namespace", return_value=True), \
                 mock.patch.object(m, "_refresh_network",
                                   side_effect=lambda light=False: setattr(m.state, "online", True)):
                adopted = m.adopt_if_running()

            self.assertTrue(adopted)
            st = m.snapshot()
            self.assertEqual(st["uplink"], "wlan0")
            self.assertEqual(st["phase"], "online")
            self.assertEqual(st["reconnect_count"], 2)
            self.assertEqual(len(st["apps"]), 1)   # yaşayan uygulama geri alındı

    def test_adopt_false_without_namespace(self):
        m = Manager(Settings(), dry_run=False)
        with mock.patch.object(system, "namespace_exists", return_value=False):
            self.assertFalse(m.adopt_if_running())

    def test_adopt_false_in_dry(self):
        self.assertFalse(Manager(Settings(), dry_run=True).adopt_if_running())


if __name__ == "__main__":
    unittest.main()
