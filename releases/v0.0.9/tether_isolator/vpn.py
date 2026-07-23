"""İzole namespace İÇİNDE OpenVPN istemcisi.

VPN, izole alanın kendi uplink'ini (tether/WiFi) taşıyıcı olarak kullanır ve
namespace içinde bir tun arabirimi açar. Böylece izole uygulamalar VPN üzerinden
uzak ağa (ör. garageliman) erişir; `redirect-gateway` yoksa internet (varsayılan
rota) izole alanın kendi uplink'inde kalır (split-tunnel).

Bu, relay'in ALTERNATİFİ değil TAMAMLAYICISIDIR: relay host'un ethernet ağına
köprü kurar, VPN ise izole alanın kendi uplink'i üzerinden şifreli bir uzak ağ
bağlantısı açar. İkisi aynı anda açık olabilir (farklı arayüz/rota).
"""
from __future__ import annotations

import logging
import os
import time

from . import system
from .config import Settings
from .engine import EngineError
from .system import run

log = logging.getLogger("tether.vpn")

RUN_DIR = "/run/tether-isolator"
LOG_PATH = os.path.join(RUN_DIR, "openvpn.log")
PID_PATH = os.path.join(RUN_DIR, "openvpn.pid")

# Modern GCM/CHACHA + eski AES-128-CBC/AES-256-CBC uyumu. Sunucu ne desteklerse
# en güçlü ortak şifre seçilir; eski sunucular için CBC geriye dönük destek.
DATA_CIPHERS = "AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305:AES-256-CBC:AES-128-CBC"


