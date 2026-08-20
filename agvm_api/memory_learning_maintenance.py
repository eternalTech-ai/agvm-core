from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from typing import Any

from memory_learning import MEMORY_LEARNING_EVENT_SCHEMA_VERSION
from runtime_scope import current_brain_id


INGEST_LEARNING_REVIEW_SCHEMA_VERSION = "agvm.maintenance.ingest_learning_review.v1"
WHOLE_BRAIN_CURSOR_SCHEMA_VERSION = "agvm.maintenance.whole_brain_cursor.v1"


_EVENT_WEIGHTS: dict[str, float] = {
    "contradiction_detected": 22.0,
    "candidate_suppressed_duplicate": 19.0,
    "clarification_requested": 15.0,
    "clarification_answered": 9.0,
    "node_persisted": 13.0,
    "candidate_selected": 12.0,
    "candidate_rejected": 7.0,
    "candidate_merged": 16.0,
    "source_asset_created": 8.0,
    "source_unit_created": 7.0,
    "candidate_previewed": 5.0,
    "sleep_queue_created": 10.0,
    "evolve_queue_created": 11.0,
    "matrix_hint_created": 14.0,
    "query_quality_observation_created": 10.0,
    "node_shape_feedback_created": 13.0,
    "deduction_candidate_created": 12.0,
}


def _stable_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _string_list(value: Any, *, limit: int = 24) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, int, float)):
        values = [value]
    elif isinstance(value, dict):
        values = [value.get("id") or value.get("node_id") or value.get("key")]
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


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    return dict(payload) if isinstance(payload, dict) else {}


def _event_target_node_ids(event: dict[str, Any]) -> list[str]:
    payload = _event_payload(event)
    values: list[Any] = [
        event.get("persisted_node_id"),
        payload.get("persisted_node_id"),
        payload.get("node_id"),
        payload.get("target_node_id"),
    ]
    values.extend(_string_list(event.get("related_node_ids"), limit=24))
    values.extend(_string_list(event.get("duplicate_targets"), limit=24))
    values.extend(_string_list(event.get("contradiction_targets"), limit=24))
    values.extend(_string_list(payload.get("related_node_ids"), limit=24))
    values.extend(_string_list(payload.get("target_node_ids"), limit=24))
    values.extend(_string_list(payload.get("duplicate_targets"), limit=24))
    values.extend(_string_list(payload.get("contradiction_targets"), limit=24))
    return _string_list(values, limit=32)


def _source_ref_id(event: dict[str, Any]) -> str:
    payload = _event_payload(event)
    for key in ("source_ref_id", "source_reference_id"):
        text = str(payload.get(key) or "").strip()
        if text:
            return text
    operation_id = str(event.get("operation_id") or "").strip()
    if operation_id:
        return f"source_ref::{operation_id}"
    return ""


def _event_summary(event: dict[str, Any]) -> str:
    kind = str(event.get("event_kind") or "").strip()
    preview_id = str(event.get("preview_id") or "").strip()
    persisted_node_id = str(event.get("persisted_node_id") or "").strip()
    source_unit_id = str(event.get("source_unit_id") or "").strip()
    targets = _event_target_node_ids(event)
    if kind == "contradiction_detected":
        return f"Ingest found a contradiction candidate touching {', '.join(targets[:3]) or 'existing memory'}."
    if kind == "candidate_suppressed_duplicate":
        return f"Ingest suppressed a duplicate candidate against {', '.join(targets[:3]) or 'existing memory'}."
    if kind == "clarification_requested":
        return "Ingest requested human clarification before safe consolidation."
    if kind == "clarification_answered":
        return "A human clarification answer is available for maintenance review."
    if kind == "source_unit_created":
        return f"Source unit {source_unit_id or 'unknown'} was extracted and may need source-link review."
    if kind == "source_asset_created":
        return "A referenced asset was extracted and may need document/source coupling review."
    if kind == "node_persisted":
        return f"Node {persisted_node_id or 'unknown'} was persisted from a source preview."
    if kind == "matrix_hint_created":
        return "Ingest produced a matrix placement hint for later calibration review."
    if kind == "evolve_queue_created":
        return "Ingest queued topology/evolve review after source learning."
    if kind == "sleep_queue_created":
        return "Ingest queued sleep consolidation after source learning."
    if preview_id:
        return f"Ingest event {kind} references preview {preview_id}."
    return f"Ingest event {kind} is available for maintenance review."


