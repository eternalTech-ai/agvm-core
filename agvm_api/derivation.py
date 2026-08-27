# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import defaultdict
from typing import Any

from source_security import sanitize_source_uri_for_persistence

from ai_modules_v2 import AiModuleContractError, validate_grow_compiler_payload
from config import BUCKET_SIZE, CLAIM_MEMORY_TYPES, ENTITY_MEMORY_TYPES, FACET_FIELDS, ROUTING_FIELDS
from llm import compiler_model, llm_enabled, structured_json
from metamemory import build_metamemory_package
from projection import (
    brainhex_to_position,
    color_from_brainhex,
    compute_radius_value,
    distance,
    heuristic_projection,
    infer_guide_area,
    latent_vector_to_angles,
    lexical_overlap,
    normalize_scores,
    quantize_to_brainhex,
    scores_to_latent_vector,
    summarize_text,
)
from retrieval import build_index, finalize_node, node_for_index, shortlist_atlas_buckets
from memory_hygiene import build_hygiene_metadata, effective_hygiene
from public_v1_geometry_placement import apply_public_v1_geometry_profile_to_seed


CLAIM_TYPES = {
    "fact",
    "identity_claim",
    "value_claim",
    "style_claim",
    "project_claim",
    "relationship_claim",
    "event_claim",
}

ENTITY_TYPES = {
    "person",
    "organization",
    "project",
    "document",
}

_SOURCE_GROUNDING_STOPWORDS = {
    "a",
    "ad",
    "al",
    "alla",
    "and",
    "as",
    "che",
    "con",
    "da",
    "del",
    "della",
    "di",
    "do",
    "document",
    "documento",
    "e",
    "for",
    "from",
    "ha",
    "has",
    "il",
    "in",
    "is",
    "la",
    "le",
    "lo",
    "nel",
    "nella",
    "of",
    "one",
    "or",
    "per",
    "source",
    "the",
    "to",
    "un",
    "una",
    "usa",
    "use",
    "uses",
    "user",
    "was",
    "with",
}


def normalize_preview_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def preserve_node_raw_text(value: str, *, limit: int = 4000) -> str:
    normalized = " ".join(str(value or "").split())
    if len(normalized) <= limit:
        return normalized
    clipped = normalized[: max(0, limit - 3)].rstrip()
    last_boundary = max(clipped.rfind(". "), clipped.rfind("; "), clipped.rfind(", "))
    if last_boundary >= 240:
        clipped = clipped[:last_boundary].rstrip()
    return f"{clipped}..."


def _source_grounding_fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_only = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", ascii_only.lower()).strip()


def _source_grounding_tokens(value: str) -> list[str]:
    folded = _source_grounding_fold(value)
    tokens = [
        token
        for token in folded.split()
        if len(token) >= 3 and token not in _SOURCE_GROUNDING_STOPWORDS
    ]
    return tokens


def _source_grounding_named_tokens(value: str) -> set[str]:
    named: set[str] = set()
    text = str(value or "")
    patterns = (
        r"\b[A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+)+\b",
        r"\b[A-Za-z]+[A-Z][A-Za-z0-9]*\b",
        r"\b[A-Z]{3,}[0-9A-Z-]*\b",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            named.update(_source_grounding_tokens(match.group(0)))
    return named


def _source_grounding_named_phrases(value: str, *, limit: int = 18) -> list[str]:
    text = str(value or "")
    patterns = (
        r"\b[A-Z][A-Za-z0-9À-ÖØ-öø-ÿ'’&.-]+(?:\s+(?:[A-Z][A-Za-z0-9À-ÖØ-öø-ÿ'’&.-]+|of|di|del|della|de|and|&)){1,6}\b",
        r"\b[A-Za-zÀ-ÖØ-öø-ÿ]+[A-Z][A-Za-z0-9À-ÖØ-öø-ÿ'’&.-]*\b",
        r"\b[A-Z]{2,}[0-9A-Z-]*\b",
    )
    phrases: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            candidate = re.sub(r"\s+", " ", match.group(0)).strip(" .,:;")
            if not candidate:
                continue
            folded = _source_grounding_fold(candidate)
            if not folded or folded in _SOURCE_GROUNDING_STOPWORDS:
                continue
            if folded in _SOURCE_GENERIC_ENTITY_TOKENS:
                continue
            if folded in seen:
                continue
            seen.add(folded)
            phrases.append(candidate)
            if len(phrases) >= limit:
                return phrases
    return phrases


_SOURCE_GENDERED_PRONOUN_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"she", "her", "hers", "herself"}),
    frozenset({"he", "him", "his", "himself"}),
)


def _source_grounding_unsupported_pronouns(source_text: str, candidate_text: str) -> list[str]:
    source_tokens = set(_source_grounding_fold(source_text).split())
    candidate_tokens = set(_source_grounding_fold(candidate_text).split())
    unsupported: list[str] = []
    for group in _SOURCE_GENDERED_PRONOUN_GROUPS:
        candidate_group_tokens = sorted(candidate_tokens.intersection(group))
        if candidate_group_tokens and not source_tokens.intersection(group):
            unsupported.extend(candidate_group_tokens)
    return _unique_strings(unsupported, limit=8)


_SOURCE_FOUNDING_RELATION_RE = re.compile(
    r"\b(?:founder|co[- ]founder|founded|co[- ]founded|created|launched|established|fondatore|cofondatore|fondato|creato|lanciato)\b",
    re.IGNORECASE,
)
_SOURCE_FOUNDING_SOURCE_ACTION_RE = re.compile(
    r"\b(?:founder|co[- ]founder|founded|co[- ]founded|created|launched|established|fonda|fondato|crea|creato|lancia|lanciato|stabilisce|stabilito)\b",
    re.IGNORECASE,
)


def _source_relation_drift_reasons(source_text: str, candidate_text: str) -> list[str]:
    if not _SOURCE_FOUNDING_RELATION_RE.search(candidate_text):
        return []
    phrases = _source_grounding_named_phrases(candidate_text, limit=24)
    if len(phrases) < 2:
        return []
    candidate_start = _source_grounding_fold(candidate_text[:96])
    unsupported: list[str] = []
    for phrase in phrases:
        folded_phrase = _source_grounding_fold(phrase)
        if not folded_phrase:
            continue
        # Usually the first named phrase is the memory subject, not the object
        # of the founding relation.
        if folded_phrase and folded_phrase in candidate_start and candidate_start.startswith(folded_phrase):
            continue
        phrase_pattern = re.compile(re.escape(phrase), re.IGNORECASE)
        matches = list(phrase_pattern.finditer(source_text))
        if not matches:
            continue
        relation_supported = False
        for source_clause in re.split(r"(?<=[.!?])\s+|\n+", source_text):
            if not source_clause.strip():
                continue
            if not re.search(re.escape(phrase), source_clause, re.IGNORECASE):
                continue
            if _SOURCE_FOUNDING_SOURCE_ACTION_RE.search(source_clause):
                relation_supported = True
                break
        if not relation_supported:
            unsupported.append(phrase)
    return _unique_strings(unsupported, limit=8)


def _source_has_memory_anchor(value: str) -> bool:
    text = str(value or "")
    if _source_grounding_named_tokens(text):
        return True
    if re.search(r"\b(?:19|20)\d{2}\b|\b\d+[,.]?\d*\b", text):
        return True
    return bool(
        re.search(
            r"\b(?:i|my|mine|me|we|our|ours|sono|mio|mia|miei|mie|noi|nostro|nostra)\b",
            text,
            re.IGNORECASE,
        )
    )


