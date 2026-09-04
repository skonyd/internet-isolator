"""Yerel HTTP daemon — JSON API + web arayüzü sunumu.

Bağımlılık yok: yalnızca standart kütüphane (http.server). Yalnızca
127.0.0.1'e bağlanır. Ağ ayrıcalığı gerektiren işlemleri root olarak
çalışan bu daemon yapar; uygulamalar gerçek kullanıcı kimliğiyle başlatılır.

Güvenlik:
  - API token (Bearer) doğrulaması (G-1)
  - Host başlığı doğrulaması + Content-Type zorunluluğu (G-2)
  - İstek gövdesi boyut sınırı (G-5)
  - Hata mesajlarında bilgi sızıntısı önleme (G-4)
  - Path-traversal koruması (B-5)
  - CSP başlığı (G-9)
"""
from __future__ import annotations

import errno
import json
import logging
import os
import secrets
import signal
import socket
import socketserver
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import __version__, apps, system, usage
from .config import Profile, Settings, VPNS_DIR
from .engine import EngineError
from .manager import Manager

log = logging.getLogger("tether.server")

WEBUI_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "webui")
_CTYPES = {".html": "text/html; charset=utf-8", ".css": "text/css",
           ".js": "application/javascript", ".svg": "image/svg+xml",
           ".png": "image/png", ".ico": "image/x-icon"}

# İstek gövdesi boyut sınırı (G-5)
MAX_BODY_SIZE = 1_048_576  # 1 MB

# Token dosya yolu (G-1)
TOKEN_PATH = "/run/tether-isolator/api.token"

# /api/restart tarafından ayarlanır; serve_forever() döndükten sonra `serve()`
# bu bayrağa bakıp süreci kendi üzerine yeniden başlatır (execv). Modül seviyesi
# bir bayrak kullanılır çünkü istek thread'i ile serve_forever()'ı çalıştıran
# ana thread ayrı; ikisi arasında en basit iletişim yolu budur.
_restart_requested = False


def request_restart() -> None:
    global _restart_requested
    _restart_requested = True


def _chown_to_real_user(*paths: str) -> None:
    """Verilen yolları gerçek kullanıcıya devreder (root iken, best-effort)."""
    if not system.is_root():
        return
    import pwd
    try:
        pw = pwd.getpwnam(system.real_user())
    except KeyError:
        return
    for p in paths:
        try:
            os.chown(p, pw.pw_uid, pw.pw_gid)
        except OSError:
            pass


def _ensure_vpns_dir() -> None:
    os.makedirs(VPNS_DIR, exist_ok=True)
    _chown_to_real_user(VPNS_DIR)


