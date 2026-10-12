/* COMMAND: the voice home - the wave, what ZEUS is doing, field telemetry and
   operations from the real registers, quick actions, and the conversation.
   Every answer shows the pipeline that produced it. */
(function () {
  "use strict";
  const M = window.ZEUS;
  const { h } = M;

  /* Quick actions: each is a phrase the engine routes to a real workflow, a
     file to pick first, or a screen. */
  const QUICK = [
    { label: "Why did the finish move?", cmd: "Why did the finish date move?" },
    { label: "Field update", ask: "Site update - any language (e.g. P17 dhalai 40% ho gaya)", cmd: (q) => `field update: ${q}` },
    { label: "Capture site evidence", screen: "capture" },
    { label: "Live progress", screen: "progress" },
    { label: "Delay chronology", screen: "chronology" },
    { label: "Offline sync", screen: "sync" },
    { label: "Plant status", screen: "industrial", params: { tab: "Overview" } },
    { label: "Open alerts", screen: "industrial", params: { tab: "Alerts" } },
    { label: "Trend C-301", cmd: "show the trend for C-301" },
    { label: "Operations agent", cmd: "run the operations agent" },
    { label: "Summarize a document", pick: "document", cmd: (p) => `summarize "${p}"` },
    { label: "Inspect a scanned report", pick: "document", cmd: (p) => `inspect "${p}"` },
    { label: "Inspect an image", pick: "image", cmd: (p) => `analyze the image ${p}` },
    { label: "Analyze a spreadsheet", pick: "document", cmd: (p) => `analyze the spreadsheet "${p}"` },
    { label: "Search my documents", ask: "Search for", cmd: (q) => `search my documents for ${q}` },
    { label: "Engineering calculation", ask: "e.g. pump power for flow = 50 m3/h, head = 30 m, efficiency = 0.72", cmd: (q) => (/^(calculate|compute|what)/i.test(q) ? q : `calculate ${q}`) },
    { label: "Approval note", ask: "Equipment tag (e.g. P-101)", cmd: (q) => `generate an approval note for ${q}` },
    { label: "Write a report", ask: "Report topic", cmd: (q) => `write a report on ${q}` },
    { label: "Make a presentation", ask: "Presentation topic", cmd: (q) => `make a presentation on ${q}` },
    { label: "Write code", ask: "What should the script do?", cmd: (q) => `write a python script to ${q}` },
    { label: "Prove it (security)", cmd: "prove it" },
    { label: "Add a task", ask: "Task", cmd: (q) => `add todo ${q}` },
    { label: "Company question", screen: "company" },
  ];

  async function runQuick(q) {
    if (q.screen) return M.go(q.screen, q.params || {});
    if (q.pick) {
      const r = await M.call("pick_file", { purpose: q.pick });
      if (r.path) M.submit(q.cmd(r.path));
      return;
    }
    if (q.ask) {
      const v = await M.form(q.label, [{ name: "value", label: q.ask }], { submit: "Run" });
      if (v && v.value.trim()) M.submit(q.cmd(v.value.trim()));
      return;
    }
    M.submit(q.cmd);
  }

  function stages(pipeline) {
    const list = (pipeline && pipeline.stages) || [];
    if (!list.length) return null;
    return h("div.stage-row", list.map((s) => h("span.stage-pill." + (s.status === "failed" ? "failed" : s.status === "running" ? "running" : "ok"), { title: s.detail || "" }, s.stage)));
  }

  function turnView(turn) {
    const r = turn.result || {};
    const p = r.pipeline || {};
    const reply = h("div.reply");
    if (turn.pending) reply.appendChild(M.loading(turn.spoken ? "listening, then thinking" : "working - the pipeline is running"));
    else {
      reply.appendChild(M.answer(r.response || "(no reply)"));
      const meta = h("div.meta",
        p.agent || r.agent ? M.chip(p.agent || r.agent, "plain") : null,
        p.model ? M.chip(p.model, "info") : null,
        p.validated === true ? M.chip("validated", "ok") : null,
        p.succeeded === false ? M.chip("did not complete", "fail") : null);
      reply.appendChild(meta);
      const st = stages(p);
      if (st) reply.appendChild(st);
      (p.artifacts || []).forEach((a) => reply.appendChild(h("div.row", { style: "margin-top:8px" },
        M.button(`Open ${M.basename(a)}`, "small", async () => { const x = await M.call("open_path", { path: a }); if (!x.success) M.toast(x.error || "could not open", "fail"); }))));
      if (r.ui_action && r.ui_action.type === "switch_tab") {
        const map = { TODO: "productivity", TASKS: "productivity", CALENDAR: "productivity", NOTES: "productivity", VAULT: "vault", SYSTEM: "models", MEMORY: "memory" };
        const target = map[String(r.ui_action.tab || "").toUpperCase()];
        if (target) reply.appendChild(M.button(`Open ${M.screens[target].label}`, "small", () => M.go(target)));
      }
    }
    return h("div.turn", h("div.ask", turn.spoken ? "🎙 " : "", turn.request), reply);
  }

  /* ---------------------------------------------------- the side cards ---- */
  /* Everything below is drawn from plant_telemetry. A number that is not in
     the reply is shown as "—", never invented; DEMO data is labelled DEMO. */
  function sourceChip(t) {
    const label = String((t.production && t.production.label) || (t.telemetry && t.telemetry.label) || "");
    if (t.telemetry && t.telemetry.live) return M.chip("LIVE", "ok");
    if (label.includes("DEMO")) return M.chip("DEMO DATA", "sample");
    return M.chip(label || "STORED READINGS", "info");
  }

  function telemetryCard(card, t) {
    const p = t.production || {}, latest = p.latest || {}, months = p.months || [];
    M.$("#tele-chip", card).replaceChildren(sourceChip(t), ...(t.telemetry && t.telemetry.live ? [] : [M.chip("NO LIVE PLANT CONNECTION", "warn")]));
    const body = [];
    if (latest.month) {
      const change = Number(p.change_pct_vs_previous);
      const prev = months.length > 1 ? months[months.length - 2].month : "";
      body.push(h("div.stack", { style: "gap:4px" },
        h("div.line", h("span", "CRUDE THROUGHPUT"), isNaN(change) ? h("span", "—")
          : h("span." + (change >= 0 ? "up" : "down"), `${change >= 0 ? "+" : ""}${M.num(change, 1)}% vs ${prev}`)),
        h("div.big", h("span.v", M.num(latest.crude_kt, 0)), h("span.u", `KT · ${latest.month}`)),
        M.spark(months.map((m) => m.crude_kt), "#00f2fe")));
      const util = Number(latest.utilisation_pct);
      body.push(h("div.sep",
        h("div.line", h("span", "UTILISATION"), h("b", isNaN(util) ? "—" : `${M.num(util, 1)} %`)),
        h("div.bar-track", h("div.bar-fill.spectrum", { style: `width:${isNaN(util) ? 0 : Math.max(0, Math.min(100, util))}%` }))));
      body.push(h("div.tiles",
        h("div.tile", h("div.k", "DOWNTIME"), h("div.v", latest.downtime_h == null ? "—" : `${M.num(latest.downtime_h, 0)} h`)),
        h("div.tile", h("div.k", "ON-SPEC"), h("div.v", { style: "color:var(--cyan)" }, latest.on_spec_pct == null ? "—" : `${M.num(latest.on_spec_pct, 1)} %`))));
    } else {
      body.push(h("p.note", "No production records on this workstation. Import readings from the Industrial workspace."));
    }
    const series = ((t.telemetry || {}).series || []).find((s) => s.direction === "rising") || ((t.telemetry || {}).series || [])[0];
    if (series) {
      body.push(h("div.sep", h("div.line", h("span", `${series.equipment} ${String(series.parameter || "").toUpperCase()}`),
        h("b", `${M.num(series.latest, 2)} ${series.unit || ""}${series.direction === "rising" ? " ↑" : series.direction === "falling" ? " ↓" : ""}`))));
    }
    if (p.plant) body.push(h("div.note", `${p.plant}${p.capacity_bpd ? ` · capacity ${M.num(p.capacity_bpd, 0)} bpd` : ""}`));
    card.replaceChildren(card.firstChild, ...body);
  }

  const STATUS_COLOUR = { operational: "#10b981", running: "#10b981", maintenance: "#f59e0b", standby: "#38bdf8", shutdown: "#f43f5e", fault: "#f43f5e" };

  function operationsCard(card, t) {
    const o = t.operations || {}, eq = o.equipment || {}, byStatus = eq.by_status || {};
    const total = Number(eq.total) || Object.values(byStatus).reduce((a, b) => a + Number(b || 0), 0);
    const sev = o.alerts_by_severity || {};
    M.$("#ops-chip", card).replaceChildren(M.chip(`${M.num(o.open_alerts, 0)} ALERTS`, sev.critical ? "fail" : o.open_alerts ? "warn" : "ok"));
    const body = [];
    if (total) {
      body.push(h("div.eyebrow", { style: "color:var(--ink-3)" }, `EQUIPMENT · ${M.num(total, 0)}`));
      body.push(h("div.mix", Object.entries(byStatus).map(([status, count]) => {
        const pct = Math.round((Number(count) / total) * 100);
        return h("div.r", { style: `--c:${STATUS_COLOUR[status] || "#38bdf8"}` },
          h("span.n", h("i"), status.charAt(0).toUpperCase() + status.slice(1)),
          h("span.t", h("div.bar-track", h("div.bar-fill", { style: `width:${pct}%` })), h("span.p", `${M.num(count, 0)}`)));
      })));
    } else body.push(h("p.note", "No equipment register on this workstation."));
    body.push(h("div.tiles",
      h("div.tile", h("div.k", "MAINT. DUE 14D"), h("div.v", M.num(o.maintenance_due_14d, 0))),
      h("div.tile", h("div.k", "OPEN PERMITS"), h("div.v", { style: "color:var(--cyan)" }, M.num(o.open_permits, 0)))));
    const top = (o.top_alerts || [])[0];
    if (top) {
      const mini = h("div.alert-mini." + String(top.severity || "").toLowerCase(), { title: top.description || "", style: "cursor:pointer", onclick: () => M.go("industrial", { tab: "Alerts" }) },
        h("div.d", h("i"), h("span", `${top.equipment || "—"} · ${top.description || ""}`)), h("span.s", top.severity || ""));
      body.push(h("div.sep", mini));
    }
    body.push(h("div.row", M.button("Industrial workspace", "small", () => M.go("industrial"))));
    card.replaceChildren(card.firstChild, ...body);
  }

  /* ------------------------------------------- the ZEUS project cards ---- */
  /* The same two glass cards, drawn from the current project (jarvis_home):
     forecast against baseline, the finish's movement, progress, and what is
     waiting in the field. PLANT switches the cards back to plant telemetry. */
  function toggle(label, mode) {
    return h("button.btn.small.ghost", { style: "height:24px;padding:0 10px", onclick: () => { M.homeMode = mode; M.refresh(); } }, label);
  }

  function projectCards(left, right, home) {
    const J = M.jv, o = home.overview || {}, p = o.project || {}, c = o.counts || {};
    J.project = p.project_id || J.project;
    const origin = p.origin_label && p.origin_label !== "RECORDED" ? M.chip(p.origin_label, "sample") : M.chip("RECORDED", "info");
    left.replaceChildren(h("div.card-head", h("h3", "Live Progress"), h("div.row", { style: "flex-wrap:nowrap" }, origin, toggle("PLANT", "plant"))));
    right.replaceChildren(h("div.card-head", h("h3", "Field & Schedule"), h("span", M.chip(`${M.num(c.pending_approvals || 0, 0)} TO APPROVE`, c.pending_approvals ? "warn" : "ok"))));
    if (o.schedule !== "approved") {
      left.append(h("p.note", `${p.name || "Project"}: ${o.note || "no approved schedule yet"}`), M.button("Projects", "small", () => M.go("projects")));
      right.append(h("p.note", "Import and approve a schedule to start tracking."));
      return;
    }
    const variance = o.finish_variance_days;
    const history = (o.finish_history || []).map((r) => r.project_finish).filter(Boolean);
    const base = o.baseline_finish ? new Date(o.baseline_finish + "T00:00:00").getTime() : null;
    left.append(h("div.stack", { style: "gap:4px" },
      h("div.jv-mini", h("span", String(p.name || "").toUpperCase().slice(0, 34))),
      h("div.line", h("span", "FORECAST FINISH"), variance == null ? h("span", "—") : h("span." + (variance > 0 ? "down" : "up"), `${variance > 0 ? "+" : ""}${variance} d vs baseline`)),
      h("div.big", h("span.v", J.d(o.forecast_finish).slice(0, 6)), h("span.u", J.d(o.forecast_finish).slice(7))),
      base && history.length > 1 ? M.spark(history.map((d) => (new Date(d + "T00:00:00").getTime() - base) / 864e5), "#00f2fe") : null),
      h("div.sep", J.bar(o.actual_progress_pct, o.planned_progress_pct)),
      h("div.tiles", h("div.tile", h("div.k", "DATA DATE"), h("div.v", J.d(o.data_date))),
        h("div.tile", h("div.k", "CRITICAL"), h("div.v", { style: "color:var(--cyan)" }, `${M.num(o.critical, 0)} · ${M.num(o.near_critical, 0)} near`))),
      h("div.row", M.button("Why did it move?", "small", () => M.go("why")), M.button("Schedule", "small ghost", () => M.go("schedule"))));
    const rows = [["Open blockers", c.open_blockers, "#f43f5e"], ["Links to confirm", c.links_to_confirm, "#f59e0b"],
      ["Sync conflicts", c.open_conflicts, "#a855f7"], ["Forecast changes", c.forecast_changes, "#38bdf8"], ["Evidence items", c.evidence, "#10b981"]];
    const top = Math.max(1, ...rows.map((r) => Number(r[1]) || 0));
    right.append(h("div.eyebrow", { style: "color:var(--ink-3)" }, `ACTIVITIES · ${M.num(c.activities, 0)}`),
      h("div.mix", rows.map(([label, value, colour]) => h("div.r", { style: `--c:${colour}` }, h("span.n", h("i"), label),
        h("span.t", h("div.bar-track", h("div.bar-fill", { style: `width:${Math.round(((Number(value) || 0) / top) * 100)}%` })), h("span.p", M.num(value || 0, 0)))))));
    const next = (o.deadlines_open || [])[0];
    if (next) right.append(h("div.sep", h("div.alert-mini.high", { style: "cursor:pointer", onclick: () => M.go("chronology") },
      h("div.d", h("i"), h("span", `${next.clause} ${next.notice_type} · ${next.activity_code || "project"}`)), h("span.s", `${next.days_left} d`))));
    if (o.deadlines_missed) right.append(M.callout(`${o.deadlines_missed} contract notice deadline(s) missed`, "fail"));
    right.append(h("div.row", M.button("Live progress", "small", () => M.go("progress")), M.button("Field capture", "small ghost", () => M.go("capture"))));
  }

  /* ---------------------------------------------------------- the home ---- */
  M.screen("command", {
    group: "COMMAND", label: "Command Center", icon: "command", title: "Command Center", home: true,
    sub: "Speak or type. Every answer shows the agent, the model and the stages that produced it.",
    async render(host) {
      const left = h("aside.card.side.left.hud", h("div.card-head", h("h3", "Field Telemetry"), h("span#tele-chip")), M.loading("reading plant records"));
      const right = h("aside.card.side.right.hud", h("div.card-head", h("h3", "Operations"), h("span#ops-chip")), M.loading("reading the registers"));
      const convo = h("div.home-convo.hud.conversation");
      M.conversation.slice(-8).forEach((t) => convo.appendChild(turnView(t)));
      const center = h("div.home-center",
        h("div.voice-line", h("span#voice-prompt.voice-status-text", "Ready when you are"), h("span#voice-sub.voice-sub", "")),
        convo);
      const quick = h("div.quick", QUICK.map((q) => {
        const b = h("button.btn.small", q.label);
        b.addEventListener("click", M.act(b, () => runQuick(q)));
        return b;
      }));
      const extras = h("div.row", { style: "flex-wrap:nowrap" },
        M.button("History", "small mono", async () => {
          const items = await M.call("history");
          M.openSheet("This session", items.length ? h("div.deck", items.slice().reverse().map((it) => h("div.card",
            h("div.row.between", h("span.mono", it.request), h("span.note", M.time(it.ts))),
            M.answer((it.result && it.result.response) || "")))) : M.empty("Nothing yet."));
        }),
        M.button("Last run", "small mono", async () => {
          const s = await M.call("status");
          const run = s.pipeline || s.last_run;
          if (!run) { M.openSheet("Pipeline", M.empty("No run recorded yet.")); return; }
          const st = run.stages || {}, notes = run.stage_notes || {};
          M.openSheet(s.pipeline ? "Running now" : "Last run", h("div.stack",
            M.kv(run, [["task", "Request"], ["kind", "Classified as"], ["agent", "Agent"], ["model", "Model"], ["status", "Status"]]),
            M.table(["Stage", "State", "Note"], Object.keys(st).map((k) => ({ Stage: k, State: st[k], Note: notes[k] || "" }))),
            h("p.note", `Tools run: ${(run.tools || []).join(", ") || "none - answered by the model"}`)));
        }));
      host.appendChild(h("div.home-grid", left, center, right, h("div.quick-row.hud", quick, extras)));
      M.voiceLine();
      if (convo.lastElementChild) setTimeout(() => convo.lastElementChild && convo.lastElementChild.scrollIntoView({ block: "end" }), 30);

      let hasProjects = false;
      if (M.homeMode !== "plant") {
        try {
          const home = await M.tool("jarvis_home");
          hasProjects = (home.projects || []).length > 0;
          if (hasProjects && home.overview) { projectCards(left, right, home); return; }
        } catch (e) { /* no project access - the plant cards below say what they can */ }
      }

      try {
        const t = await M.call("telemetry");
        if (!t || t.error) {
          const reason = t && t.error ? t.error : "No access to plant data.";
          left.replaceChildren(left.firstChild, M.callout(reason, "warn"));
          right.replaceChildren(right.firstChild, M.callout(reason, "warn"));
          return;
        }
        telemetryCard(left, t);
        operationsCard(right, t);
        if (M.homeMode === "plant") M.$(".card-head", left).appendChild(toggle("PROJECT", "project"));
      } catch (e) {
        left.replaceChildren(left.firstChild, M.callout(e.message, "warn"));
        right.replaceChildren(right.firstChild, M.callout(e.message, "warn"));
      }
    },
  });
})();