def _strip_generated_source_unit_prefix(value: str) -> str:
    text = str(value or "").strip()
    for _ in range(2):
        stripped = re.sub(
            r"^\s*(?:[#*\-\s]*)?(?:[^:\n]{1,180}\bsegment\s+\d+|section\s+\d+|page\s+title[^:\n]{0,120})\s*:\s*",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()
        if stripped == text:
            break
        text = stripped
    return text


def _source_grounding_requires_filter(*, source_type: str | None, input_mode: str) -> bool:
    folded_source_type = _source_grounding_fold(source_type or "")
    return folded_source_type in {"source_investigation", "source investigation"} or (
        str(input_mode or "").strip().lower() == "document"
        and folded_source_type
        in {
            "uploaded_document",
            "uploaded document",
            "public_web",
            "public web",
            "project_workspace",
            "project workspace",
            "public_dossier",
            "public dossier",
            "reference_library",
            "reference library",
            "technical_document",
            "technical document",
        }
    )


def _source_grounding_assessment(source_text: str, candidate_text: str, *, role: str) -> dict[str, Any]:
    source_folded = f" {_source_grounding_fold(source_text)} "
    candidate_folded = _source_grounding_fold(candidate_text)
    if not candidate_folded:
        return {"supported": False, "score": 0.0, "reason": "empty_candidate"}
    if f" {candidate_folded} " in source_folded:
        return {"supported": True, "score": 1.0, "reason": "exact_source_substring"}

    source_tokens = set(_source_grounding_tokens(source_text))
    candidate_tokens = _source_grounding_tokens(candidate_text)
    if not candidate_tokens:
        return {"supported": False, "score": 0.0, "reason": "no_meaningful_candidate_tokens"}

    matched = [token for token in candidate_tokens if token in source_tokens]
    score = len(matched) / max(1, len(candidate_tokens))
    source_all_tokens = set(_source_grounding_fold(source_text).split())
    unsupported_named_tokens = sorted(token for token in _source_grounding_named_tokens(candidate_text) if token not in source_all_tokens)
    if unsupported_named_tokens:
        return {
            "supported": False,
            "score": round(score, 4),
            "reason": "candidate_introduces_named_tokens_absent_from_source",
            "matched_token_count": len(matched),
            "candidate_token_count": len(candidate_tokens),
            "unsupported_named_tokens": unsupported_named_tokens[:8],
        }
    unsupported_pronouns = _source_grounding_unsupported_pronouns(source_text, candidate_text)
    if unsupported_pronouns:
        return {
            "supported": False,
            "score": round(score, 4),
            "reason": "candidate_introduces_gendered_pronouns_absent_from_source",
            "matched_token_count": len(matched),
            "candidate_token_count": len(candidate_tokens),
            "unsupported_pronouns": unsupported_pronouns,
        }
    unique_candidate_tokens = set(candidate_tokens)
    named_or_temporal_tokens = [
        token
        for token in unique_candidate_tokens
        if token in source_tokens and (token[:1].isdigit() or len(token) >= 5)
    ]
    role_name = str(role or "claim").strip().lower()
    if role_name == "entity":
        supported = score >= 0.75 or bool(candidate_folded and f" {candidate_folded} " in source_folded)
    else:
        supported = score >= 0.68 and len(named_or_temporal_tokens) >= 1 and len(matched) >= min(3, len(unique_candidate_tokens))
    return {
        "supported": bool(supported),
        "score": round(score, 4),
        "reason": "token_source_overlap" if supported else "candidate_not_supported_by_source_text",
        "matched_token_count": len(matched),
        "candidate_token_count": len(candidate_tokens),
    }


def _filter_source_grounded_compiler_nodes(
    *,
    source_text: str,
    input_mode: str,
    source_type: str | None,
    compiled_nodes: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if not _source_grounding_requires_filter(source_type=source_type, input_mode=input_mode):
        return compiled_nodes, None
    kept: list[dict[str, Any]] = []
    filtered: list[dict[str, Any]] = []
    for item in compiled_nodes:
        raw_text = str(item.get("raw_text") or item.get("summary") or "").strip()
        role = str(item.get("derivation_role") or "claim")
        assessment = _source_grounding_assessment(source_text, raw_text, role=role)
        annotated = {
            **item,
            "source_grounding": {
                "status": "supported" if assessment["supported"] else "unsupported",
                **assessment,
            },
        }
        if assessment["supported"]:
            kept.append(annotated)
        else:
            filtered.append(annotated)
    if not filtered:
        return kept, None
    return kept, {
        "code": "source_grounding_filtered",
        "message": (
            f"Filtered {len(filtered)} compiler-derived node(s) because source_type={source_type or 'unknown'} "
            "requires derived memory to be supported by the source text."
        ),
        "filtered_count": len(filtered),
        "kept_count": len(kept),
        "filtered_examples": [
            summarize_text(str(item.get("raw_text") or item.get("summary") or ""), limit=120)
            for item in filtered[:3]
        ],
    }


def _filter_source_grounded_decisions(
    *,
    source_text: str,
    input_mode: str,
    source_type: str | None,
    decisions: list[dict[str, Any]],
    decision_kind: str,
) -> tuple[list[dict[str, Any]], dict[str, str] | None]:
    if not _source_grounding_requires_filter(source_type=source_type, input_mode=input_mode):
        return decisions, None
    kept: list[dict[str, Any]] = []
    filtered: list[dict[str, Any]] = []
    for decision in decisions:
        decision_text = str(decision.get("source_text") or "").strip()
        assessment = _source_grounding_assessment(source_text, decision_text, role="claim")
        if assessment["supported"]:
            kept.append(decision)
        else:
            filtered.append(decision)
    if not filtered:
        return kept, None
    return kept, {
        "code": f"source_grounding_{decision_kind}_decisions_filtered",
        "message": (
            f"Filtered {len(filtered)} compiler {decision_kind} decision(s) because source_type={source_type or 'unknown'} "
            "requires decision source_text to be supported by the source text."
        ),
    }


def _fold_identity_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_only = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", ascii_only.lower()).strip()


_PERSON_NAME_STOPWORDS = {
    "il",
    "lo",
    "la",
    "i",
    "gli",
    "le",
    "un",
    "una",
    "my",
    "mio",
    "mia",
    "partner",
    "mentor",
    "mentore",
    "fratello",
    "sorella",
    "sibling",
    "stata",
    "stato",
    "decisiva",
    "decisivo",
    "documento",
    "bootstrap",
    "ho",
    "sono",
    "per",
    "for",
    "in",
    "nel",
    "nella",
    "con",
    "di",
    "dentro",
    "inside",
    "and",
    "or",
    "of",
    "at",
    "contact",
    "profile",
    "profilo",
    "org",
    "terms",
    "head",
    "member",
    "source",
    "uri",
}

_PERSON_NAME_ABSTRACT_TOKENS = {
    "rigorous",
    "technical",
    "discussions",
    "delivery",
    "structured",
    "visione",
    "operativa",
    "critiche",
    "tecniche",
    "architecture",
    "quality",
    "product",
    "systems",
    "values",
    "valori",
    "reviews",
    "review",
    "context",
    "discussion",
    "delivery",
    "scale",
    "scaling",
    "roadmaps",
    "sprints",
    "patches",
    "stories",
    "narrative",
    "narratives",
    "chief",
    "executive",
    "officer",
    "ceo",
    "founder",
    "cofounder",
    "co-founder",
    "president",
    "fondatore",
    "fondatrice",
    "imprenditore",
}

_PROJECT_STOPWORDS = {
    "documento",
    "bootstrap",
    "profile",
    "profilo",
    "contact",
    "source",
}

_EMPLOYER_STOPWORDS = {
    "bergamo",
    "milano",
    "documento",
    "bootstrap",
    "lavoro",
    "ho",
    "il",
}


def _vector_stats(values: list[float]) -> tuple[float, float, float]:
    if not values:
        return 0.0, 0.0, 0.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return mean, variance**0.5, max(values) - min(values)


def _distribution_is_degenerate(score_map: dict[str, Any], fields: tuple[str, ...] | list[str]) -> bool:
    normalized = normalize_scores(dict(score_map or {}), fields)
    values = [float(normalized.get(field) or 0.0) for field in fields]
    _mean, stdev, spread = _vector_stats(values)
    top = max(values, default=0.0)
    return stdev <= 0.025 or spread <= 0.09 or top <= (1.0 / max(1, len(values))) + 0.06


def _blend_score_maps(
    primary: dict[str, Any],
    fallback: dict[str, Any],
    fields: tuple[str, ...] | list[str],
    *,
    fallback_weight: float,
) -> dict[str, float]:
    weight = max(0.0, min(1.0, float(fallback_weight)))
    blended = {
        field: (1.0 - weight) * float(dict(primary or {}).get(field) or 0.0) + weight * float(dict(fallback or {}).get(field) or 0.0)
        for field in fields
    }
    return normalize_scores(blended, fields)


def _named_sequence_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    for match in re.finditer(r"\b([A-Z][A-Za-zÀ-ÿ']+(?:\s+[A-Z][A-Za-zÀ-ÿ']+){1,2})\b", text):
        candidate = " ".join(part.capitalize() for part in match.group(1).strip().split())
        if candidate and _looks_like_person_name(candidate) and candidate not in candidates:
            candidates.append(candidate)
    return candidates[:8]


def _looks_like_person_name(value: str) -> bool:
    tokens = [token.strip(" .,:;()[]{}") for token in re.split(r"\s+", value.strip()) if token.strip(" .,:;()[]{}")]
    if len(tokens) < 2 or len(tokens) > 3:
        return False
    lowered = [token.lower() for token in tokens]
    if any(token in _PERSON_NAME_STOPWORDS for token in lowered):
        return False
    if any(token in _PERSON_NAME_ABSTRACT_TOKENS for token in lowered):
        return False
    if all(len(token) <= 3 for token in tokens):
        return False
    return all(token[:1].isupper() for token in tokens)


def _shares_person_name_token(candidate: str, known_names: list[str]) -> bool:
    candidate_tokens = {token.lower() for token in re.split(r"\s+", candidate.strip()) if token}
    for name in known_names:
        known_tokens = {token.lower() for token in re.split(r"\s+", str(name).strip()) if token}
        if candidate_tokens & known_tokens:
            return True
    return False


def _looks_like_named_concept(value: str) -> bool:
    tokens = [token for token in re.split(r"\s+", value.strip()) if token]
    if len(tokens) < 1 or len(tokens) > 4:
        return False
    lowered = [token.lower() for token in tokens]
    if any(token in _PERSON_NAME_STOPWORDS for token in lowered):
        return False
    return all(token[:1].isupper() or token[:1].isdigit() for token in tokens)


def _extract_role_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    safe_pattern = (
        r"(?:lavora come|lavoro come|works as|i work as)\s+"
        r"([\w'’ -]+?)(?=(?:\s+(?:e|and)\s+(?:guida|builds?|is building|is constructing|sta costruendo|dentro|inside))|[.,;]|$)"
    )
    for match in re.finditer(safe_pattern, text, flags=re.IGNORECASE):
        candidate = " ".join(part.strip() for part in match.group(1).split()).strip(" .,:;")
        if candidate and candidate.lower() not in {item.lower() for item in candidates}:
            candidates.append(candidate)
    return candidates[:6]
    pattern = (
        r"(?:lavora come|lavoro come|works as|i work as)\s+"
        r"([A-Za-zÃ€-Ã¿0-9'â€™ -]+?)(?=(?:\s+(?:e|and)\s+(?:guida|builds?|is building|is constructing|sta costruendo|dentro|inside))|[.,;]|$)"
    )
    for match in re.finditer(pattern, text, flags=re.IGNORECASE):
        candidate = " ".join(part.strip() for part in match.group(1).split()).strip(" .,:;")
        if candidate and candidate.lower() not in {item.lower() for item in candidates}:
            candidates.append(candidate)
    return candidates[:6]


def _extract_project_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    patterns = (
        r"(?:guida|guido|guides|guide|leads|lead|sta costruendo|sto costruendo|is building|is constructing|works on|lavora su)\s+([A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+){0,2})",
        r"\b([A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+){0,2})\s+(?:is being built|is being constructed|Ã¨ in costruzione)\b",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            candidate = " ".join(part.strip() for part in match.group(1).split())
            if not candidate or candidate.lower() in _PROJECT_STOPWORDS:
                continue
            if not _looks_like_named_concept(candidate):
                continue
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates[:10]


def _extract_employer_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    pattern = r"(?:dentro|inside|within|all'interno di|all interno di|at|for)\s+([A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+){0,3})"
    for match in re.finditer(pattern, text):
        candidate = " ".join(part.strip() for part in match.group(1).split())
        tokens = [token for token in candidate.split() if token]
        if not candidate or candidate.lower() in _EMPLOYER_STOPWORDS:
            continue
        if len(tokens) < 2 or not _looks_like_named_concept(candidate):
            continue
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates[:6]


def map_runtime_memory_type(memory_type: str | None, *, claim_type: str | None = None, entity_type: str | None = None) -> str:
    if claim_type and claim_type in CLAIM_MEMORY_TYPES:
        return CLAIM_MEMORY_TYPES[claim_type]
    if entity_type and entity_type in ENTITY_MEMORY_TYPES:
        return ENTITY_MEMORY_TYPES[entity_type]
    normalized = str(memory_type or "").strip().lower().replace(" ", "_")
    if normalized in {"identity_claim", "value_claim", "style_claim", "project_claim", "relationship_claim", "event_claim"}:
        return CLAIM_MEMORY_TYPES.get(normalized, "knowledge")
    if normalized in {"person", "organization", "project", "document"}:
        return ENTITY_MEMORY_TYPES.get(normalized, "knowledge")
    return normalized or "knowledge"


def extract_name_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    patterns = [
        r"(?:mi chiamo|my name is)\s+([A-Za-zÀ-ÿ' ]+?)(?:[.,;]|$|\s+nato|\s+born)",
        r"(?:sono|i am)\s+(?!nato\b|nata\b|born\b)([A-Za-zÀ-ÿ' ]+?)(?:[.,;]|$|\s+nato|\s+born)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            candidate = " ".join(part.capitalize() for part in match.group(1).strip().split())
            if candidate and _looks_like_person_name(candidate) and candidate not in candidates:
                candidates.append(candidate)
    return candidates[:6]


def _extract_identity_subject_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    role_terms = (
        "Chief Executive Officer",
        "Founder and CEO",
        "Founder",
        "Co-Founder",
        "CEO",
        "President",
        "founder",
        "cofounder",
        "co-founder",
        "founder and ceo",
        "ceo",
        "chief executive officer",
        "president",
        "imprenditore",
        "fondatore",
        "fondatrice",
        "amministratore delegato",
    )
    role_pattern = "|".join(re.escape(term) for term in sorted(role_terms, key=len, reverse=True))
    person_pattern = r"([A-Z][\w'â€™-]+(?:\s+[A-Z][\w'â€™-]+){1,2})"
    patterns = [
        rf"\b{person_pattern}\s+(?:is|was|Ã¨|e)\s+(?:a|an|il|la|un|una|the)?\s*(?:{role_pattern})\b",
        rf"\b(?:{role_pattern})\s+(?:is|was|Ã¨|e|:|-)?\s*{person_pattern}\b",
        rf"\b{person_pattern}\s+[-|]\s+(?:chief executive officer|ceo|founder|fondatore|fondatrice)\b",
        rf"\b(?:contact|profile|profilo)\s+{person_pattern}\b",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            parts = [part.strip(" .,:;|-") for part in match.group(1).strip().split() if part.strip(" .,:;|-")]
            while parts and parts[0].lower() in {
                "at",
                "for",
                "with",
                "in",
                "inside",
                "within",
                "contact",
                "profile",
                "profilo",
                "the",
                "org",
            }:
                parts = parts[1:]
            candidate = " ".join(part.capitalize() for part in parts).strip(" .,:;|-")
            if candidate and _looks_like_person_name(candidate) and candidate not in candidates:
                candidates.append(candidate)
    return candidates[:8]


def _extract_relation_candidates(text: str, relation_keywords: tuple[str, ...]) -> list[str]:
    candidates: list[str] = []
    lowered = text.lower()
    if not any(keyword in lowered for keyword in relation_keywords):
        return candidates
    patterns = [
        rf"([A-Z][A-Za-zÀ-ÿ']+(?:\s+[A-Z][A-Za-zÀ-ÿ']+){{1,2}})\s+(?:è|is|was)(?:\s+(?:il|la|my|mio|mia))?\s+(?:{'|'.join(re.escape(keyword) for keyword in relation_keywords)})",
        rf"(?:{'|'.join(re.escape(keyword) for keyword in relation_keywords)}).{{0,32}}?(?:si chiama|is|è|named)\s+([A-Z][A-Za-zÀ-ÿ']+(?:\s+[A-Z][A-Za-zÀ-ÿ']+){{1,2}})(?:[.,;]|$)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            candidate = " ".join(part.capitalize() for part in match.group(1).strip().split())
            if candidate and _looks_like_person_name(candidate) and candidate not in candidates:
                candidates.append(candidate)
    return candidates[:6]


def _extract_relation_candidates_strict(text: str, relation_keywords: tuple[str, ...]) -> list[str]:
    candidates = list(_extract_relation_candidates(text, relation_keywords))
    lowered = text.lower()
    folded = _fold_identity_text(text)
    if not any(keyword in lowered for keyword in relation_keywords):
        if not any(_fold_identity_text(keyword) in folded for keyword in relation_keywords):
            return candidates
    literal_keywords = "|".join(re.escape(keyword) for keyword in relation_keywords)
    folded_keywords = "|".join(re.escape(_fold_identity_text(keyword)) for keyword in relation_keywords)
    patterns = [
        rf"([A-Z][A-Za-zÀ-ÿ']+(?:\s+[A-Z][A-Za-zÀ-ÿ']+){{1,2}})\s+(?:è|e|is|was)(?:\s+(?:il|la|my|mio|mia))?\s+(?:{literal_keywords})",
        rf"(?:{literal_keywords}).{{0,40}}?(?:si chiama|is|è|e|named)\s+([A-Z][A-Za-zÀ-ÿ']+(?:\s+[A-Z][A-Za-zÀ-ÿ']+){{1,2}})(?:[.,;]|$)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            candidate = " ".join(part.capitalize() for part in match.group(1).strip().split())
            if candidate and _looks_like_person_name(candidate) and candidate not in candidates:
                candidates.append(candidate)
    folded_patterns = [
        rf"([A-Z][A-Za-zÀ-ÿ']+(?:\s+[A-Z][A-Za-zÀ-ÿ']+){{1,2}})\s+(?:e|is|was)(?:\s+(?:il|la|my|mio|mia))?\s+(?:{folded_keywords})",
        rf"(?:{folded_keywords}).{{0,40}}?(?:si chiama|is|e|named)\s+([A-Z][A-Za-zÀ-ÿ']+(?:\s+[A-Z][A-Za-zÀ-ÿ']+){{1,2}})(?:[.,;]|$)",
    ]
    for pattern in folded_patterns:
        for match in re.finditer(pattern, folded, flags=re.IGNORECASE):
            candidate = " ".join(part.capitalize() for part in match.group(1).strip().split())
            if candidate and _looks_like_person_name(candidate) and candidate not in candidates:
                candidates.append(candidate)
    return candidates[:6]


def _extract_relation_candidates_strict_v2(text: str, relation_keywords: tuple[str, ...]) -> list[str]:
    candidates = list(_extract_relation_candidates_strict(text, relation_keywords))
    folded = _fold_identity_text(text)
    folded_keywords = "|".join(re.escape(_fold_identity_text(keyword)) for keyword in relation_keywords)
    lowered_patterns = [
        rf"(?:^|[.;,\n]\s*)([a-z][a-z' -]+(?:\s+[a-z][a-z' -]+){{1,2}})\s+(?:e|is|was)(?:\s+(?:il|la|my|mio|mia|un|una))?\s+(?:{folded_keywords})\b",
        rf"(?:^|[.;,\n]\s*)(?:{folded_keywords})(?:\s+(?:is|e|è|named|si chiama))?\s+([a-z][a-z' -]+(?:\s+[a-z][a-z' -]+){{1,2}})(?:[.,;]|$)",
    ]
    for pattern in lowered_patterns:
        for match in re.finditer(pattern, folded, flags=re.IGNORECASE):
            candidate = " ".join(part.capitalize() for part in match.group(1).strip().split()).strip(" .,:;")
            if candidate and _looks_like_person_name(candidate) and candidate not in candidates:
                candidates.append(candidate)
    return candidates[:6]


def _extract_relation_candidates_clausewise(text: str, relation_keywords: tuple[str, ...]) -> list[str]:
    candidates: list[str] = []
    literal_keywords = "|".join(re.escape(keyword) for keyword in relation_keywords)
    folded_keywords = "|".join(re.escape(_fold_identity_text(keyword)) for keyword in relation_keywords)
    clauses = [part.strip() for part in re.split(r"[.,;\n]", str(text or "")) if part.strip()]
    for clause in clauses:
        folded_clause = _fold_identity_text(clause)
        if not re.search(rf"\b(?:{folded_keywords})\b", folded_clause, flags=re.IGNORECASE):
            continue
        clause_candidates = [
            " ".join(part.capitalize() for part in match.group(1).strip().split()).strip(" .,:;")
            for match in re.finditer(
                rf"([A-Z][A-Za-zÀ-ÿ'’-]+(?:\s+[A-Z][A-Za-zÀ-ÿ'’-]+){{1,2}})\s+(?:è|is|was)(?:\s+(?:il|la|my|mio|mia|un|una))?\s+(?:{literal_keywords})\b",
                clause,
                flags=re.IGNORECASE,
            )
        ]
        clause_candidates.extend(
            " ".join(part.capitalize() for part in match.group(1).strip().split()).strip(" .,:;")
            for match in re.finditer(
                rf"(?:{literal_keywords})(?:\s+(?:is|è|e|named|si chiama))?\s+([A-Z][A-Za-zÀ-ÿ'’-]+(?:\s+[A-Z][A-Za-zÀ-ÿ'’-]+){{1,2}})(?:[.,;]|$)",
                clause,
                flags=re.IGNORECASE,
            )
        )
        unique_names: list[str] = []
        for name in clause_candidates:
            if _looks_like_person_name(name) and name not in unique_names:
                unique_names.append(name)
        for name in unique_names[:3]:
            if name not in candidates:
                candidates.append(name)
    return candidates[:6]


def _extract_alias_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    patterns = (
        r"(?:known as|conosciut[oa] anche come|appare anche come|appare come|called)\s+([A-Za-zÀ-ÿ' ]+?)(?:[.,;]|$)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            candidate = " ".join(part.capitalize() for part in match.group(1).strip().split())
            if candidate and candidate not in candidates:
                candidates.append(candidate)
    return candidates[:6]


def build_identity_nucleus(graph: dict[str, Any]) -> dict[str, Any]:
    nodes = list(graph.get("nodes") or [])
    identity_nodes = [
        node
        for node in nodes
        if str(node.get("memory_type") or "") == "identity"
    ]
    core_context_nodes = [
        node
        for node in nodes
        if str(node.get("memory_type") or "") in {"identity", "value", "identity_style", "relational", "project", "document_anchor", "document_fact", "document_summary", "knowledge", "episodic"}
    ]
    document_anchor_nodes = [
        node
        for node in nodes
        if str(node.get("memory_type") or "") == "document_anchor" or bool(node.get("is_document_anchor"))
    ]
    relation_cue_nodes = [
        node
        for node in nodes
        if any(
            cue in _fold_identity_text(f"{node.get('raw_text') or ''} {node.get('summary') or ''}")
            for cue in (
                "partner",
                "mentor",
                "mentore",
                "fratello",
                "sorella",
                "brother",
                "sister",
                "sibling",
                "ceo",
                "chief executive officer",
                "founder",
                "cofounder",
                "co-founder",
                "fondatore",
                "fondatrice",
                "amministratore delegato",
                "imprenditore",
            )
        )
    ]
    identity_nodes.sort(
        key=lambda node: (
            -float(node.get("memory_confidence") or node.get("derivation_confidence") or 0.0),
            -float((node.get("routing_facets") or {}).get("identity_centrality", 0.0)),
            str(node.get("id") or ""),
        )
    )
    core_context_nodes.sort(
        key=lambda node: (
            -float(node.get("memory_confidence") or node.get("derivation_confidence") or 0.0),
            -float((node.get("routing_facets") or {}).get("identity_centrality", 0.0)),
            str(node.get("id") or ""),
        )
    )
    document_anchor_nodes.sort(
        key=lambda node: (
            -float(node.get("memory_confidence") or node.get("derivation_confidence") or 0.0),
            str(node.get("id") or ""),
        )
    )
    analysis_nodes: list[dict[str, Any]] = []
    seen_analysis_ids: set[str] = set()
    for node in [*core_context_nodes[:80], *document_anchor_nodes[:16], *relation_cue_nodes[:48]]:
        node_id = str(node.get("id") or "")
        if not node_id or node_id in seen_analysis_ids:
            continue
        seen_analysis_ids.add(node_id)
        analysis_nodes.append(node)
    self_name_candidates: list[str] = []
    self_name_scores: dict[str, float] = {}
    aliases: list[str] = []
    partner_candidates: list[str] = []
    mentor_candidates: list[str] = []
    sibling_candidates: list[str] = []
    role_candidates: list[str] = []
    self_support_node_ids: list[str] = []
    partner_support_node_ids: list[str] = []
    mentor_support_node_ids: list[str] = []
    sibling_support_node_ids: list[str] = []
    role_support_node_ids: list[str] = []
    project_support_node_ids: list[str] = []
    employer_support_node_ids: list[str] = []
    style_support_node_ids: list[str] = []
    value_support_node_ids: list[str] = []
    employer_scores: dict[str, float] = {}

    def register_self_name(candidate: str, *, score: float, node_id: str | None = None) -> None:
        normalized = " ".join(part.capitalize() for part in str(candidate or "").strip().split())
        if not normalized or not _looks_like_person_name(normalized):
            return
        if normalized not in self_name_candidates:
            self_name_candidates.append(normalized)
        self_name_scores[normalized] = float(self_name_scores.get(normalized) or 0.0) + max(0.0, float(score))
        if node_id and node_id not in self_support_node_ids:
            self_support_node_ids.append(node_id)
    project_scores: dict[str, float] = {}
    for node in analysis_nodes:
        node_id = str(node.get("id") or "")
        node_confidence = float(node.get("memory_confidence") or node.get("derivation_confidence") or 0.6)
        raw_text = str(node.get("raw_text") or "")
        provenance = dict(node.get("provenance") or {})
        provenance_text = " ".join(
            str(provenance.get(key) or "").strip()
            for key in ("source_label", "source_type", "source_unit_title")
            if str(provenance.get(key) or "").strip()
        )
        combined_text = " ".join(part for part in (raw_text, str(node.get("summary") or ""), provenance_text) if part).strip()
        explicit_self_names = extract_name_candidates(raw_text)
        for candidate in explicit_self_names:
            register_self_name(candidate, score=node_confidence + 0.8, node_id=node_id)
        for candidate in _extract_identity_subject_candidates(combined_text):
            register_self_name(candidate, score=node_confidence + 0.55, node_id=node_id)
        if str(node.get("memory_type") or "") == "identity":
            known_self_names = explicit_self_names or list(self_name_candidates)
            for candidate in _named_sequence_candidates(f"{node.get('raw_text') or ''} {node.get('summary') or ''}"):
                if known_self_names and not _shares_person_name_token(candidate, known_self_names):
                    continue
                register_self_name(candidate, score=node_confidence + 0.3, node_id=node_id)
        for alias in _extract_alias_candidates(raw_text):
            if alias not in aliases and alias not in self_name_candidates:
                aliases.append(alias)
        lowered_text = combined_text.lower()
        guide_area = str((node.get("provenance") or {}).get("guide_conceptual_area") or "")
        memory_type = str(node.get("memory_type") or "")
        if node_id and any(token in lowered_text for token in ("partner", "fidanzat", "girlfriend", "boyfriend", "wife", "husband")):
            if node_id not in partner_support_node_ids:
                partner_support_node_ids.append(node_id)
        if node_id and "mentor" in lowered_text:
            if node_id not in mentor_support_node_ids:
                mentor_support_node_ids.append(node_id)
        if node_id and any(token in lowered_text for token in ("fratello", "sorella", "brother", "sister", "sibling")):
            if node_id not in sibling_support_node_ids:
                sibling_support_node_ids.append(node_id)
        if node_id and (
            memory_type == "identity_style"
            or guide_area == "Expression"
            or (
                any(token in lowered_text for token in ("comunica", "parla", "scrive", "si esprime", "stile", "tone", "voice", "spiega"))
                and any(token in lowered_text for token in ("dirett", "tecnic", "strutturat", "lucid", "chiar", "concis", "essenzial", "analitic", "ridond"))
            )
        ):
            if node_id not in style_support_node_ids:
                style_support_node_ids.append(node_id)
        if node_id and (
            memory_type == "value"
            or guide_area == "Values"
            or any(token in lowered_text for token in ("valori", "values", "principi", "precisione", "chiarezza", "rigore", "qualit", "responsabil", "coerenza architetturale"))
        ):
            if node_id not in value_support_node_ids:
                value_support_node_ids.append(node_id)
        for match in re.finditer(r"([A-Z][\w'’-]+(?:\s+[A-Z][\w'’-]+){1,2})\s+(?:è|is|was)(?:\s+(?:il|la))?(?:\s+(?:my|mio|mia))?\s+(?:partner|fidanzat[oa]|girlfriend|boyfriend|wife|husband)\b", combined_text, flags=re.IGNORECASE):
            candidate = " ".join(part.capitalize() for part in match.group(1).strip().split())
            if _looks_like_person_name(candidate) and candidate not in partner_candidates:
                partner_candidates.append(candidate)
        for match in re.finditer(r"([A-Z][\w'’-]+(?:\s+[A-Z][\w'’-]+){1,2})\s+(?:è stata|è stato|è|was|is)(?:\s+(?:una|un|my|mia|mio))?\s+mentor(?:e)?\b", combined_text, flags=re.IGNORECASE):
            candidate = " ".join(part.capitalize() for part in match.group(1).strip().split())
            if _looks_like_person_name(candidate) and candidate not in mentor_candidates:
                mentor_candidates.append(candidate)
        for match in re.finditer(r"([A-Z][\w'’-]+(?:\s+[A-Z][\w'’-]+){1,2})\s+(?:è|is|was)(?:\s+(?:il|la))?(?:\s+(?:mio|mia|my))?\s+(?:fratello|sorella|brother|sister|sibling)\b", combined_text, flags=re.IGNORECASE):
            candidate = " ".join(part.capitalize() for part in match.group(1).strip().split())
            if _looks_like_person_name(candidate) and candidate not in sibling_candidates:
                sibling_candidates.append(candidate)
        for candidate in [*_extract_relation_candidates_clausewise(combined_text, ("partner", "fidanzata", "fidanzato", "girlfriend", "boyfriend", "wife", "husband")), *_extract_relation_candidates_strict_v2(combined_text, ("partner", "fidanzata", "fidanzato", "girlfriend", "boyfriend", "wife", "husband"))]:
            if candidate not in partner_candidates:
                partner_candidates.append(candidate)
            if node_id and node_id not in partner_support_node_ids:
                partner_support_node_ids.append(node_id)
        for candidate in [*_extract_relation_candidates_clausewise(combined_text, ("mentor", "mentore")), *_extract_relation_candidates_strict_v2(combined_text, ("mentor", "mentore"))]:
            if candidate not in mentor_candidates:
                mentor_candidates.append(candidate)
            if node_id and node_id not in mentor_support_node_ids:
                mentor_support_node_ids.append(node_id)
        for candidate in [*_extract_relation_candidates_clausewise(combined_text, ("brother", "sister", "fratello", "sorella", "sibling")), *_extract_relation_candidates_strict_v2(combined_text, ("brother", "sister", "fratello", "sorella", "sibling"))]:
            if candidate not in sibling_candidates:
                sibling_candidates.append(candidate)
            if node_id and node_id not in sibling_support_node_ids:
                sibling_support_node_ids.append(node_id)
        for candidate in _extract_role_candidates(combined_text):
            if candidate and candidate not in role_candidates:
                role_candidates.append(candidate)
            if node_id and node_id not in role_support_node_ids:
                role_support_node_ids.append(node_id)
        for candidate in _extract_project_candidates(combined_text):
            if candidate not in self_name_scores:
                project_scores[candidate] = float(project_scores.get(candidate) or 0.0) + node_confidence
                if node_id and node_id not in project_support_node_ids:
                    project_support_node_ids.append(node_id)
        if str(node.get("memory_type") or "") == "project":
            project_name = " ".join(part for part in str(node.get("raw_text") or node.get("summary") or "").strip().split())
            if project_name and _looks_like_named_concept(project_name) and project_name not in self_name_scores:
                project_scores[project_name] = float(project_scores.get(project_name) or 0.0) + node_confidence + 0.6
                if node_id and node_id not in project_support_node_ids:
                    project_support_node_ids.append(node_id)
        for candidate in _extract_employer_candidates(combined_text):
            if candidate not in self_name_scores:
                employer_scores[candidate] = float(employer_scores.get(candidate) or 0.0) + node_confidence
                if node_id and node_id not in employer_support_node_ids:
                    employer_support_node_ids.append(node_id)
    normalized_role_candidates: list[str] = []
    for candidate in role_candidates:
        cleaned = re.split(r"\s+(?:e|and)\s+(?:guida|builds?|is building|is constructing|sta costruendo|dentro|inside)\b", candidate, maxsplit=1)[0].strip(" .,:;")
        if cleaned and not any(
            cleaned.lower() == item.lower() or cleaned.lower().startswith(f"{item.lower()} ")
            for item in normalized_role_candidates
        ):
            normalized_role_candidates.append(cleaned)
    role_candidates = normalized_role_candidates[:6]

    ordered_self_names = [
        candidate
        for candidate, _score in sorted(self_name_scores.items(), key=lambda item: (-item[1], item[0]))
        if _looks_like_person_name(candidate)
    ]
    if ordered_self_names:
        anchor_tokens = {token.lower() for token in ordered_self_names[0].split() if token}
        self_name_candidates = [
            candidate
            for candidate in ordered_self_names
            if {token.lower() for token in candidate.split() if token} & anchor_tokens
        ][:6]

    project_candidates = [
        candidate
        for candidate, _score in sorted(project_scores.items(), key=lambda item: (-item[1], item[0]))
        if candidate not in self_name_scores and candidate.lower() not in _PROJECT_STOPWORDS
    ][:10]
    employer_candidates = [
        candidate
        for candidate, _score in sorted(employer_scores.items(), key=lambda item: (-item[1], item[0]))
        if candidate not in self_name_scores and candidate not in project_candidates and candidate.lower() not in _EMPLOYER_STOPWORDS
    ][:6]
    aliases = [
        alias
        for alias in aliases
        if 1 <= len(alias.split()) <= 2 and alias.lower() not in {name.lower() for name in self_name_candidates}
    ][:10]
    core_nodes_source: list[dict[str, Any]] = []
    seen_core_node_ids: set[str] = set()
    for node in [*identity_nodes[:4], *document_anchor_nodes[:4], *core_context_nodes[:10]]:
        node_id = str(node.get("id") or "")
        if not node_id or node_id in seen_core_node_ids:
            continue
        seen_core_node_ids.add(node_id)
        core_nodes_source.append(node)
    primary_self_node_id = str(identity_nodes[0].get("id")) if identity_nodes else (str(core_context_nodes[0].get("id")) if core_context_nodes else None)
    core_name = self_name_candidates[0] if self_name_candidates else None
    return {
        "core_name": core_name,
        "primary_self_node_id": primary_self_node_id,
        "self_name_candidates": self_name_candidates[:6],
        "aliases": aliases[:10],
        "partner_candidates": partner_candidates[:6],
        "mentor_candidates": mentor_candidates[:6],
        "sibling_candidates": sibling_candidates[:6],
        "role_candidates": role_candidates[:6],
        "employer_candidates": employer_candidates[:6],
        "project_candidates": project_candidates[:10],
        "self_support_node_ids": self_support_node_ids[:16],
        "partner_support_node_ids": partner_support_node_ids[:16],
        "mentor_support_node_ids": mentor_support_node_ids[:16],
        "sibling_support_node_ids": sibling_support_node_ids[:16],
        "role_support_node_ids": role_support_node_ids[:16],
        "project_support_node_ids": project_support_node_ids[:16],
        "employer_support_node_ids": employer_support_node_ids[:16],
        "style_support_node_ids": style_support_node_ids[:24],
        "value_support_node_ids": value_support_node_ids[:24],
        "core_nodes": [
            {
                "node_id": str(node.get("id")),
                "summary": str(node.get("summary") or ""),
                "memory_type": str(node.get("memory_type") or ""),
                "confidence": float(node.get("memory_confidence") or node.get("derivation_confidence") or 0.0),
                "guide_area": str((node.get("provenance") or {}).get("guide_conceptual_area") or ""),
                "final_position": dict(node.get("final_position") or {}),
            }
            for node in core_nodes_source[:12]
        ],
    }


def _infer_radial_band(memory_type: str, guide_area: str | None, *, input_mode: str) -> str:
    normalized_memory_type = str(memory_type or "").strip().lower()
    normalized_guide_area = str(guide_area or "").strip().lower()
    if normalized_memory_type in {"identity", "value", "identity_style"} or normalized_guide_area in {"identity", "values", "meta"}:
        return "core"
    if normalized_memory_type in {"project", "technical", "operational"} or normalized_guide_area in {"projects", "systems", "operations"}:
        return "inner"
    if normalized_memory_type in {"episodic", "document_anchor"} or input_mode == "document" or normalized_guide_area in {"episodic", "documents", "media signals"}:
        return "outer"
    return "mid"


def _stabilize_compiled_semantics(
    *,
    raw_text: str,
    input_mode: str,
    summary: str,
    memory_type: str | None,
    guide_area: str | None,
    routing_scores: dict[str, Any],
    routing_facets: dict[str, Any],
    granularity: float | None,
    novelty: float | None,
    trust_compiled: bool = False,
) -> dict[str, Any]:
    if trust_compiled:
        try:
            trusted_scores = {
                field: max(0.0, min(1.0, float(routing_scores[field])))
                for field in ROUTING_FIELDS
            }
            trusted_facets = {
                field: max(0.0, min(1.0, float(routing_facets[field])))
                for field in FACET_FIELDS
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("grow_ai_semantic_vector_invalid") from exc
        return {
            "routing_semantic_scores": trusted_scores,
            "routing_facets": trusted_facets,
            "memory_type": map_runtime_memory_type(memory_type),
            "guide_area": guide_area or infer_guide_area(raw_text or summary),
            "granularity": max(0.1, min(1.0, float(granularity if granularity is not None else 0.5))),
            "novelty": max(0.0, min(1.0, float(novelty if novelty is not None else 0.5))),
            "heuristic_projection": None,
        }
    heuristic = heuristic_projection(raw_text or summary, input_mode=input_mode)
    score_weight = 0.78 if _distribution_is_degenerate(routing_scores, ROUTING_FIELDS) else 0.28
    facet_weight = 0.72 if _distribution_is_degenerate(routing_facets, FACET_FIELDS) else 0.22
    stabilized_scores = _blend_score_maps(routing_scores, heuristic["routing_semantic_scores"], ROUTING_FIELDS, fallback_weight=score_weight)
    stabilized_facets = _blend_score_maps(routing_facets, heuristic["routing_facets"], FACET_FIELDS, fallback_weight=facet_weight)
    runtime_memory_type = map_runtime_memory_type(memory_type)
    heuristic_memory_type = map_runtime_memory_type(heuristic.get("memory_type"))
    specificity = {
        "identity": 0.62,
        "knowledge": 0.30,
        "technical": 0.44,
        "operational": 0.44,
        "project": 0.58,
        "relational": 0.60,
        "identity_style": 0.64,
        "value": 0.66,
        "episodic": 0.56,
        "document_anchor": 0.48,
        "emotional": 0.42,
    }
    if specificity.get(heuristic_memory_type, 0.0) > specificity.get(runtime_memory_type, 0.0):
        runtime_memory_type = heuristic_memory_type
    stabilized_guide_area = guide_area or heuristic["expected_guide_area"]
    if not stabilized_guide_area:
        if runtime_memory_type == "project":
            stabilized_guide_area = "Projects"
        elif runtime_memory_type == "relational":
            stabilized_guide_area = "Relationships"
        elif runtime_memory_type == "identity_style":
            stabilized_guide_area = "Expression"
        elif runtime_memory_type == "value":
            stabilized_guide_area = "Values"
        elif runtime_memory_type == "episodic":
            stabilized_guide_area = "History"
        elif runtime_memory_type == "document_anchor":
            stabilized_guide_area = "Media Signals"
        elif runtime_memory_type in {"technical", "operational"}:
            stabilized_guide_area = "Operational"
        elif runtime_memory_type == "identity":
            stabilized_guide_area = "Identity"
    stabilized_granularity = float(granularity if granularity is not None else heuristic["granularity"])
    stabilized_novelty = float(novelty if novelty is not None else heuristic["novelty"])
    return {
        "routing_semantic_scores": stabilized_scores,
        "routing_facets": stabilized_facets,
        "memory_type": runtime_memory_type,
        "guide_area": stabilized_guide_area,
        "granularity": max(0.1, min(1.0, stabilized_granularity)),
        "novelty": max(0.0, min(1.0, stabilized_novelty)),
        "heuristic_projection": heuristic,
    }


def normalize_runtime_graph(graph: dict[str, Any]) -> dict[str, Any]:
    nodes = list(graph.get("nodes") or [])
    if not nodes:
        return dict(graph)
    ordered_nodes = sorted(
        nodes,
        key=lambda node: (
            0 if bool(node.get("is_document_anchor")) else 1,
            -float(node.get("memory_confidence") or node.get("derivation_confidence") or 0.0),
            str(node.get("id") or ""),
        ),
    )
    working_graph = {
        "version": graph.get("version"),
        "graph_name": graph.get("graph_name"),
        "nodes": [],
        "edges": list(graph.get("edges") or []),
        "meta": dict(graph.get("meta") or {}),
    }
    working_index = build_index([])
    rebuilt_nodes: list[dict[str, Any]] = []
    for node in ordered_nodes:
        raw_text = str(node.get("raw_text") or node.get("summary") or "").strip()
        if not raw_text:
            continue
        input_mode = "document" if bool(node.get("is_document_anchor")) or str((node.get("provenance") or {}).get("source_type") or "") == "document" else "auto"
        semantics = _stabilize_compiled_semantics(
            raw_text=raw_text,
            input_mode=input_mode,
            summary=str(node.get("summary") or summarize_text(raw_text, limit=120)),
            memory_type=str(node.get("memory_type") or ""),
            guide_area=str((node.get("provenance") or {}).get("guide_conceptual_area") or "") or None,
            routing_scores=dict(node.get("routing_semantic_scores") or {}),
            routing_facets=dict(node.get("routing_facets") or {}),
            granularity=float(node.get("granularity") or 0.5),
            novelty=float(node.get("novelty") or 0.5),
        )
        local_correction_plan = _default_local_correction_plan(
            raw_text=raw_text,
            input_mode=input_mode,
            nearby_context={"nearby_nodes": []},
            memory_type=str(semantics["memory_type"] or ""),
            guide_area=semantics.get("guide_area"),
            existing_plan=dict(node.get("local_correction_plan") or {}),
        )
        seed = build_seed(
            raw_text=raw_text,
            input_mode=input_mode,
            provenance_mode=str((node.get("provenance") or {}).get("mode") or "runtime_normalized"),
            source_label=(node.get("provenance") or {}).get("source_label"),
            source_type=(node.get("provenance") or {}).get("source_type"),
            source_trust=node.get("source_trust"),
            claim_status=node.get("claim_status"),
            node_kind_hint=str(node.get("node_kind") or ""),
            summary_override=str(node.get("summary") or summarize_text(raw_text, limit=120)),
            memory_type_override=str(semantics["memory_type"] or ""),
            guide_area_override=str(semantics.get("guide_area") or ""),
            derivation_role=node.get("derivation_role"),
            derivation_confidence=node.get("derivation_confidence"),
            derived_from_preview_id=node.get("derived_from_preview_id"),
            source_span_start=node.get("source_span_start"),
            source_span_end=node.get("source_span_end"),
            routing_scores_override=dict(semantics["routing_semantic_scores"]),
            routing_facets_override=dict(semantics["routing_facets"]),
            granularity_override=float(semantics["granularity"]),
            novelty_override=float(semantics["novelty"]),
            memory_confidence=node.get("memory_confidence"),
            identity_resolution_confidence=node.get("identity_resolution_confidence"),
            evidence_confidence=node.get("evidence_confidence"),
            stability_confidence=node.get("stability_confidence"),
            local_correction_plan=local_correction_plan,
            suggested_links=list(node.get("links") or []),
            suggested_highways=list(node.get("highways") or []),
        )
        rebuilt = finalize_node(seed, working_graph, working_index, fixed_id=str(node.get("id") or ""))
        rebuilt["temporal_role"] = node.get("temporal_role")
        rebuilt["valid_from"] = node.get("valid_from")
        rebuilt["valid_to"] = node.get("valid_to")
        rebuilt["observed_at"] = node.get("observed_at")
        rebuilt["superseded_by"] = node.get("superseded_by")
        rebuilt["obsoletes"] = list(node.get("obsoletes") or [])
        rebuilt["temporal_confidence"] = node.get("temporal_confidence")
        rebuilt["lifecycle_status"] = node.get("lifecycle_status") or "active"
        rebuilt_nodes.append(rebuilt)
        working_graph["nodes"].append(rebuilt)
        working_index = build_index(list(working_graph.get("nodes") or []))
    rebalanced_nodes = rebalance_graph_geometry(rebuilt_nodes)
    return {
        **graph,
        "nodes": rebalanced_nodes,
        "meta": {
            **dict(graph.get("meta") or {}),
            "normalized_runtime_semantics": True,
            "normalized_runtime_geometry": True,
            "node_count": len(rebalanced_nodes),
        },
    }


def _normalize_persist_mode(value: Any) -> str:
    mode = str(value or "create").strip()
    if mode == "new_node":
        return "create"
    if mode in {"create", "merge_into_existing", "attach_as_alias_or_variant"}:
        return mode
    return "create"


def _summarize_write_merge_decisions(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    decision_counts: dict[str, int] = defaultdict(int)
    for decision in decisions:
        decision_counts[_normalize_persist_mode(decision.get("decision"))] += 1
    return {
        "total": len(decisions),
        "create_count": int(decision_counts.get("create") or 0),
        "merge_into_existing_count": int(decision_counts.get("merge_into_existing") or 0),
        "attach_as_alias_or_variant_count": int(decision_counts.get("attach_as_alias_or_variant") or 0),
    }


def _summarize_write_identity_decisions(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    resolution_counts: dict[str, int] = defaultdict(int)
    resolved_count = 0
    for decision in decisions:
        resolution_type = str(decision.get("resolution_type") or "unresolved")
        resolution_counts[resolution_type] += 1
        if decision.get("resolved_node_id"):
            resolved_count += 1
    return {
        "total": len(decisions),
        "resolved_count": resolved_count,
        "resolution_type_counts": dict(resolution_counts),
    }


def _unique_strings(values: list[Any], *, limit: int = 12) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = " ".join(str(value or "").strip().split())
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
        if len(items) >= limit:
            break
    return items


def _cognitive_fold(value: Any) -> str:
    folded = _fold_identity_text(str(value or ""))
    return re.sub(r"[^\w\s:/.-]", " ", folded).strip()


def _has_any_cue(folded_text: str, cues: tuple[str, ...]) -> bool:
    return any(cue in folded_text for cue in cues)


_COGNITIVE_MEMORY_ACT_TYPES = {
    "create_new_fact",
    "update_existing_fact",
    "create_event",
    "update_relationship_state",
    "create_project_state",
    "attach_document",
    "create_deduction",
    "create_hypothesis",
    "create_future_intention",
    "create_dream_aspiration",
    "mark_contradiction",
    "supersede_old_memory",
    "demote_low_confidence_memory",
    "ask_clarification",
    "no_op_duplicate",
}

_RELATIONSHIP_CUES = (
    "padre",
    "madre",
    "figlio",
    "figlia",
    "fratello",
    "sorella",
    "family",
    "father",
    "mother",
    "son",
    "daughter",
    "brother",
    "sister",
    "partner",
    "fidanzata",
    "fidanzato",
    "moglie",
    "marito",
    "wife",
    "husband",
    "mentor",
    "mentore",
    "collaboratore",
    "collaborator",
    "team",
    "cliente",
    "customer",
    "relazione",
    "relationship",
)

_PROJECT_CUES = (
    "progetto",
    "project",
    "startup",
    "azienda",
    "company",
    "product",
    "prodotto",
    "roadmap",
    "foundry",
    "studio",
    "build",
    "building",
    "costruisco",
    "sto costruendo",
    "founded",
    "fondato",
    "fondai",
    "lancio",
    "launched",
    "acquired",
    "acquisito",
    "acquisizione",
)

_EVENT_CUES = (
    "nel ",
    "on ",
    "quando",
    "when",
    "inaugurato",
    "inaugurated",
    "annunciato",
    "announced",
    "fondato",
    "founded",
    "acquisito",
    "acquired",
    "launched",
    "lanciato",
    "started",
    "iniziato",
    "sold",
    "venduto",
)

_FUTURE_CUES = (
    "voglio",
    "vorrei",
    "intendo",
    "mi piacerebbe",
    "obiettivo",
    "goal",
    "plan",
    "planning",
    "next",
    "prossimo",
    "future",
    "futuro",
    "will",
    "build next",
)

_DREAM_CUES = (
    "sogno",
    "dream",
    "aspirazione",
    "aspire",
    "ambizione",
    "ambition",
    "vision",
    "visione",
)

_HYPOTHESIS_CUES = (
    "forse",
    "probabilmente",
    "potrebbe",
    "potrei",
    "sembra",
    "mi sembra",
    "ipotesi",
    "hypothesis",
    "maybe",
    "might",
    "could be",
    "probably",
    "apparently",
)

_DEDUCTION_CUES = (
    "deduco",
    "deduzione",
    "concludo",
    "quindi",
    "significa che",
    "fa pensare",
    "implies",
    "therefore",
    "so this means",
    "conclusion",
)

_CONTRADICTION_CUES = (
    "non e vero",
    "non e piu",
    "non e' vero",
    "non e' piu",
    "non piu",
    "correggi",
    "correzione",
    "invece",
    "sostituisci",
    "replace",
    "correction",
    "wrong",
    "instead",
    "no longer",
    "supersede",
)

_LOW_CONFIDENCE_CUES = (
    "non ricordo",
    "credo",
    "penso",
    "mi pare",
    "not sure",
    "i think",
    "i believe",
)

_LEARNING_MODES = {
    "strict_review",
    "guided_learning",
    "autonomous_cautious",
    "autonomous_research",
    "sleep_review",
}

_HIGH_IMPACT_MEMORY_ACTS = {
    "update_relationship_state",
    "mark_contradiction",
    "supersede_old_memory",
    "demote_low_confidence_memory",
}

_UNCERTAIN_MEMORY_ACTS = {
    "create_hypothesis",
    "create_deduction",
    "create_future_intention",
    "create_dream_aspiration",
}


def _normalize_learning_mode(value: Any) -> str:
    candidate = str(value or "strict_review").strip().lower().replace("-", "_").replace(" ", "_")
    return candidate if candidate in _LEARNING_MODES else "strict_review"


def _normalize_question_limit(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 12
    return max(1, min(24, parsed))


def _normalize_persist_preview_limit(value: Any, *, default: int = 128) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(4096, parsed))


def _clarification_answer_map(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {
            str(key): " ".join(str(item or "").strip().split())
            for key, item in value.items()
            if str(key) and str(item or "").strip()
        }
    answers: dict[str, str] = {}
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            question_id = str(item.get("question_id") or item.get("id") or "").strip()
            answer = " ".join(str(item.get("answer") or item.get("value") or "").strip().split())
            if question_id and answer:
                answers[question_id] = answer
    return answers


def _learning_action_is_persistable(action: str) -> bool:
    return action in {"persist", "persist_as_hypothesis"}


def _learning_act_by_preview_id(cognitive_write_plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("preview_id") or ""): dict(item)
        for item in list((cognitive_write_plan or {}).get("memory_acts") or [])
        if str(item.get("preview_id") or "")
    }


def _learning_preview_nodes(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    primary = dict(bundle.get("primary_node_preview") or {})
    derived = [dict(node) for node in list(bundle.get("derived_nodes") or [])]
    return [node for node in [primary, *derived] if str(node.get("id") or "")]


def _learning_question_text(act: dict[str, Any]) -> str:
    source_text = summarize_text(str(act.get("source_text") or ""), limit=140)
    reasons = {str(reason) for reason in list(act.get("review_reasons") or [])}
    act_type = str(act.get("act_type") or "")
    if "unresolved_person_entity" in reasons:
        return f"Chi e' {source_text} rispetto a te o al progetto, e devo collegarlo a una persona gia' presente?"
    if act_type == "update_relationship_state" or "relationship_state_update" in reasons:
        return f"Questo cambia uno stato di relazione stabile o va salvato solo come evento/fatto isolato: {source_text}?"
    if act_type in {"mark_contradiction", "supersede_old_memory"}:
        return f"Quale memoria precedente devo correggere o superare con: {source_text}?"
    if act_type in {"create_hypothesis", "create_deduction"} or "inferred_or_uncertain_memory" in reasons:
        return f"Questa informazione va salvata come fatto certo, deduzione o ipotesi separata: {source_text}?"
    if "low_confidence_language" in reasons or "low_preview_confidence" in reasons:
        return f"Quanto e' affidabile questa memoria e devo salvarla come fatto o ipotesi: {source_text}?"
    return f"Confermi che questa memoria puo' essere salvata cosi': {source_text}?"


def _learning_risk_profile(act: dict[str, Any]) -> dict[str, Any]:
    act_type = str(act.get("act_type") or "")
    reasons = {str(reason) for reason in list(act.get("review_reasons") or [])}
    non_personal = (
        act_type == "no_op_duplicate"
        or str(act.get("claim_status") or "") in {"source_metadata", "instruction", "test_artifact"}
        or any(reason.startswith("non_personal_") for reason in reasons)
    )
    high_impact = act_type in _HIGH_IMPACT_MEMORY_ACTS or bool(
        reasons
        & {
            "relationship_state_update",
            "changes_or_supersedes_existing_memory",
            "merge_without_target",
            "unresolved_person_entity",
        }
    )
    uncertain = act_type in _UNCERTAIN_MEMORY_ACTS or bool(
        reasons
        & {
            "inferred_or_uncertain_memory",
            "low_confidence_language",
            "low_preview_confidence",
        }
    )
    resolved = bool(act.get("target_node_id")) and "unresolved_person_entity" not in reasons
    return {
        "non_personal": non_personal,
        "high_impact": high_impact,
        "uncertain": uncertain,
        "resolved": resolved,
        "review_required": bool(act.get("requires_human_review")),
    }


def _learning_source_evidence_profile(node: dict[str, Any], act: dict[str, Any]) -> dict[str, Any]:
    document_role = str(node.get("document_role") or "").strip()
    memory_type = str(node.get("memory_type") or "").strip()
    review_reasons = {str(reason) for reason in list(act.get("review_reasons") or [])}
    is_raw_anchor = bool(node.get("is_document_anchor")) or document_role == "anchor" or memory_type == "document_anchor"
    is_source_child = bool(str(node.get("source_unit_id") or "").strip()) or document_role in {"chunk", "summary", "fact"}
    is_qa_affordance = _source_preview_item_is_qa_affordance(node)
    source_metadata_child = bool(
        not is_qa_affordance
        and is_source_child
        and
        review_reasons
        & {
            "non_personal_claim_status:source_metadata",
            "non_personal_claim_status:instruction",
            "non_personal_claim_status:test_artifact",
        }
    )
    return {
        "is_source_evidence": bool(is_raw_anchor or is_source_child),
        "is_raw_anchor": is_raw_anchor,
        "is_source_child": is_source_child,
        "source_metadata_child": source_metadata_child,
        "is_qa_affordance": is_qa_affordance,
        "document_role": document_role,
    }


def _build_learning_policy(
    bundle: dict[str, Any],
    *,
    learning_mode: str | None = None,
    selected_preview_ids: list[str] | None = None,
    clarification_answers: dict[str, str] | list[dict[str, Any]] | None = None,
    approved_preview_ids: list[str] | None = None,
    question_limit: int | None = None,
    phase: str = "preview",
) -> dict[str, Any]:
    mode = _normalize_learning_mode(learning_mode or (bundle.get("learning_policy") or {}).get("mode"))
    max_questions = _normalize_question_limit(question_limit or (bundle.get("learning_policy") or {}).get("question_limit") or 3)
    max_persist_preview_ids = _normalize_persist_preview_limit(
        (bundle.get("learning_policy") or {}).get("max_persist_preview_ids"),
        default=128,
    )
    cognitive_plan = dict(bundle.get("cognitive_write_plan") or {})
    act_by_id = _learning_act_by_preview_id(cognitive_plan)
    preview_nodes = _learning_preview_nodes(bundle)
    selected_set = {str(item) for item in list(selected_preview_ids or []) if str(item)}
    approved_set = {str(item) for item in list(approved_preview_ids or []) if str(item)}
    answers = _clarification_answer_map(clarification_answers)
    questions: list[dict[str, Any]] = []
    node_actions: dict[str, dict[str, Any]] = {}
    persist_preview_ids: list[str] = []
    blocked_preview_ids: list[str] = []
    deferred_preview_ids: list[str] = []
    suppressed_preview_ids: list[str] = []
    research_tasks: list[dict[str, Any]] = []
    sleep_review_queue: list[dict[str, Any]] = []

    for node in preview_nodes:
        preview_id = str(node.get("id") or "")
        act = dict(act_by_id.get(preview_id) or {})
        if not act:
            act = {
                "preview_id": preview_id,
                "act_type": str(node.get("memory_act_type") or "create_new_fact"),
                "source_text": summarize_text(str(node.get("raw_text") or node.get("summary") or ""), limit=180),
                "claim_status": str(node.get("claim_status") or "fact"),
                "review_reasons": list(node.get("cognitive_review_reasons") or []),
                "requires_human_review": bool(node.get("requires_human_review")),
                "confidence": float(node.get("preview_confidence") or node.get("memory_confidence") or 0.74),
            }
        risk = _learning_risk_profile(act)
        source_evidence = _learning_source_evidence_profile(node, act)
        selected = not selected_set or preview_id in selected_set
        question_id = f"clarify::{preview_id}"
        has_answer = bool(answers.get(question_id))
        question_needed = bool(risk["high_impact"] or risk["uncertain"] or risk["review_required"])
        if source_evidence["is_source_evidence"]:
            question_needed = False
        if question_needed and len(questions) < max_questions:
            questions.append(
                {
                    "question_id": question_id,
                    "preview_id": preview_id,
                    "question": _learning_question_text(act),
                    "reason": ",".join(_unique_strings(list(act.get("review_reasons") or []), limit=4)) or str(act.get("act_type") or "review"),
                    "answered": has_answer,
                    "answer": answers.get(question_id),
                }
            )

        action = "not_selected"
        rationale = "preview node is not selected for this commit"
        requires_answer = False
        if selected:
            raw_anchor_suppression_reasons = {
                str(reason)
                for reason in list(act.get("review_reasons") or [])
                if str(reason) in {"non_personal_claim_status:instruction", "non_personal_claim_status:test_artifact"}
            }
            non_personal_raw_anchor = bool(source_evidence["is_raw_anchor"]) and (
                str(act.get("claim_status") or "") in {"instruction", "test_artifact"}
                or bool(raw_anchor_suppression_reasons)
            )
            if non_personal_raw_anchor:
                action = "suppress"
                rationale = "non-personal/source/test raw anchor is blocked from memory persistence"
            elif source_evidence["is_raw_anchor"]:
                action = "persist"
                rationale = "source raw document anchor is persisted as retrievable evidence, not as an interpreted personal memory"
            elif source_evidence["is_source_child"] and source_evidence["source_metadata_child"]:
                action = "suppress"
                rationale = "source child is navigation, legal boilerplate, or metadata rather than useful memory evidence"
            elif source_evidence["is_source_child"]:
                action = "persist"
                rationale = "source-grounded document child is persisted as evidence; clarification applies to interpretations, not traced source material"
            elif risk["non_personal"]:
                action = "suppress"
                rationale = "non-personal/source/test metadata is blocked from memory persistence"
            elif mode == "strict_review":
                if phase == "persist":
                    action = "persist"
                    rationale = "selected save acts as explicit strict-review approval"
                else:
                    action = "needs_approval" if risk["review_required"] else "persist_if_selected"
                    rationale = "strict review waits for explicit save approval before commit"
            elif mode == "guided_learning":
                if question_needed and not has_answer and preview_id not in approved_set:
                    action = "ask_clarification"
                    rationale = "guided learning needs a bounded clarification before persisting this item"
                    requires_answer = True
                elif risk["uncertain"]:
                    action = "persist_as_hypothesis"
                    rationale = "guided learning keeps uncertain material separate from facts"
                else:
                    action = "persist"
                    rationale = "guided learning has enough approval/context to persist"
            elif mode == "autonomous_cautious":
                if risk["high_impact"]:
                    action = "defer_for_review"
                    rationale = "autonomous cautious mode will not auto-commit high-impact memory changes"
                elif risk["uncertain"]:
                    action = "persist_as_hypothesis"
                    rationale = "autonomous cautious mode stores uncertainty as hypothesis"
                else:
                    action = "persist"
                    rationale = "low-risk selected item can be committed autonomously"
            elif mode == "autonomous_research":
                if risk["high_impact"] and not risk["resolved"]:
                    action = "research_then_review"
                    rationale = "autonomous research must resolve ambiguity before commit"
                elif risk["high_impact"]:
                    action = "defer_for_review"
                    rationale = "resolved high-impact mutations still require review"
                elif risk["uncertain"]:
                    action = "persist_as_hypothesis"
                    rationale = "autonomous research preserves unresolved conclusions as hypotheses"
                else:
                    action = "persist"
                    rationale = "existing memory/document context is sufficient for a low-risk commit"
            elif mode == "sleep_review":
                action = "defer_to_sleep_review"
                rationale = "sleep review mode creates reviewable maintenance work instead of direct commit"

        if _learning_action_is_persistable(action):
            persist_preview_ids.append(preview_id)
        elif action == "suppress":
            suppressed_preview_ids.append(preview_id)
        elif action == "not_selected":
            pass
        elif action == "research_then_review":
            deferred_preview_ids.append(preview_id)
            research_tasks.append(
                {
                    "preview_id": preview_id,
                    "source_text": act.get("source_text"),
                    "reason": rationale,
                    "allowed_sources": ["existing_memory", "document_workspace"],
                }
            )
        elif action == "defer_to_sleep_review":
            deferred_preview_ids.append(preview_id)
            sleep_review_queue.append(
                {
                    "preview_id": preview_id,
                    "source_text": act.get("source_text"),
                    "reason": rationale,
                }
            )
        elif action in {"ask_clarification", "needs_approval", "defer_for_review"}:
            blocked_preview_ids.append(preview_id)
        else:
            deferred_preview_ids.append(preview_id)

        node_actions[preview_id] = {
            "preview_id": preview_id,
            "mode": mode,
            "action": action,
            "persistable": _learning_action_is_persistable(action),
            "requires_answer": requires_answer,
            "question_ids": [question_id] if question_needed else [],
            "rationale": rationale,
            "risk": risk,
            "memory_act_type": str(act.get("act_type") or ""),
        }

    pending_questions = [question for question in questions if not question.get("answered")]
    if any(action["action"] == "ask_clarification" for action in node_actions.values()):
        status = "clarification_required"
    elif any(action["action"] in {"needs_approval", "defer_for_review"} for action in node_actions.values()):
        status = "approval_required"
    elif research_tasks:
        status = "research_required"
    elif sleep_review_queue:
        status = "deferred_to_sleep_review"
    elif persist_preview_ids:
        status = "ready_to_persist" if phase == "preview" else "persist_resolved"
    else:
        status = "no_action"

    return {
        "version": "pr12f.learning_policy.v1",
        "mode": mode,
        "phase": phase,
        "status": status,
        "question_limit": max_questions,
        "max_persist_preview_ids": max_persist_preview_ids,
        "selected_preview_ids": _unique_strings(list(selected_set), limit=64),
        "approved_preview_ids": _unique_strings(list(approved_set), limit=64),
        "questions": questions,
        "pending_question_count": len(pending_questions),
        "node_actions": node_actions,
        "selection_resolution": {
            "persist_preview_ids": _unique_strings(persist_preview_ids, limit=max_persist_preview_ids),
            "blocked_preview_ids": _unique_strings(blocked_preview_ids, limit=128),
            "deferred_preview_ids": _unique_strings(deferred_preview_ids, limit=128),
            "suppressed_preview_ids": _unique_strings(suppressed_preview_ids, limit=128),
        },
        "research_tasks": research_tasks,
        "sleep_review_queue": sleep_review_queue,
        "summary": {
            "total_preview_nodes": len(preview_nodes),
            "persistable_count": len(persist_preview_ids),
            "blocked_count": len(blocked_preview_ids),
            "deferred_count": len(deferred_preview_ids),
            "suppressed_count": len(suppressed_preview_ids),
            "question_count": len(questions),
            "pending_question_count": len(pending_questions),
            "research_task_count": len(research_tasks),
            "sleep_review_target_count": len(sleep_review_queue),
        },
    }


def _apply_learning_policy_annotations(node: dict[str, Any], learning_policy: dict[str, Any]) -> dict[str, Any]:
    preview_id = str(node.get("id") or "")
    action = dict((dict(learning_policy.get("node_actions") or {}).get(preview_id) or {}))
    if not action:
        return node
    action_name = str(action.get("action") or "")
    persistable = bool(action.get("persistable"))
    if action_name in {
        "not_selected",
        "suppress",
        "ask_clarification",
        "needs_approval",
        "defer_for_review",
        "research_then_review",
        "defer_to_sleep_review",
    }:
        selected_by_default = False
    elif persistable:
        selected_by_default = True
    else:
        selected_by_default = bool(node.get("selected_by_default", False))
    risk_flags = [key for key, value in dict(action.get("risk") or {}).items() if bool(value)]
    return {
        **node,
        "selected_by_default": selected_by_default,
        "learning_mode": str(learning_policy.get("mode") or ""),
        "learning_action": action_name,
        "learning_question_ids": list(action.get("question_ids") or []),
        "learning_policy_reasons": _unique_strings([action.get("rationale"), *risk_flags], limit=8),
    }


def _summarize_learning_policy(policy: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(policy or {})
    summary = dict(payload.get("summary") or {})
    resolution = dict(payload.get("selection_resolution") or {})
    return {
        "available": bool(payload),
        "mode": str(payload.get("mode") or "strict_review"),
        "status": str(payload.get("status") or "unknown"),
        "persistable_count": int(summary.get("persistable_count") or len(list(resolution.get("persist_preview_ids") or []))),
        "blocked_count": int(summary.get("blocked_count") or len(list(resolution.get("blocked_preview_ids") or []))),
        "deferred_count": int(summary.get("deferred_count") or len(list(resolution.get("deferred_preview_ids") or []))),
        "suppressed_count": int(summary.get("suppressed_count") or len(list(resolution.get("suppressed_preview_ids") or []))),
        "question_count": int(summary.get("question_count") or len(list(payload.get("questions") or []))),
        "pending_question_count": int(summary.get("pending_question_count") or payload.get("pending_question_count") or 0),
        "research_task_count": int(summary.get("research_task_count") or len(list(payload.get("research_tasks") or []))),
        "sleep_review_target_count": int(summary.get("sleep_review_target_count") or len(list(payload.get("sleep_review_queue") or []))),
    }


def _cognitive_source_classification(
    *,
    text: str,
    input_mode: str,
    source_label: str | None,
    source_type: str | None,
    primary_preview: dict[str, Any],
) -> dict[str, Any]:
    hygiene = effective_hygiene(primary_preview)
    folded = _cognitive_fold(" ".join([text, source_label or "", source_type or ""]))
    risk_reasons: list[str] = []
    if str(hygiene.get("source_trust") or "") in {"synthetic_test", "system_metadata"}:
        risk_reasons.append(f"source_trust:{hygiene.get('source_trust')}")
    if str(hygiene.get("claim_status") or "") in {"source_metadata", "instruction", "test_artifact"}:
        risk_reasons.append(f"claim_status:{hygiene.get('claim_status')}")
    if _has_any_cue(folded, ("synthetic", "stress test", "stress-testing", "source url", "document title", "expected retrieval behavior")):
        risk_reasons.append("source_or_test_artifact_cues")
    return {
        "input_mode": input_mode,
        "source_type": source_type or (dict(primary_preview.get("provenance") or {}).get("source_type") or ("document" if input_mode == "document" else "manual_text")),
        "source_label": source_label,
        "source_trust": str(hygiene.get("source_trust") or "user_asserted"),
        "claim_status": str(hygiene.get("claim_status") or "fact"),
        "contamination_risk": bool(risk_reasons),
        "risk_reasons": _unique_strings(risk_reasons, limit=8),
    }


def _node_matching_decision(raw_text: str, decisions: list[dict[str, Any]]) -> dict[str, Any] | None:
    normalized = normalize_preview_text(raw_text)
    for decision in decisions:
        decision_text = normalize_preview_text(str(decision.get("source_text") or ""))
        if not decision_text:
            continue
        if decision_text == normalized or lexical_overlap(decision_text, normalized) >= 0.88:
            return dict(decision)
    return None


def _is_self_identity_statement(text: str, identity_nucleus: dict[str, Any]) -> bool:
    folded = _cognitive_fold(text)
    if re.search(r"\b(?:sono|mi chiamo|i am|my name is)\b", folded):
        return True
    core_name = _cognitive_fold(identity_nucleus.get("core_name"))
    return bool(core_name and core_name in folded and _has_any_cue(folded, ("sono", "i am", "identity", "identita")))


def _classify_cognitive_memory_act(
    *,
    node: dict[str, Any],
    input_mode: str,
    merge_decision: dict[str, Any] | None,
) -> str:
    raw_text = str(node.get("raw_text") or node.get("summary") or "")
    folded = _cognitive_fold(raw_text)
    hygiene = effective_hygiene(node)
    claim_status = str(hygiene.get("claim_status") or "fact")
    source_trust = str(hygiene.get("source_trust") or "user_asserted")
    memory_type = str(node.get("memory_type") or "")
    node_kind = str(node.get("node_kind") or "")
    preview_kind = str(node.get("preview_kind") or "")
    persist_mode = _normalize_persist_mode((merge_decision or {}).get("decision") or node.get("persist_mode"))
    if claim_status in {"source_metadata", "instruction", "test_artifact"} or source_trust in {"synthetic_test", "system_metadata"}:
        return "no_op_duplicate"
    if input_mode == "document" and (bool(node.get("is_document_anchor")) or memory_type == "document_anchor"):
        return "attach_document"
    if _has_any_cue(folded, _CONTRADICTION_CUES):
        return "supersede_old_memory" if persist_mode == "merge_into_existing" else "mark_contradiction"
    if _has_any_cue(folded, _DREAM_CUES):
        return "create_dream_aspiration"
    if _has_any_cue(folded, _FUTURE_CUES):
        return "create_future_intention"
    if _has_any_cue(folded, _HYPOTHESIS_CUES):
        return "create_hypothesis"
    if _has_any_cue(folded, _DEDUCTION_CUES):
        return "create_deduction"
    if node_kind == "relationship_claim" or memory_type == "relational" or _has_any_cue(folded, _RELATIONSHIP_CUES):
        return "update_relationship_state"
    if node_kind == "event_claim" or memory_type == "episodic" or re.search(r"\b(?:19|20)\d{2}\b", folded) or _has_any_cue(folded, _EVENT_CUES):
        return "create_event"
    if node_kind == "project_claim" or memory_type == "project" or _has_any_cue(folded, _PROJECT_CUES):
        return "create_project_state"
    if persist_mode == "merge_into_existing":
        return "update_existing_fact"
    if persist_mode == "attach_as_alias_or_variant":
        return "update_existing_fact"
    if preview_kind == "entity":
        return "create_new_fact"
    return "create_new_fact"


def _cognitive_review_reasons(
    *,
    node: dict[str, Any],
    memory_act_type: str,
    merge_decision: dict[str, Any] | None,
    identity_decision: dict[str, Any] | None,
    identity_nucleus: dict[str, Any],
) -> list[str]:
    reasons: list[str] = []
    raw_text = str(node.get("raw_text") or node.get("summary") or "")
    folded = _cognitive_fold(raw_text)
    hygiene = effective_hygiene(node)
    is_source_child = bool(node.get("source_unit_id")) or str(node.get("document_role") or "") in {"chunk", "summary", "fact"}
    if str(hygiene.get("claim_status") or "") in {"source_metadata", "instruction", "test_artifact"}:
        reasons.append(f"non_personal_claim_status:{hygiene.get('claim_status')}")
    if str(hygiene.get("source_trust") or "") in {"synthetic_test", "system_metadata"}:
        reasons.append(f"non_personal_source_trust:{hygiene.get('source_trust')}")
    if memory_act_type in {"mark_contradiction", "supersede_old_memory", "demote_low_confidence_memory"}:
        reasons.append("changes_or_supersedes_existing_memory")
    if memory_act_type in {"create_hypothesis", "create_deduction"}:
        reasons.append("inferred_or_uncertain_memory")
    if (
        memory_act_type == "update_relationship_state"
        and not is_source_child
        and not _has_any_cue(folded, ("padre", "madre", "father", "mother", "fratello", "sorella", "brother", "sister"))
    ):
        reasons.append("relationship_state_update")
    if _has_any_cue(folded, _LOW_CONFIDENCE_CUES):
        reasons.append("low_confidence_language")
    confidence = float(node.get("preview_confidence") or node.get("memory_confidence") or node.get("derivation_confidence") or 0.0)
    if confidence and confidence < 0.72:
        reasons.append("low_preview_confidence")
    persist_mode = _normalize_persist_mode((merge_decision or {}).get("decision") or node.get("persist_mode"))
    if persist_mode in {"merge_into_existing", "attach_as_alias_or_variant"} and not (merge_decision or {}).get("target_node_id"):
        reasons.append("merge_without_target")
    if str(node.get("preview_kind") or "") == "entity" and str(node.get("node_kind") or "") == "person":
        resolved = bool((identity_decision or {}).get("resolved_node_id"))
        if not resolved and not _is_self_identity_statement(raw_text, identity_nucleus):
            reasons.append("unresolved_person_entity")
    return _unique_strings(reasons, limit=8)


def _cognitive_clarification_question(
    *,
    node: dict[str, Any],
    memory_act_type: str,
    review_reasons: list[str],
) -> str | None:
    raw_text = summarize_text(str(node.get("raw_text") or node.get("summary") or ""), limit=120)
    if not raw_text:
        return None
    if "unresolved_person_entity" in review_reasons:
        return f"Chi e' {raw_text} rispetto a te o al progetto, e devo collegarlo a una persona gia' presente?"
    if memory_act_type == "update_relationship_state" and "relationship_state_update" in review_reasons:
        return f"Vuoi salvare '{raw_text}' come stato di relazione stabile, evento puntuale o semplice fatto?"
    if memory_act_type in {"mark_contradiction", "supersede_old_memory"}:
        return f"Quale memoria precedente devo correggere o superare con '{raw_text}'?"
    if memory_act_type in {"create_hypothesis", "create_deduction"}:
        return f"'{raw_text}' e' una tua certezza, una deduzione o un'ipotesi da tenere separata dai fatti?"
    return None


def _build_cognitive_state_transition(
    *,
    node: dict[str, Any],
    memory_act_type: str,
    identity_decision: dict[str, Any] | None,
    merge_decision: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if memory_act_type not in {"update_relationship_state", "create_project_state", "create_event", "create_future_intention", "create_dream_aspiration", "supersede_old_memory", "mark_contradiction"}:
        return None
    raw_text = str(node.get("raw_text") or node.get("summary") or "")
    if memory_act_type == "update_relationship_state":
        domain = "relationship"
    elif memory_act_type == "create_project_state":
        domain = "project"
    elif memory_act_type in {"create_event", "create_future_intention", "create_dream_aspiration"}:
        domain = "timeline"
    else:
        domain = "memory_lifecycle"
    return {
        "preview_id": str(node.get("id") or ""),
        "state_domain": domain,
        "transition_type": memory_act_type,
        "target_node_id": (merge_decision or {}).get("target_node_id") or (identity_decision or {}).get("resolved_node_id"),
        "source_text": summarize_text(raw_text, limit=180),
        "confidence": round(float(node.get("preview_confidence") or node.get("memory_confidence") or node.get("derivation_confidence") or 0.74), 4),
    }


def _build_cognitive_write_plan(
    *,
    text: str,
    input_mode: str,
    source_label: str | None,
    source_type: str | None,
    compiler_payload: dict[str, Any] | None,
    primary_preview: dict[str, Any],
    derived_nodes: list[dict[str, Any]],
    merge_decisions: list[dict[str, Any]],
    identity_decisions: list[dict[str, Any]],
    identity_nucleus: dict[str, Any],
    nearby_context: dict[str, Any],
    question_limit: int,
) -> dict[str, Any]:
    source_classification = _cognitive_source_classification(
        text=text,
        input_mode=input_mode,
        source_label=source_label,
        source_type=source_type,
        primary_preview=primary_preview,
    )
    preview_nodes = [primary_preview, *derived_nodes]
    memory_acts: list[dict[str, Any]] = []
    entities: list[dict[str, Any]] = []
    state_transitions: list[dict[str, Any]] = []
    hypothesis_proposals: list[dict[str, Any]] = []
    deduction_proposals: list[dict[str, Any]] = []
    contradiction_checks: list[dict[str, Any]] = []
    clarification_questions: list[str] = []
    review_reasons_all: list[str] = []
    sleep_evolve_queue: list[dict[str, Any]] = []
    node_annotations: dict[str, dict[str, Any]] = {}

    for node in preview_nodes:
        node_id = str(node.get("id") or "")
        raw_text = str(node.get("raw_text") or node.get("summary") or "")
        merge_decision = _node_matching_decision(raw_text, merge_decisions)
        identity_decision = _node_matching_decision(raw_text, identity_decisions)
        memory_act_type = _classify_cognitive_memory_act(
            node=node,
            input_mode=input_mode,
            merge_decision=merge_decision,
        )
        review_reasons = _cognitive_review_reasons(
            node=node,
            memory_act_type=memory_act_type,
            merge_decision=merge_decision,
            identity_decision=identity_decision,
            identity_nucleus=identity_nucleus,
        )
        requires_review = bool(review_reasons)
        if node_id and str(node.get("preview_kind") or "") == "entity":
            entities.append(
                {
                    "preview_id": node_id,
                    "text": summarize_text(raw_text, limit=120),
                    "entity_type": str(node.get("node_kind") or ""),
                    "resolution_type": (identity_decision or {}).get("resolution_type"),
                    "resolved_node_id": (identity_decision or {}).get("resolved_node_id"),
                    "confidence": round(float((identity_decision or {}).get("confidence") or node.get("preview_confidence") or 0.0), 4),
                }
            )
        act = {
            "preview_id": node_id,
            "preview_kind": str(node.get("preview_kind") or ""),
            "source_text": summarize_text(raw_text, limit=180),
            "act_type": memory_act_type,
            "memory_type": str(node.get("memory_type") or ""),
            "claim_status": str(effective_hygiene(node).get("claim_status") or "fact"),
            "persist_mode": _normalize_persist_mode((merge_decision or {}).get("decision") or node.get("persist_mode")),
            "target_node_id": (merge_decision or {}).get("target_node_id") or (identity_decision or {}).get("resolved_node_id"),
            "confidence": round(float(node.get("preview_confidence") or node.get("memory_confidence") or node.get("derivation_confidence") or 0.74), 4),
            "requires_human_review": requires_review,
            "review_reasons": review_reasons,
        }
        memory_acts.append(act)
        review_reasons_all.extend(review_reasons)
        question = _cognitive_clarification_question(node=node, memory_act_type=memory_act_type, review_reasons=review_reasons)
        if question:
            clarification_questions.append(question)
        transition = _build_cognitive_state_transition(
            node=node,
            memory_act_type=memory_act_type,
            identity_decision=identity_decision,
            merge_decision=merge_decision,
        )
        if transition:
            state_transitions.append(transition)
        if memory_act_type == "create_hypothesis":
            hypothesis_proposals.append(
                {
                    "preview_id": node_id,
                    "text": summarize_text(raw_text, limit=200),
                    "confidence": act["confidence"],
                    "status": "proposed_not_fact",
                }
            )
        if memory_act_type == "create_deduction":
            deduction_proposals.append(
                {
                    "preview_id": node_id,
                    "text": summarize_text(raw_text, limit=200),
                    "confidence": act["confidence"],
                    "status": "proposed_inference",
                }
            )
        if memory_act_type in {"mark_contradiction", "supersede_old_memory"}:
            contradiction_checks.append(
                {
                    "preview_id": node_id,
                    "source_text": summarize_text(raw_text, limit=200),
                    "target_node_id": act.get("target_node_id"),
                    "requires_target_review": not bool(act.get("target_node_id")),
                }
            )
        if memory_act_type in {"update_relationship_state", "create_project_state", "create_hypothesis", "create_deduction", "supersede_old_memory", "mark_contradiction"}:
            sleep_evolve_queue.append(
                {
                    "preview_id": node_id,
                    "reason": f"{memory_act_type}_needs_consolidation",
                    "priority": 0.9 if memory_act_type in {"supersede_old_memory", "mark_contradiction"} else 0.72,
                }
            )
        annotation_claim_status = None
        annotation_source_trust = None
        if memory_act_type in {"create_hypothesis", "create_deduction"}:
            annotation_claim_status = "hypothesis"
            annotation_source_trust = "inferred"
        node_annotations[node_id] = {
            "memory_act_type": memory_act_type,
            "cognitive_status": "review_required" if requires_review else "ready",
            "requires_human_review": requires_review,
            "cognitive_review_reasons": review_reasons,
            "cognitive_target_node_ids": _unique_strings([act.get("target_node_id")], limit=4),
            "claim_status": annotation_claim_status,
            "source_trust": annotation_source_trust,
        }

    llm_plan = dict((compiler_payload or {}).get("cognitive_write_plan") or {})
    dominant_memory_acts = _unique_strings([item.get("act_type") for item in memory_acts], limit=16)
    create_count = sum(1 for item in memory_acts if item.get("persist_mode") == "create")
    merge_count = sum(1 for item in memory_acts if item.get("persist_mode") == "merge_into_existing")
    alias_count = sum(1 for item in memory_acts if item.get("persist_mode") == "attach_as_alias_or_variant")
    suppressed_count = sum(1 for item in memory_acts if item.get("act_type") == "no_op_duplicate")
    review_required_count = sum(1 for item in memory_acts if item.get("requires_human_review"))
    clarification_questions = _unique_strings(
        clarification_questions,
        limit=max(1, int(question_limit)),
    )
    review_reasons = _unique_strings(review_reasons_all, limit=16)
    return {
        "version": "pr12e.cognitive_write_plan.v1",
        "pipeline": [
            "source_classification",
            "entity_resolution",
            "existing_memory_scan",
            "memory_act_classification",
            "state_transition_proposal",
            "deduction_hypothesis_proposal",
            "contradiction_check",
            "human_review_policy",
            "persist_policy",
            "link_geometry_and_sleep_evolve_queue",
        ],
        "source_classification": source_classification,
        "entities": entities,
        "existing_memory_scan": {
            "merge_candidate_count": len(merge_decisions),
            "identity_resolution_count": len(identity_decisions),
            "resolved_identity_count": sum(1 for item in identity_decisions if item.get("resolved_node_id")),
            "nearby_context_count": len(list((nearby_context or {}).get("nearby_nodes") or [])),
            "identity_core_name": identity_nucleus.get("core_name"),
        },
        "memory_acts": memory_acts,
        "dominant_memory_acts": dominant_memory_acts,
        "state_transitions": state_transitions,
        "deduction_proposals": deduction_proposals,
        "hypothesis_proposals": hypothesis_proposals,
        "contradiction_checks": contradiction_checks,
        "human_review": {
            "required": bool(review_required_count),
            "review_required_count": review_required_count,
            "review_reasons": review_reasons,
            "clarification_questions": clarification_questions,
            "next_slice_policy": "PR-12F will execute guided/autonomous learning modes; PR-12E only prepares the decision object.",
        },
        "mutation_plan": {
            "default_policy": "review_before_persist" if review_required_count else "safe_to_persist",
            "create_count": create_count,
            "merge_into_existing_count": merge_count,
            "attach_as_alias_or_variant_count": alias_count,
            "suppressed_or_non_personal_count": suppressed_count,
            "eligible_for_autonomous_persist": bool(not review_required_count and not source_classification.get("contamination_risk")),
        },
        "sleep_evolve_queue": sleep_evolve_queue[:16],
        "node_annotations": node_annotations,
        "llm_cognitive_write_plan": llm_plan,
        "summary": {
            "memory_act_count": len(memory_acts),
            "dominant_memory_acts": dominant_memory_acts,
            "review_required_count": review_required_count,
            "clarification_question_count": len(clarification_questions),
            "state_transition_count": len(state_transitions),
            "hypothesis_count": len(hypothesis_proposals),
            "deduction_count": len(deduction_proposals),
            "sleep_evolve_target_count": len(sleep_evolve_queue),
        },
    }


def _apply_cognitive_write_annotations(node: dict[str, Any], cognitive_write_plan: dict[str, Any]) -> dict[str, Any]:
    node_id = str(node.get("id") or "")
    annotation = dict((dict(cognitive_write_plan.get("node_annotations") or {}).get(node_id) or {}))
    if not annotation:
        return node
    updated = {
        **node,
        "memory_act_type": annotation.get("memory_act_type"),
        "cognitive_status": annotation.get("cognitive_status"),
        "requires_human_review": bool(annotation.get("requires_human_review")),
        "cognitive_review_reasons": list(annotation.get("cognitive_review_reasons") or []),
        "cognitive_target_node_ids": list(annotation.get("cognitive_target_node_ids") or []),
    }
    current_hygiene = effective_hygiene(updated)
    if annotation.get("claim_status") == "hypothesis" and str(current_hygiene.get("claim_status") or "") == "fact":
        inferred_hygiene = build_hygiene_metadata(
            raw_text=updated.get("raw_text") or updated.get("summary") or "",
            input_mode="document" if updated.get("is_document_anchor") else "auto",
            provenance=dict(updated.get("provenance") or {}),
            explicit_source_trust=annotation.get("source_trust") or "inferred",
            explicit_claim_status="hypothesis",
            memory_type=updated.get("memory_type"),
            derivation_role=updated.get("derivation_role"),
            is_document_anchor=bool(updated.get("is_document_anchor")),
        )
        updated.update(inferred_hygiene)
    return updated


def _summarize_cognitive_write_plan(plan: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(plan or {})
    summary = dict(payload.get("summary") or {})
    human_review = dict(payload.get("human_review") or {})
    mutation_plan = dict(payload.get("mutation_plan") or {})
    return {
        "available": bool(payload),
        "memory_act_count": int(summary.get("memory_act_count") or len(list(payload.get("memory_acts") or []))),
        "dominant_memory_acts": list(summary.get("dominant_memory_acts") or payload.get("dominant_memory_acts") or []),
        "review_required_count": int(summary.get("review_required_count") or human_review.get("review_required_count") or 0),
        "clarification_question_count": int(summary.get("clarification_question_count") or len(list(human_review.get("clarification_questions") or []))),
        "state_transition_count": int(summary.get("state_transition_count") or len(list(payload.get("state_transitions") or []))),
        "hypothesis_count": int(summary.get("hypothesis_count") or len(list(payload.get("hypothesis_proposals") or []))),
        "deduction_count": int(summary.get("deduction_count") or len(list(payload.get("deduction_proposals") or []))),
        "sleep_evolve_target_count": int(summary.get("sleep_evolve_target_count") or len(list(payload.get("sleep_evolve_queue") or []))),
        "default_policy": str(mutation_plan.get("default_policy") or "unknown"),
    }


def build_write_trace(
    bundle: dict[str, Any],
    *,
    input_mode: str | None,
    selected_preview_ids: list[str] | None = None,
    persisted_node_ids: list[str] | None = None,
    persisted_edge_count: int | None = None,
    merged_into_existing_ids: list[str] | None = None,
    identity_nucleus: dict[str, Any] | None = None,
    mode: str = "write_preview",
) -> dict[str, Any]:
    primary_preview = dict(bundle.get("primary_node_preview") or {})
    derived_nodes = list(bundle.get("derived_nodes") or [])
    merge_decisions = list(bundle.get("merge_decisions") or [])
    identity_decisions = list(bundle.get("identity_resolution_decisions") or [])
    selected_ids = set(selected_preview_ids or [])
    if primary_preview.get("id"):
        selected_ids.add(str(primary_preview["id"]))
    if not selected_ids:
        selected_ids = {
            str(node.get("id") or "")
            for node in [primary_preview, *derived_nodes]
            if str(node.get("id") or "")
        }
    claim_count = sum(1 for node in derived_nodes if str(node.get("preview_kind") or "") == "claim")
    entity_count = sum(1 for node in derived_nodes if str(node.get("preview_kind") or "") == "entity")
    merge_summary = _summarize_write_merge_decisions(merge_decisions)
    identity_summary = _summarize_write_identity_decisions(identity_decisions)
    cognitive_write_plan = dict(bundle.get("cognitive_write_plan") or {})
    cognitive_summary = _summarize_cognitive_write_plan(cognitive_write_plan)
    learning_policy = dict(bundle.get("learning_policy") or {})
    learning_summary = _summarize_learning_policy(learning_policy)
    persisted_ids = [str(node_id) for node_id in list(persisted_node_ids or []) if str(node_id)]
    persisted_summary = {
        "persisted_node_count": len(persisted_ids),
        "persisted_edge_count": int(persisted_edge_count or 0),
        "merged_into_existing_count": len({str(node_id) for node_id in list(merged_into_existing_ids or []) if str(node_id)}),
        "primary_persisted_node_id": persisted_ids[0] if persisted_ids else None,
        "identity_core_name": str((identity_nucleus or {}).get("core_name") or ""),
    }
    persistence_complete = mode == "write_persist"
    primary_memory_type = str(primary_preview.get("memory_type") or "memory")
    derivation_mode = str(bundle.get("derivation_mode") or "heuristic")
    actors = [
        {
            "actor_id": "compiler::projection",
            "actor_kind": "compiler",
            "status": "completed",
            "summary": f"{derivation_mode.upper()} compiler projected the primary memory and derived supporting structures.",
            "metrics": {
                "claims": claim_count,
                "entities": entity_count,
                "selected_preview_count": len(selected_ids),
                "memory_acts": int(cognitive_summary["memory_act_count"]),
                "review_required": int(cognitive_summary["review_required_count"]),
                "learning_mode": str(learning_summary["mode"]),
                "learning_status": str(learning_summary["status"]),
            },
        },
        {
            "actor_id": "merge_resolver::review",
            "actor_kind": "merge_resolver",
            "status": "completed",
            "summary": f"Merge review classified {merge_summary['total']} candidate memories before persistence.",
            "metrics": merge_summary,
        },
        {
            "actor_id": "identity_resolver::review",
            "actor_kind": "identity_resolver",
            "status": "completed",
            "summary": f"Identity resolution reviewed {identity_summary['total']} candidates and resolved {identity_summary['resolved_count']}.",
            "metrics": identity_summary,
        },
        {
            "actor_id": "persistence::commit",
            "actor_kind": "persistence_stage",
            "status": "completed" if persistence_complete else "pending",
            "summary": (
                f"Persistence committed {persisted_summary['persisted_node_count']} nodes and {persisted_summary['persisted_edge_count']} edges."
                if persistence_complete
                else "Persistence has not been executed yet; the review bundle is ready for operator approval."
            ),
            "metrics": persisted_summary,
        },
    ]
    stages = [
        {
            "stage_id": "input_received",
            "actor_id": "compiler::projection",
            "actor_kind": "compiler",
            "status": "completed",
            "summary": f"Input received in {input_mode or 'auto'} mode and normalized for AGVM compilation.",
            "counts": {
                "input_chars": len(str(primary_preview.get("raw_text") or "")),
                "input_words": len(str(primary_preview.get("raw_text") or "").split()),
            },
        },
        {
            "stage_id": "primary_projection_ready",
            "actor_id": "compiler::projection",
            "actor_kind": "compiler",
            "status": "completed",
            "summary": f"Primary projection prepared as {primary_memory_type}.",
            "counts": {"primary_nodes": 1},
        },
        {
            "stage_id": "derived_nodes_ready",
            "actor_id": "compiler::projection",
            "actor_kind": "compiler",
            "status": "completed",
            "summary": f"Derived structure includes {claim_count} claims and {entity_count} entities.",
            "counts": {
                "claims": claim_count,
                "entities": entity_count,
                "total_derived": len(derived_nodes),
            },
        },
        {
            "stage_id": "merge_review_ready",
            "actor_id": "merge_resolver::review",
            "actor_kind": "merge_resolver",
            "status": "completed",
            "summary": f"Merge review classified create/merge/alias decisions across {merge_summary['total']} preview items.",
            "counts": {
                "create": int(merge_summary["create_count"]),
                "merge_into_existing": int(merge_summary["merge_into_existing_count"]),
                "attach_as_alias_or_variant": int(merge_summary["attach_as_alias_or_variant_count"]),
            },
        },
        {
            "stage_id": "identity_resolution_ready",
            "actor_id": "identity_resolver::review",
            "actor_kind": "identity_resolver",
            "status": "completed",
            "summary": f"Identity resolution prepared {identity_summary['resolved_count']} grounded links.",
            "counts": {
                "total": int(identity_summary["total"]),
                "resolved": int(identity_summary["resolved_count"]),
            },
        },
        {
            "stage_id": "cognitive_write_ready",
            "actor_id": "compiler::projection",
            "actor_kind": "compiler",
            "status": "completed",
            "summary": (
                "Cognitive write plan classified memory acts, review needs, state transitions and sleep/evolve targets."
                if cognitive_summary["available"]
                else "Cognitive write plan not present on this legacy bundle."
            ),
            "counts": {
                "memory_acts": int(cognitive_summary["memory_act_count"]),
                "review_required": int(cognitive_summary["review_required_count"]),
                "clarification_questions": int(cognitive_summary["clarification_question_count"]),
                "state_transitions": int(cognitive_summary["state_transition_count"]),
                "sleep_evolve_targets": int(cognitive_summary["sleep_evolve_target_count"]),
            },
        },
        {
            "stage_id": "learning_policy_ready",
            "actor_id": "compiler::projection",
            "actor_kind": "compiler",
            "status": "completed",
            "summary": (
                f"Learning policy resolved mode {learning_summary['mode']} with status {learning_summary['status']}."
                if learning_summary["available"]
                else "Learning policy not present on this legacy bundle."
            ),
            "counts": {
                "persistable": int(learning_summary["persistable_count"]),
                "blocked": int(learning_summary["blocked_count"]),
                "deferred": int(learning_summary["deferred_count"]),
                "questions": int(learning_summary["question_count"]),
                "pending_questions": int(learning_summary["pending_question_count"]),
            },
        },
        {
            "stage_id": "review_ready",
            "actor_id": "compiler::projection",
            "actor_kind": "compiler",
            "status": "completed",
            "summary": f"The review bundle is ready with {len(selected_ids)} selected preview nodes.",
            "counts": {"selected_preview_count": len(selected_ids)},
        },
        {
            "stage_id": "persist_complete",
            "actor_id": "persistence::commit",
            "actor_kind": "persistence_stage",
            "status": "completed" if persistence_complete else "pending",
            "summary": (
                f"Persist complete for {persisted_summary['persisted_node_count']} nodes."
                if persistence_complete
                else "Persistence pending. Save Selected, Save All, or Bootstrap to commit this write bundle."
            ),
            "counts": {
                "persisted_nodes": int(persisted_summary["persisted_node_count"]),
                "persisted_edges": int(persisted_summary["persisted_edge_count"]),
                "merged_existing": int(persisted_summary["merged_into_existing_count"]),
            },
        },
    ]
    return {
        "mode": mode,
        "input_mode": input_mode,
        "derivation_mode": derivation_mode,
        "actors": actors,
        "stages": stages,
        "merge_decision_summary": merge_summary,
        "identity_resolution_summary": identity_summary,
        "cognitive_write_summary": cognitive_summary,
        "learning_policy_summary": learning_summary,
        "persisted_node_summary": persisted_summary,
    }


def _geometry_normalize_to_radius(position: dict[str, float], radius: float) -> dict[str, float]:
    px = float(position["x"])
    py = float(position["y"])
    pz = float(position["z"])
    norm = math.sqrt(px * px + py * py + pz * pz)
    if norm <= 1e-12:
        return {"x": 0.0, "y": 0.0, "z": round(radius, 12)}
    return {
        "x": round((px / norm) * radius, 12),
        "y": round((py / norm) * radius, 12),
        "z": round((pz / norm) * radius, 12),
    }


def _geometry_unit(position: dict[str, float]) -> dict[str, float]:
    return _geometry_normalize_to_radius(position, 1.0)


def _geometry_cross(a: dict[str, float], b: dict[str, float]) -> dict[str, float]:
    return {
        "x": float(a["y"]) * float(b["z"]) - float(a["z"]) * float(b["y"]),
        "y": float(a["z"]) * float(b["x"]) - float(a["x"]) * float(b["z"]),
        "z": float(a["x"]) * float(b["y"]) - float(a["y"]) * float(b["x"]),
    }


def _geometry_deterministic_jitter(seed_text: str) -> dict[str, float]:
    digest = hashlib.sha256(seed_text.encode("utf-8")).digest()
    components: list[float] = []
    for index in range(3):
        chunk = int.from_bytes(digest[index * 2 : index * 2 + 2], "big")
        components.append((chunk / 65535.0) * 2.0 - 1.0)
    norm = math.sqrt(sum(component * component for component in components))
    if norm <= 1e-12:
        return {"x": 0.0, "y": 0.0, "z": 1.0}
    return {
        "x": components[0] / norm,
        "y": components[1] / norm,
        "z": components[2] / norm,
    }


def _semantic_target_radius_live(node: dict[str, Any]) -> float:
    temporal_role = str(node.get("temporal_role") or "").strip().lower()
    node_kind = str(node.get("node_kind") or "").strip().lower()
    guide_area = str((node.get("provenance") or {}).get("guide_conceptual_area") or "").strip()
    if temporal_role == "stable_identity" or node_kind in {"identity", "value"}:
        return 0.18
    if node.get("is_document_anchor") or node_kind in {"document", "media", "artifact", "document_anchor"}:
        return 0.78
    if guide_area == "Identity":
        return 0.58 if temporal_role == "past_state" else 0.24
    if guide_area == "Relationships":
        if temporal_role == "past_state":
            return 0.60
        if temporal_role == "current_state":
            return 0.40
        return 0.42
    if guide_area == "Projects":
        if temporal_role in {"past_state", "future_intent"}:
            return 0.62
        if temporal_role == "current_state":
            return 0.40
        return 0.44
    if guide_area == "Expression":
        return 0.74 if temporal_role == "past_state" else 0.46
    if guide_area == "Values":
        return 0.22
    if guide_area == "History":
        return 0.68
    if temporal_role in {"past_state", "future_intent"}:
        return 0.64
    if node_kind == "event":
        return 0.58
    return 0.52


def _radial_band_target_live(node: dict[str, Any], radius: float, crowd_push: float) -> float:
    temporal_role = str(node.get("temporal_role") or "").strip().lower()
    node_kind = str(node.get("node_kind") or "").strip().lower()
    guide_area = str((node.get("provenance") or {}).get("guide_conceptual_area") or "").strip()
    base_radius = radius
    if temporal_role == "stable_identity" or node_kind in {"identity", "value"} or guide_area in {"Identity", "Values"}:
        base_radius = min(base_radius, 0.22)
    elif temporal_role in {"past_state", "future_intent"} or node.get("is_document_anchor") or node_kind in {"document", "media", "artifact", "document_anchor"}:
        base_radius = max(base_radius, 0.50)
    elif node_kind in {"event"} or guide_area == "History":
        base_radius = max(base_radius, 0.56)
    crowd_scale = 0.007
    crowd_cap = 0.10
    if guide_area in {"Projects", "Relationships", "Expression"} and temporal_role not in {"past_state", "future_intent"}:
        crowd_scale = 0.0035
        crowd_cap = 0.06
    adjusted = base_radius + min(crowd_cap, crowd_scale * crowd_push)
    return max(0.08, min(0.96, adjusted))


def rebalance_graph_geometry(nodes: list[dict[str, Any]], *, iterations: int = 8) -> list[dict[str, Any]]:
    if len(nodes) <= 2:
        return list(nodes)

    rebalanced = [dict(node) for node in nodes]
    active_indices = [
        index
        for index, node in enumerate(rebalanced)
        if str(node.get("lifecycle_status") or "active").lower() != "deleted"
    ]
    if len(active_indices) <= 2:
        return rebalanced

    positions = {
        index: {
            "x": float((rebalanced[index].get("final_position") or rebalanced[index].get("base_position") or {}).get("x", 0.0)),
            "y": float((rebalanced[index].get("final_position") or rebalanced[index].get("base_position") or {}).get("y", 0.0)),
            "z": float((rebalanced[index].get("final_position") or rebalanced[index].get("base_position") or {}).get("z", 0.18)),
        }
        for index in active_indices
    }
    target_radii = {index: _semantic_target_radius_live(rebalanced[index]) for index in active_indices}
    jitter_vectors = {
        index: _geometry_deterministic_jitter(
            f"{rebalanced[index].get('id') or ''}|{rebalanced[index].get('summary') or ''}|{index}"
        )
        for index in active_indices
    }

    for _ in range(max(1, iterations)):
        bucket_counts: dict[tuple[int, int, int], list[int]] = defaultdict(list)
        for index in active_indices:
            position = positions[index]
            bucket = (
                int(round(float(position["x"]) / BUCKET_SIZE)),
                int(round(float(position["y"]) / BUCKET_SIZE)),
                int(round(float(position["z"]) / BUCKET_SIZE)),
            )
            bucket_counts[bucket].append(index)

        updated_positions: dict[int, dict[str, float]] = {}
        for index in active_indices:
            node = rebalanced[index]
            position = positions[index]
            unit = _geometry_unit(position)
            ux, uy, uz = unit["x"], unit["y"], unit["z"]
            repulsion_x = 0.0
            repulsion_y = 0.0
            repulsion_z = 0.0
            close_neighbors = 0
            same_bucket_neighbors = 0
            current_bucket = (
                int(round(float(position["x"]) / BUCKET_SIZE)),
                int(round(float(position["y"]) / BUCKET_SIZE)),
                int(round(float(position["z"]) / BUCKET_SIZE)),
            )
            for other_index in active_indices:
                if other_index == index:
                    continue
                other = positions[other_index]
                dx = float(position["x"]) - float(other["x"])
                dy = float(position["y"]) - float(other["y"])
                dz = float(position["z"]) - float(other["z"])
                distance = math.sqrt(dx * dx + dy * dy + dz * dz)
                if distance <= 1e-9:
                    continue
                if distance < 0.22:
                    weight = (0.22 - distance) / 0.22
                    repulsion_x += (dx / distance) * weight
                    repulsion_y += (dy / distance) * weight
                    repulsion_z += (dz / distance) * weight
                    close_neighbors += 1
                other_bucket = (
                    int(round(float(other["x"]) / BUCKET_SIZE)),
                    int(round(float(other["y"]) / BUCKET_SIZE)),
                    int(round(float(other["z"]) / BUCKET_SIZE)),
                )
                if other_bucket == current_bucket:
                    same_bucket_neighbors += 1

            crowd_push = max(0, same_bucket_neighbors - 3) + max(0, close_neighbors - 4)
            target_radius = _radial_band_target_live(node, target_radii[index], float(crowd_push))
            tangent = _geometry_cross(unit, jitter_vectors[index])
            tangent_norm = math.sqrt(tangent["x"] ** 2 + tangent["y"] ** 2 + tangent["z"] ** 2)
            tangent = _geometry_unit(tangent) if tangent_norm > 1e-9 else jitter_vectors[index]
            next_position = {
                "x": float(position["x"]) + 0.075 * repulsion_x + 0.018 * tangent["x"] + 0.014 * ux * crowd_push,
                "y": float(position["y"]) + 0.075 * repulsion_y + 0.018 * tangent["y"] + 0.014 * uy * crowd_push,
                "z": float(position["z"]) + 0.075 * repulsion_z + 0.018 * tangent["z"] + 0.014 * uz * crowd_push,
            }
            updated_positions[index] = _geometry_normalize_to_radius(next_position, target_radius)
        positions.update(updated_positions)

        for bucket_indices in bucket_counts.values():
            if len(bucket_indices) <= 6:
                continue
            centroid = {
                "x": sum(positions[index]["x"] for index in bucket_indices) / len(bucket_indices),
                "y": sum(positions[index]["y"] for index in bucket_indices) / len(bucket_indices),
                "z": sum(positions[index]["z"] for index in bucket_indices) / len(bucket_indices),
            }
            radial_unit = _geometry_unit(centroid)
            tangent_a = _geometry_cross(radial_unit, {"x": 0.0, "y": 0.0, "z": 1.0})
            tangent_norm = math.sqrt(tangent_a["x"] ** 2 + tangent_a["y"] ** 2 + tangent_a["z"] ** 2)
            if tangent_norm <= 1e-9:
                tangent_a = _geometry_cross(radial_unit, {"x": 0.0, "y": 1.0, "z": 0.0})
            tangent_a = _geometry_unit(tangent_a)
            tangent_b = _geometry_unit(_geometry_cross(radial_unit, tangent_a))
            for offset, index in enumerate(sorted(bucket_indices, key=lambda item: str(rebalanced[item].get("id") or item))):
                angle = (2.0 * math.pi * offset) / max(1, len(bucket_indices))
                ring_radius = 0.032 + min(0.11, 0.005 * len(bucket_indices))
                radius = min(0.96, _radial_band_target_live(rebalanced[index], target_radii[index], float(len(bucket_indices) - 4)) + 0.004 * offset)
                displaced = {
                    "x": radial_unit["x"] * radius + ring_radius * (math.cos(angle) * tangent_a["x"] + math.sin(angle) * tangent_b["x"]),
                    "y": radial_unit["y"] * radius + ring_radius * (math.cos(angle) * tangent_a["y"] + math.sin(angle) * tangent_b["y"]),
                    "z": radial_unit["z"] * radius + ring_radius * (math.cos(angle) * tangent_a["z"] + math.sin(angle) * tangent_b["z"]),
                }
                positions[index] = _geometry_normalize_to_radius(displaced, radius)

    for index in active_indices:
        rebalanced[index]["final_position"] = positions[index]
    return rebalanced


def _default_local_correction_plan(
    *,
    raw_text: str,
    input_mode: str,
    nearby_context: dict[str, Any],
    memory_type: str,
    guide_area: str | None,
    existing_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    plan = dict(existing_plan or {})
    nearby_nodes = list((nearby_context or {}).get("nearby_nodes") or [])
    anchor_ids = [str(node.get("node_id")) for node in nearby_nodes[:3] if node.get("node_id")]
    repel_ids = [str(node.get("node_id")) for node in nearby_nodes[3:6] if node.get("node_id")]
    target_band = str(((plan.get("radial_policy") or {}) if isinstance(plan.get("radial_policy"), dict) else {}).get("target_band") or _infer_radial_band(memory_type, guide_area, input_mode=input_mode))
    band_bias = float(((plan.get("radial_policy") or {}) if isinstance(plan.get("radial_policy"), dict) else {}).get("band_bias") or 0.62)
    cluster_pull_strength = float(plan.get("cluster_pull_strength") or (0.55 if memory_type in {"identity", "value", "project"} else 0.36))
    minimum_separation = float(plan.get("minimum_separation") or (0.06 if memory_type in {"identity", "project"} else 0.045))
    return {
        "mobility": max(0.0, min(1.0, float(plan.get("mobility") or 0.45))),
        "max_shift": max(0.02, min(0.18, float(plan.get("max_shift") or 0.08))),
        "minimum_separation": max(0.025, min(0.12, minimum_separation)),
        "cluster_pull_strength": max(0.0, min(1.0, cluster_pull_strength)),
        "anchor_node_ids": list(dict.fromkeys(plan.get("anchor_node_ids") or anchor_ids)),
        "repel_node_ids": list(dict.fromkeys(plan.get("repel_node_ids") or repel_ids)),
        "attraction_targets": list(plan.get("attraction_targets") or [{"node_id": node_id, "weight": 0.55} for node_id in anchor_ids[:2]]),
        "repulsion_targets": list(plan.get("repulsion_targets") or [{"node_id": node_id, "weight": 0.35} for node_id in repel_ids[:2]]),
        "radial_policy": {
            "target_band": target_band if target_band in {"core", "inner", "mid", "outer"} else "mid",
            "band_bias": max(0.0, min(1.0, band_bias)),
            "reason": str(((plan.get("radial_policy") or {}) if isinstance(plan.get("radial_policy"), dict) else {}).get("reason") or "compiler_stabilized"),
        },
        "facet_bias": {
            "x": float(((plan.get("facet_bias") or {}) if isinstance(plan.get("facet_bias"), dict) else {}).get("x") or 0.0),
            "y": float(((plan.get("facet_bias") or {}) if isinstance(plan.get("facet_bias"), dict) else {}).get("y") or 0.0),
            "z": float(((plan.get("facet_bias") or {}) if isinstance(plan.get("facet_bias"), dict) else {}).get("z") or 0.0),
        },
    }


def resolve_identity_reference(text: str, identity_nucleus: dict[str, Any], graph: dict[str, Any]) -> dict[str, Any] | None:
    lowered = text.lower()
    self_keys = [*list(identity_nucleus.get("self_name_candidates") or []), *list(identity_nucleus.get("aliases") or [])]
    for name in self_keys:
        if name and name.lower() in lowered:
            target = next(
                (
                    node
                    for node in graph.get("nodes", [])
                    if name.lower() in str(node.get("raw_text") or "").lower()
                    and str(node.get("memory_type") or "") in {"identity", "relational", "value"}
                ),
                None,
            )
            if not target:
                fallback_id = identity_nucleus.get("primary_self_node_id")
                target = next((node for node in graph.get("nodes", []) if str(node.get("id")) == str(fallback_id)), None)
            return {
                "resolution_type": "self_reference",
                "resolved_node_id": str(target.get("id")) if target else None,
                "confidence": 0.9 if target else 0.78,
                "reason": f"Matched self-name candidate `{name}`",
            }
    return None


def nearby_context_for_seed(
    text: str,
    input_mode: str,
    graph: dict[str, Any],
    index_payload: dict[str, Any],
    atlas_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    draft = heuristic_projection(text, input_mode=input_mode)
    draft_scores = draft["routing_semantic_scores"]
    latent = scores_to_latent_vector(draft_scores)
    angles = latent_vector_to_angles(latent)
    routing_brainhex = quantize_to_brainhex(
        angles["theta"],
        angles["phi"],
        compute_radius_value(
            draft_scores,
            draft["routing_facets"],
            is_summary=draft["is_summary"],
            is_document_anchor=input_mode == "document",
            granularity=draft["granularity"],
            novelty=draft["novelty"],
        ),
    )
    base_position = brainhex_to_position(routing_brainhex)
    spatial_index = dict(index_payload.get("spatial_index") or {})
    nearby = []
    for node in spatial_index.values():
        fit = distance(base_position, dict(node["final_position"]))
        nearby.append(
            {
                "node_id": str(node["id"]),
                "summary": str(node["summary"]),
                "memory_type": str(node["memory_type"]),
                "guide_area": str((node.get("provenance") or {}).get("guide_conceptual_area") or ""),
                "bucket_key": str((node.get("bucket") or {}).get("key") or ""),
                "topology_color": str((node.get("topology_color") or {}).get("hex") or ""),
                "document_anchor": bool(node.get("is_document_anchor")),
                "distance": round(fit, 4),
            }
        )
    nearby.sort(key=lambda item: (item["distance"], item["node_id"]))
    shortlist = shortlist_atlas_buckets(atlas_payload or {"buckets": [], "node_count": 0}, base_position) if atlas_payload else []
    return {
        "draft_base_position": base_position,
        "nearby_nodes": nearby[:8],
        "atlas_shortlist": [
            {
                "bucket_key": bucket["bucket_key"],
                "node_count": int(bucket.get("node_count") or 0),
                "guide_area_histogram": dict(bucket.get("guide_area_histogram") or {}),
                "fit_score": float(bucket.get("fit_score") or 0.0),
            }
            for bucket in shortlist[:4]
        ],
    }


def span_bounds(source_text: str, fragment: str) -> tuple[int | None, int | None]:
    if not fragment:
        return None, None
    lower_source = source_text.lower()
    return _span_bounds_in_lower_source(lower_source, fragment)


def _span_bounds_in_lower_source(lower_source: str, fragment: str) -> tuple[int | None, int | None]:
    if not fragment:
        return None, None
    lower_fragment = fragment.lower()
    start = lower_source.find(lower_fragment)
    if start < 0:
        return None, None
    return start, start + len(fragment)


def build_seed(
    *,
    raw_text: str,
    input_mode: str,
    provenance_mode: str,
    source_label: str | None = None,
    source_type: str | None = None,
    source_uri: str | None = None,
    source_ref_id: str | None = None,
    source_trust: str | None = None,
    claim_status: str | None = None,
    node_kind_hint: str | None = None,
    summary_override: str | None = None,
    memory_type_override: str | None = None,
    guide_area_override: str | None = None,
    derivation_role: str | None = None,
    derivation_confidence: float | None = None,
    derived_from_preview_id: str | None = None,
    document_role: str | None = None,
    document_anchor_id: str | None = None,
    document_chunk_index: int | None = None,
    source_unit_id: str | None = None,
    source_unit_title: str | None = None,
    source_unit_kind: str | None = None,
    source_unit_role: str | None = None,
    promotion_role: str | None = None,
    source_unit_formation_strategy: str | None = None,
    source_span_start: int | None = None,
    source_span_end: int | None = None,
    routing_scores_override: dict[str, float] | None = None,
    routing_facets_override: dict[str, float] | None = None,
    granularity_override: float | None = None,
    novelty_override: float | None = None,
    memory_confidence: float | None = None,
    identity_resolution_confidence: float | None = None,
    evidence_confidence: float | None = None,
    stability_confidence: float | None = None,
    local_correction_plan: dict[str, Any] | None = None,
    suggested_links: list[dict[str, Any]] | None = None,
    suggested_highways: list[dict[str, Any]] | None = None,
    retrieval_affordance: dict[str, Any] | None = None,
    retrieval_aliases: list[str] | None = None,
    persist_mode: str = "create",
    merge_target_node_id: str | None = None,
    identity_resolution_target_node_id: str | None = None,
    identity_resolution_type: str | None = None,
    geometry_profile_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    projection = heuristic_projection(raw_text, input_mode=input_mode, node_kind_hint=node_kind_hint)
    routing_scores = normalize_scores(routing_scores_override or projection["routing_semantic_scores"], ROUTING_FIELDS)
    routing_facets = normalize_scores(routing_facets_override or projection["routing_facets"], FACET_FIELDS)
    routing_latent = scores_to_latent_vector(routing_scores)
    routing_angles = latent_vector_to_angles(routing_latent)
    routing_brainhex = quantize_to_brainhex(
        routing_angles["theta"],
        routing_angles["phi"],
        compute_radius_value(
            routing_scores,
            routing_facets,
            is_summary=projection["is_summary"],
            is_document_anchor=input_mode == "document",
            granularity=granularity_override if granularity_override is not None else projection["granularity"],
            novelty=novelty_override if novelty_override is not None else projection["novelty"],
            radial_policy=dict((local_correction_plan or {}).get("radial_policy") or {}),
        ),
    )
    semantic_color = color_from_brainhex(routing_brainhex)
    base_position = brainhex_to_position(routing_brainhex)
    explicit_memory_type = str(memory_type_override or "").strip().lower().replace(" ", "_")
    explicit_document_memory = explicit_memory_type in {"document_chunk", "document_summary", "document_fact"}
    runtime_memory_type = map_runtime_memory_type(
        memory_type_override or projection["memory_type"],
        claim_type=None if explicit_document_memory else node_kind_hint if node_kind_hint in CLAIM_TYPES else None,
        entity_type=None if explicit_document_memory else node_kind_hint if node_kind_hint in ENTITY_TYPES else None,
    )
    if input_mode == "document":
        runtime_memory_type = "document_anchor"
    guide_conceptual_area = guide_area_override or projection["expected_guide_area"] or infer_guide_area(raw_text)
    if input_mode == "document" and runtime_memory_type == "document_anchor":
        guide_conceptual_area = "Media Signals"
    resolved_source_type = source_type or ("document" if input_mode == "document" else "manual_text")
    provenance = {
        "mode": provenance_mode,
        "source_label": source_label,
        "source_type": resolved_source_type,
        "source_uri": sanitize_source_uri_for_persistence(source_uri),
        "source_ref_id": str(source_ref_id or "").strip() or None,
        "guide_conceptual_area": guide_conceptual_area,
    }
    hygiene = build_hygiene_metadata(
        raw_text=raw_text,
        input_mode=input_mode,
        provenance=provenance,
        explicit_source_trust=source_trust,
        explicit_claim_status=claim_status,
        memory_type=runtime_memory_type,
        derivation_role=derivation_role,
        document_role=document_role,
        is_document_anchor=input_mode == "document",
    )
    seed = {
        "node_kind": node_kind_hint or projection["node_kind"],
        "memory_type": runtime_memory_type,
        "raw_text": raw_text,
        "summary": summary_override or projection["summary"],
        "routing_semantic_scores": routing_scores,
        "routing_facets": routing_facets,
        "routing_brainhex": routing_brainhex,
        "semantic_color": semantic_color,
        "base_position": base_position,
        "is_document_anchor": input_mode == "document",
        "is_summary": bool(projection["is_summary"]),
        "granularity": float(granularity_override if granularity_override is not None else projection["granularity"]),
        "novelty": float(novelty_override if novelty_override is not None else projection["novelty"]),
        "provenance": provenance,
        "source_trust": hygiene["source_trust"],
        "claim_status": hygiene["claim_status"],
        "answer_eligible": hygiene["answer_eligible"],
        "profile_eligible": hygiene["profile_eligible"],
        "document_eligible": hygiene["document_eligible"],
        "derivation_role": derivation_role,
        "derivation_confidence": derivation_confidence,
        "derived_from_preview_id": derived_from_preview_id,
        "document_role": document_role,
        "document_anchor_id": document_anchor_id,
        "document_chunk_index": document_chunk_index,
        "source_unit_id": source_unit_id,
        "source_unit_title": source_unit_title,
        "source_unit_kind": source_unit_kind,
        "source_unit_role": source_unit_role,
        "promotion_role": promotion_role,
        "source_unit_formation_strategy": source_unit_formation_strategy,
        "source_span_start": source_span_start,
        "source_span_end": source_span_end,
        "memory_confidence": memory_confidence,
        "identity_resolution_confidence": identity_resolution_confidence,
        "evidence_confidence": evidence_confidence,
        "stability_confidence": stability_confidence,
        "sleep_revision_count": 0,
        "last_sleep_review_at": None,
        "local_correction_plan": dict(local_correction_plan or {}),
        "suggested_links": list(suggested_links or []),
        "suggested_highways": list(suggested_highways or []),
        "retrieval_affordance": dict(retrieval_affordance or {}),
        "retrieval_aliases": _unique_strings(
            [str(item).strip() for item in list(retrieval_aliases or []) if str(item).strip()],
            limit=12,
        ),
        "persist_mode": persist_mode,
        "merge_target_node_id": merge_target_node_id,
        "identity_resolution_target_node_id": identity_resolution_target_node_id,
        "identity_resolution_type": identity_resolution_type,
    }
    return apply_public_v1_geometry_profile_to_seed(seed, geometry_profile_context)


def classify_claim_type(text: str) -> str:
    lowered = text.lower()
    if re.search(r"\b(?:i am|we are|sono|siamo)\s+(?:building|building|costruendo|sviluppando|creating|creando)\b", lowered):
        return "project_claim"
    if any(token in lowered for token in ("my identity", "i tend", "i see myself", "i describe myself", "mi descrivo", "la mia identita")):
        return "identity_claim"
    if re.search(
        r"\b(?:is|was|are|were|e|è|era|sono)\s+(?:a|an|the|un|una|il|la)?\s*"
        r"(?:self[- ]taught\s+)?(?:founder|co[- ]founder|chief executive|ceo|entrepreneur|executive|leader|engineer|coder|"
        r"fondatore|cofondatore|imprenditore|dirigente|amministratore)\b",
        lowered,
    ):
        return "identity_claim"
    if re.search(r"\b(?:founder|co[- ]founder|ceo|chief executive|fondatore|cofondatore|imprenditore)\s+(?:of|di|del|della|at|in|inside)\b", lowered):
        return "identity_claim"
    if any(
        token in lowered
        for token in (
            "i care",
            "important",
            "matters",
            "principle",
            "prefer",
            "shaped by",
            "guided by",
            "sustainability",
            "responsibility",
            "technology must serve people",
            "valori",
            "responsabilita",
            "sostenibilita",
        )
    ):
        return "value_claim"
    if any(token in lowered for token in ("tone", "style", "voice", "structured", "direct", "analytical")):
        return "style_claim"
    if any(token in lowered for token in ("project", "startup", "product", "roadmap", "build", "system", "company", "azienda", "societ", "software", "platform", "solution")):
        return "project_claim"
    if any(token in lowered for token in ("collaborator", "partner", "customer", "team", "family", "relationship")):
        return "relationship_claim"
    if any(token in lowered for token in ("sold", "founded", "started", "happened", "acquired")) or re.search(r"\b(19|20)\d{2}\b", lowered):
        return "event_claim"
    return "fact"


def classify_entity_type(name: str, source_text: str, input_mode: str) -> str:
    lowered = source_text.lower()
    folded_name = _fold_identity_text(name)
    name_tokens = [token for token in re.findall(r"[a-z0-9]+", folded_name) if token]
    local_context = lowered
    if folded_name and folded_name in lowered:
        position = lowered.find(folded_name)
        local_context = lowered[max(0, position - 90) : position + len(folded_name) + 90]
    organization_markers = (
        "company",
        "organization",
        "startup",
        "azienda",
        "corporation",
        "university",
        "universita",
        "studio",
        "foundation",
        "foundry",
        "electric",
    )
    project_markers = (
        "project",
        "product",
        "system",
        "roadmap",
        "platform",
        "controller",
        "software",
        "solution",
        "protocol",
        "tool",
        "progetto",
        "prodotto",
    )
    person_markers = ("ceo", "founder", "founded", "founded by", "dr.", "mr.", "ms.", "professor", "self-taught", "born")
    relationship_person_markers = (
        "partner",
        "collaborator",
        "colleague",
        "friend",
        "father",
        "mother",
        "brother",
        "sister",
        "padre",
        "madre",
        "fratello",
        "sorella",
    )
    organization_name_markers = {"corporation", "corp", "inc", "ltd", "university", "universita", "studio", "foundation", "foundry", "electric"}
    if input_mode == "document" and any(token in organization_name_markers for token in name_tokens):
        return "organization"
    if input_mode == "document" and " " in name and len(name_tokens) in {2, 3} and any(token in local_context for token in person_markers):
        return "person"
    if " " in name and len(name_tokens) in {2, 3} and any(token in local_context for token in relationship_person_markers):
        return "person"
    if input_mode == "document" and any(token in local_context for token in project_markers):
        return "project"
    if input_mode == "document" and any(token in local_context for token in organization_markers):
        return "organization"
    if any(token in local_context for token in project_markers):
        return "project"
    if any(token in local_context for token in organization_markers):
        return "organization"
    if " " in name and len(name.split()) <= 3:
        return "person"
    if input_mode == "document":
        if any(char.isupper() for char in str(name)[1:]) or str(name).isupper():
            return "project"
        return "organization"
    return "organization"


def heuristic_claims(text: str) -> list[dict[str, Any]]:
    chunks = [part.strip() for part in re.split(r"[.!?]+|\n+", text) if part.strip()]
    claims: list[dict[str, Any]] = []
    normalized_source = normalize_preview_text(text)
    for chunk in chunks:
        clauses = [part.strip(" ,;") for part in re.split(r"\s+(?:and|e)\s+", chunk) if part.strip(" ,;")]
        for clause in clauses:
            if len(clause.split()) < 4:
                continue
            if normalize_preview_text(clause) == normalized_source:
                continue
            claim_type = classify_claim_type(clause)
            confidence = 0.74
            if claim_type in {"event_claim", "relationship_claim"}:
                confidence = 0.82
            elif claim_type in {"style_claim", "value_claim"}:
                confidence = 0.78
            start, end = span_bounds(text, clause)
            claims.append(
                {
                    "text": summarize_text(clause, limit=120),
                    "claim_type": claim_type,
                    "confidence": confidence,
                    "source_span_start": start,
                    "source_span_end": end,
                }
            )
    dedup: list[dict[str, Any]] = []
    seen: set[str] = set()
    for claim in claims:
        key = claim["text"].lower()
        if key in seen:
            continue
        seen.add(key)
        dedup.append(claim)
    return dedup[:8]


def heuristic_entities(text: str, input_mode: str) -> list[dict[str, Any]]:
    stopwords = {"I", "My", "We", "The", "This", "That", "It", "When", "For", "And", "Documento", "Mi", "Sono", "Parlo", "Costruisco", "Lavoro", "Voglio", "Cerco", "Nel", "Nella", "Per"}
    verb_like_leads = {"Sono", "Parlo", "Costruisco", "Lavoro", "Voglio", "Cerco", "Penso", "Credo", "Uso", "Seguo"}
    pattern = r"\b(?:[A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+)*)\b"
    matches = []
    for match in re.finditer(pattern, text):
        value = match.group(0).strip()
        if value in stopwords:
            continue
        if len(value) < 3:
            continue
        parts = value.split()
        is_acronym = value.isupper() and len(value) >= 2
        if not is_acronym and len(parts) < 2:
            continue
        if parts and parts[0] in verb_like_leads:
            continue
        matches.append((value, match.start(), match.end()))
    dedup: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value, start, end in matches:
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        entity_type = classify_entity_type(value, text, input_mode)
        confidence = 0.86 if entity_type in {"person", "organization"} else 0.8
        dedup.append(
            {
                "text": value,
                "entity_type": entity_type,
                "confidence": confidence,
                "source_span_start": start,
                "source_span_end": end,
            }
        )
    if input_mode == "document":
        title = summarize_text(text.splitlines()[0] if text.splitlines() else text, limit=56)
        start, end = span_bounds(text, title)
        dedup.insert(
            0,
            {
                "text": title,
                "entity_type": "document",
                "confidence": 0.92,
                "source_span_start": start,
                "source_span_end": end,
            },
        )
    return dedup[:8]


def llm_autoderive(text: str, input_mode: str, *, timeout_seconds: float | None = None) -> tuple[dict[str, Any] | None, str | None]:
    if not llm_enabled():
        return None, "llm_disabled"
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["claims", "entities"],
        "properties": {
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["text", "claim_type", "confidence"],
                    "properties": {
                        "text": {"type": "string"},
                        "claim_type": {"type": "string", "enum": sorted(CLAIM_TYPES)},
                        "confidence": {"type": "number"},
                    },
                },
            },
            "entities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["text", "entity_type", "confidence", "mentioned_in_claim_indexes"],
                    "properties": {
                        "text": {"type": "string"},
                        "entity_type": {"type": "string", "enum": sorted(ENTITY_TYPES)},
                        "confidence": {"type": "number"},
                        "mentioned_in_claim_indexes": {
                            "type": "array",
                            "items": {"type": "integer"},
                        },
                    },
                },
            },
        },
    }
    system_prompt = (
        "You are extracting AGVM preview derivations from one memory text. "
        "Return concise claim nodes and entity nodes only. Do not invent facts. "
        "Claims should be short atomic statements. Entities should be direct mentions."
    )
    payload, error = structured_json(
        model=compiler_model(),
        system_prompt=system_prompt,
        user_prompt=f"Input mode: {input_mode}\n\n{text}",
        schema_name="agvm_preview_derivation",
        schema=schema,
        timeout=max(1.0, min(float(timeout_seconds or 20.0), 60.0)),
        role="compiler",
    )
    return payload, error


def derive_structures(
    text: str,
    input_mode: str,
    *,
    llm_timeout_seconds: float | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, list[dict[str, str]]]:
    warnings: list[dict[str, str]] = []
    normalized_source = normalize_preview_text(text)
    payload, error = llm_autoderive(text, input_mode, timeout_seconds=llm_timeout_seconds)
    if payload:
        claims: list[dict[str, Any]] = []
        for item in list(payload.get("claims") or [])[:8]:
            claim_text = summarize_text(str(item.get("text") or "").strip(), limit=120)
            if not claim_text:
                continue
            if normalize_preview_text(claim_text) == normalized_source:
                continue
            start, end = span_bounds(text, claim_text)
            claims.append(
                {
                    "text": claim_text,
                    "claim_type": item.get("claim_type") if item.get("claim_type") in CLAIM_TYPES else classify_claim_type(claim_text),
                    "confidence": max(0.0, min(1.0, float(item.get("confidence") or 0.75))),
                    "source_span_start": start,
                    "source_span_end": end,
                }
            )
        entities: list[dict[str, Any]] = []
        for item in list(payload.get("entities") or [])[:8]:
            entity_text = summarize_text(str(item.get("text") or "").strip(), limit=72)
            if not entity_text:
                continue
            start, end = span_bounds(text, entity_text)
            entities.append(
                {
                    "text": entity_text,
                    "entity_type": item.get("entity_type") if item.get("entity_type") in ENTITY_TYPES else classify_entity_type(entity_text, text, input_mode),
                    "confidence": max(0.0, min(1.0, float(item.get("confidence") or 0.8))),
                    "source_span_start": start,
                    "source_span_end": end,
                    "mentioned_in_claim_indexes": [int(idx) for idx in list(item.get("mentioned_in_claim_indexes") or []) if int(idx) >= 0],
                }
            )
        return claims, entities, "llm", warnings

    if error and error != "llm_disabled":
        warnings.append({"code": "llm_fallback", "message": f"LLM unavailable, fallback to heuristic derivation: {error}"})
    claims = heuristic_claims(text)
    entities = heuristic_entities(text, input_mode)
    return claims, entities, "heuristic", warnings


def llm_memory_compile(
    text: str,
    input_mode: str,
    *,
    identity_nucleus: dict[str, Any],
    nearby_context: dict[str, Any],
    source_context: dict[str, Any] | None = None,
    timeout_seconds: float | None = None,
    api_key_override: str | None = None,
    model_override: str | None = None,
    execution_metadata: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    if api_key_override is None and not llm_enabled():
        return None, "llm_disabled"
    metamemory = build_metamemory_package("compiler")
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "primary_node",
            "derived_nodes",
            "merge_decisions",
            "identity_resolution_decisions",
            "local_correction_plan",
            "links_to_create",
            "highways_to_create",
            "cognitive_write_plan",
        ],
        "properties": {
            "primary_node": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "summary",
                    "memory_type",
                    "guide_area",
                    "routing_semantic_scores",
                    "routing_facets",
                    "granularity",
                    "novelty",
                    "memory_confidence",
                    "evidence_confidence",
                    "stability_confidence",
                ],
                "properties": {
                    "summary": {"type": "string"},
                    "memory_type": {"type": "string"},
                    "guide_area": {"type": ["string", "null"]},
                    "routing_semantic_scores": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ROUTING_FIELDS,
                        "properties": {field: {"type": "number"} for field in ROUTING_FIELDS},
                    },
                    "routing_facets": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": FACET_FIELDS,
                        "properties": {field: {"type": "number"} for field in FACET_FIELDS},
                    },
                    "granularity": {"type": "number"},
                    "novelty": {"type": "number"},
                    "memory_confidence": {"type": "number"},
                    "evidence_confidence": {"type": "number"},
                    "stability_confidence": {"type": "number"},
                },
            },
            "derived_nodes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "derivation_role",
                        "raw_text",
                        "summary",
                        "memory_type",
                        "routing_semantic_scores",
                        "routing_facets",
                        "confidence",
                        "memory_confidence",
                        "evidence_confidence",
                    ],
                    "properties": {
                        "derivation_role": {"type": "string", "enum": ["claim", "entity"]},
                        "claim_type": {"type": ["string", "null"]},
                        "entity_type": {"type": ["string", "null"]},
                        "raw_text": {"type": "string"},
                        "summary": {"type": "string"},
                        "memory_type": {"type": "string"},
                        "routing_semantic_scores": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ROUTING_FIELDS,
                            "properties": {field: {"type": "number"} for field in ROUTING_FIELDS},
                        },
                        "routing_facets": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": FACET_FIELDS,
                            "properties": {field: {"type": "number"} for field in FACET_FIELDS},
                        },
                        "confidence": {"type": "number"},
                        "memory_confidence": {"type": "number"},
                        "evidence_confidence": {"type": "number"},
                        "identity_resolution_confidence": {"type": ["number", "null"]},
                        "stability_confidence": {"type": ["number", "null"]},
                    },
                },
            },
            "merge_decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["source_text", "decision", "confidence", "reason"],
                    "properties": {
                        "source_text": {"type": "string"},
                        "decision": {"type": "string", "enum": ["new_node", "merge_into_existing", "attach_as_alias_or_variant"]},
                        "target_node_id": {"type": ["string", "null"]},
                        "confidence": {"type": "number"},
                        "reason": {"type": "string"},
                    },
                },
            },
            "identity_resolution_decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["source_text", "resolution_type", "confidence", "reason"],
                    "properties": {
                        "source_text": {"type": "string"},
                        "resolution_type": {"type": "string"},
                        "resolved_node_id": {"type": ["string", "null"]},
                        "confidence": {"type": "number"},
                        "reason": {"type": "string"},
                    },
                },
            },
            "local_correction_plan": {
                "type": "object",
                "additionalProperties": False,
                "required": ["mobility", "max_shift", "anchor_node_ids", "repel_node_ids", "minimum_separation"],
                "properties": {
                    "mobility": {"type": "number"},
                    "max_shift": {"type": "number"},
                    "minimum_separation": {"type": "number"},
                    "cluster_pull_strength": {"type": ["number", "null"]},
                    "anchor_node_ids": {"type": "array", "items": {"type": "string"}},
                    "repel_node_ids": {"type": "array", "items": {"type": "string"}},
                    "attraction_targets": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["node_id", "weight"],
                            "properties": {
                                "node_id": {"type": "string"},
                                "weight": {"type": "number"},
                            },
                        },
                    },
                    "repulsion_targets": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["node_id", "weight"],
                            "properties": {
                                "node_id": {"type": "string"},
                                "weight": {"type": "number"},
                            },
                        },
                    },
                    "radial_policy": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["target_band", "band_bias"],
                        "properties": {
                            "target_band": {"type": "string", "enum": ["core", "inner", "mid", "outer"]},
                            "band_bias": {"type": "number"},
                            "reason": {"type": ["string", "null"]},
                        },
                    },
                    "facet_bias": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "x": {"type": "number"},
                            "y": {"type": "number"},
                            "z": {"type": "number"},
                        },
                    },
                },
            },
            "links_to_create": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["source_text", "target_node_id", "strength", "reason"],
                    "properties": {
                        "source_text": {"type": "string"},
                        "target_node_id": {"type": "string"},
                        "strength": {"type": "number"},
                        "reason": {"type": "string"},
                    },
                },
            },
            "highways_to_create": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["source_text", "target_node_id", "strength", "reason"],
                    "properties": {
                        "source_text": {"type": "string"},
                        "target_node_id": {"type": "string"},
                        "strength": {"type": "number"},
                        "reason": {"type": "string"},
                        "kind": {"type": ["string", "null"]},
                        "stability": {"type": ["number", "null"]},
                    },
                },
            },
            "cognitive_write_plan": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "dominant_memory_acts",
                    "requires_human_review",
                    "review_reasons",
                    "clarification_questions",
                    "hypotheses",
                    "deductions",
                    "state_transitions",
                    "sleep_evolve_targets",
                    "mutation_policy",
                ],
                "properties": {
                    "dominant_memory_acts": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": sorted(_COGNITIVE_MEMORY_ACT_TYPES),
                        },
                    },
                    "requires_human_review": {"type": "boolean"},
                    "review_reasons": {"type": "array", "items": {"type": "string"}},
                    "clarification_questions": {"type": "array", "items": {"type": "string"}},
                    "hypotheses": {"type": "array", "items": {"type": "string"}},
                    "deductions": {"type": "array", "items": {"type": "string"}},
                    "state_transitions": {"type": "array", "items": {"type": "string"}},
                    "sleep_evolve_targets": {"type": "array", "items": {"type": "string"}},
                    "mutation_policy": {
                        "type": "string",
                        "enum": ["safe_to_persist", "review_before_persist", "ask_clarification", "suppress_non_personal"],
                    },
                },
            },
        },
    }
    system_prompt = (
        "You are the AGVM memory compiler.\n\n"
        f"{metamemory}\n\n"
        "Compile the source into AGVM memory objects in one single pass. "
        "This is not a generic summarization task: decide the primary memory, the atomic derived memories, "
        "merge/new-node decisions, identity resolution, local correction policy, and candidate links/highways. "
        "Use the identity nucleus as mandatory context, and treat nearby context as a local brain neighborhood. "
        "If the source contains identity, work, events, style, or values together, split them into atomic memories. "
        "Do not emit decorative nodes. Do not invent facts not grounded in the source or provided context. "
        "Use confidence consistently and keep runtime memory types coherent. "
        "Every schema field must be populated: use null for nullable fields and [] for empty arrays. "
        "Do not return flat or uniform semantic distributions. Distinguish the dominant routing scores and the dominant facets. "
        "The local_correction_plan must include a meaningful radial_policy, attraction/repulsion targets when useful, and a realistic minimum_separation. "
        "Fill cognitive_write_plan with the intended memory acts, review policy, hypotheses, deductions, state transitions, and sleep/evolve targets. "
        "Keep facts, deductions, hypotheses, documents, relationship states, project states, and source metadata separate.\n\n"
        "Source-learning extraction policy:\n"
        "- If the source context says self_memory, compile the material as memory about the selected brain/person, not as a generic web-page collage.\n"
        "- Extract what a future agent would need to answer: identity, values, operating style, projects, relationships, dated events, decisions, ambitions, and grounded implications.\n"
        "- Split semantically different claims even when they appear in one paragraph.\n"
        "- Do not promote page titles, navigation text, slogans, SEO snippets, headings, or raw metadata into memories unless they support a concrete grounded claim.\n"
        "- When the source supports an implication but not a direct fact, emit it as a hypothesis/deduction in cognitive_write_plan instead of pretending certainty."
    )
    user_prompt = (
        f"Input mode: {input_mode}\n"
        f"Source text:\n{text}\n\n"
        f"Metamemory role: compiler\n\n"
        f"Source context:\n{source_context or {}}\n\n"
        f"Identity nucleus:\n{identity_nucleus}\n\n"
        f"Nearby context:\n{nearby_context}\n"
    )
    payload, error = structured_json(
        model=model_override or compiler_model(),
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        schema_name="agvm_memory_compiler",
        schema=schema,
        timeout=max(1.0, min(float(timeout_seconds or 60.0), 120.0)),
        role="compiler",
        api_key_override=api_key_override,
        execution_metadata=execution_metadata,
    )
    return payload, error


