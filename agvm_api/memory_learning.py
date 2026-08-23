# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

from typing import Any, Protocol


MEMORY_LEARNING_EVENT_SCHEMA_VERSION = "agvm.memory_learning_event.v1"
SOURCE_REFERENCE_SCHEMA_VERSION = "agvm.source_reference.v1"
SOURCE_ASSET_SCHEMA_VERSION = "agvm.source_asset.v1"
MATRIX_REVISION_SCHEMA_VERSION = "agvm.matrix_revision.v1"
TOPOLOGY_FIELD_REVISION_SCHEMA_VERSION = "agvm.topology_field_revision.v1"
MEMORY_POLICY_REVISION_SCHEMA_VERSION = "agvm.memory_policy_revision.v1"
MEMORY_LEARNING_STORE_ADAPTER_SCHEMA_VERSION = "agvm.memory_learning_store_adapter.v1"
MEMORY_LEARNING_CAPABILITY_SCHEMA_VERSION = "agvm.memory_learning_store_capability.v1"
COGNITIVE_JOB_SCHEMA_VERSION = "agvm.cognitive_job.v1"

MEMORY_LEARNING_REQUIRED_TABLES: tuple[str, ...] = (
    "memory_learning_events",
    "source_references",
    "source_assets",
    "matrix_revisions",
    "topology_field_revisions",
    "memory_policy_revisions",
    "cognitive_jobs",
)

MEMORY_LEARNING_EVENT_KINDS: frozenset[str] = frozenset(
    {
        "source_detected",
        "source_unit_created",
        "source_asset_created",
        "candidate_previewed",
        "candidate_selected",
        "candidate_rejected",
        "candidate_merged",
        "candidate_suppressed_duplicate",
        "clarification_requested",
        "clarification_answered",
        "contradiction_detected",
        "deduction_proposed",
        "hypothesis_proposed",
        "node_persisted",
        "sleep_queue_created",
        "evolve_queue_created",
        "matrix_hint_created",
        "maintenance_proposal_applied",
        "matrix_revision_applied",
        "query_quality_observation_created",
        "node_shape_feedback_created",
        "deduction_candidate_created",
        "memory_policy_revision_proposed",
        "memory_policy_revision_applied",
        "background_job_scheduled",
        "background_job_blocked",
        "background_job_completed",
        "background_job_cancelled",
    }
)


class MemoryLearningStoreAdapter(Protocol):
    """Shared local/cloud storage boundary for durable memory-learning state."""

    def append_memory_learning_event(self, event: dict[str, Any]) -> dict[str, Any]:
        ...

    def fetch_memory_learning_events(self, **filters: Any) -> list[dict[str, Any]]:
        ...

    def upsert_source_reference(self, source_reference: dict[str, Any]) -> dict[str, Any]:
        ...

    def upsert_source_asset(self, source_asset: dict[str, Any]) -> dict[str, Any]:
        ...

    def store_matrix_revision(self, revision: dict[str, Any]) -> dict[str, Any]:
        ...

    def store_topology_field_revision(self, revision: dict[str, Any]) -> dict[str, Any]:
        ...

    def store_memory_policy_revision(self, revision: dict[str, Any]) -> dict[str, Any]:
        ...

    def capability_report(self) -> dict[str, Any]:
        ...


def memory_learning_contract_versions() -> dict[str, str]:
    return {
        "adapter": MEMORY_LEARNING_STORE_ADAPTER_SCHEMA_VERSION,
        "event": MEMORY_LEARNING_EVENT_SCHEMA_VERSION,
        "source_reference": SOURCE_REFERENCE_SCHEMA_VERSION,
        "source_asset": SOURCE_ASSET_SCHEMA_VERSION,
        "matrix_revision": MATRIX_REVISION_SCHEMA_VERSION,
        "topology_field_revision": TOPOLOGY_FIELD_REVISION_SCHEMA_VERSION,
        "memory_policy_revision": MEMORY_POLICY_REVISION_SCHEMA_VERSION,
        "cognitive_job": COGNITIVE_JOB_SCHEMA_VERSION,
    }


def build_memory_learning_capability_report(
    *,
    storage_backend: str,
    tables: dict[str, dict[str, Any]],
    writable: bool,
    brain_id: str | None = None,
) -> dict[str, Any]:
    missing_tables = [table for table in MEMORY_LEARNING_REQUIRED_TABLES if not bool(tables.get(table, {}).get("present"))]
    return {
        "schema_version": MEMORY_LEARNING_CAPABILITY_SCHEMA_VERSION,
        "adapter_schema_version": MEMORY_LEARNING_STORE_ADAPTER_SCHEMA_VERSION,
        "storage_backend": str(storage_backend or "unknown"),
        "brain_id": str(brain_id).strip() if brain_id else None,
        "writable": bool(writable),
        "ready": not missing_tables,
        "missing_tables": missing_tables,
        "tables": {name: dict(tables.get(name) or {}) for name in MEMORY_LEARNING_REQUIRED_TABLES},
        "contracts": memory_learning_contract_versions(),
        "event_kinds": sorted(MEMORY_LEARNING_EVENT_KINDS),
        "local_cloud_parity": {
            "same_contracts_required": True,
            "cloud_only_cognitive_behavior_allowed": False,
            "storage_adapter_may_differ": True,
            "auth_and_tenant_adapter_may_differ": True,
        },
        "mutation_contract": {
            "learning_events_append_only": True,
            "source_reference_upsert_allowed": True,
            "source_asset_upsert_allowed": True,
            "matrix_revision_activation_requires_apply_gate": True,
            "topology_revision_activation_requires_apply_gate": True,
            "memory_policy_revision_activation_requires_apply_gate": True,
            "cognitive_jobs_are_policy_gated": True,
            "automatic_cycles_require_entitlement_policy": True,
            "hidden_memory_node_mutation_allowed": False,
        },
        "source_asset_policy": {
            "schema_version": "agvm.source_asset_policy.v1",
            "source_references_preserve_original_uri_when_policy_allows": True,
            "source_assets_may_store_ocr_text": True,
            "source_assets_may_store_vision_summary": True,
            "binary_storage_adapter_defined_by_runtime": True,
            "requires_human_confirmation_flag_supported": True,
            "redaction_policy_required_on_source_references": True,
            "normal_context_must_not_inline_raw_assets_by_default": True,
            "local_cloud_contract_same_storage_backend_may_differ": True,
        },
    }


__all__ = [
    "MEMORY_LEARNING_CAPABILITY_SCHEMA_VERSION",
    "MEMORY_LEARNING_EVENT_KINDS",
    "MEMORY_LEARNING_EVENT_SCHEMA_VERSION",
    "MEMORY_POLICY_REVISION_SCHEMA_VERSION",
    "MEMORY_LEARNING_REQUIRED_TABLES",
    "MEMORY_LEARNING_STORE_ADAPTER_SCHEMA_VERSION",
    "COGNITIVE_JOB_SCHEMA_VERSION",
    "MATRIX_REVISION_SCHEMA_VERSION",
    "MemoryLearningStoreAdapter",
    "SOURCE_ASSET_SCHEMA_VERSION",
    "SOURCE_REFERENCE_SCHEMA_VERSION",
    "TOPOLOGY_FIELD_REVISION_SCHEMA_VERSION",
    "build_memory_learning_capability_report",
    "memory_learning_contract_versions",
]
