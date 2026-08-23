# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import re
import unicodedata
from typing import Any


SOURCE_TRUST_VALUES = {
    "verified_public",
    "user_asserted",
    "uploaded_document",
    "synthetic_test",
    "inferred",
    "system_metadata",
}

CLAIM_STATUS_VALUES = {
    "fact",
    "hypothesis",
    "source_metadata",
    "instruction",
    "test_artifact",
}

ANSWER_BLOCKING_SOURCE_TRUST = {"synthetic_test", "system_metadata"}
ANSWER_BLOCKING_CLAIM_STATUS = {"source_metadata", "instruction", "test_artifact"}

_SYNTHETIC_MARKERS = (
    "synthetic operating dossier",
    "synthetic dossier",
    "synthetic source",
    "stress-testing memory",
    "stress testing memory",
    "stress-test memory",
    "stress test memory",
    "reality-inspired dossier",
    "reality inspired dossier",
    "not a new public source",
    "not a public source",
    "composed from the public source set",
    "derived from public facts for stress-testing",
    "expected retrieval behavior",
    "test document ingestion",
    "for stress-testing memory creation",
)

_SOURCE_METADATA_PREFIXES = (
    "source",
    "public source",
    "source pack",
    "source url",
    "document title",
    "document source",
    "retrieved from",
    "reference url",
)

_INSTRUCTION_MARKERS = (
    "should be represented",
    "should answer",
    "expected to",
    "do not treat",
    "do not use",
    "retrieval behavior",
    "answer should",
)

_PUBLIC_SOURCE_MARKERS = (
    "http://",
    "https://",
    "linkedin.com",
    "press release",
    "public source",
    "official website",
    "official site",
    "website",
    "web",
    "news",
    "newsroom",
    "press-release",
    "corporation",
)

_UPLOADED_SOURCE_MARKERS = (
    "document",
    "uploaded",
    "upload",
    "pdf",
    "docx",
    "file",
)


def _fold_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_only = "".join(char for char in normalized if not unicodedata.combining(char))
    sanitized = re.sub(r"[^\w\s:/.-]", " ", ascii_only.lower())
    return " ".join(sanitized.strip().split())


def _has_marker(value: str, markers: tuple[str, ...]) -> bool:
    folded = _fold_text(value)
    return any(marker in folded for marker in markers)


def _normalized_source_trust(value: Any) -> str | None:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in SOURCE_TRUST_VALUES else None


def _normalized_claim_status(value: Any) -> str | None:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in CLAIM_STATUS_VALUES else None


def looks_like_system_metadata_source(*values: Any) -> bool:
    folded_source = _fold_text(" ".join(str(value or "") for value in values))
    return "system_pattern" in folded_source or folded_source.startswith(
        ("system", "runtime_navigation_store", "maintenance_pattern")
    )


def looks_like_synthetic_artifact(*values: Any) -> bool:
    return _has_marker(" ".join(str(value or "") for value in values), _SYNTHETIC_MARKERS)


def looks_like_source_metadata_text(text: Any) -> bool:
    raw_text = str(text or "")
    folded = _fold_text(text)
    if not folded:
        return False
    if folded in {"manual text", "manual_text", "document", "public source"}:
        return True
    short_line = len(folded) <= 240 and "\n" not in str(text or "")
    if folded.startswith(_SOURCE_METADATA_PREFIXES) and short_line:
        after_first_sentence = raw_text.split(".", 1)[1] if "." in raw_text else ""
        folded_remainder = _fold_text(after_first_sentence)
        event_terms = (
            "became",
            "acquired",
            "announced",
            "continued",
            "founded",
            "launched",
            "joined",
            "ha acquisito",
            "ha fondato",
            "e diventata",
        )
        if re.search(r"\b(?:19|20)\d{2}\b", folded_remainder) and any(term in folded_remainder for term in event_terms):
            return False
        return True
    if short_line and any(marker in folded for marker in ("http://", "https://", "source:", "url:", "document title")):
        return True
    return False


