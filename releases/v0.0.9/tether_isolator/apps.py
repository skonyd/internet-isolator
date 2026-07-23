"""İzole namespace içinde uygulama başlatma.

Uygulamalar gerçek (root olmayan) kullanıcı kimliğiyle ve GUI ortam
değişkenleri (DISPLAY/XAUTHORITY/WAYLAND) korunarak başlatılır. Kalıcı
profil dizinleri sayesinde tarayıcı oturumları silinmez.
"""
from __future__ import annotations

import logging
import os
import shlex
import subprocess

from . import system
from .config import Profile, Settings

log = logging.getLogger("tether.apps")

# Bilinen uygulamalar için izole/kalıcı profil bayrakları.
# {prog: (flag_template, subdir)} — flag içinde {dir} profil dizinine genişler.
_APP_FLAGS = {
    "google-chrome":        ("--user-data-dir={dir} --no-first-run", "chrome"),
    "google-chrome-stable": ("--user-data-dir={dir} --no-first-run", "chrome"),
    "chromium":             ("--user-data-dir={dir} --no-first-run", "chromium"),
    "chromium-browser":     ("--user-data-dir={dir} --no-first-run", "chromium"),
    "brave-browser":        ("--user-data-dir={dir} --no-first-run", "brave"),
    "opera":                ("--user-data-dir={dir}", "opera"),
    "firefox":              ("--profile {dir} --no-remote", "firefox"),
    "code":                 ("--user-data-dir={dir}", "vscode"),
    "terminator":           ("-u", ""),  # ana instance'a bağlanma
}

# UI'de "yüklü mü" diye taranan aday uygulamalar.
KNOWN_APPS = [
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "brave-browser", "opera", "firefox", "code", "terminator", "xterm",
]


def discover_installed() -> list[str]:
    """Sistemde yüklü, bilinen uygulamaları döndürür."""
    seen, out = set(), []
    for app in KNOWN_APPS:
        if app in seen:
            continue
        if system.have(app):
            out.append(app)
            seen.add(app)
    return out


def _gui_env() -> dict[str, str]:
    """Başlatılan sürece taşınacak GUI/oturum ortam değişkenleri."""
    keep = ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR",
            "DBUS_SESSION_BUS_ADDRESS", "XDG_SESSION_TYPE", "HOME", "LANG")
    return {k: os.environ[k] for k in keep if k in os.environ}


def launch(settings: Settings, profile: Profile, program: str,
           *, dry_run: bool = False) -> int:
    """Bir uygulamayı namespace içinde başlatır; PID döndürür (0 = dry/başarısız)."""
    user = system.real_user()
    prog_name = os.path.basename(program.split()[0])
    flags = ""

    # Mevcut sistem profilini kullan: --user-data-dir verme (girişler korunur).
    # Güvenlik: tarayıcı host'ta zaten açıksa, izole kopya host'taki sürece
    # bağlanır ve izolasyon delinir → başlatmayı reddet.
    if profile.use_system_profile and prog_name in _APP_FLAGS:
        if not dry_run and _running_on_host(prog_name, settings.namespace):
            raise RuntimeError(
                f"'{prog_name}' host'ta açık. Mevcut profili izole kullanmak için "
                f"önce host'taki {prog_name} pencerelerini TAMAMEN kapatın "
                f"(izolasyonun delinmemesi için).")
        # firefox dışında çoğu Chromium tabanlı için ek bayrak gerekmez
        flags = "--no-first-run" if "chrome" in prog_name or "chromium" in prog_name \
            or "brave" in prog_name else ""
        argv = shlex.split(f"{program} {flags}".strip())
        return _spawn(settings, user, argv, dry_run=dry_run)

    if prog_name in _APP_FLAGS and profile.persistent_profile:
        tmpl, sub = _APP_FLAGS[prog_name]
        data_dir = os.path.join(profile.profile_data_dir(user), sub) if sub else ""
        if data_dir and not dry_run:
            os.makedirs(data_dir, exist_ok=True)
            # Daemon root iken oluşturulan dizinler root sahipli olur; kullanıcı
            # kimliğiyle açılan uygulama (tarayıcı) bunları kullanamaz → devret.
            system.chown_to_user(profile.profile_data_dir(user), user)
        flags = tmpl.format(dir=data_dir) if "{dir}" in tmpl else tmpl

    # Program + bayrakları boşlukları koruyarak ayır (yollarda boşluk olabilir).
    argv = shlex.split(f"{program} {flags}".strip())
    return _spawn(settings, user, argv, dry_run=dry_run)


