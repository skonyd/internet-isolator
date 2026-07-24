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

function updateUplinkUI() {
  $("wifiCreds").hidden = selectedUplinkKind() !== "wifi";
}

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
  $("trafficMetric").hidden = !traffic || !currentPhase !== "idle";
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
  $("relayExtra").disabled = busy || st.relay_active;

  // Çoklu VPN Render
  if (d.vpns) {
    renderVpnList(d.vpns, st.vpns || [], running);
  }

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
      if (ap) $("useSystemProfile").checked = !!ap.use_system_profile;
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
  const body = { enabled: e.target.checked };
  if (e.target.checked) body.extra_targets = $("relayExtra").value.trim();
  await api("/api/relay", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
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
  if (e.key === "Escape") { closePalette(); closeAppPicker(); }
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
const pollTimer = setInterval(poll, 2000);
