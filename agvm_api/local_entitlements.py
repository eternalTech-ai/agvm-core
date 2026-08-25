# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

try:  # pragma: no cover - supports top-level imports in tests and Docker.
    from module_hardening import ModuleHardeningError, current_core_version, validate_module_grants
except ImportError:  # pragma: no cover
    from .module_hardening import ModuleHardeningError, current_core_version, validate_module_grants


LOCAL_LICENSE_STATE_SCHEMA_VERSION = "agvm.local_license_state.v1"
LOCAL_MODULE_LEASE_SCHEMA_VERSION = "agvm.local_module_lease.v1"
LOCAL_LICENSE_FILENAME = "agvm_local_license.json"
DEFAULT_PRO_MODULE_IDS = ("agvm_clone_app",)
KNOWN_MODULE_IDS = {
    "agvm_clone_app",
    "agvm_grow_studio",
    "agvm_maintain_studio",
    "agvm_agent_chat",
    "agvm_bench_pro",
    "agvm_advanced_cockpit",
}
PRO_PLAN = "pro"
PRO_PLUS_PLAN = "pro_plus"
PAID_PLAN_IDS = frozenset({PRO_PLAN, PRO_PLUS_PLAN})


class LocalEntitlementError(ValueError):
    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail or code


@dataclass(frozen=True)
class VerifiedModuleLease:
    payload: dict[str, Any]
    token: str
    signature_hash: str

    @property
    def plan(self) -> str:
        return str(self.payload.get("plan") or "").strip()

    @property
    def module_ids(self) -> list[str]:
        return _clean_module_ids(self.payload.get("modules") or [])

    @property
    def expires_at(self) -> str:
        return str(self.payload.get("expires_at") or "").strip()


def local_license_path() -> Path:
    data_dir = Path(os.getenv("AGVM_LAB_DATA_DIR") or Path(__file__).resolve().parent / "data").expanduser()
    return data_dir / LOCAL_LICENSE_FILENAME


def license_key_hash(license_key: str) -> str:
    clean = str(license_key or "").strip()
    if not clean:
        raise LocalEntitlementError("license_key_required")
    return hashlib.sha256(clean.encode("utf-8")).hexdigest()


def create_signed_module_lease(
    *,
    license_key: str,
    module_ids: Sequence[str] | None = None,
    plan: str = PRO_PLAN,
    ttl_hours: int = 24 * 14,
    grace_hours: int = 0,
    module_grants: Mapping[str, Any] | None = None,
    core_version: str | None = None,
    now: datetime | None = None,
    signing_secret: str | None = None,
) -> str:
    if ttl_hours <= 0 or ttl_hours > 24 * 90:
        raise LocalEntitlementError("lease_ttl_hours_out_of_range")
    secret = _require_signing_secret(signing_secret)
    issued = _now(now)
    clean_module_ids = _clean_module_ids(module_ids or DEFAULT_PRO_MODULE_IDS)
    if not clean_module_ids:
        raise LocalEntitlementError("module_ids_required")
    payload = {
        "schema_version": LOCAL_MODULE_LEASE_SCHEMA_VERSION,
        "nonce": secrets.token_urlsafe(18),
        "license_key_hash": license_key_hash(license_key),
        "plan": _clean_plan(plan),
        "modules": clean_module_ids,
        "issued_at": _format_datetime(issued),
        "expires_at": _format_datetime(issued + timedelta(hours=int(ttl_hours))),
        "issuer": "agvm_local_dev_fixture",
    }
    if grace_hours:
        if grace_hours < 0 or grace_hours > 24 * 14:
            raise LocalEntitlementError("lease_grace_hours_out_of_range")
        payload["grace_expires_at"] = _format_datetime(issued + timedelta(hours=int(ttl_hours) + int(grace_hours)))
    if module_grants:
        payload["module_grants"] = dict(module_grants)
    if core_version:
        payload["core_version"] = current_core_version(core_version)
    return encode_signed_module_lease(payload=payload, signing_secret=secret)


