# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
import threading
import time
import unicodedata
from typing import Any

from exact_field_contract import (
    EXACT_FIELD_SLOT_IDS,
    exact_field_semantic_slot_contract,
    extract_exact_user_field_request,
)
from document_need_contract import build_target_document_need_contract
from llm import compiler_model, llm_enabled, structured_json
from runtime_scope import current_data_dir


def _fold_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_only = "".join(char for char in normalized if not unicodedata.combining(char))
    sanitized = re.sub(r"[^\w\s]", " ", ascii_only.lower())
    return " ".join(sanitized.strip().split())


def _unique(values: list[Any], *, limit: int = 16) -> list[str]:
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


_SEMANTIC_CONTRACT_CACHE_TTL_SECONDS = 30 * 60
_SEMANTIC_CONTRACT_CACHE_MAX_ITEMS = 128
_SEMANTIC_CONTRACT_DISK_CACHE_FILENAME = "semantic_contract_cache.v1.json"
_SEMANTIC_CONTRACT_DISK_CACHE_SCHEMA = "agvm.semantic_contract_cache_store.v1"
_SEMANTIC_CONTRACT_DISK_CACHE_MAX_ITEMS = 512
_SEMANTIC_CONTRACT_CACHE_REVISION = "dwe2_target_document_need_v1"
_SEMANTIC_CONTRACT_CACHE_LOCK = threading.Lock()
_SEMANTIC_CONTRACT_CACHE: dict[str, dict[str, Any]] = {}


def _stable_json_hash(payload: Any) -> str:
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _semantic_cache_contract_view(legacy_contract: dict[str, Any], fallback_contract: dict[str, Any]) -> dict[str, Any]:
    legacy = dict(legacy_contract or {})
    fallback = dict(fallback_contract or {})
    legacy_context = dict(legacy.get("context_contract") or {})
    fallback_context = dict(fallback.get("context_contract") or {})
    target_need = dict(fallback.get("target_document_need_contract") or {})
    return {
        "mcp_tool_name": fallback.get("mcp_tool_name"),
        "legacy_query_kind": legacy.get("query_kind") or legacy.get("intent") or legacy.get("kind"),
        "legacy_required_slots": list(legacy.get("required_slots") or []),
        "legacy_optional_slots": list(legacy.get("optional_slots") or []),
        "legacy_required_semantic_slots": list(legacy.get("required_semantic_slots") or []),
        "legacy_optional_semantic_slots": list(legacy.get("optional_semantic_slots") or []),
        "legacy_requested_aspects": list(legacy.get("requested_aspects") or []),
        "legacy_requested_relations": list(legacy.get("requested_relations") or []),
        "legacy_required_sections": list(legacy_context.get("required_sections") or []),
        "fallback_intent": dict(fallback.get("intent") or {}),
        "fallback_required_sections": list(fallback_context.get("required_sections") or []),
        "fallback_required_semantic_slots": list(fallback_context.get("semantic_required_slot_keys") or []),
        "fallback_document_mode": dict(fallback.get("document_contract") or {}).get("mode"),
        "fallback_target_document_need_classification": target_need.get("classification"),
        "fallback_target_document_need_type": target_need.get("need_type"),
        "fallback_target_document_need_document_evidence": bool(target_need.get("document_evidence")),
        "fallback_target_document_need_pure": bool(target_need.get("pure_document_evidence")),
        "fallback_ai_required": bool(fallback.get("ai_required")),
    }


def _semantic_cache_identity_view(identity_hints: dict[str, Any] | None) -> dict[str, Any]:
    hints = _sanitize_identity_hints(identity_hints)

    def stable_candidates(key: str) -> list[str]:
        values = [str(item or "").strip() for item in list(hints.get(key) or []) if str(item or "").strip()]
        unique_by_fold: dict[str, str] = {}
        for value in values:
            unique_by_fold.setdefault(_fold_text(value), value)
        return [unique_by_fold[key] for key in sorted(unique_by_fold)]

    return {
        "core_name": str(hints.get("core_name") or "").strip() or None,
        "aliases": stable_candidates("aliases"),
        "self_name_candidates": stable_candidates("self_name_candidates"),
        "partner_candidates": stable_candidates("partner_candidates"),
        "mentor_candidates": stable_candidates("mentor_candidates"),
        "sibling_candidates": stable_candidates("sibling_candidates"),
        "role_candidates": stable_candidates("role_candidates"),
        "project_candidates": stable_candidates("project_candidates"),
        "employer_candidates": stable_candidates("employer_candidates"),
    }


def _semantic_contract_cache_key(
    *,
    query_text: str,
    retrieval_mode: str,
    legacy_contract: dict[str, Any],
    fallback_contract: dict[str, Any],
    identity_hints: dict[str, Any] | None,
    brain_revision: str | None,
    cache_scope: str | None,
) -> str:
    payload = {
        "schema_version": "agvm.semantic_contract_cache_key.v1",
        "cache_revision": _SEMANTIC_CONTRACT_CACHE_REVISION,
        "normalized_intent": _fold_text(query_text),
        "retrieval_mode": str(retrieval_mode or "balanced").strip().lower(),
        "brain_revision": str(brain_revision or "").strip(),
        "cache_scope": str(cache_scope or "").strip(),
        "contract_view": _semantic_cache_contract_view(legacy_contract, fallback_contract),
        "identity_hints_hash": _stable_json_hash(_semantic_cache_identity_view(identity_hints))[:24],
    }
    return _stable_json_hash(payload)


def _prune_semantic_contract_cache(now: float) -> None:
    expired = [
        key
        for key, entry in _SEMANTIC_CONTRACT_CACHE.items()
        if now - float(entry.get("stored_at") or now) > _SEMANTIC_CONTRACT_CACHE_TTL_SECONDS
    ]
    for key in expired:
        _SEMANTIC_CONTRACT_CACHE.pop(key, None)
    if len(_SEMANTIC_CONTRACT_CACHE) <= _SEMANTIC_CONTRACT_CACHE_MAX_ITEMS:
        return
    ordered = sorted(
        _SEMANTIC_CONTRACT_CACHE.items(),
        key=lambda item: float(item[1].get("last_access_at") or item[1].get("stored_at") or 0.0),
    )
    for key, _entry in ordered[: max(0, len(_SEMANTIC_CONTRACT_CACHE) - _SEMANTIC_CONTRACT_CACHE_MAX_ITEMS)]:
        _SEMANTIC_CONTRACT_CACHE.pop(key, None)


def _semantic_contract_cache_path() -> Any:
    return current_data_dir() / _SEMANTIC_CONTRACT_DISK_CACHE_FILENAME


def _cache_entry_is_success(entry: dict[str, Any], now: float) -> bool:
    runtime = dict(entry.get("runtime") or {})
    if now - float(entry.get("stored_at") or now) > _SEMANTIC_CONTRACT_CACHE_TTL_SECONDS:
        return False
    return bool(runtime.get("material")) and str(runtime.get("source") or "") == "llm"


def _load_semantic_contract_disk_cache(now: float) -> dict[str, Any]:
    path = _semantic_contract_cache_path()
    try:
        if not path.exists():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    entries = dict(raw.get("entries") or {}) if isinstance(raw, dict) else {}
    valid_entries = {
        str(key): dict(entry)
        for key, entry in entries.items()
        if isinstance(entry, dict) and _cache_entry_is_success(dict(entry), now)
    }
    return valid_entries


def _write_semantic_contract_disk_cache(entries: dict[str, Any]) -> None:
    path = _semantic_contract_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        ordered = sorted(
            entries.items(),
            key=lambda item: float(dict(item[1]).get("last_access_at") or dict(item[1]).get("stored_at") or 0.0),
        )
        if len(ordered) > _SEMANTIC_CONTRACT_DISK_CACHE_MAX_ITEMS:
            ordered = ordered[-_SEMANTIC_CONTRACT_DISK_CACHE_MAX_ITEMS:]
        payload = {
            "schema_version": _SEMANTIC_CONTRACT_DISK_CACHE_SCHEMA,
            "stored_at": time.time(),
            "ttl_seconds": _SEMANTIC_CONTRACT_CACHE_TTL_SECONDS,
            "max_items": _SEMANTIC_CONTRACT_DISK_CACHE_MAX_ITEMS,
            "entries": {str(key): value for key, value in ordered},
        }
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp_path.replace(path)
    except Exception:
        return


def clear_semantic_contract_cache(*, clear_disk: bool = True) -> None:
    with _SEMANTIC_CONTRACT_CACHE_LOCK:
        _SEMANTIC_CONTRACT_CACHE.clear()
        if clear_disk:
            try:
                _semantic_contract_cache_path().unlink(missing_ok=True)
            except Exception:
                return


def _get_semantic_contract_cache_entry(cache_key: str) -> dict[str, Any] | None:
    now = time.time()
    with _SEMANTIC_CONTRACT_CACHE_LOCK:
        _prune_semantic_contract_cache(now)
        entry = _SEMANTIC_CONTRACT_CACHE.get(cache_key)
        if not entry:
            disk_entries = _load_semantic_contract_disk_cache(now)
            disk_entry = disk_entries.get(cache_key)
            if not disk_entry:
                return None
            disk_entry["last_access_at"] = now
            disk_entry["hit_count"] = int(disk_entry.get("hit_count") or 0) + 1
            disk_entry["cache_tier"] = "disk"
            _SEMANTIC_CONTRACT_CACHE[cache_key] = deepcopy(disk_entry)
            _write_semantic_contract_disk_cache(disk_entries | {cache_key: disk_entry})
            return deepcopy(disk_entry)
        entry["last_access_at"] = now
        entry["hit_count"] = int(entry.get("hit_count") or 0) + 1
        entry["cache_tier"] = str(entry.get("cache_tier") or "memory")
        return deepcopy(entry)


def _store_semantic_contract_cache_entry(cache_key: str, *, contract: dict[str, Any], runtime: dict[str, Any]) -> None:
    if not cache_key:
        return
    if not bool(runtime.get("material")):
        return
    if str(runtime.get("source") or "") != "llm":
        return
    now = time.time()
    with _SEMANTIC_CONTRACT_CACHE_LOCK:
        entry = {
            "contract": deepcopy(contract),
            "runtime": deepcopy(runtime),
            "stored_at": now,
            "last_access_at": now,
            "hit_count": 0,
            "cache_tier": "memory",
        }
        _SEMANTIC_CONTRACT_CACHE[cache_key] = entry
        _prune_semantic_contract_cache(now)
        disk_entries = _load_semantic_contract_disk_cache(now)
        disk_entry = deepcopy(entry)
        disk_entry["cache_tier"] = "disk"
        disk_entries[cache_key] = disk_entry
        _write_semantic_contract_disk_cache(disk_entries)


def _semantic_model_profile(
    *,
    retrieval_mode: str,
    timeout: float | None,
    retry_timeout: float | None,
    compiler_profile: str,
    schema_name: str,
    max_output_tokens: int | None,
) -> dict[str, Any]:
    return {
        "schema_version": "agvm.semantic_model_profile.v1",
        "compiler_role": "compiler",
        "compiler_model": compiler_model(),
        "retrieval_mode": str(retrieval_mode or "balanced").strip().lower(),
        "compiler_profile": str(compiler_profile or "compact_first").strip(),
        "schema_name": str(schema_name or "").strip() or None,
        "max_output_tokens": max_output_tokens,
        "compiler_timeout_seconds": timeout,
        "compiler_retry_timeout_seconds": retry_timeout,
        "cache_ttl_seconds": _SEMANTIC_CONTRACT_CACHE_TTL_SECONDS,
    }


_SEMANTIC_PROVIDER_DEGRADED_MARKERS = (
    "timeout",
    "timed out",
    "queue_timeout",
    "llm_queue_timeout",
    "provider_error",
    "rate_limit",
    "overloaded",
    "temporarily_unavailable",
    "api_error",
    "connection",
)


def _semantic_provider_degraded_error(value: Any) -> bool:
    text = _fold_text(str(value or ""))
    return bool(text and any(marker in text for marker in _SEMANTIC_PROVIDER_DEGRADED_MARKERS))


def _semantic_provider_degraded_reason(runtime: dict[str, Any]) -> str | None:
    for key in ("retry_error", "primary_error", "error"):
        value = runtime.get(key)
        if _semantic_provider_degraded_error(value):
            return f"{key}:{value}"
    return None


def _semantic_runtime_provider_state(runtime: dict[str, Any]) -> str:
    status = str(runtime.get("status") or "").strip().lower()
    source = str(runtime.get("source") or "").strip()
    material = bool(runtime.get("material"))
    ai_required = bool(runtime.get("ai_required"))
    cache_hit = bool(runtime.get("cache_hit"))
    if cache_hit and material:
        return "cached_ai_contract"
    if material and source == "llm":
        return "fresh_llm_contract"
    if _semantic_provider_degraded_reason(runtime):
        return "provider_degraded"
    if not bool(runtime.get("enabled")) and ai_required:
        return "llm_unavailable"
    if status == "deferred":
        return "deferred"
    if not ai_required:
        return "not_required"
    if status:
        return status
    return "unknown"


_LEGACY_TARGET_IDS = {
    "identity",
    "work",
    "work_detail",
    "relationships",
    "relation_detail",
    "place",
    "style",
    "values",
    "history",
    "documents",
    "company_founding",
    "temporal_inventory",
}

_GENERALIZED_TARGET_IDS = {
    "identity",
    "work_company",
    "project",
    "document",
    "relationship",
    "family",
    "private_identifier",
    "personal_contact",
    "exact_user_field",
    "temporal",
    "location",
    "style",
    "values",
    "uncertainty",
}

_CANONICAL_TARGET_IDS = _LEGACY_TARGET_IDS | _GENERALIZED_TARGET_IDS

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
    "uncertainty": "history",
}

_FAMILY_MARKERS = (
    "father",
    "padre",
    "papa",
    "mother",
    "madre",
    "mamma",
    "family",
    "famiglia",
    "sibling",
    "fratello",
    "sorella",
    "brother",
    "sister",
    "figlio",
    "figlia",
)

_ROMANTIC_RELATION_MARKERS = (
    "fidanz",
    "girlfriend",
    "boyfriend",
    "wife",
    "husband",
    "coniuge",
    "compagno",
    "compagna",
    "partner romant",
    "romantic partner",
)

