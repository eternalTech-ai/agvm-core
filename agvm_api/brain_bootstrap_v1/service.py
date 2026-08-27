# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import unicodedata
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ai_modules_v2 import AiModuleContractError, validate_ai_execution_attestation
from brain_registry import BrainRegistryError, brain_root_path, refresh_local_brain_record, resolve_brain_scope
from derivation import _source_grounding_assessment, persist_selection, preview_bundle, resolve_persist_selection
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
GUIDED_SEED_QUALITY_POLICY = "guided_seed_v1"
GUIDED_SEED_MIN_CANDIDATES = 12
GUIDED_SEED_TARGET_CANDIDATES = 24
GUIDED_SEED_MAX_CANDIDATES = 30
GUIDED_SEED_MIN_ANSWERS = 6
GUIDED_SEED_MIN_SOURCE_TEXT_CHARS = 240
ADAPTIVE_INTERVIEW_MODE = "adaptive_ai"
ADAPTIVE_INTERVIEW_MIN_QUESTIONS = 6
ADAPTIVE_INTERVIEW_MAX_QUESTIONS = 16
SEED_CANDIDATE_MIN_LEXICAL_UNITS = 8
SEED_CANDIDATE_MIN_ALNUM_CHARS = 36
SEED_CANDIDATE_MAX_LEXICAL_UNITS = 80


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
        question_generator: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
        registry_committer: Callable[[dict[str, Any], dict[str, Any], Path], dict[str, Any]] | None = None,
    ) -> None:
        self._brain_resolver = brain_resolver
        self._brain_root_resolver = brain_root_resolver
        self._preview_builder = preview_builder or _build_manual_grow_preview
        self._apply_executor = apply_executor or _apply_manual_grow_preview
        self._mutation_probe = mutation_probe or _probe_manual_grow_mutation
        self._question_generator = question_generator or _generate_adaptive_interview
        self._registry_committer = registry_committer or _commit_bootstrap_registry

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
            goal = str(payload.get("goal") or "Build a reviewed initial memory seed.")[:4_000]
            quality_policy = (
                GUIDED_SEED_QUALITY_POLICY
                if str(payload.get("quality_policy") or "").strip() == GUIDED_SEED_QUALITY_POLICY
                else None
            )
            interview_mode = str(payload.get("interview_mode") or "manual").strip().lower()
            questions = _bounded_text_list(payload.get("questions"), max_items=32, max_chars=2_000)
            interview_plan: dict[str, Any] | None = None
            minimum_answer_count = GUIDED_SEED_MIN_ANSWERS
            if interview_mode == ADAPTIVE_INTERVIEW_MODE:
                if questions:
                    raise BootstrapV1Error("bootstrap_adaptive_interview_questions_forbidden", status_code=422)
                interview_plan = self._question_generator(goal, brain_record)
                try:
                    interview_attestation = validate_ai_execution_attestation(
                        dict(interview_plan.get("ai_execution_attestation") or {})
                    )
                except AiModuleContractError as exc:
                    raise BootstrapV1Error(
                        "bootstrap_question_generation_unattested",
                        status_code=502,
                    ) from exc
                interview_plan = {
                    **dict(interview_plan),
                    "ai_execution_attestation": interview_attestation,
                }
                questions = _validated_adaptive_questions(interview_plan)
                minimum_answer_count = _validated_adaptive_required_answer_count(
                    interview_plan,
                    question_count=len(questions),
                )
            elif interview_mode != "manual":
                raise BootstrapV1Error("bootstrap_interview_mode_not_supported", status_code=422)
            snapshot = {
                "session_id": session_id,
                "lifecycle_state": "interview_active",
                "created_at": _utc_now(),
                "updated_at": _utc_now(),
                "goal": goal,
                "quality_policy": quality_policy,
                "interview_mode": interview_mode,
                "interview_plan": interview_plan,
                "questions": questions,
                "answers": [],
                "sources": [],
                "quality_requirements": {
                    "minimum_candidate_count": GUIDED_SEED_MIN_CANDIDATES,
                    "target_candidate_count": GUIDED_SEED_TARGET_CANDIDATES,
                    "maximum_candidate_count": GUIDED_SEED_MAX_CANDIDATES,
                    "minimum_answer_count": minimum_answer_count,
                    "minimum_source_text_chars": GUIDED_SEED_MIN_SOURCE_TEXT_CHARS,
                },
                "preview": None,
                "apply_result": None,
                "request": _request_record("start", key, digest),
            }
            if quality_policy == GUIDED_SEED_QUALITY_POLICY:
                snapshot["quality"] = _bootstrap_seed_quality(
                    session=snapshot,
                    bundle={},
                    selected_ids=[],
                )
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
                reconciled = self._reconcile_applied_registry_replay(
                    operation=operation,
                    brain_record=brain_record,
                    store=store,
                    replay=replay,
                )
                if reconciled is not None:
                    return reconciled
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
                    recovered = self._append(store, recovered, operation, key, digest)
                    return self._commit_applied_registry(
                        operation=operation,
                        brain_record=brain_record,
                        store=store,
                        applied=recovered,
                    )
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
        if str(updated.get("quality_policy") or "") == GUIDED_SEED_QUALITY_POLICY:
            updated["quality"] = _bootstrap_seed_quality(session=updated, bundle={}, selected_ids=[])
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
        if str(updated.get("quality_policy") or "") == GUIDED_SEED_QUALITY_POLICY:
            updated["quality"] = _bootstrap_seed_quality(session=updated, bundle={}, selected_ids=[])
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
        updated.update(
            {
                "preview": preview,
                "quality": dict(preview.get("quality") or {}),
                "lifecycle_state": "preview_ready",
            }
        )
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
        selected = [str(value) for value in list(payload.get("selected_preview_ids") or []) if str(value)]
        if not selected or len(selected) != len(set(selected)) or not set(selected).issubset(set(available)):
            raise BootstrapV1Error("bootstrap_selected_preview_ids_invalid", status_code=422)
        if str(current.get("quality_policy") or "") == GUIDED_SEED_QUALITY_POLICY:
            quality = _bootstrap_seed_quality(
                session=current,
                bundle=dict(preview.get("preview_bundle") or {}),
                selected_ids=selected,
            )
            if quality.get("ready_to_apply") is not True:
                raise BootstrapV1Error("bootstrap_seed_quality_gate_not_met", status_code=409)

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
                return self._commit_applied_registry(
                    operation="apply",
                    brain_record=brain_record,
                    store=store,
                    applied=applied,
                )
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
        return self._commit_applied_registry(
            operation="apply",
            brain_record=brain_record,
            store=store,
            applied=applied,
        )

    def _reconcile_applied_registry_replay(
        self,
        *,
        operation: str,
        brain_record: dict[str, Any],
        store: BootstrapSessionStore,
        replay: dict[str, Any],
    ) -> dict[str, Any] | None:
        lifecycle_state = str(replay.get("lifecycle_state") or "")
        recovery_state = str(dict(replay.get("recovery") or {}).get("state") or "")
        if lifecycle_state == "recovery_required" and recovery_state == "registry_commit_failed":
            applied = copy.deepcopy(replay)
            applied.update(
                {
                    "lifecycle_state": "applied",
                    "applied_at": str(replay.get("applied_at") or _utc_now()),
                    "recovery": {
                        "state": "registry_commit_retry",
                        "automatic_reapply_allowed": False,
                    },
                }
            )
            applied = store.append(applied, expected_revision=int(replay["revision"]))
        elif lifecycle_state == "applied":
            applied = replay
        else:
            return None
        return self._commit_applied_registry(
            operation=operation,
            brain_record=brain_record,
            store=store,
            applied=applied,
            replayed=True,
        )

    def _commit_applied_registry(
        self,
        *,
        operation: str,
        brain_record: dict[str, Any],
        store: BootstrapSessionStore,
        applied: dict[str, Any],
        replayed: bool = False,
    ) -> dict[str, Any]:
        try:
            self._registry_committer(brain_record, applied, self._brain_root_resolver())
        except Exception as exc:
            failed = copy.deepcopy(applied)
            failed.update(
                {
                    "lifecycle_state": "recovery_required",
                    "recovery": {
                        "state": "registry_commit_failed",
                        "reason": type(exc).__name__,
                        "automatic_reapply_allowed": False,
                    },
                }
            )
            failed = store.append(failed, expected_revision=int(applied["revision"]))
            return bootstrap_response(
                operation=operation,
                status="recovery_required",
                brain_id=store.brain_id,
                session=failed,
                replayed=replayed,
            )
        return bootstrap_response(
            operation=operation,
            status="applied",
            brain_id=store.brain_id,
            session=applied,
            replayed=replayed,
        )

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


