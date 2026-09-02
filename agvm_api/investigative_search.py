# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import json
from typing import Any, Callable, Mapping, TypedDict

try:
    from .investigative_agent import stable_digest
    from .ai_modules_v2 import validate_ai_execution_attestation
    from .stream_contract import search_mission_ledger_digest
    from .runtime_scope import current_brain_id
except ImportError:  # pragma: no cover - direct API runtime
    from investigative_agent import stable_digest
    from ai_modules_v2 import validate_ai_execution_attestation
    from stream_contract import search_mission_ledger_digest
    from runtime_scope import current_brain_id


GROW_SEARCH_RECEIPT_SCHEMA_VERSION = "agvm.grow_search_receipt.v1"
GROW_SEARCH_BILLING_SCOPE = "parent_grow_preview"
RETRIEVAL_MODES = {"flash", "balanced", "heavy", "forensic"}
QUERY_AUTHORITIES = {
    "provider",
    "server_bound_exact_spans_unified_search",
}


class GrowSearchReceiptMaterial(TypedDict, total=False):
    schema_version: str
    receipt_id: str
    call_id: str
    child_call_id: str
    correlation_id: str
    parent_operation_id: str | None
    billing_scope: str
    idempotency_key: str
    query_text: str
    query_sha256: str
    retrieval_mode: str
    max_matches: int
    brain_revision: str
    brain_id: str | None
    search_semantic_brain_revision: str
    graph_snapshot_sha256: str
    terminal_state: str
    terminal_for_client: bool
    contract_outcome: str
    authoritative_no_match: bool
    evidence_usable: bool
    question_usable: bool
    decision_usable: bool
    novelty_certified: bool
    usable: bool
    reviewable: bool
    context_package: dict[str, Any]
    master_judgement: dict[str, Any]
    mission_plan_v2: dict[str, Any]
    mission_evidence_ledger: dict[str, Any]
    document_references: list[dict[str, Any]]
    evidence_node_ids: list[str]
    context_excerpt: str
    payload_integrity: dict[str, Any]
    result_sha256: str
    search_execution_attestation: dict[str, Any]
    semantic_authority: dict[str, Any]


SearchRunner = Callable[[dict[str, Any]], Mapping[str, Any]]
NodeFetcher = Callable[..., list[dict[str, Any]]]
DocumentChildFetcher = Callable[..., list[dict[str, Any]]]
DocumentSiblingFetcher = Callable[..., list[dict[str, Any]]]


class InvestigativeSearchError(RuntimeError):
    pass


class InvestigativeDocumentEvidenceError(RuntimeError):
    pass


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


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


def _declared_graph_revision(graph_snapshot: Mapping[str, Any]) -> str:
    meta = _dict(graph_snapshot.get("meta"))
    for candidate in (
        graph_snapshot.get("brain_revision"),
        graph_snapshot.get("graph_revision"),
        meta.get("brain_revision"),
        meta.get("graph_revision"),
    ):
        value = str(candidate or "").strip()
        if value:
            return value
    return ""


def _declared_graph_brain_id(graph_snapshot: Mapping[str, Any]) -> str:
    meta = _dict(graph_snapshot.get("meta"))
    for candidate in (
        graph_snapshot.get("brain_id"),
        meta.get("brain_id"),
    ):
        value = str(candidate or "").strip()
        if value:
            return value
    return ""


