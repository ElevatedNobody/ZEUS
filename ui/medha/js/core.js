/* ZEUS core: the bridge, DOM helpers, formatting, shared renderers.
   The page talks to ONE object ("agni") over QWebChannel - typed requests
   (ui/bridge_contract.py) and pushed events. No fetch, no XHR, no socket. */
(function () {
  "use strict";
  const M = (window.ZEUS = window.ZEUS || {});

  /* ------------------------------------------------------------ bridge ---- */
  const pending = new Map();
  const listeners = new Map();
  let nextId = 1;
  let backend = null;
  let readyResolve;
  M.ready = new Promise((resolve) => { readyResolve = resolve; });

  function connect() {
    if (typeof qt === "undefined" || !qt.webChannelTransport || typeof QWebChannel === "undefined") {
      readyResolve(null);                     // opened outside ZEUS - said so on screen
      return;
    }
    new QWebChannel(qt.webChannelTransport, (channel) => {
      backend = channel.objects.agni;
      backend.reply.connect((id, json) => {
        const entry = pending.get(id);
        if (!entry) return;
        pending.delete(id);
        let body;
        try { body = JSON.parse(json); } catch (e) { entry.reject(new Error("unreadable reply")); return; }
        if (body.ok) entry.resolve(body.result); else entry.reject(new Error(body.error || "failed"));
      });
      backend.event.connect((kind, json) => {
        let payload;
        try { payload = JSON.parse(json); } catch (e) { payload = json; }
        (listeners.get(kind) || []).forEach((fn) => { try { fn(payload); } catch (err) { console.error(err); } });
        (listeners.get("*") || []).forEach((fn) => { try { fn(kind, payload); } catch (err) { /* ignore */ } });
      });
      readyResolve(backend);
    });
  }

  M.call = function (method, args, timeoutMs) {
    return M.ready.then((b) => new Promise((resolve, reject) => {
      if (!b) { reject(new Error("ZEUS backend not connected")); return; }
      const id = nextId++;
      pending.set(id, { resolve, reject });
      b.request(id, method, JSON.stringify(args || {}));
      setTimeout(() => {
        if (pending.has(id)) { pending.delete(id); reject(new Error("timed out waiting for the backend")); }
      }, timeoutMs || 15 * 60 * 1000);
    }));
  };
  M.on = function (kind, fn) {
    if (!listeners.has(kind)) listeners.set(kind, []);
    listeners.get(kind).push(fn);
    return () => listeners.set(kind, (listeners.get(kind) || []).filter((f) => f !== fn));
  };
  /* A registered tool, through the policy engine and the audit chain. A refusal
     comes back as an error with the policy's reason - shown, never hidden. */
  M.tool = async function (name, args) {
    const r = await M.call("tool", { name: name, args: args || {} });
    if (!r.success) throw new Error(r.error || "not permitted");
    if (r.output && typeof r.output === "object" && !Array.isArray(r.output) && r.output.error && Object.keys(r.output).length <= 2)
      throw new Error(r.output.error);
    return r.output;
  };
  document.addEventListener("DOMContentLoaded", connect);

  /* --------------------------------------------------------------- DOM ---- */
  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  M.esc = (value) => String(value == null ? "" : value).replace(/[&<>"']/g, (c) => ESC[c]);
  M.html = function (markup) {
    const t = document.createElement("template");
    t.innerHTML = String(markup).trim();
    return t.content.firstElementChild;
  };
  /* h("div.card", {onclick}, children...) */
  M.h = function (spec, attrs, ...children) {
    /* "tag#id.class.class" - tag defaults to div */
    const tag = (spec.match(/^[a-z0-9-]*/i) || [""])[0] || "div";
    const node = document.createElement(tag);
    const id = spec.match(/#([\w-]+)/);
    if (id) node.id = id[1];
    const classes = [...spec.matchAll(/\.([\w-]+)/g)].map((m) => m[1]);
    if (classes.length) node.className = classes.join(" ");
    if (attrs && (typeof attrs !== "object" || attrs instanceof Node || Array.isArray(attrs))) { children.unshift(attrs); attrs = null; }
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (v == null || v === false) return;
      if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else if (k === "html") node.innerHTML = v;
      else if (k === "text") node.textContent = v;
      else node.setAttribute(k, v === true ? "" : v);
    });
    children.flat(3).forEach((c) => {
      if (c == null || c === false) return;
      node.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
    });
    return node;
  };
  M.$ = (sel, root) => (root || document).querySelector(sel);

  /* ------------------------------------------------------------ format ---- */
  M.num = function (value, digits) {
    if (value == null || value === "" || isNaN(Number(value))) return "—";
    const d = digits == null ? (Math.abs(value) >= 100 ? 0 : 2) : digits;
    return Number(value).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: 0 });
  };
  M.time = function (ts) {
    if (!ts) return "—";
    const d = typeof ts === "number" ? new Date(ts > 1e12 ? ts : ts * 1000) : new Date(ts);
    if (isNaN(d.getTime())) return String(ts);
    return d.toLocaleString([], { year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  };
  M.basename = (p) => String(p || "").split(/[\\/]/).pop();

  /* ------------------------------------------------------- renderers ---- */
  const TONE = { PASS: "ok", OK: "ok", VERIFIED: "ok", READY: "ok", ACTIVE: "ok", AVAILABLE: "ok", INSTALLED: "ok",
    WARN: "warn", FALLBACK: "warn", DEGRADED: "warn", "NOT VERIFIED": "warn", LOCKED: "warn", PARTIAL: "warn",
    FAIL: "fail", FAILED: "fail", BROKEN: "fail", ERROR: "fail", ENGAGED: "fail", MISSING: "fail", REFUSED: "fail",
    SAMPLE: "sample", DEMO: "sample", "DEMO DATA": "sample", GENERAL: "info", HYBRID: "info", VALIDATED: "info", INSUFFICIENT_DATA: "warn", RUNNING: "live", LIVE: "live" };
  M.chip = function (text, tone) {
    const t = tone || TONE[String(text || "").toUpperCase()] || "";
    return M.h("span.chip" + (t ? "." + t : ""), String(text == null ? "—" : text));
  };
  M.loading = (label) => M.h("div.loading", M.h("div.bar"), label || "loading");
  M.empty = (text) => M.h("div.empty", text);
  M.callout = (text, tone) => M.h("div.callout." + (tone || "info"), text);
  M.pre = (value) => M.h("pre.block", typeof value === "string" ? value : JSON.stringify(value, null, 2));

  M.kv = function (obj, fields) {
    const dl = M.h("dl.kv");
    const entries = fields ? fields.map(([k, label]) => [label, obj ? obj[k] : undefined])
      : Object.entries(obj || {}).filter(([, v]) => v == null || typeof v !== "object");
    entries.forEach(([label, value]) => {
      dl.appendChild(M.h("dt", String(label).replace(/_/g, " ")));
      dl.appendChild(M.h("dd", value == null || value === "" ? "—" : (value instanceof Node ? value : String(value))));
    });
    return dl;
  };

  /* columns: ["key", ...] or [{label, key, render(row)}] */
  M.table = function (columns, rows, options) {
    options = options || {};
    rows = Array.isArray(rows) ? rows : [];
    if (!rows.length) return M.empty(options.empty || "Nothing to show.");
    const cols = columns.map((c) => (typeof c === "string" ? { label: c.replace(/_/g, " "), key: c } : c));
    const table = M.h("table.data");
    table.appendChild(M.h("thead", M.h("tr", cols.map((c) => M.h("th", c.label)))));
    const body = M.h("tbody");
    rows.slice(0, options.limit || 500).forEach((row) => {
      const tr = M.h("tr" + (options.onRow ? ".clickable" : ""));
      cols.forEach((c) => {
        const value = c.render ? c.render(row) : row[c.key];
        tr.appendChild(M.h("td", value instanceof Node ? value : (value == null || value === "" ? "—" : String(value))));
      });
      if (options.onRow) tr.addEventListener("click", () => options.onRow(row));
      body.appendChild(tr);
    });
    table.appendChild(body);
    return M.h("div.table-wrap", table);
  };

  /* A small markdown subset, escaped first; citation ids become chips that
     open the evidence they point to. */
  M.answer = function (text, onCite) {
    /* Maths (js/math.js) is lifted out first so Markdown never touches it, then put
       back as HTML built from the escaped text - no raw \frac or \[ ever shows. */
    const maths = [];
    const escaped = M.esc(text || "");
    const lines = (M.math ? M.math.extract(escaped, maths) : escaped).split(/\r?\n/);
    const out = [];
    let list = null;
    const inline = (s) => s
      .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
      .replace(/(^|[\s(])_([^_]{3,}?)_(?=[\s.,;:)!?]|$)/g, "$1<i>$2</i>")   // not inside Monthly_Rent_INR
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\[\s*([EFCGM]\d+(?:\s*[,;]\s*[EFCGM]?\d+)*)\s*\]/g, (m, group) => {
        let kind = "";
        return group.split(/\s*[,;]\s*/).map((item) => {
          if (/^[A-Z]/.test(item)) kind = item[0]; else item = kind + item;
          return `<span class="cite ${item[0] === "C" ? "c" : item[0] === "G" ? "g" : ""}" data-cite="${item}">${item}</span>`;
        }).join("");
      })
      .replace(/(^|[\s(])([EFCGM]\d{1,3})(?=[\s.,;:)]|$)/g, (m, lead, id) => `${lead}<span class="cite ${id[0] === "C" ? "c" : id[0] === "G" ? "g" : ""}" data-cite="${id}">${id}</span>`);
    /* Pipe tables (register answers): header row, separator, rows - each cell
       through the same inline renderer, so an [E3] in a cell is a chip too. */
    let table = null;
    const cells = (row) => row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
    const flushTable = () => {
      if (!table) return;
      const [head, ...body] = table.filter((r) => !/^\s*\|?\s*:?-{2,}/.test(r));
      out.push(`<div class="table-wrap answer-table"><table class="data"><thead><tr>${cells(head).map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead>`
        + `<tbody>${body.map((r) => `<tr>${cells(r).map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
      table = null;
    };
    lines.forEach((line) => {
      if (/^\s*\|.*\|\s*$/.test(line)) {
        if (list) { out.push(`<ul>${list.join("")}</ul>`); list = null; }
        (table = table || []).push(line);
        return;
      }
      flushTable();
      const bullet = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)/);
      if (bullet) { if (!list) { list = []; } list.push(`<li>${inline(bullet[1])}</li>`); return; }
      if (list) { out.push(`<ul>${list.join("")}</ul>`); list = null; }
      const block = line.match(/^\s*\u0001(\d+)\u0001\s*$/);
      if (block && maths[+block[1]] && maths[+block[1]].display) out.push(`\u0001${block[1]}\u0001`);
      else if (/^###\s+/.test(line)) out.push(`<h4>${inline(line.replace(/^###\s+/, ""))}</h4>`);
      else if (/^&gt;\s?/.test(line)) out.push(`<blockquote>${inline(line.replace(/^&gt;\s?/, ""))}</blockquote>`);
      else if (/^---+$/.test(line.trim())) out.push("<hr>");
      else if (line.trim()) out.push(`<p>${inline(line)}</p>`);
    });
    flushTable();
    if (list) out.push(`<ul>${list.join("")}</ul>`);
    const node = M.h("div.answer", { html: out.join("").replace(/\u0001(\d+)\u0001/g, (m, k) => (maths[+k] ? maths[+k].html : "")) });
    if (onCite) node.addEventListener("click", (e) => { const c = e.target.closest(".cite"); if (c) onCite(c.dataset.cite); });
    return node;
  };

  M.spark = function (values, color, height) {
    const v = (values || []).map(Number).filter((x) => !isNaN(x));
    if (v.length < 2) return M.h("div.note", "not enough points");
    const min = Math.min(...v), max = Math.max(...v), span = max - min || 1, w = 100, hgt = height || 30;
    const pts = v.map((x, i) => `${(i / (v.length - 1)) * w},${hgt - ((x - min) / span) * (hgt - 4) - 2}`).join(" ");
    const stroke = color || "var(--process)", id = `spk${++sparkId}`;
    return M.html(`<svg class="spark" viewBox="0 0 ${w} ${hgt}" preserveAspectRatio="none">
      <defs><linearGradient id="${id}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${stroke}" stop-opacity="0.35"/><stop offset="1" stop-color="${stroke}" stop-opacity="0"/></linearGradient></defs>
      <polygon points="0,${hgt} ${pts} ${w},${hgt}" fill="url(#${id})"/>
      <polyline points="${pts}" fill="none" stroke="${stroke}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" vector-effect="non-scaling-stroke"/></svg>`);
  };
  let sparkId = 0;

  /* The ZEUS mark: an oil drop in a spectrum ring. */
  M.badge = () => M.html(`<div class="badge"><div><svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 2.69l5.66 5.66a8 8 0 1 1-11.31 0z"/></svg></div></div>`);

  /* Line chart for a few series over years: [{label, points:[{year, value}]}] */
  M.lineChart = function (series, options) {
    options = options || {};
    const all = series.flatMap((s) => s.points || []);
    if (all.length < 2) return M.empty("Not enough figures to draw.");
    const years = [...new Set(all.map((p) => p.year))].sort((a, b) => a - b);
    const values = all.map((p) => Number(p.value));
    const min = Math.min(0, ...values), max = Math.max(...values), span = max - min || 1;
    const W = 1200, H = options.height || 300, L = 56, B = 28, T = 12, R = 14;
    const x = (year) => L + ((years.indexOf(year)) / Math.max(1, years.length - 1)) * (W - L - R);
    const y = (value) => T + (1 - (value - min) / span) * (H - T - B);
    const colors = ["var(--flare)", "var(--process)", "var(--sample)", "var(--ok)", "var(--warn)"];
    let svg = `<svg class="chart" viewBox="0 0 ${W} ${H}">`;
    [0, 0.25, 0.5, 0.75, 1].forEach((f) => {
      const v = min + f * span, yy = y(v);
      svg += `<line x1="${L}" x2="${W - R}" y1="${yy}" y2="${yy}" stroke="var(--steel-2)"/><text x="${L - 6}" y="${yy + 3}" text-anchor="end" fill="var(--ink-3)" font-size="10" font-family="JetBrains Mono, Cascadia Mono, Consolas">${M.esc(M.num(v))}</text>`;
    });
    if (min < 0) svg += `<line x1="${L}" x2="${W - R}" y1="${y(0)}" y2="${y(0)}" stroke="var(--ink-3)" stroke-dasharray="3 3"/>`;
    years.forEach((yr, i) => { if (years.length <= 14 || i % Math.ceil(years.length / 12) === 0) svg += `<text x="${x(yr)}" y="${H - 6}" text-anchor="middle" fill="var(--ink-3)" font-size="10" font-family="JetBrains Mono, Cascadia Mono, Consolas">${yr}</text>`; });
    series.forEach((s, i) => {
      const pts = (s.points || []).slice().sort((a, b) => a.year - b.year);
      const color = colors[i % colors.length];
      svg += `<polyline points="${pts.map((p) => `${x(p.year)},${y(Number(p.value))}`).join(" ")}" fill="none" stroke="${color}" stroke-width="2.2" stroke-linejoin="round"/>`;
      pts.forEach((p) => { svg += `<circle cx="${x(p.year)}" cy="${y(Number(p.value))}" r="3.2" fill="var(--crude)" stroke="${color}" stroke-width="2"><title>${M.esc(s.label)} ${p.year}: ${M.esc(M.num(p.value))}</title></circle>`; });
    });
    svg += "</svg>";
    const legend = M.h("div.legend", series.map((s, i) => M.h("span", M.h("i", { style: `background:${colors[i % colors.length]}` }), s.label)));
    return M.h("div.stack", M.html(svg), legend);
  };

  /* ------------------------------------------------------------ toasts ---- */
  M.toast = function (text, tone) {
    let host = M.$(".toasts");
    if (!host) { host = M.h("div.toasts"); document.body.appendChild(host); }
    const t = M.h("div.toast" + (tone ? "." + tone : ""), text);
    host.appendChild(t);
    setTimeout(() => { t.style.transition = "opacity 240ms"; t.style.opacity = "0"; setTimeout(() => t.remove(), 260); }, tone === "fail" ? 7000 : 4200);
  };

  /* A modal form: fields [{name, label, type, value, options}] -> values or null */
  M.form = function (title, fields, options) {
    options = options || {};
    return new Promise((resolve) => {
      const inputs = {};
      const body = M.h("div.stack", fields.map((f) => {
        let input;
        if (f.type === "select") input = M.h("select.field", (f.options || []).map((o) => M.h("option", { value: o, selected: o === f.value }, o)));
        else if (f.type === "textarea") input = M.h("textarea.field", { placeholder: f.placeholder || "" }, f.value || "");
        else input = M.h("input.field", { type: f.type || "text", placeholder: f.placeholder || "", value: f.value || "", autocomplete: "off" });
        inputs[f.name] = input;
        return M.h("div", M.h("label.label.eyebrow", f.label), input);
      }));
      const close = (value) => { scrim.remove(); resolve(value); };
      const submit = () => { const values = {}; Object.entries(inputs).forEach(([k, i]) => (values[k] = i.value)); close(values); };
      const scrim = M.h("div.scrim", M.h("div.modal",
        M.h("h3", { style: "margin-bottom:6px" }, title),
        options.note ? M.h("p.note", options.note) : null,
        body,
        M.h("div.actions", M.h("button.btn.ghost", { onclick: () => close(null) }, "Cancel"),
          M.h("button.btn." + (options.danger ? "danger" : "primary"), { onclick: submit }, options.submit || "Continue"))));
      scrim.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.tagName !== "TEXTAREA") submit(); if (e.key === "Escape") close(null); });
      document.body.appendChild(scrim);
      const first = Object.values(inputs)[0];
      if (first) setTimeout(() => first.focus(), 30);
    });
  };
  M.confirm = (title, text, danger) => M.form(title, [], { note: text, submit: danger ? "Yes, do it" : "Continue", danger }).then((v) => v !== null);

  /* Run an async action with its button disabled; errors become toasts. */
  M.act = function (button, fn) {
    return async function () {
      const label = button.innerHTML;
      button.disabled = true;
      button.innerHTML = `<span class="spin"></span>${label}`;
      try { await fn(); } catch (e) { M.toast(e.message || String(e), "fail"); } finally { button.disabled = false; button.innerHTML = label; }
    };
  };
  M.button = function (label, cls, fn) {
    const b = M.h("button.btn" + (cls ? "." + cls.split(" ").join(".") : ""), label);
    if (fn) b.addEventListener("click", M.act(b, fn));
    return b;
  };
  /* Load a section: shows progress, then content or the error, in place. */
  M.section = async function (host, label, fn) {
    const wait = M.loading(label);
    host.appendChild(wait);
    try { await fn(); } catch (e) { host.appendChild(M.callout(e.message || String(e), "fail")); } finally { wait.remove(); }
  };
})();
