# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any


MODULE_PATH = Path(__file__).resolve().parent
ROOT_PATH = MODULE_PATH.parent
if not (ROOT_PATH / "docs").exists() and (MODULE_PATH / "docs").exists():
    ROOT_PATH = MODULE_PATH
LEGACY_GUIDE_PATH = ROOT_PATH / "AGVM_BetaVectorMemory_ExpandedGuide_v6.md"
MASTER_PATH = ROOT_PATH / "docs" / "AGVM_MASTER.md"
SPATIAL_BRIEF_PATH = ROOT_PATH / "docs" / "AGVM_METAMEMORY_SPATIAL_BRIEF.md"
GUIDE_PATH = MASTER_PATH

ROLE_SECTION_HINTS = {
    "compiler": (
        "PRODUCT DEFINITION",
        "AI-CORE",
        "COGNITIVE GROW",
        "IDENTITY",
        "MCP",
        "COMPILER",
    ),
    "retrieval": (
        "AI-CORE RUNTIME CONTRACT",
        "AI SPATIAL LANDING CONTRACT",
        "COORDINATE SYSTEM",
        "GUIDE AREAS",
        "RADIAL BANDS",
        "LANDING POLICY",
        "PATH POLICY",
        "RETRIEVAL",
        "MCP",
    ),
    "answer": (
        "PRODUCT DEFINITION",
        "RETRIEVAL",
        "CONTEXT",
        "ANSWER DEMO",
        "MCP",
    ),
    "sleep": (
        "HEURISTIC LEARNING",
        "CALIBRATION LOOP",
        "SLEEP",
        "EVOLVE",
        "MAINTENANCE",
        "METAMEMORY",
    ),
}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _heading_blocks(markdown: str) -> list[tuple[str, str]]:
    lines = markdown.splitlines()
    blocks: list[tuple[str, str]] = []
    current_heading = "INTRO"
    current_lines: list[str] = []
    for line in lines:
        if re.match(r"^#{1,6}\s+", line):
            if current_lines:
                blocks.append((current_heading, "\n".join(current_lines).strip()))
            current_heading = re.sub(r"^#{1,6}\s+", "", line).strip()
            current_lines = []
            continue
        current_lines.append(line)
    if current_lines:
        blocks.append((current_heading, "\n".join(current_lines).strip()))
    return blocks


@lru_cache(maxsize=1)
def _source_documents() -> tuple[tuple[str, str, str], ...]:
    paths = [
        ("spatial_brief", SPATIAL_BRIEF_PATH),
        ("master", MASTER_PATH),
        ("legacy_guide", LEGACY_GUIDE_PATH),
    ]
    documents: list[tuple[str, str, str]] = []
    for source_kind, path in paths:
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if text.strip():
            documents.append((source_kind, str(path), text))
    return tuple(documents)


@lru_cache(maxsize=1)
def _guide_sections() -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    for source_kind, path, text in _source_documents():
        for heading, body in _heading_blocks(text):
            sections.append((f"{source_kind}: {heading}", body))
    return sections


def _select_blocks(role: str) -> list[tuple[str, str]]:
    hints = ROLE_SECTION_HINTS.get(role, ())
    sections = _guide_sections()
    if not sections:
        return []
    selected: list[tuple[str, str]] = []
    for heading, body in sections:
        upper = heading.upper()
        if any(hint in upper for hint in hints):
            selected.append((heading, body))
    if not selected:
        selected = sections[:8]
    return selected


def _truncate_body(body: str, limit: int) -> str:
    cleaned = _normalize(body)
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 3].rstrip()}..."


@lru_cache(maxsize=8)
def build_metamemory_package(role: str) -> str:
    blocks = _select_blocks(role)
    if not blocks:
        return (
            "AGVM constitution unavailable. Default to: LLM-first, local-first retrieval; "
            "memory compiler is single-call; answer must be grounded; text loading must be budgeted."
        )
    per_block_limit = {
        "compiler": 1200,
        "retrieval": 1200,
        "answer": 900,
        "sleep": 900,
    }.get(role, 1000)
    snippets = []
    for heading, body in blocks[:8]:
        snippets.append(f"[{heading}] {_truncate_body(body, per_block_limit)}")
    package = "\n\n".join(snippets)
    role_marker = (
        "\nCore retrieval headings: AI Spatial Landing Contract; COORDINATE SYSTEM.\n"
        if role == "retrieval"
        else "\n"
    )
    return (
        "AGVM constitutional metamemory package.\n"
        "Use this as the governing policy for interpretation, memory compilation, retrieval planning, "
        "answer generation, and sleep/evolve decisions."
        f"{role_marker}\n"
        f"{package}"
    )


