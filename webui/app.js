// Tether Isolator — arayüz mantığı (bağımlılıksız, vanilla JS)
// v0.1.0 — Sprint UX: onboarding, trust badge, tema, komut paleti, trafik, hız testi, bildirim
"use strict";

const $ = (id) => document.getElementById(id);
// API token'ı iki kaynaktan dener: URL fragment (#token=…) ve localStorage.
// ÖNEMLİ: localStorage kullanılır (sessionStorage DEĞİL) çünkü sessionStorage
// sekmeye özeldir — yeni bir sekme/pencerede (ör. masaüstü kısayolu tekrar
// tıklanmadan, panel linki elle açılınca) token taşınmaz ve o sekmedeki HER
// istek (hangi uygulama tıklanırsa tıklansın) 401 "yetkisiz erişim" döner.
// localStorage aynı origin'in TÜM sekme/pencerelerinde paylaşılır; bir sekmede
// alınan token diğerlerinde de geçerli olur.
let apiToken = "";
try { apiToken = localStorage.getItem("tisor_token") || ""; } catch (_) {}
if (location.hash.startsWith("#token=")) {
  apiToken = decodeURIComponent(location.hash.substring(7));
  try { localStorage.setItem("tisor_token", apiToken); } catch (_) {}
  history.replaceState(null, "", location.pathname + location.search);
}

const api = async (path, opts = {}) => {
  opts.headers = opts.headers || {};
  if (apiToken) opts.headers["Authorization"] = "Bearer " + apiToken;
  const r = await fetch(path, opts);
  if (!r.ok) {
    if (r.status === 401) {
      throw new Error("Oturum yetkisi geçersiz/eksik. Paneli 'Tether Isolator' " +
        "simgesinden yeniden açın ya da sayfayı tamamen yenileyin (Ctrl+Shift+R).");
    }
    throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
  }
  return r.json();
};

const PHASE = {
  idle:         { label: "Boşta",          hint: "Bir oturum başlatın." },
  starting:     { label: "Başlatılıyor",   hint: "Ağ alanı kuruluyor…" },
  online:       { label: "Çevrimiçi",      hint: "İzole ve bağlı." },
  degraded:     { label: "Kısıtlı",        hint: "Uplink var, internet doğrulanamadı." },
  reconnecting: { label: "Yeniden bağlanıyor", hint: "Uplink bekleniyor…" },
  stopping:     { label: "Durduruluyor",   hint: "Temizleniyor…" },
};

let selectedUplink = null;
const selectedApps = new Set();
let busy = false;
let currentPhase = "idle";
let installedApps = [];
let customAppNames = {}; // command -> görünen ad (.desktop'tan eklenen özel uygulamalar)
let allInterfaces = [];
let lastEventTime = 0;
const MAX_EVENTS = 50;
let activeUplink = null;
let eventFilter = "all";
let hostPublicIp = null;   // U-3: host dış IP'si
let profiles = {};
let activeProfileName = "default";

function selectedUplinkKind() {
  const i = allInterfaces.find((x) => x.name === selectedUplink);
  return i ? i.kind : "";
}

// ------------------------------------------------------ Wi-Fi seçici (Ubuntu/GNOME tarzı)
let wifiNetworks = [];      // son tarama sonucu: {ssid, signal, security, saved}
let wifiScanning = false;
let wifiScannedFor = null;  // hangi arayüz için son tarandı
let wifiScanError = "";     // son taramadan gelen hata mesajı (varsa)
let wifiCurrentSsid = "";   // arayüzün ŞU AN bağlı olduğu ağ
let wifiAuthTarget = null;  // parola penceresinin hedefi: {ssid, hidden}

// GNOME'un ağ simgesi: ortak merkezli üç yay + nokta. Sinyal seviyesine göre
// yaylar sönükleşir (0 = yalnızca nokta → çok zayıf / menzil dışı).
function wifiSignalIcon(signal) {
  let level;
  if (signal === null || signal === undefined) level = 0;
  else if (signal >= -55) level = 3;
  else if (signal >= -67) level = 2;
  else if (signal >= -78) level = 1;
  else level = 0;
  const arc = (need, d) =>
    `<path d="${d}" fill="none" stroke="currentColor" stroke-width="1.7"` +
    ` stroke-linecap="round" class="${level >= need ? "" : "arc-off"}"/>`;
  return '<svg viewBox="0 0 16 16" aria-hidden="true">' +
    arc(3, "M1.26 6.84 A8.8 8.8 0 0 1 14.74 6.84") +
    arc(2, "M3.40 8.64 A6 6 0 0 1 12.60 8.64") +
    arc(1, "M5.55 10.44 A3.2 3.2 0 0 1 10.45 10.44") +
    '<circle cx="8" cy="12.6" r="1.35" fill="currentColor"/></svg>';
}

const WIFI_LOCK_SVG =
  '<svg viewBox="0 0 16 16" aria-hidden="true">' +
  '<rect x="3.5" y="7" width="9" height="7" rx="1.6" fill="currentColor"/>' +
  '<path d="M5.75 7V5.25a2.25 2.25 0 0 1 4.5 0V7" fill="none" stroke="currentColor" stroke-width="1.4"/></svg>';
const WIFI_CHECK_SVG =
  '<svg viewBox="0 0 16 16" aria-hidden="true">' +
  '<path d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" stroke-width="2"' +
  ' stroke-linecap="round" stroke-linejoin="round"/></svg>';
// Dolu cog: ince çizgili/ışınsal bir dişli 16px'te "parlaklık" simgesi gibi
// okunuyordu; dolu gövde küçük boyutta net kalıyor.
const WIFI_GEAR_SVG =
  '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="' +
  'M12 15.5A3.5 3.5 0 0 1 8.5 12 3.5 3.5 0 0 1 12 8.5a3.5 3.5 0 0 1 3.5 3.5 3.5 3.5 0 0 1-3.5 3.5' +
  'm7.43-2.53c.04-.32.07-.64.07-.97 0-.33-.03-.66-.07-1l2.11-1.63c.19-.15.24-.42.12-.64l-2-3.46' +
  'c-.12-.22-.39-.31-.61-.22l-2.49 1c-.52-.39-1.06-.73-1.69-.98l-.37-2.65A.506.506 0 0 0 14 2h-4' +
  'c-.25 0-.46.18-.5.42l-.37 2.65c-.63.25-1.17.59-1.69.98l-2.49-1c-.22-.09-.49 0-.61.22l-2 3.46' +
  'c-.13.22-.07.49.12.64L4.57 11c-.04.34-.07.67-.07 1 0 .33.03.65.07.97l-2.11 1.66' +
  'c-.19.15-.25.42-.12.64l2 3.46c.12.22.39.3.61.22l2.49-1.01c.52.4 1.06.74 1.69.99l.37 2.65' +
  'c.04.24.25.42.5.42h4c.25 0 .46-.18.5-.42l.37-2.65c.63-.26 1.17-.59 1.69-.99l2.49 1.01' +
  'c.22.08.49 0 .61-.22l2-3.46c.12-.22.07-.49-.12-.64l-2.11-1.66Z"/></svg>';

function updateUplinkUI() {
  const isWifi = selectedUplinkKind() === "wifi";
  $("wifiCreds").hidden = !isWifi;
  if (isWifi && wifiScannedFor !== selectedUplink) {
    scanWifi();
  }
}

async function scanWifi() {
  if (!selectedUplink || selectedUplinkKind() !== "wifi" || wifiScanning) return;
  wifiScanning = true;
  wifiScannedFor = selectedUplink;
  $("wifiSpinner").hidden = false;
  renderWifiNetworks();
  try {
    const res = await api(`/api/wifi/scan?iface=${encodeURIComponent(selectedUplink)}`);
    wifiNetworks = res.networks || [];
    wifiCurrentSsid = res.current || "";
    wifiScanError = res.error || "";
  } catch (e) {
    wifiNetworks = [];
    wifiScanError = e.message;
  } finally {
    wifiScanning = false;
    $("wifiSpinner").hidden = true;
    renderWifiNetworks();
  }
}

