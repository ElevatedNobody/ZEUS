"""
Meridian Coastal Refinery — a complete fictional dataset.

Everything here is INVENTED. There is no Meridian Petrochemicals Limited, no
Vasant Point, and no employee named in this file exists. Every row is marked
`source = 'DEMO'` in the database so any part of ZEUS can tell seeded
demonstration data apart from something a real operator entered, and so the
whole lot can be removed in one statement.

Why it exists
-------------
ZEUS could already answer questions about plant equipment, and could already
build reports and decks from plant data. It just had nothing to answer them
ABOUT: the tables were empty, so every industrial question came back with
nothing found, and every generated deliverable had nothing real to put in it.

This is the corpus that makes those features demonstrable. It is deliberately
INTERNALLY CONSISTENT rather than merely plausible - the maintenance history
of P-101 explains its condition, its vibration readings trend the way its
findings describe, the spare it consumed is missing from stores, and the
inspector who signed its report is in the employee directory in a role that
would actually sign it. Data that contradicts itself is worse than no data,
because the answers built on it are confidently wrong.

Scale
-----
Sized so a laptop can carry it: a few hundred equipment-linked records and
about two thousand readings. Enough for trends, aggregates and comparisons;
small enough that seeding takes a second or two.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from typing import Any, Dict, List

DEMO = "DEMO"

# One seed, so the same dataset is produced on every machine. A demo where the
# numbers change between runs cannot be documented or tested against.
_RANDOM = random.Random(20260101)

TODAY = date(2026, 9, 1)


# ============================================================== company ====

COMPANY = {
    "name": "Meridian Petrochemicals Limited",
    "short_name": "Meridian",
    "site": "Meridian Coastal Refinery",
    "location": "Vasant Point, Konkan Coast, India",
    "founded": "1987",
    "capacity_mmtpa": 9.6,
    "capacity_bpd": 193_000,
    "complexity_nelson": 9.4,
    "employees": 1_840,
    "shift_pattern": "3 x 8h rotating, 4 crews",
    "certifications": "ISO 9001:2015, ISO 14001:2015, ISO 45001:2018, OHSAS",
    "crude_slate": "Arab Medium, Basrah Light, Murban, domestic offshore",
    "products": ("LPG, Naphtha, Motor Spirit, Superior Kerosene, ATF, "
                 "High Speed Diesel, Bitumen, Sulphur, Petcoke"),
    "safety_philosophy": (
        "Process safety is managed to a hierarchy of controls: eliminate, "
        "substitute, engineer, administrate, protect. No production target "
        "overrides a safety-critical isolation. Every permit is signed by a "
        "competent person, and every deviation is recorded whether or not it "
        "resulted in harm."),
    "maintenance_strategy": (
        "Risk-based. Criticality is assessed from consequence x likelihood; "
        "critical rotating equipment carries condition monitoring with "
        "vibration and bearing-temperature trending, static equipment follows "
        "a thickness-survey interval set by corrosion rate, and non-critical "
        "assets run to failure with held spares."),
    "note": "SAMPLE / DEMO DATA — a fictional organisation for demonstration.",
}

UNITS = [
    ("CDU-1", "Crude Distillation Unit 1", 4.8, "Atmospheric crude separation", 1987),
    ("CDU-2", "Crude Distillation Unit 2", 4.8, "Atmospheric crude separation", 2004),
    ("VDU-1", "Vacuum Distillation Unit", 2.6, "Vacuum gas oil recovery", 1987),
    ("FCC-1", "Fluid Catalytic Cracker", 2.2, "Heavy gas oil conversion", 1996),
    ("HDT-1", "Diesel Hydrotreater", 2.4, "Sulphur removal from distillate", 2009),
    ("HDT-2", "Naphtha Hydrotreater", 1.4, "Feed preparation for reforming", 2009),
    ("CRU-1", "Catalytic Reformer", 1.1, "Octane upgrading, hydrogen production", 1996),
    ("HGU-1", "Hydrogen Generation Unit", 0.09, "Steam methane reforming", 2009),
    ("SRU-1", "Sulphur Recovery Unit", 0.11, "Claus sulphur recovery", 2009),
    ("UTL-1", "Utilities and Offsites", 0.0, "Steam, power, air, nitrogen", 1987),
    ("TKF-1", "Tank Farm and Blending", 0.0, "Intermediate and product storage", 1987),
    ("ETP-1", "Effluent Treatment Plant", 0.0, "Waste water treatment", 1992),
]

DEPARTMENTS = [
    "Refinery Operations", "Process Engineering", "Mechanical Maintenance",
    "Electrical Maintenance", "Instrumentation & Control", "Inspection",
    "Reliability", "HSE", "Utilities", "Laboratory", "Projects",
    "Procurement", "Stores", "IT / OT", "Finance", "Administration",
    "Emergency Response",
]


# ============================================================ equipment ====
# tag, name, type, unit, criticality, manufacturer, model, installed, extras

_EQUIPMENT_SPEC: List[tuple] = [
    # --- rotating: pumps ---
    ("P-101", "CDU-1 Crude Charge Pump A", "centrifugal_pump", "CDU-1", "critical",
     "Kirloskar", "KDS 200-500", "1987-11-02",
     {"duty": "Crude charge to CDU-1 preheat train", "rated_flow_m3h": 640,
      "rated_head_m": 210, "driver": "M-101 induction motor 450 kW",
      "seal": "API Plan 53B dual pressurised", "spare": "P-102"}),
    ("P-102", "CDU-1 Crude Charge Pump B", "centrifugal_pump", "CDU-1", "critical",
     "Kirloskar", "KDS 200-500", "1987-11-02",
     {"duty": "Standby to P-101", "rated_flow_m3h": 640, "rated_head_m": 210,
      "driver": "M-102 induction motor 450 kW", "spare": "P-101"}),
    ("P-103", "VDU Bottoms Pump", "centrifugal_pump", "VDU-1", "high",
     "Flowserve", "HPX 6x8-13", "1998-03-14",
     {"duty": "Vacuum residue transfer, 340 degC", "rated_flow_m3h": 210,
      "seal": "API Plan 32 with quench"}),
    ("P-204A", "FCC Slurry Pump A", "centrifugal_pump", "FCC-1", "critical",
     "Flowserve", "HPX 8x10-15", "1996-06-20",
     {"duty": "Main column slurry circulation", "rated_flow_m3h": 480,
      "service": "Erosive catalyst-laden", "spare": "P-204B"}),
    ("P-204B", "FCC Slurry Pump B", "centrifugal_pump", "FCC-1", "critical",
     "Flowserve", "HPX 8x10-15", "1996-06-20",
     {"duty": "Standby to P-204A", "rated_flow_m3h": 480, "spare": "P-204A"}),
    ("P-305", "Diesel Hydrotreater Charge Pump", "centrifugal_pump", "HDT-1", "high",
     "Sulzer", "MSD-D 6x8", "2009-08-01",
     {"duty": "Feed to reactor at 68 barg", "rated_flow_m3h": 290}),
    ("P-410", "Cooling Water Circulation Pump A", "centrifugal_pump", "UTL-1", "high",
     "KSB", "Omega 300-670", "1987-11-02",
     {"duty": "Circulating cooling water", "rated_flow_m3h": 4200}),
    ("P-411", "Cooling Water Circulation Pump B", "centrifugal_pump", "UTL-1", "medium",
     "KSB", "Omega 300-670", "1987-11-02", {"duty": "Standby", "rated_flow_m3h": 4200}),
    ("P-520", "Firewater Jockey Pump", "centrifugal_pump", "UTL-1", "critical",
     "Grundfos", "NK 80-250", "2012-02-10",
     {"duty": "Firewater ring main pressurisation", "safety_critical": True}),
    ("P-601", "Effluent Transfer Pump", "centrifugal_pump", "ETP-1", "low",
     "Kirloskar", "DB 100-315", "1992-07-19", {"duty": "ETP inlet transfer"}),

    # --- rotating: compressors and turbines ---
    ("C-201", "FCC Wet Gas Compressor", "centrifugal_compressor", "FCC-1", "critical",
     "Elliott", "38M9", "1996-06-20",
     {"duty": "Main column overhead gas", "driver": "ST-201 steam turbine",
      "stages": 2, "rated_flow_nm3h": 62_000, "surge_control": "Anti-surge on FIC-2011"}),
    ("C-202", "Instrument Air Compressor A", "screw_compressor", "UTL-1", "critical",
     "Atlas Copco", "ZR 315", "2011-04-12",
     {"duty": "Instrument air, oil-free", "rated_flow_nm3h": 3_200,
      "safety_critical": True}),
    ("C-203", "Instrument Air Compressor B", "screw_compressor", "UTL-1", "high",
     "Atlas Copco", "ZR 315", "2011-04-12", {"duty": "Standby"}),
    ("C-301", "Hydrogen Make-up Compressor", "reciprocating_compressor", "HDT-1",
     "critical", "Burckhardt", "2K90-2A", "2009-08-01",
     {"duty": "Make-up hydrogen to reactor", "discharge_barg": 78, "stages": 2}),
    ("ST-201", "FCC Wet Gas Turbine Driver", "steam_turbine", "FCC-1", "critical",
     "Elliott", "YR", "1996-06-20", {"duty": "Drives C-201", "rated_kw": 4_100}),

    # --- static: exchangers, vessels, columns ---
    ("E-301", "CDU-1 Crude Preheat Exchanger", "shell_tube_exchanger", "CDU-1", "high",
     "Godrej", "AES 1200-6000", "1987-11-02",
     {"duty": "Crude vs kerosene pumparound", "area_m2": 640,
      "design_barg": 22, "tube_material": "SA-179"}),
    ("E-302", "CDU-1 Overhead Condenser", "air_cooled_exchanger", "CDU-1", "high",
     "GEA", "ACHE 4-bay", "1987-11-02",
     {"duty": "Naphtha condensation", "bays": 4, "fans": 8}),
    ("E-405", "FCC Main Column Bottoms Cooler", "shell_tube_exchanger", "FCC-1",
     "medium", "Godrej", "BEM 900-4500", "1996-06-20",
     {"duty": "Slurry cooling", "area_m2": 310, "fouling": "High, catalyst fines"}),
    ("V-401", "CDU-1 Overhead Reflux Drum", "pressure_vessel", "CDU-1", "high",
     "L&T", "HZ-2400", "1987-11-02",
     {"design_barg": 12, "diameter_mm": 2400, "material": "SA-516 Gr70",
      "relief": "PSV-4011 set 11.5 barg"}),
    ("V-402", "FCC Slurry Settler", "pressure_vessel", "FCC-1", "medium",
     "L&T", "HZ-1800", "1996-06-20", {"design_barg": 6, "diameter_mm": 1800}),
    ("V-510", "Hydrogen KO Drum", "pressure_vessel", "HDT-1", "critical",
     "L&T", "VT-1600", "2009-08-01",
     {"design_barg": 85, "material": "SA-516 Gr70 + PWHT",
      "service": "Wet H2S - HIC susceptible"}),
    ("T-101", "CDU-1 Atmospheric Column", "distillation_column", "CDU-1", "critical",
     "L&T", "AC-4800", "1987-11-02",
     {"trays": 48, "diameter_mm": 4800, "height_m": 52,
      "overhead_degc": 128, "bottoms_degc": 352}),
    ("T-201", "FCC Main Fractionator", "distillation_column", "FCC-1", "critical",
     "L&T", "MF-5200", "1996-06-20",
     {"trays": 32, "diameter_mm": 5200, "height_m": 44}),
    ("B-701", "Utility Boiler 1", "boiler", "UTL-1", "critical",
     "Thermax", "WB-120", "1992-05-30",
     {"steam_tph": 120, "pressure_barg": 42, "fuel": "Refinery fuel gas"}),
    ("B-702", "Utility Boiler 2", "boiler", "UTL-1", "high",
     "Thermax", "WB-120", "1992-05-30", {"steam_tph": 120, "pressure_barg": 42}),
    ("F-101", "CDU-1 Crude Heater", "fired_heater", "CDU-1", "critical",
     "Furnace Fabrica", "VC-4P", "1987-11-02",
     {"duty_mw": 58, "passes": 4, "fuel": "Refinery fuel gas + fuel oil",
      "coil_material": "9Cr-1Mo"}),
    ("TK-801", "Crude Storage Tank 1", "storage_tank", "TKF-1", "high",
     "L&T", "FR-48000", "1987-11-02",
     {"capacity_m3": 48_000, "roof": "External floating", "product": "Crude oil"}),
    ("TK-802", "Crude Storage Tank 2", "storage_tank", "TKF-1", "high",
     "L&T", "FR-48000", "1987-11-02", {"capacity_m3": 48_000, "roof": "External floating"}),
    ("TK-810", "HSD Product Tank", "storage_tank", "TKF-1", "medium",
     "L&T", "CR-22000", "1996-03-11",
     {"capacity_m3": 22_000, "roof": "Internal floating", "product": "High Speed Diesel"}),
    ("M-101", "P-101 Drive Motor", "electric_motor", "CDU-1", "critical",
     "ABB", "M3BP 450", "1987-11-02", {"rated_kw": 450, "voltage_v": 6_600}),
    ("M-204", "P-204A Drive Motor", "electric_motor", "FCC-1", "critical",
     "ABB", "M3BP 355", "1996-06-20", {"rated_kw": 355, "voltage_v": 6_600}),
    ("PSV-4011", "V-401 Relief Valve", "relief_valve", "CDU-1", "critical",
     "Anderson Greenwood", "Series 500", "1987-11-02",
     {"set_barg": 11.5, "orifice": "J", "safety_critical": True}),
    ("PSV-5101", "V-510 Relief Valve", "relief_valve", "HDT-1", "critical",
     "Anderson Greenwood", "Series 500", "2009-08-01",
     {"set_barg": 84, "orifice": "L", "safety_critical": True}),
    ("FL-901", "Elevated Flare", "flare", "UTL-1", "critical",
     "Zeeco", "EF-42", "1987-11-02", {"tip_diameter_mm": 1_050, "height_m": 92}),
]

# Where each asset stands today. Anything not listed runs normally.
_STATUS_OVERRIDE = {
    "P-101": "maintenance",     # the worked example - see the history below
    "P-204B": "standby",
    "P-102": "standby",
    "P-411": "standby",
    "C-203": "standby",
    "B-702": "standby",
    "E-405": "degraded",        # fouling, cleaning scheduled
    "TK-802": "out_of_service", # internal inspection
}


def equipment_rows() -> List[Dict[str, Any]]:
    rows = []
    for (tag, name, kind, unit, criticality, maker, model,
         installed, extras) in _EQUIPMENT_SPEC:
        rows.append({
            "tag": tag, "name": name, "equipment_type": kind,
            "unit": unit, "location": f"{unit} area",
            "status": _STATUS_OVERRIDE.get(tag, "running"),
            "criticality": criticality, "manufacturer": maker, "model": model,
            "serial_number": f"MPL-{tag.replace('-', '')}-{abs(hash(tag)) % 90000 + 10000}",
            "installed_on": installed, "attributes": extras,
        })
    return rows


# ====================================================== the P-101 story ====
# One asset is documented end to end, because a demonstration needs a thread
# somebody can actually pull. Every record below is consistent with every
# other: the vibration trend rises through the readings, the June inspection
# finds what that trend implies, the August work order consumes the bearing
# that stores is now short of, and the inspector who signed it holds a role
# that would sign it.

P101_STORY = {
    "tag": "P-101",
    "summary": (
        "Progressive bearing deterioration on the coupling-end of the CDU-1 "
        "crude charge pump, first visible as a rising vibration trend in "
        "March 2026, confirmed by inspection in June, and taken out of "
        "service for bearing replacement on 2026-08-24."),
}

MAINTENANCE: List[Dict[str, Any]] = [
    # --- P-101, in order ---
    dict(equipment_tag="P-101", performed_on="2024-03-18", maintenance_type="preventive",
         description="Annual overhaul: bearings inspected, seal plan flushed, alignment checked",
         technician="R. Deshmukh", duration_hours=26.0,
         parts_used="Mechanical seal kit MS-200-53B; gaskets",
         findings="Coupling-end bearing within tolerance. Alignment 0.04 mm, acceptable.",
         status="completed", next_due="2025-03-18", work_order="WO-2024-0412"),
    dict(equipment_tag="P-101", performed_on="2025-03-22", maintenance_type="preventive",
         description="Annual overhaul: bearings, seal, alignment, baseplate grout check",
         technician="R. Deshmukh", duration_hours=28.5,
         parts_used="Mechanical seal kit MS-200-53B; bearing grease NLGI 2",
         findings=("Coupling-end bearing showing early raceway marking, no measurable "
                   "clearance increase. Flagged for vibration monitoring at 3-month "
                   "interval rather than 6."),
         status="completed", next_due="2026-03-22", work_order="WO-2025-0388"),
    dict(equipment_tag="P-101", performed_on="2026-04-02", maintenance_type="predictive",
         description="Vibration survey following trend alarm on VIB-101",
         technician="S. Iyer (Reliability)", duration_hours=3.0, parts_used="",
         findings=("Overall velocity 4.9 mm/s RMS, up from 2.8 mm/s in January. "
                   "Spectrum shows 1x and 2x BPFO at 162 Hz consistent with outer-race "
                   "defect on the coupling-end bearing. Not yet at the 7.1 mm/s "
                   "ISO 10816-3 Zone C boundary."),
         status="completed", next_due="2026-06-01", work_order="WO-2026-0201"),
    dict(equipment_tag="P-101", performed_on="2026-08-24", maintenance_type="corrective",
         description=("Bearing replacement, coupling-end. Pump isolated under PTW-2026-0912 "
                      "and LOTO-2026-0455; duty transferred to P-102."),
         technician="R. Deshmukh, A. Fernandes", duration_hours=34.0,
         parts_used="Bearing SKF 6317 C3 (BRG-6317-C3) x1; seal kit MS-200-53B; O-rings",
         findings=("Outer race spalled over approximately 25 mm of the load zone, "
                   "consistent with the June inspection. Shaft journal within tolerance, "
                   "no scoring. Housing bore 0.02 mm oversize, within limit. New bearing "
                   "fitted, alignment set to 0.03 mm. Awaiting run-in and vibration "
                   "verification before return to duty."),
         status="in_progress", next_due="2026-09-05", work_order="WO-2026-0876"),

    # --- everything else ---
    dict(equipment_tag="P-204A", performed_on="2026-02-14", maintenance_type="corrective",
         description="Casing wear ring replacement following efficiency loss",
         technician="A. Fernandes", duration_hours=19.0,
         parts_used="Wear ring set WR-8x10; gasket set",
         findings="Erosion of wear rings to 3.2 mm clearance against 1.5 mm design. "
                  "Typical for slurry service at this run hours.",
         status="completed", next_due="2027-02-14", work_order="WO-2026-0118"),
    dict(equipment_tag="C-201", performed_on="2026-01-20", maintenance_type="preventive",
         description="Anti-surge valve stroke test and controller verification",
         technician="D. Menon (I&C)", duration_hours=6.0, parts_used="",
         findings="Valve strokes full travel in 1.8 s against 2.0 s requirement. "
                  "Controller response verified against surge line. Acceptable.",
         status="completed", next_due="2026-07-20", work_order="WO-2026-0064"),
    dict(equipment_tag="C-201", performed_on="2026-07-18", maintenance_type="preventive",
         description="Anti-surge valve stroke test and controller verification",
         technician="D. Menon (I&C)", duration_hours=5.5, parts_used="",
         findings="Stroke time 1.9 s. No drift since January. Acceptable.",
         status="completed", next_due="2027-01-18", work_order="WO-2026-0733"),
    dict(equipment_tag="E-405", performed_on="2026-05-11", maintenance_type="corrective",
         description="Hydrojet cleaning of shell side, catalyst fines fouling",
         technician="Contract - Hydroblast Services", duration_hours=42.0,
         parts_used="Shell gaskets; channel cover gasket",
         findings="Approximately 14 mm fouling deposit on shell side. Heat duty "
                  "recovered from 68% to 94% of design after cleaning.",
         status="completed", next_due="2026-11-11", work_order="WO-2026-0455"),
    dict(equipment_tag="F-101", performed_on="2026-03-30", maintenance_type="preventive",
         description="Burner inspection and tip cleaning, all 12 burners",
         technician="M. Rathore", duration_hours=16.0, parts_used="Burner tips x4",
         findings="Four tips replaced for erosion. Flame pattern uniform after work. "
                  "Coil TMT peak 582 degC against 620 degC design limit.",
         status="completed", next_due="2026-09-30", work_order="WO-2026-0287"),
    dict(equipment_tag="B-701", performed_on="2026-06-08", maintenance_type="preventive",
         description="Annual safety valve testing and boiler internal inspection",
         technician="Contract - Thermax Service", duration_hours=54.0,
         parts_used="Safety valve seat kit; manhole gaskets",
         findings="Waterside scale minimal. Safety valves reset to 42 barg, "
                  "popped within 1.5% of set. Statutory certificate renewed.",
         status="completed", next_due="2027-06-08", work_order="WO-2026-0521"),
    dict(equipment_tag="P-410", performed_on="2026-07-02", maintenance_type="preventive",
         description="Bearing lubrication and coupling inspection",
         technician="A. Fernandes", duration_hours=4.0, parts_used="Grease NLGI 2",
         findings="Coupling element within wear limit. Vibration 2.1 mm/s, normal.",
         status="completed", next_due="2027-01-02", work_order="WO-2026-0640"),
    dict(equipment_tag="C-301", performed_on="2026-04-19", maintenance_type="preventive",
         description="Valve plate inspection, both stages",
         technician="R. Deshmukh", duration_hours=22.0,
         parts_used="Valve plate set VP-2K90 x2; piston rings",
         findings="First-stage valve plates showing normal wear at 18,000 run hours. "
                  "Replaced as planned. Second stage acceptable, left in service.",
         status="completed", next_due="2027-04-19", work_order="WO-2026-0334"),
    dict(equipment_tag="TK-802", performed_on="2026-08-01", maintenance_type="corrective",
         description="Tank taken out of service for statutory internal inspection",
         technician="Inspection department", duration_hours=0.0, parts_used="",
         findings="Tank drained, cleaned and gas-freed. Internal inspection in progress.",
         status="in_progress", next_due="2026-10-15", work_order="WO-2026-0801"),
    dict(equipment_tag="P-305", performed_on="2026-05-27", maintenance_type="preventive",
         description="Seal flush system inspection and filter change",
         technician="A. Fernandes", duration_hours=5.0, parts_used="Seal flush filter x2",
         findings="Flush pressure 3.2 barg above stuffing box, correct. No leakage.",
         status="completed", next_due="2026-11-27", work_order="WO-2026-0488"),
    dict(equipment_tag="M-101", performed_on="2026-08-24", maintenance_type="preventive",
         description="Motor insulation resistance test during P-101 outage",
         technician="V. Krishnan (Electrical)", duration_hours=3.0, parts_used="",
         findings="IR 1,850 Mohm at 5 kV, polarisation index 2.4. Winding healthy.",
         status="completed", next_due="2027-08-24", work_order="WO-2026-0877"),
]

INSPECTIONS: List[Dict[str, Any]] = [
    dict(equipment_tag="P-101", inspected_on="2026-06-15", inspection_type="vibration",
         inspector="S. Iyer", result="attention",
         findings=("Overall vibration 6.8 mm/s RMS at the coupling-end bearing, against "
                   "an ISO 10816-3 Zone B/C boundary of 4.5 mm/s for this machine class. "
                   "Envelope spectrum confirms outer-race defect frequency (BPFO 162 Hz) "
                   "with sidebands at shaft speed. Bearing temperature 78 degC against "
                   "a 70 degC alarm. Deterioration since the April survey is consistent "
                   "with a propagating spall."),
         recommendation=("Plan bearing replacement within 8 weeks. Transfer duty to P-102 "
                         "and monitor weekly until the work order is executed. Do not "
                         "defer beyond 2026-08-31."),
         document_path="Refinery_Inspection_Report_P101.pdf", next_due="2026-09-15"),
    dict(equipment_tag="P-101", inspected_on="2026-03-05", inspection_type="vibration",
         inspector="S. Iyer", result="pass",
         findings="Overall 3.1 mm/s RMS. Slight rise from January baseline of 2.8 mm/s. "
                  "No discrete defect frequency yet resolvable.",
         recommendation="Continue quarterly monitoring.", next_due="2026-06-05"),
    dict(equipment_tag="C-201", inspected_on="2026-07-22", inspection_type="performance",
         inspector="N. Bhattacharya", result="pass",
         findings="Polytropic efficiency 76.4% against 78% design. Surge margin 22% at "
                  "current operating point. No abnormal axial displacement.",
         recommendation="No action. Next performance test at turnaround.",
         document_path="Compressor_C201_Inspection_Report.pdf", next_due="2027-07-22"),
    dict(equipment_tag="V-510", inspected_on="2026-02-28", inspection_type="thickness",
         inspector="P. Nair", result="attention",
         findings=("Ultrasonic thickness survey at 24 grid points. Minimum reading "
                   "11.4 mm at the bottom knuckle against 12.0 mm nominal and 9.5 mm "
                   "retirement thickness. Corrosion rate 0.12 mm/year over three "
                   "surveys, consistent with wet H2S service."),
         recommendation=("Remaining life approximately 15 years at current rate. Reduce "
                         "survey interval to 24 months. Confirm water wash rate is "
                         "meeting the 3% design target."),
         next_due="2028-02-28"),
    dict(equipment_tag="T-101", inspected_on="2025-11-10", inspection_type="internal",
         inspector="P. Nair", result="pass",
         findings="Trays 1-48 inspected during turnaround. Tray 22 valve caps: 6 missing, "
                  "replaced. No shell corrosion beyond allowance. Overhead corrosion "
                  "monitoring coupons show 0.08 mm/year.",
         recommendation="Next internal at the 2029 turnaround.", next_due="2029-11-10"),
    dict(equipment_tag="TK-802", inspected_on="2026-08-20", inspection_type="internal",
         inspector="P. Nair", result="in_progress",
         findings="Floor plate scanning in progress. 40% complete. Two areas of "
                  "underside pitting identified for further evaluation.",
         recommendation="Complete scan before issuing fitness-for-service assessment.",
         document_path="Tank_Inspection_Report.pdf", next_due="2026-10-15"),
    dict(equipment_tag="PSV-4011", inspected_on="2026-01-15", inspection_type="function",
         inspector="D. Menon", result="pass",
         findings="Pop tested on bench at 11.42 barg against 11.5 barg set, within the "
                  "3% tolerance. Seat tightness acceptable.",
         recommendation="Return to service. Next test 2028-01-15.", next_due="2028-01-15"),
    dict(equipment_tag="PSV-5101", inspected_on="2026-01-16", inspection_type="function",
         inspector="D. Menon", result="pass",
         findings="Pop tested at 83.1 barg against 84 barg set. Within tolerance.",
         recommendation="Return to service.", next_due="2028-01-16"),
    dict(equipment_tag="E-301", inspected_on="2026-04-08", inspection_type="thickness",
         inspector="P. Nair", result="pass",
         findings="Shell minimum 9.8 mm against 8.0 mm retirement. Tube bundle eddy "
                  "current: 3 tubes plugged of 640, cumulative 1.2% plugged.",
         recommendation="Acceptable. Next survey 2029.", next_due="2029-04-08"),
    dict(equipment_tag="F-101", inspected_on="2026-03-30", inspection_type="visual",
         inspector="M. Rathore", result="attention",
         findings="Two convection-section tubes show external scaling. Radiant coil "
                  "shows no bulging or creep damage. Refractory intact.",
         recommendation="Clean convection section at the next opportunity shutdown.",
         next_due="2026-09-30"),
]

ALERTS: List[Dict[str, Any]] = [
    dict(alert_id="ALM-2026-0412", raised_at="2026-08-22T04:18:00", equipment_tag="P-101",
         category="vibration", severity="high",
         description="P-101 coupling-end vibration 7.2 mm/s, above 7.1 mm/s trip alarm",
         status="resolved", acknowledged_by="Shift B Control Room",
         acknowledged_at="2026-08-22T04:22:00", resolved_at="2026-08-24T09:00:00"),
    dict(alert_id="ALM-2026-0418", raised_at="2026-08-24T09:05:00", equipment_tag="P-101",
         category="availability", severity="medium",
         description="P-101 out of service for bearing replacement, duty on P-102",
         status="open", acknowledged_by="Shift A Control Room",
         acknowledged_at="2026-08-24T09:10:00"),
    dict(alert_id="ALM-2026-0431", raised_at="2026-08-28T16:44:00", equipment_tag="E-405",
         category="process", severity="medium",
         description="E-405 outlet temperature 8 degC above target, fouling suspected",
         status="open", acknowledged_by="FCC Panel"),
    dict(alert_id="ALM-2026-0433", raised_at="2026-08-29T02:11:00", equipment_tag="C-202",
         category="process", severity="low",
         description="Instrument air dewpoint -38 degC against -40 degC target",
         status="acknowledged", acknowledged_by="Utilities Panel",
         acknowledged_at="2026-08-29T02:30:00"),
    dict(alert_id="ALM-2026-0440", raised_at="2026-08-31T11:02:00", equipment_tag="TK-810",
         category="process", severity="low",
         description="TK-810 level 88%, approaching 90% high alarm",
         status="open", acknowledged_by="Tank Farm"),
    dict(alert_id="ALM-2026-0402", raised_at="2026-08-14T22:37:00", equipment_tag="B-702",
         category="equipment", severity="medium",
         description="B-702 forced draft fan bearing temperature 82 degC, alarm at 80 degC",
         status="resolved", acknowledged_by="Utilities Panel",
         acknowledged_at="2026-08-14T22:41:00", resolved_at="2026-08-15T06:20:00"),
]

INVENTORY: List[Dict[str, Any]] = [
    dict(part_number="BRG-6317-C3", description="Bearing SKF 6317 C3 deep groove ball",
         category="Bearings", quantity=1, minimum_stock=3, unit="ea",
         storage_location="Stores A-14-03", supplier="SKF India",
         equipment_tag="P-101", last_counted="2026-08-25"),
    dict(part_number="MS-200-53B", description="Mechanical seal kit, API Plan 53B, 200 mm",
         category="Seals", quantity=4, minimum_stock=2, unit="ea",
         storage_location="Stores A-11-07", supplier="John Crane",
         equipment_tag="P-101", last_counted="2026-08-25"),
    dict(part_number="WR-8x10", description="Casing wear ring set, 8x10 slurry pump",
         category="Pump Parts", quantity=2, minimum_stock=2, unit="set",
         storage_location="Stores A-12-01", supplier="Flowserve",
         equipment_tag="P-204A", last_counted="2026-07-30"),
    dict(part_number="VP-2K90", description="Valve plate set, Burckhardt 2K90 stage 1",
         category="Compressor Parts", quantity=1, minimum_stock=2, unit="set",
         storage_location="Stores B-04-02", supplier="Burckhardt Compression",
         equipment_tag="C-301", last_counted="2026-06-15"),
    dict(part_number="BT-VC4P-TIP", description="Burner tip, Furnace Fabrica VC-4P",
         category="Heater Parts", quantity=8, minimum_stock=4, unit="ea",
         storage_location="Stores B-07-05", supplier="Furnace Fabrica",
         equipment_tag="F-101", last_counted="2026-04-01"),
    dict(part_number="GSK-AES1200", description="Shell gasket, spiral wound, AES 1200",
         category="Gaskets", quantity=12, minimum_stock=6, unit="ea",
         storage_location="Stores A-02-11", supplier="Lamons",
         equipment_tag="E-301", last_counted="2026-08-10"),
    dict(part_number="LUB-NLGI2", description="Bearing grease NLGI 2 lithium complex",
         category="Lubricants", quantity=46, minimum_stock=20, unit="kg",
         storage_location="Stores C-01-01", supplier="Shell Lubricants",
         last_counted="2026-08-28"),
    dict(part_number="FLT-SF-100", description="Seal flush filter element 10 micron",
         category="Filters", quantity=6, minimum_stock=8, unit="ea",
         storage_location="Stores A-15-02", supplier="Pall India",
         equipment_tag="P-305", last_counted="2026-08-20"),
    dict(part_number="PSV-J-KIT", description="Relief valve overhaul kit, J orifice",
         category="Safety Valves", quantity=3, minimum_stock=2, unit="ea",
         storage_location="Stores B-09-01", supplier="Anderson Greenwood",
         equipment_tag="PSV-4011", last_counted="2026-01-20"),
    dict(part_number="CPL-EL-355", description="Coupling element, 355 kW rating",
         category="Couplings", quantity=2, minimum_stock=1, unit="ea",
         storage_location="Stores A-13-04", supplier="Rexnord",
         equipment_tag="M-204", last_counted="2026-07-15"),
]


# =========================================================== employees ====
# Fictional. Named so the P-101 thread is traceable to real roles: the
# technician on the work orders, the reliability engineer on the vibration
# surveys, and the inspector who signed the report all appear here.

EMPLOYEES: List[Dict[str, Any]] = [
    dict(employee_id="MPL-1001", name="Ramesh Deshmukh", role="Senior Maintenance Technician",
         department="Mechanical Maintenance", email="r.deshmukh@meridian.example",
         phone="+91-98XXXX-1001", location="CDU/VDU Area", started_on="2005-06-13",
         notes="Rotating equipment specialist. Lead technician on P-101 and P-102. "
               "Certified: API 686 machinery installation, mechanical seal fitting."),
    dict(employee_id="MPL-1002", name="Sunita Iyer", role="Reliability Engineer",
         department="Reliability", email="s.iyer@meridian.example",
         phone="+91-98XXXX-1002", location="Technical Building", started_on="2014-02-03",
         notes="Condition monitoring lead. Owns the vibration programme for rotating "
               "equipment. Certified: ISO 18436-2 Vibration Analyst Category III."),
    dict(employee_id="MPL-1003", name="Prakash Nair", role="Senior Inspection Engineer",
         department="Inspection", email="p.nair@meridian.example",
         phone="+91-98XXXX-1003", location="Inspection Office", started_on="2001-09-20",
         notes="Static equipment and tanks. Signs thickness surveys and fitness-for-service "
               "assessments. Certified: API 510, API 570, API 653."),
    dict(employee_id="MPL-1004", name="Anil Fernandes", role="Maintenance Technician",
         department="Mechanical Maintenance", email="a.fernandes@meridian.example",
         phone="+91-98XXXX-1004", location="FCC Area", started_on="2011-11-07",
         notes="Slurry and heavy-service pumps. Certified: confined space entry, rigging."),
    dict(employee_id="MPL-1005", name="Deepa Menon", role="Instrumentation Engineer",
         department="Instrumentation & Control", email="d.menon@meridian.example",
         phone="+91-98XXXX-1005", location="Technical Building", started_on="2013-07-15",
         notes="Safety instrumented systems and relief valve testing. "
               "Certified: TUV Functional Safety Engineer."),
    dict(employee_id="MPL-1006", name="Nikhil Bhattacharya", role="Process Engineer",
         department="Process Engineering", email="n.bhattacharya@meridian.example",
         phone="+91-98XXXX-1006", location="Technical Building", started_on="2016-01-11",
         notes="FCC and gas plant. Performance testing of rotating machinery."),
    dict(employee_id="MPL-1007", name="Vijay Krishnan", role="Senior Electrical Engineer",
         department="Electrical Maintenance", email="v.krishnan@meridian.example",
         phone="+91-98XXXX-1007", location="Substation 2", started_on="2008-04-22",
         notes="HV motors and switchgear. Certified: electrical safety authorised person."),
    dict(employee_id="MPL-1008", name="Meera Rathore", role="Fired Heater Specialist",
         department="Mechanical Maintenance", email="m.rathore@meridian.example",
         phone="+91-98XXXX-1008", location="CDU Area", started_on="2010-08-30",
         notes="Fired heaters and burners across CDU, VDU and HDT."),
    dict(employee_id="MPL-1009", name="Arjun Pillai", role="Maintenance Manager",
         department="Mechanical Maintenance", email="a.pillai@meridian.example",
         phone="+91-98XXXX-1009", location="Maintenance Building", started_on="1998-03-16",
         notes="Head of Mechanical Maintenance. Approves work orders above 20 hours "
               "and all critical-equipment interventions."),
    dict(employee_id="MPL-1010", name="Kavita Rao", role="HSE Manager",
         department="HSE", email="k.rao@meridian.example",
         phone="+91-98XXXX-1010", location="Admin Building", started_on="2006-10-02",
         notes="Process safety and occupational health. Signs high-risk permits."),
    dict(employee_id="MPL-1011", name="Sanjay Gupta", role="Shift Superintendent",
         department="Refinery Operations", email="s.gupta@meridian.example",
         phone="+91-98XXXX-1011", location="Main Control Room", started_on="2003-05-19",
         notes="Shift A. Authority to shut down any unit on safety grounds."),
    dict(employee_id="MPL-1012", name="Fatima Sheikh", role="Shift Superintendent",
         department="Refinery Operations", email="f.sheikh@meridian.example",
         phone="+91-98XXXX-1012", location="Main Control Room", started_on="2007-02-26",
         notes="Shift B."),
    dict(employee_id="MPL-1013", name="George Mathew", role="Panel Operator",
         department="Refinery Operations", email="g.mathew@meridian.example",
         phone="+91-98XXXX-1013", location="CDU Control Room", started_on="2012-09-03",
         notes="CDU-1 and CDU-2 panel."),
    dict(employee_id="MPL-1014", name="Priya Sharma", role="Laboratory Chemist",
         department="Laboratory", email="p.sharma@meridian.example",
         phone="+91-98XXXX-1014", location="Central Laboratory", started_on="2018-06-25",
         notes="Product certification and crude assay."),
    dict(employee_id="MPL-1015", name="Rahul Verma", role="Reliability Manager",
         department="Reliability", email="r.verma@meridian.example",
         phone="+91-98XXXX-1015", location="Technical Building", started_on="2009-01-12",
         notes="Owns criticality assessment and the risk-based maintenance strategy."),
    dict(employee_id="MPL-1016", name="Anita Joshi", role="Stores Officer",
         department="Stores", email="a.joshi@meridian.example",
         phone="+91-98XXXX-1016", location="Central Stores", started_on="2015-04-08",
         notes="Spares issue and minimum-stock monitoring."),
    dict(employee_id="MPL-1017", name="Imran Qureshi", role="Procurement Officer",
         department="Procurement", email="i.qureshi@meridian.example",
         phone="+91-98XXXX-1017", location="Admin Building", started_on="2017-11-20",
         notes="Rotating equipment spares and service contracts."),
    dict(employee_id="MPL-1018", name="Lakshmi Menon", role="Utilities Engineer",
         department="Utilities", email="l.menon@meridian.example",
         phone="+91-98XXXX-1018", location="Utilities Block", started_on="2014-08-18",
         notes="Boilers, cooling water and instrument air."),
    dict(employee_id="MPL-1019", name="Tarun Kapoor", role="Projects Engineer",
         department="Projects", email="t.kapoor@meridian.example",
         phone="+91-98XXXX-1019", location="Projects Office", started_on="2019-03-04",
         notes="Turnaround planning and capital projects."),
    dict(employee_id="MPL-1020", name="Neha Bansal", role="Emergency Response Coordinator",
         department="Emergency Response", email="n.bansal@meridian.example",
         phone="+91-98XXXX-1020", location="Fire Station", started_on="2016-07-11",
         notes="Fire and rescue, emergency drills, mutual aid coordination."),
    dict(employee_id="MPL-1021", name="Sameer Dutta", role="OT Systems Engineer",
         department="IT / OT", email="s.dutta@meridian.example",
         phone="+91-98XXXX-1021", location="Technical Building", started_on="2020-02-17",
         notes="Historian, DCS interfaces and the plant data network."),
    dict(employee_id="MPL-1022", name="Rohini Desai", role="Finance Analyst",
         department="Finance", email="r.desai@meridian.example",
         phone="+91-98XXXX-1022", location="Admin Building", started_on="2018-09-10",
         notes="Maintenance cost tracking and capital budgeting."),
]


# ============================================================= readings ====

def _reading_series() -> List[Dict[str, Any]]:
    """Instrument readings, generated so the trends match the written record.

    P-101's vibration is the one that matters: it has to rise from a healthy
    2.8 mm/s in January through 4.9 in April and 6.8 in June to the 7.2 that
    tripped the alarm in August, because three inspection reports and a work
    order all describe exactly that curve. A flat or random trace would make
    every one of those documents read as fiction.
    """
    rows: List[Dict[str, Any]] = []
    start = TODAY - timedelta(days=240)

    # P-101 vibration: the documented deterioration, then the repair.
    anchors = [(0, 2.8), (60, 3.1), (110, 4.9), (165, 6.8), (235, 7.2)]
    for day in range(0, 241, 2):
        value = _interpolate(anchors, day)
        if day > 236:                      # after the 24 Aug bearing change
            value = 2.4
        rows.append(_reading("P-101", start + timedelta(days=day),
                             "vibration_velocity", round(value + _RANDOM.uniform(-0.08, 0.08), 2),
                             "mm/s"))

    # Bearing temperature follows the same story, one step behind.
    for day in range(0, 241, 4):
        base = 62 + (_interpolate(anchors, day) - 2.8) * 5.2
        if day > 236:
            base = 58
        rows.append(_reading("P-101", start + timedelta(days=day),
                             "bearing_temperature", round(base + _RANDOM.uniform(-1.1, 1.1), 1),
                             "degC"))

    # Steady services, for contrast and for aggregate questions.
    steady = [
        ("P-204A", "vibration_velocity", 3.4, 0.35, "mm/s"),
        ("P-410", "vibration_velocity", 2.1, 0.2, "mm/s"),
        ("C-201", "discharge_pressure", 8.6, 0.18, "barg"),
        ("C-201", "suction_temperature", 41.0, 1.4, "degC"),
        ("C-301", "discharge_pressure", 78.0, 0.9, "barg"),
        ("T-101", "overhead_temperature", 128.0, 2.6, "degC"),
        ("T-101", "bottoms_temperature", 352.0, 3.8, "degC"),
        ("F-101", "coil_outlet_temperature", 358.0, 3.1, "degC"),
        ("B-701", "steam_flow", 112.0, 5.5, "t/h"),
        ("V-401", "pressure", 9.8, 0.22, "barg"),
        ("TK-810", "level", 74.0, 7.0, "%"),
    ]
    for tag, parameter, mean, spread, unit in steady:
        for day in range(0, 241, 6):
            rows.append(_reading(tag, start + timedelta(days=day), parameter,
                                 round(_RANDOM.gauss(mean, spread), 2), unit))

    # E-405 fouling: recovers at the May clean, then climbs again - which is
    # what the August alert about outlet temperature is reporting.
    for day in range(0, 241, 5):
        if day < 130:
            value = 96 - day * 0.22
        elif day < 136:
            value = 94.0
        else:
            value = 94 - (day - 136) * 0.19
        rows.append(_reading("E-405", start + timedelta(days=day), "duty_percent",
                             round(value + _RANDOM.uniform(-0.9, 0.9), 1), "%"))
    return rows


def _interpolate(anchors: List[tuple], day: int) -> float:
    """Straight lines between the documented points."""
    if day <= anchors[0][0]:
        return anchors[0][1]
    for (day_a, value_a), (day_b, value_b) in zip(anchors, anchors[1:]):
        if day_a <= day <= day_b:
            span = day_b - day_a
            return value_a + (value_b - value_a) * ((day - day_a) / span if span else 0)
    return anchors[-1][1]


def _reading(tag: str, when: date, parameter: str, value: float,
             unit: str) -> Dict[str, Any]:
    return {"equipment_tag": tag,
            "measured_at": datetime(when.year, when.month, when.day, 8, 0).isoformat(" "),
            "parameter": parameter, "value": value, "unit": unit,
            "quality": "good", "source": DEMO}


READINGS = _reading_series()


# ================================================== operations and KPIs ====
# Structured series the deck and workbook builders draw on for charts.

PRODUCTION_KPI = [
    # month, crude processed (kt), capacity utilisation %, energy intensity
    # (MBN), unplanned downtime (h), on-spec production %
    ("2025-10", 742, 92.8, 108.4, 14, 99.2),
    ("2025-11", 718, 91.1, 109.1, 22, 98.8),
    ("2025-12", 751, 93.9, 107.9, 9, 99.4),
    ("2026-01", 760, 95.1, 106.8, 6, 99.6),
    ("2026-02", 688, 91.4, 110.2, 31, 98.1),
    ("2026-03", 745, 93.2, 108.0, 12, 99.3),
    ("2026-04", 733, 91.7, 108.9, 18, 99.0),
    ("2026-05", 702, 87.8, 112.4, 44, 97.9),
    ("2026-06", 748, 93.6, 107.6, 11, 99.4),
    ("2026-07", 756, 94.6, 107.1, 8, 99.5),
    ("2026-08", 729, 91.2, 109.3, 26, 98.9),
]

SAFETY_KPI = [
    # month, recordable incidents, near misses, LTI, permits issued,
    # safety observations, training hours
    ("2025-10", 1, 14, 0, 412, 288, 640),
    ("2025-11", 0, 11, 0, 388, 301, 520),
    ("2025-12", 2, 18, 1, 402, 264, 480),
    ("2026-01", 0, 16, 0, 431, 322, 710),
    ("2026-02", 1, 21, 0, 455, 298, 560),
    ("2026-03", 0, 13, 0, 418, 341, 690),
    ("2026-04", 1, 19, 0, 407, 315, 605),
    ("2026-05", 0, 24, 0, 498, 356, 880),
    ("2026-06", 0, 12, 0, 396, 329, 640),
    ("2026-07", 1, 15, 0, 421, 344, 720),
    ("2026-08", 0, 17, 0, 448, 361, 655),
]

MAINTENANCE_COST = [
    # month, planned (INR lakh), unplanned, contract, spares, total
    ("2025-10", 148.2, 42.1, 96.4, 61.8, 348.5),
    ("2025-11", 152.6, 58.9, 88.2, 70.4, 370.1),
    ("2025-12", 141.0, 31.4, 102.6, 54.2, 329.2),
    ("2026-01", 156.8, 24.6, 91.0, 48.9, 321.3),
    ("2026-02", 149.4, 88.2, 110.4, 96.1, 444.1),
    ("2026-03", 163.2, 36.8, 94.8, 58.4, 353.2),
    ("2026-04", 158.9, 44.2, 89.6, 66.2, 358.9),
    ("2026-05", 171.4, 102.6, 148.2, 88.4, 510.6),
    ("2026-06", 154.8, 28.4, 96.2, 52.1, 331.5),
    ("2026-07", 149.6, 22.8, 92.4, 46.8, 311.6),
    ("2026-08", 168.2, 66.4, 98.8, 79.6, 413.0),
]

INCIDENTS = [
    dict(incident_id="INC-2026-004", occurred_on="2026-05-18", unit="FCC-1",
         category="process_safety", severity="minor",
         title="Slurry leak from E-405 channel cover during restart",
         description=("Gasket weep observed on the E-405 channel cover approximately "
                      "40 minutes after restart following hydrojet cleaning. Unit rate "
                      "reduced and the exchanger isolated. No fire, no injury, "
                      "approximately 15 litres contained in the bund."),
         root_cause=("Channel cover bolting was tightened in the correct sequence but "
                     "to the previous gasket specification; the replacement gasket "
                     "required a higher seating stress."),
         actions=("Torque specification updated in the job pack. Toolbox talk delivered "
                  "to all mechanical crews on gasket seating stress."),
         lti=0, cost_lakh=8.4),
    dict(incident_id="INC-2026-002", occurred_on="2026-02-27", unit="UTL-1",
         category="occupational", severity="minor",
         title="Hand injury during boiler valve maintenance",
         description=("Technician sustained a laceration to the left hand while removing "
                      "a seized valve bonnet nut. Treated on site, returned to duty the "
                      "same shift."),
         root_cause="Incorrect tool selection; a slogging spanner was used without a "
                    "striking guard.",
         actions="Struck-tool procedure reissued. Guarded spanners provided to all crews.",
         lti=0, cost_lakh=1.2),
    dict(incident_id="INC-2025-011", occurred_on="2025-12-09", unit="TKF-1",
         category="occupational", severity="serious",
         title="Fall from height during tank gauging platform access",
         description=("Operator fell approximately 1.8 m from a gauging platform access "
                      "ladder. Fractured wrist. 21 days lost time."),
         root_cause=("Ladder cage was corroded and a rung had deformed. The defect had "
                     "been reported eleven weeks earlier and the work order had not "
                     "been prioritised."),
         actions=("All tank access structures inspected within 30 days. Work-order "
                  "prioritisation rules revised so structural defects on access routes "
                  "are raised to high priority automatically."),
         lti=21, cost_lakh=18.6),
    dict(incident_id="INC-2026-001", occurred_on="2026-01-22", unit="CDU-1",
         category="environmental", severity="minor",
         title="Flare smoking during CDU-1 rate change",
         description="Visible smoking from FL-901 for approximately 12 minutes during a "
                     "rapid rate reduction. Steam to the flare tip was increased and the "
                     "smoke cleared.",
         root_cause="Flare steam ratio controller was in manual following earlier work.",
         actions="Controller returned to auto. Shift handover checklist updated to "
                 "include flare steam controller mode.",
         lti=0, cost_lakh=0.0),
]

PROJECTS = [
    dict(project_id="PRJ-2026-01", name="CDU-1 Preheat Train Revamp",
         status="in_progress", start="2026-03-01", finish="2027-02-28",
         budget_lakh=4_820.0, spent_lakh=1_640.0, owner="Tarun Kapoor",
         objective="Recover 6 degC of preheat and reduce F-101 fired duty by 4%."),
    dict(project_id="PRJ-2025-04", name="Flare Gas Recovery Unit",
         status="in_progress", start="2025-09-15", finish="2026-12-20",
         budget_lakh=7_150.0, spent_lakh=5_390.0, owner="Tarun Kapoor",
         objective="Recover routine flare gas to fuel system; cut flaring by 70%."),
    dict(project_id="PRJ-2026-02", name="Condition Monitoring Expansion Phase 2",
         status="in_progress", start="2026-06-01", finish="2026-11-30",
         budget_lakh=680.0, spent_lakh=214.0, owner="Rahul Verma",
         objective="Online vibration monitoring on 40 additional critical machines."),
    dict(project_id="PRJ-2024-07", name="SRU Tail Gas Treatment Upgrade",
         status="completed", start="2024-04-01", finish="2025-08-30",
         budget_lakh=3_960.0, spent_lakh=4_102.0, owner="Tarun Kapoor",
         objective="Raise sulphur recovery from 98.6% to 99.8%."),
    dict(project_id="PRJ-2026-03", name="Tank Farm Fire Protection Refurbishment",
         status="planned", start="2026-11-01", finish="2027-08-31",
         budget_lakh=2_240.0, spent_lakh=0.0, owner="Neha Bansal",
         objective="Replace foam lines and deluge on crude and product tankage."),
]

TIMELINE = [
    ("1987", "Refinery commissioned with CDU-1, VDU-1 and utilities at 3.0 MMTPA"),
    ("1992", "Effluent treatment plant and second utility boiler commissioned"),
    ("1996", "FCC-1 and catalytic reformer added; capacity reaches 6.0 MMTPA"),
    ("2004", "CDU-2 commissioned, taking capacity to 8.4 MMTPA"),
    ("2009", "Hydrotreating complex, hydrogen and sulphur recovery added for BS-IV"),
    ("2013", "First integrated turnaround across all major units"),
    ("2016", "BS-VI fuel quality upgrade completed"),
    ("2019", "Condition monitoring programme established for critical rotating equipment"),
    ("2022", "Capacity debottlenecked to 9.6 MMTPA"),
    ("2024", "SRU tail gas treatment upgrade; sulphur recovery to 99.8%"),
    ("2025", "Flare gas recovery project sanctioned"),
    ("2026", "CDU-1 preheat revamp under execution; next turnaround planned 2027"),
]
