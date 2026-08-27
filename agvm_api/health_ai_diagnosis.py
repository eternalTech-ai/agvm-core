# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import os
import time
from typing import Any, Callable


AI_EXECUTION_ATTESTATION_SCHEMA_VERSION = "agvm.ai_execution_attestation.v2"
_NON_PROVIDER_ATTESTATION_NAMES = {"heuristic", "fallback", "deterministic", "none", "mock"}
HEALTH_AI_DIAGNOSIS_SCHEMA_VERSION = "agvm.health_ai_diagnosis.v2"
HEALTH_AI_REQUEST_SCHEMA_VERSION = "agvm.health_ai_readonly_request.v2"

_ALLOWED_ACTIONS = frozenset({"grow", "sleep", "evolve", "calibrate_brain", "none"})
_ACTION_ALIASES = {
    "grow_repair": "grow",
    "grow_source_preview": "grow",
    "sleep_preview": "sleep",
    "evolve_preview": "evolve",
    "matrix_calibration_preview": "calibrate_brain",
    "calibrate": "calibrate_brain",
}
_MUTATION_KEYS = frozenset(
    {
        "apply",
        "apply_payload",
        "delete",
        "insert",
        "mutation",
        "mutations",
        "patch",
        "persist",
        "sql",
        "update",
        "write",
        "write_payload",
    }
)

_HEALTH_AI_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": sorted(_ALLOWED_ACTIONS)},
                    "summary": {"type": "string"},
                    "rationale": {"type": "string"},
                    "evidence_refs": {"type": "array", "items": {"type": "string"}},
                },
                "additionalProperties": False,
            },
        },
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
    "additionalProperties": False,
}

_HEALTH_AI_SYSTEM_PROMPT = """You are the read-only diagnosis layer for Detwin Brain Health.
The deterministic whole-brain report is authoritative. Interpret only the supplied checks,
alerts and feedback ledger. Every recommendation must cite exact values from
evidence_ref_catalog. Never propose or emit a mutation, patch, write, SQL statement or hidden
operation. Return concise operator-facing recommendations; do not expose chain-of-thought.
If no action is justified, return one recommendation with action 'none'."""


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _base_attestation(*, status: str, input_sha256: str, failure_reason: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": AI_EXECUTION_ATTESTATION_SCHEMA_VERSION,
        "operation": "brain_health_readonly_diagnosis",
        "status": status,
        "provider_execution_verified": False,
        "provider": None,
        "model": None,
        "provider_request_id": None,
        "started_at": None,
        "completed_at": None,
        "latency_ms": None,
        "input_sha256": input_sha256,
        "output_sha256": None,
        "read_only": True,
        "mutation_attempted": False,
        "mutation_applied": False,
        "failure_reason": failure_reason,
    }


def _evidence_catalog(health: dict[str, Any], ledger: dict[str, Any]) -> set[str]:
    refs = {f"reason:{value}" for value in _as_list(health.get("reason_codes")) if str(value).strip()}
    refs.update(f"check:{key}" for key in _as_dict(health.get("checks")))
    for alert in _as_list(health.get("health_alerts")):
        row = _as_dict(alert)
        alert_id = str(row.get("alert_id") or row.get("id") or "").strip()
        if alert_id:
            refs.add(f"alert:{alert_id}")
    for signal in _as_list(ledger.get("signals")):
        row = _as_dict(signal)
        signal_id = str(row.get("signal_id") or "").strip()
        if signal_id:
            refs.add(f"feedback:{signal_id}")
        search_id = str(row.get("search_id") or "").strip()
        if search_id:
            refs.add(f"search:{search_id}")
        for evidence in _as_list(row.get("evidence_refs")):
            evidence_row = _as_dict(evidence)
            kind = str(evidence_row.get("kind") or "evidence").strip()
            identifier = str(evidence_row.get("id") or "").strip()
            if identifier:
                refs.add(f"{kind}:{identifier}")
    return refs


