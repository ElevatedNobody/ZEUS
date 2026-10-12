"""
Deterministic critical-path scheduling - the numbers ZEUS reasons with.

No model computes a date here. A forward pass with actuals, blockers and a data
date, a backward pass, total and free float, the critical and near-critical
sets, and - for every activity - the DRIVER: the one constraint that set its
early start (a predecessor relationship, its actual start, the data date, an
open blocker, a start-no-earlier-than constraint, or the project start). The
"why did the finish date move?" explanation walks those drivers.

Conventions (as P6 and MS Project display them):
  * dates are working days on the project calendar; a task of duration d that
    starts on S finishes at the END of S + (d-1) working days (inclusive);
  * a finish milestone occurs at the end of its day, a start milestone at the
    start of its day;
  * FS: the successor starts the next working day after the predecessor
    finishes (+ lag); SS: starts no earlier than the predecessor's start
    (+ lag); FF / SF constrain the successor's finish;
  * the data date is the first day remaining work can be scheduled on: nothing
    unfinished is forecast in the past;
  * an open blocker with an expected clearance holds the remaining work until
    the working day after that clearance.
"""

from __future__ import annotations

import datetime as _dt
import math
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

DAY = _dt.timedelta(days=1)
MILESTONES = ("start_milestone", "finish_milestone")


class ScheduleError(ValueError):
    pass


class Calendar:
    def __init__(self, workdays: str = "0123456", holidays=()):
        days = {int(c) for c in str(workdays or "") if c.isdigit() and int(c) < 7}
        self.workdays = days or set(range(7))
        self.holidays = set()
        for h in holidays or ():
            try:
                self.holidays.add(h if isinstance(h, _dt.date) else _dt.date.fromisoformat(str(h)[:10]))
            except ValueError:
                continue

    def is_work(self, day: _dt.date) -> bool:
        return day.weekday() in self.workdays and day not in self.holidays

    def next_work(self, day: _dt.date) -> _dt.date:
        for _ in range(3660):
            if self.is_work(day):
                return day
            day += DAY
        raise ScheduleError("the calendar has no working day within ten years")

    def prev_work(self, day: _dt.date) -> _dt.date:
        for _ in range(3660):
            if self.is_work(day):
                return day
            day -= DAY
        raise ScheduleError("the calendar has no working day within ten years")

    def add(self, day: _dt.date, n: int) -> _dt.date:
        """n working days after (n > 0) or before (n < 0) a working day."""
        current = self.next_work(day) if n >= 0 else self.prev_work(day)
        step = DAY if n >= 0 else -DAY
        remaining = abs(int(n))
        while remaining:
            current += step
            if self.is_work(current):
                remaining -= 1
        return current

    def diff(self, a: _dt.date, b: _dt.date) -> int:
        """Working-day steps from a to b (negative when b is before a)."""
        if a == b:
            return 0
        sign = 1 if b > a else -1
        lo, hi = (a, b) if b > a else (b, a)
        count, day = 0, lo
        while day < hi:
            day += DAY
            if self.is_work(day):
                count += 1
        return sign * count

    def span(self, start: _dt.date, finish: _dt.date) -> int:
        """Working days from start to finish inclusive (a duration)."""
        count, day = 0, start
        while day <= finish:
            if self.is_work(day):
                count += 1
            day += DAY
        return count

    def as_dict(self) -> Dict:
        return {"workdays": "".join(str(d) for d in sorted(self.workdays)),
                "holidays": sorted(h.isoformat() for h in self.holidays)}


@dataclass
class ActivityIn:
    id: str
    code: str
    name: str
    duration: int = 1
    type: str = "task"
    planned_start: Optional[_dt.date] = None
    planned_finish: Optional[_dt.date] = None
    baseline_start: Optional[_dt.date] = None
    baseline_finish: Optional[_dt.date] = None
    actual_start: Optional[_dt.date] = None
    actual_finish: Optional[_dt.date] = None
    percent_complete: Optional[float] = None
    remaining: Optional[int] = None               # explicit remaining working days
    constraint_start: Optional[_dt.date] = None   # start no earlier than
    blocked_until: Optional[_dt.date] = None      # open blocker: expected clearance date
    blocker_id: Optional[str] = None
    blocker_open_no_date: bool = False


