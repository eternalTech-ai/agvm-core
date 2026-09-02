# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import json
import time
from typing import Any, Callable


PUBLIC_V1_LANDING_SCHEMA_VERSION = "agvm.public_v1_landing_plan.v1"
_PUBLIC_V1_PROMPT_SCHEMA_VERSION = "agvm.public_v1_landing_prompt.v1"
_PUBLIC_V1_PROVIDER_SCHEMA_NAME = "agvm_public_v1_landing_plan_v1"


_COORDINATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "x": {"type": "number", "minimum": -1.0, "maximum": 1.0},
        "y": {"type": "number", "minimum": -1.0, "maximum": 1.0},
        "z": {"type": "number", "minimum": -1.0, "maximum": 1.0},
    },
}

_DESTINATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "destination_id": {"type": "string"},
        "label": {"type": "string"},
        "reason": {"type": "string"},
        "routing_intent": {"type": "string"},
        "expected_discovery": {"type": "string"},
        "hydration_policy": {"type": "string"},
        "region_ref": {"type": ["string", "null"]},
        "coordinate": {"anyOf": [_COORDINATE_SCHEMA, {"type": "null"}]},
        "novel_region_candidate": {"type": ["string", "null"]},
        "radius": {"type": "number", "minimum": 0.05, "maximum": 0.5},
        "execution_role": {"type": "string", "enum": ["primary", "reserve"]},
    },
}

_PUBLIC_V1_PROVIDER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "planner_summary": {"type": "string"},
        "inverse_answer_paths": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "path_id": {"type": "string"},
                    "mission_id": {"type": "string"},
                    "strand_id": {"type": "string"},
                    "answer_field": {"type": "string"},
                    "answer_hypothesis": {"type": "string"},
                    "goal": {"type": "string"},
                    "routing_intent": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "destinations": {"type": "array", "items": _DESTINATION_SCHEMA},
                    "preferred_edges": {"type": "array", "items": {"type": "string"}},
                    "stop_condition": {"type": "string"},
                },
            },
        },
        "uncertainty": {"type": "string"},
    },
}


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _text(value: Any, *, limit: int = 500) -> str:
    return str(value or "").strip()[:limit]


