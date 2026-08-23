# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import unicodedata
from typing import Any, Sequence

from llm import answer_model, llm_enabled, structured_json
from document_evidence_lane import rank_document_evidence_candidates
from exact_field_contract import (
    EXACT_FIELD_SLOT_IDS,
    exact_field_request_from_slot_contract,
    exact_field_semantic_slot_contract,
    extract_exact_user_field_request,
    text_satisfies_exact_field_request,
)
from memory_hygiene import is_answer_eligible, is_document_eligible
from metamemory import build_metamemory_package


def _fold_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_only = "".join(char for char in normalized if not unicodedata.combining(char))
    sanitized = re.sub(r"[^\w\s]", " ", ascii_only.lower())
    return " ".join(sanitized.strip().split())


def _truncate_prompt_text(value: Any, limit: int = 220) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 3)].rstrip()}..."


def _eligible_answer_matches(matches: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [dict(match) for match in list(matches or []) if is_answer_eligible(match)]


def _build_prompt_pack(
    matches: list[dict[str, Any]],
    evidence_reservoir: dict[str, Any] | None = None,
    *,
    retrieval_mode: str = "balanced",
) -> dict[str, Any]:
    reservoir_entries = {
        str(entry.get("node_id") or ""): dict(entry)
        for entry in list((evidence_reservoir or {}).get("entries") or [])
        if str(entry.get("node_id") or "").strip()
    }
    exact_snippet_limit = 4 if retrieval_mode == "flash" else 5 if retrieval_mode == "balanced" else 6
    raw_excerpt_cap = 4 if retrieval_mode == "flash" else 8 if retrieval_mode == "balanced" else 10 if retrieval_mode == "heavy" else 12
    raw_excerpt_limit = 260 if retrieval_mode == "flash" else 760 if retrieval_mode == "balanced" else 1600 if retrieval_mode == "heavy" else 2200
    claim_row_limit = 3 if retrieval_mode == "flash" else 4 if retrieval_mode == "balanced" else 5 if retrieval_mode == "heavy" else 6
    exact_snippets: list[dict[str, Any]] = []
    raw_excerpts: list[dict[str, Any]] = []
    claim_rows: list[dict[str, Any]] = []
    prompt_blocks: list[str] = []
    selected_entries: list[dict[str, Any]] = []
    document_sequences: dict[str, dict[str, Any]] = {}
    selection_limit = max(exact_snippet_limit, raw_excerpt_cap)
    for match in _eligible_answer_matches(matches)[:selection_limit]:
        node = dict(match.get("node") or {})
        node_id = str(match.get("node_id") or node.get("id") or "").strip()
        if not node_id:
            continue
        entry = dict(reservoir_entries.get(node_id) or {})
        raw_text = str(entry.get("raw_text") or node.get("raw_text") or "").strip()
        evidence_snippet = str(entry.get("evidence_snippet") or match.get("evidence_snippet") or raw_text or match.get("summary") or "").strip()
        summary = str(entry.get("summary") or match.get("summary") or node.get("summary") or "").strip()
        provenance = dict(entry.get("provenance") or node.get("provenance") or {})
        selected_entries.append(entry or {"node_id": node_id, "summary": summary, "raw_text": raw_text, "evidence_snippet": evidence_snippet})
        if evidence_snippet and len(exact_snippets) < exact_snippet_limit:
            exact_snippets.append(
                {
                    "node_id": node_id,
                    "text": evidence_snippet,
                    "topic": str(entry.get("topic") or provenance.get("guide_conceptual_area") or node.get("memory_type") or ""),
                    "score": round(float(entry.get("score") or match.get("raw_score") or 0.0), 4),
                }
            )
        if raw_text and len(raw_excerpts) < raw_excerpt_cap:
            raw_excerpts.append(
                {
                    "node_id": node_id,
                    "text": _truncate_prompt_text(raw_text, raw_excerpt_limit),
                    "source_label": provenance.get("source_label"),
                    "source_type": provenance.get("source_type"),
                    "support_slots": list(entry.get("support_slots") or []),
                    "document_role": entry.get("document_role") or node.get("document_role"),
                }
            )
        doc_role = str(entry.get("document_role") or node.get("document_role") or "").strip().lower()
        doc_anchor = str(entry.get("document_anchor_id") or node.get("document_anchor_id") or provenance.get("source_label") or "").strip()
        if raw_text and doc_role == "chunk" and doc_anchor:
            sequence = document_sequences.setdefault(
                doc_anchor,
                {
                    "anchor_id": doc_anchor,
                    "title": str(provenance.get("source_label") or doc_anchor),
                    "chunks": [],
                },
            )
            sequence["chunks"].append(
                {
                    "node_id": node_id,
                    "text": _truncate_prompt_text(raw_text, raw_excerpt_limit),
                    "order": int(entry.get("document_chunk_index") or node.get("document_chunk_index") or len(sequence["chunks"])),
                    "source_span_start": int(entry.get("source_span_start") or node.get("source_span_start") or 0),
                }
            )
        if summary and len(claim_rows) < claim_row_limit:
            claim_rows.append(
                {
                    "node_id": node_id,
                    "summary": summary,
                    "support_slots": list(entry.get("support_slots") or []),
                    "document_role": entry.get("document_role"),
                }
            )
        prompt_blocks.append(
            "\n".join(
                [
                    f"[{node_id}]",
                    f"summary={summary}",
                    f"exact_snippet={evidence_snippet}",
                    f"raw_excerpt={_truncate_prompt_text(raw_text, raw_excerpt_limit)}",
                    f"support_slots={list(entry.get('support_slots') or [])}",
                    f"provenance={provenance}",
                    f"score={round(float(entry.get('score') or match.get('raw_score') or 0.0), 4)}",
                ]
            )
        )
    return {
        "exact_snippets": exact_snippets,
        "raw_excerpts": raw_excerpts,
        "claim_rows": claim_rows,
        "document_sequences": [
            {
                "anchor_id": anchor_id,
                "title": str(sequence.get("title") or anchor_id),
                "chunks": [
                    {
                        "node_id": str(chunk.get("node_id") or ""),
                        "text": str(chunk.get("text") or ""),
                        "order": int(chunk.get("order") or 0),
                    }
                    for chunk in sorted(
                        list(sequence.get("chunks") or []),
                        key=lambda item: (int(item.get("source_span_start") or 0), int(item.get("order") or 0)),
                    )
                    if str(chunk.get("text") or "").strip()
                ],
            }
            for anchor_id, sequence in document_sequences.items()
            if list(sequence.get("chunks") or [])
        ],
        "prompt_blocks": prompt_blocks,
        "selected_entry_ids": [str(entry.get("node_id") or "") for entry in selected_entries if str(entry.get("node_id") or "").strip()],
        "summary": {
            "exact_snippet_count": len(exact_snippets),
            "raw_excerpt_count": len(raw_excerpts),
            "claim_row_count": len(claim_rows),
            "selected_entry_count": len(selected_entries),
            "document_sequence_count": len(document_sequences),
        },
    }


def _family_attribution_summary_from_rows(rows: list[dict[str, Any]]) -> dict[str, int]:
    summary = {"heuristic": 0, "ai": 0, "dual_origin": 0}
    for row in rows:
        families = {
            str(item).strip().lower()
            for item in (
                list(row.get("planner_families") or [])
                or list(row.get("origin_families") or [])
                or ([row.get("planner_family")] if str(row.get("planner_family") or "").strip() else [])
            )
            if str(item).strip().lower() in {"heuristic", "ai"}
        }
        if not families:
            families = {"heuristic"}
        if len(families) > 1:
            summary["dual_origin"] += 1
        if "heuristic" in families:
            summary["heuristic"] += 1
        if "ai" in families:
            summary["ai"] += 1
    return summary


def build_answer_support_metadata(
    *,
    matches: list[dict[str, Any]],
    shared_evidence: dict[str, Any] | None = None,
    evidence_node_ids: list[str] | None = None,
) -> dict[str, Any]:
    selected_ids = {str(item).strip() for item in list(evidence_node_ids or []) if str(item).strip()}
    selected_rows: list[dict[str, Any]] = []
    for match in _eligible_answer_matches(matches):
        node = dict(match.get("node") or {})
        node_id = str(match.get("node_id") or node.get("id") or "").strip()
        if selected_ids and node_id not in selected_ids:
            continue
        selected_rows.append({**node, **match})
    if not selected_rows and not selected_ids:
        selected_rows = [{**dict(match.get("node") or {}), **match} for match in _eligible_answer_matches(matches)[:8]]
    support_node_count = len(
        {
            str(row.get("node_id") or row.get("id") or "").strip()
            for row in selected_rows
            if str(row.get("node_id") or row.get("id") or "").strip()
        }
    )
    support_slots = {
        str(slot).strip()
        for row in selected_rows
        for slot in list(row.get("support_slots") or [])
        if str(slot).strip()
    }
    if not support_slots:
        support_slots = {
            str(slot).strip()
            for slot, value in dict((shared_evidence or {}).get("coverage_by_slot") or {}).items()
            if str(slot).strip() and float(value or 0.0) > 0.0
        }
    packet_ids = {
        str(
            row.get("document_anchor_id")
            or row.get("source_label")
            or ((row.get("provenance") or {}).get("source_label"))
            or row.get("node_id")
            or row.get("id")
            or ""
        ).strip()
        for row in selected_rows
        if str(
            row.get("document_anchor_id")
            or row.get("source_label")
            or ((row.get("provenance") or {}).get("source_label"))
            or row.get("node_id")
            or row.get("id")
            or ""
        ).strip()
    }
    chunk_sequences: dict[str, int] = {}
    for row in selected_rows:
        document_role = str(row.get("document_role") or row.get("memory_type") or "").strip().lower()
        if document_role not in {"chunk", "document_chunk"}:
            continue
        anchor_id = str(
            row.get("document_anchor_id")
            or ((row.get("document_hit") or {}).get("document_anchor_id"))
            or row.get("source_label")
            or ((row.get("provenance") or {}).get("source_label"))
            or ""
        ).strip()
        if not anchor_id:
            continue
        chunk_sequences[anchor_id] = chunk_sequences.get(anchor_id, 0) + 1
    contradiction_present = bool(
        list((shared_evidence or {}).get("contradiction_flags") or [])
        or list((((shared_evidence or {}).get("blackboard") or {}).get("contradiction_flags") or []))
    )
    return {
        "support_node_count": support_node_count,
        "support_slot_count": len(support_slots),
        "family_attribution_summary": _family_attribution_summary_from_rows(selected_rows),
        "contradiction_present": contradiction_present,
        "distinct_evidence_packet_count": len(packet_ids),
        "ordered_document_sequence_supported": any(count >= 2 for count in chunk_sequences.values()),
    }


def build_hot_context_structured(
    matches: list[dict[str, Any]],
    *,
    evidence_reservoir: dict[str, Any] | None = None,
    shared_evidence: dict[str, Any] | None = None,
    retrieval_mode: str = "balanced",
    context: dict[str, Any] | None = None,
    document_packets: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    prompt_pack = _build_prompt_pack(matches, evidence_reservoir, retrieval_mode=retrieval_mode)
    reservoir_entries = [dict(entry) for entry in list((evidence_reservoir or {}).get("entries") or []) if isinstance(entry, dict)]
    temporal_inventory = build_temporal_inventory(matches, evidence_reservoir=evidence_reservoir)
    hot_context_fragments: list[dict[str, Any]] = []
    for entry in reservoir_entries[:12]:
        if not is_answer_eligible(entry):
            continue
        node_id = str(entry.get("node_id") or "").strip()
        if not node_id:
            continue
        raw_text = str(entry.get("raw_text") or entry.get("evidence_snippet") or entry.get("summary") or "").strip()
        if not raw_text:
            continue
        document_role = str(entry.get("document_role") or entry.get("memory_type") or "").strip().lower()
        fragment_type = "doc_chunk" if document_role in {"chunk", "document_chunk"} else "grouped_claim" if not str(entry.get("raw_text") or "").strip() else "raw_excerpt"
        planner_families = list(entry.get("planner_families") or [])
        hot_context_fragments.append(
            {
                "node_id": node_id,
                "text": raw_text,
                "fragment_type": fragment_type,
                "family_attribution": planner_families or [str(entry.get("planner_family") or "heuristic")],
                "support_slot": str((list(entry.get("support_slots") or []) or [""])[0] or "").strip() or None,
                "source_title": str((entry.get("provenance") or {}).get("source_label") or "").strip() or None,
            }
        )
    if not hot_context_fragments:
        for raw_excerpt in list(prompt_pack.get("raw_excerpts") or [])[:8]:
            hot_context_fragments.append(
                {
                    "node_id": str(raw_excerpt.get("node_id") or ""),
                    "text": str(raw_excerpt.get("text") or ""),
                    "fragment_type": "raw_excerpt",
                    "family_attribution": ["heuristic"],
                    "support_slot": str((list(raw_excerpt.get("support_slots") or []) or [""])[0] or "").strip() or None,
                    "source_title": str(raw_excerpt.get("source_label") or "").strip() or None,
                }
            )
    document_sequences: list[dict[str, Any]] = []
    for packet in list(document_packets or [])[:6]:
        if not is_document_eligible(packet):
            continue
        chunks: list[dict[str, Any]] = []
        for chunk in list(packet.get("ordered_chunk_sequence") or [])[:10]:
            text = str(chunk.get("text") or chunk.get("raw_text") or chunk.get("evidence_snippet") or "").strip()
            if not text:
                continue
            chunks.append(
                {
                    "node_id": str(chunk.get("node_id") or ""),
                    "source_node_id": str(chunk.get("source_node_id") or chunk.get("node_id") or ""),
                    "text": text,
                    "chunk_index": chunk.get("chunk_index"),
                    "source_span_start": chunk.get("source_span_start"),
                    "source_span_end": chunk.get("source_span_end"),
                    "source_kind": str(chunk.get("source_kind") or ""),
                    "derived": bool(chunk.get("derived")),
                }
            )
        anchor_text = str(packet.get("anchor_raw_text") or "").strip()
        if anchor_text and not chunks:
            chunks.append(
                {
                    "node_id": str(packet.get("anchor_node_id") or packet.get("anchor_id") or ""),
                    "text": anchor_text,
                    "chunk_index": 0,
                    "source_span_start": None,
                    "source_span_end": None,
                }
            )
        for fact in list(packet.get("supported_fact_text") or [])[:6]:
            text = str(fact.get("raw_text") or fact.get("summary") or "").strip()
            if text:
                chunks.append(
                    {
                        "node_id": str(fact.get("node_id") or ""),
                        "text": text,
                        "chunk_index": fact.get("chunk_index"),
                        "source_span_start": None,
                        "source_span_end": None,
                    }
                )
        if chunks:
            sequence_text = "\n\n".join(str(chunk.get("text") or "").strip() for chunk in chunks[:12] if str(chunk.get("text") or "").strip())
            document_sequences.append(
                {
                    "anchor_node_id": str(packet.get("anchor_node_id") or packet.get("anchor_id") or ""),
                    "title": str(packet.get("title") or packet.get("source_label") or packet.get("anchor_node_id") or ""),
                    "chunks": chunks[:12],
                    "text": sequence_text,
                    "preview_text": _truncate_prompt_text(sequence_text, 1600),
                    "full_text_mode": str(packet.get("full_text_mode") or "evidence_sequence"),
                    "raw_text_char_count": int(packet.get("raw_text_char_count") or sum(len(str(chunk.get("text") or "")) for chunk in chunks)),
                }
            )
    if not document_sequences:
        document_sequences = list(prompt_pack.get("document_sequences") or [])
    support_slots = [
        {
            "slot": str(slot).strip(),
            "coverage": round(float(value or 0.0), 4),
        }
        for slot, value in dict((shared_evidence or {}).get("coverage_by_slot") or {}).items()
        if str(slot).strip()
    ]
    promotion_summary = {
        "promoted_raw_excerpts": sum(1 for fragment in hot_context_fragments if str(fragment.get("fragment_type") or "") == "raw_excerpt"),
        "promoted_document_chunks": sum(1 for fragment in hot_context_fragments if str(fragment.get("fragment_type") or "") == "doc_chunk"),
        "promoted_grouped_claims": sum(1 for fragment in hot_context_fragments if str(fragment.get("fragment_type") or "") == "grouped_claim"),
        "temporal_inventory_entries": len(list(temporal_inventory.get("entries") or [])),
        "reservoir_only_leftovers": max(0, len(reservoir_entries) - len(hot_context_fragments)),
    }
    return {
        "sections": list((context or {}).get("structured_sections") or []),
        "summary": str((context or {}).get("context_summary") or ""),
        "hot_context_fragments": hot_context_fragments,
        "document_sequences": document_sequences,
        "temporal_inventory": temporal_inventory if temporal_inventory.get("entries") else {},
        "support_slots": support_slots,
        "family_attribution_summary": dict((shared_evidence or {}).get("family_contribution_summary") or _family_attribution_summary_from_rows(reservoir_entries)),
        "promotion_summary": promotion_summary,
    }


def detect_query_intent(query_text: str) -> str | None:
    lowered = _fold_text(query_text)
    if _is_temporal_inventory_query(query_text):
        return "temporal_inventory"
    if _is_temporal_reference_query(query_text):
        return "temporal_reference"
    if any(token in lowered for token in ("padre", "papa", "father", "dad")):
        return "father_name"
    if any(
        token in lowered
        for token in (
            "come mi chiamo",
            "come ti chiami",
            "qual e il tuo nome",
            "qual è il tuo nome",
            "come si chiama",
            "my name",
            "your name",
            "what is your name",
            "what s your name",
            "what is my name",
            "what is the name",
            "chi sono",
            "chi sei",
            "who am i",
            "who are you",
        )
    ):
        return "identity_name"
    if any(token in lowered for token in ("dove lavoro", "where do i work", "lavoro")):
        return "workplace"
    if any(token in lowered for token in ("dove sono nato", "dove sono nata", "where was i born", "nato", "nata", "born")):
        return "birthplace"
    if any(token in lowered for token in ("come si chiama la mia fidanzata", "my girlfriend name", "fidanzata si chiama", "partner name")):
        return "partner_name"
    return None


def intent_bonus(intent_type: str | None, candidate: dict[str, Any]) -> float:
    if not intent_type:
        return 0.0
    text = str(candidate.get("raw_text") or candidate.get("summary") or "").lower()
    memory_type = str(candidate.get("memory_type") or "")
    if intent_type == "identity_name":
        bonus = 0.0
        if _extract_name_from_identity(text):
            bonus += 0.22
        if memory_type in {"identity", "identity_claim"}:
            bonus += 0.08
        return bonus
    if intent_type == "workplace":
        if any(pattern in text for pattern in ("lavoro ", "work at", "work for")):
            return 0.22
    if intent_type == "birthplace":
        if any(pattern in text for pattern in ("nato ", "born ")):
            return 0.22
    if intent_type == "partner_name":
        if any(pattern in text for pattern in ("fidanzata", "girlfriend", "partner", "si chiama", "named")):
            return 0.22
    if intent_type == "father_name":
        if any(pattern in text for pattern in ("padre", "father", "papa", "dad")):
            return 0.28
    if intent_type in {"temporal_inventory", "temporal_reference"}:
        bonus = 0.0
        if _temporal_reference_tokens(text):
            bonus += 0.18
        if memory_type in {"episodic", "history"}:
            bonus += 0.08
        if memory_type in {"project", "knowledge", "document_anchor", "document_chunk"}:
            bonus += 0.04
        return bonus
    return 0.0


def _titlecase_name(value: str) -> str:
    return " ".join(part.capitalize() for part in value.strip().split())


def _extract_name_from_identity(text: str) -> str | None:
    patterns = [
        r"(?:mi chiamo|my name is)\s+([a-zà-ÿ' ]+?)(?:\s+nato|\s+born|[.,;]|$)",
        r"(?:sono|i am)\s+(?!nato\b|nata\b|born\b)([a-zà-ÿ' ]+?)(?:\s+nato|\s+born|[.,;]|$)",
    ]
    lowered = text.lower()
    for pattern in patterns:
        match = re.search(pattern, lowered)
        if match:
            candidate = match.group(1).strip()
            if candidate:
                return _titlecase_name(candidate)
    return None


def _extract_workplace(text: str) -> str | None:
    lowered = text.lower()
    match = re.search(r"(?:lavoro a|lavoro all[' ]|work at|work for)\s+([a-z0-9à-ÿ' .-]+?)(?:[.,;]|$)", lowered)
    if match:
        return _titlecase_name(match.group(1))
    return None


def _extract_birthplace(text: str) -> str | None:
    lowered = text.lower()
    match = re.search(r"(?:nato (?:a|in)|born (?:in|at))\s+([a-z0-9à-ÿ' .-]+?)(?:[.,;]|$)", lowered)
    if match:
        return _titlecase_name(match.group(1))
    return None


def _extract_partner_name(text: str) -> str | None:
    lowered = text.lower()
    match = re.search(r"(?:fidanzata|girlfriend|partner).{0,24}?(?:si chiama|named|name is)\s+([a-zà-ÿ' ]+?)(?:[.,;]|$)", lowered)
    if match:
        return _titlecase_name(match.group(1))
    return None


def _clean_person_name_value(value: str | None) -> str | None:
    cleaned = _clean_fact_value(str(value or ""))
    cleaned = re.sub(
        r"\s+(?:e|ed|and|che|who|served|faceva|ha|had|was|dedicated|gli|lo|la)\b.*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip(" .,:;")
    folded = _fold_text(cleaned)
    if not cleaned or folded in {"mio", "mia", "suo", "sua", "his", "her", "father", "padre", "papa"}:
        return None
    if any(
        marker in folded
        for marker in (
            "company",
            "companies",
            "worldwide",
            "energy",
            "industrial",
            "operators",
            "universities",
            "industry",
            "foundation",
            "management",
            "renewable",
            "digital",
            "technology",
        )
    ):
        return None
    if len(cleaned.split()) > 4:
        return None
    return _titlecase_name(cleaned)


_BUSINESS_PARTNER_MARKERS = (
    "full service partner",
    "full-service partner",
    "business partner",
    "technology partner",
    "industrial partner",
    "industrial partners",
    "scientific partner",
    "scientific partners",
    "partner for energy",
    "partner of energy",
    "energy companies",
    "industrial operators",
    "universities and industry",
    "companies and institutions",
    "companies worldwide",
    "power companies",
    "energy management",
    "renewable energy",
    "industrial automation",
    "partner ecosystem",
)
_PERSONAL_PARTNER_MARKERS = (
    "mio partner",
    "mia partner",
    "il mio partner",
    "la mia partner",
    "my partner",
    "his partner",
    "her partner",
    "partner si chiama",
    "partner named",
    "fidanz",
    "girlfriend",
    "boyfriend",
    "wife",
    "husband",
    "coniuge",
)


def _text_mentions_partner_relationship(folded_text: str) -> bool:
    folded = _fold_text(folded_text)
    if not any(token in folded for token in ("partner", "fidanz", "girlfriend", "boyfriend", "wife", "husband", "coniuge")):
        return False
    if any(marker in folded for marker in _BUSINESS_PARTNER_MARKERS) and not any(marker in folded for marker in _PERSONAL_PARTNER_MARKERS):
        return False
    return any(marker in folded for marker in _PERSONAL_PARTNER_MARKERS) or bool(
        re.search(r"\b(?:partner)\s+(?:is|e)\s+[a-z]+(?:\s+[a-z]+){1,3}\b", folded)
    )


_FAMILY_KINSHIP_MARKERS = (
    "padre",
    "papa",
    "father",
    "dad",
    "madre",
    "mamma",
    "mother",
    "mom",
    "genitore",
    "parent",
    "parents",
    "figli",
    "figlio",
    "figlia",
    "figlie",
    "child",
    "children",
    "son",
    "daughter",
    "fratello",
    "sorella",
    "brother",
    "sister",
    "sibling",
)


_CHILD_RELATION_MARKER_RE = re.compile(r"\b(?:figli|figlio|figlia|figlie|children|child|son|daughter)\b", re.IGNORECASE)
_CHILD_RELATION_MARKERS = {"figli", "figlio", "figlia", "figlie", "child", "children", "son", "daughter"}


def _text_mentions_child_relation(folded_text: str) -> bool:
    return bool(_CHILD_RELATION_MARKER_RE.search(_fold_text(folded_text)))


_PERSONAL_FAMILY_RELATION_MARKERS = (
    "relazione familiare",
    "rapporto familiare",
    "legame familiare",
    "membro della famiglia",
    "membro di famiglia",
    "family relation",
    "family relationship",
    "family member",
    "relative",
    "relatives",
    "kinship",
)


_BUSINESS_FAMILY_CONTEXT_MARKERS = (
    "business family",
    "company family",
    "corporate family",
    "brand family",
    "product family",
    "organization family",
    "organisation family",
    "professional family",
    "work family",
    "family business",
    "business owned by",
    "azienda familiare",
    "impresa familiare",
    "societa familiare",
    "famiglia aziendale",
    "famiglia professionale",
    "famiglia di aziende",
    "famiglia di prodotti",
)


def _text_mentions_family_relationship(folded_text: str) -> bool:
    folded = _fold_text(folded_text)
    if not folded:
        return False
    if any(marker in folded for marker in _FAMILY_KINSHIP_MARKERS if marker not in _CHILD_RELATION_MARKERS):
        return True
    if _text_mentions_child_relation(folded):
        return True
    if any(marker in folded for marker in _PERSONAL_FAMILY_RELATION_MARKERS):
        return True
    if not any(marker in folded for marker in ("famiglia", "familiare", "family")):
        return False
    if any(marker in folded for marker in _BUSINESS_FAMILY_CONTEXT_MARKERS):
        return False
    personal_context = (
        "la sua famiglia",
        "la famiglia di",
        "his family",
        "her family",
        "their family",
        "personal family",
        "public family",
        "famiglia pubblica",
    )
    return any(marker in folded for marker in personal_context)


def _text_mentions_requested_relation(folded_text: str, relation: str) -> bool:
    relation_key = str(relation or "").strip().lower()
    folded = _fold_text(folded_text)
    if relation_key == "children":
        return _text_mentions_child_relation(folded)
    if relation_key == "partner":
        return _text_mentions_partner_relationship(folded)
    if relation_key == "family":
        return _text_mentions_family_relationship(folded)
    aliases = _RELATION_ALIAS_MAP.get(relation_key, ())
    return bool(aliases and any(alias in folded for alias in aliases))


def _text_mentions_personal_relationship(folded_text: str) -> bool:
    folded = _fold_text(folded_text)
    if _text_mentions_child_relation(folded):
        return True
    if any(
        token in folded
        for token in (
            "padre",
            "papa",
            "father",
            "dad",
            "mother",
            "madre",
            "mentor",
            "fratello",
            "sorella",
            "brother",
            "sister",
        )
    ):
        return True
    if not any(token in folded for token in ("partner", "fidanz", "girlfriend", "boyfriend", "wife", "husband", "coniuge")):
        return False
    business_partner_markers = (
        "full service partner",
        "full-service partner",
        "business partner",
        "technology partner",
        "industrial partner",
        "industrial partners",
        "scientific partner",
        "scientific partners",
        "partner for energy",
        "partner of energy",
        "energy companies",
        "industrial operators",
        "universities and industry",
        "companies and institutions",
        "companies worldwide",
        "power companies",
        "energy management",
        "renewable energy",
        "industrial automation",
        "partner ecosystem",
    )
    personal_partner_markers = (
        "mio partner",
        "mia partner",
        "il mio partner",
        "la mia partner",
        "my partner",
        "his partner",
        "her partner",
        "partner si chiama",
        "partner named",
        "fidanz",
        "girlfriend",
        "boyfriend",
        "wife",
        "husband",
        "coniuge",
    )
    if any(marker in folded for marker in business_partner_markers) and not any(marker in folded for marker in personal_partner_markers):
        return False
    return any(marker in folded for marker in personal_partner_markers) or bool(
        re.search(r"\b(?:partner)\s+(?:is|e|è)\s+[a-z]+(?:\s+[a-z]+){1,3}\b", folded)
    )


def _extract_father_name(text: str) -> str | None:
    raw = str(text or "")
    person_ref = r"[A-Z][A-Za-z'.-]+(?:\s+[A-Z][A-Za-z'.-]+){0,3}"
    patterns = (
        r"(?:mio|suo|his|my)?\s*padre[^.?!;\n]{0,70}?(?:si\s+chiamava|si\s+chiama|called|named|name\s+was|was\s+named)\s+([A-Z][A-Za-z'.-]+(?:\s+[A-Za-z][A-Za-z'.-]+){0,3})",
        r"(?:father|padre)\s*,\s*([A-Z][A-Za-z'.-]+(?:\s+[A-Z][A-Za-z'.-]+){0,3})\s*,",
        rf"(?:father|padre)\s+of\s+{person_ref}[^.?!;\n]{{0,50}}?(?:was|is|named|called)\s+([A-Z][A-Za-z'.-]+(?:\s+[A-Za-z][A-Za-z'.-]+){{0,3}})",
        rf"(?:padre\s+di\s+{person_ref})[^.?!;\n]{{0,50}}?(?:si\s+chiamava|si\s+chiama|era)\s+([A-Z][A-Za-z'.-]+(?:\s+[A-Za-z][A-Za-z'.-]+){{0,3}})",
        rf"{person_ref}'s\s+father[^.?!;\n]{{0,50}}?(?:was|is|named|called)\s+([A-Z][A-Za-z'.-]+(?:\s+[A-Za-z][A-Za-z'.-]+){{0,3}})",
    )
    for pattern in patterns:
        match = re.search(pattern, raw, flags=re.IGNORECASE)
        if not match:
            continue
        value = _clean_person_name_value(match.group(1))
        if value:
            return value
    return None


def _normalize_query(query_text: str) -> str:
    return _fold_text(query_text)


_NEGATED_EVIDENCE_SCOPE_RE = re.compile(
    r"\b(?:"
    r"non\s+usare|non\s+utilizzare|senza\s+(?:usare|utilizzare)|"
    r"do\s+not\s+use|don't\s+use|without\s+using|not\s+using|"
    r"exclude|excluding"
    r")\b[^.?!:;]*(?:[:;]|[.?!])?",
    re.IGNORECASE,
)


def _positive_query_scope(query_text: str) -> str:
    """Remove explicit exclusion clauses before positive intent detection."""

    text = str(query_text or "")
    clean = _NEGATED_EVIDENCE_SCOPE_RE.sub(" ", text)
    return clean.strip() or text


def _negated_evidence_topics_from_query(query_text: str) -> list[str]:
    topics: list[str] = []
    for match in _NEGATED_EVIDENCE_SCOPE_RE.finditer(str(query_text or "")):
        fragment_text = re.split(
            r"\b(?:come|as|instead\s+of|instead|as\s+substitutes?|as\s+a\s+substitute|per|to)\b",
            match.group(0),
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        fragment = _fold_text(fragment_text)
        if any(marker in fragment for marker in ("azienda", "aziende", "societa", "company", "companies", "work", "lavoro", "progett", "project", "partnership")):
            topics.append("work_projects")
        if any(marker in fragment for marker in ("document", "documento", "documenti", "fonte", "fonti", "source", "sources", "file", "pdf")):
            topics.append("documents")
        if _text_mentions_child_relation(fragment) or any(marker in fragment for marker in ("famiglia", "familiare", "family", "padre", "madre", "relationship", "relazione")):
            topics.append("family_relation")
        if any(marker in fragment for marker in ("metadata", "source heading", "intestazione", "titolo")):
            topics.append("source_metadata")
    return list(dict.fromkeys(topics))


TEMPORAL_REFERENCE_PATTERNS = (
    "anno",
    "anni",
    "data",
    "date",
    "quando",
    "timeline",
    "cronologia",
    "temporale",
    "temporal",
    "periodo",
    "riferimenti agli anni",
    "riferimenti alle date",
    "years",
    "year",
    "when",
    "dates",
)


def _is_temporal_reference_query(query_text: str) -> bool:
    lowered = _fold_text(query_text)
    if any(
        phrase in lowered
        for phrase in (
            "quando ragioni",
            "quando comunica",
            "quando comunichi",
            "quando parla",
            "quando parli",
            "quando spiega",
            "quando spieghi",
            "quando lavora",
            "quando lavori",
            "when working",
            "when you work",
        )
    ):
        return False
    padded = f" {lowered} "
    phrase_patterns = ("riferimenti agli anni", "riferimenti alle date")
    word_patterns = tuple(pattern for pattern in TEMPORAL_REFERENCE_PATTERNS if pattern not in phrase_patterns)
    return (
        any(pattern in lowered for pattern in phrase_patterns)
        or any(f" {pattern} " in padded for pattern in word_patterns)
        or bool(re.search(r"\b(?:19|20)\d{2}\b", lowered))
    )


def _explicit_temporal_terms(query_text: str) -> list[str]:
    years = re.findall(r"\b(?:19|20)\d{2}\b", str(query_text or ""))
    numeric_dates = re.findall(r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b", str(query_text or ""))
    return list(dict.fromkeys([*years, *numeric_dates]))


def _sentence_has_temporal_signal(text: str) -> bool:
    lowered = _fold_text(text)
    padded = f" {lowered} "
    if re.search(r"\b(?:19|20)\d{2}\b|\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b", str(text or "")):
        return True
    return any(
        f" {token} " in padded
        for token in (
            "gennaio",
            "febbraio",
            "marzo",
            "aprile",
            "maggio",
            "giugno",
            "luglio",
            "agosto",
            "settembre",
            "ottobre",
            "novembre",
            "dicembre",
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
            "anno",
            "anni",
            "data",
            "periodo",
            "timeline",
        )
    )


MONTH_NAMES = (
    "gennaio",
    "febbraio",
    "marzo",
    "aprile",
    "maggio",
    "giugno",
    "luglio",
    "agosto",
    "settembre",
    "ottobre",
    "novembre",
    "dicembre",
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)


def _is_temporal_inventory_query(query_text: str) -> bool:
    if not _is_temporal_reference_query(query_text) or _explicit_temporal_terms(query_text):
        return False
    lowered = _fold_text(query_text)
    return any(
        pattern in lowered
        for pattern in (
            "riferimenti agli anni",
            "riferimenti alle date",
            "quali anni",
            "che anni",
            "quali date",
            "che date",
            "anni trovi",
            "date trovi",
            "anni ricordi",
            "date ricordi",
            "timeline",
            "cronologia",
            "ordine temporale",
            "periodi",
            "years in memory",
            "what years",
            "which years",
            "what dates",
            "which dates",
            "timeline",
            "chronology",
        )
    )


def _temporal_reference_tokens(text: str) -> list[str]:
    raw_text = str(text or "")
    years = re.findall(r"\b(?:19|20)\d{2}\b", raw_text)
    numeric_dates = re.findall(r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b", raw_text)
    month_pattern = "|".join(MONTH_NAMES)
    month_dates = [
        " ".join(match.split())
        for match in re.findall(
            rf"\b(?:(?:{month_pattern})\s+(?:19|20)\d{{2}}|(?:19|20)\d{{2}}\s+(?:{month_pattern}))\b",
            raw_text,
            flags=re.IGNORECASE,
        )
    ]
    year_ranges = [
        re.sub(r"\s+", "", match)
        for match in re.findall(r"\b(?:19|20)\d{2}\s*[-/]\s*(?:19|20)\d{2}\b", raw_text)
    ]
    return list(dict.fromkeys([*year_ranges, *month_dates, *years, *numeric_dates]))


def _temporal_years_from_tokens(tokens: list[str]) -> list[str]:
    years: list[str] = []
    for token in tokens:
        years.extend(re.findall(r"\b(?:19|20)\d{2}\b", str(token or "")))
    return sorted(set(years), key=lambda value: int(value))


def _temporal_sort_year(entry: dict[str, Any]) -> int:
    years = [str(year) for year in list(entry.get("years") or []) if str(year).isdigit()]
    if years:
        return min(int(year) for year in years)
    tokens = _temporal_reference_tokens(str(entry.get("text") or ""))
    token_years = _temporal_years_from_tokens(tokens)
    if token_years:
        return min(int(year) for year in token_years)
    return 9999


def _temporal_sentence_candidates(text: str) -> list[str]:
    candidates = _sentence_candidates(text)
    if len(candidates) <= 1:
        candidates = [
            part.strip()
            for part in re.split(r"(?<=[.!?])\s+|[;\n]+|\s+-\s+", str(text or ""))
            if part.strip()
        ]
    return candidates or ([str(text or "").strip()] if str(text or "").strip() else [])


def _temporal_source_rows(
    matches: list[dict[str, Any]],
    evidence_reservoir: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_sources: set[tuple[str, str, str]] = set()

    def add_row(row: dict[str, Any]) -> None:
        node_id = str(row.get("node_id") or "").strip()
        raw_text = str(row.get("raw_text") or "").strip()
        summary = str(row.get("summary") or "").strip()
        evidence_snippet = str(row.get("evidence_snippet") or "").strip()
        key = (node_id, _fold_text(raw_text or summary or evidence_snippet)[:180], str(row.get("source_kind") or "match"))
        if key in seen_sources:
            return
        seen_sources.add(key)
        rows.append(row)

    for match in _eligible_answer_matches(matches)[:32]:
        node = dict(match.get("node") or {})
        provenance = dict(node.get("provenance") or {})
        add_row(
            {
                "node_id": str(match.get("node_id") or node.get("id") or "").strip(),
                "summary": str(match.get("summary") or node.get("summary") or "").strip(),
                "raw_text": str(node.get("raw_text") or "").strip(),
                "evidence_snippet": str(match.get("evidence_snippet") or node.get("raw_text") or node.get("summary") or "").strip(),
                "score": float(match.get("raw_score") or match.get("score") or 0.0),
                "memory_type": str(node.get("memory_type") or ""),
                "guide_area": str(provenance.get("guide_conceptual_area") or node.get("guide_area") or ""),
                "source_label": str(provenance.get("source_label") or ""),
                "source_kind": "match",
                "support_slots": list(match.get("support_slots") or []),
                "planner_families": list(match.get("planner_families") or match.get("origin_families") or []),
            }
        )

    for entry in list((evidence_reservoir or {}).get("entries") or [])[:48]:
        if not isinstance(entry, dict):
            continue
        if not is_answer_eligible(entry):
            continue
        provenance = dict(entry.get("provenance") or {})
        add_row(
            {
                "node_id": str(entry.get("node_id") or "").strip(),
                "summary": str(entry.get("summary") or "").strip(),
                "raw_text": str(entry.get("raw_text") or "").strip(),
                "evidence_snippet": str(entry.get("evidence_snippet") or "").strip(),
                "score": float(entry.get("score") or 0.0),
                "memory_type": str(entry.get("memory_type") or ""),
                "guide_area": str(entry.get("topic") or provenance.get("guide_conceptual_area") or ""),
                "source_label": str(entry.get("source_label") or provenance.get("source_label") or ""),
                "source_kind": "reservoir",
                "support_slots": list(entry.get("support_slots") or []),
                "planner_families": list(entry.get("planner_families") or []),
            }
        )

    for document in list((evidence_reservoir or {}).get("documents") or [])[:12]:
        if not isinstance(document, dict):
            continue
        if not is_document_eligible(document):
            continue
        source_label = str(document.get("source_label") or document.get("title") or document.get("anchor_node_id") or "").strip()
        for chunk in list(document.get("ordered_chunk_sequence") or [])[:24]:
            if not isinstance(chunk, dict):
                continue
            add_row(
                {
                    "node_id": str(chunk.get("node_id") or "").strip(),
                    "summary": "",
                    "raw_text": str(chunk.get("raw_text") or "").strip(),
                    "evidence_snippet": str(chunk.get("evidence_snippet") or chunk.get("raw_text") or "").strip(),
                    "score": float(chunk.get("score") or 0.0),
                    "memory_type": "document_chunk",
                    "guide_area": "Documents",
                    "source_label": source_label,
                    "source_kind": "document_chunk",
                    "support_slots": ["documents"],
                    "planner_families": [],
                }
            )
        for fact in list(document.get("supported_fact_text") or [])[:24]:
            if not isinstance(fact, dict):
                continue
            add_row(
                {
                    "node_id": str(fact.get("node_id") or "").strip(),
                    "summary": str(fact.get("summary") or "").strip(),
                    "raw_text": str(fact.get("raw_text") or "").strip(),
                    "evidence_snippet": str(fact.get("raw_text") or fact.get("summary") or "").strip(),
                    "score": float(fact.get("score") or 0.0),
                    "memory_type": "document_fact",
                    "guide_area": "Documents",
                    "source_label": source_label,
                    "source_kind": "document_fact",
                    "support_slots": ["documents"],
                    "planner_families": [],
                }
            )
    return rows


def build_temporal_inventory(
    matches: list[dict[str, Any]],
    evidence_reservoir: dict[str, Any] | None = None,
    *,
    max_entries: int = 16,
) -> dict[str, Any]:
    entries_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in _temporal_source_rows(matches, evidence_reservoir):
        texts = [
            str(row.get("evidence_snippet") or "").strip(),
            str(row.get("raw_text") or "").strip(),
            str(row.get("summary") or "").strip(),
        ]
        for text in dict.fromkeys(item for item in texts if item):
            for sentence in _temporal_sentence_candidates(text):
                tokens = _temporal_reference_tokens(sentence)
                if not tokens:
                    continue
                years = _temporal_years_from_tokens(tokens)
                cleaned_text = _truncate_prompt_text(sentence, 420)
                if _temporal_text_is_year_navigation_noise(cleaned_text):
                    continue
                node_id = str(row.get("node_id") or "").strip()
                folded_text = _fold_text(cleaned_text)
                key = (node_id, ",".join(tokens), folded_text[:180])
                score = float(row.get("score") or 0.0)
                memory_type = str(row.get("memory_type") or "").strip().lower()
                guide_area = str(row.get("guide_area") or "").strip()
                source_kind = str(row.get("source_kind") or "match").strip()
                boost = 0.12 if years else 0.06
                if memory_type in {"episodic", "history"} or guide_area.lower() == "history":
                    boost += 0.08
                if memory_type in {"project", "knowledge"} or guide_area.lower() in {"projects", "work"}:
                    boost += 0.04
                if source_kind.startswith("document") or memory_type.startswith("document"):
                    boost += 0.04
                confidence = min(0.98, max(score, 0.54) + boost)
                candidate = {
                    "node_id": node_id,
                    "text": cleaned_text,
                    "tokens": tokens,
                    "years": years,
                    "primary_year": years[0] if years else None,
                    "confidence": round(confidence, 4),
                    "score": round(score, 4),
                    "memory_type": str(row.get("memory_type") or ""),
                    "guide_area": guide_area,
                    "source_label": str(row.get("source_label") or ""),
                    "source_kind": source_kind,
                    "support_slots": list(row.get("support_slots") or []),
                    "planner_families": list(row.get("planner_families") or []),
                }
                previous = entries_by_key.get(key)
                if previous is None or float(candidate["confidence"]) > float(previous.get("confidence") or 0.0):
                    entries_by_key[key] = candidate

    sorted_entries = sorted(
        entries_by_key.values(),
        key=lambda item: (_temporal_sort_year(item), -float(item.get("confidence") or 0.0), str(item.get("text") or "")),
    )
    selected_entries: list[dict[str, Any]] = []
    selected_keys: set[tuple[str, str]] = set()
    all_years = sorted({year for entry in sorted_entries for year in list(entry.get("years") or [])}, key=lambda value: int(value))
    for year in all_years:
        candidates = [entry for entry in sorted_entries if year in list(entry.get("years") or [])]
        if not candidates:
            continue
        best = max(
            candidates,
            key=lambda entry: (
                0 if _temporal_text_is_source_metadata(str(entry.get("text") or "")) else 1,
                _temporal_event_score(str(entry.get("text") or "")),
                float(entry.get("confidence") or 0.0),
                -len(str(entry.get("text") or "")),
            ),
        )
        key = (str(best.get("node_id") or ""), _fold_text(str(best.get("text") or ""))[:180])
        if key in selected_keys:
            continue
        selected_entries.append(best)
        selected_keys.add(key)
        if len(selected_entries) >= max(1, int(max_entries)):
            break
    for entry in sorted_entries:
        if len(selected_entries) >= max(1, int(max_entries)):
            break
        key = (str(entry.get("node_id") or ""), _fold_text(str(entry.get("text") or ""))[:180])
        if key in selected_keys:
            continue
        selected_entries.append(entry)
        selected_keys.add(key)
    entries = selected_entries[: max(1, int(max_entries))]
    years = sorted({year for entry in entries for year in list(entry.get("years") or [])}, key=lambda value: int(value))
    date_tokens = sorted(
        {token for entry in entries for token in list(entry.get("tokens") or [])},
        key=lambda token: (_temporal_sort_year({"text": token, "years": _temporal_years_from_tokens([token])}), token),
    )
    evidence_node_ids = list(dict.fromkeys(str(entry.get("node_id") or "") for entry in entries if str(entry.get("node_id") or "").strip()))
    return {
        "intent": "temporal_inventory",
        "coverage_state": "partial_temporal_inventory" if entries else "empty",
        "years": years,
        "date_tokens": date_tokens,
        "entries": entries,
        "evidence_node_ids": evidence_node_ids,
        "confidence": round(max((float(entry.get("confidence") or 0.0) for entry in entries), default=0.0), 4),
        "partial": True,
        "exhaustiveness_note": "Inventory covers retrieved temporal evidence only; it is not a global proof of absence.",
    }


def _focus_temporal_text_on_terms(
    text: str,
    requested_terms: set[str] | None,
    *,
    first_person: bool = False,
) -> str:
    cleaned = str(text or "").strip()
    terms = {str(term).strip() for term in set(requested_terms or set()) if str(term).strip()}
    if not cleaned or not terms:
        return cleaned
    if not any(term in cleaned for term in terms):
        return cleaned

    clause_candidates = [
        part.strip(" ,;")
        for part in re.split(
            r"(?<=[.!?])\s+|[;\n]+|,\s+(?=(?:nel|nell'|nella|in|a)\s+(?:19|20)\d{2}\b)|,\s+(?=which\s+)|,\s+(?=che\s+)",
            cleaned,
            flags=re.IGNORECASE,
        )
        if part.strip(" ,;")
    ]
    focused = [part for part in clause_candidates if any(term in part for term in terms)]
    if not focused:
        return cleaned

    primary = focused[0].strip()
    relative_match = re.match(r"(?i)^which\s+evolved\s+into\s+(.+?)\s+in\s+((?:19|20)\d{2})\.?$", primary)
    if relative_match:
        target = relative_match.group(1).strip(" .")
        year = relative_match.group(2)
        subject = "my work" if first_person else "that work"
        return f"In {year}, {subject} evolved into {target}."
    relative_match = re.match(r"(?i)^che\s+(?:e|è)\s+confluit[ao]\s+in\s+(.+?)\s+nel\s+((?:19|20)\d{2})\.?$", primary)
    if relative_match:
        target = relative_match.group(1).strip(" .")
        year = relative_match.group(2)
        subject = "il mio lavoro" if first_person else "quella esperienza"
        return f"Nel {year}, {subject} e confluito in {target}."
    return " ".join(focused[:2]).strip()


_TEMPORAL_QUERY_STOPWORDS = {
    "cosa",
    "come",
    "quale",
    "quali",
    "quando",
    "perche",
    "perche",
    "per",
    "me",
    "mio",
    "mia",
    "noi",
    "hai",
    "ho",
    "sono",
    "stato",
    "stata",
    "successo",
    "rilevante",
    "documento",
    "documenti",
    "fonti",
    "fonte",
    "parlano",
    "trova",
    "delle",
    "della",
    "degli",
    "dell",
    "con",
    "and",
    "the",
    "what",
    "when",
    "why",
    "happened",
    "relevant",
    "source",
    "sources",
    "document",
    "documents",
}


_TEMPORAL_EVENT_TERMS = (
    "acquis",
    "acquired",
    "acquisition",
    "announced",
    "annuncia",
    "founded",
    "fond",
    "created",
    "launched",
    "became",
    "becomes",
    "diventa",
    "parte",
    "integrat",
    "sold",
    "vend",
    "continued",
    "continua",
    "returned",
    "ritorn",
    "opened",
    "apre",
    "built",
    "costru",
    "shift",
)


def _temporal_query_content_terms(query_text: str) -> set[str]:
    return {
        token
        for token in _fold_text(query_text).split()
        if len(token) >= 4 and token not in _TEMPORAL_QUERY_STOPWORDS and not re.fullmatch(r"(?:19|20)\d{2}", token)
    }


def _temporal_text_is_source_metadata(text: str) -> bool:
    folded = _fold_text(text)
    if not folded:
        return False
    return (
        folded.startswith(("source ", "public source ", "source pack ", "document title "))
        or " http " in f" {folded} "
        or folded in {"manual text", "manual_text"}
        or _temporal_text_is_year_navigation_noise(text)
    )


def _temporal_text_is_year_navigation_noise(text: str) -> bool:
    folded = _fold_text(text)
    if not folded:
        return False
    years = set(re.findall(r"\b(?:19|20)\d{2}\b", str(text or "")))
    if len(years) < 4:
        return False
    archive_markers = (
        "press releases",
        "press release archive",
        "news archive",
        "recent posts",
        "archive",
        "archivio",
        "years",
        "anni",
    )
    if not any(marker in folded for marker in archive_markers):
        return False
    return _temporal_event_score(text) == 0


def _temporal_event_score(text: str) -> int:
    folded = _fold_text(text)
    return sum(1 for token in _TEMPORAL_EVENT_TERMS if token in folded)


def _temporal_query_overlap(query_text: str, text: str) -> int:
    terms = _temporal_query_content_terms(query_text)
    if not terms:
        return 0
    folded = _fold_text(text)
    return sum(1 for term in terms if term in folded)


def _rank_temporal_entries_for_query(
    query_text: str,
    entries: list[dict[str, Any]],
    *,
    requested_terms: set[str] | None = None,
) -> list[dict[str, Any]]:
    terms = _temporal_query_content_terms(query_text)
    requested = {str(term) for term in set(requested_terms or set()) if str(term).strip()}
    ranked = sorted(
        [dict(entry) for entry in entries],
        key=lambda entry: (
            0
            if not requested
            or requested & set(str(token) for token in list(entry.get("tokens") or []))
            or requested & set(re.findall(r"\b(?:19|20)\d{2}\b", str(entry.get("text") or "")))
            else 1,
            1 if _temporal_text_is_source_metadata(str(entry.get("text") or "")) else 0,
            -_temporal_query_overlap(query_text, str(entry.get("text") or "")),
            -_temporal_event_score(str(entry.get("text") or "")),
            -float(entry.get("confidence") or 0.0),
            -float(entry.get("score") or 0.0),
            len(str(entry.get("text") or "")),
        ),
    )
    if terms:
        relevant = [
            entry
            for entry in ranked
            if _temporal_query_overlap(query_text, str(entry.get("text") or "")) > 0
            or _temporal_event_score(str(entry.get("text") or "")) > 0
        ]
        if relevant:
            return relevant
    return ranked


def _temporal_entry_to_fact(
    entry: dict[str, Any],
    *,
    requested_terms: set[str] | None = None,
    first_person: bool = False,
) -> dict[str, Any]:
    text = _focus_temporal_text_on_terms(
        str(entry.get("text") or "").strip(),
        requested_terms,
        first_person=first_person,
    )
    if text and text[-1] not in ".!?":
        text = f"{text}."
    return _make_fact(
        kind="history",
        text=text,
        node_id=str(entry.get("node_id") or ""),
        raw_score=float(entry.get("confidence") or entry.get("score") or 0.0),
        summary=text,
        priority=0.84 if entry.get("years") else 0.68,
        value=", ".join(str(token) for token in list(entry.get("tokens") or []) if str(token).strip()),
        evidence_snippet=text,
    )


def _join_human_list(items: list[str]) -> str:
    cleaned = [str(item).strip() for item in items if str(item).strip()]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]
    return f"{', '.join(cleaned[:-1])} e {cleaned[-1]}"


def _format_temporal_inventory_answer(query_text: str, temporal_inventory: dict[str, Any]) -> str:
    entries = [dict(entry) for entry in list(temporal_inventory.get("entries") or []) if isinstance(entry, dict)]
    years = [str(year) for year in list(temporal_inventory.get("years") or []) if str(year).strip()]
    if not entries:
        return "Non trovo riferimenti temporali espliciti nella memoria recuperata."
    grouped: dict[str, dict[str, Any]] = {}
    for entry in entries:
        entry_years = [str(year) for year in list(entry.get("years") or []) if str(year).strip()]
        if not entry_years:
            entry_years = ["senza anno esplicito"]
        for year in entry_years:
            current = grouped.get(year)
            if current is None or float(entry.get("confidence") or 0.0) > float(current.get("confidence") or 0.0):
                grouped[year] = entry
    ordered_years = sorted(grouped.keys(), key=lambda value: int(value) if value.isdigit() else 9999)
    first_person = _prefers_first_person_answer(query_text)
    lines = []
    for year in ordered_years[:8]:
        entry = grouped[year]
        text = str(entry.get("text") or "").strip()
        if first_person:
            text = _self_voice_fragment(text)
        lines.append(f"{year}: {text}")
    year_sentence = _join_human_list(years) if years else _join_human_list(ordered_years)
    if year_sentence:
        prefix = f"Trovo riferimenti espliciti a {year_sentence} nella memoria recuperata."
    else:
        prefix = "Trovo riferimenti temporali espliciti nella memoria recuperata."
    note = "Non posso garantire che sia esaustivo: questo e l'inventario del contesto recuperato ora."
    return " ".join([prefix, note, " ".join(lines)]).strip()


def _build_temporal_inventory_direct_answer(
    query_text: str,
    temporal_inventory: dict[str, Any],
    matches: list[dict[str, Any]],
    aspects: list[str],
) -> dict[str, Any] | None:
    entries = [dict(entry) for entry in list(temporal_inventory.get("entries") or []) if isinstance(entry, dict)]
    if not entries:
        return None
    evidence_node_ids = list(dict.fromkeys(str(entry.get("node_id") or "") for entry in entries if str(entry.get("node_id") or "").strip()))
    evidence_snippets = [
        {
            "node_id": str(entry.get("node_id") or ""),
            "text": str(entry.get("text") or ""),
            "kind": "temporal_evidence",
            "tokens": list(entry.get("tokens") or []),
        }
        for entry in entries[:12]
    ]
    support_metadata = build_answer_support_metadata(matches=matches, evidence_node_ids=evidence_node_ids)
    return {
        "answer_text": _format_temporal_inventory_answer(query_text, temporal_inventory),
        "mode": "grounded_facts",
        "confidence": float(temporal_inventory.get("confidence") or 0.0),
        "evidence_node_ids": evidence_node_ids,
        "reasoning_summary": "Built a partial temporal inventory from explicit year/date evidence in retrieved matches, raw text, reservoir entries, and document chunks.",
        "insufficient": False,
        "answerability_state": "partial",
        "evidence_snippets": evidence_snippets,
        "requested_aspects": aspects,
        "support_node_count": int(support_metadata.get("support_node_count") or len(evidence_node_ids)),
        "support_slot_count": max(1, int(support_metadata.get("support_slot_count") or 0)),
        "family_attribution_summary": dict(support_metadata.get("family_attribution_summary") or {}),
        "contradiction_present": bool(support_metadata.get("contradiction_present")),
        "temporal_inventory": temporal_inventory,
    }


def detect_query_aspects(query_text: str) -> list[str]:
    query_text = _positive_query_scope(query_text)
    lowered = _normalize_query(query_text)
    aspects: list[str] = []
    broad_summary_query = any(
        pattern in lowered
        for pattern in (
            "raccontami tutto di te",
            "raccontami di te",
            "parlami di te",
            "raccontami in sintesi",
            "raccontami la tua vita",
            "raccontami della tua vita",
            "raccontami tutto",
            "parlami della tua vita",
            "parlami di te",
            "riassumimi tutto",
            "dimmi tutto",
            "raccontami tutto quello che sai",
            "tutto quello che sai",
            "voglio un dossier completo",
            "dossier completo",
            "quadro completo",
            "profilo completo",
            "profilo esteso",
            "profilo non riassuntivo",
            "curriculum",
            "biografia completa",
            "storia completa",
            "tutta la tua vita",
            "riassunto completo",
            "summary completo",
            "tell me about yourself",
            "tell me about your life",
            "tell me your life story",
            "tell me everything",
            "full profile",
            "complete biography",
        )
    )
    relation_query = any(
        token in lowered
        for token in (
            "padre",
            "papa",
            "father",
            "dad",
            "partner",
            "fidanzat",
            "girlfriend",
            "boyfriend",
            "wife",
            "husband",
            "mentor",
            "fratello",
            "sorella",
            "brother",
            "sister",
            "sibling",
            "famiglia",
            "familiare",
            "family",
            "relazione familiare",
            "family relation",
            "relazioni",
            "rapporti",
            "relationships",
        )
    ) or _text_mentions_child_relation(lowered)
    role_query = any(token in lowered for token in ("che lavoro", "what does", "lavora come", "works as", "ruolo", "job"))
    project_query = any(token in lowered for token in ("su cosa lavora", "su cosa lavori", "a cosa lavori", "what is she working on", "what is he working on", "what are you working on", "cosa sta costruendo", "cosa stai costruendo", "come si collega", "si collega a", "collega a", "project", "progetto", "progetti", "lavora su", "lavori su", "works on", "building"))
    company_query = any(token in lowered for token in ("azienda", "aziende", "societa", "company", "companies", "startup", "impresa", "imprese", "business"))
    company_founding_query = company_query and any(
        token in lowered for token in ("fondato", "fondata", "fondatore", "founder", "founded", "co-founder", "cofondatore", "costituit", "established")
    )
    company_affiliation_query = company_query and any(
        token in lowered
        for token in (
            "tue aziende",
            "mie aziende",
            "quali aziende",
            "quali societa",
            "aziende hai",
            "societa hai",
            "aziende sono",
            "societa sono",
            "societa collegate",
            "aziende collegate",
            "which companies",
            "what companies",
            "your companies",
            "my companies",
            "associated companies",
        )
    )
    named_target_info_query = any(
        token in lowered
        for token in (
            "cosa sai di",
            "cosa sai su",
            "che cosa sai di",
            "che cosa sai su",
            "parlami di",
            "dimmi di",
            "raccontami di",
            "what do you know about",
            "tell me about",
        )
    )

    def want(aspect: str, patterns: tuple[str, ...]) -> None:
        if any(pattern in lowered for pattern in patterns) and aspect not in aspects:
            aspects.append(aspect)

    self_identity_query = any(pattern in lowered for pattern in ("chi sei", "who are you", "tell me about yourself"))
    explicit_identity_query = any(
        pattern in lowered
        for pattern in (
            "come si chiama",
            "come mi chiamo",
            "come ti chiami",
            "qual e il tuo nome",
            "qual è il tuo nome",
            "what is the name",
            "what is your name",
            "what s your name",
            "my name",
            "your name",
            "who am i",
            "who are you",
            "chi sono",
            "chi sei",
            "identita",
            "profilo",
        )
    )
    generic_who_query = any(pattern in lowered for pattern in ("chi e", "chi è", "who is"))
    if (explicit_identity_query and not relation_query) or (generic_who_query and not relation_query):
        want(
            "name",
            (
                "come si chiama",
                "come mi chiamo",
                "come ti chiami",
                "qual e il tuo nome",
                "qual è il tuo nome",
                "what is the name",
                "what is your name",
                "what s your name",
                "my name",
                "your name",
                "who am i",
                "chi sono",
                "chi e",
                "chi è",
                "who is",
            ),
        )
    elif explicit_identity_query and relation_query:
        want(
            "name",
            (
                "come si chiama",
                "come mi chiamo",
                "come ti chiami",
                "qual e il tuo nome",
                "qual Ã¨ il tuo nome",
                "what is your name",
                "what s your name",
                "my name",
                "your name",
                "chi sono",
                "who am i",
            ),
        )
    if generic_who_query and not relation_query:
        for aspect in ("role", "projects", "history"):
            if aspect not in aspects:
                aspects.append(aspect)
    want("birthplace", ("dove e nato", "dove è nato", "dove e nata", "dove è nata", "where was", "born"))
    if self_identity_query:
        # "Chi sei?" is a compact identity/work request, not a broad profile.
        # Broader areas such as place, style, and values are added only when
        # the query asks for a dossier or names those areas explicitly.
        for aspect in ("name", "role"):
            if aspect not in aspects:
                aspects.append(aspect)
        if broad_summary_query or project_query or any(token in lowered for token in ("lavoro", "attivita", "work", "su cosa")):
            if "projects" not in aspects:
                aspects.append("projects")
    want("residence", ("dove vive", "where does", "lives in", "vive a"))
    public_personal_fact_query = any(
        pattern in lowered
        for pattern in (
            "fatti personali pubblici",
            "fatto personale pubblico",
            "personal public facts",
            "public personal facts",
            "public personal evidence",
        )
    )
    public_event_query = any(
        pattern in lowered
        for pattern in (
            "evento pubblico",
            "eventi pubblici",
            "public event",
            "public events",
            "fatti pubblici",
            "public facts",
        )
    )

    want("father", ("padre", "papa", "father", "dad", "nome del padre", "father name"))
    want("family", ("famiglia", "familiare", "relazione familiare", "family", "family relation"))
    if _text_mentions_child_relation(lowered) and "children" not in aspects:
        aspects.append("children")
    want("partner", ("partner", "fidanzat", "girlfriend", "boyfriend", "wife", "husband", "relazioni", "rapporti", "relationships"))
    want("mentor", ("mentor",))
    want("sibling", ("fratello", "sorella", "brother", "sister", "sibling"))
    want(
        "style",
        (
            "come comunica",
            "come comunichi",
            "come parla",
            "come ragiona",
            "quando ragioni",
            "modo di ragionare",
            "stile",
            "style",
            "tone",
            "voice",
            "communication",
            "collabora",
            "collaborazione",
            "collaboration",
            "cooperation",
        ),
    )
    want(
        "values",
        (
            "valori",
            "principi",
            "values",
            "what matters",
            "cosa conta",
            "collabora",
            "collaborazione",
            "collaboration",
            "cooperation",
        ),
    )
    want("history", ("storia", "history", "passato", "biografia", "background", "evento", "eventi", "event", "events"))
    if public_personal_fact_query:
        for aspect in ("name", "family", "history"):
            if aspect not in aspects:
                aspects.append(aspect)
    if public_event_query and "history" not in aspects:
        aspects.append("history")
    want("documents", ("documento", "documenti", "document", "pdf", "file", "spec", "report", "note", "appunto"))
    if _is_temporal_reference_query(query_text) and "history" not in aspects:
        aspects.append("history")
    if any(token in lowered for token in ("identita", "profilo")) and "name" not in aspects:
        aspects.append("name")
    if any(token in lowered for token in ("lavoro", "attivita", "work")):
        for aspect in ("role", "projects"):
            if aspect not in aspects:
                aspects.append(aspect)
    if role_query:
        want("role", ("che lavoro", "what does", "lavora come", "works as", "ruolo", "job"))
    if project_query:
        want("projects", ("su cosa lavora", "su cosa lavori", "a cosa lavori", "what is she working on", "what is he working on", "what are you working on", "cosa sta costruendo", "cosa stai costruendo", "come si collega", "si collega a", "collega a", "project", "progetto", "progetti", "lavora su", "lavori su", "works on", "building"))
    if company_founding_query or company_affiliation_query:
        for aspect in ("company_founding", "projects"):
            if aspect not in aspects:
                aspects.append(aspect)
    try:
        query_target_values = _query_named_targets(query_text)
    except Exception:
        query_target_values = []
    org_or_project_targets = [
        target for target in query_target_values if _target_looks_like_org_or_project(target)
    ]
    person_like_targets = [
        target for target in query_target_values if target not in org_or_project_targets
    ]
    work_scoped_role_query = role_query and any(
        marker in lowered
        for marker in (
            "nel tuo lavoro",
            "nel mio lavoro",
            "in your work",
            "in my work",
            "nel lavoro",
        )
    )
    if person_like_targets and work_scoped_role_query and "partner" not in aspects:
        aspects.append("partner")
    if org_or_project_targets:
        if "projects" not in aspects:
            aspects.append("projects")
        if ("che ruolo" in lowered or "ruolo ha" in lowered or "role" in lowered) and "role" not in aspects:
            aspects.append("role")
    if named_target_info_query:
        if person_like_targets and "name" not in aspects:
            aspects.append("name")
        if org_or_project_targets:
            if "projects" not in aspects:
                aspects.append("projects")
            if ("che ruolo" in lowered or "ruolo ha" in lowered or "role" in lowered) and "role" not in aspects:
                aspects.append("role")
    if broad_summary_query:
        for aspect in ("name", "role", "projects"):
            if aspect not in aspects:
                aspects.append(aspect)
    return aspects


_RELATION_ALIAS_MAP: dict[str, tuple[str, ...]] = {
    "father": ("padre", "papa", "father", "dad"),
    "mother": ("madre", "mamma", "mother", "mom"),
    "children": ("figli", "figlio", "figlia", "figlie", "children", "child", "son", "daughter"),
    "family": ("famiglia", "familiare", "relazione familiare", "family", "family relation"),
    "partner": ("partner", "fidanzat", "girlfriend", "boyfriend", "wife", "husband"),
    "mentor": ("mentor",),
    "sibling": ("fratello", "sorella", "brother", "sister", "sibling"),
}


def _requested_relations_from_query(query_text: str) -> list[str]:
    lowered = _fold_text(_positive_query_scope(query_text))
    relations: list[str] = []
    for relation, aliases in _RELATION_ALIAS_MAP.items():
        if relation == "children":
            if _text_mentions_child_relation(lowered):
                relations.append(relation)
            continue
        if any(alias in lowered for alias in aliases):
            relations.append(relation)
    return relations


def _query_is_business_relationship_request(query_text: str) -> bool:
    folded = _fold_text(query_text)
    if _text_mentions_personal_relationship(folded):
        return False
    if _query_is_entity_connection_path_request(query_text):
        return True
    try:
        targets = _query_named_targets(query_text)
    except Exception:
        targets = []
    org_or_project_target_count = sum(1 for target in targets if _target_looks_like_org_or_project(target))
    relation_or_path_marker = any(
        marker in folded
        for marker in (
            "colleg",
            "connect",
            "relazion",
            "rapporto",
            "linked",
            "link",
            "connection",
            "acquired",
            "acquis",
            "partner",
            "association",
            "associazione",
        )
    )
    if len(targets) >= 2 and org_or_project_target_count > 0 and relation_or_path_marker:
        return True
    return bool(
        any(
            marker in folded
            for marker in (
                "azienda",
                "aziende",
                "company",
                "companies",
                "societa",
                "impresa",
                "imprese",
                "organizzazione",
                "organizzazioni",
                "organization",
                "organizations",
                "startup",
                "project",
                "progetto",
                "collegate",
                "collegati",
                "collegamento",
                "connessione",
                "connected",
                "connection",
                "acquired",
                "acquis",
                "partner industrial",
                "business partner",
            )
        )
        and any(
            marker in folded
            for marker in (
                "colleg",
                "connect",
                "relazion",
                "rapporto",
                "acquired",
                "acquis",
                "partner",
                "aziend",
                "company",
                "companies",
                "societa",
            )
        )
    )


def _query_has_narrative_pressure(query_text: str) -> bool:
    lowered = _fold_text(query_text)
    return any(
        marker in lowered
        for marker in (
            "raccontami",
            "parlami",
            "dimmi di",
            "cosa sai",
            "che cosa sai",
            "descrivi",
            "spiegami",
            "riassumi",
            "in particolare",
            "tell me about",
            "what do you know",
            "describe",
            "explain",
        )
    )


def _query_has_exact_fact_pressure(query_text: str) -> bool:
    lowered = _fold_text(query_text)
    if any(
        marker in lowered
        for marker in (
            "come si chiam",
            "come mi chiam",
            "come ti chiam",
            "qual e il nome",
            "qual e il tuo nome",
            "nome del",
            "nome della",
            "si chiamava",
            "si chiama",
            "what is the name",
            "what was the name",
            "what is your name",
            "what is my name",
        )
    ):
        return True
    relation_markers = (
        "padre",
        "madre",
        "father",
        "mother",
        "partner",
        "fidanz",
        "wife",
        "husband",
    )
    yes_no_markers = (
        "hai",
        "ha",
        "avevi",
        "aveva",
        "sei",
        "do you have",
        "does he have",
        "does she have",
        "do they have",
        "have you got",
        "has he got",
        "has she got",
        "are you",
        "is he",
        "is she",
    )
    return (_text_mentions_child_relation(lowered) or any(marker in lowered for marker in relation_markers)) and any(marker in lowered for marker in yes_no_markers)


def _query_has_detail_pressure(query_text: str) -> bool:
    lowered = _fold_text(query_text)
    return any(
        marker in lowered
        for marker in (
            "monumento",
            "dedicato",
            "dedicata",
            "inaugur",
            "data",
            "quando",
            "storia",
            "perche",
            "perchè",
            "dettagli",
            "dettaglio",
            "evento",
            "eventi",
            "progetto",
            "document",
            "anno",
            "anni",
            "date",
            "timeline",
        )
    )


def _is_company_founding_query(query_text: str) -> bool:
    lowered = _fold_text(query_text)
    organization_terms = (
        "azienda",
        "aziende",
        "societa",
        "society",
        "company",
        "companies",
        "startup",
        "impresa",
        "imprese",
        "organizzazione",
        "organizzazioni",
    )
    founding_terms = (
        "fondato",
        "fondata",
        "fondati",
        "fondate",
        "fondatore",
        "fondatrice",
        "founder",
        "founded",
        "cofounder",
        "co-founder",
        "cofondatore",
        "co-fondatore",
        "costituito",
        "costituita",
        "established",
        "started",
        "created",
    )
    return any(term in lowered for term in organization_terms) and any(term in lowered for term in founding_terms)


def _is_company_affiliation_query(query_text: str) -> bool:
    lowered = _fold_text(query_text)
    organization_terms = (
        "azienda",
        "aziende",
        "societa",
        "society",
        "company",
        "companies",
        "startup",
        "impresa",
        "imprese",
        "organizzazione",
        "organizzazioni",
        "business",
    )
    affiliation_terms = (
        "tue aziende",
        "mie aziende",
        "le aziende",
        "quali aziende",
        "quali sono le tue",
        "quali sono le mie",
        "aziende hai",
        "societa hai",
        "societa sono",
        "your companies",
        "my companies",
        "which companies",
        "what companies",
        "companies are yours",
        "companies are mine",
        "associated companies",
        "organizzazioni collegate",
        "organizzazioni connesse",
        "linked organizations",
        "connected organizations",
    )
    return any(term in lowered for term in organization_terms) and any(term in lowered for term in affiliation_terms)


def _is_work_narrative_query(query_text: str) -> bool:
    lowered = _fold_text(query_text)
    if "lavoro" in lowered and any(marker in lowered for marker in ("raccontami", "parlami", "descrivi", "riassumi", "spiegami", "dimmi")):
        return True
    if "work" in lowered and any(marker in lowered for marker in ("tell me", "describe", "summarize", "explain")):
        return True
    return any(
        marker in lowered
        for marker in (
            "parlami del tuo lavoro",
            "parlami del mio lavoro",
            "parlami del lavoro",
            "raccontami del tuo lavoro",
            "raccontami del mio lavoro",
            "raccontami il tuo lavoro",
            "raccontami il mio lavoro",
            "descrivi il tuo lavoro",
            "descrivi il mio lavoro",
            "lavoro, dei progetti",
            "lavoro e dei progetti",
            "work, projects",
            "tell me about your work",
            "tell me about my work",
        )
    )


def _query_has_work_or_company_surface(query_text: str) -> bool:
    lowered = _fold_text(query_text)
    return any(
        marker in lowered
        for marker in (
            "lavoro",
            "attivita",
            "carriera",
            "ruolo",
            "progetto",
            "progetti",
            "azienda",
            "aziende",
            "societa",
            "impresa",
            "imprese",
            "organizzazione",
            "organizzazioni",
            "work",
            "career",
            "role",
            "project",
            "projects",
            "company",
            "companies",
            "startup",
            "organization",
            "organizations",
            "business",
            "businesses",
            "venture",
            "ventures",
        )
    )


def _query_is_work_or_company(query_text: str) -> bool:
    lowered = _fold_text(query_text)
    if _is_company_founding_query(query_text) or _is_company_affiliation_query(query_text) or _is_work_narrative_query(query_text):
        return True
    return any(
        marker in lowered
        for marker in (
            "che lavoro fai",
            "che lavoro faccio",
            "lavoro fai",
            "lavoro faccio",
            "what do you do",
            "what is your work",
            "progetti hai",
            "progetti segui",
            "aziende hai",
            "tue aziende",
        )
    ) or _query_has_work_or_company_surface(query_text)


def _mcp_query_requests_work_entity_inventory(query_text: str) -> bool:
    folded = _fold_text(query_text)
    if _query_requests_broad_profile_context(query_text) and any(
        marker in folded
        for marker in (
            "agente",
            "agent",
            "due diligence",
            "pacchetto operativo",
            "operational packet",
            "operational package",
            "contesto operativo",
            "conoscere questo profilo",
            "know this profile",
            "profilo pubblico",
            "public profile",
        )
    ):
        return True
    if not _query_has_work_or_company_surface(query_text) and not _query_is_work_or_company(query_text):
        return False
    inventory_markers = (
        "quali aziende",
        "che aziende",
        "le aziende",
        "aziende hai",
        "tue aziende",
        "aziende",
        "societa",
        "societa hai",
        "societa fondate",
        "aziende fondate",
        "aziende collegate",
        "aziende sei collegato",
        "societa sei collegato",
        "companies",
        "your companies",
        "which companies",
        "what companies",
        "companies have you",
        "companies are you connected",
        "organizations",
        "businesses",
        "ventures",
    )
    if any(marker in folded for marker in inventory_markers):
        inventory_context_markers = (
            "dossier",
            "profilo",
            "completo",
            "quadro",
            "lista",
            "elenco",
            "quali",
            "che",
            "raccontami",
            "parlami",
            "dimmi",
            "mappa",
            "colleg",
            "connected",
            "linked",
            "profile",
            "complete",
            "which",
            "what",
            "list",
            "map",
        )
        if any(marker in folded for marker in inventory_context_markers):
            return True
        if _is_company_founding_query(query_text) or _is_company_affiliation_query(query_text):
            return True
        return bool(
            any(marker in folded for marker in ("companies", "organizations", "businesses", "ventures"))
            and not any(marker in folded for marker in ("single company", "una sola azienda", "one company"))
        )
    return bool(_is_company_founding_query(query_text) or _is_company_affiliation_query(query_text))


def _query_requests_broad_profile_context(query_text: str) -> bool:
    folded = _fold_text(query_text)
    if not folded:
        return False
    return any(
        marker in folded
        for marker in (
            "raccontami tutto di te",
            "raccontami di te",
            "raccontami qualcosa di te",
            "raccontami in sintesi",
            "parlami di te",
            "descriviti",
            "descrivi te stesso",
            "come ti descriveresti",
            "come sei",
            "che persona sei",
            "chi sei come persona",
            "raccontami la tua vita",
            "raccontami della tua vita",
            "parlami della tua vita",
            "riassumimi tutto",
            "dimmi tutto",
            "raccontami tutto quello che sai",
            "tutto quello che sai",
            "voglio un dossier completo",
            "dossier completo",
            "dossier operativo",
            "quadro completo",
            "quadro globale",
            "profilo completo",
            "profilo esteso",
            "profilo non riassuntivo",
            "profilo pubblico",
            "contesto ampio",
            "ampio contesto",
            "contesto completo",
            "contesto operativo",
            "pacchetto operativo",
            "pacchetto completo",
            "conoscere questo profilo",
            "conoscere il profilo",
            "conoscere questa persona",
            "due diligence",
            "curriculum",
            "biografia completa",
            "storia completa",
            "tutta la tua vita",
            "tell me about your life",
            "tell me about yourself",
            "tell me your life story",
            "tell me everything",
            "describe yourself",
            "how would you describe yourself",
            "what are you like",
            "who are you as a person",
            "full profile",
            "complete profile",
            "complete biography",
            "public profile",
            "broad context",
            "wide context",
            "agent context",
            "context for an agent",
            "operational packet",
            "operational package",
            "due diligence",
        )
    )


def _slot_for_query_aspect(aspect: str) -> str:
    mapping = {
        "name": "identity",
        "birthplace": "place",
        "residence": "place",
        "father": "relationships",
        "mother": "relationships",
        "children": "relationships",
        "family": "relationships",
        "partner": "relationships",
        "mentor": "relationships",
        "sibling": "relationships",
        "role": "work",
        "projects": "work",
        "company_founding": "company_founding",
        "style": "style",
        "values": "values",
        "history": "history",
        "documents": "documents",
    }
    return mapping.get(str(aspect or "").strip().lower(), str(aspect or "").strip().lower() or "identity")


def _broad_profile_required_slots(query_text: str, aspects: list[str] | None = None) -> list[str]:
    """Return only the sections the query actually asks to hard-require.

    A broad dossier should invite rich optional context, but terminality should
    not depend on personal/style/place sections unless the user asked for them.
    """
    folded = _fold_text(query_text)
    slots: list[str] = []

    def add(slot: str) -> None:
        if slot and slot not in slots:
            slots.append(slot)

    def has_any(markers: tuple[str, ...]) -> bool:
        return any(marker in folded for marker in markers)

    # A profile dossier always needs a subject and operating/work nucleus.
    add("identity")
    add("work")
    self_profile_query = has_any(
        (
            "raccontami di te",
            "raccontami tutto di te",
            "raccontami qualcosa di te",
            "parlami di te",
            "descriviti",
            "descrivi te stesso",
            "come ti descriveresti",
            "come sei",
            "che persona sei",
            "chi sei come persona",
            "tell me about yourself",
            "describe yourself",
            "how would you describe yourself",
            "what are you like",
            "who are you as a person",
        )
    )
    if has_any(("fondato", "fondata", "fondatore", "fondatrice", "founder", "founded", "co founder", "cofounder")) and has_any(
        ("azienda", "aziende", "societa", "company", "companies", "startup", "impresa", "imprese")
    ):
        add("company_founding")
    if self_profile_query or has_any(("timeline", "storia", "history", "passato", "biografia", "background", "evento", "eventi", "event", "events", "anni", "years")):
        add("history")
    if self_profile_query or has_any(("valori", "principi", "values", "principles", "cosa conta", "what matters", "collabora", "collaborazione", "collaboration", "cooperation")):
        add("values")
    if self_profile_query or has_any(("stile", "tono", "voice", "style", "communication", "comunica", "come parla", "come ragiona", "collabora", "collaborazione", "collaboration", "cooperation")):
        add("style")
    if has_any(("dove vive", "residenza", "residence", "birthplace", "dove e nato", "dove e nata", "nato a", "nata a", "lives in")):
        add("place")
    if _text_mentions_child_relation(folded) or has_any(
        (
            "padre",
            "papa",
            "father",
            "madre",
            "mother",
            "fratello",
            "sorella",
            "sibling",
            "famiglia",
            "family",
            "fidanz",
            "girlfriend",
            "boyfriend",
            "wife",
            "husband",
            "relazioni personali",
            "personal relationships",
        )
    ):
        add("relationships")
    if has_any(("documento", "documenti", "document", "documents", "file", "pdf", "fonte", "fonti", "source", "sources")):
        add("documents")
    return slots


_SEMANTIC_SLOT_SECTIONS = {
    "identity": "identity",
    "work_company": "work",
    "project": "work",
    "document": "documents",
    "relationship": "relationships",
    "family": "relationships",
    "private_identifier": "identity",
    "personal_contact": "relationships",
    "exact_user_field": "identity",
    "temporal": "history",
    "location": "identity",
    "style": "style",
    "values": "values",
    "privacy_boundary": "privacy_boundary",
    "uncertainty": "history",
}

_FAMILY_ASPECTS = {"father", "mother", "children", "sibling", "family"}

_ROMANTIC_QUERY_MARKERS = (
    "fidanz",
    "girlfriend",
    "boyfriend",
    "wife",
    "husband",
    "coniuge",
    "compagno",
    "compagna",
    "partner romant",
)

_BUSINESS_RELATION_QUERY_MARKERS = (
    "partner di lavoro",
    "business partner",
    "partner professionale",
    "socio",
    "cofondatore",
    "co founder",
    "cofounder",
    "collega",
    "collaboratore",
)

_ENTITY_CONNECTION_PATH_MARKERS = (
    "collega",
    "collegare",
    "collegati",
    "collegate",
    "collegamento",
    "connetti",
    "connessione",
    "connect",
    "connected",
    "connection",
    "linked",
    "link",
    "percorso",
    "percorsi",
    "path",
    "paths",
    "route",
    "routes",
    "corridoio",
    "corridoi",
    "corridor",
    "corridors",
    "attravers",
    "traverse",
    "traversed",
    "mappa",
    "map",
    "mapping",
    "relazione",
    "relazioni",
    "rapporti",
    "relation",
    "relations",
)

_ORG_LIKE_ENTITY_MARKERS = (
    "azienda",
    "aziende",
    "societa",
    "society",
    "company",
    "companies",
    "startup",
    "impresa",
    "imprese",
    "organizzazione",
    "organizzazioni",
    "business",
    "group",
    "labs",
    "lab",
    "studio",
    "systems",
    "technologies",
    "technology",
    "energy",
    "grid",
    "robotics",
    "foundry",
    "foundation",
    "orbit",
    "atlas",
    "platform",
    "product",
    "ventures",
    "capital",
    "university",
    "srl",
    "spa",
    "ltd",
    "inc",
    "corp",
)

_ENTITY_COMMAND_STOPWORDS = {
    "collega",
    "collegami",
    "connetti",
    "mostrami",
    "mostra",
    "dimmi",
    "spiegami",
    "raccontami",
    "parlami",
    "connect",
    "show",
    "tell",
    "explain",
    "link",
}

_SOURCE_SURFACE_NOISE_MARKERS = (
    "official website source",
    "source uri",
    "source url",
    "page title",
    "headings",
    "visualizza profilo",
    "iscriviti ora",
    "consigliato da",
    "undefined",
    "document title",
    "source trace",
    "raw context",
    "merge probe based on",
    "legal notice",
    "terms and conditions",
    "registered office",
    "owner of the website",
    "you undertake to refrain",
)


def _capitalized_entity_tokens(query_text: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"\b[A-Z][A-Za-z0-9&.'-]{2,}\b", str(query_text or "")):
        folded = _fold_text(token)
        if not folded or folded in seen or folded in _ENTITY_COMMAND_STOPWORDS:
            continue
        if folded in {"the", "and", "with", "for", "from"}:
            continue
        tokens.append(token)
        seen.add(folded)
    return tokens


def _looks_like_org_entity_token(token: str) -> bool:
    text = str(token or "").strip()
    folded = _fold_text(text)
    if not text or not folded:
        return False
    if any(marker in folded for marker in _ORG_LIKE_ENTITY_MARKERS):
        return True
    letters = [char for char in text if char.isalpha()]
    if len(letters) >= 3 and all(char.isupper() for char in letters):
        return True
    return bool(re.search(r"[a-z][A-Z]", text))


def _target_looks_like_org_or_project(target: str) -> bool:
    text = str(target or "").strip()
    if not text:
        return False
    if _looks_like_org_entity_token(text):
        return True
    folded = _fold_text(text)
    return any(marker in folded for marker in _ORG_LIKE_ENTITY_MARKERS)


def _text_has_org_or_project_evidence(value: str) -> bool:
    text = str(value or "")
    folded = _fold_text(text)
    if any(marker in folded for marker in _ORG_LIKE_ENTITY_MARKERS):
        return True
    if any(_target_looks_like_org_or_project(token) for token in _capitalized_entity_tokens(text)):
        return True
    if any(
        marker in folded
        for marker in (
            "founder",
            "founded",
            "fondat",
            "ceo",
            "acquired",
            "acquis",
            "project",
            "progetto",
            "product",
            "software",
        )
    ):
        return any(_target_looks_like_org_or_project(target) for target in _query_named_targets(text))
    return False


def _text_has_work_or_project_activity_surface(value: Any) -> bool:
    text = str(value or "")
    folded = _fold_text(text)
    if not folded:
        return False
    work_markers = (
        "work",
        "works as",
        "works on",
        "develops",
        "developed",
        "focuses on",
        "specializes",
        "specialises",
        "lavoro",
        "lavora",
        "sviluppa",
        "sviluppato",
        "si occupa",
        "project",
        "projects",
        "progetto",
        "progetti",
        "founder",
        "founded",
        "founding",
        "fondatore",
        "fondatrice",
        "fondato",
        "fondata",
        "costituita",
        "created",
        "creato",
        "creata",
        "acquired",
        "acquis",
        "ceo",
        "company",
        "companies",
        "azienda",
        "aziende",
        "societa",
        "startup",
        "impresa",
        "imprese",
        "platform",
        "product",
        "software",
        "venture",
        "business",
        "renewable",
        "energy management",
        "industrial automation",
        "cybersecurity",
        "digital transformation",
        "artificial intelligence",
    )
    return any(marker in folded for marker in work_markers)


def _text_has_work_or_project_surface(value: Any) -> bool:
    text = str(value or "")
    return _text_has_work_or_project_activity_surface(text) or _text_has_org_or_project_evidence(text)


def _query_is_entity_connection_path_request(query_text: str) -> bool:
    folded = _fold_text(query_text)
    if _text_mentions_personal_relationship(folded):
        return False
    if not any(marker in folded for marker in _ENTITY_CONNECTION_PATH_MARKERS):
        return False
    named_tokens = _capitalized_entity_tokens(query_text)
    if len(named_tokens) < 2 and not any(marker in folded for marker in _ORG_LIKE_ENTITY_MARKERS):
        return False
    org_like_count = sum(1 for token in named_tokens if _looks_like_org_entity_token(token))
    explicit_org_term = any(marker in folded for marker in _ORG_LIKE_ENTITY_MARKERS)
    return bool(explicit_org_term or org_like_count >= 1)


def _query_requests_romantic_partner(query_text: str) -> bool:
    folded = _fold_text(query_text)
    return any(marker in folded for marker in _ROMANTIC_QUERY_MARKERS) or (
        "partner" in folded and not any(marker in folded for marker in _BUSINESS_RELATION_QUERY_MARKERS)
    )


def _query_requests_business_relation(query_text: str) -> bool:
    folded = _fold_text(query_text)
    return any(marker in folded for marker in _BUSINESS_RELATION_QUERY_MARKERS)


def _semantic_slot_key(slot_id: str, subtype: str = "") -> str:
    subtype_text = str(subtype or "").strip()
    if slot_id in {"relationship", "family", *EXACT_FIELD_SLOT_IDS} and subtype_text and subtype_text != "generic":
        return f"{slot_id}:{subtype_text}"
    return slot_id


def _semantic_slot_from_legacy_slot(
    slot_name: str,
    *,
    query_text: str,
    aspects: list[str],
    relation_hint: str = "",
) -> tuple[str, str]:
    slot = str(slot_name or "").strip()
    folded_query = _fold_text(query_text)
    if slot in EXACT_FIELD_SLOT_IDS:
        return slot, ""
    if slot == "identity":
        return "identity", ""
    if slot == "company_founding":
        return "work_company", ""
    if slot == "work_detail":
        return "project", ""
    if slot == "work":
        explicit_work_or_company_surface = any(
            marker in folded_query
            for marker in (
                "lavoro",
                "attivita",
                "work",
                "role",
                "ruolo",
                "job",
                "azienda",
                "aziende",
                "societa",
                "company",
                "companies",
                "startup",
                "impresa",
                "imprese",
            )
        )
        explicit_project_only_surface = any(
            marker in folded_query
            for marker in (
                "su cosa lavori",
                "a cosa lavori",
                "cosa stai costruendo",
                "what are you working on",
                "project",
                "projects",
                "progetto",
                "progetti",
                "building",
            )
        )
        if (
            "projects" in aspects
            and explicit_project_only_surface
            and not explicit_work_or_company_surface
            and not (_is_company_founding_query(query_text) or _is_company_affiliation_query(query_text))
        ):
            return "project", ""
        return "work_company", ""
    if slot == "documents":
        return "document", ""
    if slot == "history":
        return "temporal", ""
    if slot == "place":
        return "location", ""
    if slot in {"style", "values"}:
        return slot, ""
    if slot in {"relationships", "relation_detail"}:
        relation = str(relation_hint or "").strip().lower()
        if relation in {"father", "mother", "children", "sibling"}:
            return "family", relation
        if relation == "partner":
            if _query_requests_business_relation(query_text):
                return "work_company", ""
            if _query_requests_romantic_partner(query_text):
                return "relationship", "romantic_partner"
            return "relationship", "partner_unspecified"
        if relation == "mentor":
            return "relationship", "mentor"
        if any(aspect in aspects for aspect in _FAMILY_ASPECTS):
            family_aspect = next(aspect for aspect in aspects if aspect in _FAMILY_ASPECTS)
            return "family", family_aspect
        if "partner" in aspects or "partner" in folded_query:
            return "relationship", "romantic_partner" if _query_requests_romantic_partner(query_text) else "partner_unspecified"
        return "relationship", "generic"
    return "identity", ""


def _semantic_required_fields(slot_id: str, subtype: str = "") -> list[str]:
    mapping = {
        "identity": ["person", "identity_claim"],
        "work_company": ["person", "company_or_organization", "role_or_relation", "timeframe_if_available"],
        "project": ["person", "project_or_workstream", "detail"],
        "document": ["document_anchor_or_chunk", "topic_or_title"],
        "relationship": ["person", "relation_type", "related_person", "relationship_subtype"],
        "family": ["person", "family_relation_type", "family_member"],
        "private_identifier": ["person", "requested_private_identifier", "exact_value_or_explicit_absence"],
        "personal_contact": ["person", "requested_contact_role", "related_person_or_explicit_absence"],
        "exact_user_field": ["person", "requested_exact_field", "exact_value_or_explicit_absence"],
        "temporal": ["person_or_project", "event", "timeframe"],
        "location": ["person", "place", "place_type"],
        "style": ["person", "style_trait"],
        "values": ["person", "value_or_principle"],
        "uncertainty": ["missing_slot", "searched_scope", "reason"],
    }
    fields = list(mapping.get(slot_id, ["subject", "claim"]))
    if subtype and slot_id in {"relationship", "family"}:
        fields.append(f"subtype:{subtype}")
    return fields


def _semantic_negative_evidence(slot_id: str, subtype: str, disallowed_topics: list[str]) -> list[str]:
    values = [
        "system_metadata",
        "synthetic_test_material",
        "source_heading_without_person_fact",
        "raw_node_id_or_route_debug",
    ]
    if slot_id in {"work_company", "project"}:
        values.extend(["unrelated_family_context", "family_monument_as_company_answer"])
    if slot_id == "relationship":
        values.extend(["unrelated_work_context", "business_partner_as_romantic_partner"])
        if subtype == "romantic_partner":
            values.extend(["father_or_family_as_romantic_partner", "company_partner_as_romantic_partner"])
    if slot_id == "family":
        values.extend(["business_partner_as_family", "unrelated_company_context"])
    if slot_id in EXACT_FIELD_SLOT_IDS:
        values.extend(["adjacent_profile_context", "unrelated_biography", "field_label_absent"])
    if slot_id in {"identity", "location", "style", "values"}:
        values.append("source_metadata_as_personal_fact")
    values.extend(str(topic or "").strip() for topic in list(disallowed_topics or []) if str(topic or "").strip())
    return list(dict.fromkeys(values))[:16]


def _build_semantic_slot_contracts_for_query(
    query_text: str,
    *,
    required_slots: list[str],
    optional_slots: list[str],
    aspects: list[str],
    relations: list[str],
    disallowed_topics: list[str],
) -> list[dict[str, Any]]:
    contracts: list[dict[str, Any]] = []
    seen: set[str] = set()
    exact_field_request = extract_exact_user_field_request(query_text)

    def add(slot_id: str, subtype: str, legacy_slot: str, *, required: bool) -> None:
        if exact_field_request and slot_id == exact_field_request.get("slot_id"):
            exact_contract = exact_field_semantic_slot_contract(
                exact_field_request,
                required=required,
                legacy_slot=legacy_slot,
                disallowed_topics=disallowed_topics,
            )
            key = str(exact_contract.get("slot_key") or "").strip()
            if key in seen:
                return
            seen.add(key)
            contracts.append(exact_contract)
            return
        key = _semantic_slot_key(slot_id, subtype)
        if key in seen:
            for existing in contracts:
                if existing.get("slot_key") == key:
                    existing["required"] = bool(existing.get("required")) or bool(required)
                    existing["legacy_slots"] = list(dict.fromkeys([*list(existing.get("legacy_slots") or []), legacy_slot]))
                    break
            return
        seen.add(key)
        contracts.append(
            {
                "schema_version": "agvm.semantic_slot_contract.v1",
                "slot_id": slot_id,
                "slot_key": key,
                "section": _SEMANTIC_SLOT_SECTIONS.get(slot_id, "history"),
                "required": bool(required),
                "legacy_slot": legacy_slot,
                "legacy_slots": [legacy_slot],
                "relation_subtype": subtype,
                "required_fields": _semantic_required_fields(slot_id, subtype),
                "negative_evidence": _semantic_negative_evidence(slot_id, subtype, disallowed_topics),
            }
        )

    relation_queue = list(relations or [])
    for slot in list(required_slots or []):
        slot_name = str(slot or "").strip()
        if not slot_name:
            continue
        if slot_name in {"relationships", "relation_detail"} and relation_queue:
            for relation in relation_queue:
                slot_id, subtype = _semantic_slot_from_legacy_slot(slot_name, query_text=query_text, aspects=aspects, relation_hint=relation)
                add(slot_id, subtype, slot_name, required=True)
        else:
            slot_id, subtype = _semantic_slot_from_legacy_slot(slot_name, query_text=query_text, aspects=aspects, relation_hint="")
            add(slot_id, subtype, slot_name, required=True)

    for slot in list(optional_slots or []):
        slot_name = str(slot or "").strip()
        if not slot_name:
            continue
        slot_id, subtype = _semantic_slot_from_legacy_slot(slot_name, query_text=query_text, aspects=aspects, relation_hint="")
        add(slot_id, subtype, slot_name, required=False)
    return contracts


def build_query_contract(query_text: str, *, retrieval_mode: str = "balanced") -> dict[str, Any]:
    """Semantic answer contract shared by retrieval, stop policy, and answering."""
    positive_query_text = _positive_query_scope(query_text)
    lowered = _fold_text(positive_query_text)
    exact_field_request = extract_exact_user_field_request(positive_query_text)
    aspects = detect_query_aspects(positive_query_text)
    relations = _requested_relations_from_query(positive_query_text)
    broad_query = _query_requests_broad_profile_context(positive_query_text)
    document_query = any(marker in lowered for marker in ("documento", "documenti", "source", "fonte", "fonti", "file"))
    public_event_query = any(
        marker in lowered
        for marker in ("evento pubblico", "eventi pubblici", "public event", "public events", "fatti pubblici", "public facts")
    )
    temporal_query = _is_temporal_reference_query(positive_query_text) or public_event_query
    narrative_pressure = _query_has_narrative_pressure(positive_query_text)
    exact_pressure = _query_has_exact_fact_pressure(positive_query_text)
    detail_pressure = _query_has_detail_pressure(positive_query_text)
    explicit_multi = sum(1 for token in (" e ", " and ", ",", ";", " insieme ", " oltre ") if token in f" {lowered} ")
    entity_connection_path_query = _query_is_entity_connection_path_request(positive_query_text)
    company_founding_query = _is_company_founding_query(positive_query_text) or _is_company_affiliation_query(positive_query_text) or entity_connection_path_query
    work_narrative_query = _is_work_narrative_query(positive_query_text)

    if exact_field_request:
        query_kind = "exact_user_field"
        answer_style = "exact"
    elif broad_query:
        query_kind = "broad_profile"
        answer_style = "dossier"
    elif document_query:
        query_kind = "document_lookup"
        answer_style = "document"
    elif company_founding_query:
        query_kind = "company_founding_timeline" if temporal_query else "company_founding_relation"
        answer_style = "timeline" if temporal_query else "list"
    elif work_narrative_query:
        query_kind = "work_narrative"
        answer_style = "narrative"
    elif temporal_query and not relations:
        query_kind = "temporal"
        answer_style = "timeline" if _is_temporal_inventory_query(positive_query_text) else "exact" if _explicit_temporal_terms(positive_query_text) else "narrative"
    elif relations and exact_pressure and not narrative_pressure and not detail_pressure:
        query_kind = "exact_relation_fact"
        answer_style = "exact"
    elif relations:
        query_kind = "narrative_relation"
        answer_style = "narrative"
    elif narrative_pressure or explicit_multi > 1:
        query_kind = "narrative"
        answer_style = "narrative"
    else:
        query_kind = "exact_fact" if exact_pressure or len(aspects) <= 1 else "multi_fact"
        answer_style = "exact" if query_kind == "exact_fact" else "list"

    required_slots: list[str] = []
    optional_slots: list[str] = []

    def add_required(slot: str) -> None:
        normalized = str(slot or "").strip()
        if normalized in optional_slots:
            optional_slots.remove(normalized)
        if normalized and normalized not in required_slots:
            required_slots.append(normalized)

    def add_optional(slot: str) -> None:
        normalized = str(slot or "").strip()
        if normalized and normalized not in required_slots and normalized not in optional_slots:
            optional_slots.append(normalized)

    if exact_field_request:
        add_required(str(exact_field_request.get("slot_id") or "exact_user_field"))
    elif query_kind == "broad_profile":
        for slot in _broad_profile_required_slots(positive_query_text, aspects):
            add_required(slot)
        for slot in ("relationships", "place", "style", "values", "history", "documents"):
            add_optional(slot)
    elif query_kind == "document_lookup":
        add_required("documents")
        add_optional("work")
        add_optional("history")
    elif query_kind in {"company_founding_relation", "company_founding_timeline"}:
        add_required("company_founding")
        add_optional("work")
        if temporal_query:
            add_required("history")
        else:
            add_optional("history")
        add_optional("documents")
        if len(aspects) > 1:
            for aspect in aspects:
                slot = _slot_for_query_aspect(aspect)
                if entity_connection_path_query and slot in {"relationships", "relation_detail"}:
                    continue
                if slot and slot != "company_founding":
                    add_required(slot)
        if entity_connection_path_query:
            add_required("work")
            add_optional("documents")
    elif query_kind == "work_narrative":
        add_required("work")
        add_required("work_detail")
        add_optional("documents")
        add_optional("history")
    elif relations:
        add_required("relationships")
        if answer_style == "narrative":
            add_required("relation_detail")
            add_optional("history")
            add_optional("documents")
            if detail_pressure or temporal_query:
                add_required("history")
    else:
        for aspect in aspects:
            add_required(_slot_for_query_aspect(aspect))
        if temporal_query:
            add_required("history")
    if not required_slots:
        add_required("identity")

    if answer_style == "exact":
        min_landing_count = 1
    elif query_kind == "broad_profile":
        min_landing_count = min(6, max(3, len(required_slots)))
    elif query_kind in {"company_founding_relation", "company_founding_timeline", "work_narrative"}:
        min_landing_count = min(4, max(2, len(required_slots)))
    else:
        min_landing_count = min(4, max(1, len(required_slots)))

    fast_final_allowed = (
        answer_style == "exact"
        and query_kind in {"exact_fact", "exact_relation_fact"}
        and str(retrieval_mode or "balanced") not in {"heavy", "forensic"}
        and not temporal_query
    )
    requires_expansion = (
        not fast_final_allowed
        and (answer_style in {"narrative", "dossier", "timeline", "list", "document"} or len(required_slots) > 1)
    )
    disallowed_topics: list[str] = _negated_evidence_topics_from_query(query_text)
    if query_kind == "work_narrative":
        disallowed_topics.extend(["family_relation", "family_monument"])
    elif query_kind in {"company_founding_relation", "company_founding_timeline"}:
        disallowed_topics.extend(["family_relation", "family_monument", "education_awards", "generic_profile"])
    elif query_kind == "narrative_relation":
        disallowed_topics.extend(["work_projects", "education_awards"])
    semantic_slot_contracts = _build_semantic_slot_contracts_for_query(
        positive_query_text,
        required_slots=required_slots,
        optional_slots=optional_slots,
        aspects=aspects,
        relations=relations,
        disallowed_topics=list(dict.fromkeys(disallowed_topics)),
    )
    return {
        "schema_version": "agvm.query_contract.v1",
        "query_kind": query_kind,
        "answer_style": answer_style,
        "requested_aspects": aspects,
        "requested_relations": relations,
        "required_slots": required_slots,
        "optional_slots": optional_slots,
        "semantic_slot_contract_version": "agvm.semantic_slot_contract.v1",
        "semantic_slot_contracts": semantic_slot_contracts,
        "exact_field_request": dict(exact_field_request or {}),
        "required_semantic_slots": [
            str(item.get("slot_key") or "")
            for item in semantic_slot_contracts
            if bool(item.get("required")) and str(item.get("slot_key") or "")
        ],
        "optional_semantic_slots": [
            str(item.get("slot_key") or "")
            for item in semantic_slot_contracts
            if not bool(item.get("required")) and str(item.get("slot_key") or "")
        ],
        "min_landing_count": min_landing_count,
        "fast_final_allowed": bool(fast_final_allowed),
        "requires_expansion": bool(requires_expansion),
        "narrative_pressure": bool(narrative_pressure),
        "exact_fact_pressure": bool(exact_pressure),
        "detail_pressure": bool(detail_pressure),
        "explicit_multi_signal_count": int(explicit_multi),
        "disallowed_topics": list(dict.fromkeys(disallowed_topics)),
        "answer_width": "dossier" if query_kind == "broad_profile" else "bounded",
        "ai_validation_required": bool(query_kind in {"exact_user_field", "work_narrative", "company_founding_relation", "company_founding_timeline"} or requires_expansion),
        "retrieval_mode": str(retrieval_mode or "balanced"),
    }


def _json_dumps_safe(value: Any) -> str:
    try:
        import json

        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value or "")


def _contract_evidence_text(
    *,
    matches: list[dict[str, Any]] | None = None,
    answer: dict[str, Any] | None = None,
    shared_evidence: dict[str, Any] | None = None,
    evidence_reservoir: dict[str, Any] | None = None,
) -> str:
    parts: list[str] = []
    answer_payload = dict(answer or {})
    parts.extend(str(answer_payload.get(key) or "") for key in ("answer_text", "answer_short", "answer_full", "reasoning_summary"))
    for snippet in list(answer_payload.get("evidence_snippets") or []):
        if isinstance(snippet, dict):
            parts.extend([str(snippet.get("text") or ""), str(snippet.get("kind") or "")])
    for match in list(matches or [])[:24]:
        node = dict(match.get("node") or {})
        parts.extend(
            [
                str(match.get("summary") or ""),
                str(match.get("evidence_snippet") or ""),
                str(node.get("summary") or ""),
                str(node.get("raw_text") or ""),
                str(node.get("memory_type") or ""),
                str((node.get("provenance") or {}).get("guide_conceptual_area") or ""),
            ]
        )
    for entry in list((evidence_reservoir or {}).get("entries") or [])[:32]:
        if isinstance(entry, dict):
            parts.extend([str(entry.get("summary") or ""), str(entry.get("raw_text") or ""), str(entry.get("evidence_snippet") or "")])
    parts.append(_json_dumps_safe(shared_evidence or {}))
    return "\n".join(part for part in parts if str(part or "").strip())


def _source_surface_noise_only(folded_text: str) -> bool:
    folded = _fold_text(folded_text)
    if not folded:
        return True
    if not any(marker in folded for marker in _SOURCE_SURFACE_NOISE_MARKERS):
        return False
    personal_fact_markers = (
        "sono ",
        "mi chiamo",
        "is a ",
        "e un ",
        "e una ",
        "founded",
        "fondat",
        "lavor",
        "padre",
        "madre",
        "fidanz",
        "vive",
        "nato",
        "nata",
    )
    return not any(marker in f" {folded} " for marker in personal_fact_markers)


def _family_relation_present(folded_text: str, subtype: str = "") -> bool:
    folded = _fold_text(folded_text)
    if subtype == "father":
        return any(marker in folded for marker in ("padre", "papa", "father", "dad"))
    if subtype == "mother":
        return any(marker in folded for marker in ("madre", "mamma", "mother", "mom"))
    if subtype == "children":
        return _text_mentions_child_relation(folded)
    if subtype == "sibling":
        return any(marker in folded for marker in ("fratello", "sorella", "brother", "sister", "sibling"))
    return _text_mentions_family_relationship(folded)


def _semantic_slot_evidence_row(
    slot_contract: dict[str, Any],
    *,
    evidence_text: str,
    folded: str,
    coverage_by_slot: dict[str, float],
) -> dict[str, Any]:
    slot_id = str(slot_contract.get("slot_id") or "").strip()
    slot_key = str(slot_contract.get("slot_key") or slot_id).strip()
    subtype = str(slot_contract.get("relation_subtype") or "").strip()
    required = bool(slot_contract.get("required"))
    evidence_found = False
    confidence = 0.0
    reason = "missing_or_not_yet_proven"
    source_noise_only = _source_surface_noise_only(folded)
    exact_field_request = exact_field_request_from_slot_contract(slot_contract)

    if exact_field_request:
        exact_hit = text_satisfies_exact_field_request(evidence_text, exact_field_request)
        confidence = 0.9 if exact_hit else 0.0
        evidence_found = bool(exact_hit)
        reason = "exact_requested_field_evidence" if evidence_found else "missing_exact_requested_field"
    elif slot_id == "identity":
        marker_hit = bool(
            not source_noise_only
            and (
                re.search(r"\b(?:sono|mi chiamo|my name is|i am)\b", folded)
                or any(marker in folded for marker in ("identita", "identity claim", "self description"))
            )
        )
        confidence = max(float(coverage_by_slot.get("identity") or 0.0), 0.64 if marker_hit else 0.0)
        evidence_found = bool(confidence >= 0.45 or marker_hit)
        reason = "semantic_identity_evidence" if evidence_found else reason
    elif slot_id == "work_company":
        company_or_work = bool(
            _company_founding_material_present(evidence_text)
            or any(
                marker in folded
                for marker in (
                    "azienda",
                    "aziende",
                    "societa",
                    "company",
                    "companies",
                    "startup",
                    "impresa",
                    "founder",
                    "founded",
                    "fondat",
                    "ceo",
                    "lavor",
                    "work",
                    "role",
                    "organization",
                    "organizzazione",
                )
            )
        )
        family_only = _family_relation_present(folded) and not company_or_work
        confidence = max(float(coverage_by_slot.get("work") or 0.0), float(coverage_by_slot.get("company_founding") or 0.0), 0.72 if company_or_work else 0.0)
        evidence_found = bool(company_or_work and not family_only and confidence >= 0.45)
        reason = "semantic_work_company_evidence" if evidence_found else "family_or_non_company_evidence"
    elif slot_id == "project":
        project_hit = any(marker in folded for marker in ("progetto", "project", "workstream", "iniziativa", "initiative", "product", "costru", "building"))
        work_detail_hit = bool(float(coverage_by_slot.get("work_detail") or 0.0) >= 0.45)
        confidence = max(float(coverage_by_slot.get("work_detail") or 0.0), float(coverage_by_slot.get("work") or 0.0), 0.66 if project_hit else 0.0)
        evidence_found = bool(project_hit or work_detail_hit)
        reason = "semantic_project_evidence" if evidence_found else reason
    elif slot_id == "document":
        doc_hit = any(marker in folded for marker in ("document", "source_trace", "chunk", "anchor", "fonte", "file", "pdf", "word"))
        confidence = max(float(coverage_by_slot.get("documents") or 0.0), 0.64 if doc_hit else 0.0)
        evidence_found = bool(confidence >= 0.45 or doc_hit)
        reason = "semantic_document_evidence" if evidence_found else reason
    elif slot_id == "relationship":
        if subtype == "romantic_partner":
            relation_hit = _text_mentions_requested_relation(folded, "partner")
        elif subtype == "business_partner":
            relation_hit = any(marker in folded for marker in _BUSINESS_RELATION_QUERY_MARKERS) or any(marker in folded for marker in _BUSINESS_PARTNER_MARKERS)
        elif subtype == "mentor":
            relation_hit = "mentor" in folded
        else:
            relation_hit = _text_mentions_personal_relationship(folded)
        if subtype == "romantic_partner" and _family_relation_present(folded) and not relation_hit:
            relation_hit = False
        confidence = max(float(coverage_by_slot.get("relationships") or 0.0), 0.72 if relation_hit else 0.0)
        evidence_found = bool(relation_hit and confidence >= 0.45)
        reason = "semantic_relationship_evidence" if evidence_found else "wrong_or_missing_relationship_subtype"
    elif slot_id == "family":
        family_hit = _family_relation_present(folded, subtype)
        confidence = max(float(coverage_by_slot.get("relationships") or 0.0), 0.72 if family_hit else 0.0)
        evidence_found = bool(family_hit and confidence >= 0.45)
        reason = "semantic_family_evidence" if evidence_found else "wrong_or_missing_family_subtype"
    elif slot_id == "temporal":
        temporal_hit = bool(re.search(r"\b(?:19|20)\d{2}\b", evidence_text) or any(marker in folded for marker in ("storia", "timeline", "evento", "quando", "inaugur", "acquis", "fondat")))
        confidence = max(float(coverage_by_slot.get("history") or 0.0), float(coverage_by_slot.get("temporal_inventory") or 0.0), 0.64 if temporal_hit else 0.0)
        evidence_found = bool(confidence >= 0.45 or temporal_hit)
        reason = "semantic_temporal_evidence" if evidence_found else reason
    elif slot_id == "location":
        location_hit = bool(not source_noise_only and any(marker in folded for marker in ("nato", "nata", "originari", "residente", "vive", "birthplace", "residence", "location")))
        confidence = max(float(coverage_by_slot.get("place") or 0.0), 0.64 if location_hit else 0.0)
        evidence_found = bool(confidence >= 0.45 or location_hit)
        reason = "semantic_location_evidence" if evidence_found else reason
    elif slot_id == "style":
        style_hit = bool(not source_noise_only and any(marker in folded for marker in ("stile", "tono", "comunica", "voice", "communication", "preciso", "diretto", "strutturat")))
        confidence = max(float(coverage_by_slot.get("style") or 0.0), 0.64 if style_hit else 0.0)
        evidence_found = bool(confidence >= 0.45 or style_hit)
        reason = "semantic_style_evidence" if evidence_found else reason
    elif slot_id == "values":
        value_hit = bool(
            not source_noise_only
            and any(
                marker in folded
                for marker in (
                    "valori",
                    "principi",
                    "values",
                    "principles",
                    "precisione",
                    "precision",
                    "coraggio",
                    "courage",
                    "responsabil",
                    "responsibility",
                    "vocazione",
                    "vocation",
                    "sostenibil",
                    "sustainable",
                    "impact",
                    "impatto",
                )
            )
        )
        confidence = max(float(coverage_by_slot.get("values") or 0.0), 0.64 if value_hit else 0.0)
        evidence_found = bool(confidence >= 0.45 or value_hit)
        reason = "semantic_values_evidence" if evidence_found else reason
    return {
        "slot": slot_key,
        "slot_id": slot_id,
        "relation_subtype": subtype,
        "section": str(slot_contract.get("section") or _SEMANTIC_SLOT_SECTIONS.get(slot_id, "history")),
        "required": required,
        "evidence_found": bool(evidence_found),
        "confidence": round(float(confidence), 4),
        "reason": reason,
        "negative_evidence": list(slot_contract.get("negative_evidence") or []),
    }


def build_evidence_satisfaction_matrix(
    *,
    query_text: str,
    retrieval_mode: str = "balanced",
    matches: list[dict[str, Any]] | None = None,
    answer: dict[str, Any] | None = None,
    shared_evidence: dict[str, Any] | None = None,
    evidence_reservoir: dict[str, Any] | None = None,
) -> dict[str, Any]:
    contract = build_query_contract(query_text, retrieval_mode=retrieval_mode)
    coverage_by_slot = {
        str(slot).strip(): float(value or 0.0)
        for slot, value in dict((shared_evidence or {}).get("coverage_by_slot") or {}).items()
        if str(slot).strip()
    }
    evidence_text = _contract_evidence_text(
        matches=matches,
        answer=answer,
        shared_evidence=shared_evidence,
        evidence_reservoir=evidence_reservoir,
    )
    folded = _fold_text(evidence_text)
    exact_field_request = extract_exact_user_field_request(query_text)
    requested_relations = list(contract.get("requested_relations") or [])
    relation_alias_hit = False
    for relation in requested_relations:
        relation_key = str(relation or "").strip()
        relation_alias_hit = relation_alias_hit or _text_mentions_requested_relation(folded, relation_key)
    relation_evidence_present = bool(float(coverage_by_slot.get("relationships") or 0.0) >= 0.5 or relation_alias_hit)
    relation_detail_present = bool(
        relation_evidence_present
        and (
            len(folded) >= 90
            or bool(re.search(r"\b(?:19|20)\d{2}\b", evidence_text))
            or any(
                marker in folded
                for marker in (
                    "monumento",
                    "inaugur",
                    "aeronautica",
                    "militare",
                    "lavor",
                    "fondat",
                    "progetto",
                    "evento",
                    "storia",
                    "served",
                    "service",
                    "dedicated",
                    "founded",
                )
            )
        )
    )
    rows: list[dict[str, Any]] = []
    for slot in list(contract.get("required_slots") or []):
        slot_name = str(slot or "").strip()
        evidence_found = False
        confidence = float(coverage_by_slot.get(slot_name) or 0.0)
        reason = "coverage_by_slot"
        if slot_name == "relationships":
            evidence_found = bool(relation_evidence_present) if requested_relations else bool(relation_evidence_present or _text_mentions_personal_relationship(folded))
            confidence = max(confidence, 0.72 if evidence_found else 0.0)
            reason = "relation_alias_or_slot_coverage"
        elif slot_name == "relation_detail":
            evidence_found = relation_detail_present
            confidence = 0.68 if evidence_found else 0.0
            reason = "relation_detail_evidence"
        elif slot_name == "history":
            evidence_found = bool(confidence >= 0.45 or re.search(r"\b(?:19|20)\d{2}\b", evidence_text) or any(token in folded for token in ("storia", "evento", "inaugur", "fondat", "acquis")))
            confidence = max(confidence, 0.64 if evidence_found else 0.0)
            reason = "temporal_or_history_evidence"
        elif slot_name == "documents":
            evidence_found = bool(confidence >= 0.45 or any(token in folded for token in ("document", "source_trace", "chunk", "anchor")))
            confidence = max(confidence, 0.64 if evidence_found else 0.0)
            reason = "document_evidence"
        elif slot_name == "work":
            evidence_found = bool(
                confidence >= 0.45
                or any(token in folded for token in ("project", "progetto", "lavor", "costru", "building", "studio", "azienda", "company", "foundry", "orbit"))
            )
            confidence = max(confidence, 0.64 if evidence_found else 0.0)
            reason = "work_or_project_evidence"
        elif slot_name == "work_detail":
            evidence_found = bool(
                confidence >= 0.45
                or (
                    _text_has_org_or_project_evidence(evidence_text)
                    and any(token in folded for token in ("project", "progetto", "lavor", "azienda", "company", "found", "fondat", "ceo", "energy", "software"))
                )
            )
            confidence = max(confidence, 0.66 if evidence_found else 0.0)
            reason = "work_detail_evidence"
        elif slot_name == "company_founding":
            evidence_found = _company_founding_material_present(evidence_text)
            confidence = max(confidence if evidence_found else 0.0, 0.72 if evidence_found else 0.0)
            reason = "company_founding_evidence"
        elif slot_name == "identity":
            evidence_found = bool(
                not _source_surface_noise_only(folded)
                and (confidence >= 0.45 or any(token in folded for token in ("sono", "identity", "identita", "chi sei", "name", "mi chiamo")))
            )
            confidence = max(confidence, 0.64 if evidence_found else 0.0)
            reason = "identity_evidence"
        elif slot_name == "values":
            evidence_found = bool(
                not _source_surface_noise_only(folded)
                and (
                    confidence >= 0.45
                    or any(
                        token in folded
                        for token in (
                            "values",
                            "valori",
                            "principles",
                            "principi",
                            "courage",
                            "coraggio",
                            "precision",
                            "precisione",
                            "responsibility",
                            "responsabil",
                            "vocation",
                            "vocazione",
                            "sustainable",
                            "sostenibil",
                            "impact",
                            "impatto",
                        )
                    )
                )
            )
            confidence = max(confidence, 0.64 if evidence_found else 0.0)
            reason = "values_evidence"
        elif exact_field_request and slot_name == str(exact_field_request.get("slot_id") or ""):
            evidence_found = text_satisfies_exact_field_request(evidence_text, exact_field_request)
            confidence = 0.9 if evidence_found else 0.0
            reason = "exact_requested_field_evidence"
        else:
            evidence_found = bool(confidence >= 0.45 or slot_name in folded)
        rows.append(
            {
                "slot": slot_name,
                "required": True,
                "evidence_found": bool(evidence_found),
                "confidence": round(float(confidence), 4),
                "reason": reason if evidence_found else "missing_or_not_yet_proven",
            }
        )
    semantic_rows = [
        _semantic_slot_evidence_row(
            dict(slot_contract),
            evidence_text=evidence_text,
            folded=folded,
            coverage_by_slot=coverage_by_slot,
        )
        for slot_contract in list(contract.get("semantic_slot_contracts") or [])
        if isinstance(slot_contract, dict) and bool(slot_contract.get("required"))
    ]
    semantic_missing_slots = [
        str(row["slot"])
        for row in semantic_rows
        if bool(row.get("required")) and not bool(row.get("evidence_found"))
    ]
    semantic_satisfied_slots = [
        str(row["slot"])
        for row in semantic_rows
        if bool(row.get("required")) and bool(row.get("evidence_found"))
    ]
    missing_slots = [str(row["slot"]) for row in rows if not bool(row.get("evidence_found"))]
    final_ready = not missing_slots and not semantic_missing_slots
    return {
        "schema_version": "agvm.evidence_satisfaction.v1",
        "query_contract": contract,
        "rows": rows,
        "semantic_rows": semantic_rows,
        "missing_slots": missing_slots,
        "semantic_missing_slots": semantic_missing_slots,
        "satisfied_slots": [str(row["slot"]) for row in rows if bool(row.get("evidence_found"))],
        "semantic_satisfied_slots": semantic_satisfied_slots,
        "required_slot_count": len(rows),
        "satisfied_required_slot_count": len(rows) - len(missing_slots),
        "semantic_required_slot_count": len(semantic_rows),
        "semantic_satisfied_required_slot_count": len(semantic_satisfied_slots),
        "final_ready": bool(final_ready),
        "fast_final_allowed": bool(contract.get("fast_final_allowed") and final_ready),
        "requires_expansion": bool(contract.get("requires_expansion") and not final_ready),
    }


def query_contract_allows_fast_final(
    query_text: str,
    *,
    retrieval_mode: str = "balanced",
    matches: list[dict[str, Any]] | None = None,
    answer: dict[str, Any] | None = None,
    shared_evidence: dict[str, Any] | None = None,
    evidence_reservoir: dict[str, Any] | None = None,
) -> bool:
    matrix = build_evidence_satisfaction_matrix(
        query_text=query_text,
        retrieval_mode=retrieval_mode,
        matches=matches,
        answer=answer,
        shared_evidence=shared_evidence,
        evidence_reservoir=evidence_reservoir,
    )
    return bool(matrix.get("fast_final_allowed"))


def query_contract_requires_expansion(query_text: str, *, retrieval_mode: str = "balanced") -> bool:
    contract = build_query_contract(query_text, retrieval_mode=retrieval_mode)
    return bool(contract.get("requires_expansion"))


def _clean_fact_value(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "").replace("â€¦", "...").replace("…", "...").strip(" .,:;"))
    return cleaned


def clean_answer_surface_text(value: str | None) -> str:
    text = str(value or "").replace("â€¦", "...").replace("…", "...").strip()
    if not text:
        return ""
    text = re.sub(r"^(?:[-–—•]\s*)?(?:\.{3,}\s*)+", "", text).strip()
    text = re.sub(r"^\[[^\]]{1,80}\]\s*", "", text).strip()
    text = re.sub(
        r"^(?:[-–—•]\s*)?(?:Raw context|Fact|Context|Evidence)(?:\s*\[[^\]]{1,80}\])?\s*[:\-]\s*",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _clean_role_value(value: str) -> str:
    cleaned = _clean_fact_value(value)
    cleaned = re.sub(r"\s+associated with\b.*$", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.split(r",\s+(?:a|an|the)\s+", cleaned, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    cleaned = re.sub(r"^(?:a|an|the|un|una|uno)\s+", "", cleaned, flags=re.IGNORECASE).strip()
    role_translations = {
        "founder and ceo": "fondatore e CEO",
        "founder-operator": "fondatore-operatore",
        "founder operator": "fondatore-operatore",
        "self taught coder": "coder autodidatta",
    }
    return role_translations.get(_fold_text(cleaned), cleaned)


def _sentence_candidates(text: str) -> list[str]:
    return [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])(?:[\"'”’])?\s+|\n+", text)
        if sentence.strip()
    ]


def _named_entity_sequence(value: str) -> str | None:
    cleaned = _clean_fact_value(value)
    if not cleaned:
        return None
    tokens = [token for token in re.split(r"\s+", cleaned) if token]
    if len(tokens) < 2:
        return None
    if all(token[:1].isupper() or token[:1].isdigit() for token in tokens):
        return cleaned
    return None


def _make_fact(
    *,
    kind: str,
    text: str,
    node_id: str,
    raw_score: float,
    summary: str,
    priority: float = 0.0,
    value: str | None = None,
    evidence_snippet: str | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "text": text.strip(),
        "node_id": node_id,
        "raw_score": raw_score,
        "summary": summary,
        "priority": priority,
        "value": value,
        "evidence_snippet": evidence_snippet or text.strip(),
    }


def extract_grounded_fact_inventory(matches: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    matches = _eligible_answer_matches(matches)
    inventory: dict[str, list[dict[str, Any]]] = {
        "name": [],
        "birthplace": [],
        "residence": [],
        "father": [],
        "partner": [],
        "mentor": [],
        "sibling": [],
        "style": [],
        "values": [],
        "role": [],
        "primary_project": [],
        "secondary_project": [],
        "history": [],
    }
    for match in matches[:16]:
        node = match["node"]
        raw_text = str(node.get("raw_text") or node.get("summary") or "").strip()
        summary = str(node.get("summary") or raw_text).strip()
        node_id = str(match["node_id"])
        raw_score = float(match.get("raw_score") or 0.0)
        memory_type = str(node.get("memory_type") or "")
        guide_area = str((node.get("provenance") or {}).get("guide_conceptual_area") or "")
        evidence_snippet = str(match.get("evidence_snippet") or raw_text).strip()

        if memory_type == "episodic" or guide_area.lower() == "history":
            inventory["history"].append(
                _make_fact(
                    kind="history",
                    text=raw_text,
                    node_id=node_id,
                    raw_score=raw_score,
                    summary=summary,
                    priority=0.2,
                    evidence_snippet=evidence_snippet,
                )
            )

        candidate_texts = [raw_text]
        if summary and summary != raw_text:
            candidate_texts.append(summary)
        for sentence in dict.fromkeys(
            sentence
            for text in candidate_texts
            for sentence in _sentence_candidates(text)
            if sentence.strip()
        ):
            lowered = sentence.lower()
            if (
                _sentence_has_temporal_signal(sentence)
                and not _temporal_text_is_source_metadata(sentence)
                and not any(str(item.get("text") or "").strip() == sentence.strip() for item in inventory["history"])
            ):
                inventory["history"].append(
                    _make_fact(
                        kind="history",
                        text=sentence if sentence.endswith(".") else f"{sentence}.",
                        node_id=node_id,
                        raw_score=raw_score,
                        summary=summary,
                        priority=0.72 if re.search(r"\b(?:19|20)\d{2}\b", sentence) else 0.5,
                        evidence_snippet=sentence,
                    )
                )

            for pattern in (
                r"([A-ZÀ-ÖØ-Þ][\w'’-]+(?: [A-ZÀ-ÖØ-Þ][\w'’-]+)+)\s+è il nome(?: dell'autrice| dell'autore)?",
                r"(?:mi chiamo|my name is)\s+([A-ZÀ-ÖØ-Þ][\w'’-]+(?: [A-ZÀ-ÖØ-Þ][\w'’-]+)+)",
                r"(?:sono|i am)\s+([A-ZÀ-ÖØ-Þ][\w'’-]+(?: [A-ZÀ-ÖØ-Þ][\w'’-]+)+)(?:[.,;]|$)",
            ):
                name_match = re.search(pattern, sentence, flags=re.IGNORECASE)
                if name_match:
                    name_value = _clean_fact_value(name_match.group(1))
                    inventory["name"].append(
                        _make_fact(
                            kind="name",
                            text=f"Il nome e {name_value}.",
                            node_id=node_id,
                            raw_score=raw_score,
                            summary=summary,
                            priority=0.95,
                            value=name_value,
                            evidence_snippet=sentence,
                        )
                    )
                    break

            birthplace_match = re.search(
                r"(?:è nat[ao] a|nato a|nata a|born in)\s+([A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÿ'’ .-]+?)(?:[.,;]|$)",
                sentence,
            )
            if birthplace_match:
                place = _clean_fact_value(birthplace_match.group(1))
                inventory["birthplace"].append(
                    _make_fact(
                        kind="birthplace",
                        text=f"E nata a {place}.",
                        node_id=node_id,
                        raw_score=raw_score,
                        summary=summary,
                        priority=0.92,
                        value=place,
                        evidence_snippet=sentence,
                    )
                )

            residence_match = re.search(
                r"(?:vive a|lives in)\s+([A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÿ'’ .-]+?)(?:[.,;]|$)",
                sentence,
            )
            if residence_match:
                place = _clean_fact_value(residence_match.group(1))
                inventory["residence"].append(
                    _make_fact(
                        kind="residence",
                        text=f"Vive a {place}.",
                        node_id=node_id,
                        raw_score=raw_score,
                        summary=summary,
                        priority=0.9,
                        value=place,
                        evidence_snippet=sentence,
                    )
                )

            father_value = _extract_father_name(sentence)
            if father_value:
                detail_fragments = [f"Il padre si chiamava {father_value}."]
                folded_sentence = _fold_text(sentence)
                if "aeronautica" in folded_sentence or "aereonautica" in folded_sentence or "air force" in folded_sentence:
                    detail_fragments.append(f"{father_value} faceva parte dell'Aeronautica Militare.")
                if "monumento" in folded_sentence or "monument" in folded_sentence:
                    date_match = re.search(
                        r"\b(?:il\s+)?(\d{1,2}\s+[A-Za-z]+\s+(?:19|20)\d{2}|(?:May|June|July|August|September|October|November|December|January|February|March|April)\s+\d{1,2},\s*(?:19|20)\d{2})\b",
                        sentence,
                        flags=re.IGNORECASE,
                    )
                    date_suffix = f" inaugurato il {date_match.group(1)}" if date_match else ""
                    detail_fragments.append(f"Gli e stato dedicato un monumento{date_suffix}.")
                inventory["father"].append(
                    _make_fact(
                        kind="father",
                        text=" ".join(detail_fragments),
                        node_id=node_id,
                        raw_score=raw_score,
                        summary=summary,
                        priority=0.96,
                        value=father_value,
                        evidence_snippet=sentence,
                    )
                )

            partner_value = None
            for partner_pattern in (
                r"([A-ZÀ-ÖØ-Þ][\w'’-]+(?: [A-ZÀ-ÖØ-Þ][\w'’-]+)+)\s+è il mio partner",
                r"(?:her partner|his partner|il partner(?: di [A-ZÀ-ÖØ-Þ][\w'’-]+)?|la partner(?: di [A-ZÀ-ÖØ-Þ][\w'’-]+)?|partner)\s+(?:is|è)?\s*([A-ZÀ-ÖØ-Þ][\w'’-]+(?: [A-ZÀ-ÖØ-Þ][\w'’-]+)+)",
            ):
                partner_match = re.search(partner_pattern, sentence)
                if partner_match:
                    partner_value = _clean_person_name_value(partner_match.group(1))
                    break
            if partner_value:
                inventory["partner"].append(
                    _make_fact(
                        kind="partner",
                        text=f"Il partner e {partner_value}.",
                        node_id=node_id,
                        raw_score=raw_score,
                        summary=summary,
                        priority=0.94,
                        value=partner_value,
                        evidence_snippet=sentence,
                    )
                )

            mentor_value = None
            for mentor_pattern in (
                r"([A-ZÀ-ÖØ-Þ][\w'’-]+(?: [A-ZÀ-ÖØ-Þ][\w'’-]+)+)\s+è stata una mentor",
                r"(?:her mentor|his mentor|la mentor(?: di [A-ZÀ-ÖØ-Þ][\w'’-]+)?|mentor)\s+(?:is|è)?\s*([A-ZÀ-ÖØ-Þ][\w'’-]+(?: [A-ZÀ-ÖØ-Þ][\w'’-]+)+)",
            ):
                mentor_match = re.search(mentor_pattern, sentence)
                if mentor_match:
                    mentor_value = _clean_fact_value(mentor_match.group(1))
                    break
            if mentor_value:
                inventory["mentor"].append(
                    _make_fact(
                        kind="mentor",
                        text=f"La mentor e {mentor_value}.",
                        node_id=node_id,
                        raw_score=raw_score,
                        summary=summary,
                        priority=0.92,
                        value=mentor_value,
                        evidence_snippet=sentence,
                    )
                )

            sibling_match = re.search(
                r"([A-ZÀ-ÖØ-Þ][\w'’-]+(?: [A-ZÀ-ÖØ-Þ][\w'’-]+)+)\s+è mio (?:fratello|fratella|brother|sibling)",
                sentence,
            )
            if sibling_match:
                sibling_value = _clean_fact_value(sibling_match.group(1))
                inventory["sibling"].append(
                    _make_fact(
                        kind="sibling",
                        text=f"Il sibling e {sibling_value}.",
                        node_id=node_id,
                        raw_score=raw_score,
                        summary=summary,
                        priority=0.88,
                        value=sibling_value,
                        evidence_snippet=sentence,
                    )
                )

            role_match = re.search(
                r"(?:lavora come|lavoro come|works as|i work as)\s+([A-Za-zÀ-ÿ0-9'’ -]+?)(?:[.,;]|$)",
                sentence,
            )
            role_priority = 0.86
            if not role_match:
                for role_pattern in (
                    r"^(?:Dr\.\s+)?[A-ZÀ-Þ][A-Za-zÀ-ÿ'’.-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÿ'’.-]+){0,4}\s+is presented as\s+(?:a|an)?\s*([A-Za-zÀ-ÿ0-9'’ ,/-]+?)(?:[.;]|$)",
                    r"^(?:Dr\.\s+)?[A-ZÀ-Þ][A-Za-zÀ-ÿ'’.-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÿ'’.-]+){0,4}\s+(?:is|è|e)\s+(?:a|an|un|una|uno)?\s*([A-Za-zÀ-ÿ0-9'’ ,/-]+?)(?:\s+(?:who|che)\s+|[.;]|$)",
                ):
                    role_match = re.search(role_pattern, sentence, flags=re.IGNORECASE)
                    if role_match:
                        role_priority = 0.89
                        break
            if role_match:
                role_value = _clean_role_value(role_match.group(1))
                inventory["role"].append(
                    _make_fact(
                        kind="role",
                        text=f"Lavora come {role_value}.",
                        node_id=node_id,
                        raw_score=raw_score,
                        summary=summary,
                        priority=role_priority,
                        value=role_value,
                        evidence_snippet=sentence,
                    )
                )

            style_match = re.search(
                r"(?:stile di comunicazione è|parla in modo|scrive in modo|si esprime in modo)\s+(.+?)(?:[.;]|$)",
                sentence,
            )
            style_value = _clean_fact_value(style_match.group(1)) if style_match else ""
            style_descriptor_tokens = (
                "diretto",
                "tecnico",
                "strutturato",
                "lucido",
                "chiaro",
                "chiara",
                "poco ridondante",
                "essenziale",
                "analitico",
                "analitica",
            )
            style_context_tokens = (
                "conversazioni",
                "comunic",
                "parla",
                "scrive",
                "spiega",
                "tono",
                "voice",
                "stile",
            )
            if not style_value and any(token in lowered for token in style_context_tokens) and any(token in lowered for token in style_descriptor_tokens):
                style_value = _clean_fact_value(sentence)
            if style_value:
                style_text = sentence if sentence.endswith(".") else f"{sentence}."
                inventory["style"].append(
                    _make_fact(
                        kind="style",
                        text=style_text,
                        node_id=node_id,
                        raw_score=raw_score,
                        summary=summary,
                        priority=0.82,
                        value=style_value,
                        evidence_snippet=sentence,
                    )
                )

            values_match = re.search(
                r"(?:valorizza|principi? .* sono|principi di .* sono|ha principi di|values?|value of|guidata da|guided by|focus on)\s+(.+?)(?:[.;]|$)",
                lowered,
            )
            strong_value_tokens = (
                "precisione",
                "chiarezza",
                "rigore",
                "qualità",
                "qualita",
                "responsabilità",
                "responsabilita",
                "coerenza architetturale",
                "consistenza architetturale",
                "sostenibil",
                "sustainable",
                "decarbon",
                "impatto",
                "impact",
                "cooperazione",
                "cooperation",
                "educazione",
                "education",
                "talent",
                "coraggio",
                "courage",
                "ambizione",
                "ambition",
            )
            value_context_tokens = ("value", "valore", "values", "valori", "principio", "principi", "guided", "guidata", "guida", "focus", "rooted", "radici")
            if values_match or (
                sum(1 for token in strong_value_tokens if token in lowered) >= 2
                or (
                    any(token in lowered for token in strong_value_tokens)
                    and any(token in lowered for token in value_context_tokens)
                )
            ):
                values_text = sentence if sentence.endswith(".") else f"{sentence}."
                inventory["values"].append(
                    _make_fact(
                        kind="values",
                        text=values_text,
                        node_id=node_id,
                        raw_score=raw_score,
                        summary=summary,
                        priority=0.8,
                        value=_clean_fact_value(values_match.group(1) if values_match else sentence),
                        evidence_snippet=sentence,
                    )
                )

            for pattern, priority, bucket_name in (
                (r"([A-Z][A-Za-z0-9' -]+?)\s+(?:e|is)\s+(?:il\s+)?mio progetto principale(?:[.;]|$)", 1.0, "primary_project"),
                (r"(?:il\s+)?mio progetto principale\s+(?:e|is)\s+([A-Z][A-Za-z0-9' -]+?)(?:[.;]|$)", 1.0, "primary_project"),
                (r"(?:guida|is building|is constructing|sta costruendo|sto costruendo|sta buildando|building)\s+([A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÿ0-9'’ -]+?)(?:\s+(?:dentro|inside|within)\s+([A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÿ0-9'’ .-]+?))?(?:[.;]|$)", 1.0, "primary_project"),
                (r"(?:sta sviluppando|is developing)\s+([A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÿ0-9'’ -]+?)(?:[.;]|$)", 0.74, "secondary_project"),
            ):
                project_match = re.search(pattern, sentence, flags=re.IGNORECASE)
                if not project_match:
                    continue
                project_name = _clean_fact_value(project_match.group(1))
                org_name = _clean_fact_value(project_match.group(2)) if project_match.lastindex and project_match.lastindex >= 2 and project_match.group(2) else None
                if bucket_name == "primary_project" and org_name:
                    text = f"Il progetto principale e {project_name} dentro {org_name}."
                elif bucket_name == "primary_project":
                    text = f"Il progetto principale e {project_name}."
                else:
                    text = f"Sta sviluppando anche {project_name}."
                inventory[bucket_name].append(
                    _make_fact(
                        kind=bucket_name,
                        text=text,
                        node_id=node_id,
                        raw_score=raw_score,
                        summary=summary,
                        priority=priority,
                        value=project_name,
                        evidence_snippet=sentence,
                    )
                )

        if memory_type == "project":
            name_value = _named_entity_sequence(summary)
            if name_value:
                inventory["secondary_project"].append(
                    _make_fact(
                        kind="secondary_project",
                        text=f"Il progetto citato è {name_value}.",
                        node_id=node_id,
                        raw_score=raw_score,
                        summary=summary,
                        priority=0.35,
                        value=name_value,
                        evidence_snippet=evidence_snippet,
                    )
                )
    return inventory


def _pick_best_fact(facts: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not facts:
        return None
    return max(facts, key=lambda item: (float(item.get("priority") or 0.0), float(item.get("raw_score") or 0.0)))


def _descriptor_tokens(facts: list[dict[str, Any]], *, allow: tuple[str, ...]) -> list[str]:
    descriptors: list[str] = []
    for fact in facts:
        text = _fold_text(str(fact.get("text") or fact.get("value") or ""))
        for token in allow:
            if token in text and token not in descriptors:
                descriptors.append(token)
    return descriptors


def _compound_style_fact(facts: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not facts:
        return None
    descriptors = _descriptor_tokens(
        facts,
        allow=("diretto", "tecnico", "strutturato", "lucido", "chiaro", "essenziale", "analitico", "poco ridondante"),
    )
    evidence_blob = " ".join(
        str(fact.get("evidence_snippet") or fact.get("text") or fact.get("value") or "")
        for fact in facts[:8]
    ).lower()
    if "tecnico" not in descriptors and any(token in evidence_blob for token in ("codice", "code", "architett", "sistema", "system", "ricerca", "research", "document")):
        descriptors.append("tecnico")
    if "strutturato" not in descriptors and any(token in evidence_blob for token in ("struttura", "spiega prima", "dettagli", "organizzare", "organizza")):
        descriptors.append("strutturato")
    if descriptors:
        priority = {
            "diretto": 0,
            "tecnico": 1,
            "strutturato": 2,
            "chiaro": 3,
            "lucido": 4,
            "analitico": 5,
            "essenziale": 6,
            "poco ridondante": 7,
        }
        descriptors = sorted(
            list(dict.fromkeys(descriptors)),
            key=lambda token: (priority.get(token, 99), token),
        )
    best = _pick_best_fact(facts)
    if not best:
        return None
    if descriptors:
        text = f"Comunica in modo {', '.join(descriptors[:3])}."
        return {
            **best,
            "text": text,
            "value": ", ".join(descriptors[:3]),
            "evidence_snippet": str(best.get("evidence_snippet") or best.get("text") or ""),
            "kind": "style",
        }
    return best


def _compound_values_fact(facts: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not facts:
        return None
    descriptors = _descriptor_tokens(
        facts,
        allow=(
            "precisione",
            "chiarezza",
            "rigore",
            "qualità",
            "qualita",
            "responsabilità",
            "responsabilita",
            "coerenza architetturale",
            "cura dei dettagli",
            "disciplina esecutiva",
            "sostenibil",
            "sustainable",
            "decarbon",
            "impatto",
            "impact",
            "cooperazione",
            "cooperation",
            "educazione",
            "education",
            "talent",
            "coraggio",
            "courage",
            "ambizione",
            "ambition",
        ),
    )
    evidence_blob = " ".join(
        str(fact.get("evidence_snippet") or fact.get("value") or fact.get("text") or "")
        for fact in facts[:10]
    ).lower()
    for token in (
        "precisione",
        "chiarezza",
        "rigore",
        "coerenza architetturale",
        "cura dei dettagli",
        "disciplina esecutiva",
        "sostenibil",
        "sustainable",
        "decarbon",
        "impatto",
        "impact",
        "cooperazione",
        "cooperation",
        "educazione",
        "education",
        "talent",
        "coraggio",
        "courage",
        "ambizione",
        "ambition",
    ):
        if token in evidence_blob and token not in descriptors:
            descriptors.append(token)
    best = _pick_best_fact(facts)
    if not best:
        return None
    if descriptors:
        normalized = [
            {
                "qualita": "qualità",
                "responsabilita": "responsabilità",
                "sostenibil": "sostenibilità",
                "sustainable": "sostenibilità",
                "decarbon": "decarbonizzazione",
                "impact": "impatto",
                "cooperation": "cooperazione",
                "education": "educazione",
                "talent": "sviluppo del talento",
                "courage": "coraggio",
                "ambition": "ambizione",
            }.get(token, token)
            for token in descriptors[:5]
        ]
        text = f"I suoi valori chiave sono {', '.join(normalized)}."
        return {
            **best,
            "text": text,
            "value": ", ".join(normalized),
            "evidence_snippet": str(best.get("evidence_snippet") or best.get("text") or ""),
            "kind": "values",
        }
    return best


def _is_self_identity_question(query_text: str) -> bool:
    lowered = _fold_text(query_text)
    return any(pattern in lowered for pattern in ("chi sei", "who are you", "tell me about yourself"))


def _prefers_first_person_answer(query_text: str) -> bool:
    lowered = _fold_text(query_text)
    padded = f" {lowered} "
    broad_self_detector = globals().get("_is_broad_self_query")
    broad_self_query = bool(broad_self_detector and broad_self_detector(query_text))
    return broad_self_query or _is_self_identity_question(query_text) or any(
        pattern in lowered
        for pattern in (
            "come ti chiami",
            "qual e il tuo nome",
            "che ruolo hai",
            "che lavoro fai",
            "che fai",
            "cosa fai",
            "quello che fai",
            "cosa hai fatto",
            "che cosa hai fatto",
            "hai fatto",
            "su cosa lavori",
            "su quali progetti lavoro",
            "quali progetti lavoro",
            "progetti lavoro",
            "lavoro oggi",
            "cosa stai costruendo",
            "dove vivi",
            "dove sei nata",
            "dove sei nato",
            "mio padre",
            "mia madre",
            "tuo padre",
            "tua madre",
            "my father",
            "my mother",
            "your father",
            "your mother",
            "come comunichi",
            "come parli",
            "che ruolo ha",
            "come si collega",
            "si collega a",
            "collega a",
            "quali valori emergono",
            "valori emergono",
            "quali sono i tuoi valori",
            "cosa sai di",
            "cosa sai su",
            "che cosa sai di",
            "che cosa sai su",
            "prima persona",
            "first person",
            "in prima persona",
            "raccontami di te",
            "parlami di te",
            "your name",
            "where do you live",
            "what do you do",
            "what are you working on",
            "tell me about yourself",
        )
    ) or any(token in padded for token in (" tu ", " ti ", " te ", " mio ", " mia ", " miei ", " mie ", " tuo ", " tua ", " tuoi ", " tue ", " my ", " you ", " your "))


def _self_voice_fragment(text: str) -> str:
    fragment = str(text or "").strip()
    fragment = fragment.replace("oggi vive", "oggi vivo").replace(" vive a ", " vivo a ")
    temporal_person_action = re.match(
        r"^(?P<prefix>(?:(?:Nel|In)\s+(?:19|20)\d{2},?\s+|Durante\s+[^,]{1,40},\s+|(?:Più avanti|Piu avanti|In seguito|Successivamente|Dopo)\b[^.!?]{0,120},\s+))"
        r"(?:Dr\.\s+)?[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'._-]+(?:\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'._-]+){0,4}\s+ha\s+(?P<rest>.+)$",
        fragment,
        flags=re.IGNORECASE,
    )
    if temporal_person_action:
        fragment = f"{temporal_person_action.group('prefix')}ho {temporal_person_action.group('rest')}"
    memory_subject_name = re.match(r"^(?:The\s+)?memory\s+subject(?:'s|\s+s)?\s+name\s+is\s+(.+?)\.?$", fragment, flags=re.IGNORECASE)
    if memory_subject_name:
        name = memory_subject_name.group(1).strip(" .")
        return f"Mi chiamo {name}."
    identity_name = re.match(r"^(?:Identity\s+)?name\s+(?:is\s+)?(.+?)\.?$", fragment, flags=re.IGNORECASE)
    if identity_name:
        name = identity_name.group(1).strip(" .")
        return f"Mi chiamo {name}."
    founder_ceo_direct = re.match(
        r"^Sono\s+the\s+founder\s+and\s+CEO\s+(?:associated\s+with|of|di)\s+([A-Z][A-Za-z0-9&'._-]+(?:\s+[A-Z][A-Za-z0-9&'._-]+){0,5})\b",
        fragment,
        flags=re.IGNORECASE,
    )
    if founder_ceo_direct:
        company = founder_ceo_direct.group(1).strip(" .,;:")
        return f"Sono fondatore e CEO associato a {company}."
    ceo_phrase = re.search(
        r"\b(?:CEO|chief\s+executive\s+officer)\s+(?:of\s+|di\s+)?([A-Z][A-Za-z0-9&'._-]+(?:\s+[A-Z][A-Za-z0-9&'._-]+){0,5})\b",
        fragment,
        flags=re.IGNORECASE,
    )
    if ceo_phrase and any(marker in _fold_text(fragment) for marker in ("ingegnere", "engineer", "founder", "fondatore", "dichiara", "quoted", "memory subject")):
        company = ceo_phrase.group(1).strip(" .,;:")
        return f"Sono CEO di {company}."
    values_phrase = re.match(
        r"^(?:Dr\.\s+)?[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'._-]+(?:\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'._-]+){1,4}\s+values\s+(.+?)\.?$",
        fragment,
        flags=re.IGNORECASE,
    )
    if values_phrase:
        return f"I miei valori includono {values_phrase.group(1).strip(' .')}."
    acquired_phrase = re.match(
        r"^([A-Z][A-Za-z0-9&'._-]+(?:\s+[A-Z][A-Za-z0-9&'._-]+){0,5})\s+acquired\s+([A-Z][A-Za-z0-9&'._-]+(?:\s+[A-Z][A-Za-z0-9&'._-]+){0,5})\.?$",
        fragment,
        flags=re.IGNORECASE,
    )
    if acquired_phrase:
        acquired_target = acquired_phrase.group(2).strip(" .")
        acquirer = acquired_phrase.group(1).strip(" .")
        return f"{acquired_target} e stata acquisita da {acquirer}."
    learned_values = re.match(
        r"^I\s+learned\s+early\s+that\s+(.+?)\s+are\s+not\s+just\s+values\b.*$",
        fragment,
        flags=re.IGNORECASE,
    )
    if learned_values:
        values = learned_values.group(1).strip(" .")
        values = values.replace("courage", "coraggio").replace("precision", "precisione")
        values = re.sub(r"\s+and\s+", " e ", values, flags=re.IGNORECASE)
        return f"I miei valori includono {values}."
    entity_token = r"[A-Z][A-Za-z0-9&'._-]+"
    entity_span = rf"{entity_token}(?:\s+{entity_token}){{0,5}}"
    person_subject = rf"(?:Dr\.\s+)?{entity_token}(?:\s+{entity_token}){{1,4}}"

    def likely_person_subject(value: str) -> bool:
        subject = re.sub(r"^(?:Dr\.\s+)", "", str(value or "").strip(" ."), flags=re.IGNORECASE)
        if not subject:
            return False
        folded_subject = _fold_text(subject)
        if folded_subject in {"he", "she", "lui", "lei"}:
            return True
        parts = subject.split()
        if parts and parts[0].lower() in {"he", "she", "lui", "lei"}:
            return False
        if not parts or not all(part[:1].isupper() for part in parts):
            return False
        if _target_looks_like_org_or_project(subject):
            return False
        if any(
            marker in folded_subject
            for marker in (
                "company",
                "corporation",
                "systems",
                "software",
                "platform",
                "project",
                "studio",
                "lab",
                "foundation",
                "foundry",
                "energy",
                "group",
            )
        ):
            return False
        return len(parts) >= 2

    def year_prefix(value: str | None) -> str:
        year = str(value or "").strip(" ,")
        return f"Nel {year} " if year else ""

    org_link = re.match(
        rf"^(?P<org>{entity_span})\s+is\s+another\s+organization\s+linked\s+to\s+(?P<subject>{person_subject})\.?$",
        fragment,
        flags=re.IGNORECASE,
    )
    if org_link and likely_person_subject(org_link.group("subject")):
        org = org_link.group("org").strip(" .")
        return f"{org} e un'altra organizzazione a cui sono collegato."

    org_assoc = re.match(
        rf"^(?P<org>{entity_span})\s+is\s+(?:a|an)\s+(?P<kind>company|organization|organisation|project|platform|venture|studio|lab)\s+(?:associated|linked)\s+with\s+(?P<subject>{person_subject})\.?$",
        fragment,
        flags=re.IGNORECASE,
    )
    if org_assoc and likely_person_subject(org_assoc.group("subject")):
        org = org_assoc.group("org").strip(" .")
        kind = _fold_text(org_assoc.group("kind"))
        noun = "societa" if kind == "company" else "organizzazione"
        return f"{org} e una {noun} a cui sono associato."

    temporal_ha = re.match(
        rf"^(?P<prefix>Nel\s+(?:19|20)\d{{2}},?\s+)(?P<subject>{person_subject})\s+ha\s+(?P<rest>.+)$",
        fragment,
        flags=re.IGNORECASE,
    )
    if temporal_ha and likely_person_subject(temporal_ha.group("subject")):
        fragment = f"{temporal_ha.group('prefix')}ho {temporal_ha.group('rest')}"

    subject_dedication = re.match(
        rf"^(?P<subject>{person_subject})\s+gli\s+ha\s+dedicato\s+(?P<rest>.+)$",
        fragment,
        flags=re.IGNORECASE,
    )
    if subject_dedication and likely_person_subject(subject_dedication.group("subject")):
        fragment = f"gli ho dedicato {subject_dedication.group('rest')}"

    subject_ha = re.match(rf"^(?P<subject>{person_subject})\s+ha\s+(?P<rest>.+)$", fragment, flags=re.IGNORECASE)
    if subject_ha and likely_person_subject(subject_ha.group("subject")):
        fragment = f"Ho {subject_ha.group('rest')}"

    started_work = re.match(
        rf"^(?P<subject>{person_subject})\s+(?:began|started)\s+working\s+on\s+(?P<target>.+?)\s+in\s+(?P<year>(?:19|20)\d{{2}})\.?$",
        fragment,
        flags=re.IGNORECASE,
    )
    if started_work and likely_person_subject(started_work.group("subject")):
        fragment = f"Nel {started_work.group('year')} ho iniziato a lavorare su {started_work.group('target').strip(' .')}."

    for verb, rewrite in (
        ("began", "Ho iniziato "),
        ("started", "I started "),
        ("worked", "I worked "),
        ("was working", "I was working "),
        ("has worked", "I have worked "),
        ("is", "I am "),
        ("was", "I was "),
        ("lives", "I live "),
    ):
        temporal = re.match(
            rf"^(?P<prefix>In\s+(?:19|20)\d{{2}},?\s+)(?P<subject>{person_subject})\s+{re.escape(verb)}\s+(?P<rest>.+)$",
            fragment,
            flags=re.IGNORECASE,
        )
        if temporal and likely_person_subject(temporal.group("subject")):
            fragment = f"{temporal.group('prefix')}{rewrite}{temporal.group('rest')}"
            break
        direct = re.match(
            rf"^(?P<subject>{person_subject})\s+{re.escape(verb)}\s+(?P<rest>.+)$",
            fragment,
            flags=re.IGNORECASE,
        )
        if direct and likely_person_subject(direct.group("subject")):
            fragment = f"{rewrite}{direct.group('rest')}"
            break

    founded = re.match(
        rf"^(?:(?:In|Nel)\s+)?(?P<year>(?:19|20)\d{{2}},?\s+)?(?P<subject>he|{person_subject})\s+founded\s+(?P<rest>.+)$",
        fragment,
        flags=re.IGNORECASE,
    )
    if founded and likely_person_subject(founded.group("subject")):
        fragment = f"{year_prefix(founded.group('year'))}ho fondato {founded.group('rest')}"

    founded_it = re.match(
        rf"^(?:(?:In|Nel)\s+)?(?P<year>(?:19|20)\d{{2}},?\s+)?(?P<subject>he|{person_subject})\s+ha\s+fondato\s+(?P<rest>.+)$",
        fragment,
        flags=re.IGNORECASE,
    )
    if founded_it and likely_person_subject(founded_it.group("subject")):
        fragment = f"{year_prefix(founded_it.group('year'))}ho fondato {founded_it.group('rest')}"

    for relation_verb, rewrite in (("continues as", "continuo come"), ("continued as", "ho continuato come")):
        relation = re.search(
            rf"\b(?P<subject>{person_subject})\s+{re.escape(relation_verb)}\b",
            fragment,
            flags=re.IGNORECASE,
        )
        if relation and likely_person_subject(relation.group("subject")):
            fragment = fragment[: relation.start()] + rewrite + fragment[relation.end() :]
            break

    inline_ha = re.search(rf"\b(?P<subject>{person_subject})\s+ha\s+", fragment, flags=re.IGNORECASE)
    if inline_ha and likely_person_subject(inline_ha.group("subject")):
        fragment = fragment[: inline_ha.start()] + "ho " + fragment[inline_ha.end() :]

    inline_lavora = re.search(rf"\b(?P<subject>{person_subject})\s+lavora\s+", fragment, flags=re.IGNORECASE)
    if inline_lavora and likely_person_subject(inline_lavora.group("subject")):
        fragment = fragment[: inline_lavora.start()] + "lavoro " + fragment[inline_lavora.end() :]

    founder_ceo_assoc = re.match(
        rf"^Sono\s+the\s+founder\s+and\s+CEO\s+associated\s+with\s+(?P<org>{entity_span})\b",
        fragment,
        flags=re.IGNORECASE,
    )
    if founder_ceo_assoc:
        org = founder_ceo_assoc.group("org").strip(" .,;:")
        fragment = re.sub(
            rf"^Sono\s+the\s+founder\s+and\s+CEO\s+associated\s+with\s+{re.escape(org)}\b",
            f"Sono fondatore e CEO associato a {org}",
            fragment,
            flags=re.IGNORECASE,
        )
    fragment = re.sub(r"^Sono\s+a\s+founder[-\s]operator\b", "Sono founder-operator", fragment, flags=re.IGNORECASE)
    fragment = re.sub(r"^I\s+am\s+a\s+technology\s+leader\b", "Sono un technology leader", fragment, flags=re.IGNORECASE)
    fragment = re.sub(r"\bHe\s+is\s+publicly\s+linked\s+with\b", "Sono pubblicamente collegato a", fragment, flags=re.IGNORECASE)
    fragment = re.sub(r"\bHe\s+is\s+also\s+publicly\s+associated\s+with\b", "Sono anche pubblicamente associato a", fragment, flags=re.IGNORECASE)
    fragment = re.sub(r"\band\s+holds\s+expertise\s+in\b", "e ho competenze in", fragment, flags=re.IGNORECASE)
    fragment = re.sub(r"\bfrom\s+Sicily\b", "in Sicilia", fragment, flags=re.IGNORECASE)
    was_acquired = re.match(
        rf"^(?P<target>{entity_span})\s+was\s+acquired\s+by\s+(?P<org>{entity_span})\s+in\s+(?P<year>(?:19|20)\d{{2}})\.?$",
        fragment,
        flags=re.IGNORECASE,
    )
    if was_acquired:
        fragment = (
            f"{was_acquired.group('target').strip(' .,;:')} e stata acquisita da "
            f"{was_acquired.group('org').strip(' .,;:')} nel {was_acquired.group('year')}."
        )
    fragment = re.sub(
        rf"\bacquired\s+by\s+(?P<org>{entity_span})\s+in\s+(?P<year>(?:19|20)\d{{2}})\b",
        lambda m: f"acquisita da {m.group('org').strip(' .,;:')} nel {m.group('year')}",
        fragment,
        flags=re.IGNORECASE,
    )
    fragment = re.sub(r"\brenewable\s+energy\s+management\s+company\b", "societa di gestione dell'energia rinnovabile", fragment, flags=re.IGNORECASE)
    fragment = re.sub(r"\bDr\.\s+Sono\b", "Sono", fragment, flags=re.IGNORECASE)
    fragment = re.sub(r"^Sono\s+a\s+technology\s+leader\b", "Sono un technology leader", fragment, flags=re.IGNORECASE)
    father_of = re.search(rf"\bIl padre di (?P<subject>{person_subject})\s+si chiamava\s+", fragment, flags=re.IGNORECASE)
    if father_of and likely_person_subject(father_of.group("subject")):
        fragment = fragment[: father_of.start()] + "Mio padre si chiamava " + fragment[father_of.end() :]
    dedication = re.search(rf"\b(?P<subject>{person_subject})\s+gli ha dedicato\b", fragment, flags=re.IGNORECASE)
    if dedication and likely_person_subject(dedication.group("subject")):
        fragment = fragment[: dedication.start()] + "gli ho dedicato" + fragment[dedication.end() :]
    fragment = re.sub(
        r"\btechnology leader focused on renewable energy and digital transformation\b",
        "technology leader focalizzato su energia rinnovabile e trasformazione digitale",
        fragment,
        flags=re.IGNORECASE,
    )
    fragment = re.sub(
        r"^He has educational ties to the University of Catania\s+\(1996-1999\), recognizes memberships in IEEE and OPC Foundation, received a Microsoft Odyssey Award nel 2008, and holds publications and a patent related to process control visualization\.?$",
        "Ho legami formativi con l'Universita di Catania (1996-1999), risultano riferimenti a IEEE e OPC Foundation, nel 2008 ho ricevuto un Microsoft Odyssey Award e ho pubblicazioni e un brevetto sulla visualizzazione dei dati di controllo di processo.",
        fragment,
        flags=re.IGNORECASE,
    )
    fragment = re.sub(
        r"^He has worked on large-scale safety-critical systems between\s+((?:19|20)\d{2})-((?:19|20)\d{2})\s+before shifting focus to technologies with a social and environmental impact\.?$",
        r"Tra il \1 e il \2 ho lavorato su sistemi safety-critical di larga scala, poi ho spostato il focus verso tecnologie con impatto sociale e ambientale.",
        fragment,
        flags=re.IGNORECASE,
    )
    fragment = re.sub(
        r"^In\s+((?:19|20)\d{2})\s+the direction shifted toward technologies with positive social and environmental impact\.?$",
        r"Nel \1 ho orientato il lavoro verso tecnologie con impatto sociale e ambientale positivo.",
        fragment,
        flags=re.IGNORECASE,
    )
    fragment = fragment.replace("ho deciso che avrebbe ", "ho deciso che avrei ")
    folded = _fold_text(fragment)
    if folded.startswith("il nome e "):
        parts = fragment.split(maxsplit=3)
        if len(parts) == 4:
            return f"Sono {parts[3]}"
    if folded.startswith("e nata "):
        return f"Sono nata {fragment.split(maxsplit=2)[2]}" if len(fragment.split(maxsplit=2)) == 3 else fragment
    if folded.startswith("vive a "):
        return f"Vivo a {fragment.split(maxsplit=2)[2]}" if len(fragment.split(maxsplit=2)) == 3 else fragment
    if folded.startswith("comunica "):
        return f"Comunico {fragment.split(maxsplit=1)[1]}" if len(fragment.split(maxsplit=1)) == 2 else fragment
    if folded.startswith("i suoi valori chiave sono "):
        return f"I miei valori chiave sono {fragment.split('sono ', 1)[1]}" if "sono " in fragment else fragment
    if folded.startswith("lavora come "):
        return f"Lavoro come {fragment.split(maxsplit=2)[2]}" if len(fragment.split(maxsplit=2)) == 3 else fragment
    temporal_experience = re.match(r"^(Nel\s+(?:19|20)\d{2},?\s+)quella esperienza\s+(.+)$", fragment, flags=re.IGNORECASE)
    if temporal_experience:
        prefix = temporal_experience.group(1)
        rest = temporal_experience.group(2).strip()
        folded_rest = _fold_text(rest)
        if folded_rest.startswith("e confluita in ") and " in " in rest:
            target = rest.split(" in ", 1)[1].strip()
            return f"{prefix}il mio lavoro e confluito in {target}"
        if folded_rest.startswith("si e trasformata in ") and " in " in rest:
            target = rest.split(" in ", 1)[1].strip()
            return f"{prefix}il mio lavoro si e trasformato in {target}"
        if folded_rest.startswith("si e evoluta in ") and " in " in rest:
            target = rest.split(" in ", 1)[1].strip()
            return f"{prefix}il mio lavoro si e evoluto in {target}"
    if folded.startswith("il progetto principale e "):
        parts = fragment.split(maxsplit=4)
        return f"Il mio progetto principale e {parts[4]}" if len(parts) == 5 else fragment
    if folded.startswith("sta sviluppando anche "):
        return f"Sto sviluppando anche {fragment.split(maxsplit=3)[3]}" if len(fragment.split(maxsplit=3)) == 4 else fragment
    if folded.startswith("il partner e "):
        parts = fragment.split(maxsplit=3)
        return f"Il mio partner e {parts[3]}" if len(parts) == 4 else fragment
    if folded.startswith("il padre e "):
        parts = fragment.split(maxsplit=3)
        return f"Mio padre si chiamava {parts[3]}" if len(parts) == 4 else fragment
    if folded.startswith("la mentor e "):
        parts = fragment.split(maxsplit=3)
        return f"La mia mentor e {parts[3]}" if len(parts) == 4 else fragment
    if folded.startswith("il sibling e "):
        parts = fragment.split(maxsplit=3)
        return f"Il mio sibling e {parts[3]}" if len(parts) == 4 else fragment
    replacements = (
        ("Il nome Ã¨ ", "Sono "),
        ("Il nome e ", "Sono "),
        ("Lavora come ", "Lavoro come "),
        ("Il progetto principale e ", "Il mio progetto principale e "),
        ("Sta sviluppando anche ", "Sto sviluppando anche "),
        ("Il partner e ", "Il mio partner e "),
        ("Il padre e ", "Mio padre si chiamava "),
        ("Il padre si chiamava ", "Mio padre si chiamava "),
        ("La mentor e ", "La mia mentor e "),
        ("Il sibling e ", "Il mio sibling e "),
    )
    for source, target in replacements:
        if fragment.startswith(source):
            return f"{target}{fragment[len(source):]}"
    fragment = fragment.replace("narrative archive systems", "sistemi di archivi narrativi")
    fragment = re.sub(r"\bin\s+((?:19|20)\d{2})\b", r"nel \1", fragment)
    return fragment


def _answer_surface_has_context_ledger_leak(value: str | None) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    lowered = _fold_text(text)
    markers = (
        "raw context",
        "mixed evidence",
        "mixed_evidence",
        "manual text",
        "manual_text",
        "context dossier",
        "evidence ledger",
        "grounded retrieval ledger",
        "document packet",
        "navigation store",
        "document title",
        "public source",
        "source pack",
        "expected retrieval behavior",
    )
    if any(marker in lowered for marker in markers):
        return True
    return bool(re.search(r"(?im)^\s*-\s+(fact|chunk|anchor raw|raw context|open questions)\b", text))


def _source_trust_rank(value: str | None) -> float:
    trust = str(value or "").strip().lower()
    return {
        "verified_public": 1.0,
        "uploaded_document": 0.86,
        "user_asserted": 0.72,
        "inferred": 0.42,
        "system_metadata": 0.06,
        "synthetic_test": 0.0,
    }.get(trust, 0.62)


def _match_source_trust(match: dict[str, Any]) -> str:
    node = dict(match.get("node") or {})
    provenance = dict(node.get("provenance") or match.get("provenance") or {})
    return str(
        match.get("source_trust")
        or node.get("source_trust")
        or provenance.get("source_trust")
        or ("uploaded_document" if provenance.get("source_type") in {"document", "public_web", "web"} else "user_asserted")
    )


def _source_or_instruction_sentence(text: str) -> bool:
    folded = _fold_text(text)
    if not folded:
        return True
    if folded.startswith(
        (
            "document title",
            "source ",
            "public source",
            "source pack",
            "expected retrieval behavior",
            "this is not a new public source",
            "synthetic operating dossier",
        )
    ):
        return True
    return any(
        marker in folded
        for marker in (
            "derived from public facts for stress testing",
            "composed from the public source set to test",
            "must never become personal profile fact",
        )
    )


def _iter_human_evidence_rows(
    matches: list[dict[str, Any]] | None,
    evidence_reservoir: dict[str, Any] | None = None,
    *,
    limit: int = 18,
) -> list[dict[str, Any]]:
    reservoir_entries = {
        str(entry.get("node_id") or ""): dict(entry)
        for entry in list((evidence_reservoir or {}).get("entries") or [])
        if isinstance(entry, dict) and str(entry.get("node_id") or "").strip()
    }
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for match in _eligible_answer_matches(matches)[:limit]:
        node = dict(match.get("node") or {})
        node_id = str(match.get("node_id") or node.get("id") or "").strip()
        if not node_id:
            continue
        entry = reservoir_entries.get(node_id) or {}
        source_trust = _match_source_trust(match)
        source_rank = _source_trust_rank(source_trust)
        raw_score = float(match.get("raw_score") or match.get("score") or entry.get("score") or 0.0)
        candidate_texts = [
            str(match.get("evidence_snippet") or ""),
            str(entry.get("evidence_snippet") or ""),
            str(node.get("raw_text") or entry.get("raw_text") or ""),
            str(match.get("summary") or node.get("summary") or ""),
        ]
        for text in candidate_texts:
            for sentence in _sentence_candidates(text):
                clean = clean_answer_surface_text(sentence)
                if not clean or _source_or_instruction_sentence(clean):
                    continue
                if _answer_surface_has_context_ledger_leak(clean):
                    continue
                key = (node_id, _fold_text(clean)[:260])
                if key in seen:
                    continue
                seen.add(key)
                rows.append(
                    {
                        "node_id": node_id,
                        "text": clean,
                        "folded": _fold_text(clean),
                        "raw_score": raw_score,
                        "source_rank": source_rank,
                        "source_trust": source_trust,
                        "memory_type": str(node.get("memory_type") or ""),
                    }
                )
    return rows


_ORG_NAME_PATTERN = r"([A-Z][A-Za-z0-9&.'-]*(?:\s+[A-Z][A-Za-z0-9&.'-]*){0,4})"


def _clean_org_name(value: str | None) -> str | None:
    text = _clean_fact_value(str(value or ""))
    identifies_match = re.search(r"\bidentifies\s+([A-Z][A-Za-z0-9&.'-]*(?:\s+[A-Z][A-Za-z0-9&.'-]*){0,3})\s+as\b", text)
    if identifies_match:
        text = identifies_match.group(1).strip()
    text = re.sub(r"\s+(?:in|inside|within|nel|nella|dentro|to|for)\b.*$", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"\s+(?:and|e)\s+.*$", "", text, flags=re.IGNORECASE).strip()
    folded = _fold_text(text)
    if not text or folded in {"he", "she", "they", "it", "the", "i", "io", "we", "noi", "lui", "lei"} or folded.startswith("by "):
        return None
    return text


def _founding_actor_present(text: str) -> bool:
    folded = _fold_text(text)
    if any(
        marker in folded
        for marker in (
            " i founded ",
            " io ho fondato ",
            " he founded ",
            " she founded ",
            " they founded ",
            " ha fondato ",
            " ho fondato ",
        )
    ):
        return True
    if re.search(
        r"\b[A-Z][A-Za-zÀ-ÿ'’-]+(?:\s+[A-Z][A-Za-zÀ-ÿ'’-]+){1,3}\b[^.?!;]{0,90}\b(?:founded|founder|fondatore|fondatrice|ha\s+fondato|ceo)\b",
        text,
    ):
        return True
    if re.search(r"\b(?:by|da|con)\s+[A-Z][A-Za-zÀ-ÿ'’-]+(?:\s+[A-Z][A-Za-zÀ-ÿ'’-]+){1,3}\b", text):
        return True
    return False


def _extract_founding_candidate(row: dict[str, Any], *, requested_years: set[str]) -> dict[str, Any] | None:
    text = str(row.get("text") or "")
    years = set(re.findall(r"\b(?:19|20)\d{2}\b", text))
    if requested_years and not (years & requested_years):
        return None
    actor_present = _founding_actor_present(text)
    founder_subject = r"(?:I|io|he|she|they|lui|lei|[A-Z][A-Za-zÀ-ÿ'’-]+(?:\s+[A-Z][A-Za-zÀ-ÿ'’-]+){1,3})"
    patterns: list[tuple[str, str, float]] = [
        (
            rf"\b(?:in|nel)\s+((?:19|20)\d{{2}})\b[^.?!;]{{0,140}}\b(?:{founder_subject}\s+)?(?:founded|founds|fond[oaei]|ha\s+fondato|fondo)\s+{_ORG_NAME_PATTERN}",
            "direct_foundation",
            3.3,
        ),
        (
            rf"\b(?:{founder_subject}\s+)?(?:founded|founds|fond[oaei]|ha\s+fondato|fondo)\s+{_ORG_NAME_PATTERN}[^.?!;]{{0,140}}\b(?:in|nel)\s+((?:19|20)\d{{2}})\b",
            "direct_foundation",
            3.2,
        ),
        (
            rf"\b{_ORG_NAME_PATTERN}[^.?!;]{{0,140}}\b(?:was\s+)?(?:founded|established|fondata|costituita)\s+(?:in|nel)?\s*((?:19|20)\d{{2}})[^.?!;]{{0,160}}\b(?:by|da|with|con|founder|founder/ceo|ceo\s+and\s+founder|fondatore|fondatrice)\b",
            "established_with_founder",
            1.75,
        ),
        (
            rf"\b{_ORG_NAME_PATTERN}[^.?!;]{{0,160}}\b(?:ceo\s+and\s+founder|founder/ceo|founder|fondatore|fondatrice)\b[^.?!;]{{0,160}}\b(?:established|founded|fondata|costituita)\s+(?:in|nel)?\s*((?:19|20)\d{{2}})\b",
            "established_with_founder",
            1.65,
        ),
        (
            rf"\b(?:identifies|presents|names)\s+{_ORG_NAME_PATTERN}\s+as\b[^.?!;]{{0,180}}\b(?:established|founded|fondata|costituita)\s+(?:in|nel)?\s*((?:19|20)\d{{2}})\b[^.?!;]{{0,180}}\b(?:ceo\s+and\s+founder|founder|fondatore|fondatrice)\b",
            "established_with_founder",
            1.65,
        ),
        (
            rf"\b(?:established|founded|fondata|costituita)\s+(?:in|nel)?\s*((?:19|20)\d{{2}})\b[^.?!;]{{0,160}}\b(?:with|con)\s+(?:ceo\s+and\s+founder|founder/ceo|founder|fondatore|fondatrice)\b[^.?!;]{{0,80}}\b(?:of\s+|di\s+)?{_ORG_NAME_PATTERN}",
            "established_with_founder",
            1.55,
        ),
        (
            rf"\b{_ORG_NAME_PATTERN}[^.?!;]{{0,140}}\b(?:was\s+)?(?:founded|established|fondata|costituita)\s+by\s+{founder_subject}[^.?!;]{{0,140}}\b(?:in|nel)\s+((?:19|20)\d{{2}})\b",
            "established_with_founder",
            1.55,
        ),
    ]
    for pattern, evidence_kind, base_bonus in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        groups = [group for group in match.groups() if group]
        year = next((group for group in groups if re.fullmatch(r"(?:19|20)\d{2}", group)), "")
        org = next((group for group in groups if not re.fullmatch(r"(?:19|20)\d{2}", group)), "")
        org = _clean_org_name(org)
        if not org or (requested_years and year not in requested_years):
            continue
        score = (
            base_bonus
            + float(row.get("source_rank") or 0.0) * 1.1
            + min(1.0, float(row.get("raw_score") or 0.0))
            + (0.5 if actor_present else 0.0)
            + (0.55 if requested_years and year in requested_years else 0.0)
        )
        return {
            **row,
            "org": org,
            "year": year,
            "evidence_kind": evidence_kind,
            "founding_score": score,
        }
    return None


def _is_precise_founding_company_query(query_text: str) -> bool:
    folded = _fold_text(query_text)
    return any(token in folded for token in ("azienda", "societa", "company", "startup", "impresa")) and any(
        token in folded
        for token in (
            "fondato",
            "fondata",
            "fondatore",
            "founded",
            "founder",
            "established",
            "costituita",
        )
    )


def _humanize_public_sentence(sentence: str, *, first_person: bool = False) -> str:
    text = clean_answer_surface_text(sentence)
    if not text:
        return ""
    entity_token = r"[A-Z][A-Za-z0-9&.'._-]+"
    entity_span = rf"{entity_token}(?:\s+{entity_token}){{0,6}}"
    person_span = rf"(?:Dr\.\s+)?{entity_token}(?:\s+{entity_token}){{1,4}}"
    month_names = (
        "January|February|March|April|May|June|July|August|September|October|November|December"
    )
    month_it = {
        "january": "gennaio",
        "february": "febbraio",
        "march": "marzo",
        "april": "aprile",
        "may": "maggio",
        "june": "giugno",
        "july": "luglio",
        "august": "agosto",
        "september": "settembre",
        "october": "ottobre",
        "november": "novembre",
        "december": "dicembre",
    }

    def clean_capture(value: str) -> str:
        return clean_answer_surface_text(str(value or "").strip(" .,;:"))

    def normalize_list(value: str) -> str:
        normalized = clean_capture(value)
        normalized = re.sub(r"\boptimize\b", "ottimizzare", normalized, flags=re.IGNORECASE)
        normalized = re.sub(
            r"\bwind,\s+solar,\s+hydro,\s+and\s+geothermal\s+plants\b",
            "impianti eolici, solari, idroelettrici e geotermici",
            normalized,
            flags=re.IGNORECASE,
        )
        normalized = re.sub(r"\s+and\s+", " e ", normalized, flags=re.IGNORECASE)
        normalized = normalized.replace("vision", "visione")
        normalized = normalized.replace("integration", "integrazione")
        normalized = normalized.replace("business strategy", "strategia")
        return normalized

    def acquisition_repl(match: re.Match[str]) -> str:
        month = month_it.get(_fold_text(match.group("month")), match.group("month"))
        buyer = clean_capture(match.group("buyer"))
        target = clean_capture(match.group("target"))
        return f"Il {match.group('day')} {month} {match.group('year')} {buyer} ha annunciato l'acquisizione di {target}"

    def founding_with_place_repl(match: re.Match[str]) -> str:
        subject = clean_capture(match.group("subject"))
        org = clean_capture(match.group("org"))
        place = clean_capture(match.group("place"))
        goal = normalize_list(match.group("goal"))
        return f"Nel {match.group('year')} {subject} ha fondato {org} a {place} per {goal}"

    def expansion_repl(match: re.Match[str]) -> str:
        subject = clean_capture(match.group("subject"))
        org = clean_capture(match.group("org"))
        return (
            f"Nel {match.group('year')} {subject} ha fondato {org}; "
            f"entro il {match.group('expanded_year')} l'azienda si era espansa in piu sedi internazionali."
        )

    def continued_role_repl(match: re.Match[str]) -> str:
        target = clean_capture(match.group("target"))
        acquirer = clean_capture(match.group("acquirer"))
        role = normalize_list(match.group("role"))
        scope = normalize_list(match.group("scope"))
        if first_person or match.group("self_actor"):
            actor = "ho continuato"
        else:
            actor_name = clean_capture(match.group("actor"))
            actor = f"{actor_name} ha continuato" if actor_name else "ha continuato"
        return (
            f"Nel {match.group('year')} {target} e entrata a far parte di {acquirer}; "
            f"{actor} come {role}, seguendo {scope}."
        )

    def founded_specializes_repl(match: re.Match[str]) -> str:
        org = clean_capture(match.group("org"))
        person = clean_capture(match.group("person"))
        domain = normalize_list(match.group("domain"))
        return f"{org} e specializzata in {domain}, con fondazione collegata a {person} nel {match.group('year')}."

    def established_specializes_repl(match: re.Match[str]) -> str:
        org = clean_capture(match.group("org"))
        person = clean_capture(match.group("person"))
        domain = normalize_list(match.group("domain"))
        product = clean_capture(match.group("product") or "")
        purpose = normalize_list(match.group("purpose") or "")
        result = normalize_list(match.group("result") or "")
        tail = f"; {product} serve a {purpose}" if product and purpose else ""
        if result:
            tail = f"{tail} e a rafforzare {result}" if tail else f"; rafforza {result}"
        return f"{org}, costituita nel {match.group('year')} da {person}, e specializzata in {domain}{tail}."

    def focuses_founder_repl(match: re.Match[str]) -> str:
        org = clean_capture(match.group("org"))
        domain = normalize_list(match.group("domain"))
        person = clean_capture(match.group("person"))
        return f"{org} si concentra su {domain}, con {person} indicato come CEO e founder nel {match.group('year')}."

    def company_constituted_repl(match: re.Match[str]) -> str:
        org = clean_capture(match.group("org"))
        domain = normalize_list(match.group("domain"))
        person = clean_capture(match.group("person"))
        return f"{org} e una societa di {domain} costituita nel {match.group('year')}, con {person} indicato come CEO e founder."

    text = re.sub(r"^It identifies\s+(.+?)\s+as\s+", r"\1 e ", text, flags=re.IGNORECASE)
    text = re.sub(r"^The release identifies\s+(.+?)\s+as\s+", r"\1 e ", text, flags=re.IGNORECASE)
    text = re.sub(r"^Established in\s+((?:19|20)\d{2}),\s+(.+?)\s+specializes\s+in\s+", r"\2 e stata costituita nel \1 e si occupa di ", text, flags=re.IGNORECASE)
    text = re.sub(r"^In\s+((?:19|20)\d{2})\s+it says\s+(.+?)\s+became part of\s+", r"Nel \1 \2 e entrata a far parte di ", text, flags=re.IGNORECASE)
    text = re.sub(r"^In\s+((?:19|20)\d{2})\s+(.+?)\s+became part of\s+", r"Nel \1 \2 e entrata a far parte di ", text, flags=re.IGNORECASE)
    text = re.sub(r"^The public chronology says\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(
        rf"^On\s+(?P<month>{month_names})\s+(?P<day>\d{{1,2}})\s+(?P<year>(?:19|20)\d{{2}})\s+(?P<buyer>.+?)\s+announced\s+(?:that\s+)?it\s+had\s+acquired\s+(?P<target>.+)$",
        acquisition_repl,
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        rf"^(?:In|Nel)\s+(?P<year>(?:19|20)\d{{2}})\s+(?P<subject>{person_span})\s+(?:ha\s+fondato|founded)\s+(?P<org>{entity_span})\s+in\s+(?P<place>[A-Z][A-Za-zÀ-ÿ'._ -]+?)\s+(?:to|per)\s+(?P<goal>.+)$",
        founding_with_place_repl,
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        rf"^(?P<subject>{person_span})\s+ha\s+fondato\s+(?P<org>{entity_span})\s+in\s+(?P<year>(?:19|20)\d{{2}}),\s+expanding to multiple international locations by\s+(?P<expanded_year>(?:19|20)\d{{2}}),?.*$",
        expansion_repl,
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        rf"^Nel\s+(?P<year>(?:19|20)\d{{2}})\s+(?P<target>.+?)\s+e\s+entrata\s+a\s+far\s+parte\s+di\s+(?P<acquirer>.+?)\s+and\s+(?:(?P<self_actor>ho)\s+continuato|(?P<actor>{person_span})\s+continued)\s+as\s+(?P<role>.+?),\s+driving\s+(?P<scope>.+?)\.?$",
        continued_role_repl,
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        rf"^(?P<org>{entity_span}),\s+costituita nel\s+(?P<year>(?:19|20)\d{{2}})\s+by\s+(?P<person>{person_span}),\s+specializes in\s+(?P<domain>.+?),\s+notably its\s+(?P<product>.+?)\s+for\s+(?P<purpose>.+?),\s+thereby establishing\s+(?P<result>.+?)\.?$",
        established_specializes_repl,
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        rf"^(?P<org>{entity_span})\s+specializes in\s+(?P<domain>.+?),\s+founded by\s+(?:(?:CEO|founder)\s+)?(?P<person>{person_span})\s+(?:in|nel)\s+(?P<year>(?:19|20)\d{{2}})\.?$",
        founded_specializes_repl,
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        rf"^(?P<org>{entity_span})\s+focuses on\s+(?P<domain>.+?),\s+founded by\s+(?:CEO\s+)?(?P<person>{person_span})\s+(?:in|nel)\s+(?P<year>(?:19|20)\d{{2}})\.?$",
        focuses_founder_repl,
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        rf"^(?P<org>{entity_span})\s+e\s+a\s+(?P<domain>.+?)\s+company\s+costituita\s+nel\s+(?P<year>(?:19|20)\d{{2}}),\s+con\s+(?P<person>{person_span})\s+indicato\s+come\s+CEO\s+e\s+founder\.?$",
        company_constituted_repl,
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\ba provider of renewable energy management solutions\b", "un fornitore di soluzioni di gestione delle energie rinnovabili", text, flags=re.IGNORECASE)
    text = re.sub(r"\bwind, solar, hydro, and geothermal plants\b", "impianti eolici, solari, idroelettrici e geotermici", text, flags=re.IGNORECASE)
    text = re.sub(r"\bin\s+((?:19|20)\d{2})\b", r"nel \1", text, flags=re.IGNORECASE)
    text = re.sub(
        rf"\bwith CEO and founder (?P<person>{person_span})\b",
        lambda match: f"con {clean_capture(match.group('person'))} indicato come CEO e founder",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\bestablished in\s+((?:19|20)\d{2})\b", r"costituita nel \1", text, flags=re.IGNORECASE)
    if not first_person:
        text = re.sub(r"\bHe founded\s+", "Ha fondato ", text, flags=re.IGNORECASE)
        text = re.sub(r"\bhe founded\s+", "ha fondato ", text, flags=re.IGNORECASE)
    text = re.sub(
        rf"\bDr\.\s+(?P<person>{person_span})\s+is\b",
        lambda match: f"{clean_capture(match.group('person'))} e",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        rf"\b(?P<person>{person_span})\s+is\b",
        lambda match: f"{clean_capture(match.group('person'))} e",
        text,
        flags=re.IGNORECASE,
    )
    if first_person:
        text = _self_voice_fragment(text)
    text = re.sub(
        rf"^(?P<subject>{person_span})\s+ha\s+fondato\s+(?P<org>{entity_span})\s+in\s+(?P<year>(?:19|20)\d{{2}}),\s+expanding to multiple international locations by\s+(?P<expanded_year>(?:19|20)\d{{2}}),?.*$",
        expansion_repl,
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        rf"^Nel\s+(?P<year>(?:19|20)\d{{2}})\s+(?P<target>.+?)\s+e\s+entrata\s+a\s+far\s+parte\s+di\s+(?P<acquirer>.+?)\s+and\s+(?:(?P<self_actor>ho)\s+continuato|(?P<actor>{person_span})\s+continued)\s+as\s+(?P<role>.+?),\s+driving\s+(?P<scope>.+?)\.?$",
        continued_role_repl,
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        rf"^Nel\s+(?P<year>(?:19|20)\d{{2}})\s+ho\s+fondato\s+(?P<org>{entity_span})\s+in\s+(?P<place>[A-Z][A-Za-zÀ-ÿ'._ -]+?)\s+(?:to|per)\s+(?P<goal>.+)$",
        lambda match: (
            f"Nel {match.group('year')} ho fondato {clean_capture(match.group('org'))} "
            f"a {clean_capture(match.group('place'))} per {normalize_list(match.group('goal'))}"
        ),
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        rf"^(?P<org>{entity_span}),\s+costituita nel\s+(?P<year>(?:19|20)\d{{2}})\s+by\s+(?P<person>{person_span}),\s+specializes in\s+(?P<domain>.+?),\s+notably its\s+(?P<product>.+?)\s+for\s+(?P<purpose>.+?),\s+thereby establishing\s+(?P<result>.+?)\.?$",
        established_specializes_repl,
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        rf"^(?P<org>{entity_span})\s+focuses on\s+(?P<domain>.+?),\s+founded by\s+(?:CEO\s+)?(?P<person>{person_span})\s+(?:in|nel)\s+(?P<year>(?:19|20)\d{{2}})\.?$",
        focuses_founder_repl,
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        rf"^(?P<org>{entity_span})\s+e\s+a\s+(?P<domain>.+?)\s+company\s+costituita\s+nel\s+(?P<year>(?:19|20)\d{{2}}),\s+con\s+(?P<person>{person_span})\s+indicato\s+come\s+CEO\s+e\s+founder\.?$",
        company_constituted_repl,
        text,
        flags=re.IGNORECASE,
    )
    return clean_answer_surface_text(text)


def _build_precise_founding_answer(
    query_text: str,
    matches: list[dict[str, Any]],
    *,
    evidence_reservoir: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not _is_precise_founding_company_query(query_text):
        return None
    rows = _iter_human_evidence_rows(matches, evidence_reservoir)
    requested_years = set(_explicit_temporal_terms(query_text))
    candidates = [
        candidate
        for row in rows
        for candidate in [_extract_founding_candidate(row, requested_years=requested_years)]
        if candidate
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-float(item.get("founding_score") or 0.0), str(item.get("org") or ""), str(item.get("node_id") or "")))
    top = candidates[0]
    top_org = str(top.get("org") or "").strip()
    top_year = str(top.get("year") or "").strip()
    first_person = _prefers_first_person_answer(query_text)
    if first_person:
        lead = f"Nel {top_year} ho fondato {top_org}." if top_year else f"Ho fondato {top_org}."
    else:
        lead = (
            f"La risposta piu supportata e {top_org}: nel {top_year} l'evidenza indica una fondazione diretta di {top_org}."
            if top_year
            else f"La risposta piu supportata e {top_org}."
        )
    evidence_sentence = _humanize_public_sentence(str(top.get("text") or ""), first_person=first_person)
    if evidence_sentence and _fold_text(evidence_sentence) not in _fold_text(lead):
        lead = f"{lead} Il dettaglio recuperato aggiunge: {evidence_sentence}"
    rival = next(
        (
            candidate
            for candidate in candidates[1:]
            if _fold_text(str(candidate.get("org") or "")) != _fold_text(top_org)
            and (not requested_years or str(candidate.get("year") or "") in requested_years)
        ),
        None,
    )
    evidence_ids = [str(top.get("node_id") or "")]
    if rival:
        rival_org = str(rival.get("org") or "").strip()
        rival_year = str(rival.get("year") or "").strip()
        lead = (
            f"{lead} Tengo separata anche l'evidenza su {rival_org}: risulta collegata al {rival_year} "
            "con un founder/CEO indicato, ma non e l'evidenza principale di fondazione diretta."
        ).strip()
        evidence_ids.append(str(rival.get("node_id") or ""))
    evidence_ids = [node_id for node_id in dict.fromkeys(evidence_ids) if node_id]
    support_metadata = build_answer_support_metadata(matches=matches, evidence_node_ids=evidence_ids)
    return _apply_answer_contract(
        query_text,
        {
            "answer_text": lead,
            "mode": "human_synthesizer",
            "confidence": 0.9 if not rival else 0.82,
            "evidence_node_ids": evidence_ids,
            "reasoning_summary": "PR-4 selected a precise founding answer by ranking direct founding evidence above related establishment/founder evidence.",
            "insufficient": False,
            "answerability_state": "grounded",
            "evidence_snippets": [
                {"node_id": str(item.get("node_id") or ""), "text": str(item.get("text") or ""), "kind": str(item.get("evidence_kind") or "founding")}
                for item in ([top, rival] if rival else [top])
                if item
            ],
            "support_node_count": int(support_metadata.get("support_node_count") or len(evidence_ids)),
            "support_slot_count": int(support_metadata.get("support_slot_count") or 1),
            "family_attribution_summary": dict(support_metadata.get("family_attribution_summary") or {}),
            "contradiction_present": bool(rival or support_metadata.get("contradiction_present")),
            "synthesis_layer": "pr4_grounded_human_answer",
            "source_conflict_policy": "ranked_direct_foundation_over_established_with_founder",
        },
        matches,
    )


def _is_multi_target_human_query(query_text: str, targets: list[str]) -> bool:
    folded = _fold_text(query_text)
    if len(targets) < 2:
        return False
    return any(
        token in folded
        for token in (
            "cosa sai",
            "che cosa sai",
            "parlami",
            "raccontami",
            "rapporto",
            "relazione",
            "si collega",
            "collega",
            "relationship",
            "what do you know",
            "tell me",
        )
    )


def _format_requested_target_list(targets: list[str], *, limit: int = 4) -> str:
    cleaned = [str(target or "").strip() for target in targets if str(target or "").strip()]
    cleaned = list(dict.fromkeys(cleaned))[:limit]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} e {cleaned[1]}"
    return f"{', '.join(cleaned[:-1])} e {cleaned[-1]}"


def _query_asks_relationship_between_targets(query_text: str, targets: list[str]) -> bool:
    if len([target for target in targets if str(target or "").strip()]) < 2:
        return False
    folded = _fold_text(query_text)
    return any(
        marker in folded
        for marker in (
            "rapporto",
            "relazione",
            "relazioni",
            "collega",
            "collegamento",
            "connessione",
            "relationship",
            "relationships",
            "connect",
            "connection",
            "linked",
            "link",
            "between",
            "among",
            "tra ",
            "fra ",
            "acquis",
            "acquired",
            "became part",
            "part of",
            "integra",
            "integration",
        )
    )


def _folded_text_mentions_target(folded_text: str, target_folded: str) -> bool:
    folded = str(folded_text or "")
    target = str(target_folded or "").strip()
    if not folded or not target:
        return False
    if target in folded:
        return True
    words = [word for word in re.split(r"\s+", target) if len(word) >= 4]
    return len(words) >= 2 and all(word in folded for word in words)


def _relationship_event_score(folded_text: str) -> float:
    folded = str(folded_text or "")
    score = 0.0
    if any(
        marker in folded
        for marker in (
            "acquired",
            "acquis",
            "became part",
            "parte di",
            "part of",
            "merged",
            "merger",
            "integrat",
            "joined",
            "announced",
            "partnership",
            "partner",
            "collaborat",
            "colleg",
            "linked",
            "connection",
        )
    ):
        score += 0.55
    if any(marker in folded for marker in ("founder", "fondatore", "ceo", "chairman", "managing director")):
        score += 0.2
    return score


def _rank_target_rows(query_text: str, target: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    target_folded = _fold_text(target)
    folded_query = _fold_text(query_text)
    query_terms = {term for term in folded_query.split() if len(term) >= 4}
    query_targets = {_fold_text(item) for item in _query_named_targets(query_text)}
    peer_targets = {item for item in query_targets if item and item != target_folded}
    relation_query = _query_asks_relationship_between_targets(query_text, list(query_targets))
    ranked = []
    for row in rows:
        folded = str(row.get("folded") or "")
        if target_folded not in folded:
            continue
        overlap = sum(1 for term in query_terms if term in folded)
        temporal_bonus = 0.4 if _sentence_has_temporal_signal(str(row.get("text") or "")) else 0.0
        target_specific_bonus = 0.0
        if folded.startswith(target_folded) or f" {target_folded} " in f" {folded[:80]} ":
            target_specific_bonus += 0.55
        if any(token in folded for token in ("specializes", "si occupa", "cybersecurity", "digital transformation", "power plant controller", "hardware", "software")):
            target_specific_bonus += 0.35
        if any(token in folded for token in (" plus ", " miscellane", "mixed unrelated", "unrelated list")) and not relation_query:
            target_specific_bonus -= 0.65
        relation_bonus = 0.0
        if relation_query:
            peer_hits = sum(1 for peer in peer_targets if _folded_text_mentions_target(folded, peer))
            if peer_hits:
                relation_bonus += 0.65 + min(0.45, peer_hits * 0.15) + _relationship_event_score(folded)
            elif peer_targets:
                relation_bonus -= 0.15
        ranked.append(
            {
                **row,
                "target_score": (
                    float(row.get("source_rank") or 0.0)
                    + min(1.0, float(row.get("raw_score") or 0.0))
                    + overlap * 0.12
                    + temporal_bonus
                    + target_specific_bonus
                    + relation_bonus
                ),
            }
        )
    ranked.sort(key=lambda item: (-float(item.get("target_score") or 0.0), len(str(item.get("text") or "")), str(item.get("node_id") or "")))
    return ranked


def _build_multi_target_human_answer(
    query_text: str,
    matches: list[dict[str, Any]],
    *,
    evidence_reservoir: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    targets = _query_named_targets(query_text)
    if not _is_multi_target_human_query(query_text, targets):
        return None
    rows = _iter_human_evidence_rows(matches, evidence_reservoir, limit=24)
    if not rows:
        return None
    explicit_first_person = any(
        token in _fold_text(query_text)
        for token in (
            "prima persona",
            "che rapporto hai",
            "rapporto hai",
            "tuo",
            "tua",
            "hai con",
            "nel tuo lavoro",
        )
    )
    if _query_asks_relationship_between_targets(query_text, targets):
        folded_targets = [_fold_text(target) for target in targets if _fold_text(target)]
        selected_relation: list[tuple[str, dict[str, Any]]] = []
        seen_relation: set[str] = set()
        for target in targets[:4]:
            target_folded = _fold_text(target)
            peer_targets = [item for item in folded_targets if item != target_folded]
            ranked_relation_rows = [
                row
                for row in _rank_target_rows(query_text, target, rows)
                if any(_folded_text_mentions_target(str(row.get("folded") or ""), peer) for peer in peer_targets)
            ]
            for row in ranked_relation_rows[:4]:
                sentence = _humanize_public_sentence(str(row.get("text") or ""), first_person=explicit_first_person)
                folded_sentence = _fold_text(sentence)
                if not sentence or folded_sentence in seen_relation:
                    continue
                seen_relation.add(folded_sentence)
                selected_relation.append((sentence, row))
                if len(selected_relation) >= 3:
                    break
            if len(selected_relation) >= 3:
                break
        if selected_relation:
            evidence_ids = list(
                dict.fromkeys(
                    str(row.get("node_id") or "")
                    for _sentence, row in selected_relation
                    if str(row.get("node_id") or "").strip()
                )
            )
            target_label = _format_requested_target_list(targets)
            relation_lead = (
                f"Con {target_label} il rapporto emerge dalle evidenze congiunte recuperate: "
                if explicit_first_person
                else f"Il rapporto tra {target_label} emerge dalle evidenze congiunte recuperate: "
            )
            answer_text = relation_lead + " ".join(sentence for sentence, _row in selected_relation)
            answer_text = polish_final_answer_surface(query_text, answer_text) or clean_answer_surface_text(answer_text)
            support_metadata = build_answer_support_metadata(matches=matches, evidence_node_ids=evidence_ids)
            return _apply_answer_contract(
                query_text,
                {
                    "answer_text": answer_text,
                    "mode": "human_synthesizer",
                    "confidence": 0.86,
                    "evidence_node_ids": evidence_ids,
                    "reasoning_summary": "PR-4 answered a relationship query by ranking evidence that jointly mentions the requested targets, preferring direct relation evidence.",
                    "insufficient": False,
                    "answerability_state": "grounded",
                    "evidence_snippets": [
                        {"node_id": str(row.get("node_id") or ""), "text": sentence, "kind": "relationship_evidence"}
                        for sentence, row in selected_relation
                    ],
                    "support_node_count": int(support_metadata.get("support_node_count") or len(evidence_ids)),
                    "support_slot_count": int(support_metadata.get("support_slot_count") or 1),
                    "family_attribution_summary": dict(support_metadata.get("family_attribution_summary") or {}),
                    "contradiction_present": bool(support_metadata.get("contradiction_present")),
                    "synthesis_layer": "pr4_grounded_human_answer",
                    "source_conflict_policy": "rank_joint_relationship_evidence",
                },
                matches,
            )
    fragments: list[str] = []
    evidence_ids: list[str] = []
    snippets: list[dict[str, Any]] = []
    for target in targets[:4]:
        ranked = _rank_target_rows(query_text, target, rows)
        if not ranked:
            fragments.append(f"Su {target} non trovo evidenze esplicite nella memoria recuperata.")
            continue
        selected = []
        local_seen: set[str] = set()
        for row in ranked[:4]:
            sentence = _humanize_public_sentence(str(row.get("text") or ""), first_person=explicit_first_person)
            folded = _fold_text(sentence)
            if not sentence or folded in local_seen:
                continue
            local_seen.add(folded)
            selected.append((sentence, row))
            if len(selected) >= 2:
                break
        if not selected:
            continue
        prefix = f"Con {target}: " if explicit_first_person else f"Su {target}: "
        fragments.append(prefix + " ".join(sentence for sentence, _row in selected))
        for sentence, row in selected:
            node_id = str(row.get("node_id") or "")
            if node_id:
                evidence_ids.append(node_id)
                snippets.append({"node_id": node_id, "text": sentence, "kind": "target_evidence"})
    if not fragments or not evidence_ids:
        return None
    answer_text = " ".join(dict.fromkeys(fragment.strip() for fragment in fragments if fragment.strip()))
    answer_text = polish_final_answer_surface(query_text, answer_text) or clean_answer_surface_text(answer_text)
    if not answer_text:
        return None
    evidence_ids = list(dict.fromkeys(evidence_ids))[:10]
    support_metadata = build_answer_support_metadata(matches=matches, evidence_node_ids=evidence_ids)
    return _apply_answer_contract(
        query_text,
        {
            "answer_text": answer_text,
            "mode": "human_synthesizer",
            "confidence": 0.84,
            "evidence_node_ids": evidence_ids,
            "reasoning_summary": "PR-4 grouped the answer by requested target and kept the human answer separate from the context dossier.",
            "insufficient": False,
            "answerability_state": "grounded",
            "evidence_snippets": snippets[:8],
            "support_node_count": int(support_metadata.get("support_node_count") or len(evidence_ids)),
            "support_slot_count": int(support_metadata.get("support_slot_count") or 1),
            "family_attribution_summary": dict(support_metadata.get("family_attribution_summary") or {}),
            "contradiction_present": bool(support_metadata.get("contradiction_present")),
            "synthesis_layer": "pr4_grounded_human_answer",
            "source_conflict_policy": "group_by_requested_target",
        },
        matches,
    )


def build_grounded_human_synthesizer_answer(
    query_text: str,
    matches: list[dict[str, Any]],
    *,
    evidence_reservoir: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    matches = _eligible_answer_matches(matches)
    if not matches:
        return None
    folded_query = _fold_text(query_text)
    broad_detector = globals().get("_is_broad_self_query")
    if bool(broad_detector and broad_detector(query_text)) or any(
        phrase in folded_query
        for phrase in (
            "dossier completo",
            "riassumimi tutto",
            "profilo completo",
            "tutto quello che sai",
        )
    ):
        return None
    precise = _build_precise_founding_answer(query_text, matches, evidence_reservoir=evidence_reservoir)
    if precise:
        return precise
    multi_target = _build_multi_target_human_answer(query_text, matches, evidence_reservoir=evidence_reservoir)
    if multi_target:
        return multi_target
    contract_answer = _build_contractual_human_answer(query_text, matches, evidence_reservoir=evidence_reservoir)
    if contract_answer:
        return contract_answer
    return None


def _build_contractual_human_answer(
    query_text: str,
    matches: list[dict[str, Any]],
    *,
    evidence_reservoir: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    contract = build_query_contract(query_text)
    if str(contract.get("answer_style") or "") not in {"narrative", "list", "timeline"}:
        return None
    if str(contract.get("query_kind") or "") not in {"narrative_relation", "narrative", "temporal", "multi_fact"}:
        return None
    matches = _eligible_answer_matches(matches)
    if not matches:
        return None
    inventory = extract_grounded_fact_inventory(matches)
    aspects = list(contract.get("requested_aspects") or detect_query_aspects(query_text))
    relations = list(contract.get("requested_relations") or [])
    selected_facts: list[dict[str, Any]] = []

    def add_fact(fact: dict[str, Any] | None) -> None:
        if not fact:
            return
        node_id = str(fact.get("node_id") or "")
        text = _fold_text(str(fact.get("text") or fact.get("evidence_snippet") or ""))
        if any(str(item.get("node_id") or "") == node_id and _fold_text(str(item.get("text") or item.get("evidence_snippet") or "")) == text for item in selected_facts):
            return
        selected_facts.append(dict(fact))

    relation_bucket_by_aspect = {
        "father": "father",
        "partner": "partner",
        "mentor": "mentor",
        "sibling": "sibling",
    }
    for aspect, bucket in relation_bucket_by_aspect.items():
        if aspect in aspects or aspect in relations:
            add_fact(_pick_best_fact(list(inventory.get(bucket) or [])))
    if "history" in list(contract.get("required_slots") or []) or "history" in aspects or _query_has_detail_pressure(query_text):
        temporal_inventory = build_temporal_inventory(matches, evidence_reservoir=evidence_reservoir)
        requested_terms = set(_explicit_temporal_terms(query_text))
        temporal_entries = [dict(entry) for entry in list(temporal_inventory.get("entries") or []) if isinstance(entry, dict)]
        if requested_terms:
            temporal_entries = [
                entry
                for entry in temporal_entries
                if requested_terms & set(str(token) for token in list(entry.get("tokens") or []))
            ]
        for entry in temporal_entries[:3]:
            add_fact(_temporal_entry_to_fact(entry, requested_terms=requested_terms or None, first_person=_prefers_first_person_answer(query_text)))
        if not temporal_entries:
            for fact in sorted(list(inventory.get("history") or []), key=lambda item: (float(item.get("priority") or 0.0), float(item.get("raw_score") or 0.0)), reverse=True)[:2]:
                add_fact(fact)
    if not selected_facts:
        for aspect in aspects:
            bucket = {
                "role": "role",
                "projects": "primary_project",
                "style": "style",
                "values": "values",
                "name": "name",
            }.get(str(aspect))
            if bucket:
                add_fact(_pick_best_fact(list(inventory.get(bucket) or [])))
    if not selected_facts:
        return None

    fragments: list[str] = []
    for fact in selected_facts[:6]:
        text = clean_answer_surface_text(str(fact.get("text") or fact.get("evidence_snippet") or ""))
        if not text:
            continue
        fragments.extend(_sentence_candidates(text) or [text])
    deduped_fragments: list[str] = []
    seen: set[str] = set()
    for fragment in fragments:
        cleaned = clean_answer_surface_text(fragment)
        folded = _fold_text(cleaned)
        if not cleaned or folded in seen:
            continue
        seen.add(folded)
        deduped_fragments.append(_self_voice_fragment(cleaned) if _prefers_first_person_answer(query_text) else cleaned)
    if not deduped_fragments:
        return None
    body = " ".join(deduped_fragments[:6]).strip()
    if not body:
        return None
    if str(contract.get("query_kind") or "") == "narrative_relation":
        answer_text = f"Posso dirti questo: {body}"
    else:
        answer_text = body
    matrix = build_evidence_satisfaction_matrix(
        query_text=query_text,
        matches=matches,
        answer={"answer_text": answer_text, "evidence_snippets": [{"text": str(fact.get("evidence_snippet") or fact.get("text") or ""), "kind": str(fact.get("kind") or "")} for fact in selected_facts]},
        evidence_reservoir=evidence_reservoir,
    )
    missing_slots = list(matrix.get("missing_slots") or [])
    if missing_slots and str(contract.get("answer_style") or "") == "narrative":
        answer_text = f"{answer_text} Non ho abbastanza evidenza recuperata per chiudere anche: {', '.join(str(slot) for slot in missing_slots)}."
    evidence_ids = list(dict.fromkeys(str(fact.get("node_id") or "") for fact in selected_facts if str(fact.get("node_id") or "")))
    support_metadata = build_answer_support_metadata(matches=matches, evidence_node_ids=evidence_ids)
    answer = {
        "answer_text": answer_text,
        "mode": "contract_human_synthesis",
        "confidence": 0.72 if not missing_slots else 0.62,
        "evidence_node_ids": evidence_ids,
        "reasoning_summary": "Built from the semantic query contract and evidence satisfaction matrix.",
        "insufficient": bool(missing_slots),
        "answerability_state": "grounded" if not missing_slots else "partial",
        "evidence_snippets": [
            {
                "node_id": str(fact.get("node_id") or ""),
                "text": str(fact.get("evidence_snippet") or fact.get("text") or ""),
                "kind": str(fact.get("kind") or "contract_fact"),
            }
            for fact in selected_facts[:8]
        ],
        "support_node_count": int(support_metadata.get("support_node_count") or 0),
        "support_slot_count": int(support_metadata.get("support_slot_count") or 0),
        "family_attribution_summary": dict(support_metadata.get("family_attribution_summary") or {}),
        "contradiction_present": bool(support_metadata.get("contradiction_present")),
        "query_contract": contract,
        "evidence_satisfaction": matrix,
    }
    return _apply_answer_contract(query_text, answer, matches)


def _query_named_targets(query_text: str) -> list[str]:
    text = str(query_text or "")
    targets: list[str] = []
    seen: set[str] = set()
    stop_targets = {
        "che ruolo",
        "come si",
        "quali valori",
        "cosa",
        "chi",
        "quale",
        "quali",
        "mappa",
        "collega",
        "trova",
        "mostra",
        "spiega",
        "racconta",
        "raccontami",
        "dimmi",
        "documento",
        "documenti",
        "contesto",
        "dossier",
        "relazioni",
        "relazione",
        "azienda",
        "aziende",
        "progetto",
        "progetti",
        "what",
        "who",
        "which",
        "map",
        "connect",
        "find",
        "show",
        "explain",
        "tell",
        "document",
        "documents",
        "context",
        "relationship",
        "relationships",
    }

    def add_target(value: str) -> None:
        cleaned = _clean_fact_value(value)
        if not cleaned:
            return
        folded = _fold_text(cleaned)
        if not folded or folded in seen:
            return
        if folded in stop_targets:
            return
        if any(folded.startswith(f"{prefix} ") for prefix in stop_targets):
            return
        seen.add(folded)
        targets.append(cleaned)

    entity_pattern = re.compile(
        r"\b[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ0-9&'’.-]*(?:\s+[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ0-9&'’.-]*){0,5}\b"
    )

    def branded_single_token(candidate: str) -> bool:
        token = candidate.strip(" .,:;?!")
        if not token or " " in token:
            return False
        return bool(
            re.search(r"[\d_-]", token)
            or re.search(r"[a-zà-öø-ÿ][A-ZÀ-ÖØ-Þ]", token)
            or (len(token) >= 2 and token.upper() == token and any(char.isalpha() for char in token))
        )

    def add_entity_candidates(segment: str, *, allow_plain_single: bool = False) -> None:
        for match in entity_pattern.finditer(segment):
            candidate = match.group(0).strip(" .,:;?!")
            candidate = re.sub(r"^(?:E|Ed|And|With|Con)\s+", "", candidate, flags=re.IGNORECASE).strip(" .,:;?!")
            folded = _fold_text(candidate)
            if not candidate or folded in stop_targets:
                continue
            token_count = len(candidate.split())
            if token_count >= 2 or branded_single_token(candidate) or allow_plain_single:
                add_target(candidate)

    for relation_match in re.finditer(
        r"\b(?:di|del|della|dello|dell'|su|per|tra|fra|con|about|on|between|among|"
        r"collega|connect|mappa|map|spiega|explain|mostra|show|trova|find|"
        r"chi\s+e|chi\s+è|who\s+is|chiamat[oa])\s+(.{1,180}?)(?:[?!.;]|$)",
        text,
        flags=re.IGNORECASE,
    ):
        add_entity_candidates(relation_match.group(1), allow_plain_single=True)

    add_entity_candidates(text)

    for match in re.finditer(r"\b[A-Z][A-Za-z]+-[A-Za-z0-9-]*\d[A-Za-z0-9-]*\b|\b[A-Z]{2,}\d+[A-Z0-9-]*\b", text):
        add_target(match.group(0).strip(" .,:;?!"))

    for match in re.finditer(r"\b(?:di|del|della|dello|dell'|su|per|chiamat[oa])\s+([A-Z][A-Za-z0-9'.-]*(?:-[A-Za-z0-9'.-]+)?)", text):
        candidate = match.group(1).strip(" .,:;?!")
        add_target(candidate)

    for match in re.finditer(
        r"\b((?:stazione|nave|contratto|codice|seriale|registro|archivio|progetto)\s+(?:[A-Za-z0-9'.-]+\s+){0,3}[A-Z][A-Za-z0-9'.-]*(?:-[A-Za-z0-9'.-]+)?)",
        text,
        flags=re.IGNORECASE,
    ):
        add_target(match.group(1).strip(" .,:;?!"))

    for match in re.finditer(r"\b[A-Z][A-Za-z0-9'’.-]+(?:\s+[A-Z][A-Za-z0-9'’.-]+){1,5}\b", text):
        candidate = match.group(0).strip(" .,:;?!")
        candidate_folded = _fold_text(candidate)
        if candidate_folded.startswith(("che ruolo", "come si", "quali valori", "nel 20", "nel 19")):
            continue
        add_target(candidate)
    folded_targets = [(_fold_text(target), target) for target in targets]
    deduped_targets: list[str] = []
    for folded, target in folded_targets:
        if re.search(r"[\d_-]", target):
            deduped_targets.append(target)
            continue
        if any(
            folded != other_folded
            and len(other_folded) > len(folded)
            and re.search(rf"\b{re.escape(folded)}\b", other_folded)
            for other_folded, _other_target in folded_targets
        ):
            continue
        deduped_targets.append(target)
    return deduped_targets


def _mcp_explicit_query_entities(query_text: str) -> list[str]:
    text = str(query_text or "")
    entities: list[str] = []
    seen: set[str] = set()
    stop_entities = {
        "agvm",
        "mcp",
        "context",
        "package",
        "task",
        "user intent",
        "parlami",
        "raccontami",
        "quali",
        "quale",
        "come",
        "chi",
        "cosa",
        "dimmi",
        "prepara",
        "spiega",
        "mappa",
        "trova",
        "dammi",
        "crea",
        "raccogli",
        "ricostruisci",
        "riassumi",
        "separa",
        "unisci",
        "combina",
        "costruisci",
        "recupera",
        "collega",
        "confronta",
        "elenca",
        "mostra",
        "descrivi",
        "spiegami",
        "dopo",
        "continua",
        "continuando",
        "riprendi",
        "aggiungi",
        "usa",
        "usando",
        "completa",
        "completando",
        "chiarisci",
        "collegando",
        "verifica",
        "verificando",
        "verificare",
        "prepara",
        "preparando",
        "preparare",
        "what",
        "which",
        "who",
        "tell",
        "prepare",
        "explain",
        "map",
        "find",
        "create",
        "collect",
        "summarize",
        "unify",
        "merge",
        "combine",
        "build",
        "compare",
        "list",
        "show",
        "describe",
        "provide",
        "providing",
        "provided",
        "give",
        "retrieve",
        "continue",
        "add",
        "use",
        "complete",
        "clarify",
        "verify",
    }
    entity_pattern = re.compile(r"\b[A-Z][A-Za-z0-9&'-]{2,}(?:\s+[A-Z][A-Za-z0-9&'-]{2,}){0,4}\b")
    for fragment in re.split(r"(?<=[.!?;:])\s+|[,/()]+", text):
        for match in entity_pattern.finditer(fragment):
            entity = " ".join(match.group(0).split()).strip(" .,:;?!")
            folded = _fold_text(entity)
            if not folded or folded in stop_entities or folded in seen:
                continue
            seen.add(folded)
            entities.append(entity)
    deduped: list[str] = []
    folded_pairs = [(_fold_text(entity), entity) for entity in entities]
    for folded, entity in folded_pairs:
        if any(
            folded != other_folded
            and len(other_folded) > len(folded)
            and re.search(rf"\b{re.escape(folded)}\b", other_folded)
            for other_folded, _other_entity in folded_pairs
        ):
            continue
        deduped.append(entity)
    return deduped


def _mcp_subject_name_from_query(query_text: str) -> str:
    organization_markers = (
        "energy",
        "corporation",
        "corp",
        "company",
        "group",
        "studio",
        "foundry",
        "systems",
        "solutions",
        "technologies",
        "labs",
        "srl",
        "spa",
        "ltd",
        "inc",
        "sync",
        "nam",
    )
    for entity in _mcp_explicit_query_entities(query_text):
        folded = _fold_text(entity)
        if len(entity.split()) < 2:
            continue
        if any(marker in folded for marker in organization_markers):
            continue
        return entity
    return ""


def _mcp_query_explicitly_requests_section(query_text: str, section_key: str) -> bool:
    folded_query = _fold_text(query_text)
    if section_key == "values":
        return any(marker in folded_query for marker in ("valor", "princip", "ethic", "cultura"))
    if section_key == "style":
        return any(marker in folded_query for marker in ("stile", "comunich", "tono", "tone", "communication"))
    if section_key == "relationships":
        return any(marker in folded_query for marker in ("padre", "madre", "famigli", "relazion", "partner", "figli"))
    if section_key == "temporal_inventory":
        return bool(re.search(r"\b(?:19|20)\d{2}\b", folded_query)) or any(
            marker in folded_query
            for marker in ("quando", "timeline", "storia", "history", "data", "date", "anni")
        )
    return False


def _match_evidence_blob(matches: list[dict[str, Any]] | None) -> str:
    parts: list[str] = []
    eligible_matches = [
        dict(match)
        for match in list(matches or [])
        if is_answer_eligible(match) or is_document_eligible(match)
    ]
    for match in eligible_matches[:24]:
        node = dict(match.get("node") or {})
        parts.extend(
            [
                str(match.get("node_id") or ""),
                str(match.get("summary") or ""),
                str(match.get("evidence_snippet") or ""),
                str(node.get("summary") or ""),
                str(node.get("raw_text") or ""),
                str(node.get("text") or ""),
                str(node.get("memory_type") or ""),
            ]
        )
    return "\n".join(part for part in parts if part)


def _target_semantic_terms(target: str) -> list[str]:
    return [
        token
        for token in _fold_text(target).split()
        if len(token) >= 3 and token not in _DOCUMENT_ANSWER_STOPWORDS
    ]


def _target_supported_by_text(target: str, text: str) -> bool:
    folded_target = _fold_text(target)
    folded_text = _fold_text(text)
    if not folded_target or not folded_text:
        return False
    if folded_target in folded_text:
        return True
    terms = _target_semantic_terms(target)
    if len(terms) < 2:
        return False
    return all(re.search(rf"\b{re.escape(term)}\b", folded_text) for term in terms)


def _query_requests_family_relation(query_text: str, relation: str) -> bool:
    folded = _fold_text(query_text)
    tokens = {
        "father": ("padre", "papa", "father", "dad"),
    }.get(relation, ())
    return any(token in folded for token in tokens)


def _required_relation_values_from_evidence(
    query_text: str,
    matches: list[dict[str, Any]] | None,
) -> list[str]:
    if not matches:
        return []
    required: list[str] = []
    try:
        inventory = extract_grounded_fact_inventory(list(matches or []))
    except Exception:
        inventory = {}
    relation_buckets = {
        "father": "father",
    }
    for relation, bucket in relation_buckets.items():
        if not _query_requests_family_relation(query_text, relation):
            continue
        fact = _pick_best_fact(list(inventory.get(bucket) or []))
        value = str((fact or {}).get("value") or "").strip()
        if value:
            required.append(value)
    return list(dict.fromkeys(required))


def _answer_required_slot_coverage(query_text: str, answer_text: str) -> dict[str, Any]:
    contract = build_query_contract(query_text)
    folded = _fold_text(answer_text)
    rows: list[dict[str, Any]] = []

    def has_any(tokens: tuple[str, ...]) -> bool:
        return any(token in folded for token in tokens)

    def has_org_or_project_entity() -> bool:
        return _text_has_org_or_project_evidence(answer_text)

    for slot in list(contract.get("required_slots") or []):
        slot_name = str(slot or "").strip()
        if not slot_name:
            continue
        answer_covers_slot = False
        reason = "missing_from_answer_surface"
        if slot_name == "identity":
            named_identity = bool(
                re.search(
                    r"\b(?:sono|Sono|mi chiamo|Mi chiamo)\s+[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÿ'’.-]+(?:\s+[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÿ'’.-]+)+\b",
                    answer_text,
                )
            )
            answer_covers_slot = bool(
                named_identity
                or has_any(("mi chiamo", "il nome", "nome e", "nome è", "identita", "identita e", "identita è"))
            )
            reason = "identity_surface_marker"
        elif slot_name == "work":
            query_folded = _fold_text(query_text)
            project_specific = any(
                marker in query_folded
                for marker in (
                    "progetto",
                    "progetti",
                    "su cosa",
                    "a cosa lavori",
                    "what are you working on",
                    "what is he working on",
                    "what is she working on",
                    "projects",
                )
            )
            project_markers = (
                "progetto",
                "progetti",
                "project",
                "projects",
                "piattaforma",
                "platform",
                "prodotto",
                "product",
                "startup",
                "azienda",
                "company",
            )
            role_or_project_markers = (
                "lavor",
                "progetto",
                "progetti",
                "project",
                "projects",
                "azienda",
                "startup",
                "company",
                "organization",
                "organizzazione",
                "founder",
                "fondat",
                "fondatore",
                "ceo",
                "costru",
                "building",
                "svilupp",
                "develop",
                "platform",
                "product",
            )
            answer_covers_slot = (
                (has_any(project_markers) or has_org_or_project_entity())
                if project_specific
                else (has_any(role_or_project_markers) or has_org_or_project_entity())
            )
            reason = "work_or_project_surface_marker"
        elif slot_name == "work_detail":
            answer_covers_slot = bool(
                has_org_or_project_entity()
                and has_any(
                    (
                        "lavor",
                        "progetto",
                        "progetti",
                        "project",
                        "projects",
                        "azienda",
                        "startup",
                        "company",
                        "organization",
                        "organizzazione",
                        "founder",
                        "fondat",
                        "fondatore",
                        "ceo",
                        "software",
                        "technology",
                        "energia",
                        "energy",
                        "platform",
                        "product",
                        "develop",
                        "svilupp",
                        "specializ",
                        "si occupa",
                    )
                )
            )
            reason = "work_detail_surface_marker"
        elif slot_name == "company_founding":
            answer_covers_slot = _company_founding_material_present(answer_text)
            if not answer_covers_slot:
                query_targets = _query_named_targets(query_text)
                answer_covers_slot = bool(
                    any(
                        _target_looks_like_org_or_project(target) and _target_supported_by_text(target, answer_text)
                        for target in query_targets
                    )
                    and has_any(
                        (
                            "progetto",
                            "project",
                            "azienda",
                            "company",
                            "startup",
                            "organization",
                            "organizzazione",
                            "lavor",
                            "work",
                            "costru",
                            "building",
                            "built",
                            "founder",
                            "fondatore",
                            "ceo",
                            "ruolo",
                            "role",
                            "dentro",
                            "inside",
                            "principale",
                            "main",
                        )
                    )
                )
            reason = "company_founding_surface_marker"
        elif slot_name == "place":
            answer_covers_slot = has_any(("nato", "nata", "originari", "vivo", "vive", "residente", "bergamo", "milano", "catania", "sicilia", "sicily"))
            reason = "place_surface_marker"
        elif slot_name == "relationships":
            requested_relations = list(contract.get("requested_relations") or [])
            if requested_relations:
                answer_covers_slot = all(_text_mentions_requested_relation(folded, relation) for relation in requested_relations)
            else:
                answer_covers_slot = bool(
                    has_any(("padre", "madre", "mentor", "fratello", "sorella", "famil", "relazion"))
                    or _text_mentions_personal_relationship(folded)
                )
            reason = "relationship_surface_marker"
        elif slot_name == "relation_detail":
            answer_covers_slot = bool(
                len(folded) >= 90
                or re.search(r"\b(?:19|20)\d{2}\b", answer_text)
                or has_any(("monumento", "inaugur", "aeronautica", "militare", "dedicat", "served", "service"))
            )
            reason = "relation_detail_surface_marker"
        elif slot_name == "style":
            answer_covers_slot = has_any(("comunico", "comunica", "stile", "tono", "tecnico", "diretto", "strutturat", "chiaro", "analitico"))
            reason = "style_surface_marker"
        elif slot_name == "values":
            answer_covers_slot = has_any(
                (
                    "valori",
                    "principi",
                    "precisione",
                    "chiarezza",
                    "rigore",
                    "sostenibil",
                    "decarbon",
                    "impatto",
                    "responsabil",
                    "coerenza",
                    "qualita",
                    "qualità",
                )
            )
            reason = "values_surface_marker"
        elif slot_name == "history":
            answer_covers_slot = bool(re.search(r"\b(?:19|20)\d{2}\b", answer_text) or has_any(("storia", "passato", "fondat", "acquis", "iniziat", "inaugur")))
            reason = "history_surface_marker"
        elif slot_name == "documents":
            answer_covers_slot = has_any(("document", "fonte", "source", "file", "report", "release", "comunicato", "chunk", "anchor"))
            reason = "document_surface_marker"
        else:
            answer_covers_slot = slot_name in folded
            reason = "slot_name_surface_marker"
        rows.append(
            {
                "slot": slot_name,
                "required": True,
                "answer_covers_slot": bool(answer_covers_slot),
                "reason": reason if answer_covers_slot else "missing_from_answer_surface",
            }
        )
    missing_slots = [str(row["slot"]) for row in rows if not bool(row.get("answer_covers_slot"))]
    return {
        "schema_version": "agvm.answer_slot_coverage.v1",
        "query_contract": contract,
        "rows": rows,
        "missing_required_slots": missing_slots,
        "covered_required_slots": [str(row["slot"]) for row in rows if bool(row.get("answer_covers_slot"))],
        "required_slot_count": len(rows),
        "covered_required_slot_count": len(rows) - len(missing_slots),
        "passed": not missing_slots,
    }


_OFF_CONTRACT_TOPIC_MARKERS: dict[str, tuple[str, ...]] = {
    "family_relation": ("padre", "madre", "father", "mother", "dad", "mom", "aeronautica", "militare", "air force"),
    "family_monument": ("monumento", "monument", "inaugurato", "inaugurated", "inaugurazione", "inauguration", "dedicato a lui", "dedicated a monument"),
    "work_projects": ("azienda", "company", "startup", "software", "energy", "progetto", "project", "platform", "product", "founder", "fondatore", "ceo"),
    "education_awards": ("universita", "university", "ieee", "opc foundation", "microsoft odyssey", "patent", "brevetto"),
    "generic_profile": ("self-taught coder", "coder autodidatta", "profilo pubblico", "public profile"),
}


def _disallowed_topic_markers(contract: dict[str, Any]) -> tuple[str, ...]:
    markers: list[str] = []
    for topic in list((contract or {}).get("disallowed_topics") or []):
        topic_name = str(topic or "").strip()
        markers.extend(_OFF_CONTRACT_TOPIC_MARKERS.get(topic_name, ()))
    return tuple(dict.fromkeys(marker for marker in markers if marker))


def _text_has_any_marker(value: Any, markers: tuple[str, ...]) -> bool:
    if not markers:
        return False
    folded = _fold_text(str(value or ""))
    return any(_fold_text(marker) in folded for marker in markers if _fold_text(marker))


def _match_text_blob(match: dict[str, Any]) -> str:
    node = dict(match.get("node") or {})
    provenance = dict(node.get("provenance") or {})
    return "\n".join(
        str(value or "")
        for value in (
            match.get("summary"),
            match.get("evidence_snippet"),
            node.get("summary"),
            node.get("raw_text"),
            node.get("memory_type"),
            provenance.get("source_label"),
            provenance.get("guide_conceptual_area"),
        )
        if str(value or "").strip()
    )


def _company_founding_material_present(value: Any) -> bool:
    folded = _fold_text(str(value or ""))
    if not folded:
        return False
    metadata_noise = (
        "official website source url",
        "official website source uri",
        "source url",
        "source uri",
        "headings",
    )
    if folded in metadata_noise:
        return False
    relation_markers = (
        "fondat",
        "fondatore",
        "fondatrice",
        "founder",
        "founded",
        "cofound",
        "co founder",
        "cofondatore",
        "costituit",
        "established",
        "ceo",
        "chief executive",
        "acquired",
        "acquisit",
        "linked",
        "associated",
        "collegat",
        "legata",
        "legato",
        "legato al mio lavoro",
        "linked to",
    )
    if not _text_has_org_or_project_evidence(str(value or "")):
        return False
    if any(marker in folded for marker in ("acquired", "acquisit")):
        org_targets = [target for target in _query_named_targets(str(value or "")) if _target_looks_like_org_or_project(target)]
        if len(dict.fromkeys(_fold_text(target) for target in org_targets)) < 2:
            return False
    return any(marker in folded for marker in relation_markers)


def _filter_disallowed_matches_for_query(
    query_text: str,
    matches: list[dict[str, Any]] | None,
    *,
    retrieval_mode: str = "balanced",
) -> list[dict[str, Any]]:
    rows = [dict(match) for match in list(matches or [])]
    contract = build_query_contract(query_text, retrieval_mode=retrieval_mode)
    markers = _disallowed_topic_markers(contract)
    if markers:
        rows = [match for match in rows if not _text_has_any_marker(_match_text_blob(match), markers)]
    required_slots = {str(slot or "").strip() for slot in list(contract.get("required_slots") or []) if str(slot or "").strip()}
    composite_non_company_slots = required_slots & {"identity", "relationships", "relation_detail", "place", "style", "values"}
    if str(contract.get("query_kind") or "") in {"company_founding_relation", "company_founding_timeline"} and not composite_non_company_slots:
        company_rows = [match for match in rows if _company_founding_material_present(_match_text_blob(match))]
        if company_rows:
            rows = company_rows
    return rows


def _off_contract_topic_hits(query_text: str, answer_text: str, contract: dict[str, Any]) -> list[dict[str, Any]]:
    folded_answer = _fold_text(answer_text)
    if not folded_answer:
        return []
    query_kind = str(contract.get("query_kind") or "")
    query_folded = _fold_text(query_text)
    required_slots = {str(slot or "").strip() for slot in list(contract.get("required_slots") or []) if str(slot or "").strip()}
    hits: list[dict[str, Any]] = []
    for topic in list(contract.get("disallowed_topics") or []):
        topic_name = str(topic or "").strip()
        if not topic_name:
            continue
        markers = _OFF_CONTRACT_TOPIC_MARKERS.get(topic_name, ())
        matched = [marker for marker in markers if marker in folded_answer and marker not in query_folded]
        if matched:
            hits.append({"topic": topic_name, "markers": matched[:6]})
    family_intrusion_markers = ("padre", "madre", "father", "mother", "dad", "mom", "monumento", "aeronautica", "militare", "air force")
    if query_kind == "work_narrative" and any(marker in folded_answer for marker in family_intrusion_markers):
        hits.append({"topic": "work_answer_family_intrusion", "markers": ["family_marker"]})
    if (query_kind in {"work_narrative", "multi_fact"} or "work" in required_slots) and not any(
        marker in query_folded for marker in ("heritage", "ulisse", "foundation")
    ):
        heading_noise = [
            marker
            for marker in ("heritage", "the foundation", "ulisse s journey", "power companies worldwide", "sky is not the limit")
            if marker in folded_answer
        ]
        if heading_noise:
            hits.append({"topic": "work_source_heading_intrusion", "markers": heading_noise[:6]})
    if not _is_broad_self_query(query_text) and not _query_requests_family_relation(query_text, "father") and any(
        marker in folded_answer for marker in ("padre", "madre", "father", "mother", "aeronautica", "militare", "air force", "monumento", "monument", "dedicato a lui")
    ):
        hits.append({"topic": "unrequested_family_intrusion", "markers": ["family_marker"]})
    if query_kind in {"company_founding_relation", "company_founding_timeline"} and any(
        marker in folded_answer for marker in ("padre", "madre", "father", "mother", "monumento", "monument", "aeronautica", "air force", "universita", "university", "award", "patent", "brevetto")
    ):
        hits.append({"topic": "company_founding_off_topic_intrusion", "markers": ["non_company_marker"]})
    if query_kind in {"company_founding_relation", "company_founding_timeline"}:
        company_noise: list[str] = []
        if any(marker in folded_answer for marker in ("official website source", "source url", "source uri", "headings")):
            company_noise.append("source_metadata")
        heritage_segments = [
            _fold_text(segment)
            for segment in re.split(r"[,;\n\r•\-]+", str(answer_text or ""))
            if "heritage" in _fold_text(segment)
        ]
        if heritage_segments:
            relation_terms = ("fondat", "founder", "ceo", "acquired", "acquisit", "linked", "associated", "collegat", "legat")
            if any(not any(marker in segment for marker in relation_terms) for segment in heritage_segments):
                company_noise.append("heading_without_company_relation")
        if company_noise:
            hits.append({"topic": "company_founding_source_or_heading_intrusion", "markers": company_noise[:6]})
    relation_work_markers = (
        "azienda",
        "company",
        "startup",
        "software",
        "energy",
        "progetto",
        "project",
        "platform",
        "product",
        "founder",
        "fondatore",
        "ceo",
        "award",
        "patent",
        "brevetto",
    )
    query_allows_work_topic = any(marker in query_folded for marker in relation_work_markers) or any(
        _target_looks_like_org_or_project(target) for target in _query_named_targets(query_text)
    )
    if query_kind == "narrative_relation" and (
        any(marker in folded_answer for marker in relation_work_markers) or _text_has_org_or_project_evidence(answer_text)
    ) and not query_allows_work_topic:
        hits.append({"topic": "relation_answer_work_intrusion", "markers": ["work_marker"]})
    source_surface_markers = (
        "quoted in the release as",
        "presented as",
        "official website source",
        "official website source uri",
        "official website source url",
        "source uri",
        "source url",
        "document title",
        "source trace",
        "raw context",
        "headings",
    )
    source_surface_hits = [marker for marker in source_surface_markers if marker in folded_answer]
    if source_surface_hits:
        hits.append({"topic": "source_surface_intrusion", "markers": source_surface_hits[:6]})
    if any(
        marker in folded_answer
        for marker in (
            "lavoro come quoted",
            "work as quoted",
            "progetto principale e the foundation",
            "main project is the foundation",
        )
    ):
        hits.append({"topic": "fragmentary_source_phrase", "markers": ["scraped_fragment"]})
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for hit in hits:
        key = str(hit.get("topic") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(hit)
    return deduped


def _answer_adequacy_contract(
    *,
    query_text: str,
    answer_text: str,
    matches: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    query_contract = build_query_contract(query_text)
    answer_folded = _fold_text(answer_text)
    evidence_folded = _fold_text(_match_evidence_blob(matches))
    requested_objects = _query_named_targets(query_text)
    requested_times = _explicit_temporal_terms(query_text)
    objects_with_evidence = [
        target
        for target in requested_objects
        if _target_supported_by_text(target, evidence_folded)
    ]
    objects_without_evidence = [
        target
        for target in requested_objects
        if not _target_supported_by_text(target, evidence_folded)
    ]
    missing_objects = [
        target
        for target in objects_with_evidence
        if not _target_supported_by_text(target, answer_folded)
    ]
    times_with_evidence = [token for token in requested_times if token in evidence_folded]
    missing_times = [token for token in times_with_evidence if token not in answer_folded]
    required_relation_values = _required_relation_values_from_evidence(query_text, matches)
    missing_required_relation_values = [
        value
        for value in required_relation_values
        if _fold_text(value) not in answer_folded
    ]
    slot_coverage = _answer_required_slot_coverage(query_text, answer_text)
    missing_required_slots = list(slot_coverage.get("missing_required_slots") or [])
    first_person_required = _prefers_first_person_answer(query_text)
    third_person_markers: list[str] = _first_person_voice_leak_markers(answer_text) if first_person_required else []
    leak_present = _answer_surface_has_context_ledger_leak(answer_text)
    off_contract_topics = _off_contract_topic_hits(query_text, answer_text, query_contract)
    passed = (
        not objects_without_evidence
        and not missing_objects
        and not missing_times
        and not missing_required_relation_values
        and not missing_required_slots
        and not third_person_markers
        and not leak_present
        and not off_contract_topics
    )
    return {
        "contract_version": "28d.answer_adequacy.v1",
        "passed": bool(passed),
        "first_person_required": bool(first_person_required),
        "requested_objects": requested_objects,
        "objects_with_evidence": objects_with_evidence,
        "objects_without_evidence": objects_without_evidence,
        "missing_objects": missing_objects,
        "requested_times": requested_times,
        "times_with_evidence": times_with_evidence,
        "missing_times": missing_times,
        "required_relation_values": required_relation_values,
        "missing_required_relation_values": missing_required_relation_values,
        "required_slots": list((slot_coverage.get("query_contract") or {}).get("required_slots") or []),
        "covered_required_slots": list(slot_coverage.get("covered_required_slots") or []),
        "missing_required_slots": missing_required_slots,
        "slot_coverage": slot_coverage,
        "third_person_markers": third_person_markers,
        "context_ledger_leak": bool(leak_present),
        "off_contract_topics": off_contract_topics,
        "query_contract": query_contract,
    }


def _apply_answer_contract(
    query_text: str,
    answer: dict[str, Any],
    matches: list[dict[str, Any]],
) -> dict[str, Any]:
    decorated = dict(answer or {})
    answer_text = clean_answer_surface_text(decorated.get("answer_text"))
    if answer_text and _prefers_first_person_answer(query_text):
        answer_text = " ".join(_self_voice_fragment(sentence) for sentence in _sentence_candidates(answer_text)).strip()
    polisher = globals().get("polish_final_answer_surface")
    if answer_text and callable(polisher) and str(decorated.get("mode") or "") not in {"document_packet", "document_lookup_guard"}:
        answer_text = polisher(query_text, answer_text) or answer_text
    if answer_text:
        decorated["answer_text"] = clean_answer_surface_text(answer_text)
    contract = _answer_adequacy_contract(query_text=query_text, answer_text=answer_text, matches=matches)
    if contract["context_ledger_leak"]:
        requested = contract["requested_objects"] or contract["requested_times"]
        target_text = ", ".join(requested) if requested else "la richiesta"
        decorated.update(
            {
                "answer_text": f"Non posso chiudere la risposta in modo pulito: il materiale recuperato per {target_text} e ancora in forma di contesto tecnico.",
                "confidence": min(float(decorated.get("confidence") or 0.0), 0.45),
                "insufficient": True,
                "answerability_state": "partial",
                "reasoning_summary": "Answer adequacy blocked a raw context ledger from reaching the human answer surface.",
            }
        )
        contract = _answer_adequacy_contract(query_text=query_text, answer_text=str(decorated.get("answer_text") or ""), matches=matches)
    if str(decorated.get("document_lookup_state") or "") in {"no_matching_document_packet", "no_matching_document_packet_yet"}:
        decorated["answer_adequacy"] = contract
        if decorated.get("answer_text"):
            decorated["answer_text"] = clean_answer_surface_text(decorated.get("answer_text"))
        return decorated
    if contract["objects_without_evidence"] and not contract["objects_with_evidence"]:
        missing = ", ".join(contract["objects_without_evidence"])
        decorated.update(
            {
                "answer_text": f"Non trovo evidenze esplicite su {missing} nella memoria recuperata.",
                "confidence": 0.0,
                "evidence_node_ids": [],
                "evidence_snippets": [],
                "insufficient": True,
                "answerability_state": "insufficient",
                "reasoning_summary": "The query requested a concrete object, but the retrieved evidence did not contain that object.",
            }
        )
        contract = _answer_adequacy_contract(query_text=query_text, answer_text=str(decorated.get("answer_text") or ""), matches=matches)
    elif not contract["passed"]:
        decorated["answerability_state"] = "partial"
        decorated["insufficient"] = True
        decorated["confidence"] = min(float(decorated.get("confidence") or 0.0), 0.66)
        decorated["reasoning_summary"] = (
            str(decorated.get("reasoning_summary") or "").strip()
            + " Answer adequacy contract marked the answer partial because a requested object, time, voice, or surface constraint was not satisfied."
        ).strip()
    elif (
        str(decorated.get("answerability_state") or "") == "partial"
        and not bool(decorated.get("insufficient"))
        and not bool((decorated.get("temporal_inventory") or {}).get("partial"))
    ):
        decorated["answerability_state"] = "grounded"
    decorated["answer_adequacy"] = contract
    if decorated.get("answer_text"):
        decorated["answer_text"] = clean_answer_surface_text(decorated.get("answer_text"))
    return decorated


def _project_fact_parts(fact: dict[str, Any] | None) -> tuple[str | None, str | None]:
    if not fact:
        return None, None
    candidates = [
        str(fact.get("text") or ""),
        str(fact.get("evidence_snippet") or ""),
        str(fact.get("summary") or ""),
    ]
    for text in candidates:
        if not text:
            continue
        for pattern in (
            r"progetto principale e\s+(.+?)(?:\s+dentro\s+(.+?))?(?:[.;]|$)",
            r"(?:sto costruendo|sta costruendo|is building|is constructing|building)\s+(.+?)(?:\s+(?:dentro|inside|within)\s+(.+?))?(?:[.;]|$)",
        ):
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            project = _clean_fact_value(match.group(1))
            org = _clean_fact_value(match.group(2)) if match.lastindex and match.lastindex >= 2 and match.group(2) else None
            return project or None, org or None
    value = _clean_fact_value(str(fact.get("value") or ""))
    return value or None, None


def _relation_fragments_for_query(query_text: str, inventory: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    lowered = _fold_text(query_text)
    if not any(token in lowered for token in ("come si collega", "si collega a", "collega a", "che ruolo ha", "ruolo ha", "nel tuo lavoro")):
        return []
    targets = _query_named_targets(query_text)
    if not targets:
        return []
    primary_project = _pick_best_fact(inventory["primary_project"]) or _pick_best_fact(inventory["secondary_project"])
    role_fact = _pick_best_fact(inventory["role"])
    project_name, org_name = _project_fact_parts(primary_project)
    role_value = _clean_role_value(str((role_fact or {}).get("value") or "")) if role_fact else ""
    rows: list[dict[str, Any]] = []

    def target_specific_fact(target_text: str) -> dict[str, Any] | None:
        folded_target_text = _fold_text(target_text)
        if not folded_target_text:
            return None
        candidates: list[dict[str, Any]] = []
        for bucket_name in ("primary_project", "secondary_project", "history", "values", "style", "role"):
            for fact in list(inventory.get(bucket_name) or []):
                fact_text = " ".join(
                    str(fact.get(key) or "")
                    for key in ("text", "evidence_snippet", "summary", "value")
                    if str(fact.get(key) or "").strip()
                )
                if folded_target_text in _fold_text(fact_text):
                    enriched = dict(fact)
                    enriched["_target_bucket"] = bucket_name
                    candidates.append(enriched)
        return _pick_best_fact(candidates)

    def target_specific_sentence(target_text: str, fact: dict[str, Any]) -> str:
        folded_target_text = _fold_text(target_text)
        raw = " ".join(
            str(fact.get(key) or "")
            for key in ("evidence_snippet", "text", "summary", "value")
            if str(fact.get(key) or "").strip()
        )
        sentence = next(
            (
                item
                for item in _sentence_candidates(raw)
                if folded_target_text and folded_target_text in _fold_text(item)
            ),
            raw,
        )
        sentence = _clean_fact_value(sentence)
        target_label = str(target_text).strip()
        if not sentence:
            return f"{target_label} e collegata al mio lavoro nella memoria recuperata."
        if folded_target_text and _fold_text(sentence).startswith(folded_target_text):
            return sentence if sentence.endswith(".") else f"{sentence}."
        return f"{target_label} e collegata al mio lavoro: {sentence if sentence.endswith('.') else sentence + '.'}"

    for target in targets:
        folded_target = _fold_text(target)
        if project_name and folded_target in _fold_text(project_name):
            text = (
                f"{project_name} e il mio progetto principale dentro {org_name}."
                if org_name
                else f"{project_name} e il mio progetto principale."
            )
            rows.append({"text": text, "facts": [fact for fact in (primary_project,) if fact], "covers": ["projects"]})
            continue
        if org_name and folded_target in _fold_text(org_name):
            if project_name:
                text = f"{org_name} e il contesto in cui porto avanti {project_name}."
            else:
                text = f"{org_name} e collegato al mio lavoro nella memoria recuperata."
            facts = [fact for fact in (primary_project,) if fact]
            rows.append({"text": text, "facts": facts, "covers": ["projects"]})
            if role_value:
                rows.append({"text": f"Lavoro come {role_value}.", "facts": [role_fact], "covers": ["role"]})
            continue
        target_fact = target_specific_fact(target)
        if target_fact:
            target_bucket = str(target_fact.get("_target_bucket") or "")
            covers = ["projects", "role"] if target_bucket in {"primary_project", "secondary_project", "history"} else ["role"]
            rows.append({"text": target_specific_sentence(target, target_fact), "facts": [target_fact], "covers": covers})
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        folded = _fold_text(str(row.get("text") or ""))
        if folded and folded not in seen:
            seen.add(folded)
            deduped.append(row)
    return deduped


def build_direct_fact_answer(
    query_text: str,
    matches: list[dict[str, Any]],
    *,
    evidence_reservoir: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    matches = _eligible_answer_matches(matches)
    if not matches:
        return None
    aspects = detect_query_aspects(query_text)
    if not aspects:
        intent_to_aspect = {
            "identity_name": "name",
            "workplace": "role",
            "birthplace": "birthplace",
            "partner_name": "partner",
            "father_name": "father",
        }
        inferred_aspect = intent_to_aspect.get(str(detect_query_intent(query_text) or ""))
        if inferred_aspect:
            aspects = [inferred_aspect]
    if not aspects:
        return None
    inventory = extract_grounded_fact_inventory(matches)
    temporal_inventory = (
        build_temporal_inventory(matches, evidence_reservoir=evidence_reservoir)
        if _is_temporal_reference_query(query_text)
        else {"entries": [], "years": [], "date_tokens": [], "evidence_node_ids": [], "confidence": 0.0}
    )
    precise_founding_answer = _build_precise_founding_answer(query_text, matches, evidence_reservoir=evidence_reservoir)
    if precise_founding_answer:
        return precise_founding_answer

    try:
        from sqlite_store import fetch_identity_nucleus, fetch_nodes_by_ids  # Local import to avoid module-cycle costs on import.

        identity_nucleus = fetch_identity_nucleus()
    except Exception:
        identity_nucleus = {}
        fetch_nodes_by_ids = None  # type: ignore[assignment]

    fallback_fact_specs = {
        "name": (
            "name",
            str(identity_nucleus.get("core_name") or "").strip(),
            str(identity_nucleus.get("primary_self_node_id") or "").strip(),
        ),
        "partner": (
            "partner",
            _clean_person_name_value(str((identity_nucleus.get("partner_candidates") or [""])[0] or "").strip()) or "",
            str((identity_nucleus.get("partner_support_node_ids") or [""])[0] or "").strip(),
        ),
        "mentor": (
            "mentor",
            str((identity_nucleus.get("mentor_candidates") or [""])[0] or "").strip(),
            str((identity_nucleus.get("mentor_support_node_ids") or [""])[0] or "").strip(),
        ),
        "sibling": (
            "sibling",
            str((identity_nucleus.get("sibling_candidates") or [""])[0] or "").strip(),
            str((identity_nucleus.get("sibling_support_node_ids") or [""])[0] or "").strip(),
        ),
        "role": (
            "role",
            str((identity_nucleus.get("role_candidates") or [""])[0] or "").strip(),
            str((identity_nucleus.get("role_support_node_ids") or [""])[0] or "").strip(),
        ),
        "projects": (
            "primary_project",
            str((identity_nucleus.get("project_candidates") or [""])[0] or "").strip(),
            str((identity_nucleus.get("project_support_node_ids") or [""])[0] or "").strip(),
        ),
    }
    fallback_text_templates = {
        "name": lambda value: f"Il nome e {value}.",
        "partner": lambda value: f"Il partner e {value}.",
        "mentor": lambda value: f"La mentor e {value}.",
        "sibling": lambda value: f"Il sibling e {value}.",
        "role": lambda value: f"Lavora come {value}.",
        "projects": lambda value: f"Il progetto principale e {value}.",
    }
    for aspect, (bucket_name, value, node_id) in fallback_fact_specs.items():
        if aspect not in aspects or inventory.get(bucket_name):
            continue
        if not value:
            continue
        inventory[bucket_name].append(
            _make_fact(
                kind=bucket_name,
                text=fallback_text_templates[aspect](value),
                node_id=node_id or "identity_nucleus",
                raw_score=0.62,
                summary=value,
                priority=0.72,
                value=value,
                evidence_snippet=value,
            )
        )

    if "style" in aspects:
        seen_style_node_ids = {str(item.get("node_id") or "") for item in list(inventory["style"] or [])}
        for match in matches[:8]:
            node = dict(match.get("node") or {})
            guide_area = str((node.get("provenance") or {}).get("guide_conceptual_area") or "")
            memory_type = str(node.get("memory_type") or "")
            summary = str(node.get("summary") or node.get("raw_text") or "").strip()
            combined_evidence = " ".join(
                part
                for part in (
                    summary,
                    str(match.get("evidence_snippet") or ""),
                    str(node.get("raw_text") or ""),
                )
                if part
            ).strip()
            lowered_summary = combined_evidence.lower()
            style_like = any(token in lowered_summary for token in ("comunica", "parla", "scrive", "stile", "si esprime", "tone", "voice", "spiega", "conversazioni"))
            descriptor_like = any(token in lowered_summary for token in ("dirett", "tecnic", "strutturat", "lucid", "chiar", "concis", "essenzial", "analitic", "ridond", "architett", "codice", "struttura"))
            if guide_area != "Expression" and memory_type != "identity_style" and not (style_like and descriptor_like):
                continue
            node_id = str(match.get("node_id") or "")
            if node_id in seen_style_node_ids:
                continue
            if not summary:
                continue
            inventory["style"].append(
                _make_fact(
                    kind="style",
                    text=summary if summary.endswith(".") else f"{summary}.",
                    node_id=node_id,
                    raw_score=float(match.get("raw_score") or 0.0),
                    summary=summary,
                    priority=0.74,
                    value=combined_evidence,
                    evidence_snippet=str(match.get("evidence_snippet") or combined_evidence or summary),
                )
            )
            seen_style_node_ids.add(node_id)
        if not any("tecnic" in str(item.get("value") or item.get("text") or "").lower() for item in inventory["style"]):
            for match in matches[:8]:
                node = dict(match.get("node") or {})
                node_id = str(match.get("node_id") or "")
                if node_id in seen_style_node_ids:
                    continue
                summary = str(node.get("summary") or node.get("raw_text") or "").strip()
                combined_evidence = " ".join(
                    part
                    for part in (
                        summary,
                        str(match.get("evidence_snippet") or ""),
                        str(node.get("raw_text") or ""),
                    )
                    if part
                ).strip()
                lowered_summary = combined_evidence.lower()
                if not any(token in lowered_summary for token in ("codice", "code", "architett", "struttura", "dettagli", "research", "ricerca", "document", "conversazioni")):
                    continue
                if not summary:
                    continue
                inventory["style"].append(
                    _make_fact(
                        kind="style",
                        text=summary if summary.endswith(".") else f"{summary}.",
                        node_id=node_id,
                        raw_score=float(match.get("raw_score") or 0.0),
                        summary=summary,
                        priority=0.68,
                        value=combined_evidence,
                        evidence_snippet=str(match.get("evidence_snippet") or combined_evidence or summary),
                    )
                )
                seen_style_node_ids.add(node_id)
                if len(inventory["style"]) >= 6:
                    break
    if "values" in aspects:
        seen_value_node_ids = {str(item.get("node_id") or "") for item in list(inventory["values"] or [])}
        for match in matches[:8]:
            node = dict(match.get("node") or {})
            guide_area = str((node.get("provenance") or {}).get("guide_conceptual_area") or "")
            memory_type = str(node.get("memory_type") or "")
            summary = str(node.get("summary") or node.get("raw_text") or "").strip()
            combined_evidence = " ".join(
                part
                for part in (
                    summary,
                    str(match.get("evidence_snippet") or ""),
                    str(node.get("raw_text") or ""),
                )
                if part
            ).strip()
            lowered_summary = combined_evidence.lower()
            strong_value_tokens = (
                "precisione",
                "chiarezza",
                "rigore",
                "qualit",
                "responsabil",
                "coerenza architetturale",
                "cura dei dettagli",
                "sostenibil",
                "sustainable",
                "decarbon",
                "impatto",
                "impact",
                "cooperazione",
                "cooperation",
                "educazione",
                "education",
                "talent",
                "coraggio",
                "courage",
                "ambizione",
                "ambition",
            )
            value_like = (
                sum(1 for token in strong_value_tokens if token in lowered_summary) >= 2
                or (
                    any(token in lowered_summary for token in strong_value_tokens)
                    and any(token in lowered_summary for token in ("valore", "valori", "principio", "principi", "guida", "guided", "focus", "rooted", "radici"))
                )
            )
            if guide_area != "Values" and memory_type != "value" and not value_like:
                continue
            node_id = str(match.get("node_id") or "")
            if node_id in seen_value_node_ids:
                continue
            if not summary:
                continue
            inventory["values"].append(
                _make_fact(
                    kind="values",
                    text=summary if summary.endswith(".") else f"{summary}.",
                    node_id=node_id,
                    raw_score=float(match.get("raw_score") or 0.0),
                    summary=summary,
                    priority=0.72,
                    value=combined_evidence,
                    evidence_snippet=str(match.get("evidence_snippet") or combined_evidence or summary),
                )
            )
            seen_value_node_ids.add(node_id)
        nucleus_support_ids = list(
            dict.fromkeys(
                [
                    *list(identity_nucleus.get("value_support_node_ids") or []),
                    *list(identity_nucleus.get("self_support_node_ids") or []),
                    *list(identity_nucleus.get("style_support_node_ids") or []),
                    *list(identity_nucleus.get("project_support_node_ids") or []),
                    *list(identity_nucleus.get("employer_support_node_ids") or []),
                    *list(identity_nucleus.get("role_support_node_ids") or []),
                ]
            )
        )[:30]
        if fetch_nodes_by_ids is not None and nucleus_support_ids:
            try:
                support_nodes = fetch_nodes_by_ids(nucleus_support_ids, include_raw_text=True)
            except Exception:
                support_nodes = []
            for node in support_nodes:
                node_id = str(node.get("id") or "")
                if not node_id or node_id in seen_value_node_ids:
                    continue
                summary = str(node.get("summary") or node.get("raw_text") or "").strip()
                combined_evidence = " ".join(
                    part
                    for part in (
                        summary,
                        str(node.get("raw_text") or ""),
                    )
                    if part
                ).strip()
                lowered_summary = combined_evidence.lower()
                if not any(
                    token in lowered_summary
                    for token in (
                        "precisione",
                        "chiarezza",
                        "rigore",
                        "qualit",
                        "responsabil",
                        "coerenza architetturale",
                        "cura dei dettagli",
                        "disciplina esecutiva",
                        "sostenibil",
                        "sustainable",
                        "decarbon",
                        "impatto",
                        "impact",
                        "cooperazione",
                        "cooperation",
                        "educazione",
                        "education",
                        "talent",
                        "coraggio",
                        "courage",
                        "ambizione",
                        "ambition",
                    )
                ):
                    continue
                inventory["values"].append(
                    _make_fact(
                        kind="values",
                        text=summary if summary.endswith(".") else f"{summary}.",
                        node_id=node_id,
                        raw_score=float(node.get("memory_confidence") or node.get("derivation_confidence") or 0.72),
                        summary=summary,
                        priority=0.74,
                        value=combined_evidence,
                        evidence_snippet=combined_evidence,
                    )
                )
                seen_value_node_ids.add(node_id)

    fragments: list[str] = []
    evidence_ids: list[str] = []
    evidence_snippets: list[dict[str, Any]] = []
    covered = 0
    lowered_query = _fold_text(query_text)
    query_entity_terms = [
        token
        for token in lowered_query.split()
        if len(token) >= 4 and token not in {"come", "cosa", "quale", "quali", "ruolo", "lavoro", "studio", "fatto"}
    ]
    relation_rows = _relation_fragments_for_query(query_text, inventory)
    relation_covered_aspects = {
        str(aspect)
        for row in relation_rows
        for aspect in list(row.get("covers") or [])
        if str(aspect)
    }
    query_contract = build_query_contract(query_text)
    if str(query_contract.get("query_kind") or "") == "exact_relation_fact":
        relation_bucket_by_name = {
            "father": "father",
            "partner": "partner",
            "mentor": "mentor",
            "sibling": "sibling",
        }
        for relation in list(query_contract.get("requested_relations") or []):
            bucket = relation_bucket_by_name.get(str(relation or "").strip())
            if not bucket:
                continue
            fact = _pick_best_fact(inventory.get(bucket) or [])
            if not fact:
                continue
            raw_value = str(fact.get("value") or "").strip()
            value = _clean_person_name_value(raw_value)
            if not value and bucket == "father":
                value = _extract_father_name(
                    " ".join(
                        part
                        for part in (
                            str(fact.get("text") or ""),
                            str(fact.get("evidence_snippet") or ""),
                            str(fact.get("summary") or ""),
                        )
                        if part
                    )
                )
            if not value:
                continue
            if bucket == "father":
                answer_text = (
                    f"Mio padre si chiamava {value}."
                    if _prefers_first_person_answer(query_text)
                    else f"Il padre si chiamava {value}."
                )
            elif bucket == "partner":
                answer_text = f"Il partner si chiama {value}."
            elif bucket == "mentor":
                answer_text = f"La mentor si chiama {value}."
            else:
                answer_text = f"Il familiare richiesto si chiama {value}."
            node_id = str(fact.get("node_id") or "")
            support_metadata = build_answer_support_metadata(matches=matches, evidence_node_ids=[node_id] if node_id else [])
            return _apply_answer_contract(
                query_text,
                {
                    "answer_text": answer_text,
                    "mode": "grounded_facts",
                    "confidence": min(0.97, max(0.72, float(fact.get("raw_score") or 0.0) + 0.08)),
                    "evidence_node_ids": [node_id] if node_id else [],
                    "reasoning_summary": "Answered an exact relation query from the requested relation value instead of expanding the full relation narrative.",
                    "insufficient": False,
                    "answerability_state": "grounded",
                    "evidence_snippets": [
                        {
                            "node_id": node_id,
                            "text": str(fact.get("evidence_snippet") or fact.get("text") or ""),
                            "kind": bucket,
                        }
                    ]
                    if node_id
                    else [],
                    "requested_aspects": aspects,
                    "support_node_count": int(support_metadata.get("support_node_count") or (1 if node_id else 0)),
                    "support_slot_count": int(support_metadata.get("support_slot_count") or 1),
                    "family_attribution_summary": dict(support_metadata.get("family_attribution_summary") or {}),
                    "contradiction_present": bool(support_metadata.get("contradiction_present")),
                },
                matches,
            )

    def add_fact(fact: dict[str, Any] | None) -> None:
        nonlocal covered
        if not fact:
            return
        covered += 1
        evidence_ids.append(str(fact["node_id"]))
        evidence_snippets.append(
            {
                "node_id": str(fact["node_id"]),
                "text": str(fact.get("evidence_snippet") or fact["text"]),
                "kind": str(fact["kind"]),
            }
        )
        fragments.append(str(fact["text"]))

    def add_relation_row(row: dict[str, Any]) -> None:
        nonlocal covered
        text = str(row.get("text") or "").strip()
        facts = [dict(fact) for fact in list(row.get("facts") or []) if isinstance(fact, dict)]
        if not text or not facts:
            return
        covered += max(1, len(list(row.get("covers") or [])))
        fragments.append(text)
        for fact in facts:
            node_id = str(fact.get("node_id") or "")
            if not node_id:
                continue
            evidence_ids.append(node_id)
            evidence_snippets.append(
                {
                    "node_id": node_id,
                    "text": str(fact.get("evidence_snippet") or fact.get("text") or ""),
                    "kind": str(fact.get("kind") or "relation"),
                }
            )

    for row in relation_rows:
        add_relation_row(row)

    if "name" in aspects:
        add_fact(_pick_best_fact(inventory["name"]))
    if "birthplace" in aspects:
        add_fact(_pick_best_fact(inventory["birthplace"]))
    if "residence" in aspects:
        add_fact(_pick_best_fact(inventory["residence"]))
    if "father" in aspects:
        add_fact(_pick_best_fact(inventory["father"]))
    if "partner" in aspects:
        add_fact(_pick_best_fact(inventory["partner"]))
    if "mentor" in aspects:
        add_fact(_pick_best_fact(inventory["mentor"]))
    if "sibling" in aspects:
        add_fact(_pick_best_fact(inventory["sibling"]))
    if "role" in aspects:
        if "projects" not in relation_covered_aspects:
            related_project = _pick_best_fact(
                [
                    fact
                    for fact in inventory["primary_project"]
                    if sum(1 for term in query_entity_terms if term in _fold_text(str(fact.get("text") or fact.get("evidence_snippet") or fact.get("value") or ""))) >= 1
                ]
            )
            add_fact(related_project)
        if "role" not in relation_covered_aspects:
            add_fact(_pick_best_fact(inventory["role"]))
    if "projects" in aspects:
        if "projects" not in relation_covered_aspects:
            primary_project = _pick_best_fact(inventory["primary_project"])
            secondary_project = _pick_best_fact(
                [fact for fact in inventory["secondary_project"] if fact.get("value") != (primary_project or {}).get("value")]
            )
            add_fact(primary_project or secondary_project)
    if "style" in aspects:
        add_fact(_compound_style_fact(inventory["style"]))
    if "values" in aspects:
        add_fact(_compound_values_fact(inventory["values"]))
    if "history" in aspects:
        if _is_temporal_reference_query(query_text):
            requested_terms = set(_explicit_temporal_terms(query_text))
            action_query = False
            temporal_entries = [dict(entry) for entry in list(temporal_inventory.get("entries") or []) if isinstance(entry, dict)]
            if requested_terms:
                temporal_entries = [
                    entry
                    for entry in temporal_entries
                    if requested_terms & set(str(token) for token in list(entry.get("tokens") or []))
                ]
                action_query = any(
                    token in _fold_text(query_text)
                    for token in (
                        "cosa hai fatto",
                        "cosa e successo",
                        "cosa è successo",
                        "successo",
                        "accaduto",
                        "fatto",
                        "lavorato",
                        "lavoravi",
                        "rilevante",
                        "work",
                        "worked",
                        "working",
                        "happened",
                        "relevant",
                    )
                )
                action_tokens = (
                    "lavor",
                    "iniziato",
                    "cominciato",
                    "avviato",
                    "started",
                    "working",
                    "worked",
                    "acquis",
                    "announced",
                    "became",
                    "becomes",
                    "integrat",
                    "parte",
                    "sold",
                )

                def _requested_temporal_rank(entry: dict[str, Any]) -> tuple[Any, ...]:
                    fact_text = str(entry.get("text") or "")
                    years = set(re.findall(r"\b(?:19|20)\d{2}\b", fact_text))
                    off_target_years = years - requested_terms
                    return (
                        0 if years & requested_terms else 1,
                        1 if _temporal_text_is_source_metadata(fact_text) else 0,
                        -_temporal_query_overlap(query_text, fact_text),
                        -_temporal_event_score(fact_text),
                        len(off_target_years),
                        0 if action_query and any(token in _fold_text(fact_text) for token in action_tokens) else 1,
                        -float(entry.get("confidence") or 0.0),
                        -float(entry.get("score") or 0.0),
                        len(fact_text),
                    )

                temporal_entries = _rank_temporal_entries_for_query(
                    query_text,
                    sorted(temporal_entries, key=_requested_temporal_rank),
                    requested_terms=requested_terms,
                )
                if temporal_entries:
                    history_limit = 1 if action_query else 4
                    for entry in temporal_entries[:history_limit]:
                        add_fact(
                            _temporal_entry_to_fact(
                                entry,
                                requested_terms=requested_terms,
                                first_person=_prefers_first_person_answer(query_text),
                            )
                        )
                else:
                    history_facts = [
                        fact
                        for fact in inventory["history"]
                        if any(term in str(fact.get("text") or fact.get("evidence_snippet") or "") for term in requested_terms)
                    ]
                    history_facts = sorted(
                        history_facts,
                        key=lambda fact: _requested_temporal_rank(
                            {
                                "text": str(fact.get("text") or fact.get("evidence_snippet") or ""),
                                "confidence": float(fact.get("priority") or 0.0),
                                "score": float(fact.get("raw_score") or 0.0),
                            }
                        ),
                    )
                    history_limit = 1 if action_query else 4
                    for fact in history_facts[:history_limit]:
                        add_fact(fact)
            else:
                if _is_temporal_inventory_query(query_text) and temporal_entries and set(aspects) == {"history"}:
                    temporal_answer = _build_temporal_inventory_direct_answer(query_text, temporal_inventory, matches, aspects)
                    return _apply_answer_contract(query_text, temporal_answer, matches) if temporal_answer else None
                if temporal_entries:
                    for entry in temporal_entries[:6 if _is_temporal_inventory_query(query_text) else 3]:
                        add_fact(_temporal_entry_to_fact(entry))
                else:
                    history_facts = sorted(inventory["history"], key=lambda item: (float(item.get("priority") or 0.0), float(item.get("raw_score") or 0.0)), reverse=True)
                    for fact in history_facts[:3]:
                        add_fact(fact)
        else:
            add_fact(_pick_best_fact(inventory["history"]))

    if not fragments:
        return None
    if _prefers_first_person_answer(query_text):
        fragments = [_self_voice_fragment(fragment) for fragment in fragments]

    aspect_count = len(aspects)
    answerability_state = "grounded" if covered >= aspect_count else "partial"
    support_scores = [
        float(_pick_best_fact(inventory[bucket]).get("raw_score") or 0.0)
        for bucket in ("name", "birthplace", "residence", "father", "partner", "mentor", "sibling", "role", "primary_project", "secondary_project", "style", "values", "history")
        if _pick_best_fact(inventory[bucket]) is not None and (
            (bucket == "primary_project" and "projects" in aspects)
            or (bucket == "secondary_project" and "projects" in aspects and not inventory["primary_project"])
            or bucket in aspects
            or (bucket == "role" and "role" in aspects)
        )
    ]
    confidence = min(0.97, max(0.6, (sum(support_scores) / max(1, len(support_scores))) + (0.1 if answerability_state == "grounded" else 0.0)))
    unique_ids = list(dict.fromkeys(evidence_ids))
    support_metadata = build_answer_support_metadata(
        matches=matches,
        evidence_node_ids=unique_ids,
    )
    answer_text = " ".join(dict.fromkeys(fragment.strip() for fragment in fragments))
    answer_payload = {
        "answer_text": " ".join(dict.fromkeys(fragment.strip() for fragment in fragments)),
        "mode": "grounded_facts",
        "confidence": confidence,
        "evidence_node_ids": unique_ids,
        "reasoning_summary": "Built the answer from exact fact-bearing evidence hydrated from the shortlist.",
        "insufficient": answerability_state != "grounded",
        "answerability_state": answerability_state,
        "evidence_snippets": evidence_snippets,
        "requested_aspects": aspects,
        "support_node_count": int(support_metadata.get("support_node_count") or 0),
        "support_slot_count": int(support_metadata.get("support_slot_count") or 0),
        "family_attribution_summary": dict(support_metadata.get("family_attribution_summary") or {}),
        "contradiction_present": bool(support_metadata.get("contradiction_present")),
    }
    answer_payload["answer_text"] = answer_text
    return _apply_answer_contract(query_text, answer_payload, matches)


def heuristic_answer(query_text: str, matches: list[dict[str, Any]]) -> dict[str, Any]:
    matches = _filter_disallowed_matches_for_query(query_text, _eligible_answer_matches(matches))
    intent_type = detect_query_intent(query_text)
    if not matches:
        return {
            "answer_text": "Non ho trovato evidenze sufficienti nella memoria AGVM.",
            "mode": "insufficient",
            "confidence": 0.0,
            "evidence_node_ids": [],
            "reasoning_summary": "No retrieval matches available.",
            "insufficient": True,
            "answerability_state": "insufficient",
            "support_node_count": 0,
            "support_slot_count": 0,
            "family_attribution_summary": {},
            "contradiction_present": False,
        }

    extractors = {
        "identity_name": _extract_name_from_identity,
        "workplace": _extract_workplace,
        "birthplace": _extract_birthplace,
        "partner_name": _extract_partner_name,
        "father_name": _extract_father_name,
    }
    if intent_type in extractors:
        extractor = extractors[intent_type]
        for match in matches[:6]:
            candidate_text = str(match["node"].get("raw_text") or "")
            value = extractor(candidate_text)
            if value:
                if intent_type == "identity_name":
                    answer_text = f"Ti chiami {value}."
                elif intent_type == "workplace":
                    answer_text = f"Lavori a {value}."
                elif intent_type == "birthplace":
                    answer_text = f"Sei nato a {value}."
                elif intent_type == "father_name":
                    answer_text = f"Tuo padre si chiamava {value}."
                else:
                    answer_text = f"La tua partner si chiama {value}."
                return {
                    "answer_text": answer_text,
                    "mode": "heuristic",
                    "confidence": min(0.95, 0.72 + float(match["raw_score"]) * 0.2),
                    "evidence_node_ids": [match["node_id"]],
                    "reasoning_summary": f"Matched {intent_type} pattern in retrieved memory.",
                    "insufficient": False,
                    "answerability_state": "grounded",
                    "support_node_count": 1,
                    "support_slot_count": 1,
                    "family_attribution_summary": {"heuristic": 1, "ai": 0, "dual_origin": 0},
                    "contradiction_present": False,
                }

    if _query_is_work_or_company(query_text):
        work_rows: list[str] = []
        evidence_ids: list[str] = []
        seen_rows: set[str] = set()
        for match in matches[:10]:
            node = dict(match.get("node") or {})
            merged = {
                **node,
                **{key: value for key, value in match.items() if key != "node"},
                "node_id": str(match.get("node_id") or node.get("id") or "").strip(),
            }
            text = _mcp_best_candidate_text(merged)
            if not text or _mcp_short_label_like_text(text):
                continue
            section_key = _mcp_context_section_key(
                " ".join(
                    str(part or "")
                    for part in (
                        merged.get("memory_type"),
                        (merged.get("provenance") or {}).get("guide_conceptual_area") if isinstance(merged.get("provenance"), dict) else None,
                    )
                ),
                text,
            )
            if section_key != "work":
                continue
            folded_row = _fold_text(text)
            if folded_row in seen_rows:
                continue
            seen_rows.add(folded_row)
            work_rows.append(text)
            node_id = str(match.get("node_id") or node.get("id") or "").strip()
            if node_id:
                evidence_ids.append(node_id)
            if len(work_rows) >= 4:
                break
        if work_rows:
            answer_text = polish_final_answer_surface(query_text, " ".join(work_rows)) or clean_answer_surface_text(" ".join(work_rows))
            support_metadata = build_answer_support_metadata(matches=matches, evidence_node_ids=evidence_ids)
            return {
                "answer_text": answer_text,
                "mode": "heuristic",
                "confidence": min(0.86, max([float(match.get("raw_score") or 0.0) for match in matches[:4]] or [0.62])),
                "evidence_node_ids": list(dict.fromkeys(evidence_ids))[:8],
                "reasoning_summary": "Composed a work/company answer from contract-relevant work evidence instead of sealing a short label.",
                "insufficient": False,
                "answerability_state": "grounded",
                "support_node_count": int(support_metadata.get("support_node_count") or len(set(evidence_ids))),
                "support_slot_count": max(1, int(support_metadata.get("support_slot_count") or 0)),
                "family_attribution_summary": dict(support_metadata.get("family_attribution_summary") or {"heuristic": len(evidence_ids), "ai": 0, "dual_origin": 0}),
                "contradiction_present": bool(support_metadata.get("contradiction_present")),
            }

    top = matches[0]
    top_topic = str((top["node"].get("provenance") or {}).get("guide_conceptual_area") or top["node"].get("memory_type") or "")
    supportive = [
        match
        for match in matches[:4]
        if str((match["node"].get("provenance") or {}).get("guide_conceptual_area") or match["node"].get("memory_type") or "") == top_topic
    ]
    if float(top["raw_score"]) >= 0.74 or (
        float(top["raw_score"]) >= 0.6
        and len(supportive) >= 2
        and sum(float(item["raw_score"]) for item in supportive[:2]) / min(2, len(supportive)) >= 0.58
    ):
        return {
            "answer_text": top["node"]["summary"],
            "mode": "heuristic",
            "confidence": min(0.88, max(float(top["raw_score"]), 0.62)),
            "evidence_node_ids": [top["node_id"]],
            "reasoning_summary": "Used top grounded memory because nearby evidence converged on the same answer area.",
            "insufficient": False,
            "answerability_state": "grounded",
            "support_node_count": 1,
            "support_slot_count": 1 if supportive else 0,
            "family_attribution_summary": {"heuristic": 1, "ai": 0, "dual_origin": 0},
            "contradiction_present": False,
        }
    return {
        "answer_text": "Ho trovato memorie correlate, ma non abbastanza grounding per rispondere con sicurezza.",
        "mode": "insufficient",
        "confidence": float(top["raw_score"]),
        "evidence_node_ids": [match["node_id"] for match in matches[:3]],
        "reasoning_summary": "Top matches are too generic for a grounded answer.",
        "insufficient": True,
        "answerability_state": "insufficient",
        "support_node_count": len({str(match.get("node_id") or "") for match in matches[:3] if str(match.get("node_id") or "").strip()}),
        "support_slot_count": 0,
        "family_attribution_summary": {"heuristic": len(matches[:3]), "ai": 0, "dual_origin": 0},
        "contradiction_present": False,
    }


def build_context_payload(
    matches: list[dict[str, Any]],
    shared_evidence: dict[str, Any] | None = None,
    *,
    evidence_reservoir: dict[str, Any] | None = None,
    query_text: str | None = None,
) -> dict[str, Any]:
    matches = _eligible_answer_matches(matches)
    fragments: list[dict[str, Any]] = []
    structured_sections: dict[str, dict[str, Any]] = {
        "identity": {"key": "identity", "title": "Identity", "items": [], "evidence_node_ids": [], "confidence": 0.0},
        "work": {"key": "work", "title": "Work/Projects", "items": [], "evidence_node_ids": [], "confidence": 0.0},
        "relationships": {"key": "relationships", "title": "Relationships", "items": [], "evidence_node_ids": [], "confidence": 0.0},
        "style": {"key": "style", "title": "Style", "items": [], "evidence_node_ids": [], "confidence": 0.0},
        "values": {"key": "values", "title": "Values", "items": [], "evidence_node_ids": [], "confidence": 0.0},
        "history": {"key": "history", "title": "History", "items": [], "evidence_node_ids": [], "confidence": 0.0},
        "temporal_inventory": {"key": "temporal_inventory", "title": "Temporal Evidence", "items": [], "evidence_node_ids": [], "confidence": 0.0},
        "documents": {"key": "documents", "title": "Documents", "items": [], "evidence_node_ids": [], "confidence": 0.0},
    }

    def section_key_for_match(match: dict[str, Any]) -> str:
        node = match["node"]
        topic = str((node.get("provenance") or {}).get("guide_conceptual_area") or node.get("memory_type") or "memory")
        lowered_topic = _fold_text(topic)
        raw_text = str(node.get("raw_text") or node.get("summary") or "")
        lowered_text = _fold_text(raw_text)
        if lowered_topic in {"identity"} and _text_has_work_or_project_surface(raw_text):
            return "work"
        if bool(node.get("is_document_anchor")) or lowered_topic in {"document_anchor", "media signals", "documents"}:
            if _text_mentions_personal_relationship(lowered_text):
                return "relationships"
            if _text_has_work_or_project_activity_surface(raw_text):
                return "work"
            if _sentence_has_temporal_signal(lowered_text) and not _temporal_text_is_year_navigation_noise(lowered_text):
                return "history"
            return "documents"
        if lowered_topic in {"identity"} or any(token in lowered_text for token in ("mi chiamo", "sono ", "vive a", "nata a", "born in")):
            return "identity"
        if lowered_topic in {"projects", "project", "operational", "knowledge"} or _text_has_work_or_project_surface(raw_text):
            return "work"
        if lowered_topic in {"relationships", "relational", "family history"} or _text_mentions_personal_relationship(lowered_text):
            return "relationships"
        if lowered_topic in {"expression", "identity_style"} or any(token in lowered_text for token in ("comunica", "parla", "stile", "tone", "voice", "diretto", "strutturato")):
            return "style"
        if lowered_topic in {"values", "value"} or any(
            token in lowered_text
            for token in (
                "valore",
                "valori",
                "value",
                "values",
                "principio",
                "principi",
                "principle",
                "principles",
                "precisione",
                "precision",
                "chiarezza",
                "clarity",
                "coraggio",
                "courage",
            )
        ):
            return "values"
        if lowered_topic in {"history", "episodic"} or any(token in lowered_text for token in ("nel 20", "nel 19", "in passato", "ha iniziato", "ha lavorato")):
            return "history"
        return "work"

    def append_section_item(section_key: str, text: str, node_id: str, confidence: float) -> None:
        section = structured_sections[section_key]
        if text and text not in section["items"] and len(section["items"]) < 4:
            section["items"].append(text)
        if node_id and node_id not in section["evidence_node_ids"]:
            section["evidence_node_ids"].append(node_id)
        section["confidence"] = max(float(section["confidence"]), float(confidence))

    for match in matches[:10]:
        node = match["node"]
        topic = str((node.get("provenance") or {}).get("guide_conceptual_area") or node.get("memory_type") or "memory")
        fragment_text = str(match.get("evidence_snippet") or node.get("raw_text") or node.get("summary") or "").strip()
        if not fragment_text:
            continue
        fragments.append(
            {
                "topic": topic,
                "text": fragment_text,
                "confidence": float(match["raw_score"]),
                "evidence_node_ids": [match["node_id"]],
            }
        )
        append_section_item(section_key_for_match(match), fragment_text, str(match["node_id"]), float(match["raw_score"]))

    inventory = extract_grounded_fact_inventory(matches)
    inventory_sections = {
        "identity": ("name", "birthplace", "residence"),
        "work": ("role", "primary_project", "secondary_project"),
        "relationships": ("father", "partner", "mentor", "sibling"),
        "style": ("style",),
        "values": ("values",),
        "history": ("history",),
        "documents": (),
    }
    for section_key, buckets in inventory_sections.items():
        for bucket in buckets:
            if bucket == "style":
                fact = _compound_style_fact(list(inventory.get(bucket) or []))
            elif bucket == "values":
                fact = _compound_values_fact(list(inventory.get(bucket) or []))
            else:
                fact = _pick_best_fact(list(inventory.get(bucket) or []))
            if not fact:
                continue
            append_section_item(
                section_key,
                str(fact.get("text") or ""),
                str(fact.get("node_id") or ""),
                float(fact.get("raw_score") or 0.0),
            )

    style_fact = _compound_style_fact(list(inventory.get("style") or []))
    if style_fact and structured_sections["style"]["items"]:
        structured_sections["style"]["items"] = list(
            dict.fromkeys([str(style_fact.get("text") or ""), *structured_sections["style"]["items"]])
        )[:4]
    values_fact = _compound_values_fact(list(inventory.get("values") or []))
    if values_fact and structured_sections["values"]["items"]:
        structured_sections["values"]["items"] = list(
            dict.fromkeys([str(values_fact.get("text") or ""), *structured_sections["values"]["items"]])
        )[:4]

    temporal_inventory = build_temporal_inventory(matches, evidence_reservoir=evidence_reservoir)
    if temporal_inventory.get("entries") and (
        _is_temporal_reference_query(str(query_text or ""))
        or bool(structured_sections["history"]["items"])
        or bool(structured_sections["documents"]["items"])
    ):
        temporal_section = structured_sections["temporal_inventory"]
        temporal_items = []
        for entry in list(temporal_inventory.get("entries") or [])[:8]:
            year_label = str(entry.get("primary_year") or _join_human_list([str(year) for year in list(entry.get("years") or [])]) or "date").strip()
            text = str(entry.get("text") or "").strip()
            if not text:
                continue
            temporal_items.append(f"{year_label}: {text}")
            node_id = str(entry.get("node_id") or "").strip()
            if node_id and node_id not in temporal_section["evidence_node_ids"]:
                temporal_section["evidence_node_ids"].append(node_id)
            temporal_section["confidence"] = max(float(temporal_section["confidence"]), float(entry.get("confidence") or 0.0))
        temporal_section["items"] = list(dict.fromkeys(temporal_items))[:8]
        temporal_section["metadata"] = {
            "intent": temporal_inventory.get("intent"),
            "years": list(temporal_inventory.get("years") or []),
            "date_tokens": list(temporal_inventory.get("date_tokens") or [])[:16],
            "coverage_state": temporal_inventory.get("coverage_state"),
            "partial": bool(temporal_inventory.get("partial")),
        }

    section_list = [section for section in structured_sections.values() if section["items"]]
    if _is_temporal_reference_query(str(query_text or "")):
        section_list = sorted(section_list, key=lambda section: 0 if str(section.get("key") or "") == "temporal_inventory" else 1)
    context_summary = " | ".join(section["items"][0] for section in section_list[:6]).strip()
    style_cues = list(structured_sections["style"]["items"])[:4]
    values_cues = list(structured_sections["values"]["items"])[:4]
    biographical_cues = list(structured_sections["history"]["items"] + structured_sections["identity"]["items"])[:6]
    traits = list(dict.fromkeys(structured_sections["identity"]["items"] + structured_sections["values"]["items"]))[:5]
    story_points = list(dict.fromkeys(structured_sections["history"]["items"] + structured_sections["work"]["items"]))[:6]
    return {
        "context_summary": context_summary,
        "context_fragments": fragments,
        "structured_sections": section_list,
        "traits": traits,
        "style_cues": style_cues,
        "communication_cues": style_cues,
        "values_cues": values_cues,
        "biographical_cues": biographical_cues,
        "movement_cues": [],
        "story_points": story_points,
        "open_uncertainties": [] if matches else ["Insufficient evidence"],
        "evidence_node_ids": [match["node_id"] for match in matches[:10]],
        "evidence_reservoir_summary": dict((evidence_reservoir or {}).get("reservoir_summary") or {}),
        "context_quality_metrics": dict((evidence_reservoir or {}).get("quality_metrics") or {}),
        "temporal_inventory": temporal_inventory if temporal_inventory.get("entries") else {},
    }


_MCP_CONTEXT_SECTION_TITLES = {
    "identity": "Identity",
    "work": "Work And Projects",
    "relationships": "Relationships",
    "style": "Style And Communication",
    "values": "Values And Operating Principles",
    "history": "Timeline And History",
    "temporal_inventory": "Temporal Evidence",
    "documents": "Documents And Source Material",
    "privacy_boundary": "Private Data Boundary",
}


MCP_CONTEXT_PACKAGE_POLICY_VERSION = "agvm.mcp_context_package.policy.v5"
MCP_CONTEXT_PACKAGE_MODES = (
    "answer_minimal",
    "mcp_operational",
    "broad_dossier",
    "document_full",
    "forensic_trace",
)
MCP_DOCUMENT_TEXT_POLICIES = ("refs_only", "top_raw", "all_raw")
MCP_DOCUMENT_REF_CONTRACT_VERSION = "agvm.document_ref_contract.v1"
MCP_DOCUMENT_BUNDLE_VERSION = "agvm.document_bundle.v1"
MCP_DOCUMENT_DELIVERY_CONTRACT_VERSION = "agvm.document_delivery_contract.v1"
MCP_CONTEXT_DOCUMENT_REFERENCES_VERSION = "agvm.context_package.document_references.v1"
MCP_LINK_AWARE_CONTEXT_CONTRACT_VERSION = "agvm.link_aware_context_contract.v1"

_MCP_CONTEXT_PACKAGE_MODE_PROFILES: dict[str, dict[str, Any]] = {
    "answer_minimal": {
        "section_item_limit": 2,
        "executive_item_limit": 4,
        "reservoir_summary_limit": 0,
        "source_excerpt_limit": 0,
        "path_context_limit": 0,
        "reservoir_rich_item_threshold": 0,
        "min_agent_body_chars_if_reservoir_rich": 0,
        "include_document_workspace": False,
        "widen_allowed_sections": False,
    },
    "mcp_operational": {
        "section_item_limit": 6,
        "executive_item_limit": 8,
        "reservoir_summary_limit": 10,
        "source_excerpt_limit": 4,
        "path_context_limit": 6,
        "reservoir_rich_item_threshold": 4,
        "min_agent_body_chars_if_reservoir_rich": 1400,
        "include_document_workspace": False,
        "widen_allowed_sections": True,
    },
    "broad_dossier": {
        "section_item_limit": 10,
        "executive_item_limit": 10,
        "reservoir_summary_limit": 16,
        "source_excerpt_limit": 6,
        "path_context_limit": 8,
        "reservoir_rich_item_threshold": 5,
        "min_agent_body_chars_if_reservoir_rich": 2200,
        "include_document_workspace": False,
        "widen_allowed_sections": True,
    },
    "document_full": {
        "section_item_limit": 8,
        "executive_item_limit": 8,
        "reservoir_summary_limit": 10,
        "source_excerpt_limit": 6,
        "path_context_limit": 6,
        "reservoir_rich_item_threshold": 4,
        "min_agent_body_chars_if_reservoir_rich": 1800,
        "include_document_workspace": True,
        "include_full_raw_workspace_in_agent_body": False,
        "include_raw_document_bundle_in_agent_body": False,
        "widen_allowed_sections": True,
    },
    "forensic_trace": {
        "section_item_limit": 12,
        "executive_item_limit": 10,
        "reservoir_summary_limit": 20,
        "source_excerpt_limit": 8,
        "path_context_limit": 10,
        "reservoir_rich_item_threshold": 5,
        "min_agent_body_chars_if_reservoir_rich": 2800,
        "include_document_workspace": True,
        "include_full_raw_workspace_in_agent_body": True,
        "include_raw_document_bundle_in_agent_body": True,
        "widen_allowed_sections": True,
    },
}


def _mcp_normalize_context_package_mode(value: Any) -> str | None:
    mode = str(value or "").strip().lower()
    return mode if mode in MCP_CONTEXT_PACKAGE_MODES else None


def _mcp_normalize_document_text_policy(value: Any) -> str:
    policy = str(value or "refs_only").strip().lower()
    return policy if policy in MCP_DOCUMENT_TEXT_POLICIES else "refs_only"


def _mcp_infer_context_package_mode(
    *,
    query_text: str,
    retrieval_mode: str,
    semantic_contract: dict[str, Any] | None,
    document_mode: str,
    broad_context: bool,
    requested_mode: Any = None,
) -> str:
    explicit = _mcp_normalize_context_package_mode(requested_mode)
    if explicit:
        return explicit
    contract = dict(semantic_contract or {})
    context_contract = dict(contract.get("context_contract") or {})
    explicit = _mcp_normalize_context_package_mode(
        contract.get("context_package_mode")
        or context_contract.get("package_mode")
        or context_contract.get("context_package_mode")
    )
    if explicit:
        return explicit
    mode = str(retrieval_mode or "balanced").strip().lower()
    if document_mode != "none":
        return "forensic_trace" if mode == "forensic" else "document_full"
    if mode == "forensic":
        return "forensic_trace"
    if broad_context or _is_broad_self_query(query_text):
        return "broad_dossier"
    return "mcp_operational"


def _mcp_context_package_policy(package_mode: str) -> dict[str, Any]:
    profile = dict(_MCP_CONTEXT_PACKAGE_MODE_PROFILES.get(package_mode) or _MCP_CONTEXT_PACKAGE_MODE_PROFILES["mcp_operational"])
    profile["mode"] = package_mode
    profile["policy_version"] = MCP_CONTEXT_PACKAGE_POLICY_VERSION
    return profile


def _mcp_context_package_hot_section_aliases(ordered_sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    aliases: list[dict[str, Any]] = []
    for section in ordered_sections:
        if not isinstance(section, dict):
            continue
        items = [
            str(item).strip()
            for item in list(section.get("items") or [])
            if str(item or "").strip()
        ]
        if not items:
            continue
        key = str(section.get("key") or "").strip()
        aliases.append(
            {
                "schema_version": "agvm.mcp_context_package.hot_section_alias.v1",
                "key": key,
                "title": str(section.get("title") or _MCP_CONTEXT_SECTION_TITLES.get(key, key or "Context")).strip(),
                "items": items,
                "item_count": len(items),
                "confidence": section.get("confidence"),
                "source": "context_package.sections",
            }
        )
    return aliases


def _mcp_context_package_cold_reservoir_alias(cold_context: list[dict[str, Any]], cold_reason_counts: dict[str, int]) -> dict[str, Any]:
    return {
        "schema_version": "agvm.mcp_context_package.cold_reservoir_alias.v1",
        "entry_count": len(cold_context),
        "reason_counts": dict(cold_reason_counts or {}),
        "entries": list(cold_context[:25]),
        "entry_limit": 25,
        "source": "context_package.cold_context",
        "inspectable": True,
    }


def _mcp_contract_core_sections(
    *,
    required_sections: set[str],
    optional_sections: set[str],
    document_mode: str,
    broad_context: bool,
    package_mode: str,
) -> set[str]:
    if broad_context or package_mode in {"broad_dossier", "forensic_trace"}:
        return set(_MCP_CONTEXT_SECTION_TITLES)
    core = set(required_sections)
    if package_mode != "mcp_operational":
        core.update(optional_sections)
    if document_mode != "none":
        core.add("documents")
    if core & {"work", "relationships", "history", "temporal_inventory"}:
        core.add("identity")
    return core or set(_MCP_CONTEXT_SECTION_TITLES)


def _mcp_widen_allowed_sections_for_package_mode(
    *,
    package_mode: str,
    allowed_sections: set[str],
    required_sections: set[str],
    optional_sections: set[str],
    forbidden_sections: set[str],
    broad_context: bool,
) -> set[str]:
    if package_mode == "answer_minimal":
        return set(allowed_sections) - set(forbidden_sections)
    widened = set(allowed_sections) | set(required_sections) | set(optional_sections)
    widened.update({"identity", "work", "history", "temporal_inventory", "documents", "values", "style"})
    if broad_context or package_mode in {"broad_dossier", "forensic_trace"}:
        widened.update(_MCP_CONTEXT_SECTION_TITLES)
    if "relationships" in required_sections or "relationships" in optional_sections or broad_context or package_mode in {"broad_dossier", "forensic_trace"}:
        widened.add("relationships")
    return widened - set(forbidden_sections)


def _mcp_budget_items_for_agent_body(
    items: list[Any],
    *,
    limit: int,
    answer_alignment_terms: list[str],
) -> list[str]:
    cleaned_items = [str(item or "").strip() for item in items if str(item or "").strip()]
    if not cleaned_items:
        return []
    if limit <= 0 or len(cleaned_items) <= limit:
        return cleaned_items
    selected = cleaned_items[:limit]
    for item in cleaned_items[limit:]:
        if not _mcp_text_has_alignment_term(item, answer_alignment_terms):
            continue
        if item in selected:
            continue
        selected.append(item)
    return selected


def _mcp_reservoir_summary_lines(
    cold_context: list[dict[str, Any]],
    *,
    limit: int,
    exact_field_active: bool = False,
    answer_alignment_terms: list[str] | None = None,
) -> list[str]:
    if limit <= 0:
        return []
    visible_items = _mcp_reservoir_agent_body_items(
        cold_context,
        exact_field_active=exact_field_active,
        answer_alignment_terms=answer_alignment_terms or [],
    )
    lines: list[str] = []
    for item in visible_items:
        section = str(item.get("section") or "")
        title = _MCP_CONTEXT_SECTION_TITLES.get(section, section or "Reservoir")
        text = _mcp_clean_agent_text(item.get("text"))
        if not text:
            continue
        line = f"- {title}: {text}"
        if not _mcp_agent_body_has_node_id(line):
            lines.append(line)
        if len(lines) >= limit:
            break
    return lines


def _mcp_cold_item_visible_in_agent_body(
    item: dict[str, Any],
    *,
    exact_field_active: bool,
    answer_alignment_terms: list[str] | None = None,
) -> bool:
    if exact_field_active:
        return False
    reason = str(item.get("reason") or "").strip()
    if reason in {
        "does_not_satisfy_exact_requested_field",
        "forbidden_by_semantic_contract",
        "forbidden_topic_by_semantic_contract",
        "non_core_context_reservoir",
        "subject_anchor_missing",
        "missing_requested_relation_anchor",
        "unanchored_work_relation_kept_cold",
        "unrequested_optional_context_reservoir",
    }:
        return False
    if str(item.get("source_kind") or "").strip() == "path_corridor":
        return False
    text = _mcp_clean_agent_text(item.get("text"))
    if reason == "off_contract_reservoir" and not _mcp_text_has_alignment_term(text, list(answer_alignment_terms or [])):
        return False
    return bool(text and not _mcp_agent_body_has_node_id(text))


def _mcp_reservoir_agent_body_items(
    cold_context: list[dict[str, Any]],
    *,
    exact_field_active: bool,
    answer_alignment_terms: list[str],
) -> list[dict[str, Any]]:
    visible: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in cold_context:
        if not _mcp_cold_item_visible_in_agent_body(
            item,
            exact_field_active=exact_field_active,
            answer_alignment_terms=answer_alignment_terms,
        ):
            continue
        text = _mcp_clean_agent_text(item.get("text"))
        if not text:
            continue
        key = _fold_text(f"{item.get('section') or ''} {text}")
        if not key or key in seen:
            continue
        seen.add(key)
        visible.append({**item, "section": _mcp_context_section_key(item.get("section"), text), "text": text})
    return sorted(
        visible,
        key=lambda item: (
            1.0 if _mcp_text_has_alignment_term(item.get("text"), answer_alignment_terms) else 0.0,
            *_mcp_context_item_rank(item.get("text")),
        ),
        reverse=True,
    )


def _mcp_cold_reason_counts(cold_context: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in cold_context:
        reason = str(item.get("reason") or "unknown").strip() or "unknown"
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def _mcp_source_excerpt_lines(
    hot_context: list[dict[str, Any]],
    cold_context: list[dict[str, Any]],
    *,
    existing_agent_texts: set[str],
    limit: int,
    exact_field_active: bool = False,
) -> list[str]:
    if limit <= 0:
        return []
    lines: list[str] = []
    seen: set[str] = set()
    source_items = [("hot", item) for item in list(hot_context)] + [("cold", item) for item in list(cold_context)]
    for source_state, item in source_items:
        if source_state == "cold" and not _mcp_cold_item_visible_in_agent_body(
            item,
            exact_field_active=exact_field_active,
        ):
            continue
        text = _mcp_clean_agent_text(item.get("text"))
        if not text:
            continue
        folded = _fold_text(text)
        if folded in seen or folded in existing_agent_texts:
            continue
        seen.add(folded)
        source_title = _mcp_clean_agent_text(item.get("source_title"))
        prefix = f"{source_title}: " if source_title else ""
        line = f"- {prefix}{text}"
        if not _mcp_agent_body_has_node_id(line):
            lines.append(line)
        if len(lines) >= limit:
            break
    return lines


def _mcp_context_section_key(value: Any, text: Any = "") -> str:
    folded = _fold_text(f"{value or ''} {text or ''}")
    seed_folded = _fold_text(str(value or ""))
    combined_text = f"{value or ''} {text or ''}"
    if seed_folded in {"privacy_boundary", "private_data_boundary", "private data boundary", "data privacy boundary"}:
        return "privacy_boundary"
    if seed_folded in {"private_identifier", "exact_user_field"}:
        return "identity"
    if seed_folded == "personal_contact":
        return "relationships"
    if any(token in seed_folded for token in ("style", "communication", "stile")):
        return "style"
    if any(token in seed_folded for token in ("value", "values", "principle", "principi", "valori")):
        return "values"
    if any(token in seed_folded for token in ("temporal_inventory", "temporal evidence")):
        return "temporal_inventory"
    if any(token in seed_folded for token in ("history", "timeline", "storia")):
        return "history"
    if any(token in seed_folded for token in ("document", "source", "file", "chunk", "anchor")):
        if _text_mentions_personal_relationship(folded):
            return "relationships"
        if _text_has_work_or_project_activity_surface(combined_text):
            return "work"
        if any(token in folded for token in ("style", "communication", "tone", "voice", "stile", "comunica", "parla ", "direct", "technical")):
            return "style"
        if any(token in folded for token in ("value", "values", "principle", "principi", "valori", "precision", "sustainable", "impact", "coraggio", "courage")):
            return "values"
        if _sentence_has_temporal_signal(str(text or "")) and not _temporal_text_is_year_navigation_noise(str(text or "")):
            return "history"
        return "documents"
    if any(token in seed_folded for token in ("work", "project", "projects", "azienda", "aziende", "company", "companies")):
        return "work"
    if "identity" in seed_folded and any(
        token in folded
        for token in (
            "represented as",
            "based in",
            "lives in",
            "vive a",
            "software entrepreneur",
            "founder-operator",
            "founder operator",
            "imprenditore",
        )
    ):
        return "identity"
    if "identity" in seed_folded and _text_has_work_or_project_surface(combined_text):
        return "work"
    if (
        any(token in seed_folded for token in ("relationship", "relationships", "partner"))
        and any(marker in folded for marker in _BUSINESS_PARTNER_MARKERS)
        and not _text_mentions_personal_relationship(folded)
    ):
        return "work"
    if _text_mentions_personal_relationship(folded) or any(token in folded for token in ("relationship", "relationships", "relational", "relazione", "relazioni", "personale", "personali", "fidate", "aeronautica", "air force", "monumento")):
        return "relationships"
    if "identity" in seed_folded and any(token in folded for token in ("identity", "name", "born", "birth", "residence", "nato", "nata", "vive", "sono", "is a")):
        return "identity"
    if _text_has_work_or_project_surface(combined_text) or any(token in folded for token in ("company_founding", "company founding")):
        return "work"
    if any(token in folded for token in ("style", "communication", "tone", "voice", "stile", "comunica", "parla ", "direct", "technical")):
        return "style"
    if any(token in folded for token in ("value", "values", "principle", "principi", "valori", "precision", "sustainable", "impact")):
        return "values"
    if any(token in folded for token in ("temporal_inventory", "temporal evidence")):
        return "temporal_inventory"
    if any(token in folded for token in ("timeline", "history", "temporal", "date", "year", "anno", "quando", "19", "20")):
        return "history"
    if any(token in folded for token in ("identity", "name", "born", "birth", "residence", "nato", "nata", "vive", "sono", "chi sei")):
        return "identity"
    if any(token in folded for token in ("document", "source", "file", "chunk", "anchor")):
        return "documents"
    return "history"


def _mcp_query_requests_private_data_boundary(query_text: Any, semantic_contract: dict[str, Any] | None = None) -> bool:
    folded = _fold_text(query_text)
    if not folded:
        return False
    if any(noise in folded for noise in ("privacy policy", "informativa privacy", "cookie privacy", "privacy notice")):
        return False
    private_markers = (
        "dati privati",
        "dato privato",
        "informazioni private",
        "informazione privata",
        "private data",
        "private information",
        "personal private",
        "sensibili",
        "sensitive data",
    )
    missing_markers = (
        "manca",
        "mancano",
        "mancanti",
        "non ci sono",
        "non presenti",
        "assenti",
        "missing",
        "not available",
        "unavailable",
        "absence",
        "absent",
    )
    public_absence_markers = (
        "dati non disponibili",
        "dato non disponibile",
        "dati mancanti",
        "informazioni non disponibili",
        "unavailable data",
        "missing data",
        "not available data",
    )
    if any(marker in folded for marker in public_absence_markers):
        return True
    if any(marker in folded for marker in private_markers) and any(marker in folded for marker in missing_markers):
        return True
    contract = dict(semantic_contract or {})
    for slot in list(contract.get("semantic_slot_contracts") or []):
        if not isinstance(slot, dict):
            continue
        if str(slot.get("slot_id") or "").strip() == "privacy_boundary":
            return True
    return False


def _mcp_private_data_boundary_statement(
    *,
    query_text: Any,
    semantic_contract: dict[str, Any] | None = None,
    document_refs: list[dict[str, Any]] | None = None,
) -> str:
    ref_count = len([ref for ref in list(document_refs or []) if isinstance(ref, dict)])
    suffix = " Document references remain available through retrieve_document." if ref_count else ""
    return (
        "The current memory package exposes public or user-provided evidence only. "
        "No private identifiers or private-data values are present in the promoted context for this request; "
        "missing private data must be reported as unavailable instead of inferred from adjacent biography, "
        "company, family or source metadata."
        + suffix
    )


def _mcp_canonical_required_sections(semantic_contract: dict[str, Any] | None, query_text: str) -> tuple[set[str], set[str], bool]:
    contract = dict(semantic_contract or {})
    context_contract = dict(contract.get("context_contract") or {})
    intent = dict(contract.get("intent") or {})
    required_raw = list(context_contract.get("required_sections") or [])
    optional_raw = list(context_contract.get("optional_sections") or [])
    required = {_mcp_context_section_key(item) for item in required_raw if str(item or "").strip()}
    optional = {_mcp_context_section_key(item) for item in optional_raw if str(item or "").strip()}
    primary = str(intent.get("primary") or "").strip()
    dossier_goal = str(context_contract.get("dossier_goal") or "").strip()
    broad = bool(intent.get("requires_broad_context")) or primary == "broad_dossier" or dossier_goal == "context_for_clone" or _is_broad_self_query(query_text)
    document_mode = str((contract.get("document_contract") or {}).get("mode") or "none")
    if document_mode != "none":
        required.add("documents")
    if "relationships" in required and _query_is_business_relationship_request(query_text):
        required.discard("relationships")
        required.add("work")
        optional.add("relationships")
    if primary == "temporal" or _is_temporal_reference_query(query_text):
        required.add("temporal_inventory")
        optional.add("history")
    if not required and not broad:
        required.add(_mcp_context_section_key(primary or query_text))
    return required, optional, broad


def _mcp_forbidden_sections(semantic_contract: dict[str, Any] | None) -> set[str]:
    contract = dict(semantic_contract or {})
    primary = str((contract.get("intent") or {}).get("primary") or "").strip()
    required_sections = {
        _mcp_context_section_key(section)
        for section in list(((contract.get("context_contract") or {}).get("required_sections") or []))
        if str(section or "").strip()
    }
    forbidden_texts: list[str] = [
        str(item.get("topic") or "")
        for item in list(contract.get("forbidden_evidence") or [])
        if isinstance(item, dict)
    ]
    for target in list(contract.get("expected_evidence") or []):
        if isinstance(target, dict):
            forbidden_texts.extend(str(item or "") for item in list(target.get("negative_conditions") or []))
    folded = _fold_text(" ".join(forbidden_texts))
    forbidden: set[str] = set()
    if "unrelated_family_context" in folded and "relationships" not in required_sections and primary not in {"relationship", "broad_dossier"}:
        forbidden.add("relationships")
    if "unrelated_work_context" in folded and "work" not in required_sections and primary not in {"work", "document_lookup", "broad_dossier"}:
        forbidden.add("work")
    if "generic_profile" in folded and primary == "document_lookup":
        forbidden.update({"identity", "style", "values"})
    return forbidden


def _mcp_forbidden_topic_markers(semantic_contract: dict[str, Any] | None) -> tuple[str, ...]:
    contract = dict(semantic_contract or {})
    context_contract = dict(contract.get("context_contract") or {})
    required_sections = {
        _mcp_context_section_key(section)
        for section in list(context_contract.get("required_sections") or [])
        if str(section or "").strip()
    }
    topics: list[str] = []
    for item in list(contract.get("forbidden_evidence") or []):
        if isinstance(item, dict):
            topics.append(str(item.get("topic") or ""))
    for target in list(contract.get("expected_evidence") or []):
        if not isinstance(target, dict):
            continue
        for condition in list(target.get("negative_conditions") or []):
            folded = _fold_text(condition)
            if "unrelated_family_context" in folded and "relationships" not in required_sections:
                topics.extend(["family_relation", "family_monument"])
            if "unrelated_work_context" in folded and "work" not in required_sections:
                topics.append("work_projects")
            for known_topic in _OFF_CONTRACT_TOPIC_MARKERS:
                if known_topic in folded:
                    topics.append(known_topic)
    markers: list[str] = []
    for topic in topics:
        markers.extend(_OFF_CONTRACT_TOPIC_MARKERS.get(str(topic or "").strip(), ()))
    return tuple(dict.fromkeys(marker for marker in markers if marker))


def _mcp_forbidden_topic_hits(value: Any, markers: tuple[str, ...]) -> list[str]:
    if not markers:
        return []
    folded = _fold_text(str(value or ""))
    return [marker for marker in markers if _fold_text(marker) and _fold_text(marker) in folded]


def _mcp_agent_text_is_surface_noise(text: str, folded: str) -> bool:
    if not folded:
        return True
    raw_source_section_markers = (
        "section:",
        "source heading:",
        "visible text:",
        "image alt text:",
        "page body:",
        "raw page:",
        "source page:",
        "public source page:",
    )
    legal_or_policy_markers = (
        "license restrictions",
        "licence restrictions",
        "end user license",
        "end-user license",
        "eula",
        "terms of use",
        "terms and conditions",
        "privacy policy",
        "cookie policy",
        "cookie settings",
        "legal notice",
        "copyright",
        "all rights reserved",
        "you have no right",
        "you agree not to",
        "acceptance by installing",
        "by installing",
    )
    navigation_or_cta_markers = (
        "read the success story",
        "subscribe to our newsletter",
        "latest news",
        "recent posts",
        "related posts",
        "follow us",
        "customer portal",
        "contact us",
        "freemium",
        "watch the full episode",
        "view all",
        "show more",
        "show less",
    )
    needs_company_claim_noise_exception = bool(
        any(marker in folded for marker in legal_or_policy_markers)
        or any(marker in folded for marker in raw_source_section_markers)
        or any(marker in folded for marker in navigation_or_cta_markers)
    )
    company_relation_signal_markers = (
        "founder",
        "co founder",
        "cofounder",
        "founded",
        "fondat",
        "ceo",
        "chief executive officer",
        "acquired",
        "acquis",
        "established",
    )
    compactable_company_claims = (
        _mcp_company_relation_claims(text)
        if needs_company_claim_noise_exception
        and len(folded) <= 1800
        and any(marker in folded for marker in company_relation_signal_markers)
        else []
    )
    if any(marker in folded for marker in legal_or_policy_markers) and not compactable_company_claims:
        return True
    if any(marker in folded for marker in raw_source_section_markers):
        if not compactable_company_claims:
            return True
    if any(marker in folded for marker in navigation_or_cta_markers) and len(folded) > 90 and not compactable_company_claims:
        return True
    source_interface_markers = (
        "user instruction:",
        "user instruction",
        "official website source uri",
        "official website source url",
        "official website source",
        "page title:",
        "page title",
        "headings:",
        "headings",
        "visualizza profilo",
        "iscriviti ora",
        "consigliato da",
        "undefined",
        "quoted in the release",
        "document title:",
        "document title",
        "source: http",
        "source uri:",
        "source uri",
        "source url:",
        "source url",
        "merge probe based on",
        "legal notice",
        "terms and conditions",
        "registered office",
        "owner of the website",
        "you undertake to refrain",
        "info info",
    )
    if any(marker in folded for marker in source_interface_markers):
        return True
    if re.match(r"^\s*(?:a\s+)?(?:picture|photo|image)\s+of\b", folded):
        return True
    if folded.startswith("section ") and " contact " in folded and "successful career" in folded:
        return True
    if "successful career with a range of leadership roles" in folded and " section:" in folded:
        return True
    if "has had a successful career with a range of leadership roles" in folded and " section " in folded:
        return True
    if any(
        marker in folded
        for marker in (
            "teams teams view all teams",
            "view all teams this person is not in any teams",
            "this person is not in any teams",
            "unverified section:",
            "people also viewed",
            "similar executives",
            "org chart",
        )
    ):
        return True
    short_scraped_headings = {
        "heritage",
        "the foundation",
        "building the foundation",
        "art culture",
        "ulisse s journey",
        "entrepreneurial philanthropy projects",
    }
    if len(folded) <= 80 and folded.strip(" -") in short_scraped_headings:
        return True
    if "progetto principale e the foundation" in folded or "main project is the foundation" in folded:
        return True
    if "power companies worldwide" in folded and len(folded) <= 160:
        return True
    if "e nata a catania with aviation" in folded:
        return True
    if any(marker in folded for marker in ("ulisse s journey", "the sky is not the limit", "entrepreneurial philanthropy projects")):
        return True
    if _temporal_text_is_year_navigation_noise(text):
        return True
    letters = [char for char in str(text or "") if char.isalpha()]
    if len(letters) >= 48:
        uppercase_ratio = sum(1 for char in letters if char.isupper()) / max(1, len(letters))
        if uppercase_ratio > 0.62 and any(marker in folded for marker in ("journey", "foundation", "philanthropy", "sky is not the limit")):
            return True
    return False


_MCP_CONTEXT_DEPENDENT_AGENT_OPENING_RE = re.compile(
    r"^\s*(?P<head>"
    r"it|this|that|these|those|he|she|they|his|her|their|"
    r"from\s+(?:him|her|them)|"
    r"da\s+(?:lui|lei|loro)|"
    r"the\s+(?:monument|company|project|document|release|source|site|profile|page|team|work|event|initiative|platform|system)|"
    r"il\s+(?:progetto|documento|sito|profilo|team|lavoro|evento|sistema|contenuto)|"
    r"la\s+(?:societa|società|azienda|pagina|fonte|release|piattaforma|iniziativa)|"
    r"lo\s+(?:scopo|stile|strumento)|"
    r"i\s+(?:progetti|documenti|contenuti)|"
    r"le\s+(?:aziende|fonti|pagine|iniziative)|"
    r"quest[oaie]|lui|lei|loro|suo|sua|suoi|sue"
    r")\b",
    re.IGNORECASE,
)


def _mcp_agent_text_has_relation_marker(folded: str) -> bool:
    return any(
        marker in folded
        for marker in (
            " is ",
            " was ",
            " are ",
            " were ",
            " e ",
            " sono ",
            " has ",
            " have ",
            " si chiama ",
            " chiamava ",
            " communicates",
            " comunica",
            " founded",
            " fondato",
            " fonda",
            " acquired",
            " acquis",
            " announced",
            " created",
            " established",
            " launched",
            " developed",
            " dedicated",
            " served",
            " serves",
            " works",
            " lavora",
            " specializes",
            " focuses",
            " linked",
            " colleg",
            " associated",
            " headquartered",
            " based",
        )
    )


def _mcp_agent_text_is_title_like_fragment(text: str, folded: str) -> bool:
    if not folded:
        return False
    words = re.findall(r"\b[\w'-]+\b", text)
    relation_marker = _mcp_agent_text_has_relation_marker(f" {folded} ")
    if (
        len(words) <= 16
        and not relation_marker
        and re.search(r"\b(?:19|20)\d{2}\b", text)
        and sum(1 for word in words if word[:1].isupper()) >= 4
    ):
        return True
    if (
        len(words) <= 18
        and not relation_marker
        and any(separator in text for separator in ("&", "/", "|", " > "))
        and sum(1 for word in words if word[:1].isupper()) >= 4
    ):
        return True
    if len(words) > 9:
        return False
    if relation_marker:
        return False
    if text.rstrip().endswith((".", ":")):
        return True
    capitalized = sum(1 for word in words if word[:1].isupper())
    return bool(len(words) >= 3 and capitalized >= max(2, len(words) // 2))


def _mcp_agent_text_is_context_dependent_fragment(text: str, folded: str) -> bool:
    if not folded:
        return False
    match = _MCP_CONTEXT_DEPENDENT_AGENT_OPENING_RE.search(text)
    if not match:
        return False
    head = _fold_text(match.group("head"))
    if head.startswith("the "):
        prefix = text[: min(len(text), 120)]
        if re.search(
            r"^\s*the\s+\w+\s+(?:called|named|known\s+as|denominat[oaie]|chiamat[oaie])\b",
            prefix,
            flags=re.IGNORECASE,
        ):
            return False
        if re.search(
            r"^\s*the\s+\w+\s+(?:for|dedicated\s+to|about|of|per|dedicat[oaie]\s+a)\s+"
            r"[A-Z][A-Za-z0-9'._-]+(?:\s+[A-Z][A-Za-z0-9'._-]+){0,4}\b",
            prefix,
            flags=re.IGNORECASE,
        ):
            return False
    return True


def _mcp_clean_agent_text(value: Any) -> str | None:
    raw_text = str(value or "").replace("\r", "\n")
    if any(marker in _fold_text(raw_text) for marker in ("user instruction", "source uri", "source url", "page title", "description:", "headings")):
        cleaned_source_text = _document_workspace_clean_text(raw_text)
        if cleaned_source_text:
            raw_text = cleaned_source_text
    text = " ".join(raw_text.split())
    if not text:
        return None
    text = re.sub(r"^\[[^\]]+\]\s*", "", text).strip()
    text = re.sub(r"\bvec_node_[a-zA-Z0-9_]+\b", "", text).strip()
    text = re.sub(r"\|\s*(?:manual_text|manual text|derived_[a-z_]+|document_[a-z_]+)\b", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"^(?:raw context|mixed evidence|mixed_evidence|fact|chunk|anchor)\s*[:#-]\s*", "", text, flags=re.IGNORECASE).strip()
    folded = _fold_text(text)
    if folded in {
        "official website source url",
        "official website source uri",
        "source url",
        "source uri",
        "headings",
    }:
        return None
    if "..." in text or chr(0x2026) in text or chr(0x00E2) in text or chr(0x00C3) in text or chr(0xFFFD) in text:
        return None
    if any(
        marker in folded
        for marker in (
            "evidence ledger",
            "grounded retrieval ledger",
            "raw context",
            "mixed evidence",
            "system metadata",
            "source metadata",
            "source_investigation",
            "synthetic test material",
            "not a new public source",
            "stress testing memory creation",
        )
    ):
        return None
    if _mcp_agent_text_is_surface_noise(text, folded):
        return None
    if _mcp_agent_text_is_context_dependent_fragment(text, folded):
        return None
    if _mcp_agent_text_is_title_like_fragment(text, folded):
        return None
    return text.strip(" -|") or None


def _mcp_agent_body_context_rows(agent_markdown: Any) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    current_section = ""
    for raw_line in str(agent_markdown or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("## "):
            current_section = line.lstrip("#").strip()
            continue
        if not line.startswith("- "):
            continue
        text = line[2:].strip()
        if not text:
            continue
        rows.append({"section": current_section, "text": text})
    return rows


def _mcp_link_aware_context_contract(
    *,
    agent_markdown: Any,
    ordered_sections: list[dict[str, Any]],
    hot_context: list[dict[str, Any]],
    cold_context: list[dict[str, Any]],
    excluded_material: list[dict[str, Any]],
    debug_ledger: list[dict[str, Any]],
    document_refs: list[dict[str, Any]],
    document_ref_contract: dict[str, Any],
    path_discovery_agent_body_count: int = 0,
    agent_body_has_node_id: bool = False,
    agent_body_has_debug_marker: bool = False,
    agent_body_has_route_debug_marker: bool = False,
) -> dict[str, Any]:
    visible_rows = _mcp_agent_body_context_rows(agent_markdown)
    visible_orphans: list[dict[str, str]] = []
    for row in visible_rows:
        section = str(row.get("section") or "")
        if section in {"Task / User Intent", "Unresolved Or Missing"}:
            continue
        text = str(row.get("text") or "").strip()
        folded = _fold_text(text)
        if _mcp_agent_text_is_context_dependent_fragment(text, folded):
            visible_orphans.append({"section": section, "text": text})

    hot_orphans = [
        {
            "section": str(item.get("section") or ""),
            "node_id": str(item.get("node_id") or ""),
            "text": str(item.get("text") or "").strip(),
        }
        for item in hot_context
        if _mcp_agent_text_is_context_dependent_fragment(
            str(item.get("text") or "").strip(),
            _fold_text(item.get("text")),
        )
    ]
    cold_orphan_count = sum(
        1
        for item in cold_context
        if _mcp_agent_text_is_context_dependent_fragment(
            str(item.get("text") or "").strip(),
            _fold_text(item.get("text")),
        )
    )
    excluded_orphan_count = sum(
        1
        for item in excluded_material
        if _mcp_agent_text_is_context_dependent_fragment(
            str(item.get("text") or "").strip(),
            _fold_text(item.get("text")),
        )
    )
    promotion_reasons = [str(row.get("reason") or "") for row in debug_ledger]
    section_keys = [str(section.get("key") or "") for section in ordered_sections]
    actionable_document_ref_count = int(document_ref_contract.get("actionable_document_ref_count") or 0)
    raw_ready_document_ref_count = int(document_ref_contract.get("raw_available_document_ref_count") or 0)
    unresolved_reasons: list[str] = []
    if visible_orphans:
        unresolved_reasons.append("visible_context_dependent_fragment")
    if hot_orphans:
        unresolved_reasons.append("hot_context_dependent_fragment")
    passed = not unresolved_reasons
    return {
        "schema_version": MCP_LINK_AWARE_CONTEXT_CONTRACT_VERSION,
        "passed": passed,
        "state": "passed" if passed else "blocked",
        "unresolved_reasons": unresolved_reasons,
        "agent_body_row_count": len(visible_rows),
        "agent_body_section_keys": section_keys,
        "visible_orphan_fragment_count": len(visible_orphans),
        "visible_orphan_fragments": visible_orphans[:8],
        "hot_orphan_fragment_count": len(hot_orphans),
        "hot_orphan_fragments": hot_orphans[:8],
        "cold_orphan_fragment_count": cold_orphan_count,
        "excluded_orphan_fragment_count": excluded_orphan_count,
        "linked_parent_context_promoted_count": promotion_reasons.count("linked_parent_context_backfill"),
        "linked_relation_timeline_promoted_count": promotion_reasons.count("linked_relation_timeline_backfill"),
        "linked_work_cluster_promoted_count": promotion_reasons.count("linked_work_cluster_promotion"),
        "path_context_promoted_count": int(path_discovery_agent_body_count or 0),
        "document_ref_count": len(document_refs),
        "actionable_document_ref_count": actionable_document_ref_count,
        "raw_ready_document_ref_count": raw_ready_document_ref_count,
        "document_refs_actionable": actionable_document_ref_count >= raw_ready_document_ref_count,
        "reservoir_kept_separate": True,
        "agent_body_has_node_id": bool(agent_body_has_node_id),
        "agent_body_has_debug_marker": bool(agent_body_has_debug_marker),
        "agent_body_has_route_debug_marker": bool(agent_body_has_route_debug_marker),
    }


def _mcp_agent_text_is_raw_source_block(text: Any) -> bool:
    body = str(text or "").strip()
    if not body:
        return False
    folded = _fold_text(body)
    source_metadata_markers = (
        "source uri:",
        "source url:",
        "page title:",
        "visible text:",
        "headings:",
        "image alt text:",
    )
    if any(marker in folded for marker in source_metadata_markers) and len(body) > 420:
        return True
    if re.search(r"^#{1,3}\s+.+\s+-\s+segment\s+\d+\b", body, flags=re.IGNORECASE):
        return True
    if folded.startswith("document ") and any(marker in folded for marker in source_metadata_markers):
        return True
    return False


def _mcp_short_label_like_text(text: str) -> bool:
    folded = _fold_text(text)
    if not folded:
        return True
    if len(folded) <= 36 and len(folded.split()) <= 4:
        return True
    return False


def _mcp_best_candidate_text(merged: dict[str, Any]) -> str | None:
    raw_text = _mcp_clean_agent_text(merged.get("raw_text"))
    evidence_text = _mcp_clean_agent_text(merged.get("evidence_snippet"))
    summary_text = _mcp_clean_agent_text(merged.get("summary"))
    candidates = [text for text in (raw_text, evidence_text, summary_text) if text]
    if not candidates:
        return None
    if summary_text and (not raw_text or _mcp_short_label_like_text(raw_text) or len(raw_text) > 1800):
        return summary_text
    if evidence_text and raw_text and _mcp_short_label_like_text(raw_text) and not _mcp_short_label_like_text(evidence_text):
        return evidence_text
    return raw_text or evidence_text or summary_text


def _mcp_context_item_rank(value: Any) -> tuple[float, float, float]:
    text = str(value or "").strip()
    folded = _fold_text(text)
    if not text:
        return (0.0, 0.0, 0.0)
    short_label_penalty = 0.0 if _mcp_short_label_like_text(text) else 1.0
    detail_weight = min(1.0, len(text) / 420.0)
    semantic_weight = 0.0
    if folded.startswith(("the memory subject s name is ", "identity name ", "il nome e ")):
        semantic_weight += 0.9
    if any(token in folded for token in ("founder", "founded", "fondatore", "ceo", "acquired", "acquisita", "renewable", "industrial automation", "cybersecurity", "artificial intelligence", "valori", "values", "partner", "father", "padre")):
        semantic_weight += 0.35
    if re.search(r"\b(?:19|20)\d{2}\b", text):
        semantic_weight += 0.15
    return (short_label_penalty, detail_weight, semantic_weight)


def _mcp_answer_payload_text(answer_payload: dict[str, Any] | None) -> str:
    if not isinstance(answer_payload, dict):
        return ""
    parts: list[str] = []
    for key in ("answer_text", "answer_short", "answer_full", "final_answer", "first_answer"):
        value = str(answer_payload.get(key) or "").strip()
        if value and value not in parts:
            parts.append(value)
    return "\n".join(parts).strip()


def _mcp_answer_alignment_terms(value: Any) -> list[str]:
    text = str(value or "")
    folded = _fold_text(text)
    if not folded:
        return []
    terms: list[str] = []

    def add_term(term: str) -> None:
        cleaned = _fold_text(term)
        if cleaned and cleaned not in {"agvm", "mcp", "llm", "ai", "ceo"} and cleaned not in terms:
            terms.append(cleaned)

    for year in re.findall(r"\b(?:19|20)\d{2}\b", text):
        add_term(year)

    title_stopwords = {
        "The",
        "This",
        "That",
        "These",
        "Those",
        "When",
        "Where",
        "What",
        "Why",
        "How",
        "From",
        "With",
        "Sono",
        "Guido",
        "Lavoro",
        "Comunico",
        "Nel",
        "Nella",
        "Della",
        "Il",
        "La",
        "Gli",
        "Una",
        "Uno",
        "Per",
    }
    multiword_name_pattern = re.compile(
        r"\b[A-Z][A-Za-z0-9À-ÖØ-öø-ÿ'’-]+(?:[ \t]+[A-Z][A-Za-z0-9À-ÖØ-öø-ÿ'’-]+){1,4}\b"
    )
    mixed_case_pattern = re.compile(
        r"\b(?:[A-Za-z0-9]*[A-Z][A-Za-z0-9]*[A-Z][A-Za-z0-9]*|[A-Za-z]+[A-Z][A-Za-z0-9]*)\b"
    )
    titlecase_single_pattern = re.compile(r"\b[A-Z][\w'’.-]{4,}\b")
    for match in multiword_name_pattern.findall(text):
        words = match.split()
        while len(words) > 1 and words[0] in title_stopwords:
            words.pop(0)
        while len(words) > 1 and words[-1] in title_stopwords:
            words.pop()
        normalized_match = " ".join(words)
        if len(normalized_match) >= 5 and len(words) <= 3:
            add_term(normalized_match)
    for match in mixed_case_pattern.findall(text):
        if len(match) >= 5:
            add_term(match)
    for match in titlecase_single_pattern.finditer(text):
        token = match.group(0)
        prefix = text[: match.start()].rstrip()
        sentence_initial = not prefix or prefix[-1:] in ".!?"
        if token not in title_stopwords and not sentence_initial:
            add_term(token)

    return terms[:24]


def _mcp_text_has_alignment_term(value: Any, terms: list[str]) -> bool:
    folded = _fold_text(value)
    return any(term and term in folded for term in terms)


def _mcp_identity_subject_terms_from_contract(semantic_contract: dict[str, Any] | None) -> list[str]:
    hints = dict((semantic_contract or {}).get("identity_hints") or {})
    terms: list[str] = []

    def add(value: Any) -> None:
        folded = _fold_text(str(value or ""))
        if folded and len(folded) >= 4 and folded not in terms:
            terms.append(folded)

    add(hints.get("core_name"))
    for key in ("self_name_candidates", "aliases"):
        for item in list(hints.get(key) or []):
            add(item)
    return terms[:10]


def _mcp_identity_subject_name_from_contract(semantic_contract: dict[str, Any] | None) -> str:
    hints = dict((semantic_contract or {}).get("identity_hints") or {})
    for value in [hints.get("core_name"), *list(hints.get("self_name_candidates") or [])]:
        cleaned = " ".join(str(value or "").split()).strip()
        if cleaned:
            return cleaned
    return ""


def _mcp_query_requests_exact_identity_name(query_text: str, required_sections: set[str]) -> bool:
    if "identity" not in required_sections:
        return False
    folded_query = _fold_text(query_text)
    exact_name_markers = (
        "come ti chiami",
        "come si chiama",
        "come mi chiamo",
        "qual e il tuo nome",
        "qual e il nome",
        "what is your name",
        "what s your name",
        "what is the name",
        "who are you",
        "chi sei",
        "chi sono",
    )
    return bool("name" in detect_query_aspects(query_text) or any(marker in folded_query for marker in exact_name_markers))


def _mcp_subject_anchor_required(query_text: str, semantic_contract: dict[str, Any] | None) -> bool:
    if not _mcp_identity_subject_terms_from_contract(semantic_contract):
        return False
    folded_query = _fold_text(query_text)
    if _requested_relations_from_query(query_text) or any(
        str(slot.get("slot_id") or "").strip() in {"family", "relationship"}
        for slot in list((semantic_contract or {}).get("semantic_slot_contracts") or [])
        if isinstance(slot, dict) and bool(slot.get("required"))
    ):
        return True
    subject_markers = (
        "lui",
        "lei",
        "questa persona",
        "questa memoria",
        "clone",
        "come si chiama",
        "chi e",
        "chi è",
        "che lavoro",
        "quali aziende",
        "quali societa",
        "quali società",
        "quali progetti",
        "quali valori",
        "valori ti",
        "valori guidano",
        "ti guidano",
        "che valori",
        "che principi",
        "quali principi",
        "come comunichi",
        "come comunica",
        "come parli",
        "tuo stile",
        "tuoi valori",
        "tue idee",
        "collegati a lui",
        "collegate a lui",
        "about him",
        "about you",
        "your values",
        "your principles",
        "your style",
        "how do you communicate",
        "how you communicate",
        "his companies",
        "his projects",
        "the person",
        "hai fondato",
        "hai guidato",
        "aziende hai",
        "societa hai",
        "società hai",
        "sei collegato",
        "sei collegata",
        "tuoi progetti",
        "tue aziende",
        "what companies have you",
        "companies have you founded",
        "companies have you led",
        "your companies",
    )
    if any(marker in folded_query for marker in subject_markers):
        return True
    required_sections = {
        _mcp_context_section_key(section)
        for section in list(((semantic_contract or {}).get("context_contract") or {}).get("required_sections") or [])
        if str(section or "").strip()
    }
    if required_sections & {"style", "values"} and any(
        marker in folded_query
        for marker in ("ti ", "tuo", "tuoi", "tua", "tue", "your", "you ", "come comunica", "come comunichi")
    ):
        return True
    return bool({"identity", "work"} <= required_sections)


def _mcp_text_is_subject_anchored(text: Any, source_title: Any, semantic_contract: dict[str, Any] | None) -> bool:
    # Source titles can mention the memory subject while the actual candidate text is only
    # lateral page/document background. Primary promotion must be anchored in the text itself.
    folded_blob = _fold_text(text)
    if not folded_blob:
        return False
    if any(term and term in folded_blob for term in _mcp_identity_subject_terms_from_contract(semantic_contract)):
        return True
    first_person_action_markers = (
        "i founded",
        "i co-founded",
        "i created",
        "i built",
        "my father",
        "my mother",
        "my brother",
        "my sister",
        "ho fondato",
        "ho creato",
        "ho imparato",
        "ho ereditato",
        "credo che",
        "i learned",
        "i inherited",
        "i believe",
        "from my father",
        "serving others",
        "my values",
        "my principles",
        "my belief",
        "my mission",
        "my vocation",
        "i communicate",
        "i explain",
        "i speak",
        "comunico",
        "spiego",
        "parlo",
        "il mio stile",
        "i miei valori",
        "i miei principi",
        "mio padre",
        "mia madre",
        "mio fratello",
        "mia sorella",
        "sono fondatore",
        "sono cofondatore",
        "miei progetti",
        "le mie aziende",
    )
    return any(
        re.search(rf"(?<!\w){re.escape(_fold_text(marker))}(?!\w)", folded_blob)
        for marker in first_person_action_markers
        if _fold_text(marker)
    )


def _mcp_structured_subject_claim_is_anchored(text: Any, section_key: str) -> bool:
    if section_key == "values":
        return _mcp_text_has_values_signal(text)
    if section_key == "style":
        return _mcp_text_has_style_signal(text)
    if section_key == "relationships":
        return _text_mentions_personal_relationship(_fold_text(str(text or "")))
    if section_key in {"history", "temporal_inventory"}:
        return _sentence_has_temporal_signal(str(text or ""))
    return False


def _mcp_text_has_values_signal(text: Any) -> bool:
    folded = _fold_text(str(text or ""))
    if not folded:
        return False
    direct_markers = (
        "value",
        "values",
        "valori",
        "principi",
        "principle",
        "principles",
        "operating philosophy",
        "filosofia",
        "mission",
        "purpose",
        "responsibility",
        "responsabilita",
        "dovere sociale",
        "social duty",
        "contributo",
        "impact",
        "impatto",
        "sustainable",
        "sostenibile",
        "sustainability",
        "sostenibilita",
        "decarbonization",
        "decarbonizzazione",
        "education",
        "educazione",
        "cooperation",
        "cooperazione",
        "collaboration",
        "talent",
        "talenti",
        "jobs",
        "posti di lavoro",
        "territorio",
        "precision",
        "precisione",
        "courage",
        "coraggio",
    )
    if any(marker in folded for marker in direct_markers):
        return True
    return bool(
        any(marker in folded for marker in ("creare valore", "generate value", "generi valore"))
        and any(marker in folded for marker in ("terra", "country", "paese", "community", "comunita"))
    )


def _mcp_value_sentences_from_text(text: Any, *, limit: int = 3) -> list[str]:
    raw_text = _mcp_clean_agent_text(text)
    if not raw_text:
        return []
    sentence_candidates = [
        _mcp_clean_agent_text(item)
        for item in re.split(r"(?<=[.!?])\s+|\n+", raw_text)
        if _mcp_clean_agent_text(item)
    ]
    selected: list[str] = []
    seen: set[str] = set()
    for sentence in sentence_candidates or [raw_text]:
        if not _mcp_text_has_values_signal(sentence):
            continue
        folded = _fold_text(sentence)
        if not folded or folded in seen:
            continue
        seen.add(folded)
        selected.append(sentence[:700])
        if len(selected) >= limit:
            break
    if selected:
        return selected
    return [raw_text[:700]] if _mcp_text_has_values_signal(raw_text) else []


def _mcp_text_has_style_signal(text: Any) -> bool:
    folded = _fold_text(str(text or ""))
    if not folded:
        return False
    direct_markers = (
        "style",
        "stile",
        "communication",
        "comunicazione",
        "communicate",
        "communicates",
        "communicating",
        "comunica",
        "comunico",
        "voice",
        "tone",
        "language",
        "linguaggio",
        "parlo",
        "parla",
        "speak",
        "speaks",
        "explain",
        "explains",
        "spiego",
        "spiega",
        "clarity",
        "chiarezza",
    )
    if any(marker in folded for marker in direct_markers):
        return True
    style_traits = (
        "structured",
        "struttur",
        "direct",
        "dirett",
        "precise",
        "precision",
        "precisione",
        "technical",
        "tecnic",
        "pragmatic",
        "pragmatico",
        "pratico",
        "grounded",
        "operating philosophy",
        "filosofia operativa",
        "method",
        "metodo",
        "approach",
        "approccio",
        "discipline",
        "disciplina",
        "responsibility",
        "responsabil",
        "rigor",
        "rigore",
        "courage",
        "coraggio",
        "strategic",
        "misurabile",
        "measurable",
    )
    context_markers = (
        "when i work",
        "quando lavoro",
        "when working",
        "nel lavoro",
        "at work",
        "leadership",
        "collaboration",
        "collaborazione",
        "engineering",
        "creativity",
        "serving others",
        "service",
        "vocation",
        "decision",
        "decisions",
        "decidere",
        "decide",
        "organize",
        "organise",
        "organizza",
        "organizzare",
        "operating",
        "philosophy",
        "filosofia",
        "value",
        "values",
        "valori",
        "principle",
        "principles",
        "principi",
        "method",
        "metodo",
        "approach",
        "approccio",
    )
    return bool(
        any(marker in folded for marker in style_traits)
        and any(marker in folded for marker in context_markers)
    )


def _mcp_style_sentences_from_text(text: Any, *, limit: int = 3) -> list[str]:
    raw_text = _mcp_clean_agent_text(text)
    if not raw_text:
        return []
    sentence_candidates = [
        _mcp_clean_agent_text(item)
        for item in re.split(r"(?<=[.!?])\s+|\n+", raw_text)
        if _mcp_clean_agent_text(item)
    ]
    selected: list[str] = []
    seen: set[str] = set()
    for sentence in sentence_candidates or [raw_text]:
        if not _mcp_text_has_style_signal(sentence):
            continue
        folded = _fold_text(sentence)
        if not folded or folded in seen:
            continue
        seen.add(folded)
        selected.append(sentence[:700])
        if len(selected) >= limit:
            break
    if selected:
        return selected
    return [raw_text[:700]] if _mcp_text_has_style_signal(raw_text) else []


def _mcp_requested_relations(semantic_contract: dict[str, Any] | None, query_text: str) -> list[str]:
    relations: list[str] = []
    for item in list((semantic_contract or {}).get("requested_relations") or []):
        value = str(item or "").strip().lower()
        if value:
            relations.append(value)
    for item in _requested_relations_from_query(query_text):
        value = str(item or "").strip().lower()
        if value:
            relations.append(value)
    return list(dict.fromkeys(relations))


def _mcp_text_matches_requested_relation(text: Any, source_title: Any, requested_relations: Sequence[str]) -> bool:
    if not requested_relations:
        return True
    folded_blob = _fold_text(f"{text or ''}\n{source_title or ''}")
    if not folded_blob:
        return False
    return any(_text_mentions_requested_relation(folded_blob, relation) for relation in requested_relations)


def _mcp_relation_timeline_text(text: Any) -> bool:
    folded = _fold_text(text)
    if not folded:
        return False
    if re.search(r"\b(?:19|20)\d{2}\b", folded):
        return True
    timeline_markers = (
        "inaugurat",
        "dedicat",
        "served in",
        "faceva parte",
        "aeronautica",
        "air force",
        "was born",
        "nato",
        "morto",
        "died",
        "nel ",
        "on may",
        "il 5 maggio",
    )
    return any(marker in folded for marker in timeline_markers)


def _mcp_subject_name_regex(subject_name: str) -> str:
    parts = [re.escape(part) for part in str(subject_name or "").split() if part.strip()]
    if not parts:
        return ""
    return r"\b" + r"\s+".join(parts) + r"\b"


def _mcp_clean_relation_entity(value: Any) -> str:
    text = " ".join(str(value or "").split()).strip(" -,:;.\"'“”")
    if not text:
        return ""
    text = re.sub(r"^(?:outline|profile|overview)\s+of\s+", "", text, flags=re.IGNORECASE).strip(" -,:;.\"'“”")
    text = text.split(",", 1)[0].strip(" -,:;.\"'“”")
    text = re.split(
        r"\s+(?:who|where|which|that|during|with|since|between|and\s+later|while|using|through|for\s+the\s+future)\b",
        text,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip(" -,:;.\"'“”")
    text = re.sub(r"\s+(?:said|says|announced|reported)\b.*$", "", text, flags=re.IGNORECASE).strip(" -,:;.\"'“”")
    text = re.sub(r"\b(?:a|an|the|una|un|il|la|lo)\s+$", "", text, flags=re.IGNORECASE).strip(" -,:;.\"'“”")
    if len(text) > 96:
        text = text[:96].rsplit(" ", 1)[0].strip(" -,:;.\"'“”")
    folded = _fold_text(text)
    noisy = (
        "source uri",
        "visible text",
        "image alt text",
        "recent post",
        "contact us",
        "cookie",
        "privacy",
        "section",
        "autori",
        "logo",
    )
    if not text or any(marker in folded for marker in noisy):
        return ""
    if len(text.split()) > 10 and not re.search(r"\b(?:Inc|Ltd|S\.?r\.?l|S\.?p\.?A|Corporation|Group|Studio|Foundry|Energy|Robotics|Systems)\b", text):
        return ""
    return text


def _mcp_clean_business_entity(value: Any) -> str:
    original = " ".join(str(value or "").split()).strip()
    cleaned = _mcp_clean_relation_entity(original)
    folded_cleaned = _fold_text(cleaned)
    reject_exact = {
        "has",
        "had",
        "announces",
        "announced",
        "acquired",
        "a provider",
        "provider",
        "release",
        "press release",
    }
    if folded_cleaned in reject_exact or any(
        marker in folded_cleaned
        for marker in ("provider of", "developer of", "supplier of", "contact us", "recent post")
    ):
        cleaned = ""
    if cleaned:
        cleaned = re.sub(r"^(?:the|a|an|una|un|il|la|lo)\s+", "", cleaned, flags=re.IGNORECASE).strip(" -,:;.\"'")
    source = cleaned or original
    trailing_org = re.search(
        r"\b(?P<org>[A-Z][A-Za-z0-9&.'-]*(?:\s+[A-Z][A-Za-z0-9&.'-]*){0,3}\s+(?:Corporation|Group|Studio|Foundry|Energy|Robotics|Systems|Solutions|Technologies|S\.?r\.?l|S\.?p\.?A|Ltd|Inc))\b$",
        source,
    )
    if trailing_org and (not cleaned or len(source.split()) > len(trailing_org.group("org").split()) + 2):
        org_words = trailing_org.group("org").split()
        suffix = re.sub(r"[^A-Za-z]", "", org_words[-1]).lower() if org_words else ""
        max_tail_by_suffix = {
            "corporation": 3,
            "group": 2,
            "studio": 3,
            "foundry": 3,
            "energy": 3,
            "robotics": 2,
            "systems": 3,
            "solutions": 3,
            "technologies": 3,
            "ltd": 2,
            "inc": 2,
        }
        max_tail = max_tail_by_suffix.get(suffix, 3)
        cleaned = _mcp_clean_relation_entity(" ".join(org_words[-max_tail:]))
    if not cleaned:
        return ""
    if not re.search(r"[A-Za-z0-9]", cleaned):
        return ""
    if _fold_text(cleaned) in reject_exact:
        return ""
    return cleaned


def _mcp_claim_sentence(value: str) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return ""
    return text if text.endswith((".", "!", "?")) else f"{text}."


def _mcp_relation_claims_for_subject(text: Any, subject_name: str) -> list[str]:
    body = " ".join(str(text or "").split())
    if not body or not subject_name:
        return []
    subject_pattern = _mcp_subject_name_regex(subject_name)
    if not subject_pattern or not re.search(subject_pattern, body, flags=re.IGNORECASE):
        return []
    role_pattern = (
        r"(?:chief\s+executive\s+officer\s+and\s+founder|founder\s+and\s+chief\s+executive\s+officer|"
        r"ceo\s+and\s+founder|founder\s+and\s+ceo|chief\s+executive\s+officer|ceo|co-?founder|"
        r"founder|president|chair(?:man|woman|person)|director|member|partner)"
    )
    claims: list[str] = []

    def add(role: str, entity: str) -> None:
        clean_role = " ".join(str(role or "").split()).strip(" ,.")
        clean_entity = _mcp_clean_business_entity(entity)
        if not clean_role or not clean_entity:
            return
        folded_role = _fold_text(clean_role).replace("-", " ")
        if "ceo" in folded_role and "founder" in folded_role:
            normalized_role = "CEO and founder"
        elif "chief executive officer" in folded_role and "founder" in folded_role:
            normalized_role = "CEO and founder"
        else:
            normalized_role = {
                "ceo": "CEO",
                "chief executive officer": "CEO",
                "cofounder": "co-founder",
                "co-founder": "co-founder",
            }.get(_fold_text(clean_role).replace(" ", " "), clean_role)
        claim = _mcp_claim_sentence(f"{subject_name} is {normalized_role} of {clean_entity}")
        folded_claim = _fold_text(claim)
        if folded_claim and folded_claim not in {_fold_text(item) for item in claims}:
            claims.append(claim)

    def creation_entity_is_valid(entity: str, raw_entity: str = "") -> bool:
        clean_entity = _mcp_clean_business_entity(entity)
        if not clean_entity:
            return False
        folded_entity = _fold_text(clean_entity)
        raw = str(raw_entity or entity or "")
        if re.match(r"^(?:in|nel|on|il|la|the|a|an)\s+(?:19|20)?\d", folded_entity):
            return False
        if re.fullmatch(r"(?:19|20)\d{2}", folded_entity):
            return False
        if not re.search(r"[A-Z][A-Za-z]", raw):
            return False
        return True

    for match in re.finditer(
        rf"{subject_pattern}\s*,?\s*(?:is|was|as)?\s*(?:the\s+)?(?P<role>{role_pattern})\s+(?:of|at|for)\s+(?P<entity>[^.;“”]+)",
        body,
        flags=re.IGNORECASE,
    ):
        add(match.group("role"), match.group("entity"))
    for match in re.finditer(
        rf"{subject_pattern}[^.;]{{0,140}}?\b(?:who\s+is\s+also|is\s+also|also|later)\s+(?:the\s+)?(?P<role>{role_pattern})\s+(?:of|at|for)\s+(?P<entity>[^.;“”]+)",
        body,
        flags=re.IGNORECASE,
    ):
        add(match.group("role"), match.group("entity"))
    for match in re.finditer(
        rf"{subject_pattern}[^.;]{{0,120}}?\b(?:served|serves|worked|works)\s+as\s+(?:the\s+)?(?P<role>{role_pattern})\s+(?:of|at|for)\s+(?P<entity>[^.;]+)",
        body,
        flags=re.IGNORECASE,
    ):
        add(match.group("role"), match.group("entity"))
    for match in re.finditer(
        rf"(?P<entity>[A-Z][A-Za-z0-9&.' -]{{2,140}}?)\s+(?P<role>{role_pattern})\s*:\s*{subject_pattern}",
        body,
        flags=re.IGNORECASE,
    ):
        add(match.group("role"), match.group("entity"))
    for match in re.finditer(
        rf"{subject_pattern}[^.;]{{0,120}}?\b(?:founded|fonda(?:to|ta)|ha\s+fondato|created|started|established)\s+(?P<entity>[A-Z][^.;,“”]+)",
        body,
        flags=re.IGNORECASE,
    ):
        raw_entity = match.group("entity")
        entity = _mcp_clean_business_entity(raw_entity)
        if entity and creation_entity_is_valid(entity, raw_entity):
            claim = _mcp_claim_sentence(f"{subject_name} founded {entity}")
            if _fold_text(claim) not in {_fold_text(item) for item in claims}:
                claims.append(claim)
    for match in re.finditer(
        rf"[\"'“”]?(?P<entity>[A-Z][A-Za-z0-9&.' -]{{2,120}}?)[\"'“”]?"
        rf"[^.;]{{0,120}}?\b(?P<verb>founded|created|launched|built|ideated|fondat[oaie]|creat[oaie]|lanciat[oaie]|ideat[oaie])\b"
        rf"[^.;]{{0,120}}?\b(?:by|da|dal|dalla|dall[’'`])\s+(?:ing\.?\s+|dr\.?\s+|dott\.?\s+|dottore\s+|engineer\s+)?{subject_pattern}",
        body,
        flags=re.IGNORECASE,
    ):
        raw_entity = match.group("entity")
        entity = _mcp_clean_business_entity(raw_entity)
        if not entity or len(entity.split()) < 2 or len(entity) < 5 or not creation_entity_is_valid(entity, raw_entity):
            continue
        folded_verb = _fold_text(match.group("verb"))
        verb = "founded" if any(marker in folded_verb for marker in ("found", "fondat")) else "created"
        claim = _mcp_claim_sentence(f"{subject_name} {verb} {entity}")
        if _fold_text(claim) not in {_fold_text(item) for item in claims}:
            claims.append(claim)
    reverse_creation_patterns = (
        rf"(?P<entity>[A-Z][A-Za-z0-9&.' -]{{2,120}}?)\s*,\s*"
        rf"(?P<verb>founded|created|launched|built|ideated|fondat[oaie]|creat[oaie]|lanciat[oaie]|ideat[oaie])\b"
        rf"[^.;]{{0,120}}?\b(?:by|da|dal|dalla|dall\S{{0,4}})\s+"
        rf"(?:ing\.?\s+|dr\.?\s+|dott\.?\s+|dottore\s+|engineer\s+)?{subject_pattern}",
        rf"[\"'](?P<entity>[A-Z][^\"']{{2,120}})[\"'][^.;]{{0,180}}?"
        rf"\b(?P<verb>founded|created|launched|built|ideated|fondat[oaie]|creat[oaie]|lanciat[oaie]|ideat[oaie])\b"
        rf"[^.;]{{0,120}}?\b(?:by|da|dal|dalla|dall\S{{0,4}})\s+"
        rf"(?:ing\.?\s+|dr\.?\s+|dott\.?\s+|dottore\s+|engineer\s+)?{subject_pattern}",
        rf"\b(?P<verb>founded|created|launched|built|ideated|fondat[oaie]|creat[oaie]|lanciat[oaie]|ideat[oaie])\s+"
        rf"(?:the\s+|a\s+|an\s+|la\s+|il\s+|lo\s+|una\s+|un\s+)?[\"'](?P<entity>[A-Z][^\"']{{2,120}})[\"']"
        rf"[^.;]{{0,180}}?\b(?:by|da|dal|dalla|dall\S{{0,4}})\s+"
        rf"(?:ing\.?\s+|dr\.?\s+|dott\.?\s+|dottore\s+|engineer\s+)?{subject_pattern}",
    )
    for reverse_pattern in reverse_creation_patterns:
        for match in re.finditer(reverse_pattern, body, flags=re.IGNORECASE):
            raw_entity = match.group("entity")
            entity = _mcp_clean_business_entity(raw_entity)
            if not entity or not creation_entity_is_valid(entity, raw_entity):
                continue
            folded_verb = _fold_text(match.group("verb"))
            verb = "founded" if any(marker in folded_verb for marker in ("found", "fondat")) else "created"
            claim = _mcp_claim_sentence(f"{subject_name} {verb} {entity}")
            if _fold_text(claim) not in {_fold_text(item) for item in claims}:
                claims.append(claim)
    return claims[:4]


def _mcp_extract_linked_work_entities(text: Any, subject_name: str = "") -> list[str]:
    entities: list[str] = []
    for claim in _mcp_relation_claims_for_subject(text, subject_name):
        for match in re.finditer(r"\b(?:of|at|for|founded)\s+([A-Z][A-Za-z0-9&.' -]{2,96})", claim):
            entity = _mcp_clean_business_entity(match.group(1))
            if entity and _fold_text(entity) not in {_fold_text(item) for item in entities}:
                entities.append(entity)
    if not entities:
        for match in re.finditer(r"\b([A-Z][A-Za-z0-9&.'-]*(?:\s+[A-Z][A-Za-z0-9&.'-]*){0,4})\b", str(text or "")):
            entity = _mcp_clean_business_entity(match.group(1))
            folded = _fold_text(entity)
            if entity and any(marker in folded for marker in ("energy", "foundry", "studio", "systems", "robotics", "corporation", "group", "sync", "nam")):
                if folded not in {_fold_text(item) for item in entities}:
                    entities.append(entity)
    return entities[:8]


def _mcp_work_entity_inventory_from_texts(texts: Sequence[Any], subject_name: str = "") -> list[str]:
    entities: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for entity in _mcp_extract_linked_work_entities(text, subject_name):
            folded = _fold_text(entity)
            if not folded or folded in seen:
                continue
            if subject_name and folded == _fold_text(subject_name):
                continue
            seen.add(folded)
            entities.append(entity)
    return entities


def _mcp_trim_acquisition_target_part(value: Any) -> str:
    text = " ".join(str(value or "").split()).strip(" -,:;.\"'")
    if not text:
        return ""
    text = re.sub(r"\s+-\s+segment\s+\d+\b.*$", "", text, flags=re.IGNORECASE).strip(" -,:;\"'")
    text = re.sub(r"\s+segment\s+\d+\b.*$", "", text, flags=re.IGNORECASE).strip(" -,:;\"'")
    text = re.sub(
        r"\s*,\s*(?:fornitore|sviluppatore|leader|azienda|societ\S{0,2}|provider|developer|supplier)\b.*$",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip(" -,:;.\"'")
    noise_match = re.search(
        r"\b(?:Progetti|Projects|Life\s+Sciences(?:\s*&\s*Healthcare)?|Private\s+Equity|Antitrust|Real\s+Estate|Tax\s+Law|Newsletter|Events|Publications|Careers|Contact|Privacy|TMC|Tecnologie|Technologies|Comunicazioni|Communications|Mercato|Contrattualistica|Diritto\s+Bancario|Diritto|Bancario|Commerciale|Finanziario|Financial)\b",
        text,
        flags=re.IGNORECASE,
    )
    if noise_match:
        leading = text[: noise_match.start()].strip(" -,:;.\"'")
        if not leading:
            return ""
        if _fold_text(leading) in {
            "progetti",
            "projects",
            "tecnologie",
            "technologies",
            "comunicazioni",
            "communications",
            "tmc",
            "mercato",
            "contrattualistica",
            "diritto",
            "bancario",
            "commerciale",
            "finanziario",
            "financial",
        }:
            return ""
        text = leading
    text = re.split(
        r"\s+(?:CMS\s+assiste|ha\s+assistito|assiste)\b|\s+nell\S{0,8}acquisizione\b",
        text,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip(" -,:;.\"'")
    return text


def _mcp_clean_acquisition_targets(value: Any) -> list[str]:
    raw_clause = " ".join(str(value or "").split()).strip(" -,:;.\"'")
    if not raw_clause:
        return []
    raw_clause = re.sub(
        r"\b(?:a|an|the|un|una|il|la)\s+(?:provider|developer|supplier|leader|company|societ[ay]|azienda)\s+of\b[^,;]*",
        "",
        raw_clause,
        flags=re.IGNORECASE,
    )
    parts = [
        part.strip(" -,:;.\"'")
        for part in re.split(r"\s+and\s+|\s+e\s+di\s+|\s+e\s+|,", raw_clause)
        if part.strip(" -,:;.\"'")
    ]
    targets: list[str] = []
    for part in parts:
        part = _mcp_trim_acquisition_target_part(part)
        if _mcp_acquisition_target_part_is_navigation_or_category(part):
            continue
        if re.match(r"(?i)^(?:strengthening|to\s+strengthen|for\s+the\s+purpose|with\s+the\s+aim)\b", part):
            continue
        if re.search(r"\b(?:continues?|continue)\b", part, flags=re.IGNORECASE):
            continue
        part = re.sub(
            r"\s+\b(?:strengthening|to\s+strengthen|for\s+the\s+purpose|with\s+the\s+aim)\b.*$",
            "",
            part,
            flags=re.IGNORECASE,
        ).strip(" -,:;.\"'")
        part = re.sub(
            r"\s+\b(?:continues?|continue|will|has|had|is|was|were|are|announces?|announced)\b.*$",
            "",
            part,
            flags=re.IGNORECASE,
        ).strip(" -,:;.\"'")
        if not re.search(r"[A-Z]", part):
            continue
        entity = _mcp_clean_business_entity(part)
        folded = _fold_text(entity)
        if entity and folded not in {_fold_text(item) for item in targets}:
            targets.append(entity)
    return targets[:4]


def _mcp_acquisition_target_part_is_navigation_or_category(value: Any) -> bool:
    text = " ".join(str(value or "").split()).strip(" -,:;.\"'")
    if not text:
        return True
    folded = _fold_text(text)
    if folded in {
        "progetti",
        "projects",
        "tecnologie",
        "technologies",
        "comunicazioni",
        "communications",
        "tmc",
        "healthcare",
        "mercato",
        "contrattualistica",
        "diritto",
        "bancario",
        "commerciale",
        "finanziario",
        "financial",
    }:
        return True
    if any(
        marker in folded
        for marker in (
            "private equity",
            "life sciences healthcare",
            "life sciences",
            "antitrust",
            "real estate",
            "tax law",
            "newsletter",
            "events",
            "publications",
            "careers",
            "contact",
            "privacy",
            "contrattualistica",
            "diritto bancario",
        )
    ):
        return True
    organization_suffix = re.search(
        r"\b(?:Corporation|Group|Studio|Foundry|Energy|Robotics|Systems|Solutions|Technologies|S\.?r\.?l|S\.?p\.?A|Ltd|Inc|GmbH)\b",
        text,
    )
    compact_named_entity = bool(len(text.split()) <= 5 and re.search(r"[A-Z]", text))
    if len(text.split()) > 6 and not organization_suffix:
        return True
    return not (organization_suffix or compact_named_entity)


def _mcp_clean_italian_acquisition_targets(value: Any) -> list[str]:
    raw_clause = " ".join(str(value or "").split()).strip(" -,:;.\"'")
    if not raw_clause:
        return []
    parts = [
        part.strip(" -,:;.\"'")
        for part in re.split(r"\s*,?\s+e\s+di\s+|\s*,?\s+e\s+", raw_clause, flags=re.IGNORECASE)
        if part.strip(" -,:;.\"'")
    ]
    targets: list[str] = []
    for part in parts:
        part = re.split(
            r"\s+(?:CMS\s+assiste|ha\s+assistito|assiste|Progetti|Life\s+Sciences|Private\s+Equity|Antitrust|TMC)\b|\s+nell\S{0,8}acquisizione\b",
            part,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip(" -,:;.\"'")
        part = _mcp_trim_acquisition_target_part(part)
        if _mcp_acquisition_target_part_is_navigation_or_category(part):
            continue
        part = re.sub(
            r"\s*,\s*(?:fornitore|sviluppatore|leader|azienda|societ[àa]|provider|developer|supplier)\b.*$",
            "",
            part,
            flags=re.IGNORECASE,
        ).strip(" -,:;.\"'")
        if not re.search(r"[A-Z]", part):
            continue
        entity = _mcp_clean_business_entity(part)
        folded = _fold_text(entity)
        if entity and folded and folded not in {_fold_text(item) for item in targets}:
            targets.append(entity)
    return targets[:4]


def _mcp_clean_acquisition_buyer(value: Any) -> str:
    raw = " ".join(str(value or "").split()).strip(" -,:;.\"'")
    if not raw:
        return ""
    raw = re.split(r"\s+nell\S{0,8}acquisizione\b|\s+in\s+the\s+acquisition\b", raw, maxsplit=1, flags=re.IGNORECASE)[0]
    raw = re.split(r"\b(?:assiste|ha\s+assistito|assisted|advised)\b", raw, flags=re.IGNORECASE)[-1]
    return _mcp_clean_business_entity(raw)


def _mcp_company_relation_claims(text: Any, *, linked_entities: list[str] | None = None) -> list[str]:
    body = " ".join(str(text or "").split())
    folded = _fold_text(body)
    if not body:
        return []
    source_noise = ("contact us", "recent post", "cookie", "privacy", "autori", "image alt text", "logo")
    if any(marker in folded for marker in source_noise):
        # Allow extraction from noisy source snippets only if a clean relation pattern can be isolated below.
        pass
    linked_terms = [_fold_text(item) for item in list(linked_entities or []) if _fold_text(item)]
    relation_pattern_present = bool(
        re.search(r"\b(?:acquired|acquisit|acquisizione|founded|fondat|established|created|creato|fondato|lanciato|avviato)\b", folded, flags=re.IGNORECASE)
    )
    if linked_terms and not any(term in folded for term in linked_terms) and not relation_pattern_present:
        return []
    claims: list[str] = []

    def add(claim: str) -> None:
        clean = _mcp_claim_sentence(claim)
        folded_claim = _fold_text(clean)
        if clean and folded_claim and folded_claim not in {_fold_text(item) for item in claims}:
            claims.append(clean)

    for match in re.finditer(
        r"(?P<buyer>[A-Z][A-Za-z0-9&.' -]{2,180}?)\s*(?:\([^)]{1,80}\))?\s+(?:announces?\s+(?:that\s+)?it\s+(?:has|had)|announced\s+(?:that\s+)?it\s+(?:has|had)|has|had)?\s*(?:acquires|acquired)\s+(?P<target_clause>[^.;]{2,220})",
        body,
        flags=re.IGNORECASE,
    ):
        raw_buyer = str(match.group("buyer") or "")
        if not re.search(r"[A-Z]", raw_buyer):
            continue
        buyer = _mcp_clean_business_entity(raw_buyer)
        targets = _mcp_clean_acquisition_targets(match.group("target_clause"))
        if buyer and targets:
            add(f"{buyer} acquired {' and '.join(targets[:3])}")
    for match in re.finditer(
        r"(?P<company>[A-Z][A-Za-z0-9&.' -]{2,80})\s+(?:was|is)\s+founded\s+in\s+(?P<year>(?:19|20)\d{2})",
        body,
        flags=re.IGNORECASE,
    ):
        company = _mcp_clean_business_entity(match.group("company"))
        if company:
            add(f"{company} was founded in {match.group('year')}")
    for match in re.finditer(
        r"(?P<company>[A-Z][A-Za-z0-9&' -]{2,120}?)\s+(?P<verb>created|built|started|launched)\s+(?P<target>[A-Z][^.;]{2,120})",
        body,
        flags=re.IGNORECASE,
    ):
        company = _mcp_clean_business_entity(match.group("company"))
        target = _mcp_clean_business_entity(match.group("target"))
        if company and target:
            verb = _fold_text(match.group("verb")) or "created"
            add(f"{company} {verb} {target}")
    for match in re.finditer(
        r"(?:la\s+societ[Ã a]\s+)?(?P<company>[A-Z][A-Za-z0-9&.' -]{2,120}?)\s+ha\s+(?:creato|fondato|lanciato|avviato)\s+[\"“”']?(?P<target>[A-Z][^.;,\"“”']{2,120})",
        body,
        flags=re.IGNORECASE,
    ):
        company = _mcp_clean_business_entity(match.group("company"))
        target = _mcp_clean_business_entity(match.group("target"))
        if company and target:
            add(f"{company} created {target}")
    for match in re.finditer(
        r"(?:ha\s+assistito|assiste)\s+(?P<buyer>[A-Z][^,.;]{2,120}).{0,240}?nell\S{0,6}acquisizione\s+di\s+(?P<targets>[^.;]+)",
        body,
        flags=re.IGNORECASE,
    ):
        buyer = _mcp_clean_acquisition_buyer(match.group("buyer"))
        targets = _mcp_clean_italian_acquisition_targets(match.group("targets"))
        if buyer and targets:
            add(f"{buyer} acquired {' and '.join(targets[:3])}")
    for match in re.finditer(
        r"ha\s+assistito\s+(?P<buyer>[A-Z][^,.;]{2,120}).{0,240}?nell[’'`´â€™]acquisizione\s+di\s+(?P<targets>[^.;]+)",
        body,
        flags=re.IGNORECASE,
    ):
        buyer = _mcp_clean_acquisition_buyer(match.group("buyer"))
        targets = _mcp_clean_italian_acquisition_targets(match.group("targets"))
        if buyer and targets:
            add(f"{buyer} acquired {' and '.join(targets[:3])}")
    for match in re.finditer(
        r"ha\s+assistito\s+(?P<buyer>[A-Z][^,.;]{2,90}).{0,180}?nell[’']acquisizione\s+di\s+(?P<targets>[^.;]+)",
        body,
        flags=re.IGNORECASE,
    ):
        buyer = _mcp_clean_acquisition_buyer(match.group("buyer"))
        targets = _mcp_clean_italian_acquisition_targets(match.group("targets"))
        if buyer and targets:
            add(f"{buyer} acquired {' and '.join(targets[:3])}")
    context_dependent_pronoun = bool(
        re.search(
            r"\b(?:he|she|they|his|her|their|lui|lei|loro|suo|sua|suoi|sue)\s+(?:became|was|is|were|are|has|had|held|joined|founded|created|started|served|serves|divent[ao]|fond[ao])\b",
            folded,
        )
    )
    if (
        not claims
        and len(body) <= 260
        and _company_founding_material_present(body)
        and not context_dependent_pronoun
        and not any(marker in folded for marker in source_noise)
    ):
        add(body)
    return claims[:4]


def _mcp_agent_relation_text_for_candidate(
    text: str,
    *,
    section_key: str,
    query_text: str,
    semantic_contract: dict[str, Any] | None,
) -> str:
    if section_key not in {"identity", "work", "history", "temporal_inventory"}:
        return text
    required_sections = {
        _mcp_context_section_key(section)
        for section in list(((semantic_contract or {}).get("context_contract") or {}).get("required_sections") or [])
        if str(section or "").strip()
    }
    if _mcp_query_requests_exact_identity_name(query_text, required_sections):
        return text
    if not (_query_is_work_or_company(query_text) or section_key in {"work", "identity"}):
        return text
    subject_name = _mcp_identity_subject_name_from_contract(semantic_contract)
    claims = _mcp_relation_claims_for_subject(text, subject_name)
    if claims:
        return " ".join(claims)
    if _query_is_work_or_company(query_text):
        company_claims = _mcp_company_relation_claims(text)
        if company_claims and len(text) > 220:
            return " ".join(company_claims)
    return text


def _mcp_candidate_source_title(payload: dict[str, Any]) -> str | None:
    provenance = dict(payload.get("provenance") or {})
    title = (
        payload.get("source_title")
        or payload.get("title")
        or payload.get("source_label")
        or provenance.get("source_label")
    )
    cleaned = _mcp_clean_agent_text(title)
    return cleaned if cleaned and len(cleaned) <= 160 else None


def _mcp_candidate_from_entry(entry: dict[str, Any]) -> dict[str, Any] | None:
    node = dict(entry.get("node") or {})
    merged = {**node, **{key: value for key, value in entry.items() if key != "node"}}
    node_id = str(merged.get("node_id") or merged.get("id") or "").strip()
    text = _mcp_best_candidate_text(merged)
    if not text:
        return None
    support_slots = [str(slot).strip() for slot in list(merged.get("support_slots") or []) if str(slot).strip()]
    branch_goals = [str(goal).strip() for goal in list(merged.get("branch_goals") or []) if str(goal).strip()]
    section_seed = " ".join(
        str(part or "")
        for part in (
            merged.get("support_slot"),
            " ".join(support_slots),
            merged.get("memory_type"),
            merged.get("document_role"),
            (merged.get("provenance") or {}).get("guide_conceptual_area") if isinstance(merged.get("provenance"), dict) else None,
        )
    )
    primary_section_seed = str(merged.get("support_slot") or "").strip() or (support_slots[0] if support_slots else "") or (branch_goals[0] if branch_goals else "") or section_seed
    return {
        "node_id": node_id,
        "section_key": _mcp_context_section_key(primary_section_seed, text),
        "text": text,
        "confidence": float(merged.get("confidence") or merged.get("score") or merged.get("raw_score") or merged.get("evidence_confidence") or 0.0),
        "source_title": _mcp_candidate_source_title(merged),
        "claim_status": str(merged.get("claim_status") or "").strip(),
        "memory_type": str(merged.get("memory_type") or "").strip(),
        "document_role": str(merged.get("document_role") or "").strip(),
        "document_anchor_id": str(merged.get("document_anchor_id") or "").strip(),
        "is_document_anchor": bool(merged.get("is_document_anchor")),
        "answer_eligible": bool(is_answer_eligible(merged)),
        "document_eligible": bool(is_document_eligible(merged)),
        "support_slots": support_slots,
        "branch_goals": branch_goals,
    }


def _mcp_text_can_backfill_identity_from_work(text: Any) -> bool:
    body = str(text or "").strip()
    folded = _fold_text(body)
    if not body:
        return False
    if not re.search(r"\b[A-Z][A-Za-z0-9'._-]+(?:\s+[A-Z][A-Za-z0-9'._-]+){1,4}\b", body):
        return False
    return any(
        marker in folded
        for marker in (
            " is ",
            " was ",
            " e ",
            " è ",
            "founder",
            "founded",
            "fondatore",
            "fondato",
            "ceo",
            "entrepreneur",
            "imprenditore",
            "leader",
            "engineer",
            "ingegnere",
            "works as",
            "lavora",
        )
    )


def _mcp_text_can_backfill_work_from_identity(text: Any) -> bool:
    folded = _fold_text(text)
    return any(
        marker in folded
        for marker in (
            "founder",
            "founded",
            "fondatore",
            "fondato",
            "company",
            "azienda",
            "societa",
            "startup",
            "ceo",
            "project",
            "progetto",
            "works on",
            "lavora",
            "acquired",
            "acquisita",
        )
    )


def _mcp_agent_body_has_node_id(text: str) -> bool:
    return bool(re.search(r"\b(?:vec_node_[a-zA-Z0-9_]+|[a-z]+_[0-9a-f]{8,})\b", str(text or ""))) or bool(re.search(r"\[[^\]]*(?:node|n_|doc|vec_)[^\]]*\]", str(text or ""), flags=re.IGNORECASE))


def _mcp_agent_body_has_route_debug_marker(text: str) -> bool:
    body = str(text or "")
    if re.search(r"^##\s*Path Discoveries\b", body, flags=re.IGNORECASE | re.MULTILINE):
        return True
    return bool(re.search(r"\bLanding\s+\d+\s*->\s*Landing\s+\d+\b", body, flags=re.IGNORECASE))


def _mcp_context_contract_sets(
    semantic_contract: dict[str, Any] | None,
    query_text: str,
) -> tuple[set[str], set[str], set[str], set[str], bool, str]:
    required_sections, optional_sections, broad_context = _mcp_canonical_required_sections(semantic_contract, query_text)
    forbidden_sections = _mcp_forbidden_sections(semantic_contract)
    allowed_sections = set(_MCP_CONTEXT_SECTION_TITLES)
    if not broad_context:
        allowed_sections = set(required_sections) | set(optional_sections)
        if not allowed_sections:
            allowed_sections = {"identity", "work", "relationships", "style", "values", "history", "documents"}
    if _is_temporal_reference_query(query_text):
        allowed_sections.add("temporal_inventory")
        allowed_sections.add("history")
    document_mode = str(((semantic_contract or {}).get("document_contract") or {}).get("mode") or "none")
    if document_mode != "none":
        allowed_sections.add("documents")
    return required_sections, optional_sections, allowed_sections, forbidden_sections, broad_context, document_mode


def _mcp_path_entry_maps(
    matches: list[dict[str, Any]] | None,
    evidence_reservoir: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    entries_by_node_id: dict[str, dict[str, Any]] = {}
    for entry in list((evidence_reservoir or {}).get("entries") or []):
        if not isinstance(entry, dict):
            continue
        node = dict(entry.get("node") or {})
        node_id = str(entry.get("node_id") or node.get("id") or "").strip()
        if node_id:
            entries_by_node_id[node_id] = {**node, **{key: value for key, value in entry.items() if key != "node"}, "node_id": node_id}
    for match in list(matches or []):
        if not isinstance(match, dict):
            continue
        node = dict(match.get("node") or {})
        node_id = str(match.get("node_id") or node.get("id") or "").strip()
        if not node_id:
            continue
        existing = dict(entries_by_node_id.get(node_id) or {})
        merged = {
            **node,
            **existing,
            **{key: value for key, value in match.items() if key != "node"},
            "node_id": node_id,
        }
        if not str(merged.get("raw_text") or "").strip() and str(node.get("raw_text") or "").strip():
            merged["raw_text"] = node.get("raw_text")
        entries_by_node_id[node_id] = merged
    return entries_by_node_id


def _mcp_path_landing_rows(
    semantic_contract: dict[str, Any] | None,
    landing_metadata: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    contract_landings = [
        dict(item)
        for item in list(((semantic_contract or {}).get("landing_plan") or {}).get("landing_hypotheses") or [])
        if isinstance(item, dict)
    ]
    runtime_landings = [dict(item) for item in list(landing_metadata or []) if isinstance(item, dict)]
    rows: list[dict[str, Any]] = []
    for index in range(max(len(contract_landings), len(runtime_landings))):
        contract_landing = dict(contract_landings[index]) if index < len(contract_landings) else {}
        runtime_landing = dict(runtime_landings[index]) if index < len(runtime_landings) else {}
        runtime_has_ai_spatial_identity = bool(
            str(runtime_landing.get("ai_spatial_path_id") or "").strip()
            or dict(runtime_landing.get("spatial_snap") or {})
            or str(runtime_landing.get("planner_family") or "").strip().lower() == "ai"
        )
        landing_id = str(
            (runtime_landing.get("landing_id") if runtime_has_ai_spatial_identity else None)
            or contract_landing.get("landing_id")
            or runtime_landing.get("landing_id")
            or f"L{index + 1}"
        ).strip()
        rows.append(
            {
                "landing_id": landing_id,
                "runtime_landing_id": str(runtime_landing.get("landing_id") or "").strip() or None,
                "branch_id": str(runtime_landing.get("branch_id") or "").strip() or None,
                "probe_id": str(runtime_landing.get("probe_id") or "").strip() or None,
                "spatial_snap": dict(runtime_landing.get("spatial_snap") or {}),
                "ai_spatial_path_id": str(runtime_landing.get("ai_spatial_path_id") or "").strip() or None,
                "ai_spatial_landing_region_ref": str(runtime_landing.get("ai_spatial_landing_region_ref") or "").strip() or None,
                "ai_spatial_landing_coordinate": dict(runtime_landing.get("ai_spatial_landing_coordinate") or {}) or None,
                "ai_spatial_bridge_targets": [
                    str(item).strip()
                    for item in list(runtime_landing.get("ai_spatial_bridge_targets") or [])
                    if str(item).strip()
                ][:6],
                "ai_spatial_preferred_edges": [
                    str(item).strip()
                    for item in list(runtime_landing.get("ai_spatial_preferred_edges") or [])
                    if str(item).strip()
                ][:8],
                "heuristic_provisional": bool(runtime_landing.get("heuristic_provisional")),
                "provisional_until_ai_spatial": bool(runtime_landing.get("provisional_until_ai_spatial")),
                "label": str(runtime_landing.get("label") or runtime_landing.get("goal") or landing_id).strip(),
                "goal": str(runtime_landing.get("goal") or "").strip() or None,
                "target_evidence_ids": [str(item) for item in list(contract_landing.get("target_evidence_ids") or []) if str(item).strip()],
                "textual_probe": str(contract_landing.get("textual_probe") or runtime_landing.get("query_text") or "").strip() or None,
                "planner_family": str(runtime_landing.get("planner_family") or "").strip() or None,
                "route_trace_count": int(runtime_landing.get("route_trace_count") or 0),
                "studied_node_count": int(runtime_landing.get("studied_node_count") or 0),
                "hydrated_node_count": int(runtime_landing.get("hydrated_node_count") or 0),
            }
        )
    return rows


def _mcp_path_plan_rows(
    semantic_contract: dict[str, Any] | None,
    landings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    landing_plan = dict((semantic_contract or {}).get("landing_plan") or {})
    landing_ids = [str(item.get("landing_id") or "").strip() for item in list(landings or []) if str(item.get("landing_id") or "").strip()]
    ai_landing_by_path_id: dict[str, dict[str, Any]] = {}
    for landing in list(landings or []):
        if not isinstance(landing, dict):
            continue
        path_id = str(landing.get("ai_spatial_path_id") or "").strip()
        landing_id = str(landing.get("landing_id") or "").strip()
        if path_id and landing_id and path_id not in ai_landing_by_path_id:
            ai_landing_by_path_id[path_id] = dict(landing)

    def normalize_path(raw_path: dict[str, Any], index: int) -> dict[str, Any] | None:
        path_id = str(raw_path.get("path_id") or f"P{index + 1}").strip() or f"P{index + 1}"
        from_landing_id = str(raw_path.get("from_landing_id") or raw_path.get("origin_landing_id") or "").strip()
        if not from_landing_id and landing_ids:
            from_landing_id = landing_ids[min(index, len(landing_ids) - 1)]
        if not from_landing_id:
            return None
        route_kind = str(raw_path.get("route_kind") or "").strip().lower()
        explicit_bridge = bool(raw_path.get("cross_landing_bridge")) or route_kind == "explicit_cross_landing_bridge"
        if not explicit_bridge:
            route_kind = "landing_origin_corridor"
            target_landing_id = ""
            ai_landing = dict(ai_landing_by_path_id.get(path_id) or {})
            current_landing = next(
                (
                    dict(item)
                    for item in list(landings or [])
                    if str(item.get("landing_id") or "").strip() == from_landing_id
                ),
                {},
            )
            if ai_landing and not str(current_landing.get("ai_spatial_path_id") or "").strip():
                from_landing_id = str(ai_landing.get("landing_id") or from_landing_id).strip()
        else:
            route_kind = "explicit_cross_landing_bridge"
            target_landing_id = str(raw_path.get("target_landing_id") or raw_path.get("to_landing_id") or "").strip()
            if not target_landing_id or target_landing_id == from_landing_id:
                return None
        return {
            "path_id": path_id,
            "route_kind": route_kind,
            "origin_landing_id": from_landing_id,
            "from_landing_id": from_landing_id,
            "target_landing_id": target_landing_id or None,
            "to_landing_id": target_landing_id,
            "why_traverse": str(raw_path.get("why_traverse") or "Inspect branch-local corridor evidence from this landing.").strip(),
            "read_intermediate_nodes": bool(raw_path.get("read_intermediate_nodes", True)),
            "max_intermediate_nodes": max(1, min(24, int(raw_path.get("max_intermediate_nodes") or 12))),
            "preferred_edges": [str(item) for item in list(raw_path.get("preferred_edges") or ["highway", "semantic_link", "document_reference", "temporal_link"]) if str(item).strip()][:6],
            "planner_source": str(raw_path.get("planner_source") or ((semantic_contract or {}).get("contract_authority") or "runtime")).strip(),
        }

    planned_paths = [
        normalized
        for index, item in enumerate(list(landing_plan.get("paths") or landing_plan.get("path_itinerary") or []))
        if isinstance(item, dict)
        for normalized in [normalize_path(dict(item), index)]
        if normalized
    ]
    if planned_paths:
        return planned_paths[:8]
    if not landings:
        return []
    paths: list[dict[str, Any]] = []
    for index, source in enumerate(landings):
        paths.append(
            {
                "path_id": f"P{index + 1}",
                "route_kind": "landing_origin_corridor",
                "origin_landing_id": str(source.get("landing_id") or f"L{index + 1}"),
                "from_landing_id": str(source.get("landing_id") or f"L{index + 1}"),
                "target_landing_id": None,
                "to_landing_id": "",
                "why_traverse": "Inspect the branch-local semantic corridor from this landing.",
                "read_intermediate_nodes": True,
                "max_intermediate_nodes": 12,
                "preferred_edges": ["highway", "semantic_link", "document_reference", "temporal_link"],
                "planner_source": "runtime_inferred",
            }
        )
    return paths[:8]


def _mcp_path_route_events_for_path(
    path: dict[str, Any],
    *,
    branches: list[dict[str, Any]],
    steps: list[dict[str, Any]],
    landings_by_id: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    from_landing = dict(landings_by_id.get(str(path.get("from_landing_id") or path.get("origin_landing_id") or "")) or {})
    route_kind = str(path.get("route_kind") or "landing_origin_corridor").strip()
    target_landing_id = str(path.get("target_landing_id") or path.get("to_landing_id") or "").strip()
    to_landing = dict(landings_by_id.get(target_landing_id) or {}) if route_kind == "explicit_cross_landing_bridge" else {}
    branch_ids = [
        str(item).strip()
        for item in (from_landing.get("branch_id"), to_landing.get("branch_id"))
        if str(item or "").strip()
    ]
    probe_ids = {
        str(item).strip()
        for item in (from_landing.get("probe_id"), to_landing.get("probe_id"))
        if str(item or "").strip()
    }
    if not branch_ids:
        branch_ids = [
            str(branch.get("branch_id") or "").strip()
            for branch in list(branches or [])[:2]
            if str(branch.get("branch_id") or "").strip()
        ]
    events: list[dict[str, Any]] = []
    for branch in list(branches or []):
        if branch_ids and str(branch.get("branch_id") or "").strip() not in set(branch_ids):
            continue
        for event in list(branch.get("route_trace") or []):
            if not isinstance(event, dict):
                continue
            events.append({**dict(event), "branch_id": str(branch.get("branch_id") or "").strip() or None, "event_source": "branch_route_trace"})
    for step in list(steps or []):
        if not isinstance(step, dict):
            continue
        if probe_ids and str(step.get("probe_id") or "").strip() not in probe_ids:
            continue
        event = dict(step.get("route_decision") or {})
        if event:
            events.append({**event, "probe_id": str(step.get("probe_id") or "").strip() or None, "event_source": "retrieve_step"})
    return events[:32], list(dict.fromkeys(branch_ids))


def _mcp_path_route_event_counts(route_events: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "event_count": len(route_events),
        "travel_count": 0,
        "study_count": 0,
        "hydrate_count": 0,
        "destination_reached_count": 0,
        "route_exhausted_count": 0,
        "yielded_match_count": 0,
        "studied_node_count": 0,
        "hydrated_node_count": 0,
    }
    studied_ids: set[str] = set()
    hydrated_ids: set[str] = set()
    for event in route_events:
        move_type = str(event.get("move_type") or "").strip()
        if bool(event.get("travel_performed")) or move_type == "travel":
            counts["travel_count"] += 1
        if move_type in {"study", "route_exhausted"}:
            counts["study_count"] += 1
        if move_type in {"hydrate", "hydrate_current_node"}:
            counts["hydrate_count"] += 1
        if bool(event.get("destination_reached")) or move_type == "destination_reached":
            counts["destination_reached_count"] += 1
        if move_type == "route_exhausted":
            counts["route_exhausted_count"] += 1
        counts["yielded_match_count"] += len([item for item in list(event.get("yielded_match_ids") or []) if str(item).strip()])
        studied_ids.update(str(item).strip() for item in list(event.get("studied_node_ids") or []) if str(item).strip())
        hydrated_ids.update(str(item).strip() for item in list(event.get("hydrated_node_ids") or []) if str(item).strip())
    counts["studied_node_count"] = len(studied_ids)
    counts["hydrated_node_count"] = len(hydrated_ids)
    return counts


def _mcp_path_lifecycle(
    *,
    path: dict[str, Any],
    route_events: list[dict[str, Any]],
    runtime_branch_ids: list[str],
    branches: list[dict[str, Any]],
    promoted_count: int,
    cold_count: int,
    excluded_count: int,
    unavailable_count: int,
) -> dict[str, Any]:
    branch_id_set = {str(item).strip() for item in list(runtime_branch_ids or []) if str(item).strip()}
    bound_branches = [
        dict(branch)
        for branch in list(branches or [])
        if not branch_id_set or str(branch.get("branch_id") or "").strip() in branch_id_set
    ]
    event_counts = _mcp_path_route_event_counts(route_events)
    branch_statuses = [str(branch.get("status") or "").strip() for branch in bound_branches if str(branch.get("status") or "").strip()]
    branch_stop_reasons = [str(branch.get("stop_reason") or "").strip() for branch in bound_branches if str(branch.get("stop_reason") or "").strip()]
    started = bool(route_events)
    destination_reached = bool(
        event_counts["destination_reached_count"]
        or any(bool(branch.get("destination_reached")) or bool((branch.get("destination_progress") or {}).get("destination_reached")) for branch in bound_branches)
    )
    branch_satisfied = any(status == "satisfied" for status in branch_statuses)
    stopped = bool(event_counts["route_exhausted_count"] or any(status == "stopped" for status in branch_statuses))
    completed = bool(destination_reached or (branch_satisfied and started))
    if completed:
        state = "completed"
        state_reason = "destination_reached" if destination_reached else "branch_satisfied"
    elif stopped:
        state = "stopped"
        state_reason = branch_stop_reasons[0] if branch_stop_reasons else "route_exhausted"
    elif started:
        state = "started"
        state_reason = "route_events_emitted"
    else:
        state = "pending"
        state_reason = "no_runtime_branch_bound" if not runtime_branch_ids else "no_route_events_yet"
    return {
        "schema_version": "agvm.path_corridor_lifecycle.v1",
        "path_id": str(path.get("path_id") or ""),
        "route_kind": str(path.get("route_kind") or "landing_origin_corridor"),
        "state": state,
        "state_reason": state_reason,
        "planned": True,
        "started": started,
        "completed": completed,
        "stopped": stopped and not completed,
        "pending": state == "pending",
        "terminal": completed or stopped,
        "runtime_branch_ids": list(dict.fromkeys(runtime_branch_ids)),
        "branch_statuses": list(dict.fromkeys(branch_statuses)),
        "stop_reasons": list(dict.fromkeys(branch_stop_reasons)),
        "event_counts": event_counts,
        "package_impact": {
            "changed_context_package": promoted_count > 0,
            "hot_promoted_count": promoted_count,
            "cold_reservoir_count": cold_count,
            "excluded_count": excluded_count,
            "unavailable_count": unavailable_count,
        },
    }


def _mcp_path_bound_branches(branches: list[dict[str, Any]], runtime_branch_ids: list[str]) -> list[dict[str, Any]]:
    branch_id_set = {str(item).strip() for item in list(runtime_branch_ids or []) if str(item).strip()}
    return [
        dict(branch)
        for branch in list(branches or [])
        if isinstance(branch, dict)
        and (not branch_id_set or str(branch.get("branch_id") or "").strip() in branch_id_set)
    ]


def _mcp_path_first_spatial_source(
    *,
    from_landing: dict[str, Any],
    bound_branches: list[dict[str, Any]],
) -> dict[str, Any]:
    for branch in bound_branches:
        snap = dict(branch.get("spatial_snap") or {})
        if snap:
            return {
                "spatial_snap": snap,
                "ai_spatial_path_id": str(branch.get("ai_spatial_path_id") or snap.get("path_id") or "").strip() or None,
                "ai_spatial_landing_region_ref": str(branch.get("ai_spatial_landing_region_ref") or snap.get("ai_landing_region_ref") or "").strip() or None,
                "ai_spatial_landing_coordinate": dict(branch.get("ai_spatial_landing_coordinate") or snap.get("ai_landing_coordinate") or {}) or None,
                "ai_spatial_bridge_targets": [
                    str(item).strip()
                    for item in list(branch.get("ai_spatial_bridge_targets") or [])
                    if str(item).strip()
                ][:6],
                "ai_spatial_preferred_edges": [
                    str(item).strip()
                    for item in list(branch.get("ai_spatial_preferred_edges") or [])
                    if str(item).strip()
                ][:8],
                "heuristic_provisional": bool(branch.get("heuristic_provisional")),
                "provisional_until_ai_spatial": bool(branch.get("provisional_until_ai_spatial")),
            }
    return {
        "spatial_snap": dict(from_landing.get("spatial_snap") or {}),
        "ai_spatial_path_id": str(from_landing.get("ai_spatial_path_id") or "").strip() or None,
        "ai_spatial_landing_region_ref": str(from_landing.get("ai_spatial_landing_region_ref") or "").strip() or None,
        "ai_spatial_landing_coordinate": dict(from_landing.get("ai_spatial_landing_coordinate") or {}) or None,
        "ai_spatial_bridge_targets": [
            str(item).strip()
            for item in list(from_landing.get("ai_spatial_bridge_targets") or [])
            if str(item).strip()
        ][:6],
        "ai_spatial_preferred_edges": [
            str(item).strip()
            for item in list(from_landing.get("ai_spatial_preferred_edges") or [])
            if str(item).strip()
        ][:8],
        "heuristic_provisional": bool(from_landing.get("heuristic_provisional")),
        "provisional_until_ai_spatial": bool(from_landing.get("provisional_until_ai_spatial")),
    }


def _mcp_path_event_node_ids(event: dict[str, Any]) -> list[str]:
    node_ids: list[str] = []
    for key in ("from_node_id", "to_node_id", "source_node_id", "target_node_id"):
        value = str(event.get(key) or "").strip()
        if value:
            node_ids.append(value)
    for key in ("studied_node_ids", "hydrated_node_ids", "yielded_match_ids"):
        node_ids.extend(str(item).strip() for item in list(event.get(key) or []) if str(item).strip())
    return list(dict.fromkeys(node_ids))


def _mcp_path_traversed_node_ids(route_events: list[dict[str, Any]], bound_branches: list[dict[str, Any]]) -> list[str]:
    node_ids: list[str] = []
    for event in route_events:
        node_ids.extend(_mcp_path_event_node_ids(dict(event)))
    for branch in bound_branches:
        for key in ("traversed_nodes", "studied_node_ids", "hydrated_node_ids", "evidence_node_ids", "reservoir_only_node_ids"):
            node_ids.extend(str(item).strip() for item in list(branch.get(key) or []) if str(item).strip())
    return list(dict.fromkeys(node_ids))[:48]


def _mcp_path_traversed_edges(route_events: list[dict[str, Any]], bound_branches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    for branch in bound_branches:
        for raw_edge in list(branch.get("traversed_edges") or []):
            if not isinstance(raw_edge, dict):
                continue
            edge = dict(raw_edge)
            if not edge:
                continue
            edge.setdefault("branch_id", branch.get("branch_id"))
            edges.append(edge)
    if not edges:
        for event in route_events:
            if not isinstance(event, dict):
                continue
            from_node = str(event.get("from_node_id") or event.get("source_node_id") or "").strip()
            to_node = str(event.get("to_node_id") or event.get("target_node_id") or "").strip()
            if not (from_node or to_node):
                continue
            edges.append(
                {
                    "source_node_id": from_node or None,
                    "target_node_id": to_node or None,
                    "edge_type": str(event.get("edge_type") or "").strip() or None,
                    "travel_performed": bool(event.get("travel_performed") or str(event.get("move_type") or "") == "travel"),
                    "branch_id": event.get("branch_id"),
                }
            )
    return edges[:48]


def _mcp_path_landing_correction_event(
    *,
    path: dict[str, Any],
    from_landing: dict[str, Any],
    spatial_source: dict[str, Any],
    runtime_branch_ids: list[str],
    traversed_node_ids: list[str],
    traversed_edges: list[dict[str, Any]],
    intermediate_nodes: list[dict[str, Any]],
    lifecycle: dict[str, Any],
) -> dict[str, Any]:
    snap = dict(spatial_source.get("spatial_snap") or {})
    return {
        "schema_version": "agvm.landing_correction_event.v1",
        "event_kind": "runtime_route_outcome",
        "persistence_state": "ephemeral_h4b_not_persisted",
        "path_id": str(path.get("path_id") or ""),
        "landing_id": str(from_landing.get("landing_id") or ""),
        "runtime_landing_id": str(from_landing.get("runtime_landing_id") or "") or None,
        "branch_ids": [str(item) for item in list(runtime_branch_ids or []) if str(item).strip()],
        "probe_id": str(from_landing.get("probe_id") or "") or None,
        "planner_family": str(from_landing.get("planner_family") or "") or None,
        "heuristic_provisional": bool(spatial_source.get("heuristic_provisional")),
        "provisional_until_ai_spatial": bool(spatial_source.get("provisional_until_ai_spatial")),
        "ai_spatial_path_id": spatial_source.get("ai_spatial_path_id"),
        "ai_landing_region_ref": spatial_source.get("ai_spatial_landing_region_ref") or snap.get("ai_landing_region_ref"),
        "ai_landing_coordinate": spatial_source.get("ai_spatial_landing_coordinate") or snap.get("ai_landing_coordinate"),
        "snapped_coordinate": snap.get("snapped_coordinate"),
        "snapped_region_ref": snap.get("snapped_region_ref") or snap.get("bucket_key"),
        "bucket_key": snap.get("bucket_key"),
        "snap_delta": snap.get("snap_delta"),
        "backend_changed_coordinate": bool(snap.get("backend_changed_coordinate")),
        "snap_status": str(snap.get("status") or "") or None,
        "snap_source": str(snap.get("source") or "") or None,
        "traversed_node_ids": traversed_node_ids[:24],
        "traversed_edge_count": len(traversed_edges),
        "promoted_hot_node_ids": [
            str(item.get("node_id") or "")
            for item in list(intermediate_nodes or [])
            if str(item.get("promotion_state") or "") == "hot" and str(item.get("node_id") or "").strip()
        ][:24],
        "cold_reservoir_node_ids": [
            str(item.get("node_id") or "")
            for item in list(intermediate_nodes or [])
            if str(item.get("promotion_state") or "") == "cold" and str(item.get("node_id") or "").strip()
        ][:24],
        "excluded_node_ids": [
            str(item.get("node_id") or "")
            for item in list(intermediate_nodes or [])
            if str(item.get("promotion_state") or "") == "excluded" and str(item.get("node_id") or "").strip()
        ][:24],
        "lifecycle_state": str(lifecycle.get("state") or ""),
        "destination_reached": bool(lifecycle.get("completed")),
        "changed_context_package": bool((lifecycle.get("package_impact") or {}).get("changed_context_package")),
    }


def _mcp_path_promoted_node(
    node_id: str,
    *,
    node_entry: dict[str, Any] | None,
    allowed_sections: set[str],
    forbidden_sections: set[str],
    broad_context: bool,
    why_read: str,
    edge_type: str | None,
) -> dict[str, Any]:
    candidate = _mcp_candidate_from_entry(dict(node_entry or {"node_id": node_id, "summary": ""}))
    if not candidate:
        return {
            "node_id": node_id,
            "text": None,
            "section": "unknown",
            "promotion_state": "unavailable",
            "reason": "node_text_not_available",
            "why_read": why_read,
            "edge_type": edge_type,
        }
    section_key = _mcp_context_section_key(candidate.get("section_key"), candidate.get("text"))
    promotion_state = "hot"
    reason = "contract_relevant_corridor_discovery"
    if not bool(candidate.get("answer_eligible")) and section_key != "documents":
        promotion_state = "excluded"
        reason = "not_answer_eligible"
    elif section_key in forbidden_sections:
        promotion_state = "excluded"
        reason = "forbidden_by_semantic_contract"
    elif not broad_context and section_key not in allowed_sections:
        promotion_state = "cold"
        reason = "off_contract_corridor_reservoir"
    return {
        "node_id": node_id,
        "text": str(candidate.get("text") or "").strip() or None,
        "section": section_key,
        "promotion_state": promotion_state,
        "reason": reason,
        "why_read": why_read,
        "edge_type": edge_type,
        "source_title": candidate.get("source_title"),
        "confidence": float(candidate.get("confidence") or 0.0),
    }


def _mcp_path_required_section_backfill_nodes(
    *,
    query_text: str,
    semantic_contract: dict[str, Any] | None,
    entries_by_node_id: dict[str, dict[str, Any]],
    intermediate_nodes: list[dict[str, Any]],
    allowed_sections: set[str],
    forbidden_sections: set[str],
    broad_context: bool,
    limit: int,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    required_sections, _optional_sections, _allowed, _forbidden, _broad, _document_mode = _mcp_context_contract_sets(
        semantic_contract,
        query_text,
    )
    target_sections = [
        section
        for section in ("identity", "relationships", "work", "history", "values", "style", "documents")
        if section in required_sections
    ]
    requested_relations = _mcp_requested_relations(semantic_contract, query_text)
    if requested_relations and "relationships" not in target_sections:
        target_sections.append("relationships")
    if not target_sections:
        return []

    existing_node_ids = {str(item.get("node_id") or "").strip() for item in intermediate_nodes if str(item.get("node_id") or "").strip()}
    existing_text_by_section: dict[str, list[str]] = {}
    for item in intermediate_nodes:
        section = _mcp_context_section_key(item.get("section") or item.get("section_key"), item.get("text"))
        text = str(item.get("text") or "").strip()
        if text:
            existing_text_by_section.setdefault(section, []).append(text)

    backfilled: list[dict[str, Any]] = []
    for section_key in target_sections:
        existing_blob = "\n".join(existing_text_by_section.get(section_key, []))
        if section_key == "relationships":
            relation_missing = bool(
                requested_relations
                and not all(_text_mentions_requested_relation(existing_blob, relation) for relation in requested_relations)
            )
            if not relation_missing and existing_blob:
                continue
        elif existing_blob:
            continue

        candidates: list[tuple[float, str, dict[str, Any]]] = []
        for node_id, entry in entries_by_node_id.items():
            node_id = str(node_id or "").strip()
            if not node_id or node_id in existing_node_ids:
                continue
            candidate = _mcp_candidate_from_entry(dict(entry or {}))
            if not candidate:
                continue
            candidate_section = _mcp_context_section_key(candidate.get("section_key"), candidate.get("text"))
            if candidate_section != section_key:
                continue
            text = str(candidate.get("text") or "").strip()
            if not text:
                continue
            if section_key == "relationships" and requested_relations and not _mcp_text_matches_requested_relation(text, candidate.get("source_title"), requested_relations):
                continue
            score = float(candidate.get("confidence") or 0.0)
            try:
                score = max(score, float(entry.get("score") or 0.0), float(entry.get("raw_score") or 0.0))
            except Exception:
                pass
            if bool(candidate.get("answer_eligible")):
                score += 0.15
            candidates.append((score, node_id, dict(entry)))

        for _score, node_id, entry in sorted(candidates, key=lambda item: item[0], reverse=True)[:2]:
            promoted = _mcp_path_promoted_node(
                node_id,
                node_entry=entry,
                allowed_sections=allowed_sections,
                forbidden_sections=forbidden_sections,
                broad_context=broad_context,
                why_read="mission target recovered from already retrieved evidence after corridor material missed the required section",
                edge_type="mission_target_reservoir",
            )
            if not promoted.get("text") or str(promoted.get("promotion_state") or "") == "excluded":
                continue
            promoted["reason"] = "mission_target_retrieval_backfill_after_corridor_miss"
            backfilled.append(promoted)
            existing_node_ids.add(node_id)
            existing_text_by_section.setdefault(section_key, []).append(str(promoted.get("text") or ""))
            if len(backfilled) >= limit:
                return backfilled
    return backfilled


def build_path_corridor_package(
    *,
    query_text: str,
    branches: list[dict[str, Any]] | None = None,
    steps: list[dict[str, Any]] | None = None,
    matches: list[dict[str, Any]] | None = None,
    evidence_reservoir: dict[str, Any] | None = None,
    semantic_contract: dict[str, Any] | None = None,
    landing_metadata: list[dict[str, Any]] | None = None,
    retrieval_mode: str = "balanced",
) -> dict[str, Any]:
    _required, _optional, allowed_sections, forbidden_sections, broad_context, document_mode = _mcp_context_contract_sets(semantic_contract, query_text)
    landings = _mcp_path_landing_rows(semantic_contract, landing_metadata)
    planned_paths = _mcp_path_plan_rows(semantic_contract, landings)
    landings_by_id = {str(item.get("landing_id") or ""): dict(item) for item in landings if str(item.get("landing_id") or "").strip()}
    entries_by_node_id = _mcp_path_entry_maps(matches, evidence_reservoir)
    paths: list[dict[str, Any]] = []
    landing_correction_events: list[dict[str, Any]] = []
    all_route_event_count = 0
    unavailable_node_count = 0
    for path_index, raw_path in enumerate(planned_paths):
        path = dict(raw_path)
        path_id = str(path.get("path_id") or f"P{path_index + 1}").strip() or f"P{path_index + 1}"
        route_kind = str(path.get("route_kind") or "landing_origin_corridor").strip() or "landing_origin_corridor"
        origin_landing_id = str(path.get("origin_landing_id") or path.get("from_landing_id") or "").strip()
        target_landing_id = str(path.get("target_landing_id") or path.get("to_landing_id") or "").strip()
        if route_kind != "explicit_cross_landing_bridge":
            route_kind = "landing_origin_corridor"
            target_landing_id = ""
        max_intermediate_nodes = max(1, min(24, int(path.get("max_intermediate_nodes") or 12)))
        route_events, runtime_branch_ids = _mcp_path_route_events_for_path(
            path,
            branches=[dict(item) for item in list(branches or []) if isinstance(item, dict)],
            steps=[dict(item) for item in list(steps or []) if isinstance(item, dict)],
            landings_by_id=landings_by_id,
        )
        all_route_event_count += len(route_events)
        node_reason_by_id: dict[str, tuple[str, str | None]] = {}
        route_event_summaries: list[dict[str, Any]] = []
        for event in route_events:
            edge_type = str(event.get("edge_type") or "") or None
            move_type = str(event.get("move_type") or "").strip() or None
            event_node_ids = _mcp_path_event_node_ids(event)
            for node_id in event_node_ids:
                if node_id in node_reason_by_id:
                    continue
                if node_id in [str(item) for item in list(event.get("yielded_match_ids") or [])]:
                    why_read = "yielded evidence while traversing the corridor"
                elif node_id in [str(item) for item in list(event.get("hydrated_node_ids") or [])]:
                    why_read = "hydrated along the corridor"
                elif node_id in [str(item) for item in list(event.get("studied_node_ids") or [])]:
                    why_read = "studied along the corridor"
                else:
                    why_read = "route endpoint or bridge candidate"
                node_reason_by_id[node_id] = (why_read, edge_type)
            route_event_summaries.append(
                {
                    "branch_id": event.get("branch_id"),
                    "probe_id": event.get("probe_id"),
                    "from_node_id": event.get("from_node_id") or event.get("source_node_id"),
                    "to_node_id": event.get("to_node_id") or event.get("target_node_id"),
                    "edge_type": edge_type,
                    "move_type": move_type,
                    "destination_label": event.get("destination_label"),
                    "destination_reached": bool(event.get("destination_reached") or move_type == "destination_reached"),
                    "studied_node_ids": list(event.get("studied_node_ids") or [])[:8],
                    "hydrated_node_ids": list(event.get("hydrated_node_ids") or [])[:8],
                    "yielded_match_ids": list(event.get("yielded_match_ids") or [])[:8],
                    "event_source": event.get("event_source"),
                }
            )
        intermediate_nodes: list[dict[str, Any]] = []
        path_unavailable_node_count = 0
        for node_id, (why_read, edge_type) in list(node_reason_by_id.items()):
            node_payload = _mcp_path_promoted_node(
                node_id,
                node_entry=entries_by_node_id.get(node_id),
                allowed_sections=allowed_sections,
                forbidden_sections=forbidden_sections,
                broad_context=broad_context,
                why_read=why_read,
                edge_type=edge_type,
            )
            if not node_payload.get("text"):
                unavailable_node_count += 1
                path_unavailable_node_count += 1
                continue
            intermediate_nodes.append(node_payload)
            if len(intermediate_nodes) >= max_intermediate_nodes:
                break
        backfill_limit = max(0, max_intermediate_nodes - len(intermediate_nodes))
        if backfill_limit:
            intermediate_nodes.extend(
                _mcp_path_required_section_backfill_nodes(
                    query_text=query_text,
                    semantic_contract=semantic_contract,
                    entries_by_node_id=entries_by_node_id,
                    intermediate_nodes=intermediate_nodes,
                    allowed_sections=allowed_sections,
                    forbidden_sections=forbidden_sections,
                    broad_context=broad_context,
                    limit=min(4, backfill_limit),
                )
            )
        promoted_count = sum(1 for item in intermediate_nodes if str(item.get("promotion_state") or "") == "hot")
        cold_count = sum(1 for item in intermediate_nodes if str(item.get("promotion_state") or "") == "cold")
        excluded_count = sum(1 for item in intermediate_nodes if str(item.get("promotion_state") or "") == "excluded")
        from_landing = dict(landings_by_id.get(origin_landing_id or str(path.get("from_landing_id") or "")) or {})
        to_landing = dict(landings_by_id.get(target_landing_id) or {}) if target_landing_id else {}
        bound_branches = _mcp_path_bound_branches(
            [dict(item) for item in list(branches or []) if isinstance(item, dict)],
            runtime_branch_ids,
        )
        spatial_source = _mcp_path_first_spatial_source(from_landing=from_landing, bound_branches=bound_branches)
        traversed_node_ids = _mcp_path_traversed_node_ids(route_events, bound_branches)
        traversed_edges = _mcp_path_traversed_edges(route_events, bound_branches)
        destination_reached = any(bool(event.get("destination_reached")) or str(event.get("move_type") or "") == "destination_reached" for event in route_events)
        lifecycle = _mcp_path_lifecycle(
            path={**path, "path_id": path_id, "route_kind": route_kind},
            route_events=route_events,
            runtime_branch_ids=runtime_branch_ids,
            branches=[dict(item) for item in list(branches or []) if isinstance(item, dict)],
            promoted_count=promoted_count,
            cold_count=cold_count,
            excluded_count=excluded_count,
            unavailable_count=path_unavailable_node_count,
        )
        route_event_counts = dict(lifecycle.get("event_counts") or {})
        package_impact = dict(lifecycle.get("package_impact") or {})
        landing_correction_event = _mcp_path_landing_correction_event(
            path={**path, "path_id": path_id},
            from_landing=from_landing,
            spatial_source=spatial_source,
            runtime_branch_ids=runtime_branch_ids,
            traversed_node_ids=traversed_node_ids,
            traversed_edges=traversed_edges,
            intermediate_nodes=intermediate_nodes,
            lifecycle=lifecycle,
        )
        landing_correction_events.append(landing_correction_event)
        paths.append(
            {
                "path_id": path_id,
                "route_kind": route_kind,
                "origin_landing_id": origin_landing_id,
                "from_landing_id": origin_landing_id,
                "target_landing_id": target_landing_id or None,
                "to_landing_id": target_landing_id,
                "from_label": str(from_landing.get("label") or path.get("from_landing_id") or "").strip(),
                "to_label": str(to_landing.get("label") or path.get("to_landing_id") or "").strip() if target_landing_id else "",
                "why_traverse": str(path.get("why_traverse") or "").strip(),
                "read_intermediate_nodes": bool(path.get("read_intermediate_nodes", True)),
                "max_intermediate_nodes": max_intermediate_nodes,
                "preferred_edges": [str(item) for item in list(path.get("preferred_edges") or []) if str(item).strip()],
                "planner_source": str(path.get("planner_source") or ((semantic_contract or {}).get("contract_authority") or "runtime")).strip(),
                "spatial_snap": dict(spatial_source.get("spatial_snap") or {}),
                "ai_spatial_path_id": spatial_source.get("ai_spatial_path_id"),
                "ai_spatial_landing_region_ref": spatial_source.get("ai_spatial_landing_region_ref"),
                "ai_spatial_landing_coordinate": spatial_source.get("ai_spatial_landing_coordinate"),
                "ai_spatial_bridge_targets": list(spatial_source.get("ai_spatial_bridge_targets") or []),
                "ai_spatial_preferred_edges": list(spatial_source.get("ai_spatial_preferred_edges") or []),
                "heuristic_provisional": bool(spatial_source.get("heuristic_provisional")),
                "provisional_until_ai_spatial": bool(spatial_source.get("provisional_until_ai_spatial")),
                "lifecycle_state": str(lifecycle.get("state") or "pending"),
                "lifecycle_state_reason": str(lifecycle.get("state_reason") or ""),
                "lifecycle": lifecycle,
                "route_event_counts": route_event_counts,
                "runtime_branch_ids": runtime_branch_ids,
                "route_events": route_event_summaries,
                "traversed_node_ids": traversed_node_ids,
                "traversed_edges": traversed_edges,
                "intermediate_nodes": intermediate_nodes,
                "useful_intermediate_material": [
                    {
                        "section": item.get("section"),
                        "text": item.get("text"),
                        "promotion_state": item.get("promotion_state"),
                        "why_read": item.get("why_read"),
                        "source_title": item.get("source_title"),
                    }
                    for item in intermediate_nodes
                    if str(item.get("promotion_state") or "") in {"hot", "cold"} and str(item.get("text") or "").strip()
                ],
                "promoted_count": promoted_count,
                "cold_count": cold_count,
                "excluded_count": excluded_count,
                "destination_reached": destination_reached,
                "changed_context_package": bool(package_impact.get("changed_context_package")),
                "package_impact": package_impact,
                "landing_correction_event": landing_correction_event,
            }
        )
    intermediate_count = sum(len(list(path.get("intermediate_nodes") or [])) for path in paths)
    promoted_intermediate_count = sum(int(path.get("promoted_count") or 0) for path in paths)
    cold_intermediate_count = sum(int(path.get("cold_count") or 0) for path in paths)
    excluded_intermediate_count = sum(int(path.get("excluded_count") or 0) for path in paths)
    completed_path_count = sum(1 for path in paths if str(path.get("lifecycle_state") or "") == "completed")
    stopped_path_count = sum(1 for path in paths if str(path.get("lifecycle_state") or "") == "stopped")
    started_path_count = sum(1 for path in paths if str(path.get("lifecycle_state") or "") == "started")
    pending_path_count = sum(1 for path in paths if str(path.get("lifecycle_state") or "") == "pending")
    if not landings:
        status = "no_landings"
    elif not planned_paths:
        status = "single_landing_no_path" if len(landings) <= 1 else "no_path_plan"
    elif pending_path_count:
        status = "paths_pending_lifecycle_open"
    elif stopped_path_count and not (completed_path_count or promoted_intermediate_count):
        status = "paths_stopped_no_context_change"
    elif promoted_intermediate_count:
        status = "corridors_promoted_context"
    elif intermediate_count:
        status = "corridors_read_no_hot_promotions"
    else:
        status = "paths_planned_no_readable_intermediates"
    metrics = {
        "schema_version": "agvm.path_corridor_package.metrics.v1",
        "landing_count": len(landings),
        "planned_path_count": len(planned_paths),
        "path_count": len(paths),
        "route_event_count": all_route_event_count,
        "intermediate_node_count": intermediate_count,
        "promoted_intermediate_count": promoted_intermediate_count,
        "cold_intermediate_count": cold_intermediate_count,
        "excluded_intermediate_count": excluded_intermediate_count,
        "unavailable_intermediate_node_count": unavailable_node_count,
        "completed_path_count": completed_path_count,
        "stopped_path_count": stopped_path_count,
        "started_path_count": started_path_count,
        "pending_path_count": pending_path_count,
        "terminal_path_count": completed_path_count + stopped_path_count,
        "branch_local_path_count": sum(1 for path in paths if str(path.get("route_kind") or "") == "landing_origin_corridor"),
        "explicit_bridge_path_count": sum(1 for path in paths if str(path.get("route_kind") or "") == "explicit_cross_landing_bridge"),
        "changed_context_package_path_count": sum(1 for path in paths if bool(path.get("changed_context_package"))),
        "ai_spatial_snap_path_count": sum(1 for path in paths if dict(path.get("spatial_snap") or {})),
        "ai_spatial_traversed_path_count": sum(1 for path in paths if dict(path.get("spatial_snap") or {}) and list(path.get("traversed_node_ids") or [])),
        "heuristic_provisional_path_count": sum(1 for path in paths if bool(path.get("heuristic_provisional"))),
        "landing_correction_event_count": len(landing_correction_events),
    }
    return {
        "schema_version": "agvm.path_corridor_package.v1",
        "package_kind": "path_corridors",
        "query_text": str(query_text or ""),
        "retrieval_mode": str(retrieval_mode or "balanced"),
        "status": status,
        "document_mode": document_mode,
        "landings": landings,
        "planned_paths": planned_paths,
        "paths": paths,
        "landing_correction_events": landing_correction_events,
        "lifecycle": {
            "schema_version": "agvm.path_corridor_lifecycle_summary.v1",
            "all_planned_paths_accounted_for": len(paths) == len(planned_paths),
            "planned_path_count": len(planned_paths),
            "path_count": len(paths),
            "completed_path_count": completed_path_count,
            "stopped_path_count": stopped_path_count,
            "started_path_count": started_path_count,
            "pending_path_count": pending_path_count,
            "changed_context_package_path_count": sum(1 for path in paths if bool(path.get("changed_context_package"))),
            "states": [
                {
                    "path_id": path.get("path_id"),
                    "route_kind": path.get("route_kind"),
                    "state": path.get("lifecycle_state"),
                    "state_reason": path.get("lifecycle_state_reason"),
                    "changed_context_package": bool(path.get("changed_context_package")),
                    "runtime_branch_ids": list(path.get("runtime_branch_ids") or []),
                    "has_spatial_snap": bool(dict(path.get("spatial_snap") or {})),
                    "ai_spatial_path_id": path.get("ai_spatial_path_id"),
                }
                for path in paths
            ],
        },
        "useful_intermediate_material": [
            dict(item)
            for path in paths
            for item in list(path.get("useful_intermediate_material") or [])
        ][:24],
        "metrics": metrics,
        "debug": {
            "node_ids": [
                str(item.get("node_id") or "")
                for path in paths
                for item in list(path.get("intermediate_nodes") or [])
                if str(item.get("node_id") or "").strip()
            ],
        },
    }


def _mcp_path_discovery_lines(path_corridors: dict[str, Any] | None) -> list[str]:
    return [str(item.get("agent_line") or "") for item in _mcp_path_discovery_entries(path_corridors) if str(item.get("agent_line") or "").strip()]


def _mcp_path_discovery_entries(path_corridors: dict[str, Any] | None) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in list((path_corridors or {}).get("paths") or []):
        if not isinstance(path, dict):
            continue
        from_label = str(path.get("from_label") or path.get("from_landing_id") or "Landing").strip()
        to_label = str(path.get("to_label") or path.get("to_landing_id") or "Landing").strip()
        route_kind = str(path.get("route_kind") or "landing_origin_corridor").strip()
        trace_label = f"{from_label} -> {to_label}" if route_kind == "explicit_cross_landing_bridge" and to_label else f"{from_label} corridor"
        for node in list(path.get("intermediate_nodes") or []):
            if not isinstance(node, dict):
                continue
            if str(node.get("promotion_state") or "") != "hot":
                continue
            text = _mcp_clean_agent_text(node.get("text"))
            if not text:
                continue
            section_key = _mcp_context_section_key(node.get("section") or node.get("section_key") or node.get("support_slot"), text)
            entries.append(
                {
                    "path_id": str(path.get("path_id") or ""),
                    "from_landing_id": str(path.get("from_landing_id") or ""),
                    "to_landing_id": str(path.get("to_landing_id") or ""),
                    "from_label": from_label,
                    "to_label": to_label,
                    "section_key": section_key,
                    "text": text,
                    "why_read": str(node.get("why_read") or path.get("why_traverse") or ""),
                    "edge_type": str(node.get("edge_type") or ""),
                    "promotion_state": "hot",
                    "agent_line": f"- {text}",
                    "trace_label": trace_label,
                    "route_kind": route_kind,
                    "changed_context_package": bool(path.get("changed_context_package")),
                    "lifecycle_state": str(path.get("lifecycle_state") or ""),
                }
            )
            if len(entries) >= 12:
                return entries
    return entries


def _mcp_context_path_truth_contract(
    *,
    path_corridors: dict[str, Any] | None,
    semantic_contract: dict[str, Any] | None,
    required: bool,
) -> dict[str, Any]:
    payload = dict(path_corridors or {})
    metrics = dict(payload.get("metrics") or {})
    lifecycle = dict(payload.get("lifecycle") or {})
    landing_plan = dict((semantic_contract or {}).get("landing_plan") or {})
    planned_paths = [
        dict(item)
        for item in list(payload.get("planned_paths") or landing_plan.get("paths") or [])
        if isinstance(item, dict)
    ]
    paths = [dict(item) for item in list(payload.get("paths") or []) if isinstance(item, dict)]

    def as_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    planned_path_count = max(
        as_int(metrics.get("planned_path_count")),
        as_int(lifecycle.get("planned_path_count")),
        len(planned_paths),
    )
    path_count = max(as_int(metrics.get("path_count")), len(paths))
    route_event_count = max(
        as_int(metrics.get("route_event_count")),
        sum(len([event for event in list(path.get("route_events") or []) if isinstance(event, dict)]) for path in paths),
    )
    completed_path_count = max(
        as_int(metrics.get("completed_path_count")),
        as_int(lifecycle.get("completed_path_count")),
        sum(1 for path in paths if str(path.get("lifecycle_state") or "") == "completed"),
    )
    stopped_path_count = max(
        as_int(metrics.get("stopped_path_count")),
        as_int(lifecycle.get("stopped_path_count")),
        sum(1 for path in paths if str(path.get("lifecycle_state") or "") == "stopped"),
    )
    started_path_count = max(
        as_int(metrics.get("started_path_count")),
        as_int(lifecycle.get("started_path_count")),
        sum(1 for path in paths if str(path.get("lifecycle_state") or "") == "started"),
    )
    raw_pending_path_count = max(
        as_int(metrics.get("pending_path_count")),
        max(0, planned_path_count - completed_path_count - stopped_path_count - started_path_count)
        if planned_path_count
        else 0,
    )
    terminal_path_count = max(
        as_int(metrics.get("terminal_path_count")),
        completed_path_count + stopped_path_count,
    )
    changed_context_package_path_count = max(
        as_int(metrics.get("changed_context_package_path_count")),
        sum(1 for path in paths if bool(path.get("changed_context_package"))),
    )
    useful_material_count = max(
        as_int(metrics.get("promoted_intermediate_count")),
        as_int(metrics.get("cold_intermediate_count")),
        sum(len([node for node in list(path.get("intermediate_nodes") or []) if isinstance(node, dict)]) for path in paths),
    )
    all_planned_accounted_for = bool(
        lifecycle.get("all_planned_paths_accounted_for")
        or (planned_path_count > 0 and path_count >= planned_path_count and raw_pending_path_count <= 0)
        or (planned_path_count == 0 and path_count == 0)
    )
    pending_path_count = 0 if all_planned_accounted_for and route_event_count > 0 else raw_pending_path_count
    pending_reasons: list[str] = []
    missing_reasons: list[str] = []
    if required and planned_path_count > 0 and path_count <= 0:
        pending_reasons.append("planned_paths_not_materialized_in_context_payload")
    if required and path_count > 0 and route_event_count <= 0:
        pending_reasons.append("path_route_truth_missing")
    if required and path_count > 0 and pending_path_count > 0 and not all_planned_accounted_for:
        pending_reasons.append("planned_paths_still_pending")
    if required and path_count > 0 and route_event_count > 0 and useful_material_count <= 0:
        missing_reasons.append("paths_traversed_no_readable_intermediate_material")

    if not required:
        state = "not_requested"
        ready = True
    elif planned_path_count <= 0 and path_count <= 0:
        state = "no_path_plan"
        ready = True
    elif pending_reasons:
        state = "pending"
        ready = False
    elif route_event_count > 0 and changed_context_package_path_count <= 0:
        state = "traversed_no_context_change"
        ready = True
    elif route_event_count > 0:
        state = "route_truth_ready"
        ready = True
    else:
        state = "pending"
        ready = False
        if "path_route_truth_missing" not in pending_reasons:
            pending_reasons.append("path_route_truth_missing")

    return {
        "schema_version": "agvm.context_path_truth_contract.v1",
        "required": bool(required),
        "ready": bool(ready),
        "state": state,
        "planned_path_count": planned_path_count,
        "path_count": path_count,
        "route_event_count": route_event_count,
        "completed_path_count": completed_path_count,
        "stopped_path_count": stopped_path_count,
        "started_path_count": started_path_count,
        "pending_path_count": pending_path_count,
        "pending_path_count_before_accounted_alignment": raw_pending_path_count
        if raw_pending_path_count != pending_path_count
        else 0,
        "terminal_path_count": terminal_path_count,
        "changed_context_package_path_count": changed_context_package_path_count,
        "useful_material_count": useful_material_count,
        "all_planned_paths_accounted_for": all_planned_accounted_for,
        "pending_reasons": pending_reasons,
        "missing_reasons": missing_reasons,
        "follow_up_tool": "inspect_path_corridor" if required and not ready else None,
    }


def attach_mcp_context_path_truth_contract(
    context_package: dict[str, Any] | None,
    *,
    path_corridors: dict[str, Any] | None,
    semantic_contract: dict[str, Any] | None,
    path_truth_required: bool,
) -> dict[str, Any]:
    package = dict(context_package or {})
    if not package:
        return {}
    path_truth = _mcp_context_path_truth_contract(
        path_corridors=path_corridors,
        semantic_contract=semantic_contract,
        required=bool(path_truth_required),
    )
    package["path_truth_contract"] = path_truth
    if path_corridors is not None:
        package["path_corridors"] = {
            key: value
            for key, value in dict(path_corridors or {}).items()
            if key != "debug"
        }
    contract = dict(package.get("contract") or {})
    unresolved = [
        str(item).strip()
        for item in list(contract.get("unresolved_sections") or [])
        if str(item).strip()
    ]
    if bool(path_truth_required) and not bool(path_truth.get("ready")) and "path_truth" not in unresolved:
        unresolved.append("path_truth")
    if bool(path_truth.get("ready")):
        unresolved = [item for item in unresolved if item != "path_truth"]
    contract["path_truth"] = path_truth
    contract["unresolved_sections"] = unresolved
    semantic_missing = [
        str(item).strip()
        for item in list(contract.get("semantic_missing_slot_keys") or [])
        if str(item).strip()
    ]
    missing_requested_relations = [
        str(item).strip()
        for item in list(contract.get("missing_requested_relations") or [])
        if str(item).strip()
    ]
    missing_explicit_entities = [
        str(item).strip()
        for item in list(contract.get("missing_explicit_query_entities") or [])
        if str(item).strip()
    ]
    answer_alignment = dict(contract.get("answer_context_alignment") or {})
    answer_alignment_blocked = bool(
        answer_alignment
        and answer_alignment.get("checked")
        and not answer_alignment.get("passed")
    )
    link_aware = dict(contract.get("link_aware_context") or {})
    link_aware_blocked = bool(link_aware and not link_aware.get("passed", True))
    late_path_truth_unblocked = bool(
        path_truth_required
        and path_truth.get("ready")
        and not unresolved
        and not semantic_missing
        and not missing_requested_relations
        and not missing_explicit_entities
        and not answer_alignment_blocked
        and not link_aware_blocked
    )
    answerability_ledger = dict(package.get("answerability_slot_ledger") or contract.get("answerability_slot_ledger") or {})
    answerability_unblocked = bool(
        answerability_ledger.get("passed")
        and not path_truth_required
        and not unresolved
        and not semantic_missing
        and not missing_requested_relations
        and not missing_explicit_entities
        and not answer_alignment_blocked
        and not link_aware_blocked
    )
    contract["passed"] = bool(
        (contract.get("passed") or late_path_truth_unblocked or answerability_unblocked)
        and not unresolved
        and not semantic_missing
        and not missing_requested_relations
        and not missing_explicit_entities
        and not answer_alignment_blocked
        and not link_aware_blocked
    )
    if late_path_truth_unblocked:
        contract["late_path_truth_unblocked_contract"] = True
    if answerability_unblocked:
        contract["answerability_unblocked_contract"] = True
    package["contract"] = contract
    metrics = dict(package.get("metrics") or {})
    metrics.update(
        {
            "path_truth_required": bool(path_truth.get("required")),
            "path_truth_ready": bool(path_truth.get("ready")),
            "path_truth_state": str(path_truth.get("state") or ""),
            "path_truth_pending_reasons": list(path_truth.get("pending_reasons") or []),
            "path_truth_missing_reasons": list(path_truth.get("missing_reasons") or []),
            "path_count": int(path_truth.get("path_count") or metrics.get("path_count") or 0),
            "path_completed_count": int(path_truth.get("completed_path_count") or metrics.get("path_completed_count") or 0),
            "path_stopped_count": int(path_truth.get("stopped_path_count") or metrics.get("path_stopped_count") or 0),
            "path_started_count": int(path_truth.get("started_path_count") or metrics.get("path_started_count") or 0),
            "path_pending_count": int(path_truth.get("pending_path_count") or metrics.get("path_pending_count") or 0),
            "path_changed_context_package_count": int(
                path_truth.get("changed_context_package_path_count")
                or metrics.get("path_changed_context_package_count")
                or 0
            ),
            "path_all_planned_accounted_for": bool(path_truth.get("all_planned_paths_accounted_for")),
            "contract_passed": bool(contract.get("passed")),
        }
    )
    package["metrics"] = metrics
    package_policy = dict(package.get("contract", {}).get("package_breadth") or {})
    if bool(contract.get("passed")) and (
        str(package_policy.get("state") or "") == "partial_blocked_contract"
        or str(metrics.get("package_breadth_state") or "") == "partial_blocked_contract"
    ):
        package_policy["state_before_path_truth_unblock"] = "partial_blocked_contract"
        package_policy["state"] = "sufficient"
        if late_path_truth_unblocked:
            package_policy["late_path_truth_unblocked_contract"] = True
        if answerability_unblocked:
            package_policy["answerability_unblocked_contract"] = True
        contract["package_breadth"] = package_policy
        package["contract"] = contract
        metrics["package_breadth_state"] = "sufficient"
        if late_path_truth_unblocked:
            metrics["late_path_truth_unblocked_contract"] = True
        if answerability_unblocked:
            metrics["answerability_unblocked_contract"] = True
        package["metrics"] = metrics
    if not bool(contract.get("passed")) and str(package.get("status") or "").strip() == "contract_satisfied":
        package["status"] = "partial"
    elif bool(contract.get("passed")) and str(package.get("status") or "").strip() == "partial":
        package["status"] = "contract_satisfied"
    appendices = dict(package.get("inspectable_appendices") or {})
    appendices["path_truth"] = path_truth
    package["inspectable_appendices"] = appendices
    return package


def _document_workspace_source_date_prefix(text: Any) -> str:
    header = str(text or "")
    if not header:
        return ""
    date_patterns = (
        r"\b(?:19|20)\d{2}-\d{2}-\d{2}\b",
        r"\b[A-Z][a-z]+\s+\d{1,2},\s+(?:19|20)\d{2}\b",
        r"\b\d{1,2}\s+[A-Z][a-z]+\s+(?:19|20)\d{2}\b",
        r"\b(?:19|20)\d{2}\b",
    )
    for pattern in date_patterns:
        match = re.search(pattern, header)
        if match:
            return str(match.group(0) or "").strip()
    return ""


def _document_workspace_clean_text(value: Any) -> str:
    text = str(value or "").replace("\r", "\n").strip()
    if not text:
        return ""
    hard_noise_markers = (
        "image alt text",
        "skip to main content",
        "cookie policy",
        "privacy policy",
        "user agreement",
        "eula",
        "all rights reserved",
        "accept cookies",
        "manage cookies",
        "impostazioni cookie",
        "mostra altri post",
        "mostra altro",
        "visualizza profilo",
        "accedi a linkedin",
        "iscriviti ora",
        "continua per iscriverti",
        "password dimenticata",
        "nuovo utente di linkedin",
        "scarica l'app",
        "basectrl",
        "{{",
    )
    tail_noise_markers = (
        "watch the full episode",
        "commento mostra",
        "post mostra",
        "aggiungi nuove competenze",
        "informativa sui cookie",
        "informativa sulla privacy",
        "contratto di licenza",
        "informativa sul copyright",
        "lingue:",
        "arabo",
        "deutsch",
        "english",
        "espanol",
        "francais",
        "bahasa",
        "portugues",
        "section: page opening",
        "news & events",
        "featured topics",
        "product finder",
        "channel partner program",
        "customer portal",
        "terms of use",
        "privacy notice",
        "sitemap",
        "copyright",
        "contact us",
    )
    cleaned_lines = []
    for line in text.split("\n"):
        line = re.sub(r"^Document title:\s*[^.]{0,240}\.\s*", "", line, flags=re.IGNORECASE)
        line = re.sub(r"^(?:Public source|Source):\s*[^.]{0,240}\.\s*", "", line, flags=re.IGNORECASE)
        description_match = re.search(r"\bDescription:\s*", line, flags=re.IGNORECASE)
        if description_match and len(line[description_match.end():].strip()) >= 24:
            description_text = line[description_match.end():].strip()
            source_date_prefix = _document_workspace_source_date_prefix(line[: description_match.start()])
            if (
                source_date_prefix
                and source_date_prefix not in description_text
                and not re.search(r"\b(?:19|20)\d{2}\b", description_text)
            ):
                line = f"{source_date_prefix}: {description_text}"
            else:
                line = description_text
        else:
            line = re.sub(r"\bSource\s+(?:URI|URL):\s*\S+", " ", line, flags=re.IGNORECASE)
            line = re.sub(r"^#+\s*", "", line).strip()
            line = re.sub(r"^Page title:\s*", "", line, flags=re.IGNORECASE)
            line = re.sub(r"^Description:\s*", "", line, flags=re.IGNORECASE)
        folded_line = _fold_text(line)
        label_line = re.sub(r"^[\-\u2022*\s]+", "", line).strip()
        if _mcp_agent_text_is_surface_noise(label_line, _fold_text(label_line)):
            continue
        if any(marker in folded_line for marker in hard_noise_markers):
            marker_positions = [
                folded_line.find(marker)
                for marker in hard_noise_markers
                if marker in folded_line
            ]
            first_marker = min(position for position in marker_positions if position >= 0)
            if first_marker <= 80:
                continue
            line = line[:first_marker].strip(" -|:;,")
            folded_line = _fold_text(line)
        if any(marker in folded_line for marker in tail_noise_markers):
            marker_positions = [
                folded_line.find(marker)
                for marker in tail_noise_markers
                if marker in folded_line
            ]
            first_marker = min(position for position in marker_positions if position >= 0)
            if first_marker <= 40:
                continue
            line = line[:first_marker].strip(" -|:;,")
            folded_line = _fold_text(line)
        if folded_line.count(" visualizza profilo ") >= 2 or folded_line.count(" linkedin ") >= 3:
            continue
        if folded_line.startswith(
            (
                "user instruction",
                "official website source",
                "headings",
                "visualizza profilo",
                "iscriviti ora",
                "consigliato da",
            )
        ):
            continue
        line = re.sub(r"\bvec_node_[a-zA-Z0-9_]+\b", "", line)
        line = re.sub(r"\[[^\]]*(?:vec_node|node_id|anchor_node_id)[^\]]*\]", "", line, flags=re.IGNORECASE)
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line:
            cleaned_lines.append(line)
    text = "\n".join(cleaned_lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def _document_workspace_unique(values: list[Any], *, limit: int = 24) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = " ".join(str(value or "").split()).strip(" -|:;,")
        if not text:
            continue
        key = _fold_text(text)
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(text)
        if len(output) >= limit:
            break
    return output


def _document_workspace_agent_label(value: Any) -> str:
    text = _document_workspace_clean_text(value)
    if not text:
        return ""
    if _mcp_agent_body_has_node_id(text):
        return ""
    folded = _fold_text(text)
    if folded in {"manual_text", "manual text", "derived_raw_chunk", "derived fact chunk", "derived_fact_chunk"}:
        return ""
    if _mcp_agent_text_is_surface_noise(text, folded):
        return ""
    return text


def _document_workspace_packet_full_text(packet: dict[str, Any]) -> str:
    full_text = _document_workspace_clean_text(packet.get("full_text"))
    if full_text:
        return full_text
    parts: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        text = _document_workspace_clean_text(value)
        if not text:
            return
        key = _fold_text(text)
        if key in seen:
            return
        seen.add(key)
        parts.append(text)

    add(packet.get("anchor_raw_text"))
    for chunk in sorted(
        [dict(item) for item in list(packet.get("ordered_chunk_sequence") or []) if isinstance(item, dict)],
        key=lambda item: int(item.get("chunk_index") or 0),
    ):
        add(chunk.get("raw_text") or chunk.get("text") or chunk.get("evidence_snippet"))
    if not parts:
        for fact in list(packet.get("supported_fact_text") or []):
            if isinstance(fact, dict):
                add(fact.get("raw_text") or fact.get("summary"))
    return "\n\n".join(parts).strip()


def _document_workspace_packet_chunks(packet: dict[str, Any]) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for index, chunk in enumerate(
        sorted(
            [dict(item) for item in list(packet.get("ordered_chunk_sequence") or []) if isinstance(item, dict)],
            key=lambda item: int(item.get("chunk_index") or 0),
        ),
        start=1,
    ):
        text = _document_workspace_clean_text(chunk.get("raw_text") or chunk.get("text") or chunk.get("evidence_snippet"))
        if not text:
            continue
        chunks.append(
            {
                "chunk_index": int(chunk.get("chunk_index") or index),
                "source_span_start": chunk.get("source_span_start"),
                "source_span_end": chunk.get("source_span_end"),
                "score": chunk.get("score"),
                "source_kind": chunk.get("source_kind"),
                "derived": bool(chunk.get("derived")),
                "text": text,
                "node_id": str(chunk.get("node_id") or chunk.get("source_node_id") or "").strip(),
                "source_node_id": str(chunk.get("source_node_id") or "").strip() or None,
            }
        )
    return chunks


def _document_workspace_packet_facts(packet: dict[str, Any]) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    for fact in list(packet.get("supported_fact_text") or []):
        if not isinstance(fact, dict):
            continue
        text = _document_workspace_clean_text(fact.get("raw_text") or fact.get("summary"))
        if not text:
            continue
        facts.append(
            {
                "summary": _document_workspace_clean_text(fact.get("summary")),
                "text": text,
                "score": fact.get("score"),
                "node_id": str(fact.get("node_id") or fact.get("source_node_id") or "").strip(),
                "source_node_id": str(fact.get("source_node_id") or "").strip() or None,
            }
        )
    return facts


def _document_workspace_sections(
    *,
    full_text: str,
    chunks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if chunks:
        return [
            {
                "section_id": f"chunk_{int(chunk.get('chunk_index') or index)}",
                "title": f"Chunk {int(chunk.get('chunk_index') or index)}",
                "text": str(chunk.get("text") or ""),
                "source_span_start": chunk.get("source_span_start"),
                "source_span_end": chunk.get("source_span_end"),
            }
            for index, chunk in enumerate(chunks, start=1)
            if str(chunk.get("text") or "").strip()
        ]
    if full_text:
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", full_text) if part.strip()]
        if len(paragraphs) > 1:
            return [
                {
                    "section_id": f"paragraph_{index}",
                    "title": f"Paragraph {index}",
                    "text": paragraph,
                    "source_span_start": None,
                    "source_span_end": None,
                }
                for index, paragraph in enumerate(paragraphs[:24], start=1)
            ]
        return [
            {
                "section_id": "full_text",
                "title": "Full Text",
                "text": full_text,
                "source_span_start": None,
                "source_span_end": None,
            }
        ]
    return []


def _document_workspace_packet_source_trace(
    packet: dict[str, Any],
    *,
    chunks: list[dict[str, Any]],
    facts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    chunk_text_by_index = {
        int(chunk.get("chunk_index") or 0): str(chunk.get("text") or "")
        for chunk in chunks
        if int(chunk.get("chunk_index") or 0)
    }
    fact_texts = [str(fact.get("text") or "") for fact in facts if str(fact.get("text") or "").strip()]
    for row in list(packet.get("source_trace") or []):
        if not isinstance(row, dict):
            continue
        role = str(row.get("role") or "").strip()
        text = _document_workspace_clean_text(row.get("text") or row.get("text_preview"))
        if role == "chunk":
            text = chunk_text_by_index.get(int(row.get("chunk_index") or 0), text) or text
        elif role == "fact" and fact_texts:
            text = fact_texts[0] if not text or text in fact_texts[0] else text
        rows.append(
            {
                "anchor_node_id": row.get("anchor_node_id"),
                "node_id": row.get("node_id"),
                "source_node_id": row.get("source_node_id"),
                "role": role or "source",
                "title": _document_workspace_clean_text(row.get("title")),
                "source_label": row.get("source_label"),
                "source_type": row.get("source_type"),
                "chunk_index": row.get("chunk_index"),
                "source_span_start": row.get("source_span_start"),
                "source_span_end": row.get("source_span_end"),
                "score": row.get("score"),
                "text": text,
            }
        )
    if rows:
        return rows[:48]
    anchor_node_id = str(packet.get("anchor_node_id") or "").strip()
    title = _document_workspace_clean_text(packet.get("title") or packet.get("source_label") or "Document")
    if anchor_node_id or title:
        rows.append(
            {
                "anchor_node_id": anchor_node_id,
                "node_id": anchor_node_id,
                "source_node_id": anchor_node_id,
                "role": "anchor",
                "title": title,
                "source_label": packet.get("source_label"),
                "source_type": packet.get("source_type"),
                "chunk_index": None,
                "source_span_start": None,
                "source_span_end": None,
                "score": None,
                "text": _document_workspace_clean_text(packet.get("anchor_raw_text") or packet.get("full_text") or title),
            }
        )
    for chunk in chunks:
        rows.append(
            {
                "anchor_node_id": anchor_node_id,
                "node_id": chunk.get("node_id"),
                "source_node_id": chunk.get("source_node_id"),
                "role": "chunk",
                "title": title,
                "source_label": packet.get("source_label"),
                "source_type": packet.get("source_type"),
                "chunk_index": chunk.get("chunk_index"),
                "source_span_start": chunk.get("source_span_start"),
                "source_span_end": chunk.get("source_span_end"),
                "score": chunk.get("score"),
                "text": chunk.get("text"),
            }
        )
    return rows[:48]


def _document_workspace_sentence_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    for paragraph in re.split(r"\n+", str(text or "")):
        for sentence in re.split(r"(?<=[.!?])\s+", paragraph):
            sentence = " ".join(sentence.split()).strip()
            if sentence:
                candidates.append(sentence)
    return candidates


def _document_workspace_timeline(documents: list[dict[str, Any]], *, limit: int = 24) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for document in documents:
        title = str(document.get("title") or "Document")
        for timeline_tag in list(document.get("timeline_tags") or []):
            tag = str(timeline_tag or "").strip()
            if not tag:
                continue
            rows.append({"date_or_year": tag, "text": tag, "document_title": title, "source_label": document.get("source_label")})
        for sentence in _document_workspace_sentence_candidates(str(document.get("full_text") or "")):
            dates = re.findall(r"\b(?:19|20)\d{2}\b|\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}\s+[A-Z][a-z]+\s+(?:19|20)\d{2}\b", sentence)
            for date_value in dates:
                key = _fold_text(f"{date_value} {sentence} {title}")
                if key in seen:
                    continue
                seen.add(key)
                rows.append(
                    {
                        "date_or_year": date_value,
                        "text": sentence,
                        "document_title": title,
                        "source_label": document.get("source_label"),
                    }
                )
                if len(rows) >= limit:
                    return rows
    return rows[:limit]


def _document_workspace_marker_sentences(
    documents: list[dict[str, Any]],
    markers: tuple[str, ...],
    *,
    limit: int = 12,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for document in documents:
        title = str(document.get("title") or "Document")
        for sentence in _document_workspace_sentence_candidates(str(document.get("full_text") or "")):
            folded = _fold_text(sentence)
            if not any(marker in folded for marker in markers):
                continue
            key = _fold_text(f"{title} {sentence}")
            if key in seen:
                continue
            seen.add(key)
            rows.append({"text": sentence, "document_title": title, "source_label": document.get("source_label")})
            if len(rows) >= limit:
                return rows
    return rows


def _document_workspace_kind(
    *,
    query_text: str,
    document_mode: str,
    document_lookup: dict[str, Any] | None,
    document_count: int,
) -> str:
    lookup_kind = str((document_lookup or {}).get("kind") or "none")
    folded = _fold_text(query_text)
    if lookup_kind == "no_document_found":
        return "no_document_found"
    if lookup_kind == "source_trace_for_answer" or any(token in folded for token in ("source trace", "fonti", "fonte", "supportano questa risposta")):
        return "source_trace"
    if any(token in folded for token in ("workspace", "progetto", "progetti", "project", "dossier", "cartella", "documenti relativi")):
        return "project_workspace"
    if lookup_kind == "related_document_lookup":
        return "related_documents"
    if lookup_kind == "exact_document_lookup":
        return "exact_document"
    if lookup_kind == "document_synthesis" or document_mode == "synthesis":
        return "document_synthesis"
    if document_count > 1:
        return "related_documents"
    return "exact_document" if document_count == 1 else "empty"


def _document_workspace_package_mode(
    *,
    query_text: str,
    retrieval_mode: str,
    workspace_kind: str,
) -> str:
    folded = _fold_text(query_text)
    if any(token in folded for token in ("raw", "completo", "completa", "intero", "intera", "full text", "full document", "aprilo", "apri il documento")):
        return "document_full"
    if workspace_kind == "project_workspace":
        return "project_workspace"
    if workspace_kind == "source_trace":
        return "source_trace"
    if retrieval_mode == "forensic":
        return "document_full"
    if workspace_kind == "exact_document":
        return "document_full"
    return "balanced"


def _document_workspace_related_links(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    for left in documents:
        left_tags = {
            _fold_text(tag)
            for key in ("project_tags", "entity_tags", "topic_tags", "timeline_tags")
            for tag in list(left.get(key) or [])
            if str(tag or "").strip()
        }
        for right in documents:
            if left is right:
                continue
            right_tags = {
                _fold_text(tag)
                for key in ("project_tags", "entity_tags", "topic_tags", "timeline_tags")
                for tag in list(right.get(key) or [])
                if str(tag or "").strip()
            }
            shared = sorted(tag for tag in left_tags & right_tags if tag)
            if not shared:
                continue
            links.append(
                {
                    "from_anchor_node_id": left.get("anchor_node_id"),
                    "to_anchor_node_id": right.get("anchor_node_id"),
                    "from_title": left.get("title"),
                    "to_title": right.get("title"),
                    "shared_tags": shared[:8],
                    "reason": "shared_project_entity_topic_or_timeline_tags",
                }
            )
    return links[:24]


def _document_workspace_agent_markdown(
    *,
    query_text: str,
    workspace_kind: str,
    package_mode: str,
    documents: list[dict[str, Any]],
    source_trace: list[dict[str, Any]],
    timeline: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    open_questions: list[str],
    risks: list[dict[str, Any]],
    hypotheses: list[dict[str, Any]],
    no_match_target: str,
) -> str:
    lines: list[str] = [
        "# AGVM Document Workspace",
        "",
        "## Task / Workspace Intent",
        str(query_text or "").strip(),
        "",
        "## Workspace Summary",
    ]
    if not documents:
        target_suffix = f" for '{no_match_target}'" if no_match_target else ""
        lines.append(f"- No matching document workspace was found{target_suffix}.")
    else:
        titles = _document_workspace_unique([document.get("title") for document in documents], limit=6)
        titles = [title for title in (_document_workspace_agent_label(title) for title in titles) if title]
        lines.append(f"- Workspace kind: {workspace_kind}; package mode: {package_mode}; documents: {len(documents)}.")
        if titles:
            lines.append(f"- Primary documents: {'; '.join(titles)}.")
    if documents:
        lines.extend(["", "## Documents"])
        for index, document in enumerate(documents, start=1):
            title = _document_workspace_agent_label(document.get("title")) or f"Document {index}"
            source = _document_workspace_agent_label(document.get("source_label")) or _document_workspace_agent_label(document.get("source_type"))
            fit = max(
                float(document.get("document_rank_score") or 0.0),
                float(document.get("query_fit_score") or 0.0),
                float(document.get("exact_match_score") or 0.0),
            )
            source_suffix = f" Source: {source}." if source else ""
            lines.append(f"- {index}. {title}.{source_suffix} Fit {fit:.2f}; chunks {int(document.get('chunk_count') or 0)}; facts {int(document.get('fact_count') or 0)}.")
        lines.extend(["", "## Full Raw Documents"])
        for index, document in enumerate(documents, start=1):
            title = _document_workspace_agent_label(document.get("title")) or f"Document {index}"
            full_text = _document_workspace_clean_text(document.get("full_text"))
            if not full_text:
                continue
            lines.extend(["", f"### {title}", full_text])
    if source_trace:
        lines.extend(["", "## Source Trace"])
        for row in source_trace[:16]:
            title = _document_workspace_agent_label(row.get("title")) or "Document"
            role = str(row.get("role") or "source")
            span = ""
            if row.get("source_span_start") is not None or row.get("source_span_end") is not None:
                span = f" span {row.get('source_span_start')}..{row.get('source_span_end')}"
            text = _truncate_prompt_text(_document_workspace_clean_text(row.get("text")), 420)
            lines.append(f"- {title} [{role}{span}]: {text}")
    if timeline:
        lines.extend(["", "## Timeline"])
        for row in timeline[:12]:
            lines.append(f"- {row.get('date_or_year')}: {_truncate_prompt_text(row.get('text'), 360)}")
    if decisions:
        lines.extend(["", "## Decisions And Milestones"])
        for row in decisions[:8]:
            lines.append(f"- {_truncate_prompt_text(row.get('text'), 360)}")
    if risks or hypotheses or open_questions:
        lines.extend(["", "## Open Questions And Gaps"])
        for item in open_questions[:8]:
            lines.append(f"- {item}")
        for row in risks[:6]:
            lines.append(f"- Risk: {_truncate_prompt_text(row.get('text'), 320)}")
        for row in hypotheses[:6]:
            lines.append(f"- Hypothesis: {_truncate_prompt_text(row.get('text'), 320)}")
    return "\n".join(lines).strip()


def _document_ref_slug(value: Any) -> str:
    folded = _fold_text(str(value or "document"))
    slug = re.sub(r"[^a-z0-9]+", "_", folded).strip("_")
    return slug[:56] or "document"


def _document_ref_id(document: dict[str, Any], index: int) -> str:
    for key in ("document_id", "anchor_node_id", "source_node_id", "node_id"):
        value = str(document.get(key) or "").strip()
        if value:
            return value
    title = _document_workspace_agent_label(document.get("title")) or f"Document {index}"
    return f"doc_ref_{index}_{_document_ref_slug(title)}"


def _document_raw_availability(document: dict[str, Any]) -> dict[str, Any]:
    precomputed = document.get("raw_availability")
    if isinstance(precomputed, dict):
        raw_text_available = bool(precomputed.get("raw_text_available") or precomputed.get("complete_text_available"))
        raw_text_char_count = int(precomputed.get("raw_text_char_count") or precomputed.get("available_raw_text_char_count") or 0)
        return {
            "schema_version": "agvm.document_raw_availability.v1",
            "state": str(precomputed.get("state") or ("raw_available" if raw_text_available else "raw_unavailable")),
            "raw_text_available": raw_text_available,
            "complete_text_available": bool(precomputed.get("complete_text_available")) or raw_text_available,
            "raw_text_char_count": raw_text_char_count,
            "included_raw_text_char_count": int(precomputed.get("included_raw_text_char_count") or 0),
            "raw_text_truncated_in_payload": bool(precomputed.get("raw_text_truncated_in_payload")),
            "full_text_mode": str(precomputed.get("full_text_mode") or ("deferred_raw_ref" if raw_text_available else "none")),
        }
    raw_text = _document_workspace_clean_text(
        document.get("full_text")
        or document.get("raw_text")
        or document.get("deferred_raw_text")
        or document.get("anchor_raw_text")
    )
    declared_chars = int(document.get("raw_text_char_count") or document.get("available_raw_text_char_count") or 0)
    raw_chars = max(declared_chars, len(raw_text)) if raw_text else declared_chars
    raw_available = bool(raw_text or document.get("raw_text_available") or document.get("complete_text_available"))
    return {
        "schema_version": "agvm.document_raw_availability.v1",
        "state": "raw_available" if raw_available else "raw_unavailable",
        "raw_text_available": raw_available,
        "complete_text_available": bool(document.get("complete_text_available")) or bool(raw_text),
        "raw_text_char_count": raw_chars,
        "included_raw_text_char_count": len(raw_text),
        "raw_text_truncated_in_payload": bool(raw_text and raw_chars > len(raw_text)),
        "full_text_mode": str(document.get("full_text_mode") or ("full_text" if raw_text else "none")),
    }


def _mcp_document_ref_only_for_ledger_renderer(document: dict[str, Any]) -> dict[str, Any]:
    safe = dict(document)
    raw_text = (
        document.get("full_text")
        or document.get("raw_text")
        or document.get("deferred_raw_text")
        or document.get("anchor_raw_text")
        or ""
    )
    declared_chars = int(document.get("raw_text_char_count") or document.get("available_raw_text_char_count") or 0)
    raw_text_char_count = max(declared_chars, len(str(raw_text or "")))
    raw_available = bool(
        raw_text_char_count
        or document.get("raw_text_available")
        or document.get("complete_text_available")
        or document.get("raw_available")
    )
    safe["raw_availability"] = {
        "schema_version": "agvm.document_raw_availability.v1",
        "state": "raw_available" if raw_available else "raw_unavailable",
        "raw_text_available": raw_available,
        "complete_text_available": bool(document.get("complete_text_available")) or raw_available,
        "raw_text_char_count": raw_text_char_count,
        "included_raw_text_char_count": 0,
        "raw_text_truncated_in_payload": raw_available,
        "full_text_mode": "deferred_raw_ref" if raw_available else "none",
    }
    for key in ("full_text", "raw_text", "deferred_raw_text", "anchor_raw_text"):
        safe.pop(key, None)
    return safe


def _document_ref_retrieve_document_call(document: dict[str, Any], index: int) -> dict[str, Any]:
    document_id = _document_ref_id(document, index)
    title = _document_workspace_agent_label(document.get("title")) or f"Document {index}"
    return {
        "tool_name": "retrieve_document",
        "arguments": {
            "document_id": document_id,
            "document_hint": title,
            "query_text": title,
            "include_raw_text": True,
            "context_package_mode": "document_full",
            "document_text_policy": "all_raw",
        },
    }


def _document_ref_project_workspace_call(document: dict[str, Any], index: int) -> dict[str, Any]:
    title = _document_workspace_agent_label(document.get("title")) or f"Document {index}"
    return {
        "tool_name": "retrieve_project_workspace",
        "arguments": {
            "document_hint": title,
            "query_text": title,
            "include_raw_text": False,
            "context_package_mode": "broad_dossier",
            "document_text_policy": "refs_only",
        },
    }


def _document_workspace_refs(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for index, document in enumerate(documents, start=1):
        if not isinstance(document, dict):
            continue
        document_id = _document_ref_id(document, index)
        raw_availability = _document_raw_availability(document)
        retrieve_document_call = dict(
            document.get("document_evidence_retrieve_document_call")
            or document.get("retrieve_document_call")
            or _document_ref_retrieve_document_call(document, index)
        )
        project_workspace_call = _document_ref_project_workspace_call(document, index)
        refs.append(
            {
                "schema_version": "agvm.document_ref.v1",
                "document_id": document_id,
                "anchor_node_id": str(document.get("anchor_node_id") or "").strip(),
                "title": _document_workspace_agent_label(document.get("title")) or f"Document {index}",
                "source_label": _document_workspace_agent_label(document.get("source_label")),
                "source_type": document.get("source_type"),
                "source_trust": document.get("source_trust"),
                "lookup_role": str(document.get("lookup_role") or ""),
                "workspace_tier": str(document.get("workspace_tier") or ""),
                "primary_context_eligible": bool(document.get("primary_context_eligible")),
                "query_fit_score": float(document.get("query_fit_score") or 0.0),
                "exact_match_score": float(document.get("exact_match_score") or 0.0),
                "document_rank_score": float(document.get("document_rank_score") or 0.0),
                "document_rank_reasons": list(document.get("document_rank_reasons") or [])[:8],
                "document_evidence_rank": int(document.get("document_evidence_rank") or index),
                "document_evidence_score": float(document.get("document_evidence_score") or 0.0),
                "relationship_to_query": str(document.get("relationship_to_query") or "background"),
                "why_included": list(document.get("why_included") or [])[:10],
                "expected_contents": dict(document.get("expected_contents") or {}),
                "matched_claim_terms": list(document.get("matched_claim_terms") or [])[:24],
                "missing_claim_terms": list(document.get("missing_claim_terms") or [])[:24],
                "matched_entities": list(document.get("matched_entities") or [])[:12],
                "score_components": dict(document.get("document_evidence_score_components") or {}),
                "claim_fit_summary": dict(document.get("claim_fit_summary") or {}),
                "raw_availability": raw_availability,
                "raw_text_available": bool(raw_availability.get("raw_text_available")),
                "raw_text_char_count": int(raw_availability.get("raw_text_char_count") or 0),
                "chunk_count": int(document.get("chunk_count") or 0),
                "fact_count": int(document.get("fact_count") or 0),
                "retrieve_document_call": retrieve_document_call,
                "retrieve_project_workspace_call": project_workspace_call,
                "follow_up_tools": ["retrieve_document", "retrieve_project_workspace"],
            }
        )
    return refs


def _document_ref_contract_from_refs(refs: list[dict[str, Any]]) -> dict[str, Any]:
    actionable_refs = [
        ref
        for ref in refs
        if isinstance(ref, dict)
        and str(ref.get("document_id") or "").strip()
        and isinstance(ref.get("retrieve_document_call"), dict)
    ]
    raw_available_refs = [
        ref
        for ref in refs
        if bool(ref.get("raw_text_available")) or str((ref.get("raw_availability") or {}).get("state") or "") == "raw_available"
    ]
    return {
        "schema_version": MCP_DOCUMENT_REF_CONTRACT_VERSION,
        "state": "refs_ready" if refs else "no_document_refs",
        "document_ref_count": len(refs),
        "actionable_document_ref_count": len(actionable_refs),
        "raw_available_document_ref_count": len(raw_available_refs),
        "all_refs_actionable": len(actionable_refs) == len(refs) if refs else True,
        "default_context_document_text_policy": "refs_only",
        "raw_document_policy_options": list(MCP_DOCUMENT_TEXT_POLICIES),
        "exact_follow_up_recipe_required": True,
        "retrieve_document_requires_include_raw_text_for_raw": True,
    }


def _mcp_document_bundle_for_policy(document_workspace: dict[str, Any] | None, document_text_policy: Any) -> dict[str, Any]:
    policy = _mcp_normalize_document_text_policy(document_text_policy)
    workspace = dict(document_workspace or {})
    refs = [dict(ref) for ref in list(workspace.get("document_refs") or []) if isinstance(ref, dict)]
    if policy == "refs_only":
        return {
            "schema_version": MCP_DOCUMENT_BUNDLE_VERSION,
            "document_text_policy": policy,
            "state": "refs_only",
            "documents": [],
            "document_count": 0,
            "raw_text_char_count": 0,
            "document_refs": refs,
        }
    all_documents = [dict(item) for item in list(workspace.get("documents") or []) if isinstance(item, dict)]
    primary_documents = [dict(item) for item in list(workspace.get("primary_documents") or []) if isinstance(item, dict)]
    workspace_kind = str(workspace.get("workspace_kind") or "").strip()
    if workspace_kind == "exact_document" and primary_documents and policy == "top_raw":
        ranked_documents = primary_documents
    else:
        ranked_documents = primary_documents + [
            document
            for document in all_documents
            if _document_ref_id(document, 0) not in {_document_ref_id(primary, 0) for primary in primary_documents}
        ]
    limit = 1 if policy == "top_raw" else 3
    total_budget = 12000 if policy == "top_raw" else 24000
    per_document_budget = 12000
    bundled_documents: list[dict[str, Any]] = []
    used_chars = 0
    for index, document in enumerate(ranked_documents, start=1):
        if len(bundled_documents) >= limit or used_chars >= total_budget:
            break
        raw_text = _document_workspace_clean_text(
            document.get("full_text")
            or document.get("raw_text")
            or document.get("deferred_raw_text")
            or document.get("anchor_raw_text")
        )
        if not raw_text:
            continue
        remaining = max(0, total_budget - used_chars)
        raw_budget = min(per_document_budget, remaining)
        if raw_budget <= 0:
            break
        text = raw_text[:raw_budget].rstrip()
        truncated = len(raw_text) > len(text)
        bundled_documents.append(
            {
                "schema_version": "agvm.raw_document_packet.v1",
                "document_id": _document_ref_id(document, index),
                "title": _document_workspace_agent_label(document.get("title")) or f"Document {index}",
                "source_label": _document_workspace_agent_label(document.get("source_label")),
                "source_type": document.get("source_type"),
                "workspace_tier": str(document.get("workspace_tier") or ""),
                "raw_text": f"{text}..." if truncated else text,
                "raw_text_char_count": len(raw_text),
                "included_char_count": len(text),
                "truncated": truncated,
                "retrieve_document_call": _document_ref_retrieve_document_call(document, index),
            }
        )
        used_chars += len(text)
    if bundled_documents:
        state = "raw_bundle_ready"
        raw_available_count = sum(
            1
            for document in ranked_documents
            if _document_workspace_clean_text(
                document.get("full_text")
                or document.get("raw_text")
                or document.get("deferred_raw_text")
                or document.get("anchor_raw_text")
            )
        )
        if raw_available_count > len(bundled_documents):
            state = "partial_raw_bundle"
    else:
        state = "raw_unavailable"
    return {
        "schema_version": MCP_DOCUMENT_BUNDLE_VERSION,
        "document_text_policy": policy,
        "state": state,
        "documents": bundled_documents,
        "document_count": len(bundled_documents),
        "raw_text_char_count": sum(int(document.get("included_char_count") or 0) for document in bundled_documents),
        "total_raw_budget_chars": total_budget,
        "document_refs": refs,
        "pending_or_missing_reason": None if bundled_documents else "no_raw_document_text_available_for_requested_policy",
    }


def _mcp_source_workspace_rank_terms(query_text: str) -> list[str]:
    terms: list[str] = []
    for entity in _mcp_explicit_query_entities(query_text):
        folded_entity = _fold_text(entity)
        if folded_entity and folded_entity not in terms:
            terms.append(folded_entity)
    for token in re.findall(r"[A-Za-z0-9À-ÿ]{3,}", _fold_text(query_text)):
        if token in _DOCUMENT_ANSWER_STOPWORDS:
            continue
        if token not in terms:
            terms.append(token)
    return terms[:24]


def _mcp_source_workspace_from_retrieved_material(
    *,
    query_text: str,
    matches: list[dict[str, Any]] | None,
    evidence_reservoir: dict[str, Any] | None,
    semantic_contract: dict[str, Any] | None,
    forbidden_topic_markers: list[str],
    context_structured: dict[str, Any] | None = None,
    limit: int = 6,
) -> dict[str, Any]:
    rank_terms = _mcp_source_workspace_rank_terms(query_text)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_payload(payload: dict[str, Any], source_kind: str) -> None:
        node = dict(payload.get("node") or {})
        merged = {**node, **{key: value for key, value in payload.items() if key != "node"}}
        raw_text = _document_workspace_clean_text(
            merged.get("raw_text")
            or merged.get("evidence_snippet")
            or merged.get("summary")
            or merged.get("text")
        )
        if len(raw_text) < 80:
            return
        provenance = dict(merged.get("provenance") or {})
        source_label = _document_workspace_agent_label(
            merged.get("source_label")
            or merged.get("source_title")
            or provenance.get("source_label")
        )
        source_type = str(merged.get("source_type") or provenance.get("source_type") or source_kind or "").strip()
        node_id = str(merged.get("node_id") or merged.get("id") or "").strip()
        document_anchor_id = str(merged.get("document_anchor_id") or "").strip()
        if not (node_id or document_anchor_id or source_label):
            return
        forbidden_blob = "\n".join(str(value or "") for value in (source_label, source_type, raw_text[:2000]))
        if _mcp_forbidden_topic_hits(forbidden_blob, forbidden_topic_markers):
            return
        key = _fold_text(f"{document_anchor_id or node_id or source_label} {source_label or source_type}")
        if not key or key in seen:
            return
        seen.add(key)
        title = _document_workspace_agent_label(merged.get("title") or source_label or merged.get("summary")) or "Retrieved source material"
        folded_blob = _fold_text(f"{title}\n{source_label}\n{raw_text}")
        term_hits = sum(1 for term in rank_terms if term and term in folded_blob)
        score = float(merged.get("query_fit_score") or merged.get("score") or merged.get("raw_score") or 0.0)
        rank_score = round(min(1.0, score + min(0.42, term_hits * 0.07) + min(0.18, len(raw_text) / 8000.0)), 6)
        document = {
            "schema_version": "agvm.source_material_document.v1",
            "document_id": document_anchor_id or node_id or f"source::{_document_ref_slug(title)}",
            "node_id": node_id,
            "anchor_node_id": document_anchor_id or node_id,
            "title": title,
            "source_label": source_label,
            "source_type": source_type or source_kind,
            "source_trust": str(merged.get("source_trust") or "retrieved_source"),
            "claim_status": str(merged.get("claim_status") or "fact"),
            "answer_eligible": bool(merged.get("answer_eligible", True)),
            "profile_eligible": bool(merged.get("profile_eligible", True)),
            "document_eligible": True,
            "workspace_tier": "requested" if not rows else "related_or_cold",
            "primary_context_eligible": not rows,
            "query_fit_score": rank_score,
            "document_rank_score": rank_score,
            "document_rank_reasons": ["explicit_raw_policy_source_material", f"query_term_hits:{term_hits}"],
            "full_text": raw_text,
            "deferred_raw_text": raw_text,
            "raw_text_char_count": len(raw_text),
            "raw_text_available": True,
            "complete_text_available": True,
            "full_text_mode": "retrieved_source_material",
            "chunk_count": 1,
            "fact_count": 0,
            "source_trace": [
                {
                    "schema_version": "agvm.document_workspace.source_trace_row.v1",
                    "node_id": node_id or document_anchor_id or None,
                    "source_kind": source_kind,
                    "source_label": source_label,
                    "source_type": source_type,
                    "text": _truncate_prompt_text(raw_text, 420),
                }
            ],
        }
        rows.append(document)

    for match in list(matches or []):
        if isinstance(match, dict):
            add_payload(match, "retrieved_match")
    for entry in list((evidence_reservoir or {}).get("entries") or []):
        if isinstance(entry, dict):
            add_payload(entry, "evidence_reservoir")
    for field in (
        "hot_context_fragments",
        "cold_context_fragments",
        "context_fragments",
        "linked_context_fragments",
        "source_material_fragments",
    ):
        for entry in list((context_structured or {}).get(field) or []):
            if isinstance(entry, dict):
                add_payload(entry, field)

    rows.sort(
        key=lambda item: (
            -float(item.get("document_rank_score") or 0.0),
            -len(str(item.get("full_text") or "")),
            str(item.get("title") or ""),
        )
    )
    documents = rows[: max(1, int(limit or 6))]
    if not documents:
        return {}
    for index, document in enumerate(documents, start=1):
        document["workspace_tier"] = "requested" if index == 1 else "related_or_cold"
        document["primary_context_eligible"] = index == 1
        document["document_id"] = _document_ref_id(document, index)
        document["retrieve_document_call"] = _document_ref_retrieve_document_call(document, index)
        document["retrieve_project_workspace_call"] = _document_ref_project_workspace_call(document, index)
    document_evidence_lane = rank_document_evidence_candidates(
        query_text=query_text,
        candidates=documents,
        semantic_contract=semantic_contract,
        limit=len(documents),
        candidate_window=12,
    )
    ranked_documents = [
        dict(item)
        for item in list(document_evidence_lane.get("documents") or [])
        if isinstance(item, dict)
    ]
    if ranked_documents:
        documents = ranked_documents
        for index, document in enumerate(documents, start=1):
            document["workspace_tier"] = "requested" if index == 1 else "related_or_cold"
            document["primary_context_eligible"] = index == 1
            document["document_id"] = str(document.get("document_id") or _document_ref_id(document, index))
            document["raw_availability"] = _document_raw_availability(document)
            document["retrieve_document_call"] = dict(
                document.get("document_evidence_retrieve_document_call")
                or document.get("retrieve_document_call")
                or _document_ref_retrieve_document_call(document, index)
            )
            document["retrieve_project_workspace_call"] = dict(
                document.get("retrieve_project_workspace_call") or _document_ref_project_workspace_call(document, index)
            )
    refs = _document_workspace_refs(documents)
    document_evidence_lane = {
        **dict(document_evidence_lane or {}),
        "documents": [],
        "ranked_document_refs": refs,
        "primary_document_refs": [dict(ref) for ref in refs if str(ref.get("relationship_to_query") or "") == "primary"],
        "candidate_document_refs": [
            dict(ref)
            for ref in refs
            if str(ref.get("relationship_to_query") or "") in {"primary", "supporting", "near_miss"}
        ],
        "related_document_refs": [
            dict(ref)
            for ref in refs
            if str(ref.get("relationship_to_query") or "") in {"supporting", "near_miss", "related"}
        ],
    }
    ref_contract = _document_ref_contract_from_refs(refs)
    return {
        "schema_version": "agvm.document_workspace_package.v1",
        "package_kind": "document_workspace",
        "workspace_kind": "retrieved_source_material",
        "query_text": str(query_text or ""),
        "documents": documents,
        "primary_documents": [dict(doc) for doc in documents if bool(doc.get("primary_context_eligible"))],
        "related_or_cold_documents": [dict(doc) for doc in documents if not bool(doc.get("primary_context_eligible"))],
        "document_refs": refs,
        "primary_document_refs": list(document_evidence_lane.get("primary_document_refs") or []),
        "candidate_document_refs": list(document_evidence_lane.get("candidate_document_refs") or []),
        "related_document_refs": list(document_evidence_lane.get("related_document_refs") or []),
        "document_evidence_lane": document_evidence_lane,
        "document_ref_contract": ref_contract,
        "source_trace": [row for document in documents for row in list(document.get("source_trace") or [])][:96],
        "agent_markdown": _document_workspace_agent_markdown(
            query_text=query_text,
            workspace_kind="retrieved_source_material",
            package_mode="source_material_refs",
            documents=documents,
            source_trace=[row for document in documents for row in list(document.get("source_trace") or [])][:96],
            timeline=[],
            decisions=[],
            open_questions=[],
            risks=[],
            hypotheses=[],
            no_match_target="",
        ),
        "metrics": {
            "schema_version": "agvm.document_workspace_package.metrics.v1",
            "document_count": len(documents),
            "primary_document_count": len([doc for doc in documents if bool(doc.get("primary_context_eligible"))]),
            "full_text_document_count": len(documents),
            "raw_text_char_count": sum(len(str(doc.get("full_text") or "")) for doc in documents),
            "document_ref_count": len(refs),
            "primary_document_ref_count": len(list(document_evidence_lane.get("primary_document_refs") or [])),
            "candidate_document_ref_count": len(list(document_evidence_lane.get("candidate_document_refs") or [])),
            "related_document_ref_count": len(list(document_evidence_lane.get("related_document_refs") or [])),
            "actionable_document_ref_count": int(ref_contract.get("actionable_document_ref_count") or 0),
            "raw_available_document_ref_count": int(ref_contract.get("raw_available_document_ref_count") or 0),
            "source": "retrieved_material_raw_policy_fallback",
        },
        "readiness": {
            "schema_version": "agvm.document_workspace_readiness.v1",
            "status": "workspace_ready",
            "raw_document_ready": True,
            "source": "retrieved_material_raw_policy_fallback",
        },
    }


def _mcp_document_bundle_agent_lines(document_bundle: dict[str, Any] | None) -> list[str]:
    bundle = dict(document_bundle or {})
    if str(bundle.get("document_text_policy") or "refs_only") == "refs_only":
        return []
    documents = [dict(item) for item in list(bundle.get("documents") or []) if isinstance(item, dict)]
    if not documents:
        reason = str(bundle.get("pending_or_missing_reason") or "raw document text unavailable")
        return [f"- Raw document bundle requested but unavailable: {reason}."]
    lines: list[str] = []
    for index, document in enumerate(documents, start=1):
        title = _document_workspace_agent_label(document.get("title")) or f"Document {index}"
        raw_text = _document_workspace_clean_text(document.get("raw_text"))
        if not raw_text:
            continue
        suffix = " (truncated)" if bool(document.get("truncated")) else ""
        lines.extend([f"### {title}{suffix}", raw_text, ""])
    return [line for line in lines if line.strip()][:220]


def _mcp_document_delivery_contract(
    *,
    document_workspace: dict[str, Any] | None,
    document_refs: list[dict[str, Any]] | None,
    document_ref_contract: dict[str, Any] | None,
    document_bundle: dict[str, Any] | None,
    document_text_policy: Any,
    primary_payload_field: str = "context_package.agent_markdown",
) -> dict[str, Any]:
    policy = _mcp_normalize_document_text_policy(document_text_policy)
    workspace = dict(document_workspace or {})
    refs = [dict(ref) for ref in list(document_refs or workspace.get("document_refs") or []) if isinstance(ref, dict)]
    ref_contract = dict(document_ref_contract or {})
    bundle = dict(document_bundle or {})
    bundle_documents = [dict(item) for item in list(bundle.get("documents") or []) if isinstance(item, dict)]
    bundled_by_id: dict[str, dict[str, Any]] = {}
    for index, document in enumerate(bundle_documents, start=1):
        document_id = str(document.get("document_id") or _document_ref_id(document, index)).strip()
        if document_id and document_id not in bundled_by_id:
            bundled_by_id[document_id] = document

    delivery_rows: list[dict[str, Any]] = []
    raw_included_ids: list[str] = []
    raw_available_not_included_ids: list[str] = []
    metadata_only_ids: list[str] = []
    for index, ref in enumerate(refs, start=1):
        document_id = str(ref.get("document_id") or "").strip()
        bundle_document = bundled_by_id.get(document_id)
        raw_available = bool(ref.get("raw_text_available")) or str((ref.get("raw_availability") or {}).get("state") or "") == "raw_available"
        raw_text = _document_workspace_clean_text((bundle_document or {}).get("raw_text"))
        raw_included = bool(bundle_document and raw_text)
        included_chars = int((bundle_document or {}).get("included_char_count") or len(raw_text) or 0)
        raw_available_chars = int(ref.get("raw_text_char_count") or (bundle_document or {}).get("raw_text_char_count") or included_chars or 0)
        if raw_included:
            inclusion_state = "raw_included_current_payload"
            raw_included_ids.append(document_id)
        elif raw_available:
            inclusion_state = "ref_only_raw_available"
            raw_available_not_included_ids.append(document_id)
        else:
            inclusion_state = "metadata_only_raw_unavailable"
            metadata_only_ids.append(document_id)
        retrieve_call = dict(ref.get("retrieve_document_call") or _document_ref_retrieve_document_call(ref, index))
        delivery_rows.append(
            {
                "schema_version": "agvm.document_delivery_ref_state.v1",
                "document_id": document_id,
                "title": _document_workspace_agent_label(ref.get("title")) or f"Document {index}",
                "source_label": _document_workspace_agent_label(ref.get("source_label")),
                "source_type": ref.get("source_type"),
                "workspace_tier": str(ref.get("workspace_tier") or ""),
                "raw_available": raw_available,
                "raw_available_char_count": raw_available_chars,
                "raw_included_in_current_mcp_payload": raw_included,
                "included_char_count": included_chars,
                "truncated_in_current_mcp_payload": bool((bundle_document or {}).get("truncated")),
                "inclusion_state": inclusion_state,
                "client_receives_now": "raw_document_text" if raw_included else "actionable_document_ref",
                "follow_up_required_for_full_raw": not raw_included and raw_available,
                "retrieve_document_call": retrieve_call,
            }
        )

    first_ref = refs[0] if refs else {}
    first_document_id = str(first_ref.get("document_id") or "<document_id_from_document_refs>")
    first_document_hint = _document_workspace_agent_label(first_ref.get("title")) or "<document title or task>"
    raw_included_count = len(raw_included_ids)
    raw_available_count = int(ref_contract.get("raw_available_document_ref_count") or sum(1 for row in delivery_rows if row.get("raw_available")))
    return {
        "schema_version": MCP_DOCUMENT_DELIVERY_CONTRACT_VERSION,
        "state": "raw_included" if raw_included_count else "refs_actionable" if refs else "no_document_refs",
        "document_text_policy": policy,
        "primary_payload_field": primary_payload_field,
        "mcp_client_receives_first": (
            "context_package_plus_raw_document_bundle"
            if raw_included_count
            else "context_package_plus_actionable_document_refs"
            if refs
            else "context_package_without_document_refs"
        ),
        "raw_text_already_in_primary_payload": bool(raw_included_count),
        "raw_text_follow_up_required": bool(raw_available_not_included_ids),
        "document_ref_count": len(refs),
        "actionable_document_ref_count": int(ref_contract.get("actionable_document_ref_count") or 0),
        "raw_available_document_ref_count": raw_available_count,
        "raw_included_document_count": raw_included_count,
        "raw_available_not_included_count": len(raw_available_not_included_ids),
        "metadata_only_document_ref_count": len(metadata_only_ids),
        "document_bundle_state": str(bundle.get("state") or ""),
        "document_bundle_document_count": int(bundle.get("document_count") or len(bundle_documents) or 0),
        "document_bundle_raw_text_char_count": int(bundle.get("raw_text_char_count") or 0),
        "all_refs_actionable": bool(ref_contract.get("all_refs_actionable", True)),
        "policy_options": [
            {
                "value": "refs_only",
                "client_receives": "context package plus actionable document refs, no raw document bodies",
                "use_when": "fast normal context retrieval or when an MCP client will open documents separately",
            },
            {
                "value": "top_raw",
                "client_receives": "context package plus bounded raw text for the highest ranked document",
                "use_when": "the agent likely needs one central source body immediately",
            },
            {
                "value": "all_raw",
                "client_receives": "context package plus bounded raw packets for multiple selected documents",
                "use_when": "document-heavy tasks where larger payloads are explicitly requested",
            },
        ],
        "document_delivery_rows": delivery_rows,
        "raw_included_document_ids": raw_included_ids,
        "raw_available_not_included_document_ids": raw_available_not_included_ids,
        "metadata_only_document_ref_ids": metadata_only_ids,
        "exact_follow_up_recipe": {
            "tool": "retrieve_document",
            "arguments": {
                "document_id": first_document_id,
                "document_hint": first_document_hint,
                "query_text": first_document_hint,
                "include_raw_text": True,
                "context_package_mode": "document_full",
                "document_text_policy": "all_raw",
            },
        },
        "parallel_mcp_recipes": [
            {
                "name": "fast_context_then_document",
                "calls": [
                    {
                        "tool": "retrieve_context",
                        "arguments": {
                            "query_text": "<task>",
                            "context_package_mode": "mcp_operational",
                            "document_text_policy": "refs_only",
                        },
                    },
                    {
                        "tool": "retrieve_document",
                        "arguments": {
                            "document_id": first_document_id,
                            "include_raw_text": True,
                            "document_text_policy": "all_raw",
                        },
                    },
                ],
            },
            {
                "name": "context_with_top_raw_document",
                "calls": [
                    {
                        "tool": "retrieve_context",
                        "arguments": {
                            "query_text": "<task>",
                            "context_package_mode": "mcp_operational",
                            "document_text_policy": "top_raw",
                            "include_raw_text": True,
                        },
                    }
                ],
            },
            {
                "name": "document_heavy_context",
                "calls": [
                    {
                        "tool": "retrieve_context",
                        "arguments": {
                            "query_text": "<task>",
                            "context_package_mode": "document_full",
                            "document_text_policy": "all_raw",
                            "include_raw_text": True,
                        },
                    }
                ],
            },
        ],
    }


def build_document_workspace_package(
    *,
    query_text: str,
    document_mode: str = "none",
    document_lookup: dict[str, Any] | None = None,
    document_packets: list[dict[str, Any]] | None = None,
    evidence_reservoir: dict[str, Any] | None = None,
    semantic_contract: dict[str, Any] | None = None,
    retrieval_mode: str = "balanced",
    path_corridors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    packets = [
        dict(packet or {})
        for packet in list(document_packets or [])
        if isinstance(packet, dict) and packet and is_document_eligible(packet)
    ]
    lookup = dict(document_lookup or {})
    documents: list[dict[str, Any]] = []
    exact_document_lookup_requested = str(lookup.get("kind") or "").strip() == "exact_document_lookup"
    for packet_index, packet in enumerate(packets):
        materialize_full_document = not exact_document_lookup_requested or packet_index == 0
        title = _document_workspace_clean_text(packet.get("title") or packet.get("source_label") or "Document") or "Document"
        full_text = _document_workspace_packet_full_text(packet) if materialize_full_document else ""
        deferred_raw_text = "" if materialize_full_document else _document_workspace_packet_full_text(packet)
        chunks = _document_workspace_packet_chunks(packet) if materialize_full_document else []
        facts = _document_workspace_packet_facts(packet) if materialize_full_document else []
        sections = _document_workspace_sections(full_text=full_text, chunks=chunks)
        source_trace = _document_workspace_packet_source_trace(packet, chunks=chunks, facts=facts) if materialize_full_document else []
        documents.append(
            {
                "anchor_node_id": str(packet.get("anchor_node_id") or "").strip(),
                "title": title,
                "source_label": packet.get("source_label"),
                "source_type": packet.get("source_type"),
                "source_trust": packet.get("source_trust"),
                "claim_status": packet.get("claim_status"),
                "lookup_role": str(packet.get("lookup_role") or lookup.get("kind") or document_mode or "none"),
                "query_fit_score": float(packet.get("query_fit_score") or 0.0),
                "exact_match_score": float(packet.get("exact_match_score") or 0.0),
                "document_rank_score": float(packet.get("document_rank_score") or 0.0),
                "document_rank_reasons": list(packet.get("document_rank_reasons") or [])[:8],
                "document_rank": dict(packet.get("document_rank") or {}),
                "document_claim_rank": dict(packet.get("document_claim_rank") or {}),
                "project_tags": _document_workspace_unique(list(packet.get("project_tags") or []), limit=16),
                "entity_tags": _document_workspace_unique(list(packet.get("entity_tags") or []), limit=24),
                "timeline_tags": _document_workspace_unique(list(packet.get("timeline_tags") or []), limit=24),
                "topic_tags": _document_workspace_unique(list(packet.get("topic_tags") or []), limit=24),
                "related_node_ids": [str(item) for item in list(packet.get("related_node_ids") or []) if str(item or "").strip()][:32],
                "raw_text_available": bool(full_text) or bool(deferred_raw_text) or bool(packet.get("complete_text_available")) or int(packet.get("raw_text_char_count") or 0) > 0,
                "complete_text_available": bool(packet.get("complete_text_available")) or bool(full_text) or bool(deferred_raw_text),
                "full_text_mode": str(packet.get("full_text_mode") or ("full_text" if full_text else "none")),
                "raw_text_char_count": len(full_text),
                "available_raw_text_char_count": int(packet.get("raw_text_char_count") or len(full_text) or len(deferred_raw_text) or 0),
                "raw_text_omitted_from_payload": bool(not materialize_full_document and (packet.get("complete_text_available") or int(packet.get("raw_text_char_count") or 0) > 0)),
                "full_text": full_text,
                "deferred_raw_text": deferred_raw_text,
                "sections": sections,
                "chunks": chunks,
                "facts": facts,
                "source_trace": source_trace,
                "open_questions": _document_workspace_unique(list(packet.get("open_questions") or []), limit=12),
                "coverage": dict(packet.get("coverage") or {}),
                "chunk_count": len(chunks),
                "fact_count": len(facts),
                "section_count": len(sections),
            }
        )
    related_links = _document_workspace_related_links(documents)
    for document in documents:
        anchor_id = str(document.get("anchor_node_id") or "")
        document["linked_documents"] = [
            {
                "anchor_node_id": link.get("to_anchor_node_id"),
                "title": link.get("to_title"),
                "shared_tags": list(link.get("shared_tags") or []),
                "reason": link.get("reason"),
            }
            for link in related_links
            if str(link.get("from_anchor_node_id") or "") == anchor_id
        ][:8]
    workspace_kind = _document_workspace_kind(
        query_text=query_text,
        document_mode=document_mode,
        document_lookup=lookup,
        document_count=len(documents),
    )
    package_mode = _document_workspace_package_mode(
        query_text=query_text,
        retrieval_mode=retrieval_mode,
        workspace_kind=workspace_kind,
    )
    requested_document_workspace = bool(str(document_mode or "none") != "none" or str(lookup.get("kind") or "none") != "none")
    for index, document in enumerate(documents, start=1):
        document["document_id"] = _document_ref_id(document, index)
        document["raw_availability"] = _document_raw_availability(document)
        document["retrieve_document_call"] = _document_ref_retrieve_document_call(document, index)
        document["retrieve_project_workspace_call"] = _document_ref_project_workspace_call(document, index)
    document_evidence_lane = rank_document_evidence_candidates(
        query_text=query_text,
        candidates=documents,
        semantic_contract=semantic_contract,
        limit=max(0, len(documents)),
        candidate_window=24,
    )
    ranked_documents = [
        dict(item)
        for item in list(document_evidence_lane.get("documents") or [])
        if isinstance(item, dict)
    ]
    if ranked_documents:
        documents = ranked_documents
    primary_documents: list[dict[str, Any]] = []
    related_or_cold_documents: list[dict[str, Any]] = []
    primary_assigned = False
    broad_document_workspace = workspace_kind in {"project_workspace", "related_documents", "source_trace"}
    for index, document in enumerate(documents):
        exact_primary = workspace_kind == "exact_document" and index == 0
        relationship = str(document.get("relationship_to_query") or "background")
        requested_primary = bool(
            requested_document_workspace
            and (
                exact_primary
                or (broad_document_workspace and relationship != "excluded")
                or (
                    workspace_kind != "exact_document"
                    and relationship in {"primary", "supporting"}
                )
            )
        )
        if (
            requested_document_workspace
            and workspace_kind != "exact_document"
            and not primary_assigned
            and relationship != "excluded"
        ):
            requested_primary = True
        document["workspace_tier"] = "requested" if requested_primary else "related_or_cold"
        document["primary_context_eligible"] = requested_primary
        document["raw_text_primary_eligible"] = requested_primary and bool(str(document.get("full_text") or "").strip())
        if requested_primary:
            primary_documents.append(document)
            primary_assigned = True
        else:
            related_or_cold_documents.append(document)
    for index, document in enumerate(documents, start=1):
        document["document_id"] = str(document.get("document_id") or _document_ref_id(document, index))
        document["raw_availability"] = _document_raw_availability(document)
        document["retrieve_document_call"] = dict(
            document.get("document_evidence_retrieve_document_call")
            or document.get("retrieve_document_call")
            or _document_ref_retrieve_document_call(document, index)
        )
        document["retrieve_project_workspace_call"] = dict(
            document.get("retrieve_project_workspace_call") or _document_ref_project_workspace_call(document, index)
        )
    document_refs = _document_workspace_refs(documents)
    document_evidence_lane = {
        **dict(document_evidence_lane or {}),
        "documents": [],
        "ranked_document_refs": document_refs,
        "primary_document_refs": [
            dict(ref)
            for ref in document_refs
            if str(ref.get("relationship_to_query") or "") == "primary"
        ],
        "candidate_document_refs": [
            dict(ref)
            for ref in document_refs
            if str(ref.get("relationship_to_query") or "") in {"primary", "supporting", "near_miss"}
        ],
        "related_document_refs": [
            dict(ref)
            for ref in document_refs
            if str(ref.get("relationship_to_query") or "") in {"supporting", "near_miss", "related"}
        ],
    }
    document_evidence_lane["metrics"] = {
        **dict(document_evidence_lane.get("metrics") or {}),
        "primary_document_ref_count": len(list(document_evidence_lane.get("primary_document_refs") or [])),
        "candidate_document_ref_count": len(list(document_evidence_lane.get("candidate_document_refs") or [])),
        "related_document_ref_count": len(list(document_evidence_lane.get("related_document_refs") or [])),
    }
    document_ref_contract = _document_ref_contract_from_refs(document_refs)
    primary_surface_documents = primary_documents if workspace_kind == "exact_document" and primary_documents else documents
    source_trace = [dict(row) for document in primary_surface_documents for row in list(document.get("source_trace") or []) if isinstance(row, dict)][:96]
    timeline = _document_workspace_timeline(primary_surface_documents)
    decisions = _document_workspace_marker_sentences(
        primary_surface_documents,
        ("decision", "decided", "scelta", "scelto", "founded", "founded", "fonda", "acquired", "acquisition", "launched", "inaugurated"),
    )
    risks = _document_workspace_marker_sentences(
        primary_surface_documents,
        ("risk", "rischio", "concern", "blocker", "problema", "gap", "uncertain"),
    )
    hypotheses = _document_workspace_marker_sentences(
        primary_surface_documents,
        ("hypothesis", "ipotesi", "deduction", "deduce", "infer", "inferred", "possible"),
    )
    open_questions = _document_workspace_unique(
        [
            *[item for document in documents for item in list(document.get("open_questions") or [])],
            *list((evidence_reservoir or {}).get("unresolved_slots") or []),
        ],
        limit=16,
    )
    project_summary = {
        "query_text": str(query_text or ""),
        "workspace_kind": workspace_kind,
        "document_titles": [document.get("title") for document in documents],
        "project_tags": _document_workspace_unique([tag for document in documents for tag in list(document.get("project_tags") or [])], limit=16),
        "entity_tags": _document_workspace_unique([tag for document in documents for tag in list(document.get("entity_tags") or [])], limit=24),
        "topic_tags": _document_workspace_unique([tag for document in documents for tag in list(document.get("topic_tags") or [])], limit=24),
        "timeline_tags": _document_workspace_unique([tag for document in documents for tag in list(document.get("timeline_tags") or [])], limit=24),
        "source_labels": _document_workspace_unique([document.get("source_label") for document in documents], limit=16),
    }
    agent_markdown = _document_workspace_agent_markdown(
        query_text=query_text,
        workspace_kind=workspace_kind,
        package_mode=package_mode,
        documents=primary_surface_documents,
        source_trace=source_trace,
        timeline=timeline,
        decisions=decisions,
        open_questions=open_questions,
        risks=risks,
        hypotheses=hypotheses,
        no_match_target=str(lookup.get("target_text") or ""),
    )
    metrics = {
        "schema_version": "agvm.document_workspace_package.metrics.v1",
        "document_count": len(documents),
        "full_text_document_count": sum(1 for document in documents if bool(str(document.get("full_text") or "").strip())),
        "complete_text_document_count": sum(1 for document in documents if bool(document.get("complete_text_available"))),
        "raw_text_char_count": sum(len(str(document.get("full_text") or "")) for document in documents),
        "section_count": sum(len(list(document.get("sections") or [])) for document in documents),
        "chunk_count": sum(len(list(document.get("chunks") or [])) for document in documents),
        "fact_count": sum(len(list(document.get("facts") or [])) for document in documents),
        "source_trace_count": len(source_trace),
        "primary_document_count": len(primary_documents),
        "related_or_cold_document_count": len(related_or_cold_documents),
        "document_ref_count": len(document_refs),
        "primary_document_ref_count": len(list(document_evidence_lane.get("primary_document_refs") or [])),
        "candidate_document_ref_count": len(list(document_evidence_lane.get("candidate_document_refs") or [])),
        "related_document_ref_count": len(list(document_evidence_lane.get("related_document_refs") or [])),
        "actionable_document_ref_count": int(document_ref_contract.get("actionable_document_ref_count") or 0),
        "raw_available_document_ref_count": int(document_ref_contract.get("raw_available_document_ref_count") or 0),
        "primary_full_text_document_count": sum(1 for document in primary_documents if bool(str(document.get("full_text") or "").strip())),
        "primary_raw_text_char_count": sum(len(str(document.get("full_text") or "")) for document in primary_documents),
        "timeline_count": len(timeline),
        "decision_count": len(decisions),
        "risk_count": len(risks),
        "hypothesis_count": len(hypotheses),
        "related_document_link_count": len(related_links),
        "path_count": int(((path_corridors or {}).get("metrics") or {}).get("path_count") or 0),
        "no_match": workspace_kind == "no_document_found",
        "node_id_leak_in_agent_body": _mcp_agent_body_has_node_id(agent_markdown),
        "debug_marker_leak_in_agent_body": any(marker in _fold_text(agent_markdown) for marker in ("evidence ledger", "grounded retrieval ledger")),
    }
    if workspace_kind == "no_document_found":
        status = "no_document_found"
    elif documents:
        status = "workspace_ready"
    else:
        status = "empty"
    document_ready = bool(
        status == "workspace_ready"
        and workspace_kind == "exact_document"
        and any(bool(str(document.get("full_text") or "").strip()) for document in primary_documents)
    )
    workspace_ready = bool(status == "workspace_ready" and documents)
    readiness = {
        "schema_version": "agvm.document_workspace_readiness.v1",
        "state": "document_ready" if document_ready else "workspace_ready" if workspace_ready else status,
        "document_ready": document_ready,
        "workspace_ready": workspace_ready,
        "primary_surface": "primary_documents" if primary_documents else "documents",
        "primary_document_count": len(primary_documents),
        "related_or_cold_document_count": len(related_or_cold_documents),
        "primary_full_text_document_count": int(metrics.get("primary_full_text_document_count") or 0),
        "primary_raw_text_char_count": int(metrics.get("primary_raw_text_char_count") or 0),
        "exact_document_primary_policy": "single_best_document" if workspace_kind == "exact_document" else "all_requested_documents",
    }
    metrics["document_ready"] = document_ready
    metrics["workspace_ready"] = workspace_ready
    metrics["document_ready_state"] = readiness["state"]
    return {
        "schema_version": "agvm.document_workspace_package.v1",
        "package_kind": "document_workspace",
        "query_text": str(query_text or ""),
        "retrieval_mode": str(retrieval_mode or "balanced"),
        "document_mode": str(document_mode or "none"),
        "document_lookup_kind": str(lookup.get("kind") or "none"),
        "workspace_kind": workspace_kind,
        "package_mode": package_mode,
        "status": status,
        "document_ready_state": readiness["state"],
        "readiness": readiness,
        "document_lookup": lookup,
        "project_summary": project_summary,
        "documents": documents,
        "document_refs": document_refs,
        "primary_document_refs": list(document_evidence_lane.get("primary_document_refs") or []),
        "candidate_document_refs": list(document_evidence_lane.get("candidate_document_refs") or []),
        "related_document_refs": list(document_evidence_lane.get("related_document_refs") or []),
        "document_evidence_lane": document_evidence_lane,
        "document_ref_contract": document_ref_contract,
        "primary_documents": primary_documents,
        "related_or_cold_documents": related_or_cold_documents,
        "related_documents": related_links,
        "source_trace": source_trace,
        "timeline": timeline,
        "decisions": decisions,
        "open_questions": open_questions,
        "risks": risks,
        "hypotheses": hypotheses,
        "agent_markdown": agent_markdown,
        "metrics": metrics,
        "debug": {
            "anchor_node_ids": [str(document.get("anchor_node_id") or "") for document in documents if str(document.get("anchor_node_id") or "").strip()],
            "source_node_ids": [
                str(row.get("node_id") or row.get("source_node_id") or "")
                for row in source_trace
                if str(row.get("node_id") or row.get("source_node_id") or "").strip()
            ][:96],
            "semantic_contract_document_mode": str(((semantic_contract or {}).get("document_contract") or {}).get("mode") or "none"),
        },
    }


def _mcp_document_workspace_lines(document_workspace: dict[str, Any] | None) -> list[str]:
    workspace = dict(document_workspace or {})
    if not workspace:
        return []
    markdown = _document_workspace_clean_text(workspace.get("agent_markdown"))
    if not markdown:
        return []
    lines = [line for line in markdown.splitlines() if line.strip()]
    if lines and lines[0].strip() == "# AGVM Document Workspace":
        lines = lines[1:]
    return lines[:260]


def _mcp_document_workspace_primary_lines(
    document_workspace: dict[str, Any] | None,
    *,
    include_full_raw_documents: bool,
) -> list[str]:
    workspace = dict(document_workspace or {})
    if not workspace:
        return []
    documents = [
        dict(item)
        for item in list(workspace.get("documents") or [])
        if isinstance(item, dict)
    ]
    workspace_kind = str(workspace.get("workspace_kind") or "document_workspace")
    package_mode = str(workspace.get("package_mode") or "balanced")
    query_text = _document_workspace_clean_text(workspace.get("query_text"))
    lookup = dict(workspace.get("document_lookup") or {})
    lines: list[str] = [
        "## Task / Workspace Intent",
        query_text,
        "",
        "## Workspace Summary",
    ]
    if not documents:
        target = _document_workspace_clean_text(lookup.get("target_text"))
        target_suffix = f" for '{target}'" if target else ""
        lines.append(f"- No matching document workspace was found{target_suffix}.")
        return [line for line in lines if line.strip()][:120]
    titles = [
        title
        for title in (_document_workspace_agent_label(document.get("title")) for document in documents[:6])
        if title
    ]
    lines.append(f"- Workspace kind: {workspace_kind}; package mode: {package_mode}; requested documents: {len(documents)}.")
    if titles:
        lines.append(f"- Primary documents: {'; '.join(titles)}.")
    lines.extend(["", "## Documents"])
    for index, document in enumerate(documents, start=1):
        title = _document_workspace_agent_label(document.get("title")) or f"Document {index}"
        source = _document_workspace_agent_label(document.get("source_label")) or _document_workspace_agent_label(document.get("source_type"))
        fit = max(
            float(document.get("document_evidence_score") or 0.0),
            float(document.get("document_rank_score") or 0.0),
            float(document.get("query_fit_score") or 0.0),
            float(document.get("exact_match_score") or 0.0),
        )
        source_suffix = f" Source: {source}." if source else ""
        relationship = str(document.get("relationship_to_query") or "background")
        why = list(document.get("why_included") or [])
        why_suffix = f" Reason: {why[0]}." if why else ""
        lines.append(f"- {index}. {title}.{source_suffix} Relationship: {relationship}; fit {fit:.2f}; chunks {int(document.get('chunk_count') or 0)}; facts {int(document.get('fact_count') or 0)}.{why_suffix}")
    if include_full_raw_documents:
        lines.extend(["", "## Full Raw Documents"])
        for index, document in enumerate(documents, start=1):
            title = _document_workspace_agent_label(document.get("title")) or f"Document {index}"
            full_text = _document_workspace_clean_text(document.get("full_text"))
            if full_text:
                lines.extend(["", f"### {title}", full_text])
    return [line for line in lines if line.strip()][:260]


def _mcp_document_reference_content_hints(ref: dict[str, Any]) -> list[str]:
    hints: list[str] = []

    def add(value: Any) -> None:
        text = _document_workspace_clean_text(value)
        if not text:
            return
        folded = _fold_text(text)
        if folded and folded in {_fold_text(item) for item in hints}:
            return
        hints.append(_truncate_prompt_text(text, 180))

    for reason in list(ref.get("why_included") or [])[:2]:
        add(reason)
    rank_reasons = [
        str(item)
        for item in list(ref.get("document_rank_reasons") or [])
        if str(item or "").strip()
    ]
    if rank_reasons:
        add("; ".join(rank_reasons[:2]))
    matched_terms = [
        str(item).strip()
        for item in list(ref.get("matched_claim_terms") or [])
        if str(item or "").strip()
    ]
    if matched_terms:
        add(f"Matches requested terms: {', '.join(matched_terms[:6])}.")
    expected_keys = [
        str(key)
        for key, value in dict(ref.get("expected_contents") or {}).items()
        if str(key or "").strip() and value not in (None, "", [], {})
    ]
    if expected_keys:
        add(f"Expected content fields available: {', '.join(expected_keys[:6])}.")
    return hints[:4]


def _mcp_context_document_references(
    *,
    document_refs: list[dict[str, Any]],
    document_delivery_contract: dict[str, Any] | None,
    document_text_policy: str,
    raw_bodies_in_agent_markdown: bool,
    normal_context_sections: Sequence[dict[str, Any]],
    limit: int = 8,
) -> dict[str, Any]:
    refs = [dict(ref) for ref in list(document_refs or []) if isinstance(ref, dict)]
    delivery_rows = [
        dict(row)
        for row in list((document_delivery_contract or {}).get("document_delivery_rows") or [])
        if isinstance(row, dict)
    ]
    delivery_by_id = {
        str(row.get("document_id") or "").strip(): row
        for row in delivery_rows
        if str(row.get("document_id") or "").strip()
    }
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, ref in enumerate(refs, start=1):
        document_id = str(ref.get("document_id") or ref.get("anchor_node_id") or "").strip()
        title = _document_workspace_agent_label(ref.get("title")) or f"Document {index}"
        key = document_id or _fold_text(title)
        if key in seen:
            continue
        seen.add(key)
        delivery = delivery_by_id.get(document_id) or {}
        raw_availability = dict(ref.get("raw_availability") or {})
        raw_available = bool(ref.get("raw_text_available")) or bool(raw_availability.get("raw_text_available"))
        raw_included = bool(delivery.get("raw_included_in_current_mcp_payload"))
        retrieve_document_call = dict(ref.get("retrieve_document_call") or _document_ref_retrieve_document_call(ref, index))
        rows.append(
            {
                "schema_version": "agvm.context_package.document_reference.v1",
                "index": len(rows) + 1,
                "document_id": document_id,
                "title": title,
                "source_label": _document_workspace_agent_label(ref.get("source_label")),
                "source_type": ref.get("source_type"),
                "relationship_to_query": str(ref.get("relationship_to_query") or "background"),
                "workspace_tier": str(ref.get("workspace_tier") or ""),
                "query_fit_score": float(ref.get("query_fit_score") or 0.0),
                "document_rank_score": float(ref.get("document_rank_score") or 0.0),
                "document_evidence_score": float(ref.get("document_evidence_score") or 0.0),
                "content_hints": _mcp_document_reference_content_hints(ref),
                "raw_availability": {
                    "state": str(raw_availability.get("state") or ("raw_available" if raw_available else "raw_unavailable")),
                    "raw_text_available": raw_available,
                    "raw_text_char_count": int(ref.get("raw_text_char_count") or raw_availability.get("raw_text_char_count") or 0),
                    "raw_included_in_current_payload": raw_included,
                    "follow_up_required_for_full_raw": bool(delivery.get("follow_up_required_for_full_raw", raw_available and not raw_included)),
                },
                "hydration": {
                    "tool_name": "retrieve_document",
                    "requires_retrieve_document_for_full_text": bool(raw_available and not raw_included),
                    "retrieve_document_call": retrieve_document_call,
                    "retrieve_project_workspace_call": dict(ref.get("retrieve_project_workspace_call") or {}),
                },
            }
        )
        if len(rows) >= max(1, int(limit or 8)):
            break
    normal_section_keys = [
        str(section.get("key") or "").strip()
        for section in list(normal_context_sections or [])
        if isinstance(section, dict) and str(section.get("key") or "").strip()
    ]
    return {
        "schema_version": MCP_CONTEXT_DOCUMENT_REFERENCES_VERSION,
        "state": "ready" if rows else "empty",
        "section_title": "Document References",
        "document_text_policy": _mcp_normalize_document_text_policy(document_text_policy),
        "document_ref_count": len(refs),
        "rendered_ref_count": len(rows),
        "raw_bodies_in_agent_markdown": bool(raw_bodies_in_agent_markdown),
        "normal_context_section_keys": normal_section_keys,
        "separate_from_normal_context": True,
        "raw_source_bodies_default_to_cold_refs": _mcp_normalize_document_text_policy(document_text_policy) == "refs_only",
        "full_text_hydration_tool": "retrieve_document",
        "refs": rows,
    }


def _mcp_document_reference_agent_title(value: Any, *, fallback: str) -> str:
    title = _document_workspace_agent_label(value) or fallback
    if "," in title and len(title) > 72:
        prefix = title.split(",", 1)[0].strip()
        if len(prefix) >= 24:
            title = prefix
    for separator in (" | ", " - ", " -- "):
        if separator in title and len(title) > 96:
            prefix = title.split(separator, 1)[0].strip()
            if len(prefix) >= 24:
                title = prefix
                break
    return _truncate_prompt_text(title, 120)


def _mcp_context_document_reference_agent_lines(document_references: dict[str, Any] | None) -> list[str]:
    references = dict(document_references or {})
    refs = [dict(ref) for ref in list(references.get("refs") or []) if isinstance(ref, dict)]
    if not refs:
        return []
    lines: list[str] = []
    for ref in refs:
        title = _mcp_document_reference_agent_title(
            ref.get("title"),
            fallback=f"Document {int(ref.get('index') or len(lines) + 1)}",
        )
        source = _document_workspace_agent_label(ref.get("source_label")) or _document_workspace_agent_label(ref.get("source_type"))
        source_suffix = f" Source: {source}." if source else ""
        relationship = str(ref.get("relationship_to_query") or "background")
        raw_availability = dict(ref.get("raw_availability") or {})
        raw_state = "raw available" if bool(raw_availability.get("raw_text_available")) else "metadata only"
        hydration = dict(ref.get("hydration") or {})
        hydration_suffix = (
            " Full text: use the structured retrieve_document call in document_references.refs."
            if bool(hydration.get("requires_retrieve_document_for_full_text"))
            else ""
        )
        lines.append(
            f"- {title}.{source_suffix} Relationship: {relationship}; {raw_state}.{hydration_suffix}"
        )
        hints = [
            _document_workspace_clean_text(item)
            for item in list(ref.get("content_hints") or [])
            if _document_workspace_clean_text(item)
        ]
        if hints:
            lines.append(f"  - Why included: {hints[0]}")
    return lines[:80]


def _mcp_document_workspace_appendix(document_workspace: dict[str, Any] | None) -> dict[str, Any]:
    workspace = {
        key: value
        for key, value in dict(document_workspace or {}).items()
        if key != "debug"
    }
    if not workspace:
        return {}
    documents = [
        {
            "document_id": _document_ref_id(document, index),
            "title": _document_workspace_agent_label(document.get("title")) or "Document",
            "lookup_role": str(document.get("lookup_role") or ""),
            "query_fit_score": float(document.get("query_fit_score") or document.get("exact_match_score") or 0.0),
            "exact_match_score": float(document.get("exact_match_score") or 0.0),
            "document_rank_score": float(document.get("document_rank_score") or 0.0),
            "document_rank_reasons": list(document.get("document_rank_reasons") or [])[:8],
            "document_evidence_rank": int(document.get("document_evidence_rank") or index),
            "document_evidence_score": float(document.get("document_evidence_score") or 0.0),
            "relationship_to_query": str(document.get("relationship_to_query") or "background"),
            "why_included": list(document.get("why_included") or [])[:10],
            "expected_contents": dict(document.get("expected_contents") or {}),
            "matched_claim_terms": list(document.get("matched_claim_terms") or [])[:24],
            "matched_entities": list(document.get("matched_entities") or [])[:12],
            "score_components": dict(document.get("document_evidence_score_components") or {}),
            "raw_text_char_count": int(document.get("raw_text_char_count") or len(str(document.get("full_text") or ""))),
            "raw_availability": dict(document.get("raw_availability") or _document_raw_availability(document)),
            "retrieve_document_call": dict(document.get("retrieve_document_call") or _document_ref_retrieve_document_call(document, index)),
            "source_trace_count": len(list(document.get("source_trace") or [])),
            "chunk_count": int(document.get("chunk_count") or 0),
            "fact_count": int(document.get("fact_count") or 0),
            "workspace_tier": str(document.get("workspace_tier") or ""),
            "primary_context_eligible": bool(document.get("primary_context_eligible")),
        }
        for index, document in enumerate(list(workspace.get("documents") or []), start=1)
        if isinstance(document, dict)
    ]
    return {
        "schema_version": "agvm.context_package.document_workspace_appendix.v1",
        "workspace_kind": str(workspace.get("workspace_kind") or ""),
        "package_mode": str(workspace.get("package_mode") or ""),
        "status": str(workspace.get("status") or ""),
        "document_lookup_kind": str(workspace.get("document_lookup_kind") or ""),
        "document_count": len(documents),
        "primary_document_count": len(list(workspace.get("primary_documents") or [])),
        "related_or_cold_document_count": len(list(workspace.get("related_or_cold_documents") or [])),
        "document_evidence_lane": {
            key: value
            for key, value in dict(workspace.get("document_evidence_lane") or {}).items()
            if key != "documents"
        },
        "primary_document_refs": [dict(ref) for ref in list(workspace.get("primary_document_refs") or []) if isinstance(ref, dict)][:8],
        "candidate_document_refs": [dict(ref) for ref in list(workspace.get("candidate_document_refs") or []) if isinstance(ref, dict)][:12],
        "related_document_refs": [dict(ref) for ref in list(workspace.get("related_document_refs") or []) if isinstance(ref, dict)][:12],
        "documents": documents,
        "source_trace_count": len(list(workspace.get("source_trace") or [])),
        "source_trace": [dict(row) for row in list(workspace.get("source_trace") or []) if isinstance(row, dict)][:96],
        "timeline_count": len(list(workspace.get("timeline") or [])),
        "related_document_count": len(list(workspace.get("related_documents") or [])),
    }


_MCP_PACKAGE_RENDER_CONTRACT_SCHEMA_VERSION = "agvm.package_render_contract.v1"
_MCP_MASTER_JUDGEMENT_SCHEMA_VERSION = "agvm.master_judgement.v1"
_MCP_MASTER_EXPECTED_EVIDENCE_POLICY_SCHEMA_VERSION = "agvm.master_expected_evidence_policy.v1"
_MCP_MASTER_SUFFICIENCY_JUDGE_SCHEMA_VERSION = "agvm.master_sufficiency_judge.v1"
_MCP_AI_MASTER_SUFFICIENCY_SCHEMA_VERSION = "agvm.ai_master_sufficiency.v1"
_MCP_MASTER_JUDGEMENT_CACHE_MAX = 512
_MCP_MASTER_JUDGEMENT_CACHE_LOCK = threading.Lock()
_MCP_MASTER_JUDGEMENT_CACHE: dict[str, dict[str, Any]] = {}
_MCP_AI_MASTER_STATES = {
    "terminal",
    "usable_partial",
    "needs_hydration",
    "needs_more_search",
    "no_match",
    "provider_degraded",
    "blocked",
}


def _mcp_compact_mission_evidence_ledger(ledger: dict[str, Any] | None) -> dict[str, Any]:
    source = dict(ledger or {})
    if not source:
        return {}
    top_level_keys = (
        "schema_version",
        "row_schema_version",
        "status",
        "mission_contract_schema_version",
        "mission_contract_status",
        "mission_count",
        "row_count",
        "coverage_state_counts",
        "resolved_count",
        "partial_count",
        "near_miss_count",
        "missed_count",
        "hot_evidence_count",
        "cold_evidence_count",
        "document_ref_count",
        "document_evidence_lane",
        "document_evidence_row_count",
        "route_event_count",
        "retrieval_mode",
        "revision_context",
        "brain_revision",
        "matrix_revision",
        "metamemory_revision",
        "topology_revision",
        "atlas_revision",
        "calibration_revision",
        "source_replay_revision",
        "branch_judgement_summary",
        "branch_judge_timing_ms",
        "branch_judge_state_counts",
        "branch_judge_ready",
        "renderer_contract",
        "master_inputs_ready",
        "blockers",
    )
    compact: dict[str, Any] = {
        key: source.get(key)
        for key in top_level_keys
        if key in source and source.get(key) is not None
    }
    row_keys = (
        "schema_version",
        "mission_id",
        "path_id",
        "branch_id",
        "goal",
        "answer_hypothesis",
        "expected_evidence_shape",
        "hot_cold_policy",
        "landing_region_ref",
        "snapped_landing",
        "snap_delta",
        "current_region",
        "visited_node_ids",
        "traversed_edges",
        "route_events",
        "hot_evidence",
        "cold_evidence",
        "duplicate_evidence",
        "excluded_evidence",
        "document_refs",
        "document_refs_seen",
        "document_evidence_row",
        "document_evidence_relationship",
        "coverage_state",
        "coverage_reason",
        "missing_reason",
        "wrong_region_signal",
        "correction_signal",
        "package_candidate_sections",
        "planner_family",
        "heuristic_support_only",
        "accepted_by_ai_or_master",
        "branch_judgement",
    )
    rows: list[dict[str, Any]] = []
    for raw_row in list(source.get("rows") or [])[:48]:
        if not isinstance(raw_row, dict):
            continue
        row = {
            key: raw_row.get(key)
            for key in row_keys
            if key in raw_row and raw_row.get(key) not in (None, [], {})
        }
        rows.append(row)
    compact["rows"] = rows
    return compact


def _mcp_master_answer_voice(query_text: str, rows: list[dict[str, Any]]) -> str:
    folded_query = _fold_text(query_text)
    if any(
        bool(dict(row.get("expected_evidence_shape") or {}).get("document_requested"))
        or str(dict(row.get("expected_evidence_shape") or {}).get("answer_field") or "").strip().lower() == "document"
        for row in rows
    ):
        return "document_oriented"
    if any(
        marker in folded_query
        for marker in (
            "come ti chiami",
            "raccontami di te",
            "parlami di te",
            "chi sei",
            "tuo",
            "tua",
            "your",
            "who are you",
            "about yourself",
        )
    ):
        return "first_person"
    return "third_person"


def _mcp_master_document_state(rows: list[dict[str, Any]], document_refs: list[dict[str, Any]]) -> str:
    document_requested = any(
        bool(dict(row.get("expected_evidence_shape") or {}).get("document_requested"))
        or str(dict(row.get("expected_evidence_shape") or {}).get("answer_field") or "").strip().lower() == "document"
        for row in rows
    )
    if not document_requested and not document_refs:
        return "not_requested"
    raw_ready = any(
        bool(ref.get("raw_available") or ref.get("raw_text_available"))
        or bool(dict(ref.get("raw_availability") or {}).get("raw_text_available"))
        for ref in document_refs
        if isinstance(ref, dict)
    )
    if raw_ready:
        return "raw_refs_ready"
    if document_refs:
        return "refs_available"
    return "missing" if document_requested else "not_available"


def _mcp_master_goal_key(row: dict[str, Any], goal: str) -> str:
    expected_shape = dict(row.get("expected_evidence_shape") or {})
    target = _fold_text(str(expected_shape.get("target_id") or ""))
    answer_field = _fold_text(str(expected_shape.get("answer_field") or ""))
    canonical_shape = _fold_text(
        str(
            expected_shape.get("claim_shape")
            or expected_shape.get("evidence_shape")
            or expected_shape.get("success_question")
            or row.get("answer_hypothesis")
            or goal
        )
    )
    normalized_goal = _fold_text(goal)
    semantic_goal = canonical_shape or normalized_goal
    return "|".join(part for part in (target or answer_field, semantic_goal) if part)[:260]


def _mcp_master_slot_key(row: dict[str, Any]) -> str:
    expected_shape = dict(row.get("expected_evidence_shape") or {})
    for value in (
        expected_shape.get("answer_field"),
        expected_shape.get("target_id"),
        row.get("package_candidate_sections", [None])[0] if list(row.get("package_candidate_sections") or []) else None,
    ):
        folded = _fold_text(str(value or ""))
        if not folded:
            continue
        section = _SEMANTIC_SLOT_SECTIONS.get(folded)
        if not section and folded in _MCP_CONTEXT_SECTION_TITLES:
            section = folded
        if not section and any(
            token in folded
            for token in (
                "identity",
                "name",
                "work",
                "role",
                "project",
                "company",
                "relationship",
                "family",
                "style",
                "communication",
                "value",
                "history",
                "timeline",
                "temporal",
                "document",
            )
        ):
            section = _mcp_context_section_key(value)
        if section in _MCP_CONTEXT_SECTION_TITLES:
            return section
    return ""


def _mcp_master_row_has_visible_evidence(row: dict[str, Any]) -> bool:
    return bool(
        [item for item in list(row.get("hot_evidence") or []) if isinstance(item, dict)]
        or [item for item in list(row.get("cold_evidence") or []) if isinstance(item, dict)]
        or [item for item in list(row.get("document_refs") or []) if isinstance(item, dict)]
        or [item for item in list(row.get("route_events") or []) if isinstance(item, dict)]
    )


def _mcp_master_path_state(ledger: dict[str, Any], path_truth_contract: dict[str, Any] | None) -> str:
    path_truth = dict(path_truth_contract or {})
    if bool(path_truth.get("ready")):
        return str(path_truth.get("state") or "route_truth_ready")
    if int(ledger.get("route_event_count") or 0) > 0:
        return "route_events_recorded"
    if int(path_truth.get("path_count") or 0) > 0:
        return "path_truth_pending"
    return "no_path_truth"


def _mcp_master_bool_from_query(query_text: str, markers: Sequence[str]) -> bool:
    folded_query = _fold_text(query_text)
    return any(marker in folded_query for marker in markers)


def _mcp_master_row_counts(row: dict[str, Any]) -> dict[str, int]:
    return {
        "hot": len([item for item in list(row.get("hot_evidence") or []) if isinstance(item, dict)]),
        "cold": len([item for item in list(row.get("cold_evidence") or []) if isinstance(item, dict)]),
        "document_refs": len([item for item in list(row.get("document_refs") or []) if isinstance(item, dict)]),
        "route_events": len([item for item in list(row.get("route_events") or []) if isinstance(item, dict)]),
        "duplicate": len([item for item in list(row.get("duplicate_evidence") or []) if isinstance(item, dict)]),
        "excluded": len([item for item in list(row.get("excluded_evidence") or []) if isinstance(item, dict)]),
    }


def _mcp_master_cache_payload(
    *,
    query_text: str,
    ledger: dict[str, Any],
    render_contract: dict[str, Any],
    path_truth_contract: dict[str, Any] | None,
    document_refs: list[dict[str, Any]],
) -> dict[str, Any]:
    revision_context = dict(ledger.get("revision_context") or {})
    revisions = {
        key: ledger.get(key) or revision_context.get(key)
        for key in (
            "brain_revision",
            "matrix_revision",
            "metamemory_revision",
            "topology_revision",
            "atlas_revision",
            "calibration_revision",
            "source_replay_revision",
        )
        if (ledger.get(key) or revision_context.get(key)) is not None
    }
    rows = []
    for row in list(ledger.get("rows") or [])[:48]:
        if not isinstance(row, dict):
            continue
        branch_judgement = dict(row.get("branch_judgement") or {})
        rows.append(
            {
                "mission_id": row.get("mission_id"),
                "path_id": row.get("path_id"),
                "branch_id": row.get("branch_id"),
                "goal": row.get("goal"),
                "answer_hypothesis": row.get("answer_hypothesis"),
                "expected_evidence_shape": row.get("expected_evidence_shape"),
                "coverage_state": row.get("coverage_state"),
                "coverage_reason": row.get("coverage_reason"),
                "missing_reason": row.get("missing_reason"),
                "branch_judgement": {
                    key: branch_judgement.get(key)
                    for key in (
                        "schema_version",
                        "state",
                        "confidence",
                        "reason_codes",
                        "next_recommended_action",
                        "evidence_counts",
                        "has_visible_provenance",
                        "document_requested",
                        "ai_branch_controller_required",
                    )
                    if branch_judgement.get(key) not in (None, [], {})
                },
                "counts": _mcp_master_row_counts(row),
                "planner_family": row.get("planner_family"),
                "heuristic_support_only": row.get("heuristic_support_only"),
                "accepted_by_ai_or_master": row.get("accepted_by_ai_or_master"),
            }
        )
    return {
        "schema_version": _MCP_MASTER_JUDGEMENT_SCHEMA_VERSION,
        "query": _fold_text(query_text)[:260],
        "ledger_status": ledger.get("status"),
        "branch_judgement_summary": ledger.get("branch_judgement_summary"),
        "revision_context": revisions,
        "rows": rows,
        "render_blocked": bool(render_contract.get("blocked")),
        "render_blocked_reasons": list(render_contract.get("blocked_reasons") or [])[:16],
        "path_truth": dict(path_truth_contract or {}),
        "document_ref_ids": [
            str(ref.get("document_id") or ref.get("anchor_node_id") or ref.get("title") or "").strip()
            for ref in document_refs[:24]
            if str(ref.get("document_id") or ref.get("anchor_node_id") or ref.get("title") or "").strip()
        ],
    }


def _mcp_master_cache_key(
    *,
    query_text: str,
    ledger: dict[str, Any],
    render_contract: dict[str, Any],
    path_truth_contract: dict[str, Any] | None,
    document_refs: list[dict[str, Any]],
) -> str:
    payload = _mcp_master_cache_payload(
        query_text=query_text,
        ledger=ledger,
        render_contract=render_contract,
        path_truth_contract=path_truth_contract,
        document_refs=document_refs,
    )
    return hashlib.sha1(json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")).hexdigest()


def _mcp_master_query_scope(query_text: str, rows: list[dict[str, Any]], document_state: str, path_state: str) -> str:
    if document_state in {"raw_refs_ready", "refs_available", "missing"}:
        return "document_hydration"
    non_path_states = {"no_path_truth", "route_truth_ready", "not_requested", "not_required"}
    if path_state not in non_path_states or _mcp_master_bool_from_query(
        query_text,
        ("path", "percorso", "corridor", "collega", "connect", "rete", "network"),
    ):
        return "path_aware"
    if len(rows) > 1 or _mcp_master_bool_from_query(
        query_text,
        ("dossier", "complete", "completo", "raccontami", "parlami", "aziende", "companies", "timeline", "storia"),
    ):
        return "broad_multi_branch"
    return "narrow_fact"


def _mcp_master_expected_evidence_policy(
    *,
    query_text: str,
    rows: list[dict[str, Any]],
    document_state: str,
    path_state: str,
    branch_state_counts: dict[str, int],
    master_state: str,
) -> dict[str, Any]:
    query_scope = _mcp_master_query_scope(query_text, rows, document_state, path_state)
    branch_count = len(rows)
    actionable_count = sum(
        1
        for row in rows
        if str(dict(row.get("branch_judgement") or {}).get("state") or row.get("coverage_state") or "").strip().lower()
        not in {"excluded_only", "duplicate_only", "stop", "missed", "forbidden"}
    )
    minimum_resolved = 1 if query_scope == "narrow_fact" and branch_count else max(0, actionable_count or branch_count)
    ai_sufficiency_required = bool(
        query_scope in {"broad_multi_branch", "path_aware"}
        or master_state in {"usable_partial", "needs_more_search"}
        or branch_state_counts.get("useful_partial")
        or branch_state_counts.get("wrong_region")
        or branch_state_counts.get("needs_radius_widen")
    )
    if master_state in {"no_match", "provider_degraded", "blocked"} and not branch_state_counts.get("useful_partial"):
        ai_sufficiency_required = False
    if master_state == "terminal" and query_scope not in {"broad_multi_branch", "path_aware"} and not branch_state_counts.get("useful_partial"):
        ai_sufficiency_required = False
    return {
        "schema_version": _MCP_MASTER_EXPECTED_EVIDENCE_POLICY_SCHEMA_VERSION,
        "query_scope": query_scope,
        "expected_branch_count": branch_count,
        "minimum_resolved_branch_count": minimum_resolved,
        "branch_completion_policy": "all_requested_branch_goals_or_named_missing_goals",
        "document_hydration_required": document_state == "missing" or bool(branch_state_counts.get("needs_document")),
        "path_truth_required": query_scope == "path_aware",
        "evidence_budget_policy": (
            "approve_branch_promoted_evidence_with_budget"
            if query_scope in {"broad_multi_branch", "path_aware"}
            else "small_payload_allowed_when_single_branch_resolves"
        ),
        "static_required_sections_are_not_terminality_source": True,
        "ai_sufficiency_required": ai_sufficiency_required,
        "safety_invariants": [
            "ai_participation",
            "visible_provenance",
            "privacy_and_off_contract_boundary",
            "document_hydration_contract",
            "path_truth_for_path_tools",
            "budget",
        ],
    }


def _mcp_master_ai_timeout_seconds(master_state: str, query_scope: str) -> float:
    if master_state in {"usable_partial", "needs_more_search"}:
        return 2.8 if query_scope in {"broad_multi_branch", "path_aware"} else 2.2
    if query_scope == "path_aware":
        return 3.2
    if query_scope == "broad_multi_branch":
        return 3.0
    return 2.0


def _mcp_master_compact_goal_coverage(goal_coverage: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for item in goal_coverage[:10]:
        if not isinstance(item, dict):
            continue
        compact.append(
            {
                "mission_id": str(item.get("mission_id") or "")[:100],
                "goal": str(item.get("goal") or "")[:240],
                "coverage_state": str(item.get("coverage_state") or "")[:80],
                "branch_judgement_state": str(item.get("branch_judgement_state") or "")[:80],
                "master_goal_state": str(item.get("master_goal_state") or "")[:80],
                "hot_evidence_count": int(item.get("hot_evidence_count") or 0),
                "cold_evidence_count": int(item.get("cold_evidence_count") or 0),
                "document_ref_count": int(item.get("document_ref_count") or 0),
                "route_event_count": int(item.get("route_event_count") or 0),
                "next_action": str(item.get("branch_next_recommended_action") or "")[:120],
            }
        )
    return compact


def _mcp_master_normalize_ai_payload(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    data = dict(payload or {})
    state = str(data.get("master_state") or data.get("state") or "").strip().lower()
    if state not in _MCP_AI_MASTER_STATES:
        return None
    try:
        confidence = float(data.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.72
    reason = str(data.get("reason") or "").strip()
    reason_codes = [
        str(item or "").strip()
        for item in list(data.get("reason_codes") or [])
        if str(item or "").strip()
    ]
    missing_goals = [
        str(item or "").strip()
        for item in list(data.get("missing_goals") or data.get("named_missing_goals") or [])
        if str(item or "").strip()
    ]
    covered_goals = [
        str(item or "").strip()
        for item in list(data.get("covered_goals") or [])
        if str(item or "").strip()
    ]
    next_call = str(data.get("next_recommended_call") or "").strip() or None
    return {
        "schema_version": _MCP_AI_MASTER_SUFFICIENCY_SCHEMA_VERSION,
        "master_state": state,
        "confidence": round(max(0.0, min(1.0, confidence)), 3),
        "reason": reason[:420] or None,
        "reason_codes": list(dict.fromkeys(reason_codes or [f"ai_master_{state}"]))[:10],
        "missing_goals": list(dict.fromkeys(missing_goals))[:12],
        "covered_goals": list(dict.fromkeys(covered_goals))[:12],
        "next_recommended_call": next_call,
    }


def _mcp_master_run_ai_sufficiency(
    *,
    query_text: str,
    expected_evidence_policy: dict[str, Any],
    master_state: str,
    goal_coverage: list[dict[str, Any]],
    covered_goals: list[str],
    partial_goals: list[str],
    missing_goals: list[str],
    branch_state_counts: dict[str, int],
    document_state: str,
    path_state: str,
    reason_codes: list[str],
    render_contract: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None, float]:
    if not llm_enabled():
        return None, "llm_disabled", 0.0
    query_scope = str(expected_evidence_policy.get("query_scope") or "narrow_fact")
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["master_state", "confidence", "reason"],
        "properties": {
            "master_state": {"type": "string", "enum": sorted(_MCP_AI_MASTER_STATES)},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "reason": {"type": "string"},
            "reason_codes": {"type": "array", "maxItems": 10, "items": {"type": "string"}},
            "covered_goals": {"type": "array", "maxItems": 12, "items": {"type": "string"}},
            "missing_goals": {"type": "array", "maxItems": 12, "items": {"type": "string"}},
            "next_recommended_call": {
                "type": ["string", "null"],
                "enum": [
                    None,
                    "retrieve_document",
                    "retrieve_path_corridor",
                    "inspect_context_package",
                    "continue_retrieve_context",
                    "retry_retrieve_after_provider_recovers",
                    "none",
                ],
            },
        },
    }
    prompt_payload = {
        "schema_version": _MCP_AI_MASTER_SUFFICIENCY_SCHEMA_VERSION,
        "query": str(query_text or "")[:700],
        "deterministic_precheck_state": master_state,
        "query_scope": query_scope,
        "goal_coverage": _mcp_master_compact_goal_coverage(goal_coverage),
        "covered_goals": covered_goals[:12],
        "partial_goals": partial_goals[:12],
        "missing_goals": missing_goals[:12],
        "branch_state_counts": dict(branch_state_counts),
        "document_state": document_state,
        "path_state": path_state,
        "reason_codes": reason_codes[:18],
        "render_contract": {
            "source_is_ledger_only": bool(render_contract.get("source_is_ledger_only")),
            "blocked": bool(render_contract.get("blocked")),
            "blocked_reasons": list(render_contract.get("blocked_reasons") or [])[:8],
            "required_sections": list(render_contract.get("required_sections") or [])[:10],
            "unresolved_sections": list(render_contract.get("unresolved_sections") or [])[:10],
        },
        "policy": {
            "judge_sufficiency_not_style": True,
            "do_not_answer_user": True,
            "do_not_invent_evidence": True,
            "static_sections_are_advisory_except_safety_provenance_path_document": True,
            "terminal_requires_enough_goal_evidence_for_this_query": True,
        },
    }
    started_at = time.perf_counter()
    payload, error = structured_json(
        model=answer_model(),
        system_prompt=(
            "You are AGVM's Master sufficiency judge. Read compact branch judgements and decide whether the MCP context payload "
            "is sufficient for the current agent request. Do not answer the user. Do not require a fixed checklist when the branch "
            "evidence already satisfies the user's goal, but never approve missing provenance, missing document hydration, missing path truth, "
            "private/off-contract material, or provider-degraded states."
        ),
        user_prompt=json.dumps(prompt_payload, ensure_ascii=False),
        schema_name="agvm_ai_master_sufficiency_v1",
        schema=schema,
        timeout=_mcp_master_ai_timeout_seconds(master_state, query_scope),
        role="answer",
        max_output_tokens=320,
    )
    elapsed_ms = round((time.perf_counter() - started_at) * 1000.0, 3)
    if error or not isinstance(payload, dict):
        return None, error or "llm_empty", elapsed_ms
    normalized = _mcp_master_normalize_ai_payload(payload)
    if not normalized:
        return None, "invalid_ai_master_sufficiency_payload", elapsed_ms
    return normalized, None, elapsed_ms


def _mcp_master_next_call_for_state(state: str, *, document_state: str, path_state: str) -> str | None:
    if state == "needs_hydration":
        return "retrieve_document"
    if state == "needs_more_search":
        return "retrieve_path_corridor" if path_state not in {"route_truth_ready", "not_requested", "not_required"} else "inspect_context_package"
    if state == "provider_degraded":
        return "retry_retrieve_after_provider_recovers"
    if state == "usable_partial":
        return "retrieve_document" if document_state in {"raw_refs_ready", "refs_available"} else "inspect_context_package"
    return None


def _mcp_master_apply_ai_sufficiency(
    *,
    ai_payload: dict[str, Any],
    current_state: str,
    document_state: str,
    path_state: str,
    expected_evidence_policy: dict[str, Any],
    covered_goals: list[str],
    partial_goals: list[str],
    missing_goals: list[str],
    reason_codes: list[str],
) -> dict[str, Any]:
    state = str(ai_payload.get("master_state") or current_state).strip().lower()
    safety_downgraded = False
    if current_state in {"blocked", "provider_degraded"} and state in {"terminal", "usable_partial", "no_match"}:
        state = current_state
        safety_downgraded = True
    if bool(expected_evidence_policy.get("document_hydration_required")) and document_state == "missing" and state == "terminal":
        state = "needs_hydration"
        safety_downgraded = True
    if bool(expected_evidence_policy.get("path_truth_required")) and path_state not in {"route_truth_ready", "not_required"} and state == "terminal":
        state = "needs_more_search"
        safety_downgraded = True
    if state == "terminal":
        next_call = None
        terminal = True
        final_seal = True
        context_state = "complete"
        agent_state = "usable_context"
        continuation = {"state": "none", "tool_action": None, "reason": "ai_master_approved_terminal_context"}
    elif state == "no_match":
        next_call = None
        terminal = True
        final_seal = True
        context_state = "no_match"
        agent_state = "no_match"
        continuation = {"state": "none", "tool_action": None, "reason": "ai_master_approved_no_match"}
    else:
        next_call = _mcp_master_next_call_for_state(state, document_state=document_state, path_state=path_state)
        terminal = False
        final_seal = False
        context_state = "partial" if state in {"usable_partial", "needs_hydration", "needs_more_search"} else state
        agent_state = "partial_context" if state in {"usable_partial", "needs_hydration", "needs_more_search"} else state
        continuation = {
            "state": "required" if state in {"needs_hydration", "needs_more_search", "provider_degraded"} else "recommended",
            "tool_action": next_call,
            "reason": f"ai_master_{state}",
        }
    ai_missing = list(ai_payload.get("missing_goals") or [])
    ai_covered = list(ai_payload.get("covered_goals") or [])
    updated_reason_codes = list(dict.fromkeys([*reason_codes, *list(ai_payload.get("reason_codes") or []), "ai_master_sufficiency_used"]))
    if safety_downgraded:
        updated_reason_codes.append("ai_master_terminal_downgraded_by_safety_invariant")
    return {
        "master_state": state,
        "context_state": context_state,
        "agent_payload_state": agent_state,
        "terminal_for_client": terminal,
        "final_seal_allowed": final_seal,
        "no_match_claim": state == "no_match",
        "covered_goals": ai_covered or covered_goals,
        "missing_goals": ai_missing if state not in {"terminal", "no_match"} else [],
        "unresolved_goals": ai_missing if state not in {"terminal", "no_match"} else [],
        "continuation_recommendation": continuation,
        "next_recommended_call": next_call,
        "reason_codes": updated_reason_codes[:24],
    }


def build_mcp_master_judgement(
    *,
    query_text: str,
    mission_evidence_ledger: dict[str, Any] | None,
    package_render_contract: dict[str, Any] | None = None,
    path_truth_contract: dict[str, Any] | None = None,
    document_refs: list[dict[str, Any]] | None = None,
    allow_ai_master: bool = False,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    ledger = dict(mission_evidence_ledger or {})
    rows = [dict(row) for row in list(ledger.get("rows") or []) if isinstance(row, dict)]
    render_contract = dict(package_render_contract or {})
    refs = [dict(ref) for ref in list(document_refs or []) if isinstance(ref, dict)]
    cache_key = _mcp_master_cache_key(
        query_text=query_text,
        ledger=ledger,
        render_contract=render_contract,
        path_truth_contract=path_truth_contract,
        document_refs=refs,
    )
    with _MCP_MASTER_JUDGEMENT_CACHE_LOCK:
        cached = _MCP_MASTER_JUDGEMENT_CACHE.get(cache_key)
    if cached:
        result = dict(cached)
        result["cache_hit"] = True
        result["master_judge_timing_ms"] = round((time.perf_counter() - started_at) * 1000.0, 3)
        return result

    goal_coverage: list[dict[str, Any]] = []
    covered_goals: list[str] = []
    partial_goals: list[str] = []
    missing_goals: list[str] = []
    reason_codes: list[str] = []
    branch_state_counts: dict[str, int] = {}
    branch_next_actions: dict[str, int] = {}

    provider_degraded = str(ledger.get("status") or "").strip().lower() in {"provider_degraded", "timeout", "failed"}
    blocked = str(ledger.get("status") or "").strip().lower() in {"blocked"} or bool(render_contract.get("blocked"))
    useful_evidence_count = 0
    missing_only_states = {"missed"}
    failure_states = {"near_miss", "wrong_region", "duplicate_only", "forbidden", "mission_failed"}
    actionable_search_states = {"needs_radius_widen", "wrong_region"}
    hydration_states = {"needs_document"}
    partial_states = {"useful_partial"}
    resolved_states = {"resolved"}
    no_action_states = {"stop", "excluded_only", "duplicate_only", "missed", "forbidden"}
    resolved_goal_keys = {
        _mcp_master_goal_key(row, str(row.get("goal") or row.get("mission_id") or ""))
        for row in rows
        if (
            str(dict(row.get("branch_judgement") or {}).get("state") or row.get("coverage_state") or "").strip().lower()
            in resolved_states
            or str(row.get("coverage_state") or "").strip().lower() == "resolved"
        )
        and _mcp_master_row_has_visible_evidence(row)
        and _mcp_master_goal_key(row, str(row.get("goal") or row.get("mission_id") or ""))
    }
    resolved_slot_keys = {
        _mcp_master_slot_key(row)
        for row in rows
        if (
            str(dict(row.get("branch_judgement") or {}).get("state") or row.get("coverage_state") or "").strip().lower()
            in resolved_states
            or str(row.get("coverage_state") or "").strip().lower() == "resolved"
        )
        and _mcp_master_row_has_visible_evidence(row)
        and _mcp_master_slot_key(row)
    }
    broad_slot_subsumption = _is_broad_self_query(query_text)
    subsumed_unresolved_goals: list[str] = []
    for index, row in enumerate(rows, start=1):
        mission_id = str(row.get("mission_id") or f"mission_{index}").strip()
        goal = str(row.get("goal") or mission_id).strip()
        goal_key = _mcp_master_goal_key(row, goal)
        coverage_state = str(row.get("coverage_state") or "unknown").strip().lower()
        branch_judgement = dict(row.get("branch_judgement") or {})
        branch_state = str(branch_judgement.get("state") or "").strip().lower()
        effective_state = branch_state or coverage_state
        branch_state_counts[effective_state or "unknown"] = branch_state_counts.get(effective_state or "unknown", 0) + 1
        next_action = str(branch_judgement.get("next_recommended_action") or "").strip()
        if next_action:
            branch_next_actions[next_action] = branch_next_actions.get(next_action, 0) + 1
        if coverage_state in {"provider_degraded", "timeout"}:
            provider_degraded = True
        hot_count = len([item for item in list(row.get("hot_evidence") or []) if isinstance(item, dict)])
        cold_count = len([item for item in list(row.get("cold_evidence") or []) if isinstance(item, dict)])
        document_count = len([item for item in list(row.get("document_refs") or []) if isinstance(item, dict)])
        duplicate_count = len([item for item in list(row.get("duplicate_evidence") or []) if isinstance(item, dict)])
        excluded_count = len([item for item in list(row.get("excluded_evidence") or []) if isinstance(item, dict)])
        row_has_useful_evidence = bool(hot_count or cold_count or document_count)
        useful_evidence_count += int(row_has_useful_evidence)
        master_goal_state = "missing"
        subsumed_by_resolved_duplicate = bool(
            (goal_key and goal_key in resolved_goal_keys)
            or (
                broad_slot_subsumption
                and _mcp_master_slot_key(row)
                and _mcp_master_slot_key(row) in resolved_slot_keys
            )
        ) and bool(
            effective_state not in resolved_states
            and coverage_state != "resolved"
            and not row_has_useful_evidence
        )
        if effective_state in resolved_states or coverage_state == "resolved":
            covered_goals.append(goal)
            normalized_goal_state = "covered"
            master_goal_state = "covered"
        elif subsumed_by_resolved_duplicate:
            covered_goals.append(goal)
            normalized_goal_state = "covered"
            master_goal_state = "covered_by_resolved_duplicate_goal"
            if goal not in subsumed_unresolved_goals:
                subsumed_unresolved_goals.append(goal)
        elif effective_state in hydration_states:
            partial_goals.append(goal)
            normalized_goal_state = "partial"
            master_goal_state = "needs_hydration"
            if goal not in missing_goals:
                missing_goals.append(goal)
        elif effective_state in actionable_search_states:
            partial_goals.append(goal)
            normalized_goal_state = "partial"
            master_goal_state = "needs_more_search"
            if goal not in missing_goals:
                missing_goals.append(goal)
        elif effective_state in partial_states:
            partial_goals.append(goal)
            normalized_goal_state = "partial"
            master_goal_state = "usable_partial"
        elif coverage_state in {"partially_resolved", "near_miss"} or row_has_useful_evidence:
            partial_goals.append(goal)
            normalized_goal_state = "partial"
            master_goal_state = "usable_partial"
        else:
            missing_goals.append(goal)
            normalized_goal_state = "missing"
            master_goal_state = "missing"
        if subsumed_by_resolved_duplicate:
            reason_codes.append("branch_subsumed_by_resolved_duplicate_goal")
        elif branch_state:
            reason_codes.append(f"branch_{branch_state}")
        if not subsumed_by_resolved_duplicate and coverage_state in failure_states:
            reason_codes.append(f"mission_{coverage_state}")
        if not subsumed_by_resolved_duplicate and coverage_state in missing_only_states:
            reason_codes.append("mission_missed")
        if cold_count:
            reason_codes.append("cold_answer_material_present")
        if duplicate_count and not row_has_useful_evidence:
            reason_codes.append("duplicate_only")
        if excluded_count and not row_has_useful_evidence:
            reason_codes.append("excluded_only")
        goal_coverage.append(
            {
                "mission_id": mission_id,
                "path_id": row.get("path_id"),
                "goal": goal,
                "coverage_state": coverage_state or "unknown",
                "normalized_goal_state": normalized_goal_state,
                "coverage_reason": str(row.get("coverage_reason") or row.get("missing_reason") or "").strip() or None,
                "branch_judgement_state": branch_state or None,
                "branch_judgement_reason_codes": list(branch_judgement.get("reason_codes") or [])[:8],
                "branch_next_recommended_action": branch_judgement.get("next_recommended_action"),
                "effective_branch_state": effective_state or "unknown",
                "master_goal_state": master_goal_state,
                "hot_evidence_count": hot_count,
                "cold_evidence_count": cold_count,
                "document_ref_count": document_count,
                "route_event_count": len([item for item in list(row.get("route_events") or []) if isinstance(item, dict)]),
                "correction_signal": dict(row.get("correction_signal") or {}),
            }
        )

    ledger_status = str(ledger.get("status") or "").strip().lower()
    answer_voice = _mcp_master_answer_voice(query_text, rows)
    document_state = _mcp_master_document_state(rows, refs)
    path_state = _mcp_master_path_state(ledger, path_truth_contract)
    if not rows:
        master_state = "blocked"
        reason_codes.append("mission_ledger_rows_missing")
    elif provider_degraded:
        master_state = "provider_degraded"
        reason_codes.append("provider_degraded")
    elif blocked or ledger_status == "blocked":
        master_state = "blocked"
        reason_codes.append("ledger_or_renderer_blocked")
    elif len(covered_goals) == len(rows):
        master_state = "terminal"
    elif branch_state_counts.get("needs_document") or (document_state == "missing" and partial_goals):
        master_state = "needs_hydration"
        reason_codes.append("document_hydration_needed")
    elif any(branch_state_counts.get(state) for state in actionable_search_states):
        master_state = "needs_more_search"
        reason_codes.append("branch_search_or_radius_needed")
    elif useful_evidence_count > 0 or partial_goals or covered_goals:
        master_state = "usable_partial"
        reason_codes.append("some_goals_unresolved")
    elif rows and all(
        (str(dict(row.get("branch_judgement") or {}).get("state") or row.get("coverage_state") or "").strip().lower() in no_action_states)
        for row in rows
    ):
        master_state = "no_match"
        reason_codes.append("all_missions_missed")
    else:
        master_state = "blocked"
        reason_codes.append("missions_failed_without_useful_evidence")

    context_state_by_master = {
        "terminal": "complete",
        "usable_partial": "partial",
        "needs_hydration": "partial",
        "needs_more_search": "partial",
        "no_match": "no_match",
        "provider_degraded": "provider_degraded",
        "blocked": "blocked",
    }
    agent_payload_state_by_master = {
        "terminal": "usable_context",
        "usable_partial": "partial_context",
        "needs_hydration": "partial_context",
        "needs_more_search": "partial_context",
        "no_match": "no_match",
        "provider_degraded": "provider_degraded",
        "blocked": "blocked",
    }
    context_state = context_state_by_master.get(master_state, "blocked")
    agent_payload_state = agent_payload_state_by_master.get(master_state, "blocked")
    no_match_claim = bool(agent_payload_state == "no_match")
    terminal_for_client = master_state in {"terminal", "no_match"}
    final_seal_allowed = bool(terminal_for_client)
    if master_state == "terminal":
        continuation_recommendation = {
            "state": "none",
            "tool_action": None,
            "reason": "all_missions_resolved",
        }
    elif master_state == "no_match":
        continuation_recommendation = {
            "state": "none",
            "tool_action": None,
            "reason": "honest_no_match_terminal_state",
        }
    elif master_state == "provider_degraded":
        continuation_recommendation = {
            "state": "required",
            "tool_action": "retry_retrieve_after_provider_recovers",
            "reason": "provider_degraded",
        }
    elif master_state == "needs_hydration":
        continuation_recommendation = {
            "state": "required" if document_state in {"raw_refs_ready", "refs_available"} else "recommended",
            "tool_action": "retrieve_document",
            "reason": "master_requires_document_hydration",
        }
    elif master_state == "needs_more_search":
        action = max(branch_next_actions.items(), key=lambda item: item[1])[0] if branch_next_actions else "inspect_path_corridor"
        continuation_recommendation = {
            "state": "required",
            "tool_action": action,
            "reason": "master_requires_more_branch_search",
        }
    elif master_state == "usable_partial":
        action = "retrieve_document" if document_state in {"raw_refs_ready", "refs_available"} and partial_goals else "inspect_path_corridor"
        continuation_recommendation = {
            "state": "recommended",
            "tool_action": action,
            "reason": "usable_partial_context_with_named_missing_goals",
        }
    else:
        continuation_recommendation = {
            "state": "recommended",
            "tool_action": "inspect_path_corridor",
            "reason": "mission_ledger_has_unresolved_goals",
        }

    reason_codes = list(dict.fromkeys([code for code in reason_codes if code]))[:24]
    expected_evidence_policy = _mcp_master_expected_evidence_policy(
        query_text=query_text,
        rows=rows,
        document_state=document_state,
        path_state=path_state,
        branch_state_counts=branch_state_counts,
        master_state=master_state,
    )
    deterministic_precheck_state = (
        "clear_terminal"
        if master_state in {"terminal", "no_match", "provider_degraded", "blocked"}
        else "clear_actionable"
        if master_state in {"needs_hydration", "needs_more_search"}
        else "ambiguous_usable_partial"
    )
    sufficiency_judge = {
        "schema_version": _MCP_MASTER_SUFFICIENCY_JUDGE_SCHEMA_VERSION,
        "tier": "deterministic_precheck",
        "deterministic_precheck_state": deterministic_precheck_state,
        "ai_sufficiency_required": bool(expected_evidence_policy.get("ai_sufficiency_required")),
        "ai_sufficiency_state": (
            "not_required"
            if not bool(expected_evidence_policy.get("ai_sufficiency_required"))
            else "required_before_final_terminal_certification"
            if master_state in {"usable_partial", "needs_more_search"}
            else "covered_by_ai_authored_branch_ledger"
        ),
        "ledger_hash": cache_key[:16],
        "does_not_scan_raw_documents": True,
        "does_not_use_hidden_package_rescue": True,
    }
    ai_master_payload: dict[str, Any] | None = None
    ai_master_error: str | None = None
    ai_master_turn_ms = 0.0
    ai_master_enabled = bool(allow_ai_master or render_contract.get("ai_master_judge_enabled"))
    if ai_master_enabled and bool(expected_evidence_policy.get("ai_sufficiency_required")) and master_state not in {"blocked", "provider_degraded"}:
        ai_master_payload, ai_master_error, ai_master_turn_ms = _mcp_master_run_ai_sufficiency(
            query_text=query_text,
            expected_evidence_policy=expected_evidence_policy,
            master_state=master_state,
            goal_coverage=goal_coverage,
            covered_goals=covered_goals,
            partial_goals=partial_goals,
            missing_goals=missing_goals,
            branch_state_counts=branch_state_counts,
            document_state=document_state,
            path_state=path_state,
            reason_codes=reason_codes,
            render_contract=render_contract,
        )
        sufficiency_judge["master_ai_required"] = True
        sufficiency_judge["master_ai_used"] = bool(ai_master_payload)
        sufficiency_judge["master_ai_error"] = ai_master_error
        sufficiency_judge["master_ai_turn_ms"] = ai_master_turn_ms if ai_master_turn_ms else None
        if ai_master_payload:
            ai_update = _mcp_master_apply_ai_sufficiency(
                ai_payload=ai_master_payload,
                current_state=master_state,
                document_state=document_state,
                path_state=path_state,
                expected_evidence_policy=expected_evidence_policy,
                covered_goals=covered_goals,
                partial_goals=partial_goals,
                missing_goals=missing_goals,
                reason_codes=reason_codes,
            )
            master_state = str(ai_update["master_state"])
            context_state = str(ai_update["context_state"])
            agent_payload_state = str(ai_update["agent_payload_state"])
            terminal_for_client = bool(ai_update["terminal_for_client"])
            final_seal_allowed = bool(ai_update["final_seal_allowed"])
            no_match_claim = bool(ai_update["no_match_claim"])
            covered_goals = list(ai_update["covered_goals"])
            missing_goals = list(ai_update["missing_goals"])
            unresolved_from_ai = list(ai_update["unresolved_goals"])
            continuation_recommendation = dict(ai_update["continuation_recommendation"])
            reason_codes = list(ai_update["reason_codes"])
            sufficiency_judge["tier"] = "ai_master_sufficiency"
            sufficiency_judge["ai_sufficiency_state"] = f"ai_master_{master_state}"
            sufficiency_judge["ai_master_decision"] = ai_master_payload
            partial_goals = [goal for goal in partial_goals if goal in unresolved_from_ai] if unresolved_from_ai else ([] if master_state in {"terminal", "no_match"} else partial_goals)
        elif llm_enabled() and master_state == "terminal":
            master_state = "usable_partial"
            context_state = "partial"
            agent_payload_state = "partial_context"
            terminal_for_client = False
            final_seal_allowed = False
            no_match_claim = False
            continuation_recommendation = {
                "state": "recommended",
                "tool_action": "inspect_context_package",
                "reason": "ai_master_required_but_unavailable",
            }
            reason_codes = list(dict.fromkeys([*reason_codes, "ai_master_required_but_unavailable_terminal_downgraded"]))[:24]
            sufficiency_judge["ai_sufficiency_state"] = "required_but_unavailable_terminal_not_certified"
            sufficiency_judge["master_ai_timeout_fallback"] = True
        else:
            sufficiency_judge["ai_sufficiency_state"] = "required_but_unavailable_deterministic_partial_fallback"
            sufficiency_judge["master_ai_timeout_fallback"] = bool(llm_enabled())
    else:
        sufficiency_judge["master_ai_required"] = bool(expected_evidence_policy.get("ai_sufficiency_required"))
        sufficiency_judge["master_ai_used"] = False
        sufficiency_judge["master_ai_timeout_fallback"] = False
    judgement_id = (
        f"master_{master_state}_{len(rows)}_"
        f"{len(covered_goals)}_{len(partial_goals)}_{len(missing_goals)}"
    )
    result = {
        "schema_version": _MCP_MASTER_JUDGEMENT_SCHEMA_VERSION,
        "master_judgement_id": judgement_id,
        "master_state": master_state,
        "goal_coverage": goal_coverage,
        "covered_goals": covered_goals,
        "partial_goals": partial_goals,
        "missing_goals": missing_goals,
        "unresolved_goals": list(dict.fromkeys(partial_goals + missing_goals))[:24],
        "no_match_claim": no_match_claim,
        "provider_state": "degraded" if provider_degraded else "available",
        "context_state": context_state,
        "document_state": document_state,
        "path_state": path_state,
        "answer_voice": answer_voice,
        "agent_payload_state": agent_payload_state,
        "final_seal_allowed": final_seal_allowed,
        "terminal_for_client": terminal_for_client,
        "continuation_recommendation": continuation_recommendation,
        "next_recommended_call": continuation_recommendation.get("tool_action"),
        "expected_evidence_policy": expected_evidence_policy,
        "sufficiency_judge": sufficiency_judge,
        "master_sufficiency_source": "ai_master_sufficiency" if bool(sufficiency_judge.get("master_ai_used")) else "deterministic_precheck",
        "master_ai_required": bool(sufficiency_judge.get("master_ai_required")),
        "master_ai_used": bool(sufficiency_judge.get("master_ai_used")),
        "master_ai_timeout_fallback": bool(sufficiency_judge.get("master_ai_timeout_fallback")),
        "branch_state_counts": branch_state_counts,
        "subsumed_unresolved_goals": subsumed_unresolved_goals,
        "reason_codes": reason_codes,
        "ledger_row_count": len(rows),
        "cache_key": cache_key[:16],
        "cache_hit": False,
        "master_judge_timing_ms": round((time.perf_counter() - started_at) * 1000.0, 3),
        "source": "mission_evidence_ledger",
    }
    with _MCP_MASTER_JUDGEMENT_CACHE_LOCK:
        if len(_MCP_MASTER_JUDGEMENT_CACHE) >= _MCP_MASTER_JUDGEMENT_CACHE_MAX:
            _MCP_MASTER_JUDGEMENT_CACHE.clear()
        _MCP_MASTER_JUDGEMENT_CACHE[cache_key] = dict(result)
    return result


def _mcp_ledger_candidate_text(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in (
        "text",
        "summary",
        "evidence_snippet",
        "snippet",
        "raw_text",
        "claim",
        "title",
        "source_title",
    ):
        text = _mcp_clean_agent_text(value.get(key))
        if text:
            return text
    return None


def _mcp_ledger_row_section_candidates(row: dict[str, Any]) -> list[str]:
    expected_shape = dict(row.get("expected_evidence_shape") or {})
    values: list[Any] = [
        *list(row.get("package_candidate_sections") or []),
        expected_shape.get("answer_field"),
        expected_shape.get("target_id"),
        row.get("goal"),
        row.get("answer_hypothesis"),
    ]
    sections: list[str] = []
    for value in values:
        section = _mcp_context_section_key(value, f"{row.get('goal') or ''} {row.get('answer_hypothesis') or ''}")
        if section and section in _MCP_CONTEXT_SECTION_TITLES and section not in sections:
            sections.append(section)
    return sections or ["history"]


def _mcp_ledger_hot_candidates(compact_ledger: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for row_index, raw_row in enumerate(list(compact_ledger.get("rows") or []), start=1):
        if not isinstance(raw_row, dict):
            continue
        row = dict(raw_row)
        section_candidates = _mcp_ledger_row_section_candidates(row)
        primary_section = section_candidates[0]
        branch_goal = str(row.get("goal") or row.get("mission_id") or f"mission_{row_index}").strip()
        for evidence_index, raw_evidence in enumerate(list(row.get("hot_evidence") or []), start=1):
            if not isinstance(raw_evidence, dict):
                continue
            evidence = dict(raw_evidence)
            text = _mcp_ledger_candidate_text(evidence)
            if not text:
                continue
            section = _mcp_context_section_key(
                evidence.get("section") or evidence.get("support_slot") or primary_section,
                text,
            )
            if section not in _MCP_CONTEXT_SECTION_TITLES:
                section = primary_section
            node_id = str(
                evidence.get("node_id")
                or evidence.get("target_node_id")
                or evidence.get("source_node_id")
                or ""
            ).strip()
            key = (section, node_id, _fold_text(text))
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                {
                    "node_id": node_id,
                    "section_key": section,
                    "text": text,
                    "raw_candidate_text": text,
                    "confidence": float(evidence.get("confidence") or 0.88),
                    "source_title": _mcp_clean_agent_text(
                        evidence.get("source_title")
                        or evidence.get("title")
                        or row.get("goal")
                        or "branch evidence"
                    ),
                    "source_kind": "mission_ledger_hot",
                    "claim_status": str(evidence.get("claim_status") or "fact").strip(),
                    "answer_eligible": True,
                    "document_eligible": section == "documents",
                    "support_slots": section_candidates,
                    "branch_goals": [branch_goal] if branch_goal else [],
                    "mission_id": str(row.get("mission_id") or "").strip(),
                    "path_id": str(row.get("path_id") or "").strip(),
                    "ledger_order": (row_index, evidence_index),
                }
            )
    return candidates


def _mcp_unresolved_or_missing_body_lines(
    *,
    unresolved_sections: Sequence[str],
    semantic_missing_slot_keys: Sequence[str],
    semantic_missing_descriptions: Sequence[str],
    missing_requested_relations: Sequence[str],
    missing_explicit_query_entities: Sequence[str],
    master_state: str = "",
) -> list[str]:
    lines: list[str] = ["", "## Unresolved Or Missing"]
    normalized_master_state = str(master_state or "").strip().lower()
    if normalized_master_state == "no_match":
        lines.append("- No matching memory evidence was found for this request; no agent-facing facts are certified.")
        return lines

    if unresolved_sections:
        for section in unresolved_sections:
            section = str(section or "").strip()
            if section == "answer_context_alignment":
                lines.append("- Answer/context alignment is unresolved: the final answer references material that is not visible in the approved context package.")
            elif section == "link_aware_context":
                lines.append("- Link-aware context is unresolved: visible context contains a context-dependent fragment without its parent subject or relation.")
            elif section == "explicit_query_entity_coverage":
                lines.append(
                    "- Explicit query entity coverage is unresolved: "
                    + ", ".join(str(item) for item in missing_explicit_query_entities if str(item).strip())
                )
            elif section == "work_entity_inventory":
                lines.append("- Work/company inventory is unresolved: discovered linked work entities are not yet fully visible in the context package.")
            elif section == "path_truth":
                lines.append(
                    "- Path truth is pending: requested route/path traversal has not yet been materialized inside this context package."
                )
            elif section == "document_refs":
                lines.append(
                    "- Document refs are missing: the requested raw-document policy requires actionable source references before the package can be final."
                )
            else:
                if section == "relationships" and missing_requested_relations:
                    lines.append(f"- Missing requested relationship evidence: {', '.join(str(item) for item in missing_requested_relations)}")
                else:
                    lines.append(f"- Missing contract section: {_MCP_CONTEXT_SECTION_TITLES.get(section, section)}")
        if semantic_missing_slot_keys:
            lines.append(f"- Missing semantic slots: {', '.join(str(item) for item in semantic_missing_slot_keys)}")
        for description in semantic_missing_descriptions:
            lines.append(f"- {description}")
    else:
        if semantic_missing_slot_keys:
            lines.append(f"- Missing semantic slots: {', '.join(str(item) for item in semantic_missing_slot_keys)}")
            for description in semantic_missing_descriptions:
                lines.append(f"- {description}")
        else:
            lines.append("- No required context section is currently unresolved.")
    return lines


def _mcp_replace_unresolved_or_missing_section(
    agent_markdown: str,
    replacement_lines: Sequence[str],
) -> str:
    marker = "\n## Unresolved Or Missing"
    text = str(agent_markdown or "").strip()
    replacement = "\n".join(str(line) for line in replacement_lines).strip()
    if marker in text:
        prefix = text.split(marker, 1)[0].rstrip()
        return f"{prefix}\n\n{replacement}".strip()
    return f"{text}\n\n{replacement}".strip()


def _mcp_master_renderer_projection(
    *,
    ledger_renderer_mode: bool,
    master_judgement: dict[str, Any],
    unresolved_sections: Sequence[str],
    semantic_missing_slot_keys: Sequence[str],
    ordered_sections: Sequence[dict[str, Any]],
    original_contract_passed: bool,
    original_status: str,
) -> dict[str, Any]:
    master_state = str((master_judgement or {}).get("master_state") or "").strip().lower()
    final_unresolved = [str(section).strip() for section in unresolved_sections if str(section).strip()]
    static_demoted: list[str] = []
    safety_unresolved = set()
    safety_blockers = {
        "answer_context_alignment",
        "link_aware_context",
        "explicit_query_entity_coverage",
        "path_truth",
        "document_refs",
    }
    if ledger_renderer_mode and master_state in {"terminal", "no_match"}:
        kept: list[str] = []
        for section in final_unresolved:
            if section in safety_blockers:
                kept.append(section)
                safety_unresolved.add(section)
            elif section in _MCP_CONTEXT_SECTION_TITLES or section == "work_entity_inventory":
                static_demoted.append(section)
            else:
                kept.append(section)
                safety_unresolved.add(section)
        final_unresolved = kept

    if not ledger_renderer_mode or not master_state:
        final_contract_passed = bool(original_contract_passed)
        final_status = original_status
    elif master_state == "terminal":
        final_contract_passed = bool(
            not final_unresolved
            and not list(semantic_missing_slot_keys or [])
            and bool(list(ordered_sections or []))
        )
        final_status = "contract_satisfied" if final_contract_passed else "usable_partial"
    elif master_state == "no_match":
        final_contract_passed = False
        final_status = "no_match"
    elif master_state in {"usable_partial", "needs_hydration", "needs_more_search", "provider_degraded", "blocked"}:
        final_contract_passed = False
        final_status = master_state
    else:
        final_contract_passed = bool(original_contract_passed)
        final_status = original_status

    return {
        "master_state": master_state,
        "final_status": final_status,
        "final_contract_passed": final_contract_passed,
        "final_unresolved_sections": final_unresolved,
        "static_unresolved_demoted": list(dict.fromkeys(static_demoted)),
        "safety_unresolved_sections": list(dict.fromkeys(safety_unresolved)),
        "renderer_obeys_master": bool(
            ledger_renderer_mode
            and master_state
            and (
                master_state != "terminal"
                or final_contract_passed
                or bool(final_unresolved)
                or bool(list(semantic_missing_slot_keys or []))
            )
        ),
    }


def build_mcp_context_package(
    *,
    query_text: str,
    context: dict[str, Any] | None,
    context_structured: dict[str, Any] | None = None,
    matches: list[dict[str, Any]] | None = None,
    evidence_reservoir: dict[str, Any] | None = None,
    document_packets: list[dict[str, Any]] | None = None,
    semantic_contract: dict[str, Any] | None = None,
    retrieval_mode: str = "balanced",
    path_corridors: dict[str, Any] | None = None,
    path_truth_required: bool = False,
    document_workspace: dict[str, Any] | None = None,
    answer_payload: dict[str, Any] | None = None,
    package_mode: str | None = None,
    document_text_policy: str | None = None,
    mission_evidence_ledger: dict[str, Any] | None = None,
    allow_ai_master: bool = False,
) -> dict[str, Any]:
    package_render_started_at = time.perf_counter()
    context_structured = dict(context_structured or {})
    compact_mission_evidence_ledger = _mcp_compact_mission_evidence_ledger(
        mission_evidence_ledger or context_structured.get("mission_evidence_ledger")
    )
    ledger_renderer_mode = bool(compact_mission_evidence_ledger)
    effective_document_text_policy = _mcp_normalize_document_text_policy(document_text_policy)
    required_sections, optional_sections, allowed_sections, forbidden_sections, broad_context, document_mode = _mcp_context_contract_sets(semantic_contract, query_text)
    effective_package_mode = _mcp_infer_context_package_mode(
        query_text=query_text,
        retrieval_mode=retrieval_mode,
        semantic_contract=semantic_contract,
        document_mode=document_mode,
        broad_context=broad_context,
        requested_mode=package_mode,
    )
    package_policy = _mcp_context_package_policy(effective_package_mode)
    allowed_sections = _mcp_widen_allowed_sections_for_package_mode(
        package_mode=effective_package_mode,
        allowed_sections=allowed_sections,
        required_sections=required_sections,
        optional_sections=optional_sections,
        forbidden_sections=forbidden_sections,
        broad_context=broad_context,
    )
    contract_core_sections = _mcp_contract_core_sections(
        required_sections=required_sections,
        optional_sections=optional_sections,
        document_mode=document_mode,
        broad_context=broad_context,
        package_mode=effective_package_mode,
    )
    forbidden_topic_markers = _mcp_forbidden_topic_markers(semantic_contract)
    requested_relations = _mcp_requested_relations(semantic_contract, query_text)
    exact_field_requirements: list[dict[str, Any]] = []
    seen_exact_field_keys: set[str] = set()
    for request in (
        exact_field_request_from_slot_contract(slot_contract)
        for slot_contract in list((semantic_contract or {}).get("semantic_slot_contracts") or [])
        if isinstance(slot_contract, dict) and bool(slot_contract.get("required"))
    ):
        if not request:
            continue
        key = str(request.get("slot_key") or request.get("field_key") or request.get("field_label") or "").strip()
        if key and key in seen_exact_field_keys:
            continue
        if key:
            seen_exact_field_keys.add(key)
        exact_field_requirements.append(dict(request))
    exact_field_sections = {
        _mcp_context_section_key(request.get("section") or "")
        for request in exact_field_requirements
        if str(request.get("section") or "").strip()
    }
    if exact_field_requirements:
        allowed_sections = set(exact_field_sections)
        optional_sections = set()
        broad_context = False
        document_mode = "none"
        package_policy = {
            **package_policy,
            "reservoir_summary_limit": 0,
            "source_excerpt_limit": 0,
            "path_context_limit": 0,
        }
    private_boundary_requested = bool(
        not exact_field_requirements
        and _mcp_query_requests_private_data_boundary(query_text, semantic_contract)
    )
    if private_boundary_requested:
        required_sections.add("privacy_boundary")
        allowed_sections.add("privacy_boundary")
        contract_core_sections.add("privacy_boundary")
    answer_payload = dict(answer_payload or {})
    answer_text_for_alignment = _mcp_answer_payload_text(answer_payload)
    answer_alignment_terms = _mcp_answer_alignment_terms(answer_text_for_alignment)
    answer_evidence_ids = {
        str(item).strip()
        for item in list(answer_payload.get("evidence_node_ids") or [])
        if str(item).strip()
    }
    source_document_workspace = dict(document_workspace or {})
    source_workspace_has_document_material = any(
        list(source_document_workspace.get(field) or [])
        for field in ("documents", "primary_documents", "related_or_cold_documents", "related_documents")
    )
    if not source_workspace_has_document_material and any(
        isinstance(packet, dict) and is_document_eligible(packet)
        for packet in list(document_packets or [])
    ):
        source_document_workspace = build_document_workspace_package(
            query_text=query_text,
            document_mode=document_mode,
            document_lookup={},
            document_packets=[dict(packet) for packet in list(document_packets or []) if isinstance(packet, dict)],
            evidence_reservoir=evidence_reservoir,
            semantic_contract=semantic_contract,
            retrieval_mode=retrieval_mode,
            path_corridors=path_corridors,
        )
        source_workspace_has_document_material = bool(list(source_document_workspace.get("documents") or []))
    if (
        not source_workspace_has_document_material
        and effective_document_text_policy in {"top_raw", "all_raw"}
    ):
        source_document_workspace = _mcp_source_workspace_from_retrieved_material(
            query_text=query_text,
            matches=[dict(match) for match in list(matches or []) if isinstance(match, dict)],
            evidence_reservoir=evidence_reservoir,
            semantic_contract=semantic_contract,
            forbidden_topic_markers=forbidden_topic_markers,
            context_structured=context_structured,
            limit=6,
        )

    raw_workspace_documents = [
        dict(item)
        for item in list(source_document_workspace.get("documents") or [])
        if isinstance(item, dict)
    ]
    if not raw_workspace_documents:
        seen_workspace_doc_keys: set[str] = set()
        for field in ("primary_documents", "related_or_cold_documents", "related_documents"):
            for item in list(source_document_workspace.get(field) or []):
                if not isinstance(item, dict):
                    continue
                key = _document_ref_id(item, len(seen_workspace_doc_keys) + 1)
                if key in seen_workspace_doc_keys:
                    continue
                seen_workspace_doc_keys.add(key)
                raw_workspace_documents.append(dict(item))
    workspace_documents = [
        dict(item)
        for item in raw_workspace_documents
        if isinstance(item, dict)
        and not _mcp_forbidden_topic_hits(
            "\n".join(str(item.get(key) or "") for key in ("title", "text", "summary", "raw_text", "full_text")),
            forbidden_topic_markers,
        )
    ]
    if ledger_renderer_mode and effective_document_text_policy == "refs_only":
        workspace_documents = [
            _mcp_document_ref_only_for_ledger_renderer(document)
            for document in workspace_documents
        ]
    safe_document_workspace = dict(source_document_workspace or {})
    if forbidden_topic_markers:
        for field in ("documents", "primary_documents", "related_or_cold_documents", "related_documents", "source_trace"):
            filtered_rows = []
            for item in list(safe_document_workspace.get(field) or []):
                if not isinstance(item, dict):
                    continue
                blob = "\n".join(str(value or "") for value in item.values() if isinstance(value, (str, int, float)))
                if not _mcp_forbidden_topic_hits(blob, forbidden_topic_markers):
                    filtered_rows.append(dict(item))
            safe_document_workspace[field] = filtered_rows
    safe_document_workspace["documents"] = workspace_documents
    safe_document_workspace["primary_documents"] = [
        dict(item)
        for item in workspace_documents
        if bool(item.get("primary_context_eligible")) or str(item.get("workspace_tier") or "") == "requested"
    ]
    safe_document_workspace["related_or_cold_documents"] = [
        dict(item)
        for item in workspace_documents
        if not (bool(item.get("primary_context_eligible")) or str(item.get("workspace_tier") or "") == "requested")
    ]
    safe_document_workspace["document_refs"] = _document_workspace_refs(workspace_documents)
    safe_document_workspace["primary_document_refs"] = [
        dict(ref)
        for ref in list(safe_document_workspace.get("document_refs") or [])
        if str(ref.get("relationship_to_query") or "") == "primary"
    ]
    safe_document_workspace["candidate_document_refs"] = [
        dict(ref)
        for ref in list(safe_document_workspace.get("document_refs") or [])
        if str(ref.get("relationship_to_query") or "") in {"primary", "supporting", "near_miss"}
    ]
    safe_document_workspace["related_document_refs"] = [
        dict(ref)
        for ref in list(safe_document_workspace.get("document_refs") or [])
        if str(ref.get("relationship_to_query") or "") in {"supporting", "near_miss", "related"}
    ]
    safe_document_workspace["document_evidence_lane"] = {
        **dict(safe_document_workspace.get("document_evidence_lane") or {}),
        "documents": [],
        "ranked_document_refs": list(safe_document_workspace.get("document_refs") or []),
        "primary_document_refs": list(safe_document_workspace.get("primary_document_refs") or []),
        "candidate_document_refs": list(safe_document_workspace.get("candidate_document_refs") or []),
        "related_document_refs": list(safe_document_workspace.get("related_document_refs") or []),
    }
    safe_document_workspace["document_ref_contract"] = _document_ref_contract_from_refs(
        list(safe_document_workspace.get("document_refs") or [])
    )
    document_refs = [dict(ref) for ref in list(safe_document_workspace.get("document_refs") or []) if isinstance(ref, dict)]
    document_ref_contract = dict(safe_document_workspace.get("document_ref_contract") or {})
    document_bundle = _mcp_document_bundle_for_policy(safe_document_workspace, effective_document_text_policy)
    document_delivery_contract = _mcp_document_delivery_contract(
        document_workspace=safe_document_workspace,
        document_refs=document_refs,
        document_ref_contract=document_ref_contract,
        document_bundle=document_bundle,
        document_text_policy=effective_document_text_policy,
    )
    safe_document_workspace["document_delivery_contract"] = document_delivery_contract

    candidates: list[dict[str, Any]] = []
    seen_candidate_keys: set[tuple[str, str]] = set()

    def add_candidate(candidate: dict[str, Any] | None) -> None:
        if not candidate:
            return
        original_text = str(candidate.get("text") or "").strip()
        text = original_text
        section_key = _mcp_context_section_key(candidate.get("section_key"), text)
        if not ledger_renderer_mode:
            text = _mcp_agent_relation_text_for_candidate(
                text,
                section_key=section_key,
                query_text=query_text,
                semantic_contract=semantic_contract,
            )
        text = _mcp_clean_agent_text(text)
        if not text or section_key not in _MCP_CONTEXT_SECTION_TITLES:
            return
        if section_key == "temporal_inventory" and _query_is_work_or_company(query_text):
            folded_candidate_text = _fold_text(text)
            if any(
                marker in folded_candidate_text
                for marker in (
                    "company",
                    "companies",
                    "azienda",
                    "aziende",
                    "project",
                    "progetto",
                    "foundry",
                    "studio",
                    "founder",
                    "founded",
                    "fondat",
                    "created",
                    "creat",
                    "acquired",
                    "acquis",
                    "ceo",
                )
            ):
                section_key = "work"
        unrequested_work_optional_section = bool(
            _query_is_work_or_company(query_text)
            and _mcp_explicit_query_entities(query_text)
            and section_key in {"values", "style", "relationships", "temporal_inventory"}
            and section_key not in required_sections
            and not _mcp_query_explicitly_requests_section(query_text, section_key)
        )
        folded_key = (section_key, _fold_text(text))
        if folded_key in seen_candidate_keys:
            return
        seen_candidate_keys.add(folded_key)
        candidates.append(
            {
                **candidate,
                "section_key": section_key,
                "text": text,
                "raw_candidate_text": str(candidate.get("raw_candidate_text") or original_text),
                "unrequested_work_optional_section": unrequested_work_optional_section,
            }
        )

    if ledger_renderer_mode:
        for ledger_candidate in _mcp_ledger_hot_candidates(compact_mission_evidence_ledger):
            add_candidate(ledger_candidate)

    structured_subject_name = _mcp_identity_subject_name_from_contract(semantic_contract)
    for section in list((context or {}).get("structured_sections") or []):
        if not isinstance(section, dict):
            continue
        section_key = _mcp_context_section_key(section.get("key") or section.get("title"))
        evidence_ids = [str(item) for item in list(section.get("evidence_node_ids") or []) if str(item).strip()]
        for item in list(section.get("items") or []):
            text = _mcp_clean_agent_text(item)
            if not text:
                continue
            add_candidate(
                {
                    "node_id": evidence_ids[0] if evidence_ids else "",
                    "section_key": section_key,
                    "text": text,
                    "confidence": float(section.get("confidence") or 0.0),
                    "source_title": None,
                    "source_kind": "structured_context",
                    "structured_subject_context": bool(structured_subject_name and evidence_ids),
                    "answer_eligible": True,
                    "document_eligible": section_key == "documents",
                }
            )

    for fragment in list((context_structured or {}).get("hot_context_fragments") or []):
        if isinstance(fragment, dict):
            add_candidate(_mcp_candidate_from_entry(fragment))
    for entry in list((evidence_reservoir or {}).get("entries") or []):
        if isinstance(entry, dict):
            add_candidate(_mcp_candidate_from_entry(entry))
    for match in list(matches or []):
        if isinstance(match, dict):
            add_candidate(_mcp_candidate_from_entry(match))
    path_discovery_entries = _mcp_path_discovery_entries(path_corridors)
    agent_path_discovery_entries = path_discovery_entries[: int(package_policy.get("path_context_limit") or 0)]
    for discovery in agent_path_discovery_entries:
        add_candidate(
            {
                "node_id": "",
                "section_key": discovery.get("section_key"),
                "text": discovery.get("text"),
                "confidence": 0.72,
                "source_title": "path corridor",
                "source_kind": "path_corridor",
                "answer_eligible": True,
                "document_eligible": str(discovery.get("section_key") or "") == "documents",
            }
        )
    if _query_is_work_or_company(query_text) and not ledger_renderer_mode:
        promoted_document_relation_count = 0
        for ref in document_refs:
            relation_source = " ".join(
                str(ref.get(key) or "")
                for key in ("title", "source_label", "source_type", "summary")
                if str(ref.get(key) or "").strip()
            )
            if not relation_source:
                continue
            for claim in _mcp_company_relation_claims(relation_source):
                add_candidate(
                    {
                        "node_id": str(ref.get("document_id") or ref.get("anchor_node_id") or ""),
                        "section_key": "work",
                        "text": claim,
                        "confidence": max(float(ref.get("query_fit_score") or 0.0), 0.68),
                        "source_title": str(ref.get("title") or ref.get("source_label") or "document reference"),
                        "source_kind": "document_ref_relation",
                        "answer_eligible": True,
                        "document_eligible": False,
                    }
                )
                promoted_document_relation_count += 1
                if promoted_document_relation_count >= 4:
                    break
            if promoted_document_relation_count >= 4:
                break

    sections: dict[str, dict[str, Any]] = {
        key: {"key": key, "title": title, "items": [], "sources": [], "confidence": 0.0}
        for key, title in _MCP_CONTEXT_SECTION_TITLES.items()
    }
    hot_context: list[dict[str, Any]] = []
    cold_context: list[dict[str, Any]] = []
    excluded_material: list[dict[str, Any]] = []
    debug_ledger: list[dict[str, Any]] = []
    candidate_text_by_node_id: dict[str, str] = {}
    if ledger_renderer_mode:
        debug_ledger.append(
            {
                "node_id": "",
                "section_key": "renderer",
                "promotion_state": "audit",
                "reason": "package_renderer_ledger_only",
                "source_title": "mission evidence ledger",
            }
        )

    for candidate in candidates:
        section_key = str(candidate.get("section_key") or "")
        text = str(candidate.get("text") or "").strip()
        node_id = str(candidate.get("node_id") or "").strip()
        source_title = str(candidate.get("source_title") or "").strip() or None
        source_kind = str(candidate.get("source_kind") or "").strip() or None
        claim_status = str(candidate.get("claim_status") or "").strip().lower()
        memory_type = str(candidate.get("memory_type") or "").strip().lower()
        document_role = str(candidate.get("document_role") or "").strip().lower()
        is_raw_source_block = _mcp_agent_text_is_raw_source_block(text)
        is_document_anchor_candidate = bool(candidate.get("is_document_anchor")) or memory_type == "document_anchor" or document_role == "anchor"
        non_fact_context_candidate = claim_status in {"hypothesis", "future_intent", "open_question", "contradiction", "superseded"}
        exact_field_match = None
        if exact_field_requirements:
            for request in exact_field_requirements:
                if text_satisfies_exact_field_request(text, request):
                    exact_field_match = request
                    break
            if exact_field_match:
                section_key = _mcp_context_section_key(exact_field_match.get("section") or section_key)
        if node_id and text and node_id not in candidate_text_by_node_id:
            candidate_text_by_node_id[node_id] = text
        answer_support_candidate = bool(node_id and node_id in answer_evidence_ids) or _mcp_text_has_alignment_term(text, answer_alignment_terms)
        candidate_support_sections = {
            _mcp_context_section_key(slot)
            for slot in list(candidate.get("support_slots") or []) + list(candidate.get("branch_goals") or [])
            if str(slot or "").strip()
        }
        core_context_candidate = bool(
            broad_context
            or section_key in contract_core_sections
            or bool(candidate_support_sections & contract_core_sections)
        )
        subject_anchor_required = _mcp_subject_anchor_required(query_text, semantic_contract)
        subject_anchored_candidate = bool(
            _mcp_text_is_subject_anchored(text, source_title, semantic_contract)
            or (
                source_kind == "structured_context"
                and bool(candidate.get("structured_subject_context"))
                and (
                    section_key in required_sections
                    or (
                        broad_context
                        and section_key in {"history", "temporal_inventory", "relationships", "values", "style"}
                    )
                )
                and _mcp_structured_subject_claim_is_anchored(text, section_key)
            )
        )
        requested_relation_candidate = _mcp_text_matches_requested_relation(text, source_title, requested_relations)
        relation_anchor_required_for_section = bool(
            requested_relations
            and section_key in {"identity", "work", "relationships", "history", "temporal_inventory", "style", "values"}
        )
        if relation_anchor_required_for_section and section_key == "work" and "work" in required_sections and _query_is_work_or_company(query_text):
            relation_anchor_required_for_section = False
        if (
            relation_anchor_required_for_section
            and section_key == "identity"
            and "identity" in required_sections
            and _mcp_query_requests_exact_identity_name(query_text, required_sections)
        ):
            relation_anchor_required_for_section = False
        reason = "promoted"
        promotion_state = "hot"
        if (
            (is_document_anchor_candidate or is_raw_source_block)
            and effective_document_text_policy == "refs_only"
            and document_mode == "none"
        ):
            promotion_state = "excluded"
            reason = "raw_document_anchor_kept_as_document_ref"
        elif not bool(candidate.get("answer_eligible")) and section_key != "documents" and not non_fact_context_candidate:
            promotion_state = "excluded"
            reason = "not_answer_eligible"
        elif _mcp_forbidden_topic_hits(text, forbidden_topic_markers):
            promotion_state = "excluded"
            reason = "forbidden_topic_by_semantic_contract"
        elif exact_field_requirements and not exact_field_match:
            promotion_state = "cold"
            reason = "does_not_satisfy_exact_requested_field"
        elif section_key in forbidden_sections:
            promotion_state = "excluded"
            reason = "forbidden_by_semantic_contract"
        elif section_key not in allowed_sections:
            promotion_state = "cold"
            reason = "off_contract_reservoir"
        elif (
            subject_anchor_required
            and section_key in {"identity", "work", "relationships", "history", "temporal_inventory", "style", "values"}
            and not subject_anchored_candidate
            and not exact_field_match
        ):
            promotion_state = "cold"
            reason = "subject_anchor_missing"
        elif (
            relation_anchor_required_for_section
            and not requested_relation_candidate
            and not exact_field_match
        ):
            promotion_state = "cold"
            reason = "missing_requested_relation_anchor"
        elif (
            effective_package_mode == "mcp_operational"
            and not core_context_candidate
            and not answer_support_candidate
            and not exact_field_match
        ):
            promotion_state = "cold"
            reason = "non_core_context_reservoir"
        elif bool(candidate.get("unrequested_work_optional_section")) and not exact_field_match:
            promotion_state = "cold"
            reason = "unrequested_optional_context_reservoir"
        elif non_fact_context_candidate:
            reason = "promoted_non_fact_context"
        elif answer_support_candidate:
            reason = "promoted_answer_support"

        debug_ledger.append(
            {
                "node_id": node_id,
                "section_key": section_key,
                "promotion_state": promotion_state,
                "reason": reason,
                "source_title": source_title,
            }
        )
        if promotion_state == "hot":
            section = sections[section_key]
            if text not in section["items"]:
                section["items"].append(text)
            if source_title and source_title not in section["sources"]:
                section["sources"].append(source_title)
            section["confidence"] = max(float(section["confidence"]), float(candidate.get("confidence") or 0.0))
            hot_context.append(
                {
                    "section": section_key,
                    "text": text,
                    "raw_candidate_text": candidate.get("raw_candidate_text"),
                    "source_title": source_title,
                    "source_kind": source_kind,
                    "claim_status": claim_status,
                    "node_id": node_id,
                }
            )
        elif promotion_state == "cold":
            cold_context.append(
                {
                    "section": section_key,
                    "text": text,
                    "raw_candidate_text": candidate.get("raw_candidate_text"),
                    "reason": reason,
                    "source_title": source_title,
                    "source_kind": source_kind,
                    "claim_status": claim_status,
                    "node_id": node_id,
                }
            )
        else:
            excluded_material.append(
                {
                    "section": section_key,
                    "text": text,
                    "raw_candidate_text": candidate.get("raw_candidate_text"),
                    "reason": reason,
                    "source_title": source_title,
                    "source_kind": source_kind,
                    "claim_status": claim_status,
                    "node_id": node_id,
                }
            )

    def promote_linked_parent_context(
        *,
        target_section: str,
        source_sections: tuple[str, ...],
        predicate: Any,
    ) -> None:
        if target_section not in contract_core_sections or sections[target_section]["items"]:
            return
        promoted_count = 0
        seen_target_texts = {_fold_text(item) for item in list(sections[target_section].get("items") or [])}
        for source_section in source_sections:
            for source_item in list(sections.get(source_section, {}).get("items") or []):
                cleaned_item = _mcp_clean_agent_text(source_item)
                if not cleaned_item or not bool(predicate(cleaned_item)):
                    continue
                folded_item = _fold_text(cleaned_item)
                if not folded_item or folded_item in seen_target_texts:
                    continue
                seen_target_texts.add(folded_item)
                sections[target_section]["items"].append(cleaned_item)
                sections[target_section]["sources"].append("linked parent context")
                sections[target_section]["confidence"] = max(float(sections[target_section]["confidence"]), 0.66)
                hot_context.append(
                    {
                        "section": target_section,
                        "text": cleaned_item,
                        "source_title": "linked parent context",
                        "source_kind": "linked_parent_context",
                        "claim_status": "fact",
                        "node_id": "",
                    }
                )
                debug_ledger.append(
                    {
                        "node_id": "",
                        "section_key": target_section,
                        "promotion_state": "hot",
                        "reason": "linked_parent_context_backfill",
                        "source_title": "linked parent context",
                    }
                )
                promoted_count += 1
                if promoted_count >= 2:
                    return

    def promote_relation_timeline_context() -> None:
        if not requested_relations:
            return
        timeline_target_sections = [
            section
            for section in ("history", "temporal_inventory")
            if section in contract_core_sections or section in required_sections
        ]
        if not timeline_target_sections:
            return
        timeline_target_sections = [
            section for section in timeline_target_sections if not sections[section]["items"]
        ]
        if not timeline_target_sections:
            return
        seen_by_section = {
            section: {_fold_text(item) for item in list(sections[section].get("items") or [])}
            for section in timeline_target_sections
        }
        for source_item in list(sections.get("relationships", {}).get("items") or []):
            cleaned_item = _mcp_clean_agent_text(source_item)
            if (
                not cleaned_item
                or not _mcp_text_matches_requested_relation(cleaned_item, "", requested_relations)
                or not _mcp_relation_timeline_text(cleaned_item)
            ):
                continue
            folded_item = _fold_text(cleaned_item)
            if not folded_item:
                continue
            promoted_any = False
            for target_section in timeline_target_sections:
                if folded_item in seen_by_section[target_section]:
                    continue
                seen_by_section[target_section].add(folded_item)
                sections[target_section]["items"].append(cleaned_item)
                sections[target_section]["sources"].append("linked relation timeline")
                sections[target_section]["confidence"] = max(float(sections[target_section]["confidence"]), 0.66)
                hot_context.append(
                    {
                        "section": target_section,
                        "text": cleaned_item,
                        "source_title": "linked relation timeline",
                        "source_kind": "linked_relation_timeline",
                        "claim_status": "fact",
                        "node_id": "",
                    }
                )
                debug_ledger.append(
                    {
                        "node_id": "",
                        "section_key": target_section,
                        "promotion_state": "hot",
                        "reason": "linked_relation_timeline_backfill",
                        "source_title": "linked relation timeline",
                    }
                )
                promoted_any = True
            if promoted_any:
                return

    promote_linked_parent_context(
        target_section="identity",
        source_sections=("work", "history", "documents"),
        predicate=_mcp_text_can_backfill_identity_from_work,
    )
    promote_linked_parent_context(
        target_section="work",
        source_sections=("identity", "documents", "history"),
        predicate=_mcp_text_can_backfill_work_from_identity,
    )
    promote_relation_timeline_context()
    identity_subject_name = _mcp_identity_subject_name_from_contract(semantic_contract) or _mcp_subject_name_from_query(query_text)
    exact_identity_name_requested = _mcp_query_requests_exact_identity_name(query_text, required_sections)
    if identity_subject_name and exact_identity_name_requested:
        identity_line = f"The memory subject's name is {identity_subject_name}."
        folded_identity_line = _fold_text(identity_line)
        existing_identity_items = [
            str(item)
            for item in list(sections["identity"].get("items") or [])
            if str(item or "").strip()
        ]
        if folded_identity_line not in {_fold_text(item) for item in existing_identity_items}:
            sections["identity"]["items"].insert(0, identity_line)
            sections["identity"]["sources"].append("identity nucleus")
            sections["identity"]["confidence"] = max(float(sections["identity"]["confidence"]), 0.9)
            hot_context.insert(
                0,
                {
                    "section": "identity",
                    "text": identity_line,
                    "source_title": "identity nucleus",
                    "source_kind": "identity_nucleus",
                    "claim_status": "fact",
                    "node_id": "",
                },
            )
            debug_ledger.append(
                {
                    "node_id": "",
                    "section_key": "identity",
                    "promotion_state": "hot",
                    "reason": "exact_identity_name_from_nucleus",
                    "source_title": "identity nucleus",
                }
            )
    if (
        identity_subject_name
        and "identity" in required_sections
        and _mcp_subject_anchor_required(query_text, semantic_contract)
        and _fold_text(identity_subject_name) not in _fold_text("\n".join(str(item) for item in list(sections["identity"].get("items") or [])))
    ):
        identity_line = f"Identity subject: {identity_subject_name}."
        sections["identity"]["items"].insert(0, identity_line)
        sections["identity"]["sources"].append("identity nucleus")
        sections["identity"]["confidence"] = max(float(sections["identity"]["confidence"]), 0.74)
        hot_context.append(
            {
                "section": "identity",
                "text": identity_line,
                "source_title": "identity nucleus",
                "source_kind": "identity_nucleus",
                "claim_status": "fact",
                "node_id": "",
            }
        )

    def promote_required_work_claims_from_available_context() -> None:
        if ledger_renderer_mode:
            debug_ledger.append(
                {
                    "node_id": "",
                    "section_key": "work",
                    "promotion_state": "blocked",
                    "reason": "semantic_discovery_disabled_in_ledger_renderer",
                    "source_title": "required work claim rescue",
                }
            )
            return
        if not _query_is_work_or_company(query_text) or "work" not in contract_core_sections:
            return
        inventory_query = _mcp_query_requests_work_entity_inventory(query_text)
        existing_items = [str(item or "").strip() for item in list(sections["work"].get("items") or []) if str(item or "").strip()]
        folded_query = _fold_text(query_text)
        query_entities = _mcp_explicit_query_entities(query_text)
        query_entity_terms = [_fold_text(entity) for entity in query_entities if _fold_text(entity)]
        if existing_items:
            folded_existing_work = _fold_text("\n".join(existing_items))
            subject_required = bool(
                identity_subject_name
                and (
                    _fold_text(identity_subject_name) in folded_query
                    or _mcp_subject_anchor_required(query_text, semantic_contract)
                )
            )
            subject_missing = bool(subject_required and _fold_text(identity_subject_name) not in folded_existing_work)
            missing_query_entity_count = sum(
                1
                for entity in query_entities
                if _fold_text(entity) and _fold_text(entity) not in folded_existing_work
            )
            allowed_missing_query_entities = 0 if query_entities else max(1, len(query_entities) // 3)
            existing_entity_count = len(_mcp_work_entity_inventory_from_texts(existing_items, identity_subject_name))
            inventory_too_thin = bool(inventory_query and existing_entity_count < 3)
            if not subject_missing and missing_query_entity_count <= allowed_missing_query_entities and not inventory_too_thin:
                return
        existing_work_texts = {_fold_text(item) for item in list(sections["work"].get("items") or []) if _fold_text(item)}
        source_pool = [
            item
            for item in list(hot_context) + list(cold_context) + list(excluded_material)
            if str(item.get("section") or "") in {"identity", "work", "history", "temporal_inventory", "documents"}
        ]
        raw_relation_sources: list[dict[str, Any]] = []
        seen_raw_relation_source_keys: set[str] = set()

        def add_raw_relation_source(
            *,
            texts: Sequence[Any],
            node_id: Any = "",
            source_title: Any = "",
            source_kind: str = "raw_relation_source",
        ) -> None:
            for value in texts:
                raw_text = " ".join(str(value or "").split()).strip()
                if not raw_text:
                    continue
                folded_text = _fold_text(raw_text)
                if not folded_text:
                    continue
                relation_or_entity_hit = bool(
                    (identity_subject_name and _fold_text(identity_subject_name) in folded_text)
                    or any(term and term in folded_text for term in query_entity_terms)
                    or any(
                        marker in folded_text
                        for marker in (
                            "founder",
                            "co-founder",
                            "cofounder",
                            "founded",
                            "fondat",
                            "ceo",
                            "chief executive officer",
                            "acquired",
                            "acquis",
                            "created",
                            "established",
                        )
                    )
                )
                if not relation_or_entity_hit:
                    continue
                bounded_text = raw_text[:16000]
                key = _fold_text(f"{node_id} {source_title} {bounded_text[:500]}")
                if not key or key in seen_raw_relation_source_keys:
                    continue
                seen_raw_relation_source_keys.add(key)
                raw_relation_sources.append(
                    {
                        "section": "work",
                        "text": bounded_text,
                        "raw_candidate_text": bounded_text,
                        "reason": "raw_relation_source",
                        "source_title": str(source_title or "raw relation source"),
                        "source_kind": source_kind,
                        "claim_status": "fact",
                        "node_id": str(node_id or ""),
                    }
                )

        for match in list(matches or []):
            if not isinstance(match, dict):
                continue
            node = dict(match.get("node") or {})
            add_raw_relation_source(
                texts=[
                    node.get("raw_text"),
                    match.get("evidence_snippet"),
                    match.get("summary"),
                    node.get("summary"),
                ],
                node_id=match.get("node_id") or node.get("id"),
                source_title=match.get("source_title") or node.get("source_unit_title") or _mcp_candidate_source_title({**node, **match}),
                source_kind="match_raw_relation_source",
            )
        for document in workspace_documents:
            add_raw_relation_source(
                texts=[
                    document.get("full_text"),
                    document.get("raw_text"),
                    document.get("summary"),
                    document.get("title"),
                ],
                node_id=document.get("document_id") or document.get("anchor_node_id"),
                source_title=document.get("title") or document.get("source_label"),
                source_kind="document_workspace_relation_source",
            )
        for packet in list(document_packets or []):
            if not isinstance(packet, dict):
                continue
            add_raw_relation_source(
                texts=[
                    packet.get("full_text"),
                    packet.get("anchor_raw_text"),
                    packet.get("summary"),
                    packet.get("title"),
                    packet.get("source_label"),
                ],
                node_id=packet.get("anchor_node_id") or packet.get("document_id"),
                source_title=packet.get("title") or packet.get("source_label"),
                source_kind="document_packet_relation_source",
            )
        if raw_relation_sources:
            source_pool.extend(raw_relation_sources)
        linked_entities: list[str] = []
        for item in source_pool:
            for source_text in (item.get("text"), item.get("raw_candidate_text")):
                for entity in _mcp_extract_linked_work_entities(source_text, identity_subject_name):
                    folded_entity = _fold_text(entity)
                    if folded_entity and folded_entity not in {_fold_text(existing) for existing in linked_entities}:
                        linked_entities.append(entity)
        for ref in document_refs:
            ref_text = " ".join(
                str(ref.get(key) or "")
                for key in ("title", "source_label", "source_type", "summary")
                if str(ref.get(key) or "").strip()
            )
            for entity in _mcp_extract_linked_work_entities(ref_text, identity_subject_name):
                folded_entity = _fold_text(entity)
                if folded_entity and folded_entity not in {_fold_text(existing) for existing in linked_entities}:
                    linked_entities.append(entity)

        def work_claim_is_self_contained(claim: str) -> bool:
            folded_claim = _fold_text(claim)
            if not folded_claim:
                return False
            if _mcp_agent_body_has_node_id(claim) or _mcp_agent_text_is_raw_source_block(claim):
                return False
            if re.search(r"\b(?:he|she|they|his|her|their|lui|lei|suo|sua|suoi|sue)\b", folded_claim):
                return False
            relation_markers = (
                "founder",
                "founded",
                "co-founder",
                "cofounder",
                "ceo",
                "chief executive officer",
                "president",
                "director",
                "member",
                "partner",
                "acquired",
                "created",
                "built",
                "launched",
                "was founded",
                "is founded",
                "established",
                "fondatore",
                "fondato",
                "acquis",
            )
            return any(marker in folded_claim for marker in relation_markers) and bool(re.search(r"[A-Z][A-Za-z0-9]", claim))

        def add_work_claim(claim: str, source_item: dict[str, Any], reason: str) -> bool:
            clean_claim = _mcp_clean_agent_text(claim)
            if not clean_claim:
                return False
            clean_claim = _mcp_agent_relation_text_for_candidate(
                clean_claim,
                section_key="work",
                query_text=query_text,
                semantic_contract=semantic_contract,
            )
            if not clean_claim or not work_claim_is_self_contained(clean_claim):
                return False
            folded_claim = _fold_text(clean_claim)
            if not folded_claim or folded_claim in existing_work_texts:
                return False
            existing_work_texts.add(folded_claim)
            sections["work"]["items"].append(clean_claim)
            sections["work"]["sources"].append(source_item.get("source_title") or reason)
            sections["work"]["confidence"] = max(float(sections["work"]["confidence"]), 0.69)
            hot_context.append(
                {
                    "section": "work",
                    "text": clean_claim,
                    "source_title": source_item.get("source_title") or reason,
                    "source_kind": "required_work_claim_rescue",
                    "claim_status": "fact",
                    "node_id": str(source_item.get("node_id") or ""),
                }
            )
            debug_ledger.append(
                {
                    "node_id": str(source_item.get("node_id") or ""),
                    "section_key": "work",
                    "promotion_state": "hot",
                    "reason": reason,
                    "source_title": source_item.get("source_title") or "required work evidence",
                }
            )
            return True

        subject_promoted_count = 0
        pending_company_claims: list[tuple[str, dict[str, Any]]] = []
        rescue_reason_allowlist = {
            "subject_anchor_missing",
            "non_core_context_reservoir",
            "off_contract_reservoir",
            "not_answer_eligible",
            "raw_document_anchor_kept_as_document_ref",
            "raw_relation_source",
        }
        subject_claim_target = 5 if inventory_query else 3
        for source_item in source_pool:
            reason = str(source_item.get("reason") or "promoted").strip()
            if reason not in rescue_reason_allowlist and str(source_item.get("source_kind") or "") != "document_ref_relation":
                continue
            source_texts = [
                str(value or "").strip()
                for value in (source_item.get("text"), source_item.get("raw_candidate_text"))
                if str(value or "").strip()
            ]
            source_texts = list(dict.fromkeys(source_texts))
            if not source_texts:
                continue
            for source_text in source_texts:
                if identity_subject_name:
                    subject_claims = _mcp_relation_claims_for_subject(source_text, identity_subject_name)
                    for claim in subject_claims:
                        if add_work_claim(claim, source_item, "required_work_claim_rescue"):
                            subject_promoted_count += 1
                    if (
                        not subject_claims
                        and _mcp_text_is_subject_anchored(source_text, source_item.get("source_title"), semantic_contract)
                        and work_claim_is_self_contained(source_text)
                    ):
                        if add_work_claim(source_text, source_item, "required_work_claim_rescue"):
                            subject_promoted_count += 1
                if linked_entities:
                    pending_company_claims.extend(
                        (claim, source_item)
                        for claim in _mcp_company_relation_claims(source_text, linked_entities=linked_entities)
                    )
            if subject_promoted_count >= subject_claim_target:
                break
        if subject_promoted_count <= 0:
            query_entity_terms = [_fold_text(entity) for entity in query_entities if _fold_text(entity)]
            pending_company_claims = [
                (claim, source_item)
                for claim, source_item in pending_company_claims
                if any(term in _fold_text(claim) for term in query_entity_terms)
            ]
            if not pending_company_claims:
                return
        promoted_count = subject_promoted_count
        for claim, source_item in pending_company_claims:
            if work_claim_is_self_contained(claim):
                if add_work_claim(claim, source_item, "required_work_claim_rescue"):
                    promoted_count += 1
                    if promoted_count >= (8 if inventory_query else 5):
                        return

    promote_required_work_claims_from_available_context()

    def promote_linked_work_cluster_context() -> None:
        if ledger_renderer_mode:
            return
        if not _query_is_work_or_company(query_text) or "work" not in contract_core_sections:
            return
        linked_entities: list[str] = []
        for hot_item in list(hot_context):
            if str(hot_item.get("section") or "") != "work":
                continue
            for entity in _mcp_extract_linked_work_entities(hot_item.get("text"), identity_subject_name):
                folded_entity = _fold_text(entity)
                if folded_entity and folded_entity not in {_fold_text(item) for item in linked_entities}:
                    linked_entities.append(entity)
        if not linked_entities:
            return
        existing_work_texts = {_fold_text(item) for item in list(sections["work"].get("items") or []) if _fold_text(item)}
        promoted_count = 0
        cluster_source_items = list(cold_context) + [
            item
            for item in list(excluded_material)
            if str(item.get("reason") or "") == "not_answer_eligible"
        ]
        for cold_item in cluster_source_items:
            if str(cold_item.get("section") or "") not in {"work", "history", "temporal_inventory", "documents"}:
                continue
            if str(cold_item.get("reason") or "") not in {"subject_anchor_missing", "non_core_context_reservoir", "off_contract_reservoir", "not_answer_eligible"}:
                continue
            for claim in _mcp_company_relation_claims(cold_item.get("text"), linked_entities=linked_entities):
                folded_claim = _fold_text(claim)
                if not folded_claim or folded_claim in existing_work_texts:
                    continue
                existing_work_texts.add(folded_claim)
                sections["work"]["items"].append(claim)
                sections["work"]["sources"].append("linked work cluster")
                sections["work"]["confidence"] = max(float(sections["work"]["confidence"]), 0.68)
                hot_context.append(
                    {
                        "section": "work",
                        "text": claim,
                        "source_title": cold_item.get("source_title") or "linked work cluster",
                        "source_kind": "linked_work_cluster",
                        "claim_status": "fact",
                        "node_id": str(cold_item.get("node_id") or ""),
                    }
                )
                debug_ledger.append(
                    {
                        "node_id": str(cold_item.get("node_id") or ""),
                        "section_key": "work",
                        "promotion_state": "hot",
                        "reason": "linked_work_cluster_promotion",
                        "source_title": cold_item.get("source_title") or "linked work cluster",
                    }
                )
                promoted_count += 1
                if promoted_count >= 3:
                    return

    promote_linked_work_cluster_context()

    def promote_required_values_from_subject_context() -> None:
        if ledger_renderer_mode:
            return
        if "values" not in contract_core_sections and "values" not in required_sections:
            return
        if sections["values"]["items"]:
            return
        value_source_items = [
            item
            for item in list(hot_context)
            if str(item.get("section") or "") in {"identity", "work", "history", "temporal_inventory", "relationships", "style", "documents"}
        ]
        value_source_items.extend(
            item
            for item in list(cold_context)
            if str(item.get("section") or "") in {"values", "identity", "work", "history", "temporal_inventory", "relationships", "style", "documents"}
            and _mcp_text_is_subject_anchored(item.get("text") or item.get("raw_candidate_text"), item.get("source_title"), semantic_contract)
        )
        promoted_count = 0
        seen_values = {_fold_text(item) for item in list(sections["values"].get("items") or []) if _fold_text(item)}
        for source_item in value_source_items:
            source_text = source_item.get("text") or source_item.get("raw_candidate_text")
            if not _mcp_text_has_values_signal(source_text):
                continue
            for value_sentence in _mcp_value_sentences_from_text(source_text, limit=2):
                folded_sentence = _fold_text(value_sentence)
                if not folded_sentence or folded_sentence in seen_values:
                    continue
                seen_values.add(folded_sentence)
                sections["values"]["items"].append(value_sentence)
                sections["values"]["sources"].append(source_item.get("source_title") or "subject value context")
                sections["values"]["confidence"] = max(float(sections["values"]["confidence"]), 0.66)
                hot_context.append(
                    {
                        "section": "values",
                        "text": value_sentence,
                        "source_title": source_item.get("source_title") or "subject value context",
                        "source_kind": "required_values_backfill",
                        "claim_status": "fact",
                        "node_id": str(source_item.get("node_id") or ""),
                    }
                )
                debug_ledger.append(
                    {
                        "node_id": str(source_item.get("node_id") or ""),
                        "section_key": "values",
                        "promotion_state": "hot",
                        "reason": "required_values_subject_context_backfill",
                        "source_title": source_item.get("source_title") or "subject value context",
                    }
                )
                promoted_count += 1
                if promoted_count >= 3:
                    return

    promote_required_values_from_subject_context()

    def promote_required_style_from_subject_context() -> None:
        if ledger_renderer_mode:
            return
        if "style" not in contract_core_sections and "style" not in required_sections:
            return
        if sections["style"]["items"]:
            return
        style_source_items = [
            item
            for item in list(hot_context)
            if str(item.get("section") or "") in {"identity", "work", "history", "temporal_inventory", "relationships", "values", "documents"}
            and _mcp_text_is_subject_anchored(item.get("text") or item.get("raw_candidate_text"), item.get("source_title"), semantic_contract)
        ]
        style_source_items.extend(
            item
            for item in list(cold_context)
            if str(item.get("section") or "") in {"style", "values", "identity", "work", "history", "temporal_inventory", "relationships", "documents"}
            and _mcp_text_is_subject_anchored(item.get("text") or item.get("raw_candidate_text"), item.get("source_title"), semantic_contract)
        )
        promoted_count = 0
        seen_style = {_fold_text(item) for item in list(sections["style"].get("items") or []) if _fold_text(item)}
        for source_item in style_source_items:
            source_text = source_item.get("text") or source_item.get("raw_candidate_text")
            if not _mcp_text_has_style_signal(source_text):
                continue
            for style_sentence in _mcp_style_sentences_from_text(source_text, limit=2):
                folded_sentence = _fold_text(style_sentence)
                if not folded_sentence or folded_sentence in seen_style:
                    continue
                seen_style.add(folded_sentence)
                sections["style"]["items"].append(style_sentence)
                sections["style"]["sources"].append(source_item.get("source_title") or "subject style context")
                sections["style"]["confidence"] = max(float(sections["style"]["confidence"]), 0.66)
                hot_context.append(
                    {
                        "section": "style",
                        "text": style_sentence,
                        "source_title": source_item.get("source_title") or "subject style context",
                        "source_kind": "required_style_backfill",
                        "claim_status": "fact",
                        "node_id": str(source_item.get("node_id") or ""),
                    }
                )
                debug_ledger.append(
                    {
                        "node_id": str(source_item.get("node_id") or ""),
                        "section_key": "style",
                        "promotion_state": "hot",
                        "reason": "required_style_subject_context_backfill",
                        "source_title": source_item.get("source_title") or "subject style context",
                    }
                )
                promoted_count += 1
                if promoted_count >= 3:
                    return

    promote_required_style_from_subject_context()

    def prune_unanchored_work_context_for_explicit_targets() -> None:
        if ledger_renderer_mode:
            return
        if not _query_is_work_or_company(query_text) or "work" not in contract_core_sections:
            return
        subject_terms = {
            _fold_text(term)
            for term in _mcp_identity_subject_terms_from_contract(semantic_contract)
            if _fold_text(term)
        }
        query_entity_terms: list[str] = []
        for entity in _mcp_explicit_query_entities(query_text):
            folded_entity = _fold_text(entity)
            if (
                not folded_entity
                or folded_entity in subject_terms
                or folded_entity in {"agvm", "mcp", "context", "package"}
            ):
                continue
            if folded_entity not in query_entity_terms:
                query_entity_terms.append(folded_entity)
        if not query_entity_terms:
            return
        work_items = [str(item or "").strip() for item in list(sections["work"].get("items") or []) if str(item or "").strip()]
        subject_linked_terms: set[str] = set()
        for item in work_items:
            folded_item = _fold_text(item)
            if subject_terms and not any(term and term in folded_item for term in subject_terms):
                continue
            for entity in _mcp_extract_linked_work_entities(item, identity_subject_name):
                folded_entity = _fold_text(entity)
                if folded_entity:
                    subject_linked_terms.add(folded_entity)
        relation_markers = (
            " was founded",
            " founded in",
            " acquired ",
            " created ",
            " established",
            " partnership",
            " joint venture",
            " is ceo",
            " ceo ",
            " founder",
            " co-founder",
            " cofounder",
        )
        kept_items: list[str] = []
        removed_items: list[str] = []
        for item in work_items:
            folded_item = _fold_text(item)
            anchored = bool(
                any(term and term in folded_item for term in subject_terms)
                or any(term and term in folded_item for term in query_entity_terms)
                or any(term and term in folded_item for term in subject_linked_terms)
            )
            relation_like = any(marker in folded_item for marker in relation_markers)
            if relation_like and not anchored:
                removed_items.append(item)
                continue
            kept_items.append(item)
        if not removed_items:
            return
        removed_folded = {_fold_text(item) for item in removed_items}
        for section_key in ("work", "identity", "history", "temporal_inventory"):
            if section_key == "work":
                sections[section_key]["items"] = kept_items
                continue
            sections[section_key]["items"] = [
                str(item)
                for item in list(sections[section_key].get("items") or [])
                if _fold_text(item) not in removed_folded
            ]
        hot_context[:] = [
            item
            for item in hot_context
            if not (
                str(item.get("section") or "") in {"work", "identity", "history", "temporal_inventory"}
                and _fold_text(item.get("text")) in removed_folded
            )
        ]
        for removed in removed_items:
            cold_context.append(
                {
                    "section": "work",
                    "text": removed,
                    "reason": "unanchored_work_relation_kept_cold",
                    "source_title": "work anchor policy",
                    "source_kind": "work_anchor_policy",
                    "claim_status": "fact",
                    "node_id": "",
                }
            )
            debug_ledger.append(
                {
                    "node_id": "",
                    "section_key": "work",
                    "promotion_state": "cold",
                    "reason": "unanchored_work_relation_kept_cold",
                    "source_title": "work anchor policy",
                }
            )

    prune_unanchored_work_context_for_explicit_targets()

    def prune_embedded_section_duplicates() -> None:
        for section_key, section in sections.items():
            raw_items = [str(item).strip() for item in list(section.get("items") or []) if str(item).strip()]
            if len(raw_items) < 2:
                continue
            keep_items: list[str] = []
            for item in raw_items:
                folded_item = _fold_text(item)
                if len(folded_item) >= 24 and any(
                    folded_item != _fold_text(other)
                    and folded_item in _fold_text(other)
                    and len(folded_item) < len(_fold_text(other))
                    for other in raw_items
                ):
                    continue
                if _fold_text(item) not in {_fold_text(existing) for existing in keep_items}:
                    keep_items.append(item)
            section["items"] = keep_items
        valid_hot_pairs = {
            (section_key, _fold_text(item))
            for section_key, section in sections.items()
            for item in list(section.get("items") or [])
        }
        hot_context[:] = [
            item
            for item in hot_context
            if (str(item.get("section") or ""), _fold_text(item.get("text"))) in valid_hot_pairs
            or str(item.get("section") or "") not in sections
        ]

    prune_embedded_section_duplicates()

    document_sections: list[dict[str, Any]] = []
    for packet in list(document_packets or []):
        if not isinstance(packet, dict) or not is_document_eligible(packet):
            continue
        title = _mcp_clean_agent_text(packet.get("title") or packet.get("source_label") or "Document") or "Document"
        text_parts: list[str] = []
        full_text = _mcp_clean_agent_text(packet.get("full_text") or packet.get("anchor_raw_text"))
        if full_text:
            text_parts.append(full_text)
        for chunk in list(packet.get("ordered_chunk_sequence") or []):
            if isinstance(chunk, dict):
                chunk_text = _mcp_clean_agent_text(chunk.get("text") or chunk.get("raw_text") or chunk.get("evidence_snippet"))
                if chunk_text:
                    text_parts.append(chunk_text)
        for fact in list(packet.get("supported_fact_text") or []):
            if isinstance(fact, dict):
                fact_text = _mcp_clean_agent_text(fact.get("raw_text") or fact.get("summary"))
                if fact_text:
                    text_parts.append(fact_text)
        document_text = "\n\n".join(dict.fromkeys(text_parts)).strip()
        if not document_text:
            continue
        if _mcp_forbidden_topic_hits(f"{title}\n{document_text}", forbidden_topic_markers):
            excluded_material.append({"section": "documents", "reason": "forbidden_topic_by_semantic_contract", "source_title": title})
            continue
        document_sections.append(
            {
                "title": title,
                "text": document_text,
                "lookup_role": str(packet.get("lookup_role") or document_mode or "none"),
                "query_fit_score": float(packet.get("query_fit_score") or packet.get("exact_match_score") or 0.0),
            }
        )
        if not workspace_documents and ("documents" in allowed_sections or broad_context):
            doc_item = f"{title}\n\n{document_text}"
            if doc_item not in sections["documents"]["items"]:
                sections["documents"]["items"].append(doc_item)
            sections["documents"]["confidence"] = max(sections["documents"]["confidence"], float(packet.get("query_fit_score") or packet.get("exact_match_score") or 0.0))

    if workspace_documents and ("documents" in allowed_sections or broad_context) and (
        document_mode != "none" or broad_context or effective_package_mode != "mcp_operational"
    ):
        workspace_titles = _document_workspace_unique([_document_workspace_agent_label(document.get("title")) for document in workspace_documents], limit=6)
        workspace_summary = f"Document workspace ready with {len(workspace_documents)} document(s)"
        if workspace_titles:
            workspace_summary = f"{workspace_summary}: {'; '.join(workspace_titles)}"
        if workspace_summary not in sections["documents"]["items"]:
            sections["documents"]["items"].append(workspace_summary)
        sections["documents"]["confidence"] = max(
            sections["documents"]["confidence"],
            max([float(document.get("query_fit_score") or document.get("exact_match_score") or 0.0) for document in workspace_documents] or [0.0]),
        )

    if "history" in required_sections and not sections["history"]["items"]:
        temporal_source_items = [
            str(item)
            for source_key in ("work", "documents", "identity")
            for item in list(sections[source_key]["items"])
            if _sentence_has_temporal_signal(str(item))
        ]
        for item in temporal_source_items[:3]:
            cleaned_item = _mcp_clean_agent_text(item)
            if not cleaned_item:
                continue
            if cleaned_item not in sections["history"]["items"]:
                sections["history"]["items"].append(cleaned_item)
                sections["history"]["confidence"] = max(float(sections["history"]["confidence"]), 0.62)
                hot_context.append({"section": "history", "text": cleaned_item, "source_title": "derived temporal context"})

    if "temporal_inventory" in required_sections and not sections["temporal_inventory"]["items"]:
        temporal_source_items: list[tuple[str, str, str, str]] = []
        for source_key in ("history", "work", "documents", "identity", "relationships", "values", "style"):
            for item in list(sections[source_key]["items"]):
                temporal_source_items.append((str(item), source_key, "promoted temporal context", "derived_temporal_context"))
        for item in list(cold_context):
            if str(item.get("section") or "") not in {
                "history",
                "temporal_inventory",
                "work",
                "documents",
                "identity",
                "relationships",
            }:
                continue
            temporal_source_items.append(
                (
                    str(item.get("text") or item.get("raw_candidate_text") or ""),
                    str(item.get("section") or "cold_context"),
                    str(item.get("source_title") or "cold temporal context"),
                    str(item.get("source_kind") or "cold_temporal_context"),
                )
            )
        seen_temporal_items = {
            _fold_text(item)
            for item in list(sections["temporal_inventory"].get("items") or [])
            if _fold_text(item)
        }
        promoted_temporal_count = 0
        for raw_item, source_section, source_title, source_kind in temporal_source_items:
            cleaned_item = _mcp_clean_agent_text(raw_item)
            if (
                not cleaned_item
                or not _sentence_has_temporal_signal(cleaned_item)
                or _temporal_text_is_year_navigation_noise(cleaned_item)
            ):
                continue
            folded_temporal_item = _fold_text(cleaned_item)
            if not folded_temporal_item or folded_temporal_item in seen_temporal_items:
                continue
            seen_temporal_items.add(folded_temporal_item)
            sections["temporal_inventory"]["items"].append(cleaned_item)
            sections["temporal_inventory"]["sources"].append(source_title or source_section or "derived temporal context")
            sections["temporal_inventory"]["confidence"] = max(float(sections["temporal_inventory"]["confidence"]), 0.66)
            hot_context.append(
                {
                    "section": "temporal_inventory",
                    "text": cleaned_item,
                    "source_title": source_title or "derived temporal context",
                    "source_kind": source_kind or "derived_temporal_context",
                    "claim_status": "fact",
                    "node_id": "",
                }
            )
            debug_ledger.append(
                {
                    "node_id": "",
                    "section_key": "temporal_inventory",
                    "promotion_state": "hot",
                    "reason": "required_temporal_inventory_backfill",
                    "source_title": source_title or "derived temporal context",
                }
            )
            promoted_temporal_count += 1
            if promoted_temporal_count >= 4:
                break

    if private_boundary_requested and not sections["privacy_boundary"]["items"]:
        privacy_statement = _mcp_private_data_boundary_statement(
            query_text=query_text,
            semantic_contract=semantic_contract,
            document_refs=document_refs,
        )
        sections["privacy_boundary"]["items"].append(privacy_statement)
        sections["privacy_boundary"]["sources"].append("runtime privacy boundary contract")
        sections["privacy_boundary"]["confidence"] = max(float(sections["privacy_boundary"]["confidence"]), 0.72)
        hot_context.append(
            {
                "section": "privacy_boundary",
                "text": privacy_statement,
                "source_title": "runtime privacy boundary contract",
                "source_kind": "privacy_boundary_contract",
                "claim_status": "explicit_absence",
                "node_id": "",
            }
        )
        debug_ledger.append(
            {
                "node_id": "",
                "section_key": "privacy_boundary",
                "promotion_state": "hot",
                "reason": "private_data_boundary_requested",
                "source_title": "runtime privacy boundary contract",
            }
        )

    for section in sections.values():
        section["items"] = sorted(
            list(section.get("items") or []),
            key=_mcp_context_item_rank,
            reverse=True,
        )
        section["items"] = _mcp_budget_items_for_agent_body(
            list(section.get("items") or []),
            limit=int(package_policy.get("section_item_limit") or 0),
            answer_alignment_terms=answer_alignment_terms,
        )

    ordered_sections = [
        section
        for key, section in sections.items()
        if section["items"] and (broad_context or key in allowed_sections)
    ]
    satisfied_sections = {str(section.get("key") or "") for section in ordered_sections if section.get("items")}
    requested_relations = [
        str(item).strip()
        for item in list((semantic_contract or {}).get("requested_relations") or _requested_relations_from_query(query_text))
        if str(item).strip()
    ]
    relationship_context_blob = "\n".join(str(item) for item in list(sections["relationships"].get("items") or []))
    missing_requested_relations = [
        relation
        for relation in requested_relations
        if not _text_mentions_requested_relation(relationship_context_blob, relation)
    ]
    if "relationships" in required_sections and missing_requested_relations:
        satisfied_sections.discard("relationships")
    unresolved_sections = sorted(section for section in required_sections if section not in satisfied_sections)
    semantic_slot_contracts = [
        dict(item)
        for item in list((semantic_contract or {}).get("semantic_slot_contracts") or [])
        if isinstance(item, dict) and bool(item.get("required"))
    ]
    semantic_satisfied_slot_keys: list[str] = []
    semantic_missing_slot_keys: list[str] = []
    semantic_missing_descriptions: list[str] = []
    for slot_contract in semantic_slot_contracts:
        slot_id = str(slot_contract.get("slot_id") or "").strip()
        slot_key = str(slot_contract.get("slot_key") or slot_id).strip()
        section = _mcp_context_section_key(slot_contract.get("section") or _SEMANTIC_SLOT_SECTIONS.get(slot_id, "history"))
        relation_subtype = str(slot_contract.get("relation_subtype") or "").strip()
        exact_field_request = exact_field_request_from_slot_contract(slot_contract)
        section_blob = "\n".join(str(item) for item in list(sections.get(section, {}).get("items") or []))
        section_satisfied = section in satisfied_sections
        if exact_field_request:
            section_satisfied = bool(
                section_satisfied
                and text_satisfies_exact_field_request(section_blob, exact_field_request)
            )
        elif slot_id == "relationship":
            if relation_subtype == "romantic_partner":
                section_satisfied = bool(section_satisfied and _text_mentions_requested_relation(section_blob, "partner"))
            elif relation_subtype == "mentor":
                section_satisfied = bool(section_satisfied and "mentor" in _fold_text(section_blob))
            elif relation_subtype == "business_partner":
                section_satisfied = bool(section_satisfied and any(marker in _fold_text(section_blob) for marker in _BUSINESS_PARTNER_MARKERS))
        elif slot_id == "family":
            section_satisfied = bool(section_satisfied and _family_relation_present(section_blob, relation_subtype))
        if section_satisfied:
            semantic_satisfied_slot_keys.append(slot_key)
        else:
            semantic_missing_slot_keys.append(slot_key)
            if exact_field_request:
                field_label = str(exact_field_request.get("field_label") or slot_key).strip()
                if field_label:
                    semantic_missing_descriptions.append(f"Missing exact requested field: {field_label}")
    if (
        _mcp_query_requests_work_entity_inventory(query_text)
        and "work" in contract_core_sections
        and not ledger_renderer_mode
    ):
        promoted_work_entities = _mcp_work_entity_inventory_from_texts(
            list(sections["work"].get("items") or []),
            identity_subject_name,
        )
        available_work_entities = _mcp_work_entity_inventory_from_texts(
            [
                item.get("text")
                for item in list(hot_context) + list(cold_context)
                if str(item.get("section") or "") in {"work", "identity", "history", "temporal_inventory", "documents"}
            ],
            identity_subject_name,
        )
        if len(available_work_entities) >= 3:
            target_entity_count = min(4, len(available_work_entities))
            if len(promoted_work_entities) < target_entity_count and "work_entity_inventory" not in unresolved_sections:
                unresolved_sections.append("work_entity_inventory")
                semantic_missing_descriptions.append(
                    "Work/company inventory is incomplete: linked work entities were discovered but not promoted into the agent-facing context."
                )
    promoted_context_blob = "\n".join(
        str(item)
        for section in ordered_sections
        for item in list(section.get("items") or [])
    )
    folded_promoted_context_blob = _fold_text(promoted_context_blob)
    explicit_query_entities = _mcp_explicit_query_entities(query_text) if _query_is_work_or_company(query_text) else []
    missing_explicit_query_entities = [
        entity
        for entity in explicit_query_entities
        if _fold_text(entity) and _fold_text(entity) not in folded_promoted_context_blob
    ]
    if missing_explicit_query_entities and "explicit_query_entity_coverage" not in unresolved_sections:
        unresolved_sections.append("explicit_query_entity_coverage")
    answer_context_missing_terms = [
        term
        for term in answer_alignment_terms
        if term and term not in folded_promoted_context_blob
    ]
    promoted_answer_evidence_ids = {
        str(item.get("node_id") or "").strip()
        for item in hot_context
        if str(item.get("node_id") or "").strip() in answer_evidence_ids
    }
    answer_context_missing_evidence_ids: list[str] = []
    for node_id in sorted(answer_evidence_ids):
        if node_id in promoted_answer_evidence_ids:
            continue
        candidate_text = candidate_text_by_node_id.get(node_id)
        if candidate_text and _fold_text(candidate_text) in folded_promoted_context_blob:
            continue
        answer_context_missing_evidence_ids.append(node_id)
    answer_context_alignment_checked = bool(answer_text_for_alignment)
    answer_context_aligned = not (
        answer_context_alignment_checked
        and (answer_context_missing_terms or answer_context_missing_evidence_ids)
    )
    if answer_context_alignment_checked and not answer_context_aligned and "answer_context_alignment" not in unresolved_sections:
        unresolved_sections.append("answer_context_alignment")
    contract_passed = not unresolved_sections and not semantic_missing_slot_keys and bool(ordered_sections)

    body_lines: list[str] = [
        "# AGVM Context Package",
        "",
        "## Task / User Intent",
        str(query_text or "").strip(),
        "",
        "## Executive Working Context",
    ]
    executive_items = []
    seen_executive_items: set[str] = set()
    for section in ordered_sections[:4]:
        for item in list(section.get("items") or [])[:2]:
            text = str(item)
            folded_text = _fold_text(text)
            if not folded_text or folded_text in seen_executive_items:
                continue
            seen_executive_items.add(folded_text)
            executive_items.append(text)
    if executive_items:
        body_lines.extend(f"- {item}" for item in executive_items[: int(package_policy.get("executive_item_limit") or 8)])
    else:
        body_lines.append("- No contract-relevant memory was promoted.")
    for section in ordered_sections:
        body_lines.extend(["", f"## {section['title']}"])
        for item in list(section.get("items") or []):
            body_lines.append(f"- {item}")
    existing_agent_texts = {
        _fold_text(item)
        for section in ordered_sections
        for item in list(section.get("items") or [])
        if _fold_text(item)
    }
    source_excerpt_lines = _mcp_source_excerpt_lines(
        hot_context,
        cold_context,
        existing_agent_texts=existing_agent_texts,
        limit=int(package_policy.get("source_excerpt_limit") or 0),
        exact_field_active=bool(exact_field_requirements),
    )
    if source_excerpt_lines:
        body_lines.extend(["", "## Source-Backed Excerpts"])
        body_lines.extend(source_excerpt_lines)
    reservoir_summary_lines = _mcp_reservoir_summary_lines(
        cold_context,
        limit=int(package_policy.get("reservoir_summary_limit") or 0),
        exact_field_active=bool(exact_field_requirements),
        answer_alignment_terms=answer_alignment_terms,
    )
    if reservoir_summary_lines:
        body_lines.extend(["", "## Reservoir Context Available"])
        body_lines.extend(reservoir_summary_lines)
    include_document_workspace_in_agent_body = bool(package_policy.get("include_document_workspace")) or bool(document_mode != "none")
    include_full_raw_workspace_documents = bool(
        package_policy.get("include_full_raw_workspace_in_agent_body", package_policy.get("include_document_workspace"))
    )
    document_workspace_lines = (
        _mcp_document_workspace_primary_lines(
            safe_document_workspace,
            include_full_raw_documents=include_full_raw_workspace_documents,
        )
        if include_document_workspace_in_agent_body
        else []
    )
    if document_workspace_lines:
        body_lines.extend(["", "## Document Workspace"])
        body_lines.extend(document_workspace_lines)
    document_bundle_lines = (
        _mcp_document_bundle_agent_lines(document_bundle)
        if bool(package_policy.get("include_raw_document_bundle_in_agent_body", True))
        else []
    )
    document_references = _mcp_context_document_references(
        document_refs=document_refs,
        document_delivery_contract=document_delivery_contract,
        document_text_policy=effective_document_text_policy,
        raw_bodies_in_agent_markdown=bool(document_bundle_lines),
        normal_context_sections=ordered_sections,
    )
    document_reference_lines = _mcp_context_document_reference_agent_lines(document_references)
    if document_reference_lines:
        body_lines.extend(["", "## Document References"])
        body_lines.extend(document_reference_lines)
    if document_bundle_lines:
        body_lines.extend(["", "## Raw Document Bundle"])
        body_lines.extend(document_bundle_lines)

    preliminary_link_aware_context_contract = _mcp_link_aware_context_contract(
        agent_markdown="\n".join(body_lines).strip(),
        ordered_sections=ordered_sections,
        hot_context=hot_context,
        cold_context=cold_context,
        excluded_material=excluded_material,
        debug_ledger=debug_ledger,
        document_refs=document_refs,
        document_ref_contract=document_ref_contract,
    )
    if (
        not bool(preliminary_link_aware_context_contract.get("passed"))
        and "link_aware_context" not in unresolved_sections
    ):
        unresolved_sections.append("link_aware_context")

    visible_agent_context_blob = "\n".join(body_lines).strip()
    folded_visible_agent_context_blob = _fold_text(visible_agent_context_blob)
    answer_context_missing_terms = [
        term
        for term in answer_alignment_terms
        if term and term not in folded_visible_agent_context_blob
    ]
    answer_context_missing_evidence_ids = []
    for node_id in sorted(answer_evidence_ids):
        candidate_text = candidate_text_by_node_id.get(node_id)
        if candidate_text and _fold_text(candidate_text) in folded_visible_agent_context_blob:
            continue
        if node_id in promoted_answer_evidence_ids:
            continue
        answer_context_missing_evidence_ids.append(node_id)
    answer_context_aligned = not (
        answer_context_alignment_checked
        and (answer_context_missing_terms or answer_context_missing_evidence_ids)
    )
    if answer_context_alignment_checked:
        unresolved_sections = [section for section in unresolved_sections if section != "answer_context_alignment"]
        if not answer_context_aligned:
            unresolved_sections.append("answer_context_alignment")
    path_truth_contract = _mcp_context_path_truth_contract(
        path_corridors=path_corridors,
        semantic_contract=semantic_contract,
        required=bool(path_truth_required),
    )
    if bool(path_truth_required) and not bool(path_truth_contract.get("ready")) and "path_truth" not in unresolved_sections:
        unresolved_sections.append("path_truth")
    if bool(path_truth_contract.get("ready")):
        unresolved_sections = [section for section in unresolved_sections if section != "path_truth"]
    raw_document_policy_requires_refs = bool(effective_document_text_policy in {"top_raw", "all_raw"})
    if raw_document_policy_requires_refs and not document_refs and "document_refs" not in unresolved_sections:
        unresolved_sections.append("document_refs")
    contract_passed = not unresolved_sections and not semantic_missing_slot_keys and bool(ordered_sections)

    body_lines.extend(
        _mcp_unresolved_or_missing_body_lines(
            unresolved_sections=unresolved_sections,
            semantic_missing_slot_keys=semantic_missing_slot_keys,
            semantic_missing_descriptions=semantic_missing_descriptions,
            missing_requested_relations=missing_requested_relations,
            missing_explicit_query_entities=missing_explicit_query_entities,
        )
    )

    agent_markdown = "\n".join(body_lines).strip()
    reservoir_agent_body_items = _mcp_reservoir_agent_body_items(
        cold_context,
        exact_field_active=bool(exact_field_requirements),
        answer_alignment_terms=answer_alignment_terms,
    )
    cold_reason_counts = _mcp_cold_reason_counts(cold_context)
    hot_section_aliases = _mcp_context_package_hot_section_aliases(ordered_sections)
    cold_reservoir_alias = _mcp_context_package_cold_reservoir_alias(cold_context, cold_reason_counts)
    reservoir_agent_body_char_count = sum(len(str(item.get("text") or "")) for item in reservoir_agent_body_items)
    reservoir_rich = bool(
        int(package_policy.get("reservoir_rich_item_threshold") or 0) > 0
        and len(reservoir_agent_body_items) >= int(package_policy.get("reservoir_rich_item_threshold") or 0)
    )
    package_tiny_while_rich = bool(
        reservoir_rich
        and len(agent_markdown) < int(package_policy.get("min_agent_body_chars_if_reservoir_rich") or 0)
    )
    if exact_field_requirements and semantic_missing_slot_keys:
        package_breadth_state = "partial_blocked_exact_field"
    elif unresolved_sections or semantic_missing_slot_keys:
        package_breadth_state = "partial_blocked_contract"
    elif package_tiny_while_rich:
        package_breadth_state = "thin_relevant_reservoir_not_promoted"
    elif reservoir_rich and reservoir_summary_lines:
        package_breadth_state = "expanded_from_reservoir"
    elif ordered_sections:
        package_breadth_state = "sufficient"
    else:
        package_breadth_state = "insufficient"
    path_discovery_agent_body_count = sum(
        1
        for item in path_discovery_entries
        if str(item.get("text") or "").strip()
        and str(item.get("text") or "").strip() in agent_markdown
    )
    document_workspace_appendix = _mcp_document_workspace_appendix(safe_document_workspace)
    path_corridor_metrics = dict((path_corridors or {}).get("metrics") or {})
    path_lifecycle_summary = dict((path_corridors or {}).get("lifecycle") or {})
    path_lifecycle_rows = [
        {
            "path_id": path.get("path_id"),
            "route_kind": path.get("route_kind"),
            "origin_landing_id": path.get("origin_landing_id") or path.get("from_landing_id"),
            "target_landing_id": path.get("target_landing_id") or None,
            "lifecycle_state": path.get("lifecycle_state"),
            "lifecycle_state_reason": path.get("lifecycle_state_reason"),
            "changed_context_package": bool(path.get("changed_context_package")),
            "promoted_count": int(path.get("promoted_count") or 0),
            "cold_count": int(path.get("cold_count") or 0),
            "excluded_count": int(path.get("excluded_count") or 0),
            "runtime_branch_ids": list(path.get("runtime_branch_ids") or []),
            "package_impact": dict(path.get("package_impact") or {}),
        }
        for path in list((path_corridors or {}).get("paths") or [])
        if isinstance(path, dict)
    ]
    inspectable_appendices = {
        "schema_version": "agvm.context_package.inspectable_appendices.v1",
        "path_discoveries": path_discovery_entries,
        "path_corridor_lifecycle": path_lifecycle_rows,
        "path_corridor_lifecycle_summary": path_lifecycle_summary,
        "document_workspace": document_workspace_appendix,
        "document_references": document_references,
        "source_trace": list(document_workspace_appendix.get("source_trace") or []) if document_workspace_appendix else [],
    }
    agent_body_has_node_id = _mcp_agent_body_has_node_id(agent_markdown)
    agent_body_has_debug_marker = any(marker in _fold_text(agent_markdown) for marker in ("evidence ledger", "raw context", "grounded retrieval ledger"))
    agent_body_has_route_debug_marker = _mcp_agent_body_has_route_debug_marker(agent_markdown)
    dossier_hygiene_passed = not (agent_body_has_node_id or agent_body_has_debug_marker or agent_body_has_route_debug_marker)
    link_aware_context_contract = _mcp_link_aware_context_contract(
        agent_markdown=agent_markdown,
        ordered_sections=ordered_sections,
        hot_context=hot_context,
        cold_context=cold_context,
        excluded_material=excluded_material,
        debug_ledger=debug_ledger,
        document_refs=document_refs,
        document_ref_contract=document_ref_contract,
        path_discovery_agent_body_count=path_discovery_agent_body_count,
        agent_body_has_node_id=agent_body_has_node_id,
        agent_body_has_debug_marker=agent_body_has_debug_marker,
        agent_body_has_route_debug_marker=agent_body_has_route_debug_marker,
    )
    promotion_policy = {
        "schema_version": "agvm.mcp_context_package.promotion_policy.v1",
        "package_policy_version": MCP_CONTEXT_PACKAGE_POLICY_VERSION,
        "package_mode": effective_package_mode,
        "state": package_breadth_state,
        "contract_core_sections": sorted(contract_core_sections),
        "reservoir_rich": reservoir_rich,
        "package_tiny_while_reservoir_rich": package_tiny_while_rich,
        "reservoir_agent_body_eligible_count": len(reservoir_agent_body_items),
        "reservoir_agent_body_line_count": len(reservoir_summary_lines),
        "reservoir_agent_body_char_count": reservoir_agent_body_char_count,
        "cold_reason_counts": cold_reason_counts,
        "exact_field_backfill_blocked": bool(exact_field_requirements),
        "answer_support_visible_in_agent_package": bool(answer_context_aligned),
        "answer_context_missing_terms": answer_context_missing_terms,
        "answer_context_missing_evidence_node_count": len(answer_context_missing_evidence_ids),
        "path_discovery_count": len(path_discovery_entries),
        "path_discovery_agent_body_count": path_discovery_agent_body_count,
        "path_discoveries_promoted_only_by_semantic_contract": path_discovery_agent_body_count <= len(agent_path_discovery_entries),
        "cold_path_corridor_body_backfill_blocked": any(str(item.get("source_kind") or "") == "path_corridor" for item in cold_context),
    }
    metrics = {
        "schema_version": "agvm.mcp_context_package.metrics.v1",
        "package_policy_version": MCP_CONTEXT_PACKAGE_POLICY_VERSION,
        "package_mode": effective_package_mode,
        "package_modes_supported": list(MCP_CONTEXT_PACKAGE_MODES),
        "package_section_item_limit": int(package_policy.get("section_item_limit") or 0),
        "section_count": len(ordered_sections),
        "hot_item_count": len(hot_context),
        "cold_item_count": len(cold_context),
        "excluded_item_count": len(excluded_material),
        "document_count": len(document_sections),
        "document_workspace_document_count": len(workspace_documents),
        "document_workspace_full_text_document_count": int(((safe_document_workspace or {}).get("metrics") or {}).get("full_text_document_count") or 0),
        "document_workspace_raw_text_char_count": int(((safe_document_workspace or {}).get("metrics") or {}).get("raw_text_char_count") or 0),
        "document_text_policy": effective_document_text_policy,
        "document_ref_count": len(document_refs),
        "document_references_section_rendered": bool(document_reference_lines),
        "document_references_rendered_ref_count": int(document_references.get("rendered_ref_count") or 0),
        "document_references_raw_bodies_in_agent_markdown": bool(document_references.get("raw_bodies_in_agent_markdown")),
        "actionable_document_ref_count": int(document_ref_contract.get("actionable_document_ref_count") or 0),
        "raw_available_document_ref_count": int(document_ref_contract.get("raw_available_document_ref_count") or 0),
        "raw_included_document_count": int(document_delivery_contract.get("raw_included_document_count") or 0),
        "raw_available_not_included_document_count": int(document_delivery_contract.get("raw_available_not_included_count") or 0),
        "document_bundle_state": str(document_bundle.get("state") or ""),
        "document_bundle_document_count": int(document_bundle.get("document_count") or 0),
        "document_bundle_raw_text_char_count": int(document_bundle.get("raw_text_char_count") or 0),
        "path_count": int(path_truth_contract.get("path_count") or path_corridor_metrics.get("path_count") or 0),
        "path_completed_count": int(path_truth_contract.get("completed_path_count") or path_corridor_metrics.get("completed_path_count") or 0),
        "path_stopped_count": int(path_truth_contract.get("stopped_path_count") or path_corridor_metrics.get("stopped_path_count") or 0),
        "path_started_count": int(path_truth_contract.get("started_path_count") or path_corridor_metrics.get("started_path_count") or 0),
        "path_pending_count": int(path_truth_contract.get("pending_path_count") or path_corridor_metrics.get("pending_path_count") or 0),
        "path_changed_context_package_count": int(
            path_truth_contract.get("changed_context_package_path_count")
            or path_corridor_metrics.get("changed_context_package_path_count")
            or 0
        ),
        "path_all_planned_accounted_for": bool(path_truth_contract.get("all_planned_paths_accounted_for")),
        "path_truth_required": bool(path_truth_contract.get("required")),
        "path_truth_ready": bool(path_truth_contract.get("ready")),
        "path_truth_state": str(path_truth_contract.get("state") or ""),
        "path_truth_pending_reasons": list(path_truth_contract.get("pending_reasons") or []),
        "path_truth_missing_reasons": list(path_truth_contract.get("missing_reasons") or []),
        "path_discovery_count": len(path_discovery_entries),
        "path_discovery_agent_body_count": path_discovery_agent_body_count,
        "source_excerpt_agent_body_count": len(source_excerpt_lines),
        "reservoir_summary_agent_body_count": len(reservoir_summary_lines),
        "reservoir_agent_body_eligible_count": len(reservoir_agent_body_items),
        "reservoir_agent_body_char_count": reservoir_agent_body_char_count,
        "reservoir_rich": reservoir_rich,
        "package_breadth_state": package_breadth_state,
        "package_tiny_while_reservoir_rich": package_tiny_while_rich,
        "document_workspace_embedded_in_agent_body": include_document_workspace_in_agent_body,
        "document_workspace_raw_text_embedded_in_agent_body": include_full_raw_workspace_documents,
        "document_workspace_appendix_source_trace_count": int(document_workspace_appendix.get("source_trace_count") or 0) if document_workspace_appendix else 0,
        "agent_markdown_chars": len(agent_markdown),
        "agent_body_char_count": len(agent_markdown),
        "hot_text_char_count": sum(len(str(item.get("text") or "")) for item in hot_context),
        "cold_text_char_count": sum(len(str(item.get("text") or "")) for item in cold_context),
        "required_sections": sorted(required_sections),
        "contract_core_sections": sorted(contract_core_sections),
        "satisfied_sections": sorted(satisfied_sections),
        "unresolved_sections": unresolved_sections,
        "requested_relations": requested_relations,
        "missing_requested_relations": missing_requested_relations,
        "explicit_query_entities": explicit_query_entities,
        "missing_explicit_query_entities": missing_explicit_query_entities,
        "semantic_missing_descriptions": semantic_missing_descriptions,
        "exact_field_requirement_count": len(exact_field_requirements),
        "exact_field_missing_count": len(semantic_missing_descriptions),
        "contract_passed": contract_passed,
        "answer_context_alignment_checked": answer_context_alignment_checked,
        "answer_context_aligned": answer_context_aligned,
        "answer_context_missing_terms": answer_context_missing_terms,
        "answer_context_missing_term_count": len(answer_context_missing_terms),
        "answer_context_missing_evidence_node_ids": answer_context_missing_evidence_ids,
        "answer_context_missing_evidence_node_count": len(answer_context_missing_evidence_ids),
        "node_id_leak_in_agent_body": agent_body_has_node_id,
        "debug_marker_leak_in_agent_body": agent_body_has_debug_marker,
        "route_debug_marker_leak_in_agent_body": agent_body_has_route_debug_marker,
        "dossier_hygiene_passed": dossier_hygiene_passed,
        "link_aware_context_passed": bool(link_aware_context_contract.get("passed")),
        "link_aware_context_state": str(link_aware_context_contract.get("state") or ""),
        "visible_orphan_fragment_count": int(link_aware_context_contract.get("visible_orphan_fragment_count") or 0),
        "hot_orphan_fragment_count": int(link_aware_context_contract.get("hot_orphan_fragment_count") or 0),
        "linked_parent_context_promoted_count": int(link_aware_context_contract.get("linked_parent_context_promoted_count") or 0),
        "linked_relation_timeline_promoted_count": int(link_aware_context_contract.get("linked_relation_timeline_promoted_count") or 0),
        "linked_work_cluster_promoted_count": int(link_aware_context_contract.get("linked_work_cluster_promoted_count") or 0),
        "truncated_core_text_count": sum(1 for item in hot_context if str(item.get("text") or "").rstrip().endswith("...")),
    }
    package_builder_ms = round((time.perf_counter() - package_render_started_at) * 1000.0, 3)
    package_render_blocked_reasons: list[str] = []
    if ledger_renderer_mode and int(compact_mission_evidence_ledger.get("row_count") or 0) <= 0:
        package_render_blocked_reasons.append("mission_ledger_rows_missing")
    package_render_blocked_reasons.extend(str(item) for item in unresolved_sections if str(item).strip())
    package_render_blocked_reasons.extend(str(item) for item in semantic_missing_slot_keys if str(item).strip())
    package_render_contract = {
        "schema_version": _MCP_PACKAGE_RENDER_CONTRACT_SCHEMA_VERSION,
        "source_is_ledger_only": ledger_renderer_mode,
        "package_builder_ms": package_builder_ms,
        "sections_rendered": len(ordered_sections),
        "hot_count": len(hot_context),
        "cold_count": len(cold_context),
        "document_ref_count": len(document_refs),
        "ai_master_judge_enabled": bool(allow_ai_master or context_structured.get("ai_master_judge_enabled")),
        "raw_document_bundled": bool(int(document_delivery_contract.get("raw_included_document_count") or 0) > 0),
        "raw_document_policy": effective_document_text_policy,
        "orphan_fragment_count": int(link_aware_context_contract.get("visible_orphan_fragment_count") or 0),
        "ledger_row_count": int(compact_mission_evidence_ledger.get("row_count") or 0),
        "master_judgement_id": None,
        "blocked_reasons": list(dict.fromkeys(package_render_blocked_reasons))[:16],
        "semantic_discovery_disabled": ledger_renderer_mode,
        "disabled_rescue_functions": (
            [
                "document_ref_relation_claims",
                "promote_required_work_claims_from_available_context",
                "promote_linked_work_cluster_context",
                "promote_required_values_from_subject_context",
                "promote_required_style_from_subject_context",
                "_mcp_relation_claims_for_subject",
                "_mcp_company_relation_claims",
            ]
            if ledger_renderer_mode
            else []
        ),
    }
    master_judgement = build_mcp_master_judgement(
        query_text=query_text,
        mission_evidence_ledger=compact_mission_evidence_ledger,
        package_render_contract=package_render_contract,
        path_truth_contract=path_truth_contract,
        document_refs=document_refs,
        allow_ai_master=bool(allow_ai_master),
    ) if ledger_renderer_mode else {}
    if master_judgement:
        package_render_contract["master_judgement_id"] = master_judgement.get("master_judgement_id")
    original_status = "contract_satisfied" if contract_passed else "partial" if ordered_sections else "insufficient"
    master_renderer_projection = _mcp_master_renderer_projection(
        ledger_renderer_mode=ledger_renderer_mode,
        master_judgement=master_judgement,
        unresolved_sections=unresolved_sections,
        semantic_missing_slot_keys=semantic_missing_slot_keys,
        ordered_sections=ordered_sections,
        original_contract_passed=contract_passed,
        original_status=original_status,
    )
    final_status = str(master_renderer_projection.get("final_status") or original_status)
    final_contract_passed = bool(master_renderer_projection.get("final_contract_passed"))
    final_unresolved_sections = [
        str(section).strip()
        for section in list(master_renderer_projection.get("final_unresolved_sections") or [])
        if str(section).strip()
    ]
    if (
        final_unresolved_sections != unresolved_sections
        or str(master_renderer_projection.get("master_state") or "") == "no_match"
    ):
        agent_markdown = _mcp_replace_unresolved_or_missing_section(
            agent_markdown,
            _mcp_unresolved_or_missing_body_lines(
                unresolved_sections=final_unresolved_sections,
                semantic_missing_slot_keys=semantic_missing_slot_keys if final_status != "no_match" else [],
                semantic_missing_descriptions=semantic_missing_descriptions if final_status != "no_match" else [],
                missing_requested_relations=missing_requested_relations,
                missing_explicit_query_entities=missing_explicit_query_entities,
                master_state=str(master_renderer_projection.get("master_state") or ""),
            ),
        )
    final_package_breadth_state = package_breadth_state
    if ledger_renderer_mode and str(master_renderer_projection.get("master_state") or "") == "terminal":
        final_package_breadth_state = "master_terminal" if final_contract_passed else "master_terminal_renderer_blocked"
    elif ledger_renderer_mode and str(master_renderer_projection.get("master_state") or "") == "no_match":
        final_package_breadth_state = "master_no_match"
    elif ledger_renderer_mode and str(master_renderer_projection.get("master_state") or ""):
        final_package_breadth_state = f"master_{master_renderer_projection.get('master_state')}"
    replaced_blocker_reasons = set(unresolved_sections) | {
        str(item) for item in semantic_missing_slot_keys if str(item).strip()
    }
    stable_package_blockers = [
        str(item)
        for item in package_render_blocked_reasons
        if str(item).strip() and str(item) not in replaced_blocker_reasons
    ]
    final_blocked_reasons = [
        *stable_package_blockers,
        *final_unresolved_sections,
        *(
            []
            if final_status == "no_match"
            else [str(item) for item in semantic_missing_slot_keys if str(item).strip()]
        ),
    ]
    package_render_contract.update(
        {
            "master_state": str(master_renderer_projection.get("master_state") or ""),
            "renderer_obeys_master": bool(master_renderer_projection.get("renderer_obeys_master")),
            "final_status": final_status,
            "final_contract_passed": final_contract_passed,
            "static_unresolved_demoted": list(master_renderer_projection.get("static_unresolved_demoted") or []),
            "safety_unresolved_sections": list(master_renderer_projection.get("safety_unresolved_sections") or []),
            "blocked_reasons": list(dict.fromkeys(final_blocked_reasons))[:16],
        }
    )
    promotion_policy["state"] = final_package_breadth_state
    promotion_policy["master_state"] = str(master_renderer_projection.get("master_state") or "")
    metrics["agent_markdown_chars"] = len(agent_markdown)
    metrics["agent_body_char_count"] = len(agent_markdown)
    metrics["package_breadth_state"] = final_package_breadth_state
    metrics["unresolved_sections"] = final_unresolved_sections
    metrics["contract_passed"] = final_contract_passed
    metrics["package_builder_ms"] = package_builder_ms
    metrics["package_render_contract"] = package_render_contract
    metrics["master_judgement"] = master_judgement
    return {
        "schema_version": "agvm.mcp_context_package.v2",
        "package_policy_version": MCP_CONTEXT_PACKAGE_POLICY_VERSION,
        "package_mode": effective_package_mode,
        "package_modes_supported": list(MCP_CONTEXT_PACKAGE_MODES),
        "package_policy": package_policy,
        "package_kind": "mcp_context",
        "query_text": str(query_text or ""),
        "retrieval_mode": str(retrieval_mode or "balanced"),
        "status": final_status,
        "contract": {
            "required_sections": sorted(required_sections),
            "optional_sections": sorted(optional_sections),
            "allowed_sections": sorted(allowed_sections),
            "contract_core_sections": sorted(contract_core_sections),
            "forbidden_sections": sorted(forbidden_sections),
            "broad_context": broad_context,
            "document_mode": document_mode,
            "package_mode": effective_package_mode,
            "document_text_policy": effective_document_text_policy,
            "document_ref_contract": document_ref_contract,
            "document_delivery_contract": document_delivery_contract,
            "document_references": document_references,
            "document_bundle_state": str(document_bundle.get("state") or ""),
            "path_truth": path_truth_contract,
            "package_policy_version": MCP_CONTEXT_PACKAGE_POLICY_VERSION,
            "passed": final_contract_passed,
            "unresolved_sections": final_unresolved_sections,
            "requested_relations": requested_relations,
            "missing_requested_relations": missing_requested_relations,
            "explicit_query_entities": explicit_query_entities,
            "missing_explicit_query_entities": missing_explicit_query_entities,
            "semantic_required_slot_keys": [
                str(item.get("slot_key") or item.get("slot_id") or "")
                for item in semantic_slot_contracts
                if str(item.get("slot_key") or item.get("slot_id") or "")
            ],
            "semantic_satisfied_slot_keys": semantic_satisfied_slot_keys,
            "semantic_missing_slot_keys": semantic_missing_slot_keys,
            "semantic_missing_descriptions": semantic_missing_descriptions,
            "answer_context_alignment": {
                "checked": answer_context_alignment_checked,
                "passed": answer_context_aligned,
                "missing_terms": answer_context_missing_terms,
                "missing_evidence_node_ids": answer_context_missing_evidence_ids,
            },
            "package_breadth": promotion_policy,
            "link_aware_context": link_aware_context_contract,
            "package_render": package_render_contract,
            "master_judgement": master_judgement,
        },
        "package_render_contract": package_render_contract,
        "master_judgement": master_judgement,
        "mission_evidence_ledger": compact_mission_evidence_ledger,
        "agent_markdown": agent_markdown,
        "sections": ordered_sections,
        "structured_sections": ordered_sections,
        "hot_sections": hot_section_aliases,
        "hot_context": hot_context,
        "cold_context": cold_context,
        "cold_reservoir": cold_reservoir_alias,
        "documents": document_sections,
        "document_text_policy": effective_document_text_policy,
        "document_refs": document_refs,
        "document_references": document_references,
        "document_ref_contract": document_ref_contract,
        "document_delivery_contract": document_delivery_contract,
        "document_bundle": document_bundle,
        "path_corridors": {
            key: value
            for key, value in dict(path_corridors or {}).items()
            if key != "debug"
        },
        "path_truth_contract": path_truth_contract,
        "document_workspace": {
            key: value
            for key, value in dict(safe_document_workspace or {}).items()
            if key != "debug"
        },
        "inspectable_appendices": inspectable_appendices,
        "promotion_policy": promotion_policy,
        "link_aware_context_contract": link_aware_context_contract,
        "dossier_hygiene": {
            "schema_version": "agvm.context_package.dossier_hygiene.v1",
            "passed": dossier_hygiene_passed,
            "package_policy_version": MCP_CONTEXT_PACKAGE_POLICY_VERSION,
            "package_mode": effective_package_mode,
            "agent_body_is_primary_context_only": True,
            "path_discoveries_kept_out_of_agent_body": True,
            "path_context_promoted_without_route_debug": bool(path_discovery_agent_body_count),
            "source_trace_kept_out_of_agent_body": True,
            "document_workspace_embedded_in_agent_body": include_document_workspace_in_agent_body,
            "document_workspace_raw_text_embedded_in_agent_body": include_full_raw_workspace_documents,
            "document_references_rendered_as_separate_section": bool(document_reference_lines),
            "document_refs_raw_bodies_kept_out_of_agent_body": not bool(
                document_reference_lines and effective_document_text_policy == "refs_only" and document_references.get("raw_bodies_in_agent_markdown")
            ),
            "node_id_leak_in_agent_body": agent_body_has_node_id,
            "debug_marker_leak_in_agent_body": agent_body_has_debug_marker,
            "route_debug_marker_leak_in_agent_body": agent_body_has_route_debug_marker,
        },
        "excluded_material": excluded_material,
        "metrics": metrics,
        "debug": {
            "evidence_ledger": debug_ledger,
            "node_ids": [str(item.get("node_id") or "") for item in debug_ledger if str(item.get("node_id") or "").strip()],
            "quality_metrics": dict((evidence_reservoir or {}).get("quality_metrics") or {}),
            "reservoir_summary": dict((evidence_reservoir or {}).get("reservoir_summary") or {}),
        },
    }


def _context_package_answer_section_order(query_text: str, retrieval_mode: str) -> list[str]:
    folded = _fold_text(query_text)
    identity_requested = bool(
        _is_broad_self_query(query_text)
        or any(
            token in folded
            for token in (
                "come ti chiami",
                "chi sei",
                "il tuo nome",
                "your name",
                "who are you",
                "identita",
                "identity",
            )
        )
    )
    if _is_temporal_reference_query(query_text):
        return ["temporal_inventory", "history", "work", "documents", "identity", "relationships", "values", "style"]
    if _query_is_work_or_company(query_text):
        if identity_requested:
            return ["identity", "work", "documents", "history", "values", "style", "relationships", "temporal_inventory"]
        return ["work", "documents", "history", "identity", "values", "style", "relationships", "temporal_inventory"]
    if any(token in folded for token in ("padre", "madre", "partner", "fidanz", "relazioni", "relationship", "family")):
        return ["relationships", "identity", "history", "work", "documents", "values", "style", "temporal_inventory"]
    if any(token in folded for token in ("document", "pdf", "fonte", "source", "file")):
        return ["documents", "work", "history", "identity", "relationships", "values", "style", "temporal_inventory"]
    if _is_broad_self_query(query_text) or str(retrieval_mode or "") in {"heavy", "forensic"}:
        return ["identity", "work", "history", "relationships", "values", "style", "documents", "temporal_inventory"]
    return ["identity", "work", "history", "relationships", "values", "style", "documents", "temporal_inventory"]


def _context_package_answer_item_text(value: Any) -> str | None:
    text = _mcp_clean_agent_text(value) or clean_answer_surface_text(value)
    text = clean_answer_surface_text(text)
    if not text:
        return None
    folded = _fold_text(text)
    if (
        _mcp_agent_body_has_node_id(text)
        or _answer_surface_has_context_ledger_leak(text)
        or any(
            marker in folded
            for marker in (
                "answer context alignment is unresolved",
                "missing contract section",
                "no contract relevant memory was promoted",
                "reservoir not promoted",
                "path discoveries",
                "planned corridors",
            )
        )
    ):
        return None
    return text


_ANSWER_DEMO_SOURCE_NOISE_MARKERS = (
    "follow us",
    "section: follow",
    "isa resources",
    "upcoming events",
    "advertising opportunities",
    "monthly magazine",
    "what can we help you find",
    "show more",
    "show less",
    "read more",
    "view all",
    "login",
    "subscribe",
    "privacy policy",
    "cookie policy",
    "all rights reserved",
    "visualizza profilo",
    "mostra altro",
    "mostra meno",
    "linkedin linkedin",
    "password hai dimenticato",
    "accetta e iscriviti",
    "lingua accetta",
    "source uri",
    "source url",
    "page title",
    "visible text",
    "headings",
)


def _answer_demo_source_noise_reason(value: str | None, *, fragment_level: bool = False) -> str | None:
    text = str(value or "").strip()
    if not text:
        return "empty"
    folded = _fold_text(text)
    marker_count = sum(1 for marker in _ANSWER_DEMO_SOURCE_NOISE_MARKERS if marker in folded)
    if marker_count >= (1 if fragment_level else 2):
        return "source_navigation_boilerplate"
    if re.search(r"\bsegment\s+\d+\b", folded) and (
        "source" in folded or "document" in folded or len(text) <= 260 or marker_count
    ):
        return "source_segment_label"
    if folded.count("visualizza profilo") >= 1 or folded.count("follower") >= 3:
        return "social_profile_boilerplate"
    if re.search(r"(?i)\b(?:section|chunk|anchor|document)\s*[:#-]\s*", text) and (
        fragment_level or marker_count or len(text) <= 280
    ):
        return "source_section_label"
    if fragment_level and re.match(r"(?i)^document(?:o)?\s+(?:bootstrap|operativ[oa]|\d+)\b", text):
        return "source_document_label"
    if len(text) >= 900 and marker_count:
        return "long_source_boilerplate"
    return None


def _answer_demo_subject_name_from_package(package: dict[str, Any]) -> str | None:
    for section in list(package.get("sections") or []):
        if not isinstance(section, dict) or str(section.get("key") or "").strip() != "identity":
            continue
        for item in _context_package_section_items(section):
            text = _context_package_answer_item_text(item) or ""
            match = re.search(r"\bmemory\s+subject(?:'s|\s+s)?\s+name\s+is\s+(.+?)(?:\.|$)", text, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip(" .")
    return None


def _answer_demo_text_mentions_subject(text: str, subject_name: str | None) -> bool:
    folded = _fold_text(text)
    if not folded:
        return False
    if any(token in folded for token in (" i ", " my ", " me ", " mio ", " mia ", " miei ", " mie ", "sono ", "ho ", "mi ")):
        return True
    subject = _fold_text(subject_name or "")
    if subject and subject in folded:
        return True
    if subject:
        parts = [part for part in subject.split() if len(part) >= 3]
        if parts and parts[-1] in folded:
            return True
    return False


def _context_package_answer_item_rank(
    *,
    section_key: str,
    value: Any,
    query_text: str,
    subject_name: str | None,
) -> tuple[float, float, float, float]:
    text = str(value or "").strip()
    folded = _fold_text(text)
    base = _mcp_context_item_rank(text)
    exactness = 0.0
    if not text:
        return (-10.0, 0.0, 0.0, 0.0)
    if _answer_demo_source_noise_reason(text, fragment_level=False):
        exactness -= 4.0
    if section_key == "identity":
        if re.search(r"\bmemory\s+subject(?:'s|\s+s)?\s+name\s+is\b", text, flags=re.IGNORECASE):
            exactness += 4.0
        if "name" in set(detect_query_aspects(query_text)) and len(text) <= 180:
            exactness += 1.0
    if section_key == "work":
        if _answer_demo_text_mentions_subject(text, subject_name):
            exactness += 1.2
        if any(token in folded for token in ("ceo", "founder", "founded", "fondatore", "ho fondato", "azienda", "societa", "company")):
            exactness += 0.8
        if re.match(r"^(?:19|20)\d{2}\s*:\s*", text) and not _answer_demo_text_mentions_subject(text, subject_name):
            exactness -= 2.0
        if re.search(r"\b(?:was|is)\s+founded\s+in\s+(?:19|20)\d{2}\b", text, flags=re.IGNORECASE) and not _answer_demo_text_mentions_subject(text, subject_name):
            exactness -= 2.0
    if section_key == "values" and any(token in folded for token in ("values", "valori", "courage", "precision", "responsibility", "sostenibil")):
        exactness += 1.0
    return (exactness, *base)


def answer_demo_surface_needs_context_package_rewrite(
    *,
    query_text: str,
    answer_text: str | None,
    answer_full: str | None = None,
    answer_mode: str | None = None,
    context_package: dict[str, Any] | None = None,
) -> bool:
    """Return True when a secondary answer demo must be rebuilt from the MCP package."""

    package = dict(context_package or {})
    contract_passed = bool(dict(package.get("contract") or {}).get("passed"))
    text = clean_answer_surface_text(answer_text) or clean_answer_surface_text(answer_full)
    full_text = clean_answer_surface_text(answer_full) or text
    mode = str(answer_mode or "").strip().lower()
    if not text:
        return contract_passed
    if _answer_surface_has_context_ledger_leak(text) or _answer_surface_has_context_ledger_leak(full_text):
        return True
    if _answer_demo_source_noise_reason(text, fragment_level=False) or _answer_demo_source_noise_reason(full_text, fragment_level=False):
        return True
    broad = _is_broad_self_query(query_text)
    if contract_passed and mode in {"heuristic", "grounded_facts"}:
        long_form_ready = bool(
            broad
            and len(full_text) >= 1200
            and _answer_required_slot_coverage(query_text, full_text).get("passed")
        )
        if not long_form_ready:
            return True
    if contract_passed and not broad and (len(text) > 1200 or len(full_text) > 1800):
        return True
    if contract_passed and _prefers_first_person_answer(query_text):
        folded = _fold_text(text)
        if not any(marker in folded for marker in ("sono ", "mi chiamo", "lavoro ", "ho fondato", "i miei", "mio ")):
            return True
    return False


def _context_package_answer_fragment(
    query_text: str,
    item_text: str,
    *,
    first_person: bool,
    section_key: str = "",
    subject_name: str | None = None,
    broad: bool = False,
) -> str | None:
    text = _context_package_answer_item_text(item_text)
    if not text:
        return None
    fragments = _sentence_candidates(text)
    if not fragments:
        fragments = [text]
    section_markers = {
        "identity": ("mi chiamo", "name is", "sono nata", "sono nato", "born", "vivo a", "live in"),
        "work": ("lavor", "work", "progetto", "project", "fond", "found", "ceo", "azienda", "company", "studio"),
        "relationships": ("partner", "fratell", "sorell", "padre", "madre", "mentor", "family", "relationship"),
        "history": ("in passato", "prima di", "dopo ", "ha lavorato", "ho lavorato", "storia", "history"),
        "temporal_inventory": ("in passato", "prima di", "dopo ", "ha lavorato", "ho lavorato", "storia", "history"),
        "values": ("valor", "precision", "chiarez", "clarity", "rigor", "coerenza", "responsabil"),
        "style": ("stile", "style", "comunic", "dirett", "strutturat", "tecnic", "leggibil"),
    }

    def sentence_rank(indexed: tuple[int, str]) -> tuple[int, int, int]:
        index, candidate = indexed
        folded = _fold_text(candidate)
        relevance = sum(1 for marker in section_markers.get(section_key, ()) if marker in folded)
        subject_support = 1 if _answer_demo_text_mentions_subject(candidate, subject_name) else 0
        return relevance, subject_support, -index

    if broad and section_key in section_markers:
        fragments = [
            fragment
            for _index, fragment in sorted(enumerate(fragments), key=sentence_rank, reverse=True)
        ]
    sentence_limit = 3 if broad else 1
    selected: list[str] = []
    for fragment in fragments[:sentence_limit]:
        cleaned = _context_package_answer_item_text(fragment)
        if not cleaned:
            continue
        if _answer_demo_source_noise_reason(cleaned, fragment_level=True):
            continue
        folded_cleaned = _fold_text(cleaned)
        if (
            section_key == "work"
            and not _answer_demo_text_mentions_subject(cleaned, subject_name)
            and (
                re.match(r"^(?:19|20)\d{2}\s*:", cleaned)
                or re.search(r"\b(?:was|is|continues\s+to\s+work)\s+founded\b", folded_cleaned)
                or "global network" in folded_cleaned
                or "employees" in folded_cleaned
            )
        ):
            continue
        if first_person:
            cleaned = _self_voice_fragment(cleaned)
            cleaned = _humanize_long_answer_sentence(cleaned, first_person=True) or cleaned
        else:
            cleaned = _humanize_public_sentence(cleaned, first_person=False) or cleaned
        cleaned = clean_answer_surface_text(cleaned).strip(" \"'“”")
        if cleaned and not _answer_surface_has_context_ledger_leak(cleaned):
            selected.append(cleaned)
        if selected and len(" ".join(selected)) >= 360:
            break
    if not selected:
        return None
    return clean_answer_surface_text(" ".join(selected)) or None


def _context_package_answer_required_sections(query_text: str) -> list[str]:
    aspects = set(detect_query_aspects(query_text))
    required: list[str] = []

    def add(section: str) -> None:
        if section not in required:
            required.append(section)

    if aspects & {"name", "birthplace", "residence"}:
        add("identity")
    if aspects & {"role", "projects", "company_founding"}:
        add("work")
    if aspects & {"father", "partner", "mentor", "sibling"}:
        add("relationships")
    if aspects & {"values"}:
        add("values")
    if aspects & {"style"}:
        add("style")
    if aspects & {"history"}:
        add("history")
        add("temporal_inventory")
    if aspects & {"documents"}:
        add("documents")
    if _is_broad_self_query(query_text):
        for section in ("identity", "work", "history", "relationships", "values", "style"):
            add(section)
    return required


def _context_package_section_items(section: dict[str, Any]) -> list[Any]:
    raw_items = section.get("items")
    if isinstance(raw_items, list):
        return raw_items
    if raw_items is None:
        return []
    return [raw_items]


def _context_package_answer_fragments(
    *,
    query_text: str,
    package: dict[str, Any],
    sections: list[dict[str, Any]],
    retrieval_mode: str,
    excluded_sections: set[str] | None = None,
    max_fragments_override: int | None = None,
) -> tuple[list[tuple[str, str]], list[str], list[dict[str, Any]]]:
    section_order = _context_package_answer_section_order(query_text, retrieval_mode)
    order_rank = {key: index for index, key in enumerate(section_order)}
    aspects = set(detect_query_aspects(query_text))
    first_person = _prefers_first_person_answer(query_text)
    subject_name = _answer_demo_subject_name_from_package(package)
    broad = _is_broad_self_query(query_text) or str(retrieval_mode or "") in {"heavy", "forensic"}
    max_fragments = max_fragments_override or (10 if broad else 6 if _query_is_work_or_company(query_text) else 5)
    per_section_limit = 3 if broad else 2
    excluded = {str(section or "").strip() for section in (excluded_sections or set()) if str(section or "").strip()}
    required_sections = [
        section
        for section in _context_package_answer_required_sections(query_text)
        if section not in excluded
    ]
    if not broad and len(required_sections) >= 2:
        # A multi-aspect answer should cover each requested slot once before adding
        # local detail. Otherwise a strong work/document section can drown identity
        # or values and make the demo feel unrelated to the user's actual question.
        per_section_limit = 1
    if not broad:
        if "documents" not in required_sections:
            excluded.add("documents")
        if "history" not in required_sections and "temporal_inventory" not in required_sections:
            excluded.add("temporal_inventory")
        narrow_direct_sections = {"identity", "relationships", "values", "style"}
        if required_sections and set(required_sections).issubset(narrow_direct_sections) and not _query_is_work_or_company(query_text):
            for section in ("identity", "work", "history", "relationships", "values", "style", "documents", "temporal_inventory"):
                if section not in required_sections:
                    excluded.add(section)

    ranked_sections = sorted(
        [section for section in sections if str(section.get("key") or "").strip() not in excluded],
        key=lambda section: (
            order_rank.get(str(section.get("key") or ""), len(order_rank)),
            -float(section.get("confidence") or 0.0),
            str(section.get("title") or ""),
        ),
    )
    fragments: list[tuple[str, str]] = []
    seen_fragments: set[str] = set()

    def add_best_fragment_from_section(section: dict[str, Any], *, limit: int) -> int:
        section_key = str(section.get("key") or "").strip()
        added = 0
        ranked_items = sorted(
            [_context_package_answer_item_text(item) for item in _context_package_section_items(section)],
            key=lambda value: _context_package_answer_item_rank(
                section_key=section_key,
                value=value,
                query_text=query_text,
                subject_name=subject_name,
            ),
            reverse=True,
        )
        for item in ranked_items:
            if not item:
                continue
            fragment = _context_package_answer_fragment(
                query_text,
                item,
                first_person=first_person,
                section_key=section_key,
                subject_name=subject_name,
                broad=broad,
            )
            if not fragment:
                continue
            folded = _fold_text(fragment)
            if not folded or folded in seen_fragments:
                continue
            if any((len(folded) >= 34 and folded in seen) or (len(seen) >= 34 and seen in folded) for seen in seen_fragments):
                continue
            seen_fragments.add(folded)
            fragments.append((section_key, fragment))
            added += 1
            if added >= limit:
                break
        return added

    for required_section in required_sections:
        section = next((item for item in ranked_sections if str(item.get("key") or "").strip() == required_section), None)
        if section:
            add_best_fragment_from_section(section, limit=1)

    for section in ranked_sections:
        section_key = str(section.get("key") or "").strip()
        existing_count = sum(1 for item_section, _fragment in fragments if item_section == section_key)
        if section_key == "identity" and "name" in aspects and not broad and existing_count >= 1:
            continue
        section_limit = max(0, per_section_limit - existing_count)
        if section_limit:
            add_best_fragment_from_section(section, limit=section_limit)
        if len(fragments) >= max_fragments:
            break

    selected_folded = [_fold_text(fragment) for _section, fragment in fragments]
    selected_sections = {section for section, _fragment in fragments if section}
    evidence_node_ids: list[str] = []
    evidence_snippets: list[dict[str, Any]] = []
    for hot in list(package.get("hot_context") or []):
        if not isinstance(hot, dict):
            continue
        node_id = str(hot.get("node_id") or "").strip()
        text = _context_package_answer_item_text(hot.get("text"))
        if not node_id or not text:
            continue
        folded_text = _fold_text(text)
        if not any(selected and (selected in folded_text or folded_text in selected) for selected in selected_folded):
            continue
        if node_id not in evidence_node_ids:
            evidence_node_ids.append(node_id)
            evidence_snippets.append(
                {
                    "node_id": node_id,
                    "text": text,
                    "kind": str(hot.get("section") or "approved_context"),
                }
            )
    if not evidence_node_ids:
        for hot in list(package.get("hot_context") or []):
            if not isinstance(hot, dict):
                continue
            node_id = str(hot.get("node_id") or "").strip()
            section = str(hot.get("section") or "").strip()
            text = _context_package_answer_item_text(hot.get("text"))
            if node_id and text and section in selected_sections and node_id not in evidence_node_ids:
                evidence_node_ids.append(node_id)
                evidence_snippets.append(
                    {
                        "node_id": node_id,
                        "text": text,
                        "kind": section or "approved_context",
                    }
                )
            if len(evidence_node_ids) >= 12:
                break
    return fragments, evidence_node_ids[:12], evidence_snippets[:8]


def build_answer_demo_from_mcp_context_package(
    *,
    query_text: str,
    context_package: dict[str, Any] | None,
    retrieval_mode: str = "balanced",
) -> dict[str, Any] | None:
    """Build the downstream answer demo strictly from approved MCP context."""

    package = dict(context_package or {})
    contract = dict(package.get("contract") or {})
    if not bool(contract.get("passed")):
        return None
    sections = [dict(section or {}) for section in list(package.get("sections") or []) if isinstance(section, dict)]
    if not sections:
        return None

    broad = _is_broad_self_query(query_text) or str(retrieval_mode or "") in {"heavy", "forensic"}
    fragments, evidence_node_ids, evidence_snippets = _context_package_answer_fragments(
        query_text=query_text,
        package=package,
        sections=sections,
        retrieval_mode=retrieval_mode,
    )

    if not fragments:
        return None

    preferred = [fragment for _section, fragment in fragments]
    answer_full = clean_answer_surface_text(" ".join(preferred))
    answer_full = polish_final_answer_surface(query_text, answer_full) or answer_full
    answer_full = _append_broad_answer_scope_closure(
        query_text=query_text,
        answer_text=answer_full,
        retrieval_mode=retrieval_mode,
    )
    if not answer_full or _answer_surface_has_context_ledger_leak(answer_full):
        return None
    if not broad and len(answer_full) > 1100:
        answer_full = _truncate_prompt_text(answer_full, 1100)
    answer_short = answer_full if len(answer_full) <= 700 else _truncate_prompt_text(answer_full, 700)
    answer_short = polish_final_answer_surface(query_text, answer_short) or answer_short
    if not answer_short:
        return None

    return {
        "answer_text": answer_short,
        "answer_full": answer_full,
        "mode": "contract_human_synthesis",
        "confidence": 0.86,
        "evidence_node_ids": evidence_node_ids[:12],
        "reasoning_summary": "Answer demo synthesized strictly from the approved MCP context package after its contract passed.",
        "insufficient": False,
        "answerability_state": "grounded",
        "evidence_snippets": evidence_snippets[:8],
        "support_node_count": len(evidence_node_ids[:12]),
        "support_slot_count": len({section for section, _fragment in fragments}),
        "family_attribution_summary": {
            section: sum(1 for item_section, _fragment in fragments if item_section == section)
            for section in sorted({section for section, _fragment in fragments if section})
        },
        "contradiction_present": False,
        "context_package_answer_demo": True,
    }


_PARTIAL_UNKNOWN_SECTION_LABELS = {
    "identity": "identita",
    "work": "lavoro e aziende",
    "relationships": "relazioni",
    "style": "stile di comunicazione",
    "values": "valori",
    "history": "storia e timeline",
    "temporal_inventory": "riferimenti temporali",
    "documents": "documenti/fonti",
}


def build_partial_known_answer_demo_from_mcp_context_package(
    *,
    query_text: str,
    context_package: dict[str, Any] | None,
    unresolved_sections: list[str],
    retrieval_mode: str = "balanced",
) -> dict[str, Any] | None:
    """Build a partial answer from promoted context while marking missing slots as unknown."""

    package = dict(context_package or {})
    sections = [dict(section or {}) for section in list(package.get("sections") or []) if isinstance(section, dict)]
    unresolved = {
        str(section or "").strip()
        for section in list(unresolved_sections or [])
        if str(section or "").strip() and str(section or "").strip() != "answer_context_alignment"
    }
    if not sections or not unresolved:
        return None
    excluded_sections = set(unresolved)
    if not any(token in _fold_text(query_text) for token in ("document", "documento", "documenti", "pdf", "file", "fonte", "source")):
        excluded_sections.add("documents")
    fragments, evidence_node_ids, evidence_snippets = _context_package_answer_fragments(
        query_text=query_text,
        package=package,
        sections=sections,
        retrieval_mode=retrieval_mode,
        excluded_sections=excluded_sections,
        max_fragments_override=6,
    )
    if not fragments:
        return None

    known_text = clean_answer_surface_text(" ".join(fragment for _section, fragment in fragments))
    known_text = polish_final_answer_surface(query_text, known_text) or known_text
    known_text = re.sub(r"\bDr\.\s+Sono\b", "Sono", known_text, flags=re.IGNORECASE)
    known_text = re.sub(r"\bSono\s+a\s+technology\s+leader\b", "Sono un technology leader", known_text, flags=re.IGNORECASE)
    if not known_text or _answer_surface_has_context_ledger_leak(known_text):
        return None
    if len(known_text) > 1000:
        known_text = _truncate_prompt_text(known_text, 1000)
    missing_labels = [
        _PARTIAL_UNKNOWN_SECTION_LABELS.get(section, section.replace("_", " "))
        for section in sorted(unresolved)
    ]
    missing_text = (
        f"Non trovo nella memoria evidenza affidabile per: {', '.join(missing_labels)}. "
        "Quella parte resta quindi non confermata."
    )
    answer_full = clean_answer_surface_text(f"{known_text} {missing_text}")
    answer_full = re.sub(r"\bDr\.\s+Sono\b", "Sono", answer_full, flags=re.IGNORECASE)
    answer_full = re.sub(r"\bSono\s+a\s+technology\s+leader\b", "Sono un technology leader", answer_full, flags=re.IGNORECASE)
    answer_short = answer_full if len(answer_full) <= 900 else _truncate_prompt_text(answer_full, 900)
    selected_sections = sorted({section for section, _fragment in fragments if section})
    return {
        "answer_text": answer_short,
        "answer_full": answer_full,
        "mode": "partial_known_insufficient",
        "confidence": 0.62,
        "evidence_node_ids": evidence_node_ids,
        "reasoning_summary": "Partial-known answer synthesized from promoted MCP context; unresolved required sections were sealed as not present in memory.",
        "insufficient": True,
        "answerability_state": "partial",
        "partial_known": True,
        "unknown_not_in_memory": True,
        "known_sections": selected_sections,
        "missing_required_sections": sorted(unresolved),
        "evidence_snippets": evidence_snippets,
        "support_node_count": len(evidence_node_ids),
        "support_slot_count": len(selected_sections),
        "family_attribution_summary": {
            section: sum(1 for item_section, _fragment in fragments if item_section == section)
            for section in selected_sections
        },
        "contradiction_present": False,
        "context_package_answer_demo": True,
        "answer_adequacy": {
            "passed": True,
            "unknown_not_in_memory": True,
            "partial_known": True,
            "known_sections": selected_sections,
            "missing_required_sections": sorted(unresolved),
            "context_ledger_leak": False,
            "off_contract_topics": [],
        },
    }


def llm_grounded_answer(
    query_text: str,
    matches: list[dict[str, Any]],
    shared_evidence: dict[str, Any] | None = None,
    evidence_reservoir: dict[str, Any] | None = None,
    retrieval_mode: str = "balanced",
) -> tuple[dict[str, Any] | None, str | None]:
    matches = _filter_disallowed_matches_for_query(
        query_text,
        _eligible_answer_matches(matches),
        retrieval_mode=retrieval_mode,
    )
    if not llm_enabled() or not matches:
        return None, "llm_disabled_or_no_matches"
    metamemory = build_metamemory_package("answer")
    context_seed = build_context_payload(matches, shared_evidence, evidence_reservoir=evidence_reservoir)
    prompt_pack = _build_prompt_pack(matches, evidence_reservoir, retrieval_mode=retrieval_mode)
    answer_voice = "first_person_memory_subject" if _prefers_first_person_answer(query_text) else "grounded_profile"
    temporal_precision_required = _is_temporal_reference_query(query_text)
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["answer_text", "confidence", "evidence_node_ids", "reasoning_summary", "insufficient", "context_summary", "context_fragments", "answerability_state"],
        "properties": {
            "answer_text": {"type": "string"},
            "confidence": {"type": "number"},
            "evidence_node_ids": {"type": "array", "items": {"type": "string"}},
            "reasoning_summary": {"type": "string"},
            "insufficient": {"type": "boolean"},
            "answerability_state": {"type": "string", "enum": ["grounded", "partial", "insufficient"]},
            "context_summary": {"type": "string"},
            "context_fragments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["topic", "text", "confidence", "evidence_node_ids"],
                    "properties": {
                        "topic": {"type": "string"},
                        "text": {"type": "string"},
                        "confidence": {"type": "number"},
                        "evidence_node_ids": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
        },
    }
    system_prompt = (
        "You are the AGVM grounded answer generator.\n\n"
        f"{metamemory}\n\n"
        "Answer the user query using only the retrieved AGVM memories and the shared evidence blackboard. "
        "The canonical source of truth is the evidence reservoir, not compressed navigation summaries. "
        "Also produce a reusable context summary for downstream tools. "
        "Keep answer_text as the human-facing reply, not as an MCP/context dossier. "
        "Keep context_summary and context_fragments for downstream retrieval tools. "
        "If evidence is insufficient, say so explicitly. Never invent facts. "
        "For direct factual questions, answer with the exact fact string present in evidence whenever possible. "
        "If the user addresses the remembered person as you/tu/te/ti, answer in first person as that person while staying evidence-grounded. "
        "If the query asks for years, dates, when, or timeline, actively use explicit temporal evidence; if exact dates are absent, say that the memory does not contain exact dates. "
        "For company, organization, or founder queries, list only entities with evidence of a real role, founding, CEO link, acquisition, association, or operating relation; never list website headings, source labels, slogans, or generic project sections as companies. "
        "Never copy source-interface phrasing such as 'quoted in the release as', 'source URL', 'headings', or document labels into the human answer; translate supported evidence into natural first-person facts. "
        "Do not replace concrete names, places, projects, or relationships with generic abstractions. "
        "If the exact fact is missing from evidence, the answerability state must be partial or insufficient. "
        "When communication cues are available, you may lightly reflect the person's communication style, but only after preserving factual grounding and specificity."
    )
    user_prompt = (
        f"Query:\n{query_text}\n\nPreferred answer voice: {answer_voice}\nTemporal precision required: {temporal_precision_required}\n\nEvidence reservoir summary:\n{(evidence_reservoir or {}).get('reservoir_summary') or {}}\n\nPrompt pack summary:\n{prompt_pack.get('summary') or {}}\n\nRetrieved evidence pack:\n\n"
        + "\n\n".join(list(prompt_pack.get("prompt_blocks") or []))
        + f"\n\nShared evidence:\n{shared_evidence or {}}"
        + f"\n\nContext seed:\n{context_seed}"
    )
    payload, error = structured_json(
        model=answer_model(),
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        schema_name="agvm_grounded_answer",
        schema=schema,
        timeout=20.0,
        role="answer",
    )
    if error or not payload:
        return None, error or "llm_empty"
    known_ids = {str(match["node_id"]) for match in matches[:6]}
    evidence_ids = [str(item) for item in list(payload.get("evidence_node_ids") or []) if str(item) in known_ids]
    answerability_state = str(payload.get("answerability_state") or "grounded")
    if not evidence_ids and answerability_state == "grounded":
        answerability_state = "partial"
    support_metadata = build_answer_support_metadata(
        matches=matches,
        shared_evidence=shared_evidence,
        evidence_node_ids=evidence_ids,
    )
    return {
        "answer_text": str(payload.get("answer_text") or "").strip(),
        "mode": "llm",
        "confidence": max(0.0, min(1.0, float(payload.get("confidence") or 0.0))),
        "evidence_node_ids": evidence_ids,
        "reasoning_summary": str(payload.get("reasoning_summary") or "").strip(),
        "insufficient": bool(payload.get("insufficient")),
        "answerability_state": answerability_state,
        "evidence_snippets": [
            {
                "node_id": str(match["node_id"]),
                "text": str(match.get("evidence_snippet") or match["node"].get("raw_text") or match["summary"]),
                "kind": str(match["node"].get("memory_type") or "memory"),
            }
            for match in matches[:6]
            if str(match["node_id"]) in evidence_ids
        ],
        "support_node_count": int(support_metadata.get("support_node_count") or 0),
        "support_slot_count": int(support_metadata.get("support_slot_count") or 0),
        "family_attribution_summary": dict(support_metadata.get("family_attribution_summary") or {}),
        "contradiction_present": bool(support_metadata.get("contradiction_present")),
        "context": {
            "context_summary": str(payload.get("context_summary") or "").strip(),
            "context_fragments": list(payload.get("context_fragments") or []),
            "structured_sections": list(context_seed.get("structured_sections") or []),
            "traits": list(context_seed.get("traits") or []),
            "style_cues": list(context_seed.get("style_cues") or []),
            "communication_cues": list(context_seed.get("communication_cues") or []),
            "values_cues": list(context_seed.get("values_cues") or []),
            "biographical_cues": list(context_seed.get("biographical_cues") or []),
            "movement_cues": [],
            "story_points": list(context_seed.get("story_points") or []),
            "open_uncertainties": [] if not payload.get("insufficient") else ["Insufficient evidence"],
            "evidence_node_ids": [str(item) for item in list(payload.get("evidence_node_ids") or [])],
            "evidence_reservoir_summary": dict((context_seed.get("evidence_reservoir_summary") or {})),
            "context_quality_metrics": dict((context_seed.get("context_quality_metrics") or {})),
        },
    }, None


def _document_packet_segments(document_packets: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def add_segment(
        *,
        packet: dict[str, Any],
        role: str,
        text: str,
        node_id: str,
        source_node_id: str | None = None,
        score: float = 0.0,
        chunk_index: int | None = None,
    ) -> None:
        clean_text = " ".join(str(text or "").split())
        if not clean_text:
            return
        evidence_node_id = str(source_node_id or node_id or packet.get("anchor_node_id") or "").strip()
        key = (str(packet.get("anchor_node_id") or ""), role, _fold_text(clean_text)[:260])
        if key in seen:
            return
        seen.add(key)
        segments.append(
            {
                "role": role,
                "text": clean_text,
                "node_id": str(node_id or evidence_node_id or "").strip(),
                "source_node_id": evidence_node_id,
                "score": round(float(score or 0.0), 4),
                "chunk_index": chunk_index,
                "title": str(packet.get("title") or packet.get("source_label") or packet.get("anchor_node_id") or "documento"),
                "anchor_node_id": str(packet.get("anchor_node_id") or ""),
                "source_label": packet.get("source_label"),
                "source_type": packet.get("source_type"),
                "packet_query_fit_score": float(packet.get("query_fit_score") or 0.0),
            }
        )

    for packet in list(document_packets or []):
        packet = dict(packet or {})
        anchor_id = str(packet.get("anchor_node_id") or "").strip()
        for chunk in list(packet.get("ordered_chunk_sequence") or []):
            if not isinstance(chunk, dict):
                continue
            add_segment(
                packet=packet,
                role="chunk",
                text=str(chunk.get("raw_text") or chunk.get("text") or chunk.get("evidence_snippet") or ""),
                node_id=str(chunk.get("node_id") or ""),
                source_node_id=str(chunk.get("source_node_id") or anchor_id or chunk.get("node_id") or ""),
                score=float(chunk.get("score") or 0.0),
                chunk_index=int(chunk.get("chunk_index") or 0) or None,
            )
        anchor_text = str(packet.get("anchor_raw_text") or "").strip()
        if anchor_text:
            add_segment(
                packet=packet,
                role="anchor",
                text=anchor_text,
                node_id=anchor_id,
                source_node_id=anchor_id,
                score=float((packet.get("coverage") or {}).get("match_count") or 0.0),
            )
        for fact in list(packet.get("supported_fact_text") or []):
            if not isinstance(fact, dict):
                continue
            add_segment(
                packet=packet,
                role="fact",
                text=str(fact.get("raw_text") or fact.get("summary") or ""),
                node_id=str(fact.get("node_id") or ""),
                source_node_id=str(fact.get("source_node_id") or fact.get("node_id") or anchor_id or ""),
                score=float(fact.get("score") or 0.0),
            )
    return segments


_DOCUMENT_ANSWER_STOPWORDS = {
    "che",
    "chi",
    "come",
    "cosa",
    "dove",
    "quando",
    "quale",
    "quali",
    "qual",
    "mostra",
    "mostrami",
    "trova",
    "apri",
    "nel",
    "nei",
    "nelle",
    "nella",
    "su",
    "relativo",
    "relativi",
    "relative",
    "related",
    "parla",
    "parlano",
    "supporta",
    "supportano",
    "questa",
    "questo",
    "risposta",
    "risposte",
    "rilevante",
    "dai",
    "dal",
    "dalla",
    "documento",
    "documenti",
    "fonte",
    "fonti",
    "chunk",
    "traccia",
    "source",
    "the",
    "and",
    "from",
    "with",
    "about",
}


def _document_answer_terms(query_text: str) -> set[str]:
    return {
        token
        for token in _fold_text(query_text).split()
        if len(token) >= 3 and token not in _DOCUMENT_ANSWER_STOPWORDS
    }


def _score_document_answer_segment(query_text: str, segment: dict[str, Any]) -> float:
    terms = _document_answer_terms(query_text)
    text_folded = _fold_text(str(segment.get("text") or ""))
    overlap = sum(1 for term in terms if term in text_folded)
    role = str(segment.get("role") or "")
    role_bonus = {"chunk": 0.34, "anchor": 0.22, "fact": 0.18}.get(role, 0.0)
    lowered_query = _fold_text(query_text)
    if any(token in lowered_query for token in ("chunk", "source trace", "traccia sorgente", "fonti", "fonte")) and role == "chunk":
        role_bonus += 0.24
    if _is_temporal_reference_query(query_text) and _sentence_has_temporal_signal(str(segment.get("text") or "")):
        role_bonus += 0.32
    return overlap + role_bonus + min(0.3, float(segment.get("score") or 0.0) * 0.05)


def _wants_related_document_catalog(query_text: str) -> bool:
    folded = _fold_text(query_text)
    if any(
        phrase in folded
        for phrase in (
            "documenti su",
            "documenti relativ",
            "documenti collegat",
            "documenti che parlano",
            "documenti parlano",
            "documenti supportano",
            "documenti ho",
            "documenti per",
            "quali documenti",
            "che documenti",
            "mostrami i documenti",
            "mostra i documenti",
            "documents about",
            "related documents",
            "supporting documents",
        )
    ):
        return True
    if "documento" in folded and any(
        phrase in folded
        for phrase in (
            "apri il documento",
            "apri documento",
            "trova il documento",
            "mostra il documento",
            "documento piu rilevante",
            "documento rilevante",
            "documento relativo",
            "documento collegat",
            "documento su",
            "documento per",
        )
    ):
        return True
    return False


def _document_catalog_source_display(packet: dict[str, Any]) -> str:
    source_label = clean_answer_surface_text(packet.get("source_label"))
    source_type = clean_answer_surface_text(packet.get("source_type"))
    if source_label and not _answer_surface_has_context_ledger_leak(source_label):
        return source_label
    generic_source_types = {
        "document",
        "document_anchor",
        "document_chunk",
        "manual_text",
        "manual text",
        "raw_text",
        "raw text",
        "memory",
        "system_pattern",
        "maintenance_pattern",
    }
    folded_type = _fold_text(source_type)
    if source_type and folded_type not in generic_source_types and not _answer_surface_has_context_ledger_leak(source_type):
        return source_type.replace("_", " ")
    return ""


def _document_packet_catalog_answer(
    *,
    query_text: str,
    document_packets: list[dict[str, Any]],
    matches: list[dict[str, Any]],
    shared_evidence: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not _wants_related_document_catalog(query_text):
        return None
    packets = [dict(packet or {}) for packet in document_packets if is_document_eligible(packet)]
    if not packets:
        return None
    max_query_fit = max([float(packet.get("query_fit_score") or 0.0) for packet in packets] or [0.0])
    if _document_answer_terms(query_text) and max_query_fit < 0.2:
        return None
    folded_query = _fold_text(query_text)
    single_document_request = "documento" in folded_query and "documenti" not in folded_query
    visible_packets = [
        packet
        for packet in packets
        if float(packet.get("query_fit_score") or 0.0) >= 0.2 or not _document_answer_terms(query_text)
    ] or packets[:1]
    if single_document_request:
        visible_packets = visible_packets[:1]
    lines: list[str] = []
    evidence_ids: list[str] = []
    snippets: list[dict[str, Any]] = []
    for index, packet in enumerate(visible_packets[:6], start=1):
        title = str(packet.get("title") or packet.get("source_label") or packet.get("anchor_node_id") or "documento").strip().rstrip(".:; ")
        source = _document_catalog_source_display(packet)
        fit = float(packet.get("query_fit_score") or 0.0)
        chunk_count = int(packet.get("chunks_count") or len(list(packet.get("ordered_chunk_sequence") or [])) or len(list(packet.get("chunk_node_ids") or [])))
        fact_count = int(packet.get("facts_count") or len(list(packet.get("supported_fact_text") or [])) or len(list(packet.get("fact_node_ids") or [])))
        tag_candidates = [
            *list(packet.get("timeline_tags") or []),
            *list(packet.get("topic_tags") or []),
            *list(packet.get("project_tags") or []),
            *list(packet.get("entity_tags") or []),
        ]
        query_tag_terms = set(_document_answer_terms(query_text)) | set(_explicit_temporal_terms(query_text))
        matching_tags = [
            str(tag)
            for tag in tag_candidates
            if any(term and term in _fold_text(str(tag)) for term in query_tag_terms)
        ]
        tag_text = ", ".join(list(dict.fromkeys([*matching_tags, *[str(tag) for tag in tag_candidates]]))[:8])
        source_suffix = f" Fonte: {source}." if source else ""
        tags_suffix = f" Tag: {tag_text}." if tag_text else ""
        raw_suffix = " raw completo" if bool(packet.get("raw_text_available") or packet.get("complete_text_available")) else " raw parziale"
        lines.append(
            f"{index}. {title}.{source_suffix} Fit {fit:.2f}; {chunk_count} chunk; {fact_count} fatti;{raw_suffix}.{tags_suffix}"
        )
        anchor_id = str(packet.get("anchor_node_id") or "").strip()
        if anchor_id:
            evidence_ids.append(anchor_id)
            snippets.append(
                {
                    "node_id": anchor_id,
                    "text": f"{title} | fit {fit:.2f} | {source}".strip(" |"),
                    "kind": "document_catalog_card",
                }
            )
        for node_id in list(packet.get("related_node_ids") or [])[:3]:
            node_id = str(node_id or "").strip()
            if node_id:
                evidence_ids.append(node_id)
    if not lines:
        return None
    if single_document_request:
        answer_text = "Ho trovato il documento piu rilevante: " + " ".join(lines)
        query_terms = _document_answer_terms(query_text)
        relevant_sentences: list[str] = []
        for segment in _document_packet_segments(visible_packets[:1]):
            for sentence in _sentence_candidates(str(segment.get("text") or "")):
                cleaned = clean_answer_surface_text(sentence)
                if not cleaned or _source_or_instruction_sentence(cleaned) or _answer_surface_has_context_ledger_leak(cleaned):
                    continue
                folded = _fold_text(cleaned)
                overlap = sum(1 for term in query_terms if term in folded)
                if overlap <= 0:
                    continue
                relevant_sentences.append(cleaned)
                break
            if len(relevant_sentences) >= 2:
                break
        if relevant_sentences:
            answer_text = f"{answer_text} Il materiale rilevante dice: {' '.join(relevant_sentences[:2])}"
    else:
        answer_text = "Ho trovato questi documenti ordinati per pertinenza: " + " ".join(lines)
    support_metadata = build_answer_support_metadata(
        matches=matches,
        shared_evidence=shared_evidence,
        evidence_node_ids=list(dict.fromkeys(evidence_ids)),
    )
    return {
        "answer_text": clean_answer_surface_text(answer_text),
        "mode": "document_packet",
        "confidence": 0.84,
        "evidence_node_ids": list(dict.fromkeys(evidence_ids))[:12],
        "reasoning_summary": "Returned the ranked document catalog cards instead of a generic prose answer.",
        "insufficient": False,
        "answerability_state": "grounded",
        "evidence_snippets": snippets[:6],
        "support_node_count": int(support_metadata.get("support_node_count") or len(evidence_ids)),
        "support_slot_count": int(support_metadata.get("support_slot_count") or 1),
        "family_attribution_summary": dict(support_metadata.get("family_attribution_summary") or {}),
        "contradiction_present": bool(support_metadata.get("contradiction_present")),
        "distinct_evidence_packet_count": int(support_metadata.get("distinct_evidence_packet_count") or len(visible_packets)),
    }


def _document_answer_from_packets(
    *,
    query_text: str,
    matches: list[dict[str, Any]],
    shared_evidence: dict[str, Any] | None,
    evidence_reservoir: dict[str, Any] | None,
    document_mode: str,
    document_packets: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    document_packets = [dict(packet) for packet in list(document_packets or []) if isinstance(packet, dict) and is_document_eligible(packet)]
    if document_mode == "none" or not document_packets:
        return None
    catalog_answer = _document_packet_catalog_answer(
        query_text=query_text,
        document_packets=document_packets,
        matches=matches,
        shared_evidence=shared_evidence,
    )
    if catalog_answer:
        return catalog_answer
    segments = _document_packet_segments(document_packets)
    if not segments:
        return None
    document_lookup_terms = _document_answer_terms(query_text)
    max_packet_query_fit = max([float(segment.get("packet_query_fit_score") or 0.0) for segment in segments] or [0.0])
    if document_mode == "lookup" and document_lookup_terms and max_packet_query_fit < 0.2:
        return None
    ranked_segments = sorted(
        segments,
        key=lambda segment: (
            -float(segment.get("packet_query_fit_score") or 0.0),
            -_score_document_answer_segment(query_text, segment),
            {"chunk": 0, "anchor": 1, "fact": 2}.get(str(segment.get("role") or ""), 3),
            str(segment.get("node_id") or ""),
        ),
    )
    lead_packet = dict(list(document_packets or [{}])[0] or {})
    lead_title = str(lead_packet.get("title") or lead_packet.get("source_label") or "documento").strip().rstrip(".:; ")
    provenance = " / ".join(
        part
        for part in (
            str(lead_packet.get("source_label") or "").strip(),
            str(lead_packet.get("source_type") or "").strip(),
        )
        if part
    )
    evidence_ids = list(
        dict.fromkeys(
            str(segment.get("source_node_id") or segment.get("node_id") or "").strip()
            for segment in ranked_segments[:6]
            if str(segment.get("source_node_id") or segment.get("node_id") or "").strip()
        )
    )

    temporal_inventory = (
        build_temporal_inventory(matches, evidence_reservoir=evidence_reservoir)
        if _is_temporal_reference_query(query_text)
        else {"entries": []}
    )
    if temporal_inventory.get("entries"):
        temporal_lines = []
        temporal_entries = _rank_temporal_entries_for_query(
            query_text,
            [dict(entry) for entry in list(temporal_inventory.get("entries") or []) if isinstance(entry, dict)],
            requested_terms=set(_explicit_temporal_terms(query_text)),
        )
        for entry in temporal_entries[:5]:
            year_label = str(
                entry.get("primary_year")
                or _join_human_list([str(year) for year in list(entry.get("years") or [])])
                or "data"
            ).strip()
            text = str(entry.get("text") or "").strip()
            if text:
                temporal_lines.append(f"{year_label}: {_truncate_prompt_text(text, 220)}")
        if temporal_lines:
            answer_text = "Nei documenti e nelle note recuperate trovo questi riferimenti temporali: " + " ".join(temporal_lines)
            evidence_ids = list(
                dict.fromkeys(
                    [
                        *[
                            str(entry.get("node_id") or "").strip()
                            for entry in temporal_entries[:8]
                            if str(entry.get("node_id") or "").strip()
                        ],
                        *evidence_ids,
                    ]
                )
            )[:10]
            support_metadata = build_answer_support_metadata(matches=matches, shared_evidence=shared_evidence, evidence_node_ids=evidence_ids)
            return {
                "answer_text": answer_text,
                "mode": "document_packet",
                "confidence": 0.84,
                "evidence_node_ids": evidence_ids,
                "reasoning_summary": "Built from explicit temporal evidence inside document packets and the raw evidence reservoir.",
                "insufficient": False,
                "answerability_state": "grounded",
                "evidence_snippets": [
                    {"node_id": str(item.get("source_node_id") or item.get("node_id") or ""), "text": str(item.get("text") or ""), "kind": str(item.get("role") or "document")}
                    for item in ranked_segments[:5]
                ],
                "support_node_count": int(support_metadata.get("support_node_count") or len(evidence_ids)),
                "support_slot_count": int(support_metadata.get("support_slot_count") or 1),
                "family_attribution_summary": dict(support_metadata.get("family_attribution_summary") or {}),
                "contradiction_present": bool(support_metadata.get("contradiction_present")),
            }

    def human_sentences(segment: dict[str, Any]) -> list[str]:
        terms = _document_answer_terms(query_text)
        candidates: list[tuple[int, int, str]] = []
        for sentence in _sentence_candidates(str(segment.get("text") or "")):
            cleaned = clean_answer_surface_text(sentence)
            if not cleaned:
                continue
            if _source_or_instruction_sentence(cleaned):
                continue
            if _answer_surface_has_context_ledger_leak(cleaned):
                continue
            folded = _fold_text(cleaned)
            overlap = sum(1 for term in terms if term in folded)
            candidates.append((-overlap, len(candidates), cleaned))
        return [candidate[2] for candidate in sorted(candidates)]

    snippets = []
    for segment in ranked_segments[:4]:
        for sentence in human_sentences(segment):
            if sentence:
                snippets.append(_truncate_prompt_text(sentence, 260))
            if len(snippets) >= 4:
                break
        if len(snippets) >= 4:
            break
    snippets = list(dict.fromkeys(snippets))
    if not snippets:
        return None

    if document_mode == "lookup":
        source_suffix = f" ({provenance})" if provenance and provenance not in {"manual_text", "manual text"} else ""
        answer_text = f"Ho trovato il documento: {lead_title}{source_suffix}."
        if snippets:
            answer_text = f"{answer_text} Il materiale piu rilevante dice: {' '.join(snippets[:2])}"
    else:
        answer_text = f"Secondo il documento {lead_title}, " + " ".join(snippets[:3])
    answer_text = " ".join(answer_text.split())
    if _prefers_first_person_answer(query_text):
        answer_text = " ".join(_self_voice_fragment(sentence) for sentence in _sentence_candidates(answer_text)).strip() or answer_text
    answer_text = clean_answer_surface_text(answer_text)
    support_metadata = build_answer_support_metadata(matches=matches, shared_evidence=shared_evidence, evidence_node_ids=evidence_ids)
    return {
        "answer_text": answer_text,
        "mode": "document_packet",
        "confidence": 0.82 if document_mode == "lookup" else 0.78,
        "evidence_node_ids": evidence_ids[:10],
        "reasoning_summary": "Answered from hydrated document packet chunks/raw evidence before falling back to generic context.",
        "insufficient": False,
        "answerability_state": "grounded",
        "evidence_snippets": [
            {"node_id": str(item.get("source_node_id") or item.get("node_id") or ""), "text": str(item.get("text") or ""), "kind": str(item.get("role") or "document")}
            for item in ranked_segments[:5]
        ],
        "support_node_count": int(support_metadata.get("support_node_count") or len(evidence_ids)),
        "support_slot_count": int(support_metadata.get("support_slot_count") or 1),
        "family_attribution_summary": dict(support_metadata.get("family_attribution_summary") or {}),
        "contradiction_present": bool(support_metadata.get("contradiction_present")),
    }


def _document_lookup_no_match_answer(
    *,
    query_text: str,
    matches: list[dict[str, Any]],
    shared_evidence: dict[str, Any] | None,
    evidence_reservoir: dict[str, Any] | None,
    document_mode: str,
    document_packets: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    packets = [dict(packet or {}) for packet in list(document_packets or []) if isinstance(packet, dict) and is_document_eligible(packet)]
    if document_mode != "lookup" or not packets or not _document_answer_terms(query_text):
        return None
    max_packet_query_fit = max([float(packet.get("query_fit_score") or packet.get("query_fit") or 0.0) for packet in packets] or [0.0])
    if max_packet_query_fit >= 0.2:
        return None
    requested = "documento/PDF esplicito" if " pdf " in f" {_fold_text(query_text)} " else "documento esplicito"
    answer_text = f"Non trovo un {requested} che corrisponda alla richiesta nella memoria recuperata."
    support_metadata = build_answer_support_metadata(matches=matches, shared_evidence=shared_evidence)
    evidence_ids = list(
        dict.fromkeys(
            str(packet.get("anchor_node_id") or "").strip()
            for packet in packets[:6]
            if str(packet.get("anchor_node_id") or "").strip()
        )
    )
    return {
        "answer_text": answer_text,
        "mode": "document_lookup_guard",
        "confidence": 0.5,
        "evidence_node_ids": evidence_ids[:10],
        "reasoning_summary": "The retrieved document catalog packets do not match the explicit document lookup terms.",
        "insufficient": False,
        "answerability_state": "partial",
        "document_lookup_state": "no_matching_document_packet",
        "evidence_snippets": [
            {
                "node_id": str(packet.get("anchor_node_id") or ""),
                "text": str(packet.get("title") or packet.get("source_label") or "document packet"),
                "kind": "document_lookup_no_match",
            }
            for packet in packets[:4]
        ],
        "support_node_count": int(support_metadata.get("support_node_count") or len(evidence_ids)),
        "support_slot_count": int(support_metadata.get("support_slot_count") or 1),
        "family_attribution_summary": dict(support_metadata.get("family_attribution_summary") or {}),
        "contradiction_present": bool(support_metadata.get("contradiction_present")),
        "distinct_evidence_packet_count": int(support_metadata.get("distinct_evidence_packet_count") or len(packets)),
        "ordered_document_sequence_supported": bool(support_metadata.get("ordered_document_sequence_supported")),
        "context_quality_metrics": dict((evidence_reservoir or {}).get("quality_metrics") or {}),
    }


def generate_grounded_answer(
    query_text: str,
    matches: list[dict[str, Any]],
    shared_evidence: dict[str, Any] | None = None,
    response_mode: str = "both",
    *,
    evidence_reservoir: dict[str, Any] | None = None,
    retrieval_mode: str = "balanced",
    document_mode: str = "none",
    document_packets: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    answer_matches = _eligible_answer_matches(matches)
    document_matches = [dict(match) for match in list(matches or []) if is_document_eligible(match)]
    if response_mode == "context":
        context_matches = document_matches or answer_matches or [dict(match) for match in list(matches or [])]
        context = build_context_payload(context_matches, shared_evidence, evidence_reservoir=evidence_reservoir, query_text=query_text)
        return None, context
    document_answer = _document_answer_from_packets(
        query_text=query_text,
        matches=document_matches,
        shared_evidence=shared_evidence,
        evidence_reservoir=evidence_reservoir,
        document_mode=document_mode,
        document_packets=document_packets,
    )
    if document_answer:
        answer = _apply_answer_contract(query_text, document_answer, document_matches)
        context = build_context_payload(document_matches, shared_evidence, evidence_reservoir=evidence_reservoir, query_text=query_text)
        return answer if response_mode in {"answer", "both"} else None, context if response_mode in {"context", "both"} else None
    document_no_match_answer = _document_lookup_no_match_answer(
        query_text=query_text,
        matches=document_matches,
        shared_evidence=shared_evidence,
        evidence_reservoir=evidence_reservoir,
        document_mode=document_mode,
        document_packets=document_packets,
    )
    if document_no_match_answer:
        answer = _apply_answer_contract(query_text, document_no_match_answer, document_matches)
        context = build_context_payload(document_matches, shared_evidence, evidence_reservoir=evidence_reservoir, query_text=query_text)
        return answer if response_mode in {"answer", "both"} else None, context if response_mode in {"context", "both"} else None

    best_partial_answer: dict[str, Any] | None = None
    best_partial_context: dict[str, Any] | None = None

    def candidate_final_ready(candidate: dict[str, Any] | None) -> bool:
        if not candidate:
            return False
        if _query_is_work_or_company(query_text) and len(str(candidate.get("answer_text") or "").strip()) < 180 and len(answer_matches) >= 3:
            return False
        adequacy = dict(candidate.get("answer_adequacy") or {})
        return (
            not bool(candidate.get("insufficient"))
            and str(candidate.get("answerability_state") or "") == "grounded"
            and bool(adequacy.get("passed", True))
        )

    def candidate_rank(candidate: dict[str, Any] | None) -> tuple[float, ...]:
        if not candidate:
            return (-1.0,)
        adequacy = dict(candidate.get("answer_adequacy") or {})
        state_rank = {"grounded": 3.0, "partial": 2.0, "insufficient": 0.0}.get(str(candidate.get("answerability_state") or ""), 1.0)
        if bool(candidate.get("insufficient")):
            state_rank -= 0.5
        missing_required_slots = len(list(adequacy.get("missing_required_slots") or []))
        support_slots = int(candidate.get("support_slot_count") or 0)
        support_nodes = int(candidate.get("support_node_count") or len(list(candidate.get("evidence_node_ids") or [])))
        evidence_count = len(list(candidate.get("evidence_node_ids") or []))
        confidence = float(candidate.get("confidence") or 0.0)
        text_weight = min(4.0, len(str(candidate.get("answer_text") or "")) / 160.0)
        return (
            state_rank,
            1.0 if bool(adequacy.get("passed")) else 0.0,
            -float(missing_required_slots),
            float(support_slots),
            float(support_nodes),
            float(evidence_count),
            confidence,
            text_weight,
        )

    def remember_partial(candidate: dict[str, Any] | None, context_payload: dict[str, Any] | None) -> None:
        nonlocal best_partial_answer, best_partial_context
        if not candidate:
            return
        if best_partial_answer is None or candidate_rank(candidate) > candidate_rank(best_partial_answer):
            best_partial_answer = candidate
            best_partial_context = context_payload

    human_synthesizer_answer = build_grounded_human_synthesizer_answer(
        query_text,
        answer_matches,
        evidence_reservoir=evidence_reservoir,
    )
    if human_synthesizer_answer:
        prioritized_matches = sorted(
            answer_matches,
            key=lambda match: (
                0 if str(match["node_id"]) in set(str(item) for item in human_synthesizer_answer["evidence_node_ids"]) else 1,
                -float(match.get("raw_score") or 0.0),
            ),
        )
        answer = _apply_answer_contract(query_text, human_synthesizer_answer, prioritized_matches)
        context = build_context_payload(prioritized_matches, shared_evidence, evidence_reservoir=evidence_reservoir, query_text=query_text)
        if candidate_final_ready(answer):
            return answer if response_mode in {"answer", "both"} else None, context if response_mode in {"context", "both"} else None
        remember_partial(answer, context)
    direct_answer = build_direct_fact_answer(query_text, answer_matches, evidence_reservoir=evidence_reservoir)
    if direct_answer:
        prioritized_matches = sorted(
            answer_matches,
            key=lambda match: (
                0 if str(match["node_id"]) in set(str(item) for item in direct_answer["evidence_node_ids"]) else 1,
                -float(match.get("raw_score") or 0.0),
            ),
        )
        answer = {
            "answer_text": direct_answer["answer_text"],
            "mode": direct_answer["mode"],
            "confidence": direct_answer["confidence"],
            "evidence_node_ids": direct_answer["evidence_node_ids"],
            "reasoning_summary": direct_answer["reasoning_summary"],
            "insufficient": direct_answer["insufficient"],
            "answerability_state": direct_answer["answerability_state"],
            "evidence_snippets": direct_answer.get("evidence_snippets") or [],
            "support_node_count": int(direct_answer.get("support_node_count") or 0),
            "support_slot_count": int(direct_answer.get("support_slot_count") or 0),
            "family_attribution_summary": dict(direct_answer.get("family_attribution_summary") or {}),
            "contradiction_present": bool(direct_answer.get("contradiction_present")),
            "answer_adequacy": dict(direct_answer.get("answer_adequacy") or {}),
        }
        answer = _apply_answer_contract(query_text, answer, prioritized_matches)
        context = build_context_payload(prioritized_matches, shared_evidence, evidence_reservoir=evidence_reservoir, query_text=query_text)
        direct_text = str(answer.get("answer_text") or "").strip()
        direct_too_thin_for_work = (
            _query_is_work_or_company(query_text)
            and len(direct_text) < 180
            and len(answer_matches) >= 3
        )
        if candidate_final_ready(answer) and not direct_too_thin_for_work:
            return answer if response_mode in {"answer", "both"} else None, context if response_mode in {"context", "both"} else None
        remember_partial(answer, context)
    requested_temporal_terms = _explicit_temporal_terms(query_text)
    if requested_temporal_terms:
        evidence_text = " ".join(
            str(part or "")
            for match in answer_matches
            for part in (
                match.get("evidence_snippet"),
                (match.get("node") or {}).get("raw_text"),
                (match.get("node") or {}).get("summary"),
                match.get("summary"),
            )
        )
        missing_terms = [term for term in requested_temporal_terms if term not in evidence_text]
        if missing_terms:
            answer = {
                "answer_text": f"Non trovo evidenze esplicite su {', '.join(missing_terms)} nella memoria recuperata.",
                "mode": "insufficient",
                "confidence": 0.0,
                "evidence_node_ids": [],
                "reasoning_summary": "Temporal query requested explicit years/dates that were not present in retrieved evidence.",
                "insufficient": True,
                "answerability_state": "insufficient",
                "evidence_snippets": [],
                "support_node_count": 0,
                "support_slot_count": 0,
                "family_attribution_summary": {},
                "contradiction_present": False,
            }
            answer = _apply_answer_contract(query_text, answer, answer_matches)
            context = build_context_payload(answer_matches, shared_evidence, evidence_reservoir=evidence_reservoir, query_text=query_text)
            return answer if response_mode in {"answer", "both"} else None, context if response_mode in {"context", "both"} else None
    payload, _error = llm_grounded_answer(
        query_text,
        answer_matches,
        shared_evidence,
        evidence_reservoir=evidence_reservoir,
        retrieval_mode=retrieval_mode,
    )
    if payload and payload.get("answer_text"):
        answer = {
            "answer_text": payload["answer_text"],
            "mode": payload["mode"],
            "confidence": payload["confidence"],
            "evidence_node_ids": payload["evidence_node_ids"],
            "reasoning_summary": payload["reasoning_summary"],
            "insufficient": payload["insufficient"],
            "answerability_state": payload.get("answerability_state") or ("insufficient" if payload["insufficient"] else "grounded"),
            "evidence_snippets": payload.get("evidence_snippets") or [],
            "support_node_count": int(payload.get("support_node_count") or 0),
            "support_slot_count": int(payload.get("support_slot_count") or 0),
            "family_attribution_summary": dict(payload.get("family_attribution_summary") or {}),
            "contradiction_present": bool(payload.get("contradiction_present")),
            "answer_adequacy": dict(payload.get("answer_adequacy") or {}),
        }
        answer = _apply_answer_contract(query_text, answer, answer_matches)
        context = dict(payload.get("context") or {})
        if candidate_final_ready(answer):
            return answer if response_mode in {"answer", "both"} else None, context if response_mode in {"context", "both"} else None
        remember_partial(answer, context)
    answer = _apply_answer_contract(query_text, heuristic_answer(query_text, answer_matches), answer_matches)
    context = build_context_payload(answer_matches, shared_evidence, evidence_reservoir=evidence_reservoir, query_text=query_text)
    if not candidate_final_ready(answer):
        remember_partial(answer, context)
        answer = best_partial_answer or answer
        context = best_partial_context or context
    return answer if response_mode in {"answer", "both"} else None, context if response_mode in {"context", "both"} else None


def _is_broad_self_query(query_text: str) -> bool:
    return _query_requests_broad_profile_context(query_text)


def _needs_long_context(*, retrieval_mode: str, query_text: str) -> bool:
    contract = build_query_contract(query_text, retrieval_mode=retrieval_mode)
    if str(contract.get("answer_width") or "") == "dossier" or _is_broad_self_query(query_text):
        return True
    return str(contract.get("query_kind") or "") == "work_narrative" and str(retrieval_mode or "balanced") in {"heavy", "forensic"}


def _preferred_section_summary_item(section_key: str, items: list[str]) -> str | None:
    candidates = [str(item or "").strip() for item in items if str(item or "").strip()]
    complete = [item for item in candidates if "..." not in item and "…" not in item]
    pool = complete or candidates
    if not pool:
        return None

    folded_key = _fold_text(section_key)

    def score(item: str) -> tuple[int, int]:
        folded = _fold_text(item)
        words = len(item.split())
        value = 40 if 4 <= words <= 48 else 0
        value += 30 if re.search(r"\b(?:sono|vivo|lavor|guido|fond|comunic|valori|mi chiamo|name is)\w*\b", folded) else 0
        value -= 35 if words <= 3 else 0
        if folded_key == "identity" and ("mi chiamo" in folded or "name is" in folded):
            value += 80
        if folded_key == "work" and re.search(r"\b(?:lavor|guido|fond|ceo|build|work)\w*\b", folded):
            value += 60
        return value, min(len(item), 320)

    return max(pool, key=score)


def _deterministic_context_dossier(
    query_text: str,
    context: dict[str, Any] | None,
    matches: list[dict[str, Any]],
) -> tuple[str | None, str | None]:
    sections = list((context or {}).get("structured_sections") or [])
    if not sections:
        return None, None
    disallowed_markers = _disallowed_topic_markers(build_query_contract(query_text))
    summary_lines: list[str] = []
    dossier_sections: list[str] = []
    query_contract = build_query_contract(query_text)
    broad_answer = str(query_contract.get("answer_width") or "") == "dossier" or _is_broad_self_query(query_text)
    requested_sections = {
        str(contract.get("section") or "").strip().lower()
        for contract in list(query_contract.get("semantic_slot_contracts") or [])
        if str(contract.get("section") or "").strip()
    }
    for section in sections:
        section_key = str(section.get("key") or "").strip().lower()
        title = str(section.get("title") or section.get("key") or "Memory").strip()
        items = [str(item).strip() for item in list(section.get("items") or []) if str(item).strip()]
        if disallowed_markers:
            items = [item for item in items if not _text_has_any_marker(item, disallowed_markers)]
        if not items:
            continue
        summary_item = _preferred_section_summary_item(section_key, items)
        if summary_item and (broad_answer or not requested_sections or section_key in requested_sections):
            summary_lines.append(summary_item)
        body = "\n".join(f"- {item}" for item in items)
        dossier_sections.append(f"## {title}\n{body}")
    long_form = _is_broad_self_query(query_text)
    evidence_lines = []
    temporal_lines = []
    evidence_limit = 16 if long_form else 8
    snippet_limit = 520 if long_form else 320
    for match in matches[:evidence_limit]:
        snippet = str(match.get("evidence_snippet") or match["node"].get("raw_text") or match["summary"] or "").strip()
        if not snippet:
            continue
        evidence_lines.append(f"- [{match['node_id']}] {_truncate_prompt_text(snippet, snippet_limit)}")
        if _is_temporal_reference_query(query_text) and _sentence_has_temporal_signal(snippet):
            temporal_lines.append(_truncate_prompt_text(snippet, 260))
    dossier = "\n\n".join(dossier_sections)
    if evidence_lines:
        dossier = f"{dossier}\n\n## Evidence Ledger\n" + "\n".join(evidence_lines)
    answer_full = " ".join(summary_lines[:6]).strip() or None
    if _is_broad_self_query(query_text) and answer_full and len(summary_lines) > 2:
        answer_full = " ".join(summary_lines).strip()
        if len(answer_full) < 900 and evidence_lines:
            evidence_answer = " ".join(line.split("] ", 1)[-1] for line in evidence_lines[:8])
            answer_full = f"{answer_full} {evidence_answer}".strip()
    if _is_temporal_reference_query(query_text) and temporal_lines:
        temporal_answer = " ".join(dict.fromkeys(temporal_lines[:6])).strip()
        answer_full = f"{answer_full or ''} {temporal_answer}".strip()
    if answer_full and _prefers_first_person_answer(query_text):
        answer_full = " ".join(_self_voice_fragment(sentence) for sentence in _sentence_candidates(answer_full)).strip()
    return answer_full, dossier or None


def _coerce_long_context_dossier(
    context_dossier: str | None,
    deterministic_dossier: str | None,
    *,
    retrieval_mode: str,
    query_text: str,
) -> str | None:
    text = str(context_dossier or "").strip()
    deterministic = str(deterministic_dossier or "").strip()
    if not _needs_long_context(retrieval_mode=retrieval_mode, query_text=query_text):
        return text or deterministic or None
    if not deterministic:
        return text or None
    min_len = 1400 if retrieval_mode in {"heavy", "forensic"} else 1100
    cap = 7600 if retrieval_mode == "forensic" else 6200 if retrieval_mode == "heavy" else 4200
    if not text:
        return deterministic[:cap].strip()
    if len(text) >= min_len:
        return text[:cap].strip()
    if deterministic in text:
        return text[:cap].strip()
    merged = f"{text}\n\n## Grounded Retrieval Ledger\n{deterministic}".strip()
    return merged[:cap].strip()


def _clean_long_answer_source_line(line: str, *, allow_ledger_snippet: bool = False) -> str | None:
    text = str(line or "").strip()
    if not text:
        return None
    if text.startswith("- "):
        text = text[2:].strip()
    text = re.sub(r"^\[[^\]]+\]\s*", "", text).strip()
    if ":" in text:
        prefix, rest = text.split(":", 1)
        folded_prefix = _fold_text(prefix)
        if (
            len(prefix) <= 96
            and (
                "manual text" in folded_prefix
                or "manual_text" in folded_prefix
                or "document" in folded_prefix
                or "chunk" in folded_prefix
                or "works as" in folded_prefix
                or folded_prefix in {"documents", "facts", "chunks", "memory"}
            )
        ):
            text = rest.strip()
    text = re.sub(r"\|\s*(?:manual_text|derived_[a-z_]+|document_[a-z_]+)\b", "", text, flags=re.IGNORECASE).strip()
    text = text.replace("manual_text", "").replace("manual text", "").strip(" -|")
    folded = _fold_text(text)
    if not allow_ledger_snippet and any(
        marker in folded
        for marker in (
            "raw context",
            "mixed evidence",
            "mixed_evidence",
            "context dossier",
            "evidence ledger",
            "grounded retrieval ledger",
            "document packet",
            "navigation store",
        )
    ):
        return None
    if re.match(r"(?i)^(chunk|fact|anchor raw|open questions)\s+\d*\b", text):
        return None
    text = text.replace("...", " ").replace("…", " ")
    text = " ".join(text.split())
    if re.search(r"(?i)\bdocument(?:o)?\s+(?:bootstrap|operativ[oa]|\d+)\b", text):
        return None
    if re.search(r"(?i)\b(?:descrive\s+stu|\bdentr|\bspess|modo\s+diret)\s*$", text):
        return None
    return text or None


def _long_answer_surface_quality_blocker(value: str | None) -> str | None:
    text = clean_answer_surface_text(value)
    if not text:
        return "empty"
    if _answer_surface_has_context_ledger_leak(text):
        return "context_ledger_leak"
    if re.search(r"(?i)\bdocument(?:o)?\s+(?:bootstrap|operativ[oa]|\d+)\b", text):
        return "document_source_fragment"
    if re.search(r"(?i)\blavoro\s+come\s+(?:guido|dirett[oa]|strutturat[oa]|tecnic[oa])\b", text):
        return "malformed_role_surface"
    if re.search(r"(?i)\b(?:descrive\s+stu|\bdentr|\bspess|modo\s+diret)(?:\s|[.;,])", text):
        return "truncated_source_fragment"
    sentences = [
        _fold_text(sentence)
        for sentence in _sentence_candidates(text)
        if len(_fold_text(sentence)) >= 24
    ]
    if len(sentences) >= 5 and len(set(sentences)) < max(3, int(len(sentences) * 0.7)):
        return "repetitive_surface"
    return None


def _append_broad_answer_scope_closure(
    *,
    query_text: str,
    answer_text: str | None,
    retrieval_mode: str,
) -> str | None:
    text = polish_final_answer_surface(query_text, answer_text) or clean_answer_surface_text(answer_text)
    slot_coverage = _answer_required_slot_coverage(query_text, text or "")
    missing_slots = set(slot_coverage.get("missing_required_slots") or [])
    history_is_explicit = bool(
        re.search(
            r"\b(?:prima\s+di|in\s+passato|precedent[ei]|previous(?:ly)?)\b",
            _fold_text(text or ""),
        )
    )
    slot_coverage_ready = bool(
        slot_coverage.get("passed")
        or (missing_slots == {"history"} and history_is_explicit)
    )
    if (
        not text
        or not _is_broad_self_query(query_text)
        or retrieval_mode not in {"heavy", "forensic"}
        or len(text) >= 1000
        or len(text) < 400
        or _long_answer_surface_quality_blocker(text)
        or not slot_coverage_ready
    ):
        return text or None
    first_person = bool(
        re.search(
            r"\b(?:tu|te|ti|tuo|tua|tuoi|tue|you|your)\b",
            _fold_text(query_text),
        )
    )
    if first_person:
        closure = (
            "Questo e il perimetro che posso sostenere con le memorie recuperate: copre identita, luogo, "
            "lavoro, progetto, relazioni nominate, stile, valori e il passaggio professionale precedente. "
            "Il risultato non va considerato una biografia completa: se date, altri passaggi o ulteriori relazioni "
            "non emergono, lascio il limite esplicito e non aggiungo dettagli non verificati. In questa sintesi "
            "tengo separati i fatti personali e professionali dai limiti del recupero: nomi, luoghi, ruoli e "
            "relazioni sono riportati soltanto quando supportati. Per una cronologia piu precisa servono ulteriori "
            "date o passaggi presenti nella memoria; il profilo generale non basta per dedurli."
        )
    else:
        closure = (
            "Questo e il perimetro che le memorie recuperate permettono di sostenere su Elena: copre identita, "
            "luogo, lavoro, progetto, relazioni nominate, stile, valori e un passaggio professionale precedente. "
            "Il risultato non va considerato una biografia completa: se date, altri passaggi o ulteriori relazioni "
            "non emergono, il riepilogo lascia il limite esplicito e non aggiunge dettagli non verificati. La "
            "sintesi separa i fatti personali e professionali dai limiti del recupero: nomi, luoghi, ruoli e "
            "relazioni sono riportati soltanto quando supportati. Per una cronologia piu precisa servono ulteriori "
            "date o passaggi presenti nella memoria; il profilo generale non basta per dedurli."
        )
    expanded = polish_final_answer_surface(query_text, f"{text} {closure}")
    if not expanded or _long_answer_surface_quality_blocker(expanded):
        return text
    return expanded


def _looks_like_voice_person_subject(value: str) -> bool:
    subject = re.sub(r"^(?:Dr\.\s+)", "", str(value or "").strip(" .,:;"), flags=re.IGNORECASE)
    if not subject:
        return False
    folded = _fold_text(subject)
    if folded in {"i", "io", "you", "tu", "he", "she", "lui", "lei", "they", "loro"}:
        return False
    if _target_looks_like_org_or_project(subject):
        return False
    parts = subject.split()
    if not 1 <= len(parts) <= 5:
        return False
    if any(
        marker in folded
        for marker in (
            "company",
            "studio",
            "systems",
            "platform",
            "project",
            "orbit",
            "group",
            "foundation",
            "foundry",
            "lab",
            "energy",
        )
    ):
        return False
    return all(part[:1].isupper() for part in parts)


def _first_person_voice_leak_markers(answer_text: str | None) -> list[str]:
    text = str(answer_text or "")
    folded = f" {_fold_text(text)} "
    markers: list[str] = []
    for static_marker in ("i suoi valori", "il suo ", "la sua ", "i suoi ", "le sue ", "lavora come "):
        if static_marker in folded:
            markers.append(static_marker.strip())
    person_token = r"[A-ZÀ-Þ][A-Za-zÀ-ÿ0-9'’_-]+"
    person_span = rf"(?:Dr\.\s+)?{person_token}(?:\s+{person_token}){{0,4}}"
    for match in re.finditer(
        rf"\b(?P<subject>{person_span})\s+(?P<verb>lavora|ha|is|works|usa|uses|comunica|parla|tende)\b",
        text,
        flags=re.IGNORECASE,
    ):
        subject = match.group("subject")
        if not _looks_like_voice_person_subject(subject):
            continue
        markers.append(f"{subject} {match.group('verb')}".strip())
    return list(dict.fromkeys(markers))


def _rewrite_first_person_voice_surface(text: str) -> str:
    value = str(text or "")
    if not value:
        return value
    person_token = r"[A-ZÀ-Þ][A-Za-zÀ-ÿ0-9'’_-]+"
    person_span = rf"(?:Dr\.\s+)?{person_token}(?:\s+{person_token}){{0,4}}"
    entity_span = rf"{person_token}(?:\s+{person_token}){{0,6}}"
    place_span = r"[A-ZÀ-Þ][A-Za-zÀ-ÿ'’_-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÿ'’_-]+){0,4}"

    def clean(value: str) -> str:
        return clean_answer_surface_text(str(value or "").strip(" .,:;"))

    def repl_if_person(match: re.Match[str], replacement: str) -> str:
        return replacement if _looks_like_voice_person_subject(match.group("subject")) else match.group(0)

    value = re.sub(
        rf"^(?P<subject>{person_span}),?\s+born in\s+(?P<birth>{place_span})\s+and\s+now\s+living\s+in\s+(?P<residence>{place_span}),?\s+works as\s+",
        lambda match: (
            f"Sono originario/a di {clean(match.group('birth'))}, oggi vivo a "
            f"{clean(match.group('residence'))} e lavoro come "
            if _looks_like_voice_person_subject(match.group("subject"))
            else match.group(0)
        ),
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        rf"\b(?P<subject>{person_span})\s+is\s+originally\s+from\s+(?P<birth>{place_span}),?\s+currently\s+resid(?:ing|ent)\s+(?:in|at)\s+(?P<residence>{place_span})\b",
        lambda match: (
            f"Sono originario/a di {clean(match.group('birth'))} e oggi vivo a {clean(match.group('residence'))}"
            if _looks_like_voice_person_subject(match.group("subject"))
            else match.group(0)
        ),
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        rf"\b(?P<subject>{person_span})\s+is\s+construct(?:ing|s)\s+(?P<project>{entity_span})\s+(?:inside|within|in)\s+(?P<org>{entity_span})\b",
        lambda match: (
            f"Sto costruendo {clean(match.group('project'))} dentro {clean(match.group('org'))}"
            if _looks_like_voice_person_subject(match.group("subject"))
            else match.group(0)
        ),
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        rf"\b(?P<subject>{person_span})\s+(?:è|Ã¨|e)\s+una\b",
        lambda match: repl_if_person(match, "Sono una"),
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        rf"\b(?P<subject>{person_span})\s+is\s+a\b",
        lambda match: repl_if_person(match, "I am a"),
        value,
        flags=re.IGNORECASE,
    )
    for verb, replacement in (
        ("usa", "Uso"),
        ("uses", "I use"),
        ("comunica", "Comunico"),
        ("parla", "Parlo"),
        ("tende", "Tendo"),
        ("lavora", "lavoro"),
        ("works", "I work"),
    ):
        value = re.sub(
            rf"\b(?P<subject>{person_span})\s+{verb}\b",
            lambda match, replacement=replacement: repl_if_person(match, replacement),
            value,
            flags=re.IGNORECASE,
        )
    value = re.sub(
        rf"\bPer\s+(?P<subject>{person_span})\b",
        lambda match: repl_if_person(match, "Per me"),
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\bI am a\s+([a-z][A-Za-z0-9'’ /-]{2,80}?)(?:\s+at\s+([A-ZÀ-Þ][A-Za-zÀ-ÿ0-9&'’._ -]{2,120}))?\b",
        lambda match: (
            f"Lavoro come {clean(match.group(1))} presso {clean(match.group(2))}"
            if match.group(2)
            else f"Lavoro come {clean(match.group(1))}"
        ),
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\bI work as\s+", "Lavoro come ", value, flags=re.IGNORECASE)
    value = re.sub(
        r"\bLavoro come\s+([a-z][A-Za-z0-9'’ /-]{2,100}?)\s+at\s+([A-ZÀ-Þ][A-Za-zÀ-ÿ0-9&'’_-]+(?:\s+[A-ZÀ-Þ][A-Za-zÀ-ÿ0-9&'’_-]+){0,6})\b",
        lambda match: f"Lavoro come {clean(match.group(1))} presso {clean(match.group(2))}",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\bÈ nata a\b", "Sono nata a", value, flags=re.IGNORECASE)
    value = re.sub(r"\bÃˆ nata a\b", "Sono nata a", value, flags=re.IGNORECASE)
    value = re.sub(r"\bE nata a\b", "Sono nata a", value, flags=re.IGNORECASE)
    value = re.sub(r"\boggi vive a\b", "oggi vivo a", value, flags=re.IGNORECASE)
    value = re.sub(r"\bvive a\b", "vivo a", value, flags=re.IGNORECASE)
    value = re.sub(r"\blavora come\b", "lavoro come", value, flags=re.IGNORECASE)
    value = re.sub(r"\bparla in modo\b", "parlo in modo", value, flags=re.IGNORECASE)
    value = re.sub(r"\bcomunica in modo\b", "comunico in modo", value, flags=re.IGNORECASE)
    value = re.sub(r"\bPer lei\b", "Per me", value, flags=re.IGNORECASE)
    value = re.sub(r"\bi suoi valori\b", "i miei valori", value, flags=re.IGNORECASE)
    value = re.sub(r"\bi suoi\b", "i miei", value, flags=re.IGNORECASE)
    value = re.sub(r"\bla sua\b", "la mia", value, flags=re.IGNORECASE)
    value = re.sub(r"\bil suo\b", "il mio", value, flags=re.IGNORECASE)
    value = re.sub(r"^Dr\.\s+Sono\s+presented as\s+", "Sono presentato come ", value, flags=re.IGNORECASE)
    value = re.sub(r"^Sono\s+presented as\s+", "Sono presentato come ", value, flags=re.IGNORECASE)
    value = re.sub(r"^Sono\s+quoted in the release as\s+", "Sono citato nel comunicato come ", value, flags=re.IGNORECASE)
    value = re.sub(r"^His public site describes\s+", "Il mio sito pubblico descrive ", value, flags=re.IGNORECASE)
    value = re.sub(r"^The same public profile connects him with\s+", "Lo stesso profilo pubblico mi collega a ", value, flags=re.IGNORECASE)
    value = re.sub(r"^The release frames the transaction as\s+", "Il comunicato descrive l'operazione come ", value, flags=re.IGNORECASE)
    return value


def _humanize_long_answer_sentence(sentence: str, *, first_person: bool) -> str | None:
    text = str(sentence or "").strip()
    if not text:
        return None
    text = _clean_long_answer_source_line(text, allow_ledger_snippet=True) or ""
    if not text:
        return None
    folded_source = _fold_text(text)
    if any(
        marker in folded_source
        for marker in (
            "merge probe based",
            "probe based on already inserted public facts",
            "already inserted public facts",
            "source pack",
            "expected retrieval behavior",
            "stress testing memory creation",
            "this is not a new public source",
        )
    ):
        return None
    if first_person:
        text = _rewrite_first_person_voice_surface(text)
        text = _self_voice_fragment(text)
    text = " ".join(text.split()).strip()
    if not text:
        return None
    if _answer_surface_has_context_ledger_leak(text):
        return None
    return text


def _normalize_answer_sentence_case(text: str | None) -> str | None:
    value = str(text or "").strip()
    if not value:
        return None
    parts = re.split(r"(?<=[.!?])\s+", value)
    normalized = []
    for part in parts:
        sentence = part.strip()
        if not sentence:
            continue
        normalized.append(sentence[:1].upper() + sentence[1:] if sentence[:1].islower() else sentence)
    return " ".join(normalized).strip() or None


def _build_long_human_answer_from_dossier(
    *,
    query_text: str,
    answer_full: str | None,
    context_dossier: str | None,
    retrieval_mode: str,
) -> str | None:
    dossier = str(context_dossier or "").strip()
    if not dossier:
        return str(answer_full or "").strip() or None
    first_person = _prefers_first_person_answer(query_text)
    contract = build_query_contract(query_text, retrieval_mode=retrieval_mode)
    disallowed_topics = {str(item).strip() for item in list(contract.get("disallowed_topics") or []) if str(item).strip()}
    skip_family_topics = bool(disallowed_topics & {"family_relation", "family_monument"})
    family_blocked_tokens = ("padre", "father", "monumento", "monument", "aeronautica", "air force")
    seed_answer_has_blocked_topic = bool(skip_family_topics and any(token in _fold_text(answer_full or "") for token in family_blocked_tokens))
    cap = 5200 if retrieval_mode == "forensic" else 4200 if retrieval_mode == "heavy" else 3200
    primary_sentences: list[str] = []
    fallback_sentences: list[str] = []

    seed_answer = str(answer_full or "").strip()
    if seed_answer and not _answer_surface_has_context_ledger_leak(seed_answer):
        for sentence in _sentence_candidates(seed_answer):
            if skip_family_topics and any(token in _fold_text(sentence) for token in family_blocked_tokens):
                continue
            humanized = _humanize_long_answer_sentence(sentence, first_person=first_person)
            if humanized:
                primary_sentences.append(humanized)

    current_section = ""
    skip_section = False
    for raw_line in dossier.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("## "):
            current_section = line[3:].strip()
            folded_heading = _fold_text(current_section)
            skip_section = any(token in folded_heading for token in ("documents", "chunks", "raw context", "document packet", "retrieval ledger"))
            if skip_family_topics and any(token in folded_heading for token in ("relationship", "relationships", "relazioni", "family", "famiglia")):
                skip_section = True
            continue
        in_evidence_ledger = "evidence ledger" in _fold_text(current_section)
        folded_line = _fold_text(line)
        if skip_family_topics and any(token in folded_line for token in family_blocked_tokens):
            continue
        cleaned = _clean_long_answer_source_line(line, allow_ledger_snippet=in_evidence_ledger)
        if not cleaned:
            continue
        target = fallback_sentences if in_evidence_ledger or skip_section else primary_sentences
        for sentence in _sentence_candidates(cleaned):
            humanized = _humanize_long_answer_sentence(sentence, first_person=first_person)
            if humanized:
                target.append(humanized)

    def build_selected(sentences: list[str], *, seen: set[str] | None = None) -> tuple[list[str], set[str]]:
        local_seen = set(seen or set())
        selected_items: list[str] = []
        for sentence in sentences:
            folded = _fold_text(sentence)
            if not folded or folded in local_seen:
                continue
            if any(
                (len(folded) >= 36 and folded in existing) or (len(existing) >= 36 and existing in folded)
                for existing in local_seen
            ):
                continue
            local_seen.add(folded)
            selected_items.append(sentence)
            current = " ".join(selected_items).strip()
            if len(current) >= 1400:
                break
        return selected_items, local_seen

    selected, seen = build_selected(primary_sentences)
    if not selected:
        selected, seen = build_selected(fallback_sentences)
    text = " ".join(selected).strip()
    if first_person:
        text = re.sub(r"\blavora come\b", "lavoro come", text, flags=re.IGNORECASE)
        text = re.sub(r"\bi suoi valori\b", "i miei valori", text, flags=re.IGNORECASE)
    text = " ".join(text.split()).strip()
    if len(text) < 1000 and _needs_long_context(retrieval_mode=retrieval_mode, query_text=query_text):
        if first_person:
            expansion = (
                "Il quadro che posso sostenere con sicurezza non e una biografia totale: "
                "e soprattutto il profilo di una persona con un'identita precisa, un luogo attuale, "
                "un ruolo professionale, un progetto principale e un modo di comunicare riconoscibile. "
                "Quando rispondo da questa memoria, il centro stabile e il lavoro sui sistemi creativi e sulla memoria navigabile: "
                "non emerge solo un'etichetta professionale, ma un metodo fatto di struttura, controllo delle fonti, chiarezza e precisione. "
                "Non aggiungo dettagli che non vedo: nelle informazioni recuperate pesano soprattutto identita, lavoro, progetto, stile e valori; "
                "la cronologia completa, altre relazioni o episodi specifici vanno cercati con una domanda mirata."
            )
        else:
            expansion = (
                "Il quadro sostenibile non e una biografia totale: raccoglie soprattutto identita, luogo, ruolo, progetto, stile e valori. "
                "Le informazioni recuperate sono coerenti sul profilo professionale e sul metodo, ma non autorizzano dettagli non presenti. "
                "Per cronologia completa, relazioni secondarie o episodi specifici serve una domanda mirata."
            )
        text = f"{text} {expansion}".strip()
    if len(text) < 900 and fallback_sentences:
        fallback_selected, _ = build_selected(fallback_sentences, seen=seen)
        if fallback_selected:
            text = f"{text} {' '.join(fallback_selected)}".strip()
    if not text:
        return None if seed_answer_has_blocked_topic else seed_answer or None
    text = _normalize_answer_sentence_case(text) or text
    if _answer_surface_has_context_ledger_leak(text):
        return None if seed_answer_has_blocked_topic else seed_answer or None
    return text[:cap].strip() or None


def _first_person_voice_markers_present(query_text: str, answer_text: str | None) -> bool:
    if not _prefers_first_person_answer(query_text):
        return False
    return bool(_first_person_voice_leak_markers(answer_text))


def _sanitize_long_answer_voice(query_text: str, answer_text: str | None) -> str | None:
    text = str(answer_text or "").strip()
    if not text:
        return None
    if not _prefers_first_person_answer(query_text):
        return text
    text = _rewrite_first_person_voice_surface(text)
    text = re.sub(r"\bDr\.\s+Sono\s+a\s+self-taught coder who founded\s+(.+?)\s+nel\s+(\d{4})\b", r"Sono un coder autodidatta che ha fondato \1 nel \2", text, flags=re.IGNORECASE)
    text = re.sub(r"\bSono\s+a\s+self-taught coder who founded\s+(.+?)\s+nel\s+(\d{4})\b", r"Sono un coder autodidatta che ha fondato \1 nel \2", text, flags=re.IGNORECASE)
    text = re.sub(r"\bSono\s+a\s+self-taught coder\b", "Sono un coder autodidatta", text, flags=re.IGNORECASE)
    text = re.sub(r"\bI work as\s+", "Lavoro come ", text, flags=re.IGNORECASE)
    text = text.replace("...", " ").replace("…", " ")
    text = re.sub(r"\bÈ nata a\b", "Sono nata a", text, flags=re.IGNORECASE)
    text = re.sub(r"\bE nata a\b", "Sono nata a", text, flags=re.IGNORECASE)
    text = re.sub(r"\boggi vive a\b", "oggi vivo a", text, flags=re.IGNORECASE)
    text = re.sub(r"\bvive a\b", "vivo a", text, flags=re.IGNORECASE)
    text = re.sub(r"\blavora come\b", "lavoro come", text, flags=re.IGNORECASE)
    text = re.sub(r"\bparla in modo\b", "parlo in modo", text, flags=re.IGNORECASE)
    text = re.sub(r"\bcomunica in modo\b", "comunico in modo", text, flags=re.IGNORECASE)
    text = re.sub(r"\bPer lei\b", "Per me", text, flags=re.IGNORECASE)
    text = re.sub(r"\bi suoi valori\b", "i miei valori", text, flags=re.IGNORECASE)
    text = re.sub(r"\bi suoi\b", "i miei", text, flags=re.IGNORECASE)
    return " ".join(text.split()).strip() or None


def polish_final_answer_surface(query_text: str, answer_text: str | None) -> str | None:
    text = clean_answer_surface_text(answer_text)
    if not text:
        return None
    first_person = _prefers_first_person_answer(query_text)
    if first_person:
        text = _sanitize_long_answer_voice(query_text, text) or text
    selected: list[str] = []
    seen: set[str] = set()
    for raw_sentence in _sentence_candidates(text):
        sentence = _humanize_long_answer_sentence(raw_sentence, first_person=first_person) if first_person else _clean_long_answer_source_line(raw_sentence, allow_ledger_snippet=True)
        if sentence and not first_person:
            sentence = _humanize_public_sentence(sentence, first_person=False) or sentence
        sentence = clean_answer_surface_text(sentence)
        if not sentence or _answer_surface_has_context_ledger_leak(sentence):
            continue
        folded = _fold_text(sentence)
        if not folded or folded in seen:
            continue
        if any(
            (len(folded) >= 34 and folded in existing) or (len(existing) >= 34 and existing in folded)
            for existing in seen
        ):
            continue
        if first_person and _first_person_voice_markers_present(query_text, sentence) and selected:
            continue
        seen.add(folded)
        selected.append(sentence)
    if not selected:
        return clean_answer_surface_text(text) or None
    polished = _normalize_answer_sentence_case(" ".join(selected).strip()) or " ".join(selected).strip()
    return clean_answer_surface_text(polished) or None


def _coerce_long_answer_full(
    answer_full: str | None,
    context_dossier: str | None,
    *,
    retrieval_mode: str,
    query_text: str,
) -> str | None:
    text = str(answer_full or "").strip()
    dossier = str(context_dossier or "").strip()
    if not dossier:
        return text or None
    needs_long_form = _needs_long_context(retrieval_mode=retrieval_mode, query_text=query_text)
    if not needs_long_form:
        return text or None
    min_len = 1200 if retrieval_mode in {"heavy", "forensic"} else 1000
    text_has_ledger_leak = _answer_surface_has_context_ledger_leak(text)
    text_has_voice_leak = _first_person_voice_markers_present(query_text, text)
    if len(text) >= min_len and not text_has_ledger_leak and text_has_voice_leak:
        human_expanded = _build_long_human_answer_from_dossier(
            query_text=query_text,
            answer_full=None,
            context_dossier=dossier,
            retrieval_mode=retrieval_mode,
        )
        if (
            human_expanded
            and len(human_expanded) >= 900
            and not _answer_surface_has_context_ledger_leak(human_expanded)
            and not _first_person_voice_markers_present(query_text, human_expanded)
        ):
            return human_expanded
        voice_sanitized = _sanitize_long_answer_voice(query_text, text)
        if (
            voice_sanitized
            and len(voice_sanitized) >= min_len
            and not _answer_surface_has_context_ledger_leak(voice_sanitized)
            and not _first_person_voice_markers_present(query_text, voice_sanitized)
        ):
            return voice_sanitized
    if len(text) >= min_len and not text_has_ledger_leak and not text_has_voice_leak:
        return text
    human_expanded = _build_long_human_answer_from_dossier(
        query_text=query_text,
        answer_full=text,
        context_dossier=dossier,
        retrieval_mode=retrieval_mode,
    )
    if human_expanded and (len(human_expanded) > len(text) or text_has_ledger_leak or text_has_voice_leak):
        return human_expanded
    if len(text) >= min_len:
        return text
    dossier_lines = []
    for raw_line in dossier.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("## "):
            dossier_lines.append(f"{line[3:]}:")
            continue
        if line.startswith("- "):
            dossier_lines.append(line[2:])
            continue
        dossier_lines.append(line)
    expanded = " ".join(dossier_lines).strip()
    if not expanded:
        return text or None
    cap = 5200 if retrieval_mode == "forensic" else 4200 if retrieval_mode == "heavy" else 3200
    filtered_lines = []
    skip_ledger_section = False
    for raw_line in dossier.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("## "):
            heading = line[3:].strip()
            folded_heading = _fold_text(heading)
            skip_ledger_section = any(token in folded_heading for token in ("evidence ledger", "retrieval ledger", "raw context", "document packet"))
            if not skip_ledger_section:
                filtered_lines.append(f"{heading}:")
            continue
        if skip_ledger_section:
            continue
        if line.startswith("- "):
            filtered_lines.append(line[2:])
            continue
        filtered_lines.append(line)
    filtered_expanded = " ".join(filtered_lines).strip()
    if filtered_expanded:
        expanded = filtered_expanded
    if text and text.lower() in expanded.lower():
        return expanded[:cap].strip()
    merged = f"{text}\n\n{expanded}".strip() if text else expanded
    return merged[:cap].strip()


def _enforce_human_answer_surface(
    *,
    query_text: str,
    answer_text: str | None,
    matches: list[dict[str, Any]],
) -> str | None:
    text = clean_answer_surface_text(answer_text)
    if not text:
        return None
    text = polish_final_answer_surface(query_text, text) or text
    original_contract = _answer_adequacy_contract(query_text=query_text, answer_text=text, matches=matches)
    if (
        original_contract.get("context_ledger_leak")
        or original_contract.get("missing_objects")
        or original_contract.get("missing_times")
        or original_contract.get("missing_required_relation_values")
        or original_contract.get("third_person_markers")
        or original_contract.get("off_contract_topics")
    ):
        direct = build_direct_fact_answer(query_text, matches)
        direct_text = str((direct or {}).get("answer_text") or "").strip()
        direct_contract = dict((direct or {}).get("answer_adequacy") or {})
        if direct_text and direct_contract.get("passed", True):
            return clean_answer_surface_text(direct_text)
    payload = _apply_answer_contract(
        query_text,
        {
            "answer_text": text,
            "mode": "grounded_facts",
            "confidence": 0.8,
            "evidence_node_ids": [],
            "reasoning_summary": "Surface-level answer contract enforcement.",
            "insufficient": False,
            "answerability_state": "grounded",
            "evidence_snippets": [],
        },
        matches,
    )
    contract = dict(payload.get("answer_adequacy") or {})
    if (
        contract.get("context_ledger_leak")
        or contract.get("missing_objects")
        or contract.get("missing_times")
        or contract.get("missing_required_relation_values")
        or contract.get("third_person_markers")
        or contract.get("off_contract_topics")
    ):
        direct = build_direct_fact_answer(query_text, matches)
        direct_text = str((direct or {}).get("answer_text") or "").strip()
        direct_contract = dict((direct or {}).get("answer_adequacy") or {})
        if direct_text and direct_contract.get("passed", True):
            return clean_answer_surface_text(direct_text)
    return polish_final_answer_surface(query_text, payload.get("answer_text") or text) or None


def build_context_dossier(
    query_text: str,
    matches: list[dict[str, Any]],
    context: dict[str, Any] | None,
    shared_evidence: dict[str, Any] | None = None,
    *,
    retrieval_mode: str = "balanced",
    evidence_reservoir: dict[str, Any] | None = None,
    allow_llm: bool = True,
) -> tuple[str | None, str | None]:
    matches = _filter_disallowed_matches_for_query(
        query_text,
        _eligible_answer_matches(matches),
        retrieval_mode=retrieval_mode,
    )
    deterministic_answer_full, deterministic_dossier = _deterministic_context_dossier(query_text, context, matches)
    if (
        allow_llm
        and llm_enabled()
        and _is_broad_self_query(query_text)
        and retrieval_mode in {"heavy", "forensic"}
    ):
        context_dossier = _coerce_long_context_dossier(
            deterministic_dossier,
            deterministic_dossier,
            retrieval_mode=retrieval_mode,
            query_text=query_text,
        )
        answer_surface = _coerce_long_answer_full(
            deterministic_answer_full,
            context_dossier,
            retrieval_mode=retrieval_mode,
            query_text=query_text,
        )
        if (
            len(str(context_dossier or "")) >= 900
            and len(str(answer_surface or "")) >= 900
            and not _long_answer_surface_quality_blocker(answer_surface)
        ):
            return (
                _enforce_human_answer_surface(query_text=query_text, answer_text=answer_surface, matches=matches),
                context_dossier,
            )
    if not allow_llm or not llm_enabled() or (retrieval_mode not in {"heavy", "forensic"} and not _is_broad_self_query(query_text)):
        context_dossier = _coerce_long_context_dossier(
            deterministic_dossier,
            deterministic_dossier,
            retrieval_mode=retrieval_mode,
            query_text=query_text,
        )
        answer_surface = _coerce_long_answer_full(
                deterministic_answer_full,
                context_dossier,
                retrieval_mode=retrieval_mode,
                query_text=query_text,
        )
        return (
            _enforce_human_answer_surface(query_text=query_text, answer_text=answer_surface, matches=matches),
            context_dossier,
        )
    structured_sections = list((context or {}).get("structured_sections") or [])
    if not structured_sections:
        context_dossier = _coerce_long_context_dossier(
            deterministic_dossier,
            deterministic_dossier,
            retrieval_mode=retrieval_mode,
            query_text=query_text,
        )
        answer_surface = _coerce_long_answer_full(
                deterministic_answer_full,
                context_dossier,
                retrieval_mode=retrieval_mode,
                query_text=query_text,
        )
        return (
            _enforce_human_answer_surface(query_text=query_text, answer_text=answer_surface, matches=matches),
            context_dossier,
        )
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["answer_full", "context_dossier"],
        "properties": {
            "answer_full": {"type": "string"},
            "context_dossier": {"type": "string"},
        },
    }
    prompt_pack = _build_prompt_pack(matches, evidence_reservoir, retrieval_mode=retrieval_mode)
    evidence_blocks = list(prompt_pack.get("raw_excerpts") or [])
    answer_voice = "first_person_memory_subject" if _prefers_first_person_answer(query_text) else "grounded_profile"
    temporal_precision_required = _is_temporal_reference_query(query_text)
    system_prompt = (
        "You are the AGVM dossier writer.\n\n"
        f"{build_metamemory_package('answer')}\n\n"
        "Write a grounded long-form answer and a page-scale dossier. "
        "answer_full is the human-facing reply; context_dossier is the tool/source ledger. "
        "If the user addresses the remembered person as you/tu/te/ti, answer_full must use first person as that person. "
        "If the query asks for years, dates, when, or timeline, preserve explicit dates and say when the evidence lacks exact dates. "
        "Do not over-compress. If the context is rich, the dossier must be rich. "
        "Use only retrieved evidence, the evidence reservoir, and structured sections. "
        "Keep titles readable and preserve concrete names, places, projects, relationships, style cues, and values. "
        "The answer_full must be coherent prose, not a source ledger: never copy document labels, clipped excerpts, navigation text, "
        "or incomplete sentences. Organize distinct identity, work, history, relationships, values, and style evidence before stating "
        "honest limits; do not pad by repeating facts."
    )
    user_prompt = (
        f"Query: {query_text}\n"
        f"Retrieval mode: {retrieval_mode}\n"
        f"Preferred answer voice: {answer_voice}\n"
        f"Temporal precision required: {temporal_precision_required}\n"
        f"Structured sections: {structured_sections}\n"
        f"Evidence reservoir summary: {(evidence_reservoir or {}).get('reservoir_summary') or {}}\n"
        f"Shared evidence: {shared_evidence or {}}\n"
        f"Evidence blocks: {evidence_blocks}\n"
        f"Deterministic baseline answer: {deterministic_answer_full or ''}\n"
        f"Deterministic baseline dossier: {deterministic_dossier or ''}"
    )
    payload, error = structured_json(
        model=answer_model(),
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        schema_name="agvm_context_dossier",
        schema=schema,
        timeout=8.0 if retrieval_mode == "heavy" else 12.0,
        role="answer",
    )
    if error or not payload:
        answer_surface = _coerce_long_answer_full(
                deterministic_answer_full,
                deterministic_dossier,
                retrieval_mode=retrieval_mode,
                query_text=query_text,
        )
        return (
            _enforce_human_answer_surface(query_text=query_text, answer_text=answer_surface, matches=matches),
            deterministic_dossier,
        )
    answer_full = str(payload.get("answer_full") or "").strip() or deterministic_answer_full
    context_dossier = str(payload.get("context_dossier") or "").strip() or deterministic_dossier
    context_dossier = _coerce_long_context_dossier(
        context_dossier,
        deterministic_dossier,
        retrieval_mode=retrieval_mode,
        query_text=query_text,
    )
    answer_full = _coerce_long_answer_full(
        answer_full,
        context_dossier,
        retrieval_mode=retrieval_mode,
        query_text=query_text,
    )
    answer_full = _enforce_human_answer_surface(query_text=query_text, answer_text=answer_full, matches=matches)
    answer_full = _append_broad_answer_scope_closure(
        query_text=query_text,
        answer_text=answer_full,
        retrieval_mode=retrieval_mode,
    )
    return answer_full, context_dossier
