from __future__ import annotations

import math
import hashlib
import json
from collections import Counter, defaultdict
from typing import Any

from projection import distance, position_to_bucket
from storage import utc_timestamp


SCHEMA_VERSION = "agvm.pr12g.brain_geometry_calibration.v1"
_ORIGIN = {"x": 0.0, "y": 0.0, "z": 0.0}


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _position(node: dict[str, Any]) -> dict[str, float] | None:
    for key in ("final_position", "base_position"):
        raw = node.get(key)
        if not isinstance(raw, dict):
            continue
        try:
            return {"x": float(raw["x"]), "y": float(raw["y"]), "z": float(raw["z"])}
        except (KeyError, TypeError, ValueError):
            continue
    return None


def _radius(node: dict[str, Any]) -> float | None:
    position = _position(node)
    if not position:
        return None
    return distance(position, _ORIGIN)


def _guide_area(node: dict[str, Any]) -> str:
    return str((node.get("provenance") or {}).get("guide_conceptual_area") or "").strip()


def _node_id(node: dict[str, Any]) -> str:
    return str(node.get("id") or "").strip()


def _memory_type(node: dict[str, Any]) -> str:
    return str(node.get("memory_type") or node.get("node_kind") or "").strip().lower()


def _bucket_key(node: dict[str, Any]) -> str:
    bucket = node.get("bucket")
    if isinstance(bucket, dict) and str(bucket.get("key") or "").strip():
        return str(bucket["key"])
    position = _position(node)
    if not position:
        return "unknown"
    return str(position_to_bucket(position).get("key") or "unknown")


def _bucket_parts(bucket_key: str) -> tuple[int, int, int] | None:
    try:
        parts = [int(part) for part in str(bucket_key).split(":")]
    except ValueError:
        return None
    if len(parts) != 3:
        return None
    return parts[0], parts[1], parts[2]


def _is_active(node: dict[str, Any]) -> bool:
    return str(node.get("lifecycle_status") or "active").strip().lower() != "deleted"


def _text_blob(node: dict[str, Any]) -> str:
    pieces = [
        str(node.get("summary") or ""),
        str(node.get("raw_text") or ""),
        str(node.get("temporal_role") or ""),
        str(node.get("claim_status") or ""),
        str(node.get("derivation_role") or ""),
    ]
    return " ".join(pieces).lower()


def _has_future_or_hypothesis_cue(node: dict[str, Any]) -> bool:
    text = _text_blob(node)
    if str(node.get("temporal_role") or "").strip().lower() in {"future_intent", "future", "dream"}:
        return True
    if str(node.get("claim_status") or "").strip().lower() in {"hypothesis", "inferred", "possibility", "dream"}:
        return True
    return any(token in text for token in ("future", "futuro", "dream", "sogno", "ipotesi", "hypothesis", "possibilita", "possibility"))


def expected_brain_geometry_profile(node: dict[str, Any]) -> dict[str, Any]:
    """Return the expected semantic/radial zone for a node without mutating it."""
    memory_type = _memory_type(node)
    guide_area = _guide_area(node)
    guide_area_key = guide_area.strip().lower().replace("_", " ")
    node_kind = str(node.get("node_kind") or "").strip().lower()
    document_role = str(node.get("document_role") or "").strip().lower()
    temporal_role = str(node.get("temporal_role") or "").strip().lower()
    claim_status = str(node.get("claim_status") or "").strip().lower()
    source_trust = str(node.get("source_trust") or "").strip().lower()

    if source_trust == "system_metadata" or claim_status == "source_metadata":
        return {"zone": "system_metadata", "label": "System Metadata", "min_radius": 0.62, "max_radius": 1.0, "basis": "system_metadata_guard"}
    if (
        node.get("is_document_anchor")
        or document_role in {"anchor", "chunk"}
        or memory_type in {"document_anchor", "document_chunk", "source_anchor", "source_unit", "source_chunk", "raw_source"}
        or node_kind in {"document", "document_anchor", "media", "artifact"}
    ):
        return {"zone": "documents", "label": "Documents", "min_radius": 0.62, "max_radius": 1.0, "basis": "document_substrate"}
    if _has_future_or_hypothesis_cue(node):
        return {"zone": "future_hypotheses", "label": "Future / Hypotheses", "min_radius": 0.56, "max_radius": 0.94, "basis": "future_or_hypothesis"}
    if temporal_role == "stable_identity" or guide_area_key in {"identity", "personal identity"} or memory_type == "identity" or node_kind == "identity":
        return {"zone": "identity", "label": "Identity", "min_radius": 0.06, "max_radius": 0.44, "basis": "identity_core"}
    if guide_area_key in {"values", "values and operating principles"} or memory_type in {"value", "values"} or node_kind == "value":
        return {"zone": "values", "label": "Values", "min_radius": 0.06, "max_radius": 0.40, "basis": "values_core"}
    if guide_area_key in {"relationships", "relation", "relations"} or memory_type in {"relational", "relationship"}:
        return {"zone": "relationships", "label": "Relationships", "min_radius": 0.28, "max_radius": 0.70, "basis": "relationship_ring"}
    if guide_area_key in {"projects", "work", "work and projects", "operational"} or memory_type in {"project", "technical", "operational", "work"}:
        return {"zone": "projects", "label": "Projects", "min_radius": 0.32, "max_radius": 0.82, "basis": "project_work_ring"}
    if guide_area_key in {"history", "timeline", "timeline and history", "temporal inventory"} or memory_type in {"episodic", "timeline"} or temporal_role == "past_state":
        return {"zone": "history", "label": "History", "min_radius": 0.50, "max_radius": 0.90, "basis": "temporal_history_ring"}
    if guide_area_key in {"expression", "style", "generation conditioning"} or memory_type in {"identity_style", "style"}:
        return {"zone": "expression", "label": "Expression", "min_radius": 0.24, "max_radius": 0.70, "basis": "style_expression_ring"}
    if guide_area_key in {"documents", "document", "media signals", "source material", "source"} or memory_type == "document_summary":
        return {"zone": "documents", "label": "Documents", "min_radius": 0.62, "max_radius": 1.0, "basis": "document_or_media_memory"}
    if memory_type == "document_fact":
        return {"zone": "knowledge", "label": "Knowledge", "min_radius": 0.34, "max_radius": 0.86, "basis": "derived_document_fact"}
    return {"zone": "knowledge", "label": "Knowledge", "min_radius": 0.34, "max_radius": 0.86, "basis": "general_memory_ring"}


def _edge_targets(node: dict[str, Any]) -> set[str]:
    targets: set[str] = set()
    for collection in ("links", "highways"):
        for item in list(node.get(collection) or []):
            target = str((item or {}).get("target_node_id") or "").strip()
            if target:
                targets.add(target)
    return targets


def _graph_edge_pairs(graph: dict[str, Any]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for edge in list(graph.get("edges") or []):
        if not isinstance(edge, dict):
            continue
        left = str(edge.get("source") or edge.get("source_node_id") or "").strip()
        right = str(edge.get("target") or edge.get("target_node_id") or "").strip()
        if left and right:
            pairs.add((left, right))
            pairs.add((right, left))
    return pairs


def _directly_connected(left: dict[str, Any], right: dict[str, Any], graph_edges: set[tuple[str, str]]) -> bool:
    left_id = _node_id(left)
    right_id = _node_id(right)
    if not left_id or not right_id:
        return False
    return right_id in _edge_targets(left) or left_id in _edge_targets(right) or (left_id, right_id) in graph_edges


def _zone_related(left_zone: str, right_zone: str) -> bool:
    if left_zone == right_zone:
        return True
    pair = {left_zone, right_zone}
    return pair in (
        {"documents", "projects"},
        {"documents", "history"},
        {"identity", "projects"},
        {"identity", "relationships"},
        {"values", "identity"},
        {"expression", "identity"},
        {"future_hypotheses", "projects"},
    )


def _recommendation(severity: str, code: str, message: str, *, targets: list[str] | None = None, metric: Any = None) -> dict[str, Any]:
    return {
        "severity": severity,
        "code": code,
        "message": message,
        "targets": list(targets or [])[:12],
        "metric": metric,
    }


def _round(value: float) -> float:
    return round(float(value), 6)


def _mean(values: list[float]) -> float:
    return sum(values) / max(1, len(values)) if values else 0.0


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def _stable_unit_vector(seed: str) -> dict[str, float]:
    digest = hashlib.sha256(str(seed).encode("utf-8")).digest()
    a = int.from_bytes(digest[:8], "big") / float(2**64 - 1)
    b = int.from_bytes(digest[8:16], "big") / float(2**64 - 1)
    theta = 2.0 * math.pi * a
    z = (2.0 * b) - 1.0
    xy = math.sqrt(max(0.0, 1.0 - (z * z)))
    return {"x": xy * math.cos(theta), "y": xy * math.sin(theta), "z": z}


def _stable_json_hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:24]


def _unit_vector(position: dict[str, float], *, fallback_seed: str) -> dict[str, float]:
    radius = distance(position, _ORIGIN)
    if radius <= 1e-9:
        return _stable_unit_vector(fallback_seed)
    return {
        "x": float(position["x"]) / radius,
        "y": float(position["y"]) / radius,
        "z": float(position["z"]) / radius,
    }


def _normalize_vector(vector: dict[str, float], *, fallback_seed: str) -> dict[str, float]:
    radius = distance(vector, _ORIGIN)
    if radius <= 1e-9:
        return _stable_unit_vector(fallback_seed)
    return {
        "x": float(vector["x"]) / radius,
        "y": float(vector["y"]) / radius,
        "z": float(vector["z"]) / radius,
    }


def _scale_vector(vector: dict[str, float], radius: float) -> dict[str, float]:
    return {
        "x": _round(float(vector["x"]) * radius),
        "y": _round(float(vector["y"]) * radius),
        "z": _round(float(vector["z"]) * radius),
    }


def _target_radius_for_band(current_radius: float, min_radius: float, max_radius: float) -> float:
    band_width = max(0.01, max_radius - min_radius)
    inner = min_radius + (band_width * 0.28)
    outer = max_radius - (band_width * 0.22)
    if current_radius < min_radius:
        return _round(inner)
    if current_radius > max_radius:
        return _round(outer)
    return _round(max(min_radius + 0.01, min(max_radius - 0.01, current_radius)))


def _jittered_unit_vector(unit: dict[str, float], *, seed: str, strength: float) -> dict[str, float]:
    jitter = _stable_unit_vector(f"{seed}::matrix_jitter")
    dot = (unit["x"] * jitter["x"]) + (unit["y"] * jitter["y"]) + (unit["z"] * jitter["z"])
    tangent = {
        "x": jitter["x"] - (dot * unit["x"]),
        "y": jitter["y"] - (dot * unit["y"]),
        "z": jitter["z"] - (dot * unit["z"]),
    }
    tangent = _normalize_vector(tangent, fallback_seed=f"{seed}::matrix_tangent")
    mixed = {
        "x": unit["x"] + (tangent["x"] * strength),
        "y": unit["y"] + (tangent["y"] * strength),
        "z": unit["z"] + (tangent["z"] * strength),
    }
    return _normalize_vector(mixed, fallback_seed=f"{seed}::matrix_mixed")


