# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator

DocumentLookupKind = Literal[
    "none",
    "exact_document_lookup",
    "exact_document_id_lookup",
    "direct_document_lookup",
    "related_document_lookup",
    "related_context_document",
    "document_synthesis",
    "source_trace_for_answer",
    "no_document_found",
]


class SemanticScores(BaseModel):
    self_core: float = Field(ge=0.0, le=1.0)
    values: float = Field(ge=0.0, le=1.0)
    identity_style: float = Field(ge=0.0, le=1.0)
    projectual: float = Field(ge=0.0, le=1.0)
    technical: float = Field(ge=0.0, le=1.0)
    operational: float = Field(ge=0.0, le=1.0)
    documental: float = Field(ge=0.0, le=1.0)
    conceptual: float = Field(ge=0.0, le=1.0)
    meta: float = Field(ge=0.0, le=1.0)
    relational: float = Field(ge=0.0, le=1.0)
    emotional: float = Field(ge=0.0, le=1.0)
    episodic: float = Field(ge=0.0, le=1.0)


class Facets(BaseModel):
    temporal_scope: float = Field(ge=0.0, le=1.0)
    abstraction_level: float = Field(ge=0.0, le=1.0)
    planning_horizon: float = Field(ge=0.0, le=1.0)
    agency: float = Field(ge=0.0, le=1.0)
    intimacy: float = Field(ge=0.0, le=1.0)
    institutional_vs_personal: float = Field(ge=0.0, le=1.0)
    source_reliability: float = Field(ge=0.0, le=1.0)
    expression_intensity: float = Field(ge=0.0, le=1.0)
    role_density: float = Field(ge=0.0, le=1.0)
    modality_bias: float = Field(ge=0.0, le=1.0)
    identity_centrality: float = Field(ge=0.0, le=1.0)
    recurrence_strength: float = Field(ge=0.0, le=1.0)


class Position(BaseModel):
    x: float
    y: float
    z: float


class BrainHex(BaseModel):
    theta_bin: int = Field(ge=0, le=255)
    phi_bin: int = Field(ge=0, le=255)
    radius_bin: int = Field(ge=0, le=255)
    code: str


class DisplayColor(BaseModel):
    h: float
    s: float
    l: float
    hex: str


class Bucket(BaseModel):
    x: int
    y: int
    z: int
    key: str


class Link(BaseModel):
    target_node_id: str
    strength: float = Field(ge=0.0, le=1.0)
    reason: str
    kind: str | None = None
    stability: float | None = None


class Provenance(BaseModel):
    mode: str
    source_label: str | None = None
    source_type: str | None = None
    guide_conceptual_area: str | None = None


SourceTrust = Literal["verified_public", "user_asserted", "uploaded_document", "synthetic_test", "inferred", "system_metadata"]
ClaimStatus = Literal["fact", "hypothesis", "source_metadata", "instruction", "test_artifact"]
LearningMode = Literal["strict_review", "guided_learning", "autonomous_cautious", "autonomous_research", "sleep_review"]
SourceInvestigationKind = Literal["manual_text", "url", "website", "pdf", "docx", "image", "transcript", "mixed_bundle", "unknown"]
SourceInvestigationStatus = Literal[
    "created",
    "detecting_source",
    "options_required",
    "extracting_text",
    "extracting_images",
    "running_ocr",
    "crawling_site",
    "using_browser_budget",
    "online_enrichment",
    "resolving_entities",
    "asking_clarification",
    "building_compiler_handoff",
    "preview_ready",
    "applied",
    "partial_budget_exhausted",
    "rejected_unsafe_source",
    "failed",
]
SourceUnitKind = Literal["document_page", "document_section", "web_page", "web_section", "image", "ocr_block", "manual_block"]


class DebugPayload(BaseModel):
    candidate_ids: list[str] = Field(default_factory=list)
    candidate_sources: dict[str, list[str]] = Field(default_factory=dict)
    bucket_key: str | None = None
    document_anchor_candidate_ids: list[str] = Field(default_factory=list)
    highway_expansion_ids: list[str] = Field(default_factory=list)
    suggested_origin_node_id: str | None = None


class GraphEdge(BaseModel):
    source_node_id: str
    target_node_id: str
    edge_type: Literal["derives_from", "mentions_entity"]
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str


class VectorNode(BaseModel):
    id: str
    node_kind: str
    memory_type: str
    raw_text: str
    summary: str
    routing_semantic_scores: SemanticScores
    routing_facets: Facets
    routing_brainhex: BrainHex
    semantic_color: DisplayColor
    base_position: Position
    final_position: Position
    topology_brainhex: BrainHex
    topology_color: DisplayColor
    bucket: Bucket
    is_document_anchor: bool = False
    is_summary: bool = False
    granularity: float = Field(ge=0.0, le=1.0)
    novelty: float = Field(ge=0.0, le=1.0)
    links: list[Link] = Field(default_factory=list)
    highways: list[Link] = Field(default_factory=list)
    provenance: Provenance
    debug: DebugPayload | None = None
    derivation_role: str | None = None
    derivation_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    derived_from_preview_id: str | None = None
    document_role: Literal["anchor", "summary", "chunk", "fact"] | None = None
    document_anchor_id: str | None = None
    document_chunk_index: int | None = None
    source_unit_id: str | None = None
    source_unit_title: str | None = None
    source_unit_kind: str | None = None
    source_unit_role: str | None = None
    promotion_role: str | None = None
    source_unit_formation_strategy: str | None = None
    source_span_start: int | None = None
    source_span_end: int | None = None
    memory_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    identity_resolution_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    stability_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    sleep_revision_count: int = Field(default=0, ge=0)
    last_sleep_review_at: str | None = None
    temporal_role: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    observed_at: str | None = None
    superseded_by: str | None = None
    obsoletes: list[str] = Field(default_factory=list)
    temporal_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    lifecycle_status: Literal["active", "superseded", "archived", "deleted"] = "active"
    source_trust: SourceTrust = "user_asserted"
    claim_status: ClaimStatus = "fact"
    answer_eligible: bool = True
    profile_eligible: bool = True
    document_eligible: bool = True
    retrieval_affordance: dict[str, Any] = Field(default_factory=dict)
    retrieval_aliases: list[str] = Field(default_factory=list)
    matrix_revision_id: str | None = None
    topology_revision_id: str | None = None
    matrix_calibration_plan_signature: str | None = None
    matrix_calibrated_at: str | None = None
    active_matrix_projection: dict[str, Any] = Field(default_factory=dict)
    geometry_profile_context: dict[str, Any] = Field(default_factory=dict)


class Graph(BaseModel):
    version: str
    graph_name: str
    nodes: list[VectorNode]
    edges: list[GraphEdge] = Field(default_factory=list)
    meta: dict[str, Any]


class GraphResponse(BaseModel):
    graph: Graph


class PreviewRequest(BaseModel):
    brain_id: str | None = None
    text: str = Field(min_length=1)
    input_mode: Literal["auto", "document"] = "auto"
    source_label: str | None = None
    source_type: str | None = None
    source_trust: SourceTrust | None = None
    learning_mode: LearningMode = "strict_review"
    question_limit: int = Field(default=3, ge=1, le=8)

    @field_validator("text")
    @classmethod
    def trim_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("text_required")
        return value


class PreviewNode(VectorNode):
    preview_kind: Literal["primary", "claim", "entity"]
    preview_label: str
    selected_by_default: bool
    preview_confidence: float = Field(ge=0.0, le=1.0)
    persist_mode: Literal["create", "merge_into_existing", "attach_as_alias_or_variant"] = "create"
    merge_target_node_id: str | None = None
    identity_resolution_target_node_id: str | None = None
    identity_resolution_type: str | None = None
    memory_act_type: str | None = None
    cognitive_status: str | None = None
    requires_human_review: bool = False
    cognitive_review_reasons: list[str] = Field(default_factory=list)
    cognitive_target_node_ids: list[str] = Field(default_factory=list)
    learning_mode: LearningMode | None = None
    learning_action: str | None = None
    learning_question_ids: list[str] = Field(default_factory=list)
    learning_policy_reasons: list[str] = Field(default_factory=list)
    source_grounding_status: str | None = None
    source_grounding_score: float | None = Field(default=None, ge=0.0, le=1.0)
    source_grounding_reasons: list[str] = Field(default_factory=list)


class PreviewEdge(BaseModel):
    source_preview_id: str
    target_preview_id: str
    edge_type: Literal["derives_from", "mentions_entity"]
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str


class DerivationWarning(BaseModel):
    code: str
    message: str


class WriteTraceActor(BaseModel):
    actor_id: str
    actor_kind: Literal["compiler", "merge_resolver", "identity_resolver", "persistence_stage"]
    status: Literal["completed", "pending"]
    summary: str
    metrics: dict[str, Any] = Field(default_factory=dict)


class WriteTraceStage(BaseModel):
    stage_id: Literal[
        "input_received",
        "primary_projection_ready",
        "derived_nodes_ready",
        "merge_review_ready",
        "identity_resolution_ready",
        "cognitive_write_ready",
        "learning_policy_ready",
        "review_ready",
        "persist_complete",
    ]
    actor_id: str
    actor_kind: Literal["compiler", "merge_resolver", "identity_resolver", "persistence_stage"]
    status: Literal["completed", "pending"]
    summary: str
    counts: dict[str, int] = Field(default_factory=dict)


class WriteTrace(BaseModel):
    mode: Literal["write_preview", "write_persist"]
    input_mode: Literal["auto", "document"] | None = None
    derivation_mode: Literal["llm", "heuristic"] | None = None
    actors: list[WriteTraceActor] = Field(default_factory=list)
    stages: list[WriteTraceStage] = Field(default_factory=list)
    merge_decision_summary: dict[str, Any] = Field(default_factory=dict)
    identity_resolution_summary: dict[str, Any] = Field(default_factory=dict)
    cognitive_write_summary: dict[str, Any] = Field(default_factory=dict)
    learning_policy_summary: dict[str, Any] = Field(default_factory=dict)
    persisted_node_summary: dict[str, Any] = Field(default_factory=dict)


class PreviewBundle(BaseModel):
    brain_id: str | None = None
    primary_node_preview: PreviewNode
    derived_nodes: list[PreviewNode] = Field(default_factory=list)
    derived_edges: list[PreviewEdge] = Field(default_factory=list)
    derivation_mode: Literal["llm", "heuristic"]
    warnings: list[DerivationWarning] = Field(default_factory=list)
    merge_decisions: list[dict[str, Any]] = Field(default_factory=list)
    identity_resolution_decisions: list[dict[str, Any]] = Field(default_factory=list)
    identity_nucleus: dict[str, Any] = Field(default_factory=dict)
    preview_quality_contract: dict[str, Any] = Field(default_factory=dict)
    cognitive_write_plan: dict[str, Any] = Field(default_factory=dict)
    learning_policy: dict[str, Any] = Field(default_factory=dict)
    write_trace: WriteTrace


class SourceInvestigationOptions(BaseModel):
    analyze_images: Literal["off", "ocr_only", "vision_summary"] = "off"
    crawl_sublinks: Literal["off", "same_page", "same_domain", "bounded_external"] = "off"
    use_online_enrichment: bool = False
    metadata_only: bool = False
    use_browser_budget: bool = False
    pause_on_questions: bool = False
    clarification_answers: dict[str, Any] = Field(default_factory=dict)
    clarification_default_policy: Literal["apply_defaults", "pause_when_unanswered"] = "apply_defaults"
    treat_as: Literal["auto", "self_memory", "project_workspace", "public_dossier", "reference_library", "technical_document"] = "auto"
    source_trust: Literal["unknown", "user_asserted", "uploaded_document", "public_web", "verified_public_source"] = "unknown"
    max_pages: int = Field(default=20, ge=1, le=200)
    max_crawl_pages: int = Field(default=20, ge=1, le=100)
    max_depth: int = Field(default=1, ge=0, le=5)
    max_ocr_pages: int = Field(default=8, ge=0, le=100)
    max_images: int = Field(default=12, ge=0, le=100)
    max_online_queries: int = Field(default=4, ge=0, le=50)
    fetch_timeout_seconds: float = Field(default=8.0, ge=0.1, le=30.0)
    compiler_preview_timeout_seconds: float = Field(default=25.0, ge=1.0, le=120.0)
    question_limit: int = Field(default=5, ge=0, le=12)
    max_units: int = Field(default=12, ge=1, le=1024)
    max_urls: int = Field(default=16, ge=0, le=64)
    max_total_chars: int = Field(default=120000, ge=1000, le=500000)


