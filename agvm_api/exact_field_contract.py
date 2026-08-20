from __future__ import annotations

import re
import unicodedata
from typing import Any


EXACT_FIELD_SLOT_IDS = {"private_identifier", "personal_contact", "exact_user_field"}


def fold_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_only = "".join(char for char in normalized if not unicodedata.combining(char))
    sanitized = re.sub(r"[^\w\s]", " ", ascii_only.lower())
    return " ".join(sanitized.strip().split())


def _slug(value: Any) -> str:
    folded = fold_text(value)
    return "_".join(token for token in folded.split()[:8] if token) or "requested_field"


def _clean_field_phrase(value: str) -> str:
    text = fold_text(value)
    for separator in (" per favore ", " oppure ", " o ", " e ", " grazie "):
        if separator.strip() in text:
            text = text.split(separator.strip(), 1)[0].strip()
    text = re.sub(r"\b(?:si chiama|e|qual|quale|dimmi|trova|ricordi|sai)\b", " ", text)
    text = re.sub(r"\b(?:il|lo|la|l|i|le|mio|mia|miei|mie|del|della|dello|dei|degli|delle)\b", " ", text)
    return " ".join(text.split()[:8]).strip()


_FIELD_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "slot_id": "private_identifier",
        "field_key": "tax_code",
        "field_label": "codice fiscale",
        "section": "identity",
        "aliases": ("codice fiscale", "fiscal code", "tax code", "tax id"),
    },
    {
        "slot_id": "private_identifier",
        "field_key": "passport_number",
        "field_label": "numero di passaporto",
        "section": "identity",
        "aliases": ("numero di passaporto", "passaporto", "passport number", "passport"),
    },
    {
        "slot_id": "private_identifier",
        "field_key": "identity_document",
        "field_label": "documento di identita",
        "section": "identity",
        "aliases": (
            "documento di identita",
            "documento d identita",
            "numero documento di identita",
            "carta identita",
            "carta d identita",
            "numero carta identita",
            "numero di carta d identita",
            "identity document",
            "identity document number",
            "id card",
            "id card number",
        ),
    },
    {
        "slot_id": "private_identifier",
        "field_key": "phone_number",
        "field_label": "numero di telefono",
        "section": "identity",
        "aliases": ("numero di telefono", "telefono", "phone number", "mobile number", "cellulare"),
    },
    {
        "slot_id": "private_identifier",
        "field_key": "email_address",
        "field_label": "indirizzo email",
        "section": "identity",
        "aliases": ("indirizzo email", "email", "mail address", "email address"),
    },
    {
        "slot_id": "private_identifier",
        "field_key": "private_credentials",
        "field_label": "credenziali private",
        "section": "identity",
        "aliases": (
            "credenziali private",
            "credenziali personali",
            "credenziali salvate",
            "password private",
            "password personale",
            "codice segreto privato",
            "codice segreto personale",
            "private credentials",
            "personal credentials",
            "private password",
            "secret code",
        ),
    },
    {
        "slot_id": "private_identifier",
        "field_key": "home_address",
        "field_label": "indirizzo di casa",
        "section": "identity",
        "aliases": (
            "indirizzo di casa",
            "indirizzo casa",
            "indirizzo privato",
            "indirizzo personale",
            "indirizzo abitazione",
            "home address",
            "private address",
            "residential address",
        ),
    },
    {
        "slot_id": "personal_contact",
        "field_key": "accountant",
        "field_label": "commercialista",
        "section": "relationships",
        "aliases": ("commercialista", "accountant", "tax advisor", "consulente fiscale"),
    },
    {
        "slot_id": "personal_contact",
        "field_key": "lawyer",
        "field_label": "avvocato",
        "section": "relationships",
        "aliases": ("avvocato", "lawyer", "legal advisor", "consulente legale"),
    },
    {
        "slot_id": "personal_contact",
        "field_key": "doctor",
        "field_label": "medico",
        "section": "relationships",
        "aliases": ("medico", "doctor", "physician", "dottore"),
    },
)


def _definition_for_query(folded_query: str) -> dict[str, Any] | None:
    for definition in _FIELD_DEFINITIONS:
        for alias in definition["aliases"]:
            alias_folded = fold_text(alias)
            if alias_folded and alias_folded in folded_query:
                return dict(definition)
    return None


def _generic_possessive_field(folded_query: str) -> str:
    patterns = (
        r"\b(?:qual e|quale e|dimmi|trova|ricordi|sai)\s+(?:il|lo|la|l|i|le)?\s*(?:mio|mia|miei|mie)\s+([a-z0-9 ]{3,80})",
        r"\bcome si chiama\s+(?:il|lo|la|l)?\s*(?:mio|mia|miei|mie)\s+([a-z0-9 ]{3,80})",
        r"\bchi e\s+(?:il|lo|la|l)?\s*(?:mio|mia|miei|mie)\s+([a-z0-9 ]{3,80})",
    )
    for pattern in patterns:
        match = re.search(pattern, folded_query)
        if not match:
            continue
        field = _clean_field_phrase(match.group(1))
        if field and field not in {"nome", "lavoro", "progetto", "progetti", "aziende", "storia"}:
            return field
    return ""