@dataclass
class Dependency:
    pred: str
    succ: str
    type: str = "FS"
    lag: int = 0


@dataclass
class ActivityOut:
    id: str
    code: str
    name: str
    type: str
    duration: int
    status: str                                   # not_started | in_progress | complete
    es: str
    ef: str
    ls: Optional[str] = None
    lf: Optional[str] = None
    total_float: Optional[int] = None
    free_float: Optional[int] = None
    critical: bool = False
    near_critical: bool = False
    remaining: Optional[int] = None
    percent_complete: Optional[float] = None
    driver: Dict = field(default_factory=dict)
    planned_start: Optional[str] = None
    planned_finish: Optional[str] = None
    baseline_start: Optional[str] = None
    baseline_finish: Optional[str] = None
    finish_variance_days: Optional[int] = None    # forecast finish - baseline finish (calendar days)
    blocked_until: Optional[str] = None
    blocker_id: Optional[str] = None
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict:
        return asdict(self)


@dataclass
class ScheduleResult:
    data_date: str
    project_finish: str
    finish_activity: str
    activities: Dict[str, ActivityOut]
    critical_path: List[str]
    calendar: Dict

    def as_dict(self) -> Dict:
        return {"data_date": self.data_date, "project_finish": self.project_finish,
                "finish_activity": self.finish_activity, "critical_path": self.critical_path,
                "calendar": self.calendar,
                "activities": {k: v.as_dict() for k, v in self.activities.items()}}


def topological(ids: List[str], deps: List[Dependency]) -> List[str]:
    incoming = {i: 0 for i in ids}
    outgoing: Dict[str, List[str]] = {i: [] for i in ids}
    for d in deps:
        if d.pred in incoming and d.succ in incoming:
            incoming[d.succ] += 1
            outgoing[d.pred].append(d.succ)
    ready = sorted(i for i, n in incoming.items() if n == 0)
    order: List[str] = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for nxt in sorted(outgoing[node]):
            incoming[nxt] -= 1
            if incoming[nxt] == 0:
                ready.append(nxt)
    if len(order) != len(ids):
        stuck = sorted(i for i, n in incoming.items() if n > 0)
        raise ScheduleError(f"the logic has a loop through: {', '.join(stuck[:12])}")
    return order


def _iso(day: Optional[_dt.date]) -> Optional[str]:
    return day.isoformat() if day else None


