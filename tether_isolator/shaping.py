"""Bant genişliği tavanı (Faz 4) — tc/ifb, tamamen namespace içi.

İzolasyon zaten fiziksel (Model B); bu modül host'a hiç dokunmaz — tüm
`tc`/`ip link` komutları `ip netns exec` ile namespace İÇİNDE çalışır. Asıl
kaldıraç UPLOAD değil DOWNLOAD tarafıdır: tarayıcı/video akışları uyarlanabilir
bitrate kullandığından tavan konunca otomatik olarak daha düşük kaliteye iner.

Yöntem:
  - Upload (egress): doğrudan uplink arayüzünde `tbf`.
  - Download (ingress): Linux'ta ingress qdisc'e doğrudan shape uygulanamaz;
    IFB (Intermediate Functional Block) sanal arayüzüne `mirred` ile
    yönlendirilip orada `cake` (yoksa `tbf`) ile şekillendirilir. IFB modülü
    yoksa ya da kurulamazsa, kaba ama çalışan bir düşürme (`police ... drop`)
    doğrudan ingress'te uygulanır — dalgalanır ama tavanı aşırtmaz.

Yaşam döngüsü bilgisi (ne zaman uygula/kaldır) burada YOK; bunu `manager`
yönetir — çünkü bir arayüz namespace'e her giriş/çıkışında (uplink switch,
watchdog reconnect, oturum durdurma) qdisc'lerin temizlenmesi/yeniden
uygulanması gerekir (aksi halde host'a dönen arayüz tavanı SÜRÜKLEYEBİLİR).
"""
from __future__ import annotations

import logging

from .system import run

log = logging.getLogger("tether.shaping")

IFB_DEV = "tisor-ifb0"


def _ns(ns: str, *args: str):
    return run(["ip", "netns", "exec", ns, *args], check=False)


def apply(ns: str, iface: str, *, down_kbit: int = 0, up_kbit: int = 0,
         dry_run: bool = False) -> dict:
    """Verilen arayüze indirme/yükleme tavanı uygular; sonucu döndürür.

    Her zaman önce `remove()` çağırır (idempotent yeniden uygulama) — böylece
    seviye/tavan değişince eski kurallar kalıntı bırakmaz.
    """
    if dry_run:
        return {"download_method": "cake" if down_kbit > 0 else "",
                "upload_applied": up_kbit > 0}
    remove(ns, iface, dry_run=False)
    result = {"download_method": "", "upload_applied": False}

    if up_kbit > 0:
        res = _ns(ns, "tc", "qdisc", "add", "dev", iface, "root", "handle", "1:",
                  "tbf", "rate", f"{up_kbit}kbit", "burst", "32kbit", "latency", "400ms")
        result["upload_applied"] = res.ok
        if not res.ok:
            log.warning("yükleme tavanı uygulanamadı (%s): %s", iface, res.err.strip())

    if down_kbit > 0:
        result["download_method"] = _apply_download_cap(ns, iface, down_kbit)
        if not result["download_method"]:
            log.warning("indirme tavanı hiçbir yöntemle uygulanamadı (%s)", iface)

    return result


def _apply_download_cap(ns: str, iface: str, down_kbit: int) -> str:
    link_res = _ns(ns, "ip", "link", "add", IFB_DEV, "type", "ifb")
    if not link_res.ok:
        log.info("ifb kurulamadı (%s); ingress policing'e düşülüyor", iface)
        return _apply_ingress_police(ns, iface, down_kbit)

    _ns(ns, "ip", "link", "set", IFB_DEV, "up")
    _ns(ns, "tc", "qdisc", "add", "dev", iface, "handle", "ffff:", "ingress")
    filt = _ns(ns, "tc", "filter", "add", "dev", iface, "parent", "ffff:",
              "protocol", "all", "u32", "match", "u32", "0", "0",
              "action", "mirred", "egress", "redirect", "dev", IFB_DEV)
    if not filt.ok:
        log.warning("ifb yönlendirme kurulamadı (%s); ingress policing'e düşülüyor", iface)
        _cleanup_ifb(ns, iface)
        return _apply_ingress_police(ns, iface, down_kbit)

    cake = _ns(ns, "tc", "qdisc", "add", "dev", IFB_DEV, "root", "cake",
              "bandwidth", f"{down_kbit}kbit", "besteffort")
    if cake.ok:
        return "cake"

    tbf = _ns(ns, "tc", "qdisc", "add", "dev", IFB_DEV, "root", "tbf",
             "rate", f"{down_kbit}kbit", "burst", "32kbit", "latency", "400ms")
    if tbf.ok:
        return "tbf"

    log.warning("ifb üzerinde qdisc kurulamadı (%s); ingress policing'e düşülüyor", iface)
    _cleanup_ifb(ns, iface)
    return _apply_ingress_police(ns, iface, down_kbit)


def _apply_ingress_police(ns: str, iface: str, down_kbit: int) -> str:
    _ns(ns, "tc", "qdisc", "add", "dev", iface, "handle", "ffff:", "ingress")
    res = _ns(ns, "tc", "filter", "add", "dev", iface, "parent", "ffff:",
             "protocol", "ip", "prio", "1", "u32", "match", "u32", "0", "0",
             "police", "rate", f"{down_kbit}kbit", "burst", "32k", "drop", "flowid", ":1")
    if not res.ok:
        log.warning("ingress policing de kurulamadı (%s): %s", iface, res.err.strip())
        return ""
    return "police"


def _cleanup_ifb(ns: str, iface: str) -> None:
    """Yarım kalmış ifb kurulumunu temizler (police fallback'e temiz zemin bırakır)."""
    _ns(ns, "tc", "qdisc", "del", "dev", iface, "ingress")
    _ns(ns, "tc", "qdisc", "del", "dev", IFB_DEV, "root")
    _ns(ns, "ip", "link", "del", IFB_DEV)


def remove(ns: str, iface: str, *, dry_run: bool = False) -> None:
    """Arayüzdeki TÜM tavan kurallarını kaldırır (best-effort, idempotent).

    Uplink namespace'ten çıkmadan (host'a/ başka bir yere geri taşınmadan)
    ÖNCE mutlaka çağrılmalı — aksi halde arayüz üzerindeki qdisc konfigürasyonu
    host'un gerçek bağlantısına sürüklenebilir.
    """
    if dry_run:
        return
    _ns(ns, "tc", "qdisc", "del", "dev", iface, "root")
    _ns(ns, "tc", "qdisc", "del", "dev", iface, "ingress")
    _ns(ns, "tc", "qdisc", "del", "dev", IFB_DEV, "root")
    _ns(ns, "ip", "link", "del", IFB_DEV)


def is_active(ns: str, iface: str) -> bool:
    """Arayüzde HÂLÂ bir tavan qdisc'i kurulu mu? (izleme/uzlaştırma için)."""
    res = _ns(ns, "tc", "qdisc", "show", "dev", iface)
    if not res.ok:
        return False
    return "tbf" in res.out or "ingress" in res.out
