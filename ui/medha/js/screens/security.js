/* SECURITY & VAULT: measured state only - nothing here is a configured value
   presented as a check. Every change goes through the policy engine. */
(function () {
  "use strict";
  const M = window.ZEUS;
  const { h } = M;

  M.screen("security", {
    group: "SECURITY", label: "Security Center", icon: "security", title: "Security Center",
    sub: "Measured now: sovereignty, network policy, events, accounts, face login and the emergency lockdown.",
    async render(body, params) {
      M.tabbed(body, {
        "Prove it": async (pane) => {
          let probe = null;
          const paint = async () => {
            pane.replaceChildren();
            await M.section(pane, "running every check now", async () => {
              const r = await M.tool("sovereignty_report");
              const lines = probe ? r.lines.map((l) => (l.label === "Network environment" ? { ...l, state: String(probe.internet || "").toUpperCase(), ok: probe.internet === "unreachable", detail: "probed by you just now" } : l)) : r.lines;
              pane.appendChild(h("div.card", h("div.row.between", h("h2", "Sovereign mode"), M.chip(r.sovereign_mode, r.sovereign_mode === "ACTIVE" ? "ok" : "warn"))));
              pane.appendChild(M.table([{ label: "Check", key: "label" }, { label: "State", render: (l) => M.chip(l.state, l.ok ? "ok" : "warn") }, { label: "Evidence", key: "detail" }], lines));
              pane.appendChild(h("p.note", `Measured in ${r.duration_ms} ms at ${M.time(r.measured_at)}. The network environment is known only after the probe, which opens one audited connection.`));
              pane.appendChild(h("div.row", M.button("Re-check", "primary", paint), M.button("Probe the network environment (1 audited connection)", "", async () => { probe = await M.call("probe_network"); paint(); })));
            });
          };
          await paint();
        },
        Network: async (pane) => {
          const n = await M.tool("network_status");
          const a = n.application;
          pane.appendChild(h("div.card", h("div.row.between", h("h3", "Application policy (this process)"), M.chip(a.mode.toUpperCase(), a.mode === "monitor" ? "warn" : "ok")),
            M.kv({ installed: a.installed, ...a.stats, lan_ranges: (a.lan_ranges || []).join(", ") }),
            h("div.row", { style: "margin-top:12px" }, ["sovereign", "monitor", "lockdown"].map((m) => M.button(`Set ${m}`, m === "monitor" ? "danger small" : "small", async () => { await M.tool("set_network_mode", { mode: m }); M.go("security", { tab: "Network" }); })))));
          pane.appendChild(h("div.card", h("div.row.between", h("h3", "OS firewall (Windows)"), M.chip(n.os_firewall.active ? "ACTIVE" : "NOT APPLIED", n.os_firewall.active ? "ok" : "warn")), h("p.note", n.os_firewall.detail)));
          pane.appendChild(h("h3", "Recent non-local attempts"));
          pane.appendChild(M.table([{ label: "When", render: (r) => M.time(r.ts) }, { label: "Kind", key: "kind" }, { label: "Host", key: "host" }, { label: "Port", key: "port" }, { label: "Allowed", render: (r) => (r.allowed ? "yes" : M.chip("BLOCKED", "fail")) }, { label: "Caller", key: "caller" }], a.recent, { empty: "None - nothing tried to leave this machine." }));
        },
        Events: async (pane) => {
          const s = await M.tool("security_status");
          pane.appendChild(h("div.card", M.kv({ user: s.user, role: s.role, vault: s.vault.unlocked ? "unlocked" : s.vault.configured ? "locked" : "not set up", lockdown: s.lockdown.engaged ? "ENGAGED" : "off", approvals_pending: (s.approvals_pending || []).length })));
          pane.appendChild(h("h3", "Security events"));
          pane.appendChild(M.table([{ label: "When", render: (e) => M.time(e.ts) }, { label: "Kind", key: "kind" }, { label: "Severity", render: (e) => M.chip(e.severity, e.severity === "critical" ? "fail" : e.severity === "warning" ? "warn" : "plain") }, { label: "Detail", key: "detail" }, { label: "User", key: "username" }], s.recent_events, { empty: "No security events." }));
          pane.appendChild(h("h3", "Approval decisions"));
          pane.appendChild(M.table([{ label: "Tool", key: "tool" }, { label: "Decision", render: (a) => (a.decision ? "approved" : M.chip("refused", "fail")) }, { label: "By", key: "decided_by" }, { label: "Origin", key: "origin" }], s.approval_history, { empty: "None yet." }));
        },
        Users: async (pane) => {
          const r = await M.tool("list_users");
          pane.appendChild(M.table([{ label: "User", key: "username" }, { label: "Role", render: (u) => M.chip(u.role, u.role === "admin" ? "live" : "plain") }, { label: "Active", render: (u) => (u.active ? "yes" : "no") }, { label: "Last sign-in", render: (u) => M.time(u.last_login) }], r.users,
            { onRow: async (u) => {
              const v = await M.form(`Role for ${u.username}`, [{ name: "role", label: "Role", type: "select", options: ["viewer", "operator", "engineer", "admin"], value: u.role }], { submit: "Set role" });
              if (v) { await M.tool("set_user_role", { username: u.username, role: v.role }); M.go("security", { tab: "Users" }); }
            } }));
          pane.appendChild(h("div.row", M.button("Create an account", "primary", async () => {
            const v = await M.form("New account", [{ name: "username", label: "User ID" }, { name: "password", label: "Password", type: "password" }, { name: "confirm", label: "Confirm password", type: "password" },
              { name: "role", label: "Role", type: "select", options: ["operator", "engineer", "viewer", "admin"], value: "operator" }], { submit: "Create" });
            if (!v) return;
            const res = await M.call("account_create", v);
            M.toast(res.ok ? `Created ${res.username} (${res.role})` : res.reason, res.ok ? "ok" : "fail");
            M.go("security", { tab: "Users" });
          })));
        },
        "Face login": async (pane) => {
          const f = await M.tool("face_status");
          pane.appendChild(h("div.card", h("div.row.between", h("h3", "Face login - InsightFace"), M.chip(`login: ${f.login}`, f.login === "off" ? "plain" : "ok")),
            M.kv({ model: f.model_pack || "not installed", liveness_model: f.liveness_model, liveness_policy: f.liveness_policy, match_threshold: f.threshold, enrolled_accounts: f.enrolled_accounts, your_face: f.your_face_enrolled ? "enrolled" : "not enrolled" }),
            h("p.note", `${f.note || ""} Only a 512-number template is kept, encrypted with Windows DPAPI - never a picture.`)));
          pane.appendChild(h("div.row",
            M.button("Enrol my face (camera)", "primary", async () => { M.toast("Look at the camera…", "ok"); const r = await M.tool("register_face"); if (r.error) { M.toast(r.error, "warn"); return; } M.toast(`Enrolled from ${r.frames_used} picture(s)`, "ok"); if (r.warning) M.toast(r.warning, "warn"); M.go("security", { tab: "Face login" }); }),
            M.button("Test my face", "", async () => { M.toast("Look at the camera…", "ok"); const r = await M.tool("verify_my_face"); M.toast(r.ok ? `Match - similarity ${r.similarity}` : `No match: ${r.reason}`, r.ok ? "ok" : "warn"); }),
            M.button("Remove my face", "danger", async () => { if (await M.confirm("Remove your face template?", "Face sign-in stops working for your account until you enrol again.", true)) { await M.tool("remove_face"); M.go("security", { tab: "Face login" }); } })));
        },
        Lockdown: async (pane) => {
          const s = await M.tool("security_status");
          pane.appendChild(h("div.card", h("div.row.between", h("h2", "Emergency lockdown"), M.chip(s.lockdown.engaged ? "ENGAGED" : "OFF", s.lockdown.engaged ? "fail" : "plain")),
            h("p.dim", "Cuts ZEUS's network to loopback, locks the vault, refuses pending approvals, cancels running tasks, releases models and locks the session. An administrator releases it.")));
          pane.appendChild(h("div.row",
            M.button("ENGAGE LOCKDOWN", "danger", async () => { if (await M.confirm("Engage the emergency lockdown now?", "Everything above happens immediately.", true)) await M.tool("engage_lockdown", { reason: "operator from the security center" }); }),
            M.button("Release (admin)", "", async () => { await M.tool("disengage_lockdown"); M.go("security", { tab: "Lockdown" }); })));
        },
      }, params.tab);
    },
  });

  M.screen("firewall", {
    group: "SECURITY", label: "Firewall", icon: "firewall", title: "Firewall",
    sub: "ZEUS's programs confined to this PC and the LAN - Windows Firewall rules and WFP filters. Applying or removing needs an administrator: an approval here, then Windows asks.",
    async render(body) {
      const [os, w] = await Promise.all([M.tool("firewall_status").catch((e) => ({ error: e.message })), M.tool("wfp_firewall_status").catch((e) => ({ error: e.message }))]);
      body.appendChild(os.is_admin === true ? h("div.row", M.chip("ADMINISTRATOR", "ok"), h("span.note", os.privileges))
        : M.callout(os.is_admin === false ? `ADMIN PRIVILEGES REQUIRED - ${os.privileges.replace(/^ADMIN PRIVILEGES REQUIRED /, "")}` : (os.privileges || os.error || "privileges unknown"), "warn"));
      body.appendChild(h("div.card", h("div.row.between", h("h3", "Windows Firewall rules"), os.error ? M.chip("UNKNOWN", "plain") : M.chip(os.active || os.enabled ? "ACTIVE" : "NOT APPLIED", os.active || os.enabled ? "ok" : "warn")),
        h("p.note", os.detail || os.error || ""),
        (os.rules || []).length ? M.table([{ label: "Rule (ZEUS-owned)", render: (name) => name }], os.rules) : h("p.note", "No ZEUS rules exist - FIREWALL_CRUDEX.bat apply creates them."),
        h("p.note", `Only rules named ${(os.rule_prefixes || ["ZEUS …"]).map((p) => `'${p} …'`).join(" or ")} are ever created, changed or removed; every other Windows Firewall rule is left alone.`)));
      const state = w.installed === true ? M.chip("ACTIVE", "ok") : w.installed === false ? M.chip("NOT APPLIED", "warn") : M.chip("UNKNOWN", "plain");
      body.appendChild(h("div.card", h("div.row.between", h("h3", "WFP filters (bin/agni-wfp.exe)"), state), h("p.note", w.detail || w.error || ""),
        (w.rules || []).length ? M.table([{ label: "Filter", key: "name" }, { label: "Action", key: "action" }, { label: "Program", key: "description" }], w.rules) : null));
      const out = h("div.stack");
      body.appendChild(h("div.row",
        M.button("Apply (admin)", "primary", async () => {
          const r = await M.tool("apply_wfp_firewall", { allow_lan: "true" }).catch((e) => ({ ok: false, error: e.message }));
          if (r && r.ok) { M.toast(`${r.filters_added} WFP filters installed`, "ok"); M.refresh(); }
          else out.replaceChildren(M.callout(`Not applied: ${(r && (r.error || r.detail)) || "no result"} - nothing was changed.`, "fail"));
        }),
        M.button("Blocked connections (admin)", "", async () => { const r = await M.tool("wfp_blocked_connections", { minutes: "60" }); out.replaceChildren(h("h3", `Blocked in the last ${r.minutes} min: ${r.blocked}`), M.table([{ label: "When", key: "time" }, { label: "Remote", key: "remote" }, { label: "Filter", key: "filter" }], r.events || [], { empty: "Nothing blocked." })); }),
        M.button("Remove (admin)", "danger", async () => {
          if (!(await M.confirm("Remove ZEUS's WFP filters?", "Programs are no longer confined at the WFP layer until you apply them again.", true))) return;
          const r = await M.tool("remove_wfp_firewall").catch((e) => ({ ok: false, error: e.message }));
          if (r && r.ok) { M.toast(`${r.removed_filters} filters removed`, "warn"); M.refresh(); }
          else out.replaceChildren(M.callout(`Not removed: ${(r && (r.error || r.detail)) || "no result"} - nothing was changed.`, "fail"));
        })));
      body.appendChild(out);
      body.appendChild(h("p.note", "FIREWALL_CRUDEX.bat, APPLY_FIREWALL.bat and REMOVE_FIREWALL.bat do the same from Explorer. Unrelated Windows Firewall rules are never touched."));
    },
  });

  M.screen("audit", {
    group: "SECURITY", label: "Audit", icon: "audit", title: "Audit trail",
    sub: "The tamper-evident, HMAC-chained record of every action.",
    async render(body) {
      const v = await M.tool("verify_audit_chain");
      const forks = v.forks || [];
      body.appendChild(h("div.card", h("div.row.between", h("h2", `${v.records} records`), M.chip(!v.ok ? "CHAIN BROKEN" : forks.length ? "INTACT - CONCURRENT FORK" : "CHAIN VERIFIED", !v.ok ? "fail" : forks.length ? "warn" : "ok")),
        h("p.dim", v.detail), forks.length ? h("ul.note", forks.map((f) => h("li", f.detail))) : null));
      const [s, log] = await Promise.all([M.tool("security_status"), M.tool("show_audit_log").catch(() => ({ entries: [] }))]);
      body.appendChild(h("div.card", h("h3", "Recent actions"), h("div.stack.mono", { style: "max-height:48vh;overflow:auto" }, (log.entries || []).slice().reverse().map((e) => h("div.selectable", { style: "border-bottom:1px solid var(--steel-2);padding:4px 0" }, e)))));
      body.appendChild(h("p.note", `Security events in the last 24 h: ${JSON.stringify(s.events_24h)}`));
    },
  });

  M.screen("vault", {
    group: "VAULT", label: "Vault", icon: "vault", title: "Vault",
    sub: "Encrypted locker and the employee register - unreachable while locked, never indexed, never given to a model.",
    async render(body, params) {
      M.tabbed(body, {
        Locker: async (pane) => {
          const o = await M.tool("vault_overview");
          pane.appendChild(h("div.card", h("div.row.between", h("h3", "Secure locker"), M.chip(o.unlocked ? "UNLOCKED" : o.configured ? "LOCKED" : "NOT SET UP", o.unlocked ? "warn" : o.configured ? "ok" : "plain")),
            h("p.note", `${o.cipher} · key derived with scrypt from a passphrase that is never stored · locks automatically when ZEUS locks or you sign out.`),
            h("div.row", M.button(o.configured ? "Unlock" : "Set the passphrase", "primary", async () => {
              const fields = [{ name: "passphrase", label: "Passphrase", type: "password" }];
              if (!o.configured) fields.push({ name: "confirm", label: "Confirm passphrase", type: "password" });
              const v = await M.form(o.configured ? "Unlock the vault" : "Set the vault passphrase", fields, { submit: o.configured ? "Unlock" : "Create", note: o.configured ? "" : "At least 10 characters. There is no way to recover it." });
              if (!v) return;
              const r = await M.call("vault_open", { passphrase: v.passphrase, confirm: v.confirm || "" });
              M.toast(r.ok ? (r.created ? "Vault created" : "Vault unlocked") : r.reason, r.ok ? "ok" : "fail");
              M.go("vault");
            }), M.button("Lock", "", async () => { await M.tool("lock_vault"); M.go("vault"); }))));
          if (!o.unlocked) pane.appendChild(h("p.note", "File names are confidential too - nothing is listed while the vault is locked."));
          else {
            const snap = await M.tool("vault_snapshot");
            pane.appendChild(h("div.row", M.button("Add a file…", "", async () => { const p = await M.call("pick_file", { purpose: "any" }); if (p.path) { await M.tool("add_file_to_vault", { path: p.path }); M.go("vault"); } })));
            pane.appendChild(M.table([{ label: "File", key: "display_name" }, { label: "Category", key: "category" }, { label: "Size", render: (f) => `${M.num(f.size_bytes / 1024, 0)} KB` }, { label: "Added", render: (f) => M.time(f.added_at) }], snap.files,
              { empty: "The vault is empty.", onRow: async (f) => { const folder = await M.call("pick_folder", { purpose: "export" }); if (folder.path) { const r = await M.tool("export_vault_file", { name: f.display_name, destination: folder.path }); M.toast(r.exported || r.saved || JSON.stringify(r), "ok"); } } }));
            pane.appendChild(h("p.note", "Click a file to export a decrypted copy (you will be asked to approve)."));
          }
          pane.appendChild(h("h3", "Vault audit trail"));
          pane.appendChild(M.table([{ label: "When", render: (e) => M.time(e.created_at) }, { label: "Action", key: "action" }, { label: "Subject", key: "subject" }, { label: "OK", render: (e) => (e.succeeded ? "✓" : M.chip("REFUSED", "fail")) }, { label: "By", key: "username" }], o.audit, { empty: "Nothing recorded." }));
        },
        Employees: async (pane) => {
          let s;
          try { s = await M.tool("vault_snapshot"); } catch (e) { pane.append(M.callout(e.message, "warn"), h("p.note", "The employee register is restricted data: unlock the vault (engineer role or above).")); return; }
          const f = { id: h("input.field", { placeholder: "Works no." }), name: h("input.field", { placeholder: "Full name" }), role: h("input.field", { placeholder: "Role" }), dept: h("input.field", { placeholder: "Department" }), email: h("input.field", { placeholder: "Email" }), phone: h("input.field", { placeholder: "Phone" }) };
          pane.append(h("div.grid.three", Object.values(f)), h("div.row", M.button("Add employee", "primary", async () => {
            await M.tool("add_employee", { employee_id: f.id.value.trim(), name: f.name.value.trim(), role: f.role.value, department: f.dept.value, email: f.email.value, phone: f.phone.value });
            M.go("vault", { tab: "Employees" });
          })));
          pane.appendChild(M.table(["employee_id", "name", "role", "department", "email", "phone"], s.employees, { empty: "The register is empty.",
            onRow: async (p) => { if (await M.confirm(`Remove ${p.name}?`, "The record is removed from the encrypted register.", true)) { await M.tool("remove_employee", { employee_id: p.employee_id }); M.go("vault", { tab: "Employees" }); } } }));
        },
      }, params.tab);
    },
  });
})();
