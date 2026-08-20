"""Runtime license evaluation for AGVM private module sidecars.

This helper is intentionally dependency-light so private module images can
validate platform-issued local leases without importing AGVM core runtime code.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from .entitlement_lease import EntitlementLeaseError, clean_module_ids, clean_plan, parse_iso_datetime
from .module_release import ModuleHardeningError, current_core_version, validate_module_grant


ModuleRuntimeLicenseState = Literal["installed", "missing", "expired", "invalid"]
ModuleRuntimeBackendStatus = Literal["healthy", "incompatible"]


@dataclass(frozen=True)
class ModuleRuntimeLicense:
    module_id: str
    license_state: ModuleRuntimeLicenseState
    reason: str
    token_present: bool
    lease_expires_at: str = ""
    plan: str = ""
    backend_status: ModuleRuntimeBackendStatus = "healthy"
    module_grant: Mapping[str, Any] | None = None
    source: Literal["signed_lease", "unsigned_dev_fixture", "missing_token"] = "missing_token"

    @property
    def installed(self) -> bool:
        return self.license_state == "installed" and self.backend_status == "healthy"

    @property
    def dev_fixture(self) -> bool:
        return self.source == "unsigned_dev_fixture"

    def diagnostics(self) -> dict[str, Any]:
        return {
            "license_reason": self.reason,
            "lease_expires_at": self.lease_expires_at,
            "plan": self.plan,
            "backend_status": self.backend_status,
            "module_token_present": self.token_present,
            "module_grant_required": _platform_grant_required(),
            "module_grant_present": bool(self.module_grant),
            "module_grant": _public_module_grant(self.module_grant),
            "dev_fixture": self.dev_fixture,
            "unsigned_fixture_allowed": _unsigned_fixture_allowed(),
            "license_source": self.source,
        }


def evaluate_module_runtime_license(module_id: str, *, now: datetime | None = None) -> ModuleRuntimeLicense:
    clean_module_id = _required_text(module_id, "module_id")
    token = str(os.getenv("AGVM_MODULE_TOKEN") or "").strip()
    if token:
        return _evaluate_signed_token(clean_module_id, token, now=now)
    if _unsigned_fixture_allowed():
        state = _fixture_license_state()
        return ModuleRuntimeLicense(
            module_id=clean_module_id,
            license_state=state,
            reason="unsigned_development_fixture",
            token_present=False,
            source="unsigned_dev_fixture",
        )
    return ModuleRuntimeLicense(
        module_id=clean_module_id,
        license_state="missing",
        reason="module_token_missing",
        token_present=False,
        source="missing_token",
    )


def _evaluate_signed_token(module_id: str, token: str, *, now: datetime | None = None) -> ModuleRuntimeLicense:
    try:
        envelope = _b64decode_json(token)
        payload = _mapping(envelope.get("payload"), "payload")
        signature = str(envelope.get("signature") or "").strip()
        if not signature:
            return _invalid(module_id, "lease_signature_required", token_present=True)
        clean_payload = _validate_payload(payload)
        _raise_if_expired(clean_payload, now=now)
        expected = _sign_payload(clean_payload, _signing_secret())
        if not hmac.compare_digest(signature, expected):
            return _invalid(module_id, "lease_signature_invalid", token_present=True)
    except EntitlementLeaseError as exc:
        if exc.code == "lease_expired":
            return ModuleRuntimeLicense(
                module_id=module_id,
                license_state="expired",
                reason=exc.code,
                token_present=True,
                source="signed_lease",
            )
        return _invalid(module_id, exc.code, token_present=True)
    except ValueError as exc:
        reason = str(exc) or "lease_token_invalid"
        return _invalid(module_id, reason, token_present=True)

    if module_id not in clean_module_ids(clean_payload.get("modules") or []):
        return ModuleRuntimeLicense(
            module_id=module_id,
            license_state="missing",
            reason="module_not_granted",
            token_present=True,
            lease_expires_at=str(clean_payload.get("expires_at") or ""),
            plan=str(clean_payload.get("plan") or ""),
            source="signed_lease",
        )

    grant = _module_grant(clean_payload, module_id)
    if _platform_grant_required() or grant is not None:
        if grant is None:
            return _invalid(
                module_id,
                "module_grant_required",
                token_present=True,
                lease_expires_at=str(clean_payload.get("expires_at") or ""),
                plan=str(clean_payload.get("plan") or ""),
            )
        try:
            validate_module_grant(
                grant,
                module_id=module_id,
                core_version=current_core_version(),
                expected_image_digest=_expected_image_digest(),
                expected_manifest_digest=_expected_manifest_digest(),
            )
        except ModuleHardeningError as exc:
            return ModuleRuntimeLicense(
                module_id=module_id,
                license_state="invalid",
                reason=exc.code,
                token_present=True,
                lease_expires_at=str(clean_payload.get("expires_at") or ""),
                plan=str(clean_payload.get("plan") or ""),
                backend_status="incompatible" if exc.code == "module_core_version_incompatible" else "healthy",
                module_grant=grant,
                source="signed_lease",
            )

    reason = "lease_grace_active" if _is_after_expiry(clean_payload, now=now) else "lease_valid"
    return ModuleRuntimeLicense(
        module_id=module_id,
        license_state="installed",
        reason=reason,
        token_present=True,
        lease_expires_at=str(clean_payload.get("expires_at") or ""),
        plan=str(clean_payload.get("plan") or ""),
        module_grant=grant,
        source="signed_lease",
    )


def _raise_if_expired(payload: Mapping[str, Any], *, now: datetime | None = None) -> None:
    expires_at = parse_iso_datetime(str(payload.get("expires_at") or ""))
    if expires_at > _now(now):
        return
    grace_text = str(payload.get("grace_expires_at") or "").strip()
    grace_expires_at = parse_iso_datetime(grace_text) if grace_text else None
    if _lease_grace_allowed() and grace_expires_at and grace_expires_at > _now(now):
        return
    raise EntitlementLeaseError("lease_expired")


def _validate_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if payload.get("schema_version") != "agvm.local_module_lease.v1":
        raise EntitlementLeaseError("lease_schema_invalid")
    issued_at = parse_iso_datetime(str(payload.get("issued_at") or ""))
    expires_at = parse_iso_datetime(str(payload.get("expires_at") or ""))
    if expires_at <= issued_at:
        raise EntitlementLeaseError("lease_expiry_must_follow_issue_time")
    grace_text = str(payload.get("grace_expires_at") or "").strip()
    grace_expires_at = parse_iso_datetime(grace_text) if grace_text else None
    if grace_expires_at and grace_expires_at < expires_at:
        raise EntitlementLeaseError("lease_grace_must_follow_expiry")
    key_hash = str(payload.get("license_key_hash") or "").strip()
    if len(key_hash) != 64 or any(char not in "0123456789abcdef" for char in key_hash):
        raise EntitlementLeaseError("lease_license_key_hash_invalid")
    clean_payload: dict[str, Any] = {
        "schema_version": "agvm.local_module_lease.v1",
        "nonce": _required_text(payload.get("nonce"), "nonce"),
        "license_key_hash": key_hash,
        "plan": clean_plan(str(payload.get("plan") or "")),
        "modules": clean_module_ids(payload.get("modules") or []),
        "issued_at": _format_datetime(issued_at),
        "expires_at": _format_datetime(expires_at),
        "issuer": _required_text(payload.get("issuer"), "issuer"),
    }
    if grace_expires_at:
        clean_payload["grace_expires_at"] = _format_datetime(grace_expires_at)
    if payload.get("core_version"):
        clean_payload["core_version"] = current_core_version(str(payload.get("core_version") or ""))
    grants = payload.get("module_grants")
    if grants is not None:
        if not isinstance(grants, Mapping):
            raise EntitlementLeaseError("module_grants_must_be_object")
        clean_payload["module_grants"] = {
            str(module_id): dict(_mapping(grant, "module_grant"))
            for module_id, grant in grants.items()
        }
    return clean_payload


def _invalid(
    module_id: str,
    reason: str,
    *,
    token_present: bool,
    lease_expires_at: str = "",
    plan: str = "",
) -> ModuleRuntimeLicense:
    return ModuleRuntimeLicense(
        module_id=module_id,
        license_state="invalid",
        reason=reason,
        token_present=token_present,
        lease_expires_at=lease_expires_at,
        plan=plan,
        source="signed_lease" if token_present else "missing_token",
    )


def _sign_payload(payload: Mapping[str, Any], signing_secret: str) -> str:
    return hmac.new(
        signing_secret.encode("utf-8"),
        json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _b64decode_json(token: str) -> dict[str, Any]:
    try:
        padded = token + "=" * (-len(token) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("lease_token_invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("lease_token_must_decode_to_object")
    return payload


def _signing_secret() -> str:
    secret = str(
        os.getenv("AGVM_MODULE_LICENSE_SIGNING_SECRET")
        or os.getenv("AGVM_LOCAL_LICENSE_SIGNING_SECRET")
        or ""
    ).strip()
    if len(secret) < 16:
        raise ValueError("module_license_signing_secret_missing")
    return secret


def _fixture_license_state() -> ModuleRuntimeLicenseState:
    value = str(os.getenv("AGVM_MODULE_LICENSE_STATE") or "missing").strip().lower()
    if value in {"installed", "missing", "expired", "invalid"}:
        return value  # type: ignore[return-value]
    return "invalid"


def _unsigned_fixture_allowed() -> bool:
    return str(os.getenv("AGVM_ALLOW_UNSIGNED_MODULE_LICENSE_FIXTURE") or "").strip().lower() in {"1", "true", "yes", "on"}


def _platform_grant_required() -> bool:
    return str(os.getenv("AGVM_MODULE_REQUIRE_PLATFORM_GRANT") or "true").strip().lower() in {"1", "true", "yes", "on"}


def _lease_grace_allowed() -> bool:
    return str(os.getenv("AGVM_MODULE_ALLOW_LEASE_GRACE") or "").strip().lower() in {"1", "true", "yes", "on"}


def _expected_image_digest() -> str | None:
    return str(os.getenv("AGVM_MODULE_IMAGE_DIGEST") or "").strip() or None


def _expected_manifest_digest() -> str | None:
    return str(os.getenv("AGVM_MODULE_MANIFEST_DIGEST") or "").strip() or None


def _module_grant(payload: Mapping[str, Any], module_id: str) -> Mapping[str, Any] | None:
    grants = payload.get("module_grants")
    if not isinstance(grants, Mapping):
        return None
    grant = grants.get(module_id)
    return grant if isinstance(grant, Mapping) else None


def _public_module_grant(grant: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(grant, Mapping):
        return None
    return {
        "module_id": str(grant.get("module_id") or ""),
        "version": str(grant.get("version") or ""),
        "image_digest": str(grant.get("image_digest") or ""),
        "manifest_digest": str(grant.get("manifest_digest") or ""),
        "required_core": str(grant.get("required_core") or ""),
        "rollout_channel": str(grant.get("rollout_channel") or ""),
    }


def _is_after_expiry(payload: Mapping[str, Any], *, now: datetime | None = None) -> bool:
    return parse_iso_datetime(str(payload.get("expires_at") or "")) <= _now(now)


def _format_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _now(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name}_must_be_object")
    return value


def _required_text(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name}_required")
    if "\n" in text or "\r" in text:
        raise ValueError(f"{name}_must_be_single_line")
    return text
