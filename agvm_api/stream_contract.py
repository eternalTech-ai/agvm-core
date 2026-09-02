# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any
from urllib.parse import urlencode


STREAM_EVENT_CONTRACT_SCHEMA_VERSION = "agvm.brain_os_stream_event.v1"
SEARCH_PACKAGE_REVISION_SCHEMA_VERSION = "agvm.search_package_revision.v1"
SEARCH_RESULT_SNAPSHOT_SCHEMA_VERSION = "agvm.search_result_snapshot.v2"
COMPANY_RUN_EVENT_SCHEMA_VERSION = "company-run-event.v0"
COMPANY_RUN_EVENT_ADAPTER_VERSION = "company-director.v0"
COMPANY_FINAL_RECEIPT_SCHEMA_VERSION = "naffco.ai_director.final_receipt.v1"
SEARCH_SEMANTIC_PROJECTION_SCHEMA_VERSION = "agvm.search_semantic_projection.v1"
COMPANY_DOCUMENT_TRIAGE_SCHEMA_VERSION = "company-document-triage.v0"
COMPANY_DOCUMENT_TRIAGE_MAX_CANDIDATES = 8
COMPANY_DOCUMENT_TRIAGE_EVALUATOR_CONCURRENCY = 3
COMPANY_DOCUMENT_TRIAGE_HYDRATION_CONCURRENCY = 3
COMPANY_DOCUMENT_TRIAGE_MAX_HYDRATIONS = 3
COMPANY_DOCUMENT_TRIAGE_EVALUATOR_TIMEOUT_SECONDS = 4
COMPANY_DOCUMENT_TRIAGE_HYDRATION_TIMEOUT_SECONDS = 15
COMPANY_DOCUMENT_TRIAGE_TOTAL_CHAR_BUDGET = 50_000

SEARCH_MODE_BUDGET_SECONDS: dict[str, float] = {
    "flash": 120.0,
    "quick": 120.0,
    "balanced": 180.0,
    "heavy": 360.0,
    "deep": 360.0,
    "forensic": 600.0,
}

SEARCH_AI_STAGE_TIMEOUT_SECONDS: dict[str, dict[str, float]] = {
    "planner": {"flash": 60.0, "balanced": 60.0, "heavy": 90.0, "forensic": 120.0},
    "navigation": {"flash": 60.0, "balanced": 60.0, "heavy": 90.0, "forensic": 120.0},
    "branch_controller": {"flash": 60.0, "balanced": 60.0, "heavy": 90.0, "forensic": 120.0},
    "master_judge": {"flash": 60.0, "balanced": 45.0, "heavy": 60.0, "forensic": 90.0},
    "grounded_answer": {"flash": 30.0, "balanced": 60.0, "heavy": 90.0, "forensic": 120.0},
    "final_answer_approval": {"flash": 25.0, "balanced": 45.0, "heavy": 60.0, "forensic": 90.0},
    "branch_autojudge": {"flash": 20.0, "balanced": 35.0, "heavy": 45.0, "forensic": 60.0},
    "continuity_gate": {"flash": 15.0, "balanced": 30.0, "heavy": 45.0, "forensic": 60.0},
}

_TERMINAL_SEARCH_STATES = {"blocked", "cancelled", "completed", "failed", "superseded"}
_PUBLIC_TERMINAL_SESSION_STATES = {
    "blocked",
    "cancelled",
    "completed",
    "failed",
    "review_required",
    "superseded",
}
_PUBLIC_ACTIVE_SESSION_STATES = {"created", "planning", "running"}
_SEARCH_STATE_ORDER = {
    "created": 0,
    "planning": 1,
    "running": 2,
    "review_required": 3,
    "finalizing": 4,
    "completed": 5,
    "blocked": 5,
    "cancelled": 5,
    "failed": 5,
    "superseded": 5,
}
_SEARCH_SEAL_CONTRACT_KEYS = (
    "completion_contract",
    "mcp_delivery_contract",
    "sufficiency_judge",
    "master_sufficiency",
)
_SEARCH_SEAL_BINDING_KEY_GROUPS = (
    ("package_revision", ("package_revision",)),
    ("brain_id", ("brain_id",)),
    ("brain_revision", ("brain_revision",)),
    ("mission_ledger_digest", ("mission_ledger_digest",)),
)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def search_mode_budget_seconds(mode: str | None) -> float:
    requested_mode = str(mode or "balanced").strip().lower()
    normalized_mode = (
        "flash"
        if requested_mode == "quick"
        else "heavy"
        if requested_mode == "deep"
        else requested_mode
    )
    default = float(SEARCH_MODE_BUDGET_SECONDS.get(normalized_mode, 180.0))
    if normalized_mode != "flash":
        return default
    # Quick owns one end-to-end deadline covering admission, spatial planning,
    # traversal and the terminal Master. Operators may lower that budget, but
    # the product ceiling remains 120 seconds and client/inherited deadlines
    # remain earlier authorities in `_effective_search_deadline_monotonic`.
    env_names = [f"AGVM_SEARCH_MODE_{requested_mode.upper()}_BUDGET_SECONDS"]
    if requested_mode != normalized_mode:
        env_names.append("AGVM_SEARCH_MODE_FLASH_BUDGET_SECONDS")
    for env_name in env_names:
        raw = str(os.getenv(env_name) or "").strip()
        if not raw:
            continue
        try:
            return max(1.0, min(120.0, float(raw)))
        except ValueError:
            continue
    return default


def search_ai_stage_timeout_seconds(stage: str, mode: str | None) -> float:
    """Return the single configurable timeout authority for Search AI stages."""

    normalized_stage = re.sub(r"[^a-z0-9]+", "_", str(stage or "").strip().lower()).strip("_") or "planner"
    normalized_mode = str(mode or "balanced").strip().lower()
    if normalized_mode in {"quick"}:
        normalized_mode = "flash"
    elif normalized_mode in {"deep"}:
        normalized_mode = "heavy"
    if normalized_mode not in {"flash", "balanced", "heavy", "forensic"}:
        normalized_mode = "balanced"
    defaults = SEARCH_AI_STAGE_TIMEOUT_SECONDS.get(normalized_stage) or SEARCH_AI_STAGE_TIMEOUT_SECONDS["planner"]
    default = float(defaults[normalized_mode])
    for env_name in (
        f"AGVM_SEARCH_AI_{normalized_stage.upper()}_{normalized_mode.upper()}_TIMEOUT_SECONDS",
        f"AGVM_SEARCH_AI_{normalized_stage.upper()}_TIMEOUT_SECONDS",
    ):
        raw = str(os.getenv(env_name) or "").strip()
        if not raw:
            continue
        try:
            return max(1.0, min(600.0, float(raw)))
        except ValueError:
            continue
    return default


def advance_canonical_search_state(previous: str | None, proposed: str | None) -> str:
    """Advance lifecycle state without allowing a terminal checkpoint to regress."""
    current = str(previous or "created").strip().lower()
    next_state = str(proposed or current).strip().lower()
    if current in _TERMINAL_SEARCH_STATES:
        return current
    if next_state not in _SEARCH_STATE_ORDER:
        return current
    return next_state if _SEARCH_STATE_ORDER[next_state] >= _SEARCH_STATE_ORDER.get(current, 0) else current


def project_search_result_lifecycle(
    result: dict[str, Any] | None,
    session_status: str | None,
) -> dict[str, Any]:
    """Project one server-authoritative lifecycle across every public surface."""

    projected = dict(result or {})
    status = str(session_status or "").strip().lower()
    if status not in _PUBLIC_ACTIVE_SESSION_STATES | _PUBLIC_TERMINAL_SESSION_STATES:
        return projected
    terminal_projection_promoted = False
    if status in {"blocked", "review_required"} and _attested_ai_v2_terminal_projection(projected):
        status = "completed"
        terminal_projection_promoted = True

    terminal = status in _PUBLIC_TERMINAL_SESSION_STATES
    pending = not terminal
    projected["status"] = status
    projected["final_materialization_pending"] = pending
    projected["result_ready_terminal"] = terminal
    projected["terminal_for_client"] = terminal
    if pending:
        projected["closure_state"] = "open"
        projected["final_closure_ready"] = False
        if str(projected.get("result_materialization_state") or "").strip().lower() in {
            "finalized",
            "bounded_partial_finalized",
            "partial_review_required",
        }:
            projected["result_materialization_state"] = "first_package_ready_background_running"

    current_canonical = str(projected.get("canonical_search_state") or "").strip().lower()
    if not terminal:
        canonical = "running" if status in {"planning", "running"} else "created"
    elif terminal_projection_promoted:
        canonical = "completed"
    elif status == "completed" and current_canonical in _TERMINAL_SEARCH_STATES | {"review_required"}:
        canonical = current_canonical
    else:
        canonical = status
    projected["canonical_search_state"] = canonical

    completion = _mapping(projected.get("completion_contract"))
    completion.update(
        {
            "state": (
                "background_running"
                if pending
                else "finalized"
                if status == "completed"
                else status
            ),
            "canonical_search_state": canonical,
            "final_materialization_pending": pending,
            "result_ready_terminal": terminal,
        }
    )
    projected["completion_contract"] = completion

    delivery = _mapping(projected.get("mcp_delivery_contract"))
    delivery.update(
        {
            "canonical_search_state": canonical,
            "terminal_for_client": terminal,
            "partial_for_client": pending,
            "final_materialization_pending": pending,
            "background_state": "running" if pending else "stopped",
        }
    )
    if terminal:
        if status == "completed":
            delivery["completion_state"] = "finalized"
            delivery["client_payload_state"] = "completed"
        elif status == "review_required":
            delivery["completion_state"] = "review_required"
            delivery["client_payload_state"] = "review_required"
            delivery["blocked"] = False
        elif status in {"blocked", "failed"}:
            delivery["client_payload_state"] = status
    else:
        delivery["client_payload_state"] = "running"
    projected["mcp_delivery_contract"] = delivery

    materialization = _mapping(projected.get("context_package_materialization"))
    materialization.update(
        {
            "terminal": terminal,
            "terminal_for_mcp_client": terminal,
            "final_materialization_pending": pending,
        }
    )
    projected["context_package_materialization"] = materialization
    return projected


def _search_result_current_surface(payload: dict[str, Any]) -> dict[str, Any]:
    result = _payload_result(payload)
    if not result:
        return payload
    current = dict(result)
    current.update({key: value for key, value in payload.items() if key != "result"})
    return current


