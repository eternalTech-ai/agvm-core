# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "public-core-docs" / "backend-src" / "public_v1_landing_contract.py"
if not MODULE_PATH.is_file():
    MODULE_PATH = ROOT / "agvm_api" / "public_v1_landing_contract.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("test_public_v1_landing_contract_module", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _brief() -> dict[str, Any]:
    return {
        "schema_version": "agvm.metamemory_spatial_brief.v1",
        "revision": "spatial:test",
        "source_snapshot_version": "metamemory:test",
        "source_hash": "abc123",
        "brain_revision": "brain:test",
        "coordinate_system": {"bounds": [-1.0, 1.0]},
        "nuclei": {
            "identity": {
                "id": "identity-root",
                "centroid": {"x": 0.0, "y": 0.0, "z": 0.0},
            }
        },
        "atlas_summary": {
            "bucket_count": 1,
            "node_count": 8,
            "sample_buckets": [
                {
                    "bucket_key": "0:0:0",
                    "centroid": {"x": 0.1, "y": 0.0, "z": 0.0},
                    "node_count": 8,
                    "highway_gateway": True,
                }
            ],
        },
        "highway_gateways": [
            {
                "bucket_key": "0:0:0",
                "centroid": {"x": 0.1, "y": 0.0, "z": 0.0},
            }
        ],
    }


def _kwargs() -> dict[str, Any]:
    return {
        "query_text": "Find reviewed recent project evidence.",
        "retrieval_mode": "balanced",
        "brain_revision": "brain:test",
        "semantic_contract": {"semantic_authority_v2": True},
        "semantic_contract_runtime": {"source": "search_ai_admission_materialization"},
        "answer_strands": [
            {
                "mission_id": "mission-projects",
                "strand_id": "strand-projects",
                "answer_field": "projects",
                "goal": "recent reviewed projects",
                "answer_hypothesis": "Reviewed source anchors support recent project claims.",
            }
        ],
        "identity_hints": {},
        "metamemory_spatial_brief": _brief(),
        "mode_budget": {"max_total_branches": 2},
        "allow_ai": True,
    }


def test_public_v1_landing_planner_materializes_ai_coordinate_paths() -> None:
    module = _load_module()
    calls: list[dict[str, Any]] = []

    def provider(**request: Any) -> tuple[dict[str, Any], None]:
        calls.append(dict(request))
        prompt = json.loads(request["user_prompt"])
        assert prompt["execution_contract"]["deterministic_landing_fallback_allowed"] is False
        assert prompt["execution_contract"]["metadata_is_not_routing_authority"] is True
        return {
            "planner_summary": "Land on reviewed project evidence and follow provenance roads.",
            "inverse_answer_paths": [
                {
                    "path_id": "path-projects",
                    "mission_id": "mission-projects",
                    "strand_id": "strand-projects",
                    "answer_field": "projects",
                    "answer_hypothesis": "Reviewed source anchors support recent project claims.",
                    "goal": "recent reviewed projects",
                    "routing_intent": "Land near reviewed project source anchors.",
                    "confidence": 0.9,
                    "destinations": [
                        {
                            "destination_id": "project-evidence-landing",
                            "label": "reviewed project evidence",
                            "reason": "Metamemory locates the reviewed evidence neighborhood here.",
                            "routing_intent": "Scan the local sphere, then follow evidence edges.",
                            "expected_discovery": "Source-backed recent project claims.",
                            "hydration_policy": "Hydrate promoted source anchors only.",
                            "region_ref": None,
                            "coordinate": {"x": 0.1, "y": 0.0, "z": 0.0},
                            "novel_region_candidate": None,
                            "radius": 0.18,
                            "execution_role": "primary",
                        }
                    ],
                    "preferred_edges": ["local_link", "highway", "evidence_edge"],
                    "stop_condition": "Stop when reviewed evidence covers the project strand.",
                }
            ],
            "uncertainty": "",
        }, None

    contract = module.build_public_v1_landing_contract(
        **_kwargs(),
        structured_json_fn=provider,
    )

    assert len(calls) == 1
    assert calls[0]["schema_name"] == "agvm_public_v1_landing_plan_v1"
    assert contract["status"] == "materialized"
    assert contract["materialized"] is True
    assert contract["certifiable"] is True
    assert contract["source"] == "fresh_llm"
    assert contract["routing_authority"] == "ai_coordinate_first"
    assert contract["fallback_used"] is False
    assert contract["heuristic_result_exposed"] is False
    assert contract["missing_reasons"] == []
    assert contract["metrics"]["ai_landing_count"] == 1
    assert contract["metrics"]["ai_path_count"] == 1
    assert contract["inverse_answer_paths"][0]["landing_coordinate"] == {
        "x": 0.1,
        "y": 0.0,
        "z": 0.0,
    }
    assert "optional_ai_landing_planner_not_in_public_core" not in json.dumps(contract)


