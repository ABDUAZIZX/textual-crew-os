"use strict";
(function () {
  const $ = (sel) => document.querySelector(sel);
  const el = (tag, cls, html) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html != null) e.innerHTML = html;
    return e;
  };
  const esc = (s) => String(s).replace(/[&<>"']/g, (m) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m]
  ));

  // Role presentation metadata (avatars are local SVGs, no CDN).
  const META = {
    supervisor_t1: { accent: "cyan", avatar: "supervisor", en: "Supervisor", ar: "المشرف" },
    supervisor_t2: { accent: "cyan", avatar: "supervisor", en: "Supervisor", ar: "المشرف" },
    coder: { accent: "blue", avatar: "coder", en: "Coder Agent", ar: "وكيل البرمجة" },
    sec_defensive: { accent: "green", avatar: "sec_defensive", en: "Sec-8B (Defensive)", ar: "الأمن الدفاعي" },
    sec_offensive: { accent: "yellow", avatar: "sec_offensive", en: "LAB_MODE (Offensive)", ar: "الوضع الهجومي" },
    auditor: { accent: "purple", avatar: "auditor", en: "Auditor", ar: "المدقّق" },
  };
  const TAG_BY_ACCENT = { blue: "delegation", green: "security", yellow: "lab", purple: "audit", cyan: "progress" };

  let lang = localStorage.getItem("crew_lang") || "en";
  let agents = [];
  let tasks = [];
  const histories = { cpu: [], ram: [], gpu: [], pwr: [] };
  let selectedRole = null;

  // theme state (persisted client-side, like crew_lang)
  let theme = localStorage.getItem("crew_theme") || "midnight";
  const LIGHT_THEMES = new Set(["solarized-light", "light"]);
  let prevDarkTheme = LIGHT_THEMES.has(theme) ? "midnight" : theme;
  let serverConfig = null;
  const THEMES = [
    ["midnight", "Midnight"], ["nord", "Nord"], ["gruvbox", "Gruvbox"],
    ["tokyo-night", "Tokyo Night"], ["dracula", "Dracula"],
    ["solarized-light", "Solarized Light"], ["light", "Light"],
  ];

  // chat state
  const newSessionId = () =>
    (window.crypto && crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(16).slice(2));
  let chatWs = null;
  let chatWsBackoff = 1000;
  let chatSession = newSessionId();
  let chatMode = "single";
  let chatAgents = [];          // available role values
  let chatBusy = false;
  let chatPending = 0;          // outstanding agent turns this send
  let chatTokens = 0;           // tokens accumulated this session
  const chatBubbles = {};       // role -> { el, parts: [] } while streaming

  // system views state
  let lastMetrics = null;
  let lastStatus = null;
  const liveEvents = [];        // capped buffer of WS frames for Live Activity

  // ─── i18n ────────────────────────────────────────────────
  function t(key) {
    const dict = window.I18N[lang] || window.I18N.en;
    return dict[key] != null ? dict[key] : key;
  }
  function applyLang() {
    document.documentElement.lang = lang;
    document.documentElement.dir = lang === "ar" ? "rtl" : "ltr";
    $("#lang-label").textContent = lang === "ar" ? "ع" : "EN";
    document.querySelectorAll("[data-i18n]").forEach((node) => {
      node.textContent = t(node.getAttribute("data-i18n"));
    });
    document.querySelectorAll("[data-i18n-ph]").forEach((node) => {
      node.setAttribute("placeholder", t(node.getAttribute("data-i18n-ph")));
    });
    renderAll();
  }

  // ─── helpers ─────────────────────────────────────────────
  function fmtTokens(n) {
    if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
    return String(n);
  }
  function fmtUptime(s) {
    s = Math.floor(s || 0);
    const h = String(Math.floor(s / 3600)).padStart(2, "0");
    const m = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
    const sec = String(s % 60).padStart(2, "0");
    return `${h}:${m}:${sec}`;
  }
  function nowTime() {
    return new Date().toLocaleTimeString("en-GB");
  }
  function roleLabel(role) {
    const m = META[role];
    return m ? m[lang] || m.en : role;
  }
  function avatarFor(role) {
    const m = META[role];
    return `/static/avatars/${(m && m.avatar) || "coder"}.svg`;
  }

  // ─── agents + sidebar ────────────────────────────────────
  function agentStatus(role) {
    const inProg = tasks.some((t) => t.assigned_to === role && t.status === "in_progress");
    return inProg ? "active" : "idle";
  }
  function completedFor(role) {
    return tasks.filter((t) => t.assigned_to === role && t.status === "completed").length;
  }

  function renderAgents() {
    const grid = $("#agent-grid");
    const nav = $("#nav-agents");
    if (!grid || !nav) return;
    grid.innerHTML = "";
    nav.innerHTML = "";
    agents.forEach((a) => {
      const meta = META[a.role] || { accent: "blue" };
      const st = agentStatus(a.role);
      const card = el("div", "agent-card");
      card.dataset.accent = meta.accent;
      card.dataset.role = a.role;
      if (a.role === selectedRole) card.classList.add("selected");
      card.innerHTML = `
        <div class="ac-head">
          <img src="${avatarFor(a.role)}" alt="">
          <div style="flex:1">
            <div class="ac-name">${roleLabel(a.role)}</div>
            <div class="ac-model">${a.model}</div>
          </div>
          <span class="badge ${st}">${t(st === "active" ? "running" : "idle")}</span>
        </div>
        <div class="ac-metrics">
          <div class="m"><div class="k">${t("model")}</div><div class="v" style="font-size:11px">${a.model.split(":")[0]}</div></div>
          <div class="m"><div class="k">${t("completed_tasks")}</div><div class="v">${completedFor(a.role)}</div></div>
          <div class="m"><div class="k">${t("capabilities")}</div><div class="v">${a.capabilities.length}</div></div>
        </div>`;
      card.addEventListener("click", () => { selectedRole = a.role; showView("overview"); renderAgents(); renderDetails(); });
      grid.appendChild(card);

      const item = el("div", "nav-item");
      item.innerHTML = `<img src="${avatarFor(a.role)}" width="18" height="18"><span>${roleLabel(a.role)}</span><span class="ndot ${meta.accent === "cyan" ? "blue" : meta.accent}"></span>`;
      item.addEventListener("click", () => { selectedRole = a.role; showView("overview"); renderAgents(); renderDetails(); });
      nav.appendChild(item);
    });
  }

  function renderDetails() {
    const idBox = $("#detail-id");
    const content = $("#detail-content");
    if (!idBox || !content) return;
    const a = agents.find((x) => x.role === selectedRole);
    if (!a) {
      idBox.innerHTML = "";
      content.innerHTML = `<p style="color:var(--muted)">${t("no_selection")}</p>`;
      return;
    }
    idBox.innerHTML = `
      <img src="${avatarFor(a.role)}" alt="">
      <div class="ac-name">${roleLabel(a.role)}</div>
      <div class="ac-model">${a.model}</div>
      <span class="badge ${agentStatus(a.role)}">${t(agentStatus(a.role) === "active" ? "running" : "idle")}</span>`;
    const current = tasks.find((x) => x.assigned_to === a.role && x.status === "in_progress");
    const caps = a.capabilities.map((c) => `<span class="usage-chip">${c}</span>`).join(" ") || "—";
    content.innerHTML = `
      <div><b>${t("capabilities")}:</b> ${caps}</div>
      <div style="margin-top:12px"><b>${t("current_task")}:</b> ${current ? current.description : t("no_task")}</div>`;
  }

  // ─── feed + comm bus (from WS task events) ───────────────
  function pushFeed(task) {
    const feed = $("#feed");
    if (!feed) return;
    const role = task.assigned_to || "supervisor_t1";
    const meta = META[role] || { accent: "blue" };
    const tag = TAG_BY_ACCENT[meta.accent] || "progress";
    const item = el("div", "feed-item");
    item.innerHTML = `
      <span class="fi-icon"><img src="${avatarFor(role)}" width="16" height="16"></span>
      <span class="fi-time">${nowTime()}</span>
      <div class="fi-body">
        <div class="fi-agent" style="color:var(--${meta.accent})">${roleLabel(role)}</div>
        <div class="fi-text">${task.status}: ${task.description}</div>
      </div>
      <span class="tag ${tag}">${tag}</span>`;
    feed.prepend(item);
    while (feed.children.length > 8) feed.removeChild(feed.lastChild);

    const cbus = $("#cbus");
    if (cbus) {
      const row = el("div", "cbus-item");
      row.innerHTML = `
        <img src="${avatarFor(role)}" width="18" height="18">
        <div><div class="cbus-route">Supervisor → ${roleLabel(role)}</div>
        <div class="cbus-action">${task.description}</div></div>
        <span class="cbus-time">${nowTime()}</span>`;
      cbus.prepend(row);
      while (cbus.children.length > 6) cbus.removeChild(cbus.lastChild);
    }
  }

  // ─── usage bar ───────────────────────────────────────────
  function renderUsage(report) {
    if (!report) return;
    const s = report.session, w = report.week;
    $("#usage-session").innerHTML = `${fmtTokens(s.total_tokens)} tok · <span class="cost">$${s.cost_usd.toFixed(2)}</span>`;
    $("#usage-week").innerHTML = `${fmtTokens(w.total_tokens)} tok · <span class="cost">$${w.cost_usd.toFixed(2)}</span>`;
    const box = $("#usage-models");
    box.innerHTML = "";
    w.per_model.forEach((m) => {
      const chip = el("span", "usage-chip", `<b>${m.model.split(":")[0]}</b> ${fmtTokens(m.total_tokens)} · ${m.calls} ${t("calls")}`);
      box.appendChild(chip);
    });
    $("#t-tokens").textContent = fmtTokens(s.total_tokens);
  }

  // ─── sparklines ──────────────────────────────────────────
  function drawSpark(canvas, data, color) {
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth, h = canvas.clientHeight || 44;
    canvas.width = w * dpr; canvas.height = h * dpr;
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, w, h);
    if (data.length < 2) return;
    const max = Math.max(100, ...data), min = 0;
    const step = w / (data.length - 1);
    ctx.beginPath();
    data.forEach((v, i) => {
      const x = i * step;
      const y = h - ((v - min) / (max - min)) * (h - 4) - 2;
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.stroke();
    ctx.lineTo(w, h); ctx.lineTo(0, h); ctx.closePath();
    ctx.fillStyle = color + "22"; ctx.fill();
  }
  function pushHistory(key, val) {
    const arr = histories[key];
    arr.push(val);
    if (arr.length > 40) arr.shift();
  }

  function renderMetrics(m) {
    if (!m) return;
    lastMetrics = m;
    pushHistory("cpu", m.system.cpu_percent);
    pushHistory("ram", m.system.ram_percent);
    pushHistory("gpu", m.gpu.available ? m.gpu.utilization_percent : 0);
    pushHistory("pwr", m.gpu_watts || 0);
    $("#sc-cpu").textContent = m.system.cpu_percent.toFixed(0) + "%";
    $("#sc-ram").textContent = `${(m.system.ram_used_mb / 1024).toFixed(1)} / ${(m.system.ram_total_mb / 1024).toFixed(0)} GB`;
    $("#sc-gpu").textContent = m.gpu.available ? m.gpu.utilization_percent + "%" : "N/A";
    $("#sc-pwr").textContent = m.gpu_watts != null ? m.gpu_watts.toFixed(0) + " W" : "—";
    $("#t-gpu").textContent = m.gpu.available ? `${m.gpu.utilization_percent}%` : "N/A";
    drawSpark($("#spark-cpu"), histories.cpu, "#60a5fa");
    drawSpark($("#spark-ram"), histories.ram, "#c084fc");
    drawSpark($("#spark-gpu"), histories.gpu, "#34d399");
    drawSpark($("#spark-pwr"), histories.pwr, "#facc15");
    $("#bb-updated").textContent = nowTime();
    updateStats();
  }

  function renderStatus(s) {
    if (!s) return;
    lastStatus = s;
    $("#t-tasks").textContent = `${s.tasks_completed} / ${s.tasks_total}`;
    $("#t-uptime").textContent = fmtUptime(s.uptime_seconds);
    const running = s.status === "running";
    $("#status-dot").className = "status-dot" + (running ? " running" : "");
    $("#status-pill").textContent = t(running ? "running" : "idle");
  }

  function renderAll() {
    renderAgents();
    renderDetails();
    renderSettings();
    renderChatChrome();
  }

  // ─── data fetching ───────────────────────────────────────
  async function getJSON(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(url + " " + r.status);
    return r.json();
  }
  async function refreshSlow() {
    try {
      [agents, tasks] = await Promise.all([getJSON("/api/agents"), getJSON("/api/tasks")]);
      const status = await getJSON("/api/status");
      renderStatus(status);
      renderAgents();
      renderDetails();
    } catch (e) { /* backend not ready */ }
    try { renderUsage(await getJSON("/api/usage")); } catch (e) {}
    if (isVisible("audit-logs")) refreshAudit();
  }
  async function refreshMetrics() {
    try { renderMetrics(await getJSON("/api/metrics")); } catch (e) {}
  }

  // ─── websocket with reconnect ────────────────────────────
  let ws = null, wsBackoff = 1000;
  function connectWS() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    try { ws = new WebSocket(`${proto}://${location.host}/ws`); }
    catch (e) { scheduleReconnect(); return; }
    ws.onopen = () => { wsBackoff = 1000; };
    ws.onmessage = (ev) => {
      let msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
      pushLive(msg);
      if (msg.type === "Task" && msg.data) {
        const idx = tasks.findIndex((x) => x.id === msg.data.id);
        if (idx >= 0) tasks[idx] = msg.data; else tasks.push(msg.data);
        pushFeed(msg.data);
        renderAgents();
        renderDetails();
      }
    };
    ws.onclose = () => scheduleReconnect();
    ws.onerror = () => { try { ws.close(); } catch (e) {} };
  }
  function scheduleReconnect() {
    setTimeout(connectWS, wsBackoff);
    wsBackoff = Math.min(wsBackoff * 2, 15000);
  }

  // ─── theme + settings ────────────────────────────────────
  function applyTheme() {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("crew_theme", theme);
    const sel = document.getElementById("set-theme");
    if (sel) sel.value = theme;
  }
  function setTheme(name) {
    theme = name;
    if (!LIGHT_THEMES.has(name)) prevDarkTheme = name;
    applyTheme();
  }

  function renderSettings() {
    const box = $("#settings-body");
    if (!box) return;
    const themeOpts = THEMES.map(
      ([v, label]) => `<option value="${v}"${v === theme ? " selected" : ""}>${label}</option>`
    ).join("");
    const langOpts = [["en", t("lang_en")], ["ar", t("lang_ar")]].map(
      ([v, label]) => `<option value="${v}"${v === lang ? " selected" : ""}>${label}</option>`
    ).join("");
    const c = serverConfig;
    const server = c
      ? `<dt>${t("settings_ollama_endpoint")}</dt><dd>${esc(c.ollama_host)}</dd>
         <dt>${t("settings_timeout")}</dt><dd>${c.ollama_timeout} ${t("settings_seconds")}</dd>
         <dt>${t("settings_web_addr")}</dt><dd>${esc(c.web_host)}:${c.web_port}</dd>
         <dt>${t("settings_log_level")}</dt><dd>${esc(c.log_level)}</dd>
         <dt>${t("settings_lab_mode")}</dt><dd>${t(c.lab_mode ? "on" : "off")}</dd>
         <dt>${t("settings_anthropic")}</dt><dd>${t(c.anthropic_configured ? "settings_configured" : "settings_not_configured")}</dd>
         <dt>${t("settings_data_dir")}</dt><dd>${esc(c.data_dir)}</dd>`
      : `<dt>—</dt><dd>—</dd>`;
    box.innerHTML = `
      <div class="settings-form">
        <div class="settings-group">
          <label>${t("settings_theme")}</label>
          <select id="set-theme">${themeOpts}</select>
        </div>
        <div class="settings-group">
          <label>${t("settings_language")}</label>
          <select id="set-lang">${langOpts}</select>
        </div>
        <div class="settings-group">
          <label>${t("settings_server")}</label>
          <dl class="settings-server">${server}</dl>
          <div class="settings-note">${t("settings_server_note")}</div>
        </div>
        <div class="settings-actions">
          <button class="btn" id="set-reset">${t("settings_reset")}</button>
        </div>
      </div>`;
    $("#set-theme").addEventListener("change", (e) => setTheme(e.target.value));
    $("#set-lang").addEventListener("change", (e) => {
      lang = e.target.value;
      localStorage.setItem("crew_lang", lang);
      applyLang();
    });
    $("#set-reset").addEventListener("click", () => {
      setTheme("midnight");
      lang = "en";
      localStorage.setItem("crew_lang", lang);
      applyLang();
    });
  }

  // ─── team chat ───────────────────────────────────────────
  // Lightweight markdown: escape, then fenced code, inline code, bold,
  // and newlines -> <br> (only outside <pre> blocks).
  function mdLite(raw) {
    return esc(raw)
      .split(/(```[\s\S]*?```)/g)
      .map((seg) => {
        if (seg.startsWith("```")) {
          const code = seg.replace(/^```[a-zA-Z0-9_-]*\n?/, "").replace(/```$/, "").replace(/\n$/, "");
          return `<pre><code>${code}</code></pre>`;
        }
        return seg
          .replace(/`([^`\n]+)`/g, "<code>$1</code>")
          .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
          .replace(/\n/g, "<br>");
      })
      .join("");
  }
  const brEsc = (raw) => esc(raw).replace(/\n/g, "<br>");

  function chatLog() { return $("#chat-log"); }
  function chatScroll() {
    const log = chatLog();
    if (log) log.scrollTop = log.scrollHeight;
  }
  function chatShowEmptyIfNeeded() {
    const log = chatLog();
    if (log && log.children.length === 0) {
      log.appendChild(el("div", "chat-empty", t("chat_empty")));
    }
  }
  function chatClearEmpty() {
    const empty = chatLog() && chatLog().querySelector(".chat-empty");
    if (empty) empty.remove();
  }

  function addUserBubble(content) {
    chatClearEmpty();
    const msg = el("div", "chat-msg user");
    msg.innerHTML = `<div class="who">${t("chat_you")} 👤</div>
      <div class="chat-bubble">${brEsc(content)}</div>`;
    chatLog().appendChild(msg);
    chatScroll();
  }
  function startAgentBubble(role) {
    chatClearEmpty();
    const meta = META[role] || { accent: "blue" };
    const msg = el("div", "chat-msg agent");
    msg.dataset.accent = meta.accent === "cyan" ? "cyan" : meta.accent;
    msg.innerHTML = `
      <div class="who" style="color:var(--${meta.accent})">
        <img src="${avatarFor(role)}" alt="">${roleLabel(role)}</div>
      <div class="chat-bubble"><span class="typing"><i></i><i></i><i></i></span></div>`;
    chatLog().appendChild(msg);
    chatBubbles[role] = { el: msg.querySelector(".chat-bubble"), parts: [] };
    chatScroll();
  }
  function appendAgentToken(role, text) {
    const b = chatBubbles[role];
    if (!b) return;
    b.parts.push(text);
    b.el.innerHTML = brEsc(b.parts.join(""));
    chatScroll();
  }
  function finishAgentBubble(role, tokens) {
    const b = chatBubbles[role];
    if (!b) return;
    const full = b.parts.join("");
    b.el.innerHTML = mdLite(full);
    if (tokens != null) {
      const tok = el("div", "chat-tok", `${tokens} ${t("chat_tokens")}`);
      b.el.parentElement.appendChild(tok);
      chatTokens += tokens;
    }
    delete chatBubbles[role];
    renderChatStatus();
    chatScroll();
  }
  function agentErrorBubble(role, message) {
    const b = chatBubbles[role];
    if (b) {
      b.el.className = "chat-bubble err";
      b.el.textContent = message;
      delete chatBubbles[role];
    } else {
      chatClearEmpty();
      const msg = el("div", "chat-msg agent");
      msg.innerHTML = `<div class="who" style="color:var(--red)">${roleLabel(role)}</div>
        <div class="chat-bubble err"></div>`;
      msg.querySelector(".chat-bubble").textContent = message;
      chatLog().appendChild(msg);
    }
    chatScroll();
  }
  function systemErrorBubble(message) {
    chatClearEmpty();
    const msg = el("div", "chat-msg agent");
    msg.innerHTML = `<div class="who" style="color:var(--red)">system</div>
      <div class="chat-bubble err"></div>`;
    msg.querySelector(".chat-bubble").textContent = message;
    chatLog().appendChild(msg);
    chatScroll();
  }

  function renderChatStatus() {
    const s = $("#chat-status");
    if (!s) return;
    const state = chatBusy ? t("chat_busy") : "";
    s.textContent = `${t("chat_tokens")}: ${chatTokens}` + (state ? `  ·  ${state}` : "");
  }
  function chatConn(cls, key) {
    const c = $("#chat-conn");
    const label = $("#chat-conn-label");
    if (c) c.className = "chat-conn" + (cls ? " " + cls : "");
    if (label) label.textContent = t(key);
  }
  function setChatBusy(busy) {
    chatBusy = busy;
    const send = $("#chat-send");
    if (send) send.disabled = busy;
    renderChatStatus();
  }

  // Re-label chat chrome on language change (called from renderAll).
  function renderChatChrome() {
    const modeBtn = $("#chat-mode");
    if (modeBtn) {
      modeBtn.innerHTML = chatMode === "crew"
        ? `🤝 <span>${t("chat_mode_crew")}</span>`
        : `👤 <span>${t("chat_mode_single")}</span>`;
    }
    const sel = $("#chat-agent");
    if (sel) {
      const cur = sel.value;
      sel.innerHTML = chatAgents
        .map((r) => `<option value="${r}">${roleLabel(r)}</option>`)
        .join("");
      if (cur) sel.value = cur;
      sel.disabled = chatMode === "crew";
    }
    chatConn(
      chatWs && chatWs.readyState === 1 ? "ok" : "bad",
      chatWs && chatWs.readyState === 1 ? "chat_connected" : "chat_disconnected"
    );
    renderChatStatus();
  }

  async function populateChatAgents() {
    try {
      chatAgents = await getJSON("/api/chat/agents");
    } catch (e) { chatAgents = []; }
    renderChatChrome();
  }

  function connectChatWs() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    chatConn("", "chat_connecting");
    try { chatWs = new WebSocket(`${proto}://${location.host}/ws/chat`); }
    catch (e) { scheduleChatReconnect(); return; }
    chatWs.onopen = () => { chatWsBackoff = 1000; chatConn("ok", "chat_connected"); };
    chatWs.onmessage = (ev) => {
      let f; try { f = JSON.parse(ev.data); } catch (e) { return; }
      handleChatFrame(f);
    };
    chatWs.onclose = () => { chatConn("bad", "chat_disconnected"); scheduleChatReconnect(); };
    chatWs.onerror = () => { try { chatWs.close(); } catch (e) {} };
  }
  function scheduleChatReconnect() {
    setTimeout(connectChatWs, chatWsBackoff);
    chatWsBackoff = Math.min(chatWsBackoff * 2, 15000);
  }

  function endTurnIf(done) {
    if (done) chatPending -= 1;
    if (chatPending <= 0) { chatPending = 0; setChatBusy(false); }
  }
  function handleChatFrame(f) {
    switch (f.type) {
      case "user": addUserBubble(f.content); break;
      case "start": startAgentBubble(f.agent); break;
      case "token": appendAgentToken(f.agent, f.content); break;
      case "done": finishAgentBubble(f.agent, f.tokens); endTurnIf(true); break;
      case "error":
        if (f.agent === "system") { systemErrorBubble(f.content); chatPending = 0; setChatBusy(false); }
        else { agentErrorBubble(f.agent, f.content); endTurnIf(true); }
        break;
      default: break;
    }
  }

  function sendChat() {
    const ta = $("#chat-text");
    if (!ta) return;
    const content = ta.value.trim();
    if (!content || chatBusy) return;
    if (!chatWs || chatWs.readyState !== 1) { systemErrorBubble(t("chat_disconnected")); return; }
    chatPending = chatMode === "crew" ? Math.max(1, chatAgents.length) : 1;
    setChatBusy(true);
    chatWs.send(JSON.stringify({
      session_id: chatSession,
      mode: chatMode,
      agent: chatMode === "single" ? ($("#chat-agent").value || chatAgents[0]) : null,
      content,
    }));
    ta.value = "";
  }
  function cancelChat() {
    if (!chatBusy) return;
    // Closing the socket aborts the server-side stream; then reconnect.
    Object.keys(chatBubbles).forEach((role) => {
      const b = chatBubbles[role];
      b.el.innerHTML = (b.parts.length ? brEsc(b.parts.join("")) + " " : "") +
        `<span class="chat-tok">${t("chat_cancelled")}</span>`;
      delete chatBubbles[role];
    });
    chatPending = 0;
    setChatBusy(false);
    try { chatWs.close(); } catch (e) {}
  }
  function clearChat() {
    const log = chatLog();
    if (log) log.innerHTML = "";
    chatSession = newSessionId();
    chatTokens = 0;
    chatShowEmptyIfNeeded();
    renderChatStatus();
  }
  function toggleMode() {
    chatMode = chatMode === "single" ? "crew" : "single";
    renderChatChrome();
  }
  function exportChat() {
    const log = chatLog();
    if (!log) return;
    const lines = [];
    log.querySelectorAll(".chat-msg").forEach((m) => {
      const who = (m.querySelector(".who") || {}).textContent || "";
      const body = (m.querySelector(".chat-bubble") || {}).innerText || "";
      lines.push(`### ${who.trim()}\n\n${body.trim()}\n`);
    });
    const blob = new Blob([lines.join("\n")], { type: "text/markdown" });
    const a = el("a");
    a.href = URL.createObjectURL(blob);
    a.download = `crew-chat-${chatSession.slice(0, 8)}.md`;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  async function openSessionsModal() {
    let sessions = [];
    try { sessions = await getJSON("/api/chat/sessions"); } catch (e) {}
    const overlay = el("div", "modal-overlay");
    const items = sessions.length
      ? sessions.map((s) =>
          `<div class="modal-item" data-id="${esc(s.id)}">
            <span class="mi-title">${esc(s.title || s.id.slice(0, 8))}</span>
            <span class="mi-meta">${s.message_count} ${t("chat_msgs")} · ${esc((s.updated_at || "").slice(0, 16).replace("T", " "))}</span>
          </div>`).join("")
      : `<div class="chat-empty">${t("chat_no_sessions")}</div>`;
    overlay.innerHTML = `
      <div class="modal">
        <h3>${t("chat_load_title")}</h3>
        <div class="modal-list">${items}</div>
        <div class="modal-actions"><button class="btn" id="modal-close">${t("chat_close")}</button></div>
      </div>`;
    const close = () => overlay.remove();
    overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
    overlay.querySelector("#modal-close").addEventListener("click", close);
    overlay.querySelectorAll(".modal-item").forEach((it) => {
      it.addEventListener("click", () => { loadSession(it.dataset.id); close(); });
    });
    document.body.appendChild(overlay);
  }
  async function loadSession(sessionId) {
    let messages = [];
    try { messages = await getJSON(`/api/chat/sessions/${encodeURIComponent(sessionId)}`); }
    catch (e) { return; }
    chatSession = sessionId;
    chatTokens = 0;
    const log = chatLog();
    log.innerHTML = "";
    messages.forEach((m) => {
      if (m.role === "user") {
        addUserBubble(m.content);
      } else {
        const role = m.agent_id || "coder";
        const meta = META[role] || { accent: "blue" };
        const msg = el("div", "chat-msg agent");
        msg.dataset.accent = meta.accent;
        msg.innerHTML = `
          <div class="who" style="color:var(--${meta.accent})">
            <img src="${avatarFor(role)}" alt="">${roleLabel(role)}</div>
          <div class="chat-bubble">${mdLite(m.content)}</div>`;
        if (m.tokens_used) msg.appendChild(el("div", "chat-tok", `${m.tokens_used} ${t("chat_tokens")}`));
        log.appendChild(msg);
        chatTokens += m.tokens_used || 0;
      }
    });
    chatShowEmptyIfNeeded();
    renderChatStatus();
    chatScroll();
  }

  function wireChat() {
    const send = $("#chat-send");
    const ta = $("#chat-text");
    if (send) send.addEventListener("click", sendChat);
    if (ta) {
      ta.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); sendChat(); }
      });
    }
    const modeBtn = $("#chat-mode");
    if (modeBtn) modeBtn.addEventListener("click", toggleMode);
    const clearBtn = $("#chat-clear");
    if (clearBtn) clearBtn.addEventListener("click", clearChat);
    const exportBtn = $("#chat-export");
    if (exportBtn) exportBtn.addEventListener("click", exportChat);
    const loadBtn = $("#chat-load");
    if (loadBtn) loadBtn.addEventListener("click", openSessionsModal);

    // Global shortcuts, active only while the chat view is visible.
    document.addEventListener("keydown", (e) => {
      const chatVisible = !document.querySelector('.view[data-view="chat"]').classList.contains("hidden");
      if (!chatVisible || !(e.ctrlKey || e.metaKey)) {
        if (e.key === "Escape" && chatVisible) cancelChat();
        return;
      }
      const k = e.key.toLowerCase();
      if (k === "l") { e.preventDefault(); clearChat(); }
      else if (k === "s") { e.preventDefault(); exportChat(); }
      else if (k === "t") { e.preventDefault(); toggleMode(); }
    });
  }

  // ─── system views: live activity / audit / stats ────────
  function isVisible(view) {
    const v = document.querySelector(`.view[data-view="${view}"]`);
    return v && !v.classList.contains("hidden");
  }

  function pushLive(frame) {
    if (!frame || frame.type === "snapshot") return;
    liveEvents.push({ at: nowTime(), type: frame.type, data: frame.data });
    if (liveEvents.length > 200) liveEvents.shift();
    if (isVisible("live-activity")) renderLiveActivity();
  }
  function renderLiveActivity() {
    const box = $("#live-activity-body");
    if (!box) return;
    if (liveEvents.length === 0) {
      box.innerHTML = `<p style="color:var(--muted)">${t("live_empty")}</p>`;
      return;
    }
    box.innerHTML = liveEvents
      .map((e) => {
        let text = e.type;
        let role = "";
        if (e.type === "Task" && e.data) {
          role = e.data.assigned_to || "";
          text = `${e.data.status}: ${e.data.description}`;
        }
        const meta = META[role] || { accent: "blue" };
        const label = role ? roleLabel(role) : e.type;
        return `<div class="la-row"><span class="la-time">${e.at}</span>
          <span class="la-tag" style="color:var(--${meta.accent})">${esc(label)}</span>
          <span class="la-text">${esc(text)}</span></div>`;
      })
      .join("");
    box.scrollTop = box.scrollHeight;
  }

  async function refreshAudit() {
    const box = $("#audit-logs-body");
    if (!box) return;
    let rows = [];
    try { rows = await getJSON("/api/audit?limit=200"); } catch (e) { return; }
    if (rows.length === 0) {
      box.innerHTML = `<p style="color:var(--muted)">${t("audit_empty")}</p>`;
      return;
    }
    const head =
      `<tr><th>${t("audit_seq")}</th><th>${t("audit_time")}</th><th>${t("audit_type")}</th>` +
      `<th>${t("audit_severity")}</th><th>${t("audit_agent")}</th></tr>`;
    const body = rows
      .slice()
      .reverse()
      .map(
        (r) =>
          `<tr><td>${r.seq}</td><td>${esc((r.ts || "").slice(11, 19))}</td>` +
          `<td>${esc(r.type || "")}</td>` +
          `<td class="sev-${esc(r.severity || "")}">${esc(r.severity || "")}</td>` +
          `<td>${esc(r.agent || "—")}</td></tr>`
      )
      .join("");
    box.innerHTML = `<table class="audit-table"><thead>${head}</thead><tbody>${body}</tbody></table>`;
  }

  function ensureStatsSkeleton() {
    const box = $("#stats-body");
    if (!box || box.dataset.built) return;
    box.dataset.built = "1";
    box.innerHTML = `
      <div class="stats-detail">
        <div class="stat-big"><div class="sb-k">${t("cpu_usage")}</div><div class="sb-v" id="sb-cpu">—</div><canvas id="lg-cpu"></canvas></div>
        <div class="stat-big"><div class="sb-k">${t("ram_usage")}</div><div class="sb-v" id="sb-ram">—</div><canvas id="lg-ram"></canvas></div>
        <div class="stat-big"><div class="sb-k">${t("gpu_usage")}</div><div class="sb-v" id="sb-gpu">—</div><canvas id="lg-gpu"></canvas></div>
        <div class="stat-big"><div class="sb-k">${t("power")}</div><div class="sb-v" id="sb-pwr">—</div><canvas id="lg-pwr"></canvas></div>
        <div class="stat-big"><div class="sb-k">GPU</div><div class="sb-v" id="sb-gpuname" style="font-size:14px">—</div><div class="sb-sub" id="sb-gpudetail"></div></div>
        <div class="stat-big"><div class="sb-k">${t("system")}</div><div class="sb-v" id="sb-sys" style="font-size:16px">—</div><div class="sb-sub" id="sb-sysdetail"></div></div>
      </div>`;
  }
  function updateStats() {
    if (!isVisible("stats")) return;
    ensureStatsSkeleton();
    const m = lastMetrics;
    if (m) {
      $("#sb-cpu").textContent = m.system.cpu_percent.toFixed(0) + "%";
      $("#sb-ram").textContent =
        `${(m.system.ram_used_mb / 1024).toFixed(1)} / ${(m.system.ram_total_mb / 1024).toFixed(0)} GB`;
      $("#sb-gpu").textContent = m.gpu.available ? m.gpu.utilization_percent + "%" : "N/A";
      $("#sb-pwr").textContent = m.gpu_watts != null ? m.gpu_watts.toFixed(0) + " W" : "—";
      drawSpark($("#lg-cpu"), histories.cpu, "#60a5fa");
      drawSpark($("#lg-ram"), histories.ram, "#c084fc");
      drawSpark($("#lg-gpu"), histories.gpu, "#34d399");
      drawSpark($("#lg-pwr"), histories.pwr, "#facc15");
      if (m.gpu.available) {
        $("#sb-gpuname").textContent = m.gpu.name || "GPU";
        $("#sb-gpudetail").textContent =
          `${t("gpu_vram")}: ${(m.gpu.vram_used_mb / 1024).toFixed(1)} / ${(m.gpu.vram_total_mb / 1024).toFixed(1)} GB · ${t("gpu_temp")}: ${m.gpu.temperature_c}°C`;
      } else {
        $("#sb-gpuname").textContent = "N/A";
        $("#sb-gpudetail").textContent = "";
      }
    }
    if (lastStatus) {
      $("#sb-sys").textContent = fmtUptime(lastStatus.uptime_seconds);
      $("#sb-sysdetail").textContent =
        `${t("uptime")} · ${lastStatus.tasks_completed}/${lastStatus.tasks_total} ${t("tasks")} · ` +
        `${lastStatus.agents_online} ${t("agents")}`;
    }
  }

  // ─── view router ─────────────────────────────────────────
  // Sidebar items carry data-view; clicking one shows the matching
  // <div class="view" data-view="..."> in the center column and marks
  // the item active. Agent items/cards route to overview + select.
  function showView(name) {
    document.querySelectorAll(".center .view").forEach((v) => {
      v.classList.toggle("hidden", v.dataset.view !== name);
    });
    document.querySelectorAll(".sidebar .nav-item[data-view]").forEach((n) => {
      n.classList.toggle("active", n.dataset.view === name);
    });
    // populate-on-open for the data-backed views
    if (name === "live-activity") renderLiveActivity();
    else if (name === "audit-logs") refreshAudit();
    else if (name === "stats") updateStats();
  }

  function wireNav() {
    document.querySelectorAll(".sidebar .nav-item[data-view]").forEach((item) => {
      const go = () => showView(item.dataset.view);
      item.addEventListener("click", go);
      item.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); }
      });
    });
  }

  // ─── boot ────────────────────────────────────────────────
  $("#lang-toggle").addEventListener("click", () => {
    lang = lang === "en" ? "ar" : "en";
    localStorage.setItem("crew_lang", lang);
    applyLang();
  });
  // Quick dark/light flip; remembers the last dark theme.
  $("#theme-toggle").addEventListener("click", () => {
    setTheme(LIGHT_THEMES.has(theme) ? prevDarkTheme : "solarized-light");
  });
  document.querySelectorAll("#detail-tabs .tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll("#detail-tabs .tab").forEach((x) => x.classList.remove("active"));
      tab.classList.add("active");
    });
  });

  wireNav();
  wireChat();
  applyTheme();
  applyLang();
  getJSON("/api/config").then((c) => { serverConfig = c; renderSettings(); }).catch(() => {});
  populateChatAgents();
  chatShowEmptyIfNeeded();
  connectChatWs();
  refreshSlow();
  refreshMetrics();
  connectWS();
  setInterval(refreshMetrics, 500);
  setInterval(refreshSlow, 3000);
})();
