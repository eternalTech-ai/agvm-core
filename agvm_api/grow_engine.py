# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

try:
    from .ai_modules_v2 import (
        dynamic_clarification_questions,
        validate_ai_execution_attestation,
    )
    from .derivation import preview_bundle
    from .runtime_feature_flags import GROW_V2_FLAG, require_runtime_feature
except ImportError:  # pragma: no cover - local runtime exposes agvm_api on PYTHONPATH
    from ai_modules_v2 import (
        dynamic_clarification_questions,
        validate_ai_execution_attestation,
    )
    from derivation import preview_bundle
    from runtime_feature_flags import GROW_V2_FLAG, require_runtime_feature


class GrowEngine:
    """One fail-closed semantic Grow pipeline shared by Local and Cloud."""

    def preview(
        self,
        *,
        raw_input: str,
        input_mode: str,
        graph: dict[str, Any],
        index_payload: dict[str, Any],
        atlas_payload: dict[str, Any],
        source_label: str | None = None,
        source_uri: str | None = None,
        source_type: str | None = None,
        source_trust: str = "unknown",
        learning_mode: str = "strict_review",
        question_limit: int = 12,
        clarification_answers: Mapping[str, Any] | None = None,
        source_sections: list[dict[str, Any]] | None = None,
        source_unit_formation: Mapping[str, Any] | None = None,
        source_context: Mapping[str, Any] | None = None,
        source_investigation_id: str | None = None,
        source_purpose: str | None = None,
        operator_instruction: str | None = None,
        compiler_timeout_seconds: float | None = None,
        api_key_override: str | None = None,
        model_override: str | None = None,
        brain_revision: str | None = None,
        investigation: Mapping[str, Any] | None = None,
        correlation_id: str | None = None,
        parent_operation_id: str | None = None,
    ) -> dict[str, Any]:
        require_runtime_feature(GROW_V2_FLAG, disabled_code="agvm_grow_v2_disabled")
        if not brain_revision:
            raise ValueError("grow_ai_investigator_brain_revision_required")
        return self._preview_v3(
            raw_input=raw_input,
            input_mode=input_mode,
            graph=graph,
            index_payload=index_payload,
            atlas_payload=atlas_payload,
            source_label=source_label,
            source_uri=source_uri,
            source_type=source_type,
            source_trust=source_trust,
            learning_mode=learning_mode,
            question_limit=question_limit,
            clarification_answers=clarification_answers,
            source_sections=source_sections,
            source_unit_formation=source_unit_formation,
            source_context=source_context,
            source_investigation_id=source_investigation_id,
            source_purpose=source_purpose,
            operator_instruction=operator_instruction,
            compiler_timeout_seconds=compiler_timeout_seconds,
            api_key_override=api_key_override,
            model_override=model_override,
            brain_revision=brain_revision,
            investigation=investigation,
            correlation_id=correlation_id,
            parent_operation_id=parent_operation_id,
        )

    def _preview_v3(
        self,
        *,
        raw_input: str,
        input_mode: str,
        graph: dict[str, Any],
        index_payload: dict[str, Any],
        atlas_payload: dict[str, Any],
        source_label: str | None,
        source_uri: str | None,
        source_type: str | None,
        source_trust: str,
        learning_mode: str,
        question_limit: int,
        clarification_answers: Mapping[str, Any] | None,
        source_sections: list[dict[str, Any]] | None,
        source_unit_formation: Mapping[str, Any] | None,
        source_context: Mapping[str, Any] | None,
        source_investigation_id: str | None,
        source_purpose: str | None,
        operator_instruction: str | None,
        compiler_timeout_seconds: float | None,
        api_key_override: str | None,
        model_override: str | None,
        brain_revision: str,
        investigation: Mapping[str, Any] | None,
        correlation_id: str | None,
        parent_operation_id: str | None,
    ) -> dict[str, Any]:
        try:
            from .grow_investigator import (
                GrowInvestigationBudget,
                resolve_grow_investigator_provider,
                run_grow_investigation,
            )
        except ImportError:  # pragma: no cover - local runtime exposes agvm_api on PYTHONPATH
            from grow_investigator import (
                GrowInvestigationBudget,
                resolve_grow_investigator_provider,
                run_grow_investigation,
            )

        canonical_source = dict(source_context or {})
        provider_request = {
            "api_key_override": api_key_override,
            "model_override": model_override,
            "source_investigation_id": source_investigation_id,
        }
        try:
            provider = resolve_grow_investigator_provider(provider_request)
        except TypeError:
            provider = resolve_grow_investigator_provider()
        investigator_budget = None
        if compiler_timeout_seconds is not None:
            try:
                requested_timeout = float(compiler_timeout_seconds)
            except (TypeError, ValueError):
                requested_timeout = 0.0
            if requested_timeout > 0.0:
                env_budget = GrowInvestigationBudget.from_env()
                wall_seconds = max(
                    5,
                    min(int(requested_timeout), int(env_budget.wall_budget_seconds)),
                )
                quick_preview = wall_seconds <= 90
                provider_timeout = max(
                    1,
                    min(
                        int(env_budget.provider_timeout_seconds),
                        max(1, int(wall_seconds * (0.30 if quick_preview else 0.45))),
                    ),
                )
                review_reserve = max(
                    1,
                    min(
                        int(env_budget.ai_review_reserve_seconds),
                        max(1, int(wall_seconds * (0.10 if quick_preview else 0.2))),
                        max(1, wall_seconds - provider_timeout - 1),
                    ),
                )
                investigator_budget = GrowInvestigationBudget(
                    **{
                        **env_budget.__dict__,
                        "wall_budget_seconds": wall_seconds,
                        "provider_timeout_seconds": provider_timeout,
                        "ai_review_reserve_seconds": review_reserve,
                        "max_turns": (
                            1
                            if quick_preview
                            else min(3, max(int(env_budget.max_turns), 3))
                        ),
                        "max_search_calls": min(int(env_budget.max_search_calls), 1 if quick_preview else 2),
                        "max_tool_calls": min(int(env_budget.max_tool_calls), 4 if quick_preview else 6),
                        "max_claims": min(int(env_budget.max_claims), 24 if quick_preview else 64),
                        "max_evidence_references": min(
                            int(env_budget.max_evidence_references),
                            96 if quick_preview else 192,
                        ),
                        "max_notebook_chars": min(
                            int(env_budget.max_notebook_chars),
                            12000 if quick_preview else 18000,
                        ),
                        "max_hydration_nodes_per_review": min(
                            int(env_budget.max_hydration_nodes_per_review),
                            8 if quick_preview else 16,
                        ),
                        "max_documents_per_call": min(
                            int(env_budget.max_documents_per_call),
                            3 if quick_preview else 6,
                        ),
                        "max_document_children": min(
                            int(env_budget.max_document_children),
                            8 if quick_preview else 16,
                        ),
                        "max_document_chars": min(
                            int(env_budget.max_document_chars),
                            24000 if quick_preview else 48000,
                        ),
                    }
                )
        investigated = dict(
            run_grow_investigation(
                canonical_source,
                graph,
                brain_revision,
                provider=provider,
                investigation=dict(investigation or {}) or None,
                clarification_answers=dict(clarification_answers or {}),
                question_limit=question_limit,
                correlation_id=correlation_id,
                parent_operation_id=parent_operation_id,
                budget=investigator_budget,
            )
        )
        if _grow_v3_investigator_provider_unavailable(investigated):
            fallback_result = _grow_v3_source_bound_compiler_result(
                investigated=investigated,
                raw_input=raw_input,
                input_mode=input_mode,
                graph=graph,
                index_payload=index_payload,
                atlas_payload=atlas_payload,
                source_label=source_label,
                source_type=source_type,
                source_trust=source_trust,
                learning_mode=learning_mode,
                question_limit=question_limit,
                clarification_answers=clarification_answers,
                source_sections=source_sections,
                source_unit_formation=source_unit_formation,
                source_investigation_id=source_investigation_id,
                source_purpose=source_purpose,
                operator_instruction=operator_instruction,
                source_context=canonical_source,
                compiler_timeout_seconds=compiler_timeout_seconds,
                api_key_override=api_key_override,
                model_override=model_override,
            )
            if fallback_result is not None:
                return fallback_result
        questions = _grow_v3_questions(investigated, limit=question_limit)
        complete = bool(investigated.get("complete"))
        applicable = bool(investigated.get("applicable"))
        if questions or not complete or not applicable:
            return {
                "schema_version": "agvm.grow_engine_result.v3",
                "preview_bundle": None,
                "ai_execution_attestation": dict(investigated.get("ai_execution_attestation") or {}),
                "ai_execution_ledger": list(investigated.get("ai_execution_ledger") or []),
                "clarification_questions": questions,
                "maintenance_feedback": [],
                "investigation": investigated,
                "investigation_session": {
                    "schema_version": "agvm.investigation_session.v3",
                    "status": "awaiting_answers" if questions else "incomplete",
                    "question_count": len(questions),
                    "question_limit": question_limit,
                    "provider_attested": bool(investigated.get("ai_execution_ledger")),
                    "investigation_version": investigated.get("version"),
                },
                "usage": dict(investigated.get("usage") or investigated.get("aggregate_usage") or {}),
                "apply_ready": False,
            }

        compiler_claims = [
            dict(item)
            for item in list(investigated.get("compiler_claims") or [])
            if isinstance(item, Mapping)
        ]
        direct_compiler_claims = _grow_v3_direct_compiler_claims(
            compiler_claims,
            investigation=investigated,
        )
        direct_claim_ids = {
            str(claim.get("claim_id") or "").strip()
            for claim in direct_compiler_claims
            if str(claim.get("claim_id") or "").strip()
        }
        mixed_batch_feedback_claim_ids = {
            str(claim.get("claim_id") or "").strip()
            for claim in compiler_claims
            if str(claim.get("claim_id") or "").strip()
            and str(claim.get("claim_id") or "").strip() not in direct_claim_ids
            and str(claim.get("decision") or "").strip()
            in _MIXED_BATCH_MAINTENANCE_DECISIONS
        }
        maintenance_feedback = _grow_v3_maintenance_feedback_packets(
            investigated,
            include_claim_ids=mixed_batch_feedback_claim_ids,
        )
        if not direct_compiler_claims:
            return {
                "schema_version": "agvm.grow_engine_result.v3",
                "preview_bundle": None,
                "ai_execution_attestation": dict(investigated.get("ai_execution_attestation") or {}),
                "ai_execution_ledger": list(investigated.get("ai_execution_ledger") or []),
                "clarification_questions": [],
                "maintenance_feedback": maintenance_feedback,
                "investigation": investigated,
                "investigation_session": {
                    "schema_version": "agvm.investigation_session.v3",
                    "status": "maintenance_deferred" if maintenance_feedback else "incomplete",
                    "question_count": 0,
                    "question_limit": question_limit,
                    "provider_attested": bool(investigated.get("ai_execution_ledger")),
                    "investigation_version": investigated.get("version"),
                },
                "usage": dict(investigated.get("usage") or investigated.get("aggregate_usage") or {}),
                "apply_ready": False,
            }

        compiler_sections = _grow_v3_compiler_sections(direct_compiler_claims)
        if not compiler_sections:
            raise ValueError("grow_ai_investigator_compiler_claims_required")
        compiler_input = "\n\n".join(
            str(section.get("text") or "").strip()
            for section in compiler_sections
            if str(section.get("text") or "").strip()
        )
        execution_metadata: dict[str, Any] = {}
        resolved_source_context = canonical_source
        resolved_source_context["clarification_answers"] = dict(clarification_answers or {})
        resolved_source_context["grow_investigation"] = investigated
        resolved_source_context["compiler_authority"] = dict(
            investigated.get("compiler_authority") or {}
        )
        bundle = preview_bundle(
            compiler_input or raw_input,
            input_mode,
            graph,
            index_payload,
            atlas_payload,
            source_label=source_label,
            source_type=source_type,
            source_trust=source_trust,
            learning_mode=learning_mode,
            question_limit=question_limit,
            source_sections=compiler_sections,
            source_unit_formation=dict(source_unit_formation or {}),
            source_investigation_id=source_investigation_id,
            source_purpose=source_purpose,
            operator_instruction=operator_instruction,
            source_context=resolved_source_context,
            compiler_timeout_seconds=compiler_timeout_seconds,
            compiler_api_key_override=api_key_override,
            compiler_model_override=model_override,
            compiler_execution_metadata=execution_metadata,
            require_ai=True,
        )
        compiler_attestation = validate_ai_execution_attestation(execution_metadata)
        bound_bundle = _bind_grow_v3_preview_to_claims(dict(bundle), direct_compiler_claims)
        investigated = _append_grow_v3_compiler_execution(
            investigated,
            compiler_attestation=compiler_attestation,
            preview_bundle=bound_bundle,
        )
        dynamic_questions = _grow_v3_dynamic_clarification_question_objects(
            bound_bundle,
            clarification_answers=clarification_answers,
            learning_mode=learning_mode,
            limit=question_limit,
        )
        if dynamic_questions:
            investigated["complete"] = False
            investigated["applicable"] = False
            investigated["questions"] = dynamic_questions
            investigated["pending_questions"] = dynamic_questions
            return {
                "schema_version": "agvm.grow_engine_result.v3",
                "preview_bundle": None,
                "ai_execution_attestation": compiler_attestation,
                "ai_execution_ledger": list(investigated.get("ai_execution_ledger") or []),
                "clarification_questions": dynamic_questions,
                "maintenance_feedback": maintenance_feedback,
                "investigation": investigated,
                "investigation_session": {
                    "schema_version": "agvm.investigation_session.v3",
                    "status": "awaiting_answers",
                    "question_count": len(dynamic_questions),
                    "question_limit": question_limit,
                    "provider_attested": True,
                    "investigation_version": investigated.get("version"),
                },
                "usage": dict(investigated.get("usage") or investigated.get("aggregate_usage") or {}),
                "apply_ready": False,
            }
        return {
            "schema_version": "agvm.grow_engine_result.v3",
            "preview_bundle": bound_bundle,
            "ai_execution_attestation": compiler_attestation,
            "ai_execution_ledger": list(investigated.get("ai_execution_ledger") or []),
            "clarification_questions": [],
            "maintenance_feedback": maintenance_feedback,
            "investigation": investigated,
            "investigation_session": {
                "schema_version": "agvm.investigation_session.v3",
                "status": "sufficient",
                "question_count": 0,
                "question_limit": question_limit,
                "provider_attested": True,
                "investigation_version": investigated.get("version"),
            },
            "usage": dict(investigated.get("usage") or investigated.get("aggregate_usage") or {}),
            "apply_ready": True,
        }