class SourceInvestigationRequest(BaseModel):
    brain_id: str | None = None
    raw_input: str = Field(min_length=1)
    input_kind: Literal["auto", "manual_text", "url", "website", "pdf", "docx", "image", "transcript", "mixed_bundle"] = "auto"
    source_label: str | None = None
    source_uri: str | None = None
    user_instruction: str | None = None
    options: SourceInvestigationOptions = Field(default_factory=SourceInvestigationOptions)
    run_preview: bool = True

    @field_validator("raw_input")
    @classmethod
    def trim_raw_input(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("source_input_required")
        return value

    @field_validator("source_label", "source_uri", "user_instruction")
    @classmethod
    def trim_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class SourceDetection(BaseModel):
    schema_version: str
    source_kind: SourceInvestigationKind
    confidence: float = Field(ge=0.0, le=1.0)
    signals: list[str] = Field(default_factory=list)
    recommended_options: dict[str, Any] = Field(default_factory=dict)
    requires_user_scope: bool = False
    risk_flags: list[str] = Field(default_factory=list)
    detected_language: str | None = None
    url_count: int = Field(default=0, ge=0)
    urls: list[str] = Field(default_factory=list)
    non_url_text_char_count: int = Field(default=0, ge=0)


class SourceUnitProvenance(BaseModel):
    source_label: str | None = None
    source_type: str
    hash: str | None = None
    retrieved_at: str | None = None


class SourceUnit(BaseModel):
    unit_id: str
    kind: SourceUnitKind
    title: str
    source_uri: str | None = None
    page_number: int | None = Field(default=None, ge=1)
    section_path: list[str] = Field(default_factory=list)
    raw_text: str
    clean_text: str
    summary: str
    language: str
    char_count: int = Field(ge=0)
    token_estimate: int = Field(ge=0)
    confidence: float = Field(ge=0.0, le=1.0)
    source_unit_role: str = "primary_evidence"
    promotion_role: str = "primary_evidence"
    fact_eligible: bool = True
    supporting_evidence_eligible: bool = True
    parent_unit_id: str | None = None
    segment_index: int | None = Field(default=None, ge=1)
    segment_count: int | None = Field(default=None, ge=1)
    formation_strategy: str | None = None
    provenance: SourceUnitProvenance
    extraction_trace: dict[str, Any] = Field(default_factory=dict)


class CompilerHandoff(BaseModel):
    handoff_version: str
    source_summary: str
    mega_text: str
    operator_instruction: str | None = None
    operator_instruction_policy: str | None = None
    structured_sections: list[dict[str, Any]] = Field(default_factory=list)
    document_anchor_recommendations: list[dict[str, Any]] = Field(default_factory=list)
    entity_resolution_context: dict[str, Any] = Field(default_factory=dict)
    source_purpose: Literal["self_memory", "project_workspace", "public_dossier", "reference_library", "technical_document", "unknown"]
    recommended_input_mode: Literal["auto", "document"]
    recommended_learning_mode: LearningMode
    must_preserve_raw_text: bool = True
    source_unit_formation: dict[str, Any] = Field(default_factory=dict)
    merge_fork_enrich_policy: dict[str, Any] = Field(default_factory=dict)
    relationships: list[dict[str, Any]] = Field(default_factory=list)
    provenance_map: dict[str, Any] = Field(default_factory=dict)
    questions_and_answers: list[dict[str, Any]] = Field(default_factory=list)
    preview_eligible: bool = False
    preview_blocked_reasons: list[str] = Field(default_factory=list)
    recommended_source_type: str | None = None
    recommended_source_trust: SourceTrust | None = None


class SourceInvestigationPackage(BaseModel):
    schema_version: str
    brain_id: str | None = None
    investigation_id: str
    created_at: str
    status: SourceInvestigationStatus
    source_request: dict[str, Any] = Field(default_factory=dict)
    source_reader_capabilities: dict[str, Any] = Field(default_factory=dict)
    source_detection: SourceDetection
    budgets: dict[str, Any] = Field(default_factory=dict)
    budget_usage: dict[str, Any] = Field(default_factory=dict)
    source_units: list[SourceUnit] = Field(default_factory=list)
    extracted_assets: list[dict[str, Any]] = Field(default_factory=list)
    entities: list[dict[str, Any]] = Field(default_factory=list)
    dates: list[dict[str, Any]] = Field(default_factory=list)
    projects: list[dict[str, Any]] = Field(default_factory=list)
    relationships: list[dict[str, Any]] = Field(default_factory=list)
    source_unit_formation: dict[str, Any] = Field(default_factory=dict)
    open_questions: list[dict[str, Any]] = Field(default_factory=list)
    clarification_questions: list[dict[str, Any]] = Field(default_factory=list)
    guided_grow: dict[str, Any] = Field(default_factory=dict)
    online_enrichment: dict[str, Any] = Field(default_factory=dict)
    source_purpose: dict[str, Any] = Field(default_factory=dict)
    compiler_handoff: CompilerHandoff
    compiler_preview_runtime: dict[str, Any] = Field(default_factory=dict)
    compiler_handoff_proof: dict[str, Any] = Field(default_factory=dict)
    source_formation_contract: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    blocked_reasons: list[str] = Field(default_factory=list)
    timeline: list[dict[str, Any]] = Field(default_factory=list)


class SourceInvestigationResponse(BaseModel):
    source_investigation: SourceInvestigationPackage
    preview_bundle: PreviewBundle | None = None


class McpToolContract(BaseModel):
    schema_version: str
    name: str
    title: str
    description: str
    category: Literal["retrieval", "inspection", "grow", "write", "maintenance", "agent_memory"]
    planned_slice: str
    implementation_status: Literal["schema_registered", "adapter_pending", "implemented"] = "schema_registered"
    endpoint_path: str = ""
    http_method: Literal["GET", "POST"] = "POST"
    requires_brain_id: bool = True
    scope_policy: str = "brain"
    permission_family: str = "read_only"
    tool_registration: dict[str, Any] = Field(default_factory=dict)
    module_requirement: dict[str, Any] = Field(default_factory=dict)
    client_usage: dict[str, Any] = Field(default_factory=dict)
    default_output_package: str
    default_includes_answer_demo: bool = False
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    output_laws: list[str] = Field(default_factory=list)
    backend_binding: dict[str, Any] = Field(default_factory=dict)
    safety_contract: dict[str, Any] = Field(default_factory=dict)


class McpContractRegistryResponse(BaseModel):
    schema_version: str
    source_slice: str
    registry_status: Literal["schema_registry_ready", "failed"]
    tool_schema_version: str
    guide_tool_names: list[str] = Field(default_factory=list)
    required_tool_names: list[str] = Field(default_factory=list)
    agent_memory_tool_names: list[str] = Field(default_factory=list)
    staged_tool_names: list[str] = Field(default_factory=list)
    tools: list[McpToolContract] = Field(default_factory=list)
    module_tool_registration: dict[str, Any] = Field(default_factory=dict)
    output_laws: list[str] = Field(default_factory=list)
    answer_demo_policy: dict[str, Any] = Field(default_factory=dict)
    implementation_granularity: dict[str, Any] = Field(default_factory=dict)
    registry_validation: dict[str, Any] = Field(default_factory=dict)


class AgvmUsageGuideResponse(BaseModel):
    schema_version: str
    guide_name: str
    markdown_guide: str
    policy: dict[str, Any] = Field(default_factory=dict)
    recommended_flow: list[str] = Field(default_factory=list)
    query_recipes: dict[str, Any] = Field(default_factory=dict)
    tool_map: dict[str, Any] = Field(default_factory=dict)
    first_call: dict[str, Any] = Field(default_factory=dict)


class McpRetrievalToolRequest(BaseModel):
    brain_id: str | None = None
    query_text: str = Field(min_length=1)
    retrieval_mode: Literal["flash", "balanced", "heavy", "forensic"] = "balanced"
    context_package_mode: Literal["answer_minimal", "mcp_operational", "broad_dossier", "document_full", "forensic_trace"] | None = None
    document_text_policy: Literal["refs_only", "top_raw", "all_raw"] = "refs_only"
    thread_id: str | None = None
    max_matches: int = Field(default=12, ge=1, le=24)
    include_raw_text: bool = False
    include_answer_demo: bool = False
    complete_paths: bool = False
    document_id: str | None = None
    document_hint: str | None = None

    @field_validator("query_text")
    @classmethod
    def trim_mcp_query_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query_text_required")
        return value

    @field_validator("brain_id", "thread_id", "document_id", "document_hint")
    @classmethod
    def trim_optional_mcp_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class McpInspectionRequest(BaseModel):
    brain_id: str | None = None
    search_id: str = Field(min_length=1)
    include_debug: bool = False
    include_raw_text: bool = False
    include_answer_demo: bool = False

    @field_validator("search_id")
    @classmethod
    def trim_mcp_search_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("search_id_required")
        return value


class McpMemoryObjectInspectionRequest(BaseModel):
    brain_id: str | None = None
    node_id: str = Field(min_length=1)
    include_debug: bool = False

    @field_validator("node_id")
    @classmethod
    def trim_mcp_node_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("node_id_required")
        return value


class McpToolExecutionResponse(BaseModel):
    schema_version: str
    brain_id: str | None = None
    search_id: str | None = None
    tool_name: str
    status: Literal["ok", "partial", "no_match", "needs_clarification", "blocked", "failed"]
    context_package: dict[str, Any] = Field(default_factory=dict)
    context_package_materialization: dict[str, Any] = Field(default_factory=dict)
    hot_working_memory: dict[str, Any] = Field(default_factory=dict)
    hot_working_memory_contract: dict[str, Any] = Field(default_factory=dict)
    answer_demo_materialization: dict[str, Any] = Field(default_factory=dict)
    semantic_contract_runtime: dict[str, Any] = Field(default_factory=dict)
    metamemory_snapshot: dict[str, Any] = Field(default_factory=dict)
    metamemory_spatial_brief: dict[str, Any] = Field(default_factory=dict)
    metamemory_spatial_brief_summary: dict[str, Any] = Field(default_factory=dict)
    metamemory_spatial_readiness: dict[str, Any] = Field(default_factory=dict)
    ai_spatial_landing_contract: dict[str, Any] = Field(default_factory=dict)
    ai_spatial_landing_contract_runtime: dict[str, Any] = Field(default_factory=dict)
    path_mission_contract: dict[str, Any] = Field(default_factory=dict)
    path_missions: list[dict[str, Any]] = Field(default_factory=list)
    mission_aware_merge_summary: dict[str, Any] = Field(default_factory=dict)
    mission_evidence_ledger: dict[str, Any] = Field(default_factory=dict)
    master_judgement: dict[str, Any] = Field(default_factory=dict)
    mission_learning_rollup: dict[str, Any] = Field(default_factory=dict)
    ai_landing_materialization: dict[str, Any] = Field(default_factory=dict)
    ai_materialization_hard_gate: dict[str, Any] = Field(default_factory=dict)
    mcp_background_cap: dict[str, Any] = Field(default_factory=dict)
    document_workspace: dict[str, Any] = Field(default_factory=dict)
    document_text_policy: Literal["refs_only", "top_raw", "all_raw"] = "refs_only"
    document_refs: list[dict[str, Any]] = Field(default_factory=list)
    document_ref_contract: dict[str, Any] = Field(default_factory=dict)
    document_delivery_contract: dict[str, Any] = Field(default_factory=dict)
    document_bundle: dict[str, Any] = Field(default_factory=dict)
    path_corridors: dict[str, Any] = Field(default_factory=dict)
    route_trace: dict[str, Any] = Field(default_factory=dict)
    memory_object: dict[str, Any] = Field(default_factory=dict)
    source_trace: list[dict[str, Any]] = Field(default_factory=list)
    completeness: dict[str, Any] = Field(default_factory=dict)
    payload_integrity: dict[str, Any] = Field(default_factory=dict)
    payload_truth_contract: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)
    timing: dict[str, Any] = Field(default_factory=dict)
    latency_contract: dict[str, Any] = Field(default_factory=dict)
    completion_contract: dict[str, Any] = Field(default_factory=dict)
    run_lifecycle_contract: dict[str, Any] = Field(default_factory=dict)
    runtime_state_contract: dict[str, Any] = Field(default_factory=dict)
    tool_boundary_contract: dict[str, Any] = Field(default_factory=dict)
    ai_materialization_resilience_contract: dict[str, Any] = Field(default_factory=dict)
    ai_critical_path_contract: dict[str, Any] = Field(default_factory=dict)
    route_arbitration_contract: dict[str, Any] = Field(default_factory=dict)
    first_package_background_contract: dict[str, Any] = Field(default_factory=dict)
    run_projection_event_stream_contract: dict[str, Any] = Field(default_factory=dict)
    mcp_delivery_contract: dict[str, Any] = Field(default_factory=dict)
    run_projection_truth: dict[str, Any] = Field(default_factory=dict)
    model_profile: dict[str, Any] = Field(default_factory=dict)
    answer_demo: dict[str, Any] | None = None


class AgentDemoChatTurnRequest(BaseModel):
    brain_id: str | None = None
    message: str = Field(min_length=1)
    retrieval_mode: Literal["flash", "balanced", "heavy", "forensic"] = "balanced"
    document_text_policy: Literal["refs_only", "top_raw", "all_raw"] = "refs_only"
    auto_inspect_until_terminal: bool = True
    include_answer_demo: bool = False

    @field_validator("message")
    @classmethod
    def trim_agent_demo_message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message_required")
        return value

    @field_validator("brain_id")
    @classmethod
    def trim_optional_agent_demo_brain_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class AgentDemoAssistantMessage(BaseModel):
    kind: Literal["observed_fact", "repeated_pattern", "grounded_inference", "hypothesis", "unknown"] = "unknown"
    label: str
    text: str
    evidence: str | None = None


class AgentDemoToolEvent(BaseModel):
    id: str
    tool: str
    label: str
    state: Literal["pending", "running", "done", "blocked", "error"]
    detail: str
    endpoint: str | None = None
    search_id: str | None = None
    latency_ms: int | None = None


