from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "agvm_api"
SDK_PY_DIR = ROOT / "sdk" / "python"
for path in (API_DIR, SDK_PY_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from agvm_sdk.entitlement_lease import (  # noqa: E402
    LOCAL_MODULE_LEASE_SCHEMA_VERSION,
    entitlement_status_from_lease,
    normalize_lease_payload,
)
from agvm_sdk.mcp_contract import McpContractError, normalize_mcp_tool_contract_summary  # noqa: E402
from agvm_sdk.module_manifest import (  # noqa: E402
    AGVM_MODULE_MANIFEST_SCHEMA_VERSION,
    normalize_module_manifest,
)
from agvm_sdk.module_release import build_module_release_metadata, current_core_version  # noqa: E402
from scripts import check_public_export  # noqa: E402


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_ocm16_python_sdk_can_be_imported_directly_by_module_consumers() -> None:
    registry_signing_fixture = "ocm16-" + ("x" * 32)
    manifest = normalize_module_manifest(
        {
            "schema_version": AGVM_MODULE_MANIFEST_SCHEMA_VERSION,
            "module_id": "agvm_clone_app",
            "module_version": "0.1.0",
            "edition": "paid",
            "backend_status": "healthy",
            "license_state": "installed",
            "available": True,
            "api_base_path": "/clone-app",
            "ui": {"kind": "none", "mounts": []},
            "capabilities": {"clone_chat": True},
            "mcp_tools": {"adds_tools": [], "uses_core_tools": ["retrieve_context"]},
            "license": {"plan_required": "pro"},
            "safe_fallback_message": "Module unavailable.",
            "diagnostics": {},
        }
    )
    release = build_module_release_metadata(
        module_id="agvm_clone_app",
        version="0.1.0",
        image_ref="registry.example/agvm/agvm_clone_app:0.1.0",
        signing_secret=registry_signing_fixture,
    )

    assert manifest.module_id == "agvm_clone_app"
    assert release["schema_version"] == "agvm.module_release.v1"
    assert release["required_core"] == ">=0.5.0,<1.0.0"
    assert release["compatibility"]["target"] == "agvm_core"
    assert release["compatibility"]["min_version"] == "0.5.0"
    assert release["compatibility"]["max_version"] == "1.0.0"
    assert release["changelog"] == ["Initial signed Pro module release."]
    assert release["metadata_completeness"]["complete"] is True
    assert current_core_version() == "0.5.0"


def test_ocm16_legacy_python_adapters_find_sdk_without_external_pythonpath() -> None:
    code = (
        "import json\n"
        "from module_manifest_contracts import AGVM_MODULE_MANIFEST_SCHEMA_VERSION\n"
        "from module_hardening import DEFAULT_CORE_VERSION\n"
        "print(json.dumps({'manifest': AGVM_MODULE_MANIFEST_SCHEMA_VERSION, 'core': DEFAULT_CORE_VERSION}))\n"
    )
    env = {**os.environ, "PYTHONPATH": str(API_DIR)}
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)

    assert payload == {"manifest": "agvm.module_manifest.v1", "core": "0.5.0"}


def test_ocm16_entitlement_and_mcp_contracts_are_dependency_light() -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    lease = normalize_lease_payload(
        {
            "schema_version": LOCAL_MODULE_LEASE_SCHEMA_VERSION,
            "plan": "pro",
            "modules": ["agvm_clone_app", "agvm_clone_app"],
            "issued_at": now.isoformat().replace("+00:00", "Z"),
            "expires_at": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            "issuer": "agvm-platform",
        }
    ).as_dict()
    status = entitlement_status_from_lease("agvm_clone_app", lease, active=True, reason="lease_valid")
    tool = normalize_mcp_tool_contract_summary(
        {
            "tool_name": "write_memory_commit",
            "scope_policy": "brain_apply",
            "permission_family": "explicit_apply",
            "http_method": "post",
            "endpoint": "/mcp/write-memory-commit",
        }
    )

    assert lease["modules"] == ["agvm_clone_app"]
    assert status["granted"] is True
    assert tool.as_dict()["http_method"] == "POST"
    with pytest.raises(McpContractError, match="unsupported_permission_family"):
        normalize_mcp_tool_contract_summary(
            {
                "tool_name": "wipe",
                "scope_policy": "brain_apply",
                "permission_family": "owner_override",
                "http_method": "POST",
                "endpoint": "/mcp/wipe",
            }
        )