def _commit_bootstrap_registry(
    brain_record: dict[str, Any],
    applied_session: dict[str, Any],
    brain_root: Path,
) -> dict[str, Any]:
    apply_result = dict(applied_session.get("apply_result") or {})
    persisted_node_ids = {
        str(value)
        for value in list(apply_result.get("persisted_node_ids") or [])
        if str(value).strip()
    }
    return refresh_local_brain_record(
        str(brain_record.get("brain_id") or ""),
        brain_root=brain_root,
        minimum_node_count=max(1, len(persisted_node_ids)),
        expected_bootstrap_state="applied",
        expected_bootstrap_session_id=str(applied_session.get("session_id") or ""),
    )


def _generate_adaptive_interview(goal: str, brain_record: dict[str, Any]) -> dict[str, Any]:
    from llm import compiler_model, structured_json

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise BootstrapV1Error("bootstrap_question_generation_unavailable", status_code=503)
    schema = {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "minItems": ADAPTIVE_INTERVIEW_MIN_QUESTIONS,
                "maxItems": ADAPTIVE_INTERVIEW_MAX_QUESTIONS,
                "items": {"type": "string", "minLength": 12, "maxLength": 500},
            },
            "required_answer_count": {
                "type": "integer",
                "minimum": ADAPTIVE_INTERVIEW_MIN_QUESTIONS,
                "maximum": ADAPTIVE_INTERVIEW_MAX_QUESTIONS,
            },
            "coverage_dimensions": {
                "type": "array",
                "minItems": ADAPTIVE_INTERVIEW_MIN_QUESTIONS,
                "maxItems": ADAPTIVE_INTERVIEW_MAX_QUESTIONS,
                "items": {"type": "string", "minLength": 3, "maxLength": 80},
            },
        },
        "required": ["questions", "required_answer_count", "coverage_dimensions"],
        "additionalProperties": False,
    }
    brain_name = str(brain_record.get("display_name") or brain_record.get("brain_id") or "New brain").strip()
    execution_metadata: dict[str, Any] = {}
    generated, error = structured_json(
        system_prompt=(
            "Design an adaptive human-in-the-loop interview for a new memory brain. "
            "Every question must be specific to the supplied purpose and necessary to establish the users, "
            "decisions, trusted evidence, uncertainty and clarification rules, privacy and safety boundaries, "
            "correction behavior, and success criteria. Choose the number of questions according to domain "
            "complexity within the schema bounds. Do not answer the questions and do not use generic filler."
        ),
        user_prompt=json.dumps(
            {"brain_name": brain_name, "brain_purpose": goal},
            ensure_ascii=False,
            sort_keys=True,
        ),
        schema_name="agvm_brain_bootstrap_adaptive_interview_v1",
        schema=schema,
        model=compiler_model(),
        timeout=45.0,
        role="compiler",
        max_output_tokens=3_000,
        api_key_override=api_key,
        execution_metadata=execution_metadata,
    )
    if error or not isinstance(generated, dict):
        raise BootstrapV1Error("bootstrap_question_generation_unavailable", status_code=503)
    try:
        attestation = validate_ai_execution_attestation(execution_metadata)
    except AiModuleContractError as exc:
        raise BootstrapV1Error(
            "bootstrap_question_generation_unattested",
            status_code=502,
        ) from exc
    return {
        **generated,
        "schema_version": "agvm.brain_bootstrap_v1.adaptive_interview.v1",
        "generation_source": "provider",
        "model": compiler_model(),
        "ai_execution_attestation": attestation,
    }