def looks_like_instruction_text(text: Any) -> bool:
    folded = _fold_text(text)
    if not folded:
        return False
    directive_markers = tuple(marker for marker in _INSTRUCTION_MARKERS if marker != "expected to")
    if any(marker in folded for marker in directive_markers):
        return True
    if "expected to" not in folded:
        return False
    directive_context = (
        "agent",
        "answer",
        "context",
        "memory",
        "retrieval",
        "tool",
        "use",
        "user",
    )
    return len(folded) <= 360 and any(term in folded for term in directive_context)


def infer_source_trust(
    *,
    raw_text: Any,
    input_mode: str | None = None,
    provenance_mode: Any = None,
    source_label: Any = None,
    source_type: Any = None,
    explicit_source_trust: Any = None,
) -> str:
    combined = " ".join(str(value or "") for value in (raw_text, provenance_mode, source_label, source_type))
    if looks_like_synthetic_artifact(combined):
        return "synthetic_test"
    if looks_like_system_metadata_source(provenance_mode, source_label, source_type):
        return "system_metadata"
    explicit = _normalized_source_trust(explicit_source_trust)
    if explicit:
        return explicit
    folded_source = _fold_text(f"{source_label or ''} {source_type or ''} {provenance_mode or ''}")
    if _has_marker(folded_source, _PUBLIC_SOURCE_MARKERS):
        return "verified_public"
    if _has_marker(folded_source, _UPLOADED_SOURCE_MARKERS) or str(input_mode or "").strip().lower() == "document":
        return "uploaded_document"
    return "user_asserted"


def infer_claim_status(
    *,
    raw_text: Any,
    source_trust: str,
    memory_type: Any = None,
    derivation_role: Any = None,
    explicit_claim_status: Any = None,
) -> str:
    if source_trust == "synthetic_test" or looks_like_synthetic_artifact(raw_text):
        return "test_artifact"
    explicit = _normalized_claim_status(explicit_claim_status)
    if explicit:
        return explicit
    if looks_like_instruction_text(raw_text):
        return "instruction"
    if looks_like_source_metadata_text(raw_text):
        return "source_metadata"
    if source_trust == "system_metadata":
        return "source_metadata"
    memory_type_folded = _fold_text(memory_type)
    if source_trust == "inferred" or memory_type_folded in {"hypothesis", "inference"}:
        return "hypothesis"
    return "fact"


def eligibility_for(
    *,
    source_trust: str,
    claim_status: str,
    memory_type: Any = None,
    document_role: Any = None,
    is_document_anchor: bool = False,
) -> dict[str, bool]:
    memory_type_folded = _fold_text(memory_type)
    role_folded = _fold_text(document_role)
    answer_eligible = (
        source_trust not in ANSWER_BLOCKING_SOURCE_TRUST
        and claim_status not in ANSWER_BLOCKING_CLAIM_STATUS
    )
    profile_eligible = (
        answer_eligible
        and claim_status == "fact"
        and not is_document_anchor
        and memory_type_folded not in {"document_anchor", "document_summary", "document_chunk"}
        and role_folded != "anchor"
    )
    document_eligible = (
        source_trust not in ANSWER_BLOCKING_SOURCE_TRUST
        and claim_status not in {"instruction", "test_artifact"}
        and source_trust != "system_metadata"
    )
    return {
        "answer_eligible": bool(answer_eligible),
        "profile_eligible": bool(profile_eligible),
        "document_eligible": bool(document_eligible),
    }


def build_hygiene_metadata(
    *,
    raw_text: Any,
    input_mode: str | None = None,
    provenance: dict[str, Any] | None = None,
    provenance_mode: Any = None,
    source_label: Any = None,
    source_type: Any = None,
    explicit_source_trust: Any = None,
    explicit_claim_status: Any = None,
    memory_type: Any = None,
    derivation_role: Any = None,
    document_role: Any = None,
    is_document_anchor: bool = False,
) -> dict[str, Any]:
    provenance_payload = dict(provenance or {})
    resolved_source_label = source_label if source_label is not None else provenance_payload.get("source_label")
    resolved_source_type = source_type if source_type is not None else provenance_payload.get("source_type")
    resolved_provenance_mode = provenance_mode if provenance_mode is not None else provenance_payload.get("mode")
    source_trust = infer_source_trust(
        raw_text=raw_text,
        input_mode=input_mode,
        provenance_mode=resolved_provenance_mode,
        source_label=resolved_source_label,
        source_type=resolved_source_type,
        explicit_source_trust=explicit_source_trust,
    )
    claim_status = infer_claim_status(
        raw_text=raw_text,
        source_trust=source_trust,
        memory_type=memory_type,
        derivation_role=derivation_role,
        explicit_claim_status=explicit_claim_status,
    )
    eligibility = eligibility_for(
        source_trust=source_trust,
        claim_status=claim_status,
        memory_type=memory_type,
        document_role=document_role,
        is_document_anchor=is_document_anchor,
    )
    return {
        "source_trust": source_trust,
        "claim_status": claim_status,
        **eligibility,
    }


