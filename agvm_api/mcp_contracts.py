# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

from typing import Any

from retrieval_limits import DEFAULT_RETRIEVAL_MAX_MATCHES, MAX_RETRIEVAL_MATCHES

from mcp_tool_registration import (
    LOCAL_CORE_MAINTAIN_CLOUD_HANDOFF_TOOL_NAMES,
    MCP_TOOL_REGISTRATION_STATE,
    build_mcp_module_tool_registration_summary,
    build_mcp_tool_registration,
    build_module_requirement_from_registration,
    local_core_tool_is_discoverable,
    mark_local_core_cloud_handoff_registration,
    validate_mcp_tool_registration,
)


MCP_CONTRACT_REGISTRY_SCHEMA_VERSION = "agvm.mcp_contract_registry.v1"
MCP_TOOL_CONTRACT_SCHEMA_VERSION = "agvm.mcp_tool_contract.v1"
MCP_CONTRACT_REGISTRY_SLICE = "PR-12J-A"
MCP_CONTRACT_IMPLEMENTATION_SLICE = "PR-12J-B"

MCP_OUTPUT_LAWS = [
    "stable_json_first_contract",
    "agent_markdown_optional_and_secondary",
    "no_internal_node_ids_in_primary_prose",
    "structured_trace_may_include_node_and_source_ids",
    "hot_cold_reservoir_and_excluded_material_are_separate",
    "completeness_and_budget_status_are_explicit",
    "no_match_is_returned_as_structured_state",
    "raw_document_or_source_text_is_preserved_when_requested_and_available",
    "answer_demo_is_not_default_mcp_output",
    "first_useful_package_latency_is_reported_separately_from_full_completion",
    "runtime_state_axes_are_split_for_payload_ai_provider_documents_paths_answer_and_run",
    "tool_boundaries_declare_first_payload_ids_followups_and_query_fallbacks",
    "ai_materialization_requires_fresh_cached_or_route_material_evidence",
    "compact_ai_critical_path_contract_owns_intent_landing_path_stop_before_certification",
    "ai_route_arbitration_separates_ai_seeds_from_heuristic_support_before_certification",
    "first_package_background_completion_and_reattach_are_separate",
    "run_projection_replay_uses_backend_events_not_synthetic_motion",
    "mcp_delivery_contract_declares_client_payload_state_and_missing_reasons",
    "grow_sleep_evolve_lifecycle_states_are_normalized_and_mutation_gated",
]

BRAIN_BOOTSTRAP_V1_MCP_TOOL_NAMES = [
    "brain_bootstrap_start",
    "brain_bootstrap_status",
    "brain_bootstrap_answer",
    "brain_bootstrap_add_source",
    "brain_bootstrap_preview",
    "brain_bootstrap_apply",
    "brain_bootstrap_resume",
    "brain_bootstrap_recover",
    "brain_bootstrap_cancel",
]

BRAIN_PROFILE_V1_MCP_TOOL_NAMES = [
    "brain_profile_preview",
    "brain_profile_apply",
    "brain_profile_rollback",
]

REQUIRED_MCP_TOOL_NAMES = [
    "retrieve_context",
    "retrieve_document",
    "retrieve_document_workspace",
    "retrieve_project_workspace",
    "retrieve_path_corridor",
    "retrieve_source_trace",
    "inspect_context_package",
    "inspect_route",
    "inspect_path_corridor",
    "inspect_memory_object",
    "grow_source_preview",
    "grow_source_status",
    "grow_source_apply",
    "write_memory_preview",
    "write_memory_commit",
    "ask_memory_clarification",
    "grow_preview",
    "grow_guided",
    "grow_apply",
    "grow_status",
    "brain_health",
    "geometry_calibration_preview",
    "geometry_calibration_apply",
    "geometry_calibration_rollback",
    "matrix_calibration_preview",
    "matrix_calibration_apply",
    "matrix_calibration_rollback",
    "sleep_preview",
    "sleep_apply",
    "sleep_rollback",
    "evolve_preview",
    "evolve_apply",
    "evolve_rollback",
    "list_open_questions",
    "list_hypotheses",
    "list_contradictions",
    "list_memory_os_processes",
]

GUIDE_MCP_TOOL_NAMES = [
    "get_agvm_usage_guide",
]

AGENT_MEMORY_MCP_TOOL_NAMES = [
    "list_brains",
    "active_brain",
    "create_brain",
    "select_brain",
    "ensure_brain",
    *BRAIN_BOOTSTRAP_V1_MCP_TOOL_NAMES,
    *BRAIN_PROFILE_V1_MCP_TOOL_NAMES,
]

PR12J_B_IMPLEMENTED_TOOL_NAMES = {
    "retrieve_context",
    "retrieve_document",
    "retrieve_document_workspace",
    "retrieve_project_workspace",
    "retrieve_path_corridor",
    "retrieve_source_trace",
    "inspect_context_package",
    "inspect_route",
    "inspect_path_corridor",
    "inspect_memory_object",
}

PR12J_C_IMPLEMENTED_TOOL_NAMES = {
    "grow_source_preview",
    "grow_source_status",
    "grow_source_apply",
    "write_memory_preview",
    "write_memory_commit",
    "ask_memory_clarification",
    "grow_preview",
    "grow_guided",
    "grow_apply",
    "grow_status",
}

PR12J_D_IMPLEMENTED_TOOL_NAMES = {
    "geometry_calibration_preview",
    "geometry_calibration_apply",
    "geometry_calibration_rollback",
    "matrix_calibration_preview",
    "matrix_calibration_apply",
    "matrix_calibration_rollback",
    "sleep_preview",
    "sleep_apply",
    "sleep_rollback",
    "evolve_preview",
    "evolve_apply",
    "evolve_rollback",
    "list_open_questions",
    "list_hypotheses",
    "list_contradictions",
    "list_memory_os_processes",
}

PR12J_E_IMPLEMENTED_TOOL_NAMES = {
    "brain_health",
}

IMPLEMENTED_MCP_TOOL_NAMES = (
    PR12J_B_IMPLEMENTED_TOOL_NAMES
    | PR12J_C_IMPLEMENTED_TOOL_NAMES
    | PR12J_D_IMPLEMENTED_TOOL_NAMES
    | PR12J_E_IMPLEMENTED_TOOL_NAMES
    | set(GUIDE_MCP_TOOL_NAMES)
    | set(AGENT_MEMORY_MCP_TOOL_NAMES)
    | set(BRAIN_BOOTSTRAP_V1_MCP_TOOL_NAMES)
)

MCP_CONTRACT_HTTP_METHODS = {"GET", "POST"}
MCP_CONTRACT_SCOPE_POLICIES = {
    "global",
    "registry",
    "brain",
    "brain_preview",
    "brain_apply",
    "hosted_registry",
}
MCP_CONTRACT_PERMISSION_FAMILIES = {
    "read_only",
    "read_only_export",
    "registry_write",
    "preview_only",
    "explicit_apply",
    "destructive",
}


def _schema_object(
    *,
    properties: dict[str, Any],
    required: list[str] | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required or [],
    }
    if description:
        schema["description"] = description
    return schema


def _string(
    description: str,
    *,
    enum: list[str] | None = None,
    nullable: bool = False,
    default: str | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": ["string", "null"] if nullable else "string", "description": description}
    if enum:
        schema["enum"] = enum
    if default is not None:
        schema["default"] = default
    return schema


def _boolean(description: str, *, default: bool | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "boolean", "description": description}
    if default is not None:
        schema["default"] = default
    return schema


def _integer(description: str, *, minimum: int | None = None, maximum: int | None = None, default: int | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "integer", "description": description}
    if minimum is not None:
        schema["minimum"] = minimum
    if maximum is not None:
        schema["maximum"] = maximum
    if default is not None:
        schema["default"] = default
    return schema


def _array(description: str, *, item_type: str = "object") -> dict[str, Any]:
    return {"type": "array", "description": description, "items": {"type": item_type}}


def _object(description: str) -> dict[str, Any]:
    return {"type": "object", "description": description, "additionalProperties": True}


def _query_input_schema(*, document_target: bool = False, include_thread: bool = True) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "query_text": _string("Natural language retrieval request."),
        "retrieval_mode": _string("Retrieval budget mode.", enum=["flash", "balanced", "heavy", "forensic"]),
        "context_package_mode": _string(
            "Optional MCP context package policy mode. Defaults to mcp_operational, broad_dossier for broad self queries, and document_full/forensic_trace for document lookups.",
            enum=["answer_minimal", "mcp_operational", "broad_dossier", "document_full", "forensic_trace"],
            nullable=True,
        ),
        "document_text_policy": _string(
            "Raw document hydration policy for retrieve_context. refs_only returns actionable refs; top_raw/all_raw attach bounded raw document packets when explicitly requested.",
            enum=["refs_only", "top_raw", "all_raw"],
            default="refs_only",
        ),
        "max_matches": _integer(
            "Maximum number of memory matches to expose.",
            minimum=1,
            maximum=MAX_RETRIEVAL_MATCHES,
            default=DEFAULT_RETRIEVAL_MAX_MATCHES,
        ),
        "include_raw_text": _boolean("Request full raw source/document text when available.", default=False),
        "include_answer_demo": _boolean("Optionally include downstream answer demo; never default MCP output.", default=False),
        "complete_paths": _boolean("Ask retrieval to spend extra route budget so planned path corridors complete, stop, or remain explicitly pending.", default=False),
    }
    if include_thread:
        properties["thread_id"] = _string("Optional continuity thread id.", nullable=True)
    if document_target:
        properties["document_id"] = _string("Exact document id returned by a previous document ref, when known.", nullable=True)
        properties["document_hint"] = _string("Specific document, project, source label or topic to look up.", nullable=True)
    return _schema_object(properties=properties, required=["query_text"], description="MCP retrieval input contract.")


