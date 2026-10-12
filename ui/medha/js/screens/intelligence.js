/* INTELLIGENCE & KNOWLEDGE: company records 1989-2026, cited answers with live
   stages, the document explorer, the evidence viewer and the reasoning workspace. */
(function () {
  "use strict";
  const M = window.ZEUS;
  const { h } = M;
  const STAGES = ["QUERY", "UNDERSTANDING", "SEARCHING", "EVIDENCE FOUND", "ANALYZING", "VALIDATING", "GENERATING", "READY"];
  const ORIGIN_TONE = { company: "ok", sample: "sample", demo: "sample", general_knowledge: "info", test_fixture: "plain" };
  M.lastAnalysis = null;

  /* ------------------------------------------------------ company timeline ---- */
  /* Everything on this screen is read from the knowledge base: the per-year status,
     the type / department catalogue (what the company records hold in other years),
     the events, the figures and where each document came from. */
  const YEAR_STATUS = { AVAILABLE: ["COMPANY RECORD", "ok"], SAMPLE: ["SAMPLE ONLY", "sample"],
    GENERAL: ["GENERAL KNOWLEDGE ONLY", "info"], MISSING: ["NO COMPANY RECORDS", "warn"] };
  const CONF_TONE = { HIGH: "ok", MODERATE: "info", LOW: "warn", NONE: "plain" };
  const pretty = (t) => String(t || "").replace(/_/g, " ");
  /* The answer's verification state: VERIFIED only when the cited evidence is company records. */
  const STATE = { VERIFIED: ["VERIFIED · COMPANY RECORDS", "ok"], VALIDATED: ["VALIDATED · NOT COMPANY RECORDS", "info"],
    INSUFFICIENT_DATA: ["INSUFFICIENT DATA", "warn"], FAILED: ["FAILED VALIDATION", "fail"], EVIDENCE_FOUND: ["EVIDENCE FOUND", "info"],
    RUNNING: ["RUNNING", "info"], DRAFT: ["DRAFT", "plain"] };
  const stateChip = (state, ok) => { const [label, tone] = STATE[state] || (ok ? STATE.VALIDATED : STATE.FAILED); return M.chip(label, tone); };
  /* A year with no documents but a dated register event (1989: incorporation) is not "no records". */
  const yearStatus = (y) => y.status === "MISSING" && y.event_count
    ? [`NO COMPANY DOCUMENTS · ${y.event_count} DATED EVENT${y.event_count > 1 ? "S" : ""}`, "warn"] : YEAR_STATUS[y.status];

  async function provenanceSheet(documentId, title) {
    const p = await M.tool("document_provenance", { document_id: documentId });
    if (!p || p.ok === false) { M.toast((p && p.error) || "provenance not available", "fail"); return; }
    M.openSheet(title || p.document.title, h("div.stack", M.kv(p.document, [["filename", "File"], ["source_path", "Path"], ["year", "Year"], ["doc_date", "Dated"], ["document_type", "Type"], ["authority_label", "Authority"], ["origin", "Origin"], ["confidentiality", "Classification"], ["department", "Department"], ["source_hash", "Content hash"]]),
      M.kv(p.counts), h("div.eyebrow", "ingestion history"), M.table(["at", "status", "detail"], (p.ingestion_log || []).map((l) => ({ ...l, at: M.time(l.at) })))));
  }

  M.screen("company", {
    group: "INTELLIGENCE", label: "Company Intelligence", icon: "company", title: "Company Intelligence",
    sub: "The company's own records, year by year, 1989-2026. A year without documents is shown as missing - never filled in.",
    async render(body, params, actions) {
      const BASE = 44, ZMIN = 18, ZMAX = 110;
      let zoom = BASE, selected = null;
      const zoomLabel = h("span.note.zoom-level", "100%");
      const setZoom = (z) => { zoom = Math.max(ZMIN, Math.min(ZMAX, Math.round(z))); paint(); };
      const fit = () => { const w = years.clientWidth || body.clientWidth || 900; setZoom(w / Math.max(1, coverage.years.length) - 4); };
      actions.append(M.button("−", "small", () => setZoom(zoom - 10)), zoomLabel, M.button("+", "small", () => setZoom(zoom + 12)),
        M.button("Fit", "small", () => fit()), M.button("Reset", "small ghost", () => setZoom(BASE)));

      const summary = h("div.grid.four");
      const provenance = h("div.stack");
      const fType = h("select.field", { style: "width:200px" });
      const fDept = h("select.field", { style: "width:200px" });
      const fSource = h("select.field", { style: "width:220px" });
      const fConf = h("select.field", { style: "width:170px" });
      const fText = h("input.field", { placeholder: "Search years - type, department, site, figure, event" });
      const matchNote = h("span.note");
      const filters = h("div.filters", h("div.grow", fText), fType, fDept, fSource, fConf,
        M.button("Clear", "small ghost", () => { [fType, fDept, fSource, fConf].forEach((s) => (s.value = "")); fText.value = ""; paint(); }), matchNote);
      const years = h("div.timeline", { tabindex: "0", "aria-label": "company timeline - ctrl + wheel zooms" });
      const tl = h("div.card", h("div.card-head", h("h3", "Timeline"), h("div.legend",
        h("span", h("i", { style: "background:var(--flare)" }), "company records"), h("span", h("i", { style: "background:var(--sample)" }), "sample / demo only"),
        h("span", h("i", { style: "background:var(--process)" }), "general knowledge only"), h("span", h("i", { style: "background:var(--steel-2);border:1px dashed var(--steel-3)" }), "no company records"),
        h("span", h("i.ev-key"), "dated event"))), filters, years);
      const detail = h("div.stack");
      const gaps = h("div.stack");
      body.append(summary, provenance, tl, detail, gaps);

      let coverage, timeline;
      await M.section(body, "reading the coverage report", async () => {
        [coverage, timeline] = await Promise.all([M.tool("knowledge_coverage", {}), M.tool("company_timeline", {})]);
      });
      if (!coverage || coverage.ok === false || !coverage.years) {
        body.replaceChildren(M.callout((coverage && coverage.error) || "The coverage report could not be read.", "fail"));
        return;
      }
      const s = coverage.summary;
      [["Years with company records", s.available_years, "ok"], ["Sample / demo only", s.sample_only_years, "sample"],
        ["General knowledge only", s.general_only_years, "info"], ["Years with no company documents", s.missing_years, s.missing_years ? "warn" : "ok"]].forEach(([label, value, tone]) =>
        summary.appendChild(h("div.card.metric", h("div.eyebrow", label), h("div.value", String(value)), M.chip(tone === "ok" ? "counted" : tone, tone))));
      body.insertBefore(h("p.note", `${coverage.scope_note} · ${s.company_documents} company documents, ${s.typed_sections} typed sections`), tl);
      if (!s.available_years) provenance.appendChild(M.callout("There are no company documents in the knowledge base yet. Answers about the company will say so. Add documents in Document Explorer → Add (RAG_INGEST.bat does the same).", "warn"));
      (coverage.corpora || []).forEach((c) => provenance.appendChild(h("div.card.provenance",
        h("div.row.between", h("div.row", h("div.eyebrow", "dataset"), h("b", c.name)), M.chip(`${c.documents} company documents`, "plain")),
        c.note ? M.callout(`What the dataset says about itself: ${c.note}`, "warn") : h("p.note", "no note recorded with this dataset"))));

      const opt = (select, values, label) => select.replaceChildren(h("option", { value: "" }, label), ...values.map(([v, text]) => h("option", { value: v }, text)));
      opt(fType, coverage.catalogue.document_types.map((t) => [t, pretty(t)]), "all document types");
      opt(fDept, coverage.catalogue.departments.map((d) => [d, d]), "all departments");
      opt(fSource, Object.entries(YEAR_STATUS).map(([k, [label]]) => [k, label.toLowerCase()]), "every source");
      opt(fConf, ["HIGH", "MODERATE", "LOW", "NONE"].map((c) => [c, `confidence ${c.toLowerCase()}`]), "any confidence");
      [fType, fDept, fSource, fConf].forEach((x) => x.addEventListener("change", paint));
      fText.addEventListener("input", paint);
      years.addEventListener("wheel", (e) => { if (e.ctrlKey) { e.preventDefault(); setZoom(zoom + (e.deltaY < 0 ? 8 : -8)); } }, { passive: false });

      const byYear = {};
      (timeline && timeline.years || []).forEach((y) => (byYear[y.year] = y));
      const haystack = (y) => [y.year, yearStatus(y)[0], ...Object.keys(y.types || {}).map(pretty), ...(y.departments || []),
        ...(y.sites || []), ...(y.metrics || []), ...(y.events || []).map((e) => e.label)].join(" ").toLowerCase();
      const matches = (y) => (!fType.value || (y.types || {})[fType.value]) && (!fDept.value || (y.departments || []).includes(fDept.value))
        && (!fSource.value || y.status === fSource.value) && (!fConf.value || y.confidence === fConf.value)
        && (!fText.value.trim() || haystack(y).includes(fText.value.trim().toLowerCase()));

      function paint() {
        zoomLabel.textContent = `${Math.round((zoom / BASE) * 100)}%`;
        years.style.setProperty("--year-w", zoom + "px");
        years.classList.toggle("compact", zoom < 30);
        const max = Math.max(1, ...coverage.years.map((y) => y.sources));
        let shown = 0;
        years.replaceChildren(h("div.years", coverage.years.map((y) => {
          const hit = matches(y);
          if (hit) shown += 1;
          const [label] = yearStatus(y);
          const tip = `${y.year}: ${label} - ${y.company_documents} company, ${y.sample_documents} sample, ${y.general_documents} general · ${y.figures} figures · confidence ${y.confidence}`
            + ((y.events || []).length ? "\n" + y.events.map((e) => "◆ " + e.label).join("\n") : "");
          return h("button.year" + (hit ? "" : ".dim") + (selected === y.year ? ".sel" : ""), { type: "button", title: tip, "aria-label": tip, onclick: () => select(y) },
            y.event_count ? h("i.ev", { title: `${y.event_count} dated event(s)\n` + (y.events || []).map((e) => e.label).join("\n") }) : h("i.ev.none"),
            h("div.col." + y.status, { style: `height:${14 + (y.sources / max) * 120}px` }), h("span.label", String(y.year)));
        })));
        matchNote.textContent = `${shown} of ${coverage.years.length} years match`;
      }

      async function select(y) {
        selected = y.year;
        paint();
        const [label, tone] = yearStatus(y);
        const card = h("div.card.year-detail", h("div.card-head", h("div.row", h("h2", String(y.year)), M.chip(label, tone), M.chip(`confidence ${y.confidence}`, CONF_TONE[y.confidence])),
          M.button(`Ask about ${y.year}`, "primary small", () => M.go("knowledge", { question: `What happened in ${y.year}?` }))));
        detail.replaceChildren(card);
        if (y.status === "MISSING") card.appendChild(M.callout(y.event_count
          ? `No company documents are dated ${y.year}. The records hold ${y.event_count} dated event(s) for it (below) - nothing else is estimated or filled in.`
          : `NO COMPANY RECORDS for ${y.year}. Nothing is estimated or filled in - add the year's documents (RAG_INGEST.bat) and it appears here.`, "warn"));
        card.appendChild(M.kv(y, [["company_documents", "Company documents"], ["sample_documents", "Sample / demo documents"], ["general_documents", "General knowledge"], ["figures", "Figures"], ["event_count", "Dated events"]]));
        let d = null;
        await M.section(card, `reading ${y.year}`, async () => { d = await M.tool("company_year_detail", { year: String(y.year) }); });
        if (!d || d.ok === false) { card.appendChild(M.callout((d && d.error) || "detail not available", "fail")); return; }
        const block = (title, content) => card.append(h("div.eyebrow", { style: "margin-top:16px" }, title), content);
        if ((d.events || []).length) block("Dated events", h("div.stack", d.events.map((e) =>
          h("div.row", h("span.ev-dot"), h("span.grow", e.label), M.chip(e.origin, ORIGIN_TONE[e.origin]), e.node_id ? M.button("evidence", "small ghost", () => M.showEvidence(e.node_id)) : null))));
        if ((d.documents || []).length) block("Documents", M.table([{ label: "Title", key: "title" }, { label: "Type", render: (x) => pretty(x.type) },
          { label: "Origin", render: (x) => M.chip(x.origin, ORIGIN_TONE[x.origin]) }, { label: "Dataset", render: (x) => x.dataset || "—" },
          { label: "Dated", key: "dated" }, { label: "Passages", key: "passages_for_year" }], d.documents, { onRow: (x) => provenanceSheet(x.document_id, x.title) }));
        const companySections = (d.sections || []).filter((t) => t.origin === "company");
        if (companySections.length) {
          const byType = {};
          companySections.forEach((t) => (byType[t.document_type] = byType[t.document_type] || []).push(t));
          block("Typed sections", h("div.stack", Object.entries(byType).sort().map(([type, list]) => h("div.row",
            M.chip(pretty(type), "info"), h("span.grow.note", list.map((t) => [t.heading, t.department, t.site].filter(Boolean).join(" · ")).join("  |  ")),
            M.button("evidence", "small ghost", () => M.showEvidence(list[0].node_id))))));
        }
        if ((d.figures || []).length) block("Figures stated for the year", M.table([{ label: "Figure", key: "metric" }, { label: "Value", render: (f) => M.num(f.value, 2) },
          { label: "Unit", key: "unit" }, { label: "Source", key: "title" }, { label: "Origin", render: (f) => M.chip(f.origin, ORIGIN_TONE[f.origin]) }],
          d.figures, { onRow: (f) => f.node_id && M.showEvidence(f.node_id) }));
        const t = byYear[y.year] || {};
        [["financial", "Financial changes (≥10%)", (f) => h("div.row", M.chip(f.origin, ORIGIN_TONE[f.origin]), h("span", `${f.metric}: ${M.num(f.from)} → ${M.num(f.to)} ${f.unit || ""} (${f.change_percent > 0 ? "+" : ""}${f.change_percent}%)`), f.node_id ? M.button("evidence", "small ghost", () => M.showEvidence(f.node_id)) : null)],
          ["maintenance", "Failures", (m) => h("div.row", h("span", m.event), M.button("evidence", "small ghost", () => M.showEvidence(m.node_id)))],
          ["incidents", "Incidents", (i) => h("div.row", h("span", i.name), M.button("evidence", "small ghost", () => M.showEvidence(i.node_id)))],
          ["projects", "Projects", (i) => h("div.row", h("span", i.name), M.button("evidence", "small ghost", () => M.showEvidence(i.node_id)))],
          ["decisions", "Decisions", (x) => h("div.row", h("span.grow", x.text), M.button("evidence", "small ghost", () => M.showEvidence(x.node_id)))]]
          .forEach(([key, title, render]) => { if ((t[key] || []).length) block(title, h("div.stack", t[key].slice(0, 30).map(render))); });
        if (y.status === "AVAILABLE") block("Missing this year (held in other years)", h("div.row",
          ...(y.missing_types.length ? y.missing_types.map((x) => M.chip(pretty(x), "warn")) : [M.chip("no document type missing", "ok")]),
          ...(y.missing_departments.length ? y.missing_departments.map((x) => M.chip(x, "warn")) : [M.chip("no department missing", "ok")])));
      }

      paint();
      const typeGaps = coverage.document_type_gaps || {}, deptGaps = coverage.department_gaps || {};
      const gapRows = (map) => Object.keys(map).length ? h("div.stack", Object.entries(map).map(([k, ranges]) => h("div.row", M.chip(pretty(k), "warn"), h("span.note", ranges.join(" · ")))))
        : h("div.row", M.chip("none", "ok"));
      gaps.append(h("div.grid.two", h("div.card", h("h3", "Document types missing in some years"), h("p.note", coverage.catalogue.note), gapRows(typeGaps)),
        h("div.card", h("h3", "Departments missing in some years"), gapRows(deptGaps))));
      const periods = coverage.missing_periods || [];
      if (periods.length) gaps.appendChild(h("div.card", h("h3", "Years with no company documents"), h("p.note", periods.map((p) => p.from === p.to ? `${p.from}` : `${p.from}-${p.to}`).join(" · "))));
      const first = params && params.year ? coverage.years.find((y) => y.year === Number(params.year)) : [...coverage.years].reverse().find((y) => y.status === "AVAILABLE");
      if (first) select(first);
    },
  });

  /* ------------------------------------------------------------ ask (RAG) ---- */
  function storyRail() {
    const rail = h("div.story", { style: `--segments:${STAGES.length}` }, STAGES.map((s) => h("div.seg", { "data-stage": s })));
    const labels = h("div.story-labels", { style: `--segments:${STAGES.length}` }, STAGES.map((s) => h("span", { "data-stage": s }, s)));
    const detail = h("div.note", "waiting for the question");
    const node = h("div.card", rail, labels, detail);
    node.update = (ev) => {
      const idx = STAGES.indexOf(ev.stage);
      if (idx < 0) return;
      rail.querySelectorAll(".seg").forEach((seg, i) => { seg.className = "seg" + (i < idx || ev.stage === "READY" ? " done" : i === idx ? " active" : ""); });
      labels.querySelectorAll("span").forEach((l, i) => { l.className = i < idx || ev.stage === "READY" ? "done" : i === idx ? "active" : ""; });
      detail.textContent = `${ev.stage.toLowerCase()} - ${ev.detail}${ev.agents && ev.agents.length ? " · " + ev.agents.join(", ") : ""} · ${ev.t}s`;
    };
    return node;
  }

  function evidenceCards(evidence) {
    if (!evidence.length) return M.empty("No passage you can access matched.");
    return h("div.deck", evidence.map((e) => {
      const flagged = (e.flags || []).some((f) => f.startsWith("instruction-like"));
      const card = h("div.card.evidence-card" + (["sample", "demo"].includes(e.origin) ? ".sample" : "") + (flagged ? ".flagged" : ""),
        h("div.row.between", h("div.row", h("span.cite", e.id), h("b", e.citation)), h("div.row", M.chip(e.origin, ORIGIN_TONE[e.origin]), M.chip(e.authority, "plain"))),
        (e.flags || []).length ? h("div.row", { style: "margin-top:6px" }, e.flags.map((f) => M.chip(f.slice(0, 60), f.startsWith("instruction-like") ? "warn" : "sample"))) : null,
        h("div.row", { style: "margin-top:8px" }, M.button("Open evidence", "small", () => M.showEvidence(e.node_id, e.id))));
      return card;
    }));
  }

  function artifactButtons(analysisId) {
    const formats = [["xlsx", "Excel workbook"], ["pptx", "PowerPoint deck"], ["docx", "Word report"], ["pdf", "PDF"], ["brief", "Executive brief"], ["action_plan", "Action plan"]];
    const out = h("div.stack");
    const row = h("div.row", formats.map(([fmt, label]) => M.button(label, "small", async () => {
      const r = await M.tool("create_analysis_artifact", { format: fmt, analysis_id: analysisId });
      out.appendChild(artifactResult(r));
    })));
    out.appendChild(row);
    return out;
  }
  function artifactResult(r) {
    if (!r.ok) return M.callout(r.error || "not created", "fail");
    const cons = r.consistency || {};
    return h("div.card.raised", h("div.row.between", h("div.row", M.chip(r.format.toUpperCase(), "plain"), h("b", M.basename(r.path))),
      M.chip(r.verified ? "VERIFIED" : "NOT VERIFIED", r.verified ? "ok" : "warn")),
      h("p.note", `${(r.file_check || {}).detail || ""}${cons.unsupported_numbers && cons.unsupported_numbers.length ? " · numbers not in the analysis: " + cons.unsupported_numbers.join(", ") : ""}`),
      h("div.row", M.button("Open", "small", async () => { const x = await M.call("open_path", { path: r.path }); if (!x.success) M.toast(x.error || "could not open", "fail"); })));
  }
  M.artifactResult = artifactResult;

  /* The summary first; the full deterministic detail (drivers, conflicts,
     actions, gaps) one click away - the evidence already has its own cards. */
  function answerParts(text, cite) {
    const split = String(text || "").split(/\n-{3,}\n/);
    const main = M.answer(split[0], cite);
    if (split.length < 2) return [main];
    const detail = M.answer(split.slice(1).join("\n"), cite);
    detail.hidden = true;
    const toggle = h("button.btn.ghost.small", { onclick: () => { detail.hidden = !detail.hidden; toggle.textContent = detail.hidden ? "Show the full analysis" : "Hide the full analysis"; } }, "Show the full analysis");
    return [main, toggle, detail];
  }

  async function ask(question, out) {
    out.replaceChildren();
    const rail = storyRail();
    out.appendChild(rail);
    const off = M.on("analysis", (ev) => rail.update(ev));
    try {
      const r = await M.call("ask_company", { question });
      M.lastAnalysis = r;
      if (!r.ok) { out.appendChild(M.callout(r.error || "refused", "fail")); return; }
      const byId = {};
      (r.evidence || []).forEach((e) => (byId[e.id] = e));
      const cite = (id) => { if (byId[id]) M.showEvidence(byId[id].node_id, id); else if (id[0] === "C") M.go("reasoning", { analysis: r.analysis_id, focus: id }); };
      out.appendChild(h("div.card", h("div.row.between", h("h3", "Answer"), h("div.row",
        stateChip(r.verification_state, r.verified),
        M.chip(r.narrative_source, "plain"), M.chip(r.analysis_id, "plain"))),
        ...answerParts(r.answer, cite)));
      const rs = r.reasoning || {};
      if ((rs.facts || []).length) out.appendChild(h("div.card", h("div.card-head", h("h3", "Reasoning"), h("span.note", `${rs.model || "model"} - each part checked against the evidence`)),
        h("div.grid.three", [["facts", "Facts (stated in the records)", "ok"], ["inferences", "Inferences (drawn from the facts)", "info"], ["assumptions", "Assumptions / missing", "warn"]]
          .map(([key, label, tone]) => h("div.stack", M.chip(label, tone), (rs[key] || []).length ? h("div", M.answer((rs[key] || []).map((x) => `- ${x}`).join("\n"), cite)) : h("p.note", "none"))))));
      if (r.verification_state === "VALIDATED") out.appendChild(M.callout(`Every number and citation checks out, but the cited evidence is ${(r.cited_origins || []).join(", ") || "not company records"} - not the company's own records, so this is not a verified company answer.`, "info"));
      if (!r.verified && r.validation) out.appendChild(M.callout(`Validation: ${JSON.stringify({ unsupported: r.validation.unsupported_numbers, invalid: r.validation.invalid_citations })}`, "warn"));
      out.appendChild(h("div.grid.two",
        h("div.stack", h("div.eyebrow", `Evidence (${(r.evidence || []).length})`), evidenceCards(r.evidence || [])),
        h("div.stack", h("div.eyebrow", "Create from this same analysis"), artifactButtons(r.analysis_id),
          h("div.eyebrow", "Go deeper"), h("div.row", M.button("Open in the reasoning workspace", "small", () => M.go("reasoning", { analysis: r.analysis_id }))))));
    } catch (e) { out.appendChild(M.callout(e.message, "fail")); }
    finally { off(); }
  }

  M.screen("knowledge", {
    group: "KNOWLEDGE", label: "Ask the Records", icon: "knowledge", title: "Ask the records",
    sub: "Cited answers from the company's records - calculated, validated, and honest about what is missing. Documents are data; instructions inside them are never followed.",
    async render(body, params) {
      const tabs = h("div.tabs");
      const pane = h("div.stack");
      const views = {
        "Company records": () => {
          const q = h("input.field", { placeholder: "e.g. Why did we make losses between 2019 and 2021, and what can we do?", value: params.question || "" });
          const out = h("div.stack");
          const go = M.button("Ask", "primary", async () => { if (q.value.trim()) await ask(q.value.trim(), out); });
          q.addEventListener("keydown", (e) => { if (e.key === "Enter") go.click(); });
          pane.replaceChildren(h("div.row", h("div.grow", q), go), h("div.row", ["Which unit had the highest maintenance cost?", "What was the net profit trend over the last five years?", "Show me the company history"].map((s) => h("button.btn.small.ghost", { onclick: () => { q.value = s; go.click(); } }, s))), out);
          if (params.question) go.click();
        },
        "Industrial knowledge base": () => {
          const q = h("input.field", { placeholder: "Ask the industrial knowledge base (manuals, SOPs, reports)…" });
          const out = h("div.stack");
          const go = M.button("Ask", "primary", async () => {
            if (!q.value.trim()) return;
            out.replaceChildren();
            await M.section(out, "retrieving evidence and asking the local model", async () => {
              const r = await M.tool("ask_industrial_kb", { query: q.value.trim() });
              out.appendChild(h("div.card", M.answer(r.answer || r.response || JSON.stringify(r))));
              if ((r.injection_flags || []).length) out.appendChild(M.callout(`Instructions were found inside ${r.injection_flags.length} retrieved passage(s). They were treated as document text and not followed.`, "warn"));
              if ((r.evidence || []).length) out.appendChild(M.table(["id", "document", "page", "claim_type", "how_read"], r.evidence));
            });
          });
          q.addEventListener("keydown", (e) => { if (e.key === "Enter") go.click(); });
          pane.replaceChildren(h("div.row", h("div.grow", q), go), out);
        },
      };
      const show = (name) => { tabs.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.textContent === name)); views[name](); };
      Object.keys(views).forEach((name) => tabs.appendChild(h("button.tab", { onclick: () => show(name) }, name)));
      body.append(tabs, pane);
      show("Company records");
    },
  });

  /* ------------------------------------------------------- document explorer ---- */
  M.screen("documents", {
    group: "KNOWLEDGE", label: "Document Explorer", icon: "documents", title: "Document Explorer",
    sub: "Every document you may see, with its year, type, authority and origin. Nothing outside your role is listed - not even its name.",
    async render(body) {
      const tabs = h("div.tabs");
      const pane = h("div.stack");
      const views = {
        "Company records": async () => {
          const q = h("input.field", { placeholder: "Search text" });
          const from = h("input.field", { type: "number", placeholder: "from year", style: "width:120px" });
          const to = h("input.field", { type: "number", placeholder: "to year", style: "width:120px" });
          const type = h("input.field", { placeholder: "type e.g. annual_report", style: "width:200px" });
          const out = h("div.stack");
          const run = async () => {
            out.replaceChildren();
            await M.section(out, "listing", async () => {
              const r = await M.tool("search_company_documents", { query: q.value, year_from: from.value, year_to: to.value, document_type: type.value });
              out.appendChild(h("p.note", `${r.total} document(s) visible to you`));
              out.appendChild(M.table([{ label: "Year", key: "year" }, { label: "Title", key: "title" }, { label: "Type", key: "document_type" },
                { label: "Authority", key: "authority_label" }, { label: "Origin", render: (d) => M.chip(d.origin, ORIGIN_TONE[d.origin]) },
                { label: "Class", key: "confidentiality" }], r.documents, { empty: "No documents match.", onRow: (d) => provenanceSheet(d.document_id, d.title) }));
            });
          };
          pane.replaceChildren(h("div.row", h("div.grow", q), from, to, type, M.button("Search", "primary", run)), out);
          run();
        },
        "Registers": async () => {
          /* Workbooks whose rows are records - each checked against today's date. */
          pane.replaceChildren();
          await M.section(pane, "checking the registers", async () => {
            const r = await M.tool("company_registers");
            if (!r.registers || !r.registers.length) {
              pane.appendChild(M.empty("No registers you can see. Workbooks whose rows are records (leases, employees, filings, contracts) appear here once ingested."));
              return;
            }
            pane.appendChild(h("p.note", r.note));
            r.registers.forEach((g) => {
              const q = g.quality || {};
              const ask = (text) => { M.go("knowledge", { question: text }); };
              pane.appendChild(h("div.card",
                h("div.card-head", h("h3", g.register), h("div.row", M.chip(g.origin, ORIGIN_TONE[g.origin]), M.chip(`${g.records} records`, "plain"),
                  M.chip(q.findings ? `${q.findings} data-quality findings` : "no data-quality findings", q.findings ? "warn" : "ok"))),
                h("p.dim", `${g.document}${g.key ? ` · key column ${g.key}` : ""} · ${g.confidentiality}`),
                h("div.row", g.columns.map((c) => M.chip(`${c.name}${c.role ? " · " + c.role.replace("_", " ") : ""}${c.personal ? " · personal" : ""}`, c.personal ? "warn" : "plain"))),
                (q.examples || []).length ? h("ul.note", q.examples.map((x) => h("li", x))) : null,
                h("div.row", h("span.note", `checked against ${q.checked_against}`), h("span.spacer"),
                  M.button(`List all ${g.register} records`, "small", () => ask(`List all ${g.register.replace(/_/g, " ")} records`)))));
            });
          });
        },
        "Industrial & personal": async () => {
          pane.replaceChildren();
          await M.section(pane, "listing", async () => {
            const docs = await M.tool("list_industrial_documents");
            pane.appendChild(h("h3", "Industrial knowledge base"));
            pane.appendChild(Array.isArray(docs.documents) ? M.table(Object.keys(docs.documents[0] || { title: "" }).slice(0, 5), docs.documents) : h("p.note", String(docs.documents)));
            const legacy = await M.tool("list_knowledge_sources");
            pane.appendChild(h("h3", "Indexed personal documents"));
            pane.appendChild(Array.isArray(legacy.sources) ? M.table(Object.keys(legacy.sources[0] || { source: "" }).slice(0, 4), legacy.sources) : h("p.note", String(legacy.sources)));
            const stats = await M.tool("knowledge_stats").catch(() => null);
            if (stats) pane.appendChild(M.pre(stats));
          });
        },
        Add: async () => {
          const origin = h("select.field", { style: "width:220px" }, ["company", "sample", "demo", "general_knowledge"].map((o) => h("option", o)));
          const cls = h("select.field", { style: "width:180px" }, ["", "public", "internal", "confidential", "restricted"].map((o) => h("option", { value: o }, o || "classification: default")));
          const dept = h("input.field", { placeholder: "Department (optional)", style: "width:200px" });
          const out = h("div.stack");
          const show = (r) => out.prepend(r.results ? h("div.card", h("p", `${r.files} file(s): ${r.ingested} ingested, ${r.updated} updated, ${r.skipped} skipped, ${r.failed} failed`),
            M.table(["status", "title", "detail"], r.results)) : M.pre(r));
          pane.replaceChildren(
            M.callout("Company documents: origin 'company'. Sample or demo material is labelled as such everywhere it appears. Years are read from folder names (2019\\), file names and the text. Vault files are never indexed.", "info"),
            h("div.row", origin, cls, dept),
            h("div.row",
              M.button("Add a file…", "primary", async () => { const p = await M.call("pick_file", { purpose: "document" }); if (p.path) show(await M.tool("ingest_company_document", { path: p.path, origin: origin.value, confidentiality: cls.value, department: dept.value })); }),
              M.button("Add a folder…", "", async () => { const p = await M.call("pick_folder", { purpose: "ingest" }); if (p.path) show(await M.tool("ingest_company_directory", { path: p.path, origin: origin.value, confidentiality: cls.value, department: dept.value })); }),
              M.button("Industrial KB: add a file…", "", async () => { const p = await M.call("pick_file", { purpose: "document" }); if (p.path) show(await M.tool("ingest_industrial_document", { path: p.path })); }),
              M.button("Index the industrial corpus", "", async () => show(await M.tool("ingest_industrial_knowledge_corpus", {})))),
            out);
        },
        Health: async () => {
          pane.replaceChildren();
          await M.section(pane, "checking", async () => {
            const s = await M.tool("knowledge_fabric_status");
            pane.appendChild(h("div.grid.four", [["Passages", s.passages], ["Table facts", s.table_facts], ["With vectors", s.passages_with_vectors], ["Documents", Object.values(s.documents_by_origin || {}).reduce((a, b) => a + b, 0)]].map(([l, v]) =>
              h("div.card.metric", h("div.eyebrow", l), h("div.value", M.num(v, 0))))));
            pane.appendChild(h("div.row", Object.entries(s.documents_by_origin || {}).map(([o, n]) => M.chip(`${o}: ${n}`, ORIGIN_TONE[o]))));
            pane.appendChild(M.callout(`Vector search: ${s.vector_search}`, s.passages_with_vectors ? "info" : "warn"));
            const amb = await M.tool("ambiguous_entities").catch(() => ({ entities: [] }));
            pane.appendChild(h("h3", "Entities waiting for a person"));
            pane.appendChild(M.table(["entity_id", "canonical_name", "type"], amb.entities || [], { empty: "None - no conflicting names found." }));
          });
        },
      };
      const show = (name) => { tabs.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.textContent === name)); views[name](); };
      Object.keys(views).forEach((name) => tabs.appendChild(h("button.tab", { onclick: () => show(name) }, name)));
      body.append(tabs, pane);
      show("Company records");
    },
  });

  /* --------------------------------------------------------- evidence viewer ---- */
  M.screen("evidence", {
    group: "KNOWLEDGE", label: "Evidence Viewer", icon: "evidence", title: "Evidence Viewer",
    sub: "The passages behind the last answer, with their document, section, table cells and entities.",
    async render(body) {
      const a = M.lastAnalysis;
      const lookup = h("input.field", { placeholder: "A passage id (node id), e.g. from a citation" });
      body.appendChild(h("div.row", h("div.grow", lookup), M.button("Open", "primary", () => lookup.value.trim() && M.showEvidence(lookup.value.trim()))));
      if (!a || !(a.evidence || []).length) { body.appendChild(M.empty("Ask the records first - the evidence of the last answer appears here.")); return; }
      body.appendChild(h("p.note", `From analysis ${a.analysis_id}`));
      body.appendChild(evidenceCards(a.evidence));
    },
  });

  /* ------------------------------------------------------- reasoning workspace ---- */
  M.screen("reasoning", {
    group: "WORKBENCH", label: "Reasoning Workspace", icon: "reasoning", title: "AI Reasoning Workspace",
    sub: "What the analysis found and how: figures, calculations with their inputs, drivers labelled by certainty, options, the plan, KPIs and risks. The reasoning is summarised, not replayed.",
    async render(body, params, actions) {
      const list = await M.tool("list_analyses").catch(() => ({ analyses: [] }));
      const select = h("select.field", { style: "width:360px" }, (list.analyses || []).map((x) => h("option", { value: x.analysis_id, selected: x.analysis_id === params.analysis }, `${x.analysis_id} · ${x.question.slice(0, 60)}`)));
      actions.append(select, M.button("Load", "small", () => M.go("reasoning", { analysis: select.value })));
      if (!(list.analyses || []).length) { body.appendChild(M.empty("No analyses yet - ask the records a question.")); return; }
      const a = await M.tool("get_analysis", { analysis_id: params.analysis || select.value });
      if (!a.ok) { body.appendChild(M.callout(a.error, "fail")); return; }
      body.appendChild(h("div.card", h("div.row.between", h("h3", a.question), h("div.row", stateChip(a.verification_state, a.validation && a.validation.ok), M.chip((a.plan.intents || []).join(" · "), "plain"))),
        h("p.note", `Period: ${a.plan.scope && a.plan.scope.window ? a.plan.scope.window.join("-") : "all years in the records"} · ${a.evidence.length} passages · ${a.facts.length} table facts · ${a.calculations.length} calculations`)));
      if (a.gaps && a.gaps.origin_warning) body.appendChild(M.callout(a.gaps.origin_warning, "sample"));
      if ((a.series || []).length) body.appendChild(h("div.card", h("div.card-head", h("h3", "Figures from the records")), M.lineChart(a.series.slice(0, 5).map((s) => ({ label: `${s.label} - ${s.metric}${s.unit ? " (" + s.unit + ")" : ""}`, points: s.points }))), h("p.note", "Values as reported; a year with two different figures shows the more authoritative, later one - the other is listed under conflicts.")));
      const calcs = (a.calculations || []);
      if (calcs.length) body.appendChild(h("div.card", h("div.card-head", h("h3", "Calculations"), h("span.note", "computed by the analysis engine, never by the model")),
        M.table([{ label: "Id", render: (c) => h("span.cite.c", c.id) }, { label: "Calculation", key: "name" },
          { label: "Result", render: (c) => c.value == null ? h("span.muted", (c.notes || [])[0] || "not calculated") : (typeof c.value === "object" ? JSON.stringify(c.value).slice(0, 120) : `${M.num(c.value)} ${c.unit || ""}`) },
          { label: "Method", key: "method" }, { label: "Inputs", render: (c) => `${(c.inputs || []).length}` }], calcs,
          { onRow: (c) => M.openSheet(c.name, h("div.stack", M.kv(c, [["value", "Result"], ["unit", "Unit"], ["method", "Method"]]), h("div.eyebrow", "inputs"),
            M.table(["year", "value", "unit", "title", "cell"], c.inputs || []), (c.notes || []).length ? M.callout(c.notes.join(" · "), "info") : null)) })));
      if ((a.drivers || []).length) body.appendChild(h("div.card", h("div.card-head", h("h3", "Possible drivers")),
        h("div.deck", a.drivers.map((d) => h("div.card.raised", h("div.row.between", h("b", d.driver), M.chip(d.kind === "evidenced" ? "stated in the records" : d.kind === "correlated" ? "correlation - not proof" : "hypothesis", d.kind === "evidenced" ? "ok" : d.kind === "correlated" ? "info" : "warn")),
          h("p.dim", d.statement), h("div.row", (d.evidence_ids || []).map((id) => h("span.cite", id)), d.calculation_id ? h("span.cite.c", d.calculation_id) : null))))));
      if ((a.contradictions || []).length) body.appendChild(h("div.card", h("div.card-head", h("h3", "Conflicts between sources")),
        h("div.deck", a.contradictions.map((c) => (c.type === "numeric" || c.type === "reconciliation") && c.source_a && c.source_b
          ? h("div.card.raised", h("div.row.between", h("b", `${c.metric} ${c.period}`), M.chip(c.type === "reconciliation" ? "reconciliation" : "conflict", c.type === "reconciliation" ? "info" : "warn")),
              M.kv({ "SOURCE A": `${c.source_a.title}: ${M.num(c.source_a.value)} ${c.source_a.unit || ""}`, "SOURCE B": `${c.source_b.title}${c.source_b.derived ? " (summed rows)" : ""}: ${M.num(c.source_b.value)} ${c.source_b.unit || ""}`, [c.type === "reconciliation" ? "RECONCILIATION" : "CONFLICT"]: (c.explanations || []).join("; "), PREFERRED: c.preference_reason }))
          : h("div.card.raised", h("div.row.between", h("b", c.type === "reconciliation_summary" ? `${c.metric}: ${c.count} periods do not reconcile`
              : c.type === "record" ? `${c.register || "register"} ${c.record_key || ""}`
              : c.type === "statement" ? `${c.metric} ${c.period}: a statement and a figure disagree` : `Different causes stated for ${c.subject || "?"}`),
              M.chip(c.type === "reconciliation_summary" ? "reconciliation" : c.type === "record" ? "record check" : c.type === "statement" ? "disagreement" : "statements",
                c.type === "statement" ? "warn" : "info")),
            h("p.dim", c.line || (c.stated || []).map((s) => s.cause).join(" · ")))))));
      if ((a.recommendations || []).length) body.appendChild(h("div.card", h("div.card-head", h("h3", "Recommended actions"), h("span.note", "practice library matched to the evidence - impact to be quantified from company data")),
        h("div.deck", a.recommendations.map((r) => h("div.card.raised", h("div.row.between", h("b", `${r.id}. ${r.action}`), M.chip(`confidence ${r.confidence}`, r.confidence === "high" ? "ok" : r.confidence === "medium" ? "info" : "warn")),
          M.kv(r, [["owner", "Owner"], ["timeline", "Timeline"], ["dependencies", "Dependencies"], ["cost_category", "Cost"], ["expected_impact", "Expected impact"], ["risk", "Risk"], ["kpi", "KPI"], ["data_required", "Data required"], ["success_condition", "Success condition"], ["monitoring_frequency", "Monitoring"]]))))));
      if ((a.implementation_plan || []).length) body.appendChild(h("div.card", h("div.card-head", h("h3", "Implementation plan")),
        h("div.grid.three", a.implementation_plan.map((p) => h("div.card.raised", h("div.eyebrow", p.window), h("h3", p.phase), h("div.stack", { style: "margin-top:8px" }, p.actions.map((x) => h("div.dim", `${x.id} · ${x.action} (${x.owner})`))))))));
      if ((a.kpis || []).length) body.appendChild(h("div.card", h("div.card-head", h("h3", "KPIs")), M.table(["kpi", "owner", "linked_to", { label: "Baseline", render: (k) => typeof k.baseline === "object" && k.baseline ? `${M.num(k.baseline.value)} ${k.baseline.unit || ""} (${k.baseline.year})` : k.baseline }, "monitoring_frequency"], a.kpis)));
      if ((a.risks || []).length) body.appendChild(h("div.card", h("div.card-head", h("h3", "Risks")), M.table(["risk", "linked_to", "mitigation"], a.risks)));
      if ((a.assumptions || []).length) body.appendChild(h("div.card", h("h3", "Assumptions and limits"), h("ul.dim", a.assumptions.map((x) => h("li", x)))));
      body.appendChild(h("div.card", h("div.card-head", h("h3", "Create from this analysis")), artifactButtons(a.analysis_id)));
    },
  });
})();