def _grow_v3_questions(investigation: Mapping[str, Any], *, limit: int) -> list[dict[str, Any]]:
    raw_questions = investigation.get("pending_questions")
    if raw_questions is None:
        raw_questions = investigation.get("questions")
    questions = list(raw_questions.values()) if isinstance(raw_questions, Mapping) else list(raw_questions or [])
    # The investigator enforces the configured pending-question budget. Never
    # hide a persisted blocker from the user if a legacy/malformed record is
    # wider than the current display preference.
    return [dict(item) for item in questions if isinstance(item, Mapping)]


def _grow_v3_investigator_provider_unavailable(investigation: Mapping[str, Any]) -> bool:
    failure = investigation.get("failure")
    failure = dict(failure) if isinstance(failure, Mapping) else {}
    return str(failure.get("code") or "").strip() == "provider_unavailable"


def _grow_v3_source_bound_compiler_result(
    *,
    investigated: Mapping[str, Any],
    raw_input: str,
    input_mode: str,
    graph: dict[str, Any],
    index_payload: dict[str, Any],
    atlas_payload: dict[str, Any],
    source_label: str | None,
    source_type: str | None,
    source_trust: str,
    learning_mode: str,
    question_limit: int,
    clarification_answers: Mapping[str, Any] | None,
    source_sections: list[dict[str, Any]] | None,
    source_unit_formation: Mapping[str, Any] | None,
    source_investigation_id: str | None,
    source_purpose: str | None,
    operator_instruction: str | None,
    source_context: Mapping[str, Any],
    compiler_timeout_seconds: float | None,
    api_key_override: str | None,
    model_override: str | None,
) -> dict[str, Any] | None:
    compiler_sections = [
        dict(section)
        for section in list(source_sections or [])
        if isinstance(section, Mapping)
    ]
    compiler_input = "\n\n".join(
        str(
            section.get("text")
            or section.get("raw_text")
            or section.get("summary")
            or ""
        ).strip()
        for section in compiler_sections
        if str(
            section.get("text")
            or section.get("raw_text")
            or section.get("summary")
            or ""
        ).strip()
    ).strip()
    if not compiler_input:
        compiler_input = str(raw_input or "").strip()
    if len(compiler_input) < 40:
        return None
    execution_metadata: dict[str, Any] = {}
    resolved_source_context = dict(source_context or {})
    resolved_source_context["clarification_answers"] = dict(clarification_answers or {})
    resolved_source_context["grow_investigation"] = dict(investigated or {})
    resolved_source_context["compiler_authority"] = dict(
        dict(investigated or {}).get("compiler_authority") or {}
    )
    try:
        bundle = preview_bundle(
            compiler_input,
            input_mode,
            graph,
            index_payload,
            atlas_payload,
            source_label=source_label,
            source_type=source_type,
            source_trust=source_trust,
            learning_mode=learning_mode,
            question_limit=question_limit,
            source_sections=compiler_sections,
            source_unit_formation=dict(source_unit_formation or {}),
            source_investigation_id=source_investigation_id,
            source_purpose=source_purpose,
            operator_instruction=operator_instruction,
            source_context=resolved_source_context,
            compiler_timeout_seconds=compiler_timeout_seconds,
            compiler_api_key_override=api_key_override,
            compiler_model_override=model_override,
            compiler_execution_metadata=execution_metadata,
            require_ai=True,
        )
        compiler_attestation = validate_ai_execution_attestation(execution_metadata)
    except Exception:  # noqa: BLE001 - preserve original provider-unavailable failure
        return None
    updated_investigation = dict(investigated or {})
    recovered_failure = dict(updated_investigation.pop("failure", {}) or {})
    updated_investigation.update(
        {
            "state": "active",
            "status": "COMPLETE",
            "complete": True,
            "applicable": True,
            "investigator_recovery": {
                "recovered": True,
                "original_failure": recovered_failure,
                "authority": "provider_attested_source_bound_compiler",
            },
        }
    )
    updated_investigation = _append_grow_v3_compiler_execution(
        updated_investigation,
        compiler_attestation=compiler_attestation,
        preview_bundle=dict(bundle),
    )
    dynamic_questions = _grow_v3_dynamic_clarification_question_objects(
        bundle,
        clarification_answers=clarification_answers,
        learning_mode=learning_mode,
        limit=question_limit,
    )
    if dynamic_questions:
        updated_investigation["complete"] = False
        updated_investigation["applicable"] = False
        updated_investigation["questions"] = dynamic_questions
        updated_investigation["pending_questions"] = dynamic_questions
        return {
            "schema_version": "agvm.grow_engine_result.v3",
            "preview_bundle": None,
            "ai_execution_attestation": compiler_attestation,
            "ai_execution_ledger": list(updated_investigation.get("ai_execution_ledger") or []),
            "clarification_questions": dynamic_questions,
            "maintenance_feedback": [],
            "investigation": updated_investigation,
            "investigation_session": {
                "schema_version": "agvm.investigation_session.v3",
                "status": "awaiting_answers",
                "question_count": len(dynamic_questions),
                "question_limit": question_limit,
                "provider_attested": True,
                "investigation_version": updated_investigation.get("version"),
            },
            "usage": dict(updated_investigation.get("usage") or {}),
            "apply_ready": False,
        }
    return {
        "schema_version": "agvm.grow_engine_result.v3",
        "preview_bundle": dict(bundle),
        "ai_execution_attestation": compiler_attestation,
        "ai_execution_ledger": list(updated_investigation.get("ai_execution_ledger") or []),
        "clarification_questions": [],
        "maintenance_feedback": [],
        "investigation": updated_investigation,
        "investigation_session": {
            "schema_version": "agvm.investigation_session.v3",
            "status": "sufficient",
            "question_count": 0,
            "question_limit": question_limit,
            "provider_attested": True,
            "investigation_version": updated_investigation.get("version"),
        },
        "usage": dict(updated_investigation.get("usage") or {}),
        "apply_ready": True,
    }


