"""Drift-guard: every shipped agent source carries the exact do-not-edit banner.

Mirrors test_mcp_tool_inventory.py — dependency-free (std-lib + pytest only).

Shipped agents live in plugin/agents/*.md and are PLUGIN-OWNED: the plugin dir is
replaced wholesale on every plugin update, so a user who edits one in place loses
the change on the next update. CLAUDE.md in plugin/agents/ is module documentation,
not a shipped agent, and is excluded. Each shipped agent must carry the *exact*
canonical banner baked into its source, immediately under its frontmatter, so:
  * a human opening the file is told not to edit it in place (copy it to a new
    name under .claude/agents/ to customize — see CLAUDE.tm.md), and
  * the byte-identical banner keeps any content-compare deploy of plugin-owned
    files (the refresh-on-change pattern shared_state._refresh_file_if_changed
    implements for the guidance files) a clean raw-vs-raw compare, so an unedited
    agent never churns on update.

This guard fails if a new shipped agent is added without the banner, or the
banner is removed, truncated, reworded, duplicated, or moved out from under the
frontmatter.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / "plugin" / "agents"

# CLAUDE.md is module documentation, not a shipped agent — excluded from the banner.
EXCLUDED = {"CLAUDE.md"}

# The exact, canonical do-not-edit banner. Asserted byte-for-byte (not as a
# substring) so a truncated or reworded banner is caught as drift.
BANNER = (
    '<!-- DO NOT EDIT - managed by team-management; replaced on every update. '
    'To customize, copy this file to a new name in .claude/agents/ '
    '(e.g. my-code-review.md) and edit the copy. '
    'See CLAUDE.tm.md "Customizing shipped agents". -->'
)


def shipped_agents():
    return sorted(p for p in AGENTS_DIR.glob("*.md") if p.name not in EXCLUDED)


def test_agents_dir_has_shipped_agents():
    assert shipped_agents(), f"no shipped agent files found in {AGENTS_DIR}"


@pytest.mark.parametrize("agent_path", shipped_agents(), ids=lambda p: p.name)
def test_shipped_agent_has_exact_banner_once(agent_path):
    # Exact, byte-identical banner appearing exactly once — catches a missing,
    # truncated, reworded, or duplicated banner (a substring match would not).
    text = agent_path.read_text(encoding="utf-8")
    count = text.count(BANNER)
    assert count == 1, (
        f"{agent_path.name}: expected the canonical banner exactly once, found {count}. "
        "Bake the exact banner under the frontmatter."
    )


@pytest.mark.parametrize("agent_path", shipped_agents(), ids=lambda p: p.name)
def test_banner_sits_directly_under_frontmatter(agent_path):
    text = agent_path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{agent_path.name} has no opening frontmatter fence"
    # Split off the frontmatter at its closing '---' line.
    parts = text.split("\n---", 1)
    assert len(parts) == 2, f"{agent_path.name}: frontmatter is not closed"
    after = parts[1]  # starts right after the closing '---' (before its newline)
    # Require exactly one newline between the closing '---' and the banner — i.e.
    # the banner is on the very next line, with no blank line in between.
    assert after.startswith("\n" + BANNER), (
        f"{agent_path.name}: the canonical banner must sit on the line "
        "immediately after the frontmatter close (one newline, no blank line)."
    )


def test_claude_md_is_excluded():
    # Documents the exclusion contract: CLAUDE.md is module documentation, not a
    # shipped agent, and is intentionally not required to carry the banner.
    assert (AGENTS_DIR / "CLAUDE.md").exists()
    assert "CLAUDE.md" in EXCLUDED
