# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import uuid
from copy import deepcopy
from typing import Any

from storage import utc_timestamp
from sqlite_store import (
    fetch_heuristic_calibration_snapshot,
    fetch_recent_landing_correction_events,
    fetch_recent_search_sessions,
    save_heuristic_calibration_payload,
    store_landing_correction_events,
    store_heuristic_calibration_event,
)

ROUTE_KEYS = ("highway", "link", "local")
COMPILED_PRIOR_PREFIX = "compiled_prior::"
FAILURE_SIGNATURE_PREFIX = "failure_signature::"
SPATIAL_CORRECTION_PREFIX = "spatial_correction_prior::"
MAX_COMPILED_TEMPLATES = 12
MAX_REVIEW_CANDIDATES = 12
MIN_SPATIAL_CORRECTION_STABLE_SUCCESSES = 2


def _default_scope_payload(scope_key: str) -> dict[str, Any]:
    return {
        "scope_key": str(scope_key or "global"),
        "schema_version": "heuristic_calibration.v2",
        "version": 0,
        "previous_version": None,
        "rollback_ref": None,
        "status": "active",
        "sample_count": 0.0,
        "success_count": 0.0,
        "failure_count": 0.0,
        "priors": {
            "branch_count_mean": None,
            "search_radius_mean": None,
            "crowding_penalty_factor_mean": 1.0,
            "guide_area_weights": {},
            "memory_type_weights": {},
            "destination_weights": {},
            "route_preference_weights": {key: 0.0 for key in ROUTE_KEYS},
            "answer_strand_templates": [],
            "slot_template_weights": {},
            "failure_reason_weights": {},
            "spatial_region_weights": {},
            "spatial_bucket_weights": {},
            "spatial_radial_band_weights": {},
            "spatial_correction_templates": [],
            "recommended_actions": [],
        },
        "evidence_basis": {
            "source_types": {},
            "reason_keys": {},
            "recent_source_refs": [],
        },
        "review": {
            "required": False,
            "reason": None,
            "candidate_count": 0,
        },
        "updated_at": None,
    }


def _scope_payload(snapshot: dict[str, Any], scope_key: str) -> dict[str, Any]:
    normalized_scope = str(scope_key or "global")
    if normalized_scope == "global":
        payload = dict(snapshot.get("global") or {})
    elif normalized_scope.startswith("query_class::"):
        payload = dict((snapshot.get("query_classes") or {}).get(normalized_scope.split("::", 1)[1]) or {})
    elif normalized_scope.startswith("goal::"):
        payload = dict((snapshot.get("goals") or {}).get(normalized_scope.split("::", 1)[1]) or {})
    elif normalized_scope.startswith(COMPILED_PRIOR_PREFIX):
        payload = dict((snapshot.get("compiled_priors") or {}).get(normalized_scope.split("::", 1)[1]) or {})
    elif normalized_scope.startswith(FAILURE_SIGNATURE_PREFIX):
        payload = dict((snapshot.get("failure_signatures") or {}).get(normalized_scope.split("::", 1)[1]) or {})
    elif normalized_scope.startswith(SPATIAL_CORRECTION_PREFIX):
        payload = dict((snapshot.get("spatial_correction_priors") or {}).get(normalized_scope.split("::", 1)[1]) or {})
    else:
        payload = {}
    base = _default_scope_payload(normalized_scope)
    base.update(payload)
    base["priors"] = {**dict(base.get("priors") or {}), **dict(payload.get("priors") or {})}
    base["evidence_basis"] = {**dict(base.get("evidence_basis") or {}), **dict(payload.get("evidence_basis") or {})}
    base["review"] = {**dict(base.get("review") or {}), **dict(payload.get("review") or {})}
    return base


def _merge_mean(existing_mean: Any, existing_count: Any, value: float, *, weight: float = 1.0) -> tuple[float, float]:
    prior_count = max(0.0, float(existing_count or 0.0))
    prior_mean = float(existing_mean or 0.0)
    total_count = prior_count + max(0.0, float(weight or 0.0))
    if total_count <= 0.0:
        return prior_mean, prior_count
    merged = ((prior_mean * prior_count) + (float(value) * float(weight))) / total_count
    return round(merged, 6), round(total_count, 6)


def _bump_weight_map(weight_map: dict[str, Any], key: str | None, amount: float) -> dict[str, float]:
    normalized = str(key or "").strip()
    updated = {str(name): float(value or 0.0) for name, value in dict(weight_map or {}).items() if str(name).strip()}
    if not normalized or amount <= 0.0:
        return updated
    updated[normalized] = round(updated.get(normalized, 0.0) + float(amount), 6)
    return updated


def _merge_route_preferences(weight_map: dict[str, Any], route_preferences: dict[str, Any], *, amount: float = 1.0) -> dict[str, float]:
    updated = {key: float(dict(weight_map or {}).get(key) or 0.0) for key in ROUTE_KEYS}
    source_preferences = dict(route_preferences or {})
    for key in ROUTE_KEYS:
        updated[key] = round(updated.get(key, 0.0) + max(0.0, float(source_preferences.get(key) or 0.0)) * amount, 6)
    return updated


def _normalized_route_preferences(weight_map: dict[str, Any]) -> dict[str, float]:
    weights = {key: max(0.0, float(dict(weight_map or {}).get(key) or 0.0)) for key in ROUTE_KEYS}
    total = sum(weights.values())
    if total <= 0.0:
        return {key: 0.0 for key in ROUTE_KEYS}
    return {key: round(value / total, 6) for key, value in weights.items()}


def _top_weight_choice(weight_map: dict[str, Any]) -> tuple[str | None, float]:
    normalized = {str(key): float(value or 0.0) for key, value in dict(weight_map or {}).items() if str(key).strip()}
    if not normalized:
        return None, 0.0
    key, value = max(normalized.items(), key=lambda item: (item[1], item[0]))
    total = sum(normalized.values())
    ratio = 0.0 if total <= 0.0 else float(value) / float(total)
    return key, round(ratio, 6)


def _top_weight_items(weight_map: dict[str, Any], *, limit: int = 5) -> list[dict[str, Any]]:
    weighted = [
        {"key": str(key), "weight": round(float(value or 0.0), 6)}
        for key, value in dict(weight_map or {}).items()
        if str(key).strip() and float(value or 0.0) > 0.0
    ]
    weighted.sort(key=lambda item: (-float(item["weight"]), str(item["key"])))
    return weighted[: max(0, int(limit))]


def _payload_version(payload: dict[str, Any]) -> int:
    try:
        return max(0, int(payload.get("version") or 0))
    except (TypeError, ValueError):
        return 0


def _prepare_versioned_payload(payload: dict[str, Any]) -> dict[str, Any]:
    current_version = _payload_version(payload)
    previous_updated_at = payload.get("updated_at")
    payload["previous_version"] = current_version if current_version > 0 else None
    payload["version"] = current_version + 1
    payload["schema_version"] = str(payload.get("schema_version") or "heuristic_calibration.v2")
    payload["rollback_ref"] = {
        "previous_version": current_version,
        "previous_updated_at": previous_updated_at,
        "recent_source_refs": list((dict(payload.get("evidence_basis") or {}).get("recent_source_refs") or []))[-4:],
    }
    return payload


def _compiled_prior_scope_key(query_class: str) -> str:
    normalized = str(query_class or "generic_context").strip() or "generic_context"
    return f"{COMPILED_PRIOR_PREFIX}{normalized}"


def _failure_signature_scope_key(query_class: str, reason_key: str) -> str:
    normalized_query_class = str(query_class or "generic_context").strip() or "generic_context"
    normalized_reason = str(reason_key or "unknown").strip().replace(" ", "_") or "unknown"
    return f"{FAILURE_SIGNATURE_PREFIX}{normalized_query_class}::{normalized_reason}"


def _template_goal_key(template: dict[str, Any]) -> str:
    return str(template.get("goal") or template.get("answer_field") or "").strip().lower()


def _template_signature(template: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(template.get("goal") or "").strip().lower(),
        str(template.get("answer_field") or "").strip().lower(),
        str(template.get("expected_guide_area") or "").strip().lower(),
        str(template.get("expected_memory_type") or "").strip().lower(),
    )


def _ranked_templates(templates: list[dict[str, Any]] | None, *, limit: int = MAX_COMPILED_TEMPLATES) -> list[dict[str, Any]]:
    rows = [dict(item) for item in list(templates or []) if isinstance(item, dict) and _template_goal_key(dict(item))]
    rows.sort(
        key=lambda item: (
            -float(item.get("weight") or item.get("confidence") or item.get("priority") or 0.0),
            str(item.get("goal") or ""),
            str(item.get("landing_hint") or ""),
        )
    )
    return rows[: max(0, int(limit))]


def _append_recent_ref(payload: dict[str, Any], source_ref: str | None) -> None:
    evidence_basis = dict(payload.get("evidence_basis") or {})
    refs = [str(item) for item in list(evidence_basis.get("recent_source_refs") or []) if str(item).strip()]
    normalized_ref = str(source_ref or "").strip()
    if normalized_ref:
        refs.append(normalized_ref)
    evidence_basis["recent_source_refs"] = refs[-8:]
    payload["evidence_basis"] = evidence_basis


def _register_evidence(payload: dict[str, Any], *, source_type: str, reason_key: str, source_ref: str | None) -> None:
    evidence_basis = dict(payload.get("evidence_basis") or {})
    source_types = {str(key): int(value or 0) for key, value in dict(evidence_basis.get("source_types") or {}).items()}
    reason_keys = {str(key): int(value or 0) for key, value in dict(evidence_basis.get("reason_keys") or {}).items()}
    normalized_source_type = str(source_type or "unknown").strip() or "unknown"
    normalized_reason_key = str(reason_key or "unknown").strip() or "unknown"
    source_types[normalized_source_type] = source_types.get(normalized_source_type, 0) + 1
    reason_keys[normalized_reason_key] = reason_keys.get(normalized_reason_key, 0) + 1
    evidence_basis["source_types"] = source_types
    evidence_basis["reason_keys"] = reason_keys
    payload["evidence_basis"] = evidence_basis
    _append_recent_ref(payload, source_ref)