def test_ocm16_sdk_contracts_do_not_import_private_runtime_config() -> None:
    module_release = read("sdk/python/agvm_sdk/module_release.py")
    module_manifest = read("sdk/python/agvm_sdk/module_manifest.py")
    module_runtime_license = read("sdk/python/agvm_sdk/module_runtime_license.py")
    entitlement_lease = read("sdk/python/agvm_sdk/entitlement_lease.py")
    mcp_contract = read("sdk/python/agvm_sdk/mcp_contract.py")

    for source in (module_release, module_manifest, module_runtime_license, entitlement_lease, mcp_contract):
        assert "from config import" not in source
        assert "agvm_clone_app" not in source
        assert "platform.agvm_platform" not in source


def test_ocm16_public_export_and_docker_include_sdk_contracts() -> None:
    allowlist = read("repo-policy/public-core-allowlist.txt")
    deny_policy = check_public_export.parse_denylist(ROOT / "repo-policy" / "private-denylist.txt")
    required = set(check_public_export.REQUIRED_PUBLIC_FILES)

    assert "sdk/python/agvm_sdk/__init__.py" in allowlist
    assert not deny_policy.matches("sdk/python/agvm_sdk/__init__.py")
    for path in (
        "sdk/python/pyproject.toml",
        "sdk/python/agvm_sdk/module_manifest.py",
        "sdk/python/agvm_sdk/module_release.py",
        "sdk/python/agvm_sdk/entitlement_lease.py",
        "sdk/python/agvm_sdk/module_runtime_license.py",
        "sdk/python/agvm_sdk/mcp_contract.py",
        "sdk/typescript/package.json",
        "sdk/typescript/src/index.ts",
        "sdk/typescript/src/moduleManifestContracts.ts",
        "sdk/typescript/src/moduleSlots.ts",
    ):
        assert path in allowlist
        assert path in required
        assert not deny_policy.matches(path)

    assert deny_policy.matches("docs/AGVM_SDK_CONTRACT_EXTRACTION.md")
    assert "PYTHONPATH=/app:/app/sdk/python" in read("agvm_api/Dockerfile.core")
    assert "COPY sdk/python ./sdk/python" in read("agvm_api/Dockerfile.core")
    assert "COPY sdk/python /app/sdk/python" in read("platform/Dockerfile")
    assert "COPY sdk/typescript /app/sdk/typescript" in read("agvm_cockpit_prototype/Dockerfile.core")
    assert "dockerfile: agvm_cockpit_prototype/Dockerfile.core" in read("docker-compose.core.yml")


def test_ocm16_typescript_sdk_is_the_cockpit_contract_source() -> None:
    manifest_adapter = read("agvm_cockpit_prototype/src/new-ui/modules/moduleManifestContracts.ts")
    slots_adapter = read("agvm_cockpit_prototype/src/new-ui/modules/moduleSlots.ts")
    tsconfig = read("agvm_cockpit_prototype/tsconfig.app.json")
    sdk_package = json.loads(read("sdk/typescript/package.json"))

    assert 'export * from "../../../../sdk/typescript/src/moduleManifestContracts";' in manifest_adapter
    assert 'export * from "../../../../sdk/typescript/src/moduleSlots";' in slots_adapter
    assert '../sdk/typescript/src/**/*.ts' in tsconfig
    assert sdk_package["exports"]["./module-manifest"] == "./src/moduleManifestContracts.ts"
