from __future__ import annotations

import math
from typing import Any


ACTIVE_MATRIX_PLACEMENT_SCHEMA_VERSION = "agvm.active_matrix_placement.v1"
ACTIVE_SPATIAL_REVISION_CONTEXT_SCHEMA_VERSION = "agvm.active_spatial_revision_context.v1"


def _dict_value(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _list_value(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _text(value: Any) -> str:
    return str(value or "").strip()


def _norm_label(value: Any) -> str:
    return " ".join(_text(value).lower().replace("_", " ").split())


def _guide_label_aliases(value: Any) -> set[str]:
    normalized = _norm_label(value)
    aliases = {normalized} if normalized else set()
    if normalized in {"documents", "document", "media signals", "source material", "source materials"}:
        aliases.update({"documents", "document", "media signals", "source material", "source materials"})
    if normalized in {"identity", "self", "self core", "identity style"}:
        aliases.update({"identity", "self", "self core", "identity style"})
    if normalized in {"projects", "project", "projectual"}:
        aliases.update({"projects", "project", "projectual"})
    if normalized in {"values", "value", "principles"}:
        aliases.update({"values", "value", "principles"})
    return aliases


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _position(value: Any) -> dict[str, float] | None:
    source = _dict_value(value)
    if not {"x", "y", "z"}.issubset(source):
        return None
    try:
        return {
            "x": float(source["x"]),
            "y": float(source["y"]),
            "z": float(source["z"]),
        }
    except (TypeError, ValueError):
        return None


def _delta(value: Any) -> dict[str, float]:
    source = _dict_value(value)
    return {axis: _float(source.get(axis), 0.0) for axis in ("x", "y", "z")}


def _distance(a: dict[str, float], b: dict[str, float]) -> float:
    return math.sqrt(
        (float(a["x"]) - float(b["x"])) ** 2
        + (float(a["y"]) - float(b["y"])) ** 2
        + (float(a["z"]) - float(b["z"])) ** 2
    )


def _radius(position: dict[str, float]) -> float:
    return math.sqrt(float(position["x"]) ** 2 + float(position["y"]) ** 2 + float(position["z"]) ** 2)


def _clamp_vector(delta: dict[str, float], max_norm: float) -> dict[str, float]:
    norm = _radius(delta)
    if norm <= max_norm or norm <= 1e-12:
        return dict(delta)
    scale = max_norm / norm
    return {axis: float(delta[axis]) * scale for axis in ("x", "y", "z")}


def _add_delta(position: dict[str, float], delta: dict[str, float]) -> dict[str, float]:
    return {axis: float(position[axis]) + float(delta[axis]) for axis in ("x", "y", "z")}


def _clamp_position_radius(position: dict[str, float], *, max_radius: float = 0.96) -> dict[str, float]:
    norm = _radius(position)
    if norm <= max_radius or norm <= 1e-12:
        return dict(position)
    scale = max_radius / norm
    return {axis: float(position[axis]) * scale for axis in ("x", "y", "z")}


def _blend_toward(position: dict[str, float], target: dict[str, float], weight: float) -> dict[str, float]:
    safe_weight = max(0.0, min(1.0, float(weight)))
    return {
        axis: (1.0 - safe_weight) * float(position[axis]) + safe_weight * float(target[axis])
        for axis in ("x", "y", "z")
    }


def _matching_guide_delta(axis_transform: dict[str, Any], guide_area: str) -> dict[str, float] | None:
    aliases = _guide_label_aliases(guide_area)
    if not aliases:
        return None
    for item in _list_value(axis_transform.get("by_guide_area")):
        if not isinstance(item, dict):
            continue
        if _guide_label_aliases(item.get("guide_area")) & aliases:
            return _delta(item.get("average_delta"))
    return None


def _quality_delta(matrix_revision: dict[str, Any]) -> dict[str, Any]:
    before = _dict_value(matrix_revision.get("quality_before"))
    after = _dict_value(matrix_revision.get("quality_after"))
    output: dict[str, Any] = {}
    for key in ("score", "landing_density_score", "spacing_score", "radial_alignment_score"):
        before_value = before.get(key)
        after_value = after.get(key)
        if before_value is None or after_value is None:
            continue
        output[key] = {
            "before": _float(before_value),
            "after": _float(after_value),
            "delta": round(_float(after_value) - _float(before_value), 6),
        }
    return output


def build_active_spatial_revision_context(
    matrix_revision: dict[str, Any] | None,
    topology_revision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize active matrix/topology revisions for ingest placement.

    This function is intentionally pure: local SQLite, cloud SQL and tests can
    all build the same context from the same revision payloads.
    """

    matrix = _dict_value(matrix_revision)
    topology = _dict_value(topology_revision)
    matrix_revision_id = _text(matrix.get("matrix_revision_id"))
    topology_revision_id = _text(topology.get("topology_revision_id"))
    if not matrix_revision_id and not topology_revision_id:
        return {}

    topology_matrix_id = _text(topology.get("matrix_revision_id"))
    topology_usable = bool(topology_revision_id) and (
        not matrix_revision_id or not topology_matrix_id or topology_matrix_id == matrix_revision_id
    )
    if not topology_usable:
        topology = {}
        topology_revision_id = ""

    return {
        "schema_version": ACTIVE_SPATIAL_REVISION_CONTEXT_SCHEMA_VERSION,
        "matrix_revision_id": matrix_revision_id or None,
        "topology_revision_id": topology_revision_id or None,
        "brain_id": _text(matrix.get("brain_id") or topology.get("brain_id")) or None,
        "base_projection_version": _text(matrix.get("base_projection_version")) or None,
        "plan_signature": _text(_dict_value(matrix.get("rollback_payload")).get("plan_signature")) or None,
        "semantic_axis_transform": _dict_value(matrix.get("semantic_axis_transform")),
        "radial_band_transform": _dict_value(matrix.get("radial_band_transform")),
        "guide_area_transform": _dict_value(matrix.get("guide_area_transform")),
        "attraction_priors": [dict(item) for item in _list_value(topology.get("attraction_priors")) if isinstance(item, dict)],
        "repulsion_priors": [dict(item) for item in _list_value(topology.get("repulsion_priors")) if isinstance(item, dict)],
        "rotation_hints": [dict(item) for item in _list_value(topology.get("rotation_hints")) if isinstance(item, dict)],
        "density_constraints": _dict_value(topology.get("density_constraints")),
        "quality_delta": _quality_delta(matrix),
        "topology_ignored_reason": None if topology_usable else "topology_matrix_revision_mismatch",
    }


def _projection_priors_for_guide(
    priors: list[dict[str, Any]],
    *,
    guide_area: str,
    limit: int = 8,
) -> list[dict[str, Any]]:
    aliases = _guide_label_aliases(guide_area)
    if not aliases:
        return []
    matched = []
    for prior in priors:
        if not (_guide_label_aliases(prior.get("guide_area")) & aliases):
            continue
        target = _position(prior.get("target_position"))
        if not target:
            continue
        matched.append({**prior, "target_position": target})
    return matched[:limit]


def _apply_radial_transform(
    position: dict[str, float],
    radial_transform: dict[str, Any],
) -> tuple[dict[str, float], dict[str, Any] | None]:
    raw_delta = _float(radial_transform.get("average_radius_delta"), 0.0)
    if abs(raw_delta) <= 1e-9:
        return position, None
    current_radius = _radius(position)
    if current_radius <= 1e-9:
        return position, None
    delta = max(-0.18, min(0.18, raw_delta * 0.45))
    target_radius = max(0.05, min(0.96, current_radius + delta))
    scale = target_radius / current_radius
    adjusted = {axis: float(position[axis]) * scale for axis in ("x", "y", "z")}
    return adjusted, {
        "kind": "radial_band_transform",
        "average_radius_delta": round(raw_delta, 6),
        "applied_radius_delta": round(target_radius - current_radius, 6),
        "weight": 0.45,
    }


def apply_active_spatial_revision_to_seed(
    seed: dict[str, Any],
    active_spatial_revision_context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Project a new ingest seed through the active learned matrix/topology field."""

    context = _dict_value(active_spatial_revision_context)
    matrix_revision_id = _text(context.get("matrix_revision_id"))
    topology_revision_id = _text(context.get("topology_revision_id"))
    if not matrix_revision_id and not topology_revision_id:
        return seed
    existing_matrix_revision_id = _text(seed.get("matrix_revision_id"))
    existing_topology_revision_id = _text(seed.get("topology_revision_id"))
    if existing_matrix_revision_id or existing_topology_revision_id or _dict_value(seed.get("active_matrix_projection")):
        return seed

    before = _position(seed.get("base_position"))
    if not before:
        return seed

    provenance = _dict_value(seed.get("provenance"))
    guide_area = _text(provenance.get("guide_conceptual_area"))
    position = dict(before)
    adjustments: list[dict[str, Any]] = []

    axis_transform = _dict_value(context.get("semantic_axis_transform"))
    global_delta = _clamp_vector(_delta(axis_transform.get("average_delta")), 0.16)
    guide_delta = _matching_guide_delta(axis_transform, guide_area)
    combined_delta = {axis: 0.25 * global_delta[axis] for axis in ("x", "y", "z")}
    if guide_delta is not None:
        guide_delta = _clamp_vector(guide_delta, 0.32)
        for axis in ("x", "y", "z"):
            combined_delta[axis] += 0.65 * guide_delta[axis]
    combined_delta = _clamp_vector(combined_delta, 0.28)
    if _radius(combined_delta) > 1e-9:
        position = _add_delta(position, combined_delta)
        adjustments.append(
            {
                "kind": "semantic_axis_transform",
                "guide_area": guide_area or None,
                "applied_delta": {axis: round(combined_delta[axis], 6) for axis in ("x", "y", "z")},
                "used_guide_specific_delta": guide_delta is not None,
            }
        )

    position, radial_adjustment = _apply_radial_transform(position, _dict_value(context.get("radial_band_transform")))
    if radial_adjustment:
        adjustments.append(radial_adjustment)

    for prior in _projection_priors_for_guide(_list_value(context.get("attraction_priors")), guide_area=guide_area):
        target = dict(prior["target_position"])
        strength = max(0.04, min(0.18, 0.08 + 0.12 * _float(prior.get("delta_norm"), 0.0)))
        position = _blend_toward(position, target, strength)
        adjustments.append(
            {
                "kind": "topology_attraction_prior",
                "node_id": prior.get("node_id"),
                "weight": round(strength, 4),
            }
        )

    for hint in _projection_priors_for_guide(_list_value(context.get("rotation_hints")), guide_area=guide_area, limit=4):
        target = dict(hint["target_position"])
        position = _blend_toward(position, target, 0.045)
        adjustments.append(
            {
                "kind": "topology_rotation_hint",
                "node_id": hint.get("node_id"),
                "weight": 0.045,
            }
        )

    for prior in _projection_priors_for_guide(_list_value(context.get("repulsion_priors")), guide_area=guide_area):
        target = dict(prior["target_position"])
        fit = _distance(position, target)
        if fit >= 0.34:
            continue
        push_weight = max(0.03, min(0.14, (0.34 - fit) / 0.34 * 0.14))
        direction = {axis: float(position[axis]) - float(target[axis]) for axis in ("x", "y", "z")}
        if _radius(direction) <= 1e-9:
            direction = _delta(axis_transform.get("average_delta"))
        direction = _clamp_vector(direction, push_weight)
        position = _add_delta(position, direction)
        adjustments.append(
            {
                "kind": "topology_repulsion_prior",
                "node_id": prior.get("node_id"),
                "weight": round(push_weight, 4),
                "distance_before": round(fit, 6),
            }
        )

    position = _clamp_position_radius(position)
    total_shift = _distance(before, position)
    if total_shift <= 1e-9:
        return {
            **seed,
            "matrix_revision_id": matrix_revision_id or None,
            "topology_revision_id": topology_revision_id or None,
            "matrix_calibration_plan_signature": _text(context.get("plan_signature")) or None,
            "active_spatial_revision_context": _compact_context(context),
            "active_matrix_projection": {
                "schema_version": ACTIVE_MATRIX_PLACEMENT_SCHEMA_VERSION,
                "status": "revision_attached_no_geometric_shift",
                "matrix_revision_id": matrix_revision_id or None,
                "topology_revision_id": topology_revision_id or None,
                "before_base_position": {axis: round(before[axis], 6) for axis in ("x", "y", "z")},
                "after_base_position": {axis: round(position[axis], 6) for axis in ("x", "y", "z")},
                "total_shift": 0.0,
                "adjustments": [],
            },
        }

    local_plan = dict(seed.get("local_correction_plan") or {})
    local_plan["active_matrix_projection"] = {
        "matrix_revision_id": matrix_revision_id or None,
        "topology_revision_id": topology_revision_id or None,
        "total_shift": round(total_shift, 6),
    }
    return {
        **seed,
        "base_position": position,
        "matrix_revision_id": matrix_revision_id or None,
        "topology_revision_id": topology_revision_id or None,
        "matrix_calibration_plan_signature": _text(context.get("plan_signature")) or None,
        "active_spatial_revision_context": _compact_context(context),
        "active_matrix_projection": {
            "schema_version": ACTIVE_MATRIX_PLACEMENT_SCHEMA_VERSION,
            "status": "active_revision_applied",
            "matrix_revision_id": matrix_revision_id or None,
            "topology_revision_id": topology_revision_id or None,
            "guide_area": guide_area or None,
            "before_base_position": {axis: round(before[axis], 6) for axis in ("x", "y", "z")},
            "after_base_position": {axis: round(position[axis], 6) for axis in ("x", "y", "z")},
            "total_shift": round(total_shift, 6),
            "adjustments": adjustments[:16],
            "quality_delta": _dict_value(context.get("quality_delta")),
        },
        "local_correction_plan": local_plan,
    }


def _compact_context(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": ACTIVE_SPATIAL_REVISION_CONTEXT_SCHEMA_VERSION,
        "matrix_revision_id": _text(context.get("matrix_revision_id")) or None,
        "topology_revision_id": _text(context.get("topology_revision_id")) or None,
        "plan_signature": _text(context.get("plan_signature")) or None,
        "base_projection_version": _text(context.get("base_projection_version")) or None,
    }