def _compact_source_sections_for_semantic_compiler(
    source_sections: list[dict[str, Any]] | None,
    *,
    max_sections: int = 36,
    per_section_chars: int = 1200,
    total_chars: int = 24_000,
) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    char_budget = max(0, int(total_chars))
    for raw_section in [dict(item) for item in list(source_sections or []) if isinstance(item, dict)]:
        if len(compact) >= max_sections or char_budget <= 0:
            break
        section_id = str(raw_section.get("section_id") or raw_section.get("unit_id") or "").strip()
        text = re.sub(r"\s+", " ", str(raw_section.get("text") or "").replace("\r", "\n")).strip()
        if not section_id or len(text) < 24:
            continue
        clipped = text[: min(len(text), per_section_chars, char_budget)].strip()
        if not clipped:
            continue
        char_budget -= len(clipped)
        compact.append(
            {
                "source_unit_id": section_id,
                "title": str(raw_section.get("title") or section_id).strip()[:240],
                "kind": str(raw_section.get("kind") or "").strip(),
                "source_uri": str(raw_section.get("source_uri") or "").strip(),
                "source_unit_role": str(raw_section.get("source_unit_role") or "").strip(),
                "promotion_role": str(raw_section.get("promotion_role") or "").strip(),
                "fact_eligible": bool(raw_section.get("fact_eligible") if raw_section.get("fact_eligible") is not None else True),
                "supporting_evidence_eligible": bool(raw_section.get("supporting_evidence_eligible") or False),
                "text": clipped,
            }
        )
    return compact


def llm_source_unit_semantic_compile(
    *,
    source_sections: list[dict[str, Any]] | None,
    source_label: str | None,
    source_type: str | None,
    source_trust: str | None,
    source_purpose: str | None,
    operator_instruction: str | None,
    identity_nucleus: dict[str, Any],
    nearby_context: dict[str, Any],
    source_unit_formation: dict[str, Any] | None = None,
    source_investigation_id: str | None = None,
    candidate_target: int | None = None,
    timeout_seconds: float | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, str | None]:
    sections = _compact_source_sections_for_semantic_compiler(source_sections)
    if not sections:
        return [], None, "no_source_sections"
    if not llm_enabled():
        return [], None, "llm_disabled"

    metamemory = build_metamemory_package("compiler")
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["strategy", "derived_nodes", "clarification_hints", "quality_notes"],
        "properties": {
            "strategy": {
                "type": "object",
                "additionalProperties": False,
                "required": ["source_understanding", "memory_extraction_goal", "deduction_policy", "duplicate_policy"],
                "properties": {
                    "source_understanding": {"type": "string"},
                    "memory_extraction_goal": {"type": "string"},
                    "deduction_policy": {"type": "string"},
                    "duplicate_policy": {"type": "string"},
                },
            },
            "derived_nodes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "source_unit_id",
                        "derivation_role",
                        "claim_type",
                        "memory_act_type",
                        "raw_text",
                        "summary",
                        "routing_semantic_scores",
                        "routing_facets",
                        "confidence",
                        "memory_confidence",
                        "evidence_confidence",
                        "stability_confidence",
                        "retrieval_aliases",
                        "reason",
                    ],
                    "properties": {
                        "source_unit_id": {"type": "string"},
                        "derivation_role": {"type": "string", "enum": ["claim"]},
                        "claim_type": {"type": "string", "enum": sorted(CLAIM_TYPES)},
                        "memory_act_type": {"type": "string", "enum": sorted(_COGNITIVE_MEMORY_ACT_TYPES)},
                        "raw_text": {"type": "string"},
                        "summary": {"type": "string"},
                        "routing_semantic_scores": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ROUTING_FIELDS,
                            "properties": {field: {"type": "number"} for field in ROUTING_FIELDS},
                        },
                        "routing_facets": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": FACET_FIELDS,
                            "properties": {field: {"type": "number"} for field in FACET_FIELDS},
                        },
                        "confidence": {"type": "number"},
                        "memory_confidence": {"type": "number"},
                        "evidence_confidence": {"type": "number"},
                        "stability_confidence": {"type": "number"},
                        "retrieval_aliases": {"type": "array", "items": {"type": "string"}},
                        "reason": {"type": "string"},
                    },
                },
            },
            "clarification_hints": {"type": "array", "items": {"type": "string"}},
            "quality_notes": {"type": "array", "items": {"type": "string"}},
        },
    }
    source_context = {
        "source_label": source_label,
        "source_type": source_type,
        "source_trust": source_trust,
        "source_purpose": source_purpose or "unknown",
        "operator_instruction": operator_instruction,
        "source_unit_formation": dict(source_unit_formation or {}),
        "candidate_target": max(1, min(int(candidate_target or 8), 48)),
    }
    system_prompt = (
        "You are the AGVM source-unit semantic compiler.\n\n"
        f"{metamemory}\n\n"
        "Your job is to transform traced source units into high-quality candidate memories while preserving provenance. "
        "You are not summarizing the website/document. You are deciding what the brain should learn from it.\n\n"
        "Extraction rules:\n"
        "- Emit only claims grounded in one listed source_unit_id.\n"
        "- Prefer semantically useful memories over raw page text: identity, values, operating style, projects, relationships, dates, decisions, motivations, and source-supported deductions.\n"
        "- If source_purpose is self_memory, rewrite first-person public material into memory about the selected brain/person while keeping the claim evidence-bound.\n"
        "- For self_memory, use first person when the source is first-person, or the selected person's name/neutral wording otherwise. Do not write candidate memories with he/she/his/her pronouns.\n"
        "- Keep one atomic idea per node. A good rich source should produce several distinct candidate nodes, not one mega paragraph.\n"
        "- Cover every fact-eligible source unit with at least one candidate whenever it contains usable evidence.\n"
        "- Assign all 12 routing_semantic_scores and all 12 routing_facets from the candidate meaning; these values drive placement and retrieval.\n"
        "- Candidate target is not a quota for hallucination, but if the source contains enough evidence you should approach it by splitting identity, project purpose, organizations, values, relationship/legacy, decision style, and future intention into separate nodes.\n"
        "- Never merge different cognitive categories into one node: a company list, a value statement, a family/legacy relation, and a decision-style statement must be separate candidates when supported.\n"
        "- For compact self-memory text with semicolons or multiple clauses, extract each supported clause as its own candidate instead of returning one combined summary.\n"
        "- Use value_claim/style_claim/project_claim/relationship_claim/event_claim/identity_claim when appropriate; do not collapse everything into fact.\n"
        "- Use create_deduction or create_hypothesis only when the source supports an implication but does not directly state it.\n"
        "- Never emit navigation, cookie text, SEO fragments, slogans without factual content, headings alone, source URLs, or title dumps as memory.\n"
        "- Include retrieval_aliases that a future agent could ask naturally in English and Italian.\n"
        "- If something may conflict with existing memory, keep it as a candidate and describe the issue in quality_notes or clarification_hints."
    )
    user_prompt = (
        f"Source context:\n{json.dumps(source_context, ensure_ascii=False, sort_keys=True)}\n\n"
        f"Candidate target: {source_context['candidate_target']} grounded memory candidates when the evidence supports that many.\n\n"
        f"Identity nucleus:\n{json.dumps(identity_nucleus, ensure_ascii=False, sort_keys=True, default=str)}\n\n"
        f"Nearby context sample:\n{json.dumps(nearby_context, ensure_ascii=False, sort_keys=True, default=str)[:6000]}\n\n"
        f"Traced source units:\n{json.dumps(sections, ensure_ascii=False, sort_keys=True)}"
    )
    payload, error = structured_json(
        model=compiler_model(),
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        schema_name="agvm_source_unit_semantic_compiler",
        schema=schema,
        timeout=max(1.0, min(float(timeout_seconds or 45.0), 120.0)),
        role="compiler",
        max_output_tokens=16000,
    )
    if not payload:
        return [], payload, error

    known_sections = {str(section.get("source_unit_id") or ""): section for section in sections}
    output: list[dict[str, Any]] = []
    seen: set[str] = set()

    def crosses_atomic_categories(candidate: str) -> bool:
        parts = [
            part.strip(" .;:-")
            for part in re.split(r";\s+|(?<=[.!?])\s+", str(candidate or ""))
            if len(part.strip(" .;:-")) >= 36
        ]
        if len(parts) < 2:
            return False
        categories = {classify_claim_type(part) for part in parts}
        categories.discard("fact")
        return len(categories) >= 2

    payload_items: list[dict[str, Any]] = []
    for raw_item in list(payload.get("derived_nodes") or [])[:96]:
        if isinstance(raw_item, dict):
            payload_items.extend(_source_atomized_item_variants(raw_item))

    for item in payload_items[:128]:
        source_unit_id = str(item.get("source_unit_id") or "").strip()
        section = known_sections.get(source_unit_id)
        if not section:
            continue
        claim_text = preserve_node_raw_text(_strip_generated_source_unit_prefix(str(item.get("raw_text") or item.get("summary") or "").strip()), limit=900)
        if len(claim_text) < 36:
            continue
        if crosses_atomic_categories(claim_text):
            continue
        if str(source_purpose or "").strip().lower() == "self_memory" and re.search(
            r"\b(?:he|him|his|himself|she|her|hers|herself)\b",
            claim_text,
            re.IGNORECASE,
        ):
            continue
        if not _source_has_memory_anchor(claim_text):
            continue
        section_evidence = "\n".join(
            [
                str(source_label or ""),
                str(section.get("title") or ""),
                str(section.get("text") or ""),
            ]
        ).strip()
        if _source_relation_drift_reasons(section_evidence, claim_text):
            continue
        grounding_assessment = _source_grounding_assessment(section_evidence, claim_text, role="claim")
        if not grounding_assessment["supported"]:
            if str(grounding_assessment.get("reason") or "") in {
                "candidate_introduces_named_tokens_absent_from_source",
                "candidate_introduces_gendered_pronouns_absent_from_source",
            }:
                continue
            if float(grounding_assessment.get("score") or 0.0) < 0.5:
                continue
        quality_reasons = _preview_source_quality_reasons(
            {
                "raw_text": claim_text,
                "summary": item.get("summary"),
                "document_role": "fact",
                "source_unit_id": source_unit_id,
            },
            source_scope=True,
        )
        hard_reasons = {
            "low_value_source_fact",
            "source_metadata_fragment",
            "fragmentary_bullet_or_heading_fact",
            "heading_or_title_not_memory_claim",
            "source_fact_missing_action",
        }
        if hard_reasons.intersection(quality_reasons):
            continue
        key = _source_grounding_fold(claim_text)
        if key in seen or any(existing and (existing in key or key in existing) for existing in seen):
            continue
        seen.add(key)
        claim_type = str(item.get("claim_type") or "")
        local_claim_type = _source_claim_type_for_source_fact(claim_text)
        if claim_type not in CLAIM_TYPES:
            claim_type = local_claim_type
        elif local_claim_type != "fact" and local_claim_type != claim_type:
            claim_type = local_claim_type
        confidence = max(0.0, min(1.0, float(item.get("confidence") or 0.78)))
        output.append(
            {
                "derivation_role": "claim",
                "claim_type": claim_type,
                "raw_text": claim_text,
                "summary": summarize_text(str(item.get("summary") or claim_text), limit=180),
                "memory_type": map_runtime_memory_type(None, claim_type=claim_type),
                "routing_semantic_scores": {
                    field: max(0.0, min(1.0, float(dict(item.get("routing_semantic_scores") or {})[field])))
                    for field in ROUTING_FIELDS
                },
                "routing_facets": {
                    field: max(0.0, min(1.0, float(dict(item.get("routing_facets") or {})[field])))
                    for field in FACET_FIELDS
                },
                "confidence": confidence,
                "memory_confidence": max(0.0, min(1.0, float(item.get("memory_confidence") or confidence))),
                "evidence_confidence": max(0.0, min(1.0, float(item.get("evidence_confidence") or confidence))),
                "stability_confidence": max(0.0, min(1.0, float(item.get("stability_confidence") or 0.72))),
                "selected_by_default_override": confidence >= 0.72,
                "document_role": "fact",
                "document_anchor_id": "preview_primary",
                "source_unit_id": source_unit_id,
                "source_unit_title": str(section.get("title") or source_unit_id),
                "source_unit_kind": str(section.get("kind") or ""),
                "source_unit_role": str(section.get("source_unit_role") or ""),
                "promotion_role": str(section.get("promotion_role") or ""),
                "source_unit_formation_strategy": None,
                "source_investigation_id": source_investigation_id,
                "source_grounding": {
                    "status": "supported" if grounding_assessment["supported"] else "weakly_supported",
                    "supported": True,
                    "score": round(float(grounding_assessment.get("score") or item.get("evidence_confidence") or confidence), 4),
                    "reason": str(grounding_assessment.get("reason") or "llm_semantic_source_unit_claim_with_explicit_provenance"),
                },
                "retrieval_affordance": {
                    "schema_version": "agvm.source_semantic_claim_retrieval_affordance.v1",
                    "question": (list(item.get("retrieval_aliases") or []) or _source_question_variants_for_claim(claim_text=claim_text, title=str(section.get("title") or "")))[0],
                    "answer_claim": claim_text,
                    "memory_act_type": str(item.get("memory_act_type") or ""),
                    "reason": str(item.get("reason") or ""),
                    "purpose": "semantic_memory_candidate_from_traced_source_unit",
                },
                "retrieval_aliases": _unique_strings(
                    [
                        *[str(alias).strip() for alias in list(item.get("retrieval_aliases") or []) if str(alias).strip()],
                        *_source_question_variants_for_claim(claim_text=claim_text, title=str(section.get("title") or ""), limit=4),
                    ],
                    limit=12,
                ),
            }
        )
    return output, payload, None


