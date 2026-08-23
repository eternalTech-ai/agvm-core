# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from typing import Any


DOCUMENT_EVIDENCE_LANE_SCHEMA_VERSION = "agvm.document_evidence_lane.v1"

_STOPWORDS = {
    "a",
    "about",
    "above",
    "after",
    "also",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "can",
    "che",
    "con",
    "da",
    "de",
    "dei",
    "del",
    "della",
    "delle",
    "di",
    "do",
    "does",
    "document",
    "documenti",
    "documento",
    "due",
    "e",
    "for",
    "from",
    "gli",
    "has",
    "have",
    "i",
    "il",
    "in",
    "into",
    "is",
    "it",
    "la",
    "le",
    "lo",
    "mi",
    "mostra",
    "mostrami",
    "of",
    "on",
    "or",
    "per",
    "qual",
    "quale",
    "quali",
    "show",
    "source",
    "sources",
    "support",
    "supports",
    "that",
    "the",
    "this",
    "to",
    "trova",
    "trovi",
    "un",
    "una",
    "with",
}

_LOW_SIGNAL_CLAIM_TERMS = {
    "associated",
    "caused",
    "causes",
    "consist",
    "consisting",
    "consists",
    "decrease",
    "decreased",
    "decreases",
    "effect",
    "effects",
    "enable",
    "enabled",
    "enables",
    "gene",
    "genes",
    "genetic",
    "larger",
    "map",
    "mapping",
    "maps",
    "project",
    "property",
    "properties",
    "provided",
    "related",
    "trigger",
    "triggered",
    "triggers",
    "sequence",
    "sequences",
    "show",
    "shows",
}

_TERM_VARIANT_OVERRIDES = {
    "blood": ["blood", "serum", "plasma"],
    "plasma": ["plasma", "blood", "serum"],
    "positivity": ["positivity", "positive"],
    "serum": ["serum", "blood", "plasma"],
    "weight": ["weight", "birthweight"],
}

_CLAIM_ACTIVE_RELATION_TERMS = {
    "increase",
    "increased",
    "increases",
    "decrease",
    "decreased",
    "decreases",
    "reduce",
    "reduced",
    "reduces",
    "promote",
    "promoted",
    "promotes",
    "inhibit",
    "inhibited",
    "inhibits",
    "prevent",
    "prevented",
    "prevents",
    "cause",
    "caused",
    "causes",
    "induce",
    "induced",
    "induces",
}

_CLAIM_PASSIVE_RELATION_TERMS = {
    "associated",
    "caused",
    "linked",
    "mediated",
    "triggered",
}

_CLAIM_RELATION_TERMS = _CLAIM_ACTIVE_RELATION_TERMS | _CLAIM_PASSIVE_RELATION_TERMS

_RELATIONSHIP_ORDER = {
    "primary": 0,
    "supporting": 1,
    "near_miss": 2,
    "related": 3,
    "background": 4,
    "excluded": 5,
}


def _fold_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_only = "".join(char for char in normalized if not unicodedata.combining(char))
    sanitized = re.sub(r"[^\w\s]", " ", ascii_only.lower())
    return " ".join(sanitized.strip().split())


def _clean_text(value: Any, *, limit: int = 5000) -> str:
    text = " ".join(str(value or "").split())
    if limit > 0 and len(text) > limit:
        return text[:limit].rstrip()
    return text


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def _unique_strings(values: list[Any], *, limit: int = 24) -> list[str]:
    rows: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clean_text(value, limit=260)
        key = _fold_text(text)
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append(text)
        if len(rows) >= limit:
            break
    return rows


def _target_text(query_text: str, semantic_contract: dict[str, Any] | None) -> str:
    contract = _as_dict(semantic_contract)
    target_need = _as_dict(contract.get("target_document_need"))
    target_contract = _as_dict(contract.get("target_document_need_contract"))
    nested_target_need = _as_dict(target_contract.get("target_document_need"))
    candidates = [
        target_need.get("ranking_target_text"),
        target_need.get("target_text"),
        target_need.get("claim_text"),
        nested_target_need.get("ranking_target_text"),
        nested_target_need.get("target_text"),
        target_contract.get("ranking_target_text"),
        target_contract.get("target_text"),
        query_text,
    ]
    for candidate in candidates:
        text = _clean_text(candidate, limit=1200)
        if text:
            return text
    return ""


def _claim_terms(text: str, *, limit: int = 36) -> list[str]:
    folded = _fold_text(text)
    terms: list[str] = []
    seen: set[str] = set()
    source_tokens = re.findall(r"[a-z0-9_]+", folded)
    for token in source_tokens:
        if token in _STOPWORDS:
            continue
        if len(token) < 3 and not token.isdigit():
            continue
        if token in seen:
            continue
        seen.add(token)
        terms.append(token)
        if len(terms) >= limit:
            break
    bridge_terms = _claim_bridge_terms(source_tokens)
    for term in bridge_terms:
        if term not in seen:
            seen.add(term)
            terms.append(term)
        if len(terms) >= limit:
            break
    return terms


def _claim_bridge_terms(tokens: list[str]) -> list[str]:
    token_set = {token for token in tokens if token}
    bridge: list[str] = []
    if "weight" in token_set:
        if "low" in token_set or "under" in token_set:
            bridge.append("underweight")
        if "high" in token_set or "over" in token_set:
            bridge.append("overweight")
    if "blood" in token_set and ("level" in token_set or "levels" in token_set):
        bridge.extend(["serum", "plasma"])
    return _unique_strings(bridge, limit=8)