def search_result_explicitly_unsealed(payload: dict[str, Any] | None) -> bool:
    """Return true when the authoritative current surface denies final closure."""
    current = _search_result_current_surface(dict(payload or {}))
    if current.get("final_closure_ready") is False or current.get("final_seal_allowed") is False:
        return True
    if current.get("final_materialization_pending") is True:
        return True
    if str(current.get("canonical_search_state") or "").strip().lower() in {
        "review_required", "finalizing", "incomplete", "insufficient", "partial", "timeout", "timed_out",
    }:
        return True
    if str(current.get("status") or "").strip().lower() in {
        "review_required", "finalizing", "incomplete", "insufficient", "partial", "timeout", "timed_out",
    }:
        return True
    if str(current.get("closure_state") or "").strip().lower() in {"open", "review_required", "incomplete", "blocked"}:
        return True
    if str(current.get("answer_surface_state") or "").strip().lower() in {"open", "partial", "insufficient", "review_required"}:
        return True
    blockers = current.get("final_closure_blockers")
    if isinstance(blockers, list) and blockers:
        return True
    for key in (
        "unresolved_destination_count",
        "unresolved_mission_count",
        "unresolved_required_mission_count",
        "pending_path_count",
    ):
        try:
            if int(current.get(key) or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _search_seal_binding_value(surface: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = str(surface.get(key) or "").strip()
        if value:
            return value
    return ""


def _search_seal_bindings(surface: dict[str, Any]) -> dict[str, str] | None:
    bindings = {
        name: _search_seal_binding_value(surface, keys)
        for name, keys in _SEARCH_SEAL_BINDING_KEY_GROUPS
    }
    return bindings if all(bindings.values()) else None


def search_result_has_current_seal_bindings(payload: dict[str, Any] | None) -> bool:
    """Return true only when every authoritative current seal binding is present."""
    current = _search_result_current_surface(dict(payload or {}))
    return _search_seal_bindings(current) is not None


def _search_seal_candidate_matches_current(current: dict[str, Any], candidate: dict[str, Any]) -> bool:
    current_bindings = _search_seal_bindings(current)
    candidate_bindings = _search_seal_bindings(candidate)
    return bool(current_bindings and candidate_bindings and candidate_bindings == current_bindings)


def search_result_has_final_seal(payload: dict[str, Any] | None) -> bool:
    """Return true only for a current, explicitly bound sufficiency/final seal."""
    normalized = dict(payload or {})
    current = _search_result_current_surface(normalized)
    if search_result_explicitly_unsealed(current):
        return False
    if not search_result_has_current_seal_bindings(current):
        return False
    candidates = [current]
    for key in _SEARCH_SEAL_CONTRACT_KEYS:
        nested = current.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)
    for candidate in candidates:
        if not _search_seal_candidate_matches_current(current, candidate):
            continue
        try:
            if int(candidate.get("unresolved_required_mission_count") or 0) > 0:
                return False
        except (TypeError, ValueError):
            return False
        unresolved = candidate.get("unresolved_required_missions")
        if isinstance(unresolved, list) and unresolved:
            return False
    for candidate in candidates:
        if not _search_seal_candidate_matches_current(current, candidate):
            continue
        if candidate.get("final_closure_ready") is True or candidate.get("final_seal_allowed") is True:
            return True
        if str(candidate.get("closure_state") or "").strip().lower() == "final_sealed":
            return True
        if str(candidate.get("answer_surface_state") or "").strip().lower() == "final_sealed":
            return True
        if str(candidate.get("sufficiency_status") or "").strip().lower() in {"sufficient", "satisfied"}:
            return True
        stop_reason = str(candidate.get("stop_reason") or "").strip().lower()
        if stop_reason.endswith("contract_satisfied") or stop_reason in {"final_closure_ready", "sufficiency_satisfied"}:
            return True
    return False


def _search_result_requires_review(payload: dict[str, Any]) -> bool:
    result = _payload_result(payload)
    values = [payload, result]
    review_states = {"review_required", "incomplete", "insufficient", "partial", "timeout", "timed_out"}
    for candidate in values:
        if str(candidate.get("canonical_search_state") or "").strip().lower() in review_states:
            return True
        if str(candidate.get("status") or "").strip().lower() in review_states:
            return True
        if str(candidate.get("answerability_state") or "").strip().lower() in {"review_required", "insufficient", "partial"}:
            return True
        if str(candidate.get("result_materialization_state") or "").strip().lower() in {
            "partial_review_required",
            "review_required",
        }:
            return True
        stop_reason = str(candidate.get("stop_reason") or "").strip().lower()
        if stop_reason.startswith("flash_public_partial"):
            return True
        if any(token in stop_reason for token in ("timeout", "deadline", "incomplete", "insufficient", "review_required")):
            return True
    return False


def _attested_ai_v2_terminal_projection(payload: dict[str, Any]) -> bool:
    """Recognize the one post-Master V2 terminal proof.

    Persisted snapshots can still contain a canonical state computed before the
    final Master/answer attestations arrived.  That stale projection must not
    outrank the current proof, while every safety and binding invariant remains
    mandatory.
    """

    current = _search_result_current_surface(payload)
    answer = _mapping(current.get("answer"))
    master = _mapping(current.get("master_judgement"))
    if not master:
        master = _mapping(_mapping(current.get("context_package")).get("master_judgement"))
    answer_authority = _mapping(answer.get("semantic_authority"))
    master_authority = _mapping(master.get("semantic_authority"))
    if str(answer_authority.get("mode") or "").strip().lower() != "ai_v2":
        return False
    if str(master_authority.get("mode") or "").strip().lower() != "ai_v2":
        return False
    if answer_authority.get("fallback_used") is not False or master_authority.get("fallback_used") is not False:
        return False
    if str(answer_authority.get("provider_answerability_state") or "").strip().lower() != "grounded":
        return False
    if not str(answer.get("answer_text") or "").strip():
        return False
    if not list(answer.get("evidence_node_ids") or []):
        return False
    if str(master.get("master_state") or "").strip().lower() != "terminal":
        return False
    if master.get("terminal_for_client") is not True or master.get("final_seal_allowed") is not True:
        return False
    if list(master.get("missing_goals") or []) or list(master.get("unresolved_goals") or []):
        return False
    if current.get("final_closure_ready") is not True or list(current.get("final_closure_blockers") or []):
        return False
    if not search_result_has_current_seal_bindings(current):
        return False
    integrity = current.get("payload_integrity")
    if isinstance(integrity, dict) and integrity.get("passed") is not True:
        return False
    for key in ("ai_validation_gate", "ai_materialization_hard_gate"):
        gate = current.get(key)
        if isinstance(gate, dict) and gate.get("blocked") is True:
            return False
    return True


def _attested_ai_v2_review_projection(payload: dict[str, Any]) -> bool:
    """Recognize a terminal, evidence-bearing V2 partial without sealing it.

    A bounded final Master may honestly require review after every planned path
    has run. That surface is not a provider failure: it is terminal for the
    client, remains unsealed, and preserves evidence for inspection. Legacy
    renderer fields such as ``answer_surface_state=not_ready`` must not invert
    this independently attested state back to ``blocked``.
    """

    current = _search_result_current_surface(payload)
    master = _mapping(current.get("master_judgement"))
    if not master:
        master = _mapping(_mapping(current.get("context_package")).get("master_judgement"))
    authority = _mapping(master.get("semantic_authority"))
    runtime = _mapping(_mapping(current.get("planner_runtime")).get("plan_first_runtime"))
    integrity = _mapping(current.get("payload_integrity"))
    if str(current.get("status") or "").strip().lower() != "review_required":
        return False
    if current.get("review_required") is not True or current.get("terminal_for_client") is not True:
        return False
    if str(master.get("master_state") or "").strip().lower() != "usable_partial":
        return False
    if master.get("master_ai_used") is not True or master.get("final_seal_allowed") is not False:
        return False
    if str(authority.get("mode") or "").strip().lower() != "ai_v2":
        return False
    if authority.get("master_ai_used") is not True or authority.get("fallback_used") is not False:
        return False
    if integrity.get("passed") is not True:
        return False
    if runtime.get("enabled") is not True or runtime.get("schema_version") != "agvm.search_plan_first.v3":
        return False
    if str(runtime.get("final_master_state") or "").strip().lower() != "attested":
        return False
    if int(runtime.get("final_master_attempt_count") or 0) != 1 or int(runtime.get("final_master_attested_count") or 0) != 1:
        return False
    if list(current.get("final_closure_blockers") or []):
        return False
    for key in ("ai_validation_gate", "ai_materialization_hard_gate"):
        gate = _mapping(current.get(key))
        if gate.get("blocked") is True:
            return False
    return True


def canonical_search_state(payload: dict[str, Any] | None, event_type: str | None = None) -> str:
    normalized = dict(payload or {})
    result = _payload_result(normalized)
    if _attested_ai_v2_terminal_projection(normalized):
        return "completed"
    kind = str(event_type or "").strip().lower()
    status = str(result.get("status") or normalized.get("status") or "").strip().lower()
    if normalized.get("superseded") is True or result.get("superseded") is True:
        return "superseded"
    if kind == "search_failed" or status == "failed":
        return "failed"
    completion_state = str(
        result.get("completion_state") or normalized.get("completion_state") or ""
    ).strip().lower()
    stop_reason = str(result.get("stop_reason") or normalized.get("stop_reason") or "").strip().lower()
    result_materialization_state = str(
        result.get("result_materialization_state") or normalized.get("result_materialization_state") or ""
    ).strip().lower()
    review_required = result.get("review_required") is True or normalized.get("review_required") is True
    terminal_for_client = result.get("terminal_for_client") is True or normalized.get("terminal_for_client") is True
    materialization_pending = (
        result.get("final_materialization_pending") is True
        or normalized.get("final_materialization_pending") is True
    )
    if (
        terminal_for_client
        and not materialization_pending
        and review_required
        and (
            completion_state == "review_required"
            or result_materialization_state == "partial_review_required"
        )
        and (
            stop_reason.startswith("review_required_")
            or stop_reason.startswith("flash_public_partial")
            or result_materialization_state == "partial_review_required"
        )
    ):
        return "review_required"
    explicit = str(normalized.get("canonical_search_state") or result.get("canonical_search_state") or "").strip().lower()
    if explicit in (_TERMINAL_SEARCH_STATES - {"completed"}) | {"created", "planning", "running", "review_required", "finalizing"}:
        return explicit
    if explicit in {"incomplete", "insufficient", "partial", "timeout", "timed_out"}:
        return "review_required"
    if kind == "result_snapshot_ready" and materialization_pending:
        return "blocked"
    if _attested_ai_v2_review_projection(normalized):
        return "review_required"
    if _contract_blocked(kind, normalized):
        return "blocked"
    final_sealed = search_result_has_final_seal(normalized)
    if final_sealed:
        return "completed"
    if explicit == "completed":
        return "review_required"
    if _search_result_requires_review(normalized):
        return "review_required"
    if kind in {
        "result_snapshot_ready",
        "final_materialization_started",
        "final_materialization_heartbeat",
        "final_materialization_completed",
    }:
        return "finalizing"
    if kind == "result_ready" or status == "completed":
        return "review_required"
    if kind in {"planning_started", "planning_complete", "semantic_contract_ready"}:
        return "planning"
    return "running"


def package_revision_for_result(search_id: str, result: dict[str, Any] | None) -> str:
    def stable(value: Any) -> Any:
        if isinstance(value, list):
            return [stable(item) for item in value]
        if isinstance(value, dict):
            return {
                key: stable(item)
                for key, item in value.items()
                if key not in {"package_revision", "created_at", "updated_at", "runtime_stage_timing"}
            }
        return value

    payload = stable(dict(result or {}))
    canonical = json.dumps(
        {"schema_version": SEARCH_PACKAGE_REVISION_SCHEMA_VERSION, "search_id": str(search_id), "result": payload},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def search_result_ref(search_id: str, result: dict[str, Any] | None = None) -> dict[str, Any]:
    brain_id = str(dict(result or {}).get("brain_id") or "").strip()
    endpoint = f"/memory/query-result/{search_id}"
    if brain_id:
        endpoint = f"{endpoint}?{urlencode({'brain_id': brain_id})}"
    reference: dict[str, Any] = {"search_id": str(search_id), "endpoint": endpoint}
    if brain_id:
        reference["brain_id"] = brain_id
    if result:
        reference["package_revision"] = str(result.get("package_revision") or package_revision_for_result(search_id, result))
    return reference


def search_snapshot_counters(result: dict[str, Any] | None) -> dict[str, int]:
    normalized = dict(result or {})
    explicit = _mapping(normalized.get("snapshot_counters"))

    def integer(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    def ids_for(*keys: str) -> set[str]:
        found: set[str] = set()

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in keys and isinstance(item, list):
                        found.update(str(candidate).strip() for candidate in item if str(candidate).strip())
                    else:
                        visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)

        visit(normalized)
        return found

    def maximum_for(*keys: str) -> int:
        maximum = 0

        def visit(value: Any) -> None:
            nonlocal maximum
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in keys:
                        maximum = max(maximum, integer(item))
                    else:
                        visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)

        visit(normalized)
        return maximum

    visited_ids = ids_for("visited_node_ids", "studied_node_ids")
    promoted_ids = ids_for("promoted_node_ids", "yielded_match_ids", "evidence_node_ids")
    hydrated_ids = ids_for("hydrated_node_ids")
    context_package = _mapping(normalized.get("context_package"))
    package_ids: set[str] = set()
    for key in ("matches", "evidence_snippets", "document_refs", "document_packets"):
        for item in list(normalized.get(key) or []):
            if isinstance(item, dict):
                marker = next(
                    (
                        str(item.get(candidate) or "").strip()
                        for candidate in ("id", "node_id", "document_id", "source_id", "digest")
                        if str(item.get(candidate) or "").strip()
                    ),
                    "",
                )
                package_ids.add(marker or _stable_digest(item))
    for item in list(context_package.get("sections") or []):
        package_ids.add(_stable_digest(item))
    visited_current = integer(explicit.get("visited_current")) if "visited_current" in explicit else len(visited_ids)
    visited_total = max(integer(explicit.get("visited_total")), visited_current, len(visited_ids))
    promoted_current = (
        integer(explicit.get("promoted_current"))
        if "promoted_current" in explicit
        else max(maximum_for("promoted_count", "hot_promoted_count"), len(promoted_ids))
    )
    promoted_total = max(
        integer(explicit.get("promoted_total")),
        integer(explicit.get("promoted")),
        promoted_current,
        len(promoted_ids),
    )
    # Hydration is intentionally ID-backed. Candidate/match counts are not
    # hydration and must not inflate this progress surface.
    hydrated_current = (
        integer(explicit.get("hydrated_current"))
        if "hydrated_current" in explicit
        else len(hydrated_ids)
    )
    hydrated_total = max(
        integer(explicit.get("hydrated_total")),
        integer(explicit.get("hydrated")),
        hydrated_current,
        len(hydrated_ids),
    )
    package_current = (
        integer(explicit.get("package_current"))
        if "package_current" in explicit
        else max(
            maximum_for("package_count", "hot_item_count", "cold_item_count", "document_ref_count"),
            len(package_ids),
        )
    )
    package_total = max(
        integer(explicit.get("package_total")),
        integer(explicit.get("package")),
        package_current,
        len(package_ids),
    )
    return {
        "visited_current": visited_current,
        "visited_total": visited_total,
        "hydrated_current": hydrated_current,
        "hydrated_total": hydrated_total,
        "promoted_current": promoted_current,
        "promoted_total": promoted_total,
        "package_current": package_current,
        "package_total": package_total,
        # Deprecated read aliases. New writes and event consumers use the
        # canonical current/total pairs above.
        "hydrated": hydrated_total,
        "promoted": promoted_total,
        "package": package_total,
    }


def canonicalize_search_snapshot(
    search_id: str,
    result: dict[str, Any],
    *,
    snapshot_kind: str,
    brain_id: str | None = None,
    parent_package_revision: str | None = None,
) -> dict[str, Any]:
    normalized = dict(result or {})
    if "context_package" in normalized:
        normalized["context_package"] = _mapping(normalized.get("context_package"))
    normalized.pop("canonical_search_state", None)
    for contract_key in ("completion_contract", "mcp_delivery_contract"):
        contract = normalized.get(contract_key)
        if isinstance(contract, dict):
            normalized[contract_key] = {
                key: value
                for key, value in contract.items()
                if key != "canonical_search_state"
            }
    resolved_brain_id = str(brain_id or normalized.get("brain_id") or "").strip()
    if resolved_brain_id:
        normalized["brain_id"] = resolved_brain_id
    normalized["snapshot_schema_version"] = SEARCH_RESULT_SNAPSHOT_SCHEMA_VERSION
    normalized["snapshot_kind"] = str(snapshot_kind)
    normalized["parent_package_revision"] = str(parent_package_revision or "") or None
    counters = search_snapshot_counters(normalized)
    normalized["snapshot_counters"] = counters
    normalized.update(counters)
    return canonicalize_search_result(search_id, normalized, event_type="result_ready" if snapshot_kind == "final" else "result_snapshot_ready")


def search_snapshot_is_useful(result: dict[str, Any] | None) -> bool:
    normalized = dict(result or {})
    if any(isinstance(normalized.get(key), list) and bool(normalized.get(key)) for key in ("matches", "evidence_snippets", "document_refs", "document_packets", "source_trace")):
        return True
    context_package = normalized.get("context_package")
    if not isinstance(context_package, dict):
        return False
    return bool(
        str(context_package.get("agent_markdown") or "").strip()
        or list(context_package.get("sections") or [])
        or list(context_package.get("document_refs") or [])
    )


def _stable_digest(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _stable_short_digest(value: Any, *, length: int = 16) -> str:
    return _stable_digest(value).split(":", 1)[-1][:length]


def search_mission_ledger_digest(ledger: dict[str, Any] | None) -> str:
    """Return the canonical digest used to bind a final Search package."""
    normalized = dict(ledger or {})
    return _stable_digest(normalized) if normalized else ""


def _event_sequence(event: dict[str, Any]) -> int:
    try:
        return max(0, int(event.get("seq") or event.get("sequence") or 0))
    except (TypeError, ValueError):
        return 0


def _event_result_surface(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("result", "result_summary", "result_snapshot", "result_snapshot_summary"):
        value = payload.get(key)
        if isinstance(value, dict):
            return dict(value)
    return {}


def _event_search_id(payload: dict[str, Any]) -> str:
    result = _event_result_surface(payload)
    return str(payload.get("search_id") or result.get("search_id") or "").strip()


def _event_run_id(payload: dict[str, Any], search_id: str) -> str:
    return (
        str(
            payload.get("run_id")
            or payload.get("turn_id")
            or payload.get("thread_id")
            or _event_result_surface(payload).get("thread_id")
            or ""
        ).strip()
        or f"search:{search_id}"
    )


def _event_brain_id(payload: dict[str, Any]) -> str:
    result = _event_result_surface(payload)
    return str(payload.get("brain_id") or result.get("brain_id") or "").strip()


def _event_brain_revision(payload: dict[str, Any]) -> str:
    result = _event_result_surface(payload)
    ledger = _event_mission_ledger(payload)
    return str(
        payload.get("brain_revision")
        or result.get("brain_revision")
        or ledger.get("brain_revision")
        or ""
    ).strip()


def _event_mission_ledger(payload: dict[str, Any]) -> dict[str, Any]:
    result = _event_result_surface(payload)
    context_package = _mapping(payload.get("context_package")) or _mapping(result.get("context_package"))
    shared_evidence = _mapping(payload.get("shared_evidence")) or _mapping(result.get("shared_evidence"))
    for candidate in (
        payload.get("mission_evidence_ledger"),
        result.get("mission_evidence_ledger"),
        context_package.get("mission_evidence_ledger"),
        shared_evidence.get("mission_evidence_ledger"),
    ):
        if isinstance(candidate, dict):
            return dict(candidate)
    return {}


def _event_package_revision(payload: dict[str, Any]) -> str:
    result = _event_result_surface(payload)
    return str(
        payload.get("package_revision")
        or result.get("package_revision")
        or payload.get("snapshot_revision")
        or ""
    ).strip()


def _event_ledger_digest(payload: dict[str, Any]) -> str:
    result = _event_result_surface(payload)
    explicit = str(
        payload.get("ledger_digest")
        or payload.get("mission_ledger_digest")
        or result.get("mission_ledger_digest")
        or ""
    ).strip()
    if explicit:
        return explicit
    return search_mission_ledger_digest(_event_mission_ledger(payload))


def _company_v0_event_id(
    *,
    search_id: str,
    sequence: int,
    event_type: str,
    source_event_type: str,
    suffix: str = "",
) -> str:
    material = {
        "schema_version": COMPANY_RUN_EVENT_SCHEMA_VERSION,
        "search_id": search_id,
        "sequence": sequence,
        "type": event_type,
        "source_event_type": source_event_type,
        "suffix": suffix,
    }
    return _stable_digest(material)


def _company_v0_base_event(
    source_event: dict[str, Any],
    payload: dict[str, Any],
    *,
    event_type: str,
    delta: dict[str, Any],
    terminal: bool = False,
    mission_id: str | None = None,
    suffix: str = "",
) -> dict[str, Any]:
    sequence = _event_sequence(source_event)
    source_event_type = str(source_event.get("event_type") or "").strip() or "unknown"
    search_id = _event_search_id(payload)
    brain_id = _event_brain_id(payload)
    envelope = {
        "schema_version": COMPANY_RUN_EVENT_SCHEMA_VERSION,
        "adapter_version": COMPANY_RUN_EVENT_ADAPTER_VERSION,
        "event_id": _company_v0_event_id(
            search_id=search_id,
            sequence=sequence,
            event_type=event_type,
            source_event_type=source_event_type,
            suffix=suffix,
        ),
        "sequence": sequence,
        "run_id": _event_run_id(payload, search_id),
        "search_id": search_id,
        "brain_id": brain_id,
        "type": event_type,
        "source_event_type": source_event_type,
        "terminal": bool(terminal),
        "delta": dict(delta or {}),
    }
    brain_revision = _event_brain_revision(payload)
    if brain_revision:
        envelope["brain_revision"] = brain_revision
    if mission_id:
        envelope["mission_id"] = mission_id
    snapshot_revision = str(payload.get("snapshot_revision") or "").strip()
    if snapshot_revision:
        envelope["snapshot_revision"] = snapshot_revision
    package_revision = _event_package_revision(payload)
    if package_revision:
        envelope["package_revision"] = package_revision
    ledger_digest = _event_ledger_digest(payload)
    if ledger_digest:
        envelope["ledger_digest"] = ledger_digest
    return envelope


def _row_mission_id(row: dict[str, Any]) -> str:
    return str(
        row.get("mission_id")
        or row.get("path_mission_id")
        or row.get("path_id")
        or row.get("semantic_mission_id")
        or ""
    ).strip()


def _row_path_id(row: dict[str, Any]) -> str:
    return str(row.get("path_id") or row.get("branch_id") or row.get("worker_id") or "").strip()


def _compact_evidence_delta(item: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    evidence_id = str(
        item.get("node_id")
        or item.get("evidence_id")
        or item.get("id")
        or item.get("document_id")
        or ""
    ).strip()
    return {
        "evidence_id": evidence_id,
        "node_id": str(item.get("node_id") or evidence_id).strip() or None,
        "title": str(item.get("title") or item.get("summary") or "").strip()[:240] or None,
        "text": str(item.get("text") or item.get("evidence") or item.get("evidence_snippet") or "").strip()[:700] or None,
        "trust": item.get("trust") or item.get("source_trust") or item.get("provenance_trust"),
        "provenance": _mapping(item.get("provenance")) or _mapping(row.get("provenance")),
        "promotion_state": "promoted",
        "provisional": True,
    }


def _document_key(ref: dict[str, Any]) -> str:
    return str(
        ref.get("canonical_ref")
        or ref.get("document_id")
        or ref.get("document_ref_id")
        or ref.get("document_anchor_id")
        or ref.get("anchor_node_id")
        or ref.get("node_id")
        or ref.get("source_unit_id")
        or ref.get("source_id")
        or ""
    ).strip()


def _document_hash_ref(ref: dict[str, Any]) -> str | None:
    provenance = _mapping(ref.get("provenance"))
    for key in (
        "content_hash",
        "canonical_text_sha256",
        "source_sha256",
        "digest",
        "sha256",
    ):
        value = _text_or_none(ref.get(key))
        if value:
            return value if value.startswith("sha256:") else f"sha256:{value}"
    for key in ("content_hash", "canonical_text_sha256", "source_hash", "sha256"):
        value = _text_or_none(provenance.get(key))
        if value:
            return value if value.startswith("sha256:") else f"sha256:{value}"
    raw_text = _text_or_none(ref.get("raw_text") or ref.get("text"))
    return _stable_digest(raw_text) if raw_text else None


def _document_anchor_ref(ref: dict[str, Any]) -> str | None:
    return _text_or_none(ref.get("document_anchor_id") or ref.get("anchor_node_id") or ref.get("node_id"))


def _document_ref_id(ref: dict[str, Any], document_id: str | None) -> str:
    explicit = _text_or_none(ref.get("document_ref_id") or ref.get("ref_id"))
    if explicit:
        return explicit
    if document_id:
        return f"document-ref:{document_id}"
    return f"document-ref:{_stable_short_digest(ref)}"


def _document_chunk_ids(ref: dict[str, Any]) -> list[str]:
    for key in ("chunk_ids", "document_chunk_ids", "child_preview_node_ids", "child_source_unit_ids"):
        values = [
            str(value).strip()
            for value in _as_list(ref.get(key))
            if str(value or "").strip()
        ]
        if values:
            return values[:24]
    return []


def _document_section_refs(ref: dict[str, Any]) -> list[dict[str, Any]]:
    section_refs: list[dict[str, Any]] = []
    raw_sections = _as_list(ref.get("section_refs")) or _as_list(ref.get("sections")) or _as_list(ref.get("spans"))
    for index, raw_section in enumerate(raw_sections[:24], start=1):
        if not isinstance(raw_section, dict):
            continue
        section_id = _text_or_none(
            raw_section.get("section_id")
            or raw_section.get("span_id")
            or raw_section.get("source_unit_id")
            or raw_section.get("unit_id")
            or f"section-{index}"
        )
        section_ref = {
            "section_id": section_id,
            "span_id": _text_or_none(raw_section.get("span_id")),
            "source_unit_id": _text_or_none(raw_section.get("source_unit_id") or raw_section.get("unit_id")),
            "title": _text_or_none(raw_section.get("title")),
            "page": raw_section.get("page") if raw_section.get("page") is not None else raw_section.get("page_number"),
            "char_start": raw_section.get("char_start"),
            "char_end": raw_section.get("char_end"),
            "content_hash": _document_hash_ref(raw_section),
        }
        section_refs.append({key: value for key, value in section_ref.items() if value not in (None, "", [], {})})
    return section_refs


def _document_triage_contract() -> dict[str, Any]:
    return {
        "schema_version": COMPANY_DOCUMENT_TRIAGE_SCHEMA_VERSION,
        "max_candidates": COMPANY_DOCUMENT_TRIAGE_MAX_CANDIDATES,
        "evaluator_concurrency": COMPANY_DOCUMENT_TRIAGE_EVALUATOR_CONCURRENCY,
        "hydration_concurrency": COMPANY_DOCUMENT_TRIAGE_HYDRATION_CONCURRENCY,
        "max_hydrated_documents": COMPANY_DOCUMENT_TRIAGE_MAX_HYDRATIONS,
        "evaluator_timeout_seconds": COMPANY_DOCUMENT_TRIAGE_EVALUATOR_TIMEOUT_SECONDS,
        "hydration_timeout_seconds": COMPANY_DOCUMENT_TRIAGE_HYDRATION_TIMEOUT_SECONDS,
        "total_char_budget": COMPANY_DOCUMENT_TRIAGE_TOTAL_CHAR_BUDGET,
        "dedupe_keys": ["canonical_ref", "content_hash"],
        "cancel_policy": {
            "cancel_evaluators": True,
            "cancel_hydration": True,
            "ignore_late_results": True,
        },
        "section_scoped_hydration": True,
        "refs_only_sse": True,
    }


def _compact_document_delta(ref: dict[str, Any]) -> dict[str, Any]:
    document_id = _document_key(ref)
    return {
        "document_id": document_id,
        "anchor_node_id": str(ref.get("anchor_node_id") or ref.get("node_id") or "").strip() or None,
        "title": str(ref.get("title") or ref.get("source_label") or "").strip()[:240] or None,
        "source_label": str(ref.get("source_label") or "").strip()[:240] or None,
        "source_type": str(ref.get("source_type") or "").strip() or None,
        "raw_text_available": bool(ref.get("raw_text_available") or ref.get("complete_text_available")),
        "hydration_recipe": {
            "tool": "retrieve_document",
            "document_id": document_id,
        },
        "provisional": True,
    }


def _document_refs_from_payload(payload: dict[str, Any], result: dict[str, Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    containers = [
        payload,
        result,
        _mapping(payload.get("context_package")),
        _mapping(result.get("context_package")),
        _mapping(payload.get("document_workspace")),
        _mapping(result.get("document_workspace")),
    ]
    for container in containers:
        for key in ("document_refs", "document_packets", "supporting_documents", "source_trace"):
            for ref in list(container.get(key) or []):
                if isinstance(ref, dict):
                    refs.append(dict(ref))
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for ref in refs:
        key = _document_key(ref) or _stable_digest(ref)
        if key in seen:
            continue
        seen.add(key)
        unique.append(ref)
    return unique


def _evidence_ids_from_value(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"evidence_node_ids", "node_ids", "support_node_ids"} and isinstance(item, list):
                found.update(str(candidate).strip() for candidate in item if str(candidate).strip())
            elif key in {"node_id", "evidence_id"}:
                text = str(item or "").strip()
                if text:
                    found.add(text)
            else:
                found.update(_evidence_ids_from_value(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_evidence_ids_from_value(item))
    return found


def _company_v0_citation_coverage(payload: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    ledger_rows = list(_event_mission_ledger(payload).get("rows") or [])
    evidence_ids = _evidence_ids_from_value(
        {
            "payload": payload,
            "result": result,
            "master_judgement": result.get("master_judgement"),
            "answer": result.get("answer"),
        }
    )
    document_refs = _document_refs_from_payload(payload, result)
    canonical_state = canonical_search_state({"result": result} if result else payload, "result_ready")
    final_sealed = canonical_state == "completed" and search_result_has_final_seal(result or payload)
    factual_claim_count = max(len(ledger_rows), 1 if str(result.get("answer") or result.get("answer_full") or "").strip() else 0)
    cited_count = len(evidence_ids)
    missing = 0 if final_sealed and cited_count else factual_claim_count
    return {
        "schema_version": "naffco.ai_director.citation_coverage.v1",
        "status": "complete" if final_sealed and missing == 0 else "review_required",
        "evidence_refs": cited_count,
        "document_ref_count": len(document_refs),
        "document_refs": len(document_refs),
        "result_refs": 1 if result else 0,
        "uncovered_claims": missing,
    }


def company_final_receipt_v0(source_event: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    result = _event_result_surface(payload)
    search_id = _event_search_id(payload)
    result_ref = _mapping(payload.get("result_ref")) or _mapping(result.get("result_ref"))
    receipt = {
        "schema_version": COMPANY_FINAL_RECEIPT_SCHEMA_VERSION,
        "search_id": search_id,
        "brain_id": _event_brain_id(payload),
        "brain_revision": _event_brain_revision(payload),
        "result_id": str(
            result_ref.get("result_id")
            or result_ref.get("endpoint")
            or result.get("result_id")
            or result.get("package_revision")
            or _event_package_revision(payload)
            or search_id
        ).strip(),
        "result_ready_at": str(source_event.get("created_at") or payload.get("result_ready_at") or "").strip(),
        "final_materialization_pending": False,
        "ledger_digest": _event_ledger_digest(payload),
        "last_sequence": _event_sequence(source_event),
        "citation_coverage": _company_v0_citation_coverage(payload, result),
    }
    receipt["receipt_sha256"] = _stable_digest(receipt)
    return receipt


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _text_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _int_or_zero(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _compact_public_failure_value(value: Any, *, limit: int = 480) -> Any:
    if value is None:
        return None
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        public_keys = (
            "stage",
            "phase",
            "provider",
            "provider_id",
            "model",
            "code",
            "type",
            "status",
            "reason",
            "message",
            "error",
            "stop_reason",
            "timeout_seconds",
            "elapsed_ms",
            "retryable",
        )
        compact = {
            key: projected
            for key in public_keys
            if key in value
            for projected in [_compact_public_failure_value(value.get(key), limit=limit)]
            if projected not in (None, "", [], {})
        }
        return compact or None
    if isinstance(value, list):
        compact_items = [
            projected
            for item in value[:4]
            for projected in [_compact_public_failure_value(item, limit=limit)]
            if projected not in (None, "", [], {})
        ]
        return compact_items or None
    text = str(value).strip()
    return text[:limit] if text else None


def _company_v0_failure_stage(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    raw_call = _payload_or_result_text(payload, "failed_ai_call")
    if not raw_call:
        provider_error = _payload_or_result_value(payload, "provider_error")
        if isinstance(provider_error, dict):
            raw_call = str(provider_error.get("failed_ai_call") or provider_error.get("stage") or "").strip()
    if not raw_call:
        return None, None
    public_stage = raw_call.split(":", 1)[0].strip() or raw_call.strip()
    return public_stage[:160], _stable_digest({"failed_ai_call": raw_call})


def _company_v0_runtime_stage(event_type: str, payload: dict[str, Any]) -> str | None:
    source = _payload_or_result_text(payload, "source")
    if source:
        return source
    if event_type in {"worker_started", "landing_ready", "ai_spatial_materialization_started"}:
        return event_type
    return None


def _company_v0_failure_delta(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    stage, call_digest = _company_v0_failure_stage(payload)
    provider_error = _compact_public_failure_value(_payload_or_result_value(payload, "provider_error"))
    delta = {
        "status": "blocked" if event_type == "search_blocked" else "failed",
        "stop_reason": _text_or_none(_payload_or_result_text(payload, "stop_reason")),
        "failure_stage": stage,
        "failure_call_digest": call_digest,
        "provider_error": provider_error,
        "retryable": _payload_or_result_value(payload, "retryable"),
        "provisional": False,
    }
    return {key: value for key, value in delta.items() if value not in (None, "", [], {})}


def _company_v0_status_delta(
    *,
    event_type: str,
    payload: dict[str, Any],
    terminality: dict[str, Any],
    source_type: str,
) -> dict[str, Any]:
    delta: dict[str, Any] = {
        "canonical_search_state": terminality.get("canonical_search_state"),
        "final_materialization_pending": terminality.get("final_materialization_pending"),
        "sealed_result_present": terminality.get("sealed_result_present"),
        "source_event_type": event_type,
    }
    stage = _company_v0_runtime_stage(event_type, payload)
    if stage:
        delta["stage"] = stage
    runtime_phase = _payload_or_result_text(payload, "runtime_phase")
    if runtime_phase:
        delta["runtime_phase"] = runtime_phase
    retrieval_mode = _payload_or_result_text(payload, "retrieval_mode")
    if retrieval_mode:
        delta["retrieval_mode"] = retrieval_mode
    if source_type == "result.failed":
        delta.update(_company_v0_failure_delta(event_type, payload))
    return delta


def _stream_search_id(payload: dict[str, Any]) -> str:
    return _payload_or_result_text(payload, "search_id")


def _stream_brain_id(payload: dict[str, Any]) -> str:
    return _payload_or_result_text(payload, "brain_id")


def _stream_context_package(payload: dict[str, Any]) -> dict[str, Any]:
    package = payload.get("context_package")
    if isinstance(package, dict):
        return package
    result_package = _payload_result(payload).get("context_package")
    return dict(result_package) if isinstance(result_package, dict) else {}


def _stream_mission_ledger(payload: dict[str, Any]) -> dict[str, Any]:
    ledger = payload.get("mission_evidence_ledger")
    if isinstance(ledger, dict):
        return ledger
    result = _payload_result(payload)
    ledger = result.get("mission_evidence_ledger")
    if isinstance(ledger, dict):
        return ledger
    package = _stream_context_package(payload)
    ledger = package.get("mission_evidence_ledger")
    return dict(ledger) if isinstance(ledger, dict) else {}


def _stream_semantic_contract(payload: dict[str, Any]) -> dict[str, Any]:
    contract = payload.get("semantic_contract")
    if isinstance(contract, dict):
        return contract
    result_contract = _payload_result(payload).get("semantic_contract")
    return dict(result_contract) if isinstance(result_contract, dict) else {}


def _stream_search_map_2d_truth(payload: dict[str, Any]) -> dict[str, Any]:
    truth = payload.get("search_map_2d_truth")
    if isinstance(truth, dict):
        return truth
    result_truth = _payload_result(payload).get("search_map_2d_truth")
    return dict(result_truth) if isinstance(result_truth, dict) else {}


def _stream_path_corridors(payload: dict[str, Any]) -> dict[str, Any]:
    corridors = payload.get("path_corridors")
    if isinstance(corridors, dict):
        return corridors
    result = _payload_result(payload)
    corridors = result.get("path_corridors")
    if isinstance(corridors, dict):
        return corridors
    package = _stream_context_package(payload)
    corridors = package.get("path_corridors")
    return dict(corridors) if isinstance(corridors, dict) else {}


def _mission_stream_id(row: dict[str, Any]) -> str:
    semantic_identity = {
        "mission_id": _text_or_none(row.get("mission_id") or row.get("path_mission_id")),
        "strand_id": _text_or_none(row.get("strand_id") or row.get("path_id") or row.get("goal")),
        "objective": _text_or_none(row.get("objective") or row.get("goal") or row.get("answer_hypothesis")),
        "expected_evidence_shape": row.get("expected_evidence_shape") if isinstance(row.get("expected_evidence_shape"), dict) else {},
    }
    explicit = _text_or_none(semantic_identity["mission_id"])
    return explicit or f"mission:{_stable_short_digest(semantic_identity)}"


def _stream_mission_deltas(payload: dict[str, Any], canonical_state: str) -> list[dict[str, Any]]:
    rows = [row for row in _as_list(_stream_mission_ledger(payload).get("rows")) if isinstance(row, dict)]
    deltas: list[dict[str, Any]] = []
    for row in rows[:24]:
        mission_id = _mission_stream_id(row)
        deltas.append(
            {
                "mission_id": mission_id,
                "objective": _text_or_none(row.get("objective") or row.get("goal") or row.get("answer_hypothesis")),
                "status": _text_or_none(row.get("status") or row.get("coverage_state") or row.get("judgement_state") or canonical_state),
                "strand_id": _text_or_none(row.get("strand_id") or row.get("path_id")),
                "semantic_identity_digest": _stable_digest(
                    {
                        "mission_id": mission_id,
                        "strand_id": row.get("strand_id") or row.get("path_id"),
                        "objective": row.get("objective") or row.get("goal") or row.get("answer_hypothesis"),
                        "expected_evidence_shape": row.get("expected_evidence_shape") if isinstance(row.get("expected_evidence_shape"), dict) else {},
                    }
                ),
            }
        )
    if deltas:
        return deltas
    contract = payload.get("path_mission_contract")
    if not isinstance(contract, dict):
        contract = _payload_result(payload).get("path_mission_contract")
    semantic_contract = _stream_semantic_contract(payload)
    mission_plan = _mapping(semantic_contract.get("mission_plan_v2"))
    candidate_missions = (
        _as_list(_mapping(contract).get("path_missions"))
        or _as_list(mission_plan.get("missions"))
        or _as_list(semantic_contract.get("answer_strands"))
        or _as_list(payload.get("answer_strands"))
    )
    for mission in candidate_missions[:24]:
        if not isinstance(mission, dict):
            continue
        mission_id = _mission_stream_id(mission)
        deltas.append(
            {
                "mission_id": mission_id,
                "objective": _text_or_none(
                    mission.get("objective")
                    or mission.get("semantic_goal")
                    or mission.get("goal")
                    or mission.get("answer_hypothesis")
                ),
                "status": _text_or_none(mission.get("status") or "planned"),
                "strand_id": _text_or_none(mission.get("strand_id") or mission.get("path_id")),
                "semantic_identity_digest": _stable_digest(
                    {
                        "mission_id": mission_id,
                        "strand_id": mission.get("strand_id") or mission.get("path_id"),
                        "objective": mission.get("objective") or mission.get("goal") or mission.get("answer_hypothesis"),
                        "expected_evidence_shape": mission.get("expected_evidence_shape") if isinstance(mission.get("expected_evidence_shape"), dict) else {},
                    }
                ),
            }
        )
    return deltas


def _evidence_stable_id(item: dict[str, Any], mission_id: str | None = None) -> str:
    explicit = _text_or_none(
        item.get("stable_id")
        or item.get("evidence_id")
        or item.get("node_id")
        or item.get("source_id")
        or item.get("id")
        or item.get("digest")
    )
    if explicit:
        return explicit
    return f"evidence:{_stable_short_digest({'mission_id': mission_id, 'item': item})}"


def _evidence_projection(
    item: dict[str, Any],
    *,
    source_bucket: str,
    mission_id: str | None,
) -> dict[str, Any]:
    promotion_state = _text_or_none(item.get("promotion_state"))
    if not promotion_state:
        answer_facing_bucket = source_bucket in {"evidence_snippets", "shared_evidence", "answer_evidence_snippets"}
        promotion_state = (
            "promoted"
            if source_bucket in {"hot_evidence", "promoted_evidence"}
            or item.get("promoted") is True
            or answer_facing_bucket
            else "candidate"
        )
    return {
        "stable_id": _evidence_stable_id(item, mission_id),
        "node_id": _text_or_none(item.get("node_id")),
        "mission_id": mission_id,
        "promotion_state": promotion_state,
        "trust": item.get("trust") if isinstance(item.get("trust"), dict) else _mapping(item.get("provenance")).get("trust"),
        "provenance": {
            key: value
            for key, value in _mapping(item.get("provenance")).items()
            if key in {"source_id", "source_type", "publisher", "url", "observed_at", "retrieved_at", "source_hash", "sha256", "span_id"}
        },
        "source_bucket": source_bucket,
    }


def _stream_evidence_deltas(payload: dict[str, Any]) -> list[dict[str, Any]]:
    deltas: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(item: Any, source_bucket: str, mission_id: str | None = None) -> None:
        if not isinstance(item, dict):
            return
        projected = _evidence_projection(item, source_bucket=source_bucket, mission_id=mission_id)
        stable_id = str(projected["stable_id"])
        if stable_id in seen:
            return
        seen.add(stable_id)
        deltas.append(projected)

    for row in _as_list(_stream_mission_ledger(payload).get("rows")):
        if not isinstance(row, dict):
            continue
        mission_id = _mission_stream_id(row)
        for bucket in ("hot_evidence", "promoted_evidence", "cold_evidence", "candidate_evidence"):
            for item in _as_list(row.get(bucket)):
                add(item, bucket, mission_id)
        for node_id in _as_list(row.get("evidence_node_ids")):
            add({"node_id": node_id}, "evidence_node_ids", mission_id)

    result = _payload_result(payload)
    answer = _mapping(payload.get("answer"))
    result_answer = _mapping(result.get("answer"))
    for bucket in ("matches", "evidence_snippets", "shared_evidence"):
        for item in (
            _as_list(payload.get(bucket))
            + _as_list(result.get(bucket))
            + _as_list(answer.get(bucket))
            + _as_list(result_answer.get(bucket))
        ):
            add(item, bucket)
    return deltas[:64]


def _document_projection(ref: dict[str, Any], payload: dict[str, Any], mission_id: str | None = None) -> dict[str, Any]:
    search_id = _stream_search_id(payload)
    brain_id = _stream_brain_id(payload)
    document_id = _text_or_none(ref.get("document_id") or ref.get("id") or ref.get("source_id") or ref.get("document_anchor_id"))
    anchor_node_id = _document_anchor_ref(ref)
    document_ref_id = _document_ref_id(ref, document_id)
    content_hash = _document_hash_ref(ref)
    canonical_ref = _text_or_none(ref.get("canonical_ref") or anchor_node_id or document_id or document_ref_id)
    provenance = _mapping(ref.get("provenance"))
    canonical_url = _text_or_none(ref.get("canonical_url") or ref.get("source_uri") or ref.get("url") or provenance.get("url"))
    title = _text_or_none(ref.get("title") or ref.get("filename") or ref.get("source_label"))
    section_refs = _document_section_refs(ref)
    chunk_ids = _document_chunk_ids(ref)
    hydration_result_ref = _text_or_none(ref.get("hydration_result_ref") or ref.get("result_ref"))
    hydration_recipe = {
        "tool": "retrieve_document",
        "document_id": document_id,
        "document_ref_id": document_ref_id,
        "document_hint": title,
        "anchor_node_id": anchor_node_id,
        "document_anchor_id": anchor_node_id,
        "brain_id": brain_id or None,
        "search_id": search_id or None,
        "document_text_policy": "refs_only",
        "mode": "paginated",
        "page_size": COMPANY_DOCUMENT_TRIAGE_TOTAL_CHAR_BUDGET,
        "max_chars": COMPANY_DOCUMENT_TRIAGE_TOTAL_CHAR_BUDGET,
        "timeout_seconds": COMPANY_DOCUMENT_TRIAGE_HYDRATION_TIMEOUT_SECONDS,
        "section_scope_supported": True,
        "result_ref": hydration_result_ref,
    }
    hydration_recipe = {key: value for key, value in hydration_recipe.items() if value not in (None, "", [], {})}
    return {
        "document_id": document_id,
        "document_ref_id": document_ref_id,
        "canonical_ref": canonical_ref,
        "canonical_url": canonical_url,
        "content_hash": content_hash,
        "source_hash": _document_hash_ref({"source_sha256": ref.get("source_sha256"), "provenance": ref.get("provenance")}),
        "title": title,
        "source_id": _text_or_none(ref.get("source_id")),
        "anchor_node_id": anchor_node_id,
        "document_anchor_id": anchor_node_id,
        "chunk_ids": chunk_ids,
        "section_refs": section_refs,
        "mission_id": mission_id,
        "triage_contract": _document_triage_contract(),
        "hydration": hydration_recipe,
        "hydration_recipe": hydration_recipe,
        "hydration_result_ref": hydration_result_ref,
        "provenance": {
            key: value
            for key, value in provenance.items()
            if key in {"source_id", "source_type", "publisher", "url", "observed_at", "retrieved_at", "source_hash", "sha256", "span_id"}
        },
        "raw_body_included": False,
    }


def _stream_document_deltas(payload: dict[str, Any]) -> list[dict[str, Any]]:
    deltas: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(ref: Any, mission_id: str | None = None) -> None:
        if not isinstance(ref, dict):
            return
        projected = _document_projection(ref, payload, mission_id)
        stable = str(
            projected.get("canonical_ref")
            or projected.get("document_id")
            or projected.get("anchor_node_id")
            or _stable_digest(projected)
        )
        content_hash = _text_or_none(projected.get("content_hash"))
        if content_hash:
            stable = f"{stable}:{content_hash}"
        if stable in seen:
            return
        seen.add(stable)
        deltas.append(projected)

    for ref in _as_list(payload.get("document_refs")) + _as_list(_payload_result(payload).get("document_refs")):
        add(ref)
    package = _stream_context_package(payload)
    for ref in _as_list(package.get("document_refs")) + _as_list(payload.get("document_packets")) + _as_list(_payload_result(payload).get("document_packets")):
        add(ref)
    for row in _as_list(_stream_mission_ledger(payload).get("rows")):
        if not isinstance(row, dict):
            continue
        mission_id = _mission_stream_id(row)
        for ref in _as_list(row.get("document_refs")):
            add(ref, mission_id)
    return deltas[:COMPANY_DOCUMENT_TRIAGE_MAX_CANDIDATES]


def _stream_route_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    corridors = _stream_path_corridors(payload)
    search_map = _stream_search_map_2d_truth(payload)
    route_truth = payload.get("route_truth_summary")
    if not isinstance(route_truth, dict):
        route_truth = _payload_result(payload).get("route_truth_summary")
    route_truth = dict(route_truth) if isinstance(route_truth, dict) else {}
    paths = _as_list(corridors.get("paths"))
    metrics = _mapping(corridors.get("metrics"))
    map_metrics = _mapping(search_map.get("metrics"))
    return {
        "path_count": _int_or_zero(metrics.get("path_count") or corridors.get("path_count") or map_metrics.get("route_plan_count") or len(paths)),
        "planned_corridor_count": _int_or_zero(metrics.get("planned_corridor_count") or corridors.get("planned_corridor_count") or map_metrics.get("route_plan_count") or len(paths)),
        "route_event_count": _int_or_zero(metrics.get("route_event_count") or route_truth.get("route_event_count") or map_metrics.get("route_step_count") or map_metrics.get("travel_event_count")),
        "visited_node_count": _int_or_zero(metrics.get("visited_node_count") or route_truth.get("visited_node_count") or map_metrics.get("intermediate_node_count") or _payload_or_result_value(payload, "visited_current")),
        "highway_considered_count": _int_or_zero(route_truth.get("highway_considered_count") or metrics.get("highway_considered_count") or map_metrics.get("highway_considered_count")),
        "highway_traversed_count": _int_or_zero(route_truth.get("highway_traversed_count") or metrics.get("highway_traversed_count") or map_metrics.get("highway_traversed_count")),
    }


def _stream_surface_revision(
    event_type: str,
    payload: dict[str, Any],
    *,
    canonical_state: str,
    package_revision: str | None,
) -> str:
    explicit = _payload_or_result_text(payload, "surface_revision")
    if explicit:
        return explicit
    event_seq = _payload_or_result_value(payload, "seq")
    if event_seq is not None:
        return str(event_seq)
    counters = _mapping(_payload_or_result_value(payload, "snapshot_counters"))
    if counters:
        return _stable_digest({"event_type": event_type, "canonical_state": canonical_state, "counters": counters})
    return package_revision or _stable_digest({"event_type": event_type, "canonical_state": canonical_state, "payload": payload})


def _semantic_stream_projection(
    event_type: str,
    payload: dict[str, Any],
    *,
    terminal: bool,
    canonical_state: str,
    semantic_completed: bool,
) -> dict[str, Any]:
    package_revision = _payload_or_result_text(payload, "package_revision") or None
    search_id = _stream_search_id(payload)
    result_reference = search_result_ref(search_id, _payload_result(payload) or payload) if search_id else None
    provisional = not semantic_completed
    final_materialization_pending = bool(
        _payload_delivery_contract(payload).get("final_materialization_pending")
        or payload.get("final_materialization_pending")
    )
    return {
        "schema_version": SEARCH_SEMANTIC_PROJECTION_SCHEMA_VERSION,
        "search_id": search_id or None,
        "brain_id": _stream_brain_id(payload) or None,
        "surface_revision": _stream_surface_revision(
            event_type,
            payload,
            canonical_state=canonical_state,
            package_revision=package_revision,
        ),
        "package_revision": package_revision,
        "result_ref": result_reference,
        "provisional": provisional,
        "terminality": {
            "event_terminal": terminal,
            "client_terminal": terminal and not final_materialization_pending,
            "result_ready_event": event_type == "result_ready",
            "sealed_result_present": semantic_completed,
            "final_materialization_pending": final_materialization_pending,
            "canonical_search_state": canonical_state,
        },
        "mission_deltas": _stream_mission_deltas(payload, canonical_state),
        "evidence_deltas": _stream_evidence_deltas(payload),
        "document_deltas": _stream_document_deltas(payload),
        "route_metrics": _stream_route_metrics(payload),
    }


def _company_v0_type_for_source(event_type: str, projection: dict[str, Any]) -> str:
    if event_type in {"planning_complete", "semantic_contract_ready"}:
        return "search.plan_ready"
    if event_type in {"planning_started", "worker_started"}:
        return "search.running"
    if event_type in {"landing_ready", "ai_spatial_materialization_started"}:
        return "search.running"
    if event_type in {"search_blocked", "search_failed"}:
        return "result.failed"
    if event_type == "search_cancelled":
        return "result.cancelled"
    terminality = _mapping(projection.get("terminality"))
    if event_type == "result_ready":
        canonical_state = str(terminality.get("canonical_search_state") or "").strip().lower()
        if canonical_state in {"blocked", "failed"}:
            return "result.failed"
        if terminality.get("sealed_result_present") is True and terminality.get("final_materialization_pending") is not True:
            return "result.sealed"
        return "result.review_required"
    return "core.passthrough"


def _answer_patch_text(payload: dict[str, Any]) -> str:
    answer = payload.get("answer")
    if isinstance(answer, dict):
        for key in ("answer_text", "text", "summary"):
            text = _text_or_none(answer.get(key))
            if text:
                return text
    result = _payload_result(payload)
    answer = result.get("answer")
    if isinstance(answer, dict):
        for key in ("answer_text", "text", "summary"):
            text = _text_or_none(answer.get(key))
            if text:
                return text
    return _text_or_none(
        payload.get("answer_short")
        or payload.get("answer_full")
        or result.get("answer_short")
        or result.get("answer_full")
    ) or ""


def _answer_patch_refs(payload: dict[str, Any], projection: dict[str, Any]) -> tuple[list[str], list[str]]:
    evidence_ids = [
        str(item.get("stable_id") or item.get("node_id"))
        for item in _as_list(projection.get("evidence_deltas"))
        if isinstance(item, dict)
        and str(item.get("promotion_state") or "").strip().lower() == "promoted"
        and str(item.get("stable_id") or item.get("node_id") or "").strip()
    ]
    document_ids = [
        str(item.get("document_id") or item.get("anchor_node_id"))
        for item in _as_list(projection.get("document_deltas"))
        if isinstance(item, dict) and str(item.get("document_id") or item.get("anchor_node_id") or "").strip()
    ]
    if not evidence_ids:
        evidence_ids = sorted(_evidence_ids_from_value(payload))
    if not document_ids:
        document_ids = [
            _document_key(ref)
            for ref in _document_refs_from_payload(payload, _payload_result(payload))
            if _document_key(ref)
        ]
    return evidence_ids[:24], document_ids[:24]


def _answer_patch_v0(
    *,
    event_type: str,
    payload: dict[str, Any],
    projection: dict[str, Any],
) -> dict[str, Any] | None:
    text = _answer_patch_text(payload)
    if not text:
        return None
    citation_ids, document_ref_ids = _answer_patch_refs(payload, projection)
    operation = "seal" if event_type == "answer_final" else "add"
    snapshot_digest = _stable_digest(
        {
            "search_id": projection.get("search_id"),
            "surface_revision": projection.get("surface_revision"),
            "package_revision": projection.get("package_revision"),
            "citation_ids": citation_ids,
            "document_ref_ids": document_ref_ids,
        }
    )
    return {
        "schema_version": "answer-patch.v0",
        "patch_id": _stable_digest(
            {
                "event_type": event_type,
                "snapshot_digest": snapshot_digest,
                "text": text,
            }
        ),
        "snapshot_digest": snapshot_digest,
        "operation": operation,
        "claim_id": _stable_digest({"snapshot_digest": snapshot_digest, "text": text}),
        "text": text,
        "confidence": "provisional",
        "citation_ids": citation_ids,
        "document_ref_ids": document_ref_ids,
        "uncertainty": None,
    }


def _company_document_event_common(document: dict[str, Any], *, candidate_index: int) -> dict[str, Any]:
    return {
        "schema_version": COMPANY_DOCUMENT_TRIAGE_SCHEMA_VERSION,
        "candidate_index": candidate_index,
        "document_id": document.get("document_id"),
        "document_ref_id": document.get("document_ref_id"),
        "document_anchor_id": document.get("document_anchor_id") or document.get("anchor_node_id"),
        "anchor_node_id": document.get("anchor_node_id"),
        "canonical_ref": document.get("canonical_ref"),
        "canonical_url": document.get("canonical_url"),
        "content_hash": document.get("content_hash"),
        "source_hash": document.get("source_hash"),
        "raw_body_included": False,
    }


def _company_document_ref_ready_delta(document: dict[str, Any], *, candidate_index: int) -> dict[str, Any]:
    delta = _company_document_event_common(document, candidate_index=candidate_index)
    delta.update(
        {
            "title": document.get("title"),
            "source_id": document.get("source_id"),
            "chunk_ids": list(document.get("chunk_ids") or []),
            "section_refs": list(document.get("section_refs") or []),
            "hydration_recipe": dict(document.get("hydration_recipe") or document.get("hydration") or {}),
            "triage_contract": dict(document.get("triage_contract") or _document_triage_contract()),
            "provisional": True,
            "refs_first": True,
        }
    )
    return {key: value for key, value in delta.items() if value not in (None, "", [], {})}


def _company_document_decision_started_delta(document: dict[str, Any], *, candidate_index: int) -> dict[str, Any]:
    delta = _company_document_event_common(document, candidate_index=candidate_index)
    delta.update(
        {
            "status": "running",
            "evaluator_slot": ((candidate_index - 1) % COMPANY_DOCUMENT_TRIAGE_EVALUATOR_CONCURRENCY) + 1,
            "evaluator_concurrency": COMPANY_DOCUMENT_TRIAGE_EVALUATOR_CONCURRENCY,
            "timeout_seconds": COMPANY_DOCUMENT_TRIAGE_EVALUATOR_TIMEOUT_SECONDS,
            "dedupe_keys": ["canonical_ref", "content_hash"],
            "cancelable": True,
            "provisional": True,
        }
    )
    return {key: value for key, value in delta.items() if value not in (None, "", [], {})}


def _company_document_decision_delta(
    document: dict[str, Any],
    *,
    candidate_index: int,
    should_hydrate: bool,
) -> dict[str, Any]:
    delta = _company_document_event_common(document, candidate_index=candidate_index)
    delta.update(
        {
            "status": "selected" if should_hydrate else "deferred",
            "decision": "hydrate" if should_hydrate else "defer",
            "reason": "within_hydration_budget" if should_hydrate else "hydration_budget_exhausted",
            "max_hydrated_documents": COMPANY_DOCUMENT_TRIAGE_MAX_HYDRATIONS,
            "section_scoped_hydration": True,
            "hydration_scope": {
                "mode": "section_refs" if list(document.get("section_refs") or []) else "document_ref",
                "section_refs": list(document.get("section_refs") or [])[:8],
                "chunk_ids": list(document.get("chunk_ids") or [])[:8],
            },
            "timeout_seconds": COMPANY_DOCUMENT_TRIAGE_EVALUATOR_TIMEOUT_SECONDS,
            "provisional": True,
        }
    )
    return {key: value for key, value in delta.items() if value not in (None, "", [], {})}


def _company_document_hydration_result_ref(document: dict[str, Any]) -> str:
    explicit = _text_or_none(document.get("hydration_result_ref") or _mapping(document.get("hydration_recipe")).get("result_ref"))
    if explicit:
        return explicit
    return _stable_digest(
        {
            "document_ref_id": document.get("document_ref_id"),
            "canonical_ref": document.get("canonical_ref"),
            "content_hash": document.get("content_hash"),
            "page_size": COMPANY_DOCUMENT_TRIAGE_TOTAL_CHAR_BUDGET,
        }
    )


def _company_document_hydration_delta(
    document: dict[str, Any],
    *,
    candidate_index: int,
    status: str,
) -> dict[str, Any]:
    delta = _company_document_event_common(document, candidate_index=candidate_index)
    result_ref = _company_document_hydration_result_ref(document)
    delta.update(
        {
            "status": status,
            "hydration_result_ref": result_ref,
            "result_ref": result_ref,
            "hydration_scope": {
                "mode": "section_refs" if list(document.get("section_refs") or []) else "document_ref",
                "section_refs": list(document.get("section_refs") or [])[:8],
                "chunk_ids": list(document.get("chunk_ids") or [])[:8],
            },
            "hydration_concurrency": COMPANY_DOCUMENT_TRIAGE_HYDRATION_CONCURRENCY,
            "hydration_slot": ((candidate_index - 1) % COMPANY_DOCUMENT_TRIAGE_HYDRATION_CONCURRENCY) + 1,
            "timeout_seconds": COMPANY_DOCUMENT_TRIAGE_HYDRATION_TIMEOUT_SECONDS,
            "char_budget": COMPANY_DOCUMENT_TRIAGE_TOTAL_CHAR_BUDGET,
            "provenance": dict(document.get("provenance") or {}),
            "raw_body_included": False,
            "provisional": status != "completed",
        }
    )
    return {key: value for key, value in delta.items() if value not in (None, "", [], {})}


def _company_document_open_ready_delta(document: dict[str, Any], *, candidate_index: int) -> dict[str, Any]:
    delta = _company_document_event_common(document, candidate_index=candidate_index)
    delta.update(
        {
            "status": "open_ready",
            "open_target": document.get("canonical_url") or document.get("canonical_ref") or document.get("document_id"),
            "open_kind": "canonical_url" if document.get("canonical_url") else "document_ref",
            "hydration_recipe": dict(document.get("hydration_recipe") or document.get("hydration") or {}),
            "raw_body_included": False,
            "provisional": True,
        }
    )
    return {key: value for key, value in delta.items() if value not in (None, "", [], {})}


def project_company_run_events_v0(source_event: dict[str, Any]) -> list[dict[str, Any]]:
    """Project one annotated AGVM stream event into Company RunEventV0 fixtures.

    This is a pure semantic adapter for the 90-minute vertical slice. It does
    not decide finality from UI labels: only a persisted ``result_ready`` event
    with the existing sealed-result proof becomes ``result.sealed``.
    """

    annotated = annotate_stream_event(dict(source_event or {}))
    payload = dict(annotated.get("payload") or {})
    event_type = str(annotated.get("event_type") or "").strip() or "unknown"
    projection = _mapping(_mapping(annotated.get("stream_contract")).get("semantic_projection"))
    terminality = _mapping(projection.get("terminality"))
    events: list[dict[str, Any]] = []

    for mission in _as_list(projection.get("mission_deltas")):
        if not isinstance(mission, dict):
            continue
        mission_id = _text_or_none(mission.get("mission_id"))
        if not mission_id:
            continue
        status = str(mission.get("status") or "").strip().lower()
        events.append(
            _company_v0_base_event(
                annotated,
                payload,
                event_type="mission.completed" if status in {"completed", "resolved", "covered"} else "mission.started",
                mission_id=mission_id,
                delta={
                    "mission_id": mission_id,
                    "objective": mission.get("objective"),
                    "status": mission.get("status"),
                    "semantic_identity_digest": mission.get("semantic_identity_digest"),
                    "provisional": True,
                },
                suffix=f"mission:{mission_id}",
            )
        )

    route_metrics = _mapping(projection.get("route_metrics"))
    if any(_int_or_zero(route_metrics.get(key)) for key in ("path_count", "planned_corridor_count", "route_event_count", "visited_node_count")):
        events.append(
            _company_v0_base_event(
                annotated,
                payload,
                event_type="path.step",
                delta={**route_metrics, "provisional": True},
                suffix="path.step",
            )
        )

    for evidence in _as_list(projection.get("evidence_deltas")):
        if not isinstance(evidence, dict):
            continue
        if str(evidence.get("promotion_state") or "").strip().lower() != "promoted":
            continue
        evidence_id = _text_or_none(evidence.get("stable_id") or evidence.get("node_id"))
        if not evidence_id:
            continue
        mission_id = _text_or_none(evidence.get("mission_id"))
        provenance = _mapping(evidence.get("provenance"))
        events.append(
            _company_v0_base_event(
                annotated,
                payload,
                event_type="evidence.promoted",
                mission_id=mission_id,
                delta={
                    "evidence_id": evidence_id,
                    "node_id": evidence.get("node_id"),
                    "canonical_url": provenance.get("url"),
                    "source_hash": provenance.get("source_hash") or provenance.get("sha256"),
                    "span_id": provenance.get("span_id"),
                    "status": "promoted",
                    "trust": evidence.get("trust"),
                    "provisional": True,
                },
                suffix=f"evidence:{evidence_id}",
            )
        )

    for candidate_index, document in enumerate(_as_list(projection.get("document_deltas")), start=1):
        if not isinstance(document, dict):
            continue
        document_id = _text_or_none(document.get("document_id") or document.get("anchor_node_id"))
        if not document_id:
            continue
        mission_id = _text_or_none(document.get("mission_id"))
        events.append(
            _company_v0_base_event(
                annotated,
                payload,
                event_type="document.ref_ready",
                mission_id=mission_id,
                delta=_company_document_ref_ready_delta(document, candidate_index=candidate_index),
                suffix=f"document:{document_id}",
            )
        )
        events.append(
            _company_v0_base_event(
                annotated,
                payload,
                event_type="decision_started",
                mission_id=mission_id,
                delta=_company_document_decision_started_delta(document, candidate_index=candidate_index),
                suffix=f"document:{document_id}:decision_started",
            )
        )
        should_hydrate = candidate_index <= COMPANY_DOCUMENT_TRIAGE_MAX_HYDRATIONS
        events.append(
            _company_v0_base_event(
                annotated,
                payload,
                event_type="decision",
                mission_id=mission_id,
                delta=_company_document_decision_delta(
                    document,
                    candidate_index=candidate_index,
                    should_hydrate=should_hydrate,
                ),
                suffix=f"document:{document_id}:decision",
            )
        )
        if should_hydrate:
            for hydration_event, status in (
                ("hydration.started", "started"),
                ("hydration.progress", "refs_resolved"),
                ("hydration.completed", "completed"),
            ):
                events.append(
                    _company_v0_base_event(
                        annotated,
                        payload,
                        event_type=hydration_event,
                        mission_id=mission_id,
                        delta=_company_document_hydration_delta(
                            document,
                            candidate_index=candidate_index,
                            status=status,
                        ),
                        suffix=f"document:{document_id}:{hydration_event}",
                    )
                )
        events.append(
            _company_v0_base_event(
                annotated,
                payload,
                event_type="document.open_ready",
                mission_id=mission_id,
                delta=_company_document_open_ready_delta(document, candidate_index=candidate_index),
                suffix=f"document:{document_id}:open_ready",
            )
        )

    answer_patch = _answer_patch_v0(event_type=event_type, payload=payload, projection=projection)
    if answer_patch:
        events.append(
            _company_v0_base_event(
                annotated,
                payload,
                event_type="draft.patch",
                terminal=False,
                delta=answer_patch,
                suffix=f"draft.patch:{answer_patch['patch_id']}",
            )
        )

    source_type = _company_v0_type_for_source(event_type, projection)
    if source_type in {"result.sealed", "result.review_required", "result.failed", "result.cancelled", "search.plan_ready", "search.running"}:
        delta = _company_v0_status_delta(
            event_type=event_type,
            payload=payload,
            terminality=terminality,
            source_type=source_type,
        )
        if source_type == "result.sealed":
            delta["final_receipt"] = company_final_receipt_v0(annotated, payload)
        events.append(
            _company_v0_base_event(
                annotated,
                payload,
                event_type=source_type,
                terminal=source_type in {"result.sealed", "result.review_required", "result.failed", "result.cancelled"},
                delta=delta,
                suffix=source_type,
            )
        )

    if events:
        return events
    return [
        _company_v0_base_event(
            annotated,
            payload,
            event_type="core.passthrough",
            terminal=False,
            delta={
                "source_event_type": event_type,
                "payload": payload,
                "provisional": True,
            },
            suffix="passthrough",
        )
    ]


def canonicalize_search_result(search_id: str, result: dict[str, Any], *, event_type: str = "result_ready") -> dict[str, Any]:
    normalized = dict(result or {})
    normalized["search_id"] = str(normalized.get("search_id") or search_id)
    # Every authoritative result surface carries the same monotonic progress
    # counters as streamed snapshots.  Final retrieval results do not
    # necessarily pass through ``canonicalize_search_snapshot`` before they are
    # persisted, so leaving this work to the result-ready envelope made direct
    # ``/memory/query-result`` and inspector consumers observe empty counters
    # even though the frozen branches and evidence ledger were populated.
    counters = search_snapshot_counters(normalized)
    normalized["snapshot_counters"] = counters
    normalized.update(counters)
    preexisting_final_sealed = search_result_has_final_seal(normalized)
    # Package revision is an authoritative seal binding. Materialize it before
    # evaluating closure, then recompute it after canonical lifecycle fields.
    normalized["package_revision"] = package_revision_for_result(search_id, normalized)
    if preexisting_final_sealed:
        for contract_key in _SEARCH_SEAL_CONTRACT_KEYS:
            contract = normalized.get(contract_key)
            if isinstance(contract, dict):
                contract["package_revision"] = normalized["package_revision"]
    input_final_sealed = search_result_has_final_seal(normalized)
    normalized["canonical_search_state"] = canonical_search_state({"result": normalized}, event_type)
    completion = _mapping(normalized.get("completion_contract"))
    completion["canonical_search_state"] = normalized["canonical_search_state"]
    delivery = _mapping(normalized.get("mcp_delivery_contract"))
    delivery["canonical_search_state"] = normalized["canonical_search_state"]
    if event_type == "result_ready":
        delivery["terminal_for_client"] = True
        delivery["final_materialization_pending"] = False
        if normalized["canonical_search_state"] == "completed" and input_final_sealed:
            delivery["completion_state"] = "finalized"
        elif normalized["canonical_search_state"] == "review_required":
            normalized["status"] = "review_required"
            normalized["completion_state"] = "review_required"
            normalized["review_required"] = True
            normalized["terminal_for_client"] = True
            normalized["result_ready_terminal"] = True
            normalized["final_materialization_pending"] = False
            completion.update(
                {
                    "state": "review_required",
                    "status": "review_required",
                    "completion_state": "review_required",
                    "terminal_for_client": True,
                    "result_ready_terminal": True,
                    "final_materialization_pending": False,
                    "blocked": False,
                }
            )
            delivery["completion_state"] = "review_required"
            delivery["client_payload_state"] = "review_required"
            delivery["blocked"] = False
    normalized["completion_contract"] = completion
    normalized["mcp_delivery_contract"] = delivery
    normalized["package_revision"] = package_revision_for_result(search_id, normalized)
    completion["package_revision"] = normalized["package_revision"]
    delivery["package_revision"] = normalized["package_revision"]
    if input_final_sealed:
        for contract_key in _SEARCH_SEAL_CONTRACT_KEYS:
            contract = normalized.get(contract_key)
            if isinstance(contract, dict):
                contract["package_revision"] = normalized["package_revision"]
    return normalized


def canonical_result_ready_payload(search_id: str, result: dict[str, Any]) -> dict[str, Any]:
    source = dict(result or {})
    normalized = (
        canonicalize_search_result(search_id, source)
        if source.get("snapshot_kind")
        else canonicalize_search_snapshot(
            search_id,
            source,
            snapshot_kind="final",
            brain_id=str(source.get("brain_id") or "") or None,
            parent_package_revision=str(source.get("parent_package_revision") or "") or None,
        )
    )
    blocked = normalized["canonical_search_state"] in {"blocked", "failed"}
    final_sealed = normalized["canonical_search_state"] == "completed" and search_result_has_final_seal(normalized)
    return {
        "search_id": str(search_id),
        "brain_id": normalized.get("brain_id"),
        "result": normalized,
        "result_ref": search_result_ref(search_id, normalized),
        "snapshot_kind": normalized.get("snapshot_kind") or "final",
        "parent_package_revision": normalized.get("parent_package_revision"),
        "snapshot_counters": _mapping(normalized.get("snapshot_counters")),
        "visited_current": normalized.get("visited_current", 0),
        "visited_total": normalized.get("visited_total", 0),
        "hydrated_current": normalized.get("hydrated_current", 0),
        "hydrated_total": normalized.get("hydrated_total", 0),
        "promoted_current": normalized.get("promoted_current", 0),
        "promoted_total": normalized.get("promoted_total", 0),
        "package_current": normalized.get("package_current", 0),
        "package_total": normalized.get("package_total", 0),
        "promoted": normalized.get("promoted", 0),
        "hydrated": normalized.get("hydrated", 0),
        "package": normalized.get("package", 0),
        "package_revision": normalized["package_revision"],
        "canonical_search_state": normalized["canonical_search_state"],
        "result_materialization_state": "blocked" if blocked else "finalized" if final_sealed else "review_required",
        "final_materialization_pending": False,
        "result_ready_terminal": True,
        "terminal_for_client": True,
    }

STREAM_EVENT_FAMILIES = {
    "intent",
    "ai",
    "heuristic",
    "path",
    "document",
    "package",
    "judge",
    "answer",
    "blocked",
    "runtime",
    "system",
}


_EVENT_FAMILY_BY_TYPE = {
    "worker_started": "runtime",
    "planning_started": "intent",
    "planning_complete": "intent",
    "semantic_contract_ready": "intent",
    "semantic_contract_retry_wait_started": "judge",
    "semantic_contract_retry_wait_completed": "judge",
    "landing_ready": "heuristic",
    "scout_status": "ai",
    "post_final_ai_worker_throttle": "ai",
    "post_final_worker_throttle": "path",
    "step_complete": "path",
    "worker_stopped": "path",
    "metrics_update": "runtime",
    "context_update": "package",
    "context_wave": "package",
    "result_snapshot_ready": "package",
    "final_materialization_started": "package",
    "final_materialization_heartbeat": "package",
    "final_materialization_completed": "package",
    "background_enrichment_started": "path",
    "background_enrichment_yield": "path",
    "background_enrichment_stopped": "path",
    "background_enrichment_completed": "path",
    "answer_candidate": "answer",
    "answer_partial": "answer",
    "answer_final": "answer",
    "search_stopped": "runtime",
    "result_ready": "runtime",
    "search_blocked": "blocked",
    "search_failed": "blocked",
    "calibration_skipped": "system",
}


def _payload_bool(payload: dict[str, Any], key: str) -> bool:
    return bool(payload.get(key) is True)


def _payload_text(payload: dict[str, Any], key: str) -> str:
    return str(payload.get(key) or "").strip()


def _payload_result(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload.get("result")
    return dict(result) if isinstance(result, dict) else {}


def _payload_or_result_text(payload: dict[str, Any], key: str) -> str:
    value = _payload_text(payload, key)
    if value:
        return value
    return _payload_text(_payload_result(payload), key)


def _payload_or_result_bool(payload: dict[str, Any], key: str) -> bool:
    if _payload_bool(payload, key):
        return True
    return _payload_bool(_payload_result(payload), key)


def _payload_or_result_value(payload: dict[str, Any], key: str) -> Any:
    if key in payload:
        return payload.get(key)
    return _payload_result(payload).get(key)


def _materializing_search_stopped_surface(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload or {})
    normalized["canonical_search_state"] = "finalizing"
    normalized["result_materialization_state"] = "materializing"
    normalized["final_materialization_pending"] = True
    normalized["result_ready_terminal"] = False
    normalized["terminal_for_client"] = False

    completion = _mapping(normalized.get("completion_contract"))
    completion.update(
        {
            "state": "materializing",
            "canonical_search_state": "finalizing",
            "final_materialization_pending": True,
            "result_ready_terminal": False,
        }
    )
    normalized["completion_contract"] = completion

    delivery = _mapping(normalized.get("mcp_delivery_contract"))
    delivery.update(
        {
            "canonical_search_state": "finalizing",
            "completion_state": "materializing",
            "terminal_for_client": False,
            "partial_for_client": True,
            "final_materialization_pending": True,
            "background_state": "stopping",
        }
    )
    normalized["mcp_delivery_contract"] = delivery

    if "context_package_materialization" in normalized:
        materialization = _mapping(normalized.get("context_package_materialization"))
        materialization.update(
            {
                "terminal": False,
                "terminal_for_client": False,
                "terminal_for_mcp_client": False,
                "final_materialization_pending": True,
            }
        )
        normalized["context_package_materialization"] = materialization
    return normalized


def normalize_stream_lifecycle_payload(event_type: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    normalized = dict(payload or {})
    normalized_event_type = str(event_type or "").strip()
    if normalized_event_type == "final_materialization_completed":
        normalized["result_ready_terminal"] = False
        normalized["terminal_for_client"] = False
        completion = _mapping(normalized.get("completion_contract"))
        completion.update(
            {
                "final_materialization_pending": False,
                "result_ready_terminal": False,
            }
        )
        normalized["completion_contract"] = completion
        delivery = _mapping(normalized.get("mcp_delivery_contract"))
        delivery.update(
            {
                "terminal_for_client": False,
                "partial_for_client": True,
                "final_materialization_pending": False,
            }
        )
        normalized["mcp_delivery_contract"] = delivery
        return normalized
    if normalized_event_type != "search_stopped":
        return normalized
    result = normalized.get("result")
    if isinstance(result, dict):
        normalized["result"] = _materializing_search_stopped_surface(result)
    return _materializing_search_stopped_surface(normalized)


def _payload_delivery_contract(payload: dict[str, Any]) -> dict[str, Any]:
    direct = payload.get("mcp_delivery_contract")
    if isinstance(direct, dict):
        return direct
    result_delivery = _payload_result(payload).get("mcp_delivery_contract")
    return dict(result_delivery) if isinstance(result_delivery, dict) else {}


def _contract_blocked(event_type: str, payload: dict[str, Any]) -> bool:
    if event_type in {"search_blocked", "search_failed"}:
        return True
    # Landing/path/materialization events may carry an aggregate answer
    # snapshot whose surface is still ``not_ready``. That is normal progress,
    # not a terminal Search block. Closure gates become authoritative only on
    # closure events; otherwise Company clients would stop on ``landing_ready``
    # before the following materialization/route events arrive.
    if event_type not in {"answer_final", "search_stopped", "result_ready"}:
        return False
    delivery = _payload_delivery_contract(payload)
    if str(delivery.get("client_payload_state") or "").strip() in {"blocked", "failed"}:
        return True
    stop_reason = _payload_or_result_text(payload, "stop_reason").lower()
    answer_surface_state = _payload_or_result_text(payload, "answer_surface_state").lower()
    closure_state = _payload_or_result_text(payload, "closure_state").lower()
    if any(token in stop_reason for token in ("blocked", "not_satisfied", "missing", "insufficient", "failed")):
        return True
    if any(token in answer_surface_state for token in ("blocked", "not_ready", "insufficient")):
        return True
    if closure_state == "blocked":
        return True
    if _payload_or_result_bool(payload, "ai_validation_blocked_final_seal"):
        return True
    if _payload_or_result_bool(payload, "ai_materialization_blocked_final_seal"):
        return True
    ai_gate = _payload_or_result_value(payload, "ai_validation_gate")
    if isinstance(ai_gate, dict) and ai_gate.get("blocked") is True:
        return True
    ai_materialization_gate = _payload_or_result_value(payload, "ai_materialization_hard_gate")
    if isinstance(ai_materialization_gate, dict) and ai_materialization_gate.get("blocked") is True:
        return True
    blockers = _payload_or_result_value(payload, "final_closure_blockers")
    return bool(isinstance(blockers, list) and blockers)


def _event_family(event_type: str) -> str:
    if event_type in _EVENT_FAMILY_BY_TYPE:
        return _EVENT_FAMILY_BY_TYPE[event_type]
    lowered = event_type.lower()
    if "document" in lowered:
        return "document"
    if "context" in lowered or "package" in lowered or "materialization" in lowered:
        return "package"
    if "answer" in lowered:
        return "answer"
    if "ai" in lowered or "scout" in lowered or "llm" in lowered:
        return "ai"
    if "path" in lowered or "route" in lowered or "worker" in lowered:
        return "path"
    if "fail" in lowered or "block" in lowered:
        return "blocked"
    return "runtime"


def _package_state(
    event_type: str,
    payload: dict[str, Any],
    terminal: bool,
    canonical_state: str,
    semantic_completed: bool,
) -> str:
    if event_type in {"context_update", "context_wave"}:
        return "streaming"
    if event_type == "search_stopped" and payload.get("final_materialization_pending") is True:
        return "materializing"
    if event_type in {"final_materialization_started", "final_materialization_heartbeat"}:
        return "materializing"
    if event_type == "result_ready" and canonical_state == "review_required":
        return "review_required"
    if event_type in {"final_materialization_completed", "result_ready"} and terminal and semantic_completed:
        return "finalized"
    if payload.get("context_package") or payload.get("context_package_materialization"):
        return "updated"
    return "pending"


def _path_state(event_type: str, payload: dict[str, Any], terminal: bool) -> str:
    if event_type == "landing_ready":
        return "planned"
    if event_type in {"step_complete", "worker_stopped", "background_enrichment_yield"}:
        return "traversing"
    if payload.get("path_corridors") or payload.get("route_truth_summary"):
        return "visible"
    if terminal:
        return "completed"
    return "pending"


def _document_state(event_type: str, payload: dict[str, Any]) -> str:
    document_mode = _payload_text(payload, "document_mode").lower()
    if event_type.startswith("document_"):
        return "streaming"
    if payload.get("document_workspace") or payload.get("document_packets") or payload.get("document_lookup"):
        return "available"
    if document_mode and document_mode != "none":
        return document_mode
    return "none"


def _answer_state(
    event_type: str,
    payload: dict[str, Any],
    blocked: bool,
    terminal: bool,
    canonical_state: str,
    semantic_completed: bool,
) -> str:
    if blocked:
        return "blocked"
    if event_type == "answer_partial":
        return "usable" if payload.get("usable") is True else "partial"
    if event_type == "answer_final":
        return "sealed" if payload.get("final_closure_ready") is True else "partial"
    if event_type == "result_ready" and terminal:
        if semantic_completed:
            return "sealed"
        if canonical_state == "review_required":
            return "usable_partial"
        return "partial"
    if payload.get("answer") or payload.get("answer_short"):
        return "partial"
    return "pending"


def _judge_state(payload: dict[str, Any], blocked: bool) -> str:
    if blocked:
        return "blocked"
    if payload.get("final_closure_ready") is True:
        return "approved"
    if payload.get("semantic_contract") or payload.get("semantic_contract_runtime"):
        runtime = payload.get("semantic_contract_runtime")
        if isinstance(runtime, dict):
            status = _payload_text(runtime, "status")
            return status or "ready"
        return "ready"
    if payload.get("final_closure_blockers"):
        return "needs_more"
    return "pending"


def _run_state(
    event_type: str,
    payload: dict[str, Any],
    blocked: bool,
    terminal: bool,
    canonical_state: str,
    semantic_completed: bool,
) -> str:
    if payload.get("superseded") is True:
        return "superseded"
    if event_type == "search_failed":
        return "failed"
    if event_type in {"result_ready", "search_stopped"} and canonical_state == "review_required":
        return "review_required"
    if blocked and event_type in {"answer_final", "search_stopped", "result_ready"}:
        return "blocked"
    if event_type in {"planning_started", "planning_complete", "semantic_contract_ready"}:
        return "planning"
    if event_type == "landing_ready":
        return "landing"
    if event_type in {"step_complete", "worker_stopped", "background_enrichment_started", "background_enrichment_yield", "background_enrichment_stopped", "background_enrichment_completed"}:
        return "path_streaming"
    if event_type in {"context_update", "context_wave"}:
        return "package_streaming"
    if event_type in {"answer_candidate", "answer_partial"}:
        return "answering"
    if event_type == "answer_final":
        return "final_sealed" if payload.get("final_closure_ready") is True else "answering"
    if event_type in {
        "result_snapshot_ready",
        "final_materialization_started",
        "final_materialization_heartbeat",
    }:
        return "finalizing"
    if event_type == "final_materialization_completed":
        return "finalized"
    if event_type == "search_stopped" and not terminal:
        return "finalizing"
    if event_type == "result_ready" and terminal:
        return "final_sealed" if semantic_completed else "blocked" if blocked else "review_required"
    if event_type == "search_stopped" and terminal:
        return "final_sealed" if semantic_completed else "blocked" if blocked else "review_required"
    return "running"


def _surface_state(event_type: str, run_state: str, answer_state: str, blocked: bool) -> str:
    if event_type == "search_failed":
        return "failed"
    if blocked:
        return "blocked"
    if run_state == "superseded":
        return "superseded"
    if run_state == "final_sealed":
        return "sealed"
    if run_state == "review_required" or answer_state == "usable_partial":
        return "usable_partial"
    if answer_state == "usable":
        return "usable"
    if answer_state in {"partial", "finalized"}:
        return "partial"
    return "running"


def build_stream_event_contract(event_type: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    event_type = str(event_type or "unknown")
    normalized_payload = normalize_stream_lifecycle_payload(event_type, payload)
    delivery = _payload_delivery_contract(normalized_payload)
    terminal = bool(
        event_type in {"search_blocked", "search_cancelled", "search_failed"}
        or (event_type == "result_ready" and not normalized_payload.get("final_materialization_pending"))
    )
    canonical_state = canonical_search_state(normalized_payload, event_type)
    semantic_completed = canonical_state == "completed" and search_result_has_final_seal(normalized_payload)
    blocked = canonical_state in {"blocked", "failed"} or (
        canonical_state != "review_required" and _contract_blocked(event_type, normalized_payload)
    )
    family = "blocked" if blocked and event_type == "search_failed" else _event_family(event_type)
    run_state = _run_state(
        event_type,
        normalized_payload,
        blocked,
        terminal,
        canonical_state,
        semantic_completed,
    )
    answer_state = _answer_state(
        event_type,
        normalized_payload,
        blocked,
        terminal,
        canonical_state,
        semantic_completed,
    )
    surface_state = _surface_state(event_type, run_state, answer_state, blocked)
    final_materialization_pending = bool(
        delivery.get("final_materialization_pending")
        or normalized_payload.get("final_materialization_pending")
    )
    semantic_projection = _semantic_stream_projection(
        event_type,
        normalized_payload,
        terminal=terminal,
        canonical_state=canonical_state,
        semantic_completed=semantic_completed,
    )
    return {
        "schema_version": STREAM_EVENT_CONTRACT_SCHEMA_VERSION,
        "event_type": event_type,
        "event_family": family,
        "run_state": run_state,
        "surface_state": surface_state,
        "search_id": normalized_payload.get("search_id"),
        "brain_id": normalized_payload.get("brain_id"),
        "terminal": terminal,
        "blocked": blocked,
        "failed": event_type == "search_failed",
        "superseded": run_state == "superseded",
        "package_state": _package_state(
            event_type,
            normalized_payload,
            terminal,
            canonical_state,
            semantic_completed,
        ),
        "path_state": _path_state(event_type, normalized_payload, terminal),
        "document_state": _document_state(event_type, normalized_payload),
        "answer_state": answer_state,
        "judge_state": _judge_state(normalized_payload, blocked),
        "client_payload_state": delivery.get("client_payload_state"),
        "client_terminal": bool(delivery.get("terminal_for_client") or (terminal and not final_materialization_pending)),
        "completion_state": delivery.get("completion_state"),
        "final_materialization_pending": final_materialization_pending,
        "canonical_search_state": canonical_state,
        "package_revision": _payload_or_result_text(normalized_payload, "package_revision") or None,
        "surface_revision": semantic_projection["surface_revision"],
        "semantic_projection": semantic_projection,
    }


def annotate_stream_event(event: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(event or {})
    payload = dict(normalized.get("payload") or {})
    event_type = str(normalized.get("event_type") or "")
    payload = normalize_stream_lifecycle_payload(event_type, payload)
    if event_type == "result_ready":
        result = _payload_result(payload)
        search_id = str(payload.get("search_id") or result.get("search_id") or "").strip()
        if result and search_id:
            payload = {**payload, **canonical_result_ready_payload(search_id, result)}
        elif search_id:
            payload.setdefault("result_ref", search_result_ref(search_id))
    contract = build_stream_event_contract(event_type, payload)
    payload.setdefault("stream_contract", dict(contract))
    normalized["payload"] = payload
    normalized["stream_contract"] = contract
    normalized["event_family"] = contract["event_family"]
    normalized["run_state"] = contract["run_state"]
    normalized["surface_state"] = contract["surface_state"]
    normalized["terminal"] = bool(contract["terminal"])
    return normalized
