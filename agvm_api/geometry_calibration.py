# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

"""Non-mutating geometry labels required by public Core health reports."""

from __future__ import annotations

from typing import Any


PUBLIC_CLOUD_ACTION_STUB = True


def expected_brain_geometry_profile(node: dict[str, Any]) -> dict[str, Any]:
    """Classify a node for health reporting without producing an apply plan."""

    memory_type = str(node.get("memory_type") or node.get("node_kind") or "").strip().lower()
    guide_area = str((node.get("provenance") or {}).get("guide_conceptual_area") or "").strip().lower()
    temporal_role = str(node.get("temporal_role") or "").strip().lower()
    if memory_type in {"document", "document_anchor", "document_chunk", "source_unit", "raw_source"}:
        return _profile("documents", "Documents", 0.62, 1.0, "document_substrate")
    if temporal_role in {"future", "future_intent", "dream"}:
        return _profile("future_hypotheses", "Future / Hypotheses", 0.56, 0.94, "future_memory")
    if memory_type in {"identity", "value", "values"} or guide_area in {"identity", "values"}:
        return _profile("identity", "Identity / Values", 0.06, 0.44, "identity_core")
    if memory_type in {"relationship", "relational"} or guide_area in {"relationships", "relations"}:
        return _profile("relationships", "Relationships", 0.28, 0.70, "relationship_ring")
    if memory_type in {"project", "technical", "operational", "work"}:
        return _profile("projects", "Projects", 0.32, 0.82, "project_work_ring")
    if memory_type in {"episodic", "timeline"} or temporal_role == "past_state":
        return _profile("history", "History", 0.50, 0.90, "temporal_history_ring")
    return _profile("knowledge", "Knowledge", 0.34, 0.86, "general_memory_ring")


def _profile(zone: str, label: str, minimum: float, maximum: float, basis: str) -> dict[str, Any]:
    return {
        "zone": zone,
        "label": label,
        "min_radius": minimum,
        "max_radius": maximum,
        "basis": basis,
    }