def heuristic_merge_decisions(
    source_texts: list[str],
    graph: dict[str, Any],
) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    normalized_graph = [
        (
            str(node.get("id")),
            normalize_preview_text(str(node.get("summary") or node.get("raw_text") or "")),
        )
        for node in graph.get("nodes", [])
    ]
    for text in source_texts:
        normalized = normalize_preview_text(text)
        for node_id, existing in normalized_graph:
            if normalized and existing and (normalized == existing or lexical_overlap(normalized, existing) >= 0.9):
                decisions.append(
                    {
                        "source_text": text,
                        "decision": "merge_into_existing",
                        "target_node_id": node_id,
                        "confidence": 0.84,
                        "reason": "high lexical overlap with existing node",
                    }
                )
                break
    return decisions


def heuristic_identity_decisions(
    source_texts: list[str],
    identity_nucleus: dict[str, Any],
    graph: dict[str, Any],
) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    for text in source_texts:
        resolution = resolve_identity_reference(text, identity_nucleus, graph)
        if resolution:
            decisions.append({"source_text": text, **resolution})
    return decisions


def _source_section_preview_items(
    *,
    source_text: str,
    input_mode: str,
    source_sections: list[dict[str, Any]] | None,
    source_unit_formation: dict[str, Any] | None = None,
    source_investigation_id: str | None = None,
) -> list[dict[str, Any]]:
    sections = [dict(section) for section in list(source_sections or []) if isinstance(section, dict)]
    if not sections:
        return []
    document_scope = str(input_mode or "").strip().lower() == "document"
    formation = dict(source_unit_formation or {})
    source_text_lower = str(source_text or "").lower()
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, section in enumerate(sections, start=1):
        section_id = str(section.get("section_id") or section.get("unit_id") or f"source_unit_{index}").strip()
        title = str(section.get("title") or section_id or f"Source unit {index}").strip()
        content_title = "" if str(section.get("kind") or "") == "manual_block" else title
        text, self_containment = _make_source_unit_text_self_contained(
            text=str(section.get("text") or ""),
            title=content_title,
        )
        if len(text) < 16:
            continue
        normalized_key = normalize_preview_text(f"{section_id}:{text[:420]}")
        if normalized_key in seen:
            continue
        seen.add(normalized_key)
        fact_eligible = bool(section.get("fact_eligible") if section.get("fact_eligible") is not None else True)
        supporting_eligible = bool(section.get("supporting_evidence_eligible") or False)
        start, end = _span_bounds_in_lower_source(source_text_lower, text[: min(len(text), 900)])
        claim_type = classify_claim_type(text)
        memory_type = "document_chunk" if document_scope else map_runtime_memory_type(None, claim_type=claim_type)
        role = str(section.get("source_unit_role") or "").strip() or "primary_evidence"
        promotion_role = str(section.get("promotion_role") or "").strip() or role
        formation_strategy = str(section.get("formation_strategy") or formation.get("formation_strategy") or "").strip() or None
        confidence = 0.84 if fact_eligible else 0.64 if supporting_eligible else 0.52
        selected_by_default = (
            bool(fact_eligible)
            and not _SOURCE_MOJIBAKE_RE.search(text)
            and not _SOURCE_STRUCTURED_METADATA_CHUNK_RE.search(text)
        )
        summary_prefix = content_title if content_title and content_title.lower() not in text[:120].lower() else ""
        summary = summarize_text(f"{summary_prefix}: {text}" if summary_prefix else text, limit=180)
        output.append(
            {
                "derivation_role": "claim",
                "claim_type": claim_type,
                "raw_text": text,
                "summary": summary,
                "memory_type": memory_type,
                "confidence": confidence,
                "memory_confidence": confidence,
                "evidence_confidence": 0.92 if fact_eligible else 0.7,
                "stability_confidence": 0.72 if fact_eligible else 0.52,
                "source_span_start": start,
                "source_span_end": end,
                "selected_by_default_override": selected_by_default,
                "document_role": "chunk" if document_scope else None,
                "document_anchor_id": "preview_primary" if document_scope else None,
                "document_chunk_index": index if document_scope else None,
                "source_unit_id": section_id,
                "source_unit_title": title,
                "source_unit_kind": str(section.get("kind") or ""),
                "source_unit_role": role,
                "promotion_role": promotion_role,
                "source_unit_formation_strategy": formation_strategy,
                "source_unit_self_containment": self_containment,
                "source_investigation_id": source_investigation_id,
                "source_grounding": {
                    "status": "supported",
                    "supported": True,
                    "score": 1.0,
                    "reason": "source_unit_text_from_compiler_handoff",
                },
            }
        )
    return output