def _spacing_collision_node_ids(nodes: list[dict[str, Any]], *, limit: int = 1600, threshold: float = 0.025) -> set[str]:
    positioned = [(node, _position(node)) for node in nodes if _position(node)]
    positioned = positioned[: max(1, int(limit))]
    collision_ids: set[str] = set()
    for index, (node, position) in enumerate(positioned):
        node_id = _node_id(node)
        if not node_id or not position:
            continue
        best_distance = None
        best_id = None
        for other_index, (other, other_position) in enumerate(positioned):
            if index == other_index or not other_position:
                continue
            gap = distance(position, other_position)
            if best_distance is None or gap < best_distance:
                best_distance = gap
                best_id = _node_id(other)
        if best_distance is not None and best_distance < threshold:
            collision_ids.add(node_id)
            if best_id:
                collision_ids.add(best_id)
    return collision_ids


def _radial_alignment(nodes: list[dict[str, Any]], recommendations: list[dict[str, Any]]) -> dict[str, Any]:
    by_zone: dict[str, list[dict[str, Any]]] = defaultdict(list)
    total = 0
    in_band_total = 0
    drift_records: list[dict[str, Any]] = []
    for node in nodes:
        radius = _radius(node)
        if radius is None:
            continue
        profile = expected_brain_geometry_profile(node)
        min_radius = float(profile["min_radius"])
        max_radius = float(profile["max_radius"])
        in_band = min_radius <= radius <= max_radius
        distance_from_band = 0.0 if in_band else min(abs(radius - min_radius), abs(radius - max_radius))
        total += 1
        in_band_total += 1 if in_band else 0
        row = {
            "node_id": _node_id(node),
            "zone": profile["zone"],
            "radius": _round(radius),
            "min_radius": min_radius,
            "max_radius": max_radius,
            "in_band": in_band,
            "distance_from_band": _round(distance_from_band),
            "guide_area": _guide_area(node),
            "memory_type": _memory_type(node),
        }
        by_zone[str(profile["zone"])].append(row)
        if not in_band:
            drift_records.append(row)
    zones: dict[str, Any] = {}
    for zone, rows in sorted(by_zone.items()):
        in_band = sum(1 for row in rows if row["in_band"])
        radii = [float(row["radius"]) for row in rows]
        score = in_band / max(1, len(rows))
        zones[zone] = {
            "count": len(rows),
            "in_band_count": in_band,
            "score": _round(score),
            "avg_radius": _round(_mean(radii)),
            "min_observed_radius": _round(min(radii)) if radii else 0.0,
            "max_observed_radius": _round(max(radii)) if radii else 0.0,
            "expected_band": {
                "min_radius": rows[0]["min_radius"] if rows else 0.0,
                "max_radius": rows[0]["max_radius"] if rows else 0.0,
            },
            "drift_examples": sorted(
                [row for row in rows if not row["in_band"]],
                key=lambda item: (-float(item["distance_from_band"]), str(item["node_id"])),
            )[:6],
        }
        if len(rows) >= 3 and score < 0.66:
            recommendations.append(
                _recommendation(
                    "high" if score < 0.45 else "medium",
                    f"radial_drift::{zone}",
                    f"{zone} nodes are not consistently landing in their expected radial band.",
                    targets=[str(row["node_id"]) for row in zones[zone]["drift_examples"]],
                    metric=_round(score),
                )
            )
    return {
        "score": _round(in_band_total / max(1, total)),
        "node_count": total,
        "in_band_count": in_band_total,
        "out_of_band_count": max(0, total - in_band_total),
        "zones": zones,
        "top_drift_nodes": sorted(drift_records, key=lambda item: (-float(item["distance_from_band"]), str(item["node_id"])))[:12],
    }


def _pair_nearest_distances(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any], float]]:
    pairs: list[tuple[dict[str, Any], dict[str, Any], float]] = []
    right_positions = [(node, _position(node)) for node in right]
    right_positions = [(node, position) for node, position in right_positions if position]
    for left_node in left:
        left_position = _position(left_node)
        if not left_position or not right_positions:
            continue
        best_node, best_distance = min(
            ((right_node, distance(left_position, right_position)) for right_node, right_position in right_positions if right_position),
            key=lambda item: (item[1], _node_id(item[0])),
        )
        pairs.append((left_node, best_node, best_distance))
    return pairs


def _zone_separation(zone_nodes: dict[str, list[dict[str, Any]]], recommendations: list[dict[str, Any]]) -> dict[str, Any]:
    identity = zone_nodes.get("identity", []) + zone_nodes.get("values", [])
    relationships = zone_nodes.get("relationships", [])
    future = zone_nodes.get("future_hypotheses", [])
    facts = [
        node
        for zone, nodes in zone_nodes.items()
        if zone not in {"future_hypotheses", "system_metadata"}
        for node in nodes
    ]
    relationship_pairs = _pair_nearest_distances(relationships, identity)
    relation_overlap = [pair for pair in relationship_pairs if pair[2] < 0.08]
    relationship_score = 1.0 - (len(relation_overlap) / max(1, len(relationship_pairs))) if relationship_pairs else None
    future_pairs = _pair_nearest_distances(future, facts)
    future_overlap = [pair for pair in future_pairs if pair[2] < 0.08 or _bucket_key(pair[0]) == _bucket_key(pair[1])]
    future_score = 1.0 - (len(future_overlap) / max(1, len(future_pairs))) if future_pairs else None
    scores = [value for value in (relationship_score, future_score) if value is not None]
    if relationship_pairs and relationship_score is not None and relationship_score < 0.55:
        recommendations.append(
            _recommendation(
                "medium",
                "zone_overlap::relationships_identity",
                "Relationship nodes are too close to generic identity/value nodes; relation memory may collapse into identity.",
                targets=[_node_id(pair[0]) for pair in relation_overlap[:8]],
                metric=_round(relationship_score),
            )
        )
    if future_pairs and future_score is not None and future_score < 0.55:
        recommendations.append(
            _recommendation(
                "medium",
                "zone_overlap::future_fact",
                "Future, dream or hypothesis nodes are not clearly separated from fact memory.",
                targets=[_node_id(pair[0]) for pair in future_overlap[:8]],
                metric=_round(future_score),
            )
        )
    return {
        "score": _round(_mean(scores)) if scores else 1.0,
        "relationships_vs_identity": {
            "evaluable": bool(relationship_pairs),
            "score": _round(relationship_score) if relationship_score is not None else None,
            "nearest_distance_median": _round(_median([pair[2] for pair in relationship_pairs])) if relationship_pairs else None,
            "overlap_count_under_0_08": len(relation_overlap),
        },
        "future_vs_facts": {
            "evaluable": bool(future_pairs),
            "score": _round(future_score) if future_score is not None else None,
            "nearest_distance_median": _round(_median([pair[2] for pair in future_pairs])) if future_pairs else None,
            "overlap_or_same_bucket_count": len(future_overlap),
        },
    }