def encode_signed_module_lease(*, payload: Mapping[str, Any], signing_secret: str | None = None) -> str:
    secret = _require_signing_secret(signing_secret)
    clean_payload = _validate_lease_payload(payload, check_expiry=False)
    signature = _sign_payload(clean_payload, secret)
    envelope = {"payload": clean_payload, "signature": signature}
    return _b64encode_json(envelope)


def verify_signed_module_lease(
    token: str,
    *,
    signing_secret: str | None = None,
    allow_grace: bool | None = None,
    now: datetime | None = None,
) -> VerifiedModuleLease:
    secret = _require_signing_secret(signing_secret)
    envelope = _b64decode_json(token)
    payload = _mapping(envelope.get("payload"), "payload")
    signature = str(envelope.get("signature") or "").strip()
    if not signature:
        raise LocalEntitlementError("lease_signature_required")
    clean_payload = _validate_lease_payload(payload, check_expiry=True, allow_grace=allow_grace, now=now)
    expected = _sign_payload(clean_payload, secret)
    if not hmac.compare_digest(signature, expected):
        raise LocalEntitlementError("lease_signature_invalid")
    return VerifiedModuleLease(
        payload=dict(clean_payload),
        token=str(token or "").strip(),
        signature_hash=hashlib.sha256(signature.encode("utf-8")).hexdigest(),
    )


