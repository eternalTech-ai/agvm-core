# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Literal, Mapping

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from brain_registry import BrainRegistryError, resolve_brain_scope
from llm import answer_model, structured_json
from runtime_scope import current_brain_id, use_runtime_brain
from sqlite_store import connect, fetch_search_events, fetch_search_session
from storage import utc_timestamp
from stream_contract import search_result_ref


_COMPOSITION_SCHEMA_VERSION = "agvm.search_composition.v1"
_CACHE_SCHEMA_VERSION = "agvm.search_composition_cache.v2"
_TERMINAL_SEARCH_STATES = {"completed", "failed", "blocked", "review_required", "superseded"}
_MILESTONES = {"plan", "evidence", "final"}
_OUTPUT_KINDS = {"answer", "search_narrative"}
_MAX_SEARCHES = 8
_MAX_EVIDENCE_ITEMS = 96
_MAX_EVENTS_PER_SEARCH = 180
_REPAIRABLE_SEARCH_NARRATIVE_CODES = {
    "search_composition_result_ref_invalid",
    "search_composition_evidence_ref_invalid",
    "search_composition_factual_statement_uncited",
}


def _bounded_composition_int(name: str, *, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.getenv(name) or "").strip() or default)
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _composition_model() -> str:
    return str(os.getenv("AGVM_SEARCH_COMPOSITION_MODEL") or "").strip() or answer_model()


def _composition_max_output_tokens() -> int:
    return _bounded_composition_int(
        "AGVM_SEARCH_COMPOSITION_MAX_OUTPUT_TOKENS",
        default=12000,
        minimum=3200,
        maximum=12000,
    )


def _composition_timeout_seconds() -> float:
    return float(
        _bounded_composition_int(
            "AGVM_SEARCH_COMPOSITION_TIMEOUT_SECONDS",
            default=120,
            minimum=30,
            maximum=180,
        )
    )


class SearchCompositionRequest(BaseModel):
    brain_id: str = Field(min_length=1, max_length=200)
    user_goal: str = Field(min_length=1, max_length=4000)
    search_ids: list[str] = Field(default_factory=list, max_length=_MAX_SEARCHES)
    result_refs: list[dict[str, Any] | str] = Field(default_factory=list, max_length=_MAX_SEARCHES)
    milestone: Literal["plan", "evidence", "final"] = "final"

    @field_validator("brain_id", "user_goal")
    @classmethod
    def _trim_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value_required")
        return normalized

    @field_validator("search_ids")
    @classmethod
    def _normalize_search_ids(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(str(value or "").strip() for value in values))
        if any(not value for value in normalized):
            raise ValueError("search_id_invalid")
        return normalized

    @model_validator(mode="after")
    def _require_search_reference(self) -> "SearchCompositionRequest":
        if not self.search_ids and not self.result_refs:
            raise ValueError("search_reference_required")
        return self


class SearchCompositionError(RuntimeError):
    def __init__(self, code: str, *, status_code: int, context: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.context = dict(context or {})


SearchLoader = Callable[[str], dict[str, Any] | None]
EventLoader = Callable[..., list[dict[str, Any]]]
Provider = Callable[..., tuple[dict[str, Any] | None, str | None]]
CacheReader = Callable[[str], dict[str, Any] | None]
CacheWriter = Callable[[str, dict[str, Any]], None]


def create_core_search_composition_router() -> APIRouter:
    router = APIRouter()

    @router.post("/memory/mcp/compose-search-narrative")
    @router.post("/mcp/compose-search-narrative")
    def compose_search_narrative(payload: SearchCompositionRequest) -> dict[str, Any]:
        return _run_route(payload, output_kind="search_narrative")

    @router.post("/memory/mcp/compose-grounded-answer")
    @router.post("/mcp/compose-grounded-answer")
    def compose_grounded_answer(payload: SearchCompositionRequest) -> dict[str, Any]:
        return _run_route(payload, output_kind="answer")

    return router


def _run_route(payload: SearchCompositionRequest, *, output_kind: str) -> dict[str, Any]:
    try:
        with _brain_scope(payload.brain_id):
            return compose_persisted_searches(
                payload,
                output_kind=output_kind,
                cache_reader=_read_cache,
                cache_writer=_write_cache,
            )
    except SearchCompositionError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, **exc.context},
        ) from exc


@contextmanager
def _brain_scope(brain_id: str) -> Iterator[dict[str, Any]]:
    try:
        brain_record = resolve_brain_scope(brain_id)
    except BrainRegistryError as exc:
        raise SearchCompositionError(str(exc), status_code=404) from exc
    with use_runtime_brain(brain_record):
        yield brain_record


