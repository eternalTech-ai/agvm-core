# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import sys
from contextlib import nullcontext
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
        ai_admission=_admission(),
    )

    runtime = plan["semantic_contract_runtime"]
    assert runtime["provider_call_performed"] is False
    assert runtime["origin_call_name"] == "planner_seed_admission"
    assert "ai_execution_attestation" not in runtime


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


def test_second_ai_call_failure_discards_buffered_stream_and_result(monkeypatch) -> None:
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
            event_callback("context_update", {"matches": [{"node_id": "must_not_escape"}]})
        _runtime_ai_call("navigation_actions")
        return {"matches": [{"node_id": "must_not_escape"}], "planner_runtime": {}}

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

    assert result["stop_reason"] == "blocked_ai_provider_timeout"
    assert result["matches"] == []
    assert result["billing"]["charged_units"] == 0
    assert result["search_ai_execution"]["failed_call"] == "navigation_actions"
    assert [event_type for event_type, _ in events] == ["search_blocked", "result_ready"]
    assert all("must_not_escape" not in str(payload) for _, payload in events)


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


def test_monolith_mcp_blocks_before_fast_lookup_or_session(monkeypatch) -> None:
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
        lambda request: {
            "status": "blocked",
            "reason": "blocked_ai_provider_invalid_output",
            "provider_error": "invalid_json",
            "chargeable": False,
            "charged_units": 0,
        },
    )
    monkeypatch.setattr(
        main,
        "_mcp_fast_document_tool_response",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("fast document lookup must not execute")
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

    assert response.status == "blocked"
    assert response.search_id is None
    assert response.context_package == {}
    assert response.semantic_contract_runtime["provider_error"] == "invalid_json"
    assert response.semantic_contract_runtime["billing"]["charged_units"] == 0