def _contains_mutation_payload(value: Any) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).strip().lower() in _MUTATION_KEYS:
                return True
            if _contains_mutation_payload(nested):
                return True
    elif isinstance(value, list):
        return any(_contains_mutation_payload(item) for item in value)
    return False


def _normalized_diagnosis(raw: Any, *, evidence_catalog: set[str]) -> tuple[dict[str, Any] | None, str | None]:
    diagnosis = _as_dict(_as_dict(raw).get("diagnosis") or raw)
    if _contains_mutation_payload(diagnosis):
        return None, "mutation_payload_rejected"
    summary = str(diagnosis.get("summary") or "").strip()
    if not summary:
        return None, "diagnosis_summary_missing"
    recommendations: list[dict[str, Any]] = []
    for raw_recommendation in _as_list(diagnosis.get("recommendations")):
        recommendation = _as_dict(raw_recommendation)
        raw_action = str(recommendation.get("action") or "").strip().lower()
        action = _ACTION_ALIASES.get(raw_action, raw_action)
        if action not in _ALLOWED_ACTIONS:
            return None, f"unsupported_recommendation:{action or 'missing'}"
        evidence_refs = [str(value).strip() for value in _as_list(recommendation.get("evidence_refs")) if str(value).strip()]
        if not evidence_refs:
            return None, "recommendation_evidence_refs_missing"
        unknown_refs = [value for value in evidence_refs if value not in evidence_catalog]
        if unknown_refs:
            return None, f"unknown_evidence_ref:{unknown_refs[0]}"
        recommendation_summary = str(recommendation.get("summary") or recommendation.get("rationale") or "").strip()
        if not recommendation_summary:
            return None, "recommendation_summary_missing"
        recommendations.append(
            {
                "action": action,
                "summary": recommendation_summary[:1000],
                "rationale": str(recommendation.get("rationale") or recommendation_summary).strip()[:2000],
                "evidence_refs": list(dict.fromkeys(evidence_refs))[:64],
            }
        )
    if not recommendations:
        return None, "diagnosis_recommendations_missing"
    combined_refs = [str(value).strip() for value in _as_list(diagnosis.get("evidence_refs")) if str(value).strip()]
    unknown_combined_refs = [value for value in combined_refs if value not in evidence_catalog]
    if unknown_combined_refs:
        return None, f"unknown_evidence_ref:{unknown_combined_refs[0]}"
    combined_refs.extend(ref for recommendation in recommendations for ref in recommendation["evidence_refs"])
    try:
        confidence = max(0.0, min(1.0, float(diagnosis.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        return None, "diagnosis_confidence_invalid"
    return (
        {
            "summary": summary[:3000],
            "recommendations": recommendations,
            "evidence_refs": list(dict.fromkeys(combined_refs))[:128],
            "confidence": confidence,
        },
        None,
    )


def _provider_metadata(raw: dict[str, Any]) -> dict[str, Any]:
    attestation = _as_dict(raw.get("attestation"))
    return {
        "provider": str(raw.get("provider") or attestation.get("provider") or "").strip(),
        "model": str(raw.get("model") or attestation.get("model") or "").strip(),
        "provider_request_id": str(
            raw.get("provider_request_id")
            or raw.get("request_id")
            or attestation.get("provider_request_id")
            or attestation.get("request_id")
            or ""
        ).strip(),
        "status": str(raw.get("status") or attestation.get("status") or "").strip().lower(),
        "started_at": str(raw.get("started_at") or attestation.get("started_at") or "").strip() or None,
        "completed_at": str(raw.get("completed_at") or attestation.get("completed_at") or "").strip() or None,
    }


def _request_payload(deterministic_health: dict[str, Any], feedback_ledger: dict[str, Any]) -> dict[str, Any]:
    evidence_ref_catalog = sorted(_evidence_catalog(deterministic_health, feedback_ledger))
    return {
        "schema_version": HEALTH_AI_REQUEST_SCHEMA_VERSION,
        "operation": "brain_health_readonly_diagnosis",
        "deterministic_health": {
            "brain_id": deterministic_health.get("brain_id"),
            "generated_at": deterministic_health.get("generated_at"),
            "readiness": deterministic_health.get("readiness"),
            "recommendation": deterministic_health.get("recommendation"),
            "reason_codes": list(deterministic_health.get("reason_codes") or []),
            "overall_score": deterministic_health.get("overall_score"),
            "summary": copy.deepcopy(_as_dict(deterministic_health.get("summary"))),
            "checks": copy.deepcopy(_as_dict(deterministic_health.get("checks"))),
            "health_alerts": copy.deepcopy(_as_list(deterministic_health.get("health_alerts"))),
        },
        "feedback_ledger": copy.deepcopy(feedback_ledger),
        "evidence_ref_catalog": evidence_ref_catalog,
        "response_contract": {
            "read_only": True,
            "allowed_actions": sorted(_ALLOWED_ACTIONS),
            "evidence_refs_required": True,
            "provider_metadata_required": ["provider", "model", "provider_request_id", "status"],
            "mutation_payloads_allowed": False,
            "chain_of_thought_requested": False,
        },
    }


def create_health_ai_provider_diagnoser(
    *,
    structured_json_fn: Callable[..., tuple[dict[str, Any] | None, str | None]],
    model: str,
    timeout: float = 45.0,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Adapt the configured provider to the strict, read-only Health diagnosis contract."""

    def diagnose(request: dict[str, Any]) -> dict[str, Any]:
        execution_metadata: dict[str, Any] = {}
        diagnosis, error = structured_json_fn(
            system_prompt=_HEALTH_AI_SYSTEM_PROMPT,
            user_prompt=json.dumps(request, ensure_ascii=False, sort_keys=True, default=str),
            schema_name="agvm_health_readonly_diagnosis",
            schema=_HEALTH_AI_RESPONSE_SCHEMA,
            model=model,
            timeout=timeout,
            role="compiler",
            max_output_tokens=1800,
            execution_metadata=execution_metadata,
        )
        if error or not isinstance(diagnosis, dict):
            raise RuntimeError(str(error or "health_ai_diagnosis_missing"))
        return {
            "provider": str(execution_metadata.get("provider") or ""),
            "model": str(execution_metadata.get("model") or ""),
            "provider_request_id": str(execution_metadata.get("response_id") or ""),
            "status": str(execution_metadata.get("status") or ""),
            "started_at": execution_metadata.get("started_at"),
            "completed_at": execution_metadata.get("completed_at"),
            "usage": copy.deepcopy(_as_dict(execution_metadata.get("usage"))),
            "request_sha256": execution_metadata.get("request_sha256"),
            "output_sha256": execution_metadata.get("output_sha256"),
            "diagnosis": diagnosis,
        }

    return diagnose


def runtime_health_ai_diagnoser() -> Callable[[dict[str, Any]], dict[str, Any]] | None:
    """Return a provider-backed diagnoser only when request-scoped provider auth exists."""

    try:
        from hosted_credential_context import resolved_openai_api_key
        from llm import compiler_model, llm_enabled, structured_json
    except ImportError:
        return None
    if not llm_enabled() or not resolved_openai_api_key():
        return None
    model = str(os.getenv("AGVM_HEALTH_AI_MODEL") or "").strip() or compiler_model()
    return create_health_ai_provider_diagnoser(
        structured_json_fn=structured_json,
        model=model,
    )


def build_health_ai_readonly_diagnosis(
    *,
    deterministic_health: dict[str, Any],
    feedback_ledger: dict[str, Any],
    diagnoser: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    request = _request_payload(deterministic_health, feedback_ledger)
    input_sha256 = _canonical_digest(request)
    base = {
        "schema_version": HEALTH_AI_DIAGNOSIS_SCHEMA_VERSION,
        "status": "unavailable",
        "authoritative": False,
        "deterministic_health_overridden": False,
        "diagnosis": None,
        "attestation": _base_attestation(status="unavailable", input_sha256=input_sha256, failure_reason="diagnoser_not_configured"),
        "safety_contract": {
            "read_only": True,
            "mutation_allowed": False,
            "deterministic_health_is_authoritative": True,
            "chain_of_thought_persisted": False,
        },
    }
    if diagnoser is None:
        return base

    started_at = _now()
    started = time.perf_counter()
    try:
        raw = diagnoser(copy.deepcopy(request))
    except Exception as exc:  # noqa: BLE001
        failed = copy.deepcopy(base)
        failed["status"] = "failed"
        failed["attestation"] = {
            **_base_attestation(status="failed", input_sha256=input_sha256, failure_reason="provider_execution_failed"),
            "started_at": started_at,
            "completed_at": _now(),
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "error_type": type(exc).__name__,
        }
        return failed

    raw_dict = _as_dict(raw)
    provider = _provider_metadata(raw_dict)
    missing_provider_fields = [key for key in ("provider", "model", "provider_request_id", "status") if not provider.get(key)]
    non_provider_attestation = provider.get("provider", "").lower() in _NON_PROVIDER_ATTESTATION_NAMES
    if missing_provider_fields or non_provider_attestation or provider["status"] not in {"completed", "ok", "succeeded"}:
        rejected = copy.deepcopy(base)
        rejected["status"] = "rejected"
        if missing_provider_fields:
            reason = f"provider_attestation_missing:{','.join(missing_provider_fields)}"
        elif non_provider_attestation:
            reason = "provider_attestation_not_ai_provider"
        else:
            reason = f"provider_execution_not_completed:{provider['status']}"
        rejected["attestation"] = {
            **_base_attestation(status="rejected", input_sha256=input_sha256, failure_reason=reason),
            "provider": provider.get("provider") or None,
            "model": provider.get("model") or None,
            "provider_request_id": provider.get("provider_request_id") or None,
            "started_at": provider.get("started_at") or started_at,
            "completed_at": provider.get("completed_at") or _now(),
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
        }
        return rejected

    diagnosis, validation_error = _normalized_diagnosis(raw_dict, evidence_catalog=_evidence_catalog(deterministic_health, feedback_ledger))
    if validation_error or diagnosis is None:
        rejected = copy.deepcopy(base)
        rejected["status"] = "rejected"
        rejected["attestation"] = {
            **_base_attestation(status="rejected", input_sha256=input_sha256, failure_reason=validation_error),
            "provider": provider["provider"],
            "model": provider["model"],
            "provider_request_id": provider["provider_request_id"],
            "started_at": provider.get("started_at") or started_at,
            "completed_at": provider.get("completed_at") or _now(),
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "mutation_attempted": validation_error == "mutation_payload_rejected",
        }
        return rejected

    completed_at = provider.get("completed_at") or _now()
    return {
        **base,
        "status": "ready",
        "diagnosis": diagnosis,
        "attestation": {
            **_base_attestation(status="completed", input_sha256=input_sha256),
            "provider_execution_verified": True,
            "provider": provider["provider"],
            "model": provider["model"],
            "provider_request_id": provider["provider_request_id"],
            "started_at": provider.get("started_at") or started_at,
            "completed_at": completed_at,
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "output_sha256": _canonical_digest(diagnosis),
            "failure_reason": None,
        },
    }


__all__ = [
    "AI_EXECUTION_ATTESTATION_SCHEMA_VERSION",
    "HEALTH_AI_DIAGNOSIS_SCHEMA_VERSION",
    "HEALTH_AI_REQUEST_SCHEMA_VERSION",
    "build_health_ai_readonly_diagnosis",
    "create_health_ai_provider_diagnoser",
    "runtime_health_ai_diagnoser",
]
