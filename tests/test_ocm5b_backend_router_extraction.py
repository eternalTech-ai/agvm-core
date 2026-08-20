from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "agvm_api"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

from route_classification import classify_routes, discover_fastapi_routes  # noqa: E402


CORE_ROUTER_SOURCES = [
    API_DIR / "core_runtime_router.py",
    API_DIR / "core_brain_router.py",
    API_DIR / "core_mcp_contract_router.py",
    API_DIR / "core_retrieve_router.py",
]


def test_ocm5b_core_router_sources_own_extracted_routes() -> None:
    routes = discover_fastapi_routes(CORE_ROUTER_SOURCES)
    paths = {route.path for route in routes}

    assert {"/setup/env", "/health", "/runtime/edition"} <= paths
    assert {"/memory/brains", "/mcp/brains", "/mcp/select-brain"} <= paths
    assert {"/memory/mcp/contracts", "/mcp/contracts", "/memory/mcp/tools", "/mcp/usage-guide"} <= paths

    classified = classify_routes(routes)
    assert {item.classification.category for item in classified} == {"core"}


def test_ocm5b_main_no_longer_declares_extracted_core_routes_directly() -> None:
    routes = discover_fastapi_routes([API_DIR / "main.py"])
    extracted_paths = {
        "/setup/env",
        "/health",
        "/runtime/edition",
        "/memory/brains",
        "/mcp/brains",
        "/mcp/select-brain",
        "/memory/mcp/contracts",
        "/mcp/contracts",
        "/memory/mcp/tools",
        "/mcp/usage-guide",
    }

    assert not (extracted_paths & {route.path for route in routes})


def test_ocm5b_main_mounts_core_routers_before_gate() -> None:
    source = (API_DIR / "main.py").read_text(encoding="utf-8")
    runtime_mount = source.find("create_core_runtime_router")
    brain_mount = source.find("create_core_brain_router")
    mcp_mount = source.find("create_core_mcp_contract_router")
    gate = source.rfind("install_edition_route_gate(app)")

    assert runtime_mount > 0
    assert brain_mount > 0
    assert mcp_mount > 0
    assert gate > max(runtime_mount, brain_mount, mcp_mount)
