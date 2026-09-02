# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: Apache-2.0

"""Detwin release-bundle provenance contract.

The contract deliberately contains no runtime or framework dependencies so the
same validation can be used by Core, Platform, Hosted MCP and release tooling.
Release mode is enabled by any ``AGVM_RELEASE_*`` setting.  Once enabled, the
contract fails closed unless the local image revision, the expected source SHA,
the bundle id and the complete common manifest agree.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any


RELEASE_BUNDLE_MANIFEST_SCHEMA_VERSION = "detwin.release_bundle_manifest.v1"
RUNTIME_RELEASE_BUNDLE_STATUS_SCHEMA_VERSION = (
    "detwin.runtime_release_bundle_status.v1"
)
REQUIRED_RELEASE_COMPONENTS = (
    "platform",
    "core_api",
    "core_ui",
    "hosted_mcp",
)
RELEASE_BUNDLE_MANIFEST_ENV = "AGVM_RELEASE_BUNDLE_MANIFEST"
RELEASE_BUNDLE_MANIFEST_JSON_ENV = "AGVM_RELEASE_BUNDLE_MANIFEST_JSON"
RELEASE_BUNDLE_MANIFEST_PATH_ENV = "AGVM_RELEASE_BUNDLE_MANIFEST_PATH"
_GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_BUNDLE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


class ReleaseBundleContractError(ValueError):
    """A stable, non-secret release bundle validation failure."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail or code


def build_release_bundle_manifest(
    *,
    source_sha: str,
    bundle_id: str | None = None,
    components: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build and validate the common four-image release manifest."""

    normalized_sha = _source_sha(source_sha, "source_sha")
    component_payload = components or {
        component: {"source_sha": normalized_sha}
        for component in REQUIRED_RELEASE_COMPONENTS
    }
    return normalize_release_bundle_manifest(
        {
            "schema_version": RELEASE_BUNDLE_MANIFEST_SCHEMA_VERSION,
            "bundle_id": bundle_id or f"detwin-{normalized_sha}",
            "source_sha": normalized_sha,
            "components": dict(component_payload),
        }
    )


def normalize_release_bundle_manifest(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the canonical v1 manifest or raise a stable contract error."""

    if not isinstance(payload, Mapping):
        raise ReleaseBundleContractError("release_bundle_manifest_invalid")
    if payload.get("schema_version") != RELEASE_BUNDLE_MANIFEST_SCHEMA_VERSION:
        raise ReleaseBundleContractError("release_bundle_manifest_schema_invalid")

    source_sha = _source_sha(payload.get("source_sha"), "source_sha")
    bundle_id = str(payload.get("bundle_id") or "").strip()
    if not _BUNDLE_ID_PATTERN.fullmatch(bundle_id):
        raise ReleaseBundleContractError("release_bundle_id_invalid")

    raw_components = payload.get("components")
    if not isinstance(raw_components, Mapping):
        raise ReleaseBundleContractError("release_bundle_components_invalid")
    if any(not isinstance(name, str) for name in raw_components):
        raise ReleaseBundleContractError("release_bundle_components_invalid")
    actual_names = set(raw_components)
    required_names = set(REQUIRED_RELEASE_COMPONENTS)
    if actual_names != required_names:
        missing = ",".join(sorted(required_names - actual_names))
        unexpected = ",".join(sorted(actual_names - required_names))
        detail = f"missing={missing};unexpected={unexpected}"
        raise ReleaseBundleContractError("release_bundle_components_incomplete", detail)

    normalized_components: dict[str, dict[str, Any]] = {}
    for component in REQUIRED_RELEASE_COMPONENTS:
        raw_component = raw_components.get(component)
        if not isinstance(raw_component, Mapping):
            raise ReleaseBundleContractError(
                "release_bundle_component_invalid", component
            )
        component_sha = _source_sha(
            raw_component.get("source_sha"), f"components.{component}.source_sha"
        )
        if component_sha != source_sha:
            raise ReleaseBundleContractError(
                "release_bundle_component_source_sha_mismatch", component
            )
        normalized = {"source_sha": component_sha}
        image_digest = str(raw_component.get("image_digest") or "").strip().lower()
        if image_digest:
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest):
                raise ReleaseBundleContractError(
                    "release_bundle_component_digest_invalid", component
                )
            normalized["image_digest"] = image_digest
        normalized_components[component] = normalized

    return {
        "schema_version": RELEASE_BUNDLE_MANIFEST_SCHEMA_VERSION,
        "bundle_id": bundle_id,
        "source_sha": source_sha,
        "components": normalized_components,
    }


