# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "agvm_api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from brain_bootstrap_v1.router import create_brain_bootstrap_v1_router  # noqa: E402
import brain_bootstrap_v1.service as bootstrap_service  # noqa: E402
from brain_bootstrap_v1.service import BrainBootstrapV1Service, BootstrapV1Error  # noqa: E402
from brain_bootstrap_v1.store import BootstrapSessionStore, BootstrapStoreError  # noqa: E402
from brain_registry import create_local_brain  # noqa: E402
from mcp_contracts import BRAIN_BOOTSTRAP_V1_MCP_TOOL_NAMES, build_mcp_contract_registry  # noqa: E402
from runtime_scope import use_runtime_brain  # noqa: E402
from sqlite_store import fetch_graph_snapshot, replace_runtime_graph  # noqa: E402
from storage import load_graph, load_graph_view  # noqa: E402


def _brain(tmp_path: Path) -> dict:
    path = tmp_path / "brain"
    path.mkdir()
    return {
        "brain_id": "bootstrap_v1_test_brain",
        "registry_brain_path": str(path),
        "storage_path": str(path),
    }


def _seed_candidate(index: int, *, prefix: str = "Reviewed operational policy") -> dict:
    label = f"domain{index:02x}"
    text = (
        f"{prefix} {label} connects control{label}, evidence{label}, and workflow{label} "
        "to one independently traceable brain decision."
    )
    return {
        "id": f"preview-{index}",
        "preview_id": f"preview-{index}",
        "raw_text": text,
        "summary": text,
        "derivation_role": "claim",
        "memory_type": "knowledge",
        "node_kind": "fact",
        "source_trust": "user_asserted",
        "provenance": {
            "mode": "agvm_lab_preview_claim",
            "source_label": "Reviewed Bootstrap material",
            "source_type": "manual_bootstrap",
        },
    }


def _service(
    tmp_path: Path,
    *,
    fail_apply: bool = False,
    question_generator=None,
) -> tuple[BrainBootstrapV1Service, list[dict]]:
    brain = _brain(tmp_path)
    apply_calls: list[dict] = []
    mutation_state = {"applied": False}

    def resolve_brain(**_kwargs):
        return brain

    def preview_builder(session, _brain_record):
        assert session["answers"] or session["sources"]
        return {
            "schema_version": "agvm.brain_bootstrap_v1.grow_review.v1",
            "preview_bundle": {"derived_nodes": [{"preview_id": "preview-1", "raw_text": "Reviewed seed"}]},
            "selected_preview_ids": ["preview-1"],
            "candidate_count": 1,
            "mutates_brain": False,
        }

    def apply_executor(preview, _brain_record, selected_ids):
        apply_calls.append({"preview": preview, "selected_ids": selected_ids})
        if fail_apply:
            raise RuntimeError("synthetic apply failure")
        mutation_state["applied"] = True
        return {
            "schema_version": "agvm.brain_bootstrap_v1.apply_result.v1",
            "status": "applied",
            "persisted_node_ids": ["node-1"],
            "persisted_edge_count": 0,
        }

    def mutation_probe(_brain_record, receipt):
        if not mutation_state["applied"]:
            return {"state": "not_applied", "reason": "synthetic_graph_unchanged"}
        return {
            "state": "applied",
            "receipt": {**dict(receipt), "verified": True},
            "apply_result": {
                "schema_version": "agvm.brain_bootstrap_v1.apply_result.v1",
                "status": "applied",
                "persisted_node_ids": ["node-1"],
                "persisted_edge_count": 0,
                "recovered_from_mutation_receipt": True,
            },
        }

    return (
        BrainBootstrapV1Service(
            brain_resolver=resolve_brain,
            brain_root_resolver=lambda: tmp_path,
            preview_builder=preview_builder,
            apply_executor=apply_executor,
            mutation_probe=mutation_probe,
            question_generator=question_generator,
        ),
        apply_calls,
    )


