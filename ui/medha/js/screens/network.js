/* ZEUS NETWORK - my devices, messages, files, remote tasks, sharing.
   Every value comes from a registered tool (crudex_net/tools.py) through the
   policy engine; presence is the server's, and is never shown as live while
   this device is not connected. Nothing here is decorative. */
(function () {
  "use strict";
  const M = window.ZEUS;
  const { h } = M;

  const PERM_SHORT = { SEND_FILES: "send files", RECEIVE_FILES: "receive files", READ_SHARED_DATA: "read shared data",
    WRITE_SHARED_DATA: "write shared data", SEND_TASKS: "send tasks", RECEIVE_TASKS: "run their tasks",
    READ_SHARED_KNOWLEDGE: "read shared knowledge", WRITE_SHARED_KNOWLEDGE: "add to my knowledge", VIEW_STATUS: "see my status" };

  function presence(d) {
    if (d.this_device) return M.gchip("● THIS DEVICE", "live");
    if (d.status === "revoked") return M.gchip("⚠ REVOKED", "fail");
    if (!d.paired) return M.chip("not paired", "plain");
    if (d.online === true) return M.gchip("● ONLINE", "ok");
    if (d.online === false) return M.gchip("○ OFFLINE", "plain");
    return M.gchip("— unknown (this device is offline)", "plain");
  }

  async function netGate(body) {
    const n = M.net() || (await M.refreshNet());
    if (n.mode !== "online") {
      body.appendChild(M.callout("Offline mode - the ZEUS NETWORK is switched off. Everything on this machine keeps working. " +
        "Choose Online in the user menu (top right) to reach your other devices.", "info"));
    } else if (!["ONLINE", "SYNCING"].includes(n.state)) {
      body.appendChild(M.callout(`${n.state}: ${n.detail || ""}. What you queue now is sent when the connection is back.`, n.state === "ERROR" ? "fail" : "warn"));
    }
    return n;
  }

  async function pairedDevices() {
    try { return ((await M.tool("crudex_devices")).devices || []).filter((d) => d.paired && d.status !== "revoked"); }
    catch (e) { return []; }
  }
  function deviceSelect(devices, selected) {
    return h("select.field", devices.length ? devices.map((d) => h("option", { value: d.device_id, selected: d.device_id === selected }, `${d.name} (${d.online === true ? "online" : d.online === false ? "offline" : "status unknown"})`))
      : [h("option", { value: "" }, "no paired device")]);
  }
  /* A tool call that may need the user's confirmation (ASK BEFORE SHARING). */
  async function withConfirm(name, args, what) {
    let out = await M.call("tool", { name, args });
    if (out.success && out.output && out.output.needs_confirmation) {
      if (!(await M.confirm(`Share ${what}?`, out.output.error + " It will be sent to the selected device.", false))) return null;
      out = await M.call("tool", { name, args: Object.assign({}, args, { confirmed: true }) });
    }
    if (!out.success) throw new Error(out.error || "not permitted");
    if (out.output && out.output.ok === false) throw new Error(out.output.error);
    return out.output;
  }

  /* ------------------------------------------------------------ devices ---- */
  async function showCode() {
    const r = await M.tool("create_crudex_pairing_code");
    const left = h("span.mono");
    const tick = () => { const s = Math.max(0, Math.round(r.expires_at - Date.now() / 1000)); left.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; if (s <= 0) clearInterval(timer); };
    const timer = setInterval(tick, 1000); tick();
    M.openSheet("Pair a device", h("div.stack",
      h("p.dim", "On the other ZEUS device (signed in, Online, same account): My devices → + ADD ZEUS DEVICE → Enter a pairing code."),
      h("div.crx-code", r.code.replace(/(\d{3})(\d{3})/, "$1 $2")),
      h("p.note", h("span", "Single use · expires in "), left, h("span", " · 5 wrong attempts lock pairing for 10 minutes"))));
  }
  async function enterCode() {
    const v = await M.form("Enter a pairing code", [{ name: "code", label: "6-digit code shown on your other device", placeholder: "123 456" }], { submit: "Pair" });
    if (!v) return;
    const r = await M.tool("register_crudex_pairing", { code: v.code.replace(/\s/g, "") });
    M.toast(`Paired with ${r.device.name}`, "ok");
    M.refresh();
  }
  async function editPermissions(d, catalogue) {
    const boxes = {};
    const rows = catalogue.map((p) => {
      const box = h("input", { type: "checkbox", checked: d.grants_given.includes(p.name) });
      boxes[p.name] = box;
      return h("label.crx-perm", box, h("div", h("b", p.name), h("div.note", p.text + (p.dangerous ? " - off unless you choose it" : ""))),
        d.grants_received.includes(p.name) ? M.chip(`they allow: ${PERM_SHORT[p.name]}`, "info") : null);
    });
    const save = M.button("Save permissions", "primary", async () => {
      const grants = Object.keys(boxes).filter((k) => boxes[k].checked);
      await M.tool("set_crudex_permissions", { device: d.device_id, grants });
      M.toast("Permissions saved on the server", "ok");
      M.closeSheet(); M.refresh();
    });
    M.openSheet(`What ${d.name} may do with this device`, h("div.stack",
      h("p.note", "A flow needs BOTH sides: a file from them needs their SEND_FILES and your RECEIVE_FILES. Each device controls only its own grants; the server enforces both."),
      rows, save));
  }
  M.screen("devices", {
    group: "NETWORK", label: "My Devices", icon: "devices", title: "My devices", live: "network",
    sub: "Your ZEUS devices on the ZEUS NETWORK - identified by device id and key pair, never by IP address. Pair a device before anything is shared with it.",
    async render(body, params, actions) {
      const n = await netGate(body);
      actions.append(M.button("+ ADD ZEUS DEVICE", "primary", async () => {
        const choice = await M.form("Add a ZEUS device", [{ name: "how", label: "How", type: "select", value: "Show a pairing code on this device", options: ["Show a pairing code on this device", "Enter a code from the other device"] }], { submit: "Continue" });
        if (!choice) return;
        if (choice.how.startsWith("Show")) await showCode(); else await enterCode();
      }), M.button("Refresh", "", () => M.refresh()));
      const data = await M.tool("crudex_devices");
      const me = h("div.card.stack", h("div.card-head", h("h3", "This device"), presence({ this_device: true })),
        M.kv({ name: n.device_name, device_id: n.device_id, account: n.account || "—", server: n.server || "not configured", state: `${n.state} - ${n.detail || ""}`, last_sync: M.ago(n.last_sync_at) }),
        h("div.row", M.button("Rename", "", async () => {
          const v = await M.form("Rename this device", [{ name: "name", label: "Device name", value: n.device_name }], { submit: "Rename" });
          if (v) { await M.tool("set_crudex_device_name", { name: v.name }); await M.refreshNet(); M.refresh(); }
        })));
      body.appendChild(me);
      const others = (data.devices || []).filter((d) => !d.this_device);
      if (!others.length) {
        body.appendChild(M.callout(data.live ? "No other device is registered to this account yet. Sign in to ZEUS on the other laptop, go Online with the same account's API key, then pair it here." :
          "No device list yet - connect once to read it from the server.", "info"));
        return;
      }
      const grid = h("div.crx-devices");
      others.forEach((d) => {
        const card = h("div.card.stack.crx-device" + (d.online ? ".on" : ""),
          h("div.card-head", h("h3", d.name), presence(d)),
          M.kv({ device_id: d.device_id, platform: d.platform, version: d.version, last_seen: d.last_seen ? M.ago(d.last_seen) : (d.paired ? "hidden (not shared)" : "—"), registered: M.time(d.registered_at) }),
          d.paired ? h("div.crx-grants", d.grants_given.map((g) => M.chip(PERM_SHORT[g] || g, g === "RECEIVE_TASKS" || g.startsWith("WRITE") ? "warn" : "plain"))) : null,
          h("div.row",
            d.paired ? M.button("Permissions", "", () => editPermissions(d, data.permissions)) : null,
            d.paired ? M.button("Message", "", () => M.go("messages", { device: d.device_id })) : null,
            d.paired ? M.button("Send file", "", () => M.go("transfers", { device: d.device_id })) : null,
            d.paired ? M.button("Task", "", () => M.go("rtasks", { device: d.device_id })) : null,
            d.paired ? M.button("Unpair", "", async () => {
              if (!(await M.confirm(`Unpair ${d.name}?`, "The devices stay registered but can no longer exchange anything until paired again.", true))) return;
              await M.tool("remove_crudex_pairing", { device: d.device_id }); M.refresh();
            }) : null,
            d.status !== "revoked" ? M.button("Revoke", "danger", async () => {
              if (!(await M.confirm(`Revoke ${d.name}?`, "It can no longer connect to the ZEUS NETWORK with this account, every pairing ends and nothing more is delivered to it. This cannot be undone from here.", true))) return;
              await M.tool("remove_crudex_device", { device: d.device_id }); M.toast(`${d.name} revoked`, "ok"); M.refresh();
            }) : null));
        grid.appendChild(card);
      });
      body.appendChild(grid);
    },
  });

  /* ----------------------------------------------------------- messages ---- */
  M.screen("messages", {
    group: "NETWORK", label: "Messages", icon: "messages", title: "Messages", live: "network",
    sub: "Real-time messages and notifications between your devices. Sent while offline, they wait in the outbox; each is delivered once.",
    async render(body, params) {
      await netGate(body);
      const devices = await pairedDevices();
      if (!devices.length) { body.appendChild(M.callout("Pair a device first (My devices).", "info")); return; }
      const selected = (params && params.device) || devices[0].device_id;
      const pick = deviceSelect(devices, selected);
      pick.addEventListener("change", () => M.go("messages", { device: pick.value }));
      body.appendChild(h("div.jv-pick", h("span.eyebrow", "WITH"), pick));
      const list = (await M.tool("crudex_messages", { device: selected })).messages || [];
      const STATE = { pending: ["waiting to send", "warn"], sent: ["on the server", "info"], delivered: ["delivered", "ok"], received: ["received", "ok"], failed: ["not delivered", "fail"] };
      body.appendChild(h("div.crx-thread", list.length ? list.map((m) => h("div.crx-msg." + m.direction + (m.kind === "notification" ? ".notif" : ""),
        h("div.crx-text.selectable", m.text),
        h("div.note", M.time(m.created_at), " · ", M.chip((STATE[m.state] || [m.state])[0], (STATE[m.state] || [0, "plain"])[1]))))
        : M.empty("No messages with this device yet.")));
      const input = h("textarea.field", { placeholder: "Message…", rows: 3 });
      const notif = h("input", { type: "checkbox" });
      body.appendChild(h("div.card.stack", input, h("div.row.between", h("label.row", notif, "send as a notification"),
        M.button("Send", "primary", async () => {
          if (!input.value.trim()) return;
          await M.tool("write_crudex_message", { device: selected, text: input.value, notification: notif.checked });
          input.value = ""; M.refresh();
        }))));
    },
  });

  /* ---------------------------------------------------------- transfers ---- */
  M.screen("transfers", {
    group: "NETWORK", label: "File Transfers", icon: "transfers", title: "File transfers", live: "network",
    sub: "Any file type. Streamed in 256 KB chunks (never loaded whole), resumed from the last confirmed byte after a drop, and accepted only if its SHA-256 matches.",
    async render(body, params, actions) {
      await netGate(body);
      const devices = await pairedDevices();
      if (devices.length) {
        const pick = deviceSelect(devices, params && params.device);
        body.appendChild(h("div.card.stack", h("h3", "Send"), h("div.jv-pick", pick,
          M.button("Choose a file…", "primary", async () => {
            const f = await M.call("pick_file", { purpose: "any" });
            if (!f.path) return;
            const r = await withConfirm("create_crudex_file_transfer", { device: pick.value, path: f.path }, M.basename(f.path));
            if (r) { M.toast(`Queued ${M.basename(f.path)}${r.note ? " - " + r.note : ""}`, "ok"); M.refresh(); }
          }),
          M.button("Send a generated artifact…", "", async () => {
            const arts = (await M.tool("list_artifacts")).artifacts || [];
            if (!arts.length) { M.toast("No artifacts yet", "warn"); return; }
            const v = await M.form("Send an artifact", [{ name: "a", label: "Artifact", type: "select", options: arts.slice(0, 40).map((a) => `${a.id} · ${a.name}`) }], { submit: "Send" });
            if (!v) return;
            const r = await withConfirm("create_crudex_artifact_transfer", { device: pick.value, artifact_id: parseInt(v.a, 10) }, "the artifact");
            if (r) M.refresh();
          }),
          M.button("Send project updates", "", async () => {
            const r = await withConfirm("create_crudex_project_transfer", { device: pick.value }, "this device's project updates");
            if (r) { M.toast("Project update bundle queued", "ok"); M.refresh(); }
          }))));
      }
      const rows = (await M.tool("crudex_transfers")).transfers || [];
      body.appendChild(M.table([
        { label: "", render: (t) => t.direction === "out" ? "↑" : "↓" },
        { label: "File", render: (t) => h("div", h("b", t.name), t.conflict ? M.chip("conflict copy", "warn") : null) },
        { label: "Device", render: (t) => t.peer_name },
        { label: "Progress", render: (t) => h("div.crx-prog", h("div.jv-bar", h("div.fill", { style: `width:${t.size ? Math.min(100, (t.bytes_done / t.size) * 100) : 100}%` })),
          h("span.mono", `${M.num(t.bytes_done / 1048576, 1)} / ${M.num(t.size / 1048576, 1)} MB`)) },
        { label: "State", render: (t) => M.chip(t.state, { delivered: "ok", received: "ok", uploaded: "info", uploading: "live", downloading: "live", failed: "fail", cancelled: "plain", queued: "warn", offered: "warn" }[t.state] || "plain") },
        { label: "", render: (t) => h("div.row",
          ["queued", "uploading", "offered", "downloading"].includes(t.state) ? M.button("Cancel", "small", async () => { await M.tool("cancel_crudex_transfer", { transfer_id: t.transfer_id }); M.refresh(); }) : null,
          t.final_path ? M.button("Open", "small", () => M.call("open_path", { path: t.final_path })) : null,
          t.error ? h("span.note", t.error) : null) },
      ], rows, { empty: "No transfers yet." }));
    },
  });

  /* ------------------------------------------------------- remote tasks ---- */
  M.screen("rtasks", {
    group: "NETWORK", label: "Remote Tasks", icon: "rtasks", title: "Remote tasks", live: "network",
    sub: "Ask another device to do something locally and send the result back. A fixed list of task types - never a command, a script or a desktop action. The other device must allow RECEIVE_TASKS.",
    async render(body, params) {
      await netGate(body);
      const data = await M.tool("crudex_remote_tasks");
      const types = data.types || {};
      const devices = await pairedDevices();
      if (devices.length) {
        const pick = deviceSelect(devices, params && params.device);
        const type = h("select.field", Object.entries(types).map(([k, t]) => h("option", { value: k }, `${k} - ${t.label}`)));
        const param = h("input.field", { placeholder: "question / search text / project / transfer id" });
        body.appendChild(h("div.card.stack", h("h3", "New task"), h("div.jv-pick", pick, type), param,
          h("div.row", M.button("Send task", "primary", async () => {
            const args = { device: pick.value, task_type: type.value };
            const needs = (types[type.value] || {}).params || [];
            if (needs[0]) args[needs[0]] = param.value;
            const r = await withConfirm("create_crudex_remote_task", args, "a task");
            if (r) { param.value = ""; M.toast("Task sent - its state updates as the other device reports it", "ok"); M.refresh(); }
          }))));
      }
      const tone = { PENDING: "warn", RECEIVED: "info", RUNNING: "live", COMPLETED: "ok", FAILED: "fail", CANCELLED: "plain" };
      body.appendChild(M.table([
        { label: "", render: (t) => t.direction === "out" ? "→ sent" : "← received" },
        { label: "Task", render: (t) => h("div", h("b", t.task_type), h("div.note", t.label)) },
        { label: "Device", render: (t) => t.peer_name },
        { label: "State", render: (t) => M.chip(t.state, tone[t.state]) },
        { label: "When", render: (t) => M.time(t.updated_at) },
        { label: "", render: (t) => h("div.row",
          t.result || t.error ? M.button("Result", "small", () => M.openSheet(`${t.task_type} - ${t.state}`, h("div.stack",
            t.error ? M.callout(t.error, "fail") : null, t.result && t.result.answer ? M.answer(t.result.answer) : t.result && t.result.summary ? M.answer(t.result.summary) : null,
            t.result ? M.pre(t.result) : null, h("div.note", (t.history || []).map((x) => `${x.state} ${M.time(x.at)}`).join(" → "))))) : null,
          t.direction === "out" && ["PENDING", "RECEIVED"].includes(t.state) ? M.button("Cancel", "small", async () => { await M.tool("cancel_crudex_remote_task", { task_id: t.task_id }); M.refresh(); }) : null) },
      ], data.tasks || [], { empty: "No remote tasks yet." }));
    },
  });

  /* ------------------------------------------------------------ sharing ---- */
  M.screen("sharing", {
    group: "NETWORK", label: "Sharing & Sync", icon: "sharing", title: "Sharing & sync", live: "network",
    sub: "What may leave this device, what other devices shared with it, and folders kept in sync. Keys, passwords and credential files never leave, whatever is chosen here.",
    async render(body) {
      await netGate(body);
      const pol = await M.tool("crudex_sharing_policy");
      const LABEL = { LOCAL_ONLY: "LOCAL ONLY", SHARED: "SHARED", ASK: "ASK BEFORE SHARING" };
      body.appendChild(h("div.card.stack", h("div.card-head", h("h3", "What may leave this device")),
        h("div.crx-policy", pol.categories.map((c) => {
          const sel = h("select.field", pol.choices.map((x) => h("option", { value: x, selected: pol.policy[c] === x }, LABEL[x])));
          sel.addEventListener("change", async () => { await M.tool("set_crudex_sharing_policy", { category: c, choice: sel.value }); M.toast(`${c}: ${LABEL[sel.value]}`, "ok"); });
          return h("label.crx-pol", h("span", c), sel);
        }))));
      const devices = await pairedDevices();
      if (devices.length) {
        const pick = deviceSelect(devices);
        const kind = h("select.field", ["knowledge", "memory", "conversation", "preferences", "workspace"].map((k) => h("option", { value: k }, k)));
        const title = h("input.field", { placeholder: "title" });
        const content = h("textarea.field", { rows: 3, placeholder: "the text (for conversation: the last command-center turns are used if left empty)" });
        body.appendChild(h("div.card.stack", h("h3", "Share an item"), h("div.jv-pick", pick, kind), title, content,
          h("div.row", M.button("Share", "primary", async () => {
            let value = content.value;
            if (kind.value === "conversation" && !value.trim())
              value = JSON.stringify((M.conversation || []).filter((t) => !t.pending).slice(-20).flatMap((t) => [{ role: "user", text: t.request }, { role: "assistant", text: (t.result && t.result.response) || "" }]));
            if (kind.value === "workspace" && !value.trim()) value = JSON.stringify({ screen: (M.current() || {}).id || "", project: (M.jv && M.jv.project) || "" });
            const r = await withConfirm("create_crudex_share", { device: pick.value, item_type: kind.value, title: title.value, content: value }, `a ${kind.value} item`);
            if (r) { content.value = ""; M.toast("Shared - queued for delivery", "ok"); }
          }))));
      }
      const items = (await M.tool("crudex_received_items")).items || [];
      body.appendChild(h("div.card.stack", h("div.card-head", h("h3", `Received from other devices (${items.length})`)),
        M.table([{ label: "Type", render: (i) => M.chip(i.item_type, "info") }, { label: "Title", render: (i) => i.title || "—" },
          { label: "From", render: (i) => i.peer_name }, { label: "Received", render: (i) => M.time(i.received_at) },
          { label: "", render: (i) => h("div.row", M.button("View", "small", () => M.openSheet(i.title || i.item_type, M.pre(i.content))),
            ["memory", "knowledge", "project"].includes(i.item_type) && !i.imported_at ? M.button("Import", "small", async () => {
              const r = await M.tool("approve_crudex_received_item", { item_id: i.item_id });
              M.toast(r.ok ? "Imported" : r.detail, r.ok ? "ok" : "warn"); M.refresh(); }) : null,
            i.imported_at ? M.chip("imported", "ok") : i.import_result ? h("span.note", i.import_result) : null) }], items, { empty: "Nothing received yet." })));
      const folders = (await M.tool("crudex_sync_folders")).folders || [];
      body.appendChild(h("div.card.stack", h("div.card-head", h("h3", "Synced folders"),
        devices.length ? M.button("Share a folder…", "", async () => {
          const f = await M.call("pick_folder", { purpose: "sync" });
          if (!f.path) return;
          const v = await M.form("Keep this folder in sync", [{ name: "device", label: "With", type: "select", options: devices.map((d) => d.name) }], { submit: "Share", note: f.path });
          if (!v) return;
          const d = devices.find((x) => x.name === v.device);
          const r = await withConfirm("create_crudex_folder_share", { device: d.device_id, folder: f.path, name: M.basename(f.path) }, "this folder");
          if (r) M.refresh();
        }) : null),
        h("p.note", "Both ways. A file changed on both devices is kept twice (…sync-conflict-…); deletions are never propagated. Writing into a folder needs WRITE_SHARED_DATA."),
        M.table([{ label: "Folder", render: (f) => h("div", h("b", f.name), h("div.note", f.local_path || "not accepted yet")) },
          { label: "With", render: (f) => f.peer_name }, { label: "State", render: (f) => M.chip(f.state, f.state === "active" ? "ok" : "warn") },
          { label: "Last sync", render: (f) => f.last_sync_at ? M.ago(f.last_sync_at) : "—" },
          { label: "", render: (f) => h("div.row",
            f.state === "offered" ? M.button("Accept into…", "small", async () => {
              const p = await M.call("pick_folder", { purpose: "sync" });
              if (p.path) { await M.tool("approve_crudex_folder_share", { device: f.peer, folder_key: f.folder_key, folder: p.path }); M.refresh(); }
            }) : null,
            f.state === "active" ? M.button("Sync now", "small", async () => { await M.tool("request_crudex_folder_sync", { device: f.peer, folder_key: f.folder_key }); M.toast("Sync started", "ok"); }) : null,
            M.button("Stop", "small", async () => { await M.tool("remove_crudex_folder_share", { device: f.peer, folder_key: f.folder_key }); M.refresh(); })) }],
        folders, { empty: "No synced folders." })));
    },
  });
})();
