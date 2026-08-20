"""Shared module-release hardening helpers for AGVM core and Pro modules.

This file is intentionally small and dependency-free: public core, the private
platform and module sidecars can all use it without importing product logic.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import Mapping as MappingABC
from collections.abc import Sequence as SequenceABC
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


MODULE_RELEASE_SCHEMA_VERSION = "agvm.module_release.v1"
MODULE_GRANT_SCHEMA_VERSION = "agvm.module_grant.v1"
DEFAULT_CORE_REQUIREMENT = ">=0.5.0,<1.0.0"
DEFAULT_CORE_VERSION = "0.5.0"
DEFAULT_ROLLOUT_CHANNEL = "stable"
SIGNATURE_ALGORITHM = "hmac-sha256"
DEFAULT_RELEASE_CHANGELOG = ("Initial signed Pro module release.",)
MODULE_RELEASE_COMPLETENESS_SCHEMA_VERSION = "agvm.module_release_completeness.v1"


class ModuleHardeningError(ValueError):
    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail or code


@dataclass(frozen=True)
class ModuleGrantValidation:
    module_id: str
    version: str
    image_digest: str
    manifest_digest: str
    required_core: str
    rollout_channel: str

    def as_dict(self) -> dict[str, str]:
        return {
            "module_id": self.module_id,
            "version": self.version,
            "image_digest": self.image_digest,
            "manifest_digest": self.manifest_digest,
            "required_core": self.required_core,
            "rollout_channel": self.rollout_channel,
        }


def current_core_version(value: str | None = None) -> str:
    return _clean_required_text(value or os.getenv("AGVM_CORE_VERSION") or DEFAULT_CORE_VERSION, "core_version")


def canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_digest_for_payload(payload: Mapping[str, Any]) -> str:
    return f"sha256:{hashlib.sha256(canonical_json(payload).encode('utf-8')).hexdigest()}"


def stable_image_digest(module_id: str, version: str) -> str:
    seed = f"agvm-module-image:{_clean_required_text(module_id, 'module_id')}:{_clean_required_text(version, 'version')}"
    return f"sha256:{hashlib.sha256(seed.encode('utf-8')).hexdigest()}"


def build_module_release_metadata(
    *,
    module_id: str,
    version: str,
    image_ref: str,
    required_plan: str = "pro",
    required_core: str = DEFAULT_CORE_REQUIREMENT,
    rollout_channel: str = DEFAULT_ROLLOUT_CHANNEL,
    image_digest: str | None = None,
    rollback_from_version: str | None = None,
    changelog: Sequence[str] | str | None = None,
    signing_secret: str | None = None,
) -> dict[str, Any]:
    clean_module_id = _clean_required_text(module_id, "module_id")
    clean_version = _clean_required_text(version, "version")
    clean_required_core = _clean_required_text(required_core, "required_core")
    clean_image_digest = validate_digest(image_digest or stable_image_digest(clean_module_id, clean_version), "image_digest")
    release_payload = {
        "schema_version": MODULE_RELEASE_SCHEMA_VERSION,
        "module_id": clean_module_id,
        "version": clean_version,
        "image_ref": _clean_required_text(image_ref, "image_ref"),
        "image_digest": clean_image_digest,
        "required_plan": _clean_required_text(required_plan, "required_plan"),
        "required_core": clean_required_core,
        "compatibility": core_compatibility_range(clean_required_core),
        "rollout_channel": _clean_required_text(rollout_channel, "rollout_channel"),
        "rollback_from_version": _clean_optional_text(rollback_from_version),
        "changelog": _clean_changelog(changelog),
    }
    manifest_digest = sha256_digest_for_payload(release_payload)
    release = {
        **release_payload,
        "manifest_digest": manifest_digest,
        "signature": sign_manifest_digest(
            module_id=clean_module_id,
            version=clean_version,
            image_digest=clean_image_digest,
            manifest_digest=manifest_digest,
            required_core=required_core,
            signing_secret=signing_secret,
        ),
    }
    return {
        **release,
        "metadata_completeness": module_release_metadata_completeness(release),
    }


def sign_manifest_digest(
    *,
    module_id: str,
    version: str,
    image_digest: str,
    manifest_digest: str,
    required_core: str,
    signing_secret: str | None,
) -> str:
    secret = str(signing_secret or "").strip()
    if not secret:
        return "unsigned:registry_signing_secret_missing"
    if len(secret) < 16:
        raise ModuleHardeningError("module_registry_signing_secret_too_short")
    payload = {
        "schema_version": MODULE_RELEASE_SCHEMA_VERSION,
        "module_id": _clean_required_text(module_id, "module_id"),
        "version": _clean_required_text(version, "version"),
        "image_digest": validate_digest(image_digest, "image_digest"),
        "manifest_digest": validate_digest(manifest_digest, "manifest_digest"),
        "required_core": _clean_required_text(required_core, "required_core"),
    }
    digest = hmac.new(secret.encode("utf-8"), canonical_json(payload).encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{SIGNATURE_ALGORITHM}:{digest}"


def module_grant_from_release(release: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": MODULE_GRANT_SCHEMA_VERSION,
        "module_id": _clean_required_text(release.get("module_id"), "module_id"),
        "version": _clean_required_text(release.get("version"), "version"),
        "image_ref": _clean_required_text(release.get("image_ref"), "image_ref"),
        "image_digest": validate_digest(str(release.get("image_digest") or ""), "image_digest"),
        "manifest_digest": validate_digest(str(release.get("manifest_digest") or ""), "manifest_digest"),
        "manifest_signature": _clean_required_text(release.get("signature"), "manifest_signature"),
        "required_core": _clean_required_text(release.get("required_core"), "required_core"),
        "rollout_channel": _clean_required_text(release.get("rollout_channel") or DEFAULT_ROLLOUT_CHANNEL, "rollout_channel"),
        "rollback_from_version": _clean_optional_text(release.get("rollback_from_version")),
    }


def module_release_metadata_completeness(release: Mapping[str, Any]) -> dict[str, Any]:
    missing: list[str] = []
    required_text_fields = (
        "schema_version",
        "module_id",
        "version",
        "image_ref",
        "image_digest",
        "required_plan",
        "required_core",
        "rollout_channel",
    )
    for field_name in required_text_fields:
        if not str(release.get(field_name) or "").strip():
            missing.append(field_name)
    for digest_field in ("image_digest", "manifest_digest"):
        value = str(release.get(digest_field) or "").strip()
        if not value:
            missing.append(digest_field)
            continue
        try:
            validate_digest(value, digest_field)
        except ModuleHardeningError:
            missing.append(digest_field)
    compatibility = release.get("compatibility")
    if not isinstance(compatibility, MappingABC) or not str(compatibility.get("requirement") or "").strip():
        missing.append("compatibility")
    changelog = release.get("changelog")
    if not isinstance(changelog, SequenceABC) or isinstance(changelog, (str, bytes)) or not [item for item in changelog if str(item).strip()]:
        missing.append("changelog")
    signature = str(release.get("signature") or "").strip()
    if not signature:
        missing.append("signature")
    return {
        "schema_version": MODULE_RELEASE_COMPLETENESS_SCHEMA_VERSION,
        "complete": not missing,
        "missing_fields": sorted(dict.fromkeys(missing)),
        "required_fields": [
            *required_text_fields,
            "manifest_digest",
            "signature",
            "compatibility",
            "changelog",
        ],
    }


def core_compatibility_range(requirement: str) -> dict[str, Any]:
    clean_requirement = _clean_required_text(requirement, "required_core")
    min_version: str | None = None
    max_version: str | None = None
    min_inclusive: bool | None = None
    max_inclusive: bool | None = None
    exact_version: str | None = None
    clauses = [part.strip() for part in clean_requirement.split(",") if part.strip()]
    if not clauses:
        raise ModuleHardeningError("required_core_required")
    for clause in clauses:
        op = "=="
        raw_expected = clause
        for candidate in (">=", "<=", ">", "<", "=="):
            if clause.startswith(candidate):
                op = candidate
                raw_expected = clause[len(candidate) :].strip()
                break
        normalized = _format_version(_parse_version(raw_expected))
        if op == "==":
            exact_version = normalized
            min_version = normalized
            max_version = normalized
            min_inclusive = True
            max_inclusive = True
        elif op in {">=", ">"}:
            min_version = normalized
            min_inclusive = op == ">="
        elif op in {"<=", "<"}:
            max_version = normalized
            max_inclusive = op == "<="
    return {
        "target": "agvm_core",
        "requirement": clean_requirement,
        "min_version": min_version,
        "min_inclusive": min_inclusive,
        "max_version": max_version,
        "max_inclusive": max_inclusive,
        "exact_version": exact_version,
    }


def assert_core_release_compatible(required_core: str, *, core_version: str | None = None) -> str:
    clean_core_version = current_core_version(core_version)
    clean_required_core = _clean_required_text(required_core, "required_core")
    core_compatibility_range(clean_required_core)
    if not version_satisfies(clean_core_version, clean_required_core):
        raise ModuleHardeningError("module_release_core_incompatible")
    return clean_core_version


def validate_module_grant(
    grant: Mapping[str, Any],
    *,
    module_id: str | None = None,
    core_version: str | None = None,
    expected_image_digest: str | None = None,
    expected_manifest_digest: str | None = None,
) -> ModuleGrantValidation:
    if grant.get("schema_version") != MODULE_GRANT_SCHEMA_VERSION:
        raise ModuleHardeningError("module_grant_schema_invalid")
    clean_module_id = _clean_required_text(grant.get("module_id"), "module_id")
    if module_id and clean_module_id != module_id:
        raise ModuleHardeningError("module_grant_module_id_mismatch")
    clean_version = _clean_required_text(grant.get("version"), "version")
    image_digest = validate_digest(str(grant.get("image_digest") or ""), "image_digest")
    manifest_digest = validate_digest(str(grant.get("manifest_digest") or ""), "manifest_digest")
    if expected_image_digest and image_digest != validate_digest(expected_image_digest, "expected_image_digest"):
        raise ModuleHardeningError("module_image_digest_mismatch")
    if expected_manifest_digest and manifest_digest != validate_digest(expected_manifest_digest, "expected_manifest_digest"):
        raise ModuleHardeningError("module_manifest_digest_mismatch")
    signature = _clean_required_text(grant.get("manifest_signature"), "manifest_signature")
    if not (signature.startswith(f"{SIGNATURE_ALGORITHM}:") or signature.startswith("unsigned:")):
        raise ModuleHardeningError("module_manifest_signature_algorithm_invalid")
    required_core = _clean_required_text(grant.get("required_core"), "required_core")
    clean_core_version = current_core_version(core_version)
    if not version_satisfies(clean_core_version, required_core):
        raise ModuleHardeningError("module_core_version_incompatible")
    return ModuleGrantValidation(
        module_id=clean_module_id,
        version=clean_version,
        image_digest=image_digest,
        manifest_digest=manifest_digest,
        required_core=required_core,
        rollout_channel=_clean_required_text(grant.get("rollout_channel") or DEFAULT_ROLLOUT_CHANNEL, "rollout_channel"),
    )


def validate_module_grants(
    value: Any,
    *,
    module_ids: list[str] | None = None,
    core_version: str | None = None,
) -> dict[str, dict[str, Any]]:
    if value in (None, ""):
        return {}
    if not isinstance(value, Mapping):
        raise ModuleHardeningError("module_grants_must_be_object")
    result: dict[str, dict[str, Any]] = {}
    for raw_module_id, raw_grant in value.items():
        clean_module_id = _clean_required_text(raw_module_id, "module_id")
        if not isinstance(raw_grant, Mapping):
            raise ModuleHardeningError("module_grant_must_be_object")
        validate_module_grant(raw_grant, module_id=clean_module_id, core_version=core_version)
        result[clean_module_id] = dict(raw_grant)
    if module_ids:
        missing = sorted(set(module_ids) - set(result))
        if missing:
            raise ModuleHardeningError(f"module_grants_missing:{','.join(missing)}")
    return result


def validate_digest(value: str, name: str = "digest") -> str:
    text = _clean_required_text(value, name).lower()
    if not text.startswith("sha256:"):
        raise ModuleHardeningError(f"{name}_must_start_with_sha256")
    digest = text.removeprefix("sha256:")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ModuleHardeningError(f"{name}_invalid")
    return f"sha256:{digest}"


def version_satisfies(version: str, requirement: str) -> bool:
    clean_version = _parse_version(version)
    for clause in (part.strip() for part in str(requirement or "").split(",") if part.strip()):
        op = "=="
        raw_expected = clause
        for candidate in (">=", "<=", ">", "<", "=="):
            if clause.startswith(candidate):
                op = candidate
                raw_expected = clause[len(candidate) :].strip()
                break
        expected = _parse_version(raw_expected)
        if op == ">=" and not (clean_version >= expected):
            return False
        if op == "<=" and not (clean_version <= expected):
            return False
        if op == ">" and not (clean_version > expected):
            return False
        if op == "<" and not (clean_version < expected):
            return False
        if op == "==" and not (clean_version == expected):
            return False
    return True


def _parse_version(value: str) -> tuple[int, int, int]:
    parts = str(value or "").strip().split(".")
    if not parts or len(parts) > 3:
        raise ModuleHardeningError("version_invalid")
    parsed: list[int] = []
    for part in parts:
        digits = "".join(char for char in part if char.isdigit())
        if not digits:
            raise ModuleHardeningError("version_invalid")
        parsed.append(int(digits))
    while len(parsed) < 3:
        parsed.append(0)
    return tuple(parsed[:3])  # type: ignore[return-value]


def _format_version(value: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in value)


def _clean_changelog(value: Sequence[str] | str | None) -> list[str]:
    if value is None:
        raw_items: Sequence[str] = DEFAULT_RELEASE_CHANGELOG
    elif isinstance(value, str):
        raw_items = [value]
    else:
        raw_items = value
    cleaned: list[str] = []
    for index, item in enumerate(raw_items):
        text = _clean_required_text(item, f"changelog[{index}]")
        if text not in cleaned:
            cleaned.append(text)
    if not cleaned:
        raise ModuleHardeningError("changelog_required")
    return cleaned


def _clean_required_text(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ModuleHardeningError(f"{name}_required")
    if "\n" in text or "\r" in text:
        raise ModuleHardeningError(f"{name}_must_be_single_line")
    return text


def _clean_optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if "\n" in text or "\r" in text:
        raise ModuleHardeningError("optional_text_must_be_single_line")
    return text