def activate_local_license(
    *,
    license_key: str | None = None,
    lease_token: str | None = None,
    module_ids: Sequence[str] | None = None,
    ttl_hours: int = 24 * 14,
    now: datetime | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    token = str(lease_token or "").strip()
    source = "platform_lease"
    if not token:
        if not _dev_fixture_allowed():
            raise LocalEntitlementError("lease_token_required")
        token = create_signed_module_lease(
            license_key=str(license_key or ""),
            module_ids=module_ids or DEFAULT_PRO_MODULE_IDS,
            ttl_hours=ttl_hours,
            now=now,
        )
        source = "local_dev_fixture"

    lease = verify_signed_module_lease(token, now=now)
    requested_hash = license_key_hash(license_key) if str(license_key or "").strip() else lease.payload["license_key_hash"]
    if requested_hash != lease.payload["license_key_hash"]:
        raise LocalEntitlementError("license_key_does_not_match_lease")

    state = {
        "schema_version": LOCAL_LICENSE_STATE_SCHEMA_VERSION,
        "source": source,
        "stored_at": _format_datetime(_now(now)),
        "license_key_hash": lease.payload["license_key_hash"],
        "lease_token": token,
        "lease": lease.payload,
        "signature_hash": lease.signature_hash,
    }
    _write_json_atomically(path or local_license_path(), state)
    return local_license_status(now=now, path=path)


def local_license_status(*, now: datetime | None = None, path: Path | None = None, include_token: bool = False) -> dict[str, Any]:
    state_path = path or local_license_path()
    raw_state = _read_state(state_path)
    if not raw_state:
        return _status_payload(
            state="missing",
            reason="local_license_not_configured",
            path=state_path,
            include_token=include_token,
        )
    token = str(raw_state.get("lease_token") or "").strip()
    try:
        lease = verify_signed_module_lease(token, allow_grace=_lease_grace_allowed(), now=now)
    except LocalEntitlementError as exc:
        return _status_payload(
            state="invalid" if exc.code != "lease_expired" else "expired",
            reason=exc.code,
            path=state_path,
            raw_state=raw_state,
            include_token=include_token,
        )
    state = "active"
    return _status_payload(
        state=state,
        reason="lease_valid",
        path=state_path,
        raw_state=raw_state,
        lease=lease,
        include_token=include_token,
    )


def module_entitlement_status(
    module_id: str,
    *,
    now: datetime | None = None,
    path: Path | None = None,
    include_token: bool = False,
) -> dict[str, Any]:
    clean_module_id = _clean_required_text("module_id", module_id)
    status = local_license_status(now=now, path=path, include_token=include_token)
    lease = status.get("lease") if isinstance(status.get("lease"), Mapping) else {}
    granted_modules = _clean_module_ids(lease.get("modules") or []) if lease else []
    if status["state"] != "active":
        license_state = "missing" if status["state"] == "missing" else status["state"]
        module_state = "missing" if license_state == "missing" else "unavailable"
        granted = False
    elif clean_module_id not in granted_modules:
        license_state = "missing"
        module_state = "unlicensed"
        granted = False
    else:
        license_state = "installed"
        module_state = "granted"
        granted = True
    return {
        "schema_version": "agvm.local_module_entitlement_status.v1",
        "module_id": clean_module_id,
        "granted": granted,
        "module_state": module_state,
        "license_state": license_state,
        "reason": status["reason"],
        "plan": lease.get("plan") if lease else None,
        "lease_expires_at": lease.get("expires_at") if lease else None,
        "lease_present": bool(status.get("lease_present")),
        "token_present": bool(status.get("token_present")),
        "module_token": status.get("lease_token") if include_token and granted else None,
        "module_grant": _public_module_grant(lease, clean_module_id) if lease else None,
    }


def module_env_for_supervisor(module_id: str, *, now: datetime | None = None, path: Path | None = None) -> dict[str, str]:
    status = module_entitlement_status(module_id, now=now, path=path, include_token=True)
    if not status["granted"]:
        raise LocalEntitlementError(f"module_not_granted:{module_id}", str(status.get("reason") or "module_not_granted"))
    token = str(status.get("module_token") or "").strip()
    if not token:
        raise LocalEntitlementError("module_token_missing")
    return {
        "AGVM_MODULE_ID": str(module_id),
        "AGVM_MODULE_TOKEN": token,
        "AGVM_MODULE_LICENSE_SOURCE": "local_license_lease",
    }


def all_local_module_entitlements(
    *,
    module_ids: Sequence[str] | None = None,
    now: datetime | None = None,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    ids = _clean_module_ids(module_ids or sorted(KNOWN_MODULE_IDS))
    return [module_entitlement_status(module_id, now=now, path=path) for module_id in ids]


def _status_payload(
    *,
    state: str,
    reason: str,
    path: Path,
    raw_state: Mapping[str, Any] | None = None,
    lease: VerifiedModuleLease | None = None,
    include_token: bool,
) -> dict[str, Any]:
    stored_lease = _mapping(raw_state.get("lease"), "lease") if raw_state and isinstance(raw_state.get("lease"), Mapping) else {}
    lease_payload = lease.payload if lease else dict(stored_lease)
    token = str(raw_state.get("lease_token") or "") if raw_state else ""
    return {
        "schema_version": LOCAL_LICENSE_STATE_SCHEMA_VERSION,
        "state": state,
        "reason": reason,
        "storage_path": str(path),
        "stored": bool(raw_state),
        "source": str(raw_state.get("source") or "") if raw_state else "",
        "stored_at": str(raw_state.get("stored_at") or "") if raw_state else "",
        "license_key_hash": str(raw_state.get("license_key_hash") or "") if raw_state else "",
        "license_key_masked": _mask_hash(str(raw_state.get("license_key_hash") or "")) if raw_state else "",
        "lease_present": bool(lease_payload),
        "token_present": bool(token),
        "lease": _public_lease_payload(lease_payload),
        "lease_token": token if include_token else None,
        "signing_configured": bool(_signing_secret_from_env()),
        "dev_fixture_allowed": _dev_fixture_allowed(),
    }


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LocalEntitlementError("local_license_state_unreadable", str(exc)) from exc
    if not isinstance(payload, dict):
        raise LocalEntitlementError("local_license_state_must_be_object")
    if payload.get("schema_version") != LOCAL_LICENSE_STATE_SCHEMA_VERSION:
        raise LocalEntitlementError("local_license_state_schema_invalid")
    return payload


def _write_json_atomically(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup_path = path.with_suffix(path.suffix + ".bak")
        backup_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        _chmod_private(backup_path)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(dict(payload), handle, indent=2, sort_keys=True)
            handle.write("\n")
        _chmod_private(tmp_path)
        os.replace(tmp_path, path)
        _chmod_private(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _validate_lease_payload(
    payload: Mapping[str, Any],
    *,
    check_expiry: bool,
    allow_grace: bool | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if payload.get("schema_version") != LOCAL_MODULE_LEASE_SCHEMA_VERSION:
        raise LocalEntitlementError("lease_schema_invalid")
    modules = _clean_module_ids(payload.get("modules") or [])
    if not modules:
        raise LocalEntitlementError("lease_modules_required")
    issued_at = _parse_datetime(str(payload.get("issued_at") or ""))
    expires_at = _parse_datetime(str(payload.get("expires_at") or ""))
    if expires_at <= issued_at:
        raise LocalEntitlementError("lease_expiry_must_follow_issue_time")
    grace_expires_at_text = str(payload.get("grace_expires_at") or "").strip()
    grace_expires_at = _parse_datetime(grace_expires_at_text) if grace_expires_at_text else None
    if grace_expires_at and grace_expires_at < expires_at:
        raise LocalEntitlementError("lease_grace_must_follow_expiry")
    grace_active = False
    now_value = _now(now)
    if check_expiry and expires_at <= now_value:
        grace_active = bool(allow_grace) and grace_expires_at is not None and grace_expires_at > now_value
        if not grace_active:
            raise LocalEntitlementError("lease_expired")
    key_hash = str(payload.get("license_key_hash") or "").strip()
    if len(key_hash) != 64 or any(char not in "0123456789abcdef" for char in key_hash):
        raise LocalEntitlementError("lease_license_key_hash_invalid")
    clean_payload: dict[str, Any] = {
        "schema_version": LOCAL_MODULE_LEASE_SCHEMA_VERSION,
        "nonce": _clean_required_text("nonce", payload.get("nonce")),
        "license_key_hash": key_hash,
        "plan": _clean_plan(str(payload.get("plan") or "")),
        "modules": modules,
        "issued_at": _format_datetime(issued_at),
        "expires_at": _format_datetime(expires_at),
        "issuer": _clean_required_text("issuer", payload.get("issuer")),
    }
    if grace_expires_at:
        clean_payload["grace_expires_at"] = _format_datetime(grace_expires_at)
    if payload.get("core_version"):
        clean_payload["core_version"] = current_core_version(str(payload.get("core_version") or ""))
    try:
        grants = validate_module_grants(
            payload.get("module_grants"),
            module_ids=modules if payload.get("module_grants") else None,
            core_version=str(payload.get("core_version") or "") or None,
        )
    except ModuleHardeningError as exc:
        raise LocalEntitlementError(exc.code, exc.detail) from exc
    if grants:
        clean_payload["module_grants"] = grants
    return clean_payload


def _sign_payload(payload: Mapping[str, Any], signing_secret: str) -> str:
    return hmac.new(
        signing_secret.encode("utf-8"),
        _canonical_json(payload).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _b64encode_json(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode_json(token: str) -> dict[str, Any]:
    text = str(token or "").strip()
    if not text:
        raise LocalEntitlementError("lease_token_required")
    try:
        padded = text + "=" * (-len(text) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, json.JSONDecodeError) as exc:
        raise LocalEntitlementError("lease_token_invalid") from exc
    if not isinstance(payload, dict):
        raise LocalEntitlementError("lease_token_must_decode_to_object")
    return payload


def _require_signing_secret(value: str | None = None) -> str:
    secret = str(value or _signing_secret_from_env() or "").strip()
    if len(secret) < 16:
        raise LocalEntitlementError("local_license_signing_secret_missing")
    return secret


def _signing_secret_from_env() -> str:
    return str(
        os.getenv("AGVM_LOCAL_LICENSE_SIGNING_SECRET")
        or os.getenv("AGVM_MODULE_LICENSE_SIGNING_SECRET")
        or ""
    ).strip()


def _dev_fixture_allowed() -> bool:
    return str(os.getenv("AGVM_ALLOW_DEV_LICENSE_FIXTURE") or "").strip().lower() in {"1", "true", "yes", "on"}


def _lease_grace_allowed() -> bool:
    return str(os.getenv("AGVM_ALLOW_LOCAL_LICENSE_GRACE") or "").strip().lower() in {"1", "true", "yes", "on"}


def _clean_module_ids(value: Sequence[str] | Any) -> list[str]:
    if isinstance(value, str):
        candidates = [item.strip() for item in value.split(",")]
    elif isinstance(value, Sequence):
        candidates = [str(item or "").strip() for item in value]
    else:
        raise LocalEntitlementError("module_ids_must_be_list")
    result = sorted(dict.fromkeys(item for item in candidates if item))
    for module_id in result:
        if module_id not in KNOWN_MODULE_IDS:
            raise LocalEntitlementError(f"unknown_module_id:{module_id}")
    return result


def _clean_plan(value: str) -> str:
    plan = str(value or "").strip().lower()
    if plan not in PAID_PLAN_IDS:
        raise LocalEntitlementError(f"unsupported_plan:{plan or '<missing>'}")
    return plan


def _public_lease_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not payload:
        return {}
    public: dict[str, Any] = {
        "schema_version": str(payload.get("schema_version") or ""),
        "plan": str(payload.get("plan") or ""),
        "modules": _clean_module_ids(payload.get("modules") or []),
        "issued_at": str(payload.get("issued_at") or ""),
        "expires_at": str(payload.get("expires_at") or ""),
        "issuer": str(payload.get("issuer") or ""),
    }
    if payload.get("grace_expires_at"):
        public["grace_expires_at"] = str(payload.get("grace_expires_at") or "")
    if payload.get("core_version"):
        public["core_version"] = str(payload.get("core_version") or "")
    grants = payload.get("module_grants")
    if isinstance(grants, Mapping):
        public["module_grants"] = {
            str(module_id): {
                key: str(grant.get(key) or "")
                for key in ("version", "image_digest", "manifest_digest", "required_core", "rollout_channel")
            }
            for module_id, grant in grants.items()
            if isinstance(grant, Mapping)
        }
    return public


def _public_module_grant(lease: Mapping[str, Any], module_id: str) -> dict[str, Any] | None:
    grants = lease.get("module_grants")
    if not isinstance(grants, Mapping):
        return None
    grant = grants.get(module_id)
    if not isinstance(grant, Mapping):
        return None
    return {
        "schema_version": str(grant.get("schema_version") or ""),
        "module_id": str(grant.get("module_id") or module_id),
        "version": str(grant.get("version") or ""),
        "image_ref": str(grant.get("image_ref") or ""),
        "image_digest": str(grant.get("image_digest") or ""),
        "manifest_digest": str(grant.get("manifest_digest") or ""),
        "required_core": str(grant.get("required_core") or ""),
        "rollout_channel": str(grant.get("rollout_channel") or ""),
    }


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LocalEntitlementError(f"{name}_must_be_object")
    return value


def _clean_required_text(name: str, value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise LocalEntitlementError(f"{name}_required")
    if "\n" in text or "\r" in text:
        raise LocalEntitlementError(f"{name}_must_be_single_line")
    return text


def _parse_datetime(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise LocalEntitlementError("datetime_required")
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise LocalEntitlementError("datetime_invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _now(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _mask_hash(value: str) -> str:
    text = str(value or "").strip()
    if len(text) < 12:
        return ""
    return f"{text[:8]}...{text[-6:]}"


def _chmod_private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass
