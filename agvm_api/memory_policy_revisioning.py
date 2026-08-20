from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any

from storage import utc_timestamp


MEMORY_POLICY_REVISION_SCHEMA_VERSION = "agvm.memory_policy_revision.v1"
MEMORY_POLICY_REVISION_PREVIEW_SCHEMA_VERSION = "agvm.memory_policy_revision_preview.v1"


def _stable_hash(payload: Any, *, length: int = 16) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:length]


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _string_list(value: Any, *, limit: int = 48) -> list[str]:
    if value is None:
        values: list[Any] = []
    elif isinstance(value, (str, int, float)):
        values = [value]
    elif isinstance(value, dict):
        values = [value.get("event_id") or value.get("id") or value.get("node_id")]
    else:
        values = list(value or [])
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def _priority_events(ingest_learning_review: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in list(dict(ingest_learning_review or {}).get("priority_events") or [])
        if isinstance(item, dict)
    ]


def _event_kind_histogram(events: list[dict[str, Any]], ingest_learning_review: dict[str, Any] | None) -> dict[str, int]:
    histogram = Counter(str(event.get("event_kind") or "") for event in events if str(event.get("event_kind") or ""))
    for key, value in dict(dict(ingest_learning_review or {}).get("event_kind_histogram") or {}).items():
        if str(key):
            histogram[str(key)] += _safe_int(value)
    return dict(histogram)


def _supporting_event_ids(events: list[dict[str, Any]], *extra: Any) -> list[str]:
    values: list[Any] = [event.get("event_id") for event in events]
    for item in extra:
        values.extend(_string_list(item, limit=48))
    return _string_list(values, limit=80)


def _rule(
    *,
    rule_id: str,
    action: str,
    reason: str,
    evidence: list[str],
    confidence: float,
    target: str = "",
    enabled: bool = True,
) -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "enabled": bool(enabled),
        "target": target,
        "action": action,
        "reason": reason,
        "evidence_event_ids": _string_list(evidence, limit=24),
        "confidence": round(max(0.0, min(0.95, confidence)), 4),
        "review_required": True,
    }


def _events_by_kind(events: list[dict[str, Any]], *kinds: str) -> list[dict[str, Any]]:
    wanted = {str(kind) for kind in kinds}
    return [event for event in events if str(event.get("event_kind") or "") in wanted]


def _deduction_candidates(deduction_mining: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in list(dict(deduction_mining or {}).get("candidates") or [])
        if isinstance(item, dict)
    ]


def _proposal_ids(maintenance_proposals: list[dict[str, Any]] | None, *, source_slice: str | None = None) -> list[str]:
    rows = [dict(item) for item in list(maintenance_proposals or []) if isinstance(item, dict)]
    if source_slice:
        rows = [row for row in rows if str(row.get("source_slice") or "") == source_slice]
    return _string_list([row.get("proposal_id") for row in rows], limit=48)


def _quality_delta(quality_before: dict[str, Any] | None, quality_after: dict[str, Any] | None) -> dict[str, Any]:
    before = dict(quality_before or {})
    after = dict(quality_after or {})
    before_score = _safe_float(before.get("overall") or before.get("score") or before.get("overall_quality_score"))
    after_score = _safe_float(after.get("overall") or after.get("score") or after.get("overall_quality_score"), before_score)
    return {
        "schema_version": "agvm.memory_policy_quality_delta.v1",
        "quality_before_score": round(before_score, 6),
        "quality_after_score": round(after_score, 6),
        "observed_delta": round(after_score - before_score, 6),
        "measured_policy_effect": False,
        "preview_only": True,
    }