def _classify_generic_field(field_label: str) -> tuple[str, str]:
    folded = fold_text(field_label)
    if any(
        token in folded
        for token in (
            "codice",
            "numero",
            "passaporto",
            "telefono",
            "email",
            "mail",
            "indirizzo",
            "residenza",
            "abitazione",
            "casa",
            "documento",
            "identita",
            "id",
        )
    ):
        return "private_identifier", "identity"
    if any(token in folded for token in ("commercialista", "avvocato", "medico", "dentista", "consulente", "advisor")):
        return "personal_contact", "relationships"
    return "exact_user_field", "identity"


def extract_exact_user_field_request(query_text: Any) -> dict[str, Any] | None:
    folded_query = fold_text(query_text)
    if not folded_query:
        return None
    definition = _definition_for_query(folded_query)
    if definition is None:
        field_label = _generic_possessive_field(folded_query)
        if not field_label:
            return None
        slot_id, section = _classify_generic_field(field_label)
        definition = {
            "slot_id": slot_id,
            "field_key": _slug(field_label),
            "field_label": field_label,
            "section": section,
            "aliases": (field_label,),
        }
    aliases = tuple(dict.fromkeys(fold_text(alias) for alias in definition.get("aliases", ()) if fold_text(alias)))
    field_key = str(definition.get("field_key") or _slug(definition.get("field_label"))).strip()
    slot_id = str(definition.get("slot_id") or "exact_user_field").strip()
    slot_key = f"{slot_id}:{field_key}" if field_key else slot_id
    return {
        "schema_version": "agvm.exact_user_field_request.v1",
        "slot_id": slot_id,
        "slot_key": slot_key,
        "field_key": field_key,
        "field_label": str(definition.get("field_label") or field_key).strip(),
        "section": str(definition.get("section") or "identity").strip(),
        "required_terms": list(aliases),
        "query_text": str(query_text or ""),
        "no_match_policy": "strict",
    }


def text_satisfies_exact_field_request(text: Any, request: dict[str, Any] | None) -> bool:
    req = dict(request or {})
    folded_text = fold_text(text)
    if not folded_text:
        return False
    terms = [fold_text(term) for term in list(req.get("required_terms") or []) if fold_text(term)]
    if any(term in folded_text for term in terms):
        return True
    field_label = fold_text(req.get("field_label"))
    if not field_label:
        return False
    significant = [token for token in field_label.split() if len(token) >= 3]
    return bool(significant and all(token in folded_text for token in significant))


def exact_field_semantic_slot_contract(
    request: dict[str, Any],
    *,
    required: bool = True,
    legacy_slot: str | None = None,
    disallowed_topics: list[str] | None = None,
) -> dict[str, Any]:
    req = dict(request or {})
    slot_id = str(req.get("slot_id") or "exact_user_field")
    slot_key = str(req.get("slot_key") or slot_id)
    field_label = str(req.get("field_label") or slot_key)
    field_key = str(req.get("field_key") or slot_key)
    section = str(req.get("section") or "identity")
    negative = [
        "unrelated_biography",
        "adjacent_profile_context",
        "family_or_company_context_without_requested_field",
        "source_heading_without_person_fact",
        "system_metadata",
        "synthetic_test_material",
    ]
    negative.extend(str(topic or "").strip() for topic in list(disallowed_topics or []) if str(topic or "").strip())
    return {
        "schema_version": "agvm.semantic_slot_contract.v1",
        "slot_id": slot_id,
        "slot_key": slot_key,
        "section": section,
        "required": bool(required),
        "legacy_slot": str(legacy_slot or slot_id),
        "legacy_slots": [str(legacy_slot or slot_id)],
        "relation_subtype": field_key if slot_id == "personal_contact" else "",
        "field_key": field_key,
        "requested_field_label": field_label,
        "exact_field_request": req,
        "required_fields": ["person", "requested_exact_field", f"field:{field_key}"],
        "positive_evidence": [f"explicit {field_label} evidence", "direct value for the requested field"],
        "negative_conditions": list(dict.fromkeys(negative))[:16],
        "forbidden_evidence": list(dict.fromkeys(negative))[:16],
        "negative_evidence": list(dict.fromkeys(negative))[:16],
        "success_question": f"Does promoted evidence explicitly contain the requested field '{field_label}' for this query?",
    }


def exact_field_request_from_slot_contract(slot_contract: dict[str, Any] | None) -> dict[str, Any] | None:
    contract = dict(slot_contract or {})
    request = contract.get("exact_field_request")
    if isinstance(request, dict) and request:
        return dict(request)
    slot_id = str(contract.get("slot_id") or "").strip()
    if slot_id not in EXACT_FIELD_SLOT_IDS:
        return None
    field_key = str(contract.get("field_key") or contract.get("relation_subtype") or slot_id).strip()
    field_label = str(contract.get("requested_field_label") or field_key).strip()
    return {
        "schema_version": "agvm.exact_user_field_request.v1",
        "slot_id": slot_id,
        "slot_key": str(contract.get("slot_key") or f"{slot_id}:{field_key}"),
        "field_key": field_key,
        "field_label": field_label,
        "section": str(contract.get("section") or "identity"),
        "required_terms": [fold_text(field_label)] if field_label else [],
        "no_match_policy": "strict",
    }
