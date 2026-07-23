"""Yerel HTTP daemon — JSON API + web arayüzü sunumu.

Bağımlılık yok: yalnızca standart kütüphane (http.server). Yalnızca
127.0.0.1'e bağlanır. Ağ ayrıcalığı gerektiren işlemleri root olarak
çalışan bu daemon yapar; uygulamalar gerçek kullanıcı kimliğiyle başlatılır.
"""
from __future__ import annotations

import json
import logging
import os
import socketserver
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import apps, system
from .config import Profile, Settings
from .manager import Manager

log = logging.getLogger("tether.server")

WEBUI_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "webui")
_CTYPES = {".html": "text/html; charset=utf-8", ".css": "text/css",
           ".js": "application/javascript", ".svg": "image/svg+xml",
           ".png": "image/png", ".ico": "image/x-icon"}


class _Server(ThreadingHTTPServer):
    """ThreadingHTTPServer; başlatılırken ters-DNS (getfqdn) yapmaz.

    Varsayılan HTTPServer.server_bind, server_name için socket.getfqdn(host)
    çağırır; bozuk/yavaş çözümleyicide (ör. takılı netns kalıntıları) bu çağrı
    kilitlenebilir ve listen() hiç çağrılmadığı için sunucu sessizce asılır.
    Yerel bir araç için ters-DNS gereksiz; bu yüzden atlıyoruz.
    """
    allow_reuse_address = True
    daemon_threads = True

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


class _Handler(BaseHTTPRequestHandler):
    server_version = "TetherIsolator/3.0"
    manager: Manager
    settings: Settings
    httpd = None  # serve() içinde atanır; /api/quit için gerekir

    # logları azalt
    def log_message(self, fmt, *args):  # noqa: N802
        log.debug("http: " + fmt, *args)

    # ----------------------------------------------------------- yardımcı
    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    # ------------------------------------------------------------------ GET
    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            return self._api_get(path)
        return self._static(path)

    def _api_get(self, path: str):
        if path == "/api/status":
            return self._json(self._status_payload())
        if path == "/api/interfaces":
            return self._json({"interfaces": [vars(i) for i in
                                              system.list_host_interfaces()]})
        return self._json({"error": "bulunamadı"}, 404)

    def _status_payload(self) -> dict:
        s = self.settings
        return {
            "version": "3.0.0",
            "state": self.manager.snapshot(),
            "interfaces": [vars(i) for i in system.list_host_interfaces()],
            "installed_apps": apps.discover_installed(),
            "profiles": {n: _profile_view(p) for n, p in s.profiles.items()},
            "active_profile": s.active_profile,
            "is_root": system.is_root(),
            "dry_run": self.manager.dry,
        }

    def _static(self, path: str):
        if path in ("/", ""):
            path = "/index.html"
        safe = os.path.normpath(path).lstrip("/")
        full = os.path.join(WEBUI_DIR, safe)
        if not full.startswith(WEBUI_DIR) or not os.path.isfile(full):
            self.send_error(404)
            return
        ext = os.path.splitext(full)[1]
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", _CTYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ----------------------------------------------------------------- POST
    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        body = self._body()
        try:
            return self._api_post(path, body)
        except Exception as e:  # noqa: BLE001
            log.exception("API hatası")
            return self._json({"error": str(e)}, 400)

    def _api_post(self, path: str, body: dict):
        m, s = self.manager, self.settings
        if path == "/api/start":
            pname = body.get("profile") or s.active_profile
            prof = s.profile(pname)
            uplink = body.get("uplink") or prof.uplink
            if not uplink:
                return self._json({"error": "uplink seçilmedi"}, 400)
            # gelen seçimleri profile yansıt (ve kalıcı yap)
            prof.uplink = uplink
            if "apps" in body:
                prof.apps = list(body["apps"])
            if "use_system_profile" in body:
                prof.use_system_profile = bool(body["use_system_profile"])
            if body.get("uplink_kind"):
                prof.uplink_kind = body["uplink_kind"]
            if "wifi_ssid" in body:
                prof.wifi_ssid = body["wifi_ssid"]
            if body.get("wifi_password"):   # boşsa kayıtlı parolayı koru
                prof.wifi_password = body["wifi_password"]
            s.active_profile = pname
            s.save()
            m.start_session(prof, uplink)
            return self._json({"ok": True, "state": m.snapshot()})

        if path == "/api/switch-uplink":
            new = body.get("uplink")
            if not new:
                return self._json({"error": "uplink seçilmedi"}, 400)
            m.switch_uplink(
                new,
                uplink_kind=body.get("uplink_kind"),
                wifi_ssid=body.get("wifi_ssid"),
                wifi_password=body.get("wifi_password"),
            )
            s.save()
            return self._json({"ok": True, "state": m.snapshot()})

        if path == "/api/stop":
            m.stop_session()
            return self._json({"ok": True, "state": m.snapshot()})

        if path == "/api/relay":
            if body.get("enabled"):
                extra = body.get("extra_targets")
                if isinstance(extra, str):
                    extra = [x.strip() for x in extra.replace(";", ",").split(",")]
                extra = [x for x in (extra or []) if x] if extra is not None else None
                m.enable_relay(extra_targets=extra)
                prof = m._active_profile
                if prof is not None and extra is not None:
                    prof.relay.extra_targets = list(extra)
                    s.save()
            else:
                m.disable_relay()
            return self._json({"ok": True, "state": m.snapshot()})

        if path == "/api/vpn":
            if body.get("enabled"):
                cfg = (body.get("config") or "").strip() or None
                m.connect_vpn(cfg)
                prof = m._active_profile
                if prof is not None and cfg:
                    prof.vpn_config = cfg
                    s.save()
            else:
                m.disconnect_vpn()
            return self._json({"ok": True, "state": m.snapshot()})

        if path == "/api/launch":
            prog = body.get("app")
            if not prog:
                return self._json({"error": "uygulama belirtilmedi"}, 400)
            pid = m.launch_app(prog)
            return self._json({"ok": True, "pid": pid, "state": m.snapshot()})

        if path == "/api/reconnect":
            m.force_reconnect()
            return self._json({"ok": True, "state": m.snapshot()})

        if path == "/api/profile":
            # profil oluştur/güncelle
            data = body.get("profile") or {}
            name = data.get("name")
            if not name:
                return self._json({"error": "profil adı gerekli"}, 400)
            s.profiles[name] = Profile.from_dict(data)
            s.active_profile = name
            s.save()
            return self._json({"ok": True})

        if path == "/api/quit":
            # Çıkış = yalnızca paneli/daemon'u kapat. İzole oturum + uygulamalar
            # ÇALIŞMAYA DEVAM EDER; daemon tekrar açılınca devralınır (kaldığın
            # yerden devam). Tam durdurma için 'Durdur' (/api/stop) kullanılır.
            self._json({"ok": True, "bye": True})
            if self.httpd is not None:
                threading.Thread(target=self.httpd.shutdown, daemon=True).start()
            return None

        return self._json({"error": "bulunamadı"}, 404)


