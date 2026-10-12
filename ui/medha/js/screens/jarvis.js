/* PROJECTS - the ZEUS screens: projects and their health, live progress and
   approvals, the CPM schedule, "why did the finish date move?", the delay
   chronology with notice deadlines, project-learning cards, and project search.
   Every number is the backend's: the CPM engine, the event store, the record. */
(function () {
  "use strict";
  const M = window.ZEUS;
  const { h } = M;
  const J = M.jv;

  /* ------------------------------------------------------------ projects --- */
  async function newProject() {
    const v = await M.form("New project", [{ name: "code", label: "Code (short, unique)", placeholder: "NH44-PKG3" },
      { name: "name", label: "Name" }, { name: "client", label: "Client (optional)" }, { name: "contractor", label: "Contractor (optional)" },
      { name: "location", label: "Location (optional)" }, { name: "start_date", label: "Start date (optional)", type: "date" },
      { name: "workdays", label: "Working days", type: "select", options: ["0123456 (7-day week)", "012345 (Sunday off)", "01234 (5-day week)"] }],
      { submit: "Create" });
    if (!v) return;
    v.workdays = v.workdays.split(" ")[0];
    const r = await M.tool("create_jarvis_project", v);
    await J.select(r.project.project_id);
    M.toast(`Project ${r.project.code} created - import its schedule next`, "ok");
    M.refresh();
  }

  async function importSchedule() {
    const f = await M.call("pick_file", { purpose: "schedule" });
    if (!f.path) return;
    const v = await M.form("Import schedule version", [
      { name: "kind", label: "Version", type: "select", options: ["update", "baseline"] },
      { name: "label", label: "Label", value: M.basename(f.path) },
      { name: "data_date", label: "Data date (optional - read from the file when present)", type: "date" },
      { name: "approve", label: "Approve now (engineer)", type: "select", options: ["no", "yes"] }],
      { submit: "Import", note: `${f.path}\nCSV / XLSX, MS Project XML or Primavera XER.` });
    if (!v) return;
    const r = await M.tool("ingest_schedule_file", { project: J.project, path: f.path, kind: v.kind, label: v.label,
      data_date: v.data_date || "", approve: v.approve === "yes" });
    M.toast(`${r.activities} activities, ${r.dependencies} relationships (${r.provider}) - ${r.status}${r.warnings.length ? `; ${r.warnings.length} warning(s)` : ""}`, "ok");
    M.refresh();
  }

  M.screen("projects", {
    group: "PROJECTS", label: "Projects", icon: "projects", title: "Projects",
    sub: "Every project, its forecast against its baseline, and what is waiting on you. Click a project to work on it.",
    async render(body, params, actions) {
      actions.append(M.button("New project", "small", newProject), M.button("Load DEMO scenario", "small ghost", async () => {
        const r = await M.tool("seed_jarvis_demo", {});
        J.project = r.project_id;
        M.toast(`DEMO project ${r.code} loaded through the real capture pipeline`, "ok");
        M.refresh();
      }));
      await J.load();
      const d = await M.tool("jarvis_projects");
      if (!d.projects.length) { body.appendChild(J.noProject()); }
      else {
        body.appendChild(h("div.jv-grid", d.projects.map((p) => {
          const hl = p.health || {}, c = hl.counts || {};
          const card = h("div.card.jv-project" + (p.project_id === J.project ? ".sel" : ""),
            h("div.row.between", h("span.mono.dim", p.code), J.origin(p.origin_label)),
            h("h3", p.name),
            h("p.note", [p.client, p.contractor, p.location].filter(Boolean).join(" · ") || "—"),
            hl.schedule === "approved" ? h("div.stack", { style: "gap:10px" },
              h("div.jv-kpis", J.kpi("Forecast finish", J.d(hl.forecast_finish)), J.kpi("Baseline", J.d(hl.baseline_finish)),
                J.kpi("Variance", J.days(hl.finish_variance_days), "", hl.finish_variance_days > 0 ? "late" : hl.finish_variance_days < 0 ? "early" : "")),
              J.bar(hl.actual_progress_pct, hl.planned_progress_pct)) : M.callout(hl.note || hl.error || "No approved schedule yet.", "warn"),
            h("div.row", { style: "margin-top:8px" },
              c.pending_approvals ? M.chip(`${c.pending_approvals} to approve`, "warn") : null,
              c.links_to_confirm ? M.chip(`${c.links_to_confirm} links to confirm`, "warn") : null,
              c.open_blockers ? M.chip(`${c.open_blockers} open blocker(s)`, "fail") : null,
              c.open_conflicts ? M.chip(`${c.open_conflicts} sync conflict(s)`, "fail") : null,
              hl.deadlines_missed ? M.chip(`${hl.deadlines_missed} notice deadline(s) missed`, "fail") : null,
              (hl.deadlines_open || []).length ? M.chip(`next notice due ${J.d(hl.deadlines_open[0].due_date)}`, "warn") : null));
          card.addEventListener("click", async () => { await J.select(p.project_id); M.refresh(); });
          return card;
        })));
      }
      if (J.project) {
        const panel = h("div.stack");
        body.appendChild(panel);
        await M.section(panel, "reading schedule versions and clauses", async () => {
          const [versions, refs] = await Promise.all([M.tool("jarvis_versions", { project: J.project }), M.tool("jarvis_contract_refs", { project: J.project })]);
          panel.append(
            h("div.card", h("div.card-head", h("h3", "Schedule versions"), h("div.row", M.button("Import schedule", "small primary", importSchedule))),
              M.table([{ label: "Label", key: "label" }, { label: "Kind", key: "kind" }, { label: "Data date", render: (v) => J.d(v.data_date) },
                { label: "Source", render: (v) => `${v.source_file || "—"} (${v.provider || "—"})` }, { label: "Status", render: (v) => J.status(v.status) },
                { label: "Approved", render: (v) => (v.approved_by ? `${v.approved_by} · ${J.dt(v.approved_at)}` : "—") },
                { label: "", render: (v) => v.status === "pending" ? M.button("Approve", "small", async () => { await M.tool("approve_schedule_version", { version_id: v.version_id, approve: true }); M.toast("Approved - forecast recomputed", "ok"); M.refresh(); }) : "" }],
                versions.versions, { empty: "No schedule imported yet." })),
            h("div.card", h("div.card-head", h("h3", "Contract notice clauses"), M.button("Add clause", "small", async () => {
              const v = await M.form("Contract notice clause", [{ name: "clause", label: "Clause", placeholder: "GCC 19.1" }, { name: "title", label: "Title" },
                { name: "notice_type", label: "Notice type", placeholder: "delay" }, { name: "notice_period_days", label: "Notice period (days)", type: "number", value: "7" },
                { name: "trigger_types", label: "Starts the clock", type: "select", options: ["blocker_reported,delay_reported", "blocker_reported", "delay_reported"] }], { submit: "Save" });
              if (!v) return;
              await M.tool("register_contract_notice", { project: J.project, clause: v.clause, title: v.title, notice_type: v.notice_type,
                notice_period_days: parseInt(v.notice_period_days, 10) || 7, trigger_types: v.trigger_types });
              M.refresh();
            })),
              M.table(["clause", "title", "notice_type", "notice_period_days", { label: "Clock starts on", render: (r) => JSON.parse(r.trigger_types || "[]").join(", ") }], refs.contract_refs, { empty: "No clause configured - notice deadlines are not tracked." })),
            h("div.card", h("div.card-head", h("h3", "Organisation history for learning cards"), M.button("Import past-project register", "small", async () => {
              const f = await M.call("pick_file", { purpose: "document" });
              if (!f.path) return;
              const v = await M.form("Import history", [{ name: "origin", label: "These records are", type: "select", options: ["company", "sample", "demo"] }], { submit: "Import", note: f.path });
              if (!v) return;
              const r = await M.tool("ingest_historical_activities", { path: f.path, origin: v.origin });
              M.toast(`${r.added} completed activities added${r.rejected.length ? `; ${r.rejected.length} rejected (no actual outcome)` : ""}`, "ok");
            })), h("p.note", "Completed activities with evidenced actual dates are added automatically. A register of past projects adds more - rows without actual dates are refused, never estimated.")));
        });
      }
      const routing = h("div.card", h("div.card-head", h("h3", "Local models for ZEUS tasks"), h("span.note", "verified against what is installed and trusted")));
      body.appendChild(routing);
      M.section(routing, "checking the local models", async () => {
        const r = await M.tool("jarvis_model_routing", {});
        routing.appendChild(M.table([{ label: "Task", key: "task" }, { label: "Model / engine", render: (x) => x.model || "—" },
          { label: "State", render: (x) => M.chip(x.usable ? x.state : x.state || "UNAVAILABLE", x.usable ? "ok" : "warn") },
          { label: "Purpose", key: "purpose" }, { label: "If unavailable", render: (x) => x.fallback || x.reason || "" }], r.routes));
      });
    },
  });

  /* ------------------------------------------------------- live progress --- */
  function finishChart(history, baseline) {
    const pts = (history || []).filter((r) => r.project_finish);
    if (pts.length < 2 || !baseline) return h("p.note", "The finish has one recorded value so far.");
    const base = new Date(baseline + "T00:00:00").getTime();
    const vals = pts.map((r) => Math.round((new Date(r.project_finish + "T00:00:00").getTime() - base) / 864e5));
    const W = 900, H = 150, L = 40, B = 26, T = 10, max = Math.max(1, ...vals.map(Math.abs));
    const x = (i) => L + (i / (pts.length - 1)) * (W - L - 12), y = (v) => T + (1 - (v + max) / (2 * max)) * (H - T - B);
    let svg = `<svg class="chart" viewBox="0 0 ${W} ${H}"><line x1="${L}" x2="${W - 12}" y1="${y(0)}" y2="${y(0)}" stroke="var(--ink-3)" stroke-dasharray="4 4"/>`;
    svg += `<text x="${L - 6}" y="${y(0) + 3}" text-anchor="end" fill="var(--ink-3)" font-size="10" font-family="JetBrains Mono, Cascadia Mono, Consolas">BL</text>`;
    svg += `<polyline points="${vals.map((v, i) => `${x(i)},${y(v)}`).join(" ")}" fill="none" stroke="var(--flare)" stroke-width="2.2"/>`;
    vals.forEach((v, i) => { svg += `<circle cx="${x(i)}" cy="${y(v)}" r="4" fill="var(--crude)" stroke="var(--flare)" stroke-width="2"><title>#${pts[i].seq} ${M.esc(pts[i].reason || "")}: ${pts[i].project_finish} (${v >= 0 ? "+" : ""}${v} d)</title></circle>`;
      svg += `<text x="${x(i)}" y="${H - 8}" text-anchor="middle" fill="var(--ink-3)" font-size="10" font-family="JetBrains Mono, Cascadia Mono, Consolas">#${pts[i].seq}</text>`; });
    return h("div.stack", M.html(svg + "</svg>"), h("p.note", "Forecast finish against the baseline (days) at each snapshot - hover a point for the update that set it."));
  }

  async function decide(eventId, approve) {
    const reason = approve ? "" : ((await M.form("Reject this update", [{ name: "reason", label: "Why" }], { submit: "Reject" })) || {}).reason;
    if (!approve && reason === undefined) return;
    const r = await M.tool("approve_field_update", { event_id: eventId, approve, reason: reason || "" });
    if (r.explanation && r.explanation.found) {
      M.openSheet("The forecast moved", h("div.stack", M.answer(r.explanation.answer), M.button("Open the causal chain", "primary", () => M.go("why", { change: r.explanation.change_id }))));
    } else M.toast(approve ? "Approved - the forecast was recomputed; the finish did not move" : "Rejected - kept in the history", "ok");
    M.refresh();
  }

  M.screen("progress", {
    group: "PROJECTS", label: "Live Progress", icon: "progress", title: "Live progress",
    sub: "Actual progress from approved field events against the plan - and the updates waiting for an engineer's approval.",
    async render(body) {
      if (!(await J.ensure(body))) return;
      body.appendChild(J.picker(() => M.refresh()));
      const [ov, pending] = await Promise.all([M.tool("jarvis_overview", { project: J.project }), M.tool("jarvis_pending", { project: J.project })]);
      const p = ov.project;
      if (p.origin_label !== "RECORDED") body.appendChild(M.callout(`${p.origin_label} DATA - ${p.name} is not a real project record.`, "sample"));
      if (ov.schedule !== "approved") { body.appendChild(M.callout(ov.note || "No approved schedule.", "warn")); }
      else {
        body.appendChild(h("div.jv-kpis", J.kpi("Forecast finish", J.d(ov.forecast_finish), `data date ${J.d(ov.data_date)}`),
          J.kpi("Baseline finish", J.d(ov.baseline_finish), ov.version), J.kpi("Variance", J.days(ov.finish_variance_days), "forecast - baseline", ov.finish_variance_days > 0 ? "late" : ""),
          J.kpi("Critical", ov.critical, `${ov.near_critical} near-critical`), J.kpi("Open blockers", ov.counts.open_blockers), J.kpi("Evidence items", ov.counts.evidence)));
        body.appendChild(h("div.card", h("div.card-head", h("h3", "Progress"), h("span.note", ov.progress_basis)), J.bar(ov.actual_progress_pct, ov.planned_progress_pct),
          h("p.note", `Critical path: ${(ov.critical_path || []).join(" → ") || "—"}`)));
        body.appendChild(h("div.card", h("div.card-head", h("h3", "Forecast finish over time"), M.button("Why did it move?", "small", () => M.go("why"))), finishChart(ov.finish_history, ov.baseline_finish)));
      }
      body.appendChild(h("div.card", h("div.card-head", h("h3", `Updates awaiting approval (${pending.updates.length})`), h("span.note", "approval needs an engineer; the forecast uses approved updates only")),
        M.table([{ label: "Happened", render: (e) => J.d(e.occurred_at) }, { label: "Activity", render: (e) => e.activity_code || "—" },
          { label: "Update", render: (e) => h("span", h("b", J.label(e.event_type)), " ", (e.payload.note || e.payload.description || "").slice(0, 140)) },
          { label: "Captured", render: (e) => `${J.dt(e.captured_at)} · ${e.actor || "—"} · ${e.device_id}` }, { label: "Link", render: (e) => M.num(e.confidence, 2) },
          { label: "", render: (e) => e.status === "conflicted" ? M.button("Sync conflict", "small danger", () => M.go("sync"))
            : h("div.row", { style: "flex-wrap:nowrap" }, M.button("Evidence", "small ghost", () => J.trailSheet({ event_id: e.event_id })),
              M.button("Approve", "small primary", () => decide(e.event_id, true)), M.button("Reject", "small", () => decide(e.event_id, false))) }],
          pending.updates, { empty: "Nothing is waiting for approval." })));
      body.appendChild(h("div.card", h("div.card-head", h("h3", `Field records to link (${pending.links_to_confirm.length})`), h("span.note", "confidence was too low to link automatically - confirm or correct")),
        pending.links_to_confirm.length ? h("div.stack", pending.links_to_confirm.map((o) => h("div.card.raised",
          h("div.row.between", h("span", h("b", J.label(o.kind)), ` · ${J.d(o.observed_at)}`), J.status(o.link_status)),
          h("p", o.normalized_text || o.original_text), o.original_text && o.original_text !== o.normalized_text ? h("p.note", `Original: ${o.original_text}`) : null,
          h("div.row", ((o.link_reasons || {}).candidates || []).map((c) => M.button(`${c.code} · ${M.num(c.score, 2)}`, "small", async () => {
            await M.tool("resolve_activity_link", { observation_id: o.observation_id, activity_id: c.activity_id });
            M.toast(`Linked to ${c.code} and remembered for next time`, "ok");
            M.refresh();
          })), M.button("Evidence", "small ghost", () => J.trailSheet({ evidence_id: o.evidence_id })))))) : M.empty("Every field record is linked.")));
    },
  });

  /* ------------------------------------------------------------ schedule --- */
  function gantt(rows) {
    const withDates = rows.filter((r) => r.es && r.ef);
    if (!withDates.length) return M.empty("No dated activities.");
    const all = withDates.flatMap((r) => [r.es, r.ef, r.baseline_start, r.baseline_finish].filter(Boolean)).sort();
    const t0 = new Date(all[0] + "T00:00:00").getTime(), t1 = new Date(all[all.length - 1] + "T00:00:00").getTime() + 864e5;
    const W = 1100, L = 190, rowH = 26, H = withDates.length * rowH + 30;
    const x = (iso) => L + ((new Date(iso + "T00:00:00").getTime() - t0) / (t1 - t0)) * (W - L - 10);
    let svg = `<svg viewBox="0 0 ${W} ${H}">`;
    for (let t = t0; t <= t1; t += 7 * 864e5) {
      const xx = L + ((t - t0) / (t1 - t0)) * (W - L - 10);
      svg += `<line x1="${xx}" x2="${xx}" y1="0" y2="${H - 20}" stroke="var(--steel-2)"/><text x="${xx + 2}" y="${H - 6}" fill="var(--ink-3)" font-size="10" font-family="JetBrains Mono, Cascadia Mono, Consolas">${M.esc(new Date(t).toISOString().slice(5, 10))}</text>`;
    }
    withDates.forEach((r, i) => {
      const y = i * rowH + 6;
      svg += `<text x="4" y="${y + 13}" fill="var(--ink-2)" font-size="11" font-family="JetBrains Mono, Cascadia Mono, Consolas">${M.esc(r.code).slice(0, 22)}</text>`;
      if (r.baseline_start && r.baseline_finish) svg += `<rect x="${x(r.baseline_start)}" y="${y + 13}" width="${Math.max(3, x(r.baseline_finish) - x(r.baseline_start) + (W - L) / ((t1 - t0) / 864e5))}" height="4" rx="2" fill="var(--ink-3)" opacity="0.6"><title>baseline ${r.baseline_start} → ${r.baseline_finish}</title></rect>`;
      const color = r.status === "complete" ? "var(--ok)" : r.critical ? "var(--fail)" : r.near_critical ? "var(--warn)" : "var(--process)";
      if (String(r.type).includes("milestone")) svg += `<polygon points="${x(r.ef)},${y} ${x(r.ef) + 7},${y + 7} ${x(r.ef)},${y + 14} ${x(r.ef) - 7},${y + 7}" fill="${color}"><title>${M.esc(r.code)} ${r.ef}</title></polygon>`;
      else svg += `<rect x="${x(r.es)}" y="${y}" width="${Math.max(4, x(r.ef) - x(r.es) + (W - L) / ((t1 - t0) / 864e5))}" height="11" rx="3" fill="${color}" opacity="0.85"><title>${M.esc(r.code)} ${r.es} → ${r.ef} · float ${r.total_float == null ? "—" : r.total_float}</title></rect>`;
    });
    return h("div.jv-gantt", M.html(svg + "</svg>"));
  }

  M.screen("schedule", {
    group: "PROJECTS", label: "Schedule", icon: "schedule", title: "Schedule",
    sub: "The approved schedule with approved field events applied, run through the CPM engine - dates, float, critical path and what drives each activity.",
    async render(body, params) {
      if (!(await J.ensure(body))) return;
      body.appendChild(J.picker(() => M.refresh()));
      const tabs = h("div.tabs"), pane = h("div.stack");
      const views = {
        Activities: async (asOf) => {
          const d = await M.tool("jarvis_schedule", { project: J.project, as_of: asOf || "" });
          pane.append(h("p.note", `${d.version} · data date ${J.d(d.data_date)} · forecast finish ${J.d(d.project_finish)} (baseline ${J.d(d.baseline_finish)})${d.as_of ? ` · AS KNOWN AT ${d.as_of}` : ""}`),
            M.table([{ label: "Code", key: "code" }, { label: "Activity", key: "name" }, { label: "Status", render: (r) => J.label(r.status) },
              { label: "%", render: (r) => (r.percent_complete == null ? "—" : M.num(r.percent_complete, 0)) }, { label: "Start", render: (r) => J.d(r.es) }, { label: "Finish", render: (r) => J.d(r.ef) },
              { label: "Baseline", render: (r) => J.d(r.baseline_finish) }, { label: "Var.", render: (r) => J.days(r.finish_variance_days) },
              { label: "Float", render: (r) => (r.total_float == null ? "—" : r.critical ? M.chip(String(r.total_float), "fail") : r.near_critical ? M.chip(String(r.total_float), "warn") : String(r.total_float)) },
              { label: "Driven by", render: (r) => r.driver ? `${r.driver.kind}${r.driver.pred_code ? " " + r.driver.pred_code : ""}` : "—" },
              { label: "Evidence", key: "evidence_count" }], d.activities, { onRow: (r) => J.activitySheet(r.id) }));
        },
        Gantt: async () => {
          const d = await M.tool("jarvis_schedule", { project: J.project });
          pane.append(h("p.note", "Bars: forecast (red critical, amber near-critical, green complete); thin grey line: baseline."), gantt(d.activities));
        },
        Logic: async () => {
          const d = await M.tool("jarvis_schedule", { project: J.project });
          pane.append(M.table([{ label: "Predecessor", key: "pred_code" }, { label: "Type", key: "type" }, { label: "Lag (d)", key: "lag" }, { label: "Successor", key: "succ_code" }], d.dependencies));
        },
        Versions: async () => {
          const v = (await M.tool("jarvis_versions", { project: J.project })).versions;
          const approved = v.filter((x) => x.status === "approved");
          pane.appendChild(M.table(["label", "kind", "data_date", "status", "approved_by", "source_file"], v));
          if (approved.length < 2) { pane.appendChild(h("p.note", "Two approved versions are needed to compare.")); return; }
          const a = h("select.field", approved.map((x, i) => h("option", { value: x.version_id, selected: i === approved.length - 2 }, x.label)));
          const b = h("select.field", approved.map((x, i) => h("option", { value: x.version_id, selected: i === approved.length - 1 }, x.label)));
          const out = h("div.stack");
          pane.append(h("div.filters.jv-filters", a, h("span", "→"), b, M.button("Compare", "small primary", async () => {
            const d = await M.tool("jarvis_compare_versions", { version_a: a.value, version_b: b.value });
            out.replaceChildren(h("p", `Forecast finish ${J.d(d.from.forecast_finish)} → ${J.d(d.to.forecast_finish)} (${J.days(d.finish_shift_days)})`),
              M.table([{ label: "Activity", key: "code" }, { label: "Change", key: "change" }, { label: "Finish shift", render: (c) => J.days(c.finish_shift_days) },
                { label: "Fields", render: (c) => Object.entries(c.fields || {}).map(([k, x]) => `${J.label(k)}: ${x.from || "—"} → ${x.to || "—"}`).join("; ") }], d.activity_changes, { empty: "No activity changed." }),
              d.logic_changes.length ? M.table(["pred", "succ", "change"], d.logic_changes) : h("p.note", "No logic change."));
          })), out);
        },
        "As of": async () => {
          const input = h("input.field", { type: "date", value: params.as_of ? String(params.as_of).slice(0, 10) : "" });
          const out = h("div.stack");
          pane.append(h("div.filters.jv-filters", h("span.eyebrow", "WHAT WAS KNOWN AT THE END OF"), input, M.button("Rebuild", "small primary", async () => {
            if (!input.value) return;
            out.replaceChildren();
            const before = pane.childNodes.length;
            await views.Activities.call(null, input.value);
            while (pane.childNodes.length > before) out.appendChild(pane.childNodes[before]);
          })), out, h("p.note", "Only events and approvals captured by the end of that day are used - later records are excluded."));
        },
      };
      const show = async (name) => {
        tabs.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.textContent === name));
        pane.replaceChildren();
        await M.section(pane, "running the schedule", () => views[name]());
      };
      Object.keys(views).forEach((name) => tabs.appendChild(h("button.tab", { onclick: () => show(name) }, name)));
      body.append(tabs, pane);
      show(params.tab && views[params.tab] ? params.tab : "Activities");
    },
  });

  /* ---------------------------------------------- why did the date move? --- */
  const STEP_CLASS = { "OLD FORECAST": "s-old", "NEW FORECAST": "s-new", "CHANGED ACTIVITY": "s-root", "TRIGGERING EVENT": "s-trigger", EVIDENCE: "s-evidence" };
  function openRef(ref, change) {
    if (!ref) return;
    const ids = Array.isArray(ref.id) ? ref.id : [ref.id];
    if (ref.kind === "activity" || ref.kind === "activities") J.activitySheet(ids[0]);
    else if (ref.kind === "event") J.trailSheet({ event_id: ids[0] });
    else if (ref.kind === "evidence") { if (ids[0]) J.trailSheet({ evidence_id: ids[0] }); }
    else if (ref.kind === "snapshot") M.go("schedule", { tab: "As of", as_of: change.captured_at });
  }

  M.screen("why", {
    group: "PROJECTS", label: "Why Did the Date Move?", icon: "why", title: "Why did the finish date move?",
    sub: "The causal chain from an approved field update, through the schedule logic, to the finish - every step opens its record.",
    async render(body, params) {
      if (!(await J.ensure(body))) return;
      body.appendChild(J.picker(() => M.refresh()));
      const changes = (await M.tool("jarvis_forecast_changes", { project: J.project })).changes;
      if (!changes.length) { body.appendChild(M.empty("The forecast has not changed since the schedule was approved.")); return; }
      const wanted = params.change || (changes.find((c) => c.delta_days) || changes[0]).change_id;
      const select = h("select.field.grow", changes.map((c) => h("option", { value: c.change_id, selected: c.change_id === wanted },
        `${J.dt(c.captured_at)} · finish ${J.d(c.previous_finish)} → ${J.d(c.new_finish)} (${J.days(c.delta_days)}) · ${c.root_code || "—"}`)));
      const out = h("div.stack");
      const load = async (useModel) => {
        out.replaceChildren(M.loading(useModel ? "asking the local reasoning model to phrase the facts" : "tracing the chain"));
        const w = await M.tool("jarvis_why_moved", { project: J.project, change_id: select.value, use_model: !!useModel });
        const flow = h("div.jv-flow");
        w.steps.forEach((s, i) => {
          if (i) flow.appendChild(h("div.jv-link"));
          flow.appendChild(h("div.jv-step." + (STEP_CLASS[s.step] || "s-dep"), { onclick: () => openRef(s.ref, w), title: "Open the record" },
            h("div.k", s.step), h("div.t", s.title), h("div.d", s.detail)));
        });
        out.replaceChildren(
          h("div.card", h("div.card-head", h("h3", "The answer"), h("span.note", `phrased by: ${w.narrative_source}`)), M.answer(w.answer),
            h("div.row", M.button("Phrase with the local reasoning model", "small", () => load(true)), M.button("Evidence trail of the trigger", "small ghost", () => J.trailSheet({ event_id: w.trigger.event_id })))),
          h("div.split", h("div.card", h("h3", "Causal chain"), flow),
            h("div.card", h("h3", "Evidence"), w.evidence.length ? h("div.stack", w.evidence.map((e) => h("div.card.raised.evidence-card", { style: "cursor:pointer", onclick: () => J.trailSheet({ evidence_id: e.evidence_id }) },
              h("div.row.between", h("b", e.title || e.filename || e.kind), J.origin(e.origin)), h("p.note", `${e.kind} · captured ${J.dt(e.captured_at)} by ${e.captured_by || "—"} on ${e.device_id} · ${e.source_type}`),
              h("p", (e.excerpt || "").slice(0, 280))))) : M.empty("No evidence was attached to the trigger."),
              J.sections(w.classification))),
          h("div.card", h("h3", "Everything that moved"), M.table([{ label: "Activity", render: (a) => `${a.code} ${a.name}` }, { label: "Finish", render: (a) => `${J.d(a.previous_finish)} → ${J.d(a.new_finish)}` },
            { label: "Moved", render: (a) => J.days(a.delta_days) }, { label: "Float", render: (a) => `${a.float_before == null ? "—" : a.float_before} → ${a.float_after == null ? "—" : a.float_after}` }], w.affected, { onRow: (a) => J.activitySheet(a.activity_id) })));
      };
      select.addEventListener("change", () => load(false));
      body.append(h("div.filters.jv-filters", h("span.eyebrow", "FORECAST CHANGE"), select), out);
      load(false);
    },
  });

  /* ---------------------------------------------------------- chronology --- */
  const KIND_SETS = { "All events": "", "Blockers & delays": "blocker_reported,delay_reported,blocker_cleared",
    "Letters & notices": "correspondence_received,notice_issued", "Progress": "progress_update,activity_started,activity_completed,remaining_update",
    "Schedule & forecast": "schedule_imported,schedule_changed,forecast_changed", "Sync conflicts": "conflict_detected,conflict_resolved" };

  M.screen("chronology", {
    group: "PROJECTS", label: "Delay Chronology", icon: "chronology", title: "Delay notice & chronology",
    sub: "A dated timeline built from site updates, blockers, letters, schedule snapshots and forecast changes - each entry with its evidence - and the contract notice deadlines it starts.",
    async render(body, params, actions) {
      if (!(await J.ensure(body))) return;
      body.appendChild(J.picker(() => M.refresh()));
      const exportBtn = (fmt) => M.button(fmt.toUpperCase(), "small", async () => {
        const r = await M.tool("export_delay_chronology", { project: J.project, fmt, activity: activity.value, date_from: from.value, date_to: to.value });
        M.toast(`Chronology written and verified: ${M.basename(r.saved)}`, "ok");
      });
      actions.append(exportBtn("docx"), exportBtn("xlsx"), exportBtn("pdf"), M.button("Evidence pack", "small ghost", async () => {
        const r = await M.tool("export_evidence_pack", { project: J.project, activity: activity.value });
        M.toast(`Evidence pack: ${r.evidence} files, SHA-256 checked - ${M.basename(r.saved)}`, "ok");
      }));
      const sched = await M.tool("jarvis_schedule", { project: J.project });
      const from = h("input.field", { type: "date" }), to = h("input.field", { type: "date" });
      const activity = h("select.field", h("option", { value: "" }, "all activities"), sched.activities.map((a) => h("option", { value: a.code, selected: a.code === params.activity }, `${a.code} ${a.name}`)));
      const kinds = h("select.field", Object.keys(KIND_SETS).map((k) => h("option", { value: KIND_SETS[k] }, k)));
      const delay = h("select.field", h("option", { value: "" }, "any cause"), ["material", "weather", "labour", "equipment", "design", "access", "permit", "power", "utility", "other"].map((k) => h("option", { value: k }, k)));
      const source = h("select.field", h("option", { value: "" }, "any source"), ["field", "contractor", "office", "client", "import", "review", "cpm", "sync"].map((k) => h("option", { value: k }, k)));
      const contractor = h("input.field", { placeholder: "contractor" });
      const evidenceOnly = h("input", { type: "checkbox" });
      const list = h("div.stack");
      const run = async () => {
        list.replaceChildren(M.loading("building the chronology"));
        const d = await M.tool("jarvis_chronology", { project: J.project, date_from: from.value, date_to: to.value, activity: activity.value,
          kinds: kinds.value, delay_type: delay.value, source_type: source.value, contractor: contractor.value, evidence_only: evidenceOnly.checked });
        list.replaceChildren(h("p.note", `${d.entries.length} dated entries`), d.entries.length ? h("div.jv-tl", d.entries.map((r) => h("div.row-item", { onclick: () => J.trailSheet({ event_id: r.source_id }) },
          h("div.date", J.d(r.occurred_at)), h("div.dot." + (J.dotClass(r.kind) || "x")),
          h("div", h("div.title", r.title), r.detail ? h("div.detail", r.detail) : null,
            h("div.row", { style: "margin-top:4px" }, r.status ? J.status(r.status) : null, r.delay_type ? M.chip(r.delay_type, "fail") : null,
              r.evidence_ids.length ? M.chip(`${r.evidence_ids.length} evidence`, "ok") : null, r.source_type ? M.chip(r.source_type, "plain") : null,
              ...(r.related || []).map((x) => M.chip(`${x.relation}: ${x.title || x.event_id.slice(0, 8)}`, "info"))))))) : M.empty("Nothing matches these filters."));
      };
      body.append(h("div.filters.jv-filters", from, h("span", "→"), to, activity, kinds, delay, source, contractor, h("label.row", evidenceOnly, h("span.note", "with evidence only")), M.button("Apply", "small primary", run)), list);
      const side = h("div.grid.two");
      body.appendChild(side);
      M.section(side, "checking notice deadlines", async () => {
        const [dl, win] = await Promise.all([M.tool("jarvis_notice_deadlines", { project: J.project }), M.tool("jarvis_delay_windows", { project: J.project })]);
        side.append(h("div.card", h("h3", "Contract notice deadlines"), dl.deadlines.length ? h("div.stack", dl.deadlines.map((x) => h("div.card.raised",
          h("div.row.between", h("b", `${x.clause} · ${x.notice_type}`), J.status(x.status)),
          h("p.note", `${x.activity_code || "project"} · triggered ${J.d(x.trigger_date)} · due ${J.d(x.due_date)}${x.status === "open" ? ` · ${x.days_left} day(s) left` : ""}${x.notice_reference ? ` · notice ${x.notice_reference} of ${J.d(x.notice_issued_on)}` : ""}`),
          x.reminder ? M.callout(`Reminder: the ${x.notice_type} notice under ${x.clause} is due in ${x.days_left} day(s).`, "warn") : null))) : M.empty(dl.contract_refs.length ? "No clause has been triggered." : "No contract clause is configured (Projects screen).")),
          h("div.card", h("h3", "Delay windows"), h("p.note", win.note), M.table([{ label: "Window", key: "window" }, { label: "Finish", render: (w) => `${J.d(w.finish_before)} → ${J.d(w.finish_after)}` },
            { label: "Moved", render: (w) => J.days(w.movement_days) }, { label: "Driving", render: (w) => w.driving_activity || "—" }, { label: "Field events", key: "field_events_in_window" }], win.windows, { empty: "One snapshot so far." })));
      });
      run();
    },
  });

  /* ------------------------------------------------------ learning cards --- */
  M.screen("learning", {
    group: "PROJECTS", label: "Learning Cards", icon: "learning", title: "Project-learning cards",
    sub: "Comparable completed activities from this organisation's own evidence-linked history, as planning references. Nothing is estimated where no record exists.",
    async render(body, params, actions) {
      if (!(await J.ensure(body))) return;
      body.appendChild(J.picker(() => M.refresh()));
      const sched = await M.tool("jarvis_schedule", { project: J.project });
      const tasks = sched.activities.filter((a) => a.type === "task");
      const pick = params.activity || (tasks.find((a) => a.critical && a.status !== "complete") || tasks[0] || {}).id;
      const select = h("select.field", tasks.map((a) => h("option", { value: a.id, selected: a.id === pick }, `${a.code} · ${a.name}`)));
      const out = h("div.stack");
      const load = async () => {
        out.replaceChildren(M.loading("finding comparable completed activities"));
        const c = await M.tool("jarvis_learning_cards", { activity: select.value });
        const t = c.target;
        const head = h("div.card", h("div.card-head", h("h3", `${t.code} · ${t.name}`), M.button("Learning report (.docx)", "small", async () => {
          const r = await M.tool("export_learning_report", { activity: select.value });
          M.toast(`Written and verified: ${M.basename(r.saved)}`, "ok");
        })), h("div.jv-kpis", J.kpi("Planned duration", t.planned_duration == null ? "—" : `${t.planned_duration} d`), J.kpi("Quantity", t.quantity == null ? "—" : `${M.num(t.quantity)} ${t.unit || ""}`),
          J.kpi("Forecast", `${J.d(t.forecast_start)} → ${J.d(t.forecast_finish)}`), c.stats ? J.kpi("Comparable median", `${M.num(c.stats.median_duration, 1)} d`, `n = ${c.stats.n} · ${c.stats.min_duration}-${c.stats.max_duration} d`) : null));
        if (!c.comparables.length) { out.replaceChildren(head, M.callout(c.message, "info")); return; }
        out.replaceChildren(head, h("div.jv-grid", c.comparables.map((x) => h("div.card",
          h("div.row.between", h("span.mono.dim", x.code), J.origin(x.origin_label)), h("h3", x.name), h("p.note", `${x.project_name} · ${x.source}${x.contractor ? " · " + x.contractor : ""}`),
          h("div.jv-kpis", J.kpi("Actual", `${x.actual_duration_days} d`, x.planned_duration_days ? `planned ${x.planned_duration_days} d` : ""),
            J.kpi("Productivity", x.productivity_per_day ? `${M.num(x.productivity_per_day, 1)}` : "—", x.productivity_per_day ? `${x.unit || ""}/day` : "no quantity")),
          h("p.note", `${J.d(x.actual_start)} → ${J.d(x.actual_finish)} · ${(x.conditions || {}).season || ""}${x.blockers.length ? " · blockers: " + x.blockers.map((b) => (typeof b === "string" ? b : b.category || b.description)).join(", ") : ""}`),
          h("div.row", M.chip(`similarity ${M.num(x.similarity, 2)}`, "info"), ...(x.match_reasons || []).map((r) => M.chip(r, "plain")))))),
          h("div.card", J.sections(c)));
      };
      select.addEventListener("change", load);
      body.append(h("div.filters.jv-filters", h("span.eyebrow", "ACTIVITY"), select), out);
      load();
    },
  });

  /* ------------------------------------------------ project search & ask --- */
  const EXAMPLES = ["Why did the finish date move?", "What changed between the 1 September and 10 September schedule updates?",
    "What did we know about P17 concreting on 7 September?", "When did we first know that P17 concreting was slipping?",
    "What evidence existed before the delay was reported?", "Show the notice deadlines"];

  M.screen("psearch", {
    group: "PROJECTS", label: "Project Search & Ask", icon: "psearch", title: "Project search & questions",
    sub: "Questions answered from the record - including what was known at a past date - and hybrid search across evidence, letters, activities and forecast changes.",
    async render(body) {
      if (!(await J.ensure(body))) return;
      body.appendChild(J.picker(() => M.refresh()));
      const q = h("input.field.grow", { placeholder: "Ask about the project…" });
      const answer = h("div.stack");
      const ask = async (text) => {
        q.value = text;
        answer.replaceChildren(M.loading("answering from the project record"));
        const r = await M.tool("jarvis_ask", { question: text, project: J.project });
        const extra = [];
        const d = r.data || {};
        if (r.intent === "compare_versions" && d.activity_changes) extra.push(M.table(["code", "change", "finish_shift_days"], d.activity_changes));
        if (r.intent === "why_moved" && d.steps) extra.push(M.button("Open the causal chain", "small primary", () => M.go("why", { change: d.change_id })));
        if (r.intent === "search" && d.results) extra.push(M.table(["kind", "title", "excerpt"], d.results));
        answer.replaceChildren(h("div.card", h("div.row", M.chip(J.label(r.intent), "info"), r.activity ? M.chip(`${r.activity.code} (${M.num(r.activity.score, 2)})`, "plain") : null),
          M.answer(r.answer || ""), J.sections(r), ...extra));
      };
      q.addEventListener("keydown", (e) => { if (e.key === "Enter" && q.value.trim()) ask(q.value.trim()); });
      body.append(h("div.card", h("div.filters.jv-filters", q, M.button("Ask", "small primary", () => q.value.trim() && ask(q.value.trim()))),
        h("div.row", EXAMPLES.map((x) => M.button(x, "small ghost", () => ask(x))))), answer);

      const sq = h("input.field.grow", { placeholder: "Search evidence, letters, activities, forecast changes…" });
      const kinds = h("select.field", [["", "everything"], ["evidence", "evidence"], ["correspondence", "letters"], ["activity", "activities"], ["forecast_change", "forecast changes"]].map(([v, l]) => h("option", { value: v }, l)));
      const df = h("input.field", { type: "date", title: "happened from" }), dtl = h("input.field", { type: "date", title: "happened to" });
      const kb = h("input.field", { type: "date", title: "known before (captured before this day)" });
      const results = h("div.stack");
      const search = async () => {
        results.replaceChildren(M.loading("searching"));
        const r = await M.tool("jarvis_search", { query: sq.value, project: J.project, kinds: kinds.value, date_from: df.value, date_to: dtl.value,
          known_before: kb.value ? `${kb.value}T00:00:00` : "" });
        results.replaceChildren(h("p.note", `${r.count} result(s) · ${r.backend}`), M.table([{ label: "Kind", key: "kind" }, { label: "Title", key: "title" }, { label: "Excerpt", key: "excerpt" },
          { label: "Happened", render: (x) => J.d(x.occurred_at) }, { label: "Captured", render: (x) => J.dt(x.captured_at) }, { label: "Source", key: "source" },
          { label: "Why", render: (x) => (x.signals || []).join(", ") }], r.results, {
          onRow: (x) => (x.kind === "evidence" ? J.trailSheet({ evidence_id: x.ref_id }) : x.kind === "activity" ? J.activitySheet(x.ref_id) : x.kind === "forecast_change" ? M.go("why", { change: x.ref_id }) : null) }));
      };
      sq.addEventListener("keydown", (e) => { if (e.key === "Enter") search(); });
      body.append(h("div.card", h("h3", "Search"), h("div.filters.jv-filters", sq, kinds, h("span.note", "happened"), df, h("span", "→"), dtl, h("span.note", "known before"), kb, M.button("Search", "small primary", search))), results);
    },
  });
})();
