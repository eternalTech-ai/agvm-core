# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from contextvars import copy_context
from dataclasses import dataclass
from threading import Lock
from typing import Any, Callable, Mapping, Sequence

try:
    from .grow_source_authority import grow_source_sha256, grow_source_temporal_provenance
    from .investigative_agent import (
        InvestigativeAgentBudget,
        _call_provider_with_hard_timeout,
        aggregate_execution_ledger,
        execution_ledger_entry,
        normalize_provider_result,
        run_investigative_agent,
        stable_digest,
        utc_now,
        validate_provider_call_attestation,
    )
    from .investigative_search import (
        hydrate_investigative_document_evidence,
        run_investigative_search,
    )
    from .stream_contract import search_mode_budget_seconds
except ImportError:  # pragma: no cover - direct API runtime
    from grow_source_authority import grow_source_sha256, grow_source_temporal_provenance
    from investigative_agent import (
        InvestigativeAgentBudget,
        _call_provider_with_hard_timeout,
        aggregate_execution_ledger,
        execution_ledger_entry,
        normalize_provider_result,
        run_investigative_agent,
        stable_digest,
        utc_now,
        validate_provider_call_attestation,
    )
    from investigative_search import (
        hydrate_investigative_document_evidence,
        run_investigative_search,
    )
    from stream_contract import search_mode_budget_seconds


GROW_INVESTIGATION_SCHEMA_VERSION = "agvm.grow_investigation.v3"
GROW_CLAIM_LEDGER_SCHEMA_VERSION = "agvm.grow_claim_ledger.v1"
GROW_INVESTIGATOR_TURN_SCHEMA_VERSION = "agvm.grow_investigator_turn.v1"
GROW_CLAIM_DECISION_SCHEMA_VERSION = "agvm.grow_claim_decision.v1"
GROW_CLARIFICATION_SET_SCHEMA_VERSION = "agvm.grow_clarification_set.v1"
GROW_TEMPORAL_SCOPE_SCHEMA_VERSION = "agvm.grow_temporal_scope.v1"
INITIAL_TEMPORAL_BINDING_ERROR_PREFIXES = (
    "temporal_mention_",
    "temporal_mentions_",
    "temporal_scope_",
)
GROW_QUERY_AUTHORITY = "server_bound_exact_spans_unified_search"
GROW_GRAPH_SNAPSHOT_VOLATILE_META_KEYS = {"graph_updated_at"}

DECISION_VALUES = {
    "new_memory",
    "duplicate",
    "enrich_existing",
    "evolve_existing",
    "contradicts_existing",
    "supersedes_existing",
    "delete_existing",
    "source_only",
    "defer",
}
TARGET_DECISIONS = {
    "duplicate",
    "enrich_existing",
    "evolve_existing",
    "contradicts_existing",
    "supersedes_existing",
    "delete_existing",
}
QUESTION_DECISION_EFFECTS = {
    "identity",
    "truth",
    "scope",
    "merge",
    "supersede",
    "derivation",
    "utility",
}


Provider = Callable[[dict[str, Any]], Any]
InvestigativeSearchRunner = Callable[..., Mapping[str, Any]]
HydrateRunner = Callable[[list[str], Mapping[str, Any]], Any]
DocumentHydrateRunner = Callable[..., Mapping[str, Any]]


@dataclass(frozen=True)
class _GrowDeadline:
    """One authoritative monotonic deadline for the complete Grow run."""

    started_monotonic: float
    deadline_monotonic: float
    provider_timeout_seconds: float
    monotonic_clock: Callable[[], float]
    cancellation_check: Callable[[str], None] | None = None

    def remaining_seconds(self) -> float:
        return float(self.deadline_monotonic) - float(self.monotonic_clock())

    def require(
        self,
        stage: str,
        *,
        reserve_seconds: float = 0.0,
        cap_seconds: float | None = None,
    ) -> float:
        if self.cancellation_check is not None:
            self.cancellation_check(stage)
        remaining = self.remaining_seconds()
        reserve = max(0.0, float(reserve_seconds))
        available = remaining - reserve
        if available <= 0.0:
            raise RuntimeError(
                f"grow_wall_budget_exhausted:stage={stage};"
                f"remaining_seconds={remaining:.6f};reserve_seconds={reserve:.6f}"
            )
        cap = (
            max(0.001, float(cap_seconds))
            if cap_seconds is not None
            else max(0.001, float(self.provider_timeout_seconds))
        )
        return min(cap, available)


@dataclass
class _GrowSearchBatchGeneration:
    """Cancellation fence for one concurrently executed Search wave."""

    generation: int
    brain_revision: str
    deadline_at_ms: int
    cancelled: bool = False
    cancellation_detail: str | None = None


def _deadline_error(value: Any) -> bool:
    return "grow_wall_budget_exhausted:stage=" in str(value or "")


def _child_search_budget_cap_seconds(retrieval_mode: str) -> float:
    return max(1.0, float(search_mode_budget_seconds(retrieval_mode)))


def _child_search_mode_for_budget(budget: "GrowInvestigationBudget") -> str:
    """Choose the child Search mode that still leaves Grow time to decide.

    Grow preview is an ingestion preflight: it needs enough brain context to
    detect duplicates/conflicts, but it must not spend the whole preview window
    waiting for a balanced Search finalization.  Longer/background Grow runs keep
    the richer balanced path.
    """

    wall_seconds = int(getattr(budget, "wall_budget_seconds", 0) or 0)
    if wall_seconds and wall_seconds <= 90:
        return "flash"
    return "balanced"


@dataclass(frozen=True)
class GrowInvestigationBudget:
    max_turns: int = 3
    max_repairs: int = 2
    max_search_calls: int = 3
    max_tool_calls: int = 8
    max_claims: int = 128
    max_evidence_references: int = 384
    max_notebook_chars: int = 18_000
    search_concurrency: int = 2
    wall_budget_seconds: int = 420
    provider_timeout_seconds: int = 45
    ai_review_reserve_seconds: int = 90
    max_hydration_nodes_per_review: int = 24
    max_documents_per_call: int = 8
    max_document_children: int = 24
    max_document_chars: int = 64_000

    @classmethod
    def from_env(cls) -> "GrowInvestigationBudget":
        def bounded(name: str, default: int, minimum: int, maximum: int) -> int:
            try:
                value = int(os.getenv(name, str(default)))
            except (TypeError, ValueError):
                value = default
            return max(minimum, min(maximum, value))

        return cls(
            max_turns=bounded("AGVM_GROW_AI_INVESTIGATOR_MAX_TURNS", 3, 1, 4),
            max_repairs=bounded("AGVM_GROW_AI_INVESTIGATOR_MAX_REPAIRS", 2, 0, 2),
            max_search_calls=bounded("AGVM_GROW_AI_INVESTIGATOR_MAX_SEARCH_CALLS", 3, 1, 3),
            max_tool_calls=bounded("AGVM_GROW_AI_INVESTIGATOR_MAX_TOOL_CALLS", 8, 1, 12),
            max_claims=bounded("AGVM_GROW_AI_INVESTIGATOR_MAX_CLAIMS", 128, 1, 512),
            max_evidence_references=bounded("AGVM_GROW_AI_INVESTIGATOR_MAX_EVIDENCE_REFS", 384, 1, 1024),
            max_notebook_chars=bounded("AGVM_GROW_AI_INVESTIGATOR_NOTEBOOK_CHARS", 18_000, 4_000, 64_000),
            search_concurrency=bounded("AGVM_GROW_AI_INVESTIGATOR_SEARCH_CONCURRENCY", 2, 1, 2),
            wall_budget_seconds=bounded("AGVM_GROW_AI_INVESTIGATOR_WALL_SECONDS", 420, 10, 600),
            provider_timeout_seconds=bounded("AGVM_GROW_AI_INVESTIGATOR_TIMEOUT_SECONDS", 45, 1, 60),
            ai_review_reserve_seconds=bounded("AGVM_GROW_AI_REVIEW_RESERVE_SECONDS", 90, 90, 180),
            max_hydration_nodes_per_review=bounded("AGVM_GROW_AI_HYDRATION_NODES_PER_REVIEW", 24, 1, 64),
            max_documents_per_call=bounded("AGVM_GROW_AI_MAX_DOCUMENTS_PER_CALL", 8, 1, 32),
            max_document_children=bounded("AGVM_GROW_AI_MAX_DOCUMENT_CHILDREN", 24, 1, 96),
            max_document_chars=bounded("AGVM_GROW_AI_MAX_DOCUMENT_CHARS", 64_000, 1_000, 1_000_000),
        )

    def as_dict(self) -> dict[str, int]:
        return {key: int(value) for key, value in self.__dict__.items()}


_TEMPORAL_MENTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "basis_kind": {
            "type": "string",
            "enum": ["source_span", "clarified_answer", "hydrated_brain_evidence"],
        },
        "basis_ref": {"type": "string"},
        "span_start": {"type": "integer", "minimum": 0},
        "span_end": {"type": "integer", "minimum": 1},
        "exact_text": {"type": "string"},
        "supports_fields": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": ["summary", "temporal_role", "observed_at", "valid_from", "valid_to"],
            },
            "minItems": 1,
            "maxItems": 5,
        },
    },
    "required": [
        "basis_kind",
        "basis_ref",
        "span_start",
        "span_end",
        "exact_text",
        "supports_fields",
    ],
}

_TEMPORAL_SCOPE_SCHEMA = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "properties": {
        "schema_version": {"type": "string", "enum": [GROW_TEMPORAL_SCOPE_SCHEMA_VERSION]},
        "summary": {"type": ["string", "null"]},
        "temporal_role": {"type": ["string", "null"]},
        "observed_at": {"type": ["string", "null"]},
        "valid_from": {"type": ["string", "null"]},
        "valid_to": {"type": ["string", "null"]},
        "temporal_mentions": {
            "type": "array",
            "items": _TEMPORAL_MENTION_SCHEMA,
            "maxItems": 24,
        },
    },
    "required": [
        "schema_version",
        "summary",
        "temporal_role",
        "observed_at",
        "valid_from",
        "valid_to",
        "temporal_mentions",
    ],
}

_CLAIM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "claim_id": {"type": ["string", "null"]},
        "claim_key": {"type": "string"},
        "source_unit_id": {"type": "string"},
        "source_unit_content_sha256": {"type": ["string", "null"]},
        "quote_start": {"type": "integer", "minimum": 0},
        "quote_end": {"type": "integer", "minimum": 1},
        "exact_quote": {"type": "string"},
        "subject_anchor_start": {"type": "integer", "minimum": 0},
        "subject_anchor_end": {"type": "integer", "minimum": 1},
        "neutral_subject_anchor": {"type": "string"},
        "natural_language_claim": {"type": "string"},
        "subject_hypotheses": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        "temporal_scope": _TEMPORAL_SCOPE_SCHEMA,
        "epistemic_posture": {
            "type": "string",
            "enum": ["asserted_fact", "reported_claim", "opinion", "hypothesis", "instruction"],
        },
        "investigation_need": {"type": "string"},
    },
    "required": [
        "claim_key",
        "source_unit_id",
        "quote_start",
        "quote_end",
        "exact_quote",
        "subject_anchor_start",
        "subject_anchor_end",
        "neutral_subject_anchor",
        "natural_language_claim",
        "subject_hypotheses",
        "temporal_scope",
        "epistemic_posture",
        "investigation_need",
    ],
}

_CLARIFICATION_CLAIM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "claim_key": {"type": "string"},
        "parent_claim_id": {"type": "string"},
        "basis_kind": {"type": "string", "enum": ["clarified_answer"]},
        "basis_ref": {"type": "string"},
        "basis_content_sha256": {"type": ["string", "null"]},
        "quote_start": {"type": "integer", "minimum": 0},
        "quote_end": {"type": "integer", "minimum": 1},
        "exact_quote": {"type": "string"},
        "subject_anchor_start": {"type": "integer", "minimum": 0},
        "subject_anchor_end": {"type": "integer", "minimum": 1},
        "neutral_subject_anchor": {"type": "string"},
        "natural_language_claim": {"type": "string"},
        "subject_hypotheses": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        "temporal_scope": _TEMPORAL_SCOPE_SCHEMA,
        "epistemic_posture": {
            "type": "string",
            "enum": ["asserted_fact", "reported_claim", "opinion", "hypothesis", "instruction"],
        },
        "investigation_need": {"type": "string"},
    },
    "required": [
        "claim_key", "parent_claim_id", "basis_kind", "basis_ref", "basis_content_sha256",
        "quote_start", "quote_end", "exact_quote", "subject_anchor_start", "subject_anchor_end",
        "neutral_subject_anchor", "natural_language_claim", "subject_hypotheses", "temporal_scope",
        "epistemic_posture", "investigation_need",
    ],
}

_SEARCH_QUERY_STRATEGY_SCHEMA = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "properties": {
        "query_mode": {
            "type": "string",
            "enum": [
                "open_discovery",
                "identity_scope_discovery",
                "alternative_test",
                "evidence_followup",
            ],
        },
        "independent_information_need": {"type": "string", "minLength": 1},
        "neutral_goal": {"type": "string", "minLength": 1},
        "discovery_dimensions": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
            "maxItems": 12,
        },
        "plausible_alternatives": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 0,
            "maxItems": 12,
        },
        "temporal_focus": {"type": "string", "minLength": 1},
        "counterfactual_safety_summary": {"type": "string", "minLength": 1},
    },
    "required": [
        "query_mode",
        "independent_information_need",
        "neutral_goal",
        "discovery_dimensions",
        "plausible_alternatives",
        "temporal_focus",
        "counterfactual_safety_summary",
    ],
}

_SEARCH_TOOL_ARGUMENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "affected_claim_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 128},
        "query_text": {"type": "string", "minLength": 1},
        "purpose": {"type": "string", "minLength": 1},
        "query_strategy": _SEARCH_QUERY_STRATEGY_SCHEMA,
        "retrieval_mode": {
            "type": ["string", "null"],
            "enum": ["balanced", None],
        },
        "max_matches": {"type": ["integer", "null"], "minimum": 1, "maximum": 24},
        "node_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 96},
    },
    "required": [
        "affected_claim_ids",
        "query_text",
        "purpose",
        "query_strategy",
        "retrieval_mode",
        "max_matches",
        "node_ids",
    ],
}

_HYDRATE_TOOL_ARGUMENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "affected_claim_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 128},
        "query_text": {"type": "null"},
        "purpose": {"type": "string", "minLength": 1},
        "query_strategy": {"type": "null"},
        "retrieval_mode": {"type": "null"},
        "max_matches": {"type": "null"},
        "node_ids": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 96,
        },
    },
    "required": [
        "affected_claim_ids",
        "query_text",
        "purpose",
        "query_strategy",
        "retrieval_mode",
        "max_matches",
        "node_ids",
    ],
}


def _tool_call_schema(tool_name: str, arguments_schema: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "call_id": {"type": "string"},
            "tool_name": {"type": "string", "enum": [tool_name]},
            "arguments": arguments_schema,
        },
        "required": ["call_id", "tool_name", "arguments"],
    }


_TOOL_CALL_SCHEMA = {
    "anyOf": [
        _tool_call_schema("search_brain", _SEARCH_TOOL_ARGUMENT_SCHEMA),
        _tool_call_schema("hydrate_memory_objects", _HYDRATE_TOOL_ARGUMENT_SCHEMA),
        _tool_call_schema("hydrate_document_evidence", _HYDRATE_TOOL_ARGUMENT_SCHEMA),
    ],
}

_QUESTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "question_id": {"type": "string"},
        "affected_claim_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "question_text": {"type": "string", "minLength": 1},
        "source_claim_summary": {"type": "string"},
        "brain_evidence_summary": {"type": "string"},
        "comparison": {"type": "string"},
        "reason": {"type": "string"},
        "impact": {"type": "string"},
        "next_step": {"type": "string"},
        "evidence_refs": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 32},
        "answer_type": {"type": "string", "enum": ["free_text", "single_choice", "multiple_choice", "boolean"]},
        "choices": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        "required_for_preview": {"type": "boolean"},
        "decision_effect": {"type": "string", "enum": sorted(QUESTION_DECISION_EFFECTS)},
    },
    "required": [
        "question_id",
        "affected_claim_ids",
        "question_text",
        "source_claim_summary",
        "brain_evidence_summary",
        "comparison",
        "reason",
        "impact",
        "next_step",
        "evidence_refs",
        "answer_type",
        "choices",
        "required_for_preview",
        "decision_effect",
    ],
}

_CLOSED_CLARIFICATION_GAP_VERIFIER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "schema_version": {
            "type": "string",
            "enum": ["agvm.grow_closed_clarification_gap_verifier.v1"],
        },
        "reopens_closed_gap": {"type": "boolean"},
        "closed_question_id": {"type": ["string", "null"]},
        "closed_answer_is_negative_evidence": {"type": "boolean"},
        "rationale": {"type": "string"},
    },
    "required": [
        "schema_version",
        "reopens_closed_gap",
        "closed_question_id",
        "closed_answer_is_negative_evidence",
        "rationale",
    ],
}

_ISOLATED_QUERY_COMPOSER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "schema_version": {
            "type": "string",
            "enum": ["agvm.grow_isolated_search_query.v1"],
        },
        "query_text": {"type": "string", "minLength": 1},
        "purpose": {"type": "string", "minLength": 1},
        "open_discovery_summary": {"type": "string", "minLength": 1},
        "approved": {"type": "boolean"},
        "decision_summary": {"type": "string", "minLength": 1},
    },
    "required": [
        "schema_version",
        "query_text",
        "purpose",
        "open_discovery_summary",
        "approved",
        "decision_summary",
    ],
}

_DECISION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "claim_id": {"type": "string"},
        "decision": {"type": "string", "enum": sorted(DECISION_VALUES)},
        "target_node_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 32},
        "evidence_receipt_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 16},
        "compatible_but_distinct": {"type": "boolean"},
        "rationale": {"type": "string"},
        "source_claim_summary": {"type": "string"},
        "brain_evidence_summary": {"type": "string"},
        "comparison": {"type": "string"},
        "reason": {"type": "string"},
        "impact": {"type": "string"},
        "next_step": {"type": "string"},
        "evidence_refs": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 32},
        "no_clarification_can_change_decision": {"type": "boolean"},
        "no_clarification_explanation": {"type": "string"},
    },
    "required": [
        "claim_id",
        "decision",
        "target_node_ids",
        "evidence_receipt_ids",
        "compatible_but_distinct",
        "rationale",
        "source_claim_summary",
        "brain_evidence_summary",
        "comparison",
        "reason",
        "impact",
        "next_step",
        "evidence_refs",
    ],
}

GROW_INVESTIGATOR_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "schema_version": {"type": "string", "enum": [GROW_INVESTIGATOR_TURN_SCHEMA_VERSION]},
        "status": {
            "type": "string",
            "enum": ["continue", "needs_clarification", "complete"],
            "description": (
                "Choose one turn action category. Use continue only with tool_calls. "
                "Use needs_clarification only with questions and tool_calls=[]. "
                "Use complete only with decisions and tool_calls=[]."
            ),
        },
        "claims": {"type": "array", "items": _CLAIM_SCHEMA, "maxItems": 128},
        "answer_claims": {"type": "array", "items": _CLARIFICATION_CLAIM_SCHEMA, "maxItems": 128},
        "exclusions": {
            "type": "array",
            "maxItems": 128,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "source_unit_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["source_unit_id", "reason"],
            },
        },
        "tool_calls": {
            "type": "array",
            "items": _TOOL_CALL_SCHEMA,
            "maxItems": 12,
            "description": (
                "Non-empty only when status is continue. If status is needs_clarification "
                "or complete, this array must be empty."
            ),
        },
        "questions": {
            "type": "array",
            "items": _QUESTION_SCHEMA,
            "maxItems": 24,
            "description": (
                "Non-empty only when status is needs_clarification. Do not combine "
                "questions with tool_calls or decisions."
            ),
        },
        "decisions": {
            "type": "array",
            "items": _DECISION_SCHEMA,
            "maxItems": 128,
            "description": (
                "Non-empty only when status is complete. Do not combine decisions "
                "with tool_calls or questions."
            ),
        },
        "summary": {"type": ["string", "null"]},
    },
    "required": [
        "schema_version",
        "status",
        "claims",
        "answer_claims",
        "exclusions",
        "tool_calls",
        "questions",
        "decisions",
        "summary",
    ],
}


def _decision_receipt_question_repair_schema() -> dict[str, Any]:
    """Narrow a question-only receipt repair to one unambiguous terminal action."""

    schema = json.loads(json.dumps(GROW_INVESTIGATOR_RESPONSE_SCHEMA))
    properties = _dict(schema.get("properties"))
    properties["status"] = {
        **_dict(properties.get("status")),
        "enum": ["needs_clarification"],
    }
    for field in ("claims", "answer_claims", "exclusions", "tool_calls", "decisions"):
        properties[field] = {**_dict(properties.get(field)), "maxItems": 0}
    properties["questions"] = {
        **_dict(properties.get("questions")),
        "minItems": 1,
    }
    schema["properties"] = properties
    return schema


def _question_only_receipt_action_repair_schema() -> dict[str, Any]:
    """Narrow question-only receipt repair to clarification or attested defer."""

    schema = json.loads(json.dumps(GROW_INVESTIGATOR_RESPONSE_SCHEMA))
    properties = _dict(schema.get("properties"))
    properties["status"] = {
        **_dict(properties.get("status")),
        "enum": ["needs_clarification", "complete"],
    }
    for field in ("claims", "answer_claims", "exclusions", "tool_calls"):
        properties[field] = {**_dict(properties.get(field)), "maxItems": 0}
    schema["properties"] = properties
    return schema


def _closed_gap_decision_repair_schema() -> dict[str, Any]:
    """Narrow a closed-gap repair to a terminal decision/defer action."""

    schema = json.loads(json.dumps(GROW_INVESTIGATOR_RESPONSE_SCHEMA))
    properties = _dict(schema.get("properties"))
    properties["status"] = {
        **_dict(properties.get("status")),
        "enum": ["complete"],
    }
    for field in ("claims", "answer_claims", "exclusions", "tool_calls", "questions"):
        properties[field] = {**_dict(properties.get(field)), "maxItems": 0}
    properties["decisions"] = {
        **_dict(properties.get("decisions")),
        "minItems": 1,
    }
    schema["properties"] = properties
    return schema


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _is_v3_search_receipt(value: Mapping[str, Any]) -> bool:
    return str(value.get("schema_version") or "") == "agvm.grow_search_receipt.v1"


def _receipt_has_semantic_firewall(value: Mapping[str, Any]) -> bool:
    review = _dict(value.get("query_review")) or _dict(value.get("query_critic"))
    firewall = _dict(review.get("semantic_firewall"))
    version = str(firewall.get("schema_version") or "")
    if version == "agvm.grow_search_semantic_firewall.v7":
        repair_count = firewall.get("pre_search_provider_repair_call_count")
        return bool(
            firewall.get("source_claim_visible_to_search_runtime") is False
            and firewall.get("investigator_query_visible_to_search_runtime") is False
            and firewall.get("source_blind_query_authority_used") is True
            and firewall.get("single_provider_query_authority") is True
            and isinstance(repair_count, int)
            and not isinstance(repair_count, bool)
            and 0 <= repair_count <= 1
            and str(firewall.get("neutral_subject_anchors_sha256") or "").strip()
            and str(firewall.get("subject_anchor_verification_sha256") or "").strip()
            and str(firewall.get("isolated_query_boundary_sha256") or "").strip()
        )
    return False


def _receipt_evidence_usable(value: Mapping[str, Any]) -> bool:
    if _is_v3_search_receipt(value):
        return _receipt_has_semantic_firewall(value) and value.get("evidence_usable") is True
    return value.get("usable") is True


def _receipt_question_usable(value: Mapping[str, Any]) -> bool:
    if _is_v3_search_receipt(value):
        if not _receipt_has_semantic_firewall(value):
            return False
        if "question_usable" in value:
            return value.get("question_usable") is True
        # Persisted V3 receipts created before this additive capability retain
        # evidence-backed question authority. A legacy no-match gains no new
        # authority without the explicit capability.
        return value.get("evidence_usable") is True
    return value.get("usable") is True


def _receipt_decision_usable(value: Mapping[str, Any]) -> bool:
    if _is_v3_search_receipt(value):
        return _receipt_has_semantic_firewall(value) and value.get("decision_usable") is True
    return value.get("usable") is True


def _receipt_novelty_certified(value: Mapping[str, Any]) -> bool:
    if _is_v3_search_receipt(value):
        return _receipt_has_semantic_firewall(value) and value.get("novelty_certified") is True
    return value.get("authoritative_no_match") is True and value.get("usable") is True


def _receipt_document_reference_ids(value: Mapping[str, Any]) -> set[str]:
    return {
        str(candidate or "").strip()
        for reference in _dicts(value.get("document_references"), limit=96)
        for candidate in (
            reference.get("document_id"),
            reference.get("anchor_node_id"),
            reference.get("node_id"),
        )
        if str(candidate or "").strip()
    }


def _document_hydration_candidate_receipts(
    receipts: Sequence[Mapping[str, Any]],
    *,
    claim_ids: Sequence[str],
    document_ids: Sequence[str],
) -> list[Mapping[str, Any]]:
    requested = {str(item or "").strip() for item in document_ids if str(item or "").strip()}
    if not requested:
        return []
    candidates: list[Mapping[str, Any]] = []
    for receipt in receipts:
        if not _receipt_evidence_usable(receipt):
            continue
        receipt_claim_ids = {
            str(claim_id or "").strip()
            for claim_id in list(receipt.get("affected_claim_ids") or [])
            if str(claim_id or "").strip()
        }
        if any(claim_id not in receipt_claim_ids for claim_id in claim_ids):
            continue
        if requested.issubset(_receipt_document_reference_ids(receipt)):
            candidates.append(receipt)
    return candidates


def _dicts(value: Any, *, limit: int) -> list[dict[str, Any]]:
    return [dict(item) for item in list(value or []) if isinstance(item, Mapping)][:limit]


def _strings(value: Any, *, limit: int) -> list[str]:
    result: list[str] = []
    for item in list(value or []):
        normalized = str(item or "").strip()
        if normalized and normalized not in result:
            result.append(normalized)
        if len(result) >= limit:
            break
    return result


def _compact_source_blind_text(value: Any, *, limit: int) -> str:
    text = " ".join(str(value or "").split()).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].strip() or text[:limit].strip()


def _ordered_strings(value: Any, *, limit: int) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return []
    result: list[str] = []
    for item in list(value)[:limit]:
        normalized = str(item or "").strip()
        if not normalized:
            return []
        result.append(normalized)
    return result