def test_bootstrap_v1_registry_exposes_nine_bounded_core_tools() -> None:
    registry = build_mcp_contract_registry()
    tools = {tool["name"]: tool for tool in registry["tools"]}

    assert registry["registry_validation"]["passed"] is True
    assert len(registry["tools"]) == 52
    assert set(BRAIN_BOOTSTRAP_V1_MCP_TOOL_NAMES).issubset(tools)
    for name in BRAIN_BOOTSTRAP_V1_MCP_TOOL_NAMES:
        tool = tools[name]
        assert tool["implementation_status"] == "implemented"
        assert tool["category"] == "agent_memory"
        assert tool["tool_registration"]["public_core_allowed"] is True
        assert tool["tool_registration"]["required_module_id"] is None
        assert tool["tool_registration"]["execution_surface_policy"] == "local_manual_or_cloud_action_contract"
        assert tool["tool_registration"]["cloud_required_capabilities"] == [
            "ai_research",
            "fitting",
            "backfill",
            "activation",
        ]

    allowlist_path = ROOT / "repo-policy" / "public-core-allowlist.txt"
    if allowlist_path.is_file():
        allowlist = allowlist_path.read_text(encoding="utf-8")
        assert "agvm_api/brain_bootstrap_v1/**" in allowlist


def test_bootstrap_v1_is_immutable_cas_guarded_and_writes_only_on_explicit_apply(tmp_path: Path) -> None:
    service, apply_calls = _service(tmp_path)
    start = service.execute(
        "start",
        {
            "brain_id": "bootstrap_v1_test_brain",
            "session_id": "session-a",
            "idempotency_key": "start-a",
            "goal": "Create Lorenzo's reviewed seed",
            "questions": ["What work should this brain remember?"],
        },
    )
    assert start["status"] == "started"
    assert start["revision"] == 1
    store = BootstrapSessionStore(_brain_record_from_response(tmp_path, start), brain_root=tmp_path)
    first_path = store.root / "session-a" / "revisions" / "00000001.json"
    first_bytes = first_path.read_bytes()

    answer = service.execute(
        "answer",
        {
            "brain_id": "bootstrap_v1_test_brain",
            "session_id": "session-a",
            "expected_revision": 1,
            "idempotency_key": "answer-a",
            "question_id": "work",
            "answer": "AGVM product engineering",
        },
    )
    source = service.execute(
        "add_source",
        {
            "brain_id": "bootstrap_v1_test_brain",
            "session_id": "session-a",
            "expected_revision": 2,
            "idempotency_key": "source-a",
            "source_label": "Manual profile",
            "source_text": "Lorenzo builds AGVM.",
        },
    )
    preview = service.execute(
        "preview",
        {
            "brain_id": "bootstrap_v1_test_brain",
            "session_id": "session-a",
            "expected_revision": 3,
            "idempotency_key": "preview-a",
            "capability": "grow_review",
        },
    )

    assert answer["revision"] == 2
    assert source["revision"] == 3
    assert preview["revision"] == 4
    assert preview["session"]["preview"]["candidate_count"] == 1
    assert apply_calls == []
    assert first_path.read_bytes() == first_bytes

    with pytest.raises(BootstrapV1Error, match="bootstrap_confirm_apply_required"):
        service.execute(
            "apply",
            {
                "brain_id": "bootstrap_v1_test_brain",
                "session_id": "session-a",
                "expected_revision": 4,
                "idempotency_key": "apply-a",
                "confirm_apply": False,
            },
        )
    assert apply_calls == []

    applied = service.execute(
        "apply",
        {
            "brain_id": "bootstrap_v1_test_brain",
            "session_id": "session-a",
            "expected_revision": 4,
            "idempotency_key": "apply-a",
            "confirm_apply": True,
            "selected_preview_ids": ["preview-1"],
        },
    )
    assert applied["status"] == "applied"
    assert applied["revision"] == 6
    assert applied["session"]["apply_result"]["persisted_node_ids"] == ["node-1"]
    assert len(apply_calls) == 1
    assert [item["revision"] for item in store.load_history("session-a")] == [1, 2, 3, 4, 5, 6]
    assert first_path.read_bytes() == first_bytes

    replay = service.execute(
        "apply",
        {
            "brain_id": "bootstrap_v1_test_brain",
            "session_id": "session-a",
            "expected_revision": 4,
            "idempotency_key": "apply-a",
            "confirm_apply": True,
            "selected_preview_ids": ["preview-1"],
        },
    )
    assert replay["idempotent_replay"] is True
    assert replay["revision"] == 6
    assert len(apply_calls) == 1