_BUSINESS_PARTNER_MARKERS = (
    "business partner",
    "partner di lavoro",
    "partner professionale",
    "technology partner",
    "industrial partner",
    "scientific partner",
    "full service partner",
    "full service partner",
    "co founder",
    "cofounder",
    "cofondatore",
    "socio",
    "collega",
    "colleghi",
    "colleghe",
    "collaborator",
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


def _contains_business_partner_marker(text: str) -> bool:
    folded = _fold_text(text)
    for marker in _BUSINESS_PARTNER_MARKERS:
        marker_folded = _fold_text(marker)
        if not marker_folded:
            continue
        if " " in marker_folded:
            if marker_folded in folded:
                return True
            continue
        if re.search(rf"\b{re.escape(marker_folded)}\b", folded):
            return True
    return False


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


def _looks_like_org_or_project_text(value: Any) -> bool:
    text = str(value or "").strip()
    folded = _fold_text(text)
    if not text or not folded:
        return False
    if any(marker in folded for marker in _ORG_LIKE_ENTITY_MARKERS):
        return True
    tokens = re.findall(r"\b[A-Za-z][A-Za-z0-9&.'-]{2,}\b", text)
    return any(_looks_like_org_entity_token(token) for token in tokens)


def _query_mentions_personal_relationship(query_text: str) -> bool:
    folded = _fold_text(query_text)
    return any(marker in folded for marker in (*_FAMILY_MARKERS, *_ROMANTIC_RELATION_MARKERS))


def _entity_connection_path_query(query_text: str) -> bool:
    folded = _fold_text(query_text)
    if _query_mentions_personal_relationship(query_text):
        return False
    if not any(marker in folded for marker in _ENTITY_CONNECTION_PATH_MARKERS):
        return False
    named_tokens = _capitalized_entity_tokens(query_text)
    if len(named_tokens) < 2 and not any(marker in folded for marker in _ORG_LIKE_ENTITY_MARKERS):
        return False
    org_like_count = sum(1 for token in named_tokens if _looks_like_org_entity_token(token))
    explicit_org_term = any(marker in folded for marker in _ORG_LIKE_ENTITY_MARKERS)
    return bool(explicit_org_term or org_like_count >= 1)


def _generalized_slot_id(
    slot: Any,
    *,
    query_text: str,
    claim_shape: Any = "",
    required_fields: list[Any] | None = None,
    fallback_slot: str = "",
) -> str:
    raw = _fold_text(str(slot or ""))
    query = _fold_text(query_text)
    claim = _fold_text(str(claim_shape or ""))
    fields = _fold_text(" ".join(str(item or "") for item in list(required_fields or [])))
    fallback = _fold_text(fallback_slot)
    blob = f"{raw} {query} {claim} {fields} {fallback}"
    if _entity_connection_path_query(query_text) and (
        raw in {"relationship", "relationships", "relation_detail"}
        or any(marker in blob for marker in ("relationship", "relational", "relation", "relations", "mentor", "partner", "rapporto"))
    ):
        return "work_company"
    if raw in _GENERALIZED_TARGET_IDS:
        return raw
    if raw == "identity":
        return "identity"
    if raw in {"documents"}:
        return "document"
    if raw == "work_detail":
        return "project"
    if raw in {"company_founding", "work"}:
        return "work_company"
    if raw in {"history", "temporal_inventory"}:
        return "temporal"
    if raw == "place":
        return "location"
    if raw == "style":
        return "style"
    if raw == "values":
        return "values"
    if raw in {"relationships", "relation_detail"}:
        if any(marker in query for marker in _FAMILY_MARKERS):
            return "family"
        if _contains_business_partner_marker(query):
            return "work_company"
        if any(marker in query for marker in _ROMANTIC_RELATION_MARKERS):
            return "relationship"
        return "relationship"
    if any(marker in blob for marker in ("document", "documents", "source", "file", "chunk", "anchor", "fonte", "fonti")):
        return "document"
    if any(marker in blob for marker in _FAMILY_MARKERS):
        return "family"
    if any(marker in blob for marker in _ROMANTIC_RELATION_MARKERS):
        return "relationship"
    if any(marker in blob for marker in ("project", "progetto", "initiative", "iniziativa", "product")):
        return "project"
    if _contains_business_partner_marker(blob):
        return "work_company"
    if raw in {"relationships", "relation_detail"} or any(marker in blob for marker in ("relationship", "relational", "mentor", "partner")):
        return "relationship"
    if any(
        marker in blob
        for marker in (
            "company",
            "companies",
            "azienda",
            "aziende",
            "societa",
            "startup",
            "impresa",
            "founder",
            "founded",
            "fondat",
            "role",
            "work",
            "lavor",
        )
    ):
        return "work_company"
    if any(marker in blob for marker in ("history", "timeline", "temporal", "date", "year", "anno", "quando", "19", "20")):
        return "temporal"
    if any(marker in blob for marker in ("place", "birth", "residence", "location", "nato", "nata", "vive")):
        return "location"
    if any(marker in blob for marker in ("style", "communication", "voice", "stile", "tono", "comunica")):
        return "style"
    if any(marker in blob for marker in ("value", "values", "princip", "valori")):
        return "values"
    return "identity"


def _relation_subtype(*, query_text: str, claim_shape: Any = "", required_fields: list[Any] | None = None, slot: Any = "") -> str:
    blob = _fold_text(f"{query_text or ''} {claim_shape or ''} {' '.join(str(item or '') for item in list(required_fields or []))} {slot or ''}")
    raw_slot = _fold_text(str(slot or ""))
    if any(marker in blob for marker in ("father", "padre", "papa")):
        return "father"
    if any(marker in blob for marker in ("mother", "madre", "mamma")):
        return "mother"
    if any(marker in blob for marker in ("sibling", "fratello", "sorella", "brother", "sister")):
        return "sibling"
    if raw_slot.startswith("family") or (any(marker in _fold_text(query_text) for marker in _FAMILY_MARKERS) and any(marker in blob for marker in _FAMILY_MARKERS)):
        return "generic"
    if any(marker in blob for marker in _ROMANTIC_RELATION_MARKERS):
        return "romantic_partner"
    if _contains_business_partner_marker(blob):
        return "business_partner"
    if "mentor" in blob:
        return "mentor"
    if "partner" in blob:
        return "partner_unspecified"
    return "generic"


def _query_explicitly_requests_relation_subtype(query_text: str, subtype: str) -> bool:
    folded = _fold_text(query_text)
    normalized = str(subtype or "").strip().lower()
    if not folded or not normalized:
        return False
    if normalized == "mentor":
        return "mentor" in folded
    if normalized in {"romantic_partner", "partner_unspecified"}:
        return any(marker in folded for marker in _ROMANTIC_RELATION_MARKERS) or "partner" in folded
    if normalized == "business_partner":
        return _contains_business_partner_marker(folded)
    if normalized == "father":
        return any(marker in folded for marker in ("father", "padre", "papa"))
    if normalized == "mother":
        return any(marker in folded for marker in ("mother", "madre", "mamma"))
    if normalized == "sibling":
        return any(marker in folded for marker in ("sibling", "fratello", "sorella", "brother", "sister"))
    return normalized in folded


def _query_requests_generic_public_family_event(query_text: str) -> bool:
    folded = _fold_text(query_text)
    if not folded:
        return False
    return bool(
        any(marker in folded for marker in ("famiglia", "familiare", "relazione familiare", "family", "family relation"))
        or any(marker in folded for marker in ("evento pubblico", "eventi pubblici", "public event", "public events", "fatti pubblici", "public facts"))
        or any(marker in folded for marker in ("dati non disponibili", "informazioni non disponibili", "missing data", "unavailable data"))
    )


def _soften_unrequested_relation_subtypes_for_query(contract: dict[str, Any]) -> None:
    query_text = str(contract.get("user_query") or "")
    if not _query_requests_generic_public_family_event(query_text):
        return
    for slot_contract in list(contract.get("semantic_slot_contracts") or []):
        if not isinstance(slot_contract, dict) or not bool(slot_contract.get("required")):
            continue
        slot_id = str(slot_contract.get("slot_id") or "").strip()
        subtype = str(slot_contract.get("relation_subtype") or "").strip()
        if slot_id not in {"relationship", "family"} or not subtype or subtype == "generic":
            continue
        if _query_explicitly_requests_relation_subtype(query_text, subtype):
            continue
        if slot_id == "family":
            slot_contract["relation_subtype"] = "generic"
            slot_contract["slot_key"] = "family"
            slot_contract["positive_evidence"] = _semantic_positive_evidence("family", "generic")
            slot_contract["success_question"] = "Does promoted evidence directly satisfy family for this user query without forbidden evidence?"
            slot_contract["required_softened_reason"] = "unrequested_family_relation_subtype_normalized_to_generic_family"
            continue
        slot_contract["required"] = False
        slot_contract["required_softened_reason"] = "unrequested_relation_subtype_in_public_family_event_query"


def _semantic_slot_key(slot_id: str, relation_subtype: str = "") -> str:
    subtype = str(relation_subtype or "").strip()
    if slot_id in {"relationship", "family", *EXACT_FIELD_SLOT_IDS} and subtype and subtype != "generic":
        return f"{slot_id}:{subtype}"
    return slot_id


def _looks_like_node_id(value: Any) -> bool:
    return bool(re.search(r"\b(?:vec_node_[a-z0-9_]+|node_[a-z0-9_]+|[0-9a-f]{10,})\b", str(value or ""), flags=re.IGNORECASE))


def _candidate_is_source_noise(value: Any, *, candidate_family: str = "") -> bool:
    text = str(value or "").strip()
    folded = _fold_text(text)
    if not folded:
        return True
    if _looks_like_node_id(text):
        return True
    source_markers = (
        "official website source",
        "source uri",
        "source url",
        "page title",
        "headings",
        "visualizza profilo",
        "iscriviti ora",
        "consigliato da",
        "undefined",
        "quoted in the release",
        "document title",
        "user instruction",
    )
    if any(marker in folded for marker in source_markers):
        return True
    short_scraped_headings = {
        "heritage",
        "the foundation",
        "building the foundation",
        "art culture",
        "ulisse s journey",
        "entrepreneurial philanthropy projects",
        "the sky is not the limit",
        "they say",
    }
    if len(folded) <= 80 and folded.strip(" -") in short_scraped_headings:
        return True
    if "power companies worldwide" in folded:
        return True
    if candidate_family in {"partner_candidates", "mentor_candidates", "sibling_candidates"}:
        organization_markers = (
            "company",
            "companies",
            "corporation",
            "foundation",
            "university",
            "school",
            "exhibition",
            "gmbh",
            "srl",
            "spa",
        )
        if any(marker in folded for marker in organization_markers) or _looks_like_org_or_project_text(text):
            return True
    if candidate_family in {"project_candidates", "employer_candidates"}:
        letters = [char for char in text if char.isalpha()]
        if len(letters) >= 6 and sum(1 for char in letters if char.isupper()) / max(1, len(letters)) > 0.82:
            return True
    return False


def _clean_identity_candidate_list(values: Any, *, candidate_family: str, limit: int = 8) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in list(values or []):
        text = " ".join(str(value or "").split()).strip()
        if not text or _candidate_is_source_noise(text, candidate_family=candidate_family):
            continue
        folded = _fold_text(text)
        if folded in seen:
            continue
        cleaned.append(text)
        seen.add(folded)
        if len(cleaned) >= limit:
            break
    return cleaned


def _sanitize_identity_hints(identity_hints: dict[str, Any] | None) -> dict[str, Any]:
    hints = dict(identity_hints or {})
    output: dict[str, Any] = {
        "core_name": str(hints.get("core_name") or "").strip() or None,
        "aliases": _clean_identity_candidate_list(hints.get("aliases"), candidate_family="aliases", limit=6),
        "self_name_candidates": _clean_identity_candidate_list(hints.get("self_name_candidates"), candidate_family="self_name_candidates", limit=6),
        "partner_candidates": _clean_identity_candidate_list(hints.get("partner_candidates"), candidate_family="partner_candidates", limit=4),
        "mentor_candidates": _clean_identity_candidate_list(hints.get("mentor_candidates"), candidate_family="mentor_candidates", limit=4),
        "sibling_candidates": _clean_identity_candidate_list(hints.get("sibling_candidates"), candidate_family="sibling_candidates", limit=4),
        "role_candidates": _clean_identity_candidate_list(hints.get("role_candidates"), candidate_family="role_candidates", limit=6),
        "project_candidates": _clean_identity_candidate_list(hints.get("project_candidates"), candidate_family="project_candidates", limit=8),
        "employer_candidates": _clean_identity_candidate_list(hints.get("employer_candidates"), candidate_family="employer_candidates", limit=4),
    }
    core_nodes: list[dict[str, Any]] = []
    for node in list(hints.get("core_nodes") or []):
        if not isinstance(node, dict):
            continue
        summary = " ".join(str(node.get("summary") or "").split()).strip()
        if not summary or _candidate_is_source_noise(summary):
            continue
        core_nodes.append(
            {
                "summary": summary,
                "memory_type": str(node.get("memory_type") or "").strip(),
                "guide_area": str(node.get("guide_area") or "").strip(),
                "confidence": float(node.get("confidence") or 0.0),
            }
        )
        if len(core_nodes) >= 8:
            break
    output["core_nodes"] = core_nodes
    return output


def sanitize_identity_hints(identity_hints: dict[str, Any] | None) -> dict[str, Any]:
    return _sanitize_identity_hints(identity_hints)


def _canonical_target_id(
    raw_target_id: Any,
    *,
    claim_shape: Any,
    required_fields: list[Any],
    query_text: str,
    intent_primary: str,
    fallback_slot: str,
) -> str:
    raw = _fold_text(str(raw_target_id or ""))
    claim = _fold_text(str(claim_shape or ""))
    fields = _fold_text(" ".join(str(item or "") for item in required_fields))
    query = _fold_text(query_text)
    if raw in _CANONICAL_TARGET_IDS:
        if _entity_connection_path_query(query_text) and raw in {"relationship", "relationships", "relation_detail"}:
            return "work_company"
        if raw == "identity" and any(token in f"{claim} {fields} {query}" for token in ("role", "project", "lavor", "azienda", "company", "founder", "founded", "work")):
            return "work"
        if raw == "identity" and _entity_connection_path_query(query_text):
            return "work"
        return raw
    if _looks_like_node_id(str(raw_target_id or "")):
        raw = ""
    blob = f"{raw} {claim} {fields} {query} {intent_primary} {fallback_slot}"
    if any(token in blob for token in ("document", "source trace", "fonte", "fonti", "file", "chunk")):
        return "documents"
    if any(token in blob for token in ("company", "companies", "azienda", "aziende", "societa", "founder", "founded", "fondat")) or _looks_like_org_or_project_text(raw_target_id):
        return "company_founding" if any(token in blob for token in ("founder", "founded", "fondat")) else "work"
    if any(token in blob for token in ("work", "role", "project", "lavor", "progetto", "activity", "attivita")):
        return "work"
    if _entity_connection_path_query(query_text) and any(
        token in blob for token in ("relationship", "relational", "relation", "relations", "mentor", "partner", "rapporto")
    ):
        return "work_company"
    if any(token in blob for token in ("father", "padre", "papa", "mother", "madre", "partner", "fidanz", "relationship", "relational", "mentor")):
        return "relationships"
    if any(token in blob for token in ("style", "communication", "voice", "stile", "comunica", "tono")):
        return "style"
    if any(token in blob for token in ("value", "values", "princip", "valori", "sustainable", "impact")):
        return "values"
    if any(token in blob for token in ("history", "timeline", "temporal", "date", "year", "anno", "quando", "19", "20")):
        return "history"
    if any(token in blob for token in ("place", "birth", "residence", "nato", "nata", "vive")):
        return "place"
    return "identity" if intent_primary in {"identity", "broad_dossier", "unknown"} else intent_primary if intent_primary in _CANONICAL_TARGET_IDS else "identity"


def _claim_shape_for_target(target_id: str, fallback_claim_shape: str) -> str:
    fallback = str(fallback_claim_shape or "").strip()
    if target_id in {"work", "company_founding"} and _fold_text(fallback) in {"identity", "person identity"}:
        return _claim_shape_for_slot(target_id, "work")
    if target_id in _CANONICAL_TARGET_IDS and (not fallback or _looks_like_node_id(fallback)):
        return _claim_shape_for_slot(target_id, target_id)
    return fallback or _claim_shape_for_slot(target_id, target_id)


def _contract_intent_from_legacy(query_text: str, legacy_contract: dict[str, Any]) -> str:
    query_kind = str(legacy_contract.get("query_kind") or "").strip()
    lowered = _fold_text(query_text)
    if _broad_self_query(query_text):
        return "broad_dossier"
    if query_kind == "document_lookup" or any(token in lowered for token in ("documento", "documenti", "fonte", "fonti", "source")):
        return "document_lookup"
    if query_kind == "broad_profile":
        return "broad_dossier"
    if query_kind in {"company_founding_relation", "company_founding_timeline", "work_narrative"}:
        return "work"
    if _organization_query(query_text):
        return "work"
    if query_kind == "temporal" or re.search(r"\b(?:19|20)\d{2}\b", lowered):
        return "temporal"
    if query_kind in {"narrative_relation", "exact_relation_fact"}:
        return "relationship"
    if query_kind == "multi_fact":
        return "identity"
    return "identity" if query_kind in {"exact_fact", ""} else "unknown"


def _broad_self_query(query_text: str) -> bool:
    lowered = _fold_text(query_text)
    return any(
        marker in lowered
        for marker in (
            "raccontami di te",
            "parlami di te",
            "dimmi chi sei",
            "raccontami tutto di te",
            "raccontami in sintesi",
            "riassumimi tutto",
            "dimmi tutto",
            "tutto quello che sai",
            "dossier completo",
            "dossier operativo",
            "quadro completo",
            "quadro globale",
            "profilo completo",
            "profilo pubblico",
            "profilo esteso",
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
            "tell me about yourself",
            "tell me about you",
            "tell me everything",
            "who are you",
            "full profile",
            "complete profile",
            "public profile",
            "broad context",
            "wide context",
            "agent context",
            "context for an agent",
            "operational packet",
            "operational package",
        )
    )


def _requires_first_person(query_text: str) -> bool:
    lowered = _fold_text(query_text)
    return any(
        marker in lowered
        for marker in (
            "in prima persona",
            "rispondi come",
            "chi sei",
            "tuo lavoro",
            "tuoi progetti",
            "tuo padre",
            "tua madre",
            "parlami di te",
            "raccontami di te",
            "che lavoro fai",
            "quali sono le tue aziende",
            "quali aziende hai",
            "che aziende hai",
            "come ti chiami",
            "sei fidanzato",
        )
    )


def _document_mode(query_text: str, intent_primary: str) -> str:
    lowered = _fold_text(query_text)
    if intent_primary != "document_lookup":
        return "none"
    wants_support_trace = any(
        marker in lowered
        for marker in (
            "quali fonti",
            "fonti supportano",
            "fonti dimostrano",
            "fonti provano",
            "quali documenti supportano",
            "quali documenti dimostrano",
            "quali documenti provano",
            "documenti che supportano",
            "documenti che dimostrano",
            "documenti che provano",
            "lo dimostrano",
            "lo provano",
            "lo confermano",
            "source trace",
            "sources support",
            "documents support",
            "documents prove",
            "documents confirm",
        )
    )
    if wants_support_trace:
        return "source_trace_for_answer"
    if any(marker in lowered for marker in ("documenti relativi", "documenti collegati", "documenti su", "docs about", "related documents")):
        return "related_document_lookup"
    if any(marker in lowered for marker in ("sintesi", "riassumi il documento", "secondo il documento")):
        return "document_synthesis"
    return "exact_document_lookup"


def _organization_query(query_text: str) -> bool:
    lowered = _fold_text(query_text)
    return _entity_connection_path_query(query_text) or any(token in lowered for token in ("azienda", "aziende", "societa", "society", "company", "companies", "startup", "impresa", "business"))


def _work_entity_inventory_query(query_text: str) -> bool:
    lowered = _fold_text(query_text)
    plural_company_surface = any(
        marker in lowered
        for marker in (
            "aziende",
            "societa",
            "imprese",
            "organizzazioni",
            "companies",
            "organizations",
            "businesses",
            "ventures",
        )
    )
    if not plural_company_surface and not _organization_query(query_text):
        return False
    inventory_context = any(
        marker in lowered
        for marker in (
            "dossier",
            "profilo",
            "completo",
            "quadro",
            "lista",
            "elenco",
            "quali",
            "che",
            "mappa",
            "colleg",
            "raccontami",
            "parlami",
            "dimmi",
            "complete",
            "profile",
            "which",
            "what",
            "list",
            "map",
            "connected",
            "linked",
        )
    )
    founding_or_affiliation = any(
        marker in lowered
        for marker in (
            "fondat",
            "fondatore",
            "fondatrice",
            "founder",
            "founded",
            "cofounder",
            "co founder",
            "associated",
            "collegate",
            "collegato",
            "linked",
            "connected",
            "guidat",
            "led",
        )
    )
    return bool((plural_company_surface and inventory_context) or founding_or_affiliation)


def _explicit_work_context_query(query_text: str) -> bool:
    lowered = _fold_text(query_text)
    return _organization_query(query_text) or any(
        token in lowered
        for token in (
            "lavoro",
            "lavori",
            "profession",
            "carriera",
            "career",
            "ruolo",
            "role",
            "progetto",
            "progetti",
            "project",
            "projects",
            "fondat",
            "guidat",
            "imprend",
            "entrepreneur",
            "venture",
            "ventures",
        )
    )


def _target_allowed_by_query_context(
    target_id: str,
    *,
    query_text: str,
    legacy_contract: dict[str, Any],
) -> bool:
    generalized = _generalized_slot_id(target_id, query_text=query_text, fallback_slot=target_id)
    if generalized not in {"work_company", "project"} and str(target_id or "").strip() not in {"work", "work_detail", "company_founding"}:
        return True
    if _broad_self_query(query_text) or _explicit_work_context_query(query_text):
        return True
    legacy_required = {
        str(item or "").strip()
        for item in list(legacy_contract.get("required_slots") or []) + list(legacy_contract.get("requested_aspects") or [])
        if str(item or "").strip()
    }
    if legacy_required & {"work", "work_detail", "company_founding"}:
        return True
    disallowed = {
        str(item or "").strip()
        for item in list(legacy_contract.get("disallowed_topics") or [])
        if str(item or "").strip()
    }
    if disallowed & {"work_projects", "unrelated_company_context", "education_awards"}:
        return False
    return True


def _canonical_context_section(value: Any, text: Any = "") -> str:
    folded = _fold_text(f"{value or ''} {text or ''}")
    raw_value = _fold_text(str(value or ""))
    if raw_value not in _CANONICAL_TARGET_IDS and _looks_like_org_or_project_text(f"{value or ''} {text or ''}"):
        return "work"
    generalized = _generalized_slot_id(value, query_text=str(text or ""), claim_shape=text, fallback_slot=str(value or ""))
    if generalized in _SEMANTIC_SLOT_SECTIONS:
        return _SEMANTIC_SLOT_SECTIONS[generalized]
    if "company_founding" in folded or "company founding" in folded:
        return "work"
    if any(token in folded for token in ("father", "padre", "papa", "mother", "madre", "partner", "mentor", "relationship", "relational", "monumento")):
        return "relationships"
    if any(token in folded for token in ("document", "source", "file", "chunk", "anchor", "fonte", "fonti")):
        return "documents"
    if any(token in folded for token in ("work", "project", "company", "companies", "azienda", "aziende", "societa", "foundry", "studio", "lavoro", "progetto", "progetti", "founder", "founded", "fondat")) or _looks_like_org_or_project_text(f"{value or ''} {text or ''}"):
        return "work"
    if any(token in folded for token in ("style", "communication", "tone", "voice", "stile", "comunica", "parla")):
        return "style"
    if any(token in folded for token in ("value", "values", "principle", "principi", "valori", "precision", "sustainable", "impact")):
        return "values"
    if any(token in folded for token in ("temporal_inventory", "temporal evidence")):
        return "temporal_inventory"
    if any(token in folded for token in ("timeline", "history", "temporal", "date", "year", "anno", "quando", "19", "20")):
        return "history"
    if any(token in folded for token in ("identity", "name", "born", "birth", "residence", "nato", "nata", "vive", "sono", "chi sei")):
        return "identity"
    return "history"


def _claim_shape_for_slot(slot: str, intent_primary: str) -> str:
    mapping = {
        "identity": "person identity, name, self description, or stable profile claim",
        "work": "person work, role, company, project, or operating activity",
        "work_detail": "specific work/project details, responsibilities, products, or concrete initiatives",
        "work_company": "person work, companies, operating roles, organizations, ventures, or founded/operated companies",
        "project": "specific project, product, initiative, responsibility, or concrete workstream",
        "document": "document anchor, full document, document chunk, source trace, or source-backed file evidence",
        "relationships": "person relationship, family relation, partner, mentor, or named relation",
        "relation_detail": "narrative details about a requested relationship",
        "relationship": "non-family personal relationship evidence with the requested relation subtype",
        "family": "family relation evidence with the requested family member subtype",
        "place": "birthplace, residence, or location evidence",
        "location": "birthplace, residence, or location evidence",
        "style": "communication style and voice evidence",
        "values": "values, principles, or operating philosophy evidence",
        "history": "dated, temporal, historical, or biographical event evidence",
        "temporal": "dated, temporal, historical, or biographical event evidence",
        "documents": "document anchor, document chunk, source trace, or source-backed file evidence",
        "company_founding": "company founded by the remembered person with role and timeframe",
        "private_identifier": "exact private identifier requested by the user, or explicit absence after search",
        "personal_contact": "exact named personal/professional contact role requested by the user, or explicit absence after search",
        "exact_user_field": "exact user-specific field requested by the user, or explicit absence after search",
        "uncertainty": "explicit no-match, missing evidence, contradiction, or uncertainty state",
    }
    if slot in mapping:
        return mapping[slot]
    return f"{intent_primary} evidence for slot {slot}"


def _required_fields_for_slot(slot: str) -> list[str]:
    mapping = {
        "identity": ["person", "identity_claim"],
        "work": ["person", "role_or_project"],
        "work_detail": ["person", "project_or_company", "detail"],
        "work_company": ["person", "company_or_organization", "role_or_relation", "timeframe_if_available"],
        "project": ["person", "project_or_workstream", "detail"],
        "relationships": ["person", "relation_type", "related_person"],
        "relation_detail": ["person", "related_person", "detail"],
        "relationship": ["person", "relation_type", "related_person", "relationship_subtype"],
        "family": ["person", "family_relation_type", "family_member"],
        "place": ["person", "place", "place_type"],
        "location": ["person", "place", "place_type"],
        "style": ["person", "style_trait"],
        "values": ["person", "value_or_principle"],
        "history": ["person_or_project", "event", "timeframe"],
        "temporal": ["person_or_project", "event", "timeframe"],
        "documents": ["document_anchor_or_chunk", "topic_or_title"],
        "document": ["document_anchor_or_chunk", "topic_or_title"],
        "company_founding": ["person", "company", "role", "timeframe"],
        "private_identifier": ["person", "requested_private_identifier", "exact_value_or_explicit_absence"],
        "personal_contact": ["person", "requested_contact_role", "related_person_or_explicit_absence"],
        "exact_user_field": ["person", "requested_exact_field", "exact_value_or_explicit_absence"],
        "uncertainty": ["missing_slot", "reason", "searched_scope"],
    }
    return list(mapping.get(slot, ["subject", "claim"]))


def _negative_conditions(slot: str, legacy_contract: dict[str, Any]) -> list[str]:
    conditions = [
        "do_not_use_system_metadata",
        "do_not_use_synthetic_test_material",
        "do_not_use_source_metadata_as_personal_fact",
    ]
    for topic in list(legacy_contract.get("disallowed_topics") or []):
        conditions.append(f"do_not_use_{str(topic).strip()}")
    semantic_slot = _generalized_slot_id(slot, query_text="", fallback_slot=slot)
    if slot in {"work", "work_detail", "company_founding"} or semantic_slot in {"work_company", "project"}:
        conditions.append("do_not_answer_from_unrelated_family_context")
        conditions.append("do_not_use_business_heading_without_person_role")
    if slot in {"relationships", "relation_detail"} or semantic_slot in {"relationship", "family"}:
        conditions.append("do_not_answer_from_unrelated_work_context")
        conditions.append("do_not_use_business_partner_as_romantic_partner")
    if semantic_slot in EXACT_FIELD_SLOT_IDS:
        conditions.append("do_not_use_adjacent_biography_without_requested_field")
        conditions.append("do_not_use_related_profile_context_as_exact_field")
    if semantic_slot in {"identity", "location", "style", "values"}:
        conditions.append("do_not_use_source_heading_as_personal_fact")
    return _unique(conditions, limit=12)


def _semantic_positive_evidence(slot_id: str, relation_subtype: str = "") -> list[str]:
    mapping = {
        "identity": ["self-name evidence", "stable identity claim", "remembered person surface"],
        "work_company": ["company or organization name", "role or operating relation", "timeframe when available"],
        "project": ["project/workstream name", "concrete activity", "responsibility or product detail"],
        "document": ["document title or anchor", "source-backed chunk", "topic/entity overlap"],
        "relationship": ["requested non-family relationship subtype", "related person", "relationship evidence"],
        "family": ["requested family relation subtype", "family member", "family evidence"],
        "private_identifier": ["requested private identifier label", "exact value for that identifier", "explicit absence if not present"],
        "personal_contact": ["requested contact role", "related person name", "explicit absence if not present"],
        "exact_user_field": ["requested field label", "direct value for that field", "explicit absence if not present"],
        "temporal": ["date, year, sequence, or event timeframe"],
        "location": ["birthplace, residence, or location type"],
        "style": ["communication tone or behavioral style trait"],
        "values": ["value, principle, or operating philosophy"],
        "uncertainty": ["explicit searched scope", "missing slot", "no-match reason"],
    }
    values = list(mapping.get(slot_id, ["direct evidence for the requested slot"]))
    if relation_subtype and slot_id in {"relationship", "family"}:
        values.insert(0, f"subtype:{relation_subtype}")
    return values


def _semantic_forbidden_evidence(slot_id: str, relation_subtype: str, legacy_contract: dict[str, Any]) -> list[str]:
    forbidden = [
        "system_metadata",
        "synthetic_test_material",
        "source_heading_without_person_fact",
        "raw_node_id_or_route_debug",
    ]
    if slot_id in {"work_company", "project"}:
        forbidden.extend(["unrelated_family_context", "family_monument_as_company_answer"])
    if slot_id == "relationship":
        forbidden.extend(["unrelated_work_context", "business_partner_as_romantic_partner"])
        if relation_subtype == "romantic_partner":
            forbidden.extend(["father_or_family_as_romantic_partner", "company_partner_as_romantic_partner"])
    if slot_id == "family":
        forbidden.extend(["business_partner_as_family", "unrelated_company_context"])
    if slot_id in {"identity", "location", "style", "values"}:
        forbidden.append("source_metadata_as_personal_fact")
    if slot_id in EXACT_FIELD_SLOT_IDS:
        forbidden.extend(["adjacent_profile_context", "unrelated_biography", "field_label_absent"])
    for topic in list(legacy_contract.get("disallowed_topics") or []):
        topic_name = str(topic or "").strip()
        if topic_name:
            forbidden.append(topic_name)
    return _unique(forbidden, limit=16)


def _semantic_slot_contract(
    *,
    slot: Any,
    query_text: str,
    claim_shape: Any,
    required_fields: list[Any],
    required: bool,
    legacy_contract: dict[str, Any],
    fallback_slot: str = "",
) -> dict[str, Any]:
    slot_id = _generalized_slot_id(
        slot,
        query_text=query_text,
        claim_shape=claim_shape,
        required_fields=required_fields,
        fallback_slot=fallback_slot,
    )
    relation_subtype = _relation_subtype(
        query_text=query_text,
        claim_shape=claim_shape,
        required_fields=required_fields,
        slot=slot,
    ) if slot_id in {"relationship", "family"} else ""
    slot_key = _semantic_slot_key(slot_id, relation_subtype)
    claim = str(claim_shape or "").strip() or _claim_shape_for_slot(slot_id, slot_id)
    fields = _unique(list(required_fields or []) or _required_fields_for_slot(slot_id), limit=8)
    return {
        "schema_version": "agvm.semantic_slot_contract.v1",
        "slot_id": slot_id,
        "slot_key": slot_key,
        "section": _SEMANTIC_SLOT_SECTIONS.get(slot_id, "history"),
        "required": bool(required),
        "legacy_slot": str(fallback_slot or slot or "").strip(),
        "relation_subtype": relation_subtype,
        "claim_shape": claim,
        "required_fields": fields,
        "positive_evidence": _semantic_positive_evidence(slot_id, relation_subtype),
        "negative_conditions": _negative_conditions(slot_id, legacy_contract),
        "forbidden_evidence": _semantic_forbidden_evidence(slot_id, relation_subtype, legacy_contract),
        "success_question": f"Does promoted evidence directly satisfy {slot_key} for this user query without forbidden evidence?",
    }


def _semantic_slot_contracts_from_targets(
    *,
    query_text: str,
    expected_evidence: list[dict[str, Any]],
    required_slots: list[Any],
    optional_slots: list[Any],
    legacy_contract: dict[str, Any],
) -> list[dict[str, Any]]:
    contracts: list[dict[str, Any]] = []
    seen: set[str] = set()
    exact_field_request = extract_exact_user_field_request(query_text)

    def add_contract(contract: dict[str, Any]) -> None:
        key = str(contract.get("slot_key") or "").strip()
        if not key:
            return
        if key in seen:
            for existing in contracts:
                if existing.get("slot_key") == key:
                    existing["required"] = bool(existing.get("required")) or bool(contract.get("required"))
                    legacy_values = _unique([existing.get("legacy_slot"), contract.get("legacy_slot")], limit=4)
                    existing["legacy_slots"] = legacy_values
                    break
            return
        contract["legacy_slots"] = _unique([contract.get("legacy_slot")], limit=4)
        contracts.append(contract)
        seen.add(key)

    expected_by_legacy: dict[str, dict[str, Any]] = {}
    for target in list(expected_evidence or []):
        if not isinstance(target, dict):
            continue
        legacy_slot = str(target.get("target_id") or "").strip()
        expected_by_legacy.setdefault(legacy_slot, target)

    for slot in list(required_slots or []):
        slot_name = str(slot or "").strip()
        if not slot_name:
            continue
        if exact_field_request and slot_name == exact_field_request.get("slot_id"):
            add_contract(
                exact_field_semantic_slot_contract(
                    exact_field_request,
                    required=True,
                    legacy_slot=slot_name,
                    disallowed_topics=list(legacy_contract.get("disallowed_topics") or []),
                )
            )
            continue
        target = dict(expected_by_legacy.get(slot_name) or {})
        add_contract(
            _semantic_slot_contract(
                slot=slot_name,
                query_text=query_text,
                claim_shape=target.get("claim_shape") or _claim_shape_for_slot(slot_name, slot_name),
                required_fields=list(target.get("required_fields") or _required_fields_for_slot(slot_name)),
                required=True,
                legacy_contract=legacy_contract,
                fallback_slot=slot_name,
            )
        )

    for target in list(expected_evidence or []):
        if not isinstance(target, dict):
            continue
        target_id = str(target.get("target_id") or "").strip()
        if not target_id:
            continue
        if exact_field_request and target_id == exact_field_request.get("slot_id"):
            add_contract(
                exact_field_semantic_slot_contract(
                    exact_field_request,
                    required=target_id in {str(slot or "").strip() for slot in list(required_slots or [])},
                    legacy_slot=target_id,
                    disallowed_topics=list(legacy_contract.get("disallowed_topics") or []),
                )
            )
            continue
        add_contract(
            _semantic_slot_contract(
                slot=target_id,
                query_text=query_text,
                claim_shape=target.get("claim_shape"),
                required_fields=list(target.get("required_fields") or []),
                required=target_id in {str(slot or "").strip() for slot in list(required_slots or [])},
                legacy_contract=legacy_contract,
                fallback_slot=target_id,
            )
        )

    for slot in list(optional_slots or []):
        slot_name = str(slot or "").strip()
        if not slot_name:
            continue
        add_contract(
            _semantic_slot_contract(
                slot=slot_name,
                query_text=query_text,
                claim_shape=_claim_shape_for_slot(slot_name, slot_name),
                required=False,
                required_fields=_required_fields_for_slot(slot_name),
                legacy_contract=legacy_contract,
                fallback_slot=slot_name,
            )
        )

    return contracts[:16]


def _build_expected_evidence(
    *,
    intent_primary: str,
    legacy_contract: dict[str, Any],
    query_text: str = "",
) -> list[dict[str, Any]]:
    slots = _unique(list(legacy_contract.get("required_slots") or []), limit=10) or ["identity"]
    work_inventory = _work_entity_inventory_query(query_text)
    targets: list[dict[str, Any]] = []
    for slot in slots:
        slot_name = str(slot)
        claim_shape = _claim_shape_for_slot(slot_name, intent_primary)
        required_fields = _required_fields_for_slot(slot_name)
        minimum_support = {
            "node_count": 1,
            "document_chunk_count": 1 if slot_name == "documents" else 0,
            "source_trace_count": 1 if intent_primary == "document_lookup" and slot_name == "documents" else 0,
        }
        success_question = f"Does promoted evidence directly satisfy the {slot_name} requirement for this user query?"
        if work_inventory and slot_name in {"work", "work_detail", "company_founding"}:
            claim_shape = "person work/company inventory: multiple company or organization names with the subject's role, founding, operating or affiliation relation"
            required_fields = ["person", "company_or_organization", "role_or_relation", "multiple_work_entities"]
            minimum_support = {"node_count": 2, "document_chunk_count": 0, "source_trace_count": 0}
            success_question = "Does promoted evidence expose a usable work/company inventory, not only one isolated role claim?"
        targets.append(
            {
                "target_id": slot_name,
                "claim_shape": claim_shape,
                "required_fields": required_fields,
                "acceptable_sources": ["verified_public", "user_asserted", "uploaded_document"],
                "minimum_support": minimum_support,
                "negative_conditions": _negative_conditions(slot_name, legacy_contract),
                "success_question": success_question,
            }
        )
    return targets


def _document_need_expected_evidence(target_document_need_contract: dict[str, Any]) -> dict[str, Any]:
    target_contract = dict(target_document_need_contract or {})
    need = dict(target_contract.get("target_document_need") or {})
    preserved_query = str(
        need.get("ranking_target_text")
        or need.get("preserved_query_text")
        or need.get("original_query")
        or ""
    ).strip()
    need_type = str(target_contract.get("need_type") or need.get("need_type") or "document_evidence").strip()
    semantic_document_mode = str(target_contract.get("semantic_document_mode") or need.get("semantic_document_mode") or "source_trace_for_answer")
    required_fields = ["document_anchor_or_chunk", "claim_or_query_fit", "raw_retrieval_affordance"]
    if need_type == "exact_document_id_or_title":
        required_fields = ["document_anchor_or_chunk", "document_id_or_title", "raw_retrieval_affordance"]
    elif need_type in {"related_documents", "project_document_request", "mixed_context_documents"}:
        required_fields = ["document_anchor_or_chunk", "topic_or_title", "relationship_to_query", "raw_retrieval_affordance"]
    elif semantic_document_mode == "source_trace_for_answer":
        required_fields = ["document_anchor_or_chunk", "source_trace", "claim_or_query_fit", "raw_retrieval_affordance"]
    return {
        "target_id": "document",
        "claim_shape": preserved_query or "preserved document evidence target",
        "required_fields": required_fields,
        "acceptable_sources": ["uploaded_document", "source_document", "external_dataset_document", "verified_public"],
        "minimum_support": {
            "node_count": 0,
            "document_chunk_count": 1,
            "source_trace_count": 1 if semantic_document_mode == "source_trace_for_answer" else 0,
        },
        "negative_conditions": [
            "do_not_replace_preserved_claim_with_personal_context_slot",
            "do_not_require_identity_work_style_or_relationship_when_pure_document_evidence",
            "do_not_answer_from_source_metadata_without_document_ref",
            "do_not_scan_raw_bodies_without_bounded_document_lane",
        ],
        "success_question": (
            "Do selected document refs directly satisfy the preserved target document need "
            "without relying on unrelated normal-context sections?"
        ),
        "target_document_need": dict(need),
        "target_document_need_type": need_type,
        "target_document_need_classification": str(target_contract.get("classification") or ""),
    }


def _sync_context_contract_slot_keys(contract: dict[str, Any]) -> None:
    context_contract = dict(contract.get("context_contract") or {})
    slot_contracts = [item for item in list(contract.get("semantic_slot_contracts") or []) if isinstance(item, dict)]
    context_contract["semantic_required_slot_keys"] = [
        str(item.get("slot_key") or "")
        for item in slot_contracts
        if bool(item.get("required")) and str(item.get("slot_key") or "")
    ]
    context_contract["semantic_optional_slot_keys"] = [
        str(item.get("slot_key") or "")
        for item in slot_contracts
        if not bool(item.get("required")) and str(item.get("slot_key") or "")
    ]
    contract["context_contract"] = context_contract


def _rebuild_document_need_landing_plan(contract: dict[str, Any], *, pure_document_evidence: bool, document_mode: str) -> None:
    landing_plan = dict(contract.get("landing_plan") or {})
    retrieval_mode = str(contract.get("retrieval_mode") or "balanced")
    mode_landing_cap = _mode_landing_cap(retrieval_mode)
    evidence = [item for item in list(contract.get("expected_evidence") or []) if isinstance(item, dict)]
    existing_landings = [dict(item) for item in list(landing_plan.get("landing_hypotheses") or []) if isinstance(item, dict)]
    if pure_document_evidence:
        landings = [
            {
                "landing_id": "L1",
                "target_evidence_ids": ["document"],
                "textual_probe": str((evidence[0] if evidence else {}).get("claim_shape") or contract.get("user_query") or ""),
                "route_budget": {"max_hops": 5, "max_nodes": 24, "max_document_chunks": 12},
                "planner_source": "target_document_need_contract",
            }
        ]
    else:
        landings = existing_landings[:mode_landing_cap]
        has_document_landing = any(
            "document" in {str(target or "").strip() for target in list(landing.get("target_evidence_ids") or [])}
            for landing in landings
        )
        if not has_document_landing and len(landings) < mode_landing_cap:
            landings.append(
                {
                    "landing_id": f"L{len(landings) + 1}",
                    "target_evidence_ids": ["document"],
                    "textual_probe": str(contract.get("user_query") or ""),
                    "route_budget": {"max_hops": 5, "max_nodes": 24, "max_document_chunks": 8},
                    "planner_source": "target_document_need_contract",
                }
            )
    existing_min_landings = int(landing_plan.get("min_landings") or 1)
    min_landings = (
        max(1, min(mode_landing_cap, existing_min_landings))
        if pure_document_evidence
        else max(1, min(existing_min_landings, len(landings) or 1))
    )
    max_landings = max(min_landings, min(mode_landing_cap, max(len(landings), int(landing_plan.get("max_landings") or min_landings))))
    landing_plan["min_landings"] = min_landings
    landing_plan["max_landings"] = max_landings
    landing_plan["preferred_strategy"] = "document_first" if document_mode != "none" else str(landing_plan.get("preferred_strategy") or "multi_area")
    landing_plan["landing_hypotheses"] = landings[:max_landings]
    landing_plan["paths"] = _build_path_itinerary(
        list(landing_plan.get("landing_hypotheses") or []),
        preferred_strategy=str(landing_plan.get("preferred_strategy") or "document_first"),
        document_mode=document_mode,
        source="target_document_need_contract",
    )
    contract["landing_plan"] = landing_plan


def _apply_target_document_need_contract(contract: dict[str, Any]) -> dict[str, Any]:
    query_text = str(contract.get("user_query") or "")
    legacy_contract = dict(contract.get("legacy_contract") or {})
    tool_name = str(contract.get("mcp_tool_name") or "").strip() or None
    target_document_need_contract = build_target_document_need_contract(
        query_text,
        legacy_contract=legacy_contract,
        tool_name=tool_name,
    )
    contract["target_document_need_contract"] = target_document_need_contract
    if target_document_need_contract.get("target_document_need"):
        contract["target_document_need"] = dict(target_document_need_contract.get("target_document_need") or {})
    else:
        contract.pop("target_document_need", None)

    document_contract = dict(contract.get("document_contract") or {})
    context_contract = dict(contract.get("context_contract") or {})
    document_contract["target_document_need_contract"] = dict(target_document_need_contract)
    context_contract["target_document_need_classification"] = target_document_need_contract.get("classification")
    context_contract["target_document_need_type"] = target_document_need_contract.get("need_type")
    context_contract["document_evidence_required"] = bool(target_document_need_contract.get("document_evidence"))
    context_contract["normal_context_required"] = bool(target_document_need_contract.get("normal_context_required"))
    context_contract["document_evidence_reason_codes"] = list(target_document_need_contract.get("reason_codes") or [])

    if not bool(target_document_need_contract.get("document_evidence")):
        contract["document_contract"] = document_contract
        contract["context_contract"] = context_contract
        _sync_context_contract_slot_keys(contract)
        return contract

    document_mode = str(target_document_need_contract.get("semantic_document_mode") or document_contract.get("mode") or "source_trace_for_answer")
    pure_document_evidence = bool(target_document_need_contract.get("pure_document_evidence"))
    document_need = dict(target_document_need_contract.get("target_document_need") or {})
    document_evidence = _document_need_expected_evidence(target_document_need_contract)

    document_contract.update(
        {
            "mode": document_mode,
            "query_title_or_topic": str(document_need.get("ranking_target_text") or query_text),
            "target_document_need": document_need,
            "target_document_need_classification": target_document_need_contract.get("classification"),
            "target_document_need_type": target_document_need_contract.get("need_type"),
            "preserved_ranking_target_text": document_need.get("ranking_target_text"),
            "pure_document_evidence": pure_document_evidence,
            "normal_context_required": bool(target_document_need_contract.get("normal_context_required")),
            "reason_codes": list(target_document_need_contract.get("reason_codes") or []),
            "require_title_overlap": document_mode == "exact_document_lookup",
            "require_entity_overlap": document_mode != "none",
        }
    )
    context_contract["dossier_goal"] = "document_review" if not bool(target_document_need_contract.get("normal_context_required")) else "context_with_document_refs"

    intent = dict(contract.get("intent") or {})
    if pure_document_evidence:
        intent["primary"] = "source_trace" if document_mode == "source_trace_for_answer" else "document_lookup"
        intent["secondary"] = _unique([target_document_need_contract.get("need_type"), "document"], limit=8)
        intent["requires_first_person"] = False
        intent["requires_broad_context"] = False
        intent["requires_document_mode"] = True
        contract["expected_evidence"] = [document_evidence]
        contract["semantic_slot_contracts"] = _semantic_slot_contracts_from_targets(
            query_text=query_text,
            expected_evidence=[document_evidence],
            required_slots=["document"],
            optional_slots=[],
            legacy_contract=legacy_contract,
        )
        context_contract["required_sections"] = ["documents"]
        context_contract["optional_sections"] = []
        context_contract["suppressed_normal_slots"] = list(target_document_need_contract.get("suppressed_normal_slots") or [])
    else:
        intent["requires_document_mode"] = True
        existing_evidence = [dict(item) for item in list(contract.get("expected_evidence") or []) if isinstance(item, dict)]
        if not any(str(item.get("target_id") or "").strip() == "document" for item in existing_evidence):
            existing_evidence.append(document_evidence)
        contract["expected_evidence"] = existing_evidence
        required_slots = [
            str(item.get("slot_id") or item.get("target_id") or "").strip()
            for item in list(contract.get("semantic_slot_contracts") or [])
            if isinstance(item, dict) and bool(item.get("required")) and str(item.get("slot_id") or item.get("target_id") or "").strip()
        ]
        if not any(slot and slot != "document" for slot in required_slots):
            inferred_normal_slot = "work_company" if _organization_query(query_text) else "identity"
            required_slots = [inferred_normal_slot, *required_slots]
        required_slots = _unique([*required_slots, "document"], limit=12)
        optional_slots = [
            str(item.get("slot_id") or item.get("target_id") or "").strip()
            for item in list(contract.get("semantic_slot_contracts") or [])
            if isinstance(item, dict) and not bool(item.get("required")) and str(item.get("slot_id") or item.get("target_id") or "").strip()
        ]
        contract["semantic_slot_contracts"] = _semantic_slot_contracts_from_targets(
            query_text=query_text,
            expected_evidence=existing_evidence,
            required_slots=required_slots,
            optional_slots=optional_slots,
            legacy_contract=legacy_contract,
        )
        required_sections_from_slots = [
            _SEMANTIC_SLOT_SECTIONS.get(str(item.get("slot_id") or "").strip(), "history")
            for item in list(contract.get("semantic_slot_contracts") or [])
            if isinstance(item, dict) and bool(item.get("required"))
        ]
        required_sections = _unique([*list(context_contract.get("required_sections") or []), *required_sections_from_slots, "documents"], limit=12)
        context_contract["required_sections"] = required_sections
        context_contract["optional_sections"] = [
            section for section in list(context_contract.get("optional_sections") or []) if section not in set(required_sections)
        ]

    contract["intent"] = intent
    contract["document_contract"] = document_contract
    contract["context_contract"] = context_contract
    _sync_context_contract_slot_keys(contract)
    _rebuild_document_need_landing_plan(
        contract,
        pure_document_evidence=pure_document_evidence,
        document_mode=document_mode,
    )
    contract["ai_required"] = bool(contract.get("ai_required") or document_mode != "none")
    contract["deterministic_seal_allowed"] = bool(contract.get("deterministic_seal_allowed")) and document_mode == "none"
    return contract


def _ensure_work_inventory_expected_evidence(contract: dict[str, Any]) -> None:
    query_text = str(contract.get("user_query") or "")
    if not _work_entity_inventory_query(query_text):
        return
    inventory_claim_shape = (
        "person work/company inventory: multiple company or organization names with the subject's "
        "role, founding, operating or affiliation relation"
    )
    inventory_fields = ["person", "company_or_organization", "role_or_relation", "multiple_work_entities"]
    inventory_question = "Does promoted evidence expose a usable work/company inventory, not only one isolated role claim?"
    expected = [item for item in list(contract.get("expected_evidence") or []) if isinstance(item, dict)]
    for item in expected:
        target_id = str(item.get("target_id") or "").strip()
        generalized = _generalized_slot_id(
            target_id,
            query_text=query_text,
            claim_shape=item.get("claim_shape"),
            required_fields=list(item.get("required_fields") or []),
            fallback_slot=target_id,
        )
        if generalized == "work_company" or target_id in {"work", "work_detail", "company_founding"}:
            item["claim_shape"] = inventory_claim_shape
            item["ai_freeform_goal"] = inventory_claim_shape
            item["ai_target_text"] = inventory_claim_shape
            item["required_fields"] = inventory_fields
            item["minimum_support"] = {"node_count": 2, "document_chunk_count": 0, "source_trace_count": 0}
            item["success_question"] = inventory_question
            contract["expected_evidence"] = expected
            return
    expected.append(
        {
            "target_id": "work",
            "claim_shape": inventory_claim_shape,
            "required_fields": inventory_fields,
            "acceptable_sources": ["verified_public", "user_asserted", "uploaded_document"],
            "minimum_support": {"node_count": 2, "document_chunk_count": 0, "source_trace_count": 0},
            "negative_conditions": _negative_conditions("work", dict(contract.get("legacy_contract") or {})),
            "success_question": inventory_question,
        }
    )
    contract["expected_evidence"] = expected


def _build_path_itinerary(
    landing_hypotheses: list[dict[str, Any]],
    *,
    preferred_strategy: str,
    document_mode: str,
    source: str,
) -> list[dict[str, Any]]:
    landings = [dict(item) for item in list(landing_hypotheses or []) if isinstance(item, dict)]
    if not landings:
        return []
    preferred_edges = ["highway", "semantic_link", "document_reference", "temporal_link"]
    if document_mode != "none":
        preferred_edges = ["document_reference", "semantic_link", "highway", "temporal_link"]
    paths: list[dict[str, Any]] = []
    for index, source_landing in enumerate(landings):
        source_id = str(source_landing.get("landing_id") or f"L{index + 1}").strip()
        source_targets = ", ".join(str(item) for item in list(source_landing.get("target_evidence_ids") or []) if str(item).strip())
        paths.append(
            {
                "path_id": f"P{index + 1}",
                "route_kind": "landing_origin_corridor",
                "origin_landing_id": source_id,
                "from_landing_id": source_id,
                "target_landing_id": None,
                "to_landing_id": "",
                "why_traverse": (
                    f"From landing {source_targets or source_id}, traverse the branch-local corridor "
                    "to inspect intermediate, nearby, highway, semantic-link and document evidence."
                ),
                "read_intermediate_nodes": True,
                "max_intermediate_nodes": 12 if preferred_strategy != "single_if_sufficient" else 6,
                "preferred_edges": preferred_edges,
                "planner_source": source,
            }
        )
    return paths


def _normalize_ai_path_itinerary(
    ai_paths: list[Any],
    *,
    fallback_paths: list[dict[str, Any]],
    allowed_landing_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    allowed_ids = {str(item or "").strip() for item in (allowed_landing_ids or set()) if str(item or "").strip()}
    for index, raw_path in enumerate(list(ai_paths or [])):
        if not isinstance(raw_path, dict):
            continue
        from_landing_id = str(raw_path.get("from_landing_id") or raw_path.get("origin_landing_id") or "").strip()
        target_landing_id = str(raw_path.get("target_landing_id") or raw_path.get("to_landing_id") or "").strip()
        route_kind = str(raw_path.get("route_kind") or "landing_origin_corridor").strip().lower()
        if route_kind not in {"landing_origin_corridor", "explicit_cross_landing_bridge"}:
            route_kind = "landing_origin_corridor"
        if not from_landing_id:
            continue
        if allowed_ids and from_landing_id not in allowed_ids:
            continue
        if route_kind == "explicit_cross_landing_bridge":
            if not target_landing_id or target_landing_id == from_landing_id:
                continue
            if allowed_ids and target_landing_id not in allowed_ids:
                continue
        else:
            target_landing_id = ""
        preferred_edges = _unique(list(raw_path.get("preferred_edges") or []), limit=6)
        if not preferred_edges:
            preferred_edges = ["highway", "semantic_link", "document_reference", "temporal_link"]
        normalized.append(
            {
                "path_id": str(raw_path.get("path_id") or f"P{index + 1}").strip() or f"P{index + 1}",
                "route_kind": route_kind,
                "origin_landing_id": from_landing_id,
                "from_landing_id": from_landing_id,
                "target_landing_id": target_landing_id or None,
                "to_landing_id": target_landing_id,
                "why_traverse": str(raw_path.get("why_traverse") or "Inspect branch-local corridor evidence from this landing.").strip(),
                "read_intermediate_nodes": bool(raw_path.get("read_intermediate_nodes", True)),
                "max_intermediate_nodes": max(1, min(24, int(raw_path.get("max_intermediate_nodes") or 12))),
                "preferred_edges": preferred_edges,
                "planner_source": "ai_compiled",
            }
        )
        if len(normalized) >= 8:
            break
    return normalized or fallback_paths


def _build_fallback_contract(
    *,
    query_text: str,
    retrieval_mode: str,
    legacy_contract: dict[str, Any],
    source: str,
    ai_status: str,
    tool_name: str | None = None,
    ai_error: str | None = None,
) -> dict[str, Any]:
    intent_primary = _contract_intent_from_legacy(query_text, legacy_contract)
    document_mode = _document_mode(query_text, intent_primary)
    exact_field_request = extract_exact_user_field_request(query_text)
    required_slots = _unique(list(legacy_contract.get("required_slots") or []), limit=12) or ["identity"]
    if exact_field_request:
        intent_primary = "unknown"
        document_mode = "none"
        required_slots = [str(exact_field_request.get("slot_id") or "exact_user_field")]
    if _broad_self_query(query_text):
        required_slots = ["identity", "work"]
    if _organization_query(query_text):
        required_slots = _unique(["work", *required_slots], limit=12)
    optional_slots = _unique(list(legacy_contract.get("optional_slots") or []), limit=12)
    if _broad_self_query(query_text):
        optional_slots = ["relationships", "style", "values", "history", "documents"]
    fast_final_allowed = bool(legacy_contract.get("fast_final_allowed"))
    requires_expansion = bool(legacy_contract.get("requires_expansion"))
    ai_available = bool(llm_enabled())
    ai_required = bool(ai_available or legacy_contract.get("ai_validation_required") or requires_expansion or document_mode != "none")
    deterministic_seal_allowed = bool(fast_final_allowed and not ai_required and not requires_expansion and document_mode == "none")
    mode_landing_cap = _mode_landing_cap(retrieval_mode)
    min_landings = max(1, min(mode_landing_cap, int(legacy_contract.get("min_landing_count") or 1)))
    max_landings = 1 if deterministic_seal_allowed else max(min_landings, min(mode_landing_cap, max(2, len(required_slots) + len(optional_slots[:2]))))
    preferred_strategy = "document_first" if document_mode != "none" else "multi_area" if max_landings > 1 else "single_if_sufficient"
    expected_evidence = _build_expected_evidence(
        intent_primary=intent_primary,
        legacy_contract=legacy_contract,
        query_text=query_text,
    )
    if exact_field_request:
        expected_evidence = [
            {
                "target_id": str(exact_field_request.get("slot_id") or "exact_user_field"),
                "claim_shape": str(exact_field_request.get("field_label") or "requested exact field"),
                "required_fields": ["person", "requested_exact_field", f"field:{exact_field_request.get('field_key') or ''}"],
                "acceptable_sources": ["verified_public", "user_asserted", "uploaded_document"],
                "minimum_support": {"node_count": 1, "document_chunk_count": 0, "source_trace_count": 0},
                "negative_conditions": [
                    "do_not_use_adjacent_biography_without_requested_field",
                    "do_not_use_related_profile_context_as_exact_field",
                ],
                "success_question": f"Does promoted evidence explicitly contain {exact_field_request.get('field_label')} for this query?",
            }
        ]
    semantic_slot_contracts = _semantic_slot_contracts_from_targets(
        query_text=query_text,
        expected_evidence=expected_evidence,
        required_slots=required_slots,
        optional_slots=optional_slots,
        legacy_contract=legacy_contract,
    )
    forbidden_topics = _unique(list(legacy_contract.get("disallowed_topics") or []), limit=12)
    landing_hypotheses = [
        {
            "landing_id": f"L{index + 1}",
            "target_evidence_ids": [target["target_id"]],
            "textual_probe": target["claim_shape"],
            "route_budget": {
                "max_hops": 5 if not deterministic_seal_allowed else 2,
                "max_nodes": 24 if not deterministic_seal_allowed else 8,
                "max_document_chunks": 8 if document_mode != "none" else 2,
            },
        }
        for index, target in enumerate(expected_evidence[:max_landings])
    ]
    contract = {
        "schema_version": "agvm.semantic_query_contract.v2",
        "contract_version": "2.0",
        "contract_authority": source,
        "compiler_status": ai_status,
        "compiler_error": ai_error,
        "user_query": str(query_text or ""),
        "mcp_tool_name": str(tool_name or "").strip() or None,
        "retrieval_mode": str(retrieval_mode or "balanced"),
        "legacy_contract": dict(legacy_contract or {}),
        "exact_field_request": dict(exact_field_request or {}),
        "intent": {
            "primary": intent_primary,
            "secondary": _unique([legacy_contract.get("query_kind"), *required_slots], limit=8),
            "is_followup": False,
            "requires_first_person": _requires_first_person(query_text),
            "requires_document_mode": document_mode != "none",
            "requires_broad_context": intent_primary == "broad_dossier" or str(legacy_contract.get("answer_width") or "") == "dossier",
        },
        "entities": [],
        "expected_evidence": expected_evidence,
        "semantic_slot_contract_version": "agvm.semantic_slot_contract.v1",
        "semantic_slot_contracts": semantic_slot_contracts,
        "forbidden_evidence": [
            {"topic": topic, "reason": "legacy_contract_disallowed_topic"}
            for topic in forbidden_topics
        ]
        + [
            {"topic": "system_metadata", "reason": "never_answer_from_system_metadata"},
            {"topic": "synthetic_test_material", "reason": "never_answer_from_synthetic_test_material"},
        ],
        "landing_plan": {
            "min_landings": min_landings,
            "max_landings": max_landings,
            "preferred_strategy": preferred_strategy,
            "landing_hypotheses": landing_hypotheses,
            "paths": _build_path_itinerary(
                landing_hypotheses,
                preferred_strategy=preferred_strategy,
                document_mode=document_mode,
                source=source,
            ),
            "cooperation_policy": {
                "shared_reservoir": True,
                "dedupe_evidence": True,
                "allow_early_stop_from_one_landing": True,
            },
        },
        "document_contract": {
            "mode": document_mode,
            "query_title_or_topic": str(query_text or ""),
            "required_entities": [],
            "minimum_exact_fit": 0.82,
            "minimum_related_fit": 0.62,
            "require_title_overlap": document_mode == "exact_document_lookup",
            "require_entity_overlap": document_mode != "none",
            "allow_related_when_exact_missing": False,
            "no_match_policy": "strict",
            "source_trace_policy": "claim_exact_only",
        },
        "context_contract": {
            "dossier_goal": "document_review" if document_mode != "none" else "context_for_clone" if intent_primary == "broad_dossier" else "answer_support",
            "hot_context_policy": "contract_relevant_only",
            "cold_context_policy": "reservoir_not_promoted",
            "required_sections": required_slots,
            "optional_sections": optional_slots,
            "semantic_required_slot_keys": [
                str(item.get("slot_key") or "")
                for item in semantic_slot_contracts
                if bool(item.get("required")) and str(item.get("slot_key") or "")
            ],
            "semantic_optional_slot_keys": [
                str(item.get("slot_key") or "")
                for item in semantic_slot_contracts
                if not bool(item.get("required")) and str(item.get("slot_key") or "")
            ],
            "exact_field_requirements": [dict(exact_field_request)] if exact_field_request else [],
            "max_hot_tokens": 1800,
            "max_cold_tokens": 5000,
            "promotion_rules": [
                "fragment_maps_to_expected_evidence",
                "source_is_answer_eligible",
                "not_forbidden_evidence",
                "not_system_metadata",
                "not_synthetic_test",
            ],
            "demotion_rules": [
                "off_contract_but_potentially_useful",
                "duplicate_read",
                "low_fit",
                "background_only",
            ],
        },
        "answer_contract": {
            "voice": "first_person" if _requires_first_person(query_text) else "neutral",
            "language": "it",
            "style": "document_list" if document_mode in {"exact_document_lookup", "related_document_lookup"} else "source_trace" if document_mode == "source_trace_for_answer" else "human_clone",
            "must_answer_directly": True,
            "allow_uncertainty": True,
            "forbid_raw_ledger": True,
            "forbid_source_metadata_as_fact": True,
            "forbid_unrelated_context": True,
            "required_citations_mode": "document_titles" if document_mode != "none" else "none",
            "max_chars_first_answer": 700,
            "max_chars_final_answer": 1800 if intent_primary != "broad_dossier" else 3600,
        },
        "stop_contract": {
            "required_passes": [
                "evidence_contract_satisfied",
                "context_contract_satisfied",
                "answer_contract_satisfied",
            ],
            "conditional_passes": [
                "document_contract_satisfied_if_document_mode",
                "ai_validation_satisfied_if_ai_required",
            ],
            "allow_provisional": True,
            "allow_insufficient": True,
            "max_total_ms": 30000,
            "first_answer_target_ms": 2000,
            "final_answer_target_ms": 15000,
        },
        "ai_required": ai_required,
        "deterministic_seal_allowed": deterministic_seal_allowed,
    }
    return _apply_target_document_need_contract(contract)


def _ai_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "intent_primary",
            "intent_secondary",
            "requires_first_person",
            "requires_document_mode",
            "requires_broad_context",
            "expected_evidence",
            "forbidden_topics",
            "landing_strategy",
            "min_landings",
            "max_landings",
            "document_mode",
            "answer_voice",
            "answer_style",
            "must_answer_directly",
            "ai_required",
            "deterministic_seal_allowed",
            "rationale",
        ],
        "properties": {
            "intent_primary": {
                "type": "string",
                "enum": [
                    "identity",
                    "work",
                    "relationship",
                    "document_lookup",
                    "source_trace",
                    "temporal",
                    "broad_dossier",
                    "followup",
                    "correction",
                    "unknown",
                ],
            },
            "intent_secondary": {"type": "array", "maxItems": 8, "items": {"type": "string"}},
            "requires_first_person": {"type": "boolean"},
            "requires_document_mode": {"type": "boolean"},
            "requires_broad_context": {"type": "boolean"},
            "expected_evidence": {
                "type": "array",
                "minItems": 1,
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["target_id", "claim_shape", "required_fields", "negative_conditions", "success_question"],
                    "properties": {
                        "target_id": {"type": "string", "enum": sorted(_CANONICAL_TARGET_IDS)},
                        "claim_shape": {"type": "string"},
                        "required_fields": {"type": "array", "maxItems": 8, "items": {"type": "string"}},
                        "negative_conditions": {"type": "array", "maxItems": 8, "items": {"type": "string"}},
                        "success_question": {"type": "string"},
                    },
                },
            },
            "forbidden_topics": {"type": "array", "maxItems": 10, "items": {"type": "string"}},
            "landing_strategy": {"type": "string", "enum": ["single_if_sufficient", "multi_area", "document_first", "warm_first"]},
            "min_landings": {"type": "integer", "minimum": 1, "maximum": 12},
            "max_landings": {"type": "integer", "minimum": 1, "maximum": 12},
            "path_itinerary": {
                "type": "array",
                "maxItems": 12,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["path_id", "route_kind", "origin_landing_id", "from_landing_id", "target_landing_id", "to_landing_id", "why_traverse", "read_intermediate_nodes", "max_intermediate_nodes", "preferred_edges"],
                    "properties": {
                        "path_id": {"type": "string"},
                        "route_kind": {"type": "string", "enum": ["landing_origin_corridor", "explicit_cross_landing_bridge"]},
                        "origin_landing_id": {"type": "string"},
                        "from_landing_id": {"type": "string"},
                        "target_landing_id": {"type": ["string", "null"]},
                        "to_landing_id": {"type": "string"},
                        "why_traverse": {"type": "string"},
                        "read_intermediate_nodes": {"type": "boolean"},
                        "max_intermediate_nodes": {"type": "integer", "minimum": 1, "maximum": 24},
                        "preferred_edges": {"type": "array", "maxItems": 6, "items": {"type": "string"}},
                    },
                },
            },
            "document_mode": {
                "type": "string",
                "enum": ["none", "exact_document_lookup", "related_document_lookup", "document_synthesis", "source_trace_for_answer"],
            },
            "answer_voice": {"type": "string", "enum": ["first_person", "third_person", "neutral"]},
            "answer_style": {"type": "string", "enum": ["human_clone", "technical_summary", "document_list", "source_trace"]},
            "must_answer_directly": {"type": "boolean"},
            "ai_required": {"type": "boolean"},
            "deterministic_seal_allowed": {"type": "boolean"},
            "rationale": {"type": "string"},
        },
    }


