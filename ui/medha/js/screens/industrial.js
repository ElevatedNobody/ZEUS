/* INDUSTRIAL: the plant records - every panel labelled with its source (DEMO
   data is always marked as sample records, never as live measurements). */
(function () {
  "use strict";
  const M = window.ZEUS;
  const { h } = M;
  const UNIT_ORDER = ["CDU", "VDU", "NHT", "HDT", "CRU", "FCC", "SRU", "UTIL", "OFFSITE", "TANK"];
  const unitRank = (u) => { const i = UNIT_ORDER.findIndex((p) => String(u).toUpperCase().startsWith(p)); return i < 0 ? 99 : i; };
  const sourceChip = (label) => M.chip(label || "—", String(label || "").toUpperCase().includes("DEMO") ? "sample" : "info");
  const demoNote = (isDemo) => isDemo ? M.callout("DEMO DATA - generated sample records (Meridian demo refinery). Not live plant measurements.", "sample") : null;
  const sev = (s) => { const v = String(s || "").toLowerCase(); return M.chip(v || "—", v === "critical" || v === "high" ? "fail" : v === "medium" || v === "warning" ? "warn" : "plain"); };

  async function dossier(host, tag) {
    host.replaceChildren();
    await M.section(host, "reading the equipment record", async () => {
      const d = await M.tool("equipment_details", { equipment: tag });
      const item = d.equipment || {};
      host.appendChild(h("div.row", M.button("← Plant", "small ghost", () => M.go("industrial", { tab: "Plant" })), h("h2", `${item.tag || tag} · ${item.name || ""}`), item.source === "DEMO" ? M.chip("DEMO DATA", "sample") : M.chip("RECORDED", "info")));
      host.appendChild(h("div.card", M.kv(item, [["equipment_type", "Type"], ["unit", "Unit"], ["location", "Location"], ["status", "Status"], ["criticality", "Criticality"], ["manufacturer", "Manufacturer"], ["model", "Model"], ["installed_on", "Installed"]])));
      if (d.summary) host.appendChild(h("div.card", M.answer(d.summary)));
      [["maintenance", "Maintenance"], ["inspections", "Inspections"], ["alerts", "Alerts"], ["readings", "Latest readings"]].forEach(([key, title]) => {
        const rows = d[key] || [];
        if (!Array.isArray(rows) || !rows.length) return;
        const cols = Object.keys(rows[0]).filter((c) => !["id", "created_at", "updated_at", "attributes"].includes(c)).slice(0, 6);
        host.appendChild(h("div.card", h("h3", title), M.table(cols, rows.slice(0, 20))));
      });
      host.appendChild(h("div.row",
        M.button("Run the inspection agent", "primary", () => M.submit(`run the inspection agent on ${item.tag}`)),
        M.button("Approval note (unsigned)", "", async () => { const r = await M.tool("approval_note", { equipment: item.tag }); M.toast(r.saved ? `Written: ${r.saved}` : "Not written", r.saved ? "ok" : "fail"); })));
    });
  }

  function fieldMap(equipment, alerts, onPick) {
    const byUnit = {};
    equipment.forEach((e) => { const u = e.unit || e.location || "Unassigned"; (byUnit[u] = byUnit[u] || []).push(e); });
    const units = Object.keys(byUnit).sort((a, b) => unitRank(a) - unitRank(b) || a.localeCompare(b));
    const hot = new Set(alerts.map((a) => String(a.equipment_tag || "").toUpperCase()));
    const cols = 4, cw = 190, ch = 130, W = cols * (cw + 12) + 12, rows = Math.ceil(units.length / cols);
    let svg = `<svg class="chart" viewBox="0 0 ${W} ${Math.max(150, rows * ch + 16)}">`;
    units.forEach((u, i) => {
      const x = 12 + (i % cols) * (cw + 12), y = 10 + Math.floor(i / cols) * ch;
      const items = byUnit[u];
      const alert = items.some((e) => hot.has(String(e.tag).toUpperCase()));
      svg += `<rect x="${x}" y="${y}" width="${cw}" height="${ch - 14}" rx="14" fill="var(--bitumen)" stroke="${alert ? "var(--fail)" : "var(--steel-3)"}"/>`;
      svg += `<text x="${x + 12}" y="${y + 20}" fill="var(--ink-2)" font-size="11" font-family="JetBrains Mono, Cascadia Mono, Consolas" letter-spacing="1">${M.esc(u).slice(0, 22)}</text>`;
      items.slice(0, 8).forEach((e, j) => {
        const cx = x + 22 + (j % 4) * 44, cy = y + 48 + Math.floor(j / 4) * 38;
        const color = hot.has(String(e.tag).toUpperCase()) ? "var(--fail)" : e.status === "operational" ? "var(--ok)" : e.status === "maintenance" ? "var(--warn)" : "var(--ink-3)";
        svg += `<g class="eq" data-tag="${M.esc(e.tag)}"><circle cx="${cx}" cy="${cy}" r="9" fill="${color}" fill-opacity="0.2" stroke="${color}" stroke-width="1.5"/><text x="${cx}" y="${cy + 21}" text-anchor="middle" fill="var(--ink-2)" font-size="10" font-family="Cascadia Mono, Consolas">${M.esc(e.tag)}</text></g>`;
      });
      if (items.length > 8) svg += `<text x="${x + cw - 12}" y="${y + 20}" text-anchor="end" fill="var(--ink-3)" font-size="10">+${items.length - 8}</text>`;
    });
    const node = h("div.fieldmap", { html: svg + "</svg>" });
    node.querySelectorAll("g.eq").forEach((g) => g.addEventListener("click", () => onPick(g.dataset.tag)));
    return node;
  }

  M.screen("industrial", {
    group: "INDUSTRIAL", label: "Industrial Workspace", icon: "industrial", title: "Industrial workspace",
    sub: "Plant records, alerts and analytics - every panel names its source.",
    async render(body, params) {
      const tabs = h("div.tabs");
      const pane = h("div.stack");
      const plant = () => M.tool("plant_snapshot");
      const views = {
        Overview: async () => {
          const t = await M.tool("plant_telemetry");
          const p = t.production, o = t.operations;
          pane.append(...[t.telemetry.live ? h("div.row", M.chip("LIVE PLANT CONNECTION", "ok"), h("span.note", t.telemetry.note))
            : M.callout(`NO LIVE PLANT CONNECTION - ${t.telemetry.note}${t.telemetry.newest_non_demo_reading ? " Newest non-demo reading: " + t.telemetry.newest_non_demo_reading + "." : ""}`, "warn"),
            demoNote(String(p.label).includes("DEMO")),
            h("div.grid.four",
              h("div.card.metric", h("div.eyebrow", "Crude processed"), h("div.value", p.latest && p.latest.month ? M.num(p.latest.crude_kt, 0) : "—"), h("div.caption", p.latest && p.latest.month ? `kt · ${p.latest.month}` : "no records"), sourceChip(p.label)),
              h("div.card.metric", h("div.eyebrow", "Utilisation"), h("div.value", p.latest && p.latest.month ? M.num(p.latest.utilisation_pct, 1) + "%" : "—"), h("div.caption", p.plant || "")),
              h("div.card.metric", h("div.eyebrow", "Open alerts"), h("div.value", M.num(o.open_alerts, 0)), sourceChip(o.label)),
              h("div.card.metric", h("div.eyebrow", "Maintenance due (14 d)"), h("div.value", M.num(o.maintenance_due_14d, 0)), h("div.caption", `${M.num(o.open_permits, 0)} open permits`))),
            p.months && p.months.length ? h("div.card", h("div.card-head", h("h3", "Crude processed by month"), sourceChip(p.label)), M.spark(p.months.map((m) => m.crude_kt), "var(--flare)", 40)) : null,
            h("div.card", h("div.card-head", h("h3", "Telemetry series"), sourceChip(t.telemetry.label)),
              M.table([{ label: "Equipment", key: "equipment" }, { label: "Parameter", key: "parameter" }, { label: "Latest", render: (r) => `${M.num(r.latest)} ${r.unit}` }, { label: "Trend", key: "direction" }, { label: "Source", render: (r) => sourceChip(r.label) }], t.telemetry.series, { empty: "No readings stored." }),
              h("p.note", t.telemetry.note))].filter(Boolean));
        },
        Plant: async () => {
          const d = await plant();
          pane.appendChild(demoNote(d.sources && d.sources.demo_rows && !d.sources.non_demo_rows) || h("span"));
          if (!d.equipment.length) {
            pane.append(M.empty("No equipment registered. Load the labelled sample refinery to explore the features."),
              h("div.row", M.button("Load DEMO refinery data", "primary", async () => { const r = await M.tool("seed_industrial_demo_data", {}); M.toast(JSON.stringify(r).slice(0, 160), "ok"); M.go("industrial", { tab: "Plant" }); })));
            return;
          }
          pane.appendChild(M.table([{ label: "Tag", key: "tag" }, { label: "Name", key: "name" }, { label: "Type", key: "equipment_type" }, { label: "Unit", key: "unit" }, { label: "Status", key: "status" }, { label: "Criticality", key: "criticality" }, { label: "Source", render: (r) => r.source === "DEMO" ? M.chip("DEMO", "sample") : r.source }],
            d.equipment, { onRow: (r) => dossier(pane, r.tag) }));
        },
        "Field map": async () => {
          const d = await plant();
          pane.append(...[demoNote(d.sources && d.sources.demo_rows), h("p.note", "Built from the equipment register - no map tiles, no network. Red = open alert, green = operational, amber = maintenance. Click an item for its record.")].filter(Boolean));
          pane.appendChild(d.equipment.length ? fieldMap(d.equipment, d.alerts, (tag) => dossier(pane, tag)) : M.empty("No equipment registered."));
        },
        Analytics: async () => {
          const t = await M.tool("plant_telemetry");
          const months = t.production.months || [];
          pane.appendChild(demoNote(String(t.production.label).includes("DEMO")) || h("span"));
          const metrics = [["crude_kt", "Crude processed (kt)", "var(--flare)"], ["utilisation_pct", "Utilisation %", "var(--process)"], ["energy_mbn", "Energy intensity (MBN)", "var(--warn)"], ["downtime_h", "Unplanned downtime (h)", "var(--fail)"], ["on_spec_pct", "On-spec %", "var(--ok)"]];
          pane.appendChild(h("div.grid.two", metrics.map(([key, label, color]) => {
            const values = months.map((m) => m[key]);
            return h("div.card", h("div.row.between", h("span.dim", label), h("b.num", M.num(values[values.length - 1]))), M.spark(values, color, 40),
              h("div.row.between.note", h("span", months[0] ? months[0].month : ""), h("span", values.length ? `min ${M.num(Math.min(...values))} · max ${M.num(Math.max(...values))}` : ""), h("span", months.length ? months[months.length - 1].month : "")));
          })));
          pane.appendChild(h("div.row",
            M.button("Executive report (.docx)", "primary", () => M.submit("generate the executive report on plant operations")),
            M.button("Workbook (.xlsx)", "", () => M.submit("generate the executive workbook on plant operations")),
            M.button("Deck (.pptx)", "", () => M.submit("generate the executive presentation on plant operations"))));
        },
        "Process flow": async () => {
          const d = await plant();
          const units = {};
          d.equipment.forEach((e) => { const u = e.unit || "Other"; units[u] = units[u] || { n: 0, alerts: 0, down: 0 }; units[u].n++; if (e.status !== "operational") units[u].down++; });
          d.alerts.forEach((a) => { const e = d.equipment.find((x) => String(x.tag).toUpperCase() === String(a.equipment_tag).toUpperCase()); if (e) units[e.unit || "Other"].alerts++; });
          const names = Object.keys(units).sort((a, b) => unitRank(a) - unitRank(b));
          if (!names.length) { pane.appendChild(M.empty("No equipment registered.")); return; }
          const flow = h("div.flow");
          names.forEach((u, i) => {
            const s = units[u];
            flow.appendChild(h("div.unit" + (s.alerts ? ".alert" : ""), h("b", u), h("span.dim", `${s.n} equipment · ${s.down} not running · ${s.alerts} alert(s)`)));
            if (i < names.length - 1) flow.appendChild(h("div.arrow", "↓"));
          });
          pane.append(...[demoNote(d.sources && d.sources.demo_rows), flow].filter(Boolean));
        },
        Alerts: async () => {
          const d = await plant();
          pane.appendChild(demoNote(d.alerts.some((a) => a.source === "DEMO")) || h("span"));
          pane.appendChild(h("h3", `Open alerts (${d.alerts.length})`));
          const act = async (call, a, done) => {
            try {
              const r = await call();
              if (r && r.error) { M.toast(r.error, "fail"); return; }
              const al = (r && r.alert) || {};
              M.toast(`${a.alert_id} ${al.status || done}${al.acknowledged_by ? " by " + al.acknowledged_by : ""}`, "ok");
            } catch (e) { M.toast(e.message || String(e), "fail"); return; }
            M.go("industrial", { tab: "Alerts" });
          };
          pane.appendChild(d.alerts.length ? h("div.deck", d.alerts.map((a) => h("div.card.alert-card",
            h("div.row.between", h("div.row", h("b.mono", a.alert_id), a.source === "DEMO" ? M.chip("DEMO", "sample") : M.chip(a.source || "LOCAL", "plain")),
              h("div.row", M.chip(String(a.status || "open").toUpperCase(), a.status === "acknowledged" ? "info" : "warn"), sev(a.severity))),
            h("p", { style: "margin:8px 0" }, a.description),
            M.kv({ equipment: a.equipment_tag || "—", category: a.category || "—", raised: a.raised_at || "—",
              acknowledged: a.acknowledged_by ? `${a.acknowledged_by} at ${a.acknowledged_at || "—"}` : "not yet" }),
            h("div.row", { style: "margin-top:8px" },
              a.status === "acknowledged" ? M.chip("ACKNOWLEDGED", "info") : M.button("Acknowledge", "small", () => act(() => M.tool("acknowledge_alert", { alert_id: a.alert_id }), a, "acknowledged")),
              M.button("Resolve", "small", () => act(() => M.tool("resolve_alert", { alert_id: a.alert_id }), a, "resolved")),
              a.equipment_tag ? M.button("Equipment record", "small ghost", () => dossier(pane, a.equipment_tag)) : null)))) : M.empty("No open alerts."));
          pane.appendChild(h("div.grid.two",
            h("div.card", h("h3", "Permits"), M.table(["permit_id", "permit_type", "equipment_tag", "status"], d.permits.slice(0, 20), { empty: "No permits." })),
            h("div.card", h("h3", "Isolations"), M.table(["loto_id", "equipment_tag", "isolation_status"], d.isolations.slice(0, 20), { empty: "No active isolations." }))));
          pane.appendChild(h("p.note", "Approving a permit or releasing an isolation needs a person's approval - ZEUS never does either by itself."));
        },
      };
      const show = async (name) => {
        tabs.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.textContent === name));
        pane.replaceChildren();
        await M.section(pane, "reading the plant records", views[name]);
      };
      Object.keys(views).forEach((name) => tabs.appendChild(h("button.tab", { onclick: () => show(name) }, name)));
      body.append(tabs, pane);
      show(params.tab && views[params.tab] ? params.tab : "Overview");
    },
  });
})();