def _grow_v3_dynamic_clarification_question_objects(
    bundle: Mapping[str, Any],
    *,
    clarification_answers: Mapping[str, Any] | None,
    learning_mode: str,
    limit: int,
) -> list[dict[str, Any]]:
    if str(learning_mode or "").strip() != "guided_learning":
        return []
    raw_questions = dynamic_clarification_questions(
        bundle,
        answers=clarification_answers,
        limit=limit,
    )
    questions: list[dict[str, Any]] = []
    for index, raw_question in enumerate(raw_questions, start=1):
        question = " ".join(str(raw_question or "").split()).strip()
        if not question:
            continue
        questions.append(
            {
                "question_id": f"dynamic_clarification_{index}",
                "question": question,
                "reason": "provider_clarification",
                "status": "pending",
            }
        )
    return questions


_DIRECT_GROW_DECISIONS = {
    "new_memory",
    "source_only",
    "evolve_existing",
    "enrich_existing",
    "contradicts_existing",
    "supersedes_existing",
    "delete_existing",
}
_MAINTENANCE_FEEDBACK_DECISIONS = {
    "duplicate",
    "defer",
}
_MIXED_BATCH_MAINTENANCE_DECISIONS = {
    "delete_existing",
    "supersedes_existing",
}
_MAINTENANCE_FEEDBACK_PACKET_DECISIONS = (
    _MAINTENANCE_FEEDBACK_DECISIONS | _MIXED_BATCH_MAINTENANCE_DECISIONS
)
_MAINTENANCE_INTENT_LANES = {"none", "sleep_review", "evolve_review"}