def _ai_compact_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "intent_primary",
            "intent_secondary",
            "requires_first_person",
            "requires_document_mode",
            "requires_broad_context",
            "evidence_targets",
            "forbidden_topics",
            "landing_strategy",
            "min_landings",
            "max_landings",
            "path_itinerary",
            "document_mode",
            "answer_voice",
            "answer_style",
            "must_answer_directly",
            "ai_required",
            "deterministic_seal_allowed",
        ],
        "properties": {
            "intent_primary": {
                "type": "string",
                "enum": [
                    "identity",
                    "work",
                    "relationship",
                    "document_lookup",
                    "source_trace",
                    "temporal",
                    "broad_dossier",
                    "followup",
                    "correction",
                    "unknown",
                ],
            },
            "intent_secondary": {"type": "array", "maxItems": 6, "items": {"type": "string"}},
            "requires_first_person": {"type": "boolean"},
            "requires_document_mode": {"type": "boolean"},
            "requires_broad_context": {"type": "boolean"},
            "evidence_targets": {
                "type": "array",
                "minItems": 1,
                "maxItems": 8,
                "items": {"type": "string", "enum": sorted(_CANONICAL_TARGET_IDS)},
            },
            "forbidden_topics": {"type": "array", "maxItems": 6, "items": {"type": "string"}},
            "landing_strategy": {"type": "string", "enum": ["single_if_sufficient", "multi_area", "document_first", "warm_first"]},
            "min_landings": {"type": "integer", "minimum": 1, "maximum": 12},
            "max_landings": {"type": "integer", "minimum": 1, "maximum": 12},
            "path_itinerary": {
                "type": "array",
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["path_id", "route_kind", "from_landing_id", "to_landing_id", "why_traverse", "read_intermediate_nodes", "max_intermediate_nodes", "preferred_edges"],
                    "properties": {
                        "path_id": {"type": "string"},
                        "route_kind": {"type": "string", "enum": ["landing_origin_corridor", "explicit_cross_landing_bridge"]},
                        "from_landing_id": {"type": "string"},
                        "to_landing_id": {"type": "string"},
                        "why_traverse": {"type": "string"},
                        "read_intermediate_nodes": {"type": "boolean"},
                        "max_intermediate_nodes": {"type": "integer", "minimum": 1, "maximum": 24},
                        "preferred_edges": {"type": "array", "maxItems": 5, "items": {"type": "string"}},
                    },
                },
            },
            "document_mode": {
                "type": "string",
                "enum": ["none", "exact_document_lookup", "related_document_lookup", "document_synthesis", "source_trace_for_answer"],
            },
            "answer_voice": {"type": "string", "enum": ["first_person", "third_person", "neutral"]},
            "answer_style": {"type": "string", "enum": ["human_clone", "technical_summary", "document_list", "source_trace"]},
            "must_answer_directly": {"type": "boolean"},
            "ai_required": {"type": "boolean"},
            "deterministic_seal_allowed": {"type": "boolean"},
        },
    }


