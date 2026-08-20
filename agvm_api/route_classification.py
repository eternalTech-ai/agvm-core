from __future__ import annotations

import argparse
import ast
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence

from mcp_tool_registration import GROW_MODULE_ID, MAINTAIN_MODULE_ID, required_module_id_for_tool_name


SURFACE_CATEGORIES = (
    "core",
    "paid_module",
    "platform_only",
    "dev_only",
    "deprecated",
    "compat",
)

FASTAPI_METHODS = ("get", "post", "put", "patch", "delete")


@dataclass(frozen=True)
class SurfaceClassification:
    category: str
    owner: str
    public_core_allowed: bool
    rationale: str

    def __post_init__(self) -> None:
        if self.category not in SURFACE_CATEGORIES:
            raise ValueError(f"unsupported_surface_category:{self.category}")


@dataclass(frozen=True)
class DiscoveredRoute:
    method: str
    path: str
    source: str
    line: int
    function_name: str

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}"


@dataclass(frozen=True)
class ClassifiedRoute:
    route: DiscoveredRoute
    classification: SurfaceClassification


@dataclass(frozen=True)
class ClassifiedSurface:
    kind: str
    name: str
    classification: SurfaceClassification


@dataclass(frozen=True)
class RouteRule:
    name: str
    category: str
    owner: str
    public_core_allowed: bool
    rationale: str
    prefixes: tuple[str, ...] = ()
    exact: tuple[str, ...] = ()
    source_contains: tuple[str, ...] = ()

    def matches(self, route: DiscoveredRoute) -> bool:
        source = _normalize_path_text(route.source)
        path = route.path
        if self.source_contains and any(fragment in source for fragment in self.source_contains):
            return True
        if path in self.exact:
            return True
        return any(path.startswith(prefix) for prefix in self.prefixes)

    def classification(self) -> SurfaceClassification:
        return SurfaceClassification(
            category=self.category,
            owner=self.owner,
            public_core_allowed=self.public_core_allowed,
            rationale=self.rationale,
        )