class AgentDemoChatTurnResponse(BaseModel):
    schema_version: str = "agvm.agent_demo.chat_turn.v1"
    turn_id: str
    brain_id: str | None = None
    status: Literal["terminal", "partial", "blocked", "error"]
    terminal_for_client: bool | None = None
    message: str
    assistant_messages: list[AgentDemoAssistantMessage] = Field(default_factory=list)
    tool_events: list[AgentDemoToolEvent] = Field(default_factory=list)
    mcp_response: dict[str, Any] = Field(default_factory=dict)
    policy: dict[str, Any] = Field(default_factory=dict)


class McpGrowSourceRequest(SourceInvestigationRequest):
    pass


class McpGrowApplyRequest(BaseModel):
    brain_id: str | None = None
    investigation_id: str | None = None
    source_investigation: dict[str, Any] = Field(default_factory=dict)
    source_formation_contract: dict[str, Any] = Field(default_factory=dict)
    preview_bundle: dict[str, Any] | None = None
    selected_preview_ids: list[str] = Field(default_factory=list)
    learning_mode: LearningMode = "strict_review"
    clarification_answers: dict[str, str] = Field(default_factory=dict)
    approved_preview_ids: list[str] = Field(default_factory=list)
    question_limit: int = Field(default=3, ge=1, le=8)
    confirm_apply: bool = False

    @field_validator("investigation_id")
    @classmethod
    def trim_optional_investigation_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class McpWriteMemoryPreviewRequest(PreviewRequest):
    pass


class McpWriteMemoryCommitRequest(BaseModel):
    brain_id: str | None = None
    bundle: dict[str, Any] | None = None
    text: str | None = None
    input_mode: Literal["auto", "document"] = "auto"
    source_label: str | None = None
    source_type: str | None = None
    source_trust: SourceTrust | None = None
    selected_preview_ids: list[str] = Field(default_factory=list)
    learning_mode: LearningMode = "strict_review"
    clarification_answers: dict[str, str] = Field(default_factory=dict)
    approved_preview_ids: list[str] = Field(default_factory=list)
    question_limit: int = Field(default=3, ge=1, le=8)
    confirm_apply: bool = False

    @field_validator("text", "source_label", "source_type")
    @classmethod
    def trim_optional_write_commit_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class McpClarificationRequest(BaseModel):
    brain_id: str | None = None
    raw_input: str | None = None
    text: str | None = None
    source_label: str | None = None
    source_uri: str | None = None
    user_instruction: str | None = None
    input_kind: Literal["auto", "manual_text", "url", "website", "pdf", "docx", "image", "transcript", "mixed_bundle"] = "auto"
    options: SourceInvestigationOptions = Field(default_factory=SourceInvestigationOptions)
    learning_mode: LearningMode = "guided_learning"
    question_limit: int = Field(default=5, ge=1, le=12)

    @field_validator("brain_id", "raw_input", "text", "source_label", "source_uri", "user_instruction")
    @classmethod
    def trim_optional_clarification_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class McpGrowToolExecutionResponse(BaseModel):
    schema_version: str
    brain_id: str | None = None
    tool_name: str
    status: Literal["preview_ready", "asking_clarification", "partial_budget_exhausted", "needs_review", "applied", "blocked", "failed"]
    source_investigation: dict[str, Any] = Field(default_factory=dict)
    source_formation_contract: dict[str, Any] = Field(default_factory=dict)
    memory_operation_lifecycle_contract: dict[str, Any] = Field(default_factory=dict)
    preview_bundle: dict[str, Any] | None = None
    clarification_request: dict[str, Any] = Field(default_factory=dict)
    clarification_questions: list[Any] = Field(default_factory=list)
    compiler_handoff_proof: dict[str, Any] = Field(default_factory=dict)
    persist_result: dict[str, Any] | None = None
    cognitive_write_plan: dict[str, Any] = Field(default_factory=dict)
    learning_policy: dict[str, Any] = Field(default_factory=dict)
    write_trace: dict[str, Any] = Field(default_factory=dict)
    completeness: dict[str, Any] = Field(default_factory=dict)
    mcp_latency_profile: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)
    error_contract: dict[str, Any] = Field(default_factory=dict)
    next_action: str | None = None


class McpMaintenanceRequest(BaseModel):
    brain_id: str | None = None
    mode: Literal["sleep", "evolve"] | None = None
    focus_node_id: str | None = None
    dry_run: bool = True
    max_nodes_considered: int = Field(default=20, ge=10, le=500)

    @field_validator("brain_id", "focus_node_id")
    @classmethod
    def trim_optional_mcp_maintenance_focus(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class McpMaintenanceApplyRequest(McpMaintenanceRequest):
    proposal_ids: list[str] = Field(default_factory=list)
    confirm_apply: bool = False


class McpMemoryOSListRequest(BaseModel):
    brain_id: str | None = None
    limit: int = Field(default=25, ge=1, le=100)


class McpBrainHealthRequest(BaseModel):
    brain_id: str | None = None
    limit: int = Field(default=25, ge=1, le=100)
    include_issue_samples: bool = True

    @field_validator("brain_id")
    @classmethod
    def trim_optional_mcp_brain_health_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class McpMatrixCalibrationRequest(BaseModel):
    brain_id: str | None = None
    max_nodes_considered: int = Field(default=4000, ge=50, le=4000)
    max_position_updates: int = Field(default=1600, ge=1, le=2000)
    include_recommendations: bool = True

    @field_validator("brain_id")
    @classmethod
    def trim_optional_mcp_matrix_calibration_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class McpMatrixCalibrationApplyRequest(McpMatrixCalibrationRequest):
    confirm_apply: bool = False
    rollback_consent: bool = False
    preview_signature: str | None = None

    @field_validator("preview_signature")
    @classmethod
    def trim_mcp_geometry_calibration_preview_signature(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("geometry_calibration_preview_signature_required")
        return value

    @model_validator(mode="after")
    def require_signature_for_explicit_brain(self) -> "McpMatrixCalibrationApplyRequest":
        # Missing scope is an MCP policy result. Once a brain is explicit, the
        # apply request must be bound to a nonblank preview at model validation.
        if self.brain_id and not self.preview_signature:
            raise ValueError("geometry_calibration_preview_signature_required")
        return self


class McpGeometryCalibrationRollbackRequest(BaseModel):
    brain_id: str = Field(min_length=1)
    plan_signature: str = Field(min_length=1)
    confirm_rollback: bool = False

    @field_validator("brain_id", "plan_signature")
    @classmethod
    def trim_geometry_calibration_rollback_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("geometry_calibration_rollback_value_required")
        return value


class McpBrainHealthToolExecutionResponse(BaseModel):
    schema_version: str
    brain_id: str | None = None
    tool_name: str
    status: Literal["ok", "partial", "blocked", "failed"]
    brain_health_report: dict[str, Any] = Field(default_factory=dict)
    recommendation: str = "none"
    reason_codes: list[str] = Field(default_factory=list)
    health_summary: dict[str, Any] = Field(default_factory=dict)
    checks: dict[str, Any] = Field(default_factory=dict)
    actions: list[dict[str, Any]] = Field(default_factory=list)
    retrieval_learning_rollup: dict[str, Any] = Field(default_factory=dict)
    brain_sanity_snapshot: dict[str, Any] = Field(default_factory=dict)
    health_alerts: list[dict[str, Any]] = Field(default_factory=list)
    alert_summary: dict[str, Any] = Field(default_factory=dict)
    evolution_recommendation: dict[str, Any] = Field(default_factory=dict)
    benchmark_preflight: dict[str, Any] = Field(default_factory=dict)
    validation_brain_rebuild_gate: dict[str, Any] = Field(default_factory=dict)
    automation_policy: dict[str, Any] = Field(default_factory=dict)
    safety_contract: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)


class McpMatrixCalibrationToolExecutionResponse(BaseModel):
    schema_version: str
    brain_id: str | None = None
    tool_name: str
    status: Literal["ok", "partial", "blocked", "failed", "applied"]
    maintenance_id: str | None = None
    brain_geometry_calibration: dict[str, Any] = Field(default_factory=dict)
    calibration_proposals: list[dict[str, Any]] = Field(default_factory=list)
    recommendations: list[dict[str, Any]] = Field(default_factory=list)
    matrix_change_policy: dict[str, Any] = Field(default_factory=dict)
    maintenance_truth_contract: dict[str, Any] = Field(default_factory=dict)
    memory_operation_lifecycle_contract: dict[str, Any] = Field(default_factory=dict)
    maintenance_transaction: dict[str, Any] = Field(default_factory=dict)
    matrix_delta: dict[str, Any] = Field(default_factory=dict)
    position_update_plan: dict[str, Any] = Field(default_factory=dict)
    projected_after: dict[str, Any] = Field(default_factory=dict)
    apply_policy_guard: dict[str, Any] = Field(default_factory=dict)
    rollback_snapshot: dict[str, Any] = Field(default_factory=dict)
    before_after_audit: dict[str, Any] = Field(default_factory=dict)
    mutation_surface: dict[str, Any] = Field(default_factory=dict)
    safety_contract: dict[str, Any] = Field(default_factory=dict)
    latency_profile: dict[str, Any] = Field(default_factory=dict)
    actions: list[dict[str, Any]] = Field(default_factory=list)
    budget: dict[str, Any] = Field(default_factory=dict)
    completeness: dict[str, Any] = Field(default_factory=dict)


class McpGeometryCalibrationRollbackResponse(BaseModel):
    schema_version: str = "agvm.geometry_calibration_rollback_response.v1"
    brain_id: str
    tool_name: str = "geometry_calibration_rollback"
    status: Literal["rolled_back", "already_rolled_back"]
    plan_signature: str
    rollback_result: dict[str, Any] = Field(default_factory=dict)
    mutation_surface: dict[str, Any] = Field(default_factory=dict)
    safety_contract: dict[str, Any] = Field(default_factory=dict)


# Matrix remains a compatibility alias; new contracts use Geometry Calibration copy.
McpGeometryCalibrationRequest = McpMatrixCalibrationRequest
McpGeometryCalibrationApplyRequest = McpMatrixCalibrationApplyRequest
McpMatrixCalibrationRollbackRequest = McpGeometryCalibrationRollbackRequest
McpMatrixCalibrationRollbackResponse = McpGeometryCalibrationRollbackResponse


class McpMaintenanceToolExecutionResponse(BaseModel):
    schema_version: str
    brain_id: str | None = None
    tool_name: str
    status: Literal["preview_ready", "applied", "blocked", "failed", "ok", "partial"]
    maintenance_report: dict[str, Any] = Field(default_factory=dict)
    maintenance_proposals: list[dict[str, Any]] = Field(default_factory=list)
    elastic_topology_proposals: list[dict[str, Any]] = Field(default_factory=list)
    maintenance_truth_contract: dict[str, Any] = Field(default_factory=dict)
    sleep_evolve_lifecycle_contract: dict[str, Any] = Field(default_factory=dict)
    maintenance_transaction: dict[str, Any] = Field(default_factory=dict)
    preview_budget_guard: dict[str, Any] = Field(default_factory=dict)
    maintenance_preview_plan: dict[str, Any] = Field(default_factory=dict)
    memory_operation_lifecycle_contract: dict[str, Any] = Field(default_factory=dict)
    proposal_review_table: list[dict[str, Any]] = Field(default_factory=list)
    metamemory_snapshot: dict[str, Any] = Field(default_factory=dict)
    apply_policy_guard: dict[str, Any] = Field(default_factory=dict)
    rollback_snapshot: dict[str, Any] = Field(default_factory=dict)
    before_after_audit: dict[str, Any] = Field(default_factory=dict)
    no_corruption_guards: dict[str, Any] = Field(default_factory=dict)
    mutation_surface: dict[str, Any] = Field(default_factory=dict)
    maintenance_latency_profile: dict[str, Any] = Field(default_factory=dict)
    rollback_plan: dict[str, Any] = Field(default_factory=dict)
    matrix_delta: dict[str, Any] = Field(default_factory=dict)
    process: dict[str, Any] = Field(default_factory=dict)
    open_questions: list[dict[str, Any]] = Field(default_factory=list)
    hypotheses: list[dict[str, Any]] = Field(default_factory=list)
    contradictions: list[dict[str, Any]] = Field(default_factory=list)
    processes: list[dict[str, Any]] = Field(default_factory=list)
    source_trace: list[dict[str, Any]] = Field(default_factory=list)
    completeness: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)


class PersistSelectionRequest(BaseModel):
    brain_id: str | None = None
    bundle: PreviewBundle
    selected_preview_ids: list[str] = Field(default_factory=list)
    learning_mode: LearningMode = "strict_review"
    clarification_answers: dict[str, str] = Field(default_factory=dict)
    approved_preview_ids: list[str] = Field(default_factory=list)
    question_limit: int = Field(default=3, ge=1, le=8)


class PersistSelectionResponse(BaseModel):
    graph: Graph
    persisted_node_ids: list[str]
    persisted_edge_count: int
    merged_into_existing_ids: list[str] = Field(default_factory=list)
    learning_policy: dict[str, Any] = Field(default_factory=dict)
    write_trace: WriteTrace


class AnalyzeRequest(BaseModel):
    brain_id: str | None = None
    raw_text: str = Field(min_length=1)
    source_label: str | None = None
    node_kind_hint: str | None = None
    source_type: str | None = None
    source_trust: SourceTrust | None = None
    treat_as_document: bool = False
    simulate_as_migrated_tree_node: bool = False

    @field_validator("raw_text")
    @classmethod
    def trim_raw_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("raw_text_required")
        return value


class AnalyzeResponse(BaseModel):
    node: VectorNode
    graph: Graph
    debug: DebugPayload


