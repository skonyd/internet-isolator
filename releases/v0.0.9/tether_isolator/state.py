"""Çalışma anı (runtime) durumu.

Daemon bu durumu bellekte tutar ve web arayüzü/CLI sorgular. Ayrıca disk
üzerinde bir kopya tutulur ki daemon olmadan da CLI son durumu okuyabilsin.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

STATE_DIR = "/run/tether-isolator"
STATE_FILE = os.path.join(STATE_DIR, "state.json")
# /run'a yazamıyorsak (root değilsek) kullanıcı dizinine düş
_FALLBACK = os.path.join(
    os.environ.get("XDG_RUNTIME_DIR", os.path.expanduser("~/.cache")),
    "tether-isolator-state.json",
)


@dataclass
class AppProcess:
    command: str
    pid: int
    running: bool = True
    started_at: float = field(default_factory=time.time)


@dataclass
class RuntimeState:
    # Genel
    phase: str = "idle"            # idle/starting/online/degraded/reconnecting/stopping
    profile: str = ""
    namespace: str = ""
    uplink: str = ""
    uplink_present: bool = False   # arayüz namespace içinde mevcut mu
    ip_address: str = ""
    gateway: str = ""
    online: bool = False           # internet erişimi var mı (ping)
    public_ip: str = ""            # izole alanın dış IP'si (host'tan farklı olmalı)
    last_check: float = 0.0
    # Dayanıklılık
    reconnect_count: int = 0
    last_reconnect: float = 0.0
    # Relay
    relay_active: bool = False
    relay_scope: str = ""
    relay_targets: list[str] = field(default_factory=list)  # scope=lan: etkin hedefler
    # VPN (izole alan içinde OpenVPN)
    vpn_active: bool = False
    vpn_name: str = ""       # kullanılan .ovpn dosya adı
    vpn_iface: str = ""      # ns içindeki tun arabirimi
    vpn_ip: str = ""         # VPN'in atadığı IP
    # Uygulamalar
    apps: list[AppProcess] = field(default_factory=list)
    # Olay günlüğü (son N olay) — UI'de canlı akış için
    events: list[dict] = field(default_factory=list)

    def log_event(self, level: str, message: str) -> None:
        self.events.append({"t": time.time(), "level": level, "msg": message})
        self.events = self.events[-100:]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["uptime_apps"] = len([a for a in self.apps if a.running])
        return d

    def persist(self) -> None:
        path, data = _writable_path(), json.dumps(self.to_dict())
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(data)
        except OSError:
            pass

    @classmethod
    def read(cls) -> Optional[dict]:
        for path in (STATE_FILE, _FALLBACK):
            try:
                with open(path) as f:
                    return json.load(f)
            except OSError:
                continue
        return None


def _writable_path() -> str:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        if os.access(STATE_DIR, os.W_OK):
            return STATE_FILE
    except OSError:
        pass
    return _FALLBACK
