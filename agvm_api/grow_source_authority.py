# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def grow_source_temporal_provenance(source_unit: Mapping[str, Any]) -> dict[str, str | None]:
    """Return the canonical source-level timestamps covered by Grow integrity."""

    acquisition_proof = _mapping(source_unit.get("acquisition_proof"))
    provenance = _mapping(source_unit.get("provenance"))
    published_at = str(
        source_unit.get("published_at") or provenance.get("published_at") or ""
    ).strip()
    acquired_at = str(
        source_unit.get("acquired_at") or acquisition_proof.get("acquired_at") or ""
    ).strip()
    retrieved_at = str(
        source_unit.get("retrieved_at") or provenance.get("retrieved_at") or ""
    ).strip()
    return {
        "source_published_at": published_at or None,
        "source_acquired_at": acquired_at or None,
        "source_retrieved_at": retrieved_at or None,
    }


def grow_source_sha256(source_investigation: Mapping[str, Any]) -> str:
    """Hash the canonical Grow source facts once for runtime and persistence."""

    source_units = source_investigation.get("source_units")
    try:
        units = list(source_units or [])[:10_000]
    except TypeError:
        units = []
    material: list[dict[str, Any]] = []
    for unit_value in units:
        if not isinstance(unit_value, Mapping):
            continue
        unit = unit_value
        raw_text = str(unit.get("raw_text") or "")
        material.append(
            {
                "source_unit_id": str(unit.get("unit_id") or ""),
                "content_sha256": f"sha256:{hashlib.sha256(raw_text.encode('utf-8')).hexdigest()}",
                "fact_eligible": bool(unit.get("fact_eligible", True)),
                **grow_source_temporal_provenance(unit),
            }
        )
    encoded = json.dumps(material, ensure_ascii=True, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
