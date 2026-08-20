from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from memory_learning import COGNITIVE_JOB_SCHEMA_VERSION, MEMORY_LEARNING_EVENT_SCHEMA_VERSION
from storage import utc_timestamp


COGNITIVE_JOB_POLICY_DECISION_SCHEMA_VERSION = "agvm.cognitive_job_policy_decision.v1"
COGNITIVE_JOB_CAPABILITY_SCHEMA_VERSION = "agvm.cognitive_job_capability.v1"

COGNITIVE_JOB_STATUSES: frozenset[str] = frozenset(
    {
        "queued",
        "blocked",
        "leased",
        "running",
        "completed",
        "failed",
        "cancelled",
    }
)

COGNITIVE_JOB_AUTOMATION_LEVELS: frozenset[str] = frozenset(
    {
        "manual",
        "foreground_followup",
        "local_background_observation",
        "local_operator_automation",
        "cloud_automatic",
    }
)

COGNITIVE_JOB_MUTATION_POLICIES: frozenset[str] = frozenset(
    {
        "non_mutating",
        "preview_only",
        "guarded_apply_required",
        "audited_policy_apply",
    }
)

_PLAN_RANK = {
    "free": 0,
    "pro": 1,
    "pro_plus": 2,
    "dev": 99,
}

_PLAN_ALIASES = {
    "": "free",
    "core": "free",
    "open_core": "free",
    "public_core": "free",
    "local": "free",
    "free": "free",
    "pro": "pro",
    "local_pro": "pro",
    "pro_local": "pro",
    "paid": "pro",
    "plus": "pro_plus",
    "proplus": "pro_plus",
    "pro_plus": "pro_plus",
    "cloud_plus": "pro_plus",
    "hosted_plus": "pro_plus",
    "dev": "dev",
    "development": "dev",
}

_PAID_MODULE_IDS = {
    "agvm_agent_chat",
    "agvm_advanced_cockpit",
    "agvm_bench_pro",
    "agvm_clone_app",
    "agvm_grow_studio",
    "agvm_maintain_studio",
}

_PAID_CAPABILITIES = {
    "grow_source_preview",
    "grow_source_apply",
    "sleep_preview",
    "evolve_preview",
    "matrix_calibration_preview",
    "matrix_calibration_apply",
    "deduction_mining",
    "memory_policy_preview",
}

_FULL_MAINTENANCE_CAPABILITIES = {
    "sleep_preview",
    "evolve_preview",
    "matrix_calibration_preview",
    "deduction_mining",
    "memory_policy_preview",
    "hot_context_refresh",
    "source_verification",
}

_CORE_SAFE_BACKGROUND_CAPABILITIES = {
    "post_retrieval_critic",
    "query_metacognitive_rollup",
    "brain_health_read",
}