def _grow_v3_direct_compiler_claims(
    compiler_claims: list[dict[str, Any]],
    *,
    investigation: Mapping[str, Any],
) -> list[dict[str, Any]]:
    hydrated = dict(investigation.get("hydrated_evidence") or {})
    decisions = [
        str(claim.get("decision") or "").strip()
        for claim in compiler_claims
        if isinstance(claim, Mapping)
    ]
    has_material_memory_claim = any(
        decision in (_DIRECT_GROW_DECISIONS - _MIXED_BATCH_MAINTENANCE_DECISIONS)
        for decision in decisions
    )
    direct: list[dict[str, Any]] = []
    for claim in compiler_claims:
        decision = str(claim.get("decision") or "").strip()
        targets = [
            str(target_id).strip()
            for target_id in list(claim.get("target_node_ids") or [])
            if str(target_id).strip()
        ]
        if has_material_memory_claim and decision in _MIXED_BATCH_MAINTENANCE_DECISIONS:
            continue
        if decision in {"new_memory", "source_only"}:
            if targets:
                if decision == "new_memory":
                    raise ValueError("grow_ai_investigator_new_memory_target_forbidden")
                raise ValueError("grow_ai_investigator_source_only_target_forbidden")
            direct.append(dict(claim))
        elif decision in {"evolve_existing", "enrich_existing", "contradicts_existing", "supersedes_existing", "delete_existing"}:
            if len(targets) != 1:
                if decision == "evolve_existing":
                    raise ValueError("grow_ai_investigator_evolve_single_target_required")
                raise ValueError("grow_ai_investigator_decision_single_target_required")
            if targets[0] not in hydrated:
                if decision == "evolve_existing":
                    raise ValueError("grow_ai_investigator_evolve_target_not_hydrated")
                raise ValueError("grow_ai_investigator_decision_target_not_hydrated")
            direct.append(dict(claim))
    return direct