class QueryProbe(BaseModel):
    probe_id: str
    label: str
    query_text: str
    strand_id: str | None = None
    merge_outcome: Literal["reuse_branch", "enrich_branch", "fork_new_branch"] | None = None
    dual_origin: bool = False
    origin_families: list[str] = Field(default_factory=list)
    source_probe_ids_by_family: dict[str, str] = Field(default_factory=dict)
    semantic_destination_queue: list[dict[str, Any]] = Field(default_factory=list)
    origin_destination_queues: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    ai_refinement_delta: dict[str, Any] = Field(default_factory=dict)
    merge_score: float | None = Field(default=None, ge=0.0, le=1.0)
    merge_reasons: list[str] = Field(default_factory=list)
    planner_family: Literal["heuristic", "ai"] | None = None
    family_plan_id: str | None = None
    family_plan_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    family_branch_id: str | None = None
    goal: str | None = None
    query_class: str | None = None
    answer_hypothesis: str | None = None
    weight: float
    intent_type: str | None = None
    expected_guide_area: str | None = None
    expected_memory_type: str | None = None
    landing_basis: str | None = None
    inverse_rationale: str | None = None
    radial_expectation: str | None = None
    routing_semantic_scores: SemanticScores
    routing_facets: Facets
    routing_brainhex: BrainHex
    base_position: Position
    landing_position: Position
    semantic_color: DisplayColor
    search_radius: float | None = None
    success_min_confidence: float | None = None
    max_text_chars: int | None = None
    target_bucket_keys: list[str] = Field(default_factory=list)
    crowding_penalty: float | None = None
    expected_answer_field: str | None = None
    corroboration_needs: list[str] = Field(default_factory=list)
    branch_priority: float | None = Field(default=None, ge=0.0, le=1.0)
    expected_stop_condition: str | None = None
    destination_queue: list[dict[str, Any]] = Field(default_factory=list)


class RetrieveMatch(BaseModel):
    node_id: str
    summary: str
    score: float
    raw_score: float
    probe_id: str
    label: str | None = None
    reason: str
    sources: list[str] = Field(default_factory=list)
    evidence_snippet: str | None = None
    node: VectorNode
    document_hit: dict[str, Any] | None = None


class DocumentPacket(BaseModel):
    anchor_node_id: str
    source_label: str | None = None
    source_type: str | None = None
    source_trust: SourceTrust = "user_asserted"
    claim_status: ClaimStatus = "fact"
    answer_eligible: bool = True
    profile_eligible: bool = True
    document_eligible: bool = True
    title: str
    query_fit_score: float = Field(default=0.0, ge=0.0, le=1.0)
    project_tags: list[str] = Field(default_factory=list)
    entity_tags: list[str] = Field(default_factory=list)
    timeline_tags: list[str] = Field(default_factory=list)
    topic_tags: list[str] = Field(default_factory=list)
    related_node_ids: list[str] = Field(default_factory=list)
    chunks_count: int = Field(default=0, ge=0)
    facts_count: int = Field(default=0, ge=0)
    summary_count: int = Field(default=0, ge=0)
    raw_text_available: bool = False
    catalog_index: dict[str, Any] = Field(default_factory=dict)
    summary_node_ids: list[str] = Field(default_factory=list)
    chunk_node_ids: list[str] = Field(default_factory=list)
    fact_node_ids: list[str] = Field(default_factory=list)
    top_chunk_matches: list[RetrieveMatch] = Field(default_factory=list)
    top_fact_matches: list[RetrieveMatch] = Field(default_factory=list)
    provenance_summary: str = ""
    relevance_summary: str = ""
    coverage: dict[str, Any] = Field(default_factory=dict)
    open_questions: list[str] = Field(default_factory=list)
    full_text_mode: Literal["anchor_raw", "ordered_chunk_sequence", "mixed_evidence", "none"] = "none"
    complete_text_available: bool = False
    raw_text_char_count: int = Field(default=0, ge=0)
    anchor_raw_text: str | None = None
    full_text: str | None = None
    exact_match_score: float = Field(default=0.0, ge=0.0, le=1.0)
    lookup_role: DocumentLookupKind = "none"
    source_trace: list[dict[str, Any]] = Field(default_factory=list)
    ordered_chunk_sequence: list[dict[str, Any]] = Field(default_factory=list)
    supported_fact_text: list[dict[str, Any]] = Field(default_factory=list)


class EvidenceReservoirEntry(BaseModel):
    entry_id: str
    node_id: str
    summary: str = ""
    raw_text: str = ""
    evidence_snippet: str = ""
    memory_type: str = ""
    topic: str | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)
    branch_ids: list[str] = Field(default_factory=list)
    branch_goals: list[str] = Field(default_factory=list)
    planner_families: list[Literal["heuristic", "ai"]] = Field(default_factory=list)
    support_slots: list[str] = Field(default_factory=list)
    contradiction_flags: list[str] = Field(default_factory=list)
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    score_source: str | None = None
    document_role: Literal["anchor", "summary", "chunk", "fact"] | None = None
    document_anchor_id: str | None = None
    document_chunk_index: int | None = None
    source_label: str | None = None
    source_type: str | None = None
    source_span_start: int | None = None
    source_span_end: int | None = None
    raw_text_preserved: bool = False
    source_trust: SourceTrust = "user_asserted"
    claim_status: ClaimStatus = "fact"
    answer_eligible: bool = True
    profile_eligible: bool = True
    document_eligible: bool = True


class EvidenceReservoirDocument(BaseModel):
    anchor_node_id: str
    source_label: str | None = None
    source_type: str | None = None
    source_trust: SourceTrust = "user_asserted"
    claim_status: ClaimStatus = "fact"
    answer_eligible: bool = True
    profile_eligible: bool = True
    document_eligible: bool = True
    title: str
    query_fit_score: float = Field(default=0.0, ge=0.0, le=1.0)
    project_tags: list[str] = Field(default_factory=list)
    entity_tags: list[str] = Field(default_factory=list)
    timeline_tags: list[str] = Field(default_factory=list)
    topic_tags: list[str] = Field(default_factory=list)
    related_node_ids: list[str] = Field(default_factory=list)
    chunks_count: int = Field(default=0, ge=0)
    facts_count: int = Field(default=0, ge=0)
    summary_count: int = Field(default=0, ge=0)
    raw_text_available: bool = False
    catalog_index: dict[str, Any] = Field(default_factory=dict)
    full_text_mode: Literal["anchor_raw", "ordered_chunk_sequence", "mixed_evidence", "none"] = "none"
    complete_text_available: bool = False
    raw_text_char_count: int = Field(default=0, ge=0)
    anchor_raw_text: str | None = None
    full_text: str | None = None
    exact_match_score: float = Field(default=0.0, ge=0.0, le=1.0)
    lookup_role: DocumentLookupKind = "none"
    source_trace: list[dict[str, Any]] = Field(default_factory=list)
    ordered_chunk_sequence: list[dict[str, Any]] = Field(default_factory=list)
    supported_fact_text: list[dict[str, Any]] = Field(default_factory=list)
    provenance_summary: str = ""
    relevance_summary: str = ""
    open_questions: list[str] = Field(default_factory=list)


class EvidenceReservoir(BaseModel):
    entries: list[EvidenceReservoirEntry] = Field(default_factory=list)
    documents: list[EvidenceReservoirDocument] = Field(default_factory=list)
    reservoir_summary: dict[str, Any] = Field(default_factory=dict)
    quality_metrics: dict[str, Any] = Field(default_factory=dict)
    prompt_pack_summary: dict[str, Any] = Field(default_factory=dict)


class RetrieveAnswer(BaseModel):
    answer_text: str
    mode: Literal[
        "llm",
        "heuristic",
        "insufficient",
        "partial_known_insufficient",
        "grounded_facts",
        "document_packet",
        "document_lookup_guard",
        "human_synthesizer",
        "contract_human_synthesis",
    ]
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_node_ids: list[str] = Field(default_factory=list)
    reasoning_summary: str
    insufficient: bool = False
    answerability_state: Literal["grounded", "partial", "insufficient", "ai_pending"] = "grounded"
    evidence_snippets: list[dict[str, Any]] = Field(default_factory=list)
    support_node_count: int = Field(default=0, ge=0)
    support_slot_count: int = Field(default=0, ge=0)
    family_attribution_summary: dict[str, Any] = Field(default_factory=dict)
    contradiction_present: bool = False
    answer_adequacy: dict[str, Any] = Field(default_factory=dict)
    partial_known: bool = False
    unknown_not_in_memory: bool = False
    known_sections: list[str] = Field(default_factory=list)
    missing_required_sections: list[str] = Field(default_factory=list)
    document_lookup_state: str | None = None
    document_lookup_kind: DocumentLookupKind = "none"
    document_lookup: dict[str, Any] = Field(default_factory=dict)
    supporting_documents: list[dict[str, Any]] = Field(default_factory=list)
    source_trace: list[dict[str, Any]] = Field(default_factory=list)


class RetrieveStep(BaseModel):
    probe_id: str
    label: str
    step_number: int
    bucket_key: str | None = None
    target_bucket_keys: list[str] = Field(default_factory=list)
    candidate_ids: list[str] = Field(default_factory=list)
    visited_node_ids: list[str] = Field(default_factory=list)
    visited_bucket_keys: list[str] = Field(default_factory=list)
    followed_highway_targets: list[str] = Field(default_factory=list)
    matches: list[RetrieveMatch] = Field(default_factory=list)
    route_decision: dict[str, Any] = Field(default_factory=dict)
    route_trace: list[dict[str, Any]] = Field(default_factory=list)
    active_destination: dict[str, Any] | None = None
    route_state: str | None = None
    satisfaction_score: float = 0.0
    stop_reason: str | None = None
    elapsed_ms: float | None = None


class ContextFragment(BaseModel):
    topic: str
    text: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_node_ids: list[str] = Field(default_factory=list)


class RetrieveContext(BaseModel):
    context_summary: str
    context_fragments: list[ContextFragment] = Field(default_factory=list)
    structured_sections: list[dict[str, Any]] = Field(default_factory=list)
    traits: list[str] = Field(default_factory=list)
    style_cues: list[str] = Field(default_factory=list)
    communication_cues: list[str] = Field(default_factory=list)
    values_cues: list[str] = Field(default_factory=list)
    biographical_cues: list[str] = Field(default_factory=list)
    movement_cues: list[str] = Field(default_factory=list)
    story_points: list[str] = Field(default_factory=list)
    open_uncertainties: list[str] = Field(default_factory=list)
    evidence_node_ids: list[str] = Field(default_factory=list)
    evidence_reservoir_summary: dict[str, Any] = Field(default_factory=dict)
    context_quality_metrics: dict[str, Any] = Field(default_factory=dict)


class BranchBudget(BaseModel):
    max_steps: int = Field(default=2, ge=1, le=8)
    max_candidate_reads: int = Field(default=10, ge=1, le=64)
    max_nearby_bundles: int = Field(default=2, ge=1, le=8)
    max_fulltexts: int = Field(default=3, ge=0, le=24)
    max_text_chars: int = Field(default=3200, ge=200, le=32000)
    max_highway_hops: int = Field(default=1, ge=0, le=8)
    max_pattern_hops: int = Field(default=0, ge=0, le=8)


class BranchDestination(BaseModel):
    destination_id: str
    destination_key: str | None = None
    label: str
    guide_area: str | None = None
    memory_type: str | None = None
    radial_expectation: str | None = None
    semantic_color_hint: str | None = None
    target_bucket_keys: list[str] = Field(default_factory=list)
    target_node_ids: list[str] = Field(default_factory=list)
    rationale: str | None = None
    priority: float = Field(default=0.5, ge=0.0, le=1.0)


class AnswerStrand(BaseModel):
    strand_id: str
    answer_field: str
    answer_hypothesis: str
    goal: str
    landing_hint: str
    destination_queue: list[BranchDestination] = Field(default_factory=list)
    priority: float = Field(default=0.5, ge=0.0, le=1.0)
    planner_family: Literal["heuristic", "ai"] | None = None
    seed_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    inverse_rationale: str | None = None
    expected_guide_area: str | None = None
    expected_memory_type: str | None = None
    radial_expectation: str | None = None


class BranchControllerRecommendation(BaseModel):
    action: Literal[
        "continue_current_route",
        "switch_destination",
        "hold_branch",
        "stop_branch",
        "request_radius_widen",
        "request_doc_hydration",
        "escalate_to_master",
    ]
    reason: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    target_destination_id: str | None = None
    requested_actions: list[str] = Field(default_factory=list)
    decision_source: Literal["llm", "fallback_timeout", "fallback_parse_error", "fallback_provider_error", "fallback_disabled"] | None = None
    fallback_reason: Literal["timeout", "provider_error", "parse_error", "disabled", "empty"] | None = None
    controller_kind: str | None = None
    turn_ms: float | None = Field(default=None, ge=0.0)
    evidence_basis: list[str] = Field(default_factory=list)
    hold_reason: str | None = None
    override_applied: bool = False
    fallback_preview_action: str | None = None
    escalation_needed: bool = False
    escalation_reason: str | None = None
    requested_master_action: Literal["continue", "reroute", "hold", "stop", "request_doc_hydration", "request_radius_widen"] | None = None
    blocking_destination_id: str | None = None
    blocking_state: str | None = None


