# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "agvm_api"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

from mcp_retrieval import build_mcp_retrieval_tool_output  # noqa: E402
import core_retrieve_router  # noqa: E402
from schemas import RetrieveRequest  # noqa: E402


def test_fresh_public_core_context_snapshot_keeps_plan_ai_contracts() -> None:
    ai_spatial = {
        # Construct the private input marker without shipping it verbatim in
        # the sanitized public release fixture.
        "schema_version": ".".join(("agvm", "ai_spatial_landing_contract", "v1")),
        "status": "materialized",
        "materialized": True,
        "certifiable": True,
        "inverse_answer_paths": [
            {
                "path_id": "P1",
                "answer_field": "product",
                "landing_coordinate": {"x": 0.71, "y": -0.22, "z": 0.13},
            }
        ],
    }
    path_missions = [{"mission_id": "mission_P1", "path_id": "P1"}]
    snapshot = core_retrieve_router._mcp_snapshot_from_context_update(
        search_id="search-fresh-public-snapshot",
        request=RetrieveRequest(
            query_text="What product evidence is ready?",
            retrieval_mode="balanced",
        ),
        plan={
            "semantic_contract": {},
            "ai_spatial_landing_contract": ai_spatial,
            "path_mission_contract": {
                "schema_version": "agvm.path_mission_contract.v1",
                "path_missions": path_missions,
            },
            "path_missions": path_missions,
            "landing_metadata": [
                {
                    "landing_id": "landing-ai-P1",
                    "planner_family": "ai",
                    "landing_position": {"x": 0.71, "y": -0.22, "z": 0.13},
                }
            ],
            "planner_runtime": {},
        },
        event_payload={
            "context": {
                "context_summary": "Product evidence is ready.",
                "context_fragments": [],
                "structured_sections": [],
            },
            "matches": [
                {"node_id": "node_product", "summary": "Product evidence is ready."}
            ],
        },
    )

    assert snapshot["ai_spatial_landing_contract"] == ai_spatial
    assert snapshot["path_missions"] == path_missions
    assert snapshot["landing_metadata"][0]["planner_family"] == "ai"
    materialization = snapshot["context_package_materialization"]
    assert materialization["terminal"] is False
    assert materialization["terminal_for_mcp_client"] is False
    assert materialization["final_materialization_pending"] is True


