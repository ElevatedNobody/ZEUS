/* WORKBENCH: artifacts with provenance, the Excel / PowerPoint / Word builders
   (one verified analysis -> many files) and productivity. */
(function () {
  "use strict";
  const M = window.ZEUS;
  const { h } = M;

  async function pickAnalysis(host) {
    const list = await M.tool("list_analyses").catch(() => ({ analyses: [] }));
    const select = h("select.field", (list.analyses || []).map((a) => h("option", { value: a.analysis_id }, `${a.verified ? "✓" : "·"} ${a.question.slice(0, 80)} (${a.analysis_id})`)));
    host.appendChild(list.analyses && list.analyses.length ? h("div", h("label.label.eyebrow", "From a verified analysis"), select)
      : M.callout("No analysis yet - ask the records a question (Ask the records) and the file is built from that answer's evidence and calculations.", "info"));
    return () => select.value;
  }

  function builder(id, label, icon, fmts, topicVerb, extra) {
    M.screen(id, {
      group: "WORKBENCH", label, icon, title: label,
      sub: "Built from ONE verified analysis - the same numbers, evidence ids and recommendations as the answer, checked after writing. Or from a topic, written by the local model and validated.",
      async render(body) {
        const top = h("div.card.stack");
        body.appendChild(top);
        const getId = await pickAnalysis(top);
        const out = h("div.stack");
        top.appendChild(h("div.row", fmts.map(([fmt, text]) => M.button(text, fmt === fmts[0][0] ? "primary" : "", async () => {
          const analysis = getId();
          if (!analysis) { M.toast("Ask the records first", "warn"); return; }
          out.prepend(M.artifactResult(await M.tool("create_analysis_artifact", { format: fmt, analysis_id: analysis })));
        }))));
        top.appendChild(out);
        const topic = h("input.field", { placeholder: "Or a topic, e.g. 'pump reliability programme'" });
        body.appendChild(h("div.card.stack", h("h3", "From a topic"), topic,
          h("div.row", M.button(topicVerb[0], "", () => topic.value.trim() && M.submit(`${topicVerb[1]} ${topic.value.trim()}`))),
          h("p.note", "Plant topics use the plant records (labelled DEMO where they are sample data); other topics are written by the local model. If no model answers, nothing is saved - no template text.")));
        if (extra) extra(body);
      },
    });
  }

  builder("excel", "Excel Analysis", "excel", [["xlsx", "Build the workbook"], ["action_plan", "Action-plan workbook"]], ["Create an Excel workbook", "create an excel of"], (body) => {
    body.appendChild(h("div.card.stack", h("h3", "Analyse a spreadsheet"), h("p.note", "Reads an .xlsx or .csv you choose and reports what is in it - totals, trends and anomalies, computed rather than guessed."),
      h("div.row", M.button("Choose a spreadsheet…", "", async () => { const p = await M.call("pick_file", { purpose: "document" }); if (p.path) M.submit(`analyze the spreadsheet "${p.path}"`); }))));
  });
  builder("deck", "Presentation Builder", "deck", [["pptx", "Build the deck"]], ["Make a presentation", "make a presentation on"]);
  builder("report", "Word Report Builder", "report", [["docx", "Build the report"], ["brief", "Executive brief"], ["pdf", "PDF"]], ["Write a report", "write a report on"]);

  M.screen("artifacts", {
    group: "WORKBENCH", label: "Artifact Workspace", icon: "artifacts", title: "Artifact workspace",
    sub: "Every generated file with who and what made it - verified when it was written, and re-verifiable now.",
    async render(body, params, actions) {
      const r = await M.tool("list_artifacts", { limit: 60 });
      const list = r.artifacts || [];
      actions.append(M.button("Refresh", "small", () => M.refresh()));
      if (!list.length) {
        body.appendChild(h("div.card.empty-compact", h("div.row.between", h("div", h("b", "No artifacts yet"),
          h("p.note", "Reports, workbooks, decks and approval notes appear here with who made them and how they were checked.")),
          h("div.row", M.button("Report builder", "small", () => M.go("report")), M.button("Excel builder", "small", () => M.go("excel")), M.button("Deck builder", "small", () => M.go("deck"))))));
        return;
      }
      const missing = list.filter((a) => !a.exists).length;
      body.appendChild(h("p.note", `${list.length} artifact(s)${missing ? ` · ${missing} no longer on disk` : ""}`));
      const kb = (n) => n == null ? "—" : n >= 1048576 ? `${M.num(n / 1048576, 1)} MB` : `${M.num(n / 1024, 1)} KB`;
      body.appendChild(h("div.deck", list.map((a) => {
        const result = h("span.note");
        const say = (text, ok) => { result.textContent = text; result.style.color = ok ? "var(--ok)" : "var(--fail)"; };
        const card = h("div.card.artifact-card",
          h("div.row.between", h("div.row", M.chip(a.format || a.kind || "FILE", "plain"), h("b.mono", a.name)),
            h("div.row", M.chip(a.verified ? "VERIFIED AT WRITE" : "NOT VERIFIED", a.verified ? "ok" : "warn"), a.exists ? null : M.chip("MISSING", "fail"))),
          M.kv({ created: M.time(a.created_at), by: a.creator || "—", agent: a.agent || "—", model: a.model || "—", size: kb(a.size_bytes),
            "sha-256": a.sha256 ? a.sha256.slice(0, 16) + "…" : "—", "from analysis": a.analysis_id || "—" }),
          h("p.note.mono", a.path), a.request ? h("p.dim", `“${a.request}”`) : null,
          h("div.row",
            M.button("Open", "small", async () => { const x = await M.call("open_path", { path: a.path }); if (!x.success) say(x.error || "could not open", false); }),
            M.button("Reveal", "small", async () => { const x = await M.tool("reveal_artifact", { artifact_id: String(a.id) }); if (!x.ok) say(x.error, false); }),
            M.button("Re-verify", "small", async () => { const v = await M.tool("verify_artifact", { path: a.path }); say(v.detail || (v.ok ? "opens and is unchanged" : "check failed"), v.ok); }),
            M.button("Regenerate", "small", async () => {
              if (a.analysis_id) {
                const fmt = String(a.kind || a.format || "docx").toLowerCase();
                const x = await M.tool("create_analysis_artifact", { format: fmt, analysis_id: a.analysis_id });
                say(x.ok ? `regenerated from analysis ${a.analysis_id}: ${x.path}` : (x.error || "not regenerated"), !!x.ok);
                if (x.ok) setTimeout(() => M.refresh(), 900);
              } else if (a.request) { M.submit(a.request); say("the original request was sent again - see the Command Center", true); }
              else say("nothing recorded to regenerate it from", false);
            }),
            M.button("Remove from list", "small ghost", async () => { const x = await M.tool("remove_artifact_record", { artifact_id: String(a.id) }); if (x.ok) M.refresh(); else say(x.error, false); }),
            a.exists ? M.button("Delete file", "small danger", async () => {
              const x = await M.tool("delete_file", { path: a.path }).catch((e) => ({ error: e.message }));
              if (x && (x.error || x.success === false)) say(x.error || "not deleted", false);
              else { await M.tool("remove_artifact_record", { artifact_id: String(a.id) }); M.refresh(); }
            }) : null, result));
        return card;
      })));
      body.appendChild(h("p.note", "Delete file asks for your approval first. Remove from list keeps the file on disk."));
    },
  });

  /* ------------------------------------------------------------- productivity ---- */
  M.screen("productivity", {
    group: "WORKBENCH", label: "Productivity", icon: "productivity", title: "Productivity",
    sub: "One task list, one calendar, notes and meeting minutes - all local.",
    async render(body, params) {
      const tabs = h("div.tabs");
      const pane = h("div.stack");
      const reopen = (tab) => M.go("productivity", { tab });
      const views = {
        Tasks: async () => {
          const d = await M.tool("productivity_snapshot");
          const text = h("input.field", { placeholder: "New task" }), due = h("input.field", { type: "date", style: "width:170px" });
          const prio = h("select.field", { style: "width:130px" }, h("option", { value: "" }, "normal"), h("option", "high"), h("option", "low"));
          pane.appendChild(h("div.row", h("div.grow", text), due, prio, M.button("Add", "primary", async () => { if (!text.value.trim()) return; await M.tool("add_todo", { text: text.value.trim(), due_date: due.value, priority: prio.value }); reopen("Tasks"); })));
          pane.appendChild(d.todos.length ? h("div.stack", d.todos.map((t) => {
            const box = h("input", { type: "checkbox", checked: !!t.completed });
            box.addEventListener("change", async () => { await M.tool(box.checked ? "complete_todo" : "uncomplete_todo", { todo_id: t.id }); reopen("Tasks"); });
            return h("div.card.row", box, h("span.grow", { style: t.completed ? "text-decoration:line-through;color:var(--ink-3)" : "" }, t.text), h("span.note", t.due_date || ""), t.priority === "high" ? M.chip("high", "fail") : null,
              M.button("Delete", "small ghost", async () => { await M.tool("delete_todo", { todo_id: t.id }); reopen("Tasks"); }));
          })) : M.empty("No tasks."));
        },
        Calendar: async () => {
          const d = await M.tool("productivity_snapshot");
          const title = h("input.field", { placeholder: "Event or meeting title" }), date = h("input.field", { type: "date", style: "width:170px" }), time = h("input.field", { type: "time", style: "width:130px" });
          const attendees = h("input.field", { placeholder: "Attendees (meetings)" }), agenda = h("input.field", { placeholder: "Agenda (meetings)" });
          pane.append(title, h("div.row", date, time), attendees, agenda, h("div.row",
            M.button("Add event", "primary", async () => { if (!title.value.trim() || !date.value) return M.toast("Title and date are required", "warn"); await M.tool("add_event", { title: title.value.trim(), date: date.value, event_time: time.value }); reopen("Calendar"); }),
            M.button("Schedule meeting", "", async () => { if (!title.value.trim() || !date.value) return M.toast("Title and date are required", "warn"); await M.tool("schedule_meeting", { title: title.value.trim(), date: date.value, time: time.value, attendees: attendees.value, agenda: agenda.value }); reopen("Calendar"); })));
          pane.appendChild(M.table([{ label: "Date", key: "event_date" }, { label: "Time", key: "event_time" }, { label: "Title", key: "title" }, { label: "Kind", key: "kind" }, { label: "Attendees", key: "attendees" }], d.events,
            { empty: "No events.", onRow: async (e) => { if (await M.confirm(`Delete "${e.title}"?`, "The event is removed from your calendar.", true)) { await M.tool("delete_event", { event_id: e.id }); reopen("Calendar"); } } }));
        },
        Notes: async () => {
          const d = await M.tool("productivity_snapshot");
          const title = h("input.field", { placeholder: "Note title" }), content = h("textarea.field", { placeholder: "Note" });
          pane.append(title, content, h("div.row", M.button("Save note", "primary", async () => { if (!content.value.trim()) return; await M.tool("add_note", { title: title.value, content: content.value }); reopen("Notes"); })));
          pane.appendChild(d.notes.length ? h("div.deck", d.notes.map((n) => h("div.card", h("div.row.between", h("b", n.title || "Untitled"), h("span.note", M.time(n.updated_at || n.created_at))),
            h("p.dim.selectable", { style: "white-space:pre-wrap" }, n.content), M.button("Delete", "small ghost", async () => { await M.tool("delete_note", { note_id: n.id }); reopen("Notes"); })))) : M.empty("No notes."));
        },
        Meetings: async () => {
          const out = h("div.stack");
          const show = (r) => {
            out.replaceChildren();
            const m = r.minutes || {};
            if (r.error && !m.summary) out.appendChild(M.callout(r.error, "fail"));
            if (m.summary) out.appendChild(h("div.card", M.answer(m.summary)));
            if ((m.decisions || []).length) out.appendChild(h("div.card", h("h3", "Decisions"), h("ul.dim", m.decisions.map((x) => h("li", x)))));
            if ((m.action_items || []).length) out.appendChild(M.table(["task", "owner", "due"], m.action_items));
            if ((r.tasks_created || []).length) out.appendChild(M.callout(`${r.tasks_created.length} task(s) added to your list.`, "info"));
            if (r.saved) out.appendChild(M.button("Open minutes", "small", () => M.call("open_path", { path: r.saved })));
          };
          pane.append(M.callout("Recording → local Whisper transcript → local model minutes (summary, decisions, action items) → tasks in your list → a verified .docx. An owner the model names is kept only if the transcript contains it.", "info"),
            h("div.row",
              M.button("Recording…", "primary", async () => { const p = await M.call("pick_file", { purpose: "audio" }); if (!p.path) return; out.replaceChildren(); await M.section(out, "transcribing locally and writing minutes", async () => show(await M.tool("process_meeting_audio", { audio_path: p.path }))); }),
              M.button("Transcript (.txt)…", "", async () => { const p = await M.call("pick_file", { purpose: "text" }); if (!p.path) return; out.replaceChildren(); await M.section(out, "writing minutes", async () => show(await M.tool("process_meeting_transcript", { transcript_path: p.path }))); })),
            out);
        },
      };
      const show = async (name) => {
        tabs.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.textContent === name));
        pane.replaceChildren();
        await M.section(pane, "loading", views[name]);
      };
      Object.keys(views).forEach((name) => tabs.appendChild(h("button.tab", { onclick: () => show(name) }, name)));
      body.append(tabs, pane);
      show(params.tab && views[params.tab] ? params.tab : "Tasks");
    },
  });
})();