// Bir ağı oturum için seçer. Ubuntu'da tıklama anında bağlanır; burada bağlantı
// "Başlat"/"Bu uplink'e geç" ile kurulduğu için tıklama seçim yapar ve parola
// gerekiyorsa (kayıtlı değilse) GNOME'daki gibi parola penceresini açar.
function selectWifiNetwork(ssid, saved) {
  $("wifiSsid").value = ssid;
  if (saved) $("wifiPassword").value = "";
  renderWifiNetworks();
  updateActionButtons();
}

function onWifiRowClick(n) {
  const open = n.security === "open";
  if (n.saved || open) {
    selectWifiNetwork(n.ssid, true);
    return;
  }
  openWifiAuth(n.ssid);
}

function wifiSubtitle(n) {
  if (n.ssid === wifiCurrentSsid) return "Bağlandı";
  if (n.ssid === $("wifiSsid").value) return "Seçildi — bağlanmak için Başlat'a basın";
  if (n.saved && (n.signal === null || n.signal === undefined)) return "Kayıtlı · menzil dışı";
  if (n.saved) return "Kayıtlı";
  if (n.security === "open") return "Açık ağ";
  return "Güvenli (WPA)";
}

function renderWifiNetworks() {
  const box = $("wifiNetworkList");
  box.innerHTML = "";

  if (wifiScanning && !wifiNetworks.length) {
    const d = document.createElement("div");
    d.className = "wifi-empty";
    d.textContent = "Ağlar taranıyor…";
    box.appendChild(d);
    return;
  }
  if (!wifiNetworks.length) {
    const d = document.createElement("div");
    d.className = "wifi-empty" + (wifiScanError ? " is-error" : "");
    d.textContent = wifiScanError
      ? `Tarama başarısız: ${wifiScanError}`
      : "Menzilde ağ bulunamadı. ↻ ile yeniden tarayın.";
    box.appendChild(d);
    return;
  }

  // Bağlı ağ Ubuntu'da olduğu gibi her zaman en üstte.
  const list = [...wifiNetworks].sort((a, b) => {
    if (a.ssid === wifiCurrentSsid) return -1;
    if (b.ssid === wifiCurrentSsid) return 1;
    return 0;
  });

  const selectedSsid = $("wifiSsid").value;
  list.forEach((n) => {
    const isCurrent = n.ssid === wifiCurrentSsid;
    const row = document.createElement("div");
    row.className = "wifi-row"
      + (isCurrent ? " is-current" : "")
      + (n.ssid === selectedSsid && !isCurrent ? " is-selected" : "");

    const main = document.createElement("button");
    main.type = "button";
    main.className = "wifi-row-main";

    const icon = document.createElement("span");
    icon.className = "wifi-icon";
    icon.innerHTML = wifiSignalIcon(n.signal);

    const info = document.createElement("span");
    info.className = "wifi-info";
    const name = document.createElement("span");
    name.className = "wifi-name";
    name.textContent = n.ssid;          // SSID gövdeden gelir → textContent şart
    const sub = document.createElement("span");
    sub.className = "wifi-sub";
    sub.textContent = wifiSubtitle(n);
    info.append(name, sub);

    const meta = document.createElement("span");
    meta.className = "wifi-meta";
    if (n.security !== "open") meta.innerHTML = WIFI_LOCK_SVG;
    if (isCurrent) {
      const chk = document.createElement("span");
      chk.className = "wifi-check";
      chk.innerHTML = WIFI_CHECK_SVG;
      meta.appendChild(chk);
    }

    main.append(icon, info, meta);
    main.onclick = () => onWifiRowClick(n);
    row.appendChild(main);

    if (n.saved) {
      const gear = document.createElement("button");
      gear.type = "button";
      gear.className = "wifi-gear";
      gear.title = `"${n.ssid}" ağının ayarları`;
      gear.innerHTML = WIFI_GEAR_SVG;
      gear.onclick = (ev) => { ev.stopPropagation(); openWifiEdit(n); };
      row.appendChild(gear);
    }
    box.appendChild(row);
  });
}

async function forgetWifi(ssid) {
  if (!confirm(`"${ssid}" ağı unutulsun mu?\n\n` +
               "Kayıtlı parola silinir; tekrar bağlanmak için parolayı yeniden girmen gerekir."))
    return;
  try {
    await api("/api/wifi/forget", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ssid }),
    });
    if ($("wifiSsid").value === ssid) { $("wifiSsid").value = ""; $("wifiPassword").value = ""; }
    showToast(`"${ssid}" unutuldu.`, "info");
    scanWifi();
  } catch (e) {
    showToast("Ağ unutulamadı: " + e.message, "error");
  }
}

// ---- parola penceresi (GNOME "Authentication Required" muadili) ----
function openWifiAuth(ssid, opts = {}) {
  const hidden = !!opts.hidden;
  wifiAuthTarget = { ssid: ssid || "", hidden };
  $("wifiAuthSsid").textContent = hidden ? "Gizli ağ" : ssid;
  $("wifiAuthSsidRow").hidden = !hidden;
  $("wifiAuthSsidInput").value = "";
  $("wifiAuthPassword").value = "";
  $("wifiAuthPassword").type = "password";
  $("wifiAuthShow").checked = false;
  $("wifiAuthError").hidden = true;
  $("wifiAuth").hidden = false;
  updateWifiAuthState();
  setTimeout(() => $(hidden ? "wifiAuthSsidInput" : "wifiAuthPassword").focus(), 50);
}

function closeWifiAuth() {
  $("wifiAuth").hidden = true;
  wifiAuthTarget = null;
}

// Ubuntu'daki davranış: WPA parolası 8 karakterden kısayken "Bağlan" pasif.
function updateWifiAuthState() {
  if (!wifiAuthTarget) return;
  const pw = $("wifiAuthPassword").value;
  const ssidOk = wifiAuthTarget.hidden ? !!$("wifiAuthSsidInput").value.trim() : true;
  $("wifiAuthConnect").disabled = pw.length < 8 || !ssidOk;
}

function confirmWifiAuth() {
  if (!wifiAuthTarget) return;
  const ssid = wifiAuthTarget.hidden
    ? $("wifiAuthSsidInput").value.trim()
    : wifiAuthTarget.ssid;
  const pw = $("wifiAuthPassword").value;
  if (!ssid) {
    $("wifiAuthError").textContent = "Ağ adı gerekli.";
    $("wifiAuthError").hidden = false;
    return;
  }
  if (pw.length < 8) {
    $("wifiAuthError").textContent = "WPA parolası en az 8 karakter olmalı.";
    $("wifiAuthError").hidden = false;
    return;
  }
  $("wifiSsid").value = ssid;
  $("wifiPassword").value = pw;
  // Gizli ağ listede yoksa görünür kıl ki seçili olduğu belli olsun.
  if (!wifiNetworks.some((n) => n.ssid === ssid)) {
    wifiNetworks.push({ ssid, signal: null, security: "wpa", saved: false });
  }
  closeWifiAuth();
  renderWifiNetworks();
  updateActionButtons();
  showToast(`"${ssid}" seçildi — bağlanmak için Başlat'a basın.`, "info");
}

// ---- kayıtlı ağ ayarları penceresi (GNOME ağ ayarları muadili) ----
let wifiEditTarget = null;

function wifiSignalLabel(signal) {
  if (signal === null || signal === undefined) return "menzil dışı";
  let q = "zayıf";
  if (signal >= -55) q = "mükemmel";
  else if (signal >= -67) q = "iyi";
  else if (signal >= -78) q = "orta";
  return `${Math.round(signal)} dBm · ${q}`;
}

function openWifiEdit(n) {
  wifiEditTarget = n;
  $("wifiEditSecurity").textContent = n.security === "open" ? "Açık (parolasız)" : "WPA/WPA2";
  $("wifiEditSignal").textContent = wifiSignalLabel(n.signal);
  $("wifiEditStatus").textContent = n.ssid === wifiCurrentSsid ? "Bağlı" : "Kayıtlı";
  $("wifiEditSsid").value = n.ssid;
  $("wifiEditPassword").value = "";
  $("wifiEditPassword").type = "password";
  $("wifiEditShow").checked = false;
  $("wifiEditError").hidden = true;
  $("wifiEdit").hidden = false;
  setTimeout(() => $("wifiEditPassword").focus(), 50);
}

