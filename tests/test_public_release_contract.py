"""Public-export-only release contract tests.

SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
SPDX-FileContributor: Lorenzo Massaro
SPDX-License-Identifier: AGPL-3.0-only
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


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


def test_public_release_excludes_paid_profile_and_geometry_implementations() -> None:
    for forbidden in (
        "agvm_api/brain_profile_benchmark_v1.py",
        "agvm_api/brain_profile_v1_api/authority.py",
        "agvm_api/brain_profile_v1_api/service.py",
        "agvm_api/brain_profile_v1_api/store.py",
        "agvm_api/matrix_revisioning.py",
    ):
        assert not (ROOT / forbidden).exists(), forbidden

    runtime = (ROOT / "agvm_api" / "brain_profile_runtime.py").read_text(encoding="utf-8")
    geometry = (ROOT / "agvm_api" / "geometry_calibration.py").read_text(encoding="utf-8")
    matrix_router = (ROOT / "agvm_api" / "core_mcp_matrix_router.py").read_text(encoding="utf-8")
    store = (ROOT / "agvm_api" / "sqlite_store.py").read_text(encoding="utf-8")
    assert "PUBLIC_CLOUD_ACTION_STUB = True" in runtime
    assert "PUBLIC_CLOUD_ACTION_STUB = True" in geometry
    assert "PUBLIC_CLOUD_ACTION_STUB = True" in matrix_router
    for forbidden_symbol in (
        "def apply_geometry_calibration_position_updates_with_revisions(",
        "def rollback_geometry_calibration_operation(",
        "apply_matrix_calibration_position_updates_with_revisions =",
    ):
        assert forbidden_symbol not in store


def test_public_paid_routes_return_cloud_action_contracts() -> None:
    from brain_profile_v1_api import create_brain_profile_v1_router
    from core_mcp_matrix_router import create_core_mcp_matrix_router

    app = FastAPI()
    app.include_router(create_brain_profile_v1_router())
    app.include_router(create_core_mcp_matrix_router())
    client = TestClient(app)
    for path in (
        "/mcp/brain-profile-preview",
        "/mcp/brain-profile-apply",
        "/mcp/brain-profile-rollback",
        "/mcp/geometry-calibration-preview",
        "/mcp/geometry-calibration-apply",
        "/mcp/geometry-calibration-rollback",
        "/mcp/matrix-calibration-preview",
        "/mcp/matrix-calibration-apply",
    ):
        response = client.post(path, json={})
        assert response.status_code == 200, path
        payload = response.json()
        assert payload["status"] == "blocked", path
        assert payload["reason"] == "detwin_cloud_execution_required", path
        action = payload["action_contract"]
        assert action["action"] == "use_detwin_cloud_for_advanced_tool", path
        assert action["execution_surface"] == "hosted_mcp", path
        assert action["requires_account"] is True, path
        assert action["requires_credits"] is True, path
        assert action["dynamic_usage_settlement"] is True, path
        assert action["local_execution_available"] is False, path


def test_public_release_tree_has_no_private_runtime_roots() -> None:
    assert (ROOT / ".agvm-public-export-marker").is_file()
    assert not (ROOT / "platform").exists()
    assert not (ROOT / "apps").exists()
    assert not (ROOT / "agvm_cockpit_prototype" / "src" / "new-ui" / "modules").exists()


def test_public_mcp_contract_has_v1_bootstrap_profile_and_free_grow() -> None:
    tool_names = [*GUIDE_MCP_TOOL_NAMES, *REQUIRED_MCP_TOOL_NAMES, *AGENT_MEMORY_MCP_TOOL_NAMES]

    assert len(tool_names) == 52
    assert len(set(tool_names)) == 52
    assert {
        "brain_bootstrap_start",
        "brain_bootstrap_status",
        "brain_bootstrap_answer",
        "brain_bootstrap_add_source",
        "brain_bootstrap_preview",
        "brain_bootstrap_apply",
        "brain_bootstrap_resume",
        "brain_bootstrap_recover",
        "brain_bootstrap_cancel",
        "brain_profile_preview",
        "brain_profile_apply",
        "brain_profile_rollback",
    }.issubset(tool_names)
    for name in tool_names:
        classification = classify_mcp_tool(name)
        assert classification is not None, name

    for name in ("grow_source_preview", "grow_source_status", "grow_source_apply", "grow_preview", "grow_apply"):
        classification = classify_mcp_tool(name)
        assert classification is not None
        assert classification.category == "core"
        assert classification.public_core_allowed is True

    for name in (
        "sleep_preview",
        "sleep_apply",
        "evolve_preview",
        "evolve_apply",
        "geometry_calibration_preview",
        "geometry_calibration_apply",
        "geometry_calibration_rollback",
        "matrix_calibration_preview",
    ):
        classification = classify_mcp_tool(name)
        assert classification is not None
        assert classification.category == "paid_module"
        assert classification.public_core_allowed is False


def test_public_docs_describe_visibility_without_claiming_authorization() -> None:
    modules = (ROOT / "docs" / "modules.md").read_text(encoding="utf-8")
    local_mcp = (ROOT / "docs" / "local-mcp.md").read_text(encoding="utf-8")
    cloud = (ROOT / "docs" / "cloud-and-pro.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    combined = "\n".join((modules, local_mcp, cloud, changelog))

    assert "complete current contract catalog" in modules
    assert "52" in local_mcp
    assert "brain_bootstrap_*" in local_mcp
    assert "brain_profile_*" in local_mcp
    assert "Grow" in modules and "Core" in modules
    assert "module_tool_not_enabled_by_local_mcp_lease" in modules
    assert "visibility is never treated as authorization" in cloud
    assert "structured Detwin Cloud action contract" in changelog


def test_public_ci_contains_release_hygiene_gates() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    for expected in (
        "python -m pytest",
        "Public source boundary: PASS",
        '"LicenseRef-Eternal-Tech-" + "Proprietary"',
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
        "agvm-product-shell",
        "context-core-workspace",
        "brain_explorer",
        "Brain Core",
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
    assert 'id: "clone_app"' not in app
    assert 'id: "maintain", label: "Maintain", eyebrow: "Cloud-backed"' in app
    for forbidden_import in (
        "/clone-app/",
        "/modules/clone",
        "/ops/",
        "/persona-agent/",
        "/platform/",
    ):
        assert forbidden_import not in app
    assert "core-layout" in styles
    assert "agvm-product-layout" in styles
    assert "context-live-layout" in styles
    assert "brain-density-controls" in styles
    assert "grow-workbench" in styles
    assert "brain-three-canvas" in styles
    assert "color-scheme: light" in styles


def test_every_commentable_public_file_has_owner_contributor_and_license_headers() -> None:
    hash_suffixes = {".cfg", ".conf", ".ini", ".py", ".ps1", ".sh", ".toml", ".yaml", ".yml"}
    slash_suffixes = {".cjs", ".js", ".mjs", ".ts", ".tsx"}
    block_suffixes = {".css", ".html", ".md", ".mdx", ".scss", ".xml"}
    special_names = {".dockerignore", ".env.example", ".gitignore", "Makefile"}
    generated_directories = {".git", ".pytest_cache", "__pycache__", "dist", "node_modules"}
    generated_files = {
        "agvm_cockpit_prototype/vite.config.d.ts",
        "agvm_cockpit_prototype/vite.config.js",
    }
    commentable: list[Path] = []
    for path in sorted(item for item in ROOT.rglob("*") if item.is_file()):
        if generated_directories.intersection(path.relative_to(ROOT).parts):
            continue
        relative = path.relative_to(ROOT).as_posix()
        if relative in generated_files:
            continue
        name = path.name
        suffix = path.suffix.lower()
        if (
            name.startswith("Dockerfile")
            or name in special_names
            or (name.startswith("requirements") and suffix == ".txt")
            or suffix in hash_suffixes | slash_suffixes | block_suffixes
        ):
            commentable.append(path)
            text = path.read_text(encoding="utf-8")
            expected_license = "Apache-2.0" if relative.startswith("sdk/") else "AGPL-3.0-only"
            assert "SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>" in text, relative
            assert "SPDX-FileContributor: Lorenzo Massaro" in text, relative
            license_marker = "SPDX-License-" + f"Identifier: {expected_license}"
            assert license_marker in text, relative

    assert commentable, "public release header gate did not inspect any files"
