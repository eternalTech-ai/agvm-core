# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

from collections import Counter
import hashlib
import json
import re
from typing import Any, Iterable


BRAIN_FEEDBACK_LEDGER_SCHEMA_VERSION = "agvm.brain_feedback_ledger.v2"
BRAIN_FEEDBACK_SIGNAL_SCHEMA_VERSION = "agvm.brain_feedback_signal.v2"
BRAIN_FEEDBACK_HEALTH_ROLLUP_SCHEMA_VERSION = "agvm.brain_feedback_health_rollup.v2"
BRAIN_FEEDBACK_STORED_EVENT_SCHEMA_VERSION = "agvm.brain_feedback_stored_event.v2"
BRAIN_FEEDBACK_LEDGER_PAGE_SCHEMA_VERSION = "agvm.brain_feedback_ledger_page.v2"

_SIGNAL_WEIGHTS = {
    "explicit_correct": 1.0,
    "correction_applied": 0.95,
    "explicit_review": 0.9,
    "learning_event": 0.6,
    "search_failure": 0.55,
    "evidence_hydrated": 0.45,
    "search_stop": 0.35,
    "evidence_opened": 0.3,
    "search_session": 0.2,
}

_EXPLICIT_FAMILIES = frozenset({"explicit_correct", "correction_applied", "explicit_review"})
_NEGATIVE_WORDS = frozenset(
    {
        "bad",
        "blocked",
        "failed",
        "failure",
        "incorrect",
        "missing",
        "negative",
        "reject",
        "rejected",
        "wrong",
    }
)
_POSITIVE_WORDS = frozenset({"approve", "approved", "correct", "good", "helpful", "positive", "resolved"})
_NESTED_EVENT_KEYS = (
    "events",
    "feedback_events",
    "interaction_events",
    "evidence_events",
    "review_events",
    "correction_events",
)
_ID_KEYS_BY_KIND = {
    "node": ("node_id", "target_node_id", "evidence_node_id", "memory_id"),
    "document": ("document_id", "document_ref_id", "source_document_id"),
    "evidence": ("evidence_id", "evidence_ref"),
}


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    return []


