#!/usr/bin/env python3
"""Task-scoped state directory management.

Provides TaskStateManager for the per-task state directory under
.claude/state/tasks/<task-name>/. The live surface is the transcripts
subdirectory (subagent transcript staging) and cleanup on task completion;
the former multi-session scaffold (session.json / parallel-session detection /
per-task context-warning flags) was unwired and removed in l-dead-code-removal.

Directory structure:
    .claude/state/
    ├── tasks/                      # Per-task state directories
    │   └── <task-name>/
    │       └── transcripts/        # subagent transcripts
    ├── gitlab-mappings.json        # GLOBAL
    ├── jira-mappings.json          # GLOBAL
    ├── github-mappings.json        # GLOBAL
    └── workflow-bypass.json        # GLOBAL
"""

import shutil
from pathlib import Path


class TaskStateManager:
    """Manages the per-task state directory under .claude/state/tasks/.

    Attributes:
        state_dir: Root state directory (.claude/state)
        tasks_dir: Task state directories (.claude/state/tasks)
    """

    def __init__(self, state_dir: Path):
        """Initialize TaskStateManager.

        Args:
            state_dir: Path to .claude/state directory
        """
        self.state_dir = Path(state_dir)
        self.tasks_dir = self.state_dir / "tasks"

    def get_task_state_dir(self, task_name: str) -> Path:
        """Get path to task-specific state directory.

        Args:
            task_name: Name of the task (e.g., 'h-implement-feature')

        Returns:
            Path to task state directory

        Raises:
            ValueError: If task_name is empty or contains path traversal attempts
        """
        # Reject empty names: an empty name resolves to tasks_dir itself, which
        # would let cleanup_task_state rmtree the entire tasks tree.
        if not task_name or not str(task_name).strip():
            raise ValueError("task_name cannot be empty")

        # Security: Validate task name to prevent path traversal
        task_dir = (self.tasks_dir / task_name).resolve()
        tasks_root = self.tasks_dir.resolve()
        # Reject names that resolve to the tasks root itself (e.g. ".", "./") —
        # same rmtree hazard as an empty name, which the empty-string check misses.
        if task_dir == tasks_root:
            raise ValueError(f"task_name resolves to the tasks root: {task_name!r}")
        try:
            task_dir.relative_to(tasks_root)
        except ValueError:
            raise ValueError(f"Invalid task name - path traversal detected: {task_name}")
        return task_dir

    def get_transcripts_dir(self, task_name: str) -> Path:
        """Get path to task's transcripts directory.

        Args:
            task_name: Name of the task

        Returns:
            Path to transcripts directory (may not exist)
        """
        return self.get_task_state_dir(task_name) / "transcripts"

    def cleanup_task_state(self, task_name: str) -> bool:
        """Remove task state directory on task completion.

        Args:
            task_name: Name of the task

        Returns:
            True if cleanup was performed, False if the name is empty, the
            directory didn't exist, or cleanup failed
        """
        # Guard invalid names (empty, ".", "./", traversal) before they can resolve
        # to the tasks root — cleanup must never rmtree the whole tasks tree or raise.
        if not task_name or not str(task_name).strip():
            return False
        try:
            task_dir = self.get_task_state_dir(task_name)
        except ValueError:
            return False
        if not task_dir.exists():
            return False

        try:
            shutil.rmtree(task_dir)
            return True
        except (PermissionError, OSError):
            # Log but don't fail - state cleanup is non-critical
            # Directory may have locked files on Windows
            return False
