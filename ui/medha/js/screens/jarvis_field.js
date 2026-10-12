/* PROJECTS (field side) - fast field capture in any language, the evidence
   register with its full trail, and offline sync with conflicts shown side by
   side. The page never decides anything: capture, extraction, linking and
   conflict detection happen in Python and come back to be shown. */
(function () {
  "use strict";
  const M = window.ZEUS;
  const { h } = M;
  const J = M.jv;

  /* ------------------------------------------------------- field capture --- */
  function resultView(r) {
    const obs = (r.observations || []).filter((o) => o.kind !== "note" || (o.link && o.link.status === "linked"));
    return h("div.stack",
      h("div.row", M.chip(`evidence ${r.evidence_id}`, "info"), M.chip(r.kind, "plain"), M.chip(`language ${r.language}`, r.vernacular ? "sample" : "plain"),
        r.events.length ? M.chip(`${r.events.length} update(s) submitted for approval`, "warn") : M.chip("no state change submitted", "plain"),
        M.button("Evidence trail", "small ghost", () => J.trailSheet({ evidence_id: r.evidence_id }))),
      (r.warnings || []).map((w) => M.callout(w, "warn")),
      r.original_text ? h("div.card.raised", h("div.eyebrow", "Original, as captured - kept unchanged"), h("div.jv-original.selectable", r.original_text)) : null,
      r.vernacular ? h("div.card.raised", h("div.eyebrow", `Reading · lexicon coverage ${Math.round((r.coverage || 0) * 100)}%`), h("div.jv-original", r.normalized_text),
        r.translation ? h("p.note", `Local model translation (inference): ${r.translation}`) : null,
        h("div.jv-terms", (r.terms || []).map((t) => M.chip(`${t.surface} → ${t.reading}`, "plain")))) : null,
      r.vision && r.vision.text ? h("div.card.raised", h("div.eyebrow", `Vision model ${r.vision.model} - AI inference, not a measurement`), h("p", r.vision.text)) : null,
      h("div.eyebrow", `What ZEUS read (${obs.length})`),
      obs.length ? h("div.stack", obs.map((o) => {
        const link = o.link, top = (link.candidates || [])[0];
        return h("div.card.raised",
          h("div.row.between", h("span", h("b", J.label(o.kind)), o.occurred_on ? ` · happened ${J.d(o.occurred_on)}` : "", o.expected_date ? ` · expected ${J.d(o.expected_date)}` : ""), J.status(link.status)),
          h("p", o.reading), o.text !== o.reading ? h("p.note", `Original: ${o.text}`) : null,
          h("div.row", o.percent_complete != null ? M.chip(`${o.percent_complete}%`, "info") : null, o.quantity != null ? M.chip(`${o.quantity} ${o.unit || ""}`, "info") : null,
            o.blocker_category ? M.chip(o.blocker_category, "fail") : null, M.chip(`read by ${o.method}`, "plain"),
            link.status === "linked" && top ? M.chip(`${top.code} · ${M.num(link.confidence, 2)}`, "ok") : null),
          link.status !== "linked" && link.candidates.length ? h("div.stack", h("p.note", `${link.reason} - choose the activity:`),
            h("div.row", link.candidates.map((c) => M.button(`${c.code} · ${M.num(c.score, 2)}`, "small", async () => {
              await M.tool("resolve_activity_link", { observation_id: o.observation_id, activity_id: c.activity_id });
              M.toast(`Linked to ${c.code} - submitted for approval and remembered`, "ok");
            })))) : null,
          top ? h("p.note", `Why: ${top.reasons.join("; ")}`) : null);
      })) : M.empty("No progress, blocker or delay statement was recognised - the evidence is stored and searchable."));
  }

  M.screen("capture", {
    group: "PROJECTS", label: "Field Capture", icon: "capture", title: "Field capture",
    sub: "Type or paste an update in any language or script, or add a photo, scan, DPR, spreadsheet, voice memo or letter. ZEUS reads it, links it to the schedule and submits it for approval - the original is always kept.",
    async render(body) {
      if (!(await J.ensure(body))) return;
      body.appendChild(J.picker(() => M.refresh()));
      const net = h("div.row");
      body.appendChild(net);
      M.tool("jarvis_sync_status", {}).then((s) => net.replaceChildren(
        h("span.jv-status." + (s.connectivity === "ONLINE" ? "is-online" : "is-offline"), h("i"), s.connectivity),
        h("span.note", s.connectivity === "ONLINE" ? `hub reachable · ${s.pending_sync} waiting to sync` :
          `${s.connectivity_detail}. Captures are saved on this device (${s.device_id}) and queued - ${s.pending_sync} waiting.`),
        M.button("Offline sync", "small ghost", () => M.go("sync")))).catch((e) => net.replaceChildren(M.callout(e.message, "warn")));

      const text = h("textarea.field.jv-note-input", { placeholder: "e.g.  P17 dhalai shuru, 40% ho gaya   ·   पियर P17 सरिया बांधाई पूरी   ·   Pier P17 concreting delayed - no cement" });
      const when = h("input.field", { type: "date", title: "when it happened (optional - read from the note when written there)" });
      const source = h("select.field", ["field", "contractor", "office", "client"].map((s) => h("option", { value: s }, `source: ${s}`)));
      const models = h("input", { type: "checkbox" });
      const vision = h("input", { type: "checkbox" });
      const out = h("div.stack");
      const show = (r) => { out.replaceChildren(resultView(r)); };
      const submitText = async () => {
        if (!text.value.trim()) { M.toast("Type the update first", "warn"); return; }
        out.replaceChildren(M.loading(models.checked ? "reading with the local models (slower)" : "reading, extracting and linking"));
        show(await M.tool("record_field_update", { project: J.project, text: text.value, occurred_on: when.value, source_type: source.value, use_model: models.checked }));
      };
      const fileBtn = (label, purpose, extra) => M.button(label, "", async () => {
        const f = await M.call("pick_file", { purpose });
        if (!f.path) return;
        out.replaceChildren(M.loading(purpose === "audio" ? "transcribing with the local recogniser" : purpose === "image" ? "reading the image (OCR)" : "extracting the document"));
        show(await M.tool("ingest_field_evidence", Object.assign({ project: J.project, path: f.path, caption: text.value, source_type: source.value,
          use_model: models.checked, use_vision: purpose === "image" && vision.checked }, extra || {})));
      });
      body.append(h("div.card", text,
        h("div.filters.jv-filters", when, source, h("label.row", models, h("span.note", "also use the local models")), h("label.row", vision, h("span.note", "describe photos with the vision model")),
          h("span.spacer"), M.button("Submit update", "primary", submitText)),
        h("div.row", fileBtn("Site photo", "image"), fileBtn("Document / DPR / scan", "document"), fileBtn("Spreadsheet", "document"),
          fileBtn("Voice memo", "audio"), fileBtn("Letter / notice", "document", { correspondence: true })),
        h("p.note", "The text box doubles as the caption for a file. Spoken updates work from the home screen too: press MIC and say “field update: …”.")), out);
    },
  });

  /* ------------------------------------------------------ evidence trail --- */
  M.screen("trail", {
    group: "PROJECTS", label: "Evidence Trail", icon: "trail", title: "Evidence trail",
    sub: "Every captured record with its hash, extraction, activity link, approval and schedule effect. Click any item to trace it.",
    async render(body, params, actions) {
      if (!(await J.ensure(body))) return;
      body.appendChild(J.picker(() => M.refresh()));
      actions.append(M.button("Verify the event chain", "small", async () => {
        const v = await M.tool("jarvis_verify_events", {});
        M.openSheet("Integrity", h("div.stack", M.chip(v.events.ok ? "EVENT CHAIN VERIFIED" : "EVENT CHAIN BROKEN", v.events.ok ? "ok" : "fail"),
          M.kv({ "events checked": v.events.events, "head": v.events.head, "database": v.database.ok ? "integrity ok" : "PROBLEMS" }),
          v.events.problems.length ? M.pre(v.events.problems) : null, M.pre(v.database.tables)));
      }));
      const kind = h("select.field", ["", "image", "document", "text", "voice", "spreadsheet", "correspondence", "schedule_file"].map((k) => h("option", { value: k }, k || "every kind")));
      const activity = h("input.field", { placeholder: "activity code" });
      const query = h("input.field.grow", { placeholder: "text in the record" });
      const list = h("div.stack");
      const run = async () => {
        list.replaceChildren(M.loading("reading the evidence register"));
        const d = await M.tool("jarvis_evidence", { project: J.project, kind: kind.value, activity: activity.value.trim(), query: query.value.trim() });
        list.replaceChildren(M.table([{ label: "Happened", render: (e) => J.d(e.occurred_at) }, { label: "Kind", key: "kind" }, { label: "Record", render: (e) => h("span", h("b", e.title || e.filename || "—"), h("div.note", (e.excerpt || "").slice(0, 160))) },
          { label: "Activities", render: (e) => e.activities || "—" }, { label: "Captured", render: (e) => `${J.dt(e.captured_at)} · ${e.captured_by || "—"} · ${e.device_id}` },
          { label: "Language", key: "language" }, { label: "Origin", render: (e) => J.origin(e.origin_label) }], d.evidence, { onRow: (e) => J.trailSheet({ evidence_id: e.evidence_id }), empty: "No evidence matches." }));
      };
      [query, activity].forEach((i) => i.addEventListener("keydown", (e) => { if (e.key === "Enter") run(); }));
      body.append(h("div.filters.jv-filters", kind, activity, query, M.button("Filter", "small primary", run)), list);
      run();
    },
  });

  /* -------------------------------------------------------- offline sync --- */
  M.screen("sync", {
    group: "PROJECTS", label: "Offline Sync", icon: "sync", title: "Offline conflict-safe sync",
    sub: "Field devices keep working without a network. Updates travel as bundles through a hub folder (LAN share or USB drive); conflicting submissions are kept side by side, never overwritten.",
    async render(body, params, actions) {
      actions.append(M.button("Sync now", "small primary", async () => {
        const r = await M.tool("request_sync", {});
        M.toast(r.offline ? `OFFLINE - ${r.reason}` : `Synchronised: ${(r.exported || {}).events || 0} sent, ${r.imported.length} bundle(s) read, ${r.conflicts} conflict(s)`, r.offline ? "warn" : "ok");
        M.refresh();
      }));
      const s = await M.tool("jarvis_sync_status", {});
      body.append(h("div.card",
        h("div.row.between", h("span.jv-status." + (s.connectivity === "ONLINE" ? "is-online" : "is-offline"), h("i"), s.connectivity), h("span.note", s.connectivity_detail)),
        h("div.jv-kpis", J.kpi("This device", s.device_id, "", "small"), J.kpi("Waiting to sync", s.pending_sync, "captured here, not yet sent"),
          J.kpi("Open conflicts", s.open_conflicts, "", s.open_conflicts ? "late" : ""), J.kpi("Last successful sync", s.last_successful_sync ? J.dt(s.last_successful_sync) : "never"),
          J.kpi("Other devices seen", s.peers.length, s.peers.join(", "))),
        h("p.note", s.storage),
        h("div.row", M.button(s.hub_path ? `Hub: ${s.hub_path}` : "Choose the sync hub folder", "small", async () => {
          const f = await M.call("pick_folder", { purpose: "sync hub" });
          if (!f.path) return;
          const r = await M.tool("set_sync_hub", { folder: f.path });
          M.toast(r.reachable.reachable ? "Hub set and reached" : `Hub set - not reachable now: ${r.reachable.reason}`, r.reachable.reachable ? "ok" : "warn");
          M.refresh();
        }), M.button("Export a bundle to a folder / USB", "small ghost", async () => {
          const f = await M.call("pick_folder", { purpose: "export bundle" });
          if (!f.path) return;
          const r = await M.tool("export_sync_bundle", { folder: f.path });
          M.toast(`${r.events} event(s) written to ${M.basename(r.bundle)}`, "ok");
          M.refresh();
        }), M.button("Import a bundle", "small ghost", async () => {
          const f = await M.call("pick_file", { purpose: "sync bundle" });
          if (!f.path) return;
          const r = await M.tool("ingest_sync_bundle", { path: f.path });
          M.toast(r.skipped ? r.reason : `${r.new} new event(s), ${r.duplicates} already here, ${r.conflicts.length} conflict(s)${r.refused.length ? `, ${r.refused.length} REFUSED (hash)` : ""}`, r.refused && r.refused.length ? "fail" : "ok");
          M.refresh();
        }))));
      const conflicts = (await M.tool("jarvis_sync_conflicts", { status: "" })).conflicts;
      body.appendChild(h("h3", `Conflicts (${conflicts.filter((c) => c.status === "open").length} open)`));
      if (!conflicts.length) body.appendChild(M.empty("No conflicting submissions."));
      conflicts.forEach((c) => {
        const rec = c.recommendation || {};
        const side = (key) => {
          const v = c[`version_${key}`] || {};
          return h("div.jv-version" + (rec.suggested === key && c.status === "open" ? ".suggested" : ""),
            h("div.row.between", h("span.eyebrow", `DEVICE ${key.toUpperCase()} · ${v.device_id || c[`device_${key}`]}`), J.status(v.status)),
            h("div.val", c[`value_${key}`]), h("p.note", `${J.label(c.field)} · captured ${J.dt(v.captured_at || c[`captured_${key}`])} by ${v.actor || c[`actor_${key}`] || "—"}`),
            v.note ? h("p", v.note) : null,
            h("div.row", (rec[key] || { reasons: [] }).reasons.map((r) => M.chip(r, "plain")),
              (v.evidence_ids || []).length ? M.button("Evidence", "small ghost", () => J.trailSheet({ evidence_id: v.evidence_ids[0] })) : null),
            c.status === "open" ? M.button(`Keep ${c[`value_${key}`]}`, "small" + (rec.suggested === key ? " primary" : ""), async () => {
              const why = await M.form("Resolve the conflict", [{ name: "reason", label: "Reason (kept in the history)" }], { submit: "Resolve" });
              if (!why) return;
              await M.tool("resolve_sync_conflict", { conflict_id: c.conflict_id, choice: key, reason: why.reason });
              M.toast("Resolved - the other value stays in the history as superseded", "ok");
              M.refresh();
            }) : null);
        };
        body.appendChild(h("div.card",
          h("div.row.between", h("b", `${c.activity_code || "—"} · ${c.activity_name || ""}`), J.status(c.status)),
          h("p.note", c.reason), h("div.jv-versus", side("a"), side("b")),
          c.status === "open" ? h("div.row", h("span.note", `ZEUS suggests device ${String(rec.suggested || "").toUpperCase()} - ${rec.note || ""}`),
            M.button("Enter another value", "small ghost", async () => {
              const v = await M.form("Resolve with a verified value", [{ name: "value", label: J.label(c.field) }, { name: "reason", label: "Reason" }], { submit: "Resolve" });
              if (!v) return;
              await M.tool("resolve_sync_conflict", { conflict_id: c.conflict_id, choice: "value", value: v.value, reason: v.reason });
              M.refresh();
            })) : h("p.note", `Resolved by ${c.resolved_by || "—"} at ${J.dt(c.resolved_at)}: ${c.chosen_value}`)));
      });
      body.appendChild(h("div.card", h("h3", "Sync history"), M.table([{ label: "When", render: (x) => M.time(x.at) }, { label: "Direction", key: "direction" },
        { label: "Peer", render: (x) => x.peer_device || "—" }, { label: "Events", key: "events_total" }, { label: "New", key: "events_new" },
        { label: "Duplicates", key: "duplicates" }, { label: "Conflicts", key: "conflicts" }, { label: "Bundle", render: (x) => M.basename(x.bundle_path) }], s.history, { empty: "No sync has run on this device." })));
    },
  });
})();