def _json_digest(payload: Any, *, length: int = 16) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def _revision(prefix: str, payload: Any) -> str:
    return f"{prefix}::{_json_digest(payload)}"


def _active_spatial_revisions(brain_id: str | None) -> dict[str, Any]:
    normalized = str(brain_id or "").strip()
    if not normalized:
        return {}
    try:
        from sqlite_store import fetch_active_matrix_revision, fetch_active_topology_field_revision

        matrix = fetch_active_matrix_revision(brain_id=normalized)
        topology = fetch_active_topology_field_revision(brain_id=normalized)
    except Exception:
        return {}
    return {
        "matrix_revision": dict(matrix or {}),
        "topology_field_revision": dict(topology or {}),
    }


def _active_memory_policy_revision(brain_id: str | None) -> dict[str, Any]:
    normalized = str(brain_id or "").strip()
    if not normalized:
        return {}
    try:
        from sqlite_store import fetch_active_memory_policy_revision

        return dict(fetch_active_memory_policy_revision(brain_id=normalized) or {})
    except Exception:
        return {}


def _compact_matrix_revision(revision: dict[str, Any] | None) -> dict[str, Any]:
    payload = revision if isinstance(revision, dict) else {}
    if not payload:
        return {}
    return {
        "schema_version": payload.get("schema_version"),
        "matrix_revision_id": payload.get("matrix_revision_id"),
        "parent_revision_id": payload.get("parent_revision_id"),
        "base_projection_version": payload.get("base_projection_version"),
        "semantic_axis_transform": payload.get("semantic_axis_transform") if isinstance(payload.get("semantic_axis_transform"), dict) else {},
        "radial_band_transform": payload.get("radial_band_transform") if isinstance(payload.get("radial_band_transform"), dict) else {},
        "guide_area_transform": payload.get("guide_area_transform") if isinstance(payload.get("guide_area_transform"), dict) else {},
        "quality_before": payload.get("quality_before") if isinstance(payload.get("quality_before"), dict) else {},
        "quality_after": payload.get("quality_after") if isinstance(payload.get("quality_after"), dict) else {},
        "created_at": payload.get("created_at"),
        "activated_at": payload.get("activated_at"),
    }


def _compact_memory_policy_revision(revision: dict[str, Any] | None) -> dict[str, Any]:
    payload = revision if isinstance(revision, dict) else {}
    if not payload:
        return {}
    return {
        "schema_version": payload.get("schema_version"),
        "policy_revision_id": payload.get("policy_revision_id"),
        "parent_policy_revision_id": payload.get("parent_policy_revision_id"),
        "policy_scope": payload.get("policy_scope"),
        "status": payload.get("status"),
        "ingest_rules": payload.get("ingest_rules") if isinstance(payload.get("ingest_rules"), dict) else {},
        "retrieval_rules": payload.get("retrieval_rules") if isinstance(payload.get("retrieval_rules"), dict) else {},
        "source_rules": payload.get("source_rules") if isinstance(payload.get("source_rules"), dict) else {},
        "deduction_rules": payload.get("deduction_rules") if isinstance(payload.get("deduction_rules"), dict) else {},
        "sleep_rules": payload.get("sleep_rules") if isinstance(payload.get("sleep_rules"), dict) else {},
        "evolve_rules": payload.get("evolve_rules") if isinstance(payload.get("evolve_rules"), dict) else {},
        "matrix_rules": payload.get("matrix_rules") if isinstance(payload.get("matrix_rules"), dict) else {},
        "supporting_event_ids": list(payload.get("supporting_event_ids") or [])[:24],
        "quality_after": payload.get("quality_after") if isinstance(payload.get("quality_after"), dict) else {},
        "created_at": payload.get("created_at"),
        "activated_at": payload.get("activated_at"),
    }