def _validated_adaptive_questions(interview_plan: dict[str, Any]) -> list[str]:
    questions = _bounded_text_list(
        interview_plan.get("questions"),
        max_items=ADAPTIVE_INTERVIEW_MAX_QUESTIONS,
        max_chars=500,
    )
    normalized = [" ".join(question.lower().split()) for question in questions]
    if (
        len(questions) < ADAPTIVE_INTERVIEW_MIN_QUESTIONS
        or len(questions) > ADAPTIVE_INTERVIEW_MAX_QUESTIONS
        or len(set(normalized)) != len(questions)
        or any(len(question) < 12 for question in questions)
    ):
        raise BootstrapV1Error("bootstrap_question_generation_invalid", status_code=502)
    return questions


def _validated_adaptive_required_answer_count(
    interview_plan: dict[str, Any],
    *,
    question_count: int,
) -> int:
    value = interview_plan.get("required_answer_count")
    if isinstance(value, bool):
        raise BootstrapV1Error("bootstrap_question_generation_invalid", status_code=502)
    try:
        required = int(value)
    except (TypeError, ValueError) as exc:
        raise BootstrapV1Error("bootstrap_question_generation_invalid", status_code=502) from exc
    if not ADAPTIVE_INTERVIEW_MIN_QUESTIONS <= required <= question_count:
        raise BootstrapV1Error("bootstrap_question_generation_invalid", status_code=502)
    return required


