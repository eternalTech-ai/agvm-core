# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import importlib
import sys
import threading
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "agvm_api"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

import core_retrieve_router  # noqa: E402
import retrieval  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from mcp_retrieval import build_mcp_retrieval_tool_output  # noqa: E402
from runtime_scope import use_runtime_brain  # noqa: E402
from schemas import McpRetrievalToolRequest, RetrieveRequest, RetrieveResponse  # noqa: E402


def _attestation() -> dict[str, Any]:
    return {
        "schema_version": "agvm.ai_execution_attestation.v2",
        "status": "completed",
        "provider_executed": True,
        "applicable": True,
        "legacy_read_only": False,
        "provider": "openai_compatible",
        "model": "gpt-4.1-mini",
        "request_sha256": "a" * 64,
        "output_sha256": "b" * 64,
        "usage": {
            "input_tokens": 42,
            "output_tokens": 21,
            "reasoning_tokens": 3,
            "total_tokens": 63,
        },
    }


def _planner_payload() -> dict[str, Any]:
    return {
        "answer_strands": [
            {
                "answer_field": "history",
                "answer_hypothesis": "The requested product history is stored in reviewed memory.",
                "goal": "history",
                "landing_hint": "Reviewed product history",
                "priority": 0.91,
                "destination_queue": [],
            }
        ],
        "seed_summary": "One grounded history strand.",
        "seed_query_class": "direct_fact",
    }


def _admission() -> dict[str, Any]:
    planner_payload = _planner_payload()
    answer_strands = [
        {
            **dict(item),
            "planner_family": "ai",
            "family_plan_id": "ai_family_plan",
        }
        for item in planner_payload["answer_strands"]
    ]
    return {
        "schema_version": "agvm.search_ai_admission.v2",
        "status": "admitted",
        "reason": "provider_plan_attested",
        "provider_error": None,
        "chargeable": True,
        "charged_units": 0,
        "planner_seed_payload": planner_payload,
        "answer_strands": answer_strands,
        "ai_execution_attestation": _attestation(),
    }


def _attested_runtime_plan() -> dict[str, Any]:
    admission = _admission()
    ai_strand = dict(admission["answer_strands"][0])
    ai_probe = {
        "probe_id": "probe-ai-1",
        "planner_family": "ai",
        "goal": ai_strand["goal"],
        "answer_field": ai_strand["answer_field"],
        "answer_hypothesis": ai_strand["answer_hypothesis"],
    }
    ai_branch = {
        "branch_id": "branch-ai-1",
        "planner_family": "ai",
        "goal": ai_strand["goal"],
        "answer_field": ai_strand["answer_field"],
        "answer_hypothesis": ai_strand["answer_hypothesis"],
    }
    return {
        "search_ai_admission": admission,
        "ai_execution_attestation": dict(admission["ai_execution_attestation"]),
        "answer_strands": [ai_strand],
        "probes": [ai_probe],
        "branches": [ai_branch],
        "planner_runtime": {
            "ai_execution_attested": True,
            "heuristic_fallback_used": False,
        },
    }


def _runtime_ai_call(call_name: str) -> dict[str, Any]:
    return retrieval._run_attested_search_ai_json(
        call_name=call_name,
        model="gpt-4.1-mini",
        system_prompt="Return the requested Search control payload.",
        user_prompt="Search control input.",
        schema_name=f"test_{call_name}",
        schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
        timeout=1.0,
        role="retrieval",
    )


def _attestation_for_call(index: int) -> dict[str, Any]:
    value = _attestation()
    request_digit = format((index % 14) + 1, "x")
    output_digit = format(((index + 7) % 14) + 1, "x")
    value["request_sha256"] = request_digit * 64
    value["output_sha256"] = output_digit * 64
    return value


def test_internal_plan_boundary_rejects_missing_admission_before_planning(monkeypatch) -> None:
    monkeypatch.setattr(
        retrieval,
        "build_identity_context",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("planning must not start")
        ),
    )

    with pytest.raises(retrieval.AiModuleContractError) as exc_info:
        retrieval.prepare_runtime_plan(
            RetrieveRequest(query_text="Find reviewed context."),
            {"buckets": []},
            {"core_nodes": []},
        )

    assert exc_info.value.code == "search_ai_admission_required"


@pytest.mark.parametrize(
    ("admission", "expected_code"),
    [
        (
            {
                **_admission(),
                "schema_version": "agvm.search_ai_admission.v1",
            },
            "search_ai_admission_schema_invalid",
        ),
        (
            {
                **_admission(),
                "status": "blocked",
                "reason": "blocked_ai_provider_unavailable",
            },
            "search_ai_admission_not_admitted",
        ),
        (
            {
                **_admission(),
                "ai_execution_attestation": {},
            },
            "ai_execution_attestation_schema_invalid",
        ),
    ],
)
def test_internal_plan_boundary_rejects_invalid_or_unattested_admission(
    monkeypatch,
    admission: dict[str, Any],
    expected_code: str,
) -> None:
    monkeypatch.setattr(
        retrieval,
        "build_identity_context",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("planning must not start")
        ),
    )

    with pytest.raises(retrieval.AiModuleContractError) as exc_info:
        retrieval.prepare_runtime_plan(
            RetrieveRequest(query_text="Find reviewed context."),
            {"buckets": []},
            {"core_nodes": []},
            ai_admission=admission,
        )

    assert exc_info.value.code == expected_code


def test_internal_plan_boundary_preserves_valid_admitted_path() -> None:
    plan = retrieval.prepare_runtime_plan(
        RetrieveRequest(query_text="Find reviewed context."),
        {"buckets": []},
        {"core_nodes": []},
        defer_planner_seed=True,
        ai_admission=_admission(),
    )

    assert plan["planner_runtime"]["planner_path"] == "ai_attested"
    assert plan["planner_runtime"]["ai_execution_attested"] is True
    assert plan["ai_execution_attestation"] == _attestation()
    assert all(item["planner_family"] == "ai" for item in plan["answer_strands"])
    assert all("ai" in item["origin_families"] for item in plan["probes"])
    assert all("ai" in item["origin_families"] for item in plan["branches"])


def test_semantic_scaffold_does_not_make_or_impersonate_a_second_ai_call() -> None:
    assert not hasattr(retrieval, "compile_semantic_query_contract")
    plan = retrieval.prepare_runtime_plan(
        RetrieveRequest(query_text="Find reviewed context."),
        {"buckets": []},
        {"core_nodes": []},
        defer_planner_seed=True,
        ai_admission=_admission(),
    )

    runtime = plan["semantic_contract_runtime"]
    assert runtime["provider_call_performed"] is False
    assert runtime["origin_call_name"] == "planner_seed_admission"
    assert "ai_execution_attestation" not in runtime


def test_admitted_flash_query_plan_defers_ai_spatial_preflight_without_provider_call(
    monkeypatch,
) -> None:
    original_spatial_builder = retrieval.build_public_v1_landing_contract
    builder_calls: list[dict[str, Any]] = []
    provider_called = False

    def forbidden_ai_spatial_provider(_call_name: str):
        def provider(**_kwargs: Any) -> tuple[dict[str, Any], None]:
            nonlocal provider_called
            provider_called = True
            raise AssertionError("query-plan preflight must not call AI spatial provider")

        return provider

    def no_cache_spatial_builder(**kwargs: Any) -> dict[str, Any]:
        builder_calls.append(dict(kwargs))
        return original_spatial_builder(**kwargs, use_cache=False)

    monkeypatch.setattr(retrieval, "llm_enabled", lambda: True)
    monkeypatch.setattr(retrieval, "_attested_search_ai_provider", forbidden_ai_spatial_provider)
    monkeypatch.setattr(retrieval, "build_public_v1_landing_contract", no_cache_spatial_builder)

    plan = retrieval.prepare_runtime_plan(
        RetrieveRequest(query_text="Find reviewed context.", retrieval_mode="flash"),
        {"buckets": []},
        {"core_nodes": []},
        defer_planner_seed=True,
        ai_admission=_admission(),
    )

    spatial = dict(plan.get("ai_spatial_landing_contract") or {})
    spatial_runtime = dict(plan.get("ai_spatial_landing_contract_runtime") or {})
    assert len(builder_calls) == 1
    assert builder_calls[0]["deferred"] is True
    assert builder_calls[0]["allow_ai"] is True
    assert builder_calls[0]["mode_budget"]["source"] == "preflight_initial_plan"
    assert builder_calls[0]["mode_budget"]["cache_only"] is True
    assert provider_called is False
    assert spatial["source"] == "deferred"
    assert spatial["materialized"] is False
    assert spatial["certifiable"] is False
    assert "ai_spatial_contract_deferred" in spatial["missing_reasons"]
    assert spatial_runtime["source"] == "deferred"
    assert spatial_runtime["materialized"] is False
    assert plan["planner_runtime"]["planner_path"] == "ai_attested"
    assert plan["planner_seed_runtime"]["planner_seed_source"] == "llm"
    assert all(item["planner_family"] == "ai" for item in plan["answer_strands"])


