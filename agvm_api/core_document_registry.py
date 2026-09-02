# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import json
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

from runtime_scope import current_brain_id, current_data_dir


_MAX_PREVIEW_DOCUMENTS = 128
_PREVIEW_DOCUMENTS_FILENAME = "agvm_preview_documents_v0.json"
_LOCK = threading.RLock()
_PREVIEW_DOCUMENTS_BY_KEY: dict[str, dict[str, Any]] = {}
_PREVIEW_DOCUMENT_ORDER: list[str] = []
_LOADED_REGISTRY_PATHS: set[str] = set()


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _preview_document_keys(record: dict[str, Any]) -> list[str]:
    keys = [
        _clean(record.get("document_id")),
        _clean(record.get("document_ref_id")),
        _clean(record.get("document_anchor_id")),
        _clean(record.get("source_sha256")),
        _clean(record.get("canonical_text_sha256")),
    ]
    keys.extend(_clean(item) for item in list(record.get("chunk_ids") or []))
    return [key for key in keys if key]


def _preview_registry_path(storage_path: str | None = None) -> Path | None:
    try:
        base = Path(str(storage_path or "").strip()).expanduser().resolve() if storage_path else current_data_dir()
    except Exception:  # noqa: BLE001
        return None
    return base / _PREVIEW_DOCUMENTS_FILENAME


def _index_preview_document_locked(record: dict[str, Any]) -> None:
    document_id = _clean(record.get("document_id"))
    if not document_id:
        return
    if document_id in _PREVIEW_DOCUMENT_ORDER:
        _PREVIEW_DOCUMENT_ORDER.remove(document_id)
    _PREVIEW_DOCUMENT_ORDER.append(document_id)
    for key in _preview_document_keys(record):
        _PREVIEW_DOCUMENTS_BY_KEY[key] = deepcopy(record)
    _trim_preview_documents_locked()


def _trim_preview_documents_locked() -> None:
    while len(_PREVIEW_DOCUMENT_ORDER) > _MAX_PREVIEW_DOCUMENTS:
        expired_document_id = _PREVIEW_DOCUMENT_ORDER.pop(0)
        for key, record in list(_PREVIEW_DOCUMENTS_BY_KEY.items()):
            if _clean(record.get("document_id")) == expired_document_id:
                _PREVIEW_DOCUMENTS_BY_KEY.pop(key, None)


def _load_preview_documents_from_disk(storage_path: str | None = None) -> None:
    path = _preview_registry_path(storage_path)
    if path is None:
        return
    path_key = str(path)
    with _LOCK:
        if path_key in _LOADED_REGISTRY_PATHS:
            return
        _LOADED_REGISTRY_PATHS.add(path_key)
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return
    records = [
        dict(item)
        for item in list(payload.get("documents") or payload.get("records") or [])
        if isinstance(item, dict)
    ]
    with _LOCK:
        for record in records[-_MAX_PREVIEW_DOCUMENTS:]:
            _index_preview_document_locked(record)


def _persist_preview_documents_locked(storage_path: str | None = None, brain_id: str | None = None) -> None:
    path = _preview_registry_path(storage_path)
    if path is None:
        return
    documents: list[dict[str, Any]] = []
    seen: set[str] = set()
    for document_id in _PREVIEW_DOCUMENT_ORDER[-_MAX_PREVIEW_DOCUMENTS:]:
        if document_id in seen:
            continue
        record = _PREVIEW_DOCUMENTS_BY_KEY.get(document_id)
        if not record:
            continue
        seen.add(document_id)
        documents.append(deepcopy(record))
    payload = {
        "schema_version": "agvm.preview_document_registry.v0",
        "brain_id": _clean(brain_id) or current_brain_id(),
        "retention": {
            "max_preview_documents": _MAX_PREVIEW_DOCUMENTS,
            "original_binary_retained": False,
            "storage": "canonical_text_receipt_only",
        },
        "documents": documents,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f"{path.name}.tmp")
        temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temp_path.replace(path)
    except Exception:  # noqa: BLE001
        return


def remember_preview_document(
    *,
    document_receipt: dict[str, Any],
    document_ref: dict[str, Any],
    hydration_page: dict[str, Any],
    brain_id: str | None = None,
    storage_path: str | None = None,
) -> None:
    document_id = _clean(document_receipt.get("document_id"))
    if not document_id:
        return
    raw_text = _clean(hydration_page.get("content"))
    record = {
        "schema_version": "agvm.preview_document_registry.v0",
        "brain_id": _clean(brain_id) or current_brain_id(),
        "document_id": document_id,
        "document_ref_id": _clean(document_receipt.get("document_ref_id")),
        "document_anchor_id": _clean(document_receipt.get("document_anchor_id")),
        "chunk_ids": [key for key in (_clean(item) for item in list(document_receipt.get("chunk_ids") or [])) if key],
        "source_sha256": _clean(document_receipt.get("source_sha256")),
        "canonical_text_sha256": _clean(document_receipt.get("canonical_text_sha256")),
        "canonical_url": document_ref.get("canonical_url"),
        "content_hash": _clean(document_ref.get("content_hash")),
        "source_label": _clean(document_receipt.get("source_label")) or document_id,
        "source_kind": _clean(document_receipt.get("source_kind")) or "uploaded_document",
        "raw_text": raw_text,
        "raw_text_char_count": int(hydration_page.get("content_char_count") or len(raw_text)),
        "hydration_page": deepcopy(hydration_page),
        "document_ref": deepcopy(document_ref),
        "document_receipt": deepcopy(document_receipt),
    }
    with _LOCK:
        _index_preview_document_locked(record)
        _persist_preview_documents_locked(storage_path=storage_path, brain_id=brain_id)


def preview_document_record(identifier: str | None, *, storage_path: str | None = None) -> dict[str, Any] | None:
    key = _clean(identifier)
    if not key:
        return None
    _load_preview_documents_from_disk(storage_path)
    with _LOCK:
        record = _PREVIEW_DOCUMENTS_BY_KEY.get(key)
        return deepcopy(record) if record else None


def preview_document_node(identifier: str | None, *, storage_path: str | None = None) -> dict[str, Any] | None:
    record = preview_document_record(identifier, storage_path=storage_path)
    if not record:
        return None
    document_id = _clean(record.get("document_id"))
    if not document_id:
        return None
    raw_text = _clean(record.get("raw_text"))
    provenance = {
        "source_type": "uploaded_document_preview",
        "source_label": _clean(record.get("source_label")),
        "source_uri": record.get("canonical_url"),
        "document_id": document_id,
        "document_ref_id": _clean(record.get("document_ref_id")),
        "document_anchor_id": _clean(record.get("document_anchor_id")),
        "source_sha256": _clean(record.get("source_sha256")),
        "canonical_text_sha256": _clean(record.get("canonical_text_sha256")),
        "content_hash": _clean(record.get("content_hash")),
        "preview_only": True,
    }
    return {
        "id": document_id,
        "document_id": document_id,
        "document_anchor_id": _clean(record.get("document_anchor_id")) or None,
        "summary": _clean(record.get("source_label")) or document_id,
        "raw_text": raw_text,
        "source_label": _clean(record.get("source_label")),
        "source_type": _clean(record.get("source_kind")) or "uploaded_document",
        "source_trust": "uploaded_document",
        "claim_status": "fact",
        "answer_eligible": True,
        "profile_eligible": True,
        "is_document_anchor": True,
        "source_sha256": _clean(record.get("source_sha256")),
        "canonical_text_sha256": _clean(record.get("canonical_text_sha256")),
        "provenance": provenance,
    }
