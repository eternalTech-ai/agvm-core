from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from typing import Any


RUN_PROJECTION_TRUTH_SCHEMA_VERSION = "agvm.run_projection_truth.v1"
RUN_PROJECTION_TRUTH_SLICE = "PR-12P-14R-A"

_NODE_ID_RE = re.compile(r"\b(?:vec_node|node|doc|source)_[A-Za-z0-9_-]+\b")


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _string(value: Any, fallback: str = "") -> str:
    text = str(value if value is not None else "").strip()
    return text or fallback


def _string_list(value: Any) -> list[str]:
    return [_string(item) for item in _as_list(value) if _string(item)]


def _stable_unit(seed: str, salt: str) -> float:
    digest = hashlib.sha1(f"{seed}:{salt}".encode("utf-8")).hexdigest()
    value = int(digest[:8], 16) / 0xFFFFFFFF
    return round((value * 2.0) - 1.0, 6)


def _coordinate(seed: str, raw_position: dict[str, Any] | None = None) -> list[float]:
    raw = _as_dict(raw_position)
    for keys in (("x", "y", "z"), ("radial", "angle", "depth")):
        if all(key in raw for key in keys):
            try:
                return [round(float(raw[key]), 6) for key in keys]
            except (TypeError, ValueError):
                break
    return [_stable_unit(seed, "x"), _stable_unit(seed, "y"), _stable_unit(seed, "z")]


def _node_ids_from_text(value: Any) -> list[str]:
    return sorted(set(_NODE_ID_RE.findall(str(value or ""))))


def _short_tooltip(*parts: Any, fallback: str) -> str:
    text = " | ".join(_string(part) for part in parts if _string(part))
    text = text or fallback
    return text[:220]


def _status_from_result(result: dict[str, Any], explicit_status: str | None = None) -> str:
    status = _string(explicit_status or result.get("status") or result.get("terminal_state")).lower()
    blockers = _as_list(result.get("final_closure_blockers"))
    provider = _as_dict(result.get("semantic_contract_runtime")).get("provider_state") or _as_dict(
        result.get("ai_landing_materialization")
    ).get("provider_state")
    if status in {"failed", "error"}:
        return "failed"
    if status == "blocked" or blockers:
        return "blocked"
    if _string(provider).lower() == "provider_degraded":
        return "partial"
    if status in {"running", "streaming", "open", "pending"}:
        return "streaming"
    if status in {"partial"}:
        return "partial"
    return "finalized" if result else "partial"


