from __future__ import annotations

import concurrent.futures
import hashlib
import json
import threading
import time
import unicodedata
from copy import deepcopy
from typing import Any, Callable

from llm import llm_enabled, retrieval_model, structured_json
from runtime_scope import current_data_dir


AI_SPATIAL_LANDING_CONTRACT_SCHEMA_VERSION = "agvm.ai_spatial_landing_contract.v1"
_AI_SPATIAL_CONTRACT_CACHE_TTL_SECONDS = 30 * 60
_AI_SPATIAL_CONTRACT_CACHE_MAX_ITEMS = 128
_AI_SPATIAL_CONTRACT_DISK_CACHE_FILENAME = "ai_spatial_contract_cache.v1.json"
_AI_SPATIAL_CONTRACT_DISK_CACHE_SCHEMA = "agvm.ai_spatial_contract_cache_store.v1"
_AI_SPATIAL_CONTRACT_DISK_CACHE_MAX_ITEMS = 512
_AI_SPATIAL_CONTRACT_CACHE_LOCK = threading.Lock()
_AI_SPATIAL_CONTRACT_CACHE: dict[str, dict[str, Any]] = {}


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _text(value: Any, *, limit: int | None = None) -> str:
    text = str(value or "").strip()
    if limit is not None and len(text) > limit:
        return text[: max(0, limit - 3)].rstrip() + "..."
    return text


def _bounded_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = default
    return round(max(minimum, min(maximum, numeric)), 6)


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        numeric = default
    return max(minimum, min(maximum, numeric))


def _stable_hash(payload: Any) -> str:
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _fold_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_only = "".join(char for char in normalized if not unicodedata.combining(char))
    return " ".join("".join(char.lower() if char.isalnum() else " " for char in ascii_only).split())


def _coordinate(value: Any) -> dict[str, float] | None:
    payload = _as_dict(value)
    if not payload:
        return None
    if not any(axis in payload for axis in ("x", "y", "z")):
        return None
    return {
        "x": _bounded_float(payload.get("x"), default=0.0, minimum=-1.0, maximum=1.0),
        "y": _bounded_float(payload.get("y"), default=0.0, minimum=-1.0, maximum=1.0),
        "z": _bounded_float(payload.get("z"), default=0.0, minimum=-1.0, maximum=1.0),
    }


def _dedupe_text(values: list[Any], *, limit: int = 12, text_limit: int = 160) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        text = _text(_as_dict(value).get("id") or _as_dict(value).get("label") or value, limit=text_limit)
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        output.append(text)
        if len(output) >= limit:
            break
    return output


def _runtime_fast_spatial_planning(mode_budget: dict[str, Any] | None) -> bool:
    payload = _as_dict(mode_budget)
    source = _text(payload.get("source"), limit=120).lower()
    if payload.get("cache_only"):
        return True
    if payload.get("first_payload_wait_seconds") is not None or payload.get("pre_route_wait_seconds") is not None:
        return True
    return source.startswith(("preflight", "route_round", "background_first", "background_spatial", "runtime"))


def _first_payload_single_shot_spatial_planning(mode_budget: dict[str, Any] | None) -> bool:
    payload = _as_dict(mode_budget)
    return bool(payload.get("first_payload_single_shot") or payload.get("bounded_route_single_shot"))


def _bounded_route_spatial_planning(mode_budget: dict[str, Any] | None) -> bool:
    return bool(_as_dict(mode_budget).get("bounded_route_single_shot"))


def _extended_single_call_spatial_planning(mode_budget: dict[str, Any] | None) -> bool:
    payload = _as_dict(mode_budget)
    source = _text(payload.get("source"), limit=120).lower()
    return bool(
        payload.get("bounded_route_single_shot")
        or source in {"background_first_ai_contract", "background_spatial_isolated", "bounded_complete_paths_route_worker"}
    )


def _background_spatial_recovery_source(mode_budget: dict[str, Any] | None) -> bool:
    payload = _as_dict(mode_budget)
    source = _text(payload.get("source"), limit=120).lower()
    return bool(
        payload.get("bounded_route_single_shot")
        or source in {"background_first_ai_contract", "background_spatial_isolated", "bounded_complete_paths_route_worker"}
    )


def _prefer_direct_sharded_spatial_planning(
    *,
    mode: str,
    mode_budget: dict[str, Any] | None,
    strands: list[dict[str, Any]],
) -> bool:
    payload = _as_dict(mode_budget)
    source = _text(payload.get("source"), limit=120).lower()
    if payload.get("cache_only"):
        return False
    if mode == "flash":
        return bool(_background_spatial_recovery_source(payload) and len(strands) >= 2)
    if mode not in {"balanced", "heavy", "forensic"}:
        return False
    if len(strands) < 2:
        return False
    if payload.get("bounded_route_single_shot"):
        return True
    return bool(
        source in {"background_first_ai_contract", "background_spatial_isolated", "bounded_complete_paths_route_worker"}
    )


def _mode_limits(retrieval_mode: str | None, mode_budget: dict[str, Any] | None) -> dict[str, int | float]:
    mode = _text(retrieval_mode or _as_dict(mode_budget).get("mode") or "balanced").lower()
    requested_branches = _bounded_int(
        _as_dict(mode_budget).get("max_total_branches") or _as_dict(mode_budget).get("max_probe_count"),
        default=6,
        minimum=1,
        maximum=16,
    )
    if _bounded_route_spatial_planning(mode_budget):
        if mode == "flash":
            return {"max_paths": min(requested_branches, 2), "max_waypoints": 1, "timeout": 3.6, "tokens": 300}
        if mode in {"heavy", "forensic"}:
            return {"max_paths": min(requested_branches, 6), "max_waypoints": 2, "timeout": 7.0, "tokens": 560}
        return {"max_paths": min(requested_branches, 4), "max_waypoints": 2, "timeout": 6.2, "tokens": 420}
    if _first_payload_single_shot_spatial_planning(mode_budget):
        if mode == "flash":
            recovery_paths = 2 if _background_spatial_recovery_source(mode_budget) else 1
            return {
                "max_paths": min(requested_branches, recovery_paths),
                "max_waypoints": 1,
                "timeout": 3.6 if recovery_paths > 1 else 3.0,
                "tokens": 300 if recovery_paths > 1 else 180,
            }
        if mode in {"heavy", "forensic"}:
            return {"max_paths": min(requested_branches, 3), "max_waypoints": 1, "timeout": 4.8, "tokens": 280}
        return {"max_paths": min(requested_branches, 2), "max_waypoints": 1, "timeout": 3.8, "tokens": 220}
    if _runtime_fast_spatial_planning(mode_budget):
        if mode == "flash":
            recovery_paths = 2 if _background_spatial_recovery_source(mode_budget) else 1
            return {"max_paths": min(requested_branches, recovery_paths), "max_waypoints": 1, "timeout": 4.0, "tokens": 260 if recovery_paths > 1 else 220}
        if mode == "heavy":
            return {"max_paths": min(requested_branches, 5), "max_waypoints": 1, "timeout": 8.5, "tokens": 520}
        if mode == "forensic":
            return {"max_paths": min(requested_branches, 6), "max_waypoints": 2, "timeout": 7.5, "tokens": 520}
        return {"max_paths": min(requested_branches, 3), "max_waypoints": 1, "timeout": 4.8, "tokens": 340}
    if mode == "flash":
        return {"max_paths": min(requested_branches, 1), "max_waypoints": 1, "timeout": 5.2, "tokens": 300}
    if mode == "heavy":
        return {"max_paths": min(requested_branches, 8), "max_waypoints": 2, "timeout": 8.0, "tokens": 740}
    if mode == "forensic":
        return {"max_paths": min(requested_branches, 10), "max_waypoints": 3, "timeout": 10.0, "tokens": 860}
    return {"max_paths": min(requested_branches, 4), "max_waypoints": 2, "timeout": 5.5, "tokens": 620}


