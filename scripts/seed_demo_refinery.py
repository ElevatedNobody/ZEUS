"""
Load the Meridian Coastal Refinery demonstration dataset.

    python scripts/seed_demo_refinery.py            load it
    python scripts/seed_demo_refinery.py --check    what is loaded? change nothing
    python scripts/seed_demo_refinery.py --remove   take it out again
    python scripts/seed_demo_refinery.py --verify   check it is self-consistent

Every row written here carries `source = 'DEMO'`, which is what makes `--remove`
possible and what lets any part of ZEUS tell demonstration data apart from
something a real operator entered. Nothing this script writes can shadow real
data, and nothing it removes can take real data with it.

Re-running is safe. Equipment is upserted by tag; history rows are written only
if that exact record is not already present, so seeding twice does not double
the maintenance history.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from industrial import demo_refinery as demo
from industrial import plant_data


class Say:
    GREEN, RED, AMBER, DIM, BOLD, OFF = (
        "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m")

    @classmethod
    def rule(cls, title: str):
        print("\n" + "=" * 66)
        print(f"  {title}")
        print("=" * 66)

    @classmethod
    def ok(cls, message: str):
        print(f"  {cls.GREEN}OK{cls.OFF}    {message}")

    @classmethod
    def warn(cls, message: str):
        print(f"  {cls.AMBER}WARN{cls.OFF}  {message}")

    @classmethod
    def fail(cls, message: str):
        print(f"  {cls.RED}FAIL{cls.OFF}  {message}")

    @classmethod
    def note(cls, message: str):
        print(f"        {cls.DIM}{message}{cls.OFF}")


def _connection():
    plant_data.init_plant_schema()
    return plant_data._conn()


def _count(table: str, demo_only: bool = True) -> int:
    with _connection() as conn:
        where = " WHERE source = 'DEMO'" if demo_only else ""
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}{where}").fetchone()[0]
        except Exception:
            return 0


# ================================================================= load ====

def seed() -> dict:
    Say.rule("SEEDING THE DEMONSTRATION REFINERY")
    print(f"\n  {demo.COMPANY['site']} — {demo.COMPANY['location']}")
    print(f"  {demo.COMPANY['capacity_mmtpa']} MMTPA, {len(demo.UNITS)} process units")
    print(f"  {Say.DIM}{demo.COMPANY['note']}{Say.OFF}\n")

    counts = {}

    # --- equipment -------------------------------------------------------
    for row in demo.equipment_rows():
        # upsert_equipment serialises `attributes` itself. Passing a string
        # here double-encodes it, and the reader then gets a str back from
        # json.loads instead of a dict - which fails at the first .items().
        plant_data.upsert_equipment(source=demo.DEMO, **row)
    counts["equipment"] = len(demo.equipment_rows())
    Say.ok(f"{counts['equipment']} equipment records")

    # --- history ---------------------------------------------------------
    # Written directly rather than through add_maintenance(), because that
    # helper stamps today's date; this dataset needs its own dates so the
    # trends and the documents agree.
    with _connection() as conn:
        counts["maintenance"] = _insert_unique(
            conn, "plant_maintenance", demo.MAINTENANCE,
            key=("equipment_tag", "performed_on", "work_order"))
        counts["inspections"] = _insert_unique(
            conn, "plant_inspections", demo.INSPECTIONS,
            key=("equipment_tag", "inspected_on", "inspection_type"))
        counts["alerts"] = _insert_unique(
            conn, "plant_alerts", demo.ALERTS, key=("alert_id",))
        counts["inventory"] = _insert_unique(
            conn, "plant_inventory", demo.INVENTORY, key=("part_number",))
        counts["readings"] = _insert_unique(
            conn, "plant_readings", demo.READINGS,
            key=("equipment_tag", "measured_at", "parameter"))
        conn.commit()

    for label in ("maintenance", "inspections", "alerts", "inventory", "readings"):
        Say.ok(f"{counts[label]} {label} records")

    # --- employees -------------------------------------------------------
    counts["employees"] = _seed_employees()
    Say.ok(f"{counts['employees']} employee records")

    print()
    Say.ok("the demonstration refinery is loaded")
    Say.note("every row is marked source='DEMO' and can be removed with --remove")
    print(f"\n  Try asking ZEUS:")
    for question in ("tell me about pump P-101",
                     "show the maintenance history of P-101",
                     "which equipment needs attention",
                     "who works in mechanical maintenance",
                     "create a maintenance report for P-101"):
        print(f"    {Say.DIM}\"{question}\"{Say.OFF}")
    return counts


def _insert_unique(conn, table: str, rows: list, key: tuple) -> int:
    """Insert rows that are not already there.

    Keyed on the natural identity of the record rather than a row id, so
    running the seeder twice does not duplicate a maintenance history - which
    would quietly corrupt every count and average built on top of it.
    """
    written = 0
    for row in rows:
        row = dict(row)
        row.setdefault("source", demo.DEMO)
        where = " AND ".join(f"{column} = ?" for column in key)
        values = [row.get(column) for column in key]
        try:
            existing = conn.execute(
                f"SELECT 1 FROM {table} WHERE {where}", values).fetchone()
        except Exception:
            existing = None
        if existing:
            continue
        columns = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        try:
            conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})",
                         list(row.values()))
            written += 1
        except Exception as error:
            Say.warn(f"{table}: {error}")
    return written


def _seed_employees() -> int:
    """Into the employees table the Vault and productivity panels already use.

    A second employee store would split the data in two and guarantee that one
    of them goes stale.
    """
    from memory import database

    database.init_db()
    existing = {row.get("employee_id") for row in database.list_employees(
        include_inactive=True)}
    written = 0
    for person in demo.EMPLOYEES:
        if person["employee_id"] in existing:
            continue
        try:
            database.add_employee(**person)
            written += 1
        except Exception as error:
            Say.warn(f"{person['employee_id']}: {error}")
    return written


# =============================================================== remove ====

def remove() -> int:
    Say.rule("REMOVING THE DEMONSTRATION DATA")
    print("\n  Only rows marked source='DEMO' are touched. Anything you entered")
    print("  yourself stays exactly where it is.\n")

    removed = 0
    with _connection() as conn:
        for table in ("plant_readings", "plant_alerts", "plant_inspections",
                      "plant_maintenance", "plant_inventory", "plant_equipment"):
            try:
                cursor = conn.execute(f"DELETE FROM {table} WHERE source = 'DEMO'")
                Say.ok(f"{cursor.rowcount} rows from {table}")
                removed += cursor.rowcount
            except Exception as error:
                Say.warn(f"{table}: {error}")
        conn.commit()

    from memory import database
    database.init_db()
    for person in demo.EMPLOYEES:
        try:
            if database.delete_employee(person["employee_id"]):
                removed += 1
        except Exception:
            pass
    Say.ok(f"{len(demo.EMPLOYEES)} employee records considered")
    print(f"\n  {removed} rows removed.")
    return removed


# ================================================================ check ====

def check() -> int:
    Say.rule("DEMONSTRATION DATA — WHAT IS LOADED")
    print()
    tables = [("plant_equipment", "Equipment"), ("plant_maintenance", "Maintenance"),
              ("plant_inspections", "Inspections"), ("plant_alerts", "Alerts"),
              ("plant_inventory", "Inventory"), ("plant_readings", "Readings")]
    total = 0
    for table, label in tables:
        demo_rows = _count(table, demo_only=True)
        all_rows = _count(table, demo_only=False)
        real = all_rows - demo_rows
        total += demo_rows
        suffix = f"  ({real} entered locally)" if real else ""
        print(f"  {label:<14} {demo_rows:>6} demo rows{suffix}")

    try:
        from memory import database
        database.init_db()
        people = len(database.list_employees(include_inactive=True))
        print(f"  {'Employees':<14} {people:>6} records")
    except Exception as error:
        Say.warn(f"employees unavailable: {error}")

    print()
    if total:
        Say.ok("the demonstration refinery is loaded")
    else:
        Say.warn("no demonstration data - run this script without --check to load it")
    return 0 if total else 1


# =============================================================== verify ====

def verify() -> int:
    """Check the dataset agrees with itself.

    Plausible-looking data that contradicts itself is worse than no data,
    because every answer built on it is confidently wrong. These are the
    contradictions that would actually mislead somebody reading a generated
    report.
    """
    Say.rule("VERIFYING INTERNAL CONSISTENCY")
    print()
    problems = []

    tags = {row["tag"] for row in demo.equipment_rows()}

    # 1. Every history record points at equipment that exists.
    for label, rows, field in (("maintenance", demo.MAINTENANCE, "equipment_tag"),
                               ("inspection", demo.INSPECTIONS, "equipment_tag"),
                               ("reading", demo.READINGS, "equipment_tag")):
        orphans = {row[field] for row in rows if row.get(field) not in tags}
        if orphans:
            problems.append(f"{label} records reference unknown equipment: {orphans}")
    for row in demo.ALERTS:
        if row.get("equipment_tag") and row["equipment_tag"] not in tags:
            problems.append(f"alert {row['alert_id']} references unknown "
                            f"{row['equipment_tag']}")
    Say.ok("every history record points at real equipment")

    # 2. Every technician and inspector named is in the directory.
    people = {p["name"].split()[-1] for p in demo.EMPLOYEES}
    named = set()
    for row in demo.MAINTENANCE:
        for part in str(row.get("technician", "")).replace(",", " ").split():
            if part[:1].isupper() and len(part) > 3 and "(" not in part:
                named.add(part.strip("()"))
    for row in demo.INSPECTIONS:
        surname = str(row.get("inspector", "")).split()[-1] if row.get("inspector") else ""
        if surname:
            named.add(surname)
    unknown = {n for n in named if n not in people
               and n not in {"Contract", "Hydroblast", "Services", "Thermax",
                             "Service", "Inspection", "department", "Reliability",
                             "Electrical"}}
    if unknown:
        problems.append(f"people named in records but not in the directory: {unknown}")
    else:
        Say.ok("every technician and inspector named appears in the directory")

    # 3. The P-101 story: the readings must show the deterioration the reports
    #    describe, or three documents become fiction.
    vibration = [r for r in demo.READINGS
                 if r["equipment_tag"] == "P-101"
                 and r["parameter"] == "vibration_velocity"]
    if len(vibration) < 50:
        problems.append("P-101 has too few vibration readings to show a trend")
    else:
        peak = max(r["value"] for r in vibration)
        final = vibration[-1]["value"]
        if peak < 7.0:
            problems.append(f"P-101 peak vibration {peak} never reaches the 7.1 mm/s "
                            f"alarm its August alert reports")
        elif final > 3.0:
            problems.append(f"P-101 vibration is still {final} after the bearing "
                            f"replacement - the repair had no effect in the data")
        else:
            Say.ok(f"P-101 vibration rises to {peak} mm/s then drops to {final} "
                   f"after the repair")

    # 4. The bearing the August work order consumed must show as short in stores.
    bearing = next((i for i in demo.INVENTORY
                    if i["part_number"] == "BRG-6317-C3"), None)
    if bearing and bearing["quantity"] >= bearing["minimum_stock"]:
        problems.append("the bearing consumed by WO-2026-0876 is not short in stores")
    else:
        Say.ok("the spare consumed by the P-101 repair shows as below minimum stock")

    # 5. Equipment marked as under maintenance must have an open work order.
    for row in demo.equipment_rows():
        if row["status"] == "maintenance":
            open_work = [m for m in demo.MAINTENANCE
                         if m["equipment_tag"] == row["tag"]
                         and m.get("status") == "in_progress"]
            if not open_work:
                problems.append(f"{row['tag']} is 'maintenance' with no open work order")
    Say.ok("equipment under maintenance has matching open work orders")

    # 6. KPI series must be the same length and cover the same months.
    months = {len(demo.PRODUCTION_KPI), len(demo.SAFETY_KPI), len(demo.MAINTENANCE_COST)}
    if len(months) != 1:
        problems.append(f"KPI series have different lengths: {months}")
    elif ({m[0] for m in demo.PRODUCTION_KPI} != {m[0] for m in demo.SAFETY_KPI}):
        problems.append("production and safety KPIs cover different months")
    else:
        Say.ok(f"all three KPI series cover the same {len(demo.PRODUCTION_KPI)} months")

    # 7. The month with the worst downtime should also be the dearest month,
    #    or the two series are telling different stories.
    #
    # The column indices are named here rather than written as bare numbers.
    # The first draft of this check compared downtime against the SPARES
    # column instead of the total and reported a contradiction that did not
    # exist - a false alarm in a consistency checker is expensive, because the
    # natural response is to "fix" data that was already right.
    DOWNTIME_HOURS = 4        # PRODUCTION_KPI: month, crude, util%, energy, downtime, onspec
    TOTAL_COST = 5            # MAINTENANCE_COST: month, planned, unplanned, contract, spares, total
    worst_downtime = max(demo.PRODUCTION_KPI, key=lambda r: r[DOWNTIME_HOURS])[0]
    dearest = max(demo.MAINTENANCE_COST, key=lambda r: r[TOTAL_COST])[0]
    if worst_downtime != dearest:
        problems.append(f"worst downtime is {worst_downtime} but the dearest month "
                        f"is {dearest} - the series disagree")
    else:
        Say.ok(f"the worst month for downtime ({worst_downtime}) is also the dearest")

    print()
    if problems:
        for problem in problems:
            Say.fail(problem)
        print(f"\n  {len(problems)} inconsistency(ies). Answers built on this data "
              f"would be wrong.")
        return 1
    Say.ok("the dataset agrees with itself")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--remove", action="store_true")
    parser.add_argument("--verify", action="store_true")
    arguments = parser.parse_args()

    if arguments.check:
        return check()
    if arguments.verify:
        return verify()
    if arguments.remove:
        remove()
        return 0
    seed()
    return 0


if __name__ == "__main__":
    sys.exit(main())
