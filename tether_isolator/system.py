"""Sistem yardımcıları: komut çalıştırma, arayüz keşfi, yetki/kullanıcı bilgisi.

Bu modül tüm düşük seviyeli OS etkileşimlerini tek yerde toplar; böylece
geri kalan kod test edilebilir ve okunabilir kalır.
"""
from __future__ import annotations

import logging
import os
import pwd
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("tether.system")

# Sanal/sistem arayüzleri uplink seçiminde gösterilmez.
_IGNORED_PREFIXES = ("lo", "veth", "docker", "br-", "virbr", "tisor-")


class CommandError(RuntimeError):
    """Bir kabuk komutu sıfır olmayan bir kodla döndüğünde fırlatılır."""

    def __init__(self, cmd: list[str], code: int, out: str, err: str):
        self.cmd = cmd
        self.code = code
        self.out = out
        self.err = err
        super().__init__(f"komut başarısız ({code}): {' '.join(cmd)}\n{err.strip()}")


@dataclass
class RunResult:
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0


def run(cmd: list[str], *, check: bool = True, timeout: int = 30,
        dry_run: bool = False) -> RunResult:
    """Bir komutu çalıştırır ve sonucunu döndürür.

    dry_run=True iken komut çalıştırılmaz, yalnızca loglanır (test/önizleme).
    """
    log.debug("run%s: %s", " [dry]" if dry_run else "", " ".join(cmd))
    if dry_run:
        return RunResult(0, "", "")
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError as e:
        raise CommandError(cmd, 127, "", str(e)) from e
    except subprocess.TimeoutExpired as e:
        raise CommandError(cmd, 124, "", f"zaman aşımı ({timeout}s)") from e
    res = RunResult(p.returncode, p.stdout, p.stderr)
    if check and not res.ok:
        raise CommandError(cmd, res.code, res.out, res.err)
    return res


def have(binary: str) -> bool:
    """İkili dosya PATH'te var mı?"""
    return shutil.which(binary) is not None


def is_root() -> bool:
    return os.geteuid() == 0


def real_user() -> str:
    """sudo ile çalışırken gerçek (root olmayan) kullanıcıyı bulur."""
    for var in ("SUDO_USER", "PKEXEC_UID"):
        val = os.environ.get(var)
        if val and val != "root":
            if var == "PKEXEC_UID":
                try:
                    return pwd.getpwuid(int(val)).pw_name
                except (KeyError, ValueError):
                    continue
            # B-6: SUDO_USER değerini doğrula
            try:
                pwd.getpwnam(val)
                return val
            except KeyError:
                continue
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return "root"


def user_home(username: str) -> str:
    try:
        return pwd.getpwnam(username).pw_dir
    except KeyError:
        return os.path.expanduser("~")


def chown_to_user(path: str, username: str) -> None:
    """`path`'i (ve root tarafından oluşturulmuş üst dizinlerini) kullanıcıya devreder.

    Daemon root çalıştığında oluşturduğu profil/veri dizinleri root sahipli olur;
    bu dizinleri kullanıcı kimliğiyle açılan uygulamalar (tarayıcılar) kullanamaz.
    Bu yardımcı, hedefi özyinelemeli ve kullanıcının ev dizinine kadar olan
    ata dizinleri chown ederek bunu giderir.
    """
    if not is_root():
        return
    try:
        pw = pwd.getpwnam(username)
    except KeyError:
        return
    uid, gid = pw.pw_uid, pw.pw_gid
    home = os.path.realpath(pw.pw_dir)

    def _chown(p: str) -> None:
        try:
            os.chown(p, uid, gid)
        except OSError:
            pass

    # Hedefi özyinelemeli devret
    if os.path.isdir(path):
        for root, dirs, files in os.walk(path):
            _chown(root)
            for name in (*dirs, *files):
                _chown(os.path.join(root, name))
    elif os.path.exists(path):
        _chown(path)

    # Ev dizinine kadar (dahil değil) root tarafından açılmış üst dizinleri devret
    cur = os.path.realpath(path)
    while True:
        cur = os.path.dirname(cur)
        if not cur or cur == "/" or cur == home or not cur.startswith(home):
            break
        _chown(cur)


@dataclass
class Interface:
    name: str
    state: str = "DOWN"          # UP / DOWN / UNKNOWN
    kind: str = "ethernet"        # ethernet / wifi / usb / unknown
    mac: str = ""
    in_namespace: Optional[str] = None  # hangi netns içinde (varsa)

    @property
    def is_wifi(self) -> bool:
        return self.kind == "wifi"


def _classify(name: str) -> str:
    """Arayüz adından kabaca tip tahmini (ipuçları yalnızca UI içindir)."""
    if name.startswith(("wl", "wlan", "wlp")):
        return "wifi"
    if name.startswith(("usb", "enx", "rndis")):
        return "usb"
    if name.startswith(("en", "eth", "enp")):
        return "ethernet"
    return "unknown"


def list_host_interfaces() -> list[Interface]:
    """Host (ana) namespace'teki fiziksel arayüzleri listeler."""
    res = run(["ip", "-o", "link", "show"], check=False)
    out: list[Interface] = []
    for line in res.out.splitlines():
        # Örn: "3: usb0: <BROADCAST,...> mtu 1500 ... link/ether aa:bb:.. "
        parts = line.split(": ", 2)
        if len(parts) < 3:
            continue
        name = parts[1].split("@")[0].strip()
        if name.startswith(_IGNORED_PREFIXES) or name == "lo":
            continue
        rest = parts[2]
        state = "UP" if "state UP" in rest else (
            "DOWN" if "state DOWN" in rest else "UNKNOWN")
        mac = ""
        if "link/ether " in rest:
            mac = rest.split("link/ether ", 1)[1].split()[0]
        out.append(Interface(name=name, state=state, kind=_classify(name), mac=mac))
    return out


