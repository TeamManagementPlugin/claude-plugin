"""Tests for plugin/scripts/recover_tsv.py.

Recovers `results.tsv` from `.results.tsv.bak` for an optimize task.
TSV schema (9 columns, tab-separated, see protocol_engine.py:3497-3500):
    iteration  timestamp  commit_sha  metric_value  run_count
    aggregator wall_clock_s  status  hypothesis
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "plugin" / "scripts" / "recover_tsv.py"


HEADER = (
    "iteration\ttimestamp\tcommit_sha\tmetric_value\trun_count\t"
    "aggregator\twall_clock_s\tstatus\thypothesis\n"
)
ROW_1 = "1\t2026-05-05T10:00:00Z\tabc1234\t1.5\t3\tmedian\t0.42\tok\tbaseline-tweak\n"
ROW_2 = "2\t2026-05-05T10:05:00Z\tdef5678\t1.2\t3\tmedian\t0.38\tok\tinline-cache\n"
GOOD_TSV = HEADER + ROW_1 + ROW_2


def _make_task_dir(tasks_root: Path, task: str) -> Path:
    task_dir = tasks_root / task
    task_dir.mkdir(parents=True, exist_ok=True)
    return task_dir


def _run_script(tmp_tasks_root: Path, task: str, *extra: str) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable,
        str(SCRIPT_PATH),
        "--task",
        task,
        "--tasks-root",
        str(tmp_tasks_root),
        *extra,
    ]
    return subprocess.run(cmd, capture_output=True, text=True)


@pytest.fixture
def tasks_root(tmp_path: Path) -> Path:
    root = tmp_path / "team-management" / "tasks"
    root.mkdir(parents=True)
    return root


def test_recovery_succeeds_on_corrupt_main_clean_bak(tasks_root: Path):
    task = "o-perf-tuning"
    task_dir = _make_task_dir(tasks_root, task)
    (task_dir / ".results.tsv.bak").write_text(GOOD_TSV, encoding="utf-8")
    # Simulate corruption: truncated mid-row, missing newline
    (task_dir / "results.tsv").write_text(HEADER + "1\t2026-05-05T10:00:00Z\tabc12", encoding="utf-8")

    proc = _run_script(tasks_root, task)

    assert proc.returncode == 0, f"stderr={proc.stderr!r} stdout={proc.stdout!r}"
    assert (task_dir / "results.tsv").read_text(encoding="utf-8") == GOOD_TSV


def test_missing_bak_exits_1(tasks_root: Path):
    task = "o-no-bak"
    task_dir = _make_task_dir(tasks_root, task)
    (task_dir / "results.tsv").write_text("garbage", encoding="utf-8")

    proc = _run_script(tasks_root, task)

    assert proc.returncode == 1
    # results.tsv must be unchanged (no overwrite on bak-missing)
    assert (task_dir / "results.tsv").read_text(encoding="utf-8") == "garbage"
    assert "bak" in (proc.stderr + proc.stdout).lower()


def test_malformed_row_in_bak_exits_2(tasks_root: Path):
    task = "o-malformed"
    task_dir = _make_task_dir(tasks_root, task)
    # Row 2 has only 5 columns instead of 9 — schema violation
    bad_bak = HEADER + ROW_1 + "2\t2026-05-05T10:05:00Z\tdef5678\t1.2\t3\n"
    (task_dir / ".results.tsv.bak").write_text(bad_bak, encoding="utf-8")
    original_main = "do-not-touch"
    (task_dir / "results.tsv").write_text(original_main, encoding="utf-8")

    proc = _run_script(tasks_root, task)

    assert proc.returncode == 2
    # Diagnostic must point at the offending row number
    combined = proc.stderr + proc.stdout
    assert "row" in combined.lower() or "line" in combined.lower()
    # Main TSV must not be overwritten when validation fails
    assert (task_dir / "results.tsv").read_text(encoding="utf-8") == original_main


def test_non_numeric_iteration_in_bak_exits_2(tasks_root: Path):
    """Row with non-int iteration must be rejected (engine drops such rows silently)."""
    task = "o-bad-iteration"
    task_dir = _make_task_dir(tasks_root, task)
    bad_bak = HEADER + ROW_1 + "two\t2026-05-05T10:05:00Z\tdef5678\t1.2\t3\tmedian\t0.38\tok\twhatever\n"
    (task_dir / ".results.tsv.bak").write_text(bad_bak, encoding="utf-8")
    (task_dir / "results.tsv").write_text("do-not-touch", encoding="utf-8")

    proc = _run_script(tasks_root, task)

    assert proc.returncode == 2, f"stderr={proc.stderr!r} stdout={proc.stdout!r}"
    assert "iteration" in (proc.stderr + proc.stdout).lower()
    assert (task_dir / "results.tsv").read_text(encoding="utf-8") == "do-not-touch"


def test_non_numeric_metric_value_in_ok_row_exits_2(tasks_root: Path):
    """Row with status=ok but non-float metric_value must be rejected.
    Crash rows are exempt — the engine writes the raw failing value verbatim
    on a crash, so we only enforce the float type when status != 'crash'.
    """
    task = "o-bad-metric"
    task_dir = _make_task_dir(tasks_root, task)
    bad_bak = HEADER + ROW_1 + "2\t2026-05-05T10:05:00Z\tdef5678\tnot-a-float\t3\tmedian\t0.38\tok\thypothesis\n"
    (task_dir / ".results.tsv.bak").write_text(bad_bak, encoding="utf-8")
    (task_dir / "results.tsv").write_text("do-not-touch", encoding="utf-8")

    proc = _run_script(tasks_root, task)

    assert proc.returncode == 2
    assert "metric" in (proc.stderr + proc.stdout).lower()


def test_crash_row_with_non_numeric_metric_value_is_accepted(tasks_root: Path):
    """status=crash rows preserve the raw failing value; the validator must accept them."""
    task = "o-crash-row"
    task_dir = _make_task_dir(tasks_root, task)
    crash_row = "2\t2026-05-05T10:05:00Z\tdef5678\tboom-not-a-float\t3\tmedian\t0.38\tcrash\thypothesis\n"
    crash_bak = HEADER + ROW_1 + crash_row
    (task_dir / ".results.tsv.bak").write_text(crash_bak, encoding="utf-8")
    (task_dir / "results.tsv").write_text("corrupt", encoding="utf-8")

    proc = _run_script(tasks_root, task)

    assert proc.returncode == 0, f"stderr={proc.stderr!r} stdout={proc.stdout!r}"
    assert (task_dir / "results.tsv").read_text(encoding="utf-8") == crash_bak


def test_dry_run_writes_nothing(tasks_root: Path):
    task = "o-dry-run"
    task_dir = _make_task_dir(tasks_root, task)
    (task_dir / ".results.tsv.bak").write_text(GOOD_TSV, encoding="utf-8")
    original_main = HEADER + "corrupt-leftover\n"
    (task_dir / "results.tsv").write_text(original_main, encoding="utf-8")

    proc = _run_script(tasks_root, task, "--dry-run")

    assert proc.returncode == 0
    assert (task_dir / "results.tsv").read_text(encoding="utf-8") == original_main
    assert "dry-run" in proc.stdout.lower() or "would" in proc.stdout.lower()