def test_fresh_public_core_compaction_keeps_ai_truth_and_client_terminality() -> None:
    heuristic_nodes = [
        {
            "id": f"heuristic::{index}",
            "kind": "visited",
            "origin_kind": "heuristic",
            "coordinate": [0.0, 0.0, 0.0],
        }
        for index in range(64)
    ]
    ai_node = {
        "id": "landing::ai::P1",
        "kind": "landing",
        "roles": ["landing_origin", "ai_landing_origin"],
        "semantic_area": "product",
        "coordinate": [0.71, -0.22, 0.13],
        "origin_kind": "ai",
        "source": "ai_spatial_landing_contract.inverse_answer_paths",
    }
    output = build_mcp_retrieval_tool_output(
        "retrieve_context",
        {
            "search_id": "search-fresh-public-core",
            "query_text": "What evidence defines the product?",
            "status": "completed",
            "response_mode": "context",
            "retrieval_mode": "balanced",
            "document_text_policy": "refs_only",
            "context_package": {
                "schema_version": "agvm.mcp_context_package.v2",
                "status": "contract_satisfied",
                "agent_markdown": "# Context\n\nCoordinate-first evidence is ready.",
                "contract": {"passed": True, "unresolved_sections": []},
            },
            "context_package_materialization": {
                "state": "finalized",
                "contract_passed": True,
                "final_materialization_pending": False,
            },
            "matches": [
                {
                    "node_id": "node_product",
                    "summary": "Coordinate-first evidence is ready.",
                }
            ],
            "semantic_contract_runtime": {
                "enabled": True,
                "ai_required": True,
                "status": "completed",
                "source": "llm",
                "material": True,
                "provider_state": "fresh_llm_contract",
            },
            "ai_landing_materialization": {
                "required": True,
                "materialized": True,
                "route_level_materialized": True,
                "ai_landing_count": 1,
            },
            "ai_materialization_hard_gate": {
                "required": True,
                "satisfied": True,
                "blocked": False,
            },
            "run_projection_truth": {
                "schema_version": "agvm.run_projection_truth.v1",
                "search_id": "search-fresh-public-core",
                "status": "finalized",
                "summary": {"ai_landings": 1, "planned_paths": 0, "route_edges": 0},
                "nodes": [*heuristic_nodes, ai_node],
                "edges": [],
                "paths": [],
                "events": [
                    {
                        "index": 1,
                        "type": "ai_landing_materialized",
                        "node_id": "landing::ai::P1",
                        "origin_kind": "ai",
                    }
                ],
            },
            "closure_state": "final_sealed",
            "final_closure_ready": True,
            "final_materialization_pending": False,
            "result_ready_terminal": True,
            "stop_reason": "final_sealed",
        },
        include_raw_text=False,
    )

    resilience = output["ai_materialization_resilience_contract"]
    assert resilience["ai_required"] is True
    assert resilience["ai_materialized"] is True
    assert resilience["materialization_source"] == "fresh_llm"
    assert resilience["heuristic_only_certification_allowed"] is False
    assert resilience["silent_heuristic_completion_allowed"] is False
    critical_path = output["ai_critical_path_contract"]
    assert critical_path["critical_path_role"] == "certification_gate"
    assert critical_path["ai_required"] is True
    assert critical_path["compact_contract_required"] is True
    assert "counts" in critical_path
    route_arbitration = output["route_arbitration_contract"]
    assert route_arbitration["ai_required"] is True
    assert route_arbitration["heuristic_can_certify"] is False
    assert "candidate_families" in route_arbitration
    assert "path_budget" in route_arbitration
    projected_ai = next(
        node
        for node in output["run_projection_truth"]["nodes"]
        if node.get("origin_kind") == "ai"
    )
    assert projected_ai["coordinate"] == [0.71, -0.22, 0.13]
    projection_stream = output["run_projection_event_stream_contract"]
    assert projection_stream["terminal"] is True
    assert projection_stream["client_terminal"] is True
    assert projection_stream["terminal_for_client"] is True
    assert projection_stream["final_materialization_pending"] is False


def test_fresh_public_core_first_package_is_not_projected_as_client_terminal() -> None:
    output = build_mcp_retrieval_tool_output(
        "retrieve_context",
        {
            "search_id": "search-fresh-first-package",
            "query_text": "What evidence is ready?",
            "status": "running",
            "response_mode": "context",
            "retrieval_mode": "balanced",
            "document_text_policy": "refs_only",
            "context_package": {
                "schema_version": "agvm.mcp_context_package.v2",
                "status": "contract_satisfied",
                "agent_markdown": "# Context\n\nA useful package is ready while traversal continues.",
                "contract": {"passed": True, "unresolved_sections": []},
            },
            "context_package_materialization": {
                "state": "first_useful_package_ready",
                "contract_passed": True,
                "final_materialization_pending": True,
            },
            "matches": [
                {"node_id": "node_partial", "summary": "A useful package is ready."}
            ],
            "result_materialization_state": "first_package_ready_background_running",
            "final_materialization_pending": True,
            "result_ready_terminal": False,
            "closure_state": "open",
            "final_closure_ready": False,
        },
        include_raw_text=False,
    )

    assert output["mcp_delivery_contract"]["terminal_for_client"] is False
    projection_stream = output["run_projection_event_stream_contract"]
    assert projection_stream["terminal"] is False
    assert projection_stream["client_terminal"] is False
    assert projection_stream["terminal_for_client"] is False
    assert projection_stream["final_materialization_pending"] is True
