"""İzole namespace içinde uygulama başlatma.

Uygulamalar gerçek (root olmayan) kullanıcı kimliğiyle ve GUI ortam
değişkenleri (DISPLAY/XAUTHORITY/WAYLAND) korunarak başlatılır. Kalıcı
profil dizinleri sayesinde tarayıcı oturumları silinmez.
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import stat
import subprocess
import time

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


# Host'taki GERÇEK (izole olmayan) tarayıcı profili kökleri — "profil içe aktar"
# özelliğinin kaynağı. Chromium ailesi + VS Code için kök, --user-data-dir ile
# birebir eşleşir (doğrudan "Default/" alt klasörünü içerir); bu yüzden düz
# dizin kopyası yeterlidir. Firefox farklı yapıdadır (profiles.ini), ayrı ele alınır.
_HOST_PROFILE_DIRS = {
    "google-chrome":        "~/.config/google-chrome",
    "google-chrome-stable": "~/.config/google-chrome",
    "chromium":              "~/.config/chromium",
    "chromium-browser":      "~/.config/chromium",
    "brave-browser":         "~/.config/BraveSoftware/Brave-Browser",
    "opera":                 "~/.config/opera",
    "code":                  "~/.config/Code",
}


def _firefox_default_profile() -> str:
    """profiles.ini'den varsayılan Firefox profil dizinini bulur (yoksa "")."""
    ini = os.path.expanduser("~/.mozilla/firefox/profiles.ini")
    if not os.path.isfile(ini):
        return ""
    import configparser
    cp = configparser.ConfigParser()
    try:
        cp.read(ini)
    except configparser.Error:
        return ""
    default_rel = ""
    for sec in cp.sections():
        if sec.startswith("Install") and cp.has_option(sec, "Default"):
            default_rel = cp.get(sec, "Default")
            break
    if not default_rel:
        for sec in cp.sections():
            if sec.startswith("Profile") and cp.get(sec, "Default", fallback="0") == "1":
                default_rel = cp.get(sec, "Path", fallback="")
                break
    if not default_rel:
        return ""
    base = os.path.expanduser("~/.mozilla/firefox")
    return default_rel if os.path.isabs(default_rel) else os.path.join(base, default_rel)


def host_profile_source(prog_name: str) -> str:
    """Verilen uygulama için host'taki GERÇEK profil kaynak dizinini döndürür (yoksa "")."""
    if prog_name == "firefox":
        return _firefox_default_profile()
    rel = _HOST_PROFILE_DIRS.get(prog_name)
    return os.path.expanduser(rel) if rel else ""


class ImportProfileError(RuntimeError):
    pass


def _ignore_special_files(dirpath: str, names: list[str]) -> set[str]:
    """`shutil.copytree` 'ignore' geri çağrısı: FIFO/soket/aygıt dosyalarını atlar.

    Tarayıcı profilleri bazen çalışma-zamanı IPC dosyaları içerir (ör. Opera'nın
    `oauc_pipe` adlandırılmış borusu). Bunlar veri değildir, açılamazlar
    (ENXIO/ENODEV) ve normal dosya kopyası gibi kopyalanamazlar; atlanmazlarsa
    kopyalama yarıda hatayla patlar. Gerçek profil verisi (Login Data, Cookies,
    Preferences vb. düz dosyalar) bu atlamadan etkilenmez.
    """
    skip = set()
    for name in names:
        try:
            st = os.lstat(os.path.join(dirpath, name))
        except OSError:
            continue
        m = st.st_mode
        if stat.S_ISFIFO(m) or stat.S_ISSOCK(m) or stat.S_ISBLK(m) or stat.S_ISCHR(m):
            skip.add(name)
    return skip