_CONTEXT_DEPENDENT_SOURCE_START_RE = re.compile(
    r"^\s*(?:"
    r"it|this|that|these|those|he|she|they|his|her|their|"
    r"esso|essa|questo|questa|questi|queste|lui|lei|loro|suo|sua|suoi|sue|"
    r"the\s+(?:monument|company|project|document|release|source|site|profile|page|team|work)"
    r")\b",
    re.IGNORECASE,
)


def _source_unit_context_subject(title: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(title or "")).strip(" .:-")
    if not cleaned:
        return ""
    if len(cleaned) > 140:
        cleaned = summarize_text(cleaned, limit=140).strip(" .:-")
    return cleaned


def _make_source_unit_text_self_contained(*, text: str, title: str) -> tuple[str, dict[str, Any]]:
    raw = preserve_node_raw_text(str(text or "").strip(), limit=6000)
    subject = _source_unit_context_subject(title)
    if not raw or not subject:
        return raw, {
            "schema_version": "agvm.source_unit_self_containment.v1",
            "changed": False,
            "reason": "missing_text_or_subject",
            "subject": subject,
        }
    title_already_present = subject.lower() in raw[: min(len(raw), 220)].lower()
    context_dependent_start = bool(_CONTEXT_DEPENDENT_SOURCE_START_RE.search(raw))
    if title_already_present or not context_dependent_start:
        return raw, {
            "schema_version": "agvm.source_unit_self_containment.v1",
            "changed": False,
            "reason": "already_self_contained_or_not_context_dependent",
            "subject": subject,
            "context_dependent_start": context_dependent_start,
        }
    contextualized = preserve_node_raw_text(f"{subject}: {raw}", limit=6000)
    return contextualized, {
        "schema_version": "agvm.source_unit_self_containment.v1",
        "changed": True,
        "reason": "context_dependent_source_unit_prefixed_with_section_subject",
        "subject": subject,
        "context_dependent_start": context_dependent_start,
    }


_SOURCE_METADATA_FRAGMENT_START_RE = re.compile(
    r"^\s*(?:"
    r"#{1,6}\s+|"
    r"page\s+title|source\s+url|source\s+uri|description|heading|headings|section|visible\s+text|image\s+alt\s+text|"
    r"outline\s+of|skip\s+to|global\s+menu|breadcrumb|privacy|cookie|cookies|"
    r"pagina|titolo\s+pagina|fonte|descrizione"
    r")\s*[:|-]",
    re.IGNORECASE,
)
_SOURCE_MOJIBAKE_RE = re.compile(r"(?:Ã.|â€|â€™|â€œ|â€|Â)", re.IGNORECASE)
_SOURCE_MOJIBAKE_RE = re.compile(r"(?:\u00c3.|\u00e2\u20ac|\u00c2)", re.IGNORECASE)
_SOURCE_HEADING_ONLY_RE = re.compile(r"^\s*(?:[-*]\s*)?(?:heading\s*:\s*)?[A-Z0-9][A-Za-z0-9 &/|().,'-]{2,140}\s*$")
_SOURCE_GENERIC_ENTITY_TOKENS = {
    "source uri",
    "source url",
    "source",
    "document",
    "page title",
    "visible text",
    "heading",
    "headings",
}
_SOURCE_STRUCTURED_METADATA_CHUNK_RE = re.compile(
    r"^\s*(?:outline\s+of\b|.*\bHeadquarters:\s+.*\bOperations:\s+.*\bCEO:)",
    re.IGNORECASE | re.DOTALL,
)
_SOURCE_UNCERTAIN_WINDOW_RE = re.compile(
    r"\b(?:could|might|may\s+(?:represent|be|help)|potrebbe|potrebbero|forse|possibile|possibly)\b",
    re.IGNORECASE,
)
_SOURCE_CLAIM_ACTION_RE = re.compile(
    r"\b(?:"
    r"is|are|was|were|announces?|announced|acquires?|acquired|founds?|founded|"
    r"has|have|lists?|listed|employs?|employed|"
    r"provides?|provided|develops?|developed|specializes?|specialised|specialized|"
    r"focuses?|focused|integrates?|integrated|supports?|supported|creates?|created|"
    r"builds?|building|built|designs?|designed|helps?|helped|connects?|connected|"
    r"shapes?|shaped|believes?|believed|wants?|wanted|am|"
    r"manages?|managed|leads?|led|serves?|served|operates?|operated|"
    r"e|era|sono|annuncia|annunciato|acquisisce|acquisito|fonda|fondato|"
    r"sviluppa|sviluppato|fornisce|specializza|integra|supporta|gestisce|"
    r"costruisce|costruito|progetta|progettato|crede|vuole|collega"
    r")\b",
    re.IGNORECASE,
)
_SOURCE_LOW_VALUE_SENTENCE_RE = re.compile(
    r"\b(?:"
    r"cookie|cookies|privacy|login|sign in|subscribe|newsletter|continue|password|"
    r"click|mostra altro|visualizza profilo|followers?|linkedin|accept|reject|"
    r"menu|search|skip to|copyright|all rights reserved|zoom|"
    r"commento|commenti|condividi|accedi|iscriviti|registrati|visualizza il profilo|"
    r"profile views?|visualizza profili?|mostra meno|mostra post|aggiungi nuove competenze|"
    r"pi[uù]\s+di\s+(?:quaranta|[0-9]+)\s+milioni|more\s+than\s+(?:forty|[0-9]+)\s+million\s+people|"
    r"contratto\s+di\s+licenza|informativa\s+sulla\s+privacy|informativa\s+sui\s+cookie|"
    r"what\s+they\s+say|purpose\s+of\s+processing|terms\s+of\s+use|privacy\s+policy|"
    r"statistical\s+and\s+optimization\s+purposes|registered\s+trademarks?|"
    r"names\s+of\s+corporations|for\s+more\s+information\s+visit|industry\s+segment\s+topics"
    r")\b",
    re.IGNORECASE,
)
_SOURCE_CONTEXT_DEPENDENT_SENTENCE_RE = re.compile(
    r"^\s*(?:"
    r"through\s+this|with\s+this|as\s+a\s+result|according\s+to|"
    r"this\s+(?:relationship|acquisition|company|project|source|document|release|event|initiative|platform|system|monument|profile|page)|"
    r"the\s+(?:relationship|acquisition|company|project|source|document|release|event|initiative|platform|system|monument|profile|page)|"
    r"it|this|these|those|he|she|they"
    r")\b",
    re.IGNORECASE,
)


_SOURCE_ATOMIC_BOUNDARY_RE = re.compile(
    r"\s+(?=(?:"
    r"What\s+drives\s+me|My\s+focus|Every\s+venture|From\s+[a-z]+|Over\s+the\s+past|"
    r"I\s+(?:am|created|want|believe|have|build|built|focus|focuses|founded)|"
    r"My\s+(?:work|decision|focus|vision|belief|mission)|"
    r"The\s+[A-Z][A-Za-z0-9' -]{2,80}\s+(?:project|initiative|venture|foundation|system)"
    r")\b)"
)


def _source_atomic_claim_parts(value: str) -> list[str]:
    text = _strip_generated_source_unit_prefix(str(value or "").strip())
    if not text:
        return []
    normalized = re.sub(r"\s+", " ", text).strip(" ;:-")
    raw_parts = [
        part.strip(" ;:-")
        for part in re.split(r";\s+|(?<=[.!?])\s+|\n+", normalized)
        if part.strip(" ;:-")
    ]
    split_parts: list[str] = []
    for part in raw_parts or [normalized]:
        if len(part) > 120:
            subparts = [candidate.strip(" ;:-") for candidate in _SOURCE_ATOMIC_BOUNDARY_RE.split(part) if candidate.strip(" ;:-")]
            split_parts.extend(subparts or [part])
        else:
            split_parts.append(part)
    output: list[str] = []
    seen: set[str] = set()
    for part in split_parts:
        if len(part) < 36:
            continue
        if _SOURCE_METADATA_FRAGMENT_START_RE.search(part):
            continue
        if _SOURCE_LOW_VALUE_SENTENCE_RE.search(part):
            continue
        if not _SOURCE_CLAIM_ACTION_RE.search(part):
            continue
        if not _source_has_memory_anchor(part):
            continue
        key = _source_grounding_fold(part)
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(part)
    return output


def _source_atomized_item_variants(item: dict[str, Any]) -> list[dict[str, Any]]:
    raw_text = str(item.get("raw_text") or item.get("summary") or "").strip()
    parts = _source_atomic_claim_parts(raw_text)
    if len(parts) <= 1:
        return [item]
    variants: list[dict[str, Any]] = []
    for part in parts:
        variants.append(
            {
                **item,
                "raw_text": part,
                "summary": summarize_text(part, limit=180),
                "claim_type": _source_claim_type_for_source_fact(part),
                "retrieval_aliases": _source_question_variants_for_claim(claim_text=part, title="", limit=8),
                "reason": f"Atomized from composite source candidate. {str(item.get('reason') or '').strip()}".strip(),
            }
        )
    return variants


def _is_source_investigation_scope(
    *,
    source_type: str | None,
    source_sections: list[dict[str, Any]] | None,
) -> bool:
    return _source_grounding_requires_filter(source_type=source_type, input_mode="document") and bool(source_sections)


def _preview_source_quality_reasons(
    node: dict[str, Any],
    *,
    source_scope: bool,
    is_primary: bool = False,
) -> list[str]:
    if not source_scope:
        return []
    raw_text = str(node.get("raw_text") or node.get("summary") or "").strip()
    summary = str(node.get("summary") or "").strip()
    folded = normalize_preview_text(raw_text or summary)
    preview_kind = str(node.get("preview_kind") or "")
    document_role = str(node.get("document_role") or "")
    memory_type = str(node.get("memory_type") or "")
    source_unit_id = str(node.get("source_unit_id") or "").strip()
    reasons: list[str] = []

    if not raw_text:
        return ["empty_preview_node"]

    if is_primary:
        if not bool(node.get("is_document_anchor")) or memory_type != "document_anchor" or document_role != "anchor":
            reasons.append("source_primary_must_be_raw_document_anchor")
        # A raw anchor is the exact source artifact, not a synthesized memory.
        # Its length is not a safety signal; atomic child nodes carry the
        # self-containment and minimum-information quality checks below.
        return _unique_strings(reasons, limit=8)

    if document_role in {"chunk", "summary", "fact"} or source_unit_id:
        if not source_unit_id:
            reasons.append("source_child_missing_source_unit_id")
        min_child_length = 28 if document_role == "fact" else 80
        if len(raw_text) < min_child_length:
            reasons.append("source_child_too_thin")
        if len(raw_text) > 6200:
            reasons.append("source_child_too_large")
        if document_role not in {"chunk", "summary", "fact"}:
            reasons.append("source_child_missing_document_role")
        if document_role == "fact":
            if _SOURCE_LOW_VALUE_SENTENCE_RE.search(raw_text):
                reasons.append("low_value_source_fact")
            if _SOURCE_CONTEXT_DEPENDENT_SENTENCE_RE.search(raw_text):
                reasons.append("context_dependent_source_fact")
            if _SOURCE_METADATA_FRAGMENT_START_RE.search(raw_text) or _SOURCE_METADATA_FRAGMENT_START_RE.search(summary):
                reasons.append("source_metadata_fragment")
            if raw_text.lstrip().startswith(("- ", "* ", "#")) or summary.lstrip().startswith(("- ", "* ", "#")):
                reasons.append("fragmentary_bullet_or_heading_fact")
            if not _SOURCE_CLAIM_ACTION_RE.search(raw_text):
                reasons.append("source_fact_missing_action")
            if _SOURCE_HEADING_ONLY_RE.match(raw_text) and not _SOURCE_CLAIM_ACTION_RE.search(raw_text):
                reasons.append("heading_or_title_not_memory_claim")
        return _unique_strings(reasons, limit=8)

    if preview_kind in {"claim", "entity"} and not source_unit_id:
        reasons.append(f"source_{preview_kind}_missing_source_unit_id")

    if preview_kind == "entity":
        if folded in _SOURCE_GENERIC_ENTITY_TOKENS:
            reasons.append("source_metadata_entity")
        if len(raw_text) <= 5:
            reasons.append("ambiguous_short_entity")
        if _SOURCE_METADATA_FRAGMENT_START_RE.search(raw_text):
            reasons.append("source_metadata_entity_fragment")
        return _unique_strings(reasons, limit=8)

    if len(raw_text) < 48:
        reasons.append("claim_too_short_to_be_self_contained")
    if len(raw_text) > 3200:
        reasons.append("non_anchor_claim_too_large")
    if raw_text.startswith(("- ", "* ", "...")) or summary.startswith(("- ", "* ", "...")):
        reasons.append("fragmentary_bullet_or_ledger_text")
    if raw_text.lstrip().startswith("#") or summary.lstrip().startswith("#"):
        reasons.append("markdown_heading_not_memory_claim")
    if _SOURCE_METADATA_FRAGMENT_START_RE.search(raw_text) or _SOURCE_METADATA_FRAGMENT_START_RE.search(summary):
        reasons.append("source_metadata_fragment")
    if (
        _SOURCE_HEADING_ONLY_RE.match(raw_text)
        and not re.search(r"\b(?:is|are|was|were|founded|acquired|announced|serves|provides|focuses|fonda|acquis|annunci|e'|è)\b", raw_text, re.IGNORECASE)
    ):
        reasons.append("heading_or_title_not_memory_claim")
    return _unique_strings(reasons, limit=8)