def apply_hygiene_metadata(node: dict[str, Any], *, input_mode: str | None = None) -> dict[str, Any]:
    payload = dict(node)
    provenance = dict(payload.get("provenance") or {})
    hygiene = build_hygiene_metadata(
        raw_text=payload.get("raw_text") or payload.get("summary") or "",
        input_mode=input_mode or ("document" if payload.get("is_document_anchor") else "auto"),
        provenance=provenance,
        explicit_source_trust=payload.get("source_trust"),
        explicit_claim_status=payload.get("claim_status"),
        memory_type=payload.get("memory_type"),
        derivation_role=payload.get("derivation_role"),
        document_role=payload.get("document_role"),
        is_document_anchor=bool(payload.get("is_document_anchor")),
    )
    payload.update(hygiene)
    return payload


def _payload_node(payload: dict[str, Any]) -> dict[str, Any]:
    node = payload.get("node")
    return dict(node) if isinstance(node, dict) else dict(payload)


def effective_hygiene(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return build_hygiene_metadata(raw_text="")
    node = _payload_node(payload)
    merged = {**node, **{key: value for key, value in payload.items() if key != "node"}}
    source_trust = _normalized_source_trust(merged.get("source_trust"))
    claim_status = _normalized_claim_status(merged.get("claim_status"))
    raw_for_check = merged.get("raw_text") or merged.get("evidence_snippet") or merged.get("summary") or ""
    has_explicit_hygiene = bool(
        source_trust
        and claim_status
        and all(key in merged for key in ("answer_eligible", "profile_eligible", "document_eligible"))
    )
    metadata_override_required = (
        looks_like_synthetic_artifact(raw_for_check, merged.get("source_label"), merged.get("source_type"))
        or (
            not has_explicit_hygiene
            and looks_like_system_metadata_source(
                (merged.get("provenance") or {}).get("mode") if isinstance(merged.get("provenance"), dict) else None,
                merged.get("source_label"),
                merged.get("source_type"),
            )
        )
        or looks_like_source_metadata_text(raw_for_check)
        or looks_like_instruction_text(raw_for_check)
    )
    if source_trust and claim_status and all(key in merged for key in ("answer_eligible", "profile_eligible", "document_eligible")):
        if not metadata_override_required:
            return {
                "source_trust": source_trust,
                "claim_status": claim_status,
                "answer_eligible": bool(merged.get("answer_eligible")),
                "profile_eligible": bool(merged.get("profile_eligible")),
                "document_eligible": bool(merged.get("document_eligible")),
            }
    return build_hygiene_metadata(
        raw_text=raw_for_check,
        input_mode="document" if bool(merged.get("is_document_anchor")) else "auto",
        provenance=dict(merged.get("provenance") or {}),
        source_label=merged.get("source_label"),
        source_type=merged.get("source_type"),
        explicit_source_trust=None if metadata_override_required else source_trust,
        explicit_claim_status=None if metadata_override_required else claim_status,
        memory_type=merged.get("memory_type"),
        derivation_role=merged.get("derivation_role"),
        document_role=merged.get("document_role"),
        is_document_anchor=bool(merged.get("is_document_anchor")),
    )


def is_answer_eligible(payload: dict[str, Any] | None) -> bool:
    return bool(effective_hygiene(payload).get("answer_eligible"))


def is_profile_eligible(payload: dict[str, Any] | None) -> bool:
    return bool(effective_hygiene(payload).get("profile_eligible"))


def is_document_eligible(payload: dict[str, Any] | None) -> bool:
    return bool(effective_hygiene(payload).get("document_eligible"))
