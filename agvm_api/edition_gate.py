# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from route_classification import DiscoveredRoute, SurfaceClassification, classify_route


AGVM_EDITIONS = ("core", "pro", "cloud", "dev")

FRAMEWORK_ROUTE_PATHS = {
    "/docs",
    "/docs/oauth2-redirect",
    "/openapi.json",
    "/redoc",
}

INTERNAL_SERVICE_ROUTE_PATHS = {
    "/internal/hosted-memory/capabilities",
}
INTERNAL_SERVICE_ROUTE_PREFIXES = (
    "/memory/brains/",
)

MODULE_OWNER_ENV = {
    "agvm_agent_chat": "AGVM_ENABLE_MODULE_AGENT_CHAT",
    "agvm_clone_app": "AGVM_ENABLE_MODULE_CLONE_APP",
    "agvm_grow_studio": "AGVM_ENABLE_MODULE_GROW",
    "agvm_maintain_studio": "AGVM_ENABLE_MODULE_MAINTAIN",
}


@dataclass(frozen=True)
class EditionSettings:
    edition: str
    compat_routes_enabled: bool
    dev_routes_enabled: bool
    platform_routes_enabled: bool
    module_owner_enabled: dict[str, bool]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RuntimeRouteDecision:
    allowed: bool
    category: str
    owner: str
    path: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def read_edition_settings(env: Mapping[str, str] | None = None) -> EditionSettings:
    source = env or os.environ
    edition = normalize_edition(source.get("AGVM_EDITION") or source.get("AGVM_API_EDITION") or "dev")
    module_default = edition == "dev"
    return EditionSettings(
        edition=edition,
        compat_routes_enabled=_flag(source, "AGVM_ENABLE_COMPAT_ROUTES", default=edition in {"dev", "pro"}),
        dev_routes_enabled=edition == "dev" and _flag(source, "AGVM_ENABLE_DEV_ROUTES", default=True),
        platform_routes_enabled=_flag(source, "AGVM_ENABLE_PLATFORM_ROUTES", default=edition in {"cloud", "dev"}),
        module_owner_enabled={
            owner: _flag(source, env_name, default=module_default)
            for owner, env_name in MODULE_OWNER_ENV.items()
        },
    )


def normalize_edition(value: str | None) -> str:
    clean = str(value or "").strip().lower().replace("-", "_")
    if clean in {"open_core", "public_core", "free"}:
        return "core"
    if clean in {"local_pro", "pro_local"}:
        return "pro"
    if clean in {"development", "local_dev", "monolith"}:
        return "dev"
    if clean in AGVM_EDITIONS:
        return clean
    return "dev"


def classify_runtime_route_path(path: str) -> SurfaceClassification | None:
    normalized = str(path or "").strip() or "/"
    return classify_route(
        DiscoveredRoute(
            method="GET",
            path=normalized,
            source="runtime",
            line=0,
            function_name="runtime_route",
        )
    )