def _ai_ultra_first_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "intent_primary",
            "intent_secondary",
            "requires_first_person",
            "requires_document_mode",
            "requires_broad_context",
            "evidence_targets",
            "forbidden_topics",
            "landing_strategy",
            "min_landings",
            "max_landings",
            "document_mode",
            "answer_voice",
            "answer_style",
            "must_answer_directly",
            "ai_required",
            "deterministic_seal_allowed",
        ],
        "properties": {
            "intent_primary": {
                "type": "string",
                "enum": [
                    "identity",
                    "work",
                    "relationship",
                    "document_lookup",
                    "source_trace",
                    "temporal",
                    "broad_dossier",
                    "followup",
                    "correction",
                    "unknown",
                ],
            },
            "intent_secondary": {"type": "array", "maxItems": 4, "items": {"type": "string"}},
            "requires_first_person": {"type": "boolean"},
            "requires_document_mode": {"type": "boolean"},
            "requires_broad_context": {"type": "boolean"},
            "evidence_targets": {
                "type": "array",
                "minItems": 1,
                "maxItems": 5,
                "items": {"type": "string", "enum": sorted(_CANONICAL_TARGET_IDS)},
            },
            "forbidden_topics": {"type": "array", "maxItems": 4, "items": {"type": "string"}},
            "landing_strategy": {"type": "string", "enum": ["single_if_sufficient", "multi_area", "document_first", "warm_first"]},
            "min_landings": {"type": "integer", "minimum": 1, "maximum": 8},
            "max_landings": {"type": "integer", "minimum": 1, "maximum": 8},
            "document_mode": {
                "type": "string",
                "enum": ["none", "exact_document_lookup", "related_document_lookup", "document_synthesis", "source_trace_for_answer"],
            },
            "answer_voice": {"type": "string", "enum": ["first_person", "third_person", "neutral"]},
            "answer_style": {"type": "string", "enum": ["human_clone", "technical_summary", "document_list", "source_trace"]},
            "must_answer_directly": {"type": "boolean"},
            "ai_required": {"type": "boolean"},
            "deterministic_seal_allowed": {"type": "boolean"},
        },
    }