ROUTE_RULES: tuple[RouteRule, ...] = (
    RouteRule(
        name="dev_routes",
        category="dev_only",
        owner="internal_dev_certification",
        public_core_allowed=False,
        rationale="Developer, seed, reset and certification endpoints must not ship in public core.",
        prefixes=("/dev/",),
    ),
    RouteRule(
        name="hosted_platform_scope",
        category="platform_only",
        owner="agvm_platform",
        public_core_allowed=False,
        rationale="Hosted tenant and hosted brain resolution belong to the cloud control plane.",
        prefixes=("/hosted/", "/mcp/hosted/"),
    ),
    RouteRule(
        name="standalone_clone_app_router",
        category="paid_module",
        owner="agvm_clone_app",
        public_core_allowed=False,
        rationale="Clone App is the first paid module boundary and must remain outside public core.",
        source_contains=("/apps/agvm_clone_app/backend/agvm_clone_app/api/",),
    ),
    RouteRule(
        name="clone_app_mount",
        category="paid_module",
        owner="agvm_clone_app",
        public_core_allowed=False,
        rationale="Mounted Clone App HTTP surface is paid-module product code.",
        prefixes=("/clone-app/",),
    ),
    RouteRule(
        name="agent_chat_product",
        category="paid_module",
        owner="agvm_agent_chat",
        public_core_allowed=False,
        rationale="Agent/chat product UX is not required for open-core retrieval proof.",
        exact=("/agent-demo/chat-turn", "/mcp/agent-chat-turn"),
    ),
    RouteRule(
        name="mcp_core_grow_sleep_evolve",
        category="core",
        owner="agvm_core_mcp",
        public_core_allowed=True,
        rationale="Raw local MCP Grow/Sleep/Evolve operations remain public core when they enforce preview and explicit apply policy.",
        exact=(
            "/memory/mcp/grow-source-preview",
            "/mcp/grow-source-preview",
            "/memory/mcp/grow-preview",
            "/mcp/grow-preview",
            "/memory/mcp/grow-guided",
            "/mcp/grow-guided",
            "/memory/mcp/grow-source-apply",
            "/mcp/grow-source-apply",
            "/memory/mcp/grow-apply",
            "/mcp/grow-apply",
            "/memory/mcp/grow-source-status",
            "/mcp/grow-source-status",
            "/memory/mcp/grow-status",
            "/mcp/grow-status",
            "/memory/mcp/sleep-preview",
            "/mcp/sleep-preview",
            "/memory/mcp/evolve-preview",
            "/mcp/evolve-preview",
            "/memory/mcp/sleep-apply",
            "/mcp/sleep-apply",
            "/memory/mcp/evolve-apply",
            "/mcp/evolve-apply",
        ),
    ),
    RouteRule(
        name="grow_source_product",
        category="paid_module",
        owner="agvm_grow_studio",
        public_core_allowed=False,
        rationale="Source investigation and Grow Studio orchestration are Pro module surfaces.",
        prefixes=(
            "/grow-studio/",
            "/source-investigation/",
            "/memory/source-investigation/",
            "/mcp/grow-",
            "/memory/mcp/grow-",
        ),
    ),
    RouteRule(
        name="maintain_product",
        category="paid_module",
        owner="agvm_maintain_studio",
        public_core_allowed=False,
        rationale="Sleep, Evolve, Matrix and maintenance orchestration are Pro module surfaces.",
        prefixes=(
            "/maintain-studio/",
            "/mcp/matrix-calibration-",
            "/memory/mcp/matrix-calibration-",
            "/mcp/sleep-",
            "/memory/mcp/sleep-",
            "/mcp/evolve-",
            "/memory/mcp/evolve-",
            "/mcp/list-",
            "/memory/mcp/list-",
        ),
        exact=(
            "/restructure-local-area",
            "/memory/repair-nearby",
            "/memory/sleep-evolve",
            "/memory/sleep",
            "/memory/evolve",
            "/memory/rebuild-region-summaries",
            "/memory/correct-after-query",
        ),
    ),
    RouteRule(
        name="setup_and_health",
        category="core",
        owner="agvm_core_runtime",
        public_core_allowed=True,
        rationale="Local setup and process health are required for self-hosted core.",
        prefixes=("/setup/",),
        exact=("/health", "/runtime/edition"),
    ),
    RouteRule(
        name="brain_registry",
        category="core",
        owner="agvm_core_brain_registry",
        public_core_allowed=True,
        rationale="Local brain registry, selection, export/import and scoped MCP brain resolution are core primitives.",
        prefixes=("/memory/brains", "/mcp/brains"),
        exact=("/mcp/select-brain",),
    ),
    RouteRule(
        name="graph_and_atlas",
        category="core",
        owner="agvm_core_graph_viewer",
        public_core_allowed=True,
        rationale="Base graph, atlas and bounded local inspection are part of the public memory viewer.",
        prefixes=("/cluster/", "/memory/inspect-nearby/", "/memory/get-region-summary/"),
        exact=(
            "/graph",
            "/graph-view",
            "/atlas",
            "/memory/get-atlas",
            "/rebuild-atlas",
            "/memory/rebuild-atlas",
            "/memory/geometry-calibration",
        ),
    ),
    RouteRule(
        name="mcp_contract_and_usage",
        category="core",
        owner="agvm_core_mcp",
        public_core_allowed=True,
        rationale="MCP contract registry and usage guide are required for local and hosted clients.",
        exact=(
            "/memory/mcp/contracts",
            "/mcp/contracts",
            "/memory/mcp/tools",
            "/mcp/usage-guide",
        ),
    ),
    RouteRule(
        name="local_module_license",
        category="core",
        owner="agvm_core_license",
        public_core_allowed=True,
        rationale="Local license lease status and entitlement checks are core activation primitives; paid runtime code remains in modules.",
        prefixes=("/modules/local-license",),
    ),
    RouteRule(
        name="local_module_install_supervisor",
        category="paid_module",
        owner="agvm_platform_local_supervisor",
        public_core_allowed=False,
        rationale="Local module install handoff receives paid account plans and prepares private sidecar runtime state outside public core.",
        prefixes=("/modules/local-install",),
    ),
    RouteRule(
        name="mcp_retrieve_and_inspect",
        category="core",
        owner="agvm_core_mcp",
        public_core_allowed=True,
        rationale="Retrieve and inspect tools are the public open-core MCP surface.",
        prefixes=(
            "/memory/mcp/retrieve-",
            "/mcp/retrieve-",
            "/memory/mcp/inspect-",
            "/mcp/inspect-",
        ),
    ),
    RouteRule(
        name="mcp_memory_write_primitive",
        category="core",
        owner="agvm_core_mcp",
        public_core_allowed=True,
        rationale="Raw MCP preview/commit primitives may remain core when explicit-apply policy is enforced.",
        exact=(
            "/memory/mcp/write-memory-preview",
            "/mcp/write-memory-preview",
            "/memory/mcp/write-memory-commit",
            "/mcp/write-memory-commit",
            "/memory/mcp/ask-memory-clarification",
            "/mcp/ask-memory-clarification",
        ),
    ),
    RouteRule(
        name="brain_health",
        category="core",
        owner="agvm_core_health",
        public_core_allowed=True,
        rationale="Brain health and validation are core proof and safety surfaces.",
        exact=(
            "/memory/brain-health",
            "/memory/large-brain-validation",
            "/memory/mcp/brain-health",
            "/mcp/brain-health",
        ),
    ),
    RouteRule(
        name="retrieve_engine",
        category="core",
        owner="agvm_core_retrieve",
        public_core_allowed=True,
        rationale="Retrieve, query plans, run ledgers, trace and stream APIs are core memory-engine surfaces.",
        prefixes=(
            "/memory/query-",
            "/memory/query-result/",
            "/memory/run-ledger",
            "/memory/runtime-retention",
            "/memory/get-trace/",
            "/memory/query-stream/",
        ),
        exact=("/retrieve", "/memory/query", "/memory/preview"),
    ),
    RouteRule(
        name="legacy_core_compat",
        category="compat",
        owner="agvm_core_compat",
        public_core_allowed=False,
        rationale="Legacy monolith endpoints may be needed locally now but need replacement by explicit core/MCP APIs before public extraction.",
        exact=(
            "/preview",
            "/persist-selection",
            "/analyze",
            "/nodes",
            "/rebuild",
            "/memory/save-selection",
            "/memory/bootstrap",
        ),
    ),
)


