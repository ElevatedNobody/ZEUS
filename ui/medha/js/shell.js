/* ZEUS workspace frame in the Voice Core design: the wave behind, a top bar
   (brand, live status, MIC / AUDIO / CINEMATIC VIEW, user), a glass rail, the
   command banner, the evidence sheet, approvals and the user menu. Every
   indicator shows a value the backend sent - nothing is decorative data. */
(function () {
  "use strict";
  const M = window.ZEUS;
  const { h, esc } = M;

  const ICONS = {
    command: "M4 6h16M4 12h10M4 18h7",
    company: "M3 20V9l6-4 6 4v11M15 20V12h6v8M7 20v-4h4v4",
    knowledge: "M5 4h10a4 4 0 0 1 4 4v12H9a4 4 0 0 1-4-4zM9 8h6M9 12h6",
    documents: "M7 3h7l5 5v13H7zM14 3v5h5",
    evidence: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM20 20l-4-4",
    reasoning: "M12 3a6 6 0 0 0-4 10.5V17h8v-3.5A6 6 0 0 0 12 3zM9 21h6",
    artifacts: "M4 7l8-4 8 4-8 4zM4 12l8 4 8-4M4 17l8 4 8-4",
    excel: "M4 4h16v16H4zM4 10h16M4 15h16M10 4v16",
    deck: "M3 5h18v11H3zM12 16v4M8 20h8",
    report: "M6 3h12v18H6zM9 8h6M9 12h6M9 16h4",
    productivity: "M9 11l2 2 4-4M5 4h14v16H5z",
    industrial: "M3 21V11l5 3V11l5 3V7l8 4v10z",
    automation: "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M5 19l2-2M17 7l2-2",
    memory: "M6 4h12v16H6zM9 8h6M9 12h6M9 16h3",
    models: "M4 7h16v10H4zM8 7V4M16 7V4M8 20v-3M16 20v-3",
    agents: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 21a8 8 0 0 1 16 0",
    security: "M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z",
    firewall: "M3 5h18v14H3zM3 10h18M3 15h18M9 5v5M15 10v5M9 15v4",
    audit: "M9 5h10v14H9zM5 9h4M5 13h4M5 17h4M12 9h4M12 13h4",
    vault: "M4 5h16v14H4zM12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM12 12h.01M4 9H2M4 15H2",
    projects: "M3 20h18M5 20V9l4-3v14M9 20V4l6 4v12M15 20v-8l4-2v10",
    progress: "M4 19h16M4 15l4-4 4 3 7-8M15 6h4v4",
    schedule: "M4 5h7v4H4zM9 11h8v4H9zM14 17h6v3h-6zM11 7h6M17 13v4",
    capture: "M4 8h3l2-3h6l2 3h3v11H4zM12 11a3 3 0 1 0 0 6 3 3 0 0 0 0-6z",
    trail: "M6 3h9l4 4v14H6zM9 12h7M9 16h5M9 8h3",
    chronology: "M12 3v18M12 6h6M12 11H6M12 16h7M6 11a1 1 0 1 1 0 .01M18 6a1 1 0 1 1 0 .01M19 16a1 1 0 1 1 0 .01",
    why: "M4 6h5v4H4zM15 14h5v4h-5zM9 8h3v8h3M12 3v2M12 19v2",
    sync: "M4 12a8 8 0 0 1 14-5.3M20 12a8 8 0 0 1-14 5.3M18 3v4h-4M6 21v-4h4",
    learning: "M12 5l9 4-9 4-9-4zM7 11v5c3 2 7 2 10 0v-5",
    psearch: "M10 4a6 6 0 1 0 0 12 6 6 0 0 0 0-12zM20 20l-5.5-5.5M8 10h4",
    devices: "M3 5h12v9H3zM6 18h6M9 14v4M17 8h4v11h-4zM18.5 16.5h1",
    messages: "M4 5h16v11H9l-5 4zM8 9h8M8 12h5",
    transfers: "M7 4v13M7 17l-3-3M7 17l3-3M17 20V7M17 7l-3 3M17 7l3 3",
    rtasks: "M4 6h7v5H4zM13 13h7v5h-7zM11 8.5h4v4.5M13 15.5H9V11",
    sharing: "M18 8a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM6 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM18 22a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM8.6 13.5l6.8 4M15.4 6.5l-6.8 4",
    settings: "M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM19 12a7 7 0 0 0-.1-1.2l2-1.6-2-3.4-2.4 1a7 7 0 0 0-2-1.2L14 3h-4l-.5 2.6a7 7 0 0 0-2 1.2l-2.4-1-2 3.4 2 1.6a7 7 0 0 0 0 2.4l-2 1.6 2 3.4 2.4-1a7 7 0 0 0 2 1.2L10 21h4l.5-2.6a7 7 0 0 0 2-1.2l2.4 1 2-3.4-2-1.6c.1-.4.1-.8.1-1.2z",
  };
  const RAIL_LABEL = { COMMAND: "CMD", PROJECTS: "PROJ", NETWORK: "NET", INTELLIGENCE: "INTEL", KNOWLEDGE: "KNOW", WORKBENCH: "WORK", INDUSTRIAL: "PLANT",
    AUTOMATION: "AUTO", SYSTEM: "SYSTEM", SECURITY: "SECURE", VAULT: "VAULT" };
  const GROUPS = ["COMMAND", "PROJECTS", "NETWORK", "INTELLIGENCE", "KNOWLEDGE", "WORKBENCH", "INDUSTRIAL", "AUTOMATION", "SYSTEM", "SECURITY", "VAULT"];
  M.screens = {};
  M.screen = function (id, def) { M.screens[id] = Object.assign({ id }, def); };

  const STATE_TEXT = { IDLE: "READY", LISTENING: "LISTENING", UNDERSTANDING: "UNDERSTANDING", THINKING: "THINKING",
    RETRIEVING: "RETRIEVING EVIDENCE", EXECUTING: "WORKING", WAITING_FOR_APPROVAL: "WAITING FOR YOUR APPROVAL",
    SPEAKING: "SPEAKING", ERROR: "ERROR", LOCKED: "LOCKED" };

  /* The line over the wave says what ZEUS is doing - from the backend's state. */
  const VOICE_LINE = { IDLE: "Ready when you are", LISTENING: "I'm listening", UNDERSTANDING: "Understanding…",
    THINKING: "Thinking…", RETRIEVING: "Searching the records…", EXECUTING: "Working on it…",
    WAITING_FOR_APPROVAL: "Waiting for your approval", SPEAKING: "Speaking", ERROR: "That did not complete", LOCKED: "Locked" };

  let current = null, hello = null, speak = true, lastStatus = null, statusBusy = false, lastState = { state: "IDLE" };
  const approvals = [];

  function icon(name) {
    return M.html(`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="${ICONS[name] || ICONS.command}"/></svg>`);
  }

  /* --------------------------------------------------------------- frame ---- */
  const MIC_SVG = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M19 11a7 7 0 0 1-7 7m0 0a7 7 0 0 1-7-7m7 7v4m0 0H8m4 0h4m-4-8a3 3 0 0 0 3-3V5a3 3 0 1 0-6 0v6a3 3 0 0 0 3 3z"/></svg>`;
  const AUDIO_SVG = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M11 5L6 9H2v6h4l5 4V5zM19.07 4.93a10 10 0 0 1 0 14.14M15.54 8.46a5 5 0 0 1 0 7.07"/></svg>`;

  function build() {
    M.wave.attach();
    const strip = h("header.strip.hud",
      h("div.brand-block", M.badge(), h("div",
        h("div.row", { style: "gap:10px;flex-wrap:nowrap" }, h("span.brand-name", "ZEUS"), h("span.pill#s-version", "VOICE CORE")),
        h("div.brand-sub", "Autonomous AI Command & Intelligence System"))),
      h("div.spacer"),
      h("div.status-cluster", h("div.item#s-lockdown"), h("div.item#s-approvals"), h("div.item#s-crudex"), h("div.item#s-network"), h("div.item#s-runtime"), h("div.item#s-vault")),
      h("div.clock-block", h("div.clock#s-clock", "—"), h("div.state-line#s-state", "—")),
      h("button.btn.ctrl.mic#btn-mic", { onclick: listen }, M.html(MIC_SVG), h("span#mic-status", "MIC")),
      h("button.btn.ctrl.audio#btn-speak", { onclick: toggleSpeak }, M.html(AUDIO_SVG), h("span#audio-status", "AUDIO")),
      h("button.btn.ctrl.cinema#btn-cinema", { onclick: toggleCinema, title: "Hide the panels - Esc brings them back" }, "CINEMATIC VIEW"),
      h("div.user-pill#s-user", { onclick: toggleMenu }, h("span.avatar#s-initials", "?"), h("span#s-username", "—"), M.chip("—")));
    const rail = h("nav.rail.glass.hud");
    GROUPS.forEach((group) => {
      const items = Object.values(M.screens).filter((s) => s.group === group);
      if (!items.length) return;
      rail.appendChild(h("div.group", RAIL_LABEL[group] || group));
      items.forEach((s) => {
        const tip = h("span.tip", s.label);
        const b = h("button.rail-btn", { "data-screen": s.id, onclick: () => M.go(s.id), "aria-label": s.label }, icon(s.icon || s.id), tip);
        b.addEventListener("mouseenter", () => { const r = b.getBoundingClientRect(); tip.style.top = `${r.top + r.height / 2}px`; });
        rail.appendChild(b);
      });
    });
    const canvas = h("main.canvas#canvas");
    const command = h("footer.command.hud", h("div.command-inner.glass",
      h("div.led#cmd-led"), h("span.who", "ZEUS:"),
      h("input#cmd", { placeholder: "Ask about the project, say 'field update: …', or 'go to schedule'   ·   Ctrl K", autocomplete: "off", spellcheck: "false" }),
      h("div.level#level", Array.from({ length: 5 }, () => h("i"))), h("span.state#cmd-state", "READY"),
      h("button.btn.primary.small#btn-send", { onclick: () => submit(M.$("#cmd").value) }, "Send")));
    const app = h("div.app", strip, rail, canvas, command);
    const sheet = h("aside.sheet#sheet", h("div.sheet-head", h("h3#sheet-title", "Evidence"), h("button.btn.ghost.small", { onclick: closeSheet }, "Close")), h("div.sheet-body#sheet-body"));
    document.body.append(app, sheet, h("div.cinema-hint", "CINEMATIC VIEW · ESC TO RETURN"));

    M.$("#cmd").addEventListener("keydown", (e) => { if (e.key === "Enter") submit(e.target.value); });
    window.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        if (document.body.classList.contains("cinema")) toggleCinema();
        M.$("#cmd").focus();
      }
      if (e.key !== "Escape" || M.$(".scrim")) return;                 // a dialog handles its own Escape
      if (M.$(".menu")) { M.$(".menu").remove(); return; }
      if (M.$("#sheet").classList.contains("open")) { closeSheet(); return; }
      if (document.activeElement && document.activeElement.id === "cmd") { document.activeElement.blur(); return; }
      toggleCinema();
    });
    const tick = () => { const c = M.$("#s-clock"); if (c) c.textContent = new Date().toLocaleTimeString([], { hour12: false }); };
    tick();
    setInterval(tick, 1000);
  }

  /* Cinematic view: only the wave and the voice line stay; Esc brings the panels back. */
  function toggleCinema() {
    const on = document.body.classList.toggle("cinema");
    M.$("#btn-cinema").classList.toggle("on", on);
    if (on) closeSheet();
  }
  M.toggleCinema = toggleCinema;

  /* -------------------------------------------------------------- router ---- */
  M.go = function (id, params) {
    const def = M.screens[id];
    if (!def) return;
    current = { id, params: params || {} };
    document.querySelectorAll(".rail-btn").forEach((b) => b.classList.toggle("active", b.dataset.screen === id));
    const canvas = M.$("#canvas");
    if (def.home) {                                   // the voice home: the wave is the screen
      M.wave.mode("home");
      const home = h("section.screen.home");
      canvas.replaceChildren(home);
      Promise.resolve().then(() => def.render(home, current.params)).catch((e) => home.appendChild(M.callout(e.message || String(e), "fail")));
      return;
    }
    M.wave.mode("screen");
    M.wave.sleep();
    const host = h("section.screen.panel");
    const head = h("div.screen-head", h("div", h("div.eyebrow", def.group), h("h1", def.title || def.label), def.sub ? h("p", def.sub) : null), h("div.row#screen-actions"));
    host.appendChild(head);
    const body = h("div.stack");
    host.appendChild(body);
    canvas.replaceChildren(host);
    Promise.resolve().then(() => def.render(body, current.params, M.$("#screen-actions"))).catch((e) => body.appendChild(M.callout(e.message || String(e), "fail")));
  };
  M.refresh = () => { if (current) M.go(current.id, current.params); };
  M.current = () => current;

  /* ----------------------------------------------------------- the sheet ---- */
  M.openSheet = function (title, content) {
    M.$("#sheet-title").textContent = title;
    M.$("#sheet-body").replaceChildren(content);
    M.$("#sheet").classList.add("open");
  };
  function closeSheet() { M.$("#sheet").classList.remove("open"); }
  M.closeSheet = closeSheet;

  /* Open one evidence passage (a node id) with its document, chain and table. */
  M.showEvidence = async function (nodeId, label) {
    const body = h("div.stack", M.loading("reading the evidence"));
    M.openSheet(label || "Evidence", body);
    try {
      const d = await M.tool("show_evidence", { node_id: nodeId });
      body.replaceChildren();
      if (d.error) { body.appendChild(M.callout(d.error, "warn")); return; }
      const n = d.node;
      body.appendChild(h("div.row", M.chip(n.authority_label || "source", "info"), M.chip(n.origin, n.origin === "company" ? "ok" : n.origin === "general_knowledge" ? "info" : "sample"),
        M.chip(n.confidentiality, "plain"), n.year ? M.chip(String(n.year), "plain") : null));
      body.appendChild(h("h3", n.title || n.filename));
      if (d.chain && d.chain.length) body.appendChild(h("div.chain", d.chain.map((c) => h("span", c.heading || c.kind))));
      body.appendChild(h("div.card.raised", h("div.evidence-text.selectable", { style: "max-height:none" }, n.text || "")));
      if (d.table && d.cells && d.cells.length) {
        const rows = {};
        d.cells.forEach((c) => { (rows[c.row_index] = rows[c.row_index] || {})[c.header || "col " + c.col_index] = c.value_text; });
        body.appendChild(h("div.eyebrow", "table cells"));
        body.appendChild(M.table(d.table.headers.length ? d.table.headers : Object.keys(rows[0] || {}), Object.values(rows)));
      }
      if (d.entities && d.entities.length) body.appendChild(h("div.row", d.entities.map((e) => M.chip(`${e.type}: ${e.canonical_name}`, "plain"))));
      body.appendChild(M.kv(n, [["filename", "File"], ["page", "Page"], ["section_path", "Section"], ["doc_date", "Dated"], ["document_type", "Type"], ["source_hash", "Content hash"], ["node_id", "Node"]]));
      body.appendChild(M.button("Document provenance", "small", async () => {
        const p = await M.tool("document_provenance", { document_id: n.document_id });
        body.appendChild(M.pre(p.document ? { ...p.document, counts: p.counts, ingestion: p.ingestion_log } : p));
      }));
    } catch (e) { body.replaceChildren(M.callout(e.message, "fail")); }
  };

  /* ------------------------------------------------------ command & voice ---- */
  const conversation = [];
  M.conversation = conversation;
  async function submit(text) {
    text = String(text || "").trim();
    if (!text) return;
    const nav = text.match(/^(?:go to|open|show)\s+(?:the\s+)?(.+?)(?:\s+screen)?$/i);
    if (nav) {
      const target = Object.values(M.screens).find((s) => s.label.toLowerCase() === nav[1].toLowerCase() || s.id === nav[1].toLowerCase());
      if (target) { M.$("#cmd").value = ""; M.go(target.id); return; }
    }
    /* One request at a time: a second one is refused out loud and its text is
       left in the box - never dropped silently, never run alongside the first. */
    if (busy()) { M.toast("Still working on the previous request - this one was not sent.", "warn"); return; }
    M.$("#cmd").value = "";
    M.wave.surge();
    const turn = { request: text, pending: true, ts: Date.now() / 1000 };
    conversation.push(turn);
    if (!current || current.id !== "command") M.go("command"); else M.refresh();
    try { turn.result = await M.call("submit", { text }); }
    catch (e) { turn.result = { response: "Not done: " + e.message, error: true }; }
    turn.pending = false;
    if (current && current.id === "command") M.refresh();
    else M.toast("Answer ready in the Command Center", "ok");
  }
  M.submit = submit;
  const busy = () => conversation.some((turn) => turn.pending);

  /* The recogniser's real stage, pushed by the backend as it happens:
     MIC READY, PREPARING RECOGNISER, LISTENING, SPEECH DETECTED, TRANSCRIBING,
     TRANSCRIBED, NO SPEECH, MIC ERROR, STT ERROR. Nothing here is guessed. */
  const STT_TONE = { "MIC ERROR": "fail", "STT ERROR": "fail", "NO SPEECH": "warn", TRANSCRIBED: "ok" };
  let sttStage = null, sttTimer = null;
  function onStt(p) {
    sttStage = p && p.stage ? p : null;
    clearTimeout(sttTimer);
    if (sttStage && ["TRANSCRIBED", "NO SPEECH", "MIC ERROR", "STT ERROR"].includes(sttStage.stage)) {
      sttTimer = setTimeout(() => { sttStage = null; paintVoice(); }, sttStage.stage === "TRANSCRIBED" ? 2500 : 6000);
    }
    paintVoice();
  }
  M.onStt = onStt;
  async function listen() {
    const mic = M.$("#btn-mic");
    if (mic.classList.contains("on")) return;
    if (busy()) { M.toast("Still working on the previous request - listen again when it answers.", "warn"); return; }
    mic.classList.add("on");
    sttStage = { stage: "STARTING", detail: "asking the recogniser" };
    paintVoice();
    const turn = { request: "(listening…)", pending: true, spoken: true, ts: Date.now() / 1000 };
    conversation.push(turn);
    if (!current || current.id !== "command") M.go("command"); else M.refresh();
    try {
      turn.result = await M.call("listen");
      turn.request = turn.result && turn.result.transcript ? turn.result.transcript : "(nothing heard)";
      if (turn.result && turn.result.stt) onStt(turn.result.stt);
    } catch (e) { turn.result = { response: e.message, error: true }; turn.request = "(voice)"; }
    turn.pending = false;
    mic.classList.remove("on");
    paintVoice();
    if (current && current.id === "command") M.refresh();
  }
  async function toggleSpeak() {
    speak = !speak;
    try { await M.call("set_speak", { enabled: speak }); if (!speak) await M.call("stop_speaking"); } catch (e) { M.toast(e.message, "fail"); }
    paintVoice();
  }
  /* MIC and AUDIO say what the local voice stack can really do right now. */
  function paintVoice() {
    const v = (lastStatus && lastStatus.voice) || (hello && hello.voice) || {};
    const mic = M.$("#btn-mic"), sp = M.$("#btn-speak");
    const listening = mic.classList.contains("on");
    mic.disabled = !v.can_listen;
    const stage = sttStage && (listening || STT_TONE[sttStage.stage]) ? sttStage.stage : null;
    M.$("#mic-status").textContent = !v.can_listen ? "MIC: UNAVAILABLE" : stage ? `MIC: ${stage}` : "MIC: READY";
    mic.dataset.stage = stage ? (STT_TONE[stage] || "live") : "";
    mic.title = !v.can_listen ? `Voice input unavailable: ${v.detail || "no local recogniser"}`
      : stage && sttStage.detail ? `${stage} - ${sttStage.detail}` : `Speak once - recognised locally (${v.input})`;
    sp.disabled = !v.can_speak;
    sp.classList.toggle("on", !!(v.can_speak && speak));
    const mode = v.device ? ` · ${v.device}` : "";
    M.$("#audio-status").textContent = !v.can_speak ? "AUDIO: UNAVAILABLE" : speak ? `AUDIO: ON${mode}` : "AUDIO: MUTED";
    sp.title = v.can_speak ? (speak ? `Spoken replies on (${v.output}${v.device ? ` · ${v.device} mode` : ""}) - click to mute` : "Spoken replies muted") : `Speech output unavailable: ${v.detail || ""}`;
    voiceLine(lastState);
  }

  /* The words over the wave (home screen only). */
  function voiceLine(snap) {
    const line = M.$("#voice-prompt"), sub = M.$("#voice-sub");
    if (!line || !sub) return;
    const s = (snap && snap.state) || "IDLE";
    line.textContent = VOICE_LINE[s] || s;
    line.classList.toggle("live", s !== "IDLE" && s !== "ERROR" && s !== "LOCKED");
    const v = (lastStatus && lastStatus.voice) || (hello && hello.voice) || {};
    const detail = snap && snap.detail && snap.detail !== "null" ? snap.detail : "";
    sub.textContent = s !== "IDLE" && detail ? detail
      : v.can_listen ? `Voice ready · ${v.input || "local recogniser"}${v.can_speak ? ` → ${v.output}${v.device ? " " + v.device : ""}` : ""}`
      : `Type below · voice input unavailable${v.detail ? ` - ${v.detail}` : ""}`;
  }
  M.voiceLine = () => voiceLine(lastState);

  function applyState(snap) {
    lastState = snap || { state: "IDLE" };
    const s = lastState.state || "IDLE";
    const detail = lastState.detail && lastState.detail !== "null" ? lastState.detail : "";
    const node = M.$("#cmd-state");
    if (node) { node.textContent = STATE_TEXT[s] || s; node.classList.toggle("live", s !== "IDLE"); }
    const led = M.$("#cmd-led");
    if (led) { led.classList.toggle("live", s !== "IDLE" && s !== "ERROR"); led.classList.toggle("fail", s === "ERROR"); }
    const line = M.$("#s-state");
    if (line) {
      line.textContent = (STATE_TEXT[s] || s) + (detail && s !== "IDLE" ? ` · ${detail}` : "");
      line.className = "state-line" + (s === "IDLE" ? " idle" : s === "ERROR" ? " fail" : "");
    }
    M.wave.state(s);
    voiceLine(lastState);
  }
  function applyLevel(level) {
    const bars = document.querySelectorAll("#level i");
    bars.forEach((b, i) => { b.style.height = Math.max(15, Math.min(100, (level * 100) * (1 - Math.abs(i - 2) * 0.22))) + "%"; });
    M.wave.level(level);
  }

  /* -------------------------------------------------------------- status ---- */
  async function refreshStatus() {
    if (statusBusy || document.hidden) return;
    statusBusy = true;
    try {
      const s = await M.call("status");
      lastStatus = s;
      const rt = s.runtime || {};
      M.$("#s-runtime").replaceChildren(rt.up ? M.chip(`${rt.models} local models`, "ok") : M.chip("model runtime offline", "warn"));
      const net = s.network || {};
      M.$("#s-network").replaceChildren(M.chip(`net ${String(net.mode || "?").toUpperCase()}`, net.mode === "monitor" ? "warn" : "ok"),
        h("span.muted", `${M.num(net.external_blocked, 0)} blocked`));
      M.$("#s-vault").replaceChildren(M.chip(s.vault.unlocked ? "vault UNLOCKED" : s.vault.configured ? "vault LOCKED" : "vault NOT SET UP", s.vault.unlocked ? "warn" : "plain"));
      M.$("#s-lockdown").replaceChildren(s.lockdown ? M.chip("LOCKDOWN ENGAGED", "fail") : "");
      M.$("#s-approvals").replaceChildren(s.approvals_pending ? M.chip(`${s.approvals_pending} approval(s) waiting`, "live") : "");
      paintVoice();
      refreshNet();
    } catch (e) {
      M.$("#s-runtime").replaceChildren(M.chip("backend not answering", "fail"));
    } finally { statusBusy = false; }
  }
  M.status = () => lastStatus;

  /* Security events are coalesced: one toast per kind every 20 s, with the
     count of the others - a burst (every model failing its integrity check)
     must not bury the screen. The Security Center lists every event. */
  const seen = {};
  function securityToast(ev) {
    if (ev.severity !== "warning" && ev.severity !== "critical") return;
    const now = Date.now(), entry = seen[ev.kind] || (seen[ev.kind] = { last: 0, held: 0, timer: null });
    if (now - entry.last > 20000) {
      entry.last = now;
      M.toast(`${ev.kind}: ${ev.detail}`, ev.severity === "critical" ? "fail" : "warn");
      return;
    }
    entry.held += 1;
    if (!entry.timer) entry.timer = setTimeout(() => {
      if (entry.held) M.toast(`${entry.held} more ${ev.kind} event(s) - see Security Center → Events`, "warn");
      entry.held = 0; entry.timer = null; entry.last = Date.now();
    }, 20000 - (now - entry.last));
  }

  /* ----------------------------------------------------------- approvals ---- */
  function showApproval() {
    const existing = M.$("#approval-scrim");
    if (existing) existing.remove();
    const req = approvals[0];
    if (!req) return;
    const decide = async (approved) => {
      approvals.shift();
      try { await M.call("resolve_approval", { id: req.id, approved }); } catch (e) { M.toast(e.message, "fail"); }
      showApproval();
    };
    const scrim = h("div.scrim#approval-scrim", h("div.modal",
      h("div.row", M.chip("APPROVAL REQUIRED", "live"), M.chip(`risk ${req.risk}`, String(req.risk).includes("HIGH") || String(req.risk).includes("CRIT") ? "fail" : "warn")),
      h("h2", { style: "margin:14px 0 6px" }, req.tool),
      h("p.dim", req.description),
      h("p.note", `Requested by ${req.user || "—"} · origin ${req.origin}${req.origin === "planner" ? " (proposed by a model)" : ""} · expires in ${Math.round(req.timeout || 0)} s`),
      h("div.actions", h("button.btn.danger", { onclick: () => decide(false) }, "Deny"), h("button.btn.primary", { onclick: () => decide(true) }, "Allow once"))));
    document.body.appendChild(scrim);
  }

  /* ------------------------------------------------------------ user menu ---- */
  function toggleMenu() {
    const open = M.$(".menu");
    if (open) { open.remove(); return; }
    const conn = h("div.crx-conn#crx-conn", M.loading("reading the connection"));
    const menu = h("div.menu.crx-menu",
      h("div.crx-who", h("span.avatar", M.$("#s-initials").textContent), h("div", h("b", hello && hello.user || "—"),
        h("div.note", `${String(hello && hello.role || "").toUpperCase()} · ${M.$("#s-version").textContent}`))),
      h("div.crx-sec", "ACCOUNT"),
      h("button", { onclick: () => { menu.remove(); M.call("lock"); } }, "Lock"),
      h("button", { onclick: () => { menu.remove(); M.call("logout"); } }, "Sign out"),
      h("button", { onclick: () => { menu.remove(); changePassword(); } }, "Change password"),
      h("div.crx-sec", "DEVICES"),
      h("button#crx-devices", { onclick: () => { menu.remove(); M.go("devices"); } }, "My devices"),
      h("div.crx-sec", "SETTINGS"),
      h("button", { onclick: () => { menu.remove(); M.go("settings"); } }, "Settings"),
      h("div.crx-sec", "CONNECTION MODE"),
      conn,
      h("hr"),
      h("button.danger", { onclick: async () => {
        menu.remove();
        if (await M.confirm("Engage the emergency lockdown?", "Network is cut to loopback, the vault locks, pending approvals are refused, models are released and the session locks. An administrator releases it.", true))
          M.tool("engage_lockdown", { reason: "operator from the user menu" }).catch((e) => M.toast(e.message, "fail"));
      } }, "Emergency lockdown"));
    document.body.appendChild(menu);
    paintConnection();
    setTimeout(() => document.addEventListener("click", function away(e) { if (!e.target.closest(".menu") && !e.target.closest("#s-user") && !e.target.closest(".scrim")) { menu.remove(); document.removeEventListener("click", away); } }), 0);
  }

  /* ---------------------------------------------------- ZEUS NETWORK ---- */
  /* Every value here comes from crudex_state (the real client, the real
     Credential Manager entry, the real server connection). Nothing is assumed. */
  const NET_GLYPH = { ONLINE: ["●", "ok"], OFFLINE: ["○", "plain"], CONNECTING: ["◐", "warn"], SYNCING: ["↻", "live"], ERROR: ["⚠", "fail"] };
  let net = null;
  M.net = () => net;
  const ago = (ts) => {
    if (!ts) return "never";
    const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
    return s < 60 ? `${s} second${s === 1 ? "" : "s"} ago` : s < 3600 ? `${Math.round(s / 60)} min ago` : M.time(ts);
  };
  M.ago = ago;
  /* A chip whose text starts with its own glyph: no extra dot in front of it. */
  M.gchip = (text, tone) => { const c = M.chip(text, tone); c.classList.add("glyph"); return c; };
  function netChip(state) {
    const [glyph, tone] = NET_GLYPH[state] || ["?", "plain"];
    return M.gchip(`${glyph} ${state}`, tone);
  }
  M.netChip = netChip;

  async function refreshNet() {
    try {
      const r = await M.call("crudex_state");
      if (!r.ok) throw new Error(r.error);
      net = r;
    } catch (e) { net = { state: "ERROR", detail: e.message, mode: "?", ok: false }; }
    paintNetChip();
    if (M.$("#crx-conn")) paintConnection();
    return net;
  }
  M.refreshNet = refreshNet;

  function paintNetChip() {
    const host = M.$("#s-crudex");
    if (!host || !net) return;
    const lead = net.mode === "offline" ? M.gchip("○ OFFLINE", "plain") : netChip(net.state);
    host.replaceChildren(h("span.crx-chip", { title: `ZEUS NETWORK - ${net.detail || ""}`, onclick: toggleMenu }, lead),
      net.mode === "online" && net.last_sync_at ? h("span.muted", `sync ${ago(net.last_sync_at)}`) : "");
  }

  function paintConnection() {
    const host = M.$("#crx-conn");
    if (!host) return;
    if (!net) { refreshNet(); return; }
    const online = net.mode === "online";
    const setMode = async (mode) => {
      const r = await M.call("crudex_set_mode", { mode });
      if (!r.ok) { M.toast(r.error, "fail"); }
      refreshNet();
    };
    const radio = (mode, label) => h("label.crx-radio" + (net.mode === mode ? ".on" : ""),
      h("input", { type: "radio", name: "crx-mode", value: mode, checked: net.mode === mode, onchange: () => setMode(mode) }),
      h("span.dot"), label);
    const modes = h("div.crx-modes", { role: "radiogroup", "aria-label": "Connection mode" }, radio("online", "Online"), radio("offline", "Offline"));
    const local = net.local || {};
    if (!online) {
      host.replaceChildren(modes, h("div.crx-panel",
        h("div.crx-line", M.gchip("● Local processing enabled", "ok")),
        h("div.crx-kv", h("span", "LOCAL MODEL"), M.chip(local.local_model === "READY" ? `Ready · ${local.local_models} model(s)` : "Unavailable", local.local_model === "READY" ? "ok" : "warn")),
        local.local_model !== "READY" ? h("p.note", local.local_model_detail || "") : null,
        h("div.crx-kv", h("span", "ZEUS NETWORK"), M.chip("Unavailable in Offline Mode", "plain")),
        h("p.note", "Devices, messages, file sharing and remote tasks need Online mode. Everything on this machine keeps working without the internet.")));
      return;
    }
    const keyInput = h("input.field.crx-key", { type: "password", placeholder: net.has_key ? "enter a new key to replace it" : "crx_…", autocomplete: "off", spellcheck: "false", "aria-label": "API key" });
    const save = h("button.btn.primary.small", "Save");
    save.addEventListener("click", M.act(save, async () => {
      const value = keyInput.value;
      keyInput.value = "";                                   // the key never stays in the page
      if (!value.trim()) { M.toast("Enter the API key first", "warn"); return; }
      const r = await M.call("crudex_save_key", { api_key: value });
      M.toast(r.ok ? "API key saved in Windows Credential Manager" : r.error, r.ok ? "ok" : "fail");
      refreshNet();
    }));
    const act = (label, method, cls, confirmText) => {
      const b = h("button.btn.small" + (cls ? "." + cls : ""), label);
      b.addEventListener("click", M.act(b, async () => {
        if (confirmText && !(await M.confirm(label, confirmText, true))) return;
        const r = await M.call(method);
        if (!r.ok) M.toast(r.error, "fail");
        refreshNet();
      }));
      return b;
    };
    host.replaceChildren(modes, h("div.crx-panel",
      h("div.crx-kv", h("span", "SERVER"), net.server ? h("b.mono", `${net.server}:${net.server_port}`) : M.chip("not configured", "warn")),
      !net.server ? h("p.note", net.can_configure_server ? "Set the ZEUS server address in Settings → ZEUS NETWORK." : "An administrator sets the ZEUS server address in Settings.") : null,
      h("div.crx-kv", h("span", "API KEY"), net.has_key ? h("b.mono", { title: net.key_storage }, net.masked_key) : M.chip("none saved", "warn")),
      h("div.crx-keyrow", keyInput, save),
      h("div.note", `Stored in ${net.key_storage}. The key is never shown in full again.`),
      h("div.row.crx-actions", act("Connect", "crudex_connect", "primary"), act("Disconnect", "crudex_disconnect"),
        net.has_key ? act("Remove saved key", "crudex_remove_key", "danger", "The key is deleted from Windows Credential Manager and this device disconnects. You will need the key again to reconnect.") : null),
      h("div.crx-kv", h("span", "CONNECTION STATUS"), net.state === "ONLINE" || net.state === "SYNCING" ? M.gchip("● Connected", "ok") : M.gchip("○ Not Connected", net.state === "ERROR" ? "fail" : "plain")),
      h("div.crx-kv", h("span", "STATE"), netChip(net.state)),
      net.account ? h("div.crx-kv", h("span", "ACCOUNT"), h("b", net.account)) : null,
      h("p.note.crx-detail", net.detail || ""),
      h("div.crx-kv", h("span", "LAST SYNC"), h("span.mono", ago(net.last_sync_at))),
      net.pending_events || net.active_transfers ? h("div.note", `${net.pending_events} event(s) waiting · ${net.active_transfers} transfer(s) active`) : null,
      h("div.crx-kv", h("span", "THIS DEVICE"), h("span", net.device_name || "—"))));
  }

  /* Pushed by the client as things really happen: state changes, deliveries,
     receipts, transfer progress, task states, presence, pairing. */
  let netTimer = null;
  function onNetEvent(ev) {
    if (!ev || !ev.type) return;
    if (ev.type === "message" && ev.direction === "in")
      M.toast(`${ev.kind === "notification" ? "Notification" : "Message"} from ${ev.peer_name || "a device"}: ${String(ev.text || "").slice(0, 140)}`, "ok");
    if (ev.type === "revoked") M.toast(ev.message || "This device was revoked", "fail");
    if (ev.type === "task" && ev.direction === "out" && ["COMPLETED", "FAILED"].includes(ev.state))
      M.toast(`Remote task ${ev.state.toLowerCase()}`, ev.state === "COMPLETED" ? "ok" : "warn");
    if (ev.type === "conflict") M.toast(`Sync conflict in ${ev.path} - both versions kept`, "warn");
    clearTimeout(netTimer);
    netTimer = setTimeout(() => {
      refreshNet();
      const cur = M.current && M.current();
      if (cur && M.screens[cur.id] && M.screens[cur.id].live === "network") M.refresh();
    }, ev.type === "transfer" ? 700 : 250);
  }
  async function changePassword() {
    const v = await M.form("Change password", [{ name: "current", label: "Current password", type: "password" },
      { name: "new", label: "New password", type: "password" }, { name: "confirm", label: "Confirm new password", type: "password" }], { submit: "Change" });
    if (!v) return;
    const r = await M.call("account_password", v);
    M.toast(r.ok ? "Password changed" : r.reason, r.ok ? "ok" : "fail");
  }
  M.changePassword = changePassword;

  /* ----------------------------------------------------------------- start ---- */
  M.shell = {
    async start(authState) {
      build();
      M.go("command");
      M.wave.wake(2500);                              // a short welcome swell, then the wave settles
      try {
        hello = await M.call("hello");
        speak = hello.speak !== false;
        M.$("#s-version").textContent = `VOICE CORE v${hello.version || "?"}`;
        M.$("#s-username").textContent = hello.user || (authState && authState.user) || "—";
        M.$("#s-initials").textContent = (hello.user || "?").replace(/[^a-z0-9]/gi, "").slice(0, 2).toUpperCase();
        M.$("#s-user").lastChild.replaceWith(M.chip(String(hello.role || "").toUpperCase(), hello.role === "admin" ? "live" : "plain"));
        applyState(await M.call("state"));
      } catch (e) {
        console.error("Shell start metadata error:", e);
      }
      M.on("state", applyState);
      M.on("level", (p) => applyLevel(p.level || 0));
      M.on("stt", onStt);
      M.on("approval", (req) => { approvals.push(req); showApproval(); });
      M.on("approval_done", (p) => { const i = approvals.findIndex((a) => a.id === (p && p.id)); if (i >= 0) { approvals.splice(i, 1); showApproval(); } });
      M.on("security", securityToast);
      M.on("notification", (n) => M.toast(n.message, "ok"));
      M.on("lockdown", (s) => M.toast(s.engaged ? "EMERGENCY LOCKDOWN ENGAGED" : "Lockdown released", s.engaged ? "fail" : "ok"));
      M.on("refresh", () => M.refresh());
      M.on("crudex", onNetEvent);
      paintVoice();
      refreshStatus();
      setInterval(refreshStatus, 5000);
      document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshStatus(); });
    },
  };
})();
