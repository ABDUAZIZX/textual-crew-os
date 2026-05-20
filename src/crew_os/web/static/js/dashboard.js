"use strict";
(function () {
  const $ = (sel) => document.querySelector(sel);
  const el = (tag, cls, html) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html != null) e.innerHTML = html;
    return e;
  };

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
  }

  function renderStatus(s) {
    if (!s) return;
    $("#t-tasks").textContent = `${s.tasks_completed} / ${s.tasks_total}`;
    $("#t-uptime").textContent = fmtUptime(s.uptime_seconds);
    const running = s.status === "running";
    $("#status-dot").className = "status-dot" + (running ? " running" : "");
    $("#status-pill").textContent = t(running ? "running" : "idle");
  }

  function renderAll() {
    renderAgents();
    renderDetails();
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
  document.querySelectorAll("#detail-tabs .tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll("#detail-tabs .tab").forEach((x) => x.classList.remove("active"));
      tab.classList.add("active");
    });
  });

  wireNav();
  applyLang();
  refreshSlow();
  refreshMetrics();
  connectWS();
  setInterval(refreshMetrics, 500);
  setInterval(refreshSlow, 3000);
})();
