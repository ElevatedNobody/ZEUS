/* SYSTEM & AUTOMATION: memory, models, agents, automation, settings. */
(function () {
  "use strict";
  const M = window.ZEUS;
  const { h } = M;

  function tabbed(body, views, initial) {
    const tabs = h("div.tabs");
    const pane = h("div.stack");
    const show = async (name) => {
      tabs.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.textContent === name));
      // Each render gets its own container: a slow tab that answers after the
      // operator switched away writes into a detached node, never into the next tab.
      const host = h("div.stack");
      pane.replaceChildren(host);
      await M.section(host, "loading", () => views[name](host));
    };
    Object.keys(views).forEach((name) => tabs.appendChild(h("button.tab", { onclick: () => show(name) }, name)));
    body.append(tabs, pane);
    show(initial && views[initial] ? initial : Object.keys(views)[0]);
    return show;
  }
  M.tabbed = tabbed;

  /* --------------------------------------------------------------- memory ---- */
  M.screen("memory", {
    group: "SYSTEM", label: "Hermes Memory", icon: "memory", title: "Hermes memory",
    sub: "What ZEUS remembers about you and your work. Memory is information, never authority - it cannot grant a permission or change a rule. Passwords and secrets are never stored.",
    async render(body) {
      tabbed(body, {
        Memories: async (pane) => {
          const fact = h("input.field", { placeholder: "Something to remember, e.g. 'the Unit 5 reliability review is due in March'" });
          pane.appendChild(h("div.row", h("div.grow", fact), M.button("Remember", "primary", async () => {
            if (!fact.value.trim()) return;
            const r = await M.tool("remember_fact", { fact: fact.value.trim() });
            M.toast(r.remembered || r.stored || r.message || "Remembered", "ok");
            M.refresh();
          })));
          const r = await M.tool("list_memories");
          const facts = Array.isArray(r.facts) ? r.facts : [];
          if (!facts.length) { pane.appendChild(M.empty(typeof r.facts === "string" ? r.facts : "Nothing remembered yet.")); return; }
          pane.appendChild(h("div.deck", facts.map((f) => {
            const text = typeof f === "string" ? f : (f.text || f.fact || JSON.stringify(f));
            return h("div.card.row", h("span.grow.selectable", text), f.category ? M.chip(f.category, "plain") : null,
              M.button("Forget", "small ghost", async () => { await M.tool("forget_fact", { fact: text }); M.toast("Forgotten", "ok"); M.refresh(); }));
          })));
        },
        "Context console": async (pane) => {
          const r = await M.tool("hermes_context");
          const hl = r.health || {};
          pane.appendChild(h("div.grid.two",
            h("div.card", h("div.card-head", h("h3", "Health"), M.chip(hl.error ? "ERROR" : hl.backend ? "READY" : "UNAVAILABLE", hl.error ? "fail" : hl.backend ? "ok" : "warn")),
              M.kv({ backend: hl.backend || "—", integrity: hl.integrity || "—", "stored memories": String(hl.memories ?? "—"), "this session's turns (working)": String(hl.working_turns ?? 0),
                "by type": Object.entries(hl.by_type || {}).map(([k, v]) => `${k.toLowerCase().replace(/_/g, " ")} ${v}`).join(" · ") || "—" })),
            h("div.card", h("h3", "Authority"), h("p.note", r.health && r.health.authority ? `None. ${r.health.authority.replace(/^none - /, "")}.` : "None."),
              h("p.note", "The packet reaches the model inside a fenced block as data. It never reaches the planner, the tool router or the security policy."))));
          const q = h("input.field", { placeholder: "Preview the context for a request - nothing is sent to a model" });
          const out = h("div.stack");
          pane.appendChild(h("div.card", h("h3", "Preview"), h("div.row", h("div.grow", q), M.button("Build packet", "primary", async () => {
            out.replaceChildren();
            await M.section(out, "building the packet", async () => {
              const p = await M.tool("hermes_context_preview", { question: q.value });
              out.appendChild(h("p.note", p.summary || p.reason || ""));
              renderPacket(out, p.packet, p.model_block);
            });
          })), out));
          const last = h("div.stack");
          pane.appendChild(h("div.card", h("h3", "Active context - the last request"), last));
          if (r.packet) renderPacket(last, r.packet, r.packet.model_block);
          else last.appendChild(M.empty("No request has been made in this session yet."));
        },
      });
    },
  });

  const TYPE_TONE = { DECISION: "ok", REQUIREMENT: "warn", UNRESOLVED: "warn", PREFERENCE: "info", FACT: "plain", ARTIFACT_CONTEXT: "info", PROJECT_CONTEXT: "plain" };
  function renderPacket(host, pkt, block) {
    if (!pkt) { host.appendChild(M.empty("No packet.")); return; }
    host.appendChild(h("div.stack",
      h("div.row", h("span.eyebrow", "Request (ephemeral)"), h("span.selectable", (pkt.ephemeral || {}).request || "")),
      h("div.row", h("span.eyebrow", "Needs"), ...(pkt.needs || []).map((n) => h("span", { title: n.evidence }, M.chip(n.need.replace(/_/g, " "), "info")))),
      (pkt.long_term || []).length ? M.table([{ label: "Type", render: (m) => M.chip(m.type.replace(/_/g, " "), TYPE_TONE[m.type] || "plain") }, { label: "Memory", key: "text" },
        { label: "Relevance", render: (m) => M.num(m.relevance, 2) }, { label: "Recorded", key: "recorded" }], pkt.long_term) : h("p.note", "No stored memory was relevant enough to include."),
      ...[["decisions", "Decisions"], ["unresolved", "Unresolved"], ["requirements", "Requirements"], ["preferences", "Preferences"]].filter(([k]) => (pkt[k] || []).length)
        .map(([k, label]) => h("div", h("div.eyebrow", label), h("ul.dim", pkt[k].map((x) => h("li", x))))),
      (pkt.working || []).length ? h("div", h("div.eyebrow", "This session (working memory, relevant turns)"), h("ul.dim", pkt.working.map((t) => h("li", `#${t.turn} ${t.request}${t.reply ? " → " + t.reply.slice(0, 120) : ""}`)))) : null,
      h("p.note", `recalled ${(pkt.stats || {}).recalled ?? 0} · relevant ${(pkt.stats || {}).relevant_long_term ?? 0} · session turns ${(pkt.stats || {}).working_turns_in_session ?? 0} · backend ${(pkt.stats || {}).backend || "—"}`),
      block ? h("details", h("summary", "Exactly what the model receives"), M.pre(block)) : h("p.note", "Nothing relevant - no memory block is sent with this request.")));
  }

  /* --------------------------------------------------------------- models ---- */
  const POINT_TONE = { PASS: "ok", FAIL: "fail", "NOT RUN": "plain" };
  function renderVerification(pane, r) {
    if (!r || !r.models || !r.models.length) { pane.appendChild(M.empty("No model has been probed yet - run the probe (or MODEL_PROBE.bat).")); return; }
    pane.appendChild(h("p.note", `Eight checks per model, from its last probe: ${(r.points || []).join(" · ")}.`));
    pane.appendChild(h("div.deck", r.models.map((m) => h("div.card.raised",
      h("div.row.between", h("div.row", h("b.mono", m.model), M.chip(m.status, m.status === "PASS" ? "ok" : m.status === "WARN" ? "warn" : "fail"), m.slot ? M.chip(m.slot, "plain") : null),
        h("span.note", m.tested_at ? `tested ${M.time(m.tested_at)}` : "never tested")),
      h("div.points", m.points.map((p) => h("div.point", { title: p.detail }, M.chip(p.status, POINT_TONE[p.status]), h("span", p.point), h("span.note", p.detail)))),
      m.cause ? M.callout(m.cause, "warn") : null, m.fallback ? h("p.note", `fallback: ${m.fallback}`) : null))));
  }
  async function probe(pane) {
    const out = h("div.stack");
    pane.prepend(out);
    await M.section(out, "probing - each chat model is loaded, tested and unloaded one at a time (several minutes)", async () => {
      const r = await M.tool("verify_models", { kind: "chat" });
      out.appendChild(M.callout(`${r.tested} model(s) probed; results saved.`, "ok"));
      renderVerification(out, r);
    });
  }
  M.screen("models", {
    group: "SYSTEM", label: "Model Center", icon: "models", title: "Model Center",
    sub: "This machine, the model plan for it, and which installed, trusted model serves each role. Nothing is downloaded from here.",
    async render(body) {
      tabbed(body, {
        Roles: async (pane) => {
          const r = await M.tool("model_roles", {});
          const tone = (x) => (x.state === "VERIFIED" || x.state === "INSTALLED" ? "ok" : x.state === "HYBRID" ? "info" : x.state === "FALLBACK" ? "warn" : "fail");
          pane.appendChild(M.table([{ label: "Role", key: "role" }, { label: "State", render: (x) => M.chip(x.state, tone(x)) },
            { label: "Model", key: "model" }, { label: "Planned", key: "planned_model" }, { label: "~GB", render: (x) => x.estimated_gb == null ? "—" : M.num(x.estimated_gb, 1) },
            { label: "Fits now", render: (x) => x.fits_now == null ? "—" : x.fits_now ? "yes" : M.chip("no", "warn") }, { label: "Why", render: (x) => x.fallback || x.reason }], r.roles,
            { onRow: (x) => M.openSheet(`${x.role} - ${x.model || "no model"}`, h("div.stack", M.kv(x, [["state", "State"], ["model", "Model"], ["planned_model", "Planned for this hardware"], ["hardware_tier", "Hardware tier"], ["reason", "Why"], ["fallback", "Fallback"], ["install_hint", "How to change it"]]), M.pre(x.evidence))) }));
          pane.appendChild(h("div.row", M.button("Probe every chat model (one at a time)", "", () => probe(pane))));
          pane.appendChild(h("p.note", "INSTALLED is not VERIFIED: only a real call with a validated reply earns it (Verification tab). HYBRID = no cross-encoder is installed; passages are ranked by lexical, dense, entity, temporal, metadata and authority signals. A 'measured choice' is a planned model set aside because a test showed it unsuitable for that role."));
        },
        Verification: async (pane) => {
          const r = await M.tool("model_verification");
          renderVerification(pane, r);
          pane.appendChild(h("div.row", M.button("Probe every chat model (one at a time)", "primary", () => probe(pane))));
        },
        Hardware: async (pane) => {
          const r = await M.tool("hardware_profile");
          const hw = r.hardware, gpu = (hw.gpus || [])[0];
          pane.appendChild(h("div.card", M.kv({ cpu: hw.cpu, cores: `${hw.cores} / ${hw.threads} threads`, ram: `${hw.ram_total_gb} GB (${hw.ram_available_gb} free)`, gpu: gpu ? `${gpu.name} - ${gpu.vram_total_gb} GB VRAM` : "no NVIDIA GPU detected", driver: gpu ? gpu.driver : "—", tier: hw.tier, disk_free: `${hw.disk_free_gb} GB`, os: hw.os })));
          pane.appendChild(h("div.card", h("h3", "Model plan for this hardware"), M.table(["slot", "model"], Object.entries(r.plan.slots).map(([slot, model]) => ({ slot, model }))),
            h("p.note", `Speech: Whisper ${r.plan.whisper.model} on ${r.plan.whisper.device}. Required download ≈ ${r.plan.download_gb} GB. STEP 1 : INSTALL installs only what is missing.`)));
        },
        Routing: async (pane) => {
          const r = await M.tool("model_routing");
          pane.appendChild(h("p.note", `${M.num(r.available_ram_gb)} GB model memory free`));
          pane.appendChild(M.table([{ label: "Slot", key: "slot" }, { label: "Model", key: "model" }, { label: "Installed", render: (s) => s.pulled ? M.chip("yes", "ok") : M.chip("not installed", "warn") }, { label: "~GB", key: "estimated_ram_gb" }], r.slots));
          pane.appendChild(h("p.note", r.policy));
          pane.appendChild(M.button("Release resident models", "", async () => { const x = await M.tool("release_models"); M.toast(`Released ${x.released}`, "ok"); }));
        },
        Capabilities: async (pane) => {
          const c = await M.tool("capability_report");
          pane.appendChild(M.kv({ runtime: c.local_runtime, models_installed: (c.models_pulled || []).length }));
          pane.appendChild(M.table([{ label: "Capability", key: "k" }, { label: "Status", render: (x) => M.chip(x.status || x.health || "—") }, { label: "Model", key: "model" }, { label: "Detail", key: "detail" }], Object.entries(c.capabilities).map(([k, v]) => ({ k, ...v }))));
          pane.appendChild(h("div.card", h("h3", "Voice"), M.kv(c.voice, [["state", "State"], ["speaking_with", "Speaking with"], ["listening_with", "Listening with"], ["detail", "Detail"]])));
        },
        Integrity: async (pane) => {
          const m = await M.tool("model_integrity_report");
          pane.appendChild(h("div.row", M.chip(m.exists && m.signature_valid ? "REGISTRY SIGNED" : "NOT PINNED", m.exists && m.signature_valid ? "ok" : "warn")));
          pane.appendChild(M.table([{ label: "Model", key: "model" }, { label: "Status", render: (r) => M.chip(r.status, r.status === "trusted" ? "ok" : r.status === "tampered" ? "fail" : "warn") }, { label: "Digest", key: "digest" }], m.models));
          pane.appendChild(M.button("Pin current models (admin)", "", async () => { const r = await M.tool("pin_trusted_models"); M.toast(`Pinned ${r.pinned} model(s)`, "ok"); M.refresh(); }));
        },
      });
    },
  });

  /* --------------------------------------------------------------- agents ---- */
  M.screen("agents", {
    group: "SYSTEM", label: "Agent Center", icon: "agents", title: "Agent Center",
    sub: "The specialist agents, what each owns, and the pipeline as it runs.",
    async render(body) {
      tabbed(body, {
        Pipeline: async (pane) => {
          const s = await M.call("status");
          const run = s.pipeline || s.last_run;
          if (!run) { pane.appendChild(M.empty("No agent has run yet in this session.")); return; }
          pane.appendChild(h("div.row", M.chip(s.pipeline ? "RUNNING" : "LAST RUN", s.pipeline ? "live" : "plain")));
          const st = run.stages || {};
          pane.appendChild(h("div.stage-row", Object.keys(st).map((k) => h("span.stage-pill." + (st[k] === "failed" ? "failed" : st[k] === "running" ? "running" : "ok"), `${k}: ${st[k]}`))));
          pane.appendChild(h("div.card", M.kv(run, [["task", "Request"], ["agent", "Agent"], ["model", "Model"], ["status", "Status"]])));
        },
        Roster: async (pane) => {
          const r = await M.tool("agent_roster");
          pane.appendChild(h("h3", "Specialists"));
          pane.appendChild(M.table([{ label: "Specialist", key: "name" }, { label: "Model slot", key: "model_slot" }, { label: "Tools", key: "tool_count" }, { label: "Validator", key: "validator" }, { label: "Deterministic", render: (a) => a.deterministic ? "yes" : "no" }], r.specialists));
          pane.appendChild(h("h3", "Industrial agents"));
          pane.appendChild(M.table([{ label: "Agent", key: "name" }, { label: "Implemented", render: (a) => M.chip(a.implemented ? "YES" : "NO", a.implemented ? "ok" : "warn") }, { label: "Workflow", render: (a) => (a.workflow || []).join(" → ") }], r.industrial));
        },
        Run: async (pane) => {
          const select = h("select.field", ["inspection", "document", "engineering", "calculation", "coding", "report", "research", "planning", "safety", "operations"].map((a) => h("option", a)));
          const task = h("textarea.field", { placeholder: "Task, e.g. 'pump power for flow = 50 m3/h, head = 30 m, efficiency = 0.72'" });
          const eq = h("input.field", { placeholder: "Equipment tag (optional)" });
          let path = "";
          const fileLabel = h("span.note", "No document attached");
          const out = h("div.stack");
          pane.append(select, task, eq, h("div.row", M.button("Attach a document…", "", async () => { const p = await M.call("pick_file", { purpose: "document" }); path = p.path || ""; fileLabel.textContent = path || "No document attached"; }), fileLabel),
            h("div.row", M.button("Run the agent", "primary", async () => {
              out.replaceChildren();
              await M.section(out, "the agent is working (local models)", async () => {
                const r = await M.tool("run_industrial_agent", { agent: select.value, task: task.value, document_path: path, equipment: eq.value, output_format: "docx" });
                out.appendChild(h("div.row", M.chip(r.status, r.status === "ok" ? "ok" : r.status === "partial" ? "warn" : "fail"), h("span.note", r.agent_name || "")));
                out.appendChild(h("div.card", M.answer(r.summary)));
                if ((r.findings || []).length) out.appendChild(M.table([{ label: "Severity", render: (f) => M.chip(f.severity) }, { label: "Finding", key: "statement" }, { label: "Evidence", render: (f) => (f.evidence_ids || []).join(", ") || "INFERENCE" }], r.findings));
                (r.warnings || []).forEach((w) => out.appendChild(M.callout(w, "warn")));
                (r.artifacts || []).forEach((a) => out.appendChild(M.button(`Open ${M.basename(a)}`, "small", () => M.call("open_path", { path: a }))));
              });
            })), out);
        },
      });
    },
  });

  /* ------------------------------------------------------------- automation ---- */
  M.screen("automation", {
    group: "AUTOMATION", label: "Automation", icon: "automation", title: "Automation",
    sub: "Folder watchers, reminders and schedules - each one a registered tool, checked by the policy engine and audited.",
    async render(body, params, actions) {
      const out = h("div.stack");
      const live = h("div.stack");
      const STATUS_TONE = { RUNNING: "live", WAITING_FOR_USER: "info", QUEUED: "info", PLANNING: "info", COMPLETED: "ok", CANCELLED: "plain", FAILED: "fail" };
      async function refresh() {
        const r = await M.tool("automation_overview", {});
        live.replaceChildren(
          h("div.card", h("div.card-head", h("h3", `Automations (${r.active} active)`), M.button("Refresh", "small", refresh)),
            M.table([{ label: "ID", key: "id" }, { label: "Kind", key: "kind" }, { label: "What", key: "description" },
              { label: "Status", render: (t) => M.chip(t.status, STATUS_TONE[t.status] || "plain") },
              { label: "Runs", key: "runs" }, { label: "Last run", render: (t) => t.last_run || "—" },
              { label: "Errors", render: (t) => (t.errors || []).length ? M.chip(`${t.errors.length} error(s)`, "fail") : "—" },
              { label: "", render: (t) => t.cancellable ? M.button("Cancel", "small ghost", async () => { report(await M.tool("cancel_task", { task_id: String(t.id) }), `task ${t.id}`); }) : "" }],
            r.tasks, { empty: "No automations yet - start one below.", onRow: (t) => M.openSheet(`#${t.id} ${t.description}`, h("div.stack", M.kv(t, [["status", "Status"], ["kind", "Kind"], ["runs", "Runs"]]),
              h("div.eyebrow", "runs"), h("ul.dim", (t.steps || []).slice(-30).map((x) => h("li", x))), (t.errors || []).length ? M.callout(t.errors.join("\n"), "fail") : null)) })),
          h("div.card", h("h3", "Audit - automation actions"), M.table([{ label: "When", render: (a) => a.at ? M.time(a.at) : "—" }, { label: "Action", key: "action" },
            { label: "Risk", key: "risk" }, { label: "Allowed", render: (a) => M.chip(a.approved ? "ALLOWED" : "REFUSED", a.approved ? "ok" : "fail") }], r.audit, { empty: "No automation actions audited yet." })));
      }
      function report(r, what) {
        const bad = !r || r.error || r.success === false;
        const summary = bad ? `${what}: ${(r && (r.error || r.output)) || "failed"}` :
          r.task_id ? `${what}: started as task ${r.task_id}` : r.stopped ? `stopped: ${r.stopped}` : `${what}: done`;
        out.prepend(h("div.card", M.callout(summary, bad ? "fail" : "ok"), h("details", h("summary", "details"), M.pre(r))));
        refresh();
      }
      body.append(live);
      body.appendChild(h("div.grid.two",
        h("div.card.stack", h("h3", "Watch a folder"), h("p.note", "New documents in the folder are processed as they arrive."),
          h("div.row", M.button("Choose a folder to watch…", "primary", async () => { const p = await M.call("pick_folder", { purpose: "watch" }); if (p.path) report(await M.tool("watch_folder", { path: p.path }), "watch"); }),
            M.button("Stop watching a folder…", "", async () => { const p = await M.call("pick_folder", { purpose: "watch" }); if (p.path) report(await M.tool("stop_watching_folder", { path: p.path }), "stop watching"); }))),
        (() => {
          const msg = h("input.field", { placeholder: "Reminder" }), minutes = h("input.field", { type: "number", min: "0.1", step: "0.5", placeholder: "in minutes", style: "width:140px" });
          return h("div.card.stack", h("h3", "Reminders"), h("div.row", h("div.grow", msg), minutes, M.button("Remind me", "primary", async () => {
            if (!msg.value.trim() || !(Number(minutes.value) > 0)) { M.toast("A reminder needs a text and a time in minutes", "warn"); return; }
            report(await M.tool("remind_me", { message: msg.value.trim(), minutes: minutes.value }), "reminder");
          })), h("p.note", "Pending reminders appear in the table above; cancel them there."));
        })(),
        (() => {
          const details = h("textarea.field", { placeholder: "Your week, e.g. 'shift 7-15 Mon-Fri, gym 3 times, study 1 h daily'" });
          const change = h("input.field", { placeholder: "Change to the schedule" });
          return h("div.card.stack", h("h3", "Weekly schedule"), h("p.note", "A personal weekly plan written by the local model - a document, not a background job."), details,
            h("div.row", M.button("Create schedule", "primary", async () => { if (details.value.trim()) report(await M.tool("create_schedule", { details: details.value.trim() }), "schedule"); })),
            change, h("div.row", M.button("Update schedule", "", async () => { if (change.value.trim()) report(await M.tool("update_schedule", { change_description: change.value.trim() }), "schedule update"); })));
        })(),
        h("div.card.stack", h("h3", "Overdue tasks & running work"), h("div.row",
          M.button("Overdue tasks", "", async () => report(await M.tool("overdue_tasks"), "overdue tasks")),
          M.button("Pipeline status", "", async () => report(await M.tool("pipeline_status"), "pipeline status"))))));
      body.appendChild(out);
      await refresh();
    },
  });

  /* ZEUS NETWORK - Advanced: the server address and the extra CA file. Shown to
     everyone; changed only by an administrator (checked again in Python). */
  function crudexAdvanced() {
    const card = h("div.card.stack", h("h3", "ZEUS NETWORK - Advanced"), M.loading("reading"));
    M.call("crudex_state").then((n) => {
      if (!n.ok) { card.replaceChildren(h("h3", "ZEUS NETWORK - Advanced"), M.callout(n.error, "warn")); return; }
      card.replaceChildren(h("h3", "ZEUS NETWORK - Advanced"),
        M.kv({ server_url: n.server_url || "not configured", set_by: n.server_source || "—", ca_file: n.ca_file || "Windows trust store only",
          mode: n.mode, state: `${n.state} - ${n.detail || ""}`, device: `${n.device_name} (${n.device_id})` }),
        h("p.note", "Only TLS addresses (scheme https or wss) are accepted. There is no built-in server: an administrator sets the address of the ZEUS server both devices connect to. CRUDEX_SERVER_URL in the environment overrides config.ini."),
        n.can_configure_server ? h("div.row", M.button("Change server…", "", async () => {
          const v = await M.form("ZEUS server", [{ name: "url", label: "Server address - scheme https or wss, e.g. crudex.example.org with that scheme in front", value: n.server_url || "" },
            { name: "ca_file", label: "Extra CA certificate file for a self-hosted server (optional)", value: n.ca_file || "" }], { submit: "Save" });
          if (!v) return;
          const r = await M.call("crudex_set_server", v);
          M.toast(r.ok ? "Server address saved in config.ini" : r.error, r.ok ? "ok" : "fail");
          if (r.ok) M.refresh();
        })) : h("p.note", n.server_source === "environment" ? "Set by the CRUDEX_SERVER_URL environment variable." : "Only an administrator can change it."));
    }).catch((e) => card.replaceChildren(h("h3", "ZEUS NETWORK - Advanced"), M.callout(e.message, "fail")));
    return card;
  }

  /* --------------------------------------------------------------- settings ---- */
  M.screen("settings", {
    group: "SYSTEM", label: "Settings", icon: "settings", title: "System settings",
    sub: "Voice, session, account and diagnostics. Configuration files are edited by an administrator; this screen changes only what can be changed safely at run time.",
    async render(body) {
      const s = await M.call("status");
      const v = s.voice || {};
      body.appendChild(h("div.grid.two",
        h("div.card.stack", h("h3", "Voice"), M.kv(v, [["state", "State"], ["input", "Listening with"], ["output", "Speaking with"], ["detail", "Detail"]]),
          h("div.row", M.button("Spoken replies on", "", async () => { await M.call("set_speak", { enabled: true }); M.toast("Spoken replies on", "ok"); }),
            M.button("Mute", "", async () => { await M.call("set_speak", { enabled: false }); await M.call("stop_speaking"); M.toast("Muted", "ok"); }),
            M.button("Stop speaking", "", async () => { await M.call("stop_speaking"); }))),
        h("div.card.stack", h("h3", "Session & account"), h("p.note", "The session locks itself after the idle limit in config.ini [auth]; the vault locks with it."),
          h("div.row", M.button("Lock now", "", () => M.call("lock")), M.button("Sign out", "", () => M.call("logout")), M.button("Change password", "primary", () => M.changePassword()))),
        h("div.card.stack", h("h3", "Network environment"), h("p.note", "Opens ONE audited connection to find out whether this machine can reach the internet at all."),
          h("div.row", M.button("Probe now", "", async () => { const r = await M.call("probe_network"); M.openSheet("Network probe", M.pre(r)); }))),
        crudexAdvanced(),
        h("div.card.stack", h("h3", "This machine"), M.kv({ cpu_percent: s.cpu, ram_used_gb: s.ram_used_gb, ram_total_gb: s.ram_total_gb, gpu: s.gpu ? `${s.gpu.name} · ${M.num(s.gpu.vram_free_gb, 1)}/${M.num(s.gpu.vram_gb, 1)} GB free` : "CPU only", resident_models: (s.resident_models || []).join(", ") || "none" }),
          h("div.row", M.button("System info", "", async () => M.openSheet("System info", M.pre(await M.tool("get_system_info")))), M.button("Models", "", () => M.go("models"))))));
    },
  });
})();
