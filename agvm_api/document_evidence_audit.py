# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Iterable, Mapping


DOCUMENT_EVIDENCE_AUDIT_SCHEMA_VERSION = "agvm.document_evidence_audit.v1"

REQUIRED_DOCUMENT_METADATA_FIELDS = (
    "document_anchor_id",
    "source_label",
    "source_type",
    "source_unit_id",
    "source_unit_title",
    "source_unit_kind",
    "source_unit_role",
    "retrieval_affordance",
)

_DOCUMENT_ANCHOR_MEMORY_TYPES = {"document_anchor", "source_anchor"}
_DOCUMENT_CHILD_MEMORY_TYPES = {"document_chunk", "document_fact", "document_summary", "source_unit"}
_DOCUMENT_CHILD_ROLES = {"chunk", "fact", "summary"}


def build_document_brain_audit(
    nodes: Iterable[Mapping[str, Any]],
    *,
    brain_id: str | None = None,
    expected_document_ids: Iterable[str] | None = None,
    source: str = "runtime_graph",
    sample_limit: int = 12,
) -> dict[str, Any]:
    """Build a read-only report for document/source metadata readiness.

    The audit intentionally does not rank documents and does not mutate runtime state.
    It answers whether the graph/source corpus has enough stable document identity,
    provenance, raw availability and eligibility metadata for later DWE ranking.
    """

    node_list = [_as_dict(node) for node in nodes]
    node_ids = {_clean(node.get("id")) for node in node_list if _clean(node.get("id"))}
    expected_ids = {_clean(item) for item in expected_document_ids or [] if _clean(item)}

    anchors: list[dict[str, Any]] = []
    children: list[dict[str, Any]] = []
    children_by_anchor: dict[str, list[dict[str, Any]]] = defaultdict(list)
    missing_anchor_children: list[str] = []

    for node in node_list:
        if _is_document_anchor(node):
            anchors.append(_anchor_entry(node))
        if _is_document_child(node):
            children.append(node)
            anchor_id = _clean(node.get("document_anchor_id") or node.get("derived_from_preview_id"))
            if anchor_id and anchor_id in node_ids:
                children_by_anchor[anchor_id].append(node)
            else:
                missing_anchor_children.append(_clean(node.get("id")) or "<missing-node-id>")

    anchor_ids = [_clean(anchor.get("document_anchor_id")) for anchor in anchors]
    anchor_id_counts = Counter(anchor_id for anchor_id in anchor_ids if anchor_id)
    duplicate_anchor_ids = sorted(anchor_id for anchor_id, count in anchor_id_counts.items() if count > 1)
    actual_document_ids = set(anchor_id_counts)
    actual_document_ids.update(_clean(anchor.get("node_id")) for anchor in anchors if _clean(anchor.get("node_id")))
    expected_missing = sorted(expected_ids - actual_document_ids) if expected_ids else []
    unexpected_document_ids = sorted(actual_document_ids - expected_ids) if expected_ids else []

    missing_metadata: dict[str, list[dict[str, str]]] = {field: [] for field in REQUIRED_DOCUMENT_METADATA_FIELDS}
    raw_ready_count = 0
    raw_lengths: list[int] = []
    source_unit_ready_count = 0
    retrieval_affordance_ready_count = 0
    document_eligible_count = 0
    document_ineligible_samples: list[dict[str, str]] = []
    anchor_answer_eligible_samples: list[dict[str, str]] = []

    for anchor in anchors:
        anchor_id = _clean(anchor.get("document_anchor_id"))
        node_id = _clean(anchor.get("node_id"))
        raw_length = int(anchor.get("raw_text_char_count") or 0)
        raw_lengths.append(raw_length)
        has_raw = raw_length > 0 or bool(children_by_anchor.get(node_id)) or bool(children_by_anchor.get(anchor_id))
        if has_raw:
            raw_ready_count += 1
        if anchor.get("document_eligible") is True:
            document_eligible_count += 1
        else:
            document_ineligible_samples.append(_sample(anchor))
        if anchor.get("answer_eligible") is True:
            anchor_answer_eligible_samples.append(_sample(anchor))
        if _clean(anchor.get("source_unit_id")) and _clean(anchor.get("source_unit_title")):
            source_unit_ready_count += 1
        if bool(anchor.get("retrieval_affordance")):
            retrieval_affordance_ready_count += 1
        for field in REQUIRED_DOCUMENT_METADATA_FIELDS:
            if not _field_present(anchor, field):
                missing_metadata[field].append(_sample(anchor))

    missing_metadata_counts = {field: len(samples) for field, samples in missing_metadata.items()}
    metadata_coverage = {
        field: _coverage(len(anchors) - missing_count, len(anchors))
        for field, missing_count in missing_metadata_counts.items()
    }
    blocked_reasons: list[str] = []
    watch_reasons: list[str] = []
    if expected_ids and not anchors:
        blocked_reasons.append("expected_documents_but_no_document_anchors")
    if duplicate_anchor_ids:
        blocked_reasons.append("duplicate_document_anchor_ids")
    if expected_missing:
        blocked_reasons.append("expected_document_ids_missing")
    if len([anchor_id for anchor_id in anchor_ids if not anchor_id]) > 0:
        blocked_reasons.append("document_anchor_id_missing")
    if document_ineligible_samples:
        blocked_reasons.append("document_anchor_not_document_eligible")
    if anchors and raw_ready_count < len(anchors):
        blocked_reasons.append("raw_unavailable_for_some_document_anchors")
    if missing_anchor_children:
        watch_reasons.append("document_children_missing_anchor")
    if any(missing_metadata_counts[field] for field in ("source_label", "source_type")):
        watch_reasons.append("document_provenance_incomplete")
    if any(missing_metadata_counts[field] for field in ("source_unit_id", "source_unit_title", "source_unit_kind", "source_unit_role")):
        watch_reasons.append("source_unit_metadata_incomplete")
    if missing_metadata_counts["retrieval_affordance"]:
        watch_reasons.append("retrieval_affordance_incomplete")
    if anchor_answer_eligible_samples:
        watch_reasons.append("document_anchors_answer_eligible")

    status = "ready"
    if blocked_reasons:
        status = "blocked"
    elif watch_reasons:
        status = "watch"

    return {
        "schema_version": DOCUMENT_EVIDENCE_AUDIT_SCHEMA_VERSION,
        "source": source,
        "brain_id": brain_id,
        "non_mutating": True,
        "status": status,
        "passed": status != "blocked",
        "blocked_reasons": blocked_reasons,
        "watch_reasons": watch_reasons,
        "counts": {
            "node_count": len(node_list),
            "document_anchor_count": len(anchors),
            "document_child_count": len(children),
            "raw_ready_document_anchor_count": raw_ready_count,
            "document_eligible_anchor_count": document_eligible_count,
            "document_ineligible_anchor_count": len(document_ineligible_samples),
            "anchor_answer_eligible_count": len(anchor_answer_eligible_samples),
            "source_unit_ready_anchor_count": source_unit_ready_count,
            "retrieval_affordance_ready_anchor_count": retrieval_affordance_ready_count,
            "missing_anchor_child_count": len(missing_anchor_children),
            "duplicate_document_anchor_id_count": len(duplicate_anchor_ids),
            "expected_document_missing_count": len(expected_missing),
        },
        "coverage": {
            "raw_ready_ratio": _coverage(raw_ready_count, len(anchors)),
            "document_eligible_ratio": _coverage(document_eligible_count, len(anchors)),
            "source_unit_ready_ratio": _coverage(source_unit_ready_count, len(anchors)),
            "retrieval_affordance_ready_ratio": _coverage(retrieval_affordance_ready_count, len(anchors)),
            "metadata": metadata_coverage,
        },
        "expected_documents": {
            "expected_count": len(expected_ids),
            "matched_count": len(expected_ids - set(expected_missing)) if expected_ids else 0,
            "missing_ids": expected_missing[:sample_limit],
            "unexpected_ids_sample": unexpected_document_ids[:sample_limit],
        },
        "duplicate_document_anchor_ids": duplicate_anchor_ids[:sample_limit],
        "missing_metadata_counts": missing_metadata_counts,
        "missing_metadata_samples": {
            field: samples[:sample_limit] for field, samples in missing_metadata.items() if samples
        },
        "document_ineligible_samples": document_ineligible_samples[:sample_limit],
        "anchor_answer_eligible_samples": anchor_answer_eligible_samples[:sample_limit],
        "missing_anchor_child_sample": missing_anchor_children[:sample_limit],
        "raw_text_length_distribution": _length_distribution(raw_lengths),
    }