def release_bundle_manifest_sha256(payload: Mapping[str, Any]) -> str:
    """Digest the canonical manifest for journal/evidence correlation."""

    normalized = normalize_release_bundle_manifest(payload)
    encoded = json.dumps(
        normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def runtime_release_bundle_status(
    *,
    component: str,
    environ: Mapping[str, str] | None = None,
    manifest: Mapping[str, Any] | None = None,
    observed_component_revisions: Mapping[str, str | None] | None = None,
) -> dict[str, Any]:
    """Validate runtime provenance and return a health-safe status payload.

    ``observed_component_revisions`` lets a process validate artifacts packaged
    inside its image (for example Platform can validate the embedded Core UI).
    """

    if component not in REQUIRED_RELEASE_COMPONENTS:
        raise ReleaseBundleContractError("release_bundle_component_unknown", component)
    active_env = os.environ if environ is None else environ
    image_revision = str(active_env.get("AGVM_IMAGE_REVISION") or "").strip().lower()
    expected_sha = str(active_env.get("AGVM_RELEASE_SOURCE_SHA") or "").strip().lower()
    expected_bundle_id = str(active_env.get("AGVM_RELEASE_BUNDLE_ID") or "").strip()
    release_configured = bool(
        expected_sha
        or expected_bundle_id
        or _manifest_setting_present(active_env)
        or manifest is not None
    )
    base = {
        "schema_version": RUNTIME_RELEASE_BUNDLE_STATUS_SCHEMA_VERSION,
        "component": component,
        "required_components": list(REQUIRED_RELEASE_COMPONENTS),
        "image_revision": image_revision,
        "source_sha": expected_sha,
        "bundle_id": expected_bundle_id,
        "manifest_sha256": "",
        "mismatches": [],
    }
    if not release_configured:
        return {
            **base,
            "ok": True,
            "status": "unconfigured",
            "code": "release_bundle_unconfigured",
        }

    mismatches: list[dict[str, str]] = []
    if not _is_source_sha(expected_sha):
        mismatches.append(_mismatch("source_sha", "full_git_sha", expected_sha))
    if not _is_source_sha(image_revision):
        mismatches.append(
            _mismatch("image_revision", expected_sha or "full_git_sha", image_revision)
        )
    elif _is_source_sha(expected_sha) and image_revision != expected_sha:
        mismatches.append(_mismatch("image_revision", expected_sha, image_revision))
    if not _BUNDLE_ID_PATTERN.fullmatch(expected_bundle_id):
        mismatches.append(_mismatch("bundle_id", "configured_bundle_id", expected_bundle_id))

    normalized_manifest: dict[str, Any] | None = None
    try:
        raw_manifest = manifest if manifest is not None else _load_manifest(active_env)
        normalized_manifest = normalize_release_bundle_manifest(raw_manifest)
    except ReleaseBundleContractError as exc:
        mismatches.append(
            _mismatch("manifest", RELEASE_BUNDLE_MANIFEST_SCHEMA_VERSION, exc.code)
        )

    if normalized_manifest is not None:
        manifest_sha = normalized_manifest["source_sha"]
        manifest_bundle_id = normalized_manifest["bundle_id"]
        if _is_source_sha(expected_sha) and manifest_sha != expected_sha:
            mismatches.append(_mismatch("manifest.source_sha", expected_sha, manifest_sha))
        if expected_bundle_id and manifest_bundle_id != expected_bundle_id:
            mismatches.append(
                _mismatch("manifest.bundle_id", expected_bundle_id, manifest_bundle_id)
            )
        component_sha = normalized_manifest["components"][component]["source_sha"]
        if _is_source_sha(image_revision) and component_sha != image_revision:
            mismatches.append(
                _mismatch(
                    f"manifest.components.{component}.source_sha",
                    image_revision,
                    component_sha,
                )
            )
        base["manifest_sha256"] = release_bundle_manifest_sha256(normalized_manifest)

        for observed_component, observed_revision in dict(
            observed_component_revisions or {}
        ).items():
            if observed_component not in REQUIRED_RELEASE_COMPONENTS:
                mismatches.append(
                    _mismatch(
                        f"observed.{observed_component}",
                        "known_release_component",
                        str(observed_revision or ""),
                    )
                )
                continue
            observed = str(observed_revision or "").strip().lower()
            declared = normalized_manifest["components"][observed_component][
                "source_sha"
            ]
            if observed != declared:
                mismatches.append(
                    _mismatch(f"observed.{observed_component}", declared, observed)
                )

    if mismatches:
        return {
            **base,
            "ok": False,
            "status": "mismatch",
            "code": "release_bundle_mismatch",
            "mismatches": mismatches,
        }
    return {
        **base,
        "ok": True,
        "status": "verified",
        "code": "release_bundle_verified",
    }


def _load_manifest(environ: Mapping[str, str]) -> Mapping[str, Any]:
    manifest_json = str(environ.get(RELEASE_BUNDLE_MANIFEST_JSON_ENV) or "").strip()
    manifest_generic = str(environ.get(RELEASE_BUNDLE_MANIFEST_ENV) or "").strip()
    path_text = str(environ.get(RELEASE_BUNDLE_MANIFEST_PATH_ENV) or "").strip()
    if sum(bool(value) for value in (manifest_json, manifest_generic, path_text)) > 1:
        raise ReleaseBundleContractError("release_bundle_manifest_source_ambiguous")
    inline = manifest_json or manifest_generic
    if path_text:
        try:
            raw = Path(path_text).read_text(encoding="utf-8")
        except OSError as exc:
            raise ReleaseBundleContractError(
                "release_bundle_manifest_unavailable", type(exc).__name__
            ) from exc
    elif inline:
        raw = inline
    else:
        raise ReleaseBundleContractError("release_bundle_manifest_missing")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReleaseBundleContractError("release_bundle_manifest_json_invalid") from exc
    if not isinstance(payload, Mapping):
        raise ReleaseBundleContractError("release_bundle_manifest_invalid")
    return payload


def _manifest_setting_present(environ: Mapping[str, str]) -> bool:
    return any(
        str(environ.get(name) or "").strip()
        for name in (
            RELEASE_BUNDLE_MANIFEST_ENV,
            RELEASE_BUNDLE_MANIFEST_JSON_ENV,
            RELEASE_BUNDLE_MANIFEST_PATH_ENV,
        )
    )


def _source_sha(value: Any, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _is_source_sha(normalized):
        raise ReleaseBundleContractError("release_bundle_source_sha_invalid", field)
    return normalized


def _is_source_sha(value: str) -> bool:
    return bool(_GIT_SHA_PATTERN.fullmatch(str(value or "")))


def _mismatch(field: str, expected: str, actual: str) -> dict[str, str]:
    return {"field": field, "expected": expected, "actual": actual}
