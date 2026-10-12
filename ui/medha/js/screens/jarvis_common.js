/* ZEUS - shared pieces for the project screens: the current project, the
   activity sheet, the evidence-trail sheet, progress bars and the FACT /
   INFERENCE / PREDICTION / RECOMMENDATION sections. Every value drawn here
   comes from a registered jarvis_* tool (through the policy engine). */
(function () {
  "use strict";
  const M = window.ZEUS;
  const { h } = M;
  const J = (M.jv = { project: null, projects: [], home: null });
  /* replaceChildren() would print "null" for an absent optional section - skip them */
  const put = (node, ...kids) => node.replaceChildren(...kids.flat(3).filter((k) => k != null && k !== false));
  J.put = put;

  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  J.d = (iso) => {
    const m = String(iso || "").match(/^(\d{4})-(\d{2})-(\d{2})/);
    return m ? `${m[3]} ${MONTHS[+m[2] - 1]} ${m[1]}` : "—";
  };
  J.dt = (iso) => (iso ? `${J.d(iso)} ${String(iso).slice(11, 16)}`.trim() : "—");
  J.days = (n) => (n == null ? "—" : `${n > 0 ? "+" : ""}${n} d`);
  J.label = (type) => String(type || "").replace(/_/g, " ");
  J.origin = (label) => {
    const l = String(label || "").toUpperCase();
    return M.chip(l || "—", l === "RECORDED" || l === "COMPANY" ? "info" : "sample");
  };
  J.status = (s) => M.chip(String(s || "—").toUpperCase(),
    { approved: "ok", submitted: "warn", conflicted: "fail", rejected: "fail", superseded: "plain", recorded: "info",
      open: "warn", met: "ok", late: "fail", missed: "fail", linked: "ok", needs_confirmation: "warn", unlinked: "plain" }[s] || "plain");
  J.dotClass = (kind) => ({ blocker_reported: "blocker", delay_reported: "delay", forecast_changed: "forecast",
    notice_issued: "notice", correspondence_received: "letter", activity_completed: "done" }[kind] || "");

  /* The projects, and which one this screen is about. The choice is kept in
     this page only; an operator's choice is also saved for spoken updates. */
  J.load = async function () {
    const d = await M.tool("jarvis_home", J.project ? { project: J.project } : {});
    J.home = d;
    J.projects = d.projects || [];
    if (!J.projects.find((p) => p.project_id === J.project)) J.project = d.overview ? d.overview.project.project_id : null;
    return d;
  };
  J.select = async function (projectId) {
    J.project = projectId;
    try { await M.tool("set_current_project", { project: projectId }); } catch (e) { /* a viewer may not save it - the page still switches */ }
  };
  J.picker = function (onChange) {
    if (!J.projects.length) return null;
    const select = h("select.field", J.projects.map((p) => h("option", { value: p.project_id, selected: p.project_id === J.project },
      `${p.code} · ${p.name}${p.origin !== "company" ? ` [${p.origin.toUpperCase()}]` : ""}`)));
    select.addEventListener("change", async () => { await J.select(select.value); onChange(select.value); });
    return h("div.jv-pick", h("span.eyebrow", "PROJECT"), select);
  };
  /* Nothing to show yet: say so, and offer the real ways to start. */
  J.noProject = function () {
    return h("div.stack",
      M.callout("No ZEUS project exists on this workstation yet. Create one and import its schedule, or load the clearly-labelled DEMO scenario.", "info"),
      h("div.row", M.button("Projects", "primary", () => M.go("projects")),
        M.button("Load the DEMO scenario", "", async () => {
          const r = await M.tool("seed_jarvis_demo", {});
          M.toast(`DEMO project ${r.code} loaded through the capture pipeline`, "ok");
          J.project = r.project_id;
          M.refresh();
        })));
  };
  J.ensure = async function (body) {
    await J.load();
    if (!J.project) { body.appendChild(J.noProject()); return false; }
    return true;
  };

  J.bar = function (actual, planned) {
    const a = Math.max(0, Math.min(100, Number(actual) || 0)), p = Math.max(0, Math.min(100, Number(planned) || 0));
    return h("div", h("div.jv-bar", h("div.fill", { style: `width:${a}%` }), h("div.plan", { style: `left:${p}%`, title: `planned ${p}%` })),
      h("div.jv-bar-legend", h("span", `ACTUAL ${M.num(actual, 1)}%`), h("span", `PLANNED ${M.num(planned, 1)}%`)));
  };
  J.kpi = (k, v, s, cls) => h("div.jv-kpi", h("div.k", k), h("div.v" + (cls ? "." + cls : ""), v == null ? "—" : String(v)), s ? h("div.s", s) : null);

  J.sections = function (obj) {
    const out = h("div.jv-sections");
    ["FACT", "INFERENCE", "PREDICTION", "RECOMMENDATION"].forEach((k) => {
      const items = obj[k] || [];
      if (!items.length) return;
      out.append(h("h4", k), h("ul", items.map((i) => h("li", i))));
    });
    return out;
  };

  /* ---------------------------------------------------- the activity sheet */
  J.activitySheet = async function (activity) {
    const body = h("div.stack", M.loading("reading the activity record"));
    M.openSheet("Activity", body);
    try {
      const d = await M.tool("jarvis_activity", { activity });
      const a = d.activity, f = d.forecast || {};
      M.$("#sheet-title").textContent = `${a.code} · ${a.name}`;
      put(body,
        h("div.row", J.status(f.status), f.critical ? M.chip("CRITICAL", "fail") : f.near_critical ? M.chip("NEAR-CRITICAL", "warn") : null,
          f.percent_complete != null ? M.chip(`${M.num(f.percent_complete, 0)}% complete`, "info") : null),
        M.kv({ "forecast start": J.d(f.es), "forecast finish": J.d(f.ef), "baseline finish": J.d(f.baseline_finish),
          "finish variance": J.days(f.finish_variance_days), "total float": f.total_float, "driver": f.driver ? `${f.driver.kind}${f.driver.pred_code ? " " + f.driver.pred_code : ""}${f.driver.expected_clear ? " until " + J.d(f.driver.expected_clear) : ""}` : "—",
          "location": a.location, "contractor": a.contractor, "quantity": a.planned_qty != null ? `${a.planned_qty} ${a.unit || ""}` : null }),
        h("div.row", M.button("Learning card", "small", () => M.go("learning", { activity: a.activity_id })),
          M.button("Chronology", "small ghost", () => M.go("chronology", { activity: a.code }))),
        h("div.eyebrow", "Logic"),
        M.table([{ label: "Predecessor", render: (r) => `${r.code} ${r.name}` }, { label: "Type", render: (r) => `${r.dep_type}${r.lag_days ? " " + (r.lag_days > 0 ? "+" : "") + r.lag_days : ""}` }], d.predecessors, { empty: "No predecessors." }),
        M.table([{ label: "Successor", render: (r) => `${r.code} ${r.name}` }, { label: "Type", render: (r) => r.dep_type }], d.successors, { empty: "No successors." }),
        h("div.eyebrow", `Events (${d.events.length})`),
        M.table([{ label: "Happened", render: (e) => J.d(e.occurred_at) }, { label: "Event", render: (e) => J.label(e.event_type) },
          { label: "Status", render: (e) => J.status(e.status) }, { label: "By", render: (e) => `${e.actor || "—"} · ${e.device_id}` }],
          d.events.filter((e) => e.event_type !== "approval"), { onRow: (e) => J.trailSheet({ event_id: e.event_id }), empty: "No events yet." }),
        h("div.eyebrow", `Evidence (${d.evidence.length})`),
        M.table([{ label: "Kind", key: "kind" }, { label: "Title", render: (e) => e.title || e.filename || "—" }, { label: "Captured", render: (e) => J.dt(e.captured_at) }, { label: "Origin", render: (e) => J.origin(e.origin) }],
          d.evidence, { onRow: (e) => J.trailSheet({ evidence_id: e.evidence_id }), empty: "No evidence linked." }),
        d.blockers.length ? h("div.stack", h("div.eyebrow", "Blockers"), M.table(["raised_on", "category", "status", "expected_clear", "description"], d.blockers)) : null,
        h("div.eyebrow", "Forecast finish at each snapshot"),
        M.table([{ label: "#", key: "seq" }, { label: "Known at", render: (r) => J.dt(r.captured_at) }, { label: "Finish", render: (r) => J.d(r.ef) }, { label: "Float", key: "total_float" }, { label: "Why", key: "reason" }], d.forecast_history),
        d.aliases.length ? h("p.note", `Field names that link here: ${d.aliases.map((x) => `${x.alias} (${x.source})`).join(", ")}`) : null);
    } catch (e) { body.replaceChildren(M.callout(e.message, "fail")); }
  };

  /* ----------------------------------------------------- the evidence trail */
  J.trailSheet = async function (ref) {
    const body = h("div.stack", M.loading("tracing the evidence"));
    M.openSheet("Evidence trail", body);
    try {
      const d = await M.tool("jarvis_evidence_trail", ref);
      const e = d.evidence;
      M.$("#sheet-title").textContent = `Evidence · ${e.title || e.filename || e.evidence_id}`;
      const norm = (e.metadata || {}).normalization || {};
      const media = d.media || {};
      put(body,
        h("div.row", M.chip(e.kind, "info"), J.origin(e.origin_label), e.language ? M.chip(`language ${e.language}`, "plain") : null,
          d.integrity.sha256_matches ? M.chip("SHA-256 MATCHES", "ok") : M.chip("HASH MISMATCH", "fail")),
        M.kv({ "evidence id": e.evidence_id, "captured": `${J.dt(e.captured_at)} by ${e.captured_by || "—"} on ${e.device_id}`,
          "happened": J.dt(e.occurred_at), "source": e.source_type, "file": e.filename, "sha256": e.sha256 }),
        e.original_text ? h("div.card.raised", h("div.eyebrow", "Original, as captured"), h("div.jv-original.selectable", e.original_text)) : null,
        norm.vernacular ? h("div.card.raised", h("div.eyebrow", `Reading (${norm.method || "lexicon"}${norm.model ? " · " + norm.model : ""}) · coverage ${Math.round((norm.coverage || 0) * 100)}%`),
          h("div.jv-original", norm.normalized), norm.translation ? h("p.note", `Model translation (inference): ${norm.translation}`) : null,
          h("div.jv-terms", (norm.terms || []).slice(0, 30).map((t) => M.chip(`${t.surface} → ${t.reading}`, "plain")))) : null,
        media.image && media.image.evidence_id ? h("div.card.raised", h("div.eyebrow", `Image · OCR ${media.image.ocr_engine || "—"}`),
          h("p", media.image.ocr_text || "no text read"),
          media.image.vision_description ? h("p.note", `Vision model ${media.image.vision_model} (AI inference, not a measurement): ${media.image.vision_description}`) : null) : null,
        media.voice && media.voice.evidence_id ? h("div.card.raised", h("div.eyebrow", `Voice memo · ${media.voice.status}`), h("p", media.voice.transcript || media.voice.detail || "")) : null,
        h("div.eyebrow", "Extraction steps"),
        M.table([{ label: "Method", key: "method" }, { label: "Model", render: (x) => x.model || "—" }, { label: "Status", key: "status" }, { label: "When", render: (x) => M.time(x.created_at) }], d.extractions),
        h("div.eyebrow", "What was read, and where it was linked"),
        M.table([{ label: "Kind", key: "kind" }, { label: "Statement", render: (o) => o.normalized_text || o.original_text }, { label: "Activity", render: (o) => o.activity_code || (o.link_reasons.candidates && o.link_reasons.candidates[0] ? `? ${o.link_reasons.candidates[0].code}` : "—") },
          { label: "Link", render: (o) => h("span", J.status(o.link_status), ` ${M.num(o.link_confidence, 2)}`) }], d.observations),
        ...d.observations.filter((o) => o.link_reasons && o.link_reasons.candidates).slice(0, 3).map((o) => h("p.note", `Why ${o.activity_code || "no activity"}: ${(o.link_reasons.candidates[0] || { reasons: [] }).reasons.join("; ")}`)),
        h("div.eyebrow", "Events, approvals and schedule effect"),
        ...d.events.map((ev) => h("div.card.raised",
          h("div.row.between", h("b", `${J.label(ev.event_type)}${ev.activity_code ? " · " + ev.activity_code : ""}`), J.status(ev.status)),
          M.kv({ "event id": ev.event_id, "captured": `${J.dt(ev.captured_at)} · ${ev.actor || "—"} · ${ev.device_id}`, "approved": ev.approved_by ? `${ev.approved_by} at ${J.dt(ev.approved_at)}` : "—",
            "confidence": ev.confidence, "content hash": (ev.content_hash || "").slice(0, 24) + "…" }),
          ev.schedule_effect ? h("div.stack",
            h("p.note", `Schedule effect: ${ev.activity_code} finish ${J.d(ev.schedule_effect.previous_state.ef)} → ${J.d(ev.schedule_effect.new_state.ef)}; project finish ${J.d(ev.schedule_effect.project_finish_before)} → ${J.d(ev.schedule_effect.project_finish_after)}`),
            ev.schedule_effect.downstream ? h("div.row", ev.schedule_effect.downstream.map((x) => M.chip(`${x.code} ${J.days(x.delta_days)}`, "warn"))) : null) : null)));
    } catch (err) { body.replaceChildren(M.callout(err.message, "fail")); }
  };
})();
