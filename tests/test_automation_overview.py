"""Automation screen: every background job is listed with its id, kind, status,
runs and errors, and can be cancelled from the list."""

import time

import pytest

from core.builtin_tools import automation_overview, cancel_task, remind_me, stop_watching_folder, watch_folder


def _task(overview, task_id):
    return next(t for t in overview["tasks"] if t["id"] == task_id)


def test_a_reminder_is_listed_and_cancelled_from_the_list():
    started = remind_me("check the P-101 seal", 30)
    task_id = started["task_id"]
    row = _task(automation_overview(), task_id)
    assert row["kind"] == "reminder" and row["status"] == "WAITING_FOR_USER" and row["cancellable"]
    cancel_task(task_id)
    row = _task(automation_overview(), task_id)
    assert row["status"] == "CANCELLED" and not row["cancellable"]


def test_a_folder_watch_counts_its_runs(tmp_path):
    pytest.importorskip("watchdog")
    started = watch_folder(str(tmp_path))
    if "error" in started:
        pytest.skip(started["error"])
    task_id = started["task_id"]
    try:
        (tmp_path / "new_report.txt").write_text("x", encoding="utf-8")
        deadline = time.time() + 10
        while time.time() < deadline and _task(automation_overview(), task_id)["runs"] == 0:
            time.sleep(0.2)
        row = _task(automation_overview(), task_id)
        assert row["kind"] == "folder watch" and row["status"] == "RUNNING" and row["runs"] >= 1
        assert "new_report.txt" in row["last_run"]
    finally:
        stop_watching_folder(str(tmp_path))
    assert _task(automation_overview(), task_id)["status"] == "CANCELLED"