def _public_search_runtime(request: dict[str, Any]) -> dict[str, Any]:
    """Run the same authoritative Search engine used by the public Context surface.

    Grow intentionally bypasses only the HTTP/MCP non-blocking transport.  The
    semantic planner, runtime controller, evidence ledger, Master and canonical
    terminal contracts must remain identical to an ordinary Search run.
    """

    try:
        try:
            from .retrieval import (
                prepare_runtime_plan,
                require_search_ai_admission,
                retrieve_runtime,
                search_identity_nucleus_for_named_targets,
            )
            from .schemas import RetrieveRequest
            from .sqlite_store import fetch_atlas, fetch_identity_nucleus
        except ImportError:  # pragma: no cover - direct API runtime
            from retrieval import (
                prepare_runtime_plan,
                require_search_ai_admission,
                retrieve_runtime,
                search_identity_nucleus_for_named_targets,
            )
            from schemas import RetrieveRequest
            from sqlite_store import fetch_atlas, fetch_identity_nucleus

        graph = _dict(request.get("graph_snapshot"))
        requested_brain_id = str(request.get("brain_id") or "").strip()
        runtime_brain_id = str(current_brain_id() or "").strip()
        if requested_brain_id and runtime_brain_id != requested_brain_id:
            raise InvestigativeSearchError(
                f"investigative_search_runtime_brain_mismatch:{runtime_brain_id}:{requested_brain_id}"
            )
        max_matches = max(1, min(24, int(request.get("max_matches") or 12)))
        retrieval_mode = str(request.get("retrieval_mode") or "balanced").strip().casefold()
        query = RetrieveRequest(
            query_text=str(request.get("query_text") or "").strip(),
            response_mode="context",
            retrieval_mode=retrieval_mode,
            mcp_tool_name="grow_investigative_search",
            deadline_at_ms=request.get("deadline_at_ms"),
            max_matches=max_matches,
            max_nodes_fulltext=min(24, max_matches),
            max_total_text_chars=max(6_400, min(64_000, max_matches * 1_200)),
        )
        atlas_payload = fetch_atlas()
        identity_nucleus = fetch_identity_nucleus()
        runtime_material = {
            "schema_version": "agvm.grow_search_runtime_material.v1",
            "source": "persisted_public_search_store",
            "graph_snapshot_sha256": stable_digest(graph),
            "declared_graph_revision": _declared_graph_revision(graph),
            "atlas_sha256": stable_digest(atlas_payload),
            "atlas_node_count": int(atlas_payload.get("node_count") or 0),
            "atlas_bucket_count": int(atlas_payload.get("bucket_count") or len(list(atlas_payload.get("buckets") or []))),
            "identity_nucleus_sha256": stable_digest(identity_nucleus),
            "identity_core_node_count": len(list(identity_nucleus.get("core_nodes") or [])),
        }
        admission = require_search_ai_admission(
            query,
            search_identity_nucleus_for_named_targets(query.query_text, identity_nucleus),
        )
        if str(admission.get("status") or "") != "admitted":
            reason = str(admission.get("reason") or "search_ai_admission_rejected")
            provider_error = str(admission.get("provider_error") or reason)
            raise InvestigativeSearchError(f"search_retrieval_unavailable:{reason}:{provider_error}")
        plan = prepare_runtime_plan(
            query,
            atlas_payload,
            identity_nucleus,
            ai_admission=admission,
            defer_planner_seed=True,
        )
        result = retrieve_runtime(
            query,
            atlas_payload,
            identity_nucleus,
            prepared_plan=plan,
            search_id=str(request.get("child_call_id") or "").strip() or None,
        )
        if not isinstance(result, Mapping):
            raise InvestigativeSearchError("search_retrieval_unavailable:invalid_result")
        normalized_result = dict(result)
        # Search exposes its own semantic/atlas revision under ``brain_revision``.
        # Grow, however, binds preview/resume/apply to the immutable maintenance
        # graph revision supplied with this exact snapshot.  Preserve both
        # authorities instead of comparing two intentionally different digest
        # domains as though they were interchangeable.
        search_semantic_brain_revision = str(
            normalized_result.get("brain_revision")
            or _dict(normalized_result.get("semantic_contract_runtime")).get("brain_revision")
            or ""
        ).strip()
        normalized_result["search_semantic_brain_revision"] = search_semantic_brain_revision
        normalized_result["brain_revision"] = str(request.get("brain_revision") or "").strip()
        normalized_result["brain_id"] = runtime_brain_id or requested_brain_id or None
        normalized_result["search_ai_admission"] = dict(admission)
        normalized_result["grow_search_runtime_material"] = runtime_material
        return normalized_result
    except InvestigativeSearchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise InvestigativeSearchError(f"search_retrieval_unavailable:{exc}") from exc


def _evidence_node_ids(result: Mapping[str, Any]) -> list[str]:
    candidates: list[Any] = []

    def context_ref_node_id(item: Mapping[str, Any]) -> str:
        payload = dict(item)
        node_id = str(payload.get("node_id") or payload.get("id") or "").strip()
        if not node_id:
            return ""
        provenance = _dict(payload.get("provenance"))
        has_provenance = bool(
            provenance
            or str(payload.get("source_label") or "").strip()
            or str(payload.get("source_uri") or "").strip()
            or str(payload.get("source_type") or "").strip()
            or str(payload.get("content_digest") or payload.get("digest") or "").strip()
            or payload.get("source_span_start") is not None
            or payload.get("source_span_end") is not None
        )
        return node_id if has_provenance else ""

    for surface in (
        _dict(result.get("context_package")),
        _dict(result.get("context")),
        _dict(result.get("answer")),
    ):
        candidates.extend(surface.get("evidence_node_ids") or [])
        for key in ("evidence_refs", "evidence_items", "supporting_evidence", "support_evidence"):
            for item in _dicts(surface.get(key), limit=96):
                candidates.append(context_ref_node_id(item))
        for section in _dicts(surface.get("sections") or surface.get("structured_sections"), limit=96):
            for item in _dicts(section.get("items"), limit=96):
                candidates.append(context_ref_node_id(item))
    for match in _dicts(result.get("matches"), limit=96):
        candidates.append(context_ref_node_id(match))
    ledger = _dict(result.get("mission_evidence_ledger"))
    if not ledger:
        ledger = _dict(_dict(result.get("context_package")).get("mission_evidence_ledger"))
    for row in _dicts(ledger.get("rows"), limit=96):
        evidence = _dict(row.get("evidence"))
        candidates.extend(row.get("evidence_node_ids") or evidence.get("node_ids") or [])
        for evidence_key in ("hot_evidence", "cold_evidence"):
            for raw_item in list(row.get(evidence_key) or [])[:96]:
                if not isinstance(raw_item, Mapping):
                    candidates.append(raw_item)
                    continue
                item = dict(raw_item)
                candidates.extend(
                    [
                        item.get("node_id"),
                        item.get("id"),
                    ]
                )
                nested = _dict(item.get("evidence"))
                candidates.extend(nested.get("node_ids") or [])
    return _strings(candidates, limit=96)


