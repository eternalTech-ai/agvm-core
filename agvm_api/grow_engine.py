# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

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
    ) -> dict[str, Any]:
        require_runtime_feature(GROW_V2_FLAG, disabled_code="agvm_grow_v2_disabled")
        execution_metadata: dict[str, Any] = {}
        answers = dict(clarification_answers or {})
        resolved_source_context = dict(source_context or {})
        resolved_source_context["clarification_answers"] = answers
        resolved_source_request = dict(resolved_source_context.get("source_request") or {})
        resolved_source_request.update(
            {
                "source_uri": source_uri or resolved_source_request.get("source_uri"),
                "source_ref_id": resolved_source_request.get("source_ref_id")
                or (f"source_ref::{source_investigation_id}" if source_investigation_id else None),
            }
        )
        resolved_source_context["source_request"] = resolved_source_request
        bundle = preview_bundle(
            raw_input,
            input_mode,
            graph,
            index_payload,
            atlas_payload,
            source_label=source_label,
            source_type=source_type,
            source_trust=source_trust,
            learning_mode=learning_mode,
            question_limit=question_limit,
            source_sections=list(source_sections or []),
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
        attestation = validate_ai_execution_attestation(execution_metadata)
        questions = dynamic_clarification_questions(
            bundle,
            answers=answers,
            limit=question_limit,
        )
        return {
            "schema_version": "agvm.grow_engine_result.v2",
            "preview_bundle": bundle,
            "ai_execution_attestation": attestation,
            "clarification_questions": questions,
            "investigation_session": {
                "schema_version": "agvm.investigation_session.v2",
                "status": "awaiting_answers" if questions else "sufficient",
                "question_count": len(questions),
                "question_limit": question_limit,
                "provider_attested": True,
            },
            "apply_ready": bool(not questions),
        }


__all__ = ["GrowEngine"]
