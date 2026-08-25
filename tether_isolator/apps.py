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

# Veri tasarrufu (Faz 3): yalnızca tarayıcılara uygulanır — VS Code/terminator
# hariç. Bayraklar/user.js yalnızca YENİ başlatılan örnekte etkilidir; halihazırda
# açık bir tarayıcı varsa manager.restart_apps() ile yeniden başlatılmalı.
_CHROMIUM_FAMILY = {
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "brave-browser", "opera",
}

# Otomatik oynatmayı, arka plan senkron/güncelleme/prefetch trafiğini kapatır
# ve disk önbelleğini büyütür (tekrar indirmeyi azaltır). Chrome'un eski
# "Data Saver" proxy modu masaüstünde kaldırıldığı için proxy vaadi YOK.
_CHROMIUM_DATA_SAVER_BASE = (
    "--autoplay-policy=user-gesture-required --disable-background-networking "
    "--disable-component-update --disable-sync --no-pings "
    "--disk-cache-size=1073741824 "
    "--disable-features=OptimizationHints,Translate,MediaRouter"
)
_CHROMIUM_DATA_SAVER_STRICT = "--blink-settings=imagesEnabled=false"

# Medya (video/müzik) engelleme — Chromium ailesi.
#
# Neden bu yöntem (denenen ve ELENEN alternatifler):
#   * Uzantı (`--load-extension`): Chrome 137+ komut satırından paketlenmemiş
#     uzantı yüklemeyi güvenlik gerekçesiyle kaldırdı. Bayrak SESSİZCE yok
#     sayılıyor (hata bile vermiyor) → tarayıcı açılıyor, hiçbir şey engellenmiyor.
#   * `--disable-blink-features=MediaSource`: Firefox'un MSE kapatmasının
#     karşılığı olurdu ama Chrome'da MediaSource devre dışı bırakılabilir bir
#     Blink runtime özelliği DEĞİL — bayrak etkisiz (mekanizmanın kendisi
#     çalışıyor: ör. `Notifications` başarıyla kapanıyor; `MediaSource` kapanmıyor).
#
# Kalan sağlam yol: tarayıcının KENDİ ad çözümleyicisini yönlendirmek. Bu bir
# DNS sunucusu/`resolv.conf` işi DEĞİLDİR — sistem DNS'ine hiç dokunulmaz,
# yalnızca bu tarayıcı sürecinin iç çözümleyicisi medya CDN'lerini ölü bir
# adrese eşler → segment istekleri anında bağlantı hatası alır.
#
# Sonuç: site/arayüz normal açılır (ana alan adları listede DEĞİL), yalnızca
# video/ses segmentleri inemez. Joker (`*.`) desteği YouTube'un istek başına
# ürettiği `rr3---sn-4g5ednek.googlevideo.com` gibi adları da yakalar.
_MEDIA_BLOCK_HOSTS = (
    "googlevideo.com",       # YouTube video/ses segmentleri
    "nflxvideo.net",         # Netflix
    "ttvnw.net",             # Twitch video edge
    "scdn.co",               # Spotify ses CDN
    "sndcdn.com",            # SoundCloud
    "vimeocdn.com",          # Vimeo
    "video.twimg.com",       # Twitter/X video
    "dmcdn.net",             # Dailymotion
)
# Loopback'e eşle: izole alanda 443'te dinleyen bir şey olmadığından bağlantı
# ANINDA reddedilir (blackhole IP'de olduğu gibi uzun zaman aşımı beklenmez).
_MEDIA_BLOCK_SINK = "127.0.0.1"


def _chromium_media_block_flag() -> str:
    """Chromium ailesi için `--host-resolver-rules=...` bayrağını üretir (kabuk-güvenli)."""
    rules = []
    for host in _MEDIA_BLOCK_HOSTS:
        rules.append(f"MAP {host} {_MEDIA_BLOCK_SINK}")
        rules.append(f"MAP *.{host} {_MEDIA_BLOCK_SINK}")
    return shlex.quote("--host-resolver-rules=" + ",".join(rules))


def _chromium_data_saver_flags(level: str) -> str:
    flags = _CHROMIUM_DATA_SAVER_BASE
    if level == "strict":
        flags += " " + _CHROMIUM_DATA_SAVER_STRICT
    return flags