def test_public_v1_landing_planner_defers_without_calling_provider() -> None:
    module = _load_module()
    provider_called = False

    def provider(**_request: Any) -> tuple[dict[str, Any], None]:
        nonlocal provider_called
        provider_called = True
        return {}, None

    contract = module.build_public_v1_landing_contract(
        **_kwargs(),
        deferred=True,
        structured_json_fn=provider,
    )

    assert provider_called is False
    assert contract["status"] == "deferred"
    assert contract["materialized"] is False
    assert contract["missing_reasons"] == ["ai_spatial_contract_deferred"]
    assert contract["fallback_used"] is False


def test_public_v1_landing_planner_fails_closed_on_provider_error() -> None:
    module = _load_module()

    def provider(**_request: Any) -> tuple[None, str]:
        return None, "insufficient_quota:credit_balance_exhausted"

    contract = module.build_public_v1_landing_contract(
        **_kwargs(),
        structured_json_fn=provider,
    )

    assert contract["status"] == "blocked"
    assert contract["materialized"] is False
    assert contract["certifiable"] is False
    assert contract["inverse_answer_paths"] == []
    assert contract["missing_reasons"] == [
        "ai_spatial_provider_error:insufficient_quota:credit_balance_exhausted"
    ]
    assert contract["fallback_used"] is False
    assert contract["heuristic_result_exposed"] is False


def test_public_v1_landing_planner_blocks_incomplete_strand_coverage() -> None:
    module = _load_module()

    def provider(**_request: Any) -> tuple[dict[str, Any], None]:
        return {
            "planner_summary": "Provider returned a path for the wrong strand.",
            "inverse_answer_paths": [
                {
                    "path_id": "wrong-path",
                    "mission_id": "wrong-mission",
                    "strand_id": "wrong-strand",
                    "answer_field": "other",
                    "answer_hypothesis": "Other evidence.",
                    "goal": "other",
                    "routing_intent": "Other region.",
                    "confidence": 0.7,
                    "destinations": [
                        {
                            "destination_id": "wrong-destination",
                            "label": "other",
                            "reason": "Wrong mission.",
                            "routing_intent": "Other region.",
                            "expected_discovery": "Other evidence.",
                            "hydration_policy": "Hydrate promoted evidence.",
                            "region_ref": "other:region",
                            "coordinate": None,
                            "novel_region_candidate": None,
                            "radius": 0.2,
                            "execution_role": "primary",
                        }
                    ],
                    "preferred_edges": ["local_link"],
                    "stop_condition": "Stop after evidence.",
                }
            ],
            "uncertainty": "",
        }, None

    contract = module.build_public_v1_landing_contract(
        **_kwargs(),
        structured_json_fn=provider,
    )

    assert contract["status"] == "blocked"
    assert contract["materialized"] is False
    assert contract["certifiable"] is False
    assert contract["missing_reasons"] == [
        "answer_strand_paths_missing:mission-projects"
    ]
