"""Profil ve ayar yönetimi.

Bir *profil*, izole bir oturumun tüm tanımıdır: hangi uplink, hangi
uygulamalar, DNS, relay politikası vb. Profiller kalıcıdır (silinmez);
böylece kullanıcı "iş", "kişisel" gibi birden çok kayıtlı yapılandırmayı
tekrar tekrar kullanabilir. Uygulama profil dizinleri de kalıcıdır; yani
tarayıcı oturumları/oturum açmaları korunur.

Güvenlik:
  - config.json 0600 izni (G-3)
  - http_port/watchdog_interval doğrulaması (B-8)
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Optional

from . import system

def _default_config_dir() -> str:
    """Config kök dizini — daemon root çalışsa bile GERÇEK kullanıcının ev dizininde.

    ÖNEMLİ: Daemon `pkexec`/`sudo` ile root çalışır. `~/.config` root için
    /root/.config'e çözülür; oysa masaüstü başlatıcısı ve kullanıcı ayarları
    /home/<user>/.config'tedir. İkisi ayrılırsa daemon kullanıcının portunu/
    profillerini GÖRMEZ (ör. config 8795 iken root varsayılan 8787'ye bağlanır)
    → başlatıcı yanlış portu kontrol eder ve panel "başlatılamadı" verir.
    Bu yüzden root iken config'i her zaman gerçek kullanıcının ev dizininde tutarız.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg and not system.is_root():
        base = xdg
    else:
        base = os.path.join(system.user_home(system.real_user()), ".config")
    return os.path.join(base, "tether-isolator")


CONFIG_DIR = _default_config_dir()
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
VPNS_DIR = os.path.join(CONFIG_DIR, "vpns")


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
    extra_targets: list[str] = field(default_factory=list)
    # --- eski alanlar (kullanılmıyor, eski profiller yüklensin diye korunur) ---
    scope: str = "lan"
    lan_targets: list[str] = field(default_factory=list)
    verify_hosts: list[str] = field(default_factory=list)
    allowed_domains: list[str] = field(default_factory=list)


@dataclass
class Profile:
    name: str
    uplink: str = ""
    uplink_kind: str = "auto"
    apps: list[str] = field(default_factory=list)
    dns: list[str] = field(default_factory=lambda: ["8.8.8.8", "1.1.1.1"])
    # WiFi uplink için
    wifi_ssid: str = ""
    wifi_password: str = ""
    # Davranış
    persistent_profile: bool = True
    use_system_profile: bool = False
    auto_reconnect: bool = True
    vpn_config: str = ""
    vpn_required: bool = False       # H-3: VPN zorunluysa düşünce kill-switch
    relay: RelayPolicy = field(default_factory=RelayPolicy)

    def profile_data_dir(self, username: str) -> str:
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
    onboarded: bool = False                # U-1: onboarding tamamlandı mı?
    # .desktop kısayolundan eklenen özel uygulamalar (global, VPN listesi gibi
    # tüm profillerde görünür). Her biri {"name": görünen ad, "command": çalıştırılacak komut}.
    custom_apps: list[dict] = field(default_factory=list)

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
        # Değer doğrulama (B-8)
        http_port = int(raw.get("http_port", 8787))
        if http_port < 1 or http_port > 65535:
            http_port = 8787
        watchdog_interval = int(raw.get("watchdog_interval", 4))
        if watchdog_interval < 1:
            watchdog_interval = 4
        custom_apps = [
            {"name": str(c.get("name") or c.get("command", "")), "command": str(c["command"])}
            for c in raw.get("custom_apps", [])
            if isinstance(c, dict) and c.get("command")
        ]
        return cls(
            namespace=raw.get("namespace", "tether_zone"),
            http_host=raw.get("http_host", "127.0.0.1"),
            http_port=http_port,
            watchdog_interval=watchdog_interval,
            profiles=profiles,
            active_profile=raw.get("active_profile", next(iter(profiles))),
            onboarded=bool(raw.get("onboarded", False)),
            custom_apps=custom_apps,
        )

    def save(self) -> None:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        data = {
            "namespace": self.namespace,
            "http_host": self.http_host,
            "http_port": self.http_port,
            "watchdog_interval": self.watchdog_interval,
            "active_profile": self.active_profile,
            "onboarded": self.onboarded,
            "custom_apps": self.custom_apps,
            "profiles": {name: asdict(p) for name, p in self.profiles.items()},
        }
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.chmod(tmp, 0o600)  # G-3: geçici dosyaya da 0600
        os.replace(tmp, CONFIG_FILE)
        os.chmod(CONFIG_FILE, 0o600)  # G-3: kalıcı dosyaya 0600
        # M-2: chown için system.chown_to_user kullan (tek yardımcı)
        _chown_file_only(CONFIG_DIR, CONFIG_FILE)

    def profile(self, name: Optional[str] = None) -> Profile:
        name = name or self.active_profile
        if name not in self.profiles:
            self.profiles[name] = Profile(name=name)
        return self.profiles[name]


def _chown_file_only(config_dir: str, config_file: str) -> None:
    """M-2/P-5: Yalnızca config dosyası ve dizinini chown et (özyinelemeli değil)."""
    if not system.is_root():
        return
    user = system.real_user()
    try:
        import pwd
        pw = pwd.getpwnam(user)
    except KeyError:
        return
    for path in (config_dir, config_file):
        try:
            os.chown(path, pw.pw_uid, pw.pw_gid)
        except OSError:
            pass
