# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

from fastapi import FastAPI

from brain_bootstrap_v1 import create_brain_bootstrap_v1_router
from brain_sync_restore_materializer import create_brain_sync_restore_router
from config import APP_NAME, APP_VERSION
from core_browser_security import install_core_browser_security
from core_brain_router import create_core_brain_router
from core_graph_router import create_core_graph_router
from core_license_router import create_core_license_router
from core_mcp_contract_router import create_core_mcp_contract_router
from core_mcp_ops_router import create_core_mcp_ops_router
from core_maintenance_runtime import (
    CoreMaintenanceCloudHandoffRuntime,
    create_core_maintenance_cloud_handoff_router,
)
from core_retrieve_router import create_core_retrieve_router
from core_search_composition_router import create_core_search_composition_router
from core_runtime_router import create_core_runtime_router
from edition_gate import install_edition_route_gate, read_edition_settings


def create_core_app() -> FastAPI:
    app = FastAPI(title=f"{APP_NAME} Core", version=APP_VERSION)
    install_core_browser_security(app)
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
    app.include_router(create_core_mcp_ops_router(maintenance_runtime=CoreMaintenanceCloudHandoffRuntime()))
    app.include_router(create_core_maintenance_cloud_handoff_router())
    app.include_router(create_brain_bootstrap_v1_router())
    app.include_router(create_core_retrieve_router())
    app.include_router(create_core_search_composition_router())
    app.include_router(create_core_license_router())
    app.include_router(create_brain_sync_restore_router())
    install_edition_route_gate(app, read_edition_settings())
    return app


app = create_core_app()