function closeWifiEdit() {
  $("wifiEdit").hidden = true;
  wifiEditTarget = null;
}

function wifiEditFail(msg) {
  $("wifiEditError").textContent = msg;
  $("wifiEditError").hidden = false;
}

async function saveWifiEdit() {
  if (!wifiEditTarget) return;
  const oldSsid = wifiEditTarget.ssid;
  const ssid = $("wifiEditSsid").value.trim();
  const password = $("wifiEditPassword").value;
  if (!ssid) return wifiEditFail("Ağ adı boş olamaz.");
  if (password && password.length < 8)
    return wifiEditFail("WPA parolası en az 8 karakter olmalı.");
  try {
    await api("/api/wifi/save", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ssid, old_ssid: oldSsid, password }),
    });
  } catch (e) {
    return wifiEditFail(e.message);
  }
  // Seçili ağ yeniden adlandırıldıysa seçimi de taşı.
  if ($("wifiSsid").value === oldSsid) $("wifiSsid").value = ssid;
  closeWifiEdit();
  showToast(`"${ssid}" ağ kaydı güncellendi.`, "success");
  wifiScannedFor = null;
  scanWifi();
}

$("wifiEditCloseBtn").onclick = closeWifiEdit;
$("wifiEditOverlay").onclick = closeWifiEdit;
$("wifiEditSave").onclick = saveWifiEdit;
$("wifiEditForget").onclick = () => {
  if (!wifiEditTarget) return;
  const ssid = wifiEditTarget.ssid;
  closeWifiEdit();
  forgetWifi(ssid);
};
// Kayıtlı parola yalnızca burada, açık istek üzerine sunucudan çekilir.
$("wifiEditShow").onchange = async (e) => {
  const field = $("wifiEditPassword");
  if (!e.target.checked) { field.type = "password"; return; }
  field.type = "text";
  if (field.value || !wifiEditTarget) return;
  try {
    const res = await api(`/api/wifi/secret?ssid=${encodeURIComponent(wifiEditTarget.ssid)}`);
    field.value = res.password || "";
  } catch (_) { /* kayıtlı parola yoksa alan boş kalır */ }
};
$("wifiEditPassword").onkeydown = (e) => { if (e.key === "Enter") saveWifiEdit(); };

// Bağlantı kurulduktan sonra "Bağlandı" etiketinin ve kayıtlı rozetlerinin
// güncellenmesi için listeyi tazeler. Association birkaç saniye sürebildiğinden
// kısa bir gecikmeyle yapılır.
function refreshWifiAfterConnect() {
  if (selectedUplinkKind() !== "wifi") return;
  wifiScannedFor = null;
  setTimeout(() => scanWifi(), 2500);
}

$("wifiScanBtn").onclick = () => scanWifi();
$("wifiHiddenBtn").onclick = () => openWifiAuth("", { hidden: true });
$("wifiAuthCloseBtn").onclick = closeWifiAuth;
$("wifiAuthCancel").onclick = closeWifiAuth;
$("wifiAuthOverlay").onclick = closeWifiAuth;
$("wifiAuthConnect").onclick = confirmWifiAuth;
$("wifiAuthPassword").oninput = updateWifiAuthState;
$("wifiAuthSsidInput").oninput = updateWifiAuthState;
$("wifiAuthShow").onchange = (e) => {
  $("wifiAuthPassword").type = e.target.checked ? "text" : "password";
};
$("wifiAuthPassword").onkeydown = (e) => { if (e.key === "Enter") confirmWifiAuth(); };
$("wifiAuthSsidInput").onkeydown = (e) => { if (e.key === "Enter") $("wifiAuthPassword").focus(); };

function updateActionButtons() {
  const running = !["idle", "stopping"].includes(currentPhase);
  $("startBtn").hidden = running;
  $("startBtn").disabled = busy || running;
  const canSwitch = running && selectedUplink && selectedUplink !== activeUplink;
  $("switchBtn").hidden = !running;
  $("switchBtn").disabled = busy || !canSwitch;
  $("switchHint").hidden = !running;
  $("stopBtn").disabled = busy || !running;
}

function wifiFields() {
  return {
    wifi_ssid: $("wifiSsid").value.trim(),
    wifi_password: $("wifiPassword").value,
  };
}

// ----------------------------------------------------------------- TOAST (U-11)
function showToast(message, type = "info") {
  const container = $("toastContainer");
  const toast = document.createElement("div");
  toast.className = `toast toast-${type}`;
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => { toast.classList.add("toast-hide"); setTimeout(() => toast.remove(), 300); }, 4000);
}

// ----------------------------------------------------------------- render
// Bir uplink etkinken FİZİKSEL olarak izole namespace'e taşınır (Model B);
// bu yüzden host'un arayüz listesinde (allInterfaces, /api/interfaces) artık
// GÖRÜNMEZ. Onu listeden düşürmek yerine, state.uplink'ten sentetik bir satır
// olarak ekliyoruz ki kullanıcı "bağlı olduğum arayüz nerede" diye kaybolmasın.
function classifyIfaceName(name) {
  if (/^(wl|wlan|wlp)/.test(name)) return "wifi";
  if (/^(usb|enx|rndis)/.test(name)) return "usb";
  if (/^(en|eth|enp)/.test(name)) return "ethernet";
  return "unknown";
}

function renderInterfaces(interfaces) {
  if (interfaces) allInterfaces = interfaces;
  const box = $("uplinkList");
  box.innerHTML = "";

  let list = allInterfaces;
  if (activeUplink && !list.some((i) => i.name === activeUplink)) {
    list = [{ name: activeUplink, kind: classifyIfaceName(activeUplink), state: "UP" }, ...list];
  }

  if (!list.length) {
    box.innerHTML = '<span class="chip empty">arayüz yok</span>';
    return;
  }
  list.forEach((i) => {
    const el = document.createElement("button");
    const isActive = i.name === activeUplink;
    el.className = "chip"
      + (selectedUplink === i.name ? " selected" : "")
      + (isActive ? " connected" : "");
    // U-14: dostça etiket
    const friendlyName = friendlyInterfaceName(i);
    el.innerHTML = `${friendlyName}<span class="kind">${i.kind}${isActive ? " • ✓ bağlı" : ""}</span>`;
    el.onclick = () => {
      selectedUplink = i.name;
      renderInterfaces();
      updateUplinkUI();
      updateActionButtons();
    };
    box.appendChild(el);
  });
  updateUplinkUI();
}

// U-14: Arayüz dostça adlandırma
function friendlyInterfaceName(i) {
  if (i.kind === "usb" || i.name.startsWith("enx") || i.name.startsWith("rndis"))
    return `📱 ${i.name}`;
  if (i.kind === "wifi") return `📶 ${i.name}`;
  if (i.kind === "ethernet") return `🔌 ${i.name}`;
  return i.name;
}

// U-14: Uygulama simgeleri
function appIcon(name) {
  const icons = {
    "google-chrome": "🌐", "google-chrome-stable": "🌐",
    "chromium": "🌐", "chromium-browser": "🌐",
    "brave-browser": "🦁", "opera": "🎭", "firefox": "🦊",
    "code": "💻", "terminator": "🖥️", "xterm": "🖥️",
  };
  return icons[name] || "⚙️";
}

// Profil içe aktarmayı destekleyen uygulamalar (tether_isolator/apps.py
// _HOST_PROFILE_DIRS + firefox ile birebir eşleşir).
const IMPORTABLE_PROFILE_APPS = [
  "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
  "brave-browser", "opera", "firefox", "code",
];

function renderImportAppSelect(installed) {
  const sel = $("importAppSelect");
  const list = (installed || []).filter((a) => IMPORTABLE_PROFILE_APPS.includes(a));
  sel.innerHTML = "";
  if (!list.length) {
    sel.innerHTML = '<option value="">(desteklenen uygulama bulunamadı)</option>';
    $("importProfileBtn").disabled = true;
    return;
  }
  list.forEach((a) => {
    const o = document.createElement("option");
    o.value = a; o.textContent = a;
    sel.appendChild(o);
  });
  $("importProfileBtn").disabled = false;
}