def _context_output_schema(
    *,
    package_field: str,
    package_description: str,
    require_runtime_boundary: bool = True,
) -> dict[str, Any]:
    required = ["schema_version", "tool_name", "status", package_field, "payload_integrity", "completeness", "budget"]
    if require_runtime_boundary:
        required.extend([
            "runtime_state_contract",
            "tool_boundary_contract",
            "ai_materialization_resilience_contract",
            "ai_critical_path_contract",
            "route_arbitration_contract",
            "first_package_background_contract",
            "run_projection_event_stream_contract",
            "mcp_delivery_contract",
        ])
    return _schema_object(
        properties={
            "schema_version": _string("Tool output schema version."),
            "tool_name": _string("MCP tool name."),
            "search_id": _string("Persisted search id for follow-up inspection tools when this output was produced by a retrieval run.", nullable=True),
            "status": _string("Execution state.", enum=["ok", "partial", "no_match", "needs_clarification", "blocked", "failed"]),
            package_field: _object(package_description),
            "context_package_materialization": _object("Context package readiness state, separated from any downstream answer demo."),
            "hot_working_memory": _object("Separate persistent thread/brain working memory. It is not the MCP context package unless explicitly promoted into the package."),
            "hot_working_memory_contract": _object("Reuse, demotion and stale-guard contract for hot working memory on this call."),
            "answer_demo_materialization": _object("Optional answer-demo readiness state. The demo is secondary and is not the MCP source of truth."),
            "semantic_contract": _object("Compiled semantic query contract used by retrieval, including target document need audit when document evidence is requested."),
            "semantic_contract_runtime": _object("Semantic contract compiler runtime, provider/cache state and materialization truth."),
            "target_document_need_contract": _object("DWE-2 document need classification: pure document evidence, exact hydration, related docs, source trace, mixed context+docs or normal context."),
            "target_document_need": _object("Preserved original claim/query text used as the ranking and judging target for document-evidence requests."),
            "ai_landing_materialization": _object("AI materialization truth contract: semantic contract, landing, branch, path and judge state for the run."),
            "ai_materialization_hard_gate": _object("Final AI materialization hard-gate truth, including blocked/provisional reason for semantic MCP runs."),
            "mcp_background_cap": _object("Optional first-package background cap request emitted when an AI-certified MCP context package is already useful and background continuation should not delay local usage."),
            "document_text_policy": _string("Requested document text delivery policy.", enum=["refs_only", "top_raw", "all_raw"]),
            "document_refs": _array("Actionable document references returned by the context package."),
            "document_ref_contract": _object("Actionable document reference contract, including raw availability and follow-up retrieve_document recipes."),
            "document_delivery_contract": _object("Exact document delivery truth: refs-only/top_raw/all_raw policy, raw-in-current-payload state, and follow-up document calls."),
            "document_bundle": _object("Optional bounded raw document packet bundle when document_text_policy requests top_raw/all_raw."),
            "runtime_state_contract": _object("PR-12P-14U-A normalized runtime state contract: payload, AI, provider, document, path, answer-demo, run and operator states are split so clients do not infer lifecycle from scattered fields."),
            "tool_boundary_contract": _object("PR-12P-14U-B MCP tool boundary contract: endpoint, primary field, first payload, required ids, query fallback, follow-up tools and pure MCP Lab display recipe."),
            "ai_materialization_resilience_contract": _object("PR-12P-14U-C AI materialization resilience contract: fresh/cached/route material source, provider degraded state, retry/cache evidence and no heuristic-only certification law."),
            "ai_critical_path_contract": _object("PR-12P-8B-B compact AI critical-path contract: first AI intent, required slots, landing hypotheses, path goals, forbidden evidence, stop criteria, latency and cache validity before product certification."),
            "route_arbitration_contract": _object("PR-12P-8B-C AI/heuristic route arbitration contract: AI route plan, candidate-family attribution, path budget requested/used, bridge goals and the rule that heuristic support cannot certify the product payload."),
            "first_package_background_contract": _object("PR-12P-14U-D first-package/background-completion contract: first MCP payload terminality, background state, stream-vs-final seal separation and refresh/reattach recipe."),
            "run_projection_event_stream_contract": _object("PR-12P-14U-E selected-run projection event-stream/replay contract: 2D/3D maps render backend projection events only, never random validation motion, and show a diagnostic fallback instead of a blank canvas."),
            "mcp_delivery_contract": _object("PR-12P-14U-H delivery contract: canonical client payload state, completion state, finalization pending flag, AI/document/path state, missing reasons, stream endpoint and next MCP follow-up call."),
            "run_projection_truth": _object("Normalized selected-run projection truth for 2D/3D living-brain maps: nodes, edges, paths, events and summary. Visuals must render this contract, not inferred debug state."),
            "payload_integrity": _object("Product integrity proof that the MCP package, optional answer demo, support nodes, and route state are aligned."),
            "source_trace": _array("Structured node/source trace rows."),
            "completeness": _object("Completeness, unresolved slots and budget state."),
            "budget": _object("Budget request and usage metadata."),
            "latency_contract": _object("MCP-first latency contract: first useful package timing, full completion timing, AI landing timing and benchmark basis."),
            "answer_demo": _object("Optional downstream answer demo only when include_answer_demo=true."),
        },
        required=required,
        description="MCP JSON-first output contract.",
    )


def _source_input_schema() -> dict[str, Any]:
    return _schema_object(
        properties={
            "raw_input": _string("Manual text, URL or extracted source text."),
            "input_kind": _string("Source kind hint.", enum=["auto", "manual_text", "url", "website", "pdf", "docx", "image", "transcript", "mixed_bundle"]),
            "source_label": _string("Optional source label.", nullable=True),
            "source_uri": _string("Optional source URI.", nullable=True),
            "user_instruction": _string("Optional ingestion instruction.", nullable=True),
            "options": _object("Source investigation options."),
            "run_preview": _boolean(
                "Run compiler preview when eligible. Set false for the fast MCP source-unit proof path; apply still requires a later full preview.",
                default=True,
            ),
        },
        required=["raw_input"],
        description="Grow source MCP input contract.",
    )


def _source_apply_input_schema() -> dict[str, Any]:
    return _schema_object(
        properties={
            "investigation_id": _string(
                "Source investigation id returned by grow_source_preview or grow_preview.",
                nullable=True,
            ),
            "source_investigation": _object("Optional full source investigation package returned by preview."),
            "source_formation_contract": _object("Optional formation contract returned by preview."),
            "preview_bundle": _object("Optional preview bundle returned by preview."),
            "selected_preview_ids": _array(
                "Exact preview node ids approved for persistence. Omit only when the preview contract allows applying the default reviewed set.",
                item_type="string",
            ),
            "approved_preview_ids": _array(
                "Alias/receipt list of approved preview ids when a UI review surface records approval separately.",
                item_type="string",
            ),
            "clarification_answers": _object(
                "Answers to clarification questions returned by preview. Required when the source_formation_contract reports pending questions."
            ),
            "learning_mode": _string(
                "Learning policy for the persisted nodes.",
                enum=["strict_review", "guided_learning", "autonomous_cautious", "autonomous_research", "sleep_review"],
            ),
            "question_limit": _integer("Maximum clarification questions to preserve during apply.", minimum=1, maximum=24, default=12),
            "confirm_apply": _boolean(
                "Required explicit confirmation for mutation. Set true only after the preview/formation contract has been reviewed.",
                default=False,
            ),
        },
        required=["investigation_id"],
        description=(
            "Grow source explicit-apply contract. Normal flow: call grow_source_preview, review "
            "source_formation_contract/preview_bundle/clarification_questions, then call grow_source_apply "
            "with investigation_id and confirm_apply=true. If status is blocked, answer the reported questions "
            "or pass exact selected_preview_ids from the preview."
        ),
    )


def _source_status_input_schema() -> dict[str, Any]:
    return _schema_object(
        properties={
            "investigation_id": _string("Source investigation id returned by preview.", nullable=True),
        },
        required=["investigation_id"],
        description="Read source investigation status without mutating the brain.",
    )


def _source_output_schema(*, applied: bool = False) -> dict[str, Any]:
    properties = {
        "schema_version": _string("Tool output schema version."),
        "tool_name": _string("MCP tool name."),
        "status": _string("Source investigation state.", enum=["preview_ready", "asking_clarification", "partial_budget_exhausted", "applied", "blocked", "failed"]),
        "source_investigation": _object("Versioned source investigation package."),
        "source_formation_contract": _object("PR-12P-14Q-E formation density, question gate, apply and post-save retrieval proof contract."),
        "memory_operation_lifecycle_contract": _object("PR-12P-14U-F normalized Grow/Sleep/Evolve lifecycle contract: preview, approval, apply, delta, rollback and retrieval proof states use one grammar."),
        "preview_bundle": _object("Compiler preview bundle when run and eligible."),
        "clarification_questions": _array("Bounded human clarification questions."),
        "compiler_handoff_proof": _object("PR-12I-G compiler handoff and retrieval proof."),
        "mcp_latency_profile": _object("Fast-vs-full Grow MCP profile: source_unit_only proofs are valid for client inspection, while apply requires a full preview bundle."),
    }
    if applied:
        properties["persist_result"] = _object("Persist selection result after explicit apply.")
    return _schema_object(
        properties=properties,
        required=["schema_version", "tool_name", "status", "source_investigation", "memory_operation_lifecycle_contract"],
        description="Grow source MCP output contract.",
    )


def _clarification_output_schema() -> dict[str, Any]:
    return _schema_object(
        properties={
            "schema_version": _string("Tool output schema version."),
            "tool_name": _string("MCP tool name."),
            "status": _string("Clarification state.", enum=["preview_ready", "asking_clarification", "blocked", "failed"]),
            "clarification_request": _object("Bounded clarification request package."),
            "clarification_questions": _array("Bounded human clarification questions."),
            "source_investigation": _object("Optional source investigation package."),
            "preview_bundle": _object("Optional write/source preview bundle."),
            "compiler_handoff_proof": _object("Optional PR-12I-G compiler handoff proof."),
            "memory_operation_lifecycle_contract": _object("PR-12P-14U-F normalized lifecycle state for clarification-driven memory operations."),
        },
        required=["schema_version", "tool_name", "status", "clarification_request", "clarification_questions", "memory_operation_lifecycle_contract"],
        description="Memory clarification MCP output contract.",
    )


def _write_input_schema(*, commit: bool = False) -> dict[str, Any]:
    properties = {
        "text": _string("Text to preview into memory. For commit, use this only for a one-shot text commit when no preview bundle is available.", nullable=commit),
        "input_mode": _string("Compiler input mode.", enum=["auto", "document"]),
        "source_label": _string("Optional source label.", nullable=True),
        "source_type": _string("Optional source type.", nullable=True),
        "source_trust": _string("Source trust.", enum=["verified_public", "user_asserted", "uploaded_document", "synthetic_test", "inferred", "system_metadata"]),
        "learning_mode": _string("Learning policy mode.", enum=["strict_review", "guided_learning", "autonomous_cautious", "autonomous_research", "sleep_review"]),
    }
    if commit:
        properties["bundle"] = _object("Preview bundle returned by write_memory_preview. Preferred commit flow: preview first, review, then pass this bundle.")
        properties["selected_preview_ids"] = _array("Preview ids approved by the user.", item_type="string")
        properties["approved_preview_ids"] = _array("Alias/receipt list of approved preview ids when a UI review surface records approval separately.", item_type="string")
        properties["clarification_answers"] = _object("Answers to clarification questions returned by preview.")
        properties["question_limit"] = _integer("Maximum clarification questions to preserve during apply.", minimum=1, maximum=24, default=12)
        properties["confirm_apply"] = _boolean("Required explicit confirmation for mutation. Set true only after reviewing the preview bundle.", default=False)
        schema = _schema_object(
            properties=properties,
            required=[],
            description=(
                "Write memory commit contract. Preferred flow: call write_memory_preview with text, "
                "review preview_bundle, then call write_memory_commit with bundle and confirm_apply=true. "
                "One-shot text commit is accepted only when confirm_apply=true and still runs a preview internally."
            ),
        )
        schema["anyOf"] = [{"required": ["bundle"]}, {"required": ["text"]}]
        return schema
    return _schema_object(properties=properties, required=["text"], description="Write memory preview input contract.")


def _write_output_schema(*, commit: bool = False) -> dict[str, Any]:
    properties = {
        "schema_version": _string("Tool output schema version."),
        "tool_name": _string("MCP tool name."),
        "status": _string("Write state.", enum=["preview_ready", "needs_review", "applied", "blocked", "failed"]),
        "preview_bundle": _object("Compiler preview bundle."),
        "cognitive_write_plan": _object("Cognitive write plan and review gates."),
        "learning_policy": _object("Learning policy resolution."),
        "memory_operation_lifecycle_contract": _object("PR-12P-14U-F normalized write lifecycle contract: review/apply/delta/proof states use the same grammar as Grow and maintenance."),
    }
    if commit:
        properties["persist_result"] = _object("Persisted node ids, merge ids and write trace.")
    return _schema_object(properties=properties, required=["schema_version", "tool_name", "status", "memory_operation_lifecycle_contract"], description="Write memory MCP output contract.")


