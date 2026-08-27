# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Iterable

from brain_feedback_ledger import build_brain_feedback_ledger
from sqlite_store import append_brain_feedback_signals, fetch_brain_feedback_ledger_page


LOGGER = logging.getLogger(__name__)

_EXPLICIT_KINDS = frozenset({"explicit_review", "explicit_correct"})
_OPERATIONAL_CODE = re.compile(r"^[a-zA-Z0-9_.:-]{1,96}$")
_TIMESTAMP = re.compile(r"^[0-9TZ:+.\-]{1,48}$")
_HEALTH_PERSISTED_FAMILIES = frozenset(
    {
        "correction_applied",
        "evidence_hydrated",
        "evidence_opened",
        "explicit_correct",
        "explicit_review",
        "learning_event",
    }
)


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _text(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _operational_code(value: Any, *, fallback: str) -> str | None:
    normalized = _text(value)
    if not normalized:
        return None
    if _OPERATIONAL_CODE.fullmatch(normalized):
        return normalized.lower()
    return f"{fallback}:{_canonical_digest(normalized)[:16]}"


def _ids(values: Any, *, limit: int = 64) -> list[str]:
    if values is not None and not isinstance(values, (list, tuple, set)):
        values = [values]
    output: list[str] = []
    for value in values or ():
        normalized = _text(value)
        if normalized and normalized not in output:
            output.append(normalized[:240])
        if len(output) >= limit:
            break
    return output


def _event_id(
    *,
    brain_id: str,
    session_id: str | None,
    event_kind: str,
    source_event_id: str | None,
    node_ids: list[str],
    document_ids: list[str],
    details: dict[str, Any],
) -> str:
    basis = {
        "brain_id": brain_id,
        "session_id": session_id or "",
        "event_kind": event_kind,
        "source_event_digest": _canonical_digest(_text(source_event_id)) if _text(source_event_id) else "",
        "node_ids": node_ids,
        "document_ids": document_ids,
        "details": details,
    }
    return f"{event_kind}::{_canonical_digest(basis)[:32]}"


def _sanitized_event(
    *,
    event_kind: str,
    brain_id: str,
    session_id: str | None,
    source_event_id: str | None = None,
    node_ids: Iterable[Any] | None = None,
    document_ids: Iterable[Any] | None = None,
    status: Any = None,
    stop_reason: Any = None,
    failure_reason: Any = None,
    answerability_state: Any = None,
    verdict: Any = None,
    correction_mode: Any = None,
    created_at: Any = None,
) -> tuple[dict[str, Any], str]:
    normalized_kind = re.sub(r"[^a-z0-9]+", "_", str(event_kind or "").strip().lower()).strip("_")
    normalized_brain_id = str(brain_id or "").strip()
    if not normalized_kind:
        raise ValueError("brain_feedback_event_kind_required")
    if not normalized_brain_id:
        raise ValueError("brain_feedback_brain_id_required")
    normalized_session_id = _text(session_id)
    normalized_node_ids = _ids(node_ids)
    normalized_document_ids = _ids(document_ids)
    details = {}
    for key, value in (
        ("status", status),
        ("stop_reason", stop_reason),
        ("failure_reason", failure_reason),
        ("answerability_state", answerability_state),
        ("verdict", verdict),
        ("correction_mode", correction_mode),
    ):
        if normalized := _operational_code(value, fallback=key):
            details[key] = normalized
    event_id = _event_id(
        brain_id=normalized_brain_id,
        session_id=normalized_session_id,
        event_kind=normalized_kind,
        source_event_id=source_event_id,
        node_ids=normalized_node_ids,
        document_ids=normalized_document_ids,
        details=details,
    )
    event: dict[str, Any] = {
        "schema_version": "agvm.brain_feedback_runtime_event.v2",
        "event_id": event_id,
        "event_type": normalized_kind,
        "brain_id": normalized_brain_id,
        "search_id": normalized_session_id,
        "explicit": normalized_kind in _EXPLICIT_KINDS,
        **details,
    }
    if normalized_node_ids:
        event["node_ids"] = normalized_node_ids
    if normalized_document_ids:
        event["document_ids"] = normalized_document_ids
    normalized_created_at = _text(created_at)
    if normalized_created_at and _TIMESTAMP.fullmatch(normalized_created_at):
        event["created_at"] = normalized_created_at
    return event, event_id


def _signals_for_event(event: dict[str, Any], *, source: str) -> list[dict[str, Any]]:
    kwargs: dict[str, Any] = {"brain_id": event["brain_id"]}
    if source == "search_session":
        kwargs["search_sessions"] = [event]
    elif source == "correction_history":
        kwargs["corrections"] = [event]
    elif source == "learning_event":
        kwargs["learning_events"] = [event]
    else:
        kwargs["feedback_events"] = [event]
    return list(build_brain_feedback_ledger(**kwargs).get("signals") or [])


def record_feedback_event(
    *,
    event_kind: str,
    brain_id: str,
    session_id: str | None = None,
    source: str = "feedback_event",
    source_event_id: str | None = None,
    node_ids: Iterable[Any] | None = None,
    document_ids: Iterable[Any] | None = None,
    status: Any = None,
    stop_reason: Any = None,
    failure_reason: Any = None,
    answerability_state: Any = None,
    verdict: Any = None,
    correction_mode: Any = None,
    created_at: Any = None,
) -> dict[str, Any]:
    """Append normalized feedback without changing the calling operation outcome."""

    try:
        event, base_event_id = _sanitized_event(
            event_kind=event_kind,
            brain_id=brain_id,
            session_id=session_id,
            source_event_id=source_event_id,
            node_ids=node_ids,
            document_ids=document_ids,
            status=status,
            stop_reason=stop_reason,
            failure_reason=failure_reason,
            answerability_state=answerability_state,
            verdict=verdict,
            correction_mode=correction_mode,
            created_at=created_at,
        )
        signals = _signals_for_event(event, source=source)
        signal_event_ids = [
            (
                f"{base_event_id}::{signal.get('signal_family') or 'unknown'}::"
                f"{signal.get('source_kind') or 'unknown'}"
            )
            for signal in signals
        ]
        records = append_brain_feedback_signals(
            signals,
            brain_id=event["brain_id"],
            session_id=_text(event.get("search_id")),
            event_ids=signal_event_ids,
        )
        receipt_records = [
            {
                "ledger_seq": int(record["ledger_seq"]),
                "ledger_event_id": str(record["ledger_event_id"]),
                "content_digest": str(record["content_digest"]),
                "signal_family": str(record["signal_family"]),
            }
            for record in records
        ]
        receipt_material = {
            "schema_version": "agvm.brain_feedback_ledger_receipt.v1",
            "brain_id": event["brain_id"],
            "session_id": _text(event.get("search_id")),
            "event_id": base_event_id,
            "record_count": len(receipt_records),
            "records": receipt_records,
        }
        return {
            "schema_version": "agvm.brain_feedback_ledger_write.v2",
            "status": "recorded",
            "brain_id": event["brain_id"],
            "session_id": _text(event.get("search_id")),
            "event_id": base_event_id,
            "record_count": len(records),
            "idempotent_replay": bool(records) and all(bool(row.get("idempotent_replay")) for row in records),
            "chargeable": False,
            "charged_units": 0,
            "receipt": {
                **receipt_material,
                "receipt_digest": _canonical_digest(receipt_material),
                "storage": "sqlite_append_only",
                "mutates_memory": False,
            },
        }
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception(
            "brain_feedback_ledger_write_failed brain_id=%s session_id=%s event_kind=%s error=%s",
            _text(brain_id),
            _text(session_id),
            _text(event_kind),
            type(exc).__name__,
        )
        return {
            "schema_version": "agvm.brain_feedback_ledger_write.v2",
            "status": "failed",
            "brain_id": _text(brain_id),
            "session_id": _text(session_id),
            "event_kind": _text(event_kind),
            "error_code": f"feedback_ledger_write_failed:{type(exc).__name__}",
            "chargeable": False,
            "charged_units": 0,
        }


def record_search_terminal(
    *,
    brain_id: str,
    session_id: str,
    result: dict[str, Any] | None = None,
    failed_reason: str | None = None,
) -> dict[str, Any]:
    payload = dict(result or {})
    raw_status = str(payload.get("status") or "").strip().lower()
    status = "failed" if failed_reason or raw_status in {"blocked", "failed", "error"} else "completed"
    return record_feedback_event(
        event_kind="search_failed" if status == "failed" else "search_session",
        source="search_session",
        brain_id=brain_id,
        session_id=session_id,
        status=status,
        stop_reason=payload.get("stop_reason"),
        failure_reason=failed_reason or payload.get("failure_reason"),
        answerability_state=payload.get("answerability_state"),
    )


def record_learning_event(event: dict[str, Any]) -> dict[str, Any]:
    return record_feedback_event(
        event_kind=str(event.get("event_kind") or "learning_event"),
        source="learning_event",
        brain_id=str(event.get("brain_id") or ""),
        session_id=_text(event.get("thread_id") or event.get("operation_id")),
        source_event_id=_text(event.get("event_id")),
        node_ids=[event.get("persisted_node_id"), *list(event.get("related_node_ids") or [])],
        status=event.get("apply_decision") or event.get("human_decision"),
        created_at=event.get("created_at"),
    )


def record_mcp_evidence(
    *,
    brain_id: str,
    event_kind: str,
    payload: dict[str, Any],
    session_id: str | None = None,
    source_event_id: str | None = None,
) -> dict[str, Any]:
    node_ids: list[str] = []
    document_ids: list[str] = []

    def visit(value: Any, *, depth: int = 0) -> None:
        if depth > 7 or len(node_ids) + len(document_ids) >= 64:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                normalized_key = str(key).lower()
                if normalized_key in {
                    "node_id",
                    "source_node_id",
                    "target_node_id",
                    "memory_id",
                    "anchor_node_id",
                    "focus_node_id",
                }:
                    identifier = _text(child)
                    if identifier and identifier not in node_ids:
                        node_ids.append(identifier[:240])
                elif normalized_key in {"document_id", "document_ref_id", "source_document_id"}:
                    identifier = _text(child)
                    if identifier and identifier not in document_ids:
                        document_ids.append(identifier[:240])
                elif normalized_key.endswith("node_ids") and isinstance(child, (list, tuple, set)):
                    for identifier in _ids(child, limit=64 - len(node_ids)):
                        if identifier not in node_ids:
                            node_ids.append(identifier)
                elif normalized_key.endswith("document_ids") and isinstance(child, (list, tuple, set)):
                    for identifier in _ids(child, limit=64 - len(document_ids)):
                        if identifier not in document_ids:
                            document_ids.append(identifier)
                elif isinstance(child, (dict, list, tuple)):
                    visit(child, depth=depth + 1)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child, depth=depth + 1)

    visit(payload)
    status = str(payload.get("status") or "").strip().lower()
    if status in {"blocked", "failed", "no_match"} or not (node_ids or document_ids):
        return {
            "schema_version": "agvm.brain_feedback_ledger_write.v2",
            "status": "skipped",
            "brain_id": brain_id,
            "session_id": session_id or _text(payload.get("search_id")),
            "event_kind": event_kind,
            "reason": "no_materialized_evidence",
        }
    return record_feedback_event(
        event_kind=event_kind,
        brain_id=brain_id,
        session_id=session_id or _text(payload.get("search_id")),
        source_event_id=source_event_id,
        node_ids=node_ids,
        document_ids=document_ids,
        status=payload.get("status"),
    )


