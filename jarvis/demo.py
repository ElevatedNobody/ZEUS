"""
The DEMO scenario "Highway Package A" - clearly labelled DEMO everywhere.

It is not a shortcut around the pipeline: real input files are written to
samples/jarvis/highway_package_a (a baseline and an update schedule, a site
photograph with a signboard, a daily progress report PDF, a Hindi field note,
a contractor's letter, a register of past projects), and the scenario feeds
them through exactly the code an operator uses - capture, OCR, vernacular
reading, extraction, linking, approval, CPM, chronology, notices.

The field records carry the device clock of the day they were captured
(5-10 September 2026), and the personas who "captured" them are recorded as
DEMO:<role>, never as real people. The signed-in operator who loaded the
scenario is in the audit trail.

    Baseline (01 Sep): Bridge Pier P17 01 -> 10 Sep; deck and package finish 27 Sep
    05 Sep  photo + Hinglish caption: P17 excavation complete
    07 Sep  DPR (PDF): P17 PCC complete; P17 concreting delayed - cement not received
    08 Sep  Hindi note: cement supply not received, concreting stopped, supply expected 11 Sep
    09 Sep  contractor's letter: notice of delay under GCC 19.1
    10 Sep  schedule update (data date 11 Sep)
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any, Dict, List, Optional

from jarvis import ORIGIN_DEMO, ORIGIN_SAMPLE, dates, learning, service, store

ROOT = Path(__file__).resolve().parent.parent
SCENARIO_DIR = ROOT / "samples" / "jarvis" / "highway_package_a"
PROJECT_CODE = "HPA-DEMO"
CONTRACTOR = "Vasundhara Infra Ltd (DEMO)"

BASELINE_CSV = f"""# data_date: 2026-09-01
code,name,wbs,wbs name,type,duration,start,finish,predecessors,contractor,location,unit,quantity,aliases
BR-P17-EXC,Foundation excavation - Pier P17,HPA.1.P17,Bridge Pier P17,task,5,2026-09-01,2026-09-05,,{CONTRACTOR},Bridge 1 Km 12.450,m3,420,P17 khudai
BR-P17-PCC,PCC and reinforcement - Pier P17,HPA.1.P17,Bridge Pier P17,task,2,2026-09-06,2026-09-07,BR-P17-EXC,{CONTRACTOR},Bridge 1 Km 12.450,t,14,
BR-P17-CON,Pier concreting - Pier P17,HPA.1.P17,Bridge Pier P17,task,2,2026-09-08,2026-09-09,BR-P17-PCC,{CONTRACTOR},Bridge 1 Km 12.450,m3,180,P17 dhalai; P17 pour
BR-P17-CUR,Curing and deshuttering - Pier P17,HPA.1.P17,Bridge Pier P17,task,1,2026-09-10,2026-09-10,BR-P17-CON,{CONTRACTOR},Bridge 1 Km 12.450,,,
BR-P17-CAP,Pier cap - Pier P17,HPA.1.P17,Bridge Pier P17,task,5,2026-09-11,2026-09-15,BR-P17-CUR,{CONTRACTOR},Bridge 1 Km 12.450,m3,60,
BR-S16-GIR,Girder erection - Span P16-P17,HPA.1.S16,Span P16-P17,task,6,2026-09-16,2026-09-21,BR-P17-CAP,{CONTRACTOR},Bridge 1,nos,5,
BR-S16-DECK,Deck slab - Span P16-P17,HPA.1.S16,Span P16-P17,task,6,2026-09-22,2026-09-27,BR-S16-GIR,{CONTRACTOR},Bridge 1,m3,95,
MS-BR1,Bridge 1 deck complete,HPA.1,Bridge 1,finish_milestone,0,,2026-09-27,BR-S16-DECK,{CONTRACTOR},Bridge 1,,,
RD-DRN,Side drains Km 12-14,HPA.2,Road works,task,8,2026-09-01,2026-09-08,,{CONTRACTOR},Km 12-14,rmt,1600,side drains
RD-GSB,GSB and WMM Km 12-14,HPA.2,Road works,task,10,2026-09-09,2026-09-18,RD-DRN,{CONTRACTOR},Km 12-14,m3,2400,
MS-END,Package A substantial completion,HPA,Highway Package A,finish_milestone,0,,2026-09-27,MS-BR1; RD-GSB,{CONTRACTOR},Km 12-14,,,
"""

UPDATE_CSV = f"""# data_date: 2026-09-11
code,name,wbs,wbs name,type,duration,start,finish,baseline start,baseline finish,actual start,actual finish,% complete,remaining duration,constraint date,predecessors,contractor,location,unit,quantity
BR-P17-EXC,Foundation excavation - Pier P17,HPA.1.P17,Bridge Pier P17,task,5,2026-09-01,2026-09-05,2026-09-01,2026-09-05,2026-09-01,2026-09-05,100,0,,,{CONTRACTOR},Bridge 1 Km 12.450,m3,420
BR-P17-PCC,PCC and reinforcement - Pier P17,HPA.1.P17,Bridge Pier P17,task,2,2026-09-06,2026-09-07,2026-09-06,2026-09-07,2026-09-06,2026-09-07,100,0,,BR-P17-EXC,{CONTRACTOR},Bridge 1 Km 12.450,t,14
BR-P17-CON,Pier concreting - Pier P17,HPA.1.P17,Bridge Pier P17,task,2,2026-09-12,2026-09-13,2026-09-08,2026-09-09,,,0,2,2026-09-12,BR-P17-PCC,{CONTRACTOR},Bridge 1 Km 12.450,m3,180
BR-P17-CUR,Curing and deshuttering - Pier P17,HPA.1.P17,Bridge Pier P17,task,1,2026-09-14,2026-09-14,2026-09-10,2026-09-10,,,0,1,,BR-P17-CON,{CONTRACTOR},Bridge 1 Km 12.450,,
BR-P17-CAP,Pier cap - Pier P17,HPA.1.P17,Bridge Pier P17,task,5,2026-09-15,2026-09-19,2026-09-11,2026-09-15,,,0,5,,BR-P17-CUR,{CONTRACTOR},Bridge 1 Km 12.450,m3,60
BR-S16-GIR,Girder erection - Span P16-P17,HPA.1.S16,Span P16-P17,task,6,2026-09-20,2026-09-25,2026-09-16,2026-09-21,,,0,6,,BR-P17-CAP,{CONTRACTOR},Bridge 1,nos,5
BR-S16-DECK,Deck slab - Span P16-P17,HPA.1.S16,Span P16-P17,task,6,2026-09-26,2026-10-01,2026-09-22,2026-09-27,,,0,6,,BR-S16-GIR,{CONTRACTOR},Bridge 1,m3,95
MS-BR1,Bridge 1 deck complete,HPA.1,Bridge 1,finish_milestone,0,,2026-10-01,,2026-09-27,,,0,0,,BR-S16-DECK,{CONTRACTOR},Bridge 1,,
RD-DRN,Side drains Km 12-14,HPA.2,Road works,task,8,2026-09-01,2026-09-12,2026-09-01,2026-09-08,2026-09-01,,85,2,,,{CONTRACTOR},Km 12-14,rmt,1600
RD-GSB,GSB and WMM Km 12-14,HPA.2,Road works,task,10,2026-09-13,2026-09-22,2026-09-09,2026-09-18,,,0,10,,RD-DRN,{CONTRACTOR},Km 12-14,m3,2400
MS-END,Package A substantial completion,HPA,Highway Package A,finish_milestone,0,,2026-10-01,,2026-09-27,,,0,0,,MS-BR1; RD-GSB,{CONTRACTOR},Km 12-14,,
"""

HISTORY_CSV = """# SAMPLE DATA - illustrative past projects for the DEMO scenario; not company records.
project,code,activity,category,unit,quantity,planned duration,actual start,actual finish,contractor,location,conditions,blockers,evidence
NH-44 Package 2 (SAMPLE),P09-CON,Pier concreting - Pier P09,concreting,m3,176,2,2024-08-12,2024-08-14,Vasundhara Infra Ltd (SAMPLE),Km 88,monsoon,rain,6
NH-44 Package 2 (SAMPLE),P10-CON,Pier concreting - Pier P10,concreting,m3,176,2,2024-08-20,2024-08-21,Vasundhara Infra Ltd (SAMPLE),Km 88,monsoon,,4
Expressway Pkg 5 (SAMPLE),P21-CON,Pier concreting - Pier P21,concreting,m3,190,2,2023-11-03,2023-11-06,Kaveri Buildcon (SAMPLE),Km 41,post-monsoon,cement supply,5
Expressway Pkg 5 (SAMPLE),P22-CON,Pier concreting - Pier P22,concreting,m3,190,2,2023-11-10,2023-11-11,Kaveri Buildcon (SAMPLE),Km 41,post-monsoon,,3
SH-17 Bridge (SAMPLE),P05-EXC,Foundation excavation - Pier P05,excavation,m3,400,5,2022-03-01,2022-03-06,Narmada Constructions (SAMPLE),Km 7,summer,,2
SH-17 Bridge (SAMPLE),P05-CAP,Pier cap - Pier P05,concreting,m3,58,5,2022-03-20,2022-03-26,Narmada Constructions (SAMPLE),Km 7,summer,shuttering shortage,3
"""

HINDI_NOTE = "पियर P17: सीमेंट की आपूर्ति नहीं हुई, ढलाई रुकी हुई है। सप्लाई 11 सितंबर तक अपेक्षित।"
PHOTO_CAPTION = "P17 khudai poori ho gayi, PCC kal se"
DPR_LINES = [
    "DAILY PROGRESS REPORT (DEMO) - Highway Package A",
    f"Contractor: {CONTRACTOR}",
    "Date: 07/09/2026",
    "Pier P17: PCC and reinforcement completed on 07/09/2026.",
    "Pier P17 concreting delayed - cement not received, supplier truck held at the check-post.",
    "Side drains Km 12-14: 70% complete.",
    "Prepared by: Site office (DEMO)",
]
LETTER = [
    "Vasundhara Infra Ltd (DEMO)",
    "Ref: VIL/HPA/2026/112",
    "Date: 09/09/2026",
    "To: The Project Director, Highway Package A PIU (DEMO)",
    "From: Vasundhara Infra Ltd (Contractor, DEMO)",
    "Subject: Notice of delay - Pier P17 concreting - cement supply",
    "Dear Sir,",
    "This is to notify you that the concreting of Pier P17 could not proceed on 08/09/2026 owing to "
    "non-availability of cement at site. The supplier has confirmed dispatch.",
    "We expect to resume concreting on 12/09/2026 and to complete Pier P17 by 14/09/2026.",
    "This letter is notice of a delay event under Clause 19.1 of the Contract.",
    "Yours faithfully,",
    "Project Manager, Vasundhara Infra Ltd (DEMO)",
]


def _font(size: int):
    from PIL import ImageFont
    for name in ("arialbd.ttf", "arial.ttf", "segoeuib.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _photo(path: Path) -> None:
    """A synthetic site photograph (DEMO): ground, an excavated pit, and a
    signboard whose text the OCR engine must read."""
    from PIL import Image, ImageDraw
    image = Image.new("RGB", (1280, 860), (150, 170, 190))
    draw = ImageDraw.Draw(image)
    for y in range(360, 860):
        shade = 110 + (y - 360) // 12
        draw.line([(0, y), (1280, y)], fill=(shade, shade - 25, shade - 55))
    draw.polygon([(300, 560), (980, 560), (900, 780), (380, 780)], fill=(78, 62, 45))
    draw.rectangle([380, 250, 940, 470], fill=(250, 250, 245), outline=(30, 30, 30), width=6)
    draw.rectangle([640, 470, 670, 560], fill=(90, 90, 90))
    big, small = _font(54), _font(40)
    draw.text((420, 275), "PIER P17", fill=(10, 10, 10), font=big)
    draw.text((420, 345), "EXCAVATION COMPLETE", fill=(10, 10, 10), font=small)
    draw.text((420, 400), "05/09/2026", fill=(10, 10, 10), font=small)
    draw.text((20, 820), "DEMO IMAGE - synthetic", fill=(240, 240, 240), font=_font(24))
    exif = Image.Exif()
    exif[306] = "2026:09:05 17:32:00"
    image.save(path, format="JPEG", quality=90, exif=exif.tobytes())


def _pdf(path: Path, lines: List[str]) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path), pagesize=A4)
    y = 800
    for line in lines:
        c.setFont("Helvetica-Bold" if y == 800 else "Helvetica", 12 if y == 800 else 11)
        c.drawString(56, y, line)
        y -= 26
    c.save()


def _docx(path: Path, lines: List[str]) -> None:
    from docx import Document
    document = Document()
    for line in lines:
        document.add_paragraph(line)
    document.core_properties.author = "DEMO"
    document.save(str(path))


def ensure_samples(folder: Optional[Path] = None, force: bool = False) -> Dict[str, Path]:
    folder = Path(folder or SCENARIO_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    files = {"baseline": folder / "01_baseline_schedule_2026-09-01.csv",
             "photo": folder / "02_site_photo_P17_2026-09-05.jpg",
             "dpr": folder / "03_DPR_2026-09-07.pdf",
             "hindi": folder / "04_field_note_hindi_2026-09-08.txt",
             "letter": folder / "05_contractor_letter_2026-09-09.docx",
             "update": folder / "06_schedule_update_2026-09-10.csv",
             "history": folder / "07_historical_activities_SAMPLE.csv"}
    writers = {"baseline": lambda p: p.write_text(BASELINE_CSV, encoding="utf-8"),
               "photo": _photo, "dpr": lambda p: _pdf(p, DPR_LINES),
               "hindi": lambda p: p.write_text(HINDI_NOTE + "\n", encoding="utf-8"),
               "letter": lambda p: _docx(p, LETTER),
               "update": lambda p: p.write_text(UPDATE_CSV, encoding="utf-8"),
               "history": lambda p: p.write_text(HISTORY_CSV, encoding="utf-8")}
    for key, path in files.items():
        if force or not path.exists():
            writers[key](path)
    readme = folder / "README.md"
    if force or not readme.exists():
        readme.write_text("# Highway Package A - DEMO scenario inputs\n\nSynthetic, clearly labelled DEMO data for "
                          "ZEUS. Loaded by `seed_jarvis_demo` (Projects screen -> Load DEMO) through the normal "
                          "capture pipeline. Not real project records.\n", encoding="utf-8")
    return files


def _approve_all(evidence_result: Dict[str, Any], at: str, actor: str, device: str) -> List[Dict[str, Any]]:
    out = []
    for item in evidence_result.get("events", []):
        if item["status"] == "submitted":
            out.append(service.approve_update(item["event_id"], True, "DEMO scenario review", captured_at=at,
                                              device=device, actor=actor))
    return out


def load(use_model: bool = False, folder: Optional[Path] = None, code: str = PROJECT_CODE) -> Dict[str, Any]:
    """Create the DEMO project and replay the scenario through the pipeline."""
    service.require_role("engineer", "load the DEMO scenario")
    files = ensure_samples(folder)
    if store.one("SELECT 1 FROM jv_projects WHERE code = ?", (code,)):
        raise ValueError(f"the DEMO project {code} already exists")
    office, tab1, tab2 = "HPA-OFFICE-PC (DEMO)", "HPA-TAB-01 (DEMO)", "HPA-TAB-02 (DEMO)"
    planner, engineer, foreman, clerk = ("DEMO:planning.engineer", "DEMO:site.engineer.p17", "DEMO:foreman.ravi",
                                         "DEMO:document.controller")
    project = service.create_project(code, "Highway Package A (DEMO)", client="Highway Authority PIU (DEMO)",
                                     contractor=CONTRACTOR, location="NH Km 12-14", start_date="2026-09-01",
                                     description="DEMO scenario - synthetic data", origin=ORIGIN_DEMO)
    pid = project["project_id"]
    with store.connection() as conn:
        for clause, title, kind, days, triggers in (
                ("GCC 19.1", "Notice of a delay event", "delay", 7, ["blocker_reported", "delay_reported"]),
                ("GCC 19.2", "Detailed particulars of claim", "claim_particulars", 28, ["blocker_reported"])):
            store.insert(conn, "jv_contract_refs", {
                "ref_id": store.new_id("REF"), "project_id": pid, "clause": clause, "title": title,
                "notice_type": kind, "notice_period_days": days, "trigger_types": store.dumps(triggers),
                "recipient": "Employer (PIU)", "description": f"{title} within {days} days (DEMO contract)",
                "origin": ORIGIN_DEMO, "created_by": store.current_user(), "created_at": store.now()})
    steps: Dict[str, Any] = {"project": project}
    steps["baseline"] = service.import_schedule(pid, str(files["baseline"]), kind="baseline", label="Baseline 01 Sep 2026",
                                                captured_at="2026-09-01T09:00:00", device=office, actor=planner)
    steps["baseline_approval"] = service.approve_version(steps["baseline"]["version_id"], True, "baseline agreed",
                                                         captured_at="2026-09-01T09:30:00", device=office, actor=planner)
    steps["photo"] = service.capture_update(pid, path=str(files["photo"]), text=PHOTO_CAPTION, source_type="field",
                                            captured_at="2026-09-05T17:40:00", device=tab1, actor=engineer,
                                            use_model=use_model, use_vision=use_model)
    steps["photo_approvals"] = _approve_all(steps["photo"], "2026-09-05T18:30:00", planner, office)
    steps["dpr"] = service.capture_update(pid, path=str(files["dpr"]), source_type="contractor",
                                          captured_at="2026-09-07T19:05:00", device=office,
                                          actor="DEMO:contractor.site.office", use_model=use_model)
    steps["dpr_approvals"] = _approve_all(steps["dpr"], "2026-09-07T19:40:00", planner, office)
    steps["hindi"] = service.capture_update(pid, text=files["hindi"].read_text(encoding="utf-8").strip(),
                                            source_type="field", captured_at="2026-09-08T11:20:00", device=tab2,
                                            actor=foreman, use_model=use_model, title="Foreman note (Hindi)")
    steps["hindi_approvals"] = _approve_all(steps["hindi"], "2026-09-08T12:05:00", planner, office)
    steps["letter"] = service.capture_update(pid, path=str(files["letter"]), source_type="contractor",
                                             captured_at="2026-09-09T16:10:00", device=office, actor=clerk,
                                             correspondence=True, use_model=use_model)
    steps["update"] = service.import_schedule(pid, str(files["update"]), kind="update", label="Update 10 Sep 2026",
                                              captured_at="2026-09-10T18:00:00", device=office, actor=planner)
    steps["update_approval"] = service.approve_version(steps["update"]["version_id"], True, "update accepted",
                                                       captured_at="2026-09-10T18:30:00", device=office, actor=planner)
    steps["history"] = learning.import_history(str(files["history"]), origin=ORIGIN_SAMPLE)
    steps["derived_history"] = learning.derive_history(pid)
    service.set_current_project(pid)
    store.audit("seed_jarvis_demo", "project", pid, pid, {"files": [p.name for p in files.values()]})
    return {"ok": True, "project_id": pid, "code": code, "label": "DEMO", "steps": _summary(steps)}


def _summary(steps: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for key, value in steps.items():
        if isinstance(value, dict):
            out[key] = {k: value.get(k) for k in ("ok", "evidence_id", "version_id", "events", "observations",
                                                  "warnings", "status", "added", "project_id") if k in value}
            if "observations" in out[key]:
                out[key]["observations"] = [{"kind": o["kind"], "link": o["link"]["status"],
                                             "activity": (o["link"]["candidates"] or [{}])[0].get("code")}
                                            for o in value["observations"]]
        elif isinstance(value, list):
            out[key] = [{"decision": v.get("decision"), "forecast_change": bool((v.get("forecast") or {}).get("change"))}
                        for v in value]
        else:
            out[key] = value
    return out