def compose_persisted_searches(
    payload: SearchCompositionRequest,
    *,
    output_kind: str,
    search_loader: SearchLoader = fetch_search_session,
    event_loader: EventLoader = fetch_search_events,
    provider: Provider = structured_json,
    cache_reader: CacheReader | None = None,
    cache_writer: CacheWriter | None = None,
) -> dict[str, Any]:
    if output_kind not in _OUTPUT_KINDS:
        raise SearchCompositionError("composition_output_kind_invalid", status_code=422)
    if payload.milestone not in _MILESTONES:
        raise SearchCompositionError("composition_milestone_invalid", status_code=422)

    requested_refs = _normalize_requested_result_refs(payload.result_refs)
    ordered_search_ids = list(payload.search_ids)
    for reference in requested_refs:
        search_id = str(reference.get("search_id") or "").strip()
        if search_id and search_id not in ordered_search_ids:
            ordered_search_ids.append(search_id)
    if not ordered_search_ids or len(ordered_search_ids) > _MAX_SEARCHES:
        raise SearchCompositionError("search_reference_count_invalid", status_code=422)

    materials: list[dict[str, Any]] = []
    for search_id in ordered_search_ids:
        session = search_loader(search_id)
        if not session:
            raise SearchCompositionError(
                "search_not_found",
                status_code=404,
                context={"search_id": search_id},
            )
        _validate_search_brain(session, payload.brain_id, search_id)
        result = _milestone_result(session, payload.milestone, search_id)
        canonical_ref = search_result_ref(search_id, result) if result else {
            "search_id": search_id,
            "brain_id": payload.brain_id,
            "endpoint": f"/memory/query-result/{search_id}?brain_id={payload.brain_id}",
            "package_revision": _plan_revision(search_id, session),
        }
        _validate_requested_ref(search_id, canonical_ref, requested_refs)
        events = event_loader(search_id, after_seq=0, limit=_MAX_EVENTS_PER_SEARCH)
        materials.append(
            _build_search_material(
                session=session,
                result=result,
                events=events,
                canonical_ref=canonical_ref,
                milestone=payload.milestone,
            )
        )

    evidence_ledger = _shared_evidence_ledger(materials)
    grounding_authority = _composition_grounding_authority(materials)
    composition_mode = (
        "restricted_review"
        if output_kind == "answer" and not grounding_authority["assertive_answer_authorized"]
        else output_kind
    )
    revision_digest = _revision_digest(
        output_kind=output_kind,
        milestone=payload.milestone,
        user_goal=payload.user_goal,
        materials=materials,
        composition_mode=composition_mode,
    )
    if cache_reader:
        cached = cache_reader(revision_digest)
        if cached:
            return {**cached, "cache_hit": True}

    prompt_payload = {
        "schema_version": "agvm.search_composition_prompt.v1",
        "output_kind": output_kind,
        "composition_mode": composition_mode,
        "grounding_authority": grounding_authority,
        "milestone": payload.milestone,
        "user_goal": payload.user_goal,
        "searches": [material["prompt_projection"] for material in materials],
        "allowed_evidence": evidence_ledger["items"],
        "allowed_result_refs": evidence_ledger["result_refs"],
    }
    execution: dict[str, Any] = {}
    generated, provider_error = provider(
        model=_composition_model(),
        system_prompt=_system_prompt(output_kind, payload.milestone, composition_mode=composition_mode),
        user_prompt=json.dumps(prompt_payload, ensure_ascii=False, separators=(",", ":"), default=str),
        schema_name=f"agvm_{output_kind}_{payload.milestone}_v1",
        schema=_provider_schema(),
        timeout=_composition_timeout_seconds(),
        role="answer",
        max_output_tokens=_composition_max_output_tokens(),
        execution_metadata=execution,
    )
    if provider_error or not generated:
        raise SearchCompositionError(
            "search_composition_ai_failed",
            status_code=503,
            context={"provider_error": str(provider_error or "missing_output")[:300]},
        )

    repair_ledger = {
        "schema_version": "agvm.search_composition_repair_ledger.v1",
        "repair_count": 0,
        "attempted": False,
        "trigger_error": None,
        "status": "not_attempted",
    }
    try:
        validated = _validate_generated_output(
            generated,
            evidence_ledger=evidence_ledger,
            composition_mode=composition_mode,
        )
    except SearchCompositionError as exc:
        if output_kind != "search_narrative" or exc.code not in _REPAIRABLE_SEARCH_NARRATIVE_CODES:
            raise
        repair_ledger.update(
            {
                "attempted": True,
                "trigger_error": exc.code,
                "status": "running",
            }
        )
        generated = _repair_search_narrative_output(
            generated,
            provider=provider,
            evidence_ledger=evidence_ledger,
            composition_mode=composition_mode,
            validation_error=exc,
            execution=execution,
        )
        validated = _validate_generated_output(
            generated,
            evidence_ledger=evidence_ledger,
            composition_mode=composition_mode,
        )
        repair_ledger.update({"repair_count": 1, "status": "repaired"})
    response = {
        "schema_version": _COMPOSITION_SCHEMA_VERSION,
        "status": "ok",
        "output_kind": output_kind,
        "composition_mode": composition_mode,
        "grounding_authority": grounding_authority,
        "brain_id": payload.brain_id,
        "user_goal": payload.user_goal,
        "milestone": payload.milestone,
        "search_ids": ordered_search_ids,
        "result_refs": [material["result_ref"] for material in materials],
        "revision_digest": revision_digest,
        "cache_hit": False,
        "title": validated["title"],
        "lead": validated["lead"],
        "sections": validated["sections"],
        "uncertainties": validated["uncertainties"],
        "evidence_ledger": {
            "schema_version": evidence_ledger["schema_version"],
            "result_refs": evidence_ledger["result_refs"],
            "evidence_refs": evidence_ledger["evidence_refs"],
        },
        "repair_ledger": repair_ledger,
        "provider_attestation": execution,
        "truth_contract": {
            "server_loaded_persisted_searches": True,
            "client_claims_accepted": False,
            "citations_subset_validated": True,
            "search_outcome_unchanged": True,
            "search_sufficiency_unchanged": True,
            "new_search_started": False,
            "semantic_fallback_used": False,
            "search_narrative_repair_count": repair_ledger["repair_count"],
            "assertive_answer_authorized": grounding_authority["assertive_answer_authorized"],
            "restricted_to_review": composition_mode == "restricted_review",
        },
        "created_at": utc_timestamp(),
    }
    if cache_writer:
        cache_writer(revision_digest, response)
    return response