def _maintenance_input_schema(*, apply: bool = False) -> dict[str, Any]:
    properties = {
        "brain_id": {
            **_string("Explicit brain id returned by list_brains or ensure_brain."),
            "minLength": 1,
        },
        "focus_node_id": _string("Optional focus node id for local maintenance.", nullable=True),
        "mode": _string("Optional maintenance mode override. Named tools already imply this: sleep_preview/sleep_apply use sleep, evolve_preview/evolve_apply use evolve.", enum=["sleep", "evolve"]),
        "dry_run": _boolean("Preview only; true by default for preview tools.", default=True),
        "max_nodes_considered": _integer("Maintenance node budget. Default MCP preview is 20 for fast local-client inspection; request 80+ for a deeper maintenance preview.", minimum=10, maximum=500, default=20),
    }
    if apply:
        properties["preview_signature"] = {
            **_string("Exact preview signature returned by the matching maintenance preview."),
            "minLength": 1,
        }
        properties["selected_proposal_ids"] = {
            **_array("Complete set of review-approved maintenance proposal ids returned by the matching preview.", item_type="string"),
            "minItems": 1,
            "uniqueItems": True,
        }
        properties["confirm_apply"] = _boolean("Explicit apply confirmation.", default=False)
    required = ["brain_id", "preview_signature", "selected_proposal_ids", "confirm_apply"] if apply else []
    return _schema_object(
        properties=properties,
        required=required,
        description=(
            "Maintenance MCP input contract. Preview exposes brain_id, focus_node_id and max_nodes_considered; "
            "the tool name selects sleep or evolve. Apply must be bound to the exact preview and its complete reviewed proposal selection."
        ),
    )


def _maintenance_output_schema(*, apply: bool = False) -> dict[str, Any]:
    properties = {
        "schema_version": _string("Tool output schema version."),
        "tool_name": _string("MCP tool name."),
        "status": _string("Maintenance state.", enum=["preview_ready", "applied", "blocked", "failed"]),
        "maintenance_report": _object("Sleep/evolve report with proposals, policy guard and metamemory."),
        "maintenance_proposals": _array("Reviewable maintenance proposals."),
        "maintenance_truth_contract": _object("Product-surface truth contract for preview/apply exactness and mutation visibility."),
        "sleep_evolve_lifecycle_contract": _object("PR-12P-14Q-F lifecycle contract for sleep/evolve separation, approval, delta, rollback and retrieval benefit proof."),
        "memory_operation_lifecycle_contract": _object("PR-12P-14U-F normalized lifecycle contract shared with Grow/Write so the UI and MCP clients read one approval/delta/rollback grammar."),
        "proposal_review_table": _array("Flattened proposal rows with targets, policy, rollback and selected-for-apply state."),
        "metamemory_snapshot": _object("Metamemory state used by maintenance."),
        "maintenance_latency_profile": _object("Fast-vs-deep maintenance MCP profile. Fast previews are non-mutating and bounded; deeper previews remain available by increasing max_nodes_considered."),
        "mutation_surface": _object("Non-hidden mutation contract including selection exactness and guard state."),
        "rollback_plan": _object("Rollback availability, graph hashes and raw-document preservation state."),
        "matrix_delta": _object("Metamemory, calibration and quality delta summary."),
    }
    if apply:
        properties["rollback_snapshot"] = _object("Rollback snapshot metadata created before apply.")
        properties["before_after_audit"] = _object("Before/candidate/applied graph audit.")
    return _schema_object(properties=properties, required=["schema_version", "tool_name", "status", "maintenance_report", "memory_operation_lifecycle_contract"], description="Maintenance MCP output contract.")


def _maintenance_rollback_input_schema() -> dict[str, Any]:
    return _schema_object(
        properties={
            "brain_id": {
                **_string("Explicit brain id returned by list_brains or ensure_brain."),
                "minLength": 1,
            },
            "preview_signature": {
                **_string("Exact signature of the applied Sleep/Evolve preview to restore."),
                "minLength": 1,
            },
            "confirm_rollback": _boolean("Explicit rollback confirmation.", default=False),
        },
        required=["brain_id", "preview_signature", "confirm_rollback"],
        description="Revision-safe Sleep/Evolve rollback bound to one applied persistent preview.",
    )


def _maintenance_rollback_output_schema() -> dict[str, Any]:
    return _schema_object(
        properties={
            "schema_version": _string("Maintenance rollback output schema version."),
            "brain_id": _string("Brain restored by the rollback."),
            "tool_name": _string("Canonical Sleep/Evolve rollback tool name."),
            "mode": _string("Maintenance mode restored by this rollback.", enum=["sleep", "evolve"]),
            "preview_signature": _string("Applied persistent preview restored by this rollback."),
            "status": _string("Rollback state.", enum=["rolled_back", "already_rolled_back", "blocked", "failed"]),
            "rollback_result": _object("Atomic SQLite rollback result, restored revision and idempotency proof."),
            "mutation_surface": _object("Revision-safe graph restoration evidence."),
            "maintenance_latency_profile": _object("Rollback execution latency."),
            "error": _object("Structured rollback error when blocked."),
        },
        required=["schema_version", "brain_id", "tool_name", "mode", "preview_signature", "status", "rollback_result"],
        description="Sleep/Evolve rollback MCP output contract.",
    )


def _brain_health_input_schema() -> dict[str, Any]:
    return _schema_object(
        properties={
            "limit": _integer("Recent search and maintenance rows to inspect.", minimum=1, maximum=100, default=25),
            "include_issue_samples": _boolean("Include bounded node/session issue samples for operator inspection.", default=True),
        },
        description="Fast non-mutating brain health request.",
    )


def _brain_health_output_schema() -> dict[str, Any]:
    return _schema_object(
        properties={
            "schema_version": _string("Tool output schema version."),
            "tool_name": _string("MCP tool name."),
            "status": _string("Health state.", enum=["ok", "partial", "blocked", "failed"]),
            "brain_health_report": _object("Full non-mutating brain health report."),
            "recommendation": _string(
                "Recommended next safe action.",
                enum=["none", "sleep_preview", "evolve_preview", "grow_repair", "matrix_calibration_preview", "rebuild_required"],
            ),
            "reason_codes": _array("Machine-readable recommendation reasons.", item_type="string"),
            "health_summary": _object("Compact health summary for UI/MCP clients."),
            "checks": _object("Node atomicity, source, link, document, radial and recent retrieval checks."),
            "actions": _array("Reviewable non-mutating next actions."),
            "retrieval_learning_rollup": _object("Recent MCP retrieval signal rollup with debounce/hysteresis families and recommendation hints."),
            "brain_sanity_snapshot": _object("Fast non-mutating benchmark/session sanity snapshot."),
            "health_alerts": _array("Persistable alert-shaped health signals with severity, family, recommendation and product gate impact."),
            "alert_summary": _object("Compact alert count and severity/family histogram."),
            "evolution_recommendation": _object("Ranked non-mutating recommendation for Sleep, Evolve, matrix calibration, Grow repair or rebuild planning."),
            "benchmark_preflight": _object("Benchmark gate verdict: healthy, warning, blocked until preview, or rebuild required."),
            "validation_brain_rebuild_gate": _object("Non-mutating validation brain snapshot/replay gate; blocks reset/retrieve proof when source/node-quality exports are incomplete."),
            "automation_policy": _object("Explicit policy for manual review, auto-preview and apply guards; no hidden mutation."),
            "safety_contract": _object("No-hidden-mutation and preview/apply/rollback safety contract."),
            "budget": _object("Runtime budget and audit-separation metadata."),
        },
        required=["schema_version", "tool_name", "status", "brain_health_report", "recommendation", "safety_contract"],
        description="Fast product brain health MCP output contract.",
    )


def _matrix_calibration_input_schema() -> dict[str, Any]:
    return _schema_object(
        properties={
            "brain_id": {
                **_string("Explicit brain id returned by list_brains or ensure_brain."),
                "minLength": 1,
            },
            "focus_node_id": _string("Optional focus node id for geometry inspection.", nullable=True),
            "max_nodes_considered": _integer("Geometry calibration node budget.", minimum=50, maximum=4000, default=4000),
            "max_position_updates": _integer("Maximum preview-only position deltas to include.", minimum=1, maximum=2000, default=1600),
            "include_recommendations": _boolean("Include bounded calibration recommendations.", default=True),
        },
        description="Non-mutating Geometry/Matrix calibration preview request with explicit brain, optional focus and bounded node/update limits.",
    )


def _matrix_calibration_apply_input_schema() -> dict[str, Any]:
    schema = _matrix_calibration_input_schema()
    schema["properties"] = {
        **dict(schema.get("properties") or {}),
        "confirm_apply": _boolean("Explicit matrix apply confirmation.", default=False),
        "rollback_consent": _boolean("Confirms rollback snapshot creation and guarded coordinate mutation.", default=False),
        "preview_signature": {
            **_string("Exact plan_signature returned by the matching Geometry/Matrix calibration preview."),
            "minLength": 1,
        },
        "selected_proposal_ids": {
            **_array("Complete set of reviewed calibration proposal ids returned by the matching preview.", item_type="string"),
            "minItems": 1,
            "uniqueItems": True,
        },
    }
    schema["required"] = [
        "brain_id",
        "preview_signature",
        "selected_proposal_ids",
        "confirm_apply",
        "rollback_consent",
    ]
    schema["description"] = "Guarded Geometry/Matrix calibration apply request bound to an exact reviewed preview and rollback consent."
    return schema


def _geometry_calibration_rollback_input_schema() -> dict[str, Any]:
    return _schema_object(
        properties={
            "plan_signature": _string("Exact plan_signature of the applied Geometry Calibration revision to restore."),
            "confirm_rollback": _boolean("Explicit rollback confirmation.", default=False),
        },
        required=["plan_signature"],
        description="Guarded Geometry Calibration rollback request. The MCP bridge supplies the selected brain_id.",
    )


def _matrix_calibration_output_schema() -> dict[str, Any]:
    return _schema_object(
        properties={
            "schema_version": _string("Tool output schema version."),
            "maintenance_id": _string("Maintenance ledger id when an apply is recorded."),
            "tool_name": _string("MCP tool name."),
            "status": _string("Calibration preview/apply state.", enum=["ok", "partial", "blocked", "failed", "applied"]),
            "brain_geometry_calibration": _object("Full non-mutating brain geometry calibration report."),
            "calibration_proposals": _array("Reviewable matrix calibration proposal summaries."),
            "recommendations": _array("Bounded calibration recommendations."),
            "matrix_change_policy": _object("Mutation policy for any future geometry/matrix update."),
            "maintenance_truth_contract": _object("No-hidden-mutation truth contract for calibration preview."),
            "memory_operation_lifecycle_contract": _object("Normalized lifecycle contract shared with Grow/Sleep/Evolve."),
            "matrix_delta": _object("Calibration score/check/proposal summary."),
            "position_update_plan": _object("Preview-only concrete coordinate deltas for matrix/radial repair."),
            "projected_after": _object("Projected after-metrics if the preview-only deltas were applied."),
            "apply_policy_guard": _object("Apply guard, confirmation and blocked reason contract."),
            "rollback_snapshot": _object("Rollback metadata created before any visible matrix apply."),
            "before_after_audit": _object("Before/projected/after matrix metrics and hashes."),
            "mutation_surface": _object("Visible mutation surface and touched/untouched storage areas."),
            "safety_contract": _object("Non-mutating and preview/apply/rollback guarantees."),
            "latency_profile": _object("Fast preview profile for MCP clients."),
            "actions": _array("Recommended follow-up actions."),
            "budget": _object("Runtime budget and mutation allowance."),
            "completeness": _object("Proposal/recommendation/check completeness summary."),
        },
        required=[
            "schema_version",
            "tool_name",
            "status",
            "brain_geometry_calibration",
            "maintenance_truth_contract",
            "memory_operation_lifecycle_contract",
            "safety_contract",
        ],
        description="Matrix calibration MCP output contract.",
    )


