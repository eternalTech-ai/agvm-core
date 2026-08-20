from __future__ import annotations

import hashlib
import re
from typing import Any

from storage import utc_timestamp


HOT_WORKING_MEMORY_SCHEMA_VERSION = "agvm.hot_working_memory.v1"
HOT_WORKING_MEMORY_CONTRACT_SCHEMA_VERSION = "agvm.hot_working_memory_contract.v1"
DEFAULT_MAX_HOT_ITEMS = 24
DEFAULT_MAX_ITEM_CHARS = 1400

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]{3,}")
_STOPWORDS = {
    "about",
    "ancora",
    "anche",
    "come",
    "con",
    "cosa",
    "della",
    "delle",
    "dello",
    "degli",
    "dei",
    "del",
    "dimmi",
    "does",
    "fammi",
    "for",
    "hai",
    "his",
    "ill",
    "nel",
    "per",
    "qual",
    "quale",
    "quali",
    "raccontami",
    "show",
    "sono",
    "the",
    "tua",
    "tuo",
    "tue",
    "what",
}


def _clean_text(value: Any, *, limit: int = DEFAULT_MAX_ITEM_CHARS) -> str:
    text = " ".join(str(value or "").replace("\r", "\n").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _tokens(value: Any, *, limit: int = 96) -> list[str]:
    seen: set[str] = set()
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(str(value or "").lower()):
        token = match.group(0).strip("_")
        if not token or token in _STOPWORDS or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
        if len(tokens) >= limit:
            break
    return tokens


def _item_id(node_id: str, text: str) -> str:
    if node_id:
        return f"node:{node_id}"
    digest = hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()[:16]
    return f"text:{digest}"


def _section_key(value: Any) -> str:
    section = str(value or "").strip().lower().replace(" ", "_")
    return section or "general"


def _iter_context_package_items(context_package: dict[str, Any]) -> list[dict[str, Any]]:
    package = dict(context_package or {})
    items: list[dict[str, Any]] = []
    for entry in list(package.get("hot_context") or []):
        if isinstance(entry, dict):
            items.append(
                {
                    "node_id": str(entry.get("node_id") or "").strip(),
                    "section": _section_key(entry.get("section")),
                    "text": entry.get("text"),
                    "source_title": entry.get("source_title"),
                    "source_kind": entry.get("source_kind"),
                    "source": "context_package.hot_context",
                }
            )
    for section in list(package.get("hot_sections") or package.get("sections") or []):
        if not isinstance(section, dict):
            continue
        section_key = _section_key(section.get("key") or section.get("title"))
        source_title = section.get("title")
        for value in list(section.get("items") or []):
            if isinstance(value, dict):
                text = value.get("text") or value.get("summary") or value.get("raw_text")
                node_id = str(value.get("node_id") or value.get("id") or "").strip()
                title = value.get("source_title") or source_title
                source_kind = value.get("source_kind")
            else:
                text = value
                node_id = ""
                title = source_title
                source_kind = "context_section"
            items.append(
                {
                    "node_id": node_id,
                    "section": section_key,
                    "text": text,
                    "source_title": title,
                    "source_kind": source_kind,
                    "source": "context_package.sections",
                }
            )
    deduped: dict[str, dict[str, Any]] = {}
    for item in items:
        text = _clean_text(item.get("text"))
        if not text:
            continue
        node_id = str(item.get("node_id") or "").strip()
        identity = _item_id(node_id, text)
        deduped.setdefault(
            identity,
            {
                "item_id": identity,
                "node_id": node_id or None,
                "section": _section_key(item.get("section")),
                "text": text,
                "source_title": str(item.get("source_title") or "").strip() or None,
                "source_kind": str(item.get("source_kind") or "").strip() or None,
                "source": str(item.get("source") or "context_package"),
            },
        )
    return list(deduped.values())


def _iter_document_refs(context_package: dict[str, Any], document_workspace: dict[str, Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    containers = [
        dict(context_package or {}).get("document_workspace"),
        document_workspace,
    ]
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in ("documents", "primary_documents", "related_documents"):
            for index, document in enumerate(list(container.get(key) or [])):
                if not isinstance(document, dict):
                    continue
                title = _clean_text(document.get("title") or document.get("source_label") or "Document", limit=220)
                raw_text = str(document.get("raw_text") or document.get("full_text") or document.get("text") or "")
                refs.append(
                    {
                        "document_ref_id": str(document.get("document_id") or document.get("node_id") or document.get("id") or f"{key}:{index}"),
                        "title": title,
                        "collection": key,
                        "raw_text_available": bool(raw_text.strip() or document.get("raw_text_available")),
                        "raw_text_char_count": len(raw_text),
                        "source_kind": str(document.get("source_kind") or document.get("source") or "document"),
                        "lookup_role": str(document.get("lookup_role") or ""),
                    }
                )
    deduped: dict[str, dict[str, Any]] = {}
    for ref in refs:
        key = f"{ref.get('document_ref_id')}:{ref.get('title')}"
        deduped.setdefault(key, ref)
    return list(deduped.values())[:16]


def _existing_items(existing: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in list(existing.get("items") or []):
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("item_id") or _item_id(str(item.get("node_id") or ""), str(item.get("text") or ""))).strip()
        if item_id:
            out[item_id] = dict(item)
    return out


def _sort_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        items,
        key=lambda item: (
            1 if str(item.get("state") or "hot") == "hot" else 0,
            int(item.get("hits") or 0),
            str(item.get("last_seen_at") or ""),
        ),
        reverse=True,
    )


def build_hot_working_memory_update(
    existing: dict[str, Any] | None,
    *,
    brain_id: str,
    thread_id: str,
    query_text: str,
    search_id: str,
    context_package: dict[str, Any] | None = None,
    context_package_materialization: dict[str, Any] | None = None,
    document_workspace: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    source: str = "retrieve_context",
    max_hot_items: int = DEFAULT_MAX_HOT_ITEMS,
) -> dict[str, Any]:
    timestamp = utc_timestamp()
    existing_packet = dict(existing or {})
    previous_items = _existing_items(existing_packet)
    incoming_items = _iter_context_package_items(dict(context_package or {}))
    seen_item_ids = {str(item.get("item_id") or "") for item in incoming_items if str(item.get("item_id") or "")}
    merged = previous_items
    for item in incoming_items:
        item_id = str(item.get("item_id") or "")
        previous = dict(merged.get(item_id) or {})
        merged[item_id] = {
            **previous,
            **item,
            "state": "hot",
            "first_seen_search_id": previous.get("first_seen_search_id") or search_id,
            "last_seen_search_id": search_id,
            "first_seen_at": previous.get("first_seen_at") or timestamp,
            "last_seen_at": timestamp,
            "hits": int(previous.get("hits") or 0) + 1,
            "last_update_source": source,
        }
    hot_items = _sort_items([dict(item) for item in merged.values() if str(item.get("state") or "hot") == "hot"])
    kept_items = hot_items[: max(1, int(max_hot_items))]
    overflow_items = hot_items[max(1, int(max_hot_items)) :]
    demoted_items = [
        {
            **dict(item),
            "state": "demoted",
            "demoted_at": timestamp,
            "demotion_reason": "hot_working_memory_capacity",
        }
        for item in overflow_items
    ]
    stale_demotions = [
        {
            **dict(item),
            "state": "demoted",
            "demoted_at": timestamp,
            "demotion_reason": "not_refreshed_by_latest_package",
        }
        for item in kept_items
        if seen_item_ids and str(item.get("item_id") or "") not in seen_item_ids and int(item.get("hits") or 0) <= 1
    ][:8]
    demotion_log = [*stale_demotions, *demoted_items, *list(existing_packet.get("demoted_items") or [])][:32]
    document_refs = _iter_document_refs(dict(context_package or {}), dict(document_workspace or {}))
    previous_document_refs = [dict(ref) for ref in list(existing_packet.get("hot_document_refs") or []) if isinstance(ref, dict)]
    ref_by_key = {
        str(ref.get("document_ref_id") or ref.get("title") or ""): ref
        for ref in [*previous_document_refs, *document_refs]
        if str(ref.get("document_ref_id") or ref.get("title") or "").strip()
    }
    materialization = dict(context_package_materialization or {})
    package_revision_id = str(materialization.get("package_revision_id") or f"{search_id}:package").strip()
    source_run_ids = [str(item) for item in list(existing_packet.get("source_run_ids") or []) if str(item).strip()]
    if search_id and search_id not in source_run_ids:
        source_run_ids.insert(0, search_id)
    package_revision_ids = [str(item) for item in list(existing_packet.get("package_revision_ids") or []) if str(item).strip()]
    if package_revision_id and package_revision_id not in package_revision_ids:
        package_revision_ids.insert(0, package_revision_id)
    result_payload = dict(result or {})
    read_node_ids = [
        *[str(item.get("node_id") or "") for item in kept_items if str(item.get("node_id") or "").strip()],
        *[str(node_id) for node_id in list(result_payload.get("visited_node_ids") or []) if str(node_id).strip()],
    ]
    read_bucket_keys = [str(key) for key in list(result_payload.get("visited_bucket_keys") or []) if str(key).strip()]
    token_estimate = max(0, sum(len(str(item.get("text") or "")) for item in kept_items) // 4)
    return {
        "schema_version": HOT_WORKING_MEMORY_SCHEMA_VERSION,
        "brain_id": str(brain_id or "").strip(),
        "thread_id": str(thread_id or "").strip(),
        "state": "hot_available" if kept_items else "empty",
        "last_query_text": str(query_text or "").strip(),
        "last_used_at": timestamp,
        "updated_at": timestamp,
        "source_run_ids": source_run_ids[:24],
        "package_revision_ids": package_revision_ids[:24],
        "hot_node_ids": list(dict.fromkeys(node_id for node_id in read_node_ids if node_id))[:64],
        "hot_document_refs": list(ref_by_key.values())[:16],
        "items": kept_items,
        "read_ledger": {
            "last_search_id": search_id,
            "node_ids": list(dict.fromkeys(node_id for node_id in read_node_ids if node_id))[:96],
            "bucket_keys": list(dict.fromkeys(read_bucket_keys))[:96],
            "package_revision_id": package_revision_id or None,
        },
        "continuity_summary": dict(result_payload.get("continuity_summary") or existing_packet.get("continuity_summary") or {}),
        "pinned_items": [dict(item) for item in list(existing_packet.get("pinned_items") or []) if isinstance(item, dict)][:16],
        "demoted_items": demotion_log,
        "expired_items": [dict(item) for item in list(existing_packet.get("expired_items") or []) if isinstance(item, dict)][:16],
        "token_budget": {
            "estimated_tokens": token_estimate,
            "max_hot_items": int(max_hot_items),
            "item_count": len(kept_items),
            "demoted_count": len(demotion_log),
        },
        "update_delta": {
            "source": source,
            "incoming_item_count": len(incoming_items),
            "kept_item_count": len(kept_items),
            "demoted_item_count": len(demotion_log),
            "document_ref_count": len(list(ref_by_key.values())),
        },
    }


def summarize_hot_working_memory(packet: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(packet or {})
    return {
        "schema_version": "agvm.hot_working_memory_summary.v1",
        "state": str(payload.get("state") or "empty"),
        "brain_id": payload.get("brain_id"),
        "thread_id": payload.get("thread_id"),
        "item_count": len(list(payload.get("items") or [])),
        "hot_node_count": len(list(payload.get("hot_node_ids") or [])),
        "document_ref_count": len(list(payload.get("hot_document_refs") or [])),
        "demoted_count": len(list(payload.get("demoted_items") or [])),
        "estimated_tokens": int(dict(payload.get("token_budget") or {}).get("estimated_tokens") or 0),
        "last_search_id": dict(payload.get("read_ledger") or {}).get("last_search_id"),
        "last_used_at": payload.get("last_used_at"),
    }


def build_hot_working_memory_contract(
    packet: dict[str, Any] | None,
    *,
    query_text: str,
    search_id: str | None = None,
) -> dict[str, Any]:
    payload = dict(packet or {})
    items = [dict(item) for item in list(payload.get("items") or []) if isinstance(item, dict)]
    query_tokens = set(_tokens(query_text))
    reused_items: list[dict[str, Any]] = []
    for item in items:
        item_tokens = set(_tokens(f"{item.get('section')} {item.get('text')} {item.get('source_title')}"))
        overlap = sorted(query_tokens & item_tokens)
        if overlap:
            reused_items.append(
                {
                    "item_id": item.get("item_id"),
                    "node_id": item.get("node_id"),
                    "section": item.get("section"),
                    "overlap_terms": overlap[:12],
                    "text_preview": _clean_text(item.get("text"), limit=220),
                }
            )
    available = bool(items)
    reusable = bool(reused_items)
    if reusable:
        reuse_state = "reused_candidate"
        guard_reason = "query_overlaps_hot_working_memory"
    elif available:
        reuse_state = "available_guarded_not_promoted"
        guard_reason = "no_query_overlap_hot_memory_kept_separate"
    else:
        reuse_state = "cold_start"
        guard_reason = "no_hot_working_memory"
    return {
        "schema_version": HOT_WORKING_MEMORY_CONTRACT_SCHEMA_VERSION,
        "search_id": search_id,
        "thread_id": payload.get("thread_id"),
        "state": reuse_state,
        "reuse_state": reuse_state,
        "available": available,
        "reused_for_query": reusable,
        "reused_items": reused_items[:12],
        "reused_item_count": len(reused_items),
        "reused_node_ids": [str(item.get("node_id") or "") for item in reused_items if str(item.get("node_id") or "").strip()][:24],
        "available_item_count": len(items),
        "hot_node_count": len(list(payload.get("hot_node_ids") or [])),
        "document_ref_count": len(list(payload.get("hot_document_refs") or [])),
        "demoted_item_count": len(list(payload.get("demoted_items") or [])),
        "separate_from_context_package": True,
        "included_in_mcp_context_package": False,
        "must_be_explicitly_promoted_to_package": True,
        "stale_guard": {
            "promotion_allowed": reusable,
            "reason": guard_reason,
            "unrelated_query_cannot_silently_promote_hot_memory": True,
        },
        "ui_contract": {
            "surface_label": "Hot working memory",
            "not_the_mcp_payload": True,
            "compare_against_context_package": True,
        },
    }