def _scope_summary(payload: dict[str, Any]) -> dict[str, Any]:
    priors = dict(payload.get("priors") or {})
    guide_area, guide_ratio = _top_weight_choice(priors.get("guide_area_weights") or {})
    memory_type, memory_ratio = _top_weight_choice(priors.get("memory_type_weights") or {})
    destination_key, destination_ratio = _top_weight_choice(priors.get("destination_weights") or {})
    return {
        "scope_key": str(payload.get("scope_key") or "global"),
        "version": _payload_version(payload),
        "status": str(payload.get("status") or "active"),
        "sample_count": round(float(payload.get("sample_count") or 0.0), 6),
        "success_count": round(float(payload.get("success_count") or 0.0), 6),
        "failure_count": round(float(payload.get("failure_count") or 0.0), 6),
        "branch_count_mean": priors.get("branch_count_mean"),
        "search_radius_mean": priors.get("search_radius_mean"),
        "crowding_penalty_factor_mean": priors.get("crowding_penalty_factor_mean"),
        "top_guide_area": guide_area,
        "top_guide_area_ratio": guide_ratio,
        "top_memory_type": memory_type,
        "top_memory_type_ratio": memory_ratio,
        "top_destination_key": destination_key,
        "top_destination_ratio": destination_ratio,
        "route_preference": _normalized_route_preferences(priors.get("route_preference_weights") or {}),
        "updated_at": payload.get("updated_at"),
    }


def _compiled_scope_summary(payload: dict[str, Any]) -> dict[str, Any]:
    priors = dict(payload.get("priors") or {})
    templates = _ranked_templates(list(priors.get("answer_strand_templates") or []), limit=6)
    slot_weights = dict(priors.get("slot_template_weights") or {})
    landing_weights = dict(priors.get("landing_prior_weights") or {})
    return {
        "scope_key": str(payload.get("scope_key") or ""),
        "version": _payload_version(payload),
        "status": str(payload.get("status") or "active"),
        "sample_count": round(float(payload.get("sample_count") or 0.0), 6),
        "success_count": round(float(payload.get("success_count") or 0.0), 6),
        "template_count": len(list(priors.get("answer_strand_templates") or [])),
        "top_slots": _top_weight_items(slot_weights, limit=5),
        "top_landings": _top_weight_items(landing_weights, limit=5),
        "templates": templates,
        "updated_at": payload.get("updated_at"),
        "evidence_refs": list((dict(payload.get("evidence_basis") or {}).get("recent_source_refs") or []))[-4:],
    }


def _failure_scope_summary(payload: dict[str, Any]) -> dict[str, Any]:
    priors = dict(payload.get("priors") or {})
    review = dict(payload.get("review") or {})
    return {
        "scope_key": str(payload.get("scope_key") or ""),
        "version": _payload_version(payload),
        "status": str(payload.get("status") or "review_candidate"),
        "sample_count": round(float(payload.get("sample_count") or 0.0), 6),
        "failure_count": round(float(payload.get("failure_count") or 0.0), 6),
        "top_failure_reasons": _top_weight_items(dict(priors.get("failure_reason_weights") or {}), limit=5),
        "recommended_actions": [str(item) for item in list(priors.get("recommended_actions") or [])[:5] if str(item).strip()],
        "review_required": bool(review.get("required", True)),
        "review_reason": review.get("reason"),
        "updated_at": payload.get("updated_at"),
        "evidence_refs": list((dict(payload.get("evidence_basis") or {}).get("recent_source_refs") or []))[-4:],
    }


def _spatial_correction_scope_summary(payload: dict[str, Any]) -> dict[str, Any]:
    priors = dict(payload.get("priors") or {})
    review = dict(payload.get("review") or {})
    templates = [dict(item) for item in list(priors.get("spatial_correction_templates") or []) if isinstance(item, dict)]
    return {
        "scope_key": str(payload.get("scope_key") or ""),
        "version": _payload_version(payload),
        "status": str(payload.get("status") or "review_candidate"),
        "sample_count": round(float(payload.get("sample_count") or 0.0), 6),
        "success_count": round(float(payload.get("success_count") or 0.0), 6),
        "review_required": bool(review.get("required", True)),
        "review_reason": review.get("reason"),
        "top_regions": _top_weight_items(dict(priors.get("spatial_region_weights") or {}), limit=5),
        "top_buckets": _top_weight_items(dict(priors.get("spatial_bucket_weights") or {}), limit=5),
        "top_radial_bands": _top_weight_items(dict(priors.get("spatial_radial_band_weights") or {}), limit=5),
        "template_count": len(templates),
        "templates": templates[:5],
        "updated_at": payload.get("updated_at"),
        "evidence_refs": list((dict(payload.get("evidence_basis") or {}).get("recent_source_refs") or []))[-4:],
    }


def summarize_calibration_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    snapshot = dict(snapshot or {})
    global_scope = _scope_summary(_scope_payload(snapshot, "global"))
    query_classes = {str(key): _scope_summary(value) for key, value in dict(snapshot.get("query_classes") or {}).items()}
    goals = {str(key): _scope_summary(value) for key, value in dict(snapshot.get("goals") or {}).items()}
    compiled_priors = {
        str(key): _compiled_scope_summary(value)
        for key, value in dict(snapshot.get("compiled_priors") or {}).items()
    }
    failure_signatures = {
        str(key): _failure_scope_summary(value)
        for key, value in dict(snapshot.get("failure_signatures") or {}).items()
    }
    spatial_correction_priors = {
        str(key): _spatial_correction_scope_summary(value)
        for key, value in dict(snapshot.get("spatial_correction_priors") or {}).items()
    }
    active_compiled_prior_count = sum(1 for payload in compiled_priors.values() if str(payload.get("status") or "") == "active")
    review_candidate_count = sum(1 for payload in failure_signatures.values() if bool(payload.get("review_required")))
    spatial_review_candidate_count = sum(1 for payload in spatial_correction_priors.values() if bool(payload.get("review_required")))
    return {
        "scope_count": 1 + len(query_classes) + len(goals) + len(compiled_priors) + len(failure_signatures) + len(spatial_correction_priors),
        "event_count": int(snapshot.get("event_count") or 0),
        "updated_at": snapshot.get("updated_at"),
        "global": global_scope,
        "query_classes": query_classes,
        "goals": goals,
        "compiled_prior_count": len(compiled_priors),
        "active_compiled_prior_count": active_compiled_prior_count,
        "failure_signature_count": len(failure_signatures),
        "spatial_correction_prior_count": len(spatial_correction_priors),
        "review_candidate_count": review_candidate_count,
        "spatial_review_candidate_count": spatial_review_candidate_count,
        "compiled_priors": compiled_priors,
        "failure_signatures": failure_signatures,
        "spatial_correction_priors": spatial_correction_priors,
        "recent_events": [dict(item) for item in list(snapshot.get("recent_events") or [])[:6]],
        "recent_landing_correction_events": [dict(item) for item in list(snapshot.get("recent_landing_correction_events") or [])[:6]],
    }


def _compiled_payload_for_query(snapshot: dict[str, Any], query_class: str) -> dict[str, Any]:
    return _scope_payload(snapshot, _compiled_prior_scope_key(query_class))


def _failure_payloads_for_query(snapshot: dict[str, Any], query_class: str) -> dict[str, dict[str, Any]]:
    normalized_query_class = str(query_class or "generic_context").strip() or "generic_context"
    prefix = f"{normalized_query_class}::"
    return {
        str(key): dict(value)
        for key, value in dict(snapshot.get("failure_signatures") or {}).items()
        if str(key) == normalized_query_class or str(key).startswith(prefix)
    }


def _compiled_template_weights(templates: list[dict[str, Any]]) -> dict[str, float]:
    weights: dict[str, float] = {}
    for template in templates:
        goal = str(template.get("goal") or "").strip()
        if not goal:
            continue
        amount = max(0.1, float(template.get("weight") or template.get("confidence") or template.get("priority") or 0.0))
        weights[goal] = round(weights.get(goal, 0.0) + amount, 6)
    return weights


def _compiled_template_for_goal(bundle: dict[str, Any], goal: str) -> dict[str, Any] | None:
    normalized_goal = str(goal or "").strip().lower()
    if not normalized_goal:
        return None
    for template in _ranked_templates(list(bundle.get("compiled_answer_strand_templates") or []), limit=MAX_COMPILED_TEMPLATES):
        if _template_goal_key(template) == normalized_goal:
            return dict(template)
    return None