def _compact_answer_strands(answer_strands: list[dict[str, Any]], *, max_items: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in answer_strands[:max_items]:
        payload = _as_dict(item)
        rows.append(
            {
                "answer_field": _text(payload.get("answer_field"), limit=80),
                "goal": _text(payload.get("goal"), limit=80),
                "answer_hypothesis": _text(payload.get("answer_hypothesis"), limit=260),
                "landing_hint": _text(payload.get("landing_hint"), limit=140),
            }
        )
    rows.sort(
        key=lambda row: (
            _fold_text(row.get("answer_field")),
            _fold_text(row.get("goal")),
            _fold_text(row.get("answer_hypothesis")),
            _fold_text(row.get("landing_hint")),
        )
    )
    return rows


def _compact_semantic_contract(
    contract: dict[str, Any] | None,
    runtime: dict[str, Any] | None,
    *,
    item_limit: int = 12,
) -> dict[str, Any]:
    payload = _as_dict(contract)
    landing_plan = _as_dict(payload.get("landing_plan"))
    context_contract = _as_dict(payload.get("context_contract"))
    runtime_payload = _as_dict(runtime)
    limit = max(1, min(12, int(item_limit or 12)))

    def _compact_landing_hypothesis(item: Any) -> dict[str, Any]:
        landing = _as_dict(item)
        targets = _dedupe_text(
            _as_list(landing.get("target_evidence_ids") or landing.get("target_ids") or landing.get("targets")),
            limit=4,
            text_limit=80,
        )
        target_id = (
            landing.get("target_id")
            or landing.get("id")
            or landing.get("landing_id")
            or (targets[0] if targets else "")
        )
        textual_probe = _text(
            landing.get("textual_probe")
            or landing.get("probe")
            or landing.get("claim_shape")
            or landing.get("answer_hypothesis"),
            limit=220,
        )
        return {
            "landing_id": _text(landing.get("landing_id") or landing.get("id") or target_id, limit=80),
            "target_id": _text(target_id, limit=80),
            "targets": targets,
            "goal": _text(landing.get("goal") or landing.get("why_traverse") or textual_probe, limit=160),
            "answer_field": _text(landing.get("answer_field") or landing.get("slot") or target_id, limit=80),
            "textual_probe": textual_probe,
        }

    def _compact_semantic_path(item: Any) -> dict[str, Any]:
        path = _as_dict(item)
        from_ref = (
            path.get("from")
            or path.get("from_target")
            or path.get("from_landing_id")
            or path.get("origin_landing_id")
            or path.get("source_landing_id")
        )
        to_ref = (
            path.get("to")
            or path.get("to_target")
            or path.get("to_landing_id")
            or path.get("target_landing_id")
            or path.get("target_id")
        )
        return {
            "path_id": _text(path.get("path_id") or path.get("id"), limit=80),
            "from": _text(from_ref, limit=80),
            "to": _text(to_ref, limit=80),
            "route_kind": _text(path.get("route_kind") or path.get("kind"), limit=80),
            "intent": _text(path.get("intent") or path.get("rationale") or path.get("why_traverse"), limit=200),
        }

    return {
        "ai_required": bool(payload.get("ai_required") or runtime_payload.get("ai_required")),
        "contract_authority": _text(payload.get("contract_authority"), limit=80),
        # Do not include transport/runtime source in the spatial cache key.  A
        # fresh compact contract and the same compact contract served from the
        # semantic cache must reuse the same spatial landing plan.
        "required_sections": _dedupe_text(_as_list(context_contract.get("required_sections")), limit=min(10, limit)),
        "expected_evidence_targets": [
            {
                "target_id": _text(_as_dict(item).get("target_id"), limit=80),
                "slot": _text(_as_dict(item).get("slot"), limit=80),
                "required": bool(_as_dict(item).get("required", True)),
            }
            for item in _as_list(payload.get("expected_evidence") or payload.get("expected_evidence_targets"))[:limit]
            if isinstance(item, dict)
        ],
        "landing_hypotheses": [
            _compact_landing_hypothesis(item)
            for item in _as_list(landing_plan.get("landing_hypotheses") or landing_plan.get("landings"))[:limit]
            if isinstance(item, dict)
        ],
        "semantic_paths": [
            _compact_semantic_path(item)
            for item in _as_list(landing_plan.get("paths") or payload.get("paths"))[:limit]
            if isinstance(item, dict)
        ],
    }


def _compact_nuclei(value: Any, *, limit: int = 12) -> dict[str, Any]:
    nuclei = _as_dict(value)
    compact: dict[str, Any] = {}
    for key, nucleus in list(nuclei.items())[: max(1, int(limit or 12))]:
        if not isinstance(nucleus, dict):
            continue
        compact[_text(key, limit=80)] = {
            "id": _text(nucleus.get("id") or nucleus.get("node_id"), limit=100),
            "label": _text(nucleus.get("label") or nucleus.get("name") or nucleus.get("title"), limit=120),
            "centroid": _coordinate(nucleus.get("centroid") or nucleus.get("position")),
            "guide_area": _text(nucleus.get("guide_area") or nucleus.get("dominant_guide_area"), limit=80),
            "node_count": _bounded_int(nucleus.get("node_count"), default=0, minimum=0, maximum=1_000_000),
        }
    return compact


def _compact_spatial_brief(brief: dict[str, Any] | None) -> dict[str, Any]:
    payload = _as_dict(brief)
    atlas_summary = _as_dict(payload.get("atlas_summary"))
    sample_buckets = (
        atlas_summary.get("sample_buckets")
        or atlas_summary.get("buckets")
        or payload.get("atlas_buckets")
        or []
    )
    return {
        "schema_version": payload.get("schema_version"),
        "revision": payload.get("revision"),
        "source_snapshot_version": payload.get("source_snapshot_version"),
        "metamemory_revision": payload.get("metamemory_revision") or payload.get("source_snapshot_version"),
        "source_hash": payload.get("source_hash"),
        "brain_id": payload.get("brain_id"),
        "brain_revision": payload.get("brain_revision"),
        "matrix_revision": payload.get("matrix_revision"),
        "topology_revision": payload.get("topology_revision"),
        "atlas_revision": payload.get("atlas_revision"),
        "calibration_revision": payload.get("calibration_revision"),
        "source_replay_revision": payload.get("source_replay_revision"),
        "coordinate_system": _as_dict(payload.get("coordinate_system")),
        "guide_areas": _dedupe_text(_as_list(payload.get("guide_areas")), limit=24),
        "semantic_zones": _as_dict(payload.get("semantic_zones")),
        "radial_bands": _dedupe_text(_as_list(payload.get("radial_bands")), limit=12),
        "base_matrix_summary": {
            "schema_version": _as_dict(payload.get("base_matrix_summary")).get("schema_version"),
            "matrix_revision": _as_dict(payload.get("base_matrix_summary")).get("matrix_revision"),
            "semantic_zones": _as_dict(_as_dict(payload.get("base_matrix_summary")).get("semantic_zones")),
            "bucket_centroids": _as_list(_as_dict(payload.get("base_matrix_summary")).get("bucket_centroids"))[:8],
            "policy": _text(_as_dict(payload.get("base_matrix_summary")).get("policy"), limit=180),
        },
        "topology_overlay_summary": {
            "schema_version": _as_dict(payload.get("topology_overlay_summary")).get("schema_version"),
            "topology_revision": _as_dict(payload.get("topology_overlay_summary")).get("topology_revision"),
            "matrix_revision": _as_dict(payload.get("topology_overlay_summary")).get("matrix_revision"),
            "atlas_revision": _as_dict(payload.get("topology_overlay_summary")).get("atlas_revision"),
            "overlay_present": bool(_as_dict(payload.get("topology_overlay_summary")).get("overlay_present")),
            "density_lobes": _as_list(_as_dict(payload.get("topology_overlay_summary")).get("density_lobes"))[:8],
            "active_highways": _as_list(_as_dict(payload.get("topology_overlay_summary")).get("active_highways"))[:8],
            "bridge_corridors": _as_list(_as_dict(payload.get("topology_overlay_summary")).get("bridge_corridors"))[:6],
            "attraction_priors": _as_list(_as_dict(payload.get("topology_overlay_summary")).get("attraction_priors"))[:6],
            "repulsion_priors": _as_list(_as_dict(payload.get("topology_overlay_summary")).get("repulsion_priors"))[:6],
            "pending_maintenance_proposals": _as_list(
                _as_dict(payload.get("topology_overlay_summary")).get("pending_maintenance_proposals")
            )[:6],
            "missing_reasons": _as_list(_as_dict(payload.get("topology_overlay_summary")).get("missing_reasons"))[:6],
        },
        "spatial_readiness_contract": {
            "schema_version": _as_dict(payload.get("spatial_readiness_contract")).get("schema_version"),
            "status": _as_dict(payload.get("spatial_readiness_contract")).get("status"),
            "certifiable": bool(_as_dict(payload.get("spatial_readiness_contract")).get("certifiable")),
            "missing_reasons": _as_list(_as_dict(payload.get("spatial_readiness_contract")).get("missing_reasons"))[:8],
            "stale_reasons": _as_list(_as_dict(payload.get("spatial_readiness_contract")).get("stale_reasons"))[:8],
            "revision_chain": _as_dict(_as_dict(payload.get("spatial_readiness_contract")).get("revision_chain")),
        },
        "atlas_summary": {
            "bucket_count": _bounded_int(atlas_summary.get("bucket_count"), default=0, minimum=0, maximum=1_000_000),
            "node_count": _bounded_int(atlas_summary.get("node_count"), default=0, minimum=0, maximum=10_000_000),
            "atlas_revision": _text(atlas_summary.get("atlas_revision"), limit=120),
            "sample_buckets": [
                {
                    "bucket_key": _text(_as_dict(bucket).get("bucket_key"), limit=80),
                    "centroid": _coordinate(_as_dict(bucket).get("centroid")),
                    "node_count": _bounded_int(_as_dict(bucket).get("node_count"), default=0, minimum=0, maximum=1_000_000),
                    "dominant_guide_area": _text(
                        _as_dict(bucket).get("dominant_guide_area")
                        or _as_dict(bucket).get("dominant_area")
                        or _as_dict(bucket).get("guide_area"),
                        limit=80,
                    ),
                    "highway_gateway": bool(_as_dict(bucket).get("highway_gateway")),
                }
                for bucket in _as_list(sample_buckets)[:18]
                if isinstance(bucket, dict)
            ],
        },
        "nuclei": _compact_nuclei(payload.get("nuclei"), limit=12),
        "highway_gateways": [
            {
                "bucket_key": _text(_as_dict(item).get("bucket_key"), limit=80),
                "centroid": _coordinate(_as_dict(item).get("centroid")),
                "guide_area": _text(_as_dict(item).get("guide_area") or _as_dict(item).get("dominant_guide_area"), limit=80),
            }
            for item in _as_list(payload.get("highway_gateways"))[:18]
            if isinstance(item, dict)
        ],
        "calibration_summary": _as_dict(payload.get("calibration_summary")),
        "mode_budget": _as_dict(payload.get("mode_budget")),
        "prompt_brief": _text(payload.get("prompt_brief"), limit=1400),
    }


def _compact_identity_hints(identity_hints: dict[str, Any] | None) -> dict[str, Any]:
    payload = _as_dict(identity_hints)
    compact: dict[str, Any] = {
        "core_name": _text(payload.get("core_name") or payload.get("name"), limit=120),
        "primary_self_node_id": _text(payload.get("primary_self_node_id"), limit=100),
        "aliases": _dedupe_text(_as_list(payload.get("aliases")), limit=8, text_limit=120),
        "self_name_candidates": _dedupe_text(_as_list(payload.get("self_name_candidates")), limit=6, text_limit=120),
        "role_candidates": _dedupe_text(_as_list(payload.get("role_candidates")), limit=8, text_limit=120),
        "employer_candidates": _dedupe_text(_as_list(payload.get("employer_candidates")), limit=8, text_limit=120),
        "project_candidates": _dedupe_text(_as_list(payload.get("project_candidates")), limit=8, text_limit=120),
        "value_candidates": _dedupe_text(_as_list(payload.get("value_candidates")), limit=6, text_limit=120),
    }
    core_nodes: list[dict[str, Any]] = []
    for node in _as_list(payload.get("core_nodes"))[:8]:
        node_payload = _as_dict(node)
        if not node_payload:
            continue
        core_nodes.append(
            {
                "summary": _text(node_payload.get("summary") or node_payload.get("raw_text"), limit=220),
                "memory_type": _text(node_payload.get("memory_type"), limit=80),
                "guide_area": _text(node_payload.get("guide_area"), limit=80),
                "confidence": _bounded_float(node_payload.get("confidence"), default=0.0, minimum=0.0, maximum=1.0),
            }
        )
    if core_nodes:
        compact["core_nodes"] = core_nodes
    support_counts: dict[str, int] = {}
    for key, value in payload.items():
        if key.endswith("_support_node_ids") and isinstance(value, list):
            support_counts[key] = len(value)
    if support_counts:
        compact["support_counts"] = support_counts
    return {key: value for key, value in compact.items() if value not in ("", [], {}, None)}


def _stable_mode_budget_for_cache(mode_budget: dict[str, Any] | None) -> dict[str, Any]:
    raw_mode_budget = _as_dict(mode_budget)
    volatile_keys = {
        "cache_only",
        "first_payload_wait_seconds",
        "wait_seconds",
        "timeout_seconds",
        "transport_timeout_seconds",
        "first_ai_wait_seconds",
        "pre_route_wait_seconds",
        "source",
        # These fields describe the worker/transport shape, not the semantic
        # spatial planning problem. A bounded route worker and a cache-only
        # first payload must be able to reuse the same AI-authored regions.
        "first_payload_single_shot",
        "bounded_route_single_shot",
        "max_probe_count",
        "max_total_branches",
    }
    return {
        key: value
        for key, value in raw_mode_budget.items()
        if key not in volatile_keys and value not in (None, "", [], {})
    }


def _spatial_contract_cache_signature(
    *,
    query_text: str,
    retrieval_mode: str,
    semantic_contract: dict[str, Any] | None,
    semantic_contract_runtime: dict[str, Any] | None,
    answer_strands: list[dict[str, Any]] | None,
    metamemory_spatial_brief: dict[str, Any],
    mode_budget: dict[str, Any] | None,
    brain_revision: str | None,
    cache_scope: str | None,
) -> dict[str, Any]:
    spatial_brief = _as_dict(metamemory_spatial_brief)
    stable_mode_budget = _stable_mode_budget_for_cache(mode_budget)
    # Cache identity must represent the semantic spatial problem, not the
    # transport profile that happened to ask for it. First-payload, background
    # and route workers can request different max path budgets for the same
    # inverse answer set; including that budget in the strand hash prevents a
    # fresh AI-authored plan from being reused by the next warm MCP call.
    stable_strand_limit = max(1, min(8, len(_as_list(answer_strands)) or 8))
    return {
        "schema_version": "agvm.ai_spatial_landing_contract_cache_signature.v1",
        "contract_schema_version": AI_SPATIAL_LANDING_CONTRACT_SCHEMA_VERSION,
        "normalized_intent": _fold_text(query_text),
        "retrieval_mode": str(retrieval_mode or "balanced").strip().lower(),
        "brain_revision": str(brain_revision or spatial_brief.get("brain_revision") or "").strip(),
        # The spatial brief revision may include runtime calibration telemetry.
        # The source hash is the stable constitution for this brain geometry.
        "metamemory_spatial_hash": str(spatial_brief.get("source_hash") or "").strip(),
        "matrix_revision": str(spatial_brief.get("matrix_revision") or "").strip(),
        "topology_revision": str(spatial_brief.get("topology_revision") or "").strip(),
        "atlas_revision": str(spatial_brief.get("atlas_revision") or "").strip(),
        "cache_scope": str(cache_scope or "").strip(),
        "retrieval_model": retrieval_model(),
        "semantic_contract_hash": _stable_hash(
            _compact_semantic_contract(semantic_contract, semantic_contract_runtime)
        )[:32],
        "answer_strands_hash": _stable_hash(
            _compact_answer_strands(
                [_as_dict(item) for item in _as_list(answer_strands) if isinstance(item, dict)],
                max_items=stable_strand_limit,
            )
        )[:32],
        # Identity hints are deliberately not part of the cache identity. They
        # are prompt context for the AI, but they may drift between first
        # package, background and warm calls because support counts, nucleus
        # snippets and runtime rows are refreshed. The active brain revision,
        # metamemory hash, query, semantic contract and inverse strands are the
        # stable planning problem.
        "mode_budget_hash": _stable_hash(stable_mode_budget)[:24],
    }


def _spatial_contract_cache_key(
    *,
    query_text: str,
    retrieval_mode: str,
    semantic_contract: dict[str, Any] | None,
    semantic_contract_runtime: dict[str, Any] | None,
    answer_strands: list[dict[str, Any]] | None,
    metamemory_spatial_brief: dict[str, Any],
    mode_budget: dict[str, Any] | None,
    brain_revision: str | None,
    cache_scope: str | None,
) -> str:
    key_payload = {
        **_spatial_contract_cache_signature(
            query_text=query_text,
            retrieval_mode=retrieval_mode,
            semantic_contract=semantic_contract,
            semantic_contract_runtime=semantic_contract_runtime,
            answer_strands=answer_strands,
            metamemory_spatial_brief=metamemory_spatial_brief,
            mode_budget=mode_budget,
            brain_revision=brain_revision,
            cache_scope=cache_scope,
        ),
        "schema_version": "agvm.ai_spatial_landing_contract_cache_key.v2",
    }
    return _stable_hash(key_payload)


def _cache_entry_is_success(entry: dict[str, Any], now: float) -> bool:
    contract = _as_dict(entry.get("contract"))
    if now - float(entry.get("stored_at") or now) > _AI_SPATIAL_CONTRACT_CACHE_TTL_SECONDS:
        return False
    return bool(contract.get("materialized") and contract.get("certifiable"))


def _spatial_contract_cache_path() -> Any:
    return current_data_dir() / _AI_SPATIAL_CONTRACT_DISK_CACHE_FILENAME


def _load_spatial_contract_disk_cache(now: float) -> dict[str, Any]:
    path = _spatial_contract_cache_path()
    try:
        if not path.exists():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    entries = dict(raw.get("entries") or {}) if isinstance(raw, dict) else {}
    return {
        str(key): dict(entry)
        for key, entry in entries.items()
        if isinstance(entry, dict) and _cache_entry_is_success(dict(entry), now)
    }


def _write_spatial_contract_disk_cache(entries: dict[str, Any]) -> None:
    path = _spatial_contract_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        ordered = sorted(
            entries.items(),
            key=lambda item: float(dict(item[1]).get("last_access_at") or dict(item[1]).get("stored_at") or 0.0),
        )
        if len(ordered) > _AI_SPATIAL_CONTRACT_DISK_CACHE_MAX_ITEMS:
            ordered = ordered[-_AI_SPATIAL_CONTRACT_DISK_CACHE_MAX_ITEMS:]
        payload = {
            "schema_version": _AI_SPATIAL_CONTRACT_DISK_CACHE_SCHEMA,
            "stored_at": time.time(),
            "ttl_seconds": _AI_SPATIAL_CONTRACT_CACHE_TTL_SECONDS,
            "max_items": _AI_SPATIAL_CONTRACT_DISK_CACHE_MAX_ITEMS,
            "entries": {str(key): value for key, value in ordered},
        }
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp_path.replace(path)
    except Exception:
        return


def _prune_spatial_contract_cache(now: float) -> None:
    expired = [
        key
        for key, entry in _AI_SPATIAL_CONTRACT_CACHE.items()
        if not _cache_entry_is_success(dict(entry), now)
    ]
    for key in expired:
        _AI_SPATIAL_CONTRACT_CACHE.pop(key, None)
    if len(_AI_SPATIAL_CONTRACT_CACHE) <= _AI_SPATIAL_CONTRACT_CACHE_MAX_ITEMS:
        return
    ordered = sorted(
        _AI_SPATIAL_CONTRACT_CACHE.items(),
        key=lambda item: float(item[1].get("last_access_at") or item[1].get("stored_at") or 0.0),
    )
    for key, _entry in ordered[: max(0, len(_AI_SPATIAL_CONTRACT_CACHE) - _AI_SPATIAL_CONTRACT_CACHE_MAX_ITEMS)]:
        _AI_SPATIAL_CONTRACT_CACHE.pop(key, None)


def clear_ai_spatial_contract_cache(*, clear_disk: bool = True) -> None:
    with _AI_SPATIAL_CONTRACT_CACHE_LOCK:
        _AI_SPATIAL_CONTRACT_CACHE.clear()
        if clear_disk:
            try:
                _spatial_contract_cache_path().unlink(missing_ok=True)
            except Exception:
                return


def _legacy_cache_contract_matches_signature(contract: dict[str, Any], signature: dict[str, Any]) -> bool:
    if str(contract.get("schema_version") or "") != AI_SPATIAL_LANDING_CONTRACT_SCHEMA_VERSION:
        return False
    expected_query = str(signature.get("normalized_intent") or "").strip()
    if expected_query and _fold_text(contract.get("query_text")) != expected_query:
        return False
    expected_mode = str(signature.get("retrieval_mode") or "").strip().lower()
    if expected_mode and str(contract.get("retrieval_mode") or "").strip().lower() != expected_mode:
        return False
    expected_revision = str(signature.get("brain_revision") or "").strip()
    if expected_revision and str(contract.get("brain_revision") or "").strip() != expected_revision:
        return False
    expected_hash = str(signature.get("metamemory_spatial_hash") or "").strip()
    if expected_hash and str(contract.get("metamemory_spatial_hash") or "").strip() != expected_hash:
        return False
    for field_name in ("matrix_revision", "topology_revision", "atlas_revision"):
        expected_field = str(signature.get(field_name) or "").strip()
        if expected_field and str(contract.get(field_name) or "").strip() != expected_field:
            return False
    return bool(contract.get("materialized") and contract.get("certifiable"))


def _spatial_cache_signature_equivalent(stored_signature: dict[str, Any], requested_signature: dict[str, Any]) -> bool:
    if not stored_signature or not requested_signature:
        return False
    stable_fields = (
        "contract_schema_version",
        "normalized_intent",
        "retrieval_mode",
        "brain_revision",
        "metamemory_spatial_hash",
        "matrix_revision",
        "topology_revision",
        "atlas_revision",
        "cache_scope",
        "retrieval_model",
        "semantic_contract_hash",
        "answer_strands_hash",
        "mode_budget_hash",
    )
    for field_name in stable_fields:
        stored_value = str(stored_signature.get(field_name) or "").strip()
        requested_value = str(requested_signature.get(field_name) or "").strip()
        if stored_value or requested_value:
            if stored_value != requested_value:
                return False
    return True


def _spatial_contract_has_physical_topology_dependencies(contract: dict[str, Any]) -> bool:
    for path in _as_list(contract.get("inverse_answer_paths")):
        path_payload = _as_dict(path)
        if _coordinate(path_payload.get("landing_coordinate")) is not None:
            return True
        for waypoint in _as_list(path_payload.get("waypoints")):
            waypoint_payload = _as_dict(waypoint)
            if _coordinate(waypoint_payload.get("coordinate") or waypoint_payload.get("position")) is not None:
                return True
            if _text(waypoint_payload.get("bucket_key") or waypoint_payload.get("node_id"), limit=160):
                return True
    return False


def _spatial_cache_signature_region_resnap_compatible(
    stored_signature: dict[str, Any],
    requested_signature: dict[str, Any],
    *,
    contract: dict[str, Any],
) -> bool:
    if not stored_signature or not requested_signature:
        return False
    if not bool(contract.get("materialized") and contract.get("certifiable")):
        return False
    if _spatial_contract_has_physical_topology_dependencies(contract):
        return False
    strict_fields = (
        "contract_schema_version",
        "normalized_intent",
        "retrieval_mode",
        "brain_revision",
        "metamemory_spatial_hash",
        "matrix_revision",
        "atlas_revision",
        "cache_scope",
        "retrieval_model",
        "semantic_contract_hash",
        "answer_strands_hash",
    )
    for field_name in strict_fields:
        stored_value = str(stored_signature.get(field_name) or "").strip()
        requested_value = str(requested_signature.get(field_name) or "").strip()
        if stored_value or requested_value:
            if stored_value != requested_value:
                return False
    return True


def _cache_entry_signature_match_kind(entry: dict[str, Any], signature: dict[str, Any]) -> str | None:
    if not signature:
        return None
    stored_signature = _as_dict(entry.get("signature"))
    if stored_signature:
        if _stable_hash(stored_signature) == _stable_hash(signature) or _spatial_cache_signature_equivalent(
            stored_signature,
            signature,
        ):
            return "strict_signature"
        if _spatial_cache_signature_region_resnap_compatible(
            stored_signature,
            signature,
            contract=_as_dict(entry.get("contract")),
        ):
            return "region_resnap_signature"
        return None
    return "legacy_signature" if _legacy_cache_contract_matches_signature(_as_dict(entry.get("contract")), signature) else None


def _cache_entry_matches_signature(entry: dict[str, Any], signature: dict[str, Any]) -> bool:
    return _cache_entry_signature_match_kind(entry, signature) is not None


def _mark_spatial_cache_hit(entry: dict[str, Any], *, tier: str, now: float, signature: dict[str, Any] | None) -> dict[str, Any]:
    updated = deepcopy(entry)
    updated["last_access_at"] = now
    updated["hit_count"] = int(updated.get("hit_count") or 0) + 1
    updated["cache_tier"] = tier
    if signature and not _as_dict(updated.get("signature")):
        updated["signature"] = deepcopy(signature)
        updated["signature_hash"] = _stable_hash(signature)
    if signature:
        match_kind = _cache_entry_signature_match_kind(updated, signature)
        if match_kind:
            updated["signature_match_kind"] = match_kind
    return updated


def _get_spatial_contract_cache_entry(cache_key: str, *, signature: dict[str, Any] | None = None) -> dict[str, Any] | None:
    now = time.time()
    with _AI_SPATIAL_CONTRACT_CACHE_LOCK:
        _prune_spatial_contract_cache(now)
        entry = _AI_SPATIAL_CONTRACT_CACHE.get(cache_key)
        if not entry:
            disk_entries = _load_spatial_contract_disk_cache(now)
            disk_entry = disk_entries.get(cache_key)
            if disk_entry:
                disk_entry = _mark_spatial_cache_hit(disk_entry, tier="disk", now=now, signature=signature)
                _AI_SPATIAL_CONTRACT_CACHE[cache_key] = deepcopy(disk_entry)
                _write_spatial_contract_disk_cache(disk_entries | {cache_key: disk_entry})
                return deepcopy(disk_entry)
            if signature:
                for existing_key, candidate in list(_AI_SPATIAL_CONTRACT_CACHE.items()):
                    match_kind = _cache_entry_signature_match_kind(candidate, signature)
                    if match_kind:
                        migrated = _mark_spatial_cache_hit(candidate, tier="memory_signature", now=now, signature=signature)
                        migrated["migrated_from_cache_key"] = str(existing_key)
                        migrated["signature_match_kind"] = match_kind
                        _AI_SPATIAL_CONTRACT_CACHE[cache_key] = deepcopy(migrated)
                        return deepcopy(migrated)
                for existing_key, candidate in list(disk_entries.items()):
                    match_kind = _cache_entry_signature_match_kind(candidate, signature)
                    if match_kind:
                        migrated = _mark_spatial_cache_hit(candidate, tier="disk_signature", now=now, signature=signature)
                        migrated["migrated_from_cache_key"] = str(existing_key)
                        migrated["signature_match_kind"] = match_kind
                        _AI_SPATIAL_CONTRACT_CACHE[cache_key] = deepcopy(migrated)
                        disk_entries[cache_key] = deepcopy(migrated)
                        _write_spatial_contract_disk_cache(disk_entries)
                        return deepcopy(migrated)
            return None
        entry = _mark_spatial_cache_hit(entry, tier=str(entry.get("cache_tier") or "memory"), now=now, signature=signature)
        _AI_SPATIAL_CONTRACT_CACHE[cache_key] = deepcopy(entry)
        return deepcopy(entry)


def _store_spatial_contract_cache_entry(
    cache_key: str,
    *,
    contract: dict[str, Any],
    signature: dict[str, Any] | None = None,
) -> None:
    if not cache_key or not bool(contract.get("materialized") and contract.get("certifiable")):
        return
    if str(contract.get("source") or "") != "fresh_llm":
        return
    now = time.time()
    with _AI_SPATIAL_CONTRACT_CACHE_LOCK:
        entry = {
            "contract": deepcopy(contract),
            "stored_at": now,
            "last_access_at": now,
            "hit_count": 0,
            "cache_tier": "memory",
        }
        if signature:
            entry["signature"] = deepcopy(signature)
            entry["signature_hash"] = _stable_hash(signature)
        _AI_SPATIAL_CONTRACT_CACHE[cache_key] = entry
        _prune_spatial_contract_cache(now)
        disk_entries = _load_spatial_contract_disk_cache(now)
        disk_entry = deepcopy(entry)
        disk_entry["cache_tier"] = "disk"
        disk_entries[cache_key] = disk_entry
        _write_spatial_contract_disk_cache(disk_entries)


def _contract_from_cache_entry(entry: dict[str, Any], *, cache_key: str, started_at: float) -> dict[str, Any]:
    contract = deepcopy(_as_dict(entry.get("contract")))
    contract["source"] = "ai_spatial_contract_cache"
    contract["materialization_state"] = "materialized"
    contract["status"] = "materialized"
    contract["materialized"] = True
    contract["certifiable"] = True
    contract["planner_latency_ms"] = round((time.perf_counter() - started_at) * 1000.0, 2)
    contract["cached_source"] = "fresh_llm"
    contract["cache_age_ms"] = round((time.time() - float(entry.get("stored_at") or time.time())) * 1000.0, 2)
    contract["cache"] = {
        **_as_dict(contract.get("cache")),
        "status": "hit",
        "hit": True,
        "validity": "valid",
        "tier": str(entry.get("cache_tier") or "memory"),
        "persistent": True,
        "ttl_seconds": _AI_SPATIAL_CONTRACT_CACHE_TTL_SECONDS,
        "key_fingerprint": cache_key[:24],
        "cache_age_ms": contract["cache_age_ms"],
        "cache_hit_count": int(entry.get("hit_count") or 0),
        "signature_match_kind": str(entry.get("signature_match_kind") or ""),
    }
    return contract


def _spatial_contract_schema(*, max_paths: int, max_waypoints: int) -> dict[str, Any]:
    coordinate_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "x": {"type": "number", "minimum": -1.0, "maximum": 1.0},
            "y": {"type": "number", "minimum": -1.0, "maximum": 1.0},
            "z": {"type": "number", "minimum": -1.0, "maximum": 1.0},
        },
    }
    waypoint_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "waypoint_id": {"type": "string"},
            "phrase": {"type": "string"},
            "region_ref": {"type": ["string", "null"]},
            "coordinate": {"anyOf": [coordinate_schema, {"type": "null"}]},
            "radius": {"type": "number", "minimum": 0.05, "maximum": 0.5},
            "rationale": {"type": "string"},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "planner_summary": {"type": "string"},
            "inverse_answer_paths": {
                "type": "array",
                "maxItems": max_paths,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "path_id": {"type": "string"},
                        "strand_id": {"type": ["string", "null"]},
                        "answer_field": {"type": "string"},
                        "answer_hypothesis": {"type": "string"},
                        "goal": {"type": "string"},
                        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        "landing_region_ref": {"type": ["string", "null"]},
                        "landing_coordinate": {"anyOf": [coordinate_schema, {"type": "null"}]},
                        "novel_region_candidate": {"type": ["string", "null"]},
                        "radius": {"type": "number", "minimum": 0.08, "maximum": 0.5},
                        "waypoints": {"type": "array", "maxItems": max_waypoints, "items": waypoint_schema},
                        "bridge_targets": {"type": "array", "maxItems": 6, "items": {"type": "string"}},
                        "preferred_edges": {"type": "array", "maxItems": 8, "items": {"type": "string"}},
                        "forbidden_regions": {"type": "array", "maxItems": 6, "items": {"type": "string"}},
                        "stop_condition": {"type": "string"},
                        "spatial_rationale": {"type": "string"},
                    },
                },
            },
            "uncertainty": {"type": "string"},
        },
    }