_UNSAFE_MUTATION_POLICIES = {
    "hidden_apply",
    "unguarded_apply",
    "direct_apply",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _string_list(value: Any, *, limit: int = 64) -> list[str]:
    if value is None:
        values: list[Any] = []
    elif isinstance(value, (str, int, float)):
        values = [value]
    else:
        values = list(value or [])
    output: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = _text(item)
        if not text or text in seen:
            continue
        seen.add(text)
        output.append(text)
        if len(output) >= limit:
            break
    return output


def _dict_value(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _stable_digest(payload: Any, *, length: int = 16) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def normalize_plan(value: Any) -> str:
    key = _text(value).lower().replace("-", "_").replace(" ", "_")
    return _PLAN_ALIASES.get(key, key if key in _PLAN_RANK else "free")


def plan_allows(actual_plan: Any, required_plan: Any) -> bool:
    actual = normalize_plan(actual_plan)
    required = normalize_plan(required_plan)
    return _PLAN_RANK.get(actual, 0) >= _PLAN_RANK.get(required, 0)


def infer_required_plan(*, requested_capability: str, module_id: str | None, automation_level: str) -> str:
    if automation_level == "cloud_automatic":
        return "pro_plus"
    if _text(module_id) in _PAID_MODULE_IDS:
        return "pro"
    if requested_capability in _PAID_CAPABILITIES:
        return "pro"
    return "free"


def normalize_cognitive_job(job: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    payload = {**_dict_value(job), **dict(overrides)}
    requested_capability = _text(payload.get("requested_capability") or payload.get("job_kind"))
    if not requested_capability:
        raise ValueError("cognitive_job_requested_capability_required")

    automation_level = _text(payload.get("automation_level") or "manual")
    if automation_level not in COGNITIVE_JOB_AUTOMATION_LEVELS:
        automation_level = "manual"

    raw_mutation_policy = _text(payload.get("mutation_policy") or "non_mutating")
    mutation_policy = raw_mutation_policy if raw_mutation_policy in COGNITIVE_JOB_MUTATION_POLICIES else raw_mutation_policy
    module_id = _text(payload.get("module_id")) or None
    required_plan = normalize_plan(payload.get("required_plan") or infer_required_plan(
        requested_capability=requested_capability,
        module_id=module_id,
        automation_level=automation_level,
    ))
    priority = payload.get("priority")
    try:
        priority_value = float(priority)
    except (TypeError, ValueError):
        priority_value = 0.0

    approval_required = payload.get("approval_required")
    if approval_required is None:
        approval_required = mutation_policy in {"guarded_apply_required", "audited_policy_apply"}

    normalized = {
        "schema_version": COGNITIVE_JOB_SCHEMA_VERSION,
        "job_id": _text(payload.get("job_id")) or f"cognitive_job::{uuid.uuid4()}",
        "brain_id": _text(payload.get("brain_id")) or None,
        "trigger_source": _text(payload.get("trigger_source") or "manual"),
        "requested_capability": requested_capability,
        "required_plan": required_plan,
        "module_id": module_id,
        "automation_level": automation_level,
        "mutation_policy": mutation_policy,
        "approval_required": bool(approval_required),
        "status": _text(payload.get("status") or "queued"),
        "priority": priority_value,
        "idempotency_key": _text(payload.get("idempotency_key")) or None,
        "operation_id": _text(payload.get("operation_id")) or None,
        "parent_job_id": _text(payload.get("parent_job_id")) or None,
        "workspace_id": _text(payload.get("workspace_id")) or None,
        "lease_id": _text(payload.get("lease_id")) or None,
        "lease_owner": _text(payload.get("lease_owner")) or None,
        "lease_expires_at": _text(payload.get("lease_expires_at")) or None,
        "attempts": max(0, int(payload.get("attempts") or 0)),
        "max_attempts": max(1, int(payload.get("max_attempts") or 3)),
        "payload": _dict_value(payload.get("payload")),
        "policy": _dict_value(payload.get("policy")),
        "result": _dict_value(payload.get("result")),
        "blocked_reasons": _string_list(payload.get("blocked_reasons")),
        "created_at": _text(payload.get("created_at")) or utc_timestamp(),
        "updated_at": _text(payload.get("updated_at")) or None,
        "scheduled_for": _text(payload.get("scheduled_for")) or None,
        "started_at": _text(payload.get("started_at")) or None,
        "completed_at": _text(payload.get("completed_at")) or None,
    }
    if normalized["status"] not in COGNITIVE_JOB_STATUSES:
        normalized["status"] = "queued"
    if not normalized["idempotency_key"]:
        normalized["idempotency_key"] = f"{requested_capability}:{_stable_digest({k: normalized[k] for k in ('brain_id', 'trigger_source', 'requested_capability', 'payload')})}"
    return normalized


def evaluate_cognitive_job_policy(job: dict[str, Any], runtime_context: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized_job = normalize_cognitive_job(job)
    context = _dict_value(runtime_context)
    edition = _text(context.get("backend_edition") or context.get("edition") or "core").lower()
    subscription_plan = normalize_plan(context.get("subscription_plan") or context.get("plan") or ("dev" if edition == "dev" else "free"))
    automation_level = str(normalized_job["automation_level"])
    requested_capability = str(normalized_job["requested_capability"])
    mutation_policy = str(normalized_job["mutation_policy"])
    required_plan = str(normalized_job["required_plan"])
    module_id = normalized_job.get("module_id")

    blocked_reasons: list[str] = []
    warnings: list[str] = []

    if mutation_policy in _UNSAFE_MUTATION_POLICIES:
        blocked_reasons.append("hidden_or_unguarded_mutation_policy_forbidden")

    if not plan_allows(subscription_plan, required_plan):
        blocked_reasons.append(f"plan_{subscription_plan}_does_not_satisfy_{required_plan}")

    if module_id and module_id in _PAID_MODULE_IDS and not bool(context.get("module_entitlement_valid", plan_allows(subscription_plan, "pro"))):
        blocked_reasons.append("module_entitlement_missing")

    if automation_level == "cloud_automatic":
        if edition != "cloud":
            blocked_reasons.append("cloud_automatic_requires_cloud_edition")
        if not plan_allows(subscription_plan, "pro_plus"):
            blocked_reasons.append("cloud_automatic_requires_pro_plus")
        for flag, reason in (
            ("workspace_entitlement_valid", "workspace_entitlement_missing"),
            ("hosted_persistence_enabled", "hosted_persistence_required"),
            ("cloud_projection_ready", "module_not_projected_to_hosted_runtime"),
            ("workspace_module_enabled", "workspace_module_disabled"),
            ("quota_available", "quota_unavailable"),
            ("audit_enabled", "audit_policy_required"),
        ):
            if not bool(context.get(flag)):
                blocked_reasons.append(reason)
        if bool(context.get("hosted_key_required")) and not bool(context.get("hosted_key_scope_allowed")):
            blocked_reasons.append("hosted_key_scope_not_allowed")

    if automation_level == "local_operator_automation" and not bool(context.get("local_operator_automation_enabled")):
        blocked_reasons.append("local_operator_automation_not_enabled")

    if automation_level == "local_background_observation":
        if requested_capability not in _CORE_SAFE_BACKGROUND_CAPABILITIES and requested_capability in _FULL_MAINTENANCE_CAPABILITIES:
            if edition != "dev" and not bool(context.get("local_operator_automation_enabled")):
                blocked_reasons.append("full_maintenance_not_allowed_as_local_observation")
        if mutation_policy not in {"non_mutating", "preview_only"}:
            blocked_reasons.append("local_background_observation_must_be_non_mutating_or_preview")

    if mutation_policy == "audited_policy_apply" and not bool(context.get("audited_policy_apply_allowed")):
        blocked_reasons.append("audited_policy_apply_not_allowed")

    if normalized_job["approval_required"] and mutation_policy == "guarded_apply_required" and automation_level in {"cloud_automatic", "local_background_observation"}:
        warnings.append("job_may_prepare_apply_but_exact_apply_requires_confirmation")

    if subscription_plan == "dev":
        warnings.append("dev_plan_override_is_not_customer_entitlement")

    allowed = not blocked_reasons
    return {
        "schema_version": COGNITIVE_JOB_POLICY_DECISION_SCHEMA_VERSION,
        "allowed": allowed,
        "decision": "allow" if allowed else "block",
        "blocked_reasons": blocked_reasons,
        "warnings": warnings,
        "normalized_plan": subscription_plan,
        "required_plan": required_plan,
        "backend_edition": edition,
        "automation_level": automation_level,
        "requested_capability": requested_capability,
        "mutation_policy": mutation_policy,
        "module_id": module_id,
        "policy_principles": {
            "mcp_tool_visibility_is_not_automation_permission": True,
            "hidden_memory_node_mutation_allowed": False,
            "automatic_cycles_require_entitlement_policy": True,
            "cloud_only_cognitive_behavior_allowed": False,
            "manual_tools_remain_available_by_contract": True,
        },
    }


def cognitive_job_learning_event(job: dict[str, Any], *, event_kind: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized_job = normalize_cognitive_job(job)
    if event_kind not in {"background_job_scheduled", "background_job_blocked", "background_job_completed", "background_job_cancelled"}:
        raise ValueError(f"unsupported_cognitive_job_event:{event_kind}")
    event_payload = {
        "schema_version": "agvm.cognitive_job_learning_event_payload.v1",
        "job": {
            "job_id": normalized_job["job_id"],
            "requested_capability": normalized_job["requested_capability"],
            "trigger_source": normalized_job["trigger_source"],
            "automation_level": normalized_job["automation_level"],
            "mutation_policy": normalized_job["mutation_policy"],
            "required_plan": normalized_job["required_plan"],
            "module_id": normalized_job["module_id"],
            "status": normalized_job["status"],
        },
        **_dict_value(payload),
    }
    return {
        "schema_version": MEMORY_LEARNING_EVENT_SCHEMA_VERSION,
        "event_id": f"{event_kind}::{normalized_job['job_id']}::{_stable_digest(event_payload, length=12)}",
        "brain_id": normalized_job.get("brain_id"),
        "operation_id": normalized_job.get("operation_id") or normalized_job["job_id"],
        "event_kind": event_kind,
        "event_source": "cognitive_job_scheduler",
        "sleep_evolve_priority": normalized_job.get("priority"),
        "payload": event_payload,
    }


def build_cognitive_job_capability_report(*, storage_backend: str, table_present: bool, writable: bool, brain_id: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": COGNITIVE_JOB_CAPABILITY_SCHEMA_VERSION,
        "job_schema_version": COGNITIVE_JOB_SCHEMA_VERSION,
        "policy_decision_schema_version": COGNITIVE_JOB_POLICY_DECISION_SCHEMA_VERSION,
        "storage_backend": str(storage_backend or "unknown"),
        "brain_id": _text(brain_id) or None,
        "ready": bool(table_present),
        "writable": bool(writable),
        "statuses": sorted(COGNITIVE_JOB_STATUSES),
        "automation_levels": sorted(COGNITIVE_JOB_AUTOMATION_LEVELS),
        "mutation_policies": sorted(COGNITIVE_JOB_MUTATION_POLICIES),
        "contract": {
            "leaseable": True,
            "idempotent": True,
            "resumable": True,
            "manual_mcp_tools_sufficient": True,
            "automatic_cycles_policy_gated": True,
            "hidden_mutation_allowed": False,
        },
    }


__all__ = [
    "COGNITIVE_JOB_AUTOMATION_LEVELS",
    "COGNITIVE_JOB_CAPABILITY_SCHEMA_VERSION",
    "COGNITIVE_JOB_MUTATION_POLICIES",
    "COGNITIVE_JOB_POLICY_DECISION_SCHEMA_VERSION",
    "COGNITIVE_JOB_SCHEMA_VERSION",
    "COGNITIVE_JOB_STATUSES",
    "build_cognitive_job_capability_report",
    "cognitive_job_learning_event",
    "evaluate_cognitive_job_policy",
    "infer_required_plan",
    "normalize_cognitive_job",
    "normalize_plan",
    "plan_allows",
]