def build_runtime_calibration_bundle(snapshot: dict[str, Any] | None, *, query_class: str, default_probe_limit: int, goals: list[str]) -> dict[str, Any]:
    snapshot = dict(snapshot or {})
    normalized_query_class = str(query_class or "generic_context").strip() or "generic_context"
    query_scope = _scope_payload(snapshot, f"query_class::{normalized_query_class}")
    goal_scopes = {
        str(goal): _scope_payload(snapshot, f"goal::{str(goal)}")
        for goal in list(dict.fromkeys(str(goal).strip() for goal in goals if str(goal).strip()))
    }
    sample_count = float(query_scope.get("sample_count") or 0.0)
    branch_count_mean = (dict(query_scope.get("priors") or {})).get("branch_count_mean")
    branch_count_target = int(default_probe_limit)
    if branch_count_mean is not None and sample_count >= 1.0:
        strength = min(0.55, 0.18 + (min(sample_count, 6.0) * 0.05))
        blended = (float(default_probe_limit) * (1.0 - strength)) + (float(branch_count_mean) * strength)
        branch_count_target = max(1, int(round(blended)))
    compiled_scope = _compiled_payload_for_query(snapshot, normalized_query_class)
    compiled_priors = dict(compiled_scope.get("priors") or {})
    compiled_templates = _ranked_templates(list(compiled_priors.get("answer_strand_templates") or []), limit=MAX_COMPILED_TEMPLATES)
    compiled_active = (
        str(compiled_scope.get("status") or "active") == "active"
        and float(compiled_scope.get("sample_count") or 0.0) > 0.0
        and bool(compiled_templates)
    )
    compiled_goal_weights = _compiled_template_weights(compiled_templates)
    if compiled_active:
        compiled_goal_count = len({str(item.get("goal") or "").strip() for item in compiled_templates if str(item.get("goal") or "").strip()})
        branch_count_target = max(branch_count_target, min(int(default_probe_limit) + 2, max(int(default_probe_limit), compiled_goal_count)))
    scope_keys_used = []
    if sample_count > 0.0:
        scope_keys_used.append(f"query_class::{normalized_query_class}")
    for goal, payload in goal_scopes.items():
        if float(payload.get("sample_count") or 0.0) > 0.0:
            scope_keys_used.append(f"goal::{goal}")
    if compiled_active:
        scope_keys_used.append(_compiled_prior_scope_key(normalized_query_class))
    failure_payloads = _failure_payloads_for_query(snapshot, normalized_query_class)
    review_candidate_count = sum(1 for payload in failure_payloads.values() if bool(dict(payload.get("review") or {}).get("required", True)))
    spatial_payloads = {
        str(key): dict(value)
        for key, value in dict(snapshot.get("spatial_correction_priors") or {}).items()
        if f"query_class={normalized_query_class}" in str(dict(value).get("scope_key") or key)
    }
    active_spatial_payloads = {
        key: value
        for key, value in spatial_payloads.items()
        if str(dict(value).get("status") or "") == "active" and not bool(dict(dict(value).get("review") or {}).get("required", False))
    }
    return {
        "query_class": normalized_query_class,
        "default_probe_limit": int(default_probe_limit),
        "branch_count_target": int(branch_count_target),
        "branch_count_adjustment": int(branch_count_target) - int(default_probe_limit),
        "scope_keys_used": scope_keys_used,
        "query_scope": query_scope,
        "goal_scopes": goal_scopes,
        "compiled_prior_scope": compiled_scope,
        "compiled_prior_available": compiled_active,
        "compiled_prior_scope_key": _compiled_prior_scope_key(normalized_query_class) if compiled_active else None,
        "compiled_answer_strand_templates": compiled_templates if compiled_active else [],
        "compiled_goal_weights": compiled_goal_weights if compiled_active else {},
        "compiled_template_count": len(compiled_templates) if compiled_active else 0,
        "failure_signature_count": len(failure_payloads),
        "review_candidate_count": review_candidate_count,
        "failure_signature_scope_keys": [str(payload.get("scope_key") or "") for payload in failure_payloads.values()],
        "spatial_correction_prior_count": len(spatial_payloads),
        "active_spatial_correction_prior_count": len(active_spatial_payloads),
        "spatial_correction_scope_keys": [str(payload.get("scope_key") or "") for payload in spatial_payloads.values()],
        "summary": summarize_calibration_snapshot(snapshot),
    }


def compiled_answer_strands_from_bundle(
    bundle: dict[str, Any],
    *,
    query_text: str,
    max_probe_count: int,
) -> list[dict[str, Any]]:
    if not bool(bundle.get("compiled_prior_available")):
        return []
    strands: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for index, template in enumerate(_ranked_templates(list(bundle.get("compiled_answer_strand_templates") or []), limit=max_probe_count), start=1):
        signature = _template_signature(template)
        if signature in seen:
            continue
        seen.add(signature)
        goal = str(template.get("goal") or "").strip()
        if not goal:
            continue
        confidence = max(0.0, min(1.0, float(template.get("confidence") or template.get("priority") or 0.74)))
        strands.append(
            {
                "strand_id": f"compiled_prior_{goal}_{index}",
                "query_text": str(query_text or "").strip(),
                "goal": goal,
                "landing_hint": str(template.get("landing_hint") or query_text or goal).strip(),
                "priority": round(max(0.42, confidence), 4),
                "planner_family": "heuristic",
                "answer_field": str(template.get("answer_field") or "").strip() or None,
                "answer_hypothesis": str(template.get("answer_hypothesis") or "").strip() or None,
                "expected_guide_area": str(template.get("expected_guide_area") or "").strip() or None,
                "expected_memory_type": str(template.get("expected_memory_type") or "").strip() or None,
                "radial_expectation": str(template.get("radial_expectation") or "mid").strip() or "mid",
                "inverse_rationale": "compiled_heuristic_prior",
                "destination_queue": [dict(item) for item in list(template.get("destination_queue") or []) if isinstance(item, dict)],
                "family_plan_id": "compiled_heuristic_prior",
                "family_plan_confidence": round(confidence, 4),
                "seed_confidence": round(confidence, 4),
            }
        )
        if len(strands) >= max(1, int(max_probe_count)):
            break
    return strands


def apply_calibration_to_spec(spec: dict[str, Any], *, bundle: dict[str, Any], default_radius: float) -> tuple[dict[str, Any], dict[str, Any]]:
    updated = dict(spec)
    goal = str(updated.get("goal") or "").strip()
    goal_scope = dict((bundle.get("goal_scopes") or {}).get(goal) or {})
    query_scope = dict(bundle.get("query_scope") or {})
    notes: dict[str, Any] = {
        "goal": goal,
        "guide_area_override": None,
        "memory_type_override": None,
        "radius_blend": None,
        "crowding_penalty_factor": 1.0,
        "destination_weight_count": 0,
        "route_preference_prior": {},
        "compiled_prior_template": None,
        "compiled_prior_applied": False,
        "scope_keys": [],
    }
    if float(query_scope.get("sample_count") or 0.0) > 0.0:
        notes["scope_keys"].append(str(query_scope.get("scope_key") or ""))
    if float(goal_scope.get("sample_count") or 0.0) > 0.0:
        notes["scope_keys"].append(str(goal_scope.get("scope_key") or ""))

    goal_priors = dict(goal_scope.get("priors") or {})
    query_priors = dict(query_scope.get("priors") or {})
    compiled_template = _compiled_template_for_goal(bundle, goal)
    compiled_scope_key = str(bundle.get("compiled_prior_scope_key") or "").strip()
    if compiled_template and compiled_scope_key:
        notes["scope_keys"].append(compiled_scope_key)
        notes["compiled_prior_applied"] = True
        notes["compiled_prior_template"] = {
            "goal": str(compiled_template.get("goal") or ""),
            "answer_field": str(compiled_template.get("answer_field") or ""),
            "expected_guide_area": str(compiled_template.get("expected_guide_area") or ""),
            "expected_memory_type": str(compiled_template.get("expected_memory_type") or ""),
            "confidence": round(float(compiled_template.get("confidence") or 0.0), 6),
        }
    guide_area_key, guide_area_ratio = _top_weight_choice(goal_priors.get("guide_area_weights") or {})
    if guide_area_key and guide_area_ratio >= 0.54:
        updated["expected_guide_area"] = guide_area_key
        notes["guide_area_override"] = guide_area_key
    elif compiled_template and str(compiled_template.get("expected_guide_area") or "").strip():
        updated["expected_guide_area"] = str(compiled_template.get("expected_guide_area") or "").strip()
        notes["guide_area_override"] = updated["expected_guide_area"]
    memory_type_key, memory_type_ratio = _top_weight_choice(goal_priors.get("memory_type_weights") or {})
    if memory_type_key and memory_type_ratio >= 0.54:
        updated["expected_memory_type"] = memory_type_key
        notes["memory_type_override"] = memory_type_key
    elif compiled_template and str(compiled_template.get("expected_memory_type") or "").strip():
        updated["expected_memory_type"] = str(compiled_template.get("expected_memory_type") or "").strip()
        notes["memory_type_override"] = updated["expected_memory_type"]

    goal_radius = goal_priors.get("search_radius_mean")
    query_radius = query_priors.get("search_radius_mean")
    radius_samples = max(float(goal_scope.get("sample_count") or 0.0), float(query_scope.get("sample_count") or 0.0))
    if goal_radius is not None or query_radius is not None:
        target_radius = float(goal_radius if goal_radius is not None else query_radius)
        strength = min(0.5, 0.14 + (min(radius_samples, 6.0) * 0.05))
        blended_radius = (float(default_radius) * (1.0 - strength)) + (target_radius * strength)
        updated["search_radius"] = round(max(0.14, min(0.5, blended_radius)), 4)
        notes["radius_blend"] = updated["search_radius"]

    crowding_factor = float(query_priors.get("crowding_penalty_factor_mean") or goal_priors.get("crowding_penalty_factor_mean") or 1.0)
    crowding_factor = max(0.7, min(1.35, crowding_factor))
    updated["crowding_penalty_factor"] = round(crowding_factor, 4)
    notes["crowding_penalty_factor"] = updated["crowding_penalty_factor"]

    destination_weights = {
        str(key): float(value or 0.0)
        for key, value in dict(goal_priors.get("destination_weights") or {}).items()
        if str(key).strip()
    }
    if destination_weights:
        updated["calibration_destination_weights"] = destination_weights
        notes["destination_weight_count"] = len(destination_weights)
    if compiled_template:
        compiled_destination_weights = {
            str(key): float(value or 0.0)
            for key, value in dict(compiled_template.get("destination_weights") or {}).items()
            if str(key).strip()
        }
        if compiled_destination_weights:
            merged_destination_weights = dict(updated.get("calibration_destination_weights") or {})
            for key, value in compiled_destination_weights.items():
                merged_destination_weights[key] = round(float(merged_destination_weights.get(key) or 0.0) + value, 6)
            updated["calibration_destination_weights"] = merged_destination_weights
            notes["destination_weight_count"] = len(merged_destination_weights)

    route_preference_weights = _normalized_route_preferences(
        goal_priors.get("route_preference_weights") or query_priors.get("route_preference_weights") or {}
    )
    updated["route_preference_prior"] = route_preference_weights
    notes["route_preference_prior"] = route_preference_weights
    updated["calibration_scope_keys"] = [item for item in list(dict.fromkeys(notes["scope_keys"])) if item]
    updated["calibration_applied"] = bool(updated.get("calibration_scope_keys"))
    updated["compiled_prior_applied"] = bool(notes.get("compiled_prior_applied"))
    if notes.get("compiled_prior_template"):
        updated["compiled_prior_template"] = dict(notes["compiled_prior_template"])
    return updated, notes