def _temporal_basis_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _normalize_temporal_scope(
    value: Any,
    *,
    claim: Mapping[str, Any],
    source_investigation: Mapping[str, Any],
    questions: Sequence[Mapping[str, Any]] = (),
    hydrated_evidence: Mapping[str, Any] | None = None,
    allowed_hydrated_evidence_refs: set[str] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate AI-authored semantic time against exact, already-authorized evidence spans.

    This deliberately does not parse dates or infer a temporal role.  The model
    owns the semantic interpretation; the server owns whether every cited
    temporal mention is an exact substring of the source, an answered human
    clarification, or Search-returned hydrated brain evidence.
    """

    if value is None:
        return None, None
    if not isinstance(value, Mapping):
        return None, "temporal_scope_structured_required"
    scope = dict(value)
    if str(scope.get("schema_version") or "") != GROW_TEMPORAL_SCOPE_SCHEMA_VERSION:
        return None, "temporal_scope_schema_invalid"

    normalized: dict[str, Any] = {
        "schema_version": GROW_TEMPORAL_SCOPE_SCHEMA_VERSION,
        "summary": str(scope.get("summary") or "").strip() or None,
        "temporal_role": str(scope.get("temporal_role") or "").strip() or None,
        "observed_at": str(scope.get("observed_at") or "").strip() or None,
        "valid_from": str(scope.get("valid_from") or "").strip() or None,
        "valid_to": str(scope.get("valid_to") or "").strip() or None,
        "temporal_mentions": [],
    }
    allowed_keys = set(normalized)
    if set(scope) != allowed_keys:
        return None, "temporal_scope_fields_invalid"

    source_unit_id = str(claim.get("source_unit_id") or "").strip()
    source_units = {
        str(item.get("unit_id") or ""): item
        for item in _source_units(source_investigation)
    }
    source_text = str(_dict(source_units.get(source_unit_id)).get("raw_text") or "")
    try:
        claim_start = int(claim.get("quote_start"))
        claim_end = int(claim.get("quote_end"))
    except (TypeError, ValueError):
        claim_start = -1
        claim_end = -1

    question_answers: dict[str, str] = {}
    for question in questions:
        question_id = str(question.get("question_id") or "").strip()
        if (
            question_id
            and str(question.get("answer_state") or "") == "answered"
            and str(claim.get("claim_id") or "") in _strings(question.get("affected_claim_ids"), limit=128)
        ):
            question_answers[question_id] = _temporal_basis_text(question.get("answer"))
    brain_material = {
        str(node_id): str(_dict(material).get("canonical_text") or _dict(material).get("summary") or "")
        for node_id, material in dict(hydrated_evidence or {}).items()
        if str(node_id).strip()
        and (
            allowed_hydrated_evidence_refs is None
            or str(node_id) in allowed_hydrated_evidence_refs
        )
    }

    temporal_fields = {"summary", "temporal_role", "observed_at", "valid_from", "valid_to"}
    supported_fields: set[str] = set()
    seen_mentions: set[tuple[str, str, int, int, str]] = set()
    raw_mentions_value = scope.get("temporal_mentions")
    if not isinstance(raw_mentions_value, list):
        return None, "temporal_mentions_invalid"
    raw_mentions = _dicts(raw_mentions_value, limit=25)
    if len(raw_mentions) != len(raw_mentions_value) or len(raw_mentions) > 24:
        return None, "temporal_mentions_invalid"
    for mention in raw_mentions:
        kind = str(mention.get("basis_kind") or "").strip()
        basis_ref = str(mention.get("basis_ref") or "").strip()
        exact_text = str(mention.get("exact_text") or "")
        raw_supports_fields = mention.get("supports_fields")
        if not isinstance(raw_supports_fields, list):
            return None, "temporal_mention_supports_fields_invalid"
        supports_fields = _strings(raw_supports_fields, limit=6)
        if (
            len(supports_fields) != len(raw_supports_fields)
            or not supports_fields
            or len(supports_fields) > 5
            or any(field not in temporal_fields for field in supports_fields)
        ):
            return None, "temporal_mention_supports_fields_invalid"
        try:
            span_start = int(mention.get("span_start"))
            span_end = int(mention.get("span_end"))
        except (TypeError, ValueError):
            return None, "temporal_mention_span_invalid"
        if set(mention) != {
            "basis_kind",
            "basis_ref",
            "span_start",
            "span_end",
            "exact_text",
            "supports_fields",
        }:
            return None, "temporal_mention_fields_invalid"
        if kind == "source_span":
            if basis_ref != source_unit_id or not (claim_start <= span_start < span_end <= claim_end):
                return None, "temporal_mention_source_binding_invalid"
            basis_text = source_text
        elif kind == "clarified_answer":
            basis_text = question_answers.get(basis_ref, "")
        elif kind == "hydrated_brain_evidence":
            basis_text = brain_material.get(basis_ref, "")
        else:
            return None, "temporal_mention_basis_kind_invalid"
        if (
            not basis_ref
            or not exact_text
            or span_start < 0
            or span_end <= span_start
            or span_end > len(basis_text)
            or basis_text[span_start:span_end] != exact_text
        ):
            return None, "temporal_mention_exact_span_invalid"
        for field in ("observed_at", "valid_from", "valid_to"):
            if field in supports_fields and normalized.get(field) != exact_text:
                return None, f"temporal_mention_value_binding_invalid:{field}"
        signature = (kind, basis_ref, span_start, span_end, exact_text)
        if signature in seen_mentions:
            return None, "temporal_mention_duplicate"
        if any(not normalized.get(field) for field in supports_fields):
            return None, "temporal_mention_supports_empty_field"
        seen_mentions.add(signature)
        supported_fields.update(supports_fields)
        normalized["temporal_mentions"].append(
            {
                "basis_kind": kind,
                "basis_ref": basis_ref,
                "span_start": span_start,
                "span_end": span_end,
                "exact_text": exact_text,
                "supports_fields": supports_fields,
            }
        )

    has_semantic_time = any(
        normalized.get(field)
        for field in ("summary", "temporal_role", "observed_at", "valid_from", "valid_to")
    )
    if has_semantic_time and not normalized["temporal_mentions"]:
        return None, "temporal_scope_evidence_required"
    unsupported_fields = sorted(
        field for field in temporal_fields if normalized.get(field) and field not in supported_fields
    )
    if unsupported_fields:
        return None, f"temporal_scope_field_evidence_required:{unsupported_fields[0]}"
    return normalized, None


def _content_sha256(raw_text: str) -> str:
    return f"sha256:{hashlib.sha256(raw_text.encode('utf-8')).hexdigest()}"


def _canonical_upstream_digest(value: Any) -> str:
    normalized = str(value or "").strip().casefold()
    if not normalized:
        return ""
    return normalized if normalized.startswith("sha256:") else f"sha256:{normalized}"


def _source_units(source_investigation: Mapping[str, Any]) -> list[dict[str, Any]]:
    return _dicts(source_investigation.get("source_units"), limit=10_000)


def _source_temporal_provenance(source_unit: Mapping[str, Any]) -> dict[str, str | None]:
    return grow_source_temporal_provenance(source_unit)


def _source_attribution(
    source_investigation: Mapping[str, Any],
    source_unit: Mapping[str, Any],
) -> dict[str, str | None]:
    unit_provenance = _dict(source_unit.get("provenance"))
    source_provenance = _dict(source_investigation.get("provenance"))
    source_request = _dict(source_investigation.get("source_request"))
    source_options = _dict(source_request.get("options")) or _dict(source_investigation.get("options"))
    source_uri = str(
        source_unit.get("source_uri")
        or unit_provenance.get("source_uri")
        or source_investigation.get("source_uri")
        or source_provenance.get("source_uri")
        or source_request.get("source_uri")
        or (
            source_request.get("raw_input")
            if str(source_request.get("raw_input") or "").strip().lower().startswith(("http://", "https://"))
            else None
        )
        or ""
    ).strip()
    source_trust = str(
        source_unit.get("source_trust")
        or unit_provenance.get("source_trust")
        or source_unit.get("trust")
        or unit_provenance.get("trust")
        or source_investigation.get("source_trust")
        or source_provenance.get("source_trust")
        or source_investigation.get("trust")
        or source_provenance.get("trust")
        or source_unit.get("source_type")
        or unit_provenance.get("source_type")
        or source_investigation.get("source_type")
        or source_provenance.get("source_type")
        or source_request.get("source_trust")
        or source_options.get("source_trust")
        or ""
    ).strip()
    return {
        "source_uri": source_uri or None,
        "source_trust": source_trust or None,
    }


def _source_sha256(source_investigation: Mapping[str, Any]) -> str:
    return grow_source_sha256(source_investigation)


def _grow_graph_snapshot_sha256(graph_snapshot: Mapping[str, Any]) -> str:
    """Hash the semantic graph snapshot while excluding volatile read metadata."""

    snapshot = dict(graph_snapshot)
    meta = dict(_dict(snapshot.get("meta")))
    for key in GROW_GRAPH_SNAPSHOT_VOLATILE_META_KEYS:
        meta.pop(key, None)
    if meta:
        snapshot["meta"] = meta
    else:
        snapshot.pop("meta", None)
    return stable_digest(snapshot)


def _question_content_fingerprint(question: Mapping[str, Any]) -> str:
    return stable_digest(
        {
            "affected_claim_ids": sorted(
                _strings(question.get("affected_claim_ids"), limit=128)
            ),
            "question_text": " ".join(
                str(question.get("question_text") or "").split()
            ).casefold(),
            "answer_type": str(question.get("answer_type") or "").strip().casefold(),
            "choices": [
                " ".join(choice.split()).casefold()
                for choice in _strings(question.get("choices"), limit=24)
            ],
            "decision_effect": str(question.get("decision_effect") or "")
            .strip()
            .casefold(),
        }
    )


def _closed_gap_scope_evidence_refs(question: Mapping[str, Any]) -> set[str]:
    return set(_strings(question.get("evidence_refs"), limit=32))


def _same_closed_gap_verifier_scope(
    candidate_question: Mapping[str, Any],
    closed_question: Mapping[str, Any],
) -> bool:
    candidate_claim_ids = set(
        _strings(candidate_question.get("affected_claim_ids"), limit=128)
    )
    closed_claim_ids = set(_strings(closed_question.get("affected_claim_ids"), limit=128))
    return bool(candidate_claim_ids.intersection(closed_claim_ids)) and bool(
        _closed_gap_scope_evidence_refs(candidate_question).intersection(
            _closed_gap_scope_evidence_refs(closed_question)
        )
    )


def _server_question_id(
    *,
    investigation_id: str,
    turn: int,
    provider_question_id: str,
    question: Mapping[str, Any],
    reserved_ids: set[str],
) -> str:
    base = (
        "grow-question::"
        + stable_digest(
            {
                "investigation_id": investigation_id,
                "turn": turn,
                "provider_question_id": provider_question_id,
                "question_fingerprint": _question_content_fingerprint(question),
            }
        )[:24]
    )
    candidate = base
    suffix = 2
    while candidate in reserved_ids:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def default_grow_investigator_provider(request: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, dict[str, Any]]:
    try:
        try:
            from .llm import grow_semantic_model, llm_enabled, structured_json
        except ImportError:  # pragma: no cover - direct API runtime
            from llm import grow_semantic_model, llm_enabled, structured_json

        api_key_override = str(request.get("api_key_override") or "").strip() or None
        model = str(
            request.get("model_override")
            or os.getenv("AGVM_GROW_AI_INVESTIGATOR_MODEL")
            or grow_semantic_model()
        ).strip()
        if not llm_enabled() and not api_key_override:
            return None, "llm_disabled", {"provider": "openai_compatible", "model": model}
        metadata: dict[str, Any] = {}
        payload, error = structured_json(
            model=model,
            system_prompt=str(request.get("system_prompt") or ""),
            user_prompt=str(request.get("user_prompt") or ""),
            schema_name=str(request.get("schema_name") or "agvm_grow_investigator_v3"),
            schema=_dict(request.get("schema")),
            timeout=max(0.001, float(request.get("timeout_seconds") or 60.0)),
            role="grow_semantic",
            max_output_tokens=10_000,
            api_key_override=api_key_override,
            execution_metadata=metadata,
        )
        return payload, error, metadata
    except Exception as exc:  # noqa: BLE001
        return None, str(exc), {}


def resolve_grow_investigator_provider(
    provider_request: Mapping[str, Any] | None = None,
) -> Provider | None:
    try:
        try:
            from .llm import llm_enabled
        except ImportError:  # pragma: no cover - direct API runtime
            from llm import llm_enabled
        overrides = {
            key: value
            for key, value in _dict(provider_request).items()
            if key in {"api_key_override", "model_override", "source_investigation_id"}
            and value is not None
            and value != ""
        }
        if not llm_enabled() and not str(overrides.get("api_key_override") or "").strip():
            return None
        if not overrides:
            return default_grow_investigator_provider

        def configured_provider(request: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, dict[str, Any]]:
            return default_grow_investigator_provider({**request, **overrides})

        return configured_provider
    except Exception:  # noqa: BLE001
        return None


def _system_prompt() -> str:
    return (
        "You are the Detwin Grow AI investigator. Detwin Brain and brain evidence always mean the user's knowledge graph "
        "and memory store, never a biological brain, neuroscience, or scientific brain research. Frame every "
        "fact-eligible source unit into exact-span atomic claims, "
        "write neutral, falsifiable investigation missions, and review only evidence returned by Search. The executable "
        "Search query is written by a separate source-blind authority that receives only exact server-bound subject identifiers "
        "and a neutral mission projection. "
        "The investigator's candidate query is diagnostic and is never sent to Search. The first Search wave MUST "
        "author an assertion-neutral semantic mission that preserves the claim's actual fact category—any property, relation, event, participant, "
        "quantity, cause or temporal scope—while turning every asserted value into an unknown to discover. It must then ask an open discovery "
        "question that can satisfy that mission independently of the source claim; do not collapse arbitrary claims into generic identity or services. "
        "It MUST NOT ask for yes/no confirmation of the assertion or embed the assertion as its premise. Fill query_strategy truthfully: treat the source "
        "claim as a hypothesis, and author the candidate mission directly in neutral_goal, free-form discovery_dimensions, temporal_focus, "
        "counterfactual_safety_summary and plausible_alternatives. These semantic strings must be non-empty except that plausible_alternatives may "
        "be empty. Executable query authority comes from the isolated composer and server-verified exact subject spans. The first turn plans one "
        "Search wave for the whole claim batch. That wave may contain one or two provider-authored search_brain calls when separate natural-language "
        "queries are semantically useful; their affected_claim_ids must collectively cover the claim batch and they execute as the same parallel Search wave. "
        "The first turn never creates final memories or asks semantic questions. Later turns are AI Review only: choose exactly "
        "one action category, hydrate, ask_questions, decide, or defer. A hydrate action is status=continue with "
        "questions=[] and decisions=[]; ask_questions is status=needs_clarification with tool_calls=[] and "
        "decisions=[]; decide or defer is status=complete with tool_calls=[] and questions=[]. When a question-usable receipt exists and a human "
        "answer can change identity, truth, scope, merge, supersede, derivation or utility, ask a non-empty human question "
        "before deferring. A defer decision is valid only when you explicitly attest that no clarification can change the "
        "decision and explain why; it routes unsafe structural work to maintenance review. Hydrate only Search-returned "
        "node IDs, hydrate only document IDs discovered in a usable Search receipt, decide evidence-bound memory acts, or ask a targeted question only when its answer changes identity, truth, "
        "scope, merge, supersede, derivation, or utility. Do not Search again before every available decision-changing question has been asked. After human answers, "
        "Search again at most once and with exactly one search_brain call only for a new exact-span answer_claim that the server binds from the clarification answer; otherwise run a new AI Review on the existing receipt and hydration ledger. "
        "Never repair semantic uncertainty by inventing another Search query, question, or decision. Temporal scope is a semantic part of a claim: when temporal "
        "ambiguity can change identity, truth, merge, supersede, or utility, ask a targeted human question instead of "
        "guessing. Explain what the source says, what the brain evidence says, why the date or validity interval changes "
        "the decision, and exactly what answer is needed. Author each question from the evidence in this investigation; "
        "When you author temporal_scope, use its structured semantic fields and cite every temporal mention with an exact "
        "span from the bound source claim, an answered clarification affecting that claim, or hydrated brain evidence. "
        "Every temporal mention must name supports_fields, and every populated temporal field must be supported by at least "
        "one cited mention. Do not populate semantic time without such a span and do not normalize or parse dates on the "
        "server's behalf. never use keyword rules, canned temporal questions, or fixed questionnaires. Source published, "
        "acquired and retrieved timestamps are source chronology; publication may explain evidence availability but none "
        "of these timestamps silently becomes event time or a validity interval. Source acquired/retrieved timestamps "
        "and brain created_at/ingested_at timestamps are provenance or audit metadata only: never treat them as the event "
        "time, validity interval, or proof that a claim is true. Never use keyword overlap as semantic authority. Never emit hidden "
        "reasoning or chain-of-thought. On the first turn set claim_id and source_unit_content_sha256 to null; the server "
        "binds them. Frame each autonomous source assertion as its own claim when it is fact-eligible and bound to "
        "a verbatim source span; do not collapse service scope, capabilities, schedules, staffing, quantities, or "
        "operating requirements into a different regulatory or identity claim. For every initial claim, identify the "
        "neutral subject name, identifier, or—when the atomic quote "
        "contains no proper name—the smallest exact noun phrase that denotes the subject without including the asserted "
        "value. Provide subject_anchor_start, subject_anchor_end and neutral_subject_anchor; do not turn a certification, "
        "role, industry, event, date, relationship or other asserted value into the subject. For service, capability, "
        "schedule, or staffing assertions, anchor the exact source noun phrase naming that service, capability, "
        "schedule, or staff group when no proper-name subject is present. The server binds and freezes "
        "that exact span. On later turns "
        "either return claims=[] or restate the complete existing claim ledger using each existing "
        "claim_id/claim_key and its exact source binding; you may refine only semantic annotations, never add, drop, rebind, "
        "or alter a source span/hash. Prefer claims=[] on every later turn: the server already owns and supplies the "
        "canonical claim ledger, so copying its immutable source spans back is unnecessary and can only introduce "
        "serialization drift. A later turn may propose a genuinely new atomic claim in answer_claims only when it "
        "is grounded in an exact span of an answered clarification: set basis_kind=clarified_answer, basis_ref to that "
        "question_id, basis_content_sha256=null, and parent_claim_id to an affected existing claim. The server binds its ID "
        "and digest, and that claim must Search before any question, decision, preview, or compilation. Do not restate an "
        "existing claim as an answer claim. Never reuse a question_id already present in clarifications; answered or deferred "
        "questions are closed and must not be emitted again. If a genuinely new question is needed, author a distinct "
        "question_id and the server may canonicalize any accidental collision. A human answer closes the specific "
        "gap asked by that question. When an answer says the requested source detail, regulation, code, role, "
        "characteristic, date, scope or other detail is absent, unknown, or not specified in the source, treat that as "
        "negative evidence for the requested detail and do not ask a paraphrase of the same missing-detail gap. Use the "
        "closed answer to exclude unsupported subclaims, make an evidence-bound decision from remaining evidence, or "
        "defer for maintenance review when no clarification can change the decision; ask again only for a genuinely "
        "distinct unresolved decision-changing gap. Ask every non-redundant clarification whose answer can independently change a "
        "claim decision, grouped in one turn up to the supplied question limit; do not collapse distinct unknowns into "
        "one question. Return concise rationales and the exact JSON schema only. new_memory is allowed only "
        "from an authoritative no-match receipt or a compatible-but-distinct evidence decision. source_only is allowed only "
        "for a fact-eligible claim whose exact source span/hash and source URI/trust attribution are server-bound, after "
        "Search has run on the same brain revision as a boundary and returned no decision-usable equivalent or conflicting target; "
        "state that the memory is source-bound rather than independently verified. Every question and decision "
        "must explain in human language: what the source claims, what the brain evidence says, the X-versus-Y comparison, "
        "why it matters, the next user action, and the exact receipt/node evidence refs. IDs and protocol codes are evidence, "
        "never the user-facing explanation."
    )


def _validate_search_query_strategy(value: Any, *, first_wave: bool) -> str | None:
    """Validate AI-declared epistemic shape without classifying query keywords."""

    strategy = _dict(value)
    valid_modes = {
        "open_discovery",
        "identity_scope_discovery",
        "alternative_test",
        "evidence_followup",
    }
    query_mode = str(strategy.get("query_mode") or "").strip()
    if query_mode not in valid_modes:
        return "query_mode_invalid"
    if first_wave and query_mode not in {"open_discovery", "identity_scope_discovery"}:
        return "first_wave_requires_open_discovery"
    if not str(strategy.get("independent_information_need") or "").strip():
        return "independent_information_need_missing"
    if not str(strategy.get("neutral_goal") or "").strip():
        return "neutral_goal_missing"
    if not _strings(strategy.get("discovery_dimensions"), limit=12):
        return "discovery_dimensions_missing"
    if not str(strategy.get("temporal_focus") or "").strip():
        return "temporal_focus_missing"
    if not str(strategy.get("counterfactual_safety_summary") or "").strip():
        return "counterfactual_safety_summary_missing"
    # The strategy is mission context for the Search call. It is not a
    # self-attesting semantic certificate; server-bound source spans and the
    # later Search receipt carry authority.
    return None


def _search_continuation_brief(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Project only Search-Master-authored continuation semantics for Grow.

    The projection deliberately does not infer a query or classify the gap.  It
    gives the investigator and the isolated query composer the semantic work
    that Search says remains, while preserving Search as the sole authority for
    its sufficiency verdict.
    """

    master = _dict(receipt.get("master_judgement"))
    continuation = _dict(master.get("continuation_recommendation"))

    def text_value(*values: Any, limit: int = 800) -> str | None:
        for value in values:
            text = str(value or "").strip()
            if text:
                return text[:limit]
        return None

    brief = {
        "schema_version": "agvm.grow_search_continuation_brief.v1",
        "receipt_id": text_value(receipt.get("receipt_id"), limit=200),
        "master_state": text_value(master.get("master_state"), limit=120),
        "master_decision": text_value(master.get("decision"), limit=120),
        "unresolved_gap": text_value(
            master.get("unresolved_gap"),
            continuation.get("unresolved_gap"),
        ),
        "expected_information_gain": text_value(
            master.get("expected_information_gain"),
            continuation.get("expected_information_gain"),
        ),
        "next_evidence_action": text_value(
            master.get("next_evidence_action"),
            continuation.get("next_evidence_action"),
            continuation.get("reason"),
        ),
        "recommended_tool_action": text_value(
            continuation.get("tool_action"),
            master.get("next_recommended_call"),
            limit=160,
        ),
        "continuation_state": text_value(continuation.get("state"), limit=120),
    }
    brief["material_continuation_available"] = bool(
        brief["unresolved_gap"]
        or brief["expected_information_gain"]
        or brief["next_evidence_action"]
    )
    return brief


def _search_continuation_briefs(
    receipts: Sequence[Mapping[str, Any]], *, limit: int = 4
) -> list[dict[str, Any]]:
    return [
        brief
        for receipt in list(receipts)[-limit:]
        if (brief := _search_continuation_brief(receipt))["material_continuation_available"]
    ]


def _receipt_question_evidence_authority(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Project Search-master evidence authority usable by human questions.

    A question may cite the receipt itself for a Search-declared gap.  Node
    refs are support authority only when Search-master accepted them or an
    entailed mission packet/assessment selected them.  Diagnostic-only and
    non-entailed refs remain visible in the receipt but are not executable
    question evidence refs.
    """

    receipt_id = str(receipt.get("receipt_id") or "").strip()
    receipt_node_ids = {
        str(node_id or "").strip()
        for node_id in list(receipt.get("evidence_node_ids") or [])
        if str(node_id or "").strip()
    }
    document_ids = _receipt_document_reference_ids(receipt)
    receipt_refs = receipt_node_ids | document_ids
    master = _dict(receipt.get("master_judgement"))
    supported_refs: set[str] = set()
    search_master_authority_present = bool(master)

    def add_supported_ref(value: Any) -> None:
        ref = str(value or "").strip()
        if ref and ref in receipt_refs:
            supported_refs.add(ref)

    ai_decisions = [
        _dict(master.get("ai_master_decision")),
        _dict(_dict(master.get("sufficiency_judge")).get("ai_master_decision")),
    ]
    validations = [
        _dict(master.get("mission_entailment_validation")),
        *[_dict(decision.get("mission_entailment_validation")) for decision in ai_decisions],
    ]
    for validation in validations:
        for ref in _strings(validation.get("accepted_support_ids"), limit=512):
            add_supported_ref(ref)
    for decision in ai_decisions:
        for assessment in _dicts(decision.get("mission_evidence_assessments"), limit=256):
            if assessment.get("entailed") is not True:
                continue
            if assessment.get("direct_mission_fit") is False:
                continue
            if assessment.get("success_criteria_satisfied") is False:
                continue
            for ref in _strings(assessment.get("evidence_node_ids"), limit=512):
                add_supported_ref(ref)
        authority = _dict(decision.get("_evidence_authority"))
        for packet in _dicts(authority.get("mission_claim_packets"), limit=256):
            if packet.get("entailed") is not True:
                continue
            for ref in _strings(packet.get("evidence_node_ids"), limit=512):
                add_supported_ref(ref)

    if not search_master_authority_present:
        supported_refs.update(receipt_refs)

    gap_refs = [receipt_id] if receipt_id else []
    allowed_refs = sorted({*gap_refs, *supported_refs})
    return {
        "schema_version": "agvm.grow_question_evidence_authority.v1",
        "receipt_id": receipt_id,
        "gap_authority_refs": gap_refs,
        "supported_node_ids": sorted(supported_refs & receipt_node_ids),
        "supported_document_ids": sorted(supported_refs & document_ids),
        "allowed_evidence_refs": allowed_refs,
        "search_master_authority_present": search_master_authority_present,
        "diagnostic_only_refs_executable": False,
        "non_entailed_refs_executable": False,
    }


def _source_blind_neutral_mission(
    query_strategy: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Project AI-authored neutral mission fields without exposing the claim."""

    strategy = _dict(query_strategy)
    default_goal = (
        "Openly discover the verified subjects' identity, scope, activities, "
        "relationships and relevant temporal context from Detwin knowledge graph "
        "and memory evidence."
    )
    dimensions = [
        _compact_source_blind_text(item, limit=180)
        for item in _strings(strategy.get("discovery_dimensions"), limit=12)
    ]
    dimensions = [item for item in dimensions if item]
    neutral_goal = _compact_source_blind_text(
        strategy.get("neutral_goal"), limit=700
    ) or default_goal
    temporal_focus = _compact_source_blind_text(
        strategy.get("temporal_focus"), limit=360
    )
    counterfactual_summary = _compact_source_blind_text(
        strategy.get("counterfactual_safety_summary"), limit=500
    )
    return {
        "schema_version": "agvm.grow_neutral_search_mission.v1",
        "goal": neutral_goal,
        "discovery_dimensions": dimensions,
        "temporal_focus": temporal_focus,
        "counterfactual_safety_summary": counterfactual_summary,
        "evidence_policy": (
            "Ask what the Detwin knowledge graph and memory store support without "
            "assuming any value asserted by the source being investigated."
        ),
        "brain_domain_policy": (
            "Detwin Brain, AGVM brain and brain evidence mean the configured "
            "knowledge graph and memory store only. They do not mean a biological "
            "brain, neuroscience, anatomy, medicine or cognitive science."
        ),
        "source_assertion_policy": (
            "The source claim, candidate query and asserted predicate values are "
            "not visible to the source-blind authority. Mission fields provide "
            "neutral investigation context, not facts to confirm."
        ),
    }


def _server_bound_subject_material(
    *,
    claims: Sequence[Mapping[str, Any]],
    affected_claim_ids: Sequence[str],
) -> tuple[list[str], list[dict[str, Any]], str]:
    """Return exact subject identifiers without exposing source predicates."""

    affected = _strings(affected_claim_ids, limit=128)
    affected_set = set(affected)
    claim_context = [
        {
            "claim_id": str(claim.get("claim_id") or ""),
            "exact_quote": str(claim.get("exact_quote") or ""),
            "quote_start": int(claim.get("quote_start") or 0),
            "neutral_subject_anchor": str(
                claim.get("neutral_subject_anchor") or ""
            ),
            "subject_anchor_start": int(claim.get("subject_anchor_start") or 0),
            "subject_anchor_end": int(claim.get("subject_anchor_end") or 0),
        }
        for claim in claims
        if str(claim.get("claim_id") or "") in affected_set
    ]
    if not affected or {row["claim_id"] for row in claim_context} != affected_set:
        raise RuntimeError("search_query_verification_claim_binding_invalid")

    verified_anchors: list[str] = []
    server_anchor_rows: list[dict[str, Any]] = []
    for row in claim_context:
        anchor = str(row.get("neutral_subject_anchor") or "").strip()
        quote = str(row.get("exact_quote") or "")
        quote_start = int(row.get("quote_start") or 0)
        anchor_start = int(row.get("subject_anchor_start") or 0)
        anchor_end = int(row.get("subject_anchor_end") or 0)
        rel_start = anchor_start - quote_start
        rel_end = anchor_end - quote_start
        if (
            not anchor
            or not (0 <= rel_start < rel_end <= len(quote))
            or quote[rel_start:rel_end] != anchor
        ):
            raise RuntimeError("search_query_anchor_binding_invalid")
        verified_anchors.append(anchor)
        server_anchor_rows.append(
            {
                "claim_id": row["claim_id"],
                "verified_identifier": anchor,
                "verified_identifier_start": anchor_start,
                "verified_identifier_end": anchor_end,
                "verification_summary": (
                    "Server verified the exact subject identifier span against "
                    "the bound source quote."
                ),
            }
        )
    return verified_anchors, server_anchor_rows, stable_digest(claim_context)


def _isolated_query_provider_call(
    *,
    provider: Provider,
    schema_name: str,
    schema: Mapping[str, Any],
    system_prompt: str,
    user_payload: Mapping[str, Any],
    deadline: _GrowDeadline,
    stage: str,
    role: str,
    call_id: str,
    brain_revision: str,
    parent_operation_id: str,
    timeout_cap_seconds: float | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    timeout_seconds = deadline.require(stage)
    if timeout_cap_seconds is not None:
        timeout_seconds = max(0.001, min(float(timeout_seconds), float(timeout_cap_seconds)))
    request = {
        "schema_name": schema_name,
        "schema": dict(schema),
        "system_prompt": system_prompt,
        "user_prompt": json.dumps(
            dict(user_payload), ensure_ascii=True, sort_keys=True
        ),
        "timeout_seconds": timeout_seconds,
    }
    payload, error, metadata = _call_provider_with_hard_timeout(
        provider,
        request,
        timeout_seconds=timeout_seconds,
    )
    if error or payload is None:
        raise RuntimeError(f"{stage}_provider_failed:{error or 'invalid_output'}")
    attestation = validate_provider_call_attestation(
        metadata,
        request=request,
        payload=payload,
    )
    return dict(payload), execution_ledger_entry(
        role=role,
        call_id=call_id,
        child_call_id=call_id,
        parent_operation_id=parent_operation_id,
        billing_scope="parent_grow_preview",
        brain_revision=brain_revision,
        attestation=attestation,
    )


def _closed_clarification_gap_reopened(
    *,
    provider: Provider,
    candidate_question: Mapping[str, Any],
    closed_questions: Sequence[Mapping[str, Any]],
    investigation_id: str,
    turn: int,
    deadline: _GrowDeadline,
    brain_revision: str,
    parent_operation_id: str,
    call_ordinal: int,
) -> tuple[str | None, dict[str, Any]]:
    if not closed_questions:
        return None, {}
    payload, ledger_entry = _isolated_query_provider_call(
        provider=provider,
        schema_name="agvm_grow_closed_clarification_gap_verifier_v1",
        schema=_CLOSED_CLARIFICATION_GAP_VERIFIER_SCHEMA,
        system_prompt=(
            "You are a Detwin Grow clarification-gap verifier. Decide whether a newly proposed "
            "human question reopens the same source-bound gap already closed by a prior human "
            "answer for the same claim and decision effect. This is a semantic comparison, not "
            "keyword matching. Treat a prior answer that says a requested detail is absent, "
            "unknown, unnamed, unavailable, or not specified by the source as negative source "
            "evidence that closes that missing-detail gap. Return true only when the new "
            "question asks again for that same closed missing detail in different words. Return "
            "false when the new question asks a genuinely distinct decision-changing gap."
        ),
        user_payload={
            "schema_version": "agvm.grow_closed_clarification_gap_verifier.request.v1",
            "investigation_id": investigation_id,
            "turn": turn,
            "candidate_question": {
                "question_id": str(candidate_question.get("question_id") or ""),
                "question_text": str(candidate_question.get("question_text") or "")[:1000],
                "affected_claim_ids": _strings(candidate_question.get("affected_claim_ids"), limit=128),
                "decision_effect": str(candidate_question.get("decision_effect") or ""),
                "source_claim_summary": str(candidate_question.get("source_claim_summary") or "")[:800],
                "brain_evidence_summary": str(candidate_question.get("brain_evidence_summary") or "")[:800],
                "comparison": str(candidate_question.get("comparison") or "")[:800],
                "reason": str(candidate_question.get("reason") or "")[:800],
                "next_step": str(candidate_question.get("next_step") or "")[:800],
            },
            "closed_clarification_gaps": [
                {
                    "question_id": str(question.get("question_id") or ""),
                    "question_text": str(question.get("question_text") or "")[:1000],
                    "answer_state": str(question.get("answer_state") or "unanswered"),
                    "answer": str(question.get("answer") or "")[:1000],
                    "affected_claim_ids": _strings(question.get("affected_claim_ids"), limit=128),
                    "decision_effect": str(question.get("decision_effect") or ""),
                    "source_claim_summary": str(question.get("source_claim_summary") or "")[:800],
                    "brain_evidence_summary": str(question.get("brain_evidence_summary") or "")[:800],
                    "comparison": str(question.get("comparison") or "")[:800],
                    "reason": str(question.get("reason") or "")[:800],
                    "next_step": str(question.get("next_step") or "")[:800],
                    "evidence_refs": _strings(question.get("evidence_refs"), limit=32),
                }
                for question in closed_questions
            ],
        },
        deadline=deadline,
        stage="grow_closed_clarification_gap_verifier",
        role="grow_closed_clarification_gap_verifier",
        call_id=(
            "grow-closed-gap-verifier::"
            f"{investigation_id}::turn-{turn}::ordinal-{call_ordinal}"
        ),
        brain_revision=brain_revision,
        parent_operation_id=parent_operation_id,
    )
    if payload.get("reopens_closed_gap") is not True:
        return None, ledger_entry
    closed_question_id = str(payload.get("closed_question_id") or "").strip()
    known_closed_ids = {
        str(question.get("question_id") or "")
        for question in closed_questions
        if str(question.get("question_id") or "")
    }
    if closed_question_id not in known_closed_ids:
        closed_question_id = sorted(known_closed_ids)[0] if known_closed_ids else "unknown"
    return f"closed_clarification_gap_reopened:{closed_question_id}", ledger_entry


def _compose_source_blind_search_query(
    *,
    provider: Provider,
    subject_anchors: Sequence[str],
    query_strategy: Mapping[str, Any] | None = None,
    deadline: _GrowDeadline,
    wave: int,
    brain_revision: str,
    parent_operation_id: str,
    investigation_id: str,
    max_repairs: int = 0,
    query_authority_timeout_seconds: float | None = None,
) -> tuple[str, str, dict[str, Any], list[dict[str, Any]]]:
    """Create Search text without giving either AI the source assertion."""

    anchors = _strings(subject_anchors, limit=128)
    if not anchors:
        raise RuntimeError("isolated_query_subject_anchor_required")
    neutral_mission = _source_blind_neutral_mission(query_strategy)
    system_prompt = (
        "You are the single source-blind authority for one AGVM Search query. "
        "You receive only exact server-verified subject anchors and a neutral "
        "mission, never the source claim. Detwin Brain, AGVM brain and brain "
        "evidence mean the configured knowledge graph and memory store, not a "
        "biological brain, neuroscience, anatomy, medicine or cognitive science. "
        "A server-bound subject may be a name, "
        "identifier, or exact noun phrase when the atomic source quote contains "
        "no proper name. Treat every supplied anchor only as an opaque subject "
        "reference: write and approve a natural-language query that openly "
        "discovers what entity or object it denotes, its scope, and what the "
        "knowledge graph supports. Use the neutral mission as investigation "
        "context, not as evidence and not as a source assertion to confirm. "
        "Quoting a descriptive subject anchor as a reference is not an assertion "
        "that its modifiers are true. Do not infer or test any certification, "
        "role, industry, activity, relationship, event, date or other source "
        "predicate. Reject only when no supplied anchor can denote a subject "
        "without treating an asserted value as the subject. Return only the "
        "required JSON schema."
    )

    def validate_payload(payload: Mapping[str, Any]) -> tuple[str, str, list[str]]:
        if (
            str(payload.get("schema_version") or "")
            != "agvm.grow_isolated_search_query.v1"
        ):
            raise RuntimeError("source_blind_query_authority_schema_invalid")
        if payload.get("approved") is not True:
            raise RuntimeError("source_blind_query_authority_rejected")
        text = " ".join(str(payload.get("query_text") or "").split()).strip()
        resolved_purpose = " ".join(
            str(payload.get("purpose") or "").split()
        ).strip()
        if (
            not text
            or not resolved_purpose
            or not str(payload.get("open_discovery_summary") or "").strip()
            or not str(payload.get("decision_summary") or "").strip()
        ):
            raise RuntimeError("source_blind_query_authority_output_incomplete")
        missing = [
            anchor
            for anchor in anchors
            if anchor.casefold() not in text.casefold()
        ]
        return text, resolved_purpose, missing

    authority_payload, authority_ledger = _isolated_query_provider_call(
        provider=provider,
        schema_name="agvm_grow_source_blind_search_query_v3",
        schema=_ISOLATED_QUERY_COMPOSER_SCHEMA,
        system_prompt=system_prompt,
        user_payload={
            "subject_anchors": anchors,
            "neutral_mission": neutral_mission,
        },
        deadline=deadline,
        stage="grow_source_blind_query_authority",
        role="grow_source_blind_query_authority",
        call_id=f"grow-query-authority::{investigation_id}::wave-{wave}",
        brain_revision=brain_revision,
        parent_operation_id=parent_operation_id,
        timeout_cap_seconds=query_authority_timeout_seconds,
    )
    ledgers = [authority_ledger]
    query_text, purpose, missing_anchors = validate_payload(authority_payload)
    repair_call_count = 0
    initial_authority_output_sha256 = stable_digest(authority_payload)
    if missing_anchors:
        if max_repairs <= 0:
            raise RuntimeError("isolated_query_anchor_coverage_invalid")
        repair_payload, repair_ledger = _isolated_query_provider_call(
            provider=provider,
            schema_name="agvm_grow_source_blind_search_query_v3",
            schema=_ISOLATED_QUERY_COMPOSER_SCHEMA,
            system_prompt=(
                f"{system_prompt} Repair the previous source-blind query by "
                "including every missing exact subject anchor verbatim while "
                "keeping the query open-discovery and source-predicate blind."
            ),
            user_payload={
                "subject_anchors": anchors,
                "neutral_mission": neutral_mission,
                "previous_query_text": query_text,
                "coverage_error": {
                    "missing_subject_anchors": missing_anchors,
                },
            },
            deadline=deadline,
            stage="grow_source_blind_query_authority_repair",
            role="grow_source_blind_query_authority",
            call_id=(
                f"grow-query-authority::{investigation_id}::wave-{wave}::repair-1"
            ),
            brain_revision=brain_revision,
            parent_operation_id=parent_operation_id,
            timeout_cap_seconds=query_authority_timeout_seconds,
        )
        repair_call_count = 1
        ledgers.append(repair_ledger)
        query_text, purpose, missing_anchors = validate_payload(repair_payload)
        authority_payload = repair_payload
        if missing_anchors:
            raise RuntimeError("isolated_query_anchor_coverage_invalid")

    query_sha256 = stable_digest(query_text)
    review = {
        "schema_version": "agvm.grow_isolated_query_boundary.v3",
        "query_sha256": query_sha256,
        "subject_anchors_sha256": stable_digest(anchors),
        "neutral_mission_sha256": stable_digest(neutral_mission),
        "authority_output_sha256": stable_digest(authority_payload),
        "source_claim_visible_to_authority": False,
        "predicate_values_visible_to_authority": False,
        "single_provider_query_authority": True,
        "approved": True,
        "pre_search_provider_repair_call_count": repair_call_count,
        "initial_authority_output_sha256": initial_authority_output_sha256,
    }
    return query_text, purpose, review, ledgers


def _build_server_bound_search_query(
    *,
    query_text: str,
    purpose: str,
    query_strategy: Mapping[str, Any],
    claims: Sequence[Mapping[str, Any]],
    affected_claim_ids: Sequence[str],
    first_wave: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Bind the investigator-authored query to server-verified source anchors."""

    affected = _strings(affected_claim_ids, limit=128)
    strategy = _dict(query_strategy)
    strategy_error = _validate_search_query_strategy(strategy, first_wave=first_wave)
    candidate_mission = {
        "affected_claim_ids": affected,
        "neutral_goal": str(strategy.get("neutral_goal") or "").strip(),
        "investigation_dimensions": _strings(
            strategy.get("discovery_dimensions"), limit=12
        ),
        "plausible_alternatives": _strings(
            strategy.get("plausible_alternatives"), limit=12
        ),
        "temporal_focus": str(strategy.get("temporal_focus") or "").strip(),
        "counterfactual_safety_summary": str(
            strategy.get("counterfactual_safety_summary") or ""
        ).strip(),
    }
    verified_anchors, server_anchor_rows, claim_set_sha256 = (
        _server_bound_subject_material(
            claims=claims,
            affected_claim_ids=affected,
        )
    )
    final_query = " ".join(str(query_text or "").split()).strip()
    final_purpose = " ".join(str(purpose or "").split()).strip()
    if not final_query:
        raise RuntimeError("search_query_text_missing")
    if not final_purpose:
        raise RuntimeError("search_purpose_missing")
    missing_anchors = [
        anchor
        for anchor in verified_anchors
        if anchor.casefold() not in final_query.casefold()
    ]
    if missing_anchors:
        raise RuntimeError("search_query_anchor_binding_missing")
    candidate_query_sha256 = stable_digest(query_text)
    mission_diagnostic = {
        "schema_version": "agvm.grow_investigation_mission_diagnostic.v1",
        "valid": strategy_error is None,
        "detail": strategy_error,
        "authority_role": "diagnostic_only",
        "mission_sha256": stable_digest(candidate_mission),
    }
    subject_anchor_verification = {
        "schema_version": "agvm.grow_subject_anchor_verification.v5",
        "claim_set_sha256": claim_set_sha256,
        "anchors": server_anchor_rows,
        "provider_executed": False,
        "authority_role": "server_structural_source_binding",
    }
    return {
        "schema_version": "agvm.grow_search_query_review.v5",
        "verdict": "server_bound",
        "reviewed_query_sha256": candidate_query_sha256,
        "claim_set_sha256": claim_set_sha256,
        "query_text": final_query,
        "purpose": final_purpose,
        "investigator_candidate_purpose_sha256": stable_digest(purpose),
        "neutral_subject_anchors": verified_anchors,
        "subject_anchor_verification": subject_anchor_verification,
        "investigation_mission": candidate_mission,
        "investigation_mission_verification": mission_diagnostic,
        "semantic_firewall": {
            "schema_version": "agvm.grow_search_semantic_firewall.v5",
            "source_claim_visible_to_search_runtime": False,
            "investigator_query_visible_to_search_runtime": True,
            "neutral_subject_anchors_sha256": stable_digest(verified_anchors),
            "investigation_mission_sha256": stable_digest(candidate_mission),
            "subject_anchor_verification_sha256": stable_digest(subject_anchor_verification),
            "pre_search_provider_repair_call_count": 0,
            "boundary": "investigator_exact_span_to_unified_search",
        },
    }, []


def _initial_failure(
    *,
    source_investigation: Mapping[str, Any],
    graph_snapshot: Mapping[str, Any],
    brain_revision: str,
    budget: GrowInvestigationBudget,
    correlation_id: str,
    parent_operation_id: str,
    code: str,
    detail: str,
) -> dict[str, Any]:
    source_id = str(source_investigation.get("investigation_id") or "").strip()
    return {
        "schema_version": GROW_INVESTIGATION_SCHEMA_VERSION,
        "investigation_id": source_id,
        "source_investigation_id": source_id,
        "brain_id": source_investigation.get("brain_id"),
        "brain_revision": brain_revision,
        "source_sha256": _source_sha256(source_investigation),
        "graph_snapshot_sha256": _grow_graph_snapshot_sha256(graph_snapshot),
        "version": 1,
        "state": "investigating",
        "status": "INCOMPLETE",
        "claim_ledger": [],
        "exclusions": [],
        "search_receipts": [],
        "document_evidence_receipts": [],
        "hydrated_evidence": {},
        "decisions": [],
        "questions": [],
        "pending_questions": [],
        "compiler_claims": [],
        "compiler_authority": {},
        "ai_execution_ledger": [],
        "ai_execution_attestation": aggregate_execution_ledger([], complete=False, applicable=False),
        "semantic_decision_source": "provider",
        "query_authority": GROW_QUERY_AUTHORITY,
        "semantic_fallback_used": False,
        "usage": {},
        "budget": budget.as_dict(),
        "budget_usage": {},
        "complete": False,
        "applicable": False,
        "failure": {"code": code, "detail": detail[:1000]},
        "correlation_id": correlation_id,
        "parent_operation_id": parent_operation_id,
    }


def _validate_source_units(source_investigation: Mapping[str, Any]) -> str | None:
    seen: set[str] = set()
    for unit in _source_units(source_investigation):
        unit_id = str(unit.get("unit_id") or "").strip()
        raw_text = str(unit.get("raw_text") or "")
        if not unit_id or unit_id in seen:
            return "source_unit_id_invalid"
        seen.add(unit_id)
        if bool(unit.get("fact_eligible", True)) and not raw_text:
            return f"fact_eligible_source_unit_empty:{unit_id}"
        upstream = _canonical_upstream_digest(
            unit.get("content_digest")
            or _dict(unit.get("provenance")).get("hash")
        )
        if upstream and upstream != _content_sha256(raw_text):
            return f"source_unit_content_sha256_mismatch:{unit_id}"
    return None


def _bind_initial_claims(
    payload: Mapping[str, Any],
    *,
    source_investigation: Mapping[str, Any],
    max_claims: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str], str | None]:
    units = {str(item.get("unit_id") or ""): item for item in _source_units(source_investigation)}
    raw_claims = _dicts(payload.get("claims"), limit=max_claims + 1)
    exclusions = _dicts(payload.get("exclusions"), limit=max_claims + 1)
    if len(raw_claims) > max_claims:
        return [], [], {}, "claim_budget_exhausted"
    fact_unit_ids = {
        unit_id for unit_id, unit in units.items() if bool(unit.get("fact_eligible", True))
    }
    represented_ids: set[str] = set()
    claim_key_map: dict[str, str] = {}
    seen_claim_keys: set[str] = set()
    binding_issues: list[dict[str, Any]] = []

    def exclude_unbound_claim(
        *,
        claim_key: str,
        unit_id: str,
        issue_code: str,
        error_prefix: str,
        source_claim_summary: str,
        occurrence_count: int,
    ) -> None:
        summary = (
            "This proposed claim was not used because it could not be tied "
            "to one exact passage in the canonical source."
        )
        binding_issues.append(
            {
                "source_unit_id": unit_id,
                "claim_key": claim_key,
                "issue_code": issue_code,
                "error_prefix": error_prefix,
                "occurrence_count": occurrence_count,
                "reason": summary,
                "authored_by": "server_exact_source_binding",
                "binding_state": "binding_excluded",
                "human_finding": {
                    "schema_version": "agvm.grow_source_binding_finding.v1",
                    "finding_type": "claim_excluded_unbound_source_span",
                    "summary": summary,
                    "source_claim_summary": source_claim_summary,
                    "reason": (
                        "Grow requires a byte-exact, uniquely located source quote "
                        "and subject identifier before Search or compilation."
                    ),
                    "impact": (
                        "The claim was excluded from Search, questions, decisions, "
                        "preview, and apply; other safely bound claims may continue."
                    ),
                    "next_step": (
                        "Provide or extract a more specific exact source passage if "
                        "this claim should be investigated later."
                    ),
                    "claim_key": claim_key,
                    "source_unit_id": unit_id,
                    "issue_code": issue_code,
                },
            }
        )
        represented_ids.add(unit_id)

    ledger: list[dict[str, Any]] = []
    for raw_claim in raw_claims:
        if str(raw_claim.get("claim_id") or "").strip():
            return [], [], {}, "initial_claim_id_must_be_server_bound"
        claim_key = str(raw_claim.get("claim_key") or "").strip()
        unit_id = str(raw_claim.get("source_unit_id") or "").strip()
        unit = units.get(unit_id)
        if not claim_key or claim_key in seen_claim_keys or unit is None:
            return [], [], {}, "claim_key_or_source_unit_invalid"
        seen_claim_keys.add(claim_key)
        raw_text = str(unit.get("raw_text") or "")
        exact_quote = str(raw_claim.get("exact_quote") or "")
        source_claim_summary = str(raw_claim.get("natural_language_claim") or "").strip()
        try:
            quote_start = int(raw_claim.get("quote_start"))
            quote_end = int(raw_claim.get("quote_end"))
        except (TypeError, ValueError):
            quote_start = -1
            quote_end = -1
        offsets_are_source_bound = (
            quote_start >= 0
            and quote_end > quote_start
            and quote_end <= len(raw_text)
            and raw_text[quote_start:quote_end] == exact_quote
        )
        if not offsets_are_source_bound:
            # The model may count Unicode/code points or include surrounding
            # punctuation differently. Source authority remains server-side:
            # repair offsets only when the exact provider quote occurs once in
            # the canonical raw source. In-range offsets are not authority when
            # they point at different bytes; accepting them would silently bind
            # an unrelated fragment. Missing or ambiguous quotes fail closed.
            first_match = raw_text.find(exact_quote) if exact_quote else -1
            second_match = raw_text.find(exact_quote, first_match + 1) if first_match >= 0 else -1
            if first_match < 0:
                exclude_unbound_claim(
                    claim_key=claim_key,
                    unit_id=unit_id,
                    issue_code="quote_missing",
                    error_prefix="claim_source_span_invalid",
                    source_claim_summary=source_claim_summary,
                    occurrence_count=0,
                )
                continue
            if second_match >= 0:
                exclude_unbound_claim(
                    claim_key=claim_key,
                    unit_id=unit_id,
                    issue_code="quote_ambiguous",
                    error_prefix="claim_source_span_invalid",
                    source_claim_summary=source_claim_summary,
                    occurrence_count=raw_text.count(exact_quote),
                )
                continue
            quote_start = first_match
            quote_end = first_match + len(exact_quote)
        neutral_subject_anchor = str(raw_claim.get("neutral_subject_anchor") or "")
        try:
            subject_anchor_start = int(raw_claim.get("subject_anchor_start"))
            subject_anchor_end = int(raw_claim.get("subject_anchor_end"))
        except (TypeError, ValueError):
            subject_anchor_start = -1
            subject_anchor_end = -1
        anchor_is_server_bound = bool(
            quote_start <= subject_anchor_start < subject_anchor_end <= quote_end
            and subject_anchor_end <= len(raw_text)
            and raw_text[subject_anchor_start:subject_anchor_end]
            == neutral_subject_anchor
        )
        if not anchor_is_server_bound:
            # A subject identifier is semantic provider output. Never replace it
            # with arbitrary bytes merely because its numeric offsets are in
            # bounds. Relocate only an exact, unique occurrence inside the now
            # server-bound claim quote; otherwise require the existing AI repair.
            relative_match = exact_quote.find(neutral_subject_anchor) if neutral_subject_anchor else -1
            second_relative_match = (
                exact_quote.find(neutral_subject_anchor, relative_match + 1)
                if relative_match >= 0
                else -1
            )
            if relative_match >= 0 and second_relative_match < 0:
                subject_anchor_start = quote_start + relative_match
                subject_anchor_end = subject_anchor_start + len(neutral_subject_anchor)
                anchor_is_server_bound = True
            if not anchor_is_server_bound:
                # Unlike a missing or ambiguous quote, a subject-anchor error
                # happens after the assertion span is already source-bound.
                # Silently excluding that recoverable claim lets multi-claim
                # sources lose autonomous service/capability/schedule/staffing
                # assertions while another claim from the same unit advances to
                # Search.  Fail this turn into the existing AI repair path so
                # the provider re-authors the exact subject noun phrase and the
                # server still performs only byte-exact span validation.
                return [], [], {}, f"claim_subject_anchor_invalid:{claim_key}"
        source_unit_content_sha256 = _content_sha256(raw_text)
        supplied_source_sha256 = _canonical_upstream_digest(raw_claim.get("source_unit_content_sha256"))
        if supplied_source_sha256 and supplied_source_sha256 != source_unit_content_sha256:
            return [], [], {}, f"claim_source_hash_mismatch:{claim_key}"
        natural_language_claim = source_claim_summary
        subject_hypotheses = _strings(raw_claim.get("subject_hypotheses"), limit=12)
        epistemic_posture = str(raw_claim.get("epistemic_posture") or "asserted_fact")
        investigation_need = str(raw_claim.get("investigation_need") or "").strip()
        claim_identity = {
            "unit": unit_id,
            "sha": source_unit_content_sha256,
            "start": quote_start,
            "end": quote_end,
            "quote": exact_quote,
            "claim_key": claim_key,
            "natural_language_claim": natural_language_claim,
            "investigation_need": investigation_need,
            "epistemic_posture": epistemic_posture,
        }
        claim_id = f"grow-claim::{stable_digest(claim_identity)[:24]}"
        temporal_scope, temporal_error = _normalize_temporal_scope(
            raw_claim.get("temporal_scope"),
            claim={
                "claim_id": claim_id,
                "source_unit_id": unit_id,
                "quote_start": quote_start,
                "quote_end": quote_end,
            },
            source_investigation=source_investigation,
        )
        if temporal_error:
            return [], [], {}, f"{temporal_error}:{claim_key}"
        claim_key_map[claim_key] = claim_id
        represented_ids.add(unit_id)
        ledger.append(
            {
                "schema_version": GROW_CLAIM_LEDGER_SCHEMA_VERSION,
                "claim_id": claim_id,
                "claim_key": claim_key,
                "parent_claim_id": None,
                "basis_kind": "source_span",
                "basis_ref": unit_id,
                "basis_content_sha256": source_unit_content_sha256,
                "source_unit_id": unit_id,
                "source_unit_content_sha256": source_unit_content_sha256,
                **_source_temporal_provenance(unit),
                **_source_attribution(source_investigation, unit),
                "quote_start": quote_start,
                "quote_end": quote_end,
                "exact_quote": exact_quote,
                "subject_anchor_start": subject_anchor_start,
                "subject_anchor_end": subject_anchor_end,
                "neutral_subject_anchor": neutral_subject_anchor,
                "natural_language_claim": natural_language_claim,
                "subject_hypotheses": subject_hypotheses,
                "temporal_scope": temporal_scope,
                "epistemic_posture": epistemic_posture,
                "investigation_need": investigation_need,
                "status": "SEARCHING",
                "search_receipt_ids": [],
                "decision_id": None,
            }
        )
    normalized_exclusions: list[dict[str, Any]] = list(binding_issues)
    for exclusion in exclusions:
        unit_id = str(exclusion.get("source_unit_id") or "").strip()
        reason = str(exclusion.get("reason") or "").strip()
        if unit_id not in fact_unit_ids or not reason:
            return [], [], {}, "claim_exclusion_invalid"
        represented_ids.add(unit_id)
        normalized_exclusions.append({"source_unit_id": unit_id, "reason": reason, "authored_by": "provider"})
    missing = sorted(fact_unit_ids - represented_ids)
    if missing:
        return [], [], {}, f"incomplete_claim_coverage:{','.join(missing[:12])}"
    if not ledger and binding_issues:
        first_issue = binding_issues[0]
        return (
            [],
            [],
            {},
            f"{first_issue['error_prefix']}:{first_issue['claim_key']}",
        )
    if not ledger and fact_unit_ids:
        return [], [], {}, "claim_ledger_empty"
    return ledger, normalized_exclusions, claim_key_map, None


def _bind_clarification_answer_claims(
    raw_claims_value: Any,
    *,
    claims: Sequence[Mapping[str, Any]],
    questions: Sequence[Mapping[str, Any]],
    source_investigation: Mapping[str, Any],
    hydrated_evidence: Mapping[str, Any],
    search_receipts: Sequence[Mapping[str, Any]],
    document_receipts: Sequence[Mapping[str, Any]],
    max_claims: int,
) -> tuple[list[dict[str, Any]], dict[str, str], str | None]:
    raw_claims = _dicts(raw_claims_value, limit=max_claims + 1)
    if len(raw_claims) != len(list(raw_claims_value or [])):
        return [], {}, "clarification_claims_invalid"
    if len(claims) + len(raw_claims) > max_claims:
        return [], {}, "claim_budget_exhausted"
    claims_by_id = {str(item.get("claim_id") or ""): dict(item) for item in claims}
    questions_by_id = {str(item.get("question_id") or ""): dict(item) for item in questions}
    existing_keys = {str(item.get("claim_key") or "") for item in claims}
    additions: list[dict[str, Any]] = []
    key_map: dict[str, str] = {}
    valid_postures = {"asserted_fact", "reported_claim", "opinion", "hypothesis", "instruction"}
    for raw in raw_claims:
        claim_key = str(raw.get("claim_key") or "").strip()
        parent_claim_id = str(raw.get("parent_claim_id") or "").strip()
        question_id = str(raw.get("basis_ref") or "").strip()
        question = questions_by_id.get(question_id)
        parent = claims_by_id.get(parent_claim_id)
        if (
            not claim_key
            or claim_key in existing_keys
            or claim_key in key_map
            or str(raw.get("basis_kind") or "") != "clarified_answer"
            or parent is None
            or question is None
            or str(question.get("answer_state") or "") != "answered"
            or parent_claim_id not in _strings(question.get("affected_claim_ids"), limit=128)
        ):
            return [], {}, "clarification_claim_binding_invalid"
        if str(raw.get("basis_content_sha256") or "").strip():
            return [], {}, "clarification_claim_digest_must_be_server_bound"
        answer_text = _temporal_basis_text(question.get("answer"))
        answer_sha256 = _content_sha256(answer_text)
        exact_quote = str(raw.get("exact_quote") or "")
        try:
            quote_start = int(raw.get("quote_start"))
            quote_end = int(raw.get("quote_end"))
        except (TypeError, ValueError):
            quote_start = quote_end = -1
        if not (exact_quote and 0 <= quote_start < quote_end <= len(answer_text) and answer_text[quote_start:quote_end] == exact_quote):
            first = answer_text.find(exact_quote) if exact_quote else -1
            second = answer_text.find(exact_quote, first + 1) if first >= 0 else -1
            if first < 0 or second >= 0:
                return [], {}, f"clarification_claim_answer_span_invalid:{claim_key}"
            quote_start, quote_end = first, first + len(exact_quote)
        anchor = str(raw.get("neutral_subject_anchor") or "")
        try:
            anchor_start = int(raw.get("subject_anchor_start"))
            anchor_end = int(raw.get("subject_anchor_end"))
        except (TypeError, ValueError):
            anchor_start = anchor_end = -1
        if not (
            anchor
            and quote_start <= anchor_start < anchor_end <= quote_end
            and answer_text[anchor_start:anchor_end] == anchor
        ):
            return [], {}, f"clarification_claim_subject_anchor_invalid:{claim_key}"
        natural_language_claim = str(raw.get("natural_language_claim") or "").strip()
        investigation_need = str(raw.get("investigation_need") or "").strip()
        posture = str(raw.get("epistemic_posture") or "").strip()
        if not natural_language_claim or not investigation_need or posture not in valid_postures:
            return [], {}, f"clarification_claim_annotation_invalid:{claim_key}"
        claim_id = f"grow-claim-answer::{stable_digest({'parent': parent_claim_id, 'question': question_id, 'answer': answer_sha256, 'start': quote_start, 'end': quote_end, 'quote': exact_quote})[:24]}"
        temporal_scope, temporal_error = _normalize_temporal_scope(
            raw.get("temporal_scope"),
            claim={
                "claim_id": claim_id,
                "source_unit_id": parent.get("source_unit_id"),
                "quote_start": parent.get("quote_start"),
                "quote_end": parent.get("quote_end"),
            },
            source_investigation=source_investigation,
            questions=questions,
            hydrated_evidence=hydrated_evidence,
            allowed_hydrated_evidence_refs={
                str(node_id)
                for receipt in [*search_receipts, *document_receipts]
                if parent_claim_id in _strings(receipt.get("affected_claim_ids"), limit=128)
                and _receipt_evidence_usable(receipt)
                for node_id in list(receipt.get("evidence_node_ids") or [])
            },
        )
        if temporal_error:
            return [], {}, f"clarification_claim_{temporal_error}:{claim_key}"
        addition = {
            "schema_version": GROW_CLAIM_LEDGER_SCHEMA_VERSION,
            "claim_id": claim_id,
            "claim_key": claim_key,
            "parent_claim_id": parent_claim_id,
            "basis_kind": "clarified_answer",
            "basis_ref": question_id,
            "basis_content_sha256": answer_sha256,
            "source_unit_id": parent.get("source_unit_id"),
            "source_unit_content_sha256": parent.get("source_unit_content_sha256"),
            "source_published_at": parent.get("source_published_at"),
            "source_acquired_at": parent.get("source_acquired_at"),
            "source_retrieved_at": parent.get("source_retrieved_at"),
            "quote_start": quote_start,
            "quote_end": quote_end,
            "exact_quote": exact_quote,
            "subject_anchor_start": anchor_start,
            "subject_anchor_end": anchor_end,
            "neutral_subject_anchor": anchor,
            "natural_language_claim": natural_language_claim,
            "subject_hypotheses": _strings(raw.get("subject_hypotheses"), limit=12),
            "temporal_scope": temporal_scope,
            "epistemic_posture": posture,
            "investigation_need": investigation_need,
            "status": "SEARCHING",
            "search_receipt_ids": [],
            "decision_id": None,
        }
        additions.append(addition)
        key_map[claim_key] = claim_id
    return additions, key_map, None


def _merge_review_claims(
    raw_claims_value: Any,
    *,
    claims: Sequence[Mapping[str, Any]],
    max_claims: int,
    source_investigation: Mapping[str, Any],
    questions: Sequence[Mapping[str, Any]],
    hydrated_evidence: Mapping[str, Any],
    search_receipts: Sequence[Mapping[str, Any]],
    document_receipts: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Merge provider-authored annotations without yielding server claim authority.

    Models commonly restate the structured `claims` array during AI_REVIEW.  A
    restatement is legal only when it is a one-for-one representation of the
    server ledger.  Source identity, span, content hash, workflow status,
    receipts and decision bindings always remain server-owned.
    """

    raw_claims = _dicts(raw_claims_value, limit=max_claims + 1)
    canonical = [dict(item) for item in claims]
    if not raw_claims:
        return canonical, None
    if len(raw_claims) != len(canonical) or len(raw_claims) > max_claims:
        return None, "review_claim_ledger_add_or_drop_forbidden"

    by_id = {str(item.get("claim_id") or ""): item for item in canonical}
    by_key = {str(item.get("claim_key") or ""): item for item in canonical}
    updates: dict[str, dict[str, Any]] = {}
    valid_postures = {"asserted_fact", "reported_claim", "opinion", "hypothesis", "instruction"}

    for raw in raw_claims:
        raw_id = str(raw.get("claim_id") or "").strip()
        raw_key = str(raw.get("claim_key") or "").strip()
        matched_by_id = by_id.get(raw_id) if raw_id else None
        matched_by_key = by_key.get(raw_key) if raw_key else None
        if matched_by_id is not None and matched_by_key is not None and matched_by_id is not matched_by_key:
            return None, "review_claim_id_key_rebind_forbidden"
        bound = matched_by_id or matched_by_key
        if bound is None:
            return None, "review_claim_unknown_or_rebound"
        claim_id = str(bound.get("claim_id") or "")
        if claim_id in updates:
            return None, "review_claim_duplicate_binding"
        if raw_id and raw_id != claim_id:
            return None, "review_claim_id_rebind_forbidden"
        if raw_key and raw_key != str(bound.get("claim_key") or ""):
            return None, "review_claim_key_rebind_forbidden"

        immutable_fields = (
            "parent_claim_id",
            "basis_kind",
            "basis_ref",
            "basis_content_sha256",
            "source_unit_id",
            "source_published_at",
            "source_acquired_at",
            "source_retrieved_at",
            "quote_start",
            "quote_end",
            "exact_quote",
            "subject_anchor_start",
            "subject_anchor_end",
            "neutral_subject_anchor",
        )
        for field in immutable_fields:
            if field in raw and raw.get(field) != bound.get(field):
                return None, f"review_claim_source_binding_forbidden:{claim_id}:{field}"
        supplied_sha256 = _canonical_upstream_digest(raw.get("source_unit_content_sha256"))
        if supplied_sha256 and supplied_sha256 != str(bound.get("source_unit_content_sha256") or ""):
            return None, f"review_claim_source_binding_forbidden:{claim_id}:source_unit_content_sha256"

        natural_language_claim = str(raw.get("natural_language_claim") or "").strip()
        investigation_need = str(raw.get("investigation_need") or "").strip()
        epistemic_posture = str(raw.get("epistemic_posture") or "").strip()
        if not natural_language_claim or not investigation_need or epistemic_posture not in valid_postures:
            return None, f"review_claim_annotation_invalid:{claim_id}"
        allowed_brain_evidence_refs = {
            str(node_id)
            for receipt in [*search_receipts, *document_receipts]
            if claim_id in _strings(receipt.get("affected_claim_ids"), limit=128)
            and _receipt_evidence_usable(receipt)
            for node_id in list(receipt.get("evidence_node_ids") or [])
        }
        temporal_scope, temporal_error = _normalize_temporal_scope(
            raw.get("temporal_scope"),
            claim=bound,
            source_investigation=source_investigation,
            questions=questions,
            hydrated_evidence=hydrated_evidence,
            allowed_hydrated_evidence_refs=allowed_brain_evidence_refs,
        )
        if temporal_error:
            return None, f"review_claim_{temporal_error}:{claim_id}"
        updated = dict(bound)
        updated.update(
            {
                "natural_language_claim": natural_language_claim,
                "subject_hypotheses": _strings(raw.get("subject_hypotheses"), limit=12),
                "temporal_scope": temporal_scope,
                "epistemic_posture": epistemic_posture,
                "investigation_need": investigation_need,
            }
        )
        updates[claim_id] = updated

    if set(updates) != set(by_id):
        return None, "review_claim_ledger_add_or_drop_forbidden"
    return [updates[str(item.get("claim_id") or "")] for item in canonical], None


def _validate_review_exclusions(
    raw_exclusions_value: Any,
    *,
    exclusions: Sequence[Mapping[str, Any]],
    max_claims: int,
) -> str | None:
    raw_exclusions = _dicts(raw_exclusions_value, limit=max_claims + 1)
    if not raw_exclusions:
        return None
    expected = {
        (str(item.get("source_unit_id") or ""), str(item.get("reason") or ""))
        for item in exclusions
    }
    supplied = {
        (str(item.get("source_unit_id") or ""), str(item.get("reason") or ""))
        for item in raw_exclusions
    }
    if len(raw_exclusions) != len(exclusions) or supplied != expected:
        return "review_exclusions_replace_forbidden"
    return None


def _default_hydrate(node_ids: list[str], graph_snapshot: Mapping[str, Any]) -> dict[str, Any]:
    nodes_by_id = {
        str(node.get("id") or ""): node
        for node in _dicts(graph_snapshot.get("nodes"), limit=1_000_000)
        if str(node.get("id") or "").strip()
    }
    hydrated: list[dict[str, Any]] = []
    missing: list[str] = []
    for node_id in node_ids:
        node = nodes_by_id.get(node_id)
        if node is None:
            missing.append(node_id)
            continue
        material = {
            "node_id": node_id,
            "canonical_text": str(node.get("raw_text") or node.get("summary") or "")[:8_000],
            "summary": str(node.get("summary") or "")[:2_000],
            "claim_status": str(node.get("claim_status") or "fact"),
            "source_trust": str(node.get("source_trust") or _dict(node.get("provenance")).get("source_trust") or "unknown"),
            "created_at": node.get("created_at") or _dict(node.get("provenance")).get("created_at"),
            "updated_at": node.get("updated_at") or _dict(node.get("provenance")).get("updated_at"),
            "ingested_at": node.get("ingested_at") or _dict(node.get("provenance")).get("ingested_at"),
            "node_revision": node.get("node_revision") or _dict(node.get("provenance")).get("node_revision"),
            "temporal_role": node.get("temporal_role"),
            "observed_at": node.get("observed_at"),
            "valid_from": node.get("valid_from"),
            "valid_to": node.get("valid_to"),
            "lifecycle_status": str(node.get("lifecycle_status") or "active"),
            "superseded_at": node.get("superseded_at"),
            "superseded_by": node.get("superseded_by"),
            "memory_type": str(node.get("memory_type") or ""),
            "provenance": _dict(node.get("provenance")),
        }
        material["digest"] = stable_digest(material)
        hydrated.append(material)
    return {"nodes": hydrated, "missing_node_ids": missing}


def _compiler_claims(
    claims: Sequence[Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    decisions_by_claim = {str(item.get("claim_id") or ""): dict(item) for item in decisions}
    result: list[dict[str, Any]] = []
    for claim_value in claims:
        claim = dict(claim_value)
        temporal_scope = _dict(claim.get("temporal_scope"))
        decision = decisions_by_claim.get(str(claim.get("claim_id") or ""))
        if not decision or str(decision.get("decision") or "") == "defer":
            continue
        result.append(
            {
                "claim_id": claim.get("claim_id"),
                "decision_id": decision.get("decision_id"),
                "decision": decision.get("decision"),
                "target_node_ids": list(decision.get("target_node_ids") or []),
                "evidence_receipt_ids": list(decision.get("evidence_receipt_ids") or []),
                "parent_claim_id": claim.get("parent_claim_id"),
                "basis_kind": claim.get("basis_kind"),
                "basis_ref": claim.get("basis_ref"),
                "basis_content_sha256": claim.get("basis_content_sha256"),
                "source_unit_id": claim.get("source_unit_id"),
                "source_unit_content_sha256": claim.get("source_unit_content_sha256"),
                "source_published_at": claim.get("source_published_at"),
                "source_acquired_at": claim.get("source_acquired_at"),
                "source_retrieved_at": claim.get("source_retrieved_at"),
                "source_uri": claim.get("source_uri"),
                "source_trust": claim.get("source_trust"),
                "quote_start": claim.get("quote_start"),
                "quote_end": claim.get("quote_end"),
                "exact_quote": claim.get("exact_quote"),
                "natural_language_claim": claim.get("natural_language_claim"),
                "temporal_scope": claim.get("temporal_scope"),
                "temporal_role": temporal_scope.get("temporal_role"),
                "observed_at": temporal_scope.get("observed_at"),
                "valid_from": temporal_scope.get("valid_from"),
                "valid_to": temporal_scope.get("valid_to"),
                "compiler_policy": "wording_routing_facets_geometry_only",
            }
        )
    return result


def run_grow_investigation(
    source_investigation: Mapping[str, Any],
    graph_snapshot: Mapping[str, Any],
    brain_revision: str,
    *,
    provider: Provider | None,
    investigation: Mapping[str, Any] | None = None,
    clarification_answers: Mapping[str, Any] | None = None,
    search_runner: InvestigativeSearchRunner = run_investigative_search,
    hydrate_runner: HydrateRunner | None = None,
    document_hydrate_runner: DocumentHydrateRunner = hydrate_investigative_document_evidence,
    budget: GrowInvestigationBudget | None = None,
    correlation_id: str | None = None,
    parent_operation_id: str | None = None,
    semantic_authority: Mapping[str, Any] | None = None,
    question_limit: int = 12,
    monotonic_clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Run or resume the server-authoritative Grow investigation ledger."""

    resolved_budget = budget or GrowInvestigationBudget.from_env()
    if resolved_budget.max_repairs > 2:
        resolved_budget = GrowInvestigationBudget(
            **{**resolved_budget.__dict__, "max_repairs": 2}
        )
    clock = monotonic_clock or time.monotonic
    started_monotonic = float(clock())
    deadline = _GrowDeadline(
        started_monotonic=started_monotonic,
        deadline_monotonic=(
            started_monotonic + max(0.001, float(resolved_budget.wall_budget_seconds))
        ),
        provider_timeout_seconds=max(
            0.001, float(resolved_budget.provider_timeout_seconds)
        ),
        monotonic_clock=clock,
    )
    resolved_question_limit = max(1, min(int(question_limit), 24))
    normalized_revision = str(brain_revision or "").strip()
    resolved_brain_id = str(
        source_investigation.get("brain_id")
        or _dict(investigation).get("brain_id")
        or ""
    ).strip()
    source_id = str(source_investigation.get("investigation_id") or "").strip()
    existing = _dict(investigation)
    investigation_id = str(existing.get("investigation_id") or source_id).strip()
    resolved_correlation_id = str(correlation_id or existing.get("correlation_id") or investigation_id).strip()
    resolved_parent_id = str(parent_operation_id or existing.get("parent_operation_id") or resolved_correlation_id).strip()

    def fail(code: str, detail: str) -> dict[str, Any]:
        return _initial_failure(
            source_investigation=source_investigation,
            graph_snapshot=graph_snapshot,
            brain_revision=normalized_revision,
            budget=resolved_budget,
            correlation_id=resolved_correlation_id,
            parent_operation_id=resolved_parent_id,
            code=code,
            detail=detail,
        )

    if not source_id or not normalized_revision or not investigation_id:
        return fail("grow_investigation_binding_invalid", "source investigation id and brain revision are required")
    source_error = _validate_source_units(source_investigation)
    if source_error:
        return fail("grow_source_contract_invalid", source_error)
    fact_units = [unit for unit in _source_units(source_investigation) if bool(unit.get("fact_eligible", True))]
    if not fact_units:
        return fail("grow_source_contract_invalid", "no_fact_eligible_source_units")
    if len(fact_units) > resolved_budget.max_claims:
        return fail("claim_budget_exhausted", f"fact_eligible_units={len(fact_units)}")
    fact_units_by_id = {
        str(unit.get("unit_id") or "").strip(): unit
        for unit in fact_units
        if str(unit.get("unit_id") or "").strip()
    }
    current_source_sha256 = _source_sha256(source_investigation)
    current_graph_sha256 = _grow_graph_snapshot_sha256(graph_snapshot)
    if existing:
        if str(existing.get("schema_version") or "") != GROW_INVESTIGATION_SCHEMA_VERSION:
            return fail("grow_investigation_schema_invalid", str(existing.get("schema_version") or ""))
        if str(existing.get("source_investigation_id") or "") != source_id:
            return fail("grow_investigation_source_binding_mismatch", source_id)
        if str(existing.get("brain_id") or "") != str(source_investigation.get("brain_id") or ""):
            return fail("grow_investigation_brain_binding_mismatch", str(source_investigation.get("brain_id") or ""))
        if str(existing.get("brain_revision") or "") != normalized_revision:
            return fail("grow_investigation_brain_revision_stale", normalized_revision)
        if str(existing.get("source_sha256") or "") != current_source_sha256:
            return fail("grow_investigation_source_hash_mismatch", current_source_sha256)
        if (
            existing.get("graph_snapshot_sha256")
            and str(existing.get("graph_snapshot_sha256") or "") != current_graph_sha256
        ):
            return fail("grow_investigation_graph_snapshot_mismatch", current_graph_sha256)

    authority = {
        "semantic_decision_source": "provider",
        "query_authority": GROW_QUERY_AUTHORITY,
        "semantic_fallback_used": False,
        **_dict(semantic_authority or existing.get("semantic_authority")),
    }
    if (
        str(authority.get("semantic_decision_source") or "") != "provider"
        or str(authority.get("query_authority") or "") != GROW_QUERY_AUTHORITY
        or authority.get("semantic_fallback_used") is not False
    ):
        return fail("grow_semantic_authority_invalid", json.dumps(authority, sort_keys=True))

    grow_receipt_authority_sha256 = stable_digest(
        {
            "investigation_id": investigation_id,
            "source_investigation_id": source_id,
            "source_sha256": current_source_sha256,
            "brain_id": resolved_brain_id or None,
            "brain_revision": normalized_revision,
            "semantic_authority": authority,
        }
    )

    claims = _dicts(existing.get("claim_ledger"), limit=resolved_budget.max_claims)
    for claim in claims:
        unit = fact_units_by_id.get(str(claim.get("source_unit_id") or ""))
        if unit is None:
            continue
        for key, value in _source_attribution(source_investigation, unit).items():
            if value and not str(claim.get(key) or "").strip():
                claim[key] = value
    exclusions = _dicts(existing.get("exclusions"), limit=resolved_budget.max_claims)
    receipts = _dicts(existing.get("search_receipts"), limit=resolved_budget.max_search_calls)
    for receipt in receipts:
        receipt_brain_id = str(receipt.get("brain_id") or "").strip()
        if resolved_brain_id and receipt_brain_id and receipt_brain_id != resolved_brain_id:
            return fail("grow_search_receipt_brain_binding_mismatch", receipt_brain_id)
        if resolved_brain_id and not receipt_brain_id:
            receipt["brain_id"] = resolved_brain_id
    document_receipts = _dicts(
        existing.get("document_evidence_receipts"),
        limit=resolved_budget.max_tool_calls,
    )
    decisions = _dicts(existing.get("decisions"), limit=resolved_budget.max_claims)
    questions = _dicts(existing.get("questions"), limit=24)
    hydrated_evidence = _dict(existing.get("hydrated_evidence"))
    prior_execution_ledger = _dicts(existing.get("ai_execution_ledger"), limit=256)
    run_call_authority_sha256 = stable_digest(
        {
            "investigation_id": investigation_id,
            "investigation_version": int(existing.get("version") or 0),
            "brain_id": resolved_brain_id or None,
            "brain_revision": normalized_revision,
            "prior_execution_ledger_sha256": stable_digest(prior_execution_ledger),
            "prior_search_receipt_ids": [
                str(item.get("receipt_id") or "") for item in receipts
            ],
            "prior_document_receipt_ids": [
                str(item.get("receipt_id") or "") for item in document_receipts
            ],
        }
    )
    answers = _dict(clarification_answers)
    question_ids = {str(item.get("question_id") or "") for item in questions}
    unknown_answer_ids = sorted(set(answers) - question_ids)
    if unknown_answer_ids:
        return fail("grow_clarification_answer_invalid", f"unknown_question_ids={','.join(unknown_answer_ids)}")
    for question in questions:
        question_id = str(question.get("question_id") or "")
        if question_id not in answers:
            continue
        answer_value = answers[question_id]
        deferred = bool(
            isinstance(answer_value, Mapping)
            and str(answer_value.get("state") or "").strip().casefold() == "deferred"
        )
        question["answer"] = None if deferred else answer_value
        question["answer_state"] = "deferred" if deferred else "answered"
        question["answered_at"] = utc_now()
        affected = set(_strings(question.get("affected_claim_ids"), limit=resolved_budget.max_claims))
        if not deferred:
            decisions = [item for item in decisions if str(item.get("claim_id") or "") not in affected]
            for claim in claims:
                if str(claim.get("claim_id") or "") in affected:
                    # A human answer invalidates the old decision, not the
                    # evidence notebook.  Search authority can only come from
                    # a server-bound clarification answer claim that still
                    # needs verification.
                    claim["status"] = "AI_REVIEW"
                    claim["decision_id"] = None
        else:
            for claim in claims:
                if str(claim.get("claim_id") or "") in affected:
                    claim["status"] = "AI_REVIEW"

    pending_before = [item for item in questions if str(item.get("answer_state") or "unanswered") == "unanswered"]
    if existing and pending_before and not answers:
        preserved = dict(existing)
        preserved["pending_questions"] = pending_before
        preserved["questions"] = questions
        preserved["state"] = "awaiting_clarification"
        preserved["status"] = "NEEDS_CLARIFICATION"
        preserved["complete"] = False
        preserved["applicable"] = False
        return preserved

    search_call_count = len(receipts)
    search_wave = max([int(item.get("wave") or 0) for item in receipts] or [0])
    prior_hydration_waves = [
        int(item.get("wave") or 0)
        for item in prior_execution_ledger
        if str(item.get("role") or "") in {"hydration", "document_hydration"}
    ]
    try:
        hydration_wave = int(existing.get("hydration_wave") or max(prior_hydration_waves or [0]))
    except (TypeError, ValueError):
        hydration_wave = max(prior_hydration_waves or [0])
    search_state_lock = Lock()
    query_claim_signatures = {
        stable_digest(
            {
                "query": str(item.get("query_text") or "").strip().casefold(),
                "claim_ids": sorted(_strings(item.get("affected_claim_ids"), limit=128)),
            }
        )
        for item in receipts
    }
    new_execution_ledger: list[dict[str, Any]] = []
    inflight_query_claim_signatures: set[str] = set()
    reserved_search_calls = 0
    search_batch_generation = 0
    claims_by_id = {str(item.get("claim_id") or ""): item for item in claims}
    receipts_by_id = {str(item.get("receipt_id") or ""): item for item in receipts}
    document_receipts_by_id = {
        str(item.get("receipt_id") or ""): item for item in document_receipts
    }
    receipts_by_idempotency = {
        str(item.get("idempotency_key") or ""): item
        for item in receipts
        if str(item.get("idempotency_key") or "")
    }
    hydration_review_state_keys: set[str] = set(
        _strings(existing.get("hydration_review_state_keys"), limit=128)
    )
    normalized_decisions: list[dict[str, Any]] = list(decisions)
    normalized_questions: list[dict[str, Any]] = list(questions)
    latest_turn_decisions: list[dict[str, Any]] = []
    latest_turn_questions: list[dict[str, Any]] = []
    repairing_missing_first_search = False
    repairing_empty_continue = False
    repairing_invalid_claim_reference = False
    repairing_review_source_binding = False
    repairing_initial_source_binding = False
    repairing_unanswered_clarification_claim = False
    repairing_forbidden_review_search = False
    repairing_search_call_budget = False
    repairing_document_hydration_binding = False
    repairing_decision_target_hydration = False
    repairing_unusable_decision_receipt = False
    repairing_question_reemitted = False
    repairing_closed_clarification_gap_reopened = False
    closed_clarification_gap_reopened_claim_ids: list[str] = []
    repairing_question_evidence_refs = False
    decision_receipt_repair_requires_questions = False
    question_only_action_repair_uses_narrow_schema = False
    decision_target_hydration_claim_ids: list[str] = []
    decision_target_hydration_target_ids: list[str] = []
    pre_search_provider_repair_calls = 0

    def current_review_state_key() -> str:
        answered_questions = [
            {
                "question_id": str(item.get("question_id") or ""),
                "answer_state": str(item.get("answer_state") or "unanswered"),
                "answer_sha256": stable_digest(item.get("answer")),
            }
            for item in normalized_questions
            if str(item.get("answer_state") or "unanswered") in {"answered", "deferred"}
        ]
        answered_questions.sort(key=lambda item: item["question_id"])
        return stable_digest(
            {
                "search_receipt_ids": sorted(
                    str(item.get("receipt_id") or "") for item in receipts
                ),
                "answered_clarifications": answered_questions,
            }
        )

    def current_question_only_claim_ids() -> list[str]:
        claim_ids = [str(item.get("claim_id") or "") for item in claims]
        return sorted(
            claim_id
            for claim_id in claim_ids
            if claim_id
            and any(
                claim_id in list(receipt.get("affected_claim_ids") or [])
                and _receipt_question_usable(receipt)
                for receipt in receipts
            )
            and not any(
                claim_id in list(receipt.get("affected_claim_ids") or [])
                and _receipt_decision_usable(receipt)
                for receipt in receipts
            )
        )

    def source_only_claim_boundary_eligible(claim_id: str) -> bool:
        claim = _dict(claims_by_id.get(claim_id))
        unit_id = str(claim.get("source_unit_id") or "").strip()
        unit = fact_units_by_id.get(unit_id)
        if unit is None:
            return False
        raw_text = str(unit.get("raw_text") or "")
        exact_quote = str(claim.get("exact_quote") or "")
        try:
            quote_start = int(claim.get("quote_start"))
            quote_end = int(claim.get("quote_end"))
        except (TypeError, ValueError):
            return False
        if (
            isinstance(claim.get("quote_start"), bool)
            or isinstance(claim.get("quote_end"), bool)
            or str(claim.get("basis_kind") or "") != "source_span"
            or str(claim.get("basis_ref") or "") != unit_id
            or str(claim.get("source_unit_content_sha256") or "") != _content_sha256(raw_text)
            or not exact_quote
            or quote_start < 0
            or quote_end <= quote_start
            or quote_end > len(raw_text)
            or raw_text[quote_start:quote_end] != exact_quote
        ):
            return False
        unit_attribution = _source_attribution(source_investigation, unit)
        source_uri = claim.get("source_uri") or unit_attribution.get("source_uri")
        source_trust = claim.get("source_trust") or unit_attribution.get("source_trust")
        if not str(source_trust or "").strip():
            return False
        if not str(source_uri or "").strip():
            # Uploaded documents legitimately have no public URL.  Their parser
            # receipt, file hash, exact unit hash, and verbatim quote are a
            # stronger deterministic identity than inventing a URI.
            source_request = _dict(source_investigation.get("source_request"))
            provenance = _dict(unit.get("provenance"))
            unit_digest = _canonical_upstream_digest(
                unit.get("content_digest") or provenance.get("hash")
            )
            file_digest = _canonical_upstream_digest(
                source_request.get("file_hash")
                or _dict(source_investigation.get("provenance")).get("file_hash")
            )
            local_document_identity = bool(
                str(source_trust or "").strip()
                in {"uploaded_document", "document_anchor", "technical_document"}
                and file_digest
                and unit_digest == _content_sha256(raw_text)
                and str(source_investigation.get("investigation_id") or "").strip()
            )
            if not local_document_identity:
                return False
        if any(
            claim_id in list(receipt.get("affected_claim_ids") or [])
            and _receipt_decision_usable(receipt)
            for receipt in receipts
        ):
            return False
        return any(
            claim_id in list(receipt.get("affected_claim_ids") or [])
            and str(receipt.get("brain_revision") or "") == normalized_revision
            and _is_v3_search_receipt(receipt)
            and _receipt_has_semantic_firewall(receipt)
            and (
                not str(receipt.get("grow_receipt_authority_sha256") or "").strip()
                or str(receipt.get("grow_receipt_authority_sha256") or "") == grow_receipt_authority_sha256
            )
            and (_receipt_evidence_usable(receipt) or _receipt_question_usable(receipt))
            for receipt in receipts
        )

    def source_only_decision_authorized(decision: Mapping[str, Any]) -> bool:
        claim_id = str(decision.get("claim_id") or "").strip()
        if str(decision.get("decision") or "").strip() != "source_only":
            return False
        if _strings(decision.get("target_node_ids"), limit=32):
            return False
        receipt_ids = _strings(decision.get("evidence_receipt_ids"), limit=16)
        if not receipt_ids or not source_only_claim_boundary_eligible(claim_id):
            return False
        for receipt_id in receipt_ids:
            receipt = receipts_by_id.get(receipt_id)
            if receipt is None:
                return False
            if claim_id not in list(receipt.get("affected_claim_ids") or []):
                return False
            if str(receipt.get("brain_revision") or "") != normalized_revision:
                return False
            if not _is_v3_search_receipt(receipt) or not _receipt_has_semantic_firewall(receipt):
                return False
            if (
                str(receipt.get("grow_receipt_authority_sha256") or "").strip()
                and str(receipt.get("grow_receipt_authority_sha256") or "") != grow_receipt_authority_sha256
            ):
                return False
            if not (_receipt_evidence_usable(receipt) or _receipt_question_usable(receipt)):
                return False
            if _receipt_decision_usable(receipt):
                return False
        return True

    def decision_target_hydration_repair_context(
        claim_ids: Sequence[str],
    ) -> dict[str, Any]:
        selected_claim_ids = [
            claim_id for claim_id in _strings(claim_ids, limit=resolved_budget.max_claims)
            if claim_id in claims_by_id
        ]
        selected_claim_set = set(selected_claim_ids)
        all_receipts_by_id = {**receipts_by_id, **document_receipts_by_id}
        previous_targets_by_claim: dict[str, list[str]] = {}
        receipt_ids_by_claim: dict[str, list[str]] = {}
        for decision in latest_turn_decisions:
            claim_id = str(decision.get("claim_id") or "").strip()
            if not claim_id or (selected_claim_set and claim_id not in selected_claim_set):
                continue
            receipt_ids = _strings(decision.get("evidence_receipt_ids"), limit=16)
            receipt_node_ids = {
                str(node_id or "").strip()
                for receipt_id in receipt_ids
                for node_id in list(_dict(all_receipts_by_id.get(receipt_id)).get("evidence_node_ids") or [])
                if str(node_id or "").strip()
            }
            missing_targets = [
                node_id
                for node_id in _strings(decision.get("target_node_ids"), limit=32)
                if node_id in receipt_node_ids and node_id not in hydrated_evidence
            ]
            if missing_targets:
                previous_targets_by_claim[claim_id] = sorted(dict.fromkeys(missing_targets))
                receipt_ids_by_claim[claim_id] = receipt_ids
        target_ids = sorted(
            {
                node_id
                for values in previous_targets_by_claim.values()
                for node_id in values
            }
        )
        return {
            "affected_claim_ids": selected_claim_ids,
            "unhydrated_target_node_ids": target_ids,
            "targets_by_claim": previous_targets_by_claim,
            "receipt_ids_by_claim": receipt_ids_by_claim,
            "hydration_wave_available": bool(
                receipts
                and current_review_state_key() not in hydration_review_state_keys
            ),
        }

    def context_builder(
        turn: int,
        phase: str,
        correction: dict[str, Any] | None,
        _observations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        nonlocal repairing_missing_first_search, repairing_empty_continue
        nonlocal repairing_invalid_claim_reference, repairing_review_source_binding
        nonlocal repairing_initial_source_binding, repairing_unanswered_clarification_claim
        nonlocal repairing_forbidden_review_search, repairing_document_hydration_binding
        nonlocal repairing_search_call_budget
        nonlocal repairing_decision_target_hydration
        nonlocal repairing_unusable_decision_receipt
        nonlocal repairing_question_reemitted
        nonlocal repairing_closed_clarification_gap_reopened
        nonlocal closed_clarification_gap_reopened_claim_ids
        nonlocal repairing_question_evidence_refs
        nonlocal decision_receipt_repair_requires_questions
        nonlocal question_only_action_repair_uses_narrow_schema
        nonlocal decision_target_hydration_claim_ids
        nonlocal decision_target_hydration_target_ids
        source_material = [
            {
                "source_unit_id": str(unit.get("unit_id") or ""),
                "title": str(unit.get("title") or ""),
                "raw_text": str(unit.get("raw_text") or ""),
                "content_sha256": _content_sha256(str(unit.get("raw_text") or "")),
                "fact_eligible": bool(unit.get("fact_eligible", True)),
                "source_type": _dict(unit.get("provenance")).get("source_type"),
                **_source_temporal_provenance(unit),
                **_source_attribution(source_investigation, unit),
            }
            for unit in fact_units
        ]
        question_only_claim_ids = current_question_only_claim_ids()
        question_only_receipt_ids = sorted(
            {
                str(receipt.get("receipt_id") or "")
                for receipt in receipts
                if _receipt_question_usable(receipt)
                and not _receipt_decision_usable(receipt)
                and any(
                    claim_id in list(receipt.get("affected_claim_ids") or [])
                    for claim_id in question_only_claim_ids
                )
            }
            - {""}
        )
        source_only_claim_ids = [
            claim_id
            for claim_id in question_only_claim_ids
            if source_only_claim_boundary_eligible(claim_id)
        ]
        question_evidence_authority = [
            _receipt_question_evidence_authority(receipt)
            for receipt in receipts
            if _receipt_question_usable(receipt)
        ]
        post_hydration_question_phase = bool(
            question_only_claim_ids
            and hydrated_evidence
            and current_review_state_key() in hydration_review_state_keys
        )
        protocol_feedback = dict(correction or {}) if correction else None
        repairing_missing_first_search = bool(
            protocol_feedback
            and str(protocol_feedback.get("detail") or "")
            in {
                "first_turn_requires_search_wave",
                "first_search_wave_missing_claims",
                "search_query_text_missing",
                "search_purpose_missing",
            }
            and not receipts
        )
        repairing_search_call_budget = bool(
            protocol_feedback
            and str(protocol_feedback.get("detail") or "")
            == "search_call_budget_exhausted"
        )
        repairing_empty_continue = bool(
            protocol_feedback
            and str(protocol_feedback.get("detail") or "") == "continue_response_requires_tool_calls"
        )
        repairing_mixed_review_action = bool(
            protocol_feedback
            and str(protocol_feedback.get("detail") or "")
            == "review_action_mixed_tool_and_terminal"
            and bool(claims)
        )
        repairing_invalid_claim_reference = bool(
            protocol_feedback
            and str(protocol_feedback.get("detail") or "") == "tool_call_affected_claim_invalid"
        )
        repairing_review_source_binding = bool(
            protocol_feedback
            and str(protocol_feedback.get("detail") or "").startswith(
                "review_claim_source_binding_forbidden:"
            )
        )
        initial_binding_detail = str(
            _dict(protocol_feedback).get("detail") or ""
        )
        repairing_initial_temporal_binding = initial_binding_detail.startswith(
            INITIAL_TEMPORAL_BINDING_ERROR_PREFIXES
        )
        repairing_initial_source_binding = bool(
            protocol_feedback
            and initial_binding_detail.startswith(
                (
                    "claim_source_span_invalid:",
                    "claim_subject_anchor_invalid:",
                    *INITIAL_TEMPORAL_BINDING_ERROR_PREFIXES,
                )
            )
            and not claims
        )
        repairing_unanswered_clarification_claim = bool(
            protocol_feedback
            and str(protocol_feedback.get("detail") or "")
            == "clarification_claim_binding_invalid"
            and not answers
        )
        repairing_forbidden_review_search = bool(
            protocol_feedback
            and str(protocol_feedback.get("detail") or "")
            in {"review_repeated_equivalent_query", "post_answer_search_not_authorized"}
            and bool(receipts)
        )
        repairing_document_hydration_binding = bool(
            protocol_feedback
            and str(protocol_feedback.get("detail") or "")
            == "document_hydration_not_discovered_by_search"
            and bool(receipts)
        )
        repairing_decision_target_hydration = bool(
            protocol_feedback
            and str(protocol_feedback.get("detail") or "").startswith(
                (
                    "decision_target_not_hydrated:",
                    "decision_target_hydration_required:",
                    "decision_target_hydration_repair_invalid:",
                )
            )
            and bool(receipts)
        )
        decision_target_hydration_claim_ids = []
        decision_target_hydration_target_ids = []
        if repairing_decision_target_hydration:
            detail_suffix = str(protocol_feedback.get("detail") or "").split(":", 1)[1]
            decision_target_hydration_claim_ids = [
                claim_id
                for claim_id in _strings(detail_suffix.split(","), limit=resolved_budget.max_claims)
                if claim_id in claims_by_id
            ]
        repairing_question_evidence_refs = bool(
            protocol_feedback
            and str(protocol_feedback.get("detail") or "")
            == "question_evidence_refs_invalid"
            and bool(receipts)
        )
        repairing_unusable_decision_receipt = bool(
            protocol_feedback
            and str(protocol_feedback.get("detail") or "").startswith(
                (
                    "decision_receipt_not_usable:",
                    "question_only_receipt_action_invalid:",
                )
            )
            and bool(receipts)
        )
        repairing_question_only_receipt_action = bool(
            repairing_unusable_decision_receipt
            and str(protocol_feedback.get("detail") or "").startswith(
                "question_only_receipt_action_invalid:"
            )
        )
        repairing_question_reemitted = bool(
            protocol_feedback
            and str(protocol_feedback.get("detail") or "").startswith(
                ("answered_question_reemitted:", "known_question_reemitted:")
            )
        )
        repairing_closed_clarification_gap_reopened = bool(
            protocol_feedback
            and str(protocol_feedback.get("detail") or "").startswith(
                "closed_clarification_gap_reopened:"
            )
        )
        closed_clarification_gap_reopened_claim_ids = []
        if repairing_closed_clarification_gap_reopened:
            decided_claim_ids = {
                str(decision.get("claim_id") or "")
                for decision in normalized_decisions
                if str(decision.get("claim_id") or "")
            }
            closed_clarification_gap_reopened_claim_ids = [
                claim_id
                for claim_id in sorted(str(item.get("claim_id") or "") for item in claims)
                if claim_id and claim_id not in decided_claim_ids and claim_id in claims_by_id
            ]
            closed_question_id = str(protocol_feedback.get("detail") or "").split(":", 1)[1]
            if not closed_clarification_gap_reopened_claim_ids:
                for question in normalized_questions:
                    if str(question.get("question_id") or "") == closed_question_id:
                        closed_clarification_gap_reopened_claim_ids = [
                            claim_id
                            for claim_id in _strings(
                                question.get("affected_claim_ids"),
                                limit=resolved_budget.max_claims,
                            )
                            if claim_id in claims_by_id
                        ]
                        break
        decision_receipt_repair_requires_questions = bool(
            repairing_unusable_decision_receipt
            and bool(question_only_claim_ids)
            and (
                str(protocol_feedback.get("detail") or "").startswith(
                    "decision_receipt_not_usable:"
                )
                or post_hydration_question_phase
            )
        )
        hydration_wave_open = bool(
            receipts
            and current_review_state_key() not in hydration_review_state_keys
        )
        decision_target_hydration_context = decision_target_hydration_repair_context(
            decision_target_hydration_claim_ids
        )
        decision_target_hydration_target_ids = _strings(
            decision_target_hydration_context.get("unhydrated_target_node_ids"),
            limit=resolved_budget.max_evidence_references,
        )
        if repairing_closed_clarification_gap_reopened and question_only_claim_ids:
            review_allowed_actions = ["defer"]
        elif repairing_closed_clarification_gap_reopened:
            review_allowed_actions = ["decide", "defer"]
        elif post_hydration_question_phase or decision_receipt_repair_requires_questions:
            review_allowed_actions = ["ask_questions"]
        elif repairing_question_only_receipt_action:
            review_allowed_actions = ["ask_questions", "defer"]
        elif repairing_decision_target_hydration:
            review_allowed_actions = (
                ["hydrate", "ask_questions"]
                if hydration_wave_open and decision_target_hydration_target_ids
                else ["ask_questions"]
            )
        elif question_only_claim_ids and not source_only_claim_ids:
            review_allowed_actions = ["hydrate", "ask_questions", "defer"]
        elif question_only_claim_ids:
            review_allowed_actions = ["hydrate", "ask_questions", "source_only", "defer"]
        else:
            review_allowed_actions = ["hydrate", "ask_questions", "decide", "defer"]
        hydration_wave_available = bool(
            hydration_wave_open
            and not decision_receipt_repair_requires_questions
        )
        question_only_action_repair_uses_narrow_schema = bool(
            repairing_question_only_receipt_action
            and review_allowed_actions == ["ask_questions", "defer"]
        )
        if repairing_initial_source_binding:
            protocol_feedback["instruction"] = (
                "Correct only the initial claim source binding. Read the canonical raw_text in source_units and copy "
                "each exact_quote as a non-empty verbatim substring from its source_unit_id; do not paraphrase, "
                "translate, normalize, fuzzy-match, or reconstruct source text. Recalculate zero-based quote_start "
                "and quote_end so raw_text[quote_start:quote_end] equals exact_quote exactly. Copy "
                "neutral_subject_anchor as a non-empty verbatim substring inside that exact_quote and recalculate "
                "subject_anchor_start and subject_anchor_end so the same canonical raw_text slice equals the anchor "
                "exactly and remains within the quote span. If the reported error is claim_subject_anchor_invalid, "
                "do not drop or exclude the recoverable claim; choose the smallest exact source noun phrase that "
                "denotes the autonomous assertion's subject without copying the asserted value. Service scope, "
                "capability, schedule, and staffing assertions may have service/capability/schedule/staff-group "
                "subjects rather than proper names. After fixing the initial source binding, return status=continue "
                "with one or two search_brain tool calls whose affected_claim_ids cover every returned claim_key; "
                "do not wait for a separate first-search repair turn. Keep claim_id and source_unit_content_sha256 "
                "null for server binding, preserve the provider-authored semantic investigation, and return the "
                "identical response schema. The server will not infer a quote, anchor, meaning, or semantic fallback."
            )
            if repairing_initial_temporal_binding:
                protocol_feedback["instruction"] += (
                    " Correct the reported temporal binding without weakening exact-span authority. For every "
                    "temporal mention whose basis_kind is source_span, use an exact zero-based byte span from the "
                    "canonical source unit that lies wholly inside the claim's exact_quote. If the existing atomic "
                    "exact_quote already contains the necessary temporal text, correct only the temporal mention "
                    "span. If no temporal assertion is semantically necessary, set temporal_scope to null. If the "
                    "date or interval is semantically necessary and belongs to the same atomic source assertion, "
                    "expand exact_quote verbatim just enough to include it, then recalculate quote and subject-anchor "
                    "offsets while keeping the claim atomic. Never copy a date from source metadata, infer one from "
                    "audit time, or bind a mention outside exact_quote."
                )
            protocol_feedback["canonical_source_unit_ids"] = [
                str(item.get("source_unit_id") or "") for item in source_material
            ]
            protocol_feedback["source_binding_requirements"] = {
                "verbatim_exact_quote": True,
                "zero_based_offsets": True,
                "anchor_inside_quote": True,
                "claim_id_server_bound": True,
                "fuzzy_or_semantic_server_repair": False,
            }
            if repairing_initial_temporal_binding:
                protocol_feedback["temporal_binding_requirements"] = {
                    "source_mentions_inside_exact_quote": True,
                    "exact_zero_based_offsets": True,
                    "temporal_scope_may_be_null": True,
                    "atomic_quote_may_expand_for_necessary_time": True,
                    "source_metadata_or_audit_time_forbidden": True,
                    "server_inference_forbidden": True,
                }
        elif repairing_missing_first_search:
            protocol_feedback["instruction"] = (
                "Preserve the canonical claim ledger by returning claims=[] and exclusions=[]. "
                "Return status=continue, questions=[], decisions=[], and provider-authored search_brain "
                "tool calls whose affected_claim_ids collectively cover every claim_id in claim_ledger. "
                "Each call must contain non-empty string query_text and purpose authored from the claim's "
                "semantic investigation need; the server will not invent semantic mission text. Each query_strategy must "
                "author non-empty neutral_goal, free-form discovery_dimensions, temporal_focus and "
                "counterfactual_safety_summary, with plausible_alternatives allowed to be empty. The source claim remains "
                "a hypothesis and the proposed query must avoid presupposing it or reducing the task to yes/no."
            )
            protocol_feedback["required_claim_ids"] = [
                str(item.get("claim_id") or "") for item in claims
            ]
            protocol_feedback["required_tool_name"] = "search_brain"
        elif repairing_search_call_budget:
            if not receipts:
                protocol_feedback["instruction"] = (
                    "The previous first Search wave proposed more search_brain calls than the configured wave cap. "
                    "Preserve the canonical claim ledger by returning claims=[] and exclusions=[]. Return "
                    "status=continue, questions=[], decisions=[], answer_claims=[] and compress the semantic plan into "
                    "at most first_search_wave_call_cap provider-authored search_brain tool calls whose "
                    "affected_claim_ids collectively cover every claim_id in claim_ledger. Do not drop a claim; merge "
                    "compatible information needs into a natural-language Search query when necessary. Do not use "
                    "keyword rules, fallback queries, hidden extra searches, hydration, questions, decisions, preview or apply."
                )
            else:
                protocol_feedback["instruction"] = (
                    "The previous post-answer Search wave exceeded the remaining Search call budget. Preserve the "
                    "canonical claim ledger by returning claims=[] and exclusions=[]. If exactly one server-bound "
                    "answer_claim with status SEARCHING remains and no post-answer Search wave has already run, return "
                    "status=continue with exactly one search_brain tool call for that answer_claim. Otherwise do not "
                    "Search: choose the valid evidence-led review action from the existing notebook."
                )
            protocol_feedback["first_search_wave_call_cap"] = resolved_budget.search_concurrency
            protocol_feedback["post_answer_search_call_cap"] = 1
            protocol_feedback["remaining_search_calls"] = max(
                0,
                resolved_budget.max_search_calls - search_call_count,
            )
            protocol_feedback["required_claim_ids"] = [
                str(item.get("claim_id") or "") for item in claims
            ]
        elif repairing_review_source_binding:
            protocol_feedback["instruction"] = (
                "Do not restate the canonical claim ledger. Return claims=[] and exclusions=[]. "
                "The server already owns every immutable source span, source hash, subject anchor, claim_id and "
                "claim_key. Preserve the intended status, tool calls, questions or decisions, but refer to claims "
                "only through exact affected_claim_ids. Do not copy source binding fields into the response."
            )
            protocol_feedback["required_claim_ids"] = [
                str(item.get("claim_id") or "") for item in claims
            ]
        elif repairing_unanswered_clarification_claim:
            protocol_feedback["instruction"] = (
                "No clarification answer exists in this run, so answer_claims must be an empty list. Preserve the "
                "canonical claim ledger by returning claims=[] and exclusions=[]. Preserve the intended evidence-led "
                "question or decision, using only exact claim_ids and receipt/node evidence refs already present in the "
                "notebook. Do not invent a user answer or a claim derived from one."
            )
            protocol_feedback["required_claim_ids"] = [
                str(item.get("claim_id") or "") for item in claims
            ]
            protocol_feedback["required_answer_claims"] = []
        elif repairing_forbidden_review_search:
            if str(protocol_feedback.get("detail") or "") == "review_repeated_equivalent_query":
                protocol_feedback["forbidden_action"] = "equivalent_search_query"
            else:
                protocol_feedback["forbidden_action"] = "unauthorized_search_query"
            protocol_feedback["instruction"] = (
                "Search is not authorized in this review state. Preserve the canonical claim ledger and inspect the "
                "unchanged Search receipts and hydration ledger. Return claims=[] and exclusions=[]. Choose exactly one "
                "schema-valid action: hydrate a small motivated set of Search-returned IDs, ask every available "
                "decision-changing clarification, emit an evidence-bound decision, or defer for maintenance review. "
                "Do not invent or retry a Search query, answer_claim, question, or decision outside the existing "
                "evidence notebook."
            )
            protocol_feedback["forbidden_tool_name"] = "search_brain"
            protocol_feedback["observed_search_receipt_ids"] = [
                str(item.get("receipt_id") or "") for item in receipts
            ]
        elif repairing_document_hydration_binding:
            protocol_feedback["instruction"] = (
                "The previous hydrate_document_evidence call targeted IDs that were not discovered as document "
                "references in a usable Search receipt. Preserve the canonical claim ledger by returning claims=[] "
                "and exclusions=[]. Do not Search again. If the next action needs full memory text, use "
                "hydrate_memory_objects with Search-returned evidence_node_ids only. If the receipt is reviewable or "
                "question_usable but not decision_usable, ask evidence-bound human questions or defer; do not emit a "
                "memory decision from partial review evidence."
            )
            protocol_feedback["allowed_document_reference_ids"] = sorted(
                {
                    document_id
                    for receipt in receipts
                    if _receipt_evidence_usable(receipt)
                    for document_id in _receipt_document_reference_ids(receipt)
                }
            )
            protocol_feedback["search_returned_memory_node_ids"] = sorted(
                {
                    str(node_id or "").strip()
                    for receipt in receipts
                    for node_id in list(receipt.get("evidence_node_ids") or [])
                    if str(node_id or "").strip()
                }
            )
        elif repairing_decision_target_hydration:
            protocol_feedback["instruction"] = (
                "The previous AI_REVIEW chose a duplicate, contradiction, supersede, merge/enrich, evolve or delete "
                "decision whose target_node_ids were returned by the cited Search receipt but are not hydrated in the "
                "current evidence notebook. Preserve the canonical claim ledger by returning claims=[] and "
                "exclusions=[]. Do not Search again. If decision_target_hydration_required.hydration_wave_available "
                "is true and unhydrated_target_node_ids is non-empty, return status=continue with one or "
                "more hydrate_memory_objects tool calls for a motivated subset of those Search-returned target IDs, or return "
                "status=needs_clarification with AI-authored evidence-bound questions. Set decisions=[] and "
                "answer_claims=[]. If hydration is not available, transform the attempted memory "
                "decision into status=needs_clarification with AI-authored evidence-bound questions. Do not emit duplicate, contradiction, "
                "supersede, merge/enrich, evolve or delete decisions until the target evidence is hydrated."
            )
            protocol_feedback["decision_target_hydration_required"] = decision_target_hydration_context
            protocol_feedback["allowed_actions"] = review_allowed_actions
            protocol_feedback["forbidden_actions"] = (
                ["search", "decide", "defer", "preview", "apply"]
                if "hydrate" in review_allowed_actions
                else ["search", "hydrate", "decide", "preview", "apply"]
            )
        elif repairing_closed_clarification_gap_reopened:
            protocol_feedback["instruction"] = (
                "The previous AI_REVIEW asked a new human question that semantically reopens a missing-detail "
                "gap already closed by a human clarification answer. This repair pass is terminal. Return "
                "status=complete with claims=[], answer_claims=[], exclusions=[], tool_calls=[], questions=[]. "
                "Return decisions for every required_decision_claim_ids entry. Use the closed negative answer as "
                "source-bound decision evidence: make an evidence-bound decision from remaining evidence only when "
                "the cited receipt is decision_usable; otherwise return decision=defer with "
                "no_clarification_can_change_decision=true and a human-readable explanation that no further "
                "clarification can change the closed missing-detail gap. Do not ask any question, Search, hydrate, "
                "preview, apply, or invent a fallback."
            )
            protocol_feedback["required_status"] = "complete"
            protocol_feedback["required_output_shape"] = {
                "claims": [],
                "answer_claims": [],
                "exclusions": [],
                "tool_calls": [],
                "questions": [],
                "decisions": "one_per_required_decision_claim_id",
            }
            protocol_feedback["required_decision_claim_ids"] = list(
                closed_clarification_gap_reopened_claim_ids
            )
            protocol_feedback["question_only_claim_ids"] = list(question_only_claim_ids)
            protocol_feedback["closed_clarification_gaps"] = [
                {
                    "question_id": str(item.get("question_id") or ""),
                    "question_text": str(item.get("question_text") or "")[:800],
                    "answer_state": str(item.get("answer_state") or "unanswered"),
                    "answer": str(item.get("answer") or "")[:800],
                    "affected_claim_ids": _strings(item.get("affected_claim_ids"), limit=resolved_budget.max_claims),
                    "decision_effect": str(item.get("decision_effect") or ""),
                }
                for item in normalized_questions
                if str(item.get("question_id") or "")
                and str(item.get("answer_state") or "unanswered") in {"answered", "deferred"}
            ]
        elif repairing_question_reemitted:
            protocol_feedback["instruction"] = (
                "The previous AI_REVIEW re-emitted a question_id that is already present in clarifications. "
                "Preserve the canonical claim ledger by returning claims=[] and exclusions=[]. Do not repeat any "
                "answered or deferred question; use the existing clarification answer already present in the notebook. "
                "If a genuinely new clarification is still needed, ask it as a new evidence-bound question with a "
                "new provider-authored question_id, tool_calls=[] and decisions=[]. Do not Search, hydrate, decide, "
                "defer, preview, apply, or invent a fallback question."
            )
            protocol_feedback["known_question_ids"] = sorted(
                {
                    str(item.get("question_id") or "")
                    for item in normalized_questions
                    if str(item.get("question_id") or "")
                }
            )
            protocol_feedback["closed_question_ids"] = sorted(
                {
                    str(item.get("question_id") or "")
                    for item in normalized_questions
                    if str(item.get("question_id") or "")
                    and str(item.get("answer_state") or "unanswered")
                    in {"answered", "deferred"}
                }
            )
        elif repairing_question_evidence_refs:
            protocol_feedback["instruction"] = (
                "The previous clarification question cited evidence_refs outside question_evidence_authority. "
                "Preserve the canonical claim ledger by returning claims=[] and exclusions=[]. Return a schema-valid "
                "action from the current review contract. For human questions, cite only "
                "question_evidence_authority.allowed_evidence_refs. Use supported_node_ids only for Search-master "
                "entailed support. For a Search-declared gap or missing brain evidence, cite the receipt_id from "
                "gap_authority_refs rather than diagnostic-only, non-entailed, or merely retrieved node IDs. Do not "
                "Search again, do not invent evidence, and do not use keyword or canned question logic."
            )
            protocol_feedback["question_evidence_authority"] = question_evidence_authority
        elif repairing_unusable_decision_receipt:
            if decision_receipt_repair_requires_questions:
                protocol_feedback["instruction"] = (
                    "The previous AI_REVIEW attempted a decision or defer action using a Search receipt that is "
                    "question_usable but not decision_usable. This receipt can authorize human clarification, not "
                    "memory decisions, maintenance defer, preview, or apply. Preserve the canonical claim ledger by "
                    "returning claims=[] and exclusions=[]. Return exactly status=needs_clarification with "
                    "tool_calls=[] decisions=[] answer_claims=[] and one or more human, evidence-bound questions for "
                    "the affected claims. The notebook already contains hydrated_evidence for the relevant "
                    "Search-returned candidate, so use it directly and do not request another hydration wave. Do not "
                    "Search, hydrate, decide, defer, preview, apply, or invent tool results. The number, wording and "
                    "structure of questions remain your semantic decision; ask every "
                    "non-redundant question whose answer can change identity, truth, scope, merge, supersede, "
                    "derivation or utility."
                )
                protocol_feedback["required_status"] = "needs_clarification"
                protocol_feedback["required_output_shape"] = {
                    "status": "needs_clarification",
                    "claims": [],
                    "exclusions": [],
                    "answer_claims": [],
                    "tool_calls": [],
                    "decisions": [],
                    "questions_min_items": 1,
                    "questions_authority": "ai_authored_evidence_bound",
                }
            else:
                protocol_feedback["instruction"] = (
                    "The Search receipt is question_usable but not decision_usable. Preserve the canonical claim ledger "
                    "by returning claims=[] and exclusions=[]. Return either status=needs_clarification with one or more "
                    "human, evidence-bound questions and tool_calls=[] decisions=[], or status=complete with only defer "
                    "decisions for the affected claims when you attest that no clarification can change the decision. "
                    "Do not Search again, do not hydrate in this repair turn, and do not emit compiler claims, memory "
                    "decisions, preview candidates, or apply-ready output. The number, wording and structure of questions "
                    "remain your semantic decision; ask every non-redundant question whose answer can change identity, "
                    "truth, scope, merge, supersede, derivation or utility. Defer only with "
                    "no_clarification_can_change_decision=true and a human-readable evidence-bound explanation."
                )
                protocol_feedback["allowed_terminal_actions"] = [
                    "ask_questions",
                    "defer",
                ]
            protocol_feedback["reviewable_receipt_ids"] = question_only_receipt_ids
            protocol_feedback["affected_claim_ids"] = question_only_claim_ids
            protocol_feedback["allowed_actions"] = review_allowed_actions
            protocol_feedback["forbidden_actions"] = (
                ["search", "hydrate", "decide", "defer", "preview", "apply"]
                if decision_receipt_repair_requires_questions
                else ["search", "hydrate", "decide", "preview", "apply"]
            )
        elif repairing_mixed_review_action:
            protocol_feedback["instruction"] = (
                "The previous AI_REVIEW mixed a tool action with a terminal turn action. That output is ambiguous "
                "and cannot be executed. Preserve the canonical claim ledger by returning claims=[] and exclusions=[]. "
                "Choose exactly one schema-valid action category from the current review evidence. If more evidence "
                "is needed, return status=continue with one or more allowed tool_calls and questions=[] decisions=[]. "
                "If a human answer can change the decision, return status=needs_clarification with tool_calls=[] "
                "decisions=[] and all evidence-bound questions. If the evidence is sufficient or the claim must be "
                "deferred to maintenance review, return status=complete with tool_calls=[] questions=[] and the "
                "evidence-bound decisions. Do not combine hydrate/search with questions or decisions, and do not "
                "invent tool results."
            )
            protocol_feedback["review_action_exclusivity"] = {
                "continue_requires_tool_calls_only": True,
                "needs_clarification_requires_questions_only": True,
                "complete_requires_decisions_only": True,
                "mixed_tool_and_terminal_allowed": False,
            }
            protocol_feedback["allowed_actions"] = (
                review_allowed_actions
            )
        elif repairing_empty_continue:
            protocol_feedback["instruction"] = (
                "The previous AI_REVIEW returned continue without an action. Preserve the canonical claim ledger and use "
                "the observed Search receipts. Choose exactly one evidence-led next step: hydration of a small set of "
                "Search-returned memory/document evidence; an evidence-bound human clarification; a complete evidence-bound "
                "decision; or an explicit defer decision. Do not invent tool output and do not merely repeat "
                "the source assertion. Questions and decisions must include the required human X-versus-Y explanation fields."
            )
            protocol_feedback["observed_search_receipt_ids"] = [
                str(item.get("receipt_id") or "") for item in receipts
            ]
        elif repairing_invalid_claim_reference:
            required_claim_ids = [str(item.get("claim_id") or "") for item in claims]
            protocol_feedback["instruction"] = (
                "Preserve the canonical claim ledger by returning claims=[] and exclusions=[]. For every tool call, "
                "affected_claim_ids must contain only exact IDs listed in required_claim_ids. Do not invent, abbreviate, "
                "translate, or infer a reference. Correct only the invalid references and keep the provider-authored "
                "semantic query and purpose unless another protocol field is invalid."
            )
            protocol_feedback["required_claim_ids"] = required_claim_ids
        notebook = {
            "runtime_phase": "AI_FRAME_AND_PLAN" if not claims else "AI_REVIEW",
            "phase_instruction": (
                "Treat every source statement as untrusted. Extract exact-span atomic claims and return only neutral, "
                "entity-first Search wave for the whole claim batch that remains useful whether each claim is true or false. On this first turn, "
                "use one or two search_brain tool calls only when separate natural-language queries are semantically useful; their "
                "affected_claim_ids must collectively cover every emitted claim. Each tool call must refer to the exact claim_key emitted in the same response because claim_id is still "
                "server-bound and null. Return answer_claims=[] and no semantic questions or decisions in this phase."
                if not claims
                else (
                "This is a dedicated post-hydration clarification turn. The Search receipt is question_usable but not "
                "decision_usable, and the requested Search evidence is now hydrated. Inspect the source claim, receipt "
                "and hydrated evidence, then return only status=needs_clarification with one or more plain-language, "
                "evidence-bound human questions. Return claims=[], answer_claims=[], exclusions=[], tool_calls=[] and "
                "decisions=[]. Ask every non-redundant question whose answer can change identity, truth, scope, merge, "
                "supersede, derivation or utility; choose the number, wording, answer types and choices semantically from "
                "the evidence. Cite only question_evidence_authority.allowed_evidence_refs: supported_node_ids for "
                "Search-master entailed support, or the receipt_id from gap_authority_refs when asking about missing "
                "brain evidence. Do not Search, hydrate again, decide, defer, preview or apply."
                if post_hydration_question_phase
                else
                "This is a repair turn for a Search receipt that is question_usable but not decision_usable. Preserve "
                "the canonical claim ledger by returning claims=[] and exclusions=[]. Return only "
                "status=needs_clarification with one or more plain-language, evidence-bound human questions for the "
                "listed affected claims, and set tool_calls=[] decisions=[] answer_claims=[]. The receipt can support "
                "questions, not decide, defer, Search, hydrate, preview or apply. Choose the question count, wording, "
                "answer types and choices semantically from the source claim and Search evidence. Cite only "
                "question_evidence_authority.allowed_evidence_refs: supported_node_ids for Search-master entailed "
                "support, or the receipt_id from gap_authority_refs when asking about missing brain evidence."
                if decision_receipt_repair_requires_questions
                else
                "Return claims=[] and exclusions=[]; the server already owns the canonical claim ledger. Compare the "
                "source claim with the observed brain evidence in plain human language, including the "
                "AI-authored temporal_scope when time affects meaning. Treat source_published_at as source chronology and "
                "source_acquired_at/source_retrieved_at and "
                "brain created_at/ingested_at as provenance or audit only, never as the claim's event or validity time. "
                "If unresolved temporal ambiguity can change identity, truth, merge, supersede, or utility, ask an "
                "evidence-authored human question that states what the source says, what the brain says, why the date "
                "matters, and the exact answer needed; never generate a canned or keyword-triggered question. Do not Search "
                "again unless the server has bound a new exact-span answer_claim with status SEARCHING. Otherwise choose exactly "
                "one action: hydrate a small, motivated set of Search-returned IDs, ask "
                "every necessary non-redundant decision-changing clarification up to the configured limit, emit an evidence-bound "
                "decision, or defer with a human explanation for maintenance review. Human answers already recorded in "
                "clarifications close the gap they answered. A negative answer that the requested detail is absent, "
                "unknown, unnamed, or not specified by the source is decision evidence for that absence; do not ask a "
                "paraphrase of the same missing-detail gap. For questions, cite only "
                "question_evidence_authority.allowed_evidence_refs: supported_node_ids for Search-master entailed "
                "support, or the receipt_id from gap_authority_refs when the question is grounded in missing brain "
                "evidence. A clarification answer may support a genuinely additional "
                "atomic claim: propose it only in answer_claims, bind it to parent_claim_id, basis_kind=clarified_answer, "
                "basis_ref=question_id and an exact answer span, and Search it in the same turn. It cannot be decided or "
                "compiled until a later AI_REVIEW observes that Search receipt. A target-free source_only decision is "
                "allowed only for source_only_eligible_claim_ids, where the server-bound source span/hash and "
                "source URI/trust attribution are the primary evidence, Search has run on the same brain revision as "
                "a boundary, and no Search receipt for that claim is decision_usable for an equivalent or conflicting "
                "target. Explain that this is source-bound memory, not independent verification."
                + (
                    " Receipt capability is authoritative for the current review: the listed question-only claims have "
                    "question_usable evidence but no decision_usable receipt. For claims listed in "
                    "source_only_eligible_claim_ids you may choose a target-free source_only decision. For all other "
                    "question-only claims choose only hydration, one or more evidence-bound human questions, or "
                    "maintenance defer with no_clarification_can_change_decision attestation; do not emit other "
                    "memory decisions, Search, preview or apply."
                    if question_only_claim_ids and not repairing_closed_clarification_gap_reopened
                    else ""
                )
                )
            ),
            "review_action_contract": {
                "allowed_actions": (
                    review_allowed_actions
                ),
                "post_hydration_question_phase": post_hydration_question_phase,
                "question_only_claim_ids": question_only_claim_ids,
                "question_only_receipt_ids": question_only_receipt_ids,
                "question_id_policy": {
                    "reuse_known_question_ids": False,
                    "answered_or_deferred_questions_are_closed": True,
                    "server_canonicalizes_accidental_new_question_id_collisions": True,
                    "known_question_ids": sorted(
                        {
                            str(item.get("question_id") or "")
                            for item in normalized_questions
                            if str(item.get("question_id") or "")
                        }
                    ),
                    "closed_question_ids": sorted(
                        {
                            str(item.get("question_id") or "")
                            for item in normalized_questions
                            if str(item.get("question_id") or "")
                            and str(item.get("answer_state") or "unanswered")
                            in {"answered", "deferred"}
                        }
                    ),
                },
                "initial_search_policy": "one_search_wave_with_one_or_two_provider_authored_calls_covering_claim_batch",
                "first_search_wave_call_cap": resolved_budget.search_concurrency,
                "post_answer_search_call_cap": 1,
                "post_answer_search_policy": "only_new_exact_span_server_bound_answer_claims_with_status_SEARCHING",
                "semantic_repair_search_allowed": False,
                "keyword_fallback_allowed": False,
                "fixed_questionnaire_allowed": False,
                "source_only_eligible_claim_ids": source_only_claim_ids,
                "source_only_policy": {
                    "decision": "source_only",
                    "requires_server_bound_exact_source_span": True,
                    "requires_source_uri_and_trust_attribution": True,
                    "requires_search_boundary_same_brain_revision": True,
                    "requires_no_decision_usable_receipt_for_claim": True,
                    "target_node_ids_must_be_empty": True,
                    "independent_verification_claimed": False,
                },
                "hydration_policy": {
                    "explicit_ai_review_action_required": True,
                    "server_auto_hydrates_all_receipt_evidence": False,
                    "only_search_returned_ids": True,
                    "max_nodes_per_review_state": resolved_budget.max_hydration_nodes_per_review,
                    "current_review_state_key": current_review_state_key(),
                    "hydration_wave_available": hydration_wave_available,
                },
                "decision_target_hydration_repair": (
                    decision_target_hydration_context
                    if repairing_decision_target_hydration
                    else None
                ),
            },
            "question_evidence_authority": question_evidence_authority,
            "turn": turn,
            "response_phase": phase,
            "protocol_feedback": protocol_feedback,
            "brain_revision": normalized_revision,
            "semantic_authority": authority,
            "temporal_evidence_policy": {
                "claim_temporal_scope_authority": "provider_evidence_bound",
                "source_published_at_role": "source_chronology_only",
                "source_acquired_at_role": "source_provenance_only",
                "source_retrieved_at_role": "source_provenance_only",
                "brain_created_at_role": "audit_only",
                "brain_ingested_at_role": "audit_only",
                "technical_timestamps_prove_claim_time_or_truth": False,
                "question_policy": "provider_authored_only_when_temporal_ambiguity_changes_decision",
            },
            "source_units": source_material if not claims else [],
            "source_binding_findings": [
                dict(finding)
                for exclusion in exclusions
                if (finding := _dict(exclusion.get("human_finding")))
            ],
            "claim_ledger": claims,
            "search_receipts": receipts,
            "search_continuation_briefs": _search_continuation_briefs(receipts),
            "document_evidence_receipts": document_receipts,
            "hydrated_evidence": hydrated_evidence,
            "decisions": normalized_decisions,
            "clarifications": normalized_questions,
            "clarification_answers": answers,
            "question_policy": {
                "maximum_pending_questions": resolved_question_limit,
                "group_non_redundant_questions": True,
                "ask_every_independently_decision_changing_question": True,
            },
            "clarification_answer_policy": {
                "answered_questions_close_requested_gap": True,
                "deferred_questions_are_closed": True,
                "negative_answer_closes_absent_detail_gap": True,
                "same_missing_detail_paraphrase_allowed": False,
                "instruction": (
                    "Treat every answered or deferred clarification as closed authority for the exact gap asked. "
                    "If an answer says the requested detail is not available in, not named by, or not specified by "
                    "the source, that negative answer closes the missing-detail gap. Do not ask a paraphrase of the "
                    "same absent-detail question. Exclude unsupported subclaims, make an evidence-bound decision from "
                    "remaining evidence, or defer with no-clarification-can-change-decision attestation; ask only for "
                    "a genuinely distinct unresolved gap whose answer can independently change the decision."
                ),
                "closed_clarification_gaps": [
                    {
                        "question_id": str(item.get("question_id") or ""),
                        "question_text": str(item.get("question_text") or "")[:800],
                        "answer_state": str(item.get("answer_state") or "unanswered"),
                        "answer": str(item.get("answer") or "")[:800],
                        "affected_claim_ids": _strings(item.get("affected_claim_ids"), limit=resolved_budget.max_claims),
                        "decision_effect": str(item.get("decision_effect") or ""),
                    }
                    for item in normalized_questions
                    if str(item.get("question_id") or "")
                    and str(item.get("answer_state") or "unanswered")
                    in {"answered", "deferred"}
                ],
            },
            "remaining_budget": {
                "turns": max(0, resolved_budget.max_turns - turn + 1),
                "search_calls": max(0, resolved_budget.max_search_calls - search_call_count),
                "evidence_references": max(
                    0,
                    resolved_budget.max_evidence_references
                    - len({node_id for item in receipts for node_id in list(item.get("evidence_node_ids") or [])}),
                ),
            },
        }
        encoded = json.dumps(notebook, ensure_ascii=False, sort_keys=True, default=str)
        if len(encoded) > resolved_budget.max_notebook_chars:
            notebook["search_receipts"] = [
                {
                    key: item.get(key)
                    for key in (
                        "receipt_id",
                        "affected_claim_ids",
                        "query_text",
                        "contract_outcome",
                        "authoritative_no_match",
                        "evidence_usable",
                        "question_usable",
                        "decision_usable",
                        "usable",
                        "evidence_node_ids",
                        "context_excerpt",
                        "document_references",
                        "master_judgement",
                    )
                }
                for item in receipts
            ]
            for item in notebook["search_receipts"]:
                item["continuation_brief"] = _search_continuation_brief(item)
            for item in notebook["search_receipts"]:
                item["context_excerpt"] = str(item.get("context_excerpt") or "")[:2_000]
            notebook["hydrated_evidence"] = {
                node_id: {
                    key: material.get(key)
                    for key in (
                        "node_id",
                        "canonical_text",
                        "summary",
                        "claim_status",
                        "source_trust",
                        "node_revision",
                        "temporal_role",
                        "observed_at",
                        "valid_from",
                        "valid_to",
                        "lifecycle_status",
                        "superseded_at",
                        "superseded_by",
                        "created_at",
                        "updated_at",
                        "ingested_at",
                        "provenance",
                        "digest",
                    )
                }
                for node_id, material in hydrated_evidence.items()
            }
            for material in notebook["hydrated_evidence"].values():
                material["canonical_text"] = str(material.get("canonical_text") or "")[:2_000]
                material["summary"] = str(material.get("summary") or "")[:1_000]
        return notebook

    def turn_validator(payload: dict[str, Any], turn: int) -> tuple[dict[str, Any] | None, str | None]:
        nonlocal claims, exclusions, claims_by_id, latest_turn_decisions, latest_turn_questions
        if str(payload.get("schema_version") or "") != GROW_INVESTIGATOR_TURN_SCHEMA_VERSION:
            return None, "grow_investigator_turn_schema_invalid"
        for field in ("claims", "exclusions", "tool_calls", "questions", "decisions"):
            if not isinstance(payload.get(field), list):
                return None, f"{field}_invalid"
        if payload.get("answer_claims") is not None and not isinstance(payload.get("answer_claims"), list):
            return None, "answer_claims_invalid"
        status = str(payload.get("status") or "").strip().casefold()
        if status not in {"continue", "needs_clarification", "complete"}:
            return None, "status_invalid"
        if repairing_closed_clarification_gap_reopened and (
            status != "complete"
            or payload.get("claims")
            or payload.get("answer_claims")
            or payload.get("exclusions")
            or payload.get("tool_calls")
            or payload.get("questions")
            or not payload.get("decisions")
        ):
            return None, "closed_clarification_gap_repair_action_invalid"
        key_map: dict[str, str] = {}
        answer_claim_ids: set[str] = set()
        merged_review_claims: list[dict[str, Any]] | None = None
        if not claims:
            if payload.get("answer_claims"):
                return None, "first_turn_cannot_add_answer_claims"
            candidate_claims, candidate_exclusions, key_map, bind_error = _bind_initial_claims(
                payload,
                source_investigation=source_investigation,
                max_claims=resolved_budget.max_claims,
            )
            if bind_error:
                return None, bind_error
            if payload.get("questions") or payload.get("decisions"):
                return None, "first_turn_cannot_question_or_decide"
            claims = candidate_claims
            exclusions = candidate_exclusions
            claims_by_id = {str(item.get("claim_id") or ""): item for item in claims}
        elif not (
            repairing_missing_first_search
            or repairing_empty_continue
            or repairing_search_call_budget
            or repairing_invalid_claim_reference
            or repairing_review_source_binding
            or repairing_unanswered_clarification_claim
            or repairing_document_hydration_binding
            or repairing_question_evidence_refs
            or repairing_unusable_decision_receipt
            or repairing_closed_clarification_gap_reopened
        ):
            merged_claims, merge_error = _merge_review_claims(
                payload.get("claims"),
                claims=claims,
                max_claims=resolved_budget.max_claims,
                source_investigation=source_investigation,
                questions=normalized_questions,
                hydrated_evidence=hydrated_evidence,
                search_receipts=receipts,
                document_receipts=document_receipts,
            )
            if merge_error:
                return None, merge_error
            exclusion_error = _validate_review_exclusions(
                payload.get("exclusions"),
                exclusions=exclusions,
                max_claims=resolved_budget.max_claims,
            )
            if exclusion_error:
                return None, exclusion_error
            merged_review_claims = list(merged_claims or claims)
            answer_additions, answer_key_map, answer_error = _bind_clarification_answer_claims(
                payload.get("answer_claims") or [],
                claims=merged_review_claims,
                questions=normalized_questions,
                source_investigation=source_investigation,
                hydrated_evidence=hydrated_evidence,
                search_receipts=receipts,
                document_receipts=document_receipts,
                max_claims=resolved_budget.max_claims,
            )
            if answer_error:
                return None, answer_error
            merged_review_claims.extend(answer_additions)
            key_map.update(answer_key_map)
            answer_claim_ids = {
                str(item.get("claim_id") or "") for item in answer_additions
            }
        elif payload.get("answer_claims"):
            return None, "repair_turn_cannot_add_answer_claims"

        validation_claims = merged_review_claims if merged_review_claims is not None else claims
        claims_by_id = {str(item.get("claim_id") or ""): item for item in validation_claims}

        canonical_claim_references: dict[str, str] = {}
        for canonical_claim in validation_claims:
            canonical_claim_id = str(canonical_claim.get("claim_id") or "").strip()
            canonical_claim_key = str(canonical_claim.get("claim_key") or "").strip()
            if canonical_claim_id:
                canonical_claim_references[canonical_claim_id] = canonical_claim_id
            if canonical_claim_key and canonical_claim_id:
                canonical_claim_references[canonical_claim_key] = canonical_claim_id
        excluded_claim_references = {
            str(exclusion.get("claim_key") or "").strip()
            for exclusion in exclusions
            if str(exclusion.get("binding_state") or "") == "binding_excluded"
            and str(exclusion.get("claim_key") or "").strip()
        }

        normalized_calls: list[dict[str, Any]] = []
        affected_by_search: set[str] = set()
        turn_call_ids: set[str] = set()
        for server_call_ordinal, call in enumerate(
            _dicts(payload.get("tool_calls"), limit=resolved_budget.max_tool_calls),
            start=1,
        ):
            call_id = str(call.get("call_id") or "").strip()
            if not call_id:
                return None, "tool_call_id_missing"
            if call_id in turn_call_ids:
                return None, "tool_call_id_duplicate"
            turn_call_ids.add(call_id)
            tool_name = str(call.get("tool_name") or "").strip()
            arguments = _dict(call.get("arguments"))
            requested_affected = _strings(
                arguments.get("affected_claim_ids"), limit=128
            )
            if not requested_affected:
                return None, "tool_call_affected_claim_invalid"
            unknown_affected = [
                item
                for item in requested_affected
                if item not in canonical_claim_references
                and item not in key_map
                and item not in excluded_claim_references
            ]
            if unknown_affected:
                return None, "tool_call_affected_claim_invalid"
            affected = [
                canonical_claim_references.get(item, key_map.get(item, ""))
                for item in requested_affected
                if item not in excluded_claim_references
            ]
            affected = [item for item in affected if item]
            if not affected:
                # A provider may have planned a call before exact binding was
                # finalized. A call that targets only binding-excluded claims
                # has no downstream authority and is removed, not executed.
                continue
            if any(item not in claims_by_id for item in affected):
                return None, "tool_call_affected_claim_invalid"
            arguments["affected_claim_ids"] = affected
            if tool_name == "search_brain":
                query_text = str(arguments.get("query_text") or "").strip()
                purpose = str(arguments.get("purpose") or "").strip()
                if not query_text:
                    return None, "search_query_text_missing"
                if not purpose:
                    return None, "search_purpose_missing"
                arguments["retrieval_mode"] = "balanced"
                arguments["max_matches"] = 24
                # ``node_ids`` exists only because all investigator tools share
                # one transport schema. Search never consumes it; canonicalize
                # it away instead of spending an AI repair on an irrelevant
                # field or allowing it to constrain semantic discovery.
                arguments["node_ids"] = []
                strategy_error = _validate_search_query_strategy(
                    arguments.get("query_strategy"),
                    first_wave=not receipts,
                )
                # The investigator's strategy object is retained as useful
                # diagnostics for clients and older callers.  It is not a
                # self-attesting semantic gate: the server verifies source
                # span bindings, and the Search receipt judges evidence.
                arguments["query_strategy_diagnostic"] = {
                    "valid": strategy_error is None,
                    "detail": strategy_error,
                    "semantic_authority": False,
                }
                affected_by_search.update(affected)
                signature = stable_digest(
                    {"query": query_text.casefold(), "claim_ids": sorted(affected)}
                )
                if (
                    receipts
                    and signature in query_claim_signatures
                ):
                    return None, "review_repeated_equivalent_query"
            elif tool_name == "hydrate_memory_objects":
                node_ids = _strings(arguments.get("node_ids"), limit=96)
                purpose = str(arguments.get("purpose") or "").strip()
                if not node_ids or arguments.get("query_text") is not None or not purpose:
                    return None, "hydrate_tool_arguments_invalid"
                arguments["node_ids"] = node_ids
                arguments["purpose"] = purpose
            elif tool_name == "hydrate_document_evidence":
                document_ids = _strings(
                    arguments.get("node_ids"),
                    limit=resolved_budget.max_documents_per_call,
                )
                purpose = str(arguments.get("purpose") or "").strip()
                if not document_ids or arguments.get("query_text") is not None or not purpose:
                    return None, "document_hydration_tool_arguments_invalid"
                arguments["node_ids"] = document_ids
                arguments["purpose"] = purpose
            else:
                return None, "tool_name_invalid"
            server_call_id = (
                f"grow-tool::{investigation_id}::run-{run_call_authority_sha256[:16]}::"
                f"turn-{turn}::ordinal-{server_call_ordinal}::{tool_name}::"
                f"{stable_digest({'tool_name': tool_name, 'arguments': arguments})[:20]}"
            )
            normalized_calls.append(
                {
                    **call,
                    "call_id": server_call_id,
                    "server_call_ordinal": server_call_ordinal,
                    "run_call_authority_sha256": run_call_authority_sha256,
                    "provider_call_ref": call_id,
                    "arguments": arguments,
                }
            )
        payload["tool_calls"] = normalized_calls
        search_tool_calls = [
            call
            for call in normalized_calls
            if str(call.get("tool_name") or "") == "search_brain"
        ]
        hydration_tool_calls = [
            call
            for call in normalized_calls
            if str(call.get("tool_name") or "")
            in {"hydrate_memory_objects", "hydrate_document_evidence"}
        ]
        if search_tool_calls and hydration_tool_calls:
            return None, "search_and_hydration_require_separate_waves"
        if (
            search_tool_calls
            and not receipts
            and len(search_tool_calls) > resolved_budget.search_concurrency
        ):
            return None, "search_call_budget_exhausted"
        if search_tool_calls and search_call_count + len(search_tool_calls) > resolved_budget.max_search_calls:
            return None, "search_call_budget_exhausted"
        if normalized_calls and status in {"needs_clarification", "complete"}:
            return None, "review_action_mixed_tool_and_terminal"
        if payload.get("questions") and payload.get("decisions"):
            return None, "review_action_mixed_questions_and_decisions"
        question_only_claim_ids = current_question_only_claim_ids()
        question_only_claim_set = set(question_only_claim_ids)
        raw_question_only_defer_decisions = [
            decision
            for decision in _dicts(payload.get("decisions"), limit=resolved_budget.max_claims)
            if str(decision.get("claim_id") or "") in question_only_claim_set
            and str(decision.get("decision") or "") == "defer"
        ]
        raw_question_only_source_only_decisions = [
            decision
            for decision in _dicts(payload.get("decisions"), limit=resolved_budget.max_claims)
            if str(decision.get("claim_id") or "") in question_only_claim_set
            and source_only_decision_authorized(decision)
        ]
        post_hydration_question_phase = bool(
            question_only_claim_ids
            and hydrated_evidence
            and current_review_state_key() in hydration_review_state_keys
        )
        if decision_receipt_repair_requires_questions and (
            normalized_calls
            or payload.get("decisions")
            or status != "needs_clarification"
            or not payload.get("questions")
        ):
            return None, (
                "question_only_receipt_action_invalid:"
                + (question_only_claim_ids[0] if question_only_claim_ids else "unknown")
            )
        if question_only_action_repair_uses_narrow_schema:
            repair_questions_valid = (
                status == "needs_clarification"
                and not normalized_calls
                and not payload.get("decisions")
                and bool(payload.get("questions"))
            )
            repair_defer_valid = (
                status == "complete"
                and not normalized_calls
                and not payload.get("questions")
                and bool(raw_question_only_defer_decisions)
            )
            if not (repair_questions_valid or repair_defer_valid):
                return None, (
                    "question_only_receipt_action_invalid:"
                    + (question_only_claim_ids[0] if question_only_claim_ids else "unknown")
                )
        if post_hydration_question_phase and (
            normalized_calls
            or payload.get("decisions")
            or status != "needs_clarification"
            or not payload.get("questions")
        ):
            return None, f"question_only_receipt_action_invalid:{question_only_claim_ids[0]}"
        if (
            question_only_claim_ids
            and payload.get("decisions")
            and not repairing_closed_clarification_gap_reopened
            and any(
                str(decision.get("claim_id") or "") in question_only_claim_set
                and str(decision.get("decision") or "") != "defer"
                and not source_only_decision_authorized(decision)
                for decision in _dicts(payload.get("decisions"), limit=resolved_budget.max_claims)
            )
        ):
            return None, f"decision_receipt_not_usable:{question_only_claim_ids[0]}"
        if (
            question_only_claim_ids
            and status == "complete"
            and not repairing_closed_clarification_gap_reopened
            and not raw_question_only_defer_decisions
            and not raw_question_only_source_only_decisions
        ):
            return None, f"question_only_receipt_action_invalid:{question_only_claim_ids[0]}"
        if receipts and search_tool_calls:
            if len(search_tool_calls) != 1 or search_wave >= 2:
                return None, "post_answer_search_wave_exhausted"
            authorized_search_claims = {
                claim_id
                for claim_id, claim in claims_by_id.items()
                if str(claim.get("status") or "") == "SEARCHING"
                and str(claim.get("basis_kind") or "") == "clarified_answer"
            }
            if not authorized_search_claims or affected_by_search - authorized_search_claims:
                return None, "post_answer_search_not_authorized"
            if authorized_search_claims - affected_by_search:
                return None, (
                    "post_answer_search_missing_claims:"
                    + ",".join(sorted(authorized_search_claims - affected_by_search))
                )
        if hydration_tool_calls:
            if not receipts:
                return None, "hydration_requires_search_receipt"
            hydration_state_key = current_review_state_key()
            if hydration_state_key in hydration_review_state_keys:
                return None, "review_hydration_wave_exhausted"
            requested_hydration_ids: list[str] = []
            for call in hydration_tool_calls:
                requested_hydration_ids.extend(
                    _strings(
                        _dict(call.get("arguments")).get("node_ids"),
                        limit=resolved_budget.max_evidence_references,
                    )
                )
                if str(call.get("tool_name") or "") == "hydrate_document_evidence":
                    arguments = _dict(call.get("arguments"))
                    document_candidates = _document_hydration_candidate_receipts(
                        receipts,
                        claim_ids=_strings(arguments.get("affected_claim_ids"), limit=128),
                        document_ids=_strings(
                            arguments.get("node_ids"),
                            limit=resolved_budget.max_documents_per_call,
                        ),
                    )
                    if not document_candidates:
                        return None, "document_hydration_not_discovered_by_search"
                    if len(document_candidates) != 1:
                        return None, "document_hydration_search_receipt_ambiguous"
            if len(dict.fromkeys(requested_hydration_ids)) > resolved_budget.max_hydration_nodes_per_review:
                return None, "hydration_node_budget_exhausted"
        if answer_claim_ids - affected_by_search:
            return None, (
                "clarification_claim_requires_search:"
                + ",".join(sorted(answer_claim_ids - affected_by_search))
            )
        if repairing_missing_first_search and (
            status != "continue"
            or payload.get("questions")
            or payload.get("decisions")
            or any(str(item.get("tool_name") or "") != "search_brain" for item in normalized_calls)
        ):
            return None, "first_search_repair_scope_invalid"
        if repairing_empty_continue:
            if status == "continue" and not normalized_calls:
                return None, "continue_repair_action_required"
            if status == "needs_clarification" and (normalized_calls or not payload.get("questions") or payload.get("decisions")):
                return None, "continue_repair_action_invalid"
            if status == "complete" and (normalized_calls or payload.get("questions") or not payload.get("decisions")):
                return None, "continue_repair_action_invalid"
        if not receipts:
            if status != "continue" or not search_tool_calls or any(
                str(item.get("tool_name") or "") != "search_brain" for item in normalized_calls
            ):
                return None, "first_turn_requires_search_wave"
            if set(claims_by_id) - affected_by_search:
                return None, "first_search_wave_missing_claims"

        latest_turn_questions = []
        raw_question_values = list(payload.get("questions") or [])
        raw_turn_questions = _dicts(raw_question_values, limit=25)
        pending_existing_count = sum(
            1
            for item in normalized_questions
            if str(item.get("answer_state") or "unanswered") == "unanswered"
        )
        if len(raw_turn_questions) != len(raw_question_values):
            return None, "question_budget_exhausted"
        if pending_existing_count + len(raw_turn_questions) > resolved_question_limit:
            return None, "question_budget_exhausted"
        raw_turn_question_id_counts: dict[str, int] = {}
        for raw_question in raw_turn_questions:
            raw_question_id = str(raw_question.get("question_id") or "").strip()
            if raw_question_id:
                raw_turn_question_id_counts[raw_question_id] = raw_turn_question_id_counts.get(raw_question_id, 0) + 1
        duplicate_provider_question_ids = {
            question_id
            for question_id, count in raw_turn_question_id_counts.items()
            if count > 1
        }
        existing_questions_by_id = {
            str(item.get("question_id") or ""): item
            for item in normalized_questions
            if str(item.get("question_id") or "")
        }
        existing_questions_by_fingerprint: dict[str, list[dict[str, Any]]] = {}
        for question in normalized_questions:
            existing_questions_by_fingerprint.setdefault(
                _question_content_fingerprint(question),
                [],
            ).append(question)
        reserved_question_ids = set(existing_questions_by_id)
        turn_question_ids: set[str] = set()
        turn_question_fingerprints_by_provider_id: dict[str, set[str]] = {}
        for raw_question in raw_turn_questions:
            provider_question_id = str(raw_question.get("question_id") or "").strip()
            question_id = provider_question_id
            affected = _strings(raw_question.get("affected_claim_ids"), limit=128)
            if not question_id or any(item not in claims_by_id for item in affected) or not affected:
                return None, "question_binding_invalid"
            effect = str(raw_question.get("decision_effect") or "")
            if effect not in QUESTION_DECISION_EFFECTS:
                return None, "question_decision_effect_invalid"
            for claim_id in affected:
                if not any(
                    claim_id in list(receipt.get("affected_claim_ids") or [])
                    and _receipt_question_usable(receipt)
                    for receipt in receipts
                ):
                    return None, f"question_before_usable_search_receipt:{claim_id}"
            bound_receipts = [
                receipt
                for receipt in receipts
                if any(claim_id in list(receipt.get("affected_claim_ids") or []) for claim_id in affected)
                and _receipt_question_usable(receipt)
            ]
            allowed_evidence_refs = {
                str(ref)
                for receipt in bound_receipts
                for ref in _strings(
                    _receipt_question_evidence_authority(receipt).get(
                        "allowed_evidence_refs"
                    ),
                    limit=512,
                )
                if str(ref or "").strip()
            }
            evidence_refs = _strings(raw_question.get("evidence_refs"), limit=32)
            human_fields = (
                "source_claim_summary",
                "brain_evidence_summary",
                "comparison",
                "reason",
                "impact",
                "next_step",
            )
            if any(not str(raw_question.get(field) or "").strip() for field in human_fields):
                return None, "question_human_explanation_required"
            if not str(raw_question.get("question_text") or "").strip():
                return None, "question_text_required"
            if not evidence_refs or any(ref not in allowed_evidence_refs for ref in evidence_refs):
                return None, "question_evidence_refs_invalid"
            question_fingerprint = _question_content_fingerprint(raw_question)
            prior_turn_fingerprints = turn_question_fingerprints_by_provider_id.setdefault(
                provider_question_id,
                set(),
            )
            if question_fingerprint in prior_turn_fingerprints:
                return None, "duplicate_question_id"
            prior_turn_fingerprints.add(question_fingerprint)
            canonicalized_question_id = False
            existing_question = existing_questions_by_id.get(provider_question_id)
            equivalent_questions = existing_questions_by_fingerprint.get(
                question_fingerprint,
                [],
            )
            if equivalent_questions:
                closed_equivalent = next(
                    (
                        question
                        for question in equivalent_questions
                        if str(question.get("answer_state") or "unanswered")
                        in {"answered", "deferred"}
                    ),
                    None,
                )
                canonical_existing = closed_equivalent or equivalent_questions[0]
                canonical_existing_id = str(canonical_existing.get("question_id") or provider_question_id)
                if closed_equivalent is not None:
                    return None, f"answered_question_reemitted:{canonical_existing_id}"
                return None, f"known_question_reemitted:{canonical_existing_id}"
            closed_gap_candidates = [
                question
                for question in normalized_questions
                if str(question.get("answer_state") or "unanswered") in {"answered", "deferred"}
                and _same_closed_gap_verifier_scope(
                    {
                        **raw_question,
                        "affected_claim_ids": affected,
                        "evidence_refs": evidence_refs,
                    },
                    question,
                )
            ]
            if closed_gap_candidates:
                if provider is None:
                    return None, "closed_clarification_gap_verifier_unavailable"
                try:
                    closed_gap_detail, closed_gap_ledger = _closed_clarification_gap_reopened(
                        provider=provider,
                        candidate_question={**raw_question, "affected_claim_ids": affected},
                        closed_questions=closed_gap_candidates,
                        investigation_id=investigation_id,
                        turn=turn,
                        deadline=deadline,
                        brain_revision=normalized_revision,
                        parent_operation_id=resolved_parent_id,
                        call_ordinal=len(new_execution_ledger) + 1,
                    )
                except RuntimeError as exc:
                    return None, f"closed_clarification_gap_verifier_failed:{str(exc)[:160]}"
                if closed_gap_ledger:
                    new_execution_ledger.append(closed_gap_ledger)
                if closed_gap_detail:
                    return None, closed_gap_detail
            if existing_question is not None or provider_question_id in duplicate_provider_question_ids:
                question_id = _server_question_id(
                    investigation_id=investigation_id,
                    turn=turn,
                    provider_question_id=provider_question_id,
                    question=raw_question,
                    reserved_ids=reserved_question_ids | turn_question_ids,
                )
                canonicalized_question_id = True
            question_record = {
                "schema_version": GROW_CLARIFICATION_SET_SCHEMA_VERSION,
                **raw_question,
                "question_id": question_id,
                "affected_claim_ids": affected,
                "evidence_refs": evidence_refs,
                "answer_state": "unanswered",
                "answer": None,
            }
            if canonicalized_question_id:
                question_record["provider_question_id"] = provider_question_id
                question_record["question_id_canonicalized"] = True
            latest_turn_questions.append(question_record)
            turn_question_ids.add(question_id)
        if status == "needs_clarification" and not latest_turn_questions:
            return None, "needs_clarification_requires_questions"
        if status != "needs_clarification" and latest_turn_questions:
            return None, "questions_require_needs_clarification_status"

        latest_turn_decisions = []
        for raw_decision in _dicts(payload.get("decisions"), limit=resolved_budget.max_claims):
            claim_id = str(raw_decision.get("claim_id") or "").strip()
            decision = str(raw_decision.get("decision") or "").strip()
            receipt_ids = _strings(raw_decision.get("evidence_receipt_ids"), limit=16)
            targets = _strings(raw_decision.get("target_node_ids"), limit=32)
            if claim_id not in claims_by_id or decision not in DECISION_VALUES:
                return None, "claim_decision_binding_invalid"
            if str(claims_by_id[claim_id].get("status") or "") == "SEARCHING":
                return None, f"decision_requires_post_clarification_search:{claim_id}"
            if any(
                receipt_id not in receipts_by_id and receipt_id not in document_receipts_by_id
                for receipt_id in receipt_ids
            ):
                return None, "claim_decision_receipt_invalid"
            if not receipt_ids:
                return None, "claim_decision_receipt_required"
            receipt_node_ids = {
                str(node_id)
                for receipt_id in receipt_ids
                for node_id in list(
                    _dict(receipts_by_id.get(receipt_id) or document_receipts_by_id.get(receipt_id)).get("evidence_node_ids") or []
                )
            }
            allowed_evidence_refs = {str(item) for item in [*receipt_ids, *receipt_node_ids] if str(item or "").strip()}
            evidence_refs = _strings(raw_decision.get("evidence_refs"), limit=32)
            human_fields = (
                "source_claim_summary",
                "brain_evidence_summary",
                "comparison",
                "reason",
                "impact",
                "next_step",
            )
            if any(not str(raw_decision.get(field) or "").strip() for field in human_fields):
                return None, "decision_human_explanation_required"
            if decision == "defer" and (
                raw_decision.get("no_clarification_can_change_decision") is not True
                or not str(
                    raw_decision.get("no_clarification_explanation") or ""
                ).strip()
            ):
                return None, "defer_requires_no_clarification_attestation"
            if not evidence_refs or any(ref not in allowed_evidence_refs for ref in evidence_refs):
                return None, "decision_evidence_refs_invalid"
            latest_turn_decisions.append(
                {
                    "schema_version": GROW_CLAIM_DECISION_SCHEMA_VERSION,
                    **raw_decision,
                    "claim_id": claim_id,
                    "decision": decision,
                    "target_node_ids": targets,
                    "evidence_receipt_ids": receipt_ids,
                    "evidence_refs": evidence_refs,
                }
            )
            if decision in TARGET_DECISIONS and any(
                node_id in receipt_node_ids and node_id not in hydrated_evidence
                for node_id in targets
            ):
                return None, f"decision_target_hydration_required:{claim_id}"
        if repairing_closed_clarification_gap_reopened:
            required_decision_claim_ids = set(closed_clarification_gap_reopened_claim_ids)
            provided_decision_claim_ids = {
                str(decision.get("claim_id") or "")
                for decision in latest_turn_decisions
                if str(decision.get("claim_id") or "")
            }
            if not required_decision_claim_ids:
                return None, "closed_clarification_gap_repair_claim_scope_missing"
            if provided_decision_claim_ids != required_decision_claim_ids:
                return None, "closed_clarification_gap_repair_decision_scope_invalid"
            question_only_claim_set = set(question_only_claim_ids)
            if any(
                str(decision.get("claim_id") or "") in question_only_claim_set
                and str(decision.get("decision") or "") != "defer"
                for decision in latest_turn_decisions
            ):
                return None, "closed_clarification_gap_repair_requires_defer"
        if repairing_decision_target_hydration:
            repair_targets = set(decision_target_hydration_target_ids)
            attempted_target_claim_ids = [
                str(decision.get("claim_id") or "")
                for decision in latest_turn_decisions
                if str(decision.get("decision") or "") in TARGET_DECISIONS
                and any(
                    node_id in repair_targets
                    for node_id in _strings(decision.get("target_node_ids"), limit=32)
                )
            ]
            repair_claim_id = (
                attempted_target_claim_ids[0]
                if attempted_target_claim_ids
                else decision_target_hydration_claim_ids[0]
                if decision_target_hydration_claim_ids
                else "unknown"
            )
            repair_hydration_available = bool(
                receipts
                and current_review_state_key() not in hydration_review_state_keys
                and repair_targets
            )
            requested_repair_targets = {
                node_id
                for call in hydration_tool_calls
                for node_id in _strings(
                    _dict(call.get("arguments")).get("node_ids"),
                    limit=resolved_budget.max_evidence_references,
                )
            }
            if repair_hydration_available:
                if status == "continue":
                    if (
                        not hydration_tool_calls
                        or len(hydration_tool_calls) != len(normalized_calls)
                        or payload.get("questions")
                        or payload.get("decisions")
                        or not requested_repair_targets
                        or not requested_repair_targets.issubset(repair_targets)
                    ):
                        return None, f"decision_target_hydration_repair_invalid:{repair_claim_id}"
                elif status == "needs_clarification":
                    if normalized_calls or payload.get("decisions") or not payload.get("questions"):
                        return None, f"decision_target_hydration_repair_invalid:{repair_claim_id}"
                else:
                    return None, f"decision_target_hydration_required:{repair_claim_id}"
            elif status == "needs_clarification":
                if normalized_calls or payload.get("decisions") or not payload.get("questions"):
                    return None, f"decision_target_hydration_repair_invalid:{repair_claim_id}"
            else:
                return None, f"decision_target_hydration_repair_invalid:{repair_claim_id}"
        if merged_review_claims is not None:
            claims = merged_review_claims
            claims_by_id = {str(item.get("claim_id") or ""): item for item in claims}
        return payload, None

    def execute_search_call(
        call: dict[str, Any],
        wave: int,
        batch: _GrowSearchBatchGeneration,
        review_reserve_seconds: float,
    ) -> dict[str, Any]:
        nonlocal search_call_count
        nonlocal pre_search_provider_repair_calls
        nonlocal reserved_search_calls
        arguments = _dict(call.get("arguments"))
        claim_ids = sorted(_strings(arguments.get("affected_claim_ids"), limit=128))
        query_text = str(arguments.get("query_text") or "").strip()
        mode = _child_search_mode_for_budget(resolved_budget)
        requested_limit = 24
        first_wave = wave == 1
        limit = 24
        provider_call_id = str(call.get("provider_call_ref") or "").strip()
        protocol_server_call_id = str(call.get("call_id") or "").strip()
        investigator_candidate_query_sha256 = stable_digest(query_text)
        investigator_candidate_query_reuse_sha256 = stable_digest(
            " ".join(query_text.split()).casefold()
        )

        def require_active(stage: str) -> None:
            with search_state_lock:
                if batch.cancelled:
                    raise RuntimeError(
                        batch.cancellation_detail
                        or f"grow_search_batch_cancelled:generation={batch.generation};stage={stage}"
                    )
                if batch.brain_revision != normalized_revision:
                    raise RuntimeError("grow_search_batch_brain_revision_stale")

        worker_deadline = _GrowDeadline(
            started_monotonic=deadline.started_monotonic,
            deadline_monotonic=deadline.deadline_monotonic,
            provider_timeout_seconds=deadline.provider_timeout_seconds,
            monotonic_clock=clock,
            cancellation_check=require_active,
        )
        search_deadline = _GrowDeadline(
            started_monotonic=deadline.started_monotonic,
            deadline_monotonic=(
                deadline.deadline_monotonic - max(0.0, review_reserve_seconds)
            ),
            provider_timeout_seconds=deadline.provider_timeout_seconds,
            monotonic_clock=clock,
            cancellation_check=require_active,
        )
        reserved_signature: str | None = None
        committed = False

        def release_reservation() -> None:
            nonlocal reserved_search_calls
            if reserved_signature is None or committed:
                return
            with search_state_lock:
                inflight_query_claim_signatures.discard(reserved_signature)
                reserved_search_calls = max(0, reserved_search_calls - 1)

        worker_deadline.require(
            "grow_search_query_binding",
            cap_seconds=resolved_budget.wall_budget_seconds,
        )
        verified_anchors, _, _ = _server_bound_subject_material(
            claims=claims,
            affected_claim_ids=claim_ids,
        )
        if provider is None:
            raise RuntimeError("isolated_query_provider_unavailable")
        query_text, isolated_purpose, isolated_boundary, boundary_ledger = (
            _compose_source_blind_search_query(
                provider=provider,
                subject_anchors=verified_anchors,
                query_strategy=_dict(arguments.get("query_strategy")),
                deadline=worker_deadline,
                wave=wave,
                brain_revision=normalized_revision,
                parent_operation_id=resolved_parent_id,
                investigation_id=investigation_id,
                max_repairs=resolved_budget.max_repairs,
                query_authority_timeout_seconds=max(
                    4.0,
                    min(12.0, float(resolved_budget.provider_timeout_seconds)),
                ),
            )
        )
        with search_state_lock:
            new_execution_ledger.extend(boundary_ledger)
        pre_search_provider_repair_calls += int(
            isolated_boundary.get("pre_search_provider_repair_call_count")
            or 0
        )
        query_review, _ = _build_server_bound_search_query(
            query_text=query_text,
            purpose=isolated_purpose,
            query_strategy=_dict(arguments.get("query_strategy")),
            claims=claims,
            affected_claim_ids=claim_ids,
            first_wave=first_wave,
        )
        query_review["isolated_query_boundary"] = isolated_boundary
        semantic_firewall = _dict(query_review.get("semantic_firewall"))
        semantic_firewall.update(
            {
                "schema_version": "agvm.grow_search_semantic_firewall.v7",
                "investigator_query_visible_to_search_runtime": False,
                "source_blind_query_authority_used": True,
                "single_provider_query_authority": True,
                "isolated_query_boundary_sha256": stable_digest(isolated_boundary),
                "pre_search_provider_repair_call_count": int(
                    isolated_boundary.get("pre_search_provider_repair_call_count")
                    or 0
                ),
            }
        )
        query_review["semantic_firewall"] = semantic_firewall
        worker_deadline.require(
            "grow_search_query_binding",
            cap_seconds=resolved_budget.wall_budget_seconds,
        )
        query_text = str(query_review["query_text"])
        arguments["query_text"] = query_text
        arguments["purpose"] = str(query_review["purpose"])
        server_call_authority_sha256 = stable_digest(
            {
                "investigation_id": investigation_id,
                "run_call_authority_sha256": run_call_authority_sha256,
                "batch_generation": batch.generation,
                "brain_revision": normalized_revision,
                "wave": wave,
                "query": " ".join(query_text.split()).casefold(),
                "claim_ids": claim_ids,
                "mode": mode,
                "limit": limit,
            }
        )
        child_call_id = (
            f"grow-search::{investigation_id}::wave-{wave}::"
            f"{server_call_authority_sha256[:20]}"
        )
        signature = stable_digest(
            {"query": query_text.casefold(), "claim_ids": claim_ids}
        )
        idempotency_key = stable_digest(
            {
                "investigation_id": investigation_id,
                "server_call_authority_sha256": server_call_authority_sha256,
                "brain_revision": normalized_revision,
                "query": query_text,
                "claim_ids": claim_ids,
                "mode": mode,
                "limit": limit,
            }
        )
        try:
            search_deadline.require(
                "grow_child_search",
                cap_seconds=resolved_budget.wall_budget_seconds,
            )
            with search_state_lock:
                if batch.cancelled or batch.brain_revision != normalized_revision:
                    raise RuntimeError(
                        batch.cancellation_detail or "grow_search_batch_cancelled"
                    )
                if search_deadline.remaining_seconds() <= 0.0:
                    raise RuntimeError(
                        "grow_wall_budget_exhausted:stage=grow_child_search;"
                        "remaining_seconds=0.000000;reserve_seconds=0.000000"
                    )
                persisted = receipts_by_idempotency.get(idempotency_key)
                if persisted is not None:
                    return dict(persisted)
                if (
                    signature in query_claim_signatures
                    or signature in inflight_query_claim_signatures
                ):
                    raise RuntimeError("repeated_equivalent_query")
                if (
                    search_call_count + reserved_search_calls
                    >= resolved_budget.max_search_calls
                ):
                    raise RuntimeError("search_call_budget_exhausted")
                inflight_query_claim_signatures.add(signature)
                reserved_search_calls += 1
                reserved_signature = signature
            search_deadline.require(
                "grow_child_search",
                cap_seconds=resolved_budget.wall_budget_seconds,
            )
            receipt = dict(
                search_runner(
                    graph_snapshot,
                    normalized_revision,
                    query_text,
                    mode,
                    limit,
                    resolved_correlation_id,
                    brain_id=resolved_brain_id or None,
                    semantic_authority=authority,
                    parent_operation_id=resolved_parent_id,
                    child_call_id=child_call_id,
                    billing_scope="parent_grow_preview",
                    idempotency_key=idempotency_key,
                    deadline_at_ms=batch.deadline_at_ms,
                )
            )
            receipt_brain_id = str(receipt.get("brain_id") or "").strip()
            if resolved_brain_id and receipt_brain_id and receipt_brain_id != resolved_brain_id:
                raise RuntimeError("search_receipt_brain_mismatch")
            if resolved_brain_id:
                receipt["brain_id"] = resolved_brain_id
            search_deadline.require(
                "grow_child_search",
                cap_seconds=resolved_budget.wall_budget_seconds,
            )
            receipt["affected_claim_ids"] = claim_ids
            receipt["purpose"] = str(arguments.get("purpose") or "")
            receipt["query_strategy"] = _dict(arguments.get("query_strategy"))
            receipt["query_strategy_diagnostic"] = _dict(
                arguments.get("query_strategy_diagnostic")
            )
            receipt["query_review"] = query_review
            receipt["investigator_candidate_query_sha256"] = (
                investigator_candidate_query_sha256
            )
            receipt["investigator_candidate_query_reuse_sha256"] = (
                investigator_candidate_query_reuse_sha256
            )
            receipt["candidate_query_reuse_sha256"] = stable_digest(
                " ".join(query_text.split()).casefold()
            )
            receipt["requested_max_matches"] = requested_limit
            receipt["grow_receipt_authority_sha256"] = grow_receipt_authority_sha256
            receipt["wave"] = wave
            receipt["provider_call_id"] = provider_call_id
            receipt["provider_call_ref"] = provider_call_id
            receipt["protocol_server_call_id"] = protocol_server_call_id
            receipt["server_call_id"] = child_call_id
            receipt["server_call_authority_sha256"] = server_call_authority_sha256
            receipt["idempotency_key"] = idempotency_key
            if str(receipt.get("brain_revision") or "") != normalized_revision:
                raise RuntimeError("search_receipt_revision_mismatch")
            evidence_ids = _strings(
                receipt.get("evidence_node_ids"),
                limit=resolved_budget.max_evidence_references,
            )
            receipt["evidence_node_ids"] = evidence_ids
            if _is_v3_search_receipt(receipt) and not any(
                key in receipt
                for key in (
                    "evidence_usable",
                    "question_usable",
                    "decision_usable",
                    "novelty_certified",
                    "reviewable",
                )
            ):
                receipt["usable"] = False
            receipt["grow_target_mutation_usable"] = bool(
                _receipt_decision_usable(receipt) and evidence_ids
            )
            receipt_id = str(receipt.get("receipt_id") or "").strip()
            search_ledger_entry = execution_ledger_entry(
                role="search",
                call_id=provider_call_id,
                child_call_id=child_call_id,
                parent_operation_id=resolved_parent_id,
                billing_scope="parent_grow_preview",
                idempotency_key=idempotency_key,
                wave=wave,
                brain_revision=normalized_revision,
                attestation=_dict(receipt.get("search_execution_attestation")),
            )
            with search_state_lock:
                if batch.cancelled or batch.brain_revision != normalized_revision:
                    raise RuntimeError(
                        batch.cancellation_detail or "grow_search_batch_cancelled"
                    )
                if search_deadline.remaining_seconds() <= 0.0:
                    raise RuntimeError(
                        "grow_wall_budget_exhausted:stage=grow_child_search;"
                        "remaining_seconds=0.000000;reserve_seconds=0.000000"
                    )
                if str(receipt.get("brain_revision") or "") != batch.brain_revision:
                    raise RuntimeError("search_receipt_revision_mismatch")
                evidence_union = {
                    str(node_id)
                    for item in receipts
                    for node_id in list(item.get("evidence_node_ids") or [])
                }
                evidence_union.update(evidence_ids)
                if len(evidence_union) > resolved_budget.max_evidence_references:
                    raise RuntimeError("evidence_reference_budget_exhausted")
                if not receipt_id:
                    raise RuntimeError("search_receipt_id_missing")
                if receipt_id in receipts_by_id:
                    raise RuntimeError("duplicate_search_receipt_id")
                receipts.append(receipt)
                receipts_by_id[receipt_id] = receipt
                receipts_by_idempotency[idempotency_key] = receipt
                for claim_id in claim_ids:
                    claim = claims_by_id[claim_id]
                    claim["search_receipt_ids"] = _strings(
                        [*list(claim.get("search_receipt_ids") or []), receipt_id],
                        limit=16,
                    )
                    claim["status"] = "AI_REVIEW"
                new_execution_ledger.append(search_ledger_entry)
                inflight_query_claim_signatures.discard(signature)
                query_claim_signatures.add(signature)
                reserved_search_calls = max(0, reserved_search_calls - 1)
                search_call_count += 1
                committed = True
            return receipt
        finally:
            release_reservation()

    def execute_hydration(call: dict[str, Any], wave: int) -> dict[str, Any]:
        arguments = _dict(call.get("arguments"))
        node_ids = _strings(arguments.get("node_ids"), limit=resolved_budget.max_evidence_references)
        allowed_node_ids = {
            str(node_id)
            for receipt in receipts
            for node_id in list(receipt.get("evidence_node_ids") or [])
        }
        if any(node_id not in allowed_node_ids for node_id in node_ids):
            raise RuntimeError("hydration_node_not_returned_by_search")
        missing_fresh = [node_id for node_id in node_ids if node_id not in hydrated_evidence]
        raw_result = (hydrate_runner or _default_hydrate)(missing_fresh, graph_snapshot)
        if isinstance(raw_result, Mapping):
            hydrated_nodes = _dicts(raw_result.get("nodes"), limit=resolved_budget.max_evidence_references)
            missing = _strings(raw_result.get("missing_node_ids"), limit=resolved_budget.max_evidence_references)
        else:
            hydrated_nodes = _dicts(raw_result, limit=resolved_budget.max_evidence_references)
            missing = []
        if missing:
            raise RuntimeError(f"hydration_target_missing:{','.join(missing)}")
        for material in hydrated_nodes:
            node_id = str(material.get("node_id") or material.get("id") or "").strip()
            if node_id not in missing_fresh:
                raise RuntimeError("hydration_result_node_binding_invalid")
            temporal_context = _dict(material.get("temporal_context"))
            temporal_lifecycle = _dict(temporal_context.get("lifecycle"))
            normalized = {
                "node_id": node_id,
                "canonical_text": str(material.get("canonical_text") or material.get("raw_text") or "")[:8_000],
                "summary": str(material.get("summary") or "")[:2_000],
                "claim_status": str(material.get("claim_status") or "fact"),
                "source_trust": str(material.get("source_trust") or "unknown"),
                "created_at": material.get("created_at"),
                "updated_at": material.get("updated_at"),
                "ingested_at": material.get("ingested_at"),
                "node_revision": (
                    material.get("node_revision")
                    or temporal_context.get("node_revision")
                    or temporal_lifecycle.get("node_revision")
                ),
                "temporal_role": material.get("temporal_role") or temporal_context.get("temporal_role"),
                "observed_at": material.get("observed_at") or temporal_context.get("observed_at"),
                "valid_from": material.get("valid_from") or temporal_context.get("valid_from"),
                "valid_to": material.get("valid_to") or temporal_context.get("valid_to"),
                "lifecycle_status": str(material.get("lifecycle_status") or "active"),
                "superseded_at": material.get("superseded_at") or temporal_context.get("superseded_at"),
                "superseded_by": material.get("superseded_by") or temporal_context.get("superseded_by"),
                "memory_type": str(material.get("memory_type") or ""),
                "provenance": _dict(material.get("provenance")),
            }
            normalized["digest"] = str(material.get("digest") or stable_digest(normalized))
            hydrated_evidence[node_id] = normalized
        attestation = {
            "schema_version": "agvm.hydration_execution_attestation.v1",
            "status": "completed",
            "provider_executed": False,
            "runtime_executed": True,
            "node_ids": node_ids,
            "result_sha256": stable_digest([hydrated_evidence[node_id] for node_id in node_ids]),
            "usage": {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0},
        }
        new_execution_ledger.append(
            execution_ledger_entry(
                role="hydration",
                call_id=str(call.get("call_id") or ""),
                wave=wave,
                brain_revision=normalized_revision,
                parent_operation_id=resolved_parent_id,
                attestation=attestation,
            )
        )
        return {
            "schema_version": "agvm.grow_hydrated_evidence.v1",
            "server_call_id": str(call.get("call_id") or ""),
            "provider_call_ref": str(call.get("provider_call_ref") or ""),
            "node_ids": node_ids,
            "nodes": [hydrated_evidence[node_id] for node_id in node_ids],
            "usable": True,
            "result_sha256": attestation["result_sha256"],
        }

    def execute_document_hydration(call: dict[str, Any], wave: int) -> dict[str, Any]:
        arguments = _dict(call.get("arguments"))
        claim_ids = sorted(_strings(arguments.get("affected_claim_ids"), limit=128))
        document_ids = _strings(
            arguments.get("node_ids"),
            limit=resolved_budget.max_documents_per_call,
        )
        candidates = _document_hydration_candidate_receipts(
            receipts,
            claim_ids=claim_ids,
            document_ids=document_ids,
        )
        if not candidates:
            raise RuntimeError("document_hydration_not_discovered_by_search")
        if len(candidates) != 1:
            raise RuntimeError("document_hydration_search_receipt_ambiguous")
        source_receipt = candidates[0]
        result = dict(
            document_hydrate_runner(
                source_receipt,
                normalized_revision,
                document_ids,
                max_documents=resolved_budget.max_documents_per_call,
                max_children_per_document=resolved_budget.max_document_children,
                max_total_chars=resolved_budget.max_document_chars,
            )
        )
        if str(result.get("brain_revision") or "") != normalized_revision:
            raise RuntimeError("document_hydration_revision_mismatch")
        receipt_id = str(result.get("receipt_id") or "").strip()
        if not receipt_id or result.get("usable") is not True:
            raise RuntimeError("document_hydration_receipt_unusable")
        result.update(
            {
                "affected_claim_ids": claim_ids,
                "provider_call_id": str(call.get("provider_call_ref") or ""),
                "provider_call_ref": str(call.get("provider_call_ref") or ""),
                "server_call_id": str(call.get("call_id") or ""),
                "wave": wave,
            }
        )
        if receipt_id not in document_receipts_by_id:
            document_receipts.append(result)
            document_receipts_by_id[receipt_id] = result
        for claim_id in claim_ids:
            claim = claims_by_id[claim_id]
            claim["document_evidence_receipt_ids"] = _strings(
                [*list(claim.get("document_evidence_receipt_ids") or []), receipt_id],
                limit=16,
            )
            claim["status"] = "AI_REVIEW"
        for material in _dicts(result.get("nodes"), limit=resolved_budget.max_evidence_references):
            node_id = str(material.get("node_id") or "").strip()
            if node_id:
                hydrated_evidence[node_id] = dict(material)
        attestation = {
            "schema_version": "agvm.document_hydration_execution_attestation.v1",
            "status": "completed",
            "provider_executed": False,
            "runtime_executed": True,
            "search_receipt_id": source_receipt.get("receipt_id"),
            "result_sha256": result.get("result_sha256"),
            "usage": {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0},
        }
        new_execution_ledger.append(
            execution_ledger_entry(
                role="document_hydration",
                call_id=str(call.get("call_id") or ""),
                wave=wave,
                brain_revision=normalized_revision,
                parent_operation_id=resolved_parent_id,
                attestation=attestation,
            )
        )
        return result

    def tool_batch_executor(calls: list[dict[str, Any]], _turn: int) -> list[dict[str, Any]]:
        nonlocal search_wave, hydration_wave, search_batch_generation
        deadline.require("grow_tool_batch")
        search_calls = [call for call in calls if str(call.get("tool_name") or "") == "search_brain"]
        hydration_calls = [call for call in calls if str(call.get("tool_name") or "") == "hydrate_memory_objects"]
        document_hydration_calls = [
            call for call in calls if str(call.get("tool_name") or "") == "hydrate_document_evidence"
        ]
        if search_calls and (hydration_calls or document_hydration_calls):
            raise RuntimeError("search_and_hydration_require_separate_waves")
        results_by_call_id: dict[str, dict[str, Any]] = {}
        if search_calls:
            search_wave += 1
            child_search_mode = _child_search_mode_for_budget(resolved_budget)
            child_search_cap_seconds = _child_search_budget_cap_seconds(child_search_mode)
            # A child Search must not consume the parent investigation's whole
            # wall clock: Grow still needs a provider turn to compare the
            # receipt and ask/decide.  The reserve is authoritative; if it no
            # longer fits, fail closed instead of silently shrinking it.
            review_reserve = max(
                0.0, float(resolved_budget.ai_review_reserve_seconds)
            )
            search_seconds = deadline.require(
                "grow_child_search_batch",
                reserve_seconds=review_reserve,
                cap_seconds=child_search_cap_seconds,
            )
            deadline_at_ms = int((time.time() + search_seconds) * 1000)
            with search_state_lock:
                search_batch_generation += 1
                batch = _GrowSearchBatchGeneration(
                    generation=search_batch_generation,
                    brain_revision=normalized_revision,
                    deadline_at_ms=deadline_at_ms,
                )
            executor = ThreadPoolExecutor(
                max_workers=min(resolved_budget.search_concurrency, len(search_calls))
            )
            batch_succeeded = False
            try:
                futures = [
                    (
                        call,
                        executor.submit(
                            copy_context().run,
                            execute_search_call,
                            call,
                            search_wave,
                            batch,
                            review_reserve,
                        ),
                    )
                    for call in search_calls
                ]
                for call, future in futures:
                    try:
                        if future.done():
                            # Surface the child stage's precise failure instead
                            # of replacing it with a generic batch timeout.
                            result = future.result()
                        else:
                            wait_seconds = deadline.require(
                                "grow_child_search_batch",
                                reserve_seconds=review_reserve,
                                cap_seconds=child_search_cap_seconds,
                            )
                            result = future.result(timeout=wait_seconds)
                    except FuturesTimeoutError as exc:
                        timeout_detail = (
                            "grow_wall_budget_exhausted:stage=grow_child_search;"
                            "remaining_seconds=0.000000;"
                            f"reserve_seconds={review_reserve:.6f}"
                        )
                        with search_state_lock:
                            batch.cancelled = True
                            batch.cancellation_detail = timeout_detail
                        raise RuntimeError(timeout_detail) from exc
                    results_by_call_id[str(call.get("call_id") or "")] = result
                batch_succeeded = True
            finally:
                if not batch_succeeded:
                    with search_state_lock:
                        batch.cancelled = True
                        if batch.cancellation_detail is None:
                            batch.cancellation_detail = (
                                "grow_search_batch_cancelled:"
                                f"generation={batch.generation}"
                            )
                executor.shutdown(wait=False, cancel_futures=True)
        else:
            hydration_wave += 1
            hydration_state_key = current_review_state_key()
            for call in [*hydration_calls, *document_hydration_calls]:
                deadline.require("grow_evidence_hydration")
                if str(call.get("tool_name") or "") == "hydrate_document_evidence":
                    result = execute_document_hydration(call, hydration_wave)
                else:
                    result = execute_hydration(call, hydration_wave)
                deadline.require("grow_evidence_hydration")
                results_by_call_id[str(call.get("call_id") or "")] = result
            if hydration_calls or document_hydration_calls:
                hydration_review_state_keys.add(hydration_state_key)
        ordered_results = [results_by_call_id[str(call.get("call_id") or "")] for call in calls]
        return ordered_results

    def final_validator(payload: dict[str, Any]) -> str | None:
        nonlocal normalized_decisions, normalized_questions
        status = str(payload.get("status") or "")
        if latest_turn_questions:
            existing_ids = {str(item.get("question_id") or "") for item in normalized_questions}
            if any(str(item.get("question_id") or "") in existing_ids for item in latest_turn_questions):
                return "duplicate_question_id"
            normalized_questions.extend(latest_turn_questions)
            for question in latest_turn_questions:
                for claim_id in list(question.get("affected_claim_ids") or []):
                    claims_by_id[claim_id]["status"] = "NEEDS_CLARIFICATION"
        validated_decisions: list[dict[str, Any]] = []
        for decision in latest_turn_decisions:
            claim_id = str(decision.get("claim_id") or "")
            decision_value = str(decision.get("decision") or "")
            receipt_ids = _strings(decision.get("evidence_receipt_ids"), limit=16)
            target_ids = _strings(decision.get("target_node_ids"), limit=32)
            all_receipts_by_id = {**receipts_by_id, **document_receipts_by_id}
            bound_receipts = [all_receipts_by_id[item] for item in receipt_ids]
            if any(claim_id not in list(item.get("affected_claim_ids") or []) for item in bound_receipts):
                return f"decision_receipt_claim_binding_invalid:{claim_id}"
            source_only_authorized = source_only_decision_authorized(decision)
            source_only_override_required = decision_value == "source_only" and any(
                not _receipt_decision_usable(item)
                for item in bound_receipts
            )
            if source_only_override_required and not source_only_authorized:
                return f"source_only_source_bound_authority_invalid:{claim_id}"
            if decision_value != "defer" and not source_only_authorized and any(
                not _receipt_decision_usable(item)
                for item in bound_receipts
            ):
                return f"decision_receipt_not_usable:{claim_id}"
            receipt_node_ids = {
                str(node_id)
                for item in bound_receipts
                for node_id in list(item.get("evidence_node_ids") or [])
            }
            if decision_value in TARGET_DECISIONS:
                if not target_ids or any(node_id not in receipt_node_ids for node_id in target_ids):
                    return f"decision_target_not_in_search_receipt:{claim_id}"
                if any(node_id not in hydrated_evidence for node_id in target_ids):
                    return f"decision_target_not_hydrated:{claim_id}"
            elif target_ids:
                return f"decision_targets_not_allowed:{claim_id}"
            if decision_value == "new_memory":
                novelty_certified = any(
                    _receipt_novelty_certified(item) and _receipt_decision_usable(item)
                    for item in bound_receipts
                )
                compatible_distinct = bool(decision.get("compatible_but_distinct")) and bool(receipt_node_ids) and any(
                    _receipt_evidence_usable(item)
                    for item in bound_receipts
                )
                if not novelty_certified and not compatible_distinct:
                    return f"new_memory_novelty_not_certified:{claim_id}"
            decision_id = f"grow-decision::{stable_digest({'claim_id': claim_id, 'decision': decision_value, 'targets': target_ids, 'receipts': receipt_ids})[:24]}"
            normalized = {**decision, "decision_id": decision_id}
            validated_decisions.append(normalized)
            claims_by_id[claim_id]["status"] = "COMPLETE" if decision_value != "defer" else "DEFERRED"
            claims_by_id[claim_id]["decision_id"] = decision_id
        if validated_decisions:
            replaced = {str(item.get("claim_id") or "") for item in validated_decisions}
            normalized_decisions = [item for item in normalized_decisions if str(item.get("claim_id") or "") not in replaced]
            normalized_decisions.extend(validated_decisions)
        if status == "needs_clarification":
            return None
        if status != "complete":
            return "final_status_invalid"
        pending_required = [
            item
            for item in normalized_questions
            if bool(item.get("required_for_preview"))
            and str(item.get("answer_state") or "unanswered") == "unanswered"
        ]
        if pending_required:
            return "complete_with_required_questions_pending"
        decision_claim_ids = {str(item.get("claim_id") or "") for item in normalized_decisions}
        missing_decisions = sorted(set(claims_by_id) - decision_claim_ids)
        if missing_decisions:
            return f"complete_claim_decisions_missing:{','.join(missing_decisions[:12])}"
        return None

    initial_observations = [
        {
            "resume": True,
            "clarification_answers": answers,
            "persisted_receipt_count": len(receipts),
            "persisted_decision_count": len(decisions),
        }
    ] if existing else []

    provider_for_agent: Provider | None = None
    if provider is not None:
        def provider_for_agent(request: dict[str, Any]) -> Any:
            if (
                decision_receipt_repair_requires_questions
                and str(request.get("schema_name") or "") == "agvm_grow_investigator_v3"
            ):
                request["schema"] = _decision_receipt_question_repair_schema()
            if (
                question_only_action_repair_uses_narrow_schema
                and str(request.get("schema_name") or "") == "agvm_grow_investigator_v3"
            ):
                request["schema"] = _question_only_receipt_action_repair_schema()
            if (
                repairing_closed_clarification_gap_reopened
                and str(request.get("schema_name") or "") == "agvm_grow_investigator_v3"
            ):
                request["schema"] = _closed_gap_decision_repair_schema()
            return provider(request)

    agent_result = run_investigative_agent(
        provider=provider_for_agent,
        schema_name="agvm_grow_investigator_v3",
        schema=GROW_INVESTIGATOR_RESPONSE_SCHEMA,
        role="grow_investigator",
        system_prompt=_system_prompt(),
        context_builder=context_builder,
        turn_validator=turn_validator,
        tool_batch_executor=tool_batch_executor,
        final_validator=final_validator,
        allowed_tool_names=("search_brain", "hydrate_memory_objects", "hydrate_document_evidence"),
        budget=InvestigativeAgentBudget(
            max_turns=resolved_budget.max_turns,
            max_tool_calls=resolved_budget.max_tool_calls,
            max_repairs=resolved_budget.max_repairs,
            max_no_progress_turns=2,
            provider_timeout_seconds=resolved_budget.provider_timeout_seconds,
            notebook_observation_limit=24,
        ),
        initial_observations=initial_observations,
        repair_limits_by_detail={
            "provider_error": 1,
            "provider_timeout": 1,
            "provider_attestation_invalid": 1,
            "grow_investigator_turn_schema_invalid": 1,
            "status_invalid": 1,
            "claims_invalid": 1,
            "exclusions_invalid": 1,
            "tool_calls_invalid": 1,
            "questions_invalid": 1,
            "decisions_invalid": 1,
            "answer_claims_invalid": 1,
            "continue_response_requires_tool_calls": 1,
            "first_turn_requires_search_wave": 1,
            "first_search_wave_missing_claims": 1,
            "search_query_text_missing": 1,
            "search_purpose_missing": 1,
            "search_call_budget_exhausted": 1,
            "needs_clarification_requires_questions": 1,
            "review_action_mixed_tool_and_terminal": 1,
            "review_action_mixed_questions_and_decisions": 1,
            "question_evidence_refs_invalid": 1,
            "document_hydration_not_discovered_by_search": 1,
            "answered_question_reemitted:*": 1,
            "known_question_reemitted:*": 1,
            "closed_clarification_gap_reopened:*": 1,
            "decision_target_hydration_required:*": 1,
            "decision_target_hydration_repair_invalid:*": 1,
            "decision_target_not_hydrated:*": 1,
            "decision_receipt_not_usable:*": 1,
            "question_only_receipt_action_invalid:*": 1,
            "claim_source_span_invalid:*": 1,
            "claim_subject_anchor_invalid:*": 2,
            "temporal_mention_*": 1,
            "temporal_mentions_*": 1,
            "temporal_scope_*": 1,
            "review_claim_source_binding_forbidden:*": 1,
            "tool_call_affected_claim_invalid": 1,
            "review_repeated_equivalent_query": 1,
            "post_answer_search_not_authorized": 1,
            "*": 0,
        },
        deadline_monotonic=deadline.deadline_monotonic,
        monotonic_clock=clock,
        deadline_stage_prefix="grow_investigator",
    )
    new_execution_ledger.extend(_dicts(agent_result.get("ai_execution_ledger"), limit=64))
    execution_ledger = [*prior_execution_ledger, *new_execution_ledger]
    pending_questions = [
        item
        for item in normalized_questions
        if str(item.get("answer_state") or "unanswered") == "unanswered"
    ]
    agent_status = str(agent_result.get("status") or "incomplete")
    raw_failure = _dict(agent_result.get("failure"))
    failure_detail = str(raw_failure.get("detail") or "")
    deadline_failure_stage: str | None = None
    if _deadline_error(failure_detail):
        deadline_failure_stage = failure_detail.split("stage=", 1)[1].split(";", 1)[0]
        failure = {
            "code": "grow_wall_budget_exhausted",
            "detail": (
                "Grow stopped safely because the end-to-end wall-clock budget "
                f"was exhausted during stage '{deadline_failure_stage}'."
            ),
            "stage": deadline_failure_stage,
            "missing_fields": ["stage_completion"],
            "runtime_detail": failure_detail[:1000],
        }
    else:
        failure = raw_failure if agent_status not in {"complete", "needs_clarification"} else None
    is_complete = agent_status == "complete" and not any(
        bool(item.get("required_for_preview")) for item in pending_questions
    )
    compiler_claims = _compiler_claims(claims, normalized_decisions) if is_complete else []
    is_applicable = bool(is_complete and compiler_claims)
    state = "active" if is_complete else ("awaiting_clarification" if agent_status == "needs_clarification" else "investigating")
    status = "COMPLETE" if is_complete else ("NEEDS_CLARIFICATION" if agent_status == "needs_clarification" else "INCOMPLETE")
    aggregate = aggregate_execution_ledger(execution_ledger, complete=is_complete, applicable=is_applicable)
    ledger_digest = stable_digest(claims)
    decisions_digest = stable_digest(normalized_decisions)
    receipts_digest = stable_digest(receipts)
    result = {
        "schema_version": GROW_INVESTIGATION_SCHEMA_VERSION,
        "investigation_id": investigation_id,
        "source_investigation_id": source_id,
        "brain_id": source_investigation.get("brain_id"),
        "brain_revision": normalized_revision,
        "source_sha256": current_source_sha256,
        "graph_snapshot_sha256": current_graph_sha256,
        "version": int(existing.get("version") or 0) + 1,
        **({"resume_token": existing.get("resume_token")} if existing.get("resume_token") else {}),
        "state": state,
        "status": status,
        "claim_ledger": claims,
        "exclusions": exclusions,
        "source_binding_findings": [
            dict(finding)
            for exclusion in exclusions
            if (finding := _dict(exclusion.get("human_finding")))
        ],
        "search_receipts": receipts,
        "document_evidence_receipts": document_receipts,
        "hydrated_evidence": hydrated_evidence,
        "hydration_review_state_keys": sorted(hydration_review_state_keys),
        "decisions": normalized_decisions,
        "questions": normalized_questions,
        "pending_questions": pending_questions,
        "compiler_claims": compiler_claims,
        "compiler_authority": {
            "schema_version": "agvm.grow_compiler_authority.v1",
            "claim_ledger_sha256": ledger_digest,
            "decisions_sha256": decisions_digest,
            "search_receipts_sha256": receipts_digest,
            "document_evidence_receipts_sha256": stable_digest(document_receipts),
            "compiler_claims_sha256": stable_digest(compiler_claims),
            "policy": "claim_id_and_decision_id_bound_no_semantic_reclassification",
        } if is_complete else {},
        "ai_execution_ledger": execution_ledger,
        "ai_execution_attestation": aggregate,
        "semantic_authority": authority,
        "semantic_decision_source": "provider",
        "query_authority": GROW_QUERY_AUTHORITY,
        "semantic_fallback_used": False,
        "usage": {
            **_dict(agent_result.get("usage")),
            **_dict(aggregate.get("usage")),
            "ai_request_count": int(aggregate.get("request_count") or 0),
            "successful_ai_request_count": int(aggregate.get("successful_request_count") or 0),
            "repair_turns": int(_dict(agent_result.get("usage")).get("repair_turns") or 0)
            + pre_search_provider_repair_calls,
        },
        "aggregate_usage": _dict(aggregate.get("usage")),
        "budget": resolved_budget.as_dict(),
        "budget_usage": {
            "search_calls": search_call_count,
            "search_waves": search_wave,
            "hydration_waves": hydration_wave,
            "document_evidence_receipts": len(document_receipts),
            "pre_search_provider_repair_calls": pre_search_provider_repair_calls,
            "evidence_references": len(
                {node_id for item in receipts for node_id in list(item.get("evidence_node_ids") or [])}
            ),
            "wall_elapsed_ms": round((float(clock()) - started_monotonic) * 1000.0, 3),
            "deadline_exhausted": deadline_failure_stage is not None,
            "failure_stage": deadline_failure_stage,
        },
        "summary": _dict(agent_result.get("payload")).get("summary"),
        "complete": is_complete,
        "applicable": is_applicable,
        "failure": failure,
        "deadline": {
            "schema_version": "agvm.grow_deadline.v1",
            "wall_budget_seconds": float(resolved_budget.wall_budget_seconds),
            "status": "exhausted" if deadline_failure_stage else "within_budget",
            "failure_stage": deadline_failure_stage,
        },
        "correlation_id": resolved_correlation_id,
        "parent_operation_id": resolved_parent_id,
        "updated_at": utc_now(),
    }
    result["investigation_sha256"] = stable_digest(
        {
            "investigation_id": investigation_id,
            "source_sha256": current_source_sha256,
            "brain_revision": normalized_revision,
            "claim_ledger_sha256": ledger_digest,
            "decisions_sha256": decisions_digest,
            "search_receipts_sha256": receipts_digest,
            "hydration_review_state_keys_sha256": stable_digest(sorted(hydration_review_state_keys)),
            "state": state,
        }
    )
    return result


__all__ = [
    "GROW_INVESTIGATION_SCHEMA_VERSION",
    "GROW_INVESTIGATOR_RESPONSE_SCHEMA",
    "GrowInvestigationBudget",
    "default_grow_investigator_provider",
    "resolve_grow_investigator_provider",
    "run_grow_investigation",
]