def _any_running(prog_name: str) -> bool:
    """Verilen adda BİR SÜREÇ (host ya da izole alan fark etmeksizin) çalışıyor mu?"""
    try:
        r = subprocess.run(["pgrep", "-x", prog_name], capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        return False
    if r.returncode != 0:
        return False
    found = {int(p) for p in r.stdout.split() if p.strip().isdigit()}
    return bool(found - {os.getpid()})


def import_profile(settings: Settings, profile: Profile, program: str,
                   *, dry_run: bool = False) -> str:
    """Host'taki GERÇEK tarayıcı/uygulama profilini izole (kalıcı) profile aktarır.

    Amaç: kullanıcının daha önce (izolasyon dışında ya da eski bir sürümde) girdiği
    şifre/oturum bilgilerini izole profilde de kullanılabilir kılmak. TEK SEFERLİK,
    canlı SENKRON DEĞİL bir kopyadır — sonrasında iki profil yine bağımsız gelişir;
    izolasyonun "izole taraf host'a geri yazmaz" ilkesi korunur.

    Güvenlik: kaynak/hedefle ilişkili süreç (host'ta ya da izole alanda) hâlâ
    çalışıyorsa reddedilir — açık bir tarayıcının profil dosyaları üstüne
    kopyalama veri bozulmasına yol açabilir. Mevcut izole profil silinmez;
    zaman damgalı bir yedeğe taşınır.
    """
    user = system.real_user()
    prog_name = os.path.basename(program.split()[0])
    if prog_name not in _APP_FLAGS:
        raise ImportProfileError(f"'{prog_name}' için profil içe aktarma desteklenmiyor.")
    if not dry_run and _any_running(prog_name):
        raise ImportProfileError(
            f"'{prog_name}' şu an açık (host'ta ya da izole alanda). Önce TÜM "
            f"{prog_name} pencerelerini kapatın (veri bozulmaması için).")
    src = host_profile_source(prog_name)
    if not src or not os.path.isdir(src):
        raise ImportProfileError(
            f"Host'ta '{prog_name}' için gerçek bir profil bulunamadı"
            + (f": {src}" if src else " (bilinen bir konum yok)") + ".")
    _tmpl, sub = _APP_FLAGS[prog_name]
    dest = os.path.join(profile.profile_data_dir(user), sub) if sub \
        else profile.profile_data_dir(user)
    if dry_run:
        log.info("[dry] profil içe aktarılırdı: %s -> %s", src, dest)
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.isdir(dest):
        backup = f"{dest}.bak-{int(time.time())}"
        os.rename(dest, backup)
        log.info("mevcut izole profil yedeklendi: %s", backup)
    shutil.copytree(src, dest, symlinks=True, ignore_dangling_symlinks=True,
                    ignore=_ignore_special_files)
    system.chown_to_user(profile.profile_data_dir(user), user)
    log.info("profil içe aktarıldı: %s -> %s", src, dest)
    return dest


# "Uygulama Ekle" ekranı için sistemdeki .desktop kısayollarının taranacağı
# standart XDG konumları (kullanıcıya özel olanlar ayrıca real_user() ev
# dizininden eklenir — daemon root çalıştığında ~/.local root'a çözülür).
_DESKTOP_DIRS = [
    "/usr/share/applications",
    "/usr/local/share/applications",
    "/var/lib/snapd/desktop/applications",
    "/var/lib/flatpak/exports/share/applications",
]
_USER_DESKTOP_SUBDIRS = [
    ".local/share/applications",
    ".local/share/flatpak/exports/share/applications",
]
_FIELD_CODE_RE = re.compile(r"%[fFuUdDnNickvm]")


def _parse_desktop_entry(path: str) -> dict | None:
    """Bir .desktop dosyasından {name, command} çıkarır (uygun değilse None).

    Yalnızca ana [Desktop Entry] bölümü okunur (ör. [Desktop Action ...] alt
    eylemleri atlanır). NoDisplay/Hidden=true ya da Type != Application olan
    girdiler (çoğunlukla mimeinfo/ayar paneli kısayolları) listeden çıkarılır.
    Tam masaüstü-girdisi kaçış kuralları uygulanmaz; yalnızca %f/%u gibi
    argüman yer tutucuları temizlenir — pratikte kısayolların büyük
    çoğunluğu için yeterlidir.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    in_entry = False
    name, exec_, no_display, entry_type = "", "", False, "Application"
    for raw in lines:
        line = raw.strip()
        if line.startswith("["):
            in_entry = (line == "[Desktop Entry]")
            continue
        if not in_entry:
            continue
        if line.startswith("Name=") and not name:
            name = line[5:].strip()
        elif line.startswith("Exec=") and not exec_:
            exec_ = line[5:].strip()
        elif line.startswith("NoDisplay="):
            no_display = no_display or line[10:].strip().lower() == "true"
        elif line.startswith("Hidden="):
            no_display = no_display or line[7:].strip().lower() == "true"
        elif line.startswith("Type="):
            entry_type = line[5:].strip()
    if no_display or entry_type != "Application" or not exec_:
        return None
    exec_ = " ".join(_FIELD_CODE_RE.sub("", exec_).split())
    if not exec_:
        return None
    return {"name": name or exec_, "command": exec_}


def list_desktop_apps() -> list[dict]:
    """Sistemdeki .desktop kısayollarını tarar; {name, command} listesi döndürür."""
    user = system.real_user()
    home = system.user_home(user)
    dirs = list(_DESKTOP_DIRS) + [os.path.join(home, sub) for sub in _USER_DESKTOP_SUBDIRS]
    seen: set[str] = set()
    out: list[dict] = []
    for d in dirs:
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for fn in names:
            if not fn.endswith(".desktop"):
                continue
            entry = _parse_desktop_entry(os.path.join(d, fn))
            if not entry or entry["command"] in seen:
                continue
            seen.add(entry["command"])
            out.append(entry)
    out.sort(key=lambda e: e["name"].lower())
    return out


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
    # Çalışma dizini düzeltmesi: bu süreç zinciri (ip netns exec -> sh -c ->
    # runuser) root'un cwd'sini (genelde /root) miras alır — `runuser`, `su`
    # gibi hedef kullanıcının ev dizinine CD yapmaz, yalnızca UID/GID değiştirir.
    # Başlatılan uygulama (uid=user) bu yüzden erişemediği /root'u process.cwd()
    # olarak görür; kendi içinde çalışma dizinine bağlı bir alt süreç başlatan
    # uygulamalar (ör. Claude Desktop'ın kendi `git --version` kontrolü) sessizce
    # başarısız olur. Host'ta normal bir terminalin zaten yaptığı gibi, exec'ten
    # önce kullanıcının ev dizinine geçiyoruz.
    preamble = (
        f"cd {shlex.quote(env['HOME'])} 2>/dev/null || cd /tmp; "
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

    B-7: pgrep -x 15 karakter sınırını aşmak için önce pgrep -f ile argv
    doğrulaması yapılır; eşleşen süreçlerin inode karşılaştırması ile host'ta
    olup olmadığı teyit edilir.
    """
    ns_pids = set(list_namespace_pids(namespace))
    try:
        # pgrep -f: tam komut satırında ara (15 karakter sınırı yok)
        r = subprocess.run(["pgrep", "-f", prog_name], capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        return False
    if r.returncode != 0:
        return False
    found = {int(p) for p in r.stdout.split() if p.strip().isdigit()}
    # Host'ta çalışan, bizim namespace'imizde olmayan var mı?
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