def _document_project_coupling(graph: dict[str, Any], zone_nodes: dict[str, list[dict[str, Any]]], recommendations: list[dict[str, Any]]) -> dict[str, Any]:
    graph_edges = _graph_edge_pairs(graph)
    documents = zone_nodes.get("documents", [])
    projects = zone_nodes.get("projects", [])
    rows: list[dict[str, Any]] = []
    good_count = 0
    for document in documents:
        document_position = _position(document)
        project_pairs = [(project, _position(project)) for project in projects]
        project_pairs = [(project, position) for project, position in project_pairs if position]
        nearest_project = None
        nearest_distance = None
        if document_position and project_pairs:
            nearest_project, nearest_distance = min(
                ((project, distance(document_position, position)) for project, position in project_pairs),
                key=lambda item: (item[1], _node_id(item[0])),
            )
        linked = bool(nearest_project and _directly_connected(document, nearest_project, graph_edges))
        document_bucket = _bucket_parts(_bucket_key(document))
        project_bucket = _bucket_parts(_bucket_key(nearest_project)) if nearest_project else None
        bucket_adjacent = bool(
            document_bucket
            and project_bucket
            and all(abs(document_bucket[index] - project_bucket[index]) <= 1 for index in range(3))
        )
        good = bool(linked or (nearest_distance is not None and nearest_distance <= 0.46))
        good_count += 1 if good else 0
        rows.append(
            {
                "document_id": _node_id(document),
                "nearest_project_id": _node_id(nearest_project or {}) if nearest_project else None,
                "nearest_distance": _round(nearest_distance) if nearest_distance is not None else None,
                "directly_connected": linked,
                "bucket_adjacent_x": bucket_adjacent,
                "passes": good,
            }
        )
    score = good_count / max(1, len(rows)) if rows else 1.0
    if documents and projects and score < 0.60:
        recommendations.append(
            _recommendation(
                "high",
                "document_project_coupling_low",
                "Documents are not consistently close or linked to their project/work regions.",
                targets=[str(row["document_id"]) for row in rows if not row["passes"]][:8],
                metric=_round(score),
            )
        )
    return {
        "evaluable": bool(documents and projects),
        "score": _round(score),
        "document_count": len(documents),
        "project_count": len(projects),
        "coupled_document_count": good_count,
        "orphan_document_count": max(0, len(rows) - good_count),
        "nearest_distance_median": _round(_median([float(row["nearest_distance"]) for row in rows if row["nearest_distance"] is not None])) if rows else None,
        "examples": sorted(rows, key=lambda row: (bool(row["passes"]), -(float(row["nearest_distance"] or 0.0)), str(row["document_id"])))[:10],
    }