$("importProfileBtn").onclick = () => withBusy(async () => {
  const app = $("importAppSelect").value;
  if (!app) throw new Error("Uygulama seçin.");
  if (!confirm(
    `'${app}' için host'taki GERÇEK profil izole profile kopyalanacak.\n\n` +
    `• '${app}' hem host'ta hem izole alanda KAPALI olmalı.\n` +
    `• Mevcut izole profil silinmez, yedeklenir.\n\n` +
    `Devam edilsin mi?`))
    return;
  const res = await api("/api/import-profile", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ app }),
  });
  showToast(`${app} profili içe aktarıldı.`, "info");
});

// Özel uygulama ekleme: bir .desktop kısayolundan Exec= (ve varsa Name=) okunur.
// Not: masaüstü giriş biçimi spesifikasyonundaki tam alan-kodu kaçışları
// (\\, \", \$ vb.) uygulanmaz — burada yalnızca %f/%F/%u/%U gibi argüman yer
// tutucuları temizlenir; pratikte kısayolların büyük çoğunluğu için yeterlidir.
function parseDesktopFile(text) {
  let name = "", command = "";
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!name && line.startsWith("Name=")) name = line.slice(5).trim();
    if (!command && line.startsWith("Exec=")) command = line.slice(5).trim();
    if (name && command) break;
  }
  command = command.replace(/%[fFuUdDnNickvm]/g, "").replace(/\s+/g, " ").trim();
  return { name, command };
}

async function addCustomApp(name, command) {
  if (!command) throw new Error("Komut boş olamaz.");
  await api("/api/apps/custom/add", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, command }),
  });
  showToast(`'${name || command}' uygulama listesine eklendi.`, "info");
}

// ------------------------------------------------------- Uygulama Ekle ekranı
let desktopAppsCache = null;

function closeAppPicker() {
  $("appPicker").hidden = true;
}

$("appPickerCloseBtn").addEventListener("click", closeAppPicker);
$("appPickerOverlay").addEventListener("click", closeAppPicker);

async function openAppPicker() {
  $("appPicker").hidden = false;
  $("appPickerSearch").value = "";
  $("manualAppName").value = "";
  $("manualAppCommand").value = "";
  $("appPickerSearch").focus();
  $("appPickerResults").innerHTML = '<p class="muted hint-sm" style="padding:12px 20px">Yükleniyor…</p>';
  try {
    const res = await api("/api/apps/desktop");
    desktopAppsCache = res.apps || [];
  } catch (e) {
    desktopAppsCache = [];
    $("appPickerResults").innerHTML =
      `<p class="muted hint-sm" style="padding:12px 20px">Liste alınamadı: ${escapeHtml(e.message)}</p>`;
    return;
  }
  filterAppPicker("");
}

function filterAppPicker(query) {
  const q = query.trim().toLowerCase();
  const list = desktopAppsCache || [];
  const filtered = q ? list.filter((a) => a.name.toLowerCase().includes(q) ||
                                          a.command.toLowerCase().includes(q)) : list;
  const results = $("appPickerResults");
  results.innerHTML = "";
  if (!filtered.length) {
    results.innerHTML = '<p class="muted hint-sm" style="padding:12px 20px">Eşleşme yok — aşağıdan elle ekleyin.</p>';
    return;
  }
  filtered.slice(0, 200).forEach((a) => {
    const el = document.createElement("button");
    el.className = "cp-item";
    el.textContent = a.name;
    el.title = a.command;
    el.onclick = () => withBusy(async () => {
      await addCustomApp(a.name, a.command);
      closeAppPicker();
    });
    results.appendChild(el);
  });
}

$("addAppBtn").onclick = () => openAppPicker();
$("appPickerSearch").oninput = (e) => filterAppPicker(e.target.value);

$("appPickerFileBtn").onclick = () => { $("appFileInput").click(); };

$("appFileInput").onchange = (e) => {
  const file = e.target.files[0];
  e.target.value = ""; // aynı dosyayı tekrar seçebilmek için
  if (!file) return;
  const reader = new FileReader();
  reader.onload = (ev) => withBusy(async () => {
    const { name, command } = parseDesktopFile(String(ev.target.result));
    if (!command) throw new Error("Kısayolda Exec= satırı bulunamadı.");
    await addCustomApp(name, command);
    closeAppPicker();
  });
  reader.readAsText(file);
};

$("manualAppAddBtn").onclick = () => withBusy(async () => {
  const name = $("manualAppName").value.trim();
  const command = $("manualAppCommand").value.trim();
  await addCustomApp(name, command);
  closeAppPicker();
});

function renderApps(installed, custom) {
  installedApps = installed || installedApps;
  if (custom) {
    customAppNames = {};
    custom.forEach((c) => { customAppNames[c.command] = c.name; });
  }
  const customCommands = Object.keys(customAppNames)
    .filter((c) => !installedApps.includes(c));
  const allCommands = [...installedApps, ...customCommands];
  const box = $("appList");
  box.innerHTML = "";
  if (!allCommands.length) {
    box.innerHTML = '<span class="chip empty">uygulama bulunamadı</span>';
    return;
  }
  const live = !["idle", "stopping"].includes(currentPhase);
  allCommands.forEach((a) => {
    const isCustom = Object.prototype.hasOwnProperty.call(customAppNames, a);
    const label = isCustom ? customAppNames[a] : a;
    const el = document.createElement("button");
    el.className = "chip" + (!live && selectedApps.has(a) ? " selected" : "");
    el.title = live ? "Şimdi çalıştır" : "Başlatmak için seç";
    el.appendChild(document.createTextNode(`${isCustom ? "🧩" : appIcon(a)} ${label}`));
    if (isCustom) {
      const del = document.createElement("span");
      del.textContent = " ×";
      del.title = "Uygulamayı listeden kaldır";
      del.style.opacity = "0.6";
      del.onclick = (ev) => {
        ev.stopPropagation();
        withBusy(async () => {
          if (!confirm(`'${label}' listeden kaldırılsın mı?`)) return;
          await api("/api/apps/custom/delete", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ command: a }),
          });
          selectedApps.delete(a);
        });
      };
      el.appendChild(del);
    }
    el.onclick = () => {
      if (live) {
        withBusy(async () => {
          await api("/api/launch", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ app: a }),
          });
        });
      } else {
        selectedApps.has(a) ? selectedApps.delete(a) : selectedApps.add(a);
        renderApps();
      }
    };
    box.appendChild(el);
  });
  const lbl = document.querySelector("#controlCard .field-label:nth-of-type(3)");
  if (lbl) lbl.textContent = live
    ? "Uygulamalar (tıkla = şimdi çalıştır)"
    : "Başlatılacak uygulamalar";
}

function renderProfiles(profs, active) {
  profiles = profs;
  activeProfileName = active;
  const sel = $("profileSelect");
  sel.innerHTML = "";
  Object.keys(profs).forEach((name) => {
    const o = document.createElement("option");
    o.value = name; o.textContent = name;
    if (name === active) o.selected = true;
    sel.appendChild(o);
  });
}

function fmt(v, dash = "—") { return v === undefined || v === null || v === "" ? dash : v; }

function formatBytes(bytes) {
  if (bytes === 0) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + " " + sizes[i];
}