def _grow_v3_maintenance_feedback_packets(
    investigation: Mapping[str, Any],
    *,
    include_claim_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    explicitly_included_claim_ids = set(include_claim_ids or set())
    decisions = [
        dict(item)
        for item in list(investigation.get("decisions") or [])
        if isinstance(item, Mapping)
    ]
    if not decisions:
        return []
    claims_by_id = {
        str(item.get("claim_id") or ""): dict(item)
        for item in list(investigation.get("claim_ledger") or [])
        if isinstance(item, Mapping)
    }
    packets: list[dict[str, Any]] = []
    for decision in decisions:
        decision_value = str(decision.get("decision") or "").strip()
        claim_id = str(decision.get("claim_id") or "").strip()
        if (
            decision_value not in _MAINTENANCE_FEEDBACK_DECISIONS
            and claim_id not in explicitly_included_claim_ids
        ):
            continue
        claim = claims_by_id.get(claim_id)
        if claim is None:
            raise ValueError("grow_ai_investigator_feedback_claim_missing")
        packets.append(
            _grow_v3_maintenance_feedback_packet(
                investigation=investigation,
                claim=claim,
                decision=decision,
            )
        )
    return packets


def _grow_v3_maintenance_feedback_packet(
    *,
    investigation: Mapping[str, Any],
    claim: Mapping[str, Any],
    decision: Mapping[str, Any],
) -> dict[str, Any]:
    claim_id = str(claim.get("claim_id") or "").strip()
    decision_id = str(decision.get("decision_id") or "").strip()
    claim_decision = str(decision.get("decision") or "").strip()
    if not claim_id or not decision_id or claim_decision not in _MAINTENANCE_FEEDBACK_PACKET_DECISIONS:
        raise ValueError("grow_ai_investigator_feedback_decision_invalid")
    target_node_ids = _grow_v3_unique_strings(decision.get("target_node_ids"), limit=32)
    if claim_decision != "defer" and not target_node_ids:
        raise ValueError("grow_ai_investigator_feedback_target_required")
    evidence_receipt_ids = _grow_v3_unique_strings(
        decision.get("evidence_receipt_ids"),
        limit=16,
    )
    evidence_refs = _grow_v3_unique_strings(decision.get("evidence_refs"), limit=32)
    if not evidence_receipt_ids or not evidence_refs:
        raise ValueError("grow_ai_investigator_feedback_evidence_required")
    hydrated = dict(investigation.get("hydrated_evidence") or {})
    hydrated_target_digests: list[dict[str, str]] = []
    for target_node_id in target_node_ids:
        material = hydrated.get(target_node_id)
        if not isinstance(material, Mapping):
            raise ValueError("grow_ai_investigator_feedback_target_not_hydrated")
        hydrated_target_digests.append(
            {
                "target_node_id": target_node_id,
                "digest": str(
                    material.get("digest")
                    or _canonical_grow_engine_sha256(dict(material))
                ),
            }
        )

    source_span_sha256 = _canonical_grow_engine_sha256(
        {
            "claim_id": claim_id,
            "source_unit_id": claim.get("source_unit_id"),
            "source_unit_content_sha256": claim.get("source_unit_content_sha256"),
            "basis_kind": claim.get("basis_kind"),
            "basis_ref": claim.get("basis_ref"),
            "basis_content_sha256": claim.get("basis_content_sha256"),
            "quote_start": claim.get("quote_start"),
            "quote_end": claim.get("quote_end"),
            "exact_quote": claim.get("exact_quote"),
        }
    )
    ledger_sha256 = str(investigation.get("ai_execution_ledger_sha256") or "").strip()
    if not ledger_sha256:
        ledger_sha256 = _canonical_grow_engine_sha256(
            [
                str(item.get("entry_sha256") or _canonical_grow_engine_sha256(item))
                for item in list(investigation.get("ai_execution_ledger") or [])
                if isinstance(item, Mapping)
            ]
        )
    source_sha256 = _normalize_grow_v3_sha256_reference(investigation.get("source_sha256"))
    if not source_sha256:
        source_sha256 = _normalize_grow_v3_sha256_reference(
            claim.get("source_unit_content_sha256") or claim.get("basis_content_sha256")
        )
    if not source_sha256:
        raise ValueError("grow_ai_investigator_feedback_source_sha256_required")
    body = {
        "schema_version": "agvm.maintenance_feedback.v3",
        "brain_id": investigation.get("brain_id"),
        "investigation_id": investigation.get("investigation_id"),
        "investigation_version": investigation.get("version"),
        "stage": "deferred",
        "claim_id": claim_id,
        "decision_id": decision_id,
        "claim_decision": claim_decision,
        "brain_revision_before": investigation.get("brain_revision"),
        "brain_revision_after": None,
        "source_sha256": source_sha256,
        "source_span_sha256": source_span_sha256,
        "evidence_receipt_ids": evidence_receipt_ids,
        "evidence_refs": evidence_refs,
        "hydrated_target_digests": hydrated_target_digests,
        "target_node_ids": target_node_ids,
        "persisted_node_ids": [],
        "apply_receipt_sha256": None,
        "ai_execution_ledger_sha256": ledger_sha256,
        "temporal_authority": {
            "authority": "ai_investigator_evidence_bound",
            **_grow_v3_temporal_authority(claim),
        },
        "maintenance_intent": _grow_v3_maintenance_intent(
            decision,
            target_node_ids=target_node_ids,
        ),
        "state": "deferred_to_maintenance",
    }
    feedback_id = "grow-maint-feedback::" + _canonical_grow_engine_sha256(
        {
            "schema_version": body["schema_version"],
            "brain_id": body["brain_id"],
            "investigation_id": body["investigation_id"],
            "investigation_version": body["investigation_version"],
            "stage": body["stage"],
            "claim_id": claim_id,
            "decision_id": decision_id,
            "claim_decision": claim_decision,
            "source_span_sha256": source_span_sha256,
        }
    )[:32]
    packet = {
        **body,
        "feedback_id": feedback_id,
    }
    packet["payload_sha256"] = _canonical_grow_engine_sha256(packet)
    return packet


def _grow_v3_maintenance_intent(
    decision: Mapping[str, Any],
    *,
    target_node_ids: list[str],
) -> dict[str, Any]:
    raw_intent = (
        dict(decision.get("maintenance_intent"))
        if isinstance(decision.get("maintenance_intent"), Mapping)
        else {}
    )
    lane = str(raw_intent.get("lane") or "none").strip()
    if lane not in _MAINTENANCE_INTENT_LANES:
        raise ValueError("grow_ai_investigator_maintenance_intent_lane_invalid")
    if lane in {"sleep_review", "evolve_review"} and not target_node_ids:
        raise ValueError("grow_ai_investigator_maintenance_intent_target_required")
    reason = str(
        raw_intent.get("reason")
        or decision.get("reason")
        or decision.get("rationale")
        or decision.get("comparison")
        or decision.get("brain_evidence_summary")
        or ""
    ).strip()
    expected_decision_change = str(
        raw_intent.get("expected_decision_change")
        or decision.get("impact")
        or decision.get("next_step")
        or decision.get("rationale")
        or decision.get("reason")
        or ""
    ).strip()
    if not reason or not expected_decision_change:
        raise ValueError("grow_ai_investigator_maintenance_intent_required")
    return {
        "lane": lane,
        "reason": reason,
        "expected_decision_change": expected_decision_change,
        "requested_capabilities": _grow_v3_unique_strings(
            raw_intent.get("requested_capabilities"),
            limit=16,
        ),
    }


def _normalize_grow_v3_sha256_reference(value: Any) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    if normalized.startswith("sha256:"):
        return normalized
    if len(normalized) == 64 and all(char in "0123456789abcdefABCDEF" for char in normalized):
        return f"sha256:{normalized.lower()}"
    return normalized


def _grow_v3_unique_strings(value: Any, *, limit: int) -> list[str]:
    result: list[str] = []
    if isinstance(value, (str, bytes)):
        values = [value]
    else:
        values = list(value or [])
    for item in values:
        normalized = str(item or "").strip()
        if normalized and normalized not in result:
            result.append(normalized)
        if len(result) >= limit:
            break
    return result


def _grow_v3_compiler_sections(compiler_claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    for claim in compiler_claims:
        decision = str(claim.get("decision") or "").strip()
        if decision not in _DIRECT_GROW_DECISIONS:
            continue
        claim_id = str(claim.get("claim_id") or "").strip()
        decision_id = str(claim.get("decision_id") or "").strip()
        text = str(claim.get("natural_language_claim") or claim.get("exact_quote") or "").strip()
        if not claim_id or not decision_id or not text:
            raise ValueError("grow_ai_investigator_compiler_claim_invalid")
        sections.append(
            {
                "section_id": claim_id,
                "unit_id": claim_id,
                "title": f"Investigated claim {claim_id}",
                "kind": "investigated_claim",
                "text": text,
                "source_unit_id": claim.get("source_unit_id"),
                "source_span": claim.get("source_span"),
                "source_unit_content_sha256": claim.get("source_unit_content_sha256"),
                "parent_claim_id": claim.get("parent_claim_id"),
                "basis_kind": claim.get("basis_kind"),
                "basis_ref": claim.get("basis_ref"),
                "basis_content_sha256": claim.get("basis_content_sha256"),
                "source_published_at": claim.get("source_published_at"),
                "source_acquired_at": claim.get("source_acquired_at"),
                "source_retrieved_at": claim.get("source_retrieved_at"),
                "source_uri": claim.get("source_uri"),
                "source_trust": claim.get("source_trust"),
                **_grow_v3_temporal_authority(claim),
                "claim_id": claim_id,
                "decision_id": decision_id,
                "decision": decision,
                "target_node_ids": list(claim.get("target_node_ids") or []),
                "fact_eligible": True,
                "promotion_role": "primary_evidence",
            }
        )
    return sections


def _grow_v3_temporal_authority(claim: Mapping[str, Any]) -> dict[str, Any]:
    scope_value = claim.get("temporal_scope")
    scope = dict(scope_value) if isinstance(scope_value, Mapping) else {}
    return {
        "temporal_scope": scope_value,
        "temporal_role": scope.get("temporal_role"),
        "observed_at": scope.get("observed_at"),
        "valid_from": scope.get("valid_from"),
        "valid_to": scope.get("valid_to"),
    }


def _grow_v3_compiler_structural_node(node: Mapping[str, Any]) -> bool:
    document_role = str(node.get("document_role") or "").strip()
    memory_type = str(node.get("memory_type") or "").strip()
    source_bound_role = str(node.get("source_bound_role") or "").strip()
    return bool(
        node.get("is_document_anchor")
        or document_role in {"anchor", "chunk", "summary", "source", "source_unit"}
        or memory_type
        in {
            "document_anchor",
            "document_chunk",
            "document_summary",
            "source_document",
            "source_chunk",
        }
        or source_bound_role in {"rich_section_memory", "source_anchor", "source_unit"}
    )


def _bind_grow_v3_preview_to_claims(
    bundle: dict[str, Any],
    compiler_claims: list[dict[str, Any]],
) -> dict[str, Any]:
    eligible = [
        claim
        for claim in compiler_claims
        if str(claim.get("decision") or "") in _DIRECT_GROW_DECISIONS
    ]
    claims_by_id = {str(claim.get("claim_id") or ""): claim for claim in eligible}
    raw_nodes: list[dict[str, Any]] = []
    primary_node = dict(bundle.get("primary_node_preview") or {})
    if primary_node:
        raw_nodes.append(primary_node)
    raw_nodes.extend(dict(item) for item in list(bundle.get("derived_nodes") or []))
    bound_nodes: list[dict[str, Any]] = []
    unused_claim_ids = list(claims_by_id)
    used_claim_ids: set[str] = set()
    for node in raw_nodes:
        candidate_ids = [
            str(value or "").strip()
            for value in (node.get("claim_id"), node.get("source_unit_id"))
            if str(value or "").strip()
        ]
        candidate_id = next((value for value in candidate_ids if value in claims_by_id), "")
        claim = claims_by_id.get(candidate_id)
        if claim is None:
            if _grow_v3_compiler_structural_node(node):
                structural_node = dict(node)
                structural_node["claim_binding_required"] = False
                structural_node["compiler_binding_role"] = "source_document_structure"
                structural_node.setdefault("cognitive_status", "ready")
                structural_node.setdefault("selected_by_default", True)
                bound_nodes.append(structural_node)
                continue
            raise ValueError("grow_ai_investigator_compiler_claim_binding_missing")
        claim_id = str(claim.get("claim_id") or "")
        if claim_id in used_claim_ids:
            raise ValueError("grow_ai_investigator_compiler_claim_binding_duplicate")
        used_claim_ids.add(claim_id)
        decision = str(claim.get("decision") or "").strip()
        target_node_ids = [
            str(target_id).strip()
            for target_id in list(claim.get("target_node_ids") or [])
            if str(target_id).strip()
        ]
        action = _grow_v3_materialized_action(decision, target_node_ids)
        temporal_authority = _grow_v3_temporal_authority(claim)
        compiler_provenance = dict(node.get("provenance")) if isinstance(node.get("provenance"), Mapping) else {}
        investigator_provenance = {
            **compiler_provenance,
            "grow_parent_claim_id": claim.get("parent_claim_id"),
            "grow_basis_kind": claim.get("basis_kind"),
            "grow_basis_ref": claim.get("basis_ref"),
            "grow_basis_content_sha256": claim.get("basis_content_sha256"),
            "source_published_at": claim.get("source_published_at"),
            "source_acquired_at": claim.get("source_acquired_at"),
            "source_retrieved_at": claim.get("source_retrieved_at"),
            "source_uri": claim.get("source_uri"),
            "source_trust": claim.get("source_trust"),
            "grow_temporal_scope": temporal_authority["temporal_scope"],
            "grow_temporal_authority": "ai_investigator_evidence_bound",
        }
        if claim_id in unused_claim_ids:
            unused_claim_ids.remove(claim_id)
        bound_nodes.append(
            {
                **node,
                "claim_id": claim_id,
                "decision_id": str(claim.get("decision_id") or ""),
                "claim_decision": decision,
                "target_node_ids": target_node_ids,
                "persist_mode": "create",
                "merge_target_node_id": None,
                "memory_act_type": action["memory_act_type"],
                "cognitive_status": "review_required" if action["high_impact"] else "ready",
                "requires_human_review": bool(action["high_impact"]),
                "selected_by_default": (
                    False
                    if action["high_impact"]
                    else bool(node.get("selected_by_default", True))
                ),
                "cognitive_review_reasons": (
                    ["ai_investigator_high_impact_decision"] if action["high_impact"] else []
                ),
                "cognitive_target_node_ids": target_node_ids,
                "obsoletes": [],
                "investigated_source_unit_id": claim.get("source_unit_id"),
                "source_unit_content_sha256": claim.get("source_unit_content_sha256"),
                "parent_claim_id": claim.get("parent_claim_id"),
                "basis_kind": claim.get("basis_kind"),
                "basis_ref": claim.get("basis_ref"),
                "basis_content_sha256": claim.get("basis_content_sha256"),
                "source_published_at": claim.get("source_published_at"),
                "source_acquired_at": claim.get("source_acquired_at"),
                "source_retrieved_at": claim.get("source_retrieved_at"),
                "source_uri": claim.get("source_uri"),
                "source_trust": claim.get("source_trust"),
                **temporal_authority,
                "provenance": investigator_provenance,
            }
        )
    if unused_claim_ids:
        raise ValueError("grow_ai_investigator_compiler_claim_binding_incomplete")
    claim_bound_nodes = [
        node
        for node in bound_nodes
        if str(node.get("claim_decision") or "") in _DIRECT_GROW_DECISIONS
    ]
    bound = dict(bundle)
    bound["primary_node_preview"] = bound_nodes[0]
    bound["derived_nodes"] = bound_nodes[1:]
    bound["investigation_authority"] = {
        "schema_version": "agvm.grow_compiler_authority.v1",
        "structural_node_count": len(bound_nodes) - len(claim_bound_nodes),
        "claim_decision_bindings": [
            {
                "preview_id": str(node.get("id") or ""),
                "claim_id": str(node.get("claim_id") or ""),
                "decision_id": str(node.get("decision_id") or ""),
                "claim_decision": str(node.get("claim_decision") or ""),
                "target_node_ids": list(node.get("target_node_ids") or []),
                "parent_claim_id": node.get("parent_claim_id"),
                "basis_kind": node.get("basis_kind"),
                "basis_ref": node.get("basis_ref"),
                "basis_content_sha256": node.get("basis_content_sha256"),
                "source_published_at": node.get("source_published_at"),
                "source_acquired_at": node.get("source_acquired_at"),
                "source_retrieved_at": node.get("source_retrieved_at"),
                "temporal_scope": node.get("temporal_scope"),
                "temporal_role": node.get("temporal_role"),
                "observed_at": node.get("observed_at"),
                "valid_from": node.get("valid_from"),
                "valid_to": node.get("valid_to"),
                "materialized_action": _grow_v3_materialized_action(
                    str(node.get("claim_decision") or ""),
                    list(node.get("target_node_ids") or []),
                ),
            }
            for node in claim_bound_nodes
        ],
    }
    bound["cognitive_write_plan"] = _grow_v3_cognitive_write_plan(bound_nodes)
    return bound


def _grow_v3_materialized_action(
    decision: str,
    target_node_ids: list[str],
) -> dict[str, Any]:
    normalized_decision = str(decision or "").strip()
    targets = [str(target_id).strip() for target_id in target_node_ids if str(target_id).strip()]
    action_by_decision = {
        "new_memory": ("create_new_fact", None, False),
        "source_only": ("create_new_fact", None, False),
        "enrich_existing": ("update_existing_fact", "enriches", False),
        "evolve_existing": ("evolve_existing_fact", None, True),
        "contradicts_existing": ("mark_contradiction", "contradicts", True),
        "supersedes_existing": ("supersede_old_memory", "supersedes", True),
        "delete_existing": ("delete_existing", None, True),
    }
    action = action_by_decision.get(normalized_decision)
    if action is None:
        raise ValueError("grow_ai_investigator_compiler_decision_invalid")
    memory_act_type, relation_type, high_impact = action
    if normalized_decision in {"new_memory", "source_only"} and targets:
        if normalized_decision == "new_memory":
            raise ValueError("grow_ai_investigator_new_memory_target_forbidden")
        raise ValueError("grow_ai_investigator_source_only_target_forbidden")
    if normalized_decision not in {"new_memory", "source_only"} and not targets:
        raise ValueError("grow_ai_investigator_decision_target_required")
    if normalized_decision == "evolve_existing" and len(targets) != 1:
        raise ValueError("grow_ai_investigator_evolve_single_target_required")
    if normalized_decision in {"enrich_existing", "contradicts_existing", "supersedes_existing", "delete_existing"} and len(targets) != 1:
        raise ValueError("grow_ai_investigator_decision_single_target_required")
    return {
        "persist_mode": "create",
        "memory_act_type": memory_act_type,
        "relation_type": relation_type,
        "high_impact": high_impact,
    }


def _grow_v3_cognitive_write_plan(bound_nodes: list[dict[str, Any]]) -> dict[str, Any]:
    memory_acts: list[dict[str, Any]] = []
    state_transitions: list[dict[str, Any]] = []
    contradiction_checks: list[dict[str, Any]] = []
    target_mutation_checks: list[dict[str, Any]] = []
    node_annotations: dict[str, dict[str, Any]] = {}
    structural_node_count = 0
    for node in bound_nodes:
        preview_id = str(node.get("id") or "").strip()
        decision = str(node.get("claim_decision") or "").strip()
        source_structure_binding = bool(
            str(node.get("compiler_binding_role") or "") == "source_document_structure"
            and node.get("claim_binding_required") is False
        )
        if decision not in _DIRECT_GROW_DECISIONS or source_structure_binding:
            structural_node_count += 1
            if preview_id:
                node_annotations[preview_id] = {
                    "cognitive_status": str(node.get("cognitive_status") or "ready"),
                    "requires_human_review": bool(node.get("requires_human_review", False)),
                    "compiler_binding_role": str(
                        node.get("compiler_binding_role") or "source_document_structure"
                    ),
                }
            continue
        target_node_ids = [str(item) for item in list(node.get("target_node_ids") or [])]
        action = _grow_v3_materialized_action(decision, target_node_ids)
        act = {
            "preview_id": preview_id,
            "claim_id": str(node.get("claim_id") or ""),
            "decision_id": str(node.get("decision_id") or ""),
            "claim_decision": decision,
            "act_type": action["memory_act_type"],
            "persist_mode": action["persist_mode"],
            "target_node_ids": target_node_ids,
            "requires_human_review": bool(action["high_impact"]),
            "selected_by_default": bool(node.get("selected_by_default", True)),
            "authority": "ai_investigator_decision_ledger",
        }
        memory_acts.append(act)
        node_annotations[preview_id] = {
            "memory_act_type": action["memory_act_type"],
            "cognitive_status": "review_required" if action["high_impact"] else "ready",
            "requires_human_review": bool(action["high_impact"]),
            "cognitive_review_reasons": (
                ["ai_investigator_high_impact_decision"] if action["high_impact"] else []
            ),
            "cognitive_target_node_ids": target_node_ids,
        }
        if action["relation_type"]:
            state_transitions.append(
                {
                    "preview_id": preview_id,
                    "transition_type": action["memory_act_type"],
                    "target_node_ids": target_node_ids,
                    "relation_type": action["relation_type"],
                    "authority": "ai_investigator_decision_ledger",
                }
            )
        if decision == "evolve_existing":
            target_mutation_checks.append(
                {
                    "preview_id": preview_id,
                    "decision": decision,
                    "target_node_ids": target_node_ids,
                    "requires_explicit_selection": True,
                    "history_policy": "preserve_prior_snapshot_in_target_provenance",
                }
            )
    high_impact_count = sum(
        1
        for item in memory_acts
        if item.get("requires_human_review")
    )
    return {
        "version": "agvm.grow_cognitive_write_plan.v3",
        "authority": "ai_investigator_decision_ledger",
        "memory_acts": memory_acts,
        "state_transitions": state_transitions,
        "contradiction_checks": contradiction_checks,
        "target_mutation_checks": target_mutation_checks,
        "node_annotations": node_annotations,
        "human_review": {
            "required": bool(high_impact_count),
            "review_required_count": high_impact_count,
            "review_reasons": (
                ["ai_investigator_high_impact_decision"] if high_impact_count else []
            ),
            "clarification_questions": [],
        },
        "mutation_plan": {
            "default_policy": "explicit_apply_required",
            "create_count": len(memory_acts) + structural_node_count,
            "merge_into_existing_count": 0,
            "attach_as_alias_or_variant_count": 0,
            "high_impact_count": high_impact_count,
        },
        "summary": {
            "memory_act_count": len(memory_acts),
            "structural_node_count": structural_node_count,
            "review_required_count": high_impact_count,
            "state_transition_count": len(state_transitions),
        },
    }


def _append_grow_v3_compiler_execution(
    investigation: dict[str, Any],
    *,
    compiler_attestation: dict[str, Any],
    preview_bundle: dict[str, Any],
) -> dict[str, Any]:
    try:
        from .investigative_agent import aggregate_execution_ledger, execution_ledger_entry
    except ImportError:  # pragma: no cover - local runtime exposes agvm_api on PYTHONPATH
        from investigative_agent import aggregate_execution_ledger, execution_ledger_entry

    updated = dict(investigation)
    ledger = [dict(item) for item in list(updated.get("ai_execution_ledger") or [])]
    ledger.append(
        execution_ledger_entry(
            role="compiler",
            call_id=f"compiler::{updated.get('investigation_id')}",
            attestation=compiler_attestation,
            brain_revision=str(updated.get("brain_revision") or ""),
            parent_operation_id=str(updated.get("parent_operation_id") or ""),
            child_call_id=f"compiler::{updated.get('investigation_id')}",
            billing_scope="parent_grow_preview",
            idempotency_key=f"compiler::{updated.get('investigation_id')}::{updated.get('version')}",
        )
    )
    updated["ai_execution_ledger"] = ledger
    updated["compiler_preview_sha256"] = _canonical_grow_engine_sha256(preview_bundle)
    aggregate = aggregate_execution_ledger(ledger, complete=True, applicable=True)
    updated["ai_execution_attestation"] = aggregate
    updated["ai_execution_ledger_sha256"] = str(aggregate.get("ledger_sha256") or "")
    updated["usage"] = dict(aggregate.get("usage") or {})
    return updated


def _canonical_grow_engine_sha256(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = ["GrowEngine"]