def _geometry_calibration_rollback_output_schema() -> dict[str, Any]:
    return _schema_object(
        properties={
            "schema_version": _string("Tool output schema version."),
            "brain_id": _string("Brain restored by the rollback."),
            "tool_name": _string("Canonical Geometry Calibration rollback tool name."),
            "status": _string("Rollback state.", enum=["rolled_back", "already_rolled_back"]),
            "plan_signature": _string("Applied Geometry Calibration plan restored by this operation."),
            "rollback_result": _object("Atomic rollback result and idempotency proof."),
            "mutation_surface": _object("Restored nodes and revision state."),
            "safety_contract": _object("Atomicity, full-snapshot and exactly-once guarantees."),
        },
        required=[
            "schema_version",
            "brain_id",
            "tool_name",
            "status",
            "plan_signature",
            "rollback_result",
            "mutation_surface",
            "safety_contract",
        ],
        description="Geometry Calibration rollback MCP output contract.",
    )


def _list_output_schema(field_name: str, description: str) -> dict[str, Any]:
    return _schema_object(
        properties={
            "schema_version": _string("Tool output schema version."),
            "tool_name": _string("MCP tool name."),
            "status": _string("List state.", enum=["ok", "partial", "failed"]),
            field_name: _array(description),
            "source_trace": _array("Structured trace rows behind the listed items."),
        },
        required=["schema_version", "tool_name", "status", field_name],
        description="Memory OS list output contract.",
    )


def _brain_registry_output_schema() -> dict[str, Any]:
    return _schema_object(
        properties={
            "schema_version": _string("Local brain registry schema version."),
            "registry_id": _string("Registry id."),
            "registry_path": _string("Registry file path."),
            "brain_root": _string("Brain root path."),
            "active_brain_id": _string("Currently active local brain id.", nullable=True),
            "default_brain_id": _string("Default local brain id.", nullable=True),
            "brain_count": _integer("Registered brain count.", minimum=0),
            "brains": _array("Registered local brain records."),
            "validation": _object("Local registry validation."),
        },
        required=["schema_version", "brains"],
        description="Local brain registry response.",
    )


def _active_brain_output_schema() -> dict[str, Any]:
    return _schema_object(
        properties={
            "schema_version": _string("Active brain summary schema version."),
            "brain_id": _string("Active local brain id.", nullable=True),
            "display_name": _string("Active brain display name.", nullable=True),
            "storage_path": _string("Active brain storage path.", nullable=True),
            "safe_for_mcp": _boolean("Whether the active brain is safe for MCP use."),
            "runtime_scope_status": _string("Runtime scope status.", nullable=True),
            "registry_path": _string("Registry file path.", nullable=True),
            "brain_count": _integer("Registered brain count.", minimum=0),
        },
        required=["schema_version", "brain_id"],
        description="Active local brain summary.",
    )


def _brain_create_input_schema() -> dict[str, Any]:
    return _schema_object(
        properties={
            "brain_id": _string("Optional stable local brain id. Omit to derive one from display_name.", nullable=True),
            "display_name": _string("Human-readable brain name."),
            "description": _string("Optional brain description.", nullable=True),
            "make_active": _boolean("Whether to set the created brain as globally active. MCP clients should pass this explicitly.", default=False),
            "make_default": _boolean("Whether to set the created brain as default. MCP clients should pass this explicitly.", default=False),
        },
        required=["display_name", "make_active", "make_default"],
        description="Create local brain input contract.",
    )


def _brain_admin_operation_output_schema(*, default_field: str = "brain") -> dict[str, Any]:
    return _schema_object(
        properties={
            "schema_version": _string("Brain admin operation schema version."),
            "action": _string("Brain admin action."),
            "status": _string("Operation status."),
            "brain_id": _string("Target brain id.", nullable=True),
            "brain": _object("Target brain record."),
            "registry": _object("Updated local brain registry."),
            "warnings": _array("Operation warnings."),
        },
        required=["schema_version", "action", "status", default_field],
        description="Brain admin operation response.",
    )


def _brain_select_input_schema() -> dict[str, Any]:
    return _schema_object(
        properties={
            "brain_id": _string("Existing local brain id to select."),
            "make_default": _boolean("Whether to also set this brain as default.", default=False),
        },
        required=["brain_id"],
        description="Select local brain input contract.",
    )


def _ensure_brain_input_schema() -> dict[str, Any]:
    return _schema_object(
        properties={
            "brain_id": _string("Optional desired stable local brain id.", nullable=True),
            "display_name": _string("Human-readable brain name."),
            "description": _string("Optional brain description.", nullable=True),
            "purpose": _string("Why the agent needs this brain.", nullable=True),
            "activation_policy": _string(
                "Shared active/default update policy. return_only is safest for AI clients.",
                enum=["return_only", "make_active", "make_default"],
            ),
            "create_if_missing": _boolean("Create the brain when no matching brain exists.", default=True),
        },
        required=["display_name"],
        description="Idempotent local brain onboarding input contract.",
    )


def _ensure_brain_output_schema() -> dict[str, Any]:
    return _schema_object(
        properties={
            "schema_version": _string("Ensure brain result schema version."),
            "status": _string("Ensure status.", enum=["existing", "created", "selected", "blocked"]),
            "brain_id": _string("Resolved local brain id.", nullable=True),
            "brain": _object("Resolved brain record."),
            "registry": _object("Updated local brain registry."),
            "created": _boolean("Whether a brain was created."),
            "selected": _boolean("Whether active/default selection changed."),
            "activation_policy": _string("Effective activation policy.", enum=["return_only", "make_active", "make_default"]),
            "next_recommended_tools": _array("Recommended next MCP tools.", item_type="string"),
        },
        required=["schema_version", "status", "brain_id", "brain", "registry", "created", "selected", "activation_policy"],
        description="Idempotent brain onboarding result.",
    )


def _usage_guide_output_schema() -> dict[str, Any]:
    return _schema_object(
        properties={
            "schema_version": _string("Usage guide schema version."),
            "guide_name": _string("Guide display name."),
            "markdown_guide": _string("Human-readable MCP usage guide."),
            "policy": _object("Compact machine-readable AGVM MCP usage policy."),
            "recommended_flow": _array("Recommended first-call flow.", item_type="string"),
            "query_recipes": _object("Concrete query recipes for retrieval and document tools."),
            "tool_map": _object("Tool families and safety groupings."),
            "first_call": _object("First call recommendation for new clients."),
        },
        required=["schema_version", "guide_name", "markdown_guide", "policy", "recommended_flow", "query_recipes", "tool_map", "first_call"],
        description="AGVM MCP usage guide response.",
    )


def _brain_bootstrap_v1_input_schema(operation: str) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "brain_id": _string("Explicit local brain scope.", nullable=True),
        "session_id": _string("Immutable Bootstrap V1 session id.", nullable=True),
        "idempotency_key": _string("Stable idempotency key for a mutating Bootstrap command.", nullable=True),
        "expected_revision": _integer("CAS revision expected by the caller.", minimum=1),
        "capability": _string(
            "Execution capability. AI capabilities return a Detwin Cloud action contract locally.",
            enum=["manual_interview", "manual_source", "grow_review", "ai_research", "fitting", "backfill", "activation"],
        ),
    }
    required: list[str] = []
    if operation == "start":
        properties.update(
            {
                "goal": _string("Bounded bootstrap goal.", nullable=True),
                "interview_mode": _string(
                    "Interview planning mode. adaptive_ai generates a bounded provider-backed interview; manual uses caller-supplied questions.",
                    enum=["manual", "adaptive_ai"],
                    default="manual",
                ),
                "quality_policy": _string(
                    "Optional reviewed-seed quality gate. guided_seed_v1 requires enough answers, trusted source material and 12-30 grounded atomic candidates before apply.",
                    enum=["guided_seed_v1"],
                    nullable=True,
                ),
                "questions": _array(
                    "Optional manual interview questions. Omit when interview_mode is adaptive_ai.",
                    item_type="string",
                ),
            }
        )
        required = ["idempotency_key"]
    elif operation == "status":
        required = ["session_id"]
    else:
        required = ["session_id", "idempotency_key", "expected_revision"]
    if operation == "answer":
        properties.update(
            {
                "question_id": _string("Question being answered."),
                "answer_id": _string("Optional stable answer id.", nullable=True),
                "answer": _string("Manual answer. It remains session-only until explicit apply."),
            }
        )
        required.extend(["question_id", "answer"])
    elif operation == "add_source":
        properties.update(
            {
                "source_id": _string("Optional stable source id.", nullable=True),
                "source_label": _string("Human-readable source label.", nullable=True),
                "source_kind": _string("manual_text or url_reference.", nullable=True),
                "source_text": _string("Manual source text. No network fetching occurs in local V1.", nullable=True),
                "source_uri": _string("Reference URI. It is not fetched by local V1.", nullable=True),
                "source_trust": _string("Declared source trust.", nullable=True),
            }
        )
    elif operation == "apply":
        properties.update(
            {
                "confirm_apply": _boolean("Must be true to cross the only brain-write boundary.", default=False),
                "selected_preview_ids": _array("Reviewed Grow candidate ids to apply.", item_type="string"),
            }
        )
        required.extend(["confirm_apply", "selected_preview_ids"])
    return _schema_object(
        properties=properties,
        required=required,
        description=f"Bounded Brain Bootstrap V1 {operation} input.",
    )


def _brain_bootstrap_v1_output_schema() -> dict[str, Any]:
    return _schema_object(
        properties={
            "schema_version": _string("Bootstrap response schema version."),
            "operation": _string("Executed Bootstrap operation."),
            "status": _string("Terminal or review state for this call."),
            "brain_id": _string("Resolved brain scope."),
            "session_id": _string("Bootstrap session id.", nullable=True),
            "revision": _integer("Current immutable session revision.", minimum=0),
            "revision_digest": _string("Immutable revision digest.", nullable=True),
            "lifecycle_state": _string("Current lifecycle state.", nullable=True),
            "session": _object("Current immutable session snapshot."),
            "action_contract": _object("Cloud action contract for AI-only capabilities."),
            "idempotent_replay": _boolean("Whether this response replays an existing idempotent revision."),
            "mutation_contract": _object("Explicit no-hidden-write contract."),
        },
        required=["schema_version", "operation", "status", "brain_id", "revision", "idempotent_replay", "mutation_contract"],
        description="Bounded Brain Bootstrap V1 response.",
    )


def _brain_profile_v1_input_schema(operation: str) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "brain_id": _string("Explicit brain scope.", nullable=True),
    }
    required: list[str] = []
    if operation in {"preview", "apply"}:
        properties["profile"] = _object(
            "Signed immutable agvm.brain_profile.v1 payload with exactly 12 canonical routing dimensions."
        )
        properties["benchmark"] = _object(
            "Benchmark evidence. Apply requires complete=true and all three green release metrics."
        )
        required.append("profile")
    if operation == "apply":
        properties.update(
            {
                "expected_revision": _integer("Expected runtime revision for CAS.", minimum=0),
                "idempotency_key": _string("Stable idempotency key for this activation."),
                "confirm_apply": _boolean("Must be true for atomic activation.", default=False),
                "authority": _object(
                    "Authenticated HMAC authority bound to operation, profile, benchmark, brain, tenant and revisions."
                ),
            }
        )
        required.extend(["benchmark", "expected_revision", "idempotency_key", "confirm_apply", "authority"])
    if operation == "rollback":
        properties.update(
            {
                "expected_revision": _integer("Expected current runtime revision for CAS.", minimum=1),
                "target_revision": _integer("Immediately previous revision to restore byte-for-byte.", minimum=1),
                "idempotency_key": _string("Stable idempotency key for this rollback."),
                "confirm_rollback": _boolean("Must be true for atomic rollback.", default=False),
                "authority": _object(
                    "Authenticated HMAC authority bound to rollback, archived profile and benchmark, brain, tenant and revisions."
                ),
            }
        )
        required.extend(
            ["expected_revision", "target_revision", "idempotency_key", "confirm_rollback", "authority"]
        )
    return _schema_object(
        properties=properties,
        required=required,
        description=f"Brain Profile V1 {operation} input.",
    )