def _repair_search_narrative_output(
    generated: dict[str, Any],
    *,
    provider: Provider,
    evidence_ledger: dict[str, Any],
    composition_mode: str,
    validation_error: SearchCompositionError,
    execution: dict[str, Any],
) -> dict[str, Any]:
    repair_prompt = {
        "schema_version": "agvm.search_composition_repair_prompt.v1",
        "mode": {
            "output_kind": "search_narrative",
            "composition_mode": composition_mode,
        },
        "validation_error": {
            "code": validation_error.code,
            "context": validation_error.context,
        },
        "allowed_result_refs": evidence_ledger["result_refs"],
        "allowed_evidence_refs": evidence_ledger["evidence_refs"],
        "generated_output": generated,
    }
    repaired, provider_error = provider(
        model=_composition_model(),
        system_prompt=_repair_system_prompt(composition_mode=composition_mode),
        user_prompt=json.dumps(repair_prompt, ensure_ascii=False, separators=(",", ":"), default=str),
        schema_name="agvm_search_narrative_repair_v1",
        schema=_provider_schema(),
        timeout=_composition_timeout_seconds(),
        role="answer",
        max_output_tokens=_composition_max_output_tokens(),
        execution_metadata=execution,
    )
    if provider_error or not repaired:
        raise SearchCompositionError(
            "search_composition_ai_repair_failed",
            status_code=503,
            context={
                "trigger_error": validation_error.code,
                "provider_error": str(provider_error or "missing_output")[:300],
            },
        )
    return repaired


def _repair_system_prompt(*, composition_mode: str) -> str:
    review_clause = (
        "The composition is in restricted_review mode, so do not emit paragraph kind answer or finding. "
        if composition_mode == "restricted_review"
        else ""
    )
    return (
        "You repair one invalid AGVM Search narrative JSON object. Use only the supplied generated_output, "
        "allowed_result_refs, allowed_evidence_refs, mode, and validation_error. Do not introduce new facts, "
        "new searches, new source labels, fallback prose, or any reference not explicitly allowed. Preserve the "
        "meaning of the generated output as much as possible while replacing invalid or missing refs with allowed "
        "refs only when the statement remains supported. If a factual statement cannot be supported by an allowed "
        "evidence ref, rewrite it as uncertainty or next_step rather than inventing evidence. Every paragraph and "
        "uncertainty must cite at least one allowed result_ref. "
        f"{review_clause}"
        "Return only the strict JSON schema."
    )


