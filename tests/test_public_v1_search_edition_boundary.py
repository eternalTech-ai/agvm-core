# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "agvm_api"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

import retrieval  # noqa: E402
from mcp_retrieval import build_mcp_retrieval_tool_output  # noqa: E402


def _landing_builder() -> Callable[..., dict[str, Any]]:
    source_path = (
        ROOT / "public-core-docs" / "backend-src" / "public_v1_landing_contract.py"
    )
    if not source_path.is_file():
        source_path = API_DIR / "public_v1_landing_contract.py"
    if not source_path.is_file():
        from public_v1_landing_contract import build_public_v1_landing_contract

        return build_public_v1_landing_contract
    spec = importlib.util.spec_from_file_location(
        "test_public_v1_search_landing_contract",
        source_path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_public_v1_landing_contract


def _spatial_brief() -> dict[str, Any]:
    return {
        "schema_version": "agvm.metamemory_spatial_brief.v1",
        "revision": "spatial:public-e2e",
        "source_snapshot_version": "metamemory:public-e2e",
        "source_hash": "public-e2e",
        "brain_revision": "brain:public-e2e",
        "coordinate_system": {"bounds": [-1.0, 1.0]},
        "atlas_summary": {
            "bucket_count": 1,
            "node_count": 3,
            "sample_buckets": [
                {
                    "bucket_key": "project-evidence",
                    "centroid": {"x": 0.1, "y": 0.0, "z": -0.2},
                    "node_count": 3,
                    "highway_gateway": True,
                }
            ],
        },
        "topology_overlay_summary": {
            "overlay_present": True,
            "density_lobes": [],
            "active_highways": [],
            "bridge_corridors": [],
        },
        "spatial_readiness_contract": {
            "status": "ready",
            "certifiable": True,
            "missing_reasons": [],
            "stale_reasons": [],
        },
    }


def _public_spatial_contract() -> dict[str, Any]:
    def provider(**_request: Any) -> tuple[dict[str, Any], None]:
        return {
            "planner_summary": "Land on reviewed project evidence.",
            "inverse_answer_paths": [
                {
                    "path_id": "path-projects",
                    "mission_id": "mission-projects",
                    "strand_id": "strand-projects",
                    "answer_field": "projects",
                    "answer_hypothesis": "Reviewed anchors support the project claim.",
                    "goal": "reviewed project evidence",
                    "routing_intent": "Scan the evidence sphere and follow provenance.",
                    "confidence": 0.92,
                    "destinations": [
                        {
                            "destination_id": "project-evidence-landing",
                            "label": "reviewed project evidence",
                            "reason": "The atlas exposes the reviewed evidence neighborhood.",
                            "routing_intent": "Sphere scan, then local and evidence edges.",
                            "expected_discovery": "Source-backed project claims.",
                            "hydration_policy": "Hydrate promoted source anchors only.",
                            "coordinate": {"x": 0.1, "y": 0.0, "z": -0.2},
                            "radius": 0.18,
                            "execution_role": "primary",
                        }
                    ],
                    "preferred_edges": ["local_link", "highway", "evidence_edge"],
                    "stop_condition": "Stop after reviewed evidence covers the strand.",
                }
            ],
            "uncertainty": "",
        }, None

    return _landing_builder()(
        query_text="Find reviewed project evidence.",
        retrieval_mode="balanced",
        brain_revision="brain:public-e2e",
        semantic_contract={"semantic_authority_v2": True},
        semantic_contract_runtime={"source": "search_ai_admission_materialization"},
        answer_strands=[
            {
                "mission_id": "mission-projects",
                "strand_id": "strand-projects",
                "answer_field": "projects",
                "goal": "reviewed project evidence",
            }
        ],
        metamemory_spatial_brief=_spatial_brief(),
        mode_budget={"max_total_branches": 2},
        allow_ai=True,
        structured_json_fn=provider,
    )


def _semantic_contract() -> dict[str, Any]:
    return {
        "schema_version": "agvm.semantic_context_contract.v1",
        "ai_required": True,
        "landing_plan": {
            "landing_hypotheses": [
                {"landing_id": "landing-projects", "goal": "Find reviewed evidence."}
            ],
            "paths": [{"path_id": "path-projects", "goal": "Reach reviewed evidence."}],
        },
    }


def _semantic_runtime() -> dict[str, Any]:
    return {
        "schema_version": "agvm.semantic_contract_runtime.v1",
        "enabled": True,
        "ai_required": True,
        "status": "completed",
        "source": "llm",
        "material": True,
        "provider_state": "fresh_llm_contract",
    }


def test_public_coordinate_planner_materializes_terminal_mcp_context() -> None:
    spatial_contract = _public_spatial_contract()
    semantic_contract = _semantic_contract()
    semantic_runtime = _semantic_runtime()
    materialization = retrieval.build_ai_landing_materialization_contract(
        query_text="Find reviewed project evidence.",
        response_mode="context",
        semantic_contract=semantic_contract,
        semantic_contract_runtime=semantic_runtime,
        planner_seed_runtime={
            "planner_seed_enabled": True,
            "planner_seed_source": "llm",
            "semantic_contract_ai_seed_used": True,
        },
        llm_scout_state={"enabled": True, "status": "completed"},
        probes=[{"probe_id": "probe-projects", "planner_family": "ai"}],
        branches=[
            {
                "branch_id": "branch-projects",
                "planner_family": "ai",
                "route_trace": [{"travel_performed": True}],
            }
        ],
        path_corridors={
            "paths": [{"path_id": "path-projects"}],
            "metrics": {"path_count": 1, "route_event_count": 1},
        },
        ai_materiality_summary={"material": True},
        ai_validation_gate={"required": True, "final_llm_approval": True},
        final_ai_approval={"approved": True},
        final_surface_fields={"final_closure_ready": True},
        ai_spatial_landing_contract=spatial_contract,
    )
    package_materialization = {
        "state": "ready",
        "contract_passed": True,
        "final_materialization_pending": False,
    }
    hard_gate = retrieval._build_ai_materialization_hard_gate(
        query_class="direct_fact",
        response_mode="context",
        semantic_contract=semantic_contract,
        semantic_contract_runtime=semantic_runtime,
        ai_landing_materialization=materialization,
        ai_validation_gate={"required": True, "final_llm_approval": True},
        final_surface_fields={"final_closure_ready": True},
        context_package_materialization=package_materialization,
        answer_demo_materialization={"requested": False, "state": "not_requested"},
    )

    result = {
        "search_id": "public-coordinate-positive",
        "query_text": "Find reviewed project evidence.",
        "response_mode": "context",
        "retrieval_mode": "balanced",
        "semantic_contract_runtime": semantic_runtime,
        "metamemory_spatial_brief": _spatial_brief(),
        "ai_spatial_landing_contract": spatial_contract,
        "ai_landing_materialization": materialization,
        "ai_materialization_hard_gate": hard_gate,
        "context_package": {
            "schema_version": "agvm.mcp_context_package.v2",
            "status": "context_ready",
            "agent_markdown": "# Context\nReviewed project evidence.",
            "contract": {"passed": True, "unresolved_sections": []},
            "metrics": {"hot_item_count": 1},
            "hot_sections": [
                {"section": "Projects", "items": ["Reviewed project evidence."]}
            ],
        },
        "context_package_materialization": package_materialization,
        "branches": [
            {
                "branch_id": "branch-projects",
                "planner_family": "ai",
                "route_trace": [{"travel_performed": True}],
            }
        ],
        "landing_metadata": [
            {"landing_id": "landing-projects", "planner_family": "ai"}
        ],
        "steps": [{"step_id": "route-projects"}],
        "matches": [{"node_id": "project-1", "summary": "Reviewed evidence."}],
        "visited_node_ids": ["project-1"],
        "visited_bucket_keys": ["project-evidence"],
        "stop_reason": "evidence_contract_satisfied",
        "answerability_state": "grounded",
        "result_materialization_state": "finalized",
        "result_ready_terminal": True,
    }

    assert spatial_contract["status"] == "materialized"
    assert spatial_contract["routing_authority"] == "ai_coordinate_first"
    assert spatial_contract["fallback_used"] is False
    assert materialization["route_level_materialized"] is True
    assert hard_gate["blocked"] is False

    output = build_mcp_retrieval_tool_output("retrieve_context", result)
    delivery = output["mcp_delivery_contract"]

    assert output["status"] == "ok"
    assert delivery["client_payload_state"] == "usable_context", {
        "missing_reasons": delivery["missing_reasons"],
        "context_contract": delivery["context_contract"],
        "metamemory": delivery["metamemory"],
        "ai": delivery["ai"],
        "path_truth": delivery["path_truth"],
    }
    assert delivery["terminal_for_client"] is True
    assert "blocked_no_ai_spatial_material" not in delivery["missing_reasons"]
    assert "optional_ai_landing_planner_not_in_public_core" not in delivery[
        "missing_reasons"
    ]