def _compact_topology_revision(revision: dict[str, Any] | None) -> dict[str, Any]:
    payload = revision if isinstance(revision, dict) else {}
    if not payload:
        return {}
    return {
        "schema_version": payload.get("schema_version"),
        "topology_revision_id": payload.get("topology_revision_id"),
        "matrix_revision_id": payload.get("matrix_revision_id"),
        "attraction_priors": list(payload.get("attraction_priors") or [])[:12],
        "repulsion_priors": list(payload.get("repulsion_priors") or [])[:12],
        "rotation_hints": list(payload.get("rotation_hints") or [])[:12],
        "density_constraints": payload.get("density_constraints") if isinstance(payload.get("density_constraints"), dict) else {},
        "bridge_corridors": list(payload.get("bridge_corridors") or [])[:12],
        "unstable_regions": list(payload.get("unstable_regions") or [])[:12],
        "saturated_regions": list(payload.get("saturated_regions") or [])[:12],
        "quality_before": payload.get("quality_before") if isinstance(payload.get("quality_before"), dict) else {},
        "quality_after": payload.get("quality_after") if isinstance(payload.get("quality_after"), dict) else {},
        "created_at": payload.get("created_at"),
        "activated_at": payload.get("activated_at"),
    }


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _source_hash() -> str:
    seed = [
        {"source_kind": source_kind, "path": path, "hash": hashlib.sha256(text.encode("utf-8")).hexdigest()}
        for source_kind, path, text in _source_documents()
    ]
    return _json_digest(seed)


def _as_position(value: Any) -> dict[str, float] | None:
    if not isinstance(value, dict):
        return None
    try:
        return {
            "x": round(float(value.get("x") or 0.0), 6),
            "y": round(float(value.get("y") or 0.0), 6),
            "z": round(float(value.get("z") or 0.0), 6),
        }
    except (TypeError, ValueError):
        return None


def _atlas_summary(atlas_payload: dict[str, Any] | None) -> dict[str, Any]:
    atlas = atlas_payload if isinstance(atlas_payload, dict) else {}
    buckets = atlas.get("buckets") or atlas.get("atlas_buckets") or atlas.get("bucket_summaries") or []
    bucket_rows = [dict(item) for item in buckets if isinstance(item, dict)]
    compact_buckets: list[dict[str, Any]] = []
    for row in bucket_rows[:24]:
        centroid = _as_position(row.get("centroid") or row.get("position") or row.get("center"))
        compact_buckets.append(
            {
                "bucket_key": str(row.get("bucket_key") or row.get("key") or row.get("id") or "").strip(),
                "centroid": centroid,
                "node_count": int(row.get("node_count") or row.get("count") or 0),
                "dominant_guide_area": str(row.get("dominant_guide_area") or row.get("guide_area") or "").strip(),
                "dominant_memory_type": str(row.get("dominant_memory_type") or row.get("memory_type") or "").strip(),
                "highway_gateway": bool(row.get("highway_gateway") or row.get("is_highway_gateway")),
            }
        )
    node_count = int(atlas.get("node_count") or sum(int(row.get("node_count") or row.get("count") or 0) for row in bucket_rows) or 0)
    atlas_seed = {
        "node_count": node_count,
        "bucket_count": len(bucket_rows),
        "sample_buckets": compact_buckets,
        "highway_count": len([row for row in compact_buckets if bool(row.get("highway_gateway"))]),
    }
    return {
        "schema_version": "agvm.metamemory_atlas_summary.v1",
        "atlas_revision": _revision("atlas", atlas_seed),
        "node_count": node_count,
        "bucket_count": len(bucket_rows),
        "sampled_bucket_count": len(compact_buckets),
        "buckets": compact_buckets,
        "sample_buckets": compact_buckets,
    }


