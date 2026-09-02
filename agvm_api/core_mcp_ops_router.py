# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from contextvars import copy_context
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlparse

from ai_modules_v2 import (
    AiModuleContractError,
)
from brain_registry import BrainRegistryError, resolve_brain_scope
from core_document_registry import remember_preview_document
from derivation import build_seed, persist_selection
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from grow_engine import (
    GrowEngine,
    _append_grow_v3_compiler_execution,
    _grow_v3_cognitive_write_plan,
    _grow_v3_compiler_structural_node,
    _grow_v3_materialized_action,
)
from llm import llm_runtime_status
from local_module_manifest_router import (
    MAINTAIN_MODULE_ID,
    ensure_local_module_entitled,
)
from retrieval import build_index
from runtime_scope import current_brain_id, use_runtime_brain

MCP_MEMORY_OS_LIST_SCHEMA_VERSION = "agvm.mcp_memory_os_list_output.v1"
_GROW_STANDARD_QUESTION_LIMIT = 12
_GROW_INTERACTIVE_COMPILER_TIMEOUT_SECONDS = 120.0
_GROW_INTERACTIVE_HARD_TIMEOUT_SECONDS = 150.0
CORE_GROW_UPLOAD_MAX_BYTES = 30 * 1024 * 1024
CORE_GROW_UPLOAD_CHUNK_BYTES = 1024 * 1024
CORE_DOCUMENT_HYDRATION_PAGE_CHARS = 50_000
CORE_DOCUMENT_TRIAGE_MAX_CANDIDATES = 8
CORE_DOCUMENT_TRIAGE_EVALUATOR_CONCURRENCY = 3
CORE_DOCUMENT_TRIAGE_HYDRATION_CONCURRENCY = 3
CORE_DOCUMENT_TRIAGE_MAX_HYDRATIONS = 3
CORE_DOCUMENT_TRIAGE_EVALUATOR_TIMEOUT_SECONDS = 4
CORE_DOCUMENT_TRIAGE_HYDRATION_TIMEOUT_SECONDS = 15
TRUSTED_SOURCE_RUNTIME_CONTRACT_SCHEMA_VERSION = "agvm.trusted_source_runtime_contract.v1"
TRUSTED_SOURCE_RUNTIME_PRODUCER_MODULE_ID = "agvm_grow_studio"
TRUSTED_SOURCE_RUNTIME_BOUNDARY = "private_source_extraction_runtime_boundary"
TRUSTED_SOURCE_RUNTIME_HANDOFF = "local_module_to_core_grow_investigator"
TRUSTED_SOURCE_WEB_PROVENANCE_TYPES = {"public_web", "external_reference", "public_pdf"}

try:
    from source_investigation import (
        build_file_source_investigation_package,
        build_source_compiler_handoff_proof,
        build_source_formation_contract,
        build_source_investigation_package,
        grow_source_policy_contract,
    )
except ModuleNotFoundError as exc:
    if exc.name != "source_investigation":
        raise

    _CORE_GROW_SOURCE_POLICY: dict[str, Any] = {
        "schema_version": "agvm.grow_source_policy.v1",
        "execution_mode": "adaptive_static_first",
        "defaults": {
            "analyze_images": "off",
            "crawl_scope": "external_bounded",
            "crawl_sublinks": "bounded_external",
            "follow_same_domain": True,
            "explore_external_links": True,
            "include_images": True,
            "use_online_enrichment": True,
            "use_browser_budget": True,
            "render_browser_pages": True,
            "max_pages": 250,
            "max_crawl_pages": 250,
            "max_depth": 1,
            "max_ocr_pages": 8,
            "max_images": 250,
            "max_online_queries": 25,
            "max_external_domains": 25,
            "max_pages_per_external_domain": 30,
            "max_browser_actions": 64,
            "max_browser_scrolls": 24,
            "fetch_timeout_seconds": 15.0,
            "crawl_time_budget_seconds": 1800.0,
            "compiler_preview_timeout_seconds": _GROW_INTERACTIVE_COMPILER_TIMEOUT_SECONDS,
            "question_limit": _GROW_STANDARD_QUESTION_LIMIT,
            "max_units": 1500,
            "max_urls": 250,
            "max_total_chars": 4_000_000,
            "max_remote_file_bytes": 25_000_000,
            "max_source_file_bytes": 50_000_000,
        },
        "ceilings": {
            "max_pages": 250,
            "max_crawl_pages": 250,
            "max_depth": 5,
            "max_ocr_pages": 100,
            "max_images": 1000,
            "max_online_queries": 100,
            "max_external_domains": 25,
            "max_pages_per_external_domain": 30,
            "max_browser_actions": 250,
            "max_browser_scrolls": 100,
            "crawl_time_budget_seconds": 3600.0,
            "compiler_preview_timeout_seconds": 1800.0,
            "max_units": 1500,
            "max_urls": 250,
            "max_total_chars": 10_000_000,
            "max_remote_file_bytes": 50_000_000,
            "max_source_file_bytes": 50_000_000,
        },
        "adaptive": {
            "initial_page_tranche": 8,
            "sufficient_primary_chars": 80_000,
            "minimum_static_page_chars": 900,
            "low_yield_page_chars": 320,
            "max_consecutive_low_yield_pages": 3,
            "online_enrichment_trigger_chars": 24_000,
            "browser_policy": "fallback_when_static_insufficient",
            "vision_policy": "explicit_request_only",
            "online_enrichment_policy": "requested_and_primary_insufficient",
        },
    }

    def grow_source_policy_contract() -> dict[str, Any]:
        return deepcopy(_CORE_GROW_SOURCE_POLICY)

    def build_source_investigation_package(
        raw_input: str,
        *,
        source_label: str | None = None,
        source_uri: str | None = None,
        user_instruction: str | None = None,
        input_kind: str = "auto",
        options: dict[str, Any] | None = None,
        investigation_id: str | None = None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        """Build the public Core source envelope without private extraction."""
        del options
        text = str(raw_input or "").strip()
        source_kind = str(input_kind or "auto").strip().lower() or "auto"
        supplied_uri = str(source_uri or "").strip()
        supplied_uri_is_web = supplied_uri.lower().startswith(("http://", "https://"))
        raw_starts_with_web_uri = text.lower().startswith(("http://", "https://"))
        external_source = source_kind in {"url", "website"} or (
            source_kind == "auto" and (supplied_uri_is_web or raw_starts_with_web_uri)
        )
        evidence_text = text
        if external_source and supplied_uri:
            if evidence_text == supplied_uri:
                evidence_text = ""
            elif evidence_text.startswith(f"{supplied_uri}\n\nNotes:\n"):
                evidence_text = evidence_text.removeprefix(f"{supplied_uri}\n\nNotes:\n").strip()
        fact_eligible = bool(evidence_text) and not external_source
        resolved_investigation_id = str(investigation_id or f"mcp-grow-{uuid.uuid4()}")
        unit_id = f"src_{hashlib.sha256(f'{resolved_investigation_id}:{evidence_text}'.encode()).hexdigest()[:16]}"
        title = str(source_label or source_uri or "Local Grow source").strip()
        source_unit = {
            "unit_id": unit_id,
            "kind": "manual_block" if source_kind == "manual_text" else "external_reference",
            "title": title,
            "source_uri": source_uri,
            "source_type": source_kind,
            "raw_text": evidence_text,
            "char_count": len(evidence_text),
            "token_estimate": max(1, (len(evidence_text) + 3) // 4) if evidence_text else 0,
            "confidence": 0.96 if source_kind == "manual_text" else 0.74,
            "promotion_role": "primary_evidence" if fact_eligible else "supporting_reference",
            "fact_eligible": fact_eligible,
            "status": "available" if fact_eligible else "rich_extraction_required" if external_source else "empty",
        }
        section = {
            "section_id": unit_id,
            "unit_id": unit_id,
            "title": title,
            "kind": source_unit["kind"],
            "text": evidence_text,
            "source_uri": source_uri,
            "source_type": source_kind,
            "promotion_role": source_unit["promotion_role"],
            "fact_eligible": source_unit["fact_eligible"],
        }
        compiler_handoff = {
            "schema_version": "agvm.compiler_handoff.v1",
            "recommended_input_mode": "manual" if fact_eligible else "auto",
            "recommended_source_type": source_kind,
            "structured_sections": [section] if fact_eligible else [],
            "provenance_map": {unit_id: {"source_uri": source_uri, "source_label": title}},
            "mega_text": evidence_text,
        }
        return {
            "schema_version": "agvm.source_investigation.v1",
            "investigation_id": resolved_investigation_id,
            "created_at": str(created_at or datetime.now(timezone.utc).isoformat()),
            "status": "ready" if fact_eligible else "rich_extraction_required" if external_source else "empty",
            "source_label": title,
            "source_uri": source_uri,
            "source_type": source_kind,
            "user_instruction": user_instruction,
            "source_units": [source_unit],
            "source_unit_formation": {
                "schema_version": "agvm.source_unit_formation.v1",
                "unit_ids": [unit_id],
                "source_unit_count": 1,
            },
            "compiler_handoff": compiler_handoff,
            "runtime": {
                "kind": "public_core",
                "rich_extraction_available": False,
            },
        }

    def build_file_source_investigation_package(
        file_bytes: bytes,
        *,
        file_name: str,
        content_type: str | None = None,
        source_label: str | None = None,
        user_instruction: str | None = None,
        input_kind: str = "auto",
        options: dict[str, Any] | None = None,
        investigation_id: str | None = None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        del file_bytes, file_name, content_type, source_label, user_instruction, input_kind, options, investigation_id, created_at
        raise ValueError("rich_file_extraction_unavailable")

    def build_source_compiler_handoff_proof(
        source_package: dict[str, Any],
        preview_bundle: Any | None = None,
    ) -> dict[str, Any]:
        source_units = [
            dict(item)
            for item in list(source_package.get("source_units") or [])
            if isinstance(item, dict)
        ]
        ready = any(bool(unit.get("fact_eligible")) and bool(str(unit.get("raw_text") or "").strip()) for unit in source_units)
        return {
            "schema_version": "agvm.compiler_handoff_proof.v1",
            "investigation_id": str(source_package.get("investigation_id") or ""),
            "source_unit_count": len(source_units),
            "has_preview": bool(preview_bundle),
            "ready": ready,
            "problems": [] if ready else ["rich_extraction_unavailable" if source_units else "no_source_units"],
        }

    def build_source_formation_contract(
        source_package: dict[str, Any],
        preview_bundle: Any | None = None,
        persist_result: Any | None = None,
    ) -> dict[str, Any]:
        del persist_result
        source_units = [
            dict(item)
            for item in list(source_package.get("source_units") or [])
            if isinstance(item, dict)
        ]
        return {
            "schema_version": "agvm.core_source_formation_contract.v1",
            "investigation_id": str(source_package.get("investigation_id") or ""),
            "source_unit_count": len(source_units),
            "preview_present": bool(preview_bundle),
            "state": "preview_ready" if preview_bundle else "handoff_ready",
        }
from schemas import (
    McpClarificationRequest,
    McpGrowApplyRequest,
    McpGrowRollbackRequest,
    McpGrowSourceRequest,
    McpGrowToolExecutionResponse,
    McpMaintenanceApplyRequest,
    McpMaintenanceRequest,
    McpMaintenanceToolExecutionResponse,
    McpMemoryOSListRequest,
    McpWriteMemoryCommitRequest,
    McpWriteMemoryPreviewRequest,
)
from sqlite_store import (
    GROW_INVESTIGATION_V3_SCHEMA_VERSION,
    GrowPreviewBindingStoreError,
    apply_local_grow_v2_preview_transaction,
    append_search_event,
    bootstrap_runtime_store,
    discard_grow_investigation_reservation,
    fetch_atlas,
    fetch_graph_snapshot,
    fetch_grow_investigation,
    fetch_local_grow_v2_preview,
    fetch_recent_maintenance_runs,
    fetch_recent_search_sessions,
    finalize_grow_maintenance_feedback,
    finalize_grow_preview,
    maintenance_graph_revision,
    replace_runtime_graph,
    rollback_local_grow_v2_preview_transaction,
    reserve_grow_investigation,
    resume_grow_investigation,
    store_correction_history,
    store_local_grow_v2_preview,
    store_maintenance_run,
    update_grow_investigation,
)

_GROW_PREVIEW_RUNS: dict[str, dict[str, Any]] = {}
_GROW_PREVIEW_APPLY_LOCK = threading.RLock()
_GROW_ENGINE = GrowEngine()


class TrustedSourcePackageError(ValueError):
    def __init__(self, code: str, *, source_package: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.source_package = source_package or {}


class CoreGrowUploadTooLarge(ValueError):
    pass


async def _read_core_grow_upload_bounded(
    upload: UploadFile,
    *,
    max_bytes: int = CORE_GROW_UPLOAD_MAX_BYTES,
    chunk_bytes: int = CORE_GROW_UPLOAD_CHUNK_BYTES,
) -> bytes:
    limit = max(1, int(max_bytes))
    read_size_limit = max(1, int(chunk_bytes))
    declared_size = getattr(upload, "size", None)
    if isinstance(declared_size, int) and declared_size > limit:
        raise CoreGrowUploadTooLarge

    body = bytearray()
    while True:
        remaining = limit - len(body)
        chunk = await upload.read(min(read_size_limit, remaining + 1))
        if not chunk:
            return bytes(body)
        if len(chunk) > remaining:
            raise CoreGrowUploadTooLarge
        body.extend(chunk)


def _core_upload_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _core_upload_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _core_upload_source_storage_mode(value: Any) -> str:
    mode = str(value or "auto").strip().lower()
    if mode in {"", "auto"}:
        return "auto"
    if mode == "source_bound_only":
        return "source_bound_only"
    raise HTTPException(
        status_code=400,
        detail={
            "code": "unsupported_source_storage_mode",
            "allowed": ["auto", "source_bound_only"],
        },
    )


def _core_upload_source_kind(file_name: str, content_type: str | None, file_bytes: bytes, input_kind: str) -> str:
    explicit = str(input_kind or "auto").strip().lower()
    if explicit in {"pdf", "docx"}:
        return explicit
    lowered_name = str(file_name or "").strip().lower()
    lowered_type = str(content_type or "").strip().lower()
    if file_bytes.startswith(b"%PDF") or lowered_name.endswith(".pdf") or lowered_type == "application/pdf":
        return "pdf"
    if file_bytes.startswith(b"PK\x03\x04") and lowered_name.endswith(".docx"):
        return "docx"
    if lowered_name.endswith(".docx") or lowered_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        return "docx"
    return "unknown"


def _stamp_core_upload_trusted_runtime_contract(source_package: dict[str, Any], *, brain_id: str) -> dict[str, Any]:
    package = deepcopy(dict(source_package or {}))
    package["brain_id"] = brain_id
    source_request = deepcopy(dict(package.get("source_request") or {}))
    provenance = deepcopy(dict(package.get("provenance") or {}))
    canonical_uri = str(
        package.get("source_uri")
        or source_request.get("source_uri")
        or provenance.get("source_uri")
        or ""
    ).strip()
    source_trust = str(
        source_request.get("source_trust")
        or provenance.get("source_trust")
        or provenance.get("source_type")
        or ""
    ).strip()
    if canonical_uri:
        package["source_uri"] = canonical_uri
        source_request["source_uri"] = canonical_uri
        provenance["source_uri"] = canonical_uri
    if source_trust:
        source_request["source_trust"] = source_trust
        provenance.setdefault("source_trust", source_trust)
    if source_request:
        package["source_request"] = source_request
    if provenance:
        package["provenance"] = provenance
    normalized_units: list[dict[str, Any]] = []
    for raw_unit in list(package.get("source_units") or []):
        if not isinstance(raw_unit, dict):
            continue
        unit = deepcopy(dict(raw_unit))
        if canonical_uri and not str(unit.get("source_uri") or "").strip():
            unit["source_uri"] = canonical_uri
        unit_provenance = deepcopy(dict(unit.get("provenance") or {}))
        if canonical_uri and not str(unit_provenance.get("source_uri") or "").strip():
            unit_provenance["source_uri"] = canonical_uri
        if source_trust and not str(unit_provenance.get("source_trust") or "").strip():
            unit_provenance["source_trust"] = source_trust
        if unit_provenance:
            unit["provenance"] = unit_provenance
        proof = deepcopy(dict(unit.get("acquisition_proof") or {}))
        if canonical_uri and not str(proof.get("source_uri") or "").strip():
            proof["source_uri"] = canonical_uri
        if proof:
            unit["acquisition_proof"] = proof
        normalized_units.append(unit)
    if normalized_units:
        package["source_units"] = normalized_units
    handoff = deepcopy(dict(package.get("compiler_handoff") or {}))
    if canonical_uri and handoff:
        normalized_sections: list[dict[str, Any]] = []
        for raw_section in list(handoff.get("structured_sections") or []):
            if not isinstance(raw_section, dict):
                continue
            section = deepcopy(dict(raw_section))
            if not str(section.get("source_uri") or "").strip():
                section["source_uri"] = canonical_uri
            if source_trust and not str(section.get("source_trust") or "").strip():
                section["source_trust"] = source_trust
            normalized_sections.append(section)
        if normalized_sections:
            handoff["structured_sections"] = normalized_sections
        provenance_map = deepcopy(dict(handoff.get("provenance_map") or {}))
        for key, value in list(provenance_map.items()):
            if not isinstance(value, dict):
                continue
            item = deepcopy(dict(value))
            if not str(item.get("source_uri") or "").strip():
                item["source_uri"] = canonical_uri
            if source_trust and not str(item.get("source_trust") or "").strip():
                item["source_trust"] = source_trust
            provenance_map[key] = item
        if provenance_map:
            handoff["provenance_map"] = provenance_map
        package["compiler_handoff"] = handoff
    package["trusted_source_runtime_contract"] = {
        "schema_version": TRUSTED_SOURCE_RUNTIME_CONTRACT_SCHEMA_VERSION,
        "producer_module_id": TRUSTED_SOURCE_RUNTIME_PRODUCER_MODULE_ID,
        "source_extraction_runtime_state": TRUSTED_SOURCE_RUNTIME_BOUNDARY,
        "handoff": TRUSTED_SOURCE_RUNTIME_HANDOFF,
        "single_core_grow_investigator_call": True,
    }
    runtime = dict(package.get("runtime") or {})
    runtime.update(
        {
            "brain_id": brain_id,
            "kind": str(runtime.get("kind") or "local_module_private_source_runtime"),
            "rich_extraction_available": True,
            "original_bytes_retained": False,
            "original_binary_storage": "not_retained_by_core_upload_route",
        }
    )
    package["runtime"] = runtime
    return package


def _source_units_for_document_receipt(source_package: dict[str, Any]) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for index, raw_unit in enumerate(list(source_package.get("source_units") or []), start=1):
        if not isinstance(raw_unit, dict):
            continue
        unit = dict(raw_unit)
        unit_id = str(unit.get("unit_id") or "").strip()
        if not unit_id:
            continue
        text = str(unit.get("raw_text") or unit.get("text") or "").strip()
        units.append(
            {
                "source_unit_id": unit_id,
                "unit_id": unit_id,
                "order": index,
                "kind": str(unit.get("kind") or "document_section"),
                "title": str(unit.get("title") or unit_id),
                "char_count": len(text),
                "content_digest": str(unit.get("content_digest") or dict(unit.get("provenance") or {}).get("hash") or ""),
                "source_uri": unit.get("source_uri"),
                "fact_eligible": bool(unit.get("fact_eligible", True)),
            }
        )
    return units


def _sha256_ref(value: str) -> str:
    cleaned = str(value or "").strip()
    return cleaned if cleaned.startswith("sha256:") else f"sha256:{cleaned}"


def _sha256_text_ref(text: str) -> str:
    return _sha256_ref(hashlib.sha256(str(text or "").encode("utf-8")).hexdigest())


def _document_ref_id(document_id: str) -> str:
    return f"document-ref:{document_id}"


def _document_page_offset(cursor: str | int | None) -> int:
    if cursor is None or cursor == "":
        return 0
    if isinstance(cursor, int):
        return max(0, cursor)
    raw_cursor = str(cursor or "").strip()
    if raw_cursor.startswith("offset:"):
        raw_cursor = raw_cursor.split(":", 1)[1]
    try:
        return max(0, int(raw_cursor))
    except ValueError:
        return 0


def _document_canonical_text_and_sections(source_package: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    handoff = dict(source_package.get("compiler_handoff") or {})
    candidate_sections: list[dict[str, Any]] = []

    def append_section(raw_section: dict[str, Any], index: int) -> None:
        unit_id = str(
            raw_section.get("unit_id")
            or raw_section.get("section_id")
            or raw_section.get("source_unit_id")
            or f"source_unit_{index}"
        ).strip()
        text = str(raw_section.get("raw_text") or raw_section.get("text") or "").strip()
        if not unit_id or not text:
            return
        candidate_sections.append(
            {
                "source_unit_id": unit_id,
                "order": index,
                "title": str(raw_section.get("title") or unit_id),
                "kind": str(raw_section.get("kind") or "document_section"),
                "text": text,
                "char_count": len(text),
            }
        )

    for index, raw_unit in enumerate(list(source_package.get("source_units") or []), start=1):
        if isinstance(raw_unit, dict):
            append_section(dict(raw_unit), index)
    if not candidate_sections:
        for index, raw_section in enumerate(list(handoff.get("structured_sections") or []), start=1):
            if isinstance(raw_section, dict):
                append_section(dict(raw_section), index)

    ordered_text = "\n\n".join(section["text"] for section in candidate_sections).strip()
    mega_text = str(handoff.get("mega_text") or "").strip()
    content = mega_text or ordered_text

    sections: list[dict[str, Any]] = []
    scan_from = 0
    fallback_cursor = 0
    for section in candidate_sections:
        section_text = str(section["text"])
        found = content.find(section_text, scan_from) if content and section_text else -1
        if found < 0:
            found = fallback_cursor
        start = max(0, min(found, len(content))) if content else max(0, found)
        end = min(len(content), start + len(section_text)) if content else start + len(section_text)
        scan_from = max(scan_from, end)
        fallback_cursor = end + 2
        sections.append(
            {
                **section,
                "char_start": start,
                "char_end": end,
                "complete": end - start == len(section_text),
            }
        )
    return content, sections


def _document_ref_v0(
    *,
    brain_id: str,
    document_id: str,
    document_anchor_id: str | None,
    canonical_url: str | None,
    canonical_text_sha256: str,
) -> dict[str, Any]:
    canonical_ref = document_anchor_id or document_id
    return {
        "schema_version": "agvm.document_ref.v0",
        "document_ref_id": _document_ref_id(document_id),
        "document_id": document_id,
        "brain_id": brain_id,
        "document_anchor_id": document_anchor_id,
        "canonical_ref": canonical_ref,
        "canonical_url": canonical_url,
        "content_hash": canonical_text_sha256,
        "body_included": False,
        "hydration_recipe": {
            "mode": "paginated",
            "endpoint": "/mcp/retrieve-document",
            "preview_endpoint": "/mcp/grow-source-upload",
            "document_id": document_id,
            "page_size": CORE_DOCUMENT_HYDRATION_PAGE_CHARS,
            "include_raw_text": True,
        },
    }


def _document_triage_contract_v0(document_ref: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "agvm.document_triage_fanout.v0",
        "document_ref_id": document_ref.get("document_ref_id"),
        "max_candidates": CORE_DOCUMENT_TRIAGE_MAX_CANDIDATES,
        "evaluator_concurrency": CORE_DOCUMENT_TRIAGE_EVALUATOR_CONCURRENCY,
        "hydration_concurrency": CORE_DOCUMENT_TRIAGE_HYDRATION_CONCURRENCY,
        "max_hydrated_documents": CORE_DOCUMENT_TRIAGE_MAX_HYDRATIONS,
        "evaluator_timeout_seconds": CORE_DOCUMENT_TRIAGE_EVALUATOR_TIMEOUT_SECONDS,
        "hydration_timeout_seconds": CORE_DOCUMENT_TRIAGE_HYDRATION_TIMEOUT_SECONDS,
        "total_char_budget": CORE_DOCUMENT_HYDRATION_PAGE_CHARS,
        "dedupe_keys": ["canonical_ref", "content_hash"],
        "cancel_policy": {
            "run_cancel_cancels_evaluators": True,
            "run_cancel_cancels_hydration": True,
            "late_results_ignored": True,
        },
        "section_scoped_hydration": True,
        "document_events_refs_first": True,
        "event_sequence": [
            "document.ref_ready",
            "decision_started",
            "decision",
            "hydration.started",
            "hydration.progress",
            "hydration.completed",
            "hydration.failed",
            "document.open_ready",
        ],
    }


def _document_events_v0(document_ref: dict[str, Any], hydration_page: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "schema_version": "agvm.document_event.v0",
            "event": "document.ref_ready",
            "document_ref_id": document_ref.get("document_ref_id"),
            "document_id": document_ref.get("document_id"),
            "document_anchor_id": document_ref.get("document_anchor_id"),
            "canonical_ref": document_ref.get("canonical_ref"),
            "content_hash": document_ref.get("content_hash"),
            "hydration_recipe": dict(document_ref.get("hydration_recipe") or {}),
            "refs_first": True,
            "contains_full_body": False,
        },
        {
            "schema_version": "agvm.document_event.v0",
            "event": "hydration.completed",
            "document_ref_id": document_ref.get("document_ref_id"),
            "document_id": document_ref.get("document_id"),
            "hydration_result_ref": hydration_page.get("hydration_result_ref"),
            "content_sha256": hydration_page.get("content_sha256"),
            "complete": hydration_page.get("complete"),
            "truncated": hydration_page.get("truncated"),
            "refs_first": True,
            "contains_full_body": False,
        },
        {
            "schema_version": "agvm.document_event.v0",
            "event": "document.open_ready",
            "document_ref_id": document_ref.get("document_ref_id"),
            "document_id": document_ref.get("document_id"),
            "document_anchor_id": document_ref.get("document_anchor_id"),
            "canonical_ref": document_ref.get("canonical_ref"),
            "open_ref": document_ref.get("canonical_ref"),
            "open_url": document_ref.get("canonical_url"),
            "content_hash": document_ref.get("content_hash"),
            "hydration_recipe": dict(document_ref.get("hydration_recipe") or {}),
            "refs_first": True,
            "contains_full_body": False,
        },
    ]


def _document_receipt_v0(
    *,
    brain_id: str,
    source_package: dict[str, Any],
    file_name: str,
    content_type: str | None,
    file_sha256: str,
    byte_count: int,
    grow_response: McpGrowToolExecutionResponse,
) -> dict[str, Any]:
    investigation_id = str(source_package.get("investigation_id") or grow_response.investigation_id or "")
    preview_bundle = dict(grow_response.preview_bundle or {})
    primary_preview = dict(preview_bundle.get("primary_node_preview") or {})
    preview_anchor_id = str(primary_preview.get("id") or "").strip()
    preview_child_ids = _normalized_grow_ids(
        [
            str(dict(node).get("id") or "")
            for node in list(preview_bundle.get("derived_nodes") or [])
            if isinstance(node, dict)
        ]
    )
    source_units = _source_units_for_document_receipt(source_package)
    source_request = dict(source_package.get("source_request") or {})
    provenance = dict(source_package.get("provenance") or {})
    source_label = str(source_package.get("source_label") or source_request.get("source_label") or file_name).strip()
    source_kind = str(dict(source_package.get("source_detection") or {}).get("source_kind") or source_request.get("input_kind") or "")
    canonical_url = provenance.get("source_uri") or source_request.get("source_uri") or None
    canonical_text, _sections = _document_canonical_text_and_sections(source_package)
    canonical_text_sha256 = _sha256_text_ref(canonical_text)
    source_sha256 = _sha256_ref(file_sha256)
    brain_revision = dict(grow_response.investigation or {}).get("brain_revision")
    document_ref = _document_ref_v0(
        brain_id=brain_id,
        document_id=investigation_id,
        document_anchor_id=preview_anchor_id or None,
        canonical_url=canonical_url,
        canonical_text_sha256=canonical_text_sha256,
    )
    return {
        "schema_version": "agvm.document_receipt.v0",
        "brain_id": brain_id,
        "document_id": investigation_id,
        "investigation_id": investigation_id,
        "document_ref_id": document_ref["document_ref_id"],
        "document_anchor_id": preview_anchor_id or None,
        "anchor_node_id": preview_anchor_id or None,
        "chunk_ids": preview_child_ids,
        "canonical_url": canonical_url,
        "source_sha256": source_sha256,
        "canonical_text_sha256": canonical_text_sha256,
        "canonical_text_char_count": len(canonical_text),
        "brain_revision": brain_revision,
        "grow_receipt_id": None,
        "source_label": source_label,
        "source_kind": source_kind,
        "uploaded_file": {
            "file_name": file_name,
            "content_type": content_type,
            "byte_count": byte_count,
            "sha256": file_sha256,
            "source_sha256": source_sha256,
        },
        "anchor": {
            "state": "preview_candidate" if preview_anchor_id else "not_applied",
            "document_anchor_id": preview_anchor_id or None,
            "anchor_node_id": preview_anchor_id or None,
            "persisted": False,
        },
        "source_units": source_units,
        "source_unit_ids": [unit["source_unit_id"] for unit in source_units],
        "child_source_unit_ids": [unit["source_unit_id"] for unit in source_units],
        "document_chunk_ids": preview_child_ids,
        "child_preview_node_ids": preview_child_ids,
        "retrieval_recipe": {
            "mode": "paginated",
            "page_size": CORE_DOCUMENT_HYDRATION_PAGE_CHARS,
            "document_id": investigation_id,
            "document_ref_id": document_ref["document_ref_id"],
            "content_hash": canonical_text_sha256,
            "include_raw_text": True,
            "preview_hydration": {
                "endpoint": "/mcp/grow-source-upload",
                "document_id": investigation_id,
                "source": "hydration_page_v0",
            },
            "after_apply": {
                "tool": "retrieve_document",
                "endpoint": "/mcp/retrieve-document",
                "payload": {
                    "brain_id": brain_id,
                    "document_id": preview_anchor_id or investigation_id,
                    "include_raw_text": True,
                },
            },
        },
        "document_ref": document_ref,
        "canonical_open_target": canonical_url,
        "original_binary_retained": False,
        "original_bytes_retained": False,
        "original_binary_storage": "not_retained_by_core_upload_route",
        "mutation": {
            "graph_mutated": False,
            "apply_required": True,
            "apply_endpoint": "/mcp/grow-source-apply",
        },
        "revision": {
            "investigation_version": grow_response.investigation_version,
            "brain_revision": brain_revision,
        },
    }


def _hydration_page_v0(
    source_package: dict[str, Any],
    *,
    document_id: str,
    cursor: str | int | None = None,
    page_size: int = CORE_DOCUMENT_HYDRATION_PAGE_CHARS,
) -> dict[str, Any]:
    content, sections_with_text = _document_canonical_text_and_sections(source_package)
    page_offset = _document_page_offset(cursor)
    resolved_page_size = max(1, int(page_size or CORE_DOCUMENT_HYDRATION_PAGE_CHARS))
    page_end = min(len(content), page_offset + resolved_page_size)
    page_content = content[page_offset:page_end]
    next_cursor = f"offset:{page_end}" if page_end < len(content) else None
    full_content_sha256 = _sha256_text_ref(content)
    page_content_sha256 = _sha256_text_ref(page_content)
    public_sections: list[dict[str, Any]] = []
    spans: list[dict[str, Any]] = []
    for section in sections_with_text:
        section_text = str(section.get("text") or "")
        public_section = {
            key: value
            for key, value in section.items()
            if key != "text"
        }
        public_sections.append(public_section)
        overlap_start = max(int(section.get("char_start") or 0), page_offset)
        overlap_end = min(int(section.get("char_end") or 0), page_end)
        if overlap_start >= overlap_end:
            continue
        section_relative_start = max(0, overlap_start - int(section.get("char_start") or 0))
        section_relative_end = max(section_relative_start, overlap_end - int(section.get("char_start") or 0))
        spans.append(
            {
                "span_id": f"{document_id}:span:{len(spans) + 1}",
                "ordinal": len(spans) + 1,
                "source_unit_id": section.get("source_unit_id"),
                "title": section.get("title"),
                "kind": section.get("kind"),
                "char_start": overlap_start,
                "char_end": overlap_end,
                "text": section_text[section_relative_start:section_relative_end],
            }
        )
    complete = bool(content) and page_offset == 0 and next_cursor is None and len(page_content) == len(content)
    truncated = False
    return {
        "schema_version": "agvm.hydration_page.v0",
        "document_id": document_id,
        "document_ref_id": _document_ref_id(document_id),
        "hydration_result_ref": f"hydration:{document_id}:offset:{page_offset}:chars:{len(page_content)}",
        "result_ref": f"hydration:{document_id}:offset:{page_offset}:chars:{len(page_content)}",
        "cursor": cursor,
        "page": (page_offset // resolved_page_size) + 1,
        "page_size": resolved_page_size,
        "next_cursor": next_cursor,
        "content": page_content,
        "spans": spans,
        "sections": public_sections,
        "complete": complete,
        "truncated": truncated,
        "content_sha256": full_content_sha256,
        "page_content_sha256": page_content_sha256,
        "content_char_count": len(content),
        "completeness": {
            "complete": complete,
            "truncated": truncated,
            "paginated": next_cursor is not None or page_offset > 0,
            "ordered_source_units": True,
            "source_unit_count": len(public_sections),
            "content_char_count": len(content),
            "page_char_count": len(page_content),
            "cursor": cursor,
            "next_cursor": next_cursor,
            "full_content_sha256": full_content_sha256,
            "page_content_sha256": page_content_sha256,
            "hydration_source": "compiler_handoff.mega_text"
            if str(dict(source_package.get("compiler_handoff") or {}).get("mega_text") or "").strip()
            else "source_units.raw_text",
        },
    }


_CORE_GROW_EXACT_CLAIM_ACTION_RE = re.compile(
    r"\b(?:"
    r"is|are|was|were|has|have|lists?|listed|reports?|reported|states?|stated|"
    r"provides?|provided|offers?|offered|manufactures?|manufactured|exports?|exported|"
    r"operates?|operated|serves?|served|supports?|supported|certif(?:y|ies|ied)|"
    r"approves?|approved|tests?|tested|complies?|complied|includes?|included|"
    r"announces?|announced|partners?|partnered|delivers?|delivered|supplies?|supplied|"
    r"installs?|installed|maintains?|maintained|employs?|employed|founds?|founded|"
    r"headquartered|located|based"
    r")\b",
    re.IGNORECASE,
)

_CORE_GROW_LOW_VALUE_SOURCE_RE = re.compile(
    r"\b(?:"
    r"cookie|cookies|privacy|login|sign in|subscribe|newsletter|copyright|"
    r"all rights reserved|menu|search|skip to|accept|reject|terms of use|"
    r"linkedin|followers?|profile views?|for more information visit"
    r")\b",
    re.IGNORECASE,
)


def _core_source_unit_text(unit: dict[str, Any]) -> str:
    return str(unit.get("raw_text") or unit.get("text") or "").strip()


def _core_source_exact_excerpt(text: str, *, limit: int = 1800) -> tuple[str, int, int]:
    source = str(text or "").strip()
    if not source:
        return "", 0, 0
    if len(source) <= limit:
        return source, 0, len(source)
    clipped = source[:limit]
    boundary = max(clipped.rfind("\n\n"), clipped.rfind(". "), clipped.rfind("; "), clipped.rfind(" "))
    if boundary >= 360:
        clipped = clipped[: boundary + 1]
    return clipped.strip(), 0, len(clipped.strip())


def _core_source_exact_claim_candidates(text: str, *, max_claims: int = 4) -> list[dict[str, Any]]:
    source = str(text or "").strip()
    if not source:
        return []
    normalized = re.sub(r"\s+", " ", source).strip()
    pieces = [
        part.strip(" ;:-")
        for part in re.split(r"(?<=[.!?])\s+|;\s+|\n+", normalized)
        if part.strip(" ;:-")
    ]
    claims: list[dict[str, Any]] = []
    seen: set[str] = set()
    source_lower = source.lower()
    for piece in pieces:
        if len(piece) < 44 or len(piece) > 720:
            continue
        if _CORE_GROW_LOW_VALUE_SOURCE_RE.search(piece):
            continue
        has_action = bool(_CORE_GROW_EXACT_CLAIM_ACTION_RE.search(piece))
        has_number_or_date = bool(re.search(r"\b(?:19|20)\d{2}\b|\b\d+[,.]?\d*\b", piece))
        has_named_subject = bool(re.search(r"\b[A-Z][A-Za-z0-9&.'-]+(?:\s+[A-Z][A-Za-z0-9&.'-]+)+\b", piece))
        if not (has_action and (has_named_subject or has_number_or_date)):
            continue
        key = re.sub(r"[^a-z0-9]+", " ", piece.lower()).strip()
        if not key or key in seen:
            continue
        start = source_lower.find(piece.lower())
        if start < 0:
            continue
        seen.add(key)
        claims.append({"text": piece, "source_span_start": start, "source_span_end": start + len(piece)})
        if len(claims) >= max_claims:
            break
    return claims


def _core_grow_anchor_edge(
    *,
    anchor_id: str,
    target_id: str,
    is_uploaded_document: bool,
    reason_suffix: str,
    quote_start: int | None = None,
    quote_end: int | None = None,
    exact_quote: str | None = None,
) -> dict[str, Any]:
    reason_prefix = "parser_backed_document" if is_uploaded_document else "operator_bound_public_text"
    edge = {
        "source_preview_id": anchor_id,
        "target_preview_id": target_id,
        "edge_type": "derives_from",
        "confidence": 1.0,
        "reason": f"{reason_prefix}_{reason_suffix}",
    }
    if quote_start is not None and quote_end is not None and quote_end >= quote_start:
        edge["quote_start"] = int(quote_start)
        edge["quote_end"] = int(quote_end)
    if exact_quote:
        edge["exact_quote"] = str(exact_quote)
    return edge


def _core_grow_text_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


_CORE_GROW_STOPWORDS = {
    "a",
    "about",
    "across",
    "after",
    "also",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "before",
    "by",
    "can",
    "company",
    "for",
    "from",
    "has",
    "have",
    "in",
    "into",
    "is",
    "it",
    "its",
    "more",
    "of",
    "on",
    "or",
    "over",
    "reports",
    "says",
    "states",
    "that",
    "the",
    "their",
    "this",
    "through",
    "to",
    "under",
    "with",
    "worldwide",
}


def _core_grow_tokens(value: Any) -> set[str]:
    text = _core_grow_text_key(value)
    return {
        token
        for token in text.split()
        if len(token) >= 3 and token not in _CORE_GROW_STOPWORDS
    }


def _core_grow_token_similarity(left: Any, right: Any) -> float:
    left_tokens = _core_grow_tokens(left)
    right_tokens = _core_grow_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    return intersection / union if union else 0.0


def _core_grow_subject_key(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    match = re.search(
        r"\b([A-Z][A-Za-z0-9&.'-]+(?:\s+[A-Z][A-Za-z0-9&.'-]+){0,5})\b",
        text,
    )
    if match:
        return _core_grow_text_key(match.group(1))
    tokens = [
        token
        for token in _core_grow_text_key(text).split()
        if token not in _CORE_GROW_STOPWORDS
    ]
    return " ".join(tokens[:4])


def _core_grow_position(value: Any) -> dict[str, float] | None:
    if not isinstance(value, dict):
        return None
    for key in ("final_position", "base_position", "position", "coordinate", "coordinates"):
        raw = value.get(key)
        if not isinstance(raw, dict):
            continue
        try:
            return {
                "x": float(raw["x"]),
                "y": float(raw["y"]),
                "z": float(raw["z"]),
            }
        except (KeyError, TypeError, ValueError):
            continue
    return None


def _core_grow_coordinate_ref(position: Any) -> dict[str, float] | None:
    if position is None:
        return None
    if isinstance(position, dict):
        try:
            return {
                "x": float(position["x"]),
                "y": float(position["y"]),
                "z": float(position["z"]),
            }
        except (KeyError, TypeError, ValueError):
            return None
    if isinstance(position, (list, tuple)) and len(position) >= 3:
        try:
            return {
                "x": float(position[0]),
                "y": float(position[1]),
                "z": float(position[2]),
            }
        except (TypeError, ValueError):
            return None
    return None


def _core_grow_distance(left: dict[str, float] | None, right: dict[str, float] | None) -> float | None:
    if not left or not right:
        return None
    return math.sqrt(
        (float(left["x"]) - float(right["x"])) ** 2
        + (float(left["y"]) - float(right["y"])) ** 2
        + (float(left["z"]) - float(right["z"])) ** 2
    )


def _core_grow_relation_targets(node: dict[str, Any], relation_key: str) -> list[str]:
    targets: list[str] = []
    for relation in list(node.get(relation_key) or []):
        if isinstance(relation, dict):
            target = str(
                relation.get("target_node_id")
                or relation.get("target_id")
                or relation.get("node_id")
                or ""
            ).strip()
        else:
            target = str(relation or "").strip()
        if target and target not in targets:
            targets.append(target)
    return targets


def _core_grow_graph_edge_neighbors(graph_edges: list[dict[str, Any]]) -> dict[str, set[str]]:
    neighbors: dict[str, set[str]] = {}
    for edge in graph_edges:
        if not isinstance(edge, dict):
            continue
        source_id = str(edge.get("source_id") or edge.get("from") or "").strip()
        target_id = str(edge.get("target_id") or edge.get("to") or "").strip()
        if not source_id or not target_id:
            continue
        neighbors.setdefault(source_id, set()).add(target_id)
        neighbors.setdefault(target_id, set()).add(source_id)
    return neighbors


def _core_grow_add_candidate(
    candidates: dict[str, dict[str, Any]],
    node: dict[str, Any] | None,
    *,
    reason: str,
    score: float | None = None,
) -> bool:
    if not isinstance(node, dict):
        return False
    node_id = str(node.get("id") or "").strip()
    if not node_id:
        return False
    existing = candidates.get(node_id)
    if existing is None:
        candidates[node_id] = {**node, "_grow_preflight_reasons": [reason]}
        if score is not None:
            candidates[node_id]["_grow_preflight_score"] = score
        return True
    reasons = list(existing.get("_grow_preflight_reasons") or [])
    if reason not in reasons:
        reasons.append(reason)
        existing["_grow_preflight_reasons"] = reasons
    if score is not None:
        existing["_grow_preflight_score"] = max(float(existing.get("_grow_preflight_score") or 0.0), score)
    return False


def _core_grow_preflight_candidates(
    *,
    incoming_node: dict[str, Any],
    existing_nodes: list[dict[str, Any]],
    node_lookup: dict[str, dict[str, Any]],
    graph_edge_neighbors: dict[str, set[str]],
    spatial_radius: float = 0.34,
    max_candidates: int = 500,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Build Grow audit candidates from AGVM geometry first, then topology, then text.

    This keeps the mutation planner aligned with the coordinate-first contract:
    read the candidate's spatial neighborhood, inspect local links/highways and
    provenance graph edges, and use text similarity only as a fallback/validator.
    """

    candidates: dict[str, dict[str, Any]] = {}
    counts = {
        "spatial_neighborhood_count": 0,
        "link_neighbor_count": 0,
        "highway_neighbor_count": 0,
        "evidence_edge_neighbor_count": 0,
        "text_similarity_candidate_count": 0,
        "candidate_node_count": 0,
    }
    incoming_position = _core_grow_position(incoming_node)
    spatial_ranked: list[tuple[float, dict[str, Any]]] = []
    if incoming_position:
        for existing in existing_nodes:
            distance_value = _core_grow_distance(incoming_position, _core_grow_position(existing))
            if distance_value is None:
                continue
            if distance_value <= spatial_radius:
                spatial_ranked.append((distance_value, existing))
        spatial_ranked.sort(key=lambda item: item[0])
        for distance_value, existing in spatial_ranked[:160]:
            if _core_grow_add_candidate(
                candidates,
                existing,
                reason="spatial_neighborhood",
                score=max(0.0, 1.0 - distance_value),
            ):
                counts["spatial_neighborhood_count"] += 1

    seed_nodes = list(candidates.values())[:96]
    incoming_relation_targets = {
        "links": _core_grow_relation_targets(incoming_node, "links")
        + _core_grow_relation_targets(incoming_node, "suggested_links"),
        "highways": _core_grow_relation_targets(incoming_node, "highways")
        + _core_grow_relation_targets(incoming_node, "suggested_highways"),
    }
    for target_id in incoming_relation_targets["links"]:
        if _core_grow_add_candidate(candidates, node_lookup.get(target_id), reason="incoming_link"):
            counts["link_neighbor_count"] += 1
    for target_id in incoming_relation_targets["highways"]:
        if _core_grow_add_candidate(candidates, node_lookup.get(target_id), reason="incoming_highway"):
            counts["highway_neighbor_count"] += 1

    for source in seed_nodes:
        for target_id in _core_grow_relation_targets(source, "links")[:12]:
            if _core_grow_add_candidate(candidates, node_lookup.get(target_id), reason="linked_neighbor"):
                counts["link_neighbor_count"] += 1
        for target_id in _core_grow_relation_targets(source, "highways")[:8]:
            if _core_grow_add_candidate(candidates, node_lookup.get(target_id), reason="highway_neighbor"):
                counts["highway_neighbor_count"] += 1
        for target_id in list(graph_edge_neighbors.get(str(source.get("id") or ""), set()))[:12]:
            if _core_grow_add_candidate(candidates, node_lookup.get(target_id), reason="evidence_edge_neighbor"):
                counts["evidence_edge_neighbor_count"] += 1

    text_ranked: list[tuple[float, dict[str, Any]]] = []
    incoming_text = _memory_os_node_text(incoming_node)
    for existing in existing_nodes:
        existing_text = _memory_os_node_text(existing)
        if not existing_text:
            continue
        score = _core_grow_token_similarity(incoming_text, existing_text)
        if score >= 0.18:
            text_ranked.append((score, existing))
    text_ranked.sort(key=lambda item: item[0], reverse=True)
    for score, existing in text_ranked[:120]:
        if _core_grow_add_candidate(candidates, existing, reason="text_similarity", score=score):
            counts["text_similarity_candidate_count"] += 1

    ordered = sorted(
        candidates.values(),
        key=lambda item: (
            0
            if "spatial_neighborhood" in list(item.get("_grow_preflight_reasons") or [])
            else 1
            if any(
                reason in list(item.get("_grow_preflight_reasons") or [])
                for reason in ("incoming_link", "linked_neighbor", "incoming_highway", "highway_neighbor")
            )
            else 2,
            -float(item.get("_grow_preflight_score") or 0.0),
        ),
    )
    counts["candidate_node_count"] = len(ordered[:max_candidates])
    return ordered[:max_candidates], counts


def _core_grow_number_value(raw: str) -> float | None:
    cleaned = str(raw or "").strip().lower().replace(",", "")
    multiplier = 1.0
    if cleaned.endswith("k"):
        multiplier = 1_000.0
        cleaned = cleaned[:-1]
    elif cleaned.endswith("m"):
        multiplier = 1_000_000.0
        cleaned = cleaned[:-1]
    try:
        return float(cleaned) * multiplier
    except ValueError:
        return None


def _core_grow_metric_facts(value: Any) -> dict[str, float]:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    lowered = text.lower()
    metrics: dict[str, float] = {}
    metric_patterns = [
        (
            "employee_count",
            r"\b(?:over|more than|approximately|approx\.?|around|about)?\s*(\d[\d,]*(?:\.\d+)?[km]?)\s+(?:employees|team members|staff|workforce|people)\b",
        ),
        (
            "country_count",
            r"\b(?:over|more than|approximately|approx\.?|around|about)?\s*(\d[\d,]*(?:\.\d+)?[km]?)\s+(?:countries|markets)\b",
        ),
        (
            "facility_count",
            r"\b(?:over|more than|approximately|approx\.?|around|about)?\s*(\d[\d,]*(?:\.\d+)?[km]?)\s+(?:facilities|factories|manufacturing facilities|plants)\b",
        ),
    ]
    for metric, pattern in metric_patterns:
        match = re.search(pattern, lowered, re.IGNORECASE)
        if not match:
            continue
        value_number = _core_grow_number_value(match.group(1))
        if value_number is not None:
            metrics[metric] = value_number
    return metrics


def _core_grow_location_fact(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    match = re.search(
        r"\b(?:headquarters?|headquartered|based|located)\s+(?:is\s+|are\s+|as\s+|in\s+|at\s+)?([^.;,]+(?:,\s*[^.;,]+){0,3})",
        text,
        re.IGNORECASE,
    )
    if not match:
        return ""
    return _core_grow_text_key(match.group(1))


def _core_grow_claim_conflict(
    incoming: dict[str, Any],
    existing: dict[str, Any],
) -> dict[str, Any] | None:
    incoming_text = _memory_os_node_text(incoming)
    existing_text = _memory_os_node_text(existing)
    incoming_subject = _core_grow_subject_key(incoming_text)
    existing_subject = _core_grow_subject_key(existing_text)
    if incoming_subject and existing_subject and incoming_subject != existing_subject:
        return None
    incoming_metrics = _core_grow_metric_facts(incoming_text)
    existing_metrics = _core_grow_metric_facts(existing_text)
    for metric, incoming_value in incoming_metrics.items():
        existing_value = existing_metrics.get(metric)
        if existing_value is None:
            continue
        tolerance = max(1.0, max(abs(incoming_value), abs(existing_value)) * 0.05)
        if abs(incoming_value - existing_value) <= tolerance:
            continue
        return {
            "issue_type": "conflicting_metric_claim",
            "metric": metric,
            "incoming_value": incoming_value,
            "existing_value": existing_value,
            "human_summary": (
                "This source reports a different "
                f"{metric.replace('_', ' ')} than an existing memory."
            ),
        }
    incoming_location = _core_grow_location_fact(incoming_text)
    existing_location = _core_grow_location_fact(existing_text)
    if incoming_location and existing_location and incoming_location != existing_location:
        return {
            "issue_type": "conflicting_location_claim",
            "metric": "location",
            "incoming_value": incoming_location,
            "existing_value": existing_location,
            "human_summary": "This source reports a different company location than an existing memory.",
        }
    return None


def _core_grow_preflight_evidence_ref(
    node: dict[str, Any] | None,
    *,
    match_basis: str,
) -> dict[str, Any]:
    node_dict = dict(node or {})
    provenance = dict(node_dict.get("provenance") or {})
    position = _core_grow_position(node_dict)
    evidence_ref = {
        "node_id": str(node_dict.get("id") or ""),
        "match_basis": match_basis,
        "preflight_reasons": list(node_dict.get("_grow_preflight_reasons") or []),
        "source_ref_id": str(
            node_dict.get("source_ref_id")
            or provenance.get("source_ref_id")
            or provenance.get("content_digest")
            or provenance.get("hash")
            or ""
        ),
        "content_digest": str(
            node_dict.get("content_digest")
            or provenance.get("content_digest")
            or provenance.get("hash")
            or ""
        ),
        "coordinate": _core_grow_coordinate_ref(position),
    }
    span_start = node_dict.get("source_span_start")
    span_end = node_dict.get("source_span_end")
    if span_start is not None and span_end is not None:
        try:
            resolved_start = int(span_start)
            resolved_end = int(span_end)
        except (TypeError, ValueError):
            resolved_start = -1
            resolved_end = -1
        if resolved_start >= 0 and resolved_end >= resolved_start:
            evidence_ref["source_span_start"] = resolved_start
            evidence_ref["source_span_end"] = resolved_end
    quote_start = node_dict.get("quote_start", evidence_ref.get("source_span_start"))
    quote_end = node_dict.get("quote_end", evidence_ref.get("source_span_end"))
    if quote_start is not None and quote_end is not None:
        try:
            resolved_quote_start = int(quote_start)
            resolved_quote_end = int(quote_end)
        except (TypeError, ValueError):
            resolved_quote_start = -1
            resolved_quote_end = -1
        if resolved_quote_start >= 0 and resolved_quote_end >= resolved_quote_start:
            evidence_ref["quote_start"] = resolved_quote_start
            evidence_ref["quote_end"] = resolved_quote_end
    exact_quote = str(node_dict.get("exact_quote") or "").strip()
    if exact_quote:
        evidence_ref["exact_quote"] = exact_quote[:900]
    return evidence_ref


def _core_grow_incoming_source_ref(node: dict[str, Any]) -> dict[str, Any]:
    provenance = dict(node.get("provenance") or {})
    source_ref = {
        "preview_id": str(node.get("id") or ""),
        "source_unit_id": str(node.get("source_unit_id") or ""),
        "source_ref_id": str(
            node.get("source_ref_id")
            or provenance.get("source_ref_id")
            or provenance.get("content_digest")
            or provenance.get("hash")
            or ""
        ),
        "source_uri": node.get("source_uri") or provenance.get("source_uri"),
        "content_digest": str(
            node.get("content_digest")
            or provenance.get("content_digest")
            or provenance.get("hash")
            or ""
        ),
    }
    span_start = node.get("source_span_start")
    span_end = node.get("source_span_end")
    if span_start is not None and span_end is not None:
        try:
            resolved_start = int(span_start)
            resolved_end = int(span_end)
        except (TypeError, ValueError):
            resolved_start = -1
            resolved_end = -1
        if resolved_start >= 0 and resolved_end >= resolved_start:
            source_ref["source_span_start"] = resolved_start
            source_ref["source_span_end"] = resolved_end
    exact_quote = str(node.get("exact_quote") or "").strip()
    if not exact_quote and str(node.get("source_bound_role") or "") == "atomic_claim_evidence":
        exact_quote = _memory_os_node_text(node)
    if exact_quote:
        source_ref["exact_quote"] = exact_quote[:900]
    quote_start = node.get("quote_start", source_ref.get("source_span_start"))
    quote_end = node.get("quote_end", source_ref.get("source_span_end"))
    if quote_start is not None and quote_end is not None:
        try:
            resolved_quote_start = int(quote_start)
            resolved_quote_end = int(quote_end)
        except (TypeError, ValueError):
            resolved_quote_start = -1
            resolved_quote_end = -1
        if resolved_quote_start >= 0 and resolved_quote_end >= resolved_quote_start:
            source_ref["quote_start"] = resolved_quote_start
            source_ref["quote_end"] = resolved_quote_end
    return source_ref


def _core_grow_operation_plan(
    *,
    anchor_id: str,
    primary_node: dict[str, Any],
    derived_nodes: list[dict[str, Any]],
    graph: dict[str, Any] | None,
) -> dict[str, Any]:
    graph_dict = dict(graph or {})
    existing_by_text: dict[str, dict[str, Any]] = {}
    existing_by_source: dict[str, dict[str, Any]] = {}
    existing_nodes: list[dict[str, Any]] = []
    for existing in list(graph_dict.get("nodes") or []):
        if not isinstance(existing, dict):
            continue
        node_id = str(existing.get("id") or "").strip()
        if not node_id:
            continue
        existing_nodes.append(existing)
        text_key = _core_grow_text_key(existing.get("raw_text") or existing.get("summary"))
        if text_key and text_key not in existing_by_text:
            existing_by_text[text_key] = existing
        provenance = dict(existing.get("provenance") or {})
        source_key = str(
            existing.get("source_ref_id")
            or provenance.get("source_ref_id")
            or provenance.get("content_digest")
            or provenance.get("hash")
            or ""
        ).strip()
        if source_key and source_key not in existing_by_source:
            existing_by_source[source_key] = existing

    node_lookup = {str(node.get("id") or ""): node for node in existing_nodes if str(node.get("id") or "").strip()}
    graph_edges = [
        edge
        for edge in list(graph_dict.get("edges") or graph_dict.get("graph_edges") or [])
        if isinstance(edge, dict)
    ]
    graph_edge_neighbors = _core_grow_graph_edge_neighbors(graph_edges)
    operations: list[dict[str, Any]] = []
    preflight_matches: list[dict[str, Any]] = []
    preflight_totals = {
        "spatial_neighborhood_count": 0,
        "link_neighbor_count": 0,
        "highway_neighbor_count": 0,
        "evidence_edge_neighbor_count": 0,
        "text_similarity_candidate_count": 0,
        "candidate_node_count": 0,
    }

    def operation_for(node: dict[str, Any], default_operation: str) -> dict[str, Any]:
        nonlocal preflight_totals
        preview_id = str(node.get("id") or "").strip()
        node_text = _memory_os_node_text(node)
        document_role = str(node.get("document_role") or "").strip()
        conflict_eligible = document_role in {"fact", "claim"} or str(
            node.get("source_bound_role") or ""
        ).strip() == "atomic_claim_evidence"
        text_key = _core_grow_text_key(node.get("raw_text") or node.get("summary"))
        provenance = dict(node.get("provenance") or {})
        source_key = str(
            node.get("source_ref_id")
            or provenance.get("source_ref_id")
            or provenance.get("content_digest")
            or provenance.get("hash")
            or ""
        ).strip()
        duplicate = existing_by_text.get(text_key) if text_key else None
        source_duplicate = existing_by_source.get(source_key) if source_key else None
        equivalent: dict[str, Any] | None = None
        equivalent_score = 0.0
        conflict: dict[str, Any] | None = None
        conflict_target: dict[str, Any] | None = None
        candidate_nodes, candidate_counts = _core_grow_preflight_candidates(
            incoming_node=node,
            existing_nodes=existing_nodes,
            node_lookup=node_lookup,
            graph_edge_neighbors=graph_edge_neighbors,
        )
        for key, value in candidate_counts.items():
            preflight_totals[key] = int(preflight_totals.get(key, 0)) + int(value or 0)
        if not duplicate:
            for existing in candidate_nodes:
                existing_text = _memory_os_node_text(existing)
                if not existing_text:
                    continue
                score = _core_grow_token_similarity(node_text, existing_text)
                if score > equivalent_score:
                    equivalent_score = score
                    equivalent = existing
                if conflict_eligible and not conflict:
                    conflict_candidate = _core_grow_claim_conflict(node, existing)
                    if conflict_candidate:
                        conflict = conflict_candidate
                        conflict_target = existing
        near_equivalent = equivalent if equivalent_score >= 0.86 else None
        target = duplicate or source_duplicate
        if target:
            target_text = _memory_os_node_text(target)
            if source_duplicate and not duplicate and len(node_text) > len(target_text) + 120:
                operation = {
                    "preview_id": preview_id,
                    "operation": "update_node_text",
                    "role": str(node.get("document_role") or ""),
                    "source_unit_id": str(node.get("source_unit_id") or ""),
                    "target_node_id": str(target.get("id") or ""),
                    "target_node_ids": [str(target.get("id") or "")],
                    "reason": "same_source_identity_with_richer_text",
                    "old_text_preview": target_text[:360],
                    "new_text_preview": node_text[:360],
                    "mutation_tool": "change-node-content",
                    "requires_review": True,
                    "semantic_decision_authority": "human_or_provider_required",
                    "incoming_source_ref": _core_grow_incoming_source_ref(node),
                    "preflight_evidence_refs": [
                        _core_grow_preflight_evidence_ref(target, match_basis="same_source_identity")
                    ],
                    "preflight_reasons": list(source_duplicate.get("_grow_preflight_reasons") or []),
                }
                preflight_matches.append(operation)
                return operation
            return {
                "preview_id": preview_id,
                "operation": "no_op_duplicate"
                if duplicate
                else "merge_duplicate",
                "role": str(node.get("document_role") or ""),
                "source_unit_id": str(node.get("source_unit_id") or ""),
                "target_node_id": str(target.get("id") or ""),
                "target_node_ids": [str(target.get("id") or "")],
                "reason": "exact_text_duplicate" if duplicate else "same_source_identity",
                "semantic_decision_authority": (
                    "exact_text_or_source_identity_only"
                    if duplicate or source_duplicate
                    else "human_or_provider_required"
                ),
                "incoming_source_ref": _core_grow_incoming_source_ref(node),
                "preflight_evidence_refs": [
                    _core_grow_preflight_evidence_ref(
                        target,
                        match_basis="exact_text_duplicate" if duplicate else "same_source_identity",
                    )
                ],
                "preflight_reasons": list(target.get("_grow_preflight_reasons") or []),
            }
        if near_equivalent:
            operation = {
                "preview_id": preview_id,
                "operation": "merge_duplicate",
                "role": str(node.get("document_role") or ""),
                "source_unit_id": str(node.get("source_unit_id") or ""),
                "target_node_id": str(near_equivalent.get("id") or ""),
                "target_node_ids": [str(near_equivalent.get("id") or "")],
                "reason": "near_equivalent_claim_text",
                "similarity": round(equivalent_score, 3),
                "incoming_claim": node_text[:700],
                "existing_claim": _memory_os_node_text(near_equivalent)[:700],
                "mutation_tool": "change-node-content",
                "requires_review": True,
                "semantic_decision_authority": "human_or_provider_required",
                "incoming_source_ref": _core_grow_incoming_source_ref(node),
                "preflight_evidence_refs": [
                    _core_grow_preflight_evidence_ref(
                        near_equivalent,
                        match_basis="text_reservoir_near_equivalent_candidate",
                    )
                ],
                "preflight_reasons": list(near_equivalent.get("_grow_preflight_reasons") or []),
            }
            preflight_matches.append(operation)
            return operation
        if conflict and conflict_target:
            operation = {
                "preview_id": preview_id,
                "operation": "review_conflict",
                "role": str(node.get("document_role") or ""),
                "source_unit_id": str(node.get("source_unit_id") or ""),
                "target_node_id": str(conflict_target.get("id") or ""),
                "target_node_ids": [str(conflict_target.get("id") or "")],
                "reason": conflict.get("issue_type") or "conflicting_source_bound_claim",
                "human_summary": conflict.get("human_summary") or "This source conflicts with an existing memory.",
                "incoming_claim": node_text[:900],
                "existing_claim": _memory_os_node_text(conflict_target)[:900],
                "recommended_action": (
                    "Review the two evidence-backed statements. If one is newer or better sourced, update "
                    "the canonical node with change-node-content; if both are valid in different time windows, "
                    "keep both with explicit dates."
                ),
                "mutation_tool": "change-node-content",
                "requires_review": True,
                "safe_delete": False,
                "conflict": conflict,
                "semantic_decision_authority": "human_or_provider_required",
                "deterministic_detector_role": "triage_candidate_only",
                "incoming_source_ref": _core_grow_incoming_source_ref(node),
                "preflight_evidence_refs": [
                    _core_grow_preflight_evidence_ref(
                        conflict_target,
                        match_basis="coordinate_source_bound_possible_conflict_candidate",
                    )
                ],
                "preflight_reasons": list(conflict_target.get("_grow_preflight_reasons") or []),
            }
            preflight_matches.append(operation)
            return operation
        source_bound_role = str(node.get("source_bound_role") or "").strip()
        source_role_authority = (
            "source_bound_audit_substrate_only"
            if source_bound_role == "atomic_claim_evidence"
            else "source_storage_provenance_hydration"
            if default_operation in {"create_source_node", "create_section_node"}
            else "provider_required_for_semantic_claim_authority"
        )
        return {
            "preview_id": preview_id,
            "operation": default_operation,
            "role": str(node.get("document_role") or ""),
            "source_unit_id": str(node.get("source_unit_id") or ""),
            "target_node_ids": [],
            "semantic_decision_authority": source_role_authority,
            "incoming_source_ref": _core_grow_incoming_source_ref(node),
        }

    operations.append(operation_for(primary_node, "create_source_node"))
    for node in derived_nodes:
        operations.append(
            operation_for(
                node,
                "create_section_node"
                if str(node.get("document_role") or "") in {"chunk", "summary"}
                else "create_claim_node",
            )
        )
    duplicate_count = sum(
        1
        for item in operations
        if str(item.get("operation") or "") in {"no_op_duplicate", "merge_duplicate"}
    )
    conflict_count = sum(1 for item in operations if str(item.get("operation") or "") == "review_conflict")
    update_count = sum(1 for item in operations if str(item.get("operation") or "") == "update_node_text")
    lifecycle_counts = {
        "create": 0,
        "update": 0,
        "merge": 0,
        "delete": 0,
        "review": 0,
        "no_op": 0,
    }
    maintenance_ledger_entries: list[dict[str, Any]] = []
    for item in operations:
        operation_name = str(item.get("operation") or "").strip()
        lifecycle = (
            "create"
            if operation_name in {"create_source_node", "create_section_node", "create_claim_node"}
            else "update"
            if operation_name == "update_node_text"
            else "merge"
            if operation_name == "merge_duplicate"
            else "delete"
            if operation_name in {"delete_or_supersede_node", "delete_existing", "supersede_existing"}
            else "review"
            if operation_name == "review_conflict"
            else "no_op"
            if operation_name == "no_op_duplicate"
            else "review"
        )
        lifecycle_counts[lifecycle] = int(lifecycle_counts.get(lifecycle) or 0) + 1
        maintenance_ledger_entries.append(
            {
                "preview_id": str(item.get("preview_id") or ""),
                "operation": operation_name,
                "lifecycle": lifecycle,
                "target_node_ids": [
                    str(target_id)
                    for target_id in list(item.get("target_node_ids") or [])
                    if str(target_id).strip()
                ],
                "source_unit_id": str(item.get("source_unit_id") or ""),
                "incoming_source_ref": dict(item.get("incoming_source_ref") or {}),
                "preflight_evidence_refs": [
                    dict(ref)
                    for ref in list(item.get("preflight_evidence_refs") or [])
                    if isinstance(ref, dict)
                ],
                "requires_review": bool(item.get("requires_review")),
                "semantic_decision_authority": str(
                    item.get("semantic_decision_authority")
                    or "provider_required_for_semantic_claim_authority"
                ),
                "mutation_tool": item.get("mutation_tool"),
                "safe_delete": bool(item.get("safe_delete", False)),
            }
        )
    return {
        "schema_version": "agvm.grow_operation_plan.v1",
        "strategy": "source_first_coordinate_preflight_create_or_maintain",
        "planner_mode": "source_first_coordinate_link_highway_text_preflight",
        "authority_contract": {
            "schema_version": "agvm.grow_operation_planner_authority.v1",
            "semantic_decision_authority": "provider_required_for_meaning_conflict_merge_or_delete",
            "deterministic_authority_scope": [
                "source_storage_provenance_hydration",
                "coordinate_topology_candidate_reservoir",
                "exact_text_duplicate",
                "exact_source_identity_duplicate",
            ],
            "triage_only_scope": [
                "token_similarity_near_equivalent_candidate",
                "regex_metric_or_location_possible_conflict_candidate",
            ],
            "memory_type_routing_authority": False,
            "apply_delete_without_provider_or_human": False,
        },
        "preflight": {
            "status": "graph_scanned" if graph is not None else "source_bound_only",
            "search_preflight_substrate": "runtime_graph_coordinates_links_highways_evidence_text_reservoir",
            "graph_node_count": len(list(graph_dict.get("nodes") or [])) if graph is not None else None,
            "graph_edge_count": len(graph_edges) if graph is not None else None,
            "memory_type_routing_authority": False,
            "coordinate_first": True,
            "spatial_neighborhood_count": preflight_totals["spatial_neighborhood_count"],
            "link_neighbor_count": preflight_totals["link_neighbor_count"],
            "highway_neighbor_count": preflight_totals["highway_neighbor_count"],
            "evidence_edge_neighbor_count": preflight_totals["evidence_edge_neighbor_count"],
            "text_similarity_candidate_count": preflight_totals["text_similarity_candidate_count"],
            "candidate_node_count": preflight_totals["candidate_node_count"],
            "duplicate_or_equivalent_count": duplicate_count,
            "update_candidate_count": update_count,
            "conflict_count": conflict_count,
            "conflict_scan": "coordinate_source_bound_graph_audit" if graph is not None else "not_available_without_graph",
        },
        "preflight_matches": preflight_matches[:25],
        "maintenance_ledger": {
            "schema_version": "agvm.grow_maintenance_ledger.v1",
            "authority": "operation_plan_preview_only",
            "supported_lifecycles": ["create", "update", "merge", "delete", "review", "no_op"],
            "counts": lifecycle_counts,
            "entries": maintenance_ledger_entries[:80],
            "apply_policy": {
                "create_source_or_section": "explicit_apply_receipt",
                "atomic_claim_evidence": "audit_dedup_conflict_citation_substrate",
                "update_merge_delete": "provider_or_human_review_required",
                "delete_without_review_allowed": False,
            },
        },
        "operations": operations,
    }


def _core_upload_fallback_preview_bundle(
    source_package: dict[str, Any],
    *,
    graph: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_package = _normalize_manual_source_package_provenance(source_package)
    investigation_id = str(source_package.get("investigation_id") or f"doc-upload-{uuid.uuid4()}")
    handoff = dict(source_package.get("compiler_handoff") or {})
    raw_units = [dict(unit) for unit in list(source_package.get("source_units") or []) if isinstance(unit, dict)]
    content = str(handoff.get("mega_text") or "").strip()
    if not content:
        content = "\n\n".join(
            str(unit.get("raw_text") or unit.get("text") or "").strip()
            for unit in raw_units
            if str(unit.get("raw_text") or unit.get("text") or "").strip()
        ).strip()
    anchor_id = f"{investigation_id}:document-anchor"
    source_request = dict(source_package.get("source_request") or {})
    source_provenance = dict(source_package.get("provenance") or {})
    file_sha256 = str(source_request.get("file_hash") or "").strip()
    canonical_text_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    source_uri = str(
        source_request.get("source_uri") or source_provenance.get("source_uri") or ""
    ).strip() or None
    if not source_uri and not file_sha256:
        source_uri = f"urn:agvm:manual-source:sha256:{canonical_text_sha256}"
    is_uploaded_document = bool(file_sha256)
    requested_options = dict(dict(source_request.get("options") or {}).get("requested") or {})
    requested_source_trust = str(
        source_request.get("source_trust")
        or source_provenance.get("source_trust")
        or requested_options.get("source_trust")
        or (
            "uploaded_document"
            if is_uploaded_document
            else "verified_public_source"
            if _is_http_url(source_uri)
            else "unknown"
        )
    ).strip()
    # SourceInvestigation uses the more explicit request vocabulary while the
    # persisted Graph contract uses SourceTrust. Normalize only at that edge.
    source_trust = (
        "verified_public"
        if requested_source_trust in {"public_web", "verified_public_source"}
        else requested_source_trust
    )
    recommended_source_type = str(handoff.get("recommended_source_type") or "").strip()
    source_type = (
        recommended_source_type
        if recommended_source_type
        else "uploaded_document"
        if is_uploaded_document
        else "public_source_text"
    )
    provenance_mode = (
        "uploaded_document_parser" if is_uploaded_document else "operator_bound_public_text"
    )
    grounding_status = "parser_verified" if is_uploaded_document else "operator_bound"
    source_ref_id = (
        f"sha256:{file_sha256.removeprefix('sha256:')}"
        if file_sha256
        else f"sha256:{canonical_text_sha256}"
    )
    derived_nodes: list[dict[str, Any]] = []
    derived_edges: list[dict[str, Any]] = []
    for index, unit in enumerate(raw_units, start=1):
        unit_id = str(unit.get("unit_id") or f"source_unit_{index}").strip()
        unit_text = str(unit.get("raw_text") or unit.get("text") or "").strip()
        unit_digest = str(
            unit.get("content_digest")
            or dict(unit.get("provenance") or {}).get("hash")
            or f"sha256:{hashlib.sha256(unit_text.encode('utf-8')).hexdigest()}"
        ).strip()
        chunk_id = f"{investigation_id}:chunk:{index}"
        chunk_seed = build_seed(
            raw_text=unit_text,
            input_mode="manual",
            provenance_mode=provenance_mode,
            source_label=str(unit.get("title") or source_package.get("source_label") or unit_id),
            source_type=source_type,
            source_uri=unit.get("source_uri") or source_uri,
            source_ref_id=unit_digest,
            source_trust=source_trust,
            claim_status="fact",
            summary_override=str(unit.get("title") or unit_id),
            memory_type_override="document_chunk",
            document_role="chunk",
            document_anchor_id=anchor_id,
            document_chunk_index=index - 1,
            source_unit_id=unit_id,
            source_unit_title=str(unit.get("title") or unit_id),
            source_unit_kind=str(unit.get("kind") or "document_section"),
            persist_mode="create",
        )
        derived_nodes.append(
            {
                **chunk_seed,
                "id": chunk_id,
                "content_hash": unit_digest,
                "source_uri": unit.get("source_uri") or source_uri,
                "source_trust": source_trust,
                "source_type": source_type,
                "source_span_start": 0,
                "source_span_end": len(unit_text),
                "selected_by_default": True,
                "preview_confidence": 1.0,
                "source_grounding_status": grounding_status,
                "source_grounding_score": 1.0,
                "source_bound_contract": {
                    "schema_version": "agvm.source_bound_node_contract.v1",
                    "role": "document_chunk",
                    "authority": "source_storage_provenance_hydration",
                    "requires_parent_source_hydration_for_answer": True,
                },
                "provenance": {
                    **dict(chunk_seed.get("provenance") or {}),
                    **dict(unit.get("provenance") or {}),
                    "source_type": source_type,
                    "source_unit_id": unit_id,
                    "source_uri": unit.get("source_uri") or source_uri,
                    "source_ref_id": unit_digest,
                    "content_digest": unit_digest,
                },
            }
        )
        derived_edges.append(
            _core_grow_anchor_edge(
                anchor_id=anchor_id,
                target_id=chunk_id,
                is_uploaded_document=is_uploaded_document,
                reason_suffix="contains",
            )
        )
        summary_text, summary_start, summary_end = _core_source_exact_excerpt(unit_text)
        if len(summary_text) >= 320:
            summary_id = f"{investigation_id}:summary:{index}"
            summary_seed = build_seed(
                raw_text=summary_text,
                input_mode="manual",
                provenance_mode=provenance_mode,
                source_label=str(unit.get("title") or source_package.get("source_label") or unit_id),
                source_type=source_type,
                source_uri=unit.get("source_uri") or source_uri,
                source_ref_id=unit_digest,
                source_trust=source_trust,
                claim_status="fact",
                summary_override=str(unit.get("title") or source_package.get("source_label") or unit_id),
                memory_type_override="document_summary",
                document_role="summary",
                document_anchor_id=anchor_id,
                document_chunk_index=index - 1,
                source_unit_id=unit_id,
                source_unit_title=str(unit.get("title") or unit_id),
                source_unit_kind=str(unit.get("kind") or "document_section"),
                source_span_start=summary_start,
                source_span_end=summary_end,
                persist_mode="create",
            )
            derived_nodes.append(
                {
                    **summary_seed,
                    "id": summary_id,
                    "content_hash": unit_digest,
                    "source_uri": unit.get("source_uri") or source_uri,
                    "source_trust": source_trust,
                    "source_type": source_type,
                    "selected_by_default": True,
                    "preview_confidence": 0.92,
                    "source_grounding_status": grounding_status,
                    "source_grounding_score": 1.0,
                    "source_bound_role": "rich_section_memory",
                    "source_bound_contract": {
                        "schema_version": "agvm.source_bound_node_contract.v1",
                        "role": "rich_section_memory",
                        "authority": "source_storage_provenance_hydration",
                        "requires_parent_source_hydration_for_answer": True,
                    },
                    "provenance": {
                        **dict(summary_seed.get("provenance") or {}),
                        **dict(unit.get("provenance") or {}),
                        "source_type": source_type,
                        "source_unit_id": unit_id,
                        "source_uri": unit.get("source_uri") or source_uri,
                        "source_ref_id": unit_digest,
                        "content_digest": unit_digest,
                        "source_bound_role": "rich_section_memory",
                    },
                }
            )
            derived_edges.append(
                _core_grow_anchor_edge(
                    anchor_id=anchor_id,
                    target_id=summary_id,
                    is_uploaded_document=is_uploaded_document,
                    reason_suffix="summarizes_section_excerpt",
                )
            )
        for claim_index, claim in enumerate(_core_source_exact_claim_candidates(unit_text), start=1):
            claim_text = str(claim.get("text") or "").strip()
            if not claim_text:
                continue
            quote_start = int(claim.get("source_span_start") or 0)
            quote_end = int(claim.get("source_span_end") or 0)
            claim_id = f"{investigation_id}:fact:{index}:{claim_index}"
            claim_seed = build_seed(
                raw_text=claim_text,
                input_mode="manual",
                provenance_mode=provenance_mode,
                source_label=str(unit.get("title") or source_package.get("source_label") or unit_id),
                source_type=source_type,
                source_uri=unit.get("source_uri") or source_uri,
                source_ref_id=unit_digest,
                source_trust=source_trust,
                claim_status="fact",
                summary_override=claim_text[:180],
                memory_type_override="document_fact",
                derivation_role="claim",
                derivation_confidence=0.84,
                derived_from_preview_id=anchor_id,
                document_role="fact",
                document_anchor_id=anchor_id,
                source_unit_id=unit_id,
                source_unit_title=str(unit.get("title") or unit_id),
                source_unit_kind=str(unit.get("kind") or "document_section"),
                source_span_start=quote_start,
                source_span_end=quote_end,
                evidence_confidence=0.92,
                stability_confidence=0.74,
                persist_mode="create",
            )
            derived_nodes.append(
                {
                    **claim_seed,
                    "id": claim_id,
                    "content_hash": unit_digest,
                    "source_uri": unit.get("source_uri") or source_uri,
                    "source_trust": source_trust,
                    "source_type": source_type,
                    "selected_by_default": True,
                    "preview_confidence": 0.84,
                    "source_grounding_status": grounding_status,
                    "source_grounding_score": 1.0,
                    "source_span_start": quote_start,
                    "source_span_end": quote_end,
                    "quote_start": quote_start,
                    "quote_end": quote_end,
                    "exact_quote": claim_text,
                    "source_span": {
                        "source_unit_id": unit_id,
                        "start": quote_start,
                        "end": quote_end,
                        "text": claim_text,
                    },
                    "source_bound_role": "atomic_claim_evidence",
                    "source_bound_contract": {
                        "schema_version": "agvm.source_bound_node_contract.v1",
                        "role": "atomic_claim_evidence",
                        "authority": "not_standalone_answer_authority",
                        "exact_quote_required": True,
                        "source_span_required": True,
                        "purpose": [
                            "dedup_preflight",
                            "conflict_review",
                            "citation_substrate",
                        ],
                        "requires_parent_source_hydration_for_answer": True,
                        "provider_or_human_required_for_merge_update_delete": True,
                    },
                    "retrieval_affordance": {
                        "schema_version": "agvm.source_claim_retrieval_affordance.v1",
                        "question": "What source-grounded fact does this material state?",
                        "answer_claim": claim_text,
                        "purpose": "support deduplication, conflict review and precise citations",
                    },
                    "provenance": {
                        **dict(claim_seed.get("provenance") or {}),
                        **dict(unit.get("provenance") or {}),
                        "source_type": source_type,
                        "source_unit_id": unit_id,
                        "source_uri": unit.get("source_uri") or source_uri,
                        "source_ref_id": unit_digest,
                        "content_digest": unit_digest,
                        "source_bound_role": "atomic_claim_evidence",
                    },
                }
            )
            derived_edges.append(
                _core_grow_anchor_edge(
                    anchor_id=anchor_id,
                    target_id=claim_id,
                    is_uploaded_document=is_uploaded_document,
                    reason_suffix="supports_exact_claim",
                    quote_start=quote_start,
                    quote_end=quote_end,
                    exact_quote=claim_text,
                )
            )
    anchor_seed = build_seed(
        raw_text=content,
        input_mode="document",
        provenance_mode=provenance_mode,
        source_label=str(source_package.get("source_label") or "Uploaded document"),
        source_type=source_type,
        source_uri=source_uri,
        source_ref_id=source_ref_id,
        source_trust=source_trust,
        claim_status="fact",
        summary_override=str(source_package.get("source_label") or "Uploaded document"),
        document_role="anchor",
        source_unit_id=f"document:{source_ref_id}",
        source_unit_title=str(source_package.get("source_label") or "Uploaded document"),
        source_unit_kind="document_anchor",
        persist_mode="create",
    )
    primary_node = {
        **anchor_seed,
        "id": anchor_id,
        "memory_type": "document_anchor",
        "document_role": "anchor",
        "is_document_anchor": True,
        "full_text_available": bool(content),
        "full_text_char_count": len(content),
        "content_hash": f"sha256:{canonical_text_sha256}",
        "raw_text": content,
        "summary": str(source_package.get("source_label") or "Uploaded document"),
        "source_trust": source_trust,
        "source_uri": source_uri,
        "source_unit_id": f"document:{source_ref_id}",
        "retrieved_at": (
            source_provenance.get("retrieved_at")
            or source_provenance.get("acquired_at")
            or next(
                (
                    unit.get("retrieved_at") or unit.get("acquired_at")
                    for unit in raw_units
                    if unit.get("retrieved_at") or unit.get("acquired_at")
                ),
                None,
            )
        ),
        "source_bound_contract": {
            "schema_version": "agvm.source_bound_node_contract.v1",
            "role": "source_document",
            "authority": "durable_full_text_anchor",
            "raw_text_preserved": bool(content),
            "full_text_available": bool(content),
            "full_text_char_count": len(content),
            "content_hash": f"sha256:{canonical_text_sha256}",
            "children_materialized_in_preview": len(derived_nodes),
        },
        "persist_mode": "create",
        "selected_by_default": True,
        "preview_confidence": 1.0,
        "source_grounding_status": grounding_status,
        "source_grounding_score": 1.0,
        "provenance": {
            **dict(anchor_seed.get("provenance") or {}),
            "source_type": source_type,
            "investigation_id": investigation_id,
            "source_uri": source_uri,
            "source_ref_id": source_ref_id,
            "content_digest": f"sha256:{canonical_text_sha256}",
        },
    }
    bundle = {
        "schema_version": "agvm.grow_preview_bundle.v2",
        "primary_node_preview": primary_node,
        "derived_nodes": derived_nodes,
        "derived_edges": derived_edges,
        "operation_plan": _core_grow_operation_plan(
            anchor_id=anchor_id,
            primary_node=primary_node,
            derived_nodes=derived_nodes,
            graph=graph,
        ),
    }
    return _compact_preview_contract(bundle)


_PREVIEW_TIMESTAMP_FIELDS = {
    "acquired_at",
    "created_at",
    "observed_at",
    "published_at",
    "retrieved_at",
    "source_acquired_at",
    "source_published_at",
    "source_retrieved_at",
    "updated_at",
    "valid_from",
    "valid_to",
}


def _canonical_preview_timestamp(value: Any) -> Any:
    raw = str(value or "").strip()
    if not raw:
        return value
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _compact_preview_contract(value: Any) -> Any:
    """Match the nested JSON contract that clients receive and echo on Apply."""

    if isinstance(value, dict):
        return {
            str(key): (
                _canonical_preview_timestamp(item)
                if str(key) in _PREVIEW_TIMESTAMP_FIELDS
                else _compact_preview_contract(item)
            )
            for key, item in value.items()
            if item is not None
        }
    if isinstance(value, list):
        return [_compact_preview_contract(item) for item in value]
    return value


def _core_upload_deterministic_source_attestation(
    source_package: dict[str, Any],
) -> dict[str, Any]:
    source_package = _normalize_manual_source_package_provenance(source_package)
    source_request = dict(source_package.get("source_request") or {})
    file_sha256 = str(source_request.get("file_hash") or "").strip().removeprefix("sha256:")
    canonical_text, _sections = _document_canonical_text_and_sections(source_package)
    canonical_text_sha256 = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    source_uri = str(
        source_request.get("source_uri")
        or dict(source_package.get("provenance") or {}).get("source_uri")
        or ""
    ).strip() or None
    if not source_uri and not file_sha256:
        source_uri = f"urn:agvm:manual-source:sha256:{canonical_text_sha256}"
    requested_options = dict(dict(source_request.get("options") or {}).get("requested") or {})
    source_trust = str(
        source_request.get("source_trust")
        or dict(source_package.get("provenance") or {}).get("source_trust")
        or requested_options.get("source_trust")
        or (
            "uploaded_document"
            if file_sha256
            else "verified_public_source"
            if _is_http_url(source_uri)
            else "unknown"
        )
    ).strip()
    authority_kind = (
        "parser_backed_document"
        if file_sha256
        else "operator_bound_public_text"
        if _is_http_url(source_uri)
        else "operator_bound_manual_text"
    )
    source_unit_sha256: dict[str, str] = {}
    for unit in list(source_package.get("source_units") or []):
        if not isinstance(unit, dict):
            continue
        unit_id = str(unit.get("unit_id") or "").strip()
        raw_text = str(unit.get("raw_text") or unit.get("text") or "")
        if not unit_id or not raw_text.strip():
            continue
        source_unit_sha256[unit_id] = (
            f"sha256:{hashlib.sha256(raw_text.encode('utf-8')).hexdigest()}"
        )
    return {
        "schema_version": "agvm.deterministic_document_source_attestation.v1",
        "status": "completed",
        "authority_kind": authority_kind,
        "provider_executed": False,
        "semantic_claims_emitted": False,
        "source_bound_claims_emitted": True,
        "parser_contract": "agvm.source_readers.v1",
        "source_sha256": f"sha256:{file_sha256 or canonical_text_sha256}",
        "canonical_text_sha256": (
            f"sha256:{canonical_text_sha256}"
        ),
        "source_uri": source_uri,
        "source_trust": source_trust,
        "source_unit_sha256": source_unit_sha256,
        "source_unit_count": len(source_unit_sha256),
        "preview_scope": [
            "document_anchor",
            "document_chunk",
            "document_summary",
            "document_fact",
        ],
    }


def _deterministic_public_text_eligible(payload: McpGrowSourceRequest) -> bool:
    """Select only explicit, operator-bound public text for provider-free storage.

    A URL by itself is never enough.  The caller must provide the exact text,
    an HTTP(S) provenance URI and an explicit public-source trust declaration.
    This path stores verbatim source material only; it does not compile claims.
    """

    return bool(
        not payload.investigation_id
        and str(payload.raw_input or "").strip()
        and str(payload.input_kind or "").strip() == "manual_text"
        and _is_http_url(payload.source_uri)
        and _deterministic_public_text_source_trust(payload)
        in {"public_web", "verified_public_source"}
        and not bool(getattr(payload.options, "semantic_preview", False))
    )


def _grow_source_bound_storage_only_requested(payload: McpGrowSourceRequest) -> bool:
    return str(getattr(payload.options, "source_storage_mode", "auto") or "auto").strip().lower() == "source_bound_only"


def _grow_provider_attested(
    session: dict[str, Any] | None = None,
    attestation: dict[str, Any] | None = None,
) -> bool:
    session_payload = dict(session or {})
    attestation_payload = dict(attestation or {})
    return bool(
        session_payload.get("provider_attested")
        or session_payload.get("provider_executed")
        or attestation_payload.get("provider_executed")
        or (
            str(attestation_payload.get("status") or "").strip().lower() == "completed"
            and bool(str(attestation_payload.get("provider") or "").strip())
        )
    )


def _grow_source_bound_storage_only_semantic_conflict(payload: McpGrowSourceRequest) -> bool:
    return _grow_source_bound_storage_only_requested(payload) and bool(
        getattr(payload.options, "semantic_preview", False)
        or getattr(payload.options, "pause_on_questions", False)
    )


def _deterministic_public_text_source_trust(payload: McpGrowSourceRequest) -> str:
    source_trust = str(payload.options.source_trust or "").strip()
    if source_trust in {"public_web", "verified_public_source"}:
        return source_trust
    if source_trust in {"", "unknown"} and _is_http_url(payload.source_uri):
        return "public_web"
    return source_trust


def _normalize_manual_source_package_provenance(source_package: dict[str, Any]) -> dict[str, Any]:
    """Give manual/local source text a canonical URI before durable validation.

    Browser clients may send no source URI, a local label, or an arbitrary
    ``urn:*`` for pasted text.  The deterministic source-bound authority cannot
    accept arbitrary URNs because Apply receipts need stable provenance.  Use a
    content-derived AGVM URN for non-HTTP manual text and keep HTTP URLs intact.
    Uploaded files are left unchanged because their authority is file-hash
    backed.
    """

    package = deepcopy(dict(source_package or {}))
    source_request = deepcopy(dict(package.get("source_request") or {}))
    file_sha256 = str(source_request.get("file_hash") or "").strip()
    if file_sha256:
        return package
    handoff = deepcopy(dict(package.get("compiler_handoff") or {}))
    raw_units = [
        deepcopy(dict(unit))
        for unit in list(package.get("source_units") or [])
        if isinstance(unit, dict)
    ]
    canonical_text = str(handoff.get("mega_text") or "").strip()
    if not canonical_text:
        canonical_text = "\n\n".join(
            str(unit.get("raw_text") or unit.get("text") or "").strip()
            for unit in raw_units
            if str(unit.get("raw_text") or unit.get("text") or "").strip()
        ).strip()
    if not canonical_text:
        return package
    current_uri = str(
        source_request.get("source_uri")
        or dict(package.get("provenance") or {}).get("source_uri")
        or package.get("source_uri")
        or ""
    ).strip()
    if _is_http_url(current_uri) or current_uri.startswith("urn:agvm:manual-source:sha256:"):
        canonical_uri = current_uri
    else:
        canonical_uri = f"urn:agvm:manual-source:sha256:{hashlib.sha256(canonical_text.encode('utf-8')).hexdigest()}"
    if not canonical_uri:
        return package
    source_trust = str(
        source_request.get("source_trust")
        or dict(package.get("provenance") or {}).get("source_trust")
        or dict(dict(source_request.get("options") or {}).get("effective") or {}).get("source_trust")
        or dict(dict(source_request.get("options") or {}).get("requested") or {}).get("source_trust")
        or ""
    ).strip()
    if _is_http_url(canonical_uri) and source_trust in {"", "unknown"}:
        source_trust = "public_web"
    elif canonical_uri.startswith("urn:agvm:manual-source:sha256:") and source_trust in {
        "",
        "unknown",
        "public_web",
        "verified_public_source",
    }:
        # A content-derived manual URI is an explicit operator assertion. Keep
        # the investigation vocabulary at the source boundary, but never let
        # its ``unknown`` placeholder escape into persisted Graph nodes where
        # it is not a valid SourceTrust value.
        source_trust = "user_asserted"
    package["source_uri"] = canonical_uri
    source_request["source_uri"] = canonical_uri
    if source_trust:
        source_request["source_trust"] = source_trust
        request_options = deepcopy(dict(source_request.get("options") or {}))
        for option_key in ("requested", "effective"):
            option_bucket = deepcopy(dict(request_options.get(option_key) or {}))
            if str(option_bucket.get("source_trust") or "").strip() in {"", "unknown"}:
                option_bucket["source_trust"] = source_trust
            request_options[option_key] = option_bucket
        source_request["options"] = request_options
    package["source_request"] = source_request
    provenance = deepcopy(dict(package.get("provenance") or {}))
    provenance["source_uri"] = canonical_uri
    if source_trust and str(provenance.get("source_trust") or "").strip() in {"", "unknown"}:
        provenance["source_trust"] = source_trust
    package["provenance"] = provenance
    normalized_units: list[dict[str, Any]] = []
    for unit in raw_units:
        unit_text = str(unit.get("raw_text") or unit.get("text") or "")
        unit_digest = (
            f"sha256:{hashlib.sha256(unit_text.encode('utf-8')).hexdigest()}"
            if unit_text.strip()
            else ""
        )
        unit_uri = str(unit.get("source_uri") or "").strip()
        effective_unit_uri = unit_uri if _is_http_url(unit_uri) else canonical_uri
        if effective_unit_uri and unit_uri != effective_unit_uri:
            unit["source_uri"] = effective_unit_uri
        if unit_digest:
            # The durable deterministic-source authority validates every
            # source unit against its exact bytes. Some public-core intake
            # packages expose only ``raw_text``; bind the digest while
            # normalizing provenance so preview and Apply share one proof.
            unit["content_digest"] = unit_digest
        if source_trust and str(unit.get("source_trust") or "").strip() in {"", "unknown"}:
            unit["source_trust"] = source_trust
        unit_provenance = deepcopy(dict(unit.get("provenance") or {}))
        if effective_unit_uri:
            unit_provenance["source_uri"] = effective_unit_uri
        if unit_digest:
            unit_provenance["hash"] = unit_digest
        if source_trust and str(unit_provenance.get("source_trust") or "").strip() in {"", "unknown"}:
            unit_provenance["source_trust"] = source_trust
        unit["provenance"] = unit_provenance
        proof = deepcopy(dict(unit.get("acquisition_proof") or {}))
        if effective_unit_uri:
            proof["source_uri"] = effective_unit_uri
        if unit_digest:
            proof["content_digest"] = unit_digest
        unit["acquisition_proof"] = proof
        normalized_units.append(unit)
    package["source_units"] = normalized_units
    normalized_units_by_id = {
        str(unit.get("unit_id") or "").strip(): unit
        for unit in normalized_units
        if str(unit.get("unit_id") or "").strip()
    }
    if handoff:
        normalized_sections: list[dict[str, Any]] = []
        for section in list(handoff.get("structured_sections") or []):
            if not isinstance(section, dict):
                continue
            section_copy = deepcopy(dict(section))
            section_uri = str(section_copy.get("source_uri") or "").strip()
            if not section_uri or not _is_http_url(section_uri):
                section_copy["source_uri"] = canonical_uri
            section_text = str(section_copy.get("text") or section_copy.get("raw_text") or "")
            if section_text.strip():
                section_copy["content_digest"] = (
                    f"sha256:{hashlib.sha256(section_text.encode('utf-8')).hexdigest()}"
                )
            if source_trust and str(section_copy.get("source_trust") or "").strip() in {"", "unknown"}:
                section_copy["source_trust"] = source_trust
            normalized_sections.append(section_copy)
        if normalized_sections:
            handoff["structured_sections"] = normalized_sections
        provenance_map = deepcopy(dict(handoff.get("provenance_map") or {}))
        for key, value in list(provenance_map.items()):
            if not isinstance(value, dict):
                continue
            item = deepcopy(dict(value))
            item_uri = str(item.get("source_uri") or "").strip()
            if not item_uri or not _is_http_url(item_uri):
                item["source_uri"] = canonical_uri
            matching_unit = normalized_units_by_id.get(str(key).strip())
            if matching_unit and str(matching_unit.get("content_digest") or "").strip():
                item["hash"] = str(matching_unit["content_digest"])
            if source_trust and str(item.get("source_trust") or "").strip() in {"", "unknown"}:
                item["source_trust"] = source_trust
            provenance_map[key] = item
        if provenance_map:
            handoff["provenance_map"] = provenance_map
        package["compiler_handoff"] = handoff
    return package


def _deterministic_public_text_source_package(
    payload: McpGrowSourceRequest,
    *,
    brain_id: str,
) -> dict[str, Any]:
    text = str(payload.raw_input or "").strip()
    source_uri = str(payload.source_uri or "").strip()
    source_label = str(payload.source_label or source_uri).strip()
    source_trust = _deterministic_public_text_source_trust(payload)
    investigation_id = f"mcp-grow-text-{uuid.uuid4()}"
    observed_at = datetime.now(timezone.utc).isoformat()
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    digest_ref = f"sha256:{digest}"
    unit_id = f"src_text_{digest[:16]}"
    unit = {
        "unit_id": unit_id,
        "kind": "manual_block",
        "title": source_label,
        "source_uri": source_uri,
        "source_type": "public_source_text",
        "raw_text": text,
        "clean_text": text,
        "summary": text[:280],
        "language": "unknown",
        "char_count": len(text),
        "token_estimate": max(1, (len(text) + 3) // 4),
        "confidence": 1.0,
        "source_unit_role": "primary_evidence",
        "promotion_role": "primary_evidence",
        "fact_eligible": True,
        "supporting_evidence_eligible": True,
        "content_digest": digest_ref,
        "observed_at": observed_at,
        "acquired_at": observed_at,
        "acquisition_method": "operator_bound_public_text",
        "acquisition_proof": {
            "verified": True,
            "kind": "operator_bound_public_text",
            "source_uri": source_uri,
            "content_digest": digest_ref,
            "acquired_at": observed_at,
            "method": "operator_bound_public_text",
        },
        "provenance": {
            "source_label": source_label,
            "source_type": "public_source_text",
            "source_uri": source_uri,
            "source_trust": source_trust,
            "hash": digest_ref,
            "retrieved_at": observed_at,
            "observed_at": observed_at,
        },
        "extraction_trace": {
            "stage": "source_text_binding",
            "method": "operator_bound_public_text",
            "provider_executed": False,
        },
    }
    return {
        "schema_version": "agvm.source_investigation.v1",
        "brain_id": brain_id,
        "investigation_id": investigation_id,
        "created_at": observed_at,
        "status": "preview_ready",
        "source_label": source_label,
        "source_uri": source_uri,
        "source_type": "public_source_text",
        "source_request": {
            "brain_id": brain_id,
            "input_kind": "manual_text",
            "source_label": source_label,
            "source_uri": source_uri,
            "source_trust": source_trust,
            "source_sha256": digest_ref,
            "observed_at": observed_at,
            "options": {
                "requested": {"source_trust": source_trust},
                "effective": {"source_trust": source_trust},
            },
        },
        "source_detection": {
            "schema_version": "agvm.source_detection.v1",
            "source_kind": "manual_text",
            "confidence": 1.0,
            "signals": ["explicit_manual_text", "canonical_http_source_uri"],
            "urls": [source_uri],
            "url_count": 1,
            "non_url_text_char_count": len(text),
        },
        "source_units": [unit],
        "source_unit_formation": {
            "schema_version": "agvm.source_unit_formation.v1",
            "status": "pass",
            "unit_ids": [unit_id],
            "source_unit_count": 1,
            "raw_text_preserved": True,
        },
        "compiler_handoff": {
            "schema_version": "agvm.compiler_handoff.v1",
            "handoff_version": "agvm.compiler_handoff.v1",
            "source_summary": source_label,
            "mega_text": text,
            "structured_sections": [
                {
                    "section_id": unit_id,
                    "unit_id": unit_id,
                    "title": source_label,
                    "kind": "manual_block",
                    "text": text,
                    "source_uri": source_uri,
                    "source_type": "public_source_text",
                    "source_trust": source_trust,
                    "content_digest": digest_ref,
                    "observed_at": observed_at,
                    "fact_eligible": True,
                }
            ],
            "provenance_map": {
                unit_id: {
                    "source_uri": source_uri,
                    "source_type": "public_source_text",
                    "source_trust": source_trust,
                    "hash": digest_ref,
                    "observed_at": observed_at,
                }
            },
            "source_purpose": "reference_library",
            "recommended_input_mode": "document",
            "recommended_learning_mode": "strict_review",
            "must_preserve_raw_text": True,
            "preview_eligible": True,
            "preview_blocked_reasons": [],
        },
        "provenance": {
            "source_uri": source_uri,
            "source_type": "public_source_text",
            "source_trust": source_trust,
            "source_sha256": digest_ref,
            "observed_at": observed_at,
            "mutation_policy": "preview_only_no_graph_mutation",
        },
        "runtime": {
            "kind": "deterministic_public_text",
            "provider_executed": False,
        },
    }


def _grow_deterministic_public_text_preview(
    tool_name: str,
    payload: McpGrowSourceRequest,
    *,
    brain_record: dict[str, Any],
) -> McpGrowToolExecutionResponse:
    started = time.perf_counter()
    brain_id = _brain_record_id(brain_record)
    source_package = _deterministic_public_text_source_package(payload, brain_id=brain_id)
    investigation_id = str(source_package["investigation_id"])
    with use_runtime_brain(brain_record):
        bootstrap_runtime_store()
        graph_snapshot = fetch_graph_snapshot()
        preview_brain_revision = maintenance_graph_revision(graph_snapshot)
        preview_bundle = _core_upload_fallback_preview_bundle(source_package, graph=graph_snapshot)
        selected_preview_ids = _selected_preview_ids(preview_bundle, [])
        attestation = _core_upload_deterministic_source_attestation(source_package)
        compiler_handoff_proof = build_source_compiler_handoff_proof(source_package, preview_bundle)
        source_package["compiler_handoff_proof"] = compiler_handoff_proof
        source_formation_contract = {
            "schema_version": "agvm.core_source_formation_contract.v3",
            "mode": "operator_bound_public_text_preview",
            "state": "preview_ready",
            "mutates_memory": False,
            "investigation_id": investigation_id,
            "authority": {
                "kind": "deterministic_public_text",
                "schema_version": attestation["schema_version"],
                "provider_required": False,
                "semantic_claims_allowed": False,
                "source_bound_claims_allowed": True,
                "preview_scope": list(attestation["preview_scope"]),
            },
            "apply_contract": {
                "preview_required": True,
                "explicit_confirm_apply_required": True,
                "explicit_selection_required": True,
                "apply_without_preview_allowed": False,
                "can_apply_now": bool(selected_preview_ids),
                "blocked_reasons": [] if selected_preview_ids else ["preview_bundle_missing"],
                "selected_preview_ids": selected_preview_ids,
            },
        }
        persisted_preview = store_local_grow_v2_preview(
            brain_id=brain_id,
            investigation_id=investigation_id,
            tool_name=tool_name,
            source_investigation=source_package,
            source_formation_contract=source_formation_contract,
            preview_bundle=preview_bundle,
            ai_execution_attestation=None,
            deterministic_source_attestation=attestation,
            investigation_session={
                "schema_version": "agvm.investigation_session.v3",
                "status": "preview_ready",
                "authority_kind": "deterministic_public_text",
                "provider_executed": False,
                "preview_node_count": len(selected_preview_ids),
            },
            expected_brain_revision=preview_brain_revision,
        )
    return McpGrowToolExecutionResponse(
        schema_version="agvm.mcp_grow_tool_output.v1",
        brain_id=brain_id,
        investigation_id=investigation_id,
        tool_name=tool_name,
        status="preview_ready",
        can_apply_now=bool(selected_preview_ids),
        selected_preview_ids=selected_preview_ids,
        source_investigation=source_package,
        source_formation_contract=source_formation_contract,
        memory_operation_lifecycle_contract={
            "schema_version": "agvm.memory_operation_lifecycle_contract.v3",
            "operation": "grow",
            "phase": "preview_ready",
            "next_action": "select exact server preview IDs and Apply with confirm_apply=true",
        },
        preview_bundle=preview_bundle,
        compiler_handoff_proof=compiler_handoff_proof,
        investigation_session={
            "schema_version": "agvm.investigation_session.v3",
            "status": "preview_ready",
            "authority_kind": "deterministic_public_text",
            "provider_executed": False,
            "preview_fingerprint": persisted_preview.get("preview_fingerprint"),
            "authority_fingerprint": persisted_preview.get("attestation_fingerprint"),
        },
        investigation={"brain_revision": preview_brain_revision},
        completeness={
            "preview_generated": True,
            "provider_independent_preview": True,
            "preview_authority": "deterministic_public_text",
            "preview_node_count": len(selected_preview_ids),
            "apply_ready": bool(selected_preview_ids),
        },
        mcp_latency_profile={"elapsed_ms": int((time.perf_counter() - started) * 1000)},
        budget={"credits_required": 0, "runtime": "local_core"},
        next_action="select exact server preview IDs and Apply with confirm_apply=true",
    )


def _grow_source_bound_storage_only_contract(
    investigation_id: str,
    *,
    state: str,
    selected_preview_ids: list[str] | None = None,
    attestation: dict[str, Any] | None = None,
    blocked_reason: str | None = None,
) -> dict[str, Any]:
    selected_ids = list(selected_preview_ids or [])
    authority = {
        "kind": "explicit_source_bound_storage_only",
        "provider_required": False,
        "semantic_claims_allowed": False,
        "source_bound_claims_allowed": True,
        "semantic_grow_executed": False,
        "source_storage_mode": "source_bound_only",
        "semantic_fallback_allowed": False,
        "meaning_conflict_merge_delete_authority": "provider_or_human_required",
    }
    if attestation:
        authority.update(
            {
                "schema_version": attestation.get("schema_version"),
                "source_authority_kind": attestation.get("authority_kind"),
                "preview_scope": list(attestation.get("preview_scope") or []),
            }
        )
    blocked_reasons = [blocked_reason] if blocked_reason else []
    return {
        "schema_version": "agvm.core_source_formation_contract.v3",
        "mode": "source_bound_document_anchor_preview",
        "state": state,
        "mutates_memory": False,
        "investigation_id": investigation_id,
        "source_storage_mode": "source_bound_only",
        "provider_required": False,
        "semantic_claims_allowed": False,
        "source_bound_claims_allowed": True,
        "semantic_grow_executed": False,
        "semantic_fallback_allowed": False,
        "blocked_reason": blocked_reason,
        "authority": authority,
        "apply_contract": {
            "preview_required": True,
            "explicit_confirm_apply_required": True,
            "explicit_selection_required": True,
            "apply_without_preview_allowed": False,
            "can_apply_now": bool(selected_ids) and not blocked_reason,
            "blocked_reasons": blocked_reasons,
            "selected_preview_ids": selected_ids if not blocked_reason else [],
        },
    }


def _grow_source_bound_storage_only_package(source_package: dict[str, Any]) -> dict[str, Any]:
    package = deepcopy(dict(source_package or {}))
    legacy_questions = [
        deepcopy(dict(question))
        for question in [
            *list(package.get("clarification_questions") or []),
            *list(package.get("open_questions") or []),
            *list(dict(package.get("guided_grow") or {}).get("pending_questions") or []),
        ]
        if isinstance(question, dict)
    ]
    if legacy_questions:
        existing_non_blocking = [
            deepcopy(dict(question))
            for question in list(package.get("non_blocking_clarification_questions") or [])
            if isinstance(question, dict)
        ]
        package["non_blocking_clarification_questions"] = [
            *existing_non_blocking,
            *legacy_questions,
        ]
    package["clarification_questions"] = []
    package["open_questions"] = []
    guided_grow = deepcopy(dict(package.get("guided_grow") or {}))
    if guided_grow:
        guided_grow["pending_questions"] = []
        package["guided_grow"] = guided_grow
    storage_contract = {
        "schema_version": "agvm.source_bound_storage_only.v1",
        "mode": "source_bound_only",
        "semantic_grow_executed": False,
        "semantic_fallback_allowed": False,
        "provider_required": False,
        "purpose": [
            "durable_full_text_anchor",
            "document_chunks_with_coordinates",
            "rich_section_memory_storage",
            "atomic_claim_audit_dedup_conflict_citation_substrate",
        ],
    }
    package["source_storage_contract"] = storage_contract
    return package


def _grow_source_bound_storage_only_preview(
    tool_name: str,
    payload: McpGrowSourceRequest,
    *,
    brain_record: dict[str, Any],
) -> McpGrowToolExecutionResponse:
    started = time.perf_counter()
    brain_id = _brain_record_id(brain_record)
    if not payload.run_preview:
        reason = "source_bound_only_requires_preview_apply_contract"
        return _grow_blocked(
            tool_name,
            brain_id,
            reason,
            started,
            source_formation_contract=_grow_source_bound_storage_only_contract(
                str(payload.investigation_id or ""),
                state="blocked",
                blocked_reason=reason,
            ),
        )
    if _grow_source_bound_storage_only_semantic_conflict(payload):
        reason = "source_bound_only_conflicts_with_semantic_grow"
        return _grow_blocked(
            tool_name,
            brain_id,
            reason,
            started,
            source_formation_contract=_grow_source_bound_storage_only_contract(
                str(payload.investigation_id or ""),
                state="blocked",
                blocked_reason=reason,
            ),
        )
    if payload.investigation_id or payload.resume_token:
        reason = "source_bound_only_resume_not_supported_use_apply"
        return _grow_blocked(
            tool_name,
            brain_id,
            reason,
            started,
            source_formation_contract=_grow_source_bound_storage_only_contract(
                str(payload.investigation_id or ""),
                state="blocked",
                blocked_reason=reason,
            ),
        )
    try:
        source_package = _grow_source_package(payload, brain_id)
    except TrustedSourcePackageError as exc:
        reason = str(exc.code or "source_bound_storage_source_acquisition_failed")
        return _grow_blocked(
            tool_name,
            brain_id,
            reason,
            started,
            source_investigation=exc.source_package,
            source_formation_contract=_grow_source_bound_storage_only_contract(
                str(dict(exc.source_package or {}).get("investigation_id") or ""),
                state="blocked",
                blocked_reason=reason,
            ),
        )
    investigation_id = str(source_package.get("investigation_id") or f"mcp-grow-source-bound-{uuid.uuid4()}")
    source_package["investigation_id"] = investigation_id
    source_package = _grow_v3_source_without_legacy_semantic_questions(source_package)
    source_package = _grow_source_bound_storage_only_package(source_package)
    source_package = _normalize_manual_source_package_provenance(source_package)
    if not _grow_source_package_has_source_material(source_package):
        reason = "source_bound_storage_source_material_required"
        return _grow_blocked(
            tool_name,
            brain_id,
            reason,
            started,
            source_investigation=source_package,
            source_formation_contract=_grow_source_bound_storage_only_contract(
                investigation_id,
                state="blocked",
                blocked_reason=reason,
            ),
        )
    source_package["status"] = "preview_ready"
    try:
        with use_runtime_brain(brain_record):
            bootstrap_runtime_store()
            graph_snapshot = fetch_graph_snapshot()
            preview_brain_revision = maintenance_graph_revision(graph_snapshot)
            preview_bundle = _core_upload_fallback_preview_bundle(source_package, graph=graph_snapshot)
            selected_preview_ids = _selected_preview_ids(preview_bundle, [])
            attestation = _core_upload_deterministic_source_attestation(source_package)
            compiler_handoff_proof = build_source_compiler_handoff_proof(source_package, preview_bundle)
            source_package["compiler_handoff_proof"] = compiler_handoff_proof
            source_formation_contract = _grow_source_bound_storage_only_contract(
                investigation_id,
                state="preview_ready",
                selected_preview_ids=selected_preview_ids,
                attestation=attestation,
            )
            persisted_preview = store_local_grow_v2_preview(
                brain_id=brain_id,
                investigation_id=investigation_id,
                tool_name=tool_name,
                source_investigation=source_package,
                source_formation_contract=source_formation_contract,
                preview_bundle=preview_bundle,
                ai_execution_attestation=None,
                deterministic_source_attestation=attestation,
                investigation_session={
                    "schema_version": "agvm.investigation_session.v3",
                    "status": "preview_ready",
                    "authority_kind": "explicit_source_bound_storage_only",
                    "provider_executed": False,
                    "semantic_grow_executed": False,
                    "source_storage_mode": "source_bound_only",
                    "preview_node_count": len(selected_preview_ids),
                },
                expected_brain_revision=preview_brain_revision,
            )
    except (GrowPreviewBindingStoreError, AiModuleContractError, sqlite3.DatabaseError, RuntimeError, OSError, ValueError) as exc:
        reason = str(getattr(exc, "code", "") or exc or "source_bound_storage_preview_failed")
        return _grow_blocked(
            tool_name,
            brain_id,
            reason,
            started,
            source_investigation=source_package,
            source_formation_contract=_grow_source_bound_storage_only_contract(
                investigation_id,
                state="blocked",
                blocked_reason=reason,
            ),
        )
    return McpGrowToolExecutionResponse(
        schema_version="agvm.mcp_grow_tool_output.v1",
        brain_id=brain_id,
        investigation_id=investigation_id,
        tool_name=tool_name,
        status="preview_ready",
        can_apply_now=bool(selected_preview_ids),
        selected_preview_ids=selected_preview_ids,
        source_investigation=source_package,
        source_formation_contract=source_formation_contract,
        memory_operation_lifecycle_contract={
            "schema_version": "agvm.memory_operation_lifecycle_contract.v3",
            "operation": "grow",
            "phase": "preview_ready",
            "semantic_grow_executed": False,
            "next_action": "select exact server preview IDs and Apply with confirm_apply=true",
        },
        preview_bundle=preview_bundle,
        maintenance_feedback_packets=[],
        clarification_questions=[],
        compiler_handoff_proof=compiler_handoff_proof,
        investigation_session={
            "schema_version": "agvm.investigation_session.v3",
            "status": "preview_ready",
            "authority_kind": "explicit_source_bound_storage_only",
            "provider_executed": False,
            "semantic_grow_executed": False,
            "source_storage_mode": "source_bound_only",
            "preview_fingerprint": persisted_preview.get("preview_fingerprint"),
            "authority_fingerprint": persisted_preview.get("attestation_fingerprint"),
        },
        investigation={"brain_revision": preview_brain_revision},
        completeness={
            "preview_generated": True,
            "provider_independent_preview": True,
            "preview_authority": "explicit_source_bound_storage_only",
            "semantic_claims_emitted": False,
            "semantic_grow_executed": False,
            "source_storage_mode": "source_bound_only",
            "preview_node_count": len(selected_preview_ids),
            "source_unit_count": len(
                [unit for unit in list(source_package.get("source_units") or []) if isinstance(unit, dict)]
            ),
            "apply_ready": bool(selected_preview_ids),
        },
        mcp_latency_profile={"elapsed_ms": int((time.perf_counter() - started) * 1000)},
        budget={"credits_required": 0, "runtime": "local_core"},
        next_action="select exact server preview IDs and Apply with confirm_apply=true",
    )


def _core_upload_file_hash_from_source_package(source_package: dict[str, Any]) -> str:
    source_request = dict(source_package.get("source_request") or {})
    candidates = [
        source_request.get("file_hash"),
        source_request.get("source_sha256"),
        dict(source_package.get("provenance") or {}).get("file_hash"),
        source_package.get("source_sha256"),
    ]
    for candidate in candidates:
        digest = str(candidate or "").strip()
        if not digest:
            continue
        return digest.removeprefix("sha256:")
    canonical_text, _sections = _document_canonical_text_and_sections(source_package)
    return hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()


def _core_upload_file_size_from_source_package(source_package: dict[str, Any]) -> int:
    source_request = dict(source_package.get("source_request") or {})
    for key in ("file_size_bytes", "byte_count", "content_length"):
        try:
            value = int(source_request.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return 0


def _core_upload_file_name_from_source_package(
    source_package: dict[str, Any],
    fallback: str | None = None,
) -> str:
    source_request = dict(source_package.get("source_request") or {})
    return (
        str(source_request.get("file_name") or source_package.get("source_label") or fallback or "")
        .strip()
        or "uploaded_source"
    )


def _core_upload_content_type_from_source_package(
    source_package: dict[str, Any],
    fallback: str | None = None,
) -> str | None:
    source_request = dict(source_package.get("source_request") or {})
    content_type = str(source_request.get("content_type") or fallback or "").strip()
    return content_type or None


def _core_upload_response_payload(
    *,
    brain_id: str,
    storage_path: str | None = None,
    source_package: dict[str, Any],
    file_name: str,
    content_type: str | None,
    file_bytes: bytes | None,
    grow_response: McpGrowToolExecutionResponse,
    blocker: dict[str, Any] | None = None,
    resumed_without_file: bool = False,
) -> dict[str, Any]:
    response_payload = grow_response.model_dump(mode="python", exclude_none=True)
    document_id = str(source_package.get("investigation_id") or grow_response.investigation_id or "")
    file_sha256 = (
        hashlib.sha256(file_bytes).hexdigest()
        if file_bytes is not None
        else _core_upload_file_hash_from_source_package(source_package)
    )
    byte_count = (
        len(file_bytes)
        if file_bytes is not None
        else _core_upload_file_size_from_source_package(source_package)
    )
    document_receipt = _document_receipt_v0(
        brain_id=brain_id,
        source_package=source_package,
        file_name=file_name,
        content_type=content_type,
        file_sha256=file_sha256,
        byte_count=byte_count,
        grow_response=grow_response,
    )
    hydration_page = _hydration_page_v0(source_package, document_id=document_id)
    document_ref = dict(document_receipt.get("document_ref") or {})
    remember_preview_document(
        document_receipt=document_receipt,
        document_ref=document_ref,
        hydration_page=hydration_page,
        brain_id=brain_id,
        storage_path=storage_path,
    )
    response_payload["document_receipt_v0"] = document_receipt
    response_payload["document_ref_v0"] = document_ref
    response_payload["hydration_page_v0"] = hydration_page
    response_payload.update(
        {
            "document_id": document_receipt.get("document_id"),
            "document_ref_id": document_receipt.get("document_ref_id"),
            "document_anchor_id": document_receipt.get("document_anchor_id"),
            "chunk_ids": list(document_receipt.get("chunk_ids") or []),
            "source_sha256": document_receipt.get("source_sha256"),
            "canonical_text_sha256": document_receipt.get("canonical_text_sha256"),
            "content_hash": document_ref.get("content_hash"),
            "canonical_url": document_ref.get("canonical_url"),
            "hydration_result_ref": hydration_page.get("hydration_result_ref"),
            "open_ref": document_ref.get("canonical_ref"),
            "open_url": document_ref.get("canonical_url"),
            "original_binary_retained": document_receipt.get("original_binary_retained"),
            "mutates_before_apply": False,
        }
    )
    response_payload["document_triage_contract_v0"] = _document_triage_contract_v0(document_ref)
    response_payload["document_events_v0"] = _document_events_v0(document_ref, hydration_page)
    response_payload["route_proof"] = {
        "endpoint": "/mcp/grow-source-upload",
        "aliases": [
            "/mcp/grow-source-upload",
            "/memory/mcp/grow-source-upload",
            "/source-investigation/upload",
            "/memory/source-investigation/upload",
        ],
        "multipart": True,
        "accepted_file_kinds": ["pdf", "docx"],
        "accepted_content_types": [
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ],
        "rejected_file_kinds": ["doc", "zip"],
        "actual_runtime": "core_api_app",
        "mutates_graph_before_apply": False,
        "document_anchor_id": document_receipt.get("document_anchor_id"),
        "chunk_ids": list(document_receipt.get("chunk_ids") or []),
        "source_sha256": document_receipt.get("source_sha256"),
        "canonical_text_sha256": document_receipt.get("canonical_text_sha256"),
    }
    if resumed_without_file:
        response_payload["route_proof"]["resumed_without_file"] = True
        response_payload["route_proof"]["resume_contract"] = {
            "requires_file": False,
            "requires_raw_input": False,
            "required_fields": [
                "investigation_id",
                "resume_token",
                "clarification_answers",
            ],
            "accepted_answer_fields": [
                "clarification_answers_json",
                "clarification_answers",
            ],
        }
    if blocker:
        response_payload["route_proof"]["blocker"] = blocker
    return response_payload


def _grow_source_upload_from_bytes(
    *,
    file_bytes: bytes,
    file_name: str,
    content_type: str | None,
    brain_id: str | None,
    source_label: str | None,
    source_uri: str | None,
    user_instruction: str | None,
    input_kind: str,
    treat_as: str,
    analyze_images: str,
    run_preview: bool,
    question_limit: int,
    max_pages: int,
    max_ocr_pages: int,
    max_images: int,
    max_online_queries: int,
    max_units: int,
    max_total_chars: int,
    semantic_preview: bool = False,
    source_storage_mode: str = "auto",
) -> dict[str, Any]:
    detected_kind = _core_upload_source_kind(file_name, content_type, file_bytes, input_kind)
    if detected_kind not in {"pdf", "docx"}:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "unsupported_upload_type",
                "message": "Only PDF and DOCX uploads are accepted by this Core document route.",
                "detected_kind": detected_kind,
            },
        )
    resolved_source_storage_mode = _core_upload_source_storage_mode(source_storage_mode)
    options = {
        "treat_as": treat_as,
        "analyze_images": analyze_images,
        "use_online_enrichment": False,
        "pause_on_questions": bool(semantic_preview),
        "clarification_default_policy": "pause_when_unanswered" if semantic_preview else "apply_defaults",
        "semantic_preview": bool(semantic_preview),
        "source_storage_mode": resolved_source_storage_mode,
        "question_limit": _core_upload_int(question_limit, 12),
        "max_pages": _core_upload_int(max_pages, 20),
        "max_ocr_pages": _core_upload_int(max_ocr_pages, 8),
        "max_images": _core_upload_int(max_images, 12),
        "max_online_queries": _core_upload_int(max_online_queries, 0),
        "max_units": _core_upload_int(max_units, 12),
        "max_total_chars": _core_upload_int(max_total_chars, 120000),
    }
    try:
        source_package = build_file_source_investigation_package(
            file_bytes,
            file_name=file_name,
            content_type=content_type,
            source_label=source_label,
            source_uri=source_uri,
            user_instruction=user_instruction,
            input_kind=detected_kind,
            options=options,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "source_upload_invalid") from exc
    source_package = _stamp_core_upload_trusted_runtime_contract(
        source_package,
        brain_id=str(brain_id or current_brain_id() or "data"),
    )
    try:
        brain_record = _resolve_bootstrap_ready_brain_record(brain_id)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        if int(exc.status_code) != 409 or detail.get("code") != "brain_bootstrap_required":
            raise
        fallback_brain_id = str(detail.get("brain_id") or brain_id or current_brain_id() or "data")
        source_package = _stamp_core_upload_trusted_runtime_contract(source_package, brain_id=fallback_brain_id)
        investigation_id = str(source_package.get("investigation_id") or f"doc-upload-{uuid.uuid4()}")
        source_package["investigation_id"] = investigation_id
        fallback_bundle = _core_upload_fallback_preview_bundle(source_package)
        grow_response = McpGrowToolExecutionResponse(
            schema_version="agvm.mcp_grow_tool_output.v1",
            brain_id=fallback_brain_id,
            investigation_id=investigation_id,
            tool_name="grow_source_upload",
            status="blocked",
            source_investigation=source_package,
            source_formation_contract={
                "schema_version": "agvm.core_source_formation_contract.v3",
                "mode": "document_upload_fallback",
                "state": "blocked",
                "mutates_memory": False,
                "investigation_id": investigation_id,
                "apply_contract": {
                    "preview_required": True,
                    "explicit_confirm_apply_required": True,
                    "apply_without_preview_allowed": False,
                    "can_apply_now": False,
                    "blocked_reasons": ["brain_bootstrap_required"],
                    "selected_preview_ids": [],
                },
            },
            preview_bundle=fallback_bundle,
            completeness={
                "preview_generated": True,
                "fallback_preview": True,
                "blocked": True,
                "reason": "brain_bootstrap_required",
            },
            budget={"credits_required": 0, "runtime": "local_core"},
            next_action="bootstrap or select a safe MCP brain before apply",
        )
        return _core_upload_response_payload(
            brain_id=fallback_brain_id,
            storage_path=None,
            source_package=source_package,
            file_name=file_name,
            content_type=content_type,
            file_bytes=file_bytes,
            grow_response=grow_response,
            blocker=detail,
        )
    resolved_brain_id = _brain_record_id(brain_record)
    source_package = _stamp_core_upload_trusted_runtime_contract(
        source_package,
        brain_id=resolved_brain_id,
    )
    if _core_upload_bool(run_preview) and not _core_upload_bool(semantic_preview):
        explicit_source_bound_storage = resolved_source_storage_mode == "source_bound_only"
        preview_authority_kind = (
            "explicit_source_bound_storage_only"
            if explicit_source_bound_storage
            else "deterministic_document"
        )
        investigation_id = str(source_package.get("investigation_id") or f"doc-upload-{uuid.uuid4()}")
        source_package["investigation_id"] = investigation_id
        if explicit_source_bound_storage:
            source_package = _grow_source_bound_storage_only_package(source_package)
            source_package["status"] = "preview_ready"
        with use_runtime_brain(brain_record):
            bootstrap_runtime_store()
            graph_snapshot = fetch_graph_snapshot()
            preview_brain_revision = maintenance_graph_revision(graph_snapshot)
            fallback_bundle = _core_upload_fallback_preview_bundle(source_package, graph=graph_snapshot)
            selected_preview_ids = _selected_preview_ids(fallback_bundle, [])
            source_authority_attestation = _core_upload_deterministic_source_attestation(source_package)
            source_formation_contract = {
                "schema_version": "agvm.core_source_formation_contract.v3",
                "mode": (
                    "source_bound_document_anchor_preview"
                    if explicit_source_bound_storage
                    else "parser_backed_document_preview"
                ),
                "state": "preview_ready",
                "mutates_memory": False,
                "investigation_id": investigation_id,
                "source_storage_mode": resolved_source_storage_mode,
                "semantic_grow_executed": False,
                "semantic_fallback_allowed": False,
                "authority": {
                    "kind": preview_authority_kind,
                    "schema_version": source_authority_attestation["schema_version"],
                    "provider_required": False,
                    "semantic_claims_allowed": False,
                    "source_bound_claims_allowed": True,
                    "semantic_grow_executed": False,
                    "source_storage_mode": resolved_source_storage_mode,
                    "preview_scope": list(source_authority_attestation["preview_scope"]),
                },
                "apply_contract": {
                    "preview_required": True,
                    "explicit_confirm_apply_required": True,
                    "apply_without_preview_allowed": False,
                    "can_apply_now": bool(selected_preview_ids),
                    "blocked_reasons": [] if selected_preview_ids else ["preview_bundle_missing"],
                    "selected_preview_ids": selected_preview_ids,
                },
            }
            persisted_preview = store_local_grow_v2_preview(
                brain_id=resolved_brain_id,
                investigation_id=investigation_id,
                tool_name="grow_source_upload",
                source_investigation=source_package,
                source_formation_contract=source_formation_contract,
                preview_bundle=fallback_bundle,
                ai_execution_attestation=None,
                deterministic_source_attestation=source_authority_attestation,
                investigation_session={
                    "schema_version": "agvm.investigation_session.v3",
                    "status": "preview_ready",
                    "authority_kind": preview_authority_kind,
                    "provider_executed": False,
                    "semantic_grow_executed": False,
                    "source_storage_mode": resolved_source_storage_mode,
                    "preview_node_count": len(selected_preview_ids),
                },
                expected_brain_revision=preview_brain_revision,
            )
        grow_response = McpGrowToolExecutionResponse(
            schema_version="agvm.mcp_grow_tool_output.v1",
            brain_id=resolved_brain_id,
            investigation_id=investigation_id,
            tool_name="grow_source_upload",
            status="preview_ready",
            can_apply_now=bool(selected_preview_ids),
            selected_preview_ids=selected_preview_ids,
            source_investigation=source_package,
            source_formation_contract=source_formation_contract,
            preview_bundle=fallback_bundle,
            completeness={
                "preview_generated": True,
                "provider_independent_preview": True,
                "preview_authority": preview_authority_kind,
                "semantic_grow_executed": False,
                "source_storage_mode": resolved_source_storage_mode,
                "preview_node_count": len(selected_preview_ids),
                "apply_ready": bool(selected_preview_ids),
            },
            investigation_session={
                "schema_version": "agvm.investigation_session.v3",
                "status": "preview_ready",
                "authority_kind": preview_authority_kind,
                "provider_executed": False,
                "semantic_grow_executed": False,
                "source_storage_mode": resolved_source_storage_mode,
                "preview_fingerprint": persisted_preview.get("preview_fingerprint"),
                "authority_fingerprint": persisted_preview.get("attestation_fingerprint"),
            },
            investigation={"brain_revision": preview_brain_revision},
            budget={"credits_required": 0, "runtime": "local_core"},
            next_action=(
                "select the server-issued document anchor/chunk preview IDs and call "
                "grow_source_apply with confirm_apply=true"
            ),
        )
        return _core_upload_response_payload(
            brain_id=resolved_brain_id,
            storage_path=str(brain_record.get("storage_path") or "") or None,
            source_package=source_package,
            file_name=file_name,
            content_type=content_type,
            file_bytes=file_bytes,
            grow_response=grow_response,
        )
    payload = McpGrowSourceRequest(
        brain_id=resolved_brain_id,
        raw_input=str(dict(source_package.get("compiler_handoff") or {}).get("mega_text") or ""),
        source_label=source_label or file_name,
        source_uri=source_uri,
        user_instruction=user_instruction,
        input_kind=detected_kind,
        trusted_source_investigation=source_package,
        run_preview=_core_upload_bool(run_preview),
        options={**options, "source_trust": "uploaded_document"},
    )
    grow_response = _grow_source_preview("grow_source_upload", payload)
    return _core_upload_response_payload(
        brain_id=resolved_brain_id,
        storage_path=str(brain_record.get("storage_path") or "") or None,
        source_package=source_package,
        file_name=file_name,
        content_type=content_type,
        file_bytes=file_bytes,
        grow_response=grow_response,
    )


class MaintenanceMutationRuntime(Protocol):
    def preview(
        self,
        *,
        graph: dict[str, Any],
        mode: str,
        focus_node_id: str | None,
        max_nodes_considered: int,
    ) -> dict[str, Any]: ...

    def apply(
        self,
        *,
        graph: dict[str, Any],
        mode: str,
        focus_node_id: str | None,
        max_nodes_considered: int,
        expected_preview_signature: str | None = None,
        selected_proposal_ids: list[str] | None = None,
    ) -> dict[str, Any]: ...

    def rollback(
        self,
        *,
        mode: str,
        preview_signature: str,
    ) -> dict[str, Any]: ...


def create_core_mcp_ops_router(
    *,
    maintenance_runtime: MaintenanceMutationRuntime | None = None,
) -> APIRouter:
    router = APIRouter()

    @router.get("/memory/mcp/grow-source-policy")
    @router.get("/mcp/grow-source-policy")
    def grow_source_policy() -> dict[str, Any]:
        return grow_source_policy_contract()

    @router.post("/memory/mcp/grow-source-preview", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/grow-source-preview", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    def grow_source_preview(payload: McpGrowSourceRequest) -> McpGrowToolExecutionResponse:
        return _grow_source_preview("grow_source_preview", _canonical_source_preview_payload(payload))

    @router.post("/memory/source-investigation/upload")
    @router.post("/source-investigation/upload")
    @router.post("/memory/mcp/grow-source-upload")
    @router.post("/mcp/grow-source-upload")
    async def grow_source_upload(
        file: UploadFile | None = File(default=None),
        brain_id: str | None = Form(default=None),
        source_label: str | None = Form(default=None),
        source_uri: str | None = Form(default=None),
        user_instruction: str | None = Form(default=None),
        input_kind: str = Form(default="auto"),
        treat_as: str = Form(default="technical_document"),
        analyze_images: str = Form(default="off"),
        run_preview: bool = Form(default=True),
        question_limit: int = Form(default=12),
        max_pages: int = Form(default=20),
        max_ocr_pages: int = Form(default=8),
        max_images: int = Form(default=12),
        max_online_queries: int = Form(default=0),
        max_units: int = Form(default=12),
        max_total_chars: int = Form(default=120000),
        semantic_preview: bool = Form(default=False),
        investigation_id: str | None = Form(default=None),
        resume_token: str | None = Form(default=None),
        investigation_version: int | None = Form(default=None),
        clarification_answers_json: str = Form(default="{}"),
        clarification_answers: str | None = Form(default=None),
        source_storage_mode: str = Form(default="auto"),
    ) -> dict[str, Any]:
        resume_requested = bool(investigation_id and resume_token)
        if bool(investigation_id) != bool(resume_token):
            raise HTTPException(
                status_code=400,
                detail="investigation_id_and_resume_token_required_together",
            )
        if resume_requested:
            answers = _core_upload_clarification_answers_json(
                clarification_answers
                if clarification_answers is not None and str(clarification_answers).strip()
                else clarification_answers_json
            )
            return _grow_source_upload_resume_without_file(
                brain_id=brain_id,
                source_label=source_label,
                source_uri=source_uri,
                user_instruction=user_instruction,
                input_kind=input_kind,
                treat_as=treat_as,
                analyze_images=analyze_images,
                question_limit=question_limit,
                max_pages=max_pages,
                max_ocr_pages=max_ocr_pages,
                max_images=max_images,
                max_online_queries=max_online_queries,
                max_units=max_units,
                max_total_chars=max_total_chars,
                semantic_preview=semantic_preview,
                investigation_id=str(investigation_id or ""),
                resume_token=str(resume_token or ""),
                investigation_version=investigation_version,
                clarification_answers=answers,
            )
        if file is None:
            raise HTTPException(status_code=400, detail="source_file_required")
        try:
            file_bytes = await _read_core_grow_upload_bounded(file)
        except CoreGrowUploadTooLarge:
            return JSONResponse(
                status_code=413,
                content={
                    "detail": "source_file_too_large",
                    "max_bytes": CORE_GROW_UPLOAD_MAX_BYTES,
                },
            )
        if not file_bytes:
            raise HTTPException(status_code=400, detail="source_file_required")
        return _grow_source_upload_from_bytes(
            file_bytes=file_bytes,
            file_name=file.filename or "uploaded_source",
            content_type=file.content_type,
            brain_id=brain_id,
            source_label=source_label,
            source_uri=source_uri,
            user_instruction=user_instruction,
            input_kind=input_kind,
            treat_as=treat_as,
            analyze_images=analyze_images,
            run_preview=run_preview,
            question_limit=question_limit,
            max_pages=max_pages,
            max_ocr_pages=max_ocr_pages,
            max_images=max_images,
            max_online_queries=max_online_queries,
            max_units=max_units,
            max_total_chars=max_total_chars,
            semantic_preview=semantic_preview,
            source_storage_mode=source_storage_mode,
        )

    @router.post("/memory/mcp/grow-preview", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/grow-preview", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    def grow_preview(payload: McpGrowSourceRequest) -> McpGrowToolExecutionResponse:
        return _grow_source_preview("grow_preview", _canonical_semantic_grow_payload(payload))

    @router.post("/memory/mcp/grow-guided", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/grow-guided", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    def grow_guided(payload: McpGrowSourceRequest) -> McpGrowToolExecutionResponse:
        return _grow_source_preview("grow_guided", _canonical_guided_grow_payload(payload, force_guided=True))

    @router.post("/memory/mcp/grow-source-apply", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/grow-source-apply", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    def grow_source_apply(payload: McpGrowApplyRequest) -> McpGrowToolExecutionResponse:
        return _grow_source_apply("grow_source_apply", payload)

    @router.post("/memory/mcp/grow-apply", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/grow-apply", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    def grow_apply(payload: McpGrowApplyRequest) -> McpGrowToolExecutionResponse:
        return _grow_source_apply("grow_apply", payload)

    @router.post("/memory/mcp/grow-source-rollback", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/grow-source-rollback", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    def grow_source_rollback(payload: McpGrowRollbackRequest) -> McpGrowToolExecutionResponse:
        return _grow_source_rollback("grow_source_rollback", payload)

    @router.post("/memory/mcp/grow-rollback", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/grow-rollback", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    def grow_rollback(payload: McpGrowRollbackRequest) -> McpGrowToolExecutionResponse:
        return _grow_source_rollback("grow_rollback", payload)

    @router.post("/memory/mcp/grow-source-status", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/grow-source-status", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    def grow_source_status(payload: McpGrowApplyRequest) -> McpGrowToolExecutionResponse:
        return _grow_source_status("grow_source_status", payload)

    @router.post("/memory/mcp/grow-status", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/grow-status", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    def grow_status(payload: McpGrowApplyRequest) -> McpGrowToolExecutionResponse:
        return _grow_source_status("grow_status", payload)

    @router.post("/memory/mcp/list-contradictions", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/list-contradictions", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    def list_contradictions(payload: McpMemoryOSListRequest) -> McpMaintenanceToolExecutionResponse:
        return _mcp_memory_os_list("list_contradictions", payload)

    @router.post("/memory/mcp/change-node-content", response_model_exclude_none=True)
    @router.post("/mcp/change-node-content", response_model_exclude_none=True)
    def change_node_content(payload: dict[str, Any]) -> dict[str, Any]:
        return _mcp_change_node_content(payload)

    @router.post("/memory/mcp/delete-node", response_model_exclude_none=True)
    @router.post("/mcp/delete-node", response_model_exclude_none=True)
    def delete_node(payload: dict[str, Any]) -> dict[str, Any]:
        return _mcp_delete_node(payload)

    @router.post("/memory/mcp/write-memory-preview", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/write-memory-preview", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    def write_memory_preview(payload: McpWriteMemoryPreviewRequest) -> McpGrowToolExecutionResponse:
        return _write_memory_preview("write_memory_preview", payload)

    @router.post("/memory/mcp/write-memory-commit", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/write-memory-commit", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    def write_memory_commit(payload: McpWriteMemoryCommitRequest) -> McpGrowToolExecutionResponse:
        return _write_memory_commit("write_memory_commit", payload)

    @router.post("/memory/mcp/ask-memory-clarification", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/ask-memory-clarification", response_model=McpGrowToolExecutionResponse, response_model_exclude_none=True)
    def ask_memory_clarification(payload: McpClarificationRequest) -> McpGrowToolExecutionResponse:
        return _ask_memory_clarification("ask_memory_clarification", payload)

    @router.post("/memory/mcp/sleep-preview", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/sleep-preview", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    def sleep_preview(payload: McpMaintenanceRequest) -> McpMaintenanceToolExecutionResponse:
        brain_record = _resolve_bootstrap_ready_brain_record(payload.brain_id)
        _ensure_maintain_studio_entitled()
        return _maintenance_preview(
            "sleep_preview",
            "sleep",
            payload,
            brain_record=brain_record,
            runtime=maintenance_runtime,
        )

    @router.post("/memory/mcp/evolve-preview", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/evolve-preview", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    def evolve_preview(payload: McpMaintenanceRequest) -> McpMaintenanceToolExecutionResponse:
        brain_record = _resolve_bootstrap_ready_brain_record(payload.brain_id)
        _ensure_maintain_studio_entitled()
        return _maintenance_preview(
            "evolve_preview",
            "evolve",
            payload,
            brain_record=brain_record,
            runtime=maintenance_runtime,
        )

    @router.post("/memory/mcp/sleep-apply", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/sleep-apply", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    def sleep_apply(payload: McpMaintenanceApplyRequest) -> McpMaintenanceToolExecutionResponse:
        brain_record = _resolve_bootstrap_ready_brain_record(payload.brain_id)
        _ensure_maintain_studio_entitled()
        return _maintenance_apply(
            "sleep_apply",
            "sleep",
            payload,
            brain_record=brain_record,
            runtime=maintenance_runtime,
        )

    @router.post("/memory/mcp/evolve-apply", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    @router.post("/mcp/evolve-apply", response_model=McpMaintenanceToolExecutionResponse, response_model_exclude_none=True)
    def evolve_apply(payload: McpMaintenanceApplyRequest) -> McpMaintenanceToolExecutionResponse:
        brain_record = _resolve_bootstrap_ready_brain_record(payload.brain_id)
        _ensure_maintain_studio_entitled()
        return _maintenance_apply(
            "evolve_apply",
            "evolve",
            payload,
            brain_record=brain_record,
            runtime=maintenance_runtime,
        )

    @router.post("/memory/mcp/sleep-rollback", response_model_exclude_none=True)
    @router.post("/mcp/sleep-rollback", response_model_exclude_none=True)
    def sleep_rollback(payload: dict[str, Any]) -> dict[str, Any]:
        brain_record = _resolve_bootstrap_ready_brain_record(str(payload.get("brain_id") or "").strip() or None)
        _ensure_maintain_studio_entitled()
        return _maintenance_rollback(
            "sleep_rollback",
            "sleep",
            payload,
            brain_record=brain_record,
            runtime=maintenance_runtime,
        )

    @router.post("/memory/mcp/evolve-rollback", response_model_exclude_none=True)
    @router.post("/mcp/evolve-rollback", response_model_exclude_none=True)
    def evolve_rollback(payload: dict[str, Any]) -> dict[str, Any]:
        brain_record = _resolve_bootstrap_ready_brain_record(str(payload.get("brain_id") or "").strip() or None)
        _ensure_maintain_studio_entitled()
        return _maintenance_rollback(
            "evolve_rollback",
            "evolve",
            payload,
            brain_record=brain_record,
            runtime=maintenance_runtime,
        )

    return router


def _ensure_maintain_studio_entitled() -> None:
    ensure_local_module_entitled(MAINTAIN_MODULE_ID)


def _mcp_memory_os_list(
    tool_name: str,
    payload: McpMemoryOSListRequest,
) -> McpMaintenanceToolExecutionResponse:
    brain_record = _resolve_bootstrap_ready_brain_record(payload.brain_id)
    brain_id = _brain_record_id(brain_record) or payload.brain_id
    limit = _memory_os_limit(payload.limit)
    with use_runtime_brain(brain_record):
        bootstrap_runtime_store()
        try:
            graph = fetch_graph_snapshot()
        except Exception:
            graph = {"nodes": [], "edges": []}
        try:
            recent_search_sessions = fetch_recent_search_sessions(limit=limit)
        except Exception:
            recent_search_sessions = []
        try:
            maintenance_runs = fetch_recent_maintenance_runs(limit=limit)
        except Exception:
            maintenance_runs = []
        output = _build_core_memory_os_list_output(
            tool_name,
            graph=graph,
            recent_search_sessions=recent_search_sessions,
            maintenance_runs=maintenance_runs,
            grow_runs=_memory_os_grow_runs_for_brain(brain_id),
            limit=limit,
        )
    if brain_id:
        output["brain_id"] = brain_id
    return McpMaintenanceToolExecutionResponse(**output)


def _memory_os_limit(value: int | None) -> int:
    try:
        return max(1, min(int(value or 25), 100))
    except (TypeError, ValueError):
        return 25


def _build_core_memory_os_list_output(
    tool_name: str,
    *,
    graph: dict[str, Any] | None = None,
    recent_search_sessions: list[dict[str, Any]] | None = None,
    maintenance_runs: list[dict[str, Any]] | None = None,
    grow_runs: dict[str, dict[str, Any]] | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    if tool_name != "list_contradictions":
        raise ValueError(f"unsupported_mcp_memory_os_list_tool:{tool_name}")
    safe_limit = _memory_os_limit(limit)
    nodes = [
        dict(node)
        for node in _memory_os_as_list(_memory_os_as_dict(graph).get("nodes"))
        if isinstance(node, dict)
    ]
    sessions = [dict(session) for session in _memory_os_as_list(recent_search_sessions) if isinstance(session, dict)]
    runs = [dict(run) for run in _memory_os_as_list(maintenance_runs) if isinstance(run, dict)]
    grow = {
        str(key): dict(value)
        for key, value in _memory_os_as_dict(grow_runs).items()
        if isinstance(value, dict)
    }
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    for node in nodes:
        claim_status = str(node.get("claim_status") or "").strip().lower()
        act_type = str(node.get("memory_act_type") or "").strip().lower()
        if claim_status in {"contradiction", "superseded"} or "contradiction" in act_type or "supersede" in act_type:
            claim = _memory_os_node_text(node)
            _memory_os_add_item(
                items,
                seen,
                {
                    "kind": "memory_contradiction",
                    "claim": claim,
                    "human_summary": f"The brain has a stored claim that needs conflict review: {claim}"
                    if claim
                    else "The brain has a stored claim that needs conflict review.",
                    "source": "graph_node",
                    "node_id": _memory_os_node_id(node),
                    "claim_status": node.get("claim_status"),
                    "memory_type": node.get("memory_type"),
                },
                prefix="contradiction",
            )

    for session in sessions:
        result = _memory_os_recent_result(session)
        flags = _memory_os_string_list(result.get("contradiction_flags"), limit=12)
        if bool(result.get("contradiction_present")) or flags:
            query_text = str(session.get("query_text") or result.get("query_text") or "").strip()
            _memory_os_add_item(
                items,
                seen,
                {
                    "kind": "retrieval_contradiction_signal",
                    "claim": "Recent retrieval exposed contradiction signals.",
                    "human_summary": f"Recent search evidence reported a conflict for: {query_text}"
                    if query_text
                    else "Recent search evidence reported a conflict.",
                    "source": "search_session",
                    "search_id": session.get("search_id"),
                    "query_text": query_text or None,
                    "contradiction_flags": flags,
                },
                prefix="contradiction",
            )

    for proposal, maintenance_id in _memory_os_iter_recent_proposals(runs):
        if str(proposal.get("proposal_kind") or "") == "contradiction_review":
            claim = str(proposal.get("reason") or "").strip()
            _memory_os_add_item(
                items,
                seen,
                {
                    "kind": "maintenance_contradiction_review",
                    "claim": claim,
                    "human_summary": f"Maintenance preview found a conflict to review: {claim}"
                    if claim
                    else "Maintenance preview found a conflict to review.",
                    "source": "maintenance_proposal",
                    "proposal_id": proposal.get("proposal_id"),
                    "maintenance_id": maintenance_id,
                    "risk_level": proposal.get("risk_level"),
                },
                prefix="contradiction",
            )

    for investigation_id, run in grow.items():
        for packet in _memory_os_grow_feedback_packets(run):
            decision = str(packet.get("claim_decision") or "").strip()
            if decision not in {
                "duplicate",
                "contradicts_existing",
                "supersedes_existing",
                "delete_existing",
                "enrich_existing",
            }:
                continue
            intent = _memory_os_as_dict(packet.get("maintenance_intent"))
            summary = (
                _memory_os_first_text(packet, "summary", "reason", "explanation", "claim_text", "claim")
                or _memory_os_first_text(intent, "reason", "expected_decision_change")
                or f"Grow produced a {decision.replace('_', ' ')} review decision."
            )
            _memory_os_add_item(
                items,
                seen,
                {
                    "kind": "grow_review_issue",
                    "issue_type": _memory_os_grow_issue_type(decision),
                    "status": str(packet.get("state") or "review_needed"),
                    "title": _memory_os_grow_issue_title(decision),
                    "claim": _memory_os_first_text(packet, "claim_text", "claim", "new_claim", "incoming_claim") or summary,
                    "summary": summary,
                    "human_summary": summary,
                    "source": "grow_maintenance_feedback",
                    "investigation_id": investigation_id,
                    "feedback_id": packet.get("feedback_id"),
                    "claim_decision": decision,
                    "target_node_ids": _memory_os_string_list(packet.get("target_node_ids"), limit=32),
                    "evidence_refs": _memory_os_string_list(packet.get("evidence_refs"), limit=32),
                    "document_refs": _memory_os_as_list(packet.get("document_refs")),
                    "maintenance_intent": intent,
                },
                prefix="grow_review_issue",
            )

    limited = items[:safe_limit]
    return {
        "schema_version": MCP_MEMORY_OS_LIST_SCHEMA_VERSION,
        "tool_name": tool_name,
        "status": "ok" if len(items) <= safe_limit else "partial",
        "contradictions": limited,
        "source_trace": [
            {"source": "graph", "node_count": len(nodes)},
            {"source": "recent_search_sessions", "session_count": len(sessions)},
            {"source": "maintenance_runs", "run_count": len(runs)},
            {"source": "mcp_grow_runtime_cache", "run_count": len(grow)},
        ],
        "completeness": {
            "field_name": "contradictions",
            "returned_count": len(limited),
            "total_count": len(items),
            "limit": safe_limit,
            "truncated": len(items) > safe_limit,
        },
        "budget": {
            "limit": safe_limit,
            "graph_node_count": len(nodes),
            "recent_search_session_count": len(sessions),
            "maintenance_run_count": len(runs),
            "grow_run_count": len(grow),
        },
    }


def _memory_os_grow_runs_for_brain(brain_id: str | None) -> dict[str, dict[str, Any]]:
    resolved_brain_id = str(brain_id or "").strip()
    runs: dict[str, dict[str, Any]] = {}
    for investigation_id, run in _GROW_PREVIEW_RUNS.items():
        if not isinstance(run, dict):
            continue
        run_brain_id = str(run.get("brain_id") or "").strip()
        if resolved_brain_id and run_brain_id and run_brain_id != resolved_brain_id:
            continue
        runs[str(investigation_id)] = deepcopy(run)
    return runs


def _memory_os_as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _memory_os_as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _memory_os_stable_id(prefix: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str).encode("utf-8")
    return f"{prefix}::{hashlib.sha256(encoded).hexdigest()[:16]}"


def _memory_os_string_list(value: Any, *, limit: int = 24) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, int, float)):
        values = [value]
    elif isinstance(value, dict):
        values = [value.get("id") or value.get("key") or value.get("value")]
    else:
        values = list(value or [])
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def _memory_os_node_id(node: dict[str, Any]) -> str:
    return str(node.get("id") or "").strip()


def _memory_os_node_text(node: dict[str, Any]) -> str:
    return str(node.get("raw_text") or node.get("summary") or node.get("text") or "").strip()


def _memory_os_grow_feedback_packets(run: dict[str, Any]) -> list[dict[str, Any]]:
    packets: list[dict[str, Any]] = []
    for container in [
        run,
        _memory_os_as_dict(run.get("source_investigation")),
        _memory_os_as_dict(run.get("result")),
    ]:
        for key in ("maintenance_feedback_packets", "maintenance_feedback", "feedback_packets"):
            packets.extend(
                dict(item)
                for item in _memory_os_as_list(container.get(key))
                if isinstance(item, dict)
            )
    return packets


def _memory_os_grow_issue_type(decision: str) -> str:
    return {
        "duplicate": "duplicated_claim",
        "contradicts_existing": "contradictory_claim",
        "supersedes_existing": "stale_source",
        "delete_existing": "remove_memory_candidate",
        "enrich_existing": "missing_evidence",
    }.get(decision, "review_issue")


def _memory_os_grow_issue_title(decision: str) -> str:
    return {
        "duplicate": "Duplicate claim from new source",
        "contradicts_existing": "Contradictory claim from new source",
        "supersedes_existing": "Newer source may supersede old memory",
        "delete_existing": "Memory removal candidate",
        "enrich_existing": "Existing memory needs enrichment",
    }.get(decision, "Review issue from Grow")


def _memory_os_first_text(record: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = record.get(key)
        if isinstance(value, (str, int, float)):
            text = str(value).strip()
            if text:
                return text
    return ""


def _memory_os_node_source(node: dict[str, Any]) -> dict[str, Any]:
    provenance = _memory_os_as_dict(node.get("provenance"))
    claim_date = _memory_os_first_text(
        provenance,
        "claim_date",
        "effective_date",
        "valid_from",
        "as_of_date",
        "published_date",
        "source_published_at",
        "retrieved_at",
        "source_retrieved_at",
        "created_at",
    ) or _memory_os_first_text(
        node,
        "claim_date",
        "effective_date",
        "valid_from",
        "as_of_date",
        "published_date",
        "created_at",
        "updated_at",
    )
    source_uri = _memory_os_first_text(provenance, "source_uri", "url") or _memory_os_first_text(node, "source_uri", "url")
    source_label = _memory_os_first_text(provenance, "source_label", "title", "label") or _memory_os_first_text(
        node,
        "source_label",
        "title",
        "label",
    )
    source_type = _memory_os_first_text(provenance, "source_type", "type") or _memory_os_first_text(
        node,
        "source_type",
        "memory_type",
        "node_kind",
    )
    return {
        "source": "graph_node",
        "node_id": _memory_os_node_id(node),
        "target_node_ids": [_memory_os_node_id(node)] if _memory_os_node_id(node) else [],
        "title": source_label or _memory_os_node_text(node)[:96] or "Memory item",
        "source_label": source_label or None,
        "source_uri": source_uri or None,
        "source_type": source_type or None,
        "claim_date": claim_date or None,
        "text": _memory_os_node_text(node)[:500],
    }


def _memory_os_normalized_claim(text: str) -> str:
    return " ".join(str(text or "").strip().lower().split())


def _memory_os_expired_date(node: dict[str, Any]) -> str | None:
    provenance = _memory_os_as_dict(node.get("provenance"))
    raw_date = _memory_os_first_text(
        node,
        "valid_to",
        "expires_at",
        "expiration_date",
        "certificate_expiry_date",
    ) or _memory_os_first_text(
        provenance,
        "valid_to",
        "expires_at",
        "expiration_date",
        "certificate_expiry_date",
    )
    if not raw_date:
        return None
    try:
        parsed = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return raw_date if parsed.astimezone(timezone.utc) < datetime.now(timezone.utc) else None


def _memory_os_add_graph_review_issues(
    items: list[dict[str, Any]],
    seen: set[str],
    nodes: list[dict[str, Any]],
    *,
    limit: int,
) -> None:
    duplicate_groups: dict[str, list[dict[str, Any]]] = {}
    missing_source_nodes: list[dict[str, Any]] = []
    stale_nodes: list[tuple[dict[str, Any], str]] = []
    for node in nodes:
        text = _memory_os_node_text(node)
        key = _memory_os_normalized_claim(text)
        if len(key) >= 40:
            duplicate_groups.setdefault(key, []).append(node)
        source = _memory_os_node_source(node)
        if text and not source.get("source_uri"):
            missing_source_nodes.append(node)
        expired = _memory_os_expired_date(node)
        if expired:
            stale_nodes.append((node, expired))

    review_budget = max(1, min(limit, 6))
    for group in sorted(duplicate_groups.values(), key=len, reverse=True):
        if len(group) < 2 or len(items) >= review_budget:
            continue
        text = _memory_os_node_text(group[0])
        sources = [_memory_os_node_source(node) for node in group[:6]]
        _memory_os_add_item(
            items,
            seen,
            {
                "kind": "duplicated_claim",
                "issue_type": "duplicated_claim",
                "status": "review_needed",
                "title": "Duplicate memory to review",
                "claim": text,
                "summary": f"The brain stores this same claim {len(group)} times. Review whether these should be kept as separate source observations or merged.",
                "human_summary": f"The brain stores this same claim {len(group)} times. Review whether these should be kept as separate source observations or merged.",
                "source": "graph_quality_review",
                "evidence": sources,
                "target_node_ids": [source["node_id"] for source in sources if source.get("node_id")],
                "recommended_action": "Keep every distinct source if provenance differs; otherwise merge duplicate memory.",
            },
            prefix="review_issue",
        )

    if missing_source_nodes and len(items) < review_budget:
        sources = [_memory_os_node_source(node) for node in missing_source_nodes[:8]]
        _memory_os_add_item(
            items,
            seen,
            {
                "kind": "missing_evidence",
                "issue_type": "missing_evidence",
                "status": "review_needed",
                "title": "Source link missing",
                "claim": "Some stored claims do not expose a source URL.",
                "summary": f"{len(missing_source_nodes)} stored claim(s) have text but no source URL attached. They should stay lower-confidence until a document or public URL is linked.",
                "human_summary": f"{len(missing_source_nodes)} stored claim(s) have text but no source URL attached. They should stay lower-confidence until a document or public URL is linked.",
                "source": "graph_quality_review",
                "evidence": sources,
                "target_node_ids": [source["node_id"] for source in sources if source.get("node_id")],
                "recommended_action": "Attach a source document/URL or mark the claim as not independently verified.",
            },
            prefix="review_issue",
        )

    for node, expired in stale_nodes[: max(0, review_budget - len(items))]:
        text = _memory_os_node_text(node)
        source = _memory_os_node_source(node)
        _memory_os_add_item(
            items,
            seen,
            {
                "kind": "stale_source",
                "issue_type": "stale_source",
                "status": "review_needed",
                "title": "Expired or stale source",
                "claim": text,
                "summary": f"This claim is attached to an explicit validity end date that has passed: {expired}.",
                "human_summary": f"This claim is attached to an explicit validity end date that has passed: {expired}.",
                "source": "graph_quality_review",
                "evidence": [source],
                "target_node_ids": [source["node_id"]] if source.get("node_id") else [],
                "recommended_action": "Refresh the source or supersede the stale claim after review.",
            },
            prefix="review_issue",
        )


def _payload_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, (str, int, float)):
            text = str(value).strip()
            if text:
                return text
    return ""


def _payload_bool(payload: dict[str, Any], *keys: str) -> bool:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, (str, int, float)):
            text = str(value).strip().lower()
            if text in {"1", "true", "yes", "y", "on"}:
                return True
            if text in {"0", "false", "no", "n", "off"}:
                return False
    return False


def _payload_text_list(payload: dict[str, Any], *keys: str) -> list[str]:
    values: list[Any] = []
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            values.extend(value)
        else:
            values.append(value)
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _node_content_hash(text: str) -> str:
    return f"sha256:{hashlib.sha256(str(text or '').encode('utf-8')).hexdigest()}"


def _node_mutation_id(kind: str, payload: dict[str, Any], brain_revision: str, *, suffix: str) -> str:
    material = {
        "kind": kind,
        "brain_id": _payload_text(payload, "brain_id"),
        "node_id": _payload_text(payload, "node_id", "target_node_id"),
        "target_node_ids": _payload_text_list(payload, "target_node_ids"),
        "new_content_hash": _node_content_hash(_payload_text(payload, "new_content", "content", "replacement_text", "correction_text")),
        "reason": _payload_text(payload, "reason", "human_reason", "conflict_summary"),
        "brain_revision": brain_revision,
        "suffix": suffix,
    }
    encoded = json.dumps(material, ensure_ascii=True, sort_keys=True, default=str).encode("utf-8")
    return f"mcp-{kind}-{suffix}-{hashlib.sha256(encoded).hexdigest()[:16]}"


def _compact_node_for_mcp(node: dict[str, Any] | None) -> dict[str, Any] | None:
    if not node:
        return None
    provenance = dict(node.get("provenance") or {})
    return {
        "node_id": _memory_os_node_id(node),
        "title": str(node.get("title") or node.get("label") or "").strip() or None,
        "content": _memory_os_node_text(node)[:1600],
        "content_sha256": _node_content_hash(_memory_os_node_text(node)),
        "memory_type": node.get("memory_type") or node.get("node_kind"),
        "claim_status": node.get("claim_status"),
        "lifecycle_status": node.get("lifecycle_status"),
        "source_label": provenance.get("source_label") or node.get("source_label"),
        "source_uri": provenance.get("source_uri") or node.get("source_uri"),
        "is_document_anchor": bool(
            node.get("is_document_anchor")
            or str(node.get("memory_type") or node.get("node_kind") or "").strip() == "document_anchor"
        ),
    }


def _find_graph_node(graph: dict[str, Any], node_id: str) -> dict[str, Any] | None:
    for node in list(graph.get("nodes") or []):
        if not isinstance(node, dict):
            continue
        if _memory_os_node_id(node) == node_id:
            return node
    return None


def _edge_touches_node(edge: dict[str, Any], node_ids: set[str]) -> bool:
    endpoint_keys = {
        "source",
        "source_id",
        "source_node_id",
        "from",
        "from_id",
        "target",
        "target_id",
        "target_node_id",
        "to",
        "to_id",
    }
    return any(str(edge.get(key) or "").strip() in node_ids for key in endpoint_keys)


def _graph_receipts(graph: dict[str, Any]) -> list[dict[str, Any]]:
    meta = graph.setdefault("meta", {})
    if not isinstance(meta, dict):
        meta = {}
        graph["meta"] = meta
    receipts = meta.get("node_mutation_receipts")
    if not isinstance(receipts, list):
        receipts = []
        meta["node_mutation_receipts"] = receipts
    return receipts


def _node_change_after(node: dict[str, Any], payload: dict[str, Any], now: str) -> dict[str, Any]:
    new_content = _payload_text(payload, "new_content", "content", "replacement_text", "correction_text")
    after = deepcopy(node)
    previous_text = _memory_os_node_text(node)
    fields_present = [field for field in ("raw_text", "summary", "text") if field in after]
    if not fields_present:
        fields_present = ["raw_text", "summary"]
    for field in fields_present:
        after[field] = new_content
    if _payload_text(payload, "title"):
        after["title"] = _payload_text(payload, "title")
    if _payload_text(payload, "claim_status"):
        after["claim_status"] = _payload_text(payload, "claim_status")
    else:
        after["claim_status"] = after.get("claim_status") or "corrected"
    after["updated_at"] = now
    try:
        after["node_revision"] = int(after.get("node_revision") or 0) + 1
    except (TypeError, ValueError):
        after["node_revision"] = 1
    provenance = dict(after.get("provenance") or {})
    history = list(provenance.get("manual_node_content_changes") or [])
    history.append(
        {
            "changed_at": now,
            "previous_content_sha256": _node_content_hash(previous_text),
            "new_content_sha256": _node_content_hash(new_content),
            "reason": _payload_text(payload, "reason", "human_reason", "conflict_summary") or "manual node content update",
            "search_id": _payload_text(payload, "search_id") or None,
            "conflict_id": _payload_text(payload, "conflict_id", "registry_key") or None,
        }
    )
    provenance["manual_node_content_changes"] = history[-20:]
    after["provenance"] = provenance
    return after


def _mcp_change_node_content(payload: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="json_object_required")
    node_id = _payload_text(payload, "node_id", "target_node_id")
    new_content = _payload_text(payload, "new_content", "content", "replacement_text", "correction_text")
    if not node_id:
        raise HTTPException(status_code=400, detail="node_id_required")
    if not new_content:
        raise HTTPException(status_code=400, detail="new_content_required")
    brain_record = _resolve_bootstrap_ready_brain_record(_payload_text(payload, "brain_id") or None)
    brain_id = _brain_record_id(brain_record)
    confirm_change = _payload_bool(payload, "confirm_change", "confirm_apply")
    idempotency_key = _payload_text(payload, "idempotency_key") or None
    with use_runtime_brain(brain_record):
        bootstrap_runtime_store()
        graph = fetch_graph_snapshot()
        before_revision = maintenance_graph_revision(graph)
        node = _find_graph_node(graph, node_id)
        if not node:
            return {
                "schema_version": "agvm.mcp_change_node_content.v1",
                "tool_name": "change_node_content",
                "status": "blocked",
                "brain_id": brain_id,
                "node_id": node_id,
                "blocked_reasons": ["node_not_found"],
                "mutates_memory": False,
                "mcp_latency_profile": {"elapsed_ms": int((time.perf_counter() - started) * 1000)},
            }
        preview_id = _node_mutation_id("change-node-content", payload, before_revision, suffix="preview")
        now = _utc_now()
        after_node = _node_change_after(node, payload, now)
        preview = {
            "schema_version": "agvm.mcp_change_node_content.v1",
            "tool_name": "change_node_content",
            "status": "preview_ready" if not confirm_change else "applying",
            "brain_id": brain_id,
            "node_id": node_id,
            "preview_id": preview_id,
            "before_brain_revision": before_revision,
            "expected_brain_revision": before_revision,
            "mutates_memory": False,
            "can_apply_now": True,
            "confirm_field": "confirm_change",
            "before": _compact_node_for_mcp(node),
            "after": _compact_node_for_mcp(after_node),
            "apply_contract": {
                "preview_required": False,
                "explicit_confirm_required": True,
                "confirm_change_required": True,
                "expected_brain_revision_recommended": True,
            },
            "mcp_latency_profile": {"elapsed_ms": int((time.perf_counter() - started) * 1000)},
        }
        if not confirm_change:
            return preview

        expected_revision = _payload_text(payload, "expected_brain_revision", "before_brain_revision")
        if expected_revision and expected_revision != before_revision:
            return {
                **preview,
                "status": "blocked",
                "blocked_reasons": ["brain_revision_mismatch"],
                "received_expected_brain_revision": expected_revision,
                "current_brain_revision": before_revision,
            }
        receipts = _graph_receipts(graph)
        if idempotency_key:
            for receipt in receipts:
                if isinstance(receipt, dict) and receipt.get("idempotency_key") == idempotency_key:
                    return {
                        **preview,
                        "status": "applied",
                        "mutates_memory": True,
                        "replayed": True,
                        "apply_receipt": receipt,
                    }
        graph["nodes"] = [
            after_node if isinstance(item, dict) and _memory_os_node_id(item) == node_id else item
            for item in list(graph.get("nodes") or [])
        ]
        receipt = {
            "receipt_id": _node_mutation_id("change-node-content", payload, before_revision, suffix="apply"),
            "idempotency_key": idempotency_key,
            "operation": "change_node_content",
            "brain_id": brain_id,
            "node_id": node_id,
            "changed_at": now,
            "before_brain_revision": before_revision,
            "before_content_sha256": _node_content_hash(_memory_os_node_text(node)),
            "after_content_sha256": _node_content_hash(new_content),
            "reason": _payload_text(payload, "reason", "human_reason", "conflict_summary") or None,
            "search_id": _payload_text(payload, "search_id") or None,
            "conflict_id": _payload_text(payload, "conflict_id", "registry_key") or None,
        }
        receipts.append(receipt)
        graph["meta"]["node_mutation_receipts"] = receipts[-100:]
        saved_graph = replace_runtime_graph(graph)
        after_revision = maintenance_graph_revision(saved_graph)
        receipt["after_brain_revision"] = after_revision
        try:
            store_correction_history(
                correction_id=str(receipt["receipt_id"]),
                search_id=_payload_text(payload, "search_id") or None,
                query_text=_payload_text(payload, "query_text", "question"),
                returned_answer=_payload_text(payload, "returned_answer", "answer"),
                correction_text=new_content,
                correction_mode="change_node_content",
                used_evidence_node_ids=_payload_text_list(payload, "evidence_node_ids", "used_evidence_node_ids"),
                target_node_ids=[node_id],
                action_summary=receipt,
            )
        except Exception as exc:  # pragma: no cover - ledger failure must not hide the committed graph mutation.
            receipt["history_warning"] = str(exc)
        search_id = _payload_text(payload, "search_id")
        if search_id:
            append_search_event(search_id, "node_content_changed", {"receipt": receipt})
        return {
            **preview,
            "status": "applied",
            "mutates_memory": True,
            "after_brain_revision": after_revision,
            "apply_receipt": receipt,
            "mcp_latency_profile": {"elapsed_ms": int((time.perf_counter() - started) * 1000)},
        }


def _mcp_delete_node(payload: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="json_object_required")
    target_node_ids = _payload_text_list(payload, "target_node_ids", "node_ids")
    single_node_id = _payload_text(payload, "node_id", "target_node_id")
    if single_node_id:
        target_node_ids = [single_node_id, *[node_id for node_id in target_node_ids if node_id != single_node_id]]
    if not target_node_ids:
        raise HTTPException(status_code=400, detail="node_id_required")
    brain_record = _resolve_bootstrap_ready_brain_record(_payload_text(payload, "brain_id") or None)
    brain_id = _brain_record_id(brain_record)
    confirm_delete = _payload_bool(payload, "confirm_delete", "confirm_apply")
    idempotency_key = _payload_text(payload, "idempotency_key") or None
    with use_runtime_brain(brain_record):
        bootstrap_runtime_store()
        graph = fetch_graph_snapshot()
        before_revision = maintenance_graph_revision(graph)
        node_by_id = {
            _memory_os_node_id(node): node
            for node in list(graph.get("nodes") or [])
            if isinstance(node, dict) and _memory_os_node_id(node)
        }
        existing_nodes = [node_by_id[node_id] for node_id in target_node_ids if node_id in node_by_id]
        missing_ids = [node_id for node_id in target_node_ids if node_id not in node_by_id]
        blocked_reasons = ["node_not_found"] if missing_ids else []
        protected_ids = [
            _memory_os_node_id(node)
            for node in existing_nodes
            if bool(node.get("is_document_anchor"))
            or str(node.get("memory_type") or node.get("node_kind") or "").strip() == "document_anchor"
        ]
        if protected_ids and not _payload_bool(payload, "allow_document_anchor_delete"):
            blocked_reasons.append("document_anchor_delete_requires_explicit_override")
        deleted_id_set = set(target_node_ids) - set(missing_ids)
        removed_edge_count = sum(
            1
            for edge in list(graph.get("edges") or [])
            if isinstance(edge, dict) and _edge_touches_node(edge, deleted_id_set)
        )
        preview_id = _node_mutation_id("delete-node", payload, before_revision, suffix="preview")
        preview = {
            "schema_version": "agvm.mcp_delete_node.v1",
            "tool_name": "delete_node",
            "status": "blocked" if blocked_reasons else "preview_ready" if not confirm_delete else "applying",
            "brain_id": brain_id,
            "node_ids": target_node_ids,
            "preview_id": preview_id,
            "before_brain_revision": before_revision,
            "expected_brain_revision": before_revision,
            "mutates_memory": False,
            "can_apply_now": not blocked_reasons,
            "blocked_reasons": blocked_reasons,
            "missing_node_ids": missing_ids,
            "protected_node_ids": protected_ids,
            "nodes": [_compact_node_for_mcp(node) for node in existing_nodes],
            "edges_to_remove": removed_edge_count,
            "apply_contract": {
                "preview_required": False,
                "explicit_confirm_required": True,
                "confirm_delete_required": True,
                "expected_brain_revision_recommended": True,
                "document_anchor_delete_requires_allow_document_anchor_delete": True,
            },
            "mcp_latency_profile": {"elapsed_ms": int((time.perf_counter() - started) * 1000)},
        }
        if blocked_reasons or not confirm_delete:
            return preview

        expected_revision = _payload_text(payload, "expected_brain_revision", "before_brain_revision")
        if expected_revision and expected_revision != before_revision:
            return {
                **preview,
                "status": "blocked",
                "can_apply_now": False,
                "blocked_reasons": ["brain_revision_mismatch"],
                "received_expected_brain_revision": expected_revision,
                "current_brain_revision": before_revision,
            }
        receipts = _graph_receipts(graph)
        if idempotency_key:
            for receipt in receipts:
                if isinstance(receipt, dict) and receipt.get("idempotency_key") == idempotency_key:
                    return {
                        **preview,
                        "status": "applied",
                        "mutates_memory": True,
                        "replayed": True,
                        "apply_receipt": receipt,
                    }
        graph["nodes"] = [
            node
            for node in list(graph.get("nodes") or [])
            if not (isinstance(node, dict) and _memory_os_node_id(node) in deleted_id_set)
        ]
        graph["edges"] = [
            edge
            for edge in list(graph.get("edges") or [])
            if not (isinstance(edge, dict) and _edge_touches_node(edge, deleted_id_set))
        ]
        now = _utc_now()
        receipt = {
            "receipt_id": _node_mutation_id("delete-node", payload, before_revision, suffix="apply"),
            "idempotency_key": idempotency_key,
            "operation": "delete_node",
            "brain_id": brain_id,
            "node_ids": sorted(deleted_id_set),
            "deleted_at": now,
            "before_brain_revision": before_revision,
            "removed_edge_count": removed_edge_count,
            "reason": _payload_text(payload, "reason", "human_reason", "conflict_summary") or None,
            "search_id": _payload_text(payload, "search_id") or None,
            "conflict_id": _payload_text(payload, "conflict_id", "registry_key") or None,
        }
        receipts.append(receipt)
        graph["meta"]["node_mutation_receipts"] = receipts[-100:]
        saved_graph = replace_runtime_graph(graph)
        after_revision = maintenance_graph_revision(saved_graph)
        receipt["after_brain_revision"] = after_revision
        try:
            store_correction_history(
                correction_id=str(receipt["receipt_id"]),
                search_id=_payload_text(payload, "search_id") or None,
                query_text=_payload_text(payload, "query_text", "question"),
                returned_answer=_payload_text(payload, "returned_answer", "answer"),
                correction_text=_payload_text(payload, "reason", "human_reason", "conflict_summary") or "Deleted selected node from the active brain.",
                correction_mode="delete_node",
                used_evidence_node_ids=_payload_text_list(payload, "evidence_node_ids", "used_evidence_node_ids"),
                target_node_ids=sorted(deleted_id_set),
                action_summary=receipt,
            )
        except Exception as exc:  # pragma: no cover - ledger failure must not hide the committed graph mutation.
            receipt["history_warning"] = str(exc)
        search_id = _payload_text(payload, "search_id")
        if search_id:
            append_search_event(search_id, "node_deleted", {"receipt": receipt})
        return {
            **preview,
            "status": "applied",
            "mutates_memory": True,
            "after_brain_revision": after_revision,
            "apply_receipt": receipt,
            "mcp_latency_profile": {"elapsed_ms": int((time.perf_counter() - started) * 1000)},
        }


def _memory_os_add_item(
    items: list[dict[str, Any]],
    seen: set[str],
    item: dict[str, Any],
    *,
    prefix: str,
) -> None:
    payload = dict(item)
    item_id = str(payload.get("item_id") or "").strip() or _memory_os_stable_id(prefix, payload)
    if item_id in seen:
        return
    seen.add(item_id)
    payload["item_id"] = item_id
    items.append(payload)


def _memory_os_recent_result(session: dict[str, Any]) -> dict[str, Any]:
    return _memory_os_as_dict(session.get("result"))


def _memory_os_iter_recent_proposals(
    maintenance_runs: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], str | None]]:
    pairs: list[tuple[dict[str, Any], str | None]] = []
    for run in maintenance_runs:
        report = _memory_os_as_dict(run.get("report"))
        maintenance_id = (
            str(
                run.get("maintenance_id")
                or _memory_os_as_dict(report.get("self_improvement_loop")).get("maintenance_id")
                or ""
            ).strip()
            or None
        )
        for proposal in _memory_os_as_list(report.get("maintenance_proposals")):
            if isinstance(proposal, dict):
                pairs.append((dict(proposal), maintenance_id))
    return pairs


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _resolve_brain_record(brain_id: str | None = None) -> dict[str, Any]:
    try:
        return resolve_brain_scope(brain_id=str(brain_id or "").strip() or None)
    except BrainRegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _resolve_bootstrap_ready_brain_record(brain_id: str | None = None) -> dict[str, Any]:
    brain_record = _resolve_brain_record(brain_id)
    if _brain_record_bootstrap_ready_for_mutation(brain_record):
        return brain_record
    raise HTTPException(
        status_code=409,
        detail={
            "code": "brain_bootstrap_required",
            "message": "Complete Brain Bootstrap before using Grow, Sleep or Evolve.",
            "brain_id": _brain_record_id(brain_record) or None,
        },
    )


def _brain_record_bootstrap_ready_for_mutation(brain_record: dict[str, Any]) -> bool:
    if int(brain_record.get("node_count") or 0) > 0:
        return True
    lifecycle = dict(brain_record.get("lifecycle") or {})
    capabilities = dict(brain_record.get("capabilities") or {})
    return (
        str(lifecycle.get("bootstrap_state") or "").strip().lower() == "applied"
        and str(brain_record.get("storage_path") or "").strip() != ""
        and capabilities.get("grow") is True
    )


def _brain_record_id(record: dict[str, Any]) -> str:
    return str(record.get("brain_id") or record.get("id") or "").strip()


def _grow_runtime_scope_is_implicit_default(
    runtime_scope: str,
    *,
    top_level: str,
    options_scope: str,
) -> bool:
    if not runtime_scope or top_level or options_scope:
        return False
    try:
        default_record = resolve_brain_scope(brain_id=None)
    except BrainRegistryError:
        return runtime_scope == "default_brain"
    return _brain_record_id(default_record) == runtime_scope


def _resolve_grow_request_scope(
    payload: McpGrowSourceRequest,
) -> tuple[dict[str, Any], McpGrowSourceRequest]:
    """Bind Grow to the actual request runtime before provider/Search work.

    ``options.brain_id`` is a legacy assertion, never routing authority.  HTTP
    header/query scope is already represented by ``current_brain_id()`` in the
    public middleware.  Trusted source packages may repeat the scope but may
    not redirect it.
    """

    top_level = str(payload.brain_id or "").strip()
    options_scope = str(payload.options.brain_id or "").strip()
    runtime_scope = str(current_brain_id() or "").strip()
    implicit_default_runtime_scope = _grow_runtime_scope_is_implicit_default(
        runtime_scope,
        top_level=top_level,
        options_scope=options_scope,
    )
    authoritative = top_level or (None if implicit_default_runtime_scope else runtime_scope) or None
    brain_record = _resolve_bootstrap_ready_brain_record(authoritative)
    resolved = _brain_record_id(brain_record)
    assertions = {
        "top_level": top_level,
        "options": options_scope,
    }
    if runtime_scope and not implicit_default_runtime_scope:
        assertions["http_runtime"] = runtime_scope
    mismatches = {
        source: value
        for source, value in assertions.items()
        if value and value != resolved
    }
    if mismatches:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "grow_brain_scope_mismatch",
                "resolved_brain_id": resolved,
                "mismatches": mismatches,
            },
        )
    return brain_record, payload.model_copy(update={"brain_id": resolved})


def _input_mode(payload: McpGrowSourceRequest) -> str:
    kind = str(payload.input_kind or "auto")
    return "document" if kind in {"pdf", "docx", "website", "url", "transcript", "mixed_bundle"} else "manual"


def _source_type(payload: McpGrowSourceRequest) -> str:
    options = payload.options
    if payload.input_kind in {"website", "url"}:
        return "public_web_metadata" if options.metadata_only else "external_reference"
    if payload.input_kind in {"pdf", "docx", "transcript", "mixed_bundle"}:
        return "uploaded_document"
    return str(options.treat_as or "self_memory")


def _selected_preview_ids(bundle: dict[str, Any], payload_ids: list[str]) -> list[str]:
    ids = _normalized_grow_ids(payload_ids)
    if ids:
        return ids
    primary_id = str(dict(bundle.get("primary_node_preview") or {}).get("id") or "")
    derived_ids = [str(dict(node).get("id") or "") for node in list(bundle.get("derived_nodes") or [])]
    return _normalized_grow_ids([primary_id, *derived_ids])


def _normalized_grow_ids(values: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in list(values or []):
        node_id = str(value or "").strip()
        if not node_id or node_id in seen:
            continue
        seen.add(node_id)
        normalized.append(node_id)
    return normalized


def _maintenance_feedback_packets_from_engine_result(
    engine_result: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        deepcopy(dict(item))
        for item in list(engine_result.get("maintenance_feedback") or [])
        if isinstance(item, dict)
    ]


def _maintenance_deferred_source_contract(
    investigation_id: str,
    maintenance_feedback_packets: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": "agvm.core_source_formation_contract.v3",
        "mode": "grow_ai_investigation",
        "state": "maintenance_deferred",
        "mutates_memory": False,
        "investigation_id": investigation_id,
        "maintenance_feedback_count": len(maintenance_feedback_packets),
        "apply_contract": {
            "preview_required": False,
            "explicit_confirm_apply_required": False,
            "explicit_selection_required": False,
            "apply_without_preview_allowed": False,
            "can_apply_now": False,
            "blocked_reasons": ["maintenance_feedback_deferred"],
            "selected_preview_ids": [],
        },
    }


def _grow_response_from_maintenance_deferred(
    tool_name: str,
    stored: dict[str, Any],
    *,
    started: float,
    resume_token: str | None = None,
) -> McpGrowToolExecutionResponse:
    investigation_id = str(stored.get("investigation_id") or "").strip()
    packets = [
        deepcopy(dict(item))
        for item in list(stored.get("maintenance_feedback_packets") or [])
        if isinstance(item, dict)
    ]
    source_contract = deepcopy(dict(stored.get("source_formation_contract") or {}))
    if not source_contract:
        source_contract = _maintenance_deferred_source_contract(investigation_id, packets)
    investigation = deepcopy(dict(stored.get("investigation") or {}))
    return McpGrowToolExecutionResponse(
        schema_version="agvm.mcp_grow_tool_output.v1",
        brain_id=str(stored.get("brain_id") or "") or None,
        investigation_id=investigation_id or None,
        tool_name=tool_name,
        status="needs_review",
        source_investigation=deepcopy(dict(stored.get("source_investigation") or {})),
        source_formation_contract=source_contract,
        memory_operation_lifecycle_contract={
            "schema_version": "agvm.memory_operation_lifecycle_contract.v3",
            "operation": "grow",
            "phase": "maintenance_deferred",
            "mutates_memory": False,
            "next_action": "route maintenance_feedback_packets to maintenance; Grow apply is unavailable",
        },
        preview_bundle=None,
        maintenance_feedback_packets=packets,
        clarification_questions=[],
        ai_execution_attestation=deepcopy(dict(investigation.get("ai_execution_attestation") or {})),
        ai_execution_ledger=[
            deepcopy(dict(item))
            for item in list(investigation.get("ai_execution_ledger") or [])
            if isinstance(item, dict)
        ],
        investigation=investigation,
        investigation_session=deepcopy(dict(stored.get("investigation_session") or {})),
        resume_token=resume_token,
        investigation_version=(int(stored.get("version") or 0) or None),
        usage=deepcopy(
            dict(investigation.get("usage") or investigation.get("aggregate_usage") or {})
        ),
        completeness={
            "preview_generated": False,
            "investigation_complete": bool(investigation.get("complete")),
            "investigation_applicable": bool(investigation.get("applicable")),
            "maintenance_feedback_count": len(packets),
            "apply_ready": False,
        },
        mcp_latency_profile={"elapsed_ms": int((time.perf_counter() - started) * 1000)},
        budget={"credits_required": 0, "runtime": "local_core"},
        next_action="route maintenance_feedback_packets to maintenance; Grow apply is unavailable",
    )


def _authenticated_v3_grow_record_or_response(
    *,
    tool_name: str,
    brain_record: dict[str, Any],
    brain_id: str,
    investigation_id: str,
    resume_token: str | None,
    started: float,
) -> dict[str, Any] | McpGrowToolExecutionResponse:
    if not resume_token:
        return _grow_blocked(tool_name, brain_id, "resume_token_required", started)
    try:
        with use_runtime_brain(brain_record):
            authenticated = fetch_grow_investigation(
                brain_id=brain_id,
                investigation_id=investigation_id,
                resume_token=resume_token,
            )
    except GrowPreviewBindingStoreError as exc:
        return _grow_blocked(
            tool_name,
            brain_id,
            str(getattr(exc, "code", "") or exc),
            started,
        )
    if not authenticated:
        return _grow_blocked(tool_name, brain_id, "server_preview_not_found", started)
    return dict(authenticated)


def _grow_investigation_receipt_node_ids(investigation: dict[str, Any]) -> set[str]:
    node_ids: set[str] = set()
    for raw_receipt in list(investigation.get("search_receipts") or []):
        receipt = dict(raw_receipt or {})
        for value in list(
            receipt.get("evidence_node_ids")
            or receipt.get("node_ids")
            or receipt.get("matched_node_ids")
            or []
        ):
            if str(value).strip():
                node_ids.add(str(value).strip())
        for raw_reference in list(
            receipt.get("evidence_references")
            or receipt.get("node_references")
            or receipt.get("matches")
            or []
        ):
            reference = dict(raw_reference or {})
            node_id = str(reference.get("node_id") or reference.get("id") or "").strip()
            if node_id:
                node_ids.add(node_id)
    return node_ids


def _grow_contract_fingerprint(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _grow_apply_material(
    *,
    investigation_id: str,
    brain_id: str,
    preview_fingerprint: str,
    selected_preview_ids: list[str],
    payload: McpGrowApplyRequest,
    investigation_sha256: str = "",
) -> dict[str, Any]:
    return {
        "investigation_id": investigation_id,
        "brain_id": brain_id,
        "preview_fingerprint": preview_fingerprint,
        "selected_preview_ids": sorted(selected_preview_ids),
        "learning_mode": payload.learning_mode,
        "clarification_answers": payload.clarification_answers,
        "approved_preview_ids": sorted(_normalized_grow_ids(payload.approved_preview_ids)),
        "question_limit": payload.question_limit,
        "investigation_sha256": str(investigation_sha256 or ""),
    }


def _grow_apply_fingerprint(
    *,
    investigation_id: str,
    brain_id: str,
    preview_fingerprint: str,
    selected_preview_ids: list[str],
    payload: McpGrowApplyRequest,
    investigation_sha256: str = "",
) -> str:
    return _grow_contract_fingerprint(
        _grow_apply_material(
            investigation_id=investigation_id,
            brain_id=brain_id,
            preview_fingerprint=preview_fingerprint,
            selected_preview_ids=selected_preview_ids,
            payload=payload,
            investigation_sha256=investigation_sha256,
        )
    )


def _grow_idempotent_replay(
    tool_name: str,
    receipt: dict[str, Any],
    *,
    apply_fingerprint: str,
    started: float,
) -> McpGrowToolExecutionResponse | None:
    if str(receipt.get("apply_fingerprint") or "") != apply_fingerprint:
        return None
    response_payload = deepcopy(dict(receipt.get("response") or {}))
    if not response_payload:
        return None
    response_payload["tool_name"] = tool_name
    persist_result = dict(response_payload.get("persist_result") or {})
    persist_result["idempotent_replay"] = True
    response_payload["persist_result"] = persist_result
    response_payload.setdefault(
        "persisted_node_ids",
        _normalized_grow_ids(persist_result.get("persisted_node_ids") or []),
    )
    response_payload.setdefault(
        "persisted_edge_count",
        int(persist_result.get("persisted_edge_count") or 0),
    )
    response_payload.setdefault(
        "merged_into_existing_ids",
        _normalized_grow_ids(persist_result.get("merged_into_existing_ids") or []),
    )
    response_payload.setdefault(
        "selected_preview_ids",
        _normalized_grow_ids(persist_result.get("selected_preview_ids") or []),
    )
    response_payload.setdefault("receipt_id", persist_result.get("receipt_id"))
    signed_receipt = dict(persist_result.get("signed_apply_receipt") or {})
    if signed_receipt:
        response_payload.setdefault("apply_receipt", signed_receipt)
        response_payload.setdefault("receipt_signature", signed_receipt.get("signature"))
    response_payload.setdefault("brain_revision_before", persist_result.get("before_brain_revision"))
    response_payload.setdefault("brain_revision_after", persist_result.get("after_brain_revision"))
    completeness = dict(response_payload.get("completeness") or {})
    completeness["idempotent_replay"] = True
    response_payload["completeness"] = completeness
    lifecycle = dict(response_payload.get("memory_operation_lifecycle_contract") or {})
    lifecycle.update(
        {
            "phase": "applied",
            "receipt_id": receipt.get("receipt_id"),
            "idempotent_replay": True,
        }
    )
    response_payload["memory_operation_lifecycle_contract"] = lifecycle
    response_payload["mcp_latency_profile"] = {"elapsed_ms": int((time.perf_counter() - started) * 1000)}
    return McpGrowToolExecutionResponse(**response_payload)


def _grow_response_from_durable_apply(
    tool_name: str,
    stored: dict[str, Any],
    apply_result: dict[str, Any],
    *,
    started: float,
) -> McpGrowToolExecutionResponse:
    signed_receipt = dict(apply_result.get("signed_apply_receipt") or {})
    receipt_id = str(signed_receipt.get("receipt_id") or "")
    idempotent_replay = bool(apply_result.get("idempotent_replay"))
    source_investigation = {
        **dict(stored.get("source_investigation") or {}),
        "investigation_id": str(stored.get("investigation_id") or ""),
        "status": "applied",
        "applied_at": signed_receipt.get("applied_at"),
    }
    source_formation_contract = {
        **dict(stored.get("source_formation_contract") or {}),
        "state": "applied",
        "mutates_memory": True,
    }
    persisted_node_ids = _normalized_grow_ids(apply_result.get("persisted_node_ids") or [])
    selected_preview_ids = _normalized_grow_ids(apply_result.get("selected_preview_ids") or [])
    persisted_edge_count = int(apply_result.get("persisted_edge_count") or 0)
    merged_into_existing_ids = _normalized_grow_ids(
        apply_result.get("merged_into_existing_ids") or []
    )
    return McpGrowToolExecutionResponse(
        schema_version="agvm.mcp_grow_tool_output.v1",
        brain_id=str(stored.get("brain_id") or ""),
        investigation_id=str(stored.get("investigation_id") or "") or None,
        tool_name=tool_name,
        status="applied",
        can_apply_now=False,
        selected_preview_ids=selected_preview_ids,
        receipt_id=receipt_id or None,
        receipt_signature=signed_receipt.get("signature"),
        persisted_node_ids=persisted_node_ids,
        persisted_edge_count=persisted_edge_count,
        merged_into_existing_ids=merged_into_existing_ids,
        brain_revision_before=apply_result.get("before_brain_revision"),
        brain_revision_after=apply_result.get("after_brain_revision"),
        apply_receipt=signed_receipt,
        source_investigation=source_investigation,
        source_formation_contract=source_formation_contract,
        memory_operation_lifecycle_contract={
            "schema_version": "agvm.memory_operation_lifecycle_contract.v2",
            "operation": "grow",
            "phase": "applied",
            "confirm_apply": True,
            "partial_merge_allowed": False,
            "receipt_id": receipt_id,
            "receipt_signature": signed_receipt.get("signature"),
            "idempotent_replay": idempotent_replay,
        },
        preview_bundle=deepcopy(dict(stored.get("preview_bundle") or {})),
        maintenance_feedback_packets=[
            deepcopy(dict(item))
            for item in list(stored.get("maintenance_feedback_packets") or [])
            if isinstance(item, dict)
        ],
        ai_execution_attestation=deepcopy(dict(stored.get("ai_execution_attestation") or {})),
        investigation_session=deepcopy(dict(stored.get("investigation_session") or {})),
        persist_result={
            "schema_version": "agvm.core_grow_persist_result.v2",
            "persisted_node_ids": persisted_node_ids,
            "persisted_edge_count": persisted_edge_count,
            "merged_into_existing_ids": merged_into_existing_ids,
            "selected_preview_ids": selected_preview_ids,
            "idempotent_replay": idempotent_replay,
            "receipt_id": receipt_id,
            "signed_apply_receipt": signed_receipt,
            "before_brain_revision": apply_result.get("before_brain_revision"),
            "after_brain_revision": apply_result.get("after_brain_revision"),
        },
        learning_policy=dict(apply_result.get("learning_policy") or {}),
        completeness={
            "applied": True,
            "persisted_node_count": len(persisted_node_ids),
            "persisted_edge_count": persisted_edge_count,
            "idempotent_replay": idempotent_replay,
            "source_unit_count": len(list(source_investigation.get("source_units") or [])),
        },
        mcp_latency_profile={"elapsed_ms": int((time.perf_counter() - started) * 1000)},
        budget={"credits_required": 0, "runtime": "local_core"},
    )


def _grow_response_from_durable_rollback(
    tool_name: str,
    stored: dict[str, Any],
    rollback_result: dict[str, Any],
    *,
    started: float,
) -> McpGrowToolExecutionResponse:
    signed_receipt = dict(rollback_result.get("signed_rollback_receipt") or {})
    idempotent_replay = bool(rollback_result.get("idempotent_replay"))
    return McpGrowToolExecutionResponse(
        schema_version="agvm.mcp_grow_tool_output.v1",
        brain_id=str(stored.get("brain_id") or ""),
        investigation_id=str(stored.get("investigation_id") or "") or None,
        tool_name=tool_name,
        status="rolled_back",
        source_investigation={
            **dict(stored.get("source_investigation") or {}),
            "investigation_id": str(stored.get("investigation_id") or ""),
            "status": "rolled_back",
            "rolled_back_at": signed_receipt.get("rolled_back_at"),
        },
        source_formation_contract={
            **dict(stored.get("source_formation_contract") or {}),
            "state": "rolled_back",
            "mutates_memory": True,
        },
        memory_operation_lifecycle_contract={
            "schema_version": "agvm.memory_operation_lifecycle_contract.v2",
            "operation": "grow_rollback",
            "phase": "rolled_back",
            "confirm_rollback": True,
            "receipt_id": signed_receipt.get("receipt_id"),
            "receipt_signature": signed_receipt.get("signature"),
            "idempotent_replay": idempotent_replay,
        },
        preview_bundle=deepcopy(dict(stored.get("preview_bundle") or {})),
        maintenance_feedback_packets=[
            deepcopy(dict(item))
            for item in list(stored.get("maintenance_feedback_packets") or [])
            if isinstance(item, dict)
        ],
        ai_execution_attestation=deepcopy(dict(stored.get("ai_execution_attestation") or {})),
        investigation_session=deepcopy(dict(stored.get("investigation_session") or {})),
        persist_result={
            "schema_version": "agvm.core_grow_rollback_result.v1",
            **deepcopy(rollback_result),
            "idempotent_replay": idempotent_replay,
        },
        completeness={
            "rolled_back": True,
            "idempotent_replay": idempotent_replay,
            "restored_brain_revision": rollback_result.get("rollback_to_brain_revision"),
        },
        mcp_latency_profile={"elapsed_ms": int((time.perf_counter() - started) * 1000)},
        budget={"credits_required": 0, "runtime": "local_core"},
    )


def _local_core_source_unit(payload: McpGrowSourceRequest, investigation_id: str) -> dict[str, Any]:
    raw_text = str(payload.raw_input or "").strip()
    source_unit_id = f"src_{investigation_id.removeprefix('mcp-grow-')}"
    content_digest = f"sha256:{hashlib.sha256(raw_text.encode('utf-8')).hexdigest()}"
    acquired_at = datetime.now(timezone.utc).isoformat()
    return {
        "unit_id": source_unit_id,
        "kind": "manual_block",
        "title": str(payload.source_label or "Local Grow source"),
        "source_uri": None,
        "source_type": "manual_text",
        "raw_text": raw_text,
        "char_count": len(raw_text),
        "token_estimate": max(1, (len(raw_text) + 3) // 4),
        "confidence": 0.96,
        "fact_eligible": True,
        "status": "available",
        "content_digest": content_digest,
        "acquired_at": acquired_at,
        "acquisition_method": "manual_text_passthrough",
        "acquisition_proof": {
            "verified": True,
            "kind": "manual_input",
            "source_uri": None,
            "content_digest": content_digest,
            "acquired_at": acquired_at,
            "method": "manual_text_passthrough",
        },
        "provenance": {
            "source_label": str(payload.source_label or "Local Grow source"),
            "source_type": "manual_text",
            "hash": content_digest,
            "retrieved_at": acquired_at,
        },
        "extraction_trace": {"stage": "extracting_text", "method": "manual_text_passthrough"},
    }


def _grow_payload_is_external_source(payload: McpGrowSourceRequest) -> bool:
    input_kind = str(payload.input_kind or "auto").strip().lower()
    source_uri = str(payload.source_uri or "").strip().lower()
    raw_input = str(payload.raw_input or "").strip().lower()
    return input_kind in {"url", "website"} or source_uri.startswith(("http://", "https://")) or (
        input_kind == "auto" and raw_input.startswith(("http://", "https://"))
    )


def _grow_unit_has_fetched_acquisition_proof(unit: dict[str, Any]) -> bool:
    proof = dict(unit.get("acquisition_proof") or {})
    provenance = dict(unit.get("provenance") or {})
    trace = dict(unit.get("extraction_trace") or {})
    source_uri = str(unit.get("source_uri") or proof.get("source_uri") or "").strip()
    digest = str(unit.get("content_digest") or proof.get("content_digest") or provenance.get("hash") or "").strip()
    acquired_at = str(unit.get("acquired_at") or proof.get("acquired_at") or provenance.get("retrieved_at") or "").strip()
    method = str(unit.get("acquisition_method") or proof.get("method") or trace.get("method") or "").strip()
    fetched = bool(proof.get("verified") and proof.get("kind") == "fetched_source") or bool(trace.get("fetched"))
    return fetched and source_uri.startswith(("http://", "https://")) and bool(digest and acquired_at and method)


def _trusted_source_unit_is_uploaded_file(unit: dict[str, Any]) -> bool:
    provenance = dict(unit.get("provenance") or {})
    trace = dict(unit.get("extraction_trace") or {})
    source_type = str(unit.get("source_type") or provenance.get("source_type") or "").strip()
    kind = str(unit.get("kind") or "").strip()
    if source_type.startswith("uploaded_") or source_type in {"uploaded_document", "uploaded_document_preview"}:
        return True
    return bool(
        kind in {"document_page", "document_section", "ocr_block"}
        and str(trace.get("file_name") or "").strip()
        and str(trace.get("file_hash") or provenance.get("file_hash") or "").strip()
    )


def _trusted_source_payload_package(payload: McpGrowSourceRequest) -> dict[str, Any] | None:
    package = getattr(payload, "trusted_source_investigation", None)
    if not package:
        return None
    if not isinstance(package, dict):
        raise TrustedSourcePackageError("trusted_source_package_invalid:package_must_be_object")
    return deepcopy(package)


def _trusted_source_fail(code: str, package: dict[str, Any] | None = None) -> None:
    raise TrustedSourcePackageError(f"trusted_source_package_invalid:{code}", source_package=package)


def _is_http_url(value: Any) -> bool:
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _trusted_source_url_key(value: Any) -> str:
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    path = parsed.path or "/"
    normalized = parsed._replace(scheme=parsed.scheme.lower(), netloc=parsed.netloc.lower(), path=path, fragment="")
    return normalized.geturl().rstrip("/")


def _trusted_source_digest(value: Any) -> str:
    text = str(value or "").strip()
    return text.removeprefix("sha256:").strip().lower()


def _trusted_source_raw_text(unit: dict[str, Any]) -> str:
    return str(unit.get("raw_text") or unit.get("text") or "").strip()


def _trusted_source_unit_requires_fetched_proof(unit: dict[str, Any]) -> bool:
    provenance = dict(unit.get("provenance") or {})
    kind = str(unit.get("kind") or "").strip()
    source_type = str(unit.get("source_type") or provenance.get("source_type") or "").strip()
    if _trusted_source_unit_is_uploaded_file(unit):
        return False
    return kind in {"web_page", "web_section"} or source_type in TRUSTED_SOURCE_WEB_PROVENANCE_TYPES or _is_http_url(
        unit.get("source_uri")
    )


def _trusted_source_package_url_keys(package: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    source_request = dict(package.get("source_request") or {})
    for value in [package.get("source_uri"), source_request.get("source_uri"), source_request.get("raw_input")]:
        key = _trusted_source_url_key(value)
        if key:
            keys.add(key)
    source_detection = dict(package.get("source_detection") or {})
    for value in list(source_detection.get("urls") or []):
        key = _trusted_source_url_key(value)
        if key:
            keys.add(key)
    for unit in list(package.get("source_units") or []):
        if isinstance(unit, dict):
            key = _trusted_source_url_key(unit.get("source_uri"))
            if key:
                keys.add(key)
    return keys


def _trusted_source_payload_url_keys(payload: McpGrowSourceRequest) -> set[str]:
    keys: set[str] = set()
    for value in [payload.source_uri, payload.raw_input]:
        key = _trusted_source_url_key(value)
        if key:
            keys.add(key)
    return keys


def _validate_trusted_source_unit(unit: dict[str, Any], package: dict[str, Any], index: int) -> bool:
    unit_id = str(unit.get("unit_id") or "").strip()
    if not unit_id:
        _trusted_source_fail(f"source_unit_{index}_missing_unit_id", package)

    raw_text = _trusted_source_raw_text(unit)
    fact_eligible = bool(unit.get("fact_eligible", True))
    if fact_eligible and not raw_text:
        _trusted_source_fail(f"source_unit_{unit_id}_missing_raw_text", package)

    expected_digest = hashlib.sha256(raw_text.encode("utf-8")).hexdigest() if raw_text else ""
    content_digest = _trusted_source_digest(unit.get("content_digest"))
    if fact_eligible and not content_digest:
        _trusted_source_fail(f"source_unit_{unit_id}_missing_content_digest", package)
    if fact_eligible and content_digest != expected_digest:
        _trusted_source_fail(f"source_unit_{unit_id}_content_digest_mismatch", package)

    provenance = dict(unit.get("provenance") or {})
    if fact_eligible and not provenance:
        _trusted_source_fail(f"source_unit_{unit_id}_missing_provenance", package)
    provenance_hash = _trusted_source_digest(provenance.get("hash"))
    if fact_eligible and provenance_hash != content_digest:
        _trusted_source_fail(f"source_unit_{unit_id}_provenance_hash_mismatch", package)
    if fact_eligible and not str(provenance.get("source_type") or unit.get("source_type") or "").strip():
        _trusted_source_fail(f"source_unit_{unit_id}_missing_provenance_source_type", package)

    if _trusted_source_unit_requires_fetched_proof(unit):
        proof = dict(unit.get("acquisition_proof") or {})
        trace = dict(unit.get("extraction_trace") or {})
        unit_url = str(unit.get("source_uri") or "").strip()
        proof_url = str(proof.get("source_uri") or "").strip()
        if not _is_http_url(unit_url):
            _trusted_source_fail(f"source_unit_{unit_id}_missing_http_source_uri", package)
        if not proof:
            _trusted_source_fail(f"source_unit_{unit_id}_missing_acquisition_proof", package)
        if proof.get("verified") is not True or proof.get("kind") != "fetched_source":
            _trusted_source_fail(f"source_unit_{unit_id}_unverified_acquisition_proof", package)
        if _trusted_source_url_key(proof_url) != _trusted_source_url_key(unit_url):
            _trusted_source_fail(f"source_unit_{unit_id}_acquisition_source_uri_mismatch", package)
        if _trusted_source_digest(proof.get("content_digest")) != content_digest:
            _trusted_source_fail(f"source_unit_{unit_id}_acquisition_digest_mismatch", package)
        if not str(proof.get("acquired_at") or "").strip():
            _trusted_source_fail(f"source_unit_{unit_id}_missing_acquired_at", package)
        if not str(proof.get("method") or "").strip():
            _trusted_source_fail(f"source_unit_{unit_id}_missing_acquisition_method", package)
        if trace.get("fetched") is not True:
            _trusted_source_fail(f"source_unit_{unit_id}_missing_fetched_trace", package)
        source_type = str(provenance.get("source_type") or unit.get("source_type") or "").strip()
        if source_type == "external_reference" and unit.get("primary_source_override_allowed") is not False:
            _trusted_source_fail(f"source_unit_{unit_id}_external_reference_override_not_disabled", package)

    return fact_eligible and bool(raw_text)


def _validate_trusted_source_package(
    package: dict[str, Any],
    payload: McpGrowSourceRequest,
    brain_id: str,
) -> dict[str, Any]:
    if package.get("schema_version") != "agvm.source_investigation.v1":
        _trusted_source_fail("unsupported_schema_version", package)

    contract = dict(package.get("trusted_source_runtime_contract") or {})
    if contract.get("schema_version") != TRUSTED_SOURCE_RUNTIME_CONTRACT_SCHEMA_VERSION:
        _trusted_source_fail("missing_runtime_contract", package)
    if contract.get("producer_module_id") != TRUSTED_SOURCE_RUNTIME_PRODUCER_MODULE_ID:
        _trusted_source_fail("invalid_producer_module", package)
    if contract.get("source_extraction_runtime_state") != TRUSTED_SOURCE_RUNTIME_BOUNDARY:
        _trusted_source_fail("invalid_runtime_boundary", package)
    if contract.get("handoff") != TRUSTED_SOURCE_RUNTIME_HANDOFF:
        _trusted_source_fail("invalid_handoff", package)
    if contract.get("single_core_grow_investigator_call") is not True:
        _trusted_source_fail("single_core_grow_investigator_call_required", package)

    package_brain_id = str(package.get("brain_id") or "").strip()
    if package_brain_id and package_brain_id != brain_id:
        _trusted_source_fail("brain_scope_mismatch", package)
    package_runtime_brain_id = str(dict(package.get("runtime") or {}).get("brain_id") or "").strip()
    package_request_brain_id = str(dict(package.get("source_request") or {}).get("brain_id") or "").strip()
    if (
        (package_runtime_brain_id and package_runtime_brain_id != brain_id)
        or (package_request_brain_id and package_request_brain_id != brain_id)
    ):
        _trusted_source_fail("brain_scope_mismatch", package)
    package["brain_id"] = brain_id

    source_units = [dict(unit) for unit in list(package.get("source_units") or []) if isinstance(unit, dict)]
    if not source_units:
        _trusted_source_fail("no_source_units", package)
    fact_unit_count = 0
    for index, unit in enumerate(source_units):
        if _validate_trusted_source_unit(unit, package, index):
            fact_unit_count += 1
    if fact_unit_count <= 0:
        _trusted_source_fail("no_fact_eligible_source_units", package)
    package["source_units"] = source_units

    handoff = dict(package.get("compiler_handoff") or {})
    handoff_schema = str(handoff.get("schema_version") or handoff.get("handoff_version") or "").strip()
    if handoff_schema != "agvm.compiler_handoff.v1":
        _trusted_source_fail("missing_compiler_handoff", package)
    handoff.setdefault("schema_version", handoff_schema)
    if not str(handoff.get("mega_text") or "").strip():
        _trusted_source_fail("missing_compiler_mega_text", package)
    structured_sections = [
        dict(item)
        for item in list(handoff.get("structured_sections") or [])
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    ]
    if not structured_sections:
        _trusted_source_fail("missing_compiler_structured_sections", package)
    package["compiler_handoff"] = {**handoff, "structured_sections": structured_sections}

    payload_url_keys = _trusted_source_payload_url_keys(payload)
    if payload_url_keys:
        package_url_keys = _trusted_source_package_url_keys(package)
        if not payload_url_keys.intersection(package_url_keys):
            _trusted_source_fail("source_url_mismatch", package)

    external_source = _grow_payload_is_external_source(payload) and any(
        _trusted_source_unit_requires_fetched_proof(unit) for unit in source_units
    )
    if external_source and not any(_grow_unit_has_fetched_acquisition_proof(unit) for unit in source_units):
        _trusted_source_fail("external_source_missing_fetched_acquisition_proof", package)

    runtime = dict(package.get("runtime") or {})
    runtime.update(
        {
            "kind": str(runtime.get("kind") or "local_module_private_source_runtime"),
            "rich_extraction_available": True,
            "trusted_source_package_accepted": True,
            "trusted_source_runtime_contract_schema_version": TRUSTED_SOURCE_RUNTIME_CONTRACT_SCHEMA_VERSION,
        }
    )
    package["runtime"] = runtime
    return package


def _grow_source_package(payload: McpGrowSourceRequest, brain_id: str) -> dict[str, Any]:
    trusted_package = _trusted_source_payload_package(payload)
    if trusted_package is not None:
        return _validate_trusted_source_package(trusted_package, payload, brain_id)

    options = _grow_source_package_options(payload)
    package = build_source_investigation_package(
        payload.raw_input,
        source_label=payload.source_label,
        source_uri=payload.source_uri,
        user_instruction=payload.user_instruction,
        input_kind=payload.input_kind,
        options=options,
    )
    package["brain_id"] = brain_id
    return package


def _grow_source_package_options(payload: McpGrowSourceRequest) -> dict[str, Any]:
    options = payload.options.model_dump(mode="python")
    explicit_options = set(getattr(payload.options, "model_fields_set", set()) or set())
    input_kind = str(payload.input_kind or "auto").strip().lower()
    crawl_explicit = bool({"crawl_sublinks", "crawl_scope"} & explicit_options)
    local_material_input = input_kind in {"manual_text", "transcript", "pdf", "docx", "mixed_bundle"}

    if local_material_input:
        # Local text/document inputs already carry their source material.  They
        # must not spend the interactive Grow budget on browser rendering or
        # online enrichment before the semantic investigator can ask questions
        # or return a bounded provider failure.  Callers can still opt into
        # enrichment explicitly when they are running a broader acquisition job.
        if not crawl_explicit:
            options.update(
                {
                    "crawl_scope": "off",
                    "crawl_sublinks": "off",
                    "follow_same_domain": False,
                    "explore_external_links": False,
                }
            )
        if "use_online_enrichment" not in explicit_options:
            options["use_online_enrichment"] = False
        if "max_online_queries" not in explicit_options and not bool(options.get("use_online_enrichment")):
            options["max_online_queries"] = 0
        if "use_browser_budget" not in explicit_options:
            options["use_browser_budget"] = False
        if "render_browser_pages" not in explicit_options:
            options["render_browser_pages"] = False
        if input_kind in {"manual_text", "transcript"} and "include_images" not in explicit_options:
            options["include_images"] = False

    if input_kind == "url" and not crawl_explicit:
        # A single URL is an acquisition request for that page.  Website crawl
        # must be an explicit caller choice; otherwise the interactive Grow
        # loop can spend its whole budget discovering links before it returns a
        # preview.
        options.update(
            {
                "crawl_scope": "off",
                "crawl_sublinks": "off",
                "follow_same_domain": False,
                "explore_external_links": False,
                "max_crawl_pages": 1,
                "max_pages": 1,
                "crawl_time_budget_seconds": min(
                    float(options.get("crawl_time_budget_seconds") or 15.0),
                    15.0,
                ),
            }
        )
    elif input_kind == "website" and not crawl_explicit:
        # Website mode is bounded same-domain by default.  External expansion
        # remains available only when a caller asks for it explicitly.
        options.update(
            {
                "crawl_scope": "same_domain",
                "crawl_sublinks": "same_domain",
                "explore_external_links": False,
            }
        )

    crawl_requested = str(options.get("crawl_sublinks") or "off") not in {"", "off"}
    if input_kind in {"url", "website"} and crawl_requested:
        if (
            "max_units" in explicit_options
            and "max_pages" not in explicit_options
            and "max_crawl_pages" not in explicit_options
        ):
            max_units = max(1, int(options.get("max_units") or 1))
            options["max_crawl_pages"] = max(1, min(int(options.get("max_crawl_pages") or max_units), max_units))
        if "crawl_time_budget_seconds" not in explicit_options:
            max_pages = max(1, int(options.get("max_crawl_pages") or options.get("max_pages") or 1))
            fetch_timeout = max(0.1, float(options.get("fetch_timeout_seconds") or 8.0))
            options["crawl_time_budget_seconds"] = min(
                float(options.get("crawl_time_budget_seconds") or 30.0),
                max(5.0, min(30.0, 2.0 + fetch_timeout * min(max_pages, 4))),
            )
    no_crawl_or_bounded_single_fetch = (
        input_kind not in {"url", "website"}
        or str(options.get("crawl_sublinks") or "off") in {"", "off"}
    )
    interactive_preview = bool(
        payload.run_preview
        and (
            bool(options.get("pause_on_questions"))
            or bool(options.get("semantic_preview"))
            or input_kind in {"manual_text", "transcript", "pdf", "docx", "mixed_bundle"}
        )
    )
    if (
        interactive_preview
        and no_crawl_or_bounded_single_fetch
        and "compiler_preview_timeout_seconds" not in explicit_options
    ):
        try:
            current_timeout = float(options.get("compiler_preview_timeout_seconds") or 45.0)
        except (TypeError, ValueError):
            current_timeout = 45.0
        options["compiler_preview_timeout_seconds"] = max(
            current_timeout,
            _GROW_INTERACTIVE_COMPILER_TIMEOUT_SECONDS,
        )
    return options


def _source_sections_for_grow(package: dict[str, Any]) -> list[dict[str, Any]]:
    handoff = dict(package.get("compiler_handoff") or {})
    structured = [dict(item) for item in list(handoff.get("structured_sections") or []) if isinstance(item, dict)]
    if structured:
        return structured
    return [
        {
            "section_id": str(unit.get("unit_id") or ""),
            "unit_id": str(unit.get("unit_id") or ""),
            "title": str(unit.get("title") or unit.get("unit_id") or "Source unit"),
            "kind": str(unit.get("kind") or "source_unit"),
            "text": str(unit.get("raw_text") or unit.get("text") or ""),
            "source_uri": unit.get("source_uri"),
            "source_unit_role": unit.get("source_unit_role"),
            "promotion_role": unit.get("promotion_role"),
            "fact_eligible": bool(unit.get("fact_eligible", True)),
            "content_digest": unit.get("content_digest"),
            "acquired_at": unit.get("acquired_at"),
            "acquisition_method": unit.get("acquisition_method"),
            "acquisition_proof": dict(unit.get("acquisition_proof") or {}),
            "provenance": dict(unit.get("provenance") or {}),
            "extraction_trace": dict(unit.get("extraction_trace") or {}),
        }
        for unit in list(package.get("source_units") or [])
        if isinstance(unit, dict) and str(unit.get("unit_id") or "").strip()
    ]


def _grow_source_preview_raw_input(
    source_package: dict[str, Any],
    payload: McpGrowSourceRequest,
) -> str:
    canonical_text, _sections = _document_canonical_text_and_sections(source_package)
    if str(canonical_text or "").strip():
        return str(canonical_text).strip()
    handoff = dict(source_package.get("compiler_handoff") or {})
    source_request = dict(source_package.get("source_request") or {})
    for candidate in (
        handoff.get("mega_text"),
        source_request.get("raw_input"),
        payload.raw_input,
    ):
        text = str(candidate or "").strip()
        if text:
            return text
    return ""


def _ensure_grow_source_preview_raw_input(
    source_package: dict[str, Any],
    payload: McpGrowSourceRequest,
) -> dict[str, Any]:
    package = deepcopy(dict(source_package or {}))
    raw_input = _grow_source_preview_raw_input(package, payload)
    if not raw_input:
        return package
    handoff = deepcopy(dict(package.get("compiler_handoff") or {}))
    handoff.setdefault("schema_version", "agvm.compiler_handoff.v1")
    handoff.setdefault("handoff_version", "agvm.compiler_handoff.v1")
    handoff["mega_text"] = raw_input
    handoff["mega_text_char_count"] = len(raw_input)
    package["compiler_handoff"] = handoff
    return package


def _grow_source_label_for_preview(
    payload: McpGrowSourceRequest,
    source_package: dict[str, Any],
) -> str | None:
    source_request = dict(source_package.get("source_request") or {})
    label = str(
        source_package.get("source_label")
        or source_request.get("source_label")
        or source_request.get("file_name")
        or payload.source_label
        or ""
    ).strip()
    return label or None


def _grow_source_uri_for_preview(
    payload: McpGrowSourceRequest,
    source_package: dict[str, Any],
) -> str | None:
    source_request = dict(source_package.get("source_request") or {})
    provenance = dict(source_package.get("provenance") or {})
    uri = str(
        source_package.get("source_uri")
        or source_request.get("source_uri")
        or provenance.get("source_uri")
        or payload.source_uri
        or ""
    ).strip()
    return uri or None


def _source_type_for_grow(
    payload: McpGrowSourceRequest,
    source_package: dict[str, Any],
) -> str:
    source_detection = dict(source_package.get("source_detection") or {})
    source_request = dict(source_package.get("source_request") or {})
    source_kind = str(
        source_detection.get("source_kind")
        or source_request.get("input_kind")
        or source_package.get("source_type")
        or ""
    ).strip()
    if source_kind in {"pdf", "docx", "file", "uploaded_document", "transcript", "mixed_bundle"}:
        return "uploaded_document"
    if source_kind in {"url", "website", "web_page", "public_web", "public_source_text"}:
        return "public_web_metadata" if payload.options.metadata_only else "external_reference"
    return _source_type(payload)


def _input_mode_for_grow(payload: McpGrowSourceRequest, package: dict[str, Any]) -> str:
    handoff = dict(package.get("compiler_handoff") or {})
    recommended = str(handoff.get("recommended_input_mode") or "").strip().lower()
    if recommended in {"auto", "manual", "document"}:
        return recommended
    return _input_mode(payload)


def _bind_preview_bundle_to_source_unit(bundle: dict[str, Any], source_unit: dict[str, Any]) -> dict[str, Any]:
    source_unit_id = str(source_unit.get("unit_id") or "")
    if not source_unit_id:
        return bundle
    bound = dict(bundle)
    primary = dict(bound.get("primary_node_preview") or {})
    if primary:
        if not primary.get("source_unit_id"):
            primary["source_unit_id"] = source_unit_id
        if not primary.get("source_unit_title"):
            primary["source_unit_title"] = source_unit.get("title")
        if not primary.get("source_unit_kind"):
            primary["source_unit_kind"] = source_unit.get("kind")
        bound["primary_node_preview"] = primary
    derived_nodes: list[dict[str, Any]] = []
    for raw_node in list(bound.get("derived_nodes") or []):
        node = dict(raw_node or {})
        if not node.get("source_unit_id"):
            node["source_unit_id"] = source_unit_id
        if not node.get("source_unit_title"):
            node["source_unit_title"] = source_unit.get("title")
        if not node.get("source_unit_kind"):
            node["source_unit_kind"] = source_unit.get("kind")
        derived_nodes.append(node)
    bound["derived_nodes"] = derived_nodes
    return bound


def _preview_bundle_from_engine_result(engine_result: dict[str, Any]) -> dict[str, Any]:
    for key in ("preview_bundle", "bundle", "preview"):
        candidate = engine_result.get(key)
        if hasattr(candidate, "model_dump"):
            candidate = candidate.model_dump(mode="python")
        if isinstance(candidate, dict) and candidate.get("primary_node_preview"):
            return dict(candidate)

    session = dict(engine_result.get("investigation_session") or {})
    for key in ("preview_bundle", "bundle", "preview"):
        candidate = session.get(key)
        if hasattr(candidate, "model_dump"):
            candidate = candidate.model_dump(mode="python")
        if isinstance(candidate, dict) and candidate.get("primary_node_preview"):
            return dict(candidate)
    return {}


def _grow_engine_result_can_publish_preview(
    engine_result: dict[str, Any],
    *,
    preview_bundle: dict[str, Any],
) -> bool:
    """Return true when an engine result is sufficient to enter preview_ready.

    Older Grow engines can return a valid preview bundle plus a sufficient
    investigation session without duplicating `complete/applicable` onto the
    investigation object.  Treat that as a successful preview bridge; keep hard
    failures and clarification-bearing results out of the apply path.
    """

    if not preview_bundle:
        return False
    status = str(engine_result.get("status") or "").strip().lower()
    if status in {"failed", "blocked", "error"}:
        return False
    if bool(engine_result.get("apply_ready") or engine_result.get("can_apply_now")):
        return True
    session = dict(engine_result.get("investigation_session") or {})
    session_status = str(session.get("status") or "").strip().lower()
    return session_status in {
        "sufficient",
        "preview_ready",
        "complete",
        "completed",
        "applicable",
    }


def _grow_preview_nodes_from_bundle(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    primary = dict(bundle.get("primary_node_preview") or {})
    if primary:
        nodes.append(primary)
    nodes.extend(
        dict(item)
        for item in list(bundle.get("derived_nodes") or [])
        if isinstance(item, dict)
    )
    return nodes


def _grow_preview_node_authority_digest(
    *,
    investigation_id: str,
    index: int,
    node: dict[str, Any],
) -> str:
    material = {
        "investigation_id": str(investigation_id or ""),
        "index": int(index),
        "preview_id": str(node.get("id") or ""),
        "raw_text": str(node.get("raw_text") or node.get("summary") or ""),
        "source_unit_id": str(node.get("source_unit_id") or ""),
        "document_anchor_id": str(node.get("document_anchor_id") or ""),
    }
    encoded = json.dumps(material, ensure_ascii=True, sort_keys=True, default=str).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()[:16]


def _grow_source_bound_preview_decision(node: dict[str, Any]) -> str:
    current = str(node.get("claim_decision") or "").strip()
    if current in {
        "new_memory",
        "enrich_existing",
        "evolve_existing",
        "contradicts_existing",
        "supersedes_existing",
        "delete_existing",
    }:
        return current
    return "new_memory"


def _grow_source_bound_preview_with_claim_authority(
    bundle: dict[str, Any],
    *,
    investigation_id: str,
    source_package: dict[str, Any],
) -> dict[str, Any]:
    """Attach minimal claim/decision authority to source-bound preview bundles.

    This bridge is only for preview bundles that already contain concrete nodes.
    It does not synthesize new facts; it assigns stable action identifiers needed
    by the Grow apply contract and keeps all mutations as create/source-bound
    unless the semantic investigator already supplied a stronger decision.
    """

    if not bundle:
        return {}
    raw_nodes = _grow_preview_nodes_from_bundle(bundle)
    if not raw_nodes:
        return bundle
    source_request = dict(source_package.get("source_request") or {})
    default_source_ref_id = f"source_ref::{investigation_id}"
    bound_nodes: list[dict[str, Any]] = []
    for index, raw_node in enumerate(raw_nodes):
        node = deepcopy(dict(raw_node))
        structural_node = bool(_grow_v3_compiler_structural_node(node))
        digest = _grow_preview_node_authority_digest(
            investigation_id=investigation_id,
            index=index,
            node=node,
        )
        preview_id = str(node.get("id") or "").strip() or (
            "preview_primary" if index == 0 else f"preview_node_{index}_{digest}"
        )
        claim_id = str(node.get("claim_id") or "").strip() or (
            f"{investigation_id}:claim:{index}:{digest}"
        )
        decision_id = str(node.get("decision_id") or "").strip() or (
            f"{investigation_id}:decision:{index}:{digest}"
        )
        decision = _grow_source_bound_preview_decision(node)
        target_node_ids = [
            str(target_id).strip()
            for target_id in list(node.get("target_node_ids") or [])
            if str(target_id).strip()
        ]
        action = _grow_v3_materialized_action(decision, target_node_ids)
        provenance = deepcopy(dict(node.get("provenance") or {}))
        if not provenance.get("source_ref_id"):
            provenance["source_ref_id"] = default_source_ref_id
        if not provenance.get("source_label") and source_request.get("source_label"):
            provenance["source_label"] = source_request.get("source_label")
        if not provenance.get("source_trust"):
            provenance["source_trust"] = (
                node.get("source_trust")
                or dict(source_request.get("options") or {}).get("source_trust")
                or "unknown"
            )
        raw_text = str(node.get("raw_text") or node.get("summary") or "").strip()
        basis_ref = (
            str(node.get("source_unit_id") or "").strip()
            or str(node.get("document_anchor_id") or "").strip()
            or preview_id
        )
        bound_nodes.append(
            {
                **node,
                "id": preview_id,
                "claim_id": claim_id,
                "decision_id": decision_id,
                "claim_decision": decision,
                "claim_binding_required": False
                if structural_node
                else bool(node.get("claim_binding_required", True)),
                "compiler_binding_role": "source_document_structure"
                if structural_node
                else str(node.get("compiler_binding_role") or "source_bound_claim"),
                "semantic_claim_authority": "not_applicable_source_structure"
                if structural_node
                else (
                    "source_bound_audit_substrate_only"
                    if str(node.get("source_bound_role") or "") == "atomic_claim_evidence"
                    else "provider_or_human_review_required"
                ),
                "target_node_ids": target_node_ids,
                "persist_mode": str(node.get("persist_mode") or action["persist_mode"]),
                "memory_act_type": str(node.get("memory_act_type") or action["memory_act_type"]),
                "cognitive_status": str(
                    node.get("cognitive_status")
                    or ("review_required" if action["high_impact"] else "ready")
                ),
                "requires_human_review": bool(
                    node.get("requires_human_review")
                    if node.get("requires_human_review") is not None
                    else action["high_impact"]
                ),
                "selected_by_default": bool(node.get("selected_by_default", True))
                and not bool(action["high_impact"]),
                "cognitive_review_reasons": list(node.get("cognitive_review_reasons") or []),
                "cognitive_target_node_ids": target_node_ids,
                "parent_claim_id": node.get("parent_claim_id"),
                "basis_kind": str(node.get("basis_kind") or node.get("memory_type") or "source_unit"),
                "basis_ref": basis_ref,
                "basis_content_sha256": str(
                    node.get("basis_content_sha256")
                    or (_sha256_text_ref(raw_text) if raw_text else "")
                ),
                "source_trust": str(node.get("source_trust") or provenance.get("source_trust") or "unknown"),
                "provenance": provenance,
            }
        )

    bound = deepcopy(dict(bundle))
    bound["primary_node_preview"] = bound_nodes[0]
    bound["derived_nodes"] = bound_nodes[1:]
    bound["investigation_authority"] = {
        "schema_version": "agvm.grow_compiler_authority.v1",
        "structural_node_count": sum(
            1
            for node in bound_nodes
            if str(node.get("compiler_binding_role") or "") == "source_document_structure"
            and bool(node.get("claim_binding_required")) is False
        ),
        "claim_decision_bindings": [
            {
                "preview_id": str(node.get("id") or ""),
                "claim_id": str(node.get("claim_id") or ""),
                "decision_id": str(node.get("decision_id") or ""),
                "claim_decision": str(node.get("claim_decision") or ""),
                "claim_binding_required": bool(node.get("claim_binding_required", True)),
                "binding_role": str(node.get("compiler_binding_role") or "source_bound_claim"),
                "semantic_claim_authority": str(
                    node.get("semantic_claim_authority") or "provider_or_human_review_required"
                ),
                "target_node_ids": list(node.get("target_node_ids") or []),
                "parent_claim_id": node.get("parent_claim_id"),
                "basis_kind": node.get("basis_kind"),
                "basis_ref": node.get("basis_ref"),
                "basis_content_sha256": node.get("basis_content_sha256"),
                "source_uri": dict(node.get("provenance") or {}).get("source_uri"),
                "source_ref_id": dict(node.get("provenance") or {}).get("source_ref_id"),
                "materialized_action": _grow_v3_materialized_action(
                    str(node.get("claim_decision") or ""),
                    list(node.get("target_node_ids") or []),
                ),
            }
            for node in bound_nodes
        ],
    }
    bound["cognitive_write_plan"] = _grow_v3_cognitive_write_plan(bound_nodes)
    return bound


_SOURCE_MATERIAL_NON_BLOCKING_QUESTION_CUES = {
    "source_scope",
    "source_trust",
    "source_investigation_scope",
    "web_source_scope_review",
    "document_purpose",
    "persistence_policy",
    "uncertain_claim_policy",
    "relationship_claim",
    "crawl_sublinks_off",
    "browser_budget_runtime_not_configured",
    "browser_rendering_not_configured",
    "browser_render_failed",
    "max_depth_reached",
    "max_crawl_pages_exhausted",
    "max_total_chars_exhausted",
    "max_online_queries_exhausted",
}

_SOURCE_ACQUISITION_QUESTION_CUES = {
    "source_acquisition_required",
    "url_crawl_scope",
    "crawl_scope",
    "crawl_depth",
    "source_url_required",
    "upload_required",
    "source_material_required",
    "file_required",
    "missing_source_text",
    "rich_extraction_required",
}

_SOURCE_REQUIRED_MATERIAL_QUESTION_CUES = {
    "source_url_required",
    "upload_required",
    "source_material_required",
    "file_required",
    "missing_source_text",
}


def _grow_source_question_haystack(question: dict[str, Any]) -> str:
    question_id = _grow_clarification_question_id(question).lower()
    reason = str(question.get("reason") or question.get("reason_code") or "").strip().lower()
    affects = str(question.get("affects") or "").strip().lower()
    category = str(question.get("category") or question.get("kind") or "").strip().lower()
    text = str(question.get("question") or question.get("question_text") or "").strip().lower()
    return " ".join([question_id, reason, affects, category, text])


def _grow_question_is_source_acquisition_question(question: dict[str, Any]) -> bool:
    haystack = _grow_source_question_haystack(question)
    return any(cue in haystack for cue in _SOURCE_ACQUISITION_QUESTION_CUES)


def _grow_identity_question_has_default_action(question: dict[str, Any]) -> bool:
    haystack = _grow_source_question_haystack(question)
    if not any(cue in haystack for cue in {"identity_ambiguity", "identity_project_disambiguation"}):
        return False
    return bool(
        str(
            question.get("default_action_if_unanswered")
            or question.get("default_action")
            or question.get("default_resolution")
            or ""
        ).strip()
    )


def _grow_question_requires_missing_source_material(question: dict[str, Any]) -> bool:
    haystack = _grow_source_question_haystack(question)
    return any(cue in haystack for cue in _SOURCE_REQUIRED_MATERIAL_QUESTION_CUES)


def _grow_source_package_has_source_material(source_package: dict[str, Any]) -> bool:
    """Return true when Core already has durable source text to compile.

    Source-reader questions are useful in the UI, but V3 Grow must not block a
    source-bound preview on generic legacy identity/trust questions when the
    source package already contains the text, provenance, and source units
    needed for a safe preview.  The semantic investigator can still ask later
    questions if they materially affect a claim.
    """

    for unit in list(source_package.get("source_units") or []):
        if not isinstance(unit, dict):
            continue
        if not bool(unit.get("fact_eligible", True)):
            continue
        raw_text = str(unit.get("raw_text") or unit.get("text") or "").strip()
        if len(raw_text) >= 40:
            return True
    handoff = dict(source_package.get("compiler_handoff") or {})
    return len(str(handoff.get("mega_text") or "").strip()) >= 40


def _grow_source_package_has_source_identity(source_package: dict[str, Any]) -> bool:
    source_request = dict(source_package.get("source_request") or {})
    provenance = dict(source_package.get("provenance") or {})
    for value in (
        source_package.get("source_uri"),
        source_package.get("source_label"),
        source_request.get("source_uri"),
        source_request.get("source_label"),
        provenance.get("source_uri"),
        provenance.get("source_label"),
    ):
        if str(value or "").strip():
            return True
    for unit in list(source_package.get("source_units") or []):
        if not isinstance(unit, dict):
            continue
        unit_provenance = dict(unit.get("provenance") or {})
        for value in (
            unit.get("source_uri"),
            unit.get("title"),
            unit_provenance.get("source_uri"),
            unit_provenance.get("source_label"),
            unit_provenance.get("source_type"),
        ):
            if str(value or "").strip():
                return True
    return False


def _grow_question_is_non_blocking_with_source_material(question: dict[str, Any]) -> bool:
    haystack = _grow_source_question_haystack(question)
    return any(cue in haystack for cue in _SOURCE_MATERIAL_NON_BLOCKING_QUESTION_CUES)


def _filter_non_blocking_grow_source_questions(
    questions: list[dict[str, Any]],
    *,
    source_material_ready: bool,
    source_identity_ready: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    retained: list[dict[str, Any]] = []
    defaulted: list[dict[str, Any]] = []
    for raw_question in questions:
        question = deepcopy(dict(raw_question))
        if _grow_question_is_source_acquisition_question(question):
            if not source_material_ready or _grow_question_requires_missing_source_material(question):
                retained.append(question)
                continue
        if not source_material_ready:
            question_id = _grow_clarification_question_id(question)
            question["question_id"] = question_id
            question["id"] = question_id
            question["status"] = "defaulted_non_blocking"
            question["default_resolution"] = (
                "source_material_missing; only acquisition questions may block before "
                "Grow has source text to investigate"
            )
            defaulted.append(question)
            continue
        if (
            source_material_ready
            and source_identity_ready
            and (
                _grow_question_is_source_acquisition_question(question)
                or _grow_question_is_non_blocking_with_source_material(question)
                or _grow_identity_question_has_default_action(question)
            )
        ):
            question_id = _grow_clarification_question_id(question)
            question["question_id"] = question_id
            question["id"] = question_id
            question["status"] = "defaulted_non_blocking"
            question["default_resolution"] = (
                "source_intake_should_not_pause_on_legacy_semantic_or_trust_questions; "
                "preserve source anchor/chunks and let semantic Grow ask claim-specific questions only when material"
            )
            defaulted.append(question)
            continue
        retained.append(question)
    return retained, defaulted


def _grow_v3_source_without_legacy_semantic_questions(
    source_package: dict[str, Any],
) -> dict[str, Any]:
    """Normalize source-reader questions for the V3 Grow loop.

    The source reader may already know that the operator must clarify scope,
    identity, trust or persistence before a safe preview can be compiled.  Keep
    those questions visible to the caller instead of entering the provider path:
    the resume request is the explicit handoff into the AI investigator.
    """

    package = deepcopy(dict(source_package or {}))
    source_material_ready = _grow_source_package_has_source_material(package)
    source_identity_ready = _grow_source_package_has_source_identity(package)
    raw_clarification_questions = [
        dict(item)
        for item in list(package.get("clarification_questions") or [])
        if isinstance(item, dict)
    ]
    retained_questions, defaulted_questions = _filter_non_blocking_grow_source_questions(
        raw_clarification_questions,
        source_material_ready=source_material_ready,
        source_identity_ready=source_identity_ready,
    )
    package["clarification_questions"] = retained_questions
    raw_open_questions = [
        dict(item)
        for item in list(package.get("open_questions") or [])
        if isinstance(item, dict)
    ]
    retained_open_questions, defaulted_open_questions = _filter_non_blocking_grow_source_questions(
        raw_open_questions,
        source_material_ready=source_material_ready,
        source_identity_ready=source_identity_ready,
    )
    defaulted_questions.extend(defaulted_open_questions)
    package["open_questions"] = retained_open_questions
    if defaulted_questions:
        package["non_blocking_clarification_questions"] = defaulted_questions
    formation = deepcopy(dict(package.get("source_formation_contract") or {}))
    if formation:
        gate = deepcopy(dict(formation.get("question_gate") or {}))
        raw_pending_questions = [
            dict(item)
            for item in list(gate.get("pending_questions") or retained_questions)
            if isinstance(item, dict)
        ]
        pending_questions, defaulted_pending_questions = _filter_non_blocking_grow_source_questions(
            raw_pending_questions,
            source_material_ready=source_material_ready,
            source_identity_ready=source_identity_ready,
        )
        if defaulted_pending_questions:
            existing_defaulted = [
                dict(item)
                for item in list(package.get("non_blocking_clarification_questions") or [])
                if isinstance(item, dict)
            ]
            package["non_blocking_clarification_questions"] = [
                *existing_defaulted,
                *defaulted_pending_questions,
            ]
        gate_state = "not_required"
        if pending_questions:
            gate_state = (
                "paused_for_acquisition"
                if all(_grow_question_is_source_acquisition_question(item) for item in pending_questions)
                else str(gate.get("state") or "paused_for_clarification")
            )
        gate.update(
            {
                "state": gate_state,
                "apply_blocked": bool(pending_questions),
                "question_count": len(pending_questions),
                "pending_count": len(pending_questions),
                "answered_count": 0,
                "defaulted_count": len(
                    list(package.get("non_blocking_clarification_questions") or [])
                ),
                "pending_questions": pending_questions,
            }
        )
        formation["question_gate"] = gate
        package["source_formation_contract"] = formation
    return package


def _grow_clarification_answer_text(answer_payload: Any) -> str:
    if isinstance(answer_payload, dict):
        for key in ("answer", "selected_option", "value", "action", "note"):
            value = answer_payload.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""
    if answer_payload is None:
        return ""
    return str(answer_payload).strip()


def _grow_clarification_answers(payload_answers: Any) -> dict[str, str]:
    if not isinstance(payload_answers, dict):
        return {}
    answers: dict[str, str] = {}
    for raw_question_id, raw_answer in payload_answers.items():
        question_id = str(raw_question_id or "").strip()
        answer = _grow_clarification_answer_text(raw_answer)
        if question_id and answer:
            answers[question_id] = answer
    return answers


def _grow_payload_clarification_answers(payload: McpGrowSourceRequest) -> dict[str, str]:
    answers: dict[str, str] = {}
    answers.update(_grow_clarification_answers(getattr(payload, "clarification_answers", {})))
    answers.update(_grow_clarification_answers(getattr(payload.options, "clarification_answers", {})))
    return answers


def _grow_clarification_question_id(question: dict[str, Any]) -> str:
    return str(question.get("question_id") or question.get("id") or "").strip()


def _grow_default_clarification_question_text(
    question_id: str,
    question: dict[str, Any],
) -> str:
    reason = str(question.get("reason") or "").strip().lower()
    if question_id == "source_scope_1":
        return "What kind of source is this, and how should the brain use it?"
    if question_id == "source_trust_1":
        return "How should the brain treat this source's trust level before saving knowledge from it?"
    if question_id == "url_crawl_scope_1":
        return "Should the brain read only this URL or also inspect same-domain linked pages?"
    if question_id == "identity_project_disambiguation_1":
        return "Are the extracted names directly connected to the brain owner/project, or are they only background context?"
    if question_id == "uncertain_claim_policy_1":
        return "Should uncertain extracted claims be saved as hypotheses, deferred for later review, or discarded?"
    if question_id == "image_identity_review_1":
        return "Should image-derived names or labels be saved, or kept only as source context?"
    if "identity" in question_id or "identity" in reason:
        return "Which extracted people, companies, or names should be treated as directly connected to this brain?"
    if "trust" in question_id or "trust" in reason:
        return "What trust level should the brain assign to this source?"
    if "scope" in question_id or "scope" in reason:
        return "What scope should the brain apply when learning from this source?"
    return "How should the brain treat this ambiguous source material before saving it?"


def _grow_client_clarification_question(
    raw_question: Any,
    *,
    allow_default_text: bool = True,
) -> dict[str, Any] | None:
    if not isinstance(raw_question, dict):
        return None
    question = raw_question.get("question") if isinstance(raw_question.get("question"), dict) else raw_question
    if not isinstance(question, dict):
        return None
    question_id = _grow_clarification_question_id(question)
    question_text = str(
        question.get("question")
        or question.get("prompt")
        or question.get("title")
        or ""
    ).strip()
    if not question_id:
        return None
    normalized_question = deepcopy(dict(question))
    normalized_question["question_id"] = question_id
    normalized_question["id"] = question_id
    if not question_text and not allow_default_text:
        return None
    if not question_text:
        question_text = _grow_default_clarification_question_text(question_id, normalized_question)
    normalized_question["question"] = question_text
    return normalized_question


def _apply_grow_resume_answers_to_source_package(
    source_package: dict[str, Any],
    *,
    clarification_answers: Any,
) -> dict[str, Any]:
    """Apply resume answers to a persisted source package before provider work.

    The source intake package is persisted during the first preview request.  A
    resume call sends answers separately, so the package must be rehydrated with
    those answers before checking whether questions remain open.
    """

    answers = _grow_clarification_answers(clarification_answers)
    if not answers:
        return source_package
    package = deepcopy(dict(source_package or {}))
    answered_questions: list[dict[str, Any]] = []

    def apply_answer_effect(question_id: str, answer: str) -> None:
        normalized_answer = str(answer or "").strip()
        if not question_id or not normalized_answer:
            return
        source_request = deepcopy(dict(package.get("source_request") or {}))
        request_options = deepcopy(dict(source_request.get("options") or {}))
        requested_options = deepcopy(dict(request_options.get("requested") or {}))
        effective_options = deepcopy(dict(request_options.get("effective") or {}))
        provenance = deepcopy(dict(package.get("provenance") or {}))
        if question_id == "source_trust_1":
            source_request["source_trust"] = normalized_answer
            requested_options["source_trust"] = normalized_answer
            effective_options["source_trust"] = normalized_answer
            provenance["source_trust"] = normalized_answer
            for unit in list(package.get("source_units") or []):
                if not isinstance(unit, dict):
                    continue
                unit_provenance = deepcopy(dict(unit.get("provenance") or {}))
                unit_provenance["source_trust"] = normalized_answer
                unit["provenance"] = unit_provenance
            handoff = deepcopy(dict(package.get("compiler_handoff") or {}))
            for section in list(handoff.get("structured_sections") or []):
                if isinstance(section, dict):
                    section["source_trust"] = normalized_answer
            provenance_map = deepcopy(dict(handoff.get("provenance_map") or {}))
            for key, value in list(provenance_map.items()):
                if isinstance(value, dict):
                    item = deepcopy(dict(value))
                    item["source_trust"] = normalized_answer
                    provenance_map[key] = item
            if provenance_map:
                handoff["provenance_map"] = provenance_map
            if handoff:
                package["compiler_handoff"] = handoff
        elif question_id == "source_scope_1":
            purpose = deepcopy(dict(package.get("source_purpose") or {}))
            purpose.update(
                {
                    "purpose": normalized_answer,
                    "confidence": 0.96,
                    "decision_source": "user_clarification",
                    "requires_confirmation": False,
                }
            )
            package["source_purpose"] = purpose
            requested_options["treat_as"] = normalized_answer
            effective_options["treat_as"] = normalized_answer
        elif question_id == "url_crawl_scope_1":
            requested_options["crawl_sublinks"] = normalized_answer
            effective_options["crawl_sublinks"] = normalized_answer
        elif question_id == "identity_project_disambiguation_1":
            requested_options["relationship_scope"] = normalized_answer
            effective_options["relationship_scope"] = normalized_answer
        elif question_id == "uncertain_claim_policy_1":
            requested_options["uncertainty_policy"] = normalized_answer
            effective_options["uncertainty_policy"] = normalized_answer
        request_options["requested"] = requested_options
        request_options["effective"] = effective_options
        source_request["options"] = request_options
        package["source_request"] = source_request
        if provenance:
            package["provenance"] = provenance

    def apply_to_question(raw_question: Any) -> dict[str, Any] | None:
        if not isinstance(raw_question, dict):
            return None
        question = deepcopy(dict(raw_question))
        question_id = _grow_clarification_question_id(question)
        if not question_id:
            return question
        question["question_id"] = question_id
        question["id"] = question_id
        answer = answers.get(question_id)
        if answer:
            question["answer"] = answer
            question["status"] = "answered"
            question["applied_default_action"] = None
            answered_questions.append({"question_id": question_id, "answer": answer})
            apply_answer_effect(question_id, answer)
        return question

    for list_key in ("clarification_questions", "open_questions"):
        package[list_key] = [
            question
            for question in (
                apply_to_question(item) for item in list(package.get(list_key) or [])
            )
            if isinstance(question, dict)
        ]

    formation = deepcopy(dict(package.get("source_formation_contract") or {}))
    gate = deepcopy(dict(formation.get("question_gate") or {}))
    pending_questions: list[dict[str, Any]] = []
    refreshed_pending: list[dict[str, Any]] = []
    guided_grow = deepcopy(dict(package.get("guided_grow") or {}))
    guided_pending_source = [
        item
        for item in list(guided_grow.get("pending_questions") or [])
        if isinstance(item, dict)
    ]
    source_question_candidates = [
        *list(package.get("clarification_questions") or []),
        *list(package.get("open_questions") or []),
        *list(gate.get("pending_questions") or []),
        *guided_pending_source,
    ]
    seen_question_ids: set[str] = set()
    for candidate in source_question_candidates:
        if not isinstance(candidate, dict):
            continue
        question = deepcopy(dict(candidate))
        question_id = _grow_clarification_question_id(question)
        if not question_id or question_id in seen_question_ids:
            continue
        seen_question_ids.add(question_id)
        question["question_id"] = question_id
        question["id"] = question_id
        answer = answers.get(question_id) or str(question.get("answer") or "").strip()
        if answer:
            question["answer"] = answer
            question["status"] = "answered"
            if not any(item.get("question_id") == question_id for item in answered_questions):
                answered_questions.append({"question_id": question_id, "answer": answer})
            apply_answer_effect(question_id, answer)
            continue
        pending_questions.append(question)
        refreshed_pending.append(
            {
                key: value
                for key, value in question.items()
                if key
                in {
                    "question_id",
                    "id",
                    "reason",
                    "question",
                    "options",
                    "default_action_if_unanswered",
                    "affected_units",
                    "affected_preview_ids",
                    "source_slice",
                    "affects",
                }
            }
        )
    if formation or gate:
        gate.update(
            {
                "state": "paused_for_clarification" if pending_questions else "answered",
                "apply_blocked": bool(pending_questions),
                "question_count": len(seen_question_ids),
                "pending_count": len(pending_questions),
                "answered_count": len(answered_questions),
                "pending_questions": refreshed_pending,
                "answered_questions": answered_questions,
            }
        )
        formation["question_gate"] = gate
        package["source_formation_contract"] = formation
    if guided_grow:
        guided_pending: list[dict[str, Any]] = []
        guided_answered: list[dict[str, Any]] = []
        for candidate in guided_pending_source:
            question = deepcopy(dict(candidate))
            question_id = _grow_clarification_question_id(question)
            if not question_id:
                continue
            answer = answers.get(question_id)
            if answer:
                question["question_id"] = question_id
                question["id"] = question_id
                question["question"] = str(
                    question.get("question")
                    or _grow_default_clarification_question_text(question_id, question)
                )
                question["answer"] = answer
                question["status"] = "answered"
                guided_answered.append({"question_id": question_id, "answer": answer})
                apply_answer_effect(question_id, answer)
            else:
                guided_pending.append(
                    {
                        **question,
                        "question_id": question_id,
                        "id": question_id,
                        "question": str(
                            question.get("question")
                            or _grow_default_clarification_question_text(question_id, question)
                        ),
                    }
                )
        prior_answered = [
            item
            for item in list(guided_grow.get("answered_questions") or [])
            if isinstance(item, dict)
        ]
        seen_answered = {
            str(item.get("question_id") or "").strip()
            for item in prior_answered
            if str(item.get("question_id") or "").strip()
        }
        merged_answered = list(prior_answered)
        for item in guided_answered:
            question_id = str(item.get("question_id") or "").strip()
            if question_id and question_id not in seen_answered:
                seen_answered.add(question_id)
                merged_answered.append(item)
        guided_grow["pending_questions"] = guided_pending
        guided_grow["answered_questions"] = merged_answered
        guided_grow["pending_count"] = len(guided_pending)
        guided_grow["answered_count"] = len(merged_answered)
        if not guided_pending and merged_answered:
            guided_grow["state"] = "answered"
            guided_grow["can_resume"] = False
            handoff = deepcopy(dict(package.get("compiler_handoff") or {}))
            blocked = [
                str(reason)
                for reason in list(handoff.get("preview_blocked_reasons") or [])
                if str(reason) != "pending_guided_grow_questions"
            ]
            handoff["preview_blocked_reasons"] = blocked
            if not blocked:
                handoff["preview_eligible"] = True
            package["compiler_handoff"] = handoff
            if str(package.get("status") or "").strip() == "asking_clarification":
                package["status"] = "preview_ready"
        package["guided_grow"] = guided_grow
    package["questions_and_answers"] = [
        {
            "question_id": item["question_id"],
            "answer": item["answer"],
            "status": "answered",
        }
        for item in answered_questions
        if item.get("question_id")
    ]
    package["clarification_answers"] = dict(answers)
    return package


def _grow_intake_questions_from_source_package(
    source_package: dict[str, Any],
    *,
    include_guided: bool = True,
    allow_default_text: bool = True,
) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    seen_question_ids: set[str] = set()

    def add_question(candidate: Any) -> None:
        if not isinstance(candidate, dict):
            return
        question = candidate.get("question") if isinstance(candidate.get("question"), dict) else candidate
        if not isinstance(question, dict):
            return
        if candidate.get("answer") is not None or question.get("answer") is not None:
            return
        question_status = str(candidate.get("status") or question.get("status") or "").strip()
        if question_status in {
            "answered",
            "closed",
            "resolved",
            "defaulted",
            "defaulted_non_blocking",
            "not_required",
        }:
            return
        normalized_question = _grow_client_clarification_question(
            candidate,
            allow_default_text=allow_default_text,
        )
        if normalized_question is None:
            return
        question_id = str(normalized_question.get("question_id") or "").strip()
        if question_id in seen_question_ids:
            return
        seen_question_ids.add(question_id)
        questions.append(normalized_question)

    for item in list(source_package.get("clarification_questions") or []):
        add_question(item)
    for item in list(source_package.get("open_questions") or []):
        add_question(item)
    formation_gate = dict(
        dict(source_package.get("source_formation_contract") or {}).get("question_gate") or {}
    )
    for item in list(formation_gate.get("pending_questions") or []):
        add_question(item)
    if include_guided:
        guided_grow = dict(source_package.get("guided_grow") or {})
        for item in list(guided_grow.get("pending_questions") or []):
            add_question(item)
    for item in list(source_package.get("questions_and_answers") or []):
        add_question(item)
    return questions


def _grow_apply_question_limit(
    questions: list[dict[str, Any]],
    question_limit: int | None,
) -> list[dict[str, Any]]:
    limit = int(question_limit if question_limit is not None else len(questions))
    if limit <= 0:
        return []
    return questions[:limit]


def _grow_source_package_resume_token(
    source_package: dict[str, Any],
    *,
    investigation_id: str,
) -> str:
    gate = dict(
        dict(source_package.get("source_formation_contract") or {}).get("question_gate") or {}
    )
    recipe = dict(gate.get("resume_recipe") or {})
    payload_patch = dict(recipe.get("payload_patch") or {})
    token = str(payload_patch.get("resume_token") or "").strip()
    if token:
        return token
    question_ids = ",".join(
        str(item.get("question_id") or "").strip()
        for item in _grow_intake_questions_from_source_package(source_package)
        if str(item.get("question_id") or "").strip()
    )
    digest = hashlib.sha256(f"{investigation_id}|{question_ids}".encode("utf-8")).hexdigest()[:12]
    return f"grow_resume_{digest}"


def _grow_source_package_with_public_question_gate(
    source_package: dict[str, Any],
    *,
    questions: list[dict[str, Any]],
    resume_token: str,
) -> dict[str, Any]:
    """Keep nested source metadata aligned with the public clarification state.

    Some guided Grow questions are produced by the semantic investigator after
    source intake.  The top-level response already exposes those questions, but
    clients also inspect the nested source formation contract to decide whether
    the Continue/Resume action is enabled.  Keep both surfaces consistent.
    """

    package = deepcopy(dict(source_package or {}))
    normalized_questions = [
        question
        for question in (
            _grow_client_clarification_question(item)
            for item in list(questions or [])
            if isinstance(item, dict)
        )
        if isinstance(question, dict)
    ]
    if not normalized_questions:
        return package
    formation = deepcopy(dict(package.get("source_formation_contract") or {}))
    gate = deepcopy(dict(formation.get("question_gate") or {}))
    answer_patch = {
        str(item.get("question_id") or item.get("id") or "").strip(): "<answer>"
        for item in normalized_questions
        if str(item.get("question_id") or item.get("id") or "").strip()
    }
    gate.update(
        {
            "state": "awaiting_clarification",
            "pause_on_questions": True,
            "apply_blocked": True,
            "question_count": len(normalized_questions),
            "pending_count": len(normalized_questions),
            "answered_count": 0,
            "pending_questions": normalized_questions,
            "resume_recipe": {
                "tool": "grow_source_preview",
                "payload_patch": {
                    "resume_token": resume_token,
                    "options": {"clarification_answers": answer_patch},
                },
            },
        }
    )
    formation["question_gate"] = gate
    formation["state"] = "awaiting_clarification"
    formation["apply_contract"] = {
        **dict(formation.get("apply_contract") or {}),
        "can_apply_now": False,
        "blocked_reasons": ["clarification_required"],
        "selected_preview_ids": [],
    }
    package["source_formation_contract"] = formation
    package["clarification_questions"] = normalized_questions
    package["open_questions"] = normalized_questions
    return package


def _core_upload_clarification_answers_json(raw_value: str | None) -> dict[str, str]:
    try:
        parsed = json.loads(raw_value or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid_clarification_answers_json") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="clarification_answers_must_be_object")
    return _grow_clarification_answers(parsed)


def _core_upload_resume_options(
    *,
    treat_as: str,
    analyze_images: str,
    question_limit: int,
    max_pages: int,
    max_ocr_pages: int,
    max_images: int,
    max_online_queries: int,
    max_units: int,
    max_total_chars: int,
    semantic_preview: bool,
    clarification_answers: dict[str, str],
) -> dict[str, Any]:
    return {
        "treat_as": treat_as,
        "analyze_images": analyze_images,
        "use_online_enrichment": False,
        "pause_on_questions": True,
        "clarification_default_policy": "pause_when_unanswered",
        "semantic_preview": True,
        "source_trust": "uploaded_document",
        "clarification_answers": dict(clarification_answers),
        "question_limit": _core_upload_int(question_limit, 12),
        "max_pages": _core_upload_int(max_pages, 20),
        "max_ocr_pages": _core_upload_int(max_ocr_pages, 8),
        "max_images": _core_upload_int(max_images, 12),
        "max_online_queries": _core_upload_int(max_online_queries, 0),
        "max_units": _core_upload_int(max_units, 12),
        "max_total_chars": _core_upload_int(max_total_chars, 120000),
    }


def _grow_source_upload_resume_without_file(
    *,
    brain_id: str | None,
    source_label: str | None,
    source_uri: str | None,
    user_instruction: str | None,
    input_kind: str,
    treat_as: str,
    analyze_images: str,
    question_limit: int,
    max_pages: int,
    max_ocr_pages: int,
    max_images: int,
    max_online_queries: int,
    max_units: int,
    max_total_chars: int,
    semantic_preview: bool,
    investigation_id: str,
    resume_token: str,
    investigation_version: int | None,
    clarification_answers: dict[str, str],
) -> dict[str, Any]:
    payload = McpGrowSourceRequest(
        brain_id=brain_id,
        investigation_id=investigation_id,
        resume_token=resume_token,
        investigation_version=investigation_version,
        source_label=source_label,
        source_uri=source_uri,
        user_instruction=user_instruction,
        input_kind=input_kind,
        run_preview=True,
        clarification_answers=dict(clarification_answers),
        options=_core_upload_resume_options(
            treat_as=treat_as,
            analyze_images=analyze_images,
            question_limit=question_limit,
            max_pages=max_pages,
            max_ocr_pages=max_ocr_pages,
            max_images=max_images,
            max_online_queries=max_online_queries,
            max_units=max_units,
            max_total_chars=max_total_chars,
            semantic_preview=semantic_preview,
            clarification_answers=clarification_answers,
        ),
    )
    grow_response = _grow_source_preview("grow_source_upload", payload)
    source_package = deepcopy(dict(grow_response.source_investigation or {}))
    if not source_package:
        response_payload = grow_response.model_dump(mode="python", exclude_none=True)
        response_payload["route_proof"] = {
            "endpoint": "/mcp/grow-source-upload",
            "aliases": [
                "/mcp/grow-source-upload",
                "/memory/mcp/grow-source-upload",
                "/source-investigation/upload",
                "/memory/source-investigation/upload",
            ],
            "multipart": True,
            "resumed_without_file": True,
            "resume_contract": {
                "requires_file": False,
                "requires_raw_input": False,
                "required_fields": [
                    "investigation_id",
                    "resume_token",
                    "clarification_answers",
                ],
                "accepted_answer_fields": [
                    "clarification_answers_json",
                    "clarification_answers",
                ],
            },
        }
        return response_payload
    file_name = _core_upload_file_name_from_source_package(source_package, source_label)
    content_type = _core_upload_content_type_from_source_package(source_package)
    resolved_brain_id = str(grow_response.brain_id or brain_id or current_brain_id() or "data")
    try:
        brain_record = resolve_brain_scope(resolved_brain_id)
        storage_path = str(dict(brain_record).get("storage_path") or "") or None
    except (BrainRegistryError, HTTPException):
        storage_path = None
    return _core_upload_response_payload(
        brain_id=resolved_brain_id,
        storage_path=storage_path,
        source_package=source_package,
        file_name=file_name,
        content_type=content_type,
        file_bytes=None,
        grow_response=grow_response,
        resumed_without_file=True,
    )


def _grow_intake_clarification_response(
    tool_name: str,
    *,
    brain_id: str,
    investigation_id: str,
    resume_token: str,
    source_package: dict[str, Any],
    questions: list[dict[str, Any]],
    started: float,
    investigation_version: int | None = None,
) -> McpGrowToolExecutionResponse:
    questions = [
        question
        for question in (_grow_client_clarification_question(item) for item in questions)
        if isinstance(question, dict)
    ]
    source_formation_contract = {
        "schema_version": "agvm.core_source_formation_contract.v1",
        "mode": "grow_source_intake",
        "state": "awaiting_clarification",
        "mutates_memory": False,
        "investigation_id": investigation_id,
        "resume_contract": {
            "requires_raw_input": False,
            "requires_file": False,
            "required_fields": [
                "investigation_id",
                "resume_token",
                "clarification_answers",
            ],
            "answer_field": "clarification_answers",
            "accepted_upload_answer_fields": [
                "clarification_answers_json",
                "clarification_answers",
            ],
        },
        "apply_contract": {
            "can_apply_now": False,
            "blocked_reasons": ["clarification_required"],
            "selected_preview_ids": [],
        },
    }
    return McpGrowToolExecutionResponse(
        schema_version="agvm.mcp_grow_tool_output.v1",
        brain_id=brain_id,
        investigation_id=investigation_id,
        tool_name=tool_name,
        status="asking_clarification",
        can_apply_now=False,
        selected_preview_ids=[],
        source_investigation=source_package,
        source_formation_contract=source_formation_contract,
        memory_operation_lifecycle_contract={
            "schema_version": "agvm.memory_operation_lifecycle_contract.v3",
            "operation": "grow",
            "phase": "awaiting_clarification",
            "next_action": "resume with investigation_id, resume_token and answers",
        },
        clarification_request={"questions": questions},
        clarification_questions=questions,
        compiler_handoff_proof=deepcopy(dict(source_package.get("compiler_handoff_proof") or {})),
        investigation={
            "schema_version": "agvm.grow_investigation.v3",
            "investigation_id": investigation_id,
            "source_investigation_id": investigation_id,
            "brain_id": brain_id,
            "state": "awaiting_clarification",
            "status": "ASKING_CLARIFICATION",
            "complete": False,
            "applicable": False,
            "questions": questions,
            "pending_questions": questions,
        },
        investigation_session={
            "schema_version": "agvm.investigation_session.v3",
            "status": "awaiting_answers",
            "question_count": len(questions),
            "provider_attested": False,
            "persisted": investigation_version is not None,
            "investigation_version": investigation_version,
        },
        resume_token=resume_token,
        investigation_version=investigation_version,
        completeness={
            "preview_generated": False,
            "investigation_complete": False,
            "investigation_applicable": False,
            "question_count": len(questions),
        },
        mcp_latency_profile={"elapsed_ms": int((time.perf_counter() - started) * 1000)},
        budget={"credits_required": 0, "runtime": "local_core"},
        next_action="resume with investigation_id, resume_token and answers",
    )


def _grow_provider_failure_is_source_bound_recoverable(
    engine_result: dict[str, Any],
    investigation: dict[str, Any],
) -> bool:
    failure = {
        **dict(investigation.get("failure") or {}),
        **dict(engine_result.get("failure") or {}),
    }
    failure_text = " ".join(
        str(value or "").lower()
        for value in (
            failure.get("code"),
            failure.get("detail"),
            failure.get("message"),
            investigation.get("status"),
            engine_result.get("status"),
        )
    )
    return bool(
        ("provider" in failure_text or "transport" in failure_text)
        and (
            "timeout" in failure_text
            or "timed out" in failure_text
            or "429" in failure_text
            or "quota" in failure_text
            or "insufficient_quota" in failure_text
            or "rate limit" in failure_text
            or "rate_limit" in failure_text
            or "credits" in failure_text
            or "unavailable" in failure_text
            or "not_configured" in failure_text
            or "not configured" in failure_text
        )
    )


def _grow_provider_failure_block_reason(
    engine_result: dict[str, Any],
    investigation: dict[str, Any],
) -> str:
    failure = {
        **dict(investigation.get("failure") or {}),
        **dict(engine_result.get("failure") or {}),
    }
    failure_text = " ".join(
        str(value or "").lower()
        for value in (
            failure.get("code"),
            failure.get("detail"),
            failure.get("message"),
            investigation.get("status"),
            engine_result.get("status"),
        )
    )
    if any(marker in failure_text for marker in ("429", "quota", "insufficient_quota", "credits")):
        return "blocked_ai_provider_quota"
    if "timeout" in failure_text or "timed out" in failure_text:
        return "blocked_ai_provider_timeout"
    if "provider" in failure_text or "transport" in failure_text:
        return "blocked_ai_provider_error"
    return str(failure.get("code") or "grow_ai_unavailable")


def _grow_runtime_provider_fast_block_reason() -> str | None:
    try:
        runtime = llm_runtime_status()
    except Exception:  # noqa: BLE001 - runtime status must not break Grow routing
        return None
    error_text = " ".join(
        str(payload.get("last_error") or "").lower()
        for payload in runtime.values()
        if isinstance(payload, dict)
    )
    if not error_text:
        return None
    if any(
        marker in error_text
        for marker in (
            "insufficient_quota",
            "credit_balance",
            "no credits remaining",
            "billing",
            "429",
            "quota",
            "credits",
        )
    ):
        return "blocked_ai_provider_quota"
    if any(marker in error_text for marker in ("invalid_api_key", "unauthorized", "401", "403")):
        return "blocked_ai_provider_auth"
    # A timeout is a transient observation from one bounded request, not a
    # process-wide provider-health verdict.  Keeping it in the global runtime
    # snapshot must not prevent Grow from making a later provider attempt.
    return None


def _grow_provider_failure_recovery_reason(
    engine_result: dict[str, Any],
    investigation: dict[str, Any],
) -> str:
    block_reason = _grow_provider_failure_block_reason(engine_result, investigation)
    if block_reason == "blocked_ai_provider_timeout":
        return "semantic_provider_timeout"
    if block_reason == "blocked_ai_provider_quota":
        return "semantic_provider_quota_or_rate_limited"
    return "semantic_provider_unavailable"


def _grow_engine_preview_hard_timeout_seconds(options: Any) -> float:
    try:
        requested = float(getattr(options, "compiler_preview_timeout_seconds", 45.0) or 45.0)
    except (TypeError, ValueError):
        requested = 45.0
    hard_timeout = max(2.0, requested + 2.0)
    if bool(getattr(options, "pause_on_questions", False)) or bool(
        getattr(options, "semantic_preview", False)
    ):
        hard_timeout = min(hard_timeout, _GROW_INTERACTIVE_HARD_TIMEOUT_SECONDS)
    return hard_timeout


def _canonical_guided_grow_payload(
    payload: McpGrowSourceRequest,
    *,
    force_guided: bool = False,
) -> McpGrowSourceRequest:
    """Normalize default public Grow preview into the AI investigator loop.

    Explicit source-bound/document-proof contracts keep their provider-free
    authority.  Otherwise public preview follows one Grow path: source package
    -> semantic investigator -> clarification questions when needed -> preview
    -> apply.
    """

    if not payload.run_preview:
        return payload
    if not force_guided and (
        _grow_source_bound_storage_only_requested(payload)
        or _deterministic_public_text_eligible(payload)
    ):
        return payload

    try:
        requested_timeout = float(payload.options.compiler_preview_timeout_seconds or 0.0)
    except (TypeError, ValueError):
        requested_timeout = 0.0
    try:
        requested_question_limit = int(payload.options.question_limit or 0)
    except (TypeError, ValueError):
        requested_question_limit = 0
    guided_options = payload.options.model_copy(
        update={
            "pause_on_questions": True,
            "clarification_default_policy": "pause_when_unanswered",
            "semantic_preview": True,
            "source_storage_mode": "auto",
            "compiler_preview_timeout_seconds": max(
                requested_timeout,
                _GROW_INTERACTIVE_COMPILER_TIMEOUT_SECONDS,
            ),
            "question_limit": max(requested_question_limit, _GROW_STANDARD_QUESTION_LIMIT),
        }
    )
    return payload.model_copy(update={"run_preview": True, "options": guided_options})


def _canonical_semantic_grow_payload(payload: McpGrowSourceRequest) -> McpGrowSourceRequest:
    """Require provider-backed semantic compilation without forcing an interview."""

    if not payload.run_preview:
        return payload
    try:
        requested_timeout = float(payload.options.compiler_preview_timeout_seconds or 0.0)
    except (TypeError, ValueError):
        requested_timeout = 0.0
    semantic_options = payload.options.model_copy(
        update={
            "semantic_preview": True,
            "source_storage_mode": "auto",
            "compiler_preview_timeout_seconds": max(
                requested_timeout,
                _GROW_INTERACTIVE_COMPILER_TIMEOUT_SECONDS,
            ),
        }
    )
    return payload.model_copy(update={"run_preview": True, "options": semantic_options})


def _canonical_source_preview_payload(payload: McpGrowSourceRequest) -> McpGrowSourceRequest:
    """Normalize source preview without forcing the semantic guided loop.

    Public source/document preview must be able to produce deterministic,
    source-bound anchors and chunks even when the semantic provider is
    unavailable.  Rich semantic memory and weight decisions are still
    provider-gated when the caller explicitly requests semantic preview, or by
    using grow-preview/grow-guided.
    """

    if not payload.run_preview:
        return payload
    if _grow_source_bound_storage_only_requested(payload) or _deterministic_public_text_eligible(payload):
        return payload
    if bool(getattr(payload.options, "semantic_preview", False)) or bool(
        getattr(payload.options, "pause_on_questions", False)
    ):
        return payload
    input_kind = str(payload.input_kind or "").strip().lower()
    source = getattr(payload, "source", None)
    source_kind = str(getattr(source, "kind", "") or "").strip().lower()
    source_url = str(getattr(source, "url", "") or "").strip()
    source_uri = str(
        getattr(source, "source_uri", "")
        or payload.source_uri
        or ""
    ).strip()
    if input_kind in {"url", "website", "webpage"} or source_kind in {"url", "website", "webpage"} or source_url or source_uri.startswith(("http://", "https://")):
        source_options = payload.options.model_copy(
            update={
                "pause_on_questions": False,
                "semantic_preview": False,
                "source_storage_mode": payload.options.source_storage_mode or "auto",
            }
        )
        return payload.model_copy(update={"run_preview": True, "options": source_options})
    return payload


def _run_grow_engine_preview_with_hard_timeout(
    *,
    options: Any,
    investigation_id: str,
    brain_id: str,
    preview_brain_revision: str,
    preview_kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Run semantic Grow preview under a handler-owned hard cap.

    The Grow investigator has internal stage budgets, but this API endpoint also
    needs an outer wall-clock contract. If an upstream provider, child Search,
    or adapter blocks despite cooperative timeouts, return a recoverable timeout
    envelope so callers can continue with source-bound anchors/chunks.
    """

    hard_timeout = _grow_engine_preview_hard_timeout_seconds(options)
    context = copy_context()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agvm-grow-engine-preview")
    future = executor.submit(lambda: context.run(_GROW_ENGINE.preview, **preview_kwargs))
    try:
        return dict(future.result(timeout=hard_timeout))
    except FuturesTimeoutError:
        future.cancel()
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
                "status": "INCOMPLETE",
                "state": "investigating",
                "complete": False,
                "applicable": False,
                "investigation_id": investigation_id,
                "source_investigation_id": investigation_id,
                "brain_id": brain_id,
                "brain_revision": preview_brain_revision,
                "failure": {
                    "code": "provider_timeout",
                    "detail": f"provider_timeout_after_{hard_timeout:.3f}s:grow_engine_preview",
                },
            },
            "investigation_session": {
                "schema_version": "agvm.investigation_session.v3",
                "status": "incomplete",
                "provider_attested": False,
                "provider_failure": {
                    "code": "provider_timeout",
                    "detail": f"provider_timeout_after_{hard_timeout:.3f}s:grow_engine_preview",
                },
            },
            "usage": {},
            "apply_ready": False,
        }
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _grow_source_bound_recovery_preview_response(
    tool_name: str,
    *,
    brain_record: dict[str, Any],
    brain_id: str,
    original_investigation_id: str,
    source_package: dict[str, Any],
    engine_result: dict[str, Any],
    investigation: dict[str, Any],
    preview_brain_revision: str,
    started: float,
) -> McpGrowToolExecutionResponse:
    """Return an applyable source/document preview when AI semantic review is unavailable.

    This is deliberately narrower than the full Grow investigator: it emits
    only source/document anchors, source-unit chunks, extractive section
    summaries, and exact source-bound fact sentences.  Inferred rich semantic
    nodes still require a successful AI investigation.
    """

    recovery_source_package = deepcopy(dict(source_package or {}))
    recovery_investigation_id = f"mcp-grow-source-bound-{uuid.uuid4()}"
    recovery_source_package["investigation_id"] = recovery_investigation_id
    recovery_source_package["status"] = "preview_ready"
    recovery_reason = _grow_provider_failure_recovery_reason(engine_result, investigation)
    recovery_source_package["source_bound_recovery"] = {
        "schema_version": "agvm.grow_source_bound_recovery.v1",
        "original_investigation_id": original_investigation_id,
        "reason": recovery_reason,
        "semantic_claims_emitted": False,
    }
    recovery_source_package = _normalize_manual_source_package_provenance(recovery_source_package)
    recovery_graph_snapshot: dict[str, Any] | None = None
    try:
        with use_runtime_brain(brain_record):
            bootstrap_runtime_store()
            recovery_graph_snapshot = fetch_graph_snapshot()
    except (sqlite3.DatabaseError, RuntimeError, OSError, ValueError):
        recovery_graph_snapshot = None
    preview_bundle = _core_upload_fallback_preview_bundle(
        recovery_source_package,
        graph=recovery_graph_snapshot,
    )
    warning_notice = {
        "schema_version": "agvm.grow_source_bound_recovery_warning.v1",
        "kind": "semantic_investigator_unavailable",
        "severity": "warning",
        "title": "Source saved without semantic claim expansion",
        "message": (
            "The source/document can be stored with anchors and chunks now. "
            "Rich section memories and child atomic claims need a later semantic Grow pass."
        ),
        "original_investigation_id": original_investigation_id,
    }
    recovery_source_package["source_bound_recovery"] = {
        **dict(recovery_source_package.get("source_bound_recovery") or {}),
        "warning": deepcopy(warning_notice),
    }
    preview_bundle["warnings"] = [
        *[
            deepcopy(dict(item))
            for item in list(preview_bundle.get("warnings") or [])
            if isinstance(item, dict)
        ],
        deepcopy(warning_notice),
    ]
    selected_preview_ids = _selected_preview_ids(preview_bundle, [])
    attestation = _core_upload_deterministic_source_attestation(recovery_source_package)
    compiler_handoff_proof = build_source_compiler_handoff_proof(
        recovery_source_package,
        preview_bundle,
    )
    recovery_source_package["compiler_handoff_proof"] = compiler_handoff_proof
    source_formation_contract = {
        **build_source_formation_contract(recovery_source_package, preview_bundle),
        "schema_version": "agvm.core_source_formation_contract.v3",
        "mode": "source_bound_recovery_preview",
        "state": "preview_ready",
        "mutates_memory": False,
        "investigation_id": recovery_investigation_id,
        "recovery_of": original_investigation_id,
        "authority": {
            "kind": "deterministic_source_bound_recovery",
            "schema_version": attestation["schema_version"],
            "provider_required": False,
            "semantic_claims_allowed": False,
            "source_bound_claims_allowed": True,
            "preview_scope": list(attestation.get("preview_scope") or []),
            "reason": (
                "AI semantic investigation was unavailable after source clarification; "
                "storing only source-bound document material is safe."
            ),
        },
        "apply_contract": {
            "preview_required": True,
            "explicit_confirm_apply_required": True,
            "explicit_selection_required": True,
            "apply_without_preview_allowed": False,
            "can_apply_now": bool(selected_preview_ids),
            "blocked_reasons": [] if selected_preview_ids else ["preview_bundle_missing"],
            "selected_preview_ids": selected_preview_ids,
        },
    }
    recovery_source_package["source_formation_contract"] = deepcopy(source_formation_contract)
    investigation_session = {
        "schema_version": "agvm.investigation_session.v3",
        "status": "preview_ready",
        "authority_kind": "deterministic_source_bound_recovery",
        "provider_executed": False,
        "provider_failure": dict(
            dict(investigation.get("failure") or {}) or dict(engine_result.get("failure") or {})
        ),
        "provider_recovery_reason": recovery_reason,
        "original_investigation_id": original_investigation_id,
        "preview_node_count": len(selected_preview_ids),
    }
    try:
        with use_runtime_brain(brain_record):
            persisted_preview = store_local_grow_v2_preview(
                brain_id=brain_id,
                investigation_id=recovery_investigation_id,
                tool_name=tool_name,
                source_investigation=recovery_source_package,
                source_formation_contract=source_formation_contract,
                preview_bundle=preview_bundle,
                ai_execution_attestation=None,
                deterministic_source_attestation=attestation,
                investigation_session=investigation_session,
                expected_brain_revision=preview_brain_revision,
            )
    except (GrowPreviewBindingStoreError, AiModuleContractError, sqlite3.DatabaseError) as exc:
        reason = str(getattr(exc, "code", "") or exc or "grow_source_bound_recovery_preview_failed")
        return _grow_blocked(
            tool_name,
            brain_id,
            reason,
            started,
            source_investigation=recovery_source_package,
            preview_bundle=preview_bundle,
        )
    _GROW_PREVIEW_RUNS[recovery_investigation_id] = {
        "brain_id": brain_id,
        "status": "preview_ready",
        "source_investigation": recovery_source_package,
        "source_formation_contract": source_formation_contract,
        "preview_bundle": deepcopy(preview_bundle),
        "preview_fingerprint": persisted_preview.get("preview_fingerprint"),
        "ai_execution_attestation": {},
        "deterministic_source_attestation": deepcopy(attestation),
        "attestation_fingerprint": persisted_preview.get("attestation_fingerprint"),
        "investigation_session": deepcopy(investigation_session),
        "apply_receipt": None,
    }
    return McpGrowToolExecutionResponse(
        schema_version="agvm.mcp_grow_tool_output.v1",
        brain_id=brain_id,
        investigation_id=recovery_investigation_id,
        tool_name=tool_name,
        status="preview_ready",
        can_apply_now=bool(selected_preview_ids),
        selected_preview_ids=selected_preview_ids,
        source_investigation=recovery_source_package,
        source_formation_contract=source_formation_contract,
        memory_operation_lifecycle_contract={
            "schema_version": "agvm.memory_operation_lifecycle_contract.v3",
            "operation": "grow",
            "phase": "preview_ready",
            "next_action": "select exact server preview IDs and Apply with confirm_apply=true",
        },
        preview_bundle=preview_bundle,
        maintenance_feedback_packets=[],
        clarification_questions=[],
        compiler_handoff_proof=compiler_handoff_proof,
        investigation_session={
            **investigation_session,
            "preview_fingerprint": persisted_preview.get("preview_fingerprint"),
            "authority_fingerprint": persisted_preview.get("attestation_fingerprint"),
        },
        investigation={
            "original_investigation_id": original_investigation_id,
            "provider_failure": dict(investigation.get("failure") or {}),
            "brain_revision": preview_brain_revision,
        },
        completeness={
            "preview_generated": True,
            "provider_independent_preview": True,
            "preview_authority": "deterministic_source_bound_recovery",
            "semantic_claims_emitted": False,
            "preview_node_count": len(selected_preview_ids),
            "apply_ready": bool(selected_preview_ids),
        },
        mcp_latency_profile={"elapsed_ms": int((time.perf_counter() - started) * 1000)},
        budget={"credits_required": 0, "runtime": "local_core"},
        next_action="select exact server preview IDs and Apply with confirm_apply=true",
    )


def _grow_source_preview_v3(
    tool_name: str,
    payload: McpGrowSourceRequest,
    *,
    brain_record: dict[str, Any],
) -> McpGrowToolExecutionResponse:
    """Run/resume Grow V3 from canonical server state before compiler preview."""

    started = time.perf_counter()
    brain_id = _brain_record_id(brain_record)
    options = payload.options
    clarification_answers = _grow_payload_clarification_answers(payload)
    resume_requested = bool(payload.investigation_id and payload.resume_token)
    semantic_provider_required = bool(options.semantic_preview or options.pause_on_questions)
    source_package: dict[str, Any]
    resume_token: str
    persisted: dict[str, Any]
    prepared_source_package: dict[str, Any] | None = None
    if not resume_requested:
        try:
            prepared_source_package = _grow_source_package(payload, brain_id)
            prepared_investigation_id = str(
                prepared_source_package.get("investigation_id") or f"mcp-grow-{uuid.uuid4()}"
            )
            prepared_source_package["investigation_id"] = prepared_investigation_id
            prepared_source_package = _grow_v3_source_without_legacy_semantic_questions(
                prepared_source_package
            )
        except TrustedSourcePackageError as exc:
            return _grow_blocked(
                tool_name,
                brain_id,
                exc.code,
                started,
                source_investigation=exc.source_package,
            )
        prepared_questions = _grow_intake_questions_from_source_package(
            prepared_source_package,
            include_guided=bool(options.pause_on_questions),
            allow_default_text=False,
        )
        prepared_questions, defaulted_prepared_questions = _filter_non_blocking_grow_source_questions(
            prepared_questions,
            source_material_ready=_grow_source_package_has_source_material(prepared_source_package),
            source_identity_ready=_grow_source_package_has_source_identity(prepared_source_package),
        )
        if defaulted_prepared_questions:
            existing_defaulted = [
                dict(item)
                for item in list(prepared_source_package.get("non_blocking_clarification_questions") or [])
                if isinstance(item, dict)
            ]
            prepared_source_package["non_blocking_clarification_questions"] = [
                *existing_defaulted,
                *defaulted_prepared_questions,
            ]
        prepared_questions = _grow_apply_question_limit(
            prepared_questions,
            options.question_limit,
        )
        if prepared_questions and not clarification_answers and not semantic_provider_required:
            try:
                with use_runtime_brain(brain_record):
                    bootstrap_runtime_store()
                    preview_brain_revision = maintenance_graph_revision(fetch_graph_snapshot())
                    prepared_resume_token = _grow_source_package_resume_token(
                        prepared_source_package,
                        investigation_id=prepared_investigation_id,
                    )
                    initial_investigation = {
                        "schema_version": "agvm.grow_investigation.v3",
                        "investigation_id": prepared_investigation_id,
                        "source_investigation_id": prepared_investigation_id,
                        "brain_id": brain_id,
                        "brain_revision": preview_brain_revision,
                        "state": "awaiting_clarification",
                        "status": "ASKING_CLARIFICATION",
                        "complete": False,
                        "applicable": False,
                        "questions": prepared_questions,
                        "pending_questions": prepared_questions,
                    }
                    reserved = reserve_grow_investigation(
                        brain_id=brain_id,
                        investigation_id=prepared_investigation_id,
                        source_investigation=prepared_source_package,
                        brain_revision=preview_brain_revision,
                        investigation=initial_investigation,
                        resume_token=prepared_resume_token,
                    )
                    persisted = update_grow_investigation(
                        brain_id=brain_id,
                        investigation_id=prepared_investigation_id,
                        resume_token=prepared_resume_token,
                        expected_version=int(reserved.get("version") or 0),
                        investigation=initial_investigation,
                        state="awaiting_clarification",
                    )
            except (GrowPreviewBindingStoreError, sqlite3.DatabaseError) as exc:
                reason = str(getattr(exc, "code", "") or exc or "grow_investigation_persistence_failed")
                return _grow_blocked(
                    tool_name,
                    brain_id,
                    reason,
                    started,
                    source_investigation=prepared_source_package,
                )
            return _grow_intake_clarification_response(
                tool_name,
                brain_id=brain_id,
                investigation_id=prepared_investigation_id,
                resume_token=str(persisted.get("resume_token") or prepared_resume_token),
                source_package=prepared_source_package,
                questions=prepared_questions,
                started=started,
                investigation_version=int(persisted.get("version") or 0),
            )
        if semantic_provider_required and not clarification_answers:
            provider_fast_block_reason = _grow_runtime_provider_fast_block_reason()
            if provider_fast_block_reason:
                return _grow_blocked(
                    tool_name,
                    brain_id,
                    provider_fast_block_reason,
                    started,
                    source_investigation=prepared_source_package,
                    source_formation_contract={
                        "schema_version": "agvm.core_source_formation_contract.v3",
                        "mode": "grow_ai_investigation",
                        "state": "blocked",
                        "mutates_memory": False,
                        "investigation_id": prepared_investigation_id,
                        "blocked_reason": provider_fast_block_reason,
                        "provider_required": True,
                        "semantic_fallback_allowed": False,
                        "fast_block": True,
                    },
                )
    if resume_requested:
        try:
            with use_runtime_brain(brain_record):
                existing_investigation = fetch_grow_investigation(
                    brain_id=brain_id,
                    investigation_id=str(payload.investigation_id or ""),
                    resume_token=str(payload.resume_token or ""),
                )
        except GrowPreviewBindingStoreError as exc:
            reason = str(getattr(exc, "code", "") or exc or "grow_investigation_resume_failed")
            return _grow_blocked(tool_name, brain_id, reason, started)
        if not existing_investigation:
            try:
                stateless_source_package = _grow_source_package(payload, brain_id)
                stateless_source_package["investigation_id"] = str(payload.investigation_id or "")
                stateless_source_package = _grow_v3_source_without_legacy_semantic_questions(
                    stateless_source_package
                )
                stateless_source_package = _apply_grow_resume_answers_to_source_package(
                    stateless_source_package,
                    clarification_answers=clarification_answers,
                )
                stateless_source_package = _ensure_grow_source_preview_raw_input(
                    stateless_source_package,
                    payload,
                )
            except TrustedSourcePackageError as exc:
                return _grow_blocked(
                    tool_name,
                    brain_id,
                    exc.code,
                    started,
                    source_investigation=exc.source_package,
                )
            stateless_questions = _grow_intake_questions_from_source_package(stateless_source_package)
            stateless_questions = _grow_apply_question_limit(
                stateless_questions,
                options.question_limit,
            )
            if stateless_questions:
                return _grow_intake_clarification_response(
                    tool_name,
                    brain_id=brain_id,
                    investigation_id=str(payload.investigation_id or ""),
                    resume_token=str(payload.resume_token or "")
                    or _grow_source_package_resume_token(
                        stateless_source_package,
                        investigation_id=str(payload.investigation_id or ""),
                    ),
                    source_package=stateless_source_package,
                    questions=stateless_questions,
                    started=started,
                )
            with use_runtime_brain(brain_record):
                bootstrap_runtime_store()
                stateless_preview_revision = maintenance_graph_revision(fetch_graph_snapshot())
            return _grow_source_bound_recovery_preview_response(
                tool_name,
                brain_record=brain_record,
                brain_id=brain_id,
                original_investigation_id=str(payload.investigation_id or ""),
                source_package=stateless_source_package,
                engine_result={
                    "failure": {
                        "code": "semantic_investigation_not_persisted",
                        "detail": "resuming source intake without a persisted provider investigation",
                    }
                },
                investigation={
                    "failure": {
                        "code": "semantic_investigation_not_persisted",
                        "detail": "source-bound recovery preview",
                    }
                },
                preview_brain_revision=stateless_preview_revision,
                started=started,
            )
    with use_runtime_brain(brain_record):
        bootstrap_runtime_store()
        graph = fetch_graph_snapshot()
        preview_brain_revision = maintenance_graph_revision(graph)
        try:
            if resume_requested:
                persisted = resume_grow_investigation(
                    brain_id=brain_id,
                    investigation_id=str(payload.investigation_id or ""),
                    resume_token=str(payload.resume_token or ""),
                    clarification_answers=clarification_answers,
                    expected_version=payload.investigation_version,
                    expected_brain_revision=preview_brain_revision,
                )
                source_package = _grow_v3_source_without_legacy_semantic_questions(
                    deepcopy(dict(persisted["source_investigation"]))
                )
                source_package = _apply_grow_resume_answers_to_source_package(
                    source_package,
                    clarification_answers=clarification_answers,
                )
                source_package = _ensure_grow_source_preview_raw_input(source_package, payload)
                resume_token = str(payload.resume_token or "")
            else:
                source_package = deepcopy(prepared_source_package) if prepared_source_package is not None else _grow_source_package(payload, brain_id)
                investigation_id = str(
                    source_package.get("investigation_id") or f"mcp-grow-{uuid.uuid4()}"
                )
                source_package["investigation_id"] = investigation_id
                persisted = reserve_grow_investigation(
                    brain_id=brain_id,
                    investigation_id=investigation_id,
                    source_investigation=source_package,
                    brain_revision=preview_brain_revision,
                )
                source_package = _grow_v3_source_without_legacy_semantic_questions(
                    deepcopy(dict(persisted["source_investigation"]))
                )
                source_package = _ensure_grow_source_preview_raw_input(source_package, payload)
                resume_token = str(persisted["resume_token"])
        except TrustedSourcePackageError as exc:
            return _grow_blocked(
                tool_name,
                brain_id,
                exc.code,
                started,
                source_investigation=exc.source_package,
            )
        except (GrowPreviewBindingStoreError, sqlite3.DatabaseError) as exc:
            reason = str(getattr(exc, "code", "") or exc or "grow_investigation_persistence_failed")
            return _grow_blocked(tool_name, brain_id, reason, started)

    investigation_id = str(persisted.get("investigation_id") or "")
    if investigation_id:
        source_package["investigation_id"] = investigation_id
        source_package["source_investigation_id"] = investigation_id
    source_package["brain_id"] = brain_id
    source_units = [
        dict(unit)
        for unit in list(source_package.get("source_units") or [])
        if isinstance(unit, dict)
    ]
    source_units_require_fetched_proof = any(_trusted_source_unit_requires_fetched_proof(unit) for unit in source_units)
    external_source = (
        _grow_payload_is_external_source(payload) and source_units_require_fetched_proof
        if not resume_requested
        else source_units_require_fetched_proof
    )
    source_has_fact_evidence = any(
        bool(unit.get("fact_eligible"))
        and bool(str(unit.get("raw_text") or unit.get("text") or "").strip())
        for unit in source_units
    )
    acquired_web_evidence = any(_grow_unit_has_fetched_acquisition_proof(unit) for unit in source_units)
    manual_evidence = any(
        str(unit.get("kind") or "") == "manual_block"
        and bool(unit.get("fact_eligible"))
        and bool(str(unit.get("raw_text") or unit.get("text") or "").strip())
        for unit in source_units
    )
    if not source_has_fact_evidence or (external_source and not acquired_web_evidence and not manual_evidence):
        return _grow_blocked(
            tool_name,
            brain_id,
            "rich_extraction_required",
            started,
            source_investigation=source_package,
        )

    intake_questions: list[dict[str, Any]] = []
    seen_intake_question_ids: set[str] = set()

    def add_intake_question(candidate: Any) -> None:
        if not isinstance(candidate, dict):
            return
        question = candidate.get("question") if isinstance(candidate.get("question"), dict) else candidate
        if not isinstance(question, dict):
            return
        candidate_status = str(candidate.get("status") or question.get("status") or "").strip().lower()
        if candidate_status in {
            "answered",
            "closed",
            "resolved",
            "defaulted",
            "defaulted_non_blocking",
            "not_required",
        }:
            return
        if candidate.get("answer") is not None or question.get("answer") is not None:
            return
        normalized_question = _grow_client_clarification_question(
            candidate,
            allow_default_text=False,
        )
        if normalized_question is None:
            return
        question_id = str(normalized_question.get("question_id") or "").strip()
        if question_id in seen_intake_question_ids:
            return
        seen_intake_question_ids.add(question_id)
        intake_questions.append(normalized_question)

    for item in list(source_package.get("clarification_questions") or []):
        add_intake_question(item)
    for item in list(source_package.get("open_questions") or []):
        add_intake_question(item)
    formation_gate = dict(
        dict(source_package.get("source_formation_contract") or {}).get("question_gate") or {}
    )
    for item in list(formation_gate.get("pending_questions") or []):
        add_intake_question(item)
    guided_grow = dict(source_package.get("guided_grow") or {})
    for item in list(guided_grow.get("pending_questions") or []):
        add_intake_question(item)
    for item in list(source_package.get("questions_and_answers") or []):
        add_intake_question(item)
    intake_questions = _grow_apply_question_limit(
        intake_questions,
        options.question_limit,
    )
    intake_questions, defaulted_intake_questions = _filter_non_blocking_grow_source_questions(
        intake_questions,
        source_material_ready=_grow_source_package_has_source_material(source_package),
        source_identity_ready=_grow_source_package_has_source_identity(source_package),
    )
    if defaulted_intake_questions:
        existing_defaulted = [
            dict(item)
            for item in list(source_package.get("non_blocking_clarification_questions") or [])
            if isinstance(item, dict)
        ]
        source_package["non_blocking_clarification_questions"] = [
            *existing_defaulted,
            *defaulted_intake_questions,
        ]
    intake_questions = _grow_apply_question_limit(
        intake_questions,
        options.question_limit,
    )
    if (
        intake_questions
        and not resume_requested
        and not clarification_answers
        and not bool(options.semantic_preview)
    ):
        investigation = deepcopy(dict(persisted.get("investigation") or {}))
        investigation.update(
            {
                "schema_version": "agvm.grow_investigation.v3",
                "investigation_id": investigation_id,
                "source_investigation_id": investigation_id,
                "brain_id": brain_id,
                "brain_revision": preview_brain_revision,
                "source_sha256": str(persisted.get("source_sha256") or ""),
                "state": "awaiting_clarification",
                "status": "ASKING_CLARIFICATION",
                "complete": False,
                "applicable": False,
                "questions": intake_questions,
                "pending_questions": intake_questions,
            }
        )
        try:
            with use_runtime_brain(brain_record):
                persisted = update_grow_investigation(
                    brain_id=brain_id,
                    investigation_id=investigation_id,
                    resume_token=resume_token,
                    expected_version=int(persisted.get("version") or 0),
                    investigation=investigation,
                    state="awaiting_clarification",
                )
        except (GrowPreviewBindingStoreError, sqlite3.DatabaseError) as exc:
            reason = str(getattr(exc, "code", "") or exc or "grow_investigation_persistence_failed")
            return _grow_blocked(tool_name, brain_id, reason, started, source_investigation=source_package)
        source_formation_contract = {
            "schema_version": "agvm.core_source_formation_contract.v1",
            "mode": "grow_source_intake",
            "state": "awaiting_clarification",
            "mutates_memory": False,
            "investigation_id": investigation_id,
            "apply_contract": {
                "can_apply_now": False,
                "blocked_reasons": ["clarification_required"],
                "selected_preview_ids": [],
            },
        }
        return McpGrowToolExecutionResponse(
            schema_version="agvm.mcp_grow_tool_output.v1",
            brain_id=brain_id,
            investigation_id=investigation_id,
            tool_name=tool_name,
            status="asking_clarification",
            can_apply_now=False,
            selected_preview_ids=[],
            source_investigation=source_package,
            source_formation_contract=source_formation_contract,
            memory_operation_lifecycle_contract={
                "schema_version": "agvm.memory_operation_lifecycle_contract.v3",
                "operation": "grow",
                "phase": "awaiting_clarification",
                "next_action": "resume with investigation_id, resume_token and answers",
            },
            clarification_request={"questions": intake_questions},
            clarification_questions=intake_questions,
            compiler_handoff_proof=deepcopy(dict(source_package.get("compiler_handoff_proof") or {})),
            investigation=deepcopy(dict(persisted.get("investigation") or {})),
            investigation_session={
                "schema_version": "agvm.investigation_session.v3",
                "status": "awaiting_answers",
                "question_count": len(intake_questions),
                "question_limit": int(options.question_limit or len(intake_questions)),
                "provider_attested": False,
                "investigation_version": int(persisted.get("version") or 0),
            },
            resume_token=resume_token,
            investigation_version=int(persisted.get("version") or 0),
            completeness={
                "preview_generated": False,
                "investigation_complete": False,
                "investigation_applicable": False,
                "question_count": len(intake_questions),
            },
            mcp_latency_profile={"elapsed_ms": int((time.perf_counter() - started) * 1000)},
            budget={"credits_required": 0, "runtime": "local_core"},
            next_action="resume with investigation_id, resume_token and answers",
        )

    provider_fast_block_reason = _grow_runtime_provider_fast_block_reason()
    if semantic_provider_required and provider_fast_block_reason:
        return _grow_blocked(
            tool_name,
            brain_id,
            provider_fast_block_reason,
            started,
            source_investigation=source_package,
            source_formation_contract={
                "schema_version": "agvm.core_source_formation_contract.v3",
                "mode": "grow_ai_investigation",
                "state": "blocked",
                "mutates_memory": False,
                "investigation_id": investigation_id,
                "blocked_reason": provider_fast_block_reason,
                "provider_required": True,
                "semantic_fallback_allowed": False,
                "fast_block": True,
            },
        )

    with use_runtime_brain(brain_record):
        index_payload = build_index(list(graph.get("nodes") or []))
        atlas_payload = fetch_atlas()

    try:
        # The unified Search runtime used by Grow resolves document/node
        # hydration through the active runtime store.  Keep the complete
        # investigator loop inside the same brain scope as the immutable graph
        # snapshot so multi-brain runs cannot read a process-global default.
        preview_kwargs = {
            "raw_input": _grow_source_preview_raw_input(source_package, payload),
            "input_mode": _input_mode_for_grow(payload, source_package),
            "graph": graph,
            "index_payload": index_payload,
            "atlas_payload": atlas_payload,
            "source_label": _grow_source_label_for_preview(payload, source_package),
            "source_uri": _grow_source_uri_for_preview(payload, source_package),
            "source_type": _source_type_for_grow(payload, source_package),
            "source_trust": str(options.source_trust or "unknown"),
            "learning_mode": "guided_learning" if options.pause_on_questions else "strict_review",
            "question_limit": options.question_limit,
            "source_investigation_id": investigation_id,
            "source_purpose": str(source_package.get("source_purpose") or payload.user_instruction or "") or None,
            "operator_instruction": payload.user_instruction,
            "source_sections": _source_sections_for_grow(source_package),
            "source_unit_formation": dict(source_package.get("source_unit_formation") or {}),
            "source_context": source_package,
            "clarification_answers": clarification_answers,
            "compiler_timeout_seconds": options.compiler_preview_timeout_seconds,
            "brain_revision": preview_brain_revision,
            "investigation": deepcopy(dict(persisted.get("investigation") or {})),
            "correlation_id": f"grow-investigation::{investigation_id}",
            "parent_operation_id": f"grow-preview::{investigation_id}",
        }
        with use_runtime_brain(brain_record):
            engine_result = _run_grow_engine_preview_with_hard_timeout(
                options=options,
                investigation_id=investigation_id,
                brain_id=brain_id,
                preview_brain_revision=preview_brain_revision,
                preview_kwargs=preview_kwargs,
            )
    except (ValueError, AiModuleContractError) as exc:
        reason = str(getattr(exc, "code", "") or exc or "grow_ai_unavailable")
        if not resume_requested:
            try:
                with use_runtime_brain(brain_record):
                    discard_grow_investigation_reservation(
                        brain_id=brain_id,
                        investigation_id=investigation_id,
                        resume_token=resume_token,
                    )
            except (GrowPreviewBindingStoreError, sqlite3.DatabaseError):
                pass
        return _grow_blocked(
            tool_name,
            brain_id,
            reason,
            started,
            source_investigation=source_package,
        )

    investigated = deepcopy(dict(engine_result.get("investigation") or {}))
    investigated.update(
        {
            "schema_version": "agvm.grow_investigation.v3",
            "investigation_id": investigation_id,
            "source_investigation_id": investigation_id,
            "brain_id": brain_id,
            "brain_revision": preview_brain_revision,
            "source_sha256": str(persisted.get("source_sha256") or ""),
        }
    )
    maintenance_feedback_packets = _maintenance_feedback_packets_from_engine_result(engine_result)
    questions = [
        question
        for question in (
            _grow_client_clarification_question(item)
            for item in list(engine_result.get("clarification_questions") or [])
        )
        if isinstance(question, dict)
    ]
    if questions and not bool(options.pause_on_questions):
        retained_engine_questions, defaulted_engine_questions = _filter_non_blocking_grow_source_questions(
            questions,
            source_material_ready=_grow_source_package_has_source_material(source_package),
            source_identity_ready=_grow_source_package_has_source_identity(source_package),
        )
        if defaulted_engine_questions:
            existing_defaulted = [
                dict(item)
                for item in list(source_package.get("non_blocking_clarification_questions") or [])
                if isinstance(item, dict)
            ]
            source_package["non_blocking_clarification_questions"] = [
                *existing_defaulted,
                *defaulted_engine_questions,
            ]
        questions = retained_engine_questions
    questions = _grow_apply_question_limit(questions, options.question_limit)
    if not questions and not maintenance_feedback_packets and not semantic_provider_required:
        source_gate = dict(
            dict(source_package.get("source_formation_contract") or {}).get("question_gate") or {}
        )
        source_question_candidates: list[Any] = [
            *list(source_package.get("clarification_questions") or []),
            *list(source_package.get("open_questions") or []),
            *list(source_gate.get("pending_questions") or []),
            *list(dict(source_package.get("guided_grow") or {}).get("pending_questions") or []),
        ]
        seen_source_question_ids: set[str] = set()
        for candidate in source_question_candidates:
            if not isinstance(candidate, dict):
                continue
            question_id = str(candidate.get("question_id") or "").strip()
            if not question_id or question_id in seen_source_question_ids:
                continue
            if candidate.get("answer") is not None:
                continue
            seen_source_question_ids.add(question_id)
            normalized_question = _grow_client_clarification_question(candidate)
            if normalized_question is not None:
                questions.append(normalized_question)
        if questions:
            retained_source_questions, defaulted_source_questions = _filter_non_blocking_grow_source_questions(
                questions,
                source_material_ready=_grow_source_package_has_source_material(source_package),
                source_identity_ready=_grow_source_package_has_source_identity(source_package),
            )
            if defaulted_source_questions:
                existing_defaulted = [
                    dict(item)
                    for item in list(source_package.get("non_blocking_clarification_questions") or [])
                    if isinstance(item, dict)
                ]
                source_package["non_blocking_clarification_questions"] = [
                    *existing_defaulted,
                    *defaulted_source_questions,
                ]
            questions = retained_source_questions
        questions = _grow_apply_question_limit(questions, options.question_limit)
    if (
        not questions
        and _grow_provider_failure_is_source_bound_recoverable(engine_result, investigated)
    ):
        if not semantic_provider_required:
            return _grow_source_bound_recovery_preview_response(
                tool_name,
                brain_record=brain_record,
                brain_id=brain_id,
                original_investigation_id=investigation_id,
                source_package=source_package,
                engine_result=engine_result,
                investigation=investigated,
                preview_brain_revision=preview_brain_revision,
                started=started,
            )
        reason = _grow_provider_failure_block_reason(engine_result, investigated)
        return _grow_blocked(
            tool_name,
            brain_id,
            reason,
            started,
            source_investigation=source_package,
            source_formation_contract={
                "schema_version": "agvm.core_source_formation_contract.v3",
                "mode": "grow_ai_investigation",
                "state": "blocked",
                "mutates_memory": False,
                "investigation_id": investigation_id,
                "blocked_reason": reason,
                "provider_required": True,
                "semantic_fallback_allowed": False,
            },
        )
    bundle = _grow_source_bound_preview_with_claim_authority(
        deepcopy(_preview_bundle_from_engine_result(engine_result)),
        investigation_id=investigation_id,
        source_package=source_package,
    )
    if (
        not questions
        and _grow_engine_result_can_publish_preview(engine_result, preview_bundle=bundle)
    ):
        investigated["complete"] = True
        investigated["applicable"] = True
        investigated["state"] = str(investigated.get("state") or "preview_ready")
        investigated["status"] = str(investigated.get("status") or "PREVIEW_READY")
        investigated.setdefault("version", int(persisted.get("version") or 0))
        investigated.setdefault("parent_operation_id", f"grow-preview::{investigation_id}")
        if engine_result.get("ai_execution_ledger") and not investigated.get("ai_execution_ledger"):
            investigated["ai_execution_ledger"] = [
                deepcopy(dict(item))
                for item in list(engine_result.get("ai_execution_ledger") or [])
                if isinstance(item, dict)
            ]
        compiler_attestation = dict(engine_result.get("ai_execution_attestation") or {})
        canonical_ledger = [
            deepcopy(dict(item))
            for item in list(investigated.get("ai_execution_ledger") or [])
            if isinstance(item, dict) and str(item.get("entry_sha256") or "").strip()
        ]
        has_canonical_compiler_entry = any(
            str(item.get("role") or "").strip() == "compiler"
            for item in canonical_ledger
        )
        if compiler_attestation and not has_canonical_compiler_entry:
            # Older engines exposed an informational role/status list instead
            # of the signed execution ledger required by durable Apply.  Do not
            # persist those unsigned rows as authority; bind the attested
            # compiler execution into the canonical ledger instead.
            investigated["ai_execution_ledger"] = canonical_ledger
            investigated = _append_grow_v3_compiler_execution(
                investigated,
                compiler_attestation=compiler_attestation,
                preview_bundle=bundle,
            )
    if questions or not bool(investigated.get("complete")) or not bool(investigated.get("applicable")):
        persistence_state = "awaiting_clarification" if questions else "investigating"
        engine_session = dict(engine_result.get("investigation_session") or {})
        engine_attestation = dict(engine_result.get("ai_execution_attestation") or {})
        investigated_attestation = dict(investigated.get("ai_execution_attestation") or {})
        response_attestation = (
            engine_attestation
            if _grow_provider_attested(engine_session, engine_attestation)
            else investigated_attestation or engine_attestation
        )
        if questions:
            normalized_questions = [
                question
                for question in (
                    _grow_client_clarification_question(item)
                    for item in list(questions or [])
                    if isinstance(item, dict)
                )
                if isinstance(question, dict)
            ]
            questions = normalized_questions
            investigated.update(
                {
                    "state": "awaiting_clarification",
                    "status": "ASKING_CLARIFICATION",
                    "complete": False,
                    "applicable": False,
                    "questions": questions,
                    "pending_questions": questions,
                }
            )
            source_package = _grow_source_package_with_public_question_gate(
                source_package,
                questions=questions,
                resume_token=resume_token,
            )
        try:
            with use_runtime_brain(brain_record):
                persisted = update_grow_investigation(
                    brain_id=brain_id,
                    investigation_id=investigation_id,
                    resume_token=resume_token,
                    expected_version=int(persisted.get("version") or 0),
                    investigation=investigated,
                    state=persistence_state,
                )
        except (GrowPreviewBindingStoreError, sqlite3.DatabaseError) as exc:
            reason = str(getattr(exc, "code", "") or exc or "grow_investigation_persistence_failed")
            return _grow_blocked(tool_name, brain_id, reason, started)
        source_formation_contract = {
            "schema_version": "agvm.core_source_formation_contract.v1",
            "mode": "grow_ai_investigation",
            "state": persistence_state,
            "mutates_memory": False,
            "investigation_id": investigation_id,
            "apply_contract": {
                "can_apply_now": False,
                "blocked_reasons": [
                    "clarification_required" if questions else "investigation_incomplete"
                ],
                "selected_preview_ids": [],
            },
        }
        return McpGrowToolExecutionResponse(
            schema_version="agvm.mcp_grow_tool_output.v1",
            brain_id=brain_id,
            investigation_id=investigation_id,
            tool_name=tool_name,
            status="asking_clarification" if questions else "failed",
            can_apply_now=False,
            selected_preview_ids=[],
            source_investigation=source_package,
            source_formation_contract=source_formation_contract,
            memory_operation_lifecycle_contract={
                "schema_version": "agvm.memory_operation_lifecycle_contract.v3",
                "operation": "grow",
                "phase": persistence_state,
                "next_action": "resume with investigation_id, resume_token and answers" if questions else None,
            },
            clarification_questions=questions,
            ai_execution_attestation=response_attestation,
            ai_execution_ledger=list(investigated.get("ai_execution_ledger") or []),
            investigation=deepcopy(dict(persisted.get("investigation") or {})),
            investigation_session=(
                {
                    **deepcopy(engine_session),
                    "schema_version": "agvm.investigation_session.v3",
                    "status": "awaiting_answers",
                    "question_count": len(questions),
                    "question_limit": int(options.question_limit or len(questions)),
                    "provider_attested": _grow_provider_attested(
                        engine_session,
                        response_attestation,
                    ),
                    "investigation_version": int(persisted.get("version") or 0),
                }
                if questions
                else deepcopy(engine_session)
            ),
            maintenance_feedback_packets=maintenance_feedback_packets,
            resume_token=resume_token,
            investigation_version=int(persisted.get("version") or 0),
            usage=dict(engine_result.get("usage") or investigated.get("usage") or {}),
            completeness={
                "preview_generated": False,
                "investigation_complete": bool(investigated.get("complete")),
                "investigation_applicable": bool(investigated.get("applicable")),
                "question_count": len(questions),
            },
            mcp_latency_profile={"elapsed_ms": int((time.perf_counter() - started) * 1000)},
            budget={"credits_required": 0, "runtime": "local_core"},
            next_action="resume with investigation_id, resume_token and answers" if questions else None,
        )

    if not bundle:
        if maintenance_feedback_packets:
            source_formation_contract = _maintenance_deferred_source_contract(
                investigation_id,
                maintenance_feedback_packets,
            )
            try:
                with use_runtime_brain(brain_record):
                    finalized = finalize_grow_maintenance_feedback(
                        brain_id=brain_id,
                        investigation_id=investigation_id,
                        resume_token=resume_token,
                        expected_version=int(persisted.get("version") or 0),
                        tool_name=tool_name,
                        source_investigation=source_package,
                        source_formation_contract=source_formation_contract,
                        investigation=investigated,
                        investigation_session=dict(engine_result.get("investigation_session") or {}),
                        expected_brain_revision=preview_brain_revision,
                        maintenance_feedback_packets=maintenance_feedback_packets,
                    )
            except (GrowPreviewBindingStoreError, AiModuleContractError, sqlite3.DatabaseError) as exc:
                reason = str(getattr(exc, "code", "") or exc or "grow_v3_maintenance_feedback_persistence_failed")
                return _grow_blocked(tool_name, brain_id, reason, started)
            _GROW_PREVIEW_RUNS[investigation_id] = {
                **deepcopy(finalized),
                "status": "needs_review",
            }
            return _grow_response_from_maintenance_deferred(
                tool_name,
                finalized,
                started=started,
                resume_token=resume_token,
            )
        return _grow_blocked(
            tool_name,
            brain_id,
            "preview_bridge_preview_bundle_not_attached",
            started,
            source_investigation=source_package,
            source_formation_contract={
                "schema_version": "agvm.core_source_formation_contract.v3",
                "mode": "grow_ai_investigation",
                "state": "blocked",
                "mutates_memory": False,
                "investigation_id": investigation_id,
                "apply_contract": {
                    "preview_required": True,
                    "explicit_confirm_apply_required": True,
                    "explicit_selection_required": True,
                    "apply_without_preview_allowed": False,
                    "can_apply_now": False,
                    "blocked_reasons": ["preview_bridge_preview_bundle_not_attached"],
                    "selected_preview_ids": [],
                },
            },
        )
    selected_preview_ids = _selected_preview_ids(bundle, [])
    compiler_handoff_proof = build_source_compiler_handoff_proof(source_package, bundle)
    source_package["compiler_handoff_proof"] = compiler_handoff_proof
    source_formation_contract = {
        **build_source_formation_contract(source_package, bundle),
        "schema_version": "agvm.core_source_formation_contract.v3",
        "mode": "grow_ai_investigation",
        "state": "preview_ready",
        "mutates_memory": False,
        "investigation_id": investigation_id,
        "apply_contract": {
            "preview_required": True,
            "explicit_confirm_apply_required": True,
            "explicit_selection_required": True,
            "apply_without_preview_allowed": False,
            "can_apply_now": bool(selected_preview_ids),
            "blocked_reasons": [] if selected_preview_ids else ["preview_bundle_missing"],
            "selected_preview_ids": selected_preview_ids,
        },
    }
    try:
        with use_runtime_brain(brain_record):
            finalized = finalize_grow_preview(
                brain_id=brain_id,
                investigation_id=investigation_id,
                resume_token=resume_token,
                expected_version=int(persisted.get("version") or 0),
                tool_name=tool_name,
                source_investigation=source_package,
                source_formation_contract=source_formation_contract,
                investigation=investigated,
                preview_bundle=bundle,
                ai_execution_attestation=dict(engine_result.get("ai_execution_attestation") or {}),
                investigation_session=dict(engine_result.get("investigation_session") or {}),
                maintenance_feedback_packets=maintenance_feedback_packets,
                expected_brain_revision=preview_brain_revision,
            )
    except (GrowPreviewBindingStoreError, AiModuleContractError, sqlite3.DatabaseError) as exc:
        reason = str(getattr(exc, "code", "") or exc or "grow_v3_preview_persistence_failed")
        return _grow_blocked(tool_name, brain_id, reason, started, preview_bundle=bundle)
    canonical_investigation = deepcopy(dict(finalized.get("investigation") or {}))
    return McpGrowToolExecutionResponse(
        schema_version="agvm.mcp_grow_tool_output.v1",
        brain_id=brain_id,
        investigation_id=investigation_id,
        tool_name=tool_name,
        status="preview_ready",
        source_investigation=source_package,
        source_formation_contract=source_formation_contract,
        memory_operation_lifecycle_contract={
            "schema_version": "agvm.memory_operation_lifecycle_contract.v3",
            "operation": "grow",
            "phase": "preview_ready",
            "next_action": "explicitly select preview IDs and call grow_apply with confirm_apply=true",
        },
        preview_bundle=bundle,
        maintenance_feedback_packets=maintenance_feedback_packets,
        clarification_questions=[],
        compiler_handoff_proof=compiler_handoff_proof,
        ai_execution_attestation=deepcopy(dict(finalized.get("ai_execution_attestation") or {})),
        ai_execution_ledger=list(canonical_investigation.get("ai_execution_ledger") or []),
        investigation=canonical_investigation,
        investigation_session=deepcopy(dict(finalized.get("investigation_session") or {})),
        resume_token=resume_token,
        investigation_version=int(finalized.get("version") or 0),
        usage=dict(canonical_investigation.get("usage") or {}),
        cognitive_write_plan=dict(bundle.get("cognitive_write_plan") or {}),
        learning_policy=dict(bundle.get("learning_policy") or {}),
        write_trace=dict(bundle.get("write_trace") or {}),
        completeness={
            "preview_generated": True,
            "investigation_complete": True,
            "investigation_applicable": True,
            "ai_execution_attested": True,
            "preview_node_count": len(selected_preview_ids),
            "selected_preview_count": 0,
            "source_unit_count": len(source_units),
        },
        mcp_latency_profile={"elapsed_ms": int((time.perf_counter() - started) * 1000)},
        budget={"credits_required": 0, "runtime": "local_core"},
    )


def _grow_source_preview(tool_name: str, payload: McpGrowSourceRequest) -> McpGrowToolExecutionResponse:
    brain_record, payload = _resolve_grow_request_scope(payload)
    if _grow_source_bound_storage_only_requested(payload):
        return _grow_source_bound_storage_only_preview(
            tool_name,
            payload,
            brain_record=brain_record,
        )
    if (
        payload.run_preview
        and not bool(getattr(payload.options, "semantic_preview", False))
        and not bool(getattr(payload.options, "pause_on_questions", False))
        and _deterministic_public_text_eligible(payload)
    ):
        return _grow_deterministic_public_text_preview(
            tool_name,
            payload,
            brain_record=brain_record,
        )
    if payload.run_preview:
        return _grow_source_preview_v3(tool_name, payload, brain_record=brain_record)
    started = time.perf_counter()
    brain_id = _brain_record_id(brain_record)
    try:
        source_package = _grow_source_package(payload, brain_id)
    except TrustedSourcePackageError as exc:
        return _grow_blocked(
            tool_name,
            brain_id,
            exc.code,
            started,
            source_investigation=exc.source_package,
        )
    investigation_id = str(source_package.get("investigation_id") or f"mcp-grow-{uuid.uuid4()}")
    source_package["investigation_id"] = investigation_id
    source_units = [dict(unit) for unit in list(source_package.get("source_units") or []) if isinstance(unit, dict)]
    source_units_require_fetched_proof = any(_trusted_source_unit_requires_fetched_proof(unit) for unit in source_units)
    external_source = _grow_payload_is_external_source(payload) and source_units_require_fetched_proof
    if not source_units and not external_source and str(payload.raw_input or "").strip():
        source_units = [_local_core_source_unit(payload, investigation_id)]
        source_package["source_units"] = source_units
    source_unit = source_units[0] if source_units else {}
    source_has_fact_evidence = any(
        bool(unit.get("fact_eligible"))
        and bool(str(unit.get("raw_text") or unit.get("text") or "").strip())
        for unit in source_units
    )
    acquired_web_evidence = any(_grow_unit_has_fetched_acquisition_proof(unit) for unit in source_units)
    manual_evidence = any(
        str(unit.get("kind") or "") == "manual_block"
        and bool(unit.get("fact_eligible"))
        and bool(str(unit.get("raw_text") or unit.get("text") or "").strip())
        for unit in source_units
    )
    source_requires_rich_extraction = str(source_package.get("status") or "") == "rich_extraction_required"
    if (source_requires_rich_extraction and not source_has_fact_evidence) or (
        external_source and not acquired_web_evidence and not manual_evidence
    ):
        return _grow_blocked(
            tool_name,
            brain_id,
            "rich_extraction_required",
            started,
            source_investigation=source_package,
            source_formation_contract={
                "schema_version": "agvm.core_source_formation_contract.v2",
                "mode": "source_evidence_preflight",
                "state": "blocked",
                "mutates_memory": False,
                "blocked_reason": "rich_extraction_required",
            },
        )
    if not payload.run_preview:
        source_investigation = source_package
        compiler_handoff_proof = build_source_compiler_handoff_proof(source_package)
        source_investigation["compiler_handoff_proof"] = compiler_handoff_proof
        source_formation_contract = {
            "schema_version": "agvm.core_source_formation_contract.v1",
            "mode": "local_core_source_unit_proof",
            "mutates_memory": False,
            "apply_requires_confirm_apply": True,
            "state": "handoff_ready",
            "source_kind": str(payload.input_kind or "auto"),
            "investigation_id": investigation_id,
            "apply_contract": {
                "preview_required": True,
                "explicit_confirm_apply_required": True,
                "apply_without_preview_allowed": False,
                "can_apply_now": False,
                "blocked_reasons": ["preview_bundle_missing"],
                "selected_preview_ids": [],
            },
        }
        latency_profile = {
            "schema_version": "agvm.mcp_grow_latency_profile.v1",
            "mode": "source_unit_only",
            "source_unit_only": True,
            "source_unit_proof_ready": True,
            "source_unit_count": 1,
            "compiler_handoff_visible": True,
            "preview_eligible": True,
            "full_preview_present": False,
            "apply_requires_preview_bundle": True,
            "apply_ready": False,
            "recommended_follow_up": "grow_source_preview",
            "recommended_follow_up_payload_patch": {"run_preview": True},
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }
        _GROW_PREVIEW_RUNS[investigation_id] = {
            "brain_id": brain_id,
            "status": "preview_ready",
            "source_investigation": source_investigation,
            "source_formation_contract": source_formation_contract,
            "preview_bundle": None,
            "preview_fingerprint": None,
            "apply_receipt": None,
        }
        return McpGrowToolExecutionResponse(
            schema_version="agvm.mcp_grow_tool_output.v1",
            brain_id=brain_id,
            tool_name=tool_name,
            status="preview_ready",
            can_apply_now=False,
            selected_preview_ids=[],
            source_investigation=source_investigation,
            source_formation_contract=source_formation_contract,
            memory_operation_lifecycle_contract={
                "schema_version": "agvm.memory_operation_lifecycle_contract.v1",
                "operation": "grow",
                "phase": "source_unit_proof",
                "next_action": "call grow_source_preview with run_preview=true before apply",
            },
            compiler_handoff_proof=compiler_handoff_proof,
            completeness={
                "preview_generated": False,
                "preview_present": False,
                "preview_node_count": 0,
                "selected_preview_count": 0,
                "source_status": "source_units_ready",
                "source_unit_count": len(source_units),
            },
            mcp_latency_profile=latency_profile,
            budget={"credits_required": 0, "runtime": "local_core"},
            next_action="call grow_source_preview with run_preview=true",
        )
    with use_runtime_brain(brain_record):
        bootstrap_runtime_store()
        graph = fetch_graph_snapshot()
        preview_brain_revision = maintenance_graph_revision(graph)
        index_payload = build_index(list(graph.get("nodes") or []))
        atlas_payload = fetch_atlas()
        options = payload.options
        try:
            engine_result = _GROW_ENGINE.preview(
                raw_input=payload.raw_input,
                input_mode=_input_mode_for_grow(payload, source_package),
                graph=graph,
                index_payload=index_payload,
                atlas_payload=atlas_payload,
                source_label=payload.source_label,
                source_uri=payload.source_uri,
                source_type=_source_type(payload),
                source_trust=str(options.source_trust or "unknown"),
                learning_mode="guided_learning" if options.pause_on_questions else "strict_review",
                question_limit=options.question_limit,
                source_investigation_id=investigation_id,
                source_purpose=payload.user_instruction,
                operator_instruction=payload.user_instruction,
                source_sections=_source_sections_for_grow(source_package),
                source_unit_formation=dict(source_package.get("source_unit_formation") or {}),
                source_context=source_package,
                clarification_answers=options.clarification_answers,
                compiler_timeout_seconds=options.compiler_preview_timeout_seconds,
            )
            bundle = _preview_bundle_from_engine_result(engine_result)
            attestation = dict(engine_result["ai_execution_attestation"])
            clarification_questions = list(engine_result["clarification_questions"])
            clarification_questions = _grow_apply_question_limit(
                [
                    question
                    for question in (
                        _grow_client_clarification_question(item)
                        for item in clarification_questions
                    )
                    if isinstance(question, dict)
                ],
                options.question_limit,
            )
        except (ValueError, AiModuleContractError) as exc:
            reason = str(getattr(exc, "code", "") or exc or "grow_ai_unavailable")
            if not reason.startswith("grow_ai_") and not reason.startswith("ai_execution_"):
                reason = f"grow_ai_unavailable:{reason}"
            return _grow_blocked(tool_name, brain_id, reason, started)
    if not bundle:
        return _grow_blocked(
            tool_name,
            brain_id,
            "preview_bridge_preview_bundle_not_attached",
            started,
            source_investigation=source_package,
            source_formation_contract={
                "schema_version": "agvm.core_source_formation_contract.v2",
                "mode": "shared_ai_grow_preview",
                "mutates_memory": False,
                "state": "blocked",
                "source_kind": str(payload.input_kind or "auto"),
                "investigation_id": investigation_id,
                "apply_contract": {
                    "preview_required": True,
                    "explicit_confirm_apply_required": True,
                    "apply_without_preview_allowed": False,
                    "can_apply_now": False,
                    "blocked_reasons": ["preview_bridge_preview_bundle_not_attached"],
                    "selected_preview_ids": [],
                },
            },
        )
    if len(source_units) == 1:
        bundle = _bind_preview_bundle_to_source_unit(bundle, source_unit)
    selected_preview_ids = _selected_preview_ids(bundle, [])
    compiler_handoff_proof = build_source_compiler_handoff_proof(source_package, bundle)
    source_package["compiler_handoff_proof"] = compiler_handoff_proof
    source_investigation = source_package
    if clarification_questions:
        source_investigation = _grow_source_package_with_public_question_gate(
            source_investigation,
            questions=clarification_questions,
            resume_token=resume_token,
        )
    source_formation_contract = {
        **build_source_formation_contract(source_package, bundle),
        "schema_version": "agvm.core_source_formation_contract.v2",
        "mode": "shared_ai_grow_preview",
        "mutates_memory": False,
        "apply_requires_confirm_apply": True,
        "state": "awaiting_clarification" if clarification_questions else "preview_ready",
        "source_kind": str(payload.input_kind or "auto"),
        "investigation_id": investigation_id,
        "apply_contract": {
            "preview_required": True,
            "explicit_confirm_apply_required": True,
            "apply_without_preview_allowed": False,
            "can_apply_now": bool(selected_preview_ids) and not clarification_questions,
            "blocked_reasons": (
                ["clarification_required"]
                if clarification_questions
                else [] if selected_preview_ids else ["preview_bundle_missing"]
            ),
            "selected_preview_ids": selected_preview_ids,
        },
    }
    with use_runtime_brain(brain_record):
        try:
            persisted_preview = store_local_grow_v2_preview(
                brain_id=brain_id,
                investigation_id=investigation_id,
                tool_name=tool_name,
                source_investigation=source_investigation,
                source_formation_contract=source_formation_contract,
                preview_bundle=bundle,
                ai_execution_attestation=attestation,
                investigation_session=dict(engine_result["investigation_session"]),
                expected_brain_revision=preview_brain_revision,
            )
        except (GrowPreviewBindingStoreError, AiModuleContractError) as exc:
            reason = str(getattr(exc, "code", "") or exc or "grow_v2_preview_persistence_failed")
            return _grow_blocked(tool_name, brain_id, reason, started)
    # Expose the canonical attestation bound to the durable preview. Status reads
    # this same representation, so both surfaces remain byte-for-byte equivalent.
    attestation = deepcopy(dict(persisted_preview["ai_execution_attestation"]))
    _GROW_PREVIEW_RUNS[investigation_id] = {
        "brain_id": brain_id,
        "status": "asking_clarification" if clarification_questions else "preview_ready",
        "source_investigation": source_investigation,
        "source_formation_contract": source_formation_contract,
        "preview_bundle": deepcopy(bundle),
        "preview_fingerprint": persisted_preview["preview_sha256"],
        "ai_execution_attestation": deepcopy(attestation),
        "attestation_fingerprint": persisted_preview["attestation_sha256"],
        "investigation_session": deepcopy(dict(engine_result["investigation_session"])),
        "apply_receipt": None,
    }
    return McpGrowToolExecutionResponse(
        schema_version="agvm.mcp_grow_tool_output.v1",
        brain_id=brain_id,
        tool_name=tool_name,
        status="asking_clarification" if clarification_questions else "preview_ready",
        can_apply_now=bool(selected_preview_ids) and not clarification_questions,
        selected_preview_ids=[] if clarification_questions else selected_preview_ids,
        source_investigation=source_investigation,
        source_formation_contract=source_formation_contract,
        memory_operation_lifecycle_contract={
            "schema_version": "agvm.memory_operation_lifecycle_contract.v1",
            "operation": "grow",
            "phase": "awaiting_clarification" if clarification_questions else "preview",
            "next_action": (
                "resume with investigation_id, resume_token and answers"
                if clarification_questions
                else "call grow_source_apply with confirm_apply=true and selected_preview_ids"
            ),
        },
        preview_bundle=bundle,
        clarification_questions=clarification_questions,
        compiler_handoff_proof=compiler_handoff_proof,
        ai_execution_attestation=attestation,
        investigation_session=(
            {
                **dict(engine_result["investigation_session"]),
                "schema_version": "agvm.investigation_session.v3",
                "status": "awaiting_answers",
                "question_count": len(clarification_questions),
                "question_limit": int(options.question_limit or len(clarification_questions)),
                "provider_attested": _grow_provider_attested(
                    dict(engine_result.get("investigation_session") or {}),
                    attestation,
                ),
                "investigation_version": int(persisted_preview.get("version") or 0),
            }
            if clarification_questions
            else dict(engine_result["investigation_session"])
        ),
        cognitive_write_plan=dict(bundle.get("cognitive_write_plan") or {}),
        learning_policy=dict(bundle.get("learning_policy") or {}),
        write_trace=dict(bundle.get("write_trace") or {}),
        completeness={
            "preview_generated": True,
            "ai_execution_attested": True,
            "preview_node_count": len(selected_preview_ids),
            "selected_preview_count": len(selected_preview_ids),
            "source_status": "source_units_ready",
            "source_unit_count": len(source_units),
        },
        mcp_latency_profile={"elapsed_ms": int((time.perf_counter() - started) * 1000)},
        budget={"credits_required": 0, "runtime": "local_core"},
    )


def _grow_source_apply(tool_name: str, payload: McpGrowApplyRequest) -> McpGrowToolExecutionResponse:
    with _GROW_PREVIEW_APPLY_LOCK:
        return _grow_source_apply_locked(tool_name, payload)


def _grow_sqlite_busy(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    sqlite_error_code = getattr(exc, "sqlite_errorcode", None)
    if sqlite_error_code in {
        getattr(sqlite3, "SQLITE_BUSY", 5),
        getattr(sqlite3, "SQLITE_LOCKED", 6),
    }:
        return True
    message = str(exc or "").strip().lower()
    return "locked" in message or "busy" in message


def _apply_local_grow_v2_with_busy_retry(**kwargs: Any) -> dict[str, Any]:
    retry_delays = (0.05, 0.2)
    for attempt in range(len(retry_delays) + 1):
        try:
            return apply_local_grow_v2_preview_transaction(**kwargs)
        except sqlite3.OperationalError as exc:
            if not _grow_sqlite_busy(exc) or attempt >= len(retry_delays):
                raise
            time.sleep(retry_delays[attempt])
    raise AssertionError("unreachable grow apply retry state")


def _grow_source_apply_locked(tool_name: str, payload: McpGrowApplyRequest) -> McpGrowToolExecutionResponse:
    started = time.perf_counter()
    investigation = dict(payload.source_investigation or {})
    request_investigation_id = str(payload.investigation_id or "").strip()
    embedded_investigation_id = str(investigation.get("investigation_id") or "").strip()
    if request_investigation_id and embedded_investigation_id and request_investigation_id != embedded_investigation_id:
        return _grow_blocked(tool_name, payload.brain_id, "investigation_id_mismatch", started)
    investigation_id = request_investigation_id or embedded_investigation_id
    if not investigation_id:
        return _grow_blocked(tool_name, payload.brain_id, "server_preview_investigation_required", started)
    requested_brain_id = str(payload.brain_id or investigation.get("brain_id") or "").strip() or None
    brain_record = _resolve_brain_record(requested_brain_id)
    resolved_brain_id = _brain_record_id(brain_record)
    try:
        with use_runtime_brain(brain_record):
            stored = fetch_local_grow_v2_preview(
                brain_id=resolved_brain_id,
                investigation_id=investigation_id,
            )
    except (GrowPreviewBindingStoreError, AiModuleContractError, sqlite3.DatabaseError) as exc:
        reason = str(getattr(exc, "code", "") or exc or "server_preview_invalid")
        return _grow_blocked(tool_name, resolved_brain_id, reason, started)
    if not stored:
        return _grow_blocked(tool_name, resolved_brain_id, "server_preview_not_found", started)
    stored_brain_id = str(stored.get("brain_id") or "").strip()
    if resolved_brain_id != stored_brain_id:
        return _grow_blocked(tool_name, resolved_brain_id, "server_preview_brain_mismatch", started)
    if str(stored.get("state") or "") == "maintenance_deferred":
        authenticated = _authenticated_v3_grow_record_or_response(
            tool_name=tool_name,
            brain_record=brain_record,
            brain_id=resolved_brain_id,
            investigation_id=investigation_id,
            resume_token=payload.resume_token,
            started=started,
        )
        if isinstance(authenticated, McpGrowToolExecutionResponse):
            return authenticated
        return _grow_response_from_maintenance_deferred(
            tool_name,
            authenticated,
            started=started,
            resume_token=payload.resume_token,
        )
    if str(stored.get("state") or "") in {"investigating", "awaiting_clarification"}:
        return _grow_blocked(tool_name, resolved_brain_id, "server_preview_not_ready", started)
    stored_investigation = deepcopy(dict(stored.get("investigation") or {}))
    if stored_investigation:
        stored_recovery_bundle = deepcopy(dict(stored.get("preview_bundle") or {}))
        stored_recovery_ids = _selected_preview_ids(stored_recovery_bundle, [])
        requested_recovery_ids = _normalized_grow_ids(payload.selected_preview_ids)
        server_apply_contract = dict(
            dict(stored.get("source_formation_contract") or {}).get("apply_contract") or {}
        )
        server_selected_ids = _normalized_grow_ids(
            list(server_apply_contract.get("selected_preview_ids") or [])
        )
        recovery_contract_id = str(
            dict(payload.source_formation_contract or {}).get("investigation_id") or ""
        ).strip()
        tokenless_exact_local_recovery = bool(
            not payload.resume_token
            and payload.confirm_apply
            and requested_brain_id
            and requested_brain_id == resolved_brain_id
            and str(stored.get("state") or "") == "active"
            and payload.investigation_version is not None
            and int(stored.get("version") or 0) == int(payload.investigation_version)
            and recovery_contract_id == investigation_id
            and payload.preview_bundle is not None
            and _grow_contract_fingerprint(payload.preview_bundle)
            == _grow_contract_fingerprint(stored_recovery_bundle)
            and requested_recovery_ids
            and requested_recovery_ids == stored_recovery_ids
        )
        tokenless_active_server_preview = bool(
            not payload.resume_token
            and payload.confirm_apply
            and requested_brain_id
            and requested_brain_id == resolved_brain_id
            and str(stored.get("state") or "") == "active"
            and bool(server_apply_contract.get("can_apply_now"))
            and server_selected_ids
            and not payload.preview_bundle
            and not requested_recovery_ids
        )
        if (
            not payload.resume_token
            and not payload.confirm_apply
            and (not requested_brain_id or requested_brain_id == resolved_brain_id)
            and str(stored.get("state") or "") == "active"
            and stored_recovery_bundle
        ):
            return _grow_blocked(
                tool_name,
                resolved_brain_id,
                "confirm_apply_required",
                started,
                preview_bundle=stored_recovery_bundle,
            )
        if not payload.resume_token and not (
            tokenless_exact_local_recovery or tokenless_active_server_preview
        ):
            return _grow_blocked(tool_name, resolved_brain_id, "resume_token_required", started)
        if payload.resume_token:
            try:
                with use_runtime_brain(brain_record):
                    authenticated = fetch_grow_investigation(
                        brain_id=resolved_brain_id,
                        investigation_id=investigation_id,
                        resume_token=payload.resume_token,
                    )
            except GrowPreviewBindingStoreError as exc:
                return _grow_blocked(
                    tool_name,
                    resolved_brain_id,
                    str(getattr(exc, "code", "") or exc),
                    started,
                )
            if not authenticated:
                return _grow_blocked(tool_name, resolved_brain_id, "server_preview_not_found", started)
            stored = dict(authenticated)
            stored_investigation = deepcopy(dict(stored.get("investigation") or {}))
        if (
            str(stored_investigation.get("schema_version") or "")
            == GROW_INVESTIGATION_V3_SCHEMA_VERSION
        ):
            effective_investigation_version = (
                int(stored.get("version") or 0)
                if tokenless_active_server_preview and payload.investigation_version is None
                else payload.investigation_version
            )
            if effective_investigation_version is None:
                return _grow_blocked(
                    tool_name,
                    resolved_brain_id,
                    "grow_investigation_version_required",
                    started,
                )
            if int(stored.get("version") or 0) != int(effective_investigation_version):
                return _grow_blocked(
                    tool_name,
                    resolved_brain_id,
                    "grow_investigation_version_conflict",
                    started,
                )
    bundle = deepcopy(dict(stored.get("preview_bundle") or {}))
    brain_id = stored_brain_id or None
    if not bundle:
        return _grow_blocked(tool_name, brain_id, "server_preview_bundle_required", started)
    stored_apply_contract = dict(
        dict(stored.get("source_formation_contract") or {}).get("apply_contract") or {}
    )
    if stored_apply_contract and not bool(stored_apply_contract.get("can_apply_now")):
        blocked_reasons = [
            str(item)
            for item in list(stored_apply_contract.get("blocked_reasons") or [])
            if str(item).strip()
        ]
        return _grow_blocked(
            tool_name,
            brain_id,
            blocked_reasons[0] if blocked_reasons else "preview_not_apply_ready",
            started,
            preview_bundle=bundle,
        )
    preview_fingerprint = str(stored.get("preview_fingerprint") or _grow_contract_fingerprint(bundle))
    if payload.preview_bundle is not None and _grow_contract_fingerprint(payload.preview_bundle) != preview_fingerprint:
        return _grow_blocked(tool_name, brain_id, "server_preview_bundle_mismatch", started, preview_bundle=bundle)
    contract_investigation_id = str(dict(payload.source_formation_contract or {}).get("investigation_id") or "").strip()
    if contract_investigation_id and contract_investigation_id != investigation_id:
        return _grow_blocked(tool_name, brain_id, "server_preview_contract_mismatch", started, preview_bundle=bundle)
    if not payload.confirm_apply:
        return _grow_blocked(tool_name, brain_id, "confirm_apply_required", started, preview_bundle=bundle)
    available_ids = _selected_preview_ids(bundle, [])
    requested_ids = _normalized_grow_ids(payload.selected_preview_ids)
    if stored_investigation and not requested_ids:
        stored_contract = dict(
            dict(stored.get("source_formation_contract") or {}).get("apply_contract") or {}
        )
        requested_ids = _normalized_grow_ids(list(stored_contract.get("selected_preview_ids") or []))
        if not requested_ids:
            return _grow_blocked(
                tool_name,
                brain_id,
                "explicit_selected_preview_ids_required",
                started,
                preview_bundle=bundle,
            )
    unknown_selected_ids = [node_id for node_id in requested_ids if node_id not in set(available_ids)]
    if unknown_selected_ids:
        return _grow_blocked(tool_name, brain_id, "selected_preview_ids_not_server_issued", started, preview_bundle=bundle)
    unknown_approved_ids = [
        node_id
        for node_id in _normalized_grow_ids(payload.approved_preview_ids)
        if node_id not in set(available_ids)
    ]
    if unknown_approved_ids:
        return _grow_blocked(tool_name, brain_id, "approved_preview_ids_not_server_issued", started, preview_bundle=bundle)
    selected_ids = _selected_preview_ids(bundle, requested_ids)
    if not selected_ids:
        return _grow_blocked(tool_name, brain_id, "selected_preview_ids_required", started, preview_bundle=bundle)
    if stored_investigation:
        preview_nodes = {
            str(node.get("id") or ""): node
            for node in [
                dict(bundle.get("primary_node_preview") or {}),
                *[dict(item) for item in list(bundle.get("derived_nodes") or [])],
            ]
            if str(node.get("id") or "").strip()
        }
        selected_nodes = [preview_nodes[node_id] for node_id in selected_ids]
        if any(
            not str(node.get("claim_id") or "").strip()
            or not str(node.get("decision_id") or "").strip()
            for node in selected_nodes
        ):
            return _grow_blocked(
                tool_name,
                brain_id,
                "selected_preview_claim_decision_binding_invalid",
                started,
                preview_bundle=bundle,
            )
        approved_ids = set(_normalized_grow_ids(payload.approved_preview_ids))
        high_impact_ids = {
            str(node.get("id") or "")
            for node in selected_nodes
            if str(node.get("claim_decision") or "")
            in {
                "contradicts_existing",
                "supersedes_existing",
                "evolve_existing",
                "delete_existing",
            }
        }
        if high_impact_ids - approved_ids:
            return _grow_blocked(
                tool_name,
                brain_id,
                "high_impact_preview_approval_required",
                started,
                preview_bundle=bundle,
            )
        receipt_node_ids = _grow_investigation_receipt_node_ids(stored_investigation)
        target_node_ids = {
            str(target_id)
            for node in selected_nodes
            for target_id in list(node.get("target_node_ids") or [])
            if str(target_id).strip()
        }
        if target_node_ids - receipt_node_ids:
            return _grow_blocked(
                tool_name,
                brain_id,
                "selected_preview_target_not_search_bound",
                started,
                preview_bundle=bundle,
            )
    apply_material = _grow_apply_material(
        investigation_id=investigation_id,
        brain_id=stored_brain_id,
        preview_fingerprint=preview_fingerprint,
        selected_preview_ids=selected_ids,
        payload=payload,
        investigation_sha256=str(stored.get("investigation_sha256") or ""),
    )
    apply_fingerprint = _grow_apply_fingerprint(
        investigation_id=investigation_id,
        brain_id=stored_brain_id,
        preview_fingerprint=preview_fingerprint,
        selected_preview_ids=selected_ids,
        payload=payload,
        investigation_sha256=str(stored.get("investigation_sha256") or ""),
    )

    def mutate_graph(graph: dict[str, Any], stored_bundle: dict[str, Any]) -> dict[str, Any]:
        updated_graph, persisted_ids, persisted_edge_count, merged_ids, learning_policy = persist_selection(
            stored_bundle,
            selected_ids,
            graph,
            build_index(list(graph.get("nodes") or [])),
            learning_mode=payload.learning_mode,
            clarification_answers=payload.clarification_answers,
            approved_preview_ids=payload.approved_preview_ids,
            question_limit=payload.question_limit,
        )
        return {
            "updated_graph": updated_graph,
            "persisted_node_ids": persisted_ids,
            "persisted_edge_count": persisted_edge_count,
            "merged_into_existing_ids": merged_ids,
            "learning_policy": learning_policy,
        }

    try:
        with use_runtime_brain(brain_record):
            apply_result = _apply_local_grow_v2_with_busy_retry(
                brain_id=resolved_brain_id,
                investigation_id=investigation_id,
                selected_preview_ids=selected_ids,
                apply_fingerprint=apply_fingerprint,
                apply_material=apply_material,
                preview_sha256=preview_fingerprint,
                attestation_sha256=str(stored.get("attestation_sha256") or ""),
                investigation_sha256=str(stored.get("investigation_sha256") or ""),
                expected_investigation_version=(
                    payload.investigation_version
                    if payload.investigation_version is not None
                    else int(stored.get("version") or 0)
                ),
                mutate_graph=mutate_graph,
            )
    except (GrowPreviewBindingStoreError, AiModuleContractError, sqlite3.DatabaseError) as exc:
        if _grow_sqlite_busy(exc):
            reason = "grow_v2_apply_database_busy"
        elif isinstance(exc, sqlite3.DatabaseError):
            reason = "grow_v2_apply_transaction_failed"
        else:
            reason = str(getattr(exc, "code", "") or exc or "grow_v2_apply_failed")
        return _grow_blocked(tool_name, resolved_brain_id, reason, started, preview_bundle=bundle)
    response = _grow_response_from_durable_apply(
        tool_name,
        stored,
        apply_result,
        started=started,
    )
    _GROW_PREVIEW_RUNS[investigation_id] = {
        **deepcopy(stored),
        "status": "applied",
        "persist_result": deepcopy(response.persist_result),
        "apply_receipt": deepcopy(dict(apply_result.get("signed_apply_receipt") or {})),
    }
    return response


def _grow_source_rollback(
    tool_name: str,
    payload: McpGrowRollbackRequest,
) -> McpGrowToolExecutionResponse:
    started = time.perf_counter()
    brain_record = _resolve_brain_record(payload.brain_id)
    brain_id = _brain_record_id(brain_record)
    try:
        with use_runtime_brain(brain_record):
            rollback_result = rollback_local_grow_v2_preview_transaction(
                brain_id=brain_id,
                investigation_id=payload.investigation_id,
                confirm_rollback=payload.confirm_rollback,
            )
            stored = fetch_local_grow_v2_preview(
                brain_id=brain_id,
                investigation_id=payload.investigation_id,
            )
    except (GrowPreviewBindingStoreError, AiModuleContractError, sqlite3.DatabaseError) as exc:
        reason = (
            "grow_v2_rollback_transaction_failed"
            if isinstance(exc, sqlite3.DatabaseError)
            else str(getattr(exc, "code", "") or exc or "grow_v2_rollback_failed")
        )
        return _grow_blocked(tool_name, brain_id, reason, started)
    if not stored:
        return _grow_blocked(tool_name, brain_id, "server_preview_not_found", started)
    response = _grow_response_from_durable_rollback(
        tool_name,
        stored,
        rollback_result,
        started=started,
    )
    _GROW_PREVIEW_RUNS[payload.investigation_id] = {
        **deepcopy(stored),
        "status": "rolled_back",
        "persist_result": deepcopy(response.persist_result),
    }
    return response


def _grow_source_status(tool_name: str, payload: McpGrowApplyRequest) -> McpGrowToolExecutionResponse:
    started = time.perf_counter()
    investigation = dict(payload.source_investigation or {})
    investigation_id = str(payload.investigation_id or investigation.get("investigation_id") or "").strip()
    requested_brain_id = str(payload.brain_id or investigation.get("brain_id") or "").strip() or None
    stored: dict[str, Any] = {}
    if investigation_id:
        brain_record = _resolve_brain_record(requested_brain_id)
        resolved_brain_id = _brain_record_id(brain_record)
        try:
            with use_runtime_brain(brain_record):
                stored = dict(
                    fetch_local_grow_v2_preview(
                        brain_id=resolved_brain_id,
                        investigation_id=investigation_id,
                    )
                    or {}
                )
        except (GrowPreviewBindingStoreError, AiModuleContractError) as exc:
            reason = str(getattr(exc, "code", "") or exc or "server_preview_invalid")
            return _grow_blocked(tool_name, resolved_brain_id, reason, started)
        if not stored:
            cached = dict(_GROW_PREVIEW_RUNS.get(investigation_id) or {})
            cached_brain_id = str(cached.get("brain_id") or "").strip()
            if cached_brain_id and cached_brain_id != resolved_brain_id:
                cached = {}
            if cached and not cached.get("preview_bundle"):
                stored = cached
    if not stored:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "source_investigation_not_found",
                "message": "No Grow source investigation exists for this brain.",
                "brain_id": requested_brain_id,
            },
        )
    stored_investigation = deepcopy(dict(stored.get("investigation") or {}))
    stored_state = str(stored.get("state") or "").strip()
    if stored_investigation and stored_state in {"investigating", "awaiting_clarification"}:
        if not payload.resume_token:
            return _grow_blocked(
                tool_name,
                str(stored.get("brain_id") or "") or None,
                "resume_token_required",
                started,
            )
        try:
            with use_runtime_brain(brain_record):
                authenticated = fetch_grow_investigation(
                    brain_id=resolved_brain_id,
                    investigation_id=investigation_id,
                    resume_token=payload.resume_token,
                )
        except GrowPreviewBindingStoreError as exc:
            return _grow_blocked(
                tool_name,
                resolved_brain_id,
                str(getattr(exc, "code", "") or exc),
                started,
            )
        if not authenticated:
            return _grow_blocked(tool_name, resolved_brain_id, "server_preview_not_found", started)
        stored = dict(authenticated)
        stored_investigation = deepcopy(dict(stored.get("investigation") or {}))
        stored_state = str(stored.get("state") or "").strip()
        stored_version = int(stored.get("version") or 0)
        if payload.investigation_version is not None and stored_version != int(payload.investigation_version):
            return _grow_blocked(
                tool_name,
                resolved_brain_id,
                "grow_investigation_version_conflict",
                started,
            )
    if str(stored.get("state") or "") == "consumed":
        return _grow_response_from_durable_apply(
            tool_name,
            stored,
            {**dict(stored.get("apply_result") or {}), "idempotent_replay": True},
            started=started,
        )
    if str(stored.get("state") or "") == "rolled_back":
        return _grow_response_from_durable_rollback(
            tool_name,
            stored,
            {**dict(stored.get("rollback_result") or {}), "idempotent_replay": True},
            started=started,
        )
    if stored_state == "maintenance_deferred":
        authenticated = _authenticated_v3_grow_record_or_response(
            tool_name=tool_name,
            brain_record=brain_record,
            brain_id=resolved_brain_id,
            investigation_id=investigation_id,
            resume_token=payload.resume_token,
            started=started,
        )
        if isinstance(authenticated, McpGrowToolExecutionResponse):
            return authenticated
        return _grow_response_from_maintenance_deferred(
            tool_name,
            authenticated,
            started=started,
            resume_token=str(payload.resume_token or "") or None,
        )
    if stored_state in {"investigating", "awaiting_clarification"}:
        raw_questions = stored_investigation.get("pending_questions")
        if raw_questions is None:
            raw_questions = stored_investigation.get("questions")
        questions = (
            [deepcopy(dict(item)) for item in raw_questions.values() if isinstance(item, dict)]
            if isinstance(raw_questions, dict)
            else [deepcopy(dict(item)) for item in list(raw_questions or []) if isinstance(item, dict)]
        )
        awaiting_clarification = stored_state == "awaiting_clarification"
        source_formation_contract = {
            "schema_version": "agvm.core_source_formation_contract.v3",
            "mode": "grow_ai_investigation",
            "state": stored_state,
            "mutates_memory": False,
            "investigation_id": investigation_id,
            "apply_contract": {
                "can_apply_now": False,
                "blocked_reasons": [
                    "clarification_required" if awaiting_clarification else "investigation_incomplete"
                ],
                "selected_preview_ids": [],
            },
        }
        return McpGrowToolExecutionResponse(
            schema_version="agvm.mcp_grow_tool_output.v1",
            brain_id=str(stored.get("brain_id") or ""),
            tool_name=tool_name,
            status="asking_clarification" if awaiting_clarification else "needs_review",
            can_apply_now=False,
            selected_preview_ids=[],
            source_investigation=deepcopy(dict(stored.get("source_investigation") or {})),
            source_formation_contract=source_formation_contract,
            memory_operation_lifecycle_contract={
                "schema_version": "agvm.memory_operation_lifecycle_contract.v3",
                "operation": "grow",
                "phase": stored_state,
                "next_action": (
                    "resume with investigation_id, resume_token and answers"
                    if awaiting_clarification
                    else "continue the server-side investigation"
                ),
            },
            clarification_questions=questions,
            ai_execution_attestation=deepcopy(
                dict(stored_investigation.get("ai_execution_attestation") or {})
            ),
            ai_execution_ledger=[
                deepcopy(dict(item))
                for item in list(stored_investigation.get("ai_execution_ledger") or [])
                if isinstance(item, dict)
            ],
            investigation=stored_investigation,
            investigation_session={
                "schema_version": "agvm.investigation_session.v3",
                "status": "awaiting_answers" if awaiting_clarification else "incomplete",
                "question_count": len(questions),
                "investigation_version": int(stored.get("version") or 0),
            },
            resume_token=str(payload.resume_token or ""),
            investigation_version=int(stored.get("version") or 0),
            usage=deepcopy(
                dict(stored_investigation.get("usage") or stored_investigation.get("aggregate_usage") or {})
            ),
            completeness={
                "preview_generated": False,
                "investigation_complete": bool(stored_investigation.get("complete")),
                "investigation_applicable": bool(stored_investigation.get("applicable")),
                "question_count": len(questions),
            },
            mcp_latency_profile={"elapsed_ms": int((time.perf_counter() - started) * 1000)},
            budget={"credits_required": 0, "runtime": "local_core"},
            next_action=(
                "resume with investigation_id, resume_token and answers"
                if awaiting_clarification
                else "continue the server-side investigation"
            ),
        )
    if stored.get("state") and stored_state != "active":
        return _grow_blocked(
            tool_name,
            str(stored.get("brain_id") or "") or None,
            "server_preview_stale",
            started,
            preview_bundle=deepcopy(dict(stored.get("preview_bundle") or {})),
        )
    preview_status = (
        "asking_clarification"
        if not bool(
            dict(dict(stored.get("source_formation_contract") or {}).get("apply_contract") or {}).get(
                "can_apply_now",
                True,
            )
        )
        else "preview_ready"
    )
    stored_apply_contract = dict(
        dict(stored.get("source_formation_contract") or {}).get("apply_contract") or {}
    )
    stored_selected_preview_ids = _normalized_grow_ids(
        list(stored_apply_contract.get("selected_preview_ids") or [])
    )
    if not stored_selected_preview_ids:
        stored_selected_preview_ids = _selected_preview_ids(
            dict(stored.get("preview_bundle") or {}),
            [],
        )
    stored_can_apply_now = bool(stored_apply_contract.get("can_apply_now")) or bool(
        stored_selected_preview_ids
    )
    return McpGrowToolExecutionResponse(
        schema_version="agvm.mcp_grow_tool_output.v1",
        brain_id=str(stored.get("brain_id") or ""),
        investigation_id=investigation_id or None,
        tool_name=tool_name,
        status=preview_status,
        can_apply_now=stored_can_apply_now,
        selected_preview_ids=stored_selected_preview_ids,
        source_investigation=dict(stored.get("source_investigation") or {}),
        source_formation_contract=dict(stored.get("source_formation_contract") or {}),
        preview_bundle=deepcopy(stored.get("preview_bundle")),
        maintenance_feedback_packets=[
            deepcopy(dict(item))
            for item in list(stored.get("maintenance_feedback_packets") or [])
            if isinstance(item, dict)
        ],
        ai_execution_attestation=deepcopy(dict(stored.get("ai_execution_attestation") or {})),
        ai_execution_ledger=[
            deepcopy(dict(item))
            for item in list(stored_investigation.get("ai_execution_ledger") or [])
            if isinstance(item, dict)
        ],
        investigation=stored_investigation,
        investigation_session=deepcopy(dict(stored.get("investigation_session") or {})),
        resume_token=str(payload.resume_token or "") or None,
        investigation_version=(int(stored.get("version") or 0) or None),
        usage=deepcopy(
            dict(stored_investigation.get("usage") or stored_investigation.get("aggregate_usage") or {})
        ),
        memory_operation_lifecycle_contract={
            "phase": "preview" if stored.get("preview_bundle") else "source_unit_proof",
            "next_action": "apply with confirm_apply=true" if stored.get("preview_bundle") else "run full preview before apply",
        },
        budget={"credits_required": 0, "runtime": "local_core"},
    )


def _write_memory_preview(tool_name: str, payload: McpWriteMemoryPreviewRequest) -> McpGrowToolExecutionResponse:
    source_type = str(payload.source_type or "self_memory").strip()
    treat_as = source_type if source_type in {
        "self_memory",
        "project_workspace",
        "public_dossier",
        "reference_library",
        "technical_document",
    } else "self_memory"
    request = McpGrowSourceRequest(
        brain_id=payload.brain_id,
        raw_input=payload.text,
        input_kind="manual_text" if payload.input_mode == "auto" else "mixed_bundle",
        source_label=payload.source_label,
        options={
            "treat_as": treat_as,
            "source_trust": str(payload.source_trust or "user_asserted"),
            "pause_on_questions": payload.learning_mode == "guided_learning",
            "question_limit": payload.question_limit,
        },
        run_preview=True,
    )
    return _grow_source_preview(tool_name, _canonical_semantic_grow_payload(request))


def _write_memory_commit(tool_name: str, payload: McpWriteMemoryCommitRequest) -> McpGrowToolExecutionResponse:
    started = time.perf_counter()
    brain_id = str(payload.brain_id or "").strip() or None
    response = _grow_blocked(tool_name, brain_id, "server_issued_grow_apply_required", started)
    response.memory_operation_lifecycle_contract.update(
        {
            "schema_version": "agvm.memory_operation_lifecycle_contract.v2",
            "operation": "write_memory",
            "phase": "blocked",
            "mutates_memory": False,
            "legacy_alias": True,
            "next_action": (
                "call write_memory_preview or grow_source_preview, then apply the returned "
                "server-issued investigation with grow_source_apply"
            ),
        }
    )
    response.next_action = (
        "use grow_source_apply with the server-issued investigation_id, exact selected_preview_ids, "
        "and confirm_apply=true"
    )
    return response


def _ask_memory_clarification(tool_name: str, payload: McpClarificationRequest) -> McpGrowToolExecutionResponse:
    started = time.perf_counter()
    text = payload.text or payload.raw_input or payload.user_instruction or ""
    questions = [
        {
            "question_id": "clarify-source-and-scope",
            "question": "What should this memory be used for, and should it be treated as a fact, preference, project note, or source-backed evidence?",
            "required": True,
        }
    ]
    if payload.source_uri:
        questions.append(
            {
                "question_id": "clarify-source-trust",
                "question": "Should this source be treated as verified public evidence or as user-provided context?",
                "required": False,
            }
        )
    return McpGrowToolExecutionResponse(
        schema_version="agvm.mcp_grow_tool_output.v1",
        brain_id=payload.brain_id,
        tool_name=tool_name,
        status="asking_clarification",
        clarification_questions=questions[: max(1, min(payload.question_limit, len(questions)))],
        source_investigation={
            "schema_version": "agvm.mcp_clarification_request.v1",
            "input_preview": text[:240],
            "source_label": payload.source_label,
            "source_uri": payload.source_uri,
        },
        memory_operation_lifecycle_contract={
            "schema_version": "agvm.memory_operation_lifecycle_contract.v1",
            "operation": "write_memory",
            "phase": "clarification",
            "mutates_memory": False,
            "next_action": "answer clarification questions, then call write_memory_preview or grow_source_preview",
        },
        completeness={"question_count": len(questions[: max(1, min(payload.question_limit, len(questions)))])},
        mcp_latency_profile={"elapsed_ms": int((time.perf_counter() - started) * 1000)},
        budget={"credits_required": 0, "runtime": "local_core"},
    )


def _grow_blocked(
    tool_name: str,
    brain_id: str | None,
    reason: str,
    started: float,
    *,
    preview_bundle: dict[str, Any] | None = None,
    source_investigation: dict[str, Any] | None = None,
    source_formation_contract: dict[str, Any] | None = None,
) -> McpGrowToolExecutionResponse:
    resolved_source_contract = dict(source_formation_contract or {})
    if source_investigation and not resolved_source_contract:
        resolved_source_contract = {
            "schema_version": "agvm.core_source_formation_contract.v3",
            "mode": "grow_ai_investigation",
            "state": "blocked",
            "mutates_memory": False,
            "investigation_id": str(source_investigation.get("investigation_id") or "") or None,
            "blocked_reason": reason,
        }
    return McpGrowToolExecutionResponse(
        schema_version="agvm.mcp_grow_tool_output.v1",
        brain_id=brain_id,
        tool_name=tool_name,
        status="blocked",
        can_apply_now=False,
        selected_preview_ids=[],
        source_investigation=source_investigation or {},
        source_formation_contract=resolved_source_contract,
        preview_bundle=preview_bundle,
        memory_operation_lifecycle_contract={
            "schema_version": "agvm.memory_operation_lifecycle_contract.v1",
            "blocked_reason": reason,
            "mutates_memory": False,
        },
        completeness={"blocked": True, "reason": reason},
        mcp_latency_profile={"elapsed_ms": int((time.perf_counter() - started) * 1000)},
        budget={"credits_required": 0, "runtime": "local_core"},
    )


def _maintenance_preview(
    tool_name: str,
    mode: str,
    payload: McpMaintenanceRequest,
    *,
    brain_record: dict[str, Any] | None = None,
    runtime: MaintenanceMutationRuntime | None = None,
) -> McpMaintenanceToolExecutionResponse:
    started = time.perf_counter()
    brain_record = brain_record or _resolve_bootstrap_ready_brain_record(payload.brain_id)
    brain_id = _brain_record_id(brain_record)
    with use_runtime_brain(brain_record):
        bootstrap_runtime_store()
        graph = fetch_graph_snapshot()
        if runtime is None:
            report = _build_core_maintenance_report(
                graph,
                mode=mode,
                preview_only=True,
                focus_node_id=payload.focus_node_id,
                max_nodes_considered=payload.max_nodes_considered,
                selected_proposal_ids=[],
            )
        else:
            report = runtime.preview(
                graph=graph,
                mode=mode,
                focus_node_id=payload.focus_node_id,
                max_nodes_considered=payload.max_nodes_considered,
            )
        maintenance_id = str(report.get("maintenance_id") or uuid.uuid4())
        report["maintenance_id"] = maintenance_id
        store_maintenance_run(
            maintenance_id=maintenance_id,
            mode=mode,
            applied=False,
            preview_only=True,
            focus_node_id=payload.focus_node_id,
            report=report,
        )
    status = "blocked" if report.get("maintenance_store_error") else "preview_ready"
    return _maintenance_response(tool_name, brain_id, status, report, started)


def _maintenance_apply(
    tool_name: str,
    mode: str,
    payload: McpMaintenanceApplyRequest,
    *,
    brain_record: dict[str, Any] | None = None,
    runtime: MaintenanceMutationRuntime | None = None,
) -> McpMaintenanceToolExecutionResponse:
    started = time.perf_counter()
    if not payload.confirm_apply:
        return _maintenance_response(
            tool_name,
            payload.brain_id,
            "blocked",
            {
                "applied": False,
                "mode": mode,
                "apply_policy_guard": {
                    "blocked": True,
                    "blocked_reason": "confirm_apply_required",
                    "partial_merge_allowed": False,
                },
            },
            started,
        )
    if not payload.preview_signature:
        return _maintenance_response(
            tool_name,
            payload.brain_id,
            "blocked",
            {
                "applied": False,
                "mode": mode,
                "apply_policy_guard": {
                    "blocked": True,
                    "blocked_reason": "maintenance_preview_signature_required",
                    "blocked_reasons": ["maintenance_preview_signature_required"],
                    "partial_merge_allowed": False,
                    "graph_mutation": "none",
                },
            },
            started,
        )
    brain_record = brain_record or _resolve_bootstrap_ready_brain_record(payload.brain_id)
    brain_id = _brain_record_id(brain_record)
    requested_ids = _normalized_proposal_ids(payload.proposal_ids)
    if payload.preview_signature and runtime is not None:
        with use_runtime_brain(brain_record):
            bootstrap_runtime_store()
            graph = fetch_graph_snapshot()
            applied_report = runtime.apply(
                graph=graph,
                mode=mode,
                focus_node_id=payload.focus_node_id,
                max_nodes_considered=payload.max_nodes_considered,
                expected_preview_signature=payload.preview_signature,
                selected_proposal_ids=requested_ids,
            )
        available_ids = _normalized_proposal_ids(
            [item.get("proposal_id") for item in list(applied_report.get("maintenance_proposals") or [])]
        )
        missing_ids = [proposal_id for proposal_id in requested_ids if proposal_id not in available_ids]
        unselected_ids = [proposal_id for proposal_id in available_ids if proposal_id not in requested_ids]
        if not bool(applied_report.get("applied")):
            runtime_guard = dict(applied_report.get("apply_policy_guard") or {})
            runtime_reasons = [str(item) for item in list(runtime_guard.get("blocked_reasons") or []) if str(item)]
            runtime_blocked_reason = runtime_reasons[0] if runtime_reasons else "maintenance_safety_guard_blocked_apply"
            _mark_core_maintenance_apply_blocked(
                applied_report,
                blocked_reason=runtime_blocked_reason,
                requested_ids=requested_ids,
                available_ids=available_ids,
                missing_ids=missing_ids,
                unselected_ids=unselected_ids,
            )
            return _maintenance_response(tool_name, brain_id, "blocked", applied_report, started)
        _mark_core_maintenance_apply_succeeded(
            applied_report,
            requested_ids=requested_ids,
            available_ids=available_ids,
        )
        return _maintenance_response(tool_name, brain_id, "applied", applied_report, started)
    with use_runtime_brain(brain_record):
        bootstrap_runtime_store()
        graph = fetch_graph_snapshot()
        if runtime is None:
            report = _build_core_maintenance_report(
                graph,
                mode=mode,
                preview_only=True,
                focus_node_id=payload.focus_node_id,
                max_nodes_considered=payload.max_nodes_considered,
                selected_proposal_ids=[],
            )
        else:
            report = runtime.preview(
                graph=graph,
                mode=mode,
                focus_node_id=payload.focus_node_id,
                max_nodes_considered=payload.max_nodes_considered,
            )
    available_ids = _normalized_proposal_ids(
        [item.get("proposal_id") for item in list(report.get("maintenance_proposals") or [])]
    )
    missing_ids = [proposal_id for proposal_id in requested_ids if proposal_id not in available_ids]
    unselected_ids = [proposal_id for proposal_id in available_ids if proposal_id not in requested_ids]
    blocked_reason = _core_maintenance_apply_blocked_reason(
        requested_ids=requested_ids,
        available_ids=available_ids,
        missing_ids=missing_ids,
        unselected_ids=unselected_ids,
    )
    if blocked_reason is None and runtime is None:
        blocked_reason = "maintain_apply_runtime_not_configured"
    if blocked_reason is None:
        with use_runtime_brain(brain_record):
            bootstrap_runtime_store()
            graph = fetch_graph_snapshot()
            applied_report = runtime.apply(
                graph=graph,
                mode=mode,
                focus_node_id=payload.focus_node_id,
                max_nodes_considered=payload.max_nodes_considered,
            )
        if not bool(applied_report.get("applied")):
            runtime_guard = dict(applied_report.get("apply_policy_guard") or {})
            runtime_reasons = [str(item) for item in list(runtime_guard.get("blocked_reasons") or []) if str(item)]
            runtime_blocked_reason = runtime_reasons[0] if runtime_reasons else "maintenance_safety_guard_blocked_apply"
            _mark_core_maintenance_apply_blocked(
                applied_report,
                blocked_reason=runtime_blocked_reason,
                requested_ids=requested_ids,
                available_ids=available_ids,
                missing_ids=[],
                unselected_ids=[],
            )
            return _maintenance_response(tool_name, brain_id, "blocked", applied_report, started)
        _mark_core_maintenance_apply_succeeded(
            applied_report,
            requested_ids=requested_ids,
            available_ids=available_ids,
        )
        return _maintenance_response(tool_name, brain_id, "applied", applied_report, started)
    _mark_core_maintenance_apply_blocked(
        report,
        blocked_reason=blocked_reason,
        requested_ids=requested_ids,
        available_ids=available_ids,
        missing_ids=missing_ids,
        unselected_ids=unselected_ids,
    )
    return _maintenance_response(tool_name, brain_id, "blocked", report, started)


def _maintenance_rollback(
    tool_name: str,
    mode: str,
    payload: dict[str, Any],
    *,
    brain_record: dict[str, Any] | None = None,
    runtime: MaintenanceMutationRuntime | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    brain_record = brain_record or _resolve_bootstrap_ready_brain_record(
        str(payload.get("brain_id") or "").strip() or None
    )
    brain_id = _brain_record_id(brain_record)
    preview_signature = str(payload.get("preview_signature") or "").strip()
    blocked_reason = None
    if payload.get("confirm_rollback") is not True:
        blocked_reason = "confirm_rollback_required"
    elif not preview_signature:
        blocked_reason = "preview_signature_required_for_rollback"
    elif runtime is None:
        blocked_reason = "reviewed_preview_rollback_not_configured"
    if blocked_reason:
        return _maintenance_rollback_response(
            tool_name=tool_name,
            brain_id=brain_id,
            mode=mode,
            preview_signature=preview_signature,
            status="blocked",
            started=started,
            error={"code": blocked_reason},
        )
    with use_runtime_brain(brain_record):
        bootstrap_runtime_store()
        result = runtime.rollback(mode=mode, preview_signature=preview_signature)
    error = dict(result.get("error") or {})
    if error or not bool(result.get("rolled_back")):
        return _maintenance_rollback_response(
            tool_name=tool_name,
            brain_id=brain_id,
            mode=mode,
            preview_signature=preview_signature,
            status="blocked",
            started=started,
            error=error or {"code": "maintenance_rollback_failed"},
            rollback_result=result,
        )
    return _maintenance_rollback_response(
        tool_name=tool_name,
        brain_id=brain_id,
        mode=mode,
        preview_signature=preview_signature,
        status="already_rolled_back" if bool(result.get("idempotent_replay")) else "rolled_back",
        started=started,
        rollback_result=result,
    )


def _maintenance_rollback_response(
    *,
    tool_name: str,
    brain_id: str,
    mode: str,
    preview_signature: str,
    status: str,
    started: float,
    rollback_result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "agvm.maintenance_preview_rollback.v1",
        "brain_id": brain_id,
        "tool_name": tool_name,
        "mode": mode,
        "preview_signature": preview_signature,
        "status": status,
        "rollback_result": dict(rollback_result or {}),
        "error": dict(error or {}) or None,
        "mutation_surface": {
            "runtime": "local_core",
            "credits_required": 0,
            "status": status,
            "rolled_back": status in {"rolled_back", "already_rolled_back"},
            "graph_mutation": "none" if status in {"blocked", "already_rolled_back"} else "restored",
            "revision_safe": True,
        },
        "maintenance_latency_profile": {
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        },
    }


def _mark_core_maintenance_apply_succeeded(
    report: dict[str, Any],
    *,
    requested_ids: list[str],
    available_ids: list[str],
) -> None:
    report["maintenance_proposal_summary"] = {
        **dict(report.get("maintenance_proposal_summary") or {}),
        "selected_for_apply_count": len(requested_ids),
    }
    report["apply_policy_guard"] = {
        **dict(report.get("apply_policy_guard") or {}),
        "applied": True,
        "blocked": False,
        "blocked_reason": None,
        "guard_passed": True,
        "partial_merge_allowed": False,
        "available_proposal_ids": available_ids,
        "selected_proposal_ids": requested_ids,
        "selected_missing_proposal_ids": [],
        "unselected_available_proposal_ids": [],
        "graph_mutation": "committed",
    }
    contract = dict(report.get("maintenance_contract") or {})
    contract.update(
        {
            "preview_non_mutating": False,
            "hidden_mutation_allowed": False,
            "apply_runtime": "local_core_maintain_runtime",
            "selection_exactness": {
                "exact": True,
                "requested_proposal_ids": requested_ids,
                "available_proposal_ids": available_ids,
                "missing_requested_proposal_ids": [],
                "unselected_available_proposal_ids": [],
            },
        }
    )
    report["maintenance_contract"] = contract


def _normalized_proposal_ids(values: list[Any]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in list(values or []):
        proposal_id = str(value or "").strip()
        if not proposal_id or proposal_id in seen:
            continue
        seen.add(proposal_id)
        normalized.append(proposal_id)
    return normalized


def _core_maintenance_apply_blocked_reason(
    *,
    requested_ids: list[str],
    available_ids: list[str],
    missing_ids: list[str],
    unselected_ids: list[str],
) -> str | None:
    if not requested_ids:
        return "proposal_ids_required_for_exact_apply"
    if missing_ids:
        return "requested_proposal_ids_not_available"
    if not available_ids:
        return "no_applicable_proposals"
    if unselected_ids:
        return "partial_proposal_apply_not_supported"
    return None


def _mark_core_maintenance_apply_blocked(
    report: dict[str, Any],
    *,
    blocked_reason: str,
    requested_ids: list[str],
    available_ids: list[str],
    missing_ids: list[str],
    unselected_ids: list[str],
) -> None:
    exact_selection = bool(requested_ids and not missing_ids and not unselected_ids)
    report["applied"] = False
    report["maintenance_proposal_summary"] = {
        **dict(report.get("maintenance_proposal_summary") or {}),
        "selected_for_apply_count": len(requested_ids),
    }
    report["apply_policy_guard"] = {
        "applied": False,
        "blocked": True,
        "blocked_reason": blocked_reason,
        "blocked_reasons": list(
            dict.fromkeys(
                [blocked_reason]
                + [
                    str(reason)
                    for reason in list(dict(report.get("apply_policy_guard") or {}).get("blocked_reasons") or [])
                    if str(reason) and str(reason) != "preview_only"
                ]
            )
        ),
        "guard_passed": False,
        "partial_merge_allowed": False,
        "available_proposal_ids": available_ids,
        "selected_proposal_ids": requested_ids,
        "selected_missing_proposal_ids": missing_ids,
        "unselected_available_proposal_ids": unselected_ids,
        "graph_mutation": "none",
    }
    contract = dict(report.get("maintenance_contract") or {})
    contract.update(
        {
            "preview_non_mutating": True,
            "hidden_mutation_allowed": False,
            "apply_runtime": "maintain_module_or_detwin_cloud",
            "selection_exactness": {
                "exact": exact_selection,
                "requested_proposal_ids": requested_ids,
                "available_proposal_ids": available_ids,
                "missing_requested_proposal_ids": missing_ids,
                "unselected_available_proposal_ids": unselected_ids,
            },
        }
    )
    report["maintenance_contract"] = contract


def _maintenance_response(
    tool_name: str,
    brain_id: str | None,
    status: str,
    report: dict[str, Any],
    started: float,
) -> McpMaintenanceToolExecutionResponse:
    proposals = list(report.get("maintenance_proposals") or [])
    return McpMaintenanceToolExecutionResponse(
        schema_version="agvm.mcp_maintenance_tool_output.v1",
        brain_id=brain_id,
        tool_name=tool_name,
        status=status,  # type: ignore[arg-type]
        maintenance_report=report,
        maintenance_proposals=proposals,
        elastic_topology_proposals=list(report.get("elastic_topology_proposals") or []),
        maintenance_truth_contract=dict(report.get("maintenance_contract") or {}),
        sleep_evolve_lifecycle_contract={
            "schema_version": "agvm.sleep_evolve_lifecycle_contract.v1",
            "tool_name": tool_name,
            "mode": report.get("mode"),
            "state": status,
            "applied": bool(report.get("applied")),
            "partial_merge_allowed": False,
            "approval_gate": dict(report.get("apply_policy_guard") or {}),
        },
        maintenance_transaction=dict(report.get("maintenance_transaction") or {}),
        preview_budget_guard=dict(report.get("preview_budget_guard") or {}),
        maintenance_preview_plan=dict(report.get("maintenance_preview_plan") or {}),
        memory_operation_lifecycle_contract={
            "operation": "sleep_evolve",
            "phase": "blocked" if status == "blocked" else "applied" if bool(report.get("applied")) else "preview",
            "tool_name": tool_name,
            "requires_confirm_apply_for_mutation": True,
        },
        proposal_review_table=[
            {
                "proposal_id": str(item.get("proposal_id") or item.get("id") or ""),
                "kind": item.get("kind") or item.get("proposal_kind"),
                "summary": item.get("summary") or item.get("reason") or item.get("title") or item.get("proposed_action"),
            }
            for item in proposals[:40]
        ],
        metamemory_snapshot=dict(report.get("metamemory_snapshot") or {}),
        apply_policy_guard=dict(report.get("apply_policy_guard") or {}),
        rollback_snapshot=dict(report.get("rollback_snapshot") or {}),
        before_after_audit=dict(report.get("before_after_audit") or {}),
        no_corruption_guards=dict(report.get("no_corruption_guards") or {}),
        mutation_surface={
            "runtime": "local_core",
            "credits_required": 0,
            "status": status,
            "applied": bool(report.get("applied")),
            "graph_mutation": dict(report.get("apply_policy_guard") or {}).get("graph_mutation", "none"),
            "preview_non_mutating": not bool(report.get("applied")),
            "hidden_mutation_allowed": False,
        },
        maintenance_latency_profile={"elapsed_ms": int((time.perf_counter() - started) * 1000)},
        open_questions=list(report.get("open_questions") or []),
        hypotheses=list(report.get("hypotheses") or []),
        contradictions=list(report.get("contradictions") or []),
        processes=list(report.get("processes") or []),
        source_trace=list(report.get("source_trace") or []),
        completeness={
            "proposal_count": len(proposals),
            "status": status,
            "selected_proposal_ids": list(dict(report.get("apply_policy_guard") or {}).get("selected_proposal_ids") or []),
            "selected_missing_proposal_ids": list(
                dict(report.get("apply_policy_guard") or {}).get("selected_missing_proposal_ids") or []
            ),
        },
        budget={
            "credits_required": 0,
            "runtime": "local_core",
            "blocked_reason": dict(report.get("apply_policy_guard") or {}).get("blocked_reason"),
        },
    )


def _build_core_maintenance_report(
    graph: dict[str, Any],
    *,
    mode: str,
    preview_only: bool,
    focus_node_id: str | None,
    max_nodes_considered: int,
    selected_proposal_ids: list[str],
) -> dict[str, Any]:
    nodes = [dict(node) for node in list(graph.get("nodes") or [])]
    edges = [dict(edge) for edge in list(graph.get("edges") or [])]
    if focus_node_id:
        selected_nodes = [node for node in nodes if str(node.get("id") or "") == str(focus_node_id)]
    else:
        selected_nodes = nodes[: max(10, min(int(max_nodes_considered or 80), 500))]
    proposals: list[dict[str, Any]] = []
    if mode == "sleep":
        low_confidence = [
            node
            for node in selected_nodes
            if float(node.get("memory_confidence") or node.get("confidence") or 0.75) < 0.55
        ][:12]
        for node in low_confidence:
            proposals.append(
                {
                    "proposal_id": f"sleep-review-{node.get('id')}",
                    "kind": "confidence_review",
                    "node_id": node.get("id"),
                    "summary": "Review a low-confidence local memory before it influences retrieval.",
                    "preview_only": bool(preview_only),
                }
            )
        if not proposals and selected_nodes:
            proposals.append(
                {
                    "proposal_id": "sleep-index-refresh",
                    "kind": "local_consolidation",
                    "summary": "Refresh local retrieval posture and keep the current graph unchanged.",
                    "candidate_node_count": len(selected_nodes),
                    "preview_only": bool(preview_only),
                }
            )
    else:
        isolated = []
        linked_ids: set[str] = set()
        for edge in edges:
            linked_ids.add(str(edge.get("source") or edge.get("source_id") or ""))
            linked_ids.add(str(edge.get("target") or edge.get("target_id") or ""))
        for node in selected_nodes:
            if str(node.get("id") or "") not in linked_ids:
                isolated.append(node)
        for node in isolated[:12]:
            proposals.append(
                {
                    "proposal_id": f"evolve-connect-{node.get('id')}",
                    "kind": "connection_candidate",
                    "node_id": node.get("id"),
                    "summary": "Find a stronger local neighborhood for an isolated memory.",
                    "preview_only": bool(preview_only),
                }
            )
        if not proposals and selected_nodes:
            proposals.append(
                {
                    "proposal_id": "evolve-neighborhood-scan",
                    "kind": "topology_scan",
                    "summary": "Scan the selected local memory neighborhood for future structural improvements.",
                    "candidate_node_count": len(selected_nodes),
                    "preview_only": bool(preview_only),
                }
            )
    selected_ids = _normalized_proposal_ids(selected_proposal_ids)
    applied_ids = [] if preview_only else selected_ids
    return {
        "schema_version": "agvm.core_maintenance_report.v1",
        "applied": not preview_only,
        "mode": mode,
        "preview_budget_guard": {
            "schema_version": "agvm.core_maintenance_budget_guard.v1",
            "preview_only": bool(preview_only),
            "requested_max_nodes_considered": max_nodes_considered,
            "selected_node_count": len(selected_nodes),
            "policy": "local_core_sleep_evolve_is_bounded_and_never_consumes_detwin_credits",
        },
        "maintenance_preview_plan": {
            "focus_node_id": focus_node_id,
            "selected_node_count": len(selected_nodes),
            "graph_node_count": len(nodes),
            "graph_edge_count": len(edges),
        },
        "maintenance_contract": {
            "schema_version": "agvm.core_maintenance_contract.v1",
            "runtime": "local_core",
            "advanced_maintain_runtime": "maintain_module_or_detwin_cloud",
            "mutation_requires_confirm_apply": True,
            "preview_non_mutating": bool(preview_only),
            "hidden_mutation_allowed": False,
        },
        "metamemory_snapshot": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "reviewed_node_count": len(selected_nodes),
        },
        "maintenance_proposals": proposals,
        "maintenance_proposal_summary": {
            "proposal_count": len(proposals),
            "selected_for_apply_count": 0 if preview_only else len(applied_ids),
        },
        "apply_policy_guard": {
            "applied": not preview_only,
            "partial_merge_allowed": False,
            "selected_proposal_ids": [] if preview_only else applied_ids,
            "graph_mutation": "none" if not preview_only else "preview_only",
        },
        "maintenance_transaction": {
            "schema_version": "agvm.core_maintenance_transaction.v1",
            "transaction_id": f"core-maintenance-{uuid.uuid4()}",
            "mode": mode,
            "preview_only": bool(preview_only),
            "created_at": _utc_now(),
        },
        "before_after_audit": {
            "before_node_count": len(nodes),
            "after_node_count": len(nodes),
            "before_edge_count": len(edges),
            "after_edge_count": len(edges),
        },
        "no_corruption_guards": {
            "document_anchor_delete_blocked": True,
            "raw_memory_delete_blocked": True,
            "cloud_data_accessed": False,
        },
    }