def _brain_profile_v1_output_schema() -> dict[str, Any]:
    return _schema_object(
        properties={
            "schema_version": _string("Brain Profile runtime response schema."),
            "operation": _string("preview, apply, or rollback."),
            "status": _string("preview_ready, applied, rolled_back, or cloud_required."),
            "brain_id": _string("Resolved brain scope."),
            "current_revision": _integer("Monotonic runtime operation revision.", minimum=0),
            "current_profile_revision": _integer("Signed profile revision currently active.", minimum=0),
            "previous_revision": _integer("Only revision eligible for rollback.", minimum=1),
            "profile": _object("Validated signed profile payload."),
            "benchmark": _object("Benchmark evidence bound to activation."),
            "authority": _object("Verified authority envelope persisted with the active runtime revision."),
            "action_contract": _object("Paid Detwin Cloud action contract when a lease is absent."),
            "idempotent_replay": _boolean("True when the exact request was replayed."),
            "mutation_contract": _object("Shadow, activation and rollback safety laws."),
        },
        required=[
            "schema_version",
            "operation",
            "status",
            "brain_id",
            "current_revision",
            "current_profile_revision",
            "idempotent_replay",
            "mutation_contract",
        ],
        description="Brain Profile V1 runtime response.",
    )


def _canonical_mcp_endpoint_path(tool_name: str) -> str:
    return f"/mcp/{tool_name.replace('_', '-')}"


def _permission_family_for_mutation_policy(mutation_policy: str) -> str:
    if mutation_policy in {"preview_only", "explicit_apply", "destructive", "registry_write", "read_only_export"}:
        return mutation_policy
    return "read_only"


def _scope_policy_for_permission_family(permission_family: str) -> str:
    if permission_family == "registry_write":
        return "registry"
    if permission_family == "preview_only":
        return "brain_preview"
    if permission_family in {"explicit_apply", "destructive"}:
        return "brain_apply"
    return "brain"


def _default_client_usage(*, description: str, default_output_package: str, mutation_policy: str) -> dict[str, Any]:
    return {
        "when_to_use": description,
        "default_output_package": default_output_package,
        "mutation_policy": mutation_policy,
        "must_not": [
            "Do not treat answer_demo as the source of truth.",
            "Do not call explicit apply tools without user approval.",
        ]
        if mutation_policy == "explicit_apply"
        else ["Do not treat answer_demo as the source of truth."],
        "followups": [],
    }


def _retrieval_client_usage(
    *,
    when_to_use: str,
    default_output_package: str,
    followups: list[str] | None = None,
    document_target: bool = False,
) -> dict[str, Any]:
    query_guidance = [
        "Write query_text as the user's concrete information need, not as keywords only.",
        "Include names, project labels, time window, decision/action needed and any known document/source hints.",
        "Use retrieval_mode=flash for quick recall, balanced for normal work, heavy for broad synthesis, forensic for evidence-sensitive/source-heavy checks.",
        "Keep include_answer_demo=false unless the user explicitly wants a drafted answer; the MCP package is the source of truth.",
    ]
    if document_target:
        query_guidance.extend(
            [
                "Use document_id when a previous document_ref provided an exact id.",
                "Use document_hint for a title, filename, URL, source label or topic when document_id is not known.",
                "Use document_text_policy=refs_only first; request top_raw/all_raw only when raw text is needed.",
            ]
        )
    return {
        "when_to_use": when_to_use,
        "default_output_package": default_output_package,
        "mutation_policy": "read_only",
        "query_guidance": query_guidance,
        "input_strategy": [
            "Always pass query_text.",
            "Pass brain_id only when the user/session selected an explicit AGVM brain; otherwise configure it in the MCP server.",
            "Use thread_id for follow-up turns that should share continuity.",
        ],
        "result_handling": [
            "Read the default output package first.",
            "Use search_id with inspection tools for follow-up diagnostics.",
            "Use document_refs and follow-up retrieve_document calls instead of guessing source text.",
        ],
        "followups": followups or ["inspect_context_package", "inspect_route", "retrieve_document"],
    }


def _merge_client_usage(default_usage: dict[str, Any], override_usage: dict[str, Any] | None) -> dict[str, Any]:
    if not override_usage:
        return dict(default_usage)

    merged = dict(default_usage)
    merged.update(override_usage)

    default_must_not = [str(value) for value in default_usage.get("must_not") or []]
    override_must_not = [str(value) for value in override_usage.get("must_not") or []]
    merged["must_not"] = override_must_not + [value for value in default_must_not if value not in override_must_not]
    merged.setdefault("followups", [])
    return merged


def _tool_contract(
    *,
    name: str,
    title: str,
    description: str,
    category: str,
    planned_slice: str,
    default_output_package: str,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any],
    candidate_backend_routes: list[str] | None = None,
    mutation_policy: str = "read_only",
    endpoint_path: str | None = None,
    http_method: str = "POST",
    requires_brain_id: bool = True,
    scope_policy: str | None = None,
    permission_family: str | None = None,
    client_usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    implementation_status = "implemented" if name in IMPLEMENTED_MCP_TOOL_NAMES else "schema_registered"
    binding_state = "implemented" if implementation_status == "implemented" else "adapter_pending"
    effective_permission_family = permission_family or _permission_family_for_mutation_policy(mutation_policy)
    effective_scope_policy = scope_policy or _scope_policy_for_permission_family(effective_permission_family)
    effective_endpoint_path = endpoint_path or _canonical_mcp_endpoint_path(name)
    effective_http_method = http_method.upper()
    tool_registration = build_mcp_tool_registration(
        tool_name=name,
        category=category,
        endpoint_path=effective_endpoint_path,
        http_method=effective_http_method,
        permission_family=effective_permission_family,
    )
    return {
        "schema_version": MCP_TOOL_CONTRACT_SCHEMA_VERSION,
        "name": name,
        "title": title,
        "description": description,
        "category": category,
        "planned_slice": planned_slice,
        "implementation_status": implementation_status,
        "endpoint_path": effective_endpoint_path,
        "http_method": effective_http_method,
        "requires_brain_id": bool(requires_brain_id),
        "scope_policy": effective_scope_policy,
        "permission_family": effective_permission_family,
        "tool_registration": tool_registration,
        "module_requirement": build_module_requirement_from_registration(tool_registration),
        "client_usage": _merge_client_usage(
            _default_client_usage(
                description=description,
                default_output_package=default_output_package,
                mutation_policy=mutation_policy,
            ),
            client_usage,
        ),
        "default_output_package": default_output_package,
        "default_includes_answer_demo": False,
        "input_schema": input_schema,
        "output_schema": output_schema,
        "output_laws": list(MCP_OUTPUT_LAWS),
        "backend_binding": {
            "binding_state": binding_state,
            "adapter_slice": planned_slice,
            "candidate_backend_routes": candidate_backend_routes or [],
            "thin_adapter_required": True,
            "implemented_in_slice": (
                "PR-12J-D"
                if name in PR12J_D_IMPLEMENTED_TOOL_NAMES
                else "Phase-4"
                if name in PR12J_E_IMPLEMENTED_TOOL_NAMES
                else "PR-12J-C"
                if name in PR12J_C_IMPLEMENTED_TOOL_NAMES
                else MCP_CONTRACT_IMPLEMENTATION_SLICE
                if name in PR12J_B_IMPLEMENTED_TOOL_NAMES
                else None
            ),
        },
        "safety_contract": {
            "mutation_policy": mutation_policy,
            "permission_family": effective_permission_family,
            "scope_policy": effective_scope_policy,
            "requires_explicit_apply": mutation_policy != "read_only",
            "answer_demo_policy": "not_returned_by_default",
            "primary_output_is_context_or_source_package": True,
        },
    }


def _advanced_module_visibility_note() -> str:
    return (
        " Local MCP keeps this advanced tool discoverable for planning; execution may be blocked until "
        "the user opens Detwin Cloud or connects a paid Detwin account/local Pro lease with enough credits."
    )


