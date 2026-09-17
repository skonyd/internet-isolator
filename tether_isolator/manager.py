"""Orkestratör — tüm parçaları birleştirir ve oturum yaşam döngüsünü yönetir.

Sorumluluklar:
  * start_session / stop_session
  * relay aç/kapa
  * sürekli çalışan watchdog (dayanıklılık): uplink kopup geri geldiğinde
    uygulamaları ÖLDÜRMEDEN arayüzü tekrar namespace'e alıp DHCP'yi yeniler.

Watchdog'un temel fikri: uygulamalar namespace içindedir ve namespace
yaşamaya devam eder. Yalnızca fiziksel uplink gelip gider. Telefon çıkarılıp
takıldığında arayüz host'ta yeniden belirir; watchdog bunu görür ve sessizce
namespace'e geri taşır. Uygulamaların soketleri kopsa da süreçleri yaşar;
tarayıcılar kendiliğinden yeniden dener.

Güvenlik:
  - Kill-switch (G-6): uplink yokken blackhole rota → paket sızmaz
  - IPv6 sızıntı kapatma (G-7): namespace içinde IPv6 devre dışı
  - VPN watchdog (H-3): VPN düşünce yeniden bağlan veya kill-switch
"""
from __future__ import annotations

import copy
import logging
import os
import signal
import threading
import time

from . import apps, shaping, system, usage
from .config import DataSaverPolicy, Profile, Settings, VPNS_DIR
from .engine import Engine, EngineError
from .relay import Relay
from .state import AppProcess, RuntimeState
from .vpn import VPN

# Veri tasarrufu seviyesi -> watchdog aralığı (saniye) / dış IP önbellek TTL'i
_DATA_SAVER_WATCHDOG_SEC = {"light": None, "balanced": 10, "strict": 20}
_DATA_SAVER_PUBLIC_IP_TTL = {"light": 120, "balanced": 300, "strict": 900}
# Veri tasarrufu seviyesi -> varsayılan bant genişliği tavanı (indirme, yükleme) kbit/s.
# 0 = sınırsız. Profildeki cap_down_kbit/cap_up_kbit girilirse bunu geçersiz kılar.
_LEVEL_CAPS_KBIT = {"light": (0, 0), "balanced": (2000, 1000), "strict": (700, 300)}
# Medya kademesi -> indirme tavanı (kbit/s). Uyarlanabilir oynatıcılar (YouTube,
# Netflix...) ölçtükleri hıza göre çözünürlüğü KENDİLERİ seçer; tarayıcıya
# "360p oynat" diyen bir arayüz olmadığından kaliteyi tavanla aşağı çekiyoruz.
# Değerler YouTube'un tipik VP9 bitrate'lerine göre seçildi (144p ~0.15 Mbit,
# 360p ~0.7 Mbit, 720p ~2.5 Mbit) + sayfa varlıkları için pay. DİKKAT: tavan tüm
# izole trafiği etkiler, yalnızca videoyu değil (yalnızca medyayı ayırmak için
# IP listesi gerekirdi). 144p'de tavan bilinçli olarak video bitrate'inin
# üstünde (400) tutuldu; daha aşağısı sayfaların kendisini de kullanılamaz yapar.
_MEDIA_QUALITY_CAP_KBIT = {"144p": 400, "360p": 1000, "720p": 3000}

log = logging.getLogger("tether.manager")


