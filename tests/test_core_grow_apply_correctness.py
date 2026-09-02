# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "agvm_api"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

import core_mcp_ops_router as grow_router  # noqa: E402
import core_graph_router  # noqa: E402
import sqlite_store  # noqa: E402
from ai_modules_v2 import AiModuleContractError  # noqa: E402
from investigative_agent import aggregate_execution_ledger, execution_ledger_entry  # noqa: E402
from runtime_scope import use_runtime_brain  # noqa: E402


def _node(node_id: str, summary: str) -> dict[str, Any]:
    return {
        "id": node_id,
        "node_kind": "memory",
        "memory_type": "fact",
        "raw_text": summary,
        "summary": summary,
        "final_position": {"x": 0.0, "y": 0.0, "z": 0.0},
        "links": [],
        "highways": [],
    }


def _graph(summary: str = "Existing reviewed memory") -> dict[str, Any]:
    return {
        "version": "test",
        "graph_name": "local-grow-v2-test",
        "nodes": [_node("existing-node", summary)],
        "edges": [],
    }


def _preview_bundle(*, include_derived: bool = True) -> dict[str, Any]:
    return {
        "schema_version": "agvm.grow_preview_bundle.v2",
        "primary_node_preview": {
            "id": "preview-primary",
            "summary": "Server-issued primary memory",
            "raw_text": "Server-issued primary memory",
        },
        "derived_nodes": (
            [
                {
                    "id": "preview-derived",
                    "summary": "Server-issued derived memory",
                    "raw_text": "Server-issued derived memory",
                }
            ]
            if include_derived
            else []
        ),
    }


def _attestation(seed: str = "grow-v2") -> dict[str, Any]:
    return {
        "schema_version": "agvm.ai_execution_attestation.v2",
        "status": "completed",
        "provider_executed": True,
        "applicable": True,
        "legacy_read_only": False,
        "provider": "openai_compatible",
        "model": "gpt-4.1-mini",
        "request_sha256": hashlib.sha256(f"request:{seed}".encode()).hexdigest(),
        "output_sha256": hashlib.sha256(f"output:{seed}".encode()).hexdigest(),
        "usage": {
            "input_tokens": 120,
            "output_tokens": 48,
            "reasoning_tokens": 12,
            "total_tokens": 180,
        },
    }


