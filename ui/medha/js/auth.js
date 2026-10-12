/* ZEUS sign-in. The page renders states; auth/web_login.py decides every one.
   Nothing here can mark the session signed in - only a backend reply can. */
(function () {
  "use strict";
  const M = window.ZEUS;
  const { h, esc } = M;

  const FACE_STEPS = ["CAMERA READY", "SCANNING", "VERIFYING IDENTITY", "FACE DETECTED", "LIVENESS CHECK", "IDENTITY VERIFIED", "ACCESS GRANTED"];
  const TONES = { "LOCKED": "", "INITIALIZING": "live", "CAMERA READY": "live", "SCANNING": "live", "FACE DETECTED": "live",
    "LIVENESS CHECK": "live", "VERIFYING IDENTITY": "live", "IDENTITY VERIFIED": "ok", "ACCESS GRANTED": "ok",
    "ACCESS DENIED": "fail", "CAMERA ERROR": "fail", "AUTHENTICATION TIMEOUT": "warn", "SECOND FACTOR REQUIRED": "warn",
    "SESSION EXPIRED": "warn", "LOCKDOWN": "fail", "SETUP": "live" };
  const WORKSPACE_STEPS = [["database", "Account & data store"], ["plugins", "Plugins"], ["memory", "Memory (Hermes)"],
    ["engine", "Orchestrator, agents & tools"], ["voice", "Voice"]];
  const SETTLE_MS = 12000;

  let root = null, card = null, state = null, faceUnsub = null, workspaceUnsub = null, onDone = null;

  /* The line over the wave repeats the backend's state in plain words - it
     never says more than the state the backend reported. */
  const VOICE = { "LOCKED": "Sign in to continue", "LOCKDOWN": "Emergency lockdown is engaged", "INITIALIZING": "Starting your workspace",
    "CAMERA READY": "Look at the camera", "SCANNING": "Hold still", "FACE DETECTED": "Face found", "LIVENESS CHECK": "Checking it is really you",
    "VERIFYING IDENTITY": "Verifying", "IDENTITY VERIFIED": "Identity verified", "ACCESS GRANTED": "Welcome",
    "ACCESS DENIED": "Access denied", "CAMERA ERROR": "Camera unavailable - use your password", "AUTHENTICATION TIMEOUT": "Timed out - try again",
    "SECOND FACTOR REQUIRED": "One more step", "SESSION EXPIRED": "Session expired - sign in again", "SETUP": "Create the administrator" };

  function setState(label, detail) {
    const node = M.$(".auth-state", card);
    if (!node) return;
    node.dataset.tone = TONES[label] || "";
    const line = M.$("#auth-voice");
    if (line) { line.textContent = VOICE[label] || label; line.classList.toggle("live", TONES[label] === "live"); }
    node.innerHTML = `<span class="led"></span><span>${esc(label)}</span>${detail ? `<span class="muted" style="letter-spacing:.04em;text-transform:none;font-family:var(--font-ui)">· ${esc(detail)}</span>` : ""}`;
  }
  function message(text, tone) {
    const node = M.$(".auth-msg", card);
    if (node) { node.textContent = text || ""; node.className = "auth-msg" + (tone ? " " + tone : ""); }
  }
  function shake() { card.classList.remove("shake"); void card.offsetWidth; card.classList.add("shake"); }

  function field(name, label, type) {
    const input = h("input", { type: type || "text", name, placeholder: " ", autocomplete: "off", spellcheck: "false" });
    const wrap = h("div.float", input, h("label", label));
    if (type === "password") {
      const strip = h("div.keystrip", Array.from({ length: 16 }, () => h("i")));
      input.addEventListener("input", () => {
        const n = input.value.length;
        strip.querySelectorAll("i").forEach((bar, i) => bar.classList.toggle("on", i < Math.min(16, n)));
      });
      return [wrap, strip, input];
    }
    return [wrap, null, input];
  }

  function frame() {
    card = h("div.auth-card");
    M.wave.attach();
    M.wave.mode("auth");
    root = h("div.auth-screen",
      h("div.auth-scene",
        h("div.auth-brand", M.badge(), h("div", h("div.word", "ZEUS"),
          h("div.tag", "Autonomous AI Command & Intelligence System"))),
        h("div.auth-voice", h("span#auth-voice.voice-status-text", "")),
        h("div.auth-foot", h("span", "LOCAL ONLY · NO CLOUD · EVERY ACTION AUDITED"), h("span.spacer"), h("span#auth-clock.num"))),
      h("div.auth-panel", card));
    document.body.appendChild(root);
    document.body.classList.add("auth");
    const tick = () => { const c = M.$("#auth-clock"); if (c) c.textContent = new Date().toLocaleTimeString([], { hour12: false }); };
    tick();
    root._clock = setInterval(tick, 1000);
    /* The scene moves while somebody is here and settles when they stop - an
       endless animation keeps the embedded browser repainting at full rate. */
    const wake = () => {
      root.classList.add("awake");
      M.wave.wake(SETTLE_MS);
      clearTimeout(root._settle);
      root._settle = setTimeout(() => root.classList.remove("awake"), SETTLE_MS);
    };
    ["keydown", "pointerdown", "pointermove"].forEach((type) => root.addEventListener(type, wake, { passive: true }));
    wake();
  }

  function header(title, sub) {
    return [h("div.auth-state"), h("h2.auth-title", title), h("div.auth-sub", sub)];
  }

  /* -------------------------------------------------------------- phases ---- */
  async function refresh() {
    try { state = await M.call("auth_state"); }
    catch (e) { card.replaceChildren(h("div.auth-state"), h("h2.auth-title", "Not available"), h("p.auth-sub", e.message)); return; }
    if (state.phase === "signed_in") return initializing();
    if (state.phase === "setup") return setup();
    if (state.phase === "second_factor") return secondFactor();
    if (state.phase === "unlock") return unlock();
    return login(state.remembered === "face" && state.methods.face ? "face" : "password");
  }

  function lockdownNote() {
    return state && state.lockdown && state.lockdown.engaged
      ? M.callout("EMERGENCY LOCKDOWN is engaged - after sign-in only status and recovery tools work; an administrator releases it.", "fail") : null;
  }

  function switcher(active, onChange) {
    const faceOk = state.methods.face;
    const pw = h("button.tab" + (active === "password" ? ".active" : ""), { onclick: () => onChange("password") }, "Password");
    const face = h("button.tab" + (active === "face" ? ".active" : ""), { onclick: () => faceOk && onChange("face"),
      disabled: !faceOk, title: faceOk ? "Sign in with your face" : (state.face.reason || "Face sign-in is not available") }, "Face");
    return h("div.tabs.auth-switch", pw, face);
  }

  function login(method) {
    stopFace();
    card.replaceChildren(...header("Sign in", "Use your password or your face - whichever you prefer."));
    card.appendChild(switcher(method, (m) => login(m)));
    const note = lockdownNote();
    if (note) card.appendChild(note);
    if (method === "face") { faceView("login"); return; }
    const [userWrap, , user] = field("username", "User ID");
    const [passWrap, strip, pass] = field("password", "Password", "password");
    const go = M.button("Sign in", "primary", async () => {
      if (!user.value.trim() || !pass.value) { message("Enter your user ID and password."); shake(); return; }
      setState("VERIFYING IDENTITY");
      const r = await M.call("auth_login", { username: user.value.trim(), password: pass.value });
      pass.value = ""; strip.querySelectorAll("i").forEach((i) => i.classList.remove("on"));
      if (r.ok && r.pending_factor === "face") { await refresh(); return; }
      if (r.ok) { setState("ACCESS GRANTED"); message("", ""); return initializing(); }
      setState("ACCESS DENIED"); message(r.message); shake();
    });
    card.append(userWrap, passWrap, strip, h("div.auth-msg"), h("div.auth-actions", go), footerLinks(), unavailableNote());
    setState(state.lockdown && state.lockdown.engaged ? "LOCKDOWN" : "LOCKED");
    [user, pass].forEach((i) => i.addEventListener("keydown", (e) => { if (e.key === "Enter") go.click(); }));
    setTimeout(() => user.focus(), 60);
    if (!state.methods.face && state.face && state.face.reason) message("Face sign-in: " + state.face.reason, "info");
  }

  function unlock(method) {
    stopFace();
    method = method || "password";
    card.replaceChildren(...header("Unlock", `The session of ${state.display_name || state.user} is locked.`));
    card.appendChild(switcher(method, (m) => unlock(m)));
    if (method === "face") { faceView("unlock"); card.appendChild(switchUser()); return; }
    const [passWrap, strip, pass] = field("password", "Password", "password");
    const go = M.button("Unlock", "primary", async () => {
      if (!pass.value) { message("Enter your password."); shake(); return; }
      setState("VERIFYING IDENTITY");
      const r = await M.call("auth_unlock", { password: pass.value });
      pass.value = ""; strip.querySelectorAll("i").forEach((i) => i.classList.remove("on"));
      if (r.ok && r.pending_factor === "face") { await refresh(); return; }
      if (r.ok) { setState("ACCESS GRANTED"); return initializing(); }
      setState("ACCESS DENIED"); message(r.message); shake();
    });
    card.append(passWrap, strip, h("div.auth-msg"), h("div.auth-actions", go), switchUser());
    setState(state.session_expired ? "SESSION EXPIRED" : "LOCKED", state.session_expired ? "idle limit reached" : "");
    pass.addEventListener("keydown", (e) => { if (e.key === "Enter") go.click(); });
    setTimeout(() => pass.focus(), 60);
  }

  function switchUser() {
    return h("div.auth-links",
      h("button.btn.ghost.small", { onclick: async () => { await M.call("auth_switch_user"); refresh(); } }, "Switch user"),
      h("button.btn.ghost.small", { onclick: () => M.call("quit") }, "Quit ZEUS"));
  }
  function footerLinks() {
    return h("div.auth-links", h("span.note", "Forgot your password? An administrator can reset it."),
      h("button.btn.ghost.small", { onclick: () => M.call("quit") }, "Quit"));
  }
  function unavailableNote() { return h("div.auth-unavailable", state.methods.unavailable_note); }

  function setup() {
    card.replaceChildren(...header("Create the administrator", "First run: this account administers ZEUS on this machine."));
    const [u, , user] = field("username", "User ID");
    const [p, strip, pass] = field("password", "Password", "password");
    const [c, , confirm] = field("confirm", "Confirm password", "password");
    const go = M.button("Create and sign in", "primary", async () => {
      const r = await M.call("auth_setup", { username: user.value.trim(), password: pass.value, confirm: confirm.value });
      pass.value = confirm.value = "";
      if (r.ok) { setState("ACCESS GRANTED"); return initializing(); }
      setState("ACCESS DENIED"); message(r.message); shake();
    });
    card.append(u, p, strip, c, h("div.note", `At least ${state.password_policy.min_length} characters. It is stored only as an Argon2id hash.`),
      h("div.auth-msg"), h("div.auth-actions", go));
    setState("SETUP");
    setTimeout(() => user.focus(), 60);
  }

  function secondFactor() {
    card.replaceChildren(...header("Second factor", `Password accepted for ${state.user}. This account also needs its face.`));
    faceView("second_factor");
    card.appendChild(h("div.auth-links",
      h("button.btn.ghost.small", { onclick: async () => { await M.call("auth_cancel_factor"); refresh(); } }, "Cancel and sign out")));
    setState("SECOND FACTOR REQUIRED");
  }

  /* ---------------------------------------------------------------- face ---- */
  function faceView(purpose) {
    const circumference = 2 * Math.PI * 104;
    const seg = circumference / 5;
    const rings = Array.from({ length: 5 }, (_, i) =>
      `<circle cx="110" cy="110" r="104" stroke="var(--steel-2)" stroke-dasharray="${seg - 8} ${circumference - seg + 8}" stroke-dashoffset="${-i * seg}" data-seg="${i}"/>`).join("");
    const scanner = h("div.scanner", M.html(`<svg class="segments" viewBox="0 0 220 220">${rings}</svg>`),
      h("div.preview", h("span", "The camera opens only when you start, for about two seconds.")), h("div.sweep"));
    const steps = h("div.face-steps", FACE_STEPS.map((s) => h("div", { "data-step": s }, s.charAt(0) + s.slice(1).toLowerCase())));
    const start = M.button(purpose === "second_factor" ? "Check my face" : "Scan my face", "primary", async () => {
      resetFace(scanner, steps);
      const r = await M.call("auth_face", { purpose });
      if (!r.ok) { setState("ACCESS DENIED"); message(r.message); shake(); return; }
      start.disabled = true;
      message("Opening the camera…", "info");
    });
    const cancel = h("button.btn", { onclick: () => M.call("auth_face_cancel") }, "Stop");
    card.append(scanner, steps, h("div.auth-msg"), h("div.auth-actions", start, cancel));
    stopFace();
    faceUnsub = M.on("auth_face", (ev) => {
      if (ev.purpose !== purpose) return;
      const img = M.$("img", scanner);
      if (ev.preview) {
        if (img) img.src = ev.preview; else M.$(".preview", scanner).replaceChildren(h("img", { src: ev.preview, alt: "" }));
      }
      if (ev.state === "SCANNING") {
        scanner.classList.add("scanning");
        const kept = Math.round((ev.progress || 0) * 5);
        scanner.querySelectorAll("circle").forEach((c, i) => c.setAttribute("stroke", i < kept ? "var(--flare)" : "var(--steel-2)"));
      }
      const idx = FACE_STEPS.indexOf(ev.state);
      if (idx >= 0) steps.querySelectorAll("div").forEach((d, i) => {
        d.classList.toggle("done", i < idx || (i === idx && ["IDENTITY VERIFIED", "ACCESS GRANTED"].includes(ev.state)));
        d.classList.toggle("active", i === idx && !["IDENTITY VERIFIED", "ACCESS GRANTED"].includes(ev.state));
      });
      if (ev.state === "LIVENESS CHECK" && ev.passed === false) M.$('[data-step="LIVENESS CHECK"]', steps).classList.add("fail");
      setState(ev.state === "SCANNING" ? "SCANNING" : ev.state === "CANCELLED" ? "LOCKED" : ev.state);
      if (["ACCESS DENIED", "CAMERA ERROR", "AUTHENTICATION TIMEOUT", "CANCELLED"].includes(ev.state)) {
        scanner.classList.remove("scanning");
        scanner.dataset.tone = ev.state === "CANCELLED" ? "" : "fail";
        start.disabled = false;
        if (ev.message) { message(ev.message); shake(); }
        if (ev.ended) setTimeout(refresh, 1200);
        if (ev.state === "CAMERA ERROR") message((ev.message || "Camera error.") + " Your password still works.");
      }
      if (ev.state === "ACCESS GRANTED") {
        scanner.classList.remove("scanning");
        scanner.dataset.tone = "ok";
        message("", "");
        setTimeout(initializing, 600);
      }
    });
  }
  function resetFace(scanner, steps) {
    scanner.dataset.tone = "";
    steps.querySelectorAll("div").forEach((d) => d.classList.remove("done", "active", "fail"));
    message("", "");
  }
  function stopFace() { if (faceUnsub) { faceUnsub(); faceUnsub = null; } }

  /* --------------------------------------------------------- initializing ---- */
  async function initializing() {
    stopFace();
    const ws = await M.call("auth_state").then((s) => (state = s).workspace).catch(() => ({ state: "unknown", steps: [] }));
    if (state.phase !== "signed_in") return refresh();
    card.replaceChildren(...header(`Welcome, ${state.display_name || state.user || "operator"}`, "Starting your workspace. Each line changes only when that part is running."));
    setState("INITIALIZING");
    const list = h("div.boot", WORKSPACE_STEPS.map(([key, label]) => h("div", { "data-step": key }, h("span", label), h("span.num", "waiting"))));
    card.append(list, h("div.auth-msg"));
    const paint = (step, st, detail) => {
      const row = M.$(`[data-step="${step}"]`, list);
      if (!row) return;
      row.className = st;
      row.lastChild.textContent = st === "done" ? "ready" : st === "running" ? "starting…" : st + (detail ? ` - ${detail}` : "");
    };
    (ws.steps || []).forEach((s) => paint(s.step, s.state, s.detail));
    if (workspaceUnsub) workspaceUnsub();
    workspaceUnsub = M.on("workspace", (ev) => {
      if (ev.step) paint(ev.step, ev.step_state, ev.detail);
      if (ev.state === "ready") finish();
      if (ev.state === "failed") { setState("ACCESS DENIED", "workspace failed"); message(ev.detail || "The workspace did not start - see the log.", ""); }
    });
    if (ws.state === "ready") return finish();
  }

  function finish() {
    if (workspaceUnsub) { workspaceUnsub(); workspaceUnsub = null; }
    stopFace();
    setState("ACCESS GRANTED");
    root.classList.add("leaving");
    setTimeout(() => {
      clearInterval(root._clock);
      clearTimeout(root._settle);
      M.wave.sleep();
      root.remove();
      document.body.classList.remove("auth");
      if (onDone) onDone(state);
    }, 420);
  }

  M.auth = {
    start(done) {
      onDone = done;
      frame();
      card.append(...header("ZEUS", "connecting…"));
      refresh();
    },
  };
})();