class Manager:
    def __init__(self, settings: Settings, *, dry_run: bool = False):
        self.s = settings
        self.dry = dry_run
        self.engine = Engine(settings, dry_run=dry_run)
        self.relay = Relay(settings, dry_run=dry_run)
        self.vpn = VPN(settings, dry_run=dry_run)
        self.state = RuntimeState(namespace=settings.namespace)
        self._lock = threading.RLock()
        self._wd_stop = threading.Event()
        self._wd_thread: threading.Thread | None = None
        self._active_iface: str | None = None
        self._active_profile: Profile | None = None
        # Önbellekler (P-3)
        self._cached_installed_apps: list[str] | None = None
        self._cached_interfaces_ttl: float = 0.0
        self._cached_interfaces: list = []
        # Trafik sayacı (H-1)
        self._traffic_last_rx: int = 0
        self._traffic_last_tx: int = 0
        self._traffic_session_rx: int = 0
        self._traffic_session_tx: int = 0
        self._traffic_last_ts: float = 0.0
        # Veri tasarrufu: cimri prob önbelleği
        self._public_ip_cache: str = ""
        self._public_ip_cache_ts: float = 0.0
        self._quota_triggered_month: str = ""
        # Suspend/resume (H-4)
        self._suspend_handler: threading.Thread | None = None
        # IPv6 kapalı mı (G-7)
        self._ipv6_disabled: bool = False

    @property
    def active_profile(self) -> Profile | None:
        """Etkin profili döndürür (M-1: kapsülleme)."""
        return self._active_profile

    # ---------------------------------------------------------------- start
    def start_session(self, profile: Profile, uplink: str) -> None:
        with self._lock:
            if self.state.phase in ("online", "starting", "degraded"):
                raise EngineError("Zaten etkin bir oturum var; önce durdurun.")
            self._active_iface = uplink
            self._active_profile = profile
            self.state = RuntimeState(
                phase="starting", profile=profile.name,
                namespace=self.s.namespace, uplink=uplink,
                session_started_at=time.time(),
            )
            self._event("info", f"Oturum başlatılıyor: profil={profile.name} uplink={uplink}")

        try:
            self.engine.ensure_namespace(profile)

            # IPv6 sızıntı kapatma (G-7)
            self._disable_ipv6_in_ns()

            # Uplink'i izole alana al
            self._bring_uplink_into_ns(uplink)
            # IPv6'yı arayüz İÇERİ GİRDİKTEN sonra tekrar uygula: taşınan arayüz
            # kendi disable_ipv6 değerini koruyabildiğinden yalnızca all/default
            # yetmez (bkz. _disable_ipv6_in_ns).
            self._disable_ipv6_in_ns(uplink)
            self._apply_shaping(uplink)

            with self._lock:
                self._event("info", "IP adresi alınıyor (DHCP)...")
            self.engine.start_dhcp(uplink)
        except Exception as e:
            with self._lock:
                self._event("error", f"Başlatılamadı: {e}")
                try:
                    self._remove_shaping(uplink)
                    self.engine.teardown(uplink)
                except Exception as te:  # noqa: BLE001
                    log.warning("başarısız başlatma temizliğinde: %s", te)
                self.state = RuntimeState(phase="idle", namespace=self.s.namespace)
                self._active_iface = None
                self._active_profile = None
                self.state.persist()
            raise

        with self._lock:
            self._refresh_network()
            if profile.relay.enabled_by_default:
                self.enable_relay(profile)

            # Not: VPN'ler artık panelden ADA göre yönetiliyor (çoklu VPN, VPNS_DIR
            # altında <ad>.ovpn). Oturum başlarken otomatik bağlanma yok; kullanıcı
            # istediği tüneli panelden açar. (Eski path-tabanlı profile.vpn_config
            # otomatik bağlanması yeni ad-tabanlı connect_vpn ile uyumsuzdu.)

            # Uygulamaları başlat
            for prog in profile.apps:
                try:
                    pid = apps.launch(self.s, profile, prog, dry_run=self.dry)
                    self.state.apps.append(AppProcess(
                        command=prog, pid=pid,
                        media_level=profile.data_saver.media_level))
                    self._event("info", f"Uygulama başlatıldı: {prog} (pid={pid})")
                except Exception as e:  # tek uygulama patlarsa oturum ölmesin
                    self._event("error", f"{prog} başlatılamadı: {e}")

            self.state.phase = "online" if self.state.online else "degraded"
            self._event("ok" if self.state.online else "warn",
                        "Bağlantı kuruldu." if self.state.online
                        else "Uplink hazır ama internet doğrulanamadı.")
            self.state.persist()

            if profile.auto_reconnect:
                self._start_watchdog()

    # ----------------------------------------------------------------- stop
    def stop_session(self) -> None:
        with self._lock:
            self._event("info", "Oturum durduruluyor...")
            self.state.phase = "stopping"
            self._stop_watchdog()
            self._terminate_apps()
            try:
                for v in self.state.vpns:
                    if v.get("active"):
                        self.vpn.disconnect(v["name"])
            except Exception as e:  # noqa: BLE001
                log.warning("VPN'ler kapatılırken: %s", e)
            try:
                self.relay.disable()
            except Exception as e:  # noqa: BLE001
                log.warning("relay kapatılırken: %s", e)
            self._remove_shaping(self._active_iface)
            self.engine.teardown(self._active_iface)
            self._event("ok", "Her şey eski haline döndü.")
            self.state = RuntimeState(phase="idle", namespace=self.s.namespace)
            self._active_iface = None
            self._active_profile = None
            self.state.persist()

    # ---------------------------------------------------------------- relay
    def enable_relay(self, profile: Profile | None = None,
                     extra_targets: list[str] | None = None) -> None:
        with self._lock:
            prof = profile or self._active_profile
            if not prof:
                raise EngineError("Etkin oturum yok.")
            if extra_targets is not None:
                prof.relay.extra_targets = list(extra_targets)
            try:
                routes = self.relay.enable(prof.relay)
            except Exception as e:
                self.state.relay_active = False
                self.state.relay_targets = []
                self._event("error", f"Relay açılamadı: {e}")
                self.state.persist()
                raise

            self.state.relay_active = True
            self.state.relay_scope = prof.relay.scope
            self.state.relay_targets = list(routes)
            if routes:
                detail = f" → {', '.join(routes)}"
            else:
                detail = (" (uyarı: bu makine şu an LAN'a bağlı görünmüyor; "
                          "ethernet kablosu takılı mı?)")
            self._event("ok", f"Relay açıldı{detail}. İnternet tether'de.")
            self.state.persist()

    def reassert_relay_routes(self) -> None:
        """Relay rotalarını (LAN + ek hedefler) tazeler; her yerden çağrılabilir.

        VPN istemcisi ns İÇİNDE çalışır ve kendi push route'larını ekler; bu
        rotalar relay'in host-LAN rotalarının üzerine yazabilir. Ayrıca
        kullanıcı ek hedef listesini relay AÇIKKEN değiştirirse (bkz.
        /api/relay/targets/*) yeni hedefin hemen etkin olması için de bu
        çağrılır. Relay aktifse rotaları burada yeniden uygulayarak kurum
        hedeflerinin host LAN üzerinden gitmeye devam etmesini sağlarız.
        """
        with self._lock:
            self._reassert_relay_routes_locked()

    def set_relay_scope(self, scope: str) -> None:
        """Relay kapsamını değiştirir: "lan" (tüm host LAN rotaları) ya da
        "custom" (yalnızca elle eklenen extra_targets). Relay AÇIKKEN
        çağrılırsa kanal yeniden kurulur (reassert değil): "lan"dan "custom"a
        geçişte artık listede olmayan LAN rotalarının ns içinde asılı
        kalmaması gerekir; reassert yalnızca ekler/üzerine yazar, silmez."""
        with self._lock:
            prof = self._active_profile
            if not prof:
                return
            prof.relay.scope = scope
            if self.state.relay_active:
                self.relay.disable()
                routes = self.relay.enable(prof.relay)
                self.state.relay_scope = scope
                self.state.relay_targets = list(routes)

    def remove_relay_target(self, value: str) -> None:
        """Ek hedef listeden kaldırıldığında (relay AÇIKKEN) eski rotayı da siler."""
        with self._lock:
            if not self.state.relay_active or not self._active_profile:
                return
            try:
                self.relay.remove_extra_target(self._active_profile.relay, value)
            except Exception as e:  # noqa: BLE001
                log.warning("relay hedef rotası silinirken: %s", e)
            self._reassert_relay_routes_locked()

    def _reassert_relay_routes_locked(self) -> None:
        if not self.state.relay_active or not self._active_profile:
            return
        try:
            routes = self.relay.reassert_routes(self._active_profile.relay)
            if routes:
                self.state.relay_targets = list(routes)
        except Exception as e:  # noqa: BLE001
            log.warning("relay rotaları tazelenirken: %s", e)

    def disable_relay(self) -> None:
        with self._lock:
            self.relay.disable()
            self.state.relay_active = False
            self.state.relay_scope = ""
            self.state.relay_targets = []
            self._event("info", "Relay kapatıldı; tam izolasyon.")
            self.state.persist()

    # ------------------------------------------------- bant genişliği tavanı (Faz 4)
    def _effective_caps(self, ds: DataSaverPolicy) -> tuple[int, int]:
        """Uygulanacak (indirme, yükleme) tavanı — kbit/s, 0 = sınırsız.

        İki bağımsız kaynak birleşir:
          * Veri tasarrufu (yalnızca `enabled` iken): seviye ön ayarı ya da
            kullanıcının girdiği cap_down/up_kbit.
          * Medya kademesi (`enabled`'dan BAĞIMSIZ): 360p/720p için kalite
            tavanı.
        İkisi de doluysa DAHA DÜŞÜK olan kazanır — böylece hiçbir ayarın
        vaadi çiğnenmez (720p seçiliyken 'strict' tavanı gevşetilmez).
        """
        down = up = 0
        if ds.enabled:
            base_down, base_up = _LEVEL_CAPS_KBIT.get(ds.level, (0, 0))
            down = ds.cap_down_kbit or base_down
            up = ds.cap_up_kbit or base_up
        media_down = _MEDIA_QUALITY_CAP_KBIT.get(ds.media_level, 0)
        if media_down:
            down = min(down, media_down) if down else media_down
        return max(0, down), max(0, up)

    def _apply_shaping(self, iface: str | None) -> None:
        """Etkin profile göre arayüze indirme/yükleme tavanı uygular (idempotent).

        Uplink namespace'e HER giriş yaptığında (başlangıç, uplink değişimi,
        watchdog yeniden bağlanma) çağrılmalı — arayüz taze girdiğinde qdisc
        yoktur/sıfırlanmış olabilir.
        """
        prof = self._active_profile
        if not prof or not iface:
            return
        # NOT: `ds.enabled` kontrolü _effective_caps içinde — medya kademesi
        # (360p/720p) genel veri tasarrufu KAPALIYKEN de tavan uygulayabilmeli.
        down, up = self._effective_caps(prof.data_saver)
        if down <= 0 and up <= 0:
            self._remove_shaping(iface)
            return
        try:
            info = shaping.apply(self.s.namespace, iface, down_kbit=down, up_kbit=up,
                                 dry_run=self.dry)
        except Exception as e:  # noqa: BLE001
            self.state.shaping_active = False
            self._event("warn", f"Bant genişliği tavanı uygulanamadı: {e}")
            return
        applied_down = down if info.get("download_method") else 0
        applied_up = up if info.get("upload_applied") else 0
        self.state.shaping_active = bool(applied_down or applied_up)
        self.state.shaping_down_kbit = applied_down
        self.state.shaping_up_kbit = applied_up
        self.state.shaping_method = info.get("download_method", "")
        if self.state.shaping_active:
            detail = []
            if applied_down:
                detail.append(f"↓{down}kbit/s ({self.state.shaping_method or '?'})")
            if applied_up:
                detail.append(f"↑{up}kbit/s")
            self._event("info", f"Bant genişliği tavanı: {' '.join(detail)}")

    def _remove_shaping(self, iface: str | None) -> None:
        """Arayüzdeki tavanı kaldırır — uplink namespace'ten ÇIKMADAN önce çağrılmalı.

        Aksi halde qdisc konfigürasyonu arayüzle birlikte host'a/başka bir yere
        taşınabilir (bkz. shaping.remove docstring'i).
        """
        if not iface:
            return
        try:
            shaping.remove(self.s.namespace, iface, dry_run=self.dry)
        except Exception as e:  # noqa: BLE001
            log.warning("bant genişliği tavanı kaldırılırken: %s", e)
        self.state.shaping_active = False
        self.state.shaping_down_kbit = 0
        self.state.shaping_up_kbit = 0
        self.state.shaping_method = ""

    def reassert_shaping(self) -> None:
        """Kullanıcı panelden ayarı değiştirdiğinde canlı oturuma hemen uygular."""
        with self._lock:
            iface = self._active_iface
            if not iface or self.state.phase in ("idle", "stopping"):
                return
            self._apply_shaping(iface)
            self.state.persist()

    # ---------------------------------------------------------------- VPN
    def _update_vpn_state(self, name: str, st: dict) -> None:
        st["name"] = name
        for i, v in enumerate(self.state.vpns):
            if v["name"] == name:
                self.state.vpns[i] = st
                return
        self.state.vpns.append(st)

    def connect_vpn(self, name: str) -> None:
        """İzole alan içinde belirtilen isimle OpenVPN başlatır."""
        with self._lock:
            if self.state.phase in ("idle", "stopping"):
                raise EngineError("Etkin oturum yok; önce başlatın.")
            path = os.path.join(VPNS_DIR, f"{name}.ovpn")
            if not os.path.exists(path):
                raise EngineError(f"VPN dosyası bulunamadı: {name}.ovpn")
            self._event("info", f"VPN bağlanıyor: {name}")
            try:
                self.vpn.connect(name, path)
            except Exception as e:
                self._update_vpn_state(name, {"active": False})
                self._event("error", f"VPN '{name}' bağlanamadı: {e}")
                self.state.persist()
                raise
            st = self.vpn.status(name)
            self._update_vpn_state(name, st)
            self._reassert_relay_routes_locked()
            self._event("ok", f"VPN '{name}' bağlandı ({st.get('iface')} {st.get('ip')}).")
            self.state.persist()

    def disconnect_vpn(self, name: str) -> None:
        with self._lock:
            self.vpn.disconnect(name)
            self._update_vpn_state(name, {"active": False})
            self._event("info", f"VPN '{name}' kapatıldı.")
            self.state.persist()

    # ------------------------------------------------------------- devralma
    def adopt_if_running(self) -> bool:
        if self.dry:
            return False
        with self._lock:
            if not system.namespace_exists(self.s.namespace):
                return False
            prev = RuntimeState.read() or {}
            uplink = prev.get("uplink")
            if not uplink or not system.interface_in_namespace(self.s.namespace, uplink):
                return False

            self._active_profile = self.s.profile(
                prev.get("profile") or self.s.active_profile)
            self._active_iface = uplink
            self.state = RuntimeState(
                phase="starting", profile=self._active_profile.name,
                namespace=self.s.namespace, uplink=uplink,
                reconnect_count=int(prev.get("reconnect_count", 0)),
                session_started_at=float(prev.get("session_started_at") or time.time()),
            )
            # Önceki uygulama kayıtlarından hâlâ yaşayanları geri al. Başlatma
            # anındaki medya kademesi de korunur; böylece daemon yeniden başlasa
            # bile "bu tarayıcı eski ayarla açıldı" uyarısı doğru kalır.
            for a in prev.get("apps", []):
                if apps.pid_alive(a.get("pid", 0)):
                    self.state.apps.append(AppProcess(
                        command=a.get("command", "?"), pid=a["pid"],
                        media_level=a.get("media_level", "")))
            self._event("info", f"Mevcut oturum devralındı (uplink={uplink}); "
                                f"kaldığın yerden devam.")
            self._refresh_network()
            self.state.relay_active = self.relay.is_active()
            self._apply_shaping(uplink)   # daemon yeniden başlarken ayarla senkronize et
            for v in prev.get("vpns", []):
                name = v.get("name")
                if name and self.vpn.is_active(name):
                    st = self.vpn.status(name)
                    self._update_vpn_state(name, st)
            self.state.phase = "online" if self.state.online else "degraded"
            self.state.persist()
            if self._active_profile.auto_reconnect:
                self._start_watchdog()
            return True

    def restart_apps(self) -> list[str]:
        """Açık uygulamaları kapatıp aynı listeyle yeniden başlatır.

        Veri tasarrufu bayrakları/Firefox user.js YALNIZCA yeni açılan bir
        örnekte etkili olur (ör. Chromium komut satırı bayrağı zaten çalışan
        bir sürece sonradan uygulanamaz); kullanıcı ayarı değiştirdikten sonra
        bunu tetikleyerek açık tarayıcılara da yansıtabilir.
        """
        with self._lock:
            if self.state.phase in ("idle", "stopping"):
                raise EngineError("Etkin oturum yok.")
            prof = self._active_profile
            assert prof
            programs = [a.command for a in self.state.apps]
            self._terminate_apps()
            deadline = time.time() + 3
            while time.time() < deadline and any(
                    apps.pid_alive(a.pid) for a in self.state.apps):
                time.sleep(0.1)
            self.state.apps = []
            started: list[str] = []
            for prog in programs:
                try:
                    pid = apps.launch(self.s, prof, prog, dry_run=self.dry)
                    self.state.apps.append(AppProcess(
                        command=prog, pid=pid,
                        media_level=prof.data_saver.media_level))
                    started.append(prog)
                except Exception as e:  # noqa: BLE001
                    self._event("error", f"{prog} yeniden başlatılamadı: {e}")
            self._event("info", f"Uygulamalar yeniden başlatıldı: {', '.join(started) or '(yok)'}")
            self.state.persist()
            return started

    # ---------------------------------------------------------- canlı işlemler
    def launch_app(self, program: str) -> int:
        with self._lock:
            if self.state.phase in ("idle", "stopping"):
                raise EngineError("Etkin oturum yok; önce başlatın.")
            prof = self._active_profile
            assert prof
            try:
                pid = apps.launch(self.s, prof, program, dry_run=self.dry)
            except Exception as e:  # noqa: BLE001
                self._event("error", f"{program} başlatılamadı: {e}")
                self.state.persist()
                raise
            self.state.apps.append(AppProcess(
                command=program, pid=pid,
                media_level=prof.data_saver.media_level))
            self._event("info", f"Uygulama başlatıldı: {program} (pid={pid})")
            self.state.persist()
            return pid

    def import_profile(self, program: str) -> str:
        """Host'taki GERÇEK uygulama profilini izole (kalıcı) profile bir kerelik aktarır.

        Etkin oturum GEREKMEZ (kullanıcı, oturumu başlatmadan önce eski verilerini
        taşımak isteyebilir); etkin profil yoksa ayarlardaki (varsayılan) profil
        kullanılır.
        """
        with self._lock:
            prof = self._active_profile or self.s.profile()
            try:
                dest = apps.import_profile(self.s, prof, program, dry_run=self.dry)
            except Exception as e:  # noqa: BLE001
                self._event("error", f"Profil içe aktarılamadı: {e}")
                self.state.persist()
                raise
            self._event("ok", f"'{program}' profili host'tan içe aktarıldı → {dest}")
            self.state.persist()
            return dest

    def force_reconnect(self) -> None:
        with self._lock:
            iface = self._active_iface
            if not iface:
                raise EngineError("Etkin oturum yok.")
            self.state.phase = "reconnecting"
            self._event("info", "Elle yeniden bağlanılıyor...")
            if not self.dry and not system.interface_in_namespace(self.s.namespace, iface):
                host_ifaces = {i.name for i in self._list_interfaces_cached()}
                if iface in host_ifaces:
                    self._bring_uplink_into_ns(iface)
                    self._disable_ipv6_in_ns(iface)
            self._apply_shaping(iface)
            self.engine.start_dhcp(iface)
            self.state.reconnect_count += 1
            self.state.last_reconnect = time.time()
            self._refresh_network()
            self.state.phase = "online" if self.state.online else "degraded"
            self._event("ok" if self.state.online else "warn",
                        "Yeniden bağlanıldı." if self.state.online
                        else "Bağlantı kurulamadı (uplink/veri yok).")
            self.state.persist()

    def _bring_uplink_into_ns(self, iface: str) -> None:
        prof = self._active_profile
        is_wifi = (prof is not None and prof.uplink_kind == "wifi") \
            or system._classify(iface) == "wifi"
        if is_wifi:
            self.engine.move_wifi_phy_in(iface)
            self.engine.move_uplink_in(iface)
            if prof and prof.wifi_ssid:
                self.engine.wifi_associate(iface, prof.wifi_ssid, prof.wifi_password)
                if not self.engine.wait_wifi_associated(iface):
                    raise EngineError(
                        f"WiFi ağına bağlanılamadı: '{prof.wifi_ssid}'. "
                        f"SSID/parola yanlış olabilir, ağ menzil dışı olabilir "
                        f"ya da 5GHz/bant desteklenmiyor olabilir.")
            elif prof:
                raise EngineError(
                    "WiFi uplink seçildi ama SSID/parola girilmedi. Panelde "
                    "WiFi ağ adı ve parolasını girin.")
        else:
            self.engine.move_uplink_in(iface)

    def switch_uplink(self, new_iface: str, *, uplink_kind: str | None = None,
                      wifi_ssid: str | None = None,
                      wifi_password: str | None = None) -> None:
        with self._lock:
            if self.state.phase in ("idle", "stopping"):
                raise EngineError("Etkin oturum yok; önce başlatın.")
            prof = self._active_profile
            old = self._active_iface
            if new_iface == old:
                raise EngineError("Yeni uplink mevcutla aynı.")
            self._event("info", f"Uplink değiştiriliyor: {old} → {new_iface}")

            if prof is not None:
                if uplink_kind:
                    prof.uplink_kind = uplink_kind
                if wifi_ssid is not None:
                    prof.wifi_ssid = wifi_ssid
                if wifi_password:
                    prof.wifi_password = wifi_password
                elif prof.wifi_ssid and prof.wifi_ssid in self.s.wifi_networks:
                    prof.wifi_password = self.s.wifi_networks[prof.wifi_ssid]
                if prof.wifi_ssid and prof.wifi_password:
                    self.s.remember_wifi(prof.wifi_ssid, prof.wifi_password)

            self.state.phase = "reconnecting"
            self.state.persist()

            # 1) Eski uplink'i host'a iade et — ÖNCE tavanı kaldır: arayüz ns'ten
            #    çıkmadan önce (aksi halde qdisc konfigürasyonu host'a sürüklenebilir)
            if old and not self.dry:
                try:
                    self._remove_shaping(old)
                    self.engine.stop_dhcp(old)
                    if system._classify(old) == "wifi":
                        self.engine.move_wifi_phy_out(old)
                    else:
                        self.engine.move_uplink_out(old)
                except Exception as e:  # noqa: BLE001
                    log.warning("eski uplink (%s) çıkarılırken: %s", old, e)

            # 2) Yeni uplink'i izole alana al  3) IP al
            self._active_iface = new_iface
            if prof is not None:
                prof.uplink = new_iface
            self.state.uplink = new_iface
            self._bring_uplink_into_ns(new_iface)
            self._disable_ipv6_in_ns(new_iface)
            self._apply_shaping(new_iface)
            self.engine.start_dhcp(new_iface)

            self.state.reconnect_count += 1
            self.state.last_reconnect = time.time()
            self._refresh_network()
            self.state.phase = "online" if self.state.online else "degraded"
            self._event("ok" if self.state.online else "warn",
                        f"Yeni uplink etkin: {new_iface}" if self.state.online
                        else f"{new_iface} hazır ama internet doğrulanamadı.")
            self.state.persist()

    # ------------------------------------------------------------- watchdog
    def _start_watchdog(self) -> None:
        if self._wd_thread and self._wd_thread.is_alive():
            return
        self._wd_stop.clear()
        self._wd_thread = threading.Thread(
            target=self._watchdog_loop, name="tether-watchdog", daemon=True)
        self._wd_thread.start()
        log.info("watchdog başlatıldı (her %ss)", self.s.watchdog_interval)

    def _stop_watchdog(self) -> None:
        self._wd_stop.set()
        if self._wd_thread:
            self._wd_thread.join(timeout=2)
            self._wd_thread = None

    def _watchdog_loop(self) -> None:
        while not self._wd_stop.wait(self._effective_watchdog_interval()):
            try:
                iface = self._active_iface
                if iface:
                    self._tick(iface)
            except Exception as e:  # noqa: BLE001
                log.warning("watchdog tick hatası: %s", e)

    def _effective_watchdog_interval(self) -> int:
        """Veri tasarrufu açıksa watchdog'un kendi prob trafiğini seyreltir.

        Yalnızca `frugal_probes` etkinken devreye girer; kapalıyken davranış
        `settings.watchdog_interval`'dan hiç değişmez (varsayılan sızıntısız).
        """
        base = self.s.watchdog_interval
        prof = self._active_profile
        if not prof or not prof.data_saver.enabled or not prof.data_saver.frugal_probes:
            return base
        widened = _DATA_SAVER_WATCHDOG_SEC.get(prof.data_saver.level)
        return max(base, widened) if widened else base

    def _tick(self, iface: str) -> None:
        """Tek bir izleme adımı: uplink yerinde mi, internet var mı?"""
        with self._lock:
            if self.state.phase in ("idle", "stopping"):
                return
            present = self.dry or system.interface_in_namespace(self.s.namespace, iface)
            if not present and not self.dry:
                # Tek seferlik "yok" tespiti gürültülü olabilir: ör. bu tick tam da
                # yeni bir uygulama başlatılırken (apps.launch -> kendi `ip netns
                # exec` çağrısı) çakışırsa, eşzamanlı `ip netns exec` çağrıları
                # arasında geçici bir yanlış-negatif oluşabilir. Kill-switch'i
                # kalıcı hale getirmeden önce kısaca bekleyip tekrar doğrula.
                time.sleep(0.5)
                present = system.interface_in_namespace(self.s.namespace, iface)

            if not present:
                if self.state.phase != "reconnecting":
                    self.state.phase = "reconnecting"
                    self._apply_kill_switch()
                self.state.uplink_present = False
                host_ifaces = {i.name for i in self._list_interfaces_cached()}
                if iface in host_ifaces:
                    self._event("warn", f"{iface} tekrar belirdi, namespace'e alınıyor...")
                    self._bring_uplink_into_ns(iface)
                    self._disable_ipv6_in_ns(iface)
                    self._apply_shaping(iface)
                    self.engine.start_dhcp(iface)
                    self.state.reconnect_count += 1
                    self.state.last_reconnect = time.time()
                    self._refresh_network()
                    if self.state.online:
                        self._remove_kill_switch()
                        self._event("ok", "Yeniden bağlanıldı.")
                else:
                    self._event("warn", f"{iface} bekleniyor (uplink yok).")
                self.state.persist()
                return

            # Arayüz yerinde — internet sağlığını kontrol et.
            # ÖNEMLİ: arayüz her zaman namespace'te kalan WiFi PHY gibi bir uplink
            # için "yok" tespiti host'a asla yansımayabilir (yukarıdaki host_ifaces
            # kontrolü hiç eşleşmez) — bu yüzden kill-switch'in TEK kaldırılma yolu
            # yukarıdaki host-reappearance dalı olamaz. Arayüz burada sağlıklı
            # görünüyorsa ve önceki bir tick'te kill-switch uygulanmışsa (phase hâlâ
            # "reconnecting"), onu burada da kaldır; aksi halde internet aslında
            # geri gelse bile blackhole rotası kalıcı olarak takılı kalır.
            if self.state.phase == "reconnecting":
                self._remove_kill_switch()
                self._event("ok", "Uplink doğrulandı, kill-switch kaldırıldı.")
            # P-2: Akıllı prob sıklığı — online iken light, degraded'de normal
            online_before = self.state.online
            self._refresh_network(light=self.state.online)
            if not self.state.online:
                if self.state.phase != "degraded":
                    self._event("warn", "İnternet kesildi; DHCP yenileniyor...")
                self.engine.start_dhcp(iface)
                self._refresh_network()
                self.state.phase = "online" if self.state.online else "degraded"
            else:
                self.state.phase = "online"
                # VPN watchdog (H-3): VPN aktif ama düşmüşse yeniden bağlan
                for v in self.state.vpns:
                    if v.get("active"):
                        name = v["name"]
                        if not self.vpn.is_active(name):
                            self._event("warn", f"VPN '{name}' düştü, yeniden bağlanılıyor...")
                            try:
                                self.connect_vpn(name)
                            except Exception as e:  # noqa: BLE001
                                self._event("error", f"VPN '{name}' yeniden bağlanamadı: {e}")
                                prof = self._active_profile
                                if getattr(prof, "vpn_required", False):
                                    self._apply_kill_switch()

            # Relay durumunu gerçeğe göre uzlaştır
            if self.state.relay_active and not self.dry and not self.relay.is_active():
                self.state.relay_active = False
                self.state.relay_scope = ""
                self.state.relay_targets = []
                self._event("warn", "Relay kanalı düştü; tam izolasyona dönüldü.")

            # Bant genişliği tavanını gerçeğe göre uzlaştır (Faz 4) — arayüz
            # sıfırlanmış (ör. WiFi yeniden ilişkilendirme) olabilir; kurallar
            # sessizce kaybolursa tasarruf fark edilmeden devre dışı kalır.
            if self.state.shaping_active and not self.dry and not shaping.is_active(
                    self.s.namespace, iface):
                self._event("warn", "Bant genişliği tavanı düştü; yeniden uygulanıyor.")
                self._apply_shaping(iface)

            # Uygulama canlılığı
            self._refresh_apps()
            # Trafik sayacı (H-1)
            self._update_traffic_stats(iface)
            self.state.persist()

    # ------------------------------------------------------------- yardımcı
    def _refresh_network(self, *, light: bool = False) -> None:
        iface = self._active_iface
        if not iface:
            return
        self.state.uplink_present = self.dry or system.interface_in_namespace(
            self.s.namespace, iface)
        ip, gw = self.engine.uplink_ip(iface)
        self.state.ip_address, self.state.gateway = ip, gw
        # P-2: light modda ping atma (sadece arayüz durumuna bak)
        if light:
            self.state.online = self.state.uplink_present and bool(ip)
        else:
            self.state.online = self.engine.connectivity()
        self.state.last_check = time.time()
        if not light and self.state.online:
            self.state.public_ip = self._get_public_ip_cached()

    def _get_public_ip_cached(self) -> str:
        """Dış IP sorgusu — veri tasarrufunda TTL'li önbellek, aksi halde her zaman taze."""
        prof = self._active_profile
        ttl = 0
        if prof and prof.data_saver.enabled and prof.data_saver.frugal_probes:
            ttl = _DATA_SAVER_PUBLIC_IP_TTL.get(prof.data_saver.level, 0)
        now = time.time()
        if ttl and self._public_ip_cache and (now - self._public_ip_cache_ts) < ttl:
            return self._public_ip_cache
        ip = self.engine.public_ip()
        if ip:
            self._public_ip_cache = ip
            self._public_ip_cache_ts = now
        return ip or self._public_ip_cache

    def _refresh_apps(self) -> None:
        if self.dry:
            return
        for a in self.state.apps:
            a.running = apps.pid_alive(a.pid)

    def _terminate_apps(self) -> None:
        if self.dry:
            return
        for a in self.state.apps:
            if not apps.pid_alive(a.pid):
                continue
            try:
                os.killpg(os.getpgid(a.pid), signal.SIGTERM)
            except OSError:
                try:
                    os.kill(a.pid, signal.SIGTERM)
                except OSError:
                    pass

    def _event(self, level: str, msg: str) -> None:
        log.info("[%s] %s", level, msg)
        self.state.log_event(level, msg)

    # ------------------------------------------------------- kill-switch (G-6)
    def _apply_kill_switch(self) -> None:
        """Namespace içinde fail-closed rota: uplink yokken blackhole default."""
        if self.dry:
            return
        try:
            self.engine._ns("ip", "route", "replace", "blackhole", "default",
                            check=False)
            self._event("warn", "Kill-switch etkin: internet yok, paketler engellendi.")
        except Exception as e:  # noqa: BLE001
            log.warning("kill-switch uygulanamadı: %s", e)

    def _remove_kill_switch(self) -> None:
        """Kill-switch'i kaldır (uplink geldiğinde)."""
        if self.dry:
            return
        try:
            self.engine._ns("ip", "route", "del", "blackhole", "default",
                            check=False)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------- IPv6 sızıntı kapatma (G-7)
    def _disable_ipv6_in_ns(self, iface: str | None = None) -> None:
        """Namespace içinde IPv6'yı devre dışı bırak ve GERÇEKTEN kapandığını doğrula.

        İki tuzak vardı:
          1) `all`/`default` uplink namespace'e ALINMADAN önce ayarlanıyordu;
             sonradan taşınan arayüz kendi `disable_ipv6` değerini koruyabilir.
             Bu yüzden uplink içeri girdikten sonra ARAYÜZE ÖZEL de uygulanır.
          2) Komutlar `check=False` ile çalıştırılıp sonuç hiç okunmuyordu ve
             `_ipv6_disabled = True` koşulsuz set ediliyordu → başarısızlık
             SESSİZCE yutuluyordu. Sahada IPv6 açık kalmıştı; bu hem sızıntı
             riski hem de (AAAA sorguları yüzünden) yavaş DNS demek.
        Artık değer geri okunur; kapanmadıysa görünür bir uyarı üretilir.
        """
        if self.dry:
            return
        targets = ["all", "default"] + ([iface] if iface else [])
        for scope in targets:
            self.engine._ns("sysctl", "-w",
                            f"net.ipv6.conf.{scope}.disable_ipv6=1", check=False)
        # Doğrula: 'all' kapandı mı? (arayüze özel değer de varsa kontrol edilir)
        ok = True
        for scope in targets:
            res = self.engine._ns("sysctl", "-n",
                                  f"net.ipv6.conf.{scope}.disable_ipv6", check=False)
            if not res.ok or res.out.strip() != "1":
                ok = False
                log.warning("IPv6 kapatılamadı: net.ipv6.conf.%s.disable_ipv6=%s",
                            scope, (res.out or "").strip() or "?")
        self._ipv6_disabled = ok
        if ok:
            log.info("IPv6 namespace içinde devre dışı bırakıldı (G-7)")
        else:
            self._event("warn", "IPv6 izole alanda kapatılamadı — DNS yavaşlayabilir "
                                "ve IPv6 trafiği izolasyon dışına çıkabilir.")

    # ----------------------------------------------- trafik sayacı (H-1)
    def _update_traffic_stats(self, iface: str) -> None:
        """İzole alandaki uplink arayüzünün rx/tx baytlarını oku, delta hesapla."""
        if self.dry:
            return
        try:
            res = self.engine._ns("ip", "-s", "-o", "link", "show", iface,
                                  check=False)
            if not res.ok:
                return
            # Çıktı: "3: usb0: ... stats ... RX: bytes 12345 ... TX: bytes 6789 ..."
            parts = res.out.split()
            rx_idx = None
            tx_idx = None
            for i, p in enumerate(parts):
                if p == "RX:":
                    rx_idx = i
                elif p == "TX:":
                    tx_idx = i
            delta_rx = delta_tx = 0
            if rx_idx is not None and len(parts) > rx_idx + 2:
                rx_bytes = int(parts[rx_idx + 2])
                if self._traffic_last_rx > 0:
                    d = rx_bytes - self._traffic_last_rx
                    if d >= 0:
                        delta_rx = d
                        self._traffic_session_rx += d
                self._traffic_last_rx = rx_bytes
            if tx_idx is not None and len(parts) > tx_idx + 2:
                tx_bytes = int(parts[tx_idx + 2])
                if self._traffic_last_tx > 0:
                    d = tx_bytes - self._traffic_last_tx
                    if d >= 0:
                        delta_tx = d
                        self._traffic_session_tx += d
                self._traffic_last_tx = tx_bytes
            self.state.traffic_rx = self._traffic_session_rx
            self.state.traffic_tx = self._traffic_session_tx

            now = time.time()
            if self._traffic_last_ts > 0:
                elapsed = now - self._traffic_last_ts
                if elapsed > 0:
                    self.state.traffic_rate_rx = delta_rx / elapsed
                    self.state.traffic_rate_tx = delta_tx / elapsed
            self._traffic_last_ts = now

            usage_data = usage.add(delta_rx, delta_tx)
            self.state.usage_today_rx, self.state.usage_today_tx = usage.today_bytes(usage_data)
            self.state.usage_month_rx, self.state.usage_month_tx = usage.month_bytes(usage_data)
            self._check_quota()
        except (ValueError, IndexError, OSError) as e:
            log.debug("trafik sayacı okunamadı: %s", e)

    # ----------------------------------------------- kota (veri tasarrufu)
    def _check_quota(self) -> None:
        prof = self._active_profile
        if not prof or not prof.data_saver.enabled or prof.data_saver.quota_mb <= 0:
            return
        month_key = time.strftime("%Y-%m")
        if self._quota_triggered_month != month_key:
            self._quota_triggered_month = month_key
            self.state.quota_warned = False
            self.state.quota_hit = False
        used_mb = (self.state.usage_month_rx + self.state.usage_month_tx) / 1_000_000
        pct = used_mb / prof.data_saver.quota_mb * 100
        if pct >= 100 and not self.state.quota_hit:
            self.state.quota_hit = True
            if prof.data_saver.quota_action == "killswitch":
                self._apply_kill_switch()
                self._event("error", f"Aylık veri kotası ({prof.data_saver.quota_mb} MB) "
                                     f"aşıldı; kill-switch etkinleştirildi.")
            else:
                self._event("error", f"Aylık veri kotası ({prof.data_saver.quota_mb} MB) aşıldı.")
        elif pct >= 80 and not self.state.quota_warned:
            self.state.quota_warned = True
            self._event("warn", f"Aylık veri kotasının %{pct:.0f}'i kullanıldı "
                                f"({used_mb:.0f}/{prof.data_saver.quota_mb} MB).")

    # ----------------------------------------------- önbellek (P-3)
    def _list_interfaces_cached(self) -> list:
        """Arayüz listesini kısa TTL ile önbelleğe al (P-3)."""
        now = time.time()
        if now < self._cached_interfaces_ttl:
            return self._cached_interfaces
        self._cached_interfaces = system.list_host_interfaces()
        self._cached_interfaces_ttl = now + 5  # 5 saniye TTL
        return self._cached_interfaces

    # --------------------------------------------------------------- durum
    def snapshot(self) -> dict:
        """Durumun immutable bir kopyasını döndürür (P-1)."""
        with self._lock:
            return copy.deepcopy(self.state.to_dict())
