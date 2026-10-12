"""
The industrial agents, implemented.

Every workflow here is assembled from components that already exist and are
already tested - the document pipeline and OCR, the schema extractor, the
knowledge base with citation validation, the plant tools, the deterministic
engineering engine, the sandbox - and every one follows the same rules:

  * deterministic first: numbers are computed in Python (industrial.engineering),
    never by the model;
  * evidence always: a finding carries the evidence ids behind it, and anything
    without evidence is marked as an inference;
  * the model is optional: when no local model is reachable the agent still
    returns what it could establish deterministically, and SAYS what it could
    not do, instead of inventing it;
  * artifacts are verified (core.office.finish) before they are reported;
  * the agent never approves anything - approval notes leave the signature
    block empty for a person.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.logger import get_logger

logger = get_logger("industrial.agents")


# ------------------------------------------------------------- helpers ----

def _model_available() -> bool:
    try:
        from core.ollama_client import ollama_client
        return ollama_client.is_available()
    except Exception:
        return False


def _llm(slot: str, system: str, user: str, timeout: float = 180.0,
         think: bool = False, json_format: bool = False) -> Tuple[Optional[str], str]:
    """(text or None, model name). None when no model could answer."""
    if not _model_available():
        return None, ""
    try:
        from core.model_router import model_router
        from core.ollama_client import ollama_client
        model = model_router.route(slot)
        text = ollama_client.chat(model, [{"role": "system", "content": system},
                                          {"role": "user", "content": user}],
                                  retries=0, timeout=timeout, think=think,
                                  json_format=json_format, task_type=slot,
                                  purpose="industrial agent")
        if not text or text.startswith(("[ZEUS could not", "[ZEUS could not", "[Model '")):
            return None, model
        return text, model
    except Exception as error:
        logger.warning(f"agent model call failed: {error}")
        return None, ""


def _json(text: Optional[str]):
    if not text:
        return None
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    for candidate in (cleaned, cleaned[cleaned.find("{"):cleaned.rfind("}") + 1],
                      cleaned[cleaned.find("["):cleaned.rfind("]") + 1]):
        try:
            return json.loads(candidate)
        except Exception:
            continue
    return None


def _kb_search(query: str, top_k: int = 6, **filters):
    """(evidences, info). Never raises."""
    try:
        from industrial.knowledge_base import knowledge_base
        return knowledge_base.search(query, top_k=top_k, **filters)
    except Exception as error:
        return [], {"error": str(error)}


def _document_evidence(doc, limit: int = 12, max_chars: int = 900):
    """Evidence objects from a document's own elements (page-level), so a
    summary of an uploaded file can cite it without indexing it first."""
    from industrial.provenance import Evidence
    evidences = []
    buffer, page, method, conf = [], None, None, []
    for element in doc.elements:
        if not element.text or element.is_inference:
            continue
        if page is not None and element.page_number != page and buffer:
            evidences.append((page, "\n".join(buffer), method, conf))
            buffer, conf = [], []
        page = element.page_number
        method = element.extraction_method
        if element.ocr_confidence is not None:
            conf.append(element.ocr_confidence)
        buffer.append(element.text.strip())
        if sum(len(b) for b in buffer) > max_chars:
            evidences.append((page, "\n".join(buffer), method, conf))
            buffer, conf = [], []
    if buffer:
        evidences.append((page, "\n".join(buffer), method, conf))
    out = []
    for index, (page_no, text, method_, confs) in enumerate(evidences[:limit], start=1):
        out.append(Evidence(evidence_id=f"E{index}", document_id=doc.file_hash or doc.filename,
                            filename=doc.filename, chunk_text=text[:max_chars * 2],
                            page_number=page_no, extraction_method=method_,
                            ocr_confidence=(sum(confs) / len(confs)) if confs else None,
                            claim_type="user_document", title=doc.filename))
    return out


def _grounded(system: str, question: str, evidences, slot: str = "reasoning",
              timeout: float = 240.0) -> Dict[str, Any]:
    """Model answer over evidence, citations validated. No model -> no answer."""
    from industrial import provenance
    from security import injection
    if not evidences:
        return {"answer": None, "grounded": False, "error": "no evidence to reason over"}
    flags = []
    for evidence in evidences:
        verdict = injection.scan(evidence.chunk_text)
        if verdict.flagged:
            flags.append({"id": evidence.evidence_id, "source": evidence.filename,
                          "kinds": verdict.kinds})
    injection.report_flags(flags)
    user = (f"EVIDENCE:\n{provenance.build_evidence_prompt(evidences)}\n\nTASK: {question}\n\n"
            "Use only the evidence above and cite evidence ids in square brackets.")
    text, model = _llm(slot, system + "\n" + provenance.GROUNDING_CONTRACT + "\n"
                       + injection.SYSTEM_RULE, user, timeout=timeout)
    if text is None:
        return {"answer": None, "grounded": False, "model": model,
                "error": "no local model answered - start Ollama (run START.bat) and try again",
                "injection_flags": flags}
    report = provenance.CitationValidator(evidences).validate(text)
    return {"answer": report.cleaned_answer, "grounded": report.grounded, "model": model,
            "citation_report": report.to_dict(), "injection_flags": flags,
            "sources": provenance.answer_sources(evidences, report.valid_citations or None)}


def _save_docx(title: str, subtitle: str, sections: List[Tuple[str, Any]],
               folder: str = "Agents", filename: str = "") -> Dict[str, Any]:
    """A plain, verified Word document. `sections` = [(heading, str | list | table)]
    where a table is {"columns": [...], "rows": [[...]]}."""
    from docx import Document
    from core import office
    doc = Document()
    doc.add_heading(title, level=0)
    if subtitle:
        doc.add_paragraph().add_run(subtitle).italic = True
    for heading, body in sections:
        if heading:
            doc.add_heading(heading, level=2)
        if isinstance(body, dict) and "columns" in body:
            table = doc.add_table(rows=1, cols=len(body["columns"]))
            table.style = "Light Grid Accent 1"
            for i, column in enumerate(body["columns"]):
                table.rows[0].cells[i].text = str(column)
            for row in body["rows"]:
                cells = table.add_row().cells
                for i, value in enumerate(row[:len(body["columns"])]):
                    cells[i].text = "" if value is None else str(value)
        elif isinstance(body, list):
            for item in body:
                doc.add_paragraph(str(item), style="List Bullet")
        elif body:
            for paragraph in str(body).split("\n\n"):
                if paragraph.strip():
                    doc.add_paragraph(paragraph.strip())
    out_dir = office.documents_dir(folder)
    stem = office.safe_stem(filename or title, "agent_output")
    path = office.unique_path(out_dir, stem, "docx")
    doc.save(str(path))
    return office.finish(path, "docx", open_after=False,
                         extra={"title": title})


def _record_artifact(result: Dict[str, Any], agent: str, request: str, model: str = "",
                     sources: Optional[List[str]] = None) -> None:
    try:
        from core.artifacts import register_artifact
        if result.get("saved"):
            register_artifact(result["saved"], kind=Path(result["saved"]).suffix.lstrip("."),
                              creator_agent=agent, request=request, model=model,
                              sources=sources or [], verified=bool(result.get("verified", True)))
    except Exception as error:
        logger.debug(f"artifact not registered: {error}")


# ---------------------------------------------------------- extraction ----

_THICKNESS = re.compile(r"(?i)(?:thickness|wt|t\s*min|t\s*measured|reading)[^\d\n]{0,25}"
                        r"(\d{1,3}(?:\.\d{1,3})?)\s*mm")
_PRESSURE = re.compile(r"(?i)(?:pressure|mawp|design pressure|operating pressure)[^\d\n]{0,25}"
                       r"(\d{1,4}(?:\.\d{1,3})?)\s*(bar|barg|psi|kpa|mpa)")
_TEMPERATURE = re.compile(r"(?i)(?:temperature|temp)[^\d\n]{0,25}(-?\d{1,4}(?:\.\d)?)\s*(?:°\s*)?(c|f)\b")
_YEARS = re.compile(r"(?i)(\d{1,2}(?:\.\d)?)\s*(?:years|yrs)\b")
_DEFECT_WORDS = re.compile(r"(?i)\b(corrosion|pitting|pits?|cracks?|cracking|cracked|leaks?|leakage|"
                           r"leaking|erosion|dents?|dented|bulg(?:e|es|ing)|thinning|scaling|"
                           r"fouling|vibration|misalign(?:ed|ment)|wear|delamination|"
                           r"blister(?:s|ing)?|deform(?:ed|ation)|rust|spall(?:ing)?)\b")
# "No cracks or leaks detected" is a clean result, not a defect.
_NEGATED = re.compile(r"(?i)\b(no|not|nil|without|none|free of|absence of)\b[^.;\n]{0,40}$")
_SEVERITY = [("critical", re.compile(r"(?i)\b(critical|immediate|unsafe|shut ?down|leak(?:ing)?)\b")),
             ("high", re.compile(r"(?i)\b(severe|significant|below (?:minimum|t\s*min|retirement)|crack)")),
             ("medium", re.compile(r"(?i)\b(moderate|pitting|corrosion|thinning|vibration)\b"))]


def _severity(text: str) -> str:
    for level, pattern in _SEVERITY:
        if pattern.search(text):
            return level
    return "low"


def _sentences_with(pattern: re.Pattern, text: str, limit: int = 8) -> List[str]:
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n", text):
        sentence = sentence.strip()
        if 12 < len(sentence) < 400 and pattern.search(sentence) and sentence not in out:
            out.append(sentence)
        if len(out) >= limit:
            break
    return out


def _evidence_for(sentence: str, evidences) -> List[str]:
    key = sentence[:60].lower()
    return [e.evidence_id for e in evidences if key and key in e.chunk_text.lower()][:2]


# ------------------------------------------------------------ the agents ----

def run_inspection(request, context=None):
    """Inspection report -> OCR/extraction -> schema fields -> deterministic
    measurements and corrosion calculation -> defect findings with evidence ->
    SOP/knowledge retrieval -> grounded assessment -> approval note (unsigned)."""
    from industrial.agent_interfaces import AgentResult, Finding, Recommendation
    from industrial import doc_pipeline, schemas
    from industrial import engineering
    warnings: List[str] = []
    detail: Dict[str, Any] = {"steps": []}

    if not request.document_paths:
        if request.equipment:
            return _inspection_from_records(request)
        return AgentResult(agent_id="inspection", status="blocked",
                           summary="Give the inspection report to read (a PDF, scan, photo "
                                   "or Word file) or an equipment tag.")
    path = request.document_paths[0]
    doc = doc_pipeline.extract_document(path)
    detail["steps"].append({"step": "extract", "summary": doc.summary()})
    if doc.error or not doc.text.strip():
        return AgentResult(agent_id="inspection", status="failed",
                           summary=f"Could not read {Path(path).name}: {doc.error or 'no text found'}",
                           warnings=doc.warnings, detail=detail)
    if doc.is_scanned:
        confidence = doc.mean_ocr_confidence()
        detail["ocr"] = {"pages": doc.scanned_pages, "mean_confidence": confidence}
        if confidence is not None and confidence < 0.75:
            warnings.append(f"OCR confidence is {confidence:.2f} - check every figure "
                            f"against the original scan.")

    match = schemas.classify_document(doc)
    detail["classification"] = match.to_dict()
    fields = {}
    try:
        fields = schemas.extract_fields(doc, "inspection_report")
    except Exception as error:
        warnings.append(f"schema extraction skipped: {error}")
    detail["fields"] = fields

    evidences = _document_evidence(doc)
    text = doc.text
    equipment = request.equipment
    if not equipment:
        from industrial.extraction import find_equipment_tags
        tags = find_equipment_tags(text)
        equipment = tags[0] if tags else None

    findings: List[Finding] = []
    for sentence in _sentences_with(_DEFECT_WORDS, text):
        first_defect = _DEFECT_WORDS.search(sentence)
        negated = bool(first_defect and _NEGATED.search(sentence[:first_defect.start()]))
        findings.append(Finding(statement=sentence,
                                severity="info" if negated else _severity(sentence),
                                category="observation" if negated else "defect",
                                equipment=equipment,
                                evidence_ids=_evidence_for(sentence, evidences),
                                confidence=0.8 if not doc.is_scanned else 0.6))

    thickness = [float(v) for v in _THICKNESS.findall(text)]
    pressures = [(float(v), unit) for v, unit in _PRESSURE.findall(text)]
    calculations = []
    if len(thickness) >= 2:
        years = _YEARS.findall(text)
        interval = float(years[0]) if years else None
        previous, current = max(thickness), min(thickness)
        if interval:
            try:
                calc = engineering.formula("corrosion", {
                    "previous_thickness": f"{previous} mm", "current_thickness": f"{current} mm",
                    "years": str(interval)})
                calculations.append(calc)
                findings.append(Finding(
                    statement=(f"Metal loss from {previous} mm to {current} mm over {interval} years "
                               f"= {calc['result']:.3f} mm/year (computed, verified: "
                               f"{calc['verified']})"),
                    severity="high" if calc["result"] > 0.25 else "medium",
                    category="deviation", equipment=equipment,
                    evidence_ids=[e.evidence_id for e in evidences
                                  if f"{current}" in e.chunk_text][:2],
                    confidence=0.9))
            except Exception as error:
                warnings.append(f"corrosion calculation skipped: {error}")
        else:
            warnings.append("Several thickness readings were found but no inspection interval, "
                            "so no corrosion rate was calculated.")
    detail["measurements"] = {"thickness_mm": thickness, "pressures": pressures}
    detail["calculations"] = calculations

    # Governing procedures and background from the knowledge base.
    query = " ".join(filter(None, [equipment or "", "inspection", " ".join(
        sorted({m.group(1).lower() for m in _DEFECT_WORDS.finditer(text)})[:4])]))
    kb_evidence, info = _kb_search(query, top_k=5)
    if not kb_evidence:
        warnings.append(f"No procedure or standard evidence was retrieved "
                        f"({info.get('error') or info.get('note') or 'knowledge base empty'}).")
    offset = len(evidences)
    for index, item in enumerate(kb_evidence, start=1):
        item.evidence_id = f"E{offset + index}"
    all_evidence = evidences + kb_evidence

    assessment = _grounded(
        "You are the inspection engineer's assistant. You summarise an inspection report "
        "for an approval decision. You never approve anything yourself.",
        "Summarise the condition found, the key measurements, and what the operator should "
        "do next. Separate what the REPORT says from what procedures/background say.",
        all_evidence)
    if assessment.get("answer") is None:
        warnings.append(assessment.get("error", "no model assessment"))

    recommendations: List[Recommendation] = []
    for finding in findings:
        if finding.severity in ("critical", "high"):
            recommendations.append(Recommendation(
                action=f"Engineering review of: {finding.statement[:140]}",
                priority="immediate" if finding.severity == "critical" else "high",
                rationale="severity keywords / computed metal-loss rate in the report",
                evidence_ids=finding.evidence_ids))
    if not recommendations and findings:
        recommendations.append(Recommendation(action="Continue monitoring at the planned interval",
                                              priority="low",
                                              rationale="no high-severity finding detected",
                                              evidence_ids=findings[0].evidence_ids))

    from industrial.provenance import evidence_table
    artifacts: List[str] = []
    if True:            # the approval note is the Inspection Agent's deliverable
        rows = [[f.severity.upper(), f.statement, ", ".join(f.evidence_ids) or "INFERENCE"]
                for f in findings] or [["-", "No defect keywords were found in the report", "-"]]
        sections = [
            ("Source", f"{doc.filename} - {doc.page_count or 1} page(s); read by "
                       f"{', '.join(sorted(set(doc.extraction_methods))) or 'text extraction'}"
                       + (f"; OCR mean confidence {doc.mean_ocr_confidence():.2f}"
                          if doc.mean_ocr_confidence() is not None else "")),
            ("Findings", {"columns": ["Severity", "Finding", "Evidence"], "rows": rows}),
            ("Measurements", [f"Thickness readings: {', '.join(map(str, thickness)) or 'none found'} mm",
                              f"Pressures: {', '.join(f'{v} {u}' for v, u in pressures) or 'none found'}"]
             + [f"{c['name']}: {c['result']:.4g} {c['unit']} (formula {c['formula']}; "
                f"verified {c['verified']})" for c in calculations]),
            ("Assessment (local model, cited)", assessment.get("answer") or
             "Not generated - " + str(assessment.get("error"))),
            ("Recommendations", [f"[{r.priority}] {r.action}" for r in recommendations]
             or ["None"]),
            ("Evidence", {"columns": ["Id", "Source", "Page", "Method"],
                          "rows": [[e["id"], e.get("document"), e.get("page"), e.get("how_read")]
                                   for e in evidence_table(all_evidence)]}),
            ("Approval", "Reviewed by: ____________________   Signature: ______________   "
                         "Date: __________\n\nThis note is NOT approved until signed by an "
                         "authorised person. ZEUS does not approve inspections."),
        ]
        saved = _save_docx(f"Inspection approval note - {equipment or doc.filename}",
                           f"Prepared by ZEUS Inspection Agent on {time.strftime('%Y-%m-%d %H:%M')}",
                           sections, folder="Inspection",
                           filename=f"approval_note_{equipment or Path(doc.filename).stem}")
        if saved.get("saved"):
            artifacts.append(saved["saved"])
            _record_artifact(saved, "inspection", request.task, assessment.get("model", ""),
                             [doc.source_path])
        else:
            warnings.append(f"approval note not written: {saved.get('error')}")

    unsupported = [f for f in findings if not f.evidence_ids]
    status = "ok" if findings and not unsupported else ("partial" if findings else "ok")
    summary = (f"Read {doc.filename}: {len(findings)} finding(s), "
               f"{len(calculations)} verified calculation(s), "
               f"{len(kb_evidence)} procedure/background passage(s). "
               + ("Approval note written (unsigned)." if artifacts else ""))
    return AgentResult(agent_id="inspection", status=status, summary=summary,
                       findings=findings, recommendations=recommendations,
                       evidence=evidence_table(all_evidence),
                       sources=assessment.get("sources", {}), artifacts=artifacts,
                       warnings=warnings, detail=detail)


def _inspection_from_records(request):
    """No document given: summarise the recorded inspection history of a tag."""
    from industrial.agent_interfaces import AgentResult, Finding
    from core.tool_registry import registry
    outcome = registry.execute("inspection_history", equipment=request.equipment)
    if not outcome.success or (isinstance(outcome.output, dict) and outcome.output.get("error")):
        return AgentResult(agent_id="inspection", status="failed",
                           summary=f"No inspection records for {request.equipment}: "
                                   f"{outcome.error or outcome.output.get('error')}")
    records = outcome.output.get("inspections") or outcome.output.get("records") or []
    findings = [Finding(statement=str(r)[:300], category="observation",
                        equipment=request.equipment, evidence_ids=[f"REC{i}"], confidence=1.0)
                for i, r in enumerate(records[:10], start=1)]
    demo = "DEMO" in json.dumps(outcome.output, default=str)
    return AgentResult(agent_id="inspection", status="ok",
                       summary=f"{len(records)} inspection record(s) for {request.equipment}"
                               + (" (DEMO data)" if demo else ""),
                       findings=findings, detail={"records": outcome.output})


def run_document(request, context=None):
    """Read any supported document and summarise it with citations to its own pages."""
    from industrial.agent_interfaces import AgentResult
    from industrial import doc_pipeline, schemas
    from industrial.provenance import evidence_table
    if not request.document_paths:
        return AgentResult(agent_id="industrial_document", status="blocked",
                           summary="Give the document to read.")
    doc = doc_pipeline.extract_document(request.document_paths[0])
    if doc.error or not doc.text.strip():
        return AgentResult(agent_id="industrial_document", status="failed",
                           summary=f"Could not read the document: {doc.error or 'no text found'}",
                           warnings=doc.warnings)
    evidences = _document_evidence(doc, limit=14)
    match = schemas.classify_document(doc)
    task = request.task or "Summarise this document."
    answer = _grounded("You summarise industrial documents precisely for engineers.",
                       f"{task}\nGive: purpose, key facts and figures, obligations or "
                       f"actions, and anything that looks incomplete.", evidences,
                       slot="default")
    warnings = list(doc.warnings)
    if answer.get("answer") is None:
        # Deterministic, clearly labelled extractive fallback.
        headings = [e.text for e in doc.elements if e.element_type == "heading"][:12]
        extract = headings or [e.chunk_text.split("\n")[0][:200] for e in evidences[:8]]
        answer_text = ("EXTRACTIVE OUTLINE (no model available - these are the document's own "
                       "headings/first lines, not a summary):\n- " + "\n- ".join(extract))
        warnings.append(answer.get("error", "no model"))
    else:
        answer_text = answer["answer"]
    artifacts = []
    if request.output_format == "docx":
        saved = _save_docx(f"Summary - {doc.filename}", f"Document type: {match.name}",
                           [("Summary", answer_text),
                            ("Evidence", {"columns": ["Id", "Page", "Method"],
                                          "rows": [[e.evidence_id, e.page_number, e.extraction_method]
                                                   for e in evidences]})],
                           folder="Summaries")
        if saved.get("saved"):
            artifacts.append(saved["saved"])
            _record_artifact(saved, "industrial_document", task, answer.get("model", ""),
                             [doc.source_path])
    return AgentResult(agent_id="industrial_document", status="ok" if answer.get("answer") else "partial",
                       summary=answer_text, evidence=evidence_table(evidences),
                       sources=answer.get("sources", {}), artifacts=artifacts, warnings=warnings,
                       detail={"classification": match.to_dict(), "document": doc.summary(),
                               "grounded": answer.get("grounded"),
                               "injection_flags": answer.get("injection_flags", [])})


_FORMULA_HINTS = [
    ("pump_power", r"pump (?:shaft |hydraulic )?power|power .*pump|kw .*pump"),
    ("pressure_drop", r"pressure drop|friction loss|darcy|head loss"),
    ("corrosion", r"corrosion rate|remaining life|metal loss"),
    ("wall_thickness", r"(?:minimum |required )?wall thickness|barlow thickness|t ?min"),
    ("mawp", r"\bmawp\b|maximum allowable (?:working )?pressure"),
    ("reynolds", r"reynolds"),
    ("heat_duty", r"heat duty|heat load|q ?= ?m ?cp"),
    ("lmtd", r"\blmtd\b|log mean temperature"),
    ("npsh_available", r"npsh"),
    ("velocity", r"(?:flow )?velocity in (?:a |the )?pipe|line velocity"),
    ("head_from_pressure", r"head from pressure|pressure to head|convert .*pressure .* head"),
]


def _inputs_from_text(text: str) -> Dict[str, str]:
    """'flow = 50 m3/h, head 30 m' -> {'flow': '50 m3/h', 'head': '30 m'}."""
    from industrial.engineering import FORMULAS
    names = {name for _, (_, req, opt) in FORMULAS.items() for name in req + opt}
    found = {}
    for name in names:
        label = name.replace("_", "[ _]")
        match = re.search(rf"(?i)\b{label}\s*(?:=|:|of|is)?\s*(-?\d+(?:\.\d+)?\s*[A-Za-z%/0-9\^\*]*)",
                          text)
        if match:
            found[name] = match.group(1).strip().rstrip(",.;")
    return found


def run_calculation(request, context=None):
    """Deterministic: formula library, expression evaluation or equation solving."""
    from industrial.agent_interfaces import AgentResult, Finding
    from industrial import engineering
    task = (request.task or "").strip()
    options = request.options or {}
    try:
        if options.get("formula"):
            result = engineering.formula(options["formula"],
                                         options.get("inputs") or _inputs_from_text(task))
        elif "=" in task and re.search(r"\bsolve\b", task, re.I):
            equation = re.sub(r"(?i)^.*?solve\s*(?:for\s+\w+\s*:?)?", "", task).strip()
            unknown = (re.search(r"(?i)for\s+([a-z]\w*)", task) or re.search(r"([a-z])", equation))
            result = engineering.solve(equation, unknown.group(1))
        else:
            chosen = next((name for name, pattern in _FORMULA_HINTS
                           if re.search(pattern, task, re.I)), None)
            if chosen:
                result = engineering.formula(chosen, _inputs_from_text(task))
            else:
                expression = re.sub(r"(?i)^(calculate|compute|evaluate|what is)\s*", "", task).rstrip("?")
                result = engineering.evaluate(expression)
    except Exception as error:
        return AgentResult(agent_id="calculation", status="failed",
                           summary=f"Could not compute that: {error}",
                           detail={"formulas": engineering.catalogue()})
    if result.get("error"):
        return AgentResult(agent_id="calculation", status="blocked", summary=result["error"],
                           detail=result)
    verified = result.get("verified", False)
    if "result" in result:
        text = (f"{result['name']}: {result['result']:.6g} {result['unit']} "
                f"({result['formula']}); inputs {result['inputs']}; "
                f"{'verified by an independent computation' if verified else 'NOT VERIFIED'}")
    elif "solutions" in result:
        text = "; ".join(f"{result['unknown']} = {s['value']}" for s in result["solutions"]) or "no solution"
    else:
        text = f"{result.get('expression')} = {result.get('numeric')} (exact: {result.get('exact')})"
    return AgentResult(agent_id="calculation", status="ok" if verified or "solutions" in result else "partial",
                       summary=text, findings=[Finding(statement=text, category="observation",
                                                       evidence_ids=["CALC"], confidence=1.0)],
                       detail=result)


def run_engineering(request, context=None):
    """Compute what can be computed, retrieve what the plant documents and the
    corpus say, and explain - keeping computed, documented and background
    information visibly separate."""
    from industrial.agent_interfaces import AgentResult
    calc = run_calculation(request)
    evidences, info = _kb_search(request.task, top_k=6)
    explanation = _grounded(
        "You are a process and mechanical engineering assistant. Explain clearly and "
        "state assumptions. Computed values given to you are authoritative - do not "
        "recompute them.",
        f"Question: {request.task}\nComputed result (deterministic): {calc.summary}",
        evidences) if evidences else {"answer": None,
                                      "error": info.get("error") or "no indexed evidence"}
    parts = [f"COMPUTED: {calc.summary}" if calc.status in ("ok", "partial") else
             f"NOT COMPUTED: {calc.summary}"]
    if explanation.get("answer"):
        parts.append(f"FROM DOCUMENTS AND BACKGROUND:\n{explanation['answer']}")
    else:
        parts.append(f"No document evidence used ({explanation.get('error')}).")
    return AgentResult(agent_id="engineering", status=calc.status,
                       summary="\n\n".join(parts), findings=calc.findings,
                       sources=explanation.get("sources", {}),
                       detail={"calculation": calc.detail, "grounded": explanation.get("grounded")})


def run_coding(request, context=None):
    """Plan -> write -> run in the sandbox -> read the error -> fix -> re-run (max 3
    runs) -> deliver the code, its output and every attempt."""
    from industrial.agent_interfaces import AgentResult
    from security.sandbox import run_python
    from core import office
    task = request.task
    system = ("You write correct, self-contained Python 3 scripts. Standard library, "
              "numpy, pandas, scipy and sympy are available. No network, no subprocess, "
              "no reading files outside the current folder. Print the results. Output ONLY "
              "one ```python code block.")
    code_text, model = _llm("code", system, f"Task: {task}", timeout=240)
    if code_text is None:
        return AgentResult(agent_id="industrial_coding", status="blocked",
                           summary="No local coding model answered - start Ollama and make sure "
                                   "qwen2.5-coder is installed (STEP 1 : INSTALL).")
    attempts = []
    code = _extract_code(code_text)
    for attempt in range(1, 4):
        outcome = run_python(code, timeout=30)
        attempts.append({"attempt": attempt, "exit_code": outcome["exit_code"],
                         "timed_out": outcome["timed_out"], "stdout": outcome["stdout"][-2000:],
                         "stderr": outcome["stderr"][-2000:]})
        if outcome["ok"]:
            break
        if attempt == 3:
            break
        fix, _ = _llm("code", system,
                      f"Task: {task}\n\nThis code:\n```python\n{code}\n```\nfailed with:\n"
                      f"{outcome['stderr'][-1500:] or 'timeout'}\nReturn the corrected full script.",
                      timeout=240)
        if not fix:
            break
        code = _extract_code(fix)
    succeeded = bool(attempts and attempts[-1]["exit_code"] == 0 and not attempts[-1]["timed_out"])
    folder = office.documents_dir("Code")
    path = office.unique_path(folder, office.safe_stem(task, "script")[:40], "py")
    path.write_text(code, encoding="utf-8")
    artifacts = [str(path)]
    _record_artifact({"saved": str(path), "verified": succeeded}, "industrial_coding", task, model)
    summary = (f"{'Ran successfully' if succeeded else 'Did NOT run successfully'} after "
               f"{len(attempts)} attempt(s) in the sandbox. Output:\n"
               f"{attempts[-1]['stdout'][-1500:] if attempts else ''}")
    return AgentResult(agent_id="industrial_coding", status="ok" if succeeded else "failed",
                       summary=summary, artifacts=artifacts,
                       detail={"code": code, "attempts": attempts, "model": model,
                               "sandbox": "separate process, no network, Job Object limits"})


def _extract_code(text: str) -> str:
    match = re.search(r"```(?:python|py)?\s*\n(.*?)```", text or "", re.S)
    return (match.group(1) if match else (text or "")).strip() + "\n"


def run_report_approval(request, context=None):
    """Findings in, verified approval note out. Findings come from the request
    options (e.g. the Inspection Agent's result) or from the plant records."""
    from industrial.agent_interfaces import AgentResult
    options = request.options or {}
    findings = options.get("findings") or []
    if not findings and request.equipment:
        from core.tool_registry import registry
        outcome = registry.execute("approval_note", equipment=request.equipment,
                                   findings=request.task or "", recommendation="")
        if outcome.success and isinstance(outcome.output, dict) and outcome.output.get("saved"):
            _record_artifact(outcome.output, "report_approval", request.task)
            return AgentResult(agent_id="report_approval", status="ok",
                               summary=f"Approval note for {request.equipment} written (unsigned).",
                               artifacts=[outcome.output["saved"]], detail=outcome.output)
        return AgentResult(agent_id="report_approval", status="failed",
                           summary=str(outcome.error or outcome.output))
    if not findings:
        return AgentResult(agent_id="report_approval", status="blocked",
                           summary="Give the findings (or an equipment tag with records) to "
                                   "build the approval note from.")
    rows = [[f.get("severity", ""), f.get("statement", ""), ", ".join(f.get("evidence_ids", []))
             or "INFERENCE"] for f in findings]
    saved = _save_docx(options.get("title") or "Approval note", "Prepared by ZEUS",
                       [("Findings", {"columns": ["Severity", "Finding", "Evidence"], "rows": rows}),
                        ("Approval", "Reviewed by: ____________   Signature: ____________   "
                                     "Date: ________\n\nNot approved until signed.")],
                       folder="Approvals")
    if not saved.get("saved"):
        return AgentResult(agent_id="report_approval", status="failed", summary=str(saved.get("error")))
    _record_artifact(saved, "report_approval", request.task)
    return AgentResult(agent_id="report_approval", status="ok",
                       summary=f"Approval note written with {len(rows)} finding(s) (unsigned).",
                       artifacts=[saved["saved"]])


def run_research(request, context=None):
    """Grounded research over the local knowledge base, with citations."""
    from industrial.agent_interfaces import AgentResult
    try:
        from industrial.knowledge_base import knowledge_base
        answer = knowledge_base.answer(request.task, top_k=8)
    except Exception as error:
        answer = {"answer": None, "error": str(error)}
    if not answer.get("answer"):
        return AgentResult(agent_id="research", status="blocked",
                           summary=f"No grounded answer: {answer.get('error')}",
                           evidence=answer.get("evidence", []))
    artifacts = []
    if request.output_format == "docx":
        saved = _save_docx(f"Research - {request.task[:80]}", "Grounded in local documents",
                           [("Answer", answer["answer"]),
                            ("Sources", [str(s) for s in (answer.get("sources") or {}).get("documents", [])]
                             or ["see evidence table"])], folder="Research")
        if saved.get("saved"):
            artifacts.append(saved["saved"])
            _record_artifact(saved, "research", request.task, answer.get("model", ""))
    return AgentResult(agent_id="research", status="ok" if answer.get("grounded") else "partial",
                       summary=answer["answer"], evidence=answer.get("evidence", []),
                       sources=answer.get("sources", {}), artifacts=artifacts,
                       detail={"model": answer.get("model"),
                               "injection_flags": answer.get("injection_flags", []),
                               "citation_report": answer.get("citation_report")})


def run_planning(request, context=None):
    """Goal -> a dated task plan (validated), optionally written to the task list."""
    from industrial.agent_interfaces import AgentResult
    today = time.strftime("%Y-%m-%d")
    text, model = _llm("json_structured",
                       "You plan industrial work. Return ONLY JSON: {\"tasks\": [{\"title\": str, "
                       "\"owner\": str, \"due\": \"YYYY-MM-DD\", \"priority\": \"high|normal|low\", "
                       "\"depends_on\": [int]}]}. 3 to 10 tasks, realistic dates after " + today + ".",
                       f"Goal: {request.task}", json_format=True, timeout=180)
    plan = _json(text) if text else None
    tasks = (plan or {}).get("tasks") if isinstance(plan, dict) else None
    if not tasks:
        return AgentResult(agent_id="planning", status="blocked",
                           summary="No plan was produced - " + ("the model output was not valid "
                                                                "JSON." if text else "no local model "
                                                                "answered."))
    valid = []
    for index, task in enumerate(tasks, start=1):
        title = str(task.get("title", "")).strip()
        due = str(task.get("due", "")).strip()
        if not title:
            continue
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", due) or due < today:
            due = ""
        valid.append({"n": index, "title": title[:160], "owner": str(task.get("owner", ""))[:60],
                      "due": due, "priority": task.get("priority") if task.get("priority") in
                      ("high", "normal", "low") else "normal", "depends_on": task.get("depends_on") or []})
    created = []
    if (request.options or {}).get("create_tasks"):
        from core.tool_registry import registry
        from security.policy import origin_scope
        with origin_scope("agent"):
            for task in valid:
                outcome = registry.execute("add_todo", text=task["title"], due_date=task["due"] or None,
                                           priority=task["priority"])
                if outcome.success:
                    created.append(task["title"])
    lines = [f"{t['n']}. {t['title']}" + (f" - {t['owner']}" if t['owner'] else "")
             + (f" (due {t['due']})" if t['due'] else "") + f" [{t['priority']}]" for t in valid]
    return AgentResult(agent_id="planning", status="ok",
                       summary="\n".join(lines) + (f"\n\n{len(created)} task(s) added to your list."
                                                   if created else ""),
                       detail={"tasks": valid, "created": created, "model": model})


def run_safety(request, context=None):
    """Safety position: permits, isolations, open safety alerts and the governing
    procedures - reported, never approved."""
    from industrial.agent_interfaces import AgentResult, Finding
    from core.tool_registry import registry
    parts, findings, detail = [], [], {}
    for tool, kwargs in (("safety_status", {}), ("list_permits", {"status": "open"}),
                         ("list_isolations", {})):
        outcome = registry.execute(tool, **kwargs)
        detail[tool] = outcome.output if outcome.success else {"error": outcome.error}
    status = detail.get("safety_status") or {}
    if isinstance(status, dict):
        parts.append(f"Safety status: {json.dumps(status, default=str)[:600]}")
    evidences, info = _kb_search(request.task or "safety procedure permit to work isolation", top_k=5)
    if evidences:
        grounded = _grounded("You are a process-safety assistant. Report; never approve.",
                             request.task or "What procedures govern this work?", evidences)
        if grounded.get("answer"):
            parts.append("Procedures:\n" + grounded["answer"])
    demo = "DEMO" in json.dumps(detail, default=str)
    return AgentResult(agent_id="safety", status="ok",
                       summary=("\n\n".join(parts) or "No safety data recorded.")
                       + ("\n\n(Plant records shown are DEMO data.)" if demo else ""),
                       findings=findings, detail=detail,
                       warnings=["ZEUS never approves permits or releases isolations - a "
                                 "person does, through the approval dialog."])


def run_operations(request, context=None):
    from industrial.agent_interfaces import AgentResult
    from core.tool_registry import registry
    detail = {}
    for tool, kwargs in (("plant_status", {}), ("list_plant_alerts", {"status": "open"}),
                         ("maintenance_due", {"days": 14})):
        outcome = registry.execute(tool, **kwargs)
        detail[tool] = outcome.output if outcome.success else {"error": outcome.error}
    try:
        from industrial.telemetry import telemetry_snapshot
        detail["telemetry"] = telemetry_snapshot()
    except Exception as error:
        detail["telemetry"] = {"error": str(error)}
    demo = "DEMO" in json.dumps(detail, default=str)
    summary = "\n".join(f"{name}: {json.dumps(value, default=str)[:400]}" for name, value in detail.items())
    return AgentResult(agent_id="operations", status="ok",
                       summary=summary + ("\n\n(These figures are DEMO data, not live plant "
                                          "measurements.)" if demo else ""), detail=detail)
