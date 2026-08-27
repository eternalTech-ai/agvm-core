# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "agvm_api"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

from config import FACET_FIELDS, ROUTING_FIELDS  # noqa: E402
from derivation import preview_bundle  # noqa: E402
from storage import empty_atlas, empty_index  # noqa: E402


def test_v2_empty_ai_deductions_do_not_activate_legacy_heuristics() -> None:
    compiler_payload = {
        "primary_node": {
            "summary": "A reviewed source may legitimately produce only its anchor memory.",
            "memory_type": "fact",
            "routing_semantic_scores": {field: 0.5 for field in ROUTING_FIELDS},
            "routing_facets": {field: 0.5 for field in FACET_FIELDS},
        },
        "derived_nodes": [],
        "merge_decisions": [],
        "identity_resolution_decisions": [],
        "cognitive_write_plan": {"clarification_questions": []},
    }

    bundle = preview_bundle(
        "A reviewed source may legitimately produce only its anchor memory.",
        "text",
        {"version": "test", "graph_name": "test", "nodes": [], "edges": [], "meta": {}},
        empty_index(),
        empty_atlas(),
        compiler_payload_override=compiler_payload,
        compiler_execution_metadata={"status": "completed"},
        question_limit=12,
        require_ai=True,
    )

    assert bundle["schema_version"] == "agvm.grow_preview_bundle.v2"
    assert bundle["derivation_mode"] == "llm"
    assert bundle["derived_nodes"] == []
    assert bundle["merge_decisions"] == []
    assert bundle["identity_resolution_decisions"] == []
    assert not any("fallback" in str(item.get("code") or "") for item in bundle["warnings"])