def _build_manual_grow_preview(session: dict[str, Any], brain_record: dict[str, Any]) -> dict[str, Any]:
    raw_text = _bootstrap_reviewed_material(session)
    if not raw_text:
        raise BootstrapV1Error("bootstrap_manual_material_required_before_preview", status_code=409)
    requirements = _guided_seed_requirements(session)
    with use_runtime_brain(brain_record):
        bootstrap_runtime_store()
        graph = fetch_graph_snapshot()
        if str(session.get("quality_policy") or "") == GUIDED_SEED_QUALITY_POLICY:
            bundle = _build_guided_seed_bundle(
                session=session,
                raw_text=raw_text,
                graph=graph,
                index=build_index(list(graph.get("nodes") or [])),
                atlas=fetch_atlas(),
                requirements=requirements,
            )
        else:
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
            )
    screening: dict[str, Any] | None = None
    if str(session.get("quality_policy") or "") == GUIDED_SEED_QUALITY_POLICY:
        selected, screening = _screen_seed_candidates(session=session, bundle=bundle)
    else:
        selected = _preview_ids(bundle)
    quality = _bootstrap_seed_quality(session=session, bundle=bundle, selected_ids=selected)
    return {
        "schema_version": "agvm.brain_bootstrap_v1.grow_review.v1",
        "preview_bundle": bundle,
        "selected_preview_ids": selected,
        "candidate_count": len(selected),
        "candidate_screening": screening,
        "quality": quality,
        "mutates_brain": False,
    }


def _build_guided_seed_bundle(
    *,
    session: dict[str, Any],
    raw_text: str,
    graph: dict[str, Any],
    index: dict[str, Any],
    atlas: dict[str, Any],
    requirements: dict[str, int],
) -> dict[str, Any]:
    statements = _reviewed_atomic_seed_statements(session)
    candidates: list[dict[str, Any]] = []
    for position, statement in enumerate(statements[: requirements["maximum_candidate_count"]], start=1):
        atom_bundle = preview_bundle(
            statement,
            "text",
            graph,
            index,
            atlas,
            source_label="Brain Bootstrap V1 reviewed material",
            source_type="manual_bootstrap",
            source_trust="user_asserted",
            learning_mode="strict_review",
            source_purpose="bootstrap_seed",
            operator_instruction="Preserve this reviewed atomic memory exactly; do not expand or invent content.",
            source_context={
                "bootstrap_quality_policy": GUIDED_SEED_QUALITY_POLICY,
                "candidate_target": requirements["target_candidate_count"],
                "candidate_maximum": requirements["maximum_candidate_count"],
                "atomic_seed_position": position,
            },
        )
        candidate = dict(atom_bundle.get("primary_node_preview") or {})
        if not candidate:
            continue
        preview_id = f"bootstrap_seed_{position:03d}"
        candidate.update(
            {
                "id": preview_id,
                "preview_id": preview_id,
                "preview_kind": "derived",
                "derivation_role": "reviewed_atomic_seed",
                "raw_text": statement,
                "summary": statement,
                "selected_by_default": True,
                "source_label": "Brain Bootstrap V1 reviewed material",
                "source_type": "manual_bootstrap",
                "source_trust": "user_asserted",
            }
        )
        candidate.pop("summary_full", None)
        provenance = dict(candidate.get("provenance") or {})
        provenance.update(
            {
                "mode": "agvm_brain_bootstrap_reviewed_atomic_seed",
                "source_label": "Brain Bootstrap V1 reviewed material",
                "source_type": "manual_bootstrap",
                "source_trust": "user_asserted",
            }
        )
        candidate["provenance"] = provenance
        candidates.append(candidate)
    return {
        "schema_version": "agvm.preview_bundle.v1",
        "input_mode": "text",
        "source_label": "Brain Bootstrap V1 reviewed material",
        "source_type": "manual_bootstrap",
        "source_trust": "user_asserted",
        "raw_text": raw_text,
        "primary_node_preview": {},
        "derived_nodes": candidates,
        "warnings": [],
        "preview_quality_contract": {
            "schema_version": "agvm.preview_quality_contract.v1",
            "apply_safe": True,
            "blocking_reasons": [],
            "rows": [],
            "source_scope": True,
        },
    }