def _document_references(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    sources: list[Any] = [
        result.get("document_refs"),
        result.get("supporting_documents"),
        result.get("source_trace"),
        _dict(result.get("context_package")).get("document_refs"),
        _dict(result.get("context_package")).get("source_trace"),
    ]
    ledger = _dict(
        result.get("mission_evidence_ledger")
        or _dict(result.get("context_package")).get("mission_evidence_ledger")
    )
    for row in _dicts(ledger.get("rows"), limit=96):
        sources.extend([row.get("document_refs"), row.get("document_refs_seen")])
    for source in sources:
        for item in _dicts(source, limit=48):
            normalized = {
                key: item.get(key)
                for key in (
                    "document_id",
                    "anchor_node_id",
                    "node_id",
                    "source_label",
                    "source_uri",
                    "source_type",
                    "title",
                    "digest",
                )
                if item.get(key) is not None
            }
            if normalized and normalized not in references:
                references.append(normalized)
            if len(references) >= 48:
                return references
    return references


def _context_excerpt(result: Mapping[str, Any]) -> str:
    context_package = _dict(result.get("context_package"))
    context = _dict(result.get("context"))
    if context_package:
        material: Any = context_package
    elif context:
        material = context
    else:
        material = {
            "matches": [
                {
                    "node_id": item.get("node_id") or item.get("id"),
                    "summary": item.get("summary") or _dict(item.get("node")).get("summary"),
                    "evidence_snippet": item.get("evidence_snippet"),
                }
                for item in _dicts(result.get("matches"), limit=12)
            ]
        }
    return json.dumps(material, ensure_ascii=False, sort_keys=True, default=str)[:18_000]


def _search_attestation(result: Mapping[str, Any]) -> tuple[dict[str, Any], bool, bool]:
    """Return a validated Search proof and whether provider/Master both ran."""

    execution = _dict(result.get("search_ai_execution"))
    validated_calls: list[dict[str, Any]] = []
    master_attestation: dict[str, Any] = {}
    for raw_call in _dicts(execution.get("calls"), limit=256):
        call_name = str(raw_call.get("call_name") or "").strip()
        candidate = _dict(raw_call.get("ai_execution_attestation"))
        try:
            validated = validate_ai_execution_attestation(candidate)
        except Exception:  # noqa: BLE001 - receipt proof is fail-closed
            continue
        if validated.get("provider_executed") is not True:
            continue
        validated_calls.append({**raw_call, "ai_execution_attestation": dict(validated)})
        if call_name.startswith("master_judge"):
            master_attestation = dict(validated)

    execution_status = str(execution.get("status") or "").casefold()
    execution_valid = bool(
        execution_status in {"completed", "review_required"}
        and execution.get("fallback_used") is False
        and validated_calls
    )
    if execution_valid:
        proof = {
            "schema_version": "agvm.search_execution_attestation.v2",
            "status": "completed" if execution_status == "completed" else "review_required",
            "provider_executed": True,
            "fallback_used": False,
            "call_count": len(validated_calls),
            "master_call_attested": bool(master_attestation),
            "calls_sha256": stable_digest(validated_calls),
            "usage": {
                key: sum(
                    int(_dict(_dict(item.get("ai_execution_attestation")).get("usage")).get(key) or 0)
                    for item in validated_calls
                )
                for key in ("input_tokens", "output_tokens", "reasoning_tokens", "total_tokens")
            },
            "master_ai_execution_attestation": master_attestation,
        }
        return proof, True, bool(master_attestation)

    # A narrowly-scoped injected runner may expose one aggregate attestation.
    # It is accepted only when the Master contract independently says AI V2 was
    # actually used; merely returning evidence IDs is never enough.
    for candidate in (
        result.get("ai_execution_attestation"),
        _dict(result.get("planner_runtime")).get("ai_execution_attestation"),
        _dict(result.get("semantic_contract_runtime")).get("ai_execution_attestation"),
    ):
        if isinstance(candidate, Mapping):
            try:
                validated = validate_ai_execution_attestation(candidate)
            except Exception:  # noqa: BLE001
                continue
            provider_attested = bool(validated.get("provider_executed") is True)
            master = _dict(result.get("master_judgement"))
            master_authority = _dict(master.get("semantic_authority"))
            master_attested = bool(
                master.get("master_ai_used") is True
                or _dict(master.get("sufficiency_judge")).get("master_ai_used") is True
                or master_authority.get("master_ai_used") is True
            )
            return dict(validated), provider_attested, master_attested
    return ({
        "schema_version": "agvm.search_execution_attestation.v1",
        "status": "completed",
        "search_runtime_executed": True,
        "provider_executed": False,
        "attestation_exposed": False,
    }, False, False)


def run_investigative_search(
    graph_snapshot: Mapping[str, Any],
    brain_revision: str,
    query_text: str,
    retrieval_mode: str,
    max_matches: int,
    correlation_id: str,
    *,
    brain_id: str | None = None,
    semantic_authority: Mapping[str, Any] | None = None,
    parent_operation_id: str | None = None,
    child_call_id: str | None = None,
    billing_scope: str = GROW_SEARCH_BILLING_SCOPE,
    idempotency_key: str | None = None,
    deadline_at_ms: int | None = None,
    search_runner: SearchRunner | None = None,
) -> GrowSearchReceiptMaterial:
    """Run real Search and normalize only evidence-bearing, revision-bound material.

    A local empty result never certifies novelty. ``authoritative_no_match`` is
    true only for an attested terminal Search no-match with valid payload
    integrity.
    """

    normalized_query = str(query_text or "").strip()
    normalized_revision = str(brain_revision or "").strip()
    normalized_mode = str(retrieval_mode or "balanced").strip().casefold()
    normalized_correlation = str(correlation_id or "").strip()
    normalized_brain_id = str(brain_id or "").strip()
    if not normalized_query:
        raise InvestigativeSearchError("investigative_search_query_required")
    if not normalized_revision:
        raise InvestigativeSearchError("investigative_search_brain_revision_required")
    if not normalized_correlation:
        raise InvestigativeSearchError("investigative_search_correlation_id_required")
    if normalized_mode not in RETRIEVAL_MODES:
        raise InvestigativeSearchError("investigative_search_retrieval_mode_invalid")
    normalized_limit = max(1, min(24, int(max_matches or 12)))
    declared_revision = _declared_graph_revision(graph_snapshot)
    if declared_revision and declared_revision != normalized_revision:
        raise InvestigativeSearchError(
            f"investigative_search_brain_revision_mismatch:{declared_revision}:{normalized_revision}"
        )
    declared_brain_id = _declared_graph_brain_id(graph_snapshot)
    if normalized_brain_id and declared_brain_id and declared_brain_id != normalized_brain_id:
        raise InvestigativeSearchError(
            f"investigative_search_graph_brain_mismatch:{declared_brain_id}:{normalized_brain_id}"
        )

    authority = {
        "semantic_decision_source": "provider",
        "query_authority": "provider",
        "semantic_fallback_used": False,
        **_dict(semantic_authority),
    }
    if (
        str(authority.get("semantic_decision_source") or "") != "provider"
        or str(authority.get("query_authority") or "") not in QUERY_AUTHORITIES
        or authority.get("semantic_fallback_used") is not False
    ):
        raise InvestigativeSearchError("investigative_search_semantic_authority_invalid")

    query_sha256 = stable_digest(normalized_query)
    graph_snapshot_sha256 = stable_digest(dict(graph_snapshot))
    resolved_parent_id = str(parent_operation_id or normalized_correlation).strip()
    resolved_child_id = str(
        child_call_id
        or f"grow-search::{normalized_correlation}::{query_sha256[:16]}"
    ).strip()
    resolved_idempotency_key = str(
        idempotency_key
        or stable_digest(
            {
                "parent_operation_id": resolved_parent_id,
                "child_call_id": resolved_child_id,
                "brain_revision": normalized_revision,
                "brain_id": normalized_brain_id or None,
                "query_sha256": query_sha256,
                "retrieval_mode": normalized_mode,
                "max_matches": normalized_limit,
            }
        )
    ).strip()
    envelope = {
        "graph_snapshot": dict(graph_snapshot),
        "brain_revision": normalized_revision,
        "brain_id": normalized_brain_id or None,
        "query_text": normalized_query,
        "response_mode": "context",
        "retrieval_mode": normalized_mode,
        "max_matches": normalized_limit,
        "correlation_id": normalized_correlation,
        "parent_operation_id": resolved_parent_id,
        "child_call_id": resolved_child_id,
        "billing_scope": str(billing_scope or GROW_SEARCH_BILLING_SCOPE),
        "idempotency_key": resolved_idempotency_key,
        "deadline_at_ms": deadline_at_ms,
        "semantic_authority": authority,
    }
    result = dict((search_runner or _public_search_runtime)(envelope))
    result_brain_id = str(result.get("brain_id") or "").strip()
    if normalized_brain_id and result_brain_id and result_brain_id != normalized_brain_id:
        raise InvestigativeSearchError(
            f"investigative_search_result_brain_mismatch:{result_brain_id}:{normalized_brain_id}"
        )
    attested_brain_id = normalized_brain_id or result_brain_id or declared_brain_id
    result_query = str(result.get("query_text") or normalized_query).strip()
    if result_query != normalized_query:
        raise InvestigativeSearchError("investigative_search_query_binding_mismatch")
    result_revision = str(
        result.get("brain_revision")
        or _dict(result.get("semantic_contract_runtime")).get("brain_revision")
        or normalized_revision
    ).strip()
    if result_revision != normalized_revision:
        raise InvestigativeSearchError("investigative_search_result_revision_mismatch")

    context_package = _dict(result.get("context_package"))
    master_judgement = _dict(
        result.get("master_judgement") or context_package.get("master_judgement")
    )
    mission_plan_v2 = _dict(
        result.get("mission_plan_v2")
        or _dict(result.get("semantic_contract_runtime")).get("mission_plan_v2")
        or _dict(result.get("semantic_contract")).get("mission_plan_v2")
        or _dict(result.get("planner_runtime")).get("mission_plan_v2")
    )
    mission_evidence_ledger = _dict(
        result.get("mission_evidence_ledger")
        or context_package.get("mission_evidence_ledger")
    )
    payload_integrity = _dict(result.get("payload_integrity"))
    payload_integrity_valid = bool(
        payload_integrity
        and (payload_integrity.get("passed") is True or payload_integrity.get("valid") is True)
    )
    search_attestation, search_attested, master_attested = _search_attestation(result)
    master_state = str(master_judgement.get("master_state") or "").strip().casefold()
    master_authority = _dict(master_judgement.get("semantic_authority"))
    master_contract_valid = bool(
        master_judgement
        and master_attested
        and master_authority.get("fallback_used") is False
        and str(master_authority.get("mode") or "").casefold() in {"ai_v2", "ai_attested_mission_plan_v2"}
    )
    ledger_status = str(mission_evidence_ledger.get("status") or "").strip().casefold()
    evidence_ledger_contract_valid = bool(
        mission_evidence_ledger
        and ledger_status in {"ready", "complete", "completed", "terminal"}
    )
    evidence_ledger_valid = bool(
        evidence_ledger_contract_valid
        and int(mission_evidence_ledger.get("row_count") or len(list(mission_evidence_ledger.get("rows") or []))) > 0
    )
    canonical_state = str(
        result.get("canonical_search_state")
        or result.get("completion_state")
        or result.get("status")
        or master_state
        or "evidence_ready"
    ).strip().casefold()
    terminal_for_client = bool(
        result.get("terminal_for_client") is True
        or master_judgement.get("terminal_for_client") is True
        or canonical_state in {"terminal", "complete", "completed", "finalized", "no_match"}
    )
    evidence_node_ids = _evidence_node_ids(result)
    matches = [
        {
            "node_id": str(item.get("node_id") or item.get("id") or _dict(item.get("node")).get("id") or ""),
            "summary": str(item.get("summary") or _dict(item.get("node")).get("summary") or "")[:600],
            "memory_type": str(item.get("memory_type") or _dict(item.get("node")).get("memory_type") or ""),
            "score": float(item.get("raw_score") or item.get("score") or 0.0),
        }
        for item in _dicts(result.get("matches"), limit=normalized_limit)
        if str(item.get("node_id") or item.get("id") or _dict(item.get("node")).get("id") or "").strip()
    ]
    document_references = _document_references(result)
    no_match_claim = bool(
        master_state == "no_match"
        or canonical_state == "no_match"
        or str(_dict(result.get("search_outcome_contract")).get("outcome") or "").casefold()
        == "no_match"
    )
    authoritative_no_match = bool(
        no_match_claim
        and terminal_for_client
        and payload_integrity_valid
        and search_attested
        and master_contract_valid
        and evidence_ledger_valid
    )
    planner_runtime = _dict(result.get("planner_runtime"))
    plan_first_runtime = _dict(
        result.get("plan_first_runtime") or planner_runtime.get("plan_first_runtime")
    )
    plan_first_no_match_certification = _dict(
        result.get("plan_first_no_match_certification")
    )
    ledger_digest = search_mission_ledger_digest(mission_evidence_ledger)
    master_ledger_digest = str(master_judgement.get("mission_ledger_digest") or "").strip()
    mission_plan_metadata_valid = bool(
        not mission_plan_v2
        or str(mission_plan_v2.get("schema_version") or "") == "agvm.search_mission_plan.v2"
    )
    eligible_reserves = {
        str(item).strip()
        for item in list(plan_first_runtime.get("reserve_eligible_branch_ids") or [])
        if str(item).strip()
    }
    completed_reserves = {
        str(item).strip()
        for item in list(plan_first_runtime.get("reserve_completed_branch_ids") or [])
        if str(item).strip()
    }
    plan_first_metadata_valid = bool(
        plan_first_runtime.get("schema_version") == "agvm.search_plan_first.v3"
        and plan_first_runtime.get("enabled") is True
        and plan_first_runtime.get("execution") == "bounded_primary_then_one_shot_reserve"
        and plan_first_runtime.get("primary_barrier_reached") is True
        and "reserve_activated_count" in plan_first_runtime
        and eligible_reserves.issubset(completed_reserves)
        and int(plan_first_runtime.get("final_master_attempt_count") or 0) == 1
        and int(plan_first_runtime.get("final_master_attested_count") or 0) == 1
        and master_attested
        and ledger_digest
        and master_ledger_digest == ledger_digest
    )
    proof_requirements = _dict(
        plan_first_no_match_certification.get("requirements")
    )
    proof_digest_alignment_valid = bool(
        str(plan_first_no_match_certification.get("plan_digest") or "").strip()
        and str(plan_first_no_match_certification.get("plan_digest") or "").strip()
        == str(plan_first_runtime.get("plan_digest") or "").strip()
        == str(master_judgement.get("plan_first_plan_digest") or "").strip()
        and str(plan_first_no_match_certification.get("ledger_digest") or "").strip()
        == ledger_digest
        == str(plan_first_runtime.get("ledger_digest") or "").strip()
        == master_ledger_digest
        and str(plan_first_no_match_certification.get("frozen_surface_digest") or "").strip()
        and str(plan_first_no_match_certification.get("frozen_surface_digest") or "").strip()
        == str(plan_first_runtime.get("frozen_surface_digest") or "").strip()
        == str(master_judgement.get("frozen_surface_digest") or "").strip()
    )
    proof_requirements_valid = bool(
        proof_requirements
        and all(
            proof_requirements.get(key) is True
            for key in (
                "plan_first_active",
                "primary_barrier_reached",
                "eligible_reserves_completed",
                "single_final_master",
                "plan_digest_matches",
                "ledger_digest_matches",
                "frozen_surface_digest_matches",
            )
        )
    )
    strong_no_match_contract = bool(
        mission_plan_metadata_valid
        and plan_first_metadata_valid
        and plan_first_no_match_certification.get("schema_version")
        == "agvm.search_plan_first_no_match.v1"
        and plan_first_no_match_certification.get("certified") is True
        and proof_requirements_valid
        and proof_digest_alignment_valid
    )
    authoritative_no_match = bool(
        authoritative_no_match and strong_no_match_contract
    )
    evidence_usable = bool(
        search_attested
        and master_contract_valid
        and evidence_ledger_valid
        and payload_integrity_valid
        and evidence_node_ids
    )
    novelty_certified = authoritative_no_match
    document_context_usable = bool(
        search_attested
        and master_contract_valid
        and evidence_ledger_contract_valid
        and payload_integrity_valid
        and document_references
    )
    # Clarification questions may be grounded either in returned evidence or
    # in a strongly certified absence.  This capability is intentionally
    # separate from decision_usable: an authoritative no-match can justify
    # asking the human to resolve an ambiguous source claim without pretending
    # that absence is positive brain evidence.
    partial_useful_matches = bool(
        search_attested
        and payload_integrity_valid
        and canonical_state == "review_required"
        and matches
    )
    question_usable = bool(
        evidence_usable
        or authoritative_no_match
        or document_context_usable
        or partial_useful_matches
    )
    partial_master_state = master_state in {
        "needs_more_search",
        "partial",
        "usable_partial",
        "review_required",
    }
    decision_usable = bool(
        novelty_certified
        or (evidence_usable and terminal_for_client and not partial_master_state)
    )
    # Compatibility alias for existing Grow/UI consumers.  New code uses the
    # explicit capability fields above rather than treating all successful
    # receipts as interchangeable.
    usable = decision_usable
    # An attested, revision-bound Search attempt with no evidence is still
    # useful to the AI investigator as a planning observation: it may reframe
    # a materially different query.  It is never evidence authority.  Human
    # questions use question_usable and memory decisions use
    # decision_usable/novelty_certified in grow_investigator.py.
    reviewable = bool(
        not usable
        and search_attested
        and payload_integrity_valid
        and (
            (
                master_contract_valid
                and evidence_ledger_contract_valid
                and (
                    master_state in {"needs_more_search", "partial", "usable_partial", "review_required"}
                    or not evidence_node_ids
                )
            )
            or partial_useful_matches
        )
    )
    if authoritative_no_match:
        contract_outcome = "authoritative_no_match"
    elif reviewable:
        contract_outcome = (
            "reviewable_evidence_found"
            if evidence_node_ids or partial_useful_matches
            else "reviewable_needs_more_search"
        )
    elif evidence_node_ids:
        contract_outcome = "evidence_found"
    elif terminal_for_client:
        contract_outcome = "terminal_unattested_or_integrity_invalid"
    else:
        contract_outcome = "non_terminal"

    receipt_core: dict[str, Any] = {
        "schema_version": GROW_SEARCH_RECEIPT_SCHEMA_VERSION,
        "call_id": resolved_child_id,
        "child_call_id": resolved_child_id,
        "correlation_id": normalized_correlation,
        "parent_operation_id": resolved_parent_id or None,
        "billing_scope": str(billing_scope or GROW_SEARCH_BILLING_SCOPE),
        "idempotency_key": resolved_idempotency_key,
        "query_text": normalized_query,
        "query_sha256": query_sha256,
        "retrieval_mode": normalized_mode,
        "max_matches": normalized_limit,
        "brain_revision": normalized_revision,
        "brain_id": attested_brain_id or None,
        "search_semantic_brain_revision": str(result.get("search_semantic_brain_revision") or "").strip(),
        "graph_snapshot_sha256": graph_snapshot_sha256,
        "terminal_state": canonical_state,
        "terminal_for_client": terminal_for_client,
        "contract_outcome": contract_outcome,
        "authoritative_no_match": authoritative_no_match,
        "evidence_usable": evidence_usable,
        "question_usable": question_usable,
        "decision_usable": decision_usable,
        "novelty_certified": novelty_certified,
        "usable": usable,
        "reviewable": reviewable,
        "context_package": context_package,
        "master_judgement": master_judgement,
        "mission_evidence_ledger": mission_evidence_ledger,
        "document_references": document_references,
        "evidence_node_ids": evidence_node_ids,
        "context_excerpt": _context_excerpt(result),
        "matches": matches,
        "payload_integrity": payload_integrity,
        "plan_first_no_match_certification": plan_first_no_match_certification,
        "search_outcome_contract": _dict(result.get("search_outcome_contract")),
        "completion_contract": _dict(result.get("completion_contract")),
        "run_lifecycle_contract": _dict(result.get("run_lifecycle_contract")),
        "grow_search_runtime_material": _dict(result.get("grow_search_runtime_material")),
        "search_execution_attestation": search_attestation,
        "search_ai_execution": _dict(result.get("search_ai_execution")),
        "search_authority_proof": {
            "provider_attested": search_attested,
            "master_attested": master_attested,
            "master_contract_valid": master_contract_valid,
            "evidence_ledger_valid": evidence_ledger_valid,
            "evidence_ledger_contract_valid": evidence_ledger_contract_valid,
            "payload_integrity_valid": payload_integrity_valid,
            "mission_plan_metadata_valid": mission_plan_metadata_valid,
            "plan_first_metadata_present": bool(plan_first_runtime),
            "plan_first_metadata_valid": plan_first_metadata_valid,
            "ledger_digest": ledger_digest or None,
            "master_ledger_digest": master_ledger_digest or None,
            "strong_no_match_contract": strong_no_match_contract,
            "document_context_usable": document_context_usable,
            "question_usable": question_usable,
            "partial_useful_matches": partial_useful_matches,
        },
        "semantic_authority": authority,
    }
    if mission_plan_v2:
        receipt_core["mission_plan_v2"] = mission_plan_v2
    receipt_core["result_sha256"] = stable_digest(receipt_core)
    receipt_core["receipt_id"] = f"grow-search-receipt::{receipt_core['result_sha256'][:24]}"
    return receipt_core  # type: ignore[return-value]


def hydrate_investigative_document_evidence(
    search_receipt: Mapping[str, Any],
    brain_revision: str,
    document_ids: list[str],
    *,
    max_documents: int = 8,
    max_children_per_document: int = 24,
    max_total_chars: int = 64_000,
    node_fetcher: NodeFetcher | None = None,
    child_fetcher: DocumentChildFetcher | None = None,
    sibling_fetcher: DocumentSiblingFetcher | None = None,
) -> dict[str, Any]:
    """Hydrate only documents already discovered by one usable Search receipt."""

    receipt = dict(search_receipt)
    normalized_revision = str(brain_revision or "").strip()
    receipt_revision = str(receipt.get("brain_revision") or "").strip()
    receipt_id = str(receipt.get("receipt_id") or "").strip()
    if not normalized_revision or receipt_revision != normalized_revision:
        raise InvestigativeDocumentEvidenceError("document_evidence_receipt_revision_stale")
    if not receipt_id or receipt.get("evidence_usable") is not True:
        raise InvestigativeDocumentEvidenceError("document_evidence_search_receipt_unusable")

    requested = _strings(document_ids, limit=max(1, min(32, int(max_documents))))
    if not requested:
        raise InvestigativeDocumentEvidenceError("document_evidence_ids_required")
    canonical_reference_by_id: dict[str, dict[str, Any]] = {}
    alias_targets: dict[str, set[str]] = {}
    ambiguous_ids: set[str] = set()
    for reference in _dicts(receipt.get("document_references"), limit=96):
        canonical_id = str(
            reference.get("document_id")
            or reference.get("anchor_node_id")
            or reference.get("node_id")
            or ""
        ).strip()
        if not canonical_id:
            continue
        canonical_identity = {
            key: reference.get(key)
            for key in (
                "document_id",
                "source_label",
                "source_uri",
                "source_type",
                "title",
                "digest",
            )
            if reference.get(key) is not None
        }
        previous = canonical_reference_by_id.get(canonical_id)
        if previous is not None and stable_digest(previous) != stable_digest(canonical_identity):
            ambiguous_ids.add(canonical_id)
        else:
            canonical_reference_by_id[canonical_id] = canonical_identity
        for alias_id in _strings(
            [canonical_id, reference.get("anchor_node_id"), reference.get("node_id")],
            limit=3,
        ):
            alias_targets.setdefault(alias_id, set()).add(canonical_id)
            if len(alias_targets[alias_id]) > 1:
                ambiguous_ids.add(alias_id)
    if any(document_id in ambiguous_ids for document_id in requested):
        raise InvestigativeDocumentEvidenceError("document_evidence_reference_ambiguous")
    if any(document_id not in alias_targets for document_id in requested):
        raise InvestigativeDocumentEvidenceError("document_evidence_id_not_discovered_by_search")
    resolved = _strings(
        [sorted(alias_targets[document_id])[0] for document_id in requested],
        limit=max(1, min(32, int(max_documents))),
    )
    if any(document_id in ambiguous_ids for document_id in resolved):
        raise InvestigativeDocumentEvidenceError("document_evidence_reference_ambiguous")

    if node_fetcher is None or child_fetcher is None or sibling_fetcher is None:
        try:
            from .sqlite_store import (
                fetch_document_child_nodes,
                fetch_document_source_sibling_nodes,
                fetch_nodes_by_ids,
            )
        except ImportError:  # pragma: no cover - direct API runtime
            from sqlite_store import (
                fetch_document_child_nodes,
                fetch_document_source_sibling_nodes,
                fetch_nodes_by_ids,
            )
        node_fetcher = node_fetcher or fetch_nodes_by_ids
        child_fetcher = child_fetcher or fetch_document_child_nodes
        sibling_fetcher = sibling_fetcher or fetch_document_source_sibling_nodes

    anchors = [dict(item) for item in node_fetcher(resolved, include_raw_text=True)]
    anchors_by_id = {str(item.get("id") or "").strip(): item for item in anchors}
    if any(document_id not in anchors_by_id for document_id in resolved):
        raise InvestigativeDocumentEvidenceError("document_evidence_anchor_missing")
    for document_id in resolved:
        anchor = anchors_by_id[document_id]
        is_document = bool(
            anchor.get("document_eligible")
            or anchor.get("is_document_anchor")
            or str(anchor.get("document_role") or "").strip().casefold() == "anchor"
            or str(anchor.get("memory_type") or "").strip().casefold() == "document_anchor"
        )
        if not is_document:
            raise InvestigativeDocumentEvidenceError("document_evidence_anchor_role_invalid")

    child_limit = max(1, min(96, int(max_children_per_document)))
    children = [
        dict(item)
        for item in child_fetcher(
            resolved,
            limit_per_anchor=child_limit,
            include_raw_text=True,
        )
    ]
    source_refs = [
        {
            **canonical_reference_by_id[document_id],
            "document_id": document_id,
            "document_anchor_id": document_id,
        }
        for document_id in resolved
    ]
    siblings = [
        dict(item)
        for item in sibling_fetcher(
            source_refs,
            limit_per_source=child_limit,
            include_raw_text=True,
        )
    ]
    allowed_anchor_ids = set(resolved)
    ordered_nodes: list[dict[str, Any]] = []
    seen_node_ids: set[str] = set()
    remaining_chars = max(1_000, min(1_000_000, int(max_total_chars)))
    for raw_node in [*[anchors_by_id[item] for item in resolved], *children, *siblings]:
        node_id = str(raw_node.get("id") or "").strip()
        if not node_id or node_id in seen_node_ids:
            continue
        matched_anchor_id = str(
            raw_node.get("document_anchor_id")
            or raw_node.get("_matched_document_anchor_id")
            or (node_id if node_id in allowed_anchor_ids else "")
        ).strip()
        if matched_anchor_id not in allowed_anchor_ids:
            continue
        raw_text = str(raw_node.get("raw_text") or "")
        included_text = raw_text[:remaining_chars]
        remaining_chars -= len(included_text)
        provenance = _dict(raw_node.get("provenance"))
        material = {
            "node_id": node_id,
            "document_anchor_id": matched_anchor_id,
            "document_role": str(raw_node.get("document_role") or ("anchor" if node_id == matched_anchor_id else "")),
            "canonical_text": included_text,
            "canonical_text_sha256": stable_digest(raw_text),
            "canonical_text_char_count": len(raw_text),
            "canonical_text_truncated": len(included_text) < len(raw_text),
            "summary": str(raw_node.get("summary") or "")[:2_000],
            "source_span_start": raw_node.get("source_span_start"),
            "source_span_end": raw_node.get("source_span_end"),
            "provenance": provenance,
        }
        material["digest"] = stable_digest(material)
        ordered_nodes.append(material)
        seen_node_ids.add(node_id)
        if remaining_chars <= 0:
            break

    source_trace = [
        {
            "node_id": item["node_id"],
            "document_anchor_id": item["document_anchor_id"],
            "document_role": item["document_role"],
            "source_span_start": item["source_span_start"],
            "source_span_end": item["source_span_end"],
            "source_label": _dict(item.get("provenance")).get("source_label"),
            "source_uri": _dict(item.get("provenance")).get("source_uri"),
            "content_sha256": item["canonical_text_sha256"],
            "material_digest": item["digest"],
        }
        for item in ordered_nodes
    ]
    core = {
        "schema_version": "agvm.grow_document_evidence_receipt.v1",
        "search_receipt_id": receipt_id,
        "brain_revision": normalized_revision,
        "document_ids": resolved,
        "requested_document_ids": requested,
        "evidence_node_ids": [item["node_id"] for item in ordered_nodes],
        "nodes": ordered_nodes,
        "source_trace": source_trace,
        "events": [
            {"event": "document_discovery_completed", "search_receipt_id": receipt_id, "document_ids": resolved},
            {"event": "document_hydration_completed", "document_ids": resolved, "node_count": len(ordered_nodes)},
            {"event": "source_trace_materialized", "row_count": len(source_trace)},
        ],
        "usable": bool(ordered_nodes),
        "provider_executed": False,
        "billing_child_created": False,
        "usage": {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0},
    }
    core["result_sha256"] = stable_digest(core)
    core["receipt_id"] = f"grow-document-evidence::{core['result_sha256'][:24]}"
    return core
