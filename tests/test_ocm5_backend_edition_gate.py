from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI


ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "agvm_api"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

from edition_gate import (  # noqa: E402
    EditionSettings,
    install_edition_route_gate,
    normalize_edition,
    read_edition_settings,
    route_decision,
)


def test_ocm5_core_edition_removes_paid_dev_platform_compat_and_unknown_routes() -> None:
    app = _sample_app()
    report = install_edition_route_gate(app, _settings("core"))
    paths = _paths(app)

    assert "/health" in paths
    assert "/runtime/edition" in paths
    assert "/mcp/retrieve-context" in paths
    assert "/mcp/write-memory-preview" in paths
    assert "/mcp/grow-preview" not in paths
    assert "/mcp/sleep-preview" not in paths
    assert "/clone-app/manifest" not in paths
    assert "/agent-demo/chat-turn" not in paths
    assert "/dev/audit" not in paths
    assert "/hosted/tenants" not in paths
    assert "/preview" not in paths
    assert "/unclassified-experiment" not in paths
    assert report["settings"]["edition"] == "core"
    assert report["removed_route_count"] >= 7


def test_ocm5_dev_edition_preserves_transitional_monolith_routes() -> None:
    app = _sample_app()
    report = install_edition_route_gate(app, _settings("dev"))
    paths = _paths(app)

    assert "/mcp/grow-preview" in paths
    assert "/mcp/sleep-preview" in paths
    assert "/clone-app/manifest" in paths
    assert "/agent-demo/chat-turn" in paths
    assert "/dev/audit" in paths
    assert "/hosted/tenants" in paths
    assert "/preview" in paths
    assert "/unclassified-experiment" in paths
    assert report["removed_route_count"] == 0


def test_ocm5_pro_edition_requires_per_module_flags_for_paid_routes() -> None:
    app = _sample_app()
    settings = _settings(
        "pro",
        compat=True,
        modules={
            "agvm_agent_chat": False,
            "agvm_clone_app": False,
            "agvm_grow_studio": True,
            "agvm_maintain_studio": False,
        },
    )
    install_edition_route_gate(app, settings)
    paths = _paths(app)

    assert "/health" in paths
    assert "/preview" in paths
    assert "/mcp/grow-preview" in paths
    assert "/mcp/sleep-preview" not in paths
    assert "/clone-app/manifest" not in paths
    assert "/agent-demo/chat-turn" not in paths
    assert "/dev/audit" not in paths
    assert "/hosted/tenants" not in paths


def test_ocm5_settings_are_normalized_from_env() -> None:
    assert normalize_edition("public-core") == "core"
    assert normalize_edition("local_pro") == "pro"
    assert normalize_edition("monolith") == "dev"

    core = read_edition_settings({"AGVM_EDITION": "core"})
    assert core.edition == "core"
    assert core.compat_routes_enabled is False
    assert core.dev_routes_enabled is False
    assert core.platform_routes_enabled is False
    assert not any(core.module_owner_enabled.values())

    dev = read_edition_settings({"AGVM_EDITION": "dev"})
    assert dev.edition == "dev"
    assert dev.compat_routes_enabled is True
    assert dev.dev_routes_enabled is True
    assert dev.platform_routes_enabled is True
    assert all(dev.module_owner_enabled.values())


def test_ocm5_runtime_decisions_match_classification_contract() -> None:
    settings = _settings("core")

    assert route_decision("/runtime/edition", settings).allowed is True
    assert route_decision("/mcp/retrieve-context", settings).allowed is True
    assert route_decision("/mcp/grow-preview", settings).allowed is False
    assert route_decision("/memory/sleep", settings).owner == "agvm_maintain_studio"
    assert route_decision("/hosted/tenants", settings).category == "platform_only"
    assert route_decision("/dev/audit", settings).category == "dev_only"
    assert route_decision("/preview", settings).category == "compat"


def test_ocm5_main_installs_edition_gate_after_route_declarations() -> None:
    source = (API_DIR / "main.py").read_text(encoding="utf-8")
    install_index = source.rfind("install_edition_route_gate(app)")
    runtime_router_index = source.find("app.include_router(\n    create_core_runtime_router")
    dev_route_index = source.rfind('@app.get("/dev/guide-compliance"')

    assert "from edition_gate import install_edition_route_gate" in source
    assert "from core_runtime_router import create_core_runtime_router" in source
    assert runtime_router_index > 0
    assert install_index > dev_route_index
    assert install_index > runtime_router_index


def test_ocm5_core_edition_filters_real_main_app_in_fresh_process() -> None:
    env = {
        **os.environ,
        "AGVM_ALLOW_SETUP_WITHOUT_PROVIDER": "true",
        "AGVM_EDITION": "core",
        "PYTHONPATH": f"{API_DIR}{os.pathsep}{ROOT / 'apps' / 'agvm_clone_app' / 'backend'}",
    }
    script = """
import json
import main

paths = {str(getattr(route, "path", "") or "") for route in main.app.router.routes}
print(json.dumps({
    "edition": main.app.state.agvm_route_gate_report["settings"]["edition"],
    "removed_route_count": main.app.state.agvm_route_gate_report["removed_route_count"],
    "health": "/health" in paths,
    "runtime_edition": "/runtime/edition" in paths,
    "retrieve": "/mcp/retrieve-context" in paths,
    "grow": "/mcp/grow-preview" in paths,
    "dev": "/dev/audit" in paths,
    "hosted": "/hosted/tenants" in paths,
    "compat": "/preview" in paths,
}, sort_keys=True))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        env=env,
        text=True,
        timeout=60,
    )
    payload = json.loads(completed.stdout)

    assert payload["edition"] == "core"
    assert payload["removed_route_count"] >= 1
    assert payload["health"] is True
    assert payload["runtime_edition"] is True
    assert payload["retrieve"] is True
    assert payload["grow"] is False
    assert payload["dev"] is False
    assert payload["hosted"] is False
    assert payload["compat"] is False


def _sample_app() -> FastAPI:
    app = FastAPI()

    for path in [
        "/health",
        "/runtime/edition",
        "/mcp/retrieve-context",
        "/mcp/write-memory-preview",
        "/mcp/grow-preview",
        "/mcp/sleep-preview",
        "/clone-app/manifest",
        "/agent-demo/chat-turn",
        "/dev/audit",
        "/hosted/tenants",
        "/preview",
        "/unclassified-experiment",
    ]:
        _add_get(app, path)
    return app


def _add_get(app: FastAPI, path: str) -> None:
    async def endpoint() -> dict[str, str]:
        return {"path": path}

    app.add_api_route(path, endpoint, methods=["GET"], name=path.strip("/").replace("/", "_") or "root")


def _paths(app: FastAPI) -> set[str]:
    return {str(getattr(route, "path", "") or "") for route in app.router.routes}


def _settings(
    edition: str,
    *,
    compat: bool = False,
    modules: dict[str, bool] | None = None,
    platform: bool = False,
) -> EditionSettings:
    return EditionSettings(
        edition=edition,
        compat_routes_enabled=compat,
        dev_routes_enabled=edition == "dev",
        platform_routes_enabled=platform or edition == "dev",
        module_owner_enabled=modules
        or {
            "agvm_agent_chat": edition == "dev",
            "agvm_clone_app": edition == "dev",
            "agvm_grow_studio": edition == "dev",
            "agvm_maintain_studio": edition == "dev",
        },
    )