def _compact_event(event: dict[str, Any]) -> dict[str, Any]:
    kind = str(event.get("event_kind") or "").strip()
    base_priority = _EVENT_WEIGHTS.get(kind, 1.0)
    explicit_priority = _safe_float(event.get("sleep_evolve_priority"), base_priority)
    priority = max(base_priority, explicit_priority if explicit_priority > 1.0 else explicit_priority * base_priority)
    payload = _event_payload(event)
    return {
        "event_id": str(event.get("event_id") or ""),
        "schema_version": str(event.get("schema_version") or MEMORY_LEARNING_EVENT_SCHEMA_VERSION),
        "event_kind": kind,
        "event_source": str(event.get("event_source") or ""),
        "operation_id": str(event.get("operation_id") or ""),
        "source_unit_id": str(event.get("source_unit_id") or ""),
        "source_asset_id": str(event.get("source_asset_id") or ""),
        "preview_id": str(event.get("preview_id") or ""),
        "persisted_node_id": str(event.get("persisted_node_id") or ""),
        "related_node_ids": _event_target_node_ids(event),
        "duplicate_targets": _string_list(event.get("duplicate_targets"), limit=12),
        "contradiction_targets": _string_list(event.get("contradiction_targets"), limit=12),
        "clarification_questions": list(event.get("clarification_questions") or [])[:5],
        "human_decision": event.get("human_decision"),
        "apply_decision": event.get("apply_decision"),
        "confidence": event.get("confidence"),
        "priority": round(priority, 4),
        "source_ref_id": _source_ref_id(event),
        "matrix_hint": dict(event.get("matrix_hint") or {}),
        "topology_hint": dict(event.get("topology_hint") or {}),
        "payload_summary": {
            key: payload.get(key)
            for key in (
                "candidate_role",
                "candidate_index",
                "claim_status",
                "memory_act_type",
                "source_kind",
                "source_uri",
                "reason",
            )
            if payload.get(key) is not None
        },
        "human_readable_evidence": _event_summary(event),
        "created_at": str(event.get("created_at") or ""),
    }