def _apply_source_preview_quality_contract(
    *,
    primary_preview: dict[str, Any],
    derived_nodes: list[dict[str, Any]],
    source_type: str | None,
    source_sections: list[dict[str, Any]] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    source_scope = _is_source_investigation_scope(source_type=source_type, source_sections=source_sections)
    if not source_scope:
        return primary_preview, derived_nodes, {
            "schema_version": "agvm.preview_quality_contract.v1",
            "source_scope": False,
            "apply_safe": True,
            "rows": [],
            "blocking_reasons": [],
        }

    rows: list[dict[str, Any]] = []
    blocking_reasons: list[str] = []

    def annotate(node: dict[str, Any], *, is_primary: bool = False) -> dict[str, Any]:
        annotated = dict(node)
        reasons = _preview_source_quality_reasons(annotated, source_scope=True, is_primary=is_primary)
        blocking = bool(reasons)
        if reasons:
            annotated["requires_human_review"] = True
            annotated["cognitive_status"] = "review_required"
            annotated["cognitive_review_reasons"] = _unique_strings(
                [*list(annotated.get("cognitive_review_reasons") or []), *reasons],
                limit=16,
            )
            if not is_primary:
                annotated["selected_by_default"] = False
        node_id = str(annotated.get("id") or "")
        rows.append(
            {
                "node_id": node_id,
                "preview_kind": annotated.get("preview_kind"),
                "role": "raw_anchor"
                if is_primary
                else str(annotated.get("document_role") or annotated.get("memory_type") or annotated.get("preview_kind") or ""),
                "source_unit_id": annotated.get("source_unit_id"),
                "selected_by_default": bool(annotated.get("selected_by_default", False)),
                "requires_human_review": bool(annotated.get("requires_human_review")),
                "blocking": blocking,
                "reasons": reasons,
                "summary": summarize_text(str(annotated.get("summary") or annotated.get("raw_text") or ""), limit=180),
            }
        )
        if blocking:
            blocking_reasons.extend(f"{node_id or 'preview_node'}:{reason}" for reason in reasons)
        return annotated

    primary_preview = annotate(primary_preview, is_primary=True)
    derived_nodes = [annotate(node, is_primary=False) for node in derived_nodes]
    selected_blocking = [
        row
        for row in rows
        if bool(row.get("blocking")) and (row.get("preview_kind") == "primary" or bool(row.get("selected_by_default")))
    ]
    return primary_preview, derived_nodes, {
        "schema_version": "agvm.preview_quality_contract.v1",
        "source_scope": True,
        "apply_safe": not selected_blocking,
        "row_count": len(rows),
        "blocking_row_count": sum(1 for row in rows if bool(row.get("blocking"))),
        "selected_blocking_row_count": len(selected_blocking),
        "blocking_reasons": _unique_strings(blocking_reasons, limit=32),
        "rows": rows,
    }


def _source_claim_sentence_candidates(text: str) -> list[str]:
    raw = str(text or "").replace("\r", "\n")
    if "Visible text:" in raw:
        metadata, visible = raw.split("Visible text:", 1)
        description_parts = [
            line.split(":", 1)[1].strip()
            for line in metadata.splitlines()
            if line.strip().lower().startswith("description:") and ":" in line
        ]
        raw = "\n".join([*description_parts, visible])
    lines: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if lowered.startswith(("page title:", "source url:", "source uri:", "headings:", "heading:", "section:", "visible text:")):
            continue
        if stripped.startswith(("- ", "* ")) and len(stripped) < 90:
            continue
        lines.append(stripped.lstrip("-* ").strip())
    normalized = re.sub(r"\s+", " ", " ".join(lines)).strip()
    if not normalized:
        return []
    pieces = [
        part.strip(" ;:-")
        for part in re.split(r"(?<=[.!?])\s+|;\s+", normalized)
        if part.strip(" ;:-")
    ]
    expanded_pieces: list[str] = []
    for piece in pieces:
        expanded_pieces.extend(_source_atomic_claim_parts(piece) or [piece])
    pieces = expanded_pieces
    candidates: list[str] = []
    seen: set[str] = set()
    for piece in pieces:
        if len(piece) < 44 or len(piece) > 620:
            continue
        if _SOURCE_MOJIBAKE_RE.search(piece):
            continue
        if _SOURCE_LOW_VALUE_SENTENCE_RE.search(piece):
            continue
        if _SOURCE_UNCERTAIN_WINDOW_RE.search(piece):
            continue
        if _SOURCE_CONTEXT_DEPENDENT_SENTENCE_RE.search(piece):
            continue
        if _SOURCE_METADATA_FRAGMENT_START_RE.search(piece):
            continue
        if not _SOURCE_CLAIM_ACTION_RE.search(piece):
            continue
        if not _source_has_memory_anchor(piece):
            continue
        key = normalize_preview_text(piece)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(piece)
        if len(candidates) >= 96:
            break
    return candidates


def _source_evidence_window_candidates(text: str, *, max_windows: int = 6) -> list[str]:
    raw = str(text or "").replace("\r", "\n")
    if "Visible text:" in raw:
        raw = raw.split("Visible text:", 1)[1]
    lines: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip().lstrip("-* ").strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if lowered.startswith(("page title:", "source url:", "source uri:", "heading:", "headings:", "section:")):
            continue
        if _SOURCE_LOW_VALUE_SENTENCE_RE.search(stripped):
            continue
        if _SOURCE_METADATA_FRAGMENT_START_RE.search(stripped):
            continue
        lines.append(stripped)
    normalized = re.sub(r"\s+", " ", " ".join(lines)).strip()
    if not normalized:
        return []
    sentences = [
        part.strip(" ;:-")
        for part in re.split(r"(?<=[.!?])\s+|;\s+", normalized)
        if part.strip(" ;:-")
    ]
    windows: list[str] = []
    seen: set[str] = set()
    cursor: list[str] = []
    cursor_len = 0

    def emit() -> None:
        nonlocal cursor, cursor_len
        if not cursor:
            return
        window = re.sub(r"\s+", " ", " ".join(cursor)).strip()
        cursor = []
        cursor_len = 0
        if len(window) < 96 or len(window) > 900:
            return
        if _SOURCE_MOJIBAKE_RE.search(window):
            return
        if _SOURCE_LOW_VALUE_SENTENCE_RE.search(window):
            return
        if _SOURCE_UNCERTAIN_WINDOW_RE.search(window):
            return
        if _SOURCE_METADATA_FRAGMENT_START_RE.search(window):
            return
        named_tokens = _source_grounding_named_tokens(window)
        has_signal = bool(_SOURCE_CLAIM_ACTION_RE.search(window)) or bool(named_tokens) or bool(re.search(r"\b(?:19|20)\d{2}\b|\b\d+[,.]?\d*\b", window))
        if not has_signal:
            return
        key = normalize_preview_text(window)
        if key in seen or any(existing and (existing in key or key in existing) for existing in seen):
            return
        seen.add(key)
        windows.append(window)

    for sentence in sentences:
        if len(sentence) < 32:
            continue
        projected = cursor_len + len(sentence) + (1 if cursor else 0)
        if cursor and projected > 720:
            emit()
        cursor.append(sentence)
        cursor_len += len(sentence) + (1 if cursor_len else 0)
        if cursor_len >= 360:
            emit()
        if len(windows) >= max_windows:
            break
    if len(windows) < max_windows:
        emit()
    return windows[:max_windows]


_SOURCE_STRUCTURED_FIELD_RE = re.compile(
    r"\b(Headquarters|Operations|CEO|Established|Number of employees|Business summary|Website URL|Website):\s*(.*?)"
    r"(?=\s+\b(?:Headquarters|Operations|CEO|Established|Number of employees|Business summary|Website URL|Website):|$)",
    re.IGNORECASE,
)


def _source_unit_heading(text: str) -> str:
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith(("heading:", "section:")) and ":" in stripped:
            return stripped.split(":", 1)[1].strip()
    return ""


def _source_unit_subject_from_title(*, text: str, title: str) -> str:
    heading = _source_unit_heading(text)
    candidates = [heading, title]
    for candidate in candidates:
        clean = re.sub(r"\s+-\s+segment\s+\d+\s*$", "", str(candidate or ""), flags=re.IGNORECASE).strip()
        outline = re.search(r"\bOutline\s+of\s+(.+)$", clean, flags=re.IGNORECASE)
        if outline:
            return outline.group(1).strip(" .:-")
        about = re.search(r"\bAbout\s+(.+)$", clean, flags=re.IGNORECASE)
        if about:
            return about.group(1).strip(" .:-")
    return ""


def _source_structured_claim_candidates(*, text: str, title: str) -> list[str]:
    raw = str(text or "").replace("\r", "\n")
    visible = raw.split("Visible text:", 1)[1] if "Visible text:" in raw else raw
    subject = _source_unit_subject_from_title(text=raw, title=title)
    if not subject:
        return []
    flat = re.sub(r"\s+", " ", visible).strip()
    claims: list[str] = []
    for match in _SOURCE_STRUCTURED_FIELD_RE.finditer(flat):
        field = match.group(1).strip().lower()
        value = match.group(2).strip(" .;:-")
        if not value:
            continue
        if field == "headquarters":
            claims.append(f"{subject} has headquarters at {value}.")
        elif field == "operations":
            claims.append(f"{subject} has operations at {value}.")
        elif field == "ceo":
            claims.append(f"{subject}'s CEO is {value}.")
        elif field == "established":
            claims.append(f"{subject} was established in {value}.")
        elif field == "number of employees":
            claims.append(f"{subject} has {value} employees.")
        elif field in {"business summary", "website", "website url"}:
            continue
        if len(claims) >= 8:
            break
    return claims


def _source_section_claim_preview_items(
    *,
    source_text: str,
    source_sections: list[dict[str, Any]] | None,
    source_investigation_id: str | None = None,
    max_claims_per_section: int = 6,
    max_total_claims: int = 384,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    source_text_lower = str(source_text or "").lower()
    for section in [dict(item) for item in list(source_sections or []) if isinstance(item, dict)]:
        section_id = str(section.get("section_id") or section.get("unit_id") or "").strip()
        title = str(section.get("title") or section_id or "Source unit").strip()
        content_title = "" if str(section.get("kind") or "") == "manual_block" else title
        fact_eligible = bool(section.get("fact_eligible") if section.get("fact_eligible") is not None else True)
        if not fact_eligible:
            continue
        emitted_for_section = 0
        section_text = str(section.get("text") or "")
        structured_candidates = _source_structured_claim_candidates(text=section_text, title=content_title)
        section_limit = max_claims_per_section
        if structured_candidates:
            section_limit = max(max_claims_per_section, min(8, len(structured_candidates)))
        sentence_candidates = [
            *[(sentence, True) for sentence in structured_candidates],
            *[(sentence, False) for sentence in _source_claim_sentence_candidates(section_text)],
        ]
        for sentence, structured_claim in sentence_candidates:
            claim_text, self_containment = _make_source_unit_text_self_contained(
                text=sentence,
                title=content_title,
            )
            if len(claim_text) < (28 if structured_claim else 48):
                continue
            key = normalize_preview_text(claim_text)
            if key in seen or any(existing and (existing in key or key in existing) for existing in seen):
                continue
            seen.add(key)
            start, end = _span_bounds_in_lower_source(source_text_lower, sentence[: min(len(sentence), 520)])
            claim_type = _source_claim_type_for_source_fact(claim_text)
            nucleus_role = _source_claim_nucleus_role(claim_text)
            confidence = 0.88 if nucleus_role else 0.82
            retrieval_aliases = _source_question_variants_for_claim(
                claim_text=claim_text,
                title=content_title,
                limit=8,
            )
            output.append(
                {
                    "derivation_role": "claim",
                    "claim_type": claim_type,
                    "raw_text": preserve_node_raw_text(claim_text, limit=900),
                    "summary": summarize_text(claim_text, limit=180),
                    "memory_type": map_runtime_memory_type(None, claim_type=claim_type),
                    "confidence": confidence,
                    "memory_confidence": confidence,
                    "evidence_confidence": 0.9,
                    "stability_confidence": 0.74,
                    "source_span_start": start,
                    "source_span_end": end,
                    "selected_by_default_override": bool(fact_eligible),
                    "document_role": "fact",
                    "document_anchor_id": "preview_primary",
                    "source_unit_id": section_id or None,
                    "source_unit_title": title,
                    "source_unit_kind": str(section.get("kind") or ""),
                    "source_unit_role": str(section.get("source_unit_role") or ""),
                    "promotion_role": str(section.get("promotion_role") or ""),
                    "source_unit_formation_strategy": str(section.get("formation_strategy") or "") or None,
                    "source_unit_self_containment": self_containment,
                    "source_investigation_id": source_investigation_id,
                    "source_grounding": {
                        "status": "supported",
                        "supported": True,
                        "score": 1.0,
                        "reason": "sentence_extracted_from_source_unit",
                    },
                    "retrieval_affordance": {
                        "schema_version": "agvm.source_claim_retrieval_affordance.v1",
                        "question": retrieval_aliases[0]
                        if retrieval_aliases
                        else _source_question_for_claim(claim_text=claim_text, title=content_title),
                        "answer_claim": claim_text,
                        "nucleus_role": nucleus_role,
                        "purpose": "improve_agent_query_landing_for_clean_source_claim",
                    }
                    if retrieval_aliases or nucleus_role
                    else {},
                    "retrieval_aliases": retrieval_aliases,
                }
            )
            emitted_for_section += 1
            if emitted_for_section >= section_limit or len(output) >= max_total_claims:
                break
        if len(output) >= max_total_claims:
            break
    return output


def _source_subject_for_claim(*, claim_text: str, title: str) -> str:
    phrases = _source_grounding_named_phrases(claim_text, limit=12)
    title_phrases = _source_grounding_named_phrases(title, limit=6)
    folded_claim = _source_grounding_fold(claim_text)
    action_subject_pattern = (
        r"\b([A-Z][A-Za-z0-9À-ÖØ-öø-ÿ'’&.-]+(?:\s+[A-Z][A-Za-z0-9À-ÖØ-öø-ÿ'’&.-]+){0,5})\s+"
        r"(?:founded|co-founded|established|created|acquired|announced|develops|provides|specializes|specialised|specialized|"
        r"fondato|fondata|fondò|fonda|acquisito|acquisita|sviluppa|fornisce|specializza)\b"
    )
    for match in re.finditer(action_subject_pattern, claim_text):
        candidate = re.sub(r"\s+", " ", match.group(1)).strip(" .,:;")
        if candidate and _source_grounding_fold(candidate) not in _SOURCE_GENERIC_ENTITY_TOKENS:
            return candidate
    for phrase in title_phrases:
        if _source_grounding_fold(phrase) and _source_grounding_fold(phrase) in folded_claim:
            return phrase
    multi_word = [phrase for phrase in phrases if len(phrase.split()) >= 2]
    if multi_word:
        return multi_word[0]
    if phrases:
        return phrases[0]
    return _source_unit_context_subject(title)


def _source_claim_nucleus_role(claim_text: str) -> str | None:
    lowered = str(claim_text or "").lower()
    phrases = _source_grounding_named_phrases(claim_text, limit=8)
    has_named_subject = bool(phrases)
    role_markers = (
        "founder",
        "co-founder",
        "chief executive",
        "ceo",
        "entrepreneur",
        "executive",
        "leader",
        "engineer",
        "coder",
        "professor",
        "fondatore",
        "cofondatore",
        "imprenditore",
        "dirigente",
        "amministratore",
    )
    organization_action_markers = (
        "founded",
        "co-founded",
        "established",
        "created",
        "acquired",
        "announced",
        "develops",
        "provides",
        "specializes",
        "specialised",
        "specialized",
        "integrates",
        "fondato",
        "fondata",
        "acquisito",
        "acquisita",
        "sviluppa",
        "fornisce",
        "specializza",
        "integra",
    )
    if has_named_subject and any(marker in lowered for marker in role_markers):
        return "person_identity_nucleus"
    if has_named_subject and any(marker in lowered for marker in organization_action_markers):
        return "organization_project_nucleus"
    if has_named_subject and re.search(r"\b(?:19|20)\d{2}\b", claim_text):
        return "temporal_relation_nucleus"
    return None


def _source_claim_type_for_source_fact(claim_text: str) -> str:
    nucleus_role = _source_claim_nucleus_role(claim_text)
    lowered = str(claim_text or "").lower()
    if nucleus_role == "person_identity_nucleus":
        return "identity_claim"
    if nucleus_role == "organization_project_nucleus":
        return "project_claim" if re.search(r"\b(?:develops?|provides?|speciali[sz]es?|integrates?|software|platform|solution|project|product|azienda|societ|company)\b", lowered) else "event_claim"
    return classify_claim_type(claim_text)


def _source_question_for_claim(*, claim_text: str, title: str) -> str:
    subject = _source_subject_for_claim(claim_text=claim_text, title=title)
    if subject:
        return f"What source-grounded fact does this material state about {subject}?"
    return "What source-grounded fact does this material state?"


def _source_question_variants_for_claim(*, claim_text: str, title: str, limit: int = 8) -> list[str]:
    subject = _source_subject_for_claim(claim_text=claim_text, title=title)
    subject = subject or "this source"
    variants = [
        f"What source-grounded fact does this material state about {subject}?",
        f"What should an agent remember about {subject} from this source?",
        f"Quale informazione verificata contiene questa fonte su {subject}?",
    ]
    lowered = claim_text.lower()
    if re.search(r"\b(?:19|20)\d{2}\b", claim_text):
        variants.append(f"What dated event or timeline fact involving {subject} is grounded here?")
        variants.append(f"Quale data o passaggio temporale riguarda {subject} in questa fonte?")
    if re.search(r"\b(?:company|companies|organization|organisations?|azienda|societ|corporation|foundation|foundry|studio|university|software|platform|solution|project|product)\b", lowered):
        variants.append(f"Which company or project connection involving {subject} is stated by this source?")
        variants.append(f"Quale collegamento aziendale o progettuale riguarda {subject}?")
    if re.search(r"\b(?:acquir|acquired|acquisition|acquisit|acquisizione)\b", lowered):
        variants.append(f"What acquisition relationship involving {subject} is grounded in this source?")
    if re.search(r"\b(?:found|founded|fonda|fondato|established|created|creata|creato)\b", lowered):
        variants.append(f"What founding or creation fact involving {subject} is grounded in this source?")
    if re.search(r"\b(?:sicily|sicilia|catania|acireale|italy|italia|headquarters|sede|location|place)\b", lowered):
        variants.append(f"What place or location context involving {subject} is stated here?")
    output: list[str] = []
    seen: set[str] = set()
    for variant in variants:
        clean = re.sub(r"\s+", " ", variant).strip()
        key = normalize_preview_text(clean)
        if clean and key not in seen:
            seen.add(key)
            output.append(clean)
        if len(output) >= limit:
            break
    return output


def _source_section_qa_preview_items(
    *,
    source_text: str,
    source_sections: list[dict[str, Any]] | None,
    source_investigation_id: str | None = None,
    max_qa_per_section: int = 48,
    max_total_qa: int = 1600,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    source_text_lower = str(source_text or "").lower()
    for section in [dict(item) for item in list(source_sections or []) if isinstance(item, dict)]:
        section_id = str(section.get("section_id") or section.get("unit_id") or "").strip()
        title = str(section.get("title") or section_id or "Source unit").strip()
        content_title = "" if str(section.get("kind") or "") == "manual_block" else title
        fact_eligible = bool(section.get("fact_eligible") if section.get("fact_eligible") is not None else True)
        if not fact_eligible:
            continue
        emitted_for_section = 0
        section_text = str(section.get("text") or "")
        sentence_candidates = [
            *_source_structured_claim_candidates(text=section_text, title=content_title),
            *_source_claim_sentence_candidates(section_text),
        ]
        for sentence in sentence_candidates:
            claim_text, self_containment = _make_source_unit_text_self_contained(
                text=sentence,
                title=content_title,
            )
            if len(claim_text) < 32:
                continue
            start, end = _span_bounds_in_lower_source(source_text_lower, sentence[: min(len(sentence), 520)])
            claim_type = _source_claim_type_for_source_fact(claim_text)
            confidence = 0.8
            for question in _source_question_variants_for_claim(
                claim_text=claim_text,
                title=content_title,
                limit=8,
            ):
                alias_text = f"{question} {claim_text}"
                qa_text = f"Retrieval question: {question} Answer grounded in source: {claim_text}"
                key = normalize_preview_text(alias_text)
                if key in seen or any(existing and (existing in key or key in existing) for existing in seen):
                    continue
                seen.add(key)
                output.append(
                    {
                        "derivation_role": "claim",
                        "claim_type": claim_type,
                        "raw_text": preserve_node_raw_text(qa_text, limit=1300),
                        "summary": summarize_text(claim_text, limit=180),
                        "memory_type": map_runtime_memory_type(None, claim_type=claim_type),
                        "confidence": confidence,
                        "memory_confidence": confidence,
                        "evidence_confidence": 0.9,
                        "stability_confidence": 0.72,
                        "source_span_start": start,
                        "source_span_end": end,
                        "selected_by_default_override": True,
                        "document_role": "fact",
                        "document_anchor_id": "preview_primary",
                        "source_unit_id": section_id or None,
                        "source_unit_title": title,
                        "source_unit_kind": str(section.get("kind") or ""),
                        "source_unit_role": str(section.get("source_unit_role") or ""),
                        "promotion_role": str(section.get("promotion_role") or ""),
                        "source_unit_formation_strategy": str(section.get("formation_strategy") or "") or None,
                        "source_unit_self_containment": self_containment,
                        "source_investigation_id": source_investigation_id,
                        "source_grounding": {
                            "status": "supported",
                            "supported": True,
                            "score": 1.0,
                            "reason": "source_grounded_qa_affordance_from_source_unit",
                        },
                        "retrieval_affordance": {
                            "schema_version": "agvm.source_grounded_qa_affordance.v1",
                            "question": question,
                            "answer_claim": claim_text,
                            "purpose": "improve_agent_query_landing_without_inventing_new_fact",
                        },
                        "retrieval_aliases": [question, alias_text],
                    }
                )
                emitted_for_section += 1
                if emitted_for_section >= max_qa_per_section or len(output) >= max_total_qa:
                    break
            if emitted_for_section >= max_qa_per_section or len(output) >= max_total_qa:
                break
        if len(output) >= max_total_qa:
            break
    return output


def _source_section_micro_chunk_preview_items(
    *,
    source_text: str,
    input_mode: str,
    source_sections: list[dict[str, Any]] | None,
    source_investigation_id: str | None = None,
    max_chunks_per_section: int = 6,
    max_total_chunks: int = 900,
) -> list[dict[str, Any]]:
    document_scope = str(input_mode or "").strip().lower() == "document"
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    source_text_lower = str(source_text or "").lower()
    for section in [dict(item) for item in list(source_sections or []) if isinstance(item, dict)]:
        section_id = str(section.get("section_id") or section.get("unit_id") or "").strip()
        title = str(section.get("title") or section_id or "Source unit").strip()
        content_title = "" if str(section.get("kind") or "") == "manual_block" else title
        fact_eligible = bool(section.get("fact_eligible") if section.get("fact_eligible") is not None else True)
        supporting_eligible = bool(section.get("supporting_evidence_eligible") or False)
        if not fact_eligible and not supporting_eligible:
            continue
        section_text = str(section.get("text") or "")
        for chunk_index, window in enumerate(_source_evidence_window_candidates(section_text, max_windows=max_chunks_per_section), start=1):
            chunk_text, self_containment = _make_source_unit_text_self_contained(
                text=window,
                title=content_title,
            )
            if len(chunk_text) < 96:
                continue
            key = normalize_preview_text(chunk_text)
            if key in seen or any(existing and (existing in key or key in existing) for existing in seen):
                continue
            seen.add(key)
            start, end = _span_bounds_in_lower_source(source_text_lower, window[: min(len(window), 520)])
            confidence = 0.8 if fact_eligible else 0.66
            selected_by_default = (
                bool(fact_eligible)
                and not _SOURCE_MOJIBAKE_RE.search(chunk_text)
                and not _SOURCE_STRUCTURED_METADATA_CHUNK_RE.search(chunk_text)
            )
            output.append(
                {
                    "derivation_role": "claim",
                    "claim_type": _source_claim_type_for_source_fact(chunk_text),
                    "raw_text": preserve_node_raw_text(chunk_text, limit=1300),
                    "summary": summarize_text(
                        f"{content_title}: {chunk_text}" if content_title else chunk_text,
                        limit=180,
                    ),
                    "memory_type": "document_chunk" if document_scope else map_runtime_memory_type(None, claim_type=_source_claim_type_for_source_fact(chunk_text)),
                    "confidence": confidence,
                    "memory_confidence": confidence,
                    "evidence_confidence": 0.88,
                    "stability_confidence": 0.7,
                    "source_span_start": start,
                    "source_span_end": end,
                    "selected_by_default_override": selected_by_default,
                    "document_role": "chunk",
                    "document_anchor_id": "preview_primary",
                    "document_chunk_index": chunk_index,
                    "source_unit_id": section_id or None,
                    "source_unit_title": title,
                    "source_unit_kind": str(section.get("kind") or ""),
                    "source_unit_role": str(section.get("source_unit_role") or ""),
                    "promotion_role": str(section.get("promotion_role") or ""),
                    "source_unit_formation_strategy": str(section.get("formation_strategy") or "") or None,
                    "source_unit_self_containment": self_containment,
                    "source_investigation_id": source_investigation_id,
                    "source_grounding": {
                        "status": "supported",
                        "supported": True,
                        "score": 1.0,
                        "reason": "source_grounded_micro_chunk_from_source_unit",
                    },
                    "retrieval_affordance": {
                        "schema_version": "agvm.source_grounded_micro_chunk.v1",
                        "purpose": "preserve_dense_local_context_for_mcp_retrieve_without_mega_nodes",
                    },
                }
            )
            if len(output) >= max_total_chunks:
                break
        if len(output) >= max_total_chunks:
            break
    return output


def _source_preview_item_is_qa_affordance(item: dict[str, Any]) -> bool:
    affordance = dict(item.get("retrieval_affordance") or {})
    raw_text = str(item.get("raw_text") or item.get("summary") or "").strip().lower()
    return bool(
        affordance.get("schema_version") == "agvm.source_grounded_qa_affordance.v1"
        or raw_text.startswith("retrieval question:")
    )


def _append_source_section_preview_items(
    compiled_derived: list[dict[str, Any]],
    source_items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    if not source_items:
        return compiled_derived, 0
    seen_texts = {
        _source_grounding_fold(str(item.get("raw_text") or item.get("summary") or ""))
        for item in compiled_derived
    }
    output = list(compiled_derived)
    added = 0
    def merge_aliases(base: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        merged = dict(base)
        if item.get("retrieval_affordance") and not merged.get("retrieval_affordance"):
            merged["retrieval_affordance"] = dict(item.get("retrieval_affordance") or {})
        elif item.get("retrieval_affordance") and merged.get("retrieval_affordance"):
            merged["retrieval_affordance"] = {
                **dict(merged.get("retrieval_affordance") or {}),
                "additional_affordances": [
                    *list(dict(merged.get("retrieval_affordance") or {}).get("additional_affordances") or []),
                    dict(item.get("retrieval_affordance") or {}),
                ][:8],
            }
        merged["retrieval_aliases"] = _unique_strings(
            [
                *[str(alias).strip() for alias in list(merged.get("retrieval_aliases") or []) if str(alias).strip()],
                *[str(alias).strip() for alias in list(item.get("retrieval_aliases") or []) if str(alias).strip()],
            ],
            limit=24,
        )
        return merged

    def enrich_existing(normalized_text: str, item: dict[str, Any]) -> bool:
        if not (item.get("retrieval_affordance") or item.get("retrieval_aliases")):
            return False
        for index in range(len(output) - 1, -1, -1):
            existing = output[index]
            if _source_grounding_fold(str(existing.get("raw_text") or existing.get("summary") or "")) != normalized_text:
                continue
            output[index] = merge_aliases(existing, item)
            return True
        return False

    def replace_or_enrich_contained(existing_key: str, new_key: str, item: dict[str, Any]) -> bool:
        for index in range(len(output) - 1, -1, -1):
            existing = output[index]
            current_key = _source_grounding_fold(str(existing.get("raw_text") or existing.get("summary") or ""))
            if current_key != existing_key:
                continue
            existing_role = str(existing.get("document_role") or "").strip().lower()
            item_role = str(item.get("document_role") or "").strip().lower()
            if (
                existing_role != item_role
                and existing_role in {"chunk", "fact", "summary"}
                and item_role in {"chunk", "fact", "summary"}
            ):
                return False
            if _source_preview_item_is_qa_affordance(item):
                return False
            if existing_key in new_key and len(new_key) > len(existing_key) + 24:
                replacement = merge_aliases(dict(item), existing)
                output[index] = replacement
                seen_texts.discard(existing_key)
                seen_texts.add(new_key)
                return True
            output[index] = merge_aliases(existing, item)
            return True
        return False

    for item in source_items:
        normalized = _source_grounding_fold(str(item.get("raw_text") or item.get("summary") or ""))
        if not normalized or normalized in seen_texts:
            enrich_existing(normalized, item)
            continue
        if (
            len(seen_texts) < 512
            and any(existing and (existing in normalized or normalized in existing) for existing in seen_texts)
        ):
            handled = False
            for existing in list(seen_texts):
                if existing and (existing in normalized or normalized in existing):
                    handled = replace_or_enrich_contained(existing, normalized, item)
                    break
            if handled:
                continue
        seen_texts.add(normalized)
        output.append(item)
        added += 1
    return output, added


def preview_bundle(
    text: str,
    input_mode: str,
    graph: dict[str, Any],
    index_payload: dict[str, Any],
    atlas_payload: dict[str, Any] | None = None,
    *,
    source_label: str | None = None,
    source_type: str | None = None,
    source_trust: str | None = None,
    learning_mode: str | None = None,
    question_limit: int | None = None,
    source_sections: list[dict[str, Any]] | None = None,
    source_unit_formation: dict[str, Any] | None = None,
    source_investigation_id: str | None = None,
    source_purpose: str | None = None,
    operator_instruction: str | None = None,
    source_context: dict[str, Any] | None = None,
    compiler_timeout_seconds: float | None = None,
    derivation_timeout_seconds: float | None = None,
    geometry_profile_context: dict[str, Any] | None = None,
    compiler_payload_override: dict[str, Any] | None = None,
    compiler_error_override: str | None = None,
    compiler_api_key_override: str | None = None,
    compiler_model_override: str | None = None,
    compiler_execution_metadata: dict[str, Any] | None = None,
    require_ai: bool = False,
) -> dict[str, Any]:
    question_limit = _normalize_question_limit(question_limit)
    warnings: list[dict[str, str]] = []
    source_investigation_scope = _is_source_investigation_scope(source_type=source_type, source_sections=source_sections)
    if source_investigation_scope and str(input_mode or "").strip().lower() != "document":
        input_mode = "document"
        warnings.append(
            {
                "code": "source_investigation_forced_document_anchor",
                "message": "Source investigation preview uses document mode so raw anchors and source chunks cannot be persisted as ordinary claims.",
            }
        )
    source_section_count = len([section for section in list(source_sections or []) if isinstance(section, dict)])
    direct_source_unit_preview = source_investigation_scope and (
        source_section_count >= 16 or len(str(text or "")) >= 60_000
    )
    identity_nucleus = build_identity_nucleus(graph)
    nearby_context = nearby_context_for_seed(text, input_mode, graph, index_payload, atlas_payload)
    resolved_source_context = {
        **dict(source_context or {}),
        "source_purpose": source_purpose or dict(source_context or {}).get("source_purpose"),
        "operator_instruction": operator_instruction or dict(source_context or {}).get("operator_instruction"),
        "source_type": source_type,
        "source_trust": source_trust,
        "source_label": source_label,
        "source_section_count": source_section_count,
        "source_unit_formation_status": dict(source_unit_formation or {}).get("status"),
    }
    source_request = dict(resolved_source_context.get("source_request") or {})
    source_sections_by_id = {
        str(section.get("section_id") or section.get("unit_id") or "").strip(): dict(section)
        for section in list(source_sections or [])
        if isinstance(section, dict) and str(section.get("section_id") or section.get("unit_id") or "").strip()
    }
    primary_source_uri = sanitize_source_uri_for_persistence(
        source_request.get("source_uri")
        or next(
            (
                section.get("source_uri")
                for section in source_sections_by_id.values()
                if section.get("source_uri")
            ),
            None,
        )
    )
    source_ref_id = (
        str(
            source_request.get("source_ref_id")
            or resolved_source_context.get("source_ref_id")
            or (f"source_ref::{source_investigation_id}" if source_investigation_id else "")
        ).strip()
        or None
    )

    if compiler_payload_override is not None:
        compiler_payload = dict(compiler_payload_override)
        compiler_error = compiler_error_override
    elif direct_source_unit_preview and not require_ai:
        compiler_payload = None
        compiler_error = "large_source_units_direct_preview"
        warnings.append(
            {
                "code": "large_source_units_direct_preview",
                "message": (
                    "Large source investigation skipped monolithic compiler/autoderive and built a persistible "
                    "preview directly from traced source units."
                ),
            }
        )
    else:
        compiler_payload, compiler_error = llm_memory_compile(
            text,
            input_mode,
            identity_nucleus=identity_nucleus,
            nearby_context=nearby_context,
            timeout_seconds=compiler_timeout_seconds,
            source_context=resolved_source_context,
            api_key_override=compiler_api_key_override,
            model_override=compiler_model_override,
            execution_metadata=compiler_execution_metadata,
        )
    if require_ai and not compiler_payload:
        raise ValueError(f"grow_ai_unavailable:{compiler_error or 'provider_output_missing'}")
    if require_ai:
        validation_error: AiModuleContractError | None = None
        for repair_attempt in range(3):
            try:
                compiler_payload = validate_grow_compiler_payload(compiler_payload)
                validation_error = None
                break
            except AiModuleContractError as exc:
                validation_error = exc
                if compiler_payload_override is not None or repair_attempt >= 2:
                    break
                repair_context = dict(resolved_source_context)
                prior_instruction = str(repair_context.get("operator_instruction") or "").strip()
                repair_context["operator_instruction"] = "\n".join(
                    part
                    for part in (
                        prior_instruction,
                        (
                            "Repair the previous compiler contract failure "
                            f"({exc.code}). Recompile the same grounded source. Every primary and derived node "
                            "must include all 12 routing_semantic_scores and all 12 routing_facets, each as a "
                            "finite number from 0.0 through 1.0. Preserve atomic source-grounded memories; do "
                            "not remove useful evidence merely to satisfy the schema."
                        ),
                    )
                    if part
                )
                compiler_payload, compiler_error = llm_memory_compile(
                    text,
                    input_mode,
                    identity_nucleus=identity_nucleus,
                    nearby_context=nearby_context,
                    timeout_seconds=compiler_timeout_seconds,
                    source_context=repair_context,
                    api_key_override=compiler_api_key_override,
                    model_override=compiler_model_override,
                    execution_metadata=compiler_execution_metadata,
                )
                if not compiler_payload:
                    raise ValueError(f"grow_ai_unavailable:{compiler_error or 'provider_output_missing'}") from exc
        if validation_error is not None:
            raise validation_error
    derivation_mode = "llm" if compiler_payload else "heuristic"
    if compiler_error and compiler_error not in {"llm_disabled", "large_source_units_direct_preview"}:
        warnings.append({"code": "compiler_fallback", "message": f"LLM compiler unavailable, fallback to heuristic compilation: {compiler_error}"})

    primary_compiled = dict((compiler_payload or {}).get("primary_node") or {})
    if not primary_compiled:
        heuristic = heuristic_projection(text, input_mode=input_mode)
        primary_compiled = {
            "summary": heuristic["summary"],
            "memory_type": heuristic["memory_type"],
            "guide_area": heuristic["expected_guide_area"],
            "routing_semantic_scores": heuristic["routing_semantic_scores"],
            "routing_facets": heuristic["routing_facets"],
            "granularity": heuristic["granularity"],
            "novelty": heuristic["novelty"],
            "memory_confidence": 0.78,
            "evidence_confidence": 0.82,
            "stability_confidence": 0.65,
        }
    if _source_grounding_requires_filter(source_type=source_type, input_mode=input_mode):
        primary_summary = str(primary_compiled.get("summary") or "").strip()
        if primary_summary:
            primary_summary_grounding = _source_grounding_assessment(text, primary_summary, role="claim")
            if not bool(primary_summary_grounding.get("supported")):
                primary_compiled["summary"] = summarize_text(text, limit=180)
                warnings.append(
                    {
                        "code": "source_grounding_primary_summary_rewritten",
                        "message": "Primary preview summary was rewritten because the compiler summary was not grounded in the source text.",
                    }
                )
    primary_semantics = _stabilize_compiled_semantics(
        raw_text=text,
        input_mode=input_mode,
        summary=str(primary_compiled.get("summary") or summarize_text(text, limit=120)),
        memory_type=str(primary_compiled.get("memory_type") or ""),
        guide_area=primary_compiled.get("guide_area"),
        routing_scores=dict(primary_compiled.get("routing_semantic_scores") or {}),
        routing_facets=dict(primary_compiled.get("routing_facets") or {}),
        granularity=primary_compiled.get("granularity"),
        novelty=primary_compiled.get("novelty"),
        trust_compiled=bool(require_ai and compiler_payload),
    )
    primary_compiled = {
        **primary_compiled,
        **{key: value for key, value in primary_semantics.items() if key != "heuristic_projection"},
    }
    compiler_local_correction_plan = _default_local_correction_plan(
        raw_text=text,
        input_mode=input_mode,
        nearby_context=nearby_context,
        memory_type=str(primary_compiled.get("memory_type") or ""),
        guide_area=primary_compiled.get("guide_area"),
        existing_plan=dict((compiler_payload or {}).get("local_correction_plan") or {}),
    )

    compiled_derived = list((compiler_payload or {}).get("derived_nodes") or [])
    compiled_derived, source_grounding_warning = _filter_source_grounded_compiler_nodes(
        source_text=text,
        input_mode=input_mode,
        source_type=source_type,
        compiled_nodes=compiled_derived,
    )
    if source_grounding_warning:
        warnings.append(
            {
                "code": str(source_grounding_warning.get("code") or "source_grounding_filtered"),
                "message": str(source_grounding_warning.get("message") or "Filtered unsupported compiler-derived source nodes."),
            }
        )
    if source_investigation_scope and compiled_derived:
        source_scoped_compiler_nodes = [
            item for item in compiled_derived if str(item.get("source_unit_id") or "").strip()
        ]
        suppressed_free_nodes = len(compiled_derived) - len(source_scoped_compiler_nodes)
        if suppressed_free_nodes:
            warnings.append(
                {
                    "code": "source_free_derivations_suppressed",
                    "message": (
                        f"Suppressed {suppressed_free_nodes} compiler-derived source node(s) without source_unit_id. "
                        "Real source Grow can persist only raw anchors, source facts and source chunks with explicit provenance."
                    ),
                }
            )
        compiled_derived = source_scoped_compiler_nodes
    fallback_derivation_mode = "llm" if compiled_derived else "heuristic"
    claims: list[dict[str, Any]] = []
    entities: list[dict[str, Any]] = []
    if compiled_derived:
        for item in compiled_derived:
            role = str(item.get("derivation_role") or "claim")
            if role == "claim":
                claims.append(
                    {
                        "text": preserve_node_raw_text(str(item.get("raw_text") or item.get("summary") or "").strip(), limit=900),
                        "claim_type": item.get("claim_type") if item.get("claim_type") in CLAIM_TYPES else classify_claim_type(str(item.get("raw_text") or item.get("summary") or "")),
                        "confidence": max(0.0, min(1.0, float(item.get("confidence") or 0.75))),
                        "source_span_start": item.get("source_span_start"),
                        "source_span_end": item.get("source_span_end"),
                    }
                )
            elif role == "entity":
                entities.append(
                    {
                        "text": preserve_node_raw_text(str(item.get("raw_text") or item.get("summary") or "").strip(), limit=320),
                        "entity_type": item.get("entity_type") if item.get("entity_type") in ENTITY_TYPES else classify_entity_type(str(item.get("raw_text") or item.get("summary") or ""), text, input_mode),
                        "confidence": max(0.0, min(1.0, float(item.get("confidence") or 0.8))),
                        "source_span_start": item.get("source_span_start"),
                        "source_span_end": item.get("source_span_end"),
                        "mentioned_in_claim_indexes": [int(idx) for idx in list(item.get("mentioned_in_claim_indexes") or []) if int(idx) >= 0],
                    }
                )
    elif source_investigation_scope or require_ai:
        # An empty AI deduction set is a valid reviewed outcome. V2 must not
        # manufacture deductions through the legacy heuristic path.
        fallback_derivation_mode = "llm" if require_ai else "heuristic"
        derivation_mode = "llm" if require_ai else "heuristic"
    else:
        claims, entities, fallback_derivation_mode, derivation_warnings = derive_structures(
            text,
            input_mode,
            llm_timeout_seconds=derivation_timeout_seconds,
        )
        warnings.extend(derivation_warnings)

    merge_decisions = list((compiler_payload or {}).get("merge_decisions") or [])
    merge_decisions, merge_grounding_warning = _filter_source_grounded_decisions(
        source_text=text,
        input_mode=input_mode,
        source_type=source_type,
        decisions=merge_decisions,
        decision_kind="merge",
    )
    if merge_grounding_warning:
        warnings.append(merge_grounding_warning)
    if not merge_decisions and (source_investigation_scope or require_ai):
        merge_decisions = []
    elif not merge_decisions:
        merge_decisions = heuristic_merge_decisions(
            [text, *[claim["text"] for claim in claims], *[entity["text"] for entity in entities]],
            graph,
        )

    identity_decisions = list((compiler_payload or {}).get("identity_resolution_decisions") or [])
    identity_decisions, identity_grounding_warning = _filter_source_grounded_decisions(
        source_text=text,
        input_mode=input_mode,
        source_type=source_type,
        decisions=identity_decisions,
        decision_kind="identity",
    )
    if identity_grounding_warning:
        warnings.append(identity_grounding_warning)
    if not identity_decisions and (source_investigation_scope or require_ai):
        identity_decisions = []
    elif not identity_decisions:
        identity_decisions = heuristic_identity_decisions(
            [text, *[claim["text"] for claim in claims], *[entity["text"] for entity in entities]],
            identity_nucleus,
            graph,
        )

    def decision_for_text(raw_text: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        normalized = normalize_preview_text(raw_text)
        merge_decision = next((item for item in merge_decisions if normalize_preview_text(str(item.get("source_text") or "")) == normalized), None)
        identity_decision = next((item for item in identity_decisions if normalize_preview_text(str(item.get("source_text") or "")) == normalized), None)
        return merge_decision, identity_decision

    primary_merge, primary_identity = decision_for_text(text)
    primary_seed = build_seed(
        raw_text=text,
        input_mode=input_mode,
        provenance_mode="agvm_lab_preview_primary",
        source_label=source_label,
        source_type=source_type,
        source_uri=primary_source_uri,
        source_ref_id=source_ref_id,
        source_trust=source_trust,
        summary_override=str(primary_compiled.get("summary") or summarize_text(text)),
        memory_type_override=map_runtime_memory_type(primary_compiled.get("memory_type")),
        guide_area_override=primary_compiled.get("guide_area") or infer_guide_area(text),
        document_role="anchor" if str(input_mode or "").strip().lower() == "document" else None,
        document_anchor_id="preview_primary" if str(input_mode or "").strip().lower() == "document" else None,
        routing_scores_override=dict(primary_compiled.get("routing_semantic_scores") or {}),
        routing_facets_override=dict(primary_compiled.get("routing_facets") or {}),
        granularity_override=float(primary_compiled.get("granularity") or 0.5),
        novelty_override=float(primary_compiled.get("novelty") or 0.5),
        memory_confidence=float(primary_compiled.get("memory_confidence") or 0.78),
        identity_resolution_confidence=float((primary_identity or {}).get("confidence") or 0.0) or None,
        evidence_confidence=float(primary_compiled.get("evidence_confidence") or 0.82),
        stability_confidence=float(primary_compiled.get("stability_confidence") or 0.65),
        local_correction_plan=compiler_local_correction_plan,
        persist_mode=_normalize_persist_mode((primary_merge or {}).get("decision")),
        merge_target_node_id=(primary_merge or {}).get("target_node_id"),
        identity_resolution_target_node_id=(primary_identity or {}).get("resolved_node_id"),
        identity_resolution_type=(primary_identity or {}).get("resolution_type"),
        geometry_profile_context=geometry_profile_context,
    )
    primary_node = finalize_node(primary_seed, graph, index_payload, fixed_id="preview_primary")
    primary_hygiene = effective_hygiene(primary_node)
    primary_preview = {
        **primary_node,
        "preview_kind": "primary",
        "preview_label": "Primary memory",
        "preview_confidence": 1.0,
        "selected_by_default": True,
        "persist_mode": primary_seed["persist_mode"],
        "merge_target_node_id": primary_seed.get("merge_target_node_id"),
        "identity_resolution_target_node_id": primary_seed.get("identity_resolution_target_node_id"),
        "identity_resolution_type": primary_seed.get("identity_resolution_type"),
    }

    derived_nodes: list[dict[str, Any]] = []
    derived_edges: list[dict[str, Any]] = []
    claim_nodes: list[dict[str, Any]] = []
    selected_claim_ids: set[str] = set()
    working_graph = {
        **graph,
        "nodes": list(graph.get("nodes") or []),
        "edges": list(graph.get("edges") or []),
    }
    working_index = {
        **index_payload,
        "spatial_index": dict(index_payload.get("spatial_index") or {}),
        "bucket_index": {key: list(value) for key, value in dict(index_payload.get("bucket_index") or {}).items()},
        "highway_index": {key: list(value) for key, value in dict(index_payload.get("highway_index") or {}).items()},
        "document_index": list(index_payload.get("document_index") or []),
        "node_position_map": dict(index_payload.get("node_position_map") or {}),
    }

    if source_investigation_scope and not compiled_derived:
        fallback_derivation_mode = "heuristic"
        warnings.append(
            {
                "code": "source_units_only_derivation",
                "message": "Source investigation preview derives persistible nodes only from traced source units.",
            }
        )

    if not compiled_derived and not source_investigation_scope and not require_ai:
        compiled_derived = [
            {
                "derivation_role": "claim",
                "claim_type": claim["claim_type"],
                "raw_text": claim["text"],
                "summary": summarize_text(claim["text"], limit=120),
                "memory_type": map_runtime_memory_type(None, claim_type=claim["claim_type"]),
                "routing_semantic_scores": heuristic_projection(claim["text"])["routing_semantic_scores"],
                "routing_facets": heuristic_projection(claim["text"])["routing_facets"],
                "confidence": float(claim["confidence"]),
                "memory_confidence": float(claim["confidence"]),
                "evidence_confidence": float(claim["confidence"]),
                "identity_resolution_confidence": None,
                "stability_confidence": 0.6,
                "source_span_start": claim.get("source_span_start"),
                "source_span_end": claim.get("source_span_end"),
            }
            for claim in claims
        ] + [
            {
                "derivation_role": "entity",
                "entity_type": entity["entity_type"],
                "raw_text": entity["text"],
                "summary": summarize_text(entity["text"], limit=72),
                "memory_type": map_runtime_memory_type(None, entity_type=entity["entity_type"]),
                "routing_semantic_scores": heuristic_projection(entity["text"], input_mode="document" if entity["entity_type"] == "document" else "auto")["routing_semantic_scores"],
                "routing_facets": heuristic_projection(entity["text"], input_mode="document" if entity["entity_type"] == "document" else "auto")["routing_facets"],
                "confidence": float(entity["confidence"]),
                "memory_confidence": float(entity["confidence"]),
                "evidence_confidence": float(entity["confidence"]),
                "identity_resolution_confidence": None,
                "stability_confidence": 0.6,
                "mentioned_in_claim_indexes": entity.get("mentioned_in_claim_indexes") or [],
                "source_span_start": entity.get("source_span_start"),
                "source_span_end": entity.get("source_span_end"),
            }
            for entity in entities
        ]
        derivation_mode = fallback_derivation_mode

    source_semantic_items: list[dict[str, Any]] = []
    source_semantic_payload: dict[str, Any] | None = None
    source_semantic_error: str | None = None
    source_semantic_added = 0
    source_semantic_min_viable = 1
    if source_investigation_scope and source_sections:
        source_context_purpose = str(resolved_source_context.get("source_purpose") or "").strip().lower()
        should_run_source_semantic_compiler = (
            require_ai
            or direct_source_unit_preview
            or source_context_purpose == "self_memory"
            or (
                source_section_count >= 4
                and (not compiled_derived or len(compiled_derived) < min(12, max(4, source_section_count // 2)))
            )
        )
        if should_run_source_semantic_compiler:
            semantic_candidate_target = 8
            evidence_char_target = min(32, max(8, math.ceil(len(str(text or "")) / 2800)))
            source_context_purpose = str(resolved_source_context.get("source_purpose") or "").strip().lower()
            if source_context_purpose == "self_memory":
                semantic_candidate_target = (
                    6
                    if source_section_count <= 1 and len(str(text or "")) < 700
                    else min(32, max(10, source_section_count // 2))
                )
            elif direct_source_unit_preview:
                semantic_candidate_target = min(32, max(8, source_section_count))
            else:
                semantic_candidate_target = min(18, max(6, source_section_count * 2))
            semantic_candidate_target = max(semantic_candidate_target, evidence_char_target)
            source_semantic_items, source_semantic_payload, source_semantic_error = llm_source_unit_semantic_compile(
                source_sections=source_sections,
                source_label=source_label,
                source_type=source_type,
                source_trust=source_trust,
                source_purpose=str(resolved_source_context.get("source_purpose") or ""),
                operator_instruction=str(resolved_source_context.get("operator_instruction") or ""),
                identity_nucleus=identity_nucleus,
                nearby_context=nearby_context,
                source_unit_formation=source_unit_formation,
                source_investigation_id=source_investigation_id,
                candidate_target=semantic_candidate_target,
                timeout_seconds=max(12.0, min(float(compiler_timeout_seconds or 45.0), 120.0)),
            )
            compiled_derived, source_semantic_added = _append_source_section_preview_items(compiled_derived, source_semantic_items)
            if source_semantic_added:
                derivation_mode = "llm"
                warnings.append(
                    {
                        "code": "source_unit_semantic_compiler_bridge",
                        "message": f"Added {source_semantic_added} semantic source-unit memory candidate(s) from traced sections.",
                    }
                )
            elif source_semantic_error and source_semantic_error != "llm_disabled":
                warnings.append(
                    {
                        "code": "source_unit_semantic_compiler_fallback",
                        "message": f"Semantic source-unit compiler unavailable, retaining deterministic source bridge: {source_semantic_error}",
                    }
                )
        if direct_source_unit_preview:
            source_semantic_min_viable = min(96, max(65, source_section_count * 2))
        else:
            source_semantic_min_viable = 4 if source_section_count >= 4 else 1

    def has_ai_semantic_vector(item: dict[str, Any]) -> bool:
        scores = dict(item.get("routing_semantic_scores") or {})
        facets = dict(item.get("routing_facets") or {})
        return all(field in scores for field in ROUTING_FIELDS) and all(field in facets for field in FACET_FIELDS)

    source_ai_items_by_unit: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for semantic_item in compiled_derived:
        semantic_unit_id = str(semantic_item.get("source_unit_id") or "").strip()
        if semantic_unit_id and has_ai_semantic_vector(semantic_item):
            source_ai_items_by_unit[semantic_unit_id].append(semantic_item)

    if require_ai and source_investigation_scope and source_sections:
        required_source_unit_ids = {
            str(section.get("section_id") or section.get("unit_id") or "").strip()
            for section in source_sections
            if isinstance(section, dict)
            and bool(section.get("fact_eligible") if section.get("fact_eligible") is not None else True)
            and str(section.get("section_id") or section.get("unit_id") or "").strip()
        }
        missing_source_unit_ids = sorted(required_source_unit_ids - set(source_ai_items_by_unit))
        for repair_attempt in range(2):
            if not missing_source_unit_ids:
                break
            missing_sections = [
                dict(section)
                for section in source_sections
                if isinstance(section, dict)
                and str(section.get("section_id") or section.get("unit_id") or "").strip()
                in set(missing_source_unit_ids)
            ]
            repair_items, _, repair_error = llm_source_unit_semantic_compile(
                source_sections=missing_sections,
                source_label=source_label,
                source_type=source_type,
                source_trust=source_trust,
                source_purpose=str(resolved_source_context.get("source_purpose") or ""),
                operator_instruction=(
                    str(resolved_source_context.get("operator_instruction") or "")
                    + " Cover every supplied source unit; this is a targeted structured repair."
                ),
                identity_nucleus=identity_nucleus,
                nearby_context=nearby_context,
                source_unit_formation=source_unit_formation,
                source_investigation_id=source_investigation_id,
                candidate_target=min(32, max(8, len(missing_sections) * 2)),
                timeout_seconds=max(12.0, min(float(compiler_timeout_seconds or 45.0), 120.0)),
            )
            if repair_error:
                source_semantic_error = repair_error
            compiled_derived, repaired_count = _append_source_section_preview_items(
                compiled_derived,
                repair_items,
            )
            if repaired_count:
                warnings.append(
                    {
                        "code": "source_unit_semantic_compiler_repair",
                        "message": (
                            f"Recovered {repaired_count} source-unit candidate(s) during structured AI repair "
                            f"attempt {repair_attempt + 1}."
                        ),
                    }
                )
                for semantic_item in repair_items:
                    semantic_unit_id = str(semantic_item.get("source_unit_id") or "").strip()
                    if semantic_unit_id and has_ai_semantic_vector(semantic_item):
                        source_ai_items_by_unit[semantic_unit_id].append(semantic_item)
            missing_source_unit_ids = sorted(required_source_unit_ids - set(source_ai_items_by_unit))
        if missing_source_unit_ids:
            reason = source_semantic_error or "provider_did_not_cover_all_fact_eligible_source_units"
            raise ValueError(
                "grow_ai_source_unit_coverage_incomplete:"
                f"{reason}:missing={','.join(missing_source_unit_ids[:12])}"
            )

    source_claim_items: list[dict[str, Any]] = []
    source_claims_added = 0
    source_semantic_sufficient = source_semantic_added >= source_semantic_min_viable
    if not require_ai and not source_semantic_sufficient:
        source_claim_items = _source_section_claim_preview_items(
            source_text=text,
            source_sections=source_sections,
            source_investigation_id=source_investigation_id,
        )
        compiled_derived, source_claims_added = _append_source_section_preview_items(compiled_derived, source_claim_items)
        if source_claims_added:
            warnings.append(
                {
                    "code": "source_unit_atomic_claim_bridge",
                    "message": f"Added {source_claims_added} source-grounded atomic claim preview node(s) from compiler handoff sections.",
                }
            )

    source_qa_items: list[dict[str, Any]] = []
    source_qa_added = 0
    if not require_ai and not source_semantic_sufficient:
        source_qa_items = _source_section_qa_preview_items(
            source_text=text,
            source_sections=source_sections,
            source_investigation_id=source_investigation_id,
        )
        compiled_derived, source_qa_added = _append_source_section_preview_items(compiled_derived, source_qa_items)
        if source_qa_added:
            warnings.append(
                {
                    "code": "source_unit_qa_affordance_bridge",
                    "message": f"Added {source_qa_added} source-grounded QA affordance node(s) from compiler handoff sections.",
                }
            )

    source_micro_chunk_items: list[dict[str, Any]] = []
    source_micro_chunks_added = 0
    if source_investigation_scope and source_sections:
        source_micro_chunk_items = _source_section_micro_chunk_preview_items(
            source_text=text,
            input_mode=input_mode,
            source_sections=source_sections,
            source_investigation_id=source_investigation_id,
        )
        if require_ai:
            ai_scored_chunks: list[dict[str, Any]] = []
            for chunk in source_micro_chunk_items:
                source_unit_id = str(chunk.get("source_unit_id") or "").strip()
                vector_source = (source_ai_items_by_unit.get(source_unit_id) or [None])[0]
                if not isinstance(vector_source, dict):
                    continue
                ai_scored_chunks.append(
                    {
                        **chunk,
                        "routing_semantic_scores": dict(vector_source.get("routing_semantic_scores") or {}),
                        "routing_facets": dict(vector_source.get("routing_facets") or {}),
                        "semantic_vector_source": "ai_source_unit_candidate",
                    }
                )
            source_micro_chunk_items = ai_scored_chunks
        compiled_derived, source_micro_chunks_added = _append_source_section_preview_items(compiled_derived, source_micro_chunk_items)
        if source_micro_chunks_added:
            warnings.append(
                {
                    "code": "source_unit_micro_chunk_bridge",
                    "message": f"Added {source_micro_chunks_added} source-grounded micro chunk node(s) from compiler handoff sections.",
                }
            )

    source_preview_items: list[dict[str, Any]] = []
    source_has_claim_or_chunk_preview = bool(source_semantic_added or source_claims_added or source_qa_added or source_micro_chunks_added)
    if not require_ai and not source_has_claim_or_chunk_preview:
        source_preview_items = _source_section_preview_items(
            source_text=text,
            input_mode=input_mode,
            source_sections=source_sections,
            source_unit_formation=source_unit_formation,
            source_investigation_id=source_investigation_id,
        )
        compiled_derived, source_items_added = _append_source_section_preview_items(compiled_derived, source_preview_items)
        if source_items_added:
            warnings.append(
                {
                    "code": "source_unit_preview_bridge",
                    "message": f"Added {source_items_added} source unit preview node(s) from compiler handoff sections.",
                }
            )

    derived_limit = 12
    if source_investigation_scope or source_has_claim_or_chunk_preview or source_preview_items:
        derived_limit = min(2400, max(12, len(compiled_derived)))

    for index, item in enumerate(compiled_derived[:derived_limit], start=1):
        raw_text_source = str(item.get("raw_text") or item.get("summary") or "").strip()
        context_title = (
            ""
            if str(item.get("source_unit_kind") or "") == "manual_block"
            else str(
                item.get("source_unit_title")
                or item.get("title")
                or source_label
                or primary_compiled.get("summary")
                or "Source context"
            )
        )
        raw_text_source, derived_self_containment = _make_source_unit_text_self_contained(
            text=raw_text_source,
            title=context_title,
        )
        raw_limit = 6000 if str(item.get("entity_type") or "") == "document" or item.get("source_unit_id") else 2400
        raw_text_value = preserve_node_raw_text(raw_text_source, limit=raw_limit)
        if not raw_text_value:
            continue
        role = str(item.get("derivation_role") or "claim")
        preview_id = f"preview_{role}_{index}"
        claim_type = str(item.get("claim_type") or "") if role == "claim" else None
        entity_type = str(item.get("entity_type") or "") if role == "entity" else None
        merge_decision, identity_decision = decision_for_text(summarize_text(raw_text_value, limit=240))
        document_scope = str(input_mode or "").strip().lower() == "document"
        item_document_role = str(item.get("document_role") or "").strip() or None
        item_document_anchor_id = str(item.get("document_anchor_id") or "").strip() or None
        if document_scope and entity_type == "document" and not item_document_role:
            item_document_role = "summary"
            item_document_anchor_id = "preview_primary"
        node_input_mode = "document" if entity_type == "document" and not document_scope else "auto"
        item_semantics = _stabilize_compiled_semantics(
            raw_text=raw_text_value,
            input_mode=node_input_mode,
            summary=str(item.get("summary") or summarize_text(raw_text_value, limit=120)),
            memory_type=str(item.get("memory_type") or ""),
            guide_area=None,
            routing_scores=dict(item.get("routing_semantic_scores") or {}),
            routing_facets=dict(item.get("routing_facets") or {}),
            granularity=None,
            novelty=None,
            trust_compiled=bool(require_ai and compiler_payload),
        )
        item_memory_type = item_semantics["memory_type"]
        if item_document_role == "chunk":
            item_memory_type = "document_chunk"
        elif item_document_role == "summary":
            item_memory_type = "document_summary"
        elif item_document_role == "fact":
            item_memory_type = "document_fact"
        item_document_chunk_index = item.get("document_chunk_index")
        if item_document_chunk_index is not None:
            try:
                item_document_chunk_index = int(item_document_chunk_index)
            except (TypeError, ValueError):
                item_document_chunk_index = None
        memory_type_override = (
            item_memory_type
            if item_document_role in {"chunk", "summary", "fact"}
            else map_runtime_memory_type(item_memory_type, claim_type=claim_type, entity_type=entity_type)
        )
        item_source_unit_id = str(item.get("source_unit_id") or "").strip()
        item_source_section = source_sections_by_id.get(item_source_unit_id, {})
        item_source_uri = sanitize_source_uri_for_persistence(
            item.get("source_uri") or item_source_section.get("source_uri") or primary_source_uri
        )
        seed = build_seed(
            raw_text=raw_text_value,
            input_mode=node_input_mode,
            provenance_mode=f"agvm_lab_preview_{role}",
            source_label=source_label,
            source_type=source_type,
            source_uri=item_source_uri,
            source_ref_id=source_ref_id,
            source_trust=str(primary_hygiene.get("source_trust") or source_trust or "") or None,
            claim_status="test_artifact" if str(primary_hygiene.get("claim_status") or "") == "test_artifact" else None,
            node_kind_hint=claim_type or entity_type or str(item_memory_type or role),
            summary_override=str(item.get("summary") or summarize_text(raw_text_value, limit=120)),
            memory_type_override=memory_type_override,
            derivation_role=role,
            derivation_confidence=float(item.get("confidence") or 0.75),
            derived_from_preview_id="preview_primary",
            document_role=item_document_role,
            document_anchor_id=item_document_anchor_id,
            document_chunk_index=item_document_chunk_index,
            source_unit_id=item_source_unit_id or None,
            source_unit_title=str(item.get("source_unit_title") or "").strip() or None,
            source_unit_kind=str(item.get("source_unit_kind") or "").strip() or None,
            source_unit_role=str(item.get("source_unit_role") or "").strip() or None,
            promotion_role=str(item.get("promotion_role") or "").strip() or None,
            source_unit_formation_strategy=str(item.get("source_unit_formation_strategy") or "").strip() or None,
            source_span_start=item.get("source_span_start"),
            source_span_end=item.get("source_span_end"),
            routing_scores_override=dict(item_semantics["routing_semantic_scores"]),
            routing_facets_override=dict(item_semantics["routing_facets"]),
            memory_confidence=float(item.get("memory_confidence") or item.get("confidence") or 0.75),
            identity_resolution_confidence=float((identity_decision or {}).get("confidence") or item.get("identity_resolution_confidence") or 0.0) or None,
            evidence_confidence=float(item.get("evidence_confidence") or item.get("confidence") or 0.75),
            stability_confidence=float(item.get("stability_confidence") or 0.6),
            local_correction_plan=_default_local_correction_plan(
                raw_text=raw_text_value,
                input_mode=node_input_mode,
                nearby_context=nearby_context,
                memory_type=str(item_memory_type or ""),
                guide_area=item_semantics.get("guide_area"),
                existing_plan=dict((compiler_payload or {}).get("local_correction_plan") or {}),
            ),
            retrieval_affordance=dict(item.get("retrieval_affordance") or {}),
            retrieval_aliases=list(item.get("retrieval_aliases") or []),
            persist_mode=_normalize_persist_mode((merge_decision or {}).get("decision")),
            merge_target_node_id=(merge_decision or {}).get("target_node_id"),
            identity_resolution_target_node_id=(identity_decision or {}).get("resolved_node_id"),
            identity_resolution_type=(identity_decision or {}).get("resolution_type"),
            geometry_profile_context=geometry_profile_context,
        )
        node = finalize_node(seed, working_graph, working_index, fixed_id=preview_id)
        preview_confidence = float(item.get("confidence") or 0.75)
        selected_override = item.get("selected_by_default_override")
        selected_by_default = bool(selected_override) if selected_override is not None else preview_confidence >= (0.72 if role == "claim" else 0.82)
        source_grounding = dict(item.get("source_grounding") or {})
        preview_node = {
            **node,
            "preview_kind": role,
            "preview_label": f"{role.capitalize()} {index}",
            "preview_confidence": preview_confidence,
            "selected_by_default": selected_by_default,
            "persist_mode": seed["persist_mode"],
            "merge_target_node_id": seed.get("merge_target_node_id"),
            "identity_resolution_target_node_id": seed.get("identity_resolution_target_node_id"),
            "identity_resolution_type": seed.get("identity_resolution_type"),
            "source_grounding_status": source_grounding.get("status"),
            "source_grounding_score": source_grounding.get("score"),
            "source_grounding_reasons": _unique_strings([source_grounding.get("reason")], limit=3),
            "self_containment_repair": derived_self_containment,
        }
        derived_nodes.append(preview_node)
        derived_edges.append(
            {
                "source_preview_id": "preview_primary",
                "target_preview_id": preview_id,
                "edge_type": "derives_from",
                "confidence": preview_confidence,
                "reason": f"primary_to_{role}",
            }
        )
        if role == "claim":
            claim_nodes.append(preview_node)
            if selected_by_default:
                selected_claim_ids.add(preview_id)
        else:
            mentioned_claim_ids = []
            for claim_index in item.get("mentioned_in_claim_indexes") or []:
                if 0 <= int(claim_index) < len(claim_nodes):
                    mentioned_claim_ids.append(claim_nodes[int(claim_index)]["id"])
            if not mentioned_claim_ids:
                for claim_node in claim_nodes:
                    if raw_text_value.lower() in str(claim_node["raw_text"]).lower():
                        mentioned_claim_ids.append(claim_node["id"])
            if mentioned_claim_ids and any(claim_id in selected_claim_ids for claim_id in mentioned_claim_ids):
                preview_node["selected_by_default"] = True
            for claim_id in sorted(set(mentioned_claim_ids)):
                derived_edges.append(
                    {
                        "source_preview_id": claim_id,
                        "target_preview_id": preview_id,
                        "edge_type": "mentions_entity",
                        "confidence": preview_confidence,
                        "reason": "claim_mentions_entity",
                    }
                )

        if preview_node["persist_mode"] == "create":
            working_graph["nodes"].append(node)
            working_index["spatial_index"][preview_id] = node
            bucket_key = node["bucket"]["key"]
            working_index["bucket_index"].setdefault(bucket_key, []).append(preview_id)
            if node.get("is_document_anchor"):
                working_index["document_index"].append(preview_id)
            working_index["node_position_map"][preview_id] = node["final_position"]
            for highway in node.get("highways") or []:
                target = str(highway["target_node_id"])
                working_index["highway_index"].setdefault(preview_id, []).append(target)
                working_index["highway_index"].setdefault(target, []).append(preview_id)

    cognitive_write_plan = _build_cognitive_write_plan(
        text=text,
        input_mode=input_mode,
        source_label=source_label,
        source_type=source_type,
        compiler_payload=compiler_payload,
        primary_preview=primary_preview,
        derived_nodes=derived_nodes,
        merge_decisions=merge_decisions,
        identity_decisions=identity_decisions,
        identity_nucleus=identity_nucleus,
        nearby_context=nearby_context,
        question_limit=question_limit,
    )
    primary_preview = _apply_cognitive_write_annotations(primary_preview, cognitive_write_plan)
    derived_nodes = [_apply_cognitive_write_annotations(node, cognitive_write_plan) for node in derived_nodes]
    learning_policy_seed: dict[str, Any] = {}
    if source_investigation_scope:
        source_persist_cap = min(2400, max(128, len(derived_nodes) + 1))
        learning_policy_seed["max_persist_preview_ids"] = source_persist_cap
    learning_policy = _build_learning_policy(
        {
            "primary_node_preview": primary_preview,
            "derived_nodes": derived_nodes,
            "cognitive_write_plan": cognitive_write_plan,
            "learning_policy": learning_policy_seed,
        },
        learning_mode=learning_mode,
        question_limit=question_limit,
        phase="preview",
    )
    primary_preview = _apply_learning_policy_annotations(primary_preview, learning_policy)
    derived_nodes = [_apply_learning_policy_annotations(node, learning_policy) for node in derived_nodes]
    primary_preview, derived_nodes, preview_quality_contract = _apply_source_preview_quality_contract(
        primary_preview=primary_preview,
        derived_nodes=derived_nodes,
        source_type=source_type,
        source_sections=source_sections,
    )

    result = {
        "schema_version": "agvm.grow_preview_bundle.v2" if require_ai else "agvm.grow_preview_bundle.v1",
        "primary_node_preview": primary_preview,
        "derived_nodes": derived_nodes,
        "derived_edges": derived_edges,
        "derivation_mode": derivation_mode,
        "warnings": warnings,
        "merge_decisions": merge_decisions,
        "identity_resolution_decisions": identity_decisions,
        "identity_nucleus": identity_nucleus,
        "preview_quality_contract": preview_quality_contract,
        "cognitive_write_plan": cognitive_write_plan,
        "learning_policy": learning_policy,
        "write_trace": build_write_trace(
            {
                "primary_node_preview": primary_preview,
                "derived_nodes": derived_nodes,
                "derivation_mode": derivation_mode,
                "merge_decisions": merge_decisions,
                "identity_resolution_decisions": identity_decisions,
                "cognitive_write_plan": cognitive_write_plan,
                "learning_policy": learning_policy,
            },
            input_mode=input_mode,
            mode="write_preview",
            identity_nucleus=identity_nucleus,
        ),
    }
    if require_ai:
        attestation = dict(compiler_execution_metadata or {})
        if str(attestation.get("status") or "") != "completed":
            raise ValueError("grow_ai_attestation_missing")
        result["ai_execution_attestation"] = attestation
    return result


def resolve_persist_selection(
    bundle: dict[str, Any],
    selected_preview_ids: list[str],
    *,
    learning_mode: str | None = None,
    clarification_answers: dict[str, str] | list[dict[str, Any]] | None = None,
    approved_preview_ids: list[str] | None = None,
    question_limit: int | None = None,
    include_primary: bool = True,
) -> tuple[list[str], dict[str, Any]]:
    """Resolve the exact preview IDs a persistence call is allowed to write."""
    normalized_bundle = _normalize_persist_selection_bundle(bundle)
    requested_ids = {str(value) for value in selected_preview_ids if str(value)}
    primary_preview_id = str(dict(normalized_bundle.get("primary_node_preview") or {}).get("id") or "").strip()
    if include_primary and primary_preview_id:
        requested_ids.add(primary_preview_id)
    learning_policy = _build_learning_policy(
        normalized_bundle,
        learning_mode=learning_mode,
        selected_preview_ids=list(requested_ids),
        clarification_answers=clarification_answers,
        approved_preview_ids=approved_preview_ids,
        question_limit=question_limit,
        phase="persist",
    )
    persistable_ids = {
        str(value)
        for value in list((learning_policy.get("selection_resolution") or {}).get("persist_preview_ids") or [])
        if str(value)
    }
    preview_nodes = [dict(normalized_bundle.get("primary_node_preview") or {})]
    preview_nodes.extend(
        dict(node) for node in list(normalized_bundle.get("derived_nodes") or []) if isinstance(node, dict)
    )
    resolved_ids = [
        str(node.get("id") or "")
        for node in preview_nodes
        if str(node.get("id") or "") in requested_ids and str(node.get("id") or "") in persistable_ids
    ]
    return resolved_ids, learning_policy


def _normalize_persist_selection_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    def normalized_node(value: Any) -> dict[str, Any]:
        node = dict(value) if isinstance(value, dict) else {}
        node_id = str(node.get("id") or node.get("preview_id") or node.get("node_id") or "").strip()
        return {**node, "id": node_id} if node_id else node

    return {
        **bundle,
        "primary_node_preview": normalized_node(bundle.get("primary_node_preview")),
        "derived_nodes": [
            normalized_node(node) for node in list(bundle.get("derived_nodes") or []) if isinstance(node, dict)
        ],
    }


def persist_selection(
    bundle: dict[str, Any],
    selected_preview_ids: list[str],
    graph: dict[str, Any],
    index_payload: dict[str, Any],
    *,
    learning_mode: str | None = None,
    clarification_answers: dict[str, str] | list[dict[str, Any]] | None = None,
    approved_preview_ids: list[str] | None = None,
    question_limit: int | None = None,
    geometry_profile_context: dict[str, Any] | None = None,
    include_primary: bool = True,
) -> tuple[dict[str, Any], list[str], int, list[str], dict[str, Any]]:
    normalized_bundle = _normalize_persist_selection_bundle(bundle)
    bundle.clear()
    bundle.update(normalized_bundle)
    resolved_ids, learning_policy = resolve_persist_selection(
        bundle,
        selected_preview_ids,
        learning_mode=learning_mode,
        clarification_answers=clarification_answers,
        approved_preview_ids=approved_preview_ids,
        question_limit=question_limit,
        include_primary=include_primary,
    )
    bundle["learning_policy"] = learning_policy
    selected_ids = set(resolved_ids)

    preview_nodes = [bundle["primary_node_preview"], *list(bundle.get("derived_nodes") or [])]
    preview_map = {node["id"]: node for node in preview_nodes if node["id"] in selected_ids}
    primary_preview_id = str(bundle["primary_node_preview"]["id"])
    ordered_nodes = []
    if primary_preview_id in preview_map:
        ordered_nodes.append(preview_map[primary_preview_id])
    ordered_nodes.extend(node for node in preview_nodes if node["id"] != primary_preview_id and node["id"] in preview_map)

    working_graph = {
        **graph,
        "nodes": list(graph.get("nodes") or []),
        "edges": list(graph.get("edges") or []),
    }
    persisted_nodes: list[dict[str, Any]] = []
    preview_to_persisted: dict[str, str] = {}
    current_index = index_payload
    merged_into_existing_ids: list[str] = []

    def _source_identity_key(node: dict[str, Any]) -> tuple[Any, ...] | None:
        source_unit_id = str(node.get("source_unit_id") or "").strip()
        provenance = dict(node.get("provenance") or {})
        source_ref_id = str(provenance.get("source_ref_id") or "").strip()
        if not source_unit_id or not source_ref_id:
            return None
        node_kind = str(node.get("node_kind") or "").strip()
        memory_type = str(node.get("memory_type") or "").strip()
        document_role = str(node.get("document_role") or "").strip()
        if not document_role and (node_kind == "document_anchor" or memory_type == "document_anchor"):
            document_role = "anchor"
        return (
            source_unit_id,
            source_ref_id,
            str(provenance.get("mode") or "").strip(),
            node_kind,
            memory_type,
            document_role,
            node.get("document_chunk_index"),
            " ".join(str(node.get("raw_text") or "").split()),
            " ".join(str(node.get("summary") or "").split()),
        )

    existing_by_source_identity: dict[tuple[Any, ...], str] = {}
    for existing_node in list(working_graph.get("nodes") or []):
        if not isinstance(existing_node, dict):
            continue
        identity_key = _source_identity_key(existing_node)
        existing_id = str(existing_node.get("id") or "").strip()
        if identity_key is not None and existing_id:
            existing_by_source_identity.setdefault(identity_key, existing_id)

    def _merge_target_is_compatible(preview_node: dict[str, Any], target_node_id: str) -> bool:
        target = next(
            (
                dict(node)
                for node in list(working_graph.get("nodes") or [])
                if str(node.get("id") or "") == str(target_node_id)
            ),
            {},
        )
        if not target:
            return False
        preview_memory_type = str(preview_node.get("memory_type") or "").strip().lower()
        preview_role = str(preview_node.get("document_role") or "").strip().lower()
        preview_kind = str(preview_node.get("node_kind") or "").strip().lower()
        target_memory_type = str(target.get("memory_type") or "").strip().lower()
        target_role = str(target.get("document_role") or "").strip().lower()
        target_is_anchor = bool(target.get("is_document_anchor")) or target_memory_type == "document_anchor" or target_role == "anchor"
        preview_is_document_material = (
            bool(preview_node.get("is_document_anchor"))
            or preview_memory_type in {"document_anchor", "document_chunk", "document_fact", "document_summary"}
            or preview_role in {"anchor", "chunk", "fact", "summary"}
            or bool(preview_node.get("source_unit_id"))
        )
        if target_is_anchor and not preview_is_document_material:
            return False
        if preview_memory_type == "relational" or preview_kind == "relationship_claim":
            if target_memory_type not in {"relational", "identity", "identity_style"}:
                return False
        if preview_memory_type == "value" and target_memory_type not in {"value", "identity_style", "identity"}:
            return False
        return True

    for preview_node in ordered_nodes:
        source_identity_key = _source_identity_key(preview_node)
        existing_source_node_id = (
            existing_by_source_identity.get(source_identity_key)
            if source_identity_key is not None
            else None
        )
        if existing_source_node_id:
            preview_to_persisted[preview_node["id"]] = existing_source_node_id
            merged_into_existing_ids.append(existing_source_node_id)
            continue
        persist_mode = str(preview_node.get("persist_mode") or "create")
        merge_target_node_id = preview_node.get("merge_target_node_id")
        if persist_mode in {"merge_into_existing", "attach_as_alias_or_variant"} and merge_target_node_id:
            if not _merge_target_is_compatible(preview_node, str(merge_target_node_id)):
                preview_node = {
                    **dict(preview_node),
                    "persist_mode": "create",
                    "merge_target_node_id": None,
                    "merge_guard": {
                        "blocked": True,
                        "reason": "incompatible_merge_target",
                        "target_node_id": str(merge_target_node_id),
                    },
                }
                persist_mode = "create"
                merge_target_node_id = None
            else:
                preview_to_persisted[preview_node["id"]] = str(merge_target_node_id)
                merged_into_existing_ids.append(str(merge_target_node_id))
                if preview_node["id"] != bundle["primary_node_preview"]["id"]:
                    primary_target = preview_to_persisted.get(bundle["primary_node_preview"]["id"])
                    if not primary_target:
                        continue
                    if primary_target == str(merge_target_node_id):
                        continue
                    working_graph["edges"].append(
                        {
                            "source_node_id": primary_target,
                            "target_node_id": str(merge_target_node_id),
                            "edge_type": "derives_from",
                            "confidence": float(preview_node.get("preview_confidence") or preview_node.get("derivation_confidence") or 0.75),
                            "reason": persist_mode,
                        }
                    )
                continue
        if persist_mode in {"merge_into_existing", "attach_as_alias_or_variant"} and merge_target_node_id:
            preview_to_persisted[preview_node["id"]] = str(merge_target_node_id)
            merged_into_existing_ids.append(str(merge_target_node_id))
            if preview_node["id"] != bundle["primary_node_preview"]["id"]:
                primary_target = preview_to_persisted.get(bundle["primary_node_preview"]["id"])
                if not primary_target:
                    continue
                if primary_target == str(merge_target_node_id):
                    continue
                working_graph["edges"].append(
                    {
                        "source_node_id": primary_target,
                        "target_node_id": str(merge_target_node_id),
                        "edge_type": "derives_from",
                        "confidence": float(preview_node.get("preview_confidence") or preview_node.get("derivation_confidence") or 0.75),
                        "reason": persist_mode,
                    }
                )
            continue
        seed = {
            key: value
            for key, value in preview_node.items()
            if key
            not in {
                "id",
                "final_position",
                "topology_brainhex",
                "topology_color",
                "bucket",
                "links",
                "highways",
                "debug",
                "preview_kind",
                "preview_label",
                "selected_by_default",
                "preview_confidence",
                "persist_mode",
                "merge_target_node_id",
                "identity_resolution_target_node_id",
                "identity_resolution_type",
                "memory_act_type",
                "cognitive_status",
                "requires_human_review",
                "cognitive_review_reasons",
                "cognitive_target_node_ids",
                "learning_mode",
                "learning_action",
                "learning_question_ids",
                "learning_policy_reasons",
                "source_grounding_status",
                "source_grounding_score",
                "source_grounding_reasons",
                "self_containment_repair",
            }
        }
        derived_from_ref = str(seed.get("derived_from_preview_id") or "").strip()
        if derived_from_ref in preview_to_persisted:
            seed["derived_from_preview_id"] = preview_to_persisted[derived_from_ref]
        anchor_ref = str(seed.get("document_anchor_id") or "").strip()
        if anchor_ref in preview_to_persisted:
            seed["document_anchor_id"] = preview_to_persisted[anchor_ref]
        seed = apply_public_v1_geometry_profile_to_seed(seed, geometry_profile_context)
        persisted = finalize_node(seed, working_graph, current_index)
        if str(persisted.get("document_role") or "") == "anchor":
            persisted["document_anchor_id"] = persisted["id"]
        elif str(persisted.get("document_anchor_id") or "") in {"preview_primary", str(preview_node.get("id") or "")}:
            mapped_anchor = preview_to_persisted.get(str(preview_node.get("document_anchor_id") or ""))
            if mapped_anchor:
                persisted["document_anchor_id"] = mapped_anchor
        preview_to_persisted[preview_node["id"]] = persisted["id"]
        working_graph["nodes"].append(persisted)
        persisted_nodes.append(persisted)
        persisted_source_identity_key = _source_identity_key(persisted)
        if persisted_source_identity_key is not None:
            existing_by_source_identity[persisted_source_identity_key] = str(persisted["id"])
        current_index = {
            **current_index,
            "spatial_index": {**dict(current_index.get("spatial_index") or {}), persisted["id"]: node_for_index(persisted)},
            "bucket_index": dict(current_index.get("bucket_index") or {}),
            "highway_index": {key: list(value) for key, value in dict(current_index.get("highway_index") or {}).items()},
            "document_index": list(current_index.get("document_index") or []),
            "node_position_map": {**dict(current_index.get("node_position_map") or {}), persisted["id"]: persisted["final_position"]},
        }
        bucket_key = persisted["bucket"]["key"]
        current_index["bucket_index"].setdefault(bucket_key, []).append(persisted["id"])
        if persisted.get("is_document_anchor"):
            current_index["document_index"].append(persisted["id"])
        for highway in persisted.get("highways") or []:
            target = str(highway["target_node_id"])
            current_index["highway_index"].setdefault(persisted["id"], []).append(target)
            current_index["highway_index"].setdefault(target, []).append(persisted["id"])

    edge_count = 0
    existing_edge_keys = {
        (
            str(edge.get("source_node_id") or edge.get("source") or edge.get("source_id") or ""),
            str(edge.get("target_node_id") or edge.get("target") or edge.get("target_id") or ""),
            str(edge.get("edge_type") or edge.get("type") or ""),
        )
        for edge in list(working_graph.get("edges") or [])
        if isinstance(edge, dict)
    }
    for edge in list(bundle.get("derived_edges") or []):
        source_id = preview_to_persisted.get(edge["source_preview_id"])
        target_id = preview_to_persisted.get(edge["target_preview_id"])
        if not source_id or not target_id:
            continue
        edge_key = (str(source_id), str(target_id), str(edge["edge_type"]))
        if edge_key in existing_edge_keys:
            continue
        working_graph["edges"].append(
            {
                "source_node_id": source_id,
                "target_node_id": target_id,
                "edge_type": edge["edge_type"],
                "confidence": edge["confidence"],
                "reason": edge["reason"],
            }
        )
        existing_edge_keys.add(edge_key)
        edge_count += 1

    return working_graph, [node["id"] for node in persisted_nodes], edge_count, list(dict.fromkeys(merged_into_existing_ids)), current_index