def _collect_highways(nodes: list[dict[str, Any]], lookup: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    highways: list[dict[str, Any]] = []
    for source in nodes:
        source_id = _node_id(source)
        if not source_id:
            continue
        for highway in list(source.get("highways") or []):
            if not isinstance(highway, dict):
                continue
            target_id = str(highway.get("target_node_id") or "").strip()
            if not target_id:
                continue
            highways.append({"source": source, "source_id": source_id, "target_id": target_id, "target": lookup.get(target_id), "highway": highway})
    return highways


def _highway_quality(nodes: list[dict[str, Any]], lookup: dict[str, dict[str, Any]], recommendations: list[dict[str, Any]]) -> dict[str, Any]:
    highways = _collect_highways(nodes, lookup)
    valid = [item for item in highways if isinstance(item.get("target"), dict)]
    stale = [item for item in highways if not isinstance(item.get("target"), dict)]
    reciprocal_count = 0
    related_count = 0
    strengths: list[float] = []
    for item in valid:
        source = dict(item["source"])
        target = dict(item["target"])
        source_zone = str(expected_brain_geometry_profile(source).get("zone") or "")
        target_zone = str(expected_brain_geometry_profile(target).get("zone") or "")
        if _zone_related(source_zone, target_zone):
            related_count += 1
        strengths.append(_safe_float(dict(item["highway"]).get("strength"), 0.0))
        if item["source_id"] in _edge_targets(target):
            reciprocal_count += 1
    related_ratio = related_count / max(1, len(valid))
    reciprocal_ratio = reciprocal_count / max(1, len(valid))
    avg_strength = _mean(strengths)
    stale_ratio = len(stale) / max(1, len(highways))
    if not valid and len(nodes) >= 4:
        recommendations.append(
            _recommendation(
                "high",
                "highways_missing",
                "No valid highways exist; sparse-brain routing cannot exploit fast semantic corridors.",
                metric=0.0,
            )
        )
    if stale:
        recommendations.append(
            _recommendation(
                "high",
                "highways_stale_targets",
                "Some highways point to missing nodes and should be pruned before route calibration.",
                targets=[f"{item['source_id']}->{item['target_id']}" for item in stale[:10]],
                metric=_round(stale_ratio),
            )
        )
    if valid and related_ratio < 0.45:
        recommendations.append(
            _recommendation(
                "medium",
                "highways_low_semantic_relatedness",
                "Highways exist but too few connect semantically useful regions.",
                metric=_round(related_ratio),
            )
        )
    score = (
        (0.25 if valid else 0.0)
        + (0.25 * _clamp01(related_ratio))
        + (0.18 * _clamp01(reciprocal_ratio))
        + (0.22 * _clamp01(avg_strength))
        + (0.10 * _clamp01(1.0 - stale_ratio))
    )
    return {
        "evaluable": len(nodes) >= 4,
        "score": _round(score),
        "highway_count": len(highways),
        "valid_highway_count": len(valid),
        "stale_target_count": len(stale),
        "avg_strength": _round(avg_strength),
        "same_or_related_zone_ratio": _round(related_ratio),
        "reciprocal_ratio": _round(reciprocal_ratio),
        "stale_target_ratio": _round(stale_ratio),
        "stale_examples": [{"source_id": item["source_id"], "target_id": item["target_id"]} for item in stale[:10]],
    }


def _point_segment_distance(point: dict[str, float], start: dict[str, float], end: dict[str, float]) -> tuple[float, float]:
    vx = float(end["x"]) - float(start["x"])
    vy = float(end["y"]) - float(start["y"])
    vz = float(end["z"]) - float(start["z"])
    wx = float(point["x"]) - float(start["x"])
    wy = float(point["y"]) - float(start["y"])
    wz = float(point["z"]) - float(start["z"])
    denom = vx * vx + vy * vy + vz * vz
    if denom <= 1e-12:
        return distance(point, start), 0.0
    t = max(0.0, min(1.0, (wx * vx + wy * vy + wz * vz) / denom))
    projection = {"x": start["x"] + t * vx, "y": start["y"] + t * vy, "z": start["z"] + t * vz}
    return distance(point, projection), t


def _path_bridge_potential(nodes: list[dict[str, Any]], lookup: dict[str, dict[str, Any]], recommendations: list[dict[str, Any]]) -> dict[str, Any]:
    valid_highways = [item for item in _collect_highways(nodes, lookup) if isinstance(item.get("target"), dict)]
    corridor_radius = 0.14
    sample = valid_highways[:160]
    node_positions = [(node, _position(node)) for node in nodes]
    node_positions = [(node, position) for node, position in node_positions if position]
    path_rows: list[dict[str, Any]] = []
    bridge_counts: list[float] = []
    meaningful_counts: list[float] = []
    for item in sample:
        source = dict(item["source"])
        target = dict(item["target"])
        source_position = _position(source)
        target_position = _position(target)
        if not source_position or not target_position:
            continue
        source_zone = str(expected_brain_geometry_profile(source).get("zone") or "")
        target_zone = str(expected_brain_geometry_profile(target).get("zone") or "")
        bridges: list[dict[str, Any]] = []
        for candidate, candidate_position in node_positions:
            candidate_id = _node_id(candidate)
            if candidate_id in {str(item["source_id"]), str(item["target_id"])}:
                continue
            gap, progress = _point_segment_distance(candidate_position, source_position, target_position)
            if 0.05 <= progress <= 0.95 and gap <= corridor_radius:
                candidate_zone = str(expected_brain_geometry_profile(candidate).get("zone") or "")
                meaningful = candidate_zone not in {source_zone, target_zone} or _zone_related(candidate_zone, source_zone) or _zone_related(candidate_zone, target_zone)
                bridges.append(
                    {
                        "node_id": candidate_id,
                        "zone": candidate_zone,
                        "distance_to_path": _round(gap),
                        "progress": _round(progress),
                        "meaningful_bridge": bool(meaningful),
                    }
                )
        bridges.sort(key=lambda row: (float(row["distance_to_path"]), str(row["node_id"])))
        meaningful_count = sum(1 for bridge in bridges if bridge["meaningful_bridge"])
        bridge_counts.append(float(len(bridges)))
        meaningful_counts.append(float(meaningful_count))
        path_rows.append(
            {
                "source_id": str(item["source_id"]),
                "target_id": str(item["target_id"]),
                "source_zone": source_zone,
                "target_zone": target_zone,
                "bridge_count": len(bridges),
                "meaningful_bridge_count": meaningful_count,
                "bridge_examples": bridges[:6],
            }
        )
    bridge_yield_avg = _mean([min(1.0, count / 3.0) for count in bridge_counts])
    meaningful_yield_avg = _mean([min(1.0, count / 2.0) for count in meaningful_counts])
    dead_count = sum(1 for count in bridge_counts if count <= 0)
    score = 0.45 * bridge_yield_avg + 0.55 * meaningful_yield_avg
    if sample and score < 0.25:
        recommendations.append(
            _recommendation(
                "medium",
                "path_corridor_low_bridge_yield",
                "Highway corridors rarely expose intermediate bridge nodes; path traversal may behave like point-to-point jumping.",
                targets=[f"{row['source_id']}->{row['target_id']}" for row in path_rows if int(row["bridge_count"]) == 0][:8],
                metric=_round(score),
            )
        )
    return {
        "evaluable": bool(sample),
        "score": _round(score if sample else 1.0),
        "sampled_path_count": len(path_rows),
        "corridor_radius": corridor_radius,
        "bridge_yield_avg": _round(bridge_yield_avg),
        "meaningful_bridge_yield_avg": _round(meaningful_yield_avg),
        "dead_path_count": dead_count,
        "examples": sorted(path_rows, key=lambda row: (int(row["bridge_count"]), str(row["source_id"]), str(row["target_id"])))[:10],
    }


def _landing_density(nodes: list[dict[str, Any]], recommendations: list[dict[str, Any]]) -> dict[str, Any]:
    bucket_counts = Counter(_bucket_key(node) for node in nodes)
    node_count = len(nodes)
    threshold = max(8, int(math.sqrt(max(1, node_count)) * 0.55))
    overfull = [(bucket, count) for bucket, count in bucket_counts.items() if count > threshold]
    crowded_nodes = sum(count for _, count in overfull)
    score = 1.0 - min(0.85, crowded_nodes / max(1, node_count))
    if overfull:
        recommendations.append(
            _recommendation(
                "medium",
                "landing_density_crowded_buckets",
                "Too many nodes share the same spatial bucket; landing density may hide useful local neighborhoods.",
                targets=[bucket for bucket, _ in sorted(overfull, key=lambda item: (-item[1], item[0]))[:8]],
                metric={"threshold": threshold, "crowded_node_count": crowded_nodes},
            )
        )
    return {
        "score": _round(score),
        "bucket_count": len(bucket_counts),
        "node_count": node_count,
        "overfull_threshold": threshold,
        "overfull_bucket_count": len(overfull),
        "crowded_node_count": crowded_nodes,
        "max_bucket_occupancy": max(bucket_counts.values()) if bucket_counts else 0,
        "median_bucket_occupancy": _median([float(count) for count in bucket_counts.values()]),
        "top_buckets": [
            {"bucket_key": bucket, "count": count}
            for bucket, count in sorted(bucket_counts.items(), key=lambda item: (-item[1], item[0]))[:12]
        ],
    }


def _spacing(nodes: list[dict[str, Any]], recommendations: list[dict[str, Any]]) -> dict[str, Any]:
    positioned = [(node, _position(node)) for node in nodes if _position(node)]
    positioned = positioned[:1600]
    nearest: list[float] = []
    collision_pairs: list[dict[str, Any]] = []
    for index, (node, position) in enumerate(positioned):
        best_distance = None
        best_id = None
        for other_index, (other, other_position) in enumerate(positioned):
            if index == other_index:
                continue
            gap = distance(position, other_position)
            if best_distance is None or gap < best_distance:
                best_distance = gap
                best_id = _node_id(other)
        if best_distance is not None:
            nearest.append(best_distance)
            if best_distance < 0.025:
                collision_pairs.append({"node_id": _node_id(node), "nearest_node_id": best_id, "distance": _round(best_distance)})
    collision_ratio = len(collision_pairs) / max(1, len(positioned))
    score = 1.0 - min(0.95, collision_ratio * 2.2)
    if collision_ratio > 0.12:
        recommendations.append(
            _recommendation(
                "medium",
                "spacing_collision_risk",
                "Nearest-neighbor collisions are high; map landings can look crowded and route substrate becomes ambiguous.",
                targets=[str(item["node_id"]) for item in collision_pairs[:10]],
                metric=_round(collision_ratio),
            )
        )
    return {
        "score": _round(score),
        "sampled_node_count": len(positioned),
        "collision_ratio_under_0_025": _round(collision_ratio),
        "collision_count_under_0_025": len(collision_pairs),
        "nearest_neighbor": {
            "count": len(nearest),
            "min": _round(min(nearest)) if nearest else 0.0,
            "median": _round(_median(nearest)),
            "avg": _round(_mean(nearest)),
        },
        "collision_examples": collision_pairs[:10],
    }


def _scorecard(parts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    weights = {
        "radial_alignment": 0.26,
        "zone_separation": 0.10,
        "document_project_coupling": 0.16,
        "highway_quality": 0.16,
        "path_bridge_potential": 0.14,
        "landing_density": 0.08,
        "spacing": 0.10,
    }
    numerator = 0.0
    denominator = 0.0
    for key, weight in weights.items():
        section = dict(parts.get(key) or {})
        if section.get("evaluable") is False:
            continue
        numerator += weight * _clamp01(_safe_float(section.get("score"), 1.0))
        denominator += weight
    overall = numerator / max(denominator, 1e-9)
    thresholds = {
        "overall_score_min": 0.62,
        "radial_alignment_min": 0.70,
        "landing_density_min": 0.55,
        "spacing_min": 0.65,
        "document_project_coupling_min_when_evaluable": 0.50,
        "highway_quality_min_when_evaluable": 0.40,
        "path_bridge_potential_min_when_evaluable": 0.25,
    }
    checks = {
        "overall_score": overall >= thresholds["overall_score_min"],
        "radial_alignment": _safe_float(parts["radial_alignment"].get("score")) >= thresholds["radial_alignment_min"],
        "landing_density": _safe_float(parts["landing_density"].get("score")) >= thresholds["landing_density_min"],
        "spacing": _safe_float(parts["spacing"].get("score")) >= thresholds["spacing_min"],
    }
    for key, threshold_key in (
        ("document_project_coupling", "document_project_coupling_min_when_evaluable"),
        ("highway_quality", "highway_quality_min_when_evaluable"),
        ("path_bridge_potential", "path_bridge_potential_min_when_evaluable"),
    ):
        section = dict(parts[key])
        if section.get("evaluable") is False:
            checks[key] = True
        else:
            checks[key] = _safe_float(section.get("score")) >= thresholds[threshold_key]
    return {
        "overall_score": _round(overall),
        "thresholds": thresholds,
        "checks": checks,
        "all_pass": all(checks.values()),
    }


def _calibration_proposals(recommendations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    proposal_by_code: dict[str, dict[str, Any]] = {}
    for recommendation in recommendations:
        code = str(recommendation.get("code") or "")
        family = code.split("::", 1)[0]
        if family.startswith("radial_drift"):
            proposal_code = "preview_radial_band_rebalance"
        elif family.startswith("document_project"):
            proposal_code = "preview_document_project_coupling"
        elif family.startswith("highways"):
            proposal_code = "preview_highway_backfill_or_prune"
        elif family.startswith("path_corridor"):
            proposal_code = "preview_corridor_read_policy"
        elif family.startswith("landing_density") or family.startswith("spacing"):
            proposal_code = "preview_local_density_rebalance"
        else:
            proposal_code = "preview_geometry_review"
        proposal = proposal_by_code.setdefault(
            proposal_code,
            {
                "proposal_code": proposal_code,
                "review_required": True,
                "apply_in_this_slice": False,
                "reason_codes": [],
                "evidence_refs": [],
                "target_count": 0,
            },
        )
        proposal["reason_codes"].append(code)
        proposal["evidence_refs"].append(
            {
                "source": "brain_geometry_calibration_recommendation",
                "id": code or proposal_code,
                "severity": recommendation.get("severity"),
                "metric": recommendation.get("metric"),
            }
        )
        proposal["target_count"] += len(list(recommendation.get("targets") or []))
    return list(proposal_by_code.values())


def build_matrix_calibration_position_plan(
    graph: dict[str, Any],
    *,
    max_nodes: int = 4000,
    max_updates: int = 800,
) -> dict[str, Any]:
    """Build a deterministic, non-mutating matrix repair plan with concrete position deltas."""
    nodes = [
        dict(node)
        for node in list(graph.get("nodes") or [])
        if isinstance(node, dict) and _is_active(dict(node)) and _position(dict(node))
    ][: max(1, int(max_nodes))]
    node_count = len(nodes)
    bucket_counts = Counter(_bucket_key(node) for node in nodes)
    density_threshold = max(8, int(math.sqrt(max(1, node_count)) * 0.55))
    crowded_buckets = {bucket for bucket, count in bucket_counts.items() if count > density_threshold}
    spacing_collision_ids = _spacing_collision_node_ids(nodes)
    candidates: list[dict[str, Any]] = []
    for node in nodes:
        node_id = _node_id(node)
        if not node_id:
            continue
        position = _position(node)
        if not position:
            continue
        radius = distance(position, _ORIGIN)
        profile = expected_brain_geometry_profile(node)
        min_radius = float(profile["min_radius"])
        max_radius = float(profile["max_radius"])
        outside_band = radius < min_radius or radius > max_radius
        crowded = _bucket_key(node) in crowded_buckets
        spacing_collision = node_id in spacing_collision_ids
        if not outside_band and not crowded and not spacing_collision:
            continue
        distance_from_band = 0.0 if not outside_band else min(abs(radius - min_radius), abs(radius - max_radius))
        candidates.append(
            {
                "node": node,
                "node_id": node_id,
                "position": position,
                "radius": radius,
                "profile": profile,
                "outside_band": outside_band,
                "crowded": crowded,
                "spacing_collision": spacing_collision,
                "distance_from_band": distance_from_band,
                "bucket_count": int(bucket_counts.get(_bucket_key(node), 0)),
            }
        )
    candidates.sort(
        key=lambda item: (
            not bool(item["outside_band"]),
            -float(item["distance_from_band"]),
            -int(item["bucket_count"]),
            str(item["node_id"]),
        )
    )
    updates: list[dict[str, Any]] = []
    by_zone: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    for item in candidates[: max(1, int(max_updates))]:
        node_id = str(item["node_id"])
        position = dict(item["position"])
        profile = dict(item["profile"])
        current_radius = float(item["radius"])
        min_radius = float(profile["min_radius"])
        max_radius = float(profile["max_radius"])
        target_radius = _target_radius_for_band(current_radius, min_radius, max_radius)
        if bool(item["spacing_collision"]):
            unit = _stable_unit_vector(f"{profile.get('zone') or 'knowledge'}::{node_id}::spacing")
        else:
            unit = _unit_vector(position, fallback_seed=node_id)
        reasons: list[str] = []
        if bool(item["outside_band"]):
            reasons.append(f"radial_band_rebalance::{profile.get('zone') or 'unknown'}")
        if bool(item["crowded"]):
            reasons.append("local_density_jitter")
        if bool(item["spacing_collision"]):
            reasons.append("spacing_collision_rebalance")
        jitter_strength = 0.0
        if bool(item["spacing_collision"]):
            jitter_strength += 0.34
        if bool(item["crowded"]):
            jitter_strength += 0.22
        if bool(item["outside_band"]):
            jitter_strength += 0.06
        if jitter_strength > 0.0 and not bool(item["spacing_collision"]):
            unit = _jittered_unit_vector(unit, seed=node_id, strength=min(0.48, jitter_strength))
        next_position = _scale_vector(unit, target_radius)
        from_bucket = position_to_bucket(position)
        to_bucket = position_to_bucket(next_position)
        zone = str(profile.get("zone") or "knowledge")
        by_zone[zone] += 1
        for reason in reasons:
            reason_counts[reason] += 1
        updates.append(
            {
                "node_id": node_id,
                "zone": zone,
                "proposal_code": "preview_radial_band_rebalance" if bool(item["outside_band"]) else "preview_local_density_rebalance",
                "reason_codes": reasons,
                "from_position": {key: _round(float(position[key])) for key in ("x", "y", "z")},
                "to_position": next_position,
                "from_radius": _round(current_radius),
                "to_radius": _round(distance(next_position, _ORIGIN)),
                "expected_band": {"min_radius": _round(min_radius), "max_radius": _round(max_radius)},
                "from_bucket": from_bucket,
                "to_bucket": to_bucket,
                "bucket_changed": str(from_bucket.get("key")) != str(to_bucket.get("key")),
                "preview_only": True,
            }
        )
    return {
        "schema_version": "agvm.matrix_calibration_position_plan.v1",
        "generated_at": utc_timestamp(),
        "preview_only": True,
        "mutates_graph": False,
        "hidden_mutation_allowed": False,
        "max_nodes_considered": max(1, int(max_nodes)),
        "max_updates": max(1, int(max_updates)),
        "candidate_count": len(candidates),
        "update_count": len(updates),
        "density_threshold": density_threshold,
        "crowded_bucket_count": len(crowded_buckets),
        "spacing_collision_candidate_count": len(spacing_collision_ids),
        "zone_update_counts": dict(sorted(by_zone.items())),
        "reason_counts": dict(sorted(reason_counts.items())),
        "updates": updates,
        "plan_signature": _stable_json_hash(
            {
                "schema_version": "agvm.matrix_calibration_position_plan.v1",
                "max_nodes_considered": max(1, int(max_nodes)),
                "max_updates": max(1, int(max_updates)),
                "updates": [
                    {
                        "node_id": update.get("node_id"),
                        "to_position": update.get("to_position"),
                        "reason_codes": update.get("reason_codes"),
                    }
                    for update in updates
                ],
            }
        ),
        "operator_summary": (
            "Preview-only coordinate repair plan. It proposes radial-band rebalance and local-density jitter "
            "without mutating the graph; apply must require explicit approval, before/after metrics and rollback."
        ),
    }


def apply_matrix_calibration_position_plan_to_graph(graph: dict[str, Any], position_plan: dict[str, Any]) -> dict[str, Any]:
    update_by_id = {
        str(item.get("node_id") or ""): dict(item)
        for item in list(position_plan.get("updates") or [])
        if isinstance(item, dict) and str(item.get("node_id") or "").strip()
    }
    if not update_by_id:
        return dict(graph or {})
    projected = dict(graph or {})
    projected_nodes: list[dict[str, Any]] = []
    for node in list(projected.get("nodes") or []):
        node_payload = dict(node) if isinstance(node, dict) else {}
        update = update_by_id.get(str(node_payload.get("id") or ""))
        if update:
            next_position = dict(update.get("to_position") or {})
            if {"x", "y", "z"}.issubset(next_position):
                node_payload["final_position"] = {
                    "x": float(next_position["x"]),
                    "y": float(next_position["y"]),
                    "z": float(next_position["z"]),
                }
                node_payload["bucket"] = position_to_bucket(node_payload["final_position"])
                node_payload["matrix_calibration_preview"] = {
                    "proposal_code": update.get("proposal_code"),
                    "reason_codes": list(update.get("reason_codes") or []),
                    "preview_only": True,
                }
        projected_nodes.append(node_payload)
    projected["nodes"] = projected_nodes
    projected["meta"] = {
        **dict(projected.get("meta") or {}),
        "matrix_calibration_projection": {
            "schema_version": "agvm.matrix_calibration_projection.v1",
            "preview_only": True,
            "applied_update_count": len(update_by_id),
            "generated_at": utc_timestamp(),
        },
    }
    return projected


def build_brain_geometry_calibration_report(graph: dict[str, Any], *, max_nodes: int = 4000) -> dict[str, Any]:
    nodes = [
        dict(node)
        for node in list(graph.get("nodes") or [])
        if isinstance(node, dict) and _is_active(dict(node)) and _position(dict(node))
    ][: max(1, int(max_nodes))]
    lookup = {_node_id(node): node for node in nodes if _node_id(node)}
    recommendations: list[dict[str, Any]] = []
    zone_nodes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in nodes:
        zone_nodes[str(expected_brain_geometry_profile(node).get("zone") or "knowledge")].append(node)

    radial = _radial_alignment(nodes, recommendations)
    zone_separation = _zone_separation(zone_nodes, recommendations)
    document_project = _document_project_coupling(graph, zone_nodes, recommendations)
    highway_quality = _highway_quality(nodes, lookup, recommendations)
    path_bridge = _path_bridge_potential(nodes, lookup, recommendations)
    landing_density = _landing_density(nodes, recommendations)
    spacing = _spacing(nodes, recommendations)

    parts = {
        "radial_alignment": radial,
        "zone_separation": zone_separation,
        "document_project_coupling": document_project,
        "highway_quality": highway_quality,
        "path_bridge_potential": path_bridge,
        "landing_density": landing_density,
        "spacing": spacing,
    }
    scorecard = _scorecard(parts)
    recommendations.sort(
        key=lambda item: (
            {"high": 0, "medium": 1, "low": 2}.get(str(item.get("severity") or "low"), 3),
            str(item.get("code") or ""),
        )
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_timestamp(),
        "node_count": len(nodes),
        "zone_counts": {zone: len(zone_nodes[zone]) for zone in sorted(zone_nodes)},
        "overall_score": scorecard["overall_score"],
        "radial_alignment": radial,
        "zone_separation": zone_separation,
        "document_project_coupling": document_project,
        "highway_quality": highway_quality,
        "path_bridge_potential": path_bridge,
        "landing_density": landing_density,
        "spacing": spacing,
        "benchmarks": scorecard,
        "recommendations": recommendations[:30],
        "calibration_proposals": _calibration_proposals(recommendations),
        "matrix_change_policy": {
            "mutates_graph": False,
            "requires_before_after_measurement": True,
            "requires_review": True,
            "next_slice_can_apply": "PR-12H Sleep/Evolve/Grow Cognitive Maintenance",
        },
    }