def _normalized_kind(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    return text or "unknown"


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_feedback_digest(value: Any) -> str:
    """Return the canonical digest used by persisted feedback records."""

    return _canonical_digest(value)


def prepare_feedback_signal_for_storage(
    signal: dict[str, Any],
    *,
    brain_id: str | None = None,
    session_id: str | None = None,
    event_id: str | None = None,
    appended_at: str,
) -> dict[str, Any]:
    """Build an immutable V2 storage record without mutating the caller payload."""

    if not isinstance(signal, dict) or not signal:
        raise ValueError("brain_feedback_signal_required")
    normalized = dict(signal)
    resolved_brain_id = str(brain_id or normalized.get("brain_id") or "").strip()
    if not resolved_brain_id:
        raise ValueError("brain_feedback_brain_id_required")
    resolved_session_id = str(
        session_id
        if session_id is not None
        else normalized.get("session_id") or normalized.get("search_id") or ""
    ).strip()
    resolved_event_id = str(
        event_id
        or normalized.get("event_id")
        or normalized.get("signal_id")
        or ""
    ).strip()
    if not resolved_event_id:
        raise ValueError("brain_feedback_event_id_required")

    normalized["schema_version"] = str(
        normalized.get("schema_version") or BRAIN_FEEDBACK_SIGNAL_SCHEMA_VERSION
    )
    normalized["brain_id"] = resolved_brain_id
    if resolved_session_id:
        normalized.setdefault("session_id", resolved_session_id)
    content_digest = _canonical_digest(normalized)
    identity = {
        "brain_id": resolved_brain_id,
        "session_id": resolved_session_id,
        "event_id": resolved_event_id,
    }
    return {
        "schema_version": BRAIN_FEEDBACK_STORED_EVENT_SCHEMA_VERSION,
        "signal_schema_version": normalized["schema_version"],
        "ledger_event_id": f"feedback_event::{_canonical_digest(identity)[:32]}",
        **identity,
        "source": str(normalized.get("source") or "generic").strip() or "generic",
        "signal_family": str(normalized.get("signal_family") or "search_session").strip()
        or "search_session",
        "explicit": bool(normalized.get("explicit")),
        "weight": float(normalized.get("weight") or 0.0),
        "signal_created_at": str(normalized.get("created_at") or appended_at),
        "appended_at": str(appended_at),
        "content_digest": content_digest,
        "signal": normalized,
    }


def persisted_feedback_ledger_digest(records: Iterable[dict[str, Any]]) -> str:
    """Digest a ledger snapshot independently from SQLite insertion order."""

    basis = sorted(
        (
            {
                "brain_id": str(row.get("brain_id") or ""),
                "session_id": str(row.get("session_id") or ""),
                "event_id": str(row.get("event_id") or ""),
                "content_digest": str(row.get("content_digest") or ""),
            }
            for row in records
        ),
        key=lambda row: (
            row["brain_id"],
            row["session_id"],
            row["event_id"],
            row["content_digest"],
        ),
    )
    return _canonical_digest(basis)


def _event_kind(event: dict[str, Any]) -> str:
    return _normalized_kind(
        event.get("event_kind")
        or event.get("event_type")
        or event.get("interaction_kind")
        or event.get("feedback_kind")
        or event.get("action")
        or event.get("type")
        or event.get("kind")
    )


def _signal_family(kind: str, event: dict[str, Any], *, source: str) -> str:
    tokens = set(kind.split("_"))
    if source == "correction_history" or "correction" in tokens and tokens & {"applied", "created", "stored"}:
        return "correction_applied"
    if source == "learning_event":
        return "learning_event"
    if any(token == "incorrect" or token.startswith("correct") for token in tokens) or kind == "user_correction":
        return "explicit_correct"
    if "review" in tokens or tokens & {"approve", "approved", "reject", "rejected", "rating"}:
        return "explicit_review"
    if tokens & {"hydrate", "hydrated", "hydration"}:
        return "evidence_hydrated"
    if tokens & {"open", "opened", "view", "viewed", "inspect", "inspected"} and (
        "evidence" in tokens or "document" in tokens or "node" in tokens
    ):
        return "evidence_opened"
    if "fail" in kind or "error" in tokens or tokens & {"failed", "failure"}:
        return "search_failure"
    if "stop" in kind or tokens & {"cancelled", "canceled", "timeout", "superseded"}:
        return "search_stop"
    if source == "learning_event" or "learning" in tokens or kind.endswith("_created"):
        return "learning_event"
    return "search_session"


def _signal_polarity(family: str, kind: str, event: dict[str, Any]) -> str:
    if family in {"explicit_correct", "correction_applied"}:
        return "corrective"
    if family == "search_failure":
        return "negative"
    verdict = _normalized_kind(
        event.get("verdict")
        or event.get("rating")
        or event.get("value")
        or event.get("stop_reason")
        or event.get("status")
        or kind
    )
    words = set(verdict.split("_"))
    if family == "search_stop" and words & {"completed", "contract", "ready", "satisfied", "success"}:
        return "positive"
    if words & _NEGATIVE_WORDS:
        return "negative"
    if words & _POSITIVE_WORDS:
        return "positive"
    return "neutral"


def _string_values(value: Any, *, limit: int = 32) -> list[str]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    output: list[str] = []
    for item in values:
        if isinstance(item, dict):
            candidate = str(item.get("id") or item.get("node_id") or item.get("document_id") or "").strip()
        else:
            candidate = str(item or "").strip()
        if candidate and candidate not in output:
            output.append(candidate[:240])
        if len(output) >= limit:
            break
    return output


def _evidence_refs(event: dict[str, Any]) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, value: Any) -> None:
        for identifier in _string_values(value):
            key = (kind, identifier)
            if key not in seen:
                seen.add(key)
                refs.append({"kind": kind, "id": identifier})

    for kind, keys in _ID_KEYS_BY_KIND.items():
        for key in keys:
            if key in event:
                add(kind, event.get(key))
        plural = f"{kind}_ids"
        if plural in event:
            add(kind, event.get(plural))
    for key in (
        "node_ids",
        "target_node_ids",
        "used_evidence_node_ids",
        "hydrated_node_ids",
        "opened_node_ids",
    ):
        if key in event:
            add("node", event.get(key))
    for key in ("document_refs", "evidence_refs", "sources"):
        for item in _as_list(event.get(key)):
            row = _as_dict(item)
            if row:
                if row.get("document_id") or row.get("document_ref_id"):
                    add("document", row.get("document_id") or row.get("document_ref_id"))
                elif row.get("node_id") or row.get("memory_id"):
                    add("node", row.get("node_id") or row.get("memory_id"))
                elif row.get("id"):
                    add("evidence", row.get("id"))
            elif item:
                add("evidence", item)
    return refs[:64]