UI_MODE_CLASSIFICATIONS: dict[str, SurfaceClassification] = {
    "brain": SurfaceClassification("core", "agvm_core_graph_viewer", True, "Base 3D brain viewer stays in public core."),
    "retrieve": SurfaceClassification("core", "agvm_core_retrieve", True, "Context retrieval is the primary open-core workflow."),
    "payload": SurfaceClassification("core", "agvm_core_retrieve", True, "Results/proof view is part of retrieve proof."),
    "paths": SurfaceClassification("core", "agvm_core_retrieve", True, "Legacy result alias for path proof."),
    "documents": SurfaceClassification("core", "agvm_core_retrieve", True, "Legacy result alias for document proof."),
    "health": SurfaceClassification("core", "agvm_core_health", True, "Brain health is a public proof/safety surface."),
    "benchmarks": SurfaceClassification("core", "agvm_core_bench", True, "Base benchmark/proof surface remains core."),
    "mcp_setup": SurfaceClassification("core", "agvm_core_mcp", True, "MCP setup is required for local open-core adoption."),
    "mcp_raw_console": SurfaceClassification("core", "agvm_core_mcp_raw_console", True, "Raw MCP contract console lets public-core users call core MCP tools without paid module UI."),
    "settings": SurfaceClassification("core", "agvm_core_settings", True, "Minimal local settings stay in core."),
    "clone_app": SurfaceClassification("paid_module", "agvm_clone_app", False, "Clone App is a paid module."),
    "chat": SurfaceClassification("paid_module", "agvm_agent_chat", False, "Non-core assistant chat should become an optional module."),
    "grow": SurfaceClassification("paid_module", "agvm_grow_studio", False, "Grow Studio rich source workflow is Pro."),
    "evolve": SurfaceClassification("paid_module", "agvm_maintain_studio", False, "Maintain/Sleep/Evolve rich workflow is Pro."),
}