def _build_tool_contracts() -> list[dict[str, Any]]:
    contracts = [
        _tool_contract(
            name="retrieve_context",
            title="Retrieve MCP Context",
            description="Return the MCP context package for an agent without depending on the chat answer surface.",
            category="retrieval",
            planned_slice="PR-12J-B",
            default_output_package="context_package",
            input_schema=_query_input_schema(),
            output_schema=_context_output_schema(package_field="context_package", package_description="MCP context package v2."),
            candidate_backend_routes=["POST /memory/query", "GET /memory/query-result/{search_id}"],
            client_usage=_retrieval_client_usage(
                when_to_use="Use for normal memory recall, project context, decisions, plans and broad user-history questions.",
                default_output_package="context_package",
                followups=["retrieve_document", "inspect_context_package", "inspect_route", "retrieve_path_corridor"],
            ),
        ),
        _tool_contract(
            name="retrieve_document",
            title="Retrieve Document",
            description="Hydrate an exact known document id/ref/title/hint with bounded raw text when requested and available.",
            category="retrieval",
            planned_slice="PR-12J-B",
            default_output_package="document_workspace",
            input_schema=_query_input_schema(document_target=True),
            output_schema=_context_output_schema(package_field="document_workspace", package_description="Document workspace package."),
            candidate_backend_routes=["POST /memory/query"],
            client_usage=_retrieval_client_usage(
                when_to_use="Use to hydrate or inspect a specific document/source after a document_ref, title, filename, URL or source hint is known.",
                default_output_package="document_workspace",
                followups=["retrieve_source_trace", "inspect_context_package"],
                document_target=True,
            ),
        ),
        _tool_contract(
            name="retrieve_document_workspace",
            title="Retrieve Document Workspace",
            description="Discover ranked primary and related document refs for a document-evidence task, with reasons and retrieve_document hydration recipes.",
            category="retrieval",
            planned_slice="PR-12J-B",
            default_output_package="document_workspace",
            input_schema=_query_input_schema(document_target=True),
            output_schema=_context_output_schema(package_field="document_workspace", package_description="Document evidence workspace package."),
            candidate_backend_routes=["POST /memory/mcp/retrieve-document-workspace", "POST /mcp/retrieve-document-workspace"],
            client_usage=_retrieval_client_usage(
                when_to_use="Use when the task needs document evidence discovery before deciding which exact document to hydrate.",
                default_output_package="document_workspace",
                followups=["retrieve_document", "retrieve_source_trace", "inspect_context_package"],
                document_target=True,
            ),
        ),
        _tool_contract(
            name="retrieve_project_workspace",
            title="Retrieve Project Workspace",
            description="Backward-compatible alias for retrieve_document_workspace on project/source-topic requests.",
            category="retrieval",
            planned_slice="PR-12J-B",
            default_output_package="document_workspace",
            input_schema=_query_input_schema(document_target=True),
            output_schema=_context_output_schema(package_field="document_workspace", package_description="Project document workspace package."),
            candidate_backend_routes=["POST /memory/query"],
            client_usage=_retrieval_client_usage(
                when_to_use="Use for project/source-topic workspaces when the user asks about a project, repository, module or corpus rather than one exact memory fact.",
                default_output_package="document_workspace",
                followups=["retrieve_document", "retrieve_context", "retrieve_source_trace"],
                document_target=True,
            ),
        ),
        _tool_contract(
            name="retrieve_path_corridor",
            title="Retrieve Path Corridor",
            description="Return path corridor discoveries and the context they add.",
            category="retrieval",
            planned_slice="PR-12J-B",
            default_output_package="path_corridors",
            input_schema=_query_input_schema(),
            output_schema=_context_output_schema(package_field="path_corridors", package_description="Path corridor package."),
            candidate_backend_routes=["POST /memory/query", "GET /memory/get-trace/{search_id}"],
            client_usage=_retrieval_client_usage(
                when_to_use="Use when the agent needs connective reasoning between memories, intermediate hops or why two concepts/projects are related.",
                default_output_package="path_corridors",
                followups=["inspect_path_corridor", "inspect_route", "retrieve_context"],
            ),
        ),
        _tool_contract(
            name="retrieve_source_trace",
            title="Retrieve Source Trace",
            description="Return structured source trace rows behind retrieved context or documents.",
            category="retrieval",
            planned_slice="PR-12J-B",
            default_output_package="source_trace",
            input_schema=_query_input_schema(document_target=True),
            output_schema=_context_output_schema(package_field="source_trace", package_description="Structured source trace rows."),
            candidate_backend_routes=["POST /memory/query"],
            client_usage=_retrieval_client_usage(
                when_to_use="Use when the user or task needs provenance: source rows, document anchors, evidence chain or traceability behind retrieved context.",
                default_output_package="source_trace",
                followups=["retrieve_document", "inspect_route", "inspect_context_package"],
                document_target=True,
            ),
        ),
        _tool_contract(
            name="inspect_context_package",
            title="Inspect Context Package",
            description="Inspect a saved or returned context package without invoking the answer demo.",
            category="inspection",
            planned_slice="PR-12J-B",
            default_output_package="context_package",
            input_schema=_schema_object(
                properties={"search_id": _string("Search id to inspect."), "include_debug": _boolean("Include debug fields.", default=False)},
                required=["search_id"],
            ),
            output_schema=_context_output_schema(package_field="context_package", package_description="MCP context package v2."),
            candidate_backend_routes=["GET /memory/query-result/{search_id}", "GET /memory/get-trace/{search_id}"],
        ),
        _tool_contract(
            name="inspect_route",
            title="Inspect Route",
            description="Inspect route events, landings, branches and stop reasons.",
            category="inspection",
            planned_slice="PR-12J-B",
            default_output_package="route_trace",
            input_schema=_schema_object(properties={"search_id": _string("Search id to inspect.")}, required=["search_id"]),
            output_schema=_context_output_schema(package_field="route_trace", package_description="Route trace and search events.", require_runtime_boundary=False),
            candidate_backend_routes=["GET /memory/get-trace/{search_id}"],
        ),
        _tool_contract(
            name="inspect_path_corridor",
            title="Inspect Path Corridor",
            description="Inspect path corridor hops and promoted intermediate discoveries.",
            category="inspection",
            planned_slice="PR-12J-B",
            default_output_package="path_corridors",
            input_schema=_schema_object(properties={"search_id": _string("Search id to inspect.")}, required=["search_id"]),
            output_schema=_context_output_schema(package_field="path_corridors", package_description="Path corridor package."),
            candidate_backend_routes=["GET /memory/query-result/{search_id}", "GET /memory/get-trace/{search_id}"],
        ),
        _tool_contract(
            name="inspect_memory_object",
            title="Inspect Memory Object",
            description="Inspect a memory node, document anchor or nearby cluster.",
            category="inspection",
            planned_slice="PR-12J-B",
            default_output_package="memory_object",
            input_schema=_schema_object(properties={"node_id": _string("Memory node id to inspect.")}, required=["node_id"]),
            output_schema=_context_output_schema(package_field="memory_object", package_description="Memory object plus nearby cluster.", require_runtime_boundary=False),
            candidate_backend_routes=["GET /memory/inspect-nearby/{node_id}", "GET /cluster/{node_id}"],
        ),
    ]

    for name, title in [
        ("grow_source_preview", "Grow Source Preview"),
        ("grow_source_status", "Grow Source Status"),
        ("grow_source_apply", "Grow Source Apply"),
        ("grow_preview", "Grow Preview"),
        ("grow_guided", "Grow Guided"),
        ("grow_apply", "Grow Apply"),
        ("grow_status", "Grow Status"),
    ]:
        apply_tool = name.endswith("_apply")
        status_tool = name.endswith("_status")
        contracts.append(
            _tool_contract(
                name=name,
                title=title,
                description=(
                    "Preview, inspect or explicitly apply source-based Grow. Preview is non-mutating. "
                    "Apply mutates only with confirm_apply=true after reviewing the returned formation contract."
                    if apply_tool
                    else "Preview or inspect source-based Grow. Preview returns an investigation_id plus review gates for any later apply."
                    if not status_tool
                    else "Inspect a stored source investigation by investigation_id without mutation."
                ),
                category="grow",
                planned_slice="PR-12J-C",
                default_output_package="source_investigation",
                input_schema=(
                    _source_apply_input_schema()
                    if apply_tool
                    else _source_status_input_schema()
                    if status_tool
                    else _source_input_schema()
                ),
                output_schema=_source_output_schema(applied=apply_tool),
                candidate_backend_routes=["POST /memory/source-investigation/preview", "POST /memory/source-investigation/upload", "POST /memory/save-selection"],
                mutation_policy="explicit_apply" if apply_tool else "preview_only",
            )
        )

    contracts.extend(
        [
            _tool_contract(
                name="write_memory_preview",
                title="Write Memory Preview",
                description="Preview cognitive write plan and learning policy without mutation.",
                category="write",
                planned_slice="PR-12J-C",
                default_output_package="preview_bundle",
                input_schema=_write_input_schema(),
                output_schema=_write_output_schema(),
                candidate_backend_routes=["POST /memory/preview"],
                mutation_policy="preview_only",
            ),
            _tool_contract(
                name="write_memory_commit",
                title="Write Memory Commit",
                description="Commit explicitly approved preview nodes through the existing persist-selection path.",
                category="write",
                planned_slice="PR-12J-C",
                default_output_package="persist_result",
                input_schema=_write_input_schema(commit=True),
                output_schema=_write_output_schema(commit=True),
                candidate_backend_routes=["POST /memory/save-selection"],
                mutation_policy="explicit_apply",
            ),
            _tool_contract(
                name="ask_memory_clarification",
                title="Ask Memory Clarification",
                description="Return bounded clarification questions required by Grow or guided write.",
                category="write",
                planned_slice="PR-12J-C",
                default_output_package="clarification_request",
                input_schema=_schema_object(properties={"question_ids": _array("Question ids.", item_type="string"), "answers": _object("User answers.")}),
                output_schema=_clarification_output_schema(),
                candidate_backend_routes=["POST /memory/source-investigation/preview", "POST /memory/preview"],
                mutation_policy="preview_only",
            ),
        ]
    )

    contracts.append(
        _tool_contract(
            name="brain_health",
            title="Brain Health",
            description="Return a fast, non-mutating recommendation for Grow repair, Sleep, Evolve, matrix calibration or rebuild.",
            category="maintenance",
            planned_slice="Phase-4-Brain-Health",
            default_output_package="brain_health_report",
            input_schema=_brain_health_input_schema(),
            output_schema=_brain_health_output_schema(),
            candidate_backend_routes=["GET /memory/brain-health", "POST /mcp/brain-health"],
            mutation_policy="read_only",
        )
    )

    contracts.append(
        _tool_contract(
            name="geometry_calibration_preview",
            title="Geometry Calibration Preview",
            description=(
                "Prepare a non-mutating Geometry Calibration preview for brain placement and distribution drift. "
                "This is the canonical V1 tool; it does not expose a user-facing matrix."
                + _advanced_module_visibility_note()
            ),
            category="maintenance",
            planned_slice="PR-12J-D",
            default_output_package="brain_geometry_calibration",
            input_schema=_matrix_calibration_input_schema(),
            output_schema=_matrix_calibration_output_schema(),
            candidate_backend_routes=["POST /memory/mcp/geometry-calibration-preview", "GET /memory/geometry-calibration"],
            mutation_policy="preview_only",
        )
    )

    contracts.append(
        _tool_contract(
            name="geometry_calibration_apply",
            title="Geometry Calibration Apply",
            description=(
                "Apply one reviewed Geometry Calibration plan with explicit confirmation, a complete rollback snapshot and before/after proof."
                + _advanced_module_visibility_note()
            ),
            category="maintenance",
            planned_slice="PR-12J-D",
            default_output_package="before_after_audit",
            input_schema=_matrix_calibration_apply_input_schema(),
            output_schema=_matrix_calibration_output_schema(),
            candidate_backend_routes=["POST /memory/mcp/geometry-calibration-apply"],
            mutation_policy="explicit_apply",
        )
    )

    contracts.append(
        _tool_contract(
            name="geometry_calibration_rollback",
            title="Geometry Calibration Rollback",
            description=(
                "Atomically restore the complete snapshot for one applied Geometry Calibration plan. "
                "Requires its exact plan_signature and explicit confirmation."
                + _advanced_module_visibility_note()
            ),
            category="maintenance",
            planned_slice="PR-12J-D",
            default_output_package="rollback_result",
            input_schema=_geometry_calibration_rollback_input_schema(),
            output_schema=_geometry_calibration_rollback_output_schema(),
            candidate_backend_routes=["POST /memory/mcp/geometry-calibration-rollback"],
            mutation_policy="explicit_apply",
        )
    )

    contracts.append(
        _tool_contract(
            name="matrix_calibration_preview",
            title="Matrix Calibration Preview (Deprecated Alias)",
            description=(
                "Backward-compatible alias for geometry_calibration_preview. New clients must use the Geometry Calibration name."
                + _advanced_module_visibility_note()
            ),
            category="maintenance",
            planned_slice="PR-12J-D",
            default_output_package="brain_geometry_calibration",
            input_schema=_matrix_calibration_input_schema(),
            output_schema=_matrix_calibration_output_schema(),
            candidate_backend_routes=["POST /memory/mcp/matrix-calibration-preview", "GET /memory/geometry-calibration"],
            mutation_policy="preview_only",
        )
    )

    contracts.append(
        _tool_contract(
            name="matrix_calibration_apply",
            title="Matrix Calibration Apply (Deprecated Alias)",
            description=(
                "Backward-compatible alias for geometry_calibration_apply. New clients must use the Geometry Calibration name."
                + _advanced_module_visibility_note()
            ),
            category="maintenance",
            planned_slice="PR-12J-D",
            default_output_package="before_after_audit",
            input_schema=_matrix_calibration_apply_input_schema(),
            output_schema=_matrix_calibration_output_schema(),
            candidate_backend_routes=["POST /memory/mcp/matrix-calibration-apply"],
            mutation_policy="explicit_apply",
        )
    )

    contracts.append(
        _tool_contract(
            name="matrix_calibration_rollback",
            title="Matrix Calibration Rollback (Deprecated Alias)",
            description=(
                "Backward-compatible alias for geometry_calibration_rollback. New clients must use the Geometry Calibration name."
                + _advanced_module_visibility_note()
            ),
            category="maintenance",
            planned_slice="PR-12J-D",
            default_output_package="rollback_result",
            input_schema=_geometry_calibration_rollback_input_schema(),
            output_schema=_geometry_calibration_rollback_output_schema(),
            candidate_backend_routes=["POST /memory/mcp/matrix-calibration-rollback"],
            mutation_policy="explicit_apply",
        )
    )

    for name, title, mode, apply_tool in [
        ("sleep_preview", "Sleep Preview", "sleep", False),
        ("sleep_apply", "Sleep Apply", "sleep", True),
        ("evolve_preview", "Evolve Preview", "evolve", False),
        ("evolve_apply", "Evolve Apply", "evolve", True),
    ]:
        contracts.append(
            _tool_contract(
                name=name,
                title=title,
                description=(
                    f"Run a non-mutating {mode} maintenance preview that returns reviewable proposals. "
                    "Do not infer mutation from preview_ready; apply requires the matching apply tool."
                    + _advanced_module_visibility_note()
                    if not apply_tool
                    else (
                        f"Apply reviewed {mode} maintenance proposals. Requires preview_signature and selected_proposal_ids from {mode}_preview, plus confirm_apply=true."
                        + _advanced_module_visibility_note()
                    )
                ),
                category="maintenance",
                planned_slice="PR-12J-D",
                default_output_package="maintenance_report",
                input_schema=_maintenance_input_schema(apply=apply_tool),
                output_schema=_maintenance_output_schema(apply=apply_tool),
                candidate_backend_routes=[
                    "POST /memory/mcp/sleep-preview",
                    "POST /memory/mcp/sleep-apply",
                    "POST /memory/mcp/evolve-preview",
                    "POST /memory/mcp/evolve-apply",
                    "POST /memory/sleep",
                    "POST /memory/evolve",
                ],
                mutation_policy="explicit_apply" if apply_tool else "preview_only",
            )
        )

    for name, title, mode in [
        ("sleep_rollback", "Sleep Rollback", "sleep"),
        ("evolve_rollback", "Evolve Rollback", "evolve"),
    ]:
        contracts.append(
            _tool_contract(
                name=name,
                title=title,
                description=(
                    f"Atomically restore the exact persistent graph revision captured by one applied {mode} preview. "
                    "Requires its preview_signature and confirm_rollback=true."
                    + _advanced_module_visibility_note()
                ),
                category="maintenance",
                planned_slice="PR-12J-D",
                default_output_package="rollback_result",
                input_schema=_maintenance_rollback_input_schema(),
                output_schema=_maintenance_rollback_output_schema(),
                candidate_backend_routes=[f"POST /memory/mcp/{mode}-rollback"],
                mutation_policy="explicit_apply",
            )
        )

    for name, field_name, description in [
        ("list_open_questions", "open_questions", "Open questions from retrieval, Grow and learning policy."),
        ("list_hypotheses", "hypotheses", "Hypotheses and inferred memories requiring review."),
        ("list_contradictions", "contradictions", "Contradictions and source conflicts requiring review."),
        ("list_memory_os_processes", "processes", "Running or recent memory OS processes."),
    ]:
        contracts.append(
            _tool_contract(
                name=name,
                title=name.replace("_", " ").title(),
                description=description + _advanced_module_visibility_note(),
                category="maintenance",
                planned_slice="PR-12J-D",
                default_output_package=field_name,
                input_schema=_schema_object(properties={"limit": _integer("Maximum rows.", minimum=1, maximum=100, default=25)}),
                output_schema=_list_output_schema(field_name, description),
                candidate_backend_routes=[
                    f"POST /memory/mcp/{name.replace('_', '-')}",
                    "POST /memory/sleep",
                    "POST /memory/evolve",
                    "GET /memory/get-trace/{search_id}",
                ],
            )
        )
    contracts.extend(_build_usage_guide_tool_contracts())
    contracts.extend(_build_agent_memory_tool_contracts())
    contracts.extend(_build_brain_bootstrap_v1_tool_contracts())
    contracts.extend(_build_brain_profile_v1_tool_contracts())
    order = {name: index for index, name in enumerate([*GUIDE_MCP_TOOL_NAMES, *REQUIRED_MCP_TOOL_NAMES, *AGENT_MEMORY_MCP_TOOL_NAMES])}
    contracts.sort(key=lambda tool: order.get(str(tool.get("name") or ""), len(order)))
    return contracts