def _spatial_fast_contract_schema(*, max_paths: int) -> dict[str, Any]:
    coordinate_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "x": {"type": "number", "minimum": -1.0, "maximum": 1.0},
            "y": {"type": "number", "minimum": -1.0, "maximum": 1.0},
            "z": {"type": "number", "minimum": -1.0, "maximum": 1.0},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "planner_summary": {"type": "string"},
            "inverse_answer_paths": {
                "type": "array",
                "maxItems": max_paths,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "path_id": {"type": "string"},
                        "answer_field": {"type": "string"},
                        "answer_hypothesis": {"type": "string"},
                        "goal": {"type": "string"},
                        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        "landing_region_ref": {"type": ["string", "null"]},
                        "landing_coordinate": {"anyOf": [coordinate_schema, {"type": "null"}]},
                        "novel_region_candidate": {"type": ["string", "null"]},
                        "radius": {"type": "number", "minimum": 0.08, "maximum": 0.5},
                        "preferred_edges": {"type": "array", "maxItems": 4, "items": {"type": "string"}},
                        "stop_condition": {"type": "string"},
                    },
                },
            },
            "uncertainty": {"type": "string"},
        },
    }


def _spatial_retryable_error(error: str | None) -> bool:
    normalized = _fold_text(error or "")
    if not normalized:
        return False
    return any(
        token in normalized
        for token in (
            "timeout",
            "timed out",
            "transport error",
            "connection",
            "rate limit",
            "overloaded",
            "temporarily unavailable",
            "missing output text",
            "invalid json",
            "llm empty",
        )
    )