class RouteDecision(BaseModel):
    source_node_id: str | None = None
    target_node_id: str | None = None
    destination_id: str | None = None
    destination_key: str | None = None
    destination_label: str | None = None
    edge_type: Literal["highway", "link", "local", "none"] = "none"
    move_type: Literal[
        "travel",
        "study",
        "hydrate",
        "destination_reached",
        "route_exhausted",
        "reorder",
    ] = "study"
    navigation_action: str | None = None
    candidate_source: str | None = None
    travel_performed: bool = False
    route_score: float = Field(default=0.0, ge=0.0, le=1.0)
    route_reason: str | None = None
    route_yield: float = Field(default=0.0, ge=0.0, le=1.0)
    destination_reached: bool = False
    lease_conflict: bool = False
    region_pressure: float = Field(default=0.0, ge=0.0)
    yielded_match_count: int = Field(default=0, ge=0)
    family_attribution: Literal["heuristic", "ai", "dual-origin"] | None = None
    considered_highway: bool = False
    highway_candidate_count: int = Field(default=0, ge=0)
    highway_not_used_reason: str | None = None
    best_highway_target_node_id: str | None = None
    best_highway_route_score: float | None = Field(default=None, ge=0.0, le=1.0)
    destination_relevance: float | None = Field(default=None, ge=0.0, le=1.0)
    destination_progress_gain: float | None = Field(default=None, ge=0.0, le=1.0)
    corroboration_yield: float | None = Field(default=None, ge=0.0, le=1.0)
    topology_efficiency: float | None = Field(default=None, ge=0.0, le=1.0)
    highway_usefulness_memory: float | None = Field(default=None, ge=0.0, le=1.0)
    from_node_id: str | None = None
    to_node_id: str | None = None
    from_bucket_key: str | None = None
    to_bucket_key: str | None = None
    destination_before: dict[str, Any] = Field(default_factory=dict)
    destination_after: dict[str, Any] = Field(default_factory=dict)
    semantic_order_index: int | None = None
    execution_order_index: int | None = None
    reorder_reason: str | None = None
    yielded_match_ids: list[str] = Field(default_factory=list)
    studied_node_ids: list[str] = Field(default_factory=list)
    hydrated_node_ids: list[str] = Field(default_factory=list)


class RetrieveBranch(BaseModel):
    branch_id: str
    strand_id: str | None = None
    merge_outcome: Literal["reuse_branch", "enrich_branch", "fork_new_branch"] | None = None
    dual_origin: bool = False
    origin_families: list[str] = Field(default_factory=list)
    source_probe_ids_by_family: dict[str, str] = Field(default_factory=dict)
    semantic_destination_queue: list[BranchDestination] = Field(default_factory=list)
    origin_destination_queues: dict[str, list[BranchDestination]] = Field(default_factory=dict)
    ai_refinement_delta: dict[str, Any] = Field(default_factory=dict)
    merge_score: float | None = Field(default=None, ge=0.0, le=1.0)
    merge_reasons: list[str] = Field(default_factory=list)
    merge_partner_branch_id: str | None = None
    merge_resolution_reason: str | None = None
    family_branch_id: str | None = None
    planner_family: Literal["heuristic", "ai"] | None = None
    family_plan_id: str | None = None
    family_plan_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    probe_ids: list[str] = Field(default_factory=list)
    goal: str
    status: Literal["active", "satisfied", "merged", "stopped"] = "active"
    worker_id: str | None = None
    worker_kind: str | None = None
    success_criteria: dict[str, Any] = Field(default_factory=dict)
    budget: BranchBudget = Field(default_factory=BranchBudget)
    search_radius: float = Field(default=0.28, ge=0.05, le=2.0)
    visited_node_ids: list[str] = Field(default_factory=list)
    visited_bucket_keys: list[str] = Field(default_factory=list)
    evidence_node_ids: list[str] = Field(default_factory=list)
    stop_reason: str | None = None
    answer_hypothesis: str | None = None
    expected_answer_field: str | None = None
    corroboration_needs: list[str] = Field(default_factory=list)
    branch_priority: float = Field(default=0.5, ge=0.0, le=1.0)
    expected_stop_condition: str | None = None
    start_region: dict[str, Any] = Field(default_factory=dict)
    current_region: dict[str, Any] = Field(default_factory=dict)
    current_node_id: str | None = None
    destination_queue: list[BranchDestination] = Field(default_factory=list)
    family_destination_queue: list[BranchDestination] = Field(default_factory=list)
    execution_destination_queue: list[BranchDestination] = Field(default_factory=list)
    destination_execution_index: int = Field(default=0, ge=0)
    destination_reorder_reason: str | None = None
    destination_reorder_history: list[dict[str, Any]] = Field(default_factory=list)
    destination_resolution: dict[str, Any] = Field(default_factory=dict)
    active_destination: BranchDestination | None = None
    route_state: Literal[
        "planned",
        "landing",
        "routing",
        "evidence_holding",
        "reroute_pending",
        "goal_satisfied",
        "route_exhausted",
        "stopped",
        "merged",
        "superseded",
    ] = "planned"
    route_yield: float = Field(default=0.0, ge=0.0, le=1.0)
    visited_edge_refs: list[dict[str, Any]] = Field(default_factory=list)
    candidate_node_ids: list[str] = Field(default_factory=list)
    studied_node_ids: list[str] = Field(default_factory=list)
    hydrated_node_ids: list[str] = Field(default_factory=list)
    traversed_nodes: list[str] = Field(default_factory=list)
    traversed_edges: list[dict[str, Any]] = Field(default_factory=list)
    route_trace: list[RouteDecision] = Field(default_factory=list)
    move_types: list[str] = Field(default_factory=list)
    destination_progress: dict[str, Any] = Field(default_factory=dict)
    highway_hops_taken: int = Field(default=0, ge=0)
    link_hops_taken: int = Field(default=0, ge=0)
    local_hops_taken: int = Field(default=0, ge=0)
    highway_traversed_count: int = Field(default=0, ge=0)
    link_traversed_count: int = Field(default=0, ge=0)
    local_traversed_count: int = Field(default=0, ge=0)
    considered_highway_count: int = Field(default=0, ge=0)
    studied_node_count: int = Field(default=0, ge=0)
    hydrated_node_count: int = Field(default=0, ge=0)
    route_richness_score: float = Field(default=0.0, ge=0.0, le=1.0)
    covered_slots: list[str] = Field(default_factory=list)
    local_stop_recommendation: str | None = None
    controller_kind: str | None = None
    controller_recommendation: BranchControllerRecommendation | None = None
    controller_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    controller_reason: str | None = None
    controller_turn_ms: float | None = Field(default=None, ge=0.0)
    controller_evidence_basis: list[str] = Field(default_factory=list)
    controller_decision_source: Literal["llm", "fallback_timeout", "fallback_parse_error", "fallback_provider_error", "fallback_disabled"] | None = None
    controller_fallback_reason: Literal["timeout", "provider_error", "parse_error", "disabled", "empty"] | None = None
    controller_escalation_needed: bool = False
    controller_escalation_reason: str | None = None
    controller_requested_master_action: Literal["continue", "reroute", "hold", "stop", "request_doc_hydration", "request_radius_widen"] | None = None
    controller_blocking_destination_id: str | None = None
    controller_blocking_state: str | None = None
    family_contribution_summary: dict[str, Any] = Field(default_factory=dict)
    lifecycle_stage: Literal[
        "planned",
        "landed",
        "landing",
        "routing",
        "evidence_holding",
        "reroute_pending",
        "satisfied",
        "stopped",
        "merged",
        "superseded",
    ] = "planned"
    last_round_index: int = Field(default=0, ge=0)


class SharedEvidenceTopic(BaseModel):
    topic: str
    node_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class SharedEvidence(BaseModel):
    covered_goals: list[str] = Field(default_factory=list)
    fulfilled_goals: list[str] = Field(default_factory=list)
    branch_status: dict[str, str] = Field(default_factory=dict)
    evidence_topics: list[SharedEvidenceTopic] = Field(default_factory=list)
    shared_node_ids: list[str] = Field(default_factory=list)
    shared_bucket_keys: list[str] = Field(default_factory=list)
    confidence_by_goal: dict[str, float] = Field(default_factory=dict)
    branch_overlaps: list[dict[str, Any]] = Field(default_factory=list)
    contradiction_flags: list[str] = Field(default_factory=list)
    facts_by_slot: dict[str, list[str]] = Field(default_factory=dict)
    coverage_by_slot: dict[str, float] = Field(default_factory=dict)
    unresolved_slots: list[str] = Field(default_factory=list)
    worker_reports: list[dict[str, Any]] = Field(default_factory=list)
    master_state: dict[str, Any] = Field(default_factory=dict)
    worker_registry: dict[str, Any] = Field(default_factory=dict)
    follow_up_candidates: list[dict[str, Any]] = Field(default_factory=list)
    retrieval_mode: str | None = None
    family_overlap: dict[str, Any] = Field(default_factory=dict)
    family_divergence: dict[str, Any] = Field(default_factory=dict)
    family_yield: dict[str, Any] = Field(default_factory=dict)
    family_conflicts: list[dict[str, Any]] = Field(default_factory=list)
    family_contribution_summary: dict[str, Any] = Field(default_factory=dict)
    answer_strands: list[AnswerStrand] = Field(default_factory=list)
    planner_seed_runtime: dict[str, Any] = Field(default_factory=dict)
    seed_goal_coverage: dict[str, Any] = Field(default_factory=dict)
    seed_destination_presence: dict[str, Any] = Field(default_factory=dict)
    blackboard: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence_reservoir_summary: dict[str, Any] = Field(default_factory=dict)
    context_quality_metrics: dict[str, Any] = Field(default_factory=dict)


