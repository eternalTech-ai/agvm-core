# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any, Mapping, Sequence

try:
    from .config import FACET_FIELDS, ROUTING_FIELDS
except ImportError:  # pragma: no cover - top-level runtime imports
    from config import FACET_FIELDS, ROUTING_FIELDS


AI_EXECUTION_ATTESTATION_SCHEMA_VERSION = "agvm.ai_execution_attestation.v2"
AI_EXECUTION_ATTESTATION_LEGACY_SCHEMA_VERSION = "agvm.ai_execution_attestation.v1"
GROW_PREVIEW_SCHEMA_VERSION = "agvm.grow_preview_bundle.v2"

_NON_REAL_AI_IDENTITY_MARKERS = frozenset(
    {
        "deterministic",
        "fake",
        "fallback",
        "heuristic",
        "mock",
        "none",
        "stub",
        "test",
    }
)


class AiModuleContractError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalized_sha256(value: Any, *, field: str) -> str:
    digest = str(value or "").strip().lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise AiModuleContractError(f"ai_execution_{field}_invalid")
    return digest


def _require_real_ai_identity(value: Any, *, field: str) -> str:
    identity = str(value or "").strip()
    if not identity:
        raise AiModuleContractError(f"ai_execution_{field}_missing")
    markers = {
        marker
        for marker in re.split(r"[^a-z0-9]+", identity.casefold())
        if marker
    }
    if markers & _NON_REAL_AI_IDENTITY_MARKERS:
        raise AiModuleContractError(f"ai_execution_{field}_invalid")
    return identity


