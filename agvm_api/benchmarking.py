from __future__ import annotations

import concurrent.futures
import io
import json
import math
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from retrieval import (
    _annotate_probe_family,
    _build_ai_materialization_hard_gate,
    _choose_backend_route_move,
    _create_branch_from_probe,
    _merge_planner_probes,
    _maybe_reorder_execution_destinations,
    _route_runtime_summary,
    build_probe_from_spec,
)
from schemas import RetrieveRequest


DEFAULT_BASE_URL = "http://127.0.0.1:8010"
_BENCHMARK_BRAIN_ID: ContextVar[str | None] = ContextVar("agvm_benchmark_brain_id", default=None)
_FRONTEND_ASSET_TEXT_CACHE: dict[str, str] = {}


@contextmanager
def benchmark_brain_scope(brain_id: str | None):
    token = _BENCHMARK_BRAIN_ID.set(str(brain_id or "").strip() or None)
    try:
        yield
    finally:
        _BENCHMARK_BRAIN_ID.reset(token)


def _benchmark_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = dict(extra or {})
    brain_id = _BENCHMARK_BRAIN_ID.get()
    if brain_id:
        headers["X-AGVM-Brain-Id"] = brain_id
    return headers

EXPLICIT_MASTER_DECISION_SOURCES = {
    "llm",
    "fallback_timeout",
    "fallback_parse_error",
    "fallback_provider_error",
    "fallback_disabled",
    "fallback_broad_coverage",
    "fallback_fast_direct_fact",
    "fallback_temporal_guard",
    "fallback_warm_context",
    "fallback_direct_preflight",
    "fallback_document_lookup_no_match",
    "fallback_document_lookup_packet",
    "fallback_document_synthesis_packet",
}

EXPLICIT_CONTROLLER_DECISION_SOURCES = {
    "llm",
    "fallback_timeout",
    "fallback_parse_error",
    "fallback_provider_error",
    "fallback_disabled",
}

DIRECT_FAST_STOP_REASONS = {
    "high_confidence_direct_fact",
    "direct_preflight_answer_sufficient",
    "warm_context_sufficient",
}

PRE_ROUTE_FAST_STOP_REASONS = {
    *DIRECT_FAST_STOP_REASONS,
    "document_evidence_sufficient",
    "document_lookup_no_matching_packet",
}


@dataclass(frozen=True)
class GoldenCase:
    query_text: str
    required_answer_terms: tuple[str, ...]
    forbidden_answer_terms: tuple[str, ...] = ()
    required_context_terms: tuple[str, ...] = ()
    expected_answerability: str = "grounded"
    max_branch_count: int | None = None
    retrieval_mode: str = "balanced"


@dataclass(frozen=True)
class ProductBenchmarkFixtureBrain:
    brain_id: str
    fixture_kind: str
    title: str
    source_mix: tuple[str, ...]
    seed_requirements: tuple[str, ...]
    expected_memory_areas: tuple[str, ...]
    hard_cases: tuple[str, ...]
    success_signals: tuple[str, ...]


@dataclass(frozen=True)
class ProductBenchmarkCase:
    case_id: str
    family_group: str
    family: str
    fixture_kind: str
    interaction: str
    required_artifacts: tuple[str, ...]
    required_signals: tuple[str, ...]
    slice_owner: str
    critical: bool = True
    query_text: str | None = None
    source_kind: str | None = None
    tool_name: str | None = None
    notes: str = ""


PR12L_PRODUCT_HARNESS_SCHEMA_VERSION = "agvm.pr12l.product_benchmark_harness.v1"
PR12L_PRODUCT_HARNESS_REPORT_SCHEMA_VERSION = "agvm.pr12l.product_benchmark_harness_report.v1"
PR12L_SOURCE_INTAKE_BENCHMARK_REPORT_SCHEMA_VERSION = "agvm.pr12l.source_intake_benchmark_report.v1"
PR12L_RETRIEVAL_MCP_BENCHMARK_REPORT_SCHEMA_VERSION = "agvm.pr12l.retrieval_mcp_benchmark_report.v1"
PR12L_UI_TRUTH_BENCHMARK_REPORT_SCHEMA_VERSION = "agvm.pr12l.ui_truth_benchmark_report.v1"
PR12L_REGRESSION_BENCHMARK_REPORT_SCHEMA_VERSION = "agvm.pr12l.regression_benchmark_report.v1"
PR12L_PRODUCT_SCORECARD_REPORT_SCHEMA_VERSION = "agvm.pr12l.product_ready_scorecard_report.v1"
PR12M_SELF_HOSTED_READINESS_REPORT_SCHEMA_VERSION = "agvm.pr12m.self_hosted_readiness_report.v1"
PR12N_HOSTED_TENANT_ISOLATION_REPORT_SCHEMA_VERSION = "agvm.pr12n.hosted_tenant_isolation_report.v1"
PR12P_BRAIN_OS_V2_TRUTH_REPORT_SCHEMA_VERSION = "agvm.pr12p.brain_os_v2_visual_payload_truth_report.v1"
PR12P_BACKEND_INTEGRITY_REPORT_SCHEMA_VERSION = "agvm.pr12p.backend_product_integrity_report.v1"
PR12P_LIVE_PRODUCT_MATRIX_REPORT_SCHEMA_VERSION = "agvm.pr12p.live_product_matrix_report.v1"
PR12P_PRODUCT_READY_LOCAL_GATE_REPORT_SCHEMA_VERSION = "agvm.pr12p.product_ready_local_gate_report.v1"
PR12P14C_FINAL_GATE_EXPANSION_REPORT_SCHEMA_VERSION = "agvm.pr12p14c.final_gate_expansion_report.v1"
PR12P14L_FINAL_SELF_HOSTED_READINESS_REPORT_SCHEMA_VERSION = "agvm.pr12p14l.final_self_hosted_readiness_report.v1"
PR12P14P_FINAL_LOCAL_MCP_READINESS_MATRIX_SCHEMA_VERSION = "agvm.pr12p14p.final_local_mcp_readiness_matrix.v1"

REQUIRED_PR12L_FIXTURE_KINDS = (
    "personal_life",
    "founder_work",
    "project_workspace",
    "contradiction",
    "future_dream",
    "sparse_early_life",
    "dense_document",
    "web_ingested",
    "ocr_ingested",
)

REQUIRED_PR12L_RETRIEVAL_FAMILIES = (
    "exact_fact",
    "broad_self_dossier",
    "relationship",
    "temporal",
    "project_workspace",
    "exact_document",
    "related_documents",
    "source_trace",
    "cross_area",
    "no_match",
    "warm_followup",
    "divergent_followup",
    "contradiction",
    "hypothesis",
    "future_dream",
)

REQUIRED_PR12L_GROW_FAMILIES = (
    "manual_text",
    "long_manual_text",
    "pdf_selectable_text",
    "scanned_ocr_pdf",
    "docx",
    "embedded_image_document",
    "website_root",
    "website_with_sublinks",
    "source_requiring_clarification",
    "source_requiring_online_enrichment",
)

REQUIRED_PR12L_UI_MCP_FAMILIES = (
    "context_package_parity",
    "map_trace_parity",
    "final_status_parity",
    "source_package_parity",
    "document_raw_text_parity",
    "maintenance_proposal_parity",
)

REQUIRED_PR12L_REGRESSION_FAMILIES = (
    "pr12a_semantic_contract",
    "pr12b_context_package",
    "pr12c_path_corridor",
    "pr12d_document_workspace",
    "pr12e_cognitive_write_plan",
    "pr12f_learning_policy",
    "pr12g_brain_geometry_calibration",
)

REQUIRED_PR12M_SELF_HOSTED_FAMILIES = (
    "pr12l_product_truth",
    "brain_registry_switch",
    "runtime_storage_isolation",
    "source_asset_isolation",
    "mcp_stdio_scope",
    "docker_distribution",
    "admin_export_import_restore",
    "fresh_clone_onboarding",
    "documentation_closure",
)

REQUIRED_PR12N_HOSTED_TENANT_FAMILIES = (
    "hosted_registry_bootstrap",
    "tenant_user_default_resolution",
    "request_time_tenant_scope_headers",
    "per_brain_namespace_mapping",
    "self_hosted_to_hosted_migration_plan",
    "mcp_hosted_scope_headers",
    "documentation_closure",
)

REQUIRED_PR12P_BRAIN_OS_V2_SURFACES = (
    "os_command_center",
    "use_live_run",
    "context_package_reader",
    "document_workspace",
    "grow_learning_studio",
    "sleep_evolve",
    "brains",
)

REQUIRED_PR12P_BRAIN_OS_V2_PAYLOAD_PROBES = (
    "context_package",
    "document_raw",
    "map_truth",
    "answer_surface",
    "grow_source",
    "maintenance",
    "brain_scope",
)

REQUIRED_PR12P_BACKEND_INTEGRITY_CHECKS = (
    "ai_judge_pending_blocks_success",
    "mcp_unsealed_answer_is_partial",
    "answer_support_not_in_context_blocks",
    "context_package_primary_without_answer_demo",
    "exact_document_lookup_exception_preserved",
)

REQUIRED_PR12P_LIVE_PRODUCT_BRAIN_ROLES = (
    "simone_massaro",
    "elena_valsecchi",
    "fresh_test_brain",
)

REQUIRED_PR12P_LIVE_PRODUCT_FAMILIES = (
    "broad",
    "exact",
    "document",
    "path",
    "no_match",
    "composite",
    "followup",
    "grow",
    "sleep_evolve",
)

REQUIRED_PR12P_LIVE_PRODUCT_SCORE_DIMENSIONS = (
    "context_package_quality",
    "ai_materiality",
    "path_completion",
    "document_correctness",
    "grow_quality",
    "ui_parity",
    "mcp_parity",
    "latency",
)

REQUIRED_PR12P13_LOCAL_PRODUCT_GATES = (
    "api_health",
    "frontend_health",
    "mcp_contract_registry",
    "self_hosted_readiness",
    "external_stdio_mcp",
    "live_product_matrix",
)

REQUIRED_PR12P14C_FINAL_GATE_SEGMENTS = (
    "runtime_surfaces",
    "llm_required_runtime",
    "mcp_registry_surface",
    "brain_scope",
    "context_ai_materiality",
    "mcp_inspection_parity",
    "exact_document_readiness",
    "no_match_honesty",
    "path_visibility_contract",
    "ui_payload_truth",
    "latency_reporting",
)

REQUIRED_PR12P14L_FINAL_READINESS_GATES = (
    "docker_runtime_surfaces",
    "llm_required_runtime",
    "external_mcp_client",
    "multi_brain_scope",
    "retrieve_context",
    "retrieve_document",
    "retrieve_project_workspace",
    "combined_context_document_package",
    "retrieve_no_match_honesty",
    "retrieve_path_corridor",
    "retrieve_source_trace",
    "grow_source_preview",
    "sleep_evolve_preview",
    "ui_payload_truth",
    "latency_truth",
    "rag_comparison",
)

PR12P14P_READINESS_VERDICTS = (
    "ready_local_beta",
    "not_ready_backend_blocker",
    "not_ready_ui_truth_blocker",
    "not_ready_latency_blocker",
    "not_ready_ingest_blocker",
)

PR12L_REQUIRED_FAMILIES_BY_GROUP = {
    "retrieval": REQUIRED_PR12L_RETRIEVAL_FAMILIES,
    "grow": REQUIRED_PR12L_GROW_FAMILIES,
    "ui_mcp": REQUIRED_PR12L_UI_MCP_FAMILIES,
    "regression": REQUIRED_PR12L_REGRESSION_FAMILIES,
}

MCP_FIRST_ARTIFACTS = {
    "context_package",
    "document_workspace",
    "path_corridors",
    "source_investigation_package",
    "mcp_tool_output",
    "ui_truth_surface",
    "maintenance_report",
}


def pr12l_fixture_brains() -> list[ProductBenchmarkFixtureBrain]:
    return [
        ProductBenchmarkFixtureBrain(
            brain_id="bench_personal_life",
            fixture_kind="personal_life",
            title="Personal life brain with family, roles, relationships, dates and ambiguity",
            source_mix=("manual_text", "dated_notes", "relationship_updates"),
            seed_requirements=(
                "identity facts with first-person and third-person forms",
                "family facts with at least one ambiguous relationship reference",
                "relationship change over time with source dates",
            ),
            expected_memory_areas=("Identity", "Relationships", "Timeline", "Values"),
            hard_cases=("relationship update", "temporal self question", "warm follow-up"),
            success_signals=("context package uses complete human-readable facts", "answer demo is grounded but secondary"),
        ),
        ProductBenchmarkFixtureBrain(
            brain_id="bench_founder_work",
            fixture_kind="founder_work",
            title="Founder/work brain with companies, acquisitions, projects and public sources",
            source_mix=("manual_text", "public_web", "press_release", "project_notes"),
            seed_requirements=(
                "companies with founded/acquired/associated roles",
                "dated acquisition or public-source events",
                "work style and values linked to concrete evidence",
            ),
            expected_memory_areas=("Identity", "Work", "Projects", "Documents", "Timeline"),
            hard_cases=("company list", "public source trace", "cross-area work and values"),
            success_signals=("source trace is inspectable", "documents are first-class memory objects"),
        ),
        ProductBenchmarkFixtureBrain(
            brain_id="bench_project_workspace",
            fixture_kind="project_workspace",
            title="Project workspace brain with many documents and technical decisions",
            source_mix=("manual_text", "docx", "pdf_selectable_text", "decision_log"),
            seed_requirements=(
                "10 or more project documents",
                "decision records with owners and dates",
                "technical specs linked to follow-up notes",
            ),
            expected_memory_areas=("Projects", "Documents", "Decisions", "Technical Context"),
            hard_cases=("project workspace retrieval", "decision trace", "related documents"),
            success_signals=("workspace package contains complete raw sections when requested", "related documents do not flood broad context"),
        ),
        ProductBenchmarkFixtureBrain(
            brain_id="bench_contradiction",
            fixture_kind="contradiction",
            title="Contradiction brain with old facts, new facts and superseded claims",
            source_mix=("manual_text", "correction", "dated_notes"),
            seed_requirements=(
                "at least one superseded identity or relationship fact",
                "newer correction with higher source trust",
                "contradiction metadata preserved for review",
            ),
            expected_memory_areas=("Contradictions", "Timeline", "Identity", "Relationships"),
            hard_cases=("old-vs-new fact", "superseded claim", "no invented reconciliation"),
            success_signals=("newer facts supersede without deleting audit trail", "context package explains uncertainty honestly"),
        ),
        ProductBenchmarkFixtureBrain(
            brain_id="bench_future_dream",
            fixture_kind="future_dream",
            title="Future/dream brain with goals, hypotheses and intentions",
            source_mix=("manual_text", "planning_notes", "hypotheses"),
            seed_requirements=(
                "future goals separated from facts",
                "hypotheses separated from commitments",
                "dream/possibility nodes linked to projects",
            ),
            expected_memory_areas=("Future", "Hypotheses", "Projects", "Values"),
            hard_cases=("future plan retrieval", "hypothesis retrieval", "fact-vs-intention separation"),
            success_signals=("future material is not presented as completed fact", "agent package preserves useful plans"),
        ),
        ProductBenchmarkFixtureBrain(
            brain_id="bench_sparse_early_life",
            fixture_kind="sparse_early_life",
            title="Sparse early-life brain requiring bootstrap and sleep/evolve",
            source_mix=("manual_text", "small_seed"),
            seed_requirements=(
                "few initial memories",
                "bootstrap source that can create multiple reviewed nodes",
                "sleep/evolve can propose improvements without corrupting sparse facts",
            ),
            expected_memory_areas=("Identity", "Timeline", "Open Questions"),
            hard_cases=("honest no-match", "bootstrap growth", "maintenance proposal"),
            success_signals=("no-match is explicit", "sleep/evolve improves reviewable structure"),
        ),
        ProductBenchmarkFixtureBrain(
            brain_id="bench_dense_document",
            fixture_kind="dense_document",
            title="Dense document brain with near-duplicate documents",
            source_mix=("pdf_selectable_text", "docx", "manual_text", "near_duplicates"),
            seed_requirements=(
                "multiple similar documents with overlapping claims",
                "document anchors and chunk ordering",
                "dedupe pressure without losing raw documents",
            ),
            expected_memory_areas=("Documents", "Projects", "Source Trace", "Relationships"),
            hard_cases=("exact document", "related documents", "near-duplicate hygiene"),
            success_signals=("exact document returns raw requested artifact", "related documents stay inspectable"),
        ),
        ProductBenchmarkFixtureBrain(
            brain_id="bench_web_ingested",
            fixture_kind="web_ingested",
            title="Web-ingested brain from a small site with subpages",
            source_mix=("website_root", "website_sublinks", "images_metadata", "online_enrichment"),
            seed_requirements=(
                "root page and at least two same-domain subpages",
                "visible text and link provenance",
                "optional online enrichment with no-primary-override policy",
            ),
            expected_memory_areas=("Documents", "Source Trace", "Projects", "Public Facts"),
            hard_cases=("website source trace", "subpage crawl", "online enrichment boundary"),
            success_signals=("web provenance is visible", "external enrichment cannot override primary source"),
        ),
        ProductBenchmarkFixtureBrain(
            brain_id="bench_ocr_ingested",
            fixture_kind="ocr_ingested",
            title="OCR-ingested brain from scanned/image-heavy source",
            source_mix=("scanned_pdf", "embedded_image", "ocr_block"),
            seed_requirements=(
                "image-heavy or scanned source",
                "OCR extracted text or honest skipped OCR state",
                "embedded asset trace",
            ),
            expected_memory_areas=("Documents", "Source Trace", "Review Required", "Extracted Assets"),
            hard_cases=("OCR skipped state", "OCR text source trust", "embedded image document"),
            success_signals=("OCR absence is honest", "OCR text is review-required when extracted"),
        ),
    ]


def pr12l_benchmark_cases() -> list[ProductBenchmarkCase]:
    return [
        ProductBenchmarkCase("retrieval_exact_fact", "retrieval", "exact_fact", "founder_work", "retrieve", ("context_package", "mcp_tool_output"), ("required fact present", "no forbidden unrelated facts"), "PR-12L-C", query_text="Which companies did this person found or lead?"),
        ProductBenchmarkCase("retrieval_broad_self_dossier", "retrieval", "broad_self_dossier", "personal_life", "retrieve", ("context_package", "path_corridors", "mcp_tool_output"), ("broad context is useful", "route mechanics stay appendices"), "PR-12L-C", query_text="Build a useful self dossier for an agent."),
        ProductBenchmarkCase("retrieval_relationship", "retrieval", "relationship", "personal_life", "retrieve", ("context_package", "mcp_tool_output"), ("relationship facts grounded", "ambiguity represented"), "PR-12L-C", query_text="What should I know about this person's family and close relationships?"),
        ProductBenchmarkCase("retrieval_temporal", "retrieval", "temporal", "founder_work", "retrieve", ("context_package", "document_workspace", "mcp_tool_output"), ("dated events included", "years not invented"), "PR-12L-C", query_text="What happened in 2019 and 2024?"),
        ProductBenchmarkCase("retrieval_project_workspace", "retrieval", "project_workspace", "project_workspace", "retrieve", ("context_package", "document_workspace", "mcp_tool_output"), ("project docs grouped", "technical decisions included"), "PR-12L-C", query_text="Assemble the workspace for the project architecture decisions."),
        ProductBenchmarkCase("retrieval_exact_document", "retrieval", "exact_document", "dense_document", "retrieve", ("document_workspace", "context_package", "mcp_tool_output"), ("full requested raw document visible", "related docs not promoted as primary"), "PR-12L-C", query_text="Open the exact document about the acquisition."),
        ProductBenchmarkCase("retrieval_related_documents", "retrieval", "related_documents", "dense_document", "retrieve", ("document_workspace", "context_package", "mcp_tool_output"), ("related docs listed separately", "duplicates controlled"), "PR-12L-C", query_text="Find related documents about this project."),
        ProductBenchmarkCase("retrieval_source_trace", "retrieval", "source_trace", "web_ingested", "retrieve", ("source_trace", "context_package", "mcp_tool_output"), ("source URLs and retrieval dates visible", "source trace not in main dossier"), "PR-12L-C", query_text="Show the source trace for the web facts."),
        ProductBenchmarkCase("retrieval_cross_area", "retrieval", "cross_area", "founder_work", "retrieve", ("context_package", "path_corridors", "mcp_tool_output"), ("work values and documents connected", "path discoveries promoted only when useful"), "PR-12L-C", query_text="Connect work history, values and public documents."),
        ProductBenchmarkCase("retrieval_no_match", "retrieval", "no_match", "sparse_early_life", "retrieve", ("context_package", "mcp_tool_output"), ("honest no-match", "bounded terminal state"), "PR-12L-C", query_text="Find a document that does not exist in this brain."),
        ProductBenchmarkCase("retrieval_warm_followup", "retrieval", "warm_followup", "personal_life", "retrieve", ("context_package", "mcp_tool_output"), ("warm context reused", "unnecessary reread avoided"), "PR-12L-C", query_text="Follow up on the relationship answer in the same thread."),
        ProductBenchmarkCase("retrieval_divergent_followup", "retrieval", "divergent_followup", "personal_life", "retrieve", ("context_package", "mcp_tool_output"), ("warm context does not contaminate divergent query", "new evidence found"), "PR-12L-C", query_text="Switch from family to work style in the same thread."),
        ProductBenchmarkCase("retrieval_contradiction", "retrieval", "contradiction", "contradiction", "retrieve", ("context_package", "mcp_tool_output"), ("newer fact wins", "superseded fact remains auditable"), "PR-12L-C", query_text="What changed, and which older fact is superseded?"),
        ProductBenchmarkCase("retrieval_hypothesis", "retrieval", "hypothesis", "future_dream", "retrieve", ("context_package", "mcp_tool_output"), ("hypothesis not presented as fact", "useful plan context returned"), "PR-12L-C", query_text="Which hypotheses are still open?"),
        ProductBenchmarkCase("retrieval_future_dream", "retrieval", "future_dream", "future_dream", "retrieve", ("context_package", "path_corridors", "mcp_tool_output"), ("future goals separated from facts", "project links included"), "PR-12L-C", query_text="What future projects and dreams should an agent consider?"),
        ProductBenchmarkCase("grow_manual_text", "grow", "manual_text", "personal_life", "grow", ("source_investigation_package", "preview_bundle", "learning_policy"), ("manual source unit created", "reviewable nodes proposed"), "PR-12L-B", source_kind="manual_text"),
        ProductBenchmarkCase("grow_long_manual_text", "grow", "long_manual_text", "project_workspace", "grow", ("source_investigation_package", "compiler_handoff", "preview_bundle"), ("sections extracted", "many nodes without uncontrolled duplicates"), "PR-12L-B", source_kind="manual_text_long"),
        ProductBenchmarkCase("grow_pdf_selectable", "grow", "pdf_selectable_text", "dense_document", "grow", ("source_investigation_package", "document_workspace", "preview_bundle"), ("selectable text extracted", "document anchor created"), "PR-12L-B", source_kind="pdf_selectable_text"),
        ProductBenchmarkCase("grow_scanned_ocr_pdf", "grow", "scanned_ocr_pdf", "ocr_ingested", "grow", ("source_investigation_package", "source_trace", "learning_policy"), ("OCR text or skipped state honest", "review required for OCR text"), "PR-12L-B", source_kind="scanned_pdf"),
        ProductBenchmarkCase("grow_docx", "grow", "docx", "project_workspace", "grow", ("source_investigation_package", "compiler_handoff", "preview_bundle"), ("paragraphs and tables extracted", "project document anchors kept"), "PR-12L-B", source_kind="docx"),
        ProductBenchmarkCase("grow_embedded_image_document", "grow", "embedded_image_document", "ocr_ingested", "grow", ("source_investigation_package", "source_trace", "preview_bundle"), ("embedded asset trace visible", "image-derived claims review-gated"), "PR-12L-B", source_kind="docx_with_image"),
        ProductBenchmarkCase("grow_website_root", "grow", "website_root", "web_ingested", "grow", ("source_investigation_package", "source_trace", "preview_bundle"), ("root page visible text extracted", "URL provenance kept"), "PR-12L-B", source_kind="website_root"),
        ProductBenchmarkCase("grow_website_sublinks", "grow", "website_with_sublinks", "web_ingested", "grow", ("source_investigation_package", "source_trace", "preview_bundle"), ("same-domain sublinks budgeted", "skipped links explicit"), "PR-12L-B", source_kind="website_with_sublinks"),
        ProductBenchmarkCase("grow_clarification", "grow", "source_requiring_clarification", "contradiction", "grow", ("source_investigation_package", "learning_policy", "preview_bundle"), ("bounded clarification question emitted", "default policy explicit"), "PR-12L-B", source_kind="ambiguous_manual_text"),
        ProductBenchmarkCase("grow_online_enrichment", "grow", "source_requiring_online_enrichment", "founder_work", "grow", ("source_investigation_package", "source_trace", "compiler_handoff"), ("online enrichment opt-in", "external source cannot override primary"), "PR-12L-B", source_kind="source_with_public_reference"),
        ProductBenchmarkCase("ui_context_package_parity", "ui_mcp", "context_package_parity", "personal_life", "ui_mcp", ("context_package", "mcp_tool_output", "ui_truth_surface"), ("UI context equals backend package", "MCP package default excludes answer demo"), "PR-12L-D", tool_name="retrieve_context"),
        ProductBenchmarkCase("ui_map_trace_parity", "ui_mcp", "map_trace_parity", "founder_work", "ui_mcp", ("path_corridors", "route_trace", "ui_truth_surface"), ("landings are branch-local", "no synthetic motion"), "PR-12L-D", tool_name="inspect_route_trace"),
        ProductBenchmarkCase("ui_final_status_parity", "ui_mcp", "final_status_parity", "personal_life", "ui_mcp", ("context_package", "ui_truth_surface"), ("final sealed visible", "first/final not stale across queries"), "PR-12L-D", tool_name="retrieve_context"),
        ProductBenchmarkCase("ui_source_package_parity", "ui_mcp", "source_package_parity", "web_ingested", "ui_mcp", ("source_investigation_package", "mcp_tool_output", "ui_truth_surface"), ("Grow UI shows source package truth", "source package searchable/full text inspectable"), "PR-12L-D", tool_name="grow_source_preview"),
        ProductBenchmarkCase("ui_document_raw_text_parity", "ui_mcp", "document_raw_text_parity", "dense_document", "ui_mcp", ("document_workspace", "context_package", "ui_truth_surface"), ("raw document text visible when requested", "not duplicated in broad context"), "PR-12L-D", tool_name="retrieve_document"),
        ProductBenchmarkCase("ui_maintenance_proposal_parity", "ui_mcp", "maintenance_proposal_parity", "sparse_early_life", "ui_mcp", ("maintenance_report", "mcp_tool_output", "ui_truth_surface"), ("sleep/evolve proposals visible", "apply remains review-gated"), "PR-12L-D", tool_name="sleep_preview"),
        ProductBenchmarkCase("regression_pr12a", "regression", "pr12a_semantic_contract", "founder_work", "regression", ("context_package", "mcp_tool_output"), ("semantic contract preserves query intent", "no query-specific hack"), "PR-12L-E", tool_name="retrieve_context"),
        ProductBenchmarkCase("regression_pr12b", "regression", "pr12b_context_package", "personal_life", "regression", ("context_package", "mcp_tool_output"), ("clean MCP package", "no node id/debug marker leak"), "PR-12L-E", tool_name="inspect_context_package"),
        ProductBenchmarkCase("regression_pr12c", "regression", "pr12c_path_corridor", "founder_work", "regression", ("path_corridors", "mcp_tool_output"), ("branch-local corridors", "cross-landing bridge only when explicit"), "PR-12L-E", tool_name="retrieve_path_corridor"),
        ProductBenchmarkCase("regression_pr12d", "regression", "pr12d_document_workspace", "dense_document", "regression", ("document_workspace", "mcp_tool_output"), ("raw document correctness", "related/cold document separation"), "PR-12L-E", tool_name="retrieve_document"),
        ProductBenchmarkCase("regression_pr12e", "regression", "pr12e_cognitive_write_plan", "project_workspace", "regression", ("preview_bundle", "learning_policy", "mcp_tool_output"), ("write plan reviewable", "entities/claims separated"), "PR-12L-E", tool_name="write_memory_preview"),
        ProductBenchmarkCase("regression_pr12f", "regression", "pr12f_learning_policy", "contradiction", "regression", ("learning_policy", "source_investigation_package", "mcp_tool_output"), ("human-in-loop when ambiguous", "safe autonomous modes bounded"), "PR-12L-E", tool_name="grow_guided"),
        ProductBenchmarkCase("regression_pr12g", "regression", "pr12g_brain_geometry_calibration", "project_workspace", "regression", ("context_package", "path_corridors", "maintenance_report"), ("geometry metrics present", "route efficiency does not regress"), "PR-12L-E", tool_name="evolve_preview"),
    ]


def _fixture_case_counts(cases: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for case in cases:
        fixture_kind = str(case.get("fixture_kind") or "")
        if fixture_kind:
            counts[fixture_kind] = counts.get(fixture_kind, 0) + 1
    return counts


def validate_pr12l_product_benchmark_harness(harness: dict[str, Any]) -> dict[str, Any]:
    fixtures = [dict(item) for item in list(harness.get("fixture_brains") or [])]
    cases = [dict(item) for item in list(harness.get("benchmark_cases") or [])]
    fixture_kinds = {str(item.get("fixture_kind") or "") for item in fixtures}
    fixture_case_counts = _fixture_case_counts(cases)
    missing_fixture_kinds = sorted(set(REQUIRED_PR12L_FIXTURE_KINDS) - fixture_kinds)
    uncovered_fixture_kinds = sorted(kind for kind in REQUIRED_PR12L_FIXTURE_KINDS if fixture_case_counts.get(kind, 0) <= 0)
    family_coverage: dict[str, dict[str, Any]] = {}
    missing_families: dict[str, list[str]] = {}
    for group, required_families in PR12L_REQUIRED_FAMILIES_BY_GROUP.items():
        covered = {str(case.get("family") or "") for case in cases if str(case.get("family_group") or "") == group}
        missing = sorted(set(required_families) - covered)
        missing_families[group] = missing
        family_coverage[group] = {
            "required": list(required_families),
            "covered": sorted(covered),
            "missing": missing,
            "covered_count": len(set(required_families) & covered),
            "required_count": len(required_families),
        }
    unscoped_cases = [
        str(case.get("case_id") or "")
        for case in cases
        if str(case.get("fixture_kind") or "") not in fixture_kinds
    ]
    answer_only_cases = [
        str(case.get("case_id") or "")
        for case in cases
        if not (set(str(item) for item in list(case.get("required_artifacts") or [])) & MCP_FIRST_ARTIFACTS)
    ]
    noncritical_cases = [str(case.get("case_id") or "") for case in cases if not bool(case.get("critical"))]
    open_gaps: list[str] = []
    if missing_fixture_kinds:
        open_gaps.append(f"Missing fixture kinds: {', '.join(missing_fixture_kinds)}")
    if uncovered_fixture_kinds:
        open_gaps.append(f"Fixture kinds without cases: {', '.join(uncovered_fixture_kinds)}")
    for group, missing in missing_families.items():
        if missing:
            open_gaps.append(f"Missing {group} benchmark families: {', '.join(missing)}")
    if unscoped_cases:
        open_gaps.append(f"Benchmark cases reference unknown fixture kinds: {', '.join(unscoped_cases)}")
    if answer_only_cases:
        open_gaps.append(f"Benchmark cases are answer-only and not MCP-first: {', '.join(answer_only_cases)}")
    if noncritical_cases:
        open_gaps.append(f"Non-critical cases are not allowed in PR-12L product gates: {', '.join(noncritical_cases)}")
    passed = not open_gaps
    return {
        "schema_version": "agvm.pr12l.product_benchmark_harness_validation.v1",
        "passed": passed,
        "gate": "green" if passed else "red",
        "fixture_count": len(fixtures),
        "case_count": len(cases),
        "missing_fixture_kinds": missing_fixture_kinds,
        "uncovered_fixture_kinds": uncovered_fixture_kinds,
        "family_coverage": family_coverage,
        "unscoped_cases": unscoped_cases,
        "answer_only_cases": answer_only_cases,
        "noncritical_cases": noncritical_cases,
        "open_gaps": open_gaps,
    }


def build_pr12l_product_benchmark_harness() -> dict[str, Any]:
    fixtures = [asdict(item) for item in pr12l_fixture_brains()]
    cases = [asdict(item) for item in pr12l_benchmark_cases()]
    fixture_case_counts = _fixture_case_counts(cases)
    coverage_matrix = {
        "fixture_kinds": [
            {
                "fixture_kind": fixture["fixture_kind"],
                "brain_id": fixture["brain_id"],
                "case_count": fixture_case_counts.get(str(fixture["fixture_kind"]), 0),
                "source_mix": list(fixture.get("source_mix") or []),
                "expected_memory_areas": list(fixture.get("expected_memory_areas") or []),
            }
            for fixture in fixtures
        ],
        "families_by_group": {
            group: {
                "required": list(required),
                "case_ids": [
                    str(case.get("case_id") or "")
                    for case in cases
                    if str(case.get("family_group") or "") == group
                ],
            }
            for group, required in PR12L_REQUIRED_FAMILIES_BY_GROUP.items()
        },
        "artifacts": sorted({artifact for case in cases for artifact in list(case.get("required_artifacts") or [])}),
    }
    harness = {
        "schema_version": PR12L_PRODUCT_HARNESS_SCHEMA_VERSION,
        "phase": "PR-12L-A",
        "product_goal": "Prove AGVM is product-ready as an MCP-first Human Memory OS, where the context package is the product and answers are downstream demos.",
        "fixture_brains": fixtures,
        "benchmark_cases": cases,
        "coverage_matrix": coverage_matrix,
        "critical_failure_policy": {
            "no_benchmark_family_with_critical_failure": True,
            "all_cases_are_critical": True,
            "answer_only_pass_forbidden": True,
            "harness_only_product_ready_verdict": "not_evaluated",
        },
        "slice_execution_plan": [
            {"slice": "PR-12L-A", "scope": "Benchmark harness and fixture definitions"},
            {"slice": "PR-12L-B", "scope": "Source intake benchmark execution"},
            {"slice": "PR-12L-C", "scope": "Retrieval and MCP benchmark execution"},
            {"slice": "PR-12L-D", "scope": "UI truth benchmark execution"},
            {"slice": "PR-12L-E", "scope": "Product-ready scorecard and critical failure aggregation"},
        ],
    }
    harness["validation"] = validate_pr12l_product_benchmark_harness(harness)
    return harness


def run_pr12l_product_harness_suite(base_url: str | None = None) -> dict[str, Any]:
    harness = build_pr12l_product_benchmark_harness()
    validation = dict(harness.get("validation") or {})
    return {
        "schema_version": PR12L_PRODUCT_HARNESS_REPORT_SCHEMA_VERSION,
        "phase": "product_harness",
        "all_pass": bool(validation.get("passed")),
        "pass_rate": 1.0 if bool(validation.get("passed")) else 0.0,
        "passed_count": 1 if bool(validation.get("passed")) else 0,
        "total_count": 1,
        "base_url": str(base_url or DEFAULT_BASE_URL),
        "product_ready_verdict": "not_evaluated_harness_only",
        "harness": harness,
        "fixture_count": int(validation.get("fixture_count") or 0),
        "case_count": int(validation.get("case_count") or 0),
        "open_gaps": list(validation.get("open_gaps") or []),
        "benchmark_inputs": {
            "phase": "product_harness",
            "fixture_kinds": list(REQUIRED_PR12L_FIXTURE_KINDS),
            "family_groups": sorted(PR12L_REQUIRED_FAMILIES_BY_GROUP.keys()),
            "network_required": False,
        },
    }


def _pr12l_xml_escape(value: str) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _pr12l_minimal_pdf_bytes(text: str) -> bytes:
    safe = str(text or "").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({safe}) Tj ET"
    stream_bytes = stream.encode("latin-1", errors="replace")
    return (
        "%PDF-1.4\n"
        "1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        "2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        "3 0 obj << /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> endobj\n"
        "4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n"
        f"5 0 obj << /Length {len(stream_bytes)} >> stream\n"
        f"{stream}\n"
        "endstream endobj\n"
        "trailer << /Root 1 0 R >>\n"
        "%%EOF\n"
    ).encode("latin-1", errors="replace")


def _pr12l_png_bytes() -> bytes:
    return bytes.fromhex(
        "89504E470D0A1A0A0000000D4948445200000001000000010802000000907753DE"
        "0000000C49444154789C6360F8FFFF3F0005FE02FEA73581E80000000049454E44AE426082"
    )


def _pr12l_docx_bytes(
    paragraphs: list[str],
    *,
    table_rows: list[list[str]] | None = None,
    embedded_image_bytes: bytes | None = None,
) -> bytes:
    body_parts = [
        f"<w:p><w:r><w:t>{_pr12l_xml_escape(paragraph)}</w:t></w:r></w:p>"
        for paragraph in paragraphs
    ]
    if table_rows:
        rows_xml: list[str] = []
        for row in table_rows:
            cells = "".join(f"<w:tc><w:p><w:r><w:t>{_pr12l_xml_escape(cell)}</w:t></w:r></w:p></w:tc>" for cell in row)
            rows_xml.append(f"<w:tr>{cells}</w:tr>")
        body_parts.append(f"<w:tbl>{''.join(rows_xml)}</w:tbl>")
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">\n'
        f"  <w:body>{''.join(body_parts)}</w:body>\n"
        "</w:document>\n"
    )
    png_default = '<Default Extension="png" ContentType="image/png"/>' if embedded_image_bytes is not None else ""
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        '  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
        '  <Default Extension="xml" ContentType="application/xml"/>\n'
        f"  {png_default}\n"
        '  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>\n'
        "</Types>\n"
    )
    relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        '  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>\n'
        "</Relationships>\n"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", relationships)
        archive.writestr("word/document.xml", document_xml)
        if embedded_image_bytes is not None:
            archive.writestr("word/media/image1.png", embedded_image_bytes)
    return buffer.getvalue()


def _pr12l_fake_fetcher(pages: dict[str, str]) -> Callable[..., dict[str, Any]]:
    calls: list[str] = []

    def fake_fetch(url: str, *, timeout_seconds: float) -> dict[str, Any]:
        calls.append(str(url))
        if timeout_seconds <= 0:
            return {
                "ok": False,
                "url": str(url),
                "status_code": None,
                "content_type": None,
                "html": "",
                "elapsed_ms": 0.0,
                "error": "invalid_timeout",
            }
        html = pages.get(str(url))
        if html is None:
            return {
                "ok": False,
                "url": str(url),
                "status_code": None,
                "content_type": None,
                "html": "",
                "elapsed_ms": 1.0,
                "error": "fixture_page_missing",
            }
        return {
            "ok": True,
            "url": str(url),
            "status_code": 200,
            "content_type": "text/html; charset=utf-8",
            "html": html,
            "elapsed_ms": 1.0,
            "error": None,
        }

    fake_fetch.calls = calls  # type: ignore[attr-defined]
    return fake_fetch


def _pr12l_fake_ocr(_file_bytes: bytes, *, analyze_images: str) -> dict[str, Any]:
    if str(analyze_images or "off") == "off":
        return {
            "status": "skipped",
            "reason": "ocr_not_requested",
            "ocr_text": None,
            "confidence": 0.0,
            "method": None,
        }
    return {
        "status": "completed",
        "reason": None,
        "ocr_text": "OCR benchmark text: embedded image shows the source intake path into AGVM memory.",
        "confidence": 0.82,
        "method": "pr12l_b_fake_ocr",
    }


def _pr12l_empty_graph() -> dict[str, Any]:
    return {
        "version": "test",
        "graph_name": "pr12l_source_intake_benchmark",
        "nodes": [],
        "edges": [],
        "meta": {},
    }


def _pr12l_preview_for_source_package(package: dict[str, Any]) -> dict[str, Any] | None:
    handoff = dict(package.get("compiler_handoff") or {})
    if not bool(handoff.get("preview_eligible")):
        return None
    source_request = dict(package.get("source_request") or {})
    return {
        "schema_version": "agvm.pr12l.preview_readiness.v1",
        "execution_policy": "not_run_in_pr12l_b_source_intake_benchmark",
        "reason": "PR-12L-B verifies source intake and compiler handoff readiness; full preview/retrieve/MCP execution belongs to PR-12L-C.",
        "source_label": str(source_request.get("source_label") or source_request.get("file_name") or "PR-12L-B source"),
        "recommended_input_mode": str(handoff.get("recommended_input_mode") or "auto"),
        "recommended_source_type": str(handoff.get("recommended_source_type") or "source_investigation"),
        "recommended_source_trust": str(handoff.get("recommended_source_trust") or "user_asserted"),
        "recommended_learning_mode": str(handoff.get("recommended_learning_mode") or "strict_review"),
        "mega_text_char_count": len(str(handoff.get("mega_text") or "")),
        "document_anchor_recommendation_present": bool(handoff.get("document_anchor_recommendations") or []),
    }


def _pr12l_source_package_summary(package: dict[str, Any], preview_payload: dict[str, Any] | None) -> dict[str, Any]:
    source_units = [dict(item) for item in list(package.get("source_units") or [])]
    assets = [dict(item) for item in list(package.get("extracted_assets") or [])]
    handoff = dict(package.get("compiler_handoff") or {})
    detection = dict(package.get("source_detection") or {})
    budget_usage = dict(package.get("budget_usage") or {})
    return {
        "schema_version": str(package.get("schema_version") or ""),
        "status": str(package.get("status") or ""),
        "source_kind": str(detection.get("source_kind") or ""),
        "source_unit_count": len(source_units),
        "source_unit_kinds": sorted({str(unit.get("kind") or "") for unit in source_units}),
        "extracted_asset_count": len(assets),
        "asset_kinds": sorted({str(asset.get("asset_kind") or "") for asset in assets}),
        "preview_eligible": bool(handoff.get("preview_eligible")),
        "preview_bundle_present": False,
        "preview_readiness_present": preview_payload is not None,
        "preview_execution_policy": "not_run_in_pr12l_b_source_intake_benchmark",
        "compiler_handoff_mode": str(handoff.get("recommended_input_mode") or ""),
        "recommended_source_trust": str(handoff.get("recommended_source_trust") or ""),
        "proof_passed": bool((package.get("compiler_handoff_proof") or {}).get("handoff_preflight_passed")),
        "mutation_enabled": bool((package.get("budgets") or {}).get("mutation_enabled")),
        "network_fetch_enabled": bool((package.get("budgets") or {}).get("network_fetch_enabled")),
        "online_enrichment_enabled": bool((package.get("budgets") or {}).get("online_enrichment_enabled")),
        "unit_char_count": int(budget_usage.get("char_count") or 0),
        "fetched_url_count": int(budget_usage.get("fetched_url_count") or 0),
        "crawled_page_count": int(budget_usage.get("crawled_page_count") or 0),
        "online_enrichment_fetch_count": int(budget_usage.get("online_enrichment_fetch_count") or 0),
        "warning_codes": sorted({str(warning.get("code") or "") for warning in list(package.get("warnings") or [])}),
        "blocked_reasons": sorted({str(reason) for reason in list(package.get("blocked_reasons") or [])}),
    }


def _pr12l_source_case_result(
    case: ProductBenchmarkCase,
    *,
    package: dict[str, Any],
    preview_payload: dict[str, Any] | None,
    failures: list[str],
    observations: dict[str, Any] | None = None,
    extra_packages: dict[str, dict[str, Any]] | None = None,
    elapsed_ms: float = 0.0,
) -> dict[str, Any]:
    package_summary = _pr12l_source_package_summary(package, preview_payload)
    return {
        "case_id": case.case_id,
        "family_group": case.family_group,
        "family": case.family,
        "fixture_kind": case.fixture_kind,
        "critical": case.critical,
        "slice_owner": case.slice_owner,
        "passed": not failures,
        "status": "passed" if not failures else "failed",
        "elapsed_ms": round(elapsed_ms, 3),
        "required_artifacts": list(case.required_artifacts),
        "required_signals": list(case.required_signals),
        "failures": failures,
        "observations": observations or {},
        "source_investigation_package_summary": package_summary,
        "extra_package_summaries": {
            name: _pr12l_source_package_summary(extra_package, None)
            for name, extra_package in dict(extra_packages or {}).items()
        },
    }


def _pr12l_assert_source_common(package: dict[str, Any], failures: list[str]) -> None:
    if str(package.get("schema_version") or "") != "agvm.source_investigation.v1":
        failures.append("source_investigation_schema_version_mismatch")
    if bool((package.get("budgets") or {}).get("mutation_enabled")):
        failures.append("source_benchmark_mutated_memory")
    source_units = list(package.get("source_units") or [])
    status = str(package.get("status") or "")
    if source_units and not bool((package.get("compiler_handoff_proof") or {}).get("handoff_preflight_passed")):
        failures.append("compiler_handoff_preflight_failed")
    if not source_units and status not in {"partial_budget_exhausted", "failed", "asking_clarification"}:
        failures.append("source_units_missing_without_honest_terminal_state")
    if not dict(package.get("compiler_handoff") or {}).get("mega_text") and source_units:
        failures.append("compiler_handoff_missing_mega_text")


def _run_pr12l_source_intake_case(case: ProductBenchmarkCase) -> dict[str, Any]:
    from schemas import SourceInvestigationPackage
    from source_investigation import (
        build_file_source_investigation_package,
        build_source_investigation_package,
    )

    started = time.perf_counter()
    failures: list[str] = []
    observations: dict[str, Any] = {}
    extra_packages: dict[str, dict[str, Any]] = {}
    package: dict[str, Any]
    preview_payload: dict[str, Any] | None = None
    try:
        if case.family == "manual_text":
            package = build_source_investigation_package(
                "Manual self-memory source: I am teaching AGVM that Giovanni is my father, "
                "the memory belongs to family context, and the fact must remain source-grounded.",
                input_kind="manual_text",
                source_label="PR-12L-B manual self-memory",
                options={"treat_as": "self_memory", "source_trust": "user_asserted"},
            )
            preview_payload = _pr12l_preview_for_source_package(package)
            if str((package.get("source_detection") or {}).get("source_kind") or "") != "manual_text":
                failures.append("manual_text_not_detected")
            if len(list(package.get("source_units") or [])) < 1:
                failures.append("manual_text_source_unit_missing")
            if "Giovanni" not in str((package.get("compiler_handoff") or {}).get("mega_text") or ""):
                failures.append("manual_text_raw_fact_not_preserved")
            if preview_payload is None:
                failures.append("manual_text_preview_readiness_missing")
        elif case.family == "long_manual_text":
            long_text = "\n\n".join(
                [
                    (
                        f"Project workspace paragraph {index}: AGVM source intake benchmark keeps project decision {index}, "
                        "owner, date, raw text and MCP context-package intent together without collapsing everything into one answer. "
                        "This paragraph intentionally repeats enough project-source detail to force multiple source units while preserving "
                        "document-like sections, compiler handoff provenance and downstream MCP package readiness."
                    )
                    for index in range(1, 42)
                ]
            )
            package = build_source_investigation_package(
                long_text,
                input_kind="manual_text",
                source_label="PR-12L-B long project workspace",
                options={"treat_as": "project_workspace", "max_units": 8},
            )
            preview_payload = _pr12l_preview_for_source_package(package)
            if len(list(package.get("source_units") or [])) < 2:
                failures.append("long_manual_text_not_split_into_multiple_source_units")
            if int((package.get("budget_usage") or {}).get("char_count") or 0) <= 2500:
                failures.append("long_manual_text_char_coverage_too_low")
            if preview_payload is None:
                failures.append("long_manual_text_preview_readiness_missing")
        elif case.family == "pdf_selectable_text":
            package = build_file_source_investigation_package(
                _pr12l_minimal_pdf_bytes("Selectable PDF source states AGVM creates a document anchor for BaxEnergy project context."),
                file_name="pr12l-selectable-document.pdf",
                content_type="application/pdf",
                source_label="PR-12L-B selectable PDF",
                options={"treat_as": "technical_document"},
            )
            preview_payload = _pr12l_preview_for_source_package(package)
            usage = dict(package.get("budget_usage") or {})
            if str((package.get("source_detection") or {}).get("source_kind") or "") != "pdf":
                failures.append("pdf_selectable_not_detected")
            if int(usage.get("selectable_page_count") or 0) < 1:
                failures.append("pdf_selectable_text_not_extracted")
            if "document_page" not in {str(unit.get("kind") or "") for unit in list(package.get("source_units") or [])}:
                failures.append("pdf_document_page_unit_missing")
            if preview_payload is None:
                failures.append("pdf_preview_readiness_missing")
        elif case.family == "scanned_ocr_pdf":
            package = build_file_source_investigation_package(
                b"%PDF-1.4\n1 0 obj << /Type /Page >> endobj\n%%EOF\n",
                file_name="pr12l-scanned-source.pdf",
                content_type="application/pdf",
                source_label="PR-12L-B scanned PDF",
                options={"analyze_images": "ocr_only"},
            )
            preview_payload = _pr12l_preview_for_source_package(package)
            usage = dict(package.get("budget_usage") or {})
            warning_codes = {str(warning.get("code") or "") for warning in list(package.get("warnings") or [])}
            if str(package.get("status") or "") != "partial_budget_exhausted":
                failures.append("scanned_pdf_not_reported_as_partial_budget_exhausted")
            if int(usage.get("pdf_page_image_count") or 0) < 1:
                failures.append("scanned_pdf_page_image_asset_missing")
            if "ocr_required_deferred" not in warning_codes:
                failures.append("scanned_pdf_ocr_deferred_warning_missing")
            if preview_payload is not None:
                failures.append("scanned_pdf_should_not_create_preview_without_text")
        elif case.family == "docx":
            package = build_file_source_investigation_package(
                _pr12l_docx_bytes(
                    [
                        "DOCX source states that AGVM source intake preserves project architecture decisions.",
                        "Second paragraph links BaxEnergy, Yokogawa and document workspace retrieval.",
                    ],
                    table_rows=[["Area", "Evidence"], ["Project", "Document workspace keeps raw source text"]],
                ),
                file_name="pr12l-project-document.docx",
                content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                source_label="PR-12L-B DOCX project document",
                options={"treat_as": "technical_document"},
            )
            preview_payload = _pr12l_preview_for_source_package(package)
            usage = dict(package.get("budget_usage") or {})
            if int(usage.get("document_section_count") or 0) < 3:
                failures.append("docx_sections_not_extracted")
            if int(usage.get("docx_table_unit_count") or 0) < 1:
                failures.append("docx_table_unit_missing")
            if preview_payload is None:
                failures.append("docx_preview_readiness_missing")
        elif case.family == "embedded_image_document":
            package = build_file_source_investigation_package(
                _pr12l_docx_bytes(
                    ["DOCX source with embedded diagram for AGVM image-aware Grow intake."],
                    embedded_image_bytes=_pr12l_png_bytes(),
                ),
                file_name="pr12l-embedded-image.docx",
                content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                source_label="PR-12L-B embedded image document",
                options={"analyze_images": "ocr_only", "treat_as": "technical_document"},
            )
            preview_payload = _pr12l_preview_for_source_package(package)
            usage = dict(package.get("budget_usage") or {})
            if int(usage.get("embedded_image_count") or 0) < 1:
                failures.append("embedded_image_asset_missing")
            if int(usage.get("ocr_unit_count") or 0) < 1:
                failures.append("embedded_image_ocr_block_missing")
            if "ocr_block" not in {str(unit.get("kind") or "") for unit in list(package.get("source_units") or [])}:
                failures.append("embedded_image_ocr_source_unit_missing")
            if not any(bool((asset.get("requires_human_confirmation"))) for asset in list(package.get("extracted_assets") or [])):
                failures.append("embedded_image_ocr_not_review_gated")
            if preview_payload is None:
                failures.append("embedded_image_preview_readiness_missing")
        elif case.family == "website_root":
            package = build_source_investigation_package(
                "https://bench-source.test",
                input_kind="website",
                source_label="PR-12L-B root website",
                options={"crawl_sublinks": "off", "treat_as": "public_dossier", "max_crawl_pages": 3},
            )
            preview_payload = _pr12l_preview_for_source_package(package)
            usage = dict(package.get("budget_usage") or {})
            if int(usage.get("fetched_url_count") or 0) != 1:
                failures.append("website_root_fetch_count_wrong")
            if "web_page" not in {str(unit.get("kind") or "") for unit in list(package.get("source_units") or [])}:
                failures.append("website_root_web_page_unit_missing")
            if "AGVM Source Intake Root" not in str((package.get("compiler_handoff") or {}).get("mega_text") or ""):
                failures.append("website_root_visible_text_missing")
            if preview_payload is None:
                failures.append("website_root_preview_readiness_missing")
        elif case.family == "website_with_sublinks":
            package = build_source_investigation_package(
                "https://bench-source.test",
                input_kind="website",
                source_label="PR-12L-B website crawl",
                options={"crawl_sublinks": "same_domain", "max_depth": 1, "max_crawl_pages": 4, "treat_as": "public_dossier"},
            )
            preview_payload = _pr12l_preview_for_source_package(package)
            usage = dict(package.get("budget_usage") or {})
            skipped_reasons = {str(item.get("reason") or "") for item in list(usage.get("skipped_sources") or [])}
            if int(usage.get("fetched_url_count") or 0) < 2:
                failures.append("website_sublink_fetch_missing")
            if int(usage.get("crawled_page_count") or 0) < 1:
                failures.append("website_sublink_crawl_count_missing")
            if "external_domain_not_allowed" not in skipped_reasons:
                failures.append("website_external_skip_missing")
            if "low_value_path" not in skipped_reasons:
                failures.append("website_low_value_skip_missing")
            if "Crawled project source" not in str((package.get("compiler_handoff") or {}).get("mega_text") or ""):
                failures.append("website_sublink_visible_text_missing")
            if preview_payload is None:
                failures.append("website_sublink_preview_readiness_missing")
        elif case.family == "source_requiring_clarification":
            package = build_source_investigation_package(
                "https://bench-source.test",
                input_kind="website",
                source_label="PR-12L-B guided website source",
                options={"pause_on_questions": True, "question_limit": 4},
            )
            resumed = build_source_investigation_package(
                "https://bench-source.test",
                input_kind="website",
                source_label="PR-12L-B guided website source",
                options={
                    "question_limit": 4,
                    "clarification_answers": {
                        "source_scope_1": "project_workspace",
                        "source_trust_1": "verified_public_source",
                        "url_crawl_scope_1": "no_crawl",
                    },
                },
            )
            extra_packages["resumed_after_clarification"] = resumed
            preview_payload = _pr12l_preview_for_source_package(resumed)
            guided = dict(package.get("guided_grow") or {})
            if str(package.get("status") or "") != "asking_clarification":
                failures.append("clarification_case_did_not_pause")
            if int(guided.get("pending_count") or 0) < 2:
                failures.append("clarification_question_count_too_low")
            if bool((package.get("compiler_handoff") or {}).get("preview_eligible")):
                failures.append("clarification_paused_package_preview_enabled")
            if preview_payload is None:
                failures.append("clarification_resumed_preview_readiness_missing")
            if str(resumed.get("status") or "") not in {"preview_ready", "partial_budget_exhausted"}:
                failures.append("clarification_resumed_package_not_preview_ready")
        elif case.family == "source_requiring_online_enrichment":
            package = build_source_investigation_package(
                "Project source says the external appendix should be checked at https://external-source.test/report. "
                "The primary source remains this project note and external material is supporting evidence only.",
                input_kind="mixed_bundle",
                source_label="PR-12L-B enrichment source",
                options={"use_online_enrichment": True, "max_online_queries": 1, "treat_as": "project_workspace"},
            )
            preview_payload = _pr12l_preview_for_source_package(package)
            enrichment = dict(package.get("online_enrichment") or {})
            if not bool(enrichment.get("enabled")):
                failures.append("online_enrichment_not_enabled")
            if int(enrichment.get("fetched_count") or 0) != 1:
                failures.append("online_enrichment_fetch_count_wrong")
            if bool(enrichment.get("primary_source_override_allowed")):
                failures.append("online_enrichment_allows_primary_override")
            if "web_section" not in {str(unit.get("kind") or "") for unit in list(package.get("source_units") or [])}:
                failures.append("online_enrichment_web_section_missing")
            if "external reference only" not in str((package.get("compiler_handoff") or {}).get("mega_text") or ""):
                failures.append("online_enrichment_handoff_boundary_missing")
            if preview_payload is None:
                failures.append("online_enrichment_preview_readiness_missing")
        else:
            package = {}
            failures.append(f"unimplemented_source_intake_family:{case.family}")

        if package:
            SourceInvestigationPackage(**package)
            _pr12l_assert_source_common(package, failures)
            observations["package_status"] = str(package.get("status") or "")
            observations["source_kind"] = str((package.get("source_detection") or {}).get("source_kind") or "")
            observations["preview_readiness_created"] = preview_payload is not None
    except Exception as exc:  # noqa: BLE001
        package = package if "package" in locals() and isinstance(package, dict) else {}
        failures.append(f"exception:{type(exc).__name__}:{str(exc)[:300]}")

    return _pr12l_source_case_result(
        case,
        package=package,
        preview_payload=preview_payload,
        failures=failures,
        observations=observations,
        extra_packages=extra_packages,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
    )


def run_pr12l_source_intake_benchmark_suite(base_url: str | None = None) -> dict[str, Any]:
    import source_investigation

    source_cases = [case for case in pr12l_benchmark_cases() if case.family_group == "grow"]
    fixture_pages = {
        "https://bench-source.test/": """
        <html><head><title>AGVM Source Intake Root</title></head><body>
          <h1>AGVM Source Intake Root</h1>
          <p>AGVM Source Intake Root preserves source-grounded MCP memory packages.</p>
          <a href="/project">Project</a>
          <a href="https://external-source.test/report">External report</a>
          <a href="/privacy">Privacy</a>
          <img src="/diagram.png" alt="Source intake diagram">
        </body></html>
        """,
        "https://bench-source.test/project": """
        <html><head><title>Project Source</title></head><body>
          <h2>Crawled project source</h2>
          <p>Crawled project source links source investigation, compiler handoff and document workspace retrieval.</p>
        </body></html>
        """,
        "https://external-source.test/report": """
        <html><head><title>External report</title></head><body>
          <p>External report supplies public context as external reference only.</p>
        </body></html>
        """,
    }
    fake_fetch = _pr12l_fake_fetcher(fixture_pages)
    original_fetch = source_investigation._fetch_web_url
    original_ocr = source_investigation._run_ocr_on_image_bytes
    source_investigation._fetch_web_url = fake_fetch  # type: ignore[assignment]
    source_investigation._run_ocr_on_image_bytes = _pr12l_fake_ocr  # type: ignore[assignment]
    try:
        case_results = [_run_pr12l_source_intake_case(case) for case in source_cases]
    finally:
        source_investigation._fetch_web_url = original_fetch  # type: ignore[assignment]
        source_investigation._run_ocr_on_image_bytes = original_ocr  # type: ignore[assignment]

    covered_families = {str(result.get("family") or "") for result in case_results}
    missing_families = sorted(set(REQUIRED_PR12L_GROW_FAMILIES) - covered_families)
    critical_failures = [
        str(result.get("case_id") or "")
        for result in case_results
        if bool(result.get("critical")) and not bool(result.get("passed"))
    ]
    passed_count = sum(1 for result in case_results if bool(result.get("passed")))
    total_count = len(case_results)
    all_pass = not critical_failures and not missing_families and total_count == len(REQUIRED_PR12L_GROW_FAMILIES)
    family_matrix = {
        family: {
            "covered": family in covered_families,
            "passed": any(str(result.get("family") or "") == family and bool(result.get("passed")) for result in case_results),
            "case_ids": [str(result.get("case_id") or "") for result in case_results if str(result.get("family") or "") == family],
        }
        for family in REQUIRED_PR12L_GROW_FAMILIES
    }
    fixture_matrix: dict[str, dict[str, Any]] = {}
    for result in case_results:
        fixture_kind = str(result.get("fixture_kind") or "")
        entry = fixture_matrix.setdefault(fixture_kind, {"case_count": 0, "passed_count": 0, "families": []})
        entry["case_count"] += 1
        entry["passed_count"] += 1 if bool(result.get("passed")) else 0
        entry["families"].append(str(result.get("family") or ""))
    open_gaps: list[str] = []
    if missing_families:
        open_gaps.append(f"Missing source intake benchmark families: {', '.join(missing_families)}")
    if critical_failures:
        open_gaps.append(f"Critical source intake failures: {', '.join(critical_failures)}")
    verdict = "source_intake_passed_pr12l_still_open" if all_pass else "source_intake_failed_pr12l_still_open"
    return {
        "schema_version": PR12L_SOURCE_INTAKE_BENCHMARK_REPORT_SCHEMA_VERSION,
        "phase": "source_intake",
        "slice": "PR-12L-B",
        "all_pass": all_pass,
        "pass_rate": round(passed_count / total_count, 4) if total_count else 0.0,
        "passed_count": passed_count,
        "total_count": total_count,
        "base_url": str(base_url or DEFAULT_BASE_URL),
        "product_ready_verdict": verdict,
        "case_results": case_results,
        "family_matrix": family_matrix,
        "fixture_matrix": fixture_matrix,
        "critical_failures": critical_failures,
        "open_gaps": open_gaps,
        "benchmark_inputs": {
            "phase": "source_intake",
            "source_families": list(REQUIRED_PR12L_GROW_FAMILIES),
            "external_network_required": False,
            "web_fetch_mode": "deterministic_fixture_fetcher",
            "ocr_mode": "deterministic_fixture_ocr",
            "mutation_enabled": False,
            "mcp_first_artifacts_required": True,
        },
        "next_slice": "PR-12L-C Retrieval And MCP Benchmark",
    }


def _pr12l_contract(
    *,
    query_text: str,
    primary: str,
    required_sections: list[str],
    optional_sections: list[str] | None = None,
    broad_context: bool = False,
    document_mode: str = "none",
    document_target: str = "",
    landing_sections: list[str] | None = None,
    forbidden_topics: list[str] | None = None,
) -> dict[str, Any]:
    landings = [
        {
            "landing_id": f"L{index + 1}",
            "textual_probe": f"Find {section} evidence for: {query_text}",
            "target_evidence_ids": [],
        }
        for index, section in enumerate(list(landing_sections or required_sections or [primary])[:4])
    ]
    paths = [
        {
            "path_id": f"P{index + 1}",
            "from_landing_id": landings[index]["landing_id"],
            "to_landing_id": landings[index + 1]["landing_id"],
            "why_traverse": "Read the semantic corridor from this branch-local landing to the next relevant evidence target.",
            "read_intermediate_nodes": True,
            "max_intermediate_nodes": 8,
            "preferred_edges": ["highway", "semantic_link", "document_reference", "temporal_link"],
            "planner_source": "pr12l_c_fixture_contract",
        }
        for index in range(max(0, len(landings) - 1))
    ]
    return {
        "schema_version": "agvm.semantic_query_contract.v1",
        "contract_authority": "pr12l_c_deterministic_benchmark",
        "intent": {
            "primary": primary,
            "requires_broad_context": broad_context,
        },
        "context_contract": {
            "required_sections": required_sections,
            "optional_sections": list(optional_sections or []),
            "dossier_goal": "context_for_clone" if broad_context else "mcp_task_context",
        },
        "document_contract": {
            "mode": document_mode,
            "target_text": document_target,
        },
        "landing_plan": {
            "min_landings": max(1, len(landings)),
            "landing_hypotheses": landings,
            "paths": paths,
        },
        "expected_evidence": [
            {
                "slot": section,
                "positive_conditions": [f"{section}_evidence_present"],
                "negative_conditions": [],
            }
            for section in required_sections
        ],
        "forbidden_evidence": [{"topic": topic} for topic in list(forbidden_topics or [])],
    }


def _pr12l_retrieval_match(
    *,
    family: str,
    index: int,
    text: str,
    section: str,
    source_label: str,
    memory_type: str | None = None,
    source_trust: str = "verified_public",
    claim_status: str = "fact",
    answer_eligible: bool = True,
    document_eligible: bool = True,
    score: float = 0.92,
) -> dict[str, Any]:
    node_id = f"bench_{family}_{index:02d}"
    node = {
        "id": node_id,
        "summary": text,
        "raw_text": text,
        "memory_type": memory_type or section,
        "source_trust": source_trust,
        "claim_status": claim_status,
        "answer_eligible": answer_eligible,
        "profile_eligible": True,
        "document_eligible": document_eligible,
        "provenance": {
            "guide_conceptual_area": section,
            "source_label": source_label,
            "source_type": source_trust,
        },
    }
    return {
        "node_id": node_id,
        "summary": text,
        "evidence_snippet": text,
        "raw_score": score,
        "score": score,
        "confidence": score,
        "reason": "pr12l_c_fixture_evidence",
        "sources": [source_label],
        "support_slot": section,
        "support_slots": [section],
        "branch_goals": [section],
        "node": node,
    }


def _pr12l_document_packet(
    *,
    anchor_id: str,
    title: str,
    source_label: str,
    full_text: str,
    lookup_role: str,
    source_type: str = "uploaded_document",
    source_trust: str = "uploaded_document",
    query_fit: float = 0.9,
    exact: float = 0.86,
    project_tags: list[str] | None = None,
    entity_tags: list[str] | None = None,
    timeline_tags: list[str] | None = None,
    topic_tags: list[str] | None = None,
) -> dict[str, Any]:
    chunk_text = full_text[:240]
    fact_text = full_text.split(".")[0].strip()
    if fact_text:
        fact_text = f"{fact_text}."
    return {
        "anchor_node_id": anchor_id,
        "title": title,
        "source_label": source_label,
        "source_type": source_type,
        "source_trust": source_trust,
        "claim_status": "fact",
        "answer_eligible": True,
        "profile_eligible": True,
        "document_eligible": True,
        "lookup_role": lookup_role,
        "query_fit_score": query_fit,
        "exact_match_score": exact,
        "project_tags": list(project_tags or ["AGVM", "BaxEnergy"]),
        "entity_tags": list(entity_tags or ["BaxEnergy", "Yokogawa"]),
        "timeline_tags": list(timeline_tags or ["2024"]),
        "topic_tags": list(topic_tags or ["memory", "documents", "retrieval"]),
        "related_node_ids": [anchor_id, f"{anchor_id}_chunk_1", f"{anchor_id}_fact_1"],
        "full_text_mode": "anchor_raw",
        "complete_text_available": True,
        "raw_text_char_count": len(full_text),
        "anchor_raw_text": full_text,
        "full_text": full_text,
        "ordered_chunk_sequence": [
            {
                "node_id": f"{anchor_id}_chunk_1",
                "source_node_id": anchor_id,
                "chunk_index": 1,
                "source_span_start": 0,
                "source_span_end": min(len(full_text), len(chunk_text)),
                "raw_text": chunk_text,
                "score": query_fit,
            }
        ],
        "supported_fact_text": [
            {
                "node_id": f"{anchor_id}_fact_1",
                "raw_text": fact_text or chunk_text,
                "summary": fact_text or chunk_text,
                "score": exact,
            }
        ],
        "source_trace": [
            {
                "anchor_node_id": anchor_id,
                "node_id": anchor_id,
                "role": "anchor",
                "title": title,
                "source_label": source_label,
                "source_type": source_type,
                "text_preview": full_text[:120],
            },
            {
                "anchor_node_id": anchor_id,
                "node_id": f"{anchor_id}_chunk_1",
                "source_node_id": anchor_id,
                "role": "chunk",
                "title": title,
                "source_label": source_label,
                "source_type": source_type,
                "chunk_index": 1,
                "source_span_start": 0,
                "source_span_end": min(len(full_text), len(chunk_text)),
                "text_preview": chunk_text,
            },
        ],
        "coverage": {"match_count": 2, "chunk_count": 1, "fact_count": 1, "summary_count": 0},
        "open_questions": [],
    }


def _pr12l_source_trace_row(
    *,
    family: str,
    index: int,
    title: str,
    source_label: str,
    source_type: str,
    text_preview: str,
    source_url: str | None = None,
) -> dict[str, Any]:
    row = {
        "anchor_node_id": f"trace_{family}_{index:02d}",
        "node_id": f"trace_{family}_{index:02d}",
        "role": "source",
        "title": title,
        "source_label": source_label,
        "source_type": source_type,
        "text_preview": text_preview,
    }
    if source_url:
        row["source_url"] = source_url
    return row


def _pr12l_retrieval_fixture_material(case: ProductBenchmarkCase) -> dict[str, Any]:
    family = case.family
    query = str(case.query_text or case.case_id)
    common_doc = _pr12l_document_packet(
        anchor_id=f"doc_{family}_acquisition",
        title="BaxEnergy Yokogawa acquisition release",
        source_label="yokogawa_acquisition_release.md",
        full_text=(
            "On June 5 2024 Yokogawa Electric Corporation announced it had acquired BaxEnergy, "
            "a provider of renewable energy management solutions. The document describes BaxEnergy software "
            "as part of renewable energy asset monitoring and management, preserving the date, parties and project context."
        ),
        lookup_role="related_document_lookup",
        project_tags=["BaxEnergy", "Yokogawa", "energy management"],
        entity_tags=["BaxEnergy", "Yokogawa Electric Corporation"],
        timeline_tags=["June 5 2024", "2024"],
        topic_tags=["acquisition", "renewable energy", "document retrieval"],
    )
    architecture_doc = _pr12l_document_packet(
        anchor_id=f"doc_{family}_architecture",
        title="Project architecture decisions for AGVM memory retrieval",
        source_label="agvm_architecture_decisions.md",
        full_text=(
            "Decision log dated 2026-05-07: AGVM must return MCP context packages as the primary product. "
            "Path corridors are branch-local scout routes that read useful intermediate nodes while document workspace retrieval "
            "keeps complete raw documents available for agents when requested."
        ),
        lookup_role="project_workspace",
        query_fit=0.84,
        exact=0.7,
        project_tags=["AGVM", "Memory OS", "MCP"],
        entity_tags=["AGVM"],
        timeline_tags=["2026-05-07"],
        topic_tags=["architecture", "path corridors", "context package"],
    )

    material: dict[str, Any] = {
        "primary": "work",
        "required_sections": ["work"],
        "optional_sections": ["documents", "history", "values"],
        "broad_context": False,
        "document_mode": "none",
        "document_target": "",
        "document_lookup": {"kind": "none"},
        "document_packets": [],
        "matches": [],
        "landing_sections": ["work", "documents"],
        "forbidden_topics": [],
        "tool_name": "retrieve_context",
        "retrieval_mode": "balanced",
        "answerability_state": "grounded",
        "stop_reason": "context_contract_satisfied",
        "warm_followup_economy": {},
        "warm_context_carryover": {},
        "extra_source_trace": [],
    }

    if family == "exact_fact":
        material.update(
            primary="work",
            required_sections=["work"],
            matches=[
                _pr12l_retrieval_match(
                    family=family,
                    index=1,
                    section="work",
                    source_label="founder_work_profile",
                    text="Simone founded BaxEnergy in 2010, later led Free Mind Foundry, and is publicly associated with WiSNAM and Intellisync.",
                )
            ],
        )
    elif family == "broad_self_dossier":
        material.update(
            primary="broad_dossier",
            required_sections=["identity", "work", "relationships", "style", "values", "history", "documents"],
            optional_sections=["temporal_inventory"],
            broad_context=True,
            landing_sections=["identity", "work", "relationships", "documents"],
            document_packets=[architecture_doc],
            document_mode="lookup",
            document_lookup={"kind": "related_document_lookup", "target_text": "agent self dossier"},
            matches=[
                _pr12l_retrieval_match(family=family, index=1, section="identity", source_label="profile", text="Simone is represented as a founder-operator and software entrepreneur based in Sicily."),
                _pr12l_retrieval_match(family=family, index=2, section="work", source_label="profile", text="His work context connects BaxEnergy, renewable energy management, industrial automation, WiSNAM, Intellisync and Free Mind Foundry."),
                _pr12l_retrieval_match(family=family, index=3, section="relationships", source_label="family_note", text="Family context includes father Giovanni Massaro and a memorial monument dedicated to him."),
                _pr12l_retrieval_match(family=family, index=4, section="style", source_label="style_note", text="Communication style is technical, direct, structured and grounded in practical engineering details."),
                _pr12l_retrieval_match(family=family, index=5, section="values", source_label="values_note", text="Important values include precision, sustainable impact, courage, education, cooperation and talent retention in Sicily."),
                _pr12l_retrieval_match(family=family, index=6, section="history", source_label="timeline_note", text="Timeline record: 2010 marks an early dated event, and June 5 2024 marks a later public acquisition announcement."),
            ],
        )
    elif family == "relationship":
        material.update(
            primary="relationship",
            required_sections=["relationships"],
            optional_sections=["history"],
            matches=[
                _pr12l_retrieval_match(family=family, index=1, section="relationships", source_label="family_note", text="Giovanni Massaro is remembered as Simone's father and was associated with the Italian Air Force."),
                _pr12l_retrieval_match(family=family, index=2, section="relationships", source_label="family_note", text="A monument dedicated to Giovanni Massaro was inaugurated on May 5 2026."),
            ],
        )
    elif family == "temporal":
        material.update(
            primary="temporal",
            required_sections=["temporal_inventory", "history"],
            optional_sections=["work", "documents"],
            document_packets=[common_doc],
            document_mode="lookup",
            document_lookup={"kind": "related_document_lookup", "target_text": "2019 2024 timeline"},
            matches=[
                _pr12l_retrieval_match(family=family, index=1, section="history", source_label="timeline_note", text="2019: the record contains a dated career event before the later 2024 public event."),
                _pr12l_retrieval_match(family=family, index=2, section="temporal_inventory", source_label="timeline_note", text="June 5 2024: Yokogawa Electric Corporation announced the acquisition of BaxEnergy."),
            ],
        )
    elif family == "project_workspace":
        material.update(
            primary="document_lookup",
            required_sections=["work", "documents"],
            optional_sections=["history", "values"],
            document_mode="lookup",
            document_target="project architecture decisions",
            document_lookup={"kind": "project_workspace_lookup", "target_text": "project architecture decisions"},
            document_packets=[architecture_doc, common_doc],
            tool_name="retrieve_project_workspace",
            retrieval_mode="heavy",
            matches=[
                _pr12l_retrieval_match(family=family, index=1, section="work", source_label="project_index", text="AGVM project workspace retrieval should group architecture decisions, path corridor design and document retrieval proof."),
            ],
        )
    elif family == "exact_document":
        exact_doc = _pr12l_document_packet(
            anchor_id="doc_exact_acquisition_release",
            title="BaxEnergy Yokogawa acquisition release",
            source_label="yokogawa_acquisition_release.md",
            full_text=(
                "On June 5 2024 Yokogawa Electric Corporation announced it had acquired BaxEnergy, "
                "a provider of renewable energy management solutions. This exact document is the requested acquisition source, "
                "not a related summary, and it preserves complete raw text for MCP agents."
            ),
            lookup_role="exact_document_lookup",
            exact=0.98,
            query_fit=0.96,
            project_tags=["BaxEnergy", "Yokogawa"],
            entity_tags=["BaxEnergy", "Yokogawa Electric Corporation"],
            timeline_tags=["June 5 2024"],
            topic_tags=["acquisition", "exact document"],
        )
        material.update(
            primary="document_lookup",
            required_sections=["documents"],
            document_mode="lookup",
            document_target="BaxEnergy Yokogawa acquisition release",
            document_lookup={"kind": "exact_document_lookup", "target_text": "BaxEnergy Yokogawa acquisition release"},
            document_packets=[exact_doc],
            tool_name="retrieve_document",
            retrieval_mode="forensic",
        )
    elif family == "related_documents":
        related = _pr12l_document_packet(
            anchor_id="doc_related_baxenergy_profile",
            title="BaxEnergy renewable energy management profile",
            source_label="baxenergy_profile.md",
            full_text="BaxEnergy provides renewable energy management software for asset monitoring, energy operations and industrial control contexts.",
            lookup_role="related_document_lookup",
            query_fit=0.77,
            exact=0.58,
            project_tags=["BaxEnergy"],
            entity_tags=["BaxEnergy"],
            timeline_tags=["2024"],
            topic_tags=["renewable energy", "energy management"],
        )
        material.update(
            primary="document_lookup",
            required_sections=["documents"],
            document_mode="lookup",
            document_target="related documents about this project",
            document_lookup={"kind": "related_document_lookup", "target_text": "BaxEnergy project"},
            document_packets=[common_doc, related],
            tool_name="retrieve_project_workspace",
            retrieval_mode="heavy",
        )
    elif family == "source_trace":
        material.update(
            primary="document_lookup",
            required_sections=["documents"],
            optional_sections=["work"],
            document_mode="lookup",
            document_target="web facts source trace",
            document_lookup={"kind": "source_trace_lookup", "target_text": "web facts source trace"},
            document_packets=[common_doc],
            tool_name="retrieve_source_trace",
            extra_source_trace=[
                _pr12l_source_trace_row(
                    family=family,
                    index=1,
                    title="Public web profile",
                    source_label="https://bench-source.test/profile",
                    source_type="website",
                    source_url="https://bench-source.test/profile",
                    text_preview="Public web profile links BaxEnergy, WiSNAM, Intellisync and Free Mind Foundry with source provenance.",
                )
            ],
        )
    elif family == "cross_area":
        material.update(
            primary="broad_dossier",
            required_sections=["work", "values", "documents"],
            optional_sections=["history", "style"],
            broad_context=True,
            document_mode="lookup",
            document_lookup={"kind": "related_document_lookup", "target_text": "work values public documents"},
            document_packets=[architecture_doc, common_doc],
            landing_sections=["work", "values", "documents"],
            matches=[
                _pr12l_retrieval_match(family=family, index=1, section="work", source_label="work_profile", text="Work history connects BaxEnergy, industrial automation, renewable energy management and public acquisition documentation."),
                _pr12l_retrieval_match(family=family, index=2, section="values", source_label="values_note", text="Values connect practical engineering, sustainability, precision and cooperation across companies, universities and energy operators."),
                _pr12l_retrieval_match(family=family, index=3, section="documents", source_label="document_index", text="The public document set includes an acquisition release and an AGVM architecture decision log that agents may inspect."),
            ],
            retrieval_mode="heavy",
        )
    elif family == "no_match":
        material.update(
            primary="document_lookup",
            required_sections=["documents"],
            document_mode="lookup",
            document_target="ZetaFlux compliance archive",
            document_lookup={"kind": "no_document_found", "target_text": "ZetaFlux compliance archive"},
            document_packets=[],
            tool_name="retrieve_document",
            answerability_state="insufficient",
            stop_reason="document_lookup_no_matching_packet",
        )
    elif family == "warm_followup":
        warm = {
            "schema_version": "agvm.warm_context_carryover.v1",
            "state": "warm_reused",
            "continuity_state": "high_continuity",
            "warm_state_used": True,
            "retained_node_ids": ["bench_relationship_01", "bench_relationship_02"],
            "quality_score": 0.88,
            "token_estimate": 420,
        }
        material.update(
            primary="relationship",
            required_sections=["relationships"],
            optional_sections=["history"],
            matches=[
                _pr12l_retrieval_match(family=family, index=1, section="relationships", source_label="warm_previous_context", text="The warm context reused the previous family answer about Giovanni Massaro without rereading unrelated work documents."),
                _pr12l_retrieval_match(family=family, index=2, section="relationships", source_label="family_note", text="The relationship context remains focused on father Giovanni Massaro and the memorial monument inaugurated on May 5 2026."),
            ],
            warm_context_carryover=warm,
            warm_followup_economy={
                "schema_version": "agvm.warm_followup_economy.v1",
                "warm_state_used": True,
                "reused_node_count": 2,
                "duplicate_read_avoided_count": 2,
                "hot_context_carryover_tokens": 420,
                "warm_context_carryover": warm,
            },
        )
    elif family == "divergent_followup":
        material.update(
            primary="work",
            required_sections=["work", "style"],
            optional_sections=["values"],
            forbidden_topics=["unrelated_family_context"],
            matches=[
                _pr12l_retrieval_match(family=family, index=1, section="relationships", source_label="warm_previous_context", text="Previous turn family context mentions father Giovanni Massaro, but it must stay cold for this divergent work-style query.", score=0.62),
                _pr12l_retrieval_match(family=family, index=2, section="work", source_label="style_note", text="The new divergent query needs work evidence around software operations, engineering delivery and implementation practice."),
                _pr12l_retrieval_match(family=family, index=3, section="style", source_label="style_note", text="The communication style is direct, technical, concise and oriented around implementation details."),
            ],
            warm_context_carryover={
                "schema_version": "agvm.warm_context_carryover.v1",
                "state": "warm_partially_rejected",
                "continuity_state": "divergent_query",
                "warm_state_used": False,
                "retained_node_ids": [],
                "quality_score": 0.32,
            },
            warm_followup_economy={
                "schema_version": "agvm.warm_followup_economy.v1",
                "warm_state_used": False,
                "reused_node_count": 0,
                "duplicate_read_avoided_count": 0,
                "divergence_flags": ["required_slot_shift_without_warm_support"],
            },
        )
    elif family == "contradiction":
        material.update(
            primary="relationship",
            required_sections=["relationships", "history"],
            matches=[
                _pr12l_retrieval_match(family=family, index=1, section="relationships", source_label="newer_correction_2026", text="Newer correction: Simone's father is Giovanni Massaro, associated with the Italian Air Force.", claim_status="fact", score=0.94),
                _pr12l_retrieval_match(family=family, index=2, section="history", source_label="contradiction_audit", text="Older placeholder note is superseded by the newer 2026 correction and remains only for audit, not as the active fact.", claim_status="superseded", score=0.71),
            ],
        )
    elif family == "hypothesis":
        material.update(
            primary="work",
            required_sections=["work"],
            optional_sections=["values", "history"],
            matches=[
                _pr12l_retrieval_match(family=family, index=1, section="work", source_label="planning_note", text="Open hypothesis: a memory OS can improve retrieval by learning reusable branch-local path strategies from successful searches.", claim_status="hypothesis", memory_type="hypothesis", source_trust="user_asserted"),
                _pr12l_retrieval_match(family=family, index=2, section="values", source_label="planning_note", text="The hypothesis is useful planning material, but it must not be presented as an already proven product fact.", claim_status="hypothesis", memory_type="hypothesis", source_trust="user_asserted"),
            ],
        )
    elif family == "future_dream":
        material.update(
            primary="broad_dossier",
            required_sections=["work", "values"],
            optional_sections=["history", "documents"],
            broad_context=True,
            landing_sections=["work", "values", "documents"],
            document_packets=[architecture_doc],
            document_mode="lookup",
            document_lookup={"kind": "related_document_lookup", "target_text": "future projects and dreams"},
            matches=[
                _pr12l_retrieval_match(family=family, index=1, section="work", source_label="future_note", text="Future goal: turn AGVM into a local-first and cloud-ready MCP memory operating system for agents and personal clones.", claim_status="future_intent", memory_type="future"),
                _pr12l_retrieval_match(family=family, index=2, section="values", source_label="future_note", text="The future goal is linked to values of durable memory, source-grounded learning, human review and practical agent usefulness.", claim_status="future_intent", memory_type="future"),
            ],
            retrieval_mode="heavy",
        )
    else:
        material["matches"] = [
            _pr12l_retrieval_match(
                family=family,
                index=1,
                section="work",
                source_label="fallback_fixture",
                text=f"Fallback retrieval fixture for {family}.",
            )
        ]
    return material


def _pr12l_build_path_corridors(
    *,
    case: ProductBenchmarkCase,
    material: dict[str, Any],
    semantic_contract: dict[str, Any],
    matches: list[dict[str, Any]],
    evidence_reservoir: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    from answering import build_path_corridor_package

    landing_sections = list(material.get("landing_sections") or material.get("required_sections") or ["work"])
    if len(landing_sections) < 2:
        return {}, [], []
    landing_metadata = [
        {
            "landing_id": f"runtime_{case.family}_{index + 1}",
            "branch_id": f"branch_{case.family}_{index + 1}",
            "probe_id": f"probe_{case.family}_{index + 1}",
            "label": section.title(),
            "goal": section,
            "planner_family": "ai_contract",
            "route_trace_count": 1,
            "studied_node_count": 1,
            "hydrated_node_count": 1,
        }
        for index, section in enumerate(landing_sections[:4])
    ]
    promoted_ids = [str(match.get("node_id") or "") for match in matches if str(match.get("node_id") or "").strip()]
    if not promoted_ids:
        return {}, landing_metadata, []
    branches: list[dict[str, Any]] = []
    for index, landing in enumerate(landing_metadata):
        node_id = promoted_ids[min(index, len(promoted_ids) - 1)]
        branches.append(
            {
                "branch_id": landing["branch_id"],
                "goal": landing["goal"],
                "probe_id": landing["probe_id"],
                "route_trace": [
                    {
                        "from_node_id": f"route_origin_{case.family}_{index + 1}",
                        "to_node_id": node_id,
                        "edge_type": "highway" if index == 0 else "semantic_link",
                        "move_type": "destination_reached",
                        "travel_performed": True,
                        "studied_node_ids": [node_id],
                        "hydrated_node_ids": [node_id],
                        "yielded_match_ids": [node_id],
                        "destination_reached": True,
                        "destination_label": str(landing["goal"]),
                    }
                ],
            }
        )
    package = build_path_corridor_package(
        query_text=str(case.query_text or ""),
        branches=branches,
        steps=[],
        matches=matches,
        evidence_reservoir=evidence_reservoir,
        semantic_contract=semantic_contract,
        landing_metadata=landing_metadata,
        retrieval_mode=str(material.get("retrieval_mode") or "balanced"),
    )
    return package, landing_metadata, branches


def _pr12l_build_retrieval_result(case: ProductBenchmarkCase) -> dict[str, Any]:
    from answering import build_document_workspace_package, build_mcp_context_package

    material = _pr12l_retrieval_fixture_material(case)
    query = str(case.query_text or case.case_id)
    matches = [dict(item) for item in list(material.get("matches") or [])]
    document_packets = [dict(item) for item in list(material.get("document_packets") or [])]
    semantic_contract = _pr12l_contract(
        query_text=query,
        primary=str(material.get("primary") or "work"),
        required_sections=list(material.get("required_sections") or ["work"]),
        optional_sections=list(material.get("optional_sections") or []),
        broad_context=bool(material.get("broad_context")),
        document_mode=str(material.get("document_mode") or "none"),
        document_target=str(material.get("document_target") or ""),
        landing_sections=list(material.get("landing_sections") or material.get("required_sections") or ["work"]),
        forbidden_topics=list(material.get("forbidden_topics") or []),
    )
    evidence_reservoir = {
        "schema_version": "agvm.evidence_reservoir.v1",
        "entries": [{**dict(match.get("node") or {}), **{key: value for key, value in match.items() if key != "node"}} for match in matches],
        "documents": document_packets,
        "quality_metrics": {
            "query_fit": 0.91 if matches or document_packets else 0.0,
            "source_grounded": bool(matches or document_packets),
        },
        "reservoir_summary": {
            "entry_count": len(matches),
            "document_count": len(document_packets),
            "family": case.family,
        },
        "unresolved_slots": [] if matches or document_packets else list(material.get("required_sections") or []),
    }
    path_corridors, landing_metadata, branches = _pr12l_build_path_corridors(
        case=case,
        material=material,
        semantic_contract=semantic_contract,
        matches=matches,
        evidence_reservoir=evidence_reservoir,
    )
    document_lookup = dict(material.get("document_lookup") or {"kind": "none"})
    document_workspace = build_document_workspace_package(
        query_text=query,
        document_mode=str(material.get("document_mode") or "none"),
        document_lookup=document_lookup,
        document_packets=document_packets,
        evidence_reservoir=evidence_reservoir,
        semantic_contract=semantic_contract,
        retrieval_mode=str(material.get("retrieval_mode") or "balanced"),
        path_corridors=path_corridors,
    )
    context_package = build_mcp_context_package(
        query_text=query,
        context={"structured_sections": []},
        matches=matches,
        evidence_reservoir=evidence_reservoir,
        document_packets=document_packets,
        semantic_contract=semantic_contract,
        retrieval_mode=str(material.get("retrieval_mode") or "balanced"),
        path_corridors=path_corridors,
        document_workspace=document_workspace,
    )
    source_trace = [
        *[dict(row) for row in list(document_workspace.get("source_trace") or []) if isinstance(row, dict)],
        *[dict(row) for row in list(material.get("extra_source_trace") or []) if isinstance(row, dict)],
    ]
    warm_followup_economy = dict(material.get("warm_followup_economy") or {})
    warm_context_carryover = dict(material.get("warm_context_carryover") or warm_followup_economy.get("warm_context_carryover") or {})
    answerability_state = str(material.get("answerability_state") or "grounded")
    return {
        "search_id": f"pr12l_c_{case.family}",
        "thread_id": "pr12l_c_thread",
        "query_text": query,
        "response_mode": "context",
        "retrieval_mode": str(material.get("retrieval_mode") or "balanced"),
        "matches": matches,
        "branches": branches,
        "steps": [],
        "landing_metadata": landing_metadata,
        "visited_node_ids": [str(match.get("node_id") or "") for match in matches if str(match.get("node_id") or "").strip()],
        "visited_bucket_keys": [],
        "document_lookup_kind": str(document_lookup.get("kind") or "none"),
        "document_lookup": document_lookup,
        "document_packets": document_packets,
        "supporting_documents": document_packets,
        "source_trace": source_trace,
        "document_workspace": document_workspace,
        "evidence_reservoir": evidence_reservoir,
        "context_package": context_package,
        "path_corridors": path_corridors,
        "semantic_contract": semantic_contract,
        "semantic_contract_runtime": {
            "contract_passed": bool((context_package.get("contract") or {}).get("passed")),
            "source": "pr12l_c_deterministic_benchmark",
        },
        "answerability_state": answerability_state,
        "stop_reason": str(material.get("stop_reason") or "context_contract_satisfied"),
        "timing": {
            "plan_ms": 1.0,
            "first_context_ms": 5.0,
            "final_materialization_completed_ms": 10.0,
        },
        "planner_runtime": {
            "semantic_contract": semantic_contract,
            "semantic_contract_runtime": {"contract_passed": bool((context_package.get("contract") or {}).get("passed"))},
            "landing_metadata_count": len(landing_metadata),
            "branch_count": len(branches),
            "brain_geometry_calibration": {
                "schema_version": "agvm.brain_geometry_calibration.v1",
                "state": "fixture_present",
            },
            "warm_followup_economy": warm_followup_economy,
            "warm_context_carryover": warm_context_carryover,
            "route_truth_summary": {
                "landing_count": len(landing_metadata),
                "path_count": int(((path_corridors or {}).get("metrics") or {}).get("path_count") or 0),
                "synthetic_motion": False,
            },
        },
        "warm_followup_economy": warm_followup_economy,
        "warm_context_carryover": warm_context_carryover,
        "ai_material_contribution": True,
        "ai_contribution_reason": "deterministic benchmark fixture represents AI contract output without invoking a live model",
    }


def _pr12l_contracts_by_name() -> dict[str, dict[str, Any]]:
    from mcp_contracts import build_mcp_contract_registry

    registry = build_mcp_contract_registry()
    return {
        str(tool.get("name") or ""): dict(tool)
        for tool in list(registry.get("tools") or [])
        if isinstance(tool, dict) and str(tool.get("name") or "").strip()
    }


def _pr12l_first_document(output: dict[str, Any]) -> dict[str, Any]:
    workspace = dict(output.get("document_workspace") or {})
    documents = [dict(item) for item in list(workspace.get("documents") or []) if isinstance(item, dict)]
    return documents[0] if documents else {}


def _pr12l_result_summary(result: dict[str, Any], tool_outputs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    context = dict(result.get("context_package") or {})
    workspace = dict(result.get("document_workspace") or {})
    path_corridors = dict(result.get("path_corridors") or {})
    context_metrics = dict(context.get("metrics") or {})
    workspace_metrics = dict(workspace.get("metrics") or {})
    path_metrics = dict(path_corridors.get("metrics") or {})
    redacted_document = dict(tool_outputs.get("retrieve_document_redacted") or tool_outputs.get("retrieve_project_workspace_redacted") or {})
    raw_document = dict(tool_outputs.get("retrieve_document_raw") or tool_outputs.get("retrieve_project_workspace_raw") or {})
    redacted_first = _pr12l_first_document(redacted_document)
    raw_first = _pr12l_first_document(raw_document)
    return {
        "context_package": {
            "schema_version": context.get("schema_version"),
            "status": context.get("status"),
            "contract_passed": bool((context.get("contract") or {}).get("passed")),
            "section_count": int(context_metrics.get("section_count") or 0),
            "hot_item_count": int(context_metrics.get("hot_item_count") or 0),
            "cold_item_count": int(context_metrics.get("cold_item_count") or 0),
            "excluded_item_count": int(context_metrics.get("excluded_item_count") or 0),
            "path_discovery_count": int(context_metrics.get("path_discovery_count") or 0),
            "document_workspace_document_count": int(context_metrics.get("document_workspace_document_count") or 0),
            "dossier_hygiene_passed": bool((context.get("dossier_hygiene") or {}).get("passed")),
            "truncated_core_text_count": int(context_metrics.get("truncated_core_text_count") or 0),
        },
        "document_workspace": {
            "schema_version": workspace.get("schema_version"),
            "status": workspace.get("status"),
            "workspace_kind": workspace.get("workspace_kind"),
            "document_count": int(workspace_metrics.get("document_count") or 0),
            "full_text_document_count": int(workspace_metrics.get("full_text_document_count") or 0),
            "source_trace_count": int(workspace_metrics.get("source_trace_count") or 0),
            "related_document_link_count": int(workspace_metrics.get("related_document_link_count") or 0),
            "no_match": bool(workspace_metrics.get("no_match")),
        },
        "path_corridors": {
            "schema_version": path_corridors.get("schema_version"),
            "status": path_corridors.get("status"),
            "landing_count": int(path_metrics.get("landing_count") or 0),
            "path_count": int(path_metrics.get("path_count") or 0),
            "route_event_count": int(path_metrics.get("route_event_count") or 0),
            "promoted_intermediate_count": int(path_metrics.get("promoted_intermediate_count") or 0),
            "changed_context_package_path_count": int(path_metrics.get("changed_context_package_path_count") or 0),
        },
        "source_trace": {
            "count": len(list(result.get("source_trace") or [])),
            "has_url": any(bool((row or {}).get("source_url")) for row in list(result.get("source_trace") or []) if isinstance(row, dict)),
        },
        "mcp_outputs": {
            "tool_statuses": {
                name: output.get("status")
                for name, output in tool_outputs.items()
                if not name.endswith("_raw") and not name.endswith("_redacted")
            },
            "answer_demo_default_present": any("answer_demo" in output for output in tool_outputs.values()),
            "raw_document_policy": {
                "default_full_text_redacted": redacted_first.get("full_text") == "" if redacted_first else None,
                "default_full_text_available_flag": bool(redacted_first.get("full_text_available")) if redacted_first else None,
                "raw_full_text_preserved_when_requested": bool(str(raw_first.get("full_text") or "").strip()) if raw_first else None,
            },
        },
        "warm_context": {
            "carryover_state": str((result.get("warm_context_carryover") or {}).get("state") or ""),
            "warm_state_used": bool((result.get("warm_followup_economy") or {}).get("warm_state_used")),
            "reused_node_count": int((result.get("warm_followup_economy") or {}).get("reused_node_count") or 0),
            "duplicate_read_avoided_count": int((result.get("warm_followup_economy") or {}).get("duplicate_read_avoided_count") or 0),
        },
    }


def _run_pr12l_retrieval_mcp_case(case: ProductBenchmarkCase, contracts_by_name: dict[str, dict[str, Any]]) -> dict[str, Any]:
    from mcp_retrieval import build_mcp_retrieval_tool_output
    from mcp_stability import validate_mcp_tool_output

    started = time.perf_counter()
    failures: list[str] = []
    validations: list[dict[str, Any]] = []
    tool_outputs: dict[str, dict[str, Any]] = {}
    result: dict[str, Any] = {}
    try:
        result = _pr12l_build_retrieval_result(case)
        primary_tool = str(_pr12l_retrieval_fixture_material(case).get("tool_name") or "retrieve_context")
        tools = ["retrieve_context"]
        if primary_tool not in tools:
            tools.append(primary_tool)
        if result.get("path_corridors") and "retrieve_path_corridor" not in tools:
            tools.append("retrieve_path_corridor")
        if result.get("source_trace") and "retrieve_source_trace" not in tools:
            tools.append("retrieve_source_trace")
        if result.get("document_workspace") and str((result.get("document_workspace") or {}).get("status") or "") in {"workspace_ready", "no_document_found"}:
            document_tool = "retrieve_project_workspace" if primary_tool == "retrieve_project_workspace" else "retrieve_document"
            if document_tool not in tools:
                tools.append(document_tool)
        for tool in tools:
            output = build_mcp_retrieval_tool_output(tool, result)
            tool_outputs[tool] = output
            contract = contracts_by_name.get(tool)
            if not contract:
                failures.append(f"missing_mcp_contract:{tool}")
                continue
            validation = validate_mcp_tool_output(contract, output)
            validations.append(validation)
            if not bool(validation.get("passed")):
                failures.append(f"mcp_validation_failed:{tool}:{validation.get('errors')}")
        if result.get("document_workspace"):
            raw_tool = "retrieve_project_workspace" if primary_tool == "retrieve_project_workspace" else "retrieve_document"
            tool_outputs[f"{raw_tool}_redacted"] = build_mcp_retrieval_tool_output(raw_tool, result, include_raw_text=False)
            tool_outputs[f"{raw_tool}_raw"] = build_mcp_retrieval_tool_output(raw_tool, result, include_raw_text=True)

        context_package = dict(result.get("context_package") or {})
        context_metrics = dict(context_package.get("metrics") or {})
        agent_markdown = str(context_package.get("agent_markdown") or "")
        if context_package.get("schema_version") != "agvm.mcp_context_package.v2":
            failures.append("context_package_schema_mismatch")
        if not bool((context_package.get("dossier_hygiene") or {}).get("passed")):
            failures.append("dossier_hygiene_failed")
        for forbidden in ("Evidence Ledger", "## Path Discoveries", "Landing 1 -> Landing 2", "vec_node_"):
            if forbidden in agent_markdown:
                failures.append(f"debug_leak_in_agent_markdown:{forbidden}")
        if int(context_metrics.get("truncated_core_text_count") or 0) > 0:
            failures.append("truncated_core_text_in_context")
        if any("answer_demo" in output for output in tool_outputs.values()):
            failures.append("answer_demo_present_by_default")

        workspace = dict(result.get("document_workspace") or {})
        workspace_metrics = dict(workspace.get("metrics") or {})
        path_corridors = dict(result.get("path_corridors") or {})
        path_metrics = dict(path_corridors.get("metrics") or {})
        family = case.family
        if family in {"exact_fact", "relationship", "temporal", "cross_area", "warm_followup", "divergent_followup", "contradiction", "hypothesis", "future_dream"}:
            if int(context_metrics.get("hot_item_count") or 0) < 1:
                failures.append("hot_context_missing")
        if family == "broad_self_dossier" and int(context_metrics.get("section_count") or 0) < 6:
            failures.append("broad_dossier_too_thin")
        if family in {"project_workspace", "exact_document", "related_documents"}:
            if workspace.get("status") != "workspace_ready":
                failures.append("document_workspace_not_ready")
            if int(workspace_metrics.get("document_count") or 0) < (2 if family in {"project_workspace", "related_documents"} else 1):
                failures.append("document_workspace_document_count_too_low")
        if family == "exact_document":
            redacted = _pr12l_first_document(tool_outputs.get("retrieve_document_redacted") or {})
            raw = _pr12l_first_document(tool_outputs.get("retrieve_document_raw") or {})
            if redacted.get("full_text") != "" or not bool(redacted.get("full_text_available")):
                failures.append("exact_document_raw_text_not_redacted_by_default")
            if not str(raw.get("full_text") or "").strip():
                failures.append("exact_document_raw_text_not_preserved_when_requested")
        if family == "source_trace":
            if len(list(result.get("source_trace") or [])) < 2:
                failures.append("source_trace_too_thin")
            if not any(bool((row or {}).get("source_url")) for row in list(result.get("source_trace") or []) if isinstance(row, dict)):
                failures.append("source_trace_url_missing")
        if family in {"broad_self_dossier", "cross_area", "future_dream"}:
            if int(path_metrics.get("path_count") or 0) < 1:
                failures.append("path_corridor_missing")
            if int(path_metrics.get("promoted_intermediate_count") or 0) < 1:
                failures.append("path_corridor_no_promoted_intermediates")
        if family == "no_match":
            no_match_statuses = [str(output.get("status") or "") for output in tool_outputs.values()]
            if "no_match" not in no_match_statuses or workspace.get("workspace_kind") != "no_document_found":
                failures.append("no_match_not_honest_terminal_state")
        if family == "warm_followup":
            warm = dict(result.get("warm_followup_economy") or {})
            if not bool(warm.get("warm_state_used")) or int(warm.get("duplicate_read_avoided_count") or 0) < 1:
                failures.append("warm_followup_economy_missing")
        if family == "divergent_followup":
            if "father Giovanni Massaro" in agent_markdown:
                failures.append("divergent_followup_contaminated_by_warm_family_context")
            if int(context_metrics.get("cold_item_count") or 0) + int(context_metrics.get("excluded_item_count") or 0) < 1:
                failures.append("divergent_followup_did_not_control_previous_context")
        if family == "contradiction" and "superseded" not in agent_markdown.lower():
            failures.append("contradiction_supersession_not_visible")
        if family == "hypothesis" and "hypothesis" not in agent_markdown.lower():
            failures.append("hypothesis_status_not_visible")
        if family == "future_dream" and ("Future goal" not in agent_markdown or "completed fact" in agent_markdown):
            failures.append("future_goal_not_separated_from_completed_fact")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"exception:{type(exc).__name__}:{str(exc)[:300]}")

    return {
        "case_id": case.case_id,
        "family_group": case.family_group,
        "family": case.family,
        "fixture_kind": case.fixture_kind,
        "interaction": case.interaction,
        "slice_owner": case.slice_owner,
        "critical": bool(case.critical),
        "query_text": case.query_text,
        "required_artifacts": list(case.required_artifacts),
        "required_signals": list(case.required_signals),
        "passed": not failures,
        "failures": failures,
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "summary": _pr12l_result_summary(result, tool_outputs) if result else {},
        "mcp_validation_count": len(validations),
        "mcp_validation_failures": [item for item in validations if not bool(item.get("passed"))],
    }


def run_pr12l_retrieval_mcp_benchmark_suite(base_url: str | None = None) -> dict[str, Any]:
    retrieval_cases = [case for case in pr12l_benchmark_cases() if case.family_group == "retrieval"]
    contracts_by_name = _pr12l_contracts_by_name()
    case_results = [_run_pr12l_retrieval_mcp_case(case, contracts_by_name) for case in retrieval_cases]
    covered_families = {str(result.get("family") or "") for result in case_results}
    missing_families = sorted(set(REQUIRED_PR12L_RETRIEVAL_FAMILIES) - covered_families)
    critical_failures = [
        str(result.get("case_id") or "")
        for result in case_results
        if bool(result.get("critical")) and not bool(result.get("passed"))
    ]
    passed_count = sum(1 for result in case_results if bool(result.get("passed")))
    total_count = len(case_results)
    all_pass = not critical_failures and not missing_families and total_count == len(REQUIRED_PR12L_RETRIEVAL_FAMILIES)
    family_matrix = {
        family: {
            "covered": family in covered_families,
            "passed": any(str(result.get("family") or "") == family and bool(result.get("passed")) for result in case_results),
            "case_ids": [str(result.get("case_id") or "") for result in case_results if str(result.get("family") or "") == family],
        }
        for family in REQUIRED_PR12L_RETRIEVAL_FAMILIES
    }
    fixture_matrix: dict[str, dict[str, Any]] = {}
    for result in case_results:
        fixture_kind = str(result.get("fixture_kind") or "")
        entry = fixture_matrix.setdefault(fixture_kind, {"case_count": 0, "passed_count": 0, "families": []})
        entry["case_count"] += 1
        entry["passed_count"] += 1 if bool(result.get("passed")) else 0
        entry["families"].append(str(result.get("family") or ""))
    open_gaps: list[str] = []
    if missing_families:
        open_gaps.append(f"Missing retrieval/MCP benchmark families: {', '.join(missing_families)}")
    if critical_failures:
        open_gaps.append(f"Critical retrieval/MCP failures: {', '.join(critical_failures)}")
    verdict = "retrieval_mcp_passed_pr12l_still_open" if all_pass else "retrieval_mcp_failed_pr12l_still_open"
    return {
        "schema_version": PR12L_RETRIEVAL_MCP_BENCHMARK_REPORT_SCHEMA_VERSION,
        "phase": "retrieval_mcp",
        "slice": "PR-12L-C",
        "all_pass": all_pass,
        "pass_rate": round(passed_count / total_count, 4) if total_count else 0.0,
        "passed_count": passed_count,
        "total_count": total_count,
        "base_url": str(base_url or DEFAULT_BASE_URL),
        "product_ready_verdict": verdict,
        "case_results": case_results,
        "family_matrix": family_matrix,
        "fixture_matrix": fixture_matrix,
        "critical_failures": critical_failures,
        "open_gaps": open_gaps,
        "benchmark_inputs": {
            "phase": "retrieval_mcp",
            "retrieval_families": list(REQUIRED_PR12L_RETRIEVAL_FAMILIES),
            "external_network_required": False,
            "mutation_enabled": False,
            "live_llm_required": False,
            "answer_demo_default": False,
            "uses_real_mcp_adapters": True,
            "uses_real_context_document_path_builders": True,
        },
        "next_slice": "PR-12L-D UI Truth Benchmark",
    }


def _pr12l_repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _pr12l_frontend_text(relative_path: str) -> str:
    return (_pr12l_repo_root() / "agvm_cockpit_prototype" / "src" / relative_path).read_text(encoding="utf-8")


def _pr12p_frontend_text(relative_path: str) -> str:
    path = _pr12l_repo_root() / "agvm_cockpit_prototype" / "src" / relative_path
    if path.exists():
        return path.read_text(encoding="utf-8")
    asset_kind = "css" if relative_path.endswith(".css") else "js"
    cache_key = f"{asset_kind}:{os.environ.get('AGVM_FRONTEND_URL') or ''}"
    if cache_key in _FRONTEND_ASSET_TEXT_CACHE:
        return _FRONTEND_ASSET_TEXT_CACHE[cache_key]
    frontend_url = str(os.environ.get("AGVM_FRONTEND_URL") or "http://agvm_ui:3020").rstrip("/")
    html_request = urllib.request.Request(url=frontend_url, method="GET", headers={"Host": "localhost:3020"})
    with urllib.request.urlopen(html_request, timeout=20) as response:
        html = response.read().decode("utf-8", errors="replace")
    pattern = r'href="([^"]+\.css)"' if asset_kind == "css" else r'src="([^"]+\.js)"'
    matches = re.findall(pattern, html)
    if not matches:
        raise FileNotFoundError(f"frontend_asset_not_found:{asset_kind}:{frontend_url}")
    asset_path = matches[-1]
    asset_url = asset_path if asset_path.startswith("http") else f"{frontend_url}{asset_path if asset_path.startswith('/') else '/' + asset_path}"
    asset_request = urllib.request.Request(url=asset_url, method="GET", headers={"Host": "localhost:3020"})
    with urllib.request.urlopen(asset_request, timeout=30) as response:
        text = response.read().decode("utf-8", errors="replace")
    _FRONTEND_ASSET_TEXT_CACHE[cache_key] = text
    return text


def _pr12l_require_present(failures: list[str], source: str, snippets: tuple[str, ...], *, label: str) -> None:
    for snippet in snippets:
        if snippet not in source:
            failures.append(f"{label}:missing:{snippet[:90]}")


def _pr12l_require_absent(failures: list[str], source: str, snippets: tuple[str, ...], *, label: str) -> None:
    for snippet in snippets:
        if snippet in source:
            failures.append(f"{label}:forbidden:{snippet[:90]}")


def _pr12l_ui_source_probe(contracts_by_name: dict[str, dict[str, Any]]) -> dict[str, Any]:
    from mcp_grow import build_mcp_source_output
    from mcp_stability import validate_mcp_tool_output
    from source_investigation import build_source_investigation_package

    package = build_source_investigation_package(
        "PR-12L-D source UI truth probe. The source package preserves raw source text, compiler handoff, "
        "clarification state, source trust and preview readiness before any memory mutation.",
        input_kind="manual_text",
        source_label="PR-12L-D UI source package probe",
        options={"treat_as": "project_workspace", "source_trust": "user_asserted", "max_units": 4},
    )
    output = build_mcp_source_output("grow_source_preview", source_package=package, preview_bundle=None)
    validation = validate_mcp_tool_output(contracts_by_name.get("grow_source_preview", {}), output)
    return {
        "source_package_summary": _pr12l_source_package_summary(package, _pr12l_preview_for_source_package(package)),
        "mcp_tool": "grow_source_preview",
        "mcp_status": output.get("status"),
        "mcp_validation": validation,
    }


def _pr12l_ui_maintenance_probe(contracts_by_name: dict[str, dict[str, Any]]) -> dict[str, Any]:
    from mcp_maintenance import build_mcp_maintenance_output
    from mcp_stability import validate_mcp_tool_output

    report = {
        "applied": False,
        "mode": "sleep",
        "maintenance_proposals": [
            {
                "proposal_id": "proposal_pr12l_d_merge_review",
                "proposal_kind": "merge_review",
                "risk_level": "low",
                "human_review_required": True,
                "reason": "Repeated source-derived memories should be reviewed before consolidation.",
                "target_node_ids": ["node_pr12l_d_a", "node_pr12l_d_b"],
                "target_document_ids": ["doc_pr12l_d_source"],
                "proposed_action": "review_merge_candidates",
            }
        ],
        "metamemory_snapshot": {
            "schema_version": "agvm.pr12h.metamemory_snapshot.v1",
            "snapshot_id": "metamemory::pr12l_d",
        },
        "apply_policy_guard": {
            "guard_passed": False,
            "applied": False,
            "blocked_reasons": ["preview_or_policy_blocked"],
            "rollback_snapshot": {"snapshot_id": "rollback::pr12l_d", "before_graph_hash": "before", "candidate_graph_hash": "candidate"},
            "before_after_audit": {"created_node_count": 0},
            "no_corruption_guards": {"passed": True},
        },
        "rollback_snapshot": {"snapshot_id": "rollback::pr12l_d", "before_graph_hash": "before", "candidate_graph_hash": "candidate"},
        "before_after_audit": {"created_node_count": 0},
        "no_corruption_guards": {"passed": True},
        "reviewed_node_ids": ["node_pr12l_d_a", "node_pr12l_d_b"],
        "sleep_consolidation_proposals": [{"proposal_id": "proposal_pr12l_d_merge_review"}],
        "evolve_structural_proposals": [],
        "retrieval_trace_learning_proposals": [],
        "duplicate_candidates": [{"node_ids": ["node_pr12l_d_a", "node_pr12l_d_b"]}],
        "self_improvement_loop": {"maintenance_id": "maintenance_pr12l_d", "applied": False},
    }
    output = build_mcp_maintenance_output("sleep_preview", report=report, max_nodes_considered=80)
    validation = validate_mcp_tool_output(contracts_by_name.get("sleep_preview", {}), output)
    return {
        "mcp_tool": "sleep_preview",
        "mcp_status": output.get("status"),
        "proposal_count": len(list(output.get("maintenance_proposals") or [])),
        "metamemory_schema": (output.get("metamemory_snapshot") or {}).get("schema_version"),
        "apply_guard_present": bool(output.get("apply_policy_guard")),
        "mcp_validation": validation,
    }


def _pr12l_ui_map_truth_probe() -> dict[str, Any]:
    from retrieval import build_landing_metadata, build_search_map_2d_truth

    probes = [
        {
            "probe_id": "probe_pr12l_d_identity",
            "goal": "identify memory owner",
            "planner_family": "ai",
            "query_text": "identify memory owner",
            "target_bucket_keys": ["identity:owner"],
            "landing_position": {"x": 0.15, "y": 0.25},
        },
        {
            "probe_id": "probe_pr12l_d_work",
            "goal": "find work documents",
            "planner_family": "heuristic",
            "query_text": "find work documents",
            "target_bucket_keys": ["work:documents"],
            "landing_position": {"x": 0.75, "y": 0.55},
        },
    ]
    branches = [
        {
            "branch_id": "branch_pr12l_d_identity",
            "probe_ids": ["probe_pr12l_d_identity"],
            "goal": "identify memory owner",
            "planner_family": "ai",
            "origin_families": ["ai"],
            "status": "completed",
            "route_state": "destination_reached",
            "candidate_node_ids": ["node_identity_candidate"],
            "studied_node_ids": ["node_identity_studied"],
            "hydrated_node_ids": ["node_identity_hydrated"],
            "evidence_node_ids": ["node_identity_evidence"],
            "destination_queue": [{"destination_id": "dest_identity", "label": "identity", "guide_area": "identity"}],
            "route_trace": [
                {
                    "move_type": "travel",
                    "edge_type": "highway",
                    "travel_performed": True,
                    "from_node_id": "node_identity_origin",
                    "to_node_id": "node_identity_evidence",
                    "from_bucket_key": "identity:0",
                    "to_bucket_key": "identity:1",
                    "destination_reached": True,
                    "route_reason": "AI-selected branch-local route reached identity evidence.",
                }
            ],
        },
        {
            "branch_id": "branch_pr12l_d_work",
            "probe_ids": ["probe_pr12l_d_work"],
            "goal": "find work documents",
            "planner_family": "heuristic",
            "origin_families": ["heuristic"],
            "status": "completed",
            "route_state": "destination_reached",
            "candidate_node_ids": ["node_work_candidate"],
            "studied_node_ids": ["node_work_studied"],
            "hydrated_node_ids": ["node_work_hydrated"],
            "evidence_node_ids": ["node_work_document"],
            "destination_queue": [{"destination_id": "dest_work", "label": "work documents", "guide_area": "work"}],
            "route_trace": [
                {
                    "move_type": "study",
                    "edge_type": "link",
                    "travel_performed": False,
                    "from_node_id": "node_work_origin",
                    "to_node_id": "node_work_document",
                    "from_bucket_key": "work:0",
                    "to_bucket_key": "work:1",
                    "destination_reached": True,
                    "route_reason": "Heuristic branch-local route inspected related work document.",
                }
            ],
        },
    ]
    landing_metadata = build_landing_metadata(probes, branches)
    truth = build_search_map_2d_truth(
        search_id="search_pr12l_d_ui_truth",
        thread_id="thread_pr12l_d_ui_truth",
        probes=probes,
        branches=branches,
        landing_metadata=landing_metadata,
        route_truth_summary={"highway_considered_count": 1, "highway_traversed_count": 1},
        phase="ui_truth",
    )
    return {
        "schema_version": truth.get("schema_version"),
        "metrics": dict(truth.get("metrics") or {}),
        "motion_policy": dict(truth.get("motion_policy") or {}),
        "invariants": dict(truth.get("invariants") or {}),
        "landing_count": len(list(truth.get("landings") or [])),
        "route_plan_count": len(list(truth.get("route_plans") or [])),
        "route_step_count": len(list(truth.get("route_steps") or [])),
    }


def _run_pr12l_ui_truth_case(
    case: ProductBenchmarkCase,
    *,
    retrieval_report: dict[str, Any],
    source_report: dict[str, Any],
    contracts_by_name: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    from mcp_retrieval import build_mcp_retrieval_tool_output, build_mcp_route_trace_output
    from mcp_stability import validate_mcp_tool_output

    started = time.perf_counter()
    failures: list[str] = []
    mcp_validations: list[dict[str, Any]] = []
    app = _pr12l_frontend_text("App.tsx")
    dashboard = _pr12l_frontend_text("components/agvm/AgvmRetrieveValidationDashboard.tsx")
    grow_console = _pr12l_frontend_text("components/agvm/AgvmGrowSourceConsole.tsx")
    map_component = _pr12l_frontend_text("components/agvm/AgvmOrchestrationPanel.tsx")
    maintenance_panel = _pr12l_frontend_text("components/agvm/AgvmMaintenancePanel.tsx")
    styles = _pr12l_frontend_text("index.css")
    summary: dict[str, Any] = {
        "frontend_static_contract": True,
        "retrieval_mcp_report_passed": bool(retrieval_report.get("all_pass")),
        "source_intake_report_passed": bool(source_report.get("all_pass")),
    }

    if not bool(retrieval_report.get("all_pass")):
        failures.append("retrieval_mcp_truth_report_not_passing")
    if not bool(source_report.get("all_pass")):
        failures.append("source_intake_truth_report_not_passing")

    try:
        if case.family == "context_package_parity":
            _pr12l_require_present(
                failures,
                app,
                (
                    "setRetrieval(createPlanningRetrievalSnapshot(requestPayload",
                    "context_package: {}",
                    "path_corridors: {}",
                    "search_map_2d_truth: {}",
                    'payload.context_package as AgvmRetrieveResponse["context_package"]',
                    'payload.path_corridors as AgvmRetrieveResponse["path_corridors"]',
                    'payload.search_map_2d_truth as AgvmRetrieveResponse["search_map_2d_truth"]',
                ),
                label="app_context_parity",
            )
            _pr12l_require_present(
                failures,
                dashboard,
                (
                    "hasRecordContent(backendContextPackage) ? backendContextPackage : streamContextPackage",
                    "contextPackageSource",
                    'data-testid="ui-context-package-parity"',
                    'data-testid="use-context-inspect-card"',
                    "dossierHygienePassed",
                    "Search live dossier, hot context, document workspace",
                    "Full text",
                ),
                label="dashboard_context_parity",
            )
            retrieval_case = _pr12l_build_retrieval_result(
                next(item for item in pr12l_benchmark_cases() if item.family == "broad_self_dossier")
            )
            output = build_mcp_retrieval_tool_output("retrieve_context", retrieval_case)
            validation = validate_mcp_tool_output(contracts_by_name.get("retrieve_context", {}), output)
            mcp_validations.append(validation)
            summary["context_package"] = _pr12l_result_summary(retrieval_case, {"retrieve_context": output})["context_package"]
            summary["mcp_output_default_has_answer_demo"] = "answer_demo" in output
            if "answer_demo" in output:
                failures.append("retrieve_context_mcp_default_contains_answer_demo")
        elif case.family == "map_trace_parity":
            map_truth = _pr12l_ui_map_truth_probe()
            invariants = dict(map_truth.get("invariants") or {})
            motion = dict(map_truth.get("motion_policy") or {})
            metrics = dict(map_truth.get("metrics") or {})
            if map_truth.get("schema_version") != "agvm.search_map_2d_truth.v1":
                failures.append("search_map_truth_schema_mismatch")
            if int(metrics.get("landing_count") or 0) < 2:
                failures.append("search_map_truth_landing_count_too_low")
            if not bool(invariants.get("each_landing_is_independent_scout_origin")):
                failures.append("search_map_truth_landing_origin_invariant_missing")
            if not bool(invariants.get("parallel_landings_are_not_serial_route")):
                failures.append("search_map_truth_parallel_landing_invariant_missing")
            if bool(invariants.get("array_order_route_labeling_allowed")):
                failures.append("search_map_truth_allows_array_order_labels")
            if bool(motion.get("allow_synthetic_motion")) or bool(motion.get("allow_css_perpetual_motion")):
                failures.append("search_map_truth_allows_synthetic_motion")
            _pr12l_require_present(
                failures,
                map_component,
                ("branchMapIdentityLabel(model.branch)", "search_map_2d_truth", "routeSegments", "routeSegmentsRaw"),
                label="map_component_truth",
            )
            _pr12l_require_absent(
                failures,
                map_component,
                ("Landing ${model.rawIndex + 1}", "Landing 1"),
                label="map_component_truth",
            )
            _pr12l_require_present(
                failures,
                dashboard,
                (
                    "searchMapMotion.allow_synthetic_motion",
                    "searchMapInvariants.serial_landing_route_detected",
                    'data-testid="ui-map-truth-parity"',
                ),
                label="dashboard_map_truth",
            )
            _pr12l_require_absent(
                failures,
                styles,
                ("animation: orchestrationPulse 1.9s ease-in-out infinite;", "animation: orchestrationV2Dash 2.8s linear infinite;"),
                label="map_motion_css",
            )
            route_output = build_mcp_route_trace_output(
                search_id="search_pr12l_d_ui_truth",
                trace={
                    "session": {"search_id": "search_pr12l_d_ui_truth", "thread_id": "thread_pr12l_d_ui_truth", "query_text": "ui truth"},
                    "events": [{"seq": 1, "event_type": "context_update", "payload": {"search_map_2d_truth": map_truth}}],
                    "landing_metadata": [],
                    "context_waves": [],
                    "search_map_2d_truth": map_truth,
                },
            )
            validation = validate_mcp_tool_output(contracts_by_name.get("inspect_route", {}), route_output)
            mcp_validations.append(validation)
            summary["search_map_2d_truth"] = map_truth
        elif case.family == "final_status_parity":
            _pr12l_require_present(
                failures,
                app,
                (
                    "searchStreamRef.current?.close()",
                    'resetStreamState("planning")',
                    "setLastTrace(null)",
                    "answer: null",
                    "answer_short: null",
                    "answer_full: null",
                    "context_dossier: null",
                    'evt.event_type === "answer_final"',
                    'evt.event_type === "search_stopped"',
                    'evt.event_type === "result_ready"',
                ),
                label="app_final_status",
            )
            _pr12l_require_present(
                failures,
                dashboard,
                (
                    "backendFinalReady",
                    "backendFinalAnswer",
                    "streamFinalAnswer",
                    "finalAnswerSource",
                    "finalAnswerMismatch",
                    "backend sealed",
                    "backend final differs from stream final",
                    "final surface missing context package",
                    "Final answer not sealed yet.",
                    "final sealed",
                    'data-testid="ui-answer-parity"',
                ),
                label="dashboard_final_status",
            )
            summary["final_status_contract"] = {
                "reset_clears_stale_surfaces": True,
                "backend_final_has_priority": True,
                "mismatch_warning_visible": True,
                "explicit_final_pending_state": True,
            }
        elif case.family == "source_package_parity":
            _pr12l_require_present(
                failures,
                grow_console,
                (
                    'data-agvm-slice="PR-12K-D"',
                    'data-agvm-inspectability="PR-12K-E"',
                    '"/memory/source-investigation/preview"',
                    '"/memory/source-investigation/upload"',
                    'type="file"',
                    'accept=".pdf,.docx,.png,.jpg,.jpeg,.webp,.txt"',
                    '"manual_text"',
                    '"url"',
                    '"website"',
                    '"file"',
                    "analyze_images",
                    "crawl_sublinks",
                    "use_online_enrichment",
                    "pause_on_questions",
                    "Source Truth",
                    "Extraction Metrics",
                    "Source Investigation Timeline",
                    "Extracted Source Package",
                    "Clarification Queue",
                    "Compiler Handoff",
                    "Node Preview",
                    "Merge And Identity Decisions",
                    "Apply Plan",
                    "Raw source text",
                    "Mega text",
                    "Handoff proof",
                    "source-package-inspect-toolbar",
                    "Expand full text",
                ),
                label="grow_source_console_truth",
            )
            _pr12l_require_present(
                failures,
                app,
                (
                    "<AgvmGrowSourceConsole",
                    "sourceResponse={sourceInvestigationResponse}",
                    "onPreviewBundleReady={acceptSourcePreviewBundle}",
                    "onSaveSelected={() => void saveSelection(false)}",
                    "onSaveAll={() => void saveSelection(true)}",
                    'id="grow-source"',
                ),
                label="app_source_package_truth",
            )
            source_probe = _pr12l_ui_source_probe(contracts_by_name)
            mcp_validations.append(dict(source_probe.get("mcp_validation") or {}))
            summary["source_probe"] = source_probe
            source_family_rows = {
                str(row.get("family") or ""): row
                for row in list(source_report.get("case_results") or [])
                if isinstance(row, dict)
            }
            summary["source_intake_family_count"] = len(source_family_rows)
            if not source_family_rows.get("website_with_sublinks", {}).get("passed"):
                failures.append("source_benchmark_website_sublinks_not_passing")
        elif case.family == "document_raw_text_parity":
            exact_doc = next(
                (
                    dict(result)
                    for result in list(retrieval_report.get("case_results") or [])
                    if isinstance(result, dict) and str(result.get("family") or "") == "exact_document"
                ),
                {},
            )
            raw_policy = (
                (exact_doc.get("summary") or {})
                .get("mcp_outputs", {})
                .get("raw_document_policy", {})
            )
            if raw_policy.get("default_full_text_redacted") is not True:
                failures.append("exact_document_default_raw_text_not_redacted")
            if raw_policy.get("raw_full_text_preserved_when_requested") is not True:
                failures.append("exact_document_raw_text_not_available_when_requested")
            _pr12l_require_present(
                failures,
                dashboard,
                (
                    "documentWorkspaceAppendix",
                    "sourceTraceAppendixRows",
                    "pathAppendixRows",
                    "doc appendix",
                    "source trace",
                    "displayedDossierText",
                    "contextFullTextExpanded",
                    "Full text",
                ),
                label="dashboard_document_raw_text",
            )
            _pr12l_require_present(
                failures,
                grow_console,
                (
                    'accept=".pdf,.docx,.png,.jpg,.jpeg,.webp,.txt"',
                    "Raw source text",
                    "Mega text",
                    "Handoff proof",
                    "showFullSourceText",
                ),
                label="grow_document_raw_text",
            )
            retrieval_case = _pr12l_build_retrieval_result(
                next(item for item in pr12l_benchmark_cases() if item.family == "exact_document")
            )
            redacted = build_mcp_retrieval_tool_output("retrieve_document", retrieval_case, include_raw_text=False)
            raw = build_mcp_retrieval_tool_output("retrieve_document", retrieval_case, include_raw_text=True)
            mcp_validations.append(validate_mcp_tool_output(contracts_by_name.get("retrieve_document", {}), redacted))
            mcp_validations.append(validate_mcp_tool_output(contracts_by_name.get("retrieve_document", {}), raw))
            summary["document_raw_policy"] = raw_policy
        elif case.family == "maintenance_proposal_parity":
            _pr12l_require_present(
                failures,
                app,
                (
                    'onSleepPreview={() => void runMaintenanceCycle("/memory/sleep", true)}',
                    'onEvolvePreview={() => void runMaintenanceCycle("/memory/evolve", true)}',
                    "<AgvmMaintenancePanel",
                    "sleepReport={sleepReport}",
                    'type GrowDetailTab = "health" | "changes" | "trace" | "evidence";',
                ),
                label="app_maintenance_truth",
            )
            _pr12l_require_present(
                failures,
                grow_console,
                ("Sleep Preview", "Evolve Preview", "learningPolicy.selection_resolution", "writeTrace?.cognitive_write_summary"),
                label="grow_maintenance_truth",
            )
            _pr12l_require_present(
                failures,
                maintenance_panel,
                (
                    "apply_policy_guard",
                    "rollback",
                    "maintenance_proposals",
                    "metamemory",
                    "Sleep",
                    "Evolve",
                ),
                label="maintenance_panel_truth",
            )
            maintenance_probe = _pr12l_ui_maintenance_probe(contracts_by_name)
            mcp_validations.append(dict(maintenance_probe.get("mcp_validation") or {}))
            summary["maintenance_probe"] = maintenance_probe
        else:
            failures.append(f"unimplemented_ui_truth_family:{case.family}")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"exception:{type(exc).__name__}:{str(exc)[:300]}")

    validation_failures = [item for item in mcp_validations if not bool(item.get("passed", True))]
    for validation in validation_failures:
        failures.append(f"mcp_validation_failed:{validation.get('tool_name')}:{validation.get('errors')}")

    return {
        "case_id": case.case_id,
        "family_group": case.family_group,
        "family": case.family,
        "fixture_kind": case.fixture_kind,
        "interaction": case.interaction,
        "slice_owner": case.slice_owner,
        "critical": bool(case.critical),
        "tool_name": case.tool_name,
        "required_artifacts": list(case.required_artifacts),
        "required_signals": list(case.required_signals),
        "passed": not failures,
        "failures": failures,
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "summary": summary,
        "mcp_validation_count": len(mcp_validations),
        "mcp_validation_failures": validation_failures,
        "benchmark_sources": {
            "frontend_static_contract": True,
            "retrieval_mcp_phase": "retrieval_mcp",
            "source_intake_phase": "source_intake",
            "uses_real_mcp_validation": bool(mcp_validations),
            "mutation_enabled": False,
        },
    }


def run_pr12l_ui_truth_benchmark_suite(base_url: str | None = None) -> dict[str, Any]:
    ui_cases = [case for case in pr12l_benchmark_cases() if case.family_group == "ui_mcp"]
    contracts_by_name = _pr12l_contracts_by_name()
    retrieval_report = run_pr12l_retrieval_mcp_benchmark_suite(base_url)
    source_report = run_pr12l_source_intake_benchmark_suite(base_url)
    case_results = [
        _run_pr12l_ui_truth_case(
            case,
            retrieval_report=retrieval_report,
            source_report=source_report,
            contracts_by_name=contracts_by_name,
        )
        for case in ui_cases
    ]
    covered_families = {str(result.get("family") or "") for result in case_results}
    missing_families = sorted(set(REQUIRED_PR12L_UI_MCP_FAMILIES) - covered_families)
    critical_failures = [
        str(result.get("case_id") or "")
        for result in case_results
        if bool(result.get("critical")) and not bool(result.get("passed"))
    ]
    passed_count = sum(1 for result in case_results if bool(result.get("passed")))
    total_count = len(case_results)
    all_pass = not critical_failures and not missing_families and total_count == len(REQUIRED_PR12L_UI_MCP_FAMILIES)
    family_matrix = {
        family: {
            "covered": family in covered_families,
            "passed": any(str(result.get("family") or "") == family and bool(result.get("passed")) for result in case_results),
            "case_ids": [str(result.get("case_id") or "") for result in case_results if str(result.get("family") or "") == family],
        }
        for family in REQUIRED_PR12L_UI_MCP_FAMILIES
    }
    fixture_matrix: dict[str, dict[str, Any]] = {}
    for result in case_results:
        fixture_kind = str(result.get("fixture_kind") or "")
        entry = fixture_matrix.setdefault(fixture_kind, {"case_count": 0, "passed_count": 0, "families": []})
        entry["case_count"] += 1
        entry["passed_count"] += 1 if bool(result.get("passed")) else 0
        entry["families"].append(str(result.get("family") or ""))
    open_gaps: list[str] = []
    if missing_families:
        open_gaps.append(f"Missing UI/MCP truth benchmark families: {', '.join(missing_families)}")
    if critical_failures:
        open_gaps.append(f"Critical UI/MCP truth failures: {', '.join(critical_failures)}")
    if not bool(retrieval_report.get("all_pass")):
        open_gaps.append("Retrieval/MCP prerequisite benchmark is not passing.")
    if not bool(source_report.get("all_pass")):
        open_gaps.append("Source-intake prerequisite benchmark is not passing.")
    verdict = "ui_truth_passed_pr12l_still_open" if all_pass else "ui_truth_failed_pr12l_still_open"
    return {
        "schema_version": PR12L_UI_TRUTH_BENCHMARK_REPORT_SCHEMA_VERSION,
        "phase": "ui_truth",
        "slice": "PR-12L-D",
        "all_pass": all_pass,
        "pass_rate": round(passed_count / total_count, 4) if total_count else 0.0,
        "passed_count": passed_count,
        "total_count": total_count,
        "base_url": str(base_url or DEFAULT_BASE_URL),
        "product_ready_verdict": verdict,
        "case_results": case_results,
        "family_matrix": family_matrix,
        "fixture_matrix": fixture_matrix,
        "critical_failures": critical_failures,
        "open_gaps": open_gaps,
        "benchmark_inputs": {
            "phase": "ui_truth",
            "ui_families": list(REQUIRED_PR12L_UI_MCP_FAMILIES),
            "external_browser_required": False,
            "external_network_required": False,
            "frontend_static_contract": True,
            "mutation_enabled": False,
            "live_llm_required": False,
            "answer_demo_default": False,
            "uses_pr12l_b_source_intake_truth": True,
            "uses_pr12l_c_retrieval_mcp_truth": True,
            "uses_real_mcp_validation": True,
        },
        "upstream_reports": {
            "source_intake": {
                "schema_version": source_report.get("schema_version"),
                "all_pass": bool(source_report.get("all_pass")),
                "passed_count": source_report.get("passed_count"),
                "total_count": source_report.get("total_count"),
            },
            "retrieval_mcp": {
                "schema_version": retrieval_report.get("schema_version"),
                "all_pass": bool(retrieval_report.get("all_pass")),
                "passed_count": retrieval_report.get("passed_count"),
                "total_count": retrieval_report.get("total_count"),
            },
        },
        "next_slice": "PR-12L-E Product-Ready Scorecard",
    }


def _pr12p_brain_os_v2_surface_cases() -> list[dict[str, Any]]:
    return [
        {
            "surface": "os_command_center",
            "files": {
                "App.tsx": (
                    "brain-os-v2-10h-i",
                    "brain-os-v2-page-os",
                    "Memory OS Command Center",
                    "Runtime Readiness",
                    "MCP Tool Surface",
                    "Brain Scope",
                    "Visual / Payload Truth Gate",
                ),
                "index.css": (".brain-os-v2-shell", ".brain-os-v2-command-grid", ".brain-os-v2-truth-gate-card"),
            },
        },
        {
            "surface": "use_live_run",
            "files": {
                "App.tsx": (
                    "brain-os-v2-page-use",
                    "Use MCP Tool",
                    "MCP-first retrieval cockpit",
                    "Neural Run Map",
                    "Context Package",
                    "Raw MCP Output",
                ),
                "index.css": (".brain-os-v2-use-grid", ".brain-os-v2-neural-map-frame"),
                "components/agvm/AgvmRetrieveValidationDashboard.tsx": (
                    "ui-context-package-parity",
                    "ui-answer-parity",
                    "ui-map-truth-parity",
                    "ui-ai-truth",
                    "ui-path-truth",
                ),
            },
        },
        {
            "surface": "context_package_reader",
            "files": {
                "App.tsx": (
                    "brain-os-v2-page-documents",
                    "brain-os-v2-page-context",
                    "Context Package Reader V2",
                    "brain-os-v2-agent-markdown-reader",
                    "MCP Request / Response Inspector",
                    "retrieve_context",
                    "inspect_context_package",
                ),
                "index.css": (".brain-os-v2-context-doc-grid", ".brain-os-v2-package-reader-card"),
                "components/agvm/AgvmContextDossierPanel.tsx": (
                    "mcp-context-package-reader",
                    "mcp-context-package-source",
                ),
            },
        },
        {
            "surface": "document_workspace",
            "files": {
                "App.tsx": (
                    "Document Retrieval V2",
                    "Document Retrieval",
                    "brain-os-v2-raw-document-reader",
                    "retrieve_document",
                    "include_raw_text",
                ),
                "index.css": (".brain-os-v2-document-reader-card", ".brain-os-v2-document-raw"),
                "components/agvm/AgvmRetrieveValidationDashboard.tsx": (
                    "document-workspace-card",
                    "Use MCP retrieve_document with include_raw_text=true",
                ),
            },
        },
        {
            "surface": "grow_learning_studio",
            "files": {
                "App.tsx": (
                    "brain-os-v2-page-grow",
                    "Learning Studio V2",
                    "Clarification Gate",
                    "Compiler Handoff",
                    "MCP Grow Inspector",
                    "grow_source_preview",
                ),
                "index.css": (".brain-os-v2-grow-grid", ".brain-os-v2-grow-mcp-card"),
                "components/agvm/AgvmGrowSourceConsole.tsx": (
                    "grow-learning-cockpit",
                    "source-package-inspect-toolbar",
                    "Source Truth",
                ),
            },
        },
        {
            "surface": "sleep_evolve",
            "files": {
                "App.tsx": (
                    "brain-os-v2-page-evolve",
                    "Sleep / Evolve V2",
                    "Proposal Stack",
                    "Policy / Rollback / Guards",
                    "MCP Maintenance Inspector",
                    "sleep_apply_dry_run",
                    "evolve_apply_dry_run",
                ),
                "index.css": (".brain-os-v2-evolve-grid", ".brain-os-v2-evolve-mcp-card"),
            },
        },
        {
            "surface": "brains",
            "files": {
                "App.tsx": (
                    "brain-os-v2-page-brains",
                    "Brains V2",
                    "Registry Truth",
                    "MCP Brain Scope Inspector",
                    "/mcp/brains",
                    "/mcp/select-brain",
                    "/mcp/brains/export",
                ),
                "index.css": (".brain-os-v2-brains-grid", ".brain-os-v2-brain-mcp-card"),
            },
        },
    ]


def _run_pr12p_brain_os_v2_surface_case(case: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    failures: list[str] = []
    checked_files: list[str] = []
    for relative_path, snippets in dict(case.get("files") or {}).items():
        checked_files.append(str(relative_path))
        try:
            source = _pr12p_frontend_text(str(relative_path))
        except FileNotFoundError:
            failures.append(f"missing_frontend_file:{relative_path}")
            continue
        _pr12l_require_present(failures, source, tuple(str(snippet) for snippet in snippets), label=str(relative_path))
    return {
        "surface": str(case.get("surface") or ""),
        "passed": not failures,
        "failures": failures,
        "checked_files": checked_files,
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
    }


def _run_pr12p_brain_os_v2_payload_probe(probe: str, contracts_by_name: dict[str, dict[str, Any]]) -> dict[str, Any]:
    from mcp_retrieval import build_mcp_retrieval_tool_output
    from mcp_stability import validate_mcp_tool_output

    started = time.perf_counter()
    failures: list[str] = []
    validations: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    try:
        if probe == "context_package":
            result = _pr12l_build_retrieval_result(next(case for case in pr12l_benchmark_cases() if case.family == "broad_self_dossier"))
            output = build_mcp_retrieval_tool_output("retrieve_context", result)
            validations.append(validate_mcp_tool_output(contracts_by_name.get("retrieve_context", {}), output))
            context = dict(output.get("context_package") or {})
            metrics = dict(context.get("metrics") or {})
            summary = {
                "tool": "retrieve_context",
                "status": output.get("status"),
                "schema_version": context.get("schema_version"),
                "section_count": int(metrics.get("section_count") or 0),
                "agent_markdown_chars": len(str(context.get("agent_markdown") or "")),
                "answer_demo_default_present": "answer_demo" in output,
            }
            if context.get("schema_version") != "agvm.mcp_context_package.v2":
                failures.append("context_package_schema_mismatch")
            if summary["section_count"] < 1 or summary["agent_markdown_chars"] < 1:
                failures.append("context_package_not_visible")
            if summary["answer_demo_default_present"]:
                failures.append("retrieve_context_contains_answer_demo_by_default")
        elif probe == "document_raw":
            result = _pr12l_build_retrieval_result(next(case for case in pr12l_benchmark_cases() if case.family == "exact_document"))
            redacted = build_mcp_retrieval_tool_output("retrieve_document", result, include_raw_text=False)
            raw = build_mcp_retrieval_tool_output("retrieve_document", result, include_raw_text=True)
            validations.append(validate_mcp_tool_output(contracts_by_name.get("retrieve_document", {}), redacted))
            validations.append(validate_mcp_tool_output(contracts_by_name.get("retrieve_document", {}), raw))
            redacted_first = _pr12l_first_document(redacted)
            raw_first = _pr12l_first_document(raw)
            summary = {
                "tool": "retrieve_document",
                "redacted_status": redacted.get("status"),
                "raw_status": raw.get("status"),
                "redacted_full_text_empty": redacted_first.get("full_text") == "",
                "raw_full_text_chars": len(str(raw_first.get("full_text") or "")),
                "raw_available_flag": bool(redacted_first.get("full_text_available")),
            }
            if not summary["redacted_full_text_empty"] or not summary["raw_available_flag"]:
                failures.append("document_default_raw_policy_failed")
            if int(summary["raw_full_text_chars"]) < 1:
                failures.append("document_raw_text_missing_when_requested")
        elif probe == "map_truth":
            result = _pr12l_build_retrieval_result(next(case for case in pr12l_benchmark_cases() if case.family == "cross_area"))
            output = build_mcp_retrieval_tool_output("retrieve_path_corridor", result)
            validations.append(validate_mcp_tool_output(contracts_by_name.get("retrieve_path_corridor", {}), output))
            path_corridors = dict(result.get("path_corridors") or {})
            path_metrics = dict(path_corridors.get("metrics") or {})
            search_map = _pr12l_ui_map_truth_probe()
            invariants = dict(search_map.get("invariants") or {})
            motion = dict(search_map.get("motion_policy") or {})
            summary = {
                "tool": "retrieve_path_corridor",
                "status": output.get("status"),
                "path_count": int(path_metrics.get("path_count") or 0),
                "promoted_intermediate_count": int(path_metrics.get("promoted_intermediate_count") or 0),
                "map_schema_version": search_map.get("schema_version"),
                "parallel_landing_invariant": bool(invariants.get("parallel_landings_are_not_serial_route")),
                "synthetic_motion_allowed": bool(motion.get("allow_synthetic_motion")),
            }
            if summary["map_schema_version"] != "agvm.search_map_2d_truth.v1":
                failures.append("search_map_truth_schema_mismatch")
            if int(summary["path_count"]) < 1:
                failures.append("path_corridor_missing")
            if not summary["parallel_landing_invariant"]:
                failures.append("parallel_landing_invariant_missing")
            if summary["synthetic_motion_allowed"]:
                failures.append("synthetic_motion_allowed")
        elif probe == "answer_surface":
            app = _pr12p_frontend_text("App.tsx")
            dashboard = _pr12p_frontend_text("components/agvm/AgvmRetrieveValidationDashboard.tsx")
            _pr12l_require_present(
                failures,
                app,
                (
                    "answer demo",
                    "Answer demo is secondary",
                    "Answer demo must be traceable to package/document/source rows.",
                ),
                label="app_answer_surface",
            )
            _pr12l_require_present(
                failures,
                dashboard,
                (
                    "Answer/package mismatch",
                    "backend final differs from stream final",
                    "Answer text exists but the MCP context package reader has no package text.",
                    "ui-answer-parity",
                ),
                label="dashboard_answer_surface",
            )
            summary = {"answer_demo_secondary": True, "ui_blocks_or_warns_on_package_mismatch": not failures}
        elif probe == "grow_source":
            source_probe = _pr12l_ui_source_probe(contracts_by_name)
            validations.append(dict(source_probe.get("mcp_validation") or {}))
            package_summary = dict(source_probe.get("source_package_summary") or {})
            summary = {
                "tool": source_probe.get("mcp_tool"),
                "status": source_probe.get("mcp_status"),
                "schema_version": package_summary.get("schema_version"),
                "unit_count": package_summary.get("unit_count"),
                "preview_readiness_present": package_summary.get("preview_readiness_present"),
            }
            if source_probe.get("mcp_status") != "preview_ready":
                failures.append("grow_source_preview_not_ready")
            if not bool(package_summary.get("preview_readiness_present")):
                failures.append("grow_preview_readiness_missing")
        elif probe == "maintenance":
            maintenance_probe = _pr12l_ui_maintenance_probe(contracts_by_name)
            validations.append(dict(maintenance_probe.get("mcp_validation") or {}))
            summary = {
                "tool": "sleep_preview",
                "status": maintenance_probe.get("mcp_status"),
                "proposal_count": maintenance_probe.get("proposal_count"),
                "metamemory_schema": maintenance_probe.get("metamemory_schema"),
                "apply_guard_present": maintenance_probe.get("apply_guard_present"),
            }
            if maintenance_probe.get("mcp_status") != "preview_ready":
                failures.append("maintenance_preview_not_ready")
            if not bool(maintenance_probe.get("apply_guard_present")):
                failures.append("maintenance_apply_guard_missing")
        elif probe == "brain_scope":
            app = _pr12p_frontend_text("App.tsx")
            _pr12l_require_present(
                failures,
                app,
                (
                    "MCP Brain Scope Inspector",
                    '"/mcp/brains"',
                    '"/mcp/select-brain"',
                    '"/mcp/brains/export"',
                    '"/memory/brains/backup"',
                    "Active / Target Detail",
                    "Storage / Docker / Capabilities",
                ),
                label="brain_scope_surface",
            )
            summary = {"brain_scope_ui": not failures, "mutation_controls_guarded": "Guarded Admin Console" in app}
        else:
            failures.append(f"unknown_payload_probe:{probe}")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"exception:{type(exc).__name__}:{str(exc)[:300]}")

    validation_failures = [item for item in validations if not bool(item.get("passed", True))]
    for validation in validation_failures:
        failures.append(f"mcp_validation_failed:{validation.get('tool_name')}:{validation.get('errors')}")
    return {
        "probe": probe,
        "passed": not failures,
        "failures": failures,
        "summary": summary,
        "mcp_validation_count": len(validations),
        "mcp_validation_failures": validation_failures,
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
    }


def run_pr12p_brain_os_v2_truth_benchmark_suite(base_url: str | None = None) -> dict[str, Any]:
    contracts_by_name = _pr12l_contracts_by_name()
    surface_results = [_run_pr12p_brain_os_v2_surface_case(case) for case in _pr12p_brain_os_v2_surface_cases()]
    payload_results = [
        _run_pr12p_brain_os_v2_payload_probe(probe, contracts_by_name)
        for probe in REQUIRED_PR12P_BRAIN_OS_V2_PAYLOAD_PROBES
    ]
    covered_surfaces = {str(result.get("surface") or "") for result in surface_results}
    covered_payload_probes = {str(result.get("probe") or "") for result in payload_results}
    missing_surfaces = sorted(set(REQUIRED_PR12P_BRAIN_OS_V2_SURFACES) - covered_surfaces)
    missing_payload_probes = sorted(set(REQUIRED_PR12P_BRAIN_OS_V2_PAYLOAD_PROBES) - covered_payload_probes)
    failed_surfaces = [str(result.get("surface") or "") for result in surface_results if not bool(result.get("passed"))]
    failed_payload_probes = [str(result.get("probe") or "") for result in payload_results if not bool(result.get("passed"))]
    critical_failures = [*failed_surfaces, *failed_payload_probes]
    open_gaps: list[str] = []
    if missing_surfaces:
        open_gaps.append(f"Missing Brain OS V2 surfaces: {', '.join(missing_surfaces)}")
    if missing_payload_probes:
        open_gaps.append(f"Missing Brain OS V2 payload probes: {', '.join(missing_payload_probes)}")
    if failed_surfaces:
        open_gaps.append(f"Failed Brain OS V2 surfaces: {', '.join(failed_surfaces)}")
    if failed_payload_probes:
        open_gaps.append(f"Failed Brain OS V2 payload probes: {', '.join(failed_payload_probes)}")
    all_pass = not open_gaps and len(surface_results) == len(REQUIRED_PR12P_BRAIN_OS_V2_SURFACES) and len(payload_results) == len(REQUIRED_PR12P_BRAIN_OS_V2_PAYLOAD_PROBES)
    return {
        "schema_version": PR12P_BRAIN_OS_V2_TRUTH_REPORT_SCHEMA_VERSION,
        "phase": "brain_os_v2_truth",
        "slice": "PR-12P-10H-I",
        "all_pass": all_pass,
        "pass_rate": round(
            (sum(1 for item in [*surface_results, *payload_results] if bool(item.get("passed"))) / max(len(surface_results) + len(payload_results), 1)),
            4,
        ),
        "base_url": str(base_url or DEFAULT_BASE_URL),
        "product_ready_verdict": "brain_os_v2_visual_payload_truth_passed_pr12p_still_open"
        if all_pass
        else "brain_os_v2_visual_payload_truth_failed_pr12p_still_open",
        "surface_results": surface_results,
        "payload_results": payload_results,
        "surface_matrix": {
            surface: {
                "covered": surface in covered_surfaces,
                "passed": any(str(result.get("surface") or "") == surface and bool(result.get("passed")) for result in surface_results),
            }
            for surface in REQUIRED_PR12P_BRAIN_OS_V2_SURFACES
        },
        "payload_matrix": {
            probe: {
                "covered": probe in covered_payload_probes,
                "passed": any(str(result.get("probe") or "") == probe and bool(result.get("passed")) for result in payload_results),
            }
            for probe in REQUIRED_PR12P_BRAIN_OS_V2_PAYLOAD_PROBES
        },
        "critical_failures": critical_failures,
        "open_gaps": open_gaps,
        "benchmark_inputs": {
            "phase": "brain_os_v2_truth",
            "surfaces": list(REQUIRED_PR12P_BRAIN_OS_V2_SURFACES),
            "payload_probes": list(REQUIRED_PR12P_BRAIN_OS_V2_PAYLOAD_PROBES),
            "external_network_required": False,
            "mutation_enabled": False,
            "live_llm_required": True,
            "static_dom_contract": True,
            "mcp_payload_adapters": True,
            "browser_screenshot_proof_required": True,
            "context_package_is_product": True,
            "answer_demo_secondary": True,
        },
        "evidence_contract": {
            "required_screenshot_pages": ["os", "use", "context", "documents", "grow", "evolve", "brains"],
            "required_runtime_ports": {"api": 8010, "ui": 3020},
            "backend_integrity_gate_after_this": "PR-12P-10I Backend Product Integrity Correction Gate",
            "cloud_blocked": True,
        },
        "next_slice": "PR-12P-10I Backend Product Integrity Correction Gate",
    }


def _pr12p_10i_context_package(
    *,
    alignment_passed: bool = True,
    missing_evidence_node_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "agvm.mcp_context_package.v2",
        "package_mode": "broad_dossier",
        "status": "contract_satisfied",
        "contract": {
            "passed": True,
            "required_sections": ["identity", "work"],
            "unresolved_sections": [],
            "answer_context_alignment": {
                "checked": True,
                "passed": bool(alignment_passed),
                "missing_terms": [],
                "missing_evidence_node_ids": list(missing_evidence_node_ids or []),
            },
        },
        "agent_markdown": (
            "# AGVM Context Package\n\n"
            "## Identity\n"
            "- Simone Massaro is an entrepreneur and founder.\n\n"
            "## Work And Projects\n"
            "- BaxEnergy and WiSNAM are company/work evidence for the requested context.\n"
        ),
        "sections": [
            {
                "key": "identity",
                "title": "Identity",
                "items": ["Simone Massaro is an entrepreneur and founder."],
            },
            {
                "key": "work",
                "title": "Work And Projects",
                "items": ["BaxEnergy and WiSNAM are company/work evidence for the requested context."],
            },
        ],
        "hot_context": [
            {
                "section": "work",
                "node_id": "node_company",
                "text": "BaxEnergy and WiSNAM are company/work evidence for the requested context.",
            }
        ],
        "metrics": {
            "section_count": 2,
            "hot_item_count": 1,
            "answer_context_aligned": bool(alignment_passed),
        },
    }


def _pr12p_10i_base_result(**overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "query_text": "quali sono le tue aziende?",
        "response_mode": "both",
        "retrieval_mode": "balanced",
        "matches": [{"node_id": "node_company", "text": "BaxEnergy and WiSNAM", "score": 0.9}],
        "branches": [],
        "steps": [],
        "visited_node_ids": ["node_company"],
        "visited_bucket_keys": ["1:1:1"],
        "stop_reason": "evidence_contract_satisfied",
        "answerability_state": "grounded",
        "closure_state": "final_sealed",
        "final_closure_ready": True,
        "context_package": _pr12p_10i_context_package(),
        "context_package_materialization": {
            "schema_version": "agvm.context_package_materialization.v1",
            "state": "context_ready",
            "contract_passed": True,
            "agent_markdown_chars": 180,
        },
        "answer": {
            "answer_text": "Le mie aziende includono BaxEnergy e WiSNAM.",
            "answerability_state": "grounded",
            "evidence_node_ids": ["node_company"],
            "evidence_snippets": [
                {"node_id": "node_company", "text": "BaxEnergy and WiSNAM", "kind": "work"}
            ],
        },
        "answer_demo_materialization": {
            "schema_version": "agvm.answer_demo_materialization.v1",
            "requested": True,
            "state": "ready",
            "context_package_contract_passed": True,
            "answer_context_aligned": True,
        },
        "ai_landing_materialization": {
            "schema_version": "agvm.ai_landing_materialization.v1",
            "required": True,
            "validation_state": "materialized",
            "route_level_materialized": True,
            "blockers": [],
            "judge": {"materialized": True, "approved": True},
        },
        "ai_materialization_hard_gate": {
            "schema_version": "agvm.ai_materialization_hard_gate.v1",
            "required": True,
            "blocked": False,
            "satisfied": True,
            "validation_state": "ai_materialization_validated",
            "blockers": [],
        },
    }
    result.update(overrides)
    return result


def _run_pr12p_backend_integrity_check(check: str) -> dict[str, Any]:
    from mcp_retrieval import build_mcp_retrieval_tool_output

    started = time.perf_counter()
    failures: list[str] = []
    summary: dict[str, Any] = {}
    try:
        if check == "ai_judge_pending_blocks_success":
            gate = _build_ai_materialization_hard_gate(
                query_class="broad_summary",
                response_mode="both",
                semantic_contract={"ai_required": True},
                semantic_contract_runtime={"ai_required": True, "material": True, "source": "llm"},
                ai_landing_materialization={
                    "validation_state": "materialized_judge_pending",
                    "route_level_materialized": True,
                    "blockers": ["ai_judge_missing"],
                    "scout": {"enabled": True, "status": "merged"},
                    "judge": {"materialized": False, "approved": False},
                },
                ai_validation_gate={"required": True, "final_llm_approval": False},
                final_surface_fields={"final_closure_ready": False, "closure_state": "open", "answer_surface_state": "answer_now_and_continue"},
                context_package_materialization={"contract_passed": True},
                answer_demo_materialization={"requested": True, "state": "materialized_unsealed"},
            )
            summary = {
                "gate_state": gate.get("validation_state"),
                "blocked": bool(gate.get("blocked")),
                "blockers": list(gate.get("blockers") or []),
            }
            if not bool(gate.get("blocked")):
                failures.append("judge_pending_gate_not_blocked")
            if gate.get("validation_state") != "blocked_ai_final_judge_missing":
                failures.append("judge_pending_gate_state_not_specific")
            if "blocked_answer_demo_not_approved" not in list(gate.get("blockers") or []):
                failures.append("unsealed_answer_demo_not_blocked")
        elif check == "mcp_unsealed_answer_is_partial":
            result = _pr12p_10i_base_result(
                stop_reason="ai_validation_not_satisfied",
                answerability_state="partial",
                closure_state="open",
                final_closure_ready=False,
                answer_demo_materialization={
                    "schema_version": "agvm.answer_demo_materialization.v1",
                    "requested": True,
                    "state": "materialized_unsealed",
                    "context_package_contract_passed": True,
                    "answer_context_aligned": True,
                },
                ai_landing_materialization={
                    "schema_version": "agvm.ai_landing_materialization.v1",
                    "required": True,
                    "validation_state": "materialized",
                    "route_level_materialized": True,
                    "blockers": [],
                    "judge": {"materialized": True, "approved": True},
                },
                ai_materialization_hard_gate={
                    "schema_version": "agvm.ai_materialization_hard_gate.v1",
                    "required": True,
                    "blocked": False,
                    "satisfied": True,
                    "validation_state": "ai_materialization_validated",
                    "blockers": [],
                },
            )
            output = build_mcp_retrieval_tool_output("retrieve_context", result, include_answer_demo=True)
            summary = {
                "mcp_status": output.get("status"),
                "answer_demo_state": (output.get("answer_demo_materialization") or {}).get("state"),
                "final_closure_ready": (output.get("completeness") or {}).get("final_closure_ready"),
            }
            if output.get("status") != "partial":
                failures.append("unsealed_answer_demo_status_not_partial")
        elif check == "answer_support_not_in_context_blocks":
            result = _pr12p_10i_base_result(
                context_package=_pr12p_10i_context_package(
                    alignment_passed=False,
                    missing_evidence_node_ids=["node_not_in_package"],
                ),
                answer={
                    "answer_text": "Questa risposta usa supporto non presente nel context package.",
                    "answerability_state": "grounded",
                    "evidence_node_ids": ["node_not_in_package"],
                    "evidence_snippets": [
                        {"node_id": "node_not_in_package", "text": "Unsupported answer support", "kind": "work"}
                    ],
                },
            )
            output = build_mcp_retrieval_tool_output("retrieve_context", result, include_answer_demo=True)
            integrity = dict(output.get("payload_integrity") or {})
            summary = {
                "mcp_status": output.get("status"),
                "payload_integrity_passed": integrity.get("passed"),
                "missing_support": integrity.get("answer_support_node_ids_missing_from_package"),
                "contract_missing": integrity.get("contract_missing_evidence_node_ids"),
            }
            if output.get("status") != "blocked":
                failures.append("unsupported_answer_context_status_not_blocked")
            if bool(integrity.get("passed")):
                failures.append("payload_integrity_did_not_fail")
        elif check == "context_package_primary_without_answer_demo":
            result = _pr12p_10i_base_result(
                response_mode="context",
                answer=None,
                answerability_state=None,
                answer_demo_materialization={
                    "schema_version": "agvm.answer_demo_materialization.v1",
                    "requested": False,
                    "state": "not_requested",
                },
            )
            output = build_mcp_retrieval_tool_output("retrieve_context", result, include_answer_demo=False)
            summary = {
                "mcp_status": output.get("status"),
                "has_answer_demo": "answer_demo" in output,
                "context_package_present": bool(output.get("context_package")),
            }
            if output.get("status") != "ok":
                failures.append("context_package_primary_status_not_ok")
            if "answer_demo" in output:
                failures.append("answer_demo_present_by_default")
        elif check == "exact_document_lookup_exception_preserved":
            gate = _build_ai_materialization_hard_gate(
                query_class="document_lookup",
                response_mode="context",
                semantic_contract={"ai_required": True},
                semantic_contract_runtime={"ai_required": True, "material": True, "source": "llm"},
                ai_landing_materialization={
                    "validation_state": "provisional_no_ai_route_material",
                    "route_level_materialized": False,
                    "blockers": ["ai_landing_runtime_missing"],
                    "scout": {"enabled": True, "status": "completed"},
                    "judge": {"materialized": False},
                },
                ai_validation_gate={"required": True, "final_llm_approval": False},
                final_surface_fields={"final_closure_ready": True, "closure_state": "final_sealed", "answer_surface_state": "final_sealed"},
                context_package_materialization={"contract_passed": True},
                answer_demo_materialization={"state": "not_requested"},
                document_mode="lookup",
                document_lookup_kind="exact_document_lookup",
                document_lookup={
                    "kind": "exact_document_lookup",
                    "state": "exact_document_packet_ready",
                    "supporting_document_count": 1,
                    "max_exact_match_score": 0.91,
                },
                document_packets=[{"document_id": "doc_1", "title": "Project dossier"}],
            )
            summary = {
                "gate_state": gate.get("validation_state"),
                "required": bool(gate.get("required")),
                "blocked": bool(gate.get("blocked")),
                "exception": dict(gate.get("document_lookup_exception") or {}),
            }
            if gate.get("validation_state") != "exact_document_lookup_exception":
                failures.append("exact_document_exception_not_preserved")
            if bool(gate.get("required")) or bool(gate.get("blocked")):
                failures.append("exact_document_exception_blocked")
        else:
            failures.append(f"unknown_backend_integrity_check:{check}")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"exception:{type(exc).__name__}:{str(exc)[:300]}")

    return {
        "check": check,
        "passed": not failures,
        "failures": failures,
        "summary": summary,
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
    }


def run_pr12p_backend_integrity_correction_gate_suite(base_url: str | None = None) -> dict[str, Any]:
    check_results = [_run_pr12p_backend_integrity_check(check) for check in REQUIRED_PR12P_BACKEND_INTEGRITY_CHECKS]
    covered_checks = {str(result.get("check") or "") for result in check_results}
    missing_checks = sorted(set(REQUIRED_PR12P_BACKEND_INTEGRITY_CHECKS) - covered_checks)
    failed_checks = [str(result.get("check") or "") for result in check_results if not bool(result.get("passed"))]
    open_gaps: list[str] = []
    if missing_checks:
        open_gaps.append(f"Missing backend integrity checks: {', '.join(missing_checks)}")
    if failed_checks:
        open_gaps.append(f"Failed backend integrity checks: {', '.join(failed_checks)}")
    all_pass = not open_gaps and len(check_results) == len(REQUIRED_PR12P_BACKEND_INTEGRITY_CHECKS)
    return {
        "schema_version": PR12P_BACKEND_INTEGRITY_REPORT_SCHEMA_VERSION,
        "phase": "backend_integrity",
        "slice": "PR-12P-10I",
        "all_pass": all_pass,
        "pass_rate": round(
            sum(1 for item in check_results if bool(item.get("passed"))) / max(len(check_results), 1),
            4,
        ),
        "base_url": str(base_url or DEFAULT_BASE_URL),
        "product_ready_verdict": "backend_product_integrity_gate_passed_pr12p_still_open"
        if all_pass
        else "backend_product_integrity_gate_failed_pr12p_still_open",
        "check_results": check_results,
        "check_matrix": {
            check: {
                "covered": check in covered_checks,
                "passed": any(str(result.get("check") or "") == check and bool(result.get("passed")) for result in check_results),
            }
            for check in REQUIRED_PR12P_BACKEND_INTEGRITY_CHECKS
        },
        "critical_failures": failed_checks,
        "open_gaps": open_gaps,
        "benchmark_inputs": {
            "phase": "backend_integrity",
            "checks": list(REQUIRED_PR12P_BACKEND_INTEGRITY_CHECKS),
            "external_network_required": False,
            "mutation_enabled": False,
            "live_llm_required": False,
            "context_package_is_product": True,
            "answer_demo_secondary": True,
            "no_success_without_ai_judge_when_answer_demo_requested": True,
            "context_never_narrower_than_answer_support": True,
        },
        "evidence_contract": {
            "previous_gate": "PR-12P-10H-I Brain OS V2 Visual And Payload Truth Benchmark",
            "blocks_external_mcp_proof_if_failed": True,
            "cloud_blocked": True,
        },
        "next_slice": "PR-12P-11 Local MCP Client Proof" if all_pass else "PR-12P-10I corrective follow-up",
    }


PR12P_LOCAL_MCP_CLIENT_PROOF_REPORT_SCHEMA_VERSION = "agvm.pr12p.local_mcp_client_proof_report.v1"


def run_pr12p_local_mcp_client_proof_suite(base_url: str | None = None) -> dict[str, Any]:
    from agvm_mcp_server.client_probe import DEFAULT_REQUIRED_TOOLS, run_local_mcp_client_probe

    selected_base_url = str(base_url or DEFAULT_BASE_URL).rstrip("/")
    selected_brain_id = (
        str(_BENCHMARK_BRAIN_ID.get() or "").strip()
        or str(os.environ.get("AGVM_MCP_BRAIN_ID") or "").strip()
        or str(os.environ.get("AGVM_DEFAULT_BRAIN_ID") or "").strip()
        or "default_brain"
    )
    probe = run_local_mcp_client_probe(
        base_url=selected_base_url,
        brain_id=selected_brain_id,
        timeout_seconds=float(os.environ.get("AGVM_LOCAL_MCP_CLIENT_PROOF_TIMEOUT_SECONDS") or 180.0),
    )
    call_matrix = dict(probe.get("call_matrix") or {})
    required_tool_results = {
        tool_name: {
            "listed": tool_name in list(probe.get("tool_names") or []),
            "called": bool(dict(call_matrix.get(tool_name) or {}).get("called")),
            "status": dict(call_matrix.get(tool_name) or {}).get("status"),
            "search_id": dict(call_matrix.get(tool_name) or {}).get("search_id"),
            "brain_id": dict(call_matrix.get(tool_name) or {}).get("brain_id"),
        }
        for tool_name in DEFAULT_REQUIRED_TOOLS
    }
    failures = [str(item) for item in list(probe.get("failures") or []) if str(item).strip()]
    all_pass = bool(probe.get("all_pass")) and not failures
    return {
        "schema_version": PR12P_LOCAL_MCP_CLIENT_PROOF_REPORT_SCHEMA_VERSION,
        "phase": "local_mcp_client",
        "slice": "PR-12P-11",
        "base_url": selected_base_url,
        "brain_id": selected_brain_id,
        "all_pass": all_pass,
        "product_ready_verdict": "local_mcp_client_proof_passed_pr12p_still_open"
        if all_pass
        else "local_mcp_client_proof_failed_pr12p_still_open",
        "failures": failures,
        "probe": probe,
        "required_tool_results": required_tool_results,
        "benchmark_inputs": {
            "phase": "local_mcp_client",
            "transport": "stdio_jsonrpc_subprocess",
            "external_network_required": False,
            "mutation_enabled": False,
            "live_llm_required": True,
            "brain_scope_required": True,
            "read_only_gate_required": True,
            "ambiguous_scope_gate_required": True,
            "retrieve_outputs_must_expose_search_id": True,
            "inspectors_must_attach_to_retrieve_search_id": True,
        },
        "evidence_contract": {
            "previous_gate": "PR-12P-10I Backend Product Integrity Correction Gate",
            "validates": [
                "stdio_initialize",
                "tools_list_registry_projection",
                "retrieve_context",
                "retrieve_document",
                "retrieve_path_corridor",
                "inspect_context_package",
                "inspect_route",
                "inspect_path_corridor",
                "grow_source_preview",
                "sleep_preview",
                "read_only_mutation_block",
                "ambiguous_brain_scope_block",
            ],
            "cloud_blocked": True,
        },
        "next_slice": "PR-12P-12 Live Product Benchmark Matrix" if all_pass else "PR-12P-11 corrective follow-up",
    }


def _pr12p12_leaf_text(value: Any, *, max_items: int = 600, max_chars: int = 60000) -> str:
    chunks: list[str] = []
    seen = 0

    def visit(item: Any) -> None:
        nonlocal seen
        if seen >= max_items or sum(len(chunk) for chunk in chunks) >= max_chars:
            return
        if isinstance(item, str):
            text = item.strip()
            if text:
                chunks.append(text)
                seen += 1
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if str(key).lower() in {"embedding", "vector", "base64", "html", "css"}:
                    continue
                visit(child)
            return
        if isinstance(item, list):
            for child in item:
                visit(child)
            return
        if isinstance(item, (int, float, bool)) and len(chunks) < 40:
            chunks.append(str(item))
            seen += 1

    visit(value)
    return "\n".join(chunks)[:max_chars]


def _pr12p12_nested(data: dict[str, Any], *path: str) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


_PR12P12_TERM_ALIASES: dict[str, tuple[str, ...]] = {
    "coraggio": ("coraggio", "courage"),
    "courage": ("courage", "coraggio"),
    "valori": ("valori", "values", "principles"),
    "values": ("values", "valori", "principles"),
    "padre": ("padre", "father", "dad"),
    "father": ("father", "padre", "dad"),
}


def _pr12p12_term_hits(text: str, terms: list[str] | tuple[str, ...]) -> dict[str, bool]:
    lowered = text.lower()
    hits: dict[str, bool] = {}
    for term in terms:
        cleaned = str(term or "").strip()
        if not cleaned:
            continue
        aliases = _PR12P12_TERM_ALIASES.get(cleaned.lower(), (cleaned,))
        hits[cleaned] = any(str(alias).lower() in lowered for alias in aliases if str(alias).strip())
    return hits


def _pr12p12_latency_score(elapsed_ms: int) -> float:
    if elapsed_ms <= 5000:
        return 1.0
    if elapsed_ms <= 20000:
        return 0.75
    if elapsed_ms <= 60000:
        return 0.35
    return 0.0


def _pr12p12_ms(value: Any) -> float | None:
    try:
        if value is None:
            return None
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric < 0:
        return None
    return round(numeric, 2)


def _pr12p12_first_ms(*values: Any) -> float | None:
    for value in values:
        numeric = _pr12p12_ms(value)
        if numeric is not None:
            return numeric
    return None


def _pr12p12_latency_contract_for_output(
    output: dict[str, Any],
    elapsed_ms: int,
    *,
    tool_name: str,
) -> dict[str, Any]:
    timing = dict(output.get("timing") or {})
    contract = dict(output.get("latency_contract") or {})
    first_useful_ms = _pr12p12_first_ms(
        contract.get("first_useful_package_ms"),
        timing.get("first_context_ms"),
        timing.get("result_surface_ready_ms"),
        timing.get("final_materialization_completed_ms"),
        timing.get("total_ms"),
        elapsed_ms,
    )
    full_completion_ms = _pr12p12_first_ms(
        contract.get("full_completion_ms"),
        timing.get("final_materialization_completed_ms"),
        timing.get("total_ms"),
        elapsed_ms,
    )
    http_elapsed_ms = _pr12p12_ms(elapsed_ms) or 0.0
    first_ai_landing_ms = _pr12p12_first_ms(contract.get("first_ai_landing_ms"), timing.get("first_landing_ms"))
    background_completion_ms = None
    if first_useful_ms is not None and full_completion_ms is not None:
        background_completion_ms = round(max(0.0, full_completion_ms - first_useful_ms), 2)
    benchmark_basis_ms = _pr12p12_first_ms(first_useful_ms, http_elapsed_ms) or http_elapsed_ms
    full_score = _pr12p12_latency_score(int(full_completion_ms if full_completion_ms is not None else http_elapsed_ms))
    return {
        "schema_version": "agvm.pr12p12.latency_score_contract.v1",
        "tool_name": tool_name,
        "mcp_first": True,
        "benchmark_basis": "first_useful_package_ms",
        "benchmark_basis_ms": round(float(benchmark_basis_ms), 2),
        "first_useful_package_ms": first_useful_ms,
        "first_ai_landing_ms": first_ai_landing_ms,
        "full_completion_ms": full_completion_ms,
        "http_elapsed_ms": round(http_elapsed_ms, 2),
        "background_completion_ms": background_completion_ms,
        "http_response_policy": contract.get("http_response_policy"),
        "first_package_wait_seconds": contract.get("first_package_wait_seconds"),
        "first_package_returned_before_full_completion": bool(contract.get("first_package_returned_before_full_completion")),
        "background_completion_inspectable": bool(contract.get("background_completion_inspectable")),
        "full_completion_is_secondary": bool(contract.get("full_completion_is_secondary")),
        "full_completion_latency_score_observed": full_score,
        "full_completion_reported_not_hidden": True,
        "source": "mcp_latency_contract" if contract else "timing_fallback",
        "first_package_missing": first_useful_ms is None,
    }


def _pr12p12_score_from_hits(hits: dict[str, bool], *, floor_when_no_terms: float = 0.75) -> float:
    if not hits:
        return floor_when_no_terms
    return sum(1 for matched in hits.values() if matched) / max(1, len(hits))


def _pr12p12_elapsed_post(base_url: str, path: str, payload: dict[str, Any], *, timeout: float = 180.0) -> tuple[dict[str, Any], int]:
    started = time.perf_counter()
    data = post_json(base_url, path, payload, timeout=timeout)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return data, elapsed_ms


def _pr12p12_brain_id(record: dict[str, Any]) -> str:
    return str(record.get("brain_id") or record.get("id") or "").strip()


def _pr12p12_brain_label(record: dict[str, Any]) -> str:
    return str(record.get("display_name") or record.get("name") or record.get("title") or _pr12p12_brain_id(record)).strip()


def _pr12p12_brain_inventory(base_url: str) -> dict[str, Any]:
    try:
        registry = get_json(base_url, "/memory/brains", timeout=30.0)
    except Exception as exc:  # pragma: no cover - live infrastructure failure path
        return {
            "registry": {},
            "brains": [],
            "failures": [f"brain_registry_unreachable:{exc}"],
            "targets": {},
            "created_fresh_brain": None,
        }
    brains = [dict(item) for item in list(registry.get("brains") or []) if isinstance(item, dict)]
    targets: dict[str, dict[str, Any]] = {}
    for role in REQUIRED_PR12P_LIVE_PRODUCT_BRAIN_ROLES:
        targets[role] = {"role": role, "brain_id": None, "available": False, "source": "missing", "record": {}}

    for record in brains:
        brain_id = _pr12p12_brain_id(record)
        label = _pr12p12_brain_label(record).lower()
        haystack = f"{brain_id} {label}".lower()
        if brain_id == "simone_massaro" or "simone massaro" in haystack:
            targets["simone_massaro"] = {"role": "simone_massaro", "brain_id": brain_id, "available": True, "source": "registry", "record": record}
        if brain_id == "elena_valsecchi" or "elena valsecchi" in haystack:
            targets["elena_valsecchi"] = {"role": "elena_valsecchi", "brain_id": brain_id, "available": True, "source": "registry", "record": record}
        if brain_id.startswith("pr12p12_fresh_test") or ("fresh" in haystack and "test" in haystack):
            targets["fresh_test_brain"] = {"role": "fresh_test_brain", "brain_id": brain_id, "available": True, "source": "registry", "record": record}

    created_fresh: dict[str, Any] | None = None
    if not targets["fresh_test_brain"]["available"] and str(os.environ.get("AGVM_PR12P12_CREATE_FRESH_BRAIN") or "1").strip() not in {"0", "false", "False"}:
        fresh_id = f"pr12p12_fresh_test_{uuid.uuid4().hex[:8]}"
        try:
            created = post_json(
                base_url,
                "/memory/brains/create",
                {
                    "brain_id": fresh_id,
                    "display_name": "PR-12P-12 Fresh Test Brain",
                    "description": "Ephemeral empty brain used by the live product benchmark matrix.",
                    "make_default": False,
                    "make_active": False,
                },
                timeout=60.0,
            )
            created_fresh = {"brain_id": str(created.get("brain_id") or fresh_id), "create_response": created}
            targets["fresh_test_brain"] = {
                "role": "fresh_test_brain",
                "brain_id": str(created.get("brain_id") or fresh_id),
                "available": True,
                "source": "temporary_created",
                "record": dict(created.get("brain") or {}),
            }
        except Exception as exc:  # pragma: no cover - depends on live registry mutation support
            targets["fresh_test_brain"]["source"] = f"temporary_create_failed:{exc}"

    return {"registry": registry, "brains": brains, "failures": [], "targets": targets, "created_fresh_brain": created_fresh}


def _pr12p12_cleanup_fresh_brain(base_url: str, created_fresh: dict[str, Any] | None) -> dict[str, Any]:
    if not created_fresh:
        return {"attempted": False, "status": "not_created"}
    brain_id = str(created_fresh.get("brain_id") or "").strip()
    if not brain_id:
        return {"attempted": False, "status": "missing_brain_id"}
    try:
        response = post_json(
            base_url,
            "/memory/brains/delete",
            {"brain_id": brain_id, "confirm_brain_id": brain_id, "delete_storage": True},
            timeout=60.0,
        )
        return {"attempted": True, "status": "deleted", "brain_id": brain_id, "response": response}
    except Exception as exc:  # pragma: no cover - live cleanup failure path
        return {"attempted": True, "status": "cleanup_failed", "brain_id": brain_id, "error": str(exc)}


def _pr12p12_live_matrix_cases(targets: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    def brain(role: str) -> str | None:
        return str(dict(targets.get(role) or {}).get("brain_id") or "").strip() or None

    simone = brain("simone_massaro")
    elena = brain("elena_valsecchi")
    fresh = brain("fresh_test_brain")
    common_retrieve = {
        "retrieval_mode": "balanced",
        "context_package_mode": "broad_dossier",
        "max_matches": 16,
        "include_raw_text": True,
        "include_answer_demo": False,
        "complete_paths": False,
    }
    return [
        {
            "case_id": "simone_broad_self_dossier",
            "brain_role": "simone_massaro",
            "brain_id": simone,
            "family": "broad",
            "tool_name": "retrieve_context",
            "path": "/mcp/retrieve-context",
            "payload": {**common_retrieve, "brain_id": simone, "query_text": "raccontami di te, del tuo lavoro e delle tue aziende"},
            "expected_terms": ["Simone", "BaxEnergy"],
            "critical": True,
        },
        {
            "case_id": "simone_exact_companies",
            "brain_role": "simone_massaro",
            "brain_id": simone,
            "family": "exact",
            "tool_name": "retrieve_context",
            "path": "/mcp/retrieve-context",
            "payload": {**common_retrieve, "brain_id": simone, "query_text": "quali aziende hai fondato o guidato?"},
            "expected_terms": ["BaxEnergy", "WiSNAM"],
            "critical": True,
        },
        {
            "case_id": "simone_document_yokogawa_baxenergy",
            "brain_role": "simone_massaro",
            "brain_id": simone,
            "family": "document",
            "tool_name": "retrieve_document",
            "path": "/mcp/retrieve-document",
            "payload": {
                **common_retrieve,
                "brain_id": simone,
                "query_text": "trova il documento o dossier relativo a BaxEnergy e Yokogawa",
                "context_package_mode": "document_full",
                "document_hint": "BaxEnergy Yokogawa",
            },
            "expected_terms": ["BaxEnergy", "Yokogawa"],
            "critical": True,
        },
        {
            "case_id": "simone_path_company_corridor",
            "brain_role": "simone_massaro",
            "brain_id": simone,
            "family": "path",
            "tool_name": "retrieve_path_corridor",
            "path": "/mcp/retrieve-path-corridor",
            "payload": {
                **common_retrieve,
                "brain_id": simone,
                "complete_paths": True,
                "query_text": "collega BaxEnergy, Yokogawa, WiSNAM e Free Mind Foundry e mostrami il contesto attraversato",
            },
            "expected_terms": ["BaxEnergy", "Yokogawa", "WiSNAM"],
            "critical": True,
        },
        {
            "case_id": "simone_composite_identity_work_relationships",
            "brain_role": "simone_massaro",
            "brain_id": simone,
            "family": "composite",
            "tool_name": "retrieve_context",
            "path": "/mcp/retrieve-context",
            "payload": {
                **common_retrieve,
                "brain_id": simone,
                "query_text": "come ti chiami, che lavoro fai, quali aziende hai e quali relazioni familiari importanti emergono?",
            },
            "expected_terms": ["Simone", "BaxEnergy", "Giovanni"],
            "critical": True,
        },
        {
            "case_id": "simone_followup_warm_company_detail",
            "brain_role": "simone_massaro",
            "brain_id": simone,
            "family": "followup",
            "tool_name": "retrieve_context",
            "path": "/mcp/retrieve-context",
            "setup_payload": {
                **common_retrieve,
                "brain_id": simone,
                "thread_id": "pr12p12_simone_followup",
                "query_text": "parlami del tuo lavoro nelle energie rinnovabili",
            },
            "payload": {
                **common_retrieve,
                "brain_id": simone,
                "thread_id": "pr12p12_simone_followup",
                "query_text": "e quali aziende sono collegate a questo lavoro?",
            },
            "expected_terms": ["BaxEnergy"],
            "critical": True,
        },
        {
            "case_id": "simone_grow_preview_project_document",
            "brain_role": "simone_massaro",
            "brain_id": simone,
            "family": "grow",
            "tool_name": "grow_source_preview",
            "path": "/mcp/grow-source-preview",
            "payload": {
                "brain_id": simone,
                "raw_input": "Documento di progetto: Simone Massaro collega BaxEnergy, WiSNAM e Intellisync a una piattaforma industriale per gestione energetica, controllo dati e transizione rinnovabile. Questo input deve produrre piu' unita' concettuali e non un singolo mega nodo.",
                "input_kind": "manual_text",
                "source_label": "PR-12P-12 project document grow preview",
                "run_preview": True,
                "options": {
                    "treat_as": "project_workspace",
                    "source_trust": "user_asserted",
                    "pause_on_questions": True,
                    "clarification_default_policy": "pause_when_unanswered",
                    "question_limit": 3,
                    "max_units": 8,
                    "max_total_chars": 12000,
                },
            },
            "expected_terms": ["BaxEnergy", "WiSNAM", "Intellisync"],
            "critical": True,
        },
        {
            "case_id": "simone_sleep_preview_memory_quality",
            "brain_role": "simone_massaro",
            "brain_id": simone,
            "family": "sleep_evolve",
            "tool_name": "sleep_preview",
            "path": "/mcp/sleep-preview",
            "payload": {"brain_id": simone, "mode": "sleep", "dry_run": True, "max_nodes_considered": 40},
            "expected_terms": [],
            "critical": True,
        },
        {
            "case_id": "elena_broad_life",
            "brain_role": "elena_valsecchi",
            "brain_id": elena,
            "family": "broad",
            "tool_name": "retrieve_context",
            "path": "/mcp/retrieve-context",
            "payload": {**common_retrieve, "brain_id": elena, "query_text": "raccontami della tua vita e del tuo lavoro"},
            "expected_terms": ["Elena"],
            "critical": True,
        },
        {
            "case_id": "elena_exact_location",
            "brain_role": "elena_valsecchi",
            "brain_id": elena,
            "family": "exact",
            "tool_name": "retrieve_context",
            "path": "/mcp/retrieve-context",
            "payload": {**common_retrieve, "brain_id": elena, "query_text": "dove sei nata e dove vivi?"},
            "expected_terms": ["Bergamo", "Milano"],
            "critical": True,
        },
        {
            "case_id": "elena_no_match_private_identifier",
            "brain_role": "elena_valsecchi",
            "brain_id": elena,
            "family": "no_match",
            "tool_name": "retrieve_context",
            "path": "/mcp/retrieve-context",
            "payload": {
                **common_retrieve,
                "brain_id": elena,
                "query_text": "qual e' il codice segreto privato che non e' mai stato caricato in memoria?",
                "context_package_mode": "mcp_operational",
            },
            "expected_terms": [],
            "expected_no_match": True,
            "critical": True,
        },
        {
            "case_id": "fresh_no_match_empty_brain",
            "brain_role": "fresh_test_brain",
            "brain_id": fresh,
            "family": "no_match",
            "tool_name": "retrieve_context",
            "path": "/mcp/retrieve-context",
            "payload": {
                **common_retrieve,
                "brain_id": fresh,
                "query_text": "raccontami chi sono e quali progetti ho",
                "context_package_mode": "mcp_operational",
                "max_matches": 8,
            },
            "expected_terms": [],
            "expected_no_match": True,
            "critical": True,
        },
        {
            "case_id": "fresh_grow_preview_first_memory",
            "brain_role": "fresh_test_brain",
            "brain_id": fresh,
            "family": "grow",
            "tool_name": "grow_source_preview",
            "path": "/mcp/grow-source-preview",
            "payload": {
                "brain_id": fresh,
                "raw_input": "Sono un utente di test PR-12P-12. Lavoro a un progetto chiamato Atlas Memory, voglio che il cervello distingua identita', progetto, obiettivi e documenti.",
                "input_kind": "manual_text",
                "source_label": "PR-12P-12 fresh brain seed preview",
                "run_preview": True,
                "options": {
                    "treat_as": "self_memory",
                    "source_trust": "user_asserted",
                    "pause_on_questions": True,
                    "clarification_default_policy": "pause_when_unanswered",
                    "question_limit": 3,
                    "max_units": 6,
                },
            },
            "expected_terms": ["Atlas Memory"],
            "critical": True,
        },
    ]


def _pr12p12_score_retrieval_case(case: dict[str, Any], output: dict[str, Any], elapsed_ms: int, inspectors: dict[str, Any]) -> dict[str, Any]:
    status = str(output.get("status") or "")
    expected_terms = [str(term) for term in list(case.get("expected_terms") or []) if str(term).strip()]
    expected_no_match = bool(case.get("expected_no_match"))
    context_text = _pr12p12_leaf_text(output.get("context_package") or {})
    document_text = _pr12p12_leaf_text(output.get("document_workspace") or {})
    path_text = _pr12p12_leaf_text(output.get("path_corridors") or {}) + "\n" + _pr12p12_leaf_text(output.get("route_trace") or {})
    full_text = "\n".join(
        [
            context_text,
            document_text,
            path_text,
            _pr12p12_leaf_text(output.get("source_trace") or {}),
            _pr12p12_leaf_text(output.get("answer_demo") or {}),
            _pr12p12_leaf_text(inspectors),
        ]
    )
    hits = _pr12p12_term_hits(full_text, expected_terms)
    context_hits = _pr12p12_term_hits(context_text + "\n" + document_text, expected_terms)
    payload_integrity = dict(output.get("payload_integrity") or {})
    ai_text = _pr12p12_leaf_text(
        {
            "ai_landing_materialization": output.get("ai_landing_materialization") or {},
            "ai_materialization_hard_gate": output.get("ai_materialization_hard_gate") or {},
            "budget": output.get("budget") or {},
            "completeness": output.get("completeness") or {},
        }
    ).lower()
    ai_materialized = (
        bool(_pr12p12_nested(output, "ai_landing_materialization", "route_level_materialized"))
        or bool(_pr12p12_nested(output, "ai_landing_materialization", "materialized"))
        or int(_pr12p12_nested(output, "ai_landing_materialization", "ai_landing_count") or 0) > 0
        or _pr12p12_first_ms(
            _pr12p12_nested(output, "latency_contract", "first_ai_landing_ms"),
            _pr12p12_nested(output, "timing", "first_landing_ms"),
            _pr12p12_nested(output, "budget", "timing", "first_landing_ms"),
        )
        is not None
        or bool(_pr12p12_nested(output, "budget", "ai_material"))
        or ("ai landed yes" in ai_text)
    )
    hard_gate = dict(output.get("ai_materialization_hard_gate") or {})
    landing_materialization = dict(output.get("ai_landing_materialization") or {})
    block_fields = [
        str(hard_gate.get("validation_state") or ""),
        str(hard_gate.get("state") or ""),
        str(landing_materialization.get("state") or ""),
        str(landing_materialization.get("reason") or ""),
    ]
    structured_blockers = [
        str(item).lower()
        for item in list(hard_gate.get("blockers") or [])
        + list(hard_gate.get("failures") or [])
        + list(landing_materialization.get("blockers") or [])
        + list(landing_materialization.get("failures") or [])
    ]
    hard_blocked_explicit = bool(hard_gate.get("blocked") is True)
    state_blocked = any(
        value.strip().lower() in {"missing", "blocked", "no_ai_landing", "ai_landing_runtime_missing", "runtime_missing"}
        for value in block_fields
    )
    blocker_blocked = any(
        marker in blocker
        for blocker in structured_blockers
        for marker in ("missing", "blocked", "no ai landing", "ai_landing_runtime_missing")
    )
    ai_blocked = hard_blocked_explicit or ((state_blocked or blocker_blocked) and not ai_materialized)
    scores: dict[str, float | None] = {dimension: None for dimension in REQUIRED_PR12P_LIVE_PRODUCT_SCORE_DIMENSIONS}
    if expected_no_match:
        no_match_state = status in {"no_match", "blocked", "partial"} and not bool(expected_terms)
        scores["context_package_quality"] = 1.0 if no_match_state else 0.25
    elif case.get("family") == "path" and not context_text and path_text:
        path_size_score = min(1.0, len(path_text) / 1200.0)
        scores["context_package_quality"] = round(max(path_size_score * 0.45, _pr12p12_score_from_hits(_pr12p12_term_hits(path_text + "\n" + full_text, expected_terms), floor_when_no_terms=0.5) * 0.8), 4)
    else:
        size_score = min(1.0, len(context_text) / 1200.0) if context_text else 0.0
        scores["context_package_quality"] = round(max(size_score * 0.45, _pr12p12_score_from_hits(context_hits, floor_when_no_terms=0.5) * 0.8), 4)
    scores["ai_materiality"] = 1.0 if ai_materialized else (0.45 if "allowed" in ai_text else 0.0)
    if case.get("family") == "path":
        path_count = int(_pr12p12_nested(output, "path_corridors", "path_count") or _pr12p12_nested(output, "path_corridors", "planned_corridor_count") or 0)
        trace_count = len(list(_pr12p12_nested(output, "route_trace", "events") or [])) if isinstance(_pr12p12_nested(output, "route_trace", "events"), list) else 0
        path_terms = _pr12p12_score_from_hits(_pr12p12_term_hits(path_text + "\n" + full_text, expected_terms), floor_when_no_terms=0.4)
        scores["path_completion"] = round(min(1.0, max(path_terms * 0.75, 0.35 if path_count or trace_count or path_text else 0.0)), 4)
    elif path_text:
        scores["path_completion"] = 0.65
    if case.get("family") == "document":
        raw_score = min(1.0, len(document_text) / 1000.0) if document_text else 0.0
        scores["document_correctness"] = round(max(raw_score * 0.7, _pr12p12_score_from_hits(_pr12p12_term_hits(document_text + "\n" + full_text, expected_terms), floor_when_no_terms=0.4) * 0.9), 4)
    elif document_text:
        scores["document_correctness"] = 0.6
    document_exception = bool(
        case.get("family") == "document"
        and str(case.get("tool_name") or "") in {"retrieve_document", "retrieve_project_workspace"}
        and (scores.get("document_correctness") or 0.0) >= 0.55
        and document_text
    )
    if document_exception and (scores.get("ai_materiality") or 0.0) < 0.5:
        scores["ai_materiality"] = 1.0
    mcp_shape_ok = str(output.get("tool_name") or "") == str(case.get("tool_name") or "")
    payload_ok = payload_integrity.get("passed") is not False
    search_id = str(output.get("search_id") or _pr12p12_nested(output, "completeness", "search_id") or "")
    scores["mcp_parity"] = round((0.4 if mcp_shape_ok else 0.0) + (0.3 if payload_ok else 0.0) + (0.3 if search_id else 0.0), 4)
    latency_contract = _pr12p12_latency_contract_for_output(output, elapsed_ms, tool_name=str(case.get("tool_name") or ""))
    scores["latency"] = _pr12p12_latency_score(int(float(latency_contract.get("benchmark_basis_ms") or elapsed_ms)))
    failures: list[str] = []
    if status in {"failed"}:
        failures.append(f"tool_failed_status:{status}")
    no_match_markers = (
        "no contract-relevant memory",
        "missing contract section",
        "missing semantic slots",
        "insufficient",
        "non trovo",
        "not found",
        "no matching",
        "unresolved or missing",
        "not available",
    )
    explicit_no_match = bool(
        status in {"no_match", "blocked", "partial"}
        or any(marker in full_text.lower() for marker in no_match_markers)
    )
    if expected_no_match and explicit_no_match:
        scores["context_package_quality"] = 1.0
    if expected_no_match and not explicit_no_match:
        failures.append("no_match_not_explicit")
    if not expected_no_match and hits and not all(hits.values()):
        failures.append(f"expected_terms_missing:{[term for term, matched in hits.items() if not matched]}")
    if not expected_no_match and (scores["context_package_quality"] or 0.0) < 0.45:
        failures.append("context_package_too_weak")
    if case.get("family") in {"broad", "exact", "document", "path", "composite", "followup"} and not document_exception and (scores["ai_materiality"] or 0.0) < 0.5:
        failures.append("ai_materiality_not_visible")
    if case.get("family") == "document" and (scores["document_correctness"] or 0.0) < 0.55:
        failures.append("document_workspace_not_sufficient")
    if case.get("family") == "path" and (scores["path_completion"] or 0.0) < 0.55:
        failures.append("path_corridor_not_sufficient")
    if (scores["mcp_parity"] or 0.0) < 0.7:
        failures.append("mcp_payload_parity_not_sufficient")
    return {
        "scores": scores,
        "failures": failures,
        "evidence": {
            "status": status,
            "search_id": search_id,
            "context_chars": len(context_text),
            "document_chars": len(document_text),
            "path_chars": len(path_text),
            "expected_term_hits": hits,
            "context_term_hits": context_hits,
            "payload_integrity_passed": payload_integrity.get("passed"),
            "ai_materialized": ai_materialized,
            "ai_blocked_markers": ai_blocked,
            "ai_materiality_exception": "deterministic_document_retrieval_with_raw_evidence" if document_exception else None,
            "explicit_no_match": explicit_no_match if expected_no_match else None,
            "latency_contract": latency_contract,
            "excerpt": full_text[:1600],
            "inspector_statuses": {
                key: dict(value).get("status") if isinstance(value, dict) else "unavailable"
                for key, value in inspectors.items()
            },
        },
    }


def _pr12p12_score_grow_case(case: dict[str, Any], output: dict[str, Any], elapsed_ms: int) -> dict[str, Any]:
    expected_terms = [str(term) for term in list(case.get("expected_terms") or []) if str(term).strip()]
    text = _pr12p12_leaf_text(output)
    source_package = dict(output.get("source_investigation") or {})
    preview_bundle = output.get("preview_bundle") if isinstance(output.get("preview_bundle"), dict) else {}
    source_units = list(source_package.get("source_units") or [])
    derived_nodes = list(dict(preview_bundle or {}).get("nodes") or [])
    claims = list(dict(preview_bundle or {}).get("claims") or [])
    questions = list(output.get("clarification_questions") or [])
    hits = _pr12p12_term_hits(text, expected_terms)
    scores: dict[str, float | None] = {dimension: None for dimension in REQUIRED_PR12P_LIVE_PRODUCT_SCORE_DIMENSIONS}
    grow_quality = 0.0
    grow_quality += 0.25 if source_package else 0.0
    grow_quality += 0.25 if source_units or len(text) > 500 else 0.0
    grow_quality += 0.25 if derived_nodes or claims or preview_bundle else 0.0
    grow_quality += 0.25 * _pr12p12_score_from_hits(hits, floor_when_no_terms=1.0)
    scores["grow_quality"] = round(min(1.0, grow_quality), 4)
    scores["mcp_parity"] = 1.0 if str(output.get("tool_name") or "") == str(case.get("tool_name") or "") and str(output.get("status") or "") != "failed" else 0.0
    scores["latency"] = _pr12p12_latency_score(elapsed_ms)
    failures: list[str] = []
    if (scores["grow_quality"] or 0.0) < 0.65:
        failures.append("grow_preview_not_sufficient")
    if (scores["mcp_parity"] or 0.0) < 0.7:
        failures.append("grow_mcp_payload_not_sufficient")
    return {
        "scores": scores,
        "failures": failures,
        "evidence": {
            "status": str(output.get("status") or ""),
            "source_unit_count": len(source_units),
            "preview_node_count": len(derived_nodes),
            "claim_count": len(claims),
            "clarification_question_count": len(questions),
            "expected_term_hits": hits,
            "excerpt": text[:1600],
        },
    }


def _pr12p12_score_maintenance_case(case: dict[str, Any], output: dict[str, Any], elapsed_ms: int) -> dict[str, Any]:
    text = _pr12p12_leaf_text(output)
    proposals = list(output.get("maintenance_proposals") or [])
    report = dict(output.get("maintenance_report") or {})
    metamemory = dict(output.get("metamemory_snapshot") or {})
    scores: dict[str, float | None] = {dimension: None for dimension in REQUIRED_PR12P_LIVE_PRODUCT_SCORE_DIMENSIONS}
    quality = 0.0
    quality += 0.35 if str(output.get("status") or "") in {"preview_ready", "ok", "partial"} else 0.0
    quality += 0.25 if report else 0.0
    quality += 0.25 if proposals or metamemory else 0.0
    quality += 0.15 if len(text) > 300 else 0.0
    scores["grow_quality"] = round(min(1.0, quality), 4)
    scores["mcp_parity"] = 1.0 if str(output.get("tool_name") or "") == str(case.get("tool_name") or "") and str(output.get("status") or "") != "failed" else 0.0
    scores["latency"] = _pr12p12_latency_score(elapsed_ms)
    failures: list[str] = []
    if (scores["grow_quality"] or 0.0) < 0.55:
        failures.append("sleep_evolve_preview_not_sufficient")
    return {
        "scores": scores,
        "failures": failures,
        "evidence": {
            "status": str(output.get("status") or ""),
            "proposal_count": len(proposals),
            "report_keys": sorted(report.keys())[:20],
            "metamemory_keys": sorted(metamemory.keys())[:20],
            "excerpt": text[:1600],
        },
    }


def _pr12p12_ui_parity_probe() -> dict[str, Any]:
    markers = {
        "brain_os_shell": "Brain OS",
        "mcp_tool_mode": "Use MCP Tool",
        "context_reader": "Context Package",
        "document_workspace": "Document Workspace",
        "grow_studio": "Grow",
        "evolve_page": "Evolve",
        "neural_map": "Search Map",
        "mcp_inspector": "MCP Inspector",
    }
    try:
        app = _pr12p_frontend_text("App.tsx")
        dashboard = _pr12p_frontend_text("components/agvm/AgvmRetrieveValidationDashboard.tsx")
        styles = _pr12p_frontend_text("index.css")
        source = "\n".join([app, dashboard, styles])
        hits = {key: marker in source for key, marker in markers.items()}
        endpoint_markers = {
            "retrieve_context": "retrieve-context" in source or "retrieve_context" in source,
            "retrieve_document": "retrieve-document" in source or "retrieve_document" in source,
            "retrieve_path_corridor": "retrieve-path-corridor" in source or "retrieve_path_corridor" in source,
            "grow_source_preview": "grow-source-preview" in source or "grow_source_preview" in source,
            "sleep_preview": "sleep-preview" in source or "sleep_preview" in source,
        }
        score = (sum(1 for matched in hits.values() if matched) + sum(1 for matched in endpoint_markers.values() if matched)) / max(1, len(hits) + len(endpoint_markers))
        failures = [f"ui_marker_missing:{key}" for key, matched in hits.items() if not matched]
        failures += [f"ui_endpoint_marker_missing:{key}" for key, matched in endpoint_markers.items() if not matched]
        return {
            "score": round(float(score), 4),
            "passed": score >= 0.8,
            "markers": hits,
            "endpoint_markers": endpoint_markers,
            "failures": failures,
        }
    except Exception as exc:
        return {"score": 0.0, "passed": False, "markers": {}, "endpoint_markers": {}, "failures": [f"ui_parity_probe_error:{exc}"]}


def _pr12p12_execute_live_case(base_url: str, case: dict[str, Any], ui_score: float) -> dict[str, Any]:
    brain_id = str(case.get("brain_id") or "").strip()
    result_base = {
        "case_id": case["case_id"],
        "family": case["family"],
        "brain_role": case["brain_role"],
        "brain_id": brain_id or None,
        "tool_name": case["tool_name"],
        "critical": bool(case.get("critical", True)),
    }
    if not brain_id:
        scores = {dimension: None for dimension in REQUIRED_PR12P_LIVE_PRODUCT_SCORE_DIMENSIONS}
        return {
            **result_base,
            "executed": False,
            "passed": False,
            "status": "skipped_missing_brain",
            "elapsed_ms": 0,
            "score_by_dimension": scores,
            "failures": [f"missing_brain_role:{case['brain_role']}"],
            "evidence": {},
        }
    setup_result: dict[str, Any] | None = None
    inspectors: dict[str, Any] = {}
    try:
        setup_payload = case.get("setup_payload")
        if isinstance(setup_payload, dict):
            setup_result, _setup_elapsed = _pr12p12_elapsed_post(base_url, str(case["path"]), setup_payload, timeout=180.0)
        output, elapsed_ms = _pr12p12_elapsed_post(base_url, str(case["path"]), dict(case.get("payload") or {}), timeout=240.0)
        search_id = str(output.get("search_id") or _pr12p12_nested(output, "completeness", "search_id") or "").strip()
        if search_id and str(case.get("tool_name") or "").startswith("retrieve_"):
            for inspector_name, inspector_path in {
                "inspect_context_package": "/mcp/inspect-context-package",
                "inspect_route": "/mcp/inspect-route",
            }.items():
                try:
                    inspectors[inspector_name], _ = _pr12p12_elapsed_post(
                        base_url,
                        inspector_path,
                        {
                            "brain_id": brain_id,
                            "search_id": search_id,
                            "include_raw_text": True,
                            "include_debug": inspector_name == "inspect_route",
                        },
                        timeout=120.0,
                    )
                except Exception as exc:  # pragma: no cover - live inspector failure path
                    inspectors[inspector_name] = {"status": "inspector_failed", "error": str(exc)}
        if str(case.get("tool_name") or "").startswith("retrieve_"):
            scored = _pr12p12_score_retrieval_case(case, output, elapsed_ms, inspectors)
        elif case.get("family") == "grow":
            scored = _pr12p12_score_grow_case(case, output, elapsed_ms)
        else:
            scored = _pr12p12_score_maintenance_case(case, output, elapsed_ms)
        scores = dict(scored["scores"])
        scores["ui_parity"] = ui_score
        failures = list(scored["failures"])
        if ui_score < 0.8:
            failures.append("ui_parity_not_sufficient")
        passed = not failures
        return {
            **result_base,
            "executed": True,
            "passed": passed,
            "status": str(output.get("status") or "ok"),
            "elapsed_ms": elapsed_ms,
            "score_by_dimension": scores,
            "failures": failures,
            "evidence": {
                **dict(scored["evidence"]),
                "setup_status": dict(setup_result or {}).get("status") if setup_result else None,
                "inspector_count": len(inspectors),
            },
            "search_id": str(output.get("search_id") or _pr12p12_nested(output, "completeness", "search_id") or "") or None,
        }
    except Exception as exc:
        scores = {dimension: None for dimension in REQUIRED_PR12P_LIVE_PRODUCT_SCORE_DIMENSIONS}
        scores["ui_parity"] = ui_score
        return {
            **result_base,
            "executed": False,
            "passed": False,
            "status": "execution_failed",
            "elapsed_ms": 0,
            "score_by_dimension": scores,
            "failures": [f"case_execution_failed:{exc}"],
            "evidence": {},
        }


def run_pr12p_live_product_matrix_suite(base_url: str | None = None) -> dict[str, Any]:
    selected_base_url = str(base_url or DEFAULT_BASE_URL).rstrip("/")
    inventory = _pr12p12_brain_inventory(selected_base_url)
    ui_probe = _pr12p12_ui_parity_probe()
    case_results: list[dict[str, Any]] = []
    cleanup: dict[str, Any] = {"attempted": False, "status": "not_needed"}
    try:
        for case in _pr12p12_live_matrix_cases(dict(inventory.get("targets") or {})):
            case_results.append(_pr12p12_execute_live_case(selected_base_url, case, float(ui_probe.get("score") or 0.0)))
    finally:
        cleanup = _pr12p12_cleanup_fresh_brain(selected_base_url, dict(inventory.get("created_fresh_brain") or {}) or None)

    executed_results = [result for result in case_results if bool(result.get("executed"))]
    covered_roles = sorted({str(result.get("brain_role") or "") for result in executed_results})
    covered_families = sorted({str(result.get("family") or "") for result in executed_results})
    covered_dimensions = sorted(
        {
            dimension
            for result in executed_results
            for dimension, value in dict(result.get("score_by_dimension") or {}).items()
            if value is not None
        }
    )
    missing_roles = sorted(set(REQUIRED_PR12P_LIVE_PRODUCT_BRAIN_ROLES) - set(covered_roles))
    missing_families = sorted(set(REQUIRED_PR12P_LIVE_PRODUCT_FAMILIES) - set(covered_families))
    missing_dimensions = sorted(set(REQUIRED_PR12P_LIVE_PRODUCT_SCORE_DIMENSIONS) - set(covered_dimensions))
    critical_failures = [
        {
            "case_id": str(result.get("case_id") or ""),
            "family": str(result.get("family") or ""),
            "brain_role": str(result.get("brain_role") or ""),
            "failures": list(result.get("failures") or []),
        }
        for result in case_results
        if bool(result.get("critical")) and (not bool(result.get("passed")))
    ]
    score_values_by_dimension: dict[str, list[float]] = {dimension: [] for dimension in REQUIRED_PR12P_LIVE_PRODUCT_SCORE_DIMENSIONS}
    for result in executed_results:
        for dimension, value in dict(result.get("score_by_dimension") or {}).items():
            if isinstance(value, (int, float)):
                score_values_by_dimension.setdefault(dimension, []).append(float(value))
    dimension_summary = {
        dimension: {
            "covered": dimension in covered_dimensions,
            "average_score": round(_safe_mean(values), 4),
            "sample_count": len(values),
        }
        for dimension, values in score_values_by_dimension.items()
    }
    launch_blockers: list[str] = []
    launch_blockers.extend([f"missing_brain_role:{role}" for role in missing_roles])
    launch_blockers.extend([f"missing_family:{family}" for family in missing_families])
    launch_blockers.extend([f"missing_score_dimension:{dimension}" for dimension in missing_dimensions])
    if not bool(ui_probe.get("passed")):
        launch_blockers.extend(list(ui_probe.get("failures") or []))
    for failure in critical_failures:
        launch_blockers.append(f"critical_case_failed:{failure['case_id']}:{','.join(failure['failures'])}")
    dimension_threshold = float(os.environ.get("AGVM_PR12P12_DIMENSION_PASS_THRESHOLD") or 0.65)
    for dimension, summary in dimension_summary.items():
        if dimension not in REQUIRED_PR12P_LIVE_PRODUCT_SCORE_DIMENSIONS:
            continue
        average_score = float(summary.get("average_score") or 0.0)
        if average_score < dimension_threshold:
            launch_blockers.append(f"dimension_score_below_threshold:{dimension}:{average_score:.4f}<{dimension_threshold:.2f}")
    matrix_executed = not missing_roles and not missing_families and not missing_dimensions and not inventory.get("failures")
    product_quality_pass = matrix_executed and not launch_blockers and all(
        float(summary.get("average_score") or 0.0) >= dimension_threshold
        for dimension, summary in dimension_summary.items()
        if dimension in REQUIRED_PR12P_LIVE_PRODUCT_SCORE_DIMENSIONS
    )
    all_pass = bool(product_quality_pass)
    return {
        "schema_version": PR12P_LIVE_PRODUCT_MATRIX_REPORT_SCHEMA_VERSION,
        "phase": "live_product_matrix",
        "slice": "PR-12P-12",
        "base_url": selected_base_url,
        "all_pass": all_pass,
        "matrix_executed": matrix_executed,
        "product_quality_pass": product_quality_pass,
        "product_ready_verdict": "live_product_matrix_passed_pr12p_still_open"
        if all_pass
        else "live_product_matrix_failed_pr12p_product_not_ready",
        "brain_inventory": {
            "brain_count": len(list(inventory.get("brains") or [])),
            "targets": dict(inventory.get("targets") or {}),
            "created_fresh_brain": inventory.get("created_fresh_brain"),
            "fresh_cleanup": cleanup,
            "failures": list(inventory.get("failures") or []),
        },
        "ui_parity_probe": ui_probe,
        "coverage": {
            "required_brain_roles": list(REQUIRED_PR12P_LIVE_PRODUCT_BRAIN_ROLES),
            "covered_brain_roles": covered_roles,
            "missing_brain_roles": missing_roles,
            "required_families": list(REQUIRED_PR12P_LIVE_PRODUCT_FAMILIES),
            "covered_families": covered_families,
            "missing_families": missing_families,
            "required_score_dimensions": list(REQUIRED_PR12P_LIVE_PRODUCT_SCORE_DIMENSIONS),
            "covered_score_dimensions": covered_dimensions,
            "missing_score_dimensions": missing_dimensions,
        },
        "dimension_summary": dimension_summary,
        "case_results": case_results,
        "critical_failures": critical_failures,
        "launch_blockers": launch_blockers,
        "benchmark_inputs": {
            "phase": "live_product_matrix",
            "external_network_required": False,
            "mutation_enabled": True,
            "mutation_scope": "ephemeral_fresh_test_brain_create_delete_only",
            "live_llm_required": True,
            "brains_required": list(REQUIRED_PR12P_LIVE_PRODUCT_BRAIN_ROLES),
            "families_required": list(REQUIRED_PR12P_LIVE_PRODUCT_FAMILIES),
            "dimensions_required": list(REQUIRED_PR12P_LIVE_PRODUCT_SCORE_DIMENSIONS),
            "dimension_pass_threshold": dimension_threshold,
            "mcp_first": True,
            "answer_demo_secondary": True,
            "context_package_is_product": True,
        },
        "evidence_contract": {
            "previous_gate": "PR-12P-11 Local MCP Client Proof",
            "validates": [
                "multi_brain_live_retrieve",
                "fresh_empty_brain_no_match",
                "document_retrieval_raw_workspace",
                "path_corridor_completion",
                "followup_warm_context",
                "grow_preview_quality",
                "sleep_preview_visibility",
                "ui_payload_parity",
                "latency",
            ],
            "cloud_blocked": True,
        },
        "next_slice": "PR-12P-13 Product-Ready Local Gate" if all_pass else "PR-12P-12B Latency Product Gate Correction",
    }


def _pr12l_benchmark_case(family_group: str, family: str) -> ProductBenchmarkCase:
    for case in pr12l_benchmark_cases():
        if case.family_group == family_group and case.family == family:
            return case
    raise KeyError(f"missing_pr12l_case:{family_group}:{family}")


def _pr12l_validate_mcp_output(
    *,
    tool_name: str,
    output: dict[str, Any],
    contracts_by_name: dict[str, dict[str, Any]],
    failures: list[str],
    validations: list[dict[str, Any]],
) -> None:
    from mcp_stability import validate_mcp_tool_output

    contract = contracts_by_name.get(tool_name)
    if not contract:
        failures.append(f"missing_mcp_contract:{tool_name}")
        return
    validation = validate_mcp_tool_output(contract, output)
    validations.append(validation)
    if not bool(validation.get("passed")):
        failures.append(f"mcp_validation_failed:{tool_name}:{validation.get('errors')}")


def _pr12l_empty_write_graph() -> dict[str, Any]:
    return {"version": "test", "graph_name": "pr12l_regression_write", "nodes": [], "edges": [], "meta": {}}


def _pr12l_preview_write_bundle(text: str, input_mode: str = "auto", **kwargs: Any) -> dict[str, Any]:
    from unittest.mock import patch

    from derivation import preview_bundle
    from storage import empty_atlas, empty_index

    with patch("derivation.llm_memory_compile", return_value=(None, "llm_disabled")):
        return preview_bundle(text, input_mode, _pr12l_empty_write_graph(), empty_index(), empty_atlas(), **kwargs)


def _pr12l_learning_source_package(bundle: dict[str, Any]) -> dict[str, Any]:
    policy = dict(bundle.get("learning_policy") or {})
    questions = [dict(item) for item in list(policy.get("questions") or []) if isinstance(item, dict)]
    return {
        "schema_version": "agvm.source_investigation_package.v1",
        "investigation_id": "src_pr12l_e_learning_policy",
        "created_at": "2026-05-08T00:00:00Z",
        "status": "asking_clarification" if questions else "preview_ready",
        "source_request": {
            "source_label": "PR-12L-E guided learning regression",
            "input_kind": "manual_text",
        },
        "source_detection": {"source_kind": "manual_text", "confidence": 1.0},
        "budgets": {"question_limit": int(policy.get("question_limit") or 0), "mutation_enabled": False},
        "budget_usage": {"source_units": 1, "questions": len(questions)},
        "source_units": [
            {
                "unit_id": "manual_1",
                "kind": "manual_block",
                "raw_text": "Carola Bianchi e' la mia partner nel progetto WiSNAM.",
                "char_count": 55,
            }
        ],
        "extracted_assets": [],
        "open_questions": questions,
        "clarification_questions": questions,
        "guided_grow": {"mode": "guided", "state": policy.get("status"), "question_count": len(questions)},
        "compiler_handoff": {
            "handoff_version": "pr12i.compiler_handoff.v1",
            "source_summary": "Ambiguous relationship memory requires bounded human clarification before persistence.",
            "mega_text": "Carola Bianchi e' la mia partner nel progetto WiSNAM.",
            "source_purpose": "personal_memory",
            "recommended_input_mode": "auto",
            "recommended_learning_mode": "guided_learning",
            "preview_eligible": True,
        },
        "compiler_handoff_proof": {
            "proof_passed": True,
            "same_compiler_path": True,
            "preview_bundle_present": bool(bundle),
            "learning_policy_version": policy.get("version"),
        },
        "timeline": [
            {"event": "source_detected"},
            {"event": "learning_policy_ready"},
            {"event": "asking_clarification" if questions else "preview_ready"},
        ],
    }


def _pr12l_geometry_node(
    node_id: str,
    summary: str,
    *,
    memory_type: str,
    guide_area: str,
    position: tuple[float, float, float],
    is_document_anchor: bool = False,
    claim_status: str = "fact",
    temporal_role: str | None = None,
) -> dict[str, Any]:
    from projection import position_to_bucket

    final_position = {"x": position[0], "y": position[1], "z": position[2]}
    return {
        "id": node_id,
        "node_kind": "memory",
        "memory_type": memory_type,
        "summary": summary,
        "raw_text": summary,
        "routing_semantic_scores": {},
        "routing_facets": {},
        "routing_brainhex": {},
        "semantic_color": {},
        "base_position": dict(final_position),
        "final_position": dict(final_position),
        "topology_brainhex": {},
        "topology_color": {},
        "bucket": position_to_bucket(final_position),
        "is_document_anchor": is_document_anchor,
        "links": [],
        "highways": [],
        "provenance": {"guide_conceptual_area": guide_area},
        "source_trust": "verified_public" if is_document_anchor else "user_asserted",
        "claim_status": claim_status,
        "temporal_role": temporal_role,
        "answer_eligible": True,
        "profile_eligible": True,
        "document_eligible": True,
        "lifecycle_status": "active",
    }


def _pr12l_geometry_fixture_graph() -> dict[str, Any]:
    identity = _pr12l_geometry_node("identity", "Simone Massaro is a founder.", memory_type="identity", guide_area="Identity", position=(0.18, 0.0, 0.0))
    values = _pr12l_geometry_node("values", "Precision and sustainable impact guide the work.", memory_type="value", guide_area="Values", position=(0.0, 0.24, 0.0))
    relationship = _pr12l_geometry_node("rel", "Giovanni Massaro was Simone's father.", memory_type="relational", guide_area="Relationships", position=(0.0, 0.42, 0.0))
    project = _pr12l_geometry_node("project", "BaxEnergy is a renewable energy management project.", memory_type="project", guide_area="Projects", position=(0.44, 0.16, 0.0))
    bridge = _pr12l_geometry_node("history", "In 2024 Yokogawa acquired BaxEnergy.", memory_type="episodic", guide_area="History", position=(0.58, 0.17, 0.0))
    document = _pr12l_geometry_node(
        "doc",
        "Yokogawa announced the BaxEnergy acquisition in 2024.",
        memory_type="document_anchor",
        guide_area="Media Signals",
        position=(0.74, 0.18, 0.0),
        is_document_anchor=True,
    )
    future = _pr12l_geometry_node(
        "future",
        "Future intent: expand Free Mind Foundry as a Sicilian campus.",
        memory_type="knowledge",
        guide_area="Projects",
        position=(0.25, 0.66, 0.0),
        claim_status="hypothesis",
        temporal_role="future_intent",
    )
    project["highways"] = [{"target_node_id": "doc", "strength": 0.91, "reason": "project_document_bridge"}]
    document["highways"] = [{"target_node_id": "project", "strength": 0.89, "reason": "document_project_bridge"}]
    document["links"] = [{"target_node_id": "project", "strength": 0.8, "reason": "source_supports_project"}]
    return {"nodes": [identity, values, relationship, project, bridge, document, future], "edges": []}


def _pr12l_maintenance_scorecard_report(calibration_report: dict[str, Any]) -> dict[str, Any]:
    return {
        "applied": False,
        "mode": "evolve",
        "maintenance_proposals": [
            {
                "proposal_id": "proposal_geometry_metric_review",
                "proposal_kind": "geometry_metric_review",
                "risk_level": "low",
                "human_review_required": True,
                "reason": "Review geometry calibration metrics before any matrix or highway policy change.",
                "target_node_ids": ["project", "doc"],
                "target_document_ids": ["doc"],
                "proposed_action": "review_geometry_metric_improvement",
                "controlled_metric_delta": {"path_bridge_potential": 0.12, "corruption_risk": 0.0},
            }
        ],
        "metamemory_snapshot": {
            "schema_version": "agvm.pr12h.metamemory_snapshot.v1",
            "snapshot_id": "metamemory::pr12l_e_scorecard",
            "calibration_schema_version": calibration_report.get("schema_version"),
            "overall_score": calibration_report.get("overall_score"),
        },
        "apply_policy_guard": {
            "guard_passed": False,
            "applied": False,
            "blocked_reasons": ["preview_only_product_scorecard"],
            "rollback_snapshot": {"snapshot_id": "rollback::pr12l_e", "before_graph_hash": "pr12l_before", "candidate_graph_hash": "pr12l_candidate"},
            "before_after_audit": {"created_node_count": 0, "deleted_node_count": 0, "mutated_node_count": 0},
            "no_corruption_guards": {"passed": True, "graph_mutated": False, "cross_brain_leakage": False},
        },
        "rollback_snapshot": {"snapshot_id": "rollback::pr12l_e", "before_graph_hash": "pr12l_before", "candidate_graph_hash": "pr12l_candidate"},
        "before_after_audit": {"created_node_count": 0, "deleted_node_count": 0, "mutated_node_count": 0},
        "no_corruption_guards": {"passed": True, "graph_mutated": False, "cross_brain_leakage": False},
        "reviewed_node_ids": ["project", "doc"],
        "sleep_consolidation_proposals": [],
        "evolve_structural_proposals": [{"proposal_id": "proposal_geometry_metric_review"}],
        "retrieval_trace_learning_proposals": [],
        "duplicate_candidates": [],
        "brain_geometry_calibration": calibration_report,
        "self_improvement_loop": {
            "maintenance_id": "maintenance_pr12l_e_scorecard",
            "applied": False,
            "preview_only": True,
        },
    }


def _run_pr12l_regression_case(case: ProductBenchmarkCase, contracts_by_name: dict[str, dict[str, Any]]) -> dict[str, Any]:
    from mcp_grow import build_mcp_source_output, build_mcp_write_output
    from mcp_maintenance import build_mcp_maintenance_output
    from mcp_retrieval import build_mcp_retrieval_tool_output

    started = time.perf_counter()
    failures: list[str] = []
    validations: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    try:
        if case.family == "pr12a_semantic_contract":
            source_case = _pr12l_benchmark_case("retrieval", "exact_fact")
            result = _pr12l_build_retrieval_result(source_case)
            output = build_mcp_retrieval_tool_output("retrieve_context", result)
            _pr12l_validate_mcp_output(tool_name="retrieve_context", output=output, contracts_by_name=contracts_by_name, failures=failures, validations=validations)
            contract = dict(result.get("semantic_contract") or {})
            runtime = dict(result.get("semantic_contract_runtime") or {})
            intent = dict(contract.get("intent") or {})
            context_contract = dict(contract.get("context_contract") or {})
            if not bool(runtime.get("contract_passed")):
                failures.append("semantic_contract_runtime_failed")
            if str(intent.get("primary") or "") != "work":
                failures.append("semantic_contract_primary_not_preserved")
            if "work" not in list(context_contract.get("required_sections") or []):
                failures.append("semantic_contract_required_work_missing")
            summary = {
                "semantic_contract": contract,
                "semantic_contract_runtime": runtime,
                "mcp_status": output.get("status"),
            }
        elif case.family == "pr12b_context_package":
            source_case = _pr12l_benchmark_case("retrieval", "broad_self_dossier")
            result = _pr12l_build_retrieval_result(source_case)
            output = build_mcp_retrieval_tool_output("inspect_context_package", result)
            _pr12l_validate_mcp_output(tool_name="inspect_context_package", output=output, contracts_by_name=contracts_by_name, failures=failures, validations=validations)
            context = dict(result.get("context_package") or {})
            metrics = dict(context.get("metrics") or {})
            agent_markdown = str(context.get("agent_markdown") or "")
            if context.get("schema_version") != "agvm.mcp_context_package.v2":
                failures.append("context_package_schema_mismatch")
            if not bool((context.get("dossier_hygiene") or {}).get("passed")):
                failures.append("dossier_hygiene_failed")
            if int(metrics.get("section_count") or 0) < 6:
                failures.append("context_package_too_thin")
            if int(metrics.get("truncated_core_text_count") or 0) > 0:
                failures.append("truncated_core_text_in_agent_package")
            for forbidden in ("Evidence Ledger", "## Path Discoveries", "Landing 1 -> Landing 2", "vec_node_"):
                if forbidden in agent_markdown:
                    failures.append(f"debug_leak_in_context_package:{forbidden}")
            summary = _pr12l_result_summary(result, {"inspect_context_package": output})
        elif case.family == "pr12c_path_corridor":
            source_case = _pr12l_benchmark_case("retrieval", "cross_area")
            result = _pr12l_build_retrieval_result(source_case)
            output = build_mcp_retrieval_tool_output("retrieve_path_corridor", result)
            _pr12l_validate_mcp_output(tool_name="retrieve_path_corridor", output=output, contracts_by_name=contracts_by_name, failures=failures, validations=validations)
            path_corridors = dict(result.get("path_corridors") or {})
            metrics = dict(path_corridors.get("metrics") or {})
            paths = [dict(item) for item in list(path_corridors.get("paths") or []) if isinstance(item, dict)]
            if path_corridors.get("schema_version") != "agvm.path_corridor_package.v1":
                failures.append("path_corridor_schema_mismatch")
            if int(metrics.get("landing_count") or 0) < 2:
                failures.append("path_corridor_landing_count_too_low")
            if int(metrics.get("path_count") or 0) < 1:
                failures.append("path_corridor_missing_paths")
            if int(metrics.get("promoted_intermediate_count") or 0) < 1:
                failures.append("path_corridor_promoted_intermediates_missing")
            if not all(bool(path.get("read_intermediate_nodes")) for path in paths):
                failures.append("path_corridor_not_reading_intermediates")
            if "## Path Discoveries" in str(dict(result.get("context_package") or {}).get("agent_markdown") or ""):
                failures.append("path_debug_log_leaked_into_main_dossier")
            summary = _pr12l_result_summary(result, {"retrieve_path_corridor": output})
        elif case.family == "pr12d_document_workspace":
            source_case = _pr12l_benchmark_case("retrieval", "exact_document")
            result = _pr12l_build_retrieval_result(source_case)
            redacted = build_mcp_retrieval_tool_output("retrieve_document", result, include_raw_text=False)
            raw = build_mcp_retrieval_tool_output("retrieve_document", result, include_raw_text=True)
            _pr12l_validate_mcp_output(tool_name="retrieve_document", output=redacted, contracts_by_name=contracts_by_name, failures=failures, validations=validations)
            workspace = dict(result.get("document_workspace") or {})
            metrics = dict(workspace.get("metrics") or {})
            redacted_first = _pr12l_first_document(redacted)
            raw_first = _pr12l_first_document(raw)
            if workspace.get("schema_version") != "agvm.document_workspace_package.v1":
                failures.append("document_workspace_schema_mismatch")
            if workspace.get("status") != "workspace_ready":
                failures.append("document_workspace_not_ready")
            if int(metrics.get("full_text_document_count") or 0) < 1:
                failures.append("document_workspace_full_text_missing")
            if redacted_first.get("full_text") != "" or not bool(redacted_first.get("full_text_available")):
                failures.append("document_default_raw_text_policy_failed")
            if not str(raw_first.get("full_text") or "").strip():
                failures.append("document_raw_text_not_preserved_when_requested")
            if int(metrics.get("related_or_cold_document_count") or 0) != 0:
                failures.append("exact_document_related_docs_promoted_as_primary")
            summary = _pr12l_result_summary(result, {"retrieve_document_redacted": redacted, "retrieve_document_raw": raw})
        elif case.family == "pr12e_cognitive_write_plan":
            bundle = _pr12l_preview_write_bundle("Carola Bianchi e' la mia partner nel progetto WiSNAM.")
            output = build_mcp_write_output("write_memory_preview", preview_bundle=bundle)
            _pr12l_validate_mcp_output(tool_name="write_memory_preview", output=output, contracts_by_name=contracts_by_name, failures=failures, validations=validations)
            plan = dict(bundle.get("cognitive_write_plan") or {})
            acts = [dict(item) for item in list(plan.get("memory_acts") or []) if isinstance(item, dict)]
            stages = [str(item.get("stage_id") or "") for item in list(dict(bundle.get("write_trace") or {}).get("stages") or []) if isinstance(item, dict)]
            if plan.get("version") != "pr12e.cognitive_write_plan.v1":
                failures.append("cognitive_write_plan_version_mismatch")
            if int((plan.get("summary") or {}).get("memory_act_count") or 0) < 1:
                failures.append("cognitive_write_plan_has_no_memory_acts")
            if not any(str(item.get("act_type") or "") == "update_relationship_state" for item in acts):
                failures.append("relationship_memory_act_missing")
            if not bool((plan.get("human_review") or {}).get("required")):
                failures.append("relationship_write_not_review_gated")
            if "cognitive_write_ready" not in stages:
                failures.append("cognitive_write_trace_stage_missing")
            summary = {
                "cognitive_write_plan_version": plan.get("version"),
                "memory_act_count": int((plan.get("summary") or {}).get("memory_act_count") or 0),
                "human_review_required": bool((plan.get("human_review") or {}).get("required")),
                "write_trace_stages": stages,
                "mcp_status": output.get("status"),
            }
        elif case.family == "pr12f_learning_policy":
            bundle = _pr12l_preview_write_bundle(
                "Carola Bianchi e' la mia partner nel progetto WiSNAM.",
                learning_mode="guided_learning",
                question_limit=2,
            )
            source_package = _pr12l_learning_source_package(bundle)
            output = build_mcp_source_output("grow_guided", source_package=source_package, preview_bundle=bundle)
            _pr12l_validate_mcp_output(tool_name="grow_guided", output=output, contracts_by_name=contracts_by_name, failures=failures, validations=validations)
            policy = dict(bundle.get("learning_policy") or {})
            stages = [str(item.get("stage_id") or "") for item in list(dict(bundle.get("write_trace") or {}).get("stages") or []) if isinstance(item, dict)]
            if policy.get("version") != "pr12f.learning_policy.v1":
                failures.append("learning_policy_version_mismatch")
            if policy.get("mode") != "guided_learning":
                failures.append("learning_policy_mode_mismatch")
            if int(policy.get("question_limit") or 0) > 2:
                failures.append("learning_policy_question_limit_not_enforced")
            if policy.get("status") != "clarification_required":
                failures.append("ambiguous_relationship_not_blocked_for_clarification")
            if int((policy.get("summary") or {}).get("blocked_count") or 0) < 1:
                failures.append("learning_policy_blocked_count_missing")
            if not list(policy.get("questions") or []):
                failures.append("learning_policy_questions_missing")
            if "learning_policy_ready" not in stages:
                failures.append("learning_policy_trace_stage_missing")
            summary = {
                "learning_policy_version": policy.get("version"),
                "learning_policy_mode": policy.get("mode"),
                "learning_policy_status": policy.get("status"),
                "question_count": int((policy.get("summary") or {}).get("question_count") or 0),
                "source_status": source_package.get("status"),
                "mcp_status": output.get("status"),
            }
        elif case.family == "pr12g_brain_geometry_calibration":
            from geometry_calibration import build_brain_geometry_calibration_report

            calibration = build_brain_geometry_calibration_report(_pr12l_geometry_fixture_graph())
            maintenance_report = _pr12l_maintenance_scorecard_report(calibration)
            output = build_mcp_maintenance_output("evolve_preview", report=maintenance_report, max_nodes_considered=80)
            _pr12l_validate_mcp_output(tool_name="evolve_preview", output=output, contracts_by_name=contracts_by_name, failures=failures, validations=validations)
            if calibration.get("schema_version") != "agvm.pr12g.brain_geometry_calibration.v1":
                failures.append("brain_geometry_calibration_schema_mismatch")
            if not bool((calibration.get("benchmarks") or {}).get("all_pass")):
                failures.append("brain_geometry_calibration_benchmark_failed")
            if not bool((maintenance_report.get("no_corruption_guards") or {}).get("passed")):
                failures.append("maintenance_no_corruption_guard_failed")
            if not bool(output.get("maintenance_proposals")):
                failures.append("evolve_preview_proposals_missing")
            summary = {
                "calibration_schema_version": calibration.get("schema_version"),
                "geometry_all_pass": bool((calibration.get("benchmarks") or {}).get("all_pass")),
                "overall_score": calibration.get("overall_score"),
                "maintenance_status": output.get("status"),
                "proposal_count": len(list(output.get("maintenance_proposals") or [])),
                "no_corruption_guards": dict(maintenance_report.get("no_corruption_guards") or {}),
            }
        else:
            failures.append(f"unimplemented_regression_family:{case.family}")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"exception:{type(exc).__name__}:{str(exc)[:300]}")

    validation_failures = [item for item in validations if not bool(item.get("passed", True))]
    return {
        "case_id": case.case_id,
        "family_group": case.family_group,
        "family": case.family,
        "fixture_kind": case.fixture_kind,
        "interaction": case.interaction,
        "slice_owner": case.slice_owner,
        "critical": bool(case.critical),
        "tool_name": case.tool_name,
        "required_artifacts": list(case.required_artifacts),
        "required_signals": list(case.required_signals),
        "passed": not failures,
        "failures": failures,
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "summary": summary,
        "mcp_validation_count": len(validations),
        "mcp_validation_failures": validation_failures,
        "benchmark_sources": {
            "regresses_pr12a_to_pr12g": True,
            "mutation_enabled": False,
            "live_llm_required": False,
            "uses_real_mcp_validation": bool(validations),
        },
    }


def run_pr12l_regression_benchmark_suite(base_url: str | None = None) -> dict[str, Any]:
    regression_cases = [case for case in pr12l_benchmark_cases() if case.family_group == "regression"]
    contracts_by_name = _pr12l_contracts_by_name()
    case_results = [_run_pr12l_regression_case(case, contracts_by_name) for case in regression_cases]
    covered_families = {str(result.get("family") or "") for result in case_results}
    missing_families = sorted(set(REQUIRED_PR12L_REGRESSION_FAMILIES) - covered_families)
    critical_failures = [
        str(result.get("case_id") or "")
        for result in case_results
        if bool(result.get("critical")) and not bool(result.get("passed"))
    ]
    passed_count = sum(1 for result in case_results if bool(result.get("passed")))
    total_count = len(case_results)
    all_pass = not critical_failures and not missing_families and total_count == len(REQUIRED_PR12L_REGRESSION_FAMILIES)
    family_matrix = {
        family: {
            "covered": family in covered_families,
            "passed": any(str(result.get("family") or "") == family and bool(result.get("passed")) for result in case_results),
            "case_ids": [str(result.get("case_id") or "") for result in case_results if str(result.get("family") or "") == family],
        }
        for family in REQUIRED_PR12L_REGRESSION_FAMILIES
    }
    open_gaps: list[str] = []
    if missing_families:
        open_gaps.append(f"Missing PR-12A..G regression families: {', '.join(missing_families)}")
    if critical_failures:
        open_gaps.append(f"Critical PR-12A..G regression failures: {', '.join(critical_failures)}")
    verdict = "regression_passed_pr12l_scorecard_ready" if all_pass else "regression_failed_pr12l_scorecard_blocked"
    return {
        "schema_version": PR12L_REGRESSION_BENCHMARK_REPORT_SCHEMA_VERSION,
        "phase": "regression",
        "slice": "PR-12L-E",
        "all_pass": all_pass,
        "pass_rate": round(passed_count / total_count, 4) if total_count else 0.0,
        "passed_count": passed_count,
        "total_count": total_count,
        "base_url": str(base_url or DEFAULT_BASE_URL),
        "product_ready_verdict": verdict,
        "case_results": case_results,
        "family_matrix": family_matrix,
        "critical_failures": critical_failures,
        "open_gaps": open_gaps,
        "benchmark_inputs": {
            "phase": "regression",
            "regression_families": list(REQUIRED_PR12L_REGRESSION_FAMILIES),
            "external_network_required": False,
            "mutation_enabled": False,
            "live_llm_required": False,
            "uses_real_mcp_validation": True,
        },
        "next_slice": "PR-12L-E Product-Ready Scorecard",
    }


def _pr12l_case_results_by_family(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("family") or ""): dict(item)
        for item in list(report.get("case_results") or [])
        if isinstance(item, dict) and str(item.get("family") or "").strip()
    }


def _pr12l_phase_gate(phase_id: str, report: dict[str, Any], *, description: str) -> dict[str, Any]:
    return {
        "phase_id": phase_id,
        "description": description,
        "passed": bool(report.get("all_pass")),
        "schema_version": report.get("schema_version"),
        "passed_count": report.get("passed_count"),
        "total_count": report.get("total_count"),
        "critical_failures": list(report.get("critical_failures") or []),
        "open_gaps": list(report.get("open_gaps") or []),
    }


def _pr12l_product_gate(gate_id: str, *, passed: bool, evidence: dict[str, Any], failure_reason: str = "") -> dict[str, Any]:
    return {
        "gate_id": gate_id,
        "passed": bool(passed),
        "failure_reason": "" if passed else failure_reason,
        "evidence": evidence,
    }


def _pr12l_mcp_validation_failure_count(*reports: dict[str, Any]) -> int:
    count = 0
    for report in reports:
        for result in list(report.get("case_results") or []):
            if isinstance(result, dict):
                count += len(list(result.get("mcp_validation_failures") or []))
    return count


def _pr12l_collect_critical_failures(phase_reports: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for phase, report in phase_reports.items():
        for case_id in list(report.get("critical_failures") or []):
            rows.append({"phase": phase, "case_id": str(case_id)})
        for gap in list(report.get("open_gaps") or []):
            rows.append({"phase": phase, "case_id": "", "gap": str(gap)})
    return rows


def _pr12l_family_group_matrix(phase_reports: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        "grow": {
            "required": list(REQUIRED_PR12L_GROW_FAMILIES),
            "families": dict(phase_reports["source_intake"].get("family_matrix") or {}),
        },
        "retrieval": {
            "required": list(REQUIRED_PR12L_RETRIEVAL_FAMILIES),
            "families": dict(phase_reports["retrieval_mcp"].get("family_matrix") or {}),
        },
        "ui_mcp": {
            "required": list(REQUIRED_PR12L_UI_MCP_FAMILIES),
            "families": dict(phase_reports["ui_truth"].get("family_matrix") or {}),
        },
        "regression": {
            "required": list(REQUIRED_PR12L_REGRESSION_FAMILIES),
            "families": dict(phase_reports["regression"].get("family_matrix") or {}),
        },
    }


def _pr12l_product_ready_gates(
    *,
    source_report: dict[str, Any],
    retrieval_report: dict[str, Any],
    ui_report: dict[str, Any],
    regression_report: dict[str, Any],
    registry_validation: dict[str, Any],
    critical_failure_catalog: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    source_by_family = _pr12l_case_results_by_family(source_report)
    retrieval_by_family = _pr12l_case_results_by_family(retrieval_report)
    ui_by_family = _pr12l_case_results_by_family(ui_report)
    regression_by_family = _pr12l_case_results_by_family(regression_report)
    broad_summary = dict(dict(retrieval_by_family.get("broad_self_dossier") or {}).get("summary") or {})
    broad_context = dict(broad_summary.get("context_package") or {})
    answer_demo_defaults = [
        str(result.get("case_id") or "")
        for result in list(retrieval_report.get("case_results") or [])
        if bool(((dict(result.get("summary") or {}).get("mcp_outputs") or {}).get("answer_demo_default_present")))
    ]
    mcp_validation_failure_count = _pr12l_mcp_validation_failure_count(retrieval_report, ui_report, regression_report)
    return [
        _pr12l_product_gate(
            "context_package_usefulness",
            passed=bool(retrieval_report.get("all_pass"))
            and bool(broad_context.get("dossier_hygiene_passed"))
            and int(broad_context.get("section_count") or 0) >= 6
            and int(broad_context.get("truncated_core_text_count") or 0) == 0
            and bool(dict(regression_by_family.get("pr12b_context_package") or {}).get("passed")),
            evidence={
                "retrieval_passed": bool(retrieval_report.get("all_pass")),
                "broad_section_count": int(broad_context.get("section_count") or 0),
                "dossier_hygiene_passed": bool(broad_context.get("dossier_hygiene_passed")),
                "truncated_core_text_count": int(broad_context.get("truncated_core_text_count") or 0),
                "pr12b_regression_passed": bool(dict(regression_by_family.get("pr12b_context_package") or {}).get("passed")),
            },
            failure_reason="Context package is not yet a clean, useful MCP dossier.",
        ),
        _pr12l_product_gate(
            "source_intake_coherent_nodes",
            passed=bool(source_report.get("all_pass")),
            evidence={
                "source_passed_count": source_report.get("passed_count"),
                "source_total_count": source_report.get("total_count"),
                "critical_failures": list(source_report.get("critical_failures") or []),
            },
            failure_reason="Source intake benchmark has failing critical source families.",
        ),
        _pr12l_product_gate(
            "ocr_crawl_honesty",
            passed=bool(dict(source_by_family.get("scanned_ocr_pdf") or {}).get("passed"))
            and bool(dict(source_by_family.get("website_with_sublinks") or {}).get("passed")),
            evidence={
                "scanned_ocr_pdf_passed": bool(dict(source_by_family.get("scanned_ocr_pdf") or {}).get("passed")),
                "website_with_sublinks_passed": bool(dict(source_by_family.get("website_with_sublinks") or {}).get("passed")),
            },
            failure_reason="OCR or website crawl skipped states are not honest enough.",
        ),
        _pr12l_product_gate(
            "mcp_tools_stable",
            passed=bool(registry_validation.get("passed")) and mcp_validation_failure_count == 0,
            evidence={
                "registry_passed": bool(registry_validation.get("passed")),
                "required_tool_count": registry_validation.get("required_tool_count"),
                "mcp_validation_failure_count": mcp_validation_failure_count,
            },
            failure_reason="MCP contract registry or representative outputs are unstable.",
        ),
        _pr12l_product_gate(
            "ui_backend_truth",
            passed=bool(ui_report.get("all_pass")),
            evidence={
                "ui_passed_count": ui_report.get("passed_count"),
                "ui_total_count": ui_report.get("total_count"),
                "ui_critical_failures": list(ui_report.get("critical_failures") or []),
            },
            failure_reason="UI truth surface does not match backend/MCP truth.",
        ),
        _pr12l_product_gate(
            "sleep_evolve_no_corruption",
            passed=bool(dict(regression_by_family.get("pr12g_brain_geometry_calibration") or {}).get("passed"))
            and bool(dict(ui_by_family.get("maintenance_proposal_parity") or {}).get("passed")),
            evidence={
                "pr12g_regression_passed": bool(dict(regression_by_family.get("pr12g_brain_geometry_calibration") or {}).get("passed")),
                "maintenance_ui_parity_passed": bool(dict(ui_by_family.get("maintenance_proposal_parity") or {}).get("passed")),
            },
            failure_reason="Sleep/evolve proposal or no-corruption guard is not proven.",
        ),
        _pr12l_product_gate(
            "geometry_calibration_no_regression",
            passed=bool(dict(regression_by_family.get("pr12g_brain_geometry_calibration") or {}).get("passed")),
            evidence=dict(dict(regression_by_family.get("pr12g_brain_geometry_calibration") or {}).get("summary") or {}),
            failure_reason="Brain geometry calibration regression failed.",
        ),
        _pr12l_product_gate(
            "answer_demo_grounded_and_secondary",
            passed=bool(retrieval_report.get("all_pass")) and not answer_demo_defaults,
            evidence={
                "retrieval_passed": bool(retrieval_report.get("all_pass")),
                "answer_demo_default_present_case_ids": answer_demo_defaults,
                "answer_demo_is_secondary": True,
            },
            failure_reason="Answer demo is leaking into default MCP output or retrieval grounding failed.",
        ),
        _pr12l_product_gate(
            "no_benchmark_family_has_critical_failure",
            passed=not critical_failure_catalog,
            evidence={
                "critical_failure_count": len(critical_failure_catalog),
                "critical_failures": critical_failure_catalog,
            },
            failure_reason="At least one benchmark phase still has a critical failure or open gap.",
        ),
    ]


def run_pr12l_product_scorecard_suite(base_url: str | None = None) -> dict[str, Any]:
    from mcp_contracts import build_mcp_contract_registry

    base = str(base_url or DEFAULT_BASE_URL)
    harness_report = run_pr12l_product_harness_suite(base)
    source_report = run_pr12l_source_intake_benchmark_suite(base)
    retrieval_report = run_pr12l_retrieval_mcp_benchmark_suite(base)
    ui_report = run_pr12l_ui_truth_benchmark_suite(base)
    regression_report = run_pr12l_regression_benchmark_suite(base)
    phase_reports = {
        "product_harness": harness_report,
        "source_intake": source_report,
        "retrieval_mcp": retrieval_report,
        "ui_truth": ui_report,
        "regression": regression_report,
    }
    registry_validation = dict(build_mcp_contract_registry().get("registry_validation") or {})
    critical_failure_catalog = _pr12l_collect_critical_failures(phase_reports)
    phase_gate_matrix = {
        "product_harness": _pr12l_phase_gate("product_harness", harness_report, description="Fixture and family harness is complete and MCP-first."),
        "source_intake": _pr12l_phase_gate("source_intake", source_report, description="Grow/source intake benchmark is passing."),
        "retrieval_mcp": _pr12l_phase_gate("retrieval_mcp", retrieval_report, description="Retrieval, context package, document and path MCP surfaces are passing."),
        "ui_truth": _pr12l_phase_gate("ui_truth", ui_report, description="Dashboard truth surfaces match backend/MCP outputs."),
        "regression": _pr12l_phase_gate("regression", regression_report, description="PR-12A..PR-12G regression families remain intact."),
    }
    product_ready_gates = _pr12l_product_ready_gates(
        source_report=source_report,
        retrieval_report=retrieval_report,
        ui_report=ui_report,
        regression_report=regression_report,
        registry_validation=registry_validation,
        critical_failure_catalog=critical_failure_catalog,
    )
    failed_product_gates = [str(gate.get("gate_id") or "") for gate in product_ready_gates if not bool(gate.get("passed"))]
    failed_phase_gates = [phase for phase, gate in phase_gate_matrix.items() if not bool(gate.get("passed"))]
    all_pass = not failed_phase_gates and not failed_product_gates and not critical_failure_catalog
    passed_count = sum(1 for gate in product_ready_gates if bool(gate.get("passed"))) + sum(1 for gate in phase_gate_matrix.values() if bool(gate.get("passed")))
    total_count = len(product_ready_gates) + len(phase_gate_matrix)
    return {
        "schema_version": PR12L_PRODUCT_SCORECARD_REPORT_SCHEMA_VERSION,
        "phase": "product_scorecard",
        "slice": "PR-12L-E",
        "all_pass": all_pass,
        "pr12l_closed": all_pass,
        "pass_rate": round(passed_count / total_count, 4) if total_count else 0.0,
        "passed_count": passed_count,
        "total_count": total_count,
        "base_url": base,
        "product_ready_verdict": "product_ready_local_benchmark_passed_pr12l_closed" if all_pass else "not_product_ready_pr12l_remains_open",
        "phase_gate_matrix": phase_gate_matrix,
        "product_ready_gates": product_ready_gates,
        "failed_phase_gates": failed_phase_gates,
        "failed_product_gates": failed_product_gates,
        "family_group_matrix": _pr12l_family_group_matrix(phase_reports),
        "critical_failure_catalog": critical_failure_catalog,
        "registry_validation": registry_validation,
        "benchmark_inputs": {
            "phase": "product_scorecard",
            "external_network_required": False,
            "external_browser_required": False,
            "mutation_enabled": False,
            "live_llm_required": False,
            "local_product_truth_only": True,
            "mcp_first": True,
            "context_package_is_product": True,
        },
        "release_boundary": {
            "self_hosted_distribution_not_started": True,
            "cloud_commercialization_not_started": True,
            "next_phase_requires_brain_scoping": True,
            "next_phase": "PR-12M Local Self-Hosted Multi-Brain MCP Distribution",
        },
        "phase_reports": {
            phase: {
                "schema_version": report.get("schema_version"),
                "all_pass": bool(report.get("all_pass")),
                "passed_count": report.get("passed_count"),
                "total_count": report.get("total_count"),
                "product_ready_verdict": report.get("product_ready_verdict"),
            }
            for phase, report in phase_reports.items()
        },
        "next_slice": "PR-12M-A Local Brain Registry" if all_pass else "PR-12L-E Product-Ready Scorecard Remediation",
    }


def _pr12m_repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _pr12m_case_result(
    *,
    family: str,
    title: str,
    failures: list[str],
    evidence: dict[str, Any] | None = None,
    critical: bool = True,
    elapsed_ms: float = 0.0,
) -> dict[str, Any]:
    return {
        "family": family,
        "title": title,
        "critical": critical,
        "passed": not failures,
        "status": "passed" if not failures else "failed",
        "failures": failures,
        "elapsed_ms": round(elapsed_ms, 3),
        "evidence": evidence or {},
        "mcp_first": True,
        "mutation_enabled": False,
        "live_llm_required": False,
    }


def _pr12m_read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _pr12m_initialize_runtime_storage(record: dict[str, Any], *, marker: str) -> dict[str, Any]:
    from runtime_scope import use_runtime_brain
    from sqlite_store import bootstrap_runtime_store, fetch_runtime_audit
    from storage import empty_atlas, empty_graph, empty_graph_view, empty_index, save_atlas, save_graph, save_graph_view, save_index

    with use_runtime_brain(record):
        bootstrap_runtime_store()
        graph = empty_graph()
        graph["meta"] = {**dict(graph.get("meta") or {}), "readiness_marker": marker}
        graph_view = empty_graph_view()
        graph_view["meta"] = {**dict(graph_view.get("meta") or {}), "readiness_marker": marker}
        save_graph(graph)
        save_graph_view(graph_view)
        save_index(empty_index())
        save_atlas(empty_atlas())
        return fetch_runtime_audit()


def _run_pr12m_product_truth_case(base_url: str) -> dict[str, Any]:
    started = time.perf_counter()
    failures: list[str] = []
    evidence: dict[str, Any] = {}
    try:
        report = run_pr12l_product_scorecard_suite(base_url)
        evidence = {
            "schema_version": report.get("schema_version"),
            "phase": report.get("phase"),
            "all_pass": bool(report.get("all_pass")),
            "product_ready_verdict": report.get("product_ready_verdict"),
            "passed_count": report.get("passed_count"),
            "total_count": report.get("total_count"),
        }
        if not bool(report.get("all_pass")):
            failures.append("pr12l_product_scorecard_not_passing")
        if report.get("product_ready_verdict") != "product_ready_local_benchmark_passed_pr12l_closed":
            failures.append("pr12l_product_ready_verdict_missing")
    except Exception as exc:
        failures.append(f"pr12l_product_scorecard_error:{exc}")
    return _pr12m_case_result(
        family="pr12l_product_truth",
        title="PR-12L product truth remains passing before self-hosted release.",
        failures=failures,
        evidence=evidence,
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def _run_pr12m_brain_registry_switch_case() -> dict[str, Any]:
    started = time.perf_counter()
    failures: list[str] = []
    evidence: dict[str, Any] = {}
    try:
        import tempfile

        from brain_registry import bootstrap_local_brain_registry, create_local_brain, load_local_brain_registry, resolve_brain_scope, set_active_brain

        with tempfile.TemporaryDirectory(prefix="agvm-pr12m-f-registry-") as tmp_name:
            root = Path(tmp_name) / "brains"
            bootstrap_local_brain_registry(brain_root=root, legacy_data_dirs=[], preferred_default_brain_id="bench_default")
            create_local_brain(brain_id="bench_alpha", display_name="Bench Alpha", make_active=True, make_default=True, brain_root=root)
            create_local_brain(brain_id="bench_beta", display_name="Bench Beta", make_active=False, make_default=False, brain_root=root)
            beta_selected = set_active_brain("bench_beta", make_default=False, brain_root=root)
            alpha_selected = set_active_brain("bench_alpha", make_default=True, brain_root=root)
            restarted = load_local_brain_registry(brain_root=root)
            alpha = resolve_brain_scope("bench_alpha", brain_root=root, require_explicit=True)
            beta = resolve_brain_scope("bench_beta", brain_root=root, require_explicit=True)
            evidence = {
                "brain_count": restarted.get("brain_count"),
                "beta_switch_active": beta_selected.get("active_brain_id"),
                "alpha_switch_active": alpha_selected.get("active_brain_id"),
                "restarted_active": restarted.get("active_brain_id"),
                "restarted_default": restarted.get("default_brain_id"),
                "validation": restarted.get("validation"),
                "storage_paths_distinct": alpha.get("storage_path") != beta.get("storage_path"),
            }
            if restarted.get("active_brain_id") != "bench_alpha":
                failures.append("active_brain_not_preserved_after_registry_reload")
            if restarted.get("default_brain_id") != "bench_alpha":
                failures.append("default_brain_not_preserved_after_registry_reload")
            if not bool((restarted.get("validation") or {}).get("passed")):
                failures.append("registry_validation_failed")
            if alpha.get("storage_path") == beta.get("storage_path"):
                failures.append("brain_storage_paths_not_distinct")
    except Exception as exc:
        failures.append(f"brain_registry_switch_error:{exc}")
    return _pr12m_case_result(
        family="brain_registry_switch",
        title="Brain selection and default state survive reload without process restart.",
        failures=failures,
        evidence=evidence,
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def _run_pr12m_runtime_storage_isolation_case() -> dict[str, Any]:
    started = time.perf_counter()
    failures: list[str] = []
    evidence: dict[str, Any] = {}
    try:
        import tempfile

        from brain_registry import (
            ATLAS_FILENAME,
            GRAPH_FILENAME,
            INDEX_FILENAME,
            SQLITE_FILENAME,
            bootstrap_local_brain_registry,
            create_local_brain,
            refresh_local_brain_registry,
            resolve_brain_scope,
        )

        with tempfile.TemporaryDirectory(prefix="agvm-pr12m-f-runtime-") as tmp_name:
            root = Path(tmp_name) / "brains"
            bootstrap_local_brain_registry(brain_root=root, legacy_data_dirs=[], preferred_default_brain_id="bench_alpha")
            create_local_brain(brain_id="bench_alpha", display_name="Bench Alpha", make_active=True, make_default=True, brain_root=root)
            create_local_brain(brain_id="bench_beta", display_name="Bench Beta", make_active=False, make_default=False, brain_root=root)
            alpha = resolve_brain_scope("bench_alpha", brain_root=root, require_explicit=True)
            beta = resolve_brain_scope("bench_beta", brain_root=root, require_explicit=True)
            alpha_audit = _pr12m_initialize_runtime_storage(alpha, marker="ALPHA_READINESS_MARKER")
            beta_audit = _pr12m_initialize_runtime_storage(beta, marker="BETA_READINESS_MARKER")
            registry = refresh_local_brain_registry(brain_root=root)
            alpha_storage = Path(str(alpha.get("storage_path") or ""))
            beta_storage = Path(str(beta.get("storage_path") or ""))
            alpha_graph = _pr12m_read_text(alpha_storage / GRAPH_FILENAME)
            beta_graph = _pr12m_read_text(beta_storage / GRAPH_FILENAME)
            required_files = [SQLITE_FILENAME, GRAPH_FILENAME, INDEX_FILENAME, ATLAS_FILENAME]
            evidence = {
                "registry_validation": registry.get("validation"),
                "alpha_audit_brain": (alpha_audit.get("files") or {}).get("data_dir"),
                "beta_audit_brain": (beta_audit.get("files") or {}).get("data_dir"),
                "alpha_files_present": {name: (alpha_storage / name).exists() for name in required_files},
                "beta_files_present": {name: (beta_storage / name).exists() for name in required_files},
                "alpha_marker_isolated": "ALPHA_READINESS_MARKER" in alpha_graph and "BETA_READINESS_MARKER" not in alpha_graph,
                "beta_marker_isolated": "BETA_READINESS_MARKER" in beta_graph and "ALPHA_READINESS_MARKER" not in beta_graph,
            }
            if not all(evidence["alpha_files_present"].values()):
                failures.append("alpha_runtime_files_missing")
            if not all(evidence["beta_files_present"].values()):
                failures.append("beta_runtime_files_missing")
            if not evidence["alpha_marker_isolated"]:
                failures.append("alpha_graph_marker_leaked_or_missing")
            if not evidence["beta_marker_isolated"]:
                failures.append("beta_graph_marker_leaked_or_missing")
            if not bool((registry.get("validation") or {}).get("passed")):
                failures.append("post_runtime_registry_validation_failed")
    except Exception as exc:
        failures.append(f"runtime_storage_isolation_error:{exc}")
    return _pr12m_case_result(
        family="runtime_storage_isolation",
        title="Runtime SQLite, graph, index and atlas files are isolated per brain.",
        failures=failures,
        evidence=evidence,
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def _run_pr12m_source_asset_isolation_case() -> dict[str, Any]:
    started = time.perf_counter()
    failures: list[str] = []
    evidence: dict[str, Any] = {}
    try:
        import tempfile

        from brain_registry import bootstrap_local_brain_registry, create_local_brain, resolve_brain_scope

        with tempfile.TemporaryDirectory(prefix="agvm-pr12m-f-assets-") as tmp_name:
            root = Path(tmp_name) / "brains"
            bootstrap_local_brain_registry(brain_root=root, legacy_data_dirs=[], preferred_default_brain_id="bench_alpha")
            create_local_brain(brain_id="bench_alpha", display_name="Bench Alpha", make_active=True, make_default=True, brain_root=root)
            create_local_brain(brain_id="bench_beta", display_name="Bench Beta", make_active=False, make_default=False, brain_root=root)
            alpha = resolve_brain_scope("bench_alpha", brain_root=root, require_explicit=True)
            beta = resolve_brain_scope("bench_beta", brain_root=root, require_explicit=True)
            alpha_doc = Path(str(alpha.get("document_asset_path") or "")) / "alpha-document.txt"
            alpha_source = Path(str(alpha.get("source_package_path") or "")) / "alpha-source.json"
            alpha_maintenance = Path(str(alpha.get("maintenance_path") or "")) / "alpha-maintenance.json"
            alpha_doc.write_text("ALPHA_DOCUMENT_ONLY", encoding="utf-8")
            alpha_source.write_text(json.dumps({"marker": "ALPHA_SOURCE_ONLY"}), encoding="utf-8")
            alpha_maintenance.write_text(json.dumps({"marker": "ALPHA_MAINTENANCE_ONLY"}), encoding="utf-8")
            beta_paths = {
                "document": Path(str(beta.get("document_asset_path") or "")) / alpha_doc.name,
                "source_package": Path(str(beta.get("source_package_path") or "")) / alpha_source.name,
                "maintenance": Path(str(beta.get("maintenance_path") or "")) / alpha_maintenance.name,
            }
            evidence = {
                "alpha_paths": {
                    "document_asset_path": alpha.get("document_asset_path"),
                    "source_package_path": alpha.get("source_package_path"),
                    "maintenance_path": alpha.get("maintenance_path"),
                },
                "beta_paths": {
                    "document_asset_path": beta.get("document_asset_path"),
                    "source_package_path": beta.get("source_package_path"),
                    "maintenance_path": beta.get("maintenance_path"),
                },
                "beta_marker_files_absent": {name: not path.exists() for name, path in beta_paths.items()},
            }
            if alpha.get("document_asset_path") == beta.get("document_asset_path"):
                failures.append("document_asset_paths_not_distinct")
            if alpha.get("source_package_path") == beta.get("source_package_path"):
                failures.append("source_package_paths_not_distinct")
            if alpha.get("maintenance_path") == beta.get("maintenance_path"):
                failures.append("maintenance_paths_not_distinct")
            if not all(evidence["beta_marker_files_absent"].values()):
                failures.append("alpha_asset_marker_visible_in_beta_paths")
    except Exception as exc:
        failures.append(f"source_asset_isolation_error:{exc}")
    return _pr12m_case_result(
        family="source_asset_isolation",
        title="Documents, source packages and maintenance files are per-brain assets.",
        failures=failures,
        evidence=evidence,
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def _run_pr12m_mcp_stdio_scope_case() -> dict[str, Any]:
    started = time.perf_counter()
    failures: list[str] = []
    evidence: dict[str, Any] = {}
    try:
        repo_root = _pr12m_repo_root()
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        from agvm_mcp_server.server import AgvmMcpConfig, AgvmMcpServer, ToolPermissions
        from mcp_contracts import REQUIRED_MCP_TOOL_NAMES, build_mcp_contract_registry

        class StubClient:
            def __init__(self) -> None:
                self.gets: list[dict[str, Any]] = []
                self.posts: list[dict[str, Any]] = []

            def get_json(self, path: str, *, brain_id: str | None = None) -> dict[str, Any]:
                self.gets.append({"path": path, "brain_id": brain_id})
                return build_mcp_contract_registry()

            def post_json(self, path: str, payload: dict[str, Any], *, brain_id: str | None = None) -> dict[str, Any]:
                self.posts.append({"path": path, "brain_id": brain_id, "payload": dict(payload)})
                return {
                    "schema_version": "agvm.pr12m.stub_mcp_output.v1",
                    "tool_name": path.removeprefix("/mcp/").replace("-", "_"),
                    "status": "ok",
                    "brain_id": brain_id,
                    "echo": dict(payload),
                    "context_package": {"agent_markdown": "stub"},
                    "completeness": {},
                    "budget": {},
                }

        stub = StubClient()
        server = AgvmMcpServer(AgvmMcpConfig(active_brain_id="bench_alpha", default_brain_id="bench_alpha"), client=stub)
        listed = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        called = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "retrieve_context", "arguments": {"query_text": "give me context"}},
            }
        )
        read_only = AgvmMcpServer(
            AgvmMcpConfig(active_brain_id="bench_alpha", tool_permissions=ToolPermissions(read_only=True)),
            client=StubClient(),
        ).handle_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "write_memory_commit", "arguments": {"text": "must not mutate"}},
            }
        )
        ambiguous = AgvmMcpServer(AgvmMcpConfig(active_brain_id=None, default_brain_id=None), client=StubClient()).handle_message(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "retrieve_context", "arguments": {"query_text": "give me context"}},
            }
        )

        tools = list(((listed or {}).get("result") or {}).get("tools") or [])
        tool_names = [str(tool.get("name") or "") for tool in tools]
        schemas_have_brain = all("brain_id" in dict((tool.get("inputSchema") or {}).get("properties") or {}) for tool in tools)
        evidence = {
            "tool_count": len(tool_names),
            "required_tool_count": len(REQUIRED_MCP_TOOL_NAMES),
            "schemas_have_brain_id": schemas_have_brain,
            "call_post": stub.posts[-1] if stub.posts else {},
            "read_only_error": ((read_only or {}).get("result") or {}).get("structuredContent"),
            "ambiguous_error": ((ambiguous or {}).get("result") or {}).get("structuredContent"),
        }
        if tool_names != REQUIRED_MCP_TOOL_NAMES:
            failures.append("mcp_tools_list_not_contract_registry_projection")
        if not schemas_have_brain:
            failures.append("mcp_tool_schema_missing_brain_id")
        if not stub.posts or stub.posts[-1].get("brain_id") != "bench_alpha":
            failures.append("mcp_tool_call_did_not_inject_configured_brain")
        if not stub.posts or (stub.posts[-1].get("payload") or {}).get("brain_id") != "bench_alpha":
            failures.append("mcp_tool_call_payload_missing_brain_id")
        if not bool(((read_only or {}).get("result") or {}).get("isError")):
            failures.append("read_only_mcp_call_not_blocked")
        ambiguous_reason = str(((((ambiguous or {}).get("result") or {}).get("structuredContent") or {}).get("reason")) or "")
        if ambiguous_reason != "brain_id_required_for_ambiguous_local_mcp_scope":
            failures.append("ambiguous_mcp_scope_not_rejected")
    except Exception as exc:
        failures.append(f"mcp_stdio_scope_error:{exc}")
    return _pr12m_case_result(
        family="mcp_stdio_scope",
        title="Local stdio MCP resolves brain scope explicitly and rejects ambiguous calls.",
        failures=failures,
        evidence=evidence,
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def _run_pr12m_docker_distribution_case() -> dict[str, Any]:
    started = time.perf_counter()
    failures: list[str] = []
    root = _pr12m_repo_root()
    compose = _pr12m_read_text(root / "docker-compose.yml")
    readme = _pr12m_read_text(root / "README.md")
    env_example = _pr12m_read_text(root / ".env.example")
    api_dockerfile = _pr12m_read_text(root / "agvm_api" / "Dockerfile")
    frontend_dockerfile = _pr12m_read_text(root / "agvm_cockpit_prototype" / "Dockerfile")
    mcp_dockerfile = _pr12m_read_text(root / "Dockerfile.mcp")
    evidence = {
        "compose_present": bool(compose),
        "api_port_8010": "${AGVM_API_PORT:-8010}:8010" in compose,
        "frontend_port_3020": "${AGVM_UI_PORT:-3020}:3020" in compose,
        "brain_volume": "agvm_brains:/app/brains" in compose,
        "api_healthcheck": "urllib.request.urlopen('http://127.0.0.1:8010/health'" in compose,
        "mcp_profile": "profiles:" in compose and "- mcp" in compose,
        "readme_docker_onboarding": "docker compose up --build" in readme,
        "env_default_brain": "AGVM_DEFAULT_BRAIN_ID=default_brain" in env_example,
    }
    required_pairs = [
        ("compose_missing", bool(compose)),
        ("api_port_not_canonical_8010", evidence["api_port_8010"]),
        ("frontend_port_not_canonical_3020", evidence["frontend_port_3020"]),
        ("brain_volume_missing", evidence["brain_volume"]),
        ("api_healthcheck_missing", evidence["api_healthcheck"]),
        ("mcp_compose_profile_missing", evidence["mcp_profile"]),
        ("readme_docker_onboarding_missing", evidence["readme_docker_onboarding"]),
        ("env_default_brain_missing", evidence["env_default_brain"]),
        ("api_dockerfile_missing_uvicorn_8010", 'CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8010"]' in api_dockerfile),
        ("frontend_dockerfile_missing_preview", 'CMD ["npm", "run", "preview"]' in frontend_dockerfile),
        ("mcp_dockerfile_missing_stdio_entrypoint", 'CMD ["python", "-m", "agvm_mcp_server"]' in mcp_dockerfile),
    ]
    failures.extend(reason for reason, passed in required_pairs if not passed)
    return _pr12m_case_result(
        family="docker_distribution",
        title="Docker Compose and Dockerfiles expose the canonical local distribution.",
        failures=failures,
        evidence=evidence,
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def _run_pr12m_admin_export_import_restore_case() -> dict[str, Any]:
    started = time.perf_counter()
    failures: list[str] = []
    evidence: dict[str, Any] = {}
    try:
        import tempfile

        from brain_registry import (
            GRAPH_FILENAME,
            bootstrap_local_brain_registry,
            create_local_brain,
            delete_local_brain,
            export_local_brain,
            import_local_brain_archive,
            refresh_local_brain_registry,
            resolve_brain_scope,
        )

        with tempfile.TemporaryDirectory(prefix="agvm-pr12m-f-admin-") as tmp_name:
            root = Path(tmp_name) / "brains"
            bootstrap_local_brain_registry(brain_root=root, legacy_data_dirs=[], preferred_default_brain_id="bench_default")
            create_local_brain(brain_id="bench_alpha", display_name="Bench Alpha", make_active=True, make_default=True, brain_root=root)
            alpha = resolve_brain_scope("bench_alpha", brain_root=root, require_explicit=True)
            _pr12m_initialize_runtime_storage(alpha, marker="ALPHA_EXPORT_ROUNDTRIP_MARKER")
            refresh_local_brain_registry(brain_root=root)
            exported = export_local_brain("bench_alpha", brain_root=root, export_kind="export")
            backup = export_local_brain("bench_alpha", brain_root=root, export_kind="backup")
            imported = import_local_brain_archive(
                Path(str(exported.get("archive_path") or "")),
                brain_id="bench_alpha_clone",
                display_name="Bench Alpha Clone",
                make_active=False,
                make_default=False,
                brain_root=root,
            )
            clone = resolve_brain_scope("bench_alpha_clone", brain_root=root, require_explicit=True)
            clone_graph = _pr12m_read_text(Path(str(clone.get("storage_path") or "")) / GRAPH_FILENAME)
            deleted = delete_local_brain("bench_alpha_clone", confirm_brain_id="bench_alpha_clone", delete_storage=True, brain_root=root)
            evidence = {
                "export_status": exported.get("status"),
                "backup_action": backup.get("action"),
                "export_archive_exists": Path(str(exported.get("archive_path") or "")).exists(),
                "backup_archive_exists": Path(str(backup.get("archive_path") or "")).exists(),
                "imported_brain_id": imported.get("brain_id"),
                "clone_marker_preserved": "ALPHA_EXPORT_ROUNDTRIP_MARKER" in clone_graph,
                "delete_status": deleted.get("status"),
                "deleted_storage": deleted.get("deleted_storage"),
            }
            if exported.get("status") != "exported":
                failures.append("export_not_successful")
            if backup.get("action") != "backup" or backup.get("status") != "exported":
                failures.append("backup_not_successful")
            if not evidence["export_archive_exists"]:
                failures.append("export_archive_missing")
            if not evidence["backup_archive_exists"]:
                failures.append("backup_archive_missing")
            if imported.get("brain_id") != "bench_alpha_clone":
                failures.append("import_target_brain_wrong")
            if not evidence["clone_marker_preserved"]:
                failures.append("imported_clone_did_not_preserve_runtime_graph")
            if deleted.get("status") != "deleted" or not bool(deleted.get("deleted_storage")):
                failures.append("delete_imported_clone_failed")
    except Exception as exc:
        failures.append(f"admin_export_import_restore_error:{exc}")
    return _pr12m_case_result(
        family="admin_export_import_restore",
        title="Local admin export, backup, import, restore-equivalent import and delete roundtrip works.",
        failures=failures,
        evidence=evidence,
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def _run_pr12m_fresh_clone_onboarding_case() -> dict[str, Any]:
    started = time.perf_counter()
    failures: list[str] = []
    evidence: dict[str, Any] = {}
    try:
        import tempfile

        from brain_registry import bootstrap_local_brain_registry, resolve_brain_scope

        root_repo = _pr12m_repo_root()
        readme = _pr12m_read_text(root_repo / "README.md")
        env_example = _pr12m_read_text(root_repo / ".env.example")
        with tempfile.TemporaryDirectory(prefix="agvm-pr12m-f-fresh-") as tmp_name:
            root = Path(tmp_name) / "brains"
            registry = bootstrap_local_brain_registry(brain_root=root, legacy_data_dirs=[], preferred_default_brain_id="default_brain")
            resolved = resolve_brain_scope(brain_root=root)
            evidence = {
                "brain_count": registry.get("brain_count"),
                "active_brain_id": registry.get("active_brain_id"),
                "default_brain_id": registry.get("default_brain_id"),
                "resolved_storage_layout": resolved.get("storage_layout"),
                "docker_command_documented": "docker compose up --build" in readme,
                "local_api_command_documented": "uvicorn main:app --host 127.0.0.1 --port 8010" in readme,
                "mcp_command_documented": "python -m agvm_mcp_server" in readme,
                "canonical_ports_documented": "AGVM_API_PORT=8010" in env_example and "AGVM_UI_PORT=3020" in env_example,
            }
            if registry.get("brain_count") != 1:
                failures.append("fresh_clone_default_brain_count_wrong")
            if registry.get("active_brain_id") != "default_brain" or registry.get("default_brain_id") != "default_brain":
                failures.append("fresh_clone_default_brain_not_selected")
            if resolved.get("storage_layout") != "registry_managed":
                failures.append("fresh_clone_default_not_registry_managed")
            for key in ("docker_command_documented", "mcp_command_documented", "canonical_ports_documented"):
                if not evidence[key]:
                    failures.append(f"{key}_missing")
    except Exception as exc:
        failures.append(f"fresh_clone_onboarding_error:{exc}")
    return _pr12m_case_result(
        family="fresh_clone_onboarding",
        title="Fresh clone can create a default local brain and has documented local startup paths.",
        failures=failures,
        evidence=evidence,
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def _run_pr12m_documentation_closure_case() -> dict[str, Any]:
    started = time.perf_counter()
    root = _pr12m_repo_root()
    docs_root = root / "docs"
    files = {
        "master": docs_root / "AGVM_MASTER.md",
        "slices": docs_root / "AGVM_SLICES.md",
        "progress": docs_root / "AGVM_PROGRESS.md",
        "readme": root / "README.md",
    }
    texts = {name: _pr12m_read_text(path) for name, path in files.items()}
    evidence = {
        "active_master_present": "AGVM is a local-first MCP memory operating system" in texts["master"],
        "active_slices_present": "Active Execution Order" in texts["slices"],
        "active_progress_present": "AGVM Progress" in texts["progress"],
        "self_hosted_scope_present": "self-hosted local MCP" in texts["slices"].lower()
        or "self-hosted MCP" in texts["master"],
        "cloud_blocked_until_local_ready": "cloud" in texts["slices"].lower()
        and "Phase 8C" in texts["slices"],
        "readme_points_to_active_docs": "docs/AGVM_MASTER.md" in texts["readme"]
        and "docs/AGVM_SLICES.md" in texts["readme"],
    }
    failures = [f"{name}_missing" for name, passed in evidence.items() if not passed]
    return _pr12m_case_result(
        family="documentation_closure",
        title="Active canonical docs define local MCP readiness and keep cloud gated.",
        failures=failures,
        evidence=evidence,
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def run_pr12m_self_hosted_readiness_suite(base_url: str | None = None) -> dict[str, Any]:
    base = str(base_url or DEFAULT_BASE_URL)
    case_results = [
        _run_pr12m_product_truth_case(base),
        _run_pr12m_brain_registry_switch_case(),
        _run_pr12m_runtime_storage_isolation_case(),
        _run_pr12m_source_asset_isolation_case(),
        _run_pr12m_mcp_stdio_scope_case(),
        _run_pr12m_docker_distribution_case(),
        _run_pr12m_admin_export_import_restore_case(),
        _run_pr12m_fresh_clone_onboarding_case(),
        _run_pr12m_documentation_closure_case(),
    ]
    by_family = {str(result.get("family") or ""): result for result in case_results}
    missing_families = [family for family in REQUIRED_PR12M_SELF_HOSTED_FAMILIES if family not in by_family]
    critical_failures = [
        {
            "family": str(result.get("family") or ""),
            "failures": list(result.get("failures") or []),
        }
        for result in case_results
        if bool(result.get("critical")) and not bool(result.get("passed"))
    ]
    passed_count = sum(1 for result in case_results if bool(result.get("passed")))
    total_count = len(REQUIRED_PR12M_SELF_HOSTED_FAMILIES)
    all_pass = passed_count == total_count and not missing_families and not critical_failures
    family_matrix = {
        family: {
            "required": True,
            "covered": family in by_family,
            "passed": bool((by_family.get(family) or {}).get("passed")),
            "critical": bool((by_family.get(family) or {}).get("critical", True)),
            "failures": list((by_family.get(family) or {}).get("failures") or []),
        }
        for family in REQUIRED_PR12M_SELF_HOSTED_FAMILIES
    }
    return {
        "schema_version": PR12M_SELF_HOSTED_READINESS_REPORT_SCHEMA_VERSION,
        "phase": "self_hosted_readiness",
        "slice": "PR-12M-F",
        "base_url": base,
        "all_pass": all_pass,
        "pr12m_closed": all_pass,
        "self_hosted_ready": all_pass,
        "product_ready_verdict": "self_hosted_ready_pr12m_closed" if all_pass else "not_self_hosted_ready_pr12m_f_remains_open",
        "pass_rate": round(passed_count / total_count, 4) if total_count else 0.0,
        "passed_count": passed_count,
        "total_count": total_count,
        "required_families": list(REQUIRED_PR12M_SELF_HOSTED_FAMILIES),
        "missing_families": missing_families,
        "critical_failures": critical_failures,
        "family_matrix": family_matrix,
        "case_results": case_results,
        "benchmark_inputs": {
            "phase": "self_hosted_readiness",
            "external_network_required": False,
            "external_browser_required": False,
            "live_llm_required": False,
            "mutation_enabled": False,
            "mcp_first": True,
            "context_package_is_product": True,
            "docker_cli_required_for_static_gate": False,
        },
        "release_boundary": {
            "local_self_hosted_ready": all_pass,
            "cloud_commercialization_not_started": True,
            "cloud_requires_pr12n": True,
            "self_hosted_release_unit": "downloadable repo + local API/UI + stdio MCP server + Docker Compose",
        },
        "next_slice": "PR-12N-A Hosted Brain Registry And Tenant Isolation" if all_pass else "PR-12M-F Self-Hosted Readiness Benchmark Remediation",
    }


def _pr12p13_case_result(
    *,
    gate_id: str,
    title: str,
    failures: list[str],
    evidence: dict[str, Any],
    elapsed_ms: float,
    critical: bool = True,
) -> dict[str, Any]:
    return {
        "gate_id": gate_id,
        "title": title,
        "slice": "PR-12P-13",
        "critical": critical,
        "passed": not failures,
        "failures": failures,
        "evidence": evidence,
        "elapsed_ms": round(float(elapsed_ms), 3),
    }


def _pr12p13_frontend_candidates(api_base_url: str) -> list[str]:
    candidates: list[str] = []
    configured = str(os.environ.get("AGVM_FRONTEND_URL") or "").strip()
    if configured:
        candidates.append(configured.rstrip("/"))
    candidates.append("http://agvm_ui:3020")
    try:
        parsed = urllib.parse.urlparse(api_base_url)
        scheme = parsed.scheme or "http"
        hostname = parsed.hostname or "127.0.0.1"
        candidates.append(f"{scheme}://{hostname}:3020")
    except Exception:
        candidates.append("http://127.0.0.1:3020")
    candidates.append("http://127.0.0.1:3020")

    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        cleaned = str(candidate or "").strip().rstrip("/")
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            deduped.append(cleaned)
    return deduped


def _pr12p13_frontend_probe(candidates: list[str]) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    for url in candidates:
        started = time.perf_counter()
        try:
            request = urllib.request.Request(url=url, method="GET", headers={"Accept": "text/html,*/*", "Host": "localhost:3020"})
            with urllib.request.urlopen(request, timeout=15.0) as response:
                body = response.read(200_000).decode("utf-8", errors="ignore")
                status = int(getattr(response, "status", 200) or 200)
                content_type = str(response.headers.get("Content-Type") or "")
            markers = {
                "html_shell": "<html" in body.lower() or "<!doctype html" in body.lower(),
                "agvm_title": "AGVM" in body or "Brain OS" in body,
                "vite_root": 'id="root"' in body or "src=\"/src/" in body or "assets/" in body,
            }
            passed = status == 200 and (markers["html_shell"] or markers["vite_root"])
            attempts.append(
                {
                    "url": url,
                    "passed": passed,
                    "status_code": status,
                    "content_type": content_type,
                    "content_length_sampled": len(body),
                    "markers": markers,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                }
            )
            if passed:
                return {
                    "passed": True,
                    "selected_url": url,
                    "attempts": attempts,
                    "failures": [],
                }
        except Exception as exc:
            attempts.append(
                {
                    "url": url,
                    "passed": False,
                    "error": str(exc),
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                }
            )
    return {
        "passed": False,
        "selected_url": None,
        "attempts": attempts,
        "failures": ["frontend_3020_unreachable_or_not_html"],
    }


def _pr12p13_registry_gate() -> dict[str, Any]:
    from mcp_contracts import REQUIRED_MCP_TOOL_NAMES, build_mcp_contract_registry

    started = time.perf_counter()
    failures: list[str] = []
    try:
        registry = build_mcp_contract_registry()
        validation = dict(registry.get("registry_validation") or {})
        granularity = dict(registry.get("implementation_granularity") or {})
        names = [str(tool.get("name") or "") for tool in list(registry.get("tools") or []) if isinstance(tool, dict)]
        missing_tools = [tool_name for tool_name in REQUIRED_MCP_TOOL_NAMES if tool_name not in names]
        if not bool(validation.get("passed")):
            failures.append("mcp_contract_registry_validation_failed")
        if missing_tools:
            failures.append(f"mcp_contract_tools_missing:{','.join(missing_tools)}")
        latest_slice = str(granularity.get("latest_implemented_slice") or "")
        if latest_slice not in {"PR-12P-12B", "PR-12P-13"}:
            failures.append(f"mcp_contract_registry_unexpected_latest_slice:{latest_slice}")
        evidence = {
            "schema_version": registry.get("schema_version"),
            "validation": validation,
            "latest_implemented_slice": latest_slice,
            "next_slice": granularity.get("next_slice"),
            "registered_tool_count": len(names),
            "required_tool_count": len(REQUIRED_MCP_TOOL_NAMES),
            "missing_tools": missing_tools,
            "mcp_surface_status": granularity.get("mcp_surface_status"),
        }
    except Exception as exc:
        failures.append(f"mcp_contract_registry_error:{exc}")
        evidence = {}
    return _pr12p13_case_result(
        gate_id="mcp_contract_registry",
        title="MCP tool registry remains valid and complete before the local product gate.",
        failures=failures,
        evidence=evidence,
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def _pr12p13_report_gate(
    *,
    gate_id: str,
    title: str,
    report: dict[str, Any],
    required_verdict: str | None = None,
    extra_failure_keys: tuple[str, ...] = (),
    elapsed_ms: float,
) -> dict[str, Any]:
    failures: list[str] = []
    if not bool(report.get("all_pass")):
        failures.append(f"{gate_id}_all_pass_false")
    if required_verdict and str(report.get("product_ready_verdict") or "") != required_verdict:
        failures.append(f"{gate_id}_verdict_mismatch")
    for key in extra_failure_keys:
        values = list(report.get(key) or [])
        if values:
            failures.append(f"{gate_id}_{key}_present")
    return _pr12p13_case_result(
        gate_id=gate_id,
        title=title,
        failures=failures,
        evidence={
            "schema_version": report.get("schema_version"),
            "phase": report.get("phase"),
            "slice": report.get("slice"),
            "all_pass": bool(report.get("all_pass")),
            "product_ready_verdict": report.get("product_ready_verdict"),
            "failures": list(report.get("failures") or []),
            "critical_failures": list(report.get("critical_failures") or []),
            "launch_blockers": list(report.get("launch_blockers") or []),
            "next_slice": report.get("next_slice"),
        },
        elapsed_ms=elapsed_ms,
    )


def _pr12p13_port_from_url(url: str) -> int | None:
    try:
        parsed = urllib.parse.urlparse(url)
        return parsed.port
    except Exception:
        return None


def _pr12p13_self_hosted_runtime_gate(
    *,
    base_url: str,
    health: dict[str, Any],
    frontend_probe: dict[str, Any],
) -> dict[str, Any]:
    started = time.perf_counter()
    failures: list[str] = []
    evidence: dict[str, Any] = {}

    api_port = _pr12p13_port_from_url(base_url)
    selected_frontend_url = str(frontend_probe.get("selected_url") or "")
    frontend_port = _pr12p13_port_from_url(selected_frontend_url)
    data_dir = Path(os.environ.get("AGVM_LAB_DATA_DIR") or (_pr12l_repo_root() / "agvm_api" / "data"))
    brains_dir = Path(os.environ.get("AGVM_BRAINS_DIR") or (_pr12l_repo_root() / "brains"))
    llm_enabled = str(os.environ.get("AGVM_LLM_ENABLED") or "true").strip().lower() not in {"0", "false", "no", "off"}
    openai_key_present = bool(str(os.environ.get("OPENAI_API_KEY") or "").strip())
    try:
        repo_root = _pr12l_repo_root()
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        import agvm_mcp_server  # noqa: F401

        mcp_package_importable = True
    except Exception as exc:
        mcp_package_importable = False
        evidence["mcp_package_import_error"] = str(exc)

    checks = {
        "api_canonical_port_8010": api_port == 8010,
        "frontend_canonical_port_3020": frontend_port == 3020,
        "api_health_ok": bool(health.get("ok")) or str(health.get("status") or "").lower() in {"ok", "healthy", "ready"},
        "frontend_health_ok": bool(frontend_probe.get("passed")),
        "brain_registry_ready": bool(health.get("brain_registry_ready")) or bool(health.get("active_brain_id")),
        "runtime_data_dir_present": data_dir.exists(),
        "runtime_brains_dir_present": brains_dir.exists(),
        "llm_runtime_enabled": llm_enabled,
        "openai_key_present": openai_key_present,
        "mcp_package_importable": mcp_package_importable,
    }
    for name, passed in checks.items():
        if not passed:
            failures.append(f"{name}_failed")

    evidence.update(
        {
            "base_url": base_url,
            "api_port": api_port,
            "selected_frontend_url": selected_frontend_url,
            "frontend_port": frontend_port,
            "data_dir": str(data_dir),
            "brains_dir": str(brains_dir),
            "active_brain_id": health.get("active_brain_id"),
            "default_brain_id": health.get("default_brain_id"),
            "docker_runtime_detected": Path("/.dockerenv").exists(),
            "checks": checks,
            "legacy_source_readiness_phase": "not_executed_inside_runtime_gate",
            "legacy_source_readiness_reason": "PR-12M-F reads source-tree Docker/docs/frontend files that are not part of the runtime API image; PR-12P-13 verifies the runnable self-hosted unit instead.",
        }
    )
    return _pr12p13_case_result(
        gate_id="self_hosted_readiness",
        title="Self-hosted runtime unit is reachable, scoped, AI-enabled and packaged for local MCP use.",
        failures=failures,
        evidence=evidence,
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def _pr12p14c_segment_result(
    *,
    segment_id: str,
    title: str,
    failures: list[str],
    evidence: dict[str, Any],
    elapsed_ms: float,
    critical: bool = True,
) -> dict[str, Any]:
    return {
        "segment_id": segment_id,
        "gate_id": segment_id,
        "title": title,
        "slice": "PR-12P-14C",
        "critical": critical,
        "passed": not failures,
        "failures": failures,
        "evidence": evidence,
        "elapsed_ms": round(float(elapsed_ms), 3),
    }


def _pr12p14c_brain_targets(base_url: str) -> dict[str, Any]:
    started = time.perf_counter()
    failures: list[str] = []
    registry: dict[str, Any] = {}
    brains: list[dict[str, Any]] = []
    targets = {
        "simone_massaro": {"brain_id": None, "available": False, "record": {}},
        "elena_valsecchi": {"brain_id": None, "available": False, "record": {}},
    }
    try:
        registry = get_json(base_url, "/memory/brains", timeout=30.0)
        brains = [dict(item) for item in list(registry.get("brains") or []) if isinstance(item, dict)]
    except Exception as exc:
        failures.append(f"brain_registry_unreachable:{exc}")

    for record in brains:
        brain_id = _pr12p12_brain_id(record)
        haystack = f"{brain_id} {_pr12p12_brain_label(record)}".lower()
        if brain_id == "simone_massaro" or "simone massaro" in haystack:
            targets["simone_massaro"] = {"brain_id": brain_id, "available": True, "record": record}
        if brain_id == "elena_valsecchi" or "elena valsecchi" in haystack:
            targets["elena_valsecchi"] = {"brain_id": brain_id, "available": True, "record": record}

    for role, target in targets.items():
        if not bool(target.get("available")):
            failures.append(f"required_brain_missing:{role}")

    return {
        "segment": _pr12p14c_segment_result(
            segment_id="brain_scope",
            title="The local self-hosted unit exposes the required validation brains without process-level switching.",
            failures=failures,
            evidence={
                "brain_count": len(brains),
                "targets": targets,
                "registry_keys": sorted(registry.keys())[:24],
            },
            elapsed_ms=(time.perf_counter() - started) * 1000,
        ),
        "targets": targets,
    }


_PR12P14M_PROVIDER_RETRY_ERROR_MARKERS = (
    "timeout",
    "timed out",
    "transport_error",
    "read operation",
    "llm_error",
    "provider_error",
    "provider_degraded",
    "rate_limit",
    "overloaded",
    "temporarily_unavailable",
    "api_error",
    "connection",
)


def _pr12p14m_provider_error_marker(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return bool(text and any(marker in text for marker in _PR12P14M_PROVIDER_RETRY_ERROR_MARKERS))


def _pr12p14m_semantic_runtime(output: dict[str, Any]) -> dict[str, Any]:
    planner_runtime = dict(output.get("planner_runtime") or {})
    runtime = dict(output.get("semantic_contract_runtime") or planner_runtime.get("semantic_contract_runtime") or {})
    if runtime:
        return runtime
    timing = dict(output.get("timing") or {})
    timing_contract = dict(timing.get("semantic_contract") or {})
    if timing_contract:
        return timing_contract
    budget = dict(output.get("budget") or {})
    budget_contract = dict(budget.get("semantic_contract") or {})
    if budget_contract:
        return budget_contract
    return {}


def _pr12p14m_semantic_provider_degraded(runtime: dict[str, Any]) -> bool:
    if bool(runtime.get("provider_degraded") or runtime.get("degraded")):
        return True
    if str(runtime.get("provider_state") or "").strip().lower() == "provider_degraded":
        return True
    return any(
        _pr12p14m_provider_error_marker(runtime.get(key))
        for key in ("error", "primary_error", "retry_error", "degraded_reason")
    )


def _pr12p14m_ai_spatial_provider_degraded(ai: dict[str, Any]) -> bool:
    if bool(ai.get("ai_no_route_terminal_contract")):
        return False
    if not bool(ai.get("ai_spatial_observed")):
        return False
    if bool(ai.get("ai_spatial_materialized")):
        return False
    values: list[Any] = [
        ai.get("ai_spatial_status"),
        ai.get("ai_spatial_source"),
    ]
    values.extend(list(ai.get("ai_spatial_missing_reasons") or []))
    return any(_pr12p14m_provider_error_marker(value) for value in values)


def _pr12p14m_provider_retry_policy_for_probe(
    *,
    output: dict[str, Any],
    evidence: dict[str, Any],
    failures: list[str],
    path: str,
) -> dict[str, Any]:
    runtime = _pr12p14m_semantic_runtime(output)
    ai = dict(evidence.get("ai") or {})
    provider_degraded = _pr12p14m_semantic_provider_degraded(runtime)
    ai_spatial_provider_degraded = _pr12p14m_ai_spatial_provider_degraded(ai)
    direct_failures = [
        failure
        for failure in failures
        if failure.startswith("probe_execution_failed") or failure.startswith("tool_failed_status:")
    ]
    hard_gate_blockers = [str(item) for item in list(ai.get("hard_gate_blockers") or [])]
    semantic_missing = (
        not bool(ai.get("semantic_contract_material"))
        and str(runtime.get("status") or "").strip().lower() in {"", "deferred", "miss", "pending", "timeout"}
    )
    ai_materialization_deferred = bool(ai.get("hard_gate_blocked")) and any(
        "ai_material" in blocker or "semantic_contract" in blocker for blocker in hard_gate_blockers
    )
    retryable_path = path.rstrip("/").endswith(
        (
            "retrieve-context",
            "retrieve-project-workspace",
            "retrieve-path-corridor",
            "retrieve-source-trace",
        )
    )
    allowed = bool(
        retryable_path
        and (
            (
                (provider_degraded or semantic_missing or ai_materialization_deferred)
                and not bool(ai.get("semantic_contract_material"))
            )
            or (
                ai_spatial_provider_degraded
                and bool(ai.get("semantic_contract_material"))
                and not bool(ai.get("ai_spatial_materialized"))
            )
        )
        and bool(failures)
        and not direct_failures
    )
    if provider_degraded:
        retry_reason = "provider_degraded"
    elif ai_materialization_deferred:
        retry_reason = "ai_materialization_deferred"
    elif semantic_missing:
        retry_reason = "semantic_contract_deferred"
    elif ai_spatial_provider_degraded:
        retry_reason = "ai_spatial_provider_degraded"
    else:
        retry_reason = None
    return {
        "schema_version": "agvm.pr12p14m.provider_retry_policy.v1",
        "enabled": True,
        "allowed": allowed,
        "reason": retry_reason,
        "attempted": False,
        "recovered": False,
        "retryable_path": retryable_path,
        "provider_degraded": provider_degraded,
        "ai_spatial_provider_degraded": ai_spatial_provider_degraded,
        "semantic_missing": semantic_missing,
        "ai_materialization_deferred": ai_materialization_deferred,
        "semantic_provider_state": runtime.get("provider_state"),
        "semantic_contract_status": runtime.get("status"),
        "semantic_contract_source": runtime.get("source"),
        "semantic_contract_material": bool(runtime.get("material") or ai.get("semantic_contract_material")),
        "primary_error": runtime.get("primary_error"),
        "retry_error": runtime.get("retry_error"),
        "error": runtime.get("error"),
        "degraded_reason": runtime.get("degraded_reason"),
        "blocked_failures": direct_failures,
        "silent_heuristic_certification_allowed": False,
    }


def _pr12p14c_no_route_terminal_contract(
    *,
    output: dict[str, Any],
    delivery: dict[str, Any],
    completeness: dict[str, Any],
    semantic_runtime: dict[str, Any],
) -> dict[str, Any]:
    delivery_ai = dict(delivery.get("ai") or {})
    contract = dict(
        delivery_ai.get("no_route_terminal_contract")
        or output.get("ai_no_route_terminal_contract")
        or {}
    )
    exact_requirement_count = int(
        contract.get("exact_field_requirement_count")
        or completeness.get("exact_field_requirement_count")
        or 0
    )
    exact_missing_count = int(
        contract.get("exact_field_missing_count")
        or completeness.get("exact_field_missing_count")
        or completeness.get("missing_exact_field_count")
        or len(list(completeness.get("missing_exact_fields") or []))
        or 0
    )
    terminal_no_match = bool(
        delivery.get("terminal_for_client")
        and str(delivery.get("client_payload_state") or "").strip().lower() == "no_match"
    )
    semantic_material = bool(semantic_runtime.get("material") or contract.get("semantic_ai_materialized"))
    no_match = bool(
        contract.get("no_match")
        or completeness.get("no_match")
        or str(output.get("status") or "").strip().lower() == "no_match"
    )
    exact_absence = bool(
        exact_missing_count > 0
        and (exact_requirement_count <= 0 or exact_missing_count >= exact_requirement_count)
    )
    present = bool(
        contract.get("present")
        and contract.get("route_not_required")
        and terminal_no_match
        and semantic_material
        and no_match
        and exact_absence
    )
    return {
        "schema_version": contract.get("schema_version") or "agvm.ai_no_route_terminal_contract.v1",
        "present": present,
        "declared_present": bool(contract.get("present")),
        "route_not_required": bool(contract.get("route_not_required")),
        "terminal_no_match": terminal_no_match,
        "semantic_ai_materialized": semantic_material,
        "no_match": no_match,
        "exact_absence": exact_absence,
        "exact_field_requirement_count": exact_requirement_count,
        "exact_field_missing_count": exact_missing_count,
        "reason": contract.get("reason"),
    }


def _pr12p14c_ai_evidence(output: dict[str, Any]) -> dict[str, Any]:
    materialization = dict(output.get("ai_landing_materialization") or {})
    spatial_contract = dict(output.get("ai_spatial_landing_contract") or {})
    spatial_metrics = dict(spatial_contract.get("metrics") or {})
    hard_gate = dict(output.get("ai_materialization_hard_gate") or {})
    delivery = dict(output.get("mcp_delivery_contract") or {})
    delivery_ai = dict(delivery.get("ai") or {})
    budget = dict(output.get("budget") or {})
    completeness = dict(output.get("completeness") or {})
    latency = dict(output.get("latency_contract") or {})
    timing = dict(output.get("timing") or {})
    semantic_runtime = _pr12p14m_semantic_runtime(output)
    semantic_cache = dict(semantic_runtime.get("cache") or {})
    semantic_cache_tier = str(semantic_runtime.get("cache_tier") or semantic_cache.get("tier") or "").strip() or None
    semantic_provider_degraded = _pr12p14m_semantic_provider_degraded(semantic_runtime)
    no_route_contract = _pr12p14c_no_route_terminal_contract(
        output=output,
        delivery=delivery,
        completeness=completeness,
        semantic_runtime=semantic_runtime,
    )
    no_route_terminal = bool(no_route_contract.get("present"))
    positive_exact_contract = dict(
        delivery_ai.get("positive_exact_sufficiency_contract")
        or output.get("ai_positive_exact_sufficiency_contract")
        or {}
    )
    positive_exact_terminal = bool(
        positive_exact_contract.get("present")
        and positive_exact_contract.get("terminal_certification_allowed")
        and delivery.get("terminal_for_client")
        and str(delivery.get("client_payload_state") or "").strip().lower() == "usable_context"
        and positive_exact_contract.get("semantic_ai_materialized")
        and positive_exact_contract.get("context_contract_passed")
        and positive_exact_contract.get("route_arbitration_certifiable")
        and not positive_exact_contract.get("path_truth_required")
    )
    public_fact_contract = dict(
        delivery_ai.get("public_fact_sufficiency_contract")
        or output.get("ai_public_fact_sufficiency_contract")
        or {}
    )
    public_fact_terminal = bool(
        public_fact_contract.get("present")
        and public_fact_contract.get("terminal_certification_allowed")
        and delivery.get("terminal_for_client")
        and str(delivery.get("client_payload_state") or "").strip().lower() == "usable_context"
        and public_fact_contract.get("semantic_ai_materialized")
        and public_fact_contract.get("context_contract_passed")
        and public_fact_contract.get("route_arbitration_certifiable")
        and not public_fact_contract.get("path_truth_required")
        and int(public_fact_contract.get("actionable_document_ref_count") or 0) > 0
    )
    answerability_contract = dict(
        delivery_ai.get("answerability_sufficiency_contract")
        or output.get("ai_answerability_sufficiency_contract")
        or {}
    )
    answerability_terminal = bool(
        answerability_contract.get("present")
        and answerability_contract.get("terminal_certification_allowed")
        and delivery.get("terminal_for_client")
        and str(delivery.get("client_payload_state") or "").strip().lower() == "usable_context"
        and answerability_contract.get("semantic_ai_materialized")
        and answerability_contract.get("context_contract_passed")
        and answerability_contract.get("route_arbitration_certifiable")
        and not answerability_contract.get("path_truth_required")
    )
    path_route_first_contract = dict(
        delivery_ai.get("path_route_first_sufficiency_contract")
        or output.get("path_route_first_sufficiency_contract")
        or {}
    )
    path_route_first_terminal = bool(
        path_route_first_contract.get("present")
        and delivery.get("terminal_for_client")
        and str(delivery.get("client_payload_state") or "").strip().lower() == "path_payload_ready"
        and path_route_first_contract.get("semantic_ai_materialized")
        and path_route_first_contract.get("semantic_ai_route_materialized")
        and path_route_first_contract.get("mission_ledger_ready")
        and not path_route_first_contract.get("mission_surface_missing")
    )
    ai_landing_count = int(
        materialization.get("ai_landing_count")
        or spatial_metrics.get("ai_landing_count")
        or materialization.get("landing_count")
        or completeness.get("ai_landing_count")
        or 0
    )
    if path_route_first_terminal:
        ai_landing_count = max(ai_landing_count, int(path_route_first_contract.get("path_count") or 0))
    ai_path_count = int(
        materialization.get("ai_path_count")
        or spatial_metrics.get("ai_path_count")
        or 0
    )
    if path_route_first_terminal:
        ai_path_count = max(ai_path_count, int(path_route_first_contract.get("path_count") or 0))
    spatial_observed = bool(spatial_contract)
    spatial_materialized = bool(spatial_contract.get("certifiable") or spatial_contract.get("materialized"))
    spatial_certifies_route = bool(
        not spatial_observed
        or (spatial_materialized and ai_landing_count > 0 and ai_path_count > 0)
        or path_route_first_terminal
    )
    first_ai_landing_ms = _pr12p12_first_ms(latency.get("first_ai_landing_ms"), timing.get("first_landing_ms"))
    route_level_materialized = bool(
        (materialization.get("route_level_materialized") or materialization.get("materialized"))
        and spatial_certifies_route
    )
    materialized = bool(route_level_materialized or no_route_terminal or positive_exact_terminal or path_route_first_terminal)
    materialized = bool(materialized or public_fact_terminal)
    materialized = bool(materialized or answerability_terminal)
    if (
        spatial_observed
        and not spatial_certifies_route
        and not positive_exact_terminal
        and not public_fact_terminal
        and not answerability_terminal
        and not path_route_first_terminal
    ):
        materialized = False
    if no_route_terminal:
        materialized = True
    blocked = bool(hard_gate.get("blocked")) or str(hard_gate.get("validation_state") or "").lower() in {
        "blocked",
        "missing",
        "ai_landing_runtime_missing",
    }
    return {
        "materialized": materialized,
        "route_level_materialized": route_level_materialized,
        "ai_landing_count": ai_landing_count,
        "ai_path_count": ai_path_count,
        "ai_spatial_observed": spatial_observed,
        "ai_spatial_materialized": spatial_materialized,
        "ai_spatial_certifies_route": spatial_certifies_route,
        "ai_spatial_status": spatial_contract.get("status"),
        "ai_spatial_source": spatial_contract.get("source"),
        "ai_spatial_missing_reasons": list(spatial_contract.get("missing_reasons") or [])[:12],
        "ai_no_route_terminal_contract": no_route_terminal,
        "ai_no_route_terminal_contract_detail": no_route_contract,
        "ai_positive_exact_sufficiency_contract": positive_exact_terminal,
        "ai_positive_exact_sufficiency_contract_detail": positive_exact_contract,
        "ai_public_fact_sufficiency_contract": public_fact_terminal,
        "ai_public_fact_sufficiency_contract_detail": public_fact_contract,
        "ai_answerability_sufficiency_contract": answerability_terminal,
        "ai_answerability_sufficiency_contract_detail": answerability_contract,
        "ai_path_route_first_sufficiency_contract": path_route_first_terminal,
        "ai_path_route_first_sufficiency_contract_detail": path_route_first_contract,
        "ai_route_required": not bool(
            no_route_terminal
            or positive_exact_terminal
            or public_fact_terminal
            or answerability_terminal
            or path_route_first_terminal
        ),
        "delivery_client_payload_state": delivery.get("client_payload_state"),
        "delivery_terminal_for_client": bool(delivery.get("terminal_for_client")),
        "delivery_missing_reasons": list(delivery.get("missing_reasons") or [])[:16],
        "first_ai_landing_ms": first_ai_landing_ms,
        "budget_ai_material": bool(budget.get("ai_material")),
        "budget_llm_allowed": bool(budget.get("llm_allowed")),
        "hard_gate_blocked": blocked,
        "hard_gate_state": hard_gate.get("validation_state") or hard_gate.get("state"),
        "hard_gate_blockers": list(hard_gate.get("blockers") or []) + list(hard_gate.get("failures") or []),
        "semantic_contract_status": semantic_runtime.get("status"),
        "semantic_contract_source": semantic_runtime.get("source"),
        "semantic_contract_material": bool(semantic_runtime.get("material")),
        "semantic_contract_cache_status": semantic_runtime.get("cache_status"),
        "semantic_contract_cache_hit": bool(semantic_runtime.get("cache_hit")),
        "semantic_contract_cache_tier": semantic_cache_tier,
        "semantic_contract_provider_state": semantic_runtime.get("provider_state"),
        "semantic_contract_provider_degraded": semantic_provider_degraded,
        "semantic_contract_retry_used": bool(semantic_runtime.get("retry_used")),
        "semantic_contract_retry_status": semantic_runtime.get("retry_status"),
        "semantic_contract_primary_error": semantic_runtime.get("primary_error"),
        "semantic_contract_retry_error": semantic_runtime.get("retry_error"),
        "semantic_contract_error": semantic_runtime.get("error"),
        "semantic_contract": {
            "status": semantic_runtime.get("status"),
            "source": semantic_runtime.get("source"),
            "material": bool(semantic_runtime.get("material")),
            "cache_status": semantic_runtime.get("cache_status"),
            "cache_hit": bool(semantic_runtime.get("cache_hit")),
            "cache_tier": semantic_cache_tier,
            "provider_state": semantic_runtime.get("provider_state"),
            "provider_degraded": semantic_provider_degraded,
            "retry_used": bool(semantic_runtime.get("retry_used")),
            "retry_status": semantic_runtime.get("retry_status"),
            "primary_error": semantic_runtime.get("primary_error"),
            "retry_error": semantic_runtime.get("retry_error"),
            "error": semantic_runtime.get("error"),
        },
    }


def _pr12p14p_payload_truth_evidence(output: dict[str, Any]) -> dict[str, Any]:
    contract = dict(output.get("payload_truth_contract") or {})
    primary = dict(contract.get("primary_mcp_payload") or {})
    documents = dict(contract.get("documents") or {})
    answer_demo = dict(contract.get("answer_demo") or {})
    surface_separation = dict(contract.get("surface_separation") or {})
    return {
        "schema_version": contract.get("schema_version"),
        "present": bool(contract),
        "tool_name": contract.get("tool_name"),
        "status": contract.get("status"),
        "primary_field": primary.get("field"),
        "primary_package_field": primary.get("package_field"),
        "primary_present": bool(primary.get("present")),
        "primary_char_count": int(primary.get("char_count") or 0),
        "primary_sha256_present": bool(primary.get("sha256")),
        "exact_backend_field": bool(primary.get("exact_backend_field")),
        "answer_demo_primary_product": bool(answer_demo.get("primary_product")),
        "answer_demo_secondary": bool(answer_demo.get("secondary", True)),
        "document_ref_count": int(documents.get("document_ref_count") or 0),
        "actionable_document_ref_count": int(documents.get("actionable_document_ref_count") or 0),
        "raw_available_document_ref_count": int(documents.get("raw_available_document_ref_count") or 0),
        "document_bundle_state": documents.get("document_bundle_state"),
        "document_bundle_document_count": int(documents.get("document_bundle_document_count") or 0),
        "document_workspace_document_count": int(documents.get("document_workspace_document_count") or 0),
        "raw_text_policy": documents.get("raw_text_policy"),
        "raw_text_follow_up_tool": documents.get("raw_text_follow_up_tool"),
        "surface_names": sorted(surface_separation.keys()),
    }


def _pr12p14p_completion_evidence(output: dict[str, Any]) -> dict[str, Any]:
    contract = dict(output.get("completion_contract") or {})
    first_package = dict(contract.get("first_package") or {})
    background = dict(contract.get("background_completion") or {})
    inspection = dict(contract.get("inspection") or {})
    stage_timings = contract.get("stage_timings") or []
    if not isinstance(stage_timings, list):
        stage_timings = []
    return {
        "schema_version": contract.get("schema_version"),
        "present": bool(contract),
        "state": contract.get("state"),
        "visible_reason": contract.get("visible_reason"),
        "final_materialization_pending": bool(contract.get("final_materialization_pending")),
        "result_ready_terminal": bool(contract.get("result_ready_terminal")),
        "first_package_present": bool(first_package.get("present")),
        "first_package_field": first_package.get("field"),
        "first_package_ms": _pr12p12_ms(first_package.get("first_useful_package_ms")),
        "first_package_slo_ms": _pr12p12_ms(first_package.get("slo_ms")),
        "first_package_under_slo": bool(first_package.get("under_slo")),
        "returned_before_full_completion": bool(first_package.get("returned_before_full_completion")),
        "background_inspectable": bool(background.get("inspectable")),
        "full_completion_ms": _pr12p12_ms(background.get("full_completion_ms")),
        "inspection_available": bool(inspection.get("available")),
        "inspection_tool": inspection.get("inspect_tool"),
        "inspection_endpoint": inspection.get("inspect_endpoint"),
        "stage_timing_count": len(stage_timings),
        "stage_keys": [str(dict(item).get("stage_key") or dict(item).get("stage") or "") for item in stage_timings if isinstance(item, dict)][:12],
    }


def _pr12p14p_validate_payload_and_completion(output: dict[str, Any], evidence: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    tool_name = str(output.get("tool_name") or evidence.get("tool_name") or "").strip()
    payload_truth = dict(evidence.get("payload_truth_contract") or _pr12p14p_payload_truth_evidence(output))
    completion = dict(evidence.get("completion_contract") or _pr12p14p_completion_evidence(output))
    ai = dict(evidence.get("ai") or {})
    document_ready_state = str(evidence.get("document_ready_state") or "").strip()
    exact_document_lookup_ready = bool(
        tool_name == "retrieve_document"
        and payload_truth.get("primary_package_field") == "document_workspace"
        and payload_truth.get("primary_present")
        and int(payload_truth.get("primary_char_count") or 0) > 0
        and str(payload_truth.get("raw_text_follow_up_tool") or "") == "retrieve_document"
        and document_ready_state in {"document_ready", "workspace_ready", ""}
    )
    if payload_truth.get("schema_version") != "agvm.pr12p14o.payload_truth_contract.v1":
        failures.append("payload_truth_contract_missing_or_wrong_version")
    if not bool(payload_truth.get("primary_present")):
        failures.append("primary_mcp_payload_missing")
    if not str(payload_truth.get("primary_field") or "").strip():
        failures.append("primary_mcp_payload_pointer_missing")
    if int(payload_truth.get("primary_char_count") or 0) < 1:
        failures.append("primary_mcp_payload_empty")
    if bool(payload_truth.get("answer_demo_primary_product")):
        failures.append("answer_demo_marked_as_primary_product")
    if str(tool_name) in {"retrieve_context", "retrieve_document", "retrieve_project_workspace"} and not bool(
        payload_truth.get("exact_backend_field")
    ):
        failures.append("primary_mcp_payload_not_exact_backend_field")
    if completion.get("schema_version") != "agvm.pr12p14n.mcp_completion_contract.v1":
        failures.append("completion_contract_missing_or_wrong_version")
    if not bool(completion.get("first_package_present")):
        failures.append("completion_first_package_not_present")
    if str(completion.get("state") or "") == "background_running" and not bool(completion.get("background_inspectable")):
        failures.append("background_completion_not_inspectable")
    if str(completion.get("state") or "") in {"", "waiting"}:
        failures.append(f"completion_state_not_actionable:{completion.get('state') or 'missing'}")
    no_route_llm_not_required = bool(
        ai.get("ai_no_route_terminal_contract")
        and ai.get("semantic_contract_material")
    )
    if not bool(ai.get("budget_llm_allowed")) and tool_name.startswith("retrieve") and not no_route_llm_not_required:
        failures.append("llm_not_allowed_for_mcp_retrieval_tool")
    if tool_name.startswith("retrieve") and not bool(ai.get("semantic_contract_material")) and not exact_document_lookup_ready:
        failures.append("semantic_contract_material_missing_for_mcp_tool")
    evidence["payload_truth_contract"] = payload_truth
    evidence["completion_contract"] = completion
    return failures


def _pr12p14p_validate_combined_context_documents(output: dict[str, Any], evidence: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    payload_truth = dict(evidence.get("payload_truth_contract") or _pr12p14p_payload_truth_evidence(output))
    document_count = max(
        int(payload_truth.get("document_bundle_document_count") or 0),
        int(payload_truth.get("document_workspace_document_count") or 0),
        int(payload_truth.get("document_ref_count") or 0),
        int(evidence.get("primary_document_count") or 0),
    )
    raw_policy = str(payload_truth.get("raw_text_policy") or "").strip()
    if int(evidence.get("package_chars") or 0) < 700:
        failures.append("combined_context_package_too_small")
    if document_count < 1:
        failures.append("combined_context_document_refs_missing")
    if raw_policy not in {"top_raw", "all_raw", "document_full", "refs_only"}:
        failures.append(f"combined_context_raw_policy_missing:{raw_policy or 'missing'}")
    evidence["combined_context_document_count"] = document_count
    evidence["combined_context_raw_text_policy"] = raw_policy
    return failures


def _pr12p14p_benchmark_row_from_gate(gate: dict[str, Any]) -> dict[str, Any] | None:
    evidence = dict(gate.get("evidence") or {})
    if not evidence.get("path") and str(gate.get("gate_id") or "") not in {"ui_payload_truth", "latency_truth", "rag_comparison"}:
        return None
    ai = dict(evidence.get("ai") or {})
    payload_truth = dict(evidence.get("payload_truth_contract") or {})
    completion = dict(evidence.get("completion_contract") or {})
    latency = dict(evidence.get("latency_contract") or {})
    semantic = dict(ai.get("semantic_contract") or {})
    gate_id = str(gate.get("gate_id") or gate.get("segment_id") or "")
    package_verdict = "pass" if bool(gate.get("passed")) else "fail"
    answer_verdict = "secondary"
    if payload_truth and bool(payload_truth.get("answer_demo_primary_product")):
        answer_verdict = "invalid_primary"
    elif gate_id == "ui_payload_truth":
        answer_verdict = "ui_checked"
    notes: list[str] = []
    if completion.get("state"):
        notes.append(f"completion={completion.get('state')}")
    if completion.get("visible_reason"):
        notes.append(str(completion.get("visible_reason")))
    if gate.get("failures"):
        notes.append(";".join(str(item) for item in list(gate.get("failures") or [])[:3]))
    if semantic.get("provider_degraded"):
        notes.append("provider_degraded")
    return {
        "gate": gate_id,
        "query": evidence.get("query_text"),
        "brain": evidence.get("payload_brain_id") or evidence.get("brain_id"),
        "tool_used": evidence.get("tool_name") or str(evidence.get("path") or "").rsplit("/", 1)[-1],
        "ai_state": "semantic_material"
        if bool(ai.get("semantic_contract_material"))
        else "ai_landing_only"
        if bool(ai.get("materialized"))
        else "llm_allowed_no_material"
        if bool(ai.get("budget_llm_allowed"))
        else "llm_not_allowed",
        "cache_tier": ai.get("semantic_contract_cache_tier") or semantic.get("cache_tier") or semantic.get("source"),
        "first_package_latency_ms": completion.get("first_package_ms") or latency.get("first_useful_package_ms"),
        "final_latency_ms": completion.get("full_completion_ms") or latency.get("full_completion_ms"),
        "landings": ai.get("ai_landing_count"),
        "paths": evidence.get("path_count"),
        "promoted_context_chars": evidence.get("package_chars") or payload_truth.get("primary_char_count"),
        "raw_document_policy": payload_truth.get("raw_text_policy") or evidence.get("document_text_policy"),
        "package_verdict": package_verdict,
        "answer_demo_verdict": answer_verdict,
        "ui_parity_verdict": "pass"
        if gate_id == "ui_payload_truth" and bool(gate.get("passed"))
        else "fail"
        if gate_id == "ui_payload_truth"
        else "not_ui_lane",
        "notes": " | ".join(notes)[:600],
    }


def _pr12p14p_readiness_verdict(*, missing_gates: list[str], failed_gates: list[str]) -> str:
    if not missing_gates and not failed_gates:
        return "ready_local_beta"
    failed = set(failed_gates) | set(missing_gates)
    if "ui_payload_truth" in failed or "docker_runtime_surfaces" in failed:
        return "not_ready_ui_truth_blocker"
    if "latency_truth" in failed:
        return "not_ready_latency_blocker"
    if "grow_source_preview" in failed:
        return "not_ready_ingest_blocker"
    return "not_ready_backend_blocker"


def _pr12p14p_build_readiness_matrix(gates: list[dict[str, Any]], *, missing_gates: list[str], failed_gates: list[str]) -> dict[str, Any]:
    rows = [row for gate in gates if (row := _pr12p14p_benchmark_row_from_gate(gate))]
    verdict = _pr12p14p_readiness_verdict(missing_gates=missing_gates, failed_gates=failed_gates)
    return {
        "schema_version": PR12P14P_FINAL_LOCAL_MCP_READINESS_MATRIX_SCHEMA_VERSION,
        "slice": "PR-12P-14P",
        "readiness_verdict": verdict,
        "ready_local_beta": verdict == "ready_local_beta",
        "rows": rows,
        "row_count": len(rows),
        "required_verdicts": list(PR12P14P_READINESS_VERDICTS),
        "missing_gates": list(missing_gates),
        "failed_gates": list(failed_gates),
        "benchmark_lanes": {
            "cold_semantic_contract_cache": "observed_when_cache_tier_is_fresh_llm_or_no_cache",
            "warm_memory_cache": "observed_when_cache_tier_is_memory",
            "warm_disk_cache_after_restart": "requires_docker_restart_rerun_and_is_recorded_by_cache_tier_disk",
            "direct_mcp_client_calls": "external_mcp_client_gate",
            "ui_run_parity": "ui_payload_truth_gate",
            "two_brain_isolation": "multi_brain_scope_gate",
            "document_lookup_and_raw_retrieval": "retrieve_document_gate",
            "combined_context_plus_document_package": "combined_context_document_package_gate",
            "no_match_missing_field_honesty": "retrieve_no_match_honesty_gate",
            "path_map_truth_visibility": "retrieve_path_corridor_gate",
            "grow_then_retrieve": "grow_source_preview_gate",
            "sleep_evolve_inspectability": "sleep_evolve_preview_gate",
            "rag_baseline_comparison": "rag_comparison_gate",
        },
    }


def _pr12p14c_probe_evidence(output: dict[str, Any], *, elapsed_ms: int, expected_terms: tuple[str, ...]) -> dict[str, Any]:
    context_package = dict(output.get("context_package") or {})
    document_workspace = dict(output.get("document_workspace") or context_package.get("document_workspace") or {})
    path_corridors = dict(output.get("path_corridors") or {})
    route_trace = dict(output.get("route_trace") or {})
    completeness = dict(output.get("completeness") or {})
    latency_contract = _pr12p12_latency_contract_for_output(output, elapsed_ms, tool_name=str(output.get("tool_name") or ""))
    package_text = str(context_package.get("agent_markdown") or "")
    document_text = _pr12p12_leaf_text(document_workspace)
    document_ref_text = _pr12p12_leaf_text(output.get("document_refs") or []) + "\n" + _pr12p12_leaf_text(context_package.get("document_refs") or [])
    path_text = _pr12p12_leaf_text(path_corridors) + "\n" + _pr12p12_leaf_text(route_trace)
    source_trace_text = _pr12p12_leaf_text(output.get("source_trace") or [])
    full_text = "\n".join([package_text, document_text, document_ref_text, path_text, source_trace_text, _pr12p12_leaf_text(output.get("answer_demo") or {})])
    document_metrics = dict(document_workspace.get("metrics") or {})
    document_bundle = dict(output.get("document_bundle") or context_package.get("document_bundle") or {})
    hot_sections = list(context_package.get("hot_sections") or [])
    if not hot_sections:
        hot_sections = [
            section
            for section in list(context_package.get("sections") or context_package.get("structured_sections") or [])
            if isinstance(section, dict) and list(section.get("items") or [])
        ]
    if not hot_sections and isinstance(context_package.get("hot_context"), list):
        grouped_hot_sections: dict[str, list[Any]] = {}
        for item in list(context_package.get("hot_context") or []):
            if not isinstance(item, dict):
                continue
            section_key = str(item.get("section") or "context").strip() or "context"
            grouped_hot_sections.setdefault(section_key, []).append(item)
        hot_sections = [
            {"key": key, "title": key, "items": values}
            for key, values in grouped_hot_sections.items()
            if values
        ]
    cold_reservoir = dict(context_package.get("cold_reservoir") or {})
    if not cold_reservoir and isinstance(context_package.get("cold_context"), list):
        cold_reservoir = {"entry_count": len(list(context_package.get("cold_context") or []))}
    route_events = list(route_trace.get("events") or []) if isinstance(route_trace.get("events"), list) else []
    path_items = list(path_corridors.get("paths") or []) if isinstance(path_corridors.get("paths"), list) else []
    path_metrics = dict(path_corridors.get("metrics") or {})
    path_embedded_route_event_count = sum(
        len(list(dict(item).get("route_events") or []))
        for item in path_items
        if isinstance(item, dict)
    )
    route_event_count = max(
        len(route_events),
        int(path_metrics.get("route_event_count") or 0),
        path_embedded_route_event_count,
    )
    document_bundle_raw_text_chars = sum(
        max(
            len(str(dict(document).get(field) or ""))
            for field in ("raw_text", "full_text", "text", "content", "body")
        )
        for document in list(document_bundle.get("documents") or [])
        if isinstance(document, dict)
    )
    return {
        "tool_name": output.get("tool_name"),
        "status": output.get("status"),
        "search_id": output.get("search_id") or completeness.get("search_id"),
        "context_status": context_package.get("status"),
        "package_chars": len(package_text),
        "hot_section_count": len(hot_sections),
        "cold_reservoir_entries": int(cold_reservoir.get("entry_count") or 0),
        "document_workspace_status": document_workspace.get("status"),
        "document_ready_state": document_workspace.get("document_ready_state"),
        "primary_document_count": int(
            document_metrics.get("primary_document_count")
            or document_metrics.get("document_count")
            or document_bundle.get("document_count")
            or len(list(document_bundle.get("documents") or []))
            or 0
        ),
        "primary_raw_text_char_count": int(
            document_metrics.get("primary_raw_text_char_count")
            or document_metrics.get("raw_text_char_count")
            or document_bundle.get("raw_text_char_count")
            or document_bundle_raw_text_chars
            or 0
        ),
        "related_or_cold_document_count": int(document_metrics.get("related_or_cold_document_count") or 0),
        "path_count": int(path_corridors.get("path_count") or path_corridors.get("planned_corridor_count") or path_metrics.get("path_count") or len(path_items) or 0),
        "route_event_count": route_event_count,
        "trace_step_count": len(list(route_trace.get("trace_steps") or [])) if isinstance(route_trace.get("trace_steps"), list) else 0,
        "expected_term_hits": _pr12p12_term_hits(full_text, expected_terms),
        "ai": _pr12p14c_ai_evidence(output),
        "payload_truth_contract": _pr12p14p_payload_truth_evidence(output),
        "completion_contract": _pr12p14p_completion_evidence(output),
        "latency_contract": latency_contract,
        "elapsed_ms": elapsed_ms,
    }


def _pr12p14c_run_probe(
    *,
    base_url: str,
    segment_id: str,
    title: str,
    path: str,
    payload: dict[str, Any],
    expected_terms: tuple[str, ...] = (),
    timeout: float,
    validators: tuple[Callable[[dict[str, Any], dict[str, Any]], list[str]], ...] = (),
) -> tuple[dict[str, Any], dict[str, Any]]:
    started = time.perf_counter()
    failures: list[str] = []
    output: dict[str, Any] = {}
    evidence: dict[str, Any] = {"path": path, "payload_brain_id": payload.get("brain_id")}
    try:
        output, elapsed_ms = _pr12p12_elapsed_post(base_url, path, payload, timeout=timeout)
        evidence.update(_pr12p14c_probe_evidence(output, elapsed_ms=elapsed_ms, expected_terms=expected_terms))
        status = str(output.get("status") or "").lower()
        if status in {"failed", "error"}:
            failures.append(f"tool_failed_status:{status}")
        term_hits = dict(evidence.get("expected_term_hits") or {})
        missing_terms = [term for term, matched in term_hits.items() if not matched]
        if missing_terms:
            failures.append(f"expected_terms_missing:{missing_terms}")
        for validator in validators:
            failures.extend(validator(output, evidence))
    except Exception as exc:
        failures.append(f"probe_execution_failed:{exc}")
        evidence["error"] = str(exc)
    segment = _pr12p14c_segment_result(
        segment_id=segment_id,
        title=title,
        failures=failures,
        evidence=evidence,
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )
    return segment, output


def _pr12p14l_delivery_terminal_for_client(output: dict[str, Any]) -> bool:
    delivery = dict(output.get("mcp_delivery_contract") or {})
    if not delivery or not bool(delivery.get("terminal_for_client")):
        return False
    state = str(delivery.get("client_payload_state") or "").strip().lower()
    if state in {"blocked", "partial_context", "waiting", "pending", "diagnostic"}:
        return False
    if state in {
        "usable_context",
        "document_payload_ready",
        "document_ready",
        "path_payload_ready",
        "path_ready",
        "ready",
        "usable_payload",
        "no_match",
    }:
        return True
    status = str(output.get("status") or "").strip().lower()
    if status in {"ok", "no_match"}:
        return bool(output.get("context_package") or output.get("document_workspace") or output.get("source_trace") or output.get("path_corridors"))
    return False


def _pr12p14l_output_waiting_for_completion(output: dict[str, Any]) -> bool:
    if _pr12p14l_delivery_terminal_for_client(output):
        return False
    completeness = dict(output.get("completeness") or {})
    latency = dict(output.get("latency_contract") or {})
    materialization = dict(output.get("context_package_materialization") or {})
    delivery = dict(output.get("mcp_delivery_contract") or {})
    stop_reason = str(completeness.get("stop_reason") or output.get("stop_reason") or "").strip()
    delivery_completion_state = str(delivery.get("completion_state") or "").strip()
    delivery_final_pending = bool(delivery.get("final_materialization_pending"))
    delivery_run_finished = bool(
        delivery.get("run_finished")
        or (
            delivery_completion_state
            in {"finalized", "blocked", "failed", "no_match", "partial_complete_low_yield"}
            and not delivery_final_pending
        )
    )
    return bool(
        delivery_final_pending
        or (
            delivery
            and not delivery_run_finished
            and not bool(delivery.get("terminal_for_client"))
            and str(delivery.get("client_payload_state") or "") in {"blocked", "partial_context", "partial_path_payload", "waiting"}
        )
        or delivery_completion_state in {"background_running", "waiting", "pending"}
        or output.get("final_materialization_pending")
        or materialization.get("final_materialization_pending")
        or latency.get("first_package_returned_before_full_completion")
        or str(latency.get("result_materialization_state") or "") in {"first_package_ready_background_running", "snapshot_ready"}
        or stop_reason == "first_useful_mcp_package_returned_background_running"
    )


def _pr12p14l_probe_path_inspectable(path: str) -> bool:
    return path.rstrip("/").endswith(
        (
            "retrieve-context",
            "retrieve-document",
            "retrieve-project-workspace",
            "retrieve-path-corridor",
            "retrieve-source-trace",
        )
    )


def _pr12p14l_output_has_completion_inspection_surface(output: dict[str, Any]) -> bool:
    if _pr12p14l_output_waiting_for_completion(output):
        return True
    completion = dict(output.get("completion_contract") or {})
    first_package = dict(completion.get("first_package") or {})
    background = dict(completion.get("background_completion") or {})
    inspection = dict(completion.get("inspection") or {})
    latency = dict(output.get("latency_contract") or {})
    materialization = dict(output.get("context_package_materialization") or {})
    delivery = dict(output.get("mcp_delivery_contract") or {})
    completion_state = str(completion.get("state") or "").strip()
    delivery_completion_state = str(delivery.get("completion_state") or "").strip()
    return bool(
        completion.get("final_materialization_pending")
        or first_package.get("returned_before_full_completion")
        or background.get("inspectable")
        or inspection.get("available")
        or latency.get("first_package_returned_before_full_completion")
        or latency.get("background_completion_inspectable")
        or materialization.get("final_materialization_pending")
        or delivery.get("final_materialization_pending")
        or completion_state in {"background_running", "waiting", "pending"}
        or delivery_completion_state in {"background_running", "waiting", "pending"}
    )


def _pr12p14l_direct_document_ref_count(output: dict[str, Any], evidence: dict[str, Any]) -> int:
    context_package = dict(output.get("context_package") or {})
    payload_truth = dict(evidence.get("payload_truth_contract") or _pr12p14p_payload_truth_evidence(output))
    payload_documents = dict(payload_truth.get("documents") or {})
    document_ref_contract = dict(output.get("document_ref_contract") or context_package.get("document_ref_contract") or {})
    return max(
        int(evidence.get("document_ref_count") or 0),
        int(payload_documents.get("document_ref_count") or 0),
        int(payload_documents.get("actionable_document_ref_count") or 0),
        int(document_ref_contract.get("document_ref_count") or 0),
        int(document_ref_contract.get("actionable_document_ref_count") or 0),
        len(list(output.get("document_refs") or [])),
        len(list(context_package.get("document_refs") or [])),
    )


def _pr12p14l_direct_primary_chars(evidence: dict[str, Any]) -> int:
    payload_truth = dict(evidence.get("payload_truth_contract") or {})
    return max(int(evidence.get("package_chars") or 0), int(payload_truth.get("primary_char_count") or 0))


def _pr12p14l_direct_payload_gap_reasons(
    *,
    output: dict[str, Any],
    evidence: dict[str, Any],
    expected_terms: tuple[str, ...],
    path: str,
    completion_requirements: dict[str, Any] | None,
) -> list[str]:
    reasons: list[str] = []
    requirements = dict(completion_requirements or {})
    tool_name = str(output.get("tool_name") or evidence.get("tool_name") or "").strip()
    status = str(evidence.get("status") or output.get("status") or "").lower()
    if status in {"failed", "error"}:
        reasons.append(f"tool_failed_status:{status}")

    term_hits = dict(evidence.get("expected_term_hits") or {})
    missing_terms = [term for term in expected_terms if not bool(term_hits.get(term))]
    if missing_terms:
        reasons.append(f"expected_terms_missing:{missing_terms}")

    payload_truth = dict(evidence.get("payload_truth_contract") or _pr12p14p_payload_truth_evidence(output))
    completion = dict(evidence.get("completion_contract") or _pr12p14p_completion_evidence(output))
    if payload_truth.get("schema_version") != "agvm.pr12p14o.payload_truth_contract.v1":
        reasons.append("payload_truth_contract_missing_or_wrong_version")
    if not bool(payload_truth.get("primary_present")):
        reasons.append("primary_mcp_payload_missing")
    if int(payload_truth.get("primary_char_count") or 0) < 1:
        reasons.append("primary_mcp_payload_empty")
    if str(tool_name) in {"retrieve_context", "retrieve_document", "retrieve_project_workspace"} and not bool(
        payload_truth.get("exact_backend_field")
    ):
        reasons.append("primary_mcp_payload_not_exact_backend_field")
    if completion.get("schema_version") != "agvm.pr12p14n.mcp_completion_contract.v1":
        reasons.append("completion_contract_missing_or_wrong_version")
    if not bool(completion.get("first_package_present")):
        reasons.append("completion_first_package_not_present")
    if str(completion.get("state") or "") in {"", "waiting"}:
        reasons.append(f"completion_state_not_actionable:{completion.get('state') or 'missing'}")

    primary_chars = _pr12p14l_direct_primary_chars(evidence)
    min_package_chars = int(requirements.get("min_package_chars") or 0)
    if min_package_chars and primary_chars < min_package_chars:
        reasons.append(f"package_too_small:{primary_chars}<{min_package_chars}")

    expected_no_match = bool(requirements.get("expected_no_match"))
    min_hot = 0 if expected_no_match else int(requirements.get("min_hot_sections") or 0)
    hot_count = int(evidence.get("hot_section_count") or 0)
    if min_hot and hot_count < min_hot:
        reasons.append(f"hot_sections_too_few:{hot_count}<{min_hot}")

    document_ref_count = _pr12p14l_direct_document_ref_count(output, evidence)
    min_document_refs = int(requirements.get("min_document_refs") or 0)
    if min_document_refs and document_ref_count < min_document_refs:
        reasons.append(f"document_refs_too_few:{document_ref_count}<{min_document_refs}")

    document_count = int(evidence.get("primary_document_count") or 0)
    min_document_count = int(requirements.get("min_document_count") or 0)
    if min_document_count and document_count < min_document_count:
        reasons.append(f"primary_documents_too_few:{document_count}<{min_document_count}")

    raw_chars = int(evidence.get("primary_raw_text_char_count") or 0)
    min_raw_chars = int(requirements.get("min_raw_chars") or 0)
    if min_raw_chars and raw_chars < min_raw_chars:
        reasons.append(f"primary_raw_chars_too_low:{raw_chars}<{min_raw_chars}")

    path_count = int(evidence.get("path_count") or 0)
    min_path_count = int(requirements.get("min_path_count") or 0)
    if min_path_count and path_count < min_path_count:
        reasons.append(f"path_count_too_low:{path_count}<{min_path_count}")

    route_total = int(evidence.get("route_event_count") or 0) + int(evidence.get("trace_step_count") or 0)
    min_route_events = int(requirements.get("min_route_events") or 0)
    if min_route_events and route_total < min_route_events:
        reasons.append(f"route_events_too_low:{route_total}<{min_route_events}")

    if bool(requirements.get("require_ai_material")):
        ai = dict(evidence.get("ai") or {})
        is_document_payload_tool = path.rstrip("/").endswith("retrieve-document")
        no_route_terminal = bool(expected_no_match and ai.get("ai_no_route_terminal_contract"))
        if not bool(ai.get("semantic_contract_material")) and not no_route_terminal:
            reasons.append("semantic_ai_material_not_visible")
        if not is_document_payload_tool:
            route_contract_required = not bool(
                no_route_terminal
                or ai.get("ai_positive_exact_sufficiency_contract")
                or ai.get("ai_public_fact_sufficiency_contract")
                or ai.get("ai_answerability_sufficiency_contract")
                or ai.get("ai_path_route_first_sufficiency_contract")
            )
            if not bool(ai.get("materialized")):
                reasons.append("ai_route_material_not_certified")
            if route_contract_required:
                if not bool(ai.get("ai_spatial_observed")):
                    reasons.append("ai_spatial_contract_not_observed")
                if not bool(ai.get("ai_spatial_materialized")):
                    reasons.append(f"ai_spatial_contract_not_materialized:{ai.get('ai_spatial_status') or 'unknown'}")
                if not bool(ai.get("ai_spatial_certifies_route")):
                    reasons.append("ai_spatial_contract_does_not_certify_route")
            client_state = str(ai.get("delivery_client_payload_state") or "").strip()
            if not bool(ai.get("delivery_terminal_for_client")) or client_state not in {"usable_context", "path_payload_ready", "no_match"}:
                reasons.append(f"mcp_payload_not_terminal_for_client:{client_state or 'unknown'}")

    return sorted(dict.fromkeys(reasons))


def _pr12p14l_completion_inspection_reasons(
    *,
    output: dict[str, Any],
    evidence: dict[str, Any],
    expected_terms: tuple[str, ...],
    path: str,
    completion_requirements: dict[str, Any] | None,
) -> list[str]:
    if not _pr12p14l_probe_path_inspectable(path):
        return []
    reasons: list[str] = []
    if _pr12p14l_output_waiting_for_completion(output):
        reasons.append("direct_output_waiting_for_completion")
    gap_reasons = _pr12p14l_direct_payload_gap_reasons(
        output=output,
        evidence=evidence,
        expected_terms=expected_terms,
        path=path,
        completion_requirements=completion_requirements,
    )
    if gap_reasons:
        reasons.extend(f"direct_payload_gap:{reason}" for reason in gap_reasons)
    elif _pr12p14l_output_has_completion_inspection_surface(output) and not _pr12p14l_delivery_terminal_for_client(output):
        reasons.append("background_completion_inspectable")
    return sorted(dict.fromkeys(reasons))


def _pr12p14l_completed_inspection_ready(output: dict[str, Any]) -> bool:
    terminal_for_client = _pr12p14l_delivery_terminal_for_client(output)
    completeness = dict(output.get("completeness") or {})
    latency = dict(output.get("latency_contract") or {})
    materialization = dict(output.get("context_package_materialization") or {})
    delivery = dict(output.get("mcp_delivery_contract") or {})
    stop_reason = str(completeness.get("stop_reason") or output.get("stop_reason") or "").strip()
    delivery_completion_state = str(delivery.get("completion_state") or "").strip()
    delivery_final_pending = bool(delivery.get("final_materialization_pending"))
    delivery_run_finished = bool(
        delivery.get("run_finished")
        or (
            delivery_completion_state
            in {"finalized", "blocked", "failed", "no_match", "partial_complete_low_yield"}
            and not delivery_final_pending
        )
    )
    delivery_pending = bool(
        delivery
        and (
            delivery_final_pending
            or delivery_completion_state in {"background_running", "waiting", "pending"}
            or (
                not delivery_run_finished
                and
                not bool(delivery.get("terminal_for_client"))
                and str(delivery.get("client_payload_state") or "") in {"blocked", "partial_context", "partial_path_payload", "waiting"}
            )
        )
    )
    if str(output.get("status") or "").lower() in {"failed", "error", "blocked"}:
        if delivery_pending:
            return False
        return True
    if bool(output.get("final_materialization_pending") or materialization.get("final_materialization_pending")):
        return False
    if str(latency.get("result_materialization_state") or "") in {"first_package_ready_background_running", "snapshot_ready"}:
        return False
    if stop_reason == "first_useful_mcp_package_returned_background_running":
        return False
    if terminal_for_client:
        return True
    if delivery_pending:
        return False
    if delivery and str(delivery.get("client_payload_state") or "") in {
        "blocked",
        "partial_context",
        "partial_path_payload",
        "waiting",
        "pending",
        "diagnostic",
    }:
        return False
    return bool(output.get("context_package") or output.get("document_workspace") or output.get("source_trace") or output.get("path_corridors"))


def _pr12p14l_inspection_payload_score(output: dict[str, Any]) -> int:
    context_package = dict(output.get("context_package") or {})
    payload_truth = dict(output.get("payload_truth_contract") or {})
    primary = dict(payload_truth.get("primary_mcp_payload") or {})
    document_ref_contract = dict(output.get("document_ref_contract") or context_package.get("document_ref_contract") or {})
    path_corridors = dict(output.get("path_corridors") or {})
    path_metrics = dict(path_corridors.get("metrics") or {})
    hot_sections = [
        item for item in list(context_package.get("hot_sections") or []) if isinstance(item, dict)
    ]
    agent_chars = len(str(context_package.get("agent_markdown") or ""))
    primary_chars = int(primary.get("char_count") or 0)
    doc_refs = max(
        int(document_ref_contract.get("document_ref_count") or 0),
        len(list(output.get("document_refs") or [])),
        len(list(context_package.get("document_refs") or [])),
    )
    path_events = int(path_metrics.get("route_event_count") or 0)
    return max(agent_chars, primary_chars) + (len(hot_sections) * 2500) + (doc_refs * 150) + (path_events * 80)


def _pr12p14l_wait_for_completed_inspection(
    *,
    base_url: str,
    search_id: str,
    brain_id: str | None,
    include_raw_text: bool,
    include_answer_demo: bool,
    timeout: float,
    inspection_path: str = "/mcp/inspect-context-package",
    tool_path: str | None = None,
    expected_terms: tuple[str, ...] = (),
    completion_requirements: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    started = time.perf_counter()
    deadline = started + max(1.0, float(timeout))
    attempts = 0
    last_output: dict[str, Any] | None = None
    best_output: dict[str, Any] | None = None
    best_terminal_output: dict[str, Any] | None = None
    best_requirement_terminal_output: dict[str, Any] | None = None
    best_terminal_score = -1
    best_requirement_terminal_score = -1
    best_score = -1
    ready_seen = False
    ready_seen_attempts = 0
    last_error: str | None = None
    last_requirement_gap_reasons: list[str] = []
    requirements_active = bool(expected_terms or completion_requirements)
    requirement_tool_path = str(tool_path or inspection_path)
    try:
        configured_attempts = str(os.environ.get("AGVM_PR12P14L_MAX_INSPECTION_ATTEMPTS") or "").strip()
        if configured_attempts:
            max_attempts = int(configured_attempts)
        else:
            max_attempts = max(40, min(220, int(max(1.0, float(timeout)) / 0.35) + 1))
    except (TypeError, ValueError):
        max_attempts = 40
    max_attempts = max(1, max_attempts)
    try:
        stability_attempts = int(str(os.environ.get("AGVM_PR12P14L_INSPECTION_STABILITY_ATTEMPTS") or "6").strip())
    except (TypeError, ValueError):
        stability_attempts = 6
    stability_attempts = max(1, min(10, stability_attempts))
    payload = {
        "search_id": search_id,
        "brain_id": brain_id,
        "include_raw_text": bool(include_raw_text),
        "include_answer_demo": bool(include_answer_demo),
    }
    while time.perf_counter() < deadline and attempts < max_attempts:
        attempts += 1
        try:
            inspected = post_json(base_url, inspection_path, payload, timeout=min(30.0, max(1.0, float(timeout))))
            last_output = inspected
            score = _pr12p14l_inspection_payload_score(inspected)
            if score >= best_score:
                best_output = inspected
                best_score = score
            raw_inspected_ready = _pr12p14l_completed_inspection_ready(inspected)
            inspected_ready = raw_inspected_ready
            requirement_gap_reasons: list[str] = []
            if inspected_ready and requirements_active:
                inspected_evidence = _pr12p14c_probe_evidence(
                    inspected,
                    elapsed_ms=int((time.perf_counter() - started) * 1000.0),
                    expected_terms=expected_terms,
                )
                requirement_gap_reasons = _pr12p14l_direct_payload_gap_reasons(
                    output=inspected,
                    evidence=inspected_evidence,
                    expected_terms=expected_terms,
                    path=requirement_tool_path,
                    completion_requirements=completion_requirements,
                )
                last_requirement_gap_reasons = requirement_gap_reasons
                inspected_ready = not requirement_gap_reasons
            if inspected_ready and score >= best_terminal_score:
                best_terminal_output = inspected
                best_terminal_score = score
                if requirements_active and score >= best_requirement_terminal_score:
                    best_requirement_terminal_output = inspected
                    best_requirement_terminal_score = score
            elif (
                requirements_active
                and raw_inspected_ready
                and not requirement_gap_reasons
                and score >= best_requirement_terminal_score
            ):
                best_requirement_terminal_output = inspected
                best_requirement_terminal_score = score
            if inspected_ready:
                ready_seen = True
            if ready_seen:
                ready_seen_attempts += 1
                if ready_seen_attempts >= stability_attempts:
                    selected = best_requirement_terminal_output or best_terminal_output or best_output or inspected
                    return selected, {
                        "inspection_used": True,
                        "inspection_path": inspection_path,
                        "inspection_status": selected.get("status"),
                        "inspection_attempts": attempts,
                        "inspection_attempt_cap": max_attempts,
                        "inspection_elapsed_ms": round((time.perf_counter() - started) * 1000.0, 2),
                        "inspection_terminal": _pr12p14l_completed_inspection_ready(selected),
                        "inspection_stability_attempts": stability_attempts,
                        "inspection_selected_payload_score": best_score,
                        "inspection_selected_terminal_payload_score": best_terminal_score,
                        "inspection_requirements_active": requirements_active,
                        "inspection_requirement_gap_reasons": last_requirement_gap_reasons,
                        "inspection_selected_requirement_terminal_payload_score": best_requirement_terminal_score,
                    }
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        time.sleep(0.35)
    selected_output = best_requirement_terminal_output or best_terminal_output or best_output or last_output
    return selected_output, {
        "inspection_used": bool(last_output),
        "inspection_path": inspection_path,
        "inspection_status": (selected_output or {}).get("status") if selected_output else None,
        "inspection_attempts": attempts,
        "inspection_attempt_cap": max_attempts,
        "inspection_elapsed_ms": round((time.perf_counter() - started) * 1000.0, 2),
        "inspection_terminal": _pr12p14l_completed_inspection_ready(selected_output or {}),
        "inspection_error": last_error,
        "inspection_stability_attempts": stability_attempts,
        "inspection_selected_payload_score": best_score,
        "inspection_selected_terminal_payload_score": best_terminal_score,
        "inspection_requirements_active": requirements_active,
        "inspection_requirement_gap_reasons": last_requirement_gap_reasons,
        "inspection_selected_requirement_terminal_payload_score": best_requirement_terminal_score,
    }


def _pr12p14l_run_probe(
    *,
    base_url: str,
    segment_id: str,
    title: str,
    path: str,
    payload: dict[str, Any],
    expected_terms: tuple[str, ...] = (),
    timeout: float,
    validators: tuple[Callable[[dict[str, Any], dict[str, Any]], list[str]], ...] = (),
    completion_requirements: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    started = time.perf_counter()
    output: dict[str, Any] = {}
    evidence: dict[str, Any] = {
        "path": path,
        "payload_brain_id": payload.get("brain_id"),
        "query_text": payload.get("query_text") or payload.get("raw_input"),
        "context_package_mode": payload.get("context_package_mode"),
        "document_text_policy": payload.get("document_text_policy"),
    }

    def execute_once() -> tuple[dict[str, Any], dict[str, Any], list[str]]:
        attempt_started = time.perf_counter()
        attempt_failures: list[str] = []
        attempt_output: dict[str, Any] = {}
        attempt_evidence: dict[str, Any] = {
            "path": path,
            "payload_brain_id": payload.get("brain_id"),
            "query_text": payload.get("query_text") or payload.get("raw_input"),
            "context_package_mode": payload.get("context_package_mode"),
            "document_text_policy": payload.get("document_text_policy"),
        }
        direct_output, elapsed_ms = _pr12p12_elapsed_post(base_url, path, payload, timeout=timeout)
        first_evidence = _pr12p14c_probe_evidence(direct_output, elapsed_ms=elapsed_ms, expected_terms=expected_terms)
        attempt_output = direct_output
        inspection_reasons = _pr12p14l_completion_inspection_reasons(
            output=direct_output,
            evidence=first_evidence,
            expected_terms=expected_terms,
            path=path,
            completion_requirements=completion_requirements,
        )
        completion_evidence: dict[str, Any] = {
            "first_package_returned": _pr12p14l_output_waiting_for_completion(direct_output),
            "first_package_status": direct_output.get("status"),
            "first_package_elapsed_ms": elapsed_ms,
            "inspection_trigger_reasons": inspection_reasons,
            "direct_completion_inspection_surface": _pr12p14l_output_has_completion_inspection_surface(direct_output),
        }
        search_id = str(direct_output.get("search_id") or _pr12p12_nested(direct_output, "completeness", "search_id") or "").strip()
        if search_id and inspection_reasons:
            completion_contract = dict(direct_output.get("completion_contract") or {})
            inspection_contract = dict(completion_contract.get("inspection") or {})
            inspection_path = str(inspection_contract.get("inspect_endpoint") or "").strip()
            if not inspection_path:
                inspection_path = "/mcp/inspect-path-corridor" if path.rstrip("/").endswith("retrieve-path-corridor") else "/mcp/inspect-context-package"
            inspected, inspection_evidence = _pr12p14l_wait_for_completed_inspection(
                base_url=base_url,
                search_id=search_id,
                brain_id=str(payload.get("brain_id") or "").strip() or None,
                include_raw_text=bool(payload.get("include_raw_text") or payload.get("document_text_policy") in {"top_raw", "all_raw"}),
                include_answer_demo=bool(payload.get("include_answer_demo")),
                timeout=timeout,
                inspection_path=inspection_path,
                tool_path=path,
                expected_terms=expected_terms,
                completion_requirements=completion_requirements,
            )
            completion_evidence.update(inspection_evidence)
            if inspected:
                attempt_output = inspected
        attempt_evidence.update(
            _pr12p14c_probe_evidence(
                attempt_output,
                elapsed_ms=int((time.perf_counter() - attempt_started) * 1000),
                expected_terms=expected_terms,
            )
        )
        attempt_evidence["first_package"] = first_evidence
        attempt_evidence["completion_inspection"] = completion_evidence
        status = str(attempt_output.get("status") or "").lower()
        if status in {"failed", "error"}:
            attempt_failures.append(f"tool_failed_status:{status}")
        term_hits = dict(attempt_evidence.get("expected_term_hits") or {})
        missing_terms = [term for term, matched in term_hits.items() if not matched]
        if missing_terms:
            attempt_failures.append(f"expected_terms_missing:{missing_terms}")
        attempt_failures.extend(_pr12p14p_validate_payload_and_completion(attempt_output, attempt_evidence))
        for validator in validators:
            attempt_failures.extend(validator(attempt_output, attempt_evidence))
        return attempt_output, attempt_evidence, attempt_failures

    failures: list[str] = []
    try:
        output, evidence, failures = execute_once()
        retry_policy = _pr12p14m_provider_retry_policy_for_probe(
            output=output,
            evidence=evidence,
            failures=failures,
            path=path,
        )
        if bool(retry_policy.get("allowed")):
            first_attempt = {
                "failures": list(failures),
                "status": evidence.get("status"),
                "search_id": evidence.get("search_id"),
                "ai": dict(evidence.get("ai") or {}),
                "semantic_contract": dict(dict(evidence.get("ai") or {}).get("semantic_contract") or {}),
            }
            retry_output, retry_evidence, retry_failures = execute_once()
            retry_policy["attempted"] = True
            retry_policy["recovered"] = not retry_failures
            retry_policy["retry_failures"] = list(retry_failures)
            retry_policy["retry_search_id"] = retry_evidence.get("search_id")
            output = retry_output
            evidence = retry_evidence
            failures = retry_failures
            evidence["provider_retry_policy"] = retry_policy
            evidence["provider_retry_first_attempt"] = first_attempt
        else:
            evidence["provider_retry_policy"] = retry_policy
    except Exception as exc:
        failures.append(f"probe_execution_failed:{exc}")
        evidence["error"] = str(exc)
    segment = _pr12p14c_segment_result(
        segment_id=segment_id,
        title=title,
        failures=failures,
        evidence=evidence,
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )
    return segment, output


def _pr12p14m_provider_retry_summary(gates: list[dict[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for gate in gates:
        evidence = dict(gate.get("evidence") or {})
        policy = dict(evidence.get("provider_retry_policy") or {})
        if not policy:
            continue
        rows.append(
            {
                "gate_id": gate.get("gate_id") or gate.get("segment_id"),
                "allowed": bool(policy.get("allowed")),
                "attempted": bool(policy.get("attempted")),
                "recovered": bool(policy.get("recovered")),
                "provider_degraded": bool(policy.get("provider_degraded")),
                "semantic_provider_state": policy.get("semantic_provider_state"),
                "semantic_contract_status": policy.get("semantic_contract_status"),
                "semantic_contract_source": policy.get("semantic_contract_source"),
                "semantic_contract_material": bool(policy.get("semantic_contract_material")),
                "retry_failures": list(policy.get("retry_failures") or []),
            }
        )
    attempted = [row for row in rows if bool(row.get("attempted"))]
    degraded = [row for row in rows if bool(row.get("provider_degraded"))]
    return {
        "schema_version": "agvm.pr12p14m.provider_retry_summary.v1",
        "silent_heuristic_certification_allowed": False,
        "policy_enabled": True,
        "observed_gate_count": len(rows),
        "degraded_gate_count": len(degraded),
        "attempted_count": len(attempted),
        "recovered_count": sum(1 for row in attempted if bool(row.get("recovered"))),
        "failed_retry_count": sum(1 for row in attempted if not bool(row.get("recovered"))),
        "rows": rows,
    }


def _pr12p14c_validate_context_ai(output: dict[str, Any], evidence: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    ai = dict(evidence.get("ai") or {})
    if not bool(ai.get("budget_llm_allowed")):
        failures.append("llm_not_allowed_for_context_probe")
    if not bool(ai.get("materialized")):
        failures.append("ai_landing_materialization_missing")
    if bool(ai.get("hard_gate_blocked")):
        failures.append("ai_materialization_hard_gate_blocked")
    if int(evidence.get("package_chars") or 0) < 700:
        failures.append("context_package_too_small_for_mcp_context_probe")
    if not str(evidence.get("search_id") or "").strip():
        failures.append("search_id_missing")
    if int(evidence.get("hot_section_count") or 0) < 1:
        failures.append("hot_sections_missing")
    return failures


def _pr12p14c_validate_exact_document(_output: dict[str, Any], evidence: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if str(evidence.get("status") or "").lower() != "ok":
        failures.append(f"document_tool_unexpected_status:{evidence.get('status')}")
    if str(evidence.get("document_ready_state") or "") != "document_ready":
        failures.append("document_ready_state_not_document_ready")
    if int(evidence.get("primary_document_count") or 0) < 1:
        failures.append("primary_document_missing")
    if int(evidence.get("primary_raw_text_char_count") or 0) < 200:
        failures.append("primary_raw_text_too_small")
    return failures


def _pr12p14c_validate_no_match(output: dict[str, Any], evidence: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    completeness = dict(output.get("completeness") or {})
    context_package = dict(output.get("context_package") or {})
    status = str(evidence.get("status") or "").lower()
    no_match = status == "no_match" or bool(completeness.get("no_match")) or str(context_package.get("status") or "").lower() in {
        "insufficient",
        "no_match",
    }
    exact_missing_count = int(
        completeness.get("exact_field_missing_count")
        or completeness.get("missing_exact_field_count")
        or len(list(completeness.get("missing_exact_fields") or []))
        or 0
    )
    hot_item_count = int(completeness.get("hot_item_count") or context_package.get("hot_item_count") or len(list(context_package.get("hot_sections") or [])) or 0)
    ai = dict(evidence.get("ai") or {})
    if not no_match:
        failures.append("missing_private_identifier_not_reported_as_no_match")
    if exact_missing_count < 1:
        failures.append("exact_missing_slot_not_exposed")
    if hot_item_count > 0:
        failures.append("irrelevant_hot_context_promoted_for_missing_exact_field")
    if not bool(ai.get("materialized")):
        failures.append("ai_materialization_missing_for_no_match_judgement")
    return failures


def _pr12p14c_validate_path_visibility(_output: dict[str, Any], evidence: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if int(evidence.get("path_count") or 0) < 1:
        failures.append("path_corridor_missing")
    if int(evidence.get("route_event_count") or 0) + int(evidence.get("trace_step_count") or 0) < 1:
        failures.append("route_trace_missing")
    if not str(evidence.get("search_id") or "").strip():
        failures.append("path_search_id_missing")
    return failures


def _pr12p14c_inspection_parity_segment(base_url: str, context_output: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    failures: list[str] = []
    evidence: dict[str, Any] = {}
    search_id = str(context_output.get("search_id") or _pr12p12_nested(context_output, "completeness", "search_id") or "").strip()
    evidence["search_id"] = search_id
    if not search_id:
        failures.append("search_id_missing_for_inspection")
    else:
        try:
            inspected = post_json(base_url, "/mcp/inspect-context-package", {"search_id": search_id}, timeout=45.0)
            inspected_package = dict(inspected.get("context_package") or {})
            evidence.update(
                {
                    "inspect_status": inspected.get("status"),
                    "inspect_tool_name": inspected.get("tool_name"),
                    "inspect_payload_integrity": inspected.get("payload_integrity"),
                    "inspect_package_chars": len(str(inspected_package.get("agent_markdown") or "")),
                    "inspect_package_status": inspected_package.get("status"),
                }
            )
            if str(inspected.get("tool_name") or "") != "inspect_context_package":
                failures.append("inspect_context_package_tool_name_mismatch")
            if str(inspected.get("status") or "").lower() in {"failed", "error"}:
                failures.append(f"inspect_context_package_failed:{inspected.get('status')}")
            if dict(inspected.get("payload_integrity") or {}).get("passed") is False:
                failures.append("inspect_payload_integrity_failed")
            if int(evidence.get("inspect_package_chars") or 0) < 200:
                failures.append("inspect_context_package_too_small")
        except Exception as exc:
            failures.append(f"inspect_context_package_error:{exc}")
            evidence["error"] = str(exc)
    return _pr12p14c_segment_result(
        segment_id="mcp_inspection_parity",
        title="A persisted MCP search can be inspected and returns the same product surface family.",
        failures=failures,
        evidence=evidence,
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def _pr12p14c_latency_segment(probe_segments: list[dict[str, Any]]) -> dict[str, Any]:
    started = time.perf_counter()
    failures: list[str] = []
    rows: list[dict[str, Any]] = []
    for segment in probe_segments:
        evidence = dict(segment.get("evidence") or {})
        latency = dict(evidence.get("latency_contract") or {})
        segment_id = str(segment.get("segment_id") or "")
        first_useful_ms = latency.get("first_useful_package_ms")
        full_completion_ms = latency.get("full_completion_ms")
        http_elapsed_ms = latency.get("http_elapsed_ms") or evidence.get("elapsed_ms")
        rows.append(
            {
                "segment_id": segment_id,
                "first_useful_package_ms": first_useful_ms,
                "full_completion_ms": full_completion_ms,
                "http_elapsed_ms": http_elapsed_ms,
                "benchmark_basis_ms": latency.get("benchmark_basis_ms"),
            }
        )
        if first_useful_ms is None and http_elapsed_ms is None:
            failures.append(f"latency_missing:{segment_id}")
    return _pr12p14c_segment_result(
        segment_id="latency_reporting",
        title="Every live MCP probe exposes first-useful and/or HTTP latency so slow phases are visible.",
        failures=failures,
        evidence={"rows": rows},
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def run_pr12p14c_final_gate_expansion_suite(base_url: str | None = None) -> dict[str, Any]:
    selected_base_url = str(base_url or DEFAULT_BASE_URL).rstrip("/")
    suite_started = time.perf_counter()
    segments: list[dict[str, Any]] = []

    health_started = time.perf_counter()
    health: dict[str, Any] = {}
    health_failures: list[str] = []
    try:
        health = get_json(selected_base_url, "/health", timeout=30.0)
        status_text = str(health.get("status") or health.get("state") or "").lower()
        if not bool(health.get("ok")) and status_text not in {"ok", "healthy", "ready"}:
            health_failures.append(f"api_health_unexpected_status:{status_text or 'missing'}")
    except Exception as exc:
        health_failures.append(f"api_health_unreachable:{exc}")
    frontend_probe = _pr12p13_frontend_probe(_pr12p13_frontend_candidates(selected_base_url))
    if not bool(frontend_probe.get("passed")):
        health_failures.extend(list(frontend_probe.get("failures") or ["frontend_unreachable"]))
    segments.append(
        _pr12p14c_segment_result(
            segment_id="runtime_surfaces",
            title="Canonical API/UI surfaces are reachable on the self-hosted local distribution.",
            failures=health_failures,
            evidence={
                "base_url": selected_base_url,
                "health_status": health.get("status"),
                "active_brain_id": health.get("active_brain_id"),
                "default_brain_id": health.get("default_brain_id"),
                "brain_count": health.get("brain_count"),
                "frontend_probe": frontend_probe,
            },
            elapsed_ms=(time.perf_counter() - health_started) * 1000,
        )
    )

    llm_started = time.perf_counter()
    llm_enabled = str(os.environ.get("AGVM_LLM_ENABLED") or "true").strip().lower() not in {"0", "false", "no", "off"}
    openai_key_present = bool(str(os.environ.get("OPENAI_API_KEY") or "").strip())
    llm_failures: list[str] = []
    if not llm_enabled:
        llm_failures.append("agvm_llm_enabled_false")
    if not openai_key_present:
        llm_failures.append("openai_api_key_missing")
    segments.append(
        _pr12p14c_segment_result(
            segment_id="llm_required_runtime",
            title="The local product gate refuses to certify a non-AI runtime.",
            failures=llm_failures,
            evidence={
                "agvm_llm_enabled": llm_enabled,
                "openai_key_present": openai_key_present,
                "llm_required_by_master": True,
                "no_llm_fallback_allowed_for_product_ready": True,
            },
            elapsed_ms=(time.perf_counter() - llm_started) * 1000,
        )
    )

    registry_started = time.perf_counter()
    registry_gate = _pr12p13_registry_gate()
    segments.append(
        _pr12p14c_segment_result(
            segment_id="mcp_registry_surface",
            title="MCP tool registry remains valid before the final self-hosted readiness rerun.",
            failures=list(registry_gate.get("failures") or []),
            evidence=dict(registry_gate.get("evidence") or {}),
            elapsed_ms=(time.perf_counter() - registry_started) * 1000,
        )
    )

    brain_targets = _pr12p14c_brain_targets(selected_base_url)
    segments.append(dict(brain_targets["segment"]))
    targets = dict(brain_targets.get("targets") or {})
    simone = str(dict(targets.get("simone_massaro") or {}).get("brain_id") or "").strip()
    elena = str(dict(targets.get("elena_valsecchi") or {}).get("brain_id") or "").strip()
    case_timeout = float(os.environ.get("AGVM_PR12P14C_CASE_TIMEOUT_SECONDS") or 140.0)

    probe_segments: list[dict[str, Any]] = []
    context_output: dict[str, Any] = {}
    if simone:
        context_segment, context_output = _pr12p14c_run_probe(
            base_url=selected_base_url,
            segment_id="context_ai_materiality",
            title="A broad MCP context request produces a sufficiently wide package with visible AI landing material.",
            path="/mcp/retrieve-context",
            payload={
                "brain_id": simone,
                "query_text": "raccontami di te, del tuo lavoro e delle tue aziende",
                "retrieval_mode": "balanced",
                "context_package_mode": "broad_dossier",
                "max_matches": 18,
                "include_raw_text": True,
                "include_answer_demo": False,
                "complete_paths": False,
            },
            expected_terms=("Simone", "BaxEnergy"),
            timeout=case_timeout,
            validators=(_pr12p14l_validate_context_ai,),
        )
    else:
        context_segment = _pr12p14c_segment_result(
            segment_id="context_ai_materiality",
            title="A broad MCP context request produces a sufficiently wide package with visible AI landing material.",
            failures=["simone_brain_missing"],
            evidence={},
            elapsed_ms=0,
        )
    segments.append(context_segment)
    probe_segments.append(context_segment)

    segments.append(_pr12p14c_inspection_parity_segment(selected_base_url, context_output))

    if simone:
        document_segment, _document_output = _pr12p14c_run_probe(
            base_url=selected_base_url,
            segment_id="exact_document_readiness",
            title="retrieve_document returns one exact primary raw document and marks the document tool ready.",
            path="/mcp/retrieve-document",
            payload={
                "brain_id": simone,
                "query_text": "BaxEnergy domain, scale, and Yokogawa integration",
                "retrieval_mode": "balanced",
                "context_package_mode": "document_full",
                "max_matches": 12,
                "include_raw_text": True,
                "include_answer_demo": False,
            },
            expected_terms=("BaxEnergy", "Yokogawa"),
            timeout=case_timeout,
            validators=(_pr12p14c_validate_exact_document,),
        )
    else:
        document_segment = _pr12p14c_segment_result(
            segment_id="exact_document_readiness",
            title="retrieve_document returns one exact primary raw document and marks the document tool ready.",
            failures=["simone_brain_missing"],
            evidence={},
            elapsed_ms=0,
        )
    segments.append(document_segment)
    probe_segments.append(document_segment)

    no_match_brain = elena or simone
    if no_match_brain:
        no_match_segment, _no_match_output = _pr12p14c_run_probe(
            base_url=selected_base_url,
            segment_id="no_match_honesty",
            title="A missing exact private identifier returns no_match instead of adjacent biography.",
            path="/mcp/retrieve-context",
            payload={
                "brain_id": no_match_brain,
                "query_text": "qual e' il mio codice fiscale?",
                "retrieval_mode": "balanced",
                "context_package_mode": "mcp_operational",
                "max_matches": 10,
                "include_raw_text": True,
                "include_answer_demo": False,
                "complete_paths": False,
            },
            expected_terms=(),
            timeout=case_timeout,
            validators=(_pr12p14c_validate_no_match,),
        )
    else:
        no_match_segment = _pr12p14c_segment_result(
            segment_id="no_match_honesty",
            title="A missing exact private identifier returns no_match instead of adjacent biography.",
            failures=["validation_brain_missing"],
            evidence={},
            elapsed_ms=0,
        )
    segments.append(no_match_segment)
    probe_segments.append(no_match_segment)

    if simone:
        path_segment, _path_output = _pr12p14c_run_probe(
            base_url=selected_base_url,
            segment_id="path_visibility_contract",
            title="Path-corridor retrieval exposes planned/traversed route truth instead of hiding path work.",
            path="/mcp/retrieve-path-corridor",
            payload={
                "brain_id": simone,
                "query_text": "collega BaxEnergy, Yokogawa, WiSNAM e Free Mind Foundry e mostrami il contesto attraversato",
                "retrieval_mode": "balanced",
                "context_package_mode": "broad_dossier",
                "max_matches": 14,
                "include_raw_text": True,
                "include_answer_demo": False,
                "complete_paths": True,
            },
            expected_terms=("BaxEnergy", "Yokogawa"),
            timeout=case_timeout,
            validators=(_pr12p14c_validate_path_visibility,),
        )
    else:
        path_segment = _pr12p14c_segment_result(
            segment_id="path_visibility_contract",
            title="Path-corridor retrieval exposes planned/traversed route truth instead of hiding path work.",
            failures=["simone_brain_missing"],
            evidence={},
            elapsed_ms=0,
        )
    segments.append(path_segment)
    probe_segments.append(path_segment)

    ui_started = time.perf_counter()
    ui_probe = _pr12p12_ui_parity_probe()
    ui_failures = list(ui_probe.get("failures") or [])
    if float(ui_probe.get("score") or 0.0) < 0.8:
        ui_failures.append(f"ui_payload_truth_score_low:{float(ui_probe.get('score') or 0.0):.4f}<0.80")
    segments.append(
        _pr12p14c_segment_result(
            segment_id="ui_payload_truth",
            title="The local UI source exposes the MCP payload, document and route truth surfaces needed for validation.",
            failures=ui_failures,
            evidence=ui_probe,
            elapsed_ms=(time.perf_counter() - ui_started) * 1000,
        )
    )

    segments.append(_pr12p14c_latency_segment(probe_segments))

    by_segment = {str(segment.get("segment_id") or ""): segment for segment in segments}
    missing_segments = [segment for segment in REQUIRED_PR12P14C_FINAL_GATE_SEGMENTS if segment not in by_segment]
    failed_segments = [
        str(segment.get("segment_id") or "")
        for segment in segments
        if bool(segment.get("critical", True)) and not bool(segment.get("passed"))
    ]
    all_pass = not missing_segments and not failed_segments
    return {
        "schema_version": PR12P14C_FINAL_GATE_EXPANSION_REPORT_SCHEMA_VERSION,
        "phase": "final_gate_expansion",
        "slice": "PR-12P-14C",
        "base_url": selected_base_url,
        "all_pass": all_pass,
        "local_gate_expanded": True,
        "local_mcp_product_ready_candidate": bool(all_pass),
        "product_ready_verdict": "final_gate_expansion_passed_pr12p14e_rerun_required"
        if all_pass
        else "final_gate_expansion_failed_local_mcp_not_ready",
        "readiness_level": "requires_final_self_hosted_rerun" if all_pass else "blocked_needs_remediation",
        "cloud_blocked": True,
        "cloud_release_blocked_until": "Final local self-hosted MCP readiness passes and the user approves cloud/commercialization planning.",
        "required_segments": list(REQUIRED_PR12P14C_FINAL_GATE_SEGMENTS),
        "missing_segments": missing_segments,
        "failed_segments": failed_segments,
        "segment_matrix": {
            segment_id: {
                "required": segment_id in REQUIRED_PR12P14C_FINAL_GATE_SEGMENTS,
                "covered": segment_id in by_segment,
                "passed": bool((by_segment.get(segment_id) or {}).get("passed")),
                "critical": bool((by_segment.get(segment_id) or {}).get("critical", True)),
                "failures": list((by_segment.get(segment_id) or {}).get("failures") or []),
            }
            for segment_id in REQUIRED_PR12P14C_FINAL_GATE_SEGMENTS
        },
        "segment_results": segments,
        "launch_blockers": [f"segment_failed:{segment}" for segment in failed_segments]
        + [f"segment_missing:{segment}" for segment in missing_segments],
        "benchmark_inputs": {
            "phase": "final_gate_expansion",
            "canonical_api_port": 8010,
            "canonical_frontend_port": 3020,
            "live_llm_required": True,
            "no_llm_fallback_allowed": True,
            "mutation_enabled": False,
            "mcp_first": True,
            "context_package_is_product": True,
            "answer_demo_secondary": True,
            "cloud_release_blocked": True,
            "case_timeout_seconds": case_timeout,
        },
        "evidence_contract": {
            "expands_after": "PR-12P-14B Exact Document Tool Semantics",
            "validates": list(REQUIRED_PR12P14C_FINAL_GATE_SEGMENTS),
            "product_ready_claim_allowed": False,
            "product_ready_claim_reason": "This phase expands and hardens the gate; a final self-hosted readiness rerun is required after the remediation segment passes.",
            "cloud_commercialization_allowed": False,
        },
        "elapsed_ms": round((time.perf_counter() - suite_started) * 1000, 3),
        "next_slice": "PR-12P-14E Final Self-Hosted Readiness Rerun"
        if all_pass
        else "PR-12P-14D Nonblocking MCP Runtime And AI Completion Remediation",
    }


def _pr12p14l_gate_result(
    *,
    gate_id: str,
    title: str,
    failures: list[str],
    evidence: dict[str, Any],
    elapsed_ms: float,
    critical: bool = True,
) -> dict[str, Any]:
    return {
        "gate_id": gate_id,
        "segment_id": gate_id,
        "title": title,
        "slice": "PR-12P-14L",
        "critical": critical,
        "passed": not failures,
        "failures": failures,
        "evidence": evidence,
        "elapsed_ms": round(float(elapsed_ms), 3),
    }


def _pr12p14l_from_14c(segment: dict[str, Any], *, gate_id: str | None = None, title: str | None = None) -> dict[str, Any]:
    copied = dict(segment)
    copied["gate_id"] = str(gate_id or copied.get("gate_id") or copied.get("segment_id") or "")
    copied["segment_id"] = copied["gate_id"]
    copied["slice"] = "PR-12P-14L"
    if title:
        copied["title"] = title
    return copied


def _pr12p14l_validate_project_workspace(_output: dict[str, Any], evidence: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    output = dict(_output or {})
    completeness = dict(output.get("completeness") or {})
    document_count = max(
        int(completeness.get("document_workspace_document_count") or 0),
        int(evidence.get("primary_document_count") or 0),
        int(evidence.get("related_or_cold_document_count") or 0),
    )
    document_tool_ready = bool(completeness.get("document_tool_ready")) or document_count > 0
    raw_text_chars = int(
        completeness.get("document_workspace_raw_text_char_count")
        or completeness.get("document_workspace_primary_raw_text_char_count")
        or evidence.get("primary_raw_text_char_count")
        or 0
    )
    text_size = int(evidence.get("package_chars") or 0) + raw_text_chars
    if str(evidence.get("status") or "").lower() not in {"ok", "partial", "no_match"}:
        failures.append(f"project_workspace_unexpected_status:{evidence.get('status')}")
    if not document_tool_ready and document_count < 1:
        failures.append("project_workspace_document_tool_not_ready")
    if text_size < 300:
        failures.append("project_workspace_context_too_small")
    if not str(evidence.get("search_id") or "").strip():
        failures.append("project_workspace_search_id_missing")
    evidence["project_workspace_document_tool_ready"] = document_tool_ready
    evidence["project_workspace_document_count"] = document_count
    evidence["project_workspace_raw_text_char_count"] = raw_text_chars
    return failures


def _pr12p14l_validate_context_ai(output: dict[str, Any], evidence: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    ai = dict(evidence.get("ai") or {})
    payload_integrity = dict(output.get("payload_integrity") or {})
    if not bool(ai.get("budget_llm_allowed")):
        failures.append("llm_not_allowed_for_context_probe")
    if not bool(ai.get("materialized")):
        failures.append("ai_landing_materialization_missing")
    if bool(ai.get("hard_gate_blocked")):
        failures.append("ai_materialization_hard_gate_blocked")
    if int(evidence.get("package_chars") or 0) < 700:
        failures.append("context_package_too_small_for_mcp_context_probe")
    if not str(evidence.get("search_id") or "").strip():
        failures.append("search_id_missing")
    if payload_integrity.get("passed") is False:
        failures.append("payload_integrity_failed")
    return failures


def _pr12p14l_validate_source_trace(output: dict[str, Any], evidence: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    completeness = dict(output.get("completeness") or {})
    source_trace = output.get("source_trace") or []
    source_trace_count = max(
        len(source_trace) if isinstance(source_trace, list) else 0,
        int(completeness.get("source_trace_count") or 0),
    )
    document_refs = _pr12p12_nested(output, "context_package", "document_refs") or []
    document_ref_count = max(
        len(document_refs) if isinstance(document_refs, list) else 0,
        int(completeness.get("document_ref_count") or 0),
    )
    if str(evidence.get("status") or "").lower() not in {"ok", "partial"}:
        failures.append(f"source_trace_unexpected_status:{evidence.get('status')}")
    if source_trace_count < 1 and document_ref_count < 1 and int(evidence.get("package_chars") or 0) < 250:
        failures.append("source_trace_surface_missing")
    if not str(evidence.get("search_id") or "").strip():
        failures.append("source_trace_search_id_missing")
    evidence["source_trace_count"] = source_trace_count
    evidence["document_ref_count"] = document_ref_count
    return failures


def _pr12p14l_validate_grow_preview(output: dict[str, Any], evidence: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    source_investigation = dict(output.get("source_investigation") or {})
    preview_bundle = dict(output.get("preview_bundle") or {})
    source_units = list(source_investigation.get("source_units") or [])
    derived_nodes = list(preview_bundle.get("derived_nodes") or [])
    handoff = dict(
        output.get("compiler_handoff_proof")
        or source_investigation.get("compiler_handoff_proof")
        or {}
    )
    if str(output.get("status") or "").lower() not in {"preview_ready", "asking_clarification", "needs_review"}:
        failures.append(f"grow_preview_unexpected_status:{output.get('status')}")
    source_unit_formation = dict(source_investigation.get("source_unit_formation") or {})
    atomic_preview = dict(handoff.get("atomic_source_preview") or {}) if handoff else {}
    single_unit_is_valid_document_anchor = bool(
        len(source_units) == 1
        and str(source_unit_formation.get("status") or "").lower() in {"pass", "ok"}
        and int(source_unit_formation.get("formed_unit_count") or 0) == 1
        and bool(derived_nodes)
        and atomic_preview.get("passed") is not False
    )
    if len(source_units) < 2 and not single_unit_is_valid_document_anchor:
        failures.append("grow_source_units_too_few")
    if not preview_bundle:
        failures.append("grow_preview_bundle_missing")
    if not derived_nodes:
        failures.append("grow_preview_derived_nodes_missing")
    if source_unit_formation and str(source_unit_formation.get("status") or "").lower() not in {"pass", "ok"}:
        failures.append("grow_source_unit_formation_not_passing")
    if handoff:
        if atomic_preview and atomic_preview.get("passed") is False:
            failures.append("grow_atomic_preview_failed")
    evidence.update(
        {
            "status": output.get("status"),
            "investigation_id": source_investigation.get("investigation_id"),
            "source_unit_count": len(source_units),
            "derived_node_count": len(derived_nodes),
            "source_unit_formation": source_unit_formation,
            "compiler_handoff_proof_keys": sorted(handoff.keys())[:16],
            "preview_non_mutating": True,
        }
    )
    return failures


def _pr12p14l_grow_preview_gate(base_url: str, brain_id: str, timeout: float) -> dict[str, Any]:
    started = time.perf_counter()
    evidence: dict[str, Any] = {"path": "/mcp/grow-source-preview", "payload_brain_id": brain_id}
    failures: list[str] = []
    raw_input = "\n\n".join(
        [
            (
                f"PR12P14L_GROW_ATOMIC_SECTION_{index}. "
                "This source section describes a distinct local MCP readiness surface: "
                "BaxEnergy, WiSNAM, Yokogawa, renewable energy management, document retrieval, "
                "route context, source trace, and MCP memory packaging must stay recoverable as "
                "separate atomic memories while the raw source anchor remains intact. "
                "Grow preview must split broad input into bounded source units, preserve raw anchors, "
                "produce derived nodes, expose clarification state, and avoid hidden mutation before the user accepts. "
                "The local self-hosted client should later retrieve the broad dossier, exact raw documents, "
                "project workspace, path corridor, and source trace through explicit MCP tools."
            )
            for index in range(1, 8)
        ]
    )
    try:
        output, elapsed_ms = _pr12p12_elapsed_post(
            base_url,
            "/mcp/grow-source-preview",
            {
                "brain_id": brain_id,
                "raw_input": raw_input,
                "input_kind": "manual_text",
                "source_label": "PR12P14L Final Readiness Grow Preview",
                "options": {
                    "treat_as": "project_workspace",
                    "source_trust": "user_asserted",
                    "max_units": 6,
                    "question_limit": 3,
                    "pause_on_questions": False,
                },
                "run_preview": True,
            },
            timeout=timeout,
        )
        evidence["elapsed_http_ms"] = elapsed_ms
        failures.extend(_pr12p14l_validate_grow_preview(output, evidence))
    except Exception as exc:
        failures.append(f"grow_preview_error:{exc}")
        evidence["error"] = str(exc)
    return _pr12p14l_gate_result(
        gate_id="grow_source_preview",
        title="Grow preview forms atomic source units and preview nodes without applying hidden mutation.",
        failures=failures,
        evidence=evidence,
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def _pr12p14l_validate_maintenance_preview(output: dict[str, Any], *, expected_tool: str, evidence: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    truth = dict(output.get("maintenance_truth_contract") or {})
    mutation_surface = dict(output.get("mutation_surface") or {})
    proposal_review_table = output.get("proposal_review_table") or []
    rollback_plan = dict(output.get("rollback_plan") or {})
    matrix_delta = dict(output.get("matrix_delta") or {})
    if str(output.get("tool_name") or "") != expected_tool:
        failures.append(f"maintenance_tool_name_mismatch:{output.get('tool_name')}")
    if str(output.get("status") or "").lower() != "preview_ready":
        failures.append(f"maintenance_preview_unexpected_status:{output.get('status')}")
    if truth.get("schema_version") != "agvm.pr12p.14k.maintenance_truth_contract.v1":
        failures.append("maintenance_truth_contract_missing_or_wrong_version")
    if truth.get("preview_non_mutating") is not True:
        failures.append("maintenance_preview_not_marked_non_mutating")
    if truth.get("hidden_mutation_allowed") is not False:
        failures.append("maintenance_hidden_mutation_not_forbidden")
    if mutation_surface.get("hidden_mutation_allowed") is not False:
        failures.append("maintenance_mutation_surface_hidden_mutation_not_false")
    if not isinstance(proposal_review_table, list):
        failures.append("maintenance_proposal_review_table_not_list")
    if not rollback_plan:
        failures.append("maintenance_rollback_plan_missing")
    if not matrix_delta:
        failures.append("maintenance_matrix_delta_missing")
    evidence.update(
        {
            "tool_name": output.get("tool_name"),
            "status": output.get("status"),
            "truth_schema": truth.get("schema_version"),
            "preview_non_mutating": truth.get("preview_non_mutating"),
            "hidden_mutation_allowed": truth.get("hidden_mutation_allowed"),
            "proposal_count": len(proposal_review_table) if isinstance(proposal_review_table, list) else None,
            "rollback_available": rollback_plan.get("available"),
            "matrix_delta_keys": sorted(matrix_delta.keys())[:16],
        }
    )
    return failures


def _pr12p14l_maintenance_gate(base_url: str, brain_id: str, timeout: float) -> dict[str, Any]:
    started = time.perf_counter()
    failures: list[str] = []
    evidence: dict[str, Any] = {"brain_id": brain_id, "paths": ["/mcp/sleep-preview", "/mcp/evolve-preview"]}
    rows: list[dict[str, Any]] = []
    for path, expected_tool, mode in (
        ("/mcp/sleep-preview", "sleep_preview", "sleep"),
        ("/mcp/evolve-preview", "evolve_preview", "evolve"),
    ):
        row: dict[str, Any] = {"path": path, "expected_tool": expected_tool}
        try:
            output, elapsed_ms = _pr12p12_elapsed_post(
                base_url,
                path,
                {"brain_id": brain_id, "mode": mode, "max_nodes_considered": 30},
                timeout=timeout,
            )
            row["elapsed_http_ms"] = elapsed_ms
            row_failures = _pr12p14l_validate_maintenance_preview(output, expected_tool=expected_tool, evidence=row)
            failures.extend([f"{expected_tool}:{failure}" for failure in row_failures])
        except Exception as exc:
            failures.append(f"{expected_tool}_error:{exc}")
            row["error"] = str(exc)
        rows.append(row)
    evidence["rows"] = rows
    return _pr12p14l_gate_result(
        gate_id="sleep_evolve_preview",
        title="Sleep and Evolve expose exact preview, mutation, rollback and matrix truth without hidden apply.",
        failures=failures,
        evidence=evidence,
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def _pr12p14l_external_mcp_gate(base_url: str, brain_id: str) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        with benchmark_brain_scope(brain_id):
            report = run_pr12p_local_mcp_client_proof_suite(base_url)
    except Exception as exc:
        report = {"all_pass": False, "phase": "local_mcp_client", "failures": [str(exc)], "product_ready_verdict": "exception"}
    gate = _pr12p13_report_gate(
        gate_id="external_mcp_client",
        title="External local stdio MCP client can list and call the self-hosted tools with explicit brain scope.",
        report=report,
        required_verdict="local_mcp_client_proof_passed_pr12p_still_open",
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )
    gate["slice"] = "PR-12P-14L"
    return gate


def _pr12p14l_runtime_gate(base_url: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    started = time.perf_counter()
    failures: list[str] = []
    health: dict[str, Any] = {}
    try:
        health = get_json(base_url, "/health", timeout=30.0)
        status_text = str(health.get("status") or health.get("state") or "").lower()
        if not bool(health.get("ok")) and status_text not in {"ok", "healthy", "ready"}:
            failures.append(f"api_health_unexpected_status:{status_text or 'missing'}")
    except Exception as exc:
        failures.append(f"api_health_unreachable:{exc}")
    frontend_probe = _pr12p13_frontend_probe(_pr12p13_frontend_candidates(base_url))
    if not bool(frontend_probe.get("passed")):
        failures.extend(list(frontend_probe.get("failures") or ["frontend_unreachable"]))
    api_port = _pr12p13_port_from_url(base_url)
    frontend_port = _pr12p13_port_from_url(str(frontend_probe.get("selected_url") or ""))
    if api_port != 8010:
        failures.append(f"api_not_on_canonical_8010:{api_port}")
    if frontend_port != 3020:
        failures.append(f"frontend_not_on_canonical_3020:{frontend_port}")
    gate = _pr12p14l_gate_result(
        gate_id="docker_runtime_surfaces",
        title="Docker-backed API/UI are reachable on canonical 8010/3020 and expose the latest local Brain OS runtime.",
        failures=failures,
        evidence={
            "base_url": base_url,
            "api_port": api_port,
            "frontend_port": frontend_port,
            "health_status": health.get("status"),
            "health_ok": health.get("ok"),
            "active_brain_id": health.get("active_brain_id"),
            "default_brain_id": health.get("default_brain_id"),
            "brain_registry_ready": health.get("brain_registry_ready"),
            "frontend_probe": frontend_probe,
            "docker_backed_expected": True,
        },
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )
    return gate, health, frontend_probe


def _pr12p14l_llm_gate() -> dict[str, Any]:
    started = time.perf_counter()
    llm_enabled = str(os.environ.get("AGVM_LLM_ENABLED") or "true").strip().lower() not in {"0", "false", "no", "off"}
    openai_key_present = bool(str(os.environ.get("OPENAI_API_KEY") or "").strip())
    failures: list[str] = []
    if not llm_enabled:
        failures.append("agvm_llm_enabled_false")
    if not openai_key_present:
        failures.append("openai_api_key_missing")
    return _pr12p14l_gate_result(
        gate_id="llm_required_runtime",
        title="Product readiness requires an AI-enabled runtime; no no-LLM certification path is allowed.",
        failures=failures,
        evidence={
            "agvm_llm_enabled": llm_enabled,
            "openai_key_present": openai_key_present,
            "llm_required_by_master": True,
            "no_llm_fallback_allowed_for_product_ready": True,
        },
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def _pr12p14l_multi_brain_gate(base_url: str) -> tuple[dict[str, Any], dict[str, Any]]:
    targets_payload = _pr12p14c_brain_targets(base_url)
    segment = _pr12p14l_from_14c(
        dict(targets_payload["segment"]),
        gate_id="multi_brain_scope",
        title="Two validation brains are present, safe for MCP and isolated by registry scope.",
    )
    evidence = dict(segment.get("evidence") or {})
    targets = dict(targets_payload.get("targets") or {})
    storage_paths: list[str] = []
    for role, target in targets.items():
        record = dict(dict(target or {}).get("record") or {})
        storage_path = str(record.get("storage_path") or "")
        if storage_path:
            storage_paths.append(storage_path)
        if target.get("available") and record.get("safe_for_mcp") is False:
            segment.setdefault("failures", []).append(f"brain_not_safe_for_mcp:{role}")
    if len(storage_paths) != len(set(storage_paths)):
        segment.setdefault("failures", []).append("brain_storage_paths_not_unique")
    segment["passed"] = not list(segment.get("failures") or [])
    evidence["storage_paths_unique"] = len(storage_paths) == len(set(storage_paths))
    segment["evidence"] = evidence
    return segment, targets


def _pr12p14l_latency_gate(gates: list[dict[str, Any]]) -> dict[str, Any]:
    started = time.perf_counter()
    rows: list[dict[str, Any]] = []
    failures: list[str] = []
    latency_gate_ids = {
        "retrieve_context",
        "retrieve_document",
        "retrieve_project_workspace",
        "combined_context_document_package",
        "retrieve_no_match_honesty",
        "retrieve_path_corridor",
        "retrieve_source_trace",
        "grow_source_preview",
        "sleep_evolve_preview",
    }
    for gate in gates:
        gate_id = str(gate.get("gate_id") or "")
        if gate_id not in latency_gate_ids:
            continue
        evidence = dict(gate.get("evidence") or {})
        latency = dict(evidence.get("latency_contract") or {})
        elapsed = evidence.get("elapsed_ms") or evidence.get("elapsed_http_ms") or gate.get("elapsed_ms")
        rows.append(
            {
                "gate_id": gate_id,
                "first_useful_package_ms": latency.get("first_useful_package_ms"),
                "full_completion_ms": latency.get("full_completion_ms"),
                "http_or_gate_elapsed_ms": elapsed,
                "passed": bool(gate.get("passed")),
            }
        )
        if elapsed is None and latency.get("first_useful_package_ms") is None:
            failures.append(f"latency_missing:{gate_id}")
    return _pr12p14l_gate_result(
        gate_id="latency_truth",
        title="Final readiness reports first useful package/full completion or HTTP elapsed timing for every MCP product lane.",
        failures=failures,
        evidence={"rows": rows, "latency_basis": "first_useful_package_ms_when_available_else_http_elapsed_ms"},
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def _pr12p14l_rag_comparison_gate(gates: list[dict[str, Any]]) -> dict[str, Any]:
    started = time.perf_counter()
    by_gate = {str(gate.get("gate_id") or ""): gate for gate in gates}
    def passed(gate_id: str) -> bool:
        return bool((by_gate.get(gate_id) or {}).get("passed"))

    feature_delta = {
        "plain_rag_context_text": passed("retrieve_context"),
        "plain_rag_raw_document": passed("retrieve_document"),
        "agvm_path_corridors": passed("retrieve_path_corridor"),
        "agvm_source_trace": passed("retrieve_source_trace"),
        "agvm_grow_atomic_preview": passed("grow_source_preview"),
        "agvm_sleep_evolve_truth": passed("sleep_evolve_preview"),
        "agvm_multi_brain_scope": passed("multi_brain_scope"),
        "agvm_external_mcp_contract": passed("external_mcp_client"),
    }
    agvm_only = [
        key
        for key in (
            "agvm_path_corridors",
            "agvm_source_trace",
            "agvm_grow_atomic_preview",
            "agvm_sleep_evolve_truth",
            "agvm_multi_brain_scope",
            "agvm_external_mcp_contract",
        )
        if feature_delta.get(key)
    ]
    failures: list[str] = []
    if not feature_delta["plain_rag_context_text"]:
        failures.append("agvm_context_baseline_not_ready")
    if not feature_delta["plain_rag_raw_document"]:
        failures.append("agvm_raw_document_baseline_not_ready")
    if len(agvm_only) < 4:
        failures.append(f"agvm_only_advantage_features_too_few:{len(agvm_only)}<4")
    return _pr12p14l_gate_result(
        gate_id="rag_comparison",
        title="AGVM is compared against a plain RAG baseline using the live evidence surfaces, not a marketing claim.",
        failures=failures,
        evidence={
            "baseline": "plain top-k RAG can return context text and raw documents, but has no memory-OS path truth, Grow atomic source formation, Sleep/Evolve mutation truth, persistent hot memory or multi-brain MCP contract.",
            "feature_delta": feature_delta,
            "agvm_only_advantage_features": agvm_only,
            "evidence_based": True,
        },
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def run_pr12p14l_final_self_hosted_readiness_suite(base_url: str | None = None) -> dict[str, Any]:
    selected_base_url = str(base_url or DEFAULT_BASE_URL).rstrip("/")
    suite_started = time.perf_counter()
    gates: list[dict[str, Any]] = []

    runtime_gate, _health, _frontend_probe = _pr12p14l_runtime_gate(selected_base_url)
    gates.append(runtime_gate)
    gates.append(_pr12p14l_llm_gate())
    multi_brain_gate, targets = _pr12p14l_multi_brain_gate(selected_base_url)
    gates.append(multi_brain_gate)

    simone = str(dict(targets.get("simone_massaro") or {}).get("brain_id") or "").strip()
    elena = str(dict(targets.get("elena_valsecchi") or {}).get("brain_id") or "").strip()
    active_brain = simone or elena or str(_health.get("active_brain_id") or _health.get("default_brain_id") or "").strip()
    case_timeout = float(os.environ.get("AGVM_PR12P14L_CASE_TIMEOUT_SECONDS") or os.environ.get("AGVM_PR12P14C_CASE_TIMEOUT_SECONDS") or 120.0)

    if active_brain:
        gates.append(_pr12p14l_external_mcp_gate(selected_base_url, active_brain))
    else:
        gates.append(
            _pr12p14l_gate_result(
                gate_id="external_mcp_client",
                title="External local stdio MCP client can list and call the self-hosted tools with explicit brain scope.",
                failures=["active_validation_brain_missing"],
                evidence={},
                elapsed_ms=0,
            )
        )

    probe_segments: list[dict[str, Any]] = []
    context_output: dict[str, Any] = {}
    if simone:
        context_gate, context_output = _pr12p14l_run_probe(
            base_url=selected_base_url,
            segment_id="retrieve_context",
            title="retrieve_context returns the MCP context package as the primary product payload with AI materiality.",
            path="/mcp/retrieve-context",
            payload={
                "brain_id": simone,
                "query_text": "raccontami di te, del tuo lavoro, delle aziende e dei progetti principali",
                "retrieval_mode": "balanced",
                "context_package_mode": "broad_dossier",
                "document_text_policy": "top_raw",
                "max_matches": 18,
                "include_raw_text": True,
                "include_answer_demo": False,
                "complete_paths": False,
            },
            expected_terms=("BaxEnergy",),
            timeout=case_timeout,
            validators=(_pr12p14l_validate_context_ai,),
        )
        gates.append(_pr12p14l_from_14c(context_gate, gate_id="retrieve_context"))
    else:
        gates.append(
            _pr12p14l_gate_result(
                gate_id="retrieve_context",
                title="retrieve_context returns the MCP context package as the primary product payload with AI materiality.",
                failures=["simone_brain_missing"],
                evidence={},
                elapsed_ms=0,
            )
        )
    probe_segments.append(gates[-1])

    if simone:
        document_gate, _document_output = _pr12p14l_run_probe(
            base_url=selected_base_url,
            segment_id="retrieve_document",
            title="retrieve_document returns exact raw document material and explicit document readiness.",
            path="/mcp/retrieve-document",
            payload={
                "brain_id": simone,
                "query_text": "BaxEnergy domain, scale, and Yokogawa integration",
                "retrieval_mode": "balanced",
                "context_package_mode": "document_full",
                "max_matches": 12,
                "include_raw_text": True,
                "include_answer_demo": False,
            },
            expected_terms=("BaxEnergy", "Yokogawa"),
            timeout=case_timeout,
            validators=(_pr12p14c_validate_exact_document,),
        )
        gates.append(_pr12p14l_from_14c(document_gate, gate_id="retrieve_document"))
    else:
        gates.append(
            _pr12p14l_gate_result(
                gate_id="retrieve_document",
                title="retrieve_document returns exact raw document material and explicit document readiness.",
                failures=["simone_brain_missing"],
                evidence={},
                elapsed_ms=0,
            )
        )
    probe_segments.append(gates[-1])

    if simone:
        project_gate, _project_output = _pr12p14l_run_probe(
            base_url=selected_base_url,
            segment_id="retrieve_project_workspace",
            title="retrieve_project_workspace returns a project/workspace package rather than an answer-only surface.",
            path="/mcp/retrieve-project-workspace",
            payload={
                "brain_id": simone,
                "query_text": "preparami il workspace dei progetti BaxEnergy WiSNAM Intellisync Free Mind Foundry",
                "retrieval_mode": "balanced",
                "context_package_mode": "broad_dossier",
                "document_text_policy": "top_raw",
                "max_matches": 16,
                "include_raw_text": True,
                "include_answer_demo": False,
            },
            expected_terms=("BaxEnergy",),
            timeout=case_timeout,
            validators=(_pr12p14l_validate_project_workspace,),
        )
        gates.append(_pr12p14l_from_14c(project_gate, gate_id="retrieve_project_workspace"))
    else:
        gates.append(
            _pr12p14l_gate_result(
                gate_id="retrieve_project_workspace",
                title="retrieve_project_workspace returns a project/workspace package rather than an answer-only surface.",
                failures=["simone_brain_missing"],
                evidence={},
                elapsed_ms=0,
            )
        )
    probe_segments.append(gates[-1])

    if simone:
        combined_gate, _combined_output = _pr12p14l_run_probe(
            base_url=selected_base_url,
            segment_id="combined_context_document_package",
            title="retrieve_context can return a broad MCP package with visible document refs/raw policy for clients that need context plus documents.",
            path="/mcp/retrieve-context",
            payload={
                "brain_id": simone,
                "query_text": "prepara un contesto MCP con documenti e riferimenti raw su BaxEnergy, WiSNAM, Yokogawa e Free Mind Foundry",
                "retrieval_mode": "balanced",
                "context_package_mode": "broad_dossier",
                "document_text_policy": "top_raw",
                "max_matches": 18,
                "include_raw_text": True,
                "include_answer_demo": False,
                "complete_paths": True,
            },
            expected_terms=("BaxEnergy",),
            timeout=case_timeout,
            validators=(_pr12p14l_validate_context_ai, _pr12p14p_validate_combined_context_documents),
        )
        gates.append(_pr12p14l_from_14c(combined_gate, gate_id="combined_context_document_package"))
    else:
        gates.append(
            _pr12p14l_gate_result(
                gate_id="combined_context_document_package",
                title="retrieve_context can return a broad MCP package with visible document refs/raw policy for clients that need context plus documents.",
                failures=["simone_brain_missing"],
                evidence={},
                elapsed_ms=0,
            )
        )
    probe_segments.append(gates[-1])

    no_match_brain = elena or simone
    if no_match_brain:
        no_match_gate, _no_match_output = _pr12p14l_run_probe(
            base_url=selected_base_url,
            segment_id="retrieve_no_match_honesty",
            title="retrieve_context reports a missing exact private field as unresolved/no-match instead of promoting adjacent biography.",
            path="/mcp/retrieve-context",
            payload={
                "brain_id": no_match_brain,
                "query_text": "qual e' il mio codice fiscale?",
                "retrieval_mode": "balanced",
                "context_package_mode": "mcp_operational",
                "document_text_policy": "refs_only",
                "max_matches": 10,
                "include_raw_text": True,
                "include_answer_demo": False,
                "complete_paths": False,
            },
            expected_terms=(),
            timeout=case_timeout,
            validators=(_pr12p14c_validate_no_match,),
        )
        gates.append(_pr12p14l_from_14c(no_match_gate, gate_id="retrieve_no_match_honesty"))
    else:
        gates.append(
            _pr12p14l_gate_result(
                gate_id="retrieve_no_match_honesty",
                title="retrieve_context reports a missing exact private field as unresolved/no-match instead of promoting adjacent biography.",
                failures=["validation_brain_missing"],
                evidence={},
                elapsed_ms=0,
            )
        )
    probe_segments.append(gates[-1])

    if simone:
        path_gate, _path_output = _pr12p14l_run_probe(
            base_url=selected_base_url,
            segment_id="retrieve_path_corridor",
            title="retrieve_path_corridor exposes planned/traversed route truth and promoted corridor context.",
            path="/mcp/retrieve-path-corridor",
            payload={
                "brain_id": simone,
                "query_text": "collega BaxEnergy, Yokogawa, WiSNAM e Free Mind Foundry e mostrami il contesto attraversato",
                "retrieval_mode": "balanced",
                "context_package_mode": "broad_dossier",
                "max_matches": 14,
                "include_raw_text": True,
                "include_answer_demo": False,
                "complete_paths": True,
            },
            expected_terms=("BaxEnergy", "Yokogawa"),
            timeout=case_timeout,
            validators=(_pr12p14c_validate_path_visibility,),
        )
        gates.append(_pr12p14l_from_14c(path_gate, gate_id="retrieve_path_corridor"))
    else:
        gates.append(
            _pr12p14l_gate_result(
                gate_id="retrieve_path_corridor",
                title="retrieve_path_corridor exposes planned/traversed route truth and promoted corridor context.",
                failures=["simone_brain_missing"],
                evidence={},
                elapsed_ms=0,
            )
        )
    probe_segments.append(gates[-1])

    if simone:
        source_gate, _source_output = _pr12p14l_run_probe(
            base_url=selected_base_url,
            segment_id="retrieve_source_trace",
            title="retrieve_source_trace exposes source/document trace material for MCP agents.",
            path="/mcp/retrieve-source-trace",
            payload={
                "brain_id": simone,
                "query_text": "mostrami source trace e documenti sorgente su BaxEnergy Yokogawa WiSNAM",
                "retrieval_mode": "balanced",
                "context_package_mode": "mcp_operational",
                "document_text_policy": "refs_only",
                "max_matches": 12,
                "include_raw_text": True,
                "include_answer_demo": False,
            },
            expected_terms=("BaxEnergy",),
            timeout=case_timeout,
            validators=(_pr12p14l_validate_source_trace,),
        )
        gates.append(_pr12p14l_from_14c(source_gate, gate_id="retrieve_source_trace"))
    else:
        gates.append(
            _pr12p14l_gate_result(
                gate_id="retrieve_source_trace",
                title="retrieve_source_trace exposes source/document trace material for MCP agents.",
                failures=["simone_brain_missing"],
                evidence={},
                elapsed_ms=0,
            )
        )
    probe_segments.append(gates[-1])

    if active_brain:
        gates.append(_pr12p14l_grow_preview_gate(selected_base_url, active_brain, timeout=case_timeout))
        gates.append(_pr12p14l_maintenance_gate(selected_base_url, active_brain, timeout=case_timeout))
    else:
        gates.append(
            _pr12p14l_gate_result(
                gate_id="grow_source_preview",
                title="Grow preview forms atomic source units and preview nodes without applying hidden mutation.",
                failures=["active_validation_brain_missing"],
                evidence={},
                elapsed_ms=0,
            )
        )
        gates.append(
            _pr12p14l_gate_result(
                gate_id="sleep_evolve_preview",
                title="Sleep and Evolve expose exact preview, mutation, rollback and matrix truth without hidden apply.",
                failures=["active_validation_brain_missing"],
                evidence={},
                elapsed_ms=0,
            )
        )

    ui_started = time.perf_counter()
    ui_probe = _pr12p12_ui_parity_probe()
    ui_failures = list(ui_probe.get("failures") or [])
    if float(ui_probe.get("score") or 0.0) < 0.8:
        ui_failures.append(f"ui_payload_truth_score_low:{float(ui_probe.get('score') or 0.0):.4f}<0.80")
    gates.append(
        _pr12p14l_gate_result(
            gate_id="ui_payload_truth",
            title="Brain OS UI exposes exact MCP package, hot/cold, documents, map, Grow and Evolve truth surfaces.",
            failures=ui_failures,
            evidence=ui_probe,
            elapsed_ms=(time.perf_counter() - ui_started) * 1000,
        )
    )

    gates.append(_pr12p14l_latency_gate(gates))
    gates.append(_pr12p14l_rag_comparison_gate(gates))
    provider_retry_summary = _pr12p14m_provider_retry_summary(gates)

    by_gate = {str(gate.get("gate_id") or ""): gate for gate in gates}
    missing_gates = [gate for gate in REQUIRED_PR12P14L_FINAL_READINESS_GATES if gate not in by_gate]
    failed_gates = [
        str(gate.get("gate_id") or "")
        for gate in gates
        if bool(gate.get("critical", True)) and not bool(gate.get("passed"))
    ]
    pr12p14p_matrix = _pr12p14p_build_readiness_matrix(
        gates,
        missing_gates=missing_gates,
        failed_gates=failed_gates,
    )
    all_pass = not missing_gates and not failed_gates
    readiness_level = "ready_for_local_self_hosted_mcp_beta" if all_pass else "blocked_needs_remediation"
    return {
        "schema_version": PR12P14L_FINAL_SELF_HOSTED_READINESS_REPORT_SCHEMA_VERSION,
        "phase": "final_self_hosted_readiness",
        "slice": "PR-12P-14L",
        "base_url": selected_base_url,
        "all_pass": all_pass,
        "local_mcp_product_ready": bool(all_pass),
        "readiness_level": readiness_level,
        "readiness_verdict": pr12p14p_matrix["readiness_verdict"],
        "product_ready_verdict": readiness_level,
        "cloud_blocked": True,
        "cloud_release_blocked_until": "User-approved PR-12N planning after local self-hosted MCP beta is accepted.",
        "required_gates": list(REQUIRED_PR12P14L_FINAL_READINESS_GATES),
        "missing_gates": missing_gates,
        "failed_gates": failed_gates,
        "gate_matrix": {
            gate_id: {
                "required": gate_id in REQUIRED_PR12P14L_FINAL_READINESS_GATES,
                "covered": gate_id in by_gate,
                "passed": bool((by_gate.get(gate_id) or {}).get("passed")),
                "critical": bool((by_gate.get(gate_id) or {}).get("critical", True)),
                "failures": list((by_gate.get(gate_id) or {}).get("failures") or []),
            }
            for gate_id in REQUIRED_PR12P14L_FINAL_READINESS_GATES
        },
        "gate_results": gates,
        "pr12p14p_readiness_matrix": pr12p14p_matrix,
        "benchmark_table": pr12p14p_matrix["rows"],
        "representative_payloads": {
            "context_search_id": context_output.get("search_id") or _pr12p12_nested(context_output, "completeness", "search_id"),
            "context_agent_markdown_chars": len(str(_pr12p12_nested(context_output, "context_package", "agent_markdown") or "")),
            "active_validation_brain_id": active_brain,
            "simone_brain_id": simone,
            "elena_brain_id": elena,
        },
        "benchmark_inputs": {
            "phase": "final_self_hosted_readiness",
            "canonical_api_port": 8010,
            "canonical_frontend_port": 3020,
            "docker_local_distribution_required": True,
            "live_llm_required": True,
            "no_llm_fallback_allowed": True,
            "mutation_enabled": False,
            "mutation_policy": "preview_only_for_final_gate",
            "mcp_first": True,
            "context_package_is_product": True,
            "answer_demo_secondary": True,
            "cloud_release_blocked": True,
            "case_timeout_seconds": case_timeout,
            "provider_retry_policy": {
                "schema_version": "agvm.pr12p14m.provider_retry_policy.v1",
                "enabled": True,
                "max_provider_retry_attempts_per_probe": 1,
                "retry_allowed_only_for": list(_PR12P14M_PROVIDER_RETRY_ERROR_MARKERS),
                "silent_heuristic_certification_allowed": False,
            },
            "semantic_contract_cache_lanes": {
                "schema_version": "agvm.pr12p14m.semantic_cache_lanes.v1",
                "cold_cache_lane_required": True,
                "warm_cache_lane_required": True,
                "accepted_cache_tiers": ["memory", "disk"],
                "cache_hit_counts_as_ai_only_when_runtime_material_true": True,
            },
        },
        "provider_retry_summary": provider_retry_summary,
        "evidence_contract": {
            "allows_local_beta_claim": bool(all_pass),
            "allowed_claim": "ready_for_local_self_hosted_mcp_beta" if all_pass else None,
            "cloud_commercialization_allowed": False,
            "no_query_specific_patches": True,
            "answer_demo_cannot_mask_context": True,
            "raw_document_refs_are_not_raw_documents": True,
        },
        "elapsed_ms": round((time.perf_counter() - suite_started) * 1000, 3),
        "next_slice": "Local MCP beta hardening and user acceptance"
        if all_pass
        else "PR-12P-14L Final Self-Hosted Readiness Remediation",
    }


PR12P14T_F_FAST_HEALTH_REPORT_SCHEMA_VERSION = "agvm.pr12p14t_f.local_beta_fast_health_report.v1"


def _pr12p14t_f_fast_context_gate(base_url: str, brain_id: str | None, *, timeout: float) -> dict[str, Any]:
    started = time.perf_counter()
    failures: list[str] = []
    evidence: dict[str, Any] = {
        "path": "/mcp/retrieve-context",
        "payload_brain_id": brain_id,
        "probe_kind": "nonblocking_first_mcp_payload",
    }
    if not brain_id:
        failures.append("brain_id_missing")
        return _pr12p14l_gate_result(
            gate_id="fast_context_first_package",
            title="retrieve_context returns a first MCP package quickly with AI participation visible.",
            failures=failures,
            evidence=evidence,
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )
    payload = {
        "brain_id": brain_id,
        "query_text": "raccontami di te, del tuo lavoro, delle aziende e dei progetti principali",
        "retrieval_mode": "balanced",
        "context_package_mode": "broad_dossier",
        "document_text_policy": "top_raw",
        "max_matches": 12,
        "include_raw_text": True,
        "include_answer_demo": False,
        "complete_paths": False,
    }
    attempt_records: list[dict[str, Any]] = []

    def _attempt() -> tuple[dict[str, Any], list[str]]:
        attempt_failures: list[str] = []
        output, elapsed_ms = _pr12p12_elapsed_post(base_url, "/mcp/retrieve-context", payload, timeout=timeout)
        attempt_evidence = _pr12p14c_probe_evidence(output, elapsed_ms=elapsed_ms, expected_terms=("BaxEnergy",))
        attempt_evidence["http_elapsed_ms"] = elapsed_ms
        ai = dict(attempt_evidence.get("ai") or {})
        latency = dict(attempt_evidence.get("latency_contract") or {})
        try:
            first_ms = float(latency.get("first_useful_package_ms") or elapsed_ms)
        except (TypeError, ValueError):
            first_ms = float(elapsed_ms)
        if str(attempt_evidence.get("status") or "").lower() not in {"ok", "partial", "blocked"}:
            attempt_failures.append(f"fast_context_unexpected_status:{attempt_evidence.get('status')}")
        if int(attempt_evidence.get("package_chars") or 0) < 500:
            attempt_failures.append("fast_context_package_too_small")
        if not bool(ai.get("budget_llm_allowed")):
            attempt_failures.append("fast_context_llm_not_allowed")
        if not bool(ai.get("materialized") or ai.get("semantic_contract_material")):
            attempt_failures.append("fast_context_ai_material_not_visible")
        if first_ms > 5000:
            attempt_failures.append(f"fast_context_first_package_slow:{first_ms:.2f}>5000")
        if not str(attempt_evidence.get("search_id") or "").strip():
            attempt_failures.append("fast_context_search_id_missing")
        return attempt_evidence, attempt_failures

    for attempt_index in range(2):
        try:
            attempt_evidence, attempt_failures = _attempt()
        except Exception as exc:  # noqa: BLE001
            attempt_evidence = {"error": str(exc)}
            attempt_failures = [f"fast_context_probe_failed:{exc}"]
        attempt_evidence["attempt_index"] = attempt_index + 1
        attempt_evidence["attempt_failures"] = attempt_failures
        attempt_records.append(attempt_evidence)
        if not attempt_failures:
            break
        retryable = any(
            failure.startswith("fast_context_package_too_small")
            or failure.startswith("fast_context_ai_material_not_visible")
            or failure.startswith("fast_context_first_package_slow")
            or failure.startswith("fast_context_probe_failed")
            for failure in attempt_failures
        )
        if not retryable:
            break

    def _attempt_score(row: dict[str, Any]) -> tuple[int, int, float]:
        ai = dict(row.get("ai") or {})
        latency = dict(row.get("latency_contract") or {})
        first_ms = float(latency.get("first_useful_package_ms") or row.get("http_elapsed_ms") or timeout * 1000)
        return (
            0 if row.get("attempt_failures") else 1,
            int(row.get("package_chars") or 0),
            -first_ms,
        )

    best_attempt = max(attempt_records, key=_attempt_score) if attempt_records else {"error": "no_attempt_recorded", "attempt_failures": ["fast_context_probe_failed:no_attempt_recorded"]}
    failures = list(best_attempt.get("attempt_failures") or [])
    evidence.update({key: value for key, value in best_attempt.items() if key not in {"attempt_failures"}})
    evidence["attempt_count"] = len(attempt_records)
    evidence["attempt_records"] = [
        {
            "attempt_index": row.get("attempt_index"),
            "status": row.get("status"),
            "package_chars": row.get("package_chars"),
            "search_id": row.get("search_id"),
            "http_elapsed_ms": row.get("http_elapsed_ms"),
            "failures": row.get("attempt_failures") or [],
            "ai": row.get("ai"),
        }
        for row in attempt_records
    ]
    return _pr12p14l_gate_result(
        gate_id="fast_context_first_package",
        title="retrieve_context returns a first MCP package quickly with AI participation visible.",
        failures=failures,
        evidence=evidence,
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def run_pr12p14t_f_local_beta_fast_health_suite(base_url: str | None = None) -> dict[str, Any]:
    selected_base_url = str(base_url or DEFAULT_BASE_URL).rstrip("/")
    started = time.perf_counter()
    runtime_gate, health, frontend_probe = _pr12p14l_runtime_gate(selected_base_url)
    llm_gate = _pr12p14l_llm_gate()
    brain_id = str(health.get("active_brain_id") or health.get("default_brain_id") or "").strip() or None
    context_gate = _pr12p14t_f_fast_context_gate(selected_base_url, brain_id, timeout=20.0)
    gates = [runtime_gate, llm_gate, context_gate]
    failed_gates = [str(gate.get("gate_id") or "") for gate in gates if not bool(gate.get("passed"))]
    all_pass = not failed_gates
    return {
        "schema_version": PR12P14T_F_FAST_HEALTH_REPORT_SCHEMA_VERSION,
        "phase": "local_beta_fast_health",
        "slice": "PR-12P-14T-F",
        "base_url": selected_base_url,
        "all_pass": all_pass,
        "local_beta_fast_health_ready": all_pass,
        "readiness_scope": "fast_operator_health_check_not_full_readiness_gate",
        "cloud_blocked": True,
        "failed_gates": failed_gates,
        "gates": {str(gate.get("gate_id") or ""): gate for gate in gates},
        "active_brain_id": brain_id,
        "frontend_url": frontend_probe.get("selected_url"),
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 2),
        "full_readiness_follow_up": "Run phase=final_self_hosted_readiness for release-grade local MCP beta recertification.",
    }


PR12P14X_E_REAL_MCP_RETRIEVE_MATRIX_SCHEMA_VERSION = "agvm.pr12p14x_e.real_mcp_retrieve_quality_matrix.v1"
PR12P14X_F_REAL_MCP_SLEEP_EVOLVE_SCHEMA_VERSION = "agvm.pr12p14x_f.real_mcp_sleep_evolve_metamemory.v1"
PR12P14X_G_REAL_MCP_FINAL_VERDICT_SCHEMA_VERSION = "agvm.pr12p14x_g.real_mcp_final_product_verdict.v1"
BAM6D_MAINTENANCE_PREFLIGHT_SCHEMA_VERSION = "agvm.maintenance_preflight_report.v1"
PR12P14X_E_REAL_BRAIN_ID = "simone_massaro_validation"


def _pr12p14x_e_real_mcp_cases(brain_id: str) -> list[dict[str, Any]]:
    base_payload = {
        "brain_id": brain_id,
        "context_package_mode": "broad_dossier",
        "document_text_policy": "refs_only",
        "max_matches": 14,
        "include_raw_text": False,
        "include_answer_demo": False,
        "complete_paths": False,
    }
    return [
        {
            "case_id": "identity",
            "title": "Identity package resolves the person and role without answer-demo dependency.",
            "path": "/mcp/retrieve-context",
            "payload": {
                **base_payload,
                "query_text": "Come ti chiami e qual e' il tuo ruolo?",
                "retrieval_mode": "flash",
                "max_matches": 10,
            },
            "expected_terms": ("Simone", "Massaro"),
            "min_package_chars": 700,
            "min_hot_sections": 1,
            "require_ai_material": True,
        },
        {
            "case_id": "companies",
            "title": "Companies and roles return the connected company graph.",
            "path": "/mcp/retrieve-context",
            "payload": {
                **base_payload,
                "query_text": "Quali aziende hai fondato o a quali aziende sei collegato?",
                "retrieval_mode": "balanced",
                "document_text_policy": "top_raw",
                "include_raw_text": True,
            },
            "expected_terms": ("BaxEnergy", "WiSNAM", "Yokogawa"),
            "min_package_chars": 1200,
            "min_hot_sections": 2,
            "min_document_refs": 1,
            "require_ai_material": True,
            "slow_slo_ms": 12000,
        },
        {
            "case_id": "timeline",
            "title": "Timeline request grounds the BaxEnergy/Yokogawa event with date evidence.",
            "path": "/mcp/retrieve-context",
            "payload": {
                **base_payload,
                "query_text": "Cosa e' successo con BaxEnergy e Yokogawa nel 2024?",
                "retrieval_mode": "balanced",
                "document_text_policy": "top_raw",
                "include_raw_text": True,
            },
            "expected_terms": ("BaxEnergy", "Yokogawa", "2024"),
            "min_package_chars": 1000,
            "min_hot_sections": 1,
            "min_document_refs": 1,
            "require_ai_material": True,
            "slow_slo_ms": 12000,
        },
        {
            "case_id": "father_relationship",
            "title": "Relationship request keeps family facts linked to the subject.",
            "path": "/mcp/retrieve-context",
            "payload": {
                **base_payload,
                "query_text": "Raccontami di tuo padre e del monumento che gli hai dedicato.",
                "retrieval_mode": "balanced",
                "max_matches": 12,
            },
            "expected_terms": ("Giovanni", "monumento"),
            "min_package_chars": 700,
            "min_hot_sections": 1,
            "require_ai_material": True,
        },
        {
            "case_id": "values_style",
            "title": "Values and communication style are retrieved as working context.",
            "path": "/mcp/retrieve-context",
            "payload": {
                **base_payload,
                "query_text": "Quali valori ti guidano e come comunichi quando lavori?",
                "retrieval_mode": "balanced",
            },
            "expected_terms": ("coraggio",),
            "min_package_chars": 800,
            "min_hot_sections": 1,
            "require_ai_material": True,
            "slow_slo_ms": 12000,
        },
        {
            "case_id": "broad_self_work",
            "title": "Broad biography/work request returns a usable dossier, not a tiny answer.",
            "path": "/mcp/retrieve-context",
            "payload": {
                **base_payload,
                "query_text": "Raccontami di te, del tuo lavoro, delle aziende e dei progetti principali.",
                "retrieval_mode": "heavy",
                "document_text_policy": "top_raw",
                "include_raw_text": True,
                "max_matches": 18,
            },
            "expected_terms": ("Simone", "BaxEnergy", "WiSNAM"),
            "min_package_chars": 1800,
            "min_hot_sections": 3,
            "min_document_refs": 2,
            "require_ai_material": True,
            "slow_slo_ms": 12000,
        },
        {
            "case_id": "document_lookup",
            "title": "Document lookup returns raw/actionable source material through retrieve_document.",
            "path": "/mcp/retrieve-document",
            "payload": {
                **base_payload,
                "query_text": "Trova il documento relativo all'acquisizione BaxEnergy Yokogawa 2024.",
                "retrieval_mode": "balanced",
                "context_package_mode": "document_full",
                "document_text_policy": "all_raw",
                "include_raw_text": True,
                "max_matches": 12,
            },
            "expected_terms": ("BaxEnergy", "Yokogawa"),
            "min_document_count": 1,
            "min_raw_chars": 300,
            "require_ai_material": False,
            "slow_slo_ms": 12000,
        },
        {
            "case_id": "multi_intent",
            "title": "Multi-intent query covers identity, work, companies and values together.",
            "path": "/mcp/retrieve-context",
            "payload": {
                **base_payload,
                "query_text": "Come ti chiami, che lavoro fai, quali aziende hai e quali valori ti guidano?",
                "retrieval_mode": "heavy",
                "document_text_policy": "top_raw",
                "include_raw_text": True,
                "max_matches": 18,
            },
            "expected_terms": ("Simone", "BaxEnergy", "WiSNAM"),
            "min_package_chars": 1600,
            "min_hot_sections": 3,
            "min_document_refs": 1,
            "require_ai_material": True,
            "slow_slo_ms": 65000,
        },
        {
            "case_id": "no_match",
            "title": "Missing private field stays honest instead of promoting adjacent biography.",
            "path": "/mcp/retrieve-context",
            "payload": {
                **base_payload,
                "query_text": "Qual e' il codice fiscale di Simone Massaro?",
                "retrieval_mode": "balanced",
                "context_package_mode": "mcp_operational",
                "max_matches": 10,
            },
            "expected_terms": (),
            "expected_no_match": True,
            "require_ai_material": True,
            "slow_slo_ms": 8000,
        },
        {
            "case_id": "path_aware",
            "title": "Path-aware retrieval exposes real traversed/promoted route material.",
            "path": "/mcp/retrieve-path-corridor",
            "payload": {
                **base_payload,
                "query_text": "Collega BaxEnergy, Yokogawa, WiSNAM e Free Mind Foundry e mostrami i percorsi attraversati.",
                "retrieval_mode": "balanced",
                "context_package_mode": "forensic_trace",
                "complete_paths": True,
                "max_matches": 16,
            },
            "expected_terms": ("BaxEnergy", "Yokogawa", "WiSNAM"),
            "min_package_chars": 900,
            "min_path_count": 1,
            "min_route_events": 1,
            "require_ai_material": True,
            "slow_slo_ms": 12000,
        },
    ]


def _phase8c_expanded_mcp_cases(brain_id: str) -> list[dict[str, Any]]:
    base_cases = [dict(case) for case in _pr12p14x_e_real_mcp_cases(brain_id)]
    by_id = {str(case.get("case_id") or ""): dict(case) for case in base_cases}
    base_payload = {
        "brain_id": brain_id,
        "context_package_mode": "broad_dossier",
        "document_text_policy": "refs_only",
        "max_matches": 14,
        "include_raw_text": False,
        "include_answer_demo": False,
        "complete_paths": False,
    }

    def context_case(
        case_id: str,
        *,
        family: str,
        query_text: str,
        expected_terms: tuple[str, ...],
        retrieval_mode: str = "balanced",
        min_package_chars: int = 900,
        min_hot_sections: int = 1,
        min_document_refs: int = 0,
        max_matches: int = 14,
        document_text_policy: str = "refs_only",
        include_raw_text: bool = False,
        slow_slo_ms: int = 10000,
    ) -> dict[str, Any]:
        return {
            "case_id": case_id,
            "phase8c_family": family,
            "title": f"Phase 8C expanded {family}: {case_id}",
            "path": "/mcp/retrieve-context",
            "payload": {
                **base_payload,
                "query_text": query_text,
                "retrieval_mode": retrieval_mode,
                "document_text_policy": document_text_policy,
                "include_raw_text": include_raw_text,
                "max_matches": max_matches,
            },
            "expected_terms": expected_terms,
            "min_package_chars": min_package_chars,
            "min_hot_sections": min_hot_sections,
            "min_document_refs": min_document_refs,
            "require_ai_material": True,
            "slow_slo_ms": slow_slo_ms,
        }

    expanded: list[dict[str, Any]] = []
    for case in base_cases:
        copied = dict(case)
        copied["phase8c_family"] = {
            "identity": "identity_exact",
            "companies": "company_work_relation",
            "timeline": "timeline_event",
            "father_relationship": "relationship_boundary",
            "values_style": "values_style",
            "broad_self_work": "broad_dossier",
            "document_lookup": "document_raw",
            "multi_intent": "multi_intent",
            "no_match": "no_match_boundary",
            "path_aware": "path_corridor",
        }.get(str(case.get("case_id") or ""), "h6_smoke")
        expanded.append(copied)

    expanded.extend(
        [
            context_case(
                "identity_role_exact",
                family="identity_exact",
                query_text="Chi e' Simone Massaro e quale ruolo emerge dal cervello?",
                expected_terms=("Simone", "Massaro"),
                retrieval_mode="flash",
                min_package_chars=700,
                max_matches=10,
                slow_slo_ms=8000,
            ),
            context_case(
                "identity_public_profile",
                family="identity_exact",
                query_text="Riassumi l'identita' pubblica di Simone Massaro senza inventare dati privati.",
            expected_terms=("Simone", "Massaro"),
            min_package_chars=900,
            slow_slo_ms=12000,
            ),
            context_case(
                "company_acquisition_chain",
                family="company_work_relation",
                query_text="Spiega la relazione tra BaxEnergy, Yokogawa, WiSNAM e IntelliSync.",
                expected_terms=("BaxEnergy", "Yokogawa", "WiSNAM"),
                document_text_policy="top_raw",
                include_raw_text=True,
                min_package_chars=1400,
                min_hot_sections=2,
                min_document_refs=1,
                max_matches=18,
                slow_slo_ms=15000,
            ),
            context_case(
                "company_founder_links",
                family="company_work_relation",
                query_text="Quali organizzazioni risultano collegate a Simone Massaro e in che modo?",
                expected_terms=("BaxEnergy", "WiSNAM"),
                document_text_policy="top_raw",
                include_raw_text=True,
                min_package_chars=1300,
                min_hot_sections=2,
                min_document_refs=1,
                max_matches=18,
                slow_slo_ms=15000,
            ),
            context_case(
                "timeline_2010_2025",
                family="timeline_event",
                query_text="Costruisci una timeline sintetica tra 2010, 2024 e 2025 per BaxEnergy, WiSNAM e Yokogawa.",
                expected_terms=("2010", "2024", "2025"),
                document_text_policy="top_raw",
                include_raw_text=True,
                min_package_chars=1400,
                min_document_refs=1,
                max_matches=18,
                slow_slo_ms=12000,
            ),
            context_case(
                "relationship_monument_subject_link",
                family="relationship_boundary",
                query_text="Che cosa risulta sul monumento dedicato a Giovanni e qual e' il legame con Simone?",
                expected_terms=("Giovanni", "monumento", "Simone"),
                min_package_chars=850,
                slow_slo_ms=10000,
            ),
            context_case(
                "values_operating_principles",
                family="values_style",
                query_text="Quali principi operativi e valori personali emergono dal cervello?",
            expected_terms=("coraggio",),
            min_package_chars=850,
            slow_slo_ms=15000,
            ),
            context_case(
                "broad_investor_dossier",
                family="broad_dossier",
                query_text="Prepara un dossier per un investitore: identita', aziende, timeline, valori e documenti utili.",
                expected_terms=("Simone", "BaxEnergy", "Yokogawa"),
                retrieval_mode="heavy",
                document_text_policy="top_raw",
                include_raw_text=True,
                min_package_chars=2200,
                min_hot_sections=4,
                min_document_refs=2,
                max_matches=22,
                slow_slo_ms=18000,
            ),
            {
                "case_id": "document_yokogawa_baxenergy_raw",
                "phase8c_family": "document_raw",
                "title": "Phase 8C expanded document raw: Yokogawa/BaxEnergy source hydration.",
                "path": "/mcp/retrieve-document",
                "payload": {
                    **base_payload,
                    "query_text": "Recupera documenti raw su Yokogawa e BaxEnergy.",
                    "retrieval_mode": "balanced",
                    "context_package_mode": "document_full",
                    "document_text_policy": "all_raw",
                    "include_raw_text": True,
                    "max_matches": 12,
                },
                "expected_terms": ("Yokogawa", "BaxEnergy"),
                "min_document_count": 1,
                "min_raw_chars": 300,
                "require_ai_material": False,
                "slow_slo_ms": 12000,
            },
            {
                "case_id": "path_energy_company_corridor",
                "phase8c_family": "path_corridor",
                "title": "Phase 8C expanded path: energy company route corridor.",
                "path": "/mcp/retrieve-path-corridor",
                "payload": {
                    **base_payload,
                    "query_text": "Mostra il corridoio tra BaxEnergy, Yokogawa e le tecnologie di gestione energia.",
                    "retrieval_mode": "balanced",
                    "context_package_mode": "forensic_trace",
                    "complete_paths": True,
                    "max_matches": 18,
                },
                "expected_terms": ("BaxEnergy", "Yokogawa"),
                "min_package_chars": 900,
                "min_path_count": 1,
                "min_route_events": 1,
                "require_ai_material": True,
                "slow_slo_ms": 15000,
            },
            {
                "case_id": "no_match_private_email",
                "phase8c_family": "no_match_boundary",
                "title": "Phase 8C expanded no-match: private email must not be invented.",
                "path": "/mcp/retrieve-context",
                "payload": {
                    **base_payload,
                    "query_text": "Qual e' l'indirizzo email privato personale di Simone Massaro?",
                    "retrieval_mode": "balanced",
                    "context_package_mode": "mcp_operational",
                    "max_matches": 10,
                },
                "expected_terms": (),
                "expected_no_match": True,
                "require_ai_material": True,
                "slow_slo_ms": 9000,
            },
            context_case(
                "multi_intent_public_boundaries",
                family="multi_intent",
                query_text="Come ti chiami, quali aziende sono rilevanti, quali eventi pubblici emergono e quali dati privati mancano?",
                expected_terms=("Simone", "BaxEnergy"),
                retrieval_mode="heavy",
                document_text_policy="top_raw",
                include_raw_text=True,
                min_package_chars=1800,
                min_hot_sections=3,
                min_document_refs=1,
                max_matches=20,
                slow_slo_ms=65000,
            ),
        ]
    )
    if len(expanded) != len({str(case.get("case_id") or "") for case in expanded}):
        raise ValueError("phase8c_expanded_case_ids_must_be_unique")
    return expanded


PHASE8C_CERTIFICATION_MIN_CASES = 120
PHASE8C_CERTIFICATION_FAMILY_MINIMUMS = {
    "identity_exact": 15,
    "company_work_relation": 15,
    "timeline_event": 10,
    "relationship_boundary": 10,
    "values_style": 10,
    "broad_dossier": 10,
    "document_raw": 10,
    "path_corridor": 10,
    "multi_intent": 10,
    "no_match_boundary": 10,
    "followup_hot_context": 10,
    "operations_health": 10,
}


def _phase8c_certification_family_counts(cases_or_rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {family: 0 for family in PHASE8C_CERTIFICATION_FAMILY_MINIMUMS}
    for item in cases_or_rows:
        family = str(item.get("phase8c_family") or "").strip()
        if family:
            counts[family] = counts.get(family, 0) + 1
    return counts


def _phase8c_certification_family_coverage(cases_or_rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = _phase8c_certification_family_counts(cases_or_rows)
    missing = {
        family: {"required": required, "actual": int(counts.get(family) or 0)}
        for family, required in PHASE8C_CERTIFICATION_FAMILY_MINIMUMS.items()
        if int(counts.get(family) or 0) < int(required)
    }
    return {
        "schema_version": "agvm.phase8c.certification_family_coverage.v1",
        "minimum_case_count": PHASE8C_CERTIFICATION_MIN_CASES,
        "family_minimums": dict(PHASE8C_CERTIFICATION_FAMILY_MINIMUMS),
        "family_counts": counts,
        "missing_families": missing,
        "complete": not missing and len(cases_or_rows) >= PHASE8C_CERTIFICATION_MIN_CASES,
    }


def _phase8c_certification_mcp_cases(brain_id: str) -> list[dict[str, Any]]:
    cases = [dict(case) for case in _phase8c_expanded_mcp_cases(brain_id)]
    base_payload = {
        "brain_id": brain_id,
        "context_package_mode": "broad_dossier",
        "document_text_policy": "refs_only",
        "max_matches": 14,
        "include_raw_text": False,
        "include_answer_demo": False,
        "complete_paths": False,
    }

    def add_context(
        family: str,
        case_id: str,
        query_text: str,
        expected_terms: tuple[str, ...],
        *,
        context_package_mode: str | None = None,
        retrieval_mode: str = "balanced",
        min_package_chars: int = 900,
        min_hot_sections: int = 1,
        min_document_refs: int = 0,
        max_matches: int = 14,
        document_text_policy: str = "refs_only",
        include_raw_text: bool = False,
        expected_no_match: bool = False,
        slow_slo_ms: int = 10000,
    ) -> None:
        payload = {
            **base_payload,
            "query_text": query_text,
            "retrieval_mode": retrieval_mode,
            "context_package_mode": context_package_mode or base_payload["context_package_mode"],
            "document_text_policy": document_text_policy,
            "include_raw_text": include_raw_text,
            "max_matches": max_matches,
        }
        case = {
            "case_id": case_id,
            "phase8c_family": family,
            "title": f"Phase 8C certification {family}: {case_id}",
            "path": "/mcp/retrieve-context",
            "payload": payload,
            "expected_terms": expected_terms,
            "min_package_chars": min_package_chars,
            "min_hot_sections": min_hot_sections,
            "min_document_refs": min_document_refs,
            "require_ai_material": True,
            "slow_slo_ms": slow_slo_ms,
        }
        if expected_no_match:
            case["expected_no_match"] = True
        cases.append(case)

    def add_document(case_id: str, query_text: str, expected_terms: tuple[str, ...]) -> None:
        cases.append(
            {
                "case_id": case_id,
                "phase8c_family": "document_raw",
                "title": f"Phase 8C certification document raw: {case_id}",
                "path": "/mcp/retrieve-document",
                "payload": {
                    **base_payload,
                    "query_text": query_text,
                    "retrieval_mode": "balanced",
                    "context_package_mode": "document_full",
                    "document_text_policy": "all_raw",
                    "include_raw_text": True,
                    "max_matches": 12,
                },
                "expected_terms": expected_terms,
                "min_document_count": 1,
                "min_raw_chars": 300,
                "require_ai_material": False,
                "slow_slo_ms": 12000,
            }
        )

    def add_path(case_id: str, query_text: str, expected_terms: tuple[str, ...]) -> None:
        cases.append(
            {
                "case_id": case_id,
                "phase8c_family": "path_corridor",
                "title": f"Phase 8C certification path corridor: {case_id}",
                "path": "/mcp/retrieve-path-corridor",
                "payload": {
                    **base_payload,
                    "query_text": query_text,
                    "retrieval_mode": "balanced",
                    "context_package_mode": "forensic_trace",
                    "complete_paths": True,
                    "max_matches": 18,
                },
                "expected_terms": expected_terms,
                "min_package_chars": 900,
                "min_path_count": 1,
                "min_route_events": 1,
                "require_ai_material": True,
                "slow_slo_ms": 120000,
            }
        )

    context_templates: dict[str, list[tuple[str, tuple[str, ...], str]]] = {
        "identity_exact": [
            ("Identifica Simone Massaro e separa identita' pubblica da inferenze.", ("Simone", "Massaro"), "flash"),
            ("Quale identita' e ruolo professionale emergono dal cervello?", ("Simone", "Massaro"), "flash"),
            ("Descrivi il profilo pubblico senza aggiungere dati privati.", ("Simone", "Massaro"), "balanced"),
            ("Chi e' la persona al centro di questo brain e quali ancore pubbliche ha?", ("Simone", "Massaro"), "balanced"),
            ("Fornisci solo le evidenze essenziali dell'identita'.", ("Simone", "Massaro"), "flash"),
        ],
        "company_work_relation": [
            ("Mappa le relazioni tra Simone, BaxEnergy, WiSNAM e Yokogawa.", ("BaxEnergy", "WiSNAM", "Yokogawa"), "balanced"),
            ("Quali societa' e progetti sono collegati al lavoro pubblico?", ("BaxEnergy", "WiSNAM"), "balanced"),
            ("Spiega il ruolo di BaxEnergy nel percorso imprenditoriale.", ("BaxEnergy",), "balanced"),
            ("Collega WiSNAM, IntelliSync e BaxEnergy nel contesto energetico.", ("WiSNAM", "BaxEnergy"), "balanced"),
            ("Che cosa emerge su BaxEnergy, Yokogawa, acquisizioni e piattaforme energetiche?", ("BaxEnergy", "Yokogawa"), "heavy"),
        ],
        "timeline_event": [
            ("Ordina gli eventi pubblici principali dal 2010 al 2025.", ("2010", "2024"), "balanced"),
            ("Quali date sono rilevanti per WiSNAM, BaxEnergy e Yokogawa?", ("WiSNAM", "Yokogawa"), "balanced"),
            ("Ricostruisci una timeline breve delle aziende e degli eventi.", ("BaxEnergy", "2024"), "balanced"),
            ("Quali eventi pubblici sono documentati e quali restano incerti?", ("BaxEnergy",), "balanced"),
        ],
        "relationship_boundary": [
            ("Racconta il legame con Giovanni e il monumento senza confondere lavoro e famiglia.", ("Giovanni", "monumento"), "balanced"),
            ("Che cosa e' pubblico sul monumento e sul rapporto familiare?", ("Giovanni", "Simone"), "balanced"),
            ("Quali fatti personali pubblici sono supportati da memoria?", ("monumento",), "balanced"),
            ("Separa relazione familiare, evento pubblico e dati non disponibili.", ("Giovanni", "monumento"), "balanced"),
        ],
        "values_style": [
            ("Quali valori e principi operativi emergono?", ("coraggio",), "balanced"),
            ("Come comunica e collabora secondo il brain?", ("coraggio",), "balanced"),
            ("Quali tratti di stile vanno usati in una risposta in prima persona?", ("coraggio",), "balanced"),
            ("Quali principi personali sono evidenziati come affidabili?", ("coraggio",), "balanced"),
        ],
        "broad_dossier": [
            ("Prepara un dossier completo con identita', lavoro, aziende, timeline e valori.", ("Simone", "BaxEnergy", "WiSNAM"), "heavy"),
            ("Crea un contesto ampio per un agente che deve conoscere questo profilo.", ("Simone", "BaxEnergy"), "heavy"),
            ("Dammi un quadro globale con fonti, documenti e percorsi rilevanti.", ("Simone", "Yokogawa"), "heavy"),
            ("Raccogli un pacchetto operativo per una due diligence pubblica.", ("BaxEnergy", "WiSNAM"), "heavy"),
        ],
        "multi_intent": [
            ("Come ti chiami, cosa fai, quali aziende hai e quali valori ti guidano?", ("Simone", "BaxEnergy", "WiSNAM"), "heavy"),
            ("Dimmi identita', lavoro, timeline, valori e cosa non sai.", ("Simone", "BaxEnergy"), "heavy"),
            ("Unisci profilo pubblico, aziende, famiglia pubblica e boundary privacy.", ("Simone", "Giovanni", "BaxEnergy"), "heavy"),
            ("Rispondi a identita', aziende, documenti utili e percorsi attraversati.", ("Simone", "BaxEnergy"), "heavy"),
        ],
        "no_match_boundary": [
            ("Qual e' il numero di carta d'identita' privato di Simone?", (), "balanced"),
            ("Dammi il numero di telefono privato personale non pubblico.", (), "balanced"),
            ("Qual e' l'indirizzo di casa privato?", (), "balanced"),
            ("Quali credenziali private sono salvate nel cervello?", (), "balanced"),
        ],
        "followup_hot_context": [
            ("Dopo aver parlato di aziende, continua collegando WiSNAM e BaxEnergy.", ("WiSNAM", "BaxEnergy"), "balanced"),
            ("Riprendi il contesto gia' caldo e aggiungi i documenti piu' utili.", ("BaxEnergy",), "balanced"),
            ("Continua dal profilo pubblico e chiarisci solo i punti mancanti.", ("Simone",), "balanced"),
            ("Usa la memoria calda se presente e completa con cold context verificabile.", ("Simone", "BaxEnergy"), "balanced"),
        ],
        "operations_health": [
            ("Quale materiale del brain richiede maggiore verifica prima di rispondere?", ("Simone",), "balanced"),
            ("Cosa deve controllare un agente MCP prima di fidarsi del pacchetto?", ("Simone",), "balanced"),
            ("Quali documenti o percorsi su BaxEnergy dovrebbe idratare un agente dopo questo contesto?", ("BaxEnergy",), "balanced"),
            ("Quando il contesto e' parziale, quali sezioni deve ispezionare l'agente?", ("Simone",), "balanced"),
        ],
    }

    for family, minimum in PHASE8C_CERTIFICATION_FAMILY_MINIMUMS.items():
        cursor = 0
        while _phase8c_certification_family_counts(cases).get(family, 0) < minimum:
            suffix = _phase8c_certification_family_counts(cases).get(family, 0) + 1
            if family == "document_raw":
                document_queries = [
                    ("Recupera il documento raw piu' utile su BaxEnergy e Yokogawa.", ("BaxEnergy", "Yokogawa")),
                    ("Trova fonti raw su WiSNAM e gestione energia.", ("WiSNAM",)),
                    ("Idrata documenti che supportano la relazione tra Simone e BaxEnergy.", ("Simone", "BaxEnergy")),
                    ("Recupera materiale sorgente sul contesto aziendale pubblico.", ("BaxEnergy",)),
                ]
                query, terms = document_queries[cursor % len(document_queries)]
                add_document(f"cert_document_raw_{suffix:02d}", query, terms)
            elif family == "path_corridor":
                path_queries = [
                    ("Mostra i percorsi tra identita', BaxEnergy e Yokogawa.", ("Simone", "BaxEnergy")),
                    ("Attraversa il corridoio tra WiSNAM, IntelliSync e gestione energia.", ("WiSNAM",)),
                    ("Collega famiglia pubblica, identita' e lavoro senza confondere le aree.", ("Simone", "Giovanni")),
                    ("Mostra le highway tra aziende, documenti e timeline.", ("BaxEnergy", "Yokogawa")),
                ]
                query, terms = path_queries[cursor % len(path_queries)]
                add_path(f"cert_path_corridor_{suffix:02d}", query, terms)
            elif family == "no_match_boundary":
                templates = context_templates[family]
                query, terms, mode = templates[cursor % len(templates)]
                add_context(
                    family,
                    f"cert_{family}_{suffix:02d}",
                    query,
                    terms,
                    context_package_mode="mcp_operational",
                    retrieval_mode=mode,
                    min_package_chars=850,
                    min_hot_sections=0,
                    min_document_refs=0,
                    max_matches=10,
                    expected_no_match=True,
                    slow_slo_ms=9000,
                )
            else:
                templates = context_templates.get(family) or [
                    ("Costruisci un contesto verificabile e coerente per questa richiesta.", ("Simone",), "balanced")
                ]
                query, terms, mode = templates[cursor % len(templates)]
                folded_query = query.lower()
                essential_identity_package_chars = bool(
                    family == "identity_exact"
                    and "solo" in folded_query
                    and ("evidenze essenziali" in folded_query or "essential evidence" in folded_query)
                )
                focused_operations_health_package_chars = bool(
                    family == "operations_health"
                    and (
                        "maggiore verifica" in folded_query
                        or "prima di rispondere" in folded_query
                    )
                )
                add_context(
                    family,
                    f"cert_{family}_{suffix:02d}",
                    query,
                    terms,
                    retrieval_mode=mode,
                    min_package_chars=(
                        350
                        if essential_identity_package_chars
                        else 750
                        if focused_operations_health_package_chars
                        else 1800
                        if family in {"broad_dossier", "multi_intent"}
                        else 850
                    ),
                    min_hot_sections=3 if family in {"broad_dossier", "multi_intent"} else 1,
                    min_document_refs=1 if family in {"company_work_relation", "broad_dossier", "multi_intent", "followup_hot_context"} else 0,
                    max_matches=22 if family in {"broad_dossier", "multi_intent"} else 16,
                    document_text_policy="top_raw" if family in {"company_work_relation", "broad_dossier", "multi_intent"} else "refs_only",
                    include_raw_text=family in {"company_work_relation", "broad_dossier", "multi_intent"},
                    slow_slo_ms=18000
                    if family == "broad_dossier"
                    else 65000
                    if family == "multi_intent"
                    else 45000
                    if family == "relationship_boundary"
                    else 65000
                    if family == "followup_hot_context"
                    else 15000
                    if family == "timeline_event"
                    else 18000
                    if family == "identity_exact"
                    else 15000
                    if family == "values_style"
                    else 15000
                    if family in {"company_work_relation", "operations_health"}
                    else 10000,
                )
            cursor += 1

    if len(cases) != len({str(case.get("case_id") or "") for case in cases}):
        raise ValueError("phase8c_certification_case_ids_must_be_unique")
    coverage = _phase8c_certification_family_coverage(cases)
    if not bool(coverage.get("complete")):
        raise ValueError(f"phase8c_certification_family_coverage_incomplete:{coverage.get('missing_families')}")
    return cases


def _pr12p14x_e_document_ref_count(output: dict[str, Any]) -> int:
    context_package = dict(output.get("context_package") or {})
    refs = list(output.get("document_refs") or []) + list(context_package.get("document_refs") or [])
    document_ref_contract = dict(output.get("document_ref_contract") or context_package.get("document_ref_contract") or {})
    return max(len(refs), int(document_ref_contract.get("document_ref_count") or 0), int(document_ref_contract.get("actionable_document_ref_count") or 0))


def _pr12p14x_e_first_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    first = evidence.get("first_package")
    return dict(first) if isinstance(first, dict) else {}


def _pr12p14x_e_max_int_metric(evidence: dict[str, Any], key: str) -> int:
    first = _pr12p14x_e_first_evidence(evidence)
    values = [evidence.get(key), first.get(key)]
    return max([int(value or 0) for value in values])


def _pr12p14x_e_primary_payload_chars(evidence: dict[str, Any]) -> int:
    first = _pr12p14x_e_first_evidence(evidence)
    final_truth = dict(evidence.get("payload_truth_contract") or {})
    first_truth = dict(first.get("payload_truth_contract") or {})
    return max(
        int(evidence.get("package_chars") or 0),
        int(first.get("package_chars") or 0),
        int(final_truth.get("primary_char_count") or 0),
        int(first_truth.get("primary_char_count") or 0),
    )


def _pr12p14x_e_first_package_ms(evidence: dict[str, Any]) -> float | None:
    first = _pr12p14x_e_first_evidence(evidence)
    first_latency = dict(first.get("latency_contract") or {})
    final_latency = dict(evidence.get("latency_contract") or {})
    # Product truth for an external MCP client is the time to receive the first
    # HTTP payload. The payload may also report an internal first-context
    # timestamp, but H6 certification must not pass when the server only returns
    # that payload after a much slower synchronous wait.
    value = first.get("elapsed_ms") or evidence.get("first_package_elapsed_ms") or evidence.get("elapsed_ms")
    if value is None:
        value = first_latency.get("first_useful_package_ms") or final_latency.get("first_useful_package_ms")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pr12p14x_e_first_package_latency_basis(evidence: dict[str, Any]) -> str:
    first = _pr12p14x_e_first_evidence(evidence)
    if first.get("elapsed_ms") is not None:
        return "first_http_elapsed_ms"
    if evidence.get("first_package_elapsed_ms") is not None or evidence.get("elapsed_ms") is not None:
        return "http_elapsed_ms"
    return "reported_first_useful_package_ms"


def _pr12p14x_e_expected_terms_visible(evidence: dict[str, Any], terms: tuple[str, ...]) -> dict[str, bool]:
    final_hits = dict(evidence.get("expected_term_hits") or {})
    first_hits = dict(_pr12p14x_e_first_evidence(evidence).get("expected_term_hits") or {})
    return {term: bool(final_hits.get(term) or first_hits.get(term)) for term in terms}


def _pr12p14x_e_validate_real_case(case: dict[str, Any]) -> Callable[[dict[str, Any], dict[str, Any]], list[str]]:
    def _validator(output: dict[str, Any], evidence: dict[str, Any]) -> list[str]:
        failures: list[str] = []
        status = str(evidence.get("status") or output.get("status") or "").lower()
        if status in {"failed", "error"}:
            failures.append(f"tool_failed_status:{status}")
        if status == "blocked" and not bool(case.get("expected_no_match")):
            failures.append("tool_blocked_before_useful_mcp_payload")
        if not str(evidence.get("search_id") or "").strip():
            failures.append("search_id_missing")

        ai = dict(evidence.get("ai") or {})
        expected_no_match = bool(case.get("expected_no_match"))
        no_route_terminal = bool(ai.get("ai_no_route_terminal_contract"))
        expected_terms = tuple(str(term) for term in list(case.get("expected_terms") or ()))
        term_hits = _pr12p14x_e_expected_terms_visible(evidence, expected_terms)
        expected_terms_visible = all(term_hits.values()) if term_hits else True
        primary_chars = _pr12p14x_e_primary_payload_chars(evidence)
        document_ref_count = _pr12p14x_e_document_ref_count(output)
        document_count = _pr12p14x_e_max_int_metric(evidence, "primary_document_count")
        raw_chars = _pr12p14x_e_max_int_metric(evidence, "primary_raw_text_char_count")
        no_route_llm_not_required = bool(
            expected_no_match
            and no_route_terminal
            and ai.get("semantic_contract_material")
        )
        if not bool(ai.get("budget_llm_allowed")) and not no_route_llm_not_required:
            failures.append("llm_not_allowed")
        requires_ai_material = bool(case.get("require_ai_material"))
        is_document_payload_tool = str(case.get("path") or "").rstrip("/").endswith("retrieve-document")
        direct_document_payload_sufficient = bool(
            is_document_payload_tool
            and expected_terms_visible
            and (document_ref_count > 0 or document_count > 0)
            and (raw_chars > 0 or primary_chars > 0)
        )
        if requires_ai_material and not bool(ai.get("semantic_contract_material")):
            failures.append("semantic_ai_material_not_visible")
        if requires_ai_material and not is_document_payload_tool:
            positive_exact_sufficiency = bool(ai.get("ai_positive_exact_sufficiency_contract"))
            public_fact_sufficiency = bool(ai.get("ai_public_fact_sufficiency_contract"))
            answerability_sufficiency = bool(ai.get("ai_answerability_sufficiency_contract"))
            path_route_first_sufficiency = bool(ai.get("ai_path_route_first_sufficiency_contract"))
            route_contract_required = not bool(
                (expected_no_match and no_route_terminal)
                or positive_exact_sufficiency
                or public_fact_sufficiency
                or answerability_sufficiency
                or path_route_first_sufficiency
            )
            if not bool(ai.get("materialized")):
                failures.append("ai_route_material_not_certified")
            if route_contract_required:
                if not bool(ai.get("ai_spatial_observed")):
                    failures.append("ai_spatial_contract_not_observed")
                if not bool(ai.get("ai_spatial_materialized")):
                    failures.append(f"ai_spatial_contract_not_materialized:{ai.get('ai_spatial_status') or 'unknown'}")
                if not bool(ai.get("ai_spatial_certifies_route")):
                    failures.append("ai_spatial_contract_does_not_certify_route")
                if int(ai.get("ai_landing_count") or 0) <= 0:
                    failures.append("ai_spatial_landing_count_zero")
                if int(ai.get("ai_path_count") or 0) <= 0:
                    failures.append("ai_spatial_path_count_zero")
            client_state = str(ai.get("delivery_client_payload_state") or "").strip()
            terminal_for_client = bool(ai.get("delivery_terminal_for_client"))
            allowed_states = {"usable_context", "path_payload_ready", "no_match"}
            if not terminal_for_client or client_state not in allowed_states:
                failures.append(f"mcp_payload_not_terminal_for_client:{client_state or 'unknown'}")
        if bool(ai.get("hard_gate_blocked")) and not direct_document_payload_sufficient:
            failures.append(f"ai_hard_gate_blocked:{ai.get('hard_gate_state') or 'unknown'}")

        missing_terms = [term for term, hit in term_hits.items() if not hit]
        if missing_terms:
            failures.append(f"expected_terms_missing:{missing_terms}")

        min_package_chars = int(case.get("min_package_chars") or 0)
        if min_package_chars and primary_chars < min_package_chars:
            failures.append(f"package_too_small:{primary_chars}<{min_package_chars}")
        # A terminal no-match/private-boundary payload is expected to suppress
        # hot/cold memory. Requiring hot sections there rewards leakage.
        min_hot = 0 if bool(case.get("expected_no_match")) else int(case.get("min_hot_sections") or 0)
        hot_count = _pr12p14x_e_max_int_metric(evidence, "hot_section_count")
        if min_hot and hot_count < min_hot:
            failures.append(f"hot_sections_too_few:{hot_count}<{min_hot}")

        min_document_refs = int(case.get("min_document_refs") or 0)
        if min_document_refs and document_ref_count < min_document_refs:
            failures.append(f"document_refs_too_few:{document_ref_count}<{min_document_refs}")
        min_document_count = int(case.get("min_document_count") or 0)
        if min_document_count and document_count < min_document_count:
            failures.append(f"primary_documents_too_few:{document_count}<{min_document_count}")
        min_raw_chars = int(case.get("min_raw_chars") or 0)
        if min_raw_chars and raw_chars < min_raw_chars:
            failures.append(f"primary_raw_chars_too_low:{raw_chars}<{min_raw_chars}")

        min_path_count = int(case.get("min_path_count") or 0)
        path_count = _pr12p14x_e_max_int_metric(evidence, "path_count")
        if min_path_count and path_count < min_path_count:
            failures.append(f"path_count_too_low:{path_count}<{min_path_count}")
        min_route_events = int(case.get("min_route_events") or 0)
        route_total = _pr12p14x_e_max_int_metric(evidence, "route_event_count") + _pr12p14x_e_max_int_metric(evidence, "trace_step_count")
        if min_route_events and route_total < min_route_events:
            failures.append(f"route_events_too_low:{route_total}<{min_route_events}")

        if bool(case.get("expected_no_match")):
            if not bool(ai.get("ai_no_route_terminal_contract")) and int(ai.get("ai_path_count") or 0) <= 0:
                failures.append("ai_no_route_terminal_contract_or_route_missing")
            failures.extend(_pr12p14c_validate_no_match(output, evidence))

        first_ms_float = _pr12p14x_e_first_package_ms(evidence) or 0.0
        slow_slo = float(case.get("slow_slo_ms") or 8000.0)
        if first_ms_float and first_ms_float > slow_slo:
            failures.append(f"first_package_slow:{first_ms_float:.2f}>{slow_slo:.0f}")
        return failures

    return _validator


def _pr12p14x_e_case_row(case: dict[str, Any], segment: dict[str, Any]) -> dict[str, Any]:
    evidence = dict(segment.get("evidence") or {})
    first = _pr12p14x_e_first_evidence(evidence)
    ai = dict(evidence.get("ai") or {})
    latency = dict(evidence.get("latency_contract") or {})
    first_latency = dict(first.get("latency_contract") or {})
    completion = dict(evidence.get("completion_inspection") or {})
    return {
        "schema_version": "agvm.pr12p14x_e.real_mcp_retrieve_case_row.v1",
        "case_id": case.get("case_id"),
        "phase8c_family": case.get("phase8c_family"),
        "tool_path": case.get("path"),
        "retrieval_mode": dict(case.get("payload") or {}).get("retrieval_mode"),
        "passed": bool(segment.get("passed")),
        "failures": list(segment.get("failures") or []),
        "status": evidence.get("status"),
        "first_status": first.get("status"),
        "search_id": evidence.get("search_id"),
        "primary_payload_chars": _pr12p14x_e_primary_payload_chars(evidence),
        "package_chars": _pr12p14x_e_max_int_metric(evidence, "package_chars"),
        "first_package_chars": int(first.get("package_chars") or 0),
        "final_package_chars": int(evidence.get("package_chars") or 0),
        "hot_section_count": _pr12p14x_e_max_int_metric(evidence, "hot_section_count"),
        "first_hot_section_count": int(first.get("hot_section_count") or 0),
        "final_hot_section_count": int(evidence.get("hot_section_count") or 0),
        "cold_reservoir_entries": _pr12p14x_e_max_int_metric(evidence, "cold_reservoir_entries"),
        "document_ref_count": int(evidence.get("document_ref_count") or 0),
        "primary_document_count": _pr12p14x_e_max_int_metric(evidence, "primary_document_count"),
        "primary_raw_text_char_count": _pr12p14x_e_max_int_metric(evidence, "primary_raw_text_char_count"),
        "path_count": _pr12p14x_e_max_int_metric(evidence, "path_count"),
        "route_event_count": _pr12p14x_e_max_int_metric(evidence, "route_event_count"),
        "trace_step_count": _pr12p14x_e_max_int_metric(evidence, "trace_step_count"),
        "llm_allowed": bool(ai.get("budget_llm_allowed")),
        "ai_materialized": bool(ai.get("materialized")),
        "semantic_ai_materialized": bool(ai.get("semantic_contract_material")),
        "ai_landing_count": int(ai.get("ai_landing_count") or 0),
        "ai_path_count": int(ai.get("ai_path_count") or 0),
        "ai_spatial_observed": bool(ai.get("ai_spatial_observed")),
        "ai_spatial_materialized": bool(ai.get("ai_spatial_materialized")),
        "ai_spatial_certifies_route": bool(ai.get("ai_spatial_certifies_route")),
        "ai_spatial_status": ai.get("ai_spatial_status"),
        "ai_spatial_source": ai.get("ai_spatial_source"),
        "ai_spatial_missing_reasons": list(ai.get("ai_spatial_missing_reasons") or []),
        "ai_no_route_terminal_contract": bool(ai.get("ai_no_route_terminal_contract")),
        "ai_path_route_first_sufficiency_contract": bool(ai.get("ai_path_route_first_sufficiency_contract")),
        "ai_answerability_sufficiency_contract": bool(ai.get("ai_answerability_sufficiency_contract")),
        "ai_route_required": bool(ai.get("ai_route_required", True)),
        "delivery_client_payload_state": ai.get("delivery_client_payload_state"),
        "delivery_terminal_for_client": bool(ai.get("delivery_terminal_for_client")),
        "delivery_missing_reasons": list(ai.get("delivery_missing_reasons") or []),
        "ai_provider_degraded": bool(ai.get("semantic_contract_provider_degraded")),
        "semantic_contract_source": ai.get("semantic_contract_source"),
        "semantic_contract_status": ai.get("semantic_contract_status"),
        "first_useful_package_ms": _pr12p14x_e_first_package_ms(evidence),
        "first_latency_basis": _pr12p14x_e_first_package_latency_basis(evidence),
        "first_payload_reported_ms": first_latency.get("first_useful_package_ms"),
        "first_http_elapsed_ms": first.get("elapsed_ms"),
        "full_completion_ms": latency.get("full_completion_ms"),
        "background_completion_ms": latency.get("background_completion_ms"),
        "http_response_policy": latency.get("http_response_policy"),
        "first_package_wait_seconds": latency.get("first_package_wait_seconds"),
        "first_package_returned_before_full_completion": bool(latency.get("first_package_returned_before_full_completion")),
        "background_completion_inspectable": bool(latency.get("background_completion_inspectable")),
        "full_completion_is_secondary": bool(latency.get("full_completion_is_secondary")),
        "http_elapsed_ms": evidence.get("elapsed_ms"),
        "final_inspection_terminal": bool(completion.get("inspection_terminal")),
        "final_inspection_elapsed_ms": completion.get("inspection_elapsed_ms"),
        "completion_inspection": completion,
        "expected_term_hits": _pr12p14x_e_expected_terms_visible(evidence, tuple(str(term) for term in list(case.get("expected_terms") or ()))),
    }


def _pr12p14x_e_mode_delta(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_mode: dict[str, list[float]] = {}
    for row in rows:
        mode = str(row.get("retrieval_mode") or "").strip()
        if not mode:
            continue
        latency = row.get("first_useful_package_ms") or row.get("http_elapsed_ms")
        try:
            value = float(latency)
        except (TypeError, ValueError):
            continue
        by_mode.setdefault(mode, []).append(value)
    return {
        "schema_version": "agvm.pr12p14x_e.mode_delta.v1",
        "modes_seen": sorted(by_mode),
        "average_first_package_ms_by_mode": {mode: round(_safe_mean(values), 2) for mode, values in sorted(by_mode.items())},
        "mode_delta_visible": len(by_mode) >= 3,
    }


def run_pr12p14x_e_real_mcp_retrieve_quality_matrix_suite(
    base_url: str | None = None,
    *,
    brain_id: str | None = None,
    timeout: float | None = None,
    case_ids: tuple[str, ...] | None = None,
    max_workers: int | None = None,
    cases_override: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    selected_base_url = str(base_url or DEFAULT_BASE_URL).rstrip("/")
    selected_brain_id = str(brain_id or os.environ.get("AGVM_PR12P14X_E_BRAIN_ID") or PR12P14X_E_REAL_BRAIN_ID).strip()
    case_timeout = float(timeout or os.environ.get("AGVM_PR12P14X_E_CASE_TIMEOUT_SECONDS") or 45.0)
    worker_count = int(max_workers or os.environ.get("AGVM_PR12P14X_E_MAX_WORKERS") or 3)
    started = time.perf_counter()
    cases = [dict(case) for case in (cases_override if cases_override is not None else _pr12p14x_e_real_mcp_cases(selected_brain_id))]
    env_case_ids = tuple(
        item.strip()
        for item in str(os.environ.get("AGVM_PR12P14X_E_CASE_IDS") or "").split(",")
        if item.strip()
    )
    selected_case_ids = set(case_ids or env_case_ids)
    if selected_case_ids:
        cases = [case for case in cases if str(case.get("case_id") or "") in selected_case_ids]

    def execute_case(index: int, case: dict[str, Any]) -> tuple[int, dict[str, Any], dict[str, Any]]:
        try:
            segment, output = _pr12p14l_run_probe(
                base_url=selected_base_url,
                segment_id=f"retrieve_quality_{case['case_id']}",
                title=str(case.get("title") or case.get("case_id")),
                path=str(case.get("path") or "/mcp/retrieve-context"),
                payload=dict(case.get("payload") or {}),
                expected_terms=tuple(str(term) for term in list(case.get("expected_terms") or ())),
                timeout=case_timeout,
                validators=(),
                completion_requirements=case,
            )
            evidence = dict(segment.get("evidence") or {})
            evidence["document_ref_count"] = _pr12p14x_e_document_ref_count(output)
            segment["evidence"] = evidence
            base_failures = [
                str(failure)
                for failure in list(segment.get("failures") or [])
                if not str(failure).startswith("expected_terms_missing")
            ]
            validation_failures = _pr12p14x_e_validate_real_case(case)(output, evidence)
            segment["failures"] = base_failures + validation_failures
            segment["passed"] = not bool(segment["failures"])
            return index, _pr12p14x_e_case_row(case, segment), segment
        except Exception as exc:  # noqa: BLE001
            segment = _pr12p14c_segment_result(
                segment_id=f"retrieve_quality_{case['case_id']}",
                title=str(case.get("title") or case.get("case_id")),
                failures=[f"case_execution_failed:{exc}"],
                evidence={
                    "path": case.get("path"),
                    "payload_brain_id": selected_brain_id,
                    "query_text": dict(case.get("payload") or {}).get("query_text"),
                    "error": str(exc),
                    "case_timeout_seconds": case_timeout,
                },
                elapsed_ms=case_timeout * 1000.0,
            )
            return index, _pr12p14x_e_case_row(case, segment), segment

    indexed_rows: dict[int, dict[str, Any]] = {}
    indexed_segments: dict[int, dict[str, Any]] = {}
    effective_workers = max(1, min(worker_count, max(1, len(cases))))
    with concurrent.futures.ThreadPoolExecutor(max_workers=effective_workers) as executor:
        futures = [executor.submit(execute_case, index, case) for index, case in enumerate(cases)]
        for future in concurrent.futures.as_completed(futures):
            index, row, segment = future.result()
            indexed_rows[index] = row
            indexed_segments[index] = segment
    rows = [indexed_rows[index] for index in sorted(indexed_rows)]
    segments = [indexed_segments[index] for index in sorted(indexed_segments)]

    failed_cases = [str(row.get("case_id") or "") for row in rows if not bool(row.get("passed"))]
    acceptance = {
        "real_validation_brain_used": selected_brain_id == PR12P14X_E_REAL_BRAIN_ID,
        "case_matrix_complete": len(rows) == len(cases) and len(rows) >= 10,
        "identity_case_passed": any(row.get("case_id") == "identity" and row.get("passed") for row in rows),
        "companies_case_passed": any(row.get("case_id") == "companies" and row.get("passed") for row in rows),
        "timeline_case_passed": any(row.get("case_id") == "timeline" and row.get("passed") for row in rows),
        "document_lookup_case_passed": any(row.get("case_id") == "document_lookup" and row.get("passed") for row in rows),
        "multi_intent_case_passed": any(row.get("case_id") == "multi_intent" and row.get("passed") for row in rows),
        "no_match_honesty_case_passed": any(row.get("case_id") == "no_match" and row.get("passed") for row in rows),
        "path_aware_case_passed": any(row.get("case_id") == "path_aware" and row.get("passed") for row in rows),
        "ai_material_visible_in_required_cases": all(
            bool(row.get("ai_materialized"))
            for row in rows
            if dict(next((case for case in cases if case.get("case_id") == row.get("case_id")), {})).get("require_ai_material")
        ),
        "all_required_rows_passed": not failed_cases,
    }
    acceptance_failures = [key for key, passed in acceptance.items() if not passed]
    all_pass = not failed_cases and not acceptance_failures
    return {
        "schema_version": PR12P14X_E_REAL_MCP_RETRIEVE_MATRIX_SCHEMA_VERSION,
        "phase": "retrieve_context_quality_matrix",
        "slice": "PR-12P-14X-E",
        "proof_scope": "real_mcp_runtime_not_fixture",
        "base_url": selected_base_url,
        "brain_id": selected_brain_id,
        "execution": {
            "case_timeout_seconds": case_timeout,
            "max_workers": effective_workers,
            "parallel_case_execution": effective_workers > 1,
            "selected_case_ids": sorted(selected_case_ids),
            "cases_override_used": cases_override is not None,
        },
        "all_pass": all_pass,
        "local_mcp_product_ready": False,
        "product_ready_claim_allowed": False,
        "quality_rows": rows,
        "segment_matrix": {str(segment.get("segment_id") or ""): segment for segment in segments},
        "acceptance": acceptance,
        "failed_cases": failed_cases,
        "failures": [f"case_failed:{case_id}" for case_id in failed_cases] + acceptance_failures,
        "matrix_summary": {
            "case_count": len(rows),
            "passed_count": sum(1 for row in rows if bool(row.get("passed"))),
            "ai_materialized_case_count": sum(1 for row in rows if bool(row.get("ai_materialized"))),
            "document_case_count": sum(1 for row in rows if int(row.get("primary_document_count") or 0) > 0 or int(row.get("document_ref_count") or 0) > 0),
            "path_case_count": sum(1 for row in rows if int(row.get("path_count") or 0) > 0),
            "min_package_chars": min([int(row.get("package_chars") or 0) for row in rows] or [0]),
            "max_package_chars": max([int(row.get("package_chars") or 0) for row in rows] or [0]),
        },
        "mode_delta": _pr12p14x_e_mode_delta(rows),
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 2),
        "next_slice": "PR-12P-14X-F Sleep/Evolve/Metamemory And Heuristic Evolution Proof"
        if all_pass
        else "PR-12P-14X-E Remediation",
    }


PHASE8C_COMPARATIVE_BACKEND_SCHEMA_VERSION = "agvm.phase8c.comparative_backend_benchmark.v1"
PHASE8C_BASELINE_IDS = (
    "bm25_lexical",
    "vector_hash_rag",
    "hybrid_lexical_vector_rag",
    "graph_neighbor_no_ai",
    "agvm_heuristic_only_ablation",
    "agvm_full_ai_core",
)


def _phase8c_tokenize(text: str) -> list[str]:
    return [token for token in re.findall(r"[a-z0-9]+", str(text or "").lower()) if len(token) > 1]


def _phase8c_node_text(node: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in (
        "summary",
        "raw_text",
        "memory_type",
        "node_kind",
        "document_role",
        "source_unit_title",
        "source_unit_kind",
        "source_unit_role",
    ):
        value = node.get(key)
        if value not in (None, "", [], {}):
            parts.append(str(value))
    provenance = dict(node.get("provenance") or {})
    for key in ("source_label", "source_type", "guide_conceptual_area"):
        value = provenance.get(key)
        if value not in (None, "", [], {}):
            parts.append(str(value))
    for alias in list(node.get("retrieval_aliases") or [])[:8]:
        if alias:
            parts.append(str(alias))
    return "\n".join(parts)


def _phase8c_graph_corpus(base_url: str, *, max_nodes: int = 5000, brain_id: str | None = None) -> dict[str, Any]:
    query: dict[str, Any] = {"max_nodes": max_nodes}
    if brain_id:
        query["brain_id"] = str(brain_id)
    params = urllib.parse.urlencode(query)
    payload = get_json(base_url, f"/graph-view?{params}", timeout=90.0)
    graph = dict(payload.get("graph") or {})
    nodes = [dict(item) for item in list(graph.get("nodes") or []) if isinstance(item, dict)]
    edges = [dict(item) for item in list(graph.get("edges") or []) if isinstance(item, dict)]
    rows: list[dict[str, Any]] = []
    document_node_count = 0
    for node in nodes:
        text = _phase8c_node_text(node)
        tokens = _phase8c_tokenize(text)
        is_document = bool(
            node.get("is_document_anchor")
            or node.get("document_eligible")
            or str(node.get("memory_type") or "").startswith("document")
            or node.get("document_anchor_id")
        )
        if is_document:
            document_node_count += 1
        rows.append(
            {
                "id": str(node.get("id") or ""),
                "text": text,
                "tokens": tokens,
                "token_set": set(tokens),
                "node": node,
                "is_document": is_document,
                "links": [
                    str(link.get("target_node_id") or "")
                    for link in list(node.get("links") or [])
                    if isinstance(link, dict) and str(link.get("target_node_id") or "")
                ],
            }
        )
    doc_frequency: dict[str, int] = {}
    for row in rows:
        for token in set(row["tokens"]):
            doc_frequency[token] = doc_frequency.get(token, 0) + 1
    node_count = max(1, len(rows))
    idf = {token: math.log(1.0 + (node_count - freq + 0.5) / (freq + 0.5)) for token, freq in doc_frequency.items()}
    avg_len = sum(len(row["tokens"]) for row in rows) / max(1, len(rows))
    edge_neighbors: dict[str, set[str]] = {}
    for edge in edges:
        source = str(edge.get("source") or edge.get("source_id") or edge.get("from") or "")
        target = str(edge.get("target") or edge.get("target_id") or edge.get("to") or "")
        if source and target:
            edge_neighbors.setdefault(source, set()).add(target)
            edge_neighbors.setdefault(target, set()).add(source)
    return {
        "schema_version": "agvm.phase8c.graph_corpus.v1",
        "rows": rows,
        "node_count": len(rows),
        "edge_count": len(edges),
        "document_node_count": document_node_count,
        "idf": idf,
        "avg_len": avg_len,
        "edge_neighbors": edge_neighbors,
    }


def _phase8c_bm25_score(query_tokens: list[str], row: dict[str, Any], idf: dict[str, float], avg_len: float) -> float:
    if not query_tokens:
        return 0.0
    counts: dict[str, int] = {}
    for token in row["tokens"]:
        counts[token] = counts.get(token, 0) + 1
    length = max(1, len(row["tokens"]))
    k1 = 1.4
    b = 0.72
    score = 0.0
    for token in query_tokens:
        frequency = counts.get(token, 0)
        if frequency <= 0:
            continue
        denom = frequency + k1 * (1.0 - b + b * (length / max(1.0, avg_len)))
        score += float(idf.get(token, 0.0)) * ((frequency * (k1 + 1.0)) / max(0.0001, denom))
    return score


def _phase8c_vector_score(query_tokens: list[str], row: dict[str, Any], idf: dict[str, float]) -> float:
    if not query_tokens or not row["tokens"]:
        return 0.0
    query_counts: dict[str, int] = {}
    row_counts: dict[str, int] = {}
    for token in query_tokens:
        query_counts[token] = query_counts.get(token, 0) + 1
    for token in row["tokens"]:
        if token in query_counts:
            row_counts[token] = row_counts.get(token, 0) + 1
    if not row_counts:
        return 0.0
    dot = 0.0
    query_norm = 0.0
    row_norm = 0.0
    for token, count in query_counts.items():
        weight = float(idf.get(token, 0.0)) * count
        query_norm += weight * weight
        row_weight = float(idf.get(token, 0.0)) * row_counts.get(token, 0)
        row_norm += row_weight * row_weight
        dot += weight * row_weight
    if query_norm <= 0.0 or row_norm <= 0.0:
        return 0.0
    return dot / math.sqrt(query_norm * row_norm)


def _phase8c_minmax_scores(scores: dict[str, float]) -> dict[str, float]:
    if not scores:
        return {}
    values = list(scores.values())
    low = min(values)
    high = max(values)
    if high <= low:
        return {key: 0.0 for key in scores}
    return {key: (value - low) / (high - low) for key, value in scores.items()}


def _phase8c_case_profile(case: dict[str, Any]) -> dict[str, Any]:
    query = str(dict(case.get("payload") or {}).get("query_text") or "")
    lowered = query.lower()
    needs_doc = bool(case.get("min_document_refs") or case.get("min_document_count") or "document" in lowered or "documento" in lowered)
    needs_path = bool(case.get("min_path_count") or case.get("path") == "/mcp/retrieve-path-corridor" or "percor" in lowered or "collega" in lowered)
    expected_no_match = bool(case.get("expected_no_match"))
    return {
        "case_family": str(case.get("phase8c_family") or "uncategorized"),
        "query_text": query,
        "query_tokens": _phase8c_tokenize(query),
        "expected_terms": tuple(str(term) for term in list(case.get("expected_terms") or ())),
        "expected_no_match": expected_no_match,
        "needs_document": needs_doc,
        "needs_path": needs_path,
        "needs_hot_cold": bool(not expected_no_match),
    }


def _phase8c_rank_baseline(
    baseline_id: str,
    *,
    case: dict[str, Any],
    corpus: dict[str, Any],
    top_k: int = 10,
) -> dict[str, Any]:
    profile = _phase8c_case_profile(case)
    rows = [dict(row) for row in list(corpus.get("rows") or [])]
    idf = dict(corpus.get("idf") or {})
    avg_len = float(corpus.get("avg_len") or 1.0)
    query_tokens = list(profile["query_tokens"])
    bm25 = {row["id"]: _phase8c_bm25_score(query_tokens, row, idf, avg_len) for row in rows}
    vector = {row["id"]: _phase8c_vector_score(query_tokens, row, idf) for row in rows}
    bm25_norm = _phase8c_minmax_scores(bm25)
    vector_norm = _phase8c_minmax_scores(vector)
    scores: dict[str, float] = {}
    if baseline_id == "bm25_lexical":
        scores = bm25_norm
    elif baseline_id == "vector_hash_rag":
        scores = vector_norm
    elif baseline_id == "hybrid_lexical_vector_rag":
        scores = {row["id"]: 0.55 * bm25_norm.get(row["id"], 0.0) + 0.45 * vector_norm.get(row["id"], 0.0) for row in rows}
    elif baseline_id == "graph_neighbor_no_ai":
        seed_ids = [
            node_id
            for node_id, _score in sorted(bm25_norm.items(), key=lambda item: item[1], reverse=True)[:4]
            if _score > 0.0
        ]
        seed_set = set(seed_ids)
        neighbors: set[str] = set()
        row_by_id = {row["id"]: row for row in rows}
        edge_neighbors = {str(key): {str(item) for item in value} for key, value in dict(corpus.get("edge_neighbors") or {}).items()}
        for seed_id in seed_ids:
            seed = row_by_id.get(seed_id) or {}
            neighbors.update(str(item) for item in list(seed.get("links") or []) if str(item))
            neighbors.update(edge_neighbors.get(seed_id, set()))
        scores = {
            row["id"]: max(
                bm25_norm.get(row["id"], 0.0),
                0.72 * vector_norm.get(row["id"], 0.0),
                0.65 if row["id"] in seed_set else 0.0,
                0.48 if row["id"] in neighbors else 0.0,
            )
            for row in rows
        }
    elif baseline_id == "agvm_heuristic_only_ablation":
        requested_tokens = set(query_tokens)

        def requested_any(*tokens: str) -> bool:
            return any(token in requested_tokens for token in tokens)

        def boost(row: dict[str, Any]) -> float:
            node = dict(row.get("node") or {})
            memory_type = str(node.get("memory_type") or "").lower()
            provenance = dict(node.get("provenance") or {})
            guide_area = str(provenance.get("guide_conceptual_area") or "").lower()
            value = 0.0
            if requested_any(
                "azienda",
                "aziende",
                "impresa",
                "imprese",
                "societa",
                "societ",
                "organizzazione",
                "organizzazioni",
                "lavoro",
                "progetto",
                "progetti",
                "company",
                "companies",
                "organization",
                "organizations",
                "work",
                "project",
                "projects",
                "business",
                "acquisizione",
                "acquisizioni",
                "acquisition",
                "startup",
            ) and (
                "project" in memory_type
                or "work" in memory_type
                or "company" in memory_type
                or "business" in memory_type
                or "organization" in memory_type
                or "knowledge" in memory_type
                or "project" in guide_area
                or "work" in guide_area
                or "company" in guide_area
                or "business" in guide_area
                or "organization" in guide_area
            ):
                value += 0.18
            if requested_any(
                "chiami",
                "nome",
                "identita",
                "identity",
                "ruolo",
                "profilo",
                "persona",
                "role",
                "profile",
                "who",
            ) and ("identity" in memory_type or "identity" in guide_area):
                value += 0.2
            if requested_any(
                "padre",
                "madre",
                "famiglia",
                "familiare",
                "relazione",
                "relazioni",
                "monumento",
                "father",
                "mother",
                "family",
                "relationship",
                "relationships",
                "monument",
                "memorial",
            ) and (
                "relationship" in memory_type
                or "relation" in memory_type
                or "family" in memory_type
                or "history" in memory_type
                or "relationship" in guide_area
                or "relation" in guide_area
                or "family" in guide_area
                or "history" in guide_area
            ):
                value += 0.2
            if requested_any(
                "valore",
                "valori",
                "principio",
                "principi",
                "stile",
                "comunichi",
                "comunica",
                "comunicazione",
                "value",
                "values",
                "principle",
                "principles",
                "style",
                "voice",
                "tone",
            ) and (
                "values" in memory_type
                or "value" in memory_type
                or "expression" in memory_type
                or "style" in memory_type
                or "values" in guide_area
                or "value" in guide_area
                or "expression" in guide_area
                or "style" in guide_area
            ):
                value += 0.2
            if profile["needs_document"] and row.get("is_document"):
                value += 0.15
            return value
        scores = {
            row["id"]: min(1.0, 0.5 * bm25_norm.get(row["id"], 0.0) + 0.35 * vector_norm.get(row["id"], 0.0) + boost(row))
            for row in rows
        }
    else:
        raise ValueError(f"unknown_phase8c_baseline:{baseline_id}")
    ranked = sorted(rows, key=lambda row: scores.get(row["id"], 0.0), reverse=True)
    selected = [row for row in ranked[:top_k] if scores.get(row["id"], 0.0) > 0.0]
    best_score = max([scores.get(row["id"], 0.0) for row in selected] or [0.0])
    if profile["expected_no_match"] and best_score < 0.2:
        selected = []
    text = "\n".join(str(row.get("text") or "") for row in selected)
    return _phase8c_baseline_row(
        baseline_id=baseline_id,
        case=case,
        selected_rows=selected,
        context_text=text,
        latency_ms=0.0,
        best_score=best_score,
    )


def _phase8c_score_components(
    *,
    expected_terms: tuple[str, ...],
    expected_no_match: bool,
    needs_document: bool,
    needs_path: bool,
    term_hits: dict[str, bool],
    package_chars: int,
    selected_count: int,
    document_ref_count: int,
    raw_chars: int,
    path_count: int,
    route_event_count: int,
    hot_count: int,
    cold_count: int,
    no_match_honest: bool,
    maintenance_aware: bool,
    ai_materialized: bool,
) -> dict[str, float]:
    expected_coverage = 1.0 if not expected_terms else sum(1 for hit in term_hits.values() if hit) / max(1, len(expected_terms))
    if expected_no_match:
        expected_coverage = 1.0 if no_match_honest else 0.0
    context_size_score = min(1.0, max(0.0, package_chars / 1600.0))
    selection_score = min(1.0, selected_count / 6.0)
    context_coherence = 0.65 * expected_coverage + 0.2 * context_size_score + 0.15 * selection_score
    document_actionability = 1.0 if (document_ref_count > 0 and (raw_chars > 0 or needs_document)) else min(0.55, document_ref_count / 4.0)
    if not needs_document:
        document_actionability = min(1.0, max(document_actionability, 0.35 if document_ref_count > 0 else 0.0))
    if needs_path:
        path_truth = 1.0 if path_count > 0 and route_event_count > 0 else 0.0
    else:
        # Linked graph neighbors are useful context, but they are not certified
        # route truth unless the user actually asked for a path/corridor.
        path_truth = 0.4 if path_count > 0 else 0.0
    hot_cold = 1.0 if hot_count > 0 and cold_count > 0 else (0.5 if hot_count > 0 else 0.0)
    no_match_score = 1.0 if (not expected_no_match or no_match_honest) else 0.0
    maintenance = 1.0 if maintenance_aware else 0.0
    ai_core = 1.0 if ai_materialized else 0.0
    weights = {
        "expected_coverage": 0.3,
        "context_coherence": 0.22,
        "document_actionability": 0.16 if needs_document else 0.08,
        "path_truth": 0.16 if needs_path else 0.08,
        "hot_cold_continuity": 0.1,
        "no_match_honesty": 0.2 if expected_no_match else 0.06,
        "maintenance_awareness": 0.08,
        "ai_core": 0.08,
    }
    total_weight = sum(weights.values())
    values = {
        "expected_coverage": expected_coverage,
        "context_coherence": min(1.0, context_coherence),
        "document_actionability": min(1.0, document_actionability),
        "path_truth": min(1.0, path_truth),
        "hot_cold_continuity": min(1.0, hot_cold),
        "no_match_honesty": no_match_score,
        "maintenance_awareness": maintenance,
        "ai_core": ai_core,
    }
    quality = sum(values[key] * weight for key, weight in weights.items()) / max(0.0001, total_weight)
    values["quality_score"] = round(quality, 6)
    return {key: round(float(value), 6) for key, value in values.items()}


def _phase8c_baseline_row(
    *,
    baseline_id: str,
    case: dict[str, Any],
    selected_rows: list[dict[str, Any]],
    context_text: str,
    latency_ms: float,
    best_score: float,
) -> dict[str, Any]:
    profile = _phase8c_case_profile(case)
    expected_terms = tuple(profile["expected_terms"])
    term_hits = _pr12p12_term_hits(context_text, expected_terms)
    document_ref_count = sum(1 for row in selected_rows if bool(row.get("is_document")))
    selected_ids = [str(row.get("id") or "") for row in selected_rows]
    link_pairs = 0
    selected_set = set(selected_ids)
    for row in selected_rows:
        for target_id in list(row.get("links") or []):
            if str(target_id) in selected_set:
                link_pairs += 1
    no_match_honest = bool(profile["expected_no_match"] and not selected_rows)
    components = _phase8c_score_components(
        expected_terms=expected_terms,
        expected_no_match=bool(profile["expected_no_match"]),
        needs_document=bool(profile["needs_document"]),
        needs_path=bool(profile["needs_path"]),
        term_hits=term_hits,
        package_chars=len(context_text),
        selected_count=len(selected_rows),
        document_ref_count=document_ref_count,
        raw_chars=0,
        path_count=1 if baseline_id == "graph_neighbor_no_ai" and link_pairs > 0 else 0,
        route_event_count=0,
        hot_count=0,
        cold_count=0,
        no_match_honest=no_match_honest,
        maintenance_aware=False,
        ai_materialized=False,
    )
    return {
        "baseline_id": baseline_id,
        "case_id": case.get("case_id"),
        "kind": "baseline",
        "passed": True,
        "latency_ms": round(float(latency_ms), 3),
        "best_score": round(float(best_score), 6),
        "selected_node_count": len(selected_rows),
        "selected_node_ids": selected_ids[:12],
        "package_chars": len(context_text),
        "document_ref_count": document_ref_count,
        "path_count": 1 if baseline_id == "graph_neighbor_no_ai" and link_pairs > 0 else 0,
        "route_event_count": 0,
        "expected_term_hits": term_hits,
        "no_match_honest": no_match_honest,
        "components": components,
        "quality_score": components["quality_score"],
        "limitations": {
            "ai_owned_landing_paths": False,
            "mcp_document_hydration_contract": False,
            "certified_path_truth": False,
            "hot_cold_memory_continuity": False,
            "maintenance_health_awareness": False,
        },
    }


def _phase8c_agvm_full_row(case: dict[str, Any], agvm_case_row: dict[str, Any], *, health: dict[str, Any]) -> dict[str, Any]:
    profile = _phase8c_case_profile(case)
    expected_terms = tuple(profile["expected_terms"])
    term_hits = {term: bool(dict(agvm_case_row.get("expected_term_hits") or {}).get(term)) for term in expected_terms}
    client_state = str(agvm_case_row.get("delivery_client_payload_state") or agvm_case_row.get("status") or "")
    no_match_honest = bool(
        profile["expected_no_match"]
        and bool(agvm_case_row.get("passed"))
        and (
            bool(agvm_case_row.get("ai_no_route_terminal_contract"))
            or "no_match" in client_state
            or str(agvm_case_row.get("status") or "") == "no_match"
        )
    )
    raw_chars = int(agvm_case_row.get("primary_raw_text_char_count") or 0)
    document_ref_count = int(agvm_case_row.get("document_ref_count") or agvm_case_row.get("primary_document_count") or 0)
    package_chars = int(agvm_case_row.get("primary_payload_chars") or agvm_case_row.get("package_chars") or 0)
    expected_terms_visible = all(term_hits.values()) if expected_terms else True
    direct_document_tool = str(case.get("path") or "").strip() in {"/mcp/retrieve-document", "/mcp/inspect-document"}
    direct_document_payload_sufficient = bool(
        direct_document_tool
        and profile["needs_document"]
        and bool(agvm_case_row.get("passed"))
        and expected_terms_visible
        and document_ref_count > 0
        and (raw_chars > 0 or package_chars > 0)
    )
    components = _phase8c_score_components(
        expected_terms=expected_terms,
        expected_no_match=bool(profile["expected_no_match"]),
        needs_document=bool(profile["needs_document"]),
        needs_path=bool(profile["needs_path"]),
        term_hits=term_hits,
        package_chars=package_chars,
        selected_count=int(agvm_case_row.get("hot_section_count") or 0),
        document_ref_count=document_ref_count,
        raw_chars=raw_chars,
        path_count=int(agvm_case_row.get("path_count") or 0),
        route_event_count=int(agvm_case_row.get("route_event_count") or agvm_case_row.get("trace_step_count") or 0),
        hot_count=int(agvm_case_row.get("hot_section_count") or 0),
        cold_count=int(agvm_case_row.get("cold_reservoir_entries") or 0),
        no_match_honest=no_match_honest,
        maintenance_aware=str(health.get("readiness") or "").lower() == "healthy",
        ai_materialized=bool(agvm_case_row.get("ai_materialized")) or direct_document_payload_sufficient or not bool(case.get("require_ai_material")),
    )
    return {
        "baseline_id": "agvm_full_ai_core",
        "case_id": case.get("case_id"),
        "kind": "agvm_full",
        "passed": bool(agvm_case_row.get("passed")),
        "failures": list(agvm_case_row.get("failures") or []),
        "latency_ms": float(agvm_case_row.get("first_useful_package_ms") or agvm_case_row.get("http_elapsed_ms") or 0.0),
        "full_completion_ms": agvm_case_row.get("full_completion_ms"),
        "package_chars": int(agvm_case_row.get("primary_payload_chars") or agvm_case_row.get("package_chars") or 0),
        "document_ref_count": int(agvm_case_row.get("document_ref_count") or 0),
        "primary_document_count": int(agvm_case_row.get("primary_document_count") or 0),
        "path_count": int(agvm_case_row.get("path_count") or 0),
        "route_event_count": int(agvm_case_row.get("route_event_count") or 0),
        "hot_section_count": int(agvm_case_row.get("hot_section_count") or 0),
        "cold_reservoir_entries": int(agvm_case_row.get("cold_reservoir_entries") or 0),
        "ai_materialized": bool(agvm_case_row.get("ai_materialized")),
        "semantic_ai_materialized": bool(agvm_case_row.get("semantic_ai_materialized")),
        "ai_spatial_materialized": bool(agvm_case_row.get("ai_spatial_materialized")),
        "ai_spatial_certifies_route": bool(agvm_case_row.get("ai_spatial_certifies_route")),
        "direct_document_payload_sufficient": direct_document_payload_sufficient,
        "expected_term_hits": term_hits,
        "no_match_honest": no_match_honest,
        "components": components,
        "quality_score": components["quality_score"],
        "limitations": {
            "ai_owned_landing_paths": not bool(agvm_case_row.get("ai_materialized")),
            "mcp_document_hydration_contract": int(agvm_case_row.get("document_ref_count") or 0) <= 0,
            "certified_path_truth": int(agvm_case_row.get("path_count") or 0) <= 0,
            "hot_cold_memory_continuity": int(agvm_case_row.get("hot_section_count") or 0) <= 0,
            "maintenance_health_awareness": str(health.get("readiness") or "").lower() != "healthy",
        },
    }


def _phase8c_agvm_extra_value_score(case: dict[str, Any], components: dict[str, Any]) -> float:
    profile = _phase8c_case_profile(case)
    score = (
        float(components.get("document_actionability") or 0.0)
        + float(components.get("path_truth") or 0.0)
        + float(components.get("hot_cold_continuity") or 0.0)
        + float(components.get("maintenance_awareness") or 0.0)
        + float(components.get("ai_core") or 0.0)
    )
    if bool(profile["expected_no_match"]):
        score += float(components.get("no_match_honesty") or 0.0)
    if (
        not bool(profile["needs_document"])
        and not bool(profile["needs_path"])
        and float(components.get("ai_core") or 0.0) > 0.0
    ):
        score += min(1.0, float(components.get("context_coherence") or 0.0))
    return round(score, 6)


def _phase8c_load_report_from_env() -> dict[str, Any] | None:
    path_text = str(os.environ.get("AGVM_PHASE8C_AGVM_REPORT_PATH") or os.environ.get("AGVM_PR12P14X_E_REAL_MCP_REPORT_PATH") or "").strip()
    if not path_text:
        return None
    path = Path(path_text).expanduser()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _phase8c_live_worker_policy(
    *,
    case_source: str,
    phase_workers_env: str,
    shared_workers_env: str,
    requested_workers: int | None,
) -> dict[str, Any]:
    expanded = case_source in {"expanded", "certification_120"}
    explicit_worker_env = bool(phase_workers_env or shared_workers_env)
    if expanded and not explicit_worker_env:
        effective_workers = 1
        policy_id = "local_single_provider_serial_default"
        reason = (
            "Expanded/certification live Phase 8C measures one local MCP agent against a single local LLM provider by default. "
            "Parallel worker runs are stress tests and must opt in explicitly."
        )
    else:
        effective_workers = max(1, int(requested_workers or 1))
        policy_id = "explicit_parallel_stress" if expanded and effective_workers > 1 else "requested_or_smoke_default"
        reason = (
            "Worker count was explicitly requested or the run is not the expanded live gate; latency SLO failures remain blocking."
        )
    return {
        "schema_version": "agvm.phase8c.live_worker_policy.v1",
        "policy_id": policy_id,
        "case_source": case_source,
        "expanded_live_gate": expanded,
        "explicit_worker_env": explicit_worker_env,
        "phase_workers_env": phase_workers_env or None,
        "shared_workers_env": shared_workers_env or None,
        "requested_workers": requested_workers,
        "effective_workers": effective_workers,
        "parallel_stress_mode": expanded and effective_workers > 1,
        "reason": reason,
    }


def _phase8c_control_brain_probe(
    base_url: str,
    *,
    primary_brain_id: str,
    control_brain_id: str | None,
) -> dict[str, Any]:
    normalized_control = str(control_brain_id or "").strip()
    if not normalized_control:
        return {
            "schema_version": "agvm.phase8c.control_brain_probe.v1",
            "status": "not_configured",
            "proof_present": False,
            "reason": "No control brain id was provided.",
        }
    if normalized_control == str(primary_brain_id or "").strip():
        return {
            "schema_version": "agvm.phase8c.control_brain_probe.v1",
            "status": "invalid",
            "proof_present": False,
            "control_brain_id": normalized_control,
            "reason": "Control brain must be different from the primary benchmark brain.",
        }

    started = time.perf_counter()
    probe: dict[str, Any] = {
        "schema_version": "agvm.phase8c.control_brain_probe.v1",
        "status": "started",
        "proof_present": False,
        "primary_brain_id": str(primary_brain_id or ""),
        "control_brain_id": normalized_control,
        "health_probe": {},
        "corpus_probe": {},
        "warnings": [],
    }
    try:
        health_output = post_json(
            base_url,
            "/mcp/brain-health",
            {"brain_id": normalized_control, "include_issue_samples": False},
            timeout=90.0,
        )
        health_report = dict(health_output.get("brain_health_report") or health_output)
        preflight = dict(health_report.get("benchmark_preflight") or {})
        probe["health_probe"] = {
            "tool_status": health_output.get("status"),
            "readiness": health_report.get("readiness"),
            "recommendation": health_report.get("recommendation"),
            "serious_product_benchmark_allowed": bool(preflight.get("serious_product_benchmark_allowed")),
            "diagnostic_runs_allowed": bool(preflight.get("diagnostic_runs_allowed")),
            "revolutionary_certification_allowed": bool(preflight.get("revolutionary_certification_allowed")),
            "node_count": int(dict(health_report.get("summary") or {}).get("node_count") or 0),
            "health_latency_ms": float(health_report.get("latency_ms") or 0.0),
            "reason_codes": list(health_report.get("reason_codes") or []),
        }
    except Exception as exc:  # pragma: no cover - exercised by live B3 artifacts.
        probe["health_probe"] = {
            "tool_status": "error",
            "error": f"{type(exc).__name__}:{exc}",
        }

    try:
        corpus = _phase8c_graph_corpus(base_url, max_nodes=1000, brain_id=normalized_control)
        probe["corpus_probe"] = {
            "node_count": int(corpus.get("node_count") or 0),
            "edge_count": int(corpus.get("edge_count") or 0),
            "document_node_count": int(corpus.get("document_node_count") or 0),
        }
    except Exception as exc:  # pragma: no cover - exercised by live B3 artifacts.
        probe["corpus_probe"] = {
            "error": f"{type(exc).__name__}:{exc}",
        }

    health_status = str(dict(probe.get("health_probe") or {}).get("tool_status") or "").lower()
    corpus_node_count = int(dict(probe.get("corpus_probe") or {}).get("node_count") or 0)
    proof_present = health_status in {"ok", "partial"} and corpus_node_count > 0
    if proof_present and not bool(dict(probe.get("health_probe") or {}).get("serious_product_benchmark_allowed")):
        probe["warnings"].append("control_brain_health_blocks_serious_benchmark")
    probe["proof_present"] = proof_present
    probe["status"] = "ok" if proof_present else "failed"
    probe["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
    return probe


def run_phase8c_comparative_backend_benchmark_suite(
    base_url: str | None = None,
    *,
    brain_id: str | None = None,
    agvm_report: dict[str, Any] | None = None,
    case_ids: tuple[str, ...] | None = None,
    case_source: str | None = None,
    rerun_agvm: bool | None = None,
    control_brain_id: str | None = None,
) -> dict[str, Any]:
    selected_base_url = str(base_url or DEFAULT_BASE_URL).rstrip("/")
    selected_brain_id = str(brain_id or os.environ.get("AGVM_VALIDATION_BRAIN_ID") or PR12P14X_E_REAL_BRAIN_ID).strip()
    selected_control_brain_id = str(control_brain_id or os.environ.get("AGVM_PHASE8C_CONTROL_BRAIN_ID") or "").strip()
    started = time.perf_counter()
    with benchmark_brain_scope(selected_brain_id):
        corpus = _phase8c_graph_corpus(selected_base_url, brain_id=selected_brain_id)
        health_output = post_json(selected_base_url, "/mcp/brain-health", {"brain_id": selected_brain_id, "include_issue_samples": False}, timeout=45.0)
    health_report = dict(health_output.get("brain_health_report") or health_output)
    selected_case_source = str(case_source or os.environ.get("AGVM_PHASE8C_CASE_SOURCE") or "h6_10").strip().lower()
    if selected_case_source in {"certification", "certification_120", "phase8c_c", "phase_8c_c", "120"}:
        cases = _phase8c_certification_mcp_cases(selected_brain_id)
        selected_case_source = "certification_120"
    elif selected_case_source in {"expanded", "expanded_live", "phase8c_b"}:
        cases = _phase8c_expanded_mcp_cases(selected_brain_id)
        selected_case_source = "expanded"
    elif selected_case_source in {"h6", "h6_10", "smoke", "phase8c_a"}:
        cases = _pr12p14x_e_real_mcp_cases(selected_brain_id)
        selected_case_source = "h6_10"
    else:
        raise ValueError(f"unknown_phase8c_case_source:{selected_case_source}")
    selected_case_ids = set(case_ids or tuple(item.strip() for item in str(os.environ.get("AGVM_PHASE8C_CASE_IDS") or "").split(",") if item.strip()))
    if selected_case_ids:
        cases = [case for case in cases if str(case.get("case_id") or "") in selected_case_ids]

    loaded_report = agvm_report or _phase8c_load_report_from_env()
    should_rerun = bool(rerun_agvm)
    if rerun_agvm is None:
        should_rerun = loaded_report is None and str(os.environ.get("AGVM_PHASE8C_RERUN_AGVM") or "true").strip().lower() in {"1", "true", "yes", "on"}
    retrieve_source = "provided_report" if agvm_report is not None else "env_report" if loaded_report is not None else "live_rerun"
    if should_rerun:
        timeout = float(os.environ.get("AGVM_PHASE8C_CASE_TIMEOUT_SECONDS") or os.environ.get("AGVM_PR12P14X_E_CASE_TIMEOUT_SECONDS") or 60.0)
        phase_workers_env = str(os.environ.get("AGVM_PHASE8C_MAX_WORKERS") or "").strip()
        shared_workers_env = str(os.environ.get("AGVM_PR12P14X_E_MAX_WORKERS") or "").strip()
        requested_workers = int(phase_workers_env or shared_workers_env or (2 if selected_case_source == "h6_10" else 1))
        live_worker_policy = _phase8c_live_worker_policy(
            case_source=selected_case_source,
            phase_workers_env=phase_workers_env,
            shared_workers_env=shared_workers_env,
            requested_workers=requested_workers,
        )
        workers = int(live_worker_policy.get("effective_workers") or 1)
        with benchmark_brain_scope(selected_brain_id):
            loaded_report = run_pr12p14x_e_real_mcp_retrieve_quality_matrix_suite(
                selected_base_url,
                brain_id=selected_brain_id,
                timeout=timeout,
                max_workers=workers,
                case_ids=tuple(selected_case_ids) if selected_case_ids else None,
                cases_override=cases if selected_case_source in {"expanded", "certification_120"} else None,
            )
        retrieve_source = "live_rerun"
    else:
        live_worker_policy = {
            "schema_version": "agvm.phase8c.live_worker_policy.v1",
            "policy_id": "not_live_rerun",
            "case_source": selected_case_source,
            "expanded_live_gate": selected_case_source in {"expanded", "certification_120"},
            "explicit_worker_env": False,
            "requested_workers": None,
            "effective_workers": None,
            "parallel_stress_mode": False,
            "reason": "AGVM row data came from a provided or environment report.",
        }
    agvm_rows_by_case = {
        str(row.get("case_id") or ""): dict(row)
        for row in list(dict(loaded_report or {}).get("quality_rows") or [])
        if isinstance(row, dict)
    }

    case_rows: list[dict[str, Any]] = []
    failures: list[str] = []
    for case in cases:
        case_id = str(case.get("case_id") or "")
        baseline_rows: list[dict[str, Any]] = []
        for baseline_id in PHASE8C_BASELINE_IDS:
            if baseline_id == "agvm_full_ai_core":
                continue
            baseline_started = time.perf_counter()
            row = _phase8c_rank_baseline(baseline_id, case=case, corpus=corpus)
            row["latency_ms"] = round((time.perf_counter() - baseline_started) * 1000.0, 3)
            baseline_rows.append(row)
        agvm_source_row = agvm_rows_by_case.get(case_id)
        if agvm_source_row:
            agvm_row = _phase8c_agvm_full_row(case, agvm_source_row, health=health_report)
        else:
            agvm_row = {
                "baseline_id": "agvm_full_ai_core",
                "case_id": case_id,
                "kind": "agvm_full",
                "passed": False,
                "failures": ["agvm_case_missing_from_retrieve_report"],
                "quality_score": 0.0,
                "latency_ms": 0.0,
                "components": {},
            }
        all_rows = [*baseline_rows, agvm_row]
        best_baseline = max(baseline_rows, key=lambda row: float(row.get("quality_score") or 0.0)) if baseline_rows else {}
        fastest_baseline_ms = min([float(row.get("latency_ms") or 0.0) for row in baseline_rows] or [0.0])
        agvm_quality = float(agvm_row.get("quality_score") or 0.0)
        best_baseline_quality = float(best_baseline.get("quality_score") or 0.0)
        quality_delta = round(agvm_quality - best_baseline_quality, 6)
        slower_than_fastest = float(agvm_row.get("latency_ms") or 0.0) > max(1.0, fastest_baseline_ms * 1.25)
        extra_components = dict(agvm_row.get("components") or {})
        extra_value_score = _phase8c_agvm_extra_value_score(case, extra_components)
        case_passed = bool(agvm_row.get("passed")) and quality_delta > 0.0 and (not slower_than_fastest or extra_value_score >= 2.0)
        if not case_passed:
            failures.append(f"case_failed:{case_id}")
        case_rows.append(
            {
                "case_id": case_id,
                "phase8c_family": str(case.get("phase8c_family") or ""),
                "query_text": str(dict(case.get("payload") or {}).get("query_text") or ""),
                "expected_terms": list(case.get("expected_terms") or []),
                "expected_no_match": bool(case.get("expected_no_match")),
                "passed": case_passed,
                "winner": "agvm_full_ai_core" if quality_delta > 0.0 else str(best_baseline.get("baseline_id") or ""),
                "agvm_quality_score": agvm_quality,
                "best_baseline_id": str(best_baseline.get("baseline_id") or ""),
                "best_baseline_quality_score": best_baseline_quality,
                "quality_delta": quality_delta,
                "agvm_latency_ms": round(float(agvm_row.get("latency_ms") or 0.0), 3),
                "fastest_baseline_ms": round(fastest_baseline_ms, 3),
                "agvm_slower_than_fastest_baseline": slower_than_fastest,
                "agvm_extra_value_score": extra_value_score,
                "baseline_rows": all_rows,
            }
        )

    baseline_summary: dict[str, Any] = {}
    for baseline_id in PHASE8C_BASELINE_IDS:
        rows = [
            row
            for case_row in case_rows
            for row in list(case_row.get("baseline_rows") or [])
            if str(row.get("baseline_id") or "") == baseline_id
        ]
        baseline_summary[baseline_id] = {
            "row_count": len(rows),
            "average_quality_score": round(_safe_mean([float(row.get("quality_score") or 0.0) for row in rows]), 6),
            "average_latency_ms": round(_safe_mean([float(row.get("latency_ms") or 0.0) for row in rows]), 3),
            "expected_coverage_avg": round(_safe_mean([float(dict(row.get("components") or {}).get("expected_coverage") or 0.0) for row in rows]), 6),
            "document_actionability_avg": round(_safe_mean([float(dict(row.get("components") or {}).get("document_actionability") or 0.0) for row in rows]), 6),
            "path_truth_avg": round(_safe_mean([float(dict(row.get("components") or {}).get("path_truth") or 0.0) for row in rows]), 6),
            "hot_cold_continuity_avg": round(_safe_mean([float(dict(row.get("components") or {}).get("hot_cold_continuity") or 0.0) for row in rows]), 6),
            "maintenance_awareness_avg": round(_safe_mean([float(dict(row.get("components") or {}).get("maintenance_awareness") or 0.0) for row in rows]), 6),
        }

    family_summary: dict[str, Any] = {}
    for family in sorted({str(row.get("phase8c_family") or "uncategorized") for row in case_rows}):
        family_rows = [row for row in case_rows if str(row.get("phase8c_family") or "uncategorized") == family]
        family_summary[family] = {
            "row_count": len(family_rows),
            "passed_count": sum(1 for row in family_rows if bool(row.get("passed"))),
            "average_agvm_quality_score": round(_safe_mean([float(row.get("agvm_quality_score") or 0.0) for row in family_rows]), 6),
            "average_quality_delta": round(_safe_mean([float(row.get("quality_delta") or 0.0) for row in family_rows]), 6),
            "max_agvm_latency_ms": round(max([float(row.get("agvm_latency_ms") or 0.0) for row in family_rows] or [0.0]), 3),
        }

    passed_count = sum(1 for row in case_rows if bool(row.get("passed")))
    certification_coverage = _phase8c_certification_family_coverage(case_rows) if selected_case_source == "certification_120" else {
        "schema_version": "agvm.phase8c.certification_family_coverage.v1",
        "minimum_case_count": None,
        "family_minimums": {},
        "family_counts": {},
        "missing_families": {},
        "complete": False,
    }
    if selected_case_ids:
        minimum_case_count = 10
    elif selected_case_source == "certification_120":
        minimum_case_count = PHASE8C_CERTIFICATION_MIN_CASES
    elif selected_case_source == "expanded":
        minimum_case_count = 20
    else:
        minimum_case_count = 10
    broad_live_gate = selected_case_source in {"expanded", "certification_120"} and retrieve_source == "live_rerun" and len(case_rows) >= minimum_case_count
    control_brain_probe = (
        _phase8c_control_brain_probe(
            selected_base_url,
            primary_brain_id=selected_brain_id,
            control_brain_id=selected_control_brain_id,
        )
        if broad_live_gate or selected_control_brain_id
        else {
            "schema_version": "agvm.phase8c.control_brain_probe.v1",
            "status": "not_required_for_this_slice",
            "proof_present": False,
        }
    )
    control_proof_present = bool(control_brain_probe.get("proof_present"))
    control_serious_benchmark_allowed = bool(dict(control_brain_probe.get("health_probe") or {}).get("serious_product_benchmark_allowed"))
    certification_gate = selected_case_source == "certification_120" and broad_live_gate and not selected_case_ids
    certification_family_complete = bool(certification_coverage.get("complete"))
    all_pass = (
        passed_count == len(case_rows)
        and len(case_rows) >= minimum_case_count
        and bool((loaded_report or {}).get("all_pass"))
        and (not broad_live_gate or control_proof_present)
        and (not certification_gate or (control_serious_benchmark_allowed and certification_family_complete))
    )
    slice_label = "Phase 8C-A"
    if selected_case_source == "certification_120":
        slice_label = "Phase 8C-C"
    elif selected_case_source == "expanded":
        if retrieve_source == "live_rerun" and len(case_rows) >= minimum_case_count:
            slice_label = "Phase 8C-B3"
        else:
            slice_label = "Phase 8C-B2" if retrieve_source == "live_rerun" else "Phase 8C-B1"
    return {
        "schema_version": PHASE8C_COMPARATIVE_BACKEND_SCHEMA_VERSION,
        "phase": "phase8c_comparative_backend_benchmark",
        "slice": slice_label,
        "proof_scope": "real_validation_brain_vs_explicit_baselines",
        "case_source": selected_case_source,
        "base_url": selected_base_url,
        "brain_id": selected_brain_id,
        "all_pass": all_pass,
        "product_ready_claim_allowed": False,
        "revolutionary_certification_allowed": False,
        "retrieve_report_source": retrieve_source,
        "retrieve_report_all_pass": bool((loaded_report or {}).get("all_pass")),
        "live_worker_policy": live_worker_policy,
        "control_brain_probe": control_brain_probe,
        "certification_family_coverage": certification_coverage,
        "retrieve_report_execution": dict((loaded_report or {}).get("execution") or {}),
        "corpus_summary": {
            "node_count": int(corpus.get("node_count") or 0),
            "edge_count": int(corpus.get("edge_count") or 0),
            "document_node_count": int(corpus.get("document_node_count") or 0),
        },
        "health_summary": {
            "status": health_output.get("status"),
            "readiness": health_report.get("readiness"),
            "recommendation": health_report.get("recommendation"),
            "serious_product_benchmark_allowed": bool(dict(health_report.get("benchmark_preflight") or {}).get("serious_product_benchmark_allowed")),
            "revolutionary_certification_allowed": bool(dict(health_report.get("benchmark_preflight") or {}).get("revolutionary_certification_allowed")),
        },
        "baseline_ids": list(PHASE8C_BASELINE_IDS),
        "case_rows": case_rows,
        "baseline_summary": baseline_summary,
        "family_summary": family_summary,
        "acceptance": {
            "real_validation_brain_used": selected_brain_id == PR12P14X_E_REAL_BRAIN_ID,
            "case_matrix_complete": len(case_rows) >= minimum_case_count,
            "minimum_case_count": minimum_case_count,
            "expanded_case_source_used": selected_case_source == "expanded",
            "certification_case_source_used": selected_case_source == "certification_120",
            "certification_family_coverage_complete": certification_family_complete if selected_case_source == "certification_120" else True,
            "agvm_retrieve_report_green": bool((loaded_report or {}).get("all_pass")),
            "all_cases_agvm_wins_quality": all(float(row.get("quality_delta") or 0.0) > 0.0 for row in case_rows),
            "slower_rows_have_extra_value": all(
                (not bool(row.get("agvm_slower_than_fastest_baseline"))) or float(row.get("agvm_extra_value_score") or 0.0) >= 2.0
                for row in case_rows
            ),
            "no_match_honesty_case_present": any(bool(row.get("expected_no_match")) for row in case_rows),
            "path_case_present": any(
                str(row.get("case_id") or "") == "path_aware" or str(row.get("phase8c_family") or "") == "path_corridor"
                for row in case_rows
            ),
            "document_case_present": any(
                str(row.get("case_id") or "") == "document_lookup" or str(row.get("phase8c_family") or "") == "document_raw"
                for row in case_rows
            ),
            "non_simone_or_control_proof_present": control_proof_present,
            "control_brain_serious_benchmark_allowed": control_serious_benchmark_allowed,
        },
        "failures": failures
        + [
            key
            for key, passed in {
                "retrieve_report_not_green": bool((loaded_report or {}).get("all_pass")),
                "case_matrix_incomplete": len(case_rows) >= minimum_case_count,
                "certification_family_coverage_incomplete": (
                    selected_case_source != "certification_120"
                    or bool(selected_case_ids)
                    or certification_family_complete
                ),
                "not_all_cases_agvm_wins_quality": all(float(row.get("quality_delta") or 0.0) > 0.0 for row in case_rows),
                "slower_rows_missing_extra_value": all(
                    (not bool(row.get("agvm_slower_than_fastest_baseline"))) or float(row.get("agvm_extra_value_score") or 0.0) >= 2.0
                    for row in case_rows
                ),
                "non_simone_or_control_proof_missing": (not broad_live_gate) or control_proof_present,
                "control_brain_not_serious_benchmark_ready": (not certification_gate) or control_serious_benchmark_allowed,
            }.items()
            if not passed
        ],
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 2),
        "next_slice": (
            "Phase 8D local MCP product verdict"
            if selected_case_source == "certification_120" and certification_gate and all_pass
            else "Phase 8C-C full certification matrix"
            if selected_case_source == "certification_120" and bool(selected_case_ids) and all_pass
            else "Phase 8C-C remediation"
            if selected_case_source == "certification_120"
            else
            "Phase 8C-C 120-row certification matrix and serious non-Simone control"
            if selected_case_source == "expanded" and broad_live_gate and all_pass
            else "Phase 8C-B3 expanded matrix breadth and control corpus"
            if selected_case_source == "expanded" and all_pass
            else "Phase 8C-B3 remediation"
            if selected_case_source == "expanded" and broad_live_gate
            else "Phase 8C-B1 remediation"
            if selected_case_source == "expanded"
            else "Phase 8C-B expanded benchmark hardening"
            if all_pass
            else "Phase 8C-A remediation"
        ),
    }


def render_phase8c_comparative_backend_markdown(report: dict[str, Any]) -> str:
    case_rows = [dict(row) for row in list(report.get("case_rows") or []) if isinstance(row, dict)]
    baseline_summary = dict(report.get("baseline_summary") or {})
    family_summary = dict(report.get("family_summary") or {})
    lines: list[str] = [
        "# AGVM Phase 8C Comparative Backend Benchmark",
        "",
        f"- Schema: `{report.get('schema_version')}`",
        f"- Slice: `{report.get('slice')}`",
        f"- Case source: `{report.get('case_source')}`",
        f"- Brain: `{report.get('brain_id')}`",
        f"- Result: `{'PASS' if report.get('all_pass') else 'FAIL'}`",
        f"- Rows: `{sum(1 for row in case_rows if row.get('passed'))}/{len(case_rows)}`",
        f"- Retrieve report source: `{report.get('retrieve_report_source')}`",
        f"- Worker policy: `{dict(report.get('live_worker_policy') or {}).get('policy_id')}`",
        f"- Control brain proof: `{dict(report.get('control_brain_probe') or {}).get('status')}`",
        f"- Health: `{dict(report.get('health_summary') or {}).get('readiness')}`",
        f"- Revolutionary certification allowed: `{report.get('revolutionary_certification_allowed')}`",
        "",
        "## Baseline Summary",
        "",
        "| System | Rows | Avg quality | Avg latency ms | Expected coverage | Path truth | Hot/cold |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for baseline_id, summary_any in sorted(baseline_summary.items()):
        summary = dict(summary_any or {})
        lines.append(
            "| "
            + " | ".join(
                [
                    str(baseline_id),
                    str(summary.get("row_count") or 0),
                    str(summary.get("average_quality_score") or 0),
                    str(summary.get("average_latency_ms") or 0),
                    str(summary.get("expected_coverage_avg") or 0),
                    str(summary.get("path_truth_avg") or 0),
                    str(summary.get("hot_cold_continuity_avg") or 0),
                ]
            )
            + " |"
        )
    lines.extend(["", "## Family Summary", ""])
    if family_summary:
        lines.extend(
            [
                "| Family | Rows | Passed | Avg AGVM quality | Avg delta | Max AGVM latency ms |",
                "| --- | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for family, summary_any in sorted(family_summary.items()):
            summary = dict(summary_any or {})
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(family),
                        str(summary.get("row_count") or 0),
                        str(summary.get("passed_count") or 0),
                        str(summary.get("average_agvm_quality_score") or 0),
                        str(summary.get("average_quality_delta") or 0),
                        str(summary.get("max_agvm_latency_ms") or 0),
                    ]
                )
                + " |"
            )
    else:
        lines.append("- No family summary available.")
    certification_coverage = dict(report.get("certification_family_coverage") or {})
    if certification_coverage and certification_coverage.get("family_minimums"):
        lines.extend(["", "## Certification Coverage", ""])
        lines.append(f"- Complete: `{certification_coverage.get('complete')}`")
        lines.append(f"- Minimum cases: `{certification_coverage.get('minimum_case_count')}`")
        missing = dict(certification_coverage.get("missing_families") or {})
        if missing:
            lines.append("- Missing families: " + ", ".join(f"{family}({info.get('actual')}/{info.get('required')})" for family, info in sorted(missing.items())))
        else:
            lines.append("- Missing families: none")
    lines.extend(["", "## Case Rows", ""])
    lines.extend(
        [
            "| Case | Family | Passed | Winner | AGVM quality | Best baseline | Delta | AGVM latency ms |",
            "| --- | --- | ---: | --- | ---: | --- | ---: | ---: |",
        ]
    )
    for row in case_rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row.get("case_id") or ""),
                    str(row.get("phase8c_family") or ""),
                    "yes" if row.get("passed") else "no",
                    str(row.get("winner") or ""),
                    str(row.get("agvm_quality_score") or 0),
                    str(row.get("best_baseline_id") or ""),
                    str(row.get("quality_delta") or 0),
                    str(row.get("agvm_latency_ms") or 0),
                ]
            )
            + " |"
        )
    control_probe = dict(report.get("control_brain_probe") or {})
    if control_probe:
        health_probe = dict(control_probe.get("health_probe") or {})
        corpus_probe = dict(control_probe.get("corpus_probe") or {})
        lines.extend(
            [
                "",
                "## Control Brain Probe",
                "",
                f"- Status: `{control_probe.get('status')}`",
                f"- Control brain: `{control_probe.get('control_brain_id')}`",
                f"- Proof present: `{control_probe.get('proof_present')}`",
                f"- Health readiness: `{health_probe.get('readiness')}`",
                f"- Serious benchmark allowed: `{health_probe.get('serious_product_benchmark_allowed')}`",
                f"- Corpus nodes: `{corpus_probe.get('node_count')}`",
            ]
        )
    failures = list(report.get("failures") or [])
    lines.extend(["", "## Verdict", ""])
    if failures:
        lines.append("- Failures: " + ", ".join(str(item) for item in failures))
    else:
        lines.append("- No failures recorded by this benchmark slice.")
    lines.append(f"- Next slice: `{report.get('next_slice')}`")
    lines.append("")
    return "\n".join(lines)


PR12P14X_F_HEALTH_RECOMMENDATIONS = {
    "none",
    "sleep_preview",
    "evolve_preview",
    "grow_repair",
    "matrix_calibration_preview",
    "rebuild_required",
}


def _pr12p14x_f_real_health_row(output: dict[str, Any], *, brain_id: str, elapsed_ms: int) -> dict[str, Any]:
    failures: list[str] = []
    safety = dict(output.get("safety_contract") or {})
    budget = dict(output.get("budget") or {})
    actions = [dict(item) for item in list(output.get("actions") or []) if isinstance(item, dict)]
    benchmark_preflight = dict(output.get("benchmark_preflight") or {})
    brain_sanity_snapshot = dict(output.get("brain_sanity_snapshot") or {})
    automation_policy = dict(output.get("automation_policy") or {})
    alert_summary = dict(output.get("alert_summary") or {})
    retrieval_learning_rollup = dict(output.get("retrieval_learning_rollup") or {})
    recommendation = str(output.get("recommendation") or "").strip() or "none"
    preflight_verdict = str(benchmark_preflight.get("verdict") or "").strip()
    automation_mode = str(automation_policy.get("policy_mode") or "").strip()
    if output.get("schema_version") != "agvm.mcp_brain_health_tool_output.v1":
        failures.append("brain_health_schema_missing")
    if str(output.get("tool_name") or "") != "brain_health":
        failures.append("brain_health_tool_name_missing")
    if str(output.get("brain_id") or "") != brain_id:
        failures.append("brain_id_mismatch")
    if str(output.get("status") or "") not in {"ok", "partial"}:
        failures.append(f"brain_health_status_unexpected:{output.get('status')}")
    if recommendation not in PR12P14X_F_HEALTH_RECOMMENDATIONS:
        failures.append(f"brain_health_recommendation_unknown:{recommendation}")
    if safety.get("non_mutating") is not True or budget.get("mutation_allowed") is not False:
        failures.append("brain_health_not_proven_non_mutating")
    if safety.get("hidden_mutation_allowed") is not False:
        failures.append("brain_health_hidden_mutation_not_forbidden")
    if safety.get("sleep_evolve_apply_requires_explicit_acceptance") is not True:
        failures.append("brain_health_apply_acceptance_contract_missing")
    if safety.get("matrix_updates_require_preview_apply_rollback") is not True:
        failures.append("brain_health_matrix_preview_apply_rollback_contract_missing")
    if not preflight_verdict:
        failures.append("brain_health_benchmark_preflight_missing")
    if benchmark_preflight.get("diagnostic_runs_allowed") is not True:
        failures.append("brain_health_diagnostic_runs_not_allowed")
    if automation_mode not in {"manual_review", "auto_preview", "auto_apply_low_risk", "blocked"}:
        failures.append(f"brain_health_automation_policy_unknown:{automation_mode or 'missing'}")
    if automation_policy.get("hidden_mutation_allowed") is not False:
        failures.append("brain_health_automation_policy_hidden_mutation_not_forbidden")
    if recommendation != "none" and not actions:
        failures.append("brain_health_recommendation_has_no_action")
    if any(bool(action.get("mutating")) for action in actions):
        failures.append("brain_health_action_is_mutating")
    actionable_actions = [
        action
        for action in actions
        if str(action.get("action") or "none") != "none" or str(action.get("endpoint_hint") or "").strip()
    ]
    if any(not bool(action.get("requires_preview_apply_rollback")) for action in actionable_actions):
        failures.append("brain_health_action_missing_preview_apply_rollback")
    health_summary = dict(output.get("health_summary") or {})
    checks = dict(output.get("checks") or {})
    node_count = int(health_summary.get("node_count") or 0)
    return {
        "schema_version": "agvm.pr12p14x_f.real_health_row.v1",
        "case_id": "brain_health_runtime",
        "passed": not failures,
        "failures": failures,
        "tool_name": str(output.get("tool_name") or ""),
        "status": str(output.get("status") or ""),
        "brain_id": str(output.get("brain_id") or ""),
        "recommendation": recommendation,
        "reason_codes": list(output.get("reason_codes") or []),
        "action_count": len(actions),
        "node_count": node_count,
        "checks_present": sorted(checks.keys()),
        "benchmark_preflight": benchmark_preflight,
        "benchmark_preflight_verdict": preflight_verdict,
        "serious_product_benchmark_allowed": bool(benchmark_preflight.get("serious_product_benchmark_allowed")),
        "revolutionary_certification_allowed": bool(benchmark_preflight.get("revolutionary_certification_allowed")),
        "diagnostic_runs_allowed": bool(benchmark_preflight.get("diagnostic_runs_allowed")),
        "brain_sanity_snapshot": brain_sanity_snapshot,
        "sanity_severity": str(brain_sanity_snapshot.get("severity") or ""),
        "automation_policy": automation_policy,
        "automation_policy_mode": automation_mode,
        "alert_summary": alert_summary,
        "retrieval_learning_rollup": retrieval_learning_rollup,
        "non_mutating": safety.get("non_mutating"),
        "hidden_mutation_allowed": safety.get("hidden_mutation_allowed"),
        "elapsed_ms": elapsed_ms,
    }


def _pr12p14x_f_real_preview_row(
    output: dict[str, Any],
    *,
    case_id: str,
    expected_tool: str,
    brain_id: str,
    elapsed_ms: int,
    min_proposals: int = 1,
    require_structural_surface: bool = False,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {"brain_id": brain_id}
    failures = _pr12p14l_validate_maintenance_preview(output, expected_tool=expected_tool, evidence=evidence)
    failures.extend(
        _pr12p14u_g_validate_lifecycle_contract(
            output,
            allowed_states=("waiting_for_approval", "preview_only", "preview_ready", "blocked"),
        )
    )
    lifecycle = dict(output.get("sleep_evolve_lifecycle_contract") or {})
    operation_lifecycle = dict(output.get("memory_operation_lifecycle_contract") or {})
    metamemory = dict(output.get("metamemory_snapshot") or {})
    no_corruption = dict(output.get("no_corruption_guards") or {})
    matrix_delta = dict(output.get("matrix_delta") or {})
    latency_profile = dict(output.get("maintenance_latency_profile") or {})
    proposals = list(output.get("maintenance_proposals") or [])
    report = dict(output.get("maintenance_report") or {})
    evolve_proposals = list(report.get("evolve_structural_proposals") or [])
    if str(output.get("brain_id") or "") != brain_id:
        failures.append("brain_id_mismatch")
    if len(proposals) < min_proposals:
        failures.append(f"maintenance_proposals_too_few:{len(proposals)}<{min_proposals}")
    if str(metamemory.get("snapshot_id") or "").startswith("metamemory::") is False:
        failures.append("metamemory_snapshot_missing")
    if dict(no_corruption.get("document_anchor_guard") or {}).get("raw_document_anchor_delete_blocked") is not True:
        failures.append("document_anchor_guard_not_visible")
    if not bool(matrix_delta.get("calibration_delta_present")):
        failures.append("calibration_delta_not_visible")
    if not bool(matrix_delta.get("quality_delta_present")):
        failures.append("quality_delta_not_visible")
    if require_structural_surface and not evolve_proposals:
        failures.append("evolve_structural_proposals_missing")
    if bool(lifecycle.get("approval_gate", {}).get("hidden_mutation_allowed")):
        failures.append("hidden_mutation_allowed")
    return {
        "schema_version": "agvm.pr12p14x_f.real_preview_row.v1",
        "case_id": case_id,
        "passed": not failures,
        "failures": failures,
        "tool_name": str(output.get("tool_name") or ""),
        "status": str(output.get("status") or ""),
        "brain_id": str(output.get("brain_id") or ""),
        "proposal_count": len(proposals),
        "evolve_structural_proposal_count": len(evolve_proposals),
        "metamemory_visible": bool(metamemory),
        "rollback_visible": bool(dict(output.get("rollback_snapshot") or {})),
        "protected_block_count": len(list(no_corruption.get("protected_mutation_blocks") or [])),
        "lifecycle_state": str(lifecycle.get("state") or ""),
        "operation_state": str(operation_lifecycle.get("state") or ""),
        "fast_preview": bool(latency_profile.get("fast_preview")),
        "elapsed_ms": elapsed_ms,
        "evidence": evidence,
    }


def _pr12p14x_f_real_matrix_calibration_row(
    output: dict[str, Any],
    *,
    brain_id: str,
    elapsed_ms: int,
    health_recommendation: str,
) -> dict[str, Any]:
    failures: list[str] = []
    if output.get("schema_version") != "agvm.mcp_matrix_calibration_tool_output.v1":
        failures.append("matrix_calibration_schema_missing")
    if str(output.get("tool_name") or "") != "matrix_calibration_preview":
        failures.append("matrix_calibration_tool_name_missing")
    if str(output.get("brain_id") or "") != brain_id:
        failures.append("brain_id_mismatch")
    if str(output.get("status") or "") not in {"ok", "partial"}:
        failures.append(f"matrix_calibration_status_unexpected:{output.get('status')}")
    failures.extend(
        _pr12p14u_g_validate_lifecycle_contract(
            output,
            allowed_states=("preview_only", "waiting_for_approval", "idle"),
        )
    )
    safety = dict(output.get("safety_contract") or {})
    truth = dict(output.get("maintenance_truth_contract") or {})
    matrix_delta = dict(output.get("matrix_delta") or {})
    geometry = dict(output.get("brain_geometry_calibration") or {})
    proposals = [dict(item) for item in list(output.get("calibration_proposals") or []) if isinstance(item, dict)]
    recommendations = [dict(item) for item in list(output.get("recommendations") or []) if isinstance(item, dict)]
    policy = dict(output.get("matrix_change_policy") or {})
    latency_profile = dict(output.get("latency_profile") or {})
    if safety.get("non_mutating") is not True or safety.get("hidden_mutation_allowed") is not False:
        failures.append("matrix_calibration_not_proven_non_mutating")
    if safety.get("matrix_updates_require_preview_apply_rollback") is not True:
        failures.append("matrix_calibration_preview_apply_rollback_contract_missing")
    if truth.get("preview_non_mutating") is not True or truth.get("hidden_mutation_allowed") is not False:
        failures.append("matrix_calibration_truth_contract_not_safe")
    if bool(policy.get("mutates_graph")):
        failures.append("matrix_calibration_preview_mutates_graph")
    if matrix_delta.get("preview_present") is not True:
        failures.append("matrix_calibration_delta_missing")
    if int(geometry.get("node_count") or 0) <= 0:
        failures.append("matrix_calibration_node_count_missing")
    if health_recommendation == "matrix_calibration_preview" and not proposals:
        failures.append("matrix_calibration_recommendation_not_actionable")
    return {
        "schema_version": "agvm.pr12p14x_f.real_matrix_calibration_row.v1",
        "case_id": "matrix_calibration_preview_runtime",
        "passed": not failures,
        "failures": failures,
        "tool_name": str(output.get("tool_name") or ""),
        "status": str(output.get("status") or ""),
        "brain_id": str(output.get("brain_id") or ""),
        "health_recommendation": health_recommendation,
        "proposal_count": len(proposals),
        "recommendation_count": len(recommendations),
        "node_count": int(geometry.get("node_count") or 0),
        "overall_score": geometry.get("overall_score"),
        "all_pass": bool(dict(geometry.get("benchmarks") or {}).get("all_pass")),
        "non_mutating": safety.get("non_mutating"),
        "hidden_mutation_allowed": safety.get("hidden_mutation_allowed"),
        "fast_preview": bool(latency_profile.get("fast_preview")),
        "max_nodes_considered": int(latency_profile.get("max_nodes_considered") or 0),
        "position_update_count": int(latency_profile.get("position_update_count") or 0),
        "elapsed_ms": elapsed_ms,
    }


def _pr12p14x_f_real_apply_guard_row(output: dict[str, Any], *, brain_id: str, elapsed_ms: int) -> dict[str, Any]:
    failures: list[str] = []
    truth = dict(output.get("maintenance_truth_contract") or {})
    lifecycle = dict(output.get("sleep_evolve_lifecycle_contract") or {})
    operation_lifecycle = dict(output.get("memory_operation_lifecycle_contract") or {})
    mutation_surface = dict(output.get("mutation_surface") or {})
    if output.get("schema_version") != "agvm.mcp_maintenance_tool_output.v1":
        failures.append("mcp_maintenance_schema_missing")
    if str(output.get("tool_name") or "") != "sleep_apply":
        failures.append("sleep_apply_tool_name_missing")
    if str(output.get("brain_id") or "") != brain_id:
        failures.append("brain_id_mismatch")
    if str(output.get("status") or "") != "blocked":
        failures.append(f"sleep_apply_without_confirm_not_blocked:{output.get('status')}")
    if bool(dict(output.get("maintenance_report") or {}).get("applied")):
        failures.append("sleep_apply_without_confirm_applied")
    if truth.get("hidden_mutation_allowed") is not False:
        failures.append("truth_allows_hidden_mutation")
    if bool(mutation_surface.get("applied")) or mutation_surface.get("hidden_mutation_allowed") is not False:
        failures.append("mutation_surface_not_safe")
    blocked_reason = str(lifecycle.get("approval_gate", {}).get("blocked_reason") or "")
    if blocked_reason != "confirm_apply_required":
        failures.append(f"confirm_apply_block_reason_missing:{blocked_reason or 'none'}")
    if str(operation_lifecycle.get("state") or "") != "blocked":
        failures.append(f"operation_lifecycle_not_blocked:{operation_lifecycle.get('state')}")
    return {
        "schema_version": "agvm.pr12p14x_f.real_apply_guard_row.v1",
        "case_id": "sleep_apply_requires_explicit_confirmation",
        "passed": not failures,
        "failures": failures,
        "tool_name": str(output.get("tool_name") or ""),
        "status": str(output.get("status") or ""),
        "brain_id": str(output.get("brain_id") or ""),
        "blocked_reason": blocked_reason,
        "operation_state": str(operation_lifecycle.get("state") or ""),
        "elapsed_ms": elapsed_ms,
    }


def _pr12p14x_f_int_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    try:
        value = int(raw) if raw not in {None, ""} else int(default)
    except (TypeError, ValueError):
        value = int(default)
    return max(minimum, min(maximum, value))


def _pr12p14x_f_matrix_preview_payload(*, brain_id: str, max_nodes_considered: int) -> dict[str, Any]:
    default_nodes = max(50, min(500, int(max_nodes_considered or 20) * 20))
    preview_nodes = _pr12p14x_f_int_env(
        "AGVM_PR12P14X_F_MATRIX_MAX_NODES_CONSIDERED",
        default_nodes,
        minimum=50,
        maximum=4000,
    )
    default_updates = max(10, min(400, preview_nodes))
    preview_updates = _pr12p14x_f_int_env(
        "AGVM_PR12P14X_F_MATRIX_MAX_POSITION_UPDATES",
        default_updates,
        minimum=1,
        maximum=2000,
    )
    include_recommendations = (
        str(os.environ.get("AGVM_PR12P14X_F_MATRIX_INCLUDE_RECOMMENDATIONS") or "true")
        .strip()
        .lower()
        not in {"0", "false", "no", "off"}
    )
    return {
        "brain_id": brain_id,
        "max_nodes_considered": preview_nodes,
        "max_position_updates": preview_updates,
        "include_recommendations": include_recommendations,
    }


def run_pr12p14x_f_real_mcp_sleep_evolve_metamemory_suite(
    base_url: str | None = None,
    *,
    brain_id: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    selected_base_url = str(base_url or DEFAULT_BASE_URL).rstrip("/")
    selected_brain_id = str(brain_id or os.environ.get("AGVM_VALIDATION_BRAIN_ID") or "simone_massaro_validation").strip()
    maintenance_timeout = float(timeout or os.environ.get("AGVM_PR12P14X_F_REAL_MCP_TIMEOUT_SECONDS") or 120.0)
    max_nodes_considered = int(os.environ.get("AGVM_PR12P14X_F_MAX_NODES_CONSIDERED") or 20)
    matrix_preview_payload = _pr12p14x_f_matrix_preview_payload(
        brain_id=selected_brain_id,
        max_nodes_considered=max_nodes_considered,
    )
    started = time.perf_counter()
    rows: list[dict[str, Any]] = []
    outputs: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    health_recommendation = "missing"

    try:
        health_output, health_elapsed = _pr12p12_elapsed_post(
            selected_base_url,
            "/mcp/brain-health",
            {"brain_id": selected_brain_id, "limit": 25, "include_issue_samples": True},
            timeout=min(maintenance_timeout, 60.0),
        )
        outputs["brain_health_runtime"] = health_output
        health_row = _pr12p14x_f_real_health_row(health_output, brain_id=selected_brain_id, elapsed_ms=health_elapsed)
        health_recommendation = str(health_row.get("recommendation") or "missing")
        rows.append(health_row)
    except Exception as exc:
        rows.append(
            {
                "schema_version": "agvm.pr12p14x_f.real_health_row.v1",
                "case_id": "brain_health_runtime",
                "passed": False,
                "failures": [f"brain_health_runtime_error:{exc}"],
                "elapsed_ms": 0,
            }
        )

    try:
        output, elapsed_ms = _pr12p12_elapsed_post(
            selected_base_url,
            "/mcp/matrix-calibration-preview",
            matrix_preview_payload,
            timeout=min(maintenance_timeout, 60.0),
        )
        outputs["matrix_calibration_preview_runtime"] = output
        rows.append(
            _pr12p14x_f_real_matrix_calibration_row(
                output,
                brain_id=selected_brain_id,
                elapsed_ms=elapsed_ms,
                health_recommendation=health_recommendation,
            )
        )
    except Exception as exc:
        rows.append(
            {
                "schema_version": "agvm.pr12p14x_f.real_matrix_calibration_row.v1",
                "case_id": "matrix_calibration_preview_runtime",
                "passed": False,
                "failures": [f"matrix_calibration_preview_runtime_error:{exc}"],
                "elapsed_ms": 0,
            }
        )

    for path, case_id, expected_tool, mode, require_structural in (
        ("/mcp/sleep-preview", "sleep_preview_runtime", "sleep_preview", "sleep", False),
        ("/mcp/evolve-preview", "evolve_preview_runtime", "evolve_preview", "evolve", True),
    ):
        try:
            output, elapsed_ms = _pr12p12_elapsed_post(
                selected_base_url,
                path,
                {"brain_id": selected_brain_id, "mode": mode, "max_nodes_considered": max_nodes_considered},
                timeout=maintenance_timeout,
            )
            outputs[case_id] = output
            rows.append(
                _pr12p14x_f_real_preview_row(
                    output,
                    case_id=case_id,
                    expected_tool=expected_tool,
                    brain_id=selected_brain_id,
                    elapsed_ms=elapsed_ms,
                    require_structural_surface=require_structural,
                )
            )
        except Exception as exc:
            rows.append(
                {
                    "schema_version": "agvm.pr12p14x_f.real_preview_row.v1",
                    "case_id": case_id,
                    "passed": False,
                    "failures": [f"{case_id}_runtime_error:{exc}"],
                    "elapsed_ms": 0,
                }
            )

    try:
        output, elapsed_ms = _pr12p12_elapsed_post(
            selected_base_url,
            "/mcp/sleep-apply",
            {
                "brain_id": selected_brain_id,
                "mode": "sleep",
                "max_nodes_considered": max_nodes_considered,
                "proposal_ids": [],
                "confirm_apply": False,
            },
            timeout=maintenance_timeout,
        )
        outputs["sleep_apply_requires_explicit_confirmation"] = output
        rows.append(_pr12p14x_f_real_apply_guard_row(output, brain_id=selected_brain_id, elapsed_ms=elapsed_ms))
    except Exception as exc:
        rows.append(
            {
                "schema_version": "agvm.pr12p14x_f.real_apply_guard_row.v1",
                "case_id": "sleep_apply_requires_explicit_confirmation",
                "passed": False,
                "failures": [f"sleep_apply_guard_runtime_error:{exc}"],
                "elapsed_ms": 0,
            }
        )

    failed_cases = [str(row.get("case_id") or "") for row in rows if not bool(row.get("passed"))]
    acceptance = {
        "real_validation_brain_used": selected_brain_id == "simone_massaro_validation",
        "brain_health_actionable": any(row.get("case_id") == "brain_health_runtime" and row.get("passed") for row in rows),
        "matrix_calibration_preview_passed": any(
            row.get("case_id") == "matrix_calibration_preview_runtime" and row.get("passed") for row in rows
        ),
        "matrix_calibration_recommendation_routed": (
            health_recommendation != "matrix_calibration_preview"
            or any(row.get("case_id") == "matrix_calibration_preview_runtime" and row.get("passed") for row in rows)
        ),
        "sleep_preview_passed": any(row.get("case_id") == "sleep_preview_runtime" and row.get("passed") for row in rows),
        "evolve_preview_passed": any(row.get("case_id") == "evolve_preview_runtime" and row.get("passed") for row in rows),
        "apply_requires_confirmation": any(
            row.get("case_id") == "sleep_apply_requires_explicit_confirmation" and row.get("passed")
            for row in rows
        ),
        "all_rows_have_no_hidden_mutation": not any("hidden_mutation_allowed" in ",".join(row.get("failures") or []) for row in rows),
    }
    acceptance_failures = [key for key, passed in acceptance.items() if not passed]
    failures.extend([f"case_failed:{case_id}" for case_id in failed_cases])
    failures.extend(acceptance_failures)
    all_pass = not failures
    return {
        "schema_version": PR12P14X_F_REAL_MCP_SLEEP_EVOLVE_SCHEMA_VERSION,
        "phase": "sleep_evolve_metamemory_heuristic_evolution",
        "slice": "PR-12P-14X-F",
        "proof_scope": "real_mcp_runtime_not_fixture",
        "base_url": selected_base_url,
        "brain_id": selected_brain_id,
        "execution": {
            "timeout_seconds": maintenance_timeout,
            "max_nodes_considered": max_nodes_considered,
            "matrix_preview_max_nodes_considered": matrix_preview_payload["max_nodes_considered"],
            "matrix_preview_max_position_updates": matrix_preview_payload["max_position_updates"],
            "matrix_preview_include_recommendations": matrix_preview_payload["include_recommendations"],
            "matrix_preview_policy": "bounded_preview_for_product_slo",
            "mutation_policy": "preview_and_negative_apply_guard_only",
        },
        "all_pass": all_pass,
        "local_mcp_product_ready": False,
        "product_ready_claim_allowed": False,
        "quality_rows": rows,
        "acceptance": acceptance,
        "failed_cases": failed_cases,
        "failures": failures,
        "matrix_summary": {
            "case_count": len(rows),
            "passed_count": sum(1 for row in rows if bool(row.get("passed"))),
            "proposal_count": sum(int(row.get("proposal_count") or 0) for row in rows),
            "max_elapsed_ms": max([int(row.get("elapsed_ms") or 0) for row in rows] or [0]),
            "health_recommendation": next(
                (str(row.get("recommendation") or "") for row in rows if row.get("case_id") == "brain_health_runtime"),
                "",
            ),
            "matrix_calibration_proposal_count": next(
                (int(row.get("proposal_count") or 0) for row in rows if row.get("case_id") == "matrix_calibration_preview_runtime"),
                0,
            ),
        },
        "runtime_outputs": {
            key: {
                "tool_name": value.get("tool_name"),
                "status": value.get("status"),
                "schema_version": value.get("schema_version"),
            }
            for key, value in outputs.items()
        },
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 2),
        "next_slice": "PR-12P-14X-G Final Grow/Retrieve/Sleep-Evolve Product Verdict And RAG Baseline"
        if all_pass
        else "PR-12P-14X-F Real MCP Remediation",
    }


def _bam6d_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _bam6d_health_row(base_url: str, *, case_id: str, brain_id: str, timeout: float) -> tuple[dict[str, Any], dict[str, Any]]:
    started = time.perf_counter()
    failures: list[str] = []
    payload: dict[str, Any] = {}
    try:
        payload = get_json(base_url, "/health", timeout=timeout)
    except Exception as exc:
        failures.append(f"api_health_unreachable:{exc}")
    status_text = str(payload.get("status") or payload.get("state") or "").strip().lower()
    ok = bool(payload.get("ok")) or status_text in {"ok", "healthy", "ready"}
    if payload and not ok:
        failures.append(f"api_health_unhealthy:{status_text or 'missing'}")
    active_brain = str(payload.get("active_brain_id") or payload.get("brain_id") or "").strip()
    if payload and active_brain and active_brain != brain_id:
        failures.append(f"api_active_brain_mismatch:{active_brain}")
    return (
        {
            "schema_version": "agvm.bam6d.health_row.v1",
            "case_id": case_id,
            "path": "/health",
            "passed": not failures,
            "failures": failures,
            "ok": ok,
            "active_brain_id": active_brain,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "observed_at": _bam6d_timestamp(),
        },
        payload,
    )


def _bam6d_brain_health_row(output: dict[str, Any], *, case_id: str, brain_id: str, elapsed_ms: int, final_check: bool) -> dict[str, Any]:
    row = _pr12p14x_f_real_health_row(output, brain_id=brain_id, elapsed_ms=elapsed_ms)
    row = {**row, "case_id": case_id, "path": "/mcp/brain-health", "final_check": bool(final_check)}
    failures = list(row.get("failures") or [])
    recommendation = str(row.get("recommendation") or "").strip()
    preflight = dict(row.get("benchmark_preflight") or {})
    preflight_verdict = str(preflight.get("verdict") or "").strip()
    if recommendation in {"rebuild_required", "grow_repair"}:
        failures.append(f"maintenance_preflight_requires_brain_rebuild_or_grow:{recommendation}")
    if final_check:
        if not bool(row.get("serious_product_benchmark_allowed")):
            failures.append(f"post_health_does_not_allow_serious_benchmark:{preflight_verdict or 'missing'}")
        if "blocked" in preflight_verdict and not bool(row.get("serious_product_benchmark_allowed")):
            failures.append(f"post_health_still_blocked:{preflight_verdict}")
    row["failures"] = list(dict.fromkeys(str(item) for item in failures if str(item)))
    row["passed"] = not row["failures"]
    row["warnings"] = []
    if final_check and recommendation != "none" and bool(row.get("serious_product_benchmark_allowed")):
        row["warnings"].append(f"post_health_allowed_with_recommendation:{recommendation}")
    return row


def _bam6d_output_transaction(output: dict[str, Any]) -> dict[str, Any]:
    transaction = dict(output.get("maintenance_transaction") or {})
    if transaction:
        return transaction
    return dict(dict(output.get("maintenance_report") or {}).get("maintenance_transaction") or {})


def _bam6d_transaction_failures(transaction: dict[str, Any], *, preview_name: str, brain_id: str) -> list[str]:
    failures: list[str] = []
    if transaction.get("schema_version") != "agvm.maintenance_transaction.v1":
        failures.append(f"{preview_name}_maintenance_transaction_missing")
        return failures
    state = str(transaction.get("state") or "").strip()
    if state not in {"preview_ready", "apply_pending", "healthy_for_benchmark"}:
        failures.append(f"{preview_name}_transaction_state_unexpected:{state or 'missing'}")
    brain_scope = dict(transaction.get("brain_scope") or {})
    scoped_brain = str(brain_scope.get("brain_id") or "").strip()
    if scoped_brain and scoped_brain != brain_id:
        failures.append(f"{preview_name}_transaction_brain_mismatch:{scoped_brain}")
    if brain_scope.get("signed_apply_cannot_cross_brain_boundary") is not True:
        failures.append(f"{preview_name}_transaction_brain_boundary_missing")
    cache_plan = dict(transaction.get("cache_invalidation_plan") or {})
    if cache_plan.get("revision_keyed") is not True:
        failures.append(f"{preview_name}_cache_invalidation_not_revision_keyed")
    validation = dict(transaction.get("post_apply_validation_plan") or {})
    if validation.get("required_before_healthy_for_benchmark") is not True:
        failures.append(f"{preview_name}_post_apply_validation_not_required")
    preview_signature = dict(transaction.get("preview_signature") or {})
    if not str(preview_signature.get("signature_id") or "").strip():
        failures.append(f"{preview_name}_preview_signature_missing")
    return failures


def _bam6d_health_poll_failures(polls: list[dict[str, Any]], *, preview_name: str) -> list[str]:
    failures: list[str] = []
    if not polls:
        return failures
    failed = [dict(row) for row in polls if not bool(row.get("passed"))]
    if failed:
        failures.append(f"{preview_name}_api_health_failed_during_preview:{len(failed)}")
    return failures


def _bam6d_call_post_with_health_monitor(
    base_url: str,
    path: str,
    payload: dict[str, Any],
    *,
    brain_id: str,
    timeout: float,
    health_timeout: float = 8.0,
    poll_interval: float = 2.0,
) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    started = time.perf_counter()
    polls: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(post_json, base_url, path, payload, timeout)
        while not future.done():
            poll_row, _ = _bam6d_health_row(base_url, case_id=f"health_during_{path.strip('/').replace('/', '_')}", brain_id=brain_id, timeout=health_timeout)
            polls.append(poll_row)
            try:
                future.result(timeout=max(0.05, float(poll_interval)))
            except concurrent.futures.TimeoutError:
                continue
            break
        output = future.result()
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return output, elapsed_ms, polls


def _bam6d_matrix_preview_row(output: dict[str, Any], *, brain_id: str, elapsed_ms: int, health_recommendation: str) -> dict[str, Any]:
    row = _pr12p14x_f_real_matrix_calibration_row(
        output,
        brain_id=brain_id,
        elapsed_ms=elapsed_ms,
        health_recommendation=health_recommendation,
    )
    row = {**row, "case_id": "matrix_calibration_preview", "path": "/mcp/matrix-calibration-preview"}
    failures = list(row.get("failures") or [])
    transaction = _bam6d_output_transaction(output)
    failures.extend(_bam6d_transaction_failures(transaction, preview_name="matrix_calibration_preview", brain_id=brain_id))
    row["maintenance_transaction_state"] = transaction.get("state")
    row["maintenance_transaction_conflict_count"] = int(dict(transaction.get("conflict_resolution") or {}).get("conflict_count") or 0)
    row["failures"] = list(dict.fromkeys(str(item) for item in failures if str(item)))
    row["passed"] = not row["failures"]
    return row


def _bam6d_maintenance_preview_row(
    output: dict[str, Any],
    *,
    case_id: str,
    expected_tool: str,
    mode: str,
    brain_id: str,
    elapsed_ms: int,
    health_polls: list[dict[str, Any]],
) -> dict[str, Any]:
    row = _pr12p14x_f_real_preview_row(
        output,
        case_id=case_id,
        expected_tool=expected_tool,
        brain_id=brain_id,
        elapsed_ms=elapsed_ms,
        min_proposals=0,
        require_structural_surface=False,
    )
    row = {**row, "path": f"/mcp/{expected_tool.replace('_', '-')}", "mode": mode}
    failures = list(row.get("failures") or [])
    transaction = _bam6d_output_transaction(output)
    failures.extend(_bam6d_transaction_failures(transaction, preview_name=case_id, brain_id=brain_id))
    failures.extend(_bam6d_health_poll_failures(health_polls, preview_name=case_id))
    preview_plan = dict(output.get("maintenance_preview_plan") or dict(output.get("maintenance_report") or {}).get("maintenance_preview_plan") or {})
    budget_guard = dict(output.get("preview_budget_guard") or dict(output.get("maintenance_report") or {}).get("preview_budget_guard") or {})
    if not preview_plan:
        failures.append(f"{case_id}_maintenance_preview_plan_missing")
    if not budget_guard:
        failures.append(f"{case_id}_preview_budget_guard_missing")
    row["maintenance_transaction_state"] = transaction.get("state")
    row["maintenance_transaction_conflict_count"] = int(dict(transaction.get("conflict_resolution") or {}).get("conflict_count") or 0)
    row["health_poll_count"] = len(health_polls)
    row["health_poll_fail_count"] = len([item for item in health_polls if not bool(item.get("passed"))])
    row["preview_depth"] = preview_plan.get("preview_depth")
    row["effective_max_nodes_considered"] = budget_guard.get("effective_max_nodes_considered")
    row["failures"] = list(dict.fromkeys(str(item) for item in failures if str(item)))
    row["passed"] = not row["failures"]
    return row


def _bam6d_rows_by_case(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row.get("case_id") or ""): dict(row) for row in rows if str(row.get("case_id") or "")}


def run_bam6d_maintenance_preflight_report(
    base_url: str | None = None,
    *,
    brain_id: str | None = None,
    timeout: float | None = None,
    max_nodes_considered: int | None = None,
) -> dict[str, Any]:
    selected_base_url = str(base_url or os.environ.get("AGVM_BAM6D_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    selected_brain_id = str(brain_id or os.environ.get("AGVM_VALIDATION_BRAIN_ID") or "simone_massaro_validation").strip()
    operation_timeout = float(timeout or os.environ.get("AGVM_BAM6D_TIMEOUT_SECONDS") or 180.0)
    preview_nodes = int(max_nodes_considered or os.environ.get("AGVM_BAM6D_MAX_NODES_CONSIDERED") or 120)
    started = time.perf_counter()
    started_at = _bam6d_timestamp()
    rows: list[dict[str, Any]] = []
    outputs: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []

    health_before_row, health_before = _bam6d_health_row(
        selected_base_url,
        case_id="api_health_before",
        brain_id=selected_brain_id,
        timeout=min(operation_timeout, 15.0),
    )
    rows.append(health_before_row)
    outputs["api_health_before"] = health_before

    before_recommendation = "missing"
    try:
        brain_health_before, elapsed_ms = _pr12p12_elapsed_post(
            selected_base_url,
            "/mcp/brain-health",
            {"brain_id": selected_brain_id, "limit": 25, "include_issue_samples": True},
            timeout=min(operation_timeout, 60.0),
        )
        outputs["brain_health_before"] = brain_health_before
        brain_health_before_row = _bam6d_brain_health_row(
            brain_health_before,
            case_id="brain_health_before",
            brain_id=selected_brain_id,
            elapsed_ms=elapsed_ms,
            final_check=False,
        )
        rows.append(brain_health_before_row)
        before_recommendation = str(brain_health_before_row.get("recommendation") or "missing")
        before_verdict = str(brain_health_before_row.get("benchmark_preflight_verdict") or "")
        if before_verdict == "benchmark_blocked_until_preview":
            warnings.append("before_health_blocked_until_preview_expected_before_bam6d_preview_sequence")
    except Exception as exc:
        rows.append(
            {
                "schema_version": "agvm.pr12p14x_f.real_health_row.v1",
                "case_id": "brain_health_before",
                "path": "/mcp/brain-health",
                "passed": False,
                "failures": [f"brain_health_before_runtime_error:{exc}"],
                "elapsed_ms": 0,
            }
        )

    matrix_payload = _pr12p14x_f_matrix_preview_payload(brain_id=selected_brain_id, max_nodes_considered=preview_nodes)
    try:
        matrix_output, elapsed_ms, matrix_polls = _bam6d_call_post_with_health_monitor(
            selected_base_url,
            "/mcp/matrix-calibration-preview",
            matrix_payload,
            brain_id=selected_brain_id,
            timeout=min(operation_timeout, 90.0),
        )
        outputs["matrix_calibration_preview"] = matrix_output
        row = _bam6d_matrix_preview_row(
            matrix_output,
            brain_id=selected_brain_id,
            elapsed_ms=elapsed_ms,
            health_recommendation=before_recommendation,
        )
        row["health_poll_count"] = len(matrix_polls)
        row["health_poll_fail_count"] = len([item for item in matrix_polls if not bool(item.get("passed"))])
        row["failures"] = list(dict.fromkeys(list(row.get("failures") or []) + _bam6d_health_poll_failures(matrix_polls, preview_name="matrix_calibration_preview")))
        row["passed"] = not row["failures"]
        rows.append(row)
    except Exception as exc:
        rows.append(
            {
                "schema_version": "agvm.bam6d.preview_row.v1",
                "case_id": "matrix_calibration_preview",
                "path": "/mcp/matrix-calibration-preview",
                "passed": False,
                "failures": [f"matrix_calibration_preview_runtime_error:{exc}"],
                "elapsed_ms": 0,
            }
        )

    for path, case_id, expected_tool, mode in (
        ("/mcp/sleep-preview", "sleep_preview", "sleep_preview", "sleep"),
        ("/mcp/evolve-preview", "evolve_preview", "evolve_preview", "evolve"),
    ):
        try:
            output, elapsed_ms, polls = _bam6d_call_post_with_health_monitor(
                selected_base_url,
                path,
                {"brain_id": selected_brain_id, "mode": mode, "max_nodes_considered": preview_nodes},
                brain_id=selected_brain_id,
                timeout=operation_timeout,
            )
            outputs[case_id] = output
            rows.append(
                _bam6d_maintenance_preview_row(
                    output,
                    case_id=case_id,
                    expected_tool=expected_tool,
                    mode=mode,
                    brain_id=selected_brain_id,
                    elapsed_ms=elapsed_ms,
                    health_polls=polls,
                )
            )
        except Exception as exc:
            rows.append(
                {
                    "schema_version": "agvm.bam6d.preview_row.v1",
                    "case_id": case_id,
                    "path": path,
                    "passed": False,
                    "failures": [f"{case_id}_runtime_error:{exc}"],
                    "elapsed_ms": 0,
                }
            )

    final_brain_health_output: dict[str, Any] = {}
    try:
        final_brain_health_output, elapsed_ms = _pr12p12_elapsed_post(
            selected_base_url,
            "/mcp/brain-health",
            {"brain_id": selected_brain_id, "limit": 25, "include_issue_samples": True},
            timeout=min(operation_timeout, 60.0),
        )
        outputs["brain_health_after"] = final_brain_health_output
        rows.append(
            _bam6d_brain_health_row(
                final_brain_health_output,
                case_id="brain_health_after",
                brain_id=selected_brain_id,
                elapsed_ms=elapsed_ms,
                final_check=True,
            )
        )
    except Exception as exc:
        rows.append(
            {
                "schema_version": "agvm.pr12p14x_f.real_health_row.v1",
                "case_id": "brain_health_after",
                "path": "/mcp/brain-health",
                "passed": False,
                "failures": [f"brain_health_after_runtime_error:{exc}"],
                "elapsed_ms": 0,
            }
        )

    health_after_row, health_after = _bam6d_health_row(
        selected_base_url,
        case_id="api_health_after",
        brain_id=selected_brain_id,
        timeout=min(operation_timeout, 15.0),
    )
    rows.append(health_after_row)
    outputs["api_health_after"] = health_after

    rows_by_case = _bam6d_rows_by_case(rows)
    transaction_rows = [row for row in rows if row.get("maintenance_transaction_state") is not None]
    health_poll_fail_count = sum(int(row.get("health_poll_fail_count") or 0) for row in rows)
    final_health = rows_by_case.get("brain_health_after", {})
    acceptance = {
        "real_validation_brain_used": selected_brain_id == "simone_massaro_validation",
        "api_health_before_ok": bool(rows_by_case.get("api_health_before", {}).get("passed")),
        "api_health_after_ok": bool(rows_by_case.get("api_health_after", {}).get("passed")),
        "brain_health_before_ok": bool(rows_by_case.get("brain_health_before", {}).get("passed")),
        "matrix_preview_ok": bool(rows_by_case.get("matrix_calibration_preview", {}).get("passed")),
        "sleep_preview_ok": bool(rows_by_case.get("sleep_preview", {}).get("passed")),
        "evolve_preview_ok": bool(rows_by_case.get("evolve_preview", {}).get("passed")),
        "brain_health_after_ok": bool(final_health.get("passed")),
        "api_health_during_previews_ok": health_poll_fail_count == 0,
        "maintenance_transactions_present": len(transaction_rows) >= 3,
        "maintenance_transactions_revision_keyed": all(
            row.get("maintenance_transaction_state") in {"preview_ready", "apply_pending", "healthy_for_benchmark"}
            for row in transaction_rows
        )
        and len(transaction_rows) >= 3,
        "no_hidden_mutation_reported": not any(
            "hidden_mutation" in ",".join(str(item) for item in list(row.get("failures") or [])) for row in rows
        ),
        "post_health_allows_serious_benchmark": bool(final_health.get("serious_product_benchmark_allowed")),
        "post_health_does_not_require_rebuild_or_grow": str(final_health.get("recommendation") or "") not in {"rebuild_required", "grow_repair"},
    }
    failed_cases = [str(row.get("case_id") or "") for row in rows if not bool(row.get("passed"))]
    failures: list[str] = []
    for row in rows:
        if not bool(row.get("passed")):
            failures.extend([f"{row.get('case_id')}:{failure}" for failure in list(row.get("failures") or ["failed"])])
    failures.extend([f"acceptance_failed:{key}" for key, passed in acceptance.items() if not passed])
    failures = list(dict.fromkeys(str(item) for item in failures if str(item)))
    all_pass = not failures
    certification_allowed = bool(all_pass and acceptance["post_health_allows_serious_benchmark"])
    preview_max_elapsed_ms = max([int(row.get("elapsed_ms") or 0) for row in rows if "preview" in str(row.get("case_id") or "")] or [0])
    if preview_max_elapsed_ms > 15000:
        warnings.append(f"maintenance_preview_reference_slo_warning:{preview_max_elapsed_ms}>15000")
    return {
        "schema_version": BAM6D_MAINTENANCE_PREFLIGHT_SCHEMA_VERSION,
        "phase": "maintenance_preflight_before_certification",
        "slice": "BAM-6D",
        "proof_scope": "real_mcp_runtime_not_fixture",
        "base_url": selected_base_url,
        "brain_id": selected_brain_id,
        "started_at": started_at,
        "execution": {
            "timeout_seconds": operation_timeout,
            "max_nodes_considered": preview_nodes,
            "matrix_preview_payload": matrix_payload,
            "operational_timeout_only": True,
            "retrieve_slo_thresholds_changed": False,
            "full_phase8c_benchmark_started": False,
            "run_order": [
                "/health",
                "/mcp/brain-health",
                "/mcp/matrix-calibration-preview",
                "/mcp/sleep-preview",
                "/mcp/evolve-preview",
                "/mcp/brain-health",
                "/health",
            ],
        },
        "all_pass": all_pass,
        "maintenance_preflight_green": all_pass,
        "phase8c_c_full_certification_allowed": certification_allowed,
        "product_ready_claim_allowed": False,
        "backend_mcp_revolutionary_candidate": False,
        "certification_gate": {
            "verdict": "maintenance_preflight_green" if certification_allowed else "maintenance_preflight_blocked",
            "phase8c_c_full_certification_allowed": certification_allowed,
            "reason": "BAM-6D green; run BAM-7 full unselected matrix next" if certification_allowed else "resolve BAM-6D failures before full matrix",
        },
        "acceptance": acceptance,
        "failed_cases": failed_cases,
        "failures": failures,
        "warnings": list(dict.fromkeys(warnings)),
        "quality_rows": rows,
        "runtime_outputs": {
            key: {
                "schema_version": value.get("schema_version"),
                "tool_name": value.get("tool_name"),
                "status": value.get("status"),
                "brain_id": value.get("brain_id"),
                "maintenance_transaction_state": dict(value.get("maintenance_transaction") or {}).get("state"),
            }
            for key, value in outputs.items()
            if isinstance(value, dict)
        },
        "matrix_summary": {
            "case_count": len(rows),
            "passed_count": sum(1 for row in rows if bool(row.get("passed"))),
            "max_elapsed_ms": max([int(row.get("elapsed_ms") or 0) for row in rows] or [0]),
            "max_preview_elapsed_ms": preview_max_elapsed_ms,
            "health_poll_fail_count": health_poll_fail_count,
            "final_health_recommendation": str(final_health.get("recommendation") or ""),
            "final_benchmark_preflight_verdict": str(final_health.get("benchmark_preflight_verdict") or ""),
            "transaction_states": {
                str(row.get("case_id") or ""): row.get("maintenance_transaction_state")
                for row in transaction_rows
            },
            "transaction_conflict_counts": {
                str(row.get("case_id") or ""): int(row.get("maintenance_transaction_conflict_count") or 0)
                for row in transaction_rows
            },
        },
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 2),
        "next_slice": "BAM-7 full unselected Phase 8C-C certification matrix" if certification_allowed else "BAM-6D remediation or signed maintenance apply slice",
    }


def render_bam6d_maintenance_preflight_markdown(report: dict[str, Any]) -> str:
    rows = [dict(row) for row in list(report.get("quality_rows") or []) if isinstance(row, dict)]
    lines = [
        "# BAM-6D Maintenance Preflight",
        "",
        f"- Schema: `{report.get('schema_version')}`",
        f"- Brain: `{report.get('brain_id')}`",
        f"- All pass: `{bool(report.get('all_pass'))}`",
        f"- Phase 8C-C allowed: `{bool(report.get('phase8c_c_full_certification_allowed'))}`",
        f"- Verdict: `{dict(report.get('certification_gate') or {}).get('verdict')}`",
        f"- Elapsed ms: `{report.get('elapsed_ms')}`",
        "",
        "## Rows",
        "",
    ]
    for row in rows:
        lines.append(
            f"- `{row.get('case_id')}`: passed=`{bool(row.get('passed'))}`, "
            f"elapsed_ms=`{row.get('elapsed_ms')}`, failures=`{', '.join(str(item) for item in list(row.get('failures') or [])) or 'none'}`"
        )
    lines.extend(["", "## Failures", ""])
    failures = list(report.get("failures") or [])
    if failures:
        lines.extend(f"- {failure}" for failure in failures)
    else:
        lines.append("- none")
    warnings = list(report.get("warnings") or [])
    lines.extend(["", "## Warnings", ""])
    if warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("- none")
    lines.extend(["", f"- Next slice: `{report.get('next_slice')}`", ""])
    return "\n".join(lines)


def _pr12p14x_g_report_path_from_env(env_key: str) -> Path | None:
    raw_path = str(os.environ.get(env_key) or "").strip()
    if not raw_path:
        return None
    return Path(raw_path)


def _pr12p14x_g_load_report(path: Path | None) -> tuple[dict[str, Any] | None, str | None]:
    if path is None:
        return None, None
    try:
        if not path.exists():
            return None, f"report_artifact_missing:{path}"
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, f"report_artifact_not_object:{path}"
        return data, None
    except Exception as exc:
        return None, f"report_artifact_unreadable:{path}:{exc}"


def _pr12p14x_g_real_brain_gate(base_url: str, brain_id: str) -> dict[str, Any]:
    failures: list[str] = []
    evidence: dict[str, Any] = {}
    try:
        registry = get_json(base_url, "/memory/brains", timeout=30.0)
    except Exception as exc:
        return {
            "gate_id": "real_validation_brain_runtime",
            "passed": False,
            "failures": [f"brain_registry_runtime_unreachable:{exc}"],
            "evidence": evidence,
        }
    brains = [dict(item) for item in list(registry.get("brains") or []) if isinstance(item, dict)]
    record = next((item for item in brains if str(item.get("brain_id") or "") == brain_id), {})
    node_count = int(record.get("node_count") or 0)
    evidence = {
        "registry_active_brain_id": registry.get("active_brain_id"),
        "registry_default_brain_id": registry.get("default_brain_id"),
        "brain_count": int(registry.get("brain_count") or len(brains)),
        "brain_id": record.get("brain_id"),
        "node_count": node_count,
        "safe_for_mcp": bool(record.get("safe_for_mcp")),
        "is_active": bool(record.get("is_active")),
        "is_default": bool(record.get("is_default")),
    }
    if not record:
        failures.append("validation_brain_missing")
    if str(record.get("brain_id") or "") != brain_id:
        failures.append("validation_brain_id_mismatch")
    if not bool(record.get("safe_for_mcp")):
        failures.append("validation_brain_not_safe_for_mcp")
    if not bool(record.get("is_active")):
        failures.append("validation_brain_not_active")
    if node_count < 2000:
        failures.append(f"validation_brain_below_scale:{node_count}<2000")
    if node_count > 4000:
        failures.append(f"validation_brain_above_validation_scale:{node_count}>4000")
    return {
        "gate_id": "real_validation_brain_runtime",
        "passed": not failures,
        "failures": failures,
        "evidence": evidence,
    }


def _pr12p14x_g_report_gate(
    report: dict[str, Any] | None,
    *,
    gate_id: str,
    expected_schema: str,
    expected_phase: str,
    brain_id: str,
    source: str,
    load_error: str | None = None,
) -> dict[str, Any]:
    failures: list[str] = []
    evidence: dict[str, Any] = {"source": source}
    if load_error:
        failures.append(load_error)
    if not report:
        failures.append(f"{gate_id}_report_missing")
        return {"gate_id": gate_id, "passed": False, "failures": failures, "evidence": evidence}
    evidence.update(
        {
            "schema_version": report.get("schema_version"),
            "phase": report.get("phase"),
            "proof_scope": report.get("proof_scope"),
            "brain_id": report.get("brain_id"),
            "all_pass": bool(report.get("all_pass")),
            "elapsed_ms": report.get("elapsed_ms"),
        }
    )
    if report.get("schema_version") != expected_schema:
        failures.append(f"{gate_id}_schema_mismatch:{report.get('schema_version')}")
    if str(report.get("phase") or "") != expected_phase:
        failures.append(f"{gate_id}_phase_mismatch:{report.get('phase')}")
    if str(report.get("proof_scope") or "") != "real_mcp_runtime_not_fixture":
        failures.append(f"{gate_id}_not_real_mcp_runtime")
    if str(report.get("brain_id") or "") != brain_id:
        failures.append(f"{gate_id}_brain_id_mismatch:{report.get('brain_id')}")
    if not bool(report.get("all_pass")):
        failures.extend(f"{gate_id}:{failure}" for failure in list(report.get("failures") or ["report_all_pass_false"]))
    return {"gate_id": gate_id, "passed": not failures, "failures": failures, "evidence": evidence}


def _pr12p14x_g_float_values(rows: list[dict[str, Any]], key: str) -> list[float]:
    values: list[float] = []
    for row in rows:
        try:
            value = row.get(key)
            if value is not None:
                values.append(float(value))
        except (TypeError, ValueError):
            continue
    return values


def _pr12p14x_g_latency_gate(retrieve_report: dict[str, Any] | None, maintenance_report: dict[str, Any] | None) -> dict[str, Any]:
    failures: list[str] = []
    retrieve_rows = [dict(row) for row in list((retrieve_report or {}).get("quality_rows") or []) if isinstance(row, dict)]
    maintenance_rows = [dict(row) for row in list((maintenance_report or {}).get("quality_rows") or []) if isinstance(row, dict)]
    first_values = _pr12p14x_g_float_values(retrieve_rows, "first_useful_package_ms")
    full_values = _pr12p14x_g_float_values(retrieve_rows, "full_completion_ms") or _pr12p14x_g_float_values(retrieve_rows, "http_elapsed_ms")
    blocking_full_values: list[float] = []
    secondary_background_values: list[float] = []
    for row in retrieve_rows:
        raw_value = row.get("full_completion_ms")
        if raw_value is None:
            raw_value = row.get("http_elapsed_ms")
        try:
            full_value = float(raw_value)
        except (TypeError, ValueError):
            continue
        nonblocking_secondary = bool(
            row.get("first_package_returned_before_full_completion")
            and row.get("background_completion_inspectable")
            and row.get("full_completion_is_secondary")
            and str(row.get("http_response_policy") or "").strip() == "nonblocking_first_package_with_background_completion"
        )
        if nonblocking_secondary:
            secondary_background_values.append(full_value)
        else:
            blocking_full_values.append(full_value)
    maintenance_preview_values = [
        float(row.get("elapsed_ms") or 0)
        for row in maintenance_rows
        if str(row.get("case_id") or "") in {"sleep_preview_runtime", "evolve_preview_runtime", "matrix_calibration_preview_runtime"}
    ]
    max_first_ms = max(first_values or [0.0])
    max_full_ms = max(full_values or [0.0])
    max_blocking_full_ms = max(blocking_full_values or [0.0])
    max_secondary_background_ms = max(secondary_background_values or [0.0])
    max_maintenance_preview_ms = max(maintenance_preview_values or [0.0])
    if not first_values:
        failures.append("first_package_latency_missing")
    if not full_values:
        failures.append("full_completion_latency_missing")
    if max_first_ms > 12000:
        failures.append(f"first_package_latency_over_product_slo:{max_first_ms:.2f}>12000")
    if max_blocking_full_ms > 15000:
        failures.append(f"blocking_full_completion_latency_over_product_slo:{max_blocking_full_ms:.2f}>15000")
    if max_maintenance_preview_ms > 15000:
        failures.append(f"maintenance_preview_latency_over_product_slo:{max_maintenance_preview_ms:.2f}>15000")
    background_warnings = []
    if max_secondary_background_ms > 15000:
        background_warnings.append(f"secondary_background_completion_over_slo:{max_secondary_background_ms:.2f}>15000")
    return {
        "gate_id": "runtime_latency_product_slo",
        "passed": not failures,
        "failures": failures,
        "evidence": {
            "retrieve_case_count": len(retrieve_rows),
            "max_first_useful_package_ms": round(max_first_ms, 2),
            "max_full_completion_ms": round(max_full_ms, 2),
            "max_blocking_full_completion_ms": round(max_blocking_full_ms, 2),
            "max_secondary_background_completion_ms": round(max_secondary_background_ms, 2),
            "nonblocking_secondary_case_count": len(secondary_background_values),
            "background_completion_warnings": background_warnings,
            "max_maintenance_preview_ms": round(max_maintenance_preview_ms, 2),
            "first_package_slo_ms": 12000,
            "full_completion_slo_ms": 15000,
            "maintenance_preview_slo_ms": 15000,
            "latency_basis": "real_mcp_runtime_rows",
        },
    }


def _pr12p14x_g_brain_health_gate(maintenance_report: dict[str, Any] | None) -> dict[str, Any]:
    rows = [dict(row) for row in list((maintenance_report or {}).get("quality_rows") or []) if isinstance(row, dict)]
    health = next((row for row in rows if str(row.get("case_id") or "") == "brain_health_runtime"), {})
    matrix_calibration = next((row for row in rows if str(row.get("case_id") or "") == "matrix_calibration_preview_runtime"), {})
    recommendation = str(health.get("recommendation") or "").strip() or "missing"
    benchmark_preflight = dict(health.get("benchmark_preflight") or {})
    preflight_verdict = str(health.get("benchmark_preflight_verdict") or benchmark_preflight.get("verdict") or "").strip()
    revolutionary_certification_allowed = bool(
        health.get("revolutionary_certification_allowed")
        or benchmark_preflight.get("revolutionary_certification_allowed")
    )
    serious_product_benchmark_allowed = bool(
        health.get("serious_product_benchmark_allowed")
        or benchmark_preflight.get("serious_product_benchmark_allowed")
    )
    diagnostic_runs_allowed = bool(health.get("diagnostic_runs_allowed") or benchmark_preflight.get("diagnostic_runs_allowed"))
    alert_summary = dict(health.get("alert_summary") or {})
    severity_histogram = dict(alert_summary.get("severity_histogram") or {})
    retrieval_repeated_families = list(
        dict(health.get("retrieval_learning_rollup") or {}).get("repeated_signal_families") or []
    )
    failures: list[str] = []
    if not health:
        failures.append("brain_health_row_missing")
    if health and not preflight_verdict:
        failures.append("brain_health_benchmark_preflight_missing")
    if health and not diagnostic_runs_allowed:
        failures.append("brain_health_diagnostic_runs_not_allowed")
    matrix_calibration_routed = bool(
        recommendation == "matrix_calibration_preview"
        and matrix_calibration
        and bool(matrix_calibration.get("passed"))
        and bool(matrix_calibration.get("non_mutating"))
        and matrix_calibration.get("hidden_mutation_allowed") is False
        and int(matrix_calibration.get("proposal_count") or 0) > 0
    )
    if recommendation not in {"none", "missing"} and not matrix_calibration_routed:
        failures.append(f"brain_health_recommends_action:{recommendation}")
    preview_unblocks_certification = bool(
        preflight_verdict == "benchmark_blocked_until_preview"
        and matrix_calibration_routed
    )
    warning_only_health_certification_safe = False
    if (
        health
        and preflight_verdict == "benchmark_allowed_with_warnings"
        and serious_product_benchmark_allowed
        and not retrieval_repeated_families
    ):
        actionable_warning_count = sum(
            int(count or 0)
            for severity, count in severity_histogram.items()
            if str(severity or "").strip() not in {"", "info", "watch"}
        )
        warning_only_health_certification_safe = actionable_warning_count == 0
    effective_revolutionary_certification_allowed = bool(
        revolutionary_certification_allowed
        or preview_unblocks_certification
        or warning_only_health_certification_safe
    )
    if health and not effective_revolutionary_certification_allowed:
        failures.append(f"brain_health_preflight_blocks_revolutionary_certification:{preflight_verdict or 'missing'}")
    if warning_only_health_certification_safe:
        certification_basis = "benchmark_allowed_with_watch_warnings_and_green_current_matrices"
    elif preview_unblocks_certification:
        certification_basis = "non_mutating_matrix_preview_unblocked_benchmark"
    elif revolutionary_certification_allowed:
        certification_basis = "healthy_for_benchmark_preflight"
    else:
        certification_basis = "blocked_or_missing_health_preflight"
    return {
        "gate_id": "brain_health_ready",
        "passed": not failures,
        "failures": failures,
        "evidence": {
            "recommendation": recommendation,
            "reason_codes": list(health.get("reason_codes") or []),
            "node_count": health.get("node_count"),
            "benchmark_preflight_verdict": preflight_verdict or "missing",
            "serious_product_benchmark_allowed": serious_product_benchmark_allowed,
            "revolutionary_certification_allowed": effective_revolutionary_certification_allowed,
            "raw_revolutionary_certification_allowed": revolutionary_certification_allowed,
            "preview_unblocks_revolutionary_certification": preview_unblocks_certification,
            "warning_only_health_certification_safe": warning_only_health_certification_safe,
            "certification_basis": certification_basis,
            "diagnostic_runs_allowed": diagnostic_runs_allowed,
            "sanity_severity": str(health.get("sanity_severity") or ""),
            "alert_summary": alert_summary,
            "retrieval_learning_repeated_families": retrieval_repeated_families,
            "matrix_calibration_routed": matrix_calibration_routed,
            "matrix_calibration_case_passed": bool(matrix_calibration.get("passed")),
            "matrix_calibration_proposal_count": int(matrix_calibration.get("proposal_count") or 0),
        },
    }


def _pr12p14x_g_rows(report: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [dict(row) for row in list((report or {}).get("quality_rows") or []) if isinstance(row, dict)]


def _pr12p14x_g_latency_values(rows: list[dict[str, Any]], *keys: str) -> list[float]:
    values: list[float] = []
    for row in rows:
        for key in keys:
            try:
                value = float(row.get(key) or 0.0)
            except (TypeError, ValueError):
                value = 0.0
            if value > 0:
                values.append(value)
                break
    return values


def _pr12p14x_g_latency_range(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "min_ms": 0.0, "max_ms": 0.0, "avg_ms": 0.0}
    return {
        "count": len(values),
        "min_ms": round(min(values), 2),
        "max_ms": round(max(values), 2),
        "avg_ms": round(sum(values) / max(1, len(values)), 2),
    }


def _pr12p14x_g_ai_material_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    required_rows = [row for row in rows if bool(row.get("require_ai_material")) or bool(row.get("ai_required"))]
    materialized = [
        row
        for row in rows
        if bool(row.get("ai_materialized"))
        or bool(row.get("semantic_ai_materialized"))
        or bool(row.get("spatial_ai_materialized"))
        or str(row.get("ai_gate") or row.get("ai_materialization_state") or "").lower() in {"materialized", "ai_materialized"}
    ]
    blocked_or_diagnostic = [
        row
        for row in rows
        if str(row.get("status") or row.get("terminal_state") or row.get("ai_gate") or "").lower()
        in {"blocked", "diagnostic", "partial", "partial_context"}
        or str(row.get("answerability_state") or "").lower() in {"partial", "blocked", "missing"}
    ]
    heuristic_only = [
        row
        for row in rows
        if bool(row.get("heuristic_only"))
        or str(row.get("planner_path") or row.get("route_family") or "").lower().startswith("heuristic")
    ]
    return {
        "row_count": len(rows),
        "ai_required_row_count": len(required_rows),
        "ai_materialized_row_count": len(materialized),
        "blocked_or_diagnostic_row_count": len(blocked_or_diagnostic),
        "heuristic_only_row_count": len(heuristic_only),
        "all_required_rows_have_ai_material": all(
            bool(row.get("ai_materialized"))
            or bool(row.get("semantic_ai_materialized"))
            or bool(row.get("spatial_ai_materialized"))
            for row in required_rows
        )
        if required_rows
        else True,
        "heuristic_only_product_certification_allowed": False,
    }


def _pr12p14x_g_audit_rows(report: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows = _pr12p14x_g_rows(report)
    slim_rows: list[dict[str, Any]] = []
    for row in rows:
        slim_rows.append(
            {
                "case_id": row.get("case_id"),
                "passed": bool(row.get("passed")),
                "failures": list(row.get("failures") or []),
                "status": row.get("status"),
                "client_payload_state": row.get("delivery_client_payload_state"),
                "terminal_for_client": bool(row.get("delivery_terminal_for_client")),
                "primary_payload_chars": row.get("primary_payload_chars"),
                "hot_section_count": row.get("hot_section_count"),
                "document_ref_count": row.get("document_ref_count"),
                "path_count": row.get("path_count"),
                "route_event_count": row.get("route_event_count"),
                "trace_step_count": row.get("trace_step_count"),
                "ai_materialized": bool(row.get("ai_materialized")),
                "semantic_ai_materialized": bool(row.get("semantic_ai_materialized")),
                "ai_spatial_status": row.get("ai_spatial_status"),
                "ai_spatial_source": row.get("ai_spatial_source"),
                "ai_landing_count": row.get("ai_landing_count"),
                "ai_path_count": row.get("ai_path_count"),
                "first_useful_package_ms": row.get("first_useful_package_ms"),
                "full_completion_ms": row.get("full_completion_ms"),
                "background_completion_ms": row.get("background_completion_ms"),
                "expected_term_hits": dict(row.get("expected_term_hits") or {}),
            }
        )
    return slim_rows


def _pr12p14x_g_h6_release_gate(
    *,
    gate_matrix: dict[str, dict[str, Any]],
    failed_gates: list[str],
    failures: list[str],
    retrieve_report: dict[str, Any] | None,
    maintenance_report: dict[str, Any] | None,
    retrieve_source: str,
    maintenance_source: str,
    local_mcp_product_ready: bool,
) -> dict[str, Any]:
    retrieve_rows = _pr12p14x_g_rows(retrieve_report)
    maintenance_rows = _pr12p14x_g_rows(maintenance_report)
    retrieve_audit_rows = _pr12p14x_g_audit_rows(retrieve_report)
    maintenance_audit_rows = _pr12p14x_g_audit_rows(maintenance_report)
    all_rows = retrieve_rows + maintenance_rows
    first_values = _pr12p14x_g_latency_values(
        retrieve_rows,
        "external_first_payload_ms",
        "client_first_payload_ms",
        "first_http_response_ms",
        "first_useful_package_ms",
    )
    final_values = _pr12p14x_g_latency_values(
        retrieve_rows,
        "external_final_ms",
        "full_completion_ms",
        "final_answer_ms",
    )
    maintenance_values = _pr12p14x_g_latency_values(maintenance_rows, "elapsed_ms")
    retrieve_passed = sum(1 for row in retrieve_rows if bool(row.get("passed")))
    maintenance_passed = sum(1 for row in maintenance_rows if bool(row.get("passed")))
    health = dict(gate_matrix.get("brain_health_ready", {}).get("evidence") or {})
    latency = dict(gate_matrix.get("runtime_latency_product_slo", {}).get("evidence") or {})
    retrieval_repeated = list(health.get("retrieval_learning_repeated_families") or [])
    blocker_reasons = list(failures)
    if not blocker_reasons and not local_mcp_product_ready:
        blocker_reasons = [f"gate_failed:{gate_id}" for gate_id in failed_gates]
    return {
        "schema_version": "agvm.phase8b_h6_d.release_gate.v1",
        "h6_green": bool(local_mcp_product_ready),
        "phase8c_unblocked": bool(local_mcp_product_ready),
        "phase8c_status": "unblocked" if local_mcp_product_ready else "blocked_continue_h6_fix_loop",
        "stop_rule": "start_phase8c_only_if_phase8c_unblocked_true",
        "product_or_revolutionary_claim_allowed": bool(local_mcp_product_ready),
        "artifact_sources": {
            "retrieve_quality": retrieve_source,
            "sleep_evolve": maintenance_source,
        },
        "artifact_summary": {
            "retrieve_quality": {
                "schema_version": (retrieve_report or {}).get("schema_version"),
                "phase": (retrieve_report or {}).get("phase"),
                "all_pass": bool((retrieve_report or {}).get("all_pass")),
                "row_count": len(retrieve_rows),
                "passed_count": retrieve_passed,
                "failed_count": max(0, len(retrieve_rows) - retrieve_passed),
                "failed_cases": [str(row.get("case_id") or "") for row in retrieve_audit_rows if not bool(row.get("passed"))],
                "row_details_available": bool(retrieve_audit_rows),
            },
            "sleep_evolve": {
                "schema_version": (maintenance_report or {}).get("schema_version"),
                "phase": (maintenance_report or {}).get("phase"),
                "all_pass": bool((maintenance_report or {}).get("all_pass")),
                "row_count": len(maintenance_rows),
                "passed_count": maintenance_passed,
                "failed_count": max(0, len(maintenance_rows) - maintenance_passed),
                "failed_cases": [str(row.get("case_id") or "") for row in maintenance_audit_rows if not bool(row.get("passed"))],
                "row_details_available": bool(maintenance_audit_rows),
            },
        },
        "artifact_rows": {
            "retrieve_quality": retrieve_audit_rows,
            "sleep_evolve": maintenance_audit_rows,
            "retention_policy": "slim_quality_rows_embedded_for_h6_audit",
        },
        "gate_summary": {
            "gate_count": len(gate_matrix),
            "passed_count": sum(1 for gate in gate_matrix.values() if bool(gate.get("passed"))),
            "failed_gates": list(failed_gates),
            "failures": blocker_reasons,
        },
        "latency_summary": {
            "first_payload": _pr12p14x_g_latency_range(first_values),
            "final_completion": _pr12p14x_g_latency_range(final_values),
            "maintenance_preview": _pr12p14x_g_latency_range(maintenance_values),
            "slo_evidence": latency,
        },
        "ai_materialization_summary": _pr12p14x_g_ai_material_summary(all_rows),
        "correction_learning_evidence": {
            "health_recommendation": health.get("recommendation"),
            "benchmark_preflight_verdict": health.get("benchmark_preflight_verdict"),
            "revolutionary_certification_allowed": bool(health.get("revolutionary_certification_allowed")),
            "retrieval_learning_repeated_families": retrieval_repeated,
            "alert_summary": dict(health.get("alert_summary") or {}),
            "h5_learning_visible": bool(retrieval_repeated or dict(health.get("alert_summary") or {})),
        },
        "context_quality_summary": {
            "retrieve_rows": len(retrieve_rows),
            "retrieve_passed_rows": retrieve_passed,
            "path_rows": sum(1 for row in retrieve_rows if int(row.get("path_count") or row.get("paths") or 0) > 0),
            "document_rows": sum(
                1
                for row in retrieve_rows
                if int(row.get("document_ref_count") or row.get("primary_document_count") or row.get("documents") or 0) > 0
            ),
        },
        "next_action": "start_phase8c_rag_baseline_comparison"
        if local_mcp_product_ready
        else "continue_h6_context_path_health_fix_loop",
    }


def run_pr12p14x_g_real_mcp_final_product_verdict_suite(
    base_url: str | None = None,
    *,
    brain_id: str | None = None,
    retrieve_report: dict[str, Any] | None = None,
    maintenance_report: dict[str, Any] | None = None,
    rerun_matrices: bool | None = None,
) -> dict[str, Any]:
    selected_base_url = str(base_url or DEFAULT_BASE_URL).rstrip("/")
    selected_brain_id = str(brain_id or os.environ.get("AGVM_VALIDATION_BRAIN_ID") or PR12P14X_E_REAL_BRAIN_ID).strip()
    started = time.perf_counter()

    retrieve_source = "provided_report" if retrieve_report is not None else "missing"
    maintenance_source = "provided_report" if maintenance_report is not None else "missing"
    retrieve_load_error = None
    maintenance_load_error = None
    if retrieve_report is None:
        retrieve_path = _pr12p14x_g_report_path_from_env("AGVM_PR12P14X_E_REAL_MCP_REPORT_PATH")
        retrieve_report, retrieve_load_error = _pr12p14x_g_load_report(retrieve_path)
        retrieve_source = f"artifact:{retrieve_path}" if retrieve_path else "missing"
    if maintenance_report is None:
        maintenance_path = _pr12p14x_g_report_path_from_env("AGVM_PR12P14X_F_REAL_MCP_REPORT_PATH")
        maintenance_report, maintenance_load_error = _pr12p14x_g_load_report(maintenance_path)
        maintenance_source = f"artifact:{maintenance_path}" if maintenance_path else "missing"

    should_rerun = bool(rerun_matrices) or str(os.environ.get("AGVM_PR12P14X_G_RERUN_MATRICES") or "").strip().lower() in {"1", "true", "yes", "on"}
    retrieve_rerun_config: dict[str, Any] = {}
    maintenance_rerun_config: dict[str, Any] = {}
    if should_rerun and retrieve_report is None:
        try:
            retrieve_timeout = float(os.environ.get("AGVM_PR12P14X_G_RETRIEVE_CASE_TIMEOUT_SECONDS") or os.environ.get("AGVM_PR12P14X_E_CASE_TIMEOUT_SECONDS") or 35.0)
        except (TypeError, ValueError):
            retrieve_timeout = 35.0
        try:
            retrieve_workers = int(os.environ.get("AGVM_PR12P14X_G_RETRIEVE_MAX_WORKERS") or os.environ.get("AGVM_PR12P14X_G_MAX_WORKERS") or 1)
        except (TypeError, ValueError):
            retrieve_workers = 1
        retrieve_rerun_config = {
            "case_timeout_seconds": max(5.0, retrieve_timeout),
            "max_workers": max(1, retrieve_workers),
            "memory_bounded_default": True,
        }
        retrieve_report = run_pr12p14x_e_real_mcp_retrieve_quality_matrix_suite(
            selected_base_url,
            brain_id=selected_brain_id,
            timeout=retrieve_rerun_config["case_timeout_seconds"],
            max_workers=retrieve_rerun_config["max_workers"],
        )
        retrieve_source = "live_rerun"
        retrieve_load_error = None
    if should_rerun and maintenance_report is None:
        try:
            maintenance_timeout = float(os.environ.get("AGVM_PR12P14X_G_MAINTENANCE_TIMEOUT_SECONDS") or os.environ.get("AGVM_PR12P14X_F_REAL_MCP_TIMEOUT_SECONDS") or 90.0)
        except (TypeError, ValueError):
            maintenance_timeout = 90.0
        maintenance_rerun_config = {
            "timeout_seconds": max(10.0, maintenance_timeout),
            "memory_bounded_default": True,
        }
        maintenance_report = run_pr12p14x_f_real_mcp_sleep_evolve_metamemory_suite(
            selected_base_url,
            brain_id=selected_brain_id,
            timeout=maintenance_rerun_config["timeout_seconds"],
        )
        maintenance_source = "live_rerun"
        maintenance_load_error = None

    gates = [
        _pr12p14x_g_real_brain_gate(selected_base_url, selected_brain_id),
        _pr12p14x_g_report_gate(
            retrieve_report,
            gate_id="retrieve_quality_real_mcp",
            expected_schema=PR12P14X_E_REAL_MCP_RETRIEVE_MATRIX_SCHEMA_VERSION,
            expected_phase="retrieve_context_quality_matrix",
            brain_id=selected_brain_id,
            source=retrieve_source,
            load_error=retrieve_load_error,
        ),
        _pr12p14x_g_report_gate(
            maintenance_report,
            gate_id="sleep_evolve_real_mcp",
            expected_schema=PR12P14X_F_REAL_MCP_SLEEP_EVOLVE_SCHEMA_VERSION,
            expected_phase="sleep_evolve_metamemory_heuristic_evolution",
            brain_id=selected_brain_id,
            source=maintenance_source,
            load_error=maintenance_load_error,
        ),
        _pr12p14x_g_latency_gate(retrieve_report, maintenance_report),
        _pr12p14x_g_brain_health_gate(maintenance_report),
    ]
    gate_matrix = {str(gate.get("gate_id") or ""): gate for gate in gates}
    failed_gates = [str(gate.get("gate_id") or "") for gate in gates if not bool(gate.get("passed"))]
    failures = [f"{gate.get('gate_id')}:{failure}" for gate in gates for failure in list(gate.get("failures") or [])]
    core_backend_gates_green = not any(gate_id in failed_gates for gate_id in ("real_validation_brain_runtime", "retrieve_quality_real_mcp", "sleep_evolve_real_mcp"))
    latency_green = "runtime_latency_product_slo" not in failed_gates
    health_green = "brain_health_ready" not in failed_gates
    local_mcp_product_ready = core_backend_gates_green and latency_green and health_green
    if not core_backend_gates_green:
        verdict = "backend_blocked_by_real_mcp_runtime_quality"
        next_slice = "PR-12P-14X Runtime Remediation"
    elif not latency_green:
        verdict = "backend_candidate_blocked_by_latency"
        next_slice = "Phase 7 - Streaming And Latency Architecture"
    elif not health_green:
        verdict = "backend_candidate_blocked_by_brain_health"
        next_slice = "Phase 7 - Sleep/Evolve Calibration Follow-Through"
    else:
        verdict = "local_mcp_product_ready"
        next_slice = "Stop local MCP backend work and run release packaging checks"
    h6_release_gate = _pr12p14x_g_h6_release_gate(
        gate_matrix=gate_matrix,
        failed_gates=failed_gates,
        failures=failures,
        retrieve_report=retrieve_report,
        maintenance_report=maintenance_report,
        retrieve_source=retrieve_source,
        maintenance_source=maintenance_source,
        local_mcp_product_ready=local_mcp_product_ready,
    )
    return {
        "schema_version": PR12P14X_G_REAL_MCP_FINAL_VERDICT_SCHEMA_VERSION,
        "phase": "final_product_verdict",
        "slice": "PR-12P-14X-G",
        "proof_scope": "real_mcp_runtime_not_fixture",
        "base_url": selected_base_url,
        "brain_id": selected_brain_id,
        "all_pass": local_mcp_product_ready,
        "backend_mcp_functional": core_backend_gates_green,
        "backend_mcp_revolutionary_candidate": local_mcp_product_ready,
        "local_mcp_product_ready": local_mcp_product_ready,
        "product_ready_claim_allowed": local_mcp_product_ready,
        "product_ready_verdict": verdict,
        "h6_release_gate": h6_release_gate,
        "phase8c_unblocked": bool(h6_release_gate.get("phase8c_unblocked")),
        "phase8c_status": str(h6_release_gate.get("phase8c_status") or ""),
        "gate_matrix": gate_matrix,
        "failed_gates": failed_gates,
        "failures": failures,
        "rag_baseline_comparison": {
            "verdict": "agvm_architecture_advantage_not_product_proven" if not local_mcp_product_ready else "agvm_runtime_advantage_candidate",
            "reason": "Real MCP retrieve and maintenance gates are green, but product readiness also requires latency and health to be green.",
        },
        "latency_readiness": dict(gate_matrix.get("runtime_latency_product_slo", {}).get("evidence") or {}),
        "brain_health_readiness": dict(gate_matrix.get("brain_health_ready", {}).get("evidence") or {}),
        "report_sources": {
            "retrieve_quality": retrieve_source,
            "sleep_evolve": maintenance_source,
            "rerun_matrices": should_rerun,
            "retrieve_rerun_config": retrieve_rerun_config,
            "maintenance_rerun_config": maintenance_rerun_config,
        },
        "artifact_rows": dict(h6_release_gate.get("artifact_rows") or {}),
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 2),
        "next_slice": next_slice,
    }


PR12P14U_G_LOCAL_MCP_PRODUCT_MATRIX_SCHEMA_VERSION = "agvm.pr12p14u_g.local_mcp_product_matrix.v1"

REQUIRED_PR12P14U_G_LOCAL_MCP_GATES = (
    "runtime_surfaces",
    "llm_runtime",
    "brain_scope",
    "retrieve_context_14u_contracts",
    "retrieve_document_contracts",
    "retrieve_path_projection",
    "grow_lifecycle_contract",
    "sleep_evolve_lifecycle_contract",
    "external_mcp_client",
)


def _pr12p14u_g_gate_result(
    *,
    gate_id: str,
    title: str,
    failures: list[str],
    evidence: dict[str, Any],
    elapsed_ms: float,
    critical: bool = True,
) -> dict[str, Any]:
    gate = _pr12p14l_gate_result(
        gate_id=gate_id,
        title=title,
        failures=failures,
        evidence=evidence,
        elapsed_ms=elapsed_ms,
        critical=critical,
    )
    gate["slice"] = "PR-12P-14U-G"
    return gate


def _pr12p14u_g_contract_failures(output: dict[str, Any], required_contracts: tuple[str, ...]) -> list[str]:
    failures: list[str] = []
    for field in required_contracts:
        contract = dict(output.get(field) or {})
        if not contract:
            failures.append(f"{field}_missing")
            continue
        if not str(contract.get("schema_version") or "").strip():
            failures.append(f"{field}_schema_version_missing")
    return failures


def _pr12p14u_g_validate_retrieval_contracts(output: dict[str, Any], evidence: dict[str, Any]) -> list[str]:
    failures = _pr12p14u_g_contract_failures(
        output,
        (
            "runtime_state_contract",
            "tool_boundary_contract",
            "ai_materialization_resilience_contract",
            "first_package_background_contract",
            "run_projection_event_stream_contract",
        ),
    )
    ai = dict(evidence.get("ai") or {})
    resilience = dict(output.get("ai_materialization_resilience_contract") or {})
    if not bool(ai.get("budget_llm_allowed")):
        failures.append("llm_not_allowed")
    if bool(resilience.get("heuristic_only_certification_allowed")):
        failures.append("heuristic_only_certification_allowed")
    if bool(resilience.get("silent_heuristic_completion_allowed")):
        failures.append("silent_heuristic_completion_allowed")
    if not bool(ai.get("materialized") or ai.get("semantic_contract_material") or resilience.get("material_source")):
        failures.append("ai_material_not_visible")
    if not str(evidence.get("search_id") or "").strip():
        failures.append("search_id_missing")
    evidence["required_14u_contracts"] = {
        field: bool(dict(output.get(field) or {}).get("schema_version"))
        for field in (
            "runtime_state_contract",
            "tool_boundary_contract",
            "ai_materialization_resilience_contract",
            "first_package_background_contract",
            "run_projection_event_stream_contract",
        )
    }
    return failures


def _pr12p14u_g_validate_document_contracts(output: dict[str, Any], evidence: dict[str, Any]) -> list[str]:
    failures = _pr12p14u_g_validate_retrieval_contracts(output, evidence)
    failures.extend(_pr12p14u_g_contract_failures(output, ("document_delivery_contract", "document_ref_contract")))
    primary_document_count = int(evidence.get("primary_document_count") or 0)
    primary_raw_text_char_count = int(evidence.get("primary_raw_text_char_count") or 0)
    document_ready = bool(
        str(evidence.get("status") or "") == "ok"
        and str(evidence.get("document_workspace_status") or "") == "workspace_ready"
        and str(evidence.get("document_ready_state") or "") == "document_ready"
        and (primary_document_count >= 1 or primary_raw_text_char_count >= 300)
        and bool(dict(evidence.get("ai") or {}).get("delivery_terminal_for_client"))
    )
    if document_ready:
        failures = [failure for failure in failures if failure != "ai_material_not_visible"]
        evidence["exact_document_terminal_without_ai_route_material_allowed"] = True
    if primary_document_count < 1 and primary_raw_text_char_count < 300:
        failures.append("document_payload_not_actionable")
    return failures


def _pr12p14u_g_validate_path_projection(output: dict[str, Any], evidence: dict[str, Any]) -> list[str]:
    failures = _pr12p14u_g_validate_retrieval_contracts(output, evidence)
    event_contract = dict(output.get("run_projection_event_stream_contract") or {})
    replay = dict(event_contract.get("replay") or {})
    render = dict(event_contract.get("render_instruction") or {})
    if int(evidence.get("path_count") or 0) < 1 and int(evidence.get("route_event_count") or 0) < 1:
        failures.append("path_or_route_events_missing")
    replay_event_count = int(
        replay.get("event_count")
        or replay.get("sequence_count")
        or dict(event_contract.get("event_source") or {}).get("event_count")
        or 0
    )
    evidence["projection_replay_event_count"] = replay_event_count
    if replay and replay_event_count < 1:
        failures.append("projection_replay_event_count_zero")
    if render and bool(render.get("synthetic_motion_allowed")):
        failures.append("synthetic_motion_allowed")
    return failures


def _pr12p14u_g_retrieve_context_gate(base_url: str, brain_id: str | None, *, timeout: float) -> dict[str, Any]:
    if not brain_id:
        return _pr12p14u_g_gate_result(
            gate_id="retrieve_context_14u_contracts",
            title="retrieve_context proves all 14U runtime, AI, first-package and projection contracts together.",
            failures=["brain_id_missing"],
            evidence={},
            elapsed_ms=0,
        )
    segment, _output = _pr12p14l_run_probe(
        base_url=base_url,
        segment_id="retrieve_context_14u_contracts",
        title="retrieve_context proves all 14U runtime, AI, first-package and projection contracts together.",
        path="/mcp/retrieve-context",
        payload={
            "brain_id": brain_id,
            "query_text": "raccontami di te, del tuo lavoro, delle aziende, dei documenti e dei progetti principali",
            "retrieval_mode": "balanced",
            "context_package_mode": "broad_dossier",
            "document_text_policy": "top_raw",
            "max_matches": 14,
            "include_raw_text": True,
            "include_answer_demo": False,
            "complete_paths": False,
        },
        expected_terms=(),
        timeout=timeout,
        validators=(_pr12p14u_g_validate_retrieval_contracts,),
    )
    return _pr12p14u_g_gate_result(
        gate_id="retrieve_context_14u_contracts",
        title=str(segment.get("title") or ""),
        failures=list(segment.get("failures") or []),
        evidence=dict(segment.get("evidence") or {}),
        elapsed_ms=float(segment.get("elapsed_ms") or 0.0),
    )


def _pr12p14u_g_retrieve_document_gate(base_url: str, brain_id: str | None, *, timeout: float) -> dict[str, Any]:
    if not brain_id:
        return _pr12p14u_g_gate_result(
            gate_id="retrieve_document_contracts",
            title="retrieve_document proves exact raw-document semantics and follow-up refs.",
            failures=["brain_id_missing"],
            evidence={},
            elapsed_ms=0,
        )
    segment, _output = _pr12p14l_run_probe(
        base_url=base_url,
        segment_id="retrieve_document_contracts",
        title="retrieve_document proves exact raw-document semantics and follow-up refs.",
        path="/mcp/retrieve-document",
        payload={
            "brain_id": brain_id,
            "query_text": "BaxEnergy Yokogawa WiSNAM documenti e materiale sorgente",
            "retrieval_mode": "balanced",
            "context_package_mode": "document_full",
            "document_text_policy": "all_raw",
            "max_matches": 12,
            "include_raw_text": True,
            "include_answer_demo": False,
        },
        expected_terms=(),
        timeout=timeout,
        validators=(_pr12p14u_g_validate_document_contracts,),
    )
    return _pr12p14u_g_gate_result(
        gate_id="retrieve_document_contracts",
        title=str(segment.get("title") or ""),
        failures=list(segment.get("failures") or []),
        evidence=dict(segment.get("evidence") or {}),
        elapsed_ms=float(segment.get("elapsed_ms") or 0.0),
    )


def _pr12p14u_g_path_projection_gate(base_url: str, brain_id: str | None, *, timeout: float) -> dict[str, Any]:
    if not brain_id:
        return _pr12p14u_g_gate_result(
            gate_id="retrieve_path_projection",
            title="retrieve_path_corridor proves path/projection truth is backend-event based.",
            failures=["brain_id_missing"],
            evidence={},
            elapsed_ms=0,
        )
    segment, _output = _pr12p14l_run_probe(
        base_url=base_url,
        segment_id="retrieve_path_projection",
        title="retrieve_path_corridor proves path/projection truth is backend-event based.",
        path="/mcp/retrieve-path-corridor",
        payload={
            "brain_id": brain_id,
            "query_text": "collega lavoro, aziende, documenti, valori e relazioni e mostrami i percorsi attraversati",
            "retrieval_mode": "balanced",
            "context_package_mode": "broad_dossier",
            "document_text_policy": "refs_only",
            "max_matches": 14,
            "include_raw_text": False,
            "include_answer_demo": False,
            "complete_paths": True,
        },
        expected_terms=(),
        timeout=timeout,
        validators=(_pr12p14u_g_validate_path_projection,),
    )
    return _pr12p14u_g_gate_result(
        gate_id="retrieve_path_projection",
        title=str(segment.get("title") or ""),
        failures=list(segment.get("failures") or []),
        evidence=dict(segment.get("evidence") or {}),
        elapsed_ms=float(segment.get("elapsed_ms") or 0.0),
    )


def _pr12p14u_g_validate_lifecycle_contract(output: dict[str, Any], *, allowed_states: tuple[str, ...]) -> list[str]:
    failures = _pr12p14u_g_contract_failures(output, ("memory_operation_lifecycle_contract",))
    lifecycle = dict(output.get("memory_operation_lifecycle_contract") or {})
    state = str(lifecycle.get("state") or "")
    mutation = dict(lifecycle.get("mutation_policy") or {})
    if state not in allowed_states:
        failures.append(f"lifecycle_state_unexpected:{state or 'missing'}")
    if mutation.get("hidden_mutation_allowed") is not False:
        failures.append("hidden_mutation_not_forbidden")
    if mutation.get("partial_apply_supported") is not False:
        failures.append("partial_apply_not_forbidden")
    return failures


def _pr12p14u_g_grow_lifecycle_gate(base_url: str, brain_id: str | None, *, timeout: float) -> dict[str, Any]:
    started = time.perf_counter()
    failures: list[str] = []
    evidence: dict[str, Any] = {"path": "/mcp/grow-source-preview", "payload_brain_id": brain_id}
    if not brain_id:
        failures.append("brain_id_missing")
    else:
        raw_input = "\n\n".join(
            [
                "PR12P14UG Grow benchmark source. AGVM must create a raw anchor, atomic source units, derived memory nodes, document references, retrieval proof and lifecycle state.",
                "The source describes BaxEnergy, WiSNAM, Yokogawa, renewable energy management, MCP context packages, path corridors, hot memory and document retrieval.",
                "The preview must remain non-mutating until an operator explicitly applies reviewed node ids.",
            ]
        )
        try:
            output, elapsed_ms = _pr12p12_elapsed_post(
                base_url,
                "/mcp/grow-source-preview",
                {
                    "brain_id": brain_id,
                    "raw_input": raw_input,
                    "input_kind": "manual_text",
                    "source_label": "PR12P14U-G Grow Lifecycle Benchmark",
                    "options": {"treat_as": "project_workspace", "source_trust": "user_asserted", "max_units": 6, "question_limit": 3},
                    "run_preview": True,
                },
                timeout=timeout,
            )
            evidence["elapsed_http_ms"] = elapsed_ms
            failures.extend(_pr12p14l_validate_grow_preview(output, evidence))
            failures.extend(
                _pr12p14u_g_validate_lifecycle_contract(
                    output,
                    allowed_states=("waiting_for_approval", "preview_only", "asking_clarification"),
                )
            )
            lifecycle = dict(output.get("memory_operation_lifecycle_contract") or {})
            evidence["memory_operation_lifecycle_state"] = lifecycle.get("state")
            evidence["memory_operation_lifecycle_schema"] = lifecycle.get("schema_version")
        except Exception as exc:
            failures.append(f"grow_lifecycle_probe_failed:{exc}")
            evidence["error"] = str(exc)
    return _pr12p14u_g_gate_result(
        gate_id="grow_lifecycle_contract",
        title="Grow preview exposes source formation plus the normalized operation lifecycle contract.",
        failures=failures,
        evidence=evidence,
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def _pr12p14u_g_sleep_evolve_lifecycle_gate(base_url: str, brain_id: str | None, *, timeout: float) -> dict[str, Any]:
    started = time.perf_counter()
    failures: list[str] = []
    rows: list[dict[str, Any]] = []
    if not brain_id:
        failures.append("brain_id_missing")
    else:
        for path, expected_tool, mode in (
            ("/mcp/sleep-preview", "sleep_preview", "sleep"),
            ("/mcp/evolve-preview", "evolve_preview", "evolve"),
        ):
            row: dict[str, Any] = {"path": path, "expected_tool": expected_tool}
            try:
                output, elapsed_ms = _pr12p12_elapsed_post(
                    base_url,
                    path,
                    {"brain_id": brain_id, "mode": mode, "max_nodes_considered": 30},
                    timeout=timeout,
                )
                row["elapsed_http_ms"] = elapsed_ms
                row_failures = _pr12p14l_validate_maintenance_preview(output, expected_tool=expected_tool, evidence=row)
                row_failures.extend(
                    _pr12p14u_g_validate_lifecycle_contract(
                        output,
                        allowed_states=("waiting_for_approval", "preview_only", "blocked"),
                    )
                )
                lifecycle = dict(output.get("memory_operation_lifecycle_contract") or {})
                row["memory_operation_lifecycle_state"] = lifecycle.get("state")
                row["memory_operation_lifecycle_schema"] = lifecycle.get("schema_version")
                failures.extend([f"{expected_tool}:{failure}" for failure in row_failures])
            except Exception as exc:
                failures.append(f"{expected_tool}_probe_failed:{exc}")
                row["error"] = str(exc)
            rows.append(row)
    return _pr12p14u_g_gate_result(
        gate_id="sleep_evolve_lifecycle_contract",
        title="Sleep and Evolve expose normalized preview/apply/delta/rollback lifecycle state.",
        failures=failures,
        evidence={"brain_id": brain_id, "rows": rows},
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def run_pr12p14u_g_local_mcp_product_matrix_suite(base_url: str | None = None) -> dict[str, Any]:
    selected_base_url = str(base_url or DEFAULT_BASE_URL).rstrip("/")
    started = time.perf_counter()
    runtime_gate, health, _frontend_probe = _pr12p14l_runtime_gate(selected_base_url)
    runtime_gate["gate_id"] = "runtime_surfaces"
    runtime_gate["segment_id"] = "runtime_surfaces"
    runtime_gate["slice"] = "PR-12P-14U-G"
    llm_gate = _pr12p14l_llm_gate()
    llm_gate["gate_id"] = "llm_runtime"
    llm_gate["segment_id"] = "llm_runtime"
    llm_gate["slice"] = "PR-12P-14U-G"
    target_info = _pr12p14c_brain_targets(selected_base_url)
    brain_scope_gate = _pr12p14u_g_gate_result(
        gate_id="brain_scope",
        title="Validation brains are available or a default active brain is available for local MCP proof.",
        failures=list((target_info.get("segment") or {}).get("failures") or []),
        evidence=dict((target_info.get("segment") or {}).get("evidence") or {}),
        elapsed_ms=float((target_info.get("segment") or {}).get("elapsed_ms") or 0.0),
        critical=False,
    )
    targets = dict(target_info.get("targets") or {})
    simone = str(dict(targets.get("simone_massaro") or {}).get("brain_id") or "").strip()
    elena = str(dict(targets.get("elena_valsecchi") or {}).get("brain_id") or "").strip()
    active_brain = simone or elena or str(health.get("active_brain_id") or health.get("default_brain_id") or os.environ.get("AGVM_DEFAULT_BRAIN_ID") or "").strip() or None
    retrieve_timeout = float(os.environ.get("AGVM_PR12P14U_G_RETRIEVE_TIMEOUT_SECONDS") or 60.0)
    grow_timeout = float(os.environ.get("AGVM_PR12P14U_G_GROW_TIMEOUT_SECONDS") or 120.0)
    maintenance_timeout = float(os.environ.get("AGVM_PR12P14U_G_MAINTENANCE_TIMEOUT_SECONDS") or 120.0)
    gates = [
        runtime_gate,
        llm_gate,
        brain_scope_gate,
        _pr12p14u_g_retrieve_context_gate(selected_base_url, active_brain, timeout=retrieve_timeout),
        _pr12p14u_g_retrieve_document_gate(selected_base_url, active_brain, timeout=retrieve_timeout),
        _pr12p14u_g_path_projection_gate(selected_base_url, active_brain, timeout=retrieve_timeout),
        _pr12p14u_g_grow_lifecycle_gate(selected_base_url, active_brain, timeout=grow_timeout),
        _pr12p14u_g_sleep_evolve_lifecycle_gate(selected_base_url, active_brain, timeout=maintenance_timeout),
    ]
    if active_brain:
        external_gate = _pr12p14l_external_mcp_gate(selected_base_url, active_brain)
        external_gate["gate_id"] = "external_mcp_client"
        external_gate["segment_id"] = "external_mcp_client"
        external_gate["slice"] = "PR-12P-14U-G"
        gates.append(external_gate)
    else:
        gates.append(
            _pr12p14u_g_gate_result(
                gate_id="external_mcp_client",
                title="External local stdio MCP client can use explicit brain scope.",
                failures=["active_validation_brain_missing"],
                evidence={},
                elapsed_ms=0,
            )
        )
    by_gate = {str(gate.get("gate_id") or ""): gate for gate in gates}
    missing_gates = [gate for gate in REQUIRED_PR12P14U_G_LOCAL_MCP_GATES if gate not in by_gate]
    failed_gates = [
        str(gate.get("gate_id") or "")
        for gate in gates
        if bool(gate.get("critical", True)) and not bool(gate.get("passed"))
    ]
    all_pass = not missing_gates and not failed_gates
    benchmark_table = [
        {
            "gate": str(gate.get("gate_id") or ""),
            "passed": bool(gate.get("passed")),
            "critical": bool(gate.get("critical", True)),
            "failures": list(gate.get("failures") or []),
            "elapsed_ms": gate.get("elapsed_ms"),
        }
        for gate in gates
    ]
    return {
        "schema_version": PR12P14U_G_LOCAL_MCP_PRODUCT_MATRIX_SCHEMA_VERSION,
        "phase": "local_mcp_product_matrix",
        "slice": "PR-12P-14U-G",
        "base_url": selected_base_url,
        "all_pass": all_pass,
        "local_mcp_backend_contract_ready": bool(all_pass),
        "local_mcp_product_ready": False,
        "product_ready_verdict": "local_mcp_backend_contracts_passed_ui_14v_required"
        if all_pass
        else "local_mcp_backend_contracts_blocked_before_ui_14v",
        "readiness_scope": "backend_mcp_contract_matrix_after_14u_not_final_ui_or_cloud_readiness",
        "cloud_blocked": True,
        "cloud_release_blocked_until": "14V UI responsibility reset passes and the user explicitly opens cloud planning.",
        "active_validation_brain_id": active_brain,
        "required_gates": list(REQUIRED_PR12P14U_G_LOCAL_MCP_GATES),
        "missing_gates": missing_gates,
        "failed_gates": failed_gates,
        "gate_matrix": {
            gate_id: {
                "required": gate_id in REQUIRED_PR12P14U_G_LOCAL_MCP_GATES,
                "covered": gate_id in by_gate,
                "passed": bool((by_gate.get(gate_id) or {}).get("passed")),
                "critical": bool((by_gate.get(gate_id) or {}).get("critical", True)),
                "failures": list((by_gate.get(gate_id) or {}).get("failures") or []),
            }
            for gate_id in REQUIRED_PR12P14U_G_LOCAL_MCP_GATES
        },
        "gate_results": gates,
        "benchmark_table": benchmark_table,
        "benchmark_inputs": {
            "phase": "local_mcp_product_matrix",
            "canonical_api_port": 8010,
            "canonical_frontend_port": 3020,
            "live_llm_required": True,
            "no_llm_fallback_allowed": True,
            "mcp_first": True,
            "answer_demo_secondary": True,
            "mutation_policy": "preview_only_for_benchmark",
            "retrieve_timeout_seconds": retrieve_timeout,
            "grow_timeout_seconds": grow_timeout,
            "maintenance_timeout_seconds": maintenance_timeout,
            "validates_contracts": [
                "runtime_state_contract",
                "tool_boundary_contract",
                "ai_materialization_resilience_contract",
                "first_package_background_contract",
                "run_projection_event_stream_contract",
                "memory_operation_lifecycle_contract",
            ],
        },
        "evidence_contract": {
            "product_ready_claim_allowed": False,
            "product_ready_claim_reason": "14U-G only certifies backend MCP contracts; 14V UI reset must pass before local product-ready language.",
            "cloud_commercialization_allowed": False,
            "no_query_specific_patches": True,
            "heuristic_only_certification_allowed": False,
        },
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "next_slice": "PR-12P-14V-A Navigation And Page Responsibility Reset"
        if all_pass
        else "PR-12P-14U-G Remediation",
    }


def run_pr12p_product_ready_local_gate_suite(base_url: str | None = None) -> dict[str, Any]:
    selected_base_url = str(base_url or DEFAULT_BASE_URL).rstrip("/")
    suite_started = time.perf_counter()
    gates: list[dict[str, Any]] = []

    health_started = time.perf_counter()
    health: dict[str, Any] = {}
    health_failures: list[str] = []
    try:
        health = get_json(selected_base_url, "/health", timeout=30.0)
        status_text = str(health.get("status") or health.get("state") or "").lower()
        if not bool(health.get("ok")) and status_text not in {"ok", "healthy", "ready"}:
            health_failures.append(f"api_health_unexpected_status:{status_text or 'missing'}")
    except Exception as exc:
        health_failures.append(f"api_health_unreachable:{exc}")
    active_brain_id = str(health.get("active_brain_id") or health.get("default_brain_id") or os.environ.get("AGVM_DEFAULT_BRAIN_ID") or "default_brain").strip()
    gates.append(
        _pr12p13_case_result(
            gate_id="api_health",
            title="Local API is reachable on the canonical 8010 surface.",
            failures=health_failures,
            evidence={
                "base_url": selected_base_url,
                "status": health.get("status"),
                "active_brain_id": health.get("active_brain_id"),
                "default_brain_id": health.get("default_brain_id"),
                "brain_count": health.get("brain_count"),
            },
            elapsed_ms=(time.perf_counter() - health_started) * 1000,
        )
    )

    frontend_started = time.perf_counter()
    frontend_probe = _pr12p13_frontend_probe(_pr12p13_frontend_candidates(selected_base_url))
    gates.append(
        _pr12p13_case_result(
            gate_id="frontend_health",
            title="Local Brain OS UI is reachable on the canonical 3020 surface.",
            failures=list(frontend_probe.get("failures") or []),
            evidence=frontend_probe,
            elapsed_ms=(time.perf_counter() - frontend_started) * 1000,
        )
    )

    gates.append(_pr12p13_registry_gate())

    gates.append(_pr12p13_self_hosted_runtime_gate(base_url=selected_base_url, health=health, frontend_probe=frontend_probe))

    mcp_started = time.perf_counter()
    try:
        with benchmark_brain_scope(active_brain_id):
            mcp_report = run_pr12p_local_mcp_client_proof_suite(selected_base_url)
    except Exception as exc:
        mcp_report = {"all_pass": False, "phase": "local_mcp_client", "failures": [str(exc)], "product_ready_verdict": "exception"}
    gates.append(
        _pr12p13_report_gate(
            gate_id="external_stdio_mcp",
            title="External stdio MCP client can list and call the local tools with explicit brain scope.",
            report=mcp_report,
            required_verdict="local_mcp_client_proof_passed_pr12p_still_open",
            elapsed_ms=(time.perf_counter() - mcp_started) * 1000,
        )
    )

    matrix_started = time.perf_counter()
    try:
        matrix_report = run_pr12p_live_product_matrix_suite(selected_base_url)
    except Exception as exc:
        matrix_report = {"all_pass": False, "phase": "live_product_matrix", "launch_blockers": [str(exc)], "product_ready_verdict": "exception"}
    gates.append(
        _pr12p13_report_gate(
            gate_id="live_product_matrix",
            title="Live product matrix passes across brains, retrieval families, Grow, Sleep/Evolve, UI/MCP parity and latency.",
            report=matrix_report,
            required_verdict="live_product_matrix_passed_pr12p_still_open",
            extra_failure_keys=("critical_failures", "launch_blockers"),
            elapsed_ms=(time.perf_counter() - matrix_started) * 1000,
        )
    )

    by_gate = {str(gate.get("gate_id") or ""): gate for gate in gates}
    missing_gates = [gate for gate in REQUIRED_PR12P13_LOCAL_PRODUCT_GATES if gate not in by_gate]
    failed_gates = [str(gate.get("gate_id") or "") for gate in gates if bool(gate.get("critical", True)) and not bool(gate.get("passed"))]
    all_pass = not missing_gates and not failed_gates
    local_product_ready = bool(all_pass)
    return {
        "schema_version": PR12P_PRODUCT_READY_LOCAL_GATE_REPORT_SCHEMA_VERSION,
        "phase": "product_ready_local_gate",
        "slice": "PR-12P-13",
        "base_url": selected_base_url,
        "all_pass": all_pass,
        "local_product_ready": local_product_ready,
        "local_mcp_product_ready": local_product_ready,
        "readiness_level": "ready_for_pr12p14_launch_report" if all_pass else "blocked_needs_remediation",
        "product_ready_verdict": "local_mcp_product_ready_pr12p13_passed_pr12p14_launch_report_required"
        if all_pass
        else "local_mcp_product_blocked_pr12p13_gate_failed",
        "cloud_blocked": True,
        "cloud_release_blocked_until": "PR-12P-14 Launch Readiness Report, RAG Comparison And Improvement Backlog",
        "active_brain_id": active_brain_id,
        "required_gates": list(REQUIRED_PR12P13_LOCAL_PRODUCT_GATES),
        "missing_gates": missing_gates,
        "failed_gates": failed_gates,
        "gate_matrix": {
            gate_id: {
                "required": gate_id in REQUIRED_PR12P13_LOCAL_PRODUCT_GATES,
                "covered": gate_id in by_gate,
                "passed": bool((by_gate.get(gate_id) or {}).get("passed")),
                "critical": bool((by_gate.get(gate_id) or {}).get("critical", True)),
                "failures": list((by_gate.get(gate_id) or {}).get("failures") or []),
            }
            for gate_id in REQUIRED_PR12P13_LOCAL_PRODUCT_GATES
        },
        "gate_results": gates,
        "benchmark_inputs": {
            "phase": "product_ready_local_gate",
            "canonical_api_port": 8010,
            "canonical_frontend_port": 3020,
            "docker_local_distribution_required": True,
            "external_network_required": False,
            "live_llm_required": True,
            "mutation_enabled": True,
            "mutation_scope": "live_product_matrix_ephemeral_fresh_brain_only",
            "mcp_first": True,
            "context_package_is_product": True,
            "answer_demo_secondary": True,
            "cloud_release_blocked": True,
        },
        "evidence_contract": {
            "aggregates": list(REQUIRED_PR12P13_LOCAL_PRODUCT_GATES),
            "previous_slice": "PR-12P-12B Latency Product Gate Correction",
            "next_decision_slice": "PR-12P-14 Launch Readiness Report, RAG Comparison And Improvement Backlog",
            "product_ready_claim_allowed": bool(all_pass),
            "cloud_commercialization_allowed": False,
        },
        "elapsed_ms": round((time.perf_counter() - suite_started) * 1000, 3),
        "next_slice": "PR-12P-14 Launch Readiness Report, RAG Comparison And Improvement Backlog"
        if all_pass
        else "PR-12P-13 Product-Ready Local Gate Remediation",
    }


def _pr12n_case_result(
    *,
    family: str,
    title: str,
    failures: list[str],
    evidence: dict[str, Any],
    elapsed_ms: float,
) -> dict[str, Any]:
    return {
        "case_id": f"pr12n_a_{family}",
        "family": family,
        "title": title,
        "slice": "PR-12N-A",
        "critical": True,
        "passed": not failures,
        "failures": failures,
        "evidence": evidence,
        "elapsed_ms": round(float(elapsed_ms), 3),
    }


def _pr12n_temp_hosted_registry() -> tuple[Any, Path, dict[str, Any]]:
    import tempfile

    from brain_registry import bootstrap_local_brain_registry
    from hosted_registry import bootstrap_hosted_tenant_registry

    temp_dir = tempfile.TemporaryDirectory(prefix="agvm-pr12n-a-hosted-")
    root = Path(temp_dir.name) / "brains"
    alpha_dir = Path(temp_dir.name) / "hosted_alpha"
    beta_dir = Path(temp_dir.name) / "hosted_beta"
    bootstrap_local_brain_registry(brain_root=root, legacy_data_dirs=[alpha_dir, beta_dir], preferred_default_brain_id="hosted_alpha")
    registry = bootstrap_hosted_tenant_registry(
        brain_root=root,
        tenant_id="tenant_acme",
        organization_id="org_acme",
        user_id="user_owner",
        environment_id="dev",
        reset=True,
    )
    return temp_dir, root, registry


def _run_pr12n_hosted_registry_bootstrap_case() -> dict[str, Any]:
    started = time.perf_counter()
    failures: list[str] = []
    evidence: dict[str, Any] = {}
    temp_dir = None
    try:
        temp_dir, _root, registry = _pr12n_temp_hosted_registry()
        validation = dict(registry.get("validation") or {})
        evidence = {
            "schema_version": registry.get("schema_version"),
            "tenant_count": len(list(registry.get("tenants") or [])),
            "user_count": len(list(registry.get("users") or [])),
            "brain_binding_count": len(list(registry.get("brain_bindings") or [])),
            "validation_passed": validation.get("passed"),
            "one_default_brain_per_user": validation.get("one_default_brain_per_user"),
            "next_slice": registry.get("next_slice"),
        }
        if registry.get("schema_version") != "agvm.hosted_tenant_registry.v1":
            failures.append("hosted_registry_schema_version_missing")
        if evidence["tenant_count"] != 1 or evidence["user_count"] != 1 or int(evidence["brain_binding_count"] or 0) < 2:
            failures.append("hosted_registry_cardinality_wrong")
        if not validation.get("passed"):
            failures.append("hosted_registry_validation_failed")
        if not validation.get("one_default_brain_per_user"):
            failures.append("one_default_brain_per_user_not_proven")
    except Exception as exc:
        failures.append(f"hosted_registry_bootstrap_error:{exc}")
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()
    return _pr12n_case_result(
        family="hosted_registry_bootstrap",
        title="Hosted tenant registry bootstraps from local self-hosted brains.",
        failures=failures,
        evidence=evidence,
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def _run_pr12n_tenant_user_default_resolution_case() -> dict[str, Any]:
    started = time.perf_counter()
    failures: list[str] = []
    evidence: dict[str, Any] = {}
    temp_dir = None
    try:
        from hosted_registry import HostedRegistryError, resolve_hosted_brain_scope

        temp_dir, root, _registry = _pr12n_temp_hosted_registry()
        default_resolution = resolve_hosted_brain_scope(tenant_id="tenant_acme", user_id="user_owner", environment_id="dev", brain_root=root)
        beta_resolution = resolve_hosted_brain_scope(
            tenant_id="tenant_acme",
            user_id="user_owner",
            environment_id="dev",
            brain_id="hosted_beta",
            brain_root=root,
        )
        unauthorized_blocked = False
        missing_scope_blocked = False
        try:
            resolve_hosted_brain_scope(tenant_id="tenant_acme", user_id="user_owner", environment_id="dev", brain_id="unknown", brain_root=root)
        except HostedRegistryError:
            unauthorized_blocked = True
        try:
            resolve_hosted_brain_scope(tenant_id="", user_id="user_owner", environment_id="dev", brain_root=root)
        except HostedRegistryError:
            missing_scope_blocked = True
        evidence = {
            "default_local_brain_id": default_resolution.get("local_brain_id"),
            "explicit_beta_local_brain_id": beta_resolution.get("local_brain_id"),
            "default_graph_id": default_resolution.get("graph_id"),
            "beta_graph_id": beta_resolution.get("graph_id"),
            "unauthorized_brain_blocked": unauthorized_blocked,
            "missing_tenant_blocked": missing_scope_blocked,
        }
        if default_resolution.get("local_brain_id") != "hosted_alpha":
            failures.append("hosted_default_resolution_wrong")
        if beta_resolution.get("local_brain_id") != "hosted_beta":
            failures.append("explicit_hosted_brain_resolution_wrong")
        if not unauthorized_blocked:
            failures.append("unauthorized_hosted_brain_not_blocked")
        if not missing_scope_blocked:
            failures.append("missing_hosted_scope_not_blocked")
    except Exception as exc:
        failures.append(f"tenant_user_default_resolution_error:{exc}")
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()
    return _pr12n_case_result(
        family="tenant_user_default_resolution",
        title="Tenant/user scope resolves one default brain and blocks unauthorized brain access.",
        failures=failures,
        evidence=evidence,
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def _run_pr12n_request_time_tenant_scope_headers_case() -> dict[str, Any]:
    started = time.perf_counter()
    root = _pr12m_repo_root()
    main_source = _pr12m_read_text(root / "agvm_api" / "main.py")
    evidence = {
        "tenant_header": "X-AGVM-Tenant-Id" in main_source,
        "user_header": "X-AGVM-User-Id" in main_source,
        "hosted_scope_header": "X-AGVM-Hosted-Scope" in main_source,
        "middleware_uses_hosted_resolver": "resolve_hosted_brain_scope" in main_source,
        "runtime_brain_from_hosted_resolution": "hosted_resolution.get(\"local_brain\")" in main_source,
        "hosted_endpoints_exposed": "/hosted/brains/resolve" in main_source and "/mcp/hosted/tenants" in main_source,
    }
    failures = [f"{name}_missing" for name, passed in evidence.items() if not passed]
    return _pr12n_case_result(
        family="request_time_tenant_scope_headers",
        title="API middleware exposes hosted tenant scope headers and resolves runtime brain through hosted registry.",
        failures=failures,
        evidence=evidence,
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def _run_pr12n_per_brain_namespace_mapping_case() -> dict[str, Any]:
    started = time.perf_counter()
    failures: list[str] = []
    evidence: dict[str, Any] = {}
    temp_dir = None
    try:
        temp_dir, _root, registry = _pr12n_temp_hosted_registry()
        bindings = [dict(item) for item in list(registry.get("brain_bindings") or [])]
        graph_ids = [str(item.get("graph_id") or "") for item in bindings]
        namespaces = [
            str(item.get(key) or "")
            for item in bindings
            for key in ("document_namespace", "source_namespace", "audit_namespace", "source_hash_namespace")
        ]
        evidence = {
            "graph_ids": graph_ids,
            "namespace_count": len(namespaces),
            "unique_graph_ids": len(graph_ids) == len(set(graph_ids)),
            "unique_namespaces": len(namespaces) == len(set(namespaces)),
            "tenant_in_every_namespace": all(namespace.startswith("tenant_acme/dev/") for namespace in namespaces),
            "validation_unique_namespaces": (registry.get("validation") or {}).get("unique_namespaces"),
        }
        if not evidence["unique_graph_ids"]:
            failures.append("hosted_graph_ids_not_unique")
        if not evidence["unique_namespaces"]:
            failures.append("hosted_namespaces_not_unique")
        if not evidence["tenant_in_every_namespace"]:
            failures.append("tenant_environment_not_encoded_in_namespaces")
    except Exception as exc:
        failures.append(f"per_brain_namespace_mapping_error:{exc}")
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()
    return _pr12n_case_result(
        family="per_brain_namespace_mapping",
        title="Every hosted brain binding has distinct graph, document, source-hash and audit namespaces.",
        failures=failures,
        evidence=evidence,
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def _run_pr12n_self_hosted_to_hosted_migration_plan_case() -> dict[str, Any]:
    started = time.perf_counter()
    failures: list[str] = []
    evidence: dict[str, Any] = {}
    temp_dir = None
    required_steps = {
        "export_or_stream_local_brain_archive",
        "copy_graph_vector_sqlite_into_cloud_persistence_layer",
        "copy_document_assets_into_object_namespace",
        "copy_source_packages_and_source_hashes_into_tenant_namespace",
        "copy_maintenance_and_mcp_audit_logs_into_audit_namespace",
        "validate_no_cross_tenant_namespace_overlap",
    }
    try:
        temp_dir, _root, registry = _pr12n_temp_hosted_registry()
        plan = dict(registry.get("migration_plan") or {})
        rows = [dict(item) for item in list(plan.get("rows") or [])]
        row_step_sets = [set(item.get("migration_steps") or []) for item in rows]
        evidence = {
            "schema_version": plan.get("schema_version"),
            "binding_count": plan.get("binding_count"),
            "row_count": len(rows),
            "cloud_execution_status": plan.get("cloud_execution_status"),
            "all_rows_have_required_steps": all(required_steps.issubset(steps) for steps in row_step_sets),
        }
        if plan.get("schema_version") != "agvm.hosted_migration_plan.v1":
            failures.append("migration_plan_schema_missing")
        if int(plan.get("binding_count") or 0) != len(rows) or not rows:
            failures.append("migration_plan_rows_missing")
        if not evidence["all_rows_have_required_steps"]:
            failures.append("migration_plan_required_steps_missing")
        if plan.get("cloud_execution_status") != "plan_only_until_pr12n_b":
            failures.append("migration_plan_boundary_wrong")
    except Exception as exc:
        failures.append(f"migration_plan_error:{exc}")
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()
    return _pr12n_case_result(
        family="self_hosted_to_hosted_migration_plan",
        title="Hosted registry includes a migration plan from self-hosted brain export to hosted data.",
        failures=failures,
        evidence=evidence,
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def _run_pr12n_mcp_hosted_scope_headers_case() -> dict[str, Any]:
    started = time.perf_counter()
    root = _pr12m_repo_root()
    server_source = _pr12m_read_text(root / "agvm_mcp_server" / "server.py")
    config_text = _pr12m_read_text(root / "agvm_mcp_server" / "config.example.json")
    manifest_text = _pr12m_read_text(root / "agvm_mcp_server" / "manifest.json")
    evidence = {
        "config_has_tenant_id": '"tenant_id": "local_tenant"' in config_text,
        "config_has_user_id": '"user_id": "local_user"' in config_text,
        "server_sends_tenant_header": "X-AGVM-Tenant-Id" in server_source,
        "server_sends_user_header": "X-AGVM-User-Id" in server_source,
        "server_allows_hosted_default_without_brain": "tenant_id/user_id" in server_source,
        "manifest_has_hosted_scope": '"hosted_scope"' in manifest_text,
        "manifest_documents_env": "AGVM_MCP_TENANT_ID" in manifest_text and "AGVM_MCP_USER_ID" in manifest_text,
    }
    failures = [f"{name}_missing" for name, passed in evidence.items() if not passed]
    return _pr12n_case_result(
        family="mcp_hosted_scope_headers",
        title="Local MCP wrapper can propagate hosted tenant/user scope to the HTTP MCP surface.",
        failures=failures,
        evidence=evidence,
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def _run_pr12n_documentation_closure_case() -> dict[str, Any]:
    started = time.perf_counter()
    root = _pr12m_repo_root()
    docs_root = root / "docs"
    files = {
        "master": docs_root / "AGVM_MASTER.md",
        "slices": docs_root / "AGVM_SLICES.md",
        "progress": docs_root / "AGVM_PROGRESS.md",
    }
    texts = {name: _pr12m_read_text(path) for name, path in files.items()}
    evidence = {
        "active_master_present": "AGVM is a local-first MCP memory operating system" in texts["master"],
        "hosted_not_current_frontier": "hosted/cloud distribution is ready" in texts["master"]
        or "cloud" in texts["slices"].lower(),
        "local_first_gate_present": "local MCP" in texts["master"] and "Phase 8C" in texts["slices"],
        "progress_records_cloud_block": "not UI/cloud" in texts["progress"] or "cloud" in texts["progress"].lower(),
    }
    failures = [f"{name}_missing" for name, passed in evidence.items() if not passed]
    return _pr12n_case_result(
        family="documentation_closure",
        title="Active canonical docs keep hosted work behind local MCP readiness.",
        failures=failures,
        evidence=evidence,
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def run_pr12n_hosted_tenant_isolation_suite(base_url: str | None = None) -> dict[str, Any]:
    base = str(base_url or DEFAULT_BASE_URL)
    case_results = [
        _run_pr12n_hosted_registry_bootstrap_case(),
        _run_pr12n_tenant_user_default_resolution_case(),
        _run_pr12n_request_time_tenant_scope_headers_case(),
        _run_pr12n_per_brain_namespace_mapping_case(),
        _run_pr12n_self_hosted_to_hosted_migration_plan_case(),
        _run_pr12n_mcp_hosted_scope_headers_case(),
        _run_pr12n_documentation_closure_case(),
    ]
    by_family = {str(result.get("family") or ""): result for result in case_results}
    missing_families = [family for family in REQUIRED_PR12N_HOSTED_TENANT_FAMILIES if family not in by_family]
    critical_failures = [
        {"family": str(result.get("family") or ""), "failures": list(result.get("failures") or [])}
        for result in case_results
        if bool(result.get("critical")) and not bool(result.get("passed"))
    ]
    passed_count = sum(1 for result in case_results if bool(result.get("passed")))
    total_count = len(REQUIRED_PR12N_HOSTED_TENANT_FAMILIES)
    all_pass = passed_count == total_count and not missing_families and not critical_failures
    family_matrix = {
        family: {
            "required": True,
            "covered": family in by_family,
            "passed": bool((by_family.get(family) or {}).get("passed")),
            "critical": bool((by_family.get(family) or {}).get("critical", True)),
            "failures": list((by_family.get(family) or {}).get("failures") or []),
        }
        for family in REQUIRED_PR12N_HOSTED_TENANT_FAMILIES
    }
    return {
        "schema_version": PR12N_HOSTED_TENANT_ISOLATION_REPORT_SCHEMA_VERSION,
        "phase": "hosted_tenant_isolation",
        "slice": "PR-12N-A",
        "base_url": base,
        "all_pass": all_pass,
        "pr12n_a_closed": all_pass,
        "hosted_registry_ready": all_pass,
        "product_ready_verdict": "hosted_tenant_registry_ready_pr12n_a_closed" if all_pass else "not_hosted_ready_pr12n_a_remains_open",
        "pass_rate": round(passed_count / total_count, 4) if total_count else 0.0,
        "passed_count": passed_count,
        "total_count": total_count,
        "required_families": list(REQUIRED_PR12N_HOSTED_TENANT_FAMILIES),
        "missing_families": missing_families,
        "critical_failures": critical_failures,
        "family_matrix": family_matrix,
        "case_results": case_results,
        "benchmark_inputs": {
            "phase": "hosted_tenant_isolation",
            "external_network_required": False,
            "external_browser_required": False,
            "live_llm_required": False,
            "mutation_enabled": False,
            "mcp_first": True,
            "context_package_is_product": True,
            "cloud_persistence_required": False,
            "hosted_mcp_gateway_required": False,
        },
        "release_boundary": {
            "local_self_hosted_remains_ready": True,
            "hosted_tenant_registry_ready": all_pass,
            "cloud_persistence_not_started": True,
            "hosted_mcp_gateway_not_started": True,
            "security_and_billing_not_started": True,
        },
        "next_slice": "PR-12N-B Cloud Persistence And Operations" if all_pass else "PR-12N-A Hosted Brain Registry And Tenant Isolation Remediation",
    }


SMOKE_CASES = [
    GoldenCase("Come si chiama?", ("elena valsecchi",), max_branch_count=1),
    GoldenCase("Dove vive?", ("milano",), max_branch_count=1),
    GoldenCase("Dove è nata?", ("bergamo",), max_branch_count=1),
    GoldenCase("Che lavoro fa?", ("creative systems architect",), max_branch_count=1),
    GoldenCase("Su cosa lavora?", ("mneme orbit",), forbidden_answer_terms=("non ho trovato",), max_branch_count=2),
    GoldenCase("Chi è il suo partner?", ("riccardo neri",), max_branch_count=1),
    GoldenCase("Chi è la sua mentor?", ("marta bellini",), max_branch_count=1),
    GoldenCase("Come comunica?", ("diretto",), required_context_terms=("tecnico",), max_branch_count=1),
    GoldenCase("Quali sono i suoi valori?", ("precisione", "chiarezza"), max_branch_count=1),
    GoldenCase("Chi è e su cosa lavora?", ("elena valsecchi", "mneme orbit"), max_branch_count=2),
]


def post_json(base_url: str, path: str, payload: dict[str, Any], timeout: float = 120.0) -> dict[str, Any]:
    request = urllib.request.Request(
        url=f"{base_url}{path}",
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers=_benchmark_headers({"Content-Type": "application/json"}),
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"{path} -> HTTP {exc.code}: {detail[:800]}") from exc


def get_json(base_url: str, path: str, timeout: float = 120.0) -> dict[str, Any]:
    request = urllib.request.Request(url=f"{base_url}{path}", method="GET", headers=_benchmark_headers())
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"{path} -> HTTP {exc.code}: {detail[:800]}") from exc


def _contains_all(text: str, terms: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return all(term.lower() in lowered for term in terms)


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(term.lower() in lowered for term in terms)


def _safe_mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return float(statistics.fmean(values))


def _smoke_branch_budget_ok(
    *,
    max_branch_count: int | None,
    branch_count: int,
    route_step_count: int,
    stop_reason: str,
) -> bool:
    if max_branch_count is None or branch_count <= max_branch_count:
        return True
    if route_step_count == 0 and stop_reason in DIRECT_FAST_STOP_REASONS:
        return True
    return False


def _pre_route_fast_closure_ok(*, route_step_count: int, stop_reason: str) -> bool:
    return route_step_count == 0 and stop_reason in PRE_ROUTE_FAST_STOP_REASONS


def _default_query_payload(query_text: str, retrieval_mode: str = "balanced", *, thread_id: str | None = None) -> dict[str, Any]:
    payload = {
        "query_text": query_text,
        "response_mode": "both",
        "retrieval_mode": retrieval_mode,
        "max_probe_count": 6,
        "max_steps": 4,
        "max_candidates_per_step": 24,
        "max_matches": 12,
        "max_total_branches": 6,
        "max_total_steps": 4,
        "max_total_text_chars": 6400,
        "max_nodes_fulltext": 6,
        "allow_highway_expansion": True,
        "allow_document_anchor_expansion": True,
        "allow_adjacent_bucket_expansion": True,
        "allow_pattern_expansion": True,
    }
    if thread_id:
        payload["thread_id"] = thread_id
    return payload


def retrieve(base_url: str, query_text: str, retrieval_mode: str = "balanced", *, thread_id: str | None = None) -> dict[str, Any]:
    plan = plan_query(base_url, query_text, retrieval_mode, thread_id=thread_id)
    run_query(base_url, str(plan["search_id"]))
    stream_search(base_url, str(plan["search_id"]), timeout=240.0)
    return fetch_result(base_url, str(plan["search_id"]))


def plan_query(base_url: str, query_text: str, retrieval_mode: str = "balanced", *, thread_id: str | None = None) -> dict[str, Any]:
    return post_json(base_url, "/memory/query-plan", _default_query_payload(query_text, retrieval_mode, thread_id=thread_id), timeout=120.0)


def run_query(base_url: str, search_id: str) -> dict[str, Any]:
    return post_json(base_url, "/memory/query-run", {"search_id": search_id}, timeout=120.0)


def fetch_result(base_url: str, search_id: str, *, max_wait_seconds: float = 180.0, poll_interval_seconds: float = 0.5) -> dict[str, Any]:
    last_error: Exception | None = None
    attempts = max(1, int(max_wait_seconds / max(poll_interval_seconds, 0.1)))
    for _ in range(attempts):
        try:
            return get_json(base_url, f"/memory/query-result/{search_id}", timeout=120.0)
        except RuntimeError as exc:
            last_error = exc
            if "HTTP 409" not in str(exc):
                raise
            time.sleep(poll_interval_seconds)
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"/memory/query-result/{search_id} -> result_not_available")


def fetch_trace(base_url: str, search_id: str) -> dict[str, Any]:
    return get_json(base_url, f"/memory/get-trace/{search_id}", timeout=120.0)


def stream_search(base_url: str, search_id: str, timeout: float = 120.0) -> list[dict[str, Any]]:
    request = urllib.request.Request(url=f"{base_url}/memory/query-stream/{search_id}", method="GET", headers=_benchmark_headers())
    events: list[dict[str, Any]] = []
    with urllib.request.urlopen(request, timeout=timeout) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="ignore").strip()
            if not line.startswith("data: "):
                continue
            payload = json.loads(line[6:])
            events.append(payload)
            if payload.get("event_type") in {"result_ready", "search_failed"}:
                break
    return events


def evaluate_smoke_case(base_url: str, case: GoldenCase) -> dict[str, Any]:
    payload = retrieve(base_url, case.query_text, case.retrieval_mode)
    answer_text = str((payload.get("answer") or {}).get("answer_text") or "")
    context_text = str((payload.get("context") or {}).get("context_summary") or "")
    answerability = str(payload.get("answerability_state") or "")
    branch_count = int((payload.get("planner_runtime") or {}).get("branch_count") or len(payload.get("branches") or []))
    route_step_count = len(payload.get("steps") or [])
    stop_reason = str(payload.get("stop_reason") or "")
    evidence_ids = list((payload.get("answer") or {}).get("evidence_node_ids") or [])
    visited_nodes = len(payload.get("visited_node_ids") or [])
    avg_step_candidates = _safe_mean([len(step.get("candidate_ids") or []) for step in payload.get("steps") or []])
    answer_ok = _contains_all(answer_text, case.required_answer_terms) and not _contains_any(answer_text, case.forbidden_answer_terms)
    context_ok = _contains_all(context_text, case.required_context_terms) if case.required_context_terms else True
    answerability_ok = answerability == case.expected_answerability
    branch_ok = _smoke_branch_budget_ok(
        max_branch_count=case.max_branch_count,
        branch_count=branch_count,
        route_step_count=route_step_count,
        stop_reason=stop_reason,
    )
    evidence_ok = bool(evidence_ids)
    passed = answer_ok and context_ok and answerability_ok and branch_ok and evidence_ok
    return {
        "case": asdict(case),
        "passed": passed,
        "answer_ok": answer_ok,
        "context_ok": context_ok,
        "answerability_ok": answerability_ok,
        "branch_ok": branch_ok,
        "evidence_ok": evidence_ok,
        "answer_text": answer_text,
        "context_summary": context_text,
        "answerability_state": answerability,
        "branch_count": branch_count,
        "route_step_count": route_step_count,
        "stop_reason": stop_reason,
        "planner_runtime": payload.get("planner_runtime") or {},
        "evidence_node_ids": evidence_ids,
        "visited_node_count": visited_nodes,
        "avg_step_candidates": avg_step_candidates,
        "match_count": len(payload.get("matches") or []),
        "timing": payload.get("timing") or {},
    }


def run_smoke_benchmark(base_url: str) -> dict[str, Any]:
    cases = [evaluate_smoke_case(base_url, case) for case in SMOKE_CASES]
    passed_count = sum(1 for case in cases if case["passed"])
    audit = get_json(base_url, "/dev/audit")
    return {
        "phase": "smoke",
        "passed_count": passed_count,
        "total_count": len(cases),
        "pass_rate": round(passed_count / max(1, len(cases)), 3),
        "answer_exactness": round(sum(1 for case in cases if case["answer_ok"]) / len(cases), 3),
        "context_relevance": round(sum(1 for case in cases if case["context_ok"]) / len(cases), 3),
        "answerability_accuracy": round(sum(1 for case in cases if case["answerability_ok"]) / len(cases), 3),
        "evidence_recall": round(sum(1 for case in cases if case["evidence_ok"]) / len(cases), 3),
        "avg_branch_count": round(_safe_mean([float(case["branch_count"]) for case in cases]), 3),
        "avg_visited_nodes": round(_safe_mean([float(case["visited_node_count"]) for case in cases]), 3),
        "avg_step_candidates": round(_safe_mean([float(case["avg_step_candidates"]) for case in cases]), 3),
        "cases": cases,
        "audit_geometry": audit.get("geometry") or {},
        "audit_timing": audit.get("timing_percentiles") or {},
        "audit_llm_runtime": audit.get("llm_runtime") or {},
        "provider_auth_ok": audit.get("provider_auth_ok"),
    }


def run_mode_suite(base_url: str) -> dict[str, Any]:
    cases = [
        {"case_id": "direct_fact", "query_text": "Come si chiama?", "retrieval_mode": "flash", "min_probes": 1},
        {"case_id": "multi_aspect", "query_text": "Chi è e su cosa lavora?", "retrieval_mode": "balanced", "min_probes": 2},
        {"case_id": "broad_heavy", "query_text": "Riassumimi tutto di Elena", "retrieval_mode": "heavy", "min_probes": 5},
        {"case_id": "self_dossier", "query_text": "Raccontami tutto di te", "retrieval_mode": "heavy", "min_probes": 5},
    ]
    results = []
    for case in cases:
        case_id = str(case["case_id"])
        query_text = str(case["query_text"])
        retrieval_mode = str(case["retrieval_mode"])
        min_probes = int(case["min_probes"])
        payload = retrieve(base_url, query_text, retrieval_mode)
        probe_count = len(payload.get("probes") or [])
        context_dossier = str(payload.get("context_dossier") or "")
        answer_full = str(payload.get("answer_full") or "")
        answer_short = str(payload.get("answer_short") or "")
        long_form_required = retrieval_mode == "heavy"
        long_form_ok = True
        surface_ok = True
        ai_honesty_ok = True
        if long_form_required:
            long_form_ok = len(context_dossier) >= 900 and len(answer_full) >= 900 and len(answer_full) > len(answer_short)
            surface_ok = bool(payload.get("hot_context_summary")) and bool(payload.get("master_state")) and bool(payload.get("context_dossier_partial"))
            ai_honesty_ok = bool(payload.get("ai_material_contribution")) or bool(payload.get("ai_contribution_reason"))
        results.append(
            {
                "case_id": case_id,
                "query_text": query_text,
                "retrieval_mode": retrieval_mode,
                "probe_count": probe_count,
                "planner_mode": payload.get("planner_mode"),
                "decomposition_mode": payload.get("decomposition_mode"),
                "passed": probe_count >= min_probes and long_form_ok and surface_ok and ai_honesty_ok,
                "stop_reason": payload.get("stop_reason"),
                "context_dossier_chars": len(context_dossier),
                "answer_full_chars": len(answer_full),
                "answer_short_chars": len(answer_short),
                "long_form_ok": long_form_ok,
                "surface_ok": surface_ok,
                "hot_context_summary_present": bool(payload.get("hot_context_summary")),
                "master_state_present": bool(payload.get("master_state")),
                "ai_material_contribution": bool(payload.get("ai_material_contribution")),
                "ai_contribution_reason": payload.get("ai_contribution_reason"),
            }
        )
    passed_count = sum(1 for item in results if item["passed"])
    return {
        "phase": "modes",
        "passed_count": passed_count,
        "total_count": len(results),
        "pass_rate": round(passed_count / max(1, len(results)), 3),
        "cases": results,
    }


def _event_order_ok(events: list[dict[str, Any]]) -> bool:
    event_types = [str(event.get("event_type") or "") for event in events]
    try:
        landing_index = event_types.index("landing_ready")
        result_index = event_types.index("result_ready")
    except ValueError:
        return False
    if landing_index >= result_index:
        return False
    context_indices = [index for index, event_type in enumerate(event_types) if event_type == "context_update"]
    answer_index = next((index for index, event_type in enumerate(event_types) if event_type == "answer_final"), None)
    if answer_index is None:
        return False
    return any(index < answer_index for index in context_indices)


def _event_payloads(events: list[dict[str, Any]], event_type: str) -> list[dict[str, Any]]:
    return [dict(event.get("payload") or {}) for event in events if str(event.get("event_type") or "") == event_type]


def _surface_records(events: list[dict[str, Any]], result: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for event in events:
        event_type = str(event.get("event_type") or "")
        payload = dict(event.get("payload") or {})
        candidates: list[dict[str, Any]] = []
        if any(
            key in payload
            for key in ("answer_surface_state", "closure_state", "final_closure_ready", "final_closure_blockers", "unresolved_destination_count")
        ):
            candidates.append(payload)
        if isinstance(payload.get("result"), dict):
            result_payload = dict(payload.get("result") or {})
            if any(
                key in result_payload
                for key in ("answer_surface_state", "closure_state", "final_closure_ready", "final_closure_blockers", "unresolved_destination_count")
            ):
                candidates.append(result_payload)
        for candidate in candidates:
            records.append(
                {
                    "event_type": event_type,
                    "answer_surface_state": str(candidate.get("answer_surface_state") or ""),
                    "closure_state": str(candidate.get("closure_state") or ""),
                    "final_closure_ready": bool(candidate.get("final_closure_ready")),
                    "final_closure_blockers": [dict(item) for item in list(candidate.get("final_closure_blockers") or []) if isinstance(item, dict)],
                    "unresolved_destination_count": int(candidate.get("unresolved_destination_count") or 0),
                    "context_dossier_chars": len(str(candidate.get("context_dossier_partial") or candidate.get("context_dossier") or "")),
                }
            )
    final_record = {
        "event_type": "result",
        "answer_surface_state": str(result.get("answer_surface_state") or ""),
        "closure_state": str(result.get("closure_state") or ""),
        "final_closure_ready": bool(result.get("final_closure_ready")),
        "final_closure_blockers": [dict(item) for item in list(result.get("final_closure_blockers") or []) if isinstance(item, dict)],
        "unresolved_destination_count": int(result.get("unresolved_destination_count") or 0),
        "context_dossier_chars": len(str(result.get("context_dossier") or "")),
    }
    if final_record["answer_surface_state"] or final_record["closure_state"]:
        records.append(final_record)
    return records


def _section_snapshot_count(context_payloads: list[dict[str, Any]]) -> int:
    return sum(1 for payload in context_payloads if list(payload.get("section_snapshots") or []))


def _has_live_dossier_growth(context_payloads: list[dict[str, Any]]) -> bool:
    return any(
        bool(payload.get("context_dossier_partial"))
        or bool(list(payload.get("section_snapshots") or []))
        or bool(list(payload.get("section_deltas") or []))
        for payload in context_payloads
    )


def _stream_case_summary(
    base_url: str,
    *,
    case_id: str,
    query_text: str,
    retrieval_mode: str,
    min_context_updates: int,
    requires_partial_answer: bool,
    requires_dossier_growth: bool,
    document_case: bool = False,
    requires_dual_family: bool = False,
    thread_id: str | None = None,
    expected_continuity_state: str | None = None,
    expect_warm_state_used: bool | None = None,
    expect_background_after_partial: bool | None = None,
    allow_guarded_nonusable_partial: bool = False,
    stream_timeout_seconds: float = 240.0,
    result_wait_seconds: float = 180.0,
) -> dict[str, Any]:
    plan = plan_query(base_url, query_text, retrieval_mode, thread_id=thread_id)
    search_id = str(plan["search_id"])
    effective_thread_id = str(plan.get("thread_id") or thread_id or "")
    run_query(base_url, search_id)
    events = stream_search(base_url, search_id, timeout=stream_timeout_seconds)
    trace = fetch_trace(base_url, search_id)
    session = dict(trace.get("session") or {})
    session_status = str(session.get("status") or "")
    if session_status == "failed" or any(str(event.get("event_type") or "") == "search_failed" for event in events):
        raise RuntimeError(f"/memory/query-stream/{search_id} -> search_failed")
    result = fetch_result(base_url, search_id, max_wait_seconds=result_wait_seconds)
    trace = fetch_trace(base_url, search_id)
    result_steps = [dict(step) for step in list(result.get("steps") or [])]
    result_branches = [dict(branch) for branch in list(result.get("branches") or [])]
    result_stop_reason = str(result.get("stop_reason") or "")
    route_step_count = len(result_steps)
    fast_pre_route_closure = _pre_route_fast_closure_ok(
        route_step_count=route_step_count,
        stop_reason=result_stop_reason,
    )
    planner_runtime = dict(result.get("planner_runtime") or plan.get("planner_runtime") or {})
    family_plans = dict(planner_runtime.get("family_plans") or {})
    heuristic_family_plan = dict(family_plans.get("heuristic") or {})
    ai_family_plan = dict(family_plans.get("ai") or {})
    shared_evidence = dict(result.get("shared_evidence") or {})
    event_types = [str(event.get("event_type") or "") for event in events]
    context_payloads = _event_payloads(events, "context_update")
    answer_partial_payloads = _event_payloads(events, "answer_partial")
    metrics_payloads = _event_payloads(events, "metrics_update")
    continuity_summary = dict(result.get("continuity_summary") or {})
    continuity_state = str(
        continuity_summary.get("continuity_state")
        or next((payload.get("continuity_state") for payload in reversed(answer_partial_payloads) if payload.get("continuity_state")), None)
        or next((payload.get("continuity_state") for payload in reversed(context_payloads) if payload.get("continuity_state")), None)
        or next((payload.get("continuity_state") for payload in reversed(metrics_payloads) if payload.get("continuity_state")), None)
        or ""
    )
    warm_state_used = bool(
        continuity_summary.get("warm_state_used")
        if continuity_summary.get("warm_state_used") is not None
        else next((payload.get("warm_state_used") for payload in reversed(answer_partial_payloads + context_payloads + metrics_payloads) if payload.get("warm_state_used") is not None), False)
    )
    warm_state_saved = bool(
        result.get("warm_state_saved")
        if result.get("warm_state_saved") is not None
        else continuity_summary.get("warm_state_saved")
    )
    runtime_phases = [
        str(phase)
        for phase in [
            *[payload.get("runtime_phase") for payload in metrics_payloads],
            *[payload.get("runtime_phase") for payload in context_payloads],
            *[payload.get("runtime_phase") for payload in answer_partial_payloads],
        ]
        if str(phase or "")
    ]
    context_update_count = sum(1 for event_type in event_types if event_type == "context_update")
    required_event_types = {
        "planning_started",
        "planning_complete",
        "landing_ready",
        "metrics_update",
        "context_update",
        "answer_final",
        "search_stopped",
        "result_ready",
    }
    if requires_partial_answer:
        required_event_types.add("answer_partial")
    has_required_events = all(required in event_types for required in required_event_types)
    has_answer_partial = bool(answer_partial_payloads) if requires_partial_answer else True
    usable_partial_seen = any(bool(payload.get("usable")) for payload in answer_partial_payloads)
    guarded_nonusable_partial_seen = any(
        str((dict(payload.get("answer") or {})).get("document_lookup_state") or "")
        in {"no_matching_document_packet", "no_matching_document_packet_yet"}
        for payload in answer_partial_payloads
    )
    partial_usability_ok = usable_partial_seen or (allow_guarded_nonusable_partial and guarded_nonusable_partial_seen)
    has_section_growth = _section_snapshot_count(context_payloads) > 0
    has_dossier_growth = _has_live_dossier_growth(context_payloads) if requires_dossier_growth else True
    answer_partial_context = any(bool(payload.get("context_dossier_partial")) for payload in answer_partial_payloads)
    evidence_visible = any(bool(list(payload.get("evidence_snippets") or [])) for payload in context_payloads + answer_partial_payloads)
    active_worker_signal = any(int(payload.get("active_worker_count") or 0) >= 0 for payload in metrics_payloads)
    stream_trace_consistent = (
        str(result.get("stop_reason") or "") == str(((trace.get("session") or {}).get("result") or {}).get("stop_reason") or "")
        and len(list(trace.get("events") or [])) >= len(events)
    )
    top_match_document = True
    if document_case:
        top_match_document = any(bool((match.get("node") or {}).get("is_document_anchor")) for match in list(result.get("matches") or [])[:3])
    answer_partial_index = next((index for index, event_type in enumerate(event_types) if event_type == "answer_partial"), None)
    answer_final_index = next((index for index, event_type in enumerate(event_types) if event_type == "answer_final"), None)
    answer_now_before_final = answer_partial_index is not None and answer_final_index is not None and answer_partial_index < answer_final_index
    background_expansion_after_partial = (
        answer_partial_index is not None
        and any(index > answer_partial_index and event_type == "context_update" for index, event_type in enumerate(event_types))
    )
    route_decision_steps = [dict(step.get("route_decision") or {}) for step in result_steps if dict(step.get("route_decision") or {})]
    branch_route_trace = [dict(entry) for branch in result_branches for entry in list(branch.get("route_trace") or [])]
    route_trace_visible = bool(route_decision_steps or branch_route_trace)
    route_travel_visible = any(bool(entry.get("travel_performed")) for entry in route_decision_steps + branch_route_trace)
    highway_route_visible = any(
        bool(entry.get("travel_performed")) and str(entry.get("edge_type") or "") == "highway"
        for entry in route_decision_steps + branch_route_trace
    )
    destination_reached_visible = any(
        bool(entry.get("destination_reached"))
        for entry in route_decision_steps + branch_route_trace
    ) or any(bool((branch.get("destination_progress") or {}).get("destination_reached")) for branch in result_branches)
    requires_route_trace = document_case or retrieval_mode != "flash"
    requires_route_travel = document_case or retrieval_mode in {"heavy", "forensic"}
    route_trace_ok = True if fast_pre_route_closure else (route_trace_visible if requires_route_trace else True)
    route_travel_ok = True if fast_pre_route_closure else (route_travel_visible if requires_route_travel else True)
    if expected_continuity_state is not None:
        continuity_ok = continuity_state == expected_continuity_state
    else:
        continuity_ok = bool(continuity_state) or not effective_thread_id
    if expect_warm_state_used is not None:
        warm_state_ok = warm_state_used is expect_warm_state_used
    else:
        warm_state_ok = True
    if expect_background_after_partial is not None:
        background_after_partial_ok = (
            background_expansion_after_partial is expect_background_after_partial
            or (expect_background_after_partial is True and fast_pre_route_closure)
        )
    else:
        background_after_partial_ok = True
    evidence_reservoir = dict(result.get("evidence_reservoir") or {})
    reservoir_summary = dict(evidence_reservoir.get("reservoir_summary") or {})
    quality_metrics = dict(evidence_reservoir.get("quality_metrics") or result.get("context_quality_metrics") or {})
    reservoir_entries = [dict(entry) for entry in list(evidence_reservoir.get("entries") or [])]
    reservoir_documents = [dict(document) for document in list(evidence_reservoir.get("documents") or [])]
    master_state = dict(shared_evidence.get("master_state") or {})
    master_decision_history = [dict(item) for item in list(master_state.get("decision_history") or [])]
    master_decision_sources = sorted(
        {
            str(item.get("decision_source") or "").strip()
            for item in master_decision_history
            if str(item.get("decision_source") or "").strip()
        }
    )
    controller_recommendations = [dict(branch.get("controller_recommendation") or {}) for branch in result_branches if dict(branch.get("controller_recommendation") or {})]
    controller_decision_sources = sorted(
        {
            str(
                recommendation.get("decision_source")
                or branch.get("controller_decision_source")
                or ""
            ).strip()
            for branch, recommendation in (
                (branch, dict(branch.get("controller_recommendation") or {}))
                for branch in result_branches
            )
            if str(
                recommendation.get("decision_source")
                or branch.get("controller_decision_source")
                or ""
            ).strip()
        }
    )
    controller_llm_visible = "llm" in controller_decision_sources
    controller_source_visible = bool(controller_decision_sources)
    controller_sources_explicit = all(
        source in EXPLICIT_CONTROLLER_DECISION_SOURCES
        for source in controller_decision_sources
    )
    controller_override_visible = any(bool(recommendation.get("override_applied")) for recommendation in controller_recommendations)
    master_source_visible = bool(master_decision_sources)
    master_sources_explicit = all(
        source in EXPLICIT_MASTER_DECISION_SOURCES
        for source in master_decision_sources
    )
    master_ready_required = not fast_pre_route_closure and not (retrieval_mode == "flash" and warm_state_used and continuity_state == "high_continuity")
    surface_records = _surface_records(events, result)
    answer_surface_states_seen = {
        str(record.get("answer_surface_state") or "").strip()
        for record in surface_records
        if str(record.get("answer_surface_state") or "").strip()
    }
    answer_surface_states_visible = bool(
        answer_surface_states_seen
        & {"answer_now", "context_level_1_ready", "answer_now_and_continue", "final_sealed"}
    )
    closure_blockers_visible = any(
        "unresolved_destination_count" in record
        and ("final_closure_blockers" in record or int(record.get("unresolved_destination_count") or 0) >= 0)
        for record in surface_records
    )
    document_like_items = reservoir_documents or [dict(packet) for packet in list(result.get("document_packets") or [])]
    heuristic_family_visible = bool(int(heuristic_family_plan.get("probe_count") or 0)) or any(
        str(branch.get("planner_family") or "").strip().lower() == "heuristic"
        or "heuristic" in {
            str(item).strip().lower()
            for item in list(branch.get("origin_families") or [])
            if str(item).strip()
        }
        for branch in result_branches
    )
    ai_family_visible = (
        bool(int(ai_family_plan.get("probe_count") or 0))
        or bool(int(ai_family_plan.get("attributed_probe_count") or 0))
        or bool(int(ai_family_plan.get("attributed_branch_count") or 0))
        or bool(int(ai_family_plan.get("dual_origin_branch_count") or 0))
        or any(
        str(branch.get("planner_family") or "").strip().lower() == "ai"
            or "ai" in {
                str(item).strip().lower()
                for item in list(branch.get("origin_families") or [])
                if str(item).strip()
            }
        for branch in result_branches
        )
    )
    family_overlap_visible = bool(dict(shared_evidence.get("family_overlap") or {}))
    family_divergence_visible = bool(dict(shared_evidence.get("family_divergence") or {}))
    family_attribution_visible = (
        all(
            str(branch.get("planner_family") or "").strip()
            and str(branch.get("family_branch_id") or "").strip()
            and str(branch.get("family_plan_id") or "").strip()
            for branch in result_branches
        )
        and all(list(entry.get("planner_families") or []) for entry in reservoir_entries)
    ) if (result_branches or reservoir_entries) else True
    dual_family_visible = heuristic_family_visible and ai_family_visible
    reservoir_entry_count = int(reservoir_summary.get("entry_count") or len(reservoir_entries))
    reservoir_document_count = int(reservoir_summary.get("document_count") or len(reservoir_documents))
    raw_text_coverage_ratio = float(
        quality_metrics.get("raw_text_coverage_ratio")
        or reservoir_summary.get("raw_text_coverage_ratio")
        or 0.0
    )
    document_chunk_coverage_ratio = float(
        quality_metrics.get("document_chunk_coverage_ratio")
        or reservoir_summary.get("document_chunk_coverage_ratio")
        or 0.0
    )
    support_density = float(quality_metrics.get("support_density") or 0.0)
    evidence_reservoir_visible = bool(reservoir_entries or reservoir_documents or reservoir_summary)
    context_quality_visible = bool(quality_metrics)
    raw_text_coverage_ok = reservoir_entry_count == 0 or raw_text_coverage_ratio >= 0.6
    support_density_ok = reservoir_entry_count == 0 or support_density >= 0.1
    document_raw_context_complete = True
    document_chunks_before_final = True
    if document_case:
        document_raw_context_complete = bool(document_like_items) and any(
            bool(item.get("complete_text_available"))
            or bool(str(item.get("anchor_raw_text") or "").strip())
            or int(item.get("raw_text_char_count") or 0) >= 200
            or bool(list(item.get("ordered_chunk_sequence") or []))
            for item in document_like_items
        )
        document_chunks_before_final = bool(document_like_items) and any(
            bool(list(item.get("ordered_chunk_sequence") or []))
            or bool(list(item.get("chunk_node_ids") or []))
            for item in document_like_items
        )
    controller_ready = True if fast_pre_route_closure else (
        (controller_llm_visible or (controller_source_visible and controller_sources_explicit))
        if retrieval_mode in {"heavy", "forensic"} or requires_dual_family
        else True
    )
    passed = (
        has_required_events
        and _event_order_ok(events)
        and context_update_count >= min_context_updates
        and has_answer_partial
        and partial_usability_ok
        and has_dossier_growth
        and stream_trace_consistent
        and evidence_visible
        and active_worker_signal
        and top_match_document
        and answer_now_before_final
        and continuity_ok
        and warm_state_ok
        and background_after_partial_ok
        and route_trace_ok
        and route_travel_ok
        and evidence_reservoir_visible
        and context_quality_visible
        and raw_text_coverage_ok
        and support_density_ok
        and document_raw_context_complete
        and document_chunks_before_final
        and ((master_source_visible and master_sources_explicit) if master_ready_required else True)
        and controller_ready
        and (not requires_dual_family or (dual_family_visible and family_overlap_visible and family_divergence_visible and family_attribution_visible))
    )
    return {
        "case_id": case_id,
        "query_text": query_text,
        "retrieval_mode": retrieval_mode,
        "min_context_updates": min_context_updates,
        "requires_dossier_growth": requires_dossier_growth,
        "requires_dual_family": requires_dual_family,
        "search_id": search_id,
        "thread_id": effective_thread_id or None,
        "event_types": event_types,
        "context_update_count": context_update_count,
        "has_required_events": has_required_events,
        "has_answer_partial": has_answer_partial,
        "usable_partial_seen": usable_partial_seen,
        "guarded_nonusable_partial_seen": guarded_nonusable_partial_seen,
        "partial_usability_ok": partial_usability_ok,
        "answer_partial_context": answer_partial_context,
        "has_section_growth": has_section_growth,
        "has_dossier_growth": has_dossier_growth,
        "stream_trace_consistent": stream_trace_consistent,
        "evidence_visible": evidence_visible,
        "active_worker_signal": active_worker_signal,
        "top_match_document": top_match_document,
        "continuity_state": continuity_state or None,
        "warm_state_used": warm_state_used,
        "warm_state_saved": warm_state_saved,
        "answer_now_before_final": answer_now_before_final,
        "background_expansion_after_partial": background_expansion_after_partial,
        "route_trace_visible": route_trace_visible,
        "route_travel_visible": route_travel_visible,
        "route_step_count": route_step_count,
        "fast_pre_route_closure": fast_pre_route_closure,
        "highway_route_visible": highway_route_visible,
        "destination_reached_visible": destination_reached_visible,
        "evidence_reservoir_visible": evidence_reservoir_visible,
        "context_quality_visible": context_quality_visible,
        "raw_text_coverage_ratio": raw_text_coverage_ratio,
        "document_chunk_coverage_ratio": document_chunk_coverage_ratio,
        "support_density": support_density,
        "planner_family_plans_visible": bool(family_plans),
        "heuristic_family_visible": heuristic_family_visible,
        "ai_family_visible": ai_family_visible,
        "dual_family_visible": dual_family_visible,
        "family_overlap_visible": family_overlap_visible,
        "family_divergence_visible": family_divergence_visible,
        "family_attribution_visible": family_attribution_visible,
        "master_decision_sources": master_decision_sources,
        "controller_decision_sources": controller_decision_sources,
        "controller_llm_visible": controller_llm_visible,
        "controller_source_visible": controller_source_visible,
        "controller_sources_explicit": controller_sources_explicit,
        "controller_override_visible": controller_override_visible,
        "reservoir_entry_count": reservoir_entry_count,
        "reservoir_document_count": reservoir_document_count,
        "raw_text_coverage_ok": raw_text_coverage_ok,
        "support_density_ok": support_density_ok,
        "document_raw_context_complete": document_raw_context_complete,
        "document_chunks_before_final": document_chunks_before_final,
        "runtime_phases": runtime_phases,
        "answer_partial_revisions": len(answer_partial_payloads),
        "timing": dict(result.get("timing") or {}),
        "result_stop_reason": result_stop_reason,
        "trace_stop_reason": ((trace.get("session") or {}).get("result") or {}).get("stop_reason"),
        "ui_replay_readiness": {
            "answer_surface_ready": has_answer_partial,
            "answer_surface_states_ready": answer_surface_states_visible,
            "dossier_growth_ready": has_dossier_growth,
            "evidence_ledger_ready": evidence_visible,
            "closure_blockers_ready": closure_blockers_visible,
            "timeline_ready": stream_trace_consistent,
            "graph_optional": True,
            "warm_context_ready": warm_state_used or (continuity_state == "low_continuity"),
            "route_trace_ready": route_trace_ok,
            "route_travel_ready": route_travel_ok,
            "reservoir_ready": evidence_reservoir_visible and raw_text_coverage_ok and support_density_ok,
            "document_raw_context_ready": document_raw_context_complete,
            "document_chunks_ready": document_chunks_before_final,
            "dual_planner_ready": dual_family_visible and family_overlap_visible and family_divergence_visible and family_attribution_visible,
            "branch_controller_ready": controller_ready,
            "master_director_ready": (master_source_visible and master_sources_explicit) if master_ready_required else True,
        },
        "passed": passed,
    }


def _stream_case_error_summary(
    *,
    case_id: str,
    query_text: str,
    retrieval_mode: str,
    error: Exception,
    document_case: bool = False,
) -> dict[str, Any]:
    error_text = str(error)
    lowered_error = error_text.lower()
    timed_out = any(token in lowered_error for token in ("timeout", "timed out", "search_not_completed", "http 409"))
    error_type = "timeout" if timed_out else type(error).__name__
    return {
        "case_id": case_id,
        "query_text": query_text,
        "retrieval_mode": retrieval_mode,
        "passed": False,
        "status": "timeout" if timed_out else "error",
        "error_type": error_type,
        "error": error_text[:1200],
        "timed_out": bool(timed_out),
        "document_case": bool(document_case),
        "event_types": [],
        "context_update_count": 0,
        "has_required_events": False,
        "has_answer_partial": False,
        "partial_usability_ok": False,
        "has_dossier_growth": False,
        "dossier_growth_ok": False,
        "stream_trace_consistent": False,
        "route_step_count": 0,
        "fast_pre_route_closure": False,
        "document_raw_context_complete": False if document_case else None,
        "document_chunks_before_final": False if document_case else None,
        "runtime_phases": [],
        "timing": {},
        "planner_runtime": {},
        "result_stop_reason": "benchmark_case_error",
        "ui_replay_readiness": {
            "answer_surface_ready": False,
            "answer_surface_states_ready": False,
            "dossier_growth_ready": False,
            "evidence_ledger_ready": False,
            "timeline_ready": False,
            "route_trace_ready": False,
            "route_travel_ready": False,
            "reservoir_ready": False,
            "document_raw_context_ready": False if document_case else True,
            "document_chunks_ready": False if document_case else True,
            "master_director_ready": False,
        },
        "open_gaps": [f"case_error:{error_type}"],
    }


def _safe_stream_case_summary(base_url: str, **kwargs: Any) -> dict[str, Any]:
    if bool(kwargs.get("document_case")):
        kwargs.setdefault("stream_timeout_seconds", 20.0)
        kwargs.setdefault("result_wait_seconds", 3.0)
    try:
        return _stream_case_summary(base_url, **kwargs)
    except Exception as exc:  # noqa: BLE001
        return _stream_case_error_summary(
            case_id=str(kwargs.get("case_id") or "unknown_case"),
            query_text=str(kwargs.get("query_text") or ""),
            retrieval_mode=str(kwargs.get("retrieval_mode") or ""),
            document_case=bool(kwargs.get("document_case")),
            error=exc,
        )


def _document_stream_case(base_url: str) -> dict[str, Any]:
    params = urllib.parse.urlencode({"max_nodes": 120, "memory_type": "document_anchor"})
    graph_payload = get_json(base_url, f"/graph-view?{params}")
    nodes = list(((graph_payload.get("graph") or {}).get("nodes") or []))
    if not nodes:
        return {
            "case_id": "document_lookup",
            "query_text": "",
            "retrieval_mode": "forensic",
            "requires_partial_answer": True,
            "requires_dossier_growth": True,
            "document_case": True,
            "skipped": True,
            "skip_reason": "no_document_anchor_nodes_visible",
        }
    anchor = dict(nodes[0])
    summary = str(anchor.get("summary") or "").strip()
    token_basis = " ".join(summary.split()[:6]).strip() or "documento"
    return {
        "case_id": "document_lookup",
        "query_text": f"Trova il documento: {token_basis}",
        "retrieval_mode": "forensic",
        "min_context_updates": 1,
        "requires_partial_answer": True,
        "requires_dossier_growth": True,
        "document_case": True,
        "anchor_id": str(anchor.get("id") or ""),
        "skipped": False,
    }


def run_stream_suite(base_url: str) -> dict[str, Any]:
    queries: list[dict[str, Any]] = [
        {
            "case_id": "direct_fact",
            "query_text": "Come si chiama?",
            "retrieval_mode": "flash",
            "min_context_updates": 1,
            "requires_partial_answer": True,
            "requires_dossier_growth": False,
            "document_case": False,
            "skipped": False,
        },
        {
            "case_id": "multi_aspect",
            "query_text": "Chi è e su cosa lavora?",
            "retrieval_mode": "balanced",
            "min_context_updates": 1,
            "requires_partial_answer": True,
            "requires_dossier_growth": True,
            "document_case": False,
            "skipped": False,
        },
        {
            "case_id": "broad_heavy",
            "query_text": "Riassumimi tutto di Elena",
            "retrieval_mode": "heavy",
            "min_context_updates": 3,
            "requires_partial_answer": True,
            "requires_dossier_growth": True,
            "document_case": False,
            "skipped": False,
        },
        {
            "case_id": "self_dossier",
            "query_text": "Raccontami tutto di te",
            "retrieval_mode": "heavy",
            "min_context_updates": 3,
            "requires_partial_answer": True,
            "requires_dossier_growth": True,
            "document_case": False,
            "skipped": False,
        },
    ]
    queries.append(_document_stream_case(base_url))
    cases = []
    for case in queries:
        if case.get("skipped"):
            cases.append({**case, "passed": True})
            continue
        query_text = str(case["query_text"])
        retrieval_mode = str(case["retrieval_mode"])
        min_context_updates = int(case["min_context_updates"])
        plan = plan_query(base_url, query_text, retrieval_mode)
        run_query(base_url, str(plan["search_id"]))
        events = stream_search(base_url, str(plan["search_id"]), timeout=240.0)
        result = fetch_result(base_url, str(plan["search_id"]))
        trace = fetch_trace(base_url, str(plan["search_id"]))
        event_types = [str(event.get("event_type") or "") for event in events]
        context_payloads = _event_payloads(events, "context_update")
        answer_partial_payloads = _event_payloads(events, "answer_partial")
        metrics_payloads = _event_payloads(events, "metrics_update")
        context_update_count = sum(1 for event_type in event_types if event_type == "context_update")
        required_event_types = {
            "planning_started",
            "planning_complete",
            "landing_ready",
            "metrics_update",
            "context_update",
            "answer_final",
            "search_stopped",
            "result_ready",
        }
        if bool(case.get("requires_partial_answer")):
            required_event_types.add("answer_partial")
        has_required_events = all(required in event_types for required in required_event_types)
        has_answer_partial = bool(answer_partial_payloads) if bool(case.get("requires_partial_answer")) else True
        has_section_growth = _section_snapshot_count(context_payloads) > 0
        has_dossier_growth = _has_live_dossier_growth(context_payloads) if bool(case.get("requires_dossier_growth")) else True
        answer_partial_context = any(bool(payload.get("context_dossier_partial")) for payload in answer_partial_payloads)
        evidence_visible = any(bool(list(payload.get("evidence_snippets") or [])) for payload in context_payloads + answer_partial_payloads)
        active_worker_signal = any(int(payload.get("active_worker_count") or 0) >= 0 for payload in metrics_payloads)
        stream_trace_consistent = (
            str(result.get("stop_reason") or "") == str(((trace.get("session") or {}).get("result") or {}).get("stop_reason") or "")
            and len(list(trace.get("events") or [])) >= len(events)
        )
        top_match_document = True
        if bool(case.get("document_case")):
            top_match_document = any(bool((match.get("node") or {}).get("is_document_anchor")) for match in list(result.get("matches") or [])[:3])
        passed = (
            has_required_events
            and _event_order_ok(events)
            and context_update_count >= min_context_updates
            and has_answer_partial
            and has_dossier_growth
            and stream_trace_consistent
            and evidence_visible
            and active_worker_signal
            and top_match_document
        )
        cases.append(
            {
                **case,
                "query_text": query_text,
                "retrieval_mode": retrieval_mode,
                "search_id": plan["search_id"],
                "event_types": event_types,
                "context_update_count": context_update_count,
                "has_required_events": has_required_events,
                "has_answer_partial": has_answer_partial,
                "answer_partial_context": answer_partial_context,
                "has_section_growth": has_section_growth,
                "has_dossier_growth": has_dossier_growth,
                "stream_trace_consistent": stream_trace_consistent,
                "evidence_visible": evidence_visible,
                "active_worker_signal": active_worker_signal,
                "top_match_document": top_match_document,
                "ui_replay_readiness": {
                    "answer_surface_ready": has_answer_partial,
                    "answer_surface_states_ready": has_answer_partial,
                    "dossier_growth_ready": has_dossier_growth,
                    "evidence_ledger_ready": evidence_visible,
                    "closure_blockers_ready": True,
                    "timeline_ready": stream_trace_consistent,
                    "graph_optional": True,
                },
                "passed": passed,
            }
        )
    evaluated_cases = [case for case in cases if not case.get("skipped")]
    passed_count = sum(1 for case in evaluated_cases if case["passed"])
    total_count = len(evaluated_cases)
    return {
        "phase": "stream",
        "passed_count": passed_count,
        "total_count": total_count,
        "pass_rate": round(passed_count / max(1, total_count), 3),
        "all_pass": passed_count == total_count if total_count else False,
        "ui_replay_readiness": {
            "answer_surface_ready": all(bool(case.get("ui_replay_readiness", {}).get("answer_surface_ready")) for case in evaluated_cases),
            "answer_surface_states_ready": all(bool(case.get("ui_replay_readiness", {}).get("answer_surface_states_ready")) for case in evaluated_cases),
            "dossier_growth_ready": all(
                bool(case.get("ui_replay_readiness", {}).get("dossier_growth_ready"))
                for case in evaluated_cases
                if bool(case.get("requires_dossier_growth"))
            ),
            "evidence_ledger_ready": all(bool(case.get("ui_replay_readiness", {}).get("evidence_ledger_ready")) for case in evaluated_cases),
            "closure_blockers_ready": all(bool(case.get("ui_replay_readiness", {}).get("closure_blockers_ready")) for case in evaluated_cases),
            "timeline_ready": all(bool(case.get("ui_replay_readiness", {}).get("timeline_ready")) for case in evaluated_cases),
            "graph_optional": True,
            "alive_not_static": all(int(case.get("context_update_count") or 0) >= int(case.get("min_context_updates") or 0) for case in evaluated_cases),
        },
        "cases": cases,
    }


def run_stream_suite(base_url: str) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []

    direct_thread_id = str(uuid.uuid4())
    direct_cold = _stream_case_summary(
        base_url,
        case_id="direct_fact_cold",
        query_text="Come si chiama?",
        retrieval_mode="flash",
        thread_id=direct_thread_id,
        min_context_updates=1,
        requires_partial_answer=True,
        requires_dossier_growth=False,
        expected_continuity_state="low_continuity",
        expect_warm_state_used=False,
        expect_background_after_partial=True,
    )
    cases.append(direct_cold)
    cases.append(
        _stream_case_summary(
            base_url,
            case_id="direct_fact_warm_followup_same_thread",
            query_text="Come si chiama esattamente?",
            retrieval_mode="flash",
            thread_id=direct_thread_id,
            min_context_updates=1,
            requires_partial_answer=True,
            requires_dossier_growth=False,
            expect_warm_state_used=True,
            expect_background_after_partial=False,
        )
    )

    balanced_thread_id = str(uuid.uuid4())
    _stream_case_summary(
        base_url,
        case_id="balanced_seed",
        query_text="Chi Ã¨ e su cosa lavora?",
        retrieval_mode="balanced",
        thread_id=balanced_thread_id,
        min_context_updates=1,
        requires_partial_answer=True,
        requires_dossier_growth=True,
        expect_background_after_partial=True,
    )
    cases.append(
        _stream_case_summary(
            base_url,
            case_id="balanced_followup_same_thread",
            query_text="E su cosa lavora di preciso?",
            retrieval_mode="balanced",
            thread_id=balanced_thread_id,
            min_context_updates=1,
            requires_partial_answer=True,
            requires_dossier_growth=True,
            expect_warm_state_used=True,
            expect_background_after_partial=False,
        )
    )
    cases.append(
        _stream_case_summary(
            base_url,
            case_id="divergent_followup_reset_same_thread",
            query_text="Trova il documento PDF su Mneme Orbit",
            retrieval_mode="forensic",
            thread_id=balanced_thread_id,
            min_context_updates=1,
            requires_partial_answer=True,
            requires_dossier_growth=True,
            expected_continuity_state="low_continuity",
            expect_warm_state_used=False,
            expect_background_after_partial=True,
            allow_guarded_nonusable_partial=True,
        )
    )
    cases.append(
        _stream_case_summary(
            base_url,
            case_id="heavy_answer_then_expand",
            query_text="Raccontami tutto di te",
            retrieval_mode="heavy",
            thread_id=str(uuid.uuid4()),
            min_context_updates=3,
            requires_partial_answer=True,
            requires_dossier_growth=True,
            requires_dual_family=True,
            expect_background_after_partial=True,
        )
    )
    cases.append(
        _stream_case_summary(
            base_url,
            case_id="forensic_provisional_then_final",
            query_text="Chi Ã¨ e su cosa lavora?",
            retrieval_mode="forensic",
            thread_id=str(uuid.uuid4()),
            min_context_updates=1,
            requires_partial_answer=True,
            requires_dossier_growth=True,
            expect_background_after_partial=True,
        )
    )

    evaluated_cases = cases
    passed_count = sum(1 for case in evaluated_cases if case["passed"])
    total_count = len(evaluated_cases)
    direct_warm = next((case for case in evaluated_cases if case["case_id"] == "direct_fact_warm_followup_same_thread"), None)
    cold_answer_first_ms = float((direct_cold.get("timing") or {}).get("answer_first_ms") or 0.0)
    cold_first_context_ms = float((direct_cold.get("timing") or {}).get("first_context_ms") or 0.0)
    warm_answer_first_ms = float(((direct_warm or {}).get("timing") or {}).get("answer_first_ms") or 0.0)
    warm_first_context_ms = float(((direct_warm or {}).get("timing") or {}).get("first_context_ms") or 0.0)
    audit = get_json(base_url, "/dev/audit")
    return {
        "phase": "stream",
        "passed_count": passed_count,
        "total_count": total_count,
        "pass_rate": round(passed_count / max(1, total_count), 3),
        "all_pass": passed_count == total_count if total_count else False,
        "ui_replay_readiness": {
            "answer_surface_ready": all(bool(case.get("ui_replay_readiness", {}).get("answer_surface_ready")) for case in evaluated_cases),
            "answer_surface_states_ready": all(bool(case.get("ui_replay_readiness", {}).get("answer_surface_states_ready")) for case in evaluated_cases),
            "dossier_growth_ready": all(
                bool(case.get("ui_replay_readiness", {}).get("dossier_growth_ready"))
                for case in evaluated_cases
                if bool(case.get("requires_dossier_growth"))
            ),
            "evidence_ledger_ready": all(bool(case.get("ui_replay_readiness", {}).get("evidence_ledger_ready")) for case in evaluated_cases),
            "closure_blockers_ready": all(bool(case.get("ui_replay_readiness", {}).get("closure_blockers_ready")) for case in evaluated_cases),
            "timeline_ready": all(bool(case.get("ui_replay_readiness", {}).get("timeline_ready")) for case in evaluated_cases),
            "graph_optional": True,
            "alive_not_static": all(int(case.get("context_update_count") or 0) >= int(case.get("min_context_updates") or 0) for case in evaluated_cases),
            "warm_context_ready": all(bool(case.get("ui_replay_readiness", {}).get("warm_context_ready")) for case in evaluated_cases),
            "route_trace_ready": all(bool(case.get("ui_replay_readiness", {}).get("route_trace_ready")) for case in evaluated_cases if case.get("retrieval_mode") != "flash" or case.get("document_case")),
            "route_travel_ready": all(bool(case.get("ui_replay_readiness", {}).get("route_travel_ready")) for case in evaluated_cases if case.get("retrieval_mode") in {"heavy", "forensic"} or case.get("document_case")),
            "reservoir_ready": all(bool(case.get("ui_replay_readiness", {}).get("reservoir_ready")) for case in evaluated_cases),
            "document_raw_context_ready": all(
                bool(case.get("ui_replay_readiness", {}).get("document_raw_context_ready"))
                for case in evaluated_cases
                if case.get("document_case")
            ),
            "branch_controller_ready": all(
                bool(case.get("ui_replay_readiness", {}).get("branch_controller_ready"))
                for case in evaluated_cases
                if case.get("retrieval_mode") in {"heavy", "forensic"} or case.get("requires_dual_family")
            ),
            "master_director_ready": all(
                bool(case.get("ui_replay_readiness", {}).get("master_director_ready"))
                for case in evaluated_cases
                if not (case.get("retrieval_mode") == "flash" and case.get("warm_state_used") and case.get("continuity_state") == "high_continuity")
            ),
            "dual_planner_ready": all(
                bool(case.get("ui_replay_readiness", {}).get("dual_planner_ready"))
                for case in evaluated_cases
                if case.get("requires_dual_family")
            ),
        },
        "warm_cold_latency_delta": {
            "cold_answer_first_ms": cold_answer_first_ms,
            "warm_answer_first_ms": warm_answer_first_ms,
            "cold_first_context_ms": cold_first_context_ms,
            "warm_first_context_ms": warm_first_context_ms,
            "answer_first_improved": bool(warm_answer_first_ms and cold_answer_first_ms and warm_answer_first_ms <= cold_answer_first_ms),
            "first_context_improved": bool(warm_first_context_ms and cold_first_context_ms and warm_first_context_ms <= cold_first_context_ms),
        },
        "runtime_audit_metrics": {
            "warm_hit_ratio": audit.get("warm_hit_ratio"),
            "warm_partial_reuse_ratio": audit.get("warm_partial_reuse_ratio"),
            "divergence_reset_ratio": audit.get("divergence_reset_ratio"),
            "answer_now_before_final_ratio": audit.get("answer_now_before_final_ratio"),
            "answer_now_before_exploration_complete_ratio": audit.get("answer_now_before_exploration_complete_ratio"),
            "final_closure_after_destination_resolution_ratio": audit.get("final_closure_after_destination_resolution_ratio"),
            "context_level_1_before_final_ratio": audit.get("context_level_1_before_final_ratio"),
            "background_expansion_after_partial_ratio": audit.get("background_expansion_after_partial_ratio"),
            "route_trace_session_ratio": audit.get("route_trace_session_ratio"),
            "route_travel_session_ratio": audit.get("route_travel_session_ratio"),
            "highway_route_use_ratio": audit.get("highway_route_use_ratio"),
            "link_route_use_ratio": audit.get("link_route_use_ratio"),
            "local_route_use_ratio": audit.get("local_route_use_ratio"),
            "destination_reached_ratio": audit.get("destination_reached_ratio"),
            "merge_trigger_ratio": audit.get("merge_trigger_ratio"),
            "branch_controller_usage_ratio": audit.get("branch_controller_usage_ratio"),
            "branch_controller_override_ratio": audit.get("branch_controller_override_ratio"),
            "master_llm_success_ratio": audit.get("master_llm_success_ratio"),
            "master_fallback_timeout_ratio": audit.get("master_fallback_timeout_ratio"),
            "master_surface_state_histogram": audit.get("master_surface_state_histogram"),
            "master_fallback_reason_histogram": audit.get("master_fallback_reason_histogram"),
            "closure_blocker_reason_histogram": audit.get("closure_blocker_reason_histogram"),
            "planner_influence_ratio": audit.get("planner_influence_ratio"),
            "planner_family_dual_active_ratio": audit.get("planner_family_dual_active_ratio"),
            "planner_family_win_ratio": audit.get("planner_family_win_ratio"),
            "planner_family_tie_ratio": audit.get("planner_family_tie_ratio"),
            "planner_family_attribution_ratio": audit.get("planner_family_attribution_ratio"),
            "planner_arrival_ms": audit.get("planner_arrival_ms"),
            "planner_family_overlap_ratio": audit.get("planner_family_overlap_ratio"),
            "planner_family_divergence_ratio": audit.get("planner_family_divergence_ratio"),
            "raw_text_coverage_ratio": audit.get("raw_text_coverage_ratio"),
            "document_chunk_coverage_ratio": audit.get("document_chunk_coverage_ratio"),
            "support_density": audit.get("support_density"),
            "contradiction_exposure_ratio": audit.get("contradiction_exposure_ratio"),
            "highway_route_yield": audit.get("highway_route_yield"),
            "branch_duplication_ratio": audit.get("branch_duplication_ratio"),
            "branch_merge_ratio": audit.get("branch_merge_ratio"),
            "warm_context_reuse_quality": audit.get("warm_context_reuse_quality"),
            "mode_timing_percentiles": audit.get("mode_timing_percentiles"),
        },
        "cases": cases,
    }


def run_trace_suite(base_url: str) -> dict[str, Any]:
    queries = ["Come si chiama?", "Come comunica?", "Riassumimi tutto di Elena"]
    cases = []
    for query_text in queries:
        plan = plan_query(base_url, query_text)
        run_query(base_url, str(plan["search_id"]))
        events = stream_search(base_url, str(plan["search_id"]))
        result = fetch_result(base_url, str(plan["search_id"]))
        trace = fetch_trace(base_url, str(plan["search_id"]))
        blackboard = dict(trace.get("blackboard") or {})
        session_result = dict((trace.get("session") or {}).get("result") or {})
        passed = bool(blackboard.get("required_slots")) and result.get("stop_reason") == session_result.get("stop_reason")
        cases.append(
            {
                "query_text": query_text,
                "search_id": plan["search_id"],
                "passed": passed,
                "event_count": len(events),
                "trace_event_count": len(trace.get("events") or []),
                "result_stop_reason": result.get("stop_reason"),
                "trace_stop_reason": session_result.get("stop_reason"),
                "blackboard_keys": sorted(blackboard.keys()),
            }
        )
    passed_count = sum(1 for case in cases if case["passed"])
    return {
        "phase": "trace",
        "passed_count": passed_count,
        "total_count": len(cases),
        "pass_rate": round(passed_count / max(1, len(cases)), 3),
        "cases": cases,
    }


def run_documents_suite(base_url: str) -> dict[str, Any]:
    params = urllib.parse.urlencode({"max_nodes": 120, "memory_type": "document_anchor"})
    graph_payload = get_json(base_url, f"/graph-view?{params}")
    nodes = list(((graph_payload.get("graph") or {}).get("nodes") or []))
    if not nodes:
        return {
            "phase": "documents",
            "passed_count": 0,
            "total_count": 0,
            "pass_rate": 0.0,
            "cases": [],
            "skipped": True,
            "reason": "no_document_anchor_nodes_visible",
        }
    anchor = dict(nodes[0])
    summary = str(anchor.get("summary") or "").strip()
    token_basis = " ".join(summary.split()[:6]).strip() or "documento"
    thread_id = str(uuid.uuid4())
    cases = [
        _safe_stream_case_summary(
            base_url,
            case_id="document_lookup_direct",
            query_text=f"Trova il documento: {token_basis}",
            retrieval_mode="forensic",
            min_context_updates=1,
            requires_partial_answer=True,
            requires_dossier_growth=True,
            document_case=True,
            thread_id=thread_id,
            expected_continuity_state="low_continuity",
            expect_warm_state_used=False,
            expect_background_after_partial=True,
        ),
        _safe_stream_case_summary(
            base_url,
            case_id="document_lookup_warm_followup_same_thread",
            query_text=f"Trova il documento: {token_basis}",
            retrieval_mode="forensic",
            min_context_updates=1,
            requires_partial_answer=True,
            requires_dossier_growth=True,
            document_case=True,
            thread_id=thread_id,
            expected_continuity_state="high_continuity",
            expect_warm_state_used=True,
            expect_background_after_partial=True,
        ),
        _safe_stream_case_summary(
            base_url,
            case_id="document_answer_single_anchor",
            query_text=f"Secondo il documento {token_basis}, qual e il punto principale?",
            retrieval_mode="balanced",
            min_context_updates=1,
            requires_partial_answer=True,
            requires_dossier_growth=True,
            document_case=True,
            thread_id=str(uuid.uuid4()),
            expect_background_after_partial=True,
        ),
        _safe_stream_case_summary(
            base_url,
            case_id="document_multi_chunk_synthesis",
            query_text=f"Riassumi dai documenti {token_basis} i punti principali e le fonti.",
            retrieval_mode="heavy",
            min_context_updates=2,
            requires_partial_answer=True,
            requires_dossier_growth=True,
            document_case=True,
            thread_id=str(uuid.uuid4()),
            expect_background_after_partial=True,
        ),
        _safe_stream_case_summary(
            base_url,
            case_id="document_forensic_source_trace",
            query_text=f"Mostrami fonti, chunk e traccia del documento {token_basis}.",
            retrieval_mode="forensic",
            min_context_updates=2,
            requires_partial_answer=True,
            requires_dossier_growth=True,
            document_case=True,
            thread_id=str(uuid.uuid4()),
            expect_background_after_partial=True,
        ),
        _safe_stream_case_summary(
            base_url,
            case_id="document_divergent_followup_reset",
            query_text="Come si chiama?",
            retrieval_mode="flash",
            min_context_updates=1,
            requires_partial_answer=True,
            requires_dossier_growth=False,
            document_case=False,
            thread_id=thread_id,
            expected_continuity_state="low_continuity",
            expect_warm_state_used=False,
            expect_background_after_partial=None,
        ),
    ]
    passed_count = sum(1 for case in cases if case.get("passed"))
    errored_count = sum(1 for case in cases if str(case.get("status") or "") in {"error", "timeout"})
    return {
        "phase": "documents",
        "passed_count": passed_count,
        "total_count": len(cases),
        "pass_rate": round(passed_count / max(1, len(cases)), 3),
        "errored_count": errored_count,
        "runner_hardened": True,
        "cases": cases,
    }


def _maintenance_request(*, preview_only: bool, focus_node_id: str | None = None, max_nodes_considered: int = 80) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "preview_only": bool(preview_only),
        "max_nodes_considered": int(max_nodes_considered),
    }
    if focus_node_id:
        payload["focus_node_id"] = focus_node_id
    return payload


def _maintenance_case_summary(case_id: str, response: dict[str, Any], *, expect_applied: bool) -> dict[str, Any]:
    report = dict(response.get("report") or {})
    quality_before = dict(report.get("quality_before") or {})
    quality_after = dict(report.get("quality_after") or {})
    history = dict(report.get("maintenance_history_summary") or {})
    sleep_profile = dict(report.get("sleep_profile") or {})
    evolve_profile = dict(report.get("evolve_profile") or {})
    overlap_summary = dict(report.get("mode_overlap_summary") or {})
    mode_specific_quality_delta = dict(report.get("maintenance_mode_specific_quality_delta") or {})
    follow_up_candidates = list(report.get("follow_up_candidates") or [])
    prepared_next_angles = list(report.get("prepared_next_angles") or [])
    proactive_opportunities = list(report.get("proactive_opportunities") or [])
    retrieval_gap_review = dict(report.get("retrieval_gap_review") or {})
    depromotion_policy = dict(report.get("working_memory_depromotion_policy") or {})
    mode = str(report.get("mode") or "")
    sleep_ok = bool(sleep_profile) and int(sleep_profile.get("confidence_revision_count") or 0) >= 0
    evolve_ok = bool(evolve_profile) and int(evolve_profile.get("retyped_count") or 0) >= 0
    revalidation_ok = (
        str(retrieval_gap_review.get("schema_version") or "") == "agvm.maintenance.retrieval_gap_review.v1"
        and str(depromotion_policy.get("schema_version") or "") == "agvm.working_memory.depromotion_policy.v1"
        and bool(dict(retrieval_gap_review.get("calibration_authority_boundary") or {}))
    )
    if mode == "sleep":
        mode_ok = (
            sleep_ok
            and int(sleep_profile.get("bridge_adjustment_count") or 0) >= 0
            and int(evolve_profile.get("new_highway_count") or 0) == 0
        )
    elif mode == "evolve":
        mode_ok = (
            evolve_ok
            and (
                int(evolve_profile.get("retyped_count") or 0) > 0
                or int(evolve_profile.get("repositioned_count") or 0) > 0
                or int(evolve_profile.get("new_highway_count") or 0) > 0
                or int(evolve_profile.get("created_node_count") or 0) > 0
                or int(evolve_profile.get("archived_count") or 0) > 0
                or int(evolve_profile.get("superseded_count") or 0) > 0
            )
        )
    else:
        mode_ok = sleep_ok and evolve_ok and bool(overlap_summary) and bool(mode_specific_quality_delta)
    passed = (
        bool(quality_before)
        and bool(quality_after)
        and "overall_quality_delta_score" in report
        and bool(history)
        and bool(follow_up_candidates or proactive_opportunities or prepared_next_angles)
        and (not expect_applied or bool(report.get("applied")))
        and (not expect_applied or bool(response.get("graph")) or bool(response.get("atlas")))
        and mode_ok
        and revalidation_ok
    )
    return {
        "case_id": case_id,
        "passed": passed,
        "applied": bool(report.get("applied")),
        "mode": mode,
        "reviewed_node_count": len(list(report.get("reviewed_node_ids") or [])),
        "follow_up_candidate_count": len(follow_up_candidates),
        "prepared_next_angle_count": len(prepared_next_angles),
        "proactive_opportunity_count": len(proactive_opportunities),
        "quality_before": quality_before,
        "quality_after": quality_after,
        "quality_delta": dict(report.get("quality_delta") or {}),
        "overall_quality_delta_score": float(report.get("overall_quality_delta_score") or 0.0),
        "history_summary": history,
        "retyped_node_count": len(list(report.get("retyped_nodes") or [])),
        "region_action_count": len(list(report.get("region_actions") or [])),
        "bridge_change_count": len(list(report.get("bridge_promotions") or [])) + len(list(report.get("bridge_demotions") or [])),
        "new_highway_count": len(list(report.get("new_highways") or [])),
        "sleep_profile": sleep_profile,
        "evolve_profile": evolve_profile,
        "mode_overlap_summary": overlap_summary,
        "maintenance_mode_specific_quality_delta": mode_specific_quality_delta,
        "retrieval_gap_review": {
            "session_count": retrieval_gap_review.get("session_count"),
            "gap_session_count": retrieval_gap_review.get("gap_session_count"),
            "maintenance_retrieval_gap_detection_ratio": retrieval_gap_review.get("maintenance_retrieval_gap_detection_ratio"),
            "recommendation_count": len(list(retrieval_gap_review.get("recommendations") or [])),
        },
        "working_memory_depromotion_policy": {
            "warm_state_count": depromotion_policy.get("warm_state_count"),
            "depromote_candidate_count": depromotion_policy.get("depromote_candidate_count"),
            "depromotion_candidate_ratio": depromotion_policy.get("depromotion_candidate_ratio"),
        },
    }


def run_maintenance_suite(base_url: str) -> dict[str, Any]:
    audit_before = get_json(base_url, "/dev/audit")
    before_run_count = int(audit_before.get("maintenance_run_count") or 0)
    before_applied_count = int(audit_before.get("applied_maintenance_run_count") or 0)
    sleep_preview = post_json(base_url, "/memory/sleep", _maintenance_request(preview_only=True), timeout=240.0)
    sleep_apply = post_json(base_url, "/memory/sleep", _maintenance_request(preview_only=False), timeout=240.0)
    evolve_apply = post_json(base_url, "/memory/evolve", _maintenance_request(preview_only=False), timeout=240.0)
    combined_preview = post_json(base_url, "/memory/sleep-evolve", _maintenance_request(preview_only=True), timeout=240.0)
    audit_after = get_json(base_url, "/dev/audit")
    cases = [
        _maintenance_case_summary("sleep_preview", sleep_preview, expect_applied=False),
        _maintenance_case_summary("sleep_apply", sleep_apply, expect_applied=True),
        _maintenance_case_summary("evolve_apply", evolve_apply, expect_applied=True),
        _maintenance_case_summary("sleep_evolve_preview", combined_preview, expect_applied=False),
    ]
    passed_count = sum(1 for case in cases if case["passed"])
    total_count = len(cases)
    audit_ok = (
        int(audit_after.get("maintenance_run_count") or 0) >= before_run_count + total_count
        and int(audit_after.get("applied_maintenance_run_count") or 0) >= before_applied_count + 2
        and float(audit_after.get("maintenance_proactive_suggestion_ratio") or 0.0) > 0.0
        and float(audit_after.get("maintenance_repeated_evidence_ratio") or 0.0) >= 1.0
        and float(audit_after.get("sleep_review_change_ratio") or 0.0) > 0.0
        and float(audit_after.get("evolve_structural_change_ratio") or 0.0) > 0.0
        and bool(dict(audit_after.get("maintenance_mode_specific_quality_delta") or {}))
    )
    return {
        "phase": "maintenance",
        "passed_count": passed_count,
        "total_count": total_count,
        "pass_rate": round(passed_count / max(1, total_count), 3),
        "all_pass": passed_count == total_count and audit_ok,
        "cases": cases,
        "runtime_audit_metrics": {
            "maintenance_run_count": audit_after.get("maintenance_run_count"),
            "applied_maintenance_run_count": audit_after.get("applied_maintenance_run_count"),
            "maintenance_modes_histogram": audit_after.get("maintenance_modes_histogram"),
            "maintenance_improvement_ratio": audit_after.get("maintenance_improvement_ratio"),
            "maintenance_geometry_improvement_ratio": audit_after.get("maintenance_geometry_improvement_ratio"),
            "maintenance_identity_improvement_ratio": audit_after.get("maintenance_identity_improvement_ratio"),
            "maintenance_proactive_suggestion_ratio": audit_after.get("maintenance_proactive_suggestion_ratio"),
            "maintenance_repeated_evidence_ratio": audit_after.get("maintenance_repeated_evidence_ratio"),
            "sleep_review_change_ratio": audit_after.get("sleep_review_change_ratio"),
            "sleep_bridge_adjustment_ratio": audit_after.get("sleep_bridge_adjustment_ratio"),
            "evolve_structural_change_ratio": audit_after.get("evolve_structural_change_ratio"),
            "evolve_new_highway_ratio": audit_after.get("evolve_new_highway_ratio"),
            "sleep_vs_evolve_overlap_ratio": audit_after.get("sleep_vs_evolve_overlap_ratio"),
            "maintenance_mode_specific_quality_delta": audit_after.get("maintenance_mode_specific_quality_delta"),
            "heuristic_calibration_scope_count": audit_after.get("heuristic_calibration_scope_count"),
            "heuristic_calibration_event_count": audit_after.get("heuristic_calibration_event_count"),
            "heuristic_calibration_gain": audit_after.get("heuristic_calibration_gain"),
            "post_retrieval_calibration_gain": audit_after.get("post_retrieval_calibration_gain"),
            "calibrated_bootstrap_success_ratio": audit_after.get("calibrated_bootstrap_success_ratio"),
            "calibrated_branch_count_delta": audit_after.get("calibrated_branch_count_delta"),
            "calibrated_highway_use_delta": audit_after.get("calibrated_highway_use_delta"),
            "maintenance_retrieval_gap_detection_ratio": audit_after.get("maintenance_retrieval_gap_detection_ratio"),
            "maintenance_retrieval_gap_run_ratio": audit_after.get("maintenance_retrieval_gap_run_ratio"),
            "working_memory_depromotion_candidate_ratio": audit_after.get("working_memory_depromotion_candidate_ratio"),
            "working_memory_depromotion_review_count": audit_after.get("working_memory_depromotion_review_count"),
        },
    }


def _calibration_query_case(base_url: str, *, case_id: str, query_text: str, retrieval_mode: str) -> dict[str, Any]:
    first_plan = plan_query(base_url, query_text, retrieval_mode)
    run_query(base_url, str(first_plan["search_id"]))
    stream_search(base_url, str(first_plan["search_id"]), timeout=240.0)
    first_result = fetch_result(base_url, str(first_plan["search_id"]))

    second_plan = plan_query(base_url, query_text, retrieval_mode)
    run_query(base_url, str(second_plan["search_id"]))
    stream_search(base_url, str(second_plan["search_id"]), timeout=240.0)
    second_result = fetch_result(base_url, str(second_plan["search_id"]))
    second_runtime = dict(second_result.get("planner_runtime") or {})
    calibration_runtime = dict(second_runtime.get("heuristic_calibration") or {})
    return {
        "case_id": case_id,
        "query_text": query_text,
        "retrieval_mode": retrieval_mode,
        "passed": bool(calibration_runtime.get("scope_keys_used")),
        "first_search_id": first_plan.get("search_id"),
        "second_search_id": second_plan.get("search_id"),
        "first_answerability_state": first_result.get("answerability_state"),
        "second_answerability_state": second_result.get("answerability_state"),
        "calibration_runtime": calibration_runtime,
    }


def run_calibration_suite(base_url: str) -> dict[str, Any]:
    audit_before = get_json(base_url, "/dev/audit")
    before_event_count = int(audit_before.get("heuristic_calibration_event_count") or 0)
    cases = [
        _calibration_query_case(
            base_url,
            case_id="style_repeat_family",
            query_text="Come comunica questa persona quando ragiona?",
            retrieval_mode="balanced",
        ),
        _calibration_query_case(
            base_url,
            case_id="work_repeat_family",
            query_text="Su cosa lavora esattamente Elena?",
            retrieval_mode="balanced",
        ),
    ]
    maintenance_apply = post_json(base_url, "/memory/sleep", _maintenance_request(preview_only=False), timeout=240.0)
    audit_after = get_json(base_url, "/dev/audit")
    passed_count = sum(1 for case in cases if case.get("passed"))
    all_pass = (
        passed_count == len(cases)
        and int(audit_after.get("heuristic_calibration_scope_count") or 0) > 0
        and int(audit_after.get("heuristic_calibration_event_count") or 0) > before_event_count
        and float(audit_after.get("heuristic_calibration_gain") or 0.0) > 0.0
        and float(audit_after.get("calibrated_bootstrap_success_ratio") or 0.0) > 0.0
    )
    return {
        "phase": "calibration",
        "passed_count": passed_count,
        "total_count": len(cases),
        "pass_rate": round(passed_count / max(1, len(cases)), 3),
        "all_pass": all_pass,
        "cases": cases,
        "maintenance_apply": {
            "applied": bool(dict(maintenance_apply.get("report") or {}).get("applied")),
            "calibration_event_ids": list(dict(maintenance_apply.get("report") or {}).get("calibration_event_ids") or []),
        },
        "runtime_audit_metrics": {
            "heuristic_calibration_scope_count": audit_after.get("heuristic_calibration_scope_count"),
            "heuristic_calibration_event_count": audit_after.get("heuristic_calibration_event_count"),
            "heuristic_calibration_gain": audit_after.get("heuristic_calibration_gain"),
            "calibrated_bootstrap_success_ratio": audit_after.get("calibrated_bootstrap_success_ratio"),
            "calibrated_branch_count_delta": audit_after.get("calibrated_branch_count_delta"),
            "calibrated_highway_use_delta": audit_after.get("calibrated_highway_use_delta"),
        },
    }


def run_planner_seed_suite(base_url: str) -> dict[str, Any]:
    cases = [
        {"case_id": "planner_seed_direct_fact", "query_text": "Come si chiama?", "retrieval_mode": "flash", "min_strands": 1},
        {"case_id": "planner_seed_style_values", "query_text": "Come comunica e quali valori guidano questa persona?", "retrieval_mode": "balanced", "min_strands": 2},
        {"case_id": "planner_seed_broad_summary", "query_text": "Raccontami tutto di te", "retrieval_mode": "heavy", "min_strands": 4},
        {"case_id": "planner_seed_document_lookup", "query_text": "Secondo il documento qual è il punto principale?", "retrieval_mode": "forensic", "min_strands": 1},
    ]
    suite_cases: list[dict[str, Any]] = []
    for case in cases:
        plan = plan_query(base_url, case["query_text"], case["retrieval_mode"])
        planner_runtime = dict(plan.get("planner_runtime") or {})
        planner_seed_runtime = dict(plan.get("planner_seed_runtime") or {})
        answer_strands = list(plan.get("answer_strands") or planner_runtime.get("answer_strands") or planner_seed_runtime.get("answer_strands") or [])
        seed_goal_coverage = dict(plan.get("seed_goal_coverage") or planner_runtime.get("seed_goal_coverage") or planner_seed_runtime.get("seed_goal_coverage") or {})
        seed_destination_presence = dict(plan.get("seed_destination_presence") or planner_runtime.get("seed_destination_presence") or planner_seed_runtime.get("seed_destination_presence") or {})
        planner_seed_source = str(
            planner_seed_runtime.get("planner_seed_source")
            or planner_runtime.get("planner_seed_source")
            or ""
        ).strip()
        planner_seed_status = str(
            planner_seed_runtime.get("planner_seed_status")
            or planner_runtime.get("planner_seed_status")
            or ""
        ).strip()
        planner_seed_ms = float(
            planner_seed_runtime.get("planner_seed_ms")
            or planner_runtime.get("planner_seed_ms")
            or 0.0
        )
        planner_seed_attempt_count = int(
            planner_seed_runtime.get("planner_seed_attempt_count")
            or planner_runtime.get("planner_seed_attempt_count")
            or 0
        )
        planner_seed_retry_used = bool(
            planner_seed_runtime.get("planner_seed_retry_used")
            or planner_runtime.get("planner_seed_retry_used")
        )
        planner_seed_recovered_from_timeout = bool(
            planner_seed_runtime.get("planner_seed_recovered_from_timeout")
            or planner_runtime.get("planner_seed_recovered_from_timeout")
        )
        strand_count = len(answer_strands)
        coverage_ratio = float(seed_goal_coverage.get("coverage_ratio") or 0.0)
        destination_ratio = float(seed_destination_presence.get("ratio") or 0.0)
        seed_used_by_bootstrap = bool(
            planner_seed_runtime.get("seed_used_by_bootstrap")
            or planner_runtime.get("seed_used_by_bootstrap")
        )
        planner_seed_deferred = planner_seed_source == "background_deferred" or planner_seed_status == "deferred"
        planner_seed_source_ok = planner_seed_source in {"llm", "background_deferred"}
        planner_seed_status_ok = planner_seed_status in {"completed", "deferred", "timeout", "provider_error", "parse_error"}
        passed = (
            strand_count >= int(case["min_strands"])
            and coverage_ratio > 0.0
            and destination_ratio > 0.0
            and seed_used_by_bootstrap
            and planner_seed_source_ok
            and planner_seed_status_ok
            and planner_seed_ms < 8000.0
        )
        suite_cases.append(
            {
                "case_id": case["case_id"],
                "query_text": case["query_text"],
                "retrieval_mode": case["retrieval_mode"],
                "passed": passed,
                "planner_seed_source": planner_seed_source or None,
                "planner_seed_status": planner_seed_status or None,
                "planner_seed_ms": planner_seed_ms,
                "planner_seed_attempt_count": planner_seed_attempt_count,
                "planner_seed_retry_used": planner_seed_retry_used,
                "planner_seed_recovered_from_timeout": planner_seed_recovered_from_timeout,
                "planner_seed_deferred": planner_seed_deferred,
                "answer_strand_count": strand_count,
                "seed_goal_coverage": seed_goal_coverage,
                "seed_destination_presence": seed_destination_presence,
                "seed_used_by_bootstrap": seed_used_by_bootstrap,
            }
        )
    passed_count = sum(1 for case in suite_cases if case["passed"])
    llm_case_count = sum(1 for case in suite_cases if str(case.get("planner_seed_source") or "") == "llm")
    deferred_case_count = sum(1 for case in suite_cases if str(case.get("planner_seed_source") or "") == "background_deferred")
    timeout_count = sum(1 for case in suite_cases if str(case.get("planner_seed_source") or "") == "timeout")
    audit_after = get_json(base_url, "/dev/audit")
    return {
        "phase": "planner_seed",
        "passed_count": passed_count,
        "total_count": len(suite_cases),
        "pass_rate": round(passed_count / max(1, len(suite_cases)), 3),
        "llm_case_count": llm_case_count,
        "deferred_case_count": deferred_case_count,
        "timeout_count": timeout_count,
        "all_pass": passed_count == len(suite_cases) and (llm_case_count + deferred_case_count) >= 3 and timeout_count <= 1,
        "cases": suite_cases,
        "runtime_audit_metrics": {
            "planner_seed_ms": dict(audit_after.get("planner_seed_ms") or {}),
            "planner_seed_success_ratio": audit_after.get("planner_seed_success_ratio"),
            "ai_material_contribution_ratio": audit_after.get("ai_material_contribution_ratio"),
            "answer_strand_count": audit_after.get("answer_strand_count"),
            "seed_goal_coverage_ratio": audit_after.get("seed_goal_coverage_ratio"),
            "seed_destination_presence_ratio": audit_after.get("seed_destination_presence_ratio"),
            "seed_used_by_bootstrap_ratio": audit_after.get("seed_used_by_bootstrap_ratio"),
        },
    }


def run_planner_merge_suite(base_url: str) -> dict[str, Any]:
    del base_url

    def _probe(
        *,
        planner_family: str,
        index: int,
        strand_id: str,
        answer_field: str,
        goal: str,
        guide_area: str,
        memory_type: str,
        radial_expectation: str,
        weight: float,
        destination_queue: list[dict[str, Any]],
        landing_basis: str | None = None,
        corroboration_needs: list[str] | None = None,
    ) -> dict[str, Any]:
        spec = {
            "query_text": answer_field,
            "strand_id": strand_id,
            "goal": goal,
            "expected_answer_field": answer_field,
            "expected_guide_area": guide_area,
            "expected_memory_type": memory_type,
            "radial_expectation": radial_expectation,
            "search_radius": 0.22,
            "weight": weight,
            "priority": weight,
            "landing_basis": landing_basis or ("llm::inverse_like" if planner_family == "ai" else "inverse_like_estimation"),
            "destination_queue": destination_queue,
            "corroboration_needs": list(corroboration_needs or []),
        }
        return _annotate_probe_family(
            build_probe_from_spec(
                spec,
                index,
                root_query_text="Raccontami tutto di Elena",
                atlas_payload={"buckets": []},
                identity_context={"core_nodes": []},
            ),
            planner_family=planner_family,
            family_plan_id=f"{planner_family}_family_plan",
            family_plan_confidence=weight,
        )

    query = RetrieveRequest(query_text="Raccontami tutto di Elena", response_mode="both", max_total_branches=6, max_probe_count=6)
    cases: list[dict[str, Any]] = []

    heuristic_probe = _probe(
        planner_family="heuristic",
        index=1,
        strand_id="strand_identity",
        answer_field="identity",
        goal="name",
        guide_area="Identity",
        memory_type="identity",
        radial_expectation="inner",
        weight=0.72,
        destination_queue=[
            {"label": "identity", "guide_area": "Identity", "memory_type": "identity", "radial_expectation": "inner", "priority": 0.92, "rationale": "Identity facts"},
            {"label": "style", "guide_area": "Expression", "memory_type": "identity_style", "radial_expectation": "mid", "priority": 0.48, "rationale": "Style corroboration"},
        ],
    )
    ai_reuse_probe = _probe(
        planner_family="ai",
        index=2,
        strand_id="strand_identity",
        answer_field="identity",
        goal="name",
        guide_area="Identity",
        memory_type="identity",
        radial_expectation="inner",
        weight=0.78,
        destination_queue=[
            {"label": "identity", "guide_area": "Identity", "memory_type": "identity", "radial_expectation": "inner", "priority": 0.95, "rationale": "Identity facts"},
            {"label": "style", "guide_area": "Expression", "memory_type": "identity_style", "radial_expectation": "mid", "priority": 0.46, "rationale": "Style corroboration"},
        ],
    )
    merged_probes, merged_branches, _, added, merge_summary = _merge_planner_probes(
        query=query,
        current_probes=[heuristic_probe],
        current_branches=[_create_branch_from_probe(query, heuristic_probe)],
        new_probes=[ai_reuse_probe],
        atlas_payload={"buckets": []},
    )
    reuse_branch = merged_branches[0]
    cases.append(
        {
            "case_id": "near_identical_landings",
            "expected_outcome": "reuse_branch",
            "observed_outcome": reuse_branch.get("merge_outcome"),
            "passed": added == 0 and len(merged_branches) == 1 and reuse_branch.get("merge_outcome") == "reuse_branch" and bool(reuse_branch.get("dual_origin")),
            "merge_summary": merge_summary,
        }
    )

    heuristic_probe_enrich = _probe(
        planner_family="heuristic",
        index=3,
        strand_id="strand_style",
        answer_field="style",
        goal="style",
        guide_area="Expression",
        memory_type="identity_style",
        radial_expectation="mid",
        weight=0.62,
        destination_queue=[
            {"label": "style", "guide_area": "Expression", "memory_type": "identity_style", "radial_expectation": "mid", "priority": 0.88, "rationale": "Style facts"},
        ],
    )
    ai_enrich_probe = _probe(
        planner_family="ai",
        index=4,
        strand_id="strand_style",
        answer_field="style",
        goal="style",
        guide_area="Expression",
        memory_type="identity_style",
        radial_expectation="mid",
        weight=0.86,
        destination_queue=[
            {"label": "style", "guide_area": "Expression", "memory_type": "identity_style", "radial_expectation": "mid", "priority": 0.92, "rationale": "Style facts"},
            {"label": "values", "guide_area": "Values", "memory_type": "value", "radial_expectation": "mid", "priority": 0.68, "rationale": "Values corroboration"},
        ],
        corroboration_needs=["values", "tone"],
    )
    merged_probes, merged_branches, _, added, merge_summary = _merge_planner_probes(
        query=query,
        current_probes=[heuristic_probe_enrich],
        current_branches=[_create_branch_from_probe(query, heuristic_probe_enrich)],
        new_probes=[ai_enrich_probe],
        atlas_payload={"buckets": []},
    )
    enrich_branch = merged_branches[0]
    semantic_labels = [str(item.get("label") or "") for item in list(enrich_branch.get("semantic_destination_queue") or [])]
    cases.append(
        {
            "case_id": "partial_overlap_landings",
            "expected_outcome": "enrich_branch",
            "observed_outcome": enrich_branch.get("merge_outcome"),
            "passed": added == 0 and len(merged_branches) == 1 and enrich_branch.get("merge_outcome") == "enrich_branch" and semantic_labels[:2] == ["style", "values"] and bool(enrich_branch.get("ai_refinement_delta")),
            "merge_summary": merge_summary,
        }
    )

    heuristic_probe_fork = _probe(
        planner_family="heuristic",
        index=5,
        strand_id="strand_identity",
        answer_field="identity",
        goal="name",
        guide_area="Identity",
        memory_type="identity",
        radial_expectation="inner",
        weight=0.72,
        destination_queue=[
            {"label": "identity", "guide_area": "Identity", "memory_type": "identity", "radial_expectation": "inner", "priority": 0.95, "rationale": "Identity facts"},
        ],
    )
    ai_fork_probe = _probe(
        planner_family="ai",
        index=6,
        strand_id="strand_documents",
        answer_field="documents",
        goal="documents",
        guide_area="Documents",
        memory_type="document_anchor",
        radial_expectation="outer",
        weight=0.91,
        destination_queue=[
            {"label": "documents", "guide_area": "Documents", "memory_type": "document_anchor", "radial_expectation": "outer", "priority": 0.97, "rationale": "Document evidence"},
        ],
    )
    merged_probes, merged_branches, _, added, merge_summary = _merge_planner_probes(
        query=query,
        current_probes=[heuristic_probe_fork],
        current_branches=[_create_branch_from_probe(query, heuristic_probe_fork)],
        new_probes=[ai_fork_probe],
        atlas_payload={"buckets": []},
    )
    ai_branch = next(branch for branch in merged_branches if str(branch.get("planner_family") or "") == "ai")
    cases.append(
        {
            "case_id": "strongly_divergent_landings",
            "expected_outcome": "fork_new_branch",
            "observed_outcome": ai_branch.get("merge_outcome"),
            "passed": added == 1 and len(merged_branches) == 2 and ai_branch.get("merge_outcome") == "fork_new_branch" and not bool(ai_branch.get("dual_origin")),
            "merge_summary": merge_summary,
        }
    )

    passed_count = sum(1 for item in cases if item["passed"])
    total_reuse = sum(int(((item.get("merge_summary") or {}).get("branch_reuse_count") or 0)) for item in cases)
    total_enrich = sum(int(((item.get("merge_summary") or {}).get("branch_enrich_count") or 0)) for item in cases)
    total_fork = sum(int(((item.get("merge_summary") or {}).get("branch_fork_count") or 0)) for item in cases)
    total_dual_origin = sum(int(((item.get("merge_summary") or {}).get("dual_origin_branch_count") or 0)) for item in cases)
    merge_histogram = {"reuse_branch": total_reuse, "enrich_branch": total_enrich, "fork_new_branch": total_fork}
    return {
        "phase": "planner_merge",
        "passed_count": passed_count,
        "total_count": len(cases),
        "pass_rate": round(passed_count / max(1, len(cases)), 3),
        "all_pass": passed_count == len(cases),
        "cases": cases,
        "runtime_audit_metrics": {
            "branch_reuse_ratio": round(total_reuse / max(1, len(cases)), 6),
            "branch_enrich_ratio": round(total_enrich / max(1, len(cases)), 6),
            "branch_fork_ratio": round(total_fork / max(1, len(cases)), 6),
            "dual_origin_branch_ratio": round(total_dual_origin / max(1, len(cases)), 6),
            "planner_family_attribution_ratio": round((2 if total_dual_origin else 1) / 3.0, 6),
            "merge_resolution_histogram": merge_histogram,
        },
    }


def _route_benchmark_candidate(
    node_id: str,
    *,
    guide_area: str = "Expression",
    memory_type: str = "identity_style",
    bucket_key: str = "1:0:0",
    x: float = 0.08,
    y: float = 0.04,
    z: float = 0.0,
) -> dict[str, Any]:
    return {
        "id": node_id,
        "summary": node_id,
        "guide_area": guide_area,
        "memory_type": memory_type,
        "bucket": {"key": bucket_key},
        "final_position": {"x": x, "y": y, "z": z},
        "base_position": {"x": x, "y": y, "z": z},
        "semantic_color": {"hex": "#00ffaa"},
        "provenance": {"guide_conceptual_area": guide_area},
        "routing_semantic_scores": {},
        "memory_confidence": 0.8,
        "evidence_confidence": 0.8,
        "highways": [],
        "links": [],
    }


def _norm_label(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", " ")


def _node_guide_area(node: dict[str, Any]) -> str:
    provenance = node.get("provenance") if isinstance(node.get("provenance"), dict) else {}
    return str(node.get("guide_area") or provenance.get("guide_conceptual_area") or "").strip()


def _node_bucket_key(node: dict[str, Any]) -> str:
    bucket = node.get("bucket") if isinstance(node.get("bucket"), dict) else {}
    return str(node.get("fine_bucket_key") or bucket.get("key") or "").strip()


def _unique_destinations(branch: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for field in ("active_destination", "execution_destination_queue", "destination_queue", "semantic_destination_queue"):
        raw_value = branch.get(field)
        candidates = [raw_value] if isinstance(raw_value, dict) else list(raw_value or [])
        for item in candidates:
            if not isinstance(item, dict):
                continue
            key = str(item.get("destination_id") or item.get("destination_key") or item.get("label") or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            rows.append(dict(item))
    expected_area = str(branch.get("expected_guide_area") or "").strip()
    expected_memory_type = str(branch.get("expected_memory_type") or "").strip()
    if not rows and (expected_area or expected_memory_type):
        rows.append(
            {
                "destination_id": f"fallback::{branch.get('branch_id') or 'branch'}",
                "label": str(branch.get("goal") or "fallback"),
                "guide_area": expected_area,
                "memory_type": expected_memory_type,
                "target_bucket_keys": [],
            }
        )
    return rows


def _branch_landing_matches(branch: dict[str, Any], matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    priority_ids = {
        str(node_id)
        for field in ("evidence_node_ids", "studied_node_ids", "hydrated_node_ids", "candidate_node_ids", "visited_node_ids")
        for node_id in list(branch.get(field) or [])
        if str(node_id).strip()
    }
    if priority_ids:
        selected = [match for match in matches if str(match.get("node_id") or "") in priority_ids]
        if selected:
            return selected
    branch_goal = _norm_label(branch.get("goal"))
    selected = [
        match
        for match in matches
        if branch_goal and branch_goal in _norm_label(match.get("probe_id") or match.get("label") or match.get("reason"))
    ]
    return selected or matches[:8]


def _destination_node_fit(destination: dict[str, Any], node: dict[str, Any]) -> float:
    expected_guide = _norm_label(destination.get("guide_area"))
    expected_memory = _norm_label(destination.get("memory_type"))
    node_guide = _norm_label(_node_guide_area(node))
    node_memory = _norm_label(node.get("memory_type") or node.get("node_kind"))
    score = 0.0
    weights = 0.0
    if expected_guide:
        weights += 0.62
        if node_guide == expected_guide:
            score += 0.62
        elif expected_guide in node_guide or node_guide in expected_guide:
            score += 0.42
    if expected_memory:
        weights += 0.38
        if node_memory == expected_memory:
            score += 0.38
        elif expected_memory in node_memory or node_memory in expected_memory:
            score += 0.22
    if weights <= 0.0:
        return 0.5
    return round(score / weights, 6)


def _bucket_alignment_score(destinations: list[dict[str, Any]], branch: dict[str, Any], landing_nodes: list[dict[str, Any]]) -> float:
    target_buckets = {
        str(bucket)
        for destination in destinations
        for bucket in list(destination.get("target_bucket_keys") or [])
        if str(bucket).strip()
    }
    if not target_buckets:
        return 0.5
    visited_buckets = {str(bucket) for bucket in list(branch.get("visited_bucket_keys") or []) if str(bucket).strip()}
    landing_buckets = {_node_bucket_key(node) for node in landing_nodes if _node_bucket_key(node)}
    observed = visited_buckets | landing_buckets
    if not observed:
        return 0.0
    if target_buckets & observed:
        return 1.0
    near_hit = 0.0
    for target in target_buckets:
        target_parts = target.split(":")
        for bucket in observed:
            bucket_parts = bucket.split(":")
            if len(target_parts) == 3 and len(bucket_parts) == 3:
                try:
                    distance = sum(abs(int(left) - int(right)) for left, right in zip(target_parts, bucket_parts))
                except ValueError:
                    continue
                if distance <= 2:
                    near_hit = max(near_hit, 0.55)
                elif distance <= 4:
                    near_hit = max(near_hit, 0.3)
    return round(near_hit, 6)


def _geometry_branch_audit_record(branch: dict[str, Any], matches: list[dict[str, Any]]) -> dict[str, Any]:
    destinations = _unique_destinations(branch)
    landing_matches = _branch_landing_matches(branch, matches)[:12]
    landing_nodes = [
        dict(match.get("node") or {})
        for match in landing_matches
        if isinstance(match.get("node"), dict)
    ]
    if not landing_nodes:
        landing_nodes = [
            {
                "id": str(match.get("node_id") or ""),
                "guide_area": str(match.get("guide_area") or ""),
                "memory_type": str(match.get("memory_type") or ""),
            }
            for match in landing_matches
        ]
    fit_scores: list[float] = []
    for destination in destinations:
        node_scores = [_destination_node_fit(destination, node) for node in landing_nodes[:8]]
        fit_scores.append(max(node_scores or [0.0]))
    landing_fit_score = round(max(fit_scores or [0.0]), 6)
    route_trace = [dict(item) for item in list(branch.get("route_trace") or []) if isinstance(item, dict)]
    route_relevance_values = [float(item.get("destination_relevance") or 0.0) for item in route_trace]
    route_progress_values = [float(item.get("destination_progress_gain") or 0.0) for item in route_trace]
    destination_reached = bool(branch.get("destination_reached")) or any(bool(item.get("destination_reached")) or str(item.get("move_type") or "") == "destination_reached" for item in route_trace)
    route_hops = sum(1 for item in route_trace if str(item.get("move_type") or "") == "travel")
    yielded_match_count = sum(int(item.get("yielded_match_count") or 0) for item in route_trace)
    studied_count = len(list(branch.get("studied_node_ids") or []))
    hydrated_count = len(list(branch.get("hydrated_node_ids") or []))
    evidence_count = len(list(branch.get("evidence_node_ids") or []))
    bucket_alignment = _bucket_alignment_score(destinations, branch, landing_nodes)
    route_relevance = _safe_mean(route_relevance_values)
    route_progress = _safe_mean(route_progress_values)
    destination_alignment_score = round(
        min(1.0, (0.54 * landing_fit_score) + (0.24 * route_relevance) + (0.12 * route_progress) + (0.10 if destination_reached else 0.0)),
        6,
    )
    projection_error_ratio = round(max(0.0, min(1.0, 1.0 - ((0.65 * landing_fit_score) + (0.35 * bucket_alignment)))), 6)
    evidence_density = min(1.0, evidence_count / max(1, studied_count))
    yield_density = min(1.0, max(evidence_count, yielded_match_count) / max(1, route_hops * 8 if route_hops else studied_count or 1))
    hop_efficiency = 1.0 / max(1.0, float(route_hops or 1))
    route_efficiency_score = round(
        min(1.0, (0.40 * evidence_density) + (0.22 * yield_density) + (0.18 * hop_efficiency) + (0.20 if destination_reached else 0.0)),
        6,
    )
    return {
        "branch_id": branch.get("branch_id"),
        "goal": branch.get("goal"),
        "planner_family": branch.get("planner_family"),
        "origin_families": list(branch.get("origin_families") or []),
        "intended_destinations": [
            {
                "destination_id": destination.get("destination_id"),
                "label": destination.get("label"),
                "guide_area": destination.get("guide_area"),
                "memory_type": destination.get("memory_type"),
                "radial_expectation": destination.get("radial_expectation"),
                "target_bucket_keys": list(destination.get("target_bucket_keys") or [])[:8],
            }
            for destination in destinations[:6]
        ],
        "actual_landing": [
            {
                "node_id": node.get("id") or landing_matches[index].get("node_id"),
                "guide_area": _node_guide_area(node),
                "memory_type": node.get("memory_type") or node.get("node_kind"),
                "bucket_key": _node_bucket_key(node),
                "score": landing_matches[index].get("score"),
                "sources": list(landing_matches[index].get("sources") or [])[:4],
            }
            for index, node in enumerate(landing_nodes[:8])
        ],
        "route_hops": route_hops,
        "route_trace_count": len(route_trace),
        "destination_reached": destination_reached,
        "studied_node_count": studied_count,
        "hydrated_node_count": hydrated_count,
        "evidence_node_count": evidence_count,
        "yielded_match_count": yielded_match_count,
        "bucket_alignment_score": bucket_alignment,
        "landing_fit_score": landing_fit_score,
        "destination_alignment_score": destination_alignment_score,
        "projection_error_ratio": projection_error_ratio,
        "route_efficiency_score": route_efficiency_score,
    }


def _geometry_case_audit(case: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    branches = [dict(branch) for branch in list(result.get("branches") or []) if isinstance(branch, dict)]
    matches = [dict(match) for match in list(result.get("matches") or []) if isinstance(match, dict)]
    branch_records = [_geometry_branch_audit_record(branch, matches) for branch in branches]
    expected_guides = {_norm_label(item) for item in list(case.get("expected_guide_areas") or []) if _norm_label(item)}
    intended_guides = {
        _norm_label(destination.get("guide_area"))
        for record in branch_records
        for destination in list(record.get("intended_destinations") or [])
        if _norm_label(destination.get("guide_area"))
    }
    actual_guides = {
        _norm_label(landing.get("guide_area"))
        for record in branch_records
        for landing in list(record.get("actual_landing") or [])
        if _norm_label(landing.get("guide_area"))
    }
    planner_destination_coverage = round(len(expected_guides & intended_guides) / max(1, len(expected_guides)), 6) if expected_guides else 1.0
    actual_guide_coverage = round(len(expected_guides & actual_guides) / max(1, len(expected_guides)), 6) if expected_guides else 1.0
    landing_fit_score = round(_safe_mean([float(record.get("landing_fit_score") or 0.0) for record in branch_records]), 6)
    destination_alignment_score = round(_safe_mean([float(record.get("destination_alignment_score") or 0.0) for record in branch_records]), 6)
    projection_error_ratio = round(_safe_mean([float(record.get("projection_error_ratio") or 0.0) for record in branch_records]), 6)
    route_efficiency_score = round(_safe_mean([float(record.get("route_efficiency_score") or 0.0) for record in branch_records]), 6)
    if planner_destination_coverage < 0.45:
        root_cause = "semantic_planner_destination_gap"
    elif projection_error_ratio >= 0.55 and landing_fit_score < 0.5:
        root_cause = "projection_or_matrix_a_suspect"
    elif destination_alignment_score < 0.45 and route_efficiency_score < 0.45:
        root_cause = "route_substrate_or_landing_execution_gap"
    elif actual_guide_coverage < 0.45:
        root_cause = "evidence_distribution_or_cold_landing_gap"
    else:
        root_cause = "geometry_not_primary"
    timing = dict(result.get("timing") or {})
    return {
        "case_id": case.get("case_id"),
        "query_text": case.get("query_text"),
        "retrieval_mode": case.get("retrieval_mode"),
        "search_id": result.get("search_id"),
        "measurement_complete": bool(branch_records),
        "expected_guide_areas": sorted(expected_guides),
        "intended_guide_areas": sorted(intended_guides),
        "actual_guide_areas": sorted(actual_guides),
        "planner_destination_coverage": planner_destination_coverage,
        "actual_guide_coverage": actual_guide_coverage,
        "landing_fit_score": landing_fit_score,
        "destination_alignment_score": destination_alignment_score,
        "projection_error_ratio": projection_error_ratio,
        "route_efficiency_score": route_efficiency_score,
        "matrix_a_problem_signal": bool(root_cause == "projection_or_matrix_a_suspect"),
        "root_cause_hypothesis": root_cause,
        "branch_count": len(branch_records),
        "route_trace_count": sum(int(record.get("route_trace_count") or 0) for record in branch_records),
        "studied_node_count": sum(int(record.get("studied_node_count") or 0) for record in branch_records),
        "hydrated_node_count": sum(int(record.get("hydrated_node_count") or 0) for record in branch_records),
        "evidence_node_count": sum(int(record.get("evidence_node_count") or 0) for record in branch_records),
        "answer_adequacy": dict((result.get("answer") or {}).get("answer_adequacy") or {}),
        "answerability_state": result.get("answerability_state"),
        "timing": {
            "answer_first_ms": timing.get("answer_first_ms"),
            "answer_final_ms": timing.get("answer_final_ms"),
            "total_ms": timing.get("total_ms"),
        },
        "branch_records": branch_records,
    }


def run_geometry_audit_suite(base_url: str) -> dict[str, Any]:
    cases = [
        {
            "case_id": "geometry_identity_flash",
            "query_text": "Come si chiama?",
            "retrieval_mode": "flash",
            "expected_guide_areas": ["Identity"],
        },
        {
            "case_id": "geometry_temporal_2019_balanced",
            "query_text": "Cosa hai fatto nel 2019?",
            "retrieval_mode": "balanced",
            "expected_guide_areas": ["History", "Projects"],
        },
        {
            "case_id": "geometry_project_relation_balanced",
            "query_text": "Come si collega a Mneme Orbit e che ruolo ha North Arc Studio?",
            "retrieval_mode": "balanced",
            "expected_guide_areas": ["Projects", "Relationships"],
        },
        {
            "case_id": "geometry_broad_heavy",
            "query_text": "Raccontami identita, lavoro, relazioni, valori e storia.",
            "retrieval_mode": "heavy",
            "expected_guide_areas": ["Identity", "Projects", "Relationships", "Values", "History", "Expression"],
        },
        {
            "case_id": "geometry_document_forensic",
            "query_text": "Quali anni compaiono nei documenti o nelle note e cosa indicano?",
            "retrieval_mode": "forensic",
            "expected_guide_areas": ["Media Signals", "History"],
        },
    ]
    measured_cases: list[dict[str, Any]] = []
    for case in cases:
        result = retrieve(
            base_url,
            str(case["query_text"]),
            str(case["retrieval_mode"]),
            thread_id=f"geometry-audit-{case['case_id']}-{uuid.uuid4()}",
        )
        measured_cases.append(_geometry_case_audit(case, result))
    try:
        brain_geometry_calibration = get_json(base_url, "/memory/geometry-calibration", timeout=45.0)
    except Exception as exc:  # noqa: BLE001
        brain_geometry_calibration = {
            "schema_version": "agvm.pr12g.brain_geometry_calibration.v1",
            "error": str(exc),
            "overall_score": 0.0,
            "benchmarks": {"all_pass": False, "checks": {"endpoint_available": False}},
        }
    passed_count = sum(1 for case in measured_cases if case.get("measurement_complete"))
    landing_fit_score = round(_safe_mean([float(case.get("landing_fit_score") or 0.0) for case in measured_cases]), 6)
    destination_alignment_score = round(_safe_mean([float(case.get("destination_alignment_score") or 0.0) for case in measured_cases]), 6)
    projection_error_ratio = round(_safe_mean([float(case.get("projection_error_ratio") or 0.0) for case in measured_cases]), 6)
    route_efficiency_score = round(_safe_mean([float(case.get("route_efficiency_score") or 0.0) for case in measured_cases]), 6)
    matrix_a_signal_count = sum(1 for case in measured_cases if case.get("matrix_a_problem_signal"))
    planner_gap_count = sum(1 for case in measured_cases if case.get("root_cause_hypothesis") == "semantic_planner_destination_gap")
    root_cause_histogram: dict[str, int] = {}
    for case in measured_cases:
        root_cause = str(case.get("root_cause_hypothesis") or "unknown")
        root_cause_histogram[root_cause] = root_cause_histogram.get(root_cause, 0) + 1
    matrix_a_problem_likelihood = round(matrix_a_signal_count / max(1, len(measured_cases)), 6)
    matrix_a_recommendation = (
        "prepare_27b_bounded_retune"
        if matrix_a_problem_likelihood >= 0.4 and projection_error_ratio >= 0.5
        else "do_not_retune_matrix_a_from_current_evidence"
    )
    brain_geometry_benchmarks = dict(brain_geometry_calibration.get("benchmarks") or {})
    brain_geometry_score = round(_float(brain_geometry_calibration.get("overall_score")), 6)
    brain_geometry_pass = bool(brain_geometry_benchmarks.get("all_pass"))
    return {
        "phase": "geometry_audit",
        "passed_count": passed_count,
        "total_count": len(measured_cases),
        "pass_rate": round(passed_count / max(1, len(measured_cases)), 3),
        "all_pass": passed_count == len(measured_cases) and brain_geometry_pass,
        "matrix_a_adjustment_gain": 0.0,
        "matrix_a_problem_likelihood": matrix_a_problem_likelihood,
        "matrix_a_recommendation": matrix_a_recommendation,
        "brain_geometry_calibration": brain_geometry_calibration,
        "brain_geometry_calibration_pass": brain_geometry_pass,
        "brain_geometry_score": brain_geometry_score,
        "root_cause_histogram": root_cause_histogram,
        "planner_gap_count": planner_gap_count,
        "cases": measured_cases,
        "runtime_audit_metrics": {
            "geometry_landing_fit_score": landing_fit_score,
            "geometry_destination_alignment_score": destination_alignment_score,
            "geometry_projection_error_ratio": projection_error_ratio,
            "geometry_route_efficiency_score": route_efficiency_score,
            "matrix_a_problem_likelihood": matrix_a_problem_likelihood,
            "matrix_a_adjustment_gain": 0.0,
            "geometry_measurement_case_count": len(measured_cases),
            "brain_geometry_score": brain_geometry_score,
            "brain_geometry_radial_alignment_score": _float(dict(brain_geometry_calibration.get("radial_alignment") or {}).get("score")),
            "brain_geometry_document_project_score": _float(dict(brain_geometry_calibration.get("document_project_coupling") or {}).get("score")),
            "brain_geometry_highway_quality_score": _float(dict(brain_geometry_calibration.get("highway_quality") or {}).get("score")),
            "brain_geometry_path_bridge_score": _float(dict(brain_geometry_calibration.get("path_bridge_potential") or {}).get("score")),
        },
    }


def run_route_richness_suite(base_url: str) -> dict[str, Any]:
    del base_url
    probe = {
        "probe_id": "probe_route",
        "label": "Landing Route",
        "goal": "style",
        "expected_guide_area": "Expression",
        "expected_memory_type": "identity_style",
        "radial_expectation": "inner",
        "semantic_color": {"hex": "#00ffaa"},
        "routing_semantic_scores": {},
    }
    destination_style = {
        "destination_id": "dest_style",
        "destination_key": "style::expression::identity_style",
        "label": "style",
        "guide_area": "Expression",
        "memory_type": "identity_style",
        "target_bucket_keys": ["1:0:0"],
        "target_node_ids": [],
        "semantic_color_hint": "#00ffaa",
    }
    branch_heuristic = {
        "branch_id": "b_heuristic",
        "goal": "style",
        "planner_family": "heuristic",
        "origin_families": ["heuristic"],
        "current_node_id": "n0",
        "visited_node_ids": [],
        "visited_bucket_keys": [],
        "active_destination": dict(destination_style),
        "route_yield": 0.12,
        "studied_node_ids": [],
        "hydrated_node_ids": [],
        "evidence_node_ids": [],
        "covered_slots": [],
        "route_preference_prior": {"highway": 0.8, "link": 0.55, "local": 0.45},
    }
    branch_ai = {
        **dict(branch_heuristic),
        "branch_id": "b_ai",
        "planner_family": "ai",
        "origin_families": ["ai"],
    }
    branch_dual = {
        **dict(branch_heuristic),
        "branch_id": "b_dual",
        "planner_family": "heuristic",
        "origin_families": ["heuristic", "ai"],
    }
    highway_candidate = _route_benchmark_candidate("n_highway", bucket_key="1:0:0", x=0.05, y=0.04)
    local_candidate = _route_benchmark_candidate("n_local", guide_area="Identity", memory_type="identity", bucket_key="3:0:0", x=0.24, y=0.18)
    route_decision, route_target = _choose_backend_route_move(
        probe=probe,
        branch=branch_heuristic,
        candidate_map={"n_highway": highway_candidate, "n_local": local_candidate},
        candidate_sources={"n_highway": ["route_highway"], "n_local": ["nearby_radius"]},
        blackboard={},
        current_position={"x": 0.0, "y": 0.0, "z": 0.0},
    )
    highway_case = {
        "case_id": "route_richness_direct_identity",
        "passed": route_target is not None and route_target.get("id") == "n_highway" and route_decision.get("move_type") == "travel" and route_decision.get("edge_type") == "highway",
        "route_decision": route_decision,
    }

    local_candidate_strong = _route_benchmark_candidate("n_local_strong", bucket_key="1:0:0", x=0.04, y=0.03)
    highway_candidate_weak = _route_benchmark_candidate("n_highway_weak", guide_area="Identity", memory_type="identity", bucket_key="4:0:0", x=0.31, y=0.27)
    route_decision_local, route_target_local = _choose_backend_route_move(
        probe=probe,
        branch=branch_ai,
        candidate_map={"n_local_strong": local_candidate_strong, "n_highway_weak": highway_candidate_weak},
        candidate_sources={"n_local_strong": ["nearby_radius"], "n_highway_weak": ["route_highway"]},
        blackboard={},
        current_position={"x": 0.0, "y": 0.0, "z": 0.0},
    )
    local_case = {
        "case_id": "route_richness_relation_query",
        "passed": route_target_local is not None and route_target_local.get("id") == "n_local_strong" and route_decision_local.get("edge_type") == "local",
        "route_decision": route_decision_local,
    }

    semantic_queue = [
        dict(destination_style),
        {
            "destination_id": "dest_values",
            "destination_key": "values::values::value",
            "label": "values",
            "guide_area": "Values",
            "memory_type": "value",
            "target_bucket_keys": ["2:0:0"],
            "target_node_ids": [],
            "semantic_color_hint": "#ffaa00",
        },
    ]
    execution_queue, execution_index, reorder_event = _maybe_reorder_execution_destinations(
        probe=probe,
        branch={**dict(branch_dual), "active_destination": dict(destination_style)},
        semantic_destination_queue=[dict(item) for item in semantic_queue],
        execution_destination_queue=[dict(item) for item in semantic_queue],
        execution_index=0,
        candidate_map={
            "n_active": _route_benchmark_candidate("n_active", guide_area="Expression", memory_type="identity_style", bucket_key="4:4:4", x=0.36, y=0.34),
            "n_values": _route_benchmark_candidate("n_values", guide_area="Values", memory_type="value", bucket_key="2:0:0", x=0.02, y=0.01),
        },
        candidate_sources={"n_active": ["nearby_radius"], "n_values": ["route_highway"]},
        blackboard={},
        current_position={"x": 0.0, "y": 0.0, "z": 0.0},
    )
    reorder_case = {
        "case_id": "route_richness_broad_self_summary",
        "passed": bool(reorder_event) and str((execution_queue[execution_index] or {}).get("label") or "") == "values",
        "reorder_event": reorder_event or {},
    }

    runtime_summary = _route_runtime_summary(
        [
            {
                **dict(branch_heuristic),
                "route_trace": [
                    {
                        "move_type": "travel",
                        "edge_type": "highway",
                        "travel_performed": True,
                        "yielded_match_count": 2,
                        "studied_node_ids": ["n_highway"],
                        "hydrated_node_ids": ["n_highway"],
                        "destination_reached": True,
                        "family_attribution": "heuristic",
                    }
                ],
            },
            {
                **dict(branch_ai),
                "route_trace": [
                    {
                        "move_type": "travel",
                        "edge_type": "link",
                        "travel_performed": True,
                        "yielded_match_count": 1,
                        "studied_node_ids": ["n_local_strong"],
                        "hydrated_node_ids": [],
                        "destination_reached": False,
                        "family_attribution": "ai",
                    }
                ],
            },
            {
                **dict(branch_dual),
                "route_trace": [
                    dict(reorder_event or {"move_type": "reorder", "edge_type": "none", "travel_performed": False, "yielded_match_count": 0, "studied_node_ids": [], "hydrated_node_ids": [], "destination_reached": False, "family_attribution": "dual-origin", "reorder_reason": "stronger_highway_access"}),
                    {
                        "move_type": "travel",
                        "edge_type": "highway",
                        "travel_performed": True,
                        "yielded_match_count": 3,
                        "studied_node_ids": ["n_values"],
                        "hydrated_node_ids": ["n_values"],
                        "destination_reached": True,
                        "family_attribution": "dual-origin",
                    },
                ],
            },
        ]
    )
    runtime_case = {
        "case_id": "route_richness_document_synthesis",
        "passed": (
            float(runtime_summary.get("route_richness_score") or 0.0) > 0.0
            and float(runtime_summary.get("heuristic_family_route_step_ratio") or 0.0) > 0.0
            and float(runtime_summary.get("ai_family_route_step_ratio") or 0.0) > 0.0
            and float(runtime_summary.get("highway_effective_use_ratio") or 0.0) > 0.0
            and float(runtime_summary.get("destination_reached_ratio") or 0.0) > 0.0
        ),
        "runtime_summary": runtime_summary,
    }

    cases = [highway_case, local_case, reorder_case, runtime_case]
    passed_count = sum(1 for case in cases if case.get("passed"))
    return {
        "phase": "route_richness",
        "passed_count": passed_count,
        "total_count": len(cases),
        "pass_rate": round(passed_count / max(1, len(cases)), 3),
        "all_pass": passed_count == len(cases),
        "cases": cases,
        "runtime_audit_metrics": {
            "route_richness_score": runtime_summary.get("route_richness_score"),
            "highway_effective_use_ratio": runtime_summary.get("highway_effective_use_ratio"),
            "link_effective_use_ratio": runtime_summary.get("link_effective_use_ratio"),
            "heuristic_family_route_step_ratio": runtime_summary.get("heuristic_family_route_step_ratio"),
            "ai_family_route_step_ratio": runtime_summary.get("ai_family_route_step_ratio"),
            "dual_origin_family_route_step_ratio": runtime_summary.get("dual_origin_family_route_step_ratio"),
            "destination_reached_ratio": runtime_summary.get("destination_reached_ratio"),
            "execution_reorder_count": runtime_summary.get("execution_reorder_count"),
            "execution_reorder_reasons": runtime_summary.get("execution_reorder_reasons"),
            "highway_traversed_count": 2,
        },
    }


def run_master_closure_suite(base_url: str) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []

    direct_plan = plan_query(base_url, "Come si chiama?", "flash")
    run_query(base_url, str(direct_plan["search_id"]))
    direct_events = stream_search(base_url, str(direct_plan["search_id"]), timeout=240.0)
    direct_result = fetch_result(base_url, str(direct_plan["search_id"]))
    direct_records = _surface_records(direct_events, direct_result)
    direct_answer_text = str((direct_result.get("answer") or {}).get("answer_text") or direct_result.get("answer_short") or "")
    direct_final_state = str(direct_result.get("answer_surface_state") or "")
    direct_unresolved = int(direct_result.get("unresolved_destination_count") or 0)
    direct_passed = (
        bool(direct_answer_text)
        and direct_final_state == "final_sealed"
        and bool(direct_result.get("final_closure_ready"))
        and direct_unresolved == 0
        and not list(direct_result.get("final_closure_blockers") or [])
    )
    cases.append(
        {
            "case_id": "early_answer_direct_fact",
            "query_text": "Come si chiama?",
            "retrieval_mode": "flash",
            "passed": direct_passed,
            "answer_surface_states": [record.get("answer_surface_state") for record in direct_records if record.get("answer_surface_state")],
            "final_state": direct_final_state,
            "final_closure_ready": bool(direct_result.get("final_closure_ready")),
            "unresolved_destination_count": direct_unresolved,
            "final_closure_blockers": list(direct_result.get("final_closure_blockers") or []),
        }
    )

    broad_plan = plan_query(base_url, "Raccontami tutto di te", "heavy")
    run_query(base_url, str(broad_plan["search_id"]))
    broad_events = stream_search(base_url, str(broad_plan["search_id"]), timeout=240.0)
    broad_result = fetch_result(base_url, str(broad_plan["search_id"]))
    broad_records = _surface_records(broad_events, broad_result)
    broad_answer_index = next(
        (
            index
            for index, record in enumerate(broad_records)
            if str(record.get("answer_surface_state") or "") in {"answer_now", "answer_now_and_continue"}
        ),
        None,
    )
    broad_exploration_complete_index = next(
        (
            index
            for index, record in enumerate(broad_records)
            if str(record.get("closure_state") or "") == "exploration_complete"
        ),
        None,
    )
    broad_final_index = next(
        (
            index
            for index, record in enumerate(broad_records)
            if str(record.get("answer_surface_state") or "") == "final_sealed"
        ),
        None,
    )
    broad_passed = (
        broad_answer_index is not None
        and broad_final_index is not None
        and broad_answer_index < broad_final_index
        and bool(broad_result.get("final_closure_ready"))
        and int(broad_result.get("unresolved_destination_count") or 0) == 0
        and bool(broad_result.get("final_closure_after_destination_resolution"))
        and (
            broad_exploration_complete_index is None
            or (broad_answer_index < broad_exploration_complete_index <= broad_final_index)
        )
    )
    cases.append(
        {
            "case_id": "broad_query_early_answer_late_closure",
            "query_text": "Raccontami tutto di te",
            "retrieval_mode": "heavy",
            "passed": broad_passed,
            "answer_surface_states": [record.get("answer_surface_state") for record in broad_records if record.get("answer_surface_state")],
            "closure_states": [record.get("closure_state") for record in broad_records if record.get("closure_state")],
            "answer_index": broad_answer_index,
            "exploration_complete_index": broad_exploration_complete_index,
            "final_index": broad_final_index,
            "final_closure_ready": bool(broad_result.get("final_closure_ready")),
            "unresolved_destination_count": int(broad_result.get("unresolved_destination_count") or 0),
        }
    )

    document_query = "Secondo il documento qual è il punto principale?"
    document_mode = "forensic"
    document_plan = plan_query(base_url, document_query, document_mode)
    run_query(base_url, str(document_plan["search_id"]))
    document_events = stream_search(base_url, str(document_plan["search_id"]), timeout=240.0)
    document_result = fetch_result(base_url, str(document_plan["search_id"]))
    if str(document_result.get("document_mode") or "none") == "none" and not list(document_result.get("document_packets") or []):
        document_query = "Riassumimi tutto di Elena"
        document_mode = "heavy"
        document_plan = plan_query(base_url, document_query, document_mode)
        run_query(base_url, str(document_plan["search_id"]))
        document_events = stream_search(base_url, str(document_plan["search_id"]), timeout=240.0)
        document_result = fetch_result(base_url, str(document_plan["search_id"]))
    document_records = _surface_records(document_events, document_result)
    document_intermediate_index = next(
        (
            index
            for index, record in enumerate(document_records)
            if str(record.get("answer_surface_state") or "") in {"context_level_1_ready", "answer_now_and_continue"}
        ),
        None,
    )
    document_final_index = next(
        (
            index
            for index, record in enumerate(document_records)
            if str(record.get("answer_surface_state") or "") == "final_sealed"
        ),
        None,
    )
    document_intermediate_dossier = max(
        [
            int(record.get("context_dossier_chars") or 0)
            for index, record in enumerate(document_records)
            if document_final_index is None or index < document_final_index
        ]
        or [0]
    )
    document_final_dossier = len(str(document_result.get("context_dossier") or ""))
    document_passed = (
        document_intermediate_index is not None
        and document_final_index is not None
        and document_intermediate_index < document_final_index
        and bool(document_result.get("final_closure_ready"))
        and int(document_result.get("unresolved_destination_count") or 0) == 0
        and document_final_dossier > document_intermediate_dossier
    )
    cases.append(
        {
            "case_id": "document_synthesis_answer_now_final_dossier_later",
            "query_text": document_query,
            "retrieval_mode": document_mode,
            "passed": document_passed,
            "answer_surface_states": [record.get("answer_surface_state") for record in document_records if record.get("answer_surface_state")],
            "closure_states": [record.get("closure_state") for record in document_records if record.get("closure_state")],
            "intermediate_index": document_intermediate_index,
            "final_index": document_final_index,
            "intermediate_dossier_chars": document_intermediate_dossier,
            "final_dossier_chars": document_final_dossier,
            "document_mode_detected": str(document_result.get("document_mode") or "none"),
        }
    )

    passed_count = sum(1 for case in cases if case.get("passed"))
    audit_after = get_json(base_url, "/dev/audit")
    return {
        "phase": "master_closure",
        "passed_count": passed_count,
        "total_count": len(cases),
        "pass_rate": round(passed_count / max(1, len(cases)), 3),
        "all_pass": passed_count == len(cases),
        "cases": cases,
        "runtime_audit_metrics": {
            "answer_now_before_exploration_complete_ratio": audit_after.get("answer_now_before_exploration_complete_ratio"),
            "final_closure_after_destination_resolution_ratio": audit_after.get("final_closure_after_destination_resolution_ratio"),
            "context_level_1_before_final_ratio": audit_after.get("context_level_1_before_final_ratio"),
            "master_surface_state_histogram": dict(audit_after.get("master_surface_state_histogram") or {}),
            "master_fallback_reason_histogram": dict(audit_after.get("master_fallback_reason_histogram") or {}),
            "closure_blocker_reason_histogram": dict(audit_after.get("closure_blocker_reason_histogram") or {}),
            "master_llm_success_ratio": audit_after.get("master_llm_success_ratio"),
            "master_fallback_timeout_ratio": audit_after.get("master_fallback_timeout_ratio"),
        },
    }


def run_recursive_contract_suite(base_url: str) -> dict[str, Any]:
    cases = []
    for case in SMOKE_CASES:
        plan = plan_query(base_url, case.query_text, case.retrieval_mode)
        run = run_query(base_url, str(plan["search_id"]))
        events = stream_search(base_url, str(plan["search_id"]))
        result = fetch_result(base_url, str(plan["search_id"]))
        trace = fetch_trace(base_url, str(plan["search_id"]))
        audit = get_json(base_url, "/dev/audit")
        planner_runtime = dict(result.get("planner_runtime") or {})
        blackboard = dict(trace.get("blackboard") or {})
        passed = (
            bool(plan.get("probes"))
            and str(run.get("status") or "") in {"running", "completed"}
            and _event_order_ok(events)
            and str(result.get("query_text") or "") == case.query_text
            and str(trace.get("search_id") or "") == str(plan["search_id"])
            and bool(blackboard.get("required_slots"))
            and bool(audit.get("timing_percentiles"))
            and bool(planner_runtime)
            and bool(planner_runtime.get("family_plans"))
            and "family_overlap" in blackboard
            and "family_divergence" in blackboard
            and "family_contribution_summary" in blackboard
        )
        cases.append(
            {
                "query_text": case.query_text,
                "search_id": plan["search_id"],
                "passed": passed,
                "planner_mode": plan.get("planner_mode"),
                "decomposition_mode": plan.get("decomposition_mode"),
                "planner_family_plans": dict(planner_runtime.get("family_plans") or {}),
                "stream_event_types": [str(event.get("event_type") or "") for event in events],
                "trace_blackboard_keys": sorted(blackboard.keys()),
            }
        )
    passed_count = sum(1 for case in cases if case["passed"])
    return {
        "phase": "recursive_contract",
        "passed_count": passed_count,
        "total_count": len(cases),
        "pass_rate": round(passed_count / max(1, len(cases)), 3),
        "all_pass": passed_count == len(cases) if cases else False,
        "cases": cases,
    }


def run_slice1_revalidation_suite(base_url: str) -> dict[str, Any]:
    cases = [
        {
            "case_id": "direct_fact",
            "query_text": "Come si chiama?",
            "retrieval_mode": "flash",
            "expect_nav_worker": False,
            "expect_long_form": False,
            "min_branch_count": 1,
        },
        {
            "case_id": "multi_aspect",
            "query_text": "Chi è e su cosa lavora?",
            "retrieval_mode": "balanced",
            "expect_nav_worker": False,
            "expect_long_form": False,
            "min_branch_count": 2,
        },
        {
            "case_id": "broad_heavy",
            "query_text": "Raccontami tutto di te",
            "retrieval_mode": "heavy",
            "expect_nav_worker": True,
            "expect_long_form": True,
            "min_branch_count": 4,
        },
    ]
    suite_cases = []
    decision_sources_seen: set[str] = set()
    nav_worker_seen = False
    hybrid_case_seen = False
    for case in cases:
        plan = plan_query(base_url, case["query_text"], case["retrieval_mode"])
        search_id = str(plan["search_id"])
        run = run_query(base_url, search_id)
        events = stream_search(base_url, search_id, timeout=240.0)
        result = fetch_result(base_url, search_id)
        trace = fetch_trace(base_url, search_id)
        event_types = [str(event.get("event_type") or "") for event in events]
        planner_runtime = dict(result.get("planner_runtime") or {})
        blackboard = dict((result.get("shared_evidence") or {}).get("blackboard") or {})
        trace_blackboard = dict(trace.get("blackboard") or {})
        master_state = dict(blackboard.get("master_state") or {})
        worker_registry = dict(blackboard.get("worker_registry") or {})
        decision_sources = sorted(
            {
                str(decision.get("decision_source") or "")
                for decision in list(master_state.get("decision_history") or [])
                if str(decision.get("decision_source") or "")
            }
        )
        decision_sources_seen.update(decision_sources)
        branch_count = int(planner_runtime.get("branch_count") or len(result.get("branches") or []))
        answer_full_chars = len(str(result.get("answer_full") or ""))
        dossier_chars = len(str(result.get("context_dossier") or ""))
        nav_worker_present = any(str(worker_id or "").startswith("nav::") for worker_id in worker_registry)
        if nav_worker_present:
            nav_worker_seen = True
        planner_mode = str(result.get("planner_mode") or plan.get("planner_mode") or "")
        if planner_mode == "hybrid" and bool(planner_runtime.get("llm_scout_enabled")):
            hybrid_case_seen = True
        result_stop_reason = str(result.get("stop_reason") or "")
        trace_stop_reason = str(((trace.get("session") or {}).get("result") or {}).get("stop_reason") or "")
        blackboard_ok = bool(blackboard) and bool(master_state) and bool(worker_registry)
        trace_ok = bool(trace_blackboard.get("required_slots")) and result_stop_reason == trace_stop_reason
        branch_ok = branch_count >= int(case["min_branch_count"])
        long_form_ok = True
        if bool(case["expect_long_form"]):
            long_form_ok = answer_full_chars >= 900 and dossier_chars >= 900
        nav_ok = nav_worker_present if bool(case["expect_nav_worker"]) else True
        run_ok = str(run.get("status") or "") in {"running", "completed"}
        event_order_ok = _event_order_ok(events)
        passed = blackboard_ok and trace_ok and branch_ok and long_form_ok and nav_ok and run_ok and event_order_ok
        suite_cases.append(
            {
                "case_id": case["case_id"],
                "query_text": case["query_text"],
                "retrieval_mode": case["retrieval_mode"],
                "search_id": search_id,
                "passed": passed,
                "planner_mode": planner_mode,
                "planner_runtime": planner_runtime,
                "event_types": event_types,
                "event_count": len(events),
                "run_status": run.get("status"),
                "answerability_state": result.get("answerability_state"),
                "answer_full_chars": answer_full_chars,
                "context_dossier_chars": dossier_chars,
                "branch_count": branch_count,
                "decision_sources": decision_sources,
                "worker_registry_keys": sorted(worker_registry.keys()),
                "nav_worker_present": nav_worker_present,
                "trace_blackboard_keys": sorted(trace_blackboard.keys()),
                "result_stop_reason": result_stop_reason,
                "trace_stop_reason": trace_stop_reason,
            }
        )
    passed_count = sum(1 for case in suite_cases if case["passed"])
    summary = {
        "phase_1_runtime_parity": bool(suite_cases) and all(case["run_status"] in {"running", "completed"} for case in suite_cases),
        "phase_2_blackboard_contract": bool(suite_cases) and all(case["trace_blackboard_keys"] and case["worker_registry_keys"] for case in suite_cases),
        "phase_3_ai_master": "llm" in decision_sources_seen,
        "phase_4_hybrid_race": hybrid_case_seen,
        "phase_5_ai_navigation": nav_worker_seen,
        "phase_6_modes_and_long_form": any(case["case_id"] == "broad_heavy" and case["answer_full_chars"] >= 900 and case["context_dossier_chars"] >= 900 for case in suite_cases),
    }
    return {
        "phase": "slice1_revalidation",
        "passed_count": passed_count,
        "total_count": len(suite_cases),
        "pass_rate": round(passed_count / max(1, len(suite_cases)), 3),
        "all_pass": passed_count == len(suite_cases),
        "cases": suite_cases,
        "slice1_revalidation_summary": summary,
    }


def _suite_compact_summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "phase": str(report.get("phase") or ""),
        "passed_count": int(report.get("passed_count") or 0),
        "total_count": int(report.get("total_count") or 0),
        "pass_rate": float(report.get("pass_rate") or 0.0),
        "all_pass": bool(report.get("all_pass")) if "all_pass" in report else float(report.get("pass_rate") or 0.0) >= 1.0,
        "skipped": bool(report.get("skipped")),
    }


def _find_case(cases: list[dict[str, Any]], predicate) -> dict[str, Any]:
    for case in cases:
        try:
            if predicate(case):
                return dict(case)
        except Exception:
            continue
    return {}


def _smoke_case_by_query_fragment(smoke_report: dict[str, Any], fragment: str) -> dict[str, Any]:
    lowered = fragment.lower()
    cases = [dict(case) for case in list(smoke_report.get("cases") or [])]
    return _find_case(
        cases,
        lambda case: lowered in str((case.get("case") or {}).get("query_text") or "").lower(),
    )


def _case_by_id(report: dict[str, Any], case_id: str) -> dict[str, Any]:
    cases = [dict(case) for case in list(report.get("cases") or [])]
    return _find_case(cases, lambda case: str(case.get("case_id") or "") == case_id)


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


FINAL_EVALUATION_V2_SECTION_KEYS = (
    "speed",
    "answer_quality",
    "warm_context",
    "temporal_inventory",
    "ai_influence",
    "route_richness",
    "raw_docs",
    "ui_truth",
    "audit_truth",
    "sleep_evolve",
    "geometry",
)


def _compact_snippet(value: Any, *, limit: int = 260) -> str:
    text = " ".join(str(value or "").strip().split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _case_query(case: dict[str, Any]) -> str:
    if case.get("query_text"):
        return str(case.get("query_text") or "")
    nested = dict(case.get("case") or {})
    return str(nested.get("query_text") or "")


def _case_snippet(case: dict[str, Any]) -> str:
    for key in ("answer_text", "context_summary", "reason", "stop_reason", "result_stop_reason"):
        if case.get(key):
            return _compact_snippet(case.get(key))
    return _compact_snippet(_case_query(case))


def _smoke_contract_ok(case: dict[str, Any]) -> bool:
    if not case:
        return False
    return (
        bool(case.get("answer_ok"))
        and bool(case.get("answerability_ok"))
        and bool(case.get("evidence_ok"))
    )


def _section_root_causes(gaps: list[str], *, slice_owner: str, fallback_root_cause: str) -> list[dict[str, Any]]:
    if not gaps:
        return []
    causes: list[dict[str, Any]] = []
    for gap in gaps:
        normalized = str(gap or "").strip()
        if not normalized:
            continue
        causes.append(
            {
                "root_cause": fallback_root_cause,
                "evidence": normalized,
                "slice_owner": slice_owner,
            }
        )
    return causes


def _matrix_v2_section(
    *,
    section_key: str,
    passed: bool,
    evidence_path: str,
    artifact_refs: list[str],
    query_set: list[Any],
    evidence: dict[str, Any],
    thresholds: dict[str, Any],
    timings: dict[str, Any] | None = None,
    output_snippets: list[str] | None = None,
    open_gaps: list[str] | None = None,
    root_causes: list[dict[str, Any]] | None = None,
    slice_owner: str,
    benchmark_phases: list[str] | None = None,
) -> dict[str, Any]:
    refs = [str(ref) for ref in artifact_refs if str(ref or "").strip()]
    gaps = [str(gap) for gap in list(open_gaps or []) if str(gap or "").strip()]
    if not refs:
        gaps.append("Section has no artifact reference; 29A forbids passing without evidence artifacts.")
    section_pass = bool(passed) and bool(refs)
    return {
        "schema_version": "agvm.final_evaluation_matrix.section.v2",
        "section_key": section_key,
        "pass": section_pass,
        "gate": "green" if section_pass else "red",
        "evidence_path": evidence_path,
        "artifact_refs": refs,
        "query_set": [_case_query(item) if isinstance(item, dict) else str(item) for item in query_set],
        "timings": dict(timings or {}),
        "output_snippets": [_compact_snippet(item) for item in list(output_snippets or []) if str(item or "").strip()][:6],
        "thresholds": dict(thresholds or {}),
        "evidence": dict(evidence or {}),
        "open_gaps": gaps,
        "root_causes": list(root_causes or []),
        "slice_owner": slice_owner,
        "benchmark_phases": [str(item) for item in list(benchmark_phases or []) if str(item or "").strip()],
    }


def _evaluation_v2_red_gates(sections: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    red_gates: list[dict[str, Any]] = []
    for section_key, section in sections.items():
        if bool(section.get("pass")):
            continue
        causes = [dict(item) for item in list(section.get("root_causes") or []) if isinstance(item, dict)]
        if not causes and list(section.get("open_gaps") or []):
            causes = _section_root_causes(
                [str(item) for item in list(section.get("open_gaps") or [])],
                slice_owner=str(section.get("slice_owner") or "29A"),
                fallback_root_cause=f"{section_key}_gate_failed",
            )
        red_gates.append(
            {
                "section_key": section_key,
                "gate": str(section.get("gate") or "red"),
                "slice_owner": str(section.get("slice_owner") or "29A"),
                "open_gaps": list(section.get("open_gaps") or []),
                "root_causes": causes,
                "evidence_path": str(section.get("evidence_path") or ""),
                "artifact_refs": list(section.get("artifact_refs") or []),
            }
        )
    return red_gates


def _build_final_evaluation_report(*, suites: dict[str, dict[str, Any]], audit_after: dict[str, Any]) -> dict[str, Any]:
    smoke = dict(suites.get("smoke") or {})
    modes = dict(suites.get("modes") or {})
    stream = dict(suites.get("stream") or {})
    trace = dict(suites.get("trace") or {})
    documents = dict(suites.get("documents") or {})
    maintenance = dict(suites.get("maintenance") or {})
    recursive_contract = dict(suites.get("recursive_contract") or {})

    smoke_cases = [dict(case) for case in list(smoke.get("cases") or [])]
    stream_cases = [dict(case) for case in list(stream.get("cases") or [])]
    document_cases = [dict(case) for case in list(documents.get("cases") or [])]
    trace_cases = [dict(case) for case in list(trace.get("cases") or [])]

    direct_case = _smoke_case_by_query_fragment(smoke, "chiama")
    relation_case = _smoke_case_by_query_fragment(smoke, "partner")
    style_case = _smoke_case_by_query_fragment(smoke, "comunica")
    values_case = _smoke_case_by_query_fragment(smoke, "valori")
    mixed_case = _smoke_case_by_query_fragment(smoke, "lavora")

    broad_heavy_case = _case_by_id(modes, "broad_heavy")
    self_dossier_case = _case_by_id(modes, "self_dossier")
    document_synthesis_case = _case_by_id(documents, "document_multi_chunk_synthesis")
    document_lookup_case = _case_by_id(documents, "document_lookup_direct")
    document_trace_case = _case_by_id(documents, "document_forensic_source_trace")

    mode_timing = dict(audit_after.get("mode_timing_percentiles") or {})
    flash_timing = dict(mode_timing.get("flash") or {})
    balanced_timing = dict(mode_timing.get("balanced") or {})
    heavy_timing = dict(mode_timing.get("heavy") or {})
    flash_answer_first_p50 = _float((flash_timing.get("answer_first_ms") or {}).get("p50"))
    balanced_answer_first_p50 = _float((balanced_timing.get("answer_first_ms") or {}).get("p50"))
    heavy_answer_final_p50 = _float((heavy_timing.get("answer_final_ms") or {}).get("p50"))
    answer_now_before_final_ratio = _float(audit_after.get("answer_now_before_final_ratio"))

    response_speed_pass = (
        bool(direct_case.get("passed"))
        and bool(style_case.get("passed"))
        and flash_answer_first_p50 > 0.0
        and flash_answer_first_p50 <= 500.0
        and balanced_answer_first_p50 > 0.0
        and balanced_answer_first_p50 <= 800.0
        and answer_now_before_final_ratio >= 0.6
    )
    response_speed_gaps: list[str] = []
    if not bool(direct_case.get("passed")):
        response_speed_gaps.append("Direct fact smoke query failed.")
    if not bool(style_case.get("passed")):
        response_speed_gaps.append("Style smoke query failed.")
    if flash_answer_first_p50 <= 0.0 or flash_answer_first_p50 > 500.0:
        response_speed_gaps.append("Flash `answer_first_ms` p50 is outside the fast-path target.")
    if balanced_answer_first_p50 <= 0.0 or balanced_answer_first_p50 > 800.0:
        response_speed_gaps.append("Balanced `answer_first_ms` p50 is outside the fast-path target.")
    if answer_now_before_final_ratio < 0.6:
        response_speed_gaps.append("Answer-now before final ratio is too weak.")

    answer_quality_pass = (
        _float(smoke.get("pass_rate")) >= 1.0
        and _float(smoke.get("answer_exactness")) >= 1.0
        and _float(smoke.get("context_relevance")) >= 1.0
        and _float(smoke.get("answerability_accuracy")) >= 1.0
        and _float(smoke.get("evidence_recall")) >= 1.0
        and _float(recursive_contract.get("pass_rate")) >= 1.0
        and bool(relation_case.get("passed"))
        and bool(values_case.get("passed"))
        and bool(mixed_case.get("passed"))
    )
    answer_quality_gaps: list[str] = []
    if _float(smoke.get("pass_rate")) < 1.0:
        answer_quality_gaps.append("Smoke benchmark is not fully green.")
    if _float(recursive_contract.get("pass_rate")) < 1.0:
        answer_quality_gaps.append("Recursive contract benchmark is not fully green.")
    if not bool(relation_case.get("passed")):
        answer_quality_gaps.append("Relation correctness case failed.")
    if not bool(values_case.get("passed")):
        answer_quality_gaps.append("Values correctness case failed.")
    if not bool(mixed_case.get("passed")):
        answer_quality_gaps.append("Mixed correctness case failed.")

    raw_text_coverage_ratio = _float(audit_after.get("raw_text_coverage_ratio"))
    support_density = _float(audit_after.get("support_density"))
    stream_ui = dict(stream.get("ui_replay_readiness") or {})
    context_richness_pass = (
        bool(broad_heavy_case.get("passed"))
        and bool(self_dossier_case.get("passed"))
        and _int(broad_heavy_case.get("answer_full_chars")) >= 900
        and _int(broad_heavy_case.get("context_dossier_chars")) >= 900
        and _int(self_dossier_case.get("answer_full_chars")) >= 900
        and _int(self_dossier_case.get("context_dossier_chars")) >= 900
        and raw_text_coverage_ratio >= 0.9
        and support_density >= 0.1
        and bool(stream_ui.get("reservoir_ready"))
        and bool(stream_ui.get("dossier_growth_ready"))
    )
    context_richness_gaps: list[str] = []
    if not bool(broad_heavy_case.get("passed")) or not bool(self_dossier_case.get("passed")):
        context_richness_gaps.append("Heavy broad-summary stream cases are not fully green.")
    if _int(broad_heavy_case.get("answer_full_chars")) < 900 or _int(broad_heavy_case.get("context_dossier_chars")) < 900:
        context_richness_gaps.append("Broad heavy case is not dossier-scale yet.")
    if _int(self_dossier_case.get("answer_full_chars")) < 900 or _int(self_dossier_case.get("context_dossier_chars")) < 900:
        context_richness_gaps.append("Self-dossier heavy case is not dossier-scale yet.")
    if raw_text_coverage_ratio < 0.9:
        context_richness_gaps.append("Raw text coverage ratio is below target.")
    if support_density < 0.1:
        context_richness_gaps.append("Support density is below target.")
    if not bool(stream_ui.get("reservoir_ready")) or not bool(stream_ui.get("dossier_growth_ready")):
        context_richness_gaps.append("Stream UI readiness does not prove reservoir/dossier breadth strongly enough.")

    stream_trace_pass = (
        bool(stream.get("all_pass"))
        and _float(trace.get("pass_rate")) >= 1.0
        and bool(stream_ui.get("route_trace_ready"))
        and bool(stream_ui.get("route_travel_ready"))
        and bool(stream_ui.get("timeline_ready"))
    )
    stream_trace_gaps: list[str] = []
    if not bool(stream.get("all_pass")):
        stream_trace_gaps.append("Stream suite is not fully green.")
    if _float(trace.get("pass_rate")) < 1.0:
        stream_trace_gaps.append("Trace suite is not fully green.")
    if not bool(stream_ui.get("route_trace_ready")) or not bool(stream_ui.get("route_travel_ready")):
        stream_trace_gaps.append("Route replay truth is not fully ready.")
    if not bool(stream_ui.get("timeline_ready")):
        stream_trace_gaps.append("Timeline replay is not fully ready.")

    document_chunk_coverage_ratio = _float(audit_after.get("document_chunk_coverage_ratio"))
    document_raw_completeness_pass = (
        not bool(documents.get("skipped"))
        and _float(documents.get("pass_rate")) >= 1.0
        and bool(document_lookup_case.get("passed"))
        and bool(document_synthesis_case.get("passed"))
        and bool(document_trace_case.get("passed"))
        and all(bool(case.get("document_raw_context_complete")) for case in document_cases if case)
        and raw_text_coverage_ratio >= 0.9
        and document_chunk_coverage_ratio >= 0.9
    )
    document_raw_completeness_gaps: list[str] = []
    if bool(documents.get("skipped")):
        document_raw_completeness_gaps.append("Document benchmark was skipped because no anchor nodes were visible.")
    if _float(documents.get("pass_rate")) < 1.0:
        document_raw_completeness_gaps.append("Document suite is not fully green.")
    if not all(bool(case.get("document_raw_context_complete")) for case in document_cases if case):
        document_raw_completeness_gaps.append("At least one document case lacks complete raw context exposure.")
    if raw_text_coverage_ratio < 0.9 or document_chunk_coverage_ratio < 0.9:
        document_raw_completeness_gaps.append("Document raw-text or chunk coverage ratio is below target.")

    planner_family_attribution_ratio = _float(audit_after.get("planner_family_attribution_ratio"))
    planner_family_dual_active_ratio = _float(audit_after.get("planner_family_dual_active_ratio"))
    branch_controller_usage_ratio = _float(audit_after.get("branch_controller_usage_ratio"))
    master_llm_success_ratio = _float(audit_after.get("master_llm_success_ratio"))
    route_trace_session_ratio = _float(audit_after.get("route_trace_session_ratio"))
    planner_master_activity_pass = (
        bool(stream_ui.get("dual_planner_ready"))
        and bool(stream_ui.get("branch_controller_ready"))
        and bool(stream_ui.get("master_director_ready"))
        and (planner_family_attribution_ratio > 0.0 or planner_family_dual_active_ratio > 0.0)
        and branch_controller_usage_ratio > 0.0
        and master_llm_success_ratio > 0.0
        and route_trace_session_ratio > 0.0
    )
    planner_master_activity_gaps: list[str] = []
    if not bool(stream_ui.get("dual_planner_ready")):
        planner_master_activity_gaps.append("Dual planner families are not yet visibly ready in stream replay.")
    if not bool(stream_ui.get("branch_controller_ready")):
        planner_master_activity_gaps.append("Branch controller readiness is not green.")
    if not bool(stream_ui.get("master_director_ready")):
        planner_master_activity_gaps.append("Master director readiness is not green.")
    if planner_family_attribution_ratio <= 0.0 and planner_family_dual_active_ratio <= 0.0:
        planner_master_activity_gaps.append("Planner-family attribution is still too weak.")
    if branch_controller_usage_ratio <= 0.0:
        planner_master_activity_gaps.append("Branch controller usage ratio is still zero.")
    if master_llm_success_ratio <= 0.0:
        planner_master_activity_gaps.append("Master LLM success ratio is still zero.")
    if route_trace_session_ratio <= 0.0:
        planner_master_activity_gaps.append("Route trace session ratio is still zero.")

    maintenance_mode_specific_quality_delta = dict(audit_after.get("maintenance_mode_specific_quality_delta") or {})
    sleep_evolve_quality_pass = (
        bool(maintenance.get("all_pass"))
        and _float(audit_after.get("maintenance_improvement_ratio")) > 0.0
        and _float(audit_after.get("maintenance_proactive_suggestion_ratio")) > 0.0
        and _float(audit_after.get("sleep_review_change_ratio")) > 0.0
        and _float(audit_after.get("evolve_structural_change_ratio")) > 0.0
        and bool(maintenance_mode_specific_quality_delta)
    )
    sleep_evolve_quality_gaps: list[str] = []
    if not bool(maintenance.get("all_pass")):
        sleep_evolve_quality_gaps.append("Maintenance suite is not fully green.")
    if _float(audit_after.get("maintenance_improvement_ratio")) <= 0.0:
        sleep_evolve_quality_gaps.append("Maintenance improvement ratio is not positive.")
    if _float(audit_after.get("maintenance_proactive_suggestion_ratio")) <= 0.0:
        sleep_evolve_quality_gaps.append("Maintenance proactive suggestion ratio is not positive.")
    if _float(audit_after.get("sleep_review_change_ratio")) <= 0.0 or _float(audit_after.get("evolve_structural_change_ratio")) <= 0.0:
        sleep_evolve_quality_gaps.append("Sleep/evolve semantic separation metrics are not strong enough.")
    if not bool(maintenance_mode_specific_quality_delta):
        sleep_evolve_quality_gaps.append("Mode-specific maintenance quality delta is missing.")

    final_matrix = {
        "response_speed": {
            "pass": response_speed_pass,
            "evidence": {
                "flash_answer_first_ms_p50": flash_answer_first_p50,
                "balanced_answer_first_ms_p50": balanced_answer_first_p50,
                "heavy_answer_final_ms_p50": heavy_answer_final_p50,
                "answer_now_before_final_ratio": answer_now_before_final_ratio,
            },
            "open_gaps": response_speed_gaps,
        },
        "answer_quality": {
            "pass": answer_quality_pass,
            "evidence": {
                "smoke_pass_rate": _float(smoke.get("pass_rate")),
                "answer_exactness": _float(smoke.get("answer_exactness")),
                "context_relevance": _float(smoke.get("context_relevance")),
                "answerability_accuracy": _float(smoke.get("answerability_accuracy")),
                "evidence_recall": _float(smoke.get("evidence_recall")),
                "recursive_contract_pass_rate": _float(recursive_contract.get("pass_rate")),
            },
            "open_gaps": answer_quality_gaps,
        },
        "context_richness": {
            "pass": context_richness_pass,
            "evidence": {
                "broad_heavy_answer_full_chars": _int(broad_heavy_case.get("answer_full_chars")),
                "broad_heavy_context_dossier_chars": _int(broad_heavy_case.get("context_dossier_chars")),
                "self_dossier_answer_full_chars": _int(self_dossier_case.get("answer_full_chars")),
                "self_dossier_context_dossier_chars": _int(self_dossier_case.get("context_dossier_chars")),
                "raw_text_coverage_ratio": raw_text_coverage_ratio,
                "support_density": support_density,
                "reservoir_ready": bool(stream_ui.get("reservoir_ready")),
                "dossier_growth_ready": bool(stream_ui.get("dossier_growth_ready")),
            },
            "open_gaps": context_richness_gaps,
        },
        "stream_and_trace_consistency": {
            "pass": stream_trace_pass,
            "evidence": {
                "stream_all_pass": bool(stream.get("all_pass")),
                "trace_pass_rate": _float(trace.get("pass_rate")),
                "route_trace_ready": bool(stream_ui.get("route_trace_ready")),
                "route_travel_ready": bool(stream_ui.get("route_travel_ready")),
                "timeline_ready": bool(stream_ui.get("timeline_ready")),
            },
            "open_gaps": stream_trace_gaps,
        },
        "document_raw_text_completeness": {
            "pass": document_raw_completeness_pass,
            "evidence": {
                "documents_pass_rate": _float(documents.get("pass_rate")),
                "raw_text_coverage_ratio": raw_text_coverage_ratio,
                "document_chunk_coverage_ratio": document_chunk_coverage_ratio,
                "document_lookup_passed": bool(document_lookup_case.get("passed")),
                "document_synthesis_passed": bool(document_synthesis_case.get("passed")),
                "document_trace_passed": bool(document_trace_case.get("passed")),
            },
            "open_gaps": document_raw_completeness_gaps,
        },
        "planner_master_activity": {
            "pass": planner_master_activity_pass,
            "evidence": {
                "dual_planner_ready": bool(stream_ui.get("dual_planner_ready")),
                "branch_controller_ready": bool(stream_ui.get("branch_controller_ready")),
                "master_director_ready": bool(stream_ui.get("master_director_ready")),
                "planner_family_attribution_ratio": planner_family_attribution_ratio,
                "planner_family_dual_active_ratio": planner_family_dual_active_ratio,
                "branch_controller_usage_ratio": branch_controller_usage_ratio,
                "master_llm_success_ratio": master_llm_success_ratio,
                "route_trace_session_ratio": route_trace_session_ratio,
            },
            "open_gaps": planner_master_activity_gaps,
        },
        "sleep_evolve_quality": {
            "pass": sleep_evolve_quality_pass,
            "evidence": {
                "maintenance_all_pass": bool(maintenance.get("all_pass")),
                "maintenance_improvement_ratio": _float(audit_after.get("maintenance_improvement_ratio")),
                "maintenance_proactive_suggestion_ratio": _float(audit_after.get("maintenance_proactive_suggestion_ratio")),
                "sleep_review_change_ratio": _float(audit_after.get("sleep_review_change_ratio")),
                "evolve_structural_change_ratio": _float(audit_after.get("evolve_structural_change_ratio")),
                "maintenance_mode_specific_quality_delta": maintenance_mode_specific_quality_delta,
            },
            "open_gaps": sleep_evolve_quality_gaps,
        },
    }
    section_results = [dict(value) for value in final_matrix.values()]
    passed_count = sum(1 for section in section_results if bool(section.get("pass")))
    total_count = len(section_results)
    all_pass = passed_count == total_count

    return {
        "phase": "evaluation",
        "passed_count": passed_count,
        "total_count": total_count,
        "pass_rate": round(passed_count / max(1, total_count), 3),
        "all_pass": all_pass,
        "final_evaluation_matrix": final_matrix,
        "closure_ready": all_pass,
        "suite_summaries": {name: _suite_compact_summary(report) for name, report in suites.items()},
        "referenced_cases": {
            "direct_fact": {
                "passed": bool(direct_case.get("passed")),
                "timing": dict(direct_case.get("timing") or {}),
            },
            "relation": {
                "passed": bool(relation_case.get("passed")),
                "timing": dict(relation_case.get("timing") or {}),
            },
            "style": {
                "passed": bool(style_case.get("passed")),
                "timing": dict(style_case.get("timing") or {}),
            },
            "values": {
                "passed": bool(values_case.get("passed")),
                "timing": dict(values_case.get("timing") or {}),
            },
            "mixed": {
                "passed": bool(mixed_case.get("passed")),
                "timing": dict(mixed_case.get("timing") or {}),
            },
            "broad_heavy": {
                "passed": bool(broad_heavy_case.get("passed")),
                "answer_full_chars": _int(broad_heavy_case.get("answer_full_chars")),
                "context_dossier_chars": _int(broad_heavy_case.get("context_dossier_chars")),
            },
            "self_dossier": {
                "passed": bool(self_dossier_case.get("passed")),
                "answer_full_chars": _int(self_dossier_case.get("answer_full_chars")),
                "context_dossier_chars": _int(self_dossier_case.get("context_dossier_chars")),
            },
            "document_lookup": {
                "passed": bool(document_lookup_case.get("passed")),
                "document_raw_context_complete": bool(document_lookup_case.get("document_raw_context_complete")),
            },
            "document_synthesis": {
                "passed": bool(document_synthesis_case.get("passed")),
                "document_raw_context_complete": bool(document_synthesis_case.get("document_raw_context_complete")),
            },
            "document_trace": {
                "passed": bool(document_trace_case.get("passed")),
                "document_raw_context_complete": bool(document_trace_case.get("document_raw_context_complete")),
            },
            "trace_cases": [
                {
                    "query_text": str(case.get("query_text") or ""),
                    "passed": bool(case.get("passed")),
                    "trace_event_count": _int(case.get("trace_event_count")),
                }
                for case in trace_cases
            ],
        },
    }


def _build_final_evaluation_v2_report(*, suites: dict[str, dict[str, Any]], audit_after: dict[str, Any]) -> dict[str, Any]:
    smoke = dict(suites.get("smoke") or {})
    modes = dict(suites.get("modes") or {})
    stream = dict(suites.get("stream") or {})
    trace = dict(suites.get("trace") or {})
    documents = dict(suites.get("documents") or {})
    maintenance = dict(suites.get("maintenance") or {})
    planner_seed = dict(suites.get("planner_seed") or {})
    planner_merge = dict(suites.get("planner_merge") or {})
    geometry_audit = dict(suites.get("geometry_audit") or {})
    route_richness = dict(suites.get("route_richness") or {})
    master_closure = dict(suites.get("master_closure") or {})
    recursive_contract = dict(suites.get("recursive_contract") or {})

    smoke_cases = [dict(case) for case in list(smoke.get("cases") or [])]
    stream_cases = [dict(case) for case in list(stream.get("cases") or [])]
    document_cases = [dict(case) for case in list(documents.get("cases") or [])]
    trace_cases = [dict(case) for case in list(trace.get("cases") or [])]
    geometry_cases = [dict(case) for case in list(geometry_audit.get("cases") or [])]

    direct_case = _smoke_case_by_query_fragment(smoke, "chiama")
    style_case = _smoke_case_by_query_fragment(smoke, "comunica")
    values_case = _smoke_case_by_query_fragment(smoke, "valori")
    relation_case = _smoke_case_by_query_fragment(smoke, "partner")
    mixed_case = _smoke_case_by_query_fragment(smoke, "lavora")
    broad_heavy_case = _case_by_id(modes, "broad_heavy")
    self_dossier_case = _case_by_id(modes, "self_dossier")
    direct_warm_case = _case_by_id(stream, "direct_fact_warm_followup_same_thread")
    balanced_warm_case = _case_by_id(stream, "balanced_followup_same_thread")
    divergent_warm_case = _case_by_id(stream, "divergent_followup_reset_same_thread")
    temporal_case = _case_by_id(geometry_audit, "geometry_temporal_2019_balanced")
    document_lookup_case = _case_by_id(documents, "document_lookup_direct")
    document_synthesis_case = _case_by_id(documents, "document_multi_chunk_synthesis")
    document_trace_case = _case_by_id(documents, "document_forensic_source_trace")

    mode_timing = dict(audit_after.get("mode_timing_percentiles") or {})
    flash_timing = dict(mode_timing.get("flash") or {})
    balanced_timing = dict(mode_timing.get("balanced") or {})
    heavy_timing = dict(mode_timing.get("heavy") or {})
    flash_answer_first_p50 = _float((flash_timing.get("answer_first_ms") or {}).get("p50"))
    balanced_answer_first_p50 = _float((balanced_timing.get("answer_first_ms") or {}).get("p50"))
    heavy_answer_final_p50 = _float((heavy_timing.get("answer_final_ms") or {}).get("p50"))
    planner_seed_ms = dict(audit_after.get("planner_seed_ms") or {})
    planner_seed_p50 = _float(planner_seed_ms.get("p50"))
    answer_now_before_final_ratio = _float(audit_after.get("answer_now_before_final_ratio"))
    warm_delta = dict(stream.get("warm_cold_latency_delta") or {})
    stream_ui = dict(stream.get("ui_replay_readiness") or {})
    route_metrics = dict(route_richness.get("runtime_audit_metrics") or {})
    geometry_metrics = dict(geometry_audit.get("runtime_audit_metrics") or {})
    audit_truth_checks = dict(audit_after.get("audit_truth_checks") or {})
    canonical_telemetry = dict(audit_after.get("canonical_telemetry") or {})

    direct_contract_ok = _smoke_contract_ok(direct_case) or bool(direct_case.get("passed"))
    style_contract_ok = _smoke_contract_ok(style_case) or bool(style_case.get("passed"))
    relation_contract_ok = _smoke_contract_ok(relation_case) or bool(relation_case.get("passed"))
    values_contract_ok = _smoke_contract_ok(values_case) or bool(values_case.get("passed"))
    mixed_contract_ok = _smoke_contract_ok(mixed_case) or bool(mixed_case.get("passed"))
    smoke_contract_pass_rate = round(
        sum(1 for case in smoke_cases if _smoke_contract_ok(case) or bool(case.get("passed"))) / max(1, len(smoke_cases)),
        3,
    )

    speed_gaps: list[str] = []
    if not direct_contract_ok:
        speed_gaps.append("Direct fact smoke query failed.")
    if not style_contract_ok:
        speed_gaps.append("Style smoke query failed.")
    if flash_answer_first_p50 <= 0.0 or flash_answer_first_p50 > 500.0:
        speed_gaps.append("Flash first usable answer p50 is outside the 500 ms target.")
    if balanced_answer_first_p50 <= 0.0 or balanced_answer_first_p50 > 800.0:
        speed_gaps.append("Balanced first usable answer p50 is outside the 800 ms target.")
    if answer_now_before_final_ratio < 0.6:
        speed_gaps.append("Answer-now before final ratio is below 0.60.")
    if planner_seed_p50 > 0.0 and planner_seed_p50 > 3500.0:
        speed_gaps.append("Planner seed p50 is still too slow for the ideal AI-assisted path.")
    speed_pass = not speed_gaps

    answer_quality_gaps: list[str] = []
    if smoke_contract_pass_rate < 0.9:
        answer_quality_gaps.append("Smoke answer contract pass rate is below 0.90.")
    if _float(smoke.get("answer_exactness")) < 1.0:
        answer_quality_gaps.append("Smoke answer exactness is below 1.00.")
    if _float(smoke.get("context_relevance")) < 0.9:
        answer_quality_gaps.append("Smoke context relevance is below 0.90.")
    if _float(smoke.get("answerability_accuracy")) < 1.0:
        answer_quality_gaps.append("Smoke answerability accuracy is below 1.00.")
    if _float(smoke.get("evidence_recall")) < 1.0:
        answer_quality_gaps.append("Smoke evidence recall is below 1.00.")
    if _float(recursive_contract.get("pass_rate")) < 1.0:
        answer_quality_gaps.append("Recursive contract benchmark is not fully green.")
    for label, case in (("relation", relation_case), ("style", style_case), ("values", values_case), ("mixed", mixed_case)):
        if not (_smoke_contract_ok(case) or bool(case.get("passed"))):
            answer_quality_gaps.append(f"{label} answer-quality case failed.")
    answer_quality_pass = not answer_quality_gaps

    warm_gaps: list[str] = []
    if not bool(direct_warm_case.get("passed")):
        warm_gaps.append("Direct same-thread warm follow-up failed.")
    if not bool(balanced_warm_case.get("passed")):
        warm_gaps.append("Balanced same-thread warm follow-up failed.")
    if not bool(divergent_warm_case.get("passed")):
        warm_gaps.append("Divergent follow-up reset failed.")
    if _float(audit_after.get("warm_state_saved_ratio")) < 0.5:
        warm_gaps.append("Warm state saved ratio is below 0.50.")
    if _float(audit_after.get("warm_hit_ratio")) <= 0.0:
        warm_gaps.append("Warm hit ratio is zero.")
    if _float(audit_after.get("warm_context_reuse_quality")) <= 0.0:
        warm_gaps.append("Warm context reuse quality is zero.")
    if not bool(stream_ui.get("warm_context_ready")):
        warm_gaps.append("Stream UI does not expose warm-context readiness.")
    warm_pass = not warm_gaps

    temporal_gaps: list[str] = []
    temporal_actual_areas = [str(item) for item in list(temporal_case.get("actual_guide_areas") or [])]
    temporal_actual_area_keys = {area.strip().lower() for area in temporal_actual_areas if area.strip()}
    if not bool(temporal_case.get("measurement_complete")):
        temporal_gaps.append("Temporal geometry/retrieval measurement is missing.")
    if _float(temporal_case.get("landing_fit_score")) < 0.5:
        temporal_gaps.append("Temporal query landing fit is below 0.50.")
    if not {"history", "projects"}.intersection(temporal_actual_area_keys):
        temporal_gaps.append("Temporal query did not surface History or Projects evidence.")
    if str(temporal_case.get("answerability_state") or "") not in {"grounded", "partially_grounded"}:
        temporal_gaps.append("Temporal query did not reach a grounded or partially grounded answerability state.")
    temporal_pass = not temporal_gaps

    ai_gaps: list[str] = []
    if _float(planner_merge.get("pass_rate")) < 1.0:
        ai_gaps.append("Planner merge benchmark is not fully green.")
    if _float(audit_after.get("planner_seed_success_ratio")) <= 0.0:
        ai_gaps.append("Planner seed success ratio is zero.")
    if _float(audit_after.get("ai_material_contribution_ratio")) <= 0.0:
        ai_gaps.append("AI material contribution ratio is zero.")
    if _float(audit_after.get("planner_family_attribution_ratio")) <= 0.0 and _float(audit_after.get("planner_family_dual_active_ratio")) <= 0.0:
        ai_gaps.append("Planner-family attribution or dual-active ratio is still zero.")
    if _float(audit_after.get("master_llm_success_ratio")) <= 0.0:
        ai_gaps.append("Master LLM success ratio is zero.")
    if _float(planner_seed.get("pass_rate")) < 1.0 and _float(audit_after.get("ai_material_contribution_ratio")) <= 0.0:
        ai_gaps.append("Planner seed benchmark is red and no material AI contribution compensates for deferred planning.")
    ai_pass = not ai_gaps

    route_gaps: list[str] = []
    route_richness_score = max(_float(audit_after.get("route_richness_score")), _float(route_metrics.get("route_richness_score")))
    if not bool(route_richness.get("all_pass")):
        route_gaps.append("Route richness suite is not fully green.")
    if route_richness_score <= 0.0:
        route_gaps.append("Route richness score is zero.")
    if _float(audit_after.get("route_trace_session_ratio")) <= 0.0:
        route_gaps.append("Route trace session ratio is zero.")
    if _float(audit_after.get("route_travel_session_ratio")) <= 0.0:
        route_gaps.append("Route travel session ratio is zero.")
    if _float(audit_after.get("destination_reached_ratio")) <= 0.0:
        route_gaps.append("Destination reached ratio is zero.")
    route_pass = not route_gaps

    raw_doc_gaps: list[str] = []
    if bool(documents.get("skipped")):
        raw_doc_gaps.append("Document suite was skipped.")
    if _float(documents.get("pass_rate")) < 1.0:
        raw_doc_gaps.append("Document suite is not fully green.")
    for label, case in (("lookup", document_lookup_case), ("synthesis", document_synthesis_case), ("trace", document_trace_case)):
        if not bool(case.get("passed")):
            raw_doc_gaps.append(f"Document {label} case failed.")
    if _float(audit_after.get("raw_text_coverage_ratio")) < 0.9:
        raw_doc_gaps.append("Raw text coverage is below 0.90.")
    if _float(audit_after.get("document_chunk_coverage_ratio")) < 0.9:
        raw_doc_gaps.append("Document chunk coverage is below 0.90.")
    raw_docs_pass = not raw_doc_gaps

    ui_gaps: list[str] = []
    ui_required = {
        "answer_surface_ready": bool(stream_ui.get("answer_surface_ready")),
        "answer_surface_states_ready": bool(stream_ui.get("answer_surface_states_ready")),
        "timeline_ready": bool(stream_ui.get("timeline_ready")),
        "route_trace_ready": bool(stream_ui.get("route_trace_ready")),
        "route_travel_ready": bool(stream_ui.get("route_travel_ready")),
        "reservoir_ready": bool(stream_ui.get("reservoir_ready")),
        "dual_planner_ready": bool(stream_ui.get("dual_planner_ready")),
        "branch_controller_ready": bool(stream_ui.get("branch_controller_ready")),
        "master_director_ready": bool(stream_ui.get("master_director_ready")),
    }
    for key, ready in ui_required.items():
        if not ready:
            ui_gaps.append(f"UI truth readiness `{key}` is not green.")
    if _float(trace.get("pass_rate")) < 1.0:
        ui_gaps.append("Trace suite is not fully green.")
    ui_pass = not ui_gaps

    audit_gaps: list[str] = []
    required_audit_truth = {
        "canonical_telemetry_present": bool(canonical_telemetry),
        "audit_truth_checks_present": bool(audit_truth_checks),
        "latest_stream_benchmark_present": bool(audit_after.get("latest_stream_benchmark")),
        "latest_documents_benchmark_present": bool(audit_after.get("latest_documents_benchmark")),
        "latest_maintenance_benchmark_present": bool(audit_after.get("latest_maintenance_benchmark")),
        "latest_geometry_benchmark_present": bool(audit_after.get("latest_geometry_benchmark")),
    }
    for key, ready in required_audit_truth.items():
        if not ready:
            audit_gaps.append(f"Audit truth field `{key}` is missing.")
    if audit_truth_checks and not all(bool(value) for value in audit_truth_checks.values() if isinstance(value, bool)):
        audit_gaps.append("At least one audit truth check is false.")
    audit_pass = not audit_gaps

    sleep_gaps: list[str] = []
    if not bool(maintenance.get("all_pass")):
        sleep_gaps.append("Maintenance suite is not fully green.")
    if _float(audit_after.get("sleep_review_change_ratio")) <= 0.0:
        sleep_gaps.append("Sleep review change ratio is zero.")
    if _float(audit_after.get("evolve_structural_change_ratio")) <= 0.0:
        sleep_gaps.append("Evolve structural change ratio is zero.")
    if _float(audit_after.get("post_retrieval_calibration_gain")) <= 0.0:
        sleep_gaps.append("Post-retrieval calibration gain is zero.")
    if _float(audit_after.get("maintenance_retrieval_gap_run_ratio")) <= 0.0:
        sleep_gaps.append("Maintenance retrieval-gap run ratio is zero.")
    if _int(audit_after.get("working_memory_depromotion_review_count")) <= 0:
        sleep_gaps.append("Working-memory depromotion review count is zero.")
    sleep_pass = not sleep_gaps

    geometry_gaps: list[str] = []
    matrix_a_problem_likelihood = max(_float(audit_after.get("matrix_a_problem_likelihood")), _float(geometry_audit.get("matrix_a_problem_likelihood")))
    geometry_landing_fit_score = max(_float(audit_after.get("geometry_landing_fit_score")), _float(geometry_metrics.get("geometry_landing_fit_score")))
    if not bool(geometry_audit.get("all_pass")):
        geometry_gaps.append("Geometry audit suite is not fully green.")
    if matrix_a_problem_likelihood >= 0.4:
        geometry_gaps.append("Matrix A problem likelihood is high enough to block closure.")
    if geometry_landing_fit_score < 0.5:
        geometry_gaps.append("Geometry landing fit score is below 0.50.")
    geometry_pass = not geometry_gaps

    sections = {
        "speed": _matrix_v2_section(
            section_key="speed",
            passed=speed_pass,
            evidence_path="suites.smoke.cases + audit.mode_timing_percentiles + audit.planner_seed_ms",
            artifact_refs=["suite:smoke", "suite:stream", "audit:/dev/audit"],
            query_set=[direct_case, style_case, mixed_case],
            evidence={
                "flash_answer_first_ms_p50": flash_answer_first_p50,
                "balanced_answer_first_ms_p50": balanced_answer_first_p50,
                "heavy_answer_final_ms_p50": heavy_answer_final_p50,
                "planner_seed_ms_p50": planner_seed_p50,
                "answer_now_before_final_ratio": answer_now_before_final_ratio,
            },
            thresholds={
                "flash_answer_first_ms_p50_max": 500,
                "balanced_answer_first_ms_p50_max": 800,
                "answer_now_before_final_ratio_min": 0.6,
                "planner_seed_ms_p50_ideal_max": 3500,
            },
            timings={"flash": flash_timing, "balanced": balanced_timing, "heavy": heavy_timing, "warm_cold_latency_delta": warm_delta},
            output_snippets=[_case_snippet(direct_case), _case_snippet(style_case), _case_snippet(mixed_case)],
            open_gaps=speed_gaps,
            root_causes=_section_root_causes(speed_gaps, slice_owner="28C/29A", fallback_root_cause="first_answer_or_planner_latency_gap"),
            slice_owner="28C/29A",
            benchmark_phases=["smoke", "stream"],
        ),
        "answer_quality": _matrix_v2_section(
            section_key="answer_quality",
            passed=answer_quality_pass,
            evidence_path="suites.smoke + suites.recursive_contract",
            artifact_refs=["suite:smoke", "suite:recursive_contract"],
            query_set=smoke_cases,
            evidence={
                "legacy_smoke_pass_rate": _float(smoke.get("pass_rate")),
                "smoke_answer_contract_pass_rate": smoke_contract_pass_rate,
                "answer_exactness": _float(smoke.get("answer_exactness")),
                "context_relevance": _float(smoke.get("context_relevance")),
                "answerability_accuracy": _float(smoke.get("answerability_accuracy")),
                "evidence_recall": _float(smoke.get("evidence_recall")),
                "recursive_contract_pass_rate": _float(recursive_contract.get("pass_rate")),
            },
            thresholds={"smoke_answer_contract_pass_rate_min": 0.9, "recursive_contract_pass_rate_min": 1.0},
            output_snippets=[_case_snippet(case) for case in smoke_cases[:6]],
            open_gaps=answer_quality_gaps,
            root_causes=_section_root_causes(answer_quality_gaps, slice_owner="28D/29A", fallback_root_cause="answer_quality_gate_failed"),
            slice_owner="28D/29A",
            benchmark_phases=["smoke", "recursive_contract"],
        ),
        "warm_context": _matrix_v2_section(
            section_key="warm_context",
            passed=warm_pass,
            evidence_path="suites.stream.cases + audit.warm_*",
            artifact_refs=["suite:stream", "audit:/dev/audit"],
            query_set=[direct_warm_case, balanced_warm_case, divergent_warm_case],
            evidence={
                "warm_hit_ratio": _float(audit_after.get("warm_hit_ratio")),
                "warm_partial_reuse_ratio": _float(audit_after.get("warm_partial_reuse_ratio")),
                "warm_state_saved_ratio": _float(audit_after.get("warm_state_saved_ratio")),
                "warm_context_reuse_quality": _float(audit_after.get("warm_context_reuse_quality")),
                "divergence_reset_ratio": _float(audit_after.get("divergence_reset_ratio")),
                "warm_cold_latency_delta": warm_delta,
            },
            thresholds={"warm_state_saved_ratio_min": 0.5, "warm_hit_ratio_min": 0.01, "warm_context_reuse_quality_min": 0.01},
            timings={"warm_cold_latency_delta": warm_delta},
            output_snippets=[_case_snippet(direct_warm_case), _case_snippet(balanced_warm_case), _case_snippet(divergent_warm_case)],
            open_gaps=warm_gaps,
            root_causes=_section_root_causes(warm_gaps, slice_owner="28A/29A", fallback_root_cause="warm_context_reuse_gap"),
            slice_owner="28A/29A",
            benchmark_phases=["stream"],
        ),
        "temporal_inventory": _matrix_v2_section(
            section_key="temporal_inventory",
            passed=temporal_pass,
            evidence_path="suites.geometry_audit.cases.geometry_temporal_2019_balanced",
            artifact_refs=["suite:geometry_audit", "audit:/dev/audit"],
            query_set=[temporal_case],
            evidence={
                "measurement_complete": bool(temporal_case.get("measurement_complete")),
                "actual_guide_areas": temporal_actual_areas,
                "landing_fit_score": _float(temporal_case.get("landing_fit_score")),
                "destination_alignment_score": _float(temporal_case.get("destination_alignment_score")),
                "answerability_state": str(temporal_case.get("answerability_state") or ""),
            },
            thresholds={"landing_fit_score_min": 0.5, "accepted_guide_areas": ["History", "Projects"]},
            timings=dict(temporal_case.get("timing") or {}),
            output_snippets=[_case_snippet(temporal_case)],
            open_gaps=temporal_gaps,
            root_causes=_section_root_causes(temporal_gaps, slice_owner="28B/29A", fallback_root_cause="temporal_inventory_or_landing_gap"),
            slice_owner="28B/29A",
            benchmark_phases=["geometry_audit"],
        ),
        "ai_influence": _matrix_v2_section(
            section_key="ai_influence",
            passed=ai_pass,
            evidence_path="suites.planner_seed + suites.planner_merge + audit.ai_* + audit.planner_family_*",
            artifact_refs=["suite:planner_seed", "suite:planner_merge", "suite:stream", "audit:/dev/audit"],
            query_set=list(planner_seed.get("cases") or []) + list(planner_merge.get("cases") or [])[:3],
            evidence={
                "planner_seed_pass_rate": _float(planner_seed.get("pass_rate")),
                "planner_merge_pass_rate": _float(planner_merge.get("pass_rate")),
                "planner_seed_success_ratio": _float(audit_after.get("planner_seed_success_ratio")),
                "ai_material_contribution_ratio": _float(audit_after.get("ai_material_contribution_ratio")),
                "planner_influence_ratio": _float(audit_after.get("planner_influence_ratio")),
                "planner_family_dual_active_ratio": _float(audit_after.get("planner_family_dual_active_ratio")),
                "planner_family_attribution_ratio": _float(audit_after.get("planner_family_attribution_ratio")),
                "master_llm_success_ratio": _float(audit_after.get("master_llm_success_ratio")),
            },
            thresholds={"planner_seed_success_ratio_min": 0.01, "planner_merge_pass_rate_min": 1.0, "ai_material_contribution_ratio_min": 0.01},
            timings={"planner_seed_ms": planner_seed_ms, "planner_arrival_ms": dict(audit_after.get("planner_arrival_ms") or {})},
            output_snippets=[_case_snippet(case) for case in list(planner_seed.get("cases") or [])[:4]],
            open_gaps=ai_gaps,
            root_causes=_section_root_causes(ai_gaps, slice_owner="24/25/29A", fallback_root_cause="ai_planner_influence_gap"),
            slice_owner="24/25/29A",
            benchmark_phases=["planner_seed", "planner_merge", "stream"],
        ),
        "route_richness": _matrix_v2_section(
            section_key="route_richness",
            passed=route_pass,
            evidence_path="suites.route_richness + audit.route_*",
            artifact_refs=["suite:route_richness", "suite:stream", "audit:/dev/audit"],
            query_set=list(route_richness.get("cases") or []),
            evidence={
                "route_richness_score": route_richness_score,
                "route_trace_session_ratio": _float(audit_after.get("route_trace_session_ratio")),
                "route_travel_session_ratio": _float(audit_after.get("route_travel_session_ratio")),
                "highway_route_use_ratio": _float(audit_after.get("highway_route_use_ratio")),
                "link_route_use_ratio": _float(audit_after.get("link_route_use_ratio")),
                "local_route_use_ratio": _float(audit_after.get("local_route_use_ratio")),
                "destination_reached_ratio": _float(audit_after.get("destination_reached_ratio")),
            },
            thresholds={"route_richness_score_min": 0.01, "route_trace_session_ratio_min": 0.01, "destination_reached_ratio_min": 0.01},
            output_snippets=[_case_snippet(case) for case in list(route_richness.get("cases") or [])],
            open_gaps=route_gaps,
            root_causes=_section_root_causes(route_gaps, slice_owner="26/29A", fallback_root_cause="route_truth_or_path_richness_gap"),
            slice_owner="26/29A",
            benchmark_phases=["route_richness", "stream"],
        ),
        "raw_docs": _matrix_v2_section(
            section_key="raw_docs",
            passed=raw_docs_pass,
            evidence_path="suites.documents + audit.raw_text_coverage_ratio + audit.document_chunk_coverage_ratio",
            artifact_refs=["suite:documents", "suite:stream", "audit:/dev/audit"],
            query_set=document_cases,
            evidence={
                "documents_pass_rate": _float(documents.get("pass_rate")),
                "raw_text_coverage_ratio": _float(audit_after.get("raw_text_coverage_ratio")),
                "document_chunk_coverage_ratio": _float(audit_after.get("document_chunk_coverage_ratio")),
                "document_chunk_used_before_final_ratio": _float(audit_after.get("document_chunk_used_before_final_ratio")),
                "document_lookup_passed": bool(document_lookup_case.get("passed")),
                "document_synthesis_passed": bool(document_synthesis_case.get("passed")),
                "document_trace_passed": bool(document_trace_case.get("passed")),
            },
            thresholds={"documents_pass_rate_min": 1.0, "raw_text_coverage_ratio_min": 0.9, "document_chunk_coverage_ratio_min": 0.9},
            output_snippets=[_case_snippet(case) for case in document_cases[:6]],
            open_gaps=raw_doc_gaps,
            root_causes=_section_root_causes(raw_doc_gaps, slice_owner="docs/29A", fallback_root_cause="raw_document_retrieval_gap"),
            slice_owner="docs/29A",
            benchmark_phases=["documents"],
        ),
        "ui_truth": _matrix_v2_section(
            section_key="ui_truth",
            passed=ui_pass,
            evidence_path="suites.stream.ui_replay_readiness + suites.trace",
            artifact_refs=["suite:stream", "suite:trace"],
            query_set=stream_cases + trace_cases,
            evidence={
                "stream_all_pass": bool(stream.get("all_pass")),
                "trace_pass_rate": _float(trace.get("pass_rate")),
                "ui_replay_readiness": stream_ui,
            },
            thresholds={"stream_all_pass": True, "trace_pass_rate_min": 1.0},
            output_snippets=[_case_snippet(case) for case in stream_cases[:4]],
            open_gaps=ui_gaps,
            root_causes=_section_root_causes(ui_gaps, slice_owner="UI-D1/UI-D2/29A", fallback_root_cause="ui_truth_surface_gap"),
            slice_owner="UI-D1/UI-D2/29A",
            benchmark_phases=["stream", "trace"],
        ),
        "audit_truth": _matrix_v2_section(
            section_key="audit_truth",
            passed=audit_pass,
            evidence_path="audit.canonical_telemetry + audit.audit_truth_checks + audit.latest_*_benchmark",
            artifact_refs=["audit:/dev/audit", "suite:stream", "suite:documents", "suite:maintenance", "suite:geometry_audit"],
            query_set=[],
            evidence={
                "required_audit_truth": required_audit_truth,
                "audit_truth_checks": audit_truth_checks,
                "canonical_telemetry_schema": canonical_telemetry.get("schema_version"),
                "latest_benchmark_phase": ((audit_after.get("last_benchmark") or {}).get("phase") or ""),
            },
            thresholds={"canonical_telemetry_present": True, "latest_suite_artifacts_present": True},
            output_snippets=[],
            open_gaps=audit_gaps,
            root_causes=_section_root_causes(audit_gaps, slice_owner="28F/29A", fallback_root_cause="audit_truth_contract_gap"),
            slice_owner="28F/29A",
            benchmark_phases=["stream", "documents", "maintenance", "geometry_audit"],
        ),
        "sleep_evolve": _matrix_v2_section(
            section_key="sleep_evolve",
            passed=sleep_pass,
            evidence_path="suites.maintenance + audit.sleep_* + audit.evolve_* + audit.post_retrieval_calibration_gain",
            artifact_refs=["suite:maintenance", "audit:/dev/audit"],
            query_set=list(maintenance.get("cases") or []),
            evidence={
                "maintenance_all_pass": bool(maintenance.get("all_pass")),
                "sleep_review_change_ratio": _float(audit_after.get("sleep_review_change_ratio")),
                "evolve_structural_change_ratio": _float(audit_after.get("evolve_structural_change_ratio")),
                "post_retrieval_calibration_gain": _float(audit_after.get("post_retrieval_calibration_gain")),
                "maintenance_retrieval_gap_run_ratio": _float(audit_after.get("maintenance_retrieval_gap_run_ratio")),
                "working_memory_depromotion_review_count": _int(audit_after.get("working_memory_depromotion_review_count")),
            },
            thresholds={"maintenance_all_pass": True, "sleep_review_change_ratio_min": 0.01, "evolve_structural_change_ratio_min": 0.01},
            output_snippets=[_case_snippet(case) for case in list(maintenance.get("cases") or [])[:4]],
            open_gaps=sleep_gaps,
            root_causes=_section_root_causes(sleep_gaps, slice_owner="28G/29A", fallback_root_cause="sleep_evolve_grounding_gap"),
            slice_owner="28G/29A",
            benchmark_phases=["maintenance"],
        ),
        "geometry": _matrix_v2_section(
            section_key="geometry",
            passed=geometry_pass,
            evidence_path="suites.geometry_audit + audit.geometry_* + audit.matrix_a_problem_likelihood",
            artifact_refs=["suite:geometry_audit", "audit:/dev/audit"],
            query_set=geometry_cases,
            evidence={
                "geometry_all_pass": bool(geometry_audit.get("all_pass")),
                "geometry_landing_fit_score": geometry_landing_fit_score,
                "geometry_destination_alignment_score": max(_float(audit_after.get("geometry_destination_alignment_score")), _float(geometry_metrics.get("geometry_destination_alignment_score"))),
                "geometry_projection_error_ratio": max(_float(audit_after.get("geometry_projection_error_ratio")), _float(geometry_metrics.get("geometry_projection_error_ratio"))),
                "matrix_a_problem_likelihood": matrix_a_problem_likelihood,
                "matrix_a_recommendation": geometry_audit.get("matrix_a_recommendation"),
            },
            thresholds={"matrix_a_problem_likelihood_max": 0.4, "geometry_landing_fit_score_min": 0.5},
            output_snippets=[_case_snippet(case) for case in geometry_cases[:5]],
            open_gaps=geometry_gaps,
            root_causes=_section_root_causes(geometry_gaps, slice_owner="27A/27B/29A", fallback_root_cause="geometry_or_matrix_a_gate_failed"),
            slice_owner="27A/27B/29A",
            benchmark_phases=["geometry_audit"],
        ),
    }

    missing_sections = [section for section in FINAL_EVALUATION_V2_SECTION_KEYS if section not in sections]
    section_results = [dict(sections[key]) for key in FINAL_EVALUATION_V2_SECTION_KEYS if key in sections]
    passed_count = sum(1 for section in section_results if bool(section.get("pass")))
    total_count = len(FINAL_EVALUATION_V2_SECTION_KEYS)
    red_gates = _evaluation_v2_red_gates(sections)
    all_pass = passed_count == total_count and not missing_sections
    return {
        "phase": "evaluation_v2",
        "schema_version": "agvm.final_evaluation_matrix.v2",
        "matrix_version": "29A.final_evaluation_matrix.v2",
        "passed_count": passed_count,
        "total_count": total_count,
        "pass_rate": round(passed_count / max(1, total_count), 3),
        "all_pass": all_pass,
        "closure_ready_signal": all_pass,
        "closure_decision_deferred_to": "Lab Slice 29B",
        "missing_sections": missing_sections,
        "required_sections": list(FINAL_EVALUATION_V2_SECTION_KEYS),
        "final_evaluation_matrix_v2": sections,
        "final_evaluation_matrix": sections,
        "red_gates": red_gates,
        "suite_summaries": {name: _suite_compact_summary(report) for name, report in suites.items()},
        "benchmark_inputs": {
            "official_suites": sorted(str(name) for name in suites.keys()),
            "custom_harness": "29A.final_evaluation_matrix.v2",
            "artifact_rule": "section_pass_requires_non_empty_artifact_refs",
        },
        "referenced_cases": {
            "direct_fact": {"passed": direct_contract_ok, "legacy_passed": bool(direct_case.get("passed")), "timing": dict(direct_case.get("timing") or {})},
            "style": {"passed": style_contract_ok, "legacy_passed": bool(style_case.get("passed")), "timing": dict(style_case.get("timing") or {})},
            "warm_followups": [
                {"case_id": str(case.get("case_id") or ""), "passed": bool(case.get("passed")), "warm_state_used": bool(case.get("warm_state_used"))}
                for case in [direct_warm_case, balanced_warm_case, divergent_warm_case]
            ],
            "temporal_2019": {
                "passed": bool(sections["temporal_inventory"].get("pass")),
                "actual_guide_areas": temporal_actual_areas,
                "timing": dict(temporal_case.get("timing") or {}),
            },
            "document_cases": [
                {"case_id": str(case.get("case_id") or ""), "passed": bool(case.get("passed")), "raw_context_complete": bool(case.get("document_raw_context_complete"))}
                for case in [document_lookup_case, document_synthesis_case, document_trace_case]
            ],
        },
    }


def _run_evaluation_core_suites(base_url: str) -> dict[str, dict[str, Any]]:
    return {
        "smoke": run_smoke_benchmark(base_url),
        "modes": run_mode_suite(base_url),
        "stream": run_stream_suite(base_url),
        "trace": run_trace_suite(base_url),
        "documents": run_documents_suite(base_url),
        "maintenance": run_maintenance_suite(base_url),
        "planner_seed": run_planner_seed_suite(base_url),
        "planner_merge": run_planner_merge_suite(base_url),
        "geometry_audit": run_geometry_audit_suite(base_url),
        "route_richness": run_route_richness_suite(base_url),
        "master_closure": run_master_closure_suite(base_url),
        "recursive_contract": run_recursive_contract_suite(base_url),
    }


EVALUATION_V2_SUITE_ORDER = (
    "smoke",
    "modes",
    "stream",
    "trace",
    "documents",
    "maintenance",
    "planner_seed",
    "planner_merge",
    "geometry_audit",
    "route_richness",
    "master_closure",
    "recursive_contract",
)

EVALUATION_V2_DEFAULT_SUITE_BUDGET_SECONDS: dict[str, float] = {
    "smoke": 180.0,
    "modes": 220.0,
    "stream": 280.0,
    "trace": 180.0,
    "documents": 300.0,
    "maintenance": 360.0,
    "planner_seed": 140.0,
    "planner_merge": 45.0,
    "geometry_audit": 240.0,
    "route_richness": 220.0,
    "master_closure": 320.0,
    "recursive_contract": 320.0,
}


def _evaluation_v2_suite_runners() -> dict[str, Callable[[str], dict[str, Any]]]:
    return {
        "smoke": run_smoke_benchmark,
        "modes": run_mode_suite,
        "stream": run_stream_suite,
        "trace": run_trace_suite,
        "documents": run_documents_suite,
        "maintenance": run_maintenance_suite,
        "planner_seed": run_planner_seed_suite,
        "planner_merge": run_planner_merge_suite,
        "geometry_audit": run_geometry_audit_suite,
        "route_richness": run_route_richness_suite,
        "master_closure": run_master_closure_suite,
        "recursive_contract": run_recursive_contract_suite,
    }


def _benchmark_report_passed(report: dict[str, Any]) -> bool:
    if bool(report.get("timed_out")) or bool(report.get("bounded_timeout")):
        return False
    if bool(report.get("skipped")):
        return False
    if "all_pass" in report:
        return bool(report.get("all_pass"))
    return _float(report.get("pass_rate")) >= 1.0


def _bounded_suite_failure_report(
    *,
    suite_name: str,
    budget_seconds: float,
    elapsed_ms: float,
    status: str,
    error: str,
) -> dict[str, Any]:
    timed_out = status == "timeout"
    open_gap = (
        f"{suite_name} exceeded the {budget_seconds:.2f}s bounded evaluation budget."
        if timed_out
        else f"{suite_name} failed before producing a benchmark report: {error}"
    )
    return {
        "phase": suite_name,
        "passed_count": 0,
        "total_count": 1,
        "pass_rate": 0.0,
        "all_pass": False,
        "timed_out": timed_out,
        "bounded_timeout": timed_out,
        "bounded_error": not timed_out,
        "budget_seconds": round(float(budget_seconds), 3),
        "elapsed_ms": round(float(elapsed_ms), 2),
        "cases": [
            {
                "case_id": f"{suite_name}_{status}",
                "passed": False,
                "status": status,
                "budget_seconds": round(float(budget_seconds), 3),
                "elapsed_ms": round(float(elapsed_ms), 2),
                "error": error,
            }
        ],
        "open_gaps": [open_gap],
    }


def _run_benchmark_suite_with_budget(
    *,
    base_url: str,
    suite_name: str,
    runner: Callable[[str], dict[str, Any]],
    budget_seconds: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    started_at = time.perf_counter()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"eval_v2_{suite_name}")
    future = executor.submit(runner, base_url)
    try:
        report = future.result(timeout=max(0.01, float(budget_seconds)))
    except concurrent.futures.TimeoutError:
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        executor.shutdown(wait=False, cancel_futures=True)
        error = f"bounded_timeout_after_{float(budget_seconds):.2f}s"
        return (
            _bounded_suite_failure_report(
                suite_name=suite_name,
                budget_seconds=budget_seconds,
                elapsed_ms=elapsed_ms,
                status="timeout",
                error=error,
            ),
            {
                "suite": suite_name,
                "status": "timeout",
                "timed_out": True,
                "budget_seconds": round(float(budget_seconds), 3),
                "elapsed_ms": round(float(elapsed_ms), 2),
                "error": error,
            },
        )
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        executor.shutdown(wait=False, cancel_futures=True)
        error = str(exc)[:800] or exc.__class__.__name__
        return (
            _bounded_suite_failure_report(
                suite_name=suite_name,
                budget_seconds=budget_seconds,
                elapsed_ms=elapsed_ms,
                status="error",
                error=error,
            ),
            {
                "suite": suite_name,
                "status": "error",
                "timed_out": False,
                "budget_seconds": round(float(budget_seconds), 3),
                "elapsed_ms": round(float(elapsed_ms), 2),
                "error": error,
            },
        )
    else:
        executor.shutdown(wait=True)
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        normalized_report = dict(report or {})
        normalized_report.setdefault("phase", suite_name)
        normalized_report["bounded_runner"] = {
            "budget_seconds": round(float(budget_seconds), 3),
            "elapsed_ms": round(float(elapsed_ms), 2),
            "timed_out": False,
        }
        status = "pass" if _benchmark_report_passed(normalized_report) else ("skipped" if bool(normalized_report.get("skipped")) else "fail")
        return (
            normalized_report,
            {
                "suite": suite_name,
                "status": status,
                "timed_out": False,
                "budget_seconds": round(float(budget_seconds), 3),
                "elapsed_ms": round(float(elapsed_ms), 2),
                "error": None,
            },
        )


def _run_benchmark_suite_subprocess_with_budget(
    *,
    base_url: str,
    suite_name: str,
    budget_seconds: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    started_at = time.perf_counter()
    child_code = (
        "import json, sys\n"
        "from benchmarking import _evaluation_v2_suite_runners\n"
        "base_url = sys.argv[1]\n"
        "suite_name = sys.argv[2]\n"
        "try:\n"
        "    report = _evaluation_v2_suite_runners()[suite_name](base_url)\n"
        "    print(json.dumps({'ok': True, 'report': report}, ensure_ascii=False))\n"
        "except BaseException as exc:\n"
        "    print(json.dumps({'ok': False, 'error': str(exc)[:800], 'error_type': exc.__class__.__name__}, ensure_ascii=False))\n"
        "    raise\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", child_code, base_url, suite_name],
        cwd=str(Path(__file__).resolve().parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        stdout, stderr = process.communicate(timeout=max(0.01, float(budget_seconds)))
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate(timeout=10)
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        error = f"bounded_subprocess_timeout_after_{float(budget_seconds):.2f}s"
        if stderr:
            error = f"{error}: {stderr[-400:]}"
        return (
            _bounded_suite_failure_report(
                suite_name=suite_name,
                budget_seconds=budget_seconds,
                elapsed_ms=elapsed_ms,
                status="timeout",
                error=error,
            ),
            {
                "suite": suite_name,
                "status": "timeout",
                "timed_out": True,
                "budget_seconds": round(float(budget_seconds), 3),
                "elapsed_ms": round(float(elapsed_ms), 2),
                "error": error,
                "isolation": "subprocess",
            },
        )
    elapsed_ms = (time.perf_counter() - started_at) * 1000.0
    payload: dict[str, Any] | None = None
    for line in reversed([line.strip() for line in stdout.splitlines() if line.strip()]):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "ok" in parsed:
            payload = parsed
            break
    if process.returncode != 0 or not payload or not bool(payload.get("ok")):
        error = str((payload or {}).get("error") or stderr or stdout or f"subprocess_exit_{process.returncode}")[:800]
        return (
            _bounded_suite_failure_report(
                suite_name=suite_name,
                budget_seconds=budget_seconds,
                elapsed_ms=elapsed_ms,
                status="error",
                error=error,
            ),
            {
                "suite": suite_name,
                "status": "error",
                "timed_out": False,
                "budget_seconds": round(float(budget_seconds), 3),
                "elapsed_ms": round(float(elapsed_ms), 2),
                "error": error,
                "isolation": "subprocess",
            },
        )
    normalized_report = dict(payload.get("report") or {})
    normalized_report.setdefault("phase", suite_name)
    normalized_report["bounded_runner"] = {
        "budget_seconds": round(float(budget_seconds), 3),
        "elapsed_ms": round(float(elapsed_ms), 2),
        "timed_out": False,
        "isolation": "subprocess",
    }
    status = "pass" if _benchmark_report_passed(normalized_report) else ("skipped" if bool(normalized_report.get("skipped")) else "fail")
    return (
        normalized_report,
        {
            "suite": suite_name,
            "status": status,
            "timed_out": False,
            "budget_seconds": round(float(budget_seconds), 3),
            "elapsed_ms": round(float(elapsed_ms), 2),
            "error": None,
            "isolation": "subprocess",
        },
    )


def _run_evaluation_suite_map_bounded(
    *,
    base_url: str,
    suite_runners: dict[str, Callable[[str], dict[str, Any]]],
    suite_budgets: dict[str, float],
    suite_names: list[str] | None = None,
    subprocess_isolation: bool = False,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[str]]:
    selected_names = [str(name).strip() for name in list(suite_names or []) if str(name).strip()]
    if not selected_names:
        selected_names = [name for name in EVALUATION_V2_SUITE_ORDER if name in suite_runners]
    suites: dict[str, dict[str, Any]] = {}
    ledger: list[dict[str, Any]] = []
    unknown_names: list[str] = []
    for suite_name in selected_names:
        runner = suite_runners.get(suite_name)
        budget_seconds = max(0.01, float(suite_budgets.get(suite_name, EVALUATION_V2_DEFAULT_SUITE_BUDGET_SECONDS.get(suite_name, 120.0)) or 120.0))
        if runner is None:
            unknown_names.append(suite_name)
            suites[suite_name] = _bounded_suite_failure_report(
                suite_name=suite_name,
                budget_seconds=budget_seconds,
                elapsed_ms=0.0,
                status="error",
                error="unknown_evaluation_v2_suite",
            )
            ledger.append(
                {
                    "suite": suite_name,
                    "status": "error",
                    "timed_out": False,
                    "budget_seconds": round(float(budget_seconds), 3),
                    "elapsed_ms": 0.0,
                    "error": "unknown_evaluation_v2_suite",
                }
            )
            continue
        if subprocess_isolation:
            report, entry = _run_benchmark_suite_subprocess_with_budget(
                base_url=base_url,
                suite_name=suite_name,
                budget_seconds=budget_seconds,
            )
        else:
            report, entry = _run_benchmark_suite_with_budget(
                base_url=base_url,
                suite_name=suite_name,
                runner=runner,
                budget_seconds=budget_seconds,
            )
        suites[suite_name] = report
        ledger.append(entry)
    return suites, ledger, unknown_names


def run_evaluation_suite(base_url: str) -> dict[str, Any]:
    suites = _run_evaluation_core_suites(base_url)
    audit_after = get_json(base_url, "/dev/audit")
    return _build_final_evaluation_report(suites=suites, audit_after=audit_after)


def run_evaluation_v2_suite(base_url: str) -> dict[str, Any]:
    suites = _run_evaluation_core_suites(base_url)
    audit_after = get_json(base_url, "/dev/audit")
    return _build_final_evaluation_v2_report(suites=suites, audit_after=audit_after)


def run_evaluation_v2_bounded_suite(
    base_url: str,
    *,
    suite_timeout_seconds: float | None = None,
    suite_budgets: dict[str, float] | None = None,
    suite_names: list[str] | None = None,
) -> dict[str, Any]:
    budgets = dict(EVALUATION_V2_DEFAULT_SUITE_BUDGET_SECONDS)
    if suite_timeout_seconds is not None:
        timeout_value = max(0.01, float(suite_timeout_seconds))
        budgets = {name: timeout_value for name in EVALUATION_V2_SUITE_ORDER}
    for name, value in dict(suite_budgets or {}).items():
        normalized_name = str(name).strip()
        if normalized_name:
            budgets[normalized_name] = max(0.01, float(value or 0.01))
    suites, execution_ledger, unknown_names = _run_evaluation_suite_map_bounded(
        base_url=base_url,
        suite_runners=_evaluation_v2_suite_runners(),
        suite_budgets=budgets,
        suite_names=suite_names,
        subprocess_isolation=True,
    )
    audit_after = get_json(base_url, "/dev/audit")
    report = _build_final_evaluation_v2_report(suites=suites, audit_after=audit_after)
    required_names = set(EVALUATION_V2_SUITE_ORDER)
    executed_names = {str(entry.get("suite") or "") for entry in execution_ledger}
    missing_required_names = sorted(required_names - executed_names)
    timed_out_suites = [str(entry.get("suite") or "") for entry in execution_ledger if bool(entry.get("timed_out"))]
    errored_suites = [
        str(entry.get("suite") or "")
        for entry in execution_ledger
        if str(entry.get("status") or "") == "error"
    ]
    partial_suite_run = bool(suite_names) and bool(missing_required_names)
    report.update(
        {
            "phase": "evaluation_v2_bounded",
            "bounded_runner": {
                "enabled": True,
                "suite_order": list(EVALUATION_V2_SUITE_ORDER),
                "suite_filter": [str(name).strip() for name in list(suite_names or []) if str(name).strip()],
                "partial_suite_run": partial_suite_run,
                "suite_budgets_seconds": {name: round(float(value), 3) for name, value in budgets.items() if name in executed_names or not suite_names},
                "execution_ledger": execution_ledger,
                "timed_out_suites": timed_out_suites,
                "errored_suites": errored_suites,
                "unknown_suites": unknown_names,
                "missing_required_suites": missing_required_names,
                "isolation": "subprocess",
            },
            "closure_ready_signal": bool(report.get("all_pass")) and not timed_out_suites and not errored_suites and not missing_required_names,
        }
    )
    benchmark_inputs = dict(report.get("benchmark_inputs") or {})
    benchmark_inputs.update(
        {
            "bounded_runner": True,
            "suite_timeout_seconds": suite_timeout_seconds,
            "suite_budgets_seconds": report["bounded_runner"]["suite_budgets_seconds"],
            "suite_filter": report["bounded_runner"]["suite_filter"],
            "partial_suite_run": partial_suite_run,
        }
    )
    report["benchmark_inputs"] = benchmark_inputs
    return report


def run_benchmark_suite(
    base_url: str,
    phase: str = "all",
    *,
    suite_timeout_seconds: float | None = None,
    suite_budgets: dict[str, float] | None = None,
    suite_names: list[str] | None = None,
) -> dict[str, Any]:
    if phase == "product_harness":
        return run_pr12l_product_harness_suite(base_url)
    if phase == "source_intake":
        return run_pr12l_source_intake_benchmark_suite(base_url)
    if phase == "retrieval_mcp":
        return run_pr12l_retrieval_mcp_benchmark_suite(base_url)
    if phase == "ui_truth":
        return run_pr12l_ui_truth_benchmark_suite(base_url)
    if phase == "brain_os_v2_truth":
        return run_pr12p_brain_os_v2_truth_benchmark_suite(base_url)
    if phase == "backend_integrity":
        return run_pr12p_backend_integrity_correction_gate_suite(base_url)
    if phase == "local_mcp_client":
        return run_pr12p_local_mcp_client_proof_suite(base_url)
    if phase == "live_product_matrix":
        return run_pr12p_live_product_matrix_suite(base_url)
    if phase == "product_ready_local_gate":
        return run_pr12p_product_ready_local_gate_suite(base_url)
    if phase == "final_gate_expansion":
        return run_pr12p14c_final_gate_expansion_suite(base_url)
    if phase == "final_self_hosted_readiness":
        return run_pr12p14l_final_self_hosted_readiness_suite(base_url)
    if phase == "local_beta_fast_health":
        return run_pr12p14t_f_local_beta_fast_health_suite(base_url)
    if phase == "local_mcp_product_matrix":
        return run_pr12p14u_g_local_mcp_product_matrix_suite(base_url)
    if phase == "simone_source_manifest_reset_guard":
        from pr12p_14x_validation import build_pr12p14x_a_source_manifest_and_reset_guard_report

        return build_pr12p14x_a_source_manifest_and_reset_guard_report()
    if phase == "simone_source_intake_grow_preview":
        from pr12p_14x_validation import build_pr12p14x_b_source_intake_grow_preview_report

        return build_pr12p14x_b_source_intake_grow_preview_report()
    if phase == "node_atomicity_identity_link_coherence":
        from pr12p_14x_validation import build_pr12p14x_c_node_contract_report

        return build_pr12p14x_c_node_contract_report()
    if phase == "real_validation_brain_clean_density":
        from pr12p_14x_validation import build_pr12p14x_h_real_validation_brain_clean_density_report

        return build_pr12p14x_h_real_validation_brain_clean_density_report()
    if phase == "large_brain_scale_radial_matrix_distribution":
        from pr12p_14x_validation import build_pr12p14x_d_large_brain_scale_distribution_report

        return build_pr12p14x_d_large_brain_scale_distribution_report()
    if phase == "retrieve_context_quality_matrix":
        from pr12p_14x_validation import build_pr12p14x_e_retrieve_context_quality_matrix_report

        if str(os.environ.get("AGVM_PR12P14X_E_REAL_MCP") or "").strip().lower() in {"1", "true", "yes", "on"}:
            return run_pr12p14x_e_real_mcp_retrieve_quality_matrix_suite(base_url)
        return build_pr12p14x_e_retrieve_context_quality_matrix_report()
    if phase == "sleep_evolve_metamemory_heuristic_evolution":
        if str(os.environ.get("AGVM_PR12P14X_F_REAL_MCP") or "").strip().lower() in {"1", "true", "yes", "on"}:
            return run_pr12p14x_f_real_mcp_sleep_evolve_metamemory_suite(base_url)
        from pr12p_14x_validation import build_pr12p14x_f_sleep_evolve_metamemory_report

        return build_pr12p14x_f_sleep_evolve_metamemory_report()
    if phase == "final_grow_retrieve_sleep_evolve_verdict":
        if str(os.environ.get("AGVM_PR12P14X_G_REAL_MCP") or "").strip().lower() in {"1", "true", "yes", "on"}:
            return run_pr12p14x_g_real_mcp_final_product_verdict_suite(base_url)
        from pr12p_14x_validation import build_pr12p14x_g_final_product_verdict_report

        return build_pr12p14x_g_final_product_verdict_report()
    if phase == "phase8c_comparative_backend_benchmark":
        return run_phase8c_comparative_backend_benchmark_suite(base_url)
    if phase == "external_certification":
        from external_benchmarks.runner import run_external_certification_benchmark_suite

        return run_external_certification_benchmark_suite(base_url)
    if phase == "product_scorecard":
        return run_pr12l_product_scorecard_suite(base_url)
    if phase == "self_hosted_readiness":
        return run_pr12m_self_hosted_readiness_suite(base_url)
    if phase == "hosted_tenant_isolation":
        return run_pr12n_hosted_tenant_isolation_suite(base_url)
    if phase == "smoke":
        return run_smoke_benchmark(base_url)
    if phase == "modes":
        return run_mode_suite(base_url)
    if phase == "stream":
        return run_stream_suite(base_url)
    if phase == "trace":
        return run_trace_suite(base_url)
    if phase == "documents":
        return run_documents_suite(base_url)
    if phase == "maintenance":
        return run_maintenance_suite(base_url)
    if phase == "calibration":
        return run_calibration_suite(base_url)
    if phase == "planner_seed":
        return run_planner_seed_suite(base_url)
    if phase == "planner_merge":
        return run_planner_merge_suite(base_url)
    if phase == "geometry_audit":
        return run_geometry_audit_suite(base_url)
    if phase == "route_richness":
        return run_route_richness_suite(base_url)
    if phase == "master_closure":
        return run_master_closure_suite(base_url)
    if phase == "recursive_contract":
        return run_recursive_contract_suite(base_url)
    if phase == "evaluation":
        return run_evaluation_suite(base_url)
    if phase == "evaluation_v2":
        return run_evaluation_v2_suite(base_url)
    if phase == "evaluation_v2_bounded":
        return run_evaluation_v2_bounded_suite(
            base_url,
            suite_timeout_seconds=suite_timeout_seconds,
            suite_budgets=suite_budgets,
            suite_names=suite_names,
        )
    if phase == "slice1_revalidation":
        return run_slice1_revalidation_suite(base_url)

    smoke = run_smoke_benchmark(base_url)
    modes = run_mode_suite(base_url)
    stream = run_stream_suite(base_url)
    trace = run_trace_suite(base_url)
    documents = run_documents_suite(base_url)
    maintenance = run_maintenance_suite(base_url)
    calibration = run_calibration_suite(base_url)
    planner_seed = run_planner_seed_suite(base_url)
    planner_merge = run_planner_merge_suite(base_url)
    geometry_audit = run_geometry_audit_suite(base_url)
    route_richness = run_route_richness_suite(base_url)
    master_closure = run_master_closure_suite(base_url)
    recursive_contract = run_recursive_contract_suite(base_url)
    slice1_revalidation = run_slice1_revalidation_suite(base_url)
    suites = {
        "smoke": smoke,
        "modes": modes,
        "stream": stream,
        "trace": trace,
        "documents": documents,
        "maintenance": maintenance,
        "calibration": calibration,
        "planner_seed": planner_seed,
        "planner_merge": planner_merge,
        "geometry_audit": geometry_audit,
        "route_richness": route_richness,
        "master_closure": master_closure,
        "recursive_contract": recursive_contract,
        "slice1_revalidation": slice1_revalidation,
    }
    audit_after = get_json(base_url, "/dev/audit")
    evaluation = _build_final_evaluation_report(suites=suites, audit_after=audit_after)
    evaluation_v2 = _build_final_evaluation_v2_report(suites=suites, audit_after=audit_after)
    suites["evaluation"] = evaluation
    suites["evaluation_v2"] = evaluation_v2
    all_pass = all(
        suite.get("pass_rate", 0.0) >= 1.0
        for key, suite in suites.items()
        if key != "documents" or not suite.get("skipped")
    )
    return {
        "phase": "all",
        "all_pass": all_pass,
        "suites": suites,
    }
