# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "agvm_api"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

from local_entitlements import (  # noqa: E402
    DEFAULT_PRO_MODULE_IDS,
    LocalEntitlementError,
    activate_local_license,
    all_local_module_entitlements,
    local_license_status,
    module_entitlement_status,
    module_env_for_supervisor,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AGVM local module supervisor prototype.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser("status", help="Show local license and module entitlement status.")
    status_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    activate_parser = subparsers.add_parser("activate-dev-fixture", help="Create and store a signed local dev Pro lease.")
    activate_parser.add_argument("--license-key", required=True, help="Local development license key placeholder.")
    activate_parser.add_argument("--module", action="append", dest="modules", default=[], help="Granted module id.")
    activate_parser.add_argument("--ttl-hours", type=int, default=24 * 14, help="Lease lifetime in hours.")
    activate_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    platform_activate_parser = subparsers.add_parser("activate-platform-lease", help="Store a platform-issued Pro lease locally.")
    platform_activate_parser.add_argument("--license-key", required=True, help="AGVM platform license key.")
    platform_activate_parser.add_argument("--lease-token", required=True, help="Lease token returned by /v1/licenses/activate.")
    platform_activate_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    check_parser = subparsers.add_parser("check", help="Check one module entitlement.")
    check_parser.add_argument("--module", required=True, help="Module id, for example agvm_clone_app.")
    check_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    plan_parser = subparsers.add_parser("plan", help="Print the local compose activation plan for a granted module.")
    plan_parser.add_argument("--module", default="agvm_clone_app", help="Module id.")
    plan_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    env_parser = subparsers.add_parser("module-env", help="Print env lines required by the module sidecar.")
    env_parser.add_argument("--module", default="agvm_clone_app", help="Module id.")
    env_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            return _emit_status(as_json=args.json)
        if args.command == "activate-dev-fixture":
            modules = args.modules or list(DEFAULT_PRO_MODULE_IDS)
            return _emit_activation(
                license_key=args.license_key,
                modules=modules,
                ttl_hours=args.ttl_hours,
                as_json=args.json,
            )
        if args.command == "activate-platform-lease":
            return _emit_platform_activation(
                license_key=args.license_key,
                lease_token=args.lease_token,
                as_json=args.json,
            )
        if args.command == "check":
            return _emit_check(module_id=args.module, as_json=args.json)
        if args.command == "plan":
            return _emit_plan(module_id=args.module, as_json=args.json)
        if args.command == "module-env":
            return _emit_env(module_id=args.module, as_json=args.json)
    except LocalEntitlementError as exc:
        payload = {"ok": False, "error": exc.code, "detail": exc.detail}
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 2
    return 1


def _emit_status(*, as_json: bool) -> int:
    payload = {
        "license": local_license_status(),
        "modules": all_local_module_entitlements(),
    }
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        license_status = payload["license"]
        print(f"license: {license_status['state']} ({license_status['reason']})")
        for item in payload["modules"]:
            print(f"{item['module_id']}: {item['module_state']} / {item['license_state']}")
    return 0


def _emit_activation(*, license_key: str, modules: Sequence[str], ttl_hours: int, as_json: bool) -> int:
    payload = activate_local_license(
        license_key=license_key,
        module_ids=list(modules),
        ttl_hours=ttl_hours,
    )
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"license: {payload['state']} ({payload['reason']})")
        print(f"stored: {payload['storage_path']}")
    return 0


def _emit_platform_activation(*, license_key: str, lease_token: str, as_json: bool) -> int:
    payload = activate_local_license(
        license_key=license_key,
        lease_token=lease_token,
    )
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"license: {payload['state']} ({payload['reason']})")
        print(f"source: {payload['source']}")
        print(f"stored: {payload['storage_path']}")
    return 0


def _emit_check(*, module_id: str, as_json: bool) -> int:
    payload = module_entitlement_status(module_id)
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"{payload['module_id']}: {payload['module_state']} / {payload['license_state']} ({payload['reason']})")
    return 0 if payload["granted"] else 2


def _emit_plan(*, module_id: str, as_json: bool) -> int:
    entitlement = module_entitlement_status(module_id)
    payload = {
        "schema_version": "agvm.local_module_supervisor_plan.v1",
        "module_id": module_id,
        "granted": entitlement["granted"],
        "reason": entitlement["reason"],
        "compose_files": ["docker-compose.core.yml", "docker-compose.pro.local.yml"],
        "command": "docker compose -f docker-compose.core.yml -f docker-compose.pro.local.yml up -d --build",
        "manual_env_command": f"python scripts/agvm_module_supervisor.py module-env --module {module_id}",
        "docker_socket_required": False,
    }
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"module: {module_id}")
        print(f"granted: {payload['granted']} ({payload['reason']})")
        print(f"env: {payload['manual_env_command']}")
        print(f"compose: {payload['command']}")
        print("docker_socket_required: false")
    return 0 if entitlement["granted"] else 2


def _emit_env(*, module_id: str, as_json: bool) -> int:
    payload = module_env_for_supervisor(module_id)
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for key, value in payload.items():
            print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
