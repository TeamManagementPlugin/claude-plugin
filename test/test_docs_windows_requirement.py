#!/usr/bin/env python3
"""Structural guard for the Windows install documentation
(m-docs-windows-init-requirement).

The Windows plugin launch requires running `/team-management:init` once: the
static manifests invoke the `python3` token, which Windows lacks (only
`python.exe` / the `py` launcher), so init provisions a real `python3.exe`.
That requirement is discoverability-gated — a user who skips init hits
`python3: command not found` with no hint (this is exactly how the maintainer
got stuck on the v0.3.7 update). These tests pin the user-facing docs so a
future edit cannot silently drop the Windows requirement, its self-diagnosis
symptom, or the concrete marketplace slug.

Run with: python3 -m pytest test/test_docs_windows_requirement.py -v
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
INSTALL = REPO_ROOT / "docs" / "INSTALL.md"


def _readme():
    return README.read_text(encoding="utf-8")


def _install():
    return INSTALL.read_text(encoding="utf-8")


def test_readme_shows_concrete_marketplace_slug():
    """The README install command must show the real marketplace slug, not only a
    `<placeholder>`, so the install line is copy-pasteable."""
    assert "TeamManagementPlugin/claude-plugin" in _readme()


def test_readme_documents_windows_init_requirement():
    """README must state that on Windows `/team-management:init` is required and name
    the python3 provisioning so the user understands WHY."""
    text = _readme()
    assert "Windows" in text
    assert "/team-management:init" in text
    assert "python3" in text
    assert "required" in text.lower()


def test_readme_documents_windows_symptom():
    """The self-diagnosis symptom must be present so a stuck user can match it."""
    assert "command not found" in _readme()


def test_install_documents_windows_init_requirement():
    text = _install()
    assert "Windows" in text
    assert "/team-management:init" in text
    assert "python3" in text


def test_install_documents_windows_symptom():
    assert "command not found" in _install()


def test_install_prerequisite_names_py_launcher_for_windows():
    """§1 Prerequisites must not leave the impression that a bare `python3` exists on
    Windows — it must name the `py` launcher as the Windows path. Scoped to the §1 slice
    (everything before the `## 2.` heading) so a revert of the §1 correction is caught even
    though `py launcher` also legitimately appears later in §2's init note."""
    section1 = _install().split("## 2.")[0]
    assert "py launcher" in section1.lower().replace("`", "")
    assert "python3" in section1  # §1 must name the Windows python3 gap, not just §2


def test_install_shows_concrete_marketplace_slug():
    assert "TeamManagementPlugin/claude-plugin" in _install()