class RetrieveRequest(BaseModel):
    brain_id: str | None = None
    query_text: str = Field(min_length=1)
    thread_id: str | None = None
    mcp_tool_name: str | None = None
    response_mode: Literal["answer", "context", "both"] = "both"
    retrieval_mode: Literal["flash", "balanced", "heavy", "forensic"] | None = None
    context_package_mode: Literal["answer_minimal", "mcp_operational", "broad_dossier", "document_full", "forensic_trace"] | None = None
    document_text_policy: Literal["refs_only", "top_raw", "all_raw"] = "refs_only"
    document_id: str | None = None
    complete_paths: bool = False
    max_probe_count: int = Field(default=6, ge=1, le=6)
    max_steps: int = Field(default=4, ge=1, le=8)
    max_candidates_per_step: int = Field(default=24, ge=4, le=64)
    max_matches: int = Field(default=12, ge=1, le=24)
    max_total_branches: int = Field(default=6, ge=1, le=6)
    max_total_steps: int = Field(default=4, ge=1, le=12)
    max_total_text_chars: int = Field(default=6400, ge=500, le=64000)
    max_nodes_fulltext: int = Field(default=6, ge=1, le=24)
    allow_highway_expansion: bool = True
    allow_document_anchor_expansion: bool = True
    allow_adjacent_bucket_expansion: bool = True
    allow_pattern_expansion: bool = True

    @field_validator("query_text")
    @classmethod
    def trim_query_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query_text_required")
        return value

    @field_validator("document_id", "mcp_tool_name")
    @classmethod
    def trim_optional_document_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class RetrieveResponse(BaseModel):
    search_id: str | None = None
    brain_id: str | None = None
    query_text: str
    thread_id: str | None = None
    response_mode: Literal["answer", "context", "both"] = "both"
    retrieval_mode: Literal["flash", "balanced", "heavy", "forensic"] = "balanced"
    decomposition_mode: Literal["llm", "heuristic", "hybrid"] | None = None
    planner_mode: Literal["llm", "heuristic", "hybrid"] | None = None
    document_mode: Literal["none", "lookup", "synthesis"] = "none"
    document_lookup_kind: DocumentLookupKind = "none"
    semantic_contract: dict[str, Any] = Field(default_factory=dict)
    semantic_contract_runtime: dict[str, Any] = Field(default_factory=dict)
    metamemory_snapshot: dict[str, Any] = Field(default_factory=dict)
    metamemory_spatial_brief: dict[str, Any] = Field(default_factory=dict)
    metamemory_spatial_brief_summary: dict[str, Any] = Field(default_factory=dict)
    metamemory_spatial_readiness: dict[str, Any] = Field(default_factory=dict)
    ai_spatial_landing_contract: dict[str, Any] = Field(default_factory=dict)
    ai_spatial_landing_contract_runtime: dict[str, Any] = Field(default_factory=dict)
    path_mission_contract: dict[str, Any] = Field(default_factory=dict)
    path_missions: list[dict[str, Any]] = Field(default_factory=list)
    mission_aware_merge_summary: dict[str, Any] = Field(default_factory=dict)
    mission_evidence_ledger: dict[str, Any] = Field(default_factory=dict)
    master_judgement: dict[str, Any] = Field(default_factory=dict)
    mission_learning_rollup: dict[str, Any] = Field(default_factory=dict)
    document_lookup: dict[str, Any] = Field(default_factory=dict)
    supporting_documents: list[dict[str, Any]] = Field(default_factory=list)
    source_trace: list[dict[str, Any]] = Field(default_factory=list)
    document_workspace: dict[str, Any] = Field(default_factory=dict)
    document_text_policy: Literal["refs_only", "top_raw", "all_raw"] = "refs_only"
    document_refs: list[dict[str, Any]] = Field(default_factory=list)
    document_ref_contract: dict[str, Any] = Field(default_factory=dict)
    document_delivery_contract: dict[str, Any] = Field(default_factory=dict)
    document_bundle: dict[str, Any] = Field(default_factory=dict)
    document_packets: list[DocumentPacket] = Field(default_factory=list)
    evidence_reservoir: EvidenceReservoir | None = None
    answer_strands: list[AnswerStrand] = Field(default_factory=list)
    planner_seed_runtime: dict[str, Any] = Field(default_factory=dict)
    seed_goal_coverage: dict[str, Any] = Field(default_factory=dict)
    seed_destination_presence: dict[str, Any] = Field(default_factory=dict)
    probes: list[QueryProbe]
    branches: list[RetrieveBranch] = Field(default_factory=list)
    landing_metadata: list[dict[str, Any]] = Field(default_factory=list)
    shared_evidence: SharedEvidence | None = None
    steps: list[RetrieveStep]
    matches: list[RetrieveMatch]
    visited_node_ids: list[str]
    visited_bucket_keys: list[str]
    stop_reason: str
    answer: RetrieveAnswer | None = None
    context: RetrieveContext | None = None
    answer_short: str | None = None
    answer_full: str | None = None
    context_structured: dict[str, Any] = Field(default_factory=dict)
    context_package: dict[str, Any] = Field(default_factory=dict)
    context_package_materialization: dict[str, Any] = Field(default_factory=dict)
    query_metacognitive_review: dict[str, Any] = Field(default_factory=dict)
    payload_integrity: dict[str, Any] = Field(default_factory=dict)
    payload_truth_contract: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)
    latency_contract: dict[str, Any] = Field(default_factory=dict)
    completion_contract: dict[str, Any] = Field(default_factory=dict)
    run_lifecycle_contract: dict[str, Any] = Field(default_factory=dict)
    runtime_state_contract: dict[str, Any] = Field(default_factory=dict)
    tool_boundary_contract: dict[str, Any] = Field(default_factory=dict)
    ai_materialization_resilience_contract: dict[str, Any] = Field(default_factory=dict)
    ai_critical_path_contract: dict[str, Any] = Field(default_factory=dict)
    route_arbitration_contract: dict[str, Any] = Field(default_factory=dict)
    first_package_background_contract: dict[str, Any] = Field(default_factory=dict)
    run_projection_event_stream_contract: dict[str, Any] = Field(default_factory=dict)
    mcp_delivery_contract: dict[str, Any] = Field(default_factory=dict)
    run_projection_truth: dict[str, Any] = Field(default_factory=dict)
    hot_working_memory: dict[str, Any] = Field(default_factory=dict)
    hot_working_memory_contract: dict[str, Any] = Field(default_factory=dict)
    answer_demo_materialization: dict[str, Any] = Field(default_factory=dict)
    ai_landing_materialization: dict[str, Any] = Field(default_factory=dict)
    ai_materialization_hard_gate: dict[str, Any] = Field(default_factory=dict)
    mcp_background_cap: dict[str, Any] = Field(default_factory=dict)
    path_corridors: dict[str, Any] = Field(default_factory=dict)
    early_final_surface: dict[str, Any] | None = None
    early_final_sealed: bool = False
    early_final_seal_reason: str | None = None
    early_final_seal_source: str | None = None
    background_enrichment_after_early_final: bool = False
    background_enrichment_state: str | None = None
    background_enrichment_budget: dict[str, Any] = Field(default_factory=dict)
    background_enrichment_stop_reason: str | None = None
    background_enrichment_budget_exhausted: bool = False
    background_enrichment_rounds_consumed: int = Field(default=0, ge=0)
    background_enrichment_yield_policy: dict[str, Any] = Field(default_factory=dict)
    background_enrichment_yield_reports: list[dict[str, Any]] = Field(default_factory=list)
    background_enrichment_yield_summary: dict[str, Any] = Field(default_factory=dict)
    background_enrichment_low_yield_rounds: int = Field(default=0, ge=0)
    detached_result_snapshot: bool = False
    result_snapshot_kind: str | None = None
    result_materialization_state: Literal[
        "none",
        "snapshot_ready",
        "materializing",
        "first_package_ready_background_running",
        "first_package_finalized_background_capped",
        "first_partial_package_background_running",
        "first_document_payload_ready_background_running",
        "document_payload_ready",
        "path_payload_ready",
        "partial_complete_low_yield",
        "bounded_partial_finalized",
        "finalized",
    ] | None = None
    final_materialization_pending: bool = False
    result_ready_terminal: bool = True
    result_surface_ready_ms: float | None = None
    final_materialization_started_ms: float | None = None
    final_materialization_completed_ms: float | None = None
    final_materialization_stage_timings: list[dict[str, Any]] = Field(default_factory=list)
    runtime_stage_timing: dict[str, Any] = Field(default_factory=dict)
    context_dossier: str | None = None
    hot_context_summary: dict[str, Any] = Field(default_factory=dict)
    context_dossier_partial: str | None = None
    evidence_snippets: list[dict[str, Any]] = Field(default_factory=list)
    context_waves: list[dict[str, Any]] = Field(default_factory=list)
    master_state: dict[str, Any] = Field(default_factory=dict)
    answer_surface_state: Literal["not_ready", "answer_now", "context_level_1_ready", "answer_now_and_continue", "final_sealed"] | None = None
    closure_state: Literal["open", "exploration_complete", "bounded_partial", "final_sealed"] | None = None
    final_closure_ready: bool = False
    final_closure_blockers: list[dict[str, Any]] = Field(default_factory=list)
    unresolved_destination_count: int = Field(default=0, ge=0)
    answer_now_before_exploration_complete: bool = False
    final_closure_after_destination_resolution: bool = False
    context_level_1_before_final: bool = False
    planner_runtime: dict[str, Any] = Field(default_factory=dict)
    timing: dict[str, Any] = Field(default_factory=dict)
    follow_up_candidates: list[dict[str, Any]] = Field(default_factory=list)
    answerability_state: Literal["grounded", "partial", "insufficient", "ai_pending"] | None = None
    warm_state_saved: bool = False
    continuity_summary: dict[str, Any] = Field(default_factory=dict)
    warm_followup_economy: dict[str, Any] = Field(default_factory=dict)
    warm_context_carryover: dict[str, Any] = Field(default_factory=dict)
    context_quality_metrics: dict[str, Any] = Field(default_factory=dict)
    ai_material_contribution: bool = False
    ai_contribution_reason: str | None = None
    ai_materiality: dict[str, Any] = Field(default_factory=dict)
    route_truth_summary: dict[str, Any] = Field(default_factory=dict)
    search_map_2d_truth: dict[str, Any] = Field(default_factory=dict)
    run_projection_truth: dict[str, Any] = Field(default_factory=dict)
    map_stream_state: dict[str, Any] = Field(default_factory=dict)


class SearchPlanResponse(BaseModel):
    search_id: str
    brain_id: str | None = None
    thread_id: str | None = None
    query_text: str
    response_mode: Literal["answer", "context", "both"] = "both"
    retrieval_mode: Literal["flash", "balanced", "heavy", "forensic"] = "balanced"
    decomposition_mode: Literal["llm", "heuristic", "hybrid"] | None = None
    planner_mode: Literal["llm", "heuristic", "hybrid", "hybrid_ai_spatial"] | None = None
    semantic_contract: dict[str, Any] = Field(default_factory=dict)
    semantic_contract_runtime: dict[str, Any] = Field(default_factory=dict)
    metamemory_snapshot: dict[str, Any] = Field(default_factory=dict)
    metamemory_spatial_brief: dict[str, Any] = Field(default_factory=dict)
    metamemory_spatial_brief_summary: dict[str, Any] = Field(default_factory=dict)
    metamemory_spatial_readiness: dict[str, Any] = Field(default_factory=dict)
    ai_spatial_landing_contract: dict[str, Any] = Field(default_factory=dict)
    ai_spatial_landing_contract_runtime: dict[str, Any] = Field(default_factory=dict)
    path_mission_contract: dict[str, Any] = Field(default_factory=dict)
    path_missions: list[dict[str, Any]] = Field(default_factory=list)
    mission_aware_merge_summary: dict[str, Any] = Field(default_factory=dict)
    mission_evidence_ledger: dict[str, Any] = Field(default_factory=dict)
    master_judgement: dict[str, Any] = Field(default_factory=dict)
    mission_learning_rollup: dict[str, Any] = Field(default_factory=dict)
    probe_limit_reason: str | None = None
    answer_strands: list[AnswerStrand] = Field(default_factory=list)
    planner_seed_runtime: dict[str, Any] = Field(default_factory=dict)
    seed_goal_coverage: dict[str, Any] = Field(default_factory=dict)
    seed_destination_presence: dict[str, Any] = Field(default_factory=dict)
    probes: list[QueryProbe]
    branches: list[RetrieveBranch] = Field(default_factory=list)
    landing_metadata: list[dict[str, Any]] = Field(default_factory=list)
    route_truth_summary: dict[str, Any] = Field(default_factory=dict)
    search_map_2d_truth: dict[str, Any] = Field(default_factory=dict)
    map_stream_state: dict[str, Any] = Field(default_factory=dict)
    planner_runtime: dict[str, Any] = Field(default_factory=dict)


class SearchRunRequest(BaseModel):
    brain_id: str | None = None
    search_id: str = Field(min_length=1)

    @field_validator("search_id")
    @classmethod
    def trim_search_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("search_id_required")
        return value


class SearchRunResponse(BaseModel):
    search_id: str
    brain_id: str | None = None
    status: Literal["created", "running", "completed", "failed"]
    stream_url: str
    result_url: str


class SearchRunLedgerEntry(BaseModel):
    search_id: str
    brain_id: str | None = None
    thread_id: str | None = None
    query_text: str
    response_mode: str | None = None
    retrieval_mode: str | None = None
    status: str
    terminal_state: str
    completion_state: str
    completion_reason: str | None = None
    mcp_status: str | None = None
    provider_state: str | None = None
    provider_degraded: bool = False
    ai_required: bool = False
    ai_material: bool = False
    first_package_present: bool = False
    package_revision_id: str | None = None
    package_char_count: int = 0
    result_present: bool = False
    final_materialization_pending: bool = False
    result_ready_terminal: bool = False
    inspect_available: bool = False
    created_at: str
    updated_at: str
    event_count: int = 0


class SearchRunLedgerResponse(BaseModel):
    schema_version: str = "agvm.search_run_ledger.v1"
    brain_id: str | None = None
    entries: list[SearchRunLedgerEntry] = Field(default_factory=list)


class SearchStreamEvent(BaseModel):
    seq: int
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    event_family: str | None = None
    run_state: str | None = None
    surface_state: str | None = None
    terminal: bool = False
    stream_contract: dict[str, Any] = Field(default_factory=dict)


class SearchTraceResponse(BaseModel):
    search_id: str
    brain_id: str | None = None
    thread_id: str | None = None
    session: dict[str, Any] = Field(default_factory=dict)
    events: list[SearchStreamEvent] = Field(default_factory=list)
    corrections: list[dict[str, Any]] = Field(default_factory=list)
    timing: dict[str, Any] = Field(default_factory=dict)
    planner_metadata: dict[str, Any] = Field(default_factory=dict)
    answer_strands: list[AnswerStrand] = Field(default_factory=list)
    planner_seed_runtime: dict[str, Any] = Field(default_factory=dict)
    seed_goal_coverage: dict[str, Any] = Field(default_factory=dict)
    seed_destination_presence: dict[str, Any] = Field(default_factory=dict)
    landing_metadata: list[dict[str, Any]] = Field(default_factory=list)
    context_waves: list[dict[str, Any]] = Field(default_factory=list)
    worker_stop_reasons: dict[str, str] = Field(default_factory=dict)
    follow_up_candidates: list[dict[str, Any]] = Field(default_factory=list)
    blackboard: dict[str, Any] = Field(default_factory=dict)


class RegionSummary(BaseModel):
    region_id: str
    centroid: Position | None = None
    node_count: int = 0
    dominant_concepts: list[str] = Field(default_factory=list)
    dominant_memory_types: list[str] = Field(default_factory=list)
    identity_hints: list[str] = Field(default_factory=list)
    project_hints: list[str] = Field(default_factory=list)
    place_hints: list[str] = Field(default_factory=list)
    common_outbound_highways: list[Link] = Field(default_factory=list)
    density: dict[str, Any] = Field(default_factory=dict)
    instability_flags: list[str] = Field(default_factory=list)
    retrieval_usefulness: dict[str, Any] = Field(default_factory=dict)
    updated_at: str | None = None


class RegionSummaryResponse(BaseModel):
    summary: RegionSummary
    rebuilt_at: str | None = None


class RebuildRegionSummariesResponse(BaseModel):
    summaries: list[RegionSummary] = Field(default_factory=list)
    rebuilt_at: str


class CorrectAfterQueryRequest(BaseModel):
    brain_id: str | None = None
    query_text: str = Field(
        min_length=1,
        validation_alias=AliasChoices("query_text", "original_query"),
        serialization_alias="query_text",
    )
    returned_answer: str = Field(min_length=1)
    correction_text: str = Field(min_length=1)
    correction_mode: Literal["revise", "replace", "supersede", "archive", "delete"] = "revise"
    search_id: str | None = None
    used_evidence_node_ids: list[str] = Field(default_factory=list)
    target_node_ids: list[str] = Field(default_factory=list)

    @field_validator("query_text", "returned_answer", "correction_text")
    @classmethod
    def trim_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("text_required")
        return value