def serve(settings: Settings, *, dry_run: bool = False, open_browser: bool = True):
    manager = Manager(settings, dry_run=dry_run)
    _Handler.manager = manager
    _Handler.settings = settings
    # Daemon yeniden başladıysa ve izole oturum hâlâ ayaktaysa devral.
    if manager.adopt_if_running():
        log.info("Mevcut izole oturum devralındı — kaldığın yerden devam.")
    httpd = _Server((settings.http_host, settings.http_port), _Handler)
    _Handler.httpd = httpd
    url = f"http://{settings.http_host}:{settings.http_port}/"
    log.info("Tether Isolator paneli: %s%s", url, "  [DRY-RUN]" if dry_run else "")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("kapatılıyor...")
    finally:
        # ÖNEMLİ: çıkışta oturumu teardown ETME. İzole oturum + uygulamalar
        # yaşamaya devam etsin ki daemon yeniden açılınca devralıp kaldığın
        # yerden devam edebilelim. Tam durdurma yalnızca 'Durdur' (/api/stop)
        # ile yapılır. Watchdog thread'ini yalnızca durdur.
        manager._stop_watchdog()
        httpd.server_close()


def _profile_view(p: Profile) -> dict:
    """Web'e gönderilecek güvenli profil görünümü (parola gizlenir)."""
    from dataclasses import asdict
    d = asdict(p)
    if d.get("wifi_password"):
        d["wifi_password"] = "••••••"
    return d