def test_query_run_materializes_deferred_ai_spatial_before_route(monkeypatch) -> None:
    class StopAfterRuntimeSpatial(Exception):
        pass

    spatial_calls: list[dict[str, Any]] = []
    events: list[tuple[str, dict[str, Any]]] = []

    def spatial_builder(**kwargs: Any) -> dict[str, Any]:
        spatial_calls.append(dict(kwargs))
        if kwargs["deferred"]:
            return {
                "schema_version": "agvm.public_v1_landing_landing_contract.v1",
                "status": "blocked",
                "source": "deferred",
                "materialized": False,
                "certifiable": False,
                "missing_reasons": [
                    "inverse_answer_paths_missing",
                    "ai_spatial_contract_deferred",
                ],
                "inverse_answer_paths": [],
                "metrics": {"ai_landing_count": 0, "ai_path_count": 0},
            }
        strand = dict((kwargs.get("answer_strands") or [{}])[0])
        mission_id = str(strand.get("mission_id") or strand.get("strand_id") or "strand_ai_1")
        return {
            "schema_version": "agvm.public_v1_landing_landing_contract.v1",
            "status": "materialized",
            "source": "fresh_llm",
            "materialized": True,
            "certifiable": True,
            "missing_reasons": [],
            "inverse_answer_paths": [
                {
                    "path_id": "path-ai-spatial-1",
                    "mission_id": mission_id,
                    "strand_id": str(strand.get("strand_id") or mission_id),
                    "answer_field": str(strand.get("answer_field") or "history"),
                    "answer_hypothesis": str(
                        strand.get("answer_hypothesis")
                        or "Reviewed context is stored in memory."
                    ),
                    "goal": str(strand.get("goal") or "history"),
                    "routing_intent": "land on reviewed context before traversal",
                    "confidence": 0.91,
                    "landing_coordinate": {"x": 0.0, "y": 0.0, "z": 0.0},
                    "radius": 0.2,
                    "destinations": [
                        {
                            "coordinate": {"x": 0.0, "y": 0.0, "z": 0.0},
                            "routing_intent": "land on reviewed context before traversal",
                            "reason": "AI selected the semantic region for this strand.",
                            "radius": 0.2,
                            "execution_role": "primary",
                        }
                    ],
                }
            ],
            "metrics": {"ai_landing_count": 1, "ai_path_count": 1},
        }

    def stop_before_route(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise StopAfterRuntimeSpatial()

    monkeypatch.setattr(retrieval, "llm_enabled", lambda: False)
    monkeypatch.setattr(retrieval, "build_public_v1_landing_contract", spatial_builder)
    monkeypatch.setattr(retrieval, "_master_loop_executable_work", stop_before_route)

    query = RetrieveRequest(query_text="Find reviewed context.", retrieval_mode="flash")
    plan = retrieval.prepare_runtime_plan(
        query,
        {"buckets": []},
        {"core_nodes": []},
        defer_planner_seed=True,
        ai_admission=_admission(),
    )
    assert spatial_calls[0]["deferred"] is True
    assert dict(plan["ai_spatial_landing_contract"])["source"] == "deferred"

    with pytest.raises(StopAfterRuntimeSpatial):
        retrieval.retrieve_runtime(
            query,
            {"buckets": []},
            {"core_nodes": []},
            prepared_plan=plan,
            search_id="search-runtime-spatial",
            event_callback=lambda event_type, payload: events.append((event_type, dict(payload))),
        )

    assert [call["deferred"] for call in spatial_calls] == [True, False]
    assert spatial_calls[1]["allow_ai"] is True
    assert spatial_calls[1]["mode_budget"]["bounded_route_single_shot"] is True
    assert spatial_calls[1]["mode_budget"]["source"] == "preflight_before_route"
    event_types = [event_type for event_type, _ in events]
    started_index = event_types.index("ai_spatial_materialization_started")
    completed_index = event_types.index("ai_spatial_materialization_completed")
    assert started_index < completed_index


def test_runtime_ai_spatial_uses_configurable_navigation_stage_budget(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "AGVM_SEARCH_AI_NAVIGATION_BALANCED_TIMEOUT_SECONDS",
        "47",
    )

    assert retrieval._runtime_ai_spatial_contract_timeout_seconds("balanced") == 47.0


def test_quick_search_ai_timeouts_are_configurable_and_not_micro_budgets(
    monkeypatch,
) -> None:
    monkeypatch.delenv("AGVM_SEARCH_AI_PLANNER_FLASH_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("AGVM_SEARCH_AI_NAVIGATION_FLASH_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("AGVM_SEARCH_AI_MASTER_JUDGE_FLASH_TIMEOUT_SECONDS", raising=False)

    assert retrieval._search_ai_stage_timeout_seconds("planner", "flash") == 60.0
    assert retrieval._runtime_ai_spatial_contract_timeout_seconds("flash") == 60.0
    assert retrieval._search_ai_stage_timeout_seconds("master_judge", "flash") == 60.0

    monkeypatch.setenv("AGVM_SEARCH_AI_PLANNER_FLASH_TIMEOUT_SECONDS", "73")
    monkeypatch.setenv("AGVM_SEARCH_AI_NAVIGATION_FLASH_TIMEOUT_SECONDS", "81")
    monkeypatch.setenv("AGVM_SEARCH_AI_MASTER_JUDGE_FLASH_TIMEOUT_SECONDS", "79")

    assert retrieval._search_ai_stage_timeout_seconds("planner", "flash") == 73.0
    assert retrieval._runtime_ai_spatial_contract_timeout_seconds("flash") == 81.0
    assert retrieval._search_ai_stage_timeout_seconds("master_judge", "flash") == 79.0


def test_search_ai_stage_timeout_never_outlives_global_deadline(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_structured_json(**kwargs: Any):
        captured.update(kwargs)
        kwargs["execution_metadata"].update(_attestation())
        return {"ok": True}, None

    monkeypatch.setattr(retrieval, "structured_json", fake_structured_json)
    monkeypatch.setattr(retrieval, "llm_enabled", lambda: True)
    token = retrieval._SEARCH_AI_DEADLINE.set(retrieval.time.monotonic() + 1.25)
    try:
        payload = retrieval._run_attested_search_ai_json(
            call_name="ai_spatial_landing:test",
            model="gpt-4o",
            system_prompt="Plan one grounded landing.",
            user_prompt="Find the requested identity.",
            schema_name="deadline_probe",
            schema={"type": "object"},
            timeout=73.0,
            role="ai_spatial",
        )
    finally:
        retrieval._SEARCH_AI_DEADLINE.reset(token)

    assert payload == {"ok": True}
    assert 0.1 < float(captured["timeout"]) <= 1.25


def test_runtime_provider_absent_fails_closed_without_material_or_charge(monkeypatch) -> None:
    monkeypatch.setattr(retrieval, "llm_enabled", lambda: False)
    monkeypatch.setattr(
        retrieval,
        "_retrieve_runtime_attested_impl",
        lambda *args, **kwargs: _runtime_ai_call("navigation_actions"),
    )

    result = retrieval.retrieve_runtime(
        RetrieveRequest(query_text="Find reviewed context."),
        {"buckets": []},
        {"core_nodes": []},
        ai_admission=_admission(),
    )

    assert result["stop_reason"] == "blocked_ai_provider_unavailable"
    assert result["matches"] == []
    assert result["context_package"] == {}
    assert result["billing"]["chargeable"] is False
    assert result["billing"]["charged_units"] == 0
    assert result["search_ai_execution"]["failed_call"] == "navigation_actions"


def test_second_ai_call_failure_preserves_useful_package_for_review(monkeypatch) -> None:
    monkeypatch.setattr(retrieval, "llm_enabled", lambda: True)
    provider_calls = 0

    def provider(*args, execution_metadata: dict[str, Any], **kwargs):
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls == 2:
            return None, "transport_error:timed out"
        execution_metadata.update(_attestation_for_call(provider_calls))
        return {"ok": True}, None

    def runtime_impl(*args, event_callback=None, **kwargs):
        _runtime_ai_call("branch_controller")
        if event_callback:
            event_callback(
                "context_update",
                {
                    "matches": [{"node_id": "useful-node"}],
                    "visited_node_ids": ["useful-node", "visited-node"],
                    "context_package": {
                        "status": "usable",
                        "sections": [{"key": "facts", "items": [{"node_id": "useful-node"}]}],
                        "contract": {"passed": True},
                    },
                    "mission_evidence_ledger": {
                        "rows": [
                            {
                                "mission_id": "mission-resolved",
                                "coverage_state": "resolved",
                                "goal": "Resolved criterion",
                            },
                            {
                                "mission_id": "mission-open",
                                "branch_id": "branch-open",
                                "coverage_state": "missed",
                                "coverage_reason": "supporting evidence still required",
                                "goal": "Unresolved criterion",
                            },
                        ]
                    },
                },
            )
        _runtime_ai_call("navigation_actions")
        return {"matches": [{"node_id": "useful-node"}], "planner_runtime": {}}

    monkeypatch.setattr(retrieval, "structured_json", provider)
    monkeypatch.setattr(retrieval, "_retrieve_runtime_attested_impl", runtime_impl)
    events: list[tuple[str, dict[str, Any]]] = []

    result = retrieval.retrieve_runtime(
        RetrieveRequest(query_text="Find reviewed context."),
        {"buckets": []},
        {"core_nodes": []},
        ai_admission=_admission(),
        event_callback=lambda event_type, payload: events.append((event_type, payload)),
    )

    assert result["stop_reason"] == "review_required_ai_provider_error_after_useful_evidence"
    assert result["matches"] == [{"node_id": "useful-node"}]
    assert result["late_ai_failure_preserved_checkpoint"] is True
    assert result["canonical_search_state"] == "review_required"
    assert result["status"] == "review_required"
    assert result["grounded_partial"] is True
    assert result["context_package"]["status"] == "review_required_with_preserved_evidence"
    assert result["provider_issue"] == {
        "reason": "blocked_ai_provider_timeout",
        "failed_call": "navigation_actions",
        "provider_error": "transport_error:timed out",
    }
    unresolved = result["unresolved_required_missions"]
    assert [row["mission_id"] for row in unresolved] == ["mission-resolved", "mission-open"]
    assert unresolved[0]["coverage_state"] == "resolved"
    assert unresolved[0]["judgement_current"] is False
    assert unresolved[0]["ai_branch_controller_used"] is False
    assert unresolved[1]["coverage_state"] == "missed"
    assert unresolved[1]["coverage_reason"] == "supporting evidence still required"
    assert result["billing"]["charged_units"] == 0
    assert result["search_ai_execution"]["failed_call"] == "navigation_actions"
    assert result["search_ai_execution"]["status"] == "review_required"
    assert [event_type for event_type, _ in events] == ["context_update", "search_review_required", "result_ready"]
    assert events[0][1]["matches"] == [{"node_id": "useful-node"}]
    assert events[-1][1]["result"]["matches"] == [{"node_id": "useful-node"}]
    assert events[-1][1]["result"]["canonical_search_state"] == "review_required"


def test_invalid_ai_output_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(retrieval, "llm_enabled", lambda: True)
    monkeypatch.setattr(
        retrieval,
        "structured_json",
        lambda *args, **kwargs: ({}, None),
    )
    monkeypatch.setattr(
        retrieval,
        "_retrieve_runtime_attested_impl",
        lambda *args, **kwargs: _runtime_ai_call("master_judge"),
    )

    result = retrieval.retrieve_runtime(
        RetrieveRequest(query_text="Find reviewed context."),
        {"buckets": []},
        {"core_nodes": []},
        ai_admission=_admission(),
    )

    assert result["stop_reason"] == "blocked_ai_provider_invalid_output"
    assert result["matches"] == []
    assert result["answer"] is None
    assert result["billing"]["charged_units"] == 0


def test_query_plan_timeout_returns_structured_non_chargeable_block(monkeypatch) -> None:
    monkeypatch.setattr(
        core_retrieve_router,
        "_brain_request_scope",
        lambda *_args, **_kwargs: nullcontext({"brain_id": "brain_timeout", "node_count": 1}),
    )
    monkeypatch.setattr(core_retrieve_router, "_public_search_ai_admission", lambda _payload: _admission())
    monkeypatch.setattr(
        core_retrieve_router,
        "_create_planned_search_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            retrieval.SearchAiExecutionError("ai_spatial_landing", "transport_error:timed out")
        ),
    )

    app = FastAPI()
    app.include_router(core_retrieve_router.create_core_retrieve_router())
    response = TestClient(app).post(
        "/memory/query-plan",
        json={"query_text": "Find reviewed context."},
    )

    assert response.status_code == 503
    payload = response.json()
    assert payload["code"] == "blocked_ai_provider_timeout"
    assert payload["status"] == "blocked"
    assert payload["session_created"] is False
    assert payload["mutates_memory"] is False
    assert payload["chargeable"] is False
    assert payload["charged_units"] == 0


def test_query_plan_failure_returns_persisted_terminal_search_reference(monkeypatch) -> None:
    monkeypatch.setattr(
        core_retrieve_router,
        "_brain_request_scope",
        lambda *_args, **_kwargs: nullcontext({"brain_id": "brain_timeout", "node_count": 1}),
    )
    monkeypatch.setattr(core_retrieve_router, "_public_search_ai_admission", lambda _payload: _admission())
    failure = retrieval.SearchAiExecutionError("ai_spatial_landing", "invalid_json")
    failure.search_id = "search-planning-failed"
    failure.result_ref = {
        "search_id": failure.search_id,
        "brain_id": "brain_timeout",
        "endpoint": f"/memory/query-result/{failure.search_id}?brain_id=brain_timeout",
        "package_revision": "sha256:failed-result",
    }
    monkeypatch.setattr(
        core_retrieve_router,
        "_create_planned_search_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
    )

    app = FastAPI()
    app.include_router(core_retrieve_router.create_core_retrieve_router())
    response = TestClient(app).post(
        "/memory/query-plan",
        json={"query_text": "Find reviewed context."},
    )

    assert response.status_code == 503
    payload = response.json()
    assert payload["code"] == "blocked_ai_provider_invalid_output"
    assert payload["search_id"] == failure.search_id
    assert payload["session_created"] is True
    assert payload["terminal_result_persisted"] is True
    assert payload["terminal_for_client"] is True
    assert payload["result_available"] is True
    assert payload["result_url"] == failure.result_ref["endpoint"]
    assert payload["result_ref"] == failure.result_ref
    assert payload["receipt"]["search_id"] == failure.search_id
    assert payload["detail"]["search_id"] == failure.search_id
    assert payload["chargeable"] is False
    assert payload["charged_units"] == 0


def test_planning_failure_closes_the_created_search_session(monkeypatch) -> None:
    events: list[tuple[str, str, dict[str, Any]]] = []
    failed: list[tuple[str, str]] = []
    finalized: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(core_retrieve_router, "fetch_active_search_session_by_thread", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(core_retrieve_router, "create_search_session", lambda search_id, _request: events.append((search_id, "created", {})))
    monkeypatch.setattr(
        core_retrieve_router,
        "append_search_event",
        lambda search_id, event_type, payload: events.append((search_id, event_type, dict(payload))),
    )
    monkeypatch.setattr(core_retrieve_router, "fail_search_session", lambda search_id, error: failed.append((search_id, error)))
    monkeypatch.setattr(
        core_retrieve_router,
        "finalize_search_session",
        lambda search_id, result: finalized.append((search_id, dict(result))) or dict(result),
    )
    monkeypatch.setattr(core_retrieve_router, "_runtime_atlas", lambda: {"buckets": []})
    monkeypatch.setattr(core_retrieve_router, "fetch_identity_nucleus", lambda: {"core_nodes": []})
    monkeypatch.setattr(core_retrieve_router, "current_brain_id", lambda: "brain_timeout")
    monkeypatch.setattr(
        core_retrieve_router,
        "prepare_runtime_plan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            retrieval.SearchAiExecutionError("ai_spatial_landing", "transport_error:timed out")
        ),
    )

    with pytest.raises(retrieval.SearchAiExecutionError) as raised:
        core_retrieve_router._create_planned_search_session(
            RetrieveRequest(query_text="Find reviewed context.", brain_id="brain_timeout"),
            ai_admission=_admission(),
        )

    assert len(failed) == 1
    assert failed[0][0]
    assert "search_ai_call_failed" in failed[0][1]
    planning_failed = [payload for _, event_type, payload in events if event_type == "planning_failed"]
    assert len(planning_failed) == 1
    assert planning_failed[0]["brain_id"] == "brain_timeout"
    assert planning_failed[0]["failed_call"] == "ai_spatial_landing"
    assert planning_failed[0]["reason"] == "blocked_ai_provider_timeout"
    assert planning_failed[0]["provider_error"] == "transport_error:timed out"
    assert planning_failed[0]["terminal_result_persisted"] is True
    assert isinstance(planning_failed[0]["elapsed_ms"], float)
    assert len(finalized) == 1
    terminal = finalized[0][1]
    assert raised.value.search_id == finalized[0][0]
    assert raised.value.result_ref["search_id"] == finalized[0][0]
    assert raised.value.result_ref["package_revision"]
    assert terminal["status"] == "failed"
    assert terminal["canonical_search_state"] == "failed"
    assert terminal["completion_state"] == "planning_failed"
    assert terminal["completion_contract"]["result_materialization_state"] == "planning_failed"
    assert terminal["mcp_delivery_contract"]["completion_state"] == "planning_failed"
    assert terminal["mcp_delivery_contract"]["terminal_for_client"] is True
    assert terminal["context_package"]["status"] == "planning_failed"
    assert "No heuristic or keyword fallback was run" in terminal["context_package"]["agent_markdown"]
    assert terminal["planner_runtime"]["heuristic_fallback_used"] is False
    assert terminal["planner_runtime"]["keyword_fallback_used"] is False
    result_ready = [payload for _, event_type, payload in events if event_type == "result_ready"]
    assert result_ready
    assert result_ready[-1]["completion_state"] == "planning_failed"


def test_each_runtime_ai_call_has_its_own_attestation(monkeypatch) -> None:
    monkeypatch.setattr(retrieval, "llm_enabled", lambda: True)
    provider_calls = 0

    def provider(*args, execution_metadata: dict[str, Any], **kwargs):
        nonlocal provider_calls
        provider_calls += 1
        execution_metadata.update(_attestation_for_call(provider_calls))
        return {"ok": True}, None

    def runtime_impl(*args, **kwargs):
        _runtime_ai_call("branch_controller")
        _runtime_ai_call("navigation_actions")
        return {"matches": [], "planner_runtime": {}}

    monkeypatch.setattr(retrieval, "structured_json", provider)
    monkeypatch.setattr(retrieval, "_retrieve_runtime_attested_impl", runtime_impl)

    result = retrieval.retrieve_runtime(
        RetrieveRequest(query_text="Find reviewed context."),
        {"buckets": []},
        {"core_nodes": []},
        ai_admission=_admission(),
    )

    calls = result["search_ai_execution"]["calls"]
    assert result["search_ai_execution"]["status"] == "completed"
    assert [item["call_name"] for item in calls] == [
        "planner_seed_admission",
        "branch_controller",
        "navigation_actions",
    ]
    runtime_attestations = [item["ai_execution_attestation"] for item in calls[1:]]
    assert runtime_attestations[0] is not runtime_attestations[1]
    assert runtime_attestations[0]["request_sha256"] != runtime_attestations[1]["request_sha256"]
    assert runtime_attestations[0]["output_sha256"] != runtime_attestations[1]["output_sha256"]


def test_runtime_relays_progress_before_completion_and_defers_terminal_lifecycle_to_session_writer(monkeypatch) -> None:
    events: list[tuple[str, dict[str, Any]]] = []

    def runtime_impl(*args, event_callback=None, **kwargs):
        assert event_callback is not None
        event_callback("planning_complete", {"mission_count": 2})
        assert [event_type for event_type, _ in events] == ["planning_complete"]

        event_callback("context_update", {"matches": [{"node_id": "grounded-node"}]})
        assert [event_type for event_type, _ in events] == ["planning_complete", "context_update"]

        event_callback(
            "search_stopped",
            {
                "canonical_search_state": "completed",
                "result_materialization_state": "finalized",
                "final_materialization_pending": False,
                "result_ready_terminal": True,
                "terminal_for_client": True,
            },
        )
        assert [event_type for event_type, _ in events] == ["planning_complete", "context_update"]

        event_callback("result_ready", {"package_revision": "uncanonicalized"})
        assert [event_type for event_type, _ in events] == ["planning_complete", "context_update"]
        return {
            "matches": [{"node_id": "grounded-node"}],
            "planner_runtime": {},
            "status": "review_required",
        }

    monkeypatch.setattr(retrieval, "_retrieve_runtime_attested_impl", runtime_impl)

    result = retrieval.retrieve_runtime(
        RetrieveRequest(query_text="Find reviewed context."),
        {"buckets": []},
        {"core_nodes": []},
        ai_admission=_admission(),
        search_id="search-progressive",
        event_callback=lambda event_type, payload: events.append((event_type, payload)),
    )

    assert [event_type for event_type, _ in events] == ["planning_complete", "context_update"]
    assert result["search_ai_execution"]["status"] == "completed"


def test_core_session_writer_persists_single_result_ready_after_finalize(monkeypatch) -> None:
    search_id = "search-finalizer-writer"
    brain_id = "brain-finalizer-writer"
    request_payload = {
        "brain_id": brain_id,
        "query_text": "Find reviewed context.",
        "thread_id": "thread-finalizer-writer",
        "response_mode": "context",
        "retrieval_mode": "flash",
    }
    plan = _attested_runtime_plan()
    events: list[tuple[str, dict[str, Any]]] = []
    finalized: list[dict[str, Any]] = []

    monkeypatch.setattr(
        core_retrieve_router,
        "fetch_search_session",
        lambda _search_id: {"request": request_payload, "plan": plan},
    )
    monkeypatch.setattr(core_retrieve_router, "_runtime_atlas", lambda: {"buckets": []})
    monkeypatch.setattr(core_retrieve_router, "fetch_identity_nucleus", lambda: {"core_nodes": []})
    monkeypatch.setattr(core_retrieve_router, "current_brain_id", lambda: brain_id)
    monkeypatch.setattr(core_retrieve_router, "save_search_plan", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(core_retrieve_router, "mark_search_running", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(core_retrieve_router, "_publish_mcp_first_package", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(core_retrieve_router, "normalize_retrieve_response_payload", lambda payload: dict(payload or {}))
    monkeypatch.setattr(
        core_retrieve_router,
        "_attach_brain_metadata",
        lambda payload: {**dict(payload or {}), "brain_id": dict(payload or {}).get("brain_id") or brain_id},
    )
    monkeypatch.setattr(core_retrieve_router, "_attach_mcp_surface_fields", lambda payload, **_kwargs: dict(payload or {}))
    monkeypatch.setattr(
        core_retrieve_router,
        "_apply_plan_first_usable_partial_public_projection",
        lambda result, **_kwargs: (dict(result or {}), False),
    )
    monkeypatch.setattr(core_retrieve_router, "project_search_result_lifecycle", lambda result, _status: dict(result or {}))
    monkeypatch.setattr(core_retrieve_router, "_retrieve_response_schema_safe", lambda result: dict(result or {}))

    def append_event(_search_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        events.append((event_type, dict(payload or {})))
        return {"seq": len(events), "event_type": event_type, "payload": dict(payload or {})}

    def finalize_session(_search_id: str, result: dict[str, Any]) -> dict[str, Any]:
        finalized.append(dict(result or {}))
        return {
            **dict(result or {}),
            "package_revision": "post-finalize-revision",
            "final_materialization_pending": False,
            "result_ready_terminal": True,
        }

    def runtime_impl(*_args, event_callback=None, **_kwargs):
        assert event_callback is not None
        event_callback("final_materialization_completed", {"search_id": search_id})
        event_callback(
            "search_stopped",
            {
                "search_id": search_id,
                "package_revision": "inner-stopped-revision",
                "final_materialization_pending": False,
                "result_ready_terminal": True,
                "terminal_for_client": True,
            },
        )
        event_callback(
            "result_ready",
            {
                "search_id": search_id,
                "package_revision": "inner-result-ready-revision",
                "final_materialization_pending": False,
                "result_ready_terminal": True,
            },
        )
        return {
            "search_id": search_id,
            "status": "review_required",
            "matches": [{"node_id": "grounded-node"}],
            "planner_runtime": {},
        }

    monkeypatch.setattr(core_retrieve_router, "append_search_event", append_event)
    monkeypatch.setattr(core_retrieve_router, "finalize_search_session", finalize_session)
    monkeypatch.setattr(retrieval, "_retrieve_runtime_attested_impl", runtime_impl)

    result = core_retrieve_router._run_search_session_sync(search_id)

    event_types = [event_type for event_type, _payload in events]
    assert event_types == ["worker_started", "final_materialization_completed", "result_ready"]
    assert event_types.count("result_ready") == 1
    assert "search_stopped" not in event_types
    assert finalized
    assert result["package_revision"] == "post-finalize-revision"
    result_ready_payload = events[-1][1]
    assert result_ready_payload["result"]["package_revision"] == "post-finalize-revision"
    assert result_ready_payload.get("package_revision") != "inner-result-ready-revision"


@pytest.mark.skip(reason="monolith main.py is not part of Public Core")
def test_main_session_writer_persists_single_result_ready_after_finalize(monkeypatch) -> None:
    main = importlib.import_module("main")
    search_id = "search-main-finalizer-writer"
    brain_id = "brain-main-finalizer-writer"
    request_payload = {
        "brain_id": brain_id,
        "query_text": "Find reviewed context.",
        "thread_id": "thread-main-finalizer-writer",
        "response_mode": "context",
        "retrieval_mode": "flash",
    }
    plan = _attested_runtime_plan()
    events: list[tuple[str, dict[str, Any]]] = []
    finalized: list[dict[str, Any]] = []

    monkeypatch.setattr(main, "_brain_request_scope", lambda *_args, **_kwargs: nullcontext({}))
    monkeypatch.setattr(main, "fetch_search_session", lambda _search_id: {"request": request_payload, "plan": plan})
    monkeypatch.setattr(main, "_runtime_atlas", lambda: {"buckets": []})
    monkeypatch.setattr(main, "fetch_identity_nucleus", lambda: {"core_nodes": []})
    monkeypatch.setattr(main, "current_brain_id", lambda: brain_id)
    monkeypatch.setattr(main, "save_search_plan", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "mark_search_running", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_mcp_maybe_finalize_bounded_first_package_before_heavy_worker", lambda **_kwargs: False)
    monkeypatch.setattr(main, "normalize_retrieve_response_payload", lambda payload: dict(payload or {}))
    monkeypatch.setattr(
        main,
        "_attach_brain_metadata",
        lambda payload: {**dict(payload or {}), "brain_id": dict(payload or {}).get("brain_id") or brain_id},
    )
    monkeypatch.setattr(main, "_mcp_attach_hot_working_memory_metadata", lambda result, **_kwargs: dict(result or {}))
    monkeypatch.setattr(main, "_mcp_tool_name_for_session_payload", lambda *_args, **_kwargs: "retrieve_context")
    monkeypatch.setattr(main, "_preserve_prior_ready_context_package", lambda _search_id, result, **_kwargs: dict(result or {}))
    monkeypatch.setattr(main, "_attach_mcp_surface_contracts", lambda result, **_kwargs: dict(result or {}))
    monkeypatch.setattr(main, "_record_query_metacognition_safely", lambda **kwargs: dict(kwargs["result"] or {}))
    monkeypatch.setattr(main, "_late_useful_review_required_result", lambda _result: False)
    monkeypatch.setattr(main, "_mcp_store_first_package_cache", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_mcp_runtime_storage_snapshot", lambda _tool_name, result: dict(result or {}))
    monkeypatch.setattr(main, "_mcp_schedule_runtime_retention", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "learn_from_retrieval_session", lambda **_kwargs: None)

    def append_event(_search_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        events.append((event_type, dict(payload or {})))
        return {"seq": len(events), "event_type": event_type, "payload": dict(payload or {})}

    def finalize_session(_search_id: str, result: dict[str, Any]) -> dict[str, Any]:
        finalized.append(dict(result or {}))
        return {
            **dict(result or {}),
            "package_revision": "post-finalize-revision",
            "final_materialization_pending": False,
            "result_ready_terminal": True,
        }

    def runtime_impl(*_args, event_callback=None, **_kwargs):
        assert event_callback is not None
        event_callback("final_materialization_completed", {"search_id": search_id})
        event_callback(
            "search_stopped",
            {
                "search_id": search_id,
                "package_revision": "inner-stopped-revision",
                "final_materialization_pending": False,
                "result_ready_terminal": True,
                "terminal_for_client": True,
            },
        )
        event_callback(
            "result_ready",
            {
                "search_id": search_id,
                "package_revision": "inner-result-ready-revision",
                "final_materialization_pending": False,
                "result_ready_terminal": True,
            },
        )
        return {
            "search_id": search_id,
            "status": "review_required",
            "matches": [{"node_id": "grounded-node"}],
            "planner_runtime": {},
        }

    monkeypatch.setattr(main, "append_search_event", append_event)
    monkeypatch.setattr(main, "_persist_search_stream_event", append_event)
    monkeypatch.setattr(main, "finalize_search_session", finalize_session)
    monkeypatch.setattr(retrieval, "_retrieve_runtime_attested_impl", runtime_impl)

    main._run_search_session(search_id, brain_id)

    event_types = [event_type for event_type, _payload in events]
    assert event_types == ["worker_started", "final_materialization_completed", "result_ready"]
    assert event_types.count("result_ready") == 1
    assert "search_stopped" not in event_types
    assert finalized
    result_ready_payload = events[-1][1]
    assert result_ready_payload["result"]["package_revision"] == "post-finalize-revision"
    assert result_ready_payload.get("package_revision") != "inner-result-ready-revision"


@pytest.mark.skip(reason="monolith main.py is not part of Public Core")
def test_main_delayed_search_thread_preserves_runtime_brain_context(monkeypatch, tmp_path) -> None:
    main = importlib.import_module("main")
    search_id = "search-main-delayed-context"
    brain_id = "brain-main-delayed-context"
    done = threading.Event()
    observed: dict[str, Any] = {}

    def run_session(observed_search_id: str, observed_brain_id: str | None = None) -> None:
        observed["search_id"] = observed_search_id
        observed["arg_brain_id"] = observed_brain_id
        observed["context_brain_id"] = main.current_brain_id()
        done.set()

    monkeypatch.setattr(main, "_run_search_session", run_session)

    with use_runtime_brain({"brain_id": brain_id, "storage_path": str(tmp_path)}):
        main._schedule_search_thread(search_id, delay_seconds=0.01)

    assert done.wait(1.0)
    assert observed == {
        "search_id": search_id,
        "arg_brain_id": brain_id,
        "context_brain_id": brain_id,
    }
    with main._SEARCH_THREAD_LOCK:
        main._SEARCH_THREADS.pop(search_id, None)


@pytest.mark.skip(reason="monolith main.py is not part of Public Core")
def test_main_final_materialization_heartbeat_uses_search_brain_scope(monkeypatch, tmp_path) -> None:
    main = importlib.import_module("main")
    search_id = "search-main-final-heartbeat"
    brain_id = "brain-main-final-heartbeat"
    heartbeat_seen = threading.Event()
    events: list[tuple[str, dict[str, Any], str | None]] = []

    monkeypatch.setenv("AGVM_SEARCH_FINAL_MATERIALIZATION_HEARTBEAT_SECONDS", "0.01")
    monkeypatch.setattr(main, "_mcp_defer_repeated_first_package_capture", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(main, "_mcp_capture_first_package_snapshot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_mcp_enrich_stream_payload", lambda _search_id, _event_type, payload: dict(payload or {}))

    def append_event(_search_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        events.append((event_type, dict(payload or {}), main.current_brain_id()))
        if event_type == "final_materialization_heartbeat":
            heartbeat_seen.set()
        return {"seq": len(events), "event_type": event_type, "payload": dict(payload or {})}

    monkeypatch.setattr(main, "append_search_event", append_event)

    try:
        with use_runtime_brain({"brain_id": brain_id, "storage_path": str(tmp_path)}):
            main._persist_search_stream_event(
                search_id,
                "final_materialization_started",
                {"search_id": search_id, "final_materialization_pending": True},
            )

        assert heartbeat_seen.wait(1.0)
    finally:
        main._stop_final_materialization_heartbeat(search_id)
        time.sleep(0.02)

    heartbeat_events = [event for event in events if event[0] == "final_materialization_heartbeat"]
    assert heartbeat_events
    event_type, payload, context_brain_id = heartbeat_events[0]
    assert event_type == "final_materialization_heartbeat"
    assert payload["brain_id"] == brain_id
    assert context_brain_id == brain_id
    assert payload["final_materialization_pending"] is True
    assert payload["result_ready_terminal"] is False


def test_main_background_stream_event_reenters_payload_brain_scope(monkeypatch, tmp_path) -> None:
    main = importlib.import_module("main")
    search_id = "search-main-background-callback"
    brain_id = "brain-main-background-callback"
    observed: list[tuple[str | None, str | None]] = []

    @contextmanager
    def brain_scope(resolved_brain_id: str):
        assert resolved_brain_id == brain_id
        with use_runtime_brain(
            {"brain_id": resolved_brain_id, "storage_path": str(tmp_path)}
        ):
            yield

    monkeypatch.setattr(main, "_brain_request_scope", brain_scope)
    monkeypatch.setattr(main, "_mcp_defer_repeated_first_package_capture", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(main, "_mcp_capture_first_package_snapshot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_mcp_enrich_stream_payload", lambda _search_id, _event_type, payload: dict(payload or {}))

    def append_event(_search_id: str, _event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        observed.append((main.current_brain_id(), payload.get("brain_id")))
        return {"seq": 1, "event_type": _event_type, "payload": dict(payload or {})}

    monkeypatch.setattr(main, "append_search_event", append_event)

    assert main.current_brain_id() is None
    main._persist_search_stream_event_for_brain(
        search_id,
        brain_id,
        "finalization_progress",
        {"search_id": search_id, "state": "running"},
    )

    assert observed == [(brain_id, brain_id)]
    assert main.current_brain_id() is None


@pytest.mark.skip(reason="monolith main.py is not part of Public Core")
def test_main_final_materialization_heartbeat_survives_transient_persist_error_until_terminal(
    monkeypatch,
    tmp_path,
) -> None:
    main = importlib.import_module("main")
    search_id = "search-main-final-heartbeat-transient-error"
    brain_id = "brain-main-final-heartbeat-transient-error"
    persist_error_seen = threading.Event()
    recovered_heartbeat_seen = threading.Event()
    events: list[tuple[str, dict[str, Any]]] = []
    failed_first_heartbeat = False

    monkeypatch.setenv("AGVM_SEARCH_FINAL_MATERIALIZATION_HEARTBEAT_SECONDS", "0.01")
    monkeypatch.setattr(main, "_mcp_defer_repeated_first_package_capture", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(main, "_mcp_capture_first_package_snapshot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_mcp_enrich_stream_payload", lambda _search_id, _event_type, payload: dict(payload or {}))

    def append_event(_search_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal failed_first_heartbeat
        if event_type == "final_materialization_heartbeat" and not failed_first_heartbeat:
            failed_first_heartbeat = True
            persist_error_seen.set()
            raise RuntimeError("transient heartbeat persistence failure")
        events.append((event_type, dict(payload or {})))
        if event_type == "final_materialization_heartbeat":
            recovered_heartbeat_seen.set()
        return {"seq": len(events), "event_type": event_type, "payload": dict(payload or {})}

    monkeypatch.setattr(main, "append_search_event", append_event)

    try:
        with use_runtime_brain({"brain_id": brain_id, "storage_path": str(tmp_path)}):
            main._persist_search_stream_event(
                search_id,
                "final_materialization_started",
                {"search_id": search_id, "final_materialization_pending": True},
            )

        assert persist_error_seen.wait(1.0)
        assert recovered_heartbeat_seen.wait(1.0)

        with main._SEARCH_FINAL_MATERIALIZATION_HEARTBEAT_LOCK:
            heartbeat_thread = main._SEARCH_FINAL_MATERIALIZATION_HEARTBEATS[search_id][1]
        main._persist_search_stream_event(
            search_id,
            "result_ready",
            {
                "search_id": search_id,
                "final_materialization_pending": False,
                "result_ready_terminal": True,
            },
        )
        heartbeat_thread.join(timeout=1.0)
        assert not heartbeat_thread.is_alive()
        successful_count_at_terminal = sum(
            event_type == "final_materialization_heartbeat" for event_type, _payload in events
        )
        time.sleep(0.04)
    finally:
        main._stop_final_materialization_heartbeat(search_id)

    diagnostics = [payload for event_type, payload in events if event_type == "final_materialization_heartbeat_error"]
    recovered = [payload for event_type, payload in events if event_type == "final_materialization_heartbeat"]
    assert len(diagnostics) == 1
    assert diagnostics[0]["heartbeat_persist_error_count"] == 1
    assert diagnostics[0]["heartbeat_consecutive_persist_error_count"] == 1
    assert diagnostics[0]["error"] == "transient heartbeat persistence failure"
    assert diagnostics[0]["retry_state"] == "retry_next_interval"
    assert recovered
    assert recovered[0]["heartbeat_persist_error_count"] == 1
    assert recovered[0]["heartbeat_consecutive_persist_error_count"] == 1
    assert recovered[0]["last_heartbeat_persist_error"] == "transient heartbeat persistence failure"
    assert sum(event_type == "final_materialization_heartbeat" for event_type, _payload in events) == successful_count_at_terminal
    with main._SEARCH_FINAL_MATERIALIZATION_HEARTBEAT_LOCK:
        assert search_id not in main._SEARCH_FINAL_MATERIALIZATION_HEARTBEATS


def test_internal_runtime_rejects_missing_admission_before_plan_creation(monkeypatch) -> None:
    monkeypatch.setattr(
        retrieval,
        "prepare_runtime_plan",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("plan construction must not start")
        ),
    )

    with pytest.raises(retrieval.AiModuleContractError) as exc_info:
        retrieval.retrieve_runtime(
            RetrieveRequest(query_text="Find reviewed context."),
            {"buckets": []},
            {"core_nodes": []},
        )

    assert exc_info.value.code == "search_ai_admission_required"


def test_internal_runtime_rejects_tampered_prepared_plan_before_traversal(monkeypatch) -> None:
    admission = _admission()
    tampered_plan = {
        "search_ai_admission": {
            key: value
            for key, value in admission.items()
            if key != "planner_seed_payload"
        },
        "ai_execution_attestation": _attestation(),
        "answer_strands": admission["answer_strands"],
        "probes": [{"planner_family": "heuristic"}],
        "branches": [{"planner_family": "heuristic"}],
        "planner_runtime": {
            "ai_execution_attested": True,
            "heuristic_fallback_used": False,
        },
    }
    monkeypatch.setattr(
        retrieval,
        "fetch_warm_thread_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("runtime traversal must not start")
        ),
    )

    with pytest.raises(retrieval.AiModuleContractError) as exc_info:
        retrieval.retrieve_runtime(
            RetrieveRequest(query_text="Find reviewed context."),
            {"buckets": []},
            {"core_nodes": []},
            prepared_plan=tampered_plan,
        )

    assert exc_info.value.code == "search_ai_probe_heuristic_material_forbidden"


def test_legacy_internal_retrieve_requires_admission_before_planning(monkeypatch) -> None:
    monkeypatch.setattr(
        retrieval,
        "build_identity_context",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("legacy planning must not start")
        ),
    )

    with pytest.raises(retrieval.AiModuleContractError) as exc_info:
        retrieval.retrieve(
            RetrieveRequest(query_text="Find reviewed context."),
            {},
            {},
            {"buckets": []},
        )

    assert exc_info.value.code == "search_ai_admission_required"


def test_legacy_internal_retrieve_never_falls_back_after_ai_failure(monkeypatch) -> None:
    monkeypatch.setattr(retrieval, "build_identity_context", lambda graph: {})
    monkeypatch.setattr(
        retrieval,
        "llm_retrieval_plan",
        lambda *args, **kwargs: (None, "transport_error:timed out"),
    )
    monkeypatch.setattr(
        retrieval,
        "fallback_retrieval_plan",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("heuristic fallback must not execute")
        ),
    )

    with pytest.raises(retrieval.AiModuleContractError) as exc_info:
        retrieval.retrieve(
            RetrieveRequest(query_text="Find reviewed context."),
            {},
            {},
            {"buckets": []},
            ai_admission=_admission(),
        )

    assert exc_info.value.code == "blocked_ai_provider_timeout"


def test_search_ai_admission_blocks_missing_provider_without_calling_planner(monkeypatch) -> None:
    monkeypatch.setattr(retrieval, "llm_enabled", lambda: False)
    monkeypatch.setattr(
        retrieval,
        "_run_fast_planner_seed_request",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("provider must not be called")),
    )

    admission = retrieval.require_search_ai_admission(
        RetrieveRequest(query_text="What does this brain know?"),
        {},
    )

    assert admission == {
        "schema_version": "agvm.search_ai_admission.v2",
        "status": "blocked",
        "reason": "blocked_ai_provider_unavailable",
        "provider_error": "llm_disabled",
        "chargeable": False,
        "charged_units": 0,
    }


def test_search_ai_admission_blocks_timeout_and_invalid_json(monkeypatch) -> None:
    monkeypatch.setattr(retrieval, "llm_enabled", lambda: True)
    request = RetrieveRequest(query_text="Find the reviewed product policy.")

    for provider_error, expected_reason in (
        ("transport_error:timed out", "blocked_ai_provider_timeout"),
        ("invalid_json", "blocked_ai_provider_invalid_output"),
    ):
        monkeypatch.setattr(
            retrieval,
            "_run_fast_planner_seed_request",
            lambda *args, _error=provider_error, **kwargs: (None, _error),
        )
        admission = retrieval.require_search_ai_admission(request, {})
        assert admission["status"] == "blocked"
        assert admission["reason"] == expected_reason
        assert admission["chargeable"] is False
        assert admission["charged_units"] == 0
        assert "ai_execution_attestation" not in admission


def test_search_ai_admission_uses_product_bounded_planner_timeout(monkeypatch) -> None:
    monkeypatch.setattr(retrieval, "llm_enabled", lambda: True)
    monkeypatch.delenv("AGVM_SEARCH_AI_ADMISSION_FLASH_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("AGVM_SEARCH_AI_ADMISSION_TIMEOUT_SECONDS", raising=False)
    observed_timeouts: list[float | None] = []
    started_at_ms = int(time.time() * 1000)

    def provider(*args: Any, **kwargs: Any) -> tuple[dict[str, Any], None]:
        observed_timeouts.append(kwargs.get("timeout_override"))
        kwargs["execution_metadata"].update(_attestation())
        return _planner_payload(), None

    monkeypatch.setattr(retrieval, "_run_fast_planner_seed_request", provider)

    admission = retrieval.require_search_ai_admission(
        RetrieveRequest(
            query_text="Which reviewed NAFFCO product evidence can answer this?",
            retrieval_mode="flash",
        ),
        {},
    )

    assert admission["status"] == "admitted"
    assert observed_timeouts == [pytest.approx(20.0)]
    runtime_budget_seconds = (
        int(admission["runtime_deadline_at_ms"]) - started_at_ms
    ) / 1000.0
    assert runtime_budget_seconds > 100.0


def test_search_ai_admission_timeout_env_override_is_clamped(monkeypatch) -> None:
    monkeypatch.setenv("AGVM_SEARCH_AI_ADMISSION_FLASH_TIMEOUT_SECONDS", "0.2")
    assert retrieval._search_ai_admission_timeout_seconds("flash") == 1.0

    monkeypatch.setenv("AGVM_SEARCH_AI_ADMISSION_FLASH_TIMEOUT_SECONDS", "120")
    assert retrieval._search_ai_admission_timeout_seconds("flash") == 90.0


def test_search_ai_admission_defaults_allow_one_structured_provider_round_trip(monkeypatch) -> None:
    for name in (
        "AGVM_SEARCH_AI_ADMISSION_FLASH_TIMEOUT_SECONDS",
        "AGVM_SEARCH_AI_ADMISSION_BALANCED_TIMEOUT_SECONDS",
        "AGVM_SEARCH_AI_ADMISSION_HEAVY_TIMEOUT_SECONDS",
        "AGVM_SEARCH_AI_ADMISSION_FORENSIC_TIMEOUT_SECONDS",
        "AGVM_SEARCH_AI_ADMISSION_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)

    assert retrieval._search_ai_admission_timeout_seconds("flash") == 20.0
    assert retrieval._search_ai_admission_timeout_seconds("balanced") == 30.0
    assert retrieval._search_ai_admission_timeout_seconds("heavy") == 45.0
    assert retrieval._search_ai_admission_timeout_seconds("forensic") == 60.0


def test_search_ai_timeout_http_block_does_not_report_missing_configuration() -> None:
    payload = retrieval.build_search_ai_http_block_payload(
        {
            "status": "blocked",
            "reason": "blocked_ai_provider_timeout",
            "provider_error": "transport_error:timed out",
            "chargeable": False,
            "charged_units": 0,
        }
    )

    assert payload["code"] == "blocked_ai_provider_timeout"
    assert payload["next_action"] == "retry_or_start_async_search"
    assert "Configure" not in payload["message"]
    assert "configure provider" not in payload["user_message"]


def test_core_public_search_ai_admission_hard_caps_slow_worker(monkeypatch) -> None:
    monkeypatch.setattr(core_retrieve_router, "fetch_identity_nucleus", lambda: {})
    monkeypatch.setattr(core_retrieve_router, "search_identity_nucleus_for_named_targets", lambda *_args: {})
    monkeypatch.setattr(core_retrieve_router, "_search_ai_admission_timeout_seconds", lambda *_args: 0.01)
    monkeypatch.setattr(core_retrieve_router, "_planner_seed_transport_guard_seconds", lambda: 0.01)
    started = threading.Event()

    def slow_admission(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        started.set()
        time.sleep(2.0)
        return _admission()

    monkeypatch.setattr(core_retrieve_router, "require_search_ai_admission", slow_admission)
    started_at = time.perf_counter()

    admission = core_retrieve_router._public_search_ai_admission(
        RetrieveRequest(query_text="Find reviewed context.", retrieval_mode="flash")
    )

    assert started.is_set()
    assert time.perf_counter() - started_at < 0.8
    assert admission["status"] == "blocked"
    assert admission["reason"] == "blocked_ai_provider_timeout"
    assert admission["chargeable"] is False
    assert admission["charged_units"] == 0


def test_final_context_wave_event_is_opt_in(monkeypatch) -> None:
    monkeypatch.delenv("AGVM_SEARCH_EMIT_FINAL_CONTEXT_WAVE_EVENT", raising=False)
    assert retrieval._emit_final_context_wave_event() is False

    monkeypatch.setenv("AGVM_SEARCH_EMIT_FINAL_CONTEXT_WAVE_EVENT", "true")
    assert retrieval._emit_final_context_wave_event() is True


@pytest.mark.parametrize(
    "first_result",
    [
        (None, "invalid_json"),
        (None, "missing_output_text"),
        ({}, None),
    ],
)
def test_search_ai_admission_repairs_provider_format_once_with_same_contract(
    monkeypatch,
    first_result: tuple[dict[str, Any] | None, str | None],
) -> None:
    monkeypatch.setattr(retrieval, "llm_enabled", lambda: True)
    calls: list[dict[str, Any]] = []

    def provider(**kwargs: Any) -> tuple[dict[str, Any] | None, str | None]:
        calls.append(dict(kwargs))
        if len(calls) == 1:
            return first_result
        kwargs["execution_metadata"].update(_attestation())
        return _planner_payload(), None

    monkeypatch.setattr(retrieval, "structured_json", provider)
    query_text = "What are Kinetic's reviewed services?"
    admission = retrieval.require_search_ai_admission(
        RetrieveRequest(query_text=query_text),
        {},
    )

    assert admission["status"] == "admitted"
    assert admission["reason"] == "provider_plan_attested"
    assert admission["ai_execution_attestation"] == _attestation()
    assert len(calls) == 2
    assert calls[0]["schema_name"] == calls[1]["schema_name"]
    assert calls[0]["schema"] == calls[1]["schema"]
    assert calls[0]["user_prompt"] == calls[1]["user_prompt"]
    assert query_text in calls[1]["user_prompt"]
    assert calls[1]["system_prompt"].startswith(calls[0]["system_prompt"])
    assert "previous provider output did not conform" in calls[1]["system_prompt"]


def test_search_ai_admission_second_invalid_format_fails_closed_after_one_repair(
    monkeypatch,
) -> None:
    monkeypatch.setattr(retrieval, "llm_enabled", lambda: True)
    calls: list[dict[str, Any]] = []

    def provider(**kwargs: Any) -> tuple[None, str]:
        calls.append(dict(kwargs))
        return None, "invalid_json" if len(calls) == 1 else "missing_output_text"

    monkeypatch.setattr(retrieval, "structured_json", provider)
    admission = retrieval.require_search_ai_admission(
        RetrieveRequest(query_text="Find reviewed Kinetic services."),
        {},
    )

    assert len(calls) == 2
    assert admission["status"] == "blocked"
    assert admission["reason"] == "blocked_ai_provider_invalid_output"
    assert admission["provider_error"] == "missing_output_text"
    assert "ai_execution_attestation" not in admission


def test_search_ai_admission_does_not_repair_transport_failure(monkeypatch) -> None:
    monkeypatch.setattr(retrieval, "llm_enabled", lambda: True)
    calls = 0

    def provider(**_kwargs: Any) -> tuple[None, str]:
        nonlocal calls
        calls += 1
        return None, "transport_error:connection reset"

    monkeypatch.setattr(retrieval, "structured_json", provider)
    admission = retrieval.require_search_ai_admission(
        RetrieveRequest(query_text="Find reviewed Kinetic services."),
        {},
    )

    assert calls == 1
    assert admission["status"] == "blocked"
    assert admission["reason"] == "blocked_ai_provider_error"


def test_search_ai_admission_requires_and_preserves_v2_attestation(monkeypatch) -> None:
    monkeypatch.setattr(retrieval, "llm_enabled", lambda: True)

    def provider(*args, execution_metadata: dict[str, Any], **kwargs):
        execution_metadata.update(_attestation())
        return _planner_payload(), None

    monkeypatch.setattr(retrieval, "_run_fast_planner_seed_request", provider)
    admission = retrieval.require_search_ai_admission(
        RetrieveRequest(query_text="Find the reviewed product history."),
        {},
    )

    assert admission["status"] == "admitted"
    assert admission["reason"] == "provider_plan_attested"
    assert admission["ai_execution_attestation"] == _attestation()
    assert admission["answer_strands"][0]["planner_family"] == "ai"


def test_search_ai_admission_blocks_provider_output_without_v2_attestation(monkeypatch) -> None:
    monkeypatch.setattr(retrieval, "llm_enabled", lambda: True)
    monkeypatch.setattr(
        retrieval,
        "_run_fast_planner_seed_request",
        lambda *args, **kwargs: (_planner_payload(), None),
    )

    admission = retrieval.require_search_ai_admission(
        RetrieveRequest(query_text="Find the reviewed product history."),
        {},
    )

    assert admission["status"] == "blocked"
    assert admission["reason"] == "blocked_ai_attestation_invalid"
    assert admission["chargeable"] is False
    assert admission["charged_units"] == 0


def test_blocked_result_contains_no_heuristic_material_and_no_charge() -> None:
    request = RetrieveRequest(query_text="Find a reviewed answer.", response_mode="context")
    admission = {
        "status": "blocked",
        "reason": "blocked_ai_provider_timeout",
        "provider_error": "transport_error:timed out",
        "chargeable": False,
        "charged_units": 0,
    }
    result = retrieval.normalize_retrieve_response_payload(
        retrieval.build_search_ai_blocked_result(request, admission)
    )
    RetrieveResponse.model_validate(result)
    output = build_mcp_retrieval_tool_output("retrieve_context", result)

    assert result["matches"] == []
    assert result["probes"] == []
    assert result["context_package"] == {}
    assert result["answer"] is None
    assert result["stop_reason"] == "blocked_ai_provider_timeout"
    assert output["status"] == "blocked"
    runtime = output["semantic_contract_runtime"]
    assert runtime["fallback_used"] is False
    assert runtime["heuristic_result_exposed"] is False
    assert runtime["billing"]["chargeable"] is False
    assert runtime["billing"]["charged_units"] == 0


def test_mcp_entrypoint_returns_blocked_before_session_creation(monkeypatch) -> None:
    monkeypatch.setattr(
        core_retrieve_router,
        "_brain_request_scope",
        lambda *args, **kwargs: nullcontext({"brain_id": "brain_test"}),
    )
    monkeypatch.setattr(
        core_retrieve_router,
        "_public_search_ai_admission",
        lambda request: {
            "status": "blocked",
            "reason": "blocked_ai_provider_unavailable",
            "provider_error": "llm_disabled",
            "chargeable": False,
            "charged_units": 0,
        },
    )
    monkeypatch.setattr(
        core_retrieve_router,
        "_create_planned_search_session",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("session must not be created")),
    )

    response = core_retrieve_router._run_mcp_retrieval_tool(
        "retrieve_context",
        McpRetrievalToolRequest(query_text="What is reviewed memory?"),
    )

    assert response.status == "blocked"
    assert response.search_id is None
    assert not str(response.context_package.get("agent_markdown") or "").strip()
    assert int(dict(response.context_package.get("metrics") or {}).get("public_evidence_count") or 0) == 0
    assert response.semantic_contract_runtime["provider_error"] == "llm_disabled"
    assert response.semantic_contract_runtime["billing"]["charged_units"] == 0


def test_local_retrieve_endpoint_blocks_before_session_creation(monkeypatch) -> None:
    monkeypatch.setattr(
        core_retrieve_router,
        "_brain_request_scope",
        lambda *args, **kwargs: nullcontext({"brain_id": "brain_test"}),
    )
    monkeypatch.setattr(
        core_retrieve_router,
        "_public_search_ai_admission",
        lambda request: {
            "status": "blocked",
            "reason": "blocked_ai_provider_invalid_output",
            "provider_error": "invalid_json",
            "chargeable": False,
            "charged_units": 0,
        },
    )
    monkeypatch.setattr(
        core_retrieve_router,
        "_create_planned_search_session",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("session must not be created")),
    )
    app = FastAPI()
    app.include_router(core_retrieve_router.create_core_retrieve_router())

    response = TestClient(app).post(
        "/retrieve",
        json={"query_text": "Find reviewed context.", "response_mode": "context"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["stop_reason"] == "blocked_ai_provider_invalid_output"
    assert payload["matches"] == []
    assert payload["context_package"] == {}
    assert payload["semantic_contract_runtime"]["billing"]["charged_units"] == 0


def test_query_plan_blocks_before_session_creation(monkeypatch) -> None:
    monkeypatch.setattr(
        core_retrieve_router,
        "_brain_request_scope",
        lambda *args, **kwargs: nullcontext({"brain_id": "brain_test"}),
    )
    monkeypatch.setattr(
        core_retrieve_router,
        "_public_search_ai_admission",
        lambda request: {
            "status": "blocked",
            "reason": "blocked_ai_provider_unavailable",
            "provider_error": "llm_disabled",
            "chargeable": False,
            "charged_units": 0,
        },
    )
    monkeypatch.setattr(
        core_retrieve_router,
        "_create_planned_search_session",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("search session must not be created")
        ),
    )
    app = FastAPI()
    app.include_router(core_retrieve_router.create_core_retrieve_router())

    response = TestClient(app).post(
        "/memory/query-plan",
        json={"query_text": "Plan reviewed context."},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "blocked_ai_provider_unavailable"
    assert response.json()["status"] == "blocked"
    assert response.json()["user_message"] == "AI unavailable — configure provider to run Search."
    assert response.json()["next_action"] == "configure_provider"
    assert response.json()["configuration_path"] == "/setup/env"
    assert response.json()["search_id"] is None
    assert response.json()["session_created"] is False
    assert response.json()["mutates_memory"] is False
    assert response.json()["receipt"] is None
    assert response.json()["chargeable"] is False
    assert response.json()["charged_units"] == 0
    assert response.json()["detail"]["code"] == "blocked_ai_provider_unavailable"
    assert response.json()["detail"]["charged_units"] == 0


def test_monolith_query_plan_exposes_provider_configuration_block_without_session(
    monkeypatch,
) -> None:
    if not (API_DIR / "main.py").is_file():
        pytest.skip("monolith-only query-plan boundary")
    import main

    monkeypatch.setattr(main, "current_brain_id", lambda: "brain_test")
    monkeypatch.setattr(
        main,
        "runtime_scope_summary",
        lambda: {"brain": {"brain_id": "brain_test", "node_count": 1}},
    )
    monkeypatch.setattr(main, "_require_retrieval_ready", lambda _brain: None)
    monkeypatch.setattr(main, "fetch_identity_nucleus", lambda: {})
    monkeypatch.setattr(
        main,
        "require_search_ai_admission",
        lambda request, identity: {
            "status": "blocked",
            "reason": "blocked_ai_attestation_invalid",
            "provider_error": "ai_execution_provider_not_executed",
            "chargeable": False,
            "charged_units": 0,
        },
    )
    monkeypatch.setattr(
        main,
        "create_search_session",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("search session must not be created")
        ),
    )
    monkeypatch.setattr(
        main,
        "prepare_runtime_plan",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("planner must not execute")
        ),
    )

    response = TestClient(main.app).post(
        "/memory/query-plan",
        json={"query_text": "Plan reviewed context."},
    )

    assert response.status_code == 503
    payload = response.json()
    assert payload["code"] == "blocked_ai_provider_invalid_output"
    assert payload["provider_reason"] == "blocked_ai_attestation_invalid"
    assert payload["search_id"] is None
    assert payload["session_created"] is False
    assert payload["mutates_memory"] is False
    assert payload["receipt"] is None
    assert payload["chargeable"] is False
    assert payload["charged_units"] == 0


def test_monolith_query_plan_normalizes_provider_timeout_and_closes_session(monkeypatch) -> None:
    if not (API_DIR / "main.py").is_file():
        pytest.skip("monolith-only query-plan boundary")
    import main

    failed: list[tuple[str, str]] = []
    monkeypatch.setattr(main, "current_brain_id", lambda: "brain_test")
    monkeypatch.setattr(main, "runtime_scope_summary", lambda: {"brain": {"brain_id": "brain_test", "node_count": 1}})
    monkeypatch.setattr(main, "_require_retrieval_ready", lambda _brain: None)
    monkeypatch.setattr(main, "_runtime_atlas", lambda: {"buckets": []})
    monkeypatch.setattr(main, "fetch_identity_nucleus", lambda: {})
    monkeypatch.setattr(main, "require_search_ai_admission", lambda _request, _identity: _admission())
    monkeypatch.setattr(main, "fetch_active_search_session_by_thread", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "create_search_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "append_search_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "fail_search_session", lambda search_id, error: failed.append((search_id, error)))
    monkeypatch.setattr(
        main,
        "prepare_runtime_plan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            retrieval.SearchAiExecutionError("ai_spatial_landing", "transport_error:timed out")
        ),
    )

    response = TestClient(main.app).post(
        "/memory/query-plan",
        json={"query_text": "Plan reviewed context."},
    )

    assert response.status_code == 503
    payload = response.json()
    assert payload["code"] == "blocked_ai_provider_timeout"
    assert payload["session_created"] is False
    assert payload["chargeable"] is False
    assert payload["charged_units"] == 0
    assert len(failed) == 1
    assert "search_ai_call_failed" in failed[0][1]


def test_monolith_retrieve_blocks_before_runtime_execution(monkeypatch) -> None:
    if not (API_DIR / "main.py").is_file():
        pytest.skip("monolith-only retrieve boundary")
    import main

    monkeypatch.setattr(
        main,
        "_brain_request_scope",
        lambda *args, **kwargs: nullcontext({"brain_id": "brain_test"}),
    )
    monkeypatch.setattr(main, "_attach_brain_metadata", lambda result: result)
    monkeypatch.setattr(
        main,
        "require_search_ai_admission",
        lambda request, identity: {
            "status": "blocked",
            "reason": "blocked_ai_provider_timeout",
            "provider_error": "transport_error:timed out",
            "chargeable": False,
            "charged_units": 0,
        },
    )
    monkeypatch.setattr(main, "fetch_identity_nucleus", lambda: {})
    monkeypatch.setattr(
        main,
        "retrieve_runtime",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("retrieval runtime must not execute")
        ),
    )

    response = main.retrieve_endpoint(
        RetrieveRequest(query_text="Find reviewed context.", response_mode="context")
    )

    assert response.stop_reason == "blocked_ai_provider_timeout"
    assert response.matches == []
    assert response.context_package == {}
    assert response.semantic_contract_runtime["billing"]["charged_units"] == 0


def test_monolith_mcp_retrieve_document_fast_lookup_bypasses_ai_admission(monkeypatch) -> None:
    if not (API_DIR / "main.py").is_file():
        pytest.skip("monolith-only MCP boundary")
    import main

    monkeypatch.setattr(main, "_mcp_nonblocking_enabled", lambda tool_name: False)
    monkeypatch.setattr(
        main,
        "_brain_request_scope",
        lambda *args, **kwargs: nullcontext({"brain_id": "brain_test"}),
    )
    monkeypatch.setattr(main, "_attach_brain_metadata", lambda result: result)
    monkeypatch.setattr(main, "_attach_tool_brain_metadata", lambda result: result)
    monkeypatch.setattr(
        main,
        "_public_search_ai_admission",
        lambda request: (_ for _ in ()).throw(
            AssertionError("exact document lookup must bypass AI admission")
        ),
    )
    monkeypatch.setattr(
        main,
        "_mcp_fast_document_tool_response",
        lambda *args, **kwargs: main.McpToolExecutionResponse(
            schema_version="agvm.mcp_retrieval_tool_output.v1",
            brain_id="brain_test",
            search_id=None,
            tool_name="retrieve_document",
            status="ok",
            result_ready_terminal=True,
            context_package={"status": "document_payload_ready"},
            document_workspace={
                "documents": [
                    {
                        "document_id": "doc-reviewed-local",
                        "title": "Reviewed local document",
                        "full_text": "Reviewed local document full text.",
                    }
                ]
            },
        ),
    )
    monkeypatch.setattr(
        main,
        "_create_planned_search_session",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("search session must not be created")
        ),
    )

    response = main._run_mcp_retrieval_tool(
        "retrieve_document",
        McpRetrievalToolRequest(
            query_text="Open the reviewed document.",
            document_hint="reviewed document",
            include_raw_text=True,
        ),
    )

    assert response.status == "ok"
    assert response.search_id is None
    assert response.semantic_contract_runtime["ai_required"] is False
    assert response.semantic_contract_runtime["provider_state"] == "not_required"
    assert response.semantic_contract_runtime["provider_bypassed"] is True
    assert response.document_workspace["documents"][0]["document_id"] == "doc-reviewed-local"