def build_run_projection_truth(
    result: dict[str, Any] | None,
    *,
    search_id: str | None = None,
    brain_id: str | None = None,
    thread_id: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Normalize retrieval/map/path/package truth into the living-brain projection contract."""

    source = deepcopy(_as_dict(result))
    planner_runtime = _as_dict(source.get("planner_runtime"))
    map_truth = _as_dict(source.get("search_map_2d_truth") or planner_runtime.get("search_map_2d_truth"))
    context_package = _as_dict(source.get("context_package"))
    document_workspace = _as_dict(source.get("document_workspace"))
    path_corridors = _as_dict(source.get("path_corridors"))
    ai_materialization = _as_dict(source.get("ai_landing_materialization"))

    resolved_search_id = search_id or _string(source.get("search_id") or map_truth.get("search_id")) or None
    resolved_brain_id = brain_id or _string(source.get("brain_id")) or None
    resolved_thread_id = thread_id or _string(source.get("thread_id") or map_truth.get("thread_id")) or None

    nodes_by_key: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    edge_keys: set[str] = set()
    paths: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    event_index = 0

    def add_event(event_type: str, **payload: Any) -> int:
        nonlocal event_index
        event_index += 1
        events.append({"index": event_index, "time_ms": None, "type": event_type, **payload})
        return event_index

    def add_node(
        node_id: str,
        *,
        kind: str,
        role: str | None = None,
        semantic_area: str | None = None,
        coordinate_seed: str | None = None,
        coordinate_raw: dict[str, Any] | None = None,
        tooltip: str | None = None,
        package_role: str = "debug_only",
        source_ref: str | None = None,
        confidence: float | None = None,
        origin_kind: str | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        clean_id = _string(node_id)
        if not clean_id:
            clean_id = f"{kind}:{len(nodes_by_key) + 1}"
        key = clean_id
        if key not in nodes_by_key:
            idx = add_event("node_materialized", node_id=clean_id, kind=kind)
            nodes_by_key[key] = {
                "id": clean_id,
                "kind": kind,
                "roles": [],
                "semantic_area": semantic_area or "unknown",
                "coordinate": _coordinate(coordinate_seed or clean_id, coordinate_raw),
                "visible_label": None,
                "tooltip": tooltip or clean_id,
                "package_role": package_role,
                "source_ref": source_ref,
                "confidence": confidence,
                "origin_kind": origin_kind,
                "event_index": idx,
                "source": source,
            }
        node = nodes_by_key[key]
        if role and role not in node["roles"]:
            node["roles"].append(role)
        if kind != node.get("kind") and kind not in node["roles"]:
            node["roles"].append(kind)
        if package_role != "debug_only" and node.get("package_role") == "debug_only":
            node["package_role"] = package_role
        if semantic_area and node.get("semantic_area") in {"unknown", ""}:
            node["semantic_area"] = semantic_area
        if source_ref and not node.get("source_ref"):
            node["source_ref"] = source_ref
        return node

    def normalize_landing_node_id(value: Any) -> str:
        clean = _string(value)
        if not clean:
            return ""
        return clean if clean.startswith("landing::") else f"landing::{clean}"

    def add_route_edge(
        from_id: Any,
        to_id: Any,
        *,
        kind: str = "local",
        state: str = "traversed",
        path_id: str | None = None,
        source: str | None = None,
        strength: float = 1.0,
        edge_id: str | None = None,
    ) -> None:
        clean_from = _string(from_id)
        clean_to = _string(to_id)
        if not clean_from or not clean_to or clean_from == clean_to:
            return
        clean_path = _string(path_id)
        clean_source = _string(source)
        key = f"{clean_from}->{clean_to}:{clean_path}:{clean_source}"
        if key in edge_keys:
            return
        edge_keys.add(key)
        add_node(
            clean_from,
            kind="visited",
            role="route_endpoint",
            coordinate_seed=f"from:{clean_from}",
            source=clean_source or "run_projection.route_edge",
        )
        add_node(
            clean_to,
            kind="visited",
            role="route_endpoint",
            coordinate_seed=f"to:{clean_to}",
            source=clean_source or "run_projection.route_edge",
        )
        resolved_edge_id = _string(edge_id) or f"edge::{len(edges) + 1}"
        evt = add_event("route_traversed", edge_id=resolved_edge_id, from_node_id=clean_from, to_node_id=clean_to)
        edges.append(
            {
                "id": resolved_edge_id,
                "from": clean_from,
                "to": clean_to,
                "kind": _string(kind, "local"),
                "state": _string(state, "traversed"),
                "path_id": clean_path or None,
                "event_index": evt,
                "strength": strength,
                "source": clean_source or "run_projection.route_edge",
            }
        )

    landing_ids_by_branch: dict[str, str] = {}
    for index, landing in enumerate(_as_list(map_truth.get("landings"))):
        if not isinstance(landing, dict):
            continue
        landing_id = _string(landing.get("landing_id") or landing.get("branch_id") or f"landing:{index + 1}")
        branch_id = _string(landing.get("branch_id"))
        if branch_id:
            landing_ids_by_branch[branch_id] = landing_id
        families = {item.lower() for item in _string_list(landing.get("origin_families"))}
        planner_family = _string(landing.get("planner_family")).lower()
        if planner_family:
            families.add(planner_family)
        origin_kind = "ai" if "ai" in families else "dual" if len(families) > 1 else "heuristic"
        add_event("landing_materialized", landing_id=landing_id, branch_id=branch_id or None, origin_kind=origin_kind)
        add_node(
            f"landing::{landing_id}",
            kind="landing",
            role="landing_origin",
            semantic_area=_string(landing.get("expected_answer_field") or landing.get("expected_guide_area"), "landing"),
            coordinate_seed=f"landing:{landing_id}",
            coordinate_raw=_as_dict(landing.get("landing_position")),
            tooltip=_short_tooltip(landing.get("goal"), landing.get("planner_family"), fallback=f"Landing {landing_id}"),
            package_role="debug_only",
            origin_kind=origin_kind,
            source="search_map_2d_truth.landings",
        )

    for index, landing in enumerate(_as_list(path_corridors.get("landings"))):
        if not isinstance(landing, dict):
            continue
        landing_id = _string(landing.get("landing_id") or landing.get("id") or f"path_landing:{index + 1}")
        if not landing_id:
            continue
        branch_id = _string(landing.get("branch_id"))
        if branch_id and branch_id not in landing_ids_by_branch:
            landing_ids_by_branch[branch_id] = landing_id
        add_event("path_corridor_landing_materialized", landing_id=landing_id, branch_id=branch_id or None)
        add_node(
            normalize_landing_node_id(landing_id),
            kind="landing",
            role="path_corridor_landing_origin",
            semantic_area=_string(landing.get("goal") or landing.get("label"), "path_corridor"),
            coordinate_seed=f"path_corridor_landing:{landing_id}",
            coordinate_raw=_as_dict(landing.get("landing_position") or landing.get("coordinate") or landing.get("ai_spatial_landing_coordinate")),
            tooltip=_short_tooltip(landing.get("goal"), landing.get("label"), branch_id, fallback=f"Path landing {landing_id}"),
            package_role="debug_only",
            origin_kind="path_corridor",
            source="path_corridors.landings",
        )

    ai_landing = _as_dict(ai_materialization.get("landing"))
    ai_path = _as_dict(ai_materialization.get("path"))
    semantic_contract = _as_dict(ai_materialization.get("semantic_contract"))
    ai_route_materialized = bool(
        ai_materialization.get("route_level_materialized")
        or ai_materialization.get("materialized")
        or ai_landing.get("materialized")
        or ai_path.get("corridor_materialized")
        or semantic_contract.get("materialized")
    )
    ai_hypothesis_count = int(ai_landing.get("hypothesis_count") or ai_landing.get("probe_count") or 0)
    if not ai_hypothesis_count and ai_route_materialized:
        ai_hypothesis_count = 1
    existing_ai_landings = [
        node
        for node in nodes_by_key.values()
        if node.get("kind") == "landing" and node.get("origin_kind") == "ai"
    ]
    if ai_route_materialized and not existing_ai_landings:
        semantic_area = _string(ai_landing.get("semantic_area") or ai_path.get("semantic_area"), "semantic_ai")
        expected_evidence_count = int(semantic_contract.get("expected_evidence_count") or 0)
        for index in range(max(1, ai_hypothesis_count)):
            ai_landing_id = f"ai_landing:{index + 1}"
            add_event(
                "ai_landing_materialized",
                landing_id=ai_landing_id,
                route_level_materialized=bool(ai_materialization.get("route_level_materialized")),
                corridor_materialized=bool(ai_path.get("corridor_materialized")),
            )
            add_node(
                f"landing::{ai_landing_id}",
                kind="landing",
                role="ai_landing_origin",
                semantic_area=semantic_area,
                coordinate_seed=f"ai_landing:{resolved_search_id or 'run'}:{index + 1}",
                tooltip=_short_tooltip(
                    "AI landing materialized",
                    semantic_contract.get("status"),
                    f"expected evidence {expected_evidence_count}" if expected_evidence_count else "",
                    fallback=f"AI landing {index + 1}",
                ),
                package_role="debug_only",
                origin_kind="ai",
                source="ai_landing_materialization",
            )

    for index, node in enumerate(_as_list(map_truth.get("intermediate_nodes"))):
        if not isinstance(node, dict):
            continue
        node_id = _string(node.get("node_id"))
        if not node_id:
            continue
        node_role = _string(node.get("node_role"), "visited")
        package_role = "hot_only" if node_role == "promoted" else "cold_only" if node_role == "reservoir" else "debug_only"
        add_node(
            node_id,
            kind="promoted" if node_role == "promoted" else "cold" if node_role == "reservoir" else "visited",
            role=node_role,
            semantic_area=_string(node.get("branch_id"), "route"),
            coordinate_seed=f"node:{node_id}:{index}",
            tooltip=_short_tooltip(node_role, node.get("branch_id"), fallback=node_id),
            package_role=package_role,
            source="search_map_2d_truth.intermediate_nodes",
        )

    for segment_index, segment in enumerate(_as_list(map_truth.get("route_segments"))):
        if not isinstance(segment, dict):
            continue
        from_id = _string(segment.get("from_node_id") or segment.get("from_bucket_key") or f"landing::{segment.get('landing_id')}")
        to_id = _string(segment.get("to_node_id") or segment.get("to_bucket_key"))
        if not from_id or not to_id:
            continue
        add_route_edge(
            from_id,
            to_id,
            kind=_string(segment.get("edge_type"), "local"),
            path_id=_string(segment.get("route_id") or segment.get("branch_id")) or None,
            source="search_map_2d_truth.route_segments",
            edge_id=_string(segment.get("segment_id"), f"edge::{segment_index + 1}"),
        )

    for plan_index, plan in enumerate(_as_list(map_truth.get("route_plans"))):
        if not isinstance(plan, dict):
            continue
        route_id = _string(plan.get("route_id") or plan.get("branch_id"), f"route::{plan_index + 1}")
        branch_id = _string(plan.get("branch_id"))
        landing_id = _string(plan.get("landing_id") or landing_ids_by_branch.get(branch_id) or branch_id)
        path_rows = _as_list(plan.get("path_lifecycle"))
        state = _string(plan.get("path_lifecycle_state") or plan.get("route_state") or plan.get("status"), "planned")
        paths.append(
            {
                "path_id": route_id,
                "origin_node_id": f"landing::{landing_id}" if landing_id else None,
                "origin_kind": _string(plan.get("planner_family"), "unknown"),
                "planned_node_ids": [],
                "traversed_node_ids": [
                    _string(seg.get("to_node_id") or seg.get("to_bucket_key"))
                    for seg in _as_list(map_truth.get("route_segments"))
                    if isinstance(seg, dict) and _string(seg.get("route_id")) == route_id
                ],
                "promoted_node_ids": [],
                "status": "completed" if state == "completed" else "blocked" if state in {"blocked", "stopped"} else "planned",
                "reason": _short_tooltip(plan.get("goal"), state, fallback=route_id),
                "lifecycle": path_rows[:4],
                "source": "search_map_2d_truth.route_plans",
            }
        )

    for path in _as_list(path_corridors.get("paths")):
        if not isinstance(path, dict):
            continue
        path_id = _string(path.get("path_id"))
        if not path_id or any(existing.get("path_id") == path_id for existing in paths):
            continue
        lifecycle = _as_dict(path.get("lifecycle"))
        package_impact = _as_dict(path.get("package_impact"))
        origin_node_id = normalize_landing_node_id(path.get("origin_landing_id") or path.get("from_landing_id"))
        cursor = origin_node_id
        emitted_route_edge = False
        for event_index_in_path, event in enumerate(_as_list(path.get("route_events"))):
            if not isinstance(event, dict):
                continue
            move_type = _string(event.get("move_type")).lower()
            edge_type = _string(event.get("edge_type"), "path_corridor").lower()
            explicit_from = _string(event.get("from_node_id") or event.get("source_node_id"))
            explicit_to = _string(event.get("to_node_id") or event.get("target_node_id"))
            if explicit_from:
                cursor = explicit_from
            if not explicit_to or move_type == "destination_reached" or edge_type == "none":
                continue
            add_route_edge(
                explicit_from or cursor,
                explicit_to,
                kind=edge_type,
                path_id=path_id,
                source="path_corridors.route_events",
                edge_id=f"path_corridor::{path_id}::{event_index_in_path + 1}",
            )
            emitted_route_edge = True
            cursor = explicit_to
        if not emitted_route_edge and origin_node_id:
            cursor = origin_node_id
            for node_index, node_id in enumerate(_string_list(path.get("traversed_node_ids"))):
                add_route_edge(
                    cursor,
                    node_id,
                    kind="path_node_sequence",
                    path_id=path_id,
                    source="path_corridors.traversed_node_ids",
                    edge_id=f"path_corridor::{path_id}::traversed::{node_index + 1}",
                )
                cursor = node_id
        paths.append(
            {
                "path_id": path_id,
                "origin_node_id": origin_node_id or None,
                "origin_kind": _string(path.get("route_kind"), "landing_origin_corridor"),
                "planned_node_ids": _string_list(path.get("planned_node_ids")),
                "traversed_node_ids": _string_list(path.get("traversed_node_ids")),
                "promoted_node_ids": _string_list(path.get("promoted_node_ids")),
                "status": _string(path.get("lifecycle_state") or lifecycle.get("state"), "pending"),
                "reason": _string(path.get("lifecycle_state_reason") or lifecycle.get("state_reason")),
                "changed_context_package": bool(path.get("changed_context_package") or package_impact.get("changed_context_package")),
                "source": "path_corridors.paths",
            }
        )

    for section in _as_list(context_package.get("sections")):
        if not isinstance(section, dict):
            continue
        title = _string(section.get("title") or section.get("section_id"), "context")
        for node_id in _node_ids_from_text(section):
            add_node(
                node_id,
                kind="promoted",
                role="context_package_section",
                semantic_area=title,
                tooltip=_short_tooltip(title, section.get("text") or section.get("content"), fallback=node_id),
                package_role="sent",
                source="context_package.sections",
            )
    for node_id in _node_ids_from_text(context_package.get("agent_markdown")):
        add_node(node_id, kind="promoted", role="agent_markdown_ref", package_role="sent", source="context_package.agent_markdown")

    document_refs = _as_list(source.get("document_refs") or context_package.get("document_refs") or document_workspace.get("document_refs"))
    workspace_documents = _as_list(document_workspace.get("documents"))
    for index, document in enumerate([item for item in document_refs + workspace_documents if isinstance(item, dict)]):
        doc_id = _string(
            document.get("document_id")
            or document.get("source_id")
            or document.get("anchor_node_id")
            or document.get("document_anchor_node_id")
            or f"document:{index + 1}"
        )
        anchor_id = _string(document.get("anchor_node_id") or document.get("document_anchor_node_id") or doc_id)
        add_node(
            anchor_id,
            kind="document_anchor",
            role="document_ref",
            semantic_area="documents",
            coordinate_seed=f"document:{doc_id}",
            tooltip=_short_tooltip(document.get("title"), document.get("source_label"), fallback=doc_id),
            package_role="doc_ref",
            source_ref=doc_id,
            source="document_workspace/document_refs",
        )

    metrics = _as_dict(map_truth.get("metrics"))
    ai_landings = sum(1 for node in nodes_by_key.values() if node.get("kind") == "landing" and node.get("origin_kind") == "ai")
    if not ai_landings:
        ai_landings = int(ai_materialization.get("ai_probe_count") or ai_materialization.get("ai_landing_count") or 0)
    heuristic_probes = sum(1 for node in nodes_by_key.values() if node.get("kind") == "landing" and node.get("origin_kind") == "heuristic")
    summary = {
        "ai_landings": ai_landings,
        "heuristic_probes": heuristic_probes,
        "dual_landings": sum(1 for node in nodes_by_key.values() if node.get("kind") == "landing" and node.get("origin_kind") == "dual"),
        "planned_paths": len(paths),
        "traversed_paths": sum(1 for item in paths if item.get("traversed_node_ids")),
        "promoted_nodes": sum(1 for node in nodes_by_key.values() if "promoted" in node.get("roles", []) or node.get("kind") == "promoted"),
        "hot_nodes": sum(1 for node in nodes_by_key.values() if node.get("package_role") in {"sent", "hot_only"}),
        "cold_nodes": sum(1 for node in nodes_by_key.values() if node.get("package_role") == "cold_only"),
        "document_refs": sum(1 for node in nodes_by_key.values() if node.get("kind") == "document_anchor"),
        "route_edges": len(edges),
        "blocked_events": sum(1 for item in paths if item.get("status") == "blocked"),
        "source_landing_count": int(metrics.get("landing_count") or 0),
        "source_route_segment_count": int(metrics.get("route_segment_count") or 0),
    }

    return {
        "schema": RUN_PROJECTION_TRUTH_SCHEMA_VERSION,
        "schema_version": RUN_PROJECTION_TRUTH_SCHEMA_VERSION,
        "slice": RUN_PROJECTION_TRUTH_SLICE,
        "search_id": resolved_search_id,
        "brain_id": resolved_brain_id,
        "thread_id": resolved_thread_id,
        "status": _status_from_result(source, status),
        "coordinate_space": "agvm.run_projection_truth.normalized_from_search_map_2d_v1",
        "nodes": list(nodes_by_key.values()),
        "edges": edges,
        "paths": paths,
        "events": events,
        "summary": summary,
        "invariants": {
            "labels_are_hover_only": True,
            "render_selected_run_only": True,
            "synthetic_motion_allowed": False,
            "css_perpetual_motion_allowed": False,
            "can_replay_after_refresh": bool(resolved_search_id),
            "source_truth_required": True,
        },
        "source_trace": [
            {"source": "search_map_2d_truth", "present": bool(map_truth), "metrics": metrics},
            {"source": "path_corridors", "present": bool(path_corridors), "path_count": len(_as_list(path_corridors.get("paths")))},
            {"source": "context_package", "present": bool(context_package), "agent_markdown_chars": len(_string(context_package.get("agent_markdown")))},
            {"source": "document_workspace", "present": bool(document_workspace), "document_count": len(workspace_documents)},
        ],
    }