def _claim_focus_terms(text: str, terms: list[str], *, limit: int = 8) -> list[str]:
    """Return the claim terms that most define the requested evidence target.

    Document evidence queries often contain a subject plus a relation plus the
    effect/object to prove. The normal personal-memory path should still favor
    broad semantic continuity, but the document lane needs to protect these
    discriminative terms from being drowned by generic lexical overlap.
    """

    term_set = {term for term in terms if _claim_term_weight(term) >= 0.65}
    if not term_set:
        return []
    tokens = re.findall(r"[a-z0-9_]+", _fold_text(text))
    relation_index: int | None = None
    relation_term = ""
    for index, token in enumerate(tokens):
        if token in _CLAIM_RELATION_TERMS:
            relation_index = index
            relation_term = token
            break

    focus: list[str] = []

    def add(values: list[str]) -> None:
        for value in values:
            if value in term_set and value not in focus:
                focus.append(value)
            if len(focus) >= limit:
                return

    if relation_index is not None:
        before = [token for token in tokens[:relation_index] if token in term_set]
        after = [token for token in tokens[relation_index + 1 :] if token in term_set]
        if relation_term in _CLAIM_PASSIVE_RELATION_TERMS and before:
            add(before[-4:])
        else:
            add(after[:6])
        if len(focus) < 2 and after:
            add([term for term in after if len(term) >= 8 or any(char.isdigit() for char in term)])
        if len(focus) < 2 and before:
            add([term for term in before if len(term) >= 8 or any(char.isdigit() for char in term)])

    if not focus:
        add([term for term in terms if len(term) >= 8 or any(char.isdigit() for char in term)])
    if not focus:
        add([term for term in terms[-4:] if _claim_term_weight(term) >= 0.65])
    return focus[:limit]


def _term_variants(term: str) -> list[str]:
    normalized = _fold_text(term)
    if not normalized:
        return []
    variants = list(_TERM_VARIANT_OVERRIDES.get(normalized) or [normalized])
    if normalized.endswith("ies") and len(normalized) > 4:
        variants.append(f"{normalized[:-3]}y")
    if normalized.endswith("ves") and len(normalized) > 4:
        variants.append(f"{normalized[:-3]}f")
    if normalized.endswith("es") and len(normalized) > 4:
        variants.append(normalized[:-2])
    if normalized.endswith("s") and len(normalized) > 3:
        variants.append(normalized[:-1])
    return list(dict.fromkeys(item for item in variants if item))


def _term_in_text(term: str, text: str) -> bool:
    if not text:
        return False
    for variant in _term_variants(term):
        if re.search(rf"\b{re.escape(variant)}\b", text):
            return True
        if len(variant) >= 5 and variant not in {"weight"}:
            for token in re.findall(r"[a-z0-9_]+", text):
                if token == variant:
                    return True
                if len(token) > len(variant) and variant in token and len(token) <= len(variant) + 16:
                    return True
    return False


def _claim_term_weight(term: str) -> float:
    normalized = _fold_text(term)
    if not normalized:
        return 0.0
    if normalized.isdigit():
        return 0.45 if normalized == "000" else 0.22 if len(normalized) <= 2 else 0.32
    if normalized in _CLAIM_RELATION_TERMS:
        return 0.35
    if normalized in _LOW_SIGNAL_CLAIM_TERMS:
        return 0.35
    return 1.0


def _target_entities(text: str, *, limit: int = 16) -> list[str]:
    rows: list[str] = []
    seen: set[str] = set()
    pattern = re.compile(r"\b(?:[A-Z][A-Za-z0-9_-]*|[A-Za-z]*\d[A-Za-z0-9_-]*)(?:\s+(?:[A-Z][A-Za-z0-9_-]*|[A-Za-z]*\d[A-Za-z0-9_-]*))*")
    for match in pattern.finditer(str(text or "")):
        value = _clean_text(match.group(0), limit=160)
        folded = _fold_text(value)
        if not folded or folded in _STOPWORDS or folded in seen:
            continue
        seen.add(folded)
        rows.append(value)
        if len(rows) >= limit:
            break
    return rows


def _append_scalar_text(parts: list[str], value: Any, *, limit: int = 1200) -> None:
    text = _clean_text(value, limit=limit)
    if text:
        parts.append(text)


def _append_iter_text(parts: list[str], value: Any, *, limit: int = 12, item_limit: int = 900) -> None:
    for item in _as_list(value)[:limit]:
        if isinstance(item, dict):
            for key in (
                "title",
                "summary",
                "text",
                "raw_text",
                "evidence_snippet",
                "text_preview",
                "source_label",
                "source_type",
                "reason",
                "shared_tags",
            ):
                _append_scalar_text(parts, item.get(key), limit=item_limit)
        else:
            _append_scalar_text(parts, item, limit=item_limit)


