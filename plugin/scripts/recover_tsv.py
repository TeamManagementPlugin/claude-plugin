#!/usr/bin/env python3
"""Recover an optimize-protocol task's `results.tsv` from `.results.tsv.bak`.

The protocol engine rotates the backup every 100 data rows
(see protocol_engine.py `_func_log_experiment_result`). If the main file
is corrupted (e.g. by a crashed metric writer), this script rewrites it
from the validated backup.

Usage:
    python3 plugin/scripts/recover_tsv.py --task <task-name>
    python3 plugin/scripts/recover_tsv.py --task <task-name> --dry-run
    python3 plugin/scripts/recover_tsv.py --task <task-name> --tasks-root <path>

Exit codes:
    0  recovery succeeded (or dry-run completed)
    1  `.results.tsv.bak` missing
    2  malformed row in `.results.tsv.bak`
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


EXPECTED_COLUMNS = 9
EXPECTED_HEADER = (
    "iteration\ttimestamp\tcommit_sha\tmetric_value\trun_count\t"
    "aggregator\twall_clock_s\tstatus\thypothesis"
)


def _validate_rows(content: str) -> tuple[bool, str]:
    """Return (ok, diagnostic). Validates structure + field types per the
    engine's TSV writer contract:
      - 9 tab-separated columns (header included)
      - column 0 (`iteration`) is an int (engine writes -1 for summary rows)
      - column 3 (`metric_value`) is a float — EXEMPT when col 7 (`status`)
        is "crash", because the engine preserves the raw failing value verbatim
        on crash rows
      - column 4 (`run_count`) is an int
      - column 6 (`wall_clock_s`) is a float
    Strings (timestamp, commit_sha, aggregator, status, hypothesis) are not
    type-checked because they are written as-is by the engine.
    """
    lines = content.splitlines()
    if not lines:
        return False, "empty backup file"
    if lines[0] != EXPECTED_HEADER:
        return False, f"row 1 (header): unexpected schema; got {lines[0]!r}"
    for idx, line in enumerate(lines[1:], start=2):
        if not line:
            continue
        cols = line.split("\t")
        if len(cols) != EXPECTED_COLUMNS:
            return False, f"row {idx}: expected {EXPECTED_COLUMNS} tab-separated columns, got {len(cols)}"
        try:
            int(cols[0])
        except ValueError:
            return False, f"row {idx}: 'iteration' is not an int (got {cols[0]!r})"
        try:
            int(cols[4])
        except ValueError:
            return False, f"row {idx}: 'run_count' is not an int (got {cols[4]!r})"
        try:
            float(cols[6])
        except ValueError:
            return False, f"row {idx}: 'wall_clock_s' is not a float (got {cols[6]!r})"
        if cols[7] != "crash":
            try:
                float(cols[3])
            except ValueError:
                return False, f"row {idx}: 'metric_value' is not a float on a non-crash row (got {cols[3]!r})"
    return True, ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--task", required=True, help="Task name (e.g. o-perf-tuning)")
    parser.add_argument(
        "--tasks-root",
        default="team-management/tasks",
        help="Root directory containing task directories (default: team-management/tasks)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report what would be written without modifying files")
    args = parser.parse_args(argv)

    task_dir = Path(args.tasks_root) / args.task
    bak_path = task_dir / ".results.tsv.bak"
    main_path = task_dir / "results.tsv"

    if not bak_path.exists():
        print(f"ERROR: backup not found at {bak_path}", file=sys.stderr)
        return 1

    backup_content = bak_path.read_text(encoding="utf-8")
    ok, diagnostic = _validate_rows(backup_content)
    if not ok:
        print(f"ERROR: malformed bak — {diagnostic}", file=sys.stderr)
        return 2

    if args.dry_run:
        row_count = max(0, len(backup_content.splitlines()) - 1)
        print(f"dry-run: would overwrite {main_path} with {row_count} validated data rows from {bak_path}")
        return 0

    main_path.write_text(backup_content, encoding="utf-8")
    row_count = max(0, len(backup_content.splitlines()) - 1)
    print(f"recovered {main_path}: {row_count} data rows from {bak_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
