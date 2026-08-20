from __future__ import annotations

from typing import Any


STREAM_EVENT_CONTRACT_SCHEMA_VERSION = "agvm.brain_os_stream_event.v1"

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


def _payload_delivery_contract(payload: dict[str, Any]) -> dict[str, Any]:
    direct = payload.get("mcp_delivery_contract")
    if isinstance(direct, dict):
        return direct
    result_delivery = _payload_result(payload).get("mcp_delivery_contract")
    return dict(result_delivery) if isinstance(result_delivery, dict) else {}


def _contract_blocked(event_type: str, payload: dict[str, Any]) -> bool:
    if event_type == "search_failed":
        return True
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


def _package_state(event_type: str, payload: dict[str, Any], terminal: bool) -> str:
    if event_type in {"context_update", "context_wave"}:
        return "streaming"
    if event_type == "final_materialization_started":
        return "materializing"
    if event_type in {"final_materialization_completed", "result_ready"} and terminal:
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


def _answer_state(event_type: str, payload: dict[str, Any], blocked: bool, terminal: bool) -> str:
    if blocked:
        return "blocked"
    if event_type == "answer_partial":
        return "usable" if payload.get("usable") is True else "partial"
    if event_type == "answer_final":
        return "sealed" if payload.get("final_closure_ready") is True else "partial"
    if event_type == "result_ready" and terminal:
        result = _payload_result(payload)
        if result.get("final_closure_ready") is True:
            return "sealed"
        return "finalized"
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


def _run_state(event_type: str, payload: dict[str, Any], blocked: bool, terminal: bool) -> str:
    if payload.get("superseded") is True:
        return "superseded"
    if event_type == "search_failed":
        return "failed"
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
    if event_type in {"result_snapshot_ready", "final_materialization_started"}:
        return "finalizing"
    if event_type == "final_materialization_completed":
        return "finalized"
    if event_type == "result_ready" and terminal:
        return "final_sealed" if not blocked else "blocked"
    if event_type == "search_stopped" and terminal:
        return "final_sealed" if not blocked else "blocked"
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
    if answer_state == "usable":
        return "usable"
    if answer_state in {"partial", "finalized"}:
        return "partial"
    return "running"


def build_stream_event_contract(event_type: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    normalized_payload = dict(payload or {})
    event_type = str(event_type or "unknown")
    delivery = _payload_delivery_contract(normalized_payload)
    terminal = bool(event_type in {"search_failed"} or (event_type == "result_ready" and not normalized_payload.get("final_materialization_pending")))
    if event_type == "search_stopped" and normalized_payload.get("result_ready_terminal") is True:
        terminal = True
    blocked = _contract_blocked(event_type, normalized_payload)
    family = "blocked" if blocked and event_type == "search_failed" else _event_family(event_type)
    run_state = _run_state(event_type, normalized_payload, blocked, terminal)
    answer_state = _answer_state(event_type, normalized_payload, blocked, terminal)
    surface_state = _surface_state(event_type, run_state, answer_state, blocked)
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
        "package_state": _package_state(event_type, normalized_payload, terminal),
        "path_state": _path_state(event_type, normalized_payload, terminal),
        "document_state": _document_state(event_type, normalized_payload),
        "answer_state": answer_state,
        "judge_state": _judge_state(normalized_payload, blocked),
        "client_payload_state": delivery.get("client_payload_state"),
        "client_terminal": bool(delivery.get("terminal_for_client")),
        "completion_state": delivery.get("completion_state"),
        "final_materialization_pending": bool(delivery.get("final_materialization_pending")),
    }


def annotate_stream_event(event: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(event or {})
    payload = dict(normalized.get("payload") or {})
    contract = build_stream_event_contract(str(normalized.get("event_type") or ""), payload)
    payload.setdefault("stream_contract", dict(contract))
    normalized["payload"] = payload
    normalized["stream_contract"] = contract
    normalized["event_family"] = contract["event_family"]
    normalized["run_state"] = contract["run_state"]
    normalized["surface_state"] = contract["surface_state"]
    normalized["terminal"] = bool(contract["terminal"])
    return normalized
