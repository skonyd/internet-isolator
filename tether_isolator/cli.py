"""Komut satırı arabirimi.

Hem scriptlenebilir alt komutlar (start/stop/status/gui) hem de orijinal
script gibi etkileşimli bir akış (`interactive`) sunar.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from . import __version__, apps, system
from .config import Settings
from .manager import Manager
from .state import RuntimeState

C = {"red": "\033[0;31m", "green": "\033[0;32m", "yellow": "\033[1;33m",
     "blue": "\033[0;36m", "dim": "\033[2m", "nc": "\033[0m"}


def _c(color: str, text: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"{C[color]}{text}{C['nc']}"


def _need_root() -> None:
    if not system.is_root():
        print(_c("red", "Bu işlem için root gerekir. 'sudo' ile çalıştırın."))
        sys.exit(1)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


# --------------------------------------------------------------- komutlar
def cmd_gui(args, settings: Settings) -> int:
    from . import server
    if not args.dry_run:
        _need_root()
    server.serve(settings, dry_run=args.dry_run, open_browser=not args.no_browser)
    return 0


def cmd_interfaces(args, settings: Settings) -> int:
    for i in system.list_host_interfaces():
        tag = _c("green", "UP") if i.state == "UP" else _c("dim", i.state)
        print(f"  {i.name:<12} {i.kind:<9} {tag}  {i.mac}")
    return 0


def cmd_apps(args, settings: Settings) -> int:
    found = apps.discover_installed()
    print("Yüklü uygulamalar:" if found else "Bilinen uygulama bulunamadı.")
    for a in found:
        print(f"  • {a}")
    return 0


def cmd_status(args, settings: Settings) -> int:
    data = RuntimeState.read()
    if not data:
        print(_c("dim", "Etkin oturum/durum bilgisi yok."))
        return 0
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    phase = data.get("phase", "?")
    color = {"online": "green", "degraded": "yellow", "reconnecting": "yellow",
             "idle": "dim"}.get(phase, "blue")
    print(f"Durum     : {_c(color, phase)}")
    print(f"Profil    : {data.get('profile') or '-'}")
    print(f"Uplink    : {data.get('uplink') or '-'}  "
          f"({'var' if data.get('uplink_present') else 'yok'})")
    print(f"IP        : {data.get('ip_address') or '-'}")
    print(f"İnternet  : {'evet' if data.get('online') else 'hayır'}")
    print(f"Dış IP    : {data.get('public_ip') or '-'}")
    print(f"Yeniden   : {data.get('reconnect_count', 0)} kez bağlanıldı")
    print(f"Relay     : {'AÇIK ('+data.get('relay_scope','')+')' if data.get('relay_active') else 'kapalı'}")
    return 0


def cmd_doctor(args, settings: Settings) -> int:
    """Ortam sağlık kontrolü: gerçek çalıştırma için gereken araçlar + tuzaklar."""
    ok_mark = _c("green", "✔"); warn_mark = _c("yellow", "▲"); bad_mark = _c("red", "✖")
    problems = 0

    def check(label, present, *, required=True, hint=""):
        nonlocal problems
        if present:
            print(f"  {ok_mark} {label}")
        elif required:
            problems += 1
            print(f"  {bad_mark} {label} — EKSİK. {hint}")
        else:
            print(f"  {warn_mark} {label} — yok (isteğe bağlı). {hint}")

    print(_c("blue", "Çekirdek (zorunlu):"))
    check("ip (iproute2)", system.have("ip"), hint="sudo apt install iproute2")
    check("runuser", system.have("runuser"), hint="util-linux")
    check("DHCP istemcisi (dhcpcd/udhcpc)",
          system.have("dhcpcd") or system.have("udhcpc"),
          hint="sudo apt install dhcpcd-base")
    check("pgrep", system.have("pgrep"), hint="procps")

    print(_c("blue", "WiFi (WiFi uplink için):"))
    check("iw", system.have("iw"), required=False, hint="sudo apt install iw")
    check("wpa_supplicant", system.have("wpa_supplicant"), required=False,
          hint="sudo apt install wpasupplicant")
    check("wpa_passphrase", system.have("wpa_passphrase"), required=False,
          hint="wpasupplicant")

    print(_c("blue", "Relay (host erişimi için):"))
    check("nft veya iptables", system.have("nft") or system.have("iptables"),
          required=False, hint="sudo apt install nftables")

    print(_c("blue", "Dış IP doğrulama (isteğe bağlı):"))
    check("curl veya wget", system.have("curl") or system.have("wget"),
          required=False)

    print(_c("blue", "Ağ namespace desteği:"))
    check("/run/netns yazılabilir (root)", system.is_root(), required=False,
          hint="gerçek çalıştırma root ister")

    print(_c("blue", "DNS (systemd-resolved tuzağı):"))
    import os as _os
    resolv = "/etc/resolv.conf"
    if _os.path.islink(resolv):
        target = _os.path.realpath(resolv)
        if "systemd" in target or "127.0.0.53" in _read_head(resolv):
            print(f"  {warn_mark} /etc/resolv.conf → {target} (systemd-resolved). "
                  f"İzole ns'te 127.0.0.53 erişilemez; uygulama başlatıcı bunu "
                  f"otomatik aşar (resolv.conf bind). Bilgi amaçlı.")
        else:
            print(f"  {ok_mark} /etc/resolv.conf → {target}")
    else:
        print(f"  {ok_mark} /etc/resolv.conf düz dosya")

    print()
    if problems:
        print(_c("red", f"{problems} zorunlu bileşen eksik — yukarıdaki komutlarla kurun."))
        return 1
    print(_c("green", "Zorunlu bileşenler tamam. Sistem çalıştırmaya hazır."))
    return 0


def _read_head(path: str, n: int = 400) -> str:
    try:
        with open(path) as f:
            return f.read(n)
    except OSError:
        return ""


def cmd_start(args, settings: Settings) -> int:
    _need_root()
    prof = settings.profile(args.profile)
    uplink = args.uplink or prof.uplink
    if not uplink:
        print(_c("red", "Uplink belirtin: --uplink <arayüz>"))
        return 2
    if args.app:
        prof.apps = args.app
    prof.uplink = uplink
    settings.active_profile = prof.name
    settings.save()
    m = Manager(settings, dry_run=args.dry_run)
    _setup_logging(args.verbose)
    m.start_session(prof, uplink)
    print(_c("green", "Oturum başlatıldı.") + " Durdurmak için: tetherctl stop")
    # B-3: watchdog thread'ini ön planda canlı tut (sonsuz döngü yerine)
    try:
        if m._wd_thread and m._wd_thread.is_alive():
            m._wd_thread.join()
        else:
            import signal as _sig
            _sig.pause()  # sinyal bekle (Ctrl+C)
    except KeyboardInterrupt:
        m.stop_session()
    return 0


def cmd_stop(args, settings: Settings) -> int:
    _need_root()
    m = Manager(settings, dry_run=args.dry_run)
    m.engine.teardown(None)  # kalıntı temizliği
    # etkin arayüzü durumdan oku
    data = RuntimeState.read() or {}
    if data.get("uplink"):
        m.engine.teardown(data["uplink"])
    print(_c("green", "Temizlendi."))
    return 0


def cmd_interactive(args, settings: Settings) -> int:
    """Orijinal scriptin etkileşimli akışı (uplink + uygulama seçimi)."""
    _need_root()
    _setup_logging(args.verbose)
    print(_c("green", "--- Tether Isolator (etkileşimli) ---"))
    ifaces = system.list_host_interfaces()
    if not ifaces:
        print(_c("red", "Ağ arayüzü bulunamadı.")); return 1
    print("Uplink arayüzünü seçin:")
    for i, itf in enumerate(ifaces, 1):
        print(f"  {i}) {itf.name} ({itf.kind})")
    sel = input(f"Seçiminiz (1-{len(ifaces)}): ").strip()
    try:
        uplink = ifaces[int(sel) - 1].name
    except (ValueError, IndexError):
        print(_c("red", "Geçersiz seçim.")); return 1

    found = apps.discover_installed()
    print("Başlatılacak uygulamalar (örn: 1 3):")
    for i, a in enumerate(found, 1):
        print(f"  {i}) {a}")
    sel = input("Seçiminiz: ").split()
    chosen = [found[int(x) - 1] for x in sel if x.isdigit() and 1 <= int(x) <= len(found)]

    prof = settings.profile()
    prof.uplink, prof.apps = uplink, chosen
    settings.save()
    m = Manager(settings, dry_run=args.dry_run)
    m.start_session(prof, uplink)
    print(_c("yellow", "Çalışıyor. Ctrl+C ile durdurun."))
    try:
        import time
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        m.stop_session()
    return 0


# ------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tetherctl",
        description="Tether Isolator — uygulamaları izole bir ağ alanında çalıştır.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--dry-run", action="store_true",
                   help="komutları çalıştırmadan dene (root gerekmez)")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gui", help="web arayüzünü başlat")
    g.add_argument("--no-browser", action="store_true")
    g.set_defaults(func=cmd_gui)

    s = sub.add_parser("start", help="oturum başlat")
    s.add_argument("--profile", default=None)
    s.add_argument("--uplink", default=None)
    s.add_argument("--app", action="append", help="başlatılacak uygulama (tekrarlanabilir)")
    s.set_defaults(func=cmd_start)

    st = sub.add_parser("stop", help="oturumu durdur ve temizle")
    st.set_defaults(func=cmd_stop)

    stt = sub.add_parser("status", help="durumu göster")
    stt.add_argument("--json", action="store_true")
    stt.set_defaults(func=cmd_status)

    sub.add_parser("doctor", help="ortam sağlık kontrolü (eksik araçlar/tuzaklar)").set_defaults(func=cmd_doctor)
    sub.add_parser("interfaces", help="arayüzleri listele").set_defaults(func=cmd_interfaces)
    sub.add_parser("apps", help="yüklü uygulamaları listele").set_defaults(func=cmd_apps)
    sub.add_parser("interactive", help="etkileşimli akış").set_defaults(func=cmd_interactive)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))
    settings = Settings.load()
    return args.func(args, settings)
