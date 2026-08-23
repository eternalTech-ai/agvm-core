# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

from typing import Any


def build_public_v1_landing_contract(
    *,
    query_text: str,
    retrieval_mode: str,
    brain_revision: str | None,
    **_kwargs: Any,
) -> dict[str, Any]:
    """Return a bounded neutral plan while the deterministic V1 retriever runs.

    Public Core does not ship the private legacy planning contract. Retrieval
    continues through its existing deterministic candidate and path pipeline.
    """

    return {
        "schema_version": "agvm.public_v1_landing_plan.v1",
        "status": "not_enabled",
        "source": "public_v1_deterministic_retrieval",
        "materialized": False,
        "certifiable": False,
        "query_present": bool(str(query_text or "").strip()),
        "retrieval_mode": str(retrieval_mode or "balanced"),
        "brain_revision": str(brain_revision or "") or None,
        "inverse_answer_paths": [],
        "missing_reasons": ["optional_ai_landing_planner_not_in_public_core"],
        "metrics": {"ai_landing_count": 0, "ai_path_count": 0},
        "cache": {"status": "disabled", "hit": False},
    }