def test_adaptive_bootstrap_generates_domain_questions_and_exposes_runtime_quality_gates(tmp_path: Path) -> None:
    generated_goals: list[str] = []

    def generate_questions(goal, brain_record):
        generated_goals.append(goal)
        assert brain_record["brain_id"] == "bootstrap_v1_test_brain"
        return {
            "schema_version": "agvm.brain_bootstrap_v1.adaptive_interview.v1",
            "generation_source": "provider",
            "questions": [
                f"How should the product intelligence brain handle domain requirement {index}?"
                for index in range(1, 10)
            ],
            "required_answer_count": 8,
            "coverage_dimensions": [f"dimension-{index}" for index in range(1, 10)],
        }

    service, _apply_calls = _service(tmp_path, question_generator=generate_questions)
    started = service.execute(
        "start",
        {
            "brain_id": "bootstrap_v1_test_brain",
            "session_id": "adaptive-interview",
            "idempotency_key": "adaptive-start",
            "goal": "Support product intelligence with reviewed architectural evidence.",
            "interview_mode": bootstrap_service.ADAPTIVE_INTERVIEW_MODE,
            "quality_policy": bootstrap_service.GUIDED_SEED_QUALITY_POLICY,
        },
    )

    session = started["session"]
    assert generated_goals == ["Support product intelligence with reviewed architectural evidence."]
    assert session["interview_mode"] == bootstrap_service.ADAPTIVE_INTERVIEW_MODE
    assert len(session["questions"]) == 9
    assert session["interview_plan"]["generation_source"] == "provider"
    assert session["quality"]["minimum_answer_count"] == 8
    assert session["quality"]["minimum_source_text_chars"] == bootstrap_service.GUIDED_SEED_MIN_SOURCE_TEXT_CHARS
    assert session["quality"]["ready_to_apply"] is False
    assert "more_structured_answers_required" in session["quality"]["issues"]

    with pytest.raises(BootstrapV1Error, match="bootstrap_adaptive_interview_questions_forbidden"):
        service.execute(
            "start",
            {
                "brain_id": "bootstrap_v1_test_brain",
                "session_id": "adaptive-static-questions",
                "idempotency_key": "adaptive-static-start",
                "goal": "A second brain.",
                "interview_mode": bootstrap_service.ADAPTIVE_INTERVIEW_MODE,
                "quality_policy": bootstrap_service.GUIDED_SEED_QUALITY_POLICY,
                "questions": ["This must never replace the provider-authored interview plan."],
            },
        )
def test_guided_bootstrap_cannot_apply_without_a_passing_seed_quality_report(tmp_path: Path) -> None:
    service, apply_calls = _service(tmp_path)
    started = service.execute(
        "start",
        {
            "brain_id": "bootstrap_v1_test_brain",
            "session_id": "guided-quality",
            "idempotency_key": "guided-start",
            "quality_policy": bootstrap_service.GUIDED_SEED_QUALITY_POLICY,
        },
    )
    assert started["session"]["quality_policy"] == bootstrap_service.GUIDED_SEED_QUALITY_POLICY
    service.execute(
        "answer",
        {
            "brain_id": "bootstrap_v1_test_brain",
            "session_id": "guided-quality",
            "expected_revision": 1,
            "idempotency_key": "guided-answer",
            "question_id": "purpose",
            "answer": "A real but deliberately incomplete seed.",
        },
    )
    service.execute(
        "preview",
        {
            "brain_id": "bootstrap_v1_test_brain",
            "session_id": "guided-quality",
            "expected_revision": 2,
            "idempotency_key": "guided-preview",
        },
    )

    with pytest.raises(BootstrapV1Error, match="bootstrap_seed_quality_gate_not_met"):
        service.execute(
            "apply",
            {
                "brain_id": "bootstrap_v1_test_brain",
                "session_id": "guided-quality",
                "expected_revision": 3,
                "idempotency_key": "guided-apply",
                "confirm_apply": True,
            },
        )
    assert apply_calls == []


def test_guided_bootstrap_quality_reports_real_material_and_unique_candidates() -> None:
    answers = [{"answer": f"Reviewed answer {index}"} for index in range(6)]
    derived_nodes = [_seed_candidate(index) for index in range(12)]
    source_text = " ".join(str(item["raw_text"]) for item in derived_nodes)

    quality = bootstrap_service._bootstrap_seed_quality(
        session={
            "quality_policy": bootstrap_service.GUIDED_SEED_QUALITY_POLICY,
            "answers": answers,
            "sources": [{"source_text": source_text}],
        },
        bundle={"derived_nodes": derived_nodes},
        selected_ids=[item["preview_id"] for item in derived_nodes],
    )

    assert quality["ready_to_apply"] is True
    assert quality["candidate_count"] == 12
    assert quality["unique_candidate_count"] == 12
    assert quality["ungrounded_candidate_count"] == 0
    assert quality["issues"] == []