def persisted_feedback_events_for_health(*, brain_id: str, limit: int = 400) -> list[dict[str, Any]]:
    """Project persisted signals back into the existing read-only Health input contract."""

    try:
        page = fetch_brain_feedback_ledger_page(brain_id=brain_id, limit=max(1, min(int(limit), 1000)))
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception(
            "brain_feedback_ledger_health_read_failed brain_id=%s error=%s",
            _text(brain_id),
            type(exc).__name__,
        )
        return []
    events: list[dict[str, Any]] = []
    for row in list(page.get("items") or []):
        signal = dict(row.get("signal") or {})
        family = str(signal.get("signal_family") or "")
        if family not in _HEALTH_PERSISTED_FAMILIES:
            continue
        event: dict[str, Any] = {
            "schema_version": str(signal.get("schema_version") or "agvm.brain_feedback_signal.v2"),
            "event_id": str(row.get("event_id") or signal.get("signal_id") or ""),
            "event_type": str(signal.get("source_kind") or family),
            "brain_id": str(row.get("brain_id") or brain_id),
            "search_id": row.get("session_id"),
            "explicit": bool(signal.get("explicit")),
            "created_at": signal.get("created_at") or row.get("signal_created_at"),
            **dict(signal.get("details") or {}),
        }
        for ref in list(signal.get("evidence_refs") or []):
            ref_kind = str(dict(ref).get("kind") or "")
            ref_id = _text(dict(ref).get("id"))
            if not ref_id:
                continue
            key = "document_ids" if ref_kind == "document" else "node_ids"
            event.setdefault(key, []).append(ref_id)
        events.append(event)
    return events


__all__ = [
    "persisted_feedback_events_for_health",
    "record_feedback_event",
    "record_learning_event",
    "record_mcp_evidence",
    "record_search_terminal",
]