# Firefox aynı bayrak mekanizmasını desteklemez; izole profile bir user.js
# yazılır. Chromium'daki karşılıklarıyla aynı amaç: prefetch/sync/güncelleme
# arka plan trafiğini kapat, disk önbelleğini büyüt, otomatik oynatmayı kıs.
_FIREFOX_DATA_SAVER_BASE = {
    "network.prefetch-next": False,
    "network.dns.disablePrefetch": True,
    "network.predictor.enabled": False,
    "media.autoplay.default": 5,             # 5 = kullanıcı etkileşimi gerekir
    "app.update.auto": False,
    "extensions.update.enabled": False,
    "browser.cache.disk.capacity": 1048576,  # KB — ~1 GB
    "browser.newtabpage.activity-stream.feeds.section.topstories": False,
}
_FIREFOX_DATA_SAVER_STRICT = {
    "permissions.default.image": 2,          # 2 = görselleri engelle
}

_FIREFOX_MARKER_START = "// --- tether-isolator: veri tasarrufu (otomatik) ---"
_FIREFOX_MARKER_END = "// --- tether-isolator: veri tasarrufu sonu ---"

# Medya (video/müzik) engelleme — veri tasarrufu düzeyinden BAĞIMSIZ, kendi
# işaretçi çiftiyle ayrı bir user.js bloğu (aynı dosyada iki blok bir arada
# durabilir; bkz. _splice_firefox_block).
#
# MediaSource Extensions'ı (MSE) kapatır: YouTube/Netflix/Twitch/Spotify web
# player'ı DAHİL, uyarlanabilir video/ses akışı yapan neredeyse her modern
# oynatıcı MSE'ye bağımlıdır. API'nin kendisi yoksa oynatıcı segment indirmeye
# hiç BAŞLAYAMAZ — bilinen bir CDN alan adı listesine güvenmekten (kolayca
# eksik/eski kalır) çok daha güvenilir. Düz <video src>/<audio src>
# dosyaları da ayrıca media.*.enabled ile kapatılır.
_FIREFOX_MEDIA_BLOCK_PREFS = {
    "media.mediasource.enabled": False,
    "media.mp4.enabled": False,
    "media.webm.enabled": False,
    "media.ogg.enabled": False,
    "media.wave.enabled": False,
    "media.av1.enabled": False,
}
_FIREFOX_MEDIA_MARKER_START = "// --- tether-isolator: video/müzik engelleme (otomatik) ---"
_FIREFOX_MEDIA_MARKER_END = "// --- tether-isolator: video/müzik engelleme sonu ---"