function renderState(d) {
  const st = d.state;
  document.body.dataset.phase = st.phase;
  if (st.phase !== currentPhase) {
    currentPhase = st.phase;
    renderApps();
  }
  const meta = PHASE[st.phase] || { label: st.phase, hint: "" };
  $("phaseText").textContent = meta.label;
  $("phaseHint").textContent = meta.hint;

  $("mPublicIp").textContent = fmt(st.public_ip);
  $("mIp").textContent = fmt(st.ip_address);
  $("mReconnect").textContent = st.reconnect_count || 0;
  $("mApps").textContent = (st.apps || []).filter((a) => a.running).length;

  // Trafik sayacı (U-6)
  const traffic = st.traffic_rx !== undefined;
  $("trafficMetric").hidden = !traffic || currentPhase === "idle";
  if (traffic) {
    const total = (st.traffic_rx || 0) + (st.traffic_tx || 0);
    $("mTraffic").textContent = total ? formatBytes(total) : "0 B";
  }

  $("dNamespace").textContent = fmt(st.namespace);
  $("dUplink").innerHTML = st.uplink
    ? `${st.uplink} <span class="pill ${st.uplink_present ? "ok" : "bad"}">${st.uplink_present ? "var" : "yok"}</span>`
    : "—";
  $("dGateway").textContent = fmt(st.gateway);
  $("dOnline").innerHTML = `<span class="pill ${st.online ? "ok" : "bad"}">${st.online ? "evet" : "hayır"}</span>`;

  // İzolasyon kanıtı (U-3)
  renderTrustBadge(st);

  const running = !["idle"].includes(st.phase);
  activeUplink = st.uplink || null;
  updateActionButtons();
  $("reconnectBtn").disabled = busy || !running;
  $("speedTestBtn").disabled = busy || !running || !st.online;
  $("relayToggle").disabled = busy || !running;
  $("relayToggle").checked = !!st.relay_active;
  renderRelayTargets((d.profiles?.[d.active_profile]?.relay?.extra_targets) || []);

  // Çoklu VPN Render
  if (d.vpns) {
    renderVpnList(d.vpns, st.vpns || [], running);
  }

  // Veri tasarrufu (Faz 1+2+3)
  renderDataSaver((d.profiles?.[d.active_profile]?.data_saver) || {}, st, running);

  const routes = st.relay_targets || [];
  if (st.relay_active) {
    $("relayHint").textContent = "Açık — kurum ağı erişimi var, internet tether'de.";
    $("relayRoutes").textContent = routes.length
      ? `Aynalanan ağlar: ${routes.join(", ")}`
      : "Uyarı: LAN'a bağlı görünmüyor (ethernet takılı mı?).";
  } else {
    $("relayHint").textContent = "Kapalıyken tam izole.";
    $("relayRoutes").textContent = "";
  }

  renderEvents(st.events || []);
}

// U-3: İzolasyon kanıtı
function renderTrustBadge(st) {
  const badge = $("trustBadge");
  const isOnline = st.phase === "online" || st.phase === "degraded";
  if (!isOnline || !st.public_ip) {
    badge.hidden = true;
    return;
  }
  badge.hidden = false;
  const icon = $("trustIcon") || badge.querySelector(".trust-icon");
  const text = $("trustText") || badge.querySelector(".trust-text");
  const detail = $("trustDetail") || badge.querySelector(".trust-detail");

  if (hostPublicIp && hostPublicIp !== st.public_ip) {
    icon.textContent = "✅";
    text.textContent = "İzole — işin PC'nin hattından çıkmıyor";
    detail.textContent = `İzole IP: ${st.public_ip} · PC IP: ${hostPublicIp} — farklı, yani ayrı hattasın.`;
    badge.className = "trust-badge trust-ok";
  } else if (hostPublicIp && hostPublicIp === st.public_ip) {
    icon.textContent = "❌";
    text.textContent = "Dikkat: izolasyon doğrulanamadı";
    detail.textContent = `İzole IP ve PC IP aynı: ${st.public_ip}`;
    badge.className = "trust-badge trust-warn";
  } else {
    icon.textContent = "ℹ️";
    text.textContent = "İzole — dış IP henüz doğrulanmadı";
    detail.textContent = "";
    badge.className = "trust-badge trust-info";
  }
}

function renderEvents(events) {
  const box = $("eventLog");
  if (!events || events.length === 0) {
    if (!box.querySelector(".empty-msg")) {
      box.innerHTML = '<p class="muted empty-msg">Henüz olay yok.</p>';
    }
    return;
  }

  const newestTimestamp = events[events.length - 1].t;
  if (newestTimestamp <= lastEventTime) return;
  lastEventTime = newestTimestamp;

  const placeholder = box.querySelector(".empty-msg");
  if (placeholder) placeholder.remove();

  const lastKnown = events.findLastIndex(e => e.t <= lastEventTime - 1);
  const newEvents = events.slice(lastKnown + 1);

  const wasAtBottom = box.scrollHeight - box.scrollTop <= box.clientHeight + 50;
  newEvents.forEach((e) => {
    // Filtre (U-8)
    if (eventFilter !== "all" && e.level !== eventFilter) return;
    const row = document.createElement("div");
    row.className = "event " + (e.level || "info");
    const t = new Date(e.t * 1000).toLocaleTimeString("tr-TR");
    row.innerHTML = `<span class="time">${t}</span><span class="msg">${escapeHtml(e.msg)}</span>`;
    box.appendChild(row);
  });

  if (wasAtBottom) {
    box.scrollTop = box.scrollHeight;
  }

  while (box.children.length > MAX_EVENTS) {
    box.removeChild(box.firstChild);
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

// ----------------------------------------------------------------- poll
let firstLoad = true;
let lastAppsSig = "";
async function poll() {
  try {
    const d = await api("/api/status");
    $("connState").textContent = "● daemon bağlı";
    $("connState").className = "conn-state live";
    $("versionBadge").textContent = "v" + d.version;
    const rb = $("rootBadge");
    if (d.dry_run) { rb.textContent = "DRY-RUN"; rb.className = "badge warn"; }
    else if (d.is_root) { rb.textContent = "root"; rb.className = "badge"; }
    else { rb.textContent = "yetki yok"; rb.className = "badge warn"; }

    // custom_apps eklenip/silinebildiği için yalnızca ilk yüklemede değil,
    // liste her değiştiğinde (add/delete sonrası poll'da) de yeniden çizilir.
    const appsSig = JSON.stringify([d.installed_apps, d.custom_apps]);
    if (appsSig !== lastAppsSig) {
      lastAppsSig = appsSig;
      renderApps(d.installed_apps, d.custom_apps);
      renderImportAppSelect(d.installed_apps);
    }

    if (firstLoad) {
      renderProfiles(d.profiles, d.active_profile);
      const ap = d.profiles[d.active_profile];
      if (ap) {
        $("useSystemProfile").checked = !!ap.use_system_profile;
        if (ap.wifi_ssid) selectWifiNetwork(ap.wifi_ssid, true);
      }
      firstLoad = false;
      // Host dış IP'sini al (U-3) — tek seferlik
      fetchHostPublicIp();
    }
    renderInterfaces(d.interfaces);
    renderState(d);
  } catch (e) {
    $("connState").textContent = "● daemon yok";
    $("connState").className = "conn-state dead";
  }
}

// U-3: Host dış IP'sini al
async function fetchHostPublicIp() {
  try {
    const r = await fetch("https://api.ipify.org?format=json");
    if (r.ok) {
      const data = await r.json();
      hostPublicIp = data.ip;
    }
  } catch (_) {
    // host IP alınamazsa trust badge gösterilmez
  }
}

// ----------------------------------------------------------------- aksiyon
async function withBusy(fn) {
  busy = true; poll();
  try { await fn(); }
  catch (e) { showToast(e.message, "error"); }
  finally { busy = false; await poll(); }
}

$("startBtn").onclick = () => withBusy(async () => {
  if (!selectedUplink) throw new Error("Önce bir uplink seçin.");
  const body = {
    profile: $("profileSelect").value,
    uplink: selectedUplink,
    apps: [...selectedApps],
    use_system_profile: $("useSystemProfile").checked,
    uplink_kind: selectedUplinkKind() || undefined,
  };
  if (selectedUplinkKind() === "wifi") Object.assign(body, wifiFields());
  await api("/api/start", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  refreshWifiAfterConnect();
});

$("switchBtn").onclick = () => withBusy(async () => {
  if (!selectedUplink) throw new Error("Önce geçilecek uplink'i seçin.");
  if (selectedUplink === activeUplink) throw new Error("Bu uplink zaten etkin.");
  if (!confirm(`Uplink '${activeUplink}' → '${selectedUplink}' olarak değişecek.\n` +
               `Uygulamalar kapanmaz, internet yeni uplink üzerinden devam eder. Onaylıyor musun?`))
    return;
  const body = { uplink: selectedUplink, uplink_kind: selectedUplinkKind() || undefined };
  if (selectedUplinkKind() === "wifi") Object.assign(body, wifiFields());
  await api("/api/switch-uplink", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  refreshWifiAfterConnect();
});

$("stopBtn").onclick = () => withBusy(async () => {
  await api("/api/stop", { method: "POST", headers: { "Content-Type": "application/json" } });
});

$("uplinkRefreshBtn").onclick = async () => {
  const btn = $("uplinkRefreshBtn");
  btn.classList.add("spinning");
  const started = Date.now();
  try {
    await poll();
  } finally {
    // en az 400ms göster ki anlık dönüşlerde de fark edilsin
    const wait = Math.max(0, 400 - (Date.now() - started));
    setTimeout(() => btn.classList.remove("spinning"), wait);
  }
};

$("relayToggle").onchange = (e) => withBusy(async () => {
  await api("/api/relay", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: e.target.checked }),
  });
});

// ------------------------------------------------------ Relay ek hedefler
function renderRelayTargets(targets) {
  const box = $("relayExtraList");
  box.innerHTML = "";
  if (!targets.length) {
    box.innerHTML = '<span class="chip empty">ek hedef yok</span>';
    return;
  }
  targets.forEach((t) => {
    const el = document.createElement("span");
    el.className = "chip";
    el.appendChild(document.createTextNode(`🌐 ${t}`));
    const del = document.createElement("span");
    del.textContent = " ×";
    del.title = "Listeden kaldır";
    del.style.opacity = "0.6";
    del.style.cursor = "pointer";
    del.onclick = (ev) => {
      ev.stopPropagation();
      withBusy(async () => {
        if (!confirm(`'${t}' listeden kaldırılsın mı?`)) return;
        await api("/api/relay/targets/delete", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ value: t }),
        });
      });
    };
    el.appendChild(del);
    box.appendChild(el);
  });
}

