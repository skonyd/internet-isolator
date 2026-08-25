"""server.py: /api/restart TAM SIFIRLAMA yapar.

Üç eylemin net biçimde ayrıştığını doğrular:
    Çıkış (/api/quit)             → paneli kapat, oturum arka planda YAŞAR
    Durdur (/api/stop)            → oturumu yık, daemon çalışmaya devam eder
    Yeniden Başlat (/api/restart) → oturumu yık VE daemon'ı tazele (execv)
"""
import json
import os
import tempfile
import time
import unittest
from unittest import mock
from http.client import HTTPConnection
from threading import Thread

from tether_isolator import server as srv
from tether_isolator.config import Settings
from tests._util import IsolatedPaths


class TestRestartResetsEverything(unittest.TestCase):
    def setUp(self):
        self._isolated = IsolatedPaths()
        self._isolated.__enter__()

        self.tmpdir = tempfile.mkdtemp()
        self._orig_webui = srv.WEBUI_DIR
        srv.WEBUI_DIR = os.path.join(self.tmpdir, "webui")
        os.makedirs(srv.WEBUI_DIR)
        with open(os.path.join(srv.WEBUI_DIR, "index.html"), "w") as f:
            f.write("<html></html>")

        self.settings = Settings()
        self.manager = mock.Mock()
        self.manager.dry = True
        self.manager.snapshot.return_value = {"phase": "online"}

        self.server = srv._Server(("127.0.0.1", 0), srv._Handler)
        srv._Handler.manager = self.manager
        srv._Handler.settings = self.settings
        srv._Handler._api_token = "test-token"
        srv._Handler.httpd = self.server
        self.port = self.server.server_port
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        # Modül seviyesi bayrağı testler arası sızdırmasın.
        srv._restart_requested = False

    def tearDown(self):
        self.server.shutdown()
        srv.WEBUI_DIR = self._orig_webui
        srv._restart_requested = False
        self._isolated.__exit__(None, None, None)

    def _post(self, path):
        c = HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", path, body="{}",
                  headers={"Content-Type": "application/json",
                           "Authorization": "Bearer test-token"})
        r = c.getresponse()
        data = json.loads(r.read())
        c.close()
        return r.status, data

    def test_restart_stops_session(self):
        """ASIL GARANTİ: yeniden başlatma her şeyi sıfırlamalı."""
        with mock.patch.object(srv.system, "kill_stray_daemons", return_value=[]):
            status, data = self._post("/api/restart")
        self.assertEqual(status, 200)
        self.assertTrue(data.get("restarting"))
        self.manager.stop_session.assert_called_once()

    def test_restart_survives_stop_session_failure(self):
        """Oturum durdurulamasa bile yeniden başlatma yine de ilerlemeli."""
        self.manager.stop_session.side_effect = RuntimeError("teardown patladı")
        with mock.patch.object(srv.system, "kill_stray_daemons", return_value=[]):
            status, data = self._post("/api/restart")
        self.assertEqual(status, 200)
        self.assertTrue(data.get("restarting"))

    def test_restart_sets_restart_flag(self):
        with mock.patch.object(srv.system, "kill_stray_daemons", return_value=[]):
            self._post("/api/restart")
        # Handler yanıtı ÖNCE gönderip bayrağı sonra set ediyor; kısa bir yarış
        # penceresi var (ürün hatası değil, sıralama tercihi) → kısaca bekle.
        for _ in range(50):
            if srv._restart_requested:
                break
            time.sleep(0.02)
        self.assertTrue(srv._restart_requested)

    def test_restart_cleans_stray_daemons_and_wifi_helpers(self):
        """Oturum yıkıldığı için sahipsiz kalan yardımcı süreçler de temizlenmeli."""
        with mock.patch.object(srv.system, "kill_stray_daemons",
                               return_value=[]) as killer:
            self._post("/api/restart")
        patterns = [c[0][0] for c in killer.call_args_list]
        self.assertIn("tether_isolator gui", patterns)          # kopya daemon temizliği
        self.assertTrue([p for p in patterns if "wpa_" in p],   # WiFi yardımcıları
                        "sahipsiz wpa_supplicant süreçleri temizlenmeli")

    def test_restart_never_kills_own_ancestors(self):
        """REGRESYON: pkexec sarmalayıcısı da 'tether_isolator gui' desenine uyar.

        Onu öldürmek çocuk daemon'ı da düşürüyordu → süreç execv'e ulaşamıyor,
        panel bir daha açılmıyordu ("yeniden başlat dedim ama başlamadı").
        """
        fake_ancestors = {4242, 4243}
        with mock.patch.object(srv.system, "process_ancestors",
                               return_value=fake_ancestors), \
             mock.patch.object(srv.system, "kill_stray_daemons",
                               return_value=[]) as killer:
            self._post("/api/restart")
        self.assertTrue(killer.call_args_list, "temizlik hiç çağrılmadı")
        for call in killer.call_args_list:
            excluded = call.kwargs.get("exclude_pids")
            self.assertIsNotNone(excluded, "ata koruması geçirilmemiş")
            self.assertTrue(fake_ancestors.issubset(set(excluded)),
                            "ata süreçler koruma listesinde değil")

    def test_process_ancestors_walks_ppid_chain(self):
        """Gerçek /proc üzerinden: kendi atalarımız bulunmalı, kendimiz değil."""
        me = os.getpid()
        anc = srv.system.process_ancestors(me)
        self.assertNotIn(me, anc)
        self.assertIn(os.getppid(), anc)

    def test_stop_tears_everything_down(self):
        """'Durdur' tam yıkım yapar ama daemon'ı yeniden başlatMAZ."""
        status, _ = self._post("/api/stop")
        self.assertEqual(status, 200)
        self.manager.stop_session.assert_called_once()
        self.assertFalse(srv._restart_requested)

    def test_quit_does_not_stop_session(self):
        status, data = self._post("/api/quit")
        self.assertEqual(status, 200)
        self.assertTrue(data.get("bye"))
        self.manager.stop_session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
