"""İsteğe bağlı veth relay yan-kanalı.

İzolasyon her zaman açıktır. Relay, varsayılan KAPALI, anlık açılıp kapanabilen
denetimli bir kapıdır. AÇILDIĞINDA: izole alandaki uygulamalar, bu makinenin
(host) ethernet/yerel ağ üzerinden ULAŞTIĞI HER YERE erişir. İnternet ise
DEĞİŞMEZ — izole alanın kendi uplink'inde (tether/WiFi) kalır.

Yani "aç → hem telefon interneti (tether) hem de PC'nin ethernet ağı" birlikte
çalışır. Karmaşık kapsam/allowlist/doğrulama yok; tek düğme.

Mimari:
    [namespace]  tisor-ns <--veth--> tisor-host  [host]
    - host'un varsayılan-DIŞI tüm rotaları (ör. 10.0.15.0/24) izole alana aynalanır
    - bu rotalara giden ns trafiği host'ta MASQUERADE edilir
    - varsayılan rota (internet) HİÇ değişmez → internet tether'de kalır

Relay kapatıldığında veth çifti + NAT kuralları tamamen silinir → yine tam izole.
"""
from __future__ import annotations

import ipaddress
import logging

from . import system
from .config import RelayPolicy, Settings
from .engine import EngineError
from .system import run

log = logging.getLogger("tether.relay")

HOST_VETH = "tisor-host"
NS_VETH = "tisor-ns"
NFT_TABLE = "tisor_relay"
IPT_FWD = "TISOR_FWD"
IPT_NAT = "TISOR_NAT"


