from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "agvm_api"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

from mcp_contracts import AGENT_MEMORY_MCP_TOOL_NAMES, GUIDE_MCP_TOOL_NAMES, REQUIRED_MCP_TOOL_NAMES  # noqa: E402
from route_classification import (  # noqa: E402
    DOCKER_SERVICE_CLASSIFICATIONS,
    UI_MODE_CLASSIFICATIONS,
    classify_docker_service,
    classify_mcp_tool,
    classify_route,
    classify_routes,
    discover_cockpit_modes,
    discover_docker_compose_services,
    discover_fastapi_routes,
    discover_ts_classified_modes,
)


ROUTE_SOURCES = [
    ROOT / "agvm_api" / "main.py",
    ROOT / "agvm_api" / "core_runtime_router.py",
    ROOT / "agvm_api" / "core_brain_router.py",
    ROOT / "agvm_api" / "core_mcp_contract_router.py",
    ROOT / "apps" / "agvm_clone_app" / "backend" / "agvm_clone_app" / "api" / "chat.py",
    ROOT / "apps" / "agvm_clone_app" / "backend" / "agvm_clone_app" / "api" / "teach.py",
    ROOT / "apps" / "agvm_clone_app" / "backend" / "agvm_clone_app" / "api" / "module_manifest.py",
]


def test_ocm1_every_discovered_backend_route_is_classified() -> None:
    routes = discover_fastapi_routes([path for path in ROUTE_SOURCES if path.exists()])

    assert len(routes) >= 100
    classified = classify_routes(routes)

    assert len(classified) == len(routes)
    assert {item.classification.category for item in classified} >= {"core", "paid_module", "platform_only", "dev_only", "compat"}
    assert all(classify_route(route) is not None for route in routes)


def test_ocm1_paid_product_routes_are_not_public_core_allowed() -> None:
    routes = discover_fastapi_routes([path for path in ROUTE_SOURCES if path.exists()])
    classified = classify_routes(routes)
    paid = [
        item
        for item in classified
        if item.route.path.startswith("/clone-app/")
        or item.route.path.startswith("/mcp/sleep-")
        or item.route.path.startswith("/mcp/evolve-")
        or "apps/agvm_clone_app" in item.route.source
    ]

    assert paid
    assert {item.classification.category for item in paid} == {"paid_module"}
    assert all(not item.classification.public_core_allowed for item in paid)


def test_ocm1_ui_modes_are_classified_in_python_and_typescript() -> None:
    mode_rail = ROOT / "agvm_cockpit_prototype" / "src" / "new-ui" / "shell" / "ModeRail.tsx"
    ts_classification = ROOT / "agvm_cockpit_prototype" / "src" / "new-ui" / "modules" / "coreModeClassification.ts"

    modes = discover_cockpit_modes(mode_rail)
    ts_modes = discover_ts_classified_modes(ts_classification)

    assert modes
    assert set(modes) == set(UI_MODE_CLASSIFICATIONS)
    assert set(modes) == set(ts_modes)
    assert UI_MODE_CLASSIFICATIONS["clone_app"].category == "paid_module"
    assert UI_MODE_CLASSIFICATIONS["platform"].category == "platform_only"
    assert UI_MODE_CLASSIFICATIONS["grow"].category == "core"
    assert UI_MODE_CLASSIFICATIONS["grow"].public_core_allowed is True
    assert UI_MODE_CLASSIFICATIONS["evolve"].category == "paid_module"
    assert UI_MODE_CLASSIFICATIONS["retrieve"].public_core_allowed is True


def test_ocm1_mcp_tools_are_classified() -> None:
    tool_names = [*GUIDE_MCP_TOOL_NAMES, *REQUIRED_MCP_TOOL_NAMES, *AGENT_MEMORY_MCP_TOOL_NAMES]
    missing = [name for name in tool_names if classify_mcp_tool(name) is None]

    assert not missing
    assert classify_mcp_tool("retrieve_context").category == "core"  # type: ignore[union-attr]
    assert classify_mcp_tool("write_memory_commit").category == "core"  # type: ignore[union-attr]
    assert classify_mcp_tool("grow_apply").category == "core"  # type: ignore[union-attr]
    assert classify_mcp_tool("sleep_apply").category == "paid_module"  # type: ignore[union-attr]


def test_ocm1_docker_services_are_classified() -> None:
    services = discover_docker_compose_services(ROOT / "docker-compose.yml")

    assert services
    assert set(services) <= set(DOCKER_SERVICE_CLASSIFICATIONS)
    assert all(classify_docker_service(service) is not None for service in services)
    assert DOCKER_SERVICE_CLASSIFICATIONS["agvm_api"].category == "compat"
    assert DOCKER_SERVICE_CLASSIFICATIONS["agvm_ui"].category == "compat"
    assert DOCKER_SERVICE_CLASSIFICATIONS["agvm_mcp"].category == "core"
    assert DOCKER_SERVICE_CLASSIFICATIONS["agvm_core_api"].category == "core"
    assert DOCKER_SERVICE_CLASSIFICATIONS["agvm_clone_app_api"].category == "paid_module"
