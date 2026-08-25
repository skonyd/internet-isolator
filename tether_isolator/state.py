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
    # Bu uygulama başlatılırken yürürlükte olan medya kademesi. Tarayıcı
    # bayrakları/user.js yalnızca BAŞLATMA anında uygulanabildiğinden, kullanıcı
    # sürgüyü sonradan değiştirdiğinde çalışan örnek ESKİ ayarda kalır. Panel bu
    # alanı profildeki güncel kademeyle karşılaştırıp "yeniden başlat" uyarısı
    # gösterir — aksi halde kullanıcı "kapalı dedim ama video hâlâ oynuyor" der.
    media_level: str = ""


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
    relay_targets: list[str] = field(default_factory=list)
    # VPN
    vpn_active: bool = False
    vpn_name: str = ""
    vpn_iface: str = ""
    vpn_ip: str = ""
    vpns: list[dict] = field(default_factory=list)
    # Uygulamalar
    apps: list[AppProcess] = field(default_factory=list)
    # Olay günlüğü (son N olay)
    events: list[dict] = field(default_factory=list)
    # Trafik sayacı (H-1)
    traffic_rx: int = 0            # oturum boyunca indirilen bayt
    traffic_tx: int = 0            # oturum boyunca yüklenen bayt
    traffic_rate_rx: float = 0.0   # anlık indirme hızı (B/s)
    traffic_rate_tx: float = 0.0   # anlık yükleme hızı (B/s)
    session_started_at: float = 0.0
    # Kalıcı kullanım (veri tasarrufu U-6/kota) — usage.json'dan
    usage_today_rx: int = 0
    usage_today_tx: int = 0
    usage_month_rx: int = 0
    usage_month_tx: int = 0
    quota_warned: bool = False
    quota_hit: bool = False
    # Bant genişliği tavanı (Faz 4)
    shaping_active: bool = False
    shaping_down_kbit: int = 0
    shaping_up_kbit: int = 0
    shaping_method: str = ""       # cake | tbf | police | ""
    # Uçtan uca sağlık göstergesi (H-7)
    health_gateway: bool = False   # gateway'e ping
    health_dns: bool = False       # DNS çözümü
    health_external: bool = False  # dış IP erişilebilir
    health_vpn: bool = False       # VPN aktif
    health_relay: bool = False     # relay hedefleri erişilebilir

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