def _firefox_pref_literal(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _firefox_pref_block(prefs: dict, marker_start: str, marker_end: str) -> str:
    lines = [marker_start]
    for key in sorted(prefs):
        lines.append(f'user_pref("{key}", {_firefox_pref_literal(prefs[key])});')
    lines.append(marker_end)
    return "\n".join(lines)


def _firefox_data_saver_block(level: str) -> str:
    prefs = dict(_FIREFOX_DATA_SAVER_BASE)
    if level == "strict":
        prefs.update(_FIREFOX_DATA_SAVER_STRICT)
    return _firefox_pref_block(prefs, _FIREFOX_MARKER_START, _FIREFOX_MARKER_END)


def _splice_firefox_block(existing: str, block: str | None, *,
                          marker_start: str = _FIREFOX_MARKER_START,
                          marker_end: str = _FIREFOX_MARKER_END) -> str:
    """user.js içindeki KENDİ (marker_start/end) bloğumuzu ekler/günceller/kaldırır.

    Kullanıcının elle eklediği satırlara VE varsa bu dosyadaki DİĞER
    özelliğin (farklı marker çiftiyle yazılmış) bloğuna dokunmaz — yalnızca
    kendi başlangıç/bitiş işaretçileri arasındaki bölüm değiştirilir.
    """
    if marker_start in existing and marker_end in existing:
        pre, _, rest = existing.partition(marker_start)
        _, _, post = rest.partition(marker_end)
        pre, post = pre.rstrip("\n"), post.lstrip("\n")
    else:
        pre, post = existing.rstrip("\n"), ""
    parts = [p for p in (pre, block, post) if p]
    return ("\n\n".join(parts) + "\n") if parts else ""


def _apply_firefox_data_saver(profile_dir: str, enabled: bool, level: str) -> None:
    """Firefox profilindeki user.js'i veri tasarrufu durumuna göre günceller."""
    path = os.path.join(profile_dir, "user.js")
    try:
        with open(path, encoding="utf-8") as f:
            existing = f.read()
    except OSError:
        existing = ""
    block = _firefox_data_saver_block(level) if enabled else None
    content = _splice_firefox_block(existing, block)
    if not content:
        try:
            os.remove(path)
        except OSError:
            pass
        return
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _apply_firefox_media_block(profile_dir: str, enabled: bool) -> None:
    """Firefox profilinde MediaSource + medya format desteğini kapatır/açar.

    `data_saver.enabled`'dan bağımsız çalışır (kendi marker çifti; bkz.
    _splice_firefox_block'un DİĞER bloğa dokunmama garantisi).
    """
    path = os.path.join(profile_dir, "user.js")
    try:
        with open(path, encoding="utf-8") as f:
            existing = f.read()
    except OSError:
        existing = ""
    block = _firefox_pref_block(_FIREFOX_MEDIA_BLOCK_PREFS, _FIREFOX_MEDIA_MARKER_START,
                                _FIREFOX_MEDIA_MARKER_END) if enabled else None
    content = _splice_firefox_block(existing, block, marker_start=_FIREFOX_MEDIA_MARKER_START,
                                    marker_end=_FIREFOX_MEDIA_MARKER_END)
    if not content:
        try:
            os.remove(path)
        except OSError:
            pass
        return
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


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

    # İzolasyon güvenliği: bu uygulamalar (Chromium ailesi, Firefox, VS Code)
    # aynı profil dizinini kullanan bir örnek ZATEN çalışıyorsa, isteği
    # SingletonSocket üzerinden o örneğe devredip kendileri çıkar. İzole alanda
    # bu, pencerenin host'taki süreçte açılması — yani trafiğin host ağından
    # çıkması — demektir; kullanıcı izole sandığı tarayıcıyı host ağıyla
    # kullanır. Devri engellemenin tek güvenilir yolu profil dizinini
    # ayırmaktır, bu yüzden bu uygulamalara HER ZAMAN izole bir profil verilir.
    if prog_name in _APP_FLAGS:
        if profile.use_system_profile:
            log.warning(
                "'%s' için sistem profili istendi; singleton devri izolasyonu "
                "deleceği için yok sayıldı, izole profil kullanılıyor. "
                "Oturumları taşımak için profili bir kez içe aktarın.", prog_name)
        tmpl, sub = _APP_FLAGS[prog_name]
        data_dir = os.path.join(profile.profile_data_dir(user), sub) if sub else ""
        # Kalıcı olmayan profil de izole olmak zorunda: aksi halde bayraksız
        # başlar ve doğrudan sistem profiline (dolayısıyla devre) düşer.
        # Bu yüzden dizin verilir ama her başlatmada sıfırlanır.
        if data_dir and not profile.persistent_profile and not dry_run:
            shutil.rmtree(data_dir, ignore_errors=True)
        if data_dir and not dry_run:
            os.makedirs(data_dir, exist_ok=True)
            # Daemon root iken oluşturulan dizinler root sahipli olur; kullanıcı
            # kimliğiyle açılan uygulama (tarayıcı) bunları kullanamaz → devret.
            system.chown_to_user(profile.profile_data_dir(user), user)
        flags = tmpl.format(dir=data_dir) if "{dir}" in tmpl else tmpl

        # Veri tasarrufu (Faz 3): Chromium ailesine komut satırı bayrağı ekle,
        # Firefox'a profile user.js yaz. Yalnızca YENİ başlatılan örnekte etkili.
        ds = profile.data_saver
        # Medya kademesi: yalnızca "blocked" tarayıcı seviyesinde SERT engel
        # uygular. "360p"/"720p" kaliteyi bant genişliği tavanıyla düşürür
        # (manager._apply_shaping) — tarayıcıya dokunmaz, çünkü oynatıcıya
        # "şu çözünürlüğü kullan" diyen bir tarayıcı arayüzü yok.
        hard_block = ds.media_level == "blocked"
        if prog_name in _CHROMIUM_FAMILY:
            if ds.enabled:
                flags = f"{flags} {_chromium_data_saver_flags(ds.level)}".strip()
            if hard_block:
                flags = f"{flags} {_chromium_media_block_flag()}".strip()
        elif prog_name == "firefox" and data_dir and not dry_run:
            try:
                _apply_firefox_data_saver(data_dir, ds.enabled, ds.level)
            except OSError as e:
                log.warning("Firefox veri tasarrufu user.js yazılamadı: %s", e)
            try:
                _apply_firefox_media_block(data_dir, hard_block)
            except OSError as e:
                log.warning("Firefox medya engelleme user.js yazılamadı: %s", e)

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