def _reviewed_atomic_seed_statements(session: dict[str, Any]) -> list[str]:
    source_chunks = [
        str(item.get("source_text") or "").strip()
        for item in list(session.get("sources") or [])
        if isinstance(item, dict) and str(item.get("source_text") or "").strip()
    ]
    answer_chunks = [
        str(item.get("answer") or "").strip()
        for item in list(session.get("answers") or [])
        if isinstance(item, dict) and str(item.get("answer") or "").strip()
    ]
    raw_candidates: list[str] = []
    for chunk in [*source_chunks, *answer_chunks]:
        raw_candidates.extend(
            part.strip()
            for part in re.split(r"(?<=[.!?\u3002\uff01\uff1f])\s+|[\r\n]+", chunk)
            if part.strip()
        )
    statements: list[str] = []
    semantic_rows: list[tuple[str, set[str]]] = []
    for value in raw_candidates:
        statement = " ".join(value.split()).strip()
        if statement and statement[-1] not in ".!?\u3002\uff01\uff1f":
            statement = f"{statement}."
        shape = _seed_candidate_shape(statement)
        if not (shape["complete_sentence"] and shape["informative"] and shape["atomic"]):
            continue
        semantic_text, semantic_tokens = _seed_candidate_semantic_form(statement)
        if any(
            _seed_candidates_are_duplicates(semantic_text, semantic_tokens, prior_text, prior_tokens)
            for prior_text, prior_tokens in semantic_rows
        ):
            continue
        semantic_rows.append((semantic_text, semantic_tokens))
        statements.append(statement)
    return statements


def _bootstrap_seed_quality(
    *,
    session: dict[str, Any],
    bundle: dict[str, Any],
    selected_ids: list[str],
) -> dict[str, Any]:
    policy = str(session.get("quality_policy") or "").strip()
    requirements = _guided_seed_requirements(session)
    candidates = [dict(bundle.get("primary_node_preview") or {})]
    candidates.extend(dict(item) for item in list(bundle.get("derived_nodes") or []) if isinstance(item, dict))
    selected = {str(value).strip() for value in selected_ids if str(value).strip()}
    selected_candidates = [
        item
        for item in candidates
        if str(item.get("preview_id") or item.get("node_id") or item.get("id") or "").strip() in selected
    ]
    selected_candidate_ids = {
        str(item.get("preview_id") or item.get("node_id") or item.get("id") or "").strip()
        for item in selected_candidates
    }
    missing_candidate_ids = sorted(selected - selected_candidate_ids)
    reviewed_material = _bootstrap_reviewed_material(session)
    ungrounded_candidate_ids: list[str] = []
    incomplete_candidate_ids: list[str] = []
    low_information_candidate_ids: list[str] = []
    non_atomic_candidate_ids: list[str] = []
    unverifiable_provenance_candidate_ids: list[str] = []
    semantic_representatives: list[tuple[str, str, set[str]]] = []
    duplicate_candidate_ids: list[str] = []
    for item in selected_candidates:
        candidate_id = str(item.get("preview_id") or item.get("node_id") or item.get("id") or "").strip()
        candidate_text = str(item.get("raw_text") or item.get("summary_full") or item.get("summary") or "").strip()
        candidate_shape = _seed_candidate_shape(candidate_text)
        if not candidate_shape["complete_sentence"]:
            incomplete_candidate_ids.append(candidate_id)
        if not candidate_shape["informative"]:
            low_information_candidate_ids.append(candidate_id)
        if not candidate_shape["atomic"]:
            non_atomic_candidate_ids.append(candidate_id)
        if not _seed_candidate_has_verifiable_provenance(item):
            unverifiable_provenance_candidate_ids.append(candidate_id)
        assessment = _source_grounding_assessment(
            reviewed_material,
            candidate_text,
            role=str(item.get("derivation_role") or "claim"),
        )
        if not bool(assessment.get("supported")):
            ungrounded_candidate_ids.append(candidate_id)
        semantic_text, semantic_tokens = _seed_candidate_semantic_form(candidate_text)
        if any(
            _seed_candidates_are_duplicates(semantic_text, semantic_tokens, prior_text, prior_tokens)
            for _prior_id, prior_text, prior_tokens in semantic_representatives
        ):
            duplicate_candidate_ids.append(candidate_id)
        else:
            semantic_representatives.append((candidate_id, semantic_text, semantic_tokens))
    unique_count = len(semantic_representatives)
    duplicate_ratio = 0.0 if not selected_candidates else len(duplicate_candidate_ids) / len(selected_candidates)
    answer_count = len([item for item in list(session.get("answers") or []) if str(item.get("answer") or "").strip()])
    source_text_chars = sum(
        len(str(item.get("source_text") or "").strip())
        for item in list(session.get("sources") or [])
        if isinstance(item, dict)
    )
    issues: list[str] = []
    if policy == GUIDED_SEED_QUALITY_POLICY:
        if answer_count < requirements["minimum_answer_count"]:
            issues.append("more_structured_answers_required")
        if source_text_chars < requirements["minimum_source_text_chars"]:
            issues.append("trusted_foundation_text_required")
        if (
            len(selected) < requirements["minimum_candidate_count"]
            or unique_count < requirements["minimum_candidate_count"]
        ):
            issues.append("too_few_atomic_candidates")
        if len(selected) > requirements["maximum_candidate_count"]:
            issues.append("too_many_atomic_candidates")
        if missing_candidate_ids:
            issues.append("selected_candidates_missing_from_preview")
        if incomplete_candidate_ids:
            issues.append("incomplete_candidate_sentences_detected")
        if low_information_candidate_ids:
            issues.append("candidate_information_below_minimum")
        if non_atomic_candidate_ids:
            issues.append("non_atomic_candidates_detected")
        if duplicate_candidate_ids:
            issues.append("duplicate_candidates_detected")
        if unverifiable_provenance_candidate_ids:
            issues.append("candidate_provenance_unverifiable")
        if ungrounded_candidate_ids:
            issues.append("ungrounded_candidates_detected")
    return {
        "schema_version": "agvm.brain_bootstrap_v1.seed_quality.v1",
        "policy": policy or "legacy",
        "ready_to_apply": not issues,
        "issues": issues,
        "candidate_count": len(selected),
        "unique_candidate_count": unique_count,
        "duplicate_ratio": round(duplicate_ratio, 4),
        "duplicate_candidate_ids": duplicate_candidate_ids,
        "missing_candidate_ids": missing_candidate_ids,
        "incomplete_candidate_ids": incomplete_candidate_ids,
        "low_information_candidate_ids": low_information_candidate_ids,
        "non_atomic_candidate_ids": non_atomic_candidate_ids,
        "unverifiable_provenance_candidate_ids": unverifiable_provenance_candidate_ids,
        "ungrounded_candidate_count": len(ungrounded_candidate_ids),
        "ungrounded_candidate_ids": ungrounded_candidate_ids,
        "answer_count": answer_count,
        "source_text_chars": source_text_chars,
        **requirements,
    }


