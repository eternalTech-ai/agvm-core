from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import APP_NAME, APP_VERSION
from core_brain_router import create_core_brain_router
from core_graph_router import create_core_graph_router
from core_license_router import create_core_license_router
from core_mcp_contract_router import create_core_mcp_contract_router
from core_mcp_matrix_router import create_core_mcp_matrix_router
from core_mcp_ops_router import create_core_mcp_ops_router
from core_retrieve_router import create_core_retrieve_router
from core_runtime_router import create_core_runtime_router
from edition_gate import install_edition_route_gate, read_edition_settings
try:
    from hosted_mcp_core_service_router import create_hosted_mcp_core_service_router
except ImportError:  # pragma: no cover - public Core export omits hosted internals.
    create_hosted_mcp_core_service_router = None


def create_core_app() -> FastAPI:
    app = FastAPI(title=f"{APP_NAME} Core", version=APP_VERSION)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(
        create_core_runtime_router(
            app_name=APP_NAME,
            app_version=APP_VERSION,
            hosted_registry_summary_provider=None,
        )
    )
    app.include_router(create_core_brain_router())
    app.include_router(create_core_graph_router())
    app.include_router(create_core_mcp_contract_router())
    app.include_router(create_core_mcp_ops_router())
    app.include_router(create_core_mcp_matrix_router())
    app.include_router(create_core_retrieve_router())
    app.include_router(create_core_license_router())
    if create_hosted_mcp_core_service_router is not None:
        app.include_router(create_hosted_mcp_core_service_router())
    install_edition_route_gate(app, read_edition_settings())
    return app


app = create_core_app()