def _normalize_requested_result_refs(values: list[dict[str, Any] | str]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for raw in values:
        if isinstance(raw, str):
            raw_ref = raw.strip()
            package_revision = ""
            brain_id = ""
            if raw_ref.startswith("result:") and "@" in raw_ref:
                search_id, package_revision = raw_ref.removeprefix("result:").split("@", 1)
            elif "/memory/query-result/" in raw_ref:
                search_id = raw_ref.split("/memory/query-result/", 1)[1].split("?", 1)[0]
            else:
                search_id = raw_ref
        elif isinstance(raw, Mapping):
            search_id = str(raw.get("search_id") or "").strip()
            package_revision = str(raw.get("package_revision") or "").strip()
            brain_id = str(raw.get("brain_id") or "").strip()
        else:
            search_id = ""
            package_revision = ""
            brain_id = ""
        if not search_id:
            raise SearchCompositionError("result_ref_invalid", status_code=422)
        normalized.append(
            {
                "search_id": search_id,
                "package_revision": package_revision,
                "brain_id": brain_id,
            }
        )
    return normalized


def _validate_search_brain(session: dict[str, Any], brain_id: str, search_id: str) -> None:
    request = dict(session.get("request") or {})
    session_brain_id = str(request.get("brain_id") or "").strip()
    if session_brain_id and session_brain_id != brain_id:
        raise SearchCompositionError(
            "search_brain_scope_mismatch",
            status_code=403,
            context={"search_id": search_id},
        )


def _milestone_result(session: dict[str, Any], milestone: str, search_id: str) -> dict[str, Any]:
    status = str(session.get("status") or "").strip().lower()
    snapshots = dict(session.get("result_snapshots") or {})
    result: dict[str, Any] = {}
    if milestone == "evidence":
        result = dict(snapshots.get("latest_useful") or snapshots.get("first_useful") or session.get("result") or {})
    elif milestone == "final":
        if status not in _TERMINAL_SEARCH_STATES:
            raise SearchCompositionError(
                "search_not_terminal",
                status_code=409,
                context={"search_id": search_id, "search_status": status},
            )
        result = dict(snapshots.get("final") or session.get("result") or {})
        if not result:
            raise SearchCompositionError(
                "search_terminal_result_missing",
                status_code=409,
                context={"search_id": search_id, "search_status": status},
            )
    if milestone == "evidence" and not result:
        raise SearchCompositionError(
            "search_evidence_not_ready",
            status_code=409,
            context={"search_id": search_id},
        )
    return result


def _plan_revision(search_id: str, session: dict[str, Any]) -> str:
    material = json.dumps(
        {
            "search_id": search_id,
            "plan": session.get("plan") or {},
            "updated_at": session.get("updated_at"),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return f"sha256:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def _validate_requested_ref(
    search_id: str,
    canonical_ref: dict[str, Any],
    requested_refs: list[dict[str, str]],
) -> None:
    for requested in requested_refs:
        if requested["search_id"] != search_id:
            continue
        expected_revision = str(canonical_ref.get("package_revision") or "")
        if requested["package_revision"] and requested["package_revision"] != expected_revision:
            raise SearchCompositionError(
                "result_ref_revision_mismatch",
                status_code=409,
                context={"search_id": search_id},
            )
        expected_brain = str(canonical_ref.get("brain_id") or "")
        if requested["brain_id"] and expected_brain and requested["brain_id"] != expected_brain:
            raise SearchCompositionError(
                "result_ref_brain_mismatch",
                status_code=403,
                context={"search_id": search_id},
            )


def _result_ref_token(reference: dict[str, Any]) -> str:
    return f"result:{reference.get('search_id')}@{reference.get('package_revision')}"


def _build_search_material(
    *,
    session: dict[str, Any],
    result: dict[str, Any],
    events: list[dict[str, Any]],
    canonical_ref: dict[str, Any],
    milestone: str,
) -> dict[str, Any]:
    search_id = str(session.get("search_id") or canonical_ref.get("search_id") or "")
    plan = dict(session.get("plan") or {})
    plan_projection = _plan_projection(plan)
    event_projection = _event_projection(events)
    result_projection = _result_projection(result, session_status=str(session.get("status") or ""))
    evidence = _evidence_projection(result, search_id=search_id)
    result_ref_token = _result_ref_token(canonical_ref)
    return {
        "search_id": search_id,
        "result_ref": canonical_ref,
        "result_ref_token": result_ref_token,
        "evidence": evidence,
        "prompt_projection": {
            "search_id": search_id,
            "result_ref": result_ref_token,
            "query_text": str(session.get("query_text") or dict(session.get("request") or {}).get("query_text") or "")[:4000],
            "retrieval_mode": str(session.get("response_mode") or dict(session.get("request") or {}).get("retrieval_mode") or ""),
            "session_status": str(session.get("status") or ""),
            "milestone": milestone,
            "plan": plan_projection,
            "events": event_projection,
            "result_truth": result_projection,
            "evidence_refs": [item["evidence_ref"] for item in evidence],
        },
    }


def _plan_projection(plan: dict[str, Any]) -> dict[str, Any]:
    mission_plan = dict(plan.get("mission_plan_v2") or {})
    raw_missions = list(mission_plan.get("missions") or plan.get("missions") or plan.get("probes") or [])
    missions: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_missions[:24]):
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        missions.append(
            {
                "mission_id": str(item.get("mission_id") or item.get("probe_id") or f"mission_{index + 1}")[:160],
                "goal": str(item.get("semantic_goal") or item.get("goal") or item.get("query_text") or "")[:700],
                "success_criteria": list(item.get("success_criteria") or [])[:8],
                "expected_evidence": item.get("expected_evidence_shape") or item.get("expected_evidence") or {},
            }
        )
    return {
        "mission_count": len(missions),
        "missions": missions,
        "planner_status": str(dict(plan.get("planner_runtime") or {}).get("semantic_contract_status") or ""),
    }


def _event_projection(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for raw in events[:_MAX_EVENTS_PER_SEARCH]:
        if not isinstance(raw, Mapping):
            continue
        payload = dict(raw.get("payload") or {})
        row = {
            "seq": raw.get("seq"),
            "event_type": str(raw.get("event_type") or "")[:100],
            "mission_id": str(payload.get("mission_id") or payload.get("probe_id") or "")[:160],
            "path_id": str(payload.get("path_id") or payload.get("branch_id") or "")[:160],
            "state": str(payload.get("state") or payload.get("status") or payload.get("route_state") or "")[:120],
            "reason": str(payload.get("reason") or payload.get("stop_reason") or payload.get("decision_reason") or "")[:600],
            "goal": str(payload.get("goal") or payload.get("semantic_goal") or "")[:600],
            "evidence_count": payload.get("evidence_count") or payload.get("promoted") or payload.get("match_count"),
        }
        if any(value not in (None, "", 0, []) for key, value in row.items() if key not in {"seq", "event_type"}) or row["event_type"]:
            projected.append(row)
    return projected


def _result_projection(result: dict[str, Any], *, session_status: str) -> dict[str, Any]:
    ledger = dict(result.get("mission_evidence_ledger") or dict(result.get("context_package") or {}).get("mission_evidence_ledger") or {})
    mission_rows: list[dict[str, Any]] = []
    for raw in list(ledger.get("rows") or [])[:24]:
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        judgement = dict(row.get("branch_judgement") or {})
        mission_rows.append(
            {
                "mission_id": str(row.get("mission_id") or "")[:160],
                "goal": str(row.get("goal") or "")[:700],
                "coverage_state": str(row.get("coverage_state") or "")[:120],
                "coverage_reason": str(row.get("coverage_reason") or "")[:600],
                "branch_state": str(judgement.get("state") or "")[:120],
                "branch_reason_codes": list(judgement.get("reason_codes") or [])[:12],
                "next_action": str(judgement.get("next_recommended_action") or "")[:300],
            }
        )
    master = dict(result.get("master_judgement") or result.get("master_sufficiency") or {})
    sufficiency = dict(result.get("sufficiency_judge") or master.get("sufficiency_judge") or {})
    master_decision = dict(sufficiency.get("ai_master_decision") or {})
    human_findings = _review_diagnostics_projection(
        list(master.get("human_findings") or []) + list(master.get("issue_diagnostics") or [])
    )
    return {
        "session_status": session_status,
        "canonical_search_state": str(result.get("canonical_search_state") or result.get("status") or ""),
        "stop_reason": str(result.get("stop_reason") or "")[:500],
        "answerability_state": str(result.get("answerability_state") or "")[:120],
        "final_closure_ready": result.get("final_closure_ready"),
        "master_state": str(master.get("master_state") or master.get("state") or "")[:120],
        "master_reason": str(master.get("reason") or master.get("human_reason") or "")[:800],
        "master_terminal_for_client": master.get("terminal_for_client"),
        "master_final_seal_allowed": master.get("final_seal_allowed"),
        "master_review_required": master.get("review_required"),
        "master_ai_used": master.get("master_ai_used"),
        "master_decision": str(master_decision.get("decision") or "")[:120],
        "master_can_answer_now": master_decision.get("can_answer_now"),
        "master_decision_reason": str(master_decision.get("reason") or "")[:1600],
        "master_unresolved_gap": str(
            master_decision.get("unresolved_gap") or master.get("unresolved_gap") or ""
        )[:800],
        "master_grounded_answer_present": bool(dict(master_decision.get("grounded_answer") or {})),
        "missing_goals": list(master.get("missing_goals") or sufficiency.get("missing_goals") or [])[:20],
        "reason_codes": list(master.get("reason_codes") or sufficiency.get("reason_codes") or [])[:20],
        "review_diagnostics": human_findings,
        "mission_rows": mission_rows,
    }


def _review_diagnostics_projection(values: list[Any]) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for raw in values[:16]:
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        projected.append(
            {
                "summary": str(item.get("summary") or item.get("issue") or "")[:1200],
                "reason": str(item.get("reason") or item.get("why") or "")[:1600],
                "impact": str(item.get("impact") or "")[:1200],
                "next_step": str(item.get("next_step") or "")[:1200],
                "finding_type": str(item.get("finding_type") or "")[:120],
                "severity": str(item.get("severity") or "")[:80],
            }
        )
    return projected


def _terminal_grounded_result(result_truth: Mapping[str, Any]) -> bool:
    return bool(
        str(result_truth.get("canonical_search_state") or "").strip().lower() == "completed"
        and str(result_truth.get("answerability_state") or "").strip().lower() == "grounded"
        and str(result_truth.get("master_state") or "").strip().lower() == "terminal"
        and result_truth.get("master_terminal_for_client") is True
        and result_truth.get("master_final_seal_allowed") is True
        and result_truth.get("master_review_required") is False
        and result_truth.get("master_ai_used") is True
        and result_truth.get("master_can_answer_now") is True
        and result_truth.get("master_grounded_answer_present") is True
    )


def _composition_grounding_authority(materials: list[dict[str, Any]]) -> dict[str, Any]:
    search_states: list[dict[str, Any]] = []
    for material in materials:
        truth = dict(dict(material.get("prompt_projection") or {}).get("result_truth") or {})
        search_states.append(
            {
                "search_id": str(material.get("search_id") or ""),
                "canonical_search_state": str(truth.get("canonical_search_state") or ""),
                "answerability_state": str(truth.get("answerability_state") or ""),
                "master_state": str(truth.get("master_state") or ""),
                "terminal_grounded": _terminal_grounded_result(truth),
            }
        )
    return {
        "schema_version": "agvm.search_composition_grounding_authority.v1",
        "assertive_answer_authorized": bool(search_states)
        and all(item["terminal_grounded"] for item in search_states),
        "searches": search_states,
    }


def _evidence_projection(result: dict[str, Any], *, search_id: str) -> list[dict[str, Any]]:
    collected: dict[str, dict[str, Any]] = {}

    def add(item: Mapping[str, Any], *, kind: str) -> None:
        identifier = str(
            item.get("node_id")
            or item.get("evidence_id")
            or item.get("document_id")
            or item.get("anchor_node_id")
            or ""
        ).strip()
        if not identifier:
            return
        evidence_ref = f"evidence:{kind}:{identifier}"
        text = str(
            item.get("text")
            or item.get("summary")
            or item.get("evidence_snippet")
            or item.get("text_preview")
            or item.get("title")
            or item.get("source_title")
            or ""
        ).strip()
        if not text:
            return
        candidate = {
            "evidence_ref": evidence_ref,
            "kind": kind,
            "search_id": search_id,
            "summary": text[:900],
            "title": str(item.get("title") or item.get("source_title") or item.get("source_label") or "")[:300],
            "source": str(item.get("source_label") or item.get("source") or item.get("provenance") or "")[:300],
            "timestamp": str(item.get("event_time") or item.get("observed_at") or item.get("created_at") or "")[:120],
        }
        previous = collected.get(evidence_ref)
        if previous is None or len(candidate["summary"]) > len(previous["summary"]):
            collected[evidence_ref] = candidate

    for item in list(result.get("matches") or []):
        if isinstance(item, Mapping):
            add(item, kind="memory")
    for item in list(result.get("document_refs") or []):
        if isinstance(item, Mapping):
            add(item, kind="document")
    context_package = dict(result.get("context_package") or {})
    for section_key in ("hot_sections", "cold_sections"):
        for section in list(context_package.get(section_key) or []):
            if not isinstance(section, Mapping):
                continue
            for item in list(section.get("items") or []):
                if isinstance(item, Mapping):
                    add(item, kind="memory")
    ledger = dict(result.get("mission_evidence_ledger") or context_package.get("mission_evidence_ledger") or {})
    for row in list(ledger.get("rows") or []):
        if not isinstance(row, Mapping):
            continue
        for lane, kind in (("hot_evidence", "memory"), ("cold_evidence", "memory"), ("document_refs", "document")):
            for item in list(row.get(lane) or []):
                if isinstance(item, Mapping):
                    add(item, kind=kind)
    return list(collected.values())[:_MAX_EVIDENCE_ITEMS]


def _shared_evidence_ledger(materials: list[dict[str, Any]]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    result_refs: list[str] = []
    for material in materials:
        result_refs.append(material["result_ref_token"])
        for item in material["evidence"]:
            ref = str(item.get("evidence_ref") or "")
            if ref and ref not in seen:
                seen.add(ref)
                items.append(item)
    return {
        "schema_version": "agvm.search_composition_evidence_ledger.v1",
        "items": items[:_MAX_EVIDENCE_ITEMS],
        "evidence_refs": [item["evidence_ref"] for item in items[:_MAX_EVIDENCE_ITEMS]],
        "result_refs": result_refs,
    }


def _revision_digest(
    *,
    output_kind: str,
    milestone: str,
    user_goal: str,
    materials: list[dict[str, Any]],
    composition_mode: str = "",
) -> str:
    canonical = json.dumps(
        {
            "schema_version": _CACHE_SCHEMA_VERSION,
            "output_kind": output_kind,
            "milestone": milestone,
            "user_goal": user_goal,
            "composition_mode": composition_mode,
            "result_refs": [material["result_ref_token"] for material in materials],
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _system_prompt(output_kind: str, milestone: str, *, composition_mode: str) -> str:
    if composition_mode == "restricted_review":
        return (
            "You are the evidence-bound AGVM Search review narrator. The persisted Search is not authorized to produce "
            "an assertive answer. Explain in clear human language what Search attempted, what evidence it actually found, "
            "what remains uncertain, and the exact persisted reason why it was not certified. Do not answer the user's "
            "goal as established fact and do not turn hypotheses, mission goals, document titles, or unhydrated references "
            "into conclusions. Use review_finding only for evidence explicitly present in allowed_evidence. Use uncertainty "
            "and next_step for unresolved material. Never emit paragraph kind answer or finding. Every paragraph must cite "
            "an allowed result_ref; review_finding paragraphs must cite exact allowed evidence_refs. Do not expose internal "
            "IDs in prose, reinterpret Search sufficiency, request or start another Search, or use a semantic fallback. "
            "Return only the strict JSON schema."
        )
    purpose = (
        "Answer the user's goal in clear business prose"
        if output_kind == "answer"
        else "Explain in clear business prose how AGVM searched and what it learned"
    )
    return (
        "You are the evidence-bound AGVM composition layer. "
        f"{purpose} for the {milestone} milestone. "
        "Use only the supplied persisted Search projections. Never invent a fact, source, path, query, certainty, or outcome. "
        "Every paragraph must cite at least one allowed result_ref; factual paragraphs must also cite the exact allowed evidence_refs. "
        "Explain uncertainty precisely and in human language: say what remains uncertain, why Search could not certify it, the impact, "
        "and the useful next step. Do not expose internal IDs in prose. Do not reinterpret Search sufficiency or claim that a partial run "
        "is complete. Do not request or start another Search. Return only the strict JSON schema."
    )


def _bound_paragraph_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "paragraph_id": {"type": "string", "minLength": 1, "maxLength": 120},
            "kind": {
                "type": "string",
                "enum": [
                    "search_intent",
                    "mission",
                    "finding",
                    "review_finding",
                    "discarded_path",
                    "answer",
                    "uncertainty",
                    "next_step",
                ],
            },
            "text": {"type": "string", "minLength": 1, "maxLength": 4000},
            "evidence_refs": {"type": "array", "items": {"type": "string"}, "maxItems": 16},
            "result_refs": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8},
        },
        "required": ["paragraph_id", "kind", "text", "evidence_refs", "result_refs"],
        "additionalProperties": False,
    }


def _provider_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 240},
            "lead": {"type": "array", "items": _bound_paragraph_schema(), "minItems": 1, "maxItems": 4},
            "sections": {
                "type": "array",
                "minItems": 1,
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "properties": {
                        "heading": {"type": "string", "minLength": 1, "maxLength": 180},
                        "paragraphs": {"type": "array", "items": _bound_paragraph_schema(), "minItems": 1, "maxItems": 8},
                    },
                    "required": ["heading", "paragraphs"],
                    "additionalProperties": False,
                },
            },
            "uncertainties": {
                "type": "array",
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "properties": {
                        "uncertainty_id": {"type": "string", "minLength": 1, "maxLength": 120},
                        "what": {"type": "string", "minLength": 1, "maxLength": 1200},
                        "why": {"type": "string", "minLength": 1, "maxLength": 1600},
                        "impact": {"type": "string", "minLength": 1, "maxLength": 1200},
                        "next_step": {"type": "string", "minLength": 1, "maxLength": 1200},
                        "evidence_refs": {"type": "array", "items": {"type": "string"}, "maxItems": 16},
                        "result_refs": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8},
                    },
                    "required": ["uncertainty_id", "what", "why", "impact", "next_step", "evidence_refs", "result_refs"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["title", "lead", "sections", "uncertainties"],
        "additionalProperties": False,
    }