def _semantic_compiler_profile(retrieval_mode: str, *, profile_variant: str | None = None) -> dict[str, Any]:
    mode = str(retrieval_mode or "balanced").strip().lower()
    variant = str(profile_variant or "compact_first").strip().lower()
    if variant == "ultra_first":
        if mode == "forensic":
            max_output_tokens = 420
        elif mode == "heavy":
            max_output_tokens = 360
        elif mode == "flash":
            max_output_tokens = 220
        else:
            max_output_tokens = 280
        return {
            "profile": "ultra_first",
            "schema_name": "agvm_semantic_query_contract_v2_ultra_first",
            "schema": _ai_ultra_first_schema(),
            "max_output_tokens": max_output_tokens,
        }
    if mode == "forensic":
        max_output_tokens = 900
    elif mode == "heavy":
        max_output_tokens = 780
    elif mode == "flash":
        max_output_tokens = 480
    else:
        max_output_tokens = 640
    return {
        "profile": "compact_first",
        "schema_name": "agvm_semantic_query_contract_v2_compact",
        "schema": _ai_compact_schema(),
        "max_output_tokens": max_output_tokens,
    }


def _semantic_retry_timeout_seconds(retrieval_mode: str, timeout: float | None) -> float:
    mode = str(retrieval_mode or "balanced").strip().lower()
    base = float(timeout or 8.5)
    if mode == "forensic":
        return max(8.0, min(14.0, base * 0.65))
    if mode == "heavy":
        return max(7.0, min(11.0, base * 0.65))
    if mode == "flash":
        return max(3.5, min(6.0, base * 0.55))
    return max(5.5, min(9.0, base * 0.65))