def _nuclei_summary(
    *,
    identity_nucleus: dict[str, Any] | None,
    atlas_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    identity = identity_nucleus if isinstance(identity_nucleus, dict) else {}
    atlas = atlas_payload if isinstance(atlas_payload, dict) else {}
    nuclei = atlas.get("nuclei") if isinstance(atlas.get("nuclei"), dict) else {}
    identity_position = _as_position(identity.get("centroid") or identity.get("position") or identity.get("final_position"))
    return {
        "schema_version": "agvm.metamemory_nuclei_summary.v1",
        "identity": {
            "id": str(identity.get("id") or identity.get("node_id") or nuclei.get("identity") or "").strip(),
            "name": str(identity.get("name") or identity.get("core_name") or "").strip(),
            "centroid": identity_position,
        },
        "project": nuclei.get("project") if isinstance(nuclei.get("project"), dict) else {},
        "document": nuclei.get("document") if isinstance(nuclei.get("document"), dict) else {},
    }


def _calibration_summary(calibration_snapshot: dict[str, Any] | None) -> dict[str, Any]:
    calibration = calibration_snapshot if isinstance(calibration_snapshot, dict) else {}
    compiled_priors = calibration.get("compiled_priors") if isinstance(calibration.get("compiled_priors"), dict) else {}
    failure_signatures = calibration.get("failure_signatures") if isinstance(calibration.get("failure_signatures"), dict) else {}
    spatial_correction_priors = (
        calibration.get("spatial_correction_priors")
        if isinstance(calibration.get("spatial_correction_priors"), dict)
        else {}
    )
    sample_correction_priors: list[dict[str, Any]] = []
    for key, payload in list(spatial_correction_priors.items())[:12]:
        row = payload if isinstance(payload, dict) else {}
        sample_correction_priors.append(
            {
                "prior_id": str(key),
                "status": str(row.get("status") or "").strip(),
                "scope_key": str(row.get("scope_key") or key or "").strip(),
                "expected_region": str(row.get("expected_region") or "").strip(),
                "actual_region": str(row.get("actual_region") or "").strip(),
                "review_required": bool((row.get("review") or {}).get("required", True)) if isinstance(row.get("review"), dict) else True,
            }
        )
    seed = {
        "event_count": int(calibration.get("event_count") or 0),
        "compiled_prior_count": len(compiled_priors),
        "failure_signature_count": len(failure_signatures),
        "spatial_correction_prior_count": len(spatial_correction_priors),
        "updated_at": calibration.get("updated_at"),
    }
    return {
        "schema_version": "agvm.metamemory_calibration_summary.v1",
        "calibration_revision": _revision("calibration", seed),
        "event_count": int(calibration.get("event_count") or 0),
        "compiled_prior_count": len(compiled_priors),
        "failure_signature_count": len(failure_signatures),
        "spatial_correction_prior_count": len(spatial_correction_priors),
        "updated_at": calibration.get("updated_at"),
        "sample_scope_keys": sorted(str(key) for key in list(compiled_priors.keys())[:12]),
        "sample_failure_keys": sorted(str(key) for key in list(failure_signatures.keys())[:12]),
        "sample_correction_priors": sample_correction_priors,
    }


def _base_matrix_summary(
    *,
    guide_areas: list[str],
    radial_bands: list[str],
    atlas: dict[str, Any],
    active_matrix_revision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    semantic_zones = {
        "identity_core": ["identity", "core"],
        "project_work": ["work_projects", "near", "working"],
        "document_source": ["documents", "source_evidence", "working"],
        "style_values": ["values_style", "near"],
        "history_timeline": ["timeline_history", "near", "reservoir"],
    }
    bucket_centroids = [
        {
            "bucket_key": row.get("bucket_key"),
            "centroid": row.get("centroid"),
            "node_count": row.get("node_count"),
            "dominant_guide_area": row.get("dominant_guide_area"),
        }
        for row in _as_list(atlas.get("sample_buckets"))[:16]
        if isinstance(row, dict)
    ]
    seed = {
        "coordinate_system": "normalized_semantic_xyz",
        "guide_areas": guide_areas,
        "radial_bands": radial_bands,
        "semantic_zones": semantic_zones,
    }
    active_matrix = _compact_matrix_revision(active_matrix_revision)
    matrix_revision = str(active_matrix.get("matrix_revision_id") or _revision("matrix", seed))
    return {
        "schema_version": "agvm.base_matrix_summary.v1",
        "matrix_revision": matrix_revision,
        "coordinate_system": "normalized_semantic_xyz",
        "guide_areas": list(guide_areas),
        "radial_bands": list(radial_bands),
        "semantic_zones": semantic_zones,
        "bucket_centroids": bucket_centroids,
        "active_matrix_revision": active_matrix,
        "policy": (
            "active_per_brain_matrix_revision_controls_future_spatial_reasoning"
            if active_matrix
            else "base_matrix_is_generic_projection_personal_shape_lives_in_topology_overlay"
        ),
    }


def _topology_overlay_summary(
    *,
    atlas_payload: dict[str, Any] | None,
    atlas: dict[str, Any],
    calibration: dict[str, Any],
    matrix_revision: str,
    active_topology_revision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw_atlas = atlas_payload if isinstance(atlas_payload, dict) else {}
    buckets = [dict(item) for item in _as_list(atlas.get("sample_buckets")) if isinstance(item, dict)]
    density_lobes = [
        {
            "lobe_id": f"density::{row.get('bucket_key') or index}",
            "region_ref": row.get("dominant_guide_area") or row.get("bucket_key") or "unknown",
            "bucket_key": row.get("bucket_key"),
            "centroid": row.get("centroid"),
            "node_count": int(row.get("node_count") or 0),
            "source": "atlas_bucket_density",
        }
        for index, row in enumerate(sorted(buckets, key=lambda item: int(item.get("node_count") or 0), reverse=True)[:12])
        if int(row.get("node_count") or 0) > 0
    ]
    active_highways = [
        {
            "highway_id": f"gateway::{row.get('bucket_key') or index}",
            "bucket_key": row.get("bucket_key"),
            "region_ref": row.get("dominant_guide_area") or row.get("bucket_key") or "unknown",
            "centroid": row.get("centroid"),
            "source": "atlas_highway_gateway",
        }
        for index, row in enumerate(buckets[:24])
        if bool(row.get("highway_gateway"))
    ]
    explicit_bridges = _as_list(raw_atlas.get("bridge_corridors") or raw_atlas.get("bridges") or [])
    bridge_corridors = [
        {
            "bridge_id": str((item or {}).get("bridge_id") or (item or {}).get("id") or f"bridge::{index}"),
            "from_region": str((item or {}).get("from_region") or (item or {}).get("source_region") or "").strip(),
            "to_region": str((item or {}).get("to_region") or (item or {}).get("target_region") or "").strip(),
            "confidence": (item or {}).get("confidence"),
            "source": "atlas_bridge_corridor",
        }
        for index, item in enumerate(explicit_bridges[:12])
        if isinstance(item, dict)
    ]
    correction_priors = list(calibration.get("sample_correction_priors") or [])
    attraction_priors = [
        {
            "prior_id": str(item.get("prior_id") or f"attraction::{index}"),
            "from_region": item.get("expected_region"),
            "to_region": item.get("actual_region"),
            "review_required": bool(item.get("review_required")),
            "source": "spatial_correction_prior",
        }
        for index, item in enumerate(correction_priors[:12])
        if isinstance(item, dict)
    ]
    repulsion_priors: list[dict[str, Any]] = []
    active_topology = _compact_topology_revision(active_topology_revision)
    if active_topology:
        bridge_corridors = [
            *bridge_corridors,
            *[
                {**dict(item), "source": dict(item).get("source") or "active_topology_field_revision"}
                for item in list(active_topology.get("bridge_corridors") or [])[:12]
                if isinstance(item, dict)
            ],
        ][:24]
        attraction_priors = [
            *attraction_priors,
            *[
                {**dict(item), "source": dict(item).get("source") or "active_topology_field_revision"}
                for item in list(active_topology.get("attraction_priors") or [])[:12]
                if isinstance(item, dict)
            ],
        ][:24]
        repulsion_priors = [
            *repulsion_priors,
            *[
                {**dict(item), "source": dict(item).get("source") or "active_topology_field_revision"}
                for item in list(active_topology.get("repulsion_priors") or [])[:12]
                if isinstance(item, dict)
            ],
        ][:24]
    pending_maintenance_proposals = [
        {
            "proposal_id": str(item.get("prior_id") or f"spatial_prior::{index}"),
            "proposal_kind": "spatial_correction_prior_review",
            "recommended_tool": "matrix_calibration_preview",
            "reason": "review repeated route/landing corrections before applying geometry",
        }
        for index, item in enumerate(correction_priors[:12])
        if isinstance(item, dict) and bool(item.get("review_required"))
    ]
    dominant_counts = [int(row.get("node_count") or 0) for row in density_lobes]
    asymmetry_signatures: list[dict[str, Any]] = []
    if dominant_counts:
        total = sum(dominant_counts)
        max_count = max(dominant_counts)
        asymmetry_signatures.append(
            {
                "signature_id": "density_skew",
                "max_lobe_share": round(max_count / max(1, total), 6),
                "lobe_count": len(density_lobes),
                "source": "atlas_density_distribution",
            }
        )
    overlay_present = bool(density_lobes or active_highways or bridge_corridors or attraction_priors or active_topology)
    seed = {
        "matrix_revision": matrix_revision,
        "atlas_revision": atlas.get("atlas_revision"),
        "density_lobes": density_lobes,
        "active_highways": active_highways,
        "bridge_corridors": bridge_corridors,
        "attraction_priors": attraction_priors,
        "repulsion_priors": repulsion_priors,
    }
    topology_revision = str(active_topology.get("topology_revision_id") or "") or (_revision("topology", seed) if overlay_present else None)
    missing_reasons = [] if overlay_present else ["topology_overlay_missing_or_empty"]
    return {
        "schema_version": "agvm.topology_overlay_summary.v1",
        "topology_revision": topology_revision,
        "matrix_revision": matrix_revision,
        "atlas_revision": atlas.get("atlas_revision"),
        "overlay_present": overlay_present,
        "density_lobes": density_lobes,
        "asymmetry_signatures": asymmetry_signatures,
        "active_highways": active_highways,
        "bridge_corridors": bridge_corridors,
        "attraction_priors": attraction_priors,
        "repulsion_priors": repulsion_priors,
        "correction_priors": correction_priors,
        "rotation_hints": list(active_topology.get("rotation_hints") or [])[:12] if active_topology else [],
        "active_topology_field_revision": active_topology,
        "confidence": 0.72 if overlay_present else 0.0,
        "source_evidence_window": {
            "source": (
                "active_topology_field_revision_runtime_atlas_and_calibration_snapshot"
                if active_topology
                else "runtime_atlas_and_calibration_snapshot"
            ),
            "atlas_bucket_count": int(atlas.get("bucket_count") or 0),
            "calibration_event_count": int(calibration.get("event_count") or 0),
        },
        "pending_maintenance_proposals": pending_maintenance_proposals,
        "missing_reasons": missing_reasons,
        "review_required": bool(pending_maintenance_proposals or missing_reasons),
        "mutation_policy": {
            "retrieve_mutates_geometry": False,
            "sleep_can_consolidate_low_risk_memory_quality": True,
            "evolve_can_propose_topology_changes": True,
            "matrix_calibration_can_apply_with_rollback": True,
        },
    }


def metamemory_spatial_readiness_contract(
    brief: dict[str, Any] | None,
    *,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = brief if isinstance(brief, dict) else {}
    meta = snapshot if isinstance(snapshot, dict) else {}
    topology = payload.get("topology_overlay_summary") if isinstance(payload.get("topology_overlay_summary"), dict) else {}
    base_matrix = payload.get("base_matrix_summary") if isinstance(payload.get("base_matrix_summary"), dict) else {}
    atlas = payload.get("atlas_summary") if isinstance(payload.get("atlas_summary"), dict) else {}
    missing: list[str] = []
    stale: list[str] = []
    if payload.get("schema_version") != "agvm.metamemory_spatial_brief.v1":
        missing.append("metamemory_spatial_brief_schema")
    if not str(payload.get("brain_revision") or "").strip():
        missing.append("brain_revision")
    if not str(payload.get("metamemory_revision") or payload.get("source_snapshot_version") or "").strip():
        missing.append("metamemory_revision")
    if not str(payload.get("matrix_revision") or base_matrix.get("matrix_revision") or "").strip():
        missing.append("matrix_revision")
    if not str(payload.get("atlas_revision") or atlas.get("atlas_revision") or "").strip():
        missing.append("atlas_revision")
    if not str(payload.get("calibration_revision") or "").strip():
        missing.append("calibration_revision")
    if not base_matrix:
        missing.append("base_matrix_summary")
    if not topology:
        missing.append("topology_overlay_summary")
    if topology and not bool(topology.get("overlay_present")):
        missing.append("topology_overlay_material")
    if topology and not str(payload.get("topology_revision") or topology.get("topology_revision") or "").strip():
        missing.append("topology_revision")
    if meta:
        if not bool(meta.get("guide_exists")):
            missing.append("metamemory_guide")
        if not bool(meta.get("spatial_brief_exists")):
            missing.append("spatial_brief_document")
        if str(meta.get("snapshot_version") or "").strip() and str(payload.get("source_snapshot_version") or "").strip():
            if str(meta.get("snapshot_version")) != str(payload.get("source_snapshot_version")):
                stale.append("source_snapshot_version_mismatch")
        if str(meta.get("hash") or "").strip() and str(payload.get("source_hash") or "").strip():
            if str(meta.get("hash")) != str(payload.get("source_hash")):
                stale.append("source_hash_mismatch")
    certifiable = not missing and not stale
    return {
        "schema_version": "agvm.metamemory_spatial_readiness.v1",
        "status": "ready" if certifiable else "incomplete",
        "certifiable": certifiable,
        "missing_reasons": missing,
        "stale_reasons": stale,
        "revision_chain": {
            "brain_revision": payload.get("brain_revision"),
            "metamemory_revision": payload.get("metamemory_revision") or payload.get("source_snapshot_version"),
            "matrix_revision": payload.get("matrix_revision") or base_matrix.get("matrix_revision"),
            "topology_revision": payload.get("topology_revision") or topology.get("topology_revision"),
            "atlas_revision": payload.get("atlas_revision") or atlas.get("atlas_revision"),
            "calibration_revision": payload.get("calibration_revision"),
            "source_replay_revision": payload.get("source_replay_revision"),
        },
        "policy": {
            "retrieve_mutates_geometry": False,
            "missing_overlay_blocks_revolutionary_certification": True,
            "partial_context_still_allowed": True,
        },
    }


def metamemory_spatial_brief(
    *,
    role: str = "retrieval",
    brain_id: str | None = None,
    brain_revision: str | None = None,
    atlas_payload: dict[str, Any] | None = None,
    identity_nucleus: dict[str, Any] | None = None,
    calibration_snapshot: dict[str, Any] | None = None,
    mode_budget: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snapshot = metamemory_snapshot()
    source_revision = str(snapshot.get("snapshot_version") or "")
    active_spatial_revisions = _active_spatial_revisions(brain_id)
    active_matrix_revision = dict(active_spatial_revisions.get("matrix_revision") or {})
    active_topology_revision = dict(active_spatial_revisions.get("topology_field_revision") or {})
    active_memory_policy_revision = _active_memory_policy_revision(brain_id)
    compact_memory_policy_revision = _compact_memory_policy_revision(active_memory_policy_revision)
    atlas = _atlas_summary(atlas_payload)
    nuclei = _nuclei_summary(identity_nucleus=identity_nucleus, atlas_payload=atlas_payload)
    calibration = _calibration_summary(calibration_snapshot)
    guide_areas = [
        "identity",
        "work_projects",
        "documents",
        "relationships",
        "values_style",
        "timeline_history",
        "source_evidence",
        "maintenance",
    ]
    radial_bands = ["core", "near", "working", "reservoir", "edge"]
    base_matrix = _base_matrix_summary(
        guide_areas=guide_areas,
        radial_bands=radial_bands,
        atlas=atlas,
        active_matrix_revision=active_matrix_revision,
    )
    topology_overlay = _topology_overlay_summary(
        atlas_payload=atlas_payload,
        atlas=atlas,
        calibration=calibration,
        matrix_revision=str(base_matrix.get("matrix_revision") or ""),
        active_topology_revision=active_topology_revision,
    )
    prompt_brief = (
        "Use inverse answer strands to choose guide_area, radial_band and optional coarse coordinates. "
        "Do not request a full brain dump. Backend will snap coordinates and traverse local/highway/link/document paths."
    )
    seed = {
        "schema": "agvm.metamemory_spatial_brief.v1",
        "role": role,
        "brain_id": brain_id,
        "brain_revision": brain_revision,
        "source_revision": source_revision,
        "matrix_revision": base_matrix.get("matrix_revision"),
        "topology_revision": topology_overlay.get("topology_revision"),
        "memory_policy_revision": compact_memory_policy_revision.get("policy_revision_id"),
        "atlas_revision": atlas.get("atlas_revision"),
        "calibration_revision": calibration.get("calibration_revision"),
        "atlas_bucket_count": atlas["bucket_count"],
        "calibration_event_count": calibration["event_count"],
    }
    payload = {
        "schema_version": "agvm.metamemory_spatial_brief.v1",
        "revision": f"spatial::{_json_digest(seed)}",
        "role": str(role or "retrieval"),
        "source_snapshot_version": source_revision,
        "metamemory_revision": source_revision,
        "source_hash": snapshot.get("hash"),
        "source_paths": list(snapshot.get("source_paths") or []),
        "brain_id": str(brain_id or "").strip() or None,
        "brain_revision": str(brain_revision or "").strip() or None,
        "matrix_revision": base_matrix.get("matrix_revision"),
        "topology_revision": topology_overlay.get("topology_revision"),
        "memory_policy_revision": compact_memory_policy_revision.get("policy_revision_id"),
        "atlas_revision": atlas.get("atlas_revision"),
        "calibration_revision": calibration.get("calibration_revision"),
        "source_replay_revision": _revision("source_replay", {"source_revision": source_revision, "source_hash": snapshot.get("hash")}),
        "coordinate_system": {
            "space": "normalized_semantic_xyz",
            "x": "identity_self_to_external_project_source",
            "y": "stable_long_term_to_recent_operational",
            "z": "concrete_source_evidence_to_reflective_derived_abstraction",
            "coordinate_policy": "ai_selects_semantic_region_backend_snaps_and_traverses",
        },
        "guide_areas": guide_areas,
        "semantic_zones": dict(base_matrix.get("semantic_zones") or {}),
        "radial_bands": radial_bands,
        "base_matrix_summary": base_matrix,
        "topology_overlay_summary": topology_overlay,
        "active_spatial_revision_summary": {
            "schema_version": "agvm.metamemory_active_spatial_revision_summary.v1",
            "matrix_revision": _compact_matrix_revision(active_matrix_revision),
            "topology_field_revision": _compact_topology_revision(active_topology_revision),
        },
        "active_memory_policy_revision_summary": {
            "schema_version": "agvm.metamemory_active_memory_policy_revision_summary.v1",
            "memory_policy_revision": compact_memory_policy_revision,
            "policy": (
                "active_per_brain_memory_policy_guides_future_ingest_retrieve_sleep_evolve_matrix"
                if compact_memory_policy_revision
                else "no_active_per_brain_memory_policy_revision_yet"
            ),
        },
        "atlas_summary": atlas,
        "nuclei": nuclei,
        "highway_gateways": [
            row for row in atlas["buckets"] if bool(row.get("highway_gateway"))
        ][:12],
        "calibration_summary": calibration,
        "mission_learning_summary": {
            "schema_version": "agvm.metamemory_mission_learning_summary.v1",
            "recent_failures": [],
            "cold_answer_discoveries": [],
            "correction_priors": list(calibration.get("sample_correction_priors") or []),
        },
        "mode_budget": dict(mode_budget or {}),
        "prompt_brief": prompt_brief,
        "materialization_policy": "missing_spatial_brief_blocks_final_revolutionary_certification",
    }
    payload["spatial_readiness_contract"] = metamemory_spatial_readiness_contract(payload, snapshot=snapshot)
    return payload


def metamemory_snapshot() -> dict[str, Any]:
    documents = _source_documents()
    if not documents:
        return {
            "schema_version": "agvm.metamemory.snapshot.v3",
            "snapshot_version": "guide.missing",
            "guide_path": str(GUIDE_PATH),
            "source_paths": [],
            "active_source_kind": "missing",
            "guide_exists": 0,
            "spatial_brief_exists": 0,
            "hash": "",
            "sections": 0,
            "mutable": 0,
        }
    digest = _source_hash()
    return {
        "schema_version": "agvm.metamemory.snapshot.v3",
        "snapshot_version": f"guide.{digest}",
        "guide_path": str(GUIDE_PATH),
        "source_paths": [path for _, path, _ in documents],
        "source_kinds": [source_kind for source_kind, _, _ in documents],
        "active_source_kind": "canonical_docs",
        "guide_exists": 1,
        "spatial_brief_exists": 1 if SPATIAL_BRIEF_PATH.exists() else 0,
        "hash": digest,
        "sections": len(_guide_sections()),
        "mutable": 0,
    }