def _validate_generated_output(
    generated: dict[str, Any],
    *,
    evidence_ledger: dict[str, Any],
    composition_mode: str = "answer",
) -> dict[str, Any]:
    allowed_evidence = set(evidence_ledger["evidence_refs"])
    allowed_results = set(evidence_ledger["result_refs"])
    title = str(generated.get("title") or "").strip()
    lead = list(generated.get("lead") or [])
    sections = list(generated.get("sections") or [])
    uncertainties = list(generated.get("uncertainties") or [])
    if not title or not lead or not sections:
        raise SearchCompositionError("search_composition_output_incomplete", status_code=502)

    paragraph_ids: set[str] = set()

    def validate_refs(item: Mapping[str, Any], *, item_id: str, factual: bool) -> dict[str, Any]:
        result_refs = list(dict.fromkeys(str(value).strip() for value in list(item.get("result_refs") or []) if str(value).strip()))
        evidence_refs = list(dict.fromkeys(str(value).strip() for value in list(item.get("evidence_refs") or []) if str(value).strip()))
        if not result_refs or not set(result_refs).issubset(allowed_results):
            raise SearchCompositionError(
                "search_composition_result_ref_invalid",
                status_code=502,
                context={"statement_id": item_id},
            )
        if not set(evidence_refs).issubset(allowed_evidence):
            raise SearchCompositionError(
                "search_composition_evidence_ref_invalid",
                status_code=502,
                context={"statement_id": item_id},
            )
        if factual and not evidence_refs:
            raise SearchCompositionError(
                "search_composition_factual_statement_uncited",
                status_code=502,
                context={"statement_id": item_id},
            )
        return {**dict(item), "evidence_refs": evidence_refs, "result_refs": result_refs}

    def validate_paragraph(raw: Any) -> dict[str, Any]:
        item = dict(raw) if isinstance(raw, Mapping) else {}
        paragraph_id = str(item.get("paragraph_id") or "").strip()
        kind = str(item.get("kind") or "").strip()
        text = str(item.get("text") or "").strip()
        if not paragraph_id or paragraph_id in paragraph_ids or not kind or not text:
            raise SearchCompositionError("search_composition_statement_invalid", status_code=502)
        paragraph_ids.add(paragraph_id)
        if composition_mode == "restricted_review" and kind in {"finding", "answer"}:
            raise SearchCompositionError(
                "search_composition_assertive_answer_forbidden",
                status_code=502,
                context={"statement_id": paragraph_id},
            )
        factual = kind in {"finding", "answer", "review_finding"}
        return validate_refs({**item, "paragraph_id": paragraph_id, "kind": kind, "text": text}, item_id=paragraph_id, factual=factual)

    validated_lead = [validate_paragraph(item) for item in lead]
    validated_sections: list[dict[str, Any]] = []
    for raw_section in sections:
        section = dict(raw_section) if isinstance(raw_section, Mapping) else {}
        heading = str(section.get("heading") or "").strip()
        paragraphs = [validate_paragraph(item) for item in list(section.get("paragraphs") or [])]
        if not heading or not paragraphs:
            raise SearchCompositionError("search_composition_section_invalid", status_code=502)
        validated_sections.append({"heading": heading, "paragraphs": paragraphs})

    validated_uncertainties: list[dict[str, Any]] = []
    uncertainty_ids: set[str] = set()
    for raw in uncertainties:
        item = dict(raw) if isinstance(raw, Mapping) else {}
        uncertainty_id = str(item.get("uncertainty_id") or "").strip()
        if not uncertainty_id or uncertainty_id in uncertainty_ids:
            raise SearchCompositionError("search_composition_uncertainty_invalid", status_code=502)
        uncertainty_ids.add(uncertainty_id)
        if not all(str(item.get(key) or "").strip() for key in ("what", "why", "impact", "next_step")):
            raise SearchCompositionError("search_composition_uncertainty_invalid", status_code=502)
        validated_uncertainties.append(validate_refs(item, item_id=uncertainty_id, factual=False))

    if composition_mode == "restricted_review" and not validated_uncertainties:
        raise SearchCompositionError(
            "search_composition_review_uncertainty_required",
            status_code=502,
        )

    return {
        "title": title,
        "lead": validated_lead,
        "sections": validated_sections,
        "uncertainties": validated_uncertainties,
    }


def _ensure_cache_table() -> None:
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS search_composition_cache (
                revision_digest TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL,
                response_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def _read_cache(revision_digest: str) -> dict[str, Any] | None:
    _ensure_cache_table()
    with connect() as conn:
        row = conn.execute(
            "SELECT response_json FROM search_composition_cache WHERE revision_digest = ?",
            (revision_digest,),
        ).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(str(row["response_json"] or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return dict(payload) if isinstance(payload, dict) else None


def _write_cache(revision_digest: str, response: dict[str, Any]) -> None:
    _ensure_cache_table()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO search_composition_cache (revision_digest, schema_version, response_json, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(revision_digest) DO UPDATE SET
                schema_version = excluded.schema_version,
                response_json = excluded.response_json,
                created_at = excluded.created_at
            """,
            (
                revision_digest,
                _CACHE_SCHEMA_VERSION,
                json.dumps(response, ensure_ascii=False, separators=(",", ":"), default=str),
                utc_timestamp(),
            ),
        )
        conn.commit()