class Relay:
    def __init__(self, settings: Settings, *, dry_run: bool = False):
        self.s = settings
        self.ns = settings.namespace
        self.dry = dry_run
        self._ip_forward_was_enabled: bool | None = None  # B-4: eski değer

    def _ip(self, *a: str, check: bool = True):
        return run(["ip", *a], check=check, dry_run=self.dry)

    def _ns(self, *a: str, check: bool = True):
        return run(["ip", "netns", "exec", self.ns, *a], check=check, dry_run=self.dry)

    def _addrs(self, policy: RelayPolicy):
        net = ipaddress.ip_network(policy.host_subnet, strict=False)
        hosts = list(net.hosts())
        host_ip, ns_ip = hosts[0], hosts[1]
        prefix = net.prefixlen
        return f"{host_ip}/{prefix}", f"{ns_ip}/{prefix}", str(host_ip)

    # --------------------------------------------------------------------- aç
    def enable(self, policy: RelayPolicy) -> list[str]:
        """Relay kanalını açar. Aynalanan ağların listesini döndürür."""
        host_cidr, ns_cidr, host_ip = self._addrs(policy)
        ns_subnet = policy.host_subnet
        log.info("relay açılıyor")

        # 1) veth çifti (varsa temizle)
        self._ip("link", "del", HOST_VETH, check=False)
        self._ip("link", "add", HOST_VETH, "type", "veth", "peer", "name", NS_VETH)
        # 2) ns ucunu namespace'e taşı
        self._ip("link", "set", NS_VETH, "netns", self.ns)
        # 3) adresler + up
        self._ip("addr", "add", host_cidr, "dev", HOST_VETH)
        self._ip("link", "set", HOST_VETH, "up")
        self._ns("ip", "addr", "add", ns_cidr, "dev", NS_VETH)
        self._ns("ip", "link", "set", NS_VETH, "up")

        # 4) scope="lan" ise host'un ulaştığı ağları (varsayılan-DIŞI) izole alana
        #    rota olarak ekle. scope="custom" ise yalnızca kullanıcının elle
        #    eklediği hedefler aynalanır. Varsayılan rotaya DOKUNMAYIZ → internet
        #    tether'de kalır.
        routes = self._host_lan_routes() if getattr(policy, "scope", "lan") != "custom" else []
        # 4b) kullanıcının belirttiği EK hedefler (kurum-only dış adresler): alan
        #     adlarını çöz, IP/CIDR'ları normalize et; host üzerinden geçir.
        extra = self._resolve_extra(getattr(policy, "extra_targets", []))
        for net in extra:
            if net not in routes:
                routes.append(net)
        for net in routes:
            self._ns("ip", "route", "replace", net, "via", host_ip, check=False)

        # 5) host: yönlendirme + NAT (ns trafiğini LAN'a maskele)
        # B-4: mevcut ip_forward değerini oku, sadece biz açıyorsak kaydet
        if self._ip_forward_was_enabled is None:
            fwd_res = run(["sysctl", "-n", "net.ipv4.ip_forward"], check=False, dry_run=self.dry)
            self._ip_forward_was_enabled = fwd_res.ok and fwd_res.out.strip() == "1"
        if not self._ip_forward_was_enabled:
            run(["sysctl", "-w", "net.ipv4.ip_forward=1"], check=False, dry_run=self.dry)
        if system.have("nft"):
            self._nft_enable(ns_subnet)
        elif system.have("iptables"):
            self._iptables_enable(ns_subnet)
        # Docker kuruluysa kendi FORWARD/DOCKER-USER zinciri bizim ACCEPT
        # kuralımızın üzerine DROP yazabilir; ayrıca istisna ekle.
        self._docker_user_enable()

        # 6) Doğrulama: ns ucu gerçekten izole alanın İÇİNDE mi? (asıl kanıt)
        if not self.dry and not self._ns_veth_present():
            self._ip("link", "del", HOST_VETH, check=False)
            raise EngineError(
                f"Relay kanalı kurulamadı: '{NS_VETH}' arayüzü '{self.ns}' "
                f"namespace içinde görünmüyor. Genellikle art arda başlat/durdur "
                f"sonrası oluşan bayat namespace eşleşmesinden olur — oturumu "
                f"durdurup tek temiz oturum başlatmayı deneyin.")
        return routes

    # --------------------------------------------------------- rotaları tazele
    def reassert_routes(self, policy: RelayPolicy) -> list[str]:
        """Relay rotalarını (LAN + ek hedefler) namespace'te YENİDEN uygular.

        veth/NAT'a dokunmaz — sadece 'ip route replace ... via host_ip'
        satırlarını tekrar yazar. VPN istemcisi (ns İÇİNDE çalışır) kendi
        push route'larıyla relay'in ek-hedef rotalarının ÜZERİNE yazabilir
        (ör. sunucu aynı /24'e daha spesifik/geç bir rota pushlarsa); bu
        yüzden her VPN bağlan/yeniden-bağlan sonrası çağrılmalı ki kurum
        hedefleri host LAN üzerinden gitmeye devam etsin.
        """
        if not self.is_active():
            return []
        _, _, host_ip = self._addrs(policy)
        routes = self._host_lan_routes() if getattr(policy, "scope", "lan") != "custom" else []
        extra = self._resolve_extra(getattr(policy, "extra_targets", []))
        for net in extra:
            if net not in routes:
                routes.append(net)
        for net in routes:
            self._ns("ip", "route", "replace", net, "via", host_ip, check=False)
        return routes

    def remove_extra_target(self, policy: RelayPolicy, value: str) -> None:
        """Tek bir ek hedefin ns içindeki rota(larını) siler (best-effort).

        Kullanıcı bir hedefi listeden kaldırdığında relay hâlâ açıksa,
        `reassert_routes` yalnızca kalan hedefleri yeniden yazar; kaldırılanın
        eski rotası kendiliğinden silinmez. Bu yüzden reassert'ten ÖNCE
        çağrılmalı.
        """
        if not self.is_active():
            return
        for net in self._resolve_extra([value]):
            self._ns("ip", "route", "del", net, check=False)

    # ----------------------------------------------------- host'un ulaştığı ağlar
    def _host_lan_routes(self) -> list[str]:
        """Host'un ulaştığı ağlar: varsayılan rota HARİÇ tüm rotalar.

        Varsayılan (internet) hariç tutulur → izole alanın interneti tether'de
        kalır; yalnızca host'un doğrudan/özel rotayla ulaştığı ağlar (kurumsal
        alt ağlar dâhil) aynalanır: "makinenin eriştiği her yer".
        """
        if self.dry:
            return ["10.0.0.0/24"]
        res = run(["ip", "-4", "route", "show"], check=False, dry_run=self.dry)
        if not res.ok:
            return []
        veth_net = ipaddress.ip_network("10.77.0.0/30")   # relay yan-kanalı
        out: list[str] = []
        for line in res.out.splitlines():
            line = line.strip()
            if not line or line.startswith("default"):
                continue
            if f"dev {HOST_VETH}" in line:
                continue
            dest = line.split()[0]
            try:
                net = ipaddress.ip_network(dest, strict=False)
            except ValueError:
                continue
            if net.is_loopback or net.is_link_local or net.overlaps(veth_net):
                continue
            cidr = str(net)
            if cidr not in out:
                out.append(cidr)
        return out

    # ------------------------------------------------------- ek hedef çözümleme
    def _resolve_extra(self, targets: list[str]) -> list[str]:
        """IP/CIDR'ları normalize eder, alan adlarını A kayıtlarına çözer.

        Dönen her öğe host üzerinden yönlendirilecek bir /32 ya da CIDR'dir.
        Çözülemeyen ad atlanır (uyarı loglanır) — relay yine de açılır.
        """
        import socket
        out: list[str] = []

        def _add(cidr: str) -> None:
            if cidr not in out:
                out.append(cidr)

        for raw in targets or []:
            t = str(raw).strip()
            if not t:
                continue
            # Önce IP/CIDR mi?
            try:
                _add(str(ipaddress.ip_network(t, strict=False)))
                continue
            except ValueError:
                pass
            # Değilse alan adı → A kayıtları
            if self.dry:
                _add("203.0.113.1/32")   # test için sabit
                continue
            try:
                infos = socket.getaddrinfo(t, None, family=socket.AF_INET)
            except OSError as e:
                log.warning("ek hedef çözülemedi, atlanıyor: %s (%s)", t, e)
                continue
            for info in infos:
                _add(f"{info[4][0]}/32")
        return out

    # ------------------------------------------------------------------ NAT (nft)
    def _nft_enable(self, ns_subnet: str) -> None:
        run(["nft", "add", "table", "ip", NFT_TABLE], check=False, dry_run=self.dry)
        # NAT: ns kaynaklı trafiği veth-DIŞI arayüzlerde (yani LAN'a) maskele
        run(["nft", "add", "chain", "ip", NFT_TABLE, "post",
             "{ type nat hook postrouting priority 100 ; }"],
            check=False, dry_run=self.dry)
        run(["nft", "add", "rule", "ip", NFT_TABLE, "post",
             "ip", "saddr", ns_subnet, "oifname", "!=", HOST_VETH, "masquerade"],
            check=False, dry_run=self.dry)
        # FORWARD: veth trafiğine izin (host FORWARD politikası DROP olsa bile)
        run(["nft", "add", "chain", "ip", NFT_TABLE, "fwd",
             "{ type filter hook forward priority -10 ; }"],
            check=False, dry_run=self.dry)
        run(["nft", "add", "rule", "ip", NFT_TABLE, "fwd",
             "iifname", HOST_VETH, "accept"], check=False, dry_run=self.dry)
        run(["nft", "add", "rule", "ip", NFT_TABLE, "fwd",
             "oifname", HOST_VETH, "accept"], check=False, dry_run=self.dry)

    def _nft_disable(self) -> None:
        run(["nft", "delete", "table", "ip", NFT_TABLE], check=False, dry_run=self.dry)

    # ---------------------------------------------------------- Docker uyumu
    def _docker_user_present(self) -> bool:
        res = run(["iptables", "-S", "DOCKER-USER"], check=False, dry_run=self.dry)
        return res.ok

    def _docker_user_enable(self) -> None:
        """Docker'ın kendi FORWARD zincirini (policy DROP) atlat.

        Docker, kendi ağları DIŞINDAKİ tüm forward trafiğini reddeden bir
        FORWARD zinciri kurar (nft'te ayrı bir 'filter' tablosu, priority
        'filter' yani bizim tisor_relay'in -10'undan SONRA çalışır). Bizim
        ACCEPT kuralımız bu yüzden yetmez — Docker'ın kendisi için bıraktığı
        istisna noktası olan DOCKER-USER zincirine de veth trafiğini kabul
        eden bir kural eklemek gerekir; aksi halde relay LAN'a hiç ulaşamaz.
        """
        if not system.have("iptables") or not self._docker_user_present():
            return
        for args in (["-i", HOST_VETH], ["-o", HOST_VETH]):
            check_res = run(["iptables", "-C", "DOCKER-USER", *args, "-j", "ACCEPT"],
                             check=False, dry_run=self.dry)
            if not check_res.ok:
                run(["iptables", "-I", "DOCKER-USER", *args, "-j", "ACCEPT"],
                    check=False, dry_run=self.dry)

    def _docker_user_disable(self) -> None:
        if not system.have("iptables") or not self._docker_user_present():
            return
        for args in (["-i", HOST_VETH], ["-o", HOST_VETH]):
            run(["iptables", "-D", "DOCKER-USER", *args, "-j", "ACCEPT"],
                check=False, dry_run=self.dry)

    # ------------------------------------------------------------- NAT (iptables)
    def _iptables_enable(self, ns_subnet: str) -> None:
        # FORWARD: veth trafiğine izin (-I: politikadan önce)
        run(["iptables", "-I", "FORWARD", "-i", HOST_VETH, "-j", "ACCEPT"],
            check=False, dry_run=self.dry)
        run(["iptables", "-I", "FORWARD", "-o", HOST_VETH, "-j", "ACCEPT"],
            check=False, dry_run=self.dry)
        # NAT: ns kaynağını veth-dışı çıkışta maskele
        run(["iptables", "-t", "nat", "-A", "POSTROUTING",
             "-s", ns_subnet, "!", "-o", HOST_VETH, "-j", "MASQUERADE"],
            check=False, dry_run=self.dry)

    def _iptables_cleanup(self) -> None:
        run(["iptables", "-D", "FORWARD", "-i", HOST_VETH, "-j", "ACCEPT"],
            check=False, dry_run=self.dry)
        run(["iptables", "-D", "FORWARD", "-o", HOST_VETH, "-j", "ACCEPT"],
            check=False, dry_run=self.dry)
        # yeni kural (kaynak-kapsamlı) + eski full-mod kalıntısı (kaynaksız)
        for extra in (["-s", "10.77.0.0/30"], []):
            run(["iptables", "-t", "nat", "-D", "POSTROUTING", *extra,
                 "!", "-o", HOST_VETH, "-j", "MASQUERADE"],
                check=False, dry_run=self.dry)
        # eski lan-mod özel zincir kalıntıları (varsa)
        run(["iptables", "-D", "FORWARD", "-i", HOST_VETH, "-j", IPT_FWD],
            check=False, dry_run=self.dry)
        run(["iptables", "-F", IPT_FWD], check=False, dry_run=self.dry)
        run(["iptables", "-X", IPT_FWD], check=False, dry_run=self.dry)
        run(["iptables", "-t", "nat", "-D", "POSTROUTING", "-j", IPT_NAT],
            check=False, dry_run=self.dry)
        run(["iptables", "-t", "nat", "-F", IPT_NAT], check=False, dry_run=self.dry)
        run(["iptables", "-t", "nat", "-X", IPT_NAT], check=False, dry_run=self.dry)

    # ------------------------------------------------------------------- kapat
    def disable(self) -> None:
        """Relay kanalını tamamen kaldırır → yeniden tam izolasyon."""
        log.info("relay kapatılıyor")
        self._docker_user_disable()
        if system.have("nft"):
            self._nft_disable()
        elif system.have("iptables"):
            self._iptables_cleanup()
        self._ip("link", "del", HOST_VETH, check=False)
        # B-4: ip_forward'ı eski haline döndür (biz açtıysak)
        if self._ip_forward_was_enabled is not None and not self._ip_forward_was_enabled:
            run(["sysctl", "-w", "net.ipv4.ip_forward=0"], check=False, dry_run=self.dry)
        self._ip_forward_was_enabled = None

    # ------------------------------------------------------------------- durum
    def _ns_veth_present(self) -> bool:
        """ns-ucu veth gerçekten namespace İÇİNDE mi? (relay'in asıl kanıtı)."""
        res = run(["ip", "netns", "exec", self.ns, "ip", "-o", "link", "show", NS_VETH],
                  check=False, dry_run=self.dry)
        return res.ok and NS_VETH in res.out

    def is_active(self) -> bool:
        """Relay gerçekten kurulu mu? Hem host hem ns ucu doğrulanır."""
        host = run(["ip", "-o", "link", "show", HOST_VETH],
                   check=False, dry_run=self.dry)
        host_ok = host.ok and HOST_VETH in host.out
        return host_ok and self._ns_veth_present()
