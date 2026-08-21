from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from typing import Any

from memory_learning import MATRIX_REVISION_SCHEMA_VERSION, TOPOLOGY_FIELD_REVISION_SCHEMA_VERSION
from storage import utc_timestamp


MATRIX_CALIBRATION_REVISION_BUNDLE_SCHEMA_VERSION = "agvm.matrix_calibration_revision_bundle.v1"


def _json_digest(payload: Any, *, length: int = 18) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def _dict_value(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _list_value(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _position(value: Any) -> dict[str, float] | None:
    source = _dict_value(value)
    if not {"x", "y", "z"}.issubset(source):
        return None
    try:
        return {
            "x": round(float(source["x"]), 6),
            "y": round(float(source["y"]), 6),
            "z": round(float(source["z"]), 6),
        }
    except (TypeError, ValueError):
        return None


def _metric_snapshot(report: dict[str, Any] | None) -> dict[str, Any]:
    payload = _dict_value(report)
    metrics = _dict_value(payload.get("metrics"))
    quality = _dict_value(payload.get("quality"))
    checks = _dict_value(payload.get("checks"))
    recommendations = [
        str(item.get("recommendation") or item.get("reason") or item.get("code") or "").strip()
        for item in _list_value(payload.get("recommendations"))[:12]
        if isinstance(item, dict)
    ]
    proposal_count = len([item for item in _list_value(payload.get("calibration_proposals")) if isinstance(item, dict)])
    return {
        "score": _float_or_none(payload.get("score") or metrics.get("score") or quality.get("score")),
        "landing_density_score": _float_or_none(
            metrics.get("landing_density_score") or quality.get("landing_density_score") or payload.get("landing_density_score")
        ),
        "spacing_score": _float_or_none(metrics.get("spacing_score") or quality.get("spacing_score") or payload.get("spacing_score")),
        "radial_alignment_score": _float_or_none(
            metrics.get("radial_alignment_score") or quality.get("radial_alignment_score") or payload.get("radial_alignment_score")
        ),
        "node_count": int(payload.get("node_count") or metrics.get("node_count") or 0),
        "failed_checks": [
            str(key)
            for key, value in checks.items()
            if (isinstance(value, dict) and value.get("passed") is False) or value is False
        ][:24],
        "proposal_count": proposal_count,
        "recommendations": [item for item in recommendations if item][:12],
    }


def _node_lookup(graph: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    payload = _dict_value(graph)
    return {
        str(node.get("id") or ""): dict(node)
        for node in _list_value(payload.get("nodes"))
        if isinstance(node, dict) and str(node.get("id") or "").strip()
    }


def _update_rows(graph: dict[str, Any] | None, position_plan: dict[str, Any] | None) -> list[dict[str, Any]]:
    nodes = _node_lookup(graph)
    rows: list[dict[str, Any]] = []
    for item in _list_value(_dict_value(position_plan).get("updates")):
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("node_id") or "").strip()
        if not node_id:
            continue
        node = nodes.get(node_id, {})
        before = _position(node.get("final_position") or node.get("position") or item.get("from_position"))
        after = _position(item.get("to_position"))
        if not after:
            continue
        delta = (
            {
                "x": round(after["x"] - before["x"], 6),
                "y": round(after["y"] - before["y"], 6),
                "z": round(after["z"] - before["z"], 6),
            }
            if before
            else {"x": 0.0, "y": 0.0, "z": 0.0}
        )
        reason_codes = [str(code) for code in _list_value(item.get("reason_codes")) if str(code)]
        rows.append(
            {
                "node_id": node_id,
                "guide_area": str(
                    node.get("guide_area")
                    or _dict_value(node.get("provenance")).get("guide_conceptual_area")
                    or _dict_value(node.get("routing_facets")).get("guide_area")
                    or "unknown"
                ),
                "memory_type": str(node.get("memory_type") or "unknown"),
                "before": before,
                "after": after,
                "delta": delta,
                "delta_norm": round((delta["x"] ** 2 + delta["y"] ** 2 + delta["z"] ** 2) ** 0.5, 6),
                "reason_codes": reason_codes,
                "proposal_code": str(item.get("proposal_code") or ""),
            }
        )
    return rows


def _axis_transform(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"axis_delta_count": 0, "average_delta": {"x": 0.0, "y": 0.0, "z": 0.0}, "by_guide_area": []}
    totals = defaultdict(lambda: {"count": 0, "x": 0.0, "y": 0.0, "z": 0.0})
    overall = {"x": 0.0, "y": 0.0, "z": 0.0}
    for row in rows:
        delta = _dict_value(row.get("delta"))
        guide_area = str(row.get("guide_area") or "unknown")
        totals[guide_area]["count"] += 1
        for axis in ("x", "y", "z"):
            value = float(delta.get(axis) or 0.0)
            totals[guide_area][axis] += value
            overall[axis] += value
    count = max(1, len(rows))
    by_guide_area = []
    for guide_area, payload in sorted(totals.items(), key=lambda pair: (-int(pair[1]["count"]), pair[0]))[:24]:
        item_count = max(1, int(payload["count"]))
        by_guide_area.append(
            {
                "guide_area": guide_area,
                "count": item_count,
                "average_delta": {
                    "x": round(float(payload["x"]) / item_count, 6),
                    "y": round(float(payload["y"]) / item_count, 6),
                    "z": round(float(payload["z"]) / item_count, 6),
                },
            }
        )
    return {
        "axis_delta_count": len(rows),
        "average_delta": {axis: round(float(overall[axis]) / count, 6) for axis in ("x", "y", "z")},
        "by_guide_area": by_guide_area,
    }


def _radial_transform(rows: list[dict[str, Any]]) -> dict[str, Any]:
    band_reasons = Counter(
        reason
        for row in rows
        for reason in _list_value(row.get("reason_codes"))
        if "radial" in str(reason).lower() or "band" in str(reason).lower()
    )
    radius_deltas: list[float] = []
    for row in rows:
        before = _dict_value(row.get("before"))
        after = _dict_value(row.get("after"))
        if not before or not after:
            continue
        before_radius = (float(before.get("x") or 0.0) ** 2 + float(before.get("y") or 0.0) ** 2 + float(before.get("z") or 0.0) ** 2) ** 0.5
        after_radius = (float(after.get("x") or 0.0) ** 2 + float(after.get("y") or 0.0) ** 2 + float(after.get("z") or 0.0) ** 2) ** 0.5
        radius_deltas.append(round(after_radius - before_radius, 6))
    return {
        "radial_update_count": len(radius_deltas),
        "average_radius_delta": round(sum(radius_deltas) / max(1, len(radius_deltas)), 6) if radius_deltas else 0.0,
        "reason_counts": dict(band_reasons.most_common(24)),
    }


def _guide_transform(rows: list[dict[str, Any]], position_plan: dict[str, Any] | None) -> dict[str, Any]:
    zone_counts = _dict_value(_dict_value(position_plan).get("zone_update_counts"))
    guide_counts = Counter(str(row.get("guide_area") or "unknown") for row in rows)
    reason_counts = Counter(reason for row in rows for reason in _list_value(row.get("reason_codes")))
    return {
        "updated_guide_areas": dict(guide_counts.most_common(24)),
        "planner_zone_update_counts": {str(key): int(value or 0) for key, value in zone_counts.items()},
        "reason_counts": dict(reason_counts.most_common(32)),
    }


def _topology_priors(rows: list[dict[str, Any]], before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    attraction: list[dict[str, Any]] = []
    repulsion: list[dict[str, Any]] = []
    rotation: list[dict[str, Any]] = []
    for row in rows[:80]:
        reason_text = " ".join(_list_value(row.get("reason_codes")) + [str(row.get("proposal_code") or "")]).lower()
        payload = {
            "node_id": row.get("node_id"),
            "guide_area": row.get("guide_area"),
            "target_position": row.get("after"),
            "delta_norm": row.get("delta_norm"),
            "reason_codes": row.get("reason_codes"),
        }
        if any(token in reason_text for token in ("density", "spacing", "collision", "repulsion", "jitter")):
            repulsion.append({**payload, "prior_kind": "local_density_repulsion"})
        elif any(token in reason_text for token in ("radial", "band", "alignment", "attraction")):
            attraction.append({**payload, "prior_kind": "radial_alignment_attraction"})
        else:
            rotation.append({**payload, "hint_kind": "semantic_axis_rotation"})
    before_failed = set(str(item) for item in _list_value(before.get("failed_checks")))
    after_failed = set(str(item) for item in _list_value(after.get("failed_checks")))
    return {
        "attraction_priors": attraction[:32],
        "repulsion_priors": repulsion[:32],
        "rotation_hints": rotation[:32],
        "unstable_regions": [{"check": item, "state": "still_failed"} for item in sorted(after_failed)[:24]],
        "resolved_regions": [{"check": item, "state": "resolved_by_projection"} for item in sorted(before_failed - after_failed)[:24]],
        "density_constraints": {
            "landing_density_before": before.get("landing_density_score"),
            "landing_density_after": after.get("landing_density_score"),
            "spacing_before": before.get("spacing_score"),
            "spacing_after": after.get("spacing_score"),
            "updated_node_count": len(rows),
        },
    }


def build_matrix_calibration_revision_candidates(
    *,
    graph: dict[str, Any],
    before_report: dict[str, Any],
    position_plan: dict[str, Any],
    projected_report: dict[str, Any],
    brain_id: str,
    parent_matrix_revision_id: str | None = None,
    parent_topology_revision_id: str | None = None,
    source_event_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Create reviewable matrix/topology revision records from a calibration preview."""
    normalized_brain_id = str(brain_id or "").strip() or "default"
    plan = _dict_value(position_plan)
    plan_signature = str(plan.get("plan_signature") or _json_digest(plan))
    rows = _update_rows(graph, plan)
    before_quality = _metric_snapshot(before_report)
    after_quality = _metric_snapshot(projected_report)
    timestamp = utc_timestamp()
    source_ids = [str(item) for item in _list_value(source_event_ids) if str(item)]
    rollback_payload = {
        "schema_version": "agvm.matrix_revision_rollback_payload.v1",
        "plan_signature": plan_signature,
        "created_at": timestamp,
        "parent_matrix_revision_id": parent_matrix_revision_id,
        "parent_topology_revision_id": parent_topology_revision_id,
        "node_count": len(rows),
        "node_position_sample": [
            {
                "node_id": row.get("node_id"),
                "before": row.get("before"),
                "after": row.get("after"),
            }
            for row in rows[:50]
        ],
    }
    matrix_seed = {
        "brain_id": normalized_brain_id,
        "plan_signature": plan_signature,
        "parent_matrix_revision_id": parent_matrix_revision_id,
        "semantic_axis_transform": _axis_transform(rows),
        "radial_band_transform": _radial_transform(rows),
        "guide_area_transform": _guide_transform(rows, plan),
        "quality_before": before_quality,
        "quality_after": after_quality,
    }
    matrix_revision_id = f"matrix_revision::{_json_digest(matrix_seed)}"
    topology_payload = _topology_priors(rows, before_quality, after_quality)
    topology_seed = {
        "brain_id": normalized_brain_id,
        "matrix_revision_id": matrix_revision_id,
        "plan_signature": plan_signature,
        "attraction_priors": topology_payload["attraction_priors"],
        "repulsion_priors": topology_payload["repulsion_priors"],
        "rotation_hints": topology_payload["rotation_hints"],
        "density_constraints": topology_payload["density_constraints"],
    }
    topology_revision_id = f"topology_revision::{_json_digest(topology_seed)}"
    saturated_regions = [
        {
            "guide_area": guide_area,
            "update_count": count,
            "reason": "calibration_updated_many_nodes_in_region",
        }
        for guide_area, count in Counter(str(row.get("guide_area") or "unknown") for row in rows).most_common(12)
        if count >= 2
    ]
    bridge_corridors = [
        {
            "bridge_id": f"matrix_bridge::{_json_digest({'node_id': row.get('node_id'), 'after': row.get('after')}, length=10)}",
            "node_id": row.get("node_id"),
            "guide_area": row.get("guide_area"),
            "target_position": row.get("after"),
            "source": "matrix_calibration_projection",
        }
        for row in rows[:24]
        if any("bridge" in str(reason).lower() or "highway" in str(reason).lower() for reason in _list_value(row.get("reason_codes")))
    ]
    matrix_revision = {
        "schema_version": MATRIX_REVISION_SCHEMA_VERSION,
        "matrix_revision_id": matrix_revision_id,
        "brain_id": normalized_brain_id,
        "parent_revision_id": parent_matrix_revision_id,
        "base_projection_version": "normalized_semantic_xyz.v1",
        "semantic_axis_transform": matrix_seed["semantic_axis_transform"],
        "radial_band_transform": matrix_seed["radial_band_transform"],
        "guide_area_transform": matrix_seed["guide_area_transform"],
        "quality_before": before_quality,
        "quality_after": after_quality,
        "source_event_ids": source_ids,
        "apply_policy": "preview_apply_required",
        "rollback_payload": rollback_payload,
        "created_at": timestamp,
    }
    topology_revision = {
        "schema_version": TOPOLOGY_FIELD_REVISION_SCHEMA_VERSION,
        "topology_revision_id": topology_revision_id,
        "brain_id": normalized_brain_id,
        "matrix_revision_id": matrix_revision_id,
        "attraction_priors": topology_payload["attraction_priors"],
        "repulsion_priors": topology_payload["repulsion_priors"],
        "rotation_hints": topology_payload["rotation_hints"],
        "density_constraints": topology_payload["density_constraints"],
        "bridge_corridors": bridge_corridors,
        "unstable_regions": topology_payload["unstable_regions"],
        "saturated_regions": saturated_regions,
        "source_event_ids": source_ids,
        "quality_before": before_quality,
        "quality_after": after_quality,
        "apply_policy": "preview_apply_required",
        "rollback_payload": rollback_payload,
        "created_at": timestamp,
    }
    node_annotations = [
        {
            "node_id": row["node_id"],
            "matrix_revision_id": matrix_revision_id,
            "topology_revision_id": topology_revision_id,
            "plan_signature": plan_signature,
        }
        for row in rows
    ]
    return {
        "schema_version": MATRIX_CALIBRATION_REVISION_BUNDLE_SCHEMA_VERSION,
        "brain_id": normalized_brain_id,
        "plan_signature": plan_signature,
        "candidate_count": 2,
        "node_annotation_count": len(node_annotations),
        "source_event_ids": source_ids,
        "matrix_revision": matrix_revision,
        "topology_field_revision": topology_revision,
        "node_revision_annotations": node_annotations,
        "metamemory_signature": {
            "matrix_revision_id": matrix_revision_id,
            "topology_revision_id": topology_revision_id,
            "plan_signature": plan_signature,
            "expected_active_after_apply": True,
        },
        "mutation_policy": {
            "preview_only": True,
            "hidden_mutation_allowed": False,
            "apply_requires_confirm_apply": True,
            "apply_requires_rollback_consent": True,
        },
    }