def _details(event: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key in (
        "status",
        "stop_reason",
        "failure_reason",
        "reason_code",
        "answerability_state",
        "verdict",
        "rating",
        "correction_mode",
        "recommendation",
    ):
        value = event.get(key)
        if value is not None and str(value).strip():
            output[key] = str(value).strip()[:300]
    return output


def _created_at(event: dict[str, Any], *, fallback: str | None = None) -> str | None:
    value = event.get("created_at") or event.get("timestamp") or event.get("occurred_at") or event.get("updated_at") or fallback
    return str(value).strip() if value else None


def _build_signal(
    event: dict[str, Any],
    *,
    source: str,
    brain_id: str | None,
    search_id: str | None = None,
    fallback_created_at: str | None = None,
    forced_kind: str | None = None,
) -> dict[str, Any]:
    event_view = {**_as_dict(event.get("payload")), **event}
    kind = _normalized_kind(forced_kind or _event_kind(event_view))
    family = _signal_family(kind, event_view, source=source)
    resolved_search_id = str(event_view.get("search_id") or search_id or "").strip() or None
    resolved_brain_id = str(event_view.get("brain_id") or brain_id or "").strip() or None
    evidence_refs = _evidence_refs(event_view)
    explicit = bool(event_view.get("explicit")) or family in _EXPLICIT_FAMILIES
    weight = float(_SIGNAL_WEIGHTS[family])
    identity = {
        "source": source,
        "kind": kind,
        "family": family,
        "brain_id": resolved_brain_id,
        "search_id": resolved_search_id,
        "event_id": event_view.get("event_id") or event_view.get("correction_id") or event_view.get("id"),
        "created_at": _created_at(event_view, fallback=fallback_created_at),
        "evidence_refs": evidence_refs,
        "details": _details(event_view),
    }
    signal_id = f"feedback::{_canonical_digest(identity)[:20]}"
    return {
        "schema_version": BRAIN_FEEDBACK_SIGNAL_SCHEMA_VERSION,
        "signal_id": signal_id,
        "brain_id": resolved_brain_id,
        "search_id": resolved_search_id,
        "source": source,
        "source_schema_version": str(event_view.get("schema_version") or "legacy_or_unversioned"),
        "source_kind": kind,
        "signal_family": family,
        "polarity": _signal_polarity(family, kind, event_view),
        "explicit": explicit,
        "weight": weight,
        "created_at": identity["created_at"],
        "evidence_refs": evidence_refs,
        "details": identity["details"],
    }


def _nested_events(container: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for key in _NESTED_EVENT_KEYS:
        for value in _as_list(container.get(key)):
            if isinstance(value, dict):
                yield dict(value)


def _session_signals(session: dict[str, Any], *, brain_id: str | None) -> list[dict[str, Any]]:
    search_id = str(session.get("search_id") or "").strip() or None
    created_at = _created_at(session)
    status = _normalized_kind(session.get("status") or "unknown")
    signals = [
        _build_signal(
            {**session, "event_type": "search_session"},
            source="search_session",
            brain_id=brain_id,
            search_id=search_id,
            fallback_created_at=created_at,
        )
    ]
    if status in {"failed", "error", "blocked"}:
        signals.append(
            _build_signal(
                {**session, "event_type": "search_failed"},
                source="search_session",
                brain_id=brain_id,
                search_id=search_id,
                fallback_created_at=created_at,
            )
        )
    stop_reason = str(session.get("stop_reason") or "").strip()
    if stop_reason:
        signals.append(
            _build_signal(
                {**session, "event_type": "search_stopped", "stop_reason": stop_reason},
                source="search_session",
                brain_id=brain_id,
                search_id=search_id,
                fallback_created_at=created_at,
            )
        )
    containers = [session, _as_dict(session.get("request")), _as_dict(session.get("plan")), _as_dict(session.get("result"))]
    for container in containers:
        for event in _nested_events(container):
            signals.append(
                _build_signal(
                    event,
                    source="search_event",
                    brain_id=brain_id,
                    search_id=search_id,
                    fallback_created_at=created_at,
                )
            )
    return signals


def _health_rollup(signals: list[dict[str, Any]]) -> dict[str, Any]:
    family_histogram = Counter(str(row.get("signal_family") or "unknown") for row in signals)
    source_histogram = Counter(str(row.get("source") or "unknown") for row in signals)
    polarity_histogram = Counter(str(row.get("polarity") or "neutral") for row in signals)
    explicit_weight = sum(float(row.get("weight") or 0.0) for row in signals if bool(row.get("explicit")))
    observed_weight = sum(float(row.get("weight") or 0.0) for row in signals)
    corrective_weight = sum(
        float(row.get("weight") or 0.0)
        for row in signals
        if str(row.get("polarity") or "") in {"negative", "corrective"}
    )
    evidence_ref_count = sum(len(_as_list(row.get("evidence_refs"))) for row in signals)
    return {
        "schema_version": BRAIN_FEEDBACK_HEALTH_ROLLUP_SCHEMA_VERSION,
        "signal_count": len(signals),
        "explicit_signal_count": sum(1 for row in signals if bool(row.get("explicit"))),
        "observed_signal_count": sum(1 for row in signals if not bool(row.get("explicit"))),
        "family_histogram": dict(sorted(family_histogram.items())),
        "source_histogram": dict(sorted(source_histogram.items())),
        "polarity_histogram": dict(sorted(polarity_histogram.items())),
        "weighted_signal_total": round(observed_weight, 6),
        "explicit_weight_total": round(explicit_weight, 6),
        "corrective_weight_total": round(corrective_weight, 6),
        "evidence_ref_count": evidence_ref_count,
        "priority_policy": {
            "explicit_feedback_has_priority": True,
            "explicit_correct_weight": _SIGNAL_WEIGHTS["explicit_correct"],
            "explicit_review_weight": _SIGNAL_WEIGHTS["explicit_review"],
            "evidence_open_weight": _SIGNAL_WEIGHTS["evidence_opened"],
            "search_session_weight": _SIGNAL_WEIGHTS["search_session"],
        },
        "score": round(max(0.0, 1.0 - min(1.0, corrective_weight / max(1.0, observed_weight))), 6),
        "authoritative_for_health_verdict": False,
    }


def build_brain_feedback_ledger(
    *,
    brain_id: str | None = None,
    search_sessions: list[dict[str, Any]] | None = None,
    search_events: list[dict[str, Any]] | None = None,
    feedback_events: list[dict[str, Any]] | None = None,
    corrections: list[dict[str, Any]] | None = None,
    learning_events: list[dict[str, Any]] | None = None,
    max_signals: int = 2000,
) -> dict[str, Any]:
    """Normalize legacy feedback sources into an additive, read-only V2 ledger."""

    signals: list[dict[str, Any]] = []
    source_schema_versions: set[str] = set()
    for raw_session in _as_list(search_sessions):
        if not isinstance(raw_session, dict):
            continue
        session = dict(raw_session)
        source_schema_versions.add(str(session.get("schema_version") or "legacy_or_unversioned"))
        signals.extend(_session_signals(session, brain_id=brain_id))
    for source, rows in (
        ("search_event", search_events),
        ("feedback_event", feedback_events),
        ("correction_history", corrections),
        ("learning_event", learning_events),
    ):
        for raw_event in _as_list(rows):
            if not isinstance(raw_event, dict):
                continue
            event = dict(raw_event)
            source_schema_versions.add(str(event.get("schema_version") or "legacy_or_unversioned"))
            signals.append(_build_signal(event, source=source, brain_id=brain_id))

    deduplicated: dict[str, dict[str, Any]] = {}
    for signal in signals:
        signal_id = str(signal.get("signal_id") or "")
        if signal_id and signal_id not in deduplicated:
            deduplicated[signal_id] = signal
    ordered = sorted(
        deduplicated.values(),
        key=lambda row: (str(row.get("created_at") or ""), str(row.get("signal_id") or "")),
        reverse=True,
    )[: max(1, int(max_signals))]
    digest_basis = [
        {
            "signal_id": row["signal_id"],
            "family": row["signal_family"],
            "weight": row["weight"],
            "evidence_refs": row["evidence_refs"],
        }
        for row in ordered
    ]
    rollup = _health_rollup(ordered)
    return {
        "schema_version": BRAIN_FEEDBACK_LEDGER_SCHEMA_VERSION,
        "signal_schema_version": BRAIN_FEEDBACK_SIGNAL_SCHEMA_VERSION,
        "brain_id": str(brain_id).strip() if brain_id else None,
        "signal_count": len(ordered),
        "signals": ordered,
        "health_rollup": rollup,
        "ledger_digest": _canonical_digest(digest_basis),
        "migration": {
            "mode": "additive_projection",
            "source_records_mutated": False,
            "legacy_inputs_supported": True,
            "source_schema_versions": sorted(source_schema_versions),
        },
        "safety_contract": {
            "read_only": True,
            "append_or_projection_only": True,
            "hidden_mutation_allowed": False,
        },
    }


__all__ = [
    "BRAIN_FEEDBACK_HEALTH_ROLLUP_SCHEMA_VERSION",
    "BRAIN_FEEDBACK_LEDGER_PAGE_SCHEMA_VERSION",
    "BRAIN_FEEDBACK_LEDGER_SCHEMA_VERSION",
    "BRAIN_FEEDBACK_SIGNAL_SCHEMA_VERSION",
    "BRAIN_FEEDBACK_STORED_EVENT_SCHEMA_VERSION",
    "build_brain_feedback_ledger",
    "canonical_feedback_digest",
    "persisted_feedback_ledger_digest",
    "prepare_feedback_signal_for_storage",
]