def _spatial_timeout_error(error: str | None) -> bool:
    normalized = _fold_text(error or "")
    return "timeout" in normalized or "timed out" in normalized


def _spatial_retry_timeout_seconds(retrieval_mode: str, requested_timeout: float) -> float:
    mode = _text(retrieval_mode or "balanced").lower()
    if mode == "flash":
        ceiling = 2.8
    elif mode in {"heavy", "forensic"}:
        ceiling = 6.0
    else:
        ceiling = 5.0
    return max(2.0, min(ceiling, max(2.0, float(requested_timeout or 0.0) * 0.5)))


def _first_payload_primary_timeout_seconds(retrieval_mode: str, requested_timeout: float) -> float:
    mode = _text(retrieval_mode or "balanced").lower()
    requested = max(0.1, float(requested_timeout or 0.0))
    if requested < 2.2 or mode == "flash":
        return requested
    reserve = 0.2
    available = requested - reserve if requested > reserve + 1.2 else requested
    if mode in {"heavy", "forensic"}:
        target = max(3.6, requested * 0.92)
        ceiling = 5.8
    else:
        target = max(3.2, requested * 0.94)
        ceiling = 4.4
    return max(1.2, min(available, target, ceiling))


def _extended_single_call_primary_timeout_seconds(retrieval_mode: str, requested_timeout: float) -> float:
    mode = _text(retrieval_mode or "balanced").lower()
    requested = max(0.1, float(requested_timeout or 0.0))
    if requested < 2.2 or mode == "flash":
        return requested
    reserve = 0.25
    available = requested - reserve if requested > reserve + 1.2 else requested
    if mode in {"heavy", "forensic"}:
        target = max(6.2, requested * 0.97)
        ceiling = 8.8
    else:
        target = max(5.8, requested * 0.97)
        ceiling = 7.8
    return max(1.2, min(available, target, ceiling))


def _first_payload_retry_timeout_seconds(
    retrieval_mode: str,
    requested_timeout: float,
    primary_elapsed_seconds: float,
) -> float | None:
    mode = _text(retrieval_mode or "balanced").lower()
    if mode == "flash":
        return None
    remaining = float(requested_timeout or 0.0) - max(0.0, float(primary_elapsed_seconds or 0.0)) - 0.15
    floor = 1.4 if mode in {"heavy", "forensic"} else 1.2
    if remaining < floor:
        return None
    ceiling = 2.4 if mode in {"heavy", "forensic"} else 1.8
    return max(floor, min(ceiling, remaining))


def _compact_retry_spatial_brief(brief: dict[str, Any]) -> dict[str, Any]:
    compact = _compact_spatial_brief(brief)
    atlas = _as_dict(compact.get("atlas_summary"))
    compact["atlas_summary"] = {
        **atlas,
        "sample_buckets": _as_list(atlas.get("sample_buckets"))[:8],
    }
    compact["guide_areas"] = _as_list(compact.get("guide_areas"))[:16]
    compact["radial_bands"] = _as_list(compact.get("radial_bands"))[:8]
    compact["highway_gateways"] = _as_list(compact.get("highway_gateways"))[:8]
    compact["prompt_brief"] = _text(compact.get("prompt_brief"), limit=620)
    return compact


