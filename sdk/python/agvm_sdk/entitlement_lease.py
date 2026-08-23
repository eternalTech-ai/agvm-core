# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


LOCAL_MODULE_LEASE_SCHEMA_VERSION = "agvm.local_module_lease.v1"
LOCAL_MODULE_ENTITLEMENT_STATUS_SCHEMA_VERSION = "agvm.local_module_entitlement_status.v1"
PRO_PLAN = "pro"


class EntitlementLeaseError(ValueError):
    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail or code


@dataclass(frozen=True)
class ModuleLeaseView:
    schema_version: str
    plan: str
    modules: list[str]
    issued_at: str
    expires_at: str
    issuer: str
    core_version: str | None = None
    grace_expires_at: str | None = None
    module_grants: dict[str, dict[str, Any]] | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "plan": self.plan,
            "modules": list(self.modules),
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "issuer": self.issuer,
        }
        if self.core_version:
            payload["core_version"] = self.core_version
        if self.grace_expires_at:
            payload["grace_expires_at"] = self.grace_expires_at
        if self.module_grants is not None:
            payload["module_grants"] = dict(self.module_grants)
        return payload


def normalize_lease_payload(payload: Mapping[str, Any]) -> ModuleLeaseView:
    if payload.get("schema_version") != LOCAL_MODULE_LEASE_SCHEMA_VERSION:
        raise EntitlementLeaseError("lease_schema_invalid")
    modules = clean_module_ids(payload.get("modules") or [])
    if not modules:
        raise EntitlementLeaseError("lease_modules_required")
    issued_at = parse_iso_datetime(str(payload.get("issued_at") or ""))
    expires_at = parse_iso_datetime(str(payload.get("expires_at") or ""))
    if expires_at <= issued_at:
        raise EntitlementLeaseError("lease_expiry_must_follow_issue_time")
    grace_text = clean_optional_text(payload.get("grace_expires_at"))
    grace_expires_at = parse_iso_datetime(grace_text) if grace_text else None
    if grace_expires_at and grace_expires_at < expires_at:
        raise EntitlementLeaseError("lease_grace_must_follow_expiry")
    return ModuleLeaseView(
        schema_version=LOCAL_MODULE_LEASE_SCHEMA_VERSION,
        plan=clean_plan(str(payload.get("plan") or "")),
        modules=modules,
        issued_at=format_datetime(issued_at),
        expires_at=format_datetime(expires_at),
        issuer=clean_required_text(payload.get("issuer"), "issuer"),
        core_version=clean_optional_text(payload.get("core_version")),
        grace_expires_at=format_datetime(grace_expires_at) if grace_expires_at else None,
        module_grants=dict(payload.get("module_grants")) if isinstance(payload.get("module_grants"), Mapping) else None,
    )


def public_lease_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not payload:
        return {}
    return normalize_lease_payload(payload).as_dict()


def entitlement_status_from_lease(
    module_id: str,
    lease: Mapping[str, Any] | None,
    *,
    active: bool,
    reason: str,
) -> dict[str, Any]:
    clean_module_id = clean_required_text(module_id, "module_id")
    view = normalize_lease_payload(lease) if lease else None
    modules = view.modules if view else []
    granted = bool(active and clean_module_id in modules)
    if not active:
        license_state = "missing" if reason == "local_license_not_configured" else "invalid"
        module_state = "missing" if license_state == "missing" else "unavailable"
    elif not granted:
        license_state = "missing"
        module_state = "unlicensed"
    else:
        license_state = "installed"
        module_state = "granted"
    return {
        "schema_version": LOCAL_MODULE_ENTITLEMENT_STATUS_SCHEMA_VERSION,
        "module_id": clean_module_id,
        "granted": granted,
        "module_state": module_state,
        "license_state": license_state,
        "reason": reason,
        "plan": view.plan if view else None,
        "lease_expires_at": view.expires_at if view else None,
        "lease_present": bool(view),
    }


def clean_module_ids(value: Sequence[str] | Any) -> list[str]:
    if isinstance(value, str):
        candidates = [item.strip() for item in value.split(",")]
    elif isinstance(value, Sequence):
        candidates = [str(item or "").strip() for item in value]
    else:
        raise EntitlementLeaseError("module_ids_must_be_list")
    return sorted(dict.fromkeys(item for item in candidates if item))


def clean_plan(value: str) -> str:
    plan = str(value or "").strip().lower()
    if plan != PRO_PLAN:
        raise EntitlementLeaseError(f"unsupported_plan:{plan or '<missing>'}")
    return plan


def parse_iso_datetime(value: str) -> datetime:
    text = clean_required_text(value, "datetime").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise EntitlementLeaseError("datetime_invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean_required_text(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise EntitlementLeaseError(f"{name}_required")
    if "\n" in text or "\r" in text:
        raise EntitlementLeaseError(f"{name}_must_be_single_line")
    return text


def clean_optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if "\n" in text or "\r" in text:
        raise EntitlementLeaseError("optional_text_must_be_single_line")
    return text