def _spawn(settings: Settings, user: str, argv: list[str], *, dry_run: bool) -> int:
    """argv'yi namespace içinde, kullanıcı kimliğiyle, GUI ortamıyla başlatır."""
    env = _gui_env()
    env["HOME"] = system.user_home(user)
    # DNS düzeltmesi: host'ta /etc/resolv.conf çoğunlukla systemd-resolved
    # stub'ına (127.0.0.53) SYMLINK'tir; bu adres izole ns'te erişilemez ve
    # `ip netns exec`'in otomatik bind'i symlink yüzünden tutmaz. Bu yüzden
    # başlatma sırasında (root, özel mount ns) kendi resolv.conf'umuzu
    # symlink'in ÇÖZÜLMÜŞ hedefine bind ediyoruz → uygulamalar 8.8.8.8 görür.
    # Mount özel ns'te kalır; host etkilenmez (ip netns exec / rslave yapar).
    resolv = f"/etc/netns/{settings.namespace}/resolv.conf"
    inner_cmd = ["runuser", "-u", user, "--",
                 "env", *[f"{k}={v}" for k, v in env.items()], *argv]
    preamble = (
        f"if [ -f {shlex.quote(resolv)} ]; then "
        f"t=$(readlink -f /etc/resolv.conf 2>/dev/null || echo /etc/resolv.conf); "
        f"mount --bind {shlex.quote(resolv)} \"$t\" 2>/dev/null || true; fi; exec "
    )
    inner = preamble + " ".join(shlex.quote(a) for a in inner_cmd)
    cmd = ["ip", "netns", "exec", settings.namespace, "sh", "-c", inner]
    log.info("uygulama başlatılıyor: %s", " ".join(argv))
    if dry_run:
        log.info("[dry] %s", " ".join(cmd))
        return 0
    # Başlatıp arkaplana bırak; canlılık izleme PID üzerinden (watchdog).
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc.pid


def _running_on_host(prog_name: str, namespace: str) -> bool:
    """Verilen uygulama HOST namespace'inde çalışıyor mu?

    Kendi izole namespace'imizdeki örnekler hariç tutulur; aksi halde bizim
    başlattığımız izole tarayıcı yanlışlıkla "host'ta açık" sanılırdı.
    """
    ns_pids = set(list_namespace_pids(namespace))   # izole alandakiler önce çekilir
    try:
        # pgrep -x: tam eşleşme (code vs code.py, codec, s code gibi yanlış eşleşmeleri önler)
        r = subprocess.run(["pgrep", "-x", prog_name], capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        return False
    if r.returncode != 0:
        return False
    found = {int(p) for p in r.stdout.split() if p.strip().isdigit()}
    # Host'ta çalışan, bizim namespace'imizde veya kendi PID'miz olmayan var mı?
    return bool(found - ns_pids - {os.getpid()})


def list_namespace_pids(namespace: str) -> list[int]:
    """Verilen namespace içinde çalışan süreçlerin PID listesini döndürür.

    Namespace'in net inode'u ile her sürecin /proc/<pid>/ns/net inode'unu
    karşılaştırarak güvenilir biçimde eşleştirir.
    """
    try:
        target_inode = os.stat(f"/run/netns/{namespace}").st_ino
    except OSError:
        return []
    out: list[int] = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            if os.stat(f"/proc/{pid}/ns/net").st_ino == target_inode:
                out.append(int(pid))
        except OSError:
            continue
    return out


def pid_alive(pid: int) -> bool:
    return pid > 0 and os.path.isdir(f"/proc/{pid}")