class CorrectAfterQueryPlanRequest(BaseModel):
    brain_id: str | None = None
    query_text: str = Field(
        min_length=1,
        validation_alias=AliasChoices("query_text", "original_query"),
        serialization_alias="query_text",
    )
    correction_prompt: str = Field(min_length=1)
    returned_answer: str | None = None
    search_id: str | None = None
    selected_action: dict[str, Any] = Field(default_factory=dict)
    used_evidence_node_ids: list[str] = Field(default_factory=list)
    target_node_ids: list[str] = Field(default_factory=list)

    @field_validator("query_text", "correction_prompt")
    @classmethod
    def trim_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("text_required")
        return value

    @field_validator("returned_answer")
    @classmethod
    def trim_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class CorrectAfterQueryPlanResponse(BaseModel):
    schema_version: str = "agvm.context_correction_plan.v1"
    search_id: str | None = None
    source: Literal["llm", "fallback"] = "fallback"
    planner_error: str | None = None
    correction_mode: Literal["revise", "replace", "supersede", "archive", "delete"] = "revise"
    correction_text: str
    human_summary: str
    rationale: str
    confidence: float = Field(ge=0.0, le=1.0)
    requires_confirmation: bool = True
    target_node_ids: list[str] = Field(default_factory=list)
    used_evidence_node_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CorrectAfterQueryResponse(BaseModel):
    search_id: str | None = None
    correction_id: str
    action_summary: dict[str, Any] = Field(default_factory=dict)
    graph: Graph
    atlas: AtlasResponse


class ClusterEdge(BaseModel):
    source: str
    target: str
    kind: Literal["candidate", "link", "highway", "derivation"]
    strength: float | None = None
    sources: list[str] = Field(default_factory=list)


class ClusterResponse(BaseModel):
    focus_node_id: str
    cluster_node_ids: list[str]
    candidate_ids: list[str]
    origin_node_id: str | None = None
    bucket_key: str | None = None
    candidate_sources: dict[str, list[str]]
    document_anchor_candidate_ids: list[str]
    highway_expansion_ids: list[str]
    debug_edges: list[ClusterEdge]


class AtlasBucket(BaseModel):
    bucket_key: str
    centroid: Position
    node_count: int
    document_anchor_count: int
    guide_area_histogram: dict[str, int]
    outgoing_highway_gateways: list[Link]
    fit_score: float | None = None


class AtlasResponse(BaseModel):
    bucket_size: float
    node_count: int
    bucket_count: int
    buckets: list[AtlasBucket]


class MaintenanceRequest(BaseModel):
    brain_id: str | None = None
    focus_node_id: str


class MaintenanceResponse(BaseModel):
    graph: Graph
    atlas: AtlasResponse
    cluster: ClusterResponse


class BootstrapRequest(BaseModel):
    brain_id: str | None = None
    text: str = Field(min_length=1)
    input_mode: Literal["auto", "document"] = "auto"
    source_label: str | None = None
    source_type: str | None = None
    source_trust: SourceTrust | None = None
    learning_mode: LearningMode = "strict_review"
    question_limit: int = Field(default=3, ge=1, le=8)

    @field_validator("text")
    @classmethod
    def trim_bootstrap_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("text_required")
        return value


class BootstrapResponse(BaseModel):
    brain_id: str | None = None
    graph: Graph
    persisted_node_ids: list[str]
    persisted_edge_count: int
    identity_nucleus: dict[str, Any] = Field(default_factory=dict)
    learning_policy: dict[str, Any] = Field(default_factory=dict)
    write_trace: WriteTrace


class GeometryCalibrationResponse(BaseModel):
    schema_version: str
    generated_at: str
    node_count: int = 0
    zone_counts: dict[str, int] = Field(default_factory=dict)
    overall_score: float = 0.0
    radial_alignment: dict[str, Any] = Field(default_factory=dict)
    zone_separation: dict[str, Any] = Field(default_factory=dict)
    document_project_coupling: dict[str, Any] = Field(default_factory=dict)
    highway_quality: dict[str, Any] = Field(default_factory=dict)
    path_bridge_potential: dict[str, Any] = Field(default_factory=dict)
    landing_density: dict[str, Any] = Field(default_factory=dict)
    spacing: dict[str, Any] = Field(default_factory=dict)
    benchmarks: dict[str, Any] = Field(default_factory=dict)
    recommendations: list[dict[str, Any]] = Field(default_factory=list)
    calibration_proposals: list[dict[str, Any]] = Field(default_factory=list)
    matrix_change_policy: dict[str, Any] = Field(default_factory=dict)


class AuditResponse(BaseModel):
    service: str
    version: str
    llm_enabled: bool
    provider_auth_ok: bool | None = None
    # Most runtime roles map directly to one model name. Product modules such
    # as Clone expose a bounded role-to-model map under their module key.
    llm_models: dict[str, Any]
    llm_runtime: dict[str, Any] = Field(default_factory=dict)
    runtime_signature: dict[str, Any] = Field(default_factory=dict)
    brain_registry: dict[str, Any] = Field(default_factory=dict)
    hosted_tenant_registry: dict[str, Any] = Field(default_factory=dict)
    geometry: dict[str, Any] = Field(default_factory=dict)
    brain_geometry_calibration: dict[str, Any] = Field(default_factory=dict)
    timing_percentiles: dict[str, Any] = Field(default_factory=dict)
    stop_reason_histogram: dict[str, Any] = Field(default_factory=dict)
    planner_mode_histogram: dict[str, Any] = Field(default_factory=dict)
    memory_type_histogram: dict[str, Any] = Field(default_factory=dict)
    guide_area_histogram: dict[str, Any] = Field(default_factory=dict)
    guide_area_blank_ratio: float = 0.0
    identity_memory_ratio: float = 0.0
    budget_exhausted_ratio: float = 0.0
    expected_guide_area_none_ratio: float = 0.0
    llm_scout_enabled_ratio: float = 0.0
    hybrid_merge_ratio: float = 0.0
    warm_hit_ratio: float = 0.0
    warm_partial_reuse_ratio: float = 0.0
    divergence_reset_ratio: float = 0.0
    answer_now_before_final_ratio: float = 0.0
    background_expansion_after_partial_ratio: float = 0.0
    warm_state_saved_ratio: float = 0.0
    continuity_state_histogram: dict[str, Any] = Field(default_factory=dict)
    mode_timing_percentiles: dict[str, Any] = Field(default_factory=dict)
    document_mode_detected_ratio: float = 0.0
    document_anchor_top_match_ratio: float = 0.0
    document_chunk_used_before_final_ratio: float = 0.0
    document_fact_support_ratio: float = 0.0
    raw_text_coverage_ratio: float = 0.0
    document_chunk_coverage_ratio: float = 0.0
    support_density: float = 0.0
    contradiction_exposure_ratio: float = 0.0
    highway_route_yield: float = 0.0
    branch_duplication_ratio: float = 0.0
    branch_merge_ratio: float = 0.0
    geometry_landing_fit_score: float = 0.0
    geometry_destination_alignment_score: float = 0.0
    geometry_projection_error_ratio: float = 0.0
    geometry_route_efficiency_score: float = 0.0
    matrix_a_problem_likelihood: float = 0.0
    matrix_a_adjustment_gain: float = 0.0
    warm_context_reuse_quality: float = 0.0
    document_answer_first_ms_by_mode: dict[str, Any] = Field(default_factory=dict)
    document_warm_followup_delta_ms: dict[str, Any] = Field(default_factory=dict)
    route_trace_session_ratio: float = 0.0
    route_travel_session_ratio: float = 0.0
    highway_route_use_ratio: float = 0.0
    link_route_use_ratio: float = 0.0
    local_route_use_ratio: float = 0.0
    route_richness_score: float = 0.0
    highway_effective_use_ratio: float = 0.0
    link_effective_use_ratio: float = 0.0
    heuristic_family_route_step_ratio: float = 0.0
    ai_family_route_step_ratio: float = 0.0
    dual_origin_family_route_step_ratio: float = 0.0
    destination_reached_ratio: float = 0.0
    execution_reorder_count: int = 0
    execution_reorder_reasons: dict[str, Any] = Field(default_factory=dict)
    merge_trigger_ratio: float = 0.0
    branch_controller_usage_ratio: float = 0.0
    branch_controller_override_ratio: float = 0.0
    master_llm_success_ratio: float = 0.0
    master_fallback_timeout_ratio: float = 0.0
    answer_now_before_exploration_complete_ratio: float = 0.0
    final_closure_after_destination_resolution_ratio: float = 0.0
    context_level_1_before_final_ratio: float = 0.0
    master_surface_state_histogram: dict[str, Any] = Field(default_factory=dict)
    master_fallback_reason_histogram: dict[str, Any] = Field(default_factory=dict)
    closure_blocker_reason_histogram: dict[str, Any] = Field(default_factory=dict)
    planner_influence_ratio: float = 0.0
    planner_family_dual_active_ratio: float = 0.0
    planner_family_win_ratio: float = 0.0
    planner_family_tie_ratio: float = 0.0
    planner_family_attribution_ratio: float = 0.0
    planner_arrival_ms: dict[str, Any] = Field(default_factory=dict)
    planner_family_overlap_ratio: float = 0.0
    planner_family_divergence_ratio: float = 0.0
    planner_seed_ms: dict[str, Any] = Field(default_factory=dict)
    planner_seed_success_ratio: float = 0.0
    ai_material_contribution_ratio: float = 0.0
    ai_contribution_reason_histogram: dict[str, Any] = Field(default_factory=dict)
    canonical_telemetry: dict[str, Any] = Field(default_factory=dict)
    audit_truth_checks: dict[str, Any] = Field(default_factory=dict)
    answer_strand_count: float = 0.0
    seed_goal_coverage_ratio: float = 0.0
    seed_destination_presence_ratio: float = 0.0
    seed_used_by_bootstrap_ratio: float = 0.0
    branch_reuse_ratio: float = 0.0
    branch_enrich_ratio: float = 0.0
    branch_fork_ratio: float = 0.0
    dual_origin_branch_ratio: float = 0.0
    merge_resolution_histogram: dict[str, Any] = Field(default_factory=dict)
    heuristic_calibration_scope_count: int = 0
    heuristic_calibration_event_count: int = 0
    heuristic_compiled_prior_count: int = 0
    heuristic_failure_signature_count: int = 0
    heuristic_review_candidate_count: int = 0
    heuristic_calibration_gain: float = 0.0
    post_retrieval_calibration_gain: float = 0.0
    calibrated_bootstrap_success_ratio: float = 0.0
    calibrated_branch_count_delta: float = 0.0
    calibrated_highway_use_delta: float = 0.0
    heuristic_calibration_summary: dict[str, Any] = Field(default_factory=dict)
    maintenance_run_count: int = 0
    applied_maintenance_run_count: int = 0
    maintenance_modes_histogram: dict[str, Any] = Field(default_factory=dict)
    maintenance_improvement_ratio: float = 0.0
    maintenance_geometry_improvement_ratio: float = 0.0
    maintenance_identity_improvement_ratio: float = 0.0
    maintenance_proactive_suggestion_ratio: float = 0.0
    maintenance_repeated_evidence_ratio: float = 0.0
    sleep_review_change_ratio: float = 0.0
    sleep_bridge_adjustment_ratio: float = 0.0
    evolve_structural_change_ratio: float = 0.0
    evolve_new_highway_ratio: float = 0.0
    sleep_vs_evolve_overlap_ratio: float = 0.0
    maintenance_retrieval_gap_detection_ratio: float = 0.0
    maintenance_retrieval_gap_run_ratio: float = 0.0
    working_memory_depromotion_candidate_ratio: float = 0.0
    working_memory_depromotion_review_count: int = 0
    maintenance_mode_specific_quality_delta: dict[str, Any] = Field(default_factory=dict)
    last_benchmark: dict[str, Any] = Field(default_factory=dict)
    latest_stream_benchmark: dict[str, Any] = Field(default_factory=dict)
    latest_documents_benchmark: dict[str, Any] = Field(default_factory=dict)
    latest_maintenance_benchmark: dict[str, Any] = Field(default_factory=dict)
    latest_calibration_benchmark: dict[str, Any] = Field(default_factory=dict)
    latest_planner_merge_benchmark: dict[str, Any] = Field(default_factory=dict)
    latest_geometry_benchmark: dict[str, Any] = Field(default_factory=dict)
    latest_route_richness_benchmark: dict[str, Any] = Field(default_factory=dict)
    latest_master_closure_benchmark: dict[str, Any] = Field(default_factory=dict)
    latest_evaluation_benchmark: dict[str, Any] = Field(default_factory=dict)
    final_evaluation_matrix: dict[str, Any] = Field(default_factory=dict)
    maintenance_quality_scores: list[float] = Field(default_factory=list)
    metamemory: dict[str, Any] = Field(default_factory=dict)
    endpoints: list[str] = Field(default_factory=list)
    data_files: dict[str, Any] = Field(default_factory=dict)
    guide_checklist: dict[str, str] = Field(default_factory=dict)


class GuideComplianceEntry(BaseModel):
    phase: str
    guide_refs: list[str] = Field(default_factory=list)
    status: Literal["pass", "partial", "fail"] = "partial"
    intended_behavior: str = ""
    implemented_behavior: str = ""
    live_evidence: dict[str, Any] = Field(default_factory=dict)
    open_gaps: list[str] = Field(default_factory=list)