def _ensure_token() -> str:
    """Daemon açılışında rastgele bir API token'ı üretir ve 0600 izinli dosyaya yazar."""
    token_path = TOKEN_PATH
    try:
        os.makedirs(os.path.dirname(token_path), exist_ok=True)
    except OSError:
        pass
    # Mevcut token varsa yeniden kullan (yeniden başlatmada tutarlılık); yoksa üret.
    token = ""
    try:
        with open(token_path) as f:
            token = f.read().strip()
    except OSError:
        pass
    if not token:
        token = secrets.token_urlsafe(32)
        try:
            with open(token_path, "w") as f:
                f.write(token + "\n")
        except OSError:
            log.warning("token dosyası yazılamadı: %s", token_path)
            return token
    # İzin + sahipliği HER DURUMDA uygula (mevcut token yeniden kullanılsa bile):
    # daemon root çalışır ama tarayıcıyı/başlatıcıyı gerçek kullanıcı çalıştırır.
    # 0600 + kullanıcı sahipliği → token yalnızca o kullanıcıya okunur (diğer yerel
    # kullanıcılar erişemez), ama başlatıcı token'ı okuyup panele iletebilir. Bu adım
    # atlanırsa eski root-sahipli token dosyası kullanıcıya okunamaz ve panel boş kalır.
    try:
        os.chmod(token_path, 0o600)
        import pwd
        pw = pwd.getpwnam(system.real_user())
        os.chown(token_path, pw.pw_uid, pw.pw_gid)
    except (KeyError, OSError):
        pass
    return token


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
    server_version = f"TetherIsolator/{__version__}"
    manager: Manager
    settings: Settings
    httpd = None  # serve() içinde atanır; /api/quit için gerekir
    _api_token: str = ""  # G-1: token doğrulaması

    # logları azalt
    def log_message(self, fmt, *args):  # noqa: N802
        log.debug("http: " + fmt, *args)

    # ------------------------------------------------------ güvenlik yardımcıları
    def _verify_token(self) -> bool:
        """Bearer token doğrulaması (G-1)."""
        if not self._api_token:
            return True  # token yoksa eski davranış (güvenlik dışı)
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and auth[7:] == self._api_token:
            return True
        return False

    def _verify_host(self) -> bool:
        """Host başlığı doğrulaması — DNS-rebinding koruması (G-2)."""
        host = self.headers.get("Host", "")
        # Port kısmını ayır
        hostname = host.split(":")[0] if ":" in host else host
        allowed = {"127.0.0.1", "localhost", f"127.0.0.1:{self.server.server_port}",
                   f"localhost:{self.server.server_port}"}
        return hostname in {"127.0.0.1", "localhost"}

    def _verify_content_type(self) -> bool:
        """POST isteklerinde Content-Type: application/json zorunluluğu (G-2)."""
        ct = self.headers.get("Content-Type", "")
        return ct.startswith("application/json")

    def _check_api_auth(self) -> bool:
        """API uçları için tüm güvenlik kontrollerini yapar."""
        if not self._verify_host():
            self._json({"error": "yetkisiz erişim"}, 403)
            return False
        if not self._verify_token():
            self._json({"error": "yetkisiz erişim"}, 401)
            return False
        return True

    # ----------------------------------------------------------- yardımcı
    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # API cevapları (hata cevapları dahil) açık Cache-Control taşımıyordu;
        # tarayıcı bunu (ör. bir 404'ü) örtük olarak önbelleğe alıp sonraki
        # aynı istekleri ağa hiç göndermeyebiliyordu — kod düzelse bile eski
        # cevap görünmeye devam ediyordu. Statik dosyalarla aynı duruş.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self._add_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _add_security_headers(self) -> None:
        """Tüm yanıtlara güvenlik başlıkları ekle (G-9 CSP)."""
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self'; style-src 'self'")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        if n > MAX_BODY_SIZE:
            self._json({"error": "istek çok büyük"}, 413)
            return {}  # caller boş body görür
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    # ------------------------------------------------------------------ GET
    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            if not self._check_api_auth():
                return
            return self._api_get(path)
        return self._static(path)

    def _api_get(self, path: str):
        if path == "/api/status":
            return self._json(self._status_payload())
        if path == "/api/interfaces":
            return self._json({"interfaces": [vars(i) for i in
                                              system.list_host_interfaces()]})
        if path == "/api/vpns":
            _ensure_vpns_dir()
            vpns = [f[:-5] for f in os.listdir(VPNS_DIR) if f.endswith(".ovpn")]
            return self._json({"vpns": vpns})
        if path == "/api/apps/desktop":
            return self._json({"apps": apps.list_desktop_apps()})
        if path == "/api/wifi/saved":
            return self._json({"networks": sorted(self.settings.wifi_networks.keys())})
        if path.startswith("/api/wifi/secret"):
            # Kayıtlı parolayı YALNIZCA açık istek üzerine döndürür (ağ ayarları
            # penceresindeki "Parolayı göster"). Tarama/durum yüklerinde parola
            # asla gönderilmez; bu yüzden ayrı bir uç tutuluyor.
            from urllib.parse import parse_qs
            qs = parse_qs(urlparse(self.path).query)
            ssid = (qs.get("ssid", [""])[0] or "").strip()
            if not ssid:
                return self._json({"error": "ssid gerekli"}, 400)
            if ssid not in self.settings.wifi_networks:
                return self._json({"error": "kayıtlı ağ değil"}, 404)
            return self._json({"password": self.settings.wifi_networks[ssid]})
        if path.startswith("/api/wifi/scan"):
            return self._api_wifi_scan()
        if path == "/api/usage":
            data = usage.load()
            rx_t, tx_t = usage.today_bytes(data)
            rx_m, tx_m = usage.month_bytes(data)
            return self._json({"today": {"rx": rx_t, "tx": tx_t},
                               "month": {"rx": rx_m, "tx": tx_m}})
        return self._json({"error": "bulunamadı"}, 404)

    def _api_wifi_scan(self):
        # ÖNEMLİ: do_GET, self.path'i ayrıştırıp sorgu dizesini (?iface=...)
        # ATMIŞ path'i verir; iface'i almak için ham self.path kullanılmalı.
        from urllib.parse import parse_qs
        qs = parse_qs(urlparse(self.path).query)
        iface = (qs.get("iface", [""])[0] or "").strip()
        if not iface:
            return self._json({"error": "iface gerekli"}, 400)
        m, s = self.manager, self.settings
        ns = m.state.namespace if system.interface_in_namespace(s.namespace, iface) else None
        found, err = system.wifi_scan(iface, in_namespace=ns, dry_run=m.dry)
        current = system.wifi_current_ssid(iface, in_namespace=ns, dry_run=m.dry)
        saved = s.wifi_networks
        networks = [
            {"ssid": n.ssid, "signal": n.signal, "security": n.security,
             "saved": n.ssid in saved}
            for n in found
        ]
        # Menzil dışına çıkmış ama daha önce kayıtlı olan ağları da (bulunamasa
        # bile) listenin altına ekle — Ubuntu'nun "kayıtlı ağlar" davranışı.
        seen = {n["ssid"] for n in networks}
        for ssid in saved:
            if ssid not in seen:
                networks.append({"ssid": ssid, "signal": None, "security": "wpa",
                                  "saved": True})
        return self._json({"networks": networks, "current": current,
                           "error": err if not networks else ""})

    def _status_payload(self) -> dict:
        s = self.settings
        _ensure_vpns_dir()
        all_vpns = [f[:-5] for f in os.listdir(VPNS_DIR) if f.endswith(".ovpn")]
        return {
            "version": __version__,
            "state": self.manager.snapshot(),
            "interfaces": [vars(i) for i in system.list_host_interfaces()],
            "installed_apps": apps.discover_installed(),
            "custom_apps": s.custom_apps,
            "vpns": all_vpns,
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
        # Gelişmiş path-traversal koruması (B-5)
        real = os.path.realpath(full)
        webui_real = os.path.realpath(WEBUI_DIR)
        if os.path.commonpath([real, webui_real]) != webui_real or not os.path.isfile(real):
            self.send_error(404)
            return
        ext = os.path.splitext(real)[1]
        with open(real, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", _CTYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        # Statik dosyalar (özellikle app.js) sık güncellenir; tarayıcı önbelleği
        # eski mantığı kalıcı çalıştırıp kafa karıştırıcı/kısmi hatalara (ör. yeni
        # eklenen bir düzeltmenin sessizce uygulanmaması) yol açabilir. Panel yerel
        # ve düşük trafikli olduğundan önbellekten kazanç önemsizdir.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self._add_security_headers()
        self.end_headers()
        self.wfile.write(data)

    # ----------------------------------------------------------------- POST
    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        if not self._check_api_auth():
            return
        # Content-Type kontrolü (G-2)
        if not self._verify_content_type():
            self._json({"error": "geçersiz Content-Type; application/json gerekli"}, 415)
            return
        body = self._body()
        try:
            return self._api_post(path, body)
        except (EngineError, apps.LaunchError, apps.ImportProfileError) as e:
            # Bunlar kasıtlı yazılmış, kullanıcıya gösterilmesi güvenli mesajlar
            # taşır (dahili yol/komut/stderr sızdırmaz) — G-4'ün amacı bunları
            # değil, beklenmeyen/ayrıntılı hataları gizlemekti.
            log.info("API isteği reddedildi: %s", e)
            self._json({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            log.exception("API hatası")
            # Bilgi sızıntısını önle (G-4): kullanıcıya genel mesaj, ayrıntı logda
            self._json({"error": "işlem başarısız", "code": "internal_error"}, 400)

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
            elif prof.wifi_ssid and prof.wifi_ssid in s.wifi_networks:
                # bu SSID başka bir profilde/daha önce kaydedilmişse parolayı devral
                prof.wifi_password = s.wifi_networks[prof.wifi_ssid]
            if prof.wifi_ssid and prof.wifi_password:
                s.remember_wifi(prof.wifi_ssid, prof.wifi_password)
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
                # Ek hedefler artık /api/relay/targets/* ile kalıcı olarak
                # profile.relay.extra_targets içinde tutuluyor; burada elle
                # geçirmeye gerek yok, mevcut liste kullanılır.
                m.enable_relay()
            else:
                m.disable_relay()
            return self._json({"ok": True, "state": m.snapshot()})

        if path == "/api/relay/targets/add":
            pname = body.get("profile") or s.active_profile
            prof = s.profile(pname)
            raw = body.get("value") or ""
            values = [v.strip() for v in str(raw).replace(";", ",").split(",")]
            values = [v for v in values if v]
            if not values:
                return self._json({"error": "value gerekli"}, 400)
            for v in values:
                if v not in prof.relay.extra_targets:
                    prof.relay.extra_targets.append(v)
            s.save()
            if prof is m.active_profile:
                m.reassert_relay_routes()
            return self._json({"ok": True, "state": m.snapshot()})

        if path == "/api/relay/targets/delete":
            pname = body.get("profile") or s.active_profile
            prof = s.profile(pname)
            value = (body.get("value") or "").strip()
            if not value:
                return self._json({"error": "value gerekli"}, 400)
            prof.relay.extra_targets = [v for v in prof.relay.extra_targets if v != value]
            s.save()
            if prof is m.active_profile:
                m.remove_relay_target(value)
            return self._json({"ok": True, "state": m.snapshot()})

        if path == "/api/data-saver":
            pname = body.get("profile") or s.active_profile
            prof = s.profile(pname)
            if "enabled" in body:
                prof.data_saver.enabled = bool(body["enabled"])
            if body.get("level") in ("light", "balanced", "strict"):
                prof.data_saver.level = body["level"]
            if "frugal_probes" in body:
                prof.data_saver.frugal_probes = bool(body["frugal_probes"])
            if "quota_mb" in body:
                try:
                    prof.data_saver.quota_mb = max(0, int(body["quota_mb"]))
                except (TypeError, ValueError):
                    return self._json({"error": "quota_mb sayı olmalı"}, 400)
            if body.get("quota_action") in ("warn", "killswitch"):
                prof.data_saver.quota_action = body["quota_action"]
            if "cap_down_kbit" in body:
                try:
                    prof.data_saver.cap_down_kbit = max(0, int(body["cap_down_kbit"]))
                except (TypeError, ValueError):
                    return self._json({"error": "cap_down_kbit sayı olmalı"}, 400)
            if "cap_up_kbit" in body:
                try:
                    prof.data_saver.cap_up_kbit = max(0, int(body["cap_up_kbit"]))
                except (TypeError, ValueError):
                    return self._json({"error": "cap_up_kbit sayı olmalı"}, 400)
            if body.get("media_level") in ("off", "144p", "360p", "720p", "blocked"):
                # "blocked" tarayıcı bayrağı/user.js ile gelir → yalnızca YENİ açılan
                # uygulamada etkili. "360p"/"720p" ise bant genişliği tavanı olduğundan
                # aşağıdaki reassert_shaping ile canlı oturuma ANINDA uygulanır.
                prof.data_saver.media_level = body["media_level"]
            elif "block_media" in body:
                # Geriye dönük uyumluluk (eski panel/istemci).
                prof.data_saver.media_level = "blocked" if body["block_media"] else "off"
            s.save()
            if prof is m.active_profile:
                m.reassert_shaping()
            return self._json({"ok": True, "state": m.snapshot()})

        if path == "/api/apps/restart-all":
            started = m.restart_apps()
            return self._json({"ok": True, "started": started, "state": m.snapshot()})

        if path == "/api/wifi/save":
            # Ubuntu'daki ağ ayarları penceresinin "Uygula"sı: kayıtlı bir ağın
            # parolasını değiştirir ve/veya SSID'sini yeniden adlandırır.
            ssid = (body.get("ssid") or "").strip()
            old = (body.get("old_ssid") or "").strip()
            password = body.get("password") or ""
            if not ssid:
                return self._json({"error": "ssid gerekli"}, 400)
            # Parola boş bırakıldıysa mevcut kayıtlı parola korunur (yeniden
            # adlandırmada eski kayıttan devralınır).
            if not password:
                password = s.wifi_networks.get(old or ssid, "")
            if not password:
                return self._json({"error": "parola gerekli"}, 400)
            if len(password) < 8:
                return self._json({"error": "WPA parolası en az 8 karakter olmalı"}, 400)
            if old and old != ssid:
                s.wifi_networks.pop(old, None)
            s.wifi_networks[ssid] = password
            # Bu ağı kullanan profilleri de senkron tut; aksi halde profil eski
            # SSID/parolayla bağlanmayı denemeye devam ederdi.
            for prof in s.profiles.values():
                if prof.wifi_ssid == (old or ssid):
                    prof.wifi_ssid = ssid
                    prof.wifi_password = password
            s.save()
            return self._json({"ok": True})

        if path == "/api/wifi/forget":
            # Ubuntu'daki "Bu ağı unut": kayıtlı parolayı siler; ağ taramada
            # görünmeye devam eder ama bir daha otomatik bağlanılmaz.
            ssid = (body.get("ssid") or "").strip()
            if not ssid:
                return self._json({"error": "ssid gerekli"}, 400)
            s.wifi_networks.pop(ssid, None)
            # Bu SSID'yi kullanan profillerdeki kayıtlı parolayı da temizle;
            # aksi halde "unutuldu" denen ağa profil üzerinden bağlanmaya devam
            # edilebilirdi.
            for prof in s.profiles.values():
                if prof.wifi_ssid == ssid:
                    prof.wifi_password = ""
            s.save()
            return self._json({"ok": True})

        if path == "/api/vpn":
            name = body.get("name")
            if not name:
                return self._json({"error": "VPN adı (name) belirtilmedi"}, 400)
            if body.get("enabled"):
                m.connect_vpn(name)
            else:
                m.disconnect_vpn(name)
            return self._json({"ok": True, "state": m.snapshot()})

        if path == "/api/vpns/upload":
            name = body.get("name")
            content = body.get("content")
            if not name or not content:
                return self._json({"error": "name ve content gerekli"}, 400)
            import re
            name = re.sub(r"[^a-zA-Z0-9_-]", "", name)
            if not name:
                return self._json({"error": "geçersiz VPN adı"}, 400)
            _ensure_vpns_dir()
            vpath = os.path.join(VPNS_DIR, f"{name}.ovpn")
            with open(vpath, "w") as f:
                f.write(content)
            # .ovpn dosyaları sertifika/parola içerebilir; token/config.json ile
            # aynı G-3 duruşu: 0600 + gerçek kullanıcıya chown (root varsayılan
            # umask'ıyla oluşturulursa diğer yerel kullanıcılar okuyabilirdi).
            os.chmod(vpath, 0o600)
            _chown_to_real_user(VPNS_DIR, vpath)
            return self._json({"ok": True, "state": m.snapshot()})

        if path == "/api/vpns/delete":
            name = body.get("name")
            if not name:
                return self._json({"error": "name gerekli"}, 400)
            import re
            name = re.sub(r"[^a-zA-Z0-9_-]", "", name)
            vpath = os.path.join(VPNS_DIR, f"{name}.ovpn")
            if os.path.exists(vpath):
                os.remove(vpath)
            try:
                m.disconnect_vpn(name)
            except Exception:
                pass
            return self._json({"ok": True, "state": m.snapshot()})

        if path == "/api/apps/custom/add":
            name = (body.get("name") or "").strip()
            command = (body.get("command") or "").strip()
            if not command:
                return self._json({"error": "command gerekli"}, 400)
            if not any(c["command"] == command for c in s.custom_apps):
                s.custom_apps.append({"name": name or command, "command": command})
                s.save()
            return self._json({"ok": True, "state": m.snapshot()})

        if path == "/api/apps/custom/delete":
            command = (body.get("command") or "").strip()
            if not command:
                return self._json({"error": "command gerekli"}, 400)
            s.custom_apps = [c for c in s.custom_apps if c["command"] != command]
            s.save()
            return self._json({"ok": True, "state": m.snapshot()})

        if path == "/api/launch":
            prog = body.get("app")
            if not prog:
                return self._json({"error": "uygulama belirtilmedi"}, 400)
            pid = m.launch_app(prog)
            return self._json({"ok": True, "pid": pid, "state": m.snapshot()})

        if path == "/api/import-profile":
            prog = body.get("app")
            if not prog:
                return self._json({"error": "uygulama belirtilmedi"}, 400)
            dest = m.import_profile(prog)
            return self._json({"ok": True, "dest": dest, "state": m.snapshot()})

        if path == "/api/speedtest":
            ns = m.state.namespace
            if not ns:
                return self._json({"error": "Oturum aktif değil"}, 400)
            # Veri tasarrufu açıksa hız testinin kendi indirmesini küçült
            # (varsayılan 10 MB, seviyeye göre 5/2/1 MB'a düşer).
            test_bytes = 10_000_000
            prof = m.active_profile
            if prof and prof.data_saver.enabled:
                test_bytes = {"light": 5_000_000, "balanced": 2_000_000,
                             "strict": 1_000_000}.get(prof.data_saver.level, 2_000_000)
            url = f"https://speed.cloudflare.com/__down?bytes={test_bytes}"
            cmd = ["ip", "netns", "exec", ns, "curl", "-s", "-w", "%{speed_download}", "-o", "/dev/null", url]
            try:
                import subprocess
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
                if res.returncode == 0:
                    speed_bps = float(res.stdout.strip())
                    mbps = (speed_bps * 8) / 1_000_000
                    return self._json({"ok": True, "mbps": round(mbps, 1)})
                else:
                    return self._json({"error": "Bağlantı zayıf veya başarısız"}, 400)
            except subprocess.TimeoutExpired:
                return self._json({"error": "Zaman aşımı (bağlantı çok yavaş)"}, 400)
            except Exception:
                return self._json({"error": "Test başarısız"}, 400)

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

        if path == "/api/restart":
            # Yeniden başlat = TAM SIFIRLAMA. İzole oturum (namespace, uplink,
            # uygulamalar, relay, VPN) tamamen yıkılır, arka plan servisleri
            # (watchdog, DHCP/wpa_supplicant) durdurulur, sonra daemon süreci
            # kendi üzerine yeniden başlatılır (execv) ve TEMİZ bir durumla
            # açılır. Üç eylemin farkı:
            #   Çıkış          → paneli kapat, oturum arka planda YAŞAR
            #   Durdur         → oturumu yık, daemon çalışmaya devam eder
            #   Yeniden Başlat → oturumu yık VE daemon'ı tazele (sıfırdan başla)
            #
            # execv ayrıca diskteki GÜNCEL kodu yükler: Python modülleri süreç
            # başlarken import edilir, dolayısıyla kaynak dosyalar değiştiğinde
            # yeni mantığın etkin olması için bu yol (ya da tam yeniden başlatma)
            # gerekir — panel HTML/JS'i her istekte diskten okunduğu için tek
            # başına güncellenmiş görünür ama Python tarafı eski kalabilir.
            try:
                m.stop_session()
            except Exception:  # noqa: BLE001
                log.exception("yeniden başlatma öncesi oturum durdurulamadı")
            # Kalıntı/kopya süreçleri de temizle: eski bir kod sürümünden kalmış
            # ikinci bir daemon örneği (portu tutup yenisinin başlamasını
            # engelleyebilir) ve sahipsiz kalmış WiFi yardımcı süreçleri
            # (wpa_supplicant/dhcpcd — namespace silinse de süreç kendiliğinden
            # ölmeyebilir). Kendi PID'imiz her zaman hariç tutulur.
            try:
                my_pid = os.getpid()
                # Kendi ata zincirimizi koru: daemon `pkexec env ... python3 -m
                # tether_isolator gui` ile başlatılır ve bu sarmalayıcının komut
                # satırı da desene UYAR. Onu öldürmek çocuk daemon'ı da düşürür →
                # süreç execv'e ulaşamaz, panel bir daha açılmaz.
                protected = system.process_ancestors(my_pid)
                stray_daemons = system.kill_stray_daemons(
                    "tether_isolator gui", exclude_pid=my_pid, exclude_pids=protected)
                if stray_daemons:
                    log.warning("kalıntı daemon süreçleri sonlandırıldı: %s", stray_daemons)
                stray_helpers = system.kill_stray_daemons(
                    f"/etc/netns/{s.namespace}/wpa_", exclude_pid=my_pid,
                    exclude_pids=protected)
                if stray_helpers:
                    log.warning("sahipsiz WiFi yardımcı süreçleri sonlandırıldı: %s",
                                stray_helpers)
            except Exception:  # noqa: BLE001
                log.exception("kalıntı süreç temizliği başarısız")
            self._json({"ok": True, "restarting": True})
            request_restart()
            if self.httpd is not None:
                threading.Thread(target=self.httpd.shutdown, daemon=True).start()
            return None

        return self._json({"error": "bulunamadı"}, 404)


def _pids_listening_on(port: int) -> list[int]:
    """Verilen TCP portunda DİNLEYEN süreçlerin PID'lerini döndürür (kendi netns'imiz).

    /proc/net/tcp[6] üzerinden LISTEN soketinin inode'unu bulur, sonra /proc/*/fd
    içinde o inode'a sahip süreci eşler. Kalıntı (takılı) daemon'ı kesin olarak
    hedeflemek için kullanılır — komut-satırı desenine göre pkill'in aksine kendini
    ya da sarmalayıcıyı yanlışlıkla öldürmez.
    """
    inodes: set[str] = set()
    for proto in ("tcp", "tcp6"):
        try:
            with open(f"/proc/net/{proto}") as f:
                for line in f.read().splitlines()[1:]:
                    parts = line.split()
                    if len(parts) < 10 or parts[3] != "0A":  # 0A = LISTEN
                        continue
                    try:
                        p = int(parts[1].split(":")[1], 16)
                    except (IndexError, ValueError):
                        continue
                    if p == port:
                        inodes.add(parts[9])
        except OSError:
            continue
    if not inodes:
        return []
    pids: list[int] = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            fd_dir = f"/proc/{pid}/fd"
            for fd in os.listdir(fd_dir):
                try:
                    link = os.readlink(os.path.join(fd_dir, fd))
                except OSError:
                    continue
                if link.startswith("socket:[") and link[8:-1] in inodes:
                    pids.append(int(pid))
                    break
        except OSError:
            continue
    return pids


def _port_responsive(host: str, port: int) -> bool:
    """Portta GERÇEKTEN yanıt veren bir HTTP sunucusu var mı?

    Yalnızca TCP bağlanmak yeterli değil: bağlanmış ama serve_forever'da takılmış
    bir kalıntı da TCP el sıkışmasını kabul eder. Bu yüzden minimal bir HTTP GET
    gönderip gerçek bir HTTP yanıtı bekleriz; yanıt yoksa 'takılı kalıntı' sayılır.
    """
    try:
        with socket.create_connection((host, port), timeout=1.5) as s:
            s.settimeout(2.0)
            s.sendall(f"GET / HTTP/1.0\r\nHost: {host}\r\n\r\n".encode())
            return s.recv(16).startswith(b"HTTP")
    except OSError:
        return False


def _make_server(settings: Settings) -> "_Server":
    """Sunucuyu bağlar; port takılı bir kalıntı tarafından tutuluyorsa onu kurtarır.

    Port doluysa (EADDRINUSE): önce yanıt veriyor mu diye bakılır. Yanıt veriyorsa
    zaten sağlıklı bir örnek çalışıyordur (normalde başlatıcı bunu yakalar) → hata
    olduğu gibi yükseltilir. Yanıt VERMİYORSA bu, bağlanmadan/serve etmeden takılmış
    eski bir daemon kalıntısıdır; onu (yalnızca portu tutan süreci) sonlandırıp
    yeniden bağlanmayı deneriz. Böylece 'Address already in use' yüzünden panelin
    hiç açılmaması (kalıntı biriktikçe her seferinde tekrarlayan hata) giderilir.
    """
    host, port = settings.http_host, settings.http_port
    try:
        return _Server((host, port), _Handler)
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise
        if _port_responsive(host, port):
            raise  # gerçekten sağlıklı bir örnek var; dokunma
        holders = [p for p in _pids_listening_on(port) if p != os.getpid()]
        if not holders:
            raise
        log.warning("port %d takılı bir kalıntı tarafından tutuluyor (PID %s); "
                    "sonlandırılıp yeniden bağlanılıyor", port,
                    ", ".join(map(str, holders)))
        for p in holders:
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass
        time.sleep(1.0)
        return _Server((host, port), _Handler)


def serve(settings: Settings, *, dry_run: bool = False, open_browser: bool = True):
    manager = Manager(settings, dry_run=dry_run)
    _Handler.manager = manager
    _Handler.settings = settings
    # API token'ı hazırla (G-1)
    _Handler._api_token = _ensure_token()
    # Soketi ÖNCE bağla ki panel anında açılsın. Mevcut oturum devralma
    # (adopt_if_running) senkron ağ probları (ping/curl) yapabildiğinden, eski/bayat
    # bir oturum varsa bu ~10-18 sn sürebilir; bunu ARKA PLANA alıyoruz. Aksi halde
    # panel bu süre boyunca hiç açılmaz ("bir süre sonra başlıyor" belirtisi).
    httpd = _make_server(settings)
    _Handler.httpd = httpd

    def _adopt():
        try:
            if manager.adopt_if_running():
                log.info("Mevcut izole oturum devralındı — kaldığın yerden devam.")
        except Exception as e:  # noqa: BLE001
            log.warning("oturum devralınırken hata: %s", e)
    threading.Thread(target=_adopt, name="tether-adopt", daemon=True).start()

    url = f"http://{settings.http_host}:{settings.http_port}/"
    log.info("Tether Isolator paneli: %s%s", url, "  [DRY-RUN]" if dry_run else "")
    if open_browser:
        url_with_token = f"{url}#token={_Handler._api_token}"
        threading.Timer(0.8, lambda: webbrowser.open(url_with_token)).start()
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

    if _restart_requested:
        # Süreci kendi üzerine yeniden başlat (execv): root ayrıcalığı (pkexec
        # ile alınmıştı) korunur, yeni bir polkit parolası istenmez; modüller
        # ve tüm arka plan thread'leri sıfırdan başlar.
        #
        # ÖNEMLİ: -m tether_isolator olarak yeniden başlatılmalı (sys.argv[0]'ı
        # doğrudan execv'e vermek relative import'ları kırar — __main__.py paket
        # dışı bir script gibi çalışır). Ayrıca --no-browser bayrağı YİNELENMEZ:
        # kullanıcı restart'ı panelden tetiklediği için tarayıcının otomatik
        # yeniden açılması istenir.
        log.info("yeniden başlatılıyor...")
        extra = [a for a in sys.argv[1:] if a != "--no-browser"]
        os.execv(sys.executable, [sys.executable, "-m", "tether_isolator", *extra])


def _profile_view(p: Profile) -> dict:
    """Web'e gönderilecek güvenli profil görünümü (parola gizlenir)."""
    from dataclasses import asdict
    d = asdict(p)
    if d.get("wifi_password"):
        d["wifi_password"] = "••••••"
    return d
