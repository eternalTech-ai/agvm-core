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

ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "agvm_api"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

import core_mcp_ops_router as grow_router  # noqa: E402
import sqlite_store  # noqa: E402
from ai_modules_v2 import AiModuleContractError  # noqa: E402
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
    assert applied.status == "applied"
    assert applied.persist_result["idempotent_replay"] is False
    assert applied.persist_result["signed_apply_receipt"]["signature"].startswith("hmac-sha256:")
    assert applied.persist_result["signed_apply_receipt"]["signature_algorithm"] == "HMAC-SHA256"
    assert replay.status == "applied"
    assert replay.persist_result["idempotent_replay"] is True
    assert replay.persist_result["receipt_id"] == applied.persist_result["receipt_id"]
    assert durable_status.status == "applied"
    assert durable_status.persist_result["receipt_id"] == applied.persist_result["receipt_id"]
    assert calls == {"persist": 1}
    with use_runtime_brain(brain):
        graph = sqlite_store.fetch_graph_snapshot()
    assert [node["id"] for node in graph["nodes"]].count("persisted-preview-primary") == 1


def test_router_persists_every_successful_attested_preview_before_returning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    brain = _brain(tmp_path, "brain-preview-wiring")
    _patch_brain_resolution(monkeypatch, {brain["brain_id"]: brain}, active_brain_id=brain["brain_id"])

    class AttestedEngine:
        def preview(self, **_: Any) -> dict[str, Any]:
            engine_attestation = _attestation("router-preview")
            engine_attestation.pop("applicable")
            engine_attestation.pop("legacy_read_only")
            return {
                "schema_version": "agvm.grow_engine_result.v2",
                "preview_bundle": _preview_bundle(),
                "ai_execution_attestation": engine_attestation,
                "clarification_questions": [],
                "investigation_session": {
                    "schema_version": "agvm.investigation_session.v2",
                    "status": "sufficient",
                    "provider_attested": True,
                },
                "apply_ready": True,
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
    investigation_id = str(preview.source_investigation["investigation_id"])
    grow_router._GROW_PREVIEW_RUNS.clear()
    status = grow_router._grow_source_status(
        "grow_source_status",
        grow_router.McpGrowApplyRequest(
            brain_id=brain["brain_id"],
            investigation_id=investigation_id,
        ),
    )

    assert preview.status == "preview_ready"
    assert status.status == "preview_ready"
    assert status.preview_bundle == preview.preview_bundle
    assert preview.ai_execution_attestation["applicable"] is True
    assert preview.ai_execution_attestation["legacy_read_only"] is False
    assert status.ai_execution_attestation == preview.ai_execution_attestation


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