def _mode_landing_cap(retrieval_mode: str) -> int:
    mode = str(retrieval_mode or "balanced").strip().lower()
    if mode == "flash":
        return 3
    if mode in {"heavy", "forensic"}:
        return 12
    return 6


def _mode_path_cap(retrieval_mode: str) -> int:
    mode = str(retrieval_mode or "balanced").strip().lower()
    if mode == "flash":
        return 3
    if mode == "heavy":
        return 12
    if mode == "forensic":
        return 16
    return 6


def _compact_identity_hints_for_retry(identity_hints: dict[str, Any] | None) -> dict[str, Any]:
    hints = _sanitize_identity_hints(identity_hints)
    return {
        "core_name": hints.get("core_name"),
        "aliases": list(hints.get("aliases") or [])[:3],
        "role_candidates": list(hints.get("role_candidates") or [])[:4],
        "project_candidates": list(hints.get("project_candidates") or [])[:5],
        "employer_candidates": list(hints.get("employer_candidates") or [])[:3],
        "core_nodes": list(hints.get("core_nodes") or [])[:4],
    }


def _semantic_retry_prompt_payload(
    *,
    query_text: str,
    retrieval_mode: str,
    legacy_contract: dict[str, Any],
    fallback_contract: dict[str, Any],
    identity_hints: dict[str, Any] | None,
    primary_error: str | None,
) -> dict[str, Any]:
    return {
        "retry_reason": primary_error or "primary_compiler_empty",
        "query": query_text,
        "retrieval_mode": retrieval_mode,
        "legacy_query_kind": legacy_contract.get("query_kind"),
        "legacy_required_slots": list(legacy_contract.get("required_slots") or [])[:8],
        "legacy_requested_aspects": list(legacy_contract.get("requested_aspects") or [])[:8],
        "legacy_disallowed_topics": list(legacy_contract.get("disallowed_topics") or [])[:8],
        "fallback_intent": dict(fallback_contract.get("intent") or {}),
        "fallback_required_sections": list((fallback_contract.get("context_contract") or {}).get("required_sections") or [])[:8],
        "fallback_optional_sections": list((fallback_contract.get("context_contract") or {}).get("optional_sections") or [])[:8],
        "fallback_document_mode": ((fallback_contract.get("document_contract") or {}).get("mode") or "none"),
        "canonical_target_ids": sorted(_CANONICAL_TARGET_IDS),
        "identity_hints_compact": _compact_identity_hints_for_retry(identity_hints),
    }


