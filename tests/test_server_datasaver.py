"""server.py: /api/data-saver ve /api/usage uçları (gerçek HTTP sunucusu ile).

Gerçek kullanıcı config'ine/usage.json'una dokunmaz: CONFIG_DIR IsolatedPaths ile,
usage.json yolu ayrıca geçici bir dosyaya yönlendirilir.
"""
import json
import os
import tempfile
import unittest
from unittest import mock
from http.client import HTTPConnection
from threading import Thread

from tether_isolator import server as srv
from tether_isolator import usage
from tether_isolator.config import Settings
from tests._util import IsolatedPaths


class TestDataSaverApi(unittest.TestCase):
    def setUp(self):
        self._isolated = IsolatedPaths()
        self._isolated.__enter__()

        self._usage_tmp = tempfile.TemporaryDirectory()
        usage_path = os.path.join(self._usage_tmp.name, "usage.json")
        self._usage_patch = mock.patch.object(usage, "_path", return_value=usage_path)
        self._usage_patch.start()

        self.tmpdir = tempfile.mkdtemp()
        self._orig_webui = srv.WEBUI_DIR
        srv.WEBUI_DIR = os.path.join(self.tmpdir, "webui")
        os.makedirs(srv.WEBUI_DIR)
        with open(os.path.join(srv.WEBUI_DIR, "index.html"), "w") as f:
            f.write("<html></html>")

        self.settings = Settings()
        self.manager = mock.Mock()
        self.manager.dry = True
        self.manager.snapshot.return_value = {"phase": "idle"}
        self.manager.active_profile = None

        self.server = srv._Server(("127.0.0.1", 0), srv._Handler)
        srv._Handler.manager = self.manager
        srv._Handler.settings = self.settings
        srv._Handler._api_token = "test-token"
        srv._Handler.httpd = self.server
        self.port = self.server.server_port
        self.server_thread = Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def tearDown(self):
        self.server.shutdown()
        srv.WEBUI_DIR = self._orig_webui
        self._usage_patch.stop()
        self._usage_tmp.cleanup()
        self._isolated.__exit__(None, None, None)

    def _conn(self):
        return HTTPConnection("127.0.0.1", self.port, timeout=5)

    def _post(self, path, body):
        c = self._conn()
        c.request("POST", path, body=json.dumps(body),
                  headers={"Content-Type": "application/json",
                           "Authorization": "Bearer test-token"})
        r = c.getresponse()
        data = json.loads(r.read())
        c.close()
        return r.status, data

    def _get(self, path):
        c = self._conn()
        c.request("GET", path, headers={"Authorization": "Bearer test-token"})
        r = c.getresponse()
        data = json.loads(r.read())
        c.close()
        return r.status, data

    def test_enable_data_saver_persists_to_profile(self):
        status, data = self._post("/api/data-saver", {"enabled": True, "level": "strict"})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        prof = self.settings.profile("default")
        self.assertTrue(prof.data_saver.enabled)
        self.assertEqual(prof.data_saver.level, "strict")

    def test_invalid_level_ignored(self):
        status, _ = self._post("/api/data-saver", {"level": "ultra"})
        self.assertEqual(status, 200)
        self.assertEqual(self.settings.profile("default").data_saver.level, "balanced")

    def test_quota_mb_must_be_numeric(self):
        status, data = self._post("/api/data-saver", {"quota_mb": "not-a-number"})
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_quota_mb_negative_clamped_to_zero(self):
        status, _ = self._post("/api/data-saver", {"quota_mb": -50})
        self.assertEqual(status, 200)
        self.assertEqual(self.settings.profile("default").data_saver.quota_mb, 0)

    def test_cap_kbit_fields_persist(self):
        status, _ = self._post("/api/data-saver", {"cap_down_kbit": 1500, "cap_up_kbit": 400})
        self.assertEqual(status, 200)
        ds = self.settings.profile("default").data_saver
        self.assertEqual(ds.cap_down_kbit, 1500)
        self.assertEqual(ds.cap_up_kbit, 400)

    def test_cap_down_kbit_must_be_numeric(self):
        status, data = self._post("/api/data-saver", {"cap_down_kbit": "fast"})
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_cap_up_kbit_must_be_numeric(self):
        status, data = self._post("/api/data-saver", {"cap_up_kbit": "fast"})
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_cap_kbit_negative_clamped_to_zero(self):
        status, _ = self._post("/api/data-saver", {"cap_down_kbit": -100, "cap_up_kbit": -1})
        self.assertEqual(status, 200)
        ds = self.settings.profile("default").data_saver
        self.assertEqual(ds.cap_down_kbit, 0)
        self.assertEqual(ds.cap_up_kbit, 0)

    def test_reassert_shaping_called_only_when_profile_is_active(self):
        # manager Mock olduğundan `prof is m.active_profile` False döner (Mock
        # kimliği eşleşmez) — reassert_shaping çağrılmamalı, hata da atmamalı.
        status, _ = self._post("/api/data-saver", {"enabled": True})
        self.assertEqual(status, 200)
        self.manager.reassert_shaping.assert_not_called()

    def test_media_level_persists_for_every_valid_value(self):
        for lvl in ("360p", "720p", "blocked", "off"):
            status, _ = self._post("/api/data-saver", {"media_level": lvl})
            self.assertEqual(status, 200)
            self.assertEqual(self.settings.profile("default").data_saver.media_level, lvl)

    def test_invalid_media_level_ignored(self):
        self._post("/api/data-saver", {"media_level": "720p"})
        status, _ = self._post("/api/data-saver", {"media_level": "1080p"})
        self.assertEqual(status, 200)
        self.assertEqual(self.settings.profile("default").data_saver.media_level, "720p")

    def test_legacy_block_media_still_accepted(self):
        status, _ = self._post("/api/data-saver", {"block_media": True})
        self.assertEqual(status, 200)
        self.assertEqual(self.settings.profile("default").data_saver.media_level, "blocked")
        status, _ = self._post("/api/data-saver", {"block_media": False})
        self.assertEqual(status, 200)
        self.assertEqual(self.settings.profile("default").data_saver.media_level, "off")

    def test_usage_endpoint_reflects_disk_state(self):
        usage.add(1000, 2000)
        status, data = self._get("/api/usage")
        self.assertEqual(status, 200)
        self.assertEqual(data["today"]["rx"], 1000)
        self.assertEqual(data["today"]["tx"], 2000)
        self.assertEqual(data["month"]["rx"], 1000)

    def test_usage_endpoint_empty_when_no_data(self):
        status, data = self._get("/api/usage")
        self.assertEqual(status, 200)
        self.assertEqual(data["today"], {"rx": 0, "tx": 0})


if __name__ == "__main__":
    unittest.main()
