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

from . import apps, system
from .config import Profile, Settings, VPNS_DIR
from .engine import Engine, EngineError
from .relay import Relay
from .state import AppProcess, RuntimeState
from .vpn import VPN

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
            )
            self._event("info", f"Oturum başlatılıyor: profil={profile.name} uplink={uplink}")

        try:
            self.engine.ensure_namespace(profile)

            # IPv6 sızıntı kapatma (G-7)
            self._disable_ipv6_in_ns()

            # Uplink'i izole alana al
            self._bring_uplink_into_ns(uplink)

            with self._lock:
                self._event("info", "IP adresi alınıyor (DHCP)...")
            self.engine.start_dhcp(uplink)
        except Exception as e:
            with self._lock:
                self._event("error", f"Başlatılamadı: {e}")
                try:
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
                    self.state.apps.append(AppProcess(command=prog, pid=pid))
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
            self.state.relay_scope = "lan"
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
            )
            # Önceki uygulama kayıtlarından hâlâ yaşayanları geri al.
            for a in prev.get("apps", []):
                if apps.pid_alive(a.get("pid", 0)):
                    self.state.apps.append(
                        AppProcess(command=a.get("command", "?"), pid=a["pid"]))
            self._event("info", f"Mevcut oturum devralındı (uplink={uplink}); "
                                f"kaldığın yerden devam.")
            self._refresh_network()
            self.state.relay_active = self.relay.is_active()
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

    # ---------------------------------------------------------- canlı işlemler
    def launch_app(self, program: str) -> int:
        with self._lock:
            if self.state.phase in ("idle", "stopping"):
                raise EngineError("Etkin oturum yok; önce başlatın.")
            prof = self._active_profile
            assert prof
            pid = apps.launch(self.s, prof, program, dry_run=self.dry)
            self.state.apps.append(AppProcess(command=program, pid=pid))
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

            self.state.phase = "reconnecting"
            self.state.persist()

            # 1) Eski uplink'i host'a iade et
            if old and not self.dry:
                try:
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
        while not self._wd_stop.wait(self.s.watchdog_interval):
            try:
                iface = self._active_iface
                if iface:
                    self._tick(iface)
            except Exception as e:  # noqa: BLE001
                log.warning("watchdog tick hatası: %s", e)

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
            self.state.public_ip = self.engine.public_ip()

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
    def _disable_ipv6_in_ns(self) -> None:
        """Namespace içinde IPv6'yı devre dışı bırak."""
        if self.dry or self._ipv6_disabled:
            return
        try:
            self.engine._ns("sysctl", "-w", "net.ipv6.conf.all.disable_ipv6=1",
                            check=False)
            self.engine._ns("sysctl", "-w", "net.ipv6.conf.default.disable_ipv6=1",
                            check=False)
            self._ipv6_disabled = True
            log.info("IPv6 namespace içinde devre dışı bırakıldı (G-7)")
        except Exception as e:  # noqa: BLE001
            log.warning("IPv6 kapatılamadı: %s", e)

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
            if rx_idx is not None and len(parts) > rx_idx + 2:
                rx_bytes = int(parts[rx_idx + 2])
                if self._traffic_last_rx > 0:
                    delta = rx_bytes - self._traffic_last_rx
                    if delta >= 0:
                        self._traffic_session_rx += delta
                self._traffic_last_rx = rx_bytes
            if tx_idx is not None and len(parts) > tx_idx + 2:
                tx_bytes = int(parts[tx_idx + 2])
                if self._traffic_last_tx > 0:
                    delta = tx_bytes - self._traffic_last_tx
                    if delta >= 0:
                        self._traffic_session_tx += delta
                self._traffic_last_tx = tx_bytes
            self.state.traffic_rx = self._traffic_session_rx
            self.state.traffic_tx = self._traffic_session_tx
        except (ValueError, IndexError, OSError) as e:
            log.debug("trafik sayacı okunamadı: %s", e)

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