DOCKER_SERVICE_CLASSIFICATIONS: dict[str, SurfaceClassification] = {
    "agvm_api": SurfaceClassification(
        "compat",
        "agvm_core_runtime",
        False,
        "Current API image is a transitional monolith; Slice 5/6 must split core and paid routers/images.",
    ),
    "agvm_ui": SurfaceClassification(
        "compat",
        "agvm_core_ui_host",
        False,
        "Current UI image is a transitional cockpit that still imports paid module UI directly.",
    ),
    "agvm_mcp": SurfaceClassification(
        "core",
        "agvm_core_mcp",
        True,
        "Local stdio MCP bridge is part of the public core distribution.",
    ),
    "agvm_core_api": SurfaceClassification(
        "core",
        "agvm_core_runtime",
        True,
        "Public core API image starts the extracted core FastAPI app and does not copy paid module packages.",
    ),
    "agvm_core_ui": SurfaceClassification(
        "core",
        "agvm_core_ui_host",
        True,
        "Public core UI image builds the cockpit host with the public_core shell profile.",
    ),
    "agvm_clone_app_api": SurfaceClassification(
        "paid_module",
        "agvm_clone_app",
        False,
        "Clone App API is a Pro paid-module sidecar and must not ship in the public core image.",
    ),
    "agvm_clone_app_ui": SurfaceClassification(
        "paid_module",
        "agvm_clone_app",
        False,
        "Clone App UI bundle is a Pro paid-module sidecar and must not ship in the public core UI image.",
    ),
    "agvm_grow_studio_api": SurfaceClassification(
        "paid_module",
        "agvm_grow_studio",
        False,
        "Grow Studio API is a Pro paid-module sidecar and must not ship in the public core image.",
    ),
    "agvm_maintain_studio_api": SurfaceClassification(
        "paid_module",
        "agvm_maintain_studio",
        False,
        "Maintain Studio API is a Pro paid-module sidecar and must not ship in the public core image.",
    ),
}


def discover_fastapi_routes(source_paths: Sequence[Path]) -> list[DiscoveredRoute]:
    routes: list[DiscoveredRoute] = []
    for source_path in source_paths:
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                method = _fastapi_method(decorator)
                if not method:
                    continue
                path = _decorator_first_string_arg(decorator)
                if not path:
                    continue
                routes.append(
                    DiscoveredRoute(
                        method=method.upper(),
                        path=path,
                        source=_normalize_path_text(str(source_path)),
                        line=getattr(decorator, "lineno", node.lineno),
                        function_name=node.name,
                    )
                )
    return sorted(routes, key=lambda item: (item.source, item.path, item.method, item.line))


def classify_route(route: DiscoveredRoute) -> SurfaceClassification | None:
    for rule in ROUTE_RULES:
        if rule.matches(route):
            return rule.classification()
    return None


def classify_routes(routes: Iterable[DiscoveredRoute]) -> list[ClassifiedRoute]:
    classified: list[ClassifiedRoute] = []
    missing: list[DiscoveredRoute] = []
    for route in routes:
        classification = classify_route(route)
        if classification is None:
            missing.append(route)
            continue
        classified.append(ClassifiedRoute(route=route, classification=classification))
    if missing:
        formatted = ", ".join(f"{route.key} ({route.source}:{route.line})" for route in missing)
        raise AssertionError(f"unclassified_routes:{formatted}")
    return classified


def classify_mcp_tool(tool_name: str) -> SurfaceClassification | None:
    clean = str(tool_name or "").strip()
    if clean in {"get_agvm_usage_guide", "list_brains", "active_brain", "create_brain", "select_brain", "ensure_brain"}:
        return SurfaceClassification("core", "agvm_core_mcp", True, "MCP guide and brain registry tools are core.")
    if clean.startswith("retrieve_") or clean.startswith("inspect_"):
        return SurfaceClassification("core", "agvm_core_mcp", True, "MCP retrieval and inspection tools are core.")
    if clean in {"write_memory_preview", "write_memory_commit", "ask_memory_clarification", "brain_health"}:
        return SurfaceClassification("core", "agvm_core_mcp", True, "Raw memory write/health primitives stay core with explicit permission policy.")
    required_module_id = required_module_id_for_tool_name(clean)
    if required_module_id == GROW_MODULE_ID:
        return SurfaceClassification("paid_module", "agvm_grow_studio", False, "Grow orchestration tools belong to the Pro Grow module.")
    if required_module_id == MAINTAIN_MODULE_ID:
        return SurfaceClassification("paid_module", "agvm_maintain_studio", False, "Maintenance and Matrix tools belong to the Pro Maintain module.")
    return None


def classify_ui_mode(mode: str) -> SurfaceClassification | None:
    return UI_MODE_CLASSIFICATIONS.get(str(mode or "").strip())