def interface_in_namespace(ns: str, iface: str) -> bool:
    """Belirtilen arayüz, verilen namespace içinde mevcut mu?"""
    res = run(["ip", "netns", "exec", ns, "ip", "-o", "link", "show", iface],
              check=False)
    return res.ok and iface in res.out


def namespace_exists(ns: str) -> bool:
    res = run(["ip", "netns", "list"], check=False)
    return any(line.split()[0] == ns for line in res.out.splitlines() if line.strip())


def detect_wifi_phy(iface: str) -> Optional[str]:
    """Bir WiFi arayüzünün (wlanX) bağlı olduğu PHY adını döndürür (phyN)."""
    path = f"/sys/class/net/{iface}/phy80211/name"
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


@dataclass
class WifiNetwork:
    ssid: str
    signal: Optional[float] = None   # dBm (yüksek/0'a yakın = güçlü)
    security: str = "open"           # open / wpa


def _parse_iw_scan(out: str) -> list[WifiNetwork]:
    """`iw dev <iface> scan` çıktısını ağ listesine çevirir."""
    networks: dict[str, WifiNetwork] = {}

    def _commit(bss: Optional[WifiNetwork]) -> None:
        if bss is None or not bss.ssid:
            return
        existing = networks.get(bss.ssid)
        if existing is None or (bss.signal or -999) > (existing.signal or -999):
            networks[bss.ssid] = bss

    cur: Optional[WifiNetwork] = None
    for raw in out.splitlines():
        line = raw.strip()
        if line.startswith("BSS "):
            _commit(cur)
            cur = WifiNetwork(ssid="")
        elif cur is None:
            continue
        elif line.startswith("SSID:"):
            cur.ssid = line.split("SSID:", 1)[1].strip()
        elif line.startswith("signal:"):
            try:
                cur.signal = float(line.split("signal:", 1)[1].strip().split()[0])
            except (ValueError, IndexError):
                pass
        elif line.startswith(("WPA:", "RSN:")):
            cur.security = "wpa"
    _commit(cur)
    return sorted(networks.values(), key=lambda n: -(n.signal if n.signal is not None else -999))


def wifi_scan(iface: str, *, in_namespace: Optional[str] = None,
              dry_run: bool = False) -> tuple[list[WifiNetwork], str]:
    """Çevredeki WiFi ağlarını tarar (Ubuntu ağ menüsündeki gibi bir liste için).

    `in_namespace` verilirse tarama, PHY'ı zaten devralmış olan izole alan
    içinde yapılır (arayüz artık host'ta görünmez); aksi halde host'ta yapılır.

    (ağlar, hata_mesajı) döner — tarama başarısızsa ağlar boş liste, hata_mesajı
    doludur; başarılıysa hata_mesajı boş string'dir. root olmadan `iw scan`
    çoğu sürücüde "Operation not permitted" ile başarısız olur; ayrıca
    NetworkManager aynı anda tarama yapıyorsa "Device or resource busy" (EBUSY)
    dönebilir — bu geçicidir, birkaç kez yeniden denenir.
    """
    if dry_run:
        return [], ""
    if not is_root():
        return [], "WiFi taraması root yetkisi gerektirir; paneli root olarak çalıştırın."
    if not have("iw"):
        return [], "'iw' aracı kurulu değil (sudo apt install iw)."
    base = ["ip", "netns", "exec", in_namespace] if in_namespace else []
    run(base + ["ip", "link", "set", iface, "up"], check=False, dry_run=dry_run)
    last_err = ""
    for attempt in range(3):
        res = run(base + ["iw", "dev", iface, "scan"], check=False, timeout=15,
                  dry_run=dry_run)
        if res.ok:
            return _parse_iw_scan(res.out), ""
        last_err = (res.err or res.out).strip()
        # "resource busy" (EBUSY) genelde NetworkManager'ın eşzamanlı taraması
        # yüzünden olur ve kısa bekleyince geçer; diğer hatalarda tekrar denemenin
        # faydası yok (ör. arayüz yok, izin yok).
        if "busy" not in last_err.lower():
            break
        time.sleep(1.5)
    return [], last_err or "tarama başarısız"


def kill_stray_daemons(pattern: str, *, exclude_pid: int) -> list[int]:
    """`/proc` üzerinden komut satırında `pattern` geçen (kendisi HARİÇ) tüm
    süreçlere SIGTERM gönderir; sonlandırılan PID'leri döndürür.

    "Yeniden başlat"ta artık kalıntı/kopya daemon süreçlerini (ör. eski bir
    kod sürümünden kalmış, portu tutan bir örnek) veya bize ait yalnız kalmış
    yardımcı süreçleri (wpa_supplicant vb.) temizlemek için kullanılır. Kendi
    PID'imizi (exclude_pid) HARİÇ tutmak zorunlu: execv ile kendimizi yeniden
    başlatmadan önce kendimizi SIGTERM'lemeyelim.
    """
    killed: list[int] = []
    for pid_s in os.listdir("/proc"):
        if not pid_s.isdigit():
            continue
        pid = int(pid_s)
        if pid == exclude_pid:
            continue
        try:
            with open(f"/proc/{pid_s}/cmdline", "rb") as f:
                cmdline = f.read().replace(b"\x00", b" ").decode(errors="replace")
        except OSError:
            continue
        if pattern in cmdline:
            try:
                os.kill(pid, signal.SIGTERM)
                killed.append(pid)
            except OSError:
                pass
    return killed