def test_guided_bootstrap_quality_enforces_inclusive_12_to_30_candidate_bounds() -> None:
    derived_nodes = [_seed_candidate(index, prefix="Reviewed bounded policy") for index in range(31)]
    session = {
        "quality_policy": bootstrap_service.GUIDED_SEED_QUALITY_POLICY,
        "answers": [{"answer": f"Reviewed answer {index}"} for index in range(6)],
        "sources": [{"source_text": " ".join(str(item["raw_text"]) for item in derived_nodes)}],
    }
    bundle = {"derived_nodes": derived_nodes}

    too_few = bootstrap_service._bootstrap_seed_quality(
        session=session,
        bundle=bundle,
        selected_ids=[f"preview-{index}" for index in range(11)],
    )
    minimum = bootstrap_service._bootstrap_seed_quality(
        session=session,
        bundle=bundle,
        selected_ids=[f"preview-{index}" for index in range(12)],
    )
    maximum = bootstrap_service._bootstrap_seed_quality(
        session=session,
        bundle=bundle,
        selected_ids=[f"preview-{index}" for index in range(30)],
    )
    too_many = bootstrap_service._bootstrap_seed_quality(
        session=session,
        bundle=bundle,
        selected_ids=[f"preview-{index}" for index in range(31)],
    )

    assert too_few["ready_to_apply"] is False
    assert "too_few_atomic_candidates" in too_few["issues"]
    assert minimum["ready_to_apply"] is True
    assert maximum["ready_to_apply"] is True
    assert too_many["ready_to_apply"] is False
    assert "too_many_atomic_candidates" in too_many["issues"]


def test_guided_bootstrap_apply_rechecks_the_actual_selected_subset(tmp_path: Path) -> None:
    service, apply_calls = _service(tmp_path)
    derived_nodes = [_seed_candidate(index, prefix="Reviewed selected policy") for index in range(12)]
    candidate_texts = [str(item["raw_text"]) for item in derived_nodes]
    service._preview_builder = lambda *_args: {
        "schema_version": "agvm.brain_bootstrap_v1.grow_review.v1",
        "preview_bundle": {"derived_nodes": derived_nodes},
        "selected_preview_ids": [item["preview_id"] for item in derived_nodes],
        "candidate_count": len(derived_nodes),
        "quality": {"ready_to_apply": True},
        "mutates_brain": False,
    }
    service.execute(
        "start",
        {
            "session_id": "guided-selected-subset",
            "idempotency_key": "guided-selected-start",
            "quality_policy": bootstrap_service.GUIDED_SEED_QUALITY_POLICY,
        },
    )
    revision = 1
    for index in range(6):
        service.execute(
            "answer",
            {
                "session_id": "guided-selected-subset",
                "expected_revision": revision,
                "idempotency_key": f"guided-selected-answer-{index}",
                "question_id": f"question-{index}",
                "answer": f"Reviewed answer {index}",
            },
        )
        revision += 1
    service.execute(
        "add_source",
        {
            "session_id": "guided-selected-subset",
            "expected_revision": revision,
            "idempotency_key": "guided-selected-source",
            "source_text": " ".join(candidate_texts),
        },
    )
    revision += 1
    service.execute(
        "preview",
        {
            "session_id": "guided-selected-subset",
            "expected_revision": revision,
            "idempotency_key": "guided-selected-preview",
        },
    )
    revision += 1

    with pytest.raises(BootstrapV1Error, match="bootstrap_seed_quality_gate_not_met"):
        service.execute(
            "apply",
            {
                "session_id": "guided-selected-subset",
                "expected_revision": revision,
                "idempotency_key": "guided-selected-apply-eleven",
                "confirm_apply": True,
                "selected_preview_ids": [f"preview-{index}" for index in range(11)],
            },
        )
    assert apply_calls == []

    applied = service.execute(
        "apply",
        {
            "session_id": "guided-selected-subset",
            "expected_revision": revision,
            "idempotency_key": "guided-selected-apply-twelve",
            "confirm_apply": True,
            "selected_preview_ids": [f"preview-{index}" for index in range(12)],
        },
    )
    assert applied["status"] == "applied"
    assert apply_calls[0]["selected_ids"] == [f"preview-{index}" for index in range(12)]