def compute(activities: List[ActivityIn], dependencies: List[Dependency], data_date: _dt.date,
            calendar: Optional[Calendar] = None, project_start: Optional[_dt.date] = None,
            near_critical_days: int = 5) -> ScheduleResult:
    cal = calendar or Calendar()
    acts = {a.id: a for a in activities}
    deps = [d for d in dependencies if d.pred in acts and d.succ in acts and d.pred != d.succ]
    order = topological(list(acts), deps)
    preds: Dict[str, List[Dependency]] = {i: [] for i in acts}
    succs: Dict[str, List[Dependency]] = {i: [] for i in acts}
    for d in deps:
        preds[d.succ].append(d)
        succs[d.pred].append(d)
    starts = [a.planned_start or a.baseline_start or a.actual_start for a in activities]
    start = project_start or min((s for s in starts if s), default=data_date)
    dd = cal.next_work(data_date)

    es: Dict[str, _dt.date] = {}
    ef: Dict[str, _dt.date] = {}
    out: Dict[str, ActivityOut] = {}

    def finish_point(pid: str) -> _dt.date:
        """When a predecessor is 'finished' for FS logic (end of that day)."""
        if acts[pid].type == "start_milestone":
            return cal.prev_work(es[pid] - DAY)
        return ef[pid]

    for aid in order:
        a = acts[aid]
        duration = 0 if a.type in MILESTONES else max(1, int(a.duration or 1))
        milestone = a.type in MILESTONES
        notes: List[str] = []
        # ---- candidates for the early start, each with the reason it binds
        cands: List[Tuple[_dt.date, Dict]] = [(cal.next_work(start), {"kind": "project_start"})]
        finish_cands: List[Tuple[_dt.date, Dict]] = []
        if a.constraint_start:
            cands.append((cal.next_work(a.constraint_start), {"kind": "constraint",
                                                               "date": a.constraint_start.isoformat()}))
        for d in preds[aid]:
            base = {"kind": "predecessor", "pred": d.pred, "pred_code": acts[d.pred].code,
                    "type": d.type, "lag": d.lag}
            if d.type == "FS":
                if a.type == "finish_milestone":
                    finish_cands.append((cal.add(finish_point(d.pred), d.lag), base))
                else:
                    cands.append((cal.add(finish_point(d.pred), 1 + d.lag), base))
            elif d.type == "SS":
                cands.append((cal.add(es[d.pred], d.lag), base))
            elif d.type == "FF":
                finish_cands.append((cal.add(ef[d.pred], d.lag), base))
            elif d.type == "SF":
                finish_cands.append((cal.add(es[d.pred], d.lag), base))

        if a.actual_finish:
            status = "complete"
            start_day = a.actual_start or a.actual_finish
            finish_day = a.actual_finish
            driver = {"kind": "actual", "actual_start": _iso(start_day), "actual_finish": _iso(finish_day)}
            remaining = 0
        elif a.actual_start:
            status = "in_progress"
            start_day = a.actual_start
            remaining = _remaining(a, duration, cal, dd)
            resume = [(dd, {"kind": "data_date", "date": dd.isoformat()})]
            if a.blocked_until:
                resume.append((cal.add(a.blocked_until, 1), {"kind": "blocker", "blocker_id": a.blocker_id,
                                                              "expected_clear": a.blocked_until.isoformat()}))
            elif a.blocker_open_no_date:
                notes.append("an open blocker has no expected clearance date - the forecast assumes work "
                             "resumes at the data date")
            for d in preds[aid]:            # retained logic: unfinished FS predecessors hold the rest
                if d.type == "FS" and not acts[d.pred].actual_finish:
                    resume.append((cal.add(finish_point(d.pred), 1 + d.lag),
                                   {"kind": "predecessor", "pred": d.pred, "pred_code": acts[d.pred].code,
                                    "type": d.type, "lag": d.lag, "retained_logic": True}))
            resume_day, driver = max(resume, key=_binding)
            driver = dict(driver, actual_start=_iso(start_day), remaining=remaining)
            if remaining > 0:
                finish_day = cal.add(cal.next_work(resume_day), remaining - 1)
            else:
                finish_day = max(start_day, cal.prev_work(dd - DAY))
                notes.append("no remaining duration but no actual finish reported")
        else:
            status = "not_started"
            cands.append((dd, {"kind": "data_date", "date": dd.isoformat()}))
            if a.blocked_until:
                cands.append((cal.add(a.blocked_until, 1), {"kind": "blocker", "blocker_id": a.blocker_id,
                                                             "expected_clear": a.blocked_until.isoformat()}))
            remaining = duration
            if milestone:
                start_day, driver = max(cands + finish_cands, key=_binding)
                finish_day = start_day
            else:
                start_day, driver = max(cands, key=_binding)
                finish_day = cal.add(start_day, duration - 1)
                if finish_cands:
                    fc_day, fc_driver = max(finish_cands, key=lambda item: item[0])
                    if fc_day > finish_day:
                        finish_day = fc_day
                        start_day = cal.add(finish_day, -(duration - 1))
                        driver = fc_driver
        es[aid], ef[aid] = start_day, finish_day
        out[aid] = ActivityOut(
            id=aid, code=a.code, name=a.name, type=a.type, duration=duration, status=status,
            es=start_day.isoformat(), ef=finish_day.isoformat(), remaining=remaining,
            percent_complete=(100.0 if status == "complete" else a.percent_complete),
            driver=driver, planned_start=_iso(a.planned_start), planned_finish=_iso(a.planned_finish),
            baseline_start=_iso(a.baseline_start), baseline_finish=_iso(a.baseline_finish),
            finish_variance_days=((finish_day - a.baseline_finish).days if a.baseline_finish else None),
            blocked_until=_iso(a.blocked_until), blocker_id=a.blocker_id, notes=notes)

    # ---- backward pass
    # the activity that finishes last - on a tie, the open end of the network (no
    # successors), then a finish milestone: that is the project's finish
    finish_id = max(order, key=lambda i: (ef[i], not succs[i], acts[i].type == "finish_milestone"))
    project_finish = ef[finish_id]
    lf: Dict[str, _dt.date] = {}
    ls: Dict[str, _dt.date] = {}
    for aid in reversed(order):
        a = acts[aid]
        duration = out[aid].duration
        bounds = [project_finish]
        for d in succs[aid]:
            s = acts[d.succ]
            if d.type == "FS":
                if s.type == "finish_milestone":
                    bounds.append(cal.add(lf[d.succ], -d.lag))
                elif a.type == "start_milestone":
                    bounds.append(cal.add(ls[d.succ], -d.lag))
                else:
                    bounds.append(cal.add(ls[d.succ], -(1 + d.lag)))
            elif d.type == "SS":
                bounds.append(cal.add(cal.add(ls[d.succ], -d.lag), max(0, duration - 1)))
            elif d.type == "FF":
                bounds.append(cal.add(lf[d.succ], -d.lag))
            elif d.type == "SF":
                bounds.append(cal.add(cal.add(lf[d.succ], -d.lag), max(0, duration - 1)))
        lf[aid] = min(bounds)
        ls[aid] = lf[aid] if duration == 0 else cal.add(lf[aid], -(duration - 1))
        o = out[aid]
        o.lf, o.ls = lf[aid].isoformat(), ls[aid].isoformat()
        if o.status != "complete":
            o.total_float = cal.diff(ef[aid], lf[aid])
            o.critical = o.total_float <= 0
            o.near_critical = 0 < o.total_float <= near_critical_days
    for aid in order:
        o = out[aid]
        if o.status == "complete":
            continue
        gaps = []
        for d in succs[aid]:
            if d.type == "FS" and acts[d.succ].type != "finish_milestone":
                gaps.append(cal.diff(ef[aid], cal.add(es[d.succ], -(1 + d.lag))))
        o.free_float = min(gaps) if gaps else o.total_float

    # ---- the driving path back from the finish
    path, cursor, seen = [], finish_id, set()
    while cursor and cursor not in seen:
        seen.add(cursor)
        path.append(cursor)
        drv = out[cursor].driver
        cursor = drv.get("pred") if drv.get("kind") == "predecessor" else None
    return ScheduleResult(data_date=dd.isoformat(), project_finish=project_finish.isoformat(),
                          finish_activity=finish_id, activities=out, critical_path=list(reversed(path)),
                          calendar=cal.as_dict())


_PRIORITY = {"blocker": 3, "predecessor": 2, "constraint": 1}


def _binding(item: Tuple[_dt.date, Dict]):
    """The latest candidate binds; on a tie the more informative cause is named
    (a blocker or a predecessor, rather than 'the data date')."""
    return item[0], _PRIORITY.get(item[1].get("kind"), 0)


def _remaining(a: ActivityIn, duration: int, cal: Calendar, dd: _dt.date) -> int:
    """Remaining working days of an activity in progress - from the most direct
    evidence available: an explicit remaining duration, then the reported
    percent complete, then (no progress information at all) the planned
    duration less the working days already elapsed."""
    if a.remaining is not None:
        return max(0, int(a.remaining))
    if a.percent_complete is not None:
        return max(0, int(math.ceil(duration * (1 - float(a.percent_complete) / 100.0) - 1e-9)))
    elapsed = max(0, cal.diff(a.actual_start, dd)) if a.actual_start else 0
    return max(1, duration - elapsed)