def _metadata_text(candidate: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in (
        "source_unit_id",
        "source_unit_title",
        "source_unit_kind",
        "source_unit_role",
        "document_role",
        "lookup_role",
        "full_text_mode",
        "workspace_tier",
    ):
        _append_scalar_text(parts, candidate.get(key), limit=500)
    for key in ("source_unit", "source_unit_metadata", "retrieval_affordance", "catalog_index", "coverage"):
        payload = _as_dict(candidate.get(key))
        for nested_value in payload.values():
            if isinstance(nested_value, (dict, list, tuple)):
                _append_iter_text(parts, nested_value, limit=8, item_limit=500)
            else:
                _append_scalar_text(parts, nested_value, limit=500)
    for key in ("project_tags", "entity_tags", "timeline_tags", "topic_tags", "tags"):
        _append_iter_text(parts, candidate.get(key), limit=32, item_limit=160)
    return _fold_text(" ".join(parts))


def _candidate_surfaces(candidate: dict[str, Any]) -> dict[str, str]:
    title_parts: list[str] = []
    source_parts: list[str] = []
    summary_parts: list[str] = []
    body_parts: list[str] = []
    graph_parts: list[str] = []

    for key in ("title", "document_title", "label"):
        _append_scalar_text(title_parts, candidate.get(key), limit=700)
    for key in ("source_label", "source_type", "source_trust", "provenance_summary"):
        _append_scalar_text(source_parts, candidate.get(key), limit=700)
    for key in ("summary", "relevance_summary"):
        _append_scalar_text(summary_parts, candidate.get(key), limit=1400)
    for key in (
        "full_text",
        "raw_text",
        "deferred_raw_text",
        "anchor_raw_text",
        "text",
        "evidence_snippet",
    ):
        _append_scalar_text(body_parts, candidate.get(key), limit=12000)
    for key in ("ordered_chunk_sequence", "chunks", "top_chunk_matches"):
        _append_iter_text(body_parts, candidate.get(key), limit=10, item_limit=1800)
    for key in ("supported_fact_text", "facts", "top_fact_matches"):
        _append_iter_text(body_parts, candidate.get(key), limit=10, item_limit=1400)
    for key in ("source_trace", "linked_documents", "related_documents"):
        _append_iter_text(graph_parts, candidate.get(key), limit=12, item_limit=800)
    return {
        "title": _fold_text(" ".join(title_parts)),
        "source": _fold_text(" ".join(source_parts)),
        "metadata": _metadata_text(candidate),
        "summary": _fold_text(" ".join(summary_parts)),
        "body": _fold_text(" ".join(body_parts)),
        "graph": _fold_text(" ".join(graph_parts)),
    }


def _fit(terms: list[str], text: str) -> float:
    if not terms:
        return 0.0
    weights = [_claim_term_weight(term) for term in terms]
    denominator = sum(weights)
    if denominator <= 0.0:
        return 0.0
    matched_weight = sum(weight for term, weight in zip(terms, weights) if weight > 0.0 and _term_in_text(term, text))
    return matched_weight / denominator


def _matched_terms(terms: list[str], text: str) -> list[str]:
    return [term for term in terms if term and _claim_term_weight(term) > 0.0 and _term_in_text(term, text)]


def _term_covered_by_terms(term: str, covered_terms: set[str]) -> bool:
    folded = _fold_text(term)
    if not folded:
        return False
    if folded in covered_terms:
        return True
    variants = set(_term_variants(folded))
    for covered in covered_terms:
        if covered in variants:
            return True
        if _term_in_text(folded, covered) or _term_in_text(covered, folded):
            return True
    return False


def _claim_rank_idf_by_term(candidate: dict[str, Any]) -> dict[str, float]:
    claim_rank = _document_claim_rank_payload(candidate)
    rows: dict[str, float] = {}
    for term, value in _as_dict(claim_rank.get("idf_by_term")).items():
        folded = _fold_text(term)
        score = _as_float(value, 0.0)
        if folded and score > 0.0:
            rows[folded] = score
    return rows


def _weighted_claim_coverage(
    target_terms: list[str],
    covered_terms: set[str],
    *,
    candidate: dict[str, Any],
) -> tuple[float, list[str]]:
    if not target_terms:
        return 0.0, []
    idf_by_term = _claim_rank_idf_by_term(candidate)
    total = 0.0
    matched = 0.0
    matched_terms: list[str] = []
    for term in target_terms:
        folded = _fold_text(term)
        if not folded or _claim_term_weight(folded) <= 0.0:
            continue
        weight = max(_claim_term_weight(folded), min(6.0, idf_by_term.get(folded, 0.0)))
        total += weight
        if _term_covered_by_terms(folded, covered_terms):
            matched += weight
            matched_terms.append(folded)
    if total <= 0.0:
        return 0.0, []
    return max(0.0, min(1.0, matched / total)), matched_terms


def _semantic_signal(candidate: dict[str, Any]) -> float:
    rank = _as_dict(candidate.get("document_rank"))
    values = [
        candidate.get("query_fit_score"),
        candidate.get("query_fit"),
        candidate.get("exact_match_score"),
        candidate.get("document_rank_score"),
        candidate.get("score"),
        candidate.get("raw_score"),
        rank.get("score"),
        _as_dict(candidate.get("document_evidence_lane")).get("score"),
    ]
    normalized: list[float] = []
    for value in values:
        score = _as_float(value, 0.0)
        if score <= 0:
            continue
        if score > 1.0:
            score = 1.0 - (1.0 / (1.0 + score))
        normalized.append(max(0.0, min(1.0, score)))
    return max(normalized or [0.0])


def _document_claim_rank_payload(candidate: dict[str, Any]) -> dict[str, Any]:
    claim_rank = _as_dict(candidate.get("document_claim_rank"))
    if claim_rank:
        return claim_rank
    document_rank = _as_dict(candidate.get("document_rank"))
    return _as_dict(document_rank.get("claim_rank"))


def _document_claim_rank_signal(candidate: dict[str, Any]) -> float:
    return max(0.0, min(1.0, _as_float(_document_claim_rank_payload(candidate).get("score"), 0.0)))


def _document_claim_rank_terms(candidate: dict[str, Any]) -> list[str]:
    claim_rank = _document_claim_rank_payload(candidate)
    return [
        _fold_text(term)
        for term in _as_list(claim_rank.get("matched_terms"))
        if _fold_text(term)
    ][:24]


def _document_claim_rank_strong_term_count(candidate: dict[str, Any]) -> int:
    strong_terms = {
        term
        for term in _document_claim_rank_terms(candidate)
        if term not in _STOPWORDS and _claim_term_weight(term) >= 0.8 and (len(term) >= 5 or any(char.isdigit() for char in term))
    }
    return len(strong_terms)


def _raw_signal(candidate: dict[str, Any]) -> float:
    raw_state = _as_dict(candidate.get("raw_availability"))
    if bool(raw_state.get("raw_text_available")) or str(raw_state.get("state") or "") == "raw_available":
        return 1.0
    if bool(candidate.get("raw_text_available") or candidate.get("complete_text_available")):
        return 1.0
    if any(str(candidate.get(key) or "").strip() for key in ("full_text", "raw_text", "deferred_raw_text", "anchor_raw_text")):
        return 1.0
    if _as_int(candidate.get("raw_text_char_count") or candidate.get("available_raw_text_char_count")) > 0:
        return 1.0
    return 0.0


def _graph_signal(candidate: dict[str, Any]) -> float:
    coverage = _as_dict(candidate.get("coverage"))
    count = 0
    count += len(_as_list(candidate.get("related_node_ids")))
    count += len(_as_list(candidate.get("linked_documents")))
    count += len(_as_list(candidate.get("source_trace")))
    count += _as_int(candidate.get("chunk_count") or coverage.get("chunk_count"))
    count += _as_int(candidate.get("fact_count") or coverage.get("fact_count"))
    count += _as_int(coverage.get("match_count"))
    return max(0.0, min(1.0, count / 18.0))


def _geometry_signal(candidate: dict[str, Any]) -> float:
    geometry_keys = (
        "position",
        "coordinates",
        "bucket_key",
        "brainhex",
        "topology_brainhex",
        "matrix_bucket",
        "landing_region_ref",
        "current_region",
    )
    return 1.0 if any(candidate.get(key) not in (None, "", [], {}) for key in geometry_keys) else 0.0


def _identity_available(candidate: dict[str, Any]) -> bool:
    return any(
        str(candidate.get(key) or "").strip()
        for key in (
            "document_id",
            "doc_id",
            "anchor_node_id",
            "document_anchor_node_id",
            "node_id",
            "id",
            "title",
            "source_label",
        )
    )


def _retrieve_document_call(candidate: dict[str, Any], *, index: int, target_text: str) -> dict[str, Any]:
    existing = _as_dict(candidate.get("retrieve_document_call"))
    if existing:
        return existing
    document_id = str(
        candidate.get("document_id")
        or candidate.get("doc_id")
        or candidate.get("anchor_node_id")
        or candidate.get("document_anchor_node_id")
        or candidate.get("node_id")
        or candidate.get("id")
        or ""
    ).strip()
    title = _clean_text(candidate.get("title") or candidate.get("source_label") or f"Document {index}", limit=220)
    return {
        "tool_name": "retrieve_document",
        "arguments": {
            "document_id": document_id or f"document_ref_{index}",
            "document_hint": title or f"Document {index}",
            "query_text": target_text or title or f"Document {index}",
            "include_raw_text": True,
            "context_package_mode": "document_full",
            "document_text_policy": "all_raw",
        },
    }


def _score_candidate(
    candidate: dict[str, Any],
    *,
    target_text: str,
    terms: list[str],
    target_entities: list[str],
) -> dict[str, Any]:
    surfaces = _candidate_surfaces(candidate)
    all_text = " ".join(surfaces.values()).strip()
    matched = _matched_terms(terms, all_text)
    missing = [term for term in terms if term not in matched]
    focus_terms = _claim_focus_terms(target_text, terms)
    bridge_terms = set(_claim_bridge_terms(re.findall(r"[a-z0-9_]+", _fold_text(target_text))))
    matched_entities = [
        entity
        for entity in target_entities
        if _fold_text(entity) and _fold_text(entity) in all_text
    ]
    title_score = _fit(terms, surfaces["title"])
    source_score = _fit(terms, surfaces["source"])
    metadata_score = _fit(terms, surfaces["metadata"])
    summary_score = _fit(terms, surfaces["summary"])
    body_score = _fit(terms, surfaces["body"])
    graph_text_score = _fit(terms, surfaces["graph"])
    bridge_term_list = sorted(bridge_terms)
    bridge_central_score = _fit(
        bridge_term_list,
        " ".join(
            surfaces[key]
            for key in ("title", "source")
            if surfaces.get(key)
        ),
    )
    bridge_body_score = _fit(bridge_term_list, surfaces["body"])
    semantic_signal = _semantic_signal(candidate)
    claim_rank_signal = _document_claim_rank_signal(candidate)
    claim_rank_strong_terms = _document_claim_rank_strong_term_count(candidate)
    claim_rank_terms = set(_document_claim_rank_terms(candidate))
    covered_terms = {
        _fold_text(term)
        for term in [*matched, *list(claim_rank_terms)]
        if _fold_text(term)
    }
    focus_score, matched_focus_terms = _weighted_claim_coverage(
        focus_terms,
        covered_terms,
        candidate=candidate,
    )
    idf_coverage_score, _matched_idf_terms = _weighted_claim_coverage(
        terms,
        covered_terms,
        candidate=candidate,
    )
    idf_by_term = _claim_rank_idf_by_term(candidate)
    rare_focus_count = sum(
        1
        for term in matched_focus_terms
        if idf_by_term.get(term, 0.0) >= 2.0 or len(term) >= 8 or any(char.isdigit() for char in term)
    )
    bridge_focus_count = sum(1 for term in matched_focus_terms if term in bridge_terms)
    graph_signal = _graph_signal(candidate)
    geometry_signal = _geometry_signal(candidate)
    raw_signal = _raw_signal(candidate)
    exact_phrase = 0.0
    phrase_terms = [term for term in terms if not term.isdigit()][:8]
    if len(phrase_terms) >= 3:
        exact_phrase = 1.0 if " ".join(phrase_terms) in all_text else 0.0
    matched_ratio = len(matched) / max(1, len(terms))
    missing_penalty = 0.0 if matched else 0.12
    if terms and matched_ratio < 0.18:
        missing_penalty += 0.08
    focus_signal_gate = bool(
        claim_rank_signal > 0.0
        or matched_ratio >= 0.45
        or body_score >= 0.45
        or graph_text_score >= 0.45
        or (focus_score >= 0.25 and (body_score > 0.0 or title_score > 0.0 or metadata_score > 0.0))
    )
    effective_focus_score = focus_score if focus_signal_gate else 0.0
    effective_idf_coverage_score = idf_coverage_score if focus_signal_gate else 0.0
    effective_rare_focus_count = rare_focus_count if focus_signal_gate else 0
    effective_bridge_focus_count = bridge_focus_count if focus_signal_gate else 0
    score = (
        0.18 * title_score
        + 0.12 * source_score
        + 0.14 * metadata_score
        + 0.10 * summary_score
        + 0.24 * body_score
        + 0.05 * graph_text_score
        + 0.09 * semantic_signal
        + 0.16 * claim_rank_signal
        + min(0.08, 0.04 * claim_rank_strong_terms)
        + 0.12 * effective_focus_score
        + 0.06 * effective_idf_coverage_score
        + min(0.08, 0.04 * effective_rare_focus_count)
        + 0.18 * bridge_central_score
        + 0.04 * bridge_body_score
        + 0.04 * graph_signal
        + 0.015 * geometry_signal
        + 0.025 * raw_signal
        + 0.05 * exact_phrase
        - missing_penalty
    )
    if claim_rank_signal >= 0.94 and claim_rank_strong_terms >= 2:
        score = max(score, 0.62)
    elif bridge_central_score >= 1.0 and claim_rank_signal >= 0.45:
        score = max(score, 0.62)
    elif bridge_central_score >= 1.0 and effective_focus_score >= 0.25:
        score = max(score, 0.58)
    elif effective_focus_score >= 0.80 and claim_rank_signal >= 0.72:
        score = max(score, 0.54)
    elif effective_focus_score >= 0.55 and claim_rank_signal >= 0.78:
        score = max(score, 0.50)
    elif claim_rank_signal >= 0.88 and claim_rank_strong_terms >= 1:
        score = max(score, 0.44)
    elif claim_rank_signal >= 0.72 and claim_rank_strong_terms >= 1:
        score = max(score, 0.34)
    elif effective_bridge_focus_count >= 1 and effective_focus_score >= 0.25:
        score = max(score, 0.32)
    score = round(max(0.0, min(1.0, score)), 6)
    components = {
        "title": round(title_score, 6),
        "source": round(source_score, 6),
        "source_unit_metadata": round(metadata_score, 6),
        "summary": round(summary_score, 6),
        "raw_or_chunk_text": round(body_score, 6),
        "graph_text": round(graph_text_score, 6),
        "semantic_or_vector": round(semantic_signal, 6),
        "claim_rank": round(claim_rank_signal, 6),
        "claim_rank_strong_terms": claim_rank_strong_terms,
        "claim_focus": round(effective_focus_score, 6),
        "claim_focus_raw": round(focus_score, 6),
        "claim_focus_signal_gate": focus_signal_gate,
        "claim_focus_terms": focus_terms[:12],
        "claim_bridge_terms": sorted(bridge_terms)[:12],
        "bridge_central_title_or_summary": round(bridge_central_score, 6),
        "bridge_body": round(bridge_body_score, 6),
        "matched_focus_terms": matched_focus_terms[:12],
        "idf_weighted_coverage": round(effective_idf_coverage_score, 6),
        "rare_focus_term_count": effective_rare_focus_count,
        "bridge_focus_term_count": effective_bridge_focus_count,
        "graph_links": round(graph_signal, 6),
        "geometry": round(geometry_signal, 6),
        "raw_availability": round(raw_signal, 6),
        "exact_phrase": round(exact_phrase, 6),
        "missing_penalty": round(missing_penalty, 6),
        "matched_ratio": round(matched_ratio, 6),
    }
    evidence_surfaces = [
        surface
        for surface, value in (
            ("title", title_score),
            ("source", source_score),
            ("source_unit_metadata", metadata_score),
            ("summary", summary_score),
            ("raw_or_chunk_text", body_score),
            ("graph_text", graph_text_score),
            ("semantic_or_vector", semantic_signal),
            ("claim_focus", effective_focus_score),
            ("graph_links", graph_signal),
            ("geometry", geometry_signal),
            ("raw_availability", raw_signal),
        )
        if value > 0.0
    ]
    relationship = "excluded"
    if _identity_available(candidate):
        if (
            (score >= 0.58 and matched_ratio >= 0.38)
            or (semantic_signal >= 0.82 and matched_ratio >= 0.45)
            or (claim_rank_signal >= 0.94 and claim_rank_strong_terms >= 2)
            or (effective_focus_score >= 0.80 and claim_rank_signal >= 0.72)
        ):
            relationship = "primary"
        elif (
            score >= 0.40
            or matched_ratio >= 0.42
            or (claim_rank_signal >= 0.88 and claim_rank_strong_terms >= 1)
            or (bridge_central_score >= 1.0 and claim_rank_signal >= 0.45)
            or (effective_focus_score >= 0.50 and claim_rank_signal >= 0.55)
        ):
            relationship = "supporting"
        elif (
            (score >= 0.30 and matched_ratio >= 0.30)
            or matched_ratio >= 0.30
            or (claim_rank_signal >= 0.72 and claim_rank_strong_terms >= 1)
            or (effective_focus_score >= 0.35 and (claim_rank_signal >= 0.35 or matched_ratio >= 0.50))
            or (effective_bridge_focus_count >= 1 and effective_focus_score >= 0.25)
        ):
            relationship = "near_miss"
        elif (graph_signal >= 0.28 or source_score > 0.0) and matched_ratio >= 0.18:
            relationship = "related"
        else:
            relationship = "background"
    return {
        "candidate": dict(candidate),
        "score": score,
        "relationship_to_query": relationship,
        "matched_claim_terms": matched[:24],
        "missing_claim_terms": missing[:24],
        "matched_entities": _unique_strings(matched_entities, limit=12),
        "score_components": components,
        "evidence_surfaces": evidence_surfaces,
    }


def _row_coverage_terms(row: dict[str, Any], target_terms: list[str]) -> set[str]:
    target = set(target_terms)
    candidate = _as_dict(row.get("candidate"))
    terms = {
        _fold_text(term)
        for term in _as_list(row.get("matched_claim_terms"))
        if _fold_text(term)
    }
    terms.update(term for term in _document_claim_rank_terms(candidate) if term)
    output: set[str] = set()
    for term in terms:
        if term in target:
            output.add(term)
            continue
        for target_term in target:
            if _term_in_text(target_term, term) or _term_in_text(term, target_term):
                output.add(target_term)
    return output


def _diversify_document_evidence_rows(rows: list[dict[str, Any]], *, terms: list[str]) -> list[dict[str, Any]]:
    if len(rows) <= 2 or not terms:
        return rows
    target_terms = [term for term in terms if _claim_term_weight(term) > 0.0]
    if not target_terms:
        return rows
    coverage_by_row = [_row_coverage_terms(row, target_terms) for row in rows]
    frequencies: Counter[str] = Counter(
        term
        for coverage in coverage_by_row
        for term in coverage
    )
    term_weights = {
        term: _claim_term_weight(term) * (1.0 + (1.0 / max(1, frequencies.get(term, 1))))
        for term in target_terms
    }
    total_weight = sum(term_weights.values()) or 1.0
    selected: list[dict[str, Any]] = [rows[0]]
    selected_indexes: set[int] = {0}
    covered = set(coverage_by_row[0])

    def relation_bonus(row: dict[str, Any]) -> float:
        relationship = str(row.get("relationship_to_query") or "background")
        if relationship == "primary":
            return 0.05
        if relationship == "supporting":
            return 0.035
        if relationship == "near_miss":
            return 0.015
        return 0.0

    while len(selected) < len(rows):
        best_index: int | None = None
        best_score = -1.0
        for index, row in enumerate(rows):
            if index in selected_indexes:
                continue
            coverage = coverage_by_row[index]
            new_terms = coverage - covered
            coverage_gain = sum(term_weights.get(term, 0.0) for term in new_terms) / total_weight
            coverage_quality = sum(term_weights.get(term, 0.0) for term in coverage) / total_weight
            strong_new_terms = [
                term
                for term in new_terms
                if term not in _STOPWORDS and _claim_term_weight(term) >= 0.8 and (len(term) >= 5 or any(char.isdigit() for char in term))
            ]
            candidate = _as_dict(row.get("candidate"))
            claim_rank_signal = _document_claim_rank_signal(candidate)
            components = _as_dict(row.get("score_components"))
            focus_score = _as_float(components.get("claim_focus"), 0.0)
            idf_coverage_score = _as_float(components.get("idf_weighted_coverage"), 0.0)
            base_score = _as_float(row.get("score"), 0.0)
            selection_score = (
                0.45 * base_score
                + 0.38 * coverage_gain
                + 0.08 * coverage_quality
                + 0.07 * claim_rank_signal
                + 0.14 * focus_score
                + 0.05 * idf_coverage_score
                + min(0.24, 0.12 * len(strong_new_terms))
                + relation_bonus(row)
            )
            if selection_score > best_score:
                best_score = selection_score
                best_index = index
        if best_index is None:
            break
        selected.append(rows[best_index])
        selected_indexes.add(best_index)
        covered.update(coverage_by_row[best_index])
    return selected


def _protect_central_bridge_row(rows: list[dict[str, Any]], *, visible_limit: int) -> list[dict[str, Any]]:
    if visible_limit <= 0 or len(rows) <= visible_limit:
        return rows
    if any(
        _as_float(_as_dict(row.get("score_components")).get("bridge_central_title_or_summary"), 0.0) >= 1.0
        for row in rows[:visible_limit]
    ):
        return rows
    best_index: int | None = None
    best_key: tuple[float, float, float, str] | None = None
    for index, row in enumerate(rows[visible_limit:], start=visible_limit):
        components = _as_dict(row.get("score_components"))
        bridge_central_score = _as_float(components.get("bridge_central_title_or_summary"), 0.0)
        if bridge_central_score < 1.0:
            continue
        if str(row.get("relationship_to_query") or "") == "excluded":
            continue
        candidate = _as_dict(row.get("candidate"))
        key = (
            bridge_central_score,
            _document_claim_rank_signal(candidate),
            _as_float(row.get("score"), 0.0),
            str(candidate.get("title") or candidate.get("source_label") or ""),
        )
        if best_key is None or key > best_key:
            best_key = key
            best_index = index
    if best_index is None:
        return rows
    protected = rows[best_index]
    ordered = [row for index, row in enumerate(rows) if index != best_index]
    insert_at = min(max(1, visible_limit - 1), len(ordered))
    ordered.insert(insert_at, protected)
    return ordered


def _why_included(row: dict[str, Any]) -> list[str]:
    components = _as_dict(row.get("score_components"))
    reasons: list[str] = []
    if components.get("title", 0.0) >= 0.3:
        reasons.append("title_matches_target_claim")
    if components.get("source", 0.0) > 0.0:
        reasons.append("source_label_or_type_matches_target")
    if components.get("source_unit_metadata", 0.0) > 0.0:
        reasons.append("source_unit_metadata_or_retrieval_affordance_matches")
    if components.get("raw_or_chunk_text", 0.0) > 0.0:
        reasons.append("raw_or_chunk_text_contains_claim_terms")
    if components.get("semantic_or_vector", 0.0) >= 0.45:
        reasons.append("upstream_semantic_or_vector_signal_supports_candidate")
    if components.get("graph_links", 0.0) >= 0.25:
        reasons.append("graph_or_source_trace_links_support_candidate")
    if components.get("raw_availability", 0.0) > 0.0:
        reasons.append("raw_document_can_be_hydrated_with_retrieve_document")
    reasons.append(
        f"{row.get('relationship_to_query')}_document_evidence_score_{float(row.get('score') or 0.0):.2f}"
    )
    return _unique_strings(reasons, limit=10)


def _expected_contents(row: dict[str, Any], *, target_text: str) -> dict[str, Any]:
    matched = list(row.get("matched_claim_terms") or [])[:16]
    missing = list(row.get("missing_claim_terms") or [])[:16]
    if matched:
        summary = "Expected to contain evidence about: " + ", ".join(matched[:8]) + "."
    else:
        summary = "Expected to provide background or source context for the target claim."
    return {
        "schema_version": "agvm.document_expected_contents.v1",
        "target_text": _clean_text(target_text, limit=520),
        "summary": summary,
        "evidence_surfaces": list(row.get("evidence_surfaces") or [])[:12],
        "matched_claim_terms": matched,
        "missing_claim_terms": missing,
    }


def _document_ref(candidate: dict[str, Any], *, index: int, target_text: str) -> dict[str, Any]:
    document_id = str(
        candidate.get("document_id")
        or candidate.get("doc_id")
        or candidate.get("anchor_node_id")
        or candidate.get("document_anchor_node_id")
        or candidate.get("node_id")
        or candidate.get("id")
        or ""
    ).strip()
    title = _clean_text(candidate.get("title") or candidate.get("source_label") or f"Document {index}", limit=220)
    raw_state = _as_dict(candidate.get("raw_availability"))
    raw_available = bool(raw_state.get("raw_text_available") or candidate.get("raw_text_available") or _raw_signal(candidate))
    return {
        "schema_version": "agvm.document_evidence_ref.v1",
        "document_id": document_id or f"document_ref_{index}",
        "anchor_node_id": str(candidate.get("anchor_node_id") or candidate.get("document_anchor_node_id") or "").strip(),
        "title": title or f"Document {index}",
        "source_label": _clean_text(candidate.get("source_label"), limit=220),
        "source_type": candidate.get("source_type"),
        "relationship_to_query": str(candidate.get("relationship_to_query") or "background"),
        "document_evidence_rank": _as_int(candidate.get("document_evidence_rank"), index),
        "document_evidence_score": round(_as_float(candidate.get("document_evidence_score")), 6),
        "why_included": list(candidate.get("why_included") or [])[:10],
        "expected_contents": _as_dict(candidate.get("expected_contents")),
        "matched_claim_terms": list(candidate.get("matched_claim_terms") or [])[:24],
        "missing_claim_terms": list(candidate.get("missing_claim_terms") or [])[:24],
        "matched_entities": list(candidate.get("matched_entities") or [])[:12],
        "score_components": _as_dict(candidate.get("document_evidence_score_components")),
        "raw_text_available": raw_available,
        "raw_available": raw_available,
        "retrieve_document_call": _retrieve_document_call(candidate, index=index, target_text=target_text),
    }


def rank_document_evidence_candidates(
    *,
    query_text: str,
    candidates: list[dict[str, Any]] | None,
    semantic_contract: dict[str, Any] | None = None,
    limit: int = 6,
    candidate_window: int = 24,
) -> dict[str, Any]:
    """Rank already-discovered document candidates without fetching new raw data."""

    target = _target_text(query_text, semantic_contract)
    terms = _claim_terms(target)
    entities = _target_entities(target)
    window = max(1, int(candidate_window or 24))
    rows = [
        _score_candidate(dict(candidate), target_text=target, terms=terms, target_entities=entities)
        for candidate in list(candidates or [])[:window]
        if isinstance(candidate, dict)
    ]
    rows.sort(
        key=lambda row: (
            _RELATIONSHIP_ORDER.get(str(row.get("relationship_to_query") or "background"), 4),
            -float(row.get("score") or 0.0),
            str(_as_dict(row.get("candidate")).get("title") or ""),
        )
    )
    if rows:
        first = rows[0]
        if (
            str(first.get("relationship_to_query") or "") in {"supporting", "near_miss"}
            and float(first.get("score") or 0.0) >= 0.30
            and _as_dict(first.get("score_components")).get("matched_ratio", 0.0) >= 0.24
        ):
            first["relationship_to_query"] = "primary"
    rows = _diversify_document_evidence_rows(rows, terms=terms)
    output_limit = max(0, int(limit or 0))
    if output_limit:
        rows = _protect_central_bridge_row(rows, visible_limit=output_limit)
    annotated: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        candidate = dict(row.get("candidate") or {})
        relationship = str(row.get("relationship_to_query") or "background")
        candidate["document_evidence_lane_schema_version"] = DOCUMENT_EVIDENCE_LANE_SCHEMA_VERSION
        candidate["document_evidence_rank"] = index
        candidate["document_evidence_score"] = round(float(row.get("score") or 0.0), 6)
        candidate["relationship_to_query"] = relationship
        candidate["matched_claim_terms"] = list(row.get("matched_claim_terms") or [])[:24]
        candidate["missing_claim_terms"] = list(row.get("missing_claim_terms") or [])[:24]
        candidate["matched_entities"] = list(row.get("matched_entities") or [])[:12]
        candidate["document_evidence_score_components"] = dict(row.get("score_components") or {})
        candidate["document_evidence_surfaces"] = list(row.get("evidence_surfaces") or [])[:12]
        candidate["why_included"] = _why_included(row)
        candidate["expected_contents"] = _expected_contents(row, target_text=target)
        candidate["document_evidence_retrieve_document_call"] = _retrieve_document_call(
            candidate,
            index=index,
            target_text=target,
        )
        candidate.setdefault("retrieve_document_call", candidate["document_evidence_retrieve_document_call"])
        candidate["claim_fit_summary"] = {
            "schema_version": "agvm.document_claim_fit_summary.v1",
            "target_text": _clean_text(target, limit=520),
            "matched_claim_terms": candidate["matched_claim_terms"],
            "missing_claim_terms": candidate["missing_claim_terms"],
            "matched_entities": candidate["matched_entities"],
            "relationship_to_query": relationship,
            "score": candidate["document_evidence_score"],
        }
        annotated.append(candidate)
    visible = annotated[:output_limit] if output_limit else annotated
    refs = [
        _document_ref(candidate, index=index, target_text=target)
        for index, candidate in enumerate(visible, start=1)
        if str(candidate.get("relationship_to_query") or "") != "excluded"
    ]
    primary_refs = [ref for ref in refs if str(ref.get("relationship_to_query") or "") == "primary"]
    candidate_refs = [
        ref
        for ref in refs
        if str(ref.get("relationship_to_query") or "") in {"primary", "supporting", "near_miss"}
    ]
    related_refs = [
        ref
        for ref in refs
        if str(ref.get("relationship_to_query") or "") in {"supporting", "near_miss", "related"}
    ]
    state_counts: dict[str, int] = {}
    for candidate in visible:
        relationship = str(candidate.get("relationship_to_query") or "background")
        state_counts[relationship] = state_counts.get(relationship, 0) + 1
    return {
        "schema_version": DOCUMENT_EVIDENCE_LANE_SCHEMA_VERSION,
        "target_text": target,
        "claim_terms": terms,
        "target_entities": entities,
        "candidate_count": len(list(candidates or [])),
        "scored_candidate_count": len(rows),
        "returned_candidate_count": len(visible),
        "candidate_window": window,
        "bounded_scoring": True,
        "raw_fetch_performed": False,
        "relationships": state_counts,
        "documents": visible,
        "ranked_document_refs": refs,
        "primary_document_refs": primary_refs,
        "candidate_document_refs": candidate_refs,
        "related_document_refs": related_refs,
        "metrics": {
            "schema_version": "agvm.document_evidence_lane.metrics.v1",
            "primary_document_ref_count": len(primary_refs),
            "candidate_document_ref_count": len(candidate_refs),
            "related_document_ref_count": len(related_refs),
            "excluded_candidate_count": state_counts.get("excluded", 0),
            "max_document_evidence_score": max([float(item.get("document_evidence_score") or 0.0) for item in visible] or [0.0]),
        },
    }