def _compact_runtime_spatial_brief(brief: dict[str, Any], *, retrieval_mode: str) -> dict[str, Any]:
    compact = _compact_retry_spatial_brief(brief)
    mode = _text(retrieval_mode or "balanced").lower()
    bucket_limit = 4 if mode == "flash" else (6 if mode == "balanced" else 8)
    guide_limit = 8 if mode == "flash" else (12 if mode == "balanced" else 14)
    gateway_limit = 4 if mode == "flash" else (6 if mode == "balanced" else 8)
    atlas = _as_dict(compact.get("atlas_summary"))
    compact["atlas_summary"] = {
        **atlas,
        "sample_buckets": _as_list(atlas.get("sample_buckets"))[:bucket_limit],
    }
    compact["guide_areas"] = _as_list(compact.get("guide_areas"))[:guide_limit]
    compact["radial_bands"] = _as_list(compact.get("radial_bands"))[:5]
    compact["highway_gateways"] = _as_list(compact.get("highway_gateways"))[:gateway_limit]
    compact["nuclei"] = dict(list(_as_dict(compact.get("nuclei")).items())[:8])
    compact["prompt_brief"] = _text(compact.get("prompt_brief"), limit=360)
    return compact


def _compact_first_payload_spatial_brief(brief: dict[str, Any], *, retrieval_mode: str) -> dict[str, Any]:
    compact = _compact_runtime_spatial_brief(brief, retrieval_mode=retrieval_mode)
    mode = _text(retrieval_mode or "balanced").lower()
    bucket_limit = 3 if mode == "flash" else 4
    guide_limit = 6 if mode == "flash" else 8
    gateway_limit = 3 if mode == "flash" else 4
    atlas = _as_dict(compact.get("atlas_summary"))
    compact["atlas_summary"] = {
        **atlas,
        "sample_buckets": _as_list(atlas.get("sample_buckets"))[:bucket_limit],
    }
    compact["guide_areas"] = _as_list(compact.get("guide_areas"))[:guide_limit]
    compact["radial_bands"] = _as_list(compact.get("radial_bands"))[:4]
    compact["highway_gateways"] = _as_list(compact.get("highway_gateways"))[:gateway_limit]
    compact["nuclei"] = dict(list(_as_dict(compact.get("nuclei")).items())[:5])
    compact["prompt_brief"] = _text(compact.get("prompt_brief"), limit=220)
    return compact


def _compact_route_shard_spatial_brief(brief: dict[str, Any], *, retrieval_mode: str) -> dict[str, Any]:
    compact = _compact_first_payload_spatial_brief(brief, retrieval_mode=retrieval_mode)
    topology = _as_dict(compact.get("topology_overlay_summary"))
    atlas = _as_dict(compact.get("atlas_summary"))
    semantic_zones = _as_dict(compact.get("semantic_zones"))

    def _lobe_ref(item: Any) -> dict[str, Any]:
        payload = _as_dict(item)
        return {
            "region_ref": _text(payload.get("region_ref") or payload.get("lobe_id") or payload.get("bucket_key"), limit=80),
            "guide_area": _text(payload.get("guide_area") or payload.get("dominant_guide_area"), limit=80),
            "node_count": _bounded_int(payload.get("node_count"), default=0, minimum=0, maximum=1_000_000),
        }

    def _bucket_ref(item: Any) -> dict[str, Any]:
        payload = _as_dict(item)
        return {
            "bucket_key": _text(payload.get("bucket_key"), limit=80),
            "guide_area": _text(payload.get("dominant_guide_area") or payload.get("guide_area"), limit=80),
            "node_count": _bounded_int(payload.get("node_count"), default=0, minimum=0, maximum=1_000_000),
        }

    return {
        "schema_version": compact.get("schema_version"),
        "brain_id": compact.get("brain_id"),
        "brain_revision": compact.get("brain_revision"),
        "source_hash": compact.get("source_hash"),
        "matrix_revision": compact.get("matrix_revision"),
        "topology_revision": compact.get("topology_revision"),
        "atlas_revision": compact.get("atlas_revision"),
        "calibration_revision": compact.get("calibration_revision"),
        "coordinate_system": _as_dict(compact.get("coordinate_system")),
        "guide_areas": _as_list(compact.get("guide_areas"))[:8],
        "semantic_zone_refs": list(semantic_zones.keys())[:12],
        "radial_bands": _as_list(compact.get("radial_bands"))[:4],
        "nuclei": dict(list(_as_dict(compact.get("nuclei")).items())[:5]),
        "topology_overlay_summary": {
            "overlay_present": bool(topology.get("overlay_present")),
            "density_lobes": [
                _lobe_ref(item)
                for item in _as_list(topology.get("density_lobes"))[:4]
                if isinstance(item, dict)
            ],
            "active_highway_refs": [
                _text(_as_dict(item).get("region_ref") or _as_dict(item).get("highway_id") or _as_dict(item).get("bucket_key"), limit=80)
                for item in _as_list(topology.get("active_highways"))[:4]
                if isinstance(item, dict)
            ],
            "missing_reasons": _as_list(topology.get("missing_reasons"))[:4],
        },
        "atlas_summary": {
            "bucket_count": atlas.get("bucket_count"),
            "node_count": atlas.get("node_count"),
            "sample_buckets": [
                _bucket_ref(item)
                for item in _as_list(atlas.get("sample_buckets"))[:4]
                if isinstance(item, dict)
            ],
        },
        "spatial_readiness_contract": _as_dict(compact.get("spatial_readiness_contract")),
        "prompt_brief": _text(compact.get("prompt_brief"), limit=160),
        "policy": "micro_metamemory_for_ai_owned_route_shard_region_choice",
    }


def _region_ref_slug(value: Any) -> str:
    folded = _fold_text(value)
    return "_".join(folded.split())


def _allowed_landing_region_refs(brief: dict[str, Any], *, limit: int = 40) -> list[str]:
    compact = _compact_spatial_brief(brief)
    refs: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        text = _text(value, limit=120)
        if not text:
            return
        key = text.lower()
        if key in seen:
            return
        seen.add(key)
        refs.append(text)

    semantic_zones = _as_dict(compact.get("semantic_zones"))
    for zone_key in semantic_zones.keys():
        add(zone_key)
    topology_overlay = _as_dict(compact.get("topology_overlay_summary"))
    for lobe in _as_list(topology_overlay.get("density_lobes")):
        if isinstance(lobe, dict):
            add(lobe.get("region_ref") or lobe.get("lobe_id") or lobe.get("bucket_key"))
    for highway in _as_list(topology_overlay.get("active_highways")):
        if isinstance(highway, dict):
            add(highway.get("region_ref") or highway.get("highway_id") or highway.get("bucket_key"))
    for bridge in _as_list(topology_overlay.get("bridge_corridors")):
        if isinstance(bridge, dict):
            add(bridge.get("from_region"))
            add(bridge.get("to_region"))

    guide_areas = [_region_ref_slug(item) for item in _as_list(compact.get("guide_areas"))]
    radial_bands = [_region_ref_slug(item) for item in _as_list(compact.get("radial_bands"))]
    for guide_area in guide_areas:
        if not guide_area:
            continue
        add(guide_area)
        for band in radial_bands[:4]:
            if band:
                add(f"{guide_area}:{band}")

    nuclei = _as_dict(compact.get("nuclei"))
    for nucleus_key, nucleus in nuclei.items():
        if not isinstance(nucleus, dict):
            continue
        add(nucleus_key)
        nucleus_id = _text(nucleus.get("id") or nucleus.get("node_id"), limit=80)
        if nucleus_id:
            add(f"{nucleus_key}:{nucleus_id}")

    for bucket in _as_list(_as_dict(compact.get("atlas_summary")).get("sample_buckets")):
        if not isinstance(bucket, dict):
            continue
        bucket_key = _text(bucket.get("bucket_key"), limit=80)
        guide_area = _region_ref_slug(bucket.get("dominant_guide_area"))
        if guide_area:
            add(guide_area)
        if bucket_key:
            add(f"bucket:{bucket_key}")

    for gateway in _as_list(compact.get("highway_gateways")):
        if not isinstance(gateway, dict):
            continue
        gateway_key = _text(gateway.get("bucket_key"), limit=80)
        guide_area = _region_ref_slug(gateway.get("guide_area") or gateway.get("dominant_guide_area"))
        if guide_area:
            add(f"{guide_area}:gateway")
        if gateway_key:
            add(f"gateway:{gateway_key}")

    return refs[: max(1, int(limit))]


def _normalize_waypoint(value: Any, *, index: int) -> dict[str, Any] | None:
    payload = _as_dict(value)
    phrase = _text(payload.get("phrase") or payload.get("label") or payload.get("target"), limit=220)
    region = _text(payload.get("region_ref") or payload.get("region") or payload.get("landing_region_ref"), limit=120)
    coordinate = _coordinate(payload.get("coordinate") or payload.get("position"))
    if not phrase and not region and not coordinate:
        return None
    return {
        "waypoint_id": _text(payload.get("waypoint_id") or payload.get("id") or f"W{index}", limit=80),
        "phrase": phrase,
        "region_ref": region or None,
        "coordinate": coordinate,
        "radius": _bounded_float(payload.get("radius"), default=0.18, minimum=0.05, maximum=0.5),
        "rationale": _text(payload.get("rationale") or payload.get("why"), limit=260),
    }


def _normalize_path(value: Any, *, index: int, max_waypoints: int) -> dict[str, Any] | None:
    payload = _as_dict(value)
    answer_hypothesis = _text(payload.get("answer_hypothesis"), limit=360)
    region = _text(payload.get("landing_region_ref") or payload.get("region_ref") or payload.get("landing_region"), limit=160)
    coordinate = _coordinate(payload.get("landing_coordinate") or payload.get("coordinate") or payload.get("position"))
    novel_region_candidate = _text(payload.get("novel_region_candidate") or payload.get("novel_region") or payload.get("region_candidate"), limit=240)
    if not answer_hypothesis and not region and not coordinate and not novel_region_candidate:
        return None
    waypoints: list[dict[str, Any]] = []
    for waypoint_index, waypoint in enumerate(_as_list(payload.get("waypoints")), start=1):
        normalized = _normalize_waypoint(waypoint, index=waypoint_index)
        if normalized:
            waypoints.append(normalized)
        if len(waypoints) >= max_waypoints:
            break
    return {
        "path_id": _text(payload.get("path_id") or payload.get("id") or f"P{index}", limit=80),
        "strand_id": _text(payload.get("strand_id"), limit=80) or None,
        "answer_field": _text(payload.get("answer_field"), limit=80),
        "answer_hypothesis": answer_hypothesis,
        "goal": _text(payload.get("goal"), limit=80),
        "confidence": _bounded_float(payload.get("confidence"), default=0.62, minimum=0.0, maximum=1.0),
        "landing_region_ref": region or None,
        "landing_coordinate": coordinate,
        "novel_region_candidate": novel_region_candidate or None,
        "radius": _bounded_float(payload.get("radius"), default=0.22, minimum=0.08, maximum=0.5),
        "waypoints": waypoints,
        "bridge_targets": _dedupe_text(_as_list(payload.get("bridge_targets")), limit=6),
        "preferred_edges": _dedupe_text(_as_list(payload.get("preferred_edges")), limit=8),
        "forbidden_regions": _dedupe_text(_as_list(payload.get("forbidden_regions")), limit=6),
        "stop_condition": _text(payload.get("stop_condition"), limit=260),
        "spatial_rationale": _text(payload.get("spatial_rationale") or payload.get("rationale"), limit=360),
    }