def _bounded_float(
    value: Any,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = default
    return round(max(minimum, min(maximum, numeric)), 6)


def _dedupe(values: list[Any], *, limit: int = 24) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = _text(value, limit=240)
        if not item or item in seen:
            continue
        seen.add(item)
        output.append(item)
        if len(output) >= limit:
            break
    return output


def _bounded_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 4:
        return _text(value, limit=320)
    if isinstance(value, dict):
        return {
            _text(key, limit=80): _bounded_value(item, depth=depth + 1)
            for key, item in list(value.items())[:24]
        }
    if isinstance(value, (list, tuple)):
        return [_bounded_value(item, depth=depth + 1) for item in list(value)[:24]]
    if isinstance(value, str):
        return _text(value, limit=700)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _text(value, limit=320)


def _compact_spatial_brief(value: Any) -> dict[str, Any]:
    brief = _as_dict(value)
    atlas = _as_dict(brief.get("atlas_summary"))
    topology = _as_dict(brief.get("topology_overlay_summary"))
    return {
        "schema_version": brief.get("schema_version"),
        "brain_revision": brief.get("brain_revision"),
        "revision": brief.get("revision"),
        "source_snapshot_version": brief.get("source_snapshot_version"),
        "source_hash": brief.get("source_hash"),
        "coordinate_system": _bounded_value(brief.get("coordinate_system")),
        "nuclei": _bounded_value(brief.get("nuclei")),
        "atlas_summary": {
            "bucket_count": atlas.get("bucket_count"),
            "node_count": atlas.get("node_count"),
            "sample_buckets": _bounded_value(_as_list(atlas.get("sample_buckets"))[:24]),
        },
        "highway_gateways": _bounded_value(_as_list(brief.get("highway_gateways"))[:24]),
        "topology_overlay_summary": {
            "density_lobes": _bounded_value(_as_list(topology.get("density_lobes"))[:16]),
            "active_highways": _bounded_value(_as_list(topology.get("active_highways"))[:24]),
            "bridge_corridors": _bounded_value(_as_list(topology.get("bridge_corridors"))[:16]),
            "attraction_priors": _bounded_value(_as_list(topology.get("attraction_priors"))[:12]),
            "repulsion_priors": _bounded_value(_as_list(topology.get("repulsion_priors"))[:12]),
        },
        "spatial_readiness_contract": _bounded_value(brief.get("spatial_readiness_contract")),
        "prompt_brief": _text(brief.get("prompt_brief"), limit=800),
    }


def _compact_semantic_contract(value: Any) -> dict[str, Any]:
    contract = _as_dict(value)
    return {
        key: _bounded_value(contract.get(key))
        for key in (
            "semantic_authority",
            "semantic_authority_v2",
            "root_query_text",
            "answerability_contract",
            "mission_plan_v2",
            "required_sections",
            "identity_hints",
            "named_targets",
        )
        if contract.get(key) not in (None, "", [], {})
    }


def _normalize_coordinate(value: Any) -> dict[str, float] | None:
    coordinate = _as_dict(value)
    if not all(axis in coordinate for axis in ("x", "y", "z")):
        return None
    try:
        return {
            axis: round(max(-1.0, min(1.0, float(coordinate[axis]))), 6)
            for axis in ("x", "y", "z")
        }
    except (TypeError, ValueError):
        return None


def _normalize_destination(value: Any, *, index: int) -> dict[str, Any] | None:
    destination = _as_dict(value)
    coordinate = _normalize_coordinate(
        destination.get("coordinate") or destination.get("landing_coordinate")
    )
    region_ref = _text(
        destination.get("region_ref") or destination.get("landing_region_ref"),
        limit=180,
    )
    novel_region = _text(destination.get("novel_region_candidate"), limit=220)
    if not (coordinate or region_ref or novel_region):
        return None
    execution_role = _text(destination.get("execution_role"), limit=20).lower()
    if execution_role not in {"primary", "reserve"}:
        execution_role = "primary"
    return {
        "destination_id": _text(destination.get("destination_id"), limit=100)
        or f"destination-{index}",
        "label": _text(destination.get("label"), limit=180),
        "reason": _text(destination.get("reason"), limit=500),
        "routing_intent": _text(destination.get("routing_intent"), limit=420),
        "expected_discovery": _text(destination.get("expected_discovery"), limit=700),
        "hydration_policy": _text(destination.get("hydration_policy"), limit=420),
        "region_ref": region_ref or None,
        "coordinate": coordinate,
        "novel_region_candidate": novel_region or None,
        "radius": _bounded_float(
            destination.get("radius"),
            default=0.2,
            minimum=0.05,
            maximum=0.5,
        ),
        "execution_role": execution_role,
    }


def _normalize_path(value: Any, *, index: int) -> dict[str, Any] | None:
    path = _as_dict(value)
    destinations = [
        destination
        for destination_index, item in enumerate(
            _as_list(path.get("destinations") or path.get("destination_queue")),
            start=1,
        )
        if (
            destination := _normalize_destination(item, index=destination_index)
        )
        is not None
    ]
    if not destinations:
        implicit = _normalize_destination(
            {
                "destination_id": f"path-{index}-landing",
                "label": path.get("goal") or path.get("answer_field"),
                "reason": path.get("reason"),
                "routing_intent": path.get("routing_intent"),
                "expected_discovery": path.get("answer_hypothesis"),
                "hydration_policy": path.get("hydration_policy"),
                "region_ref": path.get("landing_region_ref") or path.get("region_ref"),
                "coordinate": path.get("landing_coordinate") or path.get("coordinate"),
                "novel_region_candidate": path.get("novel_region_candidate"),
                "radius": path.get("radius"),
                "execution_role": "primary",
            },
            index=1,
        )
        if implicit:
            destinations = [implicit]
    if not destinations:
        return None
    first = destinations[0]
    preferred_edges = _dedupe(_as_list(path.get("preferred_edges")), limit=12)
    return {
        "path_id": _text(path.get("path_id") or path.get("id"), limit=100)
        or f"public-path-{index}",
        "mission_id": _text(path.get("mission_id"), limit=120) or None,
        "strand_id": _text(path.get("strand_id"), limit=120) or None,
        "answer_field": _text(path.get("answer_field"), limit=160),
        "answer_hypothesis": _text(path.get("answer_hypothesis"), limit=700),
        "goal": _text(path.get("goal"), limit=240),
        "routing_intent": _text(path.get("routing_intent"), limit=420),
        "confidence": _bounded_float(
            path.get("confidence"),
            default=0.68,
            minimum=0.0,
            maximum=1.0,
        ),
        "landing_region_ref": first.get("region_ref"),
        "landing_coordinate": first.get("coordinate"),
        "novel_region_candidate": first.get("novel_region_candidate"),
        "radius": first.get("radius"),
        "destinations": destinations,
        "destination_queue": destinations,
        "waypoints": [
            {
                "waypoint_id": str(destination.get("destination_id") or f"waypoint-{waypoint_index}"),
                "region_ref": destination.get("region_ref"),
                "coordinate": destination.get("coordinate"),
                "radius": destination.get("radius"),
                "reason": destination.get("reason"),
            }
            for waypoint_index, destination in enumerate(destinations[1:], start=1)
        ],
        "preferred_edges": preferred_edges,
        "stop_condition": _text(path.get("stop_condition"), limit=420),
        "reason": _text(path.get("reason") or first.get("reason"), limit=500) or None,
        "expected_discovery": _text(
            path.get("expected_discovery") or first.get("expected_discovery"),
            limit=700,
        )
        or None,
        "hydration_policy": _text(
            path.get("hydration_policy") or first.get("hydration_policy"),
            limit=420,
        )
        or None,
    }


def _expected_strand_ids(answer_strands: Any) -> list[str]:
    return _dedupe(
        [
            _as_dict(strand).get("mission_id") or _as_dict(strand).get("strand_id")
            for strand in _as_list(answer_strands)
            if isinstance(strand, dict)
        ],
        limit=24,
    )


def _blocked_contract(
    *,
    query_text: str,
    retrieval_mode: str,
    brain_revision: str | None,
    spatial_brief: dict[str, Any],
    source: str,
    missing_reasons: list[str],
    started_at: float,
) -> dict[str, Any]:
    return {
        "schema_version": PUBLIC_V1_LANDING_SCHEMA_VERSION,
        "status": "deferred" if source == "deferred" else "blocked",
        "materialization_state": "deferred" if source == "deferred" else "blocked",
        "source": source,
        "materialized": False,
        "certifiable": False,
        "query_present": bool(_text(query_text)),
        "query_text": _text(query_text),
        "retrieval_mode": retrieval_mode,
        "brain_revision": brain_revision,
        "metamemory_revision": spatial_brief.get("source_snapshot_version"),
        "metamemory_spatial_revision": spatial_brief.get("revision"),
        "inverse_answer_paths": [],
        "missing_reasons": _dedupe(missing_reasons, limit=16),
        "planner_latency_ms": round((time.perf_counter() - started_at) * 1000.0, 2),
        "metrics": {"ai_landing_count": 0, "ai_path_count": 0},
        "cache": {"status": "disabled", "hit": False},
        "routing_authority": "ai_coordinate_first",
        "fallback_used": False,
        "heuristic_result_exposed": False,
    }


def build_public_v1_landing_contract(
    *,
    query_text: str,
    retrieval_mode: str,
    brain_revision: str | None,
    semantic_contract: dict[str, Any] | None = None,
    semantic_contract_runtime: dict[str, Any] | None = None,
    answer_strands: list[dict[str, Any]] | None = None,
    identity_hints: dict[str, Any] | None = None,
    metamemory_spatial_brief: dict[str, Any] | None = None,
    mode_budget: dict[str, Any] | None = None,
    allow_ai: bool = True,
    deferred: bool = False,
    timeout: float | None = None,
    structured_json_fn: Callable[..., tuple[dict[str, Any] | None, str | None]] | None = None,
    **_kwargs: Any,
) -> dict[str, Any]:
    """Build a public, AI-authored coordinate-first landing plan.

    The provider selects semantic landings and ordered path destinations from
    Metamemory. Core only validates and normalizes that plan before executing
    sphere/tube scans and graph traversal. If AI is unavailable, this contract
    fails closed; it never manufactures deterministic landing geometry.
    """

    started_at = time.perf_counter()
    mode = _text(retrieval_mode or "balanced", limit=32).lower() or "balanced"
    spatial_brief = _as_dict(metamemory_spatial_brief)
    budget = _as_dict(mode_budget)
    if deferred or bool(budget.get("cache_only")):
        return _blocked_contract(
            query_text=query_text,
            retrieval_mode=mode,
            brain_revision=brain_revision,
            spatial_brief=spatial_brief,
            source="deferred",
            missing_reasons=["ai_spatial_contract_deferred"],
            started_at=started_at,
        )
    if spatial_brief.get("schema_version") != "agvm.metamemory_spatial_brief.v1":
        return _blocked_contract(
            query_text=query_text,
            retrieval_mode=mode,
            brain_revision=brain_revision,
            spatial_brief=spatial_brief,
            source="blocked_missing_metamemory",
            missing_reasons=["metamemory_spatial_brief_missing"],
            started_at=started_at,
        )
    expected_strands = _expected_strand_ids(answer_strands)
    if not _as_list(answer_strands):
        return _blocked_contract(
            query_text=query_text,
            retrieval_mode=mode,
            brain_revision=brain_revision,
            spatial_brief=spatial_brief,
            source="blocked_missing_answer_strands",
            missing_reasons=["inverse_answer_strands_missing"],
            started_at=started_at,
        )
    if not allow_ai or structured_json_fn is None:
        return _blocked_contract(
            query_text=query_text,
            retrieval_mode=mode,
            brain_revision=brain_revision,
            spatial_brief=spatial_brief,
            source="ai_unavailable",
            missing_reasons=["llm_disabled" if not allow_ai else "public_ai_provider_not_bound"],
            started_at=started_at,
        )

    prompt_payload = {
        "schema_version": _PUBLIC_V1_PROMPT_SCHEMA_VERSION,
        "query": _text(query_text, limit=700),
        "retrieval_mode": mode,
        "mode_budget": _bounded_value(budget),
        "semantic_contract": _compact_semantic_contract(semantic_contract),
        "semantic_contract_runtime": _compact_semantic_contract(semantic_contract_runtime),
        "answer_strands": _bounded_value(_as_list(answer_strands)),
        "identity_hints": _bounded_value(identity_hints),
        "metamemory_spatial_brief": _compact_spatial_brief(spatial_brief),
        "execution_contract": {
            "planner_authority": "ai_coordinate_first",
            "backend_operations": [
                "sphere_scan",
                "path_tube_scan",
                "local_link_walk",
                "highway_jump",
                "evidence_edge_follow",
            ],
            "hydrate_only_promoted_evidence": True,
            "metadata_is_not_routing_authority": True,
            "deterministic_landing_fallback_allowed": False,
        },
    }
    stage_timeout = timeout
    if stage_timeout is None:
        stage_timeout = {
            "flash": 6.0,
            "balanced": 12.0,
            "heavy": 18.0,
            "forensic": 24.0,
        }.get(mode, 12.0)
    payload, provider_error = structured_json_fn(
        system_prompt=(
            "You are the public AGVM coordinate-first landing planner. Do not answer the user. "
            "For every admitted answer strand, reuse its mission_id and strand_id and author one inverse answer path. "
            "Choose one or more ordered destinations from the Metamemory spatial brief using region_ref or an explicit x/y/z coordinate; "
            "never route by memory_type, guide_area, display tags, or generic taxonomy. The backend will scan landing spheres and path tubes, "
            "walk local links, jump useful highways, follow evidence edges, and hydrate source anchors only after evidence promotion. "
            "Use preferred_edges to express which topology roads matter. State a useful evidence stop condition. "
            "If no listed region fits, use novel_region_candidate rather than inventing a coordinate. "
            "Do not create a deterministic or metadata-based fallback path."
        ),
        user_prompt=json.dumps(prompt_payload, ensure_ascii=False, separators=(",", ":")),
        schema_name=_PUBLIC_V1_PROVIDER_SCHEMA_NAME,
        schema=_PUBLIC_V1_PROVIDER_SCHEMA,
        timeout=max(0.1, float(stage_timeout)),
        role="ai_spatial",
        max_output_tokens=1400,
    )
    if provider_error or not isinstance(payload, dict):
        reason = _text(provider_error or "invalid_json", limit=220).replace(" ", "_")
        return _blocked_contract(
            query_text=query_text,
            retrieval_mode=mode,
            brain_revision=brain_revision,
            spatial_brief=spatial_brief,
            source="llm_error",
            missing_reasons=[f"ai_spatial_provider_error:{reason}"],
            started_at=started_at,
        )

    paths = [
        path
        for path_index, item in enumerate(_as_list(payload.get("inverse_answer_paths")), start=1)
        if (path := _normalize_path(item, index=path_index)) is not None
    ]
    actual_strands = {
        str(path.get("mission_id") or path.get("strand_id") or "").strip()
        for path in paths
        if str(path.get("mission_id") or path.get("strand_id") or "").strip()
    }
    missing_reasons: list[str] = []
    if not paths:
        missing_reasons.append("inverse_answer_paths_missing")
    missing_strands = [strand for strand in expected_strands if strand not in actual_strands]
    if missing_strands:
        missing_reasons.append(f"answer_strand_paths_missing:{','.join(missing_strands)}")
    readiness = _as_dict(spatial_brief.get("spatial_readiness_contract"))
    if readiness and not bool(readiness.get("certifiable")):
        missing_reasons.append("metamemory_spatial_brief_incomplete")
        missing_reasons.extend(_as_list(readiness.get("missing_reasons")))
        missing_reasons.extend(_as_list(readiness.get("stale_reasons")))
    materialized = bool(paths and not missing_reasons)
    landing_count = sum(len(_as_list(path.get("destinations"))) for path in paths)
    waypoint_count = sum(len(_as_list(path.get("waypoints"))) for path in paths)
    return {
        "schema_version": PUBLIC_V1_LANDING_SCHEMA_VERSION,
        "status": "materialized" if materialized else "blocked",
        "materialization_state": "materialized" if materialized else "blocked",
        "source": "fresh_llm",
        "materialized": materialized,
        "certifiable": materialized,
        "query_present": bool(_text(query_text)),
        "query_text": _text(query_text),
        "retrieval_mode": mode,
        "brain_revision": brain_revision,
        "metamemory_revision": spatial_brief.get("source_snapshot_version"),
        "metamemory_spatial_revision": spatial_brief.get("revision"),
        "metamemory_spatial_hash": spatial_brief.get("source_hash"),
        "planner_summary": _text(payload.get("planner_summary"), limit=700),
        "uncertainty": _text(payload.get("uncertainty"), limit=420),
        "inverse_answer_paths": paths,
        "missing_reasons": _dedupe(missing_reasons, limit=16),
        "planner_latency_ms": round((time.perf_counter() - started_at) * 1000.0, 2),
        "metrics": {
            "ai_landing_count": landing_count,
            "ai_path_count": len(paths),
            "destination_count": landing_count,
            "waypoint_count": waypoint_count,
            "preferred_edge_count": sum(
                len(_as_list(path.get("preferred_edges"))) for path in paths
            ),
        },
        "cache": {"status": "disabled", "hit": False},
        "routing_authority": "ai_coordinate_first",
        "fallback_used": False,
        "heuristic_result_exposed": False,
    }