class VPN:
    def __init__(self, settings: Settings, *, dry_run: bool = False):
        self.s = settings
        self.ns = settings.namespace
        self.dry = dry_run

    def _ns(self, *a: str, check: bool = True, timeout: int = 30):
        return run(["ip", "netns", "exec", self.ns, *a],
                   check=check, dry_run=self.dry, timeout=timeout)

    def _dco_supported(self) -> bool:
        """openvpn '--disable-dco' bayrağını tanıyor mu? (2.6+ evet)."""
        if self.dry:
            return True
        res = run(["openvpn", "--help"], check=False, dry_run=self.dry, timeout=10)
        return res.ok and "disable-dco" in res.out

    # ------------------------------------------------------------------ bağlan
    def connect(self, config_path: str) -> None:
        """OpenVPN'i namespace içinde (arka planda) başlatır; bağlanana kadar bekler."""
        if not system.have("openvpn"):
            raise EngineError("OpenVPN kurulu değil: sudo apt install openvpn")
        if not self.dry:
            if not os.path.isfile(config_path):
                raise EngineError(f"VPN yapılandırması bulunamadı: {config_path}")
            os.makedirs(RUN_DIR, exist_ok=True)
            # Eski örnek kaldıysa temizle.
            self.disconnect()
            for p in (LOG_PATH, PID_PATH):
                try:
                    os.remove(p)
                except OSError:
                    pass

        cfg_dir = os.path.dirname(os.path.abspath(config_path)) or "/"
        # --cd: .ovpn'deki göreli yollar çözülsün. --daemon + --writepid:
        # arka planda kalsın ama pid'ini bilelim. --log: tanı için.
        # --disable-dco: OpenVPN 2.6 DCO (Data Channel Offload) kernel modülü
        # ağ namespace'i içinde takılıyor (namespace-farkında değil) → userspace
        # tun yoluna zorla. netns içinde openvpn'in çalışması için gerekli.
        cmd = [
            "openvpn",
            "--config", config_path,
            "--cd", cfg_dir,
            "--log", LOG_PATH,
            "--writepid", PID_PATH,
            "--daemon", "tisor-vpn",
            # Eski sunucular (ör. garageliman) AES-128-CBC dayatıyor; OpenVPN 2.6
            # varsayılanı sadece GCM kabul edip tüneli reddediyor. Legacy CBC'yi
            # de listeye ekle — sunucu modern desteklerse yine GCM seçilir.
            "--data-ciphers", DATA_CIPHERS,
        ]
        if self._dco_supported():
            cmd.append("--disable-dco")
        self._ns(*cmd, check=False, timeout=20)
        if self.dry:
            log.info("[dry] VPN başlatılırdı: %s", config_path)
            return

        if not self.wait_connected():
            tail = self._log_tail()
            self.disconnect()
            raise EngineError(
                "VPN bağlantısı kurulamadı (zaman aşımı). Olası nedenler: uzak "
                "sunucuya izole alanın uplink'inden erişilemiyor, sertifika/parola "
                "hatası, ya da UDP engelli.\nOpenVPN log (son satırlar):\n" + tail)

    def wait_connected(self, timeout: int = 30) -> bool:
        """OpenVPN 'Initialization Sequence Completed' yazana kadar bekler."""
        if self.dry:
            return True
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with open(LOG_PATH, errors="replace") as f:
                    data = f.read()
            except OSError:
                data = ""
            if "Initialization Sequence Completed" in data:
                return True
            # Erken/ölümcül hatalarda beklemeyi kısa kes.
            low = data.lower()
            if any(k in low for k in ("auth_failed", "cannot resolve",
                                      "connection refused", "tls handshake failed",
                                      "exiting due to fatal error")):
                if not self._pid_alive():
                    return False
            if not self._pid_alive() and data:
                return "Initialization Sequence Completed" in data
            time.sleep(1)
        return False

    # ------------------------------------------------------------------ kapat
    def disconnect(self) -> None:
        """Namespace içindeki OpenVPN sürecini durdurur."""
        if self.dry:
            log.info("[dry] VPN durdurulurdu")
            return
        pid = self._read_pid()
        if pid and self._pid_alive(pid):
            try:
                os.kill(pid, 15)  # SIGTERM — openvpn tun'u temiz kapatır
            except OSError:
                pass
        else:
            # pid dosyası yoksa isimle temizle (namespace içinde).
            self._ns("pkill", "-f", "openvpn.*tisor-vpn", check=False)
        try:
            os.remove(PID_PATH)
        except OSError:
            pass

    # ------------------------------------------------------------------ durum
    def is_active(self) -> bool:
        """VPN süreci canlı ve namespace içinde bir tun arabirimi var mı?"""
        if self.dry:
            return False
        if not self._pid_alive():
            return False
        return bool(self.tun_iface())

    def tun_iface(self) -> str:
        """Namespace içindeki tun arabiriminin adını döndürür (yoksa '')."""
        if self.dry:
            return "tun0"
        res = self._ns("ip", "-o", "link", "show", check=False)
        if not res.ok:
            return ""
        for line in res.out.splitlines():
            # "3: tun0: <...>" → arayüz adını al
            parts = line.split(":")
            if len(parts) >= 2:
                name = parts[1].strip().split("@")[0]
                if name.startswith("tun") or name.startswith("tap"):
                    return name
        return ""

    def status(self) -> dict:
        """UI için özet: aktif mi, arayüz, atanan IP."""
        if self.dry or not self.is_active():
            return {"active": False, "iface": "", "ip": ""}
        iface = self.tun_iface()
        ip = ""
        res = self._ns("ip", "-o", "-4", "addr", "show", iface, check=False)
        if res.ok:
            for tok in res.out.split():
                if "/" in tok and tok.count(".") == 3:
                    ip = tok
                    break
        return {"active": True, "iface": iface, "ip": ip}

    # ------------------------------------------------------------------ iç
    def _read_pid(self) -> int:
        try:
            with open(PID_PATH) as f:
                return int(f.read().strip() or "0")
        except (OSError, ValueError):
            return 0

    def _pid_alive(self, pid: int | None = None) -> bool:
        pid = pid if pid is not None else self._read_pid()
        return pid > 0 and os.path.isdir(f"/proc/{pid}")

    def _log_tail(self, n: int = 15) -> str:
        try:
            with open(LOG_PATH, errors="replace") as f:
                return "\n".join(f.read().splitlines()[-n:])
        except OSError:
            return "(log okunamadı)"