def _read_legacy_ai_execution_attestation(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a persisted V1 record without granting mutation authority."""

    normalized = dict(payload)
    normalized["schema_version"] = AI_EXECUTION_ATTESTATION_LEGACY_SCHEMA_VERSION
    normalized["legacy_read_only"] = True
    normalized["provider_executed"] = False
    normalized["applicable"] = False
    return normalized


def validate_ai_execution_attestation(
    value: Mapping[str, Any] | None,
    *,
    expected_request_sha256: str | None = None,
    expected_output_sha256: str | None = None,
    allow_legacy_read: bool = False,
) -> dict[str, Any]:
    payload = dict(value or {})
    schema_version = str(payload.get("schema_version") or "").strip()
    if schema_version == AI_EXECUTION_ATTESTATION_LEGACY_SCHEMA_VERSION:
        if allow_legacy_read:
            return _read_legacy_ai_execution_attestation(payload)
        raise AiModuleContractError("ai_execution_attestation_legacy_not_applicable")
    if schema_version != AI_EXECUTION_ATTESTATION_SCHEMA_VERSION:
        raise AiModuleContractError("ai_execution_attestation_schema_invalid")
    if payload.get("status") != "completed":
        raise AiModuleContractError("ai_execution_not_completed")
    if payload.get("provider_executed") is not True:
        raise AiModuleContractError("ai_execution_provider_not_executed")
    payload["provider"] = _require_real_ai_identity(payload.get("provider"), field="provider")
    payload["model"] = _require_real_ai_identity(payload.get("model"), field="model")
    payload["request_sha256"] = _normalized_sha256(
        payload.get("request_sha256"),
        field="request_sha256",
    )
    payload["output_sha256"] = _normalized_sha256(
        payload.get("output_sha256"),
        field="output_sha256",
    )
    for field, alias in (
        ("request_sha256", "request_digest"),
        ("output_sha256", "output_digest"),
    ):
        if alias not in payload:
            continue
        alias_digest = _normalized_sha256(payload.get(alias), field=alias)
        if not hmac.compare_digest(payload[field], alias_digest):
            raise AiModuleContractError(f"ai_execution_{field}_mismatch")
    for field, expected in (
        ("request_sha256", expected_request_sha256),
        ("output_sha256", expected_output_sha256),
    ):
        if expected is None:
            continue
        expected_digest = _normalized_sha256(expected, field=f"expected_{field}")
        if not hmac.compare_digest(payload[field], expected_digest):
            raise AiModuleContractError(f"ai_execution_{field}_mismatch")
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        raise AiModuleContractError("ai_execution_usage_missing")
    normalized_usage: dict[str, int] = {}
    for field in ("input_tokens", "output_tokens", "reasoning_tokens", "total_tokens"):
        try:
            normalized_usage[field] = max(0, int(usage.get(field) or 0))
        except (TypeError, ValueError) as exc:
            raise AiModuleContractError("ai_execution_usage_invalid") from exc
    return {
        **payload,
        "applicable": True,
        "legacy_read_only": False,
        "usage": normalized_usage,
    }


def _validate_unit_vector(value: Any, fields: Sequence[str], *, code: str) -> dict[str, float]:
    if not isinstance(value, Mapping) or any(field not in value for field in fields):
        raise AiModuleContractError(code)
    normalized: dict[str, float] = {}
    for field in fields:
        try:
            number = float(value[field])
        except (TypeError, ValueError) as exc:
            raise AiModuleContractError(code) from exc
        if number < 0.0 or number > 1.0:
            raise AiModuleContractError(code)
        normalized[field] = number
    return normalized


def validate_grow_compiler_payload(value: Mapping[str, Any] | None) -> dict[str, Any]:
    payload = dict(value or {})
    primary = payload.get("primary_node")
    if not isinstance(primary, Mapping):
        raise AiModuleContractError("grow_ai_primary_node_missing")
    primary_record = dict(primary)
    if not str(primary_record.get("summary") or "").strip():
        raise AiModuleContractError("grow_ai_primary_summary_missing")
    primary_record["routing_semantic_scores"] = _validate_unit_vector(
        primary_record.get("routing_semantic_scores"),
        ROUTING_FIELDS,
        code="grow_ai_routing_scores_invalid",
    )
    primary_record["routing_facets"] = _validate_unit_vector(
        primary_record.get("routing_facets"),
        FACET_FIELDS,
        code="grow_ai_routing_facets_invalid",
    )
    derived: list[dict[str, Any]] = []
    for item in list(payload.get("derived_nodes") or []):
        if not isinstance(item, Mapping):
            raise AiModuleContractError("grow_ai_derived_node_invalid")
        record = dict(item)
        if not str(record.get("raw_text") or record.get("summary") or "").strip():
            raise AiModuleContractError("grow_ai_derived_content_missing")
        record["routing_semantic_scores"] = _validate_unit_vector(
            record.get("routing_semantic_scores"),
            ROUTING_FIELDS,
            code="grow_ai_routing_scores_invalid",
        )
        record["routing_facets"] = _validate_unit_vector(
            record.get("routing_facets"),
            FACET_FIELDS,
            code="grow_ai_routing_facets_invalid",
        )
        derived.append(record)
    return {**payload, "primary_node": primary_record, "derived_nodes": derived}


def dynamic_clarification_questions(
    compiler_payload: Mapping[str, Any],
    *,
    answers: Mapping[str, Any] | None = None,
    limit: int = 12,
) -> list[str]:
    plan = compiler_payload.get("cognitive_write_plan")
    plan = dict(plan) if isinstance(plan, Mapping) else {}
    raw_questions = plan.get("clarification_questions")
    questions = raw_questions if isinstance(raw_questions, list) else []
    answered_text = {
        str(key).strip().casefold()
        for key, answer in dict(answers or {}).items()
        if str(answer or "").strip()
    }
    result: list[str] = []
    for raw in questions:
        question = " ".join(str(raw or "").split()).strip()
        if not question or question.casefold() in answered_text or question in result:
            continue
        result.append(question[:600])
        if len(result) >= max(1, min(int(limit or 12), 24)):
            break
    return result


__all__ = [
    "AI_EXECUTION_ATTESTATION_LEGACY_SCHEMA_VERSION",
    "AI_EXECUTION_ATTESTATION_SCHEMA_VERSION",
    "GROW_PREVIEW_SCHEMA_VERSION",
    "AiModuleContractError",
    "canonical_sha256",
    "dynamic_clarification_questions",
    "validate_ai_execution_attestation",
    "validate_grow_compiler_payload",
]
