"""Public-export-only release contract tests.

SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
SPDX-FileContributor: Lorenzo Massaro
SPDX-License-Identifier: AGPL-3.0-only
"""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "agvm_api"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

from mcp_contracts import (  # noqa: E402
    AGENT_MEMORY_MCP_TOOL_NAMES,
    GUIDE_MCP_TOOL_NAMES,
    REQUIRED_MCP_TOOL_NAMES,
)
from route_classification import classify_mcp_tool  # noqa: E402


def test_public_release_tree_has_no_private_runtime_roots() -> None:
    assert (ROOT / ".agvm-public-export-marker").is_file()
    assert not (ROOT / "platform").exists()
    assert not (ROOT / "apps").exists()
    assert not (ROOT / "agvm_cockpit_prototype" / "src" / "new-ui" / "modules").exists()


def test_public_mcp_contract_has_37_visible_tools_and_free_grow() -> None:
    tool_names = [*GUIDE_MCP_TOOL_NAMES, *REQUIRED_MCP_TOOL_NAMES, *AGENT_MEMORY_MCP_TOOL_NAMES]

    assert len(tool_names) == 37
    assert len(set(tool_names)) == 37
    for name in tool_names:
        classification = classify_mcp_tool(name)
        assert classification is not None, name

    for name in ("grow_source_preview", "grow_source_status", "grow_source_apply", "grow_preview", "grow_apply"):
        classification = classify_mcp_tool(name)
        assert classification is not None
        assert classification.category == "core"
        assert classification.public_core_allowed is True

    for name in ("sleep_preview", "sleep_apply", "evolve_preview", "evolve_apply", "matrix_calibration_preview"):
        classification = classify_mcp_tool(name)
        assert classification is not None
        assert classification.category == "paid_module"
        assert classification.public_core_allowed is False


def test_public_docs_describe_visibility_without_claiming_authorization() -> None:
    modules = (ROOT / "docs" / "modules.md").read_text(encoding="utf-8")
    cloud = (ROOT / "docs" / "cloud-and-pro.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    combined = "\n".join((modules, cloud, changelog))

    assert "37" in modules
    assert "Grow" in modules and "Core" in modules
    assert "module_tool_not_enabled_by_local_mcp_lease" in modules
    assert "visibility is never treated as authorization" in cloud
    assert "structured Detwin Cloud action contract" in changelog


def test_public_ci_contains_release_hygiene_gates() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    for expected in (
        "python -m pytest",
        "check_public_export.py",
        "python -m reuse lint",
        "gitleaks",
        "docker compose build",
        "docker compose up",
    ):
        assert expected in workflow


def test_public_ui_is_the_rich_local_product_shell() -> None:
    app = (ROOT / "agvm_cockpit_prototype" / "src" / "App.tsx").read_text(encoding="utf-8")
    styles = (
        ROOT / "agvm_cockpit_prototype" / "src" / "new-ui" / "neural-cockpit.css"
    ).read_text(encoding="utf-8")

    for marker in (
        "Local Workspace",
        "Guided growth",
        "Grow Workspace",
        "Operator runway",
        "Text",
        "URL",
        "Website",
        "PDF",
        "Transcript",
        "BrainThreeScene",
        "Canvas",
    ):
        assert marker in app

    assert 'label: "Clone"' not in app
    assert 'label: "Teach"' not in app
    assert 'label: "Maintain"' not in app
    assert "core-layout" in styles
    assert "grow-workbench" in styles
    assert "brain-three-canvas" in styles
    assert "color-scheme: light" in styles