def classify_docker_service(service_name: str) -> SurfaceClassification | None:
    return DOCKER_SERVICE_CLASSIFICATIONS.get(str(service_name or "").strip())


def discover_cockpit_modes(mode_rail_path: Path) -> list[str]:
    text = mode_rail_path.read_text(encoding="utf-8")
    match = re.search(r"export\s+type\s+CockpitModeKey\s*=(?P<body>.*?);", text, flags=re.S)
    if not match:
        return []
    return sorted(dict.fromkeys(re.findall(r'"([^"]+)"', match.group("body"))))


def discover_ts_classified_modes(classification_path: Path) -> list[str]:
    text = classification_path.read_text(encoding="utf-8")
    return sorted(dict.fromkeys(re.findall(r'mode:\s*"([^"]+)"', text)))


def discover_docker_compose_services(compose_path: Path) -> list[str]:
    services: list[str] = []
    in_services = False
    for raw_line in compose_path.read_text(encoding="utf-8").splitlines():
        if raw_line.strip() == "services:":
            in_services = True
            continue
        if not in_services:
            continue
        if raw_line and not raw_line.startswith(" "):
            break
        match = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", raw_line)
        if match:
            services.append(match.group(1))
    return services


def classification_summary(classifications: Iterable[SurfaceClassification]) -> dict[str, int]:
    return dict(sorted(Counter(item.category for item in classifications).items()))


def build_audit_payload(root: Path) -> dict[str, object]:
    route_sources = [
        root / "agvm_api" / "main.py",
        root / "agvm_api" / "core_runtime_router.py",
        root / "agvm_api" / "core_brain_router.py",
        root / "agvm_api" / "core_mcp_contract_router.py",
        root / "agvm_api" / "core_license_router.py",
        root / "apps" / "agvm_clone_app" / "backend" / "agvm_clone_app" / "api" / "chat.py",
        root / "apps" / "agvm_clone_app" / "backend" / "agvm_clone_app" / "api" / "teach.py",
        root / "apps" / "agvm_clone_app" / "backend" / "agvm_clone_app" / "api" / "module_manifest.py",
    ]
    routes = discover_fastapi_routes([path for path in route_sources if path.exists()])
    classified_routes = classify_routes(routes)
    ui_modes = discover_cockpit_modes(root / "agvm_cockpit_prototype" / "src" / "new-ui" / "shell" / "ModeRail.tsx")
    docker_services = discover_docker_compose_services(root / "docker-compose.yml")
    return {
        "schema_version": "agvm.ocm1.classification_audit.v1",
        "route_count": len(routes),
        "route_summary": classification_summary(item.classification for item in classified_routes),
        "ui_modes": ui_modes,
        "ui_summary": classification_summary(UI_MODE_CLASSIFICATIONS[mode] for mode in ui_modes if mode in UI_MODE_CLASSIFICATIONS),
        "docker_services": docker_services,
        "docker_summary": classification_summary(DOCKER_SERVICE_CLASSIFICATIONS[name] for name in docker_services if name in DOCKER_SERVICE_CLASSIFICATIONS),
        "routes": [
            {
                **asdict(item.route),
                "category": item.classification.category,
                "owner": item.classification.owner,
                "public_core_allowed": item.classification.public_core_allowed,
            }
            for item in classified_routes
        ],
    }


def _fastapi_method(decorator: ast.AST) -> str | None:
    if not isinstance(decorator, ast.Call):
        return None
    func = decorator.func
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr not in FASTAPI_METHODS:
        return None
    if not isinstance(func.value, ast.Name):
        return None
    if func.value.id not in {"app", "router"}:
        return None
    return func.attr


def _decorator_first_string_arg(decorator: ast.AST) -> str | None:
    if not isinstance(decorator, ast.Call) or not decorator.args:
        return None
    first = decorator.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def _normalize_path_text(value: str) -> str:
    return value.replace("\\", "/")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit AGVM open-core/module surface classifications.")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]), help="Repository root.")
    parser.add_argument("--json", action="store_true", help="Emit JSON audit payload.")
    args = parser.parse_args(argv)
    payload = build_audit_payload(Path(args.root))
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"routes: {payload['route_count']} {payload['route_summary']}")
        print(f"ui_modes: {len(payload['ui_modes'])} {payload['ui_summary']}")
        print(f"docker_services: {len(payload['docker_services'])} {payload['docker_summary']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