def _seed_candidate_shape(value: str) -> dict[str, Any]:
    text = _seed_candidate_content_text(value)
    lexical_units = _seed_candidate_lexical_units(text)
    alnum_chars = sum(1 for char in text if char.isalnum())
    terminal_count = len(re.findall(r"[.!?\u3002\uff01\uff1f]+(?:[\"'\)\]\u201d\u2019]+)?(?=\s|$)", text))
    first_cased = next((char for char in text if char.isalpha() and char.lower() != char.upper()), "")
    complete_sentence = bool(
        text
        and terminal_count == 1
        and re.search(r"[.!?\u3002\uff01\uff1f](?:[\"'\)\]\u201d\u2019]*)$", text)
        and (not first_cased or first_cased.isupper())
    )
    informative = bool(
        len(lexical_units) >= SEED_CANDIDATE_MIN_LEXICAL_UNITS
        and alnum_chars >= SEED_CANDIDATE_MIN_ALNUM_CHARS
        and len(set(lexical_units)) >= max(5, SEED_CANDIDATE_MIN_LEXICAL_UNITS // 2)
    )
    atomic = bool(terminal_count == 1 and len(lexical_units) <= SEED_CANDIDATE_MAX_LEXICAL_UNITS)
    return {
        "complete_sentence": complete_sentence,
        "informative": informative,
        "atomic": atomic,
        "lexical_unit_count": len(lexical_units),
        "alnum_char_count": alnum_chars,
        "terminal_count": terminal_count,
    }


def _seed_candidate_content_text(value: str) -> str:
    text = " ".join(str(value or "").split()).strip()
    prefix = re.match(r"^[^:\n]{1,64}:\s+(.+)$", text)
    return str(prefix.group(1) if prefix else text).strip()


def _seed_candidate_lexical_units(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    words = re.findall(r"[^\W_]+(?:['\u2019-][^\W_]+)*", normalized, flags=re.UNICODE)
    if len(words) >= 2:
        return words
    ideographs = re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", normalized)
    return ideographs or words


def _seed_candidate_semantic_form(value: str) -> tuple[str, set[str]]:
    units = [unit for unit in _seed_candidate_lexical_units(_seed_candidate_content_text(value)) if not unit.isdigit()]
    return " ".join(units), set(units)


def _seed_candidates_are_duplicates(
    left_text: str,
    left_tokens: set[str],
    right_text: str,
    right_tokens: set[str],
) -> bool:
    if not left_text or not right_text:
        return False
    if left_text == right_text:
        return True
    if min(len(left_tokens), len(right_tokens)) < 5:
        return False
    overlap = len(left_tokens & right_tokens)
    containment = overlap / min(len(left_tokens), len(right_tokens))
    union = len(left_tokens | right_tokens)
    jaccard = overlap / union if union else 0.0
    return containment >= 0.88 and jaccard >= 0.78


def _seed_candidate_has_verifiable_provenance(item: dict[str, Any]) -> bool:
    provenance = dict(item.get("provenance") or {})
    source_label = str(provenance.get("source_label") or item.get("source_label") or "").strip()
    source_type = str(provenance.get("source_type") or item.get("source_type") or "").strip()
    source_trust = str(item.get("source_trust") or provenance.get("source_trust") or "").strip()
    provenance_mode = str(provenance.get("mode") or "").strip()
    return bool(source_label and source_type and source_trust and provenance_mode)


def _screen_seed_candidates(*, session: dict[str, Any], bundle: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    reviewed_material = _bootstrap_reviewed_material(session)
    candidates = [dict(item) for item in list(bundle.get("derived_nodes") or []) if isinstance(item, dict)]
    primary = dict(bundle.get("primary_node_preview") or {})
    if primary:
        candidates.append(primary)
    accepted_ids: list[str] = []
    rejected: list[dict[str, Any]] = []
    semantic_representatives: list[tuple[str, set[str]]] = []
    for item in candidates:
        candidate_id = str(item.get("preview_id") or item.get("node_id") or item.get("id") or "").strip()
        candidate_text = str(item.get("raw_text") or item.get("summary_full") or item.get("summary") or "").strip()
        shape = _seed_candidate_shape(candidate_text)
        reasons: list[str] = []
        if not candidate_id:
            reasons.append("candidate_identity_missing")
        if not shape["complete_sentence"]:
            reasons.append("incomplete_sentence")
        if not shape["informative"]:
            reasons.append("information_below_minimum")
        if not shape["atomic"]:
            reasons.append("not_atomic")
        if not _seed_candidate_has_verifiable_provenance(item):
            reasons.append("provenance_unverifiable")
        grounding = _source_grounding_assessment(
            reviewed_material,
            candidate_text,
            role=str(item.get("derivation_role") or "claim"),
        )
        if not bool(grounding.get("supported")):
            reasons.append("not_grounded_in_reviewed_material")
        semantic_text, semantic_tokens = _seed_candidate_semantic_form(candidate_text)
        if any(
            _seed_candidates_are_duplicates(semantic_text, semantic_tokens, prior_text, prior_tokens)
            for prior_text, prior_tokens in semantic_representatives
        ):
            reasons.append("semantic_duplicate")
        if reasons:
            rejected.append({"preview_id": candidate_id or None, "reasons": reasons})
            continue
        accepted_ids.append(candidate_id)
        semantic_representatives.append((semantic_text, semantic_tokens))
    return accepted_ids, {
        "schema_version": "agvm.brain_bootstrap_v1.candidate_screening.v1",
        "fail_closed": True,
        "accepted_count": len(accepted_ids),
        "rejected_count": len(rejected),
        "rejected": rejected,
    }


def _guided_seed_requirements(session: dict[str, Any]) -> dict[str, int]:
    configured = session.get("quality_requirements")
    values = dict(configured) if isinstance(configured, dict) else {}
    requirements = {
        "minimum_candidate_count": _bounded_requirement(
            values.get("minimum_candidate_count"),
            default=GUIDED_SEED_MIN_CANDIDATES,
            minimum=1,
            maximum=GUIDED_SEED_MAX_CANDIDATES,
        ),
        "target_candidate_count": _bounded_requirement(
            values.get("target_candidate_count"),
            default=GUIDED_SEED_TARGET_CANDIDATES,
            minimum=1,
            maximum=GUIDED_SEED_MAX_CANDIDATES,
        ),
        "maximum_candidate_count": _bounded_requirement(
            values.get("maximum_candidate_count"),
            default=GUIDED_SEED_MAX_CANDIDATES,
            minimum=1,
            maximum=GUIDED_SEED_MAX_CANDIDATES,
        ),
        "minimum_answer_count": _bounded_requirement(
            values.get("minimum_answer_count"),
            default=GUIDED_SEED_MIN_ANSWERS,
            minimum=1,
            maximum=MAX_ANSWERS,
        ),
        "minimum_source_text_chars": _bounded_requirement(
            values.get("minimum_source_text_chars"),
            default=GUIDED_SEED_MIN_SOURCE_TEXT_CHARS,
            minimum=1,
            maximum=MAX_SOURCE_CHARS * MAX_SOURCES,
        ),
    }
    if not (
        requirements["minimum_candidate_count"]
        <= requirements["target_candidate_count"]
        <= requirements["maximum_candidate_count"]
    ):
        raise BootstrapV1Error("bootstrap_quality_requirements_invalid", status_code=409)
    question_count = len(list(session.get("questions") or []))
    if (
        str(session.get("interview_mode") or "") == ADAPTIVE_INTERVIEW_MODE
        and requirements["minimum_answer_count"] > question_count
    ):
        raise BootstrapV1Error("bootstrap_quality_requirements_invalid", status_code=409)
    return requirements


def _bounded_requirement(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise BootstrapV1Error("bootstrap_quality_requirements_invalid", status_code=409)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise BootstrapV1Error("bootstrap_quality_requirements_invalid", status_code=409) from exc
    if not minimum <= parsed <= maximum:
        raise BootstrapV1Error("bootstrap_quality_requirements_invalid", status_code=409)
    return parsed


def _bootstrap_reviewed_material(session: dict[str, Any]) -> str:
    chunks = [
        f"{str(item.get('question_id') or '').strip()}: {str(item.get('answer') or '').strip()}".strip(": ")
        for item in list(session.get("answers") or [])
        if isinstance(item, dict) and str(item.get("answer") or "").strip()
    ]
    chunks.extend(
        str(item.get("source_text") or "").strip()
        for item in list(session.get("sources") or [])
        if isinstance(item, dict) and str(item.get("source_text") or "").strip()
    )
    return "\n\n".join(value for value in chunks if value)


def _apply_manual_grow_preview(
    preview: dict[str, Any], brain_record: dict[str, Any], selected_ids: list[str]
) -> dict[str, Any]:
    bundle = _bootstrap_persist_bundle(preview)
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
            include_primary=False,
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
    bundle = _bootstrap_persist_bundle(preview)
    effective_selected_ids, learning_policy = resolve_persist_selection(
        bundle,
        selected_ids,
        learning_mode="strict_review",
        approved_preview_ids=selected_ids,
        include_primary=False,
    )
    if not effective_selected_ids:
        raise BootstrapV1Error("bootstrap_selected_preview_ids_not_persistable", status_code=409)
    witnesses = _selected_preview_witnesses(preview, effective_selected_ids)
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
        "effective_selected_preview_ids": effective_selected_ids,
        "suppressed_preview_ids": list(
            (learning_policy.get("selection_resolution") or {}).get("suppressed_preview_ids") or []
        ),
        "witnesses": witnesses,
    }


def _bootstrap_persist_bundle(preview: dict[str, Any]) -> dict[str, Any]:
    bundle = copy.deepcopy(dict(preview.get("preview_bundle") or {}))
    primary = dict(bundle.get("primary_node_preview") or {})
    if not str(primary.get("id") or primary.get("preview_id") or "").strip():
        primary = {
            "id": "bootstrap_primary_not_selected",
            "preview_id": "bootstrap_primary_not_selected",
            "selected_by_default": False,
        }
    bundle["primary_node_preview"] = primary
    return bundle


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

    # Grow materializes one implicit primary anchor even when the UI submits only
    # its reviewed derived candidates. Accept that root only when every edge it
    # adds is a derives_from edge into a witnessed candidate.
    auxiliary_new_node_ids = new_node_ids - matched_node_id_set
    if len(auxiliary_new_node_ids) > 1:
        return {"state": "ambiguous", "reason": "unrelated_graph_mutation_detected"}
    for auxiliary_node_id in auxiliary_new_node_ids:
        auxiliary_edges = [
            edge
            for edge in added_edges
            if auxiliary_node_id in set(_edge_endpoints(edge))
        ]
        if not auxiliary_edges:
            return {"state": "ambiguous", "reason": "unrelated_graph_mutation_detected"}
        for edge in auxiliary_edges:
            source_id, target_id = _edge_endpoints(edge)
            if (
                source_id != auxiliary_node_id
                or target_id not in matched_node_id_set
                or str(edge.get("edge_type") or "").strip() != "derives_from"
            ):
                return {"state": "ambiguous", "reason": "unrelated_graph_mutation_detected"}

    witnessed_change_ids = matched_node_id_set & (new_node_ids | changed_baseline_ids)
    if not witnessed_change_ids and not added_edges:
        return {"state": "ambiguous", "reason": "mutation_witness_preexisted_without_change"}
    edge_delta = len(added_edges)
    verified_node_ids = [*matched_node_ids, *sorted(auxiliary_new_node_ids)]
    verified_receipt = {
        **dict(receipt),
        "post_graph_digest": current_digest,
        "matched_node_ids": verified_node_ids,
        "verified": True,
    }
    return {
        "state": "applied",
        "receipt": verified_receipt,
        "apply_result": {
            "schema_version": "agvm.brain_bootstrap_v1.apply_result.v1",
            "status": "applied",
            "persisted_node_ids": verified_node_ids,
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
