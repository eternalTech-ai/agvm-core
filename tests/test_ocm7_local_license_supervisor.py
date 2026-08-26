# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "agvm_api"
CLONE_APP_BACKEND_DIR = ROOT / "apps" / "agvm_clone_app" / "backend"
PUBLIC_EXPORT = (ROOT / ".agvm-public-export-marker").exists()
for path in (API_DIR, CLONE_APP_BACKEND_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from local_entitlements import (  # noqa: E402
    activate_local_license,
    create_signed_module_lease,
    local_license_status,
    module_entitlement_status,
    module_env_for_supervisor,
)
from route_classification import classify_routes, discover_fastapi_routes  # noqa: E402


SECRET = "ocm7-local-fixture-secret"


def test_ocm7_local_license_storage_keeps_raw_key_out_of_state(monkeypatch, tmp_path: Path) -> None:
    license_key = "agvm-dev-pro-local-owner-key"
    monkeypatch.setenv("AGVM_LAB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGVM_LOCAL_LICENSE_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("AGVM_ALLOW_DEV_LICENSE_FIXTURE", "true")

    assert local_license_status()["state"] == "missing"
    status = activate_local_license(license_key=license_key, module_ids=["agvm_clone_app"])
    stored = json.loads((tmp_path / "agvm_local_license.json").read_text(encoding="utf-8"))

    assert status["state"] == "active"
    assert status["lease"]["modules"] == ["agvm_clone_app"]
    assert license_key not in json.dumps(stored)
    assert stored["license_key_hash"] == status["license_key_hash"]
    assert module_entitlement_status("agvm_clone_app")["granted"] is True
    assert module_env_for_supervisor("agvm_clone_app")["AGVM_MODULE_TOKEN"] == stored["lease_token"]


def test_ocm7_core_license_router_exposes_activation_without_docker_socket(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGVM_LAB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGVM_LOCAL_LICENSE_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("AGVM_ALLOW_DEV_LICENSE_FIXTURE", "true")
    monkeypatch.setenv("AGVM_ALLOW_SETUP_WITHOUT_PROVIDER", "true")

    core_api_app = importlib.reload(sys.modules["core_api_app"]) if "core_api_app" in sys.modules else importlib.import_module("core_api_app")
    client = TestClient(core_api_app.app)

    assert client.get("/modules/local-license").json()["state"] == "missing"
    activated = client.post(
        "/modules/local-license/activate",
        json={"license_key": "agvm-dev-pro-test", "module_ids": ["agvm_clone_app"]},
    )
    assert activated.status_code == 200
    assert activated.json()["state"] == "active"

    entitlement = client.get("/modules/local-license/modules/agvm_clone_app").json()
    assert entitlement["granted"] is True
    assert entitlement["license_state"] == "installed"
    assert client.get("/modules/local-license/entitlements").json()["schema_version"] == "agvm.local_module_entitlements.v1"
    assert "docker.sock" not in (API_DIR / "core_api_app.py").read_text(encoding="utf-8")


def test_ocm7_public_core_routes_missing_verifier_to_account_module_setup(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGVM_LAB_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AGVM_LOCAL_LICENSE_SIGNING_SECRET", raising=False)
    monkeypatch.delenv("AGVM_MODULE_LICENSE_SIGNING_SECRET", raising=False)
    monkeypatch.delenv("AGVM_ALLOW_DEV_LICENSE_FIXTURE", raising=False)
    monkeypatch.setenv("AGVM_ALLOW_SETUP_WITHOUT_PROVIDER", "true")

    core_api_app = importlib.reload(sys.modules["core_api_app"]) if "core_api_app" in sys.modules else importlib.import_module("core_api_app")
    client = TestClient(core_api_app.app)

    response = client.post(
        "/modules/local-license/activate",
        json={"lease_token": "account-issued-lease"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "local_module_setup_required",
        "message": "Set up this paid local module from the Detwin account module manager.",
        "action": "open_detwin_module_manager",
        "action_path": "/account/modules#modules",
    }


def test_ocm7_clone_app_manifest_requires_valid_signed_module_token(monkeypatch) -> None:
    if PUBLIC_EXPORT and not CLONE_APP_BACKEND_DIR.exists():
        pytest.skip("Private Clone App runtime source is not part of the public Core export.")

    monkeypatch.setenv("AGVM_LOCAL_LICENSE_SIGNING_SECRET", SECRET)
    monkeypatch.delenv("AGVM_ALLOW_UNSIGNED_MODULE_LICENSE_FIXTURE", raising=False)

    monkeypatch.setenv("AGVM_MODULE_TOKEN", "not-a-valid-token")
    clone_app = importlib.reload(sys.modules["agvm_clone_app.app"]) if "agvm_clone_app.app" in sys.modules else importlib.import_module("agvm_clone_app.app")
    invalid_payload = TestClient(clone_app.create_app()).get("/clone-app/module-manifest").json()

    assert invalid_payload["available"] is False
    assert invalid_payload["license_state"] == "invalid"
    assert invalid_payload["diagnostics"]["license_reason"] == "lease_token_invalid"

    token = create_signed_module_lease(
        license_key="agvm-dev-pro-clone",
        module_ids=["agvm_clone_app"],
        plan="pro_plus",
        signing_secret=SECRET,
    )
    monkeypatch.setenv("AGVM_MODULE_TOKEN", token)
    ready_payload = TestClient(clone_app.create_app()).get("/clone-app/module-manifest").json()

    assert ready_payload["available"] is True
    assert ready_payload["license_state"] == "installed"
    assert ready_payload["diagnostics"]["license_reason"] == "lease_valid"


def test_ocm7_compose_files_do_not_auto_install_paid_modules() -> None:
    if PUBLIC_EXPORT:
        core_compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        assert "AGVM_MODULE_LICENSE_STATE" not in core_compose
        assert "agvm_clone_app" not in core_compose
        assert "AGVM_CLONE_APP_" not in core_compose
        assert "AGVM_LOCAL_LICENSE_SIGNING_SECRET" in core_compose
        assert "AGVM_ALLOW_DEV_LICENSE_FIXTURE" in core_compose
        return

    pro_overlay = (ROOT / "docker-compose.pro.local.yml").read_text(encoding="utf-8")
    module_fragment = (ROOT / "apps" / "agvm_clone_app" / "docker-compose.module.yml").read_text(encoding="utf-8")
    core_compose = (ROOT / "docker-compose.core.yml").read_text(encoding="utf-8")

    assert "AGVM_MODULE_LICENSE_STATE: ${AGVM_MODULE_LICENSE_STATE:-missing}" in pro_overlay
    assert "AGVM_MODULE_LICENSE_STATE: ${AGVM_MODULE_LICENSE_STATE:-missing}" in module_fragment
    assert "AGVM_MODULE_LICENSE_STATE: ${AGVM_MODULE_LICENSE_STATE:-installed}" not in pro_overlay
    assert "AGVM_MODULE_LICENSE_STATE: ${AGVM_MODULE_LICENSE_STATE:-installed}" not in module_fragment
    assert "AGVM_LOCAL_LICENSE_SIGNING_SECRET" in core_compose
    assert "AGVM_ALLOW_DEV_LICENSE_FIXTURE" in core_compose


def test_ocm7_license_routes_are_core_classified() -> None:
    routes = discover_fastapi_routes([API_DIR / "core_license_router.py"])
    classified = classify_routes(routes)

    assert {route.route.path for route in classified} == {
        "/modules/local-license",
        "/modules/local-license/activate",
        "/modules/local-license/entitlements",
        "/modules/local-license/modules/{module_id}",
    }
    assert {route.classification.category for route in classified} == {"core"}


def test_ocm7_supervisor_is_a_plan_generator_not_a_docker_controller() -> None:
    source = (ROOT / "scripts" / "agvm_module_supervisor.py").read_text(encoding="utf-8")

    assert "subprocess" not in source
    assert "docker.sock" not in source
    assert "docker_socket_required" in source