def _build_usage_guide_tool_contracts() -> list[dict[str, Any]]:
    return [
        _tool_contract(
            name="get_agvm_usage_guide",
            title="Get AGVM Usage Guide",
            description="Return the AGVM MCP operating guide for generic AI clients before brain selection or retrieval.",
            category="agent_memory",
            planned_slice="MCP-AM-5",
            default_output_package="markdown_guide",
            input_schema=_schema_object(properties={}, description="No input required. Call this immediately after MCP connection."),
            output_schema=_usage_guide_output_schema(),
            candidate_backend_routes=["GET /mcp/usage-guide"],
            endpoint_path="/mcp/usage-guide",
            http_method="GET",
            requires_brain_id=False,
            scope_policy="global",
            permission_family="read_only",
            client_usage={
                "when_to_use": "Use as the first AGVM MCP call. It explains brain selection, retrieval query writing, document hydration and mutation safety.",
                "default_output_package": "markdown_guide",
                "mutation_policy": "read_only",
                "must_not": ["Do not skip brain selection guidance when connecting a new client."],
                "result_handling": [
                    "Read markdown_guide for human-readable operating rules.",
                    "Use policy and query_recipes for machine-readable planning.",
                    "Follow recommended_flow before retrieval.",
                ],
                "followups": ["list_brains", "ensure_brain", "retrieve_context"],
            },
        )
    ]


def _build_brain_bootstrap_v1_tool_contracts() -> list[dict[str, Any]]:
    contracts: list[dict[str, Any]] = []
    for operation in ("start", "status", "answer", "add_source", "preview", "apply", "resume", "recover", "cancel"):
        tool_name = f"brain_bootstrap_{operation}"
        mutation_policy = (
            "read_only"
            if operation == "status"
            else "preview_only"
            if operation == "preview"
            else "explicit_apply"
            if operation == "apply"
            else "registry_write"
        )
        contracts.append(
            _tool_contract(
                name=tool_name,
                title=f"Brain Bootstrap {operation.replace('_', ' ').title()}",
                description=(
                    f"Run bounded Brain Bootstrap V1 {operation}. Manual interview, manual sources and Grow review are local and free. "
                    "AI research, fitting, backfill and activation return a cloud-required action_contract. "
                    "Answers and sources never enter memory before brain_bootstrap_apply with explicit confirmation."
                ),
                category="agent_memory",
                planned_slice="Brain-Bootstrap-V1",
                default_output_package="session",
                input_schema=_brain_bootstrap_v1_input_schema(operation),
                output_schema=_brain_bootstrap_v1_output_schema(),
                candidate_backend_routes=[f"POST /mcp/brain-bootstrap-{operation.replace('_', '-')}"],
                endpoint_path=f"/mcp/brain-bootstrap-{operation.replace('_', '-')}",
                mutation_policy=mutation_policy,
                permission_family=mutation_policy,
                scope_policy=("brain" if operation == "status" else "brain_apply" if operation == "apply" else "brain_preview"),
                client_usage={
                    "when_to_use": f"Use for bounded Bootstrap V1 {operation} in an explicitly selected brain.",
                    "default_output_package": "session",
                    "mutation_policy": mutation_policy,
                    "must_not": [
                        "Do not claim that interview answers or sources are memory before explicit apply.",
                        "Do not execute AI research, fitting, backfill or activation locally; follow action_contract.",
                        "Do not retry a mutating command with a new idempotency key after an ambiguous failure.",
                    ],
                    "followups": ["brain_bootstrap_status", "brain_bootstrap_preview", "brain_bootstrap_apply"],
                },
            )
        )
    return contracts


def _build_brain_profile_v1_tool_contracts() -> list[dict[str, Any]]:
    contracts: list[dict[str, Any]] = []
    for operation in ("preview", "apply", "rollback"):
        tool_name = f"brain_profile_{operation}"
        mutation_policy = "preview_only" if operation == "preview" else "explicit_apply"
        contracts.append(
            _tool_contract(
                name=tool_name,
                title=f"Brain Profile {operation.title()}",
                description=(
                    f"Run bounded Brain Profile V1 {operation} over exactly 12 canonical routing dimensions. "
                    "Preview is shadow-only and non-mutating. Apply requires a signed active profile, complete green benchmark, "
                    "explicit confirmation, expected-revision CAS and idempotency. Rollback restores the immediately previous "
                    "runtime revision byte-for-byte. Fitting, backfill and activation require a paid lease or return action_contract."
                ),
                category="agent_memory",
                planned_slice="Brain-Profile-V1",
                default_output_package="brain_profile_runtime",
                input_schema=_brain_profile_v1_input_schema(operation),
                output_schema=_brain_profile_v1_output_schema(),
                candidate_backend_routes=[f"POST /mcp/brain-profile-{operation}"],
                endpoint_path=f"/mcp/brain-profile-{operation}",
                mutation_policy=mutation_policy,
                permission_family=mutation_policy,
                scope_policy="brain_preview" if operation == "preview" else "brain_apply",
                client_usage={
                    "when_to_use": f"Use for Brain Profile V1 {operation} in one explicitly selected brain.",
                    "default_output_package": "brain_profile_runtime",
                    "mutation_policy": mutation_policy,
                    "must_not": [
                        "Do not treat a shadow preview as active configuration.",
                        "Do not activate without a complete green benchmark and explicit confirmation.",
                        "Do not retry an ambiguous mutation with a different idempotency key.",
                        "Do not execute paid fitting, backfill or activation locally without a valid lease; follow action_contract.",
                    ],
                    "followups": ["brain_profile_preview", "brain_profile_apply", "brain_profile_rollback"],
                },
            )
        )
    return contracts


