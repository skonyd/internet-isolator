// Tether Isolator — arayüz mantığı (bağımlılıksız, vanilla JS)
"use strict";

const $ = (id) => document.getElementById(id);
const api = async (path, opts) => {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
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
let allInterfaces = [];
let lastEventTime = 0;
const MAX_EVENTS = 50;
let activeUplink = null;   // o an etkin (canlı) uplink

function selectedUplinkKind() {
  const i = allInterfaces.find((x) => x.name === selectedUplink);
  return i ? i.kind : "";
}

// WiFi seçiliyse SSID/parola alanlarını göster
function updateUplinkUI() {
  $("wifiCreds").hidden = selectedUplinkKind() !== "wifi";
}

// Faza + seçime göre Başlat / Geç / Durdur düğmelerini ayarla
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

// ----------------------------------------------------------------- render
function renderInterfaces(interfaces) {
  if (interfaces) allInterfaces = interfaces;
  const box = $("uplinkList");
  box.innerHTML = "";
  if (!allInterfaces.length) {
    box.innerHTML = '<span class="chip empty">arayüz yok</span>';
    return;
  }
  allInterfaces.forEach((i) => {
    const el = document.createElement("button");
    const isActive = i.name === activeUplink;
    el.className = "chip" + (selectedUplink === i.name ? " selected" : "");
    el.innerHTML = `${i.name}<span class="kind">${i.kind}${isActive ? " • etkin" : ""}</span>`;
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

function renderApps(installed) {
  installedApps = installed || installedApps;
  const box = $("appList");
  box.innerHTML = "";
  if (!installedApps.length) {
    box.innerHTML = '<span class="chip empty">uygulama bulunamadı</span>';
    return;
  }
  const live = !["idle", "stopping"].includes(currentPhase);
  installedApps.forEach((a) => {
    const el = document.createElement("button");
    el.className = "chip" + (!live && selectedApps.has(a) ? " selected" : "");
    el.innerHTML = live ? `▶ ${a}` : a;
    el.title = live ? "Şimdi çalıştır" : "Başlatmak için seç";
    el.onclick = () => {
      if (live) {
        // Oturum açıkken: tek tıkla hemen başlat (ör. Opera'yı yeniden aç)
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
  // alan başlığı ipucu
  const lbl = document.querySelector("#controlCard .field-label:nth-of-type(3)");
  if (lbl) lbl.textContent = live
    ? "Uygulamalar (tıkla = şimdi çalıştır)"
    : "Başlatılacak uygulamalar";
}

function renderProfiles(profiles, active) {
  const sel = $("profileSelect");
  sel.innerHTML = "";
  Object.keys(profiles).forEach((name) => {
    const o = document.createElement("option");
    o.value = name; o.textContent = name;
    if (name === active) o.selected = true;
    sel.appendChild(o);
  });
}

function fmt(v, dash = "—") { return v === undefined || v === null || v === "" ? dash : v; }

function renderState(d) {
  const st = d.state;
  document.body.dataset.phase = st.phase;
  if (st.phase !== currentPhase) {
    currentPhase = st.phase;
    renderApps();   // çiplerin davranışı (seç ↔ çalıştır) değişir
  }
  const meta = PHASE[st.phase] || { label: st.phase, hint: "" };
  $("phaseText").textContent = meta.label;
  $("phaseHint").textContent = meta.hint;

  $("mPublicIp").textContent = fmt(st.public_ip);
  $("mIp").textContent = fmt(st.ip_address);
  $("mReconnect").textContent = st.reconnect_count || 0;
  $("mApps").textContent = (st.apps || []).filter((a) => a.running).length;

  $("dNamespace").textContent = fmt(st.namespace);
  $("dUplink").innerHTML = st.uplink
    ? `${st.uplink} <span class="pill ${st.uplink_present ? "ok" : "bad"}">${st.uplink_present ? "var" : "yok"}</span>`
    : "—";
  $("dGateway").textContent = fmt(st.gateway);
  $("dOnline").innerHTML = `<span class="pill ${st.online ? "ok" : "bad"}">${st.online ? "evet" : "hayır"}</span>`;

  // butonlar
  const running = !["idle"].includes(st.phase);
  activeUplink = st.uplink || null;
  updateActionButtons();
  $("reconnectBtn").disabled = busy || !running;
  $("relayToggle").disabled = busy || !running;
  $("relayToggle").checked = !!st.relay_active;
  $("relayExtra").disabled = busy || st.relay_active;   // açıkken kilitli (önce kapat)

  // VPN
  $("vpnToggle").disabled = busy || !running;
  $("vpnToggle").checked = !!st.vpn_active;
  $("vpnConfig").disabled = busy || st.vpn_active;
  $("vpnHint").textContent = st.vpn_active
    ? `Bağlı — ${fmt(st.vpn_iface)} ${fmt(st.vpn_ip)}`
    : "Kapalı. .ovpn ile uzak ağa bağlan.";

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

function renderEvents(events) {
  const box = $("eventLog");

  // Boşsa placeholder göster (sadece bir kez)
  if (!events || events.length === 0) {
    if (!box.querySelector(".empty-msg")) {
      box.innerHTML = '<p class="muted empty-msg">Henüz olay yok.</p>';
    }
    return;
  }

  // Yeni olay var mı kontrol et — son timestamp'ten sonrakileri ekle
  const newestTimestamp = events[events.length - 1].t;
  if (newestTimestamp <= lastEventTime) return;
  lastEventTime = newestTimestamp;

  // Placeholder'ı kaldır
  const placeholder = box.querySelector(".empty-msg");
  if (placeholder) placeholder.remove();

  // Son polledaki en son olaydan sonraki yeni olayları bul
  const lastKnown = events.findLastIndex(e => e.t <= lastEventTime - 1);
  const newEvents = events.slice(lastKnown + 1);

  // Yeni olayları listeye ekle (sırayla)
  const wasAtBottom = box.scrollHeight - box.scrollTop <= box.clientHeight + 50;
  newEvents.forEach((e) => {
    const row = document.createElement("div");
    row.className = "event " + (e.level || "info");
    const t = new Date(e.t * 1000).toLocaleTimeString("tr-TR");
    row.innerHTML = `<span class="time">${t}</span><span class="msg">${escapeHtml(e.msg)}</span>`;
    box.appendChild(row);
  });

  // Kullanıcı en altta ise yeni olaylarda da en altta kal (scroll takip)
  if (wasAtBottom) {
    box.scrollTop = box.scrollHeight;
  }

  // Maksimum olay sayısını aşınca en eskileri sil
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

    if (firstLoad) {
      renderProfiles(d.profiles, d.active_profile);
      renderApps(d.installed_apps);
      const ap = d.profiles[d.active_profile];
      if (ap) $("useSystemProfile").checked = !!ap.use_system_profile;
      firstLoad = false;
    }
    renderInterfaces(d.interfaces);
    renderState(d);
  } catch (e) {
    $("connState").textContent = "● daemon yok";
    $("connState").className = "conn-state dead";
  }
}

// ----------------------------------------------------------------- aksiyon
async function withBusy(fn) {
  busy = true; poll();
  try { await fn(); }
  catch (e) { alert("Hata: " + e.message); }
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
  await api("/api/stop", { method: "POST" });
});

$("relayToggle").onchange = (e) => withBusy(async () => {
  const body = { enabled: e.target.checked };
  if (e.target.checked) body.extra_targets = $("relayExtra").value.trim();
  await api("/api/relay", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
});

$("reconnectBtn").onclick = () => withBusy(async () => {
  await api("/api/reconnect", { method: "POST" });
});

$("vpnToggle").onchange = (e) => withBusy(async () => {
  const body = { enabled: e.target.checked };
  if (e.target.checked) {
    const cfg = $("vpnConfig").value.trim();
    if (!cfg) throw new Error(".ovpn dosya yolunu girin.");
    body.config = cfg;
  }
  await api("/api/vpn", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
});

$("quitBtn").onclick = async () => {
  if (!confirm("Panel kapatılsın mı?\n\nİzole oturum ve uygulamalar ÇALIŞMAYA " +
               "DEVAM EDER; paneli tekrar açınca kaldığın yerden devam eder.\n" +
               "Tamamen durdurmak için önce 'Durdur' kullanın."))
    return;
  try {
    await fetch("/api/quit", { method: "POST" });
  } catch (_) { /* sunucu zaten kapandı */ }
  document.body.innerHTML =
    '<div style="display:grid;place-items:center;height:100vh;font-family:' +
    "var(--font);color:var(--muted);text-align:center\">" +
    "<div><h1 style='font-size:28px'>👋</h1>" +
    "<p>Tether Isolator kapatıldı.</p>" +
    "<p style='font-size:13px'>Bu sekmeyi kapatabilirsiniz.</p></div></div>";
  clearInterval(pollTimer);
};

// başlat
poll();
const pollTimer = setInterval(poll, 2000);
