"""Çekirdek motor — network namespace yaşam döngüsü (Model B).

Yaklaşım: seçilen uplink arayüzü *fiziksel olarak* namespace'in içine taşınır.
Bu, host'un kablolu bağlantısıyla ortak hiçbir yönlendirme/kernel yığını
paylaşılmaması demektir → yapısal, sızdırmaz izolasyon.

Bu modüldeki `ip netns ...` komut dizisi, orijinal `tether_isolator_v2.sh`
ile birebir aynı mantığı izler; yalnızca Python'a taşınmış, hata yönetimi ve
durum raporlaması eklenmiştir.
"""
from __future__ import annotations

import logging
import os
import shutil
import time

from . import system
from .config import Profile, Settings
from .system import run

log = logging.getLogger("tether.engine")


class EngineError(RuntimeError):
    pass


class Engine:
    def __init__(self, settings: Settings, *, dry_run: bool = False):
        self.s = settings
        self.ns = settings.namespace
        self.dry = dry_run

    # ----- yardımcı -----
    def _ns(self, *args: str, check: bool = True, timeout: int = 30):
        return run(["ip", "netns", "exec", self.ns, *args],
                   check=check, timeout=timeout, dry_run=self.dry)

    def _ip(self, *args: str, check: bool = True):
        return run(["ip", *args], check=check, dry_run=self.dry)

    # ----- namespace -----
    def ensure_namespace(self, profile: Profile) -> None:
        """Namespace + loopback + DNS hazırlar (yoksa oluşturur)."""
        if not system.namespace_exists(self.ns):
            log.info("namespace oluşturuluyor: %s", self.ns)
            self._ip("netns", "add", self.ns)
        self._ns("ip", "link", "set", "dev", "lo", "up")
        self._write_resolv(profile.dns)

    # glibc, A ve AAAA sorgularını VARSAYILAN olarak aynı kaynak porttan
    # paralel gönderir. Telefon hotspot'ları/basit NAT'lar (ör. iPhone Personal
    # Hotspot — 172.20.10.0/28) ikinci yanıtı sık sık düşürür; resolver o zaman
    # tam timeout (5 sn) bekler. Sonuç: bağlantı "çalışıyor" ama HER isim
    # çözümlemesi ~5 sn sürer → tarayıcılar internet yokmuş gibi davranır.
    # (Ölçüm: A tek başına 0,09 sn · AAAA tek başına 0,12 sn · ikisi 5,64 sn.)
    #
    # `single-request-reopen` glibc'ye iki sorgu için AYRI soket kullandırır ve
    # bu çakışmayı tamamen ortadan kaldırır. `timeout:2` ise başka bir nedenle
    # paket kaybolursa beklemeyi 5 sn yerine 2 sn ile sınırlar (emniyet ağı).
    _RESOLV_OPTIONS = ("single-request-reopen", "timeout:2", "attempts:2")

    def _write_resolv(self, dns: list[str]) -> None:
        path = f"/etc/netns/{self.ns}"
        if self.dry:
            log.info("[dry] resolv.conf -> %s : %s", path, dns)
            return
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "resolv.conf"), "w") as f:
            for ns in dns:
                f.write(f"nameserver {ns}\n")
            f.write(f"options {' '.join(self._RESOLV_OPTIONS)}\n")

    # ----- uplink taşıma -----
    def move_uplink_in(self, iface: str) -> None:
        """Fiziksel arayüzü namespace içine taşır ve UP yapar."""
        if system.interface_in_namespace(self.ns, iface):
            log.debug("%s zaten %s içinde", iface, self.ns)
        else:
            log.info("uplink taşınıyor: %s -> %s", iface, self.ns)
            self._ip("link", "set", iface, "netns", self.ns)
        self._ns("ip", "link", "set", "dev", iface, "up")

    def move_uplink_out(self, iface: str) -> None:
        """Arayüzü host (ana, netns id 1) namespace'ine geri taşır."""
        if not system.namespace_exists(self.ns):
            return
        if system.interface_in_namespace(self.ns, iface):
            log.info("uplink host'a geri taşınıyor: %s", iface)
            self._ns("ip", "link", "set", iface, "netns", "1", check=False)

    # ----- DHCP -----
    def start_dhcp(self, iface: str) -> None:
        """dhcpcd'yi namespace içinde (arka planda, kira yenilemeli) başlatır."""
        if system.have("dhcpcd"):
            # -w: ilk kira alınana kadar bekle; arka planda kalıp lease yeniler
            self._ns("dhcpcd", "-w", iface, check=False, timeout=45)
        elif system.have("udhcpc"):
            self._ns("udhcpc", "-i", iface, "-b", check=False, timeout=45)
        else:
            raise EngineError("DHCP istemcisi bulunamadı (dhcpcd/udhcpc).")

    def stop_dhcp(self, iface: str) -> None:
        if system.have("dhcpcd"):
            self._ns("dhcpcd", "-x", iface, check=False)

    # ----- WiFi (uplink WiFi ise) -----
    def wifi_associate(self, iface: str, ssid: str, password: str) -> None:
        """Namespace içinde wpa_supplicant ile WiFi'ye bağlanır.

        Not: WiFi PHY'ın namespace'e taşınması gerekir; bunu move_wifi_phy_in
        yapar. Bu adım gerçek donanım gerektirir.
        """
        if not system.have("wpa_supplicant"):
            raise EngineError("wpa_supplicant bulunamadı; WiFi uplink kullanılamaz.")
        conf = f"/etc/netns/{self.ns}/wpa_{iface}.conf"
        if not self.dry:
            os.makedirs(os.path.dirname(conf), exist_ok=True)
            gen = run(["wpa_passphrase", ssid, password], check=True)
            with open(conf, "w") as f:
                f.write(gen.out)
            os.chmod(conf, 0o600)
        # Aynı arayüzde artık bir wpa_supplicant varsa yenisi başlamaz → temizle.
        self._ns("pkill", "-f", f"wpa_supplicant.*{iface}", check=False)
        # nl80211 sürücüsünü açıkça ver (bazı ortamlarda otomatik algılama kaçırır).
        self._ns("wpa_supplicant", "-B", "-D", "nl80211", "-i", iface, "-c", conf,
                 check=False)

    def wifi_associated(self, iface: str) -> bool:
        """WiFi arayüzü bir erişim noktasına bağlandı mı?"""
        if self.dry:
            return True
        res = self._ns("iw", "dev", iface, "link", check=False)
        return res.ok and "Connected to" in res.out

    def wait_wifi_associated(self, iface: str, timeout: int = 20) -> bool:
        """Association tamamlanana kadar bekler (DHCP'den ÖNCE gerekli).

        wpa_supplicant -B asenkron çalışır; hemen DHCP başlatmak taşıyıcı
        gelmeden yapılan başarısız bir denemeye yol açar. Bu yüzden bekleriz.
        """
        if self.dry:
            return True
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.wifi_associated(iface):
                return True
            time.sleep(1)
        return False

    def move_wifi_phy_in(self, iface: str) -> None:
        """WiFi PHY'ı namespace'e taşır (wlan arayüzleri doğrudan taşınamaz)."""
        if self.dry:
            log.info("[dry] WiFi PHY taşınırdı: %s", iface)
            return
        phy = system.detect_wifi_phy(iface)
        if not phy:
            raise EngineError(f"{iface} için WiFi PHY bulunamadı.")
        if not system.have("iw"):
            raise EngineError(
                "WiFi için 'iw' aracı gerekli ama kurulu değil. WiFi uplink, "
                "kablosuz PHY'ı izole alana taşımak için 'iw' kullanır. Kurmak "
                "için: sudo apt install iw  (USB tether bunu gerektirmez).")
        # Modern iw (>=4.x) namespace'i İSİMLE alır: /run/netns/<ns>.
        # Eski yöntem ('sh -c echo $$' ile geçici PID) yanlıştı; o süreç
        # iw çalışmadan önce ölüyordu → "set netns <ölü-pid>" hata veriyordu.
        res = run(["iw", "phy", phy, "set", "netns", "name", self.ns],
                  check=False, dry_run=self.dry)
        if not res.ok:
            raise EngineError(
                f"WiFi PHY '{phy}' izole alana taşınamadı: "
                f"{(res.err or res.out).strip() or f'iw çıkış kodu {res.code}'}")

    def move_wifi_phy_out(self, iface: str) -> None:
        """WiFi PHY'ı namespace'ten host (PID 1) kök ağ ns'ine geri taşır.

        Canlı uplink değişiminde, eski WiFi uplink'ini namespace'i silmeden
        host'a iade etmek için kullanılır (uygulamalar yaşamaya devam eder).
        """
        if self.dry:
            log.info("[dry] WiFi PHY host'a geri taşınırdı: %s", iface)
            return
        # phy adını namespace İÇİNDEN öğren (iface artık orada).
        info = self._ns("iw", "dev", iface, "info", check=False)
        phy = ""
        if info.ok:
            for line in info.out.splitlines():
                s = line.strip()
                if s.startswith("wiphy"):
                    phy = "phy" + s.split()[1]
                    break
        if not phy:
            log.warning("%s için WiFi PHY adı bulunamadı; geri taşıma atlanıyor", iface)
            return
        # PID 1 = host kök ağ namespace'i
        self._ns("iw", "phy", phy, "set", "netns", "1", check=False)

    # ----- bağlantı / durum -----
    def connectivity(self) -> bool:
        if self.dry:
            return True
        # 3 paket gönder, EN AZ BİRİ dönerse çevrimiçi say. Tek pinge güvenmek,
        # jitter'lı WiFi'de (paket arada gecikince) yanlış "internet kesildi"
        # alarmına ve gereksiz DHCP yenilemesine yol açıyordu.
        res = self._ns("ping", "-c", "3", "-W", "2", "8.8.8.8", check=False, timeout=10)
        return res.ok

    def probe(self, hosts: list[str]) -> bool:
        """İzole alandan verilen IP'lerden EN AZ BİRİNE ulaşılıyor mu?

        Relay doğrulaması için: kurum 8.8.8.8'i engellediğinde erişilebilir
        kurumsal adreslerle (ör. DNS 10.150.0.5) teyit sağlar.
        """
        if self.dry:
            return True
        for h in hosts:
            h = str(h).strip()
            if not h:
                continue
            res = self._ns("ping", "-c", "2", "-W", "2", h, check=False, timeout=8)
            if res.ok:
                return True
        return False

    def uplink_ip(self, iface: str) -> tuple[str, str]:
        """(ip, gateway) — namespace içindeki adres bilgisi."""
        if self.dry:
            return ("10.42.0.7/24", "10.42.0.1")
        ip = ""
        res = self._ns("ip", "-o", "-4", "addr", "show", iface, check=False)
        if res.ok:
            for tok in res.out.split():
                if "/" in tok and tok.count(".") == 3:
                    ip = tok
                    break
        gw = ""
        r2 = self._ns("ip", "-4", "route", "show", "default", check=False)
        if r2.ok and "via" in r2.out:
            gw = r2.out.split("via", 1)[1].split()[0]
        return ip, gw

    def public_ip(self) -> str:
        """İzole alanın dış (genel) IP'si — host'unkinden farklı olmalı."""
        if self.dry:
            return "203.0.113.42"
        for cmd in (["curl", "-s", "--max-time", "5", "https://api.ipify.org"],
                    ["wget", "-qO-", "--timeout=5", "https://api.ipify.org"]):
            if system.have(cmd[0]):
                res = self._ns(*cmd, check=False, timeout=8)
                if res.ok and res.out.strip():
                    return res.out.strip()
        return ""

    # ----- yıkım -----
    def teardown(self, iface: str | None) -> None:
        """Her şeyi eski haline döndürür: DHCP durdur, arayüzü geri taşı, ns sil."""
        if iface:
            self.stop_dhcp(iface)
            if system._classify(iface) == "wifi":
                self.move_wifi_phy_out(iface)
            else:
                self.move_uplink_out(iface)
        if system.namespace_exists(self.ns):
            self._ip("netns", "del", self.ns, check=False)
        netns_etc = f"/etc/netns/{self.ns}"
        if not self.dry and os.path.isdir(netns_etc):
            shutil.rmtree(netns_etc, ignore_errors=True)
