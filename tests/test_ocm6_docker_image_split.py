from __future__ import annotations

import importlib
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "agvm_api"
CLONE_APP_BACKEND_DIR = ROOT / "apps" / "agvm_clone_app" / "backend"
for path in (API_DIR, CLONE_APP_BACKEND_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from route_classification import classify_docker_service, discover_docker_compose_services  # noqa: E402


def test_ocm6_core_compose_contains_only_public_core_services() -> None:
    services = discover_docker_compose_services(ROOT / "docker-compose.core.yml")
    text = (ROOT / "docker-compose.core.yml").read_text(encoding="utf-8")

    assert set(services) == {"agvm_core_api", "agvm_core_ui", "agvm_mcp"}
    assert all(classify_docker_service(service).public_core_allowed for service in services)  # type: ignore[union-attr]
    assert "agvm_clone_app" not in text
    assert "apps/agvm_clone_app" not in text


def test_ocm6_pro_overlay_and_module_fragment_are_paid_sidecars() -> None:
    pro_services = discover_docker_compose_services(ROOT / "docker-compose.pro.local.yml")
    module_services = discover_docker_compose_services(ROOT / "apps" / "agvm_clone_app" / "docker-compose.module.yml")

    assert set(pro_services) == {"agvm_clone_app_api", "agvm_clone_app_ui"}
    assert set(module_services) == {"agvm_clone_app_api", "agvm_clone_app_ui"}
    for service in [*pro_services, *module_services]:
        classification = classify_docker_service(service)
        assert classification is not None
        assert classification.category == "paid_module"
        assert classification.public_core_allowed is False


def test_ocm6_core_dockerfiles_do_not_copy_paid_module_code() -> None:
    api_dockerfile = (API_DIR / "Dockerfile.core").read_text(encoding="utf-8")
    api_ignore = (API_DIR / "Dockerfile.core.dockerignore").read_text(encoding="utf-8")
    ui_dockerfile = (ROOT / "agvm_cockpit_prototype" / "Dockerfile.core").read_text(encoding="utf-8")

    assert 'CMD ["uvicorn", "core_api_app:app"' in api_dockerfile
    assert "AGVM_EDITION=core" in api_dockerfile
    assert "apps/agvm_clone_app" not in api_dockerfile
    assert "PYTHONPATH=/app/apps/agvm_clone_app/backend" not in api_dockerfile
    assert "apps/agvm_clone_app/**" in api_ignore
    assert "VITE_AGVM_UI_SHELL_PROFILE=public_core" in ui_dockerfile
    assert "COPY sdk/typescript /app/sdk/typescript" in ui_dockerfile
    assert "COPY apps/agvm_clone_app" not in ui_dockerfile


def test_ocm6_clone_app_sidecar_dockerfiles_are_module_scoped() -> None:
    api_dockerfile = (CLONE_APP_BACKEND_DIR / "Dockerfile").read_text(encoding="utf-8")
    ui_dockerfile = (ROOT / "apps" / "agvm_clone_app" / "frontend" / "Dockerfile").read_text(encoding="utf-8")

    assert 'CMD ["uvicorn", "agvm_clone_app.app:app"' in api_dockerfile
    assert "COPY ${AGVM_CLONE_APP_SOURCE_ROOT}/backend/agvm_clone_app ./agvm_clone_app" in api_dockerfile
    assert "COPY sdk/python ./sdk/python" in api_dockerfile
    assert "COPY agvm_api" not in api_dockerfile
    assert "COPY apps" not in api_dockerfile
    assert "COPY sdk/typescript ./sdk/typescript" in ui_dockerfile
    assert "module-entry.js" in ui_dockerfile


def test_ocm6_core_api_app_imports_extracted_core_only(monkeypatch, tmp_path: Path) -> None:
    source = (API_DIR / "core_api_app.py").read_text(encoding="utf-8")
    assert "import main" not in source
    assert "agvm_clone_app" not in source
    assert "mcp_grow" not in source
    assert "mcp_maintenance" not in source

    monkeypatch.setenv("AGVM_ALLOW_SETUP_WITHOUT_PROVIDER", "true")
    monkeypatch.setenv("AGVM_LAB_DATA_DIR", str(tmp_path))
    module = importlib.reload(sys.modules["core_api_app"]) if "core_api_app" in sys.modules else importlib.import_module("core_api_app")
    paths = {getattr(route, "path", "") for route in module.app.routes}

    assert {"/health", "/setup/env", "/runtime/edition", "/memory/brains", "/mcp/contracts"} <= paths
    assert not any(path.startswith("/clone-app/") for path in paths)
    assert {
        "/mcp/grow-source-preview",
        "/mcp/grow-source-apply",
        "/mcp/sleep-preview",
        "/mcp/evolve-preview",
    } <= paths
    assert not any(path.startswith("/source-investigation/") for path in paths)
    assert not any(path.startswith("/mcp/matrix-calibration-") for path in paths)
    assert "/dev/audit" not in paths


def test_ocm6_clone_app_sidecar_app_is_standalone_module() -> None:
    source = (CLONE_APP_BACKEND_DIR / "agvm_clone_app" / "app.py").read_text(encoding="utf-8")
    assert "agvm_api" not in source
    assert "import main" not in source

    module = importlib.reload(sys.modules["agvm_clone_app.app"]) if "agvm_clone_app.app" in sys.modules else importlib.import_module("agvm_clone_app.app")
    paths = {getattr(route, "path", "") for route in module.app.routes}

    assert "/health" in paths
    assert "/clone-app/module-manifest" in paths
    assert "/clone-app/chat/sessions" in paths