def _merge_ai_payload(
    *,
    fallback_contract: dict[str, Any],
    ai_payload: dict[str, Any],
) -> dict[str, Any]:
    contract = json.loads(json.dumps(fallback_contract))
    ai_payload = dict(ai_payload or {})
    if not list(ai_payload.get("expected_evidence") or []):
        compact_targets = _unique(list(ai_payload.get("evidence_targets") or []), limit=8)
        ai_payload["expected_evidence"] = [
            {
                "target_id": target_id,
                "claim_shape": _claim_shape_for_slot(str(target_id), str(ai_payload.get("intent_primary") or contract["intent"]["primary"])),
                "required_fields": _required_fields_for_slot(str(target_id)),
                "negative_conditions": _negative_conditions(str(target_id), dict(contract.get("legacy_contract") or {})),
                "success_question": f"Does promoted evidence directly satisfy the {target_id} target for this query?",
            }
            for target_id in compact_targets
        ]
    intent_primary = str(ai_payload.get("intent_primary") or contract["intent"]["primary"])
    document_mode = str(ai_payload.get("document_mode") or contract["document_contract"]["mode"])
    exact_field_request = extract_exact_user_field_request(str(contract.get("user_query") or ""))
    legacy_view = dict(contract.get("legacy_contract") or {})
    fallback_context = dict(contract.get("context_contract") or {})
    broad_self_query = bool(
        _broad_self_query(str(contract.get("user_query") or ""))
        or str(legacy_view.get("query_kind") or "") == "broad_profile"
        or str(legacy_view.get("answer_width") or "") == "dossier"
        or bool(dict(contract.get("intent") or {}).get("requires_broad_context"))
        or str(fallback_context.get("dossier_goal") or "") == "context_for_clone"
    )
    if exact_field_request:
        intent_primary = "unknown"
        document_mode = "none"
    if broad_self_query:
        intent_primary = "broad_dossier"
    mode_landing_cap = _mode_landing_cap(str(contract.get("retrieval_mode") or "balanced"))
    min_landings = max(1, min(mode_landing_cap, int(ai_payload.get("min_landings") or contract["landing_plan"]["min_landings"] or 1)))
    max_landings = max(min_landings, min(mode_landing_cap, int(ai_payload.get("max_landings") or contract["landing_plan"]["max_landings"] or min_landings)))
    if broad_self_query:
        min_landings = max(min_landings, 3)
        max_landings = max(max_landings, 5)
    contract["contract_authority"] = "ai_compiled"
    contract["compiler_status"] = "completed"
    contract["compiler_error"] = None
    contract["intent"].update(
        {
            "primary": intent_primary,
            "secondary": _unique(list(ai_payload.get("intent_secondary") or []), limit=8),
            "requires_first_person": bool(ai_payload.get("requires_first_person", contract["intent"].get("requires_first_person"))),
            "requires_document_mode": bool(ai_payload.get("requires_document_mode", contract["intent"].get("requires_document_mode"))) or document_mode != "none",
            "requires_broad_context": bool(ai_payload.get("requires_broad_context", contract["intent"].get("requires_broad_context"))) or broad_self_query,
        }
    )
    legacy_required_slots = list((contract.get("legacy_contract") or {}).get("required_slots") or [])
    expected_evidence: list[dict[str, Any]] = []
    for index, item in enumerate(list(ai_payload.get("expected_evidence") or [])):
        if not isinstance(item, dict):
            continue
        required_fields = _unique(list(item.get("required_fields") or []), limit=8)
        raw_target_id = str(item.get("target_id") or item.get("id") or "").strip()
        raw_claim_shape = str(item.get("claim_shape") or "").strip()
        raw_goal = str(
            item.get("freeform_goal")
            or item.get("goal")
            or item.get("target_text")
            or item.get("textual_probe")
            or item.get("answer_hypothesis")
            or raw_claim_shape
            or raw_target_id
        ).strip()
        target_id = _canonical_target_id(
            item.get("target_id"),
            claim_shape=item.get("claim_shape"),
            required_fields=required_fields,
            query_text=str(contract.get("user_query") or ""),
            intent_primary=intent_primary,
            fallback_slot=str((legacy_required_slots[index] if index < len(legacy_required_slots) else "") or ""),
        )
        if not target_id:
            continue
        if not _target_allowed_by_query_context(
            target_id,
            query_text=str(contract.get("user_query") or ""),
            legacy_contract=dict(contract.get("legacy_contract") or {}),
        ):
            continue
        claim_shape = _claim_shape_for_target(target_id, str(item.get("claim_shape") or ""))
        expected_evidence.append(
            {
                "target_id": target_id,
                "canonical_target_id": target_id,
                "raw_ai_target_id": raw_target_id or target_id,
                "raw_ai_claim_shape": raw_claim_shape or claim_shape,
                "ai_freeform_goal": raw_goal,
                "ai_target_text": str(item.get("target_text") or item.get("textual_probe") or raw_goal).strip(),
                "claim_shape": claim_shape,
                "required_fields": required_fields,
                "acceptable_sources": ["verified_public", "user_asserted", "uploaded_document"],
                "minimum_support": {"node_count": 1, "document_chunk_count": 1 if document_mode != "none" else 0, "source_trace_count": 1 if document_mode == "source_trace_for_answer" else 0},
                "negative_conditions": _unique(list(item.get("negative_conditions") or []), limit=8),
                "success_question": str(item.get("success_question") or "").strip(),
            }
    )
    if expected_evidence:
        contract["expected_evidence"] = expected_evidence
    _ensure_work_inventory_expected_evidence(contract)
    if exact_field_request:
        exact_target = {
            "target_id": str(exact_field_request.get("slot_id") or "exact_user_field"),
            "claim_shape": str(exact_field_request.get("field_label") or "requested exact field"),
            "required_fields": ["person", "requested_exact_field", f"field:{exact_field_request.get('field_key') or ''}"],
            "acceptable_sources": ["verified_public", "user_asserted", "uploaded_document"],
            "minimum_support": {"node_count": 1, "document_chunk_count": 0, "source_trace_count": 0},
            "negative_conditions": [
                "do_not_use_adjacent_biography_without_requested_field",
                "do_not_use_related_profile_context_as_exact_field",
            ],
            "success_question": f"Does promoted evidence explicitly contain {exact_field_request.get('field_label')} for this query?",
        }
        contract["expected_evidence"] = [exact_target]
    if broad_self_query:
        existing_targets = {str(item.get("target_id") or "") for item in contract["expected_evidence"] if isinstance(item, dict)}
        broad_targets = [
            ("identity", "person identity, self description, stable role and profile claim"),
            ("work", "person work, role, company, project, or operating activity"),
            ("style", "communication style and voice evidence"),
            ("values", "values, principles, or operating philosophy evidence"),
            ("history", "dated, temporal, historical, or biographical event evidence"),
        ]
        for target_id, claim_shape in broad_targets:
            if target_id in existing_targets:
                continue
            contract["expected_evidence"].append(
                {
                    "target_id": target_id,
                    "claim_shape": claim_shape,
                    "required_fields": _required_fields_for_slot(target_id),
                    "acceptable_sources": ["verified_public", "user_asserted", "uploaded_document"],
                    "minimum_support": {"node_count": 1, "document_chunk_count": 0, "source_trace_count": 0},
                    "negative_conditions": _negative_conditions(target_id, dict(contract.get("legacy_contract") or {})),
                    "success_question": f"Does promoted evidence directly support the {target_id} part of the broad self dossier?",
                }
            )
            existing_targets.add(target_id)
    if _organization_query(str(contract.get("user_query") or "")) and intent_primary in {"temporal", "unknown", "identity"}:
        intent_primary = "work"
        contract["intent"]["primary"] = intent_primary
    if exact_field_request:
        intent_primary = "unknown"
        contract["intent"]["primary"] = "unknown"
        contract["intent"]["requires_document_mode"] = False
        contract["intent"]["requires_broad_context"] = False
    forbidden_topics = _unique(list(ai_payload.get("forbidden_topics") or []), limit=10)
    contract["forbidden_evidence"] = [
        {"topic": topic, "reason": "ai_contract_forbidden_topic"}
        for topic in forbidden_topics
    ] + [
        {"topic": "system_metadata", "reason": "never_answer_from_system_metadata"},
        {"topic": "synthetic_test_material", "reason": "never_answer_from_synthetic_test_material"},
    ]
    contract["landing_plan"]["min_landings"] = min_landings
    contract["landing_plan"]["max_landings"] = max_landings
    contract["landing_plan"]["preferred_strategy"] = str(ai_payload.get("landing_strategy") or contract["landing_plan"]["preferred_strategy"])
    contract["landing_plan"]["landing_hypotheses"] = [
        {
            "landing_id": f"L{index + 1}",
            "target_evidence_ids": [target["target_id"]],
            "textual_probe": str(target.get("ai_freeform_goal") or target.get("ai_target_text") or target.get("claim_shape") or ""),
            "canonical_target_id": str(target.get("canonical_target_id") or target.get("target_id") or ""),
            "raw_ai_target_id": str(target.get("raw_ai_target_id") or target.get("target_id") or ""),
            "ai_freeform_goal": str(target.get("ai_freeform_goal") or target.get("claim_shape") or ""),
            "route_budget": {
                "max_hops": 5,
                "max_nodes": 24,
                "max_document_chunks": 8 if document_mode != "none" else 2,
            },
        }
        for index, target in enumerate(contract["expected_evidence"][:max_landings])
    ]
    allowed_landing_ids = {
        str(item.get("landing_id") or "").strip()
        for item in list(contract["landing_plan"]["landing_hypotheses"] or [])
        if isinstance(item, dict)
    }
    fallback_paths = _build_path_itinerary(
        list(contract["landing_plan"]["landing_hypotheses"] or []),
        preferred_strategy=str(contract["landing_plan"]["preferred_strategy"] or "multi_area"),
        document_mode=document_mode,
        source="ai_compiled_inferred",
    )
    contract["landing_plan"]["paths"] = _normalize_ai_path_itinerary(
        list(ai_payload.get("path_itinerary") or []),
        fallback_paths=fallback_paths,
        allowed_landing_ids=allowed_landing_ids,
    )[: _mode_path_cap(str(contract.get("retrieval_mode") or "balanced"))]
    contract["document_contract"]["mode"] = document_mode
    contract["document_contract"]["require_title_overlap"] = document_mode == "exact_document_lookup"
    contract["document_contract"]["require_entity_overlap"] = document_mode != "none"
    if exact_field_request:
        legacy_required_slots = [str(exact_field_request.get("slot_id") or "exact_user_field")]
    legacy_optional_slots = list((contract.get("legacy_contract") or {}).get("optional_slots") or [])
    contract["semantic_slot_contract_version"] = "agvm.semantic_slot_contract.v1"
    contract["semantic_slot_contracts"] = _semantic_slot_contracts_from_targets(
        query_text=str(contract.get("user_query") or ""),
        expected_evidence=list(contract.get("expected_evidence") or []),
        required_slots=legacy_required_slots or [
            str(item.get("target_id") or "")
            for item in list(contract.get("expected_evidence") or [])
            if isinstance(item, dict)
        ],
        optional_slots=legacy_optional_slots,
        legacy_contract=dict(contract.get("legacy_contract") or {}),
    )
    _soften_unrequested_relation_subtypes_for_query(contract)
    if broad_self_query:
        explicitly_required_sections = {
            _canonical_context_section(slot, _claim_shape_for_slot(str(slot or "").strip(), intent_primary))
            for slot in legacy_required_slots
            if str(slot or "").strip()
        }
        hard_sections = {"identity", "work", *explicitly_required_sections}
        for slot_contract in list(contract.get("semantic_slot_contracts") or []):
            slot_section = _SEMANTIC_SLOT_SECTIONS.get(str(slot_contract.get("slot_id") or "").strip())
            if slot_section not in hard_sections:
                slot_contract["required"] = False
                slot_contract["required_softened_reason"] = "broad_self_dossier_optional_context"
    required_sections = _unique(
        [
            _SEMANTIC_SLOT_SECTIONS.get(str(slot_contract.get("slot_id") or "").strip(), "history")
            for slot_contract in list(contract.get("semantic_slot_contracts") or [])
            if isinstance(slot_contract, dict) and bool(slot_contract.get("required"))
        ]
        + [
            _canonical_context_section(slot, _claim_shape_for_slot(str(slot or "").strip(), intent_primary))
            for slot in legacy_required_slots
            if str(slot or "").strip()
        ],
        limit=12,
    )
    optional_sections = _unique(
        [
            _SEMANTIC_SLOT_SECTIONS.get(str(slot_contract.get("slot_id") or "").strip(), "history")
            for slot_contract in list(contract.get("semantic_slot_contracts") or [])
            if isinstance(slot_contract, dict) and not bool(slot_contract.get("required"))
        ]
        + list(dict(contract.get("context_contract") or {}).get("optional_sections") or []),
        limit=12,
    )
    if _organization_query(str(contract.get("user_query") or "")):
        required_sections = _unique(["work", *required_sections], limit=12)
    if _entity_connection_path_query(str(contract.get("user_query") or "")):
        required_sections = [section for section in required_sections if section != "relationships"]
        required_sections = _unique(["work", *required_sections], limit=12)
        for slot_contract in list(contract.get("semantic_slot_contracts") or []):
            if _SEMANTIC_SLOT_SECTIONS.get(str(slot_contract.get("slot_id") or "").strip()) == "relationships":
                slot_contract["required"] = False
                slot_contract["required_softened_reason"] = "entity_connection_path_query_treats_relationship_as_work_graph"
    query_folded = _fold_text(str(contract.get("user_query") or ""))
    explicit_document_request = any(
        marker in query_folded
        for marker in (
            "documento",
            "documenti",
            "document",
            "documents",
            "file",
            "pdf",
            "word",
            "fonte",
            "fonti",
            "source trace",
            "sources",
        )
    )
    if document_mode == "none" and not explicit_document_request and "documents" in required_sections:
        required_sections = [section for section in required_sections if section != "documents"]
    contract["context_contract"]["required_sections"] = required_sections or contract["context_contract"]["required_sections"]
    contract["context_contract"]["optional_sections"] = [
        section for section in optional_sections if section not in set(contract["context_contract"]["required_sections"])
    ]
    if exact_field_request:
        contract["context_contract"]["required_sections"] = [str(exact_field_request.get("section") or "identity")]
        contract["context_contract"]["optional_sections"] = []
        contract["context_contract"]["exact_field_requirements"] = [dict(exact_field_request)]
    if broad_self_query:
        broad_required_sections = _unique(["identity", "work", *required_sections], limit=12)
        broad_optional_sections = [
            section
            for section in ["relationships", "style", "values", "history", "documents", *optional_sections]
            if section not in set(broad_required_sections)
        ]
        contract["context_contract"]["required_sections"] = broad_required_sections
        contract["context_contract"]["optional_sections"] = _unique(broad_optional_sections, limit=12)
    contract["context_contract"]["semantic_required_slot_keys"] = [
        str(item.get("slot_key") or "")
        for item in list(contract.get("semantic_slot_contracts") or [])
        if bool(item.get("required")) and str(item.get("slot_key") or "")
    ]
    contract["context_contract"]["semantic_optional_slot_keys"] = [
        str(item.get("slot_key") or "")
        for item in list(contract.get("semantic_slot_contracts") or [])
        if not bool(item.get("required")) and str(item.get("slot_key") or "")
    ]
    contract["context_contract"]["dossier_goal"] = "document_review" if document_mode != "none" else "context_for_clone" if bool(ai_payload.get("requires_broad_context")) else "answer_support"
    if broad_self_query:
        contract["context_contract"]["dossier_goal"] = "context_for_clone"
    contract["answer_contract"]["voice"] = str(ai_payload.get("answer_voice") or contract["answer_contract"]["voice"])
    contract["answer_contract"]["style"] = str(ai_payload.get("answer_style") or contract["answer_contract"]["style"])
    contract["answer_contract"]["must_answer_directly"] = bool(ai_payload.get("must_answer_directly", contract["answer_contract"].get("must_answer_directly", True)))
    contract["ai_required"] = bool(llm_enabled() or ai_payload.get("ai_required") or contract.get("ai_required"))
    contract["deterministic_seal_allowed"] = bool(ai_payload.get("deterministic_seal_allowed", contract.get("deterministic_seal_allowed"))) and not bool(contract["ai_required"])
    contract["compiler_rationale"] = str(ai_payload.get("rationale") or "").strip()
    return _apply_target_document_need_contract(contract)