def build_memory_policy_revision_candidate(
    *,
    brain_id: str | None,
    mode: str,
    preview_only: bool,
    ingest_learning_review: dict[str, Any] | None,
    retrieval_gap_review: dict[str, Any] | None,
    trace_insights: dict[str, Any] | None,
    deduction_mining: dict[str, Any] | None,
    maintenance_proposals: list[dict[str, Any]] | None,
    quality_before: dict[str, Any] | None = None,
    quality_after: dict[str, Any] | None = None,
    active_policy_revision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a reviewable per-brain memory policy revision candidate.

    The builder is intentionally non-mutating. It converts repeated learning
    signals into inspectable policy rules; activation belongs to the explicit
    store/apply boundary.
    """

    normalized_brain_id = str(brain_id or "default").strip() or "default"
    events = _priority_events(ingest_learning_review)
    histogram = _event_kind_histogram(events, ingest_learning_review)
    retrieval_review = dict(retrieval_gap_review or {})
    retrieval_summary = dict(retrieval_review.get("gap_reasons") or {})
    if not retrieval_summary:
        retrieval_summary = dict(dict(trace_insights or {}).get("retrieval_gap_summary") or {}).get("gap_reasons", {}) or {}
    deductions = _deduction_candidates(deduction_mining)
    deduction_support_ids = _string_list([candidate.get("candidate_id") for candidate in deductions], limit=48)
    all_support_event_ids = _supporting_event_ids(events, deduction_support_ids, _proposal_ids(maintenance_proposals))

    source_events = _events_by_kind(events, "source_unit_created", "source_asset_created", "node_persisted", "candidate_previewed")
    contradiction_events = _events_by_kind(events, "contradiction_detected")
    clarification_events = _events_by_kind(events, "clarification_requested", "clarification_answered")
    duplicate_events = _events_by_kind(events, "candidate_suppressed_duplicate", "candidate_merged")
    matrix_events = _events_by_kind(events, "matrix_hint_created", "node_shape_feedback_created")
    query_quality_events = _events_by_kind(events, "query_quality_observation_created", "node_shape_feedback_created")

    ingest_rules: list[dict[str, Any]] = []
    if source_events:
        ingest_rules.append(
            _rule(
                rule_id="preserve_source_trace_for_future_memory",
                target="source_refs_and_raw_anchors",
                action="retain_source_refs_and_reviewable_raw_anchors_when_policy_allows",
                reason="Recent ingest created source units/assets/nodes that later maintenance must still hydrate.",
                evidence=[event.get("event_id") for event in source_events],
                confidence=0.68 + min(0.18, len(source_events) * 0.02),
            )
        )
    if duplicate_events:
        ingest_rules.append(
            _rule(
                rule_id="raise_existing_memory_scan_strictness",
                target="duplicate_prevention",
                action="compare_new_candidates_against_existing_memory_before_preview_selection",
                reason="Duplicate pressure means future Grow should be stricter before staging similar memories.",
                evidence=[event.get("event_id") for event in duplicate_events],
                confidence=0.7 + min(0.18, len(duplicate_events) * 0.03),
            )
        )
    if clarification_events:
        ingest_rules.append(
            _rule(
                rule_id="pause_on_source_scope_ambiguity",
                target="clarification_gate",
                action="ask_one_source_scope_question_before_memory_authority_when_scope_is_unclear",
                reason="Human clarification appeared in recent ingest, so future writes should preserve the question chain.",
                evidence=[event.get("event_id") for event in clarification_events],
                confidence=0.72 + min(0.16, len(clarification_events) * 0.025),
            )
        )
    if not ingest_rules:
        ingest_rules.append(
            _rule(
                rule_id="default_reviewable_memory_shape",
                target="memory_shape",
                action="prefer_self_contained_memory_text_with_source_refs_when_available",
                reason="No stronger per-brain ingest preference has enough support yet.",
                evidence=all_support_event_ids,
                confidence=0.51,
            )
        )

    retrieval_rules: list[dict[str, Any]] = []
    if retrieval_summary or query_quality_events:
        retrieval_rules.append(
            _rule(
                rule_id="use_recent_quality_feedback_for_query_planning",
                target="retrieve_context",
                action="bias_required_slots_and_hot_context_from_recent_quality_observations_without_hiding_no_match",
                reason="Retrieval/query quality observations are available and should affect future planning.",
                evidence=[event.get("event_id") for event in query_quality_events] + _proposal_ids(maintenance_proposals, source_slice="M8"),
                confidence=0.66 + min(0.2, len(query_quality_events) * 0.04 + len(retrieval_summary) * 0.03),
            )
        )
    if any("document" in str(key).lower() for key in retrieval_summary):
        retrieval_rules.append(
            _rule(
                rule_id="separate_document_refs_from_personal_context",
                target="document_retrieve",
                action="keep document refs available for hydration while preserving normal context slots separately",
                reason="Document-related retrieval gaps should not pollute personal/context memories.",
                evidence=all_support_event_ids,
                confidence=0.72,
            )
        )

    source_rules = [
        _rule(
            rule_id="bounded_source_expansion_with_review",
            target="source_investigation",
            action="crawl_or_expand_user_supplied_sources_with_budget_and_clarification_when_scope_changes",
            reason="Source expansion should improve memory quality without unbounded background exploration.",
            evidence=[event.get("event_id") for event in source_events],
            confidence=0.62 + min(0.18, len(source_events) * 0.025),
            enabled=bool(source_events),
        )
    ]

    deduction_rules: list[dict[str, Any]] = []
    if deductions:
        unsafe_count = len([candidate for candidate in deductions if str(candidate.get("classification") or "") in {"hypothesis", "question", "contradiction"}])
        deduction_rules.append(
            _rule(
                rule_id="deductions_remain_review_locked",
                target="inference_authority",
                action="allow_deductions_for_planning_but_block_high_confidence_answers_until_approved_or_source_supported",
                reason="Sleep produced reviewable deduction candidates; they should inform planning, not visible factual authority.",
                evidence=deduction_support_ids,
                confidence=0.76 + min(0.16, len(deductions) * 0.02 + unsafe_count * 0.03),
            )
        )
    if contradiction_events:
        deduction_rules.append(
            _rule(
                rule_id="contradictions_require_scope_resolution",
                target="contradiction_policy",
                action="ask_or_stage_resolution_before superseding_or_answering_from_conflicting_claims",
                reason="Contradiction feedback must produce clarification or supersession review before answer authority.",
                evidence=[event.get("event_id") for event in contradiction_events],
                confidence=0.84,
            )
        )

    sleep_rules = [
        _rule(
            rule_id="sleep_reviews_feedback_before_merge_or_deduction",
            target="sleep",
            action="consume_ingest_feedback_query_observations_and_deduction_candidates_before_merge_or_hot_promotion",
            reason="Sleep should use recorded feedback instead of only graph shape.",
            evidence=all_support_event_ids,
            confidence=0.68 + min(0.18, len(all_support_event_ids) * 0.01),
        )
    ]
    evolve_rules = [
        _rule(
            rule_id="evolve_uses_topology_and_matrix_hints",
            target="evolve",
            action="convert repeated placement_topology_feedback_into_reviewable_matrix_or_topology_candidates",
            reason="Matrix and topology hints should become explicit previews, not hidden coordinate edits.",
            evidence=[event.get("event_id") for event in matrix_events],
            confidence=0.64 + min(0.2, len(matrix_events) * 0.04),
            enabled=bool(matrix_events),
        )
    ]
    matrix_rules = [
        _rule(
            rule_id="matrix_policy_requires_active_revision_context",
            target="matrix_calibration",
            action="future_node_placement_should_use_active_matrix_and_topology_revision_when_available",
            reason="Per-brain geometry must be explicit and versioned before it can affect future placement.",
            evidence=[event.get("event_id") for event in matrix_events],
            confidence=0.7 if matrix_events else 0.56,
        )
    ]

    quality_delta = _quality_delta(quality_before, quality_after)
    active_parent_id = str(dict(active_policy_revision or {}).get("policy_revision_id") or "").strip() or None
    seed = {
        "brain_id": normalized_brain_id,
        "parent_policy_revision_id": active_parent_id,
        "histogram": histogram,
        "retrieval_summary": retrieval_summary,
        "deduction_candidate_ids": deduction_support_ids,
        "ingest_rules": ingest_rules,
        "retrieval_rules": retrieval_rules,
        "source_rules": source_rules,
        "deduction_rules": deduction_rules,
        "sleep_rules": sleep_rules,
        "evolve_rules": evolve_rules,
        "matrix_rules": matrix_rules,
        "schema_version": MEMORY_POLICY_REVISION_SCHEMA_VERSION,
    }
    policy_revision_id = f"memory_policy_revision::{_stable_hash(seed)}"
    proposed_rule_count = sum(
        len(rules)
        for rules in (ingest_rules, retrieval_rules, source_rules, deduction_rules, sleep_rules, evolve_rules, matrix_rules)
    )
    has_signal = bool(
        all_support_event_ids
        or retrieval_summary
        or deductions
        or _safe_int(dict(ingest_learning_review or {}).get("event_count") or 0) > 0
    )
    revision = {
        "schema_version": MEMORY_POLICY_REVISION_SCHEMA_VERSION,
        "policy_revision_id": policy_revision_id,
        "brain_id": normalized_brain_id,
        "parent_policy_revision_id": active_parent_id,
        "policy_scope": "brain",
        "ingest_rules": {"rules": ingest_rules},
        "retrieval_rules": {"rules": retrieval_rules},
        "source_rules": {"rules": source_rules},
        "deduction_rules": {"rules": deduction_rules},
        "sleep_rules": {"rules": sleep_rules},
        "evolve_rules": {"rules": evolve_rules},
        "matrix_rules": {"rules": matrix_rules},
        "supporting_event_ids": all_support_event_ids,
        "quality_before": dict(quality_before or {}),
        "quality_after": {
            **dict(quality_after or {}),
            "policy_preview": quality_delta,
            "expected_improvement_axes": [
                axis
                for axis, enabled in {
                    "source_trace_continuity": bool(source_events),
                    "duplicate_reduction": bool(duplicate_events),
                    "clarification_precision": bool(clarification_events or contradiction_events),
                    "retrieval_contract_fit": bool(retrieval_summary or query_quality_events),
                    "deduction_safety": bool(deductions),
                    "placement_policy_alignment": bool(matrix_events),
                }.items()
                if enabled
            ],
        },
        "status": "candidate",
        "apply_policy": "preview_apply_required",
        "rollback_payload": {
            "schema_version": "agvm.memory_policy_revision_rollback_payload.v1",
            "parent_policy_revision_id": active_parent_id,
            "candidate_only": True,
        },
        "created_at": utc_timestamp(),
        "activated_at": None,
    }
    return {
        "schema_version": MEMORY_POLICY_REVISION_PREVIEW_SCHEMA_VERSION,
        "brain_id": normalized_brain_id,
        "mode": str(mode or "sleep_evolve"),
        "preview_only": bool(preview_only),
        "available": has_signal,
        "memory_policy_revision": revision,
        "candidate_summary": {
            "policy_revision_id": policy_revision_id,
            "parent_policy_revision_id": active_parent_id,
            "supporting_event_count": len(all_support_event_ids),
            "proposed_rule_count": proposed_rule_count,
            "event_kind_histogram": histogram,
            "deduction_candidate_count": len(deductions),
            "retrieval_gap_reason_count": len(retrieval_summary),
        },
        "activation_gate": {
            "requires_explicit_apply": True,
            "requires_quality_gate": True,
            "requires_rollback_payload": True,
            "hidden_activation_allowed": False,
            "candidate_can_change_future_behavior_before_activation": False,
        },
        "local_cloud_parity": {
            "same_policy_revision_contract": True,
            "storage_adapter_may_differ": True,
            "automatic_activation_requires_cloud_or_operator_policy": True,
        },
        "mutation_policy": {
            "mutates_graph": False,
            "activates_policy": False,
            "auto_apply_allowed": False,
        },
    }


__all__ = [
    "MEMORY_POLICY_REVISION_PREVIEW_SCHEMA_VERSION",
    "MEMORY_POLICY_REVISION_SCHEMA_VERSION",
    "build_memory_policy_revision_candidate",
]