// ------------------------------------------------------- Veri tasarrufu
const LEVEL_LABEL = { light: "Hafif", balanced: "Dengeli", strict: "Katı" };
const LEVEL_CAP_KBIT = { light: [0, 0], balanced: [2000, 1000], strict: [700, 300] };
let usageToday = { rx: 0, tx: 0 };
let usageMonth = { rx: 0, tx: 0 };
let dataSaverQuotaFocused = false;
let dataSaverCapDownFocused = false;
let dataSaverCapUpFocused = false;
let lastDataSaver = {};

function renderDataSaver(ds, st, running) {
  lastDataSaver = ds || {};
  const enabled = !!ds.enabled;
  $("dataSaverToggle").checked = enabled;
  $("dataSaverToggle").disabled = busy;
  const level = ds.level || "balanced";
  document.querySelectorAll("#dataSaverLevels .level-chip").forEach((b) => {
    b.classList.toggle("active", b.dataset.level === level);
    b.disabled = busy;
  });
  $("dataSaverHint").textContent = enabled
    ? `Açık (${LEVEL_LABEL[level] || level}) — daemon prob trafiği kısıldı.`
    : "Kapalı — daemon prob trafiği normal sıklıkta.";

  const sessionTotal = (st.traffic_rx || 0) + (st.traffic_tx || 0);
  $("dsSession").textContent = running ? formatBytes(sessionTotal) : "—";
  const rate = (st.traffic_rate_rx || 0) + (st.traffic_rate_tx || 0);
  $("dsRate").textContent = running && rate ? formatBytes(rate) + "/s" : "—";

  if (!dataSaverQuotaFocused) {
    $("dataSaverQuota").value = ds.quota_mb || "";
  }
  $("dataSaverKillswitch").checked = ds.quota_action === "killswitch";

  const restartBtn = $("dataSaverRestartAppsBtn");
  restartBtn.disabled = busy || !running || !(st.apps || []).some((a) => a.running);

  renderMediaLevel(ds, st, running);

  if (!dataSaverCapDownFocused) $("dataSaverCapDown").value = ds.cap_down_kbit || "";
  if (!dataSaverCapUpFocused) $("dataSaverCapUp").value = ds.cap_up_kbit || "";
  renderDataSaverShapingText(ds, st, level);

  renderDataSaverUsage();
}

// Medya kademesi: sürgü konumu <-> sunucu değeri eşlemesi (soldan sağa).
const MEDIA_LEVELS = ["off", "144p", "360p", "720p", "blocked"];
const MEDIA_LEVEL_HINT = {
  off: "Sınırsız — video/müzik kısıtlaması yok.",
  "144p": "En düşük kalite — bant genişliği ~0,4 Mbit/s ile sınırlı.",
  "360p": "Düşük kalite — bant genişliği ~1 Mbit/s ile sınırlı.",
  "720p": "Orta kalite — bant genişliği ~3 Mbit/s ile sınırlı.",
  blocked: "Kapalı — video/ses hiç inmez (yeni açılan tarayıcılarda etkili).",
};
let mediaSliderDragging = false;

function renderMediaLevel(ds, st, running) {
  const level = MEDIA_LEVELS.includes(ds.media_level) ? ds.media_level : "off";
  const idx = MEDIA_LEVELS.indexOf(level);
  const slider = $("mediaLevelSlider");
  // Kullanıcı sürüklerken poll'un değeri geri almasını engelle.
  if (!mediaSliderDragging) slider.value = String(idx);
  slider.disabled = busy;
  $("mediaBlockHint").textContent = MEDIA_LEVEL_HINT[level];
  highlightMediaLabel(mediaSliderDragging ? Number(slider.value) : idx);
  renderMediaStaleWarning(level, st, running);
}

// Tarayıcı bayrakları/user.js YALNIZCA başlatma anında uygulanabilir. Kullanıcı
// sürgüyü çalışan bir tarayıcı varken değiştirirse o örnek eski ayarda kalır ve
// "kapalı dedim ama video hâlâ oynuyor" durumu oluşur. Bunu sessizce geçmek
// yerine açıkça söyleyip tek tıkla çözüm sunuyoruz.
function renderMediaStaleWarning(level, st, running) {
  const warn = $("mediaStaleWarn");
  const apps = (st.apps || []).filter((a) => a.running);
  // Yalnızca SERT engelin (Kapalı) tarayıcı tarafı vardır; 144p/360p/720p bant
  // genişliği tavanıyla çalışır ve canlı oturuma anında uygulanır.
  const browserSideMatters = (lv) => lv === "blocked";
  const stale = apps.filter((a) => {
    const launched = a.media_level || "off";
    if (launched === level) return false;
    return browserSideMatters(level) || browserSideMatters(launched);
  });
  if (!running || stale.length === 0) {
    warn.hidden = true;
    return;
  }
  warn.hidden = false;
  const names = [...new Set(stale.map((a) => a.command.split(/[\s/]/).pop()))].join(", ");
  $("mediaStaleText").textContent = level === "blocked"
    ? `“${names}” bu ayar seçilmeden önce açıldı; video engeli o pencerede geçerli değil.`
    : `“${names}” video engeli açıkken başlatıldı; o pencerede video hâlâ engelli.`;
  $("mediaStaleApplyBtn").disabled = busy;
}

function highlightMediaLabel(idx) {
  document.querySelectorAll(".media-range-labels span").forEach((el) => {
    el.classList.toggle("active", Number(el.dataset.idx) === idx);
  });
}

function renderDataSaverShapingText(ds, st, level) {
  const el = $("dataSaverCapText");
  if (!ds.enabled) {
    el.textContent = "";
    return;
  }
  const [defDown, defUp] = LEVEL_CAP_KBIT[level] || [0, 0];
  const down = ds.cap_down_kbit || defDown;
  const up = ds.cap_up_kbit || defUp;
  if (!down && !up) {
    el.textContent = "Bu seviyede bant genişliği tavanı yok (sınırsız).";
    return;
  }
  const wanted = [down ? `↓${down}` : null, up ? `↑${up}` : null].filter(Boolean).join(" / ") + " kbit/s";
  if (!st.shaping_active) {
    el.textContent = `Hedef: ${wanted} — henüz uygulanmadı (oturum/uplink bekleniyor).`;
    return;
  }
  const applied = [
    st.shaping_down_kbit ? `↓${st.shaping_down_kbit}kbit/s (${st.shaping_method || "?"})` : null,
    st.shaping_up_kbit ? `↑${st.shaping_up_kbit}kbit/s` : null,
  ].filter(Boolean).join(" · ");
  el.textContent = `Uygulanan: ${applied || "—"}`;
}