def test_guided_bootstrap_quality_rejects_ungrounded_candidates() -> None:
    candidates = [_seed_candidate(index, prefix="Reviewed grounded policy") for index in range(12)]
    supported = [str(item["raw_text"]) for item in candidates]
    candidates[-1]["raw_text"] = "Invented Zephyr Corporation acquired an orbital laboratory in 2047."

    quality = bootstrap_service._bootstrap_seed_quality(
        session={
            "quality_policy": bootstrap_service.GUIDED_SEED_QUALITY_POLICY,
            "answers": [{"answer": f"Reviewed answer {index}"} for index in range(6)],
            "sources": [{"source_text": " ".join(supported)}],
        },
        bundle={"derived_nodes": candidates},
        selected_ids=[item["preview_id"] for item in candidates],
    )

    assert quality["ready_to_apply"] is False
    assert quality["ungrounded_candidate_count"] == 1
    assert quality["ungrounded_candidate_ids"] == ["preview-11"]
    assert "ungrounded_candidates_detected" in quality["issues"]


def test_bootstrap_v1_rejects_stale_cas_and_idempotency_key_reuse(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    service.execute("start", {"session_id": "session-b", "idempotency_key": "start-b"})
    service.execute(
        "answer",
        {
            "session_id": "session-b",
            "expected_revision": 1,
            "idempotency_key": "answer-b",
            "question_id": "q1",
            "answer": "first",
        },
    )
    with pytest.raises(BootstrapV1Error, match="bootstrap_revision_conflict"):
        service.execute(
            "answer",
            {
                "session_id": "session-b",
                "expected_revision": 1,
                "idempotency_key": "answer-c",
                "question_id": "q2",
                "answer": "stale",
            },
        )
    with pytest.raises(Exception, match="bootstrap_idempotency_key_reused_with_different_request"):
        service.execute(
            "answer",
            {
                "session_id": "session-b",
                "expected_revision": 1,
                "idempotency_key": "answer-b",
                "question_id": "q1",
                "answer": "different",
            },
        )


@pytest.mark.parametrize("capability", ["ai_research", "fitting", "backfill", "activation"])
def test_bootstrap_v1_ai_capabilities_return_cloud_action_contract_without_local_mutation(
    tmp_path: Path, capability: str
) -> None:
    service, apply_calls = _service(tmp_path)
    started = service.execute("start", {"session_id": "session-cloud", "idempotency_key": "start-cloud"})
    response = service.execute(
        "preview",
        {
            "session_id": "session-cloud",
            "expected_revision": 1,
            "idempotency_key": f"cloud-{capability}",
            "capability": capability,
        },
    )
    assert response["status"] == "cloud_required"
    assert response["revision"] == 1
    assert response["session_id"] == "session-cloud"
    assert response["action_contract"]["capability"] == capability
    assert response["action_contract"]["execution_surface"] == "detwin_cloud"
    assert response["action_contract"]["requires_dynamic_usage_settlement"] is True
    assert response["action_contract"]["local_session_mutated"] is False
    assert apply_calls == []
    assert started["revision"] == 1


def test_bootstrap_v1_failed_apply_requires_manual_recovery_and_never_auto_reapplies(tmp_path: Path) -> None:
    service, apply_calls = _service(tmp_path, fail_apply=True)
    service.execute("start", {"session_id": "session-recover", "idempotency_key": "start-r"})
    service.execute(
        "answer",
        {
            "session_id": "session-recover",
            "expected_revision": 1,
            "idempotency_key": "answer-r",
            "question_id": "q",
            "answer": "review me",
        },
    )
    service.execute(
        "preview",
        {
            "session_id": "session-recover",
            "expected_revision": 2,
            "idempotency_key": "preview-r",
        },
    )
    failed = service.execute(
        "apply",
        {
            "session_id": "session-recover",
            "expected_revision": 3,
            "idempotency_key": "apply-r",
            "confirm_apply": True,
        },
    )
    assert failed["status"] == "recovery_required"
    assert failed["revision"] == 5
    assert len(apply_calls) == 1

    recovered = service.execute(
        "recover",
        {
            "session_id": "session-recover",
            "expected_revision": 5,
            "idempotency_key": "recover-r",
        },
    )
    assert recovered["status"] == "recovered"
    assert recovered["session"]["recovery"]["automatic_reapply_allowed"] is False
    assert len(apply_calls) == 1


def test_bootstrap_v1_hard_crash_after_mutation_recovers_without_double_apply(tmp_path: Path) -> None:
    brain = _brain(tmp_path)
    state = {"applied": False, "calls": 0}

    def resolve_brain(**_kwargs):
        return brain

    def preview_builder(_session, _brain_record):
        return {
            "schema_version": "agvm.brain_bootstrap_v1.grow_review.v1",
            "preview_bundle": {"derived_nodes": [{"preview_id": "preview-1", "raw_text": "Crash-safe seed"}]},
            "selected_preview_ids": ["preview-1"],
            "candidate_count": 1,
            "mutates_brain": False,
        }

    def crashing_apply(_preview, _brain_record, _selected_ids):
        state["calls"] += 1
        state["applied"] = True
        raise KeyboardInterrupt("crash after canonical graph commit")

    def mutation_probe(_brain_record, receipt):
        if not state["applied"]:
            return {"state": "not_applied", "reason": "synthetic_graph_unchanged"}
        return {
            "state": "applied",
            "receipt": {**dict(receipt), "verified": True, "post_graph_digest": "post-crash"},
            "apply_result": {
                "schema_version": "agvm.brain_bootstrap_v1.apply_result.v1",
                "status": "applied",
                "persisted_node_ids": ["node-crash-safe"],
                "persisted_edge_count": 0,
                "recovered_from_mutation_receipt": True,
            },
        }

    service = BrainBootstrapV1Service(
        brain_resolver=resolve_brain,
        brain_root_resolver=lambda: tmp_path,
        preview_builder=preview_builder,
        apply_executor=crashing_apply,
        mutation_probe=mutation_probe,
    )
    service.execute("start", {"session_id": "session-hard-crash", "idempotency_key": "start-hard-crash"})
    service.execute(
        "answer",
        {
            "session_id": "session-hard-crash",
            "expected_revision": 1,
            "idempotency_key": "answer-hard-crash",
            "question_id": "q",
            "answer": "reviewed",
        },
    )
    service.execute(
        "preview",
        {
            "session_id": "session-hard-crash",
            "expected_revision": 2,
            "idempotency_key": "preview-hard-crash",
        },
    )
    with pytest.raises(KeyboardInterrupt, match="crash after canonical graph commit"):
        service.execute(
            "apply",
            {
                "session_id": "session-hard-crash",
                "expected_revision": 3,
                "idempotency_key": "apply-hard-crash",
                "confirm_apply": True,
            },
        )

    pending = BootstrapSessionStore(brain, brain_root=tmp_path).load_latest("session-hard-crash")
    assert pending["lifecycle_state"] == "apply_pending"
    assert pending["apply_intent"]["mutation_receipt"]["mutation_id"].startswith("bbmut_")

    recovered = service.execute(
        "recover",
        {
            "session_id": "session-hard-crash",
            "expected_revision": 4,
            "idempotency_key": "recover-hard-crash",
        },
    )
    assert recovered["status"] == "applied"
    assert recovered["session"]["recovery"]["state"] == "already_applied_verified"
    assert recovered["session"]["apply_result"]["persisted_node_ids"] == ["node-crash-safe"]
    assert state["calls"] == 1


def test_bootstrap_recovery_rejects_unrelated_graph_mutation_even_when_witness_preexists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    brain = _brain(tmp_path)
    graphs = {
        "current": {
            "nodes": [
                {"id": "existing-witness", "raw_text": "Reviewed seed"},
                {"id": "stable", "raw_text": "Stable memory"},
            ],
            "edges": [],
        }
    }
    monkeypatch.setattr(bootstrap_service, "bootstrap_runtime_store", lambda: None)
    monkeypatch.setattr(bootstrap_service, "fetch_graph_snapshot", lambda: graphs["current"])
    preview = {
        "preview_bundle": {
            "derived_nodes": [{"preview_id": "preview-1", "raw_text": "Reviewed seed"}],
        }
    }
    receipt = bootstrap_service._build_mutation_receipt(
        brain_record=brain,
        session={"session_id": "scope-safe"},
        preview=preview,
        selected_ids=["preview-1"],
        idempotency_key="apply-scope-safe",
    )
    graphs["current"] = {
        "nodes": [
            *graphs["current"]["nodes"],
            {"id": "unrelated", "raw_text": "Unrelated concurrent mutation"},
        ],
        "edges": [],
    }

    observation = bootstrap_service._probe_manual_grow_mutation(brain, receipt)

    assert observation == {
        "state": "ambiguous",
        "reason": "unrelated_graph_mutation_detected",
    }


def test_bootstrap_recovery_accepts_the_single_implicit_primary_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    brain = _brain(tmp_path)
    graphs = {"current": {"nodes": [], "edges": []}}
    monkeypatch.setattr(bootstrap_service, "bootstrap_runtime_store", lambda: None)
    monkeypatch.setattr(bootstrap_service, "fetch_graph_snapshot", lambda: graphs["current"])
    preview = {
        "preview_bundle": {
            "primary_node_preview": {
                "id": "preview-primary",
                "raw_text": "Reviewed root",
                "summary": "Reviewed root",
                "memory_type": "project",
                "node_kind": "project",
            },
            "derived_nodes": [
                {
                    "preview_id": "preview-1",
                    "raw_text": "Reviewed seed",
                    "summary": "Reviewed seed",
                    "memory_type": "knowledge",
                    "node_kind": "fact",
                }
            ],
        }
    }
    receipt = bootstrap_service._build_mutation_receipt(
        brain_record=brain,
        session={"session_id": "implicit-primary"},
        preview=preview,
        selected_ids=["preview-1"],
        idempotency_key="apply-implicit-primary",
    )
    graphs["current"] = {
        "nodes": [
            {
                "id": "root-node",
                "raw_text": "Reviewed root",
                "summary": "Reviewed root",
                "memory_type": "project",
                "node_kind": "project",
            },
            {
                "id": "seed-node",
                "raw_text": "Reviewed seed",
                "summary": "Reviewed seed",
                "memory_type": "knowledge",
                "node_kind": "fact",
            },
        ],
        "edges": [
            {
                "source_node_id": "root-node",
                "target_node_id": "seed-node",
                "edge_type": "derives_from",
            }
        ],
    }

    observation = bootstrap_service._probe_manual_grow_mutation(brain, receipt)

    assert observation["state"] == "applied"
    assert set(observation["apply_result"]["persisted_node_ids"]) == {"root-node", "seed-node"}


def test_bootstrap_v1_rejects_registry_brain_path_outside_canonical_root(tmp_path: Path) -> None:
    root = tmp_path / "brains"
    root.mkdir()
    outside = tmp_path / "outside" / "brain"
    outside.mkdir(parents=True)
    with pytest.raises(BootstrapStoreError, match="brain_scope_outside_brain_root") as error:
        BootstrapSessionStore(
            {"brain_id": "outside", "registry_brain_path": str(outside)},
            brain_root=root,
        )
    assert error.value.status_code == 403


def test_bootstrap_v1_cancel_and_resume_are_explicit_cas_transitions(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    service.execute("start", {"session_id": "session-resume", "idempotency_key": "start-resume"})
    cancelled = service.execute(
        "cancel",
        {
            "session_id": "session-resume",
            "expected_revision": 1,
            "idempotency_key": "cancel-resume",
        },
    )
    assert cancelled["revision"] == 2
    assert cancelled["lifecycle_state"] == "cancelled"
    with pytest.raises(BootstrapV1Error, match="bootstrap_session_cancelled_resume_required"):
        service.execute(
            "answer",
            {
                "session_id": "session-resume",
                "expected_revision": 2,
                "idempotency_key": "answer-cancelled",
                "question_id": "q",
                "answer": "must resume first",
            },
        )

    resumed = service.execute(
        "resume",
        {
            "session_id": "session-resume",
            "expected_revision": 2,
            "idempotency_key": "resume-session",
        },
    )
    assert resumed["revision"] == 3
    assert resumed["lifecycle_state"] == "interview_active"


def test_bootstrap_v1_real_local_grow_review_writes_only_after_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    brain_root = tmp_path / "brains"
    monkeypatch.setenv("AGVM_BRAINS_DIR", str(brain_root))
    monkeypatch.setenv("AGVM_LLM_ENABLED", "false")
    create_local_brain(
        display_name="Bootstrap V1 Integration",
        brain_id="bootstrap_v1_integration",
        make_default=True,
        make_active=True,
        brain_root=brain_root,
    )
    service = BrainBootstrapV1Service()
    service.execute(
        "start",
        {
            "brain_id": "bootstrap_v1_integration",
            "session_id": "integration-session",
            "idempotency_key": "integration-start",
        },
    )
    service.execute(
        "answer",
        {
            "brain_id": "bootstrap_v1_integration",
            "session_id": "integration-session",
            "expected_revision": 1,
            "idempotency_key": "integration-answer",
            "question_id": "identity",
            "answer": "Lorenzo builds AGVM memory systems and explicitly reviews bootstrap candidates.",
        },
    )
    preview = service.execute(
        "preview",
        {
            "brain_id": "bootstrap_v1_integration",
            "session_id": "integration-session",
            "expected_revision": 2,
            "idempotency_key": "integration-preview",
        },
    )
    assert preview["status"] == "preview_ready"
    assert preview["session"]["preview"]["candidate_count"] > 0
    storage = brain_root / "bootstrap_v1_integration" / "storage"
    graph_before = (storage / "beta_vector_memory.graph.json").read_text(encoding="utf-8")

    applied = service.execute(
        "apply",
        {
            "brain_id": "bootstrap_v1_integration",
            "session_id": "integration-session",
            "expected_revision": 3,
            "idempotency_key": "integration-apply",
            "confirm_apply": True,
        },
    )
    assert applied["status"] == "applied"
    assert applied["session"]["apply_result"]["persisted_node_ids"]
    assert (storage / "beta_vector_memory.graph.json").read_text(encoding="utf-8") != graph_before


def test_bootstrap_grow_large_graph_exports_canonical_sqlite_edges(tmp_path: Path) -> None:
    nodes = [
        {
            "id": f"node-{index:04d}",
            "node_kind": "memory",
            "memory_type": "fact",
            "raw_text": f"Memory {index}",
            "summary": f"Memory {index}",
            "final_position": {"x": float(index % 10), "y": 0.0, "z": 0.0},
            "links": [],
            "highways": [],
        }
        for index in range(601)
    ]
    edge = {
        "source_node_id": "node-0599",
        "target_node_id": "node-0600",
        "edge_type": "semantic_link",
        "confidence": 0.9,
        "reason": "Bootstrap Grow relation",
    }
    graph = {
        "version": "test",
        "graph_name": "bootstrap-large",
        "nodes": nodes,
        "edges": [edge, dict(edge)],
    }
    brain = {"brain_id": "bootstrap_large_graph", "storage_path": str(tmp_path / "storage")}

    with use_runtime_brain(brain):
        saved = replace_runtime_graph(graph)
        sqlite_graph = fetch_graph_snapshot()
        graph_export = load_graph()
        view_export = load_graph_view()

    assert len(saved["edges"]) == 1
    assert saved["edges"] == sqlite_graph["edges"] == graph_export["edges"] == view_export["edges"]
    assert graph_export["meta"]["edge_count"] == 1
    assert view_export["meta"]["total_edge_count"] == 1
    assert view_export["meta"]["sampled_edge_count"] == 1


def test_bootstrap_v1_router_maps_domain_errors_and_exposes_status(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    app = FastAPI()
    app.include_router(create_brain_bootstrap_v1_router(service))
    client = TestClient(app)

    started = client.post(
        "/mcp/brain-bootstrap-start",
        json={"session_id": "session-http", "idempotency_key": "start-http"},
    )
    assert started.status_code == 200
    status = client.post(
        "/memory/mcp/brain-bootstrap-status",
        json={"session_id": "session-http"},
    )
    assert status.status_code == 200
    assert status.json()["revision"] == 1
    add_source = client.post(
        "/mcp/brain-bootstrap-add-source",
        json={
            "session_id": "session-http",
            "expected_revision": 1,
            "idempotency_key": "source-http",
            "source_text": "Reviewed source",
        },
    )
    assert add_source.status_code == 200
    assert add_source.json()["revision"] == 2
    stale = client.post(
        "/mcp/brain-bootstrap-answer",
        json={
            "session_id": "session-http",
            "expected_revision": 9,
            "idempotency_key": "stale-http",
            "question_id": "q",
            "answer": "a",
        },
    )
    assert stale.status_code == 409
    assert "bootstrap_revision_conflict" in stale.json()["detail"]


def _brain_record_from_response(tmp_path: Path, _response: dict) -> dict:
    return {"brain_id": "bootstrap_v1_test_brain", "registry_brain_path": str(tmp_path / "brain")}
