# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import re
import unicodedata
from typing import Any


TARGET_DOCUMENT_NEED_CONTRACT_SCHEMA_VERSION = "agvm.target_document_need_contract.v1"
_EXPLICIT_DOCUMENT_TOOLS = {
    "retrieve_document",
    "retrieve_document_workspace",
    "retrieve_project_workspace",
    "retrieve_source_trace",
}

_NORMAL_CONTEXT_SLOTS = (
    "identity",
    "work",
    "work_detail",
    "work_company",
    "company_founding",
    "project",
    "relationships",
    "relation_detail",
    "relationship",
    "family",
    "place",
    "location",
    "style",
    "values",
    "history",
    "temporal",
    "temporal_inventory",
    "private_identifier",
    "personal_contact",
    "exact_user_field",
)


def _fold_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_only = "".join(char for char in normalized if not unicodedata.combining(char))
    sanitized = re.sub(r"[^\w\s]", " ", ascii_only.lower())
    return " ".join(sanitized.strip().split())


def _unique(values: list[Any], *, limit: int = 32) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        output.append(text)
        seen.add(text)
        if len(output) >= limit:
            break
    return output


def _has_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers if marker)


def _has_word(text: str, words: tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{re.escape(word)}\b", text) for word in words if word)


_DOCUMENT_MARKERS = (
    "document",
    "documents",
    "documento",
    "documenti",
    "doc",
    "file",
    "pdf",
    "docx",
    "paper",
    "papers",
    "articolo",
    "articoli",
    "source",
    "sources",
    "fonte",
    "fonti",
    "citation",
    "citations",
    "citazione",
    "citazioni",
    "bibliografia",
    "reference",
    "references",
    "riferimento",
    "riferimenti",
)

_SOURCE_TRACE_MARKERS = (
    "source trace",
    "source traces",
    "provenance",
    "provenienza",
    "trace source",
    "traccia fonte",
    "traccia fonti",
    "fonti supportano",
    "fonti dimostrano",
    "fonti provano",
    "fonti confermano",
    "documenti supportano",
    "documenti dimostrano",
    "documenti provano",
    "documenti confermano",
    "documenti lo dimostrano",
    "documenti lo provano",
    "documenti lo confermano",
    "quali fonti",
    "quali documenti",
    "sources support",
    "sources prove",
    "sources confirm",
    "documents support",
    "documents prove",
    "documents confirm",
    "what sources",
    "which sources",
    "cite",
    "cita",
    "citami",
    "with citations",
    "con citazioni",
)

_RELATED_DOCUMENT_MARKERS = (
    "related documents",
    "related docs",
    "similar documents",
    "similar docs",
    "documenti correlati",
    "documenti collegati",
    "documenti relativi",
    "documenti simili",
    "fonti correlate",
    "fonti collegate",
    "altri documenti",
    "other documents",
    "nearby documents",
)

_EXACT_DOCUMENT_MARKERS = (
    "document id",
    "doc id",
    "id documento",
    "id del documento",
    "documento id",
    "exact document",
    "documento esatto",
    "apri il documento",
    "apri documento",
    "open document",
    "retrieve document",
    "testo integrale",
    "full document",
    "raw document",
    "documento completo",
    "titolo documento",
    "document title",
)

_PROJECT_DOCUMENT_MARKERS = (
    "project document",
    "project docs",
    "project workspace",
    "workspace document",
    "documenti del progetto",
    "documentazione progetto",
    "documentazione del progetto",
    "readme",
    "changelog",
    "runbook",
    "manual",
    "manuale",
    "specifica",
    "specification",
    "deploy",
    "deployment",
)

_NORMAL_CONTEXT_MARKERS = (
    "chi e",
    "who is",
    "spiegami",
    "explain",
    "raccontami",
    "parlami",
    "tell me about",
    "profilo",
    "profile",
    "persona",
    "person",
    "lavoro",
    "work",
    "aziende",
    "companies",
    "relazione",
    "relationship",
    "stile",
    "style",
    "valori",
    "values",
    "famiglia",
    "family",
)

_FIRST_OR_SECOND_PERSON_MARKERS = (
    "tu",
    "te",
    "ti",
    "tuo",
    "tua",
    "tuoi",
    "tue",
    "sei",
    "hai",
    "i am",
    "my ",
    "your ",
    "you ",
    "who are you",
)

_QUESTION_MARKERS = (
    "chi",
    "cosa",
    "come",
    "quando",
    "dove",
    "perche",
    "quale",
    "quali",
    "dimmi",
    "raccontami",
    "parlami",
    "who",
    "what",
    "when",
    "where",
    "why",
    "how",
    "tell",
    "explain",
    "show",
)

_SCIENTIFIC_MARKERS = (
    "biomaterial",
    "biomaterials",
    "biomedical",
    "biomarker",
    "biomarkers",
    "cell",
    "cells",
    "protein",
    "proteins",
    "gene",
    "genes",
    "expression",
    "enzyme",
    "receptor",
    "molecule",
    "molecular",
    "cancer",
    "tumor",
    "disease",
    "patient",
    "patients",
    "clinical",
    "trial",
    "study",
    "studies",
    "therapy",
    "treatment",
    "drug",
    "vaccine",
    "infection",
    "inflammation",
    "metabolism",
    "neural",
    "neurons",
    "genetic",
    "genome",
    "genomes",
    "genomic",
    "sequence",
    "sequences",
    "variation",
    "variant",
    "variants",
    "mutation",
    "mutations",
    "penetrance",
    "prevalence",
    "prion",
    "prp",
    "positivity",
    "associated",
    "correlated",
    "induces",
    "induce",
    "inhibits",
    "inhibit",
    "increases",
    "increase",
    "decreases",
    "decrease",
    "reduces",
    "reduce",
    "causes",
    "cause",
    "prevents",
    "prevent",
    "show",
    "shows",
    "exhibit",
    "exhibits",
    "enable",
    "enables",
    "properties",
)

_LEGAL_POLICY_MARKERS = (
    "policy",
    "policies",
    "regulation",
    "regulations",
    "law",
    "laws",
    "legal",
    "contract",
    "clause",
    "terms",
    "privacy",
    "retention",
    "compliance",
    "gdpr",
    "hipaa",
    "licence",
    "license",
    "must",
    "shall",
    "requires",
    "require",
    "required",
    "normativa",
    "contratto",
    "clausola",
    "policy",
    "privacy",
    "conservazione",
    "cancellazione",
    "obbligo",
    "deve",
    "devono",
)

_DECLARATIVE_VERBS = (
    "is",
    "are",
    "was",
    "were",
    "has",
    "have",
    "show",
    "shows",
    "exhibit",
    "exhibits",
    "enable",
    "enables",
    "consist",
    "consists",
    "increase",
    "increases",
    "decrease",
    "decreases",
    "reduce",
    "reduces",
    "cause",
    "causes",
    "prevent",
    "prevents",
    "support",
    "supports",
    "require",
    "requires",
    "must",
    "shall",
    "e",
    "sono",
    "ha",
    "hanno",
    "mostra",
    "mostrano",
    "riduce",
    "riducono",
    "aumenta",
    "aumentano",
    "causa",
    "causano",
    "richiede",
    "richiedono",
    "deve",
    "devono",
)


def _looks_like_exact_document_request(folded: str, query_text: str) -> bool:
    if _has_any(folded, _EXACT_DOCUMENT_MARKERS):
        return True
    if re.search(r"\b(?:doc|document)[_:-][a-z0-9][a-z0-9_-]{2,}\b", folded):
        return True
    if re.search(r"\bdoc[0-9][a-z0-9_-]{2,}\b", folded):
        return True
    if _has_any(folded, ("document", "documento", "source", "fonte")) and re.search(r"\b(?:id|uuid|title|titolo)\b", folded):
        return True
    return bool(_has_any(folded, ("document", "documento")) and re.search(r"[\"“”].{8,}[\"“”]", str(query_text or "")))


def _looks_like_personal_context(folded: str) -> bool:
    return bool(_has_any(folded, _NORMAL_CONTEXT_MARKERS) or _has_word(folded, _FIRST_OR_SECOND_PERSON_MARKERS))


def _looks_like_external_declarative_claim(folded: str, query_text: str) -> tuple[bool, str]:
    if not folded:
        return False, ""
    tokens = folded.split()
    if len(tokens) < 5:
        return False, ""
    if "?" in str(query_text or ""):
        return False, ""
    if _looks_like_personal_context(folded) and not _has_any(folded, _DOCUMENT_MARKERS + _SOURCE_TRACE_MARKERS):
        return False, ""
    if not _has_word(folded, _DECLARATIVE_VERBS):
        return False, ""
    if _has_any(folded, _SCIENTIFIC_MARKERS):
        return True, "scientific_claim"
    if _has_any(folded, _LEGAL_POLICY_MARKERS):
        return True, "legal_policy_claim"
    return False, ""


def build_target_document_need_contract(
    query_text: str,
    *,
    legacy_contract: dict[str, Any] | None = None,
    tool_name: str | None = None,
) -> dict[str, Any]:
    """Classify whether a query needs document evidence before normal context slots.

    The contract deliberately preserves the original query string as the ranking
    target. Downstream code may rewrite display text, but ranking/judging must
    keep this exact text available.
    """

    original_query = str(query_text or "")
    folded = _fold_text(original_query)
    legacy = dict(legacy_contract or {})
    legacy_kind = str(legacy.get("query_kind") or legacy.get("intent") or "").strip()
    normalized_tool = str(tool_name or "").strip()
    explicit_document_tool = normalized_tool in _EXPLICIT_DOCUMENT_TOOLS
    reason_codes: list[str] = []

    document_workspace_tool = normalized_tool == "retrieve_document_workspace"
    project_workspace_tool = normalized_tool == "retrieve_project_workspace"
    explicit_document = bool(explicit_document_tool and (_has_any(folded, _DOCUMENT_MARKERS) or legacy_kind == "document_lookup"))
    source_trace = bool(normalized_tool == "retrieve_source_trace" or (explicit_document_tool and _has_any(folded, _SOURCE_TRACE_MARKERS)))
    exact_document = bool(normalized_tool == "retrieve_document" or (explicit_document_tool and _looks_like_exact_document_request(folded, original_query)))
    related_documents = bool(explicit_document_tool and _has_any(folded, _RELATED_DOCUMENT_MARKERS))
    project_documents = bool(project_workspace_tool or (explicit_document_tool and _has_any(folded, _PROJECT_DOCUMENT_MARKERS)))
    personal_context = _looks_like_personal_context(folded)
    declarative_claim, declarative_need_type = (
        _looks_like_external_declarative_claim(folded, original_query)
        if explicit_document_tool
        else (False, "")
    )

    if legacy_kind == "document_lookup":
        reason_codes.append("legacy_query_kind_document_lookup")
    if explicit_document:
        reason_codes.append("explicit_document_surface")
    if source_trace:
        reason_codes.append("source_trace_surface")
    if exact_document:
        reason_codes.append("exact_document_surface")
    if document_workspace_tool:
        reason_codes.append("document_workspace_tool")
    if project_workspace_tool:
        reason_codes.append("project_workspace_tool")
    if related_documents:
        reason_codes.append("related_documents_surface")
    if project_documents:
        reason_codes.append("project_document_surface")
    if declarative_claim:
        reason_codes.append(declarative_need_type)
    if personal_context:
        reason_codes.append("normal_context_surface")

    document_evidence = bool(
        explicit_document_tool
        and (
            document_workspace_tool
            or project_workspace_tool
            or explicit_document
            or source_trace
            or exact_document
            or related_documents
            or project_documents
            or declarative_claim
        )
    )
    mixed = bool(document_evidence and personal_context and not exact_document and not declarative_claim)

    if not document_evidence:
        classification = "normal_context"
        need_type = "normal_context"
        semantic_document_mode = "none"
    elif mixed:
        classification = "mixed_context_documents"
        need_type = "mixed_context_documents"
        semantic_document_mode = "source_trace_for_answer" if source_trace else "related_document_lookup" if related_documents else "document_synthesis"
    elif exact_document:
        classification = "exact_document_hydration"
        need_type = "exact_document_id_or_title"
        semantic_document_mode = "exact_document_lookup"
    elif related_documents:
        classification = "related_documents"
        need_type = "related_documents"
        semantic_document_mode = "related_document_lookup"
    elif source_trace:
        classification = "source_trace"
        need_type = "source_trace"
        semantic_document_mode = "source_trace_for_answer"
    elif project_documents:
        classification = "pure_document_evidence"
        need_type = "project_document_request"
        semantic_document_mode = "related_document_lookup"
    elif document_workspace_tool:
        classification = "pure_document_evidence"
        need_type = declarative_need_type or "document_workspace_request"
        semantic_document_mode = "source_trace_for_answer"
    elif declarative_claim:
        classification = "pure_document_evidence"
        need_type = declarative_need_type or "external_claim"
        semantic_document_mode = "source_trace_for_answer"
    else:
        classification = "pure_document_evidence"
        need_type = "document_evidence"
        semantic_document_mode = "source_trace_for_answer"

    pure_document_evidence = bool(document_evidence and classification != "mixed_context_documents")
    normal_context_required = bool(not document_evidence or classification == "mixed_context_documents")
    required_slot_override = ["document"] if pure_document_evidence else []
    ensure_required_slots = ["document"] if document_evidence and not pure_document_evidence else []
    suppressed_normal_slots = list(_NORMAL_CONTEXT_SLOTS) if pure_document_evidence else []

    target_document_need = {
        "schema_version": "agvm.target_document_need.v1",
        "need_type": need_type,
        "classification": classification,
        "original_query": original_query,
        "preserved_query_text": original_query,
        "ranking_target_text": original_query,
        "display_text": original_query.strip(),
        "semantic_document_mode": semantic_document_mode,
        "requires_raw_hydration": bool(classification == "exact_document_hydration"),
        "requires_related_refs": bool(classification in {"related_documents", "mixed_context_documents"}),
        "requires_source_trace": bool(semantic_document_mode == "source_trace_for_answer"),
    }

    return {
        "schema_version": TARGET_DOCUMENT_NEED_CONTRACT_SCHEMA_VERSION,
        "classification": classification,
        "need_type": need_type,
        "document_evidence": document_evidence,
        "pure_document_evidence": pure_document_evidence,
        "normal_context_required": normal_context_required,
        "semantic_document_mode": semantic_document_mode,
        "target_document_need": target_document_need if document_evidence else None,
        "required_slot_override": required_slot_override,
        "ensure_required_slots": ensure_required_slots,
        "suppressed_normal_slots": suppressed_normal_slots,
        "allowed_required_slots": ["document"] if pure_document_evidence else [],
        "reason_codes": _unique(reason_codes, limit=16),
        "audit": {
            "why": classification,
            "reason_codes": _unique(reason_codes, limit=16),
            "preserved_ranking_target": original_query,
            "normal_context_required": normal_context_required,
            "normal_slots_suppressed": suppressed_normal_slots,
        },
    }
