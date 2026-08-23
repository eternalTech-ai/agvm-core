# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from brain_registry import BrainRegistryError, brain_root_path, resolve_brain_scope
from derivation import persist_selection, preview_bundle
from retrieval import build_index
from runtime_scope import use_runtime_brain
from sqlite_store import bootstrap_runtime_store, fetch_atlas, fetch_graph_snapshot, replace_runtime_graph

from .contracts import (
    CLOUD_REQUIRED_CAPABILITIES,
    LOCAL_CAPABILITIES,
    OPERATIONS,
    bootstrap_response,
    build_cloud_action_contract,
)
from .store import BootstrapSessionStore, BootstrapStoreError, request_digest


MAX_ANSWERS = 128
MAX_SOURCES = 32
MAX_ANSWER_CHARS = 16_000
MAX_SOURCE_CHARS = 100_000


class BootstrapV1Error(RuntimeError):
    def __init__(self, code: str, *, status_code: int = 409) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class BrainBootstrapV1Service:
    def __init__(
        self,
        *,
        brain_resolver: Callable[..., dict[str, Any]] = resolve_brain_scope,
        brain_root_resolver: Callable[[], Path] = brain_root_path,
        preview_builder: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
        apply_executor: Callable[[dict[str, Any], dict[str, Any], list[str]], dict[str, Any]] | None = None,
        mutation_probe: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self._brain_resolver = brain_resolver
        self._brain_root_resolver = brain_root_resolver
        self._preview_builder = preview_builder or _build_manual_grow_preview
        self._apply_executor = apply_executor or _apply_manual_grow_preview
        self._mutation_probe = mutation_probe or _probe_manual_grow_mutation

    def execute(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if operation not in OPERATIONS:
            raise BootstrapV1Error("bootstrap_operation_not_supported", status_code=404)
        clean_payload = dict(payload or {})
        brain_record = self._resolve_brain(clean_payload.get("brain_id"))
        brain_id = str(brain_record["brain_id"])
        capability = str(clean_payload.get("capability") or _default_capability(operation)).strip()
        session_id = str(clean_payload.get("session_id") or "").strip() or None
        if capability in CLOUD_REQUIRED_CAPABILITIES:
            session = self._store(brain_record).load_latest(session_id) if session_id else None
            return bootstrap_response(
                operation=operation,
                status="cloud_required",
                brain_id=brain_id,
                session=session,
                action_contract=build_cloud_action_contract(
                    operation=operation,
                    capability=capability,
                    brain_id=brain_id,
                    session_id=session_id,
                ),
            )
        if capability not in LOCAL_CAPABILITIES:
            raise BootstrapV1Error("bootstrap_capability_not_supported", status_code=422)
        if operation == "start":
            return self._start(brain_record, clean_payload)
        if not session_id:
            raise BootstrapV1Error("bootstrap_session_id_required", status_code=422)
        store = self._store(brain_record)
        if operation == "status":
            return bootstrap_response(
                operation=operation,
                status="ok",
                brain_id=brain_id,
                session=store.load_latest(session_id),
            )
        return self._mutate(operation, brain_record, store, session_id, clean_payload)

    def _start(self, brain_record: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        key = _required_text(payload, "idempotency_key", max_chars=128)
        session_id = str(payload.get("session_id") or "").strip() or f"bbv1-{uuid.uuid5(uuid.NAMESPACE_URL, str(brain_record['brain_id']) + ':' + key)}"
        store = self._store(brain_record)
        digest = request_digest(payload)
        with store.locked(session_id):
            try:
                history = store.load_history(session_id)
            except BootstrapStoreError as exc:
                if exc.code != "bootstrap_session_not_found":
                    raise
                history = []
            replay = store.find_idempotent(history, operation="start", idempotency_key=key, request_digest=digest)
            if replay:
                return bootstrap_response(
                    operation="start", status="started", brain_id=store.brain_id, session=replay, replayed=True
                )
            if history:
                raise BootstrapV1Error("bootstrap_session_already_exists", status_code=409)
            questions = _bounded_text_list(payload.get("questions"), max_items=32, max_chars=2_000)
            snapshot = {
                "session_id": session_id,
                "lifecycle_state": "interview_active",
                "created_at": _utc_now(),
                "updated_at": _utc_now(),
                "goal": str(payload.get("goal") or "Build a reviewed initial memory seed.")[:4_000],
                "questions": questions,
                "answers": [],
                "sources": [],
                "preview": None,
                "apply_result": None,
                "request": _request_record("start", key, digest),
            }
            created = store.append(snapshot, expected_revision=0)
            return bootstrap_response(operation="start", status="started", brain_id=store.brain_id, session=created)

    def _mutate(
        self,
        operation: str,
        brain_record: dict[str, Any],
        store: BootstrapSessionStore,
        session_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        key = _required_text(payload, "idempotency_key", max_chars=128)
        expected_revision = _required_revision(payload)
        digest = request_digest(payload)
        with store.locked(session_id):
            history = store.load_history(session_id)
            replay = store.find_idempotent(history, operation=operation, idempotency_key=key, request_digest=digest)
            if replay:
                return bootstrap_response(
                    operation=operation,
                    status=_operation_status(operation, replay),
                    brain_id=store.brain_id,
                    session=replay,
                    replayed=True,
                )
            current = history[-1]
            if int(current["revision"]) != expected_revision:
                raise BootstrapV1Error(
                    f"bootstrap_revision_conflict:expected={expected_revision}:actual={current['revision']}", status_code=409
                )
            if str(current.get("lifecycle_state") or "") == "applied" and operation not in {"status"}:
                raise BootstrapV1Error("bootstrap_session_already_applied", status_code=409)
            self._assert_operation_allowed(operation, current)
            if operation == "answer":
                return self._answer(store, current, payload, key, digest)
            if operation == "add_source":
                return self._add_source(store, current, payload, key, digest)
            if operation == "preview":
                return self._preview(brain_record, store, current, payload, key, digest)
            if operation == "apply":
                return self._apply(brain_record, store, current, payload, key, digest)
            if operation == "cancel":
                return self._simple_transition(store, current, operation, "cancelled", key, digest)
            if operation == "resume":
                if current.get("lifecycle_state") != "cancelled":
                    raise BootstrapV1Error("bootstrap_session_not_resumable", status_code=409)
                next_state = "preview_ready" if current.get("preview") else "interview_active"
                return self._simple_transition(store, current, operation, next_state, key, digest)
            if operation == "recover":
                if current.get("lifecycle_state") not in {"apply_pending", "recovery_required"}:
                    raise BootstrapV1Error("bootstrap_session_recovery_not_required", status_code=409)
                observation = self._mutation_probe(
                    brain_record,
                    dict(current.get("apply_intent") or {}).get("mutation_receipt") or {},
                )
                if observation.get("state") == "applied":
                    recovered = copy.deepcopy(current)
                    recovered.update(
                        {
                            "lifecycle_state": "applied",
                            "apply_result": dict(observation.get("apply_result") or {}),
                            "applied_at": _utc_now(),
                            "recovery": {
                                "state": "already_applied_verified",
                                "automatic_reapply_allowed": False,
                            },
                        }
                    )
                    return self._append_response(store, recovered, operation, key, digest, status="applied")
                recovered = copy.deepcopy(current)
                recovered["lifecycle_state"] = "review_required"
                recovered["recovery"] = {
                    "state": str(observation.get("state") or "manual_verification_required"),
                    "reason": str(
                        observation.get("reason")
                        or "Previous apply did not reach a verified terminal mutation receipt."
                    ),
                    "automatic_reapply_allowed": False,
                }
                return self._append_response(store, recovered, operation, key, digest, status="recovered")
        raise BootstrapV1Error("bootstrap_operation_not_implemented", status_code=501)

    @staticmethod
    def _assert_operation_allowed(operation: str, current: dict[str, Any]) -> None:
        state = str(current.get("lifecycle_state") or "")
        if state == "cancelled" and operation != "resume":
            raise BootstrapV1Error("bootstrap_session_cancelled_resume_required", status_code=409)
        if state in {"apply_pending", "recovery_required"} and operation != "recover":
            raise BootstrapV1Error("bootstrap_session_recovery_required", status_code=409)
        if operation == "apply" and state not in {"preview_ready", "review_required"}:
            raise BootstrapV1Error("bootstrap_preview_required_before_apply", status_code=409)

    def _answer(
        self,
        store: BootstrapSessionStore,
        current: dict[str, Any],
        payload: dict[str, Any],
        key: str,
        digest: str,
    ) -> dict[str, Any]:
        answers = list(current.get("answers") or [])
        if len(answers) >= MAX_ANSWERS:
            raise BootstrapV1Error("bootstrap_answer_limit_reached", status_code=422)
        answers.append(
            {
                "answer_id": str(payload.get("answer_id") or f"answer-{len(answers) + 1}"),
                "question_id": _required_text(payload, "question_id", max_chars=256),
                "answer": _required_text(payload, "answer", max_chars=MAX_ANSWER_CHARS),
                "recorded_at": _utc_now(),
            }
        )
        updated = copy.deepcopy(current)
        updated.update({"answers": answers, "preview": None, "lifecycle_state": "interview_active"})
        return self._append_response(store, updated, "answer", key, digest, status="answer_recorded")

    def _add_source(
        self,
        store: BootstrapSessionStore,
        current: dict[str, Any],
        payload: dict[str, Any],
        key: str,
        digest: str,
    ) -> dict[str, Any]:
        sources = list(current.get("sources") or [])
        if len(sources) >= MAX_SOURCES:
            raise BootstrapV1Error("bootstrap_source_limit_reached", status_code=422)
        source_text = str(payload.get("source_text") or "").strip()
        source_uri = str(payload.get("source_uri") or "").strip()
        if not source_text and not source_uri:
            raise BootstrapV1Error("bootstrap_source_text_or_uri_required", status_code=422)
        if len(source_text) > MAX_SOURCE_CHARS:
            raise BootstrapV1Error("bootstrap_source_text_too_large", status_code=422)
        sources.append(
            {
                "source_id": str(payload.get("source_id") or f"source-{len(sources) + 1}"),
                "label": str(payload.get("source_label") or source_uri or f"Source {len(sources) + 1}")[:1_000],
                "kind": str(payload.get("source_kind") or ("url_reference" if source_uri else "manual_text")),
                "source_text": source_text,
                "source_uri": source_uri or None,
                "trust": str(payload.get("source_trust") or "user_asserted"),
                "recorded_at": _utc_now(),
            }
        )
        updated = copy.deepcopy(current)
        updated.update({"sources": sources, "preview": None, "lifecycle_state": "interview_active"})
        return self._append_response(store, updated, "add_source", key, digest, status="source_recorded")

    def _preview(
        self,
        brain_record: dict[str, Any],
        store: BootstrapSessionStore,
        current: dict[str, Any],
        payload: dict[str, Any],
        key: str,
        digest: str,
    ) -> dict[str, Any]:
        preview = self._preview_builder(current, brain_record)
        if not list(preview.get("selected_preview_ids") or []):
            raise BootstrapV1Error("bootstrap_preview_has_no_reviewable_candidates", status_code=409)
        updated = copy.deepcopy(current)
        updated.update({"preview": preview, "lifecycle_state": "preview_ready"})
        return self._append_response(store, updated, "preview", key, digest, status="preview_ready")

    def _apply(
        self,
        brain_record: dict[str, Any],
        store: BootstrapSessionStore,
        current: dict[str, Any],
        payload: dict[str, Any],
        key: str,
        digest: str,
    ) -> dict[str, Any]:
        if payload.get("confirm_apply") is not True:
            raise BootstrapV1Error("bootstrap_confirm_apply_required", status_code=409)
        preview = dict(current.get("preview") or {})
        if not preview:
            raise BootstrapV1Error("bootstrap_preview_required_before_apply", status_code=409)
        available = [str(value) for value in list(preview.get("selected_preview_ids") or []) if str(value)]
        selected = [str(value) for value in list(payload.get("selected_preview_ids") or available) if str(value)]
        if not selected or not set(selected).issubset(set(available)):
            raise BootstrapV1Error("bootstrap_selected_preview_ids_invalid", status_code=422)

        mutation_receipt = _build_mutation_receipt(
            brain_record=brain_record,
            session=current,
            preview=preview,
            selected_ids=selected,
            idempotency_key=key,
        )
        pending = copy.deepcopy(current)
        pending.update(
            {
                "lifecycle_state": "apply_pending",
                "apply_intent": {
                    "selected_preview_ids": selected,
                    "confirmed": True,
                    "recorded_at": _utc_now(),
                    "mutation_receipt": mutation_receipt,
                },
            }
        )
        pending = self._append(store, pending, "apply", key, digest)
        try:
            result = self._apply_executor(preview, brain_record, selected)
        except Exception as exc:
            observation = self._mutation_probe(brain_record, mutation_receipt)
            if observation.get("state") == "applied":
                applied = copy.deepcopy(pending)
                applied.update(
                    {
                        "lifecycle_state": "applied",
                        "apply_result": dict(observation.get("apply_result") or {}),
                        "applied_at": _utc_now(),
                        "recovery": {
                            "state": "already_applied_verified",
                            "failure_boundary": type(exc).__name__,
                            "automatic_reapply_allowed": False,
                        },
                    }
                )
                applied = store.append(applied, expected_revision=int(pending["revision"]))
                return bootstrap_response(operation="apply", status="applied", brain_id=store.brain_id, session=applied)
            failed = copy.deepcopy(pending)
            failed.update(
                {
                    "lifecycle_state": "recovery_required",
                    "apply_result": {"status": "failed", "reason": type(exc).__name__},
                    "recovery": {
                        "state": str(observation.get("state") or "ambiguous"),
                        "reason": str(observation.get("reason") or "mutation_not_verified"),
                        "automatic_reapply_allowed": False,
                    },
                }
            )
            failed = store.append(failed, expected_revision=int(pending["revision"]))
            return bootstrap_response(operation="apply", status="recovery_required", brain_id=store.brain_id, session=failed)

        observation = self._mutation_probe(brain_record, mutation_receipt)
        if observation.get("state") != "applied":
            failed = copy.deepcopy(pending)
            failed.update(
                {
                    "lifecycle_state": "recovery_required",
                    "apply_result": {"status": "failed", "reason": "mutation_receipt_not_verified"},
                    "recovery": {
                        "state": str(observation.get("state") or "ambiguous"),
                        "reason": str(observation.get("reason") or "mutation_not_verified"),
                        "automatic_reapply_allowed": False,
                    },
                }
            )
            failed = store.append(failed, expected_revision=int(pending["revision"]))
            return bootstrap_response(operation="apply", status="recovery_required", brain_id=store.brain_id, session=failed)
        applied = copy.deepcopy(pending)
        applied.update(
            {
                "lifecycle_state": "applied",
                "apply_result": {**result, "mutation_receipt": observation.get("receipt")},
                "applied_at": _utc_now(),
            }
        )
        applied = store.append(applied, expected_revision=int(pending["revision"]))
        return bootstrap_response(operation="apply", status="applied", brain_id=store.brain_id, session=applied)

    def _simple_transition(
        self,
        store: BootstrapSessionStore,
        current: dict[str, Any],
        operation: str,
        lifecycle_state: str,
        key: str,
        digest: str,
    ) -> dict[str, Any]:
        updated = copy.deepcopy(current)
        updated["lifecycle_state"] = lifecycle_state
        return self._append_response(store, updated, operation, key, digest, status=lifecycle_state)

    def _append_response(
        self,
        store: BootstrapSessionStore,
        snapshot: dict[str, Any],
        operation: str,
        key: str,
        digest: str,
        *,
        status: str,
    ) -> dict[str, Any]:
        appended = self._append(store, snapshot, operation, key, digest)
        return bootstrap_response(operation=operation, status=status, brain_id=store.brain_id, session=appended)

    @staticmethod
    def _append(
        store: BootstrapSessionStore,
        snapshot: dict[str, Any],
        operation: str,
        key: str,
        digest: str,
    ) -> dict[str, Any]:
        updated = copy.deepcopy(snapshot)
        expected = int(updated.get("revision") or 0)
        updated.pop("revision_digest", None)
        updated["updated_at"] = _utc_now()
        updated["request"] = _request_record(operation, key, digest)
        return store.append(updated, expected_revision=expected)

    def _resolve_brain(self, brain_id: Any) -> dict[str, Any]:
        try:
            return self._brain_resolver(brain_id=str(brain_id or "").strip() or None, require_explicit=False)
        except BrainRegistryError as exc:
            raise BootstrapV1Error(str(exc), status_code=404) from exc

    def _store(self, brain_record: dict[str, Any]) -> BootstrapSessionStore:
        return BootstrapSessionStore(brain_record, brain_root=self._brain_root_resolver())


def _build_manual_grow_preview(session: dict[str, Any], brain_record: dict[str, Any]) -> dict[str, Any]:
    chunks = [f"{item.get('question_id')}: {item.get('answer')}" for item in list(session.get("answers") or [])]
    chunks.extend(str(item.get("source_text") or "") for item in list(session.get("sources") or []) if item.get("source_text"))
    raw_text = "\n\n".join(value.strip() for value in chunks if value.strip())
    if not raw_text:
        raise BootstrapV1Error("bootstrap_manual_material_required_before_preview", status_code=409)
    with use_runtime_brain(brain_record):
        bootstrap_runtime_store()
        graph = fetch_graph_snapshot()
        bundle = preview_bundle(
            raw_text,
            "text",
            graph,
            build_index(list(graph.get("nodes") or [])),
            fetch_atlas(),
            source_label="Brain Bootstrap V1 reviewed material",
            source_type="manual_bootstrap",
            source_trust="user_asserted",
            learning_mode="strict_review",
            source_purpose="bootstrap_seed",
            operator_instruction="Create reviewable bootstrap candidates without writing memory.",
        )
    selected = _preview_ids(bundle)
    return {
        "schema_version": "agvm.brain_bootstrap_v1.grow_review.v1",
        "preview_bundle": bundle,
        "selected_preview_ids": selected,
        "candidate_count": len(selected),
        "mutates_brain": False,
    }


def _apply_manual_grow_preview(
    preview: dict[str, Any], brain_record: dict[str, Any], selected_ids: list[str]
) -> dict[str, Any]:
    bundle = dict(preview.get("preview_bundle") or {})
    with use_runtime_brain(brain_record):
        bootstrap_runtime_store()
        graph = fetch_graph_snapshot()
        updated_graph, persisted_ids, persisted_edge_count, merged_ids, learning_policy = persist_selection(
            bundle,
            selected_ids,
            graph,
            build_index(list(graph.get("nodes") or [])),
            learning_mode="strict_review",
            approved_preview_ids=selected_ids,
        )
        replace_runtime_graph(updated_graph)
    return {
        "schema_version": "agvm.brain_bootstrap_v1.apply_result.v1",
        "status": "applied",
        "persisted_node_ids": persisted_ids,
        "persisted_edge_count": persisted_edge_count,
        "merged_into_existing_ids": merged_ids,
        "learning_policy": learning_policy,
    }


def _build_mutation_receipt(
    *,
    brain_record: dict[str, Any],
    session: dict[str, Any],
    preview: dict[str, Any],
    selected_ids: list[str],
    idempotency_key: str,
) -> dict[str, Any]:
    with use_runtime_brain(brain_record):
        bootstrap_runtime_store()
        graph = fetch_graph_snapshot()
    witnesses = _selected_preview_witnesses(preview, selected_ids)
    baseline_nodes = [dict(node) for node in list(graph.get("nodes") or []) if isinstance(node, dict)]
    baseline_node_digests = {
        str(node.get("id") or ""): _canonical_digest(node)
        for node in baseline_nodes
        if str(node.get("id") or "").strip()
    }
    baseline_edge_digests = sorted(
        _canonical_digest(dict(edge))
        for edge in list(graph.get("edges") or [])
        if isinstance(edge, dict)
    )
    semantic = {
        "brain_id": str(brain_record.get("brain_id") or ""),
        "session_id": str(session.get("session_id") or ""),
        "preview_digest": _canonical_digest(preview),
        "selected_preview_ids": sorted(selected_ids),
        "idempotency_key": idempotency_key,
    }
    return {
        "schema_version": "agvm.brain_bootstrap_v1.mutation_receipt.v1",
        "mutation_id": f"bbmut_{_canonical_digest(semantic)}",
        "baseline_graph_digest": _graph_digest(graph),
        "baseline_node_count": len(list(graph.get("nodes") or [])),
        "baseline_edge_count": len(list(graph.get("edges") or [])),
        "baseline_node_digests": baseline_node_digests,
        "baseline_edge_digests": baseline_edge_digests,
        "selected_preview_ids": sorted(selected_ids),
        "witnesses": witnesses,
    }


def _probe_manual_grow_mutation(brain_record: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    if receipt.get("schema_version") != "agvm.brain_bootstrap_v1.mutation_receipt.v1":
        return {"state": "ambiguous", "reason": "mutation_receipt_missing_or_invalid"}
    with use_runtime_brain(brain_record):
        bootstrap_runtime_store()
        graph = fetch_graph_snapshot()
    current_digest = _graph_digest(graph)
    if current_digest == str(receipt.get("baseline_graph_digest") or ""):
        return {"state": "not_applied", "reason": "canonical_graph_matches_pre_apply_cas"}
    nodes = [dict(node) for node in list(graph.get("nodes") or []) if isinstance(node, dict)]
    baseline_node_digests = dict(receipt.get("baseline_node_digests") or {})
    baseline_edge_digests = list(receipt.get("baseline_edge_digests") or [])
    if not baseline_node_digests and int(receipt.get("baseline_node_count") or 0) > 0:
        return {"state": "ambiguous", "reason": "mutation_receipt_baseline_nodes_missing"}
    if not baseline_edge_digests and int(receipt.get("baseline_edge_count") or 0) > 0:
        return {"state": "ambiguous", "reason": "mutation_receipt_baseline_edges_missing"}
    current_nodes: dict[str, dict[str, Any]] = {}
    for node in nodes:
        node_id = str(node.get("id") or "").strip()
        if not node_id or node_id in current_nodes:
            return {"state": "ambiguous", "reason": "current_graph_node_identity_invalid"}
        current_nodes[node_id] = node
    matched_node_ids: list[str] = []
    for witness in list(receipt.get("witnesses") or []):
        if not isinstance(witness, dict):
            return {"state": "ambiguous", "reason": "mutation_witness_invalid"}
        target_id = str(witness.get("merge_target_node_id") or "").strip()
        if target_id:
            match = next((node for node in nodes if str(node.get("id") or "") == target_id), None)
        else:
            fields = dict(witness.get("fields") or {})
            match = next((node for node in nodes if fields and _node_matches_witness(node, fields)), None)
        if not match:
            return {"state": "ambiguous", "reason": "mutation_witness_not_found"}
        matched_node_ids.append(str(match.get("id") or ""))
    if not matched_node_ids:
        return {"state": "ambiguous", "reason": "mutation_witnesses_empty"}
    matched_node_id_set = set(matched_node_ids)
    baseline_node_id_set = set(str(value) for value in baseline_node_digests)
    current_node_id_set = set(current_nodes)
    if not baseline_node_id_set.issubset(current_node_id_set):
        return {"state": "ambiguous", "reason": "unrelated_graph_mutation_detected"}
    new_node_ids = current_node_id_set - baseline_node_id_set
    if not new_node_ids.issubset(matched_node_id_set):
        return {"state": "ambiguous", "reason": "unrelated_graph_mutation_detected"}
    changed_baseline_ids = {
        node_id
        for node_id, baseline_digest in baseline_node_digests.items()
        if _canonical_digest(current_nodes[node_id]) != str(baseline_digest)
    }
    if not changed_baseline_ids.issubset(matched_node_id_set):
        return {"state": "ambiguous", "reason": "unrelated_graph_mutation_detected"}

    current_edges = [dict(edge) for edge in list(graph.get("edges") or []) if isinstance(edge, dict)]
    baseline_edge_counter = Counter(str(value) for value in baseline_edge_digests)
    current_edge_counter = Counter(_canonical_digest(edge) for edge in current_edges)
    if any(current_edge_counter[digest] < count for digest, count in baseline_edge_counter.items()):
        return {"state": "ambiguous", "reason": "unrelated_graph_mutation_detected"}
    remaining_baseline = baseline_edge_counter.copy()
    added_edges: list[dict[str, Any]] = []
    for edge in current_edges:
        digest = _canonical_digest(edge)
        if remaining_baseline[digest] > 0:
            remaining_baseline[digest] -= 1
        else:
            added_edges.append(edge)
    for edge in added_edges:
        source_id, target_id = _edge_endpoints(edge)
        if not source_id or not target_id or not ({source_id, target_id} & matched_node_id_set):
            return {"state": "ambiguous", "reason": "unrelated_graph_mutation_detected"}

    witnessed_change_ids = matched_node_id_set & (new_node_ids | changed_baseline_ids)
    if not witnessed_change_ids and not added_edges:
        return {"state": "ambiguous", "reason": "mutation_witness_preexisted_without_change"}
    edge_delta = len(added_edges)
    verified_receipt = {
        **dict(receipt),
        "post_graph_digest": current_digest,
        "matched_node_ids": matched_node_ids,
        "verified": True,
    }
    return {
        "state": "applied",
        "receipt": verified_receipt,
        "apply_result": {
            "schema_version": "agvm.brain_bootstrap_v1.apply_result.v1",
            "status": "applied",
            "persisted_node_ids": matched_node_ids,
            "persisted_edge_count": edge_delta,
            "merged_into_existing_ids": [],
            "recovered_from_mutation_receipt": True,
            "mutation_receipt": verified_receipt,
        },
    }


def _edge_endpoints(edge: dict[str, Any]) -> tuple[str, str]:
    source_id = str(
        edge.get("source_node_id") or edge.get("source") or edge.get("from_node_id") or edge.get("from") or ""
    ).strip()
    target_id = str(
        edge.get("target_node_id") or edge.get("target") or edge.get("to_node_id") or edge.get("to") or ""
    ).strip()
    return source_id, target_id


def _selected_preview_witnesses(preview: dict[str, Any], selected_ids: list[str]) -> list[dict[str, Any]]:
    bundle = dict(preview.get("preview_bundle") or {})
    candidates = [dict(bundle.get("primary_node_preview") or {})]
    candidates.extend(dict(item) for item in list(bundle.get("derived_nodes") or []) if isinstance(item, dict))
    by_id = {str(item.get("id") or item.get("preview_id") or ""): item for item in candidates}
    witnesses: list[dict[str, Any]] = []
    for selected_id in selected_ids:
        candidate = by_id.get(str(selected_id))
        if not candidate:
            raise BootstrapV1Error("bootstrap_selected_preview_witness_missing", status_code=422)
        fields = {
            key: _normalized_witness_value(candidate.get(key))
            for key in ("raw_text", "summary", "summary_full", "name", "memory_type", "node_kind", "source_unit_id")
            if _normalized_witness_value(candidate.get(key))
        }
        merge_target = str(candidate.get("merge_target_node_id") or "").strip()
        if not fields and not merge_target:
            raise BootstrapV1Error("bootstrap_selected_preview_witness_empty", status_code=422)
        witnesses.append(
            {
                "preview_id": str(selected_id),
                "merge_target_node_id": merge_target or None,
                "fields": fields,
            }
        )
    return witnesses


def _node_matches_witness(node: dict[str, Any], fields: dict[str, str]) -> bool:
    return all(_normalized_witness_value(node.get(key)) == value for key, value in fields.items())


def _normalized_witness_value(value: Any) -> str:
    return " ".join(str(value or "").split()).strip().lower()


def _graph_digest(graph: dict[str, Any]) -> str:
    payload = {
        "nodes": sorted((dict(item) for item in list(graph.get("nodes") or [])), key=lambda item: str(item.get("id") or "")),
        "edges": sorted(
            (dict(item) for item in list(graph.get("edges") or [])),
            key=lambda item: (
                str(item.get("source_node_id") or item.get("source") or ""),
                str(item.get("target_node_id") or item.get("target") or ""),
                str(item.get("edge_type") or item.get("kind") or ""),
            ),
        ),
    }
    return _canonical_digest(payload)


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _preview_ids(bundle: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for node in list(bundle.get("derived_nodes") or []):
        value = str(node.get("preview_id") or node.get("node_id") or node.get("id") or "").strip()
        if value and value not in values:
            values.append(value)
    primary = dict(bundle.get("primary_node_preview") or {})
    value = str(primary.get("preview_id") or primary.get("node_id") or primary.get("id") or "").strip()
    if value and value not in values:
        values.append(value)
    return values


def _required_text(payload: dict[str, Any], field: str, *, max_chars: int) -> str:
    value = str(payload.get(field) or "").strip()
    if not value:
        raise BootstrapV1Error(f"bootstrap_{field}_required", status_code=422)
    if len(value) > max_chars:
        raise BootstrapV1Error(f"bootstrap_{field}_too_large", status_code=422)
    return value


def _required_revision(payload: dict[str, Any]) -> int:
    value = payload.get("expected_revision")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise BootstrapV1Error("bootstrap_expected_revision_required", status_code=422)
    return value


def _bounded_text_list(value: Any, *, max_items: int, max_chars: int) -> list[str]:
    items = list(value or [])
    if len(items) > max_items:
        raise BootstrapV1Error("bootstrap_question_limit_reached", status_code=422)
    return [str(item).strip()[:max_chars] for item in items if str(item).strip()]


def _request_record(operation: str, key: str, digest: str) -> dict[str, Any]:
    return {"operation": operation, "idempotency_key": key, "request_digest": digest}


def _default_capability(operation: str) -> str:
    if operation == "add_source":
        return "manual_source"
    if operation in {"preview", "apply"}:
        return "grow_review"
    return "manual_interview"


def _operation_status(operation: str, revision: dict[str, Any]) -> str:
    return {
        "start": "started",
        "answer": "answer_recorded",
        "add_source": "source_recorded",
        "preview": "preview_ready",
        "apply": str(revision.get("lifecycle_state") or "apply_pending"),
        "resume": "resumed",
        "recover": "recovered",
        "cancel": "cancelled",
    }.get(operation, "ok")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