def _grow_v3_execution(*, include_compiler: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    roles = ["grow_investigator", *(["compiler"] if include_compiler else [])]
    ledger = [
        execution_ledger_entry(
            role=role,
            call_id=f"{role}-call",
            attestation=_attestation(role),
            brain_revision="brain-revision-test",
            parent_operation_id="grow-preview::router-boundary",
            billing_scope="parent_grow_preview",
        )
        for role in roles
    ]
    return ledger, aggregate_execution_ledger(ledger, complete=True, applicable=True)


def _maintenance_feedback_packet(seed: str = "structural") -> dict[str, Any]:
    return {
        "schema_version": "agvm.maintenance_feedback.v3",
        "feedback_id": f"grow-maint-feedback::{seed}",
        "payload_sha256": hashlib.sha256(f"packet:{seed}".encode()).hexdigest(),
        "brain_id": "brain-router",
        "investigation_id": "router-boundary",
        "investigation_version": 2,
        "stage": "deferred",
        "claim_id": f"claim-{seed}",
        "decision_id": f"decision-{seed}",
        "claim_decision": "duplicate",
        "brain_revision_before": "brain-revision-test",
        "brain_revision_after": None,
        "source_sha256": hashlib.sha256(f"source:{seed}".encode()).hexdigest(),
        "source_span_sha256": hashlib.sha256(f"span:{seed}".encode()).hexdigest(),
        "evidence_receipt_ids": [f"receipt-{seed}"],
        "evidence_refs": [f"receipt-{seed}", "existing-node"],
        "hydrated_target_digests": [
            {"target_node_id": "existing-node", "digest": f"digest-{seed}"}
        ],
        "target_node_ids": ["existing-node"],
        "persisted_node_ids": [],
        "apply_receipt_sha256": None,
        "ai_execution_ledger_sha256": hashlib.sha256(f"ledger:{seed}".encode()).hexdigest(),
        "temporal_authority": {"authority": "ai_investigator_evidence_bound"},
        "maintenance_intent": {
            "lane": "sleep_review",
            "reason": "The AI review found a structural duplicate.",
            "expected_decision_change": "Maintenance should decide whether to consolidate nodes.",
            "requested_capabilities": ["structural_memory_review"],
        },
        "state": "deferred_to_maintenance",
    }


def _brain(tmp_path: Path, brain_id: str) -> dict[str, Any]:
    brain = {
        "brain_id": brain_id,
        "storage_path": str(tmp_path / brain_id),
        "node_count": 1,
    }
    with use_runtime_brain(brain):
        sqlite_store.replace_runtime_graph(_graph())
    return brain


def _patch_brain_resolution(
    monkeypatch: pytest.MonkeyPatch,
    brains: dict[str, dict[str, Any]],
    *,
    active_brain_id: str,
) -> None:
    def resolve(brain_id: str | None = None) -> dict[str, Any]:
        return brains[str(brain_id or active_brain_id)]

    monkeypatch.setattr(grow_router, "_resolve_brain_record", resolve)
    monkeypatch.setattr(grow_router, "_resolve_bootstrap_ready_brain_record", resolve)


def _store_preview(
    brain: dict[str, Any],
    *,
    investigation_id: str = "grow-investigation-1",
    bundle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    preview = deepcopy(bundle or _preview_bundle())
    source_investigation = {
        "schema_version": "agvm.mcp_source_investigation.v1",
        "investigation_id": investigation_id,
        "brain_id": brain["brain_id"],
        "status": "preview_ready",
        "source_units": [{"unit_id": f"source-{investigation_id}"}],
    }
    source_formation_contract = {
        "schema_version": "agvm.core_source_formation_contract.v1",
        "investigation_id": investigation_id,
        "state": "preview_ready",
        "mutates_memory": False,
        "apply_contract": {
            "can_apply_now": True,
            "blocked_reasons": [],
        },
    }
    with use_runtime_brain(brain):
        expected_revision = sqlite_store.current_grow_preview_brain_revision(brain["brain_id"])
        return sqlite_store.store_local_grow_v2_preview(
            brain_id=brain["brain_id"],
            investigation_id=investigation_id,
            tool_name="grow_source_preview",
            source_investigation=source_investigation,
            source_formation_contract=source_formation_contract,
            preview_bundle=preview,
            ai_execution_attestation=_attestation(investigation_id),
            investigation_session={
                "schema_version": "agvm.investigation_session.v2",
                "status": "sufficient",
                "provider_attested": True,
            },
            expected_brain_revision=expected_revision,
        )


def _patch_persist_selection(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    calls = {"persist": 0}

    def persist(
        bundle: dict[str, Any],
        selected_ids: list[str],
        graph: dict[str, Any],
        index: dict[str, Any],
        **_: Any,
    ) -> tuple[dict[str, Any], list[str], int, list[str], dict[str, Any]]:
        del bundle, index
        calls["persist"] += 1
        node_id = f"persisted-{selected_ids[0]}"
        updated = deepcopy(graph)
        updated["nodes"].append(_node(node_id, f"Committed {selected_ids[0]}"))
        return updated, [node_id], 1, [], {"mode": "strict_review"}

    monkeypatch.setattr(grow_router, "persist_selection", persist)
    monkeypatch.setattr(grow_router, "build_index", lambda nodes: {"node_count": len(nodes)})
    return calls


def _apply(
    brain_id: str,
    *,
    investigation_id: str = "grow-investigation-1",
    selected_preview_ids: list[str] | None = None,
    preview_bundle: dict[str, Any] | None = None,
) -> grow_router.McpGrowToolExecutionResponse:
    return grow_router._grow_source_apply(
        "grow_source_apply",
        grow_router.McpGrowApplyRequest(
            brain_id=brain_id,
            investigation_id=investigation_id,
            selected_preview_ids=selected_preview_ids or ["preview-primary"],
            preview_bundle=preview_bundle,
            confirm_apply=True,
        ),
    )


def _apply_direct_mutation(
    brain: dict[str, Any],
    *,
    investigation_id: str,
    mutate_graph: Any,
) -> dict[str, Any]:
    stored = _store_preview(
        brain,
        investigation_id=investigation_id,
        bundle=_preview_bundle(include_derived=False),
    )
    apply_material = {
        "investigation_id": investigation_id,
        "selected_preview_ids": ["preview-primary"],
    }
    with use_runtime_brain(brain):
        return sqlite_store.apply_local_grow_v2_preview_transaction(
            brain_id=brain["brain_id"],
            investigation_id=investigation_id,
            selected_preview_ids=["preview-primary"],
            apply_fingerprint=sqlite_store.canonical_sha256(apply_material),
            apply_material=apply_material,
            preview_sha256=str(stored["preview_sha256"]),
            attestation_sha256=str(stored["attestation_sha256"]),
            mutate_graph=mutate_graph,
        )


def test_source_bound_timeout_recovery_preview_is_durable_and_applyable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    brain = _brain(tmp_path, "brain-source-bound-timeout")
    _patch_brain_resolution(
        monkeypatch,
        {brain["brain_id"]: brain},
        active_brain_id=brain["brain_id"],
    )
    monkeypatch.setattr(
        core_graph_router,
        "_resolve_brain_record",
        lambda brain_id=None: brain,
    )
    source_text = (
        "Project Aster requires a signed source review before customer-facing "
        "memory is persisted, and owner Dana retains the exact source receipt."
    )

    class TimedOutGrowEngine:
        def preview(self, **kwargs: Any) -> dict[str, Any]:
            investigation_id = str(kwargs["source_investigation_id"])
            return {
                "schema_version": "agvm.grow_engine_result.v3",
                "status": "incomplete",
                "preview_bundle": None,
                "ai_execution_attestation": {},
                "ai_execution_ledger": [],
                "clarification_questions": [],
                "maintenance_feedback": [],
                "investigation": {
                    "schema_version": "agvm.grow_investigation.v3",
                    "investigation_id": investigation_id,
                    "state": "investigating",
                    "status": "INCOMPLETE",
                    "complete": False,
                    "applicable": False,
                    "failure": {
                        "code": "provider_timeout",
                        "detail": "provider_timeout_after_13.000s",
                    },
                },
                "investigation_session": {
                    "schema_version": "agvm.investigation_session.v3",
                    "status": "incomplete",
                    "provider_attested": False,
                },
                "usage": {},
                "apply_ready": False,
            }

    monkeypatch.setattr(grow_router, "_GROW_ENGINE", TimedOutGrowEngine())
    monkeypatch.setattr(grow_router, "llm_runtime_status", lambda: {})
    app = FastAPI()
    app.include_router(grow_router.create_core_mcp_ops_router())
    app.include_router(core_graph_router.create_core_graph_router())
    client = TestClient(app)

    preview_response = client.post(
        "/memory/mcp/grow-source-preview",
        json={
            "brain_id": brain["brain_id"],
            "raw_input": source_text,
            "input_kind": "manual_text",
            "source_label": "Aster source",
            "run_preview": True,
        },
    )
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()

    assert preview["status"] == "preview_ready"
    assert preview["investigation_id"].startswith("mcp-grow-source-bound-")
    assert preview["investigation_session"]["provider_recovery_reason"] == (
        "semantic_provider_timeout"
    )
    assert preview["selected_preview_ids"]
    source_unit = preview["source_investigation"]["source_units"][0]
    expected_digest = f"sha256:{hashlib.sha256(source_text.encode()).hexdigest()}"
    assert source_unit["content_digest"] == expected_digest
    assert source_unit["provenance"]["hash"] == expected_digest
    assert source_unit["acquisition_proof"]["content_digest"] == expected_digest
    assert preview["source_investigation"]["source_request"]["source_trust"] == "user_asserted"
    preview_nodes = [
        preview["preview_bundle"]["primary_node_preview"],
        *preview["preview_bundle"]["derived_nodes"],
    ]
    assert preview_nodes
    assert all(node["source_trust"] == "user_asserted" for node in preview_nodes)

    with use_runtime_brain(brain):
        stored = sqlite_store.fetch_local_grow_v2_preview(
            brain_id=brain["brain_id"],
            investigation_id=str(preview["investigation_id"]),
        )
    assert stored is not None
    assert stored["preview_bundle"] == preview["preview_bundle"]
    assert stored["source_investigation"] == preview["source_investigation"]
    assert stored["preview_authority_kind"] == "deterministic_document"
    assert stored["deterministic_source_attestation"]["provider_executed"] is False
    assert stored["deterministic_source_attestation"]["semantic_claims_emitted"] is False

    apply_response = client.post(
        "/memory/mcp/grow-source-apply",
        json={
            "brain_id": brain["brain_id"],
            "investigation_id": preview["investigation_id"],
            "selected_preview_ids": preview["selected_preview_ids"],
            "confirm_apply": True,
        },
    )
    assert apply_response.status_code == 200, apply_response.text
    applied = apply_response.json()

    assert applied["status"] == "applied"
    assert applied["persisted_node_ids"]
    assert applied["apply_receipt"]["investigation_id"] == preview["investigation_id"]
    assert applied["apply_receipt"]["selected_preview_ids"] == preview["selected_preview_ids"]

    graph_response = client.get(f"/graph-view?brain_id={brain['brain_id']}")
    assert graph_response.status_code == 200, graph_response.text
    persisted_ids = set(applied["persisted_node_ids"])
    persisted_nodes = [
        node
        for node in graph_response.json()["graph"]["nodes"]
        if node["id"] in persisted_ids
    ]
    assert len(persisted_nodes) == len(persisted_ids)
    assert all(node["source_trust"] == "user_asserted" for node in persisted_nodes)


def _mutated_existing_graph(
    graph: dict[str, Any],
    *,
    action: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    updated = deepcopy(graph)
    before = deepcopy(updated["nodes"][0])
    before_provenance = deepcopy(before.get("provenance") or {})
    before_provenance.pop("grow_mutation_history", None)
    prior_snapshot = {**before, "provenance": before_provenance}
    history = [
        {
            "schema_version": "agvm.grow_memory_mutation_history.v1",
            "action": action,
            "changed_at": "2026-08-29T10:00:00Z",
            "claim_id": "claim-target",
            "decision_id": "decision-target",
            "prior_node_revision": max(1, int(before.get("node_revision") or 1)),
            "prior_state_sha256": sqlite_store.canonical_sha256(prior_snapshot),
            "prior_snapshot": prior_snapshot,
        }
    ]
    after = deepcopy(before)
    after["node_revision"] = max(1, int(before.get("node_revision") or 1)) + 1
    after["updated_at"] = "2026-08-29T10:00:00Z"
    after["provenance"] = {
        **before_provenance,
        "grow_mutation_history": history,
        "grow_last_mutation": {"action": action},
    }
    if action == "evolve_existing":
        after["raw_text"] = "Existing reviewed memory, evolved with bounded temporal evidence."
        after["summary"] = after["raw_text"]
        after["temporal_role"] = "current_state"
        after["valid_from"] = "2026-08-29"
    else:
        after["lifecycle_status"] = "deleted"
        after["provenance"]["deleted_at"] = "2026-08-29T10:00:00Z"
    updated["nodes"][0] = after
    return updated, after


@pytest.fixture(autouse=True)
def _clear_process_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "AGVM_GROW_PREVIEW_BINDING_SECRET",
        "test-only-grow-receipt-secret-32-bytes-minimum",
    )
    grow_router._GROW_PREVIEW_RUNS.clear()
    yield
    grow_router._GROW_PREVIEW_RUNS.clear()


def test_preview_status_and_apply_survive_process_cache_loss_and_replay_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    brain = _brain(tmp_path, "brain-durable")
    _patch_brain_resolution(monkeypatch, {brain["brain_id"]: brain}, active_brain_id=brain["brain_id"])
    _store_preview(brain)
    calls = _patch_persist_selection(monkeypatch)
    grow_router._GROW_PREVIEW_RUNS.clear()

    status = grow_router._grow_source_status(
        "grow_source_status",
        grow_router.McpGrowApplyRequest(
            brain_id=brain["brain_id"],
            investigation_id="grow-investigation-1",
        ),
    )
    applied = _apply(brain["brain_id"])
    grow_router._GROW_PREVIEW_RUNS.clear()
    replay = _apply(brain["brain_id"])
    durable_status = grow_router._grow_source_status(
        "grow_source_status",
        grow_router.McpGrowApplyRequest(
            brain_id=brain["brain_id"],
            investigation_id="grow-investigation-1",
        ),
    )

    assert status.status == "preview_ready"
    assert status.ai_execution_attestation["provider"] == "openai_compatible"
    assert status.can_apply_now is True
    assert status.selected_preview_ids == ["preview-primary", "preview-derived"]
    assert applied.status == "applied"
    assert applied.can_apply_now is False
    assert applied.selected_preview_ids == ["preview-primary"]
    assert applied.persisted_node_ids == ["persisted-preview-primary"]
    assert applied.persisted_edge_count == 1
    assert applied.receipt_id == applied.persist_result["receipt_id"]
    assert applied.apply_receipt["signature"].startswith("hmac-sha256:")
    assert applied.brain_revision_before == applied.persist_result["before_brain_revision"]
    assert applied.brain_revision_after == applied.persist_result["after_brain_revision"]
    assert applied.persist_result["idempotent_replay"] is False
    assert applied.persist_result["signed_apply_receipt"]["signature"].startswith("hmac-sha256:")
    assert applied.persist_result["signed_apply_receipt"]["signature_algorithm"] == "HMAC-SHA256"
    assert replay.status == "applied"
    assert replay.persisted_node_ids == ["persisted-preview-primary"]
    assert replay.receipt_id == applied.receipt_id
    assert replay.persist_result["idempotent_replay"] is True
    assert replay.persist_result["receipt_id"] == applied.persist_result["receipt_id"]
    assert durable_status.status == "applied"
    assert durable_status.persisted_node_ids == ["persisted-preview-primary"]
    assert durable_status.receipt_id == applied.receipt_id
    assert durable_status.persist_result["receipt_id"] == applied.persist_result["receipt_id"]
    assert calls == {"persist": 1}
    with use_runtime_brain(brain):
        graph = sqlite_store.fetch_graph_snapshot()
    assert [node["id"] for node in graph["nodes"]].count("persisted-preview-primary") == 1


@pytest.mark.parametrize("action", ["evolve_existing", "delete_existing"])
def test_durable_apply_accepts_verified_existing_node_mutation_without_node_growth(
    tmp_path: Path,
    action: str,
) -> None:
    brain = _brain(tmp_path, f"brain-{action}")

    def mutate(graph: dict[str, Any], bundle: dict[str, Any]) -> dict[str, Any]:
        del bundle
        updated, _ = _mutated_existing_graph(graph, action=action)
        return {
            "updated_graph": updated,
            "persisted_node_ids": ["existing-node"],
            "persisted_edge_count": 0,
            "merged_into_existing_ids": ["existing-node"],
        }

    result = _apply_direct_mutation(
        brain,
        investigation_id=f"grow-{action}",
        mutate_graph=mutate,
    )

    assert result["persisted_node_ids"] == ["existing-node"]
    with use_runtime_brain(brain):
        graph = sqlite_store.fetch_graph_snapshot()
    assert len(graph["nodes"]) == 1
    persisted = graph["nodes"][0]
    assert persisted["node_revision"] == 2
    assert persisted["provenance"]["grow_mutation_history"][-1]["action"] == action
    if action == "evolve_existing":
        assert persisted["raw_text"] == "Existing reviewed memory, evolved with bounded temporal evidence."
        assert persisted["valid_from"] == "2026-08-29"
        assert persisted["lifecycle_status"] == "active"
    else:
        assert persisted["raw_text"] == "Existing reviewed memory"
        assert persisted["lifecycle_status"] == "deleted"
        assert persisted.get("valid_to") is None


@pytest.mark.parametrize("failure", ["revision_only", "delete_without_tombstone"])
def test_durable_apply_rejects_unproven_existing_node_mutation(
    tmp_path: Path,
    failure: str,
) -> None:
    brain = _brain(tmp_path, f"brain-invalid-{failure}")

    def mutate(graph: dict[str, Any], bundle: dict[str, Any]) -> dict[str, Any]:
        del bundle
        action = "evolve_existing" if failure == "revision_only" else "delete_existing"
        updated, after = _mutated_existing_graph(graph, action=action)
        if failure == "revision_only":
            before = graph["nodes"][0]
            for field in sqlite_store._GROW_MUTABLE_CONTENT_FIELDS:
                after[field] = before.get(field)
        else:
            after["lifecycle_status"] = "active"
        updated["nodes"][0] = after
        return {
            "updated_graph": updated,
            "persisted_node_ids": ["existing-node"],
            "persisted_edge_count": 0,
            "merged_into_existing_ids": ["existing-node"],
        }

    with pytest.raises(sqlite_store.GrowPreviewBindingStoreError) as rejected:
        _apply_direct_mutation(
            brain,
            investigation_id=f"grow-invalid-{failure}",
            mutate_graph=mutate,
        )

    assert rejected.value.code == "persisted_node_proof_invalid"
    with use_runtime_brain(brain):
        graph = sqlite_store.fetch_graph_snapshot()
    assert len(graph["nodes"]) == 1
    assert graph["nodes"][0]["node_revision"] in {None, 1}
    assert graph["nodes"][0]["lifecycle_status"] == "active"


def test_grow_apply_can_rollback_atomically_and_replay_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    brain = _brain(tmp_path, "brain-grow-rollback")
    _patch_brain_resolution(monkeypatch, {brain["brain_id"]: brain}, active_brain_id=brain["brain_id"])
    _store_preview(brain)
    calls = _patch_persist_selection(monkeypatch)

    applied = _apply(brain["brain_id"])
    rolled_back = grow_router._grow_source_rollback(
        "grow_source_rollback",
        grow_router.McpGrowRollbackRequest(
            brain_id=brain["brain_id"],
            investigation_id="grow-investigation-1",
            confirm_rollback=True,
        ),
    )
    replay = grow_router._grow_source_rollback(
        "grow_source_rollback",
        grow_router.McpGrowRollbackRequest(
            brain_id=brain["brain_id"],
            investigation_id="grow-investigation-1",
            confirm_rollback=True,
        ),
    )

    assert applied.status == "applied"
    assert rolled_back.status == "rolled_back"
    assert rolled_back.persist_result["idempotent_replay"] is False
    assert rolled_back.persist_result["signed_rollback_receipt"]["signature"].startswith(
        "hmac-sha256:"
    )
    assert replay.status == "rolled_back"
    assert replay.persist_result["idempotent_replay"] is True
    assert calls == {"persist": 1}
    with use_runtime_brain(brain):
        graph = sqlite_store.fetch_graph_snapshot()
    assert [node["id"] for node in graph["nodes"]] == ["existing-node"]


def test_router_persists_every_successful_attested_preview_before_returning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    brain = _brain(tmp_path, "brain-preview-wiring")
    _patch_brain_resolution(monkeypatch, {brain["brain_id"]: brain}, active_brain_id=brain["brain_id"])

    class AttestedEngine:
        def preview(self, **_: Any) -> dict[str, Any]:
            ledger, aggregate = _grow_v3_execution(include_compiler=True)
            compiler_attestation = _attestation("compiler")
            compiler_attestation.pop("applicable")
            compiler_attestation.pop("legacy_read_only")
            bundle = _preview_bundle()
            bundle["primary_node_preview"].update(
                {
                    "claim_id": "claim-direct",
                    "decision_id": "decision-direct",
                    "claim_decision": "new_memory",
                }
            )
            bundle["derived_nodes"][0].update(
                {
                    "claim_id": "claim-derived",
                    "decision_id": "decision-derived",
                    "claim_decision": "new_memory",
                }
            )
            return {
                "schema_version": "agvm.grow_engine_result.v3",
                "preview_bundle": bundle,
                "ai_execution_attestation": compiler_attestation,
                "ai_execution_ledger": ledger,
                "clarification_questions": [],
                "investigation": {
                    "schema_version": "agvm.grow_investigation.v3",
                    "complete": True,
                    "applicable": True,
                    "ai_execution_ledger": ledger,
                    "ai_execution_attestation": aggregate,
                    "usage": aggregate["usage"],
                },
                "investigation_session": {
                    "schema_version": "agvm.investigation_session.v3",
                    "status": "sufficient",
                    "provider_attested": True,
                },
                "apply_ready": True,
                "usage": aggregate["usage"],
            }

    monkeypatch.setattr(grow_router, "_GROW_ENGINE", AttestedEngine())
    preview = grow_router._grow_source_preview(
        "grow_source_preview",
        grow_router.McpGrowSourceRequest(
            brain_id=brain["brain_id"],
            raw_input="A provider-attested source.",
            input_kind="manual_text",
            run_preview=True,
        ),
    )
    investigation_id = str(preview.investigation_id)
    grow_router._GROW_PREVIEW_RUNS.clear()
    status = grow_router._grow_source_status(
        "grow_source_status",
        grow_router.McpGrowApplyRequest(
            brain_id=brain["brain_id"],
            investigation_id=investigation_id,
            resume_token=preview.resume_token,
            investigation_version=preview.investigation_version,
        ),
    )

    assert preview.status == "preview_ready"
    assert status.status == "preview_ready"
    assert status.preview_bundle == preview.preview_bundle
    assert preview.ai_execution_attestation["applicable"] is True
    assert preview.ai_execution_attestation["legacy_read_only"] is False
    assert status.ai_execution_attestation == preview.ai_execution_attestation


def test_router_returns_needs_review_for_structural_only_maintenance_feedback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    brain = _brain(tmp_path, "brain-structural-boundary")
    _patch_brain_resolution(monkeypatch, {brain["brain_id"]: brain}, active_brain_id=brain["brain_id"])
    packet = _maintenance_feedback_packet("structural-only")

    class StructuralOnlyEngine:
        def preview(self, **_: Any) -> dict[str, Any]:
            ledger, aggregate = _grow_v3_execution(include_compiler=False)
            return {
                "schema_version": "agvm.grow_engine_result.v3",
                "preview_bundle": None,
                "maintenance_feedback": [packet],
                "clarification_questions": [],
                "investigation": {
                    "schema_version": "agvm.grow_investigation.v3",
                    "complete": True,
                    "applicable": True,
                    "ai_execution_ledger": ledger,
                    "ai_execution_attestation": aggregate,
                    "usage": aggregate["usage"],
                },
                "investigation_session": {
                    "schema_version": "agvm.investigation_session.v3",
                    "status": "maintenance_deferred",
                    "provider_attested": True,
                },
                "ai_execution_attestation": aggregate,
                "ai_execution_ledger": ledger,
                "apply_ready": False,
                "usage": aggregate["usage"],
            }

    monkeypatch.setattr(grow_router, "_GROW_ENGINE", StructuralOnlyEngine())

    preview = grow_router._grow_source_preview(
        "grow_source_preview",
        grow_router.McpGrowSourceRequest(
            brain_id=brain["brain_id"],
            raw_input="Kinetic duplicates an old memory.",
            input_kind="manual_text",
            run_preview=True,
        ),
    )

    assert preview.status == "needs_review"
    assert preview.investigation_id
    assert preview.preview_bundle is None
    assert preview.maintenance_feedback_packets == [packet]
    assert preview.source_formation_contract["state"] == "maintenance_deferred"
    assert preview.source_formation_contract["apply_contract"]["can_apply_now"] is False
    with use_runtime_brain(brain):
        stored = sqlite_store.fetch_local_grow_v2_preview(
            brain_id=brain["brain_id"],
            investigation_id=str(preview.investigation_id),
        )
    assert stored is not None
    assert stored["state"] == "maintenance_deferred"
    assert stored["maintenance_feedback_packets"] == [packet]
    assert stored["maintenance_feedback_packets_sha256"] == sqlite_store.canonical_sha256([packet])

    grow_router._GROW_PREVIEW_RUNS.clear()
    status_without_token = grow_router._grow_source_status(
        "grow_source_status",
        grow_router.McpGrowApplyRequest(
            brain_id=brain["brain_id"],
            investigation_id=preview.investigation_id,
        ),
    )
    status = grow_router._grow_source_status(
        "grow_source_status",
        grow_router.McpGrowApplyRequest(
            brain_id=brain["brain_id"],
            investigation_id=preview.investigation_id,
            resume_token=preview.resume_token,
        ),
    )
    apply_attempt = grow_router._grow_source_apply(
        "grow_source_apply",
        grow_router.McpGrowApplyRequest(
            brain_id=brain["brain_id"],
            investigation_id=preview.investigation_id,
            resume_token=preview.resume_token,
            confirm_apply=True,
        ),
    )

    assert status_without_token.status == "blocked"
    assert status_without_token.maintenance_feedback_packets == []
    assert status.status == "needs_review"
    assert status.preview_bundle is None
    assert status.maintenance_feedback_packets == [packet]
    assert apply_attempt.status == "needs_review"
    assert apply_attempt.preview_bundle is None
    assert apply_attempt.maintenance_feedback_packets == [packet]


def test_router_mixed_preview_propagates_maintenance_feedback_packets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    brain = _brain(tmp_path, "brain-mixed-boundary")
    _patch_brain_resolution(monkeypatch, {brain["brain_id"]: brain}, active_brain_id=brain["brain_id"])
    packet = _maintenance_feedback_packet("mixed")
    bundle = _preview_bundle(include_derived=False)
    bundle["primary_node_preview"].update(
        {
            "claim_id": "claim-direct",
            "decision_id": "decision-direct",
            "claim_decision": "new_memory",
        }
    )

    class MixedEngine:
        def preview(self, **_: Any) -> dict[str, Any]:
            ledger, aggregate = _grow_v3_execution(include_compiler=True)
            compiler_attestation = _attestation("compiler")
            compiler_attestation.pop("applicable")
            compiler_attestation.pop("legacy_read_only")
            return {
                "schema_version": "agvm.grow_engine_result.v3",
                "preview_bundle": deepcopy(bundle),
                "maintenance_feedback": [packet],
                "clarification_questions": [],
                "investigation": {
                    "schema_version": "agvm.grow_investigation.v3",
                    "complete": True,
                    "applicable": True,
                    "ai_execution_ledger": ledger,
                    "ai_execution_attestation": aggregate,
                    "usage": aggregate["usage"],
                },
                "investigation_session": {
                    "schema_version": "agvm.investigation_session.v3",
                    "status": "sufficient",
                    "provider_attested": True,
                },
                "ai_execution_attestation": compiler_attestation,
                "ai_execution_ledger": ledger,
                "apply_ready": True,
                "usage": aggregate["usage"],
            }

    monkeypatch.setattr(grow_router, "_GROW_ENGINE", MixedEngine())

    preview = grow_router._grow_source_preview(
        "grow_source_preview",
        grow_router.McpGrowSourceRequest(
            brain_id=brain["brain_id"],
            raw_input="Kinetic has one direct memory and one structural duplicate.",
            input_kind="manual_text",
            run_preview=True,
        ),
    )
    grow_router._GROW_PREVIEW_RUNS.clear()
    status = grow_router._grow_source_status(
        "grow_source_status",
        grow_router.McpGrowApplyRequest(
            brain_id=brain["brain_id"],
            investigation_id=preview.investigation_id,
            resume_token=preview.resume_token,
            investigation_version=preview.investigation_version,
        ),
    )

    assert preview.status == "preview_ready"
    assert preview.preview_bundle is not None
    assert preview.maintenance_feedback_packets == [packet]
    assert "maintenance_feedback_packets" not in preview.preview_bundle
    assert status.status == "preview_ready"
    assert status.preview_bundle == preview.preview_bundle
    assert status.maintenance_feedback_packets == [packet]


def test_apply_rejects_tamper_unknown_selection_stale_and_cross_brain(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    brain_a = _brain(tmp_path, "brain-a")
    brain_b = _brain(tmp_path, "brain-b")
    brains = {brain_a["brain_id"]: brain_a, brain_b["brain_id"]: brain_b}
    _patch_brain_resolution(monkeypatch, brains, active_brain_id=brain_a["brain_id"])
    _patch_persist_selection(monkeypatch)

    _store_preview(brain_a, investigation_id="unknown-selection")
    unknown = _apply(
        brain_a["brain_id"],
        investigation_id="unknown-selection",
        selected_preview_ids=["client-invented"],
    )
    assert unknown.status == "blocked"
    assert unknown.memory_operation_lifecycle_contract["blocked_reason"] == (
        "selected_preview_ids_not_server_issued"
    )

    _store_preview(brain_a, investigation_id="tampered")
    with use_runtime_brain(brain_a), sqlite_store.connect() as conn:
        row = conn.execute(
            "SELECT preview_payload_json FROM grow_preview_bindings WHERE token_id = ?",
            ("tampered",),
        ).fetchone()
        payload = json.loads(str(row["preview_payload_json"]))
        payload["preview_bundle"]["primary_node_preview"]["summary"] = "tampered"
        conn.execute(
            "UPDATE grow_preview_bindings SET preview_payload_json = ? WHERE token_id = ?",
            (json.dumps(payload), "tampered"),
        )
        conn.commit()
    tampered = _apply(brain_a["brain_id"], investigation_id="tampered")
    assert tampered.status == "blocked"
    assert tampered.memory_operation_lifecycle_contract["blocked_reason"] == (
        "grow_v2_preview_integrity_invalid"
    )

    _store_preview(brain_a, investigation_id="attestation-tampered")
    with use_runtime_brain(brain_a), sqlite_store.connect() as conn:
        row = conn.execute(
            "SELECT preview_payload_json FROM grow_preview_bindings WHERE token_id = ?",
            ("attestation-tampered",),
        ).fetchone()
        payload = json.loads(str(row["preview_payload_json"]))
        payload["ai_execution_attestation"]["provider"] = "tampered-provider"
        conn.execute(
            "UPDATE grow_preview_bindings SET preview_payload_json = ? WHERE token_id = ?",
            (json.dumps(payload), "attestation-tampered"),
        )
        conn.commit()
    attestation_tampered = _apply(
        brain_a["brain_id"],
        investigation_id="attestation-tampered",
    )
    assert attestation_tampered.status == "blocked"
    assert attestation_tampered.memory_operation_lifecycle_contract["blocked_reason"] == (
        "grow_v2_preview_integrity_invalid"
    )

    _store_preview(brain_a, investigation_id="stale")
    with use_runtime_brain(brain_a):
        sqlite_store.replace_runtime_graph(_graph("Concurrent graph update"))
    stale = _apply(brain_a["brain_id"], investigation_id="stale")
    assert stale.status == "blocked"
    assert stale.memory_operation_lifecycle_contract["blocked_reason"] == "server_preview_stale"

    _store_preview(brain_a, investigation_id="cross-brain")
    cross_brain = _apply(brain_b["brain_id"], investigation_id="cross-brain")
    assert cross_brain.status == "blocked"
    assert cross_brain.memory_operation_lifecycle_contract["blocked_reason"] == (
        "server_preview_not_found"
    )


def test_mismatched_replay_fails_without_duplicate_nodes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    brain = _brain(tmp_path, "brain-replay")
    _patch_brain_resolution(monkeypatch, {brain["brain_id"]: brain}, active_brain_id=brain["brain_id"])
    _store_preview(brain)
    calls = _patch_persist_selection(monkeypatch)

    applied = _apply(brain["brain_id"], selected_preview_ids=["preview-primary"])
    mismatched = _apply(brain["brain_id"], selected_preview_ids=["preview-derived"])

    assert applied.status == "applied"
    assert mismatched.status == "blocked"
    assert mismatched.memory_operation_lifecycle_contract["blocked_reason"] == "apply_receipt_mismatch"
    assert calls == {"persist": 1}


def test_persisted_receipt_tamper_is_detected_after_cache_loss(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    brain = _brain(tmp_path, "brain-receipt-tamper")
    _patch_brain_resolution(monkeypatch, {brain["brain_id"]: brain}, active_brain_id=brain["brain_id"])
    _store_preview(brain)
    _patch_persist_selection(monkeypatch)
    assert _apply(brain["brain_id"]).status == "applied"
    grow_router._GROW_PREVIEW_RUNS.clear()
    with use_runtime_brain(brain), sqlite_store.connect() as conn:
        row = conn.execute(
            "SELECT apply_result_json FROM grow_preview_bindings WHERE token_id = ?",
            ("grow-investigation-1",),
        ).fetchone()
        apply_result = json.loads(str(row["apply_result_json"]))
        apply_result["signed_apply_receipt"]["signature"] = "hmac-sha256:" + "0" * 64
        conn.execute(
            "UPDATE grow_preview_bindings SET apply_result_json = ? WHERE token_id = ?",
            (json.dumps(apply_result), "grow-investigation-1"),
        )
        conn.commit()

    status = grow_router._grow_source_status(
        "grow_source_status",
        grow_router.McpGrowApplyRequest(
            brain_id=brain["brain_id"],
            investigation_id="grow-investigation-1",
        ),
    )
    assert status.status == "blocked"
    assert status.memory_operation_lifecycle_contract["blocked_reason"] == (
        "grow_v2_apply_receipt_integrity_invalid"
    )


def test_legacy_checksum_receipt_survives_upgrade_without_duplicate_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    brain = _brain(tmp_path, "brain-legacy-receipt")
    _patch_brain_resolution(monkeypatch, {brain["brain_id"]: brain}, active_brain_id=brain["brain_id"])
    _store_preview(brain)
    calls = _patch_persist_selection(monkeypatch)
    assert _apply(brain["brain_id"]).status == "applied"

    with use_runtime_brain(brain), sqlite_store.connect() as conn:
        row = conn.execute(
            "SELECT apply_result_json FROM grow_preview_bindings WHERE token_id = ?",
            ("grow-investigation-1",),
        ).fetchone()
        apply_result = json.loads(str(row["apply_result_json"]))
        receipt = apply_result["signed_apply_receipt"]
        receipt["signature_algorithm"] = "SHA-256"
        receipt.pop("signature_key_id", None)
        receipt["signature"] = sqlite_store._legacy_local_grow_v2_receipt_checksum(receipt)
        conn.execute(
            "UPDATE grow_preview_bindings SET apply_result_json = ? WHERE token_id = ?",
            (json.dumps(apply_result), "grow-investigation-1"),
        )
        conn.commit()

    grow_router._GROW_PREVIEW_RUNS.clear()
    status = grow_router._grow_source_status(
        "grow_source_status",
        grow_router.McpGrowApplyRequest(
            brain_id=brain["brain_id"],
            investigation_id="grow-investigation-1",
        ),
    )
    replay = _apply(brain["brain_id"])

    assert status.status == "applied"
    assert status.persist_result["signed_apply_receipt"]["authenticity"] == "legacy_checksum_only"
    assert status.persist_result["signed_apply_receipt"]["authenticated_signature"] is False
    assert replay.persist_result["idempotent_replay"] is True
    assert calls == {"persist": 1}


def test_graph_and_receipt_roll_back_together_when_receipt_write_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    brain = _brain(tmp_path, "brain-atomic")
    _patch_brain_resolution(monkeypatch, {brain["brain_id"]: brain}, active_brain_id=brain["brain_id"])
    _store_preview(brain)
    calls = _patch_persist_selection(monkeypatch)
    with use_runtime_brain(brain), sqlite_store.connect() as conn:
        conn.execute(
            """
            CREATE TRIGGER fail_grow_receipt_write
            BEFORE UPDATE OF state ON grow_preview_bindings
            WHEN NEW.state = 'consumed'
            BEGIN
                SELECT RAISE(ABORT, 'receipt write failed');
            END
            """
        )
        conn.commit()

    result = _apply(brain["brain_id"])

    assert result.status == "blocked"
    assert result.memory_operation_lifecycle_contract["blocked_reason"] == (
        "grow_v2_apply_transaction_failed"
    )
    assert calls == {"persist": 1}
    with use_runtime_brain(brain):
        graph = sqlite_store.fetch_graph_snapshot()
        stored = sqlite_store.fetch_local_grow_v2_preview(
            brain_id=brain["brain_id"],
            investigation_id="grow-investigation-1",
        )
    assert [node["id"] for node in graph["nodes"]] == ["existing-node"]
    assert stored is not None
    assert stored["state"] == "active"
    assert stored["apply_result"] == {}


def test_grow_apply_retries_transient_sqlite_busy_without_duplicate_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    brain = _brain(tmp_path, "brain-transient-busy")
    _patch_brain_resolution(monkeypatch, {brain["brain_id"]: brain}, active_brain_id=brain["brain_id"])
    _store_preview(brain)
    persist_calls = _patch_persist_selection(monkeypatch)
    transaction_calls = {"count": 0}
    real_apply = grow_router.apply_local_grow_v2_preview_transaction

    def transient_busy_then_apply(**kwargs: Any) -> dict[str, Any]:
        transaction_calls["count"] += 1
        if transaction_calls["count"] == 1:
            raise sqlite_store.sqlite3.OperationalError("database is locked")
        return real_apply(**kwargs)

    monkeypatch.setattr(
        grow_router,
        "apply_local_grow_v2_preview_transaction",
        transient_busy_then_apply,
    )

    result = _apply(brain["brain_id"])

    assert result.status == "applied"
    assert sqlite_store.LOCAL_GROW_V2_TRANSACTION_BUSY_TIMEOUT_MS == 300_000
    assert transaction_calls == {"count": 2}
    assert persist_calls == {"persist": 1}
    with use_runtime_brain(brain):
        graph = sqlite_store.fetch_graph_snapshot()
        stored = sqlite_store.fetch_local_grow_v2_preview(
            brain_id=brain["brain_id"],
            investigation_id="grow-investigation-1",
        )
    assert [node["id"] for node in graph["nodes"]].count("persisted-preview-primary") == 1
    assert stored is not None
    assert stored["state"] == "consumed"


def test_provider_failure_never_persists_preview_or_exposes_apply(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    brain = _brain(tmp_path, "brain-provider-missing")
    _patch_brain_resolution(monkeypatch, {brain["brain_id"]: brain}, active_brain_id=brain["brain_id"])

    class MissingProviderEngine:
        def preview(self, **_: Any) -> dict[str, Any]:
            raise AiModuleContractError("ai_execution_provider_missing")

    monkeypatch.setattr(grow_router, "_GROW_ENGINE", MissingProviderEngine())
    result = grow_router._grow_source_preview(
        "grow_source_preview",
        grow_router.McpGrowSourceRequest(
            brain_id=brain["brain_id"],
            raw_input="This must not become a heuristic memory.",
            input_kind="manual_text",
            run_preview=True,
        ),
    )

    assert result.status == "blocked"
    assert result.memory_operation_lifecycle_contract["blocked_reason"] == (
        "ai_execution_provider_missing"
    )
    assert result.preview_bundle is None
    assert result.budget == {"credits_required": 0, "runtime": "local_core"}
    with use_runtime_brain(brain), sqlite_store.connect_readonly() as conn:
        persisted_count = conn.execute(
            "SELECT COUNT(*) FROM grow_preview_bindings WHERE operation_family = ?",
            (sqlite_store.LOCAL_GROW_V2_OPERATION_FAMILY,),
        ).fetchone()[0]
    assert persisted_count == 0


@pytest.mark.parametrize("input_kind", ["url", "auto"])
def test_unfetched_url_stops_before_provider_and_preserves_source_package(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    input_kind: str,
) -> None:
    brain = _brain(tmp_path, "brain-url-without-evidence")
    _patch_brain_resolution(monkeypatch, {brain["brain_id"]: brain}, active_brain_id=brain["brain_id"])

    class ProviderMustNotRun:
        def preview(self, **_: Any) -> dict[str, Any]:
            raise AssertionError("provider must not run without acquired source evidence")

    monkeypatch.setattr(
        grow_router,
        "build_source_investigation_package",
        lambda *_args, **_kwargs: {
            "schema_version": "agvm.source_investigation.v1",
            "investigation_id": "url-evidence-preflight",
            "status": "rich_extraction_required",
            "source_units": [
                {
                    "unit_id": "url-reference",
                    "raw_text": "",
                    "source_uri": "https://example.com/product-spec",
                    "fact_eligible": False,
                    "status": "rich_extraction_required",
                }
            ],
            "compiler_handoff": {"ready": False, "structured_sections": []},
        },
    )
    monkeypatch.setattr(grow_router, "_GROW_ENGINE", ProviderMustNotRun())
    result = grow_router._grow_source_preview(
        "grow_source_preview",
        grow_router.McpGrowSourceRequest(
            brain_id=brain["brain_id"],
            raw_input="https://example.com/product-spec",
            source_uri="https://example.com/product-spec",
            input_kind=input_kind,
            run_preview=True,
        ),
    )

    assert result.status == "blocked"
    assert result.memory_operation_lifecycle_contract["blocked_reason"] == "rich_extraction_required"
    assert result.source_investigation["status"] == "rich_extraction_required"
    assert result.source_formation_contract["state"] == "blocked"
    assert result.preview_bundle is None