def route_decision(path: str, settings: EditionSettings) -> RuntimeRouteDecision:
    normalized = str(path or "").strip() or "/"
    if settings.edition == "cloud" and normalized.startswith(
        ("/mcp/brain-bootstrap-", "/memory/mcp/brain-bootstrap-")
    ):
        return RuntimeRouteDecision(
            False,
            "core",
            "agvm_core",
            normalized,
            "Cloud Brain Bootstrap requires the signed tenant memory gateway",
        )
    if normalized in FRAMEWORK_ROUTE_PATHS:
        return RuntimeRouteDecision(True, "framework", "fastapi", normalized, "framework route")
    if normalized in INTERNAL_SERVICE_ROUTE_PATHS:
        return RuntimeRouteDecision(
            settings.edition in {"cloud", "pro", "dev"},
            "internal_service",
            "hosted_mcp",
            normalized,
            "signed internal service route"
            if settings.edition in {"cloud", "pro", "dev"}
            else "internal service route excluded from public core",
        )
    if any(normalized.startswith(prefix) for prefix in INTERNAL_SERVICE_ROUTE_PREFIXES):
        return RuntimeRouteDecision(
            settings.edition in {"cloud", "pro", "dev"},
            "internal_service",
            "hosted_mcp",
            normalized,
            "signed internal service route"
            if settings.edition in {"cloud", "pro", "dev"}
            else "internal service route excluded from public core",
        )

    classification = classify_runtime_route_path(normalized)
    if classification is None:
        return RuntimeRouteDecision(
            allowed=settings.edition == "dev",
            category="unclassified",
            owner="unknown",
            path=normalized,
            reason="unclassified routes are available only in dev edition",
        )

    category = classification.category
    owner = classification.owner
    if settings.edition == "dev":
        if category == "dev_only" and not settings.dev_routes_enabled:
            return RuntimeRouteDecision(False, category, owner, normalized, "dev routes disabled")
        return RuntimeRouteDecision(True, category, owner, normalized, "dev edition keeps transitional monolith behavior")

    if classification.public_core_allowed:
        return RuntimeRouteDecision(True, category, owner, normalized, "public core route")

    if category == "paid_module":
        enabled = settings.module_owner_enabled.get(owner, False)
        return RuntimeRouteDecision(enabled, category, owner, normalized, _module_reason(owner, enabled))

    if category == "platform_only":
        return RuntimeRouteDecision(
            settings.platform_routes_enabled,
            category,
            owner,
            normalized,
            "platform routes enabled" if settings.platform_routes_enabled else "platform route excluded from this edition",
        )

    if category == "compat":
        return RuntimeRouteDecision(
            settings.compat_routes_enabled,
            category,
            owner,
            normalized,
            "compat routes enabled" if settings.compat_routes_enabled else "compat route excluded from public core",
        )

    if category == "dev_only":
        return RuntimeRouteDecision(False, category, owner, normalized, "dev route excluded outside dev edition")

    return RuntimeRouteDecision(False, category, owner, normalized, "route category excluded")


def install_edition_route_gate(app: Any, settings: EditionSettings | None = None) -> dict[str, Any]:
    resolved = settings or read_edition_settings()
    kept_routes = []
    removed_routes = []
    for route in list(app.router.routes):
        path = str(getattr(route, "path", "") or "")
        decision = route_decision(path, resolved)
        record = {
            **decision.as_dict(),
            "methods": sorted(str(method) for method in (getattr(route, "methods", None) or [])),
            "name": str(getattr(route, "name", "") or ""),
        }
        if decision.allowed:
            kept_routes.append(route)
        else:
            removed_routes.append(record)

    app.router.routes[:] = kept_routes
    if hasattr(app, "openapi_schema"):
        app.openapi_schema = None

    report = {
        "schema_version": "agvm.runtime_edition_gate.v1",
        "settings": resolved.as_dict(),
        "route_count_before": len(kept_routes) + len(removed_routes),
        "route_count_after": len(kept_routes),
        "removed_route_count": len(removed_routes),
        "removed_routes": sorted(removed_routes, key=lambda item: (item["path"], ",".join(item["methods"]))),
    }
    app.state.agvm_edition_settings = resolved
    app.state.agvm_route_gate_report = report
    return report


def build_edition_route_report(app: Any) -> dict[str, Any]:
    report = getattr(app.state, "agvm_route_gate_report", None)
    if isinstance(report, dict):
        return report
    return {
        "schema_version": "agvm.runtime_edition_gate.v1",
        "settings": read_edition_settings().as_dict(),
        "route_count_before": len(getattr(app.router, "routes", []) or []),
        "route_count_after": len(getattr(app.router, "routes", []) or []),
        "removed_route_count": 0,
        "removed_routes": [],
    }


def _flag(source: Mapping[str, str], name: str, *, default: bool) -> bool:
    value = source.get(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _module_reason(owner: str, enabled: bool) -> str:
    env_name = MODULE_OWNER_ENV.get(owner, "AGVM_ENABLE_MODULE_<OWNER>")
    if enabled:
        return f"paid module owner enabled by {env_name}"
    return f"paid module owner disabled; set {env_name}=true outside core/dev only when licensed"
