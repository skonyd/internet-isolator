"""İzole namespace İÇİNDE OpenVPN istemcisi (Çoklu VPN Desteği)."""
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
DATA_CIPHERS = "AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305:AES-256-CBC:AES-128-CBC"

def _safe_name(name: str) -> str:
    """VPN ismi için güvenli, alfasayısal bir versiyon döndürür."""
    import re
    return re.sub(r"[^a-zA-Z0-9_-]", "", name)

def get_log_path(name: str) -> str:
    return os.path.join(RUN_DIR, f"openvpn-{_safe_name(name)}.log")

def get_pid_path(name: str) -> str:
    return os.path.join(RUN_DIR, f"openvpn-{_safe_name(name)}.pid")

def get_dev_name(name: str) -> str:
    # Arayüz adları maks 15 karakter. 'vpn-' (4) + name[:11]
    return f"vpn-{_safe_name(name)[:11]}"


class VPN:
    def __init__(self, settings: Settings, *, dry_run: bool = False):
        self.s = settings
        self.ns = settings.namespace
        self.dry = dry_run

    def _ns(self, *a: str, check: bool = True, timeout: int = 30):
        return run(["ip", "netns", "exec", self.ns, *a],
                   check=check, dry_run=self.dry, timeout=timeout)

    def _dco_supported(self) -> bool:
        if self.dry:
            return True
        res = run(["openvpn", "--help"], check=False, dry_run=self.dry, timeout=10)
        return res.ok and "disable-dco" in res.out

    # ------------------------------------------------------------------ bağlan
    def connect(self, name: str, config_path: str) -> None:
        """Belirtilen isimle yeni bir OpenVPN tüneli başlatır."""
        if not system.have("openvpn"):
            raise EngineError("OpenVPN kurulu değil: sudo apt install openvpn")
        if not self.dry:
            if not os.path.isfile(config_path):
                raise EngineError(f"VPN yapılandırması bulunamadı: {config_path}")
            os.makedirs(RUN_DIR, exist_ok=True)
            self.disconnect(name)
            for p in (get_log_path(name), get_pid_path(name)):
                try:
                    os.remove(p)
                except OSError:
                    pass

        cfg_dir = os.path.dirname(os.path.abspath(config_path)) or "/"
        cmd = [
            "openvpn",
            "--config", config_path,
            "--cd", cfg_dir,
            "--log", get_log_path(name),
            "--writepid", get_pid_path(name),
            "--daemon", f"tisor-vpn-{_safe_name(name)}",
            "--data-ciphers", DATA_CIPHERS,
            "--dev-type", "tun",
            "--dev", get_dev_name(name),
            "--script-security", "2",
        ]
        if self._dco_supported():
            cmd.append("--disable-dco")
        self._ns(*cmd, check=False, timeout=20)
        if self.dry:
            log.info("[dry] VPN %s başlatılırdı: %s", name, config_path)
            return

        if not self.wait_connected(name):
            tail = self._log_tail(name)
            self.disconnect(name)
            raise EngineError(
                f"VPN '{name}' bağlantısı kurulamadı. "
                "Olası nedenler: sertifika hatası, ağ engeli.\n"
                "Log özeti:\n" + tail)

    def wait_connected(self, name: str, timeout: int = 30) -> bool:
        if self.dry:
            return True
        deadline = time.time() + timeout
        log_file = get_log_path(name)
        while time.time() < deadline:
            try:
                with open(log_file, errors="replace") as f:
                    data = f.read()
            except OSError:
                data = ""
            if "Initialization Sequence Completed" in data:
                return True
            low = data.lower()
            if any(k in low for k in ("auth_failed", "cannot resolve",
                                      "connection refused", "tls handshake failed",
                                      "exiting due to fatal error")):
                if not self._pid_alive(name):
                    return False
            if not self._pid_alive(name) and data:
                return "Initialization Sequence Completed" in data
            time.sleep(1)
        return False

    # ------------------------------------------------------------------ kapat
    def disconnect(self, name: str) -> None:
        if self.dry:
            return
        pid = self._read_pid(name)
        if pid and self._pid_alive(name, pid):
            try:
                os.kill(pid, 15)
            except OSError:
                pass
        else:
            # (\s|$): "work" isimli tünel "work2" gibi kendisini önek olarak
            # içeren başka bir tünelin sürecini yanlışlıkla öldürmesin.
            self._ns("pkill", "-f", f"openvpn.*tisor-vpn-{_safe_name(name)}(\\s|$)",
                    check=False)
        try:
            os.remove(get_pid_path(name))
        except OSError:
            pass

    # ------------------------------------------------------------------ durum
    def is_active(self, name: str) -> bool:
        if self.dry:
            return False
        if not self._pid_alive(name):
            return False
        return bool(self.tun_iface(name))

    def tun_iface(self, name: str) -> str:
        if self.dry:
            return get_dev_name(name)
        dev = get_dev_name(name)
        res = self._ns("ip", "-o", "link", "show", dev, check=False)
        if res.ok and dev in res.out:
            return dev
        return ""

    def status(self, name: str) -> dict:
        if self.dry or not self.is_active(name):
            return {"active": False, "iface": "", "ip": ""}
        iface = self.tun_iface(name)
        ip = ""
        if iface:
            res = self._ns("ip", "-o", "-4", "addr", "show", iface, check=False)
            if res.ok:
                for tok in res.out.split():
                    if "/" in tok and tok.count(".") == 3:
                        ip = tok
                        break
        return {"active": True, "iface": iface, "ip": ip}

    # ------------------------------------------------------------------ iç
    def _read_pid(self, name: str) -> int:
        try:
            with open(get_pid_path(name)) as f:
                return int(f.read().strip() or "0")
        except (OSError, ValueError):
            return 0

    def _pid_alive(self, name: str, pid: int | None = None) -> bool:
        pid = pid if pid is not None else self._read_pid(name)
        return pid > 0 and os.path.isdir(f"/proc/{pid}")

    def _log_tail(self, name: str, n: int = 15) -> str:
        try:
            with open(get_log_path(name), errors="replace") as f:
                return "\n".join(f.read().splitlines()[-n:])
        except OSError:
            return "(log okunamadı)"