def _whole_brain_cursor(
    *,
    node_ids: list[str],
    candidate_node_ids: list[str],
    effective_limit: int,
    mode: str,
    cursor: str | None,
) -> dict[str, Any]:
    sorted_ids = sorted(_string_list(node_ids, limit=max(1, len(node_ids))))
    chunk_size = max(1, int(effective_limit or 80))
    cursor_text = str(cursor or "").strip()
    start_index = 0
    if cursor_text:
        for index, node_id in enumerate(sorted_ids):
            if node_id > cursor_text:
                start_index = index
                break
        else:
            start_index = len(sorted_ids)
    selected_chunk = sorted_ids[start_index : start_index + chunk_size]
    next_index = start_index + len(selected_chunk)
    next_cursor = selected_chunk[-1] if next_index < len(sorted_ids) and selected_chunk else None
    return {
        "schema_version": WHOLE_BRAIN_CURSOR_SCHEMA_VERSION,
        "mode": str(mode or "sleep_evolve"),
        "cursor": cursor_text or None,
        "full_node_count": len(sorted_ids),
        "chunk_size": chunk_size,
        "chunk_index": (start_index // chunk_size) if chunk_size else 0,
        "selected_node_ids": selected_chunk,
        "candidate_overlap_node_ids": [node_id for node_id in selected_chunk if node_id in set(candidate_node_ids)][:24],
        "node_id_start": selected_chunk[0] if selected_chunk else None,
        "node_id_end": selected_chunk[-1] if selected_chunk else None,
        "next_cursor": next_cursor,
        "complete": next_cursor is None,
        "policy": "bounded_eventual_whole_brain_coverage_without_loading_full_text",
    }


def build_ingest_learning_review(
    graph: dict[str, Any],
    *,
    mode: str = "sleep_evolve",
    preview_only: bool = True,
    max_events: int = 180,
    effective_node_limit: int = 80,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Build a non-mutating maintenance review from durable ingest feedback."""

    nodes = [dict(node) for node in list(graph.get("nodes") or []) if isinstance(node, dict)]
    node_ids = _string_list([node.get("id") for node in nodes], limit=max(1, len(nodes)))
    node_id_set = set(node_ids)
    brain_id = str(current_brain_id() or "default").strip()
    try:
        from sqlite_store import fetch_memory_learning_events

        events = fetch_memory_learning_events(brain_id=brain_id, limit=max_events)
    except Exception as exc:  # pragma: no cover - defensive adapter fallback.
        return {
            "schema_version": INGEST_LEARNING_REVIEW_SCHEMA_VERSION,
            "mode": str(mode or "sleep_evolve"),
            "preview_only": bool(preview_only),
            "brain_id": brain_id,
            "available": False,
            "event_count": 0,
            "events": [],
            "fetch_error": str(exc),
            "candidate_reasons_by_node_id": {},
            "candidate_scores_by_node_id": {},
            "whole_brain_cursor": _whole_brain_cursor(
                node_ids=node_ids,
                candidate_node_ids=[],
                effective_limit=effective_node_limit,
                mode=mode,
                cursor=cursor,
            ),
            "mutation_policy": "non_mutating_ingest_learning_review",
        }

    compact_events = [_compact_event(event) for event in events if isinstance(event, dict)]
    kind_histogram = dict(Counter(event["event_kind"] for event in compact_events))
    candidate_scores: dict[str, float] = defaultdict(float)
    candidate_reasons: dict[str, list[str]] = defaultdict(list)

    def add_node_reason(node_id: str, score: float, reason: str) -> None:
        if not node_id or node_id not in node_id_set:
            return
        candidate_scores[node_id] += score
        if reason not in candidate_reasons[node_id]:
            candidate_reasons[node_id].append(reason)

    for event in compact_events:
        kind = str(event.get("event_kind") or "")
        weight = _safe_float(event.get("priority"), _EVENT_WEIGHTS.get(kind, 1.0))
        reason = {
            "contradiction_detected": "ingest_contradiction",
            "candidate_suppressed_duplicate": "ingest_duplicate",
            "clarification_requested": "ingest_clarification_requested",
            "clarification_answered": "ingest_clarification_answered",
            "source_unit_created": "ingest_source_link",
            "source_asset_created": "ingest_source_asset",
            "node_persisted": "ingest_recent_persist",
            "matrix_hint_created": "ingest_matrix_hint",
            "evolve_queue_created": "ingest_evolve_queue",
            "sleep_queue_created": "ingest_sleep_queue",
        }.get(kind, f"ingest_{kind}")
        for node_id in _string_list(event.get("related_node_ids"), limit=32):
            add_node_reason(node_id, weight, reason)

    priority_events = sorted(
        compact_events,
        key=lambda event: (-_safe_float(event.get("priority")), str(event.get("event_id") or "")),
    )[:48]
    candidate_node_ids = sorted(candidate_scores, key=lambda node_id: (-candidate_scores[node_id], node_id))

    def by_kind(*kinds: str) -> list[dict[str, Any]]:
        wanted = set(kinds)
        return [event for event in priority_events if event.get("event_kind") in wanted]

    source_link_events = [
        event
        for event in priority_events
        if event.get("event_kind") in {"source_unit_created", "source_asset_created", "node_persisted", "candidate_previewed"}
        or str(event.get("source_ref_id") or "")
    ]
    topology_events = [
        event
        for event in priority_events
        if event.get("event_kind") in {"matrix_hint_created", "evolve_queue_created", "node_shape_feedback_created"}
        or dict(event.get("matrix_hint") or {})
        or dict(event.get("topology_hint") or {})
    ]

    cursor_payload = _whole_brain_cursor(
        node_ids=node_ids,
        candidate_node_ids=candidate_node_ids,
        effective_limit=effective_node_limit,
        mode=mode,
        cursor=cursor,
    )
    review_seed = {
        "brain_id": brain_id,
        "mode": mode,
        "event_ids": [event.get("event_id") for event in priority_events[:24]],
        "candidate_node_ids": candidate_node_ids[:24],
        "cursor": cursor_payload,
        "schema_version": INGEST_LEARNING_REVIEW_SCHEMA_VERSION,
    }
    return {
        "schema_version": INGEST_LEARNING_REVIEW_SCHEMA_VERSION,
        "review_id": f"ingest_learning_review::{_stable_hash(review_seed)}",
        "mode": str(mode or "sleep_evolve"),
        "preview_only": bool(preview_only),
        "brain_id": brain_id,
        "available": True,
        "event_count": len(compact_events),
        "priority_event_count": len(priority_events),
        "event_kind_histogram": kind_histogram,
        "priority_events": priority_events,
        "candidate_node_ids": candidate_node_ids[:80],
        "candidate_reasons_by_node_id": {node_id: reasons for node_id, reasons in candidate_reasons.items()},
        "candidate_scores_by_node_id": {node_id: round(score, 4) for node_id, score in candidate_scores.items()},
        "source_unit_ids": _string_list([event.get("source_unit_id") for event in compact_events], limit=80),
        "source_asset_ids": _string_list([event.get("source_asset_id") for event in compact_events], limit=80),
        "source_ref_ids": _string_list([event.get("source_ref_id") for event in compact_events], limit=80),
        "operation_ids": _string_list([event.get("operation_id") for event in compact_events], limit=80),
        "sleep_focus": {
            "duplicate_events": by_kind("candidate_suppressed_duplicate", "candidate_merged"),
            "contradiction_events": by_kind("contradiction_detected"),
            "clarification_events": by_kind("clarification_requested", "clarification_answered"),
            "source_link_events": source_link_events[:24],
        },
        "evolve_focus": {
            "topology_events": topology_events[:24],
            "matrix_hint_events": by_kind("matrix_hint_created"),
            "evolve_queue_events": by_kind("evolve_queue_created"),
            "repeated_operation_count": len([operation for operation, count in Counter(event.get("operation_id") for event in compact_events).items() if operation and count > 1]),
        },
        "whole_brain_cursor": cursor_payload,
        "human_readable_evidence": [event["human_readable_evidence"] for event in priority_events[:12]],
        "mutation_policy": "non_mutating_ingest_learning_review",
    }


__all__ = [
    "INGEST_LEARNING_REVIEW_SCHEMA_VERSION",
    "WHOLE_BRAIN_CURSOR_SCHEMA_VERSION",
    "build_ingest_learning_review",
]