def _build_agent_memory_tool_contracts() -> list[dict[str, Any]]:
    return [
        _tool_contract(
            name="list_brains",
            title="List AGVM Brains",
            description="List local AGVM brains so an AI client can choose an explicit memory scope before retrieval.",
            category="agent_memory",
            planned_slice="MCP-AM-2",
            default_output_package="brains",
            input_schema=_schema_object(properties={}, description="List local brains input contract."),
            output_schema=_brain_registry_output_schema(),
            candidate_backend_routes=["GET /mcp/brains"],
            endpoint_path="/mcp/brains",
            http_method="GET",
            requires_brain_id=False,
            scope_policy="registry",
            permission_family="read_only",
            client_usage={
                "when_to_use": "Use as the first registry discovery call when connecting an AI client to AGVM.",
                "default_output_package": "brains",
                "mutation_policy": "read_only",
                "must_not": ["Do not infer that the active brain is the right project scope without checking user intent."],
                "followups": ["ensure_brain", "select_brain", "retrieve_context"],
            },
        ),
        _tool_contract(
            name="active_brain",
            title="Inspect Active AGVM Brain",
            description="Return the current active local brain summary without changing registry state.",
            category="agent_memory",
            planned_slice="MCP-AM-2",
            default_output_package="brain_id",
            input_schema=_schema_object(properties={}, description="Inspect active brain input contract."),
            output_schema=_active_brain_output_schema(),
            candidate_backend_routes=["GET /memory/brains/active", "GET /mcp/brains/active"],
            endpoint_path="/mcp/brains/active",
            http_method="GET",
            requires_brain_id=False,
            scope_policy="registry",
            permission_family="read_only",
            client_usage={
                "when_to_use": "Use to inspect shared active state; prefer explicit brain_id for durable agent sessions.",
                "default_output_package": "brain_id",
                "mutation_policy": "read_only",
                "must_not": ["Do not treat active brain as durable session state for all future calls."],
                "followups": ["ensure_brain", "retrieve_context"],
            },
        ),
        _tool_contract(
            name="create_brain",
            title="Create AGVM Brain",
            description="Create a local AGVM brain with explicit activation/default policy.",
            category="agent_memory",
            planned_slice="MCP-AM-2",
            default_output_package="brain",
            input_schema=_brain_create_input_schema(),
            output_schema=_brain_admin_operation_output_schema(),
            candidate_backend_routes=["POST /mcp/brains/create"],
            endpoint_path="/mcp/brains/create",
            requires_brain_id=False,
            scope_policy="registry",
            permission_family="registry_write",
            mutation_policy="registry_write",
            client_usage={
                "when_to_use": "Use only when no suitable existing brain is available and the user intent allows creating a new memory scope.",
                "default_output_package": "brain",
                "mutation_policy": "registry_write",
                "must_not": ["Do not omit make_active/make_default.", "Do not create duplicate brains for the same project when ensure_brain can resolve one."],
                "followups": ["retrieve_context", "grow_source_preview", "write_memory_preview"],
            },
        ),
        _tool_contract(
            name="select_brain",
            title="Select AGVM Brain",
            description="Select an existing brain as active/default. This changes shared local registry state visible to UI and other clients.",
            category="agent_memory",
            planned_slice="MCP-AM-2",
            default_output_package="brains",
            input_schema=_brain_select_input_schema(),
            output_schema=_brain_registry_output_schema(),
            candidate_backend_routes=["POST /mcp/select-brain"],
            endpoint_path="/mcp/select-brain",
            requires_brain_id=False,
            scope_policy="registry",
            permission_family="registry_write",
            mutation_policy="registry_write",
            client_usage={
                "when_to_use": "Use only when the user explicitly wants to change the shared active/default brain.",
                "default_output_package": "brains",
                "mutation_policy": "registry_write",
                "must_not": ["Do not silently change active/default brain for other local clients."],
                "followups": ["retrieve_context", "brain_health"],
            },
        ),
        _tool_contract(
            name="ensure_brain",
            title="Ensure AGVM Brain",
            description="Resolve or create a local AGVM brain idempotently for an AI client, defaulting to explicit brain_id return without global selection.",
            category="agent_memory",
            planned_slice="MCP-AM-2",
            default_output_package="brain",
            input_schema=_ensure_brain_input_schema(),
            output_schema=_ensure_brain_output_schema(),
            candidate_backend_routes=["POST /mcp/brains/ensure"],
            endpoint_path="/mcp/brains/ensure",
            requires_brain_id=False,
            scope_policy="registry",
            permission_family="registry_write",
            mutation_policy="registry_write",
            client_usage={
                "when_to_use": "Use during AI-client onboarding to get an explicit brain_id for the current user, project or task.",
                "default_output_package": "brain",
                "mutation_policy": "registry_write",
                "must_not": ["Do not default to global activation; use activation_policy=return_only unless the user asks otherwise."],
                "followups": ["retrieve_context", "grow_source_preview", "write_memory_preview"],
            },
        ),
    ]


def validate_mcp_contract_registry(
    registry: dict[str, Any],
    *,
    required_tool_names: list[str] | None = None,
) -> dict[str, Any]:
    tools = [dict(tool) for tool in list(registry.get("tools") or []) if isinstance(tool, dict)]
    names = [str(tool.get("name") or "") for tool in tools]
    required_names = list(REQUIRED_MCP_TOOL_NAMES if required_tool_names is None else required_tool_names)
    missing_tools = [name for name in required_names if name not in names]
    duplicate_tools = sorted({name for name in names if names.count(name) > 1 and name})
    schema_errors: list[dict[str, Any]] = []
    answer_demo_default_tools: list[str] = []
    for tool in tools:
        name = str(tool.get("name") or "")
        if bool(tool.get("default_includes_answer_demo")):
            answer_demo_default_tools.append(name)
        for schema_key in ("input_schema", "output_schema"):
            schema = dict(tool.get(schema_key) or {})
            if schema.get("type") != "object":
                schema_errors.append({"tool": name, "schema": schema_key, "reason": "schema_type_not_object"})
            if not isinstance(schema.get("properties"), dict):
                schema_errors.append({"tool": name, "schema": schema_key, "reason": "schema_properties_missing"})
        if not tool.get("default_output_package"):
            schema_errors.append({"tool": name, "schema": "contract", "reason": "default_output_package_missing"})
        endpoint_path = str(tool.get("endpoint_path") or "")
        if endpoint_path and not endpoint_path.startswith("/"):
            schema_errors.append({"tool": name, "schema": "contract_metadata", "reason": "endpoint_path_must_be_absolute"})
        http_method = str(tool.get("http_method") or "")
        if http_method and http_method not in MCP_CONTRACT_HTTP_METHODS:
            schema_errors.append({"tool": name, "schema": "contract_metadata", "reason": "http_method_not_supported"})
        if "requires_brain_id" in tool and not isinstance(tool.get("requires_brain_id"), bool):
            schema_errors.append({"tool": name, "schema": "contract_metadata", "reason": "requires_brain_id_must_be_boolean"})
        scope_policy = str(tool.get("scope_policy") or "")
        if scope_policy and scope_policy not in MCP_CONTRACT_SCOPE_POLICIES:
            schema_errors.append({"tool": name, "schema": "contract_metadata", "reason": "scope_policy_not_supported"})
        permission_family = str(tool.get("permission_family") or "")
        if permission_family and permission_family not in MCP_CONTRACT_PERMISSION_FAMILIES:
            schema_errors.append({"tool": name, "schema": "contract_metadata", "reason": "permission_family_not_supported"})
        if "client_usage" in tool and not isinstance(tool.get("client_usage"), dict):
            schema_errors.append({"tool": name, "schema": "contract_metadata", "reason": "client_usage_must_be_object"})
        schema_errors.extend(validate_mcp_tool_registration(tool))
        module_requirement = dict(tool.get("module_requirement") or {})
        required_module_id = str(module_requirement.get("module_id") or "")
        registration_required_module_id = str(dict(tool.get("tool_registration") or {}).get("required_module_id") or "")
        if required_module_id != registration_required_module_id:
            schema_errors.append({"tool": name, "schema": "module_requirement", "reason": "module_requirement_registration_mismatch"})
    module_registration = dict(registry.get("module_tool_registration") or {})
    if module_registration.get("state") != MCP_TOOL_REGISTRATION_STATE:
        schema_errors.append({"tool": "__registry__", "schema": "module_tool_registration", "reason": "module_tool_registration_state_not_supported"})
    passed = not missing_tools and not duplicate_tools and not schema_errors and not answer_demo_default_tools
    return {
        "passed": passed,
        "required_tool_count": len(required_names),
        "registered_tool_count": len(tools),
        "missing_tools": missing_tools,
        "duplicate_tools": duplicate_tools,
        "schema_errors": schema_errors,
        "answer_demo_default_tools": answer_demo_default_tools,
        "output_law_count": len(list(registry.get("output_laws") or [])),
    }


def build_mcp_contract_registry() -> dict[str, Any]:
    tools = _build_tool_contracts()
    registry = {
        "schema_version": MCP_CONTRACT_REGISTRY_SCHEMA_VERSION,
        "source_slice": MCP_CONTRACT_REGISTRY_SLICE,
        "registry_status": "schema_registry_ready",
        "tool_schema_version": MCP_TOOL_CONTRACT_SCHEMA_VERSION,
        "guide_tool_names": list(GUIDE_MCP_TOOL_NAMES),
        "required_tool_names": list(REQUIRED_MCP_TOOL_NAMES),
        "agent_memory_tool_names": list(AGENT_MEMORY_MCP_TOOL_NAMES),
        "staged_tool_names": [],
        "tools": tools,
        "module_tool_registration": build_mcp_module_tool_registration_summary(tools),
        "output_laws": list(MCP_OUTPUT_LAWS),
        "answer_demo_policy": {
            "default_included": False,
            "request_flag": "include_answer_demo",
            "primary_mcp_surface": "context_package_or_source_package",
            "if_answer_and_context_disagree": "context_package_wins_answer_is_bug",
            "materialization_law": "context_package_ready_is_independent_from_answer_demo_ready",
            "default_request_response_mode": "context",
            "with_answer_demo_response_mode": "both",
        },
        "implementation_granularity": {
            "current_slice": MCP_CONTRACT_REGISTRY_SLICE,
            "latest_implemented_slice": "PR-12P-13",
            "registry_only": False,
            "next_slice": "PR-12P-14 Launch Readiness Report, RAG Comparison And Improvement Backlog",
            "operational_adapters_deferred": [],
            "implemented_adapter_slices": ["PR-12J-B", "PR-12J-C", "PR-12J-D"],
            "stability_slices": ["PR-12J-E", "PR-12P-10I", "PR-12P-11", "PR-12P-12", "PR-12P-12A", "PR-12P-12B", "PR-12P-13"],
            "mcp_surface_status": "complete_through_pr12p_13_product_ready_local_gate",
            "agent_memory_surface_status": "agent_memory_tools_callable_mcp_am_4",
            "usage_guide_surface_status": "usage_guide_tool_callable_mcp_am_5",
        },
    }
    registry["registry_validation"] = validate_mcp_contract_registry(registry)
    return registry


def build_local_core_mcp_contract_registry() -> dict[str, Any]:
    """Project the canonical catalog onto the non-mutating Local Core surface."""

    registry = build_mcp_contract_registry()
    projected_tools: list[dict[str, Any]] = []
    for source_tool in list(registry.get("tools") or []):
        tool = dict(source_tool)
        name = str(tool.get("name") or "").strip()
        if not local_core_tool_is_discoverable(name):
            continue
        if name in LOCAL_CORE_MAINTAIN_CLOUD_HANDOFF_TOOL_NAMES:
            tool["tool_registration"] = mark_local_core_cloud_handoff_registration(
                dict(tool.get("tool_registration") or {})
            )
            tool["module_requirement"] = build_module_requirement_from_registration(
                tool["tool_registration"]
            )
            tool["backend_binding"] = {
                **dict(tool.get("backend_binding") or {}),
                "runtime": "hosted_mcp",
                "local_adapter": "cloud_handoff_only",
                "local_execution_available": False,
            }
            tool["safety_contract"] = {
                **dict(tool.get("safety_contract") or {}),
                "local_graph_mutation": "forbidden",
                "cloud_execution_required": True,
                "entitlement_bypass_allowed": False,
            }
            tool["client_usage"] = {
                **dict(tool.get("client_usage") or {}),
                "when_to_use": (
                    "Use this Local Core surface to discover the paid capability and obtain the Hosted MCP "
                    "handoff. Execution remains in Detwin Cloud."
                ),
                "mutation_policy": "cloud_only",
                "followups": ["connect_detwin_cloud", "run_with_hosted_mcp"],
            }
        projected_tools.append(tool)

    names = {str(tool.get("name") or "") for tool in projected_tools}
    local_required = [name for name in REQUIRED_MCP_TOOL_NAMES if name in names]
    registry.update(
        {
            "registry_status": "schema_registry_ready",
            "required_tool_names": local_required,
            "agent_memory_tool_names": [name for name in AGENT_MEMORY_MCP_TOOL_NAMES if name in names],
            "tools": projected_tools,
            "module_tool_registration": build_mcp_module_tool_registration_summary(projected_tools),
        }
    )
    registry["implementation_granularity"] = {
        **dict(registry.get("implementation_granularity") or {}),
        "runtime_surface": "local_core",
        "maintain_execution_surface": "hosted_mcp_only",
        "local_maintain_surface": "read_only_preview_handoff",
    }
    registry["registry_validation"] = validate_mcp_contract_registry(
        registry,
        required_tool_names=local_required,
    )
    return registry
