"""Profil ve ayar yönetimi.

Bir *profil*, izole bir oturumun tüm tanımıdır: hangi uplink, hangi
uygulamalar, DNS, relay politikası vb. Profiller kalıcıdır (silinmez);
böylece kullanıcı "iş", "kişisel" gibi birden çok kayıtlı yapılandırmayı
tekrar tekrar kullanabilir. Uygulama profil dizinleri de kalıcıdır; yani
tarayıcı oturumları/oturum açmaları korunur.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Optional

from . import system

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "tether-isolator",
)
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")


def _data_dir_for(username: str) -> str:
    home = system.user_home(username)
    return os.path.join(home, ".local", "share", "tether-isolator")


@dataclass
class RelayPolicy:
    """Relay davranışı.

    Relay açıldığında izole alan, bu makinenin ULAŞTIĞI LAN ağlarına (varsayılan
    rota HARİÇ) erişir; internet izole alanın kendi uplink'inde kalır. Tek mod,
    kapsam/allowlist yok. Aşağıdaki eski alanlar yalnızca geriye dönük profil
    uyumluluğu için tutulur (artık kullanılmaz).
    """
    enabled_by_default: bool = False
    host_subnet: str = "10.77.0.0/30"   # veth yan-kanalı için özel /30
    # Relay açıkken host üzerinden geçirilecek EK hedefler: IP / CIDR / alan adı.
    # Kullanım: kurum ethernet'inin varsayılan (internet) çıkışından erişilen ama
    # tether'den engelli, LAN rotalarında görünmeyen dış adresler (ör. kurum
    # webmail: mail.havelsan.com.tr). Bunlar izole alanda host'a yönlendirilir;
    # host kendi kurum çıkışından NAT'lar. Alan adları açılışta çözülür.
    extra_targets: list[str] = field(default_factory=list)
    # --- eski alanlar (kullanılmıyor, eski profiller yüklensin diye korunur) ---
    scope: str = "lan"
    lan_targets: list[str] = field(default_factory=list)
    verify_hosts: list[str] = field(default_factory=list)
    allowed_domains: list[str] = field(default_factory=list)


@dataclass
class Profile:
    name: str
    uplink: str = ""                       # ör. "usb0" / "wlan0" (boşsa GUI sorar)
    uplink_kind: str = "auto"              # auto/usb/wifi/ethernet
    apps: list[str] = field(default_factory=list)   # başlatılacak komutlar
    dns: list[str] = field(default_factory=lambda: ["8.8.8.8", "1.1.1.1"])
    # WiFi uplink için
    wifi_ssid: str = ""
    wifi_password: str = ""                # not: düz metin; ileride keyring
    # Davranış
    persistent_profile: bool = True        # uygulama verileri kalıcı mı (izole profil)
    use_system_profile: bool = False       # mevcut tarayıcı profilini kullan (girişler korunur)
    auto_reconnect: bool = True            # supervisor açık mı
    # İzole alan İÇİNDE OpenVPN istemcisi (.ovpn yolu). Taşıyıcı olarak izole
    # alanın uplink'ini kullanır; namespace içinde tun açar → izole app'ler VPN
    # üzerinden uzak ağa (ör. garageliman) ulaşır. Boşsa VPN kurulmaz.
    vpn_config: str = ""
    relay: RelayPolicy = field(default_factory=RelayPolicy)

    def profile_data_dir(self, username: str) -> str:
        """Bu profilin kalıcı uygulama-verisi kök dizini."""
        return os.path.join(_data_dir_for(username), "profiles", self.name)

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        d = dict(d)
        relay = d.pop("relay", {}) or {}
        prof = cls(**{k: v for k, v in d.items() if k in cls.__annotations__})
        prof.relay = RelayPolicy(**{k: v for k, v in relay.items()
                                    if k in RelayPolicy.__annotations__})
        return prof


@dataclass
class Settings:
    namespace: str = "tether_zone"
    http_host: str = "127.0.0.1"
    http_port: int = 8787
    watchdog_interval: int = 4             # saniye
    profiles: dict[str, Profile] = field(default_factory=dict)
    active_profile: str = "default"

    # ---- yükle / kaydet ----
    @classmethod
    def load(cls) -> "Settings":
        if not os.path.exists(CONFIG_FILE):
            s = cls()
            s.profiles["default"] = Profile(name="default")
            return s
        with open(CONFIG_FILE) as f:
            raw = json.load(f)
        profiles = {
            name: Profile.from_dict({**p, "name": name})
            for name, p in raw.get("profiles", {}).items()
        }
        if not profiles:
            profiles["default"] = Profile(name="default")
        return cls(
            namespace=raw.get("namespace", "tether_zone"),
            http_host=raw.get("http_host", "127.0.0.1"),
            http_port=int(raw.get("http_port", 8787)),
            watchdog_interval=int(raw.get("watchdog_interval", 4)),
            profiles=profiles,
            active_profile=raw.get("active_profile", next(iter(profiles))),
        )

    def save(self) -> None:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        data = {
            "namespace": self.namespace,
            "http_host": self.http_host,
            "http_port": self.http_port,
            "watchdog_interval": self.watchdog_interval,
            "active_profile": self.active_profile,
            "profiles": {name: asdict(p) for name, p in self.profiles.items()},
        }
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, CONFIG_FILE)
        # sudo ile çalışıyorsak dosyanın sahipliğini gerçek kullanıcıya ver
        _chown_to_real_user(CONFIG_DIR)

    def profile(self, name: Optional[str] = None) -> Profile:
        name = name or self.active_profile
        if name not in self.profiles:
            self.profiles[name] = Profile(name=name)
        return self.profiles[name]


def _chown_to_real_user(path: str) -> None:
    """Config root iken yazıldıysa kullanıcının erişebilmesi için chown."""
    if not system.is_root():
        return
    user = system.real_user()
    try:
        import pwd
        pw = pwd.getpwnam(user)
    except KeyError:
        return
    for root, dirs, files in os.walk(path):
        for name in [*dirs, *files, ""]:
            target = os.path.join(root, name) if name else root
            try:
                os.chown(target, pw.pw_uid, pw.pw_gid)
            except OSError:
                pass
