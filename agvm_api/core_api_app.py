from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import APP_NAME, APP_VERSION
from core_brain_router import create_core_brain_router
from core_mcp_contract_router import create_core_mcp_contract_router
from core_mcp_operations_router import create_core_mcp_operations_router
from core_retrieve_router import create_core_retrieve_router
from core_runtime_router import create_core_runtime_router
from edition_gate import EditionSettings, install_edition_route_gate


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
    app.include_router(create_core_mcp_contract_router())
    app.include_router(create_core_retrieve_router())
    app.include_router(create_core_mcp_operations_router())
    install_edition_route_gate(
        app,
        EditionSettings(
            edition="core",
            compat_routes_enabled=False,
            dev_routes_enabled=False,
            platform_routes_enabled=False,
            module_owner_enabled={},
        ),
    )
    return app


app = create_core_app()