def _normalize_payload(
    payload: dict[str, Any],
    *,
    query_text: str,
    retrieval_mode: str,
    brain_revision: str | None,
    metamemory_spatial_brief: dict[str, Any],
    started_at: float,
    source: str,
    cache_status: str,
    cache_hit: bool,
    max_paths: int,
    max_waypoints: int,
    cache_key_fingerprint: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    paths: list[dict[str, Any]] = []
    for index, path in enumerate(_as_list(payload.get("inverse_answer_paths")), start=1):
        normalized = _normalize_path(path, index=index, max_waypoints=max_waypoints)
        if normalized:
            paths.append(normalized)
        if len(paths) >= max_paths:
            break
    missing: list[str] = []
    if not paths:
        missing.append("inverse_answer_paths_missing")
    coordinate_count = sum(1 for path in paths if path.get("landing_coordinate"))
    region_count = sum(1 for path in paths if path.get("landing_region_ref"))
    if paths and coordinate_count + region_count <= 0:
        if any(path.get("novel_region_candidate") for path in paths):
            missing.append("novel_region_candidate_requires_backend_snap_review")
        else:
            missing.append("landing_region_or_coordinate_missing")
    waypoint_count = sum(len(_as_list(path.get("waypoints"))) for path in paths)
    materialized = bool(paths and not missing)
    status = "materialized" if materialized else "blocked"
    if error:
        missing.append(error)
        status = "blocked"
        materialized = False
    readiness = _as_dict(metamemory_spatial_brief.get("spatial_readiness_contract"))
    certification_blockers: list[str] = []
    if readiness and not bool(readiness.get("certifiable")):
        certification_blockers.append("metamemory_spatial_brief_incomplete")
        certification_blockers.extend(
            str(item)
            for item in _as_list(readiness.get("missing_reasons"))
            if str(item or "").strip()
        )
        certification_blockers.extend(
            str(item)
            for item in _as_list(readiness.get("stale_reasons"))
            if str(item or "").strip()
        )
    certifiable = bool(materialized and not certification_blockers)
    runtime_ms = round((time.perf_counter() - started_at) * 1000.0, 2)
    prompt_view = {
        "query": query_text,
        "retrieval_mode": retrieval_mode,
        "metamemory_revision": metamemory_spatial_brief.get("revision"),
        "brain_revision": brain_revision,
        "paths": paths,
    }
    return {
        "schema_version": AI_SPATIAL_LANDING_CONTRACT_SCHEMA_VERSION,
        "status": status,
        "materialization_state": status,
        "source": source,
        "materialized": materialized,
        "certifiable": certifiable,
        "query_text": _text(query_text, limit=500),
        "retrieval_mode": retrieval_mode,
        "brain_revision": brain_revision,
        "metamemory_revision": metamemory_spatial_brief.get("source_snapshot_version"),
        "metamemory_spatial_revision": metamemory_spatial_brief.get("revision"),
        "metamemory_spatial_hash": metamemory_spatial_brief.get("source_hash"),
        "matrix_revision": metamemory_spatial_brief.get("matrix_revision"),
        "topology_revision": metamemory_spatial_brief.get("topology_revision"),
        "atlas_revision": metamemory_spatial_brief.get("atlas_revision"),
        "calibration_revision": metamemory_spatial_brief.get("calibration_revision"),
        "planner_latency_ms": runtime_ms,
        "planner_summary": _text(payload.get("planner_summary"), limit=500),
        "uncertainty": _text(payload.get("uncertainty"), limit=320),
        "inverse_answer_paths": paths,
        "missing_reasons": _dedupe_text(missing + certification_blockers, limit=12),
        "spatial_readiness_contract": readiness,
        "metrics": {
            "ai_landing_count": len(paths),
            "ai_path_count": len(paths),
            "waypoint_count": waypoint_count,
            "coordinate_landing_count": coordinate_count,
            "region_landing_count": region_count,
            "novel_region_candidate_count": sum(1 for path in paths if path.get("novel_region_candidate")),
            "bridge_target_count": sum(len(_as_list(path.get("bridge_targets"))) for path in paths),
            "preferred_edge_count": sum(len(_as_list(path.get("preferred_edges"))) for path in paths),
        },
        "cache": {
            "status": cache_status,
            "hit": cache_hit,
            "validity": "valid" if materialized else "not_valid",
            "key_fingerprint": cache_key_fingerprint or _stable_hash(prompt_view)[:24],
            "persistent": cache_status not in {"disabled", "not_applicable"},
            "ttl_seconds": _AI_SPATIAL_CONTRACT_CACHE_TTL_SECONDS if cache_status not in {"disabled", "not_applicable"} else None,
        },
        "backend_contract": {
            "ai_owns_semantic_to_spatial_choice": True,
            "backend_snaps_and_traverses": True,
            "ai_can_propose_novel_region_candidate": True,
            "unsnappable_candidates_must_surface_as_wrong_region_or_matrix_review": True,
            "heuristic_can_only_propose_support": True,
            "llm_per_node_scoring": False,
        },
    }


def _sharded_spatial_planning_allowed(
    *,
    mode: str,
    mode_budget: dict[str, Any] | None,
    strands: list[dict[str, Any]],
    error: str | None,
) -> bool:
    payload = _as_dict(mode_budget)
    source = _text(payload.get("source"), limit=120).lower()
    if mode == "flash" and not _background_spatial_recovery_source(payload):
        return False
    if len(strands) < 2:
        return False
    if not _spatial_retryable_error(error or ""):
        return False
    if payload.get("cache_only"):
        return False
    if payload.get("first_payload_single_shot") and source not in {
        "background_first_ai_contract",
        "background_spatial_isolated",
        "bounded_complete_paths_route_worker",
    }:
        return False
    return True


def _build_sharded_spatial_contract_payload(
    *,
    provider: Callable[..., tuple[dict[str, Any] | None, str | None]],
    query_text: str,
    mode: str,
    semantic_contract: dict[str, Any] | None,
    semantic_contract_runtime: dict[str, Any] | None,
    strands: list[dict[str, Any]],
    identity_hints: dict[str, Any] | None,
    spatial_brief: dict[str, Any],
    mode_budget: dict[str, Any] | None,
    limits: dict[str, int | float],
    max_paths: int,
    max_waypoints: int,
    requested_timeout: float,
    runtime_fast: bool,
    primary_error: str | None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    selected = [dict(item) for item in strands[: max(1, max_paths)] if isinstance(item, dict)]
    if len(selected) < 2:
        return None, {}
    shard_count = min(len(selected), max(2, min(max_paths, 4)))
    shards = [[item] for item in selected[:shard_count]]
    source = _text(_as_dict(mode_budget).get("source"), limit=120).lower()
    if str(primary_error or "") == "direct_sharded_background_profile":
        if bool(_as_dict(mode_budget).get("bounded_route_single_shot")):
            if mode == "flash":
                shard_timeout = max(2.8, min(3.8, requested_timeout))
            elif mode in {"heavy", "forensic"}:
                shard_timeout = max(6.4, min(8.0, requested_timeout))
            else:
                shard_timeout = max(6.4, min(7.4, requested_timeout))
        elif source == "background_spatial_isolated" and mode == "flash":
            shard_timeout = max(4.6, min(5.8, requested_timeout))
        elif mode == "flash":
            shard_timeout = max(2.8, min(3.6, requested_timeout))
        elif mode in {"heavy", "forensic"}:
            shard_timeout = max(5.0, min(6.4, requested_timeout))
        else:
            shard_timeout = max(3.8, min(5.2, requested_timeout))
    else:
        shard_timeout = max(1.8, min(4.2, max(requested_timeout * 0.55, requested_timeout / max(1, len(shards)))))
    shard_tokens = max(180, min(320, int(limits["tokens"])))
    bounded_route_single_shot = bool(_as_dict(mode_budget).get("bounded_route_single_shot"))
    allowed_refs = _allowed_landing_region_refs(
        spatial_brief,
        limit=10 if bounded_route_single_shot else (14 if runtime_fast else 18),
    )
    shard_errors: list[str] = []
    shard_payloads: list[dict[str, Any]] = []

    def _call_shard(index: int, shard: list[dict[str, Any]]) -> dict[str, Any]:
        prompt_payload = {
            "schema_version": "agvm.ai_spatial_landing_prompt.shard.v1",
            "query": _text(query_text, limit=320 if bounded_route_single_shot else 420),
            "retrieval_mode": mode,
            "semantic_contract": _compact_semantic_contract(
                semantic_contract,
                semantic_contract_runtime,
                item_limit=max(2, min(4 if bounded_route_single_shot else 8, len(shard) * 3)),
            ),
            "answer_strands": shard,
            "identity_hints": _compact_identity_hints(identity_hints),
            "metamemory_spatial_brief": _compact_route_shard_spatial_brief(spatial_brief, retrieval_mode=mode)
            if bounded_route_single_shot
            else _compact_runtime_spatial_brief(spatial_brief, retrieval_mode=mode)
            if runtime_fast
            else _compact_retry_spatial_brief(spatial_brief),
            "allowed_landing_region_refs": allowed_refs,
            "limits": {"max_paths": max(1, min(2, max_paths)), "max_waypoints_per_path": max(1, min(1, max_waypoints))},
            "shard": {
                "index": index,
                "count": len(shards),
                "policy": "independent_ai_owned_spatial_mission_for_multi_intent_timeout_recovery",
                "primary_error": _text(primary_error or "", limit=180),
            },
        }
        payload, error = provider(
            model=retrieval_model(),
            system_prompt=(
                "You are AGVM's sharded AI spatial landing planner. Do not answer the user. "
                "Resolve only this shard's inverse answer strand into one compact spatial mission. "
                "Choose landing_region_ref from allowed_landing_region_refs when one fits. If none fits, return landing_coordinate "
                "or novel_region_candidate so the backend can snap, reject, or mark wrong_region/needs_matrix_review explicitly."
            ),
            user_prompt=json.dumps(prompt_payload, ensure_ascii=False),
            schema_name="agvm_ai_spatial_landing_contract_v1_shard",
            schema=_spatial_fast_contract_schema(max_paths=max(1, min(2, max_paths))),
            timeout=shard_timeout,
            role="retrieval",
            max_output_tokens=shard_tokens,
        )
        return {
            "index": index,
            "payload": payload if isinstance(payload, dict) else {},
            "error": error or (None if isinstance(payload, dict) else "llm_empty"),
        }

    max_workers = max(1, min(4, len(shards)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="agvm_spatial_shard") as executor:
        futures = [executor.submit(_call_shard, index, shard) for index, shard in enumerate(shards, start=1)]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            error = _text(result.get("error"), limit=220)
            if error:
                shard_errors.append(f"shard_{result.get('index')}:{error}")
                continue
            payload = _as_dict(result.get("payload"))
            normalized = _normalize_payload(
                payload,
                query_text=query_text,
                retrieval_mode=mode,
                brain_revision=None,
                metamemory_spatial_brief=spatial_brief,
                started_at=time.perf_counter(),
                source="fresh_llm_shard",
                cache_status="not_applicable",
                cache_hit=False,
                max_paths=max(1, min(2, max_paths)),
                max_waypoints=max(1, min(1, max_waypoints)),
                error=None,
            )
            if not bool(normalized.get("materialized")):
                shard_errors.append(
                    f"shard_{result.get('index')}:{','.join(str(item) for item in list(normalized.get('missing_reasons') or []))}"
                )
                continue
            for path in list(normalized.get("inverse_answer_paths") or []):
                path_payload = dict(path)
                path_payload["path_id"] = f"S{result.get('index')}::{path_payload.get('path_id') or len(shard_payloads) + 1}"
                path_payload["spatial_rationale"] = _text(
                    path_payload.get("spatial_rationale")
                    or f"Recovered from independent AI spatial shard {result.get('index')}.",
                    limit=360,
                )
                shard_payloads.append(path_payload)
                if len(shard_payloads) >= max_paths:
                    break
            if len(shard_payloads) >= max_paths:
                break

    if not shard_payloads:
        return None, {
            "schema_version": "agvm.ai_spatial_sharded_recovery.v1",
            "attempted": True,
            "recovered": False,
            "shard_count": len(shards),
            "errors": shard_errors[:8],
            "shard_timeout_seconds": round(shard_timeout, 3),
        }
    return {
        "planner_summary": "Recovered AI spatial missions by splitting independent inverse answer strands.",
        "inverse_answer_paths": shard_payloads[:max_paths],
        "uncertainty": "One or more spatial shards recovered after a monolithic planner timeout.",
    }, {
        "schema_version": "agvm.ai_spatial_sharded_recovery.v1",
        "attempted": True,
        "recovered": True,
        "shard_count": len(shards),
        "recovered_path_count": len(shard_payloads[:max_paths]),
        "errors": shard_errors[:8],
        "shard_timeout_seconds": round(shard_timeout, 3),
        "max_workers": max_workers,
    }


def build_ai_spatial_landing_contract(
    *,
    query_text: str,
    retrieval_mode: str,
    semantic_contract: dict[str, Any] | None,
    semantic_contract_runtime: dict[str, Any] | None,
    answer_strands: list[dict[str, Any]] | None,
    identity_hints: dict[str, Any] | None,
    metamemory_spatial_brief: dict[str, Any] | None,
    mode_budget: dict[str, Any] | None,
    brain_revision: str | None,
    allow_ai: bool = True,
    deferred: bool = False,
    timeout: float | None = None,
    cache_scope: str | None = None,
    use_cache: bool = True,
    structured_json_fn: Callable[..., tuple[dict[str, Any] | None, str | None]] | None = None,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    mode = _text(retrieval_mode or "balanced").lower() or "balanced"
    limits = _mode_limits(mode, mode_budget)
    max_paths = int(limits["max_paths"])
    max_waypoints = int(limits["max_waypoints"])
    spatial_brief = _as_dict(metamemory_spatial_brief)
    cache_enabled = bool(
        use_cache
        and str(cache_scope or "").strip()
        and (
            str(brain_revision or "").strip()
            or str(spatial_brief.get("brain_revision") or "").strip()
            or str(spatial_brief.get("source_hash") or "").strip()
        )
    )
    cache_key = ""
    if cache_enabled:
        cache_signature = _spatial_contract_cache_signature(
            query_text=query_text,
            retrieval_mode=mode,
            semantic_contract=semantic_contract,
            semantic_contract_runtime=semantic_contract_runtime,
            answer_strands=[_as_dict(item) for item in _as_list(answer_strands) if isinstance(item, dict)],
            metamemory_spatial_brief=spatial_brief,
            mode_budget=mode_budget,
            brain_revision=brain_revision,
            cache_scope=cache_scope,
        )
        cache_key = _spatial_contract_cache_key(
            query_text=query_text,
            retrieval_mode=mode,
            semantic_contract=semantic_contract,
            semantic_contract_runtime=semantic_contract_runtime,
            answer_strands=[_as_dict(item) for item in _as_list(answer_strands) if isinstance(item, dict)],
            metamemory_spatial_brief=spatial_brief,
            mode_budget=mode_budget,
            brain_revision=brain_revision,
            cache_scope=cache_scope,
        )
        cached = _get_spatial_contract_cache_entry(cache_key, signature=cache_signature)
        if cached:
            return _contract_from_cache_entry(cached, cache_key=cache_key, started_at=started_at)
    else:
        cache_signature = {}
    if not spatial_brief or str(spatial_brief.get("schema_version") or "") != "agvm.metamemory_spatial_brief.v1":
        return _normalize_payload(
            {},
            query_text=query_text,
            retrieval_mode=mode,
            brain_revision=brain_revision,
            metamemory_spatial_brief=spatial_brief,
            started_at=started_at,
            source="blocked_missing_metamemory",
            cache_status="disabled",
            cache_hit=False,
            max_paths=max_paths,
            max_waypoints=max_waypoints,
            cache_key_fingerprint=cache_key[:24] if cache_key else None,
            error="metamemory_spatial_brief_missing",
        )
    if deferred:
        return _normalize_payload(
            {},
            query_text=query_text,
            retrieval_mode=mode,
            brain_revision=brain_revision,
            metamemory_spatial_brief=spatial_brief,
            started_at=started_at,
            source="deferred",
            cache_status="disabled",
            cache_hit=False,
            max_paths=max_paths,
            max_waypoints=max_waypoints,
            cache_key_fingerprint=cache_key[:24] if cache_key else None,
            error="ai_spatial_contract_deferred",
        )
    strands = _compact_answer_strands([_as_dict(item) for item in _as_list(answer_strands) if isinstance(item, dict)], max_items=max_paths)
    if not strands:
        return _normalize_payload(
            {},
            query_text=query_text,
            retrieval_mode=mode,
            brain_revision=brain_revision,
            metamemory_spatial_brief=spatial_brief,
            started_at=started_at,
            source="blocked_missing_answer_strands",
            cache_status="disabled",
            cache_hit=False,
            max_paths=max_paths,
            max_waypoints=max_waypoints,
            cache_key_fingerprint=cache_key[:24] if cache_key else None,
            error="inverse_answer_strands_missing",
        )
    if not allow_ai or not llm_enabled():
        return _normalize_payload(
            {},
            query_text=query_text,
            retrieval_mode=mode,
            brain_revision=brain_revision,
            metamemory_spatial_brief=spatial_brief,
            started_at=started_at,
            source="ai_unavailable",
            cache_status="disabled",
            cache_hit=False,
            max_paths=max_paths,
            max_waypoints=max_waypoints,
            cache_key_fingerprint=cache_key[:24] if cache_key else None,
            error="llm_disabled",
        )
    runtime_fast = _runtime_fast_spatial_planning(mode_budget)
    first_payload_single_shot = _first_payload_single_shot_spatial_planning(mode_budget)
    extended_single_call = _extended_single_call_spatial_planning(mode_budget)
    semantic_item_limit = max(3, min(12, max_paths * (2 if runtime_fast else 3)))
    spatial_prompt_brief = (
        _compact_first_payload_spatial_brief(spatial_brief, retrieval_mode=mode)
        if first_payload_single_shot
        else _compact_runtime_spatial_brief(spatial_brief, retrieval_mode=mode)
        if runtime_fast
        else _compact_retry_spatial_brief(spatial_brief)
    )
    allowed_region_limit = (
        8
        if first_payload_single_shot and mode == "flash"
        else 12
        if first_payload_single_shot
        else 12
        if mode == "flash"
        else (18 if runtime_fast and mode == "balanced" else (24 if runtime_fast else 36))
    )
    prompt_payload = {
        "schema_version": "agvm.ai_spatial_landing_prompt.v1",
        "query": _text(query_text, limit=420 if first_payload_single_shot else 700),
        "retrieval_mode": mode,
        "mode_budget": _as_dict(mode_budget),
        "semantic_contract": _compact_semantic_contract(
            semantic_contract,
            semantic_contract_runtime,
            item_limit=semantic_item_limit,
        ),
        "answer_strands": strands,
        "identity_hints": _compact_identity_hints(identity_hints),
        "metamemory_spatial_brief": spatial_prompt_brief,
        "allowed_landing_region_refs": _allowed_landing_region_refs(
            spatial_brief,
            limit=allowed_region_limit,
        ),
        "limits": {"max_paths": max_paths, "max_waypoints_per_path": max_waypoints},
        "runtime_contract": {
            "micro_contract": bool(runtime_fast),
            "policy": "ai_selects_spatial_regions_backend_snaps_and_traverses",
        },
    }
    provider = structured_json_fn or structured_json
    requested_timeout = float(timeout or limits["timeout"])
    sharded_recovery: dict[str, Any] = {}
    direct_sharded_preferred = _prefer_direct_sharded_spatial_planning(mode=mode, mode_budget=mode_budget, strands=strands)
    if direct_sharded_preferred:
        sharded_payload, sharded_recovery = _build_sharded_spatial_contract_payload(
            provider=provider,
            query_text=query_text,
            mode=mode,
            semantic_contract=semantic_contract,
            semantic_contract_runtime=semantic_contract_runtime,
            strands=strands,
            identity_hints=identity_hints,
            spatial_brief=spatial_brief,
            mode_budget=mode_budget,
            limits=limits,
            max_paths=max_paths,
            max_waypoints=max_waypoints,
            requested_timeout=requested_timeout,
            runtime_fast=runtime_fast,
            primary_error="direct_sharded_background_profile",
        )
        if isinstance(sharded_payload, dict):
            normalized = _normalize_payload(
                sharded_payload,
                query_text=query_text,
                retrieval_mode=mode,
                brain_revision=brain_revision,
                metamemory_spatial_brief=spatial_brief,
                started_at=started_at,
                source="fresh_llm_sharded",
                cache_status="miss" if cache_enabled else "disabled",
                cache_hit=False,
                max_paths=max_paths,
                max_waypoints=max(1, min(max_waypoints, 1)),
                cache_key_fingerprint=cache_key[:24] if cache_key else None,
            )
            normalized["materialization_state"] = normalized.get("status")
            normalized["provider_retry_policy"] = {
                "schema_version": "agvm.ai_spatial_provider_retry_policy.v1",
                "retry_used": False,
                "retry_status": "direct_sharded",
                "retry_skipped_reason": None,
                "primary_error": None,
                "retry_error": None,
                "primary_timeout_seconds": None,
                "primary_elapsed_seconds": None,
                "first_payload_total_timeout_seconds": round(requested_timeout, 3),
                "retry_timeout_seconds": None,
                "retry_compact_prompt": False,
                "runtime_micro_contract": bool(runtime_fast),
                "primary_profile": "direct_sharded_background",
                "sharded_recovery": sharded_recovery or {"attempted": False},
            }
            if cache_enabled and cache_key:
                _store_spatial_contract_cache_entry(cache_key, contract=normalized, signature=cache_signature)
            return normalized
        if _bounded_route_spatial_planning(mode_budget):
            normalized_error = _normalize_payload(
                {},
                query_text=query_text,
                retrieval_mode=mode,
                brain_revision=brain_revision,
                metamemory_spatial_brief=spatial_brief,
                started_at=started_at,
                source="llm_error",
                cache_status="miss" if cache_enabled else "disabled",
                cache_hit=False,
                max_paths=max_paths,
                max_waypoints=max_waypoints,
                cache_key_fingerprint=cache_key[:24] if cache_key else None,
                error="direct_sharded_ai_spatial_failed",
            )
            normalized_error["provider_retry_policy"] = {
                "schema_version": "agvm.ai_spatial_provider_retry_policy.v1",
                "retry_used": False,
                "retry_status": "direct_sharded_failed",
                "retry_skipped_reason": "bounded_route_direct_sharded_failed_no_slow_monolith",
                "primary_error": None,
                "retry_error": None,
                "primary_timeout_seconds": None,
                "primary_elapsed_seconds": None,
                "first_payload_total_timeout_seconds": round(requested_timeout, 3),
                "retry_timeout_seconds": None,
                "retry_compact_prompt": False,
                "runtime_micro_contract": bool(runtime_fast),
                "primary_profile": "direct_sharded_background",
                "sharded_recovery": sharded_recovery or {"attempted": False},
            }
            return normalized_error
    primary_timeout = requested_timeout
    first_payload_retry_timeout: float | None = None
    if first_payload_single_shot and mode != "flash" and requested_timeout >= 2.2:
        primary_timeout = (
            _extended_single_call_primary_timeout_seconds(mode, requested_timeout)
            if extended_single_call
            else _first_payload_primary_timeout_seconds(mode, requested_timeout)
        )
    primary_started = time.perf_counter()
    payload, error = provider(
        model=retrieval_model(),
        system_prompt=(
            "You are AGVM's AI spatial landing planner. Do not answer the user. "
            "Use inverse retrieval: map answer-like hypotheses to brain space from the metamemory spatial brief. "
            "The full brain is not available. Prefer landing_region_ref from allowed_landing_region_refs, but if none fits, "
            "return landing_coordinate or novel_region_candidate so the backend can snap, reject, or mark wrong_region/needs_matrix_review explicitly. "
            "Do not invent final facts. "
            "Each path must preserve the strand goal, choose a landing, set a local read radius, preferred edge types and stop condition. "
            "The backend will snap coordinates to buckets/nodes, discover nearby waypoints and traverse deterministically. "
            "Heuristics may support but may not own certification. Keep output compact and truthful."
        ),
        user_prompt=json.dumps(prompt_payload, ensure_ascii=False),
        schema_name="agvm_ai_spatial_landing_contract_v1",
        schema=_spatial_fast_contract_schema(max_paths=max_paths),
        timeout=primary_timeout,
        role="retrieval",
        max_output_tokens=int(limits["tokens"]),
    )
    primary_elapsed_seconds = max(0.0, time.perf_counter() - primary_started)
    if first_payload_single_shot and mode != "flash" and requested_timeout >= 2.2:
        first_payload_retry_timeout = _first_payload_retry_timeout_seconds(
            mode,
            requested_timeout,
            primary_elapsed_seconds,
        )
    retry_used = False
    bounded_route_single_shot = bool(_as_dict(mode_budget).get("bounded_route_single_shot"))
    primary_error = error or ("llm_empty" if not isinstance(payload, dict) else None)
    retry_error: str | None = None
    normalization_max_paths = max_paths
    normalization_max_waypoints = max_waypoints
    # Flash keeps the strict single-shot latency contract. Balanced/heavy first
    # payloads and bounded route workers get one ultra-compact retry when there
    # is real budget left; otherwise a single transport timeout can erase the
    # AI-owned spatial mission and leave path-aware rows with no route truth.
    first_payload_no_retry_budget = bool(
        first_payload_single_shot
        and not bounded_route_single_shot
        and mode != "flash"
        and primary_error
        and first_payload_retry_timeout is None
    )
    bounded_route_no_retry_budget = bool(
        bounded_route_single_shot
        and primary_error
        and first_payload_retry_timeout is None
    )
    skip_retry = bool(
        # Flash first-payload calls keep a strict single-shot boundary. Background
        # and bounded-route flash calls are still AI-owned, but they must be able
        # to recover from one transport timeout instead of permanently blocking
        # the spatial contract.
        (_spatial_timeout_error(error) and mode == "flash" and first_payload_single_shot)
        or (first_payload_single_shot and primary_error and mode == "flash")
        or first_payload_no_retry_budget
        or bounded_route_no_retry_budget
    )
    if (error or not isinstance(payload, dict)) and not skip_retry and _spatial_retryable_error(error or "llm_empty"):
        retry_used = True
        retry_max_paths = max(1, min(max_paths, 3))
        retry_max_waypoints = max(1, min(max_waypoints, 2))
        retry_prompt_payload = {
            "schema_version": "agvm.ai_spatial_landing_prompt.compact_retry.v1",
            "query": _text(query_text, limit=420),
            "retrieval_mode": mode,
            "semantic_contract": _compact_semantic_contract(
                semantic_contract,
                semantic_contract_runtime,
                item_limit=max(3, min(8, retry_max_paths * 2)),
            ),
            "answer_strands": strands[:retry_max_paths],
            "identity_hints": _compact_identity_hints(identity_hints),
            "metamemory_spatial_brief": _compact_runtime_spatial_brief(spatial_brief, retrieval_mode=mode)
            if runtime_fast
            else _compact_retry_spatial_brief(spatial_brief),
            "allowed_landing_region_refs": _allowed_landing_region_refs(
                spatial_brief,
                limit=12 if mode == "flash" else (18 if runtime_fast else 24),
            ),
            "limits": {"max_paths": retry_max_paths, "max_waypoints_per_path": retry_max_waypoints},
            "retry_reason": _text(error or "llm_empty", limit=180),
        }
        retry_timeout = (
            first_payload_retry_timeout
            if first_payload_retry_timeout is not None
            else _spatial_retry_timeout_seconds(mode, requested_timeout)
        )
        retry_payload, retry_error = provider(
            model=retrieval_model(),
            system_prompt=(
                "You are AGVM's compact AI spatial landing retry. Do not answer the user. "
                "Return only the smallest valid spatial plan: one to three inverse answer paths, "
                "each with a landing_region_ref chosen from allowed_landing_region_refs when possible, or a landing_coordinate/novel_region_candidate when no listed region fits, plus radius, preferred edges and stop condition. "
                "Use the metamemory brief; the backend will snap and traverse."
            ),
            user_prompt=json.dumps(retry_prompt_payload, ensure_ascii=False),
            schema_name="agvm_ai_spatial_landing_contract_v1_retry",
            schema=_spatial_fast_contract_schema(max_paths=retry_max_paths),
            timeout=retry_timeout,
            role="retrieval",
            max_output_tokens=min(420, int(limits["tokens"])),
        )
        if isinstance(retry_payload, dict) and not retry_error:
            payload = retry_payload
            error = None
            normalization_max_paths = retry_max_paths
            normalization_max_waypoints = retry_max_waypoints
        else:
            error = f"{error or 'llm_empty'};retry:{retry_error or 'llm_empty'}"
    if error or not isinstance(payload, dict):
        if _sharded_spatial_planning_allowed(
            mode=mode,
            mode_budget=mode_budget,
            strands=strands,
            error=error or primary_error or "llm_empty",
        ):
            sharded_payload, sharded_recovery = _build_sharded_spatial_contract_payload(
                provider=provider,
                query_text=query_text,
                mode=mode,
                semantic_contract=semantic_contract,
                semantic_contract_runtime=semantic_contract_runtime,
                strands=strands,
                identity_hints=identity_hints,
                spatial_brief=spatial_brief,
                mode_budget=mode_budget,
                limits=limits,
                max_paths=max_paths,
                max_waypoints=max_waypoints,
                requested_timeout=requested_timeout,
                runtime_fast=runtime_fast,
                primary_error=error or primary_error,
            )
            if isinstance(sharded_payload, dict):
                payload = sharded_payload
                error = None
                normalization_max_paths = max_paths
                normalization_max_waypoints = max(1, min(max_waypoints, 1))
    if error or not isinstance(payload, dict):
        normalized_error = _normalize_payload(
            {},
            query_text=query_text,
            retrieval_mode=mode,
            brain_revision=brain_revision,
            metamemory_spatial_brief=spatial_brief,
            started_at=started_at,
            source="llm_error",
            cache_status="miss" if cache_enabled else "disabled",
            cache_hit=False,
            max_paths=max_paths,
            max_waypoints=max_waypoints,
            cache_key_fingerprint=cache_key[:24] if cache_key else None,
            error=error or "llm_empty",
        )
        normalized_error["provider_retry_policy"] = {
            "schema_version": "agvm.ai_spatial_provider_retry_policy.v1",
            "retry_used": bool(retry_used),
            "retry_status": (
                "failed_after_sharded_recovery"
                if bool(sharded_recovery.get("attempted"))
                else "failed"
                if retry_used
                else ("skipped" if skip_retry else "not_needed")
            ),
            "retry_skipped_reason": (
                "bounded_route_timeout_no_sync_retry"
                if skip_retry and bounded_route_single_shot
                else "first_payload_retry_budget_exhausted"
                if skip_retry and first_payload_no_retry_budget
                else "runtime_timeout_no_sync_retry"
                if skip_retry and runtime_fast
                else ("flash_timeout_no_sync_retry" if skip_retry else None)
            ),
            "primary_error": _text(primary_error, limit=240) if primary_error else None,
            "retry_error": _text(retry_error, limit=240) if retry_error else None,
            "primary_timeout_seconds": round(primary_timeout, 3),
            "primary_elapsed_seconds": round(primary_elapsed_seconds, 3),
            "first_payload_total_timeout_seconds": round(requested_timeout, 3),
            "retry_timeout_seconds": (
                round(first_payload_retry_timeout, 3)
                if retry_used and first_payload_retry_timeout is not None
                else round(_spatial_retry_timeout_seconds(mode, requested_timeout), 3)
                if retry_used
                else None
            ),
            "retry_compact_prompt": bool(retry_used),
            "runtime_micro_contract": bool(runtime_fast),
            "primary_profile": "extended_single_call" if extended_single_call else "first_payload_split",
            "sharded_recovery": sharded_recovery or {"attempted": False},
        }
        return normalized_error
    normalized = _normalize_payload(
        payload,
        query_text=query_text,
        retrieval_mode=mode,
        brain_revision=brain_revision,
        metamemory_spatial_brief=spatial_brief,
        started_at=started_at,
        source="fresh_llm",
        cache_status="miss" if cache_enabled else "disabled",
        cache_hit=False,
        max_paths=normalization_max_paths,
        max_waypoints=normalization_max_waypoints,
        cache_key_fingerprint=cache_key[:24] if cache_key else None,
    )
    normalized["provider_retry_policy"] = {
        "schema_version": "agvm.ai_spatial_provider_retry_policy.v1",
        "retry_used": bool(retry_used),
        "retry_status": (
            "sharded_recovered"
            if bool(sharded_recovery.get("recovered"))
            else "recovered"
            if retry_used and normalized.get("materialized")
            else ("failed" if retry_used else "not_needed")
        ),
        "retry_skipped_reason": (
            "bounded_route_timeout_no_sync_retry"
            if skip_retry and bounded_route_single_shot
            else "first_payload_retry_budget_exhausted"
            if skip_retry and first_payload_no_retry_budget
            else "runtime_timeout_no_sync_retry"
            if skip_retry and runtime_fast
            else ("flash_timeout_no_sync_retry" if skip_retry else None)
        ),
        "primary_error": _text(primary_error, limit=240) if primary_error else None,
        "retry_error": _text(retry_error, limit=240) if retry_error else None,
        "primary_timeout_seconds": round(primary_timeout, 3),
        "primary_elapsed_seconds": round(primary_elapsed_seconds, 3),
        "first_payload_total_timeout_seconds": round(requested_timeout, 3),
        "retry_timeout_seconds": (
            round(first_payload_retry_timeout, 3)
            if retry_used and first_payload_retry_timeout is not None
            else round(_spatial_retry_timeout_seconds(mode, requested_timeout), 3)
            if retry_used
            else None
        ),
        "retry_compact_prompt": bool(retry_used),
        "runtime_micro_contract": bool(runtime_fast),
        "primary_profile": "extended_single_call" if extended_single_call else "first_payload_split",
        "sharded_recovery": sharded_recovery or {"attempted": False},
    }
    if sharded_recovery:
        normalized["source"] = "fresh_llm_sharded"
        normalized["materialization_state"] = normalized.get("status")
    if cache_enabled and cache_key:
        _store_spatial_contract_cache_entry(cache_key, contract=normalized, signature=cache_signature)
    return normalized