def compile_semantic_query_contract(
    *,
    query_text: str,
    retrieval_mode: str,
    legacy_contract: dict[str, Any],
    identity_hints: dict[str, Any] | None = None,
    tool_name: str | None = None,
    allow_ai: bool = True,
    deferred: bool = False,
    timeout: float | None = None,
    brain_revision: str | None = None,
    cache_scope: str | None = None,
    use_cache: bool = True,
    profile_variant: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    started_at = time.perf_counter()
    fallback_status = "deferred" if deferred else "fallback"
    fallback_source = "heuristic_draft" if deferred else "heuristic_fallback"
    requested_timeout = timeout if timeout is not None else 8.5
    retry_timeout = _semantic_retry_timeout_seconds(retrieval_mode, timeout)
    compiler_profile = _semantic_compiler_profile(retrieval_mode, profile_variant=profile_variant)
    compiler_profile_name = str(compiler_profile.get("profile") or "compact_first")
    compiler_schema_name = str(compiler_profile.get("schema_name") or "agvm_semantic_query_contract_v2_compact")
    compiler_max_output_tokens = int(compiler_profile.get("max_output_tokens") or 640)
    fallback_contract = _build_fallback_contract(
        query_text=query_text,
        retrieval_mode=retrieval_mode,
        legacy_contract=legacy_contract,
        source=fallback_source,
        ai_status=fallback_status,
        tool_name=tool_name,
    )
    ai_required = bool(fallback_contract.get("ai_required"))
    sanitized_identity_hints = _sanitize_identity_hints(identity_hints)
    cache_enabled = bool(use_cache and (str(brain_revision or "").strip() or str(cache_scope or "").strip()))
    cache_key = (
        _semantic_contract_cache_key(
            query_text=query_text,
            retrieval_mode=retrieval_mode,
            legacy_contract=legacy_contract,
            fallback_contract=fallback_contract,
            identity_hints=sanitized_identity_hints,
            brain_revision=brain_revision,
            cache_scope=cache_scope,
        )
        if cache_enabled
        else ""
    )

    def finalize_runtime(runtime: dict[str, Any], *, cache_status: str, cache_hit: bool = False) -> dict[str, Any]:
        runtime = dict(runtime or {})
        runtime.setdefault("compiler_ms", round((time.perf_counter() - started_at) * 1000.0, 2))
        runtime.setdefault("model", compiler_model())
        runtime["model_profile"] = _semantic_model_profile(
            retrieval_mode=retrieval_mode,
            timeout=requested_timeout,
            retry_timeout=retry_timeout,
            compiler_profile=compiler_profile_name,
            schema_name=compiler_schema_name,
            max_output_tokens=compiler_max_output_tokens,
        )
        runtime["cache_enabled"] = cache_enabled
        runtime["cache_status"] = cache_status
        runtime["cache_hit"] = cache_hit
        runtime["brain_revision"] = str(brain_revision or "").strip() or None
        runtime["cache_scope"] = str(cache_scope or "").strip() or None
        runtime["cache_key_fingerprint"] = cache_key[:24] if cache_key else None
        runtime["cache"] = {
            "schema_version": "agvm.semantic_contract_cache_runtime.v1",
            "enabled": cache_enabled,
            "status": cache_status,
            "hit": cache_hit,
            "tier": str(runtime.get("cache_tier") or ("memory" if cache_hit else "none")),
            "persistent": cache_enabled,
            "ttl_seconds": _SEMANTIC_CONTRACT_CACHE_TTL_SECONDS if cache_enabled else None,
            "key_fingerprint": cache_key[:24] if cache_key else None,
            "brain_revision": str(brain_revision or "").strip() or None,
            "cache_scope": str(cache_scope or "").strip() or None,
        }
        provider_degraded_reason = _semantic_provider_degraded_reason(runtime)
        provider_degraded = bool(provider_degraded_reason)
        provider_state = _semantic_runtime_provider_state(runtime)
        runtime["provider_state"] = provider_state
        runtime["provider_degraded"] = provider_degraded
        runtime["degraded"] = provider_degraded
        runtime["degraded_reason"] = provider_degraded_reason
        runtime["fresh_provider_call"] = bool(runtime.get("source") == "llm" and not cache_hit)
        runtime["cached_ai_contract"] = bool(cache_hit and runtime.get("material"))
        runtime["provider_retry_policy"] = {
            "schema_version": "agvm.semantic_contract_provider_retry_policy.v1",
            "retry_allowed_on": ["timeout", "provider_error", "rate_limit", "overloaded", "temporarily_unavailable", "api_error", "connection"],
            "retry_used": bool(runtime.get("retry_used")),
            "retry_status": runtime.get("retry_status") or "not_needed",
            "retry_timeout_seconds": runtime.get("retry_timeout_seconds"),
            "primary_error": runtime.get("primary_error"),
            "retry_error": runtime.get("retry_error"),
            "provider_degraded": provider_degraded,
            "provider_state": provider_state,
            "benchmark_retry_allowed": bool(provider_degraded and runtime.get("ai_required") and not runtime.get("material")),
            "silent_heuristic_certification_allowed": False,
        }
        return runtime

    if ai_required and cache_enabled and cache_key:
        cached = _get_semantic_contract_cache_entry(cache_key)
        if cached:
            cached_contract = deepcopy(dict(cached.get("contract") or {}))
            cached_runtime = dict(cached.get("runtime") or {})
            cached_contract["compiler_status"] = "cache_hit"
            cached_contract["compiler_cache_hit"] = True
            runtime = {
                **cached_runtime,
                "schema_version": "agvm.semantic_contract_runtime.v1",
                "enabled": bool(cached_runtime.get("enabled", True)),
                "ai_required": bool(cached_runtime.get("ai_required") or cached_contract.get("ai_required")),
                "status": "cache_hit",
                "source": "semantic_contract_cache",
                "material": bool(cached_runtime.get("material")),
                "error": None,
                "primary_error": None,
                "retry_error": None,
                "retry_used": False,
                "retry_status": "not_needed",
                "cached_primary_error": cached_runtime.get("primary_error"),
                "cached_retry_error": cached_runtime.get("retry_error"),
                "cached_retry_status": cached_runtime.get("retry_status"),
                "compiler_ms": round((time.perf_counter() - started_at) * 1000.0, 2),
                "cached_source": str(cached_runtime.get("source") or "") or None,
                "cached_status": str(cached_runtime.get("status") or "") or None,
                "cached_compiler_ms": cached_runtime.get("compiler_ms"),
                "cache_age_ms": round((time.time() - float(cached.get("stored_at") or time.time())) * 1000.0, 2),
                "cache_hit_count": int(cached.get("hit_count") or 0),
                "cache_tier": str(cached.get("cache_tier") or "memory"),
            }
            return cached_contract, finalize_runtime(runtime, cache_status="hit", cache_hit=True)

    if deferred:
        enabled = bool(llm_enabled())
        status = "deferred" if enabled and ai_required else "not_required" if not ai_required else "fallback"
        source = fallback_source if status in {"deferred", "fallback"} else "deterministic_strict_contract"
        error = None if status in {"deferred", "not_required"} else "llm_disabled"
        fallback_contract["compiler_status"] = status
        fallback_contract["contract_authority"] = source
        fallback_contract["compiler_error"] = error
        runtime = {
            "schema_version": "agvm.semantic_contract_runtime.v1",
            "enabled": enabled,
            "ai_required": ai_required,
            "status": status,
            "source": source,
            "material": False,
            "error": error,
            "compiler_ms": round((time.perf_counter() - started_at) * 1000.0, 2),
        }
        return fallback_contract, finalize_runtime(runtime, cache_status="miss" if cache_enabled else "disabled")
    if not ai_required:
        runtime = {
            "schema_version": "agvm.semantic_contract_runtime.v1",
            "enabled": bool(llm_enabled()),
            "ai_required": False,
            "status": "not_required",
            "source": "deterministic_strict_contract",
            "material": False,
            "error": None,
            "compiler_ms": round((time.perf_counter() - started_at) * 1000.0, 2),
        }
        fallback_contract["contract_authority"] = "deterministic_strict_contract"
        fallback_contract["compiler_status"] = "not_required"
        return fallback_contract, finalize_runtime(runtime, cache_status="not_applicable" if cache_enabled else "disabled")
    if not allow_ai or not llm_enabled():
        runtime = {
            "schema_version": "agvm.semantic_contract_runtime.v1",
            "enabled": bool(llm_enabled()),
            "ai_required": ai_required,
            "status": "fallback",
            "source": fallback_source,
            "material": False,
            "error": "llm_disabled_or_not_allowed",
            "compiler_ms": round((time.perf_counter() - started_at) * 1000.0, 2),
        }
        fallback_contract["compiler_error"] = "llm_disabled_or_not_allowed"
        return fallback_contract, finalize_runtime(runtime, cache_status="miss" if cache_enabled else "disabled")

    if compiler_profile_name == "ultra_first":
        system_prompt = (
            "You are AGVM's first-hop semantic route compiler. Do not answer the user. "
            "Return only the minimal authoritative contract: intent, evidence target ids, forbidden topics, landing strategy, "
            "landing count, document mode and answer voice. The backend expands your targets into path corridors and evidence checks. "
            "Use canonical target ids only; never output node ids, URLs, titles or source labels as evidence targets. "
            "AI owns the target choice; keep the output small enough for a low-latency first MCP payload."
        )
    else:
        system_prompt = (
            "You are AGVM's compact semantic route compiler. Do not answer the user. "
            "Return only the smallest authoritative route contract: intent, evidence target slots, forbidden topics, "
            "landing count and path itinerary. The backend expands targets into detailed evidence contracts and traverses nodes. "
            "Use canonical target ids only; never output node ids, URLs, titles or source labels as evidence targets. "
            "Prefer landing_origin_corridor paths; use explicit_cross_landing_bridge only when the query asks to connect separate areas. "
            "AI validation stays required for broad, document, temporal, relationship, work/project and follow-up queries."
        )
    prompt_payload = {
        "query": query_text,
        "retrieval_mode": retrieval_mode,
        "legacy_contract": legacy_contract,
        "identity_hints": _compact_identity_hints_for_retry(sanitized_identity_hints),
        "canonical_target_ids": sorted(_CANONICAL_TARGET_IDS),
        "fallback_contract_summary": {
            "intent": fallback_contract["intent"],
            "required_sections": fallback_contract["context_contract"]["required_sections"],
            "optional_sections": fallback_contract["context_contract"]["optional_sections"],
            "expected_evidence_targets": [
                str(item.get("target_id") or "")
                for item in list(fallback_contract.get("expected_evidence") or [])
                if isinstance(item, dict) and str(item.get("target_id") or "").strip()
            ],
            "document_mode": fallback_contract["document_contract"]["mode"],
            "target_document_need_contract": fallback_contract.get("target_document_need_contract"),
            "ai_required": fallback_contract["ai_required"],
            "deterministic_seal_allowed": fallback_contract["deterministic_seal_allowed"],
        },
    }
    payload, error = structured_json(
        model=compiler_model(),
        system_prompt=system_prompt,
        user_prompt=json.dumps(prompt_payload, ensure_ascii=False),
        schema_name=compiler_schema_name,
        schema=dict(compiler_profile.get("schema") or _ai_compact_schema()),
        timeout=requested_timeout,
        role="compiler",
        max_output_tokens=compiler_max_output_tokens,
    )
    if payload and not error:
        contract = _merge_ai_payload(fallback_contract=fallback_contract, ai_payload=payload)
        runtime = {
            "schema_version": "agvm.semantic_contract_runtime.v1",
            "enabled": True,
            "ai_required": bool(contract.get("ai_required")),
            "status": "completed",
            "source": "llm",
            "material": True,
            "error": None,
            "compiler_ms": round((time.perf_counter() - started_at) * 1000.0, 2),
            "model": compiler_model(),
            "attempt_count": 1,
            "retry_used": False,
            "retry_status": "not_needed",
            "primary_error": None,
        }
        runtime = finalize_runtime(runtime, cache_status="miss" if cache_enabled else "disabled")
        if cache_enabled and cache_key:
            _store_semantic_contract_cache_entry(cache_key, contract=contract, runtime=runtime)
        return contract, runtime
    primary_error = error or "llm_empty"
    retry_payload = None
    retry_error: str | None = None
    retry_status = "not_attempted"
    if ai_required and allow_ai and llm_enabled():
        retry_status = "running"
        retry_system_prompt = (
            "You are AGVM's compact semantic route compiler retry path. "
            "Return the smallest valid route contract. Do not answer the user. "
            "AI owns semantic targets, landings, exclusions and validation. Use canonical target ids only."
        )
        retry_payload, retry_error = structured_json(
            model=compiler_model(),
            system_prompt=retry_system_prompt,
            user_prompt=json.dumps(
                _semantic_retry_prompt_payload(
                    query_text=query_text,
                    retrieval_mode=retrieval_mode,
                    legacy_contract=legacy_contract,
                    fallback_contract=fallback_contract,
                    identity_hints=identity_hints,
                    primary_error=primary_error,
                ),
                ensure_ascii=False,
            ),
            schema_name=compiler_schema_name,
            schema=dict(compiler_profile.get("schema") or _ai_compact_schema()),
            timeout=retry_timeout,
            role="compiler",
            max_output_tokens=min(compiler_max_output_tokens, 520),
        )
        if retry_payload and not retry_error:
            contract = _merge_ai_payload(fallback_contract=fallback_contract, ai_payload=retry_payload)
            runtime = {
                "schema_version": "agvm.semantic_contract_runtime.v1",
                "enabled": True,
                "ai_required": bool(contract.get("ai_required")),
                "status": "completed",
                "source": "llm",
                "material": True,
                "error": None,
                "compiler_ms": round((time.perf_counter() - started_at) * 1000.0, 2),
                "model": compiler_model(),
                "attempt_count": 2,
                "retry_used": True,
                "retry_status": "recovered",
                "retry_timeout_seconds": retry_timeout,
                "primary_error": primary_error,
                "retry_error": None,
            }
            runtime = finalize_runtime(runtime, cache_status="miss" if cache_enabled else "disabled")
            if cache_enabled and cache_key:
                _store_semantic_contract_cache_entry(cache_key, contract=contract, runtime=runtime)
            return contract, runtime
        retry_status = "failed"
    fallback_contract["compiler_error"] = retry_error or primary_error
    runtime = {
        "schema_version": "agvm.semantic_contract_runtime.v1",
        "enabled": True,
        "ai_required": ai_required,
        "status": "fallback",
        "source": fallback_source,
        "material": False,
        "error": retry_error or primary_error,
        "compiler_ms": round((time.perf_counter() - started_at) * 1000.0, 2),
        "model": compiler_model(),
        "attempt_count": 2 if retry_status in {"running", "failed"} else 1,
        "retry_used": retry_status in {"running", "failed"},
        "retry_status": retry_status,
        "retry_timeout_seconds": retry_timeout if retry_status in {"running", "failed"} else None,
        "primary_error": primary_error,
        "retry_error": retry_error,
    }
    return fallback_contract, finalize_runtime(runtime, cache_status="miss" if cache_enabled else "disabled")