def external_documents_to_audit_nodes(
    documents: Iterable[Any],
    *,
    dataset_id: str = "",
    source_kind: str = "external_dataset",
) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for document in documents:
        metadata = _as_dict(_object_get(document, "metadata", {}))
        doc_id = _clean(_object_get(document, "doc_id", metadata.get("doc_id")))
        if not doc_id:
            continue
        title = _clean(_object_get(document, "title", metadata.get("title"))) or doc_id
        text = str(_object_get(document, "text", metadata.get("text")) or "")
        source_label = _clean(metadata.get("source_label")) or title
        source_type = _clean(metadata.get("source_type")) or source_kind or "external_dataset"
        source_unit_id = _clean(metadata.get("source_unit_id")) or doc_id
        source_unit_title = _clean(metadata.get("source_unit_title")) or title
        retrieval_affordance = metadata.get("retrieval_affordance")
        if not isinstance(retrieval_affordance, dict):
            retrieval_affordance = {
                "dataset_id": dataset_id,
                "external_doc_id": doc_id,
                "lookup_keys": [value for value in (doc_id, title) if value],
            }
        nodes.append(
            {
                "id": doc_id,
                "node_kind": "external_document",
                "memory_type": "document_anchor",
                "raw_text": text,
                "summary": title,
                "is_document_anchor": True,
                "document_role": "anchor",
                "document_anchor_id": doc_id,
                "source_unit_id": source_unit_id,
                "source_unit_title": source_unit_title,
                "source_unit_kind": _clean(metadata.get("source_unit_kind")) or "external_document",
                "source_unit_role": _clean(metadata.get("source_unit_role")) or "primary",
                "provenance": {
                    "source_label": source_label,
                    "source_type": source_type,
                    "dataset_id": dataset_id,
                    **{key: value for key, value in metadata.items() if key not in {"retrieval_affordance"}},
                },
                "answer_eligible": False,
                "document_eligible": True,
                "retrieval_affordance": retrieval_affordance,
            }
        )
    return nodes


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _object_get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _lower(value: Any) -> str:
    return _clean(value).lower()