function renderDataSaverUsage() {
  $("dsToday").textContent = formatBytes((usageToday.rx || 0) + (usageToday.tx || 0));
  $("dsMonth").textContent = formatBytes((usageMonth.rx || 0) + (usageMonth.tx || 0));
  const quota = lastDataSaver.quota_mb || 0;
  const bar = $("dataSaverQuotaBar");
  if (quota > 0) {
    const usedMb = ((usageMonth.rx || 0) + (usageMonth.tx || 0)) / 1_000_000;
    const pct = Math.min(100, (usedMb / quota) * 100);
    bar.hidden = false;
    $("dataSaverQuotaFill").style.width = pct.toFixed(0) + "%";
    $("dataSaverQuotaFill").classList.toggle("danger", pct >= 100);
    $("dataSaverQuotaFill").classList.toggle("warn", pct >= 80 && pct < 100);
    $("dataSaverQuotaText").textContent = `${usedMb.toFixed(0)} / ${quota} MB (%${pct.toFixed(0)})`;
  } else {
    bar.hidden = true;
    $("dataSaverQuotaText").textContent = "";
  }
}

async function pollUsage() {
  try {
    const u = await api("/api/usage");
    usageToday = u.today || { rx: 0, tx: 0 };
    usageMonth = u.month || { rx: 0, tx: 0 };
    renderDataSaverUsage();
  } catch (_) {
    // sessiz geç — ana poll zaten bağlantı durumunu gösteriyor
  }
}

$("dataSaverToggle").onchange = (e) => withBusy(async () => {
  await api("/api/data-saver", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: e.target.checked }),
  });
});

// Sürüklerken yalnızca etiketi güncelle; sunucuya ancak bırakılınca yaz
// (aksi halde 0→3 arası her ara kademe için istek gider).
$("mediaLevelSlider").oninput = (e) => {
  mediaSliderDragging = true;
  highlightMediaLabel(Number(e.target.value));
  const lvl = MEDIA_LEVELS[Number(e.target.value)] || "off";
  $("mediaBlockHint").textContent = MEDIA_LEVEL_HINT[lvl];
};

$("mediaLevelSlider").onchange = (e) => {
  const media_level = MEDIA_LEVELS[Number(e.target.value)] || "off";
  withBusy(async () => {
    await api("/api/data-saver", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ media_level }),
    });
  }).finally(() => { mediaSliderDragging = false; });
};

document.querySelectorAll("#dataSaverLevels .level-chip").forEach((btn) => {
  btn.onclick = () => withBusy(async () => {
    await api("/api/data-saver", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ level: btn.dataset.level }),
    });
  });
});

$("dataSaverQuota").onfocus = () => { dataSaverQuotaFocused = true; };
$("dataSaverQuota").onblur = () => { dataSaverQuotaFocused = false; };

$("dataSaverQuotaSaveBtn").onclick = () => withBusy(async () => {
  const raw = $("dataSaverQuota").value.trim();
  const quota_mb = raw ? parseInt(raw, 10) : 0;
  if (raw && (!Number.isFinite(quota_mb) || quota_mb < 0)) {
    throw new Error("Geçersiz kota değeri.");
  }
  await api("/api/data-saver", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      quota_mb,
      quota_action: $("dataSaverKillswitch").checked ? "killswitch" : "warn",
    }),
  });
  showToast("Kota kaydedildi.", "info");
});

$("dataSaverCapDown").onfocus = () => { dataSaverCapDownFocused = true; };
$("dataSaverCapDown").onblur = () => { dataSaverCapDownFocused = false; };
$("dataSaverCapUp").onfocus = () => { dataSaverCapUpFocused = true; };
$("dataSaverCapUp").onblur = () => { dataSaverCapUpFocused = false; };

function _parseCapField(id) {
  const raw = $(id).value.trim();
  if (!raw) return 0;
  const n = parseInt(raw, 10);
  if (!Number.isFinite(n) || n < 0) throw new Error("Geçersiz tavan değeri.");
  return n;
}

$("dataSaverCapSaveBtn").onclick = () => withBusy(async () => {
  const cap_down_kbit = _parseCapField("dataSaverCapDown");
  const cap_up_kbit = _parseCapField("dataSaverCapUp");
  await api("/api/data-saver", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cap_down_kbit, cap_up_kbit }),
  });
  showToast("Bant genişliği tavanı kaydedildi.", "info");
});

$("mediaStaleApplyBtn").onclick = () => $("dataSaverRestartAppsBtn").click();

$("dataSaverRestartAppsBtn").onclick = () => withBusy(async () => {
  if (!confirm("Açık uygulamalar kapatılıp aynı listeyle yeniden başlatılacak. Devam edilsin mi?"))
    return;
  const r = await api("/api/apps/restart-all", {
    method: "POST", headers: { "Content-Type": "application/json" },
  });
  showToast(`${(r.started || []).length} uygulama yeniden başlatıldı.`, "info");
});

function closeNetPicker() { $("netPicker").hidden = true; }

function openNetPicker() {
  $("netPicker").hidden = false;
  $("netPickerInput").value = "";
  $("netPickerInput").focus();
}

$("addRelayTargetBtn").onclick = () => openNetPicker();
$("netPickerCloseBtn").addEventListener("click", closeNetPicker);
$("netPickerOverlay").addEventListener("click", closeNetPicker);

async function addRelayTarget() {
  const value = $("netPickerInput").value.trim();
  if (!value) return;
  await withBusy(async () => {
    await api("/api/relay/targets/add", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value }),
    });
  });
  closeNetPicker();
}

$("netPickerAddBtn").onclick = () => addRelayTarget();
$("netPickerInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); addRelayTarget(); }
});

$("reconnectBtn").onclick = () => withBusy(async () => {
  await api("/api/reconnect", { method: "POST", headers: { "Content-Type": "application/json" } });
});

$("addVpnBtn").onclick = () => {
  $("vpnFileInput").click();
};

$("vpnFileInput").onchange = async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  
  // Dosya adından uzantıyı çıkar
  const name = file.name.replace(".ovpn", "");
  
  const reader = new FileReader();
  reader.onload = async (ev) => {
    const content = ev.target.result;
    await withBusy(async () => {
      await api("/api/vpns/upload", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, content }),
      });
      showToast(`${name} başarıyla eklendi`, "info");
    });
  };
  reader.readAsText(file);
  e.target.value = ""; // Aynı dosyayı tekrar seçebilmek için
};

function renderVpnList(allVpns, activeStates, isRunning) {
  const list = $("vpnList");
  list.innerHTML = "";
  
  if (allVpns.length === 0) {
    list.innerHTML = '<p class="muted hint-sm">Henüz VPN eklenmedi.</p>';
  }
  
  $("addVpnBtn").disabled = busy;
  
  allVpns.forEach(vpnName => {
    const st = activeStates.find(v => v.name === vpnName) || { active: false };
    
    const row = document.createElement("div");
    row.className = "vpn-row";
    row.style = "display: flex; justify-content: space-between; align-items: center; padding: 8px 0; border-bottom: 1px solid var(--border);";
    
    const left = document.createElement("div");
    
    const title = document.createElement("div");
    title.style = "font-weight: 500; display: flex; align-items: center; gap: 8px;";
    title.textContent = vpnName;
    
    const delBtn = document.createElement("button");
    delBtn.className = "btn ghost-btn";
    delBtn.style = "padding: 2px 6px; font-size: 10px; color: var(--danger);";
    delBtn.textContent = "Sil";
    delBtn.disabled = busy;
    delBtn.onclick = () => withBusy(async () => {
      if (!confirm(`'${vpnName}' silinsin mi?`)) return;
      await api("/api/vpns/delete", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: vpnName })
      });
    });
    title.appendChild(delBtn);
    left.appendChild(title);
    
    const hint = document.createElement("div");
    hint.className = "muted hint-sm";
    hint.textContent = st.active 
      ? `Bağlı — ${fmt(st.iface)} ${fmt(st.ip)}` 
      : "Kapalı";
    left.appendChild(hint);
    
    const right = document.createElement("div");
    const label = document.createElement("label");
    label.className = "switch";
    
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = st.active;
    cb.disabled = busy || !isRunning;
    
    cb.onchange = (e) => withBusy(async () => {
      await api("/api/vpn", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: vpnName, enabled: e.target.checked })
      });
    });
    
    const slider = document.createElement("span");
    slider.className = "slider";
    
    label.appendChild(cb);
    label.appendChild(slider);
    right.appendChild(label);
    
    row.appendChild(left);
    row.appendChild(right);
    list.appendChild(row);
  });
}