def reorder_destination_queue(destinations: list[dict[str, Any]] | None, destination_weights: dict[str, Any] | None) -> list[dict[str, Any]]:
    destination_weights = {
        str(key): float(value or 0.0)
        for key, value in dict(destination_weights or {}).items()
        if str(key).strip()
    }
    if not destination_weights:
        return [dict(item) for item in list(destinations or [])]
    ranked: list[tuple[float, int, dict[str, Any]]] = []
    for index, destination in enumerate(list(destinations or [])):
        payload = dict(destination)
        destination_key = str(payload.get("destination_key") or "").strip()
        payload["calibration_weight"] = round(float(destination_weights.get(destination_key, 0.0)), 6)
        ranked.append((float(payload["calibration_weight"]), -float(payload.get("priority") or 0.0), payload))
    ranked.sort(key=lambda item: (-item[0], item[1], str(item[2].get("destination_key") or "")))
    reordered: list[dict[str, Any]] = []
    total = max(1, len(ranked))
    for index, (_, __, payload) in enumerate(ranked, start=1):
        payload["priority"] = round(max(0.0, min(1.0, float(payload.get("priority") or 0.5) + (0.08 * ((total - index) / total)))), 4)
        reordered.append(payload)
    return reordered


def _heuristic_route_preferences_from_branches(branches: list[dict[str, Any]]) -> dict[str, float]:
    totals = {key: 0.0 for key in ROUTE_KEYS}
    for branch in branches:
        totals["highway"] += float(branch.get("highway_hops_taken") or 0.0)
        totals["link"] += float(branch.get("link_hops_taken") or 0.0)
        totals["local"] += float(branch.get("local_hops_taken") or 0.0)
    return _normalized_route_preferences(totals)