def _provenance(node: Mapping[str, Any]) -> dict[str, Any]:
    return _as_dict(node.get("provenance"))


def _is_document_anchor(node: Mapping[str, Any]) -> bool:
    return (
        bool(node.get("is_document_anchor"))
        or _lower(node.get("document_role")) == "anchor"
        or _lower(node.get("memory_type")) in _DOCUMENT_ANCHOR_MEMORY_TYPES
        or _lower(node.get("node_kind")) in _DOCUMENT_ANCHOR_MEMORY_TYPES
    )


def _is_document_child(node: Mapping[str, Any]) -> bool:
    return (
        _lower(node.get("document_role")) in _DOCUMENT_CHILD_ROLES
        or _lower(node.get("memory_type")) in _DOCUMENT_CHILD_MEMORY_TYPES
    )


def _anchor_entry(node: Mapping[str, Any]) -> dict[str, Any]:
    provenance = _provenance(node)
    node_id = _clean(node.get("id"))
    document_anchor_id = _clean(node.get("document_anchor_id")) or node_id
    retrieval_affordance = node.get("retrieval_affordance")
    if not isinstance(retrieval_affordance, dict):
        retrieval_affordance = {}
    return {
        "node_id": node_id,
        "document_anchor_id": document_anchor_id,
        "title": _clean(node.get("source_unit_title") or node.get("summary") or provenance.get("source_label")),
        "source_label": _clean(provenance.get("source_label") or node.get("source_label")),
        "source_type": _clean(provenance.get("source_type") or node.get("source_type")),
        "source_unit_id": _clean(node.get("source_unit_id")),
        "source_unit_title": _clean(node.get("source_unit_title")),
        "source_unit_kind": _clean(node.get("source_unit_kind")),
        "source_unit_role": _clean(node.get("source_unit_role")),
        "retrieval_affordance": retrieval_affordance,
        "raw_text_char_count": len(str(node.get("raw_text") or "")),
        "answer_eligible": bool(node.get("answer_eligible", True)),
        "document_eligible": bool(node.get("document_eligible", True)),
        "memory_type": _clean(node.get("memory_type")),
        "document_role": _clean(node.get("document_role") or "anchor"),
    }


def _field_present(anchor: Mapping[str, Any], field: str) -> bool:
    if field == "retrieval_affordance":
        return bool(anchor.get("retrieval_affordance"))
    return bool(_clean(anchor.get(field)))


def _sample(anchor: Mapping[str, Any]) -> dict[str, str]:
    return {
        "node_id": _clean(anchor.get("node_id")),
        "document_anchor_id": _clean(anchor.get("document_anchor_id")),
        "source_label": _clean(anchor.get("source_label")),
    }


def _coverage(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(max(0.0, min(1.0, numerator / denominator)), 6)


def _length_distribution(lengths: list[int]) -> dict[str, Any]:
    if not lengths:
        return {"count": 0, "min": 0, "p50": 0, "p90": 0, "max": 0, "avg": 0.0, "buckets": {}}
    ordered = sorted(max(0, int(value)) for value in lengths)
    buckets = {
        "empty": sum(1 for value in ordered if value == 0),
        "small_1_199": sum(1 for value in ordered if 1 <= value < 200),
        "medium_200_1999": sum(1 for value in ordered if 200 <= value < 2000),
        "large_2000_plus": sum(1 for value in ordered if value >= 2000),
    }
    return {
        "count": len(ordered),
        "min": ordered[0],
        "p50": _percentile(ordered, 0.5),
        "p90": _percentile(ordered, 0.9),
        "max": ordered[-1],
        "avg": round(sum(ordered) / len(ordered), 3),
        "buckets": buckets,
    }


def _percentile(ordered: list[int], ratio: float) -> int:
    if not ordered:
        return 0
    index = int(round((len(ordered) - 1) * ratio))
    return ordered[max(0, min(len(ordered) - 1, index))]