class GuideComplianceResponse(BaseModel):
    generated_at: str
    entries: list[GuideComplianceEntry] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)


class ResetMemoryResponse(BaseModel):
    graph: Graph
    atlas: AtlasResponse
    reset_at: str
    message: str


class IdentityNucleusResponse(BaseModel):
    identity_nucleus: dict[str, Any] = Field(default_factory=dict)
    rebuilt_at: str


class SleepEvolveRequest(BaseModel):
    brain_id: str | None = None
    preview_only: bool = True
    focus_node_id: str | None = None
    max_nodes_considered: int = Field(default=80, ge=10, le=500)


class SleepEvolveReport(BaseModel):
    applied: bool
    mode: str = "sleep_evolve"
    preview_budget_guard: dict[str, Any] = Field(default_factory=dict)
    maintenance_preview_plan: dict[str, Any] = Field(default_factory=dict)
    sleep_evolve_lifecycle_contract: dict[str, Any] = Field(default_factory=dict)
    maintenance_contract: dict[str, Any] = Field(default_factory=dict)
    proposal_schema: dict[str, Any] = Field(default_factory=dict)
    metamemory_snapshot: dict[str, Any] = Field(default_factory=dict)
    failure_signatures: dict[str, Any] = Field(default_factory=dict)
    maintenance_proposals: list[dict[str, Any]] = Field(default_factory=list)
    maintenance_proposal_summary: dict[str, Any] = Field(default_factory=dict)
    sleep_consolidation_proposals: list[dict[str, Any]] = Field(default_factory=list)
    sleep_consolidation_profile: dict[str, Any] = Field(default_factory=dict)
    deduction_candidates: list[dict[str, Any]] = Field(default_factory=list)
    deduction_mining: dict[str, Any] = Field(default_factory=dict)
    evolve_structural_proposals: list[dict[str, Any]] = Field(default_factory=list)
    elastic_topology_proposals: list[dict[str, Any]] = Field(default_factory=list)
    evolve_structural_profile: dict[str, Any] = Field(default_factory=dict)
    retrieval_trace_learning_proposals: list[dict[str, Any]] = Field(default_factory=list)
    retrieval_trace_learning_gate: dict[str, Any] = Field(default_factory=dict)
    ingest_learning_review: dict[str, Any] = Field(default_factory=dict)
    memory_policy_revision_preview: dict[str, Any] = Field(default_factory=dict)
    memory_policy_revision_candidate: dict[str, Any] = Field(default_factory=dict)
    apply_policy_guard: dict[str, Any] = Field(default_factory=dict)
    maintenance_transaction: dict[str, Any] = Field(default_factory=dict)
    rollback_snapshot: dict[str, Any] = Field(default_factory=dict)
    no_corruption_guards: dict[str, Any] = Field(default_factory=dict)
    before_after_audit: dict[str, Any] = Field(default_factory=dict)
    reviewed_node_ids: list[str] = Field(default_factory=list)
    duplicate_candidates: list[dict[str, Any]] = Field(default_factory=list)
    merges: list[dict[str, Any]] = Field(default_factory=list)
    alias_attachments: list[dict[str, Any]] = Field(default_factory=list)
    confidence_updates: list[dict[str, Any]] = Field(default_factory=list)
    highway_changes: list[dict[str, Any]] = Field(default_factory=list)
    pattern_candidates: list[dict[str, Any]] = Field(default_factory=list)
    created_nodes: list[dict[str, Any]] = Field(default_factory=list)
    repositioned_nodes: list[dict[str, Any]] = Field(default_factory=list)
    new_highways: list[dict[str, Any]] = Field(default_factory=list)
    deleted_node_ids: list[str] = Field(default_factory=list)
    archived_node_ids: list[str] = Field(default_factory=list)
    superseded_node_ids: list[str] = Field(default_factory=list)
    region_actions: list[dict[str, Any]] = Field(default_factory=list)
    trace_insights: dict[str, Any] = Field(default_factory=dict)
    correction_insights: dict[str, Any] = Field(default_factory=dict)
    bridge_promotions: list[dict[str, Any]] = Field(default_factory=list)
    bridge_demotions: list[dict[str, Any]] = Field(default_factory=list)
    retyped_nodes: list[dict[str, Any]] = Field(default_factory=list)
    follow_up_candidates: list[dict[str, Any]] = Field(default_factory=list)
    prepared_next_angles: list[dict[str, Any]] = Field(default_factory=list)
    proactive_opportunities: list[dict[str, Any]] = Field(default_factory=list)
    maintenance_history_summary: dict[str, Any] = Field(default_factory=dict)
    ai_review_runtime: dict[str, Any] = Field(default_factory=dict)
    sleep_profile: dict[str, Any] = Field(default_factory=dict)
    evolve_profile: dict[str, Any] = Field(default_factory=dict)
    mode_overlap_summary: dict[str, Any] = Field(default_factory=dict)
    maintenance_mode_specific_quality_delta: dict[str, Any] = Field(default_factory=dict)
    highway_calibration_profile: dict[str, Any] = Field(default_factory=dict)
    document_anchor_guard: dict[str, Any] = Field(default_factory=dict)
    self_improvement_loop: dict[str, Any] = Field(default_factory=dict)
    calibration_before: dict[str, Any] = Field(default_factory=dict)
    calibration_after: dict[str, Any] = Field(default_factory=dict)
    calibration_delta: dict[str, Any] = Field(default_factory=dict)
    calibration_evidence_basis: list[dict[str, Any]] = Field(default_factory=list)
    compiled_prior_recommendations: list[dict[str, Any]] = Field(default_factory=list)
    calibration_review_candidates: list[dict[str, Any]] = Field(default_factory=list)
    calibration_event_ids: list[str] = Field(default_factory=list)
    retrieval_gap_review: dict[str, Any] = Field(default_factory=dict)
    working_memory_depromotion_policy: dict[str, Any] = Field(default_factory=dict)
    quality_before: dict[str, Any] = Field(default_factory=dict)
    quality_after: dict[str, Any] = Field(default_factory=dict)
    quality_delta: dict[str, Any] = Field(default_factory=dict)
    overall_quality_delta_score: float = 0.0
    nucleus_refresh: dict[str, Any] = Field(default_factory=dict)
    untouched_areas: list[str] = Field(default_factory=list)
    reasoning_summary: str = ""


class SleepEvolveResponse(BaseModel):
    report: SleepEvolveReport
    graph: Graph | None = None
    atlas: AtlasResponse | None = None


class SeedCoverageRequest(BaseModel):
    brain_id: str | None = None
    count: int = Field(default=280, ge=1, le=10000)
    reset_first: bool = True
    include_bootstrap: bool = True
    atlas_refresh_interval: int = Field(default=80, ge=10, le=500)


class SeedCoverageResponse(BaseModel):
    graph: Graph
    atlas: AtlasResponse
    summary: dict[str, Any] = Field(default_factory=dict)


class BrainRegistryBootstrapRequest(BaseModel):
    legacy_data_dirs: list[str] = Field(default_factory=list)
    default_brain_id: str | None = None
    force_rescan: bool = True


class BrainSelectionRequest(BaseModel):
    brain_id: str = Field(min_length=1)
    make_default: bool = False


class BrainCreateRequest(BaseModel):
    brain_id: str | None = None
    display_name: str = Field(min_length=1)
    description: str | None = None
    make_default: bool = False
    make_active: bool = True


class BrainEnsureRequest(BaseModel):
    brain_id: str | None = None
    display_name: str = Field(min_length=1)
    description: str | None = None
    purpose: str | None = None
    activation_policy: Literal["return_only", "make_active", "make_default"] = "return_only"
    create_if_missing: bool = True

    @field_validator("display_name")
    @classmethod
    def trim_required_brain_ensure_display_name(cls, value: str) -> str:
        trimmed = str(value or "").strip()
        if not trimmed:
            raise ValueError("display_name_required")
        return trimmed

    @field_validator("brain_id", "description", "purpose")
    @classmethod
    def trim_optional_brain_ensure_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = str(value).strip()
        return trimmed or None


class BrainRenameRequest(BaseModel):
    brain_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    description: str | None = None


class BrainDeleteRequest(BaseModel):
    brain_id: str = Field(min_length=1)
    confirm_brain_id: str = Field(min_length=1)
    delete_storage: bool = False


class BrainExportRequest(BaseModel):
    brain_id: str = Field(min_length=1)
    export_dir: str | None = None


class BrainImportRequest(BaseModel):
    archive_path: str = Field(min_length=1)
    brain_id: str | None = None
    display_name: str | None = None
    make_active: bool = False
    make_default: bool = False
    overwrite_existing: bool = False


class BrainAdminOperationResponse(BaseModel):
    schema_version: str
    action: str
    status: str
    brain_id: str | None = None
    brain: dict[str, Any] = Field(default_factory=dict)
    registry: dict[str, Any] = Field(default_factory=dict)
    archive_path: str | None = None
    archive_size_bytes: int | None = None
    file_count: int | None = None
    export_manifest: dict[str, Any] = Field(default_factory=dict)
    import_manifest: dict[str, Any] = Field(default_factory=dict)
    deleted_storage: bool | None = None
    warnings: list[str] = Field(default_factory=list)
    next_slice: str = ""


class BrainEnsureResponse(BaseModel):
    schema_version: str = "agvm.local_brain_ensure_result.v1"
    status: Literal["existing", "created", "selected", "blocked"]
    brain_id: str | None = None
    brain: dict[str, Any] = Field(default_factory=dict)
    registry: dict[str, Any] = Field(default_factory=dict)
    created: bool = False
    selected: bool = False
    activation_policy: Literal["return_only", "make_active", "make_default"] = "return_only"
    next_recommended_tools: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class BrainRegistryResponse(BaseModel):
    schema_version: str
    registry_id: str = "local"
    registry_path: str = ""
    brain_root: str = ""
    storage_format_version: str = ""
    created_at: str = ""
    updated_at: str = ""
    active_brain_id: str | None = None
    default_brain_id: str | None = None
    brain_count: int = 0
    brains: list[dict[str, Any]] = Field(default_factory=list)
    legacy_data_dir_policy: dict[str, Any] = Field(default_factory=dict)
    runtime_scope_status: str = ""
    product_boundary: dict[str, Any] = Field(default_factory=dict)
    validation: dict[str, Any] = Field(default_factory=dict)
    next_slice: str = ""


class HostedTenantBootstrapRequest(BaseModel):
    tenant_id: str = "local_tenant"
    organization_id: str = "local_org"
    user_id: str = "local_user"
    environment_id: str = "local_self_hosted_dev"
    reset: bool = False


class HostedBrainResolveRequest(BaseModel):
    tenant_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    brain_id: str | None = None
    organization_id: str | None = None
    environment_id: str | None = None


class HostedTenantRegistryResponse(BaseModel):
    schema_version: str
    registry_id: str = "hosted_dev"
    registry_path: str = ""
    brain_root: str = ""
    created_at: str = ""
    updated_at: str = ""
    tenants: list[dict[str, Any]] = Field(default_factory=list)
    users: list[dict[str, Any]] = Field(default_factory=list)
    brain_bindings: list[dict[str, Any]] = Field(default_factory=list)
    product_boundary: dict[str, Any] = Field(default_factory=dict)
    migration_plan: dict[str, Any] = Field(default_factory=dict)
    validation: dict[str, Any] = Field(default_factory=dict)
    next_slice: str = ""


class HostedBrainScopeResolutionResponse(BaseModel):
    schema_version: str
    status: str
    tenant_id: str
    organization_id: str
    user_id: str
    environment_id: str
    brain_id: str
    local_brain_id: str
    hosted_brain_id: str
    graph_id: str
    document_namespace: str
    source_namespace: str
    audit_namespace: str
    source_hash_namespace: str
    brain_binding: dict[str, Any] = Field(default_factory=dict)
    local_brain: dict[str, Any] = Field(default_factory=dict)
    registry_validation: dict[str, Any] = Field(default_factory=dict)


BenchmarkPhase = Literal["product_harness", "source_intake", "retrieval_mcp", "ui_truth", "backend_integrity", "local_mcp_client", "live_product_matrix", "product_ready_local_gate", "final_gate_expansion", "final_self_hosted_readiness", "local_beta_fast_health", "local_mcp_product_matrix", "simone_source_manifest_reset_guard", "simone_source_intake_grow_preview", "node_atomicity_identity_link_coherence", "real_validation_brain_clean_density", "large_brain_scale_radial_matrix_distribution", "retrieve_context_quality_matrix", "sleep_evolve_metamemory_heuristic_evolution", "final_grow_retrieve_sleep_evolve_verdict", "phase8c_comparative_backend_benchmark", "external_certification", "product_scorecard", "self_hosted_readiness", "hosted_tenant_isolation", "smoke", "modes", "stream", "trace", "documents", "maintenance", "calibration", "planner_seed", "planner_merge", "geometry_audit", "route_richness", "master_closure", "recursive_contract", "evaluation", "all", "slice1_revalidation"]


class BenchmarkRunRequest(BaseModel):
    brain_id: str | None = None
    base_url: str | None = None
    phase: BenchmarkPhase = "all"
    suite_timeout_seconds: float | None = Field(default=None, ge=0.01)
    suite_budgets: dict[str, float] = Field(default_factory=dict)
    suite_names: list[str] = Field(default_factory=list)


class BenchmarkRunResponse(BaseModel):
    phase: BenchmarkPhase
    report: dict[str, Any] = Field(default_factory=dict)
    completed_at: str
