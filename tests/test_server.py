"""server.py testleri: API şeması, path-traversal, Host doğrulaması, token (M-4)."""
import json
import os
import tempfile
import unittest
from unittest import mock
from http.client import HTTPConnection
from http.server import HTTPServer
from threading import Thread

from tether_isolator import server as srv
from tether_isolator.config import Profile, Settings


class TestHandlerSecurity(unittest.TestCase):
    """_Handler güvenlik kontrolleri (doğrudan metod testi)."""

    def setUp(self):
        self.settings = Settings()
        self.manager = mock.Mock()
        self.manager.dry = True
        self.manager.snapshot.return_value = {"phase": "idle"}
        # Handler'ı kur
        self.handler = srv._Handler.__new__(srv._Handler)
        self.handler.manager = self.manager
        self.handler.settings = self.settings
        self.handler._api_token = "test-token-123"
        self.handler.server = mock.Mock()
        self.handler.server.server_port = 8787
        self.handler.headers = mock.Mock()
        self.handler.rfile = mock.Mock()
        self.handler.wfile = mock.Mock()
        self.handler.requestline = ""
        self.handler.request_version = "HTTP/1.1"
        self.handler.client_address = ("127.0.0.1", 54321)
        self.handler.command = "GET"
        self.handler.path = "/"
        self.handler.close_connection = True

    def _set_host(self, host):
        def geth(key, default=""):
            h = {"Host": host, "Content-Type": "application/json",
                 "Authorization": "Bearer test-token-123", "Content-Length": "0"}
            return h.get(key, default)
        self.handler.headers.get = geth

    def _set_auth(self, token):
        def geth(key, default=""):
            h = {"Host": "127.0.0.1", "Content-Type": "application/json",
                 "Authorization": f"Bearer {token}", "Content-Length": "0"}
            return h.get(key, default)
        self.handler.headers.get = geth

    def test_verify_token_valid(self):
        self._set_auth("test-token-123")
        self.assertTrue(self.handler._verify_token())

    def test_verify_token_invalid(self):
        self._set_auth("wrong-token")
        self.assertFalse(self.handler._verify_token())

    def test_verify_token_missing(self):
        def geth(key, default=""):
            h = {"Host": "127.0.0.1", "Content-Type": "application/json", "Content-Length": "0"}
            return h.get(key, default)
        self.handler.headers.get = geth
        self.assertFalse(self.handler._verify_token())

    def test_verify_host_valid(self):
        self._set_host("127.0.0.1")
        self.assertTrue(self.handler._verify_host())

    def test_verify_host_invalid(self):
        self._set_host("evil.com")
        self.assertFalse(self.handler._verify_host())

    def test_verify_content_type_valid(self):
        def geth(key, default=""):
            h = {"Content-Type": "application/json", "Host": "127.0.0.1",
                 "Authorization": "Bearer test-token-123", "Content-Length": "0"}
            return h.get(key, default)
        self.handler.headers.get = geth
        self.assertTrue(self.handler._verify_content_type())

    def test_verify_content_type_invalid(self):
        def geth(key, default=""):
            h = {"Content-Type": "text/plain", "Host": "127.0.0.1",
                 "Authorization": "Bearer test-token-123", "Content-Length": "0"}
            return h.get(key, default)
        self.handler.headers.get = geth
        self.assertFalse(self.handler._verify_content_type())

    def test_path_traversal_blocked(self):
        """B-5: path-traversal koruması."""
        # Sibling dizin kaçışı
        self.handler.path = "/../etc/passwd"
        with mock.patch.object(self.handler, "send_error") as send_err:
            self.handler._static("/../etc/passwd")
            send_err.assert_called_with(404)

    def test_path_traversal_sibling_blocked(self):
        """B-5: Kardeş dizin kaçışı."""
        self.handler.path = "/style.css"  # normal
        # Sibling: /webui-x/...
        self.handler.path = "/../webui-x/evil.html"
        with mock.patch.object(self.handler, "send_error") as send_err:
            self.handler._static("/../webui-x/evil.html")
            send_err.assert_called_with(404)

    def test_static_normal_works(self):
        """Normal statik dosya isteği çalışmalı."""
        self.handler.path = "/style.css"
        with mock.patch.object(self.handler, "send_response") as send_resp, \
             mock.patch.object(self.handler, "send_header"), \
             mock.patch.object(self.handler, "end_headers"), \
             mock.patch.object(self.handler, "wfile"):
            self.handler._static("/style.css")
            # 200 dönmeli (send_error çağrılmamalı)
            # send_response çağrıldıysa başarılı
            send_resp.assert_called_with(200)


class TestServerIntegration(unittest.TestCase):
    """Gerçek HTTP sunucusu ile entegrasyon testi."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # WebUI dizinini geçiciye yönlendir
        self._orig_webui = srv.WEBUI_DIR
        srv.WEBUI_DIR = os.path.join(self.tmpdir, "webui")
        os.makedirs(srv.WEBUI_DIR)
        with open(os.path.join(srv.WEBUI_DIR, "index.html"), "w") as f:
            f.write("<html></html>")
        with open(os.path.join(srv.WEBUI_DIR, "style.css"), "w") as f:
            f.write("body {}")
        # Token dosyası
        self._orig_token = srv.TOKEN_PATH
        srv.TOKEN_PATH = os.path.join(self.tmpdir, "api.token")
        with open(srv.TOKEN_PATH, "w") as f:
            f.write("integration-test-token\n")

        self.settings = Settings()
        self.manager = mock.Mock()
        self.manager.dry = True
        self.manager.snapshot.return_value = {"phase": "idle"}
        self.manager.adopt_if_running.return_value = False

        # Sunucuyu başlat
        self.server = srv._Server(("127.0.0.1", 0), srv._Handler)
        srv._Handler.manager = self.manager
        srv._Handler.settings = self.settings
        srv._Handler._api_token = "integration-test-token"
        srv._Handler.httpd = self.server
        self.port = self.server.server_port
        self.server_thread = Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def tearDown(self):
        self.server.shutdown()
        srv.WEBUI_DIR = self._orig_webui
        srv.TOKEN_PATH = self._orig_token

    def _conn(self):
        c = HTTPConnection("127.0.0.1", self.port, timeout=5)
        return c

    def test_static_index(self):
        c = self._conn()
        c.request("GET", "/")
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        self.assertIn("text/html", r.getheader("Content-Type"))
        c.close()

    def test_api_status_no_token_returns_401(self):
        c = self._conn()
        c.request("GET", "/api/status")
        r = c.getresponse()
        self.assertEqual(r.status, 401)
        c.close()

    def test_api_status_with_token(self):
        c = self._conn()
        c.request("GET", "/api/status",
                  headers={"Authorization": "Bearer integration-test-token"})
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        data = json.loads(r.read())
        self.assertIn("state", data)
        c.close()

    def test_api_status_with_bad_host(self):
        c = self._conn()
        c.request("GET", "/api/status",
                  headers={"Authorization": "Bearer integration-test-token",
                           "Host": "evil.com"})
        r = c.getresponse()
        self.assertEqual(r.status, 403)
        c.close()

    def test_static_has_csp(self):
        c = self._conn()
        c.request("GET", "/style.css")
        r = c.getresponse()
        csp = r.getheader("Content-Security-Policy")
        self.assertIsNotNone(csp)
        self.assertIn("default-src 'self'", csp)
        c.close()


if __name__ == "__main__":
    unittest.main()