// U-7: Hız testi
$("speedTestBtn").onclick = () => withBusy(async () => {
  const resultBox = $("speedTestResult");
  resultBox.hidden = false;
  resultBox.innerHTML = '<span class="spinner"></span> Hız testi yapılıyor (10MB)...';
  try {
    const res = await api("/api/speedtest", { method: "POST", headers: { "Content-Type": "application/json" } });
    if (res.ok) {
      resultBox.innerHTML = `<span class="ok">⚡ İndirme Hızı: ${res.mbps} Mbps</span>`;
    } else {
      throw new Error(res.error || "Hata oluştu");
    }
  } catch (e) {
    resultBox.innerHTML = `<span class="bad">Hız testi başarısız: ${escapeHtml(e.message)}</span>`;
  }
  setTimeout(() => { resultBox.hidden = true; }, 10000);
});

$("restartBtn").onclick = async () => {
  if (!confirm("Her şey sıfırlansın mı?\n\nİzole oturum, çalışan uygulamalar, relay " +
               "ve VPN TAMAMEN durdurulacak; ardından uygulama temiz bir durumla " +
               "yeniden başlayacak. Panel birkaç saniye içinde kendini yeniden açar.\n\n" +
               "(Oturumu korumak için 'Durdur' yerine 'Çıkış' kullan.)"))
    return;
  try {
    await api("/api/restart", { method: "POST", headers: { "Content-Type": "application/json" } });
  } catch (_) { /* daemon zaten yeniden başlıyor, bağlantı kopması beklenir */ }
  clearInterval(pollTimer);
  document.body.innerHTML =
    '<div style="display:grid;place-items:center;height:100vh;font-family:' +
    "var(--font);color:var(--muted);text-align:center\">" +
    "<div><h1 style='font-size:28px'>⟳</h1>" +
    "<p>Tether Isolator yeniden başlatılıyor…</p>" +
    "<p style='font-size:13px'>Bu sayfa birkaç saniye içinde otomatik yenilenecek.</p></div></div>";
  // daemon soketi kapatıp execv ile kendini yeniden başlatırken bir süre yanıt
  // vermez; port tekrar ayağa kalkana kadar birkaç saniyede bir dene.
  const tryReload = () => {
    fetch(location.pathname).then(() => location.reload()).catch(() => setTimeout(tryReload, 1500));
  };
  setTimeout(tryReload, 2000);
};

$("quitBtn").onclick = async () => {
  if (!confirm("Panel kapatılsın mı?\n\nİzole oturum ve uygulamalar ÇALIŞMAYA " +
               "DEVAM EDER; paneli tekrar açınca kaldığın yerden devam eder.\n" +
               "Tamamen durdurmak için önce 'Durdur' kullanın."))
    return;
  try {
    await api("/api/quit", { method: "POST", headers: { "Content-Type": "application/json" } });
  } catch (_) { /* sunucu zaten kapandı */ }
  document.body.innerHTML =
    '<div style="display:grid;place-items:center;height:100vh;font-family:' +
    "var(--font);color:var(--muted);text-align:center\">" +
    "<div><h1 style='font-size:28px'>👋</h1>" +
    "<p>Tether Isolator kapatıldı.</p>" +
    "<p style='font-size:13px'>Bu sekmeyi kapatabilirsiniz.</p></div></div>";
  clearInterval(pollTimer);
};

// U-15: Tema değiştirme
$("themeToggle").onclick = () => {
  const body = document.body;
  const current = body.dataset.theme;
  if (current === "auto") {
    body.dataset.theme = "dark";
  } else if (current === "dark") {
    body.dataset.theme = "light";
  } else {
    body.dataset.theme = "auto";
  }
  showToast(`Tema: ${body.dataset.theme}`, "info");
};

// U-13: Komut paleti
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "k") {
    e.preventDefault();
    togglePalette();
  }
  if (e.key === "Escape") {
    closePalette(); closeAppPicker(); closeNetPicker(); closeWifiAuth(); closeWifiEdit();
  }
});

function togglePalette() {
  const p = $("commandPalette");
  p.hidden = !p.hidden;
  if (!p.hidden) {
    $("cpInput").value = "";
    $("cpInput").focus();
    filterPalette("");
  }
}

function closePalette() {
  $("commandPalette").hidden = true;
}

$("commandPaletteCloseBtn").addEventListener("click", closePalette);
$("commandPaletteOverlay").addEventListener("click", closePalette);

function filterPalette(query) {
  const q = query.toLowerCase();
  const commands = [
    { label: "Başlat", action: () => $("startBtn").click(), shortcut: "" },
    { label: "Durdur", action: () => $("stopBtn").click(), shortcut: "" },
    { label: "Relay aç", action: () => { if (!$("relayToggle").disabled) { $("relayToggle").checked = true; $("relayToggle").dispatchEvent(new Event("change")); } }, shortcut: "" },
    { label: "Relay kapat", action: () => { if (!$("relayToggle").disabled) { $("relayToggle").checked = false; $("relayToggle").dispatchEvent(new Event("change")); } }, shortcut: "" },
    { label: "Veri tasarrufu aç", action: () => { if (!$("dataSaverToggle").disabled) { $("dataSaverToggle").checked = true; $("dataSaverToggle").dispatchEvent(new Event("change")); } }, shortcut: "" },
    { label: "Veri tasarrufu kapat", action: () => { if (!$("dataSaverToggle").disabled) { $("dataSaverToggle").checked = false; $("dataSaverToggle").dispatchEvent(new Event("change")); } }, shortcut: "" },
    ...MEDIA_LEVELS.map((lvl, i) => ({
      label: "Video/müzik: " + ["sınırsız", "144p", "360p", "720p", "kapalı"][i],
      action: () => {
        const s = $("mediaLevelSlider");
        if (s.disabled) return;
        s.value = String(i);
        s.dispatchEvent(new Event("change"));
      },
      shortcut: "",
    })),
    { label: "Yeniden bağlan", action: () => $("reconnectBtn").click(), shortcut: "" },
    { label: "VPN içe aktar", action: () => { if (!$("addVpnBtn").disabled) $("addVpnBtn").click(); }, shortcut: "" },
    { label: "Hız testi", action: () => $("speedTestBtn").click(), shortcut: "" },
    { label: "Tema değiştir", action: () => $("themeToggle").click(), shortcut: "🌓" },
  ];
  const results = $("cpResults");
  results.innerHTML = "";
  const filtered = q ? commands.filter(c => c.label.toLowerCase().includes(q)) : commands;
  filtered.forEach(c => {
    const el = document.createElement("button");
    el.className = "cp-item";
    el.textContent = c.label;
    el.onclick = () => { closePalette(); c.action(); };
    results.appendChild(el);
  });
  if (!filtered.length) {
    results.innerHTML = '<p class="muted" style="padding:12px;text-align:center">Sonuç yok</p>';
  }
}

$("cpInput").oninput = (e) => filterPalette(e.target.value);

// U-8: Olay filtresi
document.querySelectorAll(".filter-chip").forEach(chip => {
  chip.onclick = () => {
    document.querySelectorAll(".filter-chip").forEach(c => c.classList.remove("active"));
    chip.classList.add("active");
    eventFilter = chip.dataset.filter;
    // Mevcut olayları yeniden render et
    lastEventTime = 0; // force re-render
  };
});

// ----------------------------------------------------------------- başlat
poll();
pollUsage();
setInterval(pollUsage, 15000);
const pollTimer = setInterval(poll, 2000);