def _heuristic_session_summary(plan: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    probes = {
        str(probe.get("probe_id") or ""): dict(probe)
        for probe in list(plan.get("probes") or [])
        if str(probe.get("probe_id") or "").strip()
    }
    branches = [
        dict(branch)
        for branch in list(result.get("branches") or [])
        if str(branch.get("planner_family") or "").strip().lower() == "heuristic"
    ]
    evidence_node_ids = sorted(
        {
            str(node_id)
            for branch in branches
            for node_id in list(branch.get("evidence_node_ids") or [])
            if str(node_id).strip()
        }
    )
    query_class = next(
        (
            str(probe.get("query_class") or "").strip()
            for probe in probes.values()
            if str(probe.get("query_class") or "").strip()
        ),
        "generic_context",
    ) or "generic_context"
    return {
        "query_class": query_class,
        "branches": branches,
        "probes": probes,
        "heuristic_branch_count": len(branches),
        "evidence_node_ids": evidence_node_ids,
        "route_preferences": _heuristic_route_preferences_from_branches(branches),
        "answerability_state": str(result.get("answerability_state") or "").strip(),
        "grounded": str(result.get("answerability_state") or "").strip() in {"grounded", "partial"},
    }


def _stable_session_refs(search_id: str, session_summary: dict[str, Any], recent_sessions: list[dict[str, Any]]) -> list[str]:
    current_nodes = set(str(node_id) for node_id in list(session_summary.get("evidence_node_ids") or []) if str(node_id).strip())
    query_class = str(session_summary.get("query_class") or "")
    required_overlap = 1 if len(current_nodes) <= 2 else max(2, int(round(len(current_nodes) * 0.4)))
    stable_refs: list[str] = []
    for session in recent_sessions:
        if str(session.get("search_id") or "") == str(search_id):
            continue
        result = dict(session.get("result") or {})
        plan = dict(session.get("plan") or {})
        summary = _heuristic_session_summary(plan, result)
        if not summary.get("grounded"):
            continue
        if str(summary.get("query_class") or "") != query_class:
            continue
        overlap = current_nodes & set(str(node_id) for node_id in list(summary.get("evidence_node_ids") or []) if str(node_id).strip())
        if len(overlap) >= required_overlap:
            stable_refs.append(str(session.get("search_id") or ""))
    return stable_refs


def _persist_scope_update(
    *,
    scope_key: str,
    payload: dict[str, Any],
    source_type: str,
    source_ref: str,
    reason_key: str,
    evidence: dict[str, Any],
    delta: dict[str, Any],
) -> str:
    payload = _prepare_versioned_payload(payload)
    payload["updated_at"] = utc_timestamp()
    save_heuristic_calibration_payload(scope_key, payload)
    event_id = str(uuid.uuid4())
    store_heuristic_calibration_event(
        event_id=event_id,
        event_kind=reason_key,
        source_type=source_type,
        source_ref=source_ref,
        scope_key=scope_key,
        evidence=evidence,
        delta=delta,
    )
    return event_id


def _destination_key_from_item(destination: dict[str, Any]) -> str:
    return str(
        destination.get("destination_key")
        or destination.get("id")
        or destination.get("destination_id")
        or "::".join(
            item
            for item in [
                str(destination.get("label") or destination.get("guide_area") or "").strip(),
                str(destination.get("memory_type") or "").strip(),
            ]
            if item
        )
    ).strip()


def _branch_probe(branch: dict[str, Any], probes: dict[str, Any]) -> dict[str, Any]:
    for probe_id in list(branch.get("probe_ids") or []):
        probe = dict(probes.get(str(probe_id)) or {})
        if probe:
            return probe
    goal = str(branch.get("goal") or "").strip()
    if goal:
        for probe in probes.values():
            if str(dict(probe).get("goal") or "").strip() == goal:
                return dict(probe)
    return {}


def _template_from_branch(branch: dict[str, Any], probes: dict[str, Any]) -> dict[str, Any] | None:
    goal = str(branch.get("goal") or "").strip()
    if not goal:
        return None
    probe = _branch_probe(branch, probes)
    destination_queue = [
        dict(item)
        for item in list(branch.get("family_destination_queue") or branch.get("destination_queue") or probe.get("destination_queue") or [])
        if isinstance(item, dict)
    ][:6]
    destination_weights: dict[str, float] = {}
    sanitized_destination_queue: list[dict[str, Any]] = []
    for position, destination in enumerate(destination_queue, start=1):
        destination_key = _destination_key_from_item(destination)
        if not destination_key:
            continue
        weight = max(0.12, 1.0 - ((position - 1) * 0.2))
        destination_weights[destination_key] = round(destination_weights.get(destination_key, 0.0) + weight, 6)
        sanitized_destination_queue.append(
            {
                "destination_key": destination_key,
                "label": str(destination.get("label") or destination.get("guide_area") or destination_key).strip(),
                "guide_area": str(destination.get("guide_area") or probe.get("expected_guide_area") or "").strip() or None,
                "memory_type": str(destination.get("memory_type") or probe.get("expected_memory_type") or "").strip() or None,
                "priority": round(max(0.1, float(destination.get("priority") or weight)), 4),
            }
        )
    route_yield = max(0.0, min(1.0, float(branch.get("route_yield") or 0.0)))
    evidence_count = len([node_id for node_id in list(branch.get("evidence_node_ids") or []) if str(node_id).strip()])
    confidence = round(max(0.42, min(0.96, 0.58 + (0.08 * min(evidence_count, 4)) + (0.18 * route_yield))), 4)
    return {
        "goal": goal,
        "answer_field": str(probe.get("expected_answer_field") or probe.get("answer_field") or "").strip() or None,
        "landing_hint": str(probe.get("query_text") or branch.get("query_text") or goal).strip(),
        "answer_hypothesis": str(probe.get("answer_hypothesis") or "").strip() or None,
        "expected_guide_area": str(probe.get("expected_guide_area") or "").strip() or None,
        "expected_memory_type": str(probe.get("expected_memory_type") or "").strip() or None,
        "radial_expectation": str(probe.get("radial_expectation") or "mid").strip() or "mid",
        "destination_queue": sanitized_destination_queue,
        "destination_weights": destination_weights,
        "route_preference": _normalized_route_preferences(
            {
                "highway": float(branch.get("highway_hops_taken") or 0.0),
                "link": float(branch.get("link_hops_taken") or 0.0),
                "local": float(branch.get("local_hops_taken") or 0.0),
            }
        ),
        "confidence": confidence,
        "priority": confidence,
        "weight": round(max(0.1, float(evidence_count or 1)) * confidence, 6),
        "evidence_node_ids": [str(node_id) for node_id in list(branch.get("evidence_node_ids") or [])[:6] if str(node_id).strip()],
    }


def _merge_compiled_templates(existing_templates: list[dict[str, Any]], new_templates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for template in _ranked_templates(existing_templates, limit=MAX_COMPILED_TEMPLATES):
        merged[_template_signature(template)] = dict(template)
    for template in new_templates:
        signature = _template_signature(template)
        if not any(signature):
            continue
        existing = dict(merged.get(signature) or {})
        if existing:
            existing_weight = float(existing.get("weight") or existing.get("confidence") or 0.0)
            new_weight = float(template.get("weight") or template.get("confidence") or 0.0)
            total_weight = max(0.1, existing_weight + new_weight)
            existing["weight"] = round(total_weight, 6)
            existing["confidence"] = round(max(float(existing.get("confidence") or 0.0), float(template.get("confidence") or 0.0)), 4)
            existing["evidence_node_ids"] = list(
                dict.fromkeys(
                    [str(item) for item in list(existing.get("evidence_node_ids") or []) if str(item).strip()]
                    + [str(item) for item in list(template.get("evidence_node_ids") or []) if str(item).strip()]
                )
            )[:8]
            destination_weights = dict(existing.get("destination_weights") or {})
            for key, value in dict(template.get("destination_weights") or {}).items():
                destination_weights[str(key)] = round(float(destination_weights.get(str(key)) or 0.0) + float(value or 0.0), 6)
            existing["destination_weights"] = destination_weights
            if not list(existing.get("destination_queue") or []):
                existing["destination_queue"] = [dict(item) for item in list(template.get("destination_queue") or []) if isinstance(item, dict)]
            merged[signature] = existing
        else:
            merged[signature] = dict(template)
    return _ranked_templates(list(merged.values()), limit=MAX_COMPILED_TEMPLATES)


def _compile_success_priors(
    *,
    snapshot: dict[str, Any],
    search_id: str,
    query_class: str,
    heuristic_branches: list[dict[str, Any]],
    probes: dict[str, Any],
    evidence_basis: dict[str, Any],
) -> tuple[str | None, str | None]:
    templates = [
        template
        for template in (_template_from_branch(branch, probes) for branch in heuristic_branches)
        if template
    ]
    if not templates:
        return None, None
    scope_key = _compiled_prior_scope_key(query_class)
    compiled_scope = _scope_payload(snapshot, scope_key)
    compiled_scope["status"] = "active"
    compiled_scope["sample_count"] = round(float(compiled_scope.get("sample_count") or 0.0) + 1.0, 6)
    compiled_scope["success_count"] = round(float(compiled_scope.get("success_count") or 0.0) + 1.0, 6)
    compiled_scope["failure_count"] = float(compiled_scope.get("failure_count") or 0.0)
    compiled_scope["review"] = {"required": False, "reason": None, "candidate_count": 0}
    compiled_scope["mutation_policy"] = {
        "mode": "auditable_prior_store",
        "code_generation": False,
        "auto_apply_without_gate": False,
    }
    compiled_scope["priors"] = dict(compiled_scope.get("priors") or {})
    compiled_scope["priors"]["answer_strand_templates"] = _merge_compiled_templates(
        list(compiled_scope["priors"].get("answer_strand_templates") or []),
        templates,
    )
    slot_weights = dict(compiled_scope["priors"].get("slot_template_weights") or {})
    landing_weights = dict(compiled_scope["priors"].get("landing_prior_weights") or {})
    for template in templates:
        amount = max(0.1, float(template.get("weight") or template.get("confidence") or 0.0))
        slot_weights = _bump_weight_map(slot_weights, str(template.get("goal") or ""), amount)
        landing_key = "::".join(
            item
            for item in [
                str(template.get("goal") or "").strip(),
                str(template.get("expected_guide_area") or "").strip(),
                str(template.get("expected_memory_type") or "").strip(),
            ]
            if item
        )
        landing_weights = _bump_weight_map(landing_weights, landing_key, amount)
    compiled_scope["priors"]["slot_template_weights"] = slot_weights
    compiled_scope["priors"]["landing_prior_weights"] = landing_weights
    _register_evidence(compiled_scope, source_type="compiled_heuristic_prior", reason_key="repeated_stable_answer_strands", source_ref=search_id)
    event_id = _persist_scope_update(
        scope_key=scope_key,
        payload=compiled_scope,
        source_type="compiled_heuristic_prior",
        source_ref=search_id,
        reason_key="repeated_stable_answer_strands",
        evidence={**evidence_basis, "compiled_template_count": len(templates)},
        delta={
            "template_count": len(compiled_scope["priors"]["answer_strand_templates"]),
            "top_slots": _top_weight_items(slot_weights, limit=5),
            "mutation_policy": compiled_scope["mutation_policy"],
        },
    )
    return scope_key, event_id


def _session_failure_reason(session_summary: dict[str, Any]) -> str | None:
    if str(session_summary.get("answerability_state") or "") not in {"grounded", "partial"}:
        return "answer_not_grounded"
    branches = [dict(branch) for branch in list(session_summary.get("branches") or [])]
    if not branches:
        return "no_heuristic_family"
    evidence_count = len(list(session_summary.get("evidence_node_ids") or []))
    if evidence_count <= 0:
        return "no_heuristic_evidence"
    exhausted = sum(1 for branch in branches if "exhaust" in str(branch.get("stop_reason") or branch.get("status") or "").lower())
    if exhausted >= max(1, len(branches)):
        return "route_exhausted"
    return None


def _recommended_actions_for_failure(reason_key: str) -> list[str]:
    if reason_key == "no_heuristic_family":
        return ["compile_query_decomposition_template", "review_required_slots"]
    if reason_key == "no_heuristic_evidence":
        return ["add_landing_prior_candidate", "inspect_destination_queue"]
    if reason_key == "route_exhausted":
        return ["review_route_highway_prior", "add_destination_reachability_check"]
    if reason_key == "answer_not_grounded":
        return ["calibrate_answer_adequacy_gate", "review_landing_and_slot_templates"]
    return ["review_failure_signature"]


def _persist_failure_signature_candidate(
    *,
    snapshot: dict[str, Any],
    search_id: str,
    session_summary: dict[str, Any],
    reason_key: str,
) -> tuple[str, str]:
    query_class = str(session_summary.get("query_class") or "generic_context")
    scope_key = _failure_signature_scope_key(query_class, reason_key)
    failure_scope = _scope_payload(snapshot, scope_key)
    failure_scope["status"] = "review_candidate"
    failure_scope["sample_count"] = round(float(failure_scope.get("sample_count") or 0.0) + 1.0, 6)
    failure_scope["failure_count"] = round(float(failure_scope.get("failure_count") or 0.0) + 1.0, 6)
    failure_scope["review"] = {
        "required": True,
        "reason": reason_key,
        "candidate_count": int((dict(failure_scope.get("review") or {}).get("candidate_count") or 0)) + 1,
    }
    failure_scope["mutation_policy"] = {
        "mode": "review_candidate",
        "code_generation": False,
        "auto_apply_without_gate": False,
    }
    failure_scope["priors"] = dict(failure_scope.get("priors") or {})
    failure_scope["priors"]["failure_reason_weights"] = _bump_weight_map(
        failure_scope["priors"].get("failure_reason_weights") or {},
        reason_key,
        1.0,
    )
    actions = list(dict.fromkeys([str(item) for item in list(failure_scope["priors"].get("recommended_actions") or [])] + _recommended_actions_for_failure(reason_key)))
    failure_scope["priors"]["recommended_actions"] = actions[:8]
    _register_evidence(failure_scope, source_type="retrieval_failure_signature", reason_key=reason_key, source_ref=search_id)
    event_id = _persist_scope_update(
        scope_key=scope_key,
        payload=failure_scope,
        source_type="retrieval_failure_signature",
        source_ref=search_id,
        reason_key="compiled_failure_signature_candidate",
        evidence={
            "search_id": str(search_id),
            "query_class": query_class,
            "reason_key": reason_key,
            "answerability_state": session_summary.get("answerability_state"),
            "heuristic_branch_count": int(session_summary.get("heuristic_branch_count") or 0),
            "evidence_node_count": len(list(session_summary.get("evidence_node_ids") or [])),
        },
        delta={
            "status": "review_candidate",
            "failure_count": failure_scope["failure_count"],
            "recommended_actions": failure_scope["priors"]["recommended_actions"],
            "mutation_policy": failure_scope["mutation_policy"],
        },
    )
    return scope_key, event_id


def _coord_float(coordinate: dict[str, Any], key: str) -> float:
    try:
        return float(dict(coordinate or {}).get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _radial_band_from_coordinate(coordinate: dict[str, Any]) -> str:
    coord = dict(coordinate or {})
    if not coord:
        return "unknown"
    radius = (_coord_float(coord, "x") ** 2 + _coord_float(coord, "y") ** 2 + _coord_float(coord, "z") ** 2) ** 0.5
    if radius < 0.28:
        return "inner"
    if radius < 0.62:
        return "mid"
    return "outer"


def _average_coordinate(coordinates: list[dict[str, Any]]) -> dict[str, float]:
    clean = [dict(item) for item in coordinates if isinstance(item, dict) and item]
    if not clean:
        return {}
    return {
        "x": round(sum(_coord_float(item, "x") for item in clean) / len(clean), 6),
        "y": round(sum(_coord_float(item, "y") for item in clean) / len(clean), 6),
        "z": round(sum(_coord_float(item, "z") for item in clean) / len(clean), 6),
    }


def _event_probe(event: dict[str, Any], probes: dict[str, Any]) -> dict[str, Any]:
    probe_id = str(event.get("probe_id") or "").strip()
    if probe_id and probe_id in probes:
        return dict(probes[probe_id])
    ai_path_id = str(event.get("ai_spatial_path_id") or "").strip()
    if ai_path_id:
        for probe in probes.values():
            if str(dict(probe).get("ai_spatial_path_id") or "").strip() == ai_path_id:
                return dict(probe)
    return next((dict(probe) for probe in probes.values() if dict(probe)), {})


def _spatial_correction_scope_key(event: dict[str, Any]) -> str:
    parts = {
        "brain_revision": str(event.get("brain_revision") or "unknown").strip() or "unknown",
        "query_class": str(event.get("query_class") or "generic_context").strip() or "generic_context",
        "goal": str(event.get("goal") or "generic_goal").strip() or "generic_goal",
        "answer_field": str(event.get("answer_field") or "unknown").strip() or "unknown",
        "guide_area": str(event.get("guide_area") or "unknown").strip() or "unknown",
        "memory_type": str(event.get("memory_type") or "unknown").strip() or "unknown",
        "radial_band": str(event.get("radial_band") or "unknown").strip() or "unknown",
        "region": str(event.get("ai_landing_region_ref") or event.get("snapped_region_ref") or event.get("bucket_key") or "unknown").strip() or "unknown",
    }
    body = "::".join(f"{key}={value.replace('::', '_')}" for key, value in parts.items())
    return f"{SPATIAL_CORRECTION_PREFIX}{body}"


def _landing_correction_events_from_result(
    *,
    search_id: str,
    plan: dict[str, Any],
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    path_corridors = dict(result.get("path_corridors") or {})
    raw_events = [dict(item) for item in list(path_corridors.get("landing_correction_events") or []) if isinstance(item, dict)]
    if not raw_events:
        return []
    probes = {
        str(probe.get("probe_id") or ""): dict(probe)
        for probe in list(plan.get("probes") or [])
        if isinstance(probe, dict) and str(probe.get("probe_id") or "").strip()
    }
    spatial_contract = dict(plan.get("ai_spatial_landing_contract") or {})
    planner_runtime = dict(plan.get("planner_runtime") or {})
    brain_revision = str(
        spatial_contract.get("brain_revision")
        or planner_runtime.get("brain_revision")
        or planner_runtime.get("metamemory_brain_revision")
        or ""
    )
    enriched: list[dict[str, Any]] = []
    for raw_event in raw_events:
        event = dict(raw_event)
        probe = _event_probe(event, probes)
        snapped_coordinate = dict(event.get("snapped_coordinate") or {})
        radial_band = _radial_band_from_coordinate(snapped_coordinate)
        event.update(
            {
                "search_id": str(search_id),
                "query_class": str(probe.get("query_class") or event.get("query_class") or "generic_context").strip() or "generic_context",
                "goal": str(probe.get("goal") or event.get("goal") or "").strip(),
                "answer_field": str(
                    probe.get("expected_answer_field")
                    or probe.get("answer_field")
                    or event.get("answer_field")
                    or ""
                ).strip(),
                "guide_area": str(probe.get("expected_guide_area") or event.get("guide_area") or "").strip(),
                "memory_type": str(probe.get("expected_memory_type") or event.get("memory_type") or "").strip(),
                "radial_band": radial_band,
                "brain_revision": brain_revision,
            }
        )
        event["successful"] = bool(
            event.get("changed_context_package")
            or event.get("destination_reached")
            or list(event.get("promoted_hot_node_ids") or [])
        )
        event["scope_key"] = _spatial_correction_scope_key(event)
        enriched.append(event)
    return enriched


def _spatial_success_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        dict(event)
        for event in list(events or [])
        if bool(event.get("successful"))
        and (
            str(event.get("ai_spatial_path_id") or "").strip()
            or dict(event.get("ai_landing_coordinate") or {})
            or str(event.get("ai_landing_region_ref") or "").strip()
            or dict(event.get("snapped_coordinate") or {})
        )
    ]


def _persist_landing_correction_priors(
    *,
    snapshot: dict[str, Any],
    search_id: str,
    current_events: list[dict[str, Any]],
    recent_events: list[dict[str, Any]],
) -> dict[str, Any]:
    current_scope_keys = {str(event.get("scope_key") or "") for event in current_events if str(event.get("scope_key") or "").strip()}
    if not current_scope_keys:
        return {"review_candidate_count": 0, "scope_keys": [], "event_ids": []}
    events_by_scope: dict[str, list[dict[str, Any]]] = {}
    for event in _spatial_success_events(list(current_events or []) + list(recent_events or [])):
        scope_key = str(event.get("scope_key") or "").strip()
        if scope_key:
            events_by_scope.setdefault(scope_key, []).append(dict(event))
    event_ids: list[str] = []
    scope_keys: list[str] = []
    review_candidates: list[dict[str, Any]] = []
    for scope_key in sorted(current_scope_keys):
        scope_events = events_by_scope.get(scope_key) or []
        distinct_search_ids = sorted({str(event.get("search_id") or "") for event in scope_events if str(event.get("search_id") or "").strip()})
        if len(distinct_search_ids) < MIN_SPATIAL_CORRECTION_STABLE_SUCCESSES:
            continue
        payload = _scope_payload(snapshot, scope_key)
        raw_key = scope_key.split("::", 1)[1] if "::" in scope_key else scope_key
        existing_payload = dict((snapshot.get("spatial_correction_priors") or {}).get(raw_key) or {})
        existing_review = dict(existing_payload.get("review") or {})
        existing_is_approved_active = (
            str(existing_payload.get("status") or "") == "active"
            and not bool(existing_review.get("required", True))
        )
        payload["status"] = "active" if existing_is_approved_active else "review_candidate"
        payload["sample_count"] = max(float(payload.get("sample_count") or 0.0), float(len(scope_events)))
        payload["success_count"] = max(float(payload.get("success_count") or 0.0), float(len(scope_events)))
        payload["review"] = {
            "required": str(payload.get("status") or "") != "active",
            "reason": "repeated_ai_spatial_correction_requires_review",
            "candidate_count": len(scope_events),
        }
        payload["mutation_policy"] = {
            "mode": "review_candidate_spatial_prior",
            "code_generation": False,
            "auto_apply_without_gate": False,
            "requires_repeated_success": True,
            "requires_human_or_maintenance_approval": True,
        }
        payload["priors"] = dict(payload.get("priors") or {})
        region_weights = dict(payload["priors"].get("spatial_region_weights") or {})
        bucket_weights = dict(payload["priors"].get("spatial_bucket_weights") or {})
        radial_weights = dict(payload["priors"].get("spatial_radial_band_weights") or {})
        for event in scope_events:
            region_weights = _bump_weight_map(region_weights, str(event.get("ai_landing_region_ref") or event.get("snapped_region_ref") or ""), 1.0)
            bucket_weights = _bump_weight_map(bucket_weights, str(event.get("bucket_key") or ""), 1.0)
            radial_weights = _bump_weight_map(radial_weights, str(event.get("radial_band") or ""), 1.0)
        snapped_coordinates = [dict(event.get("snapped_coordinate") or {}) for event in scope_events]
        ai_coordinates = [dict(event.get("ai_landing_coordinate") or {}) for event in scope_events]
        template = {
            "scope_key": scope_key,
            "query_class": str(scope_events[0].get("query_class") or ""),
            "goal": str(scope_events[0].get("goal") or ""),
            "answer_field": str(scope_events[0].get("answer_field") or ""),
            "guide_area": str(scope_events[0].get("guide_area") or ""),
            "memory_type": str(scope_events[0].get("memory_type") or ""),
            "radial_band": str(scope_events[0].get("radial_band") or ""),
            "recommended_region_ref": _top_weight_choice(region_weights)[0],
            "recommended_bucket_key": _top_weight_choice(bucket_weights)[0],
            "snapped_coordinate_mean": _average_coordinate(snapped_coordinates),
            "ai_coordinate_mean": _average_coordinate(ai_coordinates),
            "successful_search_ids": distinct_search_ids[:8],
            "promoted_hot_node_ids": list(
                dict.fromkeys(
                    str(node_id)
                    for event in scope_events
                    for node_id in list(event.get("promoted_hot_node_ids") or [])
                    if str(node_id).strip()
                )
            )[:16],
            "cold_reservoir_node_ids": list(
                dict.fromkeys(
                    str(node_id)
                    for event in scope_events
                    for node_id in list(event.get("cold_reservoir_node_ids") or [])
                    if str(node_id).strip()
                )
            )[:16],
            "confidence": round(min(0.92, 0.48 + (0.12 * min(len(distinct_search_ids), 4))), 4),
        }
        templates = [dict(item) for item in list(payload["priors"].get("spatial_correction_templates") or []) if isinstance(item, dict)]
        templates = [item for item in templates if str(item.get("scope_key") or "") != scope_key]
        templates.append(template)
        payload["priors"]["spatial_correction_templates"] = templates[-MAX_COMPILED_TEMPLATES:]
        payload["priors"]["spatial_region_weights"] = region_weights
        payload["priors"]["spatial_bucket_weights"] = bucket_weights
        payload["priors"]["spatial_radial_band_weights"] = radial_weights
        _register_evidence(payload, source_type="landing_correction_event", reason_key="repeated_ai_spatial_correction", source_ref=search_id)
        event_id = _persist_scope_update(
            scope_key=scope_key,
            payload=payload,
            source_type="landing_correction_event",
            source_ref=search_id,
            reason_key="spatial_landing_correction_prior_review",
            evidence={
                "search_id": str(search_id),
                "stable_success_search_ids": distinct_search_ids[:8],
                "event_count": len(scope_events),
                "scope_key": scope_key,
                "mutation_policy": payload["mutation_policy"],
            },
            delta={
                "status": payload["status"],
                "review_required": bool(dict(payload.get("review") or {}).get("required", True)),
                "recommended_region_ref": template["recommended_region_ref"],
                "recommended_bucket_key": template["recommended_bucket_key"],
                "snapped_coordinate_mean": template["snapped_coordinate_mean"],
                "auto_apply_without_gate": False,
            },
        )
        event_ids.append(event_id)
        scope_keys.append(scope_key)
        review_candidates.append(template)
    return {
        "review_candidate_count": len(review_candidates),
        "scope_keys": scope_keys,
        "event_ids": event_ids,
        "candidates": review_candidates[:MAX_REVIEW_CANDIDATES],
    }


def _persist_landing_correction_learning(*, search_id: str, plan: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    current_events = _landing_correction_events_from_result(search_id=search_id, plan=plan, result=result)
    if not current_events:
        return {"stored_event_count": 0, "review_candidate_count": 0, "scope_keys": [], "event_ids": []}
    stored_events = store_landing_correction_events(
        search_id=search_id,
        brain_id=str(result.get("brain_id") or plan.get("brain_id") or ""),
        query_text=str(result.get("query_text") or (result.get("path_corridors") or {}).get("query_text") or ""),
        retrieval_mode=str((result.get("path_corridors") or {}).get("retrieval_mode") or result.get("retrieval_mode") or ""),
        events=current_events,
    )
    recent_events = fetch_recent_landing_correction_events(
        limit=80,
        brain_id=str(result.get("brain_id") or plan.get("brain_id") or "") or None,
    )
    snapshot = fetch_heuristic_calibration_snapshot()
    prior_learning = _persist_landing_correction_priors(
        snapshot=snapshot,
        search_id=search_id,
        current_events=stored_events or current_events,
        recent_events=recent_events,
    )
    return {
        "stored_event_count": len(stored_events),
        "stored_event_ids": [str(event.get("event_id") or "") for event in stored_events if str(event.get("event_id") or "").strip()],
        "review_candidate_count": int(prior_learning.get("review_candidate_count") or 0),
        "scope_keys": list(prior_learning.get("scope_keys") or []),
        "event_ids": list(prior_learning.get("event_ids") or []),
        "candidates": list(prior_learning.get("candidates") or []),
        "mutation_policy": {
            "code_generation": False,
            "auto_apply_without_gate": False,
            "review_only_until_approved": True,
        },
    }


def learn_from_retrieval_session(*, search_id: str, plan: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    landing_correction_learning = _persist_landing_correction_learning(search_id=search_id, plan=plan, result=result)
    session_summary = _heuristic_session_summary(plan, result)
    failure_reason = _session_failure_reason(session_summary)
    if failure_reason:
        snapshot = fetch_heuristic_calibration_snapshot()
        failure_scope_key, failure_event_id = _persist_failure_signature_candidate(
            snapshot=snapshot,
            search_id=search_id,
            session_summary=session_summary,
            reason_key=failure_reason,
        )
        return {
            "applied": False,
            "reason": failure_reason,
            "failure_candidate": True,
            "failure_scope_key": failure_scope_key,
            "event_ids": [failure_event_id],
            "landing_correction_learning": landing_correction_learning,
        }
    recent_sessions = fetch_recent_search_sessions(limit=20)
    stable_refs = _stable_session_refs(search_id, session_summary, recent_sessions)
    if not stable_refs:
        return {"applied": False, "reason": "not_repeated_stable_evidence", "landing_correction_learning": landing_correction_learning}

    snapshot = fetch_heuristic_calibration_snapshot()
    applied_scope_keys: list[str] = []
    event_ids: list[str] = []
    compiled_scope_keys: list[str] = []
    compiled_event_ids: list[str] = []
    query_class = str(session_summary.get("query_class") or "generic_context")
    heuristic_branches = [dict(branch) for branch in list(session_summary.get("branches") or [])]
    probes = dict(session_summary.get("probes") or {})
    route_preferences = dict(session_summary.get("route_preferences") or {})
    source_type = "repeated_stable_retrieval"
    evidence_basis = {
        "search_id": str(search_id),
        "query_class": query_class,
        "stable_reference_search_ids": stable_refs[:6],
        "evidence_node_ids": list(session_summary.get("evidence_node_ids") or [])[:12],
    }

    query_scope_key = f"query_class::{query_class}"
    query_scope = _scope_payload(snapshot, query_scope_key)
    query_prior_sample_count = float(query_scope.get("sample_count") or 0.0)
    query_scope["sample_count"] = round(query_prior_sample_count + 1.0, 6)
    query_scope["success_count"] = round(float(query_scope.get("success_count") or 0.0) + 1.0, 6)
    query_scope["priors"] = dict(query_scope.get("priors") or {})
    branch_count_mean, branch_count_samples = _merge_mean(
        query_scope["priors"].get("branch_count_mean"),
        query_prior_sample_count,
        float(session_summary.get("heuristic_branch_count") or 0.0),
    )
    query_scope["priors"]["branch_count_mean"] = branch_count_mean
    radius_values = [float(branch.get("search_radius") or 0.0) for branch in heuristic_branches if float(branch.get("search_radius") or 0.0) > 0.0]
    if radius_values:
        radius_mean, _ = _merge_mean(
            query_scope["priors"].get("search_radius_mean"),
            query_prior_sample_count,
            sum(radius_values) / len(radius_values),
        )
        query_scope["priors"]["search_radius_mean"] = radius_mean
    average_crowding_factor = 1.0
    if probes:
        crowding_factors = [float((dict(probe).get("crowding_penalty_factor") or 1.0)) for probe in probes.values()]
        if crowding_factors:
            average_crowding_factor = sum(crowding_factors) / len(crowding_factors)
    crowding_mean, _ = _merge_mean(
        query_scope["priors"].get("crowding_penalty_factor_mean"),
        query_prior_sample_count,
        average_crowding_factor,
    )
    query_scope["priors"]["crowding_penalty_factor_mean"] = crowding_mean
    query_scope["priors"]["route_preference_weights"] = _merge_route_preferences(
        query_scope["priors"].get("route_preference_weights") or {},
        route_preferences,
        amount=1.0,
    )
    _register_evidence(query_scope, source_type=source_type, reason_key="repeated_stable_evidence", source_ref=search_id)
    event_ids.append(
        _persist_scope_update(
            scope_key=query_scope_key,
            payload=query_scope,
            source_type=source_type,
            source_ref=search_id,
            reason_key="repeated_stable_evidence",
            evidence=evidence_basis,
            delta={
                "branch_count_mean": branch_count_mean,
                "branch_count_samples": branch_count_samples,
                "search_radius_mean": query_scope["priors"].get("search_radius_mean"),
                "crowding_penalty_factor_mean": crowding_mean,
                "route_preference": _normalized_route_preferences(query_scope["priors"].get("route_preference_weights") or {}),
            },
        )
    )
    applied_scope_keys.append(query_scope_key)

    for branch in heuristic_branches:
        goal = str(branch.get("goal") or "").strip()
        if not goal:
            continue
        scope_key = f"goal::{goal}"
        goal_scope = _scope_payload(snapshot, scope_key)
        goal_prior_sample_count = float(goal_scope.get("sample_count") or 0.0)
        goal_scope["sample_count"] = round(goal_prior_sample_count + 1.0, 6)
        goal_scope["success_count"] = round(float(goal_scope.get("success_count") or 0.0) + 1.0, 6)
        goal_scope["priors"] = dict(goal_scope.get("priors") or {})
        probe = next(
            (
                dict(probes.get(str(probe_id)) or {})
                for probe_id in list(branch.get("probe_ids") or [])
                if dict(probes.get(str(probe_id)) or {})
            ),
            {},
        )
        guide_area = str(probe.get("expected_guide_area") or "").strip()
        if guide_area:
            goal_scope["priors"]["guide_area_weights"] = _bump_weight_map(goal_scope["priors"].get("guide_area_weights") or {}, guide_area, 1.0)
        memory_type = str(probe.get("expected_memory_type") or "").strip()
        if memory_type:
            goal_scope["priors"]["memory_type_weights"] = _bump_weight_map(goal_scope["priors"].get("memory_type_weights") or {}, memory_type, 1.0)
        search_radius = float(branch.get("search_radius") or probe.get("search_radius") or 0.0)
        if search_radius > 0.0:
            merged_radius, _ = _merge_mean(goal_scope["priors"].get("search_radius_mean"), goal_prior_sample_count, search_radius)
            goal_scope["priors"]["search_radius_mean"] = merged_radius
        for position, destination in enumerate(list(branch.get("family_destination_queue") or branch.get("destination_queue") or []), start=1):
            destination_key = str(dict(destination).get("destination_key") or "").strip()
            if destination_key:
                goal_scope["priors"]["destination_weights"] = _bump_weight_map(
                    goal_scope["priors"].get("destination_weights") or {},
                    destination_key,
                    max(0.18, 1.0 - ((position - 1) * 0.22)),
                )
        goal_scope["priors"]["route_preference_weights"] = _merge_route_preferences(
            goal_scope["priors"].get("route_preference_weights") or {},
            {
                "highway": float(branch.get("highway_hops_taken") or 0.0),
                "link": float(branch.get("link_hops_taken") or 0.0),
                "local": float(branch.get("local_hops_taken") or 0.0),
            },
            amount=1.0,
        )
        branch_crowding_factor = float(probe.get("crowding_penalty_factor") or 1.0)
        merged_crowding, _ = _merge_mean(goal_scope["priors"].get("crowding_penalty_factor_mean"), goal_prior_sample_count, branch_crowding_factor)
        goal_scope["priors"]["crowding_penalty_factor_mean"] = merged_crowding
        _register_evidence(goal_scope, source_type=source_type, reason_key="repeated_stable_evidence", source_ref=search_id)
        event_ids.append(
            _persist_scope_update(
                scope_key=scope_key,
                payload=goal_scope,
                source_type=source_type,
                source_ref=search_id,
                reason_key="repeated_stable_evidence",
                evidence={
                    **evidence_basis,
                    "goal": goal,
                    "route_yield": round(float(branch.get("route_yield") or 0.0), 6),
                },
                delta={
                    "guide_area": guide_area or None,
                    "memory_type": memory_type or None,
                    "search_radius_mean": goal_scope["priors"].get("search_radius_mean"),
                    "destination_weight_count": len(dict(goal_scope["priors"].get("destination_weights") or {})),
                    "route_preference": _normalized_route_preferences(goal_scope["priors"].get("route_preference_weights") or {}),
                    "crowding_penalty_factor_mean": merged_crowding,
                },
            )
        )
        applied_scope_keys.append(scope_key)

    compiled_scope_key, compiled_event_id = _compile_success_priors(
        snapshot=snapshot,
        search_id=search_id,
        query_class=query_class,
        heuristic_branches=heuristic_branches,
        probes=probes,
        evidence_basis=evidence_basis,
    )
    if compiled_scope_key and compiled_event_id:
        applied_scope_keys.append(compiled_scope_key)
        event_ids.append(compiled_event_id)
        compiled_scope_keys.append(compiled_scope_key)
        compiled_event_ids.append(compiled_event_id)

    return {
        "applied": True,
        "reason": "repeated_stable_evidence",
        "scope_keys": list(dict.fromkeys(applied_scope_keys)),
        "event_ids": event_ids,
        "compiled_scope_keys": compiled_scope_keys,
        "compiled_event_ids": compiled_event_ids,
        "stable_reference_search_ids": stable_refs[:6],
        "landing_correction_learning": landing_correction_learning,
    }


def enrich_maintenance_report_with_calibration(*, report: dict[str, Any], maintenance_id: str, apply_updates: bool) -> dict[str, Any]:
    snapshot_before = fetch_heuristic_calibration_snapshot()
    before_summary = summarize_calibration_snapshot(snapshot_before)
    report_payload = deepcopy(dict(report or {}))
    global_scope = _scope_payload(snapshot_before, "global")
    global_scope["priors"] = dict(global_scope.get("priors") or {})
    route_delta = {"highway": 0.0, "link": 0.0, "local": 0.0}
    bridge_promotions = list(report_payload.get("bridge_promotions") or [])
    bridge_demotions = list(report_payload.get("bridge_demotions") or [])
    highway_changes = list(report_payload.get("highway_changes") or [])
    retyped_nodes = list(report_payload.get("retyped_nodes") or [])
    quality_before = dict(report_payload.get("quality_before") or {})
    quality_after = dict(report_payload.get("quality_after") or {})
    quality_delta = dict(report_payload.get("quality_delta") or {})

    route_delta["highway"] += float(len(bridge_promotions)) + float(sum(1 for change in highway_changes if "promot" in str(change.get("change") or "").lower()))
    route_delta["link"] += float(len(bridge_demotions)) * 0.5
    route_delta["local"] += 1.0 if not bridge_promotions and not highway_changes else 0.0
    global_prior_sample_count = float(global_scope.get("sample_count") or 0.0)
    global_scope["sample_count"] = round(global_prior_sample_count + 1.0, 6)
    if bool(report_payload.get("applied")):
        global_scope["success_count"] = round(float(global_scope.get("success_count") or 0.0) + 1.0, 6)
    global_scope["priors"]["route_preference_weights"] = _merge_route_preferences(
        global_scope["priors"].get("route_preference_weights") or {},
        route_delta,
        amount=1.0,
    )
    target_crowding_factor = 1.0 + max(0.0, float(quality_after.get("crowded_bucket_ratio") or 0.0) - float(quality_before.get("crowded_bucket_ratio") or 0.0))
    if bool(quality_delta.get("geometry_improved")):
        target_crowding_factor = max(0.82, target_crowding_factor - 0.08)
    merged_crowding, _ = _merge_mean(
        global_scope["priors"].get("crowding_penalty_factor_mean"),
        global_prior_sample_count,
        max(0.72, min(1.3, target_crowding_factor)),
    )
    global_scope["priors"]["crowding_penalty_factor_mean"] = merged_crowding
    for item in retyped_nodes:
        payload = dict(item)
        guide_area = str(payload.get("guide_area") or payload.get("to_guide_area") or payload.get("target_guide_area") or "").strip()
        memory_type = str(payload.get("to_memory_type") or payload.get("memory_type") or "").strip()
        if guide_area:
            global_scope["priors"]["guide_area_weights"] = _bump_weight_map(global_scope["priors"].get("guide_area_weights") or {}, guide_area, 1.0)
        if memory_type:
            global_scope["priors"]["memory_type_weights"] = _bump_weight_map(global_scope["priors"].get("memory_type_weights") or {}, memory_type, 1.0)
    _register_evidence(
        global_scope,
        source_type="maintenance_applied" if apply_updates else "maintenance_preview",
        reason_key="maintenance_quality_delta",
        source_ref=maintenance_id,
    )

    preview_snapshot = deepcopy(snapshot_before)
    preview_snapshot["global"] = global_scope
    preview_snapshot["updated_at"] = utc_timestamp()
    after_summary = summarize_calibration_snapshot(preview_snapshot)
    event_ids: list[str] = []
    if apply_updates:
        event_ids.append(
            _persist_scope_update(
                scope_key="global",
                payload=global_scope,
                source_type="maintenance_applied",
                source_ref=maintenance_id,
                reason_key="maintenance_quality_delta",
                evidence={
                    "maintenance_id": maintenance_id,
                    "mode": str(report_payload.get("mode") or ""),
                    "retyped_node_count": len(retyped_nodes),
                    "bridge_promotion_count": len(bridge_promotions),
                    "bridge_demotion_count": len(bridge_demotions),
                    "overall_quality_delta_score": float(report_payload.get("overall_quality_delta_score") or 0.0),
                },
                delta={
                    "route_preference": _normalized_route_preferences(global_scope["priors"].get("route_preference_weights") or {}),
                    "crowding_penalty_factor_mean": merged_crowding,
                    "top_guide_area": _top_weight_choice(global_scope["priors"].get("guide_area_weights") or {})[0],
                    "top_memory_type": _top_weight_choice(global_scope["priors"].get("memory_type_weights") or {})[0],
                },
            )
        )
        snapshot_after = fetch_heuristic_calibration_snapshot()
        after_summary = summarize_calibration_snapshot(snapshot_after)

    report_payload["calibration_before"] = before_summary
    report_payload["calibration_after"] = after_summary
    report_payload["calibration_delta"] = {
        "applied": bool(apply_updates),
        "route_preference": _normalized_route_preferences(global_scope["priors"].get("route_preference_weights") or {}),
        "crowding_penalty_factor_mean": merged_crowding,
        "retyped_node_count": len(retyped_nodes),
        "bridge_promotion_count": len(bridge_promotions),
        "bridge_demotion_count": len(bridge_demotions),
        "scope_keys": ["global"],
    }
    report_payload["calibration_evidence_basis"] = [
        {
            "kind": "maintenance_quality_delta",
            "maintenance_id": maintenance_id,
            "mode": str(report_payload.get("mode") or ""),
            "overall_quality_delta_score": float(report_payload.get("overall_quality_delta_score") or 0.0),
        }
    ]
    summary_for_review = after_summary if apply_updates else before_summary
    report_payload["compiled_prior_recommendations"] = [
        {
            "scope_key": str(payload.get("scope_key") or ""),
            "version": payload.get("version"),
            "template_count": payload.get("template_count"),
            "top_slots": payload.get("top_slots") or [],
            "evidence_refs": payload.get("evidence_refs") or [],
            "status": payload.get("status"),
        }
        for payload in list(dict(summary_for_review.get("compiled_priors") or {}).values())[:MAX_REVIEW_CANDIDATES]
    ]
    report_payload["calibration_review_candidates"] = [
        {
            "scope_key": str(payload.get("scope_key") or ""),
            "version": payload.get("version"),
            "reason": payload.get("review_reason"),
            "failure_count": payload.get("failure_count"),
            "recommended_actions": payload.get("recommended_actions") or [],
            "evidence_refs": payload.get("evidence_refs") or [],
            "status": payload.get("status"),
        }
        for payload in list(dict(summary_for_review.get("failure_signatures") or {}).values())[:MAX_REVIEW_CANDIDATES]
    ]
    report_payload["spatial_correction_prior_recommendations"] = [
        {
            "scope_key": str(payload.get("scope_key") or ""),
            "version": payload.get("version"),
            "status": payload.get("status"),
            "review_required": bool(payload.get("review_required", True)),
            "top_regions": payload.get("top_regions") or [],
            "top_buckets": payload.get("top_buckets") or [],
            "template_count": payload.get("template_count"),
            "evidence_refs": payload.get("evidence_refs") or [],
        }
        for payload in list(dict(summary_for_review.get("spatial_correction_priors") or {}).values())[:MAX_REVIEW_CANDIDATES]
    ]
    report_payload["calibration_event_ids"] = event_ids
    created_nodes = [dict(item) for item in list(report_payload.get("created_nodes") or []) if isinstance(item, dict)]
    document_anchor_guard = dict(report_payload.get("document_anchor_guard") or {})
    retrieval_gap_review = dict(report_payload.get("retrieval_gap_review") or {})
    post_retrieval_calibration = dict(retrieval_gap_review.get("post_retrieval_calibration") or {})
    report_payload["self_improvement_loop"] = {
        "schema_version": "agvm.pr9.self_improvement_loop.v1",
        "maintenance_id": maintenance_id,
        "mode": str(report_payload.get("mode") or ""),
        "applied": bool(apply_updates),
        "reviewable": True,
        "mutation_policy": {
            "code_generation": False,
            "fact_creation_from_maintenance": False,
            "failure_signatures_auto_apply": False,
            "compiled_priors_scope": "bootstrap_prior_store",
            "system_metadata_answer_eligible": False,
        },
        "idempotency": {
            "pattern_node_actions": [
                {
                    "node_id": str(item.get("node_id") or ""),
                    "idempotency_key": str(item.get("idempotency_key") or ""),
                    "action": str(item.get("applied_action") or item.get("action") or ""),
                    "existing_node_id": item.get("existing_node_id"),
                }
                for item in created_nodes
            ],
            "pattern_reuse_count": sum(1 for item in created_nodes if "reuse" in str(item.get("applied_action") or item.get("action") or "")),
            "pattern_create_count": sum(1 for item in created_nodes if "create" in str(item.get("applied_action") or item.get("action") or "")),
        },
        "proposed_changes": {
            "merge_candidate_count": len(list(report_payload.get("duplicate_candidates") or [])),
            "merge_count": len(list(report_payload.get("merges") or [])),
            "retyped_node_count": len(list(report_payload.get("retyped_nodes") or [])),
            "highway_suggestion_count": len(list(report_payload.get("new_highways") or [])) + len(list(report_payload.get("highway_changes") or [])),
            "pattern_candidate_count": len(list(report_payload.get("pattern_candidates") or [])),
            "follow_up_candidate_count": len(list(report_payload.get("follow_up_candidates") or [])),
            "review_candidate_count": len(list(report_payload.get("calibration_review_candidates") or [])),
        },
        "calibration_learning": {
            "event_ids": event_ids,
            "compiled_prior_recommendation_count": len(list(report_payload.get("compiled_prior_recommendations") or [])),
            "failure_review_candidate_count": len(list(report_payload.get("calibration_review_candidates") or [])),
            "spatial_correction_prior_recommendation_count": len(list(report_payload.get("spatial_correction_prior_recommendations") or [])),
            "calibration_scope_keys": list(dict(report_payload.get("calibration_delta") or {}).get("scope_keys") or []),
            "post_retrieval_calibration_gain": float(post_retrieval_calibration.get("post_retrieval_calibration_gain") or 0.0),
        },
        "document_anchor_guard": document_anchor_guard,
        "source_hygiene_guard": {
            "synthetic_test_answer_eligible": False,
            "system_metadata_answer_eligible": False,
            "document_anchor_delete_blocked": bool(document_anchor_guard.get("raw_document_anchor_delete_blocked", True)),
            "missing_document_anchor_ids": list(document_anchor_guard.get("missing_document_anchor_ids") or []),
        },
    }
    return report_payload
